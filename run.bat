@echo off
REM Bootstrap estable: selecciona la release activa, actualiza y lanza su runtime.
set PYTHONUTF8=1
cd /d "%~dp0"

if not exist ".bootstrap\Scripts\python.exe" (
  echo No hay entorno instalado. Corre primero setup-windows.bat
  pause
  exit /b 1
)

if exist ".bootstrap\Scripts\pythonw.exe" (
  start "" ".bootstrap\Scripts\pythonw.exe" bootstrap.py %* 2>"%~dp0shared\logs\startup-error.log"
) else (
  ".bootstrap\Scripts\python.exe" bootstrap.py %*
)
