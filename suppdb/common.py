"""Shared helpers: paths, settings, config (core + activated catalog), DB schema, polite HTTP."""
import json, logging, os, random, sqlite3, time, urllib.error, urllib.request, urllib.parse
from pathlib import Path

# All paths are built with pathlib (Windows / macOS / Linux) and exposed as plain strings.
ROOT_P = Path(__file__).resolve().parent.parent
ROOT = str(ROOT_P)
HERE = ROOT
DB_PATH = str(Path(os.environ["SUPP_DB"]) if os.environ.get("SUPP_DB") else ROOT_P / "data" / "research.db")
CORE_PATH = str(ROOT_P / "config" / "core.json")
CATALOG_PATH = str(ROOT_P / "config" / "catalog.json")
SETTINGS_PATH = str(Path(os.environ["SUPP_SETTINGS"]) if os.environ.get("SUPP_SETTINGS") else ROOT_P / "settings.json")
OUTPUT_DIR = str(ROOT_P / "output")
FIG_DIR = str(ROOT_P / "output" / "figures")
log = logging.getLogger("suppdb")

DEFAULT_SETTINGS = {
    "ncbi_api_key": "",            # optional; raises the NCBI limit from 3 to 10 requests/s
    "contact_email": "",           # optional; NCBI asks heavy users to identify themselves
    "interval_days": 7,            # a full update cycle runs when the last successful one is this old
    "retry_hours": 6,              # after a failed cycle, wait this long before the next attempt
    "expansion_per_run": 5,        # catalog supplements added per cycle
    "fulltext_max_per_supplement_per_run": 120,
    "fulltext_time_budget_minutes": 90,  # cap on full-text fetching per cycle (the backlog continues next cycle)
    "notify": True,                # desktop notification (macOS / Windows / Linux notify-send) when a cycle finds new data
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS articles (
  pmid TEXT PRIMARY KEY,
  pmcid TEXT,
  doi TEXT,
  title TEXT,
  abstract TEXT,
  journal TEXT,
  year INTEGER,
  pub_date TEXT,           -- Entrez date (YYYY-MM-DD)
  pub_types TEXT,          -- JSON list of PubMed publication types
  mesh_terms TEXT,         -- JSON list of MeSH descriptor names
  supplement_tags TEXT,    -- comma-separated supplement keys (denormalized; see article_supplements)
  study_design TEXT,       -- meta-analysis | systematic review | rct | other trial | review | other
  is_human INTEGER,        -- 1 if MeSH Humans, 0 if animal-only, NULL unknown
  n_participants INTEGER,  -- largest participant count stated in abstract (heuristic)
  k_studies INTEGER,       -- for MA/SR: number of included studies stated in abstract (heuristic)
  url TEXT,
  first_seen_run INTEGER
);
CREATE TABLE IF NOT EXISTS article_supplements (
  pmid TEXT, supplement TEXT,
  extracted_version INTEGER DEFAULT 0,   -- extractor version applied for this supplement
  fulltext_checked INTEGER DEFAULT 0,
  PRIMARY KEY (pmid, supplement)
);
CREATE TABLE IF NOT EXISTS outcomes (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  pmid TEXT,
  supplement TEXT,
  outcome TEXT,            -- outcome key from config
  comparison TEXT,         -- e.g. 'supplement vs control (as reported)'
  effect_measure TEXT,     -- SMD | MD | RR | OR | HR   (WMD is stored as MD)
  value REAL,
  ci_low REAL,
  ci_high REAL,
  se REAL,                 -- derived from the 95% CI (log scale for ratios)
  unit TEXT,
  n_treat INTEGER,
  n_control INTEGER,
  n_total INTEGER,
  k_studies INTEGER,       -- if the effect is a pooled MA estimate
  level TEXT,              -- 'primary' (trial result) | 'pooled' (from a meta-analysis / SR)
  source TEXT,             -- 'abstract' | 'fulltext'
  confidence TEXT,         -- high | medium | low
  snippet TEXT,            -- exact text the number came from
  extractor_version INTEGER,
  UNIQUE(pmid, supplement, outcome, effect_measure, value, ci_low, ci_high, source)
);
CREATE TABLE IF NOT EXISTS runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  started_at TEXT,
  finished_at TEXT,
  step TEXT,
  supplement TEXT,
  query TEXT,
  mindate TEXT,
  hits INTEGER,
  new_records INTEGER,
  new_effects INTEGER,
  notes TEXT
);
CREATE TABLE IF NOT EXISTS fulltext_cache (
  pmcid TEXT PRIMARY KEY,
  text TEXT                -- Results-type sections from Europe PMC OA XML; NULL = not available
);
CREATE TABLE IF NOT EXISTS coverage (
  supplement TEXT PRIMARY KEY,   -- catalog key activated by automatic coverage expansion
  activated_at TEXT,
  run_id INTEGER
);
CREATE INDEX IF NOT EXISTS ix_out_sup ON outcomes(supplement, outcome);
CREATE INDEX IF NOT EXISTS ix_art_design ON articles(study_design);
"""

def load_settings():
    s = dict(DEFAULT_SETTINGS)
    if os.path.exists(SETTINGS_PATH):
        with open(SETTINGS_PATH, encoding="utf-8-sig") as f:
            s.update({k: v for k, v in json.load(f).items() if not k.startswith("_")})
    env = {"NCBI_API_KEY": "ncbi_api_key", "SUPP_CONTACT_EMAIL": "contact_email"}
    for e, k in env.items():
        if os.environ.get(e): s[k] = os.environ[e]
    return s

def load_catalog():
    if not os.path.exists(CATALOG_PATH): return []
    with open(CATALOG_PATH, encoding="utf-8") as f:
        return json.load(f)["supplements"]

def load_config(con=None):
    """Core supplements + catalog supplements activated so far (in catalog order)."""
    with open(CORE_PATH, encoding="utf-8") as f:
        cfg = json.load(f)
    st = load_settings()
    for k in ("ncbi_api_key", "contact_email", "fulltext_max_per_supplement_per_run"):
        if st.get(k) not in (None, ""): cfg[k] = st[k]
    own = con is None
    con = con or connect()
    active = {r[0] for r in con.execute("SELECT supplement FROM coverage")}
    if own: con.close()
    have = {s["key"] for s in cfg["supplements"]}
    cfg["supplements"] += [s for s in load_catalog() if s["key"] in active and s["key"] not in have]
    return cfg

def connect(path=None):
    con = sqlite3.connect(path or DB_PATH, timeout=60)
    con.executescript(SCHEMA)
    cols = {r[1] for r in con.execute("PRAGMA table_info(article_supplements)")}
    for c in ("extracted_version", "fulltext_checked"):
        if c not in cols:
            con.execute(f"ALTER TABLE article_supplements ADD COLUMN {c} INTEGER DEFAULT 0")
    con.commit()
    return con

_last = {}
def http_get(url, params=None, min_interval=None, tries=6, timeout=60):
    """GET with per-host request spacing and exponential backoff.

    NCBI: <=3 requests/s without an API key (10/s with one). Retries network errors, timeouts,
    HTTP 429 and 5xx (honouring Retry-After); other 4xx (e.g. 404 = no open-access full text)
    are raised immediately.
    """
    host = urllib.parse.urlparse(url).netloc
    if min_interval is None:
        min_interval = 0.12 if (params and params.get("api_key")) else 0.35
    if params:
        url = url + "?" + urllib.parse.urlencode(params)
    for attempt in range(tries):
        wait = _last.get(host, 0) + min_interval - time.time()
        if wait > 0:
            time.sleep(wait)
        _last[host] = time.time()
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "supplement-research-db/2.0 (standalone)"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except Exception as e:
            code = getattr(e, "code", None)
            if code is not None and 400 <= code < 500 and code != 429:
                raise
            if attempt == tries - 1:
                raise
            delay = min(120, 2 ** (attempt + 1)) + random.uniform(0, 1.5)
            ra = getattr(e, "headers", None) and e.headers.get("Retry-After")
            if ra and str(ra).isdigit():
                delay = max(delay, min(300, int(ra)))
            log.warning("HTTP %s for %s (attempt %d/%d), retrying in %.0fs", code or type(e).__name__, host, attempt + 1, tries, delay)
            time.sleep(delay)
