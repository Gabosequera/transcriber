# Referencia técnica — Transcriptor (pipeline de metadata y perfil editorial)

> Antes `HANDOFF.md`. Es la referencia del ESTADO ACTUAL para que otra persona u otro
> agente continúe. La cronología está en [historial.md](historial.md); las decisiones
> vigentes del perfil editorial, en [especificacion-editorial.md](especificacion-editorial.md);
> el uso desde la app, en [guia-automatico.md](guia-automatico.md) y
> [guia-manual.md](guia-manual.md).

> **Actualización 2026-09-06 (2ª sesión):** RECORTES en Automático: heurística de huecos
> sin voz + carril interactivo en el timeline (mover/estirar/crear/borrar/activar con el
> mouse) + segunda pasada de la AI (recortes de contenido, skill Tarea 2) + «Cortar y
> exportar» con `trim`/`concat` de FFmpeg. Nada se corta hasta ese botón. §7.0b.
>
> **Actualización 2026-09-06:** primer uso real de la instalación administrada en la PC
> Windows (release 0.2.0). Arreglados el arranque (bytecode en la release) y la alineación
> MMS bajo `pythonw`; Whisper y MMS son pasos separados y reanudables; `large-v3-turbo` es
> el modelo por defecto (Ajustes); pasos opcionales marcables en Automático; aviso
> retomar/reescribir al reanudar. Detalle en §7 y en `historial.md`.
>
> **Actualización 2026-08-29:** el modo Automático ahora implementa el pipeline
> editorial multipista de Fase 1 en `editorial_pipeline.py`. Este documento describe
> principalmente el pipeline multimodal Manual heredado. La especificación vigente del
> flujo nuevo está en `especificacion-editorial.md`. El chunking automático usa
> `codex_chunker.py` + `codex exec` para leer la conversación completa; no se rige por
> los comandos históricos de Codex documentados más abajo para reviews de desarrollo.

Última reescritura completa: 2026-07-20; última actualización: 2026-09-06 (recortes en
Automático; instalación Windows real). El historial cronológico (qué se hizo, cuándo y por
qué) vive en `historial.md`. Los diseños y reviews (`three-brain-out/<fecha-tema>/`) y los originales pre-reescritura (`docs-archivo/`) viven en el checkout Linux de desarrollo; están en `.gitignore` y no forman parte del repositorio ni de la release.

---

## 1. Qué es

App de escritorio (Python 3.13 + customtkinter) que Gabriel (canal "El Grafo") usa para
convertir sus grabaciones largas (gameplay/reacción, OBS multitrack) en metadata rica
que una IA ("Ava", vía la skill /clipear) usa para cortar clips de TikTok/Shorts sola.

Desarrollada en Linux (`/mnt/data/transcriber`), corre también en Windows como instalación
administrada (§4). La app abre en modo **Automático** (perfil editorial de
`especificacion-editorial.md`, implementado en `editorial_pipeline.py` + `automatico_ui.py`);
el modo **Manual**, que describe el resto de esta sección, tiene cuatro pestañas y Ajustes
en el menú ⋮:

1. **Transcribir** — faster-whisper (GPU/CPU) + alineador forzado MMS (timestamps de
   palabra ~50 ms, la base de TODO el pipeline). Salidas: words/segments/srt/cues.
2. **Limpiar audio** — atenúa respiraciones SIN cambiar la duración. 3 modos: Avanzado
   (heurístico), VAD (Silero), Quirúrgico (red Respiro-en + escudo de palabras + red de
   seguridad espectral).
3. **Extraer metadata** — wizard de 3 pasos sobre el video multitrack:
   - Paso 1: mini-editor compartido (`editor_medios.py`, ver §3.1/§3.2): timeline con
     zoom/pan estilo DaVinci, waveform por pista, playhead compartido audio/video,
     reproductor STREAMING con prefetch de scrub, rects juego/cámara, roles por
     pista, MARCAS DEL AUTOR con prompts y decisiones incluir/excluir.
   - Paso 2: configuración de todos los análisis con presets globales.
   - Paso 3: corre el pipeline completo con resume transaccional.
4. **Marcar** — revisión POST-extracción (ver §3.2): importa un video ya procesado,
   encuentra su master.json por fingerprint, pinta la metadata como carriles
   read-only sobre el timeline, permite marcar viendo esa metadata y edita el GUION
   (`<video>.guion.md`) que junto al master es el paquete para la IA que edita.
5. **Ajustes** — hardware (CPU/hilos/GPU) + memoria.

## 2. El pipeline de metadata (paso 3 del wizard)

DAG de pasos en `pipeline.py`, cada uno con manifest (params_hash + entradas_hash +
version) que permite REUSE exacto y resume tras crash/cancelación:

```
extraer_voz / extraer_juego      pistas FLAC normalizadas a la timeline canonica T0
transcribir_voz                  whisper + MMS
emocion_voz / risa / pausas / ava   analisis de la voz
publicar_metadata_voz            junta el metadata.json de voz
fondo                            audio del juego: dialogo (whisper) + sonidos/
                                 transitorios/escenas (AST) + descripcion (LALM
                                 local llama.cpp u ONLINE: OpenRouter o Alibaba)
cara                             facecam: MediaPipe (reaccion/mirada/presencia) +
                                 EmotiEffLib (emociones valence/arousal)
motion                           escaneo local de cambio de pixeles (para vision)
vision_juego / vision_camara     VLM online (OpenRouter qwen3-vl): descripciones
                                 por chunk con muestreo adaptativo por movimiento
consolidar                       master.json v2 (todos los streams, IDs estables)
vistas                           superficies de lectura para la IA
```

Estados de paso: ok / reused / partial (consumible; reintenta lo fallado al re-correr)
/ failed / skipped_unavailable / skipped_not_requested / cancelled.

Cada run persiste su spec resuelto SIN claves de API en `work/spec.resuelto.json`;
`pipeline.actualizar_derivados(outdir, fuente=)` re-corre SOLO consolidar+vistas con
una snapshot NUEVA de marcas (botón «Preparar paquete» de la tab Marcar) validando
upstream en orden topológico con sha SIEMPRE — jamás un master degradado en silencio.
`fuente=` es la ruta ACTUAL del video (los paths son localizadores; la identidad la
valida el fingerprint — el video puede haberse movido tras extraer).

### El master.json (schema v2, consolidar.py 2.3)

Un JSON por video con header global (hashes de fuentes, duración ffprobe, baselines,
media_layout con rects/roles/offsets, y desde 2.3 `fuentes.media` = fingerprint del
medio, con el que la tab Marcar matchea video↔master) + streams namespaced con IDs
estables:

- `voz.transcript/emocion/risa/pausas/instrucciones` (S/E/R/P/AVA)
- `fondo.dialogo/sonidos/transitorios/escenas/descripciones`
- `video.cara.reaccion/mirada/presencia/emocion` (CR/CM/CP/CE)
- `video.juego.vlm` / `video.camara.vlm` (VJ/VC — IDs derivados, sin renumerar)
- `autor.marcas` (MK — ídem)

`IDS_EXTERNOS` = streams cuyos IDs vienen de una identidad canónica externa y NO se
renumeran (autor.marcas, video.*.vlm).

### Vistas (vistas.py 1.2) y EDL (edl.py)

- `vistas/`: inventario.json, transcript.md (limpio), transcript_anotado.md (tiers),
  indice_senales.md (semillas con cuotas), directivas.md (ledger marcas+Ava, solo si
  hay), dossier por demanda (con "Contexto visual VLM").
- `edl.py`: EDL versionada en ms + gate mecánico de bordes sobre words.json + render
  (cortar_clip.py) + cut_view con junction cards y riesgo_base.

### La skill /clipear

`~/.claude/skills/clipear/` (SKILL.md + formatos/cortos|largo.md) — protocolo por
capas para la IA que corta. En `~/.codex/skills/clipear/` es un SYMLINK a la de
.claude (2026-07-22, como el resto de las skills) — ya no hay que espejar nada.

## 3. Módulos (un renglón cada uno)

| Módulo | Qué hace |
|---|---|
| app.py | GUI 5 tabs, threading por cola msgs, reflow responsivo |
| core.py | faster-whisper GPU/CPU + fallback OOM + preload CUDA (sin nvblas) |
| align.py | alineación forzada MMS (torchaudio) |
| gate.py | limpieza de respiraciones (3 motores, duración inmutable) |
| detect_breaths.py | Respiro-en por trozos 15s/2s |
| metadata.py | arousal audio (audeering) + valence texto (pysentimiento) |
| laughter.py | risa (omine/LaughterSegmentation, timeline global) |
| extraer_pausas.py | pausas desde words.json (hechos crudos) |
| ava.py | instrucciones habladas "hey Ava" (heurística sobre words.json) |
| escena_audio.py | AST tags + transitorios + escenas del audio del juego |
| describir.py | descripción LALM por escena: llama.cpp local U OpenRouter online |
| fondo.py | orquestador del bloque audio-del-juego (guardado por capa) |
| cara.py | facecam: MediaPipe + EmotiEffLib + calibración mirada en vivo |
| vision.py | visión VLM: motion scan + chunks adaptativos + OpenRouter |
| marcas.py | marcas del autor: sidecar+store con revisión, cuarentena, Registro COMPARTIDO (transaccional) |
| guion.py | el guion `<video>.guion.md`: vista del sidecar + texto libre (JSON manda) |
| consolidar.py | master.json v2 (2.3: + fuentes.media) |
| vistas.py | superficies de lectura para la IA |
| edl.py | EDL + gate + render + cut_view |
| editor_medios.py | el MINI-EDITOR reutilizable (preview+playback+timeline+marcas) |
| wizard_extraer.py | pestaña Extraer metadata (wizard de 3 pasos sobre EditorMedios) |
| marcar.py | pestaña Marcar: master por fingerprint + carriles + guion + paquete |
| pipeline.py | orquestador DAG transaccional con resume |
| editorial_trims.py | RECORTES: heurística de huecos sin voz (+ actividad RMS por pista), documento `views/trims.json`, validación/merge de `trims.proposed.json` de la AI, paquete de revisión por bloque, unión de intervalos y segmentos conservados |
| podcast_export.py | exportación de bloques (plan) y de bloques recortados (`trim`/`atrim` + `concat` por script de filtros; sondea `-/filter_complex` vs `-filter_complex_script`) |
| medios.py | ffprobe/fingerprint/FLAC/waveform/frames/Job Object + reproductor del preview (§3.1) |
| hardware.py | config global (config.json) + detección CPU/GPU/hilos |
| audiocache.py / jobs.py / models.py | caché de audio, lock de jobs, unload de modelos |
| dialogs.py / presets.py | selectores nativos, presets |

### 3.1 El reproductor del preview (2026-07-20, diseño en three-brain-out/2026-07-20-reproductor-optimizacion/)

- **Playback = streaming**: `medios.VideoStream` — UN ffmpeg persistente por sesión de
  play (rawvideo RGB a dims exactas del letterbox, fps 30, deque acotada de 8 con
  backpressure). Warm-up A/V exige primer frame (tope 10 s; si falla se detiene).
  `playback_clock.AudioClock` sigue la salida de FFplay; `av_offset_s` ya no se usa.
  `SesionVideo` maneja atraso por reloj de arribo, respawn (cooldown 3 s, máx 4), degradación
  10fps/−25 %. El audio sigue aparte (`Reproductor`: mezcla ffmpeg → pipe → ffplay).
- **Scrub**: `FrameWorker` (frame exacto last-wins, max_w del letterbox, MJPEG q4,
  LANCZOS en pausa) + `Prefetcher` (UN ffmpeg a 0.5 fps por ventana [t−10, t+60] →
  caché LRU por bytes, 64 MB, hit instantáneo durante el drag).
- **Apagado**: TODO pasa por `wizard._stop_preview()` (epoch de sesión `_preview_epoch`
  invalida callbacks tardíos; mata stream+prefetch+timers+audio). Lo llaman play/stop,
  cambio de video, nav fuera del paso 1 y cierre.
- **Consola paso 1**: «✓ preview listo en Xs» (play/scrub NO esperan waveforms) ·
  «⏳ waveforms k/n · pista i: %» con barra por carril · «▶ … 1er frame Nms · audio
  spawn Nms» · avisos de respawn/degradación/fallo.
- **Arranque**: 6.0 s → 2.5 s en Linux. Los `available()` de análisis usan
  `find_spec` (NO importan torch/transformers/mediapipe); GPU de la GUI por
  `hardware._gpu_nvidia_smi()` (el chequeo real sigue en `resolve_device` al
  transcribir). Timings en stderr + `logs/arranque.log`.

### 3.2 La pestaña «Marcar» (2026-07-21/22, diseño en three-brain-out/2026-07-21-tab-marcar/)

Diseñada con Codex (5 rondas → READY) e implementada con review de 4 rondas → READY
(22 hallazgos aplicados). Piezas:

- **`editor_medios.py`** — el mini-editor del wizard EXTRAÍDO como componente (refactor
  A1 en dos etapas): `cargar/activar/desactivar/cerrar` + hooks (`controles_pista_extra`
  para nombre/rol del wizard, `overlay_preview` para los rects, `carriles_extra` para
  la metadata de Marcar, `on_video_cargado`, `on_playhead`). Cola+pump propios. Los
  cambios de MARCAS se observan por suscripción al Registro (sin hook propio).
- **Registro compartido** (`marcas.registro_compartido`) — UNA instancia por identidad
  de contenido `(size, hash_muestreado, inventario_sha256)`; wizard y Marcar abiertas a
  la vez jamás se pisan. Mutaciones TRANSACCIONALES (snapshot+rollback; `next_id` nunca
  retrocede), `notificar()` solo tras persistir. Segunda ruta del mismo contenido →
  `adjuntar_ruta` (rev mayor se ADOPTA persistido antes de notificar; misma rev con
  bytes distintos → conflicto VISIBLE con botón «Adoptar el de esta copia»).
- **`guion.py`** — `<video>.guion.md`: **JSON manda, el .md es vista** (decisión de
  Gabriel re-confirmada 2×). Slices con orden documental del usuario (nunca se
  reordena solo); bloques gestionados por sentinelas `<!-- g<ns>:marca:mNNNN sha=… -->`
  regenerados in situ; huérfanos → texto libre con nota (nunca se pierde texto);
  índice cronológico regenerado; gramática ESTRICTA (sentinela raro → SentinelasRotos
  + backup + «Reparar», jamás reparación silenciosa); persistencia dual
  sidecar/store con `guion_revision`; cambio externo por sha de AMBOS candidatos —
  el guard vive en `Documento.guardar()` (CambioExterno).
- **`marcar.py`** — descubrimiento del master: fingerprint fuerte (`fuentes.media`) →
  legacy (nombre+duración ±0.5 s) → picker; empates → selector modal. Saneo del
  master (copia ordenada, eventos inválidos descartados con conteo). Carriles con
  índice `starts`+`prefix_max_end` (bisect) y LOD (≤400 vectorial / coalescing por
  píxel); carril fantasma con los MK del master sin equivalente vivo; tooltips;
  click derecho «Crear marca desde este evento». El Text del guion protege los
  bloques con un PROXY Tcl (insert/delete/replace que tocan un rango gestionado se
  rechazan enteros; fail closed). Staleness por sha
  (`fuentes.marcas.sha256` vs sha del sidecar resuelto). «Preparar paquete» =
  guardar guion → `actualizar_derivados` en thread (bloqueado si marcas/guion en
  error); proyectos pre-2026-07-21 sin `spec.resuelto.json` → `SpecNoDisponible` y
  el guion queda declarado `paquete=stale_legacy` en su header (manda guion+sidecar;
  se limpia al lograr un derivados ok). Re-correr el wizard una vez lo moderniza.
- **El paquete para la IA** = `master.json` + `<video>.guion.md` (+ sidecar de
  marcas y `vistas/`).

## 4. Entornos

### Linux (desarrollo)
- `/mnt/data/transcriber`, venv `.venv/` (python 3.13). GPU RTX 3050 4GB + iGPU AMD.
- llama.cpp Vulkan en `/data/llama-vulkan/` (describir lo prefiere); build CPU en
  `/data/llama.cpp`.
- Correr: `./run.sh` o menú KDE. Crashes en `crash.log`.

### Windows (instalación administrada)
- Instalador: `setup-windows.bat` -> `setup-windows.ps1`. Separa `.bootstrap`,
  `releases/<semver>`, `runtimes/<hash>` y `shared/{models,cache,config,components,tools}`.
  Correr: `run.bat`; debug: `diagnostico.bat`.
- `launcher.py` consulta la última release estable de GitHub, y `updater.py` valida
  manifiesto, tamaño, SHA-256 y cada archivo antes de activar. La release nueva debe
  confirmar su arranque con un nonce; si falla, vuelve a la anterior.
- Un cambio de código reutiliza el runtime. Un cambio de dependencias crea otro runtime,
  pero jamás vuelve a descargar pesos presentes en `shared/cache` o `shared/models`.
- El runtime Windows usa torch/torchaudio 2.8.0 con CUDA 12.8 (índice cu128; se fijan
  juntos porque el alineador MMS usa `torchaudio.functional.forced_align`, retirado en
  versiones posteriores — `tools/build_release.py` es la fuente de verdad): soporta las RTX
  3050/4070 y Blackwell (RTX 5080).
- REGLA WINDOWS: los modelos torch corren en CPU (`hardware.use_gpu_torch()` = False
  en nt) — torch y CTranslate2 chocan por cuDNN si comparten GPU (crash nativo
  diagnosticado). Whisper sí usa GPU (carril CTranslate2 independiente). Flag
  experimental `gpu_torch_windows` en config.json.
- Todos los subprocess van con CREATE_NO_WINDOW + Job Object (`medios._popen` /
  `flags_subprocess` / `popen_gestionado`); os.replace con retry de PermissionError
  (locks de antivirus).
- Los antiguos ZIP manuales fueron retirados. El workflow vigente está en
  `.github/workflows/release.yml` y prueba una instalación Windows completa antes de publicar.

### Servicios online (OpenRouter + Alibaba)
- Keys en `config.json` (POR MÁQUINA — jamás en presets/spec/manifests/zip):
  `openrouter_api_key` (GUI: sección Visión) y `dashscope_api_key` (GUI: fila del
  modelo online, visible al elegir un modelo qwen). `describir.key_para(modelo)`
  resuelve la key según el proveedor (`prov` en MODELS_ONLINE); el pipeline la usa.
- Visión: `qwen/qwen3-vl-30b-a3b-instruct` vía OpenRouter. Determinismo bit-idéntico
  SOLO con provider pinning (deepinfra, allow_fallbacks=false) — toggle en la GUI.
  Caché VLM por chunk en `work/cache/vision/` — re-correr no re-paga.
- Audio (descripción online), DOS proveedores en `describir.MODELS_ONLINE`:
  · **OpenRouter**: Qwen-Omni NO existe ahí (verificado contra /api/v1/models
    2026-07-21: 47 modelos qwen, ninguno con input de audio). Equivalentes: nemotron
    free / gemini-2.5-flash-lite (default, ~$0.11 por 2 h) / gemini-2.5-flash
    (~$0.46) / gpt-audio-mini. Exige saldo ≥ $0.50 para audio.
  · **Alibaba Model Studio (dashscope)**: la familia Qwen-Omni POR API —
    qwen2.5-omni-7b (el mismo del carril local), qwen-omni-turbo, qwen3-omni-flash,
    qwen3.5-omni-flash/plus. Endpoint intl compatible-OpenAI; los omni EXIGEN
    stream=true → rama SSE de `_clip_online`: finish_reason=stop obligatorio
    (truncada/filtrada se RECHAZA), retry solo de streams incompletos, vigilante
    que hace shutdown del socket al cancelar (urllib3 v2: `raw._fp.fp.raw._sock`).
    Precios no publicados de forma estable → la GUI no estima USD.

## 5. Invariantes y gotchas (no romper)

- **Entorno pinneado**: transformers <5 (5.x rompe audeering), numpy 2.4.6,
  ctranslate2 4.8.1. El core whisper NO usa torch/transformers. emotiefflib se
  instala `--no-deps` (su opencv-python pisa el cv2 de mediapipe) + `pip install onnx`.
- **Duración inmutable** en gate.py: atenuar, nunca cortar.
- **Timestamps = autoridad**: los pone el pipeline (MMS / ventanas / offsets
  validados), nunca un modelo. Los eventos VLM devuelven offset_s RELATIVO validado.
- **Medir, no interpretar**: los módulos guardan hechos crudos; la interpretación es
  del orquestador (IA).
- **Codex CLI**: SIEMPRE por stdin y con modelo explícito:
  `cat prompt.md | codex exec --skip-git-repo-check -m gpt-5.6-sol` (sin -m cae a
  5.5; como argumento se cuelga). Reviews/diseños van a `three-brain-out/<fecha-tema>/`
  y el veredicto a `three-brain-out/log.md`.
- **Flujo de trabajo del proyecto**: features grandes se diseñan con Codex hasta
  READY, se implementan, y la implementación se re-revisa hasta READY. Los fixes se
  verifican con los repros exactos del reviewer.
- **Skill /clipear**: `~/.codex/skills/clipear` es un symlink a la de `.claude` —
  editar SOLO la de `.claude`; no volver a convertirla en copia.
- **Guion «JSON manda»**: el sidecar de marcas es la ÚNICA fuente de verdad; el
  .md es vista regenerada; los prompts se editan solo desde la UI del timeline.
  No proponer sincronización bidireccional .md→JSON (decisión de Gabriel, 2×).
- **Registro de marcas COMPARTIDO**: siempre `marcas.registro_compartido()` —
  jamás instanciar `Registro` directo desde una vista nueva.
- **Paths = localizadores**: la identidad de un medio SIEMPRE es el fingerprint
  (size+hash_muestreado+inventario). Los videos de Gabriel se MUEVEN de carpeta
  después de extraer (Windows/OneDrive) — toda feature nueva debe tolerar eso.
- **config.json es por máquina** (hardware, calibración de mirada, API keys) — no se
  copia entre PCs ni va en el ship zip. `av_offset_s` antiguo se ignora.
- **Reproductor del preview** (§3.1): NO volver al esquema de un ffmpeg por frame;
  todo apagado pasa por `wizard._stop_preview()`; los `available()` de análisis usan
  find_spec y NO deben importar torch/transformers (arranque de la GUI).
- **Código vivo**: la GUI no recarga .py — reiniciar la app tras editar.

## 6. Cómo verificar (headless)

En Windows (instalación administrada): `runtimes\win-py313-*\Scripts\python.exe -B -m unittest discover -s tests`, con
`shared\tools\ffmpeg` en el PATH para que corran los tests de exportación (54 tests el
2026-09-06). En Linux:

```bash
cd /mnt/data/transcriber
.venv/bin/python -m py_compile app.py pipeline.py vision.py marcas.py medios.py \
    consolidar.py vistas.py wizard_extraer.py describir.py \
    editor_medios.py marcar.py guion.py
# smoke del pipeline con video sintético: ver los tests de las sesiones en el
# historial; espeak-ng genera voz para whisper, ffmpeg lavfi genera video.
# GUI: hay display en esta máquina — instanciar App() y update() funciona.
```

CLIs útiles: `python vision.py motion <video>` (scan+plan sin red),
`python describir.py <wav> --smoke --online [MODELO]` (valida la API de audio del
proveedor del modelo: OpenRouter o Alibaba), `python marcas.py <video>` (inspección
de marcas), `python vistas.py generar|dossier <master>`,
`python edl.py validar|render|cutview`.

## 7. Estado actual y pendientes

La especificación antigua decía 3–4 bloques de hasta 70 minutos. Se corrigió a
la regla que ya ejecutaban el validador y la skill: número variable, máximo 50
minutos, objetivo hasta 45. No se cambió el algoritmo para esa corrección documental.

Las capas manuales están disponibles antes de inferir mediante un contexto de
medio que nunca se publica como master. Al aparecer la metadata real se adoptan
por fingerprint. Las eliminaciones de items y descendientes quedan protegidas
frente a respuestas AI posteriores, además de las correcciones y capas borradas.

### Reproductor — objetivo E

Reloj de salida de FFplay, warm-up sin audio prematuro, preview 30 fps/tick 16 ms,
cachés exactas acotadas y waveform persistente por contenido. VOD real de casi tres
horas en Windows, arranques 0/30/90 min: error mediano 1,19–1,44 s → 20–24 ms,
P95 absoluto ≤35 ms. Saltos repetidos ~1.780 → 10–11 ms; zoom 25,8 → 3,9 ms.
Medición, límites y comandos: [mediciones-reproductor.md](mediciones-reproductor.md).
73 unittest y smoke App/Tk; FFplay real en prueba de protocolo, dispositivo real
en benchmark. No se promete latencia constante para seeks HEVC nuevos.

### Descubrimiento — objetivo D

`editorial_catalog.py` escanea masters (omite runtimes, releases, tracks y enlaces),
cachea fingerprint/digest por tamaño+mtime y revalida el candidato antes de abrir.
Catálogo relativo en `.transcriptor/catalog.json`; reconstrucción tras borrado o
corrupción, deduplicación de copias equivalentes, selector ante versiones distintas.
No migra ni reescribe masters, marcas o recortes existentes. Los hijos nuevos se
descubren en `podcast-*/projects/`. La UI descarta resultados de otro medio mediante
generación y evita ejecutar inferencia sobre un master derivado. 69 tests y smoke
de importación automática con App/Tk en Windows.

### Temas — objetivo C

`editorial_topics.py`: petición con ámbito, ID de ciclo y digests de master/capas.
Primera pasada ajustada ≤1,5 s contra palabras y risas; segunda ligada al digest del
mapa y con cobertura exacta de sus IDs fuente. Recurrencias en un item multirrango,
jerarquía validada, publicación como capa; correcciones humanas preservadas.
`topics.proposed.json` detectado por el poll de la UI. 66 tests, incluidos orden de
pasadas, recurrencia, respuestas incompletas/obsoletas y tiempos no finitos.

### Capas — objetivo B

`editorial_layers.py` define `editorial-layer/1`, almacenamiento con revisión,
conflicto externo y tumbas para borrados. Adaptadores derivados para marcas,
bloques y recortes. `editorial_layers_ui.py` centraliza gestos, menús, tooltip,
editor de rangos/comentarios y administrador de capas. Se retiraron los gestos
duplicados del antiguo carril de recortes de Automático. El editor Manual mantiene
su carril de marcas usando el mismo Registro. Panel de Automático desplazable.
Validación: 62 tests y smoke App/Tk con gestos, persistencia y eliminación de capa,
pedidos del autor y creación de recorte mediante el adaptador.

### Proyectos modulares — 2026-09-06, objetivo A

`editorial_projects.py` deriva sub-masters por intersección con segmentos conservados,
divide eventos y reconstruye texto/referencias sin palabras eliminadas. Conserva
arousal, valencia/dominancia y valores de intensidad con baselines del padre.
Exportación atómica del video junto a `projects/<ordinal>/editorial/`. Audio para RMS
del hijo extraído con manifest y hash, sin inferencia. Ver diseño modular.
Validación: 57 tests (FFmpeg real, dos pistas, recortes, nieto, checkpoint) y App/Tk
real en Windows: importación, metadata, salto y zoom, inspección visual del preview.
El smoke detectó que el arranque sí importaba modelos: `gate.vad_available` importaba
Silero y las sondas de Ajustes cargaban torch/CT2. Ahora se comprueba presencia sin
importarlos; el chequeo del backend ocurre al procesar o redetectar explícitamente.

### 7.0b Sesión 2026-09-06 (2ª) — Recortes: silencios, carril interactivo, AI y corte

Pedido de Gabriel: que el análisis de «dónde no hay voz» NO edite nada de inmediato, sino
que proponga tramos «de aquí a aquí» visibles en el timeline (sistema de capas/selección
existente), ajustables con el mouse; que la AI haga una segunda pasada editorial sobre el
bloque ya acortado; y que solo un botón «cortar» aplique todo. Implementado y verificado
con tests (54 OK, incluida exportación real con FFmpeg) y smoke de la UI real con gestos
simulados:

- **`editorial_trims.py`** (nuevo): `analyze_silences(master, audio_paths, params)` une
  palabras (colchón 0.10/0.20 s) y risas (0.25 s) de TODAS las pistas, saca huecos ≥
  `min_gap` (default 1.0 s, incluidos aire inicial/final), conserva `keep_before/after`
  (0.3 s) y mide la ACTIVIDAD RMS del hueco por pista (`medios.envolvente` sobre
  `tracks/<id>/audio.flac`, buckets de 50 ms, piso = percentil 20): huecos con más de 12 dB
  sobre el piso quedan propuestos pero DESACTIVADOS (confianza 0.4). Documento
  `views/trims.json` (`editorial-trims/1`, identidad = fingerprint del medio, no digest del
  master): recortes con `origin` silence/ai/user, `enabled`, `edited`, `reason`,
  `evidence`. `apply_silence_analysis` reemplaza solo los de silencio NO editados y
  conserva ids + estado activado de los huecos idénticos. `enabled_intervals` = UNIÓN de
  los activos (se permiten solapes); `kept_segments` = complemento dentro de un bloque
  (restos < 0.1 s se funden). `BoundaryIndex` (bisect) avisa bordes dentro de
  palabra/risa. Propuesta de la AI `views/trims.proposed.json`
  (`editorial-trims-proposal/1`, exige `source_master_digest`): `validate_proposal` ajusta
  cada borde hasta 1.5 s con `editorial_chunks.snap_boundary` (público, con intervalos
  precalculados), valida `chunk_id`/utterances, avisa sin motivo; `merge_proposal`
  reemplaza los recortes de la AI no editados (idempotente por digest). Paquete de revisión:
  `views/trim-agent-request.md` + `chunks/<id>/trim-review.md` (o `views/trim-review.md`
  sin plan) = conversación del bloque con `⟂ RECORTE` intercalados + tema/resumen/
  topics/subtopics del chunk.
- **`podcast_export.export_plan(..., trims=)`**: por bloque calcula los segmentos
  conservados y arma un script `split`/`trim`/`setpts=PTS-S/TB` (NO `STARTPTS`: una pista
  con offset OBS conserva su desfase en el primer segmento) + `concat` (rellena con
  silencio el redondeo A/V por segmento → sin deriva). Script por archivo (límite de
  32k de la línea de comandos en Windows); `filter_script_option()` sondea
  `-/filter_complex` (FFmpeg ≥ 7; el build 2026 ya no tiene `-filter_complex_script`).
  `-t` de ENTRADA limita la lectura al bloque. `document=None` → un bloque con todo el
  medio (`editorial_trims.whole_plan`). Carpeta `podcast-<digest(plan+recortes)>` con
  `accepted-trims.json`; tolerancia de duración = 2 frames + 1 frame por segmento.
- **`editor_medios.py`**: hooks nuevos y genéricos — un carril extra puede declarar
  `gesto(fase, e, g, y0)` (press/motion/release/doble; si `press` devuelve True captura el
  botón izquierdo y no mueve el playhead) y el dueño `teclas_extra(e)` se consulta antes
  que las marcas; `_carril_en(y)`; tecla Escape bindeada.
- **`automatico_ui.py`**: carril «recortes» (26 px, debajo del de bloques): azul =
  silencio, violeta = AI, naranja = usuario; desactivado = contorno punteado; seleccionado
  = borde blanco + handles; borde rojo = cae dentro de palabra/risa; proyección rayada
  sobre las pistas; LOD por píxel a partir de 400 recortes visibles. Gestos: click
  selecciona, arrastrar mueve, bordes del seleccionado estiran, arrastre en vacío crea
  (naranja), doble click y `X` activan/desactivan, `Supr` borra, `Esc` deselecciona,
  hover = tooltip, click derecho = menú (activar, borrar, ir al inicio/final, escuchar
  desde 2 s antes, crear 1 s). Cada mutación persiste `trims.json` (atómico). Panel
  RECORTES: «Analizar silencios» (mín/margen), estado, «Preparar revisión AI», «✂ Cortar
  y exportar», «Saltar recortes al reproducir» (re-arranca la sesión al final del recorte
  activo; ayuda de revisión, no el render). `Importar JSON de la AI` enruta por `schema`;
  el poll de 2 s también detecta `trims.proposed.json`. Los recortes se cargan al terminar
  la corrida o al abrir proyecto (hilo silencioso: valida identidad + arma el índice).
- **Skill `skills/transcriptor/SKILL.md`**: Tarea 1 (bloques; ahora pide `topics`/
  `subtopics`) y Tarea 2 (recortes de contenido): NO proponer recortes por humor subido de
  tono, lisuras, términos discriminatorios ni comentarios ofensivos (post lo quita);
  criterio = aporte a la conversación (tangentes, balbuceo, arranques en falso, charla
  técnica); conservar diversión, continuidad y setups; ante la duda no recortar.
- Tests nuevos `tests/test_trims.py` (13): heurística, actividad, reanálisis, documento,
  unión/segmentos, propuesta (snap, rechazos, merge idempotente), paquete de revisión,
  script de filtros y exportación real (recortes frame-accurate, 2 pistas con offset,
  recorte que cruza la frontera de bloques). Para correrlos en esta PC hay que poner
  `shared\tools\ffmpeg` en el PATH (fuera de la release, `app_paths.TOOLS_DIR` apunta a
  `tools/`; sin ffmpeg esos tests se saltan).

Pendiente: probar con el VOD real (calibrar `min_gap`/margen y el umbral de actividad de
12 dB con audio de verdad); Linux con ffmpeg < 7 usa `-filter_complex_script` (sondeo, no
verificado allí todavía).

### 7.0 Sesión 2026-09-06 — instalación Windows real (release 0.2.0)

Hecho en la PC de Gabriel (RTX 4070 Laptop 8 GB, 32 hilos, 63 GB RAM), verificado con la
app instalada (`run.bat`) y con `tests/` (41 OK):

- **Arranque roto** («la release instalada no coincide con su manifiesto (sobran:
  `__pycache__/...`)»): el launcher importaba `release_state`/`updater` y Python escribía
  bytecode dentro de `releases/0.2.0`, y la verificación exacta lo rechazaba. Fix doble:
  `bootstrap.py` lanza el launcher con `-B` + `PYTHONDONTWRITEBYTECODE`, y
  `updater.verify_installed_release` ignora `__pycache__`/`.pyc` al arrancar
  (`strict=False`); al instalar sigue estricto (`strict=True`) → repara y deja la release limpia.
- **MMS fallaba** («'NoneType' object has no attribute 'write'»): con `pythonw` no hay
  consola y `sys.stdout/stderr` son `None`; tqdm (descarga de torch.hub) muere al escribir.
  `app._ensure_std_streams()` redirige ambos a `shared/logs/salida.log` (rota a 5 MB,
  `TQDM_MININTERVAL=5`). El modelo MMS ya quedó en `shared/cache/torch`.
- **Reanudar repetía Whisper**: el paso `transcribe_X` (Whisper+MMS) era atómico; si MMS
  fallaba se descartaba todo. Ahora `whisper_X` publica `tracks/X/words.whisper.json` +
  `utterances.whisper.json` en cuanto termina y `align_X` (MMS o copia si está desmarcado)
  produce `words.aligned.json` + `utterances.json`. `core.align_transcription()` alinea
  in place (recién transcrito o cargado de JSON). Los manifests viejos `transcribe_X` se
  adoptan como `align_X` (`_StepStore.peek(legacy_ids=…)`, misma clave) sin repetir nada.
- **Aviso al reanudar** (`automatico_ui.ResumeDialog`): si la carpeta tiene manifests
  (`editorial_pipeline.completed_work`), pregunta «Retomar con lo ya hecho» /
  «Empezar de cero (reescribir)» (`spec.rebuild=True`) / Cancelar.
- **Modelo por defecto `large-v3-turbo`**: `config.whisper_model` (Ajustes → bloque
  Whisper; `hardware.whisper_model()`), lo siguen Transcribir, Automático y el wizard.
  `hardware_whisper_suggestion()` conserva la heurística por hardware solo como sugerencia.
- **Pasos marcables en Automático** (`spec.steps`: `align`, `laughter`, `prosody`; todos
  activos por defecto): casillas en el panel «Pipeline editorial» + selector de modelo.
  Un paso desmarcado se emite como `skipped` («OMITIDO»); el master tolera pistas sin
  risa/arousal/intensidad (`editorial_master._optional_json`) y usa `words.aligned.json`
  cuando no hay prosodia. CLI: `--no-align/--no-laughter/--no-prosody`, `--model` opcional.
- `actualizar-release.bat`: reconstruye + reinstala la release desde el código fuente sin
  descargar nada (mismos pasos 3-4 de `setup-windows.ps1`). Cerrar la app antes.
- `.gitignore`: `state/` (instalación administrada).

Pendiente de esta sesión: primera corrida editorial completa con el VOD real (Whisper
correrá una vez más porque las corridas fallidas no alcanzaron a publicar; después todo se
reutiliza); publicar tag `v0.2.1` para que el auto-update lo distribuya.

### 7.1 Estado previo (2026-07-22)

FUNCIONA de punta a punta: wizard -> pipeline (voz+fondo+cara+marcas+visión) ->
master v2 -> vistas -> tab Marcar (metadata visible + marcas + guion + paquete) ->
skill /clipear -> EDL -> render. Verificado headless + GUI + API real. Reviews de
Codex: READY en todos los frentes (último: tab Marcar, 4 rondas, 2026-07-22).

Pendientes:
- **Detector de risas**: DEMASIADOS falsos positivos con contenido real (reporte de
  Gabriel 2026-07-22). Plan acordado a proponer: filtro por `conf` mínima
  configurable (carril + vistas + clipear) antes de tocar el modelo.
- Proyectos extraídos ANTES del 2026-07-21 no tienen `spec.resuelto.json`:
  «Preparar paquete» los declara `stale_legacy` (guion+sidecar mandan). Re-correr
  el wizard UNA vez (mismo outdir `metadata/` — viaja con el video) los moderniza
  reusando lo pesado.
- Primer VOD REAL con visión activada (prompts v2 listos; iterar con contenido real).
- Saldo OpenRouter >= $0.50 para validar el audio online (`--smoke --online`); para
  los qwen por API, crear la key de Alibaba Model Studio y smoke con
  `--smoke --online qwen2.5-omni-7b` (la lista dashscope de MODELS_ONLINE no está
  validada contra la cuenta real — IDs del catálogo intl 2026-07).
- /clipear real de punta a punta con el protocolo por capas + marcas + visión.
- Smoke en la PC Windows real:
  · Requisito previo: instalar pillow (paso 1 del SHIP-README — la PC no lo tenía;
    síntoma: todo negro + ModuleNotFoundError 'PIL').
  · Reproductor con el video de 2 h: primer frame <2 s, desfase A/V (calibrar
    `av_offset_s`), scrub con prefetch, sin ffmpeg huérfanos tras stop/cierre,
    timings en logs/arranque.log.
  · Tab Marcar con el proyecto real (Gabriel ya la usó el 2026-07-22: carga,
    carriles y guion OK; «Preparar paquete» en proyecto legacy → stale_legacy
    esperado).
  · Lo ya pendiente: rueda del mouse, DPI del carril de marcas, ffplay BtbN,
    Job Object.
- Diseño "operación por agentes + despacho editorial" (three-brain-out/
  2026-07-16-agente-app/DISENO-final.md): aprobado, NO implementado.
- Diferidos declarados: batching AST/emoción/respiros (HALLAZGOS-futuros.md),
  semillas del índice desde eventos VLM, torch-GPU en Windows junto a whisper.
