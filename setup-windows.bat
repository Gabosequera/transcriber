@echo off
REM Lanza el instalador automatico (setup-windows.ps1). Doble clic aca.
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup-windows.ps1"
set "SETUP_EXIT=%ERRORLEVEL%"
echo.
if not "%SETUP_EXIT%"=="0" (
  echo ERROR: la instalacion no pudo completarse. Revisa el mensaje anterior.
)
pause
exit /b %SETUP_EXIT%
