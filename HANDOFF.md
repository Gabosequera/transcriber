# HANDOFF — Transcriptor / pipeline de metadata para auto-clipping

> **Actualización 2026-08-29:** el modo Automático ahora implementa el pipeline
> editorial multipista de Fase 1 en `editorial_pipeline.py`. Este documento describe
> principalmente el pipeline multimodal Manual heredado. La especificación vigente del
> flujo nuevo está en `CAMBIO-INTERFAZ-Y-DISTRIBUCION.md`. El chunking automático usa
> `codex_chunker.py` + `codex exec` para leer la conversación completa; no se rige por
> los comandos históricos de Codex documentados más abajo para reviews de desarrollo.

Referencia técnica del ESTADO ACTUAL para que otro agente continúe.
Última reescritura completa: 2026-07-20; última actualización: 2026-07-22 (pestaña
«Marcar»: revisión post-extracción + guion para la IA). El historial cronológico
(qué se hizo, cuándo y por qué) vive en `AUTOCLIP_HANDOFF.md`. Originales
pre-reescritura en `docs-archivo/`.

---

## 1. Qué es

App de escritorio (Python 3.13 + customtkinter) que Gabriel (canal "El Grafo") usa para
convertir sus grabaciones largas (gameplay/reacción, OBS multitrack) en metadata rica
que una IA ("Ava", vía la skill /clipear) usa para cortar clips de TikTok/Shorts sola.

Desarrollada en Linux (`/mnt/data/transcriber`), corre también en una PC Windows como
carpeta PORTABLE. Cinco pestañas:

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
| medios.py | ffprobe/fingerprint/FLAC/waveform/frames/Job Object + reproductor del preview (§3.1) |
| hardware.py | config global (config.json) + detección CPU/GPU/hilos |
| audiocache.py / jobs.py / models.py | caché de audio, lock de jobs, unload de modelos |
| dialogs.py / presets.py | selectores nativos, presets |

### 3.1 El reproductor del preview (2026-07-20, diseño en three-brain-out/2026-07-20-reproductor-optimizacion/)

- **Playback = streaming**: `medios.VideoStream` — UN ffmpeg persistente por sesión de
  play (rawvideo RGB a dims exactas del letterbox, fps 15, deque acotada de 8 con
  backpressure). `SesionVideo` = política: warm-up A/V (el audio arranca al primer
  frame o a los 2 s; `av_offset_s` de config.json retrasa el reloj del video, default
  0.25), atraso por reloj de arribo, respawn (cooldown 3 s, máx 4), degradación
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
- torch quedó en 2.6.0+cu124 (el pin 2.13 no existe en cu124).
- REGLA WINDOWS: los modelos torch corren en CPU (`hardware.use_gpu_torch()` = False
  en nt) — torch y CTranslate2 chocan por cuDNN si comparten GPU (crash nativo
  diagnosticado). Whisper sí usa GPU (carril CTranslate2 independiente). Flag
  experimental `gpu_torch_windows` en config.json.
- Todos los subprocess van con CREATE_NO_WINDOW + Job Object (`medios._popen` /
  `flags_subprocess` / `popen_gestionado`); os.replace con retry de PermissionError
  (locks de antivirus).
- El ZIP manual `ship-windows-*` y `SHIP-README.txt` son históricos. No deben usarse
  para releases nuevas; el workflow vigente está en `.github/workflows/release.yml`.

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
- **config.json es por máquina** (hardware, calibración de mirada, API keys,
  `av_offset_s` del preview) — no se copia entre PCs ni va en el ship zip.
- **Reproductor del preview** (§3.1): NO volver al esquema de un ffmpeg por frame;
  todo apagado pasa por `wizard._stop_preview()`; los `available()` de análisis usan
  find_spec y NO deben importar torch/transformers (arranque de la GUI).
- **Código vivo**: la GUI no recarga .py — reiniciar la app tras editar.

## 6. Cómo verificar (headless)

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
