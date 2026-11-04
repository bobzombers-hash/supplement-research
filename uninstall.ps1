<#
.SYNOPSIS
  Removes the Supplement Research scheduled task. Data is kept unless -Purge is given.
.EXAMPLE
  powershell -NoProfile -ExecutionPolicy Bypass -File .\uninstall.ps1 [-Purge]
#>
[CmdletBinding()]
param(
    [switch]$Purge,
    [string]$Prefix = ''
)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version 2.0
$TaskName = 'SupplementResearch Update'
function Say([string]$Message) { Write-Host "[uninstall] $Message" -ForegroundColor Cyan }

if (-not $Prefix) {
    if (Test-Path -LiteralPath (Join-Path $PSScriptRoot 'run.py')) { $Prefix = $PSScriptRoot }
    elseif ($env:LOCALAPPDATA) { $Prefix = Join-Path $env:LOCALAPPDATA 'SupplementResearch' }
    else { $Prefix = $PSScriptRoot }
}
$Prefix = [System.IO.Path]::GetFullPath($Prefix).TrimEnd('\', '/')

$removed = $false
if (Get-Command Get-ScheduledTask -ErrorAction SilentlyContinue) {
    $t = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if ($t) {
        try { Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue } catch { Write-Verbose 'task was not running' }
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        $removed = $true
    }
} elseif (Get-Command 'schtasks.exe' -ErrorAction SilentlyContinue) {
    $old = $ErrorActionPreference; $ErrorActionPreference = 'Continue'
    & schtasks.exe /Delete /TN $TaskName /F 2>$null | Out-Null
    if ($LASTEXITCODE -eq 0) { $removed = $true }
    $ErrorActionPreference = $old
}
if ($removed) { Say "removed scheduled task '$TaskName'" } else { Say "no scheduled task '$TaskName' found" }
$marker = Join-Path $Prefix '.scheduler'
if (Test-Path -LiteralPath $marker) { Remove-Item -LiteralPath $marker -Force }

if ($Purge) {
    if (-not ((Test-Path -LiteralPath (Join-Path $Prefix 'run.py')) -and (Test-Path -LiteralPath (Join-Path $Prefix 'suppdb')))) {
        Say "$Prefix does not look like a Supplement Research install; not deleting it"
        exit 1
    }
    # stop a running update from this install, then leave the folder so it can be deleted
    Get-Process -Name 'python', 'pythonw' -ErrorAction SilentlyContinue |
        Where-Object { $_.Path -and $_.Path.StartsWith($Prefix, [System.StringComparison]::OrdinalIgnoreCase) } |
        Stop-Process -Force -ErrorAction SilentlyContinue
    Start-Sleep -Milliseconds 500
    Set-Location -LiteralPath ([System.IO.Path]::GetTempPath())
    Say "deleting $Prefix"
    try { Remove-Item -LiteralPath $Prefix -Recurse -Force }
    catch { Say ("could not delete everything ({0}); close any open files there and run again" -f $_.Exception.Message); exit 1 }
} else {
    Say "schedule removed; data kept in $Prefix (run uninstall.cmd -Purge to delete it)"
}
exit 0
