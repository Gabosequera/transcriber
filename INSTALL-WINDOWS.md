# Instalar y actualizar Transcriptor en Windows

## Primera instalación

1. Descarga `transcriptor-installer-vX.Y.Z.zip` desde la release estable más reciente.
2. Extrae el ZIP en una carpeta permanente, por ejemplo `C:\Transcriptor`.
3. Ejecuta `setup-windows.bat` una sola vez.
4. Abre la aplicación con `run.bat`.

No hace falta instalar Python, Git, ffmpeg ni permisos de administrador. El instalador
descarga `uv`, crea un Python bootstrap pequeño y prepara el runtime de procesamiento.

## Layout administrado

```text
Transcriptor\
├── .bootstrap\              launcher; no contiene modelos
├── releases\<versión>\     código inmutable de cada versión
├── runtimes\<hash>\        Python y dependencias por lock
├── shared\
│   ├── cache\               Hugging Face, Torch y uv
│   ├── components\          LaughterSegmentation y Respiro-en
│   ├── config\              configuración y presets por máquina
│   ├── llama\               binarios de llama.cpp
│   ├── models\              GGUF y MediaPipe
│   └── tools\               uv y ffmpeg
├── state\current.json       release activa, anterior y estado de salud
└── run.bat
```

Actualizar código no modifica `shared`. Si las dependencias no cambian, tampoco se crea
otro runtime. Cuando cambian, se instala un runtime nuevo y el anterior queda disponible
para rollback; los pesos de los modelos continúan en el mismo caché compartido.

## Configurar GitHub

El repositorio se fija una vez en `update-channel.json`:

```powershell
.\configurar-github.ps1 owner/repositorio
```

También puede ejecutarse `configurar-github.bat` y escribir `owner/repositorio`. Por
defecto se esperan releases públicas. Un repositorio privado requiere proporcionar el
token únicamente mediante `TRANSCRIPTOR_GITHUB_TOKEN`; la app nunca lo escribe a disco.

## Actualizaciones automáticas

Al arrancar, el launcher consulta la release estable más reciente de GitHub. Si existe
una versión superior:

1. descarga el manifiesto y el ZIP a `updates\`;
2. valida versión, plataforma, tamaño, SHA-256 y la lista exacta de archivos;
3. rechaza rutas inseguras, enlaces y contenido no declarado;
4. extrae a staging y prepara el runtime sólo si cambió su hash;
5. cambia atómicamente la release activa;
6. espera una confirmación de salud de la nueva interfaz;
7. vuelve a la release anterior si el arranque falla.

Sin conexión o ante cualquier error de GitHub se abre la versión ya instalada. Nunca se
borra o reemplaza una release funcional para aplicar una actualización.

## Publicar una release

1. Actualiza `VERSION`, por ejemplo a `0.2.0`.
2. Ejecuta las pruebas.
3. Crea y sube el tag correspondiente: `v0.2.0`.
4. `.github/workflows/release.yml` valida el tag, construye los assets y crea la release.

Assets producidos:

- `transcriptor-windows-vX.Y.Z.zip`: código consumido por el updater;
- `transcriptor-update-vX.Y.Z.json`: hash y contrato de instalación;
- `transcriptor-installer-vX.Y.Z.zip`: paquete para una computadora nueva.

El repositorio debe habilitar protección de tags y, si está disponible en sus ajustes,
releases inmutables. Nunca se debe reemplazar el contenido de un tag publicado: cualquier
cambio produce una versión nueva.

Antes de publicar, el workflow ejecuta `setup-windows.ps1 -NonInteractive` sobre un
runner Windows limpio y valida el runtime, sus imports, ffmpeg y el detector de risas.
La release no se publica si esa instalación integral falla.

## Diagnóstico

`diagnostico.bat` deja la consola visible. Los demás registros están en `shared\logs` y
el estado del updater en `state\update-status.json`.

## Podcasts y cortes externos

El modo Automático extrae voz y espera el plan de una AI externa; no requiere Codex
CLI para procesar. La skill se distribuye en `skills/transcriptor/SKILL.md` dentro de
la release. Consulta el flujo y los archivos en README.md.

Torch y TorchAudio se fijan juntos en 2.8.0 (CUDA 12.8), porque el alineador MMS usa
`forced_align`, retirado en versiones posteriores. El verificador ejecuta una pequeña
alineación CTC sin descargar pesos, además de abrir la interfaz. No actualices estos
paquetes por separado. Referencia: [TorchAudio 2.8](https://docs.pytorch.org/audio/2.8/tutorials/ctc_forced_alignment_api_tutorial.html).

Los cortes se exportan en segundo plano como MP4 H.264/AAC, conservando todas las
pistas y el original. Usa una carpeta con espacio para los videos recodificados; una
exportación terminada no se sobrescribe. Para mover proyectos entre máquinas, importa
el mismo video y abre su master editorial; las rutas antiguas no se usan para exportar.
