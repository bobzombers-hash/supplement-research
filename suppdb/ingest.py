"""Incremental PubMed ingestion.

For each supplement in config.json: esearch (query AND design_filter), restricted by
entrez date >= (last successful run date - 7 days overlap) unless --full. PMIDs already
in the DB are only re-tagged, never re-fetched. New PMIDs are efetched (XML, 200/batch)
and parsed into the articles table.
"""
import argparse, datetime as dt, json, re, sys, traceback
import xml.etree.ElementTree as ET
from common import connect, load_config, http_get

EUTILS = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/"

def base_params(cfg):
    p = {"db": "pubmed", "tool": "supplement-research-db"}
    if cfg.get("contact_email"): p["email"] = cfg["contact_email"]
    if cfg.get("ncbi_api_key"): p["api_key"] = cfg["ncbi_api_key"]
    return p

def esearch_count(cfg, term, mindate=None, maxdate=None):
    p = dict(base_params(cfg), term=term, retmode="json", retmax=0)
    if mindate:
        p.update(datetype="edat", mindate=mindate, maxdate=maxdate or "3000")
    return int(json.loads(http_get(EUTILS + "esearch.fcgi", p))["esearchresult"]["count"])

def esearch_ids(cfg, term, mindate=None, maxdate=None):
    """Return all PMIDs; splits by date range if > 9000 hits (esearch caps at 10k)."""
    n = esearch_count(cfg, term, mindate, maxdate)
    if n > 9000:
        lo = int((mindate or "1900")[:4]); hi = int((maxdate or str(dt.date.today().year))[:4])
        if hi > lo:
            mid = (lo + hi) // 2
            return esearch_ids(cfg, term, f"{lo}/01/01", f"{mid}/12/31") | \
                   esearch_ids(cfg, term, f"{mid+1}/01/01", f"{hi}/12/31")
    ids = set(); start = 0
    while start < n:
        p = dict(base_params(cfg), term=term, retmode="json", retmax=5000, retstart=start)
        if mindate:
            p.update(datetype="edat", mindate=mindate, maxdate=maxdate or "3000")
        ids |= set(json.loads(http_get(EUTILS + "esearch.fcgi", p, timeout=120))["esearchresult"]["idlist"])
        start += 5000
    return ids

def txt(el):
    return "".join(el.itertext()).strip() if el is not None else ""

def classify(pub_types, title):
    pt = {p.lower() for p in pub_types}; t = (title or "").lower()
    if "meta-analysis" in pt or "meta-analys" in t or "metaanalys" in t: return "meta-analysis"
    if "systematic review" in pt or "systematic review" in t: return "systematic review"
    if "randomized controlled trial" in pt: return "rct"
    if any(p.startswith("clinical trial") for p in pt) or "controlled clinical trial" in pt: return "other trial"
    if "review" in pt: return "review"
    return "other"

NUMWORD = r"(\d{1,3}(?:,\d{3})+|\d+)"
def extract_n(abstract, design):
    """Heuristic participant / study counts from abstract text."""
    a = abstract or ""
    people = re.findall(NUMWORD + r"\s+(?:[a-z-]+\s+){0,3}?(?:participants|subjects|patients|adults|individuals|men|women|volunteers|children|athletes|persons|people|elderly|infants|pregnant women|older adults)\b", a, re.I)
    ns = [int(x.replace(",", "")) for x in people]
    ns += [int(x.replace(",", "")) for x in re.findall(r"\b[Nn]\s*=\s*" + NUMWORD, a)] if design not in ("meta-analysis", "systematic review") else []
    n = max(ns) if ns else None
    if n and n > 5_000_000: n = None
    k = None
    if design in ("meta-analysis", "systematic review"):
        ks = re.findall(NUMWORD + r"\s+(?:[a-z-]+\s+){0,3}?(?:RCTs|trials|studies|articles)\b", a, re.I)
        ks = [int(x.replace(",", "")) for x in ks if int(x.replace(",", "")) < 2000]
        k = max(ks) if ks else None
    return n, k

def parse_article(art):
    mc = art.find("MedlineCitation"); a = mc.find("Article")
    pmid = mc.findtext("PMID")
    title = txt(a.find("ArticleTitle"))
    parts = []
    for at in a.findall("Abstract/AbstractText"):
        lab = at.get("Label"); s = txt(at)
        parts.append(f"{lab}: {s}" if lab else s)
    abstract = "\n".join(parts)
    journal = a.findtext("Journal/Title") or ""
    year = a.findtext("Journal/JournalIssue/PubDate/Year")
    if not year:
        md = a.findtext("Journal/JournalIssue/PubDate/MedlineDate") or ""
        m = re.search(r"(19|20)\d\d", md); year = m.group(0) if m else None
    if not year:
        year = art.findtext("PubmedData/History/PubMedPubDate[@PubStatus='pubmed']/Year")
    pub_types = [txt(p) for p in a.findall("PublicationTypeList/PublicationType")]
    mesh = [txt(m.find("DescriptorName")) for m in mc.findall("MeshHeadingList/MeshHeading")]
    doi = pmcid = None
    for aid in art.findall("PubmedData/ArticleIdList/ArticleId"):
        if aid.get("IdType") == "doi": doi = aid.text
        if aid.get("IdType") == "pmc": pmcid = aid.text
    mset = set(mesh)
    is_human = 1 if "Humans" in mset else (0 if "Animals" in mset else None)
    design = classify(pub_types, title)
    n, k = extract_n(abstract, design)
    ed = art.find("PubmedData/History/PubMedPubDate[@PubStatus='entrez']")
    pub_date = None
    if ed is not None:
        pub_date = f"{ed.findtext('Year')}-{int(ed.findtext('Month')):02d}-{int(ed.findtext('Day')):02d}"
    return dict(pmid=pmid, pmcid=pmcid, doi=doi, title=title, abstract=abstract, journal=journal,
                year=int(year) if year else None, pub_date=pub_date, pub_types=json.dumps(pub_types),
                mesh_terms=json.dumps(mesh), study_design=design, is_human=is_human,
                n_participants=n, k_studies=k, url=f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/")

def efetch(cfg, ids):
    p = dict(base_params(cfg), id=",".join(ids), retmode="xml")
    root = ET.fromstring(http_get(EUTILS + "efetch.fcgi", p, timeout=180))
    return [parse_article(x) for x in root.findall("PubmedArticle")]

def ingest_one(cfg, con, s, full=False, max_new=None):
    cur = con.cursor(); key = s["key"]
    started = dt.datetime.now().isoformat(timespec="seconds")
    last = cur.execute("SELECT max(started_at) FROM runs WHERE step='ingest' AND supplement=? AND query=? AND finished_at IS NOT NULL",
                       (key, s["search"])).fetchone()[0]
    mindate = None
    if last and not full:
        mindate = (dt.date.fromisoformat(last[:10]) - dt.timedelta(days=7)).strftime("%Y/%m/%d")
    term = f"({s['search']}) {cfg['design_filter']}"
    ids = esearch_ids(cfg, term, mindate)
    have = {r[0] for r in cur.execute("SELECT pmid FROM articles")}
    new = sorted(ids - have, reverse=True)          # newest PMIDs first
    partial = bool(max_new is not None and len(new) > max_new)
    if partial: new = new[:max_new]
    run_id = cur.execute("INSERT INTO runs(started_at, step, supplement, query, mindate, hits) VALUES (?,?,?,?,?,?)",
                         (started, "ingest", key, s["search"], mindate, len(ids))).lastrowid
    con.commit()
    added = 0
    for i in range(0, len(new), 200):
        for rec in efetch(cfg, new[i:i+200]):
            rec["first_seen_run"] = run_id; rec["supplement_tags"] = ""
            cols = ",".join(rec); qs = ",".join("?" * len(rec))
            cur.execute(f"INSERT OR IGNORE INTO articles({cols}) VALUES ({qs})", list(rec.values()))
            added += cur.rowcount
        con.commit()   # progress is kept batch by batch; a rerun skips stored PMIDs
        print(f"[{key}] fetched {min(i+200, len(new))}/{len(new)}", file=sys.stderr, flush=True)
    stored = {r[0] for r in cur.execute("SELECT pmid FROM articles")}
    cur.executemany("INSERT OR IGNORE INTO article_supplements(pmid, supplement) VALUES (?,?)", [(p, key) for p in ids if p in stored])
    cur.execute("""UPDATE articles SET supplement_tags = (SELECT group_concat(supplement, ',') FROM article_supplements s WHERE s.pmid = articles.pmid)
                   WHERE pmid IN (SELECT pmid FROM article_supplements WHERE supplement=?)""", (key,))
    # a capped (partial) ingest is left unfinished so the next run searches the same date window again
    cur.execute("UPDATE runs SET finished_at=?, new_records=?, notes=? WHERE id=?",
                (None if partial else dt.datetime.now().isoformat(timespec="seconds"), added,
                 f"partial: capped at {max_new} new records" if partial else None, run_id))
    con.commit()
    print(f"[{key}] hits={len(ids)} new={added} mindate={mindate}" + (" (partial, continues next run)" if partial else ""), flush=True)
    return added

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--full", action="store_true", help="ignore last-run date and search everything (still skips PMIDs already stored)")
    ap.add_argument("--only", nargs="*", help="limit to these supplement keys")
    ap.add_argument("--max-new", type=int, default=None, help="cap new records fetched per supplement (test runs)")
    args = ap.parse_args()
    con = connect(); cfg = load_config(con)
    failed = []
    for s in cfg["supplements"]:
        if args.only and s["key"] not in args.only: continue
        try:
            ingest_one(cfg, con, s, args.full, args.max_new)
        except Exception as e:
            failed.append(s["key"]); con.rollback()
            print(f"[{s['key']}] INGEST FAILED: {e}", flush=True); traceback.print_exc()
    if failed:
        print("ingest failed for: " + ", ".join(failed), flush=True); sys.exit(2)

if __name__ == "__main__":
    main()
