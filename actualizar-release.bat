@echo off
REM Reconstruye la release instalada a partir del codigo fuente de esta carpeta
REM (misma herramienta que setup-windows.ps1, pasos 3 y 4) y la reinstala.
REM El runtime de Python ya existente se reutiliza; no descarga nada.
REM Cierra la app antes de correr esto.
set PYTHONUTF8=1
set PYTHONDONTWRITEBYTECODE=1
cd /d "%~dp0"

if not exist ".bootstrap\Scripts\python.exe" (
  echo No hay entorno instalado. Corre primero setup-windows.bat
  pause
  exit /b 1
)

set /p VERSION=<VERSION
echo === Construyendo release %VERSION% ===
".bootstrap\Scripts\python.exe" -B tools\build_release.py --version %VERSION% --output updates\bootstrap
if errorlevel 1 goto :error

echo === Instalando release %VERSION% ===
".bootstrap\Scripts\python.exe" -B updater.py install-bundle --root "%~dp0." ^
  --manifest "updates\bootstrap\transcriptor-update-v%VERSION%.json" ^
  --archive "updates\bootstrap\transcriptor-windows-v%VERSION%.zip"
if errorlevel 1 goto :error

echo.
echo ===== Release actualizada. Ya puedes abrir la app con run.bat =====
pause
exit /b 0

:error
echo.
echo ===== Fallo la actualizacion. El error esta ARRIBA. =====
pause
exit /b 1
