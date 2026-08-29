@echo off
REM Lanza la app con consola VISIBLE (python.exe, no pythonw) para ver errores.
REM Si la app crashea al arrancar, el traceback queda en pantalla.
set PYTHONUTF8=1
cd /d "%~dp0"

if not exist ".bootstrap\Scripts\python.exe" (
  echo No hay entorno instalado. Corre primero setup-windows.bat
  pause
  exit /b 1
)

echo === Arrancando la app con consola visible (diagnostico) ===
echo.
set TRANSCRIPTOR_DIAGNOSTIC=1
".bootstrap\Scripts\python.exe" bootstrap.py %*
echo.
echo ===== La app termino / cerro. Si hubo un error, esta ARRIBA. =====
pause
