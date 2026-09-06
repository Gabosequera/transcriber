# Transcriptor editorial: especificación vigente

Última revisión: 29 de agosto de 2026.

Este documento contiene únicamente las decisiones vigentes. No es un historial de todas
las ideas discutidas. Si una propuesta futura contradice la Fase 1, manda la Fase 1.

## 1. Objetivo del producto

Convertir grabaciones largas —el caso inicial es un video de unas tres horas— en un
paquete editorial que un agente como Codex pueda entender y navegar eficientemente.

El flujo principal debe:

1. procesar todas las pistas de audio que el usuario marque como voz;
2. producir texto con timestamps precisos;
3. detectar risas;
4. medir arousal e intensidad vocal;
5. combinar cronológicamente el texto de todas las pistas de voz;
6. separar semánticamente la grabación en 3–4 chunks macro;
7. analizar después cada chunk con más detalle para encontrar momentos prometedores.

El objetivo inicial no es editar el video automáticamente. Es reducir tres horas de
material a bloques comprensibles y candidatos con evidencia y timecodes precisos.

## 2. Prioridad y fases

### Fase 1 — Primeros cambios funcionales reales

Implementación base completada el 28 de agosto de 2026. Falta la prueba de aceptación
con una grabación real cercana a tres horas; cualquier hallazgo de esa prueba sigue
perteneciendo a Fase 1. Esta fase tiene prioridad sobre el load balancer, el editor
multiclip y el resto del pipeline multimodal.

#### Fase 1A — Pipeline editorial local y multipista

- Importar un video largo e inspeccionar sus pistas de audio.
- Permitir seleccionar más de una pista como voz.
- Procesar cada pista seleccionada independientemente.
- Ejecutar exclusivamente los tres análisis editoriales definidos en la sección 4.
- Construir un transcript global sincronizado.
- Construir el master editorial canónico y sus vistas para el agente.
- Funcionar localmente sin otras máquinas ni Tailscale.

El destino principal de uso es Windows. La lógica y los artefactos deben seguir siendo
compatibles con Linux para desarrollo y para el futuro coordinador.

#### Fase 1B — Chunking macro con el agente

- Codex CLI recibe una sola conversación cronológica con todas las pistas de voz.
- Codex lee la grabación completa y propone 3–4 chunks semánticos globales.
- La aplicación refina cada límite contra palabras, risas e intervenciones de todas
  las pistas dentro de una ventana de búsqueda acotada.
- Validar cobertura completa, orden y ausencia de huecos o solapes accidentales.
- Permitir revisar o modificar los límites sin repetir inferencia de audio.
- El algoritmo local queda únicamente como fallback explícito y auditable.

#### Fase 1C — Análisis profundo por chunk

- Materializar transcript y señales de ambas pistas dentro de cada chunk.
- Analizar cada chunk por separado.
- Encontrar historias, intercambios, reacciones y momentos potencialmente buenos.
- Entregar candidatos con rango temporal, explicación y evidencia textual/acústica.

### Fase 2 — Procesamiento distribuido

Cuando la Fase 1 funcione correctamente en una sola máquina, distribuir sus tareas entre
los workers Windows y el coordinador Linux. El load balancer no debe definir ni retrasar
el formato del master o el chunking local.

### Fase 3 — Editor y automatización adicional

Quedan para después:

- varios archivos/clips movibles en un timeline;
- trim, snapping, ripple y edición automática;
- exportación a EDL/XML para un editor concreto;
- diarización del micrófono compartido;
- cualquier análisis visual o multimodal adicional.

## 3. Caso inicial confirmado

El primer caso real tiene dos pistas de voz:

| Pista | Contenido | Identidad disponible |
|---|---|---|
| A | Gabriel y un amigo comparten micrófono | `Gabriel + amigo 1` |
| B | Otro amigo usa un micrófono separado | `amigo 2` |

Las dos pistas se procesan de la misma manera. No se mezclan antes de analizarse.

Sin diarización adicional no se puede atribuir con fiabilidad cada intervención de la
pista A a Gabriel o al amigo 1. El master reservará `speaker_id`, pero será opcional y
podrá quedar en `null`. La diarización sería un cuarto análisis y no forma parte de la
Fase 1.

## 4. Los tres análisis editoriales

“Tres análisis” significa:

1. **ASR:** transcripción Whisper + alineación MMS.
2. **Risa:** detección frame-level.
3. **Prosodia:** arousal + intensidad acústica.

No se ejecutarán cara, visión, gameplay, descripción LALM, sentimiento, valence textual,
pausas, respiraciones ni instrucciones a Ava en este perfil.

Nombre del perfil: `editorial_voz`.

### 4.1 Transcripción y alineación

- Whisper genera frases, palabras y confianza de reconocimiento.
- MMS corrige `start` y `end` de cada palabra contra el audio.
- Los timestamps MMS pasan a ser la fuente temporal para las señales por palabra.
- `asr_prob` representa confianza de reconocimiento; no representa volumen, emoción ni
  importancia editorial.
- Cada palabra y frase recibe un ID estable que incorpora su `track_id`.

### 4.2 Risa

- Reutilizar el detector frame-level existente, con resolución aproximada de 20 ms.
- Guardar `t_ini`, `t_fin`, `mean_conf`, `max_conf` y `track_id`.
- Permitir risas simultáneas en pistas diferentes.
- No atribuir una risa de la pista A a una persona concreta sin diarización.

### 4.3 Arousal

- Usar únicamente la salida acústica de arousal del modelo de audeering.
- Separar este cálculo del sentimiento y las emociones derivadas del texto que hoy
  también ejecuta `metadata.py`.
- Calcular arousal en ventanas acústicas cortas y estables, con solape, limitadas a
  regiones de habla derivadas de MMS para no inferir sobre silencios largos.
- Agregarlo también por frase y asociarlo a las palabras que solapen cada ventana.
- Guardar el valor bruto y `arousal_z`, normalizado contra la misma pista.

### 4.4 Intensidad por palabra

“Intensidad” significa fuerza acústica de pronunciación. No es importancia semántica ni
confianza de Whisper.

Usando los límites MMS de cada palabra, calcular:

- RMS en dBFS;
- pico en dBFS;
- duración;
- piso acústico local antes y después;
- `intensity_z` relativo a la propia pista y al contexto cercano;
- `emphasis_score`, combinando intensidad relativa, duración y contraste contra el
  piso acústico local.

Los valores crudos se conservan para recalibrar sin repetir modelos. Arousal e intensidad
se normalizan por pista: micrófonos con ganancias distintas no son comparables mediante
dBFS bruto.

## 5. Pipeline exacto por pista

```text
1. Inspeccionar el medio y establecer la timeline canónica T0
2. Seleccionar pistas A y B como voz
3. Extraer cada pista a FLAC conservando su offset respecto a T0
4. Por pista, en paralelo cuando sea posible:
   a. Whisper
   b. MMS forced alignment
   c. risa
   d. arousal por ventanas
   e. intensidad por palabra después de MMS
5. Validar y committear artefactos por pista
6. Construir la conversación global
7. Construir el master y las vistas
8. Ejecutar chunking macro
9. Materializar y analizar cada chunk
```

Risa y arousal solo necesitan el FLAC y pueden correr mientras se completa la cadena
Whisper/MMS. La intensidad por palabra sí depende de los timestamps MMS.

Cada pista conserva su propio baseline, procedencia, versión de modelos y parámetros.

## 6. Conversación global sincronizada

La IA no leerá dos transcripts aislados. Leerá una sola vista cronológica creada al
intercalar las intervenciones de todas las pistas por `t_ini`, sin perder su origen.

```text
00:12:03.211–00:12:06.840  [A · Gabriel+amigo1]
No, espera, vuelve a mirar eso.

00:12:05.094–00:12:08.170  [B · amigo2]
Ya lo vi, está detrás de ti.

00:12:08.420–00:12:09.380  [A · Gabriel+amigo1]
[RISA 0.91]

00:12:09.510–00:12:11.020  [A · Gabriel+amigo1]
¡No puede ser!
```

El agente ve lógicamente todo el texto de todos los canales “a la vez”: un único
artefacto, la misma timeline y etiquetas de pista consistentes. Si el transcript de tres
horas excede el contexto práctico del agente, se lee jerárquicamente por ventanas, pero
nunca se analiza cada canal como una conversación independiente.

### 6.1 Intervenciones y solapes

Cada intervención global contiene:

- `utterance_id` estable;
- `track_id` y nombre visible;
- `speaker_id` opcional;
- `t_ini` y `t_fin`;
- IDs de palabras fuente;
- texto;
- resumen de señales que solapen el intervalo;
- `overlap_group` cuando haya conversación simultánea.

Si dos personas hablan al mismo tiempo se conservan ambas intervenciones. No se inventa
un orden lineal dentro del solape.

### 6.2 Bleed y transcripciones duplicadas

Una misma voz puede filtrarse en ambos micrófonos y ser transcrita dos veces. La vista
limpia necesita deduplicación conservadora:

1. buscar intervenciones de pistas distintas con alta coincidencia temporal;
2. comparar texto normalizado y alineación de palabras;
3. agrupar coincidencias fuertes en `duplicate_group`;
4. elegir la representación primaria por confianza ASR, completitud y señal relativa;
5. mostrar una sola copia en la conversación limpia;
6. conservar ambas observaciones y la decisión en el master.

No se deduplica solo porque el texto coincida. Dos personas pueden repetir la misma frase.
Los casos dudosos permanecen como solapes reales.

## 7. Master editorial

Fuente de verdad: `<proyecto>.editorial.master.json`.

El master almacena cada palabra y evento una sola vez. Los chunks son derivados que
referencian rangos e IDs; no duplican ni reemplazan la evidencia original.

Debe contener:

- fingerprint del medio y duración;
- timeline canónica y offsets de pistas;
- inventario de pistas seleccionadas;
- palabras y frases alineadas por pista;
- risa por pista;
- arousal e intensidad por pista;
- conversación global;
- grupos de solape y duplicados;
- versiones, parámetros, hashes y procedencia;
- definición versionada de chunks y resultados del agente.

Estructura conceptual:

```json
{
  "schema": "editorial-master/1",
  "media": {"duration": 10800.0, "fingerprint": "..."},
  "tracks": {
    "A": {
      "label": "Gabriel + amigo 1",
      "words": [],
      "utterances": [],
      "laughter": [],
      "arousal": []
    },
    "B": {
      "label": "amigo 2",
      "words": [],
      "utterances": [],
      "laughter": [],
      "arousal": []
    }
  },
  "conversation": {
    "utterances": [],
    "overlap_groups": [],
    "duplicate_groups": []
  },
  "chunks": []
}
```

La intensidad es una propiedad por palabra y vive dentro de `words`; no necesita una
colección paralela que pueda desincronizarse.

Ejemplo conceptual de palabra:

```json
{
  "word_id": "A-w-001284",
  "track_id": "A",
  "speaker_id": null,
  "t_ini": 1834.221,
  "t_fin": 1834.592,
  "text": "increíble",
  "asr_prob": 0.971,
  "rms_dbfs": -17.8,
  "peak_dbfs": -8.4,
  "intensity_z": 1.62,
  "arousal": 0.71,
  "arousal_z": 1.28,
  "emphasis_score": 0.84
}
```

## 8. Vistas para Codex

Codex no debe abrir de entrada un JSON enorme. El master genera vistas deterministas:

- `conversation.md`: ambas pistas intercaladas, con timecodes e IDs estables;
- `conversation-signals.md`: la misma conversación con niveles discretos de risa,
  arousal y énfasis;
- `map.json`: índices para resolver IDs y rangos a evidencia precisa;
- `chunks.json` y `chunks.md`: límites macro y justificación;
- vistas autocontenidas por chunk descritas en la sección 10.

Los niveles discretos evitan llenar el contexto con decimales. Los valores completos se
consultan bajo demanda desde el master.

## 9. Chunking macro: primera pasada del agente

### 9.1 Objetivo

Separar una grabación de unas tres horas en **3–4 chunks semánticos grandes**. El tamaño
esperado es aproximadamente 35–70 minutos, pero el contenido manda y no se imponen
divisiones uniformes.

### 9.2 Evidencia utilizada

- El texto combinado es el criterio principal: cambios de tema, actividad, historia u
  objetivo.
- Los silencios y finales de intervenciones ayudan a ubicar el borde exacto.
- Risa, arousal e intensidad son contexto secundario: ayudan a no cortar un momento
  activo y a caracterizar la energía, pero no crean un chunk por sí solos.

### 9.3 Lectura jerárquica

Si el transcript completo no cabe cómodamente en una sola lectura:

1. dividirlo determinísticamente en ventanas de 10–15 minutos;
2. incluir 30–60 segundos de contexto solapado sin cortar intervenciones;
3. identificar temas y límites candidatos en cada ventana;
4. sintetizar globalmente los mapas locales;
5. reabrir el texto alrededor de cada límite propuesto;
6. ajustar a una transición natural de la conversación.

La salida sigue representando la conversación completa. Las ventanas son una técnica de
lectura, no los chunks editoriales finales.

La implementación usa `codex exec` de forma no interactiva, efímera y con sandbox de
solo lectura. Codex devuelve `editorial-chunks/1` bajo un JSON Schema estricto. El plan
queda cacheado por el digest del master, por lo que reanudar no vuelve a invocar al
agente si la conversación no cambió.

### 9.4 Ajuste técnico de fronteras

Los timestamps devueltos por Codex expresan la decisión semántica. Para cada frontera
interna, la aplicación busca alrededor del punto propuesto y evalúa simultáneamente:

- palabras MMS activas en cualquier pista;
- risas activas en cualquier pista;
- intervenciones y solapes que atraviesen el instante;
- distancia respecto al límite semántico.

Se prioriza un punto sin palabras ni risas, después uno fuera de intervenciones y por
último el más cercano a la intención semántica. Se conservan tanto el timestamp propuesto
como el definitivo, el desplazamiento y el diagnóstico de seguridad. Si no existe
silencio global dentro del radio, el plan se mantiene válido pero registra la advertencia.

### 9.5 Contrato de cada chunk

- `chunk_id` estable;
- `t_ini`, `t_fin` y duración;
- título y resumen;
- temas y pistas participantes;
- razón concreta de inicio y final;
- IDs de primera y última intervención;
- señales destacadas como contexto;
- confianza y advertencias.

Por defecto, los chunks deben cubrir toda la timeline canónica, desde `0` hasta la
duración del medio, en orden y sin huecos ni solapes accidentales. Solo se excluye un
rango si el usuario lo marca explícitamente. No se corta dentro de una palabra, una
intervención o una risa relevante.

## 10. Materialización y análisis profundo por chunk

Después de aprobar los límites se generan vistas con slices de ambas pistas:

```text
chunks/
  chunk-a/
    transcript.md
    signals-summary.json
    laughter.json
    arousal.json
    intensity.json
    moments.md
  chunk-b/
    ...
```

`transcript.md` mantiene las pistas A y B intercaladas por timecode. Los JSON conservan
las señales separadas por pista y añaden resúmenes comparables por percentil.
`intensity.json` es un índice derivado de las palabras del master; no recalcula el audio.

Para cada chunk, el agente:

1. lee la conversación completa del rango;
2. revisa el resumen de risa, arousal e intensidad de A y B;
3. identifica historias, intercambios, reacciones, picos y partes prescindibles;
4. solicita detalle de palabras/eventos alrededor de candidatos cuando haga falta;
5. escribe `moments.md` con candidatos y evidencia.

Un candidato debe incluir:

- `t_ini` y `t_fin`;
- qué sucede;
- por qué puede funcionar editorialmente;
- evidencia textual;
- evidencia acústica de cada pista cuando exista;
- confianza y posibles problemas.

Una risa fuerte sin contexto no basta. Un momento semánticamente bueno con reacción o
arousal coincidente recibe mayor prioridad.

Modificar los límites regenera estas carpetas desde el master. No vuelve a transcribir
ni a ejecutar risa/prosodia.

## 11. Archivos esperados de la Fase 1

```text
<proyecto>/
  editorial/
    <proyecto>.editorial.master.json
    tracks/
      A/
        audio.flac
        words.whisper.json        # checkpoint de Whisper (paso whisper_A)
        utterances.whisper.json
        words.aligned.json        # tras MMS (paso align_A; copia si MMS está desmarcado)
        utterances.json
        words.json                # enriquecido con intensidad/arousal (paso prosody_A)
        laughter.json
        arousal.json
        intensity.json
        emotions.json
      B/
        ...
    views/
      conversation.md
      conversation-signals.md
      map.json
      chunks.json
      chunks.md
    chunks/
      chunk-a/
        transcript.md
        signals-summary.json
        laughter.json
        arousal.json
        intensity.json
        moments.md
```

Los nombres finales pueden ajustarse al implementar, pero deben mantenerse estas capas:
artefactos por pista, master canónico, vistas globales y vistas por chunk.

## 12. Implementación actual

- `editorial_pipeline.py`: orquestador transaccional multipista y reanudable.
- `editorial_master.py`: master, conversación global, solapes y deduplicación de bleed.
- `prosodia.py`: arousal acústico e intensidad por palabra sin modelos textuales.
- `codex_chunker.py`: lectura semántica global estructurada mediante Codex CLI.
- `editorial_chunks.py`: validación, ajuste seguro y materialización por chunk.
- `automatico_ui.py`: selección de pistas, progreso, revisión e importación externa.

Los manifests separan audio, Whisper, alineación MMS, señales, master, plan semántico y
materialización. Cambiar el prompt, regenerar chunks o mover una frontera no repite
Whisper, MMS, risa o prosodia; un fallo de MMS tampoco descarta la transcripción ya hecha.
Los pasos `align`, `laughter` y `prosody` son opcionales (`spec.steps`, casillas del panel
«Pipeline editorial»); el modelo de Whisper por defecto es el de Ajustes
(`large-v3-turbo`). Al reanudar con trabajo previo la UI pregunta si retomar o reescribir.

## 13. Criterios de aceptación de la Fase 1

Con un video cercano a tres horas y dos pistas seleccionadas:

- solo se procesan las pistas marcadas como voz;
- ninguna etapa excluida del perfil se carga o ejecuta;
- ambas pistas conservan offsets correctos respecto a T0;
- todas las palabras tienen identidad de pista y timestamps alineados válidos;
- risa, arousal e intensidad tienen procedencia de pista;
- `conversation.md` intercala correctamente A y B;
- los solapes reales se conservan;
- el bleed evidente no aparece duplicado en la vista limpia y queda auditado;
- el master valida y permite reanudar una corrida interrumpida;
- Codex propone 3–4 chunks después de leer la conversación global completa;
- cada frontera definitiva evita palabras y risas de todas las pistas o registra por
  qué no pudo evitar una intervención dentro del radio permitido;
- cada carpeta de chunk contiene transcript y señales de ambas pistas;
- cambiar un límite no repite análisis de audio;
- el sistema funciona localmente sin conexión a workers.

## 14. Interfaz vigente

El rediseño visual inicial ya está aprobado como dirección:

- la aplicación abre en modo Automático;
- el encabezado principal se simplifica;
- un menú de tres puntos contiene Automático/Manual y Ajustes;
- modo Manual conserva Transcribir, Limpiar audio, Extraer metadata y Marcar;
- Ajustes no es una pestaña principal;
- modo Automático usa una superficie tipo editor con monitor, estado y timeline.

Durante la Fase 1, el modo Automático puede limitarse a un solo video largo. La
importación de varios archivos y la edición multiclip son Fase 3.

La UI de Fase 1 debe permitir:

- importar el video;
- ver todas sus pistas de audio;
- marcar varias como voz y nombrarlas;
- iniciar/reanudar el perfil `editorial_voz`;
- ver progreso por pista y etapa;
- abrir el transcript combinado;
- revisar/ajustar chunks y abrir sus candidatos.

## 15. Fase 2: distribución

Esta sección fija la dirección futura sin formar parte de los primeros cambios reales.

### 15.1 Inventario conocido

| Nodo | Red/SO | Hardware | Rol previsto |
|---|---|---|---|
| Coordinador | local · Linux | Ryzen 7 4000 · RTX 3050 Ti 4 GB · 32 GB RAM | UI, ingestión, master, vistas y fallback |
| Worker LAN | misma Wi-Fi · Windows | Ryzen 9 · RTX 4070 8 GB · 64 GB RAM | una pista completa, CPU pesada y caché LAN |
| Worker WAN | remoto · Windows | Ryzen 9 · RTX 5080 16 GB · 32 GB RAM | Whisper principal si la transferencia compensa |

El objetivo de workers es Windows-first, manteniendo el coordinador Linux compatible.

### 15.2 Conectividad

- Usar Tailscale como red privada para LAN y máquinas remotas.
- No exponer workers a internet público.
- Usar Tailscale Grants para limitar el servicio a coordinadores autorizados.
- Reservar SSH para instalación, actualización, diagnóstico y recuperación; no usarlo
  como protocolo habitual del scheduler.

### 15.3 Worker y scheduler

Cada `transcriber-worker` será headless y anunciará:

- versión del protocolo y pipeline;
- CPU, RAM, GPU y VRAM disponible;
- backends y modelos verificados;
- carga, slots, disco y caché por hash;
- corriente/batería, temperatura y estado de suspensión cuando sea posible.

El protocolo debe soportar heartbeats, leases, progreso, cancelación, transferencia
reanudable, hashes, resultados idempotentes y reprogramación si un nodo desaparece.

Los workers pedirán trabajo al coordinador. El scheduler comparará tiempo de transferencia
más procesamiento remoto contra procesamiento local y favorecerá la localidad de datos.

### 15.4 Unidad de distribución

En el caso inicial, la unidad preferida es una pista completa:

- pista A en un worker;
- pista B en otro;
- merge/master/chunking en el coordinador.

Esto evita partir palabras y reconciliar fronteras. Solo se estudiará dividir una misma
pista por tiempo si las mediciones demuestran una ganancia clara.

### 15.5 Restricción CUDA de Windows

- Whisper/CTranslate2 sí puede usar NVIDIA.
- El código actual fuerza los módulos torch a CPU en Windows por un conflicto de DLLs
  cuDNN entre torch y CTranslate2.
- Risa y audeering dependen de torch; no deben asignarse automáticamente a CUDA hasta
  resolver o aislar ese conflicto.
- Cada worker ejecutará smoke tests por backend y publicará capacidades verificadas, no
  capacidades inferidas por el nombre de la GPU.

### 15.6 Laptops

- No aceptar trabajos pesados en batería por defecto.
- Evitar suspensión mientras haya un lease activo.
- Reportar temperatura, carga y VRAM cuando sea posible.
- Tratar suspensión, reinicio o cambio de Wi-Fi como desconexión recuperable.
- Reprogramar sin corromper manifests.

## 16. Fuera de alcance actual

- análisis facial o de gameplay;
- visión/VLM y descripción de escenas;
- sentimiento o emoción derivados del texto;
- limpieza de audio automática;
- diarización obligatoria;
- edición destructiva del video;
- decisión automática del corte final;
- exportación específica a Premiere, Resolve u otro NLE;
- varios clips movibles en el timeline;
- load balancing antes de validar el pipeline local y los chunks.

## 17. Decisiones pendientes, no bloqueantes para comenzar

- calibración del modelo Whisper inicial y política de calidad/velocidad para tres horas;
- calibración de las ventanas actuales de arousal;
- fórmula y calibración de `emphasis_score`;
- umbral conservador para deduplicar bleed;
- editor/NLE objetivo para una futura exportación;
- si más adelante se añade diarización a la pista compartida.

## 18. Releases y actualizaciones de Windows

Infraestructura implementada el 29 de agosto de 2026. Aplica transversalmente a todas
las fases y evita reinstalar modelos al desplegar cambios.

### 18.1 Separación obligatoria

- `releases/<semver>` contiene únicamente código inmutable;
- `runtimes/<hash>` contiene Python y dependencias, reutilizado mientras no cambie el lock;
- `shared/models` y `shared/cache` contienen todos los pesos y cachés persistentes;
- `shared/config` contiene configuración, presets e historial por máquina;
- `state/current.json` mantiene release actual, anterior y activación pendiente.

Los backends deben obtener sus rutas exclusivamente de `app_paths.py`. Ninguna release
puede escribir configuración, logs, modelos o bytecode dentro de su propia carpeta.

### 18.2 Protocolo de actualización

El launcher consulta `GET /repos/{owner}/{repo}/releases/latest`. Sólo acepta SemVer
estable y los assets exactos `transcriptor-update-vX.Y.Z.json` y
`transcriptor-windows-vX.Y.Z.zip`.

Antes de activar valida:

- correspondencia tag–versión–plataforma;
- tamaño y SHA-256 del ZIP;
- SHA-256 del manifiesto interno;
- lista exacta, tamaño y SHA-256 de cada archivo;
- ausencia de traversal, rutas duplicadas para Windows, enlaces y archivos no declarados.

La extracción ocurre en staging y la activación usa reemplazo atómico. La interfaz debe
confirmar un nonce de salud después de construirse. Un fallo temprano o timeout revierte
al release anterior. Red caída, rate limit o un asset inválido nunca bloquean el arranque
de la versión ya instalada.

### 18.3 Publicación

Un tag `vX.Y.Z` dispara `.github/workflows/release.yml`. El workflow exige que `VERSION`
coincida, ejecuta pruebas, construye los manifiestos y publica el paquete incremental y
el instalador para máquinas nuevas. Un release publicado nunca se reemplaza; cualquier
cambio, incluso urgente, recibe una versión nueva.
