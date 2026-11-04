<#
.SYNOPSIS
  Supplement Research DB - Windows installer (no admin rights needed).

.DESCRIPTION
  Installs to %LOCALAPPDATA%\SupplementResearch, creates a private Python environment (.venv) with
  pinned packages, copies the starting database (an existing database is kept on re-install), and
  registers a Task Scheduler task for the current user that checks daily at 10:00 and at logon
  (missed runs are caught up). run.py --if-due starts a full update when the last one is 7+ days old.
  The first update cycle starts in the background right after installing.

.EXAMPLE
  powershell -NoProfile -ExecutionPolicy Bypass -File .\install.ps1
.EXAMPLE
  .\install.ps1 -Test            # also run a small foreground test cycle first
.EXAMPLE
  .\install.ps1 -InstallPython   # install Python 3.12 with winget if no Python 3.9+ is found
#>
[CmdletBinding()]
param(
    [string]$Prefix = '',
    [string]$Python = '',
    [switch]$InstallPython,
    [switch]$NoSchedule,
    [switch]$NoFirstRun,
    [switch]$Test
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version 2.0
$TaskName = 'SupplementResearch Update'
$Src = $PSScriptRoot

function Say([string]$Message) { Write-Host "[install] $Message" -ForegroundColor Cyan }
function Warn([string]$Message) { Write-Host "[install] WARNING: $Message" -ForegroundColor Yellow }
function Fail([string]$Message) { Write-Host "[install] ERROR: $Message" -ForegroundColor Red; exit 1 }

# Run a native program without PowerShell 5.1 turning its stderr output into a terminating error.
# Output goes to the console (or nowhere with -Quiet); the function returns only the exit code.
function Invoke-Native {
    param([string]$Exe, [string[]]$Arguments, [switch]$Quiet)
    $old = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        if ($Quiet) { & $Exe @Arguments 2>$null | Out-Null } else { & $Exe @Arguments | Out-Host }
        return [int]$LASTEXITCODE
    } finally { $ErrorActionPreference = $old }
}

# ---------------------------------------------------------------------------------------- target
if (-not $Prefix) {
    if (-not $env:LOCALAPPDATA) { Fail 'LOCALAPPDATA is not set; pass -Prefix <folder>.' }
    $Prefix = Join-Path $env:LOCALAPPDATA 'SupplementResearch'
}
$Prefix = [System.IO.Path]::GetFullPath($Prefix).TrimEnd('\', '/')
foreach ($od in @($env:OneDrive, $env:OneDriveConsumer, $env:OneDriveCommercial)) {
    if ($od -and $Prefix.StartsWith($od.TrimEnd('\'), [System.StringComparison]::OrdinalIgnoreCase)) {
        Fail "$Prefix is inside OneDrive ($od). Syncing a live SQLite database can corrupt it; use the default (%LOCALAPPDATA%\SupplementResearch) or another local folder."
    }
}
if (-not (Test-Path -LiteralPath (Join-Path $Src 'run.py'))) {
    Fail "run.py not found next to install.ps1 ($Src). Extract the whole zip first (right-click > Extract All), then run install.cmd from the extracted folder."
}
Say "installing to $Prefix"
foreach ($d in @('data', 'output', 'logs', 'config')) { New-Item -ItemType Directory -Force -Path (Join-Path $Prefix $d) | Out-Null }

# ---------------------------------------------------------------------------------------- files
$same = [string]::Equals([System.IO.Path]::GetFullPath($Src).TrimEnd('\', '/'), $Prefix, [System.StringComparison]::OrdinalIgnoreCase)
if (-not $same) {
    $items = @('suppdb', 'tools', 'run.py', 'README.md', 'requirements.txt', 'requirements-py39.txt', 'settings.example.json',
               'install.ps1', 'install.cmd', 'uninstall.ps1', 'uninstall.cmd', 'install.sh', 'uninstall.sh')
    foreach ($it in $items) {
        $from = Join-Path $Src $it
        if (-not (Test-Path -LiteralPath $from)) { continue }
        $to = Join-Path $Prefix $it
        if (Test-Path -LiteralPath $to) { Remove-Item -LiteralPath $to -Recurse -Force }
        Copy-Item -LiteralPath $from -Destination $to -Recurse -Force
    }
    Get-ChildItem -LiteralPath (Join-Path $Src 'config') -Filter '*.json' | ForEach-Object {
        Copy-Item -LiteralPath $_.FullName -Destination (Join-Path $Prefix 'config') -Force
    }
    $db = Join-Path (Join-Path $Prefix 'data') 'research.db'
    $srcDb = Join-Path (Join-Path $Src 'data') 'research.db'
    if (Test-Path -LiteralPath $db) {
        Say "keeping existing database $db"
    } elseif (Test-Path -LiteralPath $srcDb) {
        Copy-Item -LiteralPath $srcDb -Destination $db
        Say ("copied starting database ({0:N0} MB)" -f ((Get-Item -LiteralPath $db).Length / 1MB))
    } else {
        $url = if ($env:SUPP_DB_URL) { $env:SUPP_DB_URL } else { 'https://github.com/bobzombers-hash/supplement-research/releases/download/db-latest/research.db.gz' }
        $gz = "$db.gz.part"; $part = "$db.part"
        try {
            [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
            Say "downloading the latest database from $url"
            $ProgressPreference = 'SilentlyContinue'
            Invoke-WebRequest -Uri $url -OutFile $gz -UseBasicParsing
            $in = [IO.File]::OpenRead($gz)
            try {
                $out = [IO.File]::Create($part)
                try { $z = New-Object IO.Compression.GZipStream($in, [IO.Compression.CompressionMode]::Decompress); $z.CopyTo($out); $z.Dispose() }
                finally { $out.Dispose() }
            } finally { $in.Dispose() }
            Move-Item -LiteralPath $part -Destination $db -Force
            Say ("downloaded database ({0:N0} MB)" -f ((Get-Item -LiteralPath $db).Length / 1MB))
        } catch {
            Warn "no starting database found or downloadable ($($_.Exception.Message)); the first run builds one from scratch (slow)"
        } finally {
            Remove-Item -LiteralPath $gz, $part -Force -ErrorAction SilentlyContinue
        }
    }
    $idx = Join-Path (Join-Path $Prefix 'output') 'index.html'
    $srcOut = Join-Path $Src 'output'
    if (-not (Test-Path -LiteralPath $idx) -and (Test-Path -LiteralPath $srcOut)) {
        Copy-Item -Path (Join-Path $srcOut '*') -Destination (Join-Path $Prefix 'output') -Recurse -Force
    }
}
$settingsPath = Join-Path $Prefix 'settings.json'
if (-not (Test-Path -LiteralPath $settingsPath)) { Copy-Item -LiteralPath (Join-Path $Prefix 'settings.example.json') -Destination $settingsPath }
Get-ChildItem -LiteralPath $Prefix -Recurse -Directory -Filter '__pycache__' -ErrorAction SilentlyContinue |
    Where-Object { $_.FullName -notlike '*.venv*' } | Remove-Item -Recurse -Force -ErrorAction SilentlyContinue
if (Get-Command Unblock-File -ErrorAction SilentlyContinue) {
    # remove the "downloaded from the internet" mark that zip extraction copies onto every file
    try {
        Get-ChildItem -LiteralPath $Prefix -Recurse -File -ErrorAction SilentlyContinue |
            Where-Object { $_.FullName -notlike '*\.venv\*' } | Unblock-File -ErrorAction SilentlyContinue
    } catch { Write-Verbose ("Unblock-File skipped: {0}" -f $_.Exception.Message) }
}

# ---------------------------------------------------------------------------------------- python
function Get-PyInfo {
    param([string]$Exe, [string[]]$Pre = @())
    $old = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $out = & $Exe @Pre -c 'import sys; print(sys.version_info[0] * 100 + sys.version_info[1]); print(sys.executable)' 2>$null
        if ($LASTEXITCODE -ne 0 -or -not $out) { return $null }
        $lines = @($out)
        if ($lines.Count -lt 2) { return $null }
        $v = [int]$lines[0]
        if ($v -lt 309) { return $null }
        return [pscustomobject]@{ Exe = $lines[1].Trim(); Ver = $v }
    } catch { return $null } finally { $ErrorActionPreference = $old }
}

function Test-StoreStub([string]$Path) {
    # %LOCALAPPDATA%\Microsoft\WindowsApps\python.exe is only an "install from the Store" alias
    # unless the Store Python package is really installed.
    if ($Path -notlike '*\WindowsApps\*') { return $false }
    $pkgs = @(Get-ChildItem -Path (Join-Path $env:LOCALAPPDATA 'Microsoft\WindowsApps') -Directory -Filter 'PythonSoftwareFoundation.Python.*' -ErrorAction SilentlyContinue)
    return ($pkgs.Count -eq 0)
}

function Find-Python {
    if ($Python) { return (Get-PyInfo $Python) }
    $py = Get-Command 'py' -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($py) {
        foreach ($v in @('-3.13', '-3.12', '-3.11', '-3.10', '-3.9', '-3')) {
            $i = Get-PyInfo $py.Source @($v)
            if ($i) { return $i }
        }
    }
    foreach ($name in @('python', 'python3')) {
        foreach ($c in @(Get-Command $name -CommandType Application -All -ErrorAction SilentlyContinue)) {
            if (Test-StoreStub $c.Source) { continue }
            $i = Get-PyInfo $c.Source
            if ($i) { return $i }
        }
    }
    $globs = @()
    if ($env:LOCALAPPDATA) { $globs += (Join-Path $env:LOCALAPPDATA 'Programs\Python\Python3*\python.exe') }
    if ($env:ProgramFiles) { $globs += (Join-Path $env:ProgramFiles 'Python3*\python.exe') }
    foreach ($g in $globs) {
        foreach ($f in @(Get-ChildItem -Path $g -ErrorAction SilentlyContinue | Sort-Object FullName -Descending)) {
            $i = Get-PyInfo $f.FullName
            if ($i) { return $i }
        }
    }
    return $null
}

$pyInfo = Find-Python
if (-not $pyInfo) {
    $winget = Get-Command 'winget' -ErrorAction SilentlyContinue
    $answer = 'n'
    if ($InstallPython) { $answer = 'y' }
    elseif ($winget -and [Environment]::UserInteractive) {
        $answer = Read-Host '[install] Python 3.9+ was not found. Install Python 3.12 now with winget (per-user, no admin)? [Y/n]'
        if (-not $answer) { $answer = 'y' }
    }
    if ($winget -and $answer -match '^[Yy]') {
        Say 'running: winget install --id Python.Python.3.12 -e --scope user'
        $null = Invoke-Native 'winget' @('install', '--id', 'Python.Python.3.12', '-e', '--scope', 'user', '--accept-package-agreements', '--accept-source-agreements')
        $pyInfo = Find-Python
    }
    if (-not $pyInfo) {
        Write-Host ''
        Write-Host 'Python 3.9 or newer is required. Install it with ONE of these, then run install.cmd again:' -ForegroundColor Yellow
        Write-Host '  * PowerShell:  winget install --id Python.Python.3.12 -e --scope user'
        Write-Host '  * or download from https://www.python.org/downloads/windows/ (the default options are fine;'
        Write-Host '    the "py" launcher it installs is how this installer finds Python)'
        Write-Host '  * Note: typing "python" may open the Microsoft Store; that alias alone is not a Python install.'
        exit 1
    }
}
Say ("using {0} (Python {1}.{2})" -f $pyInfo.Exe, [math]::Floor($pyInfo.Ver / 100), ($pyInfo.Ver % 100))

$Venv = Join-Path $Prefix '.venv'
function Get-VenvPython([string]$Name) {
    $win = Join-Path (Join-Path $Venv 'Scripts') "$Name.exe"
    if (Test-Path -LiteralPath $win) { return $win }
    return (Join-Path (Join-Path $Venv 'bin') 'python')   # non-Windows (used only for testing this script)
}
$VPy = Get-VenvPython 'python'
if (Test-Path -LiteralPath $VPy) {
    $cur = Get-PyInfo $VPy
    if (-not $cur -or $cur.Ver -ne $pyInfo.Ver) { Say 'Python version changed; rebuilding .venv'; Remove-Item -LiteralPath $Venv -Recurse -Force }
}
if (-not (Test-Path -LiteralPath $VPy)) {
    Say 'creating .venv'
    if ((Invoke-Native $pyInfo.Exe @('-m', 'venv', $Venv)) -ne 0) { Fail 'could not create the virtual environment' }
}
$VPy = Get-VenvPython 'python'
$VPyw = Get-VenvPython 'pythonw'
if (-not (Test-Path -LiteralPath $VPyw)) { $VPyw = $VPy }

$pipArgs = @('-m', 'pip', 'install', '-q', '--disable-pip-version-check', '--default-timeout', '60')
$null = Invoke-Native $VPy ($pipArgs + @('--upgrade', 'pip')) -Quiet
$req = Join-Path $Prefix 'requirements-py39.txt'
if ($pyInfo.Ver -ge 312) { $req = Join-Path $Prefix 'requirements.txt' }
Say ("installing pinned dependencies from {0}" -f (Split-Path -Leaf $req))
if ((Invoke-Native $VPy ($pipArgs + @('-r', $req))) -ne 0) {
    Warn 'pinned install failed; falling back to unpinned numpy/scipy/matplotlib'
    if ((Invoke-Native $VPy ($pipArgs + @('numpy', 'scipy', 'matplotlib'))) -ne 0) { Fail 'dependency install failed (see the messages above)' }
}
$RunPy = Join-Path $Prefix 'run.py'
if ((Invoke-Native $VPy @('-c', 'import numpy, scipy, matplotlib') -Quiet) -ne 0) { Fail 'dependencies do not import' }
if ((Invoke-Native $VPy @($RunPy, '--status') -Quiet) -ne 0) { Fail 'self-check (run.py --status) failed' }

# ---------------------------------------------------------------------------------------- schedule
$scheduled = $false
$schedDesc = 'none'
if (-not $NoSchedule) {
    $user = $null
    try { $user = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name } catch { $user = $null }
    if (-not $user) {
        if ($env:USERDOMAIN) { $user = "$env:USERDOMAIN\$env:USERNAME" } elseif ($env:USERNAME) { $user = $env:USERNAME } else { $user = $env:USER }
    }
    $xmlPath = Join-Path (Join-Path $Prefix 'logs') 'task.xml'
    $render = Join-Path (Join-Path $Prefix 'tools') 'render_task_xml.py'
    $haveCmdlet = [bool](Get-Command Register-ScheduledTask -ErrorAction SilentlyContinue)
    $haveSchtasks = [bool](Get-Command 'schtasks.exe' -ErrorAction SilentlyContinue)
    foreach ($withLogon in @($true, $false)) {
        $rArgs = @($render, $Prefix, $VPyw, $user, $xmlPath)
        if (-not $withLogon) { $rArgs += '--no-logon' }
        if ((Invoke-Native $VPy $rArgs -Quiet) -ne 0) { Fail 'could not render the task definition' }
        try {
            if ($haveCmdlet) {
                $xml = Get-Content -LiteralPath $xmlPath -Raw -Encoding Unicode
                Register-ScheduledTask -TaskName $TaskName -Xml $xml -Force | Out-Null
            } elseif ($haveSchtasks) {
                if ((Invoke-Native 'schtasks.exe' @('/Create', '/TN', $TaskName, '/XML', $xmlPath, '/F') -Quiet) -ne 0) { throw 'schtasks /Create failed' }
            } else {
                Warn 'Task Scheduler is not available on this system; no schedule registered'
                break
            }
            $scheduled = $true
            if ($withLogon) { $schedDesc = "Task Scheduler task '$TaskName': daily 10:00 + 2 min after logon, catch-up when missed (StartWhenAvailable), only with network; a full cycle runs when the last successful one is 7+ days old" }
            else { $schedDesc = "Task Scheduler task '$TaskName': daily 10:00, catch-up when missed (StartWhenAvailable), only with network (logon trigger was refused on this PC)" }
            break
        } catch {
            if ($withLogon) { Warn ("registering with a logon trigger failed ({0}); retrying with the daily trigger only" -f $_.Exception.Message) }
            else { Warn ("could not register the scheduled task: {0}" -f $_.Exception.Message) }
        }
    }
    if ($scheduled -and $haveCmdlet) {
        $t = Get-ScheduledTask -TaskName $TaskName
        $info = Get-ScheduledTaskInfo -TaskName $TaskName
        Say ("task registered: '{0}' state={1} next run={2}" -f $t.TaskName, $t.State, $info.NextRunTime)
    } elseif ($scheduled) {
        Say "task registered: '$TaskName'"
    }
}
Set-Content -LiteralPath (Join-Path $Prefix '.scheduler') -Value ($(if ($scheduled) { 'taskscheduler' } else { 'none' })) -Encoding ASCII

# ---------------------------------------------------------------------------------------- runs
if ($Test) {
    Say 'running a small test cycle (1 new supplement, 30 new records each, 2 full texts)...'
    if ((Invoke-Native $VPy @($RunPy, '--test', '--no-notify')) -eq 0) { Say 'test cycle OK' } else { Warn ("test cycle reported a problem; see {0}" -f (Join-Path (Join-Path $Prefix 'logs') 'update.log')) }
}
if (-not $NoFirstRun) {
    $started = $false
    if ($scheduled -and (Get-Command Start-ScheduledTask -ErrorAction SilentlyContinue)) {
        try { Start-ScheduledTask -TaskName $TaskName; $started = $true } catch { $started = $false }
    }
    if (-not $started) {
        $sp = @{ FilePath = $VPyw; ArgumentList = @(('"{0}"' -f $RunPy), '--if-due'); WorkingDirectory = $Prefix }
        if ($env:OS -eq 'Windows_NT') { $sp['WindowStyle'] = 'Hidden' }   # pythonw has no window anyway
        Start-Process @sp | Out-Null
    }
    Say 'first update cycle started in the background (30-90 min; progress in logs\update.log)'
}

$dash = Join-Path (Join-Path $Prefix 'output') 'index.html'
Say 'done.'
Say "schedule:  $schedDesc"
Say "dashboard: $dash"
Say "logs:      $(Join-Path (Join-Path $Prefix 'logs') 'update.log')"
Say "settings:  $settingsPath"
Say "run now:   `"$VPy`" `"$RunPy`"      remove: $(Join-Path $Prefix 'uninstall.cmd')"
exit 0
