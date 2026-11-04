"""Random-effects meta-analysis of extracted effects + database statistics.

Writes output/results.json (everything the report Page needs) and figures/*.png (forest and
funnel plots). Two evidence levels are kept strictly apart:
  primary : between-group effects reported by individual RCTs -> DerSimonian-Laird pool (REML too).
  pooled  : pooled estimates printed by published meta-analyses -> umbrella forest. Their pooled
            diamond is NOT an independent estimate (the same trials appear in many reviews).
Only effects with confidence high/medium and a valid CI are used. Nothing is imputed.
"""
import datetime as dt, json, math, os, re
from collections import Counter, defaultdict
import numpy as np
from scipy import stats
import matplotlib; matplotlib.use("Agg")
import matplotlib.pyplot as plt
from common import connect, load_config, load_catalog, HERE, OUTPUT_DIR, FIG_DIR

RATIO = {"RR", "OR", "HR"}
SUBGROUP = re.compile(r"subgroup|sensitivity|stratif|among those|in (?:the )?(?:older|younger|elderly|women|men|children|athletes|patients with)|when|dose[- ]response|per \d+|trimmed|after excluding|excluding", re.I)
# Observational-association wording: such estimates are not supplementation effects.
ASSOC = re.compile(r"associat|quartile|tertile|quintile|highest (?:vs|versus|compared)|per \d+ ?(?:nmol|ng)|levels? (?:of|below|above) \d|deficien(?:t|cy) (?:was|were)|cohort", re.I)
RCT_WORDS = re.compile(r"randomi[sz]ed|\bRCTs?\b|placebo|controlled trials?|supplementation trials?", re.I)
OBS_WORDS = re.compile(r"observational|cohort|case-control|cross-sectional|prospective studies|epidemiolog", re.I)

def dl(y, v):
    w = 1 / v; ybar = np.sum(w * y) / np.sum(w)
    Q = float(np.sum(w * (y - ybar) ** 2)); df = len(y) - 1
    C = np.sum(w) - np.sum(w ** 2) / np.sum(w)
    tau2 = max(0.0, (Q - df) / C) if df > 0 else 0.0
    ws = 1 / (v + tau2); mu = np.sum(ws * y) / np.sum(ws); se = math.sqrt(1 / np.sum(ws))
    I2 = max(0.0, (Q - df) / Q) * 100 if Q > 0 and df > 0 else 0.0
    return dict(mu=float(mu), se=se, tau2=float(tau2), Q=Q, df=df, I2=I2,
                p_Q=float(1 - stats.chi2.cdf(Q, df)) if df > 0 else None)

def reml(y, v, iters=500):
    """REML tau^2 by fixed-point iteration (Viechtbauer 2005), starting from DL."""
    tau2 = dl(y, v)["tau2"]
    for _ in range(iters):
        w = 1 / (v + tau2); mu = np.sum(w * y) / np.sum(w)
        new = max(0.0, float(np.sum(w ** 2 * ((y - mu) ** 2 - v)) / np.sum(w ** 2) + 1 / np.sum(w)))
        if abs(new - tau2) < 1e-10:
            tau2 = new; break
        tau2 = new
    w = 1 / (v + tau2); mu = np.sum(w * y) / np.sum(w)
    return dict(mu=float(mu), se=float(math.sqrt(1 / np.sum(w))), tau2=float(tau2))

def egger(y, se):
    z = y / se; prec = 1 / se
    res = stats.linregress(prec, z)
    t = res.intercept / res.intercept_stderr; p = 2 * (1 - stats.t.cdf(abs(t), len(y) - 2))
    return dict(intercept=float(res.intercept), se=float(res.intercept_stderr), p=float(p))

def back(meas, x):
    return math.exp(x) if meas in RATIO else x

def forest_png(path, title, rows, pooled, meas, unit, note):
    n = len(rows); h = 1.4 + 0.3 * (n + 2)
    fig, ax = plt.subplots(figsize=(8, h))
    for i, r in enumerate(rows):
        yy = n - i
        ax.plot([r["ci_low"], r["ci_high"]], [yy, yy], color="#1f5fbf", lw=1.2)
        ax.plot(r["value"], yy, "s", color="#1f5fbf", ms=4)
    if pooled:
        lo, mu, hi = pooled["ci_low"], pooled["value"], pooled["ci_high"]
        ax.fill([lo, mu, hi, mu], [0, 0.25, 0, -0.25], color="#c0392b")
    ax.axvline(1 if meas in RATIO else 0, color="grey", ls="--", lw=0.8)
    if meas in RATIO: ax.set_xscale("log")
    ax.set_yticks([n - i for i in range(n)] + ([0] if pooled else []))
    ax.set_yticklabels([r["label"] for r in rows] + (["Pooled (RE, DL)"] if pooled else []), fontsize=7)
    ax.set_xlabel(f"{meas}" + (f" ({unit})" if unit else ""))
    ax.set_title(title + ("\n" + note if note else ""), fontsize=9)
    fig.tight_layout(); fig.savefig(path, dpi=110); plt.close(fig)

def funnel_png(path, title, y, se, mu, meas):
    fig, ax = plt.subplots(figsize=(5, 4))
    ax.scatter(y, se, s=12); m = max(se) * 1.05
    ax.plot([mu - 1.96 * m, mu, mu + 1.96 * m], [m, 0, m], "k--", lw=0.7)
    ax.invert_yaxis(); ax.set_xlabel(("log " if meas in RATIO else "") + meas); ax.set_ylabel("SE")
    ax.set_title(title, fontsize=9); fig.tight_layout(); fig.savefig(path, dpi=110); plt.close(fig)

def conclusion(abstract):
    if not abstract: return ""
    m = re.search(r"(?:CONCLUSIONS?|Conclusions?|INTERPRETATION|IMPLICATIONS)[^:]*:\s*(.+)", abstract)
    s = m.group(1) if m else abstract.split("\n")[-1]
    s = re.split(r"(?<=[.])\s+(?=[A-Z])", s.strip())
    return " ".join(s[:2])[:420]

def main():
    con = connect(); cfg = load_config(con); cur = con.cursor()
    figdir = FIG_DIR; os.makedirs(figdir, exist_ok=True)
    for f in os.listdir(figdir):            # plots are regenerated every run; drop stale ones
        if f.endswith(".png"):
            try: os.remove(os.path.join(figdir, f))
            except OSError: pass  # e.g. open in an image viewer on Windows; it is overwritten below
    ex_snip = re.compile("|".join(cfg.get("analysis_exclude_snippet", [])) or "(?!x)x", re.I)
    ex_title = re.compile("|".join(cfg.get("analysis_exclude_title", [])) or "(?!x)x", re.I)
    art = {r[0]: dict(title=r[1], year=r[2], journal=r[3], design=r[4], n=r[5], k=r[6], abstract=r[7], doi=r[8])
           for r in cur.execute("SELECT pmid,title,year,journal,study_design,n_participants,k_studies,abstract,doi FROM articles")}
    out = dict(generated_at=dt.datetime.now().isoformat(timespec="minutes"), supplements=[], stats={})
    st = out["stats"]
    st["total_articles"] = len(art)
    st["total_effects"] = cur.execute("SELECT count(*) FROM outcomes").fetchone()[0]
    st["by_design"] = dict(Counter(a["design"] for a in art.values()))
    st["by_year"] = dict(sorted(Counter(a["year"] for a in art.values() if a["year"]).items()))
    st["runs"] = [dict(zip(["started_at", "step", "supplement", "hits", "new_records", "new_effects", "notes"], r))
                  for r in cur.execute("SELECT started_at, step, supplement, hits, new_records, new_effects, notes FROM runs ORDER BY id")]
    for s in cfg["supplements"]:
        key = s["key"]
        pm = [r[0] for r in cur.execute("SELECT pmid FROM article_supplements WHERE supplement=?", (key,))]
        sd = dict(key=key, label=s["label"], n_articles=len(pm),
                  by_design=dict(Counter(art[p]["design"] for p in pm if p in art)),
                  by_year=dict(sorted(Counter(art[p]["year"] for p in pm if p in art and art[p]["year"]).items())),
                  n_effects=cur.execute("SELECT count(*) FROM outcomes WHERE supplement=?", (key,)).fetchone()[0],
                  effects_by_outcome=dict(cur.execute("SELECT outcome, count(*) FROM outcomes WHERE supplement=? GROUP BY 1", (key,)).fetchall()),
                  effects_by_source=dict(cur.execute("SELECT source, count(*) FROM outcomes WHERE supplement=? GROUP BY 1", (key,)).fetchall()),
                  effects_by_level=dict(cur.execute("SELECT level, count(*) FROM outcomes WHERE supplement=? GROUP BY 1", (key,)).fetchall()),
                  outcomes=[])
        for ok in s["analyze"]:
            o = s["outcomes"][ok]
            rows = cur.execute("""SELECT id,pmid,effect_measure,value,ci_low,ci_high,se,unit,n_total,k_studies,level,source,confidence,snippet
                                  FROM outcomes WHERE supplement=? AND outcome=? AND confidence IN ('high','medium') ORDER BY id""", (key, ok)).fetchall()
            od = dict(key=ok, label=o["label"], levels={})
            for level in ("primary", "pooled"):
                excluded = Counter(); cand = defaultdict(list)
                for (i, pmid, meas, v, lo, hi, se, unit, n, k, lev, src, conf, snip) in rows:
                    if lev != level or not se or se <= 0: continue
                    ab = art.get(pmid, {}).get("abstract") or ""
                    if ex_title.search(art.get(pmid, {}).get("title") or "") or ex_snip.search(snip[-220:]):
                        excluded["not a supplement-vs-control comparison"] += 1; continue
                    if o.get("analysis_require") and not re.search("|".join(o["analysis_require"]), snip[-220:], re.I):
                        excluded["outcome wording too broad"] += 1; continue
                    if ASSOC.search(snip[-200:]):
                        excluded["association wording"] += 1; continue
                    if level == "pooled" and OBS_WORDS.search(ab) and not RCT_WORDS.search(ab):
                        excluded["meta-analysis of observational studies"] += 1; continue
                    if meas == "MD":
                        u = unit or o.get("default_unit")
                        conv = o.get("unit_conversions", {})
                        if o.get("unitless"):
                            conv = {u or "": 1}
                        if (not u and not o.get("unitless")) or (u or "") not in conv:
                            excluded["MD without convertible unit"] += 1; continue
                        f = conv[u or ""]; v, lo, hi, se, unit = v * f, lo * f, hi * f, se * f, o.get("canonical_unit")
                    sub = bool(SUBGROUP.search(snip[-220:]))
                    cand[(pmid, meas)].append(dict(id=i, pmid=pmid, measure=meas, value=v, ci_low=lo, ci_high=hi, se=se,
                                                   unit=unit if meas == "MD" else None, n=n, k=k, source=src, confidence=conf,
                                                   subgroup=sub, snippet=snip))
                # one effect per paper per measure: first non-subgroup mention (else first mention)
                per = {key2: next((x for x in lst if not x["subgroup"]), lst[0]) for key2, lst in cand.items()}
                # the same review indexed twice (e.g. Cochrane updates) prints identical numbers: keep one
                seen = set()
                for kk in sorted(per, key=lambda kk: per[kk]["id"]):
                    sig = (kk[1], round(per[kk]["value"], 4), round(per[kk]["ci_low"], 4), round(per[kk]["ci_high"], 4))
                    if sig in seen:
                        excluded["duplicate numbers (same review indexed twice)"] += 1; del per[kk]
                    seen.add(sig)
                bymeas = Counter(m for (_, m) in per)
                if not bymeas: continue
                meas = max(bymeas, key=lambda m: (bymeas[m], m == "SMD"))
                eff = sorted([x for (p, m), x in per.items() if m == meas], key=lambda x: (art.get(x["pmid"], {}).get("year") or 0))
                for x in eff:
                    a = art.get(x["pmid"], {})
                    x["year"] = a.get("year"); x["title"] = a.get("title")
                    x["label"] = f"PMID {x['pmid']} ({a.get('year')})" + (f" k={x['k']}" if x.get("k") else "")
                res = dict(measure=meas, unit=eff[0]["unit"], k=len(eff), excluded=dict(excluded),
                           other_measures={m: c for m, c in bymeas.items() if m != meas},
                           effects=[{kk: x[kk] for kk in ("pmid", "year", "title", "value", "ci_low", "ci_high", "n", "k", "source", "confidence", "subgroup", "snippet", "label")} for x in eff])
                null = 1 if meas in RATIO else 0
                res["n_sig_below_null"] = sum(1 for x in eff if x["ci_high"] < null)
                res["n_sig_above_null"] = sum(1 for x in eff if x["ci_low"] > null)
                res["n_ns"] = len(eff) - res["n_sig_below_null"] - res["n_sig_above_null"]
                ref = max(eff, key=lambda x: ((x.get("k") or 0), x.get("year") or 0))
                res["reference"] = {kk: ref[kk] for kk in ("pmid", "year", "title", "value", "ci_low", "ci_high", "k", "n")}
                ns = [x["n"] for x in eff if x["n"]]
                res["total_n"] = int(sum(ns)) if ns else None; res["n_reported_for"] = len(ns)
                if len(eff) >= 2:
                    y = np.array([math.log(x["value"]) if meas in RATIO else x["value"] for x in eff])
                    sev = np.array([x["se"] for x in eff]); v = sev ** 2
                    d = dl(y, v); r = reml(y, v)
                    res.update(pooled=back(meas, d["mu"]), ci_low=back(meas, d["mu"] - 1.96 * d["se"]), ci_high=back(meas, d["mu"] + 1.96 * d["se"]),
                               p=float(2 * (1 - stats.norm.cdf(abs(d["mu"] / d["se"])))), I2=d["I2"], tau2=d["tau2"], Q=d["Q"], p_Q=d["p_Q"],
                               reml_pooled=back(meas, r["mu"]), reml_ci_low=back(meas, r["mu"] - 1.96 * r["se"]), reml_ci_high=back(meas, r["mu"] + 1.96 * r["se"]))
                    if len(eff) >= 10:
                        try:
                            res["egger"] = egger(y, sev)
                        except Exception:
                            pass
                        fp = f"funnel_{key}_{ok}_{level}.png"
                        funnel_png(os.path.join(figdir, fp), f"{s['label']}: {o['label']} ({level})", y, sev, d["mu"], meas); res["funnel_png"] = "figures/" + fp
                    note = "estimates from overlapping meta-analyses; diamond is NOT independent" if level == "pooled" else ""
                    fp = f"forest_{key}_{ok}_{level}.png"
                    forest_png(os.path.join(figdir, fp), f"{s['label']}: {o['label']} - {'published meta-analyses' if level == 'pooled' else 'individual RCTs'}",
                               eff, dict(value=res["pooled"], ci_low=res["ci_low"], ci_high=res["ci_high"]), meas, res["unit"], note)
                    res["forest_png"] = "figures/" + fp
                od["levels"][level] = res
            if od["levels"]: sd["outcomes"].append(od)
        mas = cur.execute("SELECT DISTINCT pmid FROM outcomes WHERE supplement=? AND level='pooled'", (key,)).fetchall()
        sd["meta_analyses"] = sorted([dict(pmid=p, year=art[p]["year"], title=art[p]["title"], journal=art[p]["journal"], k=art[p]["k"], n=art[p]["n"],
                                           conclusion=conclusion(art[p]["abstract"]),
                                           outcomes=sorted({r[0] for r in cur.execute("SELECT outcome FROM outcomes WHERE pmid=? AND supplement=?", (p, key))}))
                                      for (p,) in mas if p in art], key=lambda x: -(x["year"] or 0))
        out["supplements"].append(sd)
    # coverage: which catalog supplements are active and which are queued for later runs
    active = {r[0]: r[1] for r in cur.execute("SELECT supplement, activated_at FROM coverage")}
    out["coverage"] = dict(core=[s["key"] for s in cfg["supplements"] if s["key"] not in active],
                           activated=[dict(key=k, label=c["label"], activated_at=active[k]) for c in load_catalog() for k in [c["key"]] if k in active],
                           queue=[dict(key=c["key"], label=c["label"]) for c in load_catalog() if c["key"] not in active and c["key"] not in {s["key"] for s in cfg["supplements"]}])
    for sd in out["supplements"]:
        sd["fulltext_pending"] = cur.execute("""SELECT count(*) FROM article_supplements s JOIN articles a ON a.pmid=s.pmid
            WHERE s.supplement=? AND s.fulltext_checked=0 AND a.pmcid IS NOT NULL AND a.study_design IN ('rct','meta-analysis','systematic review')
            AND NOT EXISTS (SELECT 1 FROM outcomes o WHERE o.pmid=s.pmid AND o.supplement=s.supplement)""", (sd["key"],)).fetchone()[0]
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    with open(os.path.join(OUTPUT_DIR, "results.json"), "w", encoding="utf-8") as f:
        json.dump(out, f, indent=1, default=float)
    for sd in out["supplements"]:
        print(f"== {sd['label']}: articles={sd['n_articles']} effects={sd['n_effects']} {sd['effects_by_level']}")
        for od in sd["outcomes"]:
            for lev, r in od["levels"].items():
                ex = r.get("excluded")
                if "pooled" in r:
                    print(f"  {od['label']:30s} {lev:7s} {r['measure']:3s} k={r['k']:3d} pooled={r['pooled']:.3f} [{r['ci_low']:.3f},{r['ci_high']:.3f}] "
                          f"REML={r['reml_pooled']:.3f} I2={r['I2']:.0f}% {r.get('unit') or ''} N={r['total_n']} egger_p={r.get('egger', {}).get('p')} sig-/ns/sig+={r['n_sig_below_null']}/{r['n_ns']}/{r['n_sig_above_null']} excl={ex}")
                else:
                    print(f"  {od['label']:30s} {lev:7s} {r['measure']:3s} k={r['k']} (insufficient for pooling) excl={ex}")

if __name__ == "__main__":
    main()
