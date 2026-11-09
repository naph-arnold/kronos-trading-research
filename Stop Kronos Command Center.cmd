@echo off
rem Stop the Kronos Command Center cleanly. Never affects the trading runners.
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0ops\windows\command_center.ps1" -Action stop %*
timeout /t 3 >nul
