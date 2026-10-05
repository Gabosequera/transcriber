# Transcriptor 0.4.4

Esta versión corrige el arranque bloqueado por Windows con el error 4551 y reúne
las mejoras locales de diarización, procesamiento en GPU y conversación legible.

## Cambios

- Python oficial firmado por Python Software Foundation en `shared/python`.
  El instalador repara los entornos existentes conservando paquetes, modelos y datos.
  Los nuevos runtimes usan ese Python y los redirectores oficiales de CPython.
- Diarización local con pyannote Community-1, selección de cantidad de hablantes
  y configuración del token de Hugging Face en Ajustes. El modelo requiere aceptar
  sus condiciones en Hugging Face; el procesamiento del audio ocurre en la PC.
- Alineación MMS, diarización, arousal y detección de risa en procesos Torch
  separados en Windows, con reintento en CPU si falla CUDA o falta memoria.
- Exportación de conversación legible y mejoras en el flujo de proyectos editoriales.
- Dependencias fijadas para conservar la compatibilidad de PyAV, Torch y torchaudio.
- Extracción de componentes auxiliares compatible con rutas largas de Windows.

## Instalación

Para una instalación nueva, descarga `transcriptor-installer-v0.4.4.zip`, extráelo
en una carpeta permanente y ejecuta `setup-windows.bat`. Después abre `run.bat`.

Si una instalación anterior muestra el error 4551, utiliza el instalador nuevo
en la carpeta existente y vuelve a ejecutar `setup-windows.bat`. La reparación
conserva los paquetes y modelos instalados. La actualización automática del código
no puede reparar por sí sola un Python que Windows bloquea antes de iniciar.

## Validación

172 pruebas locales pasan en Windows. Arranque mediante `run.bat`, confirmación de salud de la interfaz e imports
de Torch, torchaudio, PyAV y Tkinter verificados en Windows. La publicación exige
pruebas automáticas en Linux y Windows, instalación desde cero y una segunda
ejecución del instalador para comprobar su reparación idempotente.
