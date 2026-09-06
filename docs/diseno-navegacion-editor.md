# Navegación tipo editor: velocidad, atajos configurables y acciones rápidas — diseño 2026-09-06

Estado: **diseño aprobado, sin implementar**. Parte 1 es la arquitectura; Parte 2 es el
prompt para la AI que lo implemente, con sus salvaguardas. Todo se apoya en cómo está
construido hoy el reproductor (`docs/referencia-tecnica.md` §3.1 y §5,
`docs/mediciones-reproductor.md`); nada de lo que sigue cambia el modelo de sincronía.

---

## Parte 1 · Arquitectura

### 0. Punto de partida (lo que ya existe y hay que respetar)

- **El reloj de audio manda.** `medios.Reproductor.play()` lanza `ffmpeg` (mezcla desde
  `-ss t`, WAV por pipe) → `ffplay -sync audio -stats`. `playback_clock.AudioClock`
  parsea la línea de estado de FFplay y devuelve `start + valor + transcurrido`; si la
  muestra tiene más de 0,5 s devuelve `None` y el tick **no avanza por suposición**.
- **El video es un ffmpeg persistente.** `medios.VideoStream` decodifica desde `t0`
  hacia adelante con `-vf fps=F,scale=W:H` a rawvideo RGB, con deque de 8 y
  backpressure; timestamps sintéticos `ts = t0 + n/F`. `SesionVideo.frame_para(t)`
  entrega el último frame con `ts ≤ t` y aplica atraso/respawn/degradación.
- **Un solo bucle.** `EditorMedios._anim_tick` cada 16 ms: warm-up (espera el primer
  frame, tope 10 s, recién ahí arranca el audio y fija el ancla) → `t_play = reloj` →
  frame para `t_play` → auto-scroll por saltos de página.
- **Un solo apagado.** `_stop_preview()` invalida el token del tick y el
  `_preview_epoch`, mata stream, prefetch, timers y audio. Todo pasa por ahí.
- **Seek durante playback** = `_remezclar_debounced(destino)` → `_play(reiniciar=True,
  desde=t)` con debounce de 300 ms. Mute/solo durante playback = `_restart_audio()`
  (solo la mezcla; el stream y el tick siguen).
- **Teclas hoy:** `_tl_key` con keysyms fijos, ligadas solo a los dos canvases (nunca
  `bind_all`: m/i/o/x deben poder escribirse en cualquier Entry — consenso q.4). El
  dueño (Automático) va primero vía `teclas_extra` para el item de capa seleccionado.
- **Medición:** `tools/benchmark_preview.py` (3 posiciones, seeks, zoom, JSON) y
  `shared/logs/reproductor.jsonl` (first_frame_ms, audio_spawn_ms, audio_clock_ms).
  Referencia Windows, VOD HEVC 3360×1080 de 2 h 59: desfase mediano 20–24 ms.

### 1. Velocidad de reproducción (×1, ×2, ×3, ×4, ×8)

**Idea:** la velocidad es un factor `rate` que se aplica **en los dos extremos del mismo
reloj**: el audio se estira con `atempo` (así FFplay sigue siendo el reloj maestro) y el
video decodifica a `VS_FPS/rate` frames por segundo de medio. Nada más cambia.

Audio (`medios.Reproductor.play(video, pistas, t, rate=1.0)`):

- La mezcla termina en un estiramiento temporal **que conserva el tono** (lo mismo que
  hace DaVinci al acelerar): se oye lo que dicen a ×2, ×3 y ×4, solo más rápido, sin
  voz de ardilla. El ffmpeg incluido trae dos: `rubberband=tempo={rate}` (librubberband,
  la mejor calidad para voz; opciones `pitchq=quality`, `transients=smooth`) y
  `atempo={rate}` (WSOLA, más ligero, 0,5–100 en un solo filtro). Se usa rubberband y,
  si el filtro no existe en el ffmpeg de la máquina (`ffmpeg -filters`, sondeado una
  vez como `filter_script_option`), atempo. Con una pista: `-af`; con varias, encadenado
  tras `amix` en el mismo `filter_complex`. Coste medido en el PC de referencia sobre
  60 s del podcast (decodificación AAC incluida): atempo ×3/×4 ≈ 70× tiempo real;
  rubberband ×3/×4 ≈ 40× (con `pitchq=quality` ≈ 38×). Ninguno compromete el reloj.
- El audio **nunca se silencia por velocidad**. Solo el skim a ×8 (fotogramas clave)
  lo lleva a `volume=0`, porque a ×8 ya no es inteligible, y aun así FFplay consume el
  audio estirado y **sigue dando reloj**: un solo diseño de reloj para todas las
  velocidades. `preview_audio_max_rate` (config.json, por defecto 4.0) permite bajar ese
  umbral si en otra máquina el estiramiento no llega a tiempo real.
- `AudioClock(start, rate)`: `position = start + rate*(valor + transcurrido)`. El umbral
  de reloj perdido (0,5 s) sigue en segundos de pared.

Video (`medios.SesionVideo(..., rate)` → `VideoStream(fps=VS_FPS/rate, skip=...)`):

- `fps=VS_FPS/rate` produce exactamente 30 frames por segundo de **pared** a cualquier
  velocidad, y la fórmula `ts = t0 + n/fps` sigue siendo correcta sin tocarla (fps es
  la tasa de salida en tiempo de medio). `frame_hasta(t)` no cambia.
- El decodificador aun así procesa todos los frames fuente (`rate` × tiempo real). Para
  que no se ahogue: `rate ≥ 2` → `-skip_frame bidir` (salta B-frames; el filtro fps
  rellena duplicando); `rate ≥ 6` → `-skip_frame nokey` (solo claves: con GOP de 2 s son
  4 frames/s de pared a ×8, un «skim» casi gratis). Son opciones de ENTRADA (antes de
  `-i`). Constantes `VS_SKIP_BIDIR_RATE = 2.0`, `VS_SKIP_NOKEY_RATE = 6.0`.
- La política de atraso/respawn/degradación queda igual (usa reloj de arribo, no ts).
  En este PC la decodificación HEVC 3360×1080 supera 300 fps (la exportación x264
  corrió a 263 fps decodificando lo mismo), así que ×4 con `bidir` sobra.

Editor (`EditorMedios`):

- Estado `self.rate = 1.0`, `SPEEDS = (1, 2, 3, 4, 8)`. `_play(reiniciar, desde,
  rate=None)` pasa `rate` a `SesionVideo` y a `repro.play`; `_restart_audio` también.
- `set_rate(rate)`: pausado → guarda y muestra; reproduciendo → **una** re-sesión desde
  `t_play` con debounce de 150 ms (pulsar L tres veces seguidas = un solo reinicio).
  Reutiliza el camino de `_remezclar_debounced` (mismo token `_remix_n`).
- `_anim_tick` no cambia: `t_play` sale del reloj, que ya viene escalado.
- UI: `lbl_t` muestra `0:12.3 ×2`; el status del play añade `· ×2` y, si el audio va
  mudo por el tope, `(audio mudo)`. Sin widgets nuevos en el transporte.
- Fuera de alcance: reproducción hacia atrás (el stream solo decodifica hacia
  adelante). Se cubre con paso de fotograma atrás y con el skim.

### 2. Atajos configurables (`keymap.py`)

Módulo **puro** (sin Tk en las funciones de lógica; testeable en CI Linux):

- `ACTIONS`: registro ordenado `id → (etiqueta, grupo, acordes por defecto)`. Los ids
  son estables (`transport.play_pause`, `nav.next_edge`, `edit.split`, …).
- Acordes como texto: `"L"`, `"Shift+L"`, `"Ctrl+Shift+Z"`, `"space"`, `"comma"`,
  `"Left"`, `"KP_Add"`. `parse_chord` / `format_chord` normalizan mayúsculas y orden de
  modificadores. `chord_from_event(keysym, state)` traduce el evento Tk: Shift `0x1`,
  Control `0x4`, Alt `0x20000` en Windows y `0x8` (Mod1) en Linux; se ignoran NumLock y
  CapsLock. Las letras se resuelven por `keysym.lower()` para que Shift+letra funcione
  igual con CapsLock.
- `Keymap.load()`: defaults + `keymap.json` en `app_paths.CONFIG_DIR` (por máquina,
  como config.json). Ids desconocidos se ignoran con aviso; conflictos (un acorde en
  dos acciones) se reportan y gana el primero en orden de registro. `resolve(chord) →
  id | None` es un lookup de diccionario: O(1) por tecla, sin regex por evento.
- `save(overrides)` escribe solo las diferencias con los defaults; `reload()` recarga
  el singleton: los editores resuelven por el singleton en cada evento, así que un
  cambio en Ajustes aplica al instante sin re-bindear.

Despacho en `EditorMedios`:

- `_tl_key` pasa a `_dispatch(e)`: `action = keymap.resolve(chord_from_event(e))`; el
  dueño va primero con `acciones_extra(action, e) → bool` (sustituye a `teclas_extra`;
  Automático migra sus cuatro casos: borrar, X, Enter/F2, Escape). Luego la tabla
  `self._handlers[action]`.
- Las teclas deben funcionar sin tener que hacer click en el canvas antes. Regla: se
  liga `<Key>` también en el toplevel (`add=True`) con **guarda de foco**: solo se
  despacha si el widget con foco es un Canvas, un Frame o el propio toplevel; nunca si
  es Entry/Text/CTkEntry/CTkTextbox (se sigue escribiendo m/i/o/x) ni Button/Checkbox/
  OptionMenu (space/Return los activan). Además, click con botón izquierdo en cualquier
  parte no interactiva del editor → `tl.focus_set()`, y Escape en un Entry devuelve el
  foco al timeline (ya existe para el prompt de marca).
- Solo el editor **activo** despacha: `activar()/desactivar()` fijan
  `self._keys_activos`; hay tres editores en la app (wizard, Marcar, Automático).
- `return "break"` solo cuando se consumió una acción; una tecla sin acción sigue su
  propagación normal.

Ajustes → sección **Atajos** (`app._build_settings`): tabla por grupo con etiqueta,
acorde actual y botones «Grabar» (captura la siguiente pulsación con la guarda apagada
y muestra el acorde) y «×» (sin atajo); conflictos en rojo con el nombre de la otra
acción; «Restaurar predeterminados». Guardar → `keymap.save()` + `reload()`.

### 3. Acciones (inventario y atajos por defecto)

Transporte:

| Acción | Tecla | Detalle |
|---|---|---|
| Play/pausa | Space | Reproduce a la velocidad actual |
| Pausa | K | |
| Más rápido | L | ×1→×2→×3→×4→×8; pausado = play a ×1 |
| Más lento | J | ×8→×4→×3→×2→×1; a ×1 pausa |
| Velocidad exacta | 1 2 3 4 | Arranca si estaba pausado |
| Skim | Shift+L | ×8 con solo fotogramas clave |
| Play desde el item | Shift+Space | Desde el inicio del item seleccionado (o del IN) |

Navegación:

| Acción | Tecla | Detalle |
|---|---|---|
| Fotograma −/+ | , . | ±1/fps; con Shift ±10 fotogramas. Usa `_set_playhead` (caché exacta) |
| Paso −/+ | ← → | ±0,5 s; Shift ±5 s (como hoy) |
| Inicio/fin | Home End | como hoy |
| Borde anterior/siguiente | ↑ ↓ | Bordes de bloques, recortes, items de capa y marcas visibles |
| Silencio anterior/siguiente | Ctrl+↑ Ctrl+↓ | Solo recortes de origen `silence` |
| Ir a tiempo | Ctrl+G | Diálogo: `1:23:45.6`, `5025`, `+30`, `-10` |
| Inicio/fin de la selección | Shift+I Shift+O | Del item seleccionado o del IN/OUT |

El **índice de bordes** se construye una vez por cambio de documentos (mismas claves
de `LayersController._cache_key`): lista ordenada de tiempos únicos de todos los
carriles visibles; cada salto es un `bisect` (O(log n)) sobre miles de recortes.

Edición (sobre el item seleccionado; todo pasa por `LayersController.persist`, así
vale para capas propias, recortes, bloques y marcas del autor con sus validaciones):

| Acción | Tecla | Detalle |
|---|---|---|
| Dividir en el playhead | S | Rango → dos items (recortes: dos cortes; bloques: nuevo límite validado con `snap_plan_to_safe_boundaries`; marcas: dos regiones) |
| Recortar inicio/fin al playhead | [ ] | `t_ini`/`t_fin` = playhead, con validación |
| Empujar ±1 fotograma | Alt+← Alt+→ | Shift = ±10 fotogramas |
| Item anterior/siguiente | Tab Shift+Tab | Dentro del carril del item seleccionado (o el que está bajo el playhead) |
| Aceptar / quitar aceptación | A | `proposed`/`disabled` → `accepted`; `accepted` → `proposed`. Ver §3.1 |
| Activar/desactivar | X | como hoy (`disabled` ↔ `proposed`) |
| Aceptar y pasar al siguiente | Shift+A | A + `Tab`: revisar una lista de silencios sin soltar el teclado |
| Borrar | Supr | como hoy |
| Editar | Enter F2 | como hoy |
| Deseleccionar | Esc | como hoy |
| Deshacer / rehacer | Ctrl+Z Ctrl+Shift+Z | ver §4 |

#### 3.1 «Aceptado» en los recortes (tecla A)

Las capas ya tienen tres estados (`proposed`, `accepted`, `disabled`), pero el
documento de recortes solo guarda `enabled`, así que el adaptador de `editorial_layers`
muestra un recorte activo siempre como PROPUESTO. Para que A tenga sentido sobre un
recorte seleccionado (el de borde blanco):

- `trims.json` gana un campo **aditivo** `accepted: bool` (por defecto `false`) en
  `_normalize_cut`, `add_cut` y `apply_silence_analysis` (un nuevo análisis de silencios
  conserva `accepted` en los cortes que sobreviven, igual que conserva `edited`).
- Adaptador: `state = "disabled" si no enabled; "accepted" si accepted; si no "proposed"`.
  `persist` escribe `enabled = state != "disabled"` y `accepted = state == "accepted"`.
- **La exportación no cambia**: sigue cortando la unión de los `enabled`. «Aceptado»
  es una marca de revisión humana; el paquete de revisión para la AI
  (`write_review_package`) la expone para que la AI no vuelva a proponer sobre lo ya
  aceptado. En el carril, un item `accepted` se dibuja con borde de 2 px en el verde
  de acento; la barra de detalle ya muestra el badge ACEPTADO.

Vista:

| Acción | Tecla | Detalle |
|---|---|---|
| Zoom −/+ | − + | como hoy |
| Ver todo | Shift+Z | como hoy |
| Zoom a la selección | Z | Item seleccionado (o IN/OUT) con 10 % de margen |
| Seguir al playhead | F | Alterna el auto-scroll por saltos de página |
| Centrar playhead | C | |
| Saltar recortes al reproducir | Shift+T | El checkbox de RECORTES |

### 4. Deshacer / rehacer

`HistoryStack` (nuevo `editorial_history.py`, puro) con profundidad 50, por medio
cargado (se vacía al cambiar de video). `LayersController.persist()` es el único
punto de escritura, así que:

- Antes de escribir: `before = snapshot(lid)` — deep copy del documento afectado
  (`autor` → `reg.marcas`; `recortes` → `self.w.trims`; `bloques` → `self.w.plan`;
  capa propia → `store.layers[lid]`). Tras el éxito: `push(lid, before, after)`.
- `undo` restaura `before` **por los mismos caminos de guardado** (nunca escribiendo
  archivos a mano): capas propias `store.save(copia)` (la revisión sube, y la
  protección frente a respuestas de la AI sigue valiendo); recortes
  `editorial_trims.save_document`; bloques `editorial_chunks.apply_plan`; marcas del
  autor requieren un método nuevo `Registro.reemplazar(marcas)` que revalida, guarda
  y notifica como cualquier edición.
- Redo = reaplicar `after` igual. Si un documento cambió por fuera (revisión distinta
  a la del snapshot), la entrada se descarta con aviso en el status en vez de pisar.

### 5. Reglas de rendimiento (para que se sienta editor, no visor)

- Ningún temporizador nuevo: el tick de 16 ms sigue siendo el único bucle de
  animación. Cambiar de velocidad es **una** re-sesión con debounce; no hay hilos
  nuevos aparte de los que ya existen.
- Nada de trabajo por tecla que crezca con el proyecto: resolver un acorde es un
  lookup; saltar a un borde es un `bisect`; el paso de fotograma usa la caché exacta y
  el worker last-wins que ya existen.
- Sin `bind_all` de `<Motion>` ni asignaciones nuevas dentro del tick. Sin
  `time.sleep` en el hilo de UI.
- El skim a ×8 no decodifica más que fotogramas clave; ×2–×4 saltan B-frames.
- Puertas de aceptación (medidas con `tools/benchmark_preview.py --rates 1,2,3,4,8` en
  el VOD de referencia, 60 s por posición, tres posiciones):
  - ×1: desfase mediano ≤ 25 ms y P95 ≤ 35 ms (igual que hoy, sin regresión).
  - ×2–×4: cero respawns en 60 s y ≥ 20 frames mostrados por segundo de pared.
  - Cambio de velocidad: de la tecla al primer frame nuevo ≤ 400 ms (mediana).
  - Salto a borde / paso de fotograma: ≤ 30 ms de hilo de UI (sin decodificar).
  - Tick: ≤ 4 ms de media dentro de `_anim_tick` (se registra en `reproductor.jsonl`).

### 6. Opcional y con puerta: decodificación por hardware

Experimento aparte, detrás de `preview_hwaccel` en config.json (por defecto `off`):
`-hwaccel d3d11va` (Windows) / `auto` antes de `-i` en `VideoStream`, con caída
automática a software si el stream entra en FAILED durante el warm-up. Solo se activa
por defecto si el benchmark demuestra mejora en ×4 sin degradar ×1. Si no, queda
documentado como «probado, sin ganancia».

---

## Parte 2 · Prompt para la AI que lo implemente

> Copiar desde aquí hasta el final como prompt. Está escrito para una AI con acceso al
> repositorio en esta misma máquina Windows.

Vas a implementar la navegación tipo editor del Transcriptor (velocidad de reproducción,
atajos configurables, acciones rápidas, deshacer) siguiendo **exactamente** el diseño de
`docs/diseno-navegacion-editor.md`, Parte 1. No rediseñes: si algo del diseño no encaja
con el código real, para y explica el conflicto antes de escribir código.

### Antes de tocar nada

1. Lee, en este orden: `docs/referencia-tecnica.md` (§3.1, §5, §7),
   `docs/mediciones-reproductor.md`, `playback_clock.py`, `medios.py` (VideoStream,
   SesionVideo, Prefetcher, Reproductor), `editor_medios.py` entero, `editorial_layers_ui.py`,
   `automatico_ui.py` (`_build_workspace`, `_pump`, `_export_trims`), `app.py`
   (`_build_settings`, `_mostrar_vista`), `tests/test_playback.py`, `tests/test_layers.py`,
   `tools/benchmark_preview.py`, `tools/smoke_editorial_ui.py`.
2. Corre la suite completa y el benchmark de referencia **sin cambiar nada** y guarda
   el JSON como `media/bench/baseline.json` (carpeta ignorada por git):
   `runtimes\win-py313-*\Scripts\python.exe -B -m unittest discover -s tests` y
   `tools\benchmark_preview.py --source <VOD de referencia> --output media\bench\baseline.json`,
   con `PYTHONUTF8=1` y `shared\tools\ffmpeg` en PATH. Si la app está abierta, no
   reinstales la release (Windows bloquea `releases\<v>`); editar fuentes sí es seguro.

### Invariantes que NO se rompen (si una fase los exige, para y pregunta)

- El reloj de audio de FFplay es el único maestro. `AudioClock.position()` sigue
  devolviendo `None` con muestra vieja (> 0,5 s) y el tick nunca avanza por suposición.
- `_anim_tick` conserva su modelo: warm-up → reloj → frame → auto-scroll, a 16 ms.
  Ningún temporizador ni hilo nuevo para animación.
- `_stop_preview()` sigue siendo el único apagado; `_preview_epoch` y `_anim_tok`
  siguen invalidando callbacks tardíos. Sin ffmpeg/ffplay huérfanos jamás (Job Object).
- `VideoStream`: mismo framing por tamaño fijo, misma fórmula `ts = t0 + n/fps`, mismo
  buffer de 8 con backpressure. Solo cambian `fps` y las opciones de entrada de skip.
- `Prefetcher`, `FrameWorker`, caché de frames y tiles de waveform: sin cambios.
- Ninguna tecla se despacha cuando el foco está en un Entry/Text (m, i, o, x, letras y
  números deben poder escribirse). Nada de `bind_all` para `<Motion>`.
- `LayersController.persist()` sigue siendo el único punto de escritura de items; las
  validaciones existentes (`validate_items`, bloques cubren el medio, un rango por item
  en autor/recortes/bloques) se mantienen.
- No toques `podcast_export.py`, `editorial_projects.py`, `editorial_catalog.py` ni la
  exportación en general. No cambies el pie fijo del editor ni `LayerDetailBar`.
- Windows primero: pruebas reales en esta máquina. En CI Linux solo hay numpy y SDL
  dummy: la lógica de `keymap.py`, `editorial_history.py`, `AudioClock(rate)` y la
  construcción de comandos ffmpeg debe testearse sin Tk ni customtkinter.

### Fases (cada una: tests verdes + smoke + commit propio; no mezcles fases)

**Fase 1 — velocidad.** `AudioClock(start, rate)`; `Reproductor.play(..., rate)` con
estiramiento que conserva el tono (`rubberband`, y `atempo` si no está; sondeo único
cacheado como `filter_script_option`) y `volume=0` solo por encima de
`preview_audio_max_rate` (nueva clave en `hardware._DEFAULTS`, 4.0: el usuario debe
oír lo que dicen a ×2–×4 para juzgar un recorte); `VideoStream(fps=VS_FPS/rate, skip)` con `-skip_frame bidir`
desde ×2 y `nokey` desde ×6; `SesionVideo(rate)`; en `EditorMedios`: `rate`, `SPEEDS`,
`set_rate` con debounce de 150 ms, `_play`/`_restart_audio` propagan `rate`, `lbl_t` y
status muestran `×N`. Teclas provisionales L/J/K/1-4 en `_tl_key` (la Fase 2 las mueve
al keymap). Tests: reloj escalado y perdido; comando de mezcla con 1 y 3 pistas a ×1,
×2 y ×4 (atempo, volume, orden de opciones); `VideoStream` fps y flags por rate;
`SesionVideo` re-ancla con rate. Benchmark `--rates 1,2,3,4,8` con las puertas del
diseño §5. Si ×4 produce respawns en el VOD de referencia, baja ese caso a `nokey` y
repórtalo; no aflojes la puerta.

**Fase 2 — keymap.** `keymap.py` puro (ACTIONS, parse/format, `chord_from_event`,
load/save/reload, conflictos) + tests de parseo, normalización Shift/CapsLock,
modificador Alt en Windows y Linux, conflictos y overrides parciales. `_dispatch` en
`EditorMedios` con la guarda de foco y `_keys_activos`; `acciones_extra` reemplaza a
`teclas_extra` (migra Automático). Migración 1:1 de las teclas actuales: la app debe
comportarse igual que antes con el keymap por defecto. Sección «Atajos» en Ajustes con
Grabar/×/Restaurar y conflictos en rojo. Smoke Tk: pulsar teclas con el foco en un
Entry no dispara acciones; con el foco en el timeline sí; cambiar un atajo en Ajustes
aplica sin reiniciar.

**Fase 3 — navegación.** Paso de fotograma, bordes (índice ordenado + bisect, con
cache por las mismas claves que `LayersController._cache_key`), silencios, ir a tiempo,
inicio/fin de selección, zoom a selección, seguir/centrar. Tests puros del índice de
bordes y del parser de «ir a tiempo». Medición: salto a borde ≤ 30 ms de UI.

**Fase 4 — edición y deshacer.** Dividir, recortar inicio/fin, empujar, item
anterior/siguiente, play desde el item, aceptar con A / Shift+A (con el campo
`accepted` de los recortes según §3.1: aditivo, por defecto `false`, la exportación
sigue usando solo `enabled`), `editorial_history.py` + `Registro.reemplazar`.
Todo por `persist`. Tests: dividir/recortar en cada tipo de carril (autor, recortes,
bloques, capa propia) con sus validaciones; A sobre un recorte persiste `accepted` y
no altera `enabled_intervals`; un nuevo análisis de silencios conserva `accepted`;
undo/redo restauran documentos idénticos y se descartan si la revisión cambió por
fuera. Smoke Tk de una sesión completa: S, [, ], Alt+→, A, Shift+A, Ctrl+Z,
Ctrl+Shift+Z.

**Fase 5 (opcional, con puerta).** `preview_hwaccel` según §6. Solo si el benchmark
muestra ganancia en ×4 sin regresión en ×1 y con caída a software probada; si no,
déjalo `off` y documenta los números.

### Verificación obligatoria antes de cada commit

- `unittest discover -s tests` completo; `tools\smoke_editorial_ui.py`;
  `tools\benchmark_preview.py` con las puertas de §5 comparadas con `baseline.json`.
- Comprobar en `shared\logs` que no quedan procesos ffmpeg/ffplay tras cerrar la app.
- Actualizar docs en el mismo commit: `docs/referencia-tecnica.md` (§3.1 velocidad y
  despacho de teclas, §5 invariantes nuevos, §7 estado), `docs/guia-automatico.md`
  (tabla de atajos completa, nota de Ajustes → Atajos), `docs/historial.md` (entrada al
  final). Mensajes de commit en español, estilo del repo (verbo en presente, primera
  línea ≤ 80 caracteres), con el trailer de coautoría que use el repositorio.
- Al terminar la última fase: `VERSION` → `0.3.3` («prepara 0.3.3»). Reinstalar con
  `actualizar-release.bat` solo con la app cerrada; no crees el tag.

### Cuándo parar y preguntar

- Una puerta de rendimiento no se cumple y la única salida es aflojarla.
- Un invariante de arriba estorba a una fase.
- Tk en Windows reporta mal un modificador (Alt) para un atajo por defecto: propón el
  acorde alternativo (Ctrl/Shift) en vez de improvisar.
- El estiramiento de audio no llega a tiempo real en la máquina de referencia (FFplay
  se queda sin datos y el reloj se pierde): pasa de rubberband a atempo para esa
  velocidad y documenta; no silencies por debajo de ×4 ni cambies el reloj.
- Tests de la Fase 1 deben cubrir: comando con rubberband, comando con atempo cuando
  el sondeo dice que no hay rubberband, y `volume=0` solo a ×8.
