#!/usr/bin/env bash
# Removes the scheduled job(s). Data, dashboard and logs are kept unless --purge is given.
#   bash uninstall.sh [--purge]
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LABEL="com.supplementresearch.update"; MARK="# supplement-research-db"
say(){ printf '[uninstall] %s\n' "$*"; }
if [ "$(uname -s)" = "Darwin" ]; then
  PL="$HOME/Library/LaunchAgents/$LABEL.plist"
  launchctl bootout "gui/$(id -u)/$LABEL" >/dev/null 2>&1 || launchctl unload -w "$PL" >/dev/null 2>&1 || true
  [ -f "$PL" ] && rm -f "$PL" && say "removed launchd agent $LABEL"
fi
if command -v systemctl >/dev/null 2>&1 && systemctl --user show-environment >/dev/null 2>&1; then
  if [ -f "$HOME/.config/systemd/user/supplement-research.timer" ]; then
    systemctl --user disable --now supplement-research.timer >/dev/null 2>&1 || true
    rm -f "$HOME/.config/systemd/user/supplement-research."{timer,service}; systemctl --user daemon-reload
    say "removed systemd user timer"
  fi
fi
if command -v crontab >/dev/null 2>&1 && crontab -l 2>/dev/null | grep -q "$MARK"; then
  crontab -l 2>/dev/null | grep -v "$MARK" | crontab - && say "removed crontab entries"
fi
rm -f "$HERE/.scheduler"
if [ "${1:-}" = "--purge" ]; then
  if [ -f "$HERE/run.py" ] && [ -d "$HERE/suppdb" ]; then say "deleting $HERE"; rm -rf "$HERE"; fi
else
  say "schedule removed; data kept in $HERE (use --purge to delete it)"
fi
