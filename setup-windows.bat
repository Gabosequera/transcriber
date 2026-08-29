@echo off
REM Lanza el instalador automatico (setup-windows.ps1). Doble clic aca.
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup-windows.ps1"
echo.
pause
