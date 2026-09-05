# Transcriptor

> **Licencia:** uso no comercial bajo PolyForm Noncommercial 1.0.0. Vender,
> monetizar o incorporar este software a un producto o servicio comercial requiere
> una licencia escrita separada. Ver `LICENSE.md` y `COMMERCIAL-LICENSE.md`.

App de escritorio Linux/Windows con dos modos de trabajo:

- **Automático** (predeterminado): pipeline editorial multipista para videos largos.
- **Manual**: las herramientas anteriores de transcripción, limpieza, metadata y marcas.

## Podcasts · voz y propuestas externas

1. Importa el video y marca las pistas que contienen voces (pueden compartir pista).
   Pulsa **Procesar pistas de voz**. La app extrae audio mono 16 kHz, transcribe con
   Whisper, alinea palabras con MMS y extrae risa, intensidad y emoción acústica.
   Las etapas se guardan para reanudar sin repetir inferencia ya completada.
2. Fuera de la app, entrega a tu AI la skill incluida en
   [`skills/transcriptor/SKILL.md`](skills/transcriptor/SKILL.md) y la carpeta del proyecto.
   Por ejemplo: «Usa la skill transcriptor para proponer cortes del podcast en esta carpeta».
   Puedes agregar esa carpeta de skill al directorio de skills de tu herramienta de AI.
3. La AI lee la conversación completa y las señales, y escribe
   `editorial/views/cuts.proposed.json`. Propone cambios de tema con objetivo de hasta
   **45 minutos y máximo de 50 minutos** por bloque. El número de bloques es variable.
4. Con el proyecto abierto, la app detecta ese archivo cada dos segundos; también
   puedes usar **Importar plan JSON externo**. Valida su identidad y cobertura y ajusta
   bordes hasta 15 segundos sin atravesar palabras ni risas. El timeline muestra cada
   bloque en un color distinto y marca sus límites. **Revisar chunks** permite modificar
   títulos/límites y consultar la confianza antes de aceptar.
5. Pulsa **Aceptar y exportar cortes** y elige la carpeta de salida. Se crean videos
   MP4 H.264/AAC numerados, con todas las pistas de audio, y un registro del plan aceptado.
   Para fuentes de solo audio se generan M4A. El archivo original se conserva.
   La exportación recodifica para cortar con precisión de fotograma; su velocidad depende
   de resolución, duración y CPU. Puedes cancelarla: no publica una carpeta incompleta.

Para volver a un proyecto, importa su video y pulsa **Abrir proyecto existente** para
seleccionar el master JSON. No hace falta repetir el análisis para cargar otra propuesta.
La app no ejecuta Codex ni requiere una API de lenguaje para extraer esta metadata.
Las emociones son estimaciones de activación, dominancia y valencia, no diagnósticos
ni identificación de hablantes. El análisis de gameplay/cara no participa en este flujo.

Archivos del proyecto:

```text
<proyecto>/editorial/
  <nombre>.editorial.master.json   # metadata combinada, tiempos absolutos del video
  tracks/A/                      # una carpeta por pista de voz
    audio.flac
    words.aligned.json
    words.json
    utterances.json
    laughter.json
    arousal.json
    intensity.json
    emotions.json
  views/
    conversation.md
    conversation-signals.md
    chunk-agent-request.md       # contrato y source_master_digest para la AI
    cuts.proposed.json           # salida de la AI externa
    chunks.json                 # propuesta validada que pinta el timeline
  chunks/chunk-001/              # transcript y señales por bloque tras importar
```

`cuts.proposed.json` usa `schema: "editorial-chunks/1"`, `source_master_digest`,
`planner` y `chunks`. Cada bloque lleva ID, inicio/final en segundos, título, resumen,
razones, referencias de intervenciones, confianza y advertencias. El contrato completo
se genera en `chunk-agent-request.md`. Se rechazan planes de otra metadata, huecos,
solapes, tiempos no finitos y bloques de más de 3000 segundos. La AI debe escribir el
JSON completo de forma atómica. No edites directamente el master ni `chunks.json`.

También funciona sin interfaz:

```bash
python editorial_pipeline.py run video.mkv --project-dir proyecto --track '0=Voces'
python editorial_pipeline.py apply-chunks proyecto/editorial/video.editorial.master.json proyecto/editorial/views/cuts.proposed.json
```

Los modos anteriores de planificación interna siguen disponibles con `--chunker codex`
o `--chunker local`; el predeterminado es `external`. El borrador local usa señales
léxicas y pausas y requiere revisión semántica.

Validación de desarrollo: `python -m unittest discover -s tests -v`. Las pruebas usan
metadata simulada y medios sintéticos con FFmpeg; no certifican la exactitud de Whisper
ni la calidad editorial de una AI sobre un podcast real de tres horas. La instalación
Windows y la alineación CTC se verifican además en el workflow de Windows.

## Modo Manual

El modo Manual conserva las herramientas anteriores. Ajustes vive en el menú global:

1. **Transcribir** — audio → **palabras + timestamps** (whisper + alineación forzada MMS,
   ~50 ms de precisión). La base de TODO lo demás.
2. **Limpiar audio** — silencia respiraciones/ruidos entre frases usando el `words.json`,
   **sin cambiar la duración** (3 modos; el Quirúrgico usa una red dedicada).
3. **Extraer metadata** — TODO el pipeline de metadata en UN wizard de 3 pasos sobre la
   grabación multitrack de OBS (reemplaza a las viejas pestañas Metadata / Audio de
   fondo / Cara / Master):
   - **Paso 1 · Inputs**: el video → **timeline unificado estilo editor** (preview del
     frame + un carril de forma de onda por pista con picos + RMS, un solo playhead
     para audio y video; click/arrastrar para moverse, ←/→ ±0.5s, Shift ±5s, espacio =
     play/stop de la mezcla con mute/solo real; el frame se actualiza también DURANTE
     la reproducción). **Navegación tipo editor**: `+`/`-` o Ctrl+rueda = zoom,
     rueda / arrastre con el botón del medio = moverse, `Shift+Z` = ver todo.
     **Marcas del autor**: `M` deja un punto en el playhead, `I`/`O` (o arrastrar en
     el carril de marcas) crean una región, `X` cicla nota → INCLUIR → EXCLUIR
     (decisiones de corte explícitas: qué va sí o sí y qué no va en el video final),
     `Supr` borra; a cada marca le podés escribir un PROMPT para la AI («de aquí a
     aquí quiero…»). Se guardan solas en `<video>.marcas.json` — podés marcarlas
     ANTES de extraer nada, y la AI que edita las recibe como órdenes tuyas (vista
     `directivas.md` + stream `autor.marcas` del master). En el lienzo marcás con
     RECTÁNGULOS qué zona es el JUEGO y cuál tu CÁMARA (si se solapan, el hueco se le
     resta al juego y queda registrado); a cada pista de audio le asignás su rol
     (voz / juego / chat / aux / ignorar).
   - **Paso 2 · Configuración**: todas las opciones (transcripción, emoción/risa/
     pausas/Ava, diálogo/sonidos/descripción del juego, cara + calibración de mirada,
     nombre del master) con **presets globales**.
   - **Paso 3 · Salida**: elegís la carpeta y corre TODO el pipeline con barra total +
     barra del paso actual + consola. **Reanudable**: si se corta (crash, cancelar,
     hasta apagón), al volver retoma desde lo ya calculado sin pisar nada bueno; lo
     que no se pudo correr se reporta con su motivo. Logs completos en `logs/`
     (últimos 5 intentos).
   Salidas: `voz.flac`/`juego.flac` (pistas normalizadas a la misma línea de tiempo),
   `voz.metadata.json`, `juego.fondo.json`, `<video>.cara.json` + sidecars,
   `<nombre>.master.json` (v2 + `header.media_layout` con regiones/roles/offsets) y
   las **vistas** para la IA.
4. **Marcar** — revisión DESPUÉS de extraer: importás un video ya procesado y la app
   encuentra sola su `master.json` (aunque hayas movido el video: la identidad es por
   contenido, no por ruta). Sobre el timeline ves la **metadata pintada como carriles
   de colores** (habla, risa, emoción, sonidos del juego…, solo lectura, con tooltips)
   y marcás regiones/puntos con instrucciones VIENDO esa metadata — mismas teclas y
   mismo `<video>.marcas.json` que el paso 1 del wizard, incluso con las dos pestañas
   abiertas a la vez. El panel derecho es el **GUION** (`<video>.guion.md`): tu texto
   libre + los bloques de cada marca (que la app regenera sola desde las marcas — ahí
   no se editan, se editan desde el timeline). **«Preparar paquete para la AI»**
   regenera master + vistas con tus marcas al día; el paquete final para la IA que
   edita es `master.json` + `guion.md` (+ `marcas.json` y `vistas/`).

El menú global **⋮ → Ajustes** contiene la configuración de hardware
(CPU/GPU/hilos) y memoria; no es una pestaña del modo Manual.

Herramientas CLI del flujo de edición (las usa la IA, ver skill /clipear):
`vistas.py` (vistas + dossiers), `edl.py` (EDL con gate de bordes + render + revisión
del rough cut), `consolidar.py`, `ava.py`, `cara.py`.

## Pestaña «Transcribir»

Genera (a partir de un audio):
- `<nombre>.words.json` — cada palabra con `start`/`end` en segundos → **la fuente de verdad para sync**
- `<nombre>.segments.json` — frases con timestamps
- `<nombre>.srt` — subtítulos
- `<nombre>.cues.md` — guion legible con `[mm:ss.d]` para anotar dónde entra cada gráfica

## Pestaña «Limpiar audio» (silenciar respiraciones)

Usa el `words.json` (lo busca automáticamente junto al audio) para mutear todo lo que no es voz:
las respiraciones que tomás al inicio de las frases desaparecen, pero la **duración total no
cambia** → el mismo `words.json` sigue valiendo para el sync (no hay que re-transcribir).

Motor v3: **gate/ducker con envolvente asimétrica** (no un mute duro). Usa un *release*
largo tras cada palabra para que las colas mueran natural, y **atenúa** (ducking) los huecos
a un piso en dB en vez de silencio absoluto — la respiración queda enterrada pero el audio
sigue "vivo". Si querés silencio total, bajá el nivel a −60 dB.

Controles (calibralos con el oído):
- **pre** (0.08s) — margen *antes* de cada palabra. Muy chico se come el ataque de consonantes suaves (f, s).
- **post** (0.22s) — margen *después* (colas de s/n finales).
- **min-gap** (0.4s) — huecos ≥ esto se consideran "largos" (respiraciones); menores, "cortos".
- **attack** (0.02s) — subida rápida justo antes de que vuelva el habla.
- **release** (0.25s) — bajada lenta tras el habla → colas de palabra naturales.
- **floor** (−26 dB) — cuánto bajar los huecos **largos**. `−26` = ducking (audio vivo);
  `−60` ≈ silencio total.
- **short** (0 dB) — atenuación de los huecos **cortos** (respiración pegada entre frases).
  `0` = no tocar; probá `−8` a `−12` si se cuela aire entre palabras.

Botón **«Analizar huecos»**: lista cada hueco (tiempo, duración, largo/corto, cuántos dB se
le aplicarían y qué palabra va antes/después) **sin escribir audio** — para revisar antes de aplicar.
Un **♪** marca los huecos con sonido audible (probables respiraciones) como sanity check.

### Tres modos (selector arriba de los ajustes)

- **Quirúrgico** (recomendado, *Respiro-en × whisper*): una **red neuronal** entrenada para detectar
  respiraciones marca **solo las respiraciones** y las cruza con whisper (descarta las que solapan
  una palabra: son fricativas tipo "s", no aire). Toca **únicamente** respiraciones confirmadas —
  no gatea nada más → máxima fidelidad, **sin "cortes" raros a mitad de frase**. Solo `floor` y
  `release`. Es el mejor modo para narración limpia.
- **VAD** (*Silero × whisper*): detecta la voz **acústicamente** (~30 ms) y la cruza con las
  palabras. Gatea **todo lo que no es voz** (respiraciones + ruiditos: sillas, clicks de boca,
  papeles). Útil cuando además de aire hay ruidos varios. Solo `floor` y `release`.
- **Avanzado**: bordes por palabra + heurísticas (los 7 sliders). Máximo control manual.

En modo quirúrgico hay dos perillas propias:
- **floor** — cuántos dB baja cada respiración (el "cuánto muteo"). −40/−45 = casi silencio pero
  con un piso natural; −60 ≈ silencio total.
- **Sensibilidad Respiro (threshold)** — qué tan sensible es la red. Más **BAJO** = detecta **más**
  respiraciones (útil si se le escapa alguna); `0.064` es el default del paper; subilo a `0.3-0.5`
  si empieza a agarrar eses/jotas aspiradas del español.

**Setup del modo quirúrgico** (una vez): cloná el repo del modelo dentro del proyecto —
`git clone https://github.com/ydqmkkx/Respiro-en.git` — y asegurate de tener `librosa` +
`intervaltree` (ya en `requirements.txt`). Si el repo no está, la GUI simplemente no ofrece ese
modo. Corre en CPU (procesa el audio por trozos de 15 s → rápido y con mejor detección: el modelo
se entrenó con frases cortas, así que darle audios largos de una sola pasada le hace perder
respiraciones). El modelo es en inglés, pero una respiración suena igual en cualquier idioma →
funciona en español (verificado).

**El escudo de palabras** (toggle, solo en modo quirúrgico): protege el habla — descarta cualquier
respiración detectada que caiga sobre una palabra de whisper (evita comerse una "s" que la red
confunda con aire). Dos controles:
- **Escudo ON** + slider **«Encoger bordes whisper»** (0-150 ms, default 60): recorta cada palabra
  hacia adentro antes de comparar. Whisper "estira" el final de las palabras sobre el aire, así que
  encoger libera las respiraciones que quedaban escondidas en ese padding. Subilo si se te escapan
  respiraciones pegadas a una palabra.
- **Escudo OFF**: se atenúa **toda** respiración que detecte la red (máxima limpieza). Si eso te
  come alguna consonante, subí el `threshold` para que la red sea más estricta.

**Red de seguridad (aire en pausas)** — toggle, **apagado por defecto**: la red neuronal a veces
no ve algún soplido (le da score cero). Este segundo detector, sin IA, busca ruido de aire
**solo en los silencios entre palabras** y lo atenúa también. Para marcar algo exige DOS cosas a la
vez: que haya ruido de aire (broadband, tipo "shhh") Y que supere un piso de volumen — así una
pausa callada normal o un resto de voz **no se tocan** (no repite el problema del modo Avanzado).
Slider **«Sensibilidad aire»**: más bajo caza aire más sutil; subilo si llegara a tocar algo de voz.

**Si una respiración no se atenúa:** Analizá y mirá el log. Si dice `N descartada(s) por solapar
palabras`, subí «Encoger bordes» o apagá el escudo. Si aun así no aparece y es un soplido que la
red no ve (score 0), prendé la **red de seguridad**.

VAD requiere `silero-vad` + `torch` (CPU) — ya instalados. Si faltara algo, ese modo no aparece y
podés usar los otros igual.

**Flujo:** transcribí el audio crudo (con respiraciones) → words.json → pestaña «Limpiar audio»
→ *Analizar* para revisar → *Limpiar* (misma duración). Ajustá `floor`/`min-gap`/`pre` y repetí.

## Lanzar la app

```bash
cd /data/transcriber
./run.sh
```

`run.sh` prepara las librerías CUDA del venv y abre la ventana. Ahí:
1. Elegís **dispositivo** (GPU si hay, o CPU) — se ve cuál estás usando y podés cambiarlo.
2. **Importar audio** (wav/mp3/flac/m4a/mp4…).
3. **Modelo** (`tiny`→`large-v3`) e **idioma** (`es`).
4. Marcás qué **salidas** querés (`words.json` siempre sale).
5. **Ajustar timestamps con IA de alineación** (recuadro verde, recomendado): al terminar whisper,
   un 2º modelo (MMS) re-alinea las palabras al audio → tiempos precisos. Whisper transcribe bien
   pero sus tiempos de palabra son flojos (a veces pega palabra + silencio + respiración en una sola
   de varios segundos); esto lo corrige. **Las correcciones se aplican a TODAS las salidas**
   (words.json, segments.json, .srt, cues.md), así que todo queda sincronizado fino — mejor para
   limpiar respiraciones y para pegar gráficas. Corre en CPU (~2-3 min en audios largos; la 1ª vez
   descarga el modelo ~1.2 GB). Si lo desmarcás, se usan los tiempos crudos de whisper.
6. **Carpeta de destino** (por defecto, la del audio).
7. **Convertir** → barra de progreso + tiempo estimado + log en vivo (incluye la fase de alineación).

En Windows, `setup-windows.bat` realiza la primera instalación y `run.bat` abre siempre
la release estable activa. El launcher consulta GitHub, instala actualizaciones verificadas
y hace rollback automático si una versión nueva no consigue arrancar. Modelos, cachés,
configuración y runtimes viven fuera del código; consultar `INSTALL-WINDOWS.md`.

## Presets

Cada pestaña tiene una barra **Preset** arriba:
- **Guardar** — guarda la configuración actual con un nombre (modelo/idioma/dispositivo/salidas
  en Transcribir; todos los sliders en Limpiar audio). No guarda rutas de archivos, solo los ajustes.
- **Menú desplegable** — cambiá entre presets al instante; al elegir uno se aplican sus valores.
- **Eliminar** — borra el preset seleccionado, con **triple confirmación** para evitar accidentes.

En «Extraer metadata» el preset es **GLOBAL al wizard** (paso 2): guarda el estado de
TODAS las secciones de una — checks y modelos de cada análisis, las regiones del lienzo
(atadas a la resolución del video: solo se re-aplican si coincide) y la **calibración de
mirada por VALOR** (aplicar el preset restaura esa calibración exacta, aunque después
hayas recalibrado otra cámara).

Se guardan en `presets.json` junto a la app.

## Layout adaptable

- Ventana **angosta**: una sola columna (controles arriba, log abajo).
- Ventana **ancha** (≥ 1000 px) o pantalla completa: **dos columnas** — los controles/sliders a la
  izquierda y el log a la derecha, cada uno ocupando su mitad, para ver todo cómodo.

## Notas

- **GPU:** detecta automáticamente la NVIDIA. Con **4 GB de VRAM**, `medium` va bien;
  `large-v3` puede no caber — si la GPU se queda sin memoria, la app avisa y podés bajar de
  modelo o pasar a CPU.
- **CPU:** usa `int8` (lo razonable sin GPU). Modelos grandes serán lentos.
- El **primer uso** de cada modelo lo descarga una sola vez. En Windows administrado los
  cachés viven en `shared/cache`; en desarrollo se respetan los cachés del perfil.
- Corré la transcripción sobre el audio **ya editado** (después de recortar silencios), o los
  timestamps no coincidirán con el audio final.
- Palabras con `prob < 0.5` se marcan como aviso: revisalas en `words.json`.

## Recrear el entorno (si hiciera falta)

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

## Estructura

- `core.py` — lógica de transcripción (sin GUI). Reutilizable / importable.
- `app.py` — la interfaz (customtkinter, 5 pestañas).
- `editor_medios.py` — el mini-editor de video/timeline que comparten el wizard y «Marcar».
- `wizard_extraer.py` — la pestaña «Extraer metadata» (wizard de 3 pasos).
- `marcar.py` / `guion.py` — la pestaña «Marcar» y el documento del guion.
- `pipeline.py` — el orquestador headless del wizard (DAG con resume transaccional).
- `medios.py` — inspección/extracción de media (ffprobe, FLAC, waveform, audición).
- `run.sh` / `run.bat` — lanzadores (Linux / Windows).
- `bootstrap.py` / `launcher.py` / `updater.py` — selección de release, actualización
  GitHub transaccional y rollback.
- `app_paths.py` — única fuente de rutas para separar código y estado persistente.
- El resto de los módulos (metadata, risa, pausas, ava, fondo, cara, consolidar, vistas,
  edl…) están mapeados en `HANDOFF.md` §3; el panorama del auto-clipping y el flujo de la
  IA orquestadora, en `AUTOCLIP_HANDOFF.md`.
