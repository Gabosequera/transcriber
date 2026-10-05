# Transcriptor 0.4.5

Corrige el arranque que Windows bloqueaba con el error 4551 y publica las mejoras
de diarización local, GPU y conversación legible preparadas en 0.4.4.

- Python oficial firmado por Python Software Foundation, con reparación de los
  entornos existentes conservando paquetes, modelos y datos.
- Diarización local Community-1, selección de hablantes y configuración del token
  de Hugging Face en Ajustes.
- Inferencia Torch en procesos separados en Windows y reintento en CPU ante
  fallos de CUDA o memoria insuficiente.
- Exportación de conversación legible, compatibilidad de dependencias y extracción
  de componentes con rutas largas de Windows.
- Pruebas de diarización independientes de los paquetes opcionales de inferencia,
  para que también funcionen en los entornos mínimos de integración continua.

Para instalar, descarga `transcriptor-installer-v0.4.5.zip`, extráelo en una carpeta
permanente y ejecuta `setup-windows.bat`. Después abre `run.bat`.

Si Windows bloquea una instalación anterior con el error 4551, cierra la app,
extrae el instalador nuevo en la carpeta existente y ejecuta `setup-windows.bat`.
La actualización del código por sí sola no puede reparar un Python bloqueado
antes de que la app arranque. El instalador conserva los modelos y datos.

La publicación exige las pruebas en Linux y Windows y una instalación desde cero
seguida de una segunda ejecución del instalador para comprobar su idempotencia.
Los cambios se describen también en [las notas de 0.4.4](RELEASE-0.4.4.md), cuyo
primer intento de publicación se detuvo en las pruebas de integración continua.
