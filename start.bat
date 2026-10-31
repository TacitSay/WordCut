@echo off
rem ============================================================
rem  WordCut launcher - double click this file.
rem  Real logic lives in start.ps1 (kept in PowerShell so that
rem  paths with spaces / Chinese characters are handled safely).
rem  Extra args are forwarded:  start.bat -NoBrowser  /  -Restart
rem ============================================================
setlocal
cd /d "%~dp0"

where powershell >nul 2>nul
if errorlevel 1 (
    echo [X] powershell not found. Windows 7+ should have it.
    pause
    exit /b 1
)

if not exist "%~dp0start.ps1" (
    echo [X] start.ps1 not found next to this file.
    pause
    exit /b 1
)

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0start.ps1" %*
if errorlevel 1 (
    echo.
    echo [X] Startup reported an error. Check the messages above.
    pause
)
exit /b 0
