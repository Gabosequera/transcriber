# Historial del proyecto (timeline)

> Antes `AUTOCLIP_HANDOFF.md`.

Cronología de cómo se construyó el proyecto (auto-clipping primero, perfil editorial de
podcasts después), del período más viejo (arriba) al más nuevo (abajo). La referencia
técnica del estado ACTUAL está en [referencia-tecnica.md](referencia-tecnica.md).
Los diseños y reviews (`three-brain-out/<fecha-tema>/`) y los originales pre-reescritura (`docs-archivo/`) viven en el checkout Linux de desarrollo; están en `.gitignore` y no forman parte del repositorio ni de la release.

La visión: Gabriel graba VODs largos (gameplay/reacción, OBS multitrack, canal
"El Grafo"); la app extrae TODA la metadata (voz, audio del juego, cara, pantalla,
órdenes del autor) a un master.json alineado a una sola línea de tiempo, y una IA
("Ava", skill /clipear) decide y corta los clips sola. Prioridades permanentes:
timestamps correctos y no degradar calidad.

---

## 2026-07-10 — Origen: transcriptor + limpiador de respiraciones

La app nace como transcriptor (faster-whisper GPU/CPU) + limpiador de respiraciones
(gate que atenúa huecos sin voz SIN cambiar la duración).

- Crash nativo del modo VAD diagnosticado y resuelto: el preload de libs CUDA cargaba
  libnvblas, que secuestraba el BLAS de CPU de torch (SIGSEGV). Fix: excluir nvblas.
- Modo Quirúrgico: red Respiro-en (detección por trozos de 15s con solape — una sola
  pasada perdía respiraciones) + escudo de palabras con encogimiento de bordes + red
  de seguridad espectral (DSP, solo en huecos: RMS alto Y planitud alta = aire).
- Alineador forzado MMS (align.py, torchaudio): whisper transcribe bien pero sus
  tiempos de palabra son flojos; MMS los corrige a ~50 ms. Es la base de todo el
  pipeline posterior.
- Primera pestaña Metadata: arousal del audio (audeering) + valence del texto
  (pysentimiento), z-scoreado contra el baseline del propio video.

---

## 2026-07-12 — Giro al auto-clipping: la voz y el audio del juego

Decisión de rumbo: la app evoluciona a pipeline de metadata para auto-clipping.
Principio de diseño que quedó fijo: cada modalidad emite eventos `{t_ini, t_fin, ...}`
en la misma línea de tiempo; los módulos MIDEN, la interpretación es del orquestador.

- Risa (laughter.py, omine/LaughterSegmentation): timeline global de probabilidad
  (max-pool de ventanas) para timestamps absolutos. Consenso con Codex: no
  super-chunkear.
- Pausas (extraer_pausas.py): huecos entre palabras del words.json alineado, como
  hechos crudos (se descartó clasificarlas — el orquestador clasifica mejor).
- Bloque audio-del-juego (pista separada de OBS):
  - Capa 0: whisper del juego con anti-alucinación (fondo.py).
  - Capa 1: tags AST (18 familias, ventana 10s), Capa 1.5: transitorios por onsets
    (~10 ms), Capa 2: escenas por cambio de carácter (escena_audio.py).
  - Capa 3: descripción por escena con LALM Qwen-Omni vía llama.cpp/GGUF
    (describir.py) — elegido para no tocar los pins de torch/transformers. Registry
    hot-switch: qwen2.5-omni-3b / 7b / qwen3-omni-30b. Chunking por escena con
    solape intra-escena y contexto rodante; el modelo nunca pone timestamps.
- Prompt del LALM iterado: describir, no interpretar ni transcribir; fidelidad sobre
  detalle (el 3B inventaba).

---

## 2026-07-13 — master.json, skill /clipear, GPU y endurecimiento Windows

Tres frentes en paralelo: consolidación de la metadata, la infraestructura del agente
editor, y preparar la app para la PC Windows.

- consolidar.py v1 + pestaña Master: streams namespaced en un master.json único.
- Primer VOD real consolidado (25 min, 9 streams): las señales funcionan (risas,
  arousal en el gol, pausa larga -> reacción).
- Skill /clipear creada y endurecida con 5 rondas de Codex: diccionario completo del
  master, reglas duras (timestamps = autoridad, no cortar palabras), señales,
  bordes por pausas. Herramienta cortar_clip.py (ffmpeg frame-accurate).
- Flujo DaVinci: OBS HEVC no entra en Resolve Linux — transcodificar a DNxHR+PCM.
- GPU del LALM vía Vulkan (sin CUDA toolkit): benchmark real — ganador
  encoder-en-iGPU + LLM-en-CPU-16-hilos en esta laptop. torch pasó a usar todos los
  hilos lógicos.
- Cancelación del pipeline de fondo (watcher que mata llama-server) + guardado por
  capa (crash-safe) + reuso de escenas ya generadas.
- ENDURECIMIENTO WINDOWS (Fases 0-3, con Codex): hardware.py (config global +
  detección CPU/GPU multi-backend + pestaña Ajustes), fallback OOM->CPU en todos los
  módulos torch, audiocache/jobs/models (recursos), portabilidad total del código
  (paths, DLLs, run.bat), instalador automático setup-windows.bat/ps1 (uv + python
  portable + torch CUDA 12.8 + ffmpeg + llama vulkan), requirements-windows.txt.
- En la PC Windows real: instalación OK; crash del alineador diagnosticado =
  conflicto de cuDNN entre CTranslate2 y torch -> REGLA: torch en CPU en Windows
  (whisper sigue en GPU). Fixes de la prueba real: cancelar no pisa datos buenos;
  _gpu_plan elige la GPU dedicada si el modelo entra entero, con escalera de
  degradación ante OOM real.

---

## 2026-07-14 — Cara v1 y el primer clip real (el gusto editorial)

- Skill /clipear GLOBAL (~/.claude/skills/) + copia para el CLI de Codex
  (~/.codex/skills/). El hard link original se rompía al editar; hoy es espejo por
  rsync. Herramienta compartida en /mnt/data/productions/tools/.
- PRIMER CLIP REAL con la skill (Opus): técnicamente bien, editorialmente flojo
  (16s directo al gol). El feedback se ABSTRAJO a principios en la skill:
  1) la acción del juego no es el contenido — Gabriel es el contenido;
  2) el payoff necesita build-up; 3) condensar != vaciar; 4) duración típica 25-60s.
  Este loop (clip -> feedback -> abstraer -> skill) es el mecanismo de mejora.
- MODALIDAD CARA v1 (cara.py, MediaPipe Face Landmarker — runtime tflite propio, no
  toca torch/cuDNN): detección multi-escala de la facecam, 10 canales de músculos,
  esquema cara-v1 consensuado con Codex (eventos por PICO con prominencia, episodios
  con secuencia de ápices, baseline mediana/MAD). Streams al master:
  video.cara.reaccion/mirada/presencia + sidecars detalle/facecam.
- Verificado sobre el VOD real: el gol capturado de manual (episodio con arco
  completo de ápices). Fix con datos: mirada RELATIVA a la pose neutral del video
  (los umbrales absolutos daban falsos).
- Descartados: DeepFace (precisión real baja + arrastra TensorFlow), LibreFace,
  pose corporal en v1. VLM de pantalla: se decidió ir por API de nube (después
  fue vision.py).

---

## 2026-07-16 — La sesión grande: cara v2, Ava, orquestador, wizard unificado

### Cara v2: emociones (EmotiEffLib)
- enet_b0_8_va_mtl ONNX/CPU (sin torch): valence + arousal + 8 clases AffectNet
  sobre el bbox que MediaPipe ya calcula. 3 canales nuevos (valencia_pos,
  valencia_neg, excitacion) -> stream video.cara.emocion.
- Instalación con gotcha: `pip install --no-deps emotiefflib` + `onnx` (su
  opencv-python pisa el cv2 de mediapipe).
- Review Codex 2 rondas -> READY (puente de flickers, z_on por canal, etiqueta del
  extremo crudo, fallos por muestra tolerados). La cara de reposo del usuario lee
  Sadness — por eso las clases solo etiquetan picos; el baseline dimensional del
  propio video absorbe el sesgo. El gol: evento #1 del video (z 8.6, Happiness .99).

### Instrucciones habladas a "Ava" (ava.py)
- Decir "hey Ava, corta esto" al grabar -> eventos voz.instrucciones (heurística
  pura sobre words.json; variantes ava/eva/aba/eba; falsos positivos se describen,
  no se filtran). El evento es el TIMESTAMP; el texto es conveniencia.
- 2 rondas de Codex -> READY. En la skill: máxima prioridad editorial; el tramo
  hablado se EXCLUYE de los clips.

### Calibración de mirada (sesión en vivo con Gabriel)
- Guía visual en --live, zona "a cámara" como rectángulo editable con el mouse,
  reconfirmación OCULAR por iris (mirada combinada cabeza+ojos dentro del recuadro,
  con mitigación de parpadeo y ganancias verticales calibradas), calibración de
  centro con tecla c, PRESETS en config.json (el preset "main" del usuario quedó
  activo). config.json es por máquina: recalibrar en Windows.

### El orquestador: master v2 + vistas + EDL (diseño + implementación)
- Diseño 2 rondas con Codex (three-brain-out/2026-07-16-orquestador-metadata/):
  transcript-first en dos lecturas con checkpoint sellado, índice de semillas con
  cuotas, dossiers por demanda, EDL con gate mecánico y junction cards.
- Implementado: consolidar.py v2 (header global + IDs estables + duración ffprobe),
  vistas.py (bundle determinista con provenance), edl.py (validar/render/cutview),
  skill /clipear reescrita al protocolo por capas + formatos/cortos|largo.md.
- Review de implementación: rondas 3-5 de Codex (12+5+2 hallazgos, todos aplicados,
  con los repros exactos) -> CERRADO. Operativo de punta a punta.

### Diseño (NO implementado): operación por agentes + despacho editorial
- Cómo cualquier agente opera la app: operations.py headless + supervisor +
  worker persistente + transcriptorctl + MCP como adaptador fino; pedidos
  editoriales con snapshot del formato. Consenso 2 rondas.
  Spec: three-brain-out/2026-07-16-agente-app/DISENO-final.md.

### Pestaña unificada "Extraer metadata" (wizard)
- Fusiona Metadata + Audio de fondo + Cara + Master en un wizard de 3 pasos; la app
  queda en 4 tabs. Módulos nuevos: pipeline.py (DAG transaccional con manifests,
  resume, kill-safe), medios.py (ffprobe/fingerprint/FLAC a timeline canónica/
  waveform/Reproductor con mute-solo real/Job Object Windows), wizard_extraer.py.
- Diseño 2 rondas + review de implementación 4 rondas -> READY (15 hallazgos
  aplicados). Spec: three-brain-out/2026-07-16-tab-unificada/.
- Mismo día, feedback de Gabriel: el paso 1 se rediseñó como MINI-EDITOR — un solo
  timeline (ruler + carril por pista + playhead compartido), navegación por teclado,
  re-mezcla debounced, sustracción de regiones (rects.juego.menos), waveform con
  picos min/max + cuerpo RMS + ganancia visual. Reviews -> READY.
- Fix GUI: la pestaña Cara aparecía en blanco (reflow con listas hardcodeadas ->
  ahora itera _tabrefs).

---

## 2026-07-17 — Marcas del autor, navegación DaVinci, rendimiento, visión

### Marcas del autor + navegación + rendimiento del editor
- Diseño 3 rondas -> READY; implementación 4 rondas -> READY
  (three-brain-out/2026-07-16-timeline-marcas/).
- marcas.py (nuevo): puntos y regiones con PROMPTS para la IA + decisiones
  incluir/excluir desde el mini-editor, ANTES de extraer metadata. Sidecar
  `<video>.marcas.json` con revisión + store fallback + cuarentena estricta.
  IDs canónicos m#### -> MK#### en el master (sin renumerar). En el pipeline es
  ENTRADA EXTERNA (snapshot congelada): editar marcas re-hace solo
  consolidar+vistas. Vista nueva directivas.md (ledger marcas+Ava).
- Navegación estilo DaVinci: viewport con zoom (+/-/Ctrl+rueda), pan, Shift+Z fit,
  ruler adaptativo, teclas M/I/O/X/Supr, panel no modal de edición.
- Rendimiento (el editor se colgaba): waveform blitteada (1 imagen por carril vs
  ~10k items de canvas), envolvente NumPy (~15x), tiles de zoom bajo demanda,
  FrameWorker único con MJPEG escalado (~10x el scrub), frames durante playback
  (2fps adaptativo), debounce de resize, cancelación real de ffmpeg.
- AUDITORÍA WINDOWS PRE-SHIP (Claude + Codex, 2 rondas -> READY-TO-SHIP): consolas
  que flasheaban (CREATE_NO_WINDOW en consolidar/cara), Job Object en cara --live,
  retries de os.replace ante locks de antivirus, PYTHONUTF8 en los .bat. Se armó el
  ship zip de solo código (ship-windows-2026-07-17.zip + SHIP-README.txt).

### Visión del video por VLM (vision.py)
- Pedido: interpretar el VIDEO como se interpreta el audio, con un algoritmo
  matemático rápido del cambio de píxeles para muestrear adaptativo (quieto = 1
  frame por tramo; movido = más frames), chunks correlacionados sin mezclas, JSON
  determinista, GUI para JUEGO y/o CÁMARA.
- Diseño 2 rondas -> READY (three-brain-out/2026-07-17-vision-video/), con
  evidencia empírica previa: json_schema strict funciona; sin provider pinning
  OpenRouter enruta a providers distintos y el output cambia; con pinning
  (deepinfra) = bit-idéntico. Prototipo del atlas: 81x realtime.
- vision.py: motion_scan (1 decode, atlas gris por lane, máscara de la cámara en
  negro sobre el juego, hard cuts locales) + plan_chunks (función pura, actividad
  normalizada con piso absoluto, valles, cuantiles) + analizar_lane (productor de
  JPEG + 3 workers, echo de chunk_id/input_id validado, caché por chunk).
- Pipeline: pasos motion + vision_juego/camara, estado nuevo PARTIAL. Master:
  streams video.juego.vlm / video.camara.vlm. Vistas: renderer "Contexto visual".
- API key de OpenRouter en config.json (hardware._DEFAULTS); requests pasó a
  dependencia directa.

---

## 2026-07-20 — Cierre de visión, audio online, prompts v2

### Reviews finales de visión
- Implementación: 3 rondas de Codex -> READY (16 hallazgos aplicados y verificados
  con sus repros: scan cancelado no se publica, workers sin deadlock, cache key con
  tramo completo e identidad completa, salt por run con nonce, validación local
  estricta sin asserts, hard cuts respetan MIN, AbortLane siempre aborta).
- Densidad de frames (objeción de Gabriel): la fórmula vieja nunca alcanzaba el
  techo -> PLAN_VER 3: frames PROPORCIONALES al presupuesto; perilla GUI
  "Frames máx/chunk" 4..20 (default 8; ~2 fps efectivos en acción, a 20 ~4-5 fps);
  max_tokens escala con los frames (20 imágenes truncaban a 700 fijos). Deltas
  r4/r5 de Codex -> READY (él corrió 10.000 casos aleatorios del planificador).

### LALM de audio también online (OpenRouter)
- describir.py: backend local|online — mismo chunking/prompt/contexto/parseo; solo
  cambia el transporte. Qwen-Omni NO está en OpenRouter; MODELS_ONLINE curado:
  nemotron free / gemini-2.5-flash-lite (default, ~$0.09 por 90 min) /
  gemini-2.5-flash (~$0.35) / gpt-audio-mini (~$0.24). GUI: segmented LALM
  cpu/gpu/online + modelo + estimación de costo en vivo.
- Verificado con mock de transporte (la mecánica completa); el smoke real está
  BLOQUEADO por saldo (OpenRouter exige >= $0.50 para audio). Al cargar saldo:
  `python describir.py <wav> --smoke --online`.

### Prompts v2 de visión (afinados con Gabriel)
- JUEGO: primero DESCRIPTIVO con lo que se ve; después SOLO los cambios importantes
  como eventos (sin rellenar la línea de tiempo).
- CÁMARA: campos nuevos pose, atencion (alta/media/baja/no_visible),
  hablando_a_camara (mira a cámara moviendo labios), vestimenta (solo si llama la
  atención); eventos propios (gesto/emocion/movimiento/hablando_camara). SCHEMA_VER
  2 + PROMPT_VER v2 (invalida la caché vieja adrede). Verificado contra la API real
  en ambas lanes; la skill documenta los campos (hablando_a_camara = hook natural).

### Reescritura de los handoffs
- referencia-tecnica.md pasó a ser la referencia del estado actual; este archivo, el timeline.
  Originales en docs-archivo/.

---

## 2026-07-20/21 (2ª sesión) — Fix pillow, reproductor streaming, arranque, Qwen por API

### Fix Windows: ModuleNotFoundError 'PIL'
- La PC Windows no tenía pillow: el SHIP-README decía "nada de pip" por error
  (customtkinter NO depende de pillow; en Linux llegaba transitivo por
  emotiefflib/matplotlib, y en Windows emotiefflib va --no-deps). Síntoma: todo
  negro + crash en medios.py/wizard_extraer.py. Fix en la PC:
  `tools\uv.exe pip install pillow --python .venv\Scripts\python.exe`.
  Corregidos requirements*/SHIP-README/HANDOFF.

### Reproductor de video REAL en el mini-editor (diseño 3 rondas con Codex)
- Diseño en three-brain-out/2026-07-20-reproductor-optimizacion/ (DISENO.md v2 +
  DISENO-v3-addendum.md; reviews r1/r2 de diseño + r3-r6 de implementación →
  READY, todos los hallazgos aplicados y re-verificados en GUI).
- ANTES: "playback" = un ffmpeg NUEVO por frame cada 500 ms, adaptativo, que con
  un frame >0.9 s se APAGABA (en la PC Windows con un video de 2 h: imagen
  congelada, "no reproduce"). AHORA: `medios.VideoStream` — UN ffmpeg persistente
  por sesión de play (rawvideo RGB a dims exactas del letterbox, fps 15, cola
  acotada con backpressure), `SesionVideo` (warm-up A/V: el audio arranca al
  primer frame o a los 2 s; atraso por reloj de arribo; respawn con cooldown ×4;
  degradación 10fps/-25 %; av_offset_s en config.json, default 0.25),
  `Prefetcher` (UN ffmpeg a 0.5 fps por ventana [t-10, t+60] — scrub con caché
  LRU por bytes, 64 MB, sesgo hacia adelante).
- Calidad: todos los pedidos de frame a resolución REAL del canvas (antes 1024
  fijo), MJPEG q:v 4 (antes 6), LANCZOS al re-escalar. Durante play: item de
  canvas persistente (sin redibujo total por frame) y cero resize.
- Consola paso 1: "preview listo en Xs" (play/scrub no esperan waveforms),
  "waveforms k/n · pista i: 35 %" con barra en el carril, "▶ … 1er frame 240ms ·
  audio spawn 70ms", avisos de respawn/degradación.
- Robustez: `_stop_preview()` único (epoch de sesión r2.3 — mata stream+prefetch+
  timers+audio en play/stop/cambio de video/nav/cierre), lock del Job Object,
  ffplay memoizado, progreso en envolvente().
- Verificado en Linux (GUI + sintético 30 min): 1er frame ~240 ms, 15 fps, seek
  y mute en vivo, stop/cierre sin huérfanos, respawn tras kill del decoder.

### Qwen-Omni por API (pedido de Gabriel: elegir el modelo de audio como API)
- describir.py: proveedores {openrouter, dashscope} — Alibaba Model Studio sirve
  la familia Qwen-Omni que OpenRouter no tiene: qwen2.5-omni-7b (el MISMO del
  carril local), qwen-omni-turbo, qwen3-omni-flash, qwen3.5-omni-flash/plus.
  Rama dashscope: stream=true obligatorio (SSE), audio como data-URI, sin
  response_format. `dashscope_api_key` nueva en config.json con campo en la GUI
  (aparece al elegir un modelo qwen). `describir.key_para(modelo)` resuelve la
  key por proveedor (pipeline la usa). La casilla "descripción" ya no se bloquea
  sin llama.cpp local (online no lo necesita). Review Codex 3 rondas → READY
  (SSE estricto: finish_reason=stop obligatorio, retry solo de streams
  incompletos, vigilante que hace shutdown del socket al cancelar — en urllib3
  v2 el socket se alcanza por raw._fp.fp.raw._sock). Verificado con mock SSE +
  GUI; el smoke real necesita una key de Alibaba (Model Studio → API Keys).

### Arranque: 6.0 s → 2.5 s en Linux (en Windows el ahorro es mayor)
- Causa medida: los available() de metadata/escena_audio/cara/laughter/align
  IMPORTABAN torch/transformers/mediapipe/torchaudio al construir la GUI, y
  recommend_whisper_model()/has_gpu importaban ctranslate2→torch. Fix: find_spec
  (sin importar) + sondeo nvidia-smi (`hardware._gpu_nvidia_smi`); el chequeo
  real sigue al transcribir (resolve_device cae a CPU). Timings de arranque a
  stderr y logs/arranque.log.

### Ship
- `ship-windows-2026-07-17.zip` REGENERADO (mismo nombre a pedido de Gabriel),
  dos veces: post-reproductor y post-dashscope. Ahora incluye visión (vision.py,
  que el zip original no tenía), reproductor, arranque y Qwen por API. SHIP-README
  reescrito: el paso 1 es el pip de pillow (obligatorio, una vez).

---

## 2026-07-21/22 — Pestaña «Marcar»: revisión post-extracción + guion para la IA

El pedido de Gabriel: una tab nueva donde importa un video YA procesado, ve la
metadata pintada sobre el timeline (habla/risa/arousal como brackets de colores,
read-only), marca regiones y puntos con instrucciones VIENDO esa metadata, y el
panel derecho es el GUION (`<video>.guion.md`) — el documento que junto al
master.json se le entrega a la IA que edita. Decisión clave (re-preguntada 2×):
**«JSON manda, el .md es vista»** — el sidecar de marcas es la única fuente de
verdad; el .md se regenera; solo el texto libre del guion pertenece al archivo.

- Diseño con Codex gpt-5.6-sol: 5 rondas → READY
  (three-brain-out/2026-07-21-tab-marcar/DISENO-final.md). Decisiones grandes:
  refactor A1 (extraer el mini-editor del wizard a `editor_medios.py`, en dos
  etapas con verificación de equivalencia), identidad video↔master B3
  (`fuentes.media` fingerprint hacia adelante + match legacy conservador),
  Registro de marcas COMPARTIDO entre tabs (transaccional, suscriptores,
  adopción de sidecars secundarios persistida antes de notificar), guion con
  orden documental del usuario (bloques in situ, huérfanos a texto libre con
  nota, sentinelas con gramática estricta + backup + reparación explícita),
  índice de intervalos starts+prefix_max_end con LOD, carril fantasma,
  staleness por sha, y `pipeline.actualizar_derivados()` (spec.resuelto.json
  persistido sin secretos; re-corre solo consolidar+vistas con snapshot nueva
  de marcas y upstream validado con sha SIEMPRE).
- Implementación completa (editor_medios.py, marcar.py, guion.py, marcas.py
  ampliado, consolidar 2.3, pipeline, app con 5 tabs) + review de Codex en
  4 rondas → READY con 22 hallazgos aplicados (bloqueantes: master degradado
  tras run interrumpido, snapshot de marcas «opcional», pisado de ediciones
  externas del guion, detección dual de cambios en el archivo equivocado;
  más el proxy Tcl del Text fail-closed, offsets Tk sin astrales, etc.).
- Primer uso REAL en la PC Windows (2026-07-22): carga, carriles y guion OK.
  Dos incidencias del mundo real, arregladas al toque: (1) el video se había
  MOVIDO desde la extracción → `actualizar_derivados(fuente=)` acepta la ruta
  actual como localizador (la identidad la valida el fingerprint); (2) proyecto
  extraído antes del spec persistido → `SpecNoDisponible` declara
  `paquete=stale_legacy` en el header del guion (guion+sidecar mandan);
  re-correr el wizard una vez lo moderniza.
- Reporte de Gabriel con contenido real: el detector de RISAS mete demasiados
  falsos positivos → pendiente priorizado (plan: filtro por confianza mínima
  configurable antes de tocar el modelo).
- `~/.codex/skills/clipear` volvió a ser SYMLINK a la de .claude (era una copia
  idéntica pero destinada a divergir) — el espejo manual murió.

---

## 2026-09-06 — Primer uso real de la instalación Windows (release 0.2.0)

- Gabriel instaló el paquete administrado (`transcriptor-installer-v0.2.0`) en su PC
  (RTX 4070 Laptop). Dos bloqueos del mundo real, arreglados y verificados en la app
  instalada:
  (1) la app no arrancaba: el launcher dejaba `__pycache__` dentro de la release y la
  verificación exacta contra el manifiesto lo rechazaba (`bootstrap.py -B` +
  verificación tolerante al arrancar / estricta al instalar);
  (2) la alineación MMS moría al descargar el modelo: sin consola (`pythonw`) tqdm
  escribía en `sys.stderr = None` → `app._ensure_std_streams()` redirige a
  `shared/logs/salida.log`.
- Reanudar repetía TODA la transcripción: Whisper+MMS era un paso único. Ahora
  `whisper_X` y `align_X` son pasos separados con checkpoint propio; manifests viejos
  adoptados; diálogo «Retomar / Empezar de cero» al reanudar con trabajo previo.
- `large-v3-turbo` pasa a ser el modelo por defecto (configurable en Ajustes) y el panel
  «Pipeline editorial» de Automático permite elegir modelo y desmarcar MMS, risa o
  intensidad+emoción por corrida.
- `actualizar-release.bat` para reinstalar la release desde el código fuente; tests
  ampliados (pasos omitidos + adopción legacy + Whisper una sola vez). 41 tests OK.

---

## 2026-09-06 (2ª sesión) — Recortes: silencios propuestos, carril interactivo, AI y corte

- Pedido: reutilizar la idea de «cortar donde no hay voz» del modo Manual, pero dentro de
  Automático, sobre el timeline, como PROPUESTAS «de qué punto a qué punto» que se ven,
  se mueven, se quitan o se agregan con el mouse, y que solo se apliquen al pulsar cortar.
  Flujo: analizar (Whisper/risa/intensidad) → bloques de 30–45 min → temas/subtemas →
  heurística de huecos sin voz → revisión humana → segunda pasada de la AI (recortes de
  contenido que no aporta, sin tocar humor fuerte ni lisuras) → revisión → «Cortar».
- `editorial_trims.py`: huecos sin palabras ni risas en NINGUNA pista + medición de
  actividad RMS por pista (los huecos con actividad quedan propuestos pero desactivados);
  `views/trims.json` revisable; propuesta de la AI validada y con bordes ajustados;
  paquete de revisión por bloque (`trim-review.md` con `⟂ RECORTE`).
- Timeline: carril «recortes» con hooks nuevos del mini-editor (gesto por carril + teclas
  del dueño); colores por origen, handles, tooltip, menú contextual, proyección sobre las
  pistas, «saltar recortes al reproducir».
- Exportación: `trim`/`atrim` + `concat` por script de filtros (offsets de pistas
  preservados, sin deriva A/V, sondeo `-/filter_complex` para el ffmpeg 2026).
- Skill /transcriptor con la Tarea 2 (criterio editorial + prohibición explícita de
  recortar por contenido ofensivo). 54 tests OK; smoke de la UI real con gestos simulados.

---

## Pendientes al cierre de este período

- Primera corrida editorial completa con el VOD real en Windows y publicar `v0.2.1`.
- Recortes con el VOD real: calibrar hueco mínimo/margen y el umbral de actividad; primera
  Tarea 2 real de la AI; verificar el sondeo de `-/filter_complex` en Linux (ffmpeg < 7).

- Detector de risas: filtro por confianza (falsos positivos con contenido real).
- Primer VOD real con visión activada (iterar prompts v2 con contenido real).
- Saldo OpenRouter >= $0.50 -> validar audio online; key de Alibaba Model Studio
  -> smoke `--smoke --online qwen2.5-omni-7b` (IDs dashscope sin validar contra
  cuenta real).
- /clipear real de punta a punta (protocolo por capas + marcas + visión + guion).
- Smoke Windows restante: pillow (paso 1 del SHIP-README), reproductor con el
  video de 2 h (1er frame, A/V, huérfanos, arranque.log), rueda, DPI, ffplay,
  Job Object; y «Preparar paquete» sobre un proyecto extraído post-2026-07-21.
- Diseño agente-app (2026-07-16): aprobado, sin implementar.
- Diferidos: batching de inferencia (HALLAZGOS-futuros.md), semillas del índice
  desde eventos VLM, torch-GPU en Windows junto a whisper.
# 2026-09-06 — Proyectos modulares, capas, temas y reproductor (0.3.1)

Entrega de los cinco objetivos en commits separados, seguida de revisión de
compatibilidad y release 0.3.1. El candidato 0.3.0 pasó Linux; Windows detectó una
aserción de test basada en prefijos de ruta que no toleraba TEMP con nombres 8.3.
Se reemplazó por `Path.samefile`, manteniendo el tag anterior sin reescribirlo.
Suite final: 75 tests; smoke Tk con
exportación multipista, reimportación automática del hijo, silencios sin inferencia,
dos pasadas de temas, edición/borrado, marcas antes de transcribir y playback real.
Las capas comunes sustituyen los gestos propios de recortes: doble click abre el
editor; X y menú cambian estado. Los avisos de bordes de las propuestas siguen en
sus diagnósticos JSON; el carril común usa color de origen/estado/selección.

Objetivo E: reproducción ligada al reloj de salida de FFplay y cachés de navegación.
VOD Windows de 2 h 59 min: desfase mediano medido 1,19–1,44 s → 20–24 ms;
salto repetido ~1.780 → 10–11 ms; zoom 25,8 → 3,9 ms. 73 tests y smoke Tk.
Detalle reproducible y límites en `mediciones-reproductor.md`.

Objetivo D: catálogo por contenido, reconstrucción automática, carpetas movibles,
selector de versiones e importación automática de padre/hijo. 69 tests y smoke Tk.

Objetivo C: bucle externo de temas en dos pasadas, unión trazable de recurrencias,
capas multirrango y nueva Tarea 3. Suite ampliada a 66 tests.

Objetivo B: contrato y editor de capas, adaptadores sin duplicar marcas/recortes,
comentarios para AI, protección frente a cambios externos. 62 tests y smoke Tk.

Diseño publicado antes de código. Objetivo A: exportación con sub-masters trazables
por segmentos, señales multipista y audio RMS reanudable. 57 tests y smoke real de
App/Tk en Windows. Corregidas importaciones pesadas descubiertas por el smoke.

## 2026-09-06 (4ª sesión) — Timeline estable y panel derecho ajustable (0.3.2)

El detalle del item de capa bajo el mouse se escribía en el status del pie del
editor; su wrap cambiaba la altura del pie y el timeline y el preview (filas
elásticas) saltaban con cada movimiento del mouse sobre las capas. Ahora el pie
tiene altura fija de dos renglones (lo que no entra se recorta, nunca empuja) y
el detalle vive en `LayerDetailBar`, una barra propia de altura constante entre el
timeline y el status: color de origen/capa, capa, etiqueta, estado, tramos y el
comentario recortado con «…» al ancho real; sin mouse encima muestra el item
seleccionado o la ayuda. El panel derecho de Automático se redimensiona
arrastrando el divisor (doble click restaura 292; el ancho se guarda en
`config.json`). 77 tests y smoke Tk con capturas: el timeline no se mueve con
textos largos ni con el hover; el divisor cambia el ancho, respeta el mínimo y
persiste.

Misma sesión, después: selector **Salida** para los dos botones de exportación
(H.264, HEVC, ProRes 422 HQ y copia exacta sin recodificar). La copia conserva el
códec y la calidad del original y mueve cada límite entre bloques al fotograma
clave anterior, compartido por ambos bloques; el log informa el desplazamiento y
los hijos heredan el corte real. Motivación: footage raw o intra debe salir
idéntico, solo cortado. 79 tests (copia con GOP de 1 s, ProRes y HEVC reales).
