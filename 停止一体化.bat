@echo off
rem ---------------------------------------------------------------
rem  Stop-all launcher: auto-elevate to administrator.
rem  MaiBot runs elevated (MAA control needs it), so a non-elevated
rem  Stop-Process is denied and the script only reports "not running"
rem  while nothing actually stops -- that is the "cannot stop MaiBot"
rem  symptom. Elevate first, same as the start script does.
rem ---------------------------------------------------------------
net session >nul 2>&1
if %errorlevel% neq 0 (
    echo Requesting administrator privileges to stop MaiBot...
    powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
    exit /b
)
chcp 65001 >nul
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0stop_all.ps1"
pause
