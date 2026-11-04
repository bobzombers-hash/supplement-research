"""Writes the Windows Task Scheduler definition (Task Scheduler schema 1.3 = Windows 7+, UTF-16) used by install.ps1.
usage: render_task_xml.py <install_dir> <pythonw.exe> <DOMAIN\\user> <out.xml> [--no-logon]

* daily at 10:00 + 2 min after this user logs on (the logon trigger is dropped with --no-logon)
* StartWhenAvailable: a 10:00 slot missed while the PC was off/asleep runs as soon as possible
* RunOnlyIfNetworkAvailable, runs on battery, never more than one instance, 6 h time limit
* runs as the current user, only when logged on (InteractiveToken), no admin rights (LeastPrivilege)
* pythonw.exe -> no console window. run.py --if-due decides whether a full cycle is due (7 days).
"""
import datetime as dt, sys
from xml.sax.saxutils import escape as x

def render(root, pythonw, user, logon=True, start_date=None):
    root = root.rstrip("\\/")
    start = (start_date or dt.date.today()).isoformat() + "T10:00:00"
    logon_xml = f"""
    <LogonTrigger>
      <Enabled>true</Enabled>
      <UserId>{x(user)}</UserId>
      <Delay>PT2M</Delay>
    </LogonTrigger>""" if logon else ""
    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.3" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Author>{x(user)}</Author>
    <Description>Supplement Research DB: checks daily and at logon; runs a full update (PubMed ingest, extraction, meta-analysis, dashboard) when the last successful one is 7+ days old.</Description>
  </RegistrationInfo>
  <Triggers>
    <CalendarTrigger>
      <Enabled>true</Enabled>
      <StartBoundary>{start}</StartBoundary>
      <ScheduleByDay>
        <DaysInterval>1</DaysInterval>
      </ScheduleByDay>
    </CalendarTrigger>{logon_xml}
  </Triggers>
  <Principals>
    <Principal id="Author">
      <UserId>{x(user)}</UserId>
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>true</RunOnlyIfNetworkAvailable>
    <IdleSettings>
      <StopOnIdleEnd>false</StopOnIdleEnd>
      <RestartOnIdle>false</RestartOnIdle>
    </IdleSettings>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <Hidden>false</Hidden>
    <RunOnlyIfIdle>false</RunOnlyIfIdle>
    <WakeToRun>false</WakeToRun>
    <ExecutionTimeLimit>PT6H</ExecutionTimeLimit>
    <Priority>7</Priority>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{x(pythonw)}</Command>
      <Arguments>{x('"' + root + chr(92) + 'run.py" --if-due' if chr(92) in root else '"' + root + '/run.py" --if-due')}</Arguments>
      <WorkingDirectory>{x(root)}</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"""

if __name__ == "__main__":
    a = [v for v in sys.argv[1:] if not v.startswith("--")]
    root, pyw, user, out = a[:4]
    with open(out, "w", encoding="utf-16", newline="\r\n") as f:   # utf-16 codec writes the BOM
        f.write(render(root, pyw, user, logon="--no-logon" not in sys.argv))
    print(out)
