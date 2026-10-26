@echo off
rem Bilibili Downloader: double-click, or run  run.bat  in a terminal. See README.md for options.
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0start.ps1" %*
set "CODE=%errorlevel%"
if not "%CODE%"=="0" (
  echo.
  echo Something went wrong. The message above says what.
  pause
)
exit /b %CODE%
