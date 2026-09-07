# Guía del modo Automático (podcasts)

El modo Automático convierte una grabación larga con varias pistas de voz en un paquete
editorial revisable: transcripción alineada por pista, conversación global, bloques por
tema propuestos por una AI, recortes de silencios y de contenido, y la exportación de los
videos ya cortados. La regla de todo el flujo: **la app y la AI proponen; la persona
decide en el timeline; el video se corta solo al pulsar exportar**.

## Conceptos

- **Timeline canónica (T0).** Todos los tiempos son segundos absolutos del video original.
  Las pistas de audio de OBS pueden arrancar con desfase; la app las normaliza a T0 al
  extraerlas y nunca hay que sumar offsets a mano.
- **Pista de voz.** Cada pista marcada como VOZ se procesa por separado: Whisper, alineación
  MMS (timestamps de palabra de ~50 ms), risa, intensidad y emoción acústica. Una pista
  puede contener varias personas; sin diarización no se atribuye quién habla.
- **Master editorial.** `<nombre>.editorial.master.json`: todas las palabras y eventos una
  sola vez, más la conversación global con todas las pistas intercaladas por tiempo.
- **Bloques (chunks).** División del podcast en partes de hasta 50 minutos por cambio de
  tema. Los propone una AI externa y se revisan en la app.
- **Recortes.** Tramos «de aquí a aquí» que se quitarían del video: los propone la
  heurística de silencios (azul), la AI (violeta) o los dibuja la persona (naranja). Lo que
  se quita es la unión de los recortes activos.
- **Identidad por contenido.** Los proyectos se reconocen por fingerprint del medio, no por
  ruta: mover o copiar el video no rompe nada.

## 1. Importar y procesar las pistas de voz

1. **Importar video** (o audio). Aparece el timeline con una forma de onda por pista.
2. Marca **VOZ** en cada pista con voces y ponle nombre (por ejemplo «Gabriel + amigo 1»).
   Las dos primeras vienen marcadas por defecto.
3. En el panel **Pipeline editorial** elige el **modelo Whisper** (por defecto el de Ajustes,
   `large-v3-turbo`) y desmarca los pasos que no quieras en esta corrida: alineación MMS,
   intensidad + emoción, risa. Un paso desmarcado se omite y el master lo tolera.
4. Elige la **carpeta del proyecto** (por defecto una carpeta con el nombre del video, al
   lado del video) y pulsa **Procesar pistas de voz**.

Cada etapa publica sus archivos al terminar y guarda un manifest: si se cancela o se corta
la corriente, **Reanudar** retoma desde lo ya hecho. Whisper y MMS son pasos separados, así
que un fallo de MMS no repite la transcripción. Si la carpeta ya tiene trabajo previo, la
app pregunta **Retomar con lo ya hecho** o **Empezar de cero (reescribir)**.

Al terminar, **Conversación** abre `views/conversation.md`: todas las pistas intercaladas
con timecodes e IDs de intervención.

## 2. Bloques por tema con la AI

La app no ejecuta ninguna AI: prepara el paquete y espera su respuesta.

1. Fuera de la app, dale a tu AI la skill [`skills/transcriptor/SKILL.md`](../skills/transcriptor/SKILL.md)
   y la carpeta del proyecto. Por ejemplo: «Usa la skill transcriptor para proponer cortes
   del podcast en esta carpeta». La solicitud con el contrato y el `source_master_digest`
   está en `views/chunk-agent-request.md`.
2. La AI lee la conversación completa y escribe `views/cuts.proposed.json` con bloques por
   cambio de tema: objetivo hasta 45 minutos, máximo 50, número de bloques variable,
   cobertura continua de 0 al final. Puede añadir `topics` y `subtopics` por bloque; la
   segunda pasada de recortes los usa.
3. Con el proyecto abierto la app detecta ese archivo cada dos segundos (también con
   **Importar JSON de la AI**). Valida identidad y cobertura, rechaza huecos, solapes y
   bloques de más de 3000 s, y ajusta cada límite hasta 15 s para no partir palabras ni
   risas de ninguna pista. El timeline pinta cada bloque de un color con su título.
4. **Revisar chunks** permite editar títulos y límites y ver confianza y advertencias.
   Guardar vuelve a ajustar los bordes y regenera `chunks/<id>/` sin repetir análisis.
5. **Aceptar y exportar cortes** crea un video por bloque (sin recortes; ver §5 para la
   exportación con recortes).

Existen dos modos internos de planificación (`--chunker codex` con Codex CLI y
`--chunker local` con un borrador léxico) para uso desde la línea de comandos; el modo
por defecto de la app es `external` (la AI que tú elijas).

## 3. Recortes: silencios propuestos, revisados en el timeline

Sección **RECORTES** del panel derecho y carril «recortes» del timeline (debajo del carril
de bloques). Está disponible con o sin bloques.

**Analizar silencios** busca huecos sin palabras ni risas en ninguna pista, con colchones
alrededor de cada palabra (0,10 s antes, 0,20 s después) y de cada risa (0,25 s):

- `mín` = duración mínima del hueco para considerarlo (1,0 s por defecto);
- `margen` = silencio que se conserva a cada lado del recorte (0,3 s por defecto), para
  que el habla no quede pegada;
- además mide la **actividad acústica** de cada hueco sobre `tracks/<id>/audio.flac`
  (RMS en ventanas de 50 ms contra el piso de la pista). Un hueco con más de 12 dB sobre
  el piso —golpes, ruido, una risa que el detector no vio— se propone **desactivado**
  (contorno punteado) para que decidas.

Nada se corta. Los recortes aparecen en el carril y en la lista del panel («12 recortes ·
9 activos · 3:12 a quitar»). Volver a analizar con otros parámetros conserva lo que
editaste, lo que creaste y los que desactivaste.

### Trabajar en el carril

| Acción | Cómo |
|---|---|
| Seleccionar | Click sobre el recorte (borde blanco y handles) |
| Mover | Arrastrar el cuerpo |
| Estirar o encoger | Arrastrar un borde del recorte seleccionado |
| Crear uno tuyo | Arrastrar en una zona vacía del carril (queda en naranja) |
| Activar / desactivar | `X` o menú contextual |
| Editar comentario y rangos | Doble click |
| Borrar | `Supr` / `Retroceso` |
| Deseleccionar | `Esc` |
| Ver el motivo | Pasar el mouse por encima (tooltip) |
| Menú contextual | Click derecho: editar, activar/desactivar, borrar, ir al inicio |

Ayudas visuales: color por origen, borde blanco en la selección y contorno punteado
para items desactivados. Con muchos recortes se agrupan marcas indistinguibles por
píxel. **Saltar recortes al
reproducir** hace que la reproducción salte los tramos activos (re-arranca la sesión al
final de cada recorte; es una ayuda de revisión, no el render).

Todo se guarda solo en `views/trims.json` tras cada cambio. Los recortes se atan al medio
(fingerprint), así que sobreviven a un nuevo análisis de las pistas.

## 4. Segunda pasada: recortes de contenido con la AI

La heurística solo ve dónde hay y dónde no hay palabras. La pasada editorial es de la AI,
sobre el bloque ya acortado:

1. **Preparar revisión AI** escribe `views/trim-agent-request.md` (contrato y lista de
   bloques) y, por bloque, `chunks/<id>/trim-review.md` (o `views/trim-review.md` si no hay
   bloques): la conversación con los recortes activos marcados como `⟂ RECORTE`, más el
   tema, resumen, temas y subtemas del bloque. Si editas recortes después, el panel avisa
   «revisión AI desactualizada»; vuelve a prepararla.
2. Pídele a la AI la **Tarea 2** de la skill. Su criterio: quitar lo que no aporta a la
   conversación del bloque (tangentes que no llevan a nada, balbuceo, arranques en falso,
   charla técnica) y conservar diversión, continuidad y setups de un payoff posterior.
   **Nunca propone recortes por humor subido de tono, lisuras ni comentarios ofensivos**:
   eso lo quita el editor humano en post. Ante la duda, no recorta.
3. La AI escribe `views/trims.proposed.json`. La app lo importa sola (o con **Importar JSON
   de la AI**), exige el `source_master_digest` vigente, valida IDs y bloques, ajusta cada
   borde hasta 1,5 s para no partir palabras ni risas y pinta los recortes en violeta con
   su motivo. Una nueva propuesta reemplaza los recortes de la AI que no hayas tocado;
   los que moviste o desactivaste se conservan.

## 5. Cortar y exportar

**✂ Cortar y exportar** (sección RECORTES) pide una carpeta de salida y produce, dentro de
`podcast-<id>/`:

- `001.mp4`, `002.mp4`… un archivo por bloque (o uno solo si no hay bloques), con la
  unión de los recortes activos quitada, todas las pistas de audio con sus offsets, video
  H.264 (CRF 20) y audio AAC 192 kb/s. Fuentes de solo audio salen como `.m4a`.
- `accepted-plan.json`, `accepted-trims.json` (recortes y segmentos conservados) y
  `exports.json` (archivos, duración conservada y quitada por bloque).

El corte es preciso al fotograma: cada bloque se recodifica una sola vez con los segmentos
conservados concatenados, sin deriva entre audio y video aunque haya cientos de recortes.
El original nunca se modifica; una exportación terminada no se sobrescribe (cada
combinación de plan y recortes va a una carpeta distinta) y se puede cancelar sin dejar
carpetas a medias. La velocidad depende de la resolución, la duración y la CPU.

**Aceptar y exportar cortes** (botón inferior) hace lo mismo sin aplicar recortes.

El selector **Salida** (encima del botón, se recuerda entre sesiones) elige el formato de
los dos botones:

| Salida | Qué hace | Cuándo usarla |
|---|---|---|
| H.264 (compatible) | Recodifica (x264 CRF 20, AAC 192 kb/s), corte exacto | Por defecto: subir, compartir |
| HEVC (más pequeño) | Recodifica (x265 CRF 22), corte exacto | Mitad de tamaño; varias veces más lento en CPU |
| ProRes 422 HQ (edición) | Recodifica a 10 bits en `.mov`, audio PCM 24 bits | Seguir editando en Premiere/Resolve; archivos muy grandes |
| Copia exacta (sin recodificar) | Copia los streams tal cual, muy rápido | Footage raw/intra o cuando la calidad debe ser la del original |

La copia exacta conserva el códec, el contenedor y la calidad byte a byte, pero solo puede
empezar en un fotograma clave: cada límite entre bloques se mueve al clave anterior y lo
comparten los dos bloques (ni hueco ni solape); el log dice cuánto se movió y los proyectos
hijos heredan el tiempo real. Con originales intra (ProRes, DNxHR, raw, MJPEG) todo fotograma
es clave y el corte es exacto; con OBS (clave cada 2 s por defecto) se mueve como mucho 2 s.
No puede aplicar recortes: unir segmentos exige recodificar. Sin video, todos los formatos
producen `.m4a` AAC salvo la copia, que conserva el original.

## 6. Volver a un proyecto

Las exportaciones nuevas incluyen `projects/001/editorial/001.editorial.master.json`
(y uno por cada video). Importar ese hijo y abrir su master recupera solo su
transcripción y señales, en tiempos de su propio video. `derivation.segments`
permite volver a cada tramo del padre, incluso con recortes. Analizar silencios
extrae audio del hijo una vez para medir RMS; no ejecuta modelos de nuevo.

Importa cualquier padre o hijo: la app busca automáticamente su master por fingerprint
en la carpeta del medio y el conjunto vecino, carga el plan y los recortes sin repetir
análisis. Si hay metadata distinta para el mismo contenido, ofrece las versiones.
**Abrir proyecto existente** sigue disponible para metadata guardada fuera del conjunto.
Las rutas guardadas no se usan para exportar: se usa el video cargado.
El índice `.transcriptor/catalog.json` es una caché reconstruible con rutas relativas;
mover o copiar el conjunto conserva el descubrimiento. Copiar solo el video a otro
equipo requiere copiar también su proyecto: el video no contiene el transcript.

## Archivos del proyecto

**Analizar temas (dos pasadas)** prepara la Tarea 3 de la skill. Con un bloque
seleccionado analiza ese bloque; sin selección analiza todo el medio. Entrega a tu
AI `views/topics-agent-request.md`: su primera respuesta produce `topics-pass1.json`
y una solicitud actualizada; su segunda respuesta unifica recurrencias en una capa
de temas/subtemas con varios rangos por tema. Ambos JSON se importan automáticamente.
Doble click en el item permite corregir rangos y `parent_id`. Para repetir el bucle
con tus correcciones, pulsa otra vez Analizar temas. Las respuestas antiguas se rechazan.

```text
<proyecto>/editorial/
  <nombre>.editorial.master.json   # metadata combinada, tiempos absolutos del video
  run.json                         # resultado de la última corrida
  tracks/A/                        # una carpeta por pista de voz
    audio.flac                     # mono 16 kHz, normalizada a T0
    words.whisper.json             # checkpoint de Whisper
    utterances.whisper.json
    words.aligned.json             # tras MMS (copia si MMS está desmarcado)
    utterances.json
    words.json                     # enriquecido con intensidad y arousal
    laughter.json
    arousal.json
    intensity.json
    emotions.json
  views/
    conversation.md                # todas las pistas intercaladas
    conversation-signals.md        # lo mismo con niveles de risa/arousal/énfasis
    map.json                       # índices de IDs a tiempos
    chunk-agent-request.md         # contrato de la Tarea 1 (bloques)
    cuts.proposed.json             # salida de la AI (bloques)
    chunks.json / chunks.md        # plan de bloques validado
    trims.json                     # recortes revisables (silencio / AI / tuyos)
    trim-agent-request.md          # contrato de la Tarea 2 (recortes de contenido)
    trims.proposed.json            # salida de la AI (recortes)
    trim-review.md                 # revisión cuando no hay bloques
  chunks/chunk-001/                # por bloque, tras importar el plan
    transcript.md
    signals-summary.json
    laughter.json / arousal.json / intensity.json
    analysis-request.md
    moments.md                     # lo escribe la AI o el editor; nunca se pisa
    trim-review.md                 # conversación del bloque con los recortes marcados
  .work/                           # manifests de reanudación y selección de plan
```

## Contratos JSON para la AI

`cuts.proposed.json` (bloques):

```json
{
  "schema": "editorial-chunks/1",
  "planner": "<nombre de la AI>",
  "source_master_digest": "<de chunk-agent-request.md>",
  "chunks": [
    {"chunk_id": "chunk-001", "t_ini": 0.0, "t_fin": 2612.4, "title": "…", "summary": "…",
     "start_reason": "…", "end_reason": "…", "first_utterance_id": "A-u-000001",
     "last_utterance_id": "B-u-000412", "confidence": 0.8, "warnings": [],
     "topics": ["…"], "subtopics": ["…"]}
  ]
}
```

`trims.proposed.json` (recortes de contenido):

```json
{
  "schema": "editorial-trims-proposal/1",
  "planner": "<nombre de la AI>",
  "source_master_digest": "<de trim-agent-request.md>",
  "cuts": [
    {"chunk_id": "chunk-001", "t_ini": 1834.2, "t_fin": 1871.9,
     "first_utterance_id": "A-u-000412", "last_utterance_id": "B-u-000380",
     "reason": "tangente que no vuelve al tema", "confidence": 0.7}
  ]
}
```

`trims.json` (lo mantiene la app; no se edita a mano) usa `schema: "editorial-trims/1"`.
Cada recorte lleva `cut_id`, `t_ini`, `t_fin`, `origin` (`silence`, `ai`, `user`),
`enabled`, `edited`, `reason`, `confidence`, `chunk_id`, `evidence` y `warnings`.

Se rechazan planes de otra metadata, tiempos no finitos, huecos o solapes entre bloques,
bloques de más de 3000 s, IDs de intervención inexistentes y recortes fuera de rango. La AI
debe escribir cada JSON completo de forma atómica (temporal + renombrado).

## Línea de comandos

El pipeline funciona sin interfaz, con los mismos manifests y la misma reanudación:

```bash
python editorial_pipeline.py run video.mkv --project-dir proyecto --track "0=Voces" --track "1=Amigo"
```

```bash
python editorial_pipeline.py apply-chunks proyecto/editorial/video.editorial.master.json proyecto/editorial/views/cuts.proposed.json
```

Opciones de `run`: `--model` (Whisper), `--language`, `--device auto|cpu|cuda`,
`--no-align`, `--no-laughter`, `--no-prosody`, `--chunker external|codex|local`,
`--chunks N`, `--codex-model`, `--codex-timeout`, `--no-chunk-fallback`, `--rebuild`.

## Atajos del timeline

**Capas y comentarios** crea, renombra, cambia el color o elimina capas propias.
Puedes dejar marcas y pedidos antes de transcribir; se guardan en
`<nombre-del-video>/editorial/layers/` y se conservan al generar la metadata allí.
Para analizar temas hace falta el transcript del proyecto.
En cada carril arrastra en vacío para crear un rango, arrastra cuerpo/bordes para
moverlo/estirarlo y usa doble click para editar etiqueta, comentario, estado y lista
de tramos. Un item con varios rangos se selecciona como unidad; cada tramo se puede
ajustar por separado. `X` cambia estado, `Supr` borra, `Esc` deselecciona.
El menú derecho y el tooltip son comunes. Los bloques mantienen cobertura continua
y ajustan sus vecinos al cambiar un borde; dividir el plan usa **Revisar chunks**.
Las marcas envuelven el Registro compartido y los recortes mantienen `trims.json`:
no hay dos copias editables. **Preparar capas para AI** publica `views/layers.json`.

Las teclas funcionan con el foco en el timeline o en el preview (un click en cualquier
parte no interactiva del editor lo da); dentro de un campo de texto se escribe normal.
Todos los atajos se cambian en **Ajustes → Atajos** (Grabar captura la siguiente tecla,
× deja la acción sin atajo, Restaurar vuelve a los predeterminados; un conflicto se
muestra en rojo con el nombre de la otra acción) y aplican al instante. Se guardan por
máquina en `keymap.json`. En teclados latinos, `[` y `]` se escriben con AltGr y se
reconocen igual.

| Tecla / gesto | Acción |
|---|---|
| Click o arrastre sobre las pistas | Mover el playhead (scrub) |
| `←` / `→`, con `Shift` | ±0,5 s / ±5 s |
| `Inicio` / `Fin` | Ir al principio / al final |
| Espacio | Reproducir / detener la mezcla (con mute `M` y solo `S` por pista) |
| `K` | Pausa |
| `L` / `J` | Más rápido ×1→×2→×3→×4→×8 (pausado: reproduce a ×1) / más lento (a ×1 pausa) |
| `1` `2` `3` `4` | Velocidad exacta (arranca si estaba pausado) |
| `Shift` + `L` | Skim a ×8: solo fotogramas clave, audio mudo |
| `Shift` + Espacio | Reproducir desde el inicio del item seleccionado (o del IN) |
| `,` / `.`, con `Shift` | ±1 fotograma / ±10 fotogramas (según la tasa del medio) |
| `↑` / `↓` | Borde anterior / siguiente de cualquier carril (bloques, recortes, capas, marcas) |
| `Ctrl` + `↑` / `↓` | Silencio anterior / siguiente (solo recortes de la heurística) |
| `Ctrl` + `G` | Ir a tiempo: `1:23:45.6`, `5025`, `+30`, `-10` |
| `Shift` + `I` / `O` | Inicio / fin del item seleccionado (o del IN/OUT) |
| `+` / `−`, `Ctrl` + rueda | Zoom (la rueda centra en el cursor) |
| `Z` | Zoom al item seleccionado (10 % de margen) |
| `C` | Centrar el playhead |
| `F` | Seguir al playhead durante la reproducción (sí/no) |
| `Shift` + `T` | Saltar recortes al reproducir (el checkbox de RECORTES) |
| Rueda, arrastre con botón central | Desplazarse |
| `Shift` + `Z` | Ver todo |
| `M`, `I`/`O`, `X`, `Supr` en el carril de marcas | Marcas del autor (ver la guía del modo Manual) |
| Carril «recortes» | Ver la tabla de §3 |

**Velocidad.** Hasta ×4 se oye lo que dicen, solo más rápido y con el tono conservado
(el mismo estiramiento que hace DaVinci), para juzgar un recorte sin bajar la
velocidad; a ×8 el audio va mudo. El reloj muestra «0:12.3 ×2». Cambiar de velocidad
durante la reproducción reinicia la sesión desde el playhead (≈1 s en un VOD HEVC).
`preview_audio_max_rate` en `config.json` (4.0) baja el tope si en otra máquina el
estiramiento no llega a tiempo real.

## Límites y notas

- El video sigue el reloj de salida del audio. Ya no hace falta calibrar
  `av_offset_s`. El primer frame y la preparación del dispositivo pueden tardar;
  la barra informa ambas latencias. Si el video o su reloj fallan, se detiene.
- Volver a posiciones visitadas y recuperar waveforms usa cachés por contenido.
  Los saltos nuevos todavía requieren decodificar. Ver las
  [mediciones del VOD real](mediciones-reproductor.md).

- Las emociones son estimaciones acústicas de activación, dominancia y valencia; no son
  diagnósticos ni identifican hablantes.
- El análisis de cara, gameplay y visión no participa en este modo (es del modo Manual).
- El detector de risa puede dar falsos positivos con contenido real; la heurística de
  silencios los trata como voz (no los recorta), así que el resultado es conservador.
- Los parámetros de silencio (`mín`, `margen`) y el umbral de actividad de 12 dB están
  pensados para ajustarse con material real; revisa el carril antes de cortar.
