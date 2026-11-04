# Supplement Research Database

An automatically updated evidence map for dietary supplements. It harvests PubMed records and Europe PMC
open-access full texts, extracts the effect sizes the papers print, pools them with random-effects
meta-analysis, and publishes the result as a dashboard.

**Live dashboard:** https://bobzombers-hash.github.io/supplement-research/

> **Not medical advice.** This is an automated, regex-based summary of published abstracts. It is not a
> systematic review and can be wrong. Talk to a clinician before starting, stopping or changing any
> supplement.

## What it covers

* **Core supplements**, always tracked with hand-tuned outcome rules: creatine, magnesium, vitamin D and
  omega-3.
* **Curated catalog** of 93 more supplements in `config/catalog.json`. Each weekly run activates the next
  5, so coverage grows over time.
* **Per outcome** (for example vitamin D → fractures, omega-3 → triglycerides): the pooled estimate with
  a 95% CI, I² heterogeneity, Egger's small-study test (k ≥ 10), forest and funnel plots, a
  plain-language takeaway and links to the cited papers.

## How it updates

The GitHub Actions workflow [`.github/workflows/update.yml`](.github/workflows/update.yml) runs **every
Monday at 16:00 UTC** (08:00 PST / 09:00 PDT). It can also be started by hand from the Actions tab via
*Run workflow*. One run does the following:

1. Restores the SQLite database from the **`db-latest` release** (`research.db.gz`).
2. Runs one cycle, `python run.py --no-notify --expand 5 --time-budget-min 300`:
   1. **Coverage expansion:** activates the next 5 catalog supplements.
   2. **Incremental ingest:** fetches PubMed records added since the last run. New supplements get
      their full history.
   3. **Extraction:** pulls effect sizes from abstracts, plus a time-boxed chunk of the open-access
      full-text backlog.
   4. **Analysis:** DerSimonian-Laird and REML pooling.
   5. **Dashboard:** regenerates `output/index.html`.
3. Saves the updated database back to the release. Last week's copy is kept as `research.prev.db.gz`.
4. Deploys `output/` to GitHub Pages.

### Time budget

GitHub-hosted jobs are limited to 6 hours. With `--time-budget-min 300`, ingest and extraction are cut
short in time for analysis and the dashboard to finish, leaving about 25 minutes of reserve. Work is
committed to SQLite as it goes, so a cut-short run loses nothing and the next run continues the
backlog. A partial run still publishes a dashboard and saves the database, and the job summary shows a
warning.

### Optional secrets and variables

Set these under *Settings → Secrets and variables → Actions*:

| Name | Kind | Purpose |
|---|---|---|
| `NCBI_API_KEY` | secret | Raises the NCBI rate limit from 3 to 10 requests/s, which makes runs faster. Get a free key from your NCBI account. |
| `CONTACT_EMAIL` | variable | Sent to NCBI as a courtesy contact. |

## Why the database is a release asset, not a committed file

`research.db` is about 48 MB raw, about 11 MB gzipped, and grows every week. Committing it would add a
new full-size blob to git history on each run, and the repository would bloat without limit. Instead:

* The **code** lives in git.
* The **data** lives in a single rolling release, `db-latest`. It is overwritten each week with
  `--clobber` and holds `research.db.gz`, its `.sha256` and the previous week's copy.
* The **dashboard** is a Pages artifact, so it is not committed either.

Each asset stays far below GitHub's 2 GB release-asset limit and the 100 MB per-file git limit. The
workflow and the local installers use the same files.

## Run it locally

You don't need anything installed to read the results. To get your own self-updating copy:

* **Windows:** Code → Download ZIP, then Extract All, then double-click `install.cmd`. No admin rights
  are needed. It installs to `%LOCALAPPDATA%\SupplementResearch` and registers a weekly Task Scheduler
  job.
* **Mac:** Code → Download ZIP, then
  `cd ~/Downloads && unzip -q supplement-research-main.zip && bash supplement-research-main/install.sh`
  (uses launchd).
* **Linux:** `git clone`, then `bash install.sh` (uses a systemd user timer or cron).

Details, uninstall steps and settings are in [docs/INSTALL.md](docs/INSTALL.md).

### Developers

```bash
python -m venv .venv && . .venv/bin/activate && pip install -r requirements.txt
gh release download db-latest -p research.db.gz -D dist     # or download it from the Releases page
python tools/db_store.py unpack dist/research.db.gz data/research.db
python run.py --test --no-notify                            # small end-to-end smoke test
python run.py --status
```

## Limitations

* **Regex extraction is not a systematic review.** Only effect sizes printed with a 95% CI are
  captured. Trials that report only means and SDs are missed, and some sentences are misattributed.
* **Pooled rows overlap.** They combine published meta-analyses whose included trials overlap, so they
  summarise the literature; they are not independent evidence.
* **Rule-based outcomes for catalog supplements.** Supplements beyond the core four use generic
  outcome rules, so their results are noisier.
* **Only open-access full texts are read.** Everything else is read from the abstract alone.
* **Partial runs.** Runs depend on the NCBI and Europe PMC APIs being up. Large new topics can take
  several weekly runs to backfill.
* **Scheduled runs can stop.** GitHub may delay scheduled runs, and disables them in repositories with
  no activity for 60 days. Re-enable them from the Actions tab.

## Data and license

* **Data:** bibliographic records and abstracts come from **PubMed** (U.S. National Library of
  Medicine, via NCBI E-utilities). Open-access full texts come from **Europe PMC**. Abstracts remain
  under their publishers' and authors' copyrights. This project stores them only to extract the
  numeric results it displays, and links back to each source on PubMed. Check the publishers' terms
  before redistributing the database.
* **Code:** MIT license; see [LICENSE](LICENSE). The MIT license covers the code only, not the
  harvested abstracts.
