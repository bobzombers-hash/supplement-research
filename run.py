#!/usr/bin/env python3
"""One update cycle (what the scheduler calls).

  python run.py --if-due     # scheduler mode: runs only if the last successful cycle is older than
                             # interval_days (catch-up after sleep / power-off), otherwise exits quietly
  python run.py              # run a cycle now
  python run.py --test       # small end-to-end cycle (1 new catalog supplement, 30 new records each, 2 full texts)

Steps: expand coverage (activate the next N catalog supplements) -> incremental PubMed ingest ->
effect extraction (abstracts + a chunk of the open-access full-text backlog) -> random-effects
meta-analysis + plots -> static dashboard (output/index.html). Progress is committed to SQLite as it
goes, so an interrupted cycle resumes where it stopped.
"""
import argparse, datetime as dt, json, logging, logging.handlers, os, platform, shutil, sqlite3, subprocess, sys, time
from pathlib import Path

ROOT_P = Path(__file__).resolve().parent
ROOT = str(ROOT_P)
sys.path.insert(0, str(ROOT_P / "suppdb"))
from common import connect, load_settings, load_catalog, load_config, DB_PATH  # noqa: E402

LOG_DIR = str(ROOT_P / "logs")
os.makedirs(LOG_DIR, exist_ok=True)
log = logging.getLogger("run")
IS_WIN = os.name == "nt"
# Windows: never flash a console window for child processes (the scheduler starts us with pythonw.exe)
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if IS_WIN else 0

def child_python():
    """Interpreter for the step subprocesses. Under pythonw.exe use the sibling python.exe (its
    stdout can be piped into our log); CREATE_NO_WINDOW keeps it invisible."""
    exe = Path(sys.executable)
    if IS_WIN and exe.name.lower() == "pythonw.exe" and (exe.parent / "python.exe").exists():
        return str(exe.parent / "python.exe")
    return str(exe)

def setup_logging(verbose=True):
    h = logging.handlers.RotatingFileHandler(os.path.join(LOG_DIR, "update.log"), maxBytes=2_000_000, backupCount=5, encoding="utf-8")
    h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    root = logging.getLogger(); root.setLevel(logging.INFO); root.addHandler(h)
    if verbose and sys.stdout is not None and hasattr(sys.stdout, "isatty") and sys.stdout.isatty():
        c = logging.StreamHandler(sys.stdout); c.setFormatter(logging.Formatter("%(asctime)s %(message)s")); root.addHandler(c)

class Lock:
    """Exclusive non-blocking lock (released automatically if the process dies)."""
    def __init__(self, path): self.path = path; self.f = None
    def acquire(self):
        # The OS lock lives on the open handle, so a crashed run can never leave a stale lock.
        self.f = open(self.path, "a+", encoding="utf-8")
        try:
            if IS_WIN:
                import msvcrt
                self.f.seek(0)
                msvcrt.locking(self.f.fileno(), msvcrt.LK_NBLCK, 1)   # byte 0, non-blocking
            else:
                import fcntl; fcntl.flock(self.f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.f.close(); self.f = None
            return False
        info = Path(self.path + ".info")   # pid/time for humans (kept out of the locked file on Windows)
        try: info.write_text(f"{os.getpid()} {dt.datetime.now().isoformat(timespec='seconds')}\n", encoding="utf-8")
        except OSError: pass
        return True

def step(name, args, env, deadline=None):
    """Run one pipeline step; if `deadline` (epoch seconds) passes, the step is stopped (its committed work is kept)."""
    log.info("step %s: %s", name, " ".join(args))
    t0 = time.time()
    if deadline is not None and t0 >= deadline:
        log.warning("step %s skipped: time budget exhausted", name); return 124
    p = subprocess.Popen([child_python(), "-u"] + [str(ROOT_P / a) if a.endswith(".py") else a for a in args], cwd=ROOT, env=env,
                         stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                         text=True, encoding="utf-8", errors="replace", creationflags=NO_WINDOW)
    timer = None
    if deadline is not None:
        import threading
        def _kill():
            log.warning("step %s hit the time budget; stopping it (committed work is kept)", name)
            try: p.kill()
            except OSError: pass
        timer = threading.Timer(max(1.0, deadline - t0), _kill); timer.daemon = True; timer.start()
    for line in p.stdout:
        line = line.rstrip()
        if line: log.info("  [%s] %s", name, line)
    rc = p.wait()
    if timer is not None:
        timer.cancel()
        if time.time() >= deadline and rc != 0: rc = 124
    log.info("step %s finished rc=%s in %.0fs", name, rc, time.time() - t0)
    return rc

def counts(con):
    return (con.execute("SELECT count(*) FROM articles").fetchone()[0], con.execute("SELECT count(*) FROM outcomes").fetchone()[0])

def notify(title, msg, settings):
    if not settings.get("notify", True): return
    try:
        if platform.system() == "Darwin":
            subprocess.run(["osascript", "-e", f'display notification {json.dumps(msg)} with title {json.dumps(title)}'], timeout=20, check=False)
        elif IS_WIN:
            # Built-in WinRT toast via Windows PowerShell 5.1 (no extra modules); silently skipped if unavailable.
            ps = shutil.which("powershell.exe") or shutil.which("powershell")
            if ps:
                t = title.replace("'", "''"); m = msg.replace("'", "''")
                script = ("[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] > $null;"
                          "$x = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent([Windows.UI.Notifications.ToastTemplateType]::ToastText02);"
                          f"$n = $x.GetElementsByTagName('text'); $n.Item(0).AppendChild($x.CreateTextNode('{t}')) > $null; $n.Item(1).AppendChild($x.CreateTextNode('{m}')) > $null;"
                          "$id = '{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\\WindowsPowerShell\\v1.0\\powershell.exe';"
                          "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($id).Show([Windows.UI.Notifications.ToastNotification]::new($x))")
                subprocess.run([ps, "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden", "-Command", script],
                               timeout=30, check=False, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               creationflags=NO_WINDOW)
        elif shutil.which("notify-send") and os.environ.get("DISPLAY"):
            subprocess.run(["notify-send", title, msg], timeout=20, check=False)
    except Exception as e:  # notifications are best-effort
        log.info("notification skipped: %s", e)

def backup_db():
    """Keep one consistent snapshot of the DB from before this cycle (data/research.prev.db)."""
    if not os.path.exists(DB_PATH): return
    db = Path(DB_PATH)
    tmp = db.with_name(db.stem + ".prev.db.tmp"); prev = db.with_name(db.stem + ".prev.db")
    s = sqlite3.connect(str(db)); d = sqlite3.connect(str(tmp)); s.backup(d); d.close(); s.close()
    os.replace(str(tmp), str(prev))

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--if-due", action="store_true", help="scheduler mode: only run when a cycle is due")
    ap.add_argument("--test", action="store_true", help="small end-to-end cycle")
    ap.add_argument("--expand", type=int, default=None, help="catalog supplements to add this cycle (default: settings)")
    ap.add_argument("--max-new", type=int, default=None, help="cap new PubMed records per supplement")
    ap.add_argument("--fulltext-limit", type=int, default=None, help="full-text papers per supplement")
    ap.add_argument("--only", nargs="*", help="limit ingest/extract to these supplement keys")
    ap.add_argument("--no-notify", action="store_true")
    ap.add_argument("--status", action="store_true", help="print status and exit")
    ap.add_argument("--time-budget-min", type=float, default=None,
                    help="wall-clock budget for the whole cycle (e.g. 300 on GitHub Actions); ingest/extract are cut short "
                         "so analysis + dashboard always finish inside it")
    args = ap.parse_args()
    settings = load_settings()
    if args.test:
        args.expand = 1 if args.expand is None else args.expand
        args.max_new = 30 if args.max_new is None else args.max_new
        args.fulltext_limit = 2 if args.fulltext_limit is None else args.fulltext_limit

    lock = None
    if not args.status:
        # Take the lock before opening the log, so only one process ever writes (and rotates) update.log;
        # on Windows a log file held open by a second process cannot be renamed during rotation.
        lock = Lock(os.path.join(LOG_DIR, "run.lock"))
        if not lock.acquire():
            if sys.stdout is not None:
                print("another update is running; exiting")
            return 0
        setup_logging()

    con = connect()
    # --test cycles are small smoke tests; they do not count towards the weekly schedule
    real = "step='cycle' AND coalesce(notes,'') <> 'test' AND coalesce(notes,'') NOT LIKE '%\"test\": true%'"
    last_ok = con.execute(f"SELECT max(finished_at) FROM runs WHERE {real} AND finished_at IS NOT NULL").fetchone()[0]
    last_try = con.execute(f"SELECT max(started_at) FROM runs WHERE {real}").fetchone()[0]
    if args.status:
        act = con.execute("SELECT count(*) FROM coverage").fetchone()[0]
        print(json.dumps(dict(db=DB_PATH, last_successful_cycle=last_ok, last_attempt=last_try, articles=counts(con)[0], effects=counts(con)[1],
                              catalog_active=act, catalog_total=len(load_catalog())), indent=1)); return 0
    now = dt.datetime.now()
    if args.if_due:
        if last_ok and now - dt.datetime.fromisoformat(last_ok) < dt.timedelta(days=float(settings["interval_days"])):
            return 0   # not due; stay quiet (this check runs often)
        if last_try and (not last_ok or last_try > last_ok) and now - dt.datetime.fromisoformat(last_try) < dt.timedelta(hours=float(settings["retry_hours"])):
            return 0   # a recent attempt failed; back off before retrying
        log.info("cycle due (last successful: %s)", last_ok or "never")

    a0, e0 = counts(con)
    backup_db()
    run_id = con.execute("INSERT INTO runs(started_at, step, notes) VALUES (?,?,?)",
                         (now.isoformat(timespec="seconds"), "cycle", "test" if args.test else None)).lastrowid
    con.commit()

    # 1) coverage expansion
    n_exp = settings["expansion_per_run"] if args.expand is None else args.expand
    active = {r[0] for r in con.execute("SELECT supplement FROM coverage")}
    core = {s["key"] for s in load_config(con)["supplements"]}
    queue = [c for c in load_catalog() if c["key"] not in active and c["key"] not in core]
    added = queue[: max(0, int(n_exp))]
    for c in added:
        con.execute("INSERT OR IGNORE INTO coverage VALUES (?,?,?)", (c["key"], now.isoformat(timespec="seconds"), run_id))
    con.commit()
    log.info("coverage expansion: +%d (%s); %d left in queue", len(added), ", ".join(c["label"] for c in added) or "none", len(queue) - len(added))
    con.close()

    t_start = time.time()
    budget = args.time_budget_min if args.time_budget_min is not None else settings.get("cycle_time_budget_minutes")
    reserve = 25 * 60   # kept for analyze + dashboard
    hard_end = t_start + float(budget) * 60 if budget else None
    work_end = max(t_start + 60, hard_end - reserve) if hard_end else None
    if hard_end: log.info("time budget: %.0f min (ingest/extract must finish within %.0f min)", float(budget), (work_end - t_start) / 60)
    env = dict(os.environ, SUPP_DB=DB_PATH, PYTHONIOENCODING="utf-8", MPLBACKEND="Agg")
    env.setdefault("MPLCONFIGDIR", str(ROOT_P / ".mplconfig"))
    only = (["--only"] + args.only) if args.only else []
    rc = {}
    rc["ingest"] = step("ingest", ["suppdb/ingest.py"] + only + (["--max-new", str(args.max_new)] if args.max_new is not None else []), env,
                       deadline=(t_start + 0.6 * (work_end - t_start)) if work_end else None)
    ft_budget = float(settings.get("fulltext_time_budget_minutes", 90))
    if work_end: ft_budget = max(1.0, min(ft_budget, (work_end - time.time()) / 60 - 10))   # leave room for abstract extraction
    ex = ["suppdb/extract.py"] + only + ["--fulltext-budget-min", f"{ft_budget:.1f}"]
    if args.fulltext_limit is not None: ex += ["--fulltext-limit", str(args.fulltext_limit)]
    rc["extract"] = step("extract", ex, env, deadline=work_end)
    rc["analyze"] = step("analyze", ["suppdb/analyze.py"], env, deadline=hard_end)
    rc["dashboard"] = step("dashboard", ["suppdb/dashboard.py"], env, deadline=hard_end) if rc["analyze"] == 0 else 1

    con = connect()
    a1, e1 = counts(con)
    ok = all(v == 0 for v in rc.values())
    # partial ingest/extract failures (e.g. one supplement's NCBI query failed) still produce a dashboard,
    # but the cycle is not marked successful, so the scheduler retries after retry_hours.
    con.execute("UPDATE runs SET finished_at=?, new_records=?, new_effects=?, notes=? WHERE id=?",
                (dt.datetime.now().isoformat(timespec="seconds") if ok else None, a1 - a0, e1 - e0,
                 json.dumps(dict(rc=rc, expanded=[c["key"] for c in added], test=args.test)), run_id))
    con.commit(); con.close()
    msg = f"+{a1 - a0} articles, +{e1 - e0} effect sizes" + (f", +{len(added)} supplements" if added else "")
    log.info("cycle %s: %s", "OK" if ok else "FAILED " + json.dumps(rc), msg)
    if not args.no_notify and (ok and (a1 > a0 or e1 > e0 or added)):
        notify("Supplement research updated", msg + ". Open output/index.html", settings)
    elif not args.no_notify and not ok:
        notify("Supplement research update failed", "See logs/update.log", settings)
    return 0 if ok else 1

if __name__ == "__main__":
    sys.exit(main())
