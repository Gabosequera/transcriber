# Navegación y edición tipo editor: velocidad, atajos, herramientas de mouse y capas de la AI — diseño 2026-09-06

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
| Deshacer / rehacer | Ctrl+Z · Ctrl+R (también Ctrl+Shift+Z, Ctrl+Y) | Cualquier cambio escrito desde el timeline: items, pedidos, marcadores, orden de carriles, capas. Ver §4 |

Herramientas y selección múltiple (detalle en §7 y §8):

| Acción | Tecla | Detalle |
|---|---|---|
| Alternar herramienta Selección ↔ Corte | B | Una sola tecla de toggle: entra en Corte y, pulsada otra vez, vuelve a Selección. Acción `tools.toggle_cut`, configurable en Ajustes → Atajos como todas |
| Volver a Selección | V | Explícita, por si se prefiere una tecla por herramienta (`tools.select`) |
| Seleccionar todo el carril | Ctrl+A | Todos los items del carril del item seleccionado (o bajo el playhead) |
| Añadir capa | Ctrl+N | Selector de tipo (Recortes / Pedidos para la AI); se inserta encima del carril seleccionado (§10) |
| Mover la selección | ← → | Con items seleccionados las flechas mueven la selección ±1 fotograma (Shift ±10) y el playhead la sigue; sin selección mueven el playhead como hoy. Alt+←/→ siempre mueve la selección |
| Sobre varios: activar/desactivar, aceptar, borrar | X A Supr | Aplican a todo el conjunto en una sola escritura (una entrada de deshacer) |

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

### 4. Deshacer / rehacer (Ctrl+Z · Ctrl+R)

Regla: **todo cambio que se hace desde el timeline y queda escrito en el proyecto se
puede deshacer y rehacer**; lo que no escribe nada (reproducir, mover el playhead,
zoom, seleccionar, cambiar de herramienta) no entra al historial. Cubre, entre otros:
crear/mover/estirar/dividir/fusionar/borrar items en cualquier carril, escribir o
cambiar el texto de un pedido (diálogo o campo de prompt de la marca), cambiar estado
o aceptación, mover un marcador del autor, subir o bajar un carril, crear, renombrar,
recolorear o borrar una capa o un carril de recortes, importar una propuesta.

`HistoryStack` (nuevo `editorial_history.py`, puro): pila de **operaciones** con
profundidad 50, por medio cargado (se vacía al cambiar de video; no se persiste).
Cada operación es `{label, targets: [(doc_id, revision_antes)], before, after}` donde
`before`/`after` son snapshots profundos de los documentos que toca, y `doc_id` es uno
de: `autor` (lista de marcas del Registro), `trims` (documento completo, lanes
incluidos), `plan`, `layer:<id>`, `lanes` (`views/lanes.json`). Una operación puede
tocar varios documentos (borrar un lane moviendo sus cortes a `main` toca `trims` y
`lanes`): es **una** entrada.

- Los puntos de escritura son pocos y todos registran: `persist`, `persist_many`,
  `store.save`/`store.delete` (vía un envoltorio en el controlador), el guardado de
  `lanes.json`, `Registro.editar/agregar/borrar` desde la UI (a través de
  `Registro.reemplazar`), la importación de propuestas. Cada uno hace
  `history.record(label, targets, before, after)` tras escribir con éxito.
- `undo` restaura `before` **por los mismos caminos de guardado** (nunca escribiendo
  archivos a mano): capas `store.save(copia)` (la revisión sube y la protección frente
  a respuestas de la AI sigue valiendo; deshacer un borrado guarda `deleted: false`,
  cosa que una respuesta de la AI no puede hacer); recortes
  `editorial_trims.save_document`; bloques `editorial_chunks.apply_plan`; marcas
  `Registro.reemplazar(marcas)` (método nuevo que revalida, guarda y notifica como
  cualquier edición); orden `lanes.json` por su guardado. Después: `refrescar_layout`,
  barra de detalle y selección (se reselecciona el item afectado si sigue existiendo).
- Redo (Ctrl+R; también Ctrl+Shift+Z y Ctrl+Y) reaplica `after` igual. Una acción
  nueva tras un undo descarta la rama de redo, como en cualquier editor.
- Si un documento cambió por fuera (su revisión ya no es la que la entrada esperaba,
  por ejemplo la AI escribió mientras tanto), la entrada se descarta con aviso en el
  status en vez de pisar. El status dice qué se deshizo («Deshecho: mover 3
  recortes»).
- Los campos de texto: el diálogo de pedido registra al guardar (una entrada por
  cierre, no por tecla); el prompt de la marca (`e_prompt`) registra al confirmar
  (Return o perder el foco), como hoy persiste. Ctrl+Z dentro de un campo de texto
  sigue siendo el deshacer del propio campo (Tk), no el del proyecto: la guarda de
  foco del keymap ya lo garantiza.

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
- Hit-test, hover y marquesina consultan el índice ordenado que ya existe
  (`LayersController.visible_parts`, bisect sobre `_draw_indexes`): O(log n + k) por
  evento aunque haya miles de recortes. Nada de recorrer `layer["items"]` entero por
  movimiento del mouse.
- Operaciones sobre varios items = **una** escritura por documento (`persist_many`),
  **un** `refrescar_layout` y **una** entrada de deshacer. Mover 50 recortes no puede
  costar 50 guardados de `trims.json`.
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

### 7. Edición con el mouse: dos herramientas, como en un NLE

Hoy el gesto de un carril (`LayersController.gesture`) hace todo a la vez: arrastrar en
vacío crea (y abre el diálogo), arrastrar el cuerpo mueve, arrastrar un borde estira. Se
reemplaza por **dos herramientas explícitas**, con estado `tool` en el controlador y una
barra pequeña en la columna libre del transporte (columna 4 de `fr_transporte`): dos
botones tipo segmento «Selección» y «Corte». Se cambia con el mouse en la barra o con
**una sola tecla de toggle** (`tools.toggle_cut`, por defecto B: entra en Corte y,
pulsada de nuevo, vuelve a Selección); `tools.select` (V) vuelve siempre a Selección.
Las dos son acciones del keymap y se reconfiguran en Ajustes → Atajos como cualquier
otra. Esc no cambia de herramienta (solo deselecciona). El cursor del canvas cambia
(`arrow` / `crosshair`) al pasar por un carril según la herramienta, y la barra resalta
la activa. El scrub sobre las pistas de audio no cambia con la herramienta.

**Selección (V), la predeterminada.**

- Click sobre un item: lo selecciona (borde blanco). Shift+click: añade o quita del
  conjunto. Click en vacío: deselecciona.
- Arrastrar desde vacío: **marquesina** (rectángulo punteado sobre el canvas, tag
  `layer-marquee`). Al soltar, quedan seleccionados los items cuyo rango corta el
  intervalo de tiempo de la marquesina en los carriles que el rectángulo cubre
  verticalmente. Consulta `visible_parts` por carril (bisect), nunca la lista entera.
- Arrastrar desde un item seleccionado: mueve **todo el conjunto** el mismo delta,
  con previsualización punteada de cada item durante el arrastre y una única
  `persist_many` al soltar (validación de todos antes de escribir; si uno falla, no se
  mueve ninguno y el status explica cuál).
- **Bordes = recorte con afordancia, como en DaVinci.** Al pasar el mouse a ≤ 8 px del
  inicio o del final de cualquier item del carril (esté o no seleccionado), el cursor
  cambia a `sb_h_double_arrow` y ese borde se resalta (línea vertical de 3 px en el
  color del item, más clara); sobre el cuerpo el cursor es `fleur` (mover) y en vacío
  `arrow`. Arrastrar el borde lo lleva hacia donde vayas: agranda o encoge el item por
  ese lado, con previsualización punteada del nuevo rango y una etiqueta flotante en el
  canvas («0:12.3 → 0:14.0 · +1,7 s»). El otro borde no se mueve. Al soltar, `persist`
  con las validaciones de siempre (un borde no cruza al otro: mínimo un fotograma; en
  «Bloques» mover el límite ajusta al vecino como hoy). Si el item mide menos de 24 px
  en pantalla, las zonas de borde se reducen a un tercio de su ancho para que el
  cuerpo siga siendo arrastrable, y un doble click sigue abriendo el editor para
  ajustar tiempos a mano. Con varios seleccionados, arrastrar un borde recorta solo ese
  item. Sustituye al comportamiento actual de `hit()` con modo `start`/`end`, que ya
  existe pero sin cursor, sin resaltado y solo con handles en el seleccionado.
- El cursor del canvas se cambia **solo cuando cambia el estado** (vacío / cuerpo /
  borde / herramienta), nunca en cada evento de movimiento, y el hover reutiliza el
  mismo `hit()` indexado que la barra de detalle.
- Con selección activa: X, A, Supr, Enter (solo con uno) y las flechas actúan sobre el
  conjunto (§3). Esc vacía la selección. Ctrl+A selecciona todo el carril.

**Corte (B).** Pensada para el carril de recortes, pero vale para cualquier carril
editable; los bloques solo admiten mover límites (cobertura continua), así que en
«Bloques» esta herramienta solo estira/encoge límites y nunca crea, resta ni divide.

- Arrastrar una caja y soltar: nace un item con ese rango. Lo que pasa después depende
  del **tipo de carril** (§10): en un carril de **recortes** no se abre ningún diálogo
  (queda creado, en naranja, origen `user`; Enter o doble click lo editan cuando haga
  falta); en un carril de **pedidos para la AI** se abre el diálogo con el foco ya
  puesto en el campo del pedido, sin tocar el mouse, y Escape guarda y cierra.
- **Solapes = uno solo.** Si el item creado, movido o estirado se solapa con otros del
  mismo carril, se funden en uno (unión de rangos). Vale para las tres herramientas y
  para las importaciones (silencios y propuestas de la AI dentro de su carril). Los que
  solo se tocan por el borde siguen separados. Detalle en §10.
- Si la caja empieza o termina dentro de un item existente del carril, ese item **se
  estira** a la unión de ambos rangos. Si la caja toca varios, se funden en uno (el
  primero sobrevive con la unión; los demás se borran). Comentarios: se conserva el del
  superviviente; los otros se anexan al suyo separados por « · ».
- **Shift + arrastrar = restar.** Para cada item del carril que corte la caja: si la
  caja lo cubre entero, se borra; si toca un solo borde, se recorta ese borde; si queda
  estrictamente dentro, el item **se divide en dos** (dos recortes, dos regiones o dos
  tramos del item, según el carril; conservan etiqueta, comentario, estado y `accepted`).
- **Ctrl + arrastrar desde un item = mover** ese item (aunque la herramienta sea Corte).
  Ctrl + arrastrar en vacío hace scrub como en las pistas.
- Un click sin arrastre (< 4 px, misma regla que las marcas) selecciona el item bajo el
  cursor para poder usar X/A/Supr sin cambiar de herramienta.
- Previsualización durante el gesto: caja punteada blanca (crear/estirar) o roja
  (restar), más el contorno del resultado sobre los items afectados. Un solo redibujo
  por evento de movimiento; el timeline completo solo se redibuja al soltar.
- Opcional, misma fase si sale barato: imán a límites seguros (`BoundaryIndex`) dentro
  de ±0,15 s al crear o estirar, con tecla N para alternarlo. Si complica el gesto, se
  deja para después.

Reglas comunes: cada gesto termina en `persist` / `persist_many` con las validaciones
de siempre (`validate_items`, bloques contiguos, un rango por item en autor/recortes/
bloques). Nada muta los documentos vivos hasta soltar. Deshacer restaura el gesto
completo como una sola entrada.

### 8. Selección múltiple en el modelo

- `LayersController.selected` (la terna `(lid, item_id, segmento)`) pasa a ser el item
  **primario** de una lista `selection: list[tuple]` ordenada por tiempo. Todo el código
  que hoy lee `selected` sigue funcionando; el nuevo código itera `selection`.
- Solo se seleccionan items de **un mismo carril** a la vez (X, A y Supr tienen
  semántica por carril). Una marquesina que cubre varios carriles selecciona en el
  carril con más items dentro; el status lo dice.
- Semántica en lote: X → si todos están `disabled`, todos a `proposed`; si no, todos a
  `disabled`. A → si todos están `accepted`, todos a `proposed`; si no, todos a
  `accepted` (a los `disabled` también los activa). Supr → borra todos.
- `persist_many(lid, items, *, delete=False)`: valida cada item, aplica todo en una copia
  del documento, escribe una vez, refresca una vez, y empuja una sola entrada al
  historial. Para «autor» usa `Registro.reemplazar` (§4); para «recortes»,
  `save_document` único; para capas propias, un `store.save`.
- Dibujo: los seleccionados se pintan como hoy (borde blanco de 2 px, handles); el
  primario lleva además un punto en el borde superior para saber a cuál aplica Enter.

### 9. Capas de la AI: temas, subtemas y cortes sugeridos

Objetivo: cuando la heurística de silencios ya está revisada, pasarle a la AI la
conversación para que separe temas y subtemas y proponga cortes de contenido, y verlo
todo en carriles separados sin que cambie la manera de exportar.

**Qué existe.** La Tarea 2 de la skill escribe `trims.proposed.json`; la app lo valida y
funde sus cortes en el **mismo** `trims.json` con `origin: "ai"` (violeta), y la
exportación corta la **unión** de todos los activos: heurística, AI y tuyos ya «se
suman». La Tarea 3 produce una única capa `topics` con jerarquía por `parent_id`. Las
respuestas de capas (`editorial-layer-proposal`) traen **una** capa y `validate_layer`
solo acepta `kind` `user` o `topics`.

**Diseño (sin cambiar la autoridad de los datos).**

- **Carriles de recortes = «lanes» del mismo `trims.json`** (§10). Cada corte lleva un
  campo aditivo `lane`; el documento declara sus carriles en `lanes: [...]`. Dos vienen
  de fábrica: `main` («Recortes»: silencios de la heurística y cortes tuyos) y `ai`
  («Cortes sugeridos (AI)», violeta, encima del anterior). `merge_proposal` escribe en
  `ai`; `apply_silence_analysis` en `main`. Mismas operaciones en todos (mover, estirar,
  X, A, Supr, dividir, fusionar solapes). La exportación no cambia: unión de `enabled`
  de todo el documento, carril aparte. El campo `accepted` de §3.1 vale igual para los
  cortes de la AI: A alterna aceptado / no aceptado.
- **Temas arriba, subtemas debajo.** La capa `topics` se guarda como hoy (una capa,
  jerarquía y protecciones intactas) pero `lanes()` la **presenta** como varios
  carriles por profundidad: «Temas» (`parent_id` nulo), «Subtemas» (profundidad 1),
  «Subtemas 2» si hubiera más niveles. El carril sabe a qué `layer_id` pertenece;
  `persist` no cambia. Mover un tema no arrastra a sus subtemas (son items
  independientes con rangos propios, como hoy).
- **Orden de carriles** por defecto, de arriba abajo: Marcas del autor · Bloques ·
  Temas · Subtemas · Cortes sugeridos (AI) · Recortes · capas propias y de la AI. El
  orden real vive en `views/lanes.json` (§10): lista ordenada de ids de carril, solo
  presentación, reconstruible; los carriles nuevos se insertan donde diga la regla de
  «encima del seleccionado» y ▲▼ en «Capas y comentarios» lo reordenan. El nombre del
  carril se pinta como hoy en la esquina.
- **La skill puede crear las capas que necesite.** La respuesta de capas admite
  `layers: [...]` además de `layer` (compatibilidad), cada una fundida con
  `merge_response` y sus protecciones (items editados y borrados por el humano nunca se
  pisan). Nuevo `kind: "ai"` en `validate_layer` para capas auxiliares de la AI
  («Momentos», «Preguntas abiertas», lo que decida), editables como las propias y
  borrables desde «Capas y comentarios» (la tumba persistente evita que resuciten).
  Ninguna capa de `layers/` participa del corte: el único documento que corta es
  `trims.json`.
- **Un solo pedido a la AI.** Botón «Preparar revisión editorial» que escribe a la vez
  la solicitud de temas (pasada 1, `editorial_topics.prepare`) y el paquete de revisión
  de recortes (`write_review_package`, que ahora expone `accepted` y `origin` de cada
  corte), y `views/editorial-agent-request.md` con el orden: Tarea 3 (dos pasadas) y
  después Tarea 2 usando el mapa de temas como contexto, sin duplicar recortes ya
  aceptados. La app ya importa sola cada `*.proposed.json` al aparecer (sondeo de
  `_pump`); no hace falta un importador nuevo. La skill documenta esto como Tarea 4.
- La barra de detalle y el tooltip muestran el origen («AI», «silencio», «tuyo») junto
  al estado; el badge ACEPTADO ya existe.

### 10. Capas que añade el usuario: tipos, posición, solapes y diálogo

**Dos tipos al pulsar «Añadir capa»** (en «Capas y comentarios» y con la acción
`layers.new_lane`, Ctrl+N, que abre el mismo selector):

| Tipo | Dónde vive | Qué pasa al dibujar una caja |
|---|---|---|
| **Recortes** | Un carril (`lane`) nuevo dentro de `trims.json` | Nace un corte `user` sin diálogo; cuenta para la exportación como cualquier otro |
| **Pedidos para la AI** | Una capa `kind: "user"` en `layers/` (las de hoy, con nombre claro) | Se abre el diálogo con el foco en el pedido; Escape guarda y cierra |

Es la única distinción específica por ahora; más tipos entrarían por la misma tabla.

**Recortes como carriles del documento único.** `trims.json` gana dos campos
aditivos: `lanes: [{lane_id, name, color}]` y, en cada corte, `lane`. Los archivos
antiguos cargan sin migración: `lane` ausente se deriva del origen (`ai` → `ai`, lo
demás → `main`) y `lanes` ausente equivale a los dos de fábrica. Un carril de recortes
creado por el usuario es una entrada más en `lanes`; borrarlo desde «Capas y
comentarios» mueve sus cortes a `main` o los borra (la app pregunta). `adapters`
produce una capa de UI por lane y `persist` escribe en el lane del carril. La
exportación, «Saltar recortes al reproducir» y las estadísticas siguen leyendo el
documento completo: **la unión de los `enabled` de todos los lanes**.

**Posición: encima de la capa seleccionada.** El carril seleccionado es el del último
click en el timeline (`LayersController.selected[0]`, exista item o no). Al añadir una
capa de cualquier tipo, `views/lanes.json` la inserta justo antes de ese carril; sin
selección, va arriba de los carriles de recortes. `lanes.json` es una lista de ids
(`autor`, `bloques`, `topics:<id>:0`, `topics:<id>:1`, `trims:ai`, `trims:main`,
`trims:<lane_id>`, `layer:<layer_id>`); ids desconocidos se descartan al cargar y los
carriles nuevos sin entrada se colocan en su posición por defecto.

**Solapes dentro de un carril se funden en uno.** Regla `coalesce(lane)` aplicada en
`persist`/`persist_many` tras crear, mover, estirar o dividir, y en las importaciones
(`apply_silence_analysis`, `merge_proposal`): dos cortes del mismo lane cuyos rangos se
solapan estrictamente (`a.t_ini < b.t_fin and b.t_ini < a.t_fin`) se reemplazan por uno
con la unión. El **actor** (el corte que el usuario acaba de crear o mover; en las
importaciones, el más largo) impone `origin`, `enabled` y `accepted`; `reason` se
concatena con « · » sin duplicados; la evidencia de la AI se conserva si alguno la
tenía; el `cut_id` superviviente es el del actor. Los que solo se tocan por el borde no
se funden. Es idempotente y O(n) sobre el lane ordenado, así que también sanea
documentos viejos al cargar (sin escribir hasta la primera edición). Un solape entre
carriles distintos no se toca: la unión ya la hace la exportación.

**Diálogo de pedido para la AI.** `edit_dialog(focus="comment")`: al abrirse por una
creación en una capa de pedidos, el cursor queda en el campo del pedido (sin click).
`Escape` guarda y cierra (si la validación falla, muestra el error y sigue abierto);
`Ctrl+Enter` guarda y cierra; cerrar con la X de la ventana también guarda lo escrito
(un item recién creado con pedido vacío se conserva igual, como hoy). El foco vuelve al
timeline al cerrar. Abierto desde Enter o doble click, el comportamiento es el mismo.

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
   (`_build_settings`, `_mostrar_vista`), `editorial_layers.py` (LayerStore, adapters,
   merge_response), `editorial_trims.py` (merge_proposal, write_review_package,
   enabled_intervals), `editorial_topics.py`, `skills/transcriptor/SKILL.md`,
   `tests/test_playback.py`, `tests/test_layers.py`, `tests/test_trims.py`,
   `tests/test_topics.py`, `tools/benchmark_preview.py`, `tools/smoke_editorial_ui.py`.
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
  exportación en general: lo que se corta sigue siendo la unión de los `enabled` de
  `trims.json`, sin importar en qué carril se vean ni qué capas haya en `layers/`.
  No cambies el pie fijo del editor ni `LayerDetailBar` (solo añade el origen al texto).
- `trims.json` sigue siendo el único documento de recortes: los carriles de recortes,
  de fábrica o añadidos por el usuario, son `lanes` del mismo archivo. La capa `topics`
  sigue siendo una sola capa en `layers/` (los carriles por profundidad son
  presentación). `views/lanes.json` es solo orden, reconstruible. Los cambios de
  esquema son aditivos (`accepted`, `lane`, `lanes`, `kind: "ai"`, `layers: [...]` en
  la respuesta) y todo archivo antiguo debe seguir cargando sin migración (`lane` se
  deriva del origen).
- La fusión de solapes (§10) nunca cruza carriles y nunca cambia el resultado de
  `enabled_intervals` dentro de un carril (la unión de los activos es la misma antes y
  después); un test lo demuestra con documentos aleatorios.
- Ningún cambio escrito desde el timeline queda fuera del historial (§4): si añades un
  punto de escritura, registra la operación y prueba su undo/redo en el mismo commit.
- Las protecciones de `merge_response` (items editados o borrados por el humano nunca
  se pisan; tumba persistente de capas borradas) se aplican a cada capa de una
  respuesta múltiple exactamente igual que hoy a una.
- Nada muta un documento vivo durante un arrastre: previsualizar en el canvas, validar
  y escribir al soltar. Un gesto que falla deja todo como estaba y lo dice en el status.
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
Todo por `persist`. El historial cubre desde esta fase **todos** los puntos de
escritura que ya existen: items, texto de pedidos (diálogo y `e_prompt`), marcas del
autor, crear/renombrar/recolorear/borrar capas desde «Capas y comentarios», importar
propuestas. Tests: dividir/recortar en cada tipo de carril (autor, recortes,
bloques, capa propia) con sus validaciones; A sobre un recorte persiste `accepted` y
no altera `enabled_intervals`; un nuevo análisis de silencios conserva `accepted`;
undo/redo restauran documentos byte a byte idénticos para cada punto de escritura
(incluido borrar una capa y deshacerlo con `deleted: false`), una operación
multi-documento es una sola entrada, la rama de redo se descarta tras una acción
nueva, y una entrada se descarta si la revisión cambió por fuera. Smoke Tk de una
sesión completa: S, [, ], Alt+→, A, Shift+A, escribir un pedido y cerrarlo, Ctrl+Z
tres veces, Ctrl+R tres veces, y Ctrl+Z con el foco en un Entry deshace solo el texto.
**Regla para las fases siguientes:** toda mutación nueva (marquesina, mover el
conjunto, fusionar solapes, lanes, `lanes.json`, respuestas multicapa) se registra en
el historial y trae su test de undo/redo; una fase sin eso no se acepta.

**Fase 5 (opcional, con puerta).** `preview_hwaccel` según §6. Solo si el benchmark
muestra ganancia en ×4 sin regresión en ×1 y con caída a software probada; si no,
déjalo `off` y documenta los números.

**Fase 6 — selección múltiple y herramientas de mouse (§7, §8).** Primero el modelo:
`selection` + `selected` primario, `persist_many`, semántica en lote de X/A/Supr, y
tests puros de la aritmética de gestos (unión al estirar, fusión de varios, resta que
recorta un borde, resta que divide, borrado por cobertura total, bloques que solo
mueven límites) sobre documentos sintéticos de cada carril. Después la UI: barra de
herramientas en la columna libre del transporte, acciones `tools.toggle_cut` (B, un
toggle) y `tools.select` (V) en el keymap y visibles en Ajustes → Atajos, cursores,
marquesina, arrastre del conjunto con previsualización, recorte por bordes con cursor
`sb_h_double_arrow`, borde resaltado, previsualización y etiqueta de tiempo (zonas de
borde reducidas en items estrechos), Corte con Shift y Ctrl, click corto que
selecciona.
Hit-test y marquesina por `visible_parts`. Incluye `coalesce(lane)` (§10) con tests:
solape estricto se funde con las reglas del actor, contacto por el borde no, idempotente,
y `enabled_intervals` idéntico antes y después sobre documentos aleatorios. Crear en un
carril de recortes no abre diálogo; crear en una capa de pedidos abre `edit_dialog`
con el foco en el pedido y Escape guarda (test Tk: `focus_get()` es el campo del
pedido; Escape persiste el texto). Smoke Tk: marquesina de 3 recortes → X → los
tres desactivados con una sola revisión nueva de `trims.json`; arrastre del conjunto →
un solo guardado; caja que pisa dos recortes → queda uno; Shift+caja dentro de un
recorte → dos recortes que conservan `accepted`; Ctrl+arrastre en Corte → mueve;
hover a 5 px del final de un recorte no seleccionado → `tl.cget("cursor")` es
`sb_h_double_arrow` y el borde está resaltado; arrastrar ese borde 40 px → solo
`t_fin` cambia, el inicio no, y la etiqueta flotante mostró el delta; en un item de
15 px el cuerpo sigue moviéndose desde su centro; Ctrl+Z deshace cada gesto entero.
Medición:
mover 200 recortes seleccionados ≤ 100 ms desde soltar hasta redibujado; hover con
5.000 recortes en el carril sin tocar la lista entera (perfilar `hit`).

**Fase 7 — capas de la AI y carriles (§9, §10).** `lanes`/`lane` en `trims.json` con
carga de archivos antiguos (lane derivado del origen) y adaptador por lane; carriles
por profundidad de `topics`; `views/lanes.json` con la regla «encima del seleccionado»
y ▲▼ en «Capas y comentarios»; «Añadir capa» con los dos tipos (Recortes → lane nuevo;
Pedidos para la AI → capa `user`) y la acción `layers.new_lane`; borrar un lane de
recortes pregunta si mover sus cortes a `main` o borrarlos; `kind: "ai"`; `layers: [...]` en la
respuesta (con `layer` aún aceptado); `write_review_package` expone `accepted` y
`origin`; botón «Preparar revisión editorial» y `views/editorial-agent-request.md`;
Tarea 4 en `skills/transcriptor/SKILL.md` (Tarea 3 en dos pasadas, luego Tarea 2 con el
mapa de temas, sin duplicar cortes aceptados). Tests: un `trims.json` con los tres
orígenes se reparte en dos carriles y `enabled_intervals` no cambia; un `trims.json`
sin `lane` carga y exporta igual que antes; un lane nuevo del usuario recibe sus cortes
y la exportación los une con los demás; `lanes.json` inserta encima del seleccionado y
descarta ids desconocidos; una propuesta con
dos capas se funde respetando items editados y borrados; una capa `ai` se puede borrar
y no resucita; una capa `topics` con dos niveles produce dos carriles y `persist` desde
el carril «Subtemas» escribe en la misma capa; el paquete de revisión lista `accepted`.
Smoke Tk: importar `trims.proposed.json` con cortes de AI → aparecen en el carril
superior, A los acepta, y «Aceptar y exportar cortes» ignora los `disabled` de ambos
carriles (verificar con `exports.json` en un medio sintético).

### Verificación obligatoria antes de cada commit

- `unittest discover -s tests` completo; `tools\smoke_editorial_ui.py`;
  `tools\benchmark_preview.py` con las puertas de §5 comparadas con `baseline.json`.
- Comprobar en `shared\logs` que no quedan procesos ffmpeg/ffplay tras cerrar la app.
- Actualizar docs en el mismo commit: `docs/referencia-tecnica.md` (§3.1 velocidad y
  despacho de teclas, §5 invariantes nuevos, §7 estado), `docs/guia-automatico.md`
  (tabla de atajos completa, nota de Ajustes → Atajos, tabla «Trabajar en el carril»
  con las dos herramientas, sección de carriles de la AI y del botón «Preparar revisión
  editorial»), `skills/transcriptor/SKILL.md` (Tarea 4 y `layers: [...]`),
  `docs/historial.md` (entrada al final). Mensajes de commit en español, estilo del repo (verbo en presente, primera
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
