@echo off
rem Start the read-only Kronos Command Center (if not already running) and open it in the browser.
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0ops\windows\command_center.ps1" -Action start %*
if errorlevel 1 pause
