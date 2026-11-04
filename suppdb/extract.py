"""Rule-based extraction of reported effect sizes (SMD/MD/WMD/RR/OR/HR with 95% CI).

Only numbers literally printed in an abstract or open-access full text are stored; nothing is
imputed. Each row keeps the exact snippet it came from. Incremental: (article, supplement)
pairs whose extracted_version equals EXTRACTOR_VERSION are skipped; bump the version to
re-extract. Rows are de-duplicated by a UNIQUE key, so re-running is safe.

Full text: for on-topic RCTs / meta-analyses with a PMCID whose abstract yielded no effect for
an analyzed outcome, the Europe PMC open-access XML is fetched (cached in fulltext_cache) and
its Results-type sections are scanned with the same rules (source='fulltext', confidence one
notch lower).
"""
import argparse, datetime as dt, math, re, sys
import xml.etree.ElementTree as ET
from common import connect, load_config, http_get

EXTRACTOR_VERSION = 2
NUM = r"[-]?\d+(?:\.\d+)?"
UNIT = r"(?:mm ?Hg|kg|g/d[lL]|g/[lL]|mg/d[lL]|mg/[lL]|nmol/[lL]|[\u00b5u]mol/[lL]|pmol/[lL]|mmol/[lL]|ng/m[lL]|I?U/[lL]|g\b|%|points?|cm)"
# Abbreviations are matched case-sensitively with word boundaries ("or" in "for" is not OR).
MEAS = (r"(?P<meas>\bstandardi[sz]ed mean differences?|weighted mean differences?|mean differences?|"
        r"Hedges'? ?g\b|relative risks?|risk ratios?|rate ratios?|odds ratios?|hazard ratios?|"
        r"\b(?-i:SMD|WMD|MD|RR|OR|HR|IRR)\b)")
CI = r"95\s*%\s*(?:CI|confidence intervals?|CrI|credible intervals?)(?:\s*[\[(]\s*CIs?\s*[\])])?"
SEP = r"\s*(?:to|,|;|–|—|\s-\s|-(?=\d)|-(?=-)|\s|\.\.)\s*"
# "SMD = 0.31, 95% CI: 0.18-0.44"
PAT = re.compile(
    MEAS + r"\s*(?:\((?:[A-Z]{2,4})\))?\s*(?:[=:,]|of|was|were|is)?\s*(?P<val>" + NUM + r")\s*(?P<u1>" + UNIT + r")?\s*"
    r"(?:[,;(\[]\s*)*" + CI + r"\s*[:=,]?\s*[\[(]?\s*(?P<lo>" + NUM + r")\s*(?P<u2>" + UNIT + r")?" + SEP + r"(?P<hi>" + NUM + r")\s*(?P<u3>" + UNIT + r")?",
    re.I)
# "reduced SBP by 2.4 mmHg (95% CI 1.1 to 3.7)" -> MD (medium confidence), unit required
PAT_BY = re.compile(
    r"(?P<dir>reduc\w*|decreas\w*|lower\w*|increas\w*|rais\w*|improv\w*|elevat\w*)[^.;]{0,60}?\bby\s+(?P<val>" + NUM + r")\s*(?P<u1>" + UNIT + r")\s*"
    r"[,;(\[]\s*" + CI + r"\s*[:=,]?\s*[\[(]?\s*(?P<lo>" + NUM + r")\s*(?P<u2>" + UNIT + r")?" + SEP + r"(?P<hi>" + NUM + r")\s*(?P<u3>" + UNIT + r")?", re.I)
# "difference in X was 6.85 kg (95% CI 5.24-8.47)" -> MD (medium), unit required
PAT_DIFF = re.compile(
    r"(?:net )?(?:difference|net change|net reduction|net increase)[^.;]{0,80}?(?:was|were|of|=|:)\s*(?P<val>" + NUM + r")\s*(?P<u1>" + UNIT + r")\s*"
    r"[,;(\[]\s*" + CI + r"\s*[:=,]?\s*[\[(]?\s*(?P<lo>" + NUM + r")\s*(?P<u2>" + UNIT + r")?" + SEP + r"(?P<hi>" + NUM + r")\s*(?P<u3>" + UNIT + r")?", re.I)
# "estimates (95% CI) were -0.6 (-2.2 to 1.0) mm Hg for systolic BP" -> outcome named AFTER the number
PAT_FOR = re.compile(
    r"(?P<val>" + NUM + r")\s*(?P<u1>" + UNIT + r")?\s*[\[(]\s*(?P<lo>" + NUM + r")" + SEP + r"(?P<hi>" + NUM + r")\s*[\])]\s*(?P<u3>" + UNIT + r")?\s+(?:for|in|on)\s+"
    r"(?P<out>[A-Za-z][^,;.()\d]{2,50}?)(?=\s+and\s|\s+or\s|\s+vs|[,;.()\[\]]|\s*$)", re.I)

def norm_measure(m):
    m = m.lower()
    if m.startswith("standardi") or m == "smd" or m.startswith("hedges"): return "SMD"
    if m.startswith("weighted") or m == "wmd": return "MD"
    if m.startswith("mean diff") or m == "md": return "MD"
    if m.startswith(("relative risk", "risk ratio", "rate ratio")) or m in ("rr", "irr"): return "RR"
    if m.startswith("odds") or m == "or": return "OR"
    if m.startswith("hazard") or m == "hr": return "HR"
    return None

def clean(t):
    t = t or ""
    for a, b in (("−", "-"), ("‐", "-"), ("‑", "-"), ("·", "."), ("\u2009", " "), ("\xa0", " "), ("–", " to "), ("—", " to ")):
        t = t.replace(a, b)
    return t

def norm_unit(u):
    if not u: return None
    u = u.replace(" ", "")
    return {"mmhg": "mmHg", "nmol/l": "nmol/L", "ng/ml": "ng/mL", "mg/dl": "mg/dL", "mmol/l": "mmol/L", "g/dl": "g/dL", "g/l": "g/L",
            "mg/l": "mg/L", "\u00b5mol/l": "umol/L", "umol/l": "umol/L", "pmol/l": "pmol/L", "u/l": "U/L", "iu/l": "U/L"}.get(u.lower(), u)

def sentences(text):
    return re.split(r"(?<=[.!?])\s+(?=[A-Z(])|\n", text)

_RX = {}
def _topic_rx(sup, cfg_sups):
    k = (sup["key"], len(cfg_sups))
    if k not in _RX:
        other_names = [p for s in cfg_sups if s["key"] != sup["key"] for p in s["topic_patterns"]]
        _RX[k] = (re.compile("|".join(sup["topic_patterns"]), re.I),
                  re.compile("|".join(other_names), re.I) if other_names else None)
    return _RX[k]

def find_effects(text, sup, cfg_sups):
    """Yield dicts with outcome, measure, value, ci, unit, snippet, confidence."""
    outs = sup["outcomes"]
    own, other = _topic_rx(sup, cfg_sups)
    for sent in sentences(clean(text)):
        if other and other.search(sent) and not own.search(sent):
            continue  # sentence is about a different supplement (e.g. network MA)
        matches = [(m, "label") for m in PAT.finditer(sent)] + [(m, "by") for m in PAT_BY.finditer(sent)] + \
                  [(m, "diff") for m in PAT_DIFF.finditer(sent)]
        if re.search(r"95\s*%", sent) and not PAT.search(sent):
            matches += [(m, "for") for m in PAT_FOR.finditer(sent)]
        # drop overlapping matches (keep the earliest-starting, labelled first)
        matches.sort(key=lambda x: (x[0].start(), x[1] != "label"))
        kept = []
        for mm in matches:
            if kept and mm[0].start() < kept[-1][0].end(): continue
            kept.append(mm)
        prev_end = 0
        for m, kind in kept:
            if kind == "for":
                clause = m.group("out")
            elif kind == "label":
                clause = sent[prev_end:m.start()]
                if len(clause) < 4:
                    clause = sent[max(0, m.start() - 200):m.start()]
            else:
                clause = sent[max(prev_end, m.start() - 1):m.start("val")]
            prev_end = m.end()
            best = None
            for ok, o in outs.items():
                for p in o["patterns"]:
                    for om in re.finditer(p, clause, re.I):
                        dist = om.start() if kind == "for" else len(clause) - om.end()
                        if best is None or dist < best[1]:
                            best = (ok, dist)
            if not best: continue
            try:
                v, lo, hi = float(m.group("val")), float(m.group("lo")), float(m.group("hi"))
            except ValueError:
                continue
            if kind == "label":
                meas = norm_measure(m.group("meas"))
            elif kind == "for":
                ms = [norm_measure(x.group("meas")) for x in re.finditer(MEAS, sent[:m.start()], re.I)]
                meas = ms[-1] if ms else ("MD" if re.search(r"difference|change|reduction|estimate", sent[:m.start()], re.I) else None)
                if not meas: continue
            else:
                meas = "MD"
            allowed = outs[best[0]].get("measures")
            if allowed and meas not in allowed: continue
            if kind == "by" and v > 0 and min(lo, hi) < 0:
                continue  # sign of the printed CI contradicts the value (source typo): skip
            if kind == "by" and re.match(r"(reduc|decreas|lower)", m.group("dir"), re.I) and v > 0:
                v, lo, hi = -v, -hi, -lo
            if lo > hi: lo, hi = hi, lo
            if not (lo <= v <= hi): continue
            if meas in ("RR", "OR", "HR") and (lo <= 0 or v <= 0): continue
            gd = m.groupdict()
            unit = norm_unit(gd.get("u1") or gd.get("u3") or gd.get("u2"))
            if kind in ("diff", "for") and meas == "MD" and not unit: continue
            if kind == "for" and best[1] > 25: continue
            conf = "high" if best[1] <= 80 and kind == "label" else ("medium" if best[1] <= 250 else None)
            if not conf: continue
            yield dict(outcome=best[0], effect_measure=meas, value=v, ci_low=lo, ci_high=hi, unit=unit,
                       snippet=sent[max(0, m.start() - 160):m.end() + 10].strip(), confidence=conf)

def se_from_ci(meas, lo, hi):
    if meas in ("RR", "OR", "HR"):
        return (math.log(hi) - math.log(lo)) / (2 * 1.959964)
    return (hi - lo) / (2 * 1.959964)

def on_topic(sup, title):
    if any(re.search(re.escape(x), title or "", re.I) for x in sup.get("title_exclude", [])): return False
    return bool(re.search("|".join(sup["topic_patterns"]), title or "", re.I))

def store(cur, pmid, sup, eff, level, source, n_total, k):
    cur.execute("""INSERT OR IGNORE INTO outcomes(pmid,supplement,outcome,comparison,effect_measure,value,ci_low,ci_high,se,unit,
                n_total,k_studies,level,source,confidence,snippet,extractor_version) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (pmid, sup["key"], eff["outcome"], "supplement vs control (as reported)", eff["effect_measure"], eff["value"],
                 eff["ci_low"], eff["ci_high"], se_from_ci(eff["effect_measure"], eff["ci_low"], eff["ci_high"]), eff["unit"],
                 n_total, k, level, source, eff["confidence"], eff["snippet"][:600], EXTRACTOR_VERSION))
    return cur.rowcount

class OfflineError(Exception):
    pass

def fulltext_sections(pmcid, cur=None):
    if cur is not None:
        row = cur.execute("SELECT text FROM fulltext_cache WHERE pmcid=?", (pmcid,)).fetchone()
        if row: return row[0] or ""
        if cur.execute("SELECT 1 FROM fulltext_cache WHERE pmcid=?", (pmcid,)).fetchone(): return ""
    try:
        text = _fetch_fulltext(pmcid)
    except Exception as e:
        if getattr(e, "code", None) is None and not isinstance(e, ET.ParseError):
            raise OfflineError(str(e))   # network down / timeout: do not mark as unavailable
        text = None   # HTTP 4xx/5xx: not open access / not available as XML
    if cur is not None:
        cur.execute("INSERT OR REPLACE INTO fulltext_cache VALUES (?,?)", (pmcid, text))
    return text or ""

def _fetch_fulltext(pmcid):
    raw = http_get(f"https://www.ebi.ac.uk/europepmc/webservices/rest/{pmcid}/fullTextXML", min_interval=0.25, tries=2, timeout=45)
    root = ET.fromstring(raw)
    body = root.find(".//body")
    if body is None: return ""
    out = []
    for sec in body.iter("sec"):
        t = (sec.findtext("title") or "").lower()
        if any(w in t for w in ("result", "finding", "outcome", "efficacy", "effect")):
            out.extend("".join(p.itertext()) for p in sec.findall("p"))
    return "\n".join(out)

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-fulltext", action="store_true")
    ap.add_argument("--only", nargs="*", help="limit to these supplement keys")
    ap.add_argument("--fulltext-limit", type=int, default=None, help="max full-text papers per supplement this run")
    ap.add_argument("--fulltext-budget-min", type=float, default=None, help="stop fetching full text after this many minutes (whole run)")
    args = ap.parse_args()
    con = connect(); cfg = load_config(con); cur = con.cursor()
    import time as _t
    deadline = _t.time() + args.fulltext_budget_min * 60 if args.fulltext_budget_min else None
    ft_limit = args.fulltext_limit if args.fulltext_limit is not None else int(cfg.get("fulltext_max_per_supplement_per_run", 100))
    offline = [0]   # consecutive network failures (shared across supplements)
    def one(sup):
        key = sup["key"]
        started = dt.datetime.now().isoformat(timespec="seconds"); added = 0
        rows = cur.execute("""SELECT a.pmid, a.title, a.abstract, a.study_design, a.n_participants, a.k_studies, a.pmcid, s.extracted_version, s.fulltext_checked
                              FROM articles a JOIN article_supplements s ON s.pmid=a.pmid WHERE s.supplement=?""", (key,)).fetchall()
        ft_candidates = []
        for pmid, title, abstract, design, n, k, pmcid, ver, ftc in rows:
            if not on_topic(sup, title): continue
            level = "pooled" if design in ("meta-analysis", "systematic review") else "primary"
            if ver < EXTRACTOR_VERSION:
                got = 0
                for eff in find_effects((title or "") + "\n" + (abstract or ""), sup, cfg["supplements"]):
                    got += 1; added += store(cur, pmid, sup, eff, level, "abstract", n, k if level == "pooled" else None)
            else:
                got = cur.execute("SELECT count(*) FROM outcomes WHERE pmid=? AND supplement=?", (pmid, key)).fetchone()[0]
            if (not got and pmcid and not ftc and design in ("rct", "meta-analysis", "systematic review")
                    and any(re.search(p, (title or "") + (abstract or ""), re.I) for o in sup["analyze"] for p in sup["outcomes"][o]["patterns"])):
                ft_candidates.append((pmid, pmcid, design, n, k, level))
        # mark abstracts done now, so an interruption during full-text fetching does not redo them
        cur.execute("UPDATE article_supplements SET extracted_version=? WHERE supplement=?", (EXTRACTOR_VERSION, key))
        con.commit()
        ft_done = 0
        if not args.no_fulltext and offline[0] < 3:
            for pmid, pmcid, design, n, k, level in ft_candidates[:ft_limit]:
                if deadline and _t.time() > deadline:
                    print(f"[{key}] full-text time budget reached; the rest continues next run", flush=True); break
                try:
                    text = fulltext_sections(pmcid, cur)
                except OfflineError as e:
                    offline[0] += 1
                    if offline[0] >= 3:
                        print(f"[{key}] full-text server unreachable ({e}); skipping full text for the rest of this run", flush=True)
                        break
                    continue
                offline[0] = 0
                for eff in find_effects(text, sup, cfg["supplements"]):
                    eff["confidence"] = {"high": "medium", "medium": "low"}[eff["confidence"]]
                    added += store(cur, pmid, sup, eff, level, "fulltext", n, k if level == "pooled" else None)
                cur.execute("UPDATE article_supplements SET fulltext_checked=1 WHERE pmid=? AND supplement=?", (pmid, key)); ft_done += 1
                if ft_done % 20 == 0:
                    con.commit(); print(f"[{key}] fulltext {ft_done}", file=sys.stderr, flush=True)
        cur.execute("INSERT INTO runs(started_at, finished_at, step, supplement, new_effects, notes) VALUES (?,?,?,?,?,?)",
                    (started, dt.datetime.now().isoformat(timespec="seconds"), "extract", key, added,
                     f"extractor v{EXTRACTOR_VERSION}; fulltext checked: {ft_done} of {len(ft_candidates)} candidates"))
        con.commit()
        print(f"[{key}] new effects={added} fulltext checked={ft_done}/{len(ft_candidates)}", flush=True)

    failed = []
    for sup in cfg["supplements"]:
        if args.only and sup["key"] not in args.only: continue
        try:
            one(sup)
        except Exception as ex:
            con.rollback(); failed.append(sup["key"])
            print(f"[{sup['key']}] EXTRACT FAILED: {ex}", flush=True)
            import traceback; traceback.print_exc()
    if failed:
        print("extract failed for: " + ", ".join(failed), flush=True); sys.exit(2)

if __name__ == "__main__":
    main()
