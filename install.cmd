@echo off
rem Double-click to install Supplement Research (no admin rights needed).
setlocal
if not exist "%~dp0install.ps1" (
  echo install.ps1 was not found next to this file.
  echo Right-click the downloaded zip, choose "Extract All...", then double-click install.cmd in the extracted folder.
  pause
  exit /b 1
)
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1" %*
set "RC=%ERRORLEVEL%"
echo.
if "%RC%"=="0" (echo Installation finished. The dashboard is %LOCALAPPDATA%\SupplementResearch\output\index.html) else (echo Installation FAILED ^(code %RC%^). See the messages above.)
pause
exit /b %RC%
