@echo off
rem Removes the scheduled task. Add -Purge to also delete the program and its data.
rem Everything runs on one line so cmd does not need to re-read this file after -Purge deletes it.
cd /d "%TEMP%" & powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0uninstall.ps1" %* & pause
