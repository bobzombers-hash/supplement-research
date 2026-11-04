"""Writes the launchd LaunchAgent plist (valid by construction via plistlib).
usage: render_launchd.py <install_dir> <python> <out.plist> [label]"""
import os, plistlib, sys
root, py, out = sys.argv[1:4]
label = sys.argv[4] if len(sys.argv) > 4 else "com.supplementresearch.update"
pl = {
    "Label": label,
    "ProgramArguments": [py, os.path.join(root, "run.py"), "--if-due"],
    "WorkingDirectory": root,
    # Checked daily at 10:00 and at login/boot; run.py starts a cycle only when the last successful
    # one is >= interval_days old. launchd fires a missed calendar slot when the Mac wakes from
    # sleep, and RunAtLoad covers the case where it was switched off.
    "StartCalendarInterval": [{"Hour": 10, "Minute": 0}],
    "RunAtLoad": True,
    "ProcessType": "Background",
    "LowPriorityIO": True,
    "Nice": 10,
    "StandardOutPath": os.path.join(root, "logs", "launchd.out.log"),
    "StandardErrorPath": os.path.join(root, "logs", "launchd.err.log"),
    "EnvironmentVariables": {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin:/usr/local/bin", "PYTHONUNBUFFERED": "1"},
}
with open(out, "wb") as f:
    plistlib.dump(pl, f)
print(out)
