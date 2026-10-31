@echo off
rem Stop WordCut services (backend uvicorn + frontend vite) started from this folder.
setlocal
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0stop.ps1"
pause
