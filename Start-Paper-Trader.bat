@echo off
title Alpaca AI Dashboard
cd /d "%~dp0"
echo Starting dashboard on http://127.0.0.1:4200 ...
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0start-dashboard.ps1"
if errorlevel 1 pause
