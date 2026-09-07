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

Cierre de la sesión: diseño (sin código) de la navegación tipo editor en
`docs/diseno-navegacion-editor.md`: velocidad ×1–×8 aplicando el mismo `rate` al
reloj de FFplay (`atempo`) y al stream de video (`fps=VS_FPS/rate`, skip de B-frames
y de no-claves), atajos configurables con `keymap.py` puro y sección Atajos, índice
de bordes con bisect, acciones de edición por `persist` (dividir, recortar, empujar,
A = aceptado con `accepted` aditivo en trims.json sin tocar la exportación),
deshacer/rehacer y puertas de rendimiento medidas con `benchmark_preview.py`. La
Parte 2 es el prompt por fases con invariantes y condiciones de parada para la AI
que lo implemente.

Ampliación del mismo diseño: herramientas de mouse al estilo NLE (Selección con
marquesina y operaciones en lote por `persist_many`; Corte que crea o estira, resta
o divide con Shift y mueve con Ctrl, sin diálogo), y capas de la AI en carriles
separados sin cambiar la autoridad de los datos: temas y subtemas como carriles por
profundidad de la misma capa `topics`, cortes sugeridos de la AI como vista por
origen del mismo `trims.json` (la exportación sigue cortando la unión de los
activos), `kind: "ai"` y respuestas con varias capas para que la skill añada las que
necesite, y un solo pedido «Preparar revisión editorial» (Tarea 4).

Última ampliación del diseño: los carriles de recortes pasan a ser `lanes` del
`trims.json` único (dos de fábrica, `main` y `ai`, más los que añada el usuario),
los solapes dentro de un carril se funden en un solo corte con reglas de actor y
sin cambiar `enabled_intervals`, «Añadir capa» ofrece dos tipos (Recortes: sin
diálogo al dibujar; Pedidos para la AI: diálogo con foco en el pedido y Escape
guarda), la capa nueva se inserta encima de la seleccionada vía `views/lanes.json`,
y una sola tecla de toggle (B) entra y sale de la herramienta Corte.

Y deshacer/rehacer generalizado (Ctrl+Z · Ctrl+R): historial de operaciones sobre
snapshots de los documentos del proyecto (marcas, recortes con lanes, plan, capas,
orden de carriles), restaurado por los mismos caminos de guardado, con entradas
multi-documento, descarte si un documento cambió por fuera y la regla de que toda
mutación nueva de las fases siguientes se registra con su test.

Detalle añadido a la herramienta Selección: recorte por bordes al estilo DaVinci
(cursor de doble flecha y borde resaltado al acercarse a ≤ 8 px del inicio o el
final de cualquier item, arrastre que agranda o encoge solo ese lado con
previsualización y etiqueta de tiempo, zonas reducidas en items estrechos, cursor
cambiado solo cuando cambia el estado).

## 2026-09-06 (5ª sesión) — Navegación tipo editor: Fase 1 velocidad y Fase 2 atajos

Empieza la implementación del diseño `docs/diseno-navegacion-editor.md`, una fase
por commit. Línea base medida antes de tocar nada (`media/bench/baseline.json`,
79 tests verdes).

**Fase 1 — velocidad ×1/×2/×3/×4/×8.** El mismo `rate` en los dos extremos del
reloj: `comando_mezcla` termina la mezcla en `rubberband=tempo=R` (tono
conservado; `atempo` si el ffmpeg de la máquina no lo trae) y FFplay sigue siendo
el maestro con `AudioClock(start, rate)`; `SesionVideo(rate)` decodifica a
`VS_FPS/rate` con `-skip_frame bidir` desde ×2 y `nokey` desde ×6. Por encima de
`preview_audio_max_rate` (4.0) el audio va mudo pero sigue dando reloj.
`set_rate` es una re-sesión con debounce de 150 ms; el reloj muestra «×2».
Benchmark por velocidades (`--rates`, `--seconds`): ×2–×4 sin respawns y 29,9
frames/s de pared; ×1 idéntico al código anterior con el mismo script. La puerta
«cambio de velocidad ≤ 400 ms» no se cumple (≈1,0 s: es el primer frame de una
sesión nueva); queda documentada sin aflojarla. 84 tests.

**Fase 2 — atajos configurables.** `keymap.py` puro (inventario completo del
diseño §3 con ids estables, acordes normalizados, evento Tk con Alt por
plataforma y AltGr latino, conflictos, `keymap.json` con solo las diferencias) y
`keymap_ui.KeymapSettings` en Ajustes (Grabar, ×, Restaurar, conflictos en rojo).
`EditorMedios` cambia los bindings por keysym en los canvases por UN `<Key>` en el
toplevel con guarda de foco y despacho solo del editor activo; `acciones_extra`
reemplaza a `teclas_extra` (Automático migra sus cuatro acciones). Migración 1:1:
la app se comporta igual con el keymap por defecto. Smoke Tk: teclas con el foco
en un Entry no disparan; en el timeline sí; un atajo cambiado aplica sin
reiniciar. 93 tests.

**Fase 3 — navegación.** `editorial_nav.py` puro: índice de bordes (`EdgeIndex`,
bisect sobre tiempos únicos), bordes de todas las capas y de los silencios,
`parse_goto`. `LayersController.edges()/silences()` cachean el índice con la
clave de `all()`; el editor cae a las marcas e IN/OUT cuando no hay carriles.
Acciones: `,` `.` fotograma (±10 con Shift), `↑` `↓` bordes, `Ctrl+↑/↓` silencios,
`Ctrl+G` ir a tiempo, `Shift+I/O` inicio/fin de la selección, `Z` zoom a la
selección, `C` centrar, `F` seguir, `Shift+T` saltar recortes, `Shift+Espacio`
play desde el item. Medido en el smoke: salto a borde < 30 ms de UI. 97 tests.

**Fase 4 — edición y deshacer.** `editorial_edits.py` (dividir, recortar,
empujar, item siguiente, aceptar en lote) y `editorial_history.py` (pila de
operaciones con snapshots por documento, profundidad 50, rama de redo
descartada, `Stale`). `LayersController.transact` envuelve todo punto de
escritura (`persist` con etiqueta, dividir, recortar, empujar, aceptar, diálogo
de capas, importaciones registradas en el hilo de UI) y las marcas del editor
entran por `editor.transaccion`; `undo/redo` restauran por los caminos de
guardado (`Registro.reemplazar` nuevo, `save_document`, `apply_plan`,
`store.save` con tumba/levantamiento). La validez de una entrada se comprueba
por digest de contenido sin campos volátiles (un número de revisión invalidaba
la N−1 al deshacer la N). `accepted` aditivo en `trims.json` (borde verde,
conservado por el análisis de silencios, expuesto en `trim-review.md`; la
exportación sigue con `enabled`). Smoke Tk: S, [, ], Alt+→, A, Shift+A, pedido,
marca y prompt, Ctrl+Z ×3, Ctrl+R ×3, deshacer todo hasta la tumba y rehacer
todo, Ctrl+Z en un Entry no toca el proyecto, cambio externo → entrada
descartada sin pisar. 106 tests.

**Fase 5 — hwaccel, probada y descartada.** `d3d11va` empeora ×1 (2,69 s frente a
2,24 s por 30 s de medio) y el primer frame (~1,0 s frente a ~0,85 s) y no cambia
×4: queda `off` sin código, con los números en `mediciones-reproductor.md`.

**Fase 6 — selección múltiple y herramientas de mouse.** Aritmética pura en
`editorial_edits.py` (`box_add` crea/estira/funde, `box_subtract` borra/recorta/
divide, `marquee_select`) y `editorial_trims.coalesce` (solapes estrictos del
mismo carril y del mismo `enabled` se funden con reglas de actor; la unión que
exporta no cambia, probado con documentos aleatorios). `LayersController`: hit-test
por `visible_parts` con zonas de borde de 8 px (un tercio en items estrechos),
cursor y borde resaltado solo cuando cambia el estado, `selection` + primario,
herramientas Selección (click, Shift+click, marquesina, arrastre del conjunto con
previsualización, recorte por bordes con etiqueta flotante) y Corte (caja, Shift
resta, Ctrl mueve, Ctrl+vacío scrub), `persist_many` (una escritura, una entrada
de deshacer), X/A/Supr en lote, flechas que mueven el conjunto, barra Selección/
Corte en el transporte, `edit_dialog(focus="comment")` con Escape/Ctrl+Enter/X
que guardan. Smoke Tk: marquesina de 3 → X → una revisión; arrastre del conjunto
→ un guardado; caja sobre dos → uno; Shift+caja → dos con `accepted`; Ctrl+arrastre
mueve; cursor `sb_h_double_arrow` y borde resaltado a 5 px del fin de un recorte
no seleccionado; arrastrar ese borde solo cambia `t_fin` y muestra el delta; un
item de 15 px se mueve desde el centro; crear en una capa de pedidos abre el
diálogo con el foco en el pedido; Ctrl+Z deshace cada gesto entero. Mover 120
recortes: 43 ms; hover con 5.000 recortes: 2,2 ms. 112 tests.

**Fase 7 — carriles de la AI y lanes de `trims.json`.** El documento de recortes
gana `lanes` y `lane` (aditivos; los archivos viejos cargan derivando el carril del
origen), `add_lane`/`remove_lane`, fusión de solapes también en las importaciones
(silencios y propuestas de la AI, dentro de su carril). `editorial_layers` produce
un carril de UI por lane (`trims:<lane>`), presenta la capa `topics` por profundidad
(`topics:<id>:<n>`: Temas, Subtemas…), ordena por `views/lanes.json`
(desconocidos fuera, nuevos junto a su vecino natural, «encima del seleccionado»),
admite `kind: "ai"` y funde respuestas con `layers: [...]`. Controlador: añadir
capa (Ctrl+N, dos tipos), borrar carril (mover a «Recortes» o borrar), renombrar,
▲▼, documento de historial `lanes`; origen en la barra de detalle y el tooltip.
Botón «Preparar revisión editorial» (Tarea 4: temas en dos pasadas y luego
recortes sin duplicar los aceptados) y Tarea 4 en la skill. Smoke Tk: propuesta
de la AI → carril superior, A acepta, X desactiva, la exportación une los activos
de ambos carriles e ignora los desactivados; carril nuevo encima del seleccionado,
caja sin diálogo, borrar moviendo a «Recortes» y deshacerlo como UNA entrada;
respuesta de dos capas `ai`; temas con dos niveles y persistir desde «Subtemas».
121 tests. `VERSION` → 0.3.3 («prepara 0.3.3»).

## 2026-09-07 — Barra de herramientas tipo NLE y menú contextual (0.3.4)

Pedido de Gabriel: que las acciones de teclado tengan botones «físicos» con la
descripción al pasar el mouse, y que todo lo que hace una tecla se pueda hacer
también desde la barra o con click derecho. `toolbar_ui.py`: `Tooltip` (aparece
tras 450 ms con etiqueta y atajo VIGENTE, leídos del keymap al mostrarse),
`tool_button` y `build_menu` (todas las acciones disponibles agrupadas, cada una
con su atajo como acelerador). `keymap.menu_groups/tooltip_text` son el modelo
puro. El transporte del editor pasa a ser una barra: herramientas del dueño a la
izquierda (`fr_tools`: Selección/Corte, deshacer, rehacer, dividir, recortar
inicio/fin, aceptar, activar, borrar, añadir capa), transporte centrado (inicio,
−0,5 s, −1 fotograma, play, +1 fotograma, +0,5 s, fin, velocidad: click sube,
click derecho baja), reloj y ⋮ a la derecha; la etiqueta larga de atajos
desaparece. Click derecho en el timeline o el preview (y ⋮) abre el mismo menú;
sobre un item lo selecciona y pone primero editar, aceptar, activar, dividir,
recortar, borrar, ir al inicio y reproducir desde aquí; sobre un carril vacío,
«Añadir capa encima…». Marcar conserva su propio menú de metadata.

Misma sesión, pedidos de Gabriel sobre la marcha: **A** = herramienta Selección (también
V) y **B** = Corte, sin alternar, como en DaVinci; los estados dejan de ser toggles:
**E** acepta, **X** desactiva, **P** activa (vuelve a propuesto; sobre un aceptado le
quita la marca), y sobre varios items todos reciben el mismo estado. Propuesto y
aceptado se cortan igual (aceptar es la marca de revisión humana): para que se
distingan, el propuesto se pinta con relleno rayado y el aceptado sólido con borde
verde y ✓. **D** también borra. Arreglado el subrayado blanco de la selección: al
seleccionar (click o marquesina) se redibujaba el preview y no el timeline; y un click
sin arrastre sobre un conjunto deja solo ese item. Tooltips y menú muestran los
atajos en forma legible («Supr», «→», «.», «Espacio»). 122 tests, smoke completo.

Rango a repetir («limitador») en la regla, pedido de Gabriel: con el botón derecho
arriba, arrastrar define el rango, click fija la entrada, Ctrl+click la salida (ambos
sobrescriben) y Shift (click o arrastre) lo quita; los dos puntos se arrastran con el
botón izquierdo y el cursor avisa; la reproducción vuelve a la entrada al llegar a la
salida (re-sesión). Acciones `loop.*` en el menú, sin tecla por defecto.

## 2026-09-07 — Primera Tarea 4 real: los subtemas siguen al padre ajustado (0.3.5)

Primer intento de revisión editorial completa sobre un bloque real (42:34, dos pistas)
con Claude: la pasada 1 de temas (28 temas, 39 subtemas, bordes en límites de
intervención) fue rechazada con «el ajuste del tema padre deja un subtema fuera».
Causa en `editorial_topics.validate`: el padre se ajusta primero hasta 1,5 s contra
palabras y risas, y en un mapa real casi todos los bordes se mueven; cuando el padre
se encoge, el subtema que compartía ese borde (el caso normal: el primer y el último
subtema tocan los bordes del tema) ya no cabe en ningún rango ajustado y la búsqueda
del padre fallaba. Ahora el rango padre se localiza por los bordes que la AI propuso
(`proposed`), sus bordes ajustados hacen de tope y el borde del subtema que quedó fuera
apunta al borde del padre, así que `snap_boundary` lo deja pegado a él. Los mensajes
de error nombran el `item_id`. Test de regresión con una palabra que cruza el borde
final compartido; 122 tests. La propuesta real valida completa (67 rangos ajustados,
todos los subtemas dentro de su padre).

Misma sesión: la segunda propuesta (ChatGPT, 35 items) también quedó sin importar,
esta vez porque Gabriel editó un recorte entre «Preparar» e importar y el
`source_layers_digest` ya no coincidía; la app lo rechaza bien pero solo lo dice la
consola. Gabriel pidió además aclarar el panel derecho («tres botones para preparar
para la AI y no sé cuál pulsar»), una pasada de recortes más profunda, reimportar el
video recortado con su metadata y una etapa nueva de montaje por temas con la AI
(clips reordenables y pistas de video). Todo eso quedó escrito como plan por fases
para otro agente en `docs/plan-montaje-ai.md`, con los textos de skill de las tareas
nuevas. Hallazgo al redactarlo: «Cortar y exportar» ya publica un proyecto hijo por
video con el master derivado a los segmentos conservados (`publish_child`), así que
la reimportación pedida existe; falta propagar las capas de temas al hijo.

## 2026-09-07 — Panel derecho: un botón para la AI y estado del ciclo (0.3.6)

Fase A del plan de montaje (`docs/plan-montaje-ai.md` §4). Gabriel no sabía cuál de los
tres botones de «preparar» pulsar ni en qué punto estaba el ciclo con la AI (la
propuesta de ChatGPT rechazada por «las capas cambiaron» solo se veía en la consola).
El panel queda en el orden del flujo: Conversación · Revisar bloques · Importar JSON ·
RECORTES (Analizar silencios · Exportar con recortes · Saltar recortes) · Salida ·
Exportar bloques · Abrir proyecto · Procesar pistas / Reanudar · Capas… · **Preparar
para la AI ▾** · línea de estado. El desplegable ofrece Revisión completa (por
defecto), Solo temas, Solo recortes (y en las fases siguientes Recortes profundos y
Montaje por temas); «Preparar capas para AI» desaparece porque `views/layers.json` ya
se escribe tras cada cambio y en cada Preparar. En un video recortado (hijo) se
ocultan Procesar pistas y Exportar bloques. Módulo puro nuevo `editorial_cycle.py`
(`status(views, layers_digest, last_error)`), con `tests/test_cycle.py` (7 tests): sin
pedido, temas pasada 1/2/hecha, revisión completa hasta «recortes importados: N en
«Cortes sugeridos (AI)»», solo recortes (normal y profundo), propuesta rechazada con
«Último error», montaje, y **«el pedido quedó viejo»** cuando el digest de la foto de
capas ya no es el del pedido (el caso que confundió a Gabriel). Tooltips en todos los
botones. De paso, dos carreras reales: el sondeo reimportaba un JSON importado a mano
(«pasada fuera de orden» espurio) y un `trims_loaded` tardío pisaba un documento
editado después; y el `rename` final de la exportación reintenta ante el bloqueo
transitorio de Windows. 129 tests y smoke completo (con las comprobaciones nuevas:
etiqueta de estado, pedido viejo, botones ocultos en el hijo y «Cancelar» visible
mientras trabaja). Probado contra el proyecto real: pendiente hasta reinstalar la
release con la app cerrada.

## 2026-09-07 — Tarea 2 «modo profundo»: recortes de contenido agresivos (0.3.7)

Fase B del plan. Después de la primera revisión editorial la AI propuso solo tres
recortes; Gabriel quiere poder pedirle explícitamente una lectura más exigente de las
dos pistas (tangentes sin retorno, lectura en voz alta, lo que no es divertido ni
lleva a ningún lado, meta y técnica), con la regla dura de nunca recortar por lisuras,
insultos, humor negro ni contenido «funable». La sección «Tarea 2 · modo profundo» de
la skill lo dice con detalle; «Preparar para la AI ▾ → Recortes profundos» escribe el
paquete de la Tarea 2 con `mode: deep` y `lane: ai-deep`; la propuesta (`"mode":
"deep"`) cae en un carril propio «Cortes profundos (AI)» (rosa, declarado al importar
sin migrar archivos viejos) con el motivo precedido por «[profundo]», para aceptar o
descartar la pasada en bloque. La AI solo puede escribir en carriles `ai*`; cada
pasada reemplaza únicamente los cortes de la AI de su carril. Cuatro tests nuevos
(pedido, import a carril propio conservando la primera pasada, rechazo de `lane:
main`, archivo viejo sin el carril) y el smoke prepara el pedido profundo, importa
una propuesta y comprueba el carril, el orden y la etiqueta de estado. 133 tests.

## 2026-09-07 — El video recortado hereda temas y capas; «Abrir el video recortado» (0.3.8)

Fase C del plan. Gabriel quería exportar con los recortes, volver a abrir el resultado
y tener «el mismo JSON pero habiendo quitado la metadata que caía dentro de lo
recortado». El master derivado ya existía (`publish_child`); faltaban las capas:
`editorial_projects.derive_layers` remapea al reloj del hijo la capa de temas, las de
pedidos y las de la AI con el mismo criterio que la metadata (`map_range`): un rango
que cae entero en un recorte desaparece (y sus subtemas), uno que cruza un recorte
queda en dos tramos contiguos, y cada item guarda `source_item_id` y cada tramo
`source_range` para volver al padre; ids, estados y ediciones se conservan;
`views/lanes.json` viaja también; `trims.json` y las marcas del autor no. «Exportar
con recortes» pasa las capas visibles y, al terminar con un solo archivo, muestra
«Abrir el video recortado» en la caja RECORTES: importa el video y carga su hijo (por
huella o, si el catálogo aún no lo tiene, por la ruta del master exportado). Dos
tests nuevos (remapeo puro con jerarquía y exportación real con `layers/` en el hijo)
y el smoke pulsa el botón y comprueba los rangos heredados de la capa de temas.

## 2026-09-07 — Timeline de montaje: clips, pistas de video, preview y exportación (0.3.9)

Fase D del plan, la grande. Para que la AI pueda «editar de verdad» (reordenar tramos,
apilar clips) el timeline gana un modo **Montaje** (conmutador Fuente | Montaje, Ctrl+M):
la regla mide la secuencia, las pistas de video `V1`, `V2`… se apilan (la de arriba
tapa; siempre hay una vacía encima para arrastrar hacia arriba), una franja fina dice
de qué tema es cada clip, y las waveforms se componen en tiempo de secuencia. Modelo
puro `editorial_montaje.py` (`views/montaje.json`, `editorial-montaje/1`): aplanado por
intervalos con la pista más alta al mando, insert con ripple y overwrite, split, trim
acotado, remove con ripple, desplazamiento de conjuntos, mapa secuencia↔fuente. El
editor recibe un `mapa_tiempo`: el playhead, el reloj y la regla viven en secuencia; la
sesión de video, el audio y los frames en fuente; al terminar un tramo la reproducción
se re-arma desde el siguiente (el mismo mecanismo de «saltar recortes», ≈1 s por junta
en HEVC, aceptado en esta etapa). Gestos: mover con imán y cambio de pista, bordes,
Corte, Shift-multiselección; teclas S/[/]/E/X/P/Supr/Tab/↑↓ y las nuevas del grupo
Montaje (Ctrl+Shift+A añade la selección de la fuente, Ctrl+Shift+T el tema,
Ctrl+Shift+R revela la fuente, Ctrl+Shift+↑/↓ cambian de pista); deshacer/rehacer
por el documento `montaje` del historial. `podcast_export.export_montage` renderiza un
solo video con los tramos en orden de secuencia (el script de filtros ya concatenaba en
el orden de la lista) y publica un hijo con segmentos no cronológicos
(`time_map(chronological=False)`). Caja MONTAJE en el panel con estado y «Exportar
montaje». 11 tests nuevos (modelo, historial, exportación real no cronológica con
hijo) y bloque del smoke con captura (`--screenshot`). 146 tests.

## 2026-09-07 — Tarea 5: la AI propone el montaje por temas (0.4.0)

Fase E del plan: la etapa nueva que pidió Gabriel. «Preparar para la AI ▾ → Montaje
por temas» escribe el pedido con la duración objetivo (campo en la caja MONTAJE, 15 min
por defecto, ±15 %), el transcript con los temas y subtemas intercalados como
encabezados, las señales con la lista de picos y, en pasadas siguientes, la secuencia
actual con una tarjeta por junta (palabras a cada lado, salto temporal, temas, riesgo
mecánico). La AI responde `montaje.proposed.json` (clips en orden de secuencia, con
tema, motivo, confianza y nota de junta; `keep` y `repeat`); la app valida identidad y
digests (también el del montaje vivo), ajusta bordes, avisa sin bloquear cuando la
duración o un clip se salen de los límites, reemplaza solo los clips de la AI que la
persona no aceptó ni editó, coloca los nuevos en V1 en el orden propuesto y sube la
pasada. La sección «Tarea 5 — Montaje por temas» de la skill pide explícitamente
conservar el humor tal como es (la AI no censura; Resolve es la última pasada humana).
Tres tests del ciclo (pedido y transcript, validación con tolerancia/repetidos/orden,
bucle de dos pasadas con protección y `keep`) y el bloque del smoke; 149 tests.
