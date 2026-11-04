# Installing on your own computer (Windows, Mac, Linux)

> Get the files with **Code → Download ZIP** on the GitHub page (it saves `supplement-research-main.zip`),
> or `git clone https://github.com/bobzombers-hash/supplement-research.git` (then use the cloned folder
> wherever these steps say `supplement-research-main`). The installer downloads the latest database
> from the repo's `db-latest` release, so you start from the same data as the live dashboard.
> You do not need to install anything locally just to *view* the results: see the live dashboard.

A self-contained copy of the supplement evidence database (PubMed + Europe PMC → SQLite → effect-size
extraction → random-effects meta-analysis → dashboard). Once installed it keeps itself up to date on
your computer, with no bot or cloud service involved.

## Install on Windows (no admin rights needed)

**Easiest:** right-click `supplement-research-main.zip`, choose **Extract All… → Extract**, open
the extracted folder, and **double-click `install.cmd`**. Windows usually puts the files one level
down, in another `supplement-research-main` folder; open that one. If
SmartScreen warns about an unrecognised app, click **More info → Run anyway**.

**Or one line in PowerShell**, with the zip in Downloads:

```powershell
cd $HOME\Downloads; Expand-Archive .\supplement-research-main.zip -DestinationPath . -Force; .\supplement-research-main\install.cmd
```

What it does:

* **Installs to `%LOCALAPPDATA%\SupplementResearch`**, i.e. `C:\Users\<you>\AppData\Local\SupplementResearch`.
  It refuses OneDrive-synced folders, because syncing a live database can corrupt it.
* **Finds Python 3.9+.** It tries the `py` launcher first, then `python`, then the usual install folders.
  * It skips the Microsoft Store `python.exe` alias, which is not a real install.
  * If no Python is found, it offers `winget install Python.Python.3.12` (per user, no admin). It
    prints manual instructions if winget isn't available.
  * Run `install.cmd -InstallPython` to skip the question.
* **Sets up a private environment** in `.venv` with pinned numpy, scipy and matplotlib.
* **Downloads the latest database** (about 11 MB compressed) from the `db-latest` release. If a database is already installed, it is kept, so re-running
  `install.cmd` is also how you upgrade.
* **Registers a Task Scheduler task**, `SupplementResearch Update`, for your user. It needs no admin
  rights and no stored password. The task:
  * runs `pythonw.exe run.py --if-due`, so no console window appears
  * runs daily at 10:00 and 2 minutes after you log on
  * catches up if 10:00 was missed while the PC was off or asleep ("Run task as soon as possible
    after a scheduled start is missed")
  * starts only when a network connection is available, and runs on battery too
* **Starts the first update cycle in the background right away.** It takes 30–90 minutes. A Windows
  notification appears when it finishes.

To view the dashboard, open `%LOCALAPPDATA%\SupplementResearch\output\index.html` (paste that path
into the Explorer address bar).

To remove the schedule, double-click `uninstall.cmd` in that folder, or run `uninstall.cmd -Purge`
to also delete the program and its data.

## Install on a Mac (one line)

Download `supplement-research-main.zip` to your Downloads folder, open **Terminal**, and paste:

```bash
cd ~/Downloads && { [ -d supplement-research-main ] || unzip -q supplement-research-main.zip; } && bash supplement-research-main/install.sh
```

The installer:

* copies the program and the starting database (~50 MB) to `~/SupplementResearch`. That folder sits
  outside Documents and Desktop on purpose, because macOS blocks background jobs from reading those.
* creates a private Python environment in `~/SupplementResearch/.venv` with pinned numpy, scipy and
  matplotlib. Nothing is installed system-wide.
* registers a LaunchAgent, `~/Library/LaunchAgents/com.supplementresearch.update.plist`. It needs no
  admin rights.

**Requirements:** Python 3.9 or newer.

* If you have Homebrew Python it is used.
* Otherwise Apple's `/usr/bin/python3` is used. If the Command Line Tools are missing, run
  `xcode-select --install` once and re-run the installer.

The first full update starts within a minute of installing. It downloads any new records since the
snapshot, adds 5 new supplements and analyses them, so allow it 30 to 90 minutes. You can keep
working; you get a notification when it finishes.

## Install on Linux

```bash
unzip -q supplement-research-main.zip && bash supplement-research-main/install.sh
```

It uses a systemd user timer if one is available, otherwise crontab. Add `--test` to also run a small
check cycle immediately.

## The schedule

| OS | Mechanism | When it checks |
|---|---|---|
| Windows | Task Scheduler task `SupplementResearch Update` (current user) | daily at 10:00 with "run as soon as possible if missed", and 2 min after logon, only with network |
| macOS | launchd LaunchAgent | daily at 10:00 and at every login or boot |
| Linux (systemd) | `supplement-research.timer` (user) | daily at 10:00 and 15 min after boot, with `Persistent=true` |
| Linux (no systemd) | crontab | hourly at :07 and at `@reboot` |

Each check runs `run.py --if-due`. That starts an update cycle only when the last successful cycle is
**7 or more days old** (`interval_days`); otherwise it exits instantly.

Missed runs catch up automatically:

* If the computer was asleep, launchd and systemd fire the missed check on wake.
* If it was switched off, the login, boot or hourly check fires.
* A failed cycle (for example, no internet) is retried after 6 hours (`retry_hours`).

## What one cycle does

1. **Coverage expansion.** Activates the next 5 supplements (`expansion_per_run`) from the curated
   catalog in `config/catalog.json`. The catalog has 93 supplements, each with a PubMed query and 2–4
   outcomes. The 4 core supplements (creatine, magnesium, vitamin D, omega-3) are always active.
2. **Incremental PubMed ingest.** Fetches records added since the last run, with a 7-day overlap. A
   newly activated supplement gets its full history.
3. **Extraction.** Regex extraction of printed effect sizes (SMD, MD, RR, OR or HR with a 95% CI) from
   abstracts, plus a chunk of the open-access full-text backlog from Europe PMC. The chunk is limited to
   120 papers per supplement and 90 minutes per cycle.
4. **Analysis.** DerSimonian-Laird and REML random-effects pooling, I², Egger's test (k ≥ 10), and
   forest and funnel plots.
5. **Dashboard.** Regenerates `output/index.html`, a single offline HTML file. It shows database
   statistics, a results table with plain-language takeaways, a forest-plot picker, the cited
   meta-analyses with PubMed links, coverage, limitations and the last-updated time.

Nothing is invented: every number comes from a sentence printed in an abstract or a full text. Rows that
cannot be parsed are skipped.

Progress is committed to SQLite as each step runs. If a cycle is interrupted (sleep, shutdown, network
loss), the next one continues where it stopped.

## Everyday use

Windows (PowerShell):

```powershell
$S = "$env:LOCALAPPDATA\SupplementResearch"
start "$S\output\index.html"                      # view the dashboard
& "$S\.venv\Scripts\python.exe" "$S\run.py"        # run a cycle right now
& "$S\.venv\Scripts\python.exe" "$S\run.py" --status
Get-Content "$S\logs\update.log" -Wait -Tail 30      # watch progress
Start-ScheduledTask "SupplementResearch Update"      # trigger the scheduled check by hand
& "$S\uninstall.cmd"                                # remove the schedule (add -Purge to delete everything)
```

macOS / Linux:

```bash
open ~/SupplementResearch/output/index.html                       # view the dashboard (Mac)
~/SupplementResearch/.venv/bin/python ~/SupplementResearch/run.py  # run a cycle right now
~/SupplementResearch/.venv/bin/python ~/SupplementResearch/run.py --status
tail -f ~/SupplementResearch/logs/update.log                        # watch progress
bash ~/SupplementResearch/uninstall.sh                              # remove the schedule (keeps data)
bash ~/SupplementResearch/uninstall.sh --purge                      # remove the schedule and delete everything
```

Re-running `install.sh` upgrades the program. It keeps your database and `settings.json`.

## Settings (`settings.json` in the install folder)

| key | default | meaning |
|---|---|---|
| `ncbi_api_key` | "" | optional free NCBI key ([get one](https://account.ncbi.nlm.nih.gov/settings/)); raises the rate limit from 3 to 10 requests per second. Also read from the `NCBI_API_KEY` env var. |
| `contact_email` | "" | sent to NCBI and Europe PMC as a courtesy (also `SUPP_CONTACT_EMAIL`) |
| `interval_days` | 7 | days between update cycles |
| `retry_hours` | 6 | wait before retrying after a failed cycle |
| `expansion_per_run` | 5 | catalog supplements added per cycle (0 = stop expanding) |
| `fulltext_max_per_supplement_per_run` | 120 | open-access full texts read per supplement per cycle |
| `fulltext_time_budget_minutes` | 90 | time cap for the full-text step |
| `notify` | true | desktop notification after each cycle (Windows toast / macOS / Linux notify-send) |

**Adding your own supplement:** add an entry to `config/catalog.json`, copying the shape of an existing
one. It is picked up in queue order.

## Files

* `install.cmd` / `install.ps1`, `uninstall.cmd` / `uninstall.ps1` — the Windows installer and uninstaller.
* `install.sh` / `uninstall.sh` — the macOS and Linux installer and uninstaller.
* `tools/render_task_xml.py` (Windows task) and `tools/render_launchd.py` (macOS agent) — write the schedule definitions.
* `run.py` — the orchestrator: lock, due-check, steps, logging and notification.
* `suppdb/`
  * `common.py` — settings, schema, and HTTP with retries and backoff.
  * `ingest.py`, `extract.py`, `analyze.py`, `dashboard.py` — the four steps.
* `config/core.json` — the 4 core supplements, outcomes and global rules.
* `config/catalog.json` — the expansion catalog.
* `data/research.db` — the SQLite database.
  * `data/research.prev.db` — a snapshot from before the last cycle.
* `output/` — `index.html`, `results.json` and `figures/`.
* `logs/` — `update.log` (rotated at 2 MB, 5 kept) and the launchd stdout and stderr.

## Limitations

* **Regex extraction is not a systematic review.** It reads only effect sizes printed with a 95% CI.
  Many trials report means and SDs instead, and those are not captured. Some sentences are
  misattributed or skipped.
* **Pooled estimates overlap.** "Pooled" rows combine published meta-analyses whose trials overlap, so
  they are a summary of the literature, not independent evidence. "Primary" rows come from individual
  RCTs.
* **Plain-language takeaways are rule-based.** They describe the numbers (direction, size, confidence
  interval, heterogeneity, small-study bias). They are not medical advice.
* **Only open-access full texts are read.** Papers without an open-access full text on Europe PMC are read from the abstract only.
* **New catalog supplements use generic outcome rules.** Their results are noisier than the
  hand-tuned core four until someone reviews them.
* **The computer has to be on sometimes.** It must be on and online at some point for updates to
  happen. A cycle needs the PubMed and Europe PMC APIs to be reachable.
* **The first cycle for each new supplement downloads its whole history.** That can take a while for
  big topics such as probiotics or iron.
* **Windows sleep.** On Windows the task doesn't wake the PC. A 10:00 check missed during sleep or
  shutdown runs soon after the PC is back on (and at the next logon). The task runs only while you
  are logged in, which avoids storing a password.
