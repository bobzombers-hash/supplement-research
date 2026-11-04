#!/usr/bin/env bash
# Supplement Research DB: standalone installer (macOS + Linux).
#   bash install.sh                 install to ~/SupplementResearch and schedule weekly updates
#   bash install.sh --test          also run one small end-to-end update cycle right away
# Options: --prefix DIR   --scheduler auto|launchd|systemd|cron|none   --python PATH   --test
set -euo pipefail
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PREFIX="${SUPP_HOME:-$HOME/SupplementResearch}"
SCHED=auto; TEST=0; PYBIN=""
LABEL="com.supplementresearch.update"; MARK="# supplement-research-db"
while [ $# -gt 0 ]; do
  case "$1" in
    --prefix) PREFIX="$2"; shift 2;;
    --scheduler) SCHED="$2"; shift 2;;
    --no-schedule) SCHED=none; shift;;
    --python) PYBIN="$2"; shift 2;;
    --test) TEST=1; shift;;
    -h|--help) sed -n '2,6p' "$0"; exit 0;;
    *) echo "unknown option: $1" >&2; exit 2;;
  esac
done
say(){ printf '\033[1m[install]\033[0m %s\n' "$*"; }
die(){ printf '[install] ERROR: %s\n' "$*" >&2; exit 1; }
OS="$(uname -s)"
mkdir -p "$PREFIX"; PREFIX="$(cd "$PREFIX" && pwd)"
case "$PREFIX" in *" "*) die "install path must not contain spaces: $PREFIX";; esac
if [ "$OS" = "Darwin" ]; then
  case "$PREFIX" in "$HOME/Desktop"*|"$HOME/Documents"*|"$HOME/Downloads"*|*/Library/Mobile\ Documents*)
    die "macOS blocks background jobs from reading Desktop/Documents/Downloads/iCloud. Use the default (~/SupplementResearch) or another --prefix.";; esac
fi

# 1) files ---------------------------------------------------------------------------------------
say "installing to $PREFIX"
mkdir -p "$PREFIX"/{data,output,logs,config}
if [ "$SRC" != "$PREFIX" ]; then
  for f in suppdb tools run.py install.sh uninstall.sh install.ps1 install.cmd uninstall.ps1 uninstall.cmd README.md requirements.txt requirements-py39.txt settings.example.json; do
    rm -rf "${PREFIX:?}/$f"; cp -R "$SRC/$f" "$PREFIX/$f"
  done
  cp "$SRC"/config/*.json "$PREFIX/config/"
  if [ -f "$PREFIX/data/research.db" ]; then
    say "keeping existing database $PREFIX/data/research.db"
  elif [ -f "$SRC/data/research.db" ]; then
    cp "$SRC/data/research.db" "$PREFIX/data/research.db"; say "copied starting database ($(du -h "$PREFIX/data/research.db" | cut -f1))"
  elif command -v curl >/dev/null 2>&1 && curl -fsSL --retry 3 -o "$PREFIX/data/research.db.gz.part" "${SUPP_DB_URL:-https://github.com/bobzombers-hash/supplement-research/releases/download/db-latest/research.db.gz}" \
       && gunzip -c "$PREFIX/data/research.db.gz.part" > "$PREFIX/data/research.db.part"; then
    mv "$PREFIX/data/research.db.part" "$PREFIX/data/research.db"; rm -f "$PREFIX/data/research.db.gz.part"
    say "downloaded the latest database from GitHub ($(du -h "$PREFIX/data/research.db" | cut -f1))"
  else
    rm -f "$PREFIX/data/research.db.gz.part" "$PREFIX/data/research.db.part"
    say "no starting database found or downloadable; the first run builds one from scratch (slow)"
  fi
  if [ ! -f "$PREFIX/output/index.html" ] && [ -d "$SRC/output" ]; then cp -R "$SRC/output/." "$PREFIX/output/"; fi
fi
[ -f "$PREFIX/settings.json" ] || cp "$PREFIX/settings.example.json" "$PREFIX/settings.json"
find "$PREFIX" -name '__pycache__' -prune -exec rm -rf {} + 2>/dev/null || true
[ "$OS" = "Darwin" ] && xattr -dr com.apple.quarantine "$PREFIX" 2>/dev/null || true

# 2) python + venv -------------------------------------------------------------------------------
pyok(){ "$1" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' >/dev/null 2>&1; }
if [ -z "$PYBIN" ]; then
  for c in /opt/homebrew/bin/python3 /usr/local/bin/python3 python3.13 python3.12 python3.11 python3.10 python3 /usr/bin/python3; do
    p="$(command -v "$c" 2>/dev/null || true)"; [ -n "$p" ] || continue
    if [ "$OS" = "Darwin" ] && [ "$p" = "/usr/bin/python3" ] && ! xcode-select -p >/dev/null 2>&1; then continue; fi
    if pyok "$p"; then PYBIN="$p"; break; fi
  done
fi
[ -n "$PYBIN" ] || die "Python 3.9+ not found. On a Mac run: xcode-select --install   (or: brew install python), then re-run."
PYV="$("$PYBIN" -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
say "using $PYBIN (Python $PYV)"
VENV="$PREFIX/.venv"
if [ -x "$VENV/bin/python" ] && [ "$("$VENV/bin/python" -c 'import sys; print("%d.%d" % sys.version_info[:2])')" != "$PYV" ]; then rm -rf "$VENV"; fi
[ -x "$VENV/bin/python" ] || "$PYBIN" -m venv "$VENV"
VPY="$VENV/bin/python"
"$VPY" -m pip install -q --disable-pip-version-check --default-timeout 60 --upgrade pip >/dev/null 2>&1 || true
if "$VPY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 12) else 1)'; then REQ="$PREFIX/requirements.txt"; else REQ="$PREFIX/requirements-py39.txt"; fi
say "installing pinned dependencies from $(basename "$REQ")"
if ! "$VPY" -m pip install -q --disable-pip-version-check --default-timeout 60 -r "$REQ"; then
  say "pinned install failed; falling back to unpinned numpy/scipy/matplotlib"
  "$VPY" -m pip install -q --disable-pip-version-check --default-timeout 60 numpy scipy matplotlib || die "dependency install failed (see output above)"
fi
"$VPY" -c 'import numpy, scipy, matplotlib' || die "dependencies do not import"
"$VPY" "$PREFIX/run.py" --status >/dev/null || die "self-check failed"

# 3) schedule ------------------------------------------------------------------------------------
if [ "$SCHED" = auto ]; then
  if [ "$OS" = "Darwin" ]; then SCHED=launchd
  elif command -v systemctl >/dev/null 2>&1 && systemctl --user show-environment >/dev/null 2>&1; then SCHED=systemd
  elif command -v crontab >/dev/null 2>&1; then SCHED=cron
  else SCHED=none; say "WARNING: no launchd, systemd user session or crontab found; no schedule registered"; fi
fi
CMD="$VPY $PREFIX/run.py --if-due"
case "$SCHED" in
  launchd)
    PL="$HOME/Library/LaunchAgents/$LABEL.plist"; mkdir -p "$HOME/Library/LaunchAgents"
    "$VPY" "$PREFIX/tools/render_launchd.py" "$PREFIX" "$VPY" "$PL" "$LABEL" >/dev/null
    command -v plutil >/dev/null 2>&1 && plutil -lint "$PL"
    launchctl bootout "gui/$(id -u)/$LABEL" >/dev/null 2>&1 || true
    launchctl bootstrap "gui/$(id -u)" "$PL" 2>/dev/null || launchctl load -w "$PL"
    launchctl print "gui/$(id -u)/$LABEL" >/dev/null 2>&1 && say "launchd agent $LABEL loaded ($PL)"
    SCHED_DESC="launchd LaunchAgent $LABEL: checks daily at 10:00 and at login; runs a cycle when the last successful one is 7+ days old (missed slots fire on wake)";;
  systemd)
    UD="$HOME/.config/systemd/user"; mkdir -p "$UD"
    cat > "$UD/supplement-research.service" <<UNIT
[Unit]
Description=Supplement research DB update (runs only when due)
Wants=network-online.target
After=network-online.target
[Service]
Type=oneshot
WorkingDirectory=$PREFIX
ExecStart=$CMD
Nice=10
UNIT
    cat > "$UD/supplement-research.timer" <<UNIT
[Unit]
Description=Daily due-check for the supplement research DB (weekly cycle)
[Timer]
OnCalendar=*-*-* 10:00:00
OnBootSec=15min
Persistent=true
RandomizedDelaySec=600
[Install]
WantedBy=timers.target
UNIT
    systemctl --user daemon-reload && systemctl --user enable --now supplement-research.timer
    systemctl --user list-timers supplement-research.timer --no-pager || true
    SCHED_DESC="systemd user timer supplement-research.timer: daily 10:00 + 15 min after boot, Persistent=true; cycle when 7+ days since last success"
    say "tip: run 'loginctl enable-linger $USER' so the timer also runs while you are logged out";;
  cron)
    TMP="$(mktemp)"; (crontab -l 2>/dev/null || true) | grep -v "$MARK" > "$TMP" || true
    echo "7 * * * * cd $PREFIX && $CMD >/dev/null 2>&1 $MARK" >> "$TMP"
    echo "@reboot sleep 600 && cd $PREFIX && $CMD >/dev/null 2>&1 $MARK" >> "$TMP"
    crontab "$TMP"; rm -f "$TMP"
    say "crontab entries:"; crontab -l | grep "$MARK"
    SCHED_DESC="crontab: hourly due-check at :07 + @reboot (anacron-style: cycle when 7+ days since last success)";;
  none) SCHED_DESC="none";;
  *) die "unknown scheduler $SCHED";;
esac
echo "$SCHED" > "$PREFIX/.scheduler"

# 4) optional test cycle -------------------------------------------------------------------------
if [ "$TEST" = 1 ]; then
  say "running a small test cycle (1 new supplement, 30 new records each, 2 full texts)..."
  "$VPY" "$PREFIX/run.py" --test --no-notify && say "test cycle OK" || say "test cycle reported a problem; see $PREFIX/logs/update.log"
  [ -f "$PREFIX/output/index.html" ] && say "dashboard: $PREFIX/output/index.html"
fi
say "done."
say "schedule: $SCHED_DESC"
say "dashboard: $PREFIX/output/index.html    logs: $PREFIX/logs/update.log    settings: $PREFIX/settings.json"
say "run now: $VPY $PREFIX/run.py      remove schedule: bash $PREFIX/uninstall.sh"
