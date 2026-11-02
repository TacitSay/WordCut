@echo off
REM WordCut 生产模式启动（免 Node）：后端在同一端口托管界面与 API
REM 等价于：powershell -ExecutionPolicy Bypass -File start.ps1 -Prod
setlocal
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0start.ps1" -Prod %*
if errorlevel 1 pause
endlocal
