# Guía del modo Manual

El modo Manual (menú ⋮ → **Manual**) conserva las herramientas originales del proyecto, las
del pipeline de auto-clipping para VODs de gameplay y reacción. Son cuatro pestañas;
**Ajustes** vive en el menú global y contiene hardware (CPU/hilos/GPU), el modelo Whisper
por defecto, memoria y las claves de API de los servicios online.

1. **Transcribir** — audio → palabras con timestamps (Whisper + alineación forzada MMS,
   ~50 ms de precisión). Es la base de todo lo demás.
2. **Limpiar audio** — silencia respiraciones y ruidos entre frases usando el `words.json`,
   sin cambiar la duración (tres modos; el Quirúrgico usa una red dedicada).
3. **Extraer metadata** — todo el pipeline multimodal en un wizard de tres pasos sobre la
   grabación multipista de OBS.
4. **Marcar** — revisión después de extraer: metadata pintada sobre el timeline, marcas
   con instrucciones y el guion para la IA que edita.

Herramientas de línea de comandos del flujo de edición (las usa la IA, ver la skill
`/clipear` del entorno de desarrollo): `vistas.py` (vistas y dossiers), `edl.py` (EDL con
gate de bordes, render y revisión del rough cut), `consolidar.py`, `ava.py`, `cara.py`.

## Transcribir

1. Elige el **dispositivo** (GPU si hay, o CPU); se ve cuál está en uso y se puede cambiar.
2. **Importar audio** (wav, mp3, flac, m4a, mp4…).
3. **Modelo** (`tiny` → `large-v3-turbo`; por defecto el elegido en Ajustes) e **idioma**.
4. Marca las **salidas** que quieras (`words.json` siempre sale).
5. **Ajustar timestamps con IA de alineación** (recomendado): al terminar Whisper, un
   segundo modelo (MMS) realinea las palabras al audio. Whisper transcribe bien pero sus
   tiempos de palabra son flojos (a veces pega palabra + silencio + respiración en una sola
   de varios segundos); MMS lo corrige, y la corrección se aplica a todas las salidas.
   Corre en CPU (2–3 min en audios largos; la primera vez descarga el modelo, ~1,2 GB).
6. **Carpeta de destino** (por defecto, la del audio) y **Convertir**: barra de progreso,
   tiempo estimado y log en vivo.

Salidas:

- `<nombre>.words.json` — cada palabra con `start`/`end` en segundos: la fuente de verdad
  para sincronizar.
- `<nombre>.segments.json` — frases con timestamps.
- `<nombre>.srt` — subtítulos.
- `<nombre>.cues.md` — guion legible con `[mm:ss.d]` para anotar dónde entra cada gráfica.

Notas:

- **GPU:** detecta la NVIDIA automáticamente. `large-v3-turbo` tiene calidad de `large`,
  es mucho más rápido y cabe en 4 GB con int8. Si la GPU se queda sin memoria, la app avisa
  y se puede bajar de modelo (Ajustes) o pasar a CPU.
- **CPU:** usa int8. Los modelos grandes serán lentos.
- El primer uso de cada modelo lo descarga una sola vez. En Windows administrado los cachés
  viven en `shared\cache`; en desarrollo se respetan los cachés del perfil.
- Transcribe el audio ya editado (después de recortar silencios), o los timestamps no
  coincidirán con el audio final.
- Las palabras con `prob < 0.5` se marcan como aviso: revísalas en `words.json`.

## Limpiar audio (silenciar respiraciones)

Usa el `words.json` (lo busca automáticamente junto al audio) para atenuar todo lo que no
es voz. Las respiraciones al inicio de las frases desaparecen, pero la **duración total no
cambia**: el mismo `words.json` sigue valiendo para sincronizar.

Motor: gate/ducker con envolvente asimétrica (no un mute duro). Usa un *release* largo tras
cada palabra para que las colas mueran naturales y **atenúa** los huecos a un piso en dB en
vez de silencio absoluto: la respiración queda enterrada pero el audio sigue vivo. Para
silencio total, baja el nivel a −60 dB.

Controles (calíbralos con el oído):

- **pre** (0,08 s) — margen antes de cada palabra. Muy chico se come el ataque de
  consonantes suaves (f, s).
- **post** (0,22 s) — margen después (colas de s/n finales).
- **min-gap** (0,4 s) — huecos ≥ esto se consideran «largos» (respiraciones); menores,
  «cortos».
- **attack** (0,02 s) — subida rápida justo antes de que vuelva el habla.
- **release** (0,25 s) — bajada lenta tras el habla: colas de palabra naturales.
- **floor** (−26 dB) — cuánto bajar los huecos largos. −26 = ducking (audio vivo);
  −60 ≈ silencio total.
- **short** (0 dB) — atenuación de los huecos cortos (respiración pegada entre frases).
  0 = no tocar; prueba −8 a −12 si se cuela aire entre palabras.

**Analizar huecos** lista cada hueco (tiempo, duración, largo/corto, dB que se le aplicarían
y qué palabra va antes y después) sin escribir audio, para revisar antes de aplicar. Un ♪
marca los huecos con sonido audible (probables respiraciones).

### Tres modos

- **Quirúrgico** (recomendado, Respiro-en × Whisper): una red neuronal entrenada para
  detectar respiraciones marca solo las respiraciones y las cruza con Whisper (descarta las
  que solapan una palabra: son fricativas tipo «s», no aire). Toca únicamente respiraciones
  confirmadas, sin cortes raros a mitad de frase. Solo `floor` y `release`, más dos perillas
  propias:
  - **floor** — cuántos dB baja cada respiración. −40/−45 = casi silencio con un piso
    natural; −60 ≈ silencio total.
  - **Sensibilidad Respiro (threshold)** — más bajo detecta más respiraciones; `0.064` es
    el valor del paper; súbelo a 0,3–0,5 si empieza a agarrar eses o jotas aspiradas.
- **VAD** (Silero × Whisper): detecta la voz acústicamente (~30 ms) y la cruza con las
  palabras. Atenúa todo lo que no es voz (respiraciones y ruidos: sillas, clicks de boca,
  papeles). Solo `floor` y `release`. Requiere `silero-vad` + `torch` en CPU; si faltan, el
  modo no aparece y los otros funcionan igual.
- **Avanzado**: bordes por palabra + heurísticas (los siete controles). Máximo control.

**Escudo de palabras** (solo en Quirúrgico): protege el habla descartando cualquier
respiración detectada que caiga sobre una palabra de Whisper.

- **Escudo ON** + **Encoger bordes Whisper** (0–150 ms, por defecto 60): recorta cada
  palabra hacia adentro antes de comparar. Whisper estira el final de las palabras sobre el
  aire; encoger libera las respiraciones escondidas en ese margen. Súbelo si se escapan
  respiraciones pegadas a una palabra.
- **Escudo OFF**: se atenúa toda respiración que detecte la red. Si se come alguna
  consonante, sube el `threshold`.

**Red de seguridad (aire en pausas)**, apagada por defecto: la red neuronal a veces no ve
algún soplido. Este segundo detector, sin IA, busca ruido de aire solo en los silencios
entre palabras y exige dos cosas a la vez: ruido de aire de banda ancha y un piso de
volumen, así una pausa callada o un resto de voz no se tocan. **Sensibilidad aire**: más
bajo caza aire más sutil; súbelo si llegara a tocar voz.

Si una respiración no se atenúa: analiza y mira el log. Si dice «N descartada(s) por
solapar palabras», sube «Encoger bordes» o apaga el escudo. Si es un soplido que la red no
ve (score 0), enciende la red de seguridad.

Setup del modo Quirúrgico en desarrollo (una vez): clona `https://github.com/ydqmkkx/Respiro-en`
dentro del proyecto y ten `librosa` + `intervaltree` (ya en `requirements.txt`). En la
instalación de Windows el componente viene en `shared\components`. Corre en CPU por trozos
de 15 s (el modelo se entrenó con frases cortas). Es un modelo en inglés, pero una
respiración suena igual en cualquier idioma; verificado en español.

Flujo: transcribe el audio crudo → `words.json` → Limpiar audio → **Analizar** para revisar
→ **Limpiar** (misma duración). Ajusta `floor`/`min-gap`/`pre` y repite.

## Extraer metadata (wizard de tres pasos)

Reemplaza a las viejas pestañas Metadata, Audio de fondo, Cara y Master.

- **Paso 1 · Inputs.** El video en un timeline unificado estilo editor: preview del frame,
  un carril de forma de onda por pista (picos + RMS), un solo playhead para audio y video,
  reproducción de la mezcla con mute/solo real (el frame se actualiza también durante la
  reproducción). Navegación tipo editor: `+`/`−` o `Ctrl`+rueda = zoom, rueda o arrastre
  con el botón central = desplazarse, `Shift`+`Z` = ver todo, `←`/`→` ±0,5 s (`Shift`
  ±5 s), espacio = play/stop.
  **Marcas del autor:** `M` deja un punto en el playhead, `I`/`O` (o arrastrar en el
  carril de marcas) crean una región, `X` cicla nota → INCLUIR → EXCLUIR (decisiones de
  corte explícitas: qué va sí o sí y qué no va), `Supr` borra; a cada marca se le puede
  escribir un **prompt** para la AI («de aquí a aquí quiero…»). Se guardan solas en
  `<video>.marcas.json`, antes de extraer nada, y la AI que edita las recibe como órdenes
  (vista `directivas.md` y stream `autor.marcas` del master). En el lienzo se marcan con
  rectángulos qué zona es el juego y cuál la cámara (si se solapan, el hueco se le resta al
  juego); a cada pista de audio se le asigna su rol (voz / juego / chat / aux / ignorar).
- **Paso 2 · Configuración.** Todas las opciones (transcripción; emoción, risa, pausas,
  instrucciones a Ava; diálogo, sonidos y descripción del audio del juego; cara y
  calibración de mirada; nombre del master) con presets globales del wizard.
- **Paso 3 · Salida.** Carpeta de salida y ejecución de todo el pipeline con barra total,
  barra del paso actual y consola. **Reanudable:** si se corta (crash, cancelar, apagón),
  al volver retoma desde lo ya calculado sin pisar nada bueno; lo que no se pudo correr se
  reporta con su motivo. Logs en `logs/` (últimos cinco intentos).

Salidas: `voz.flac`/`juego.flac` (pistas normalizadas a la misma línea de tiempo),
`voz.metadata.json`, `juego.fondo.json`, `<video>.cara.json` y sidecars,
`<nombre>.master.json` (v2, con `header.media_layout`: regiones, roles y offsets) y las
vistas para la IA.

## Marcar (revisión después de extraer)

Importa un video ya procesado y la app encuentra sola su `master.json` (aunque el video se
haya movido: la identidad es por contenido). Sobre el timeline se ve la metadata pintada
como carriles de colores (habla, risa, emoción, sonidos del juego…), solo lectura, con
tooltips; y se marcan regiones y puntos con instrucciones viendo esa metadata, con las
mismas teclas y el mismo `<video>.marcas.json` que el paso 1 del wizard, incluso con las
dos pestañas abiertas a la vez.

El panel derecho es el **guion** (`<video>.guion.md`): texto libre más los bloques de cada
marca, que la app regenera desde las marcas (ahí no se editan; se editan desde el
timeline). **Preparar paquete para la AI** regenera master y vistas con las marcas al día.
El paquete final para la IA que edita es `master.json` + `guion.md` (más `marcas.json` y
`vistas/`).

## Presets

Cada pestaña tiene una barra **Preset** arriba:

- **Guardar** — guarda la configuración actual con un nombre (modelo, idioma, dispositivo y
  salidas en Transcribir; todos los controles en Limpiar audio). No guarda rutas de
  archivos, solo ajustes.
- **Menú desplegable** — cambia entre presets al instante.
- **Eliminar** — borra el preset seleccionado, con triple confirmación.

En Extraer metadata el preset es global al wizard (paso 2): guarda el estado de todas las
secciones (checks y modelos de cada análisis, las regiones del lienzo atadas a la
resolución del video, y la calibración de mirada por valor). Se guardan en `presets.json`
en la carpeta de configuración por máquina.

## Layout adaptable

- Ventana angosta: una sola columna (controles arriba, log abajo).
- Ventana ancha (≥ 1000 px) o pantalla completa: dos columnas, controles a la izquierda y
  log a la derecha.
