# Proyectos modulares, capas y temas — diseño 2026-09-06

## Decisiones

Cada medio tiene su propia timeline T0. Un hijo mantiene `editorial-master/1` y
añade `derivation: {schema: "editorial-derivation/1", source_master_digest,
source_fingerprint, source_master, source_range, segments}`. Cada segmento guarda
`source_ini/source_fin` y `child_ini/child_fin`. La conversión intersecta cada evento
con cada segmento conservado. Divide eventos que cruzan recortes, conserva su ID
fuente y reconstruye intervenciones a partir de las palabras conservadas. No copia
texto eliminado. Recalcula referencias y vistas; no ejecuta modelos. Los valores
acústicos y baselines siguen siendo mediciones del padre, con procedencia explícita.

Las exportaciones publican medio y proyecto hijo en una sola transacción:

```text
TODO/
  original.mkv
  proyecto/editorial/                 # se conserva la estructura 0.2.1
    original.editorial.master.json
    tracks/  views/  chunks/  .work/
    layers/<id>.json
  exportaciones/podcast-<digest>/
    accepted-plan.json  accepted-trims.json  exports.json
    001.mp4
    projects/001/editorial/
      001.editorial.master.json
      views/  layers/  tracks/         # audio solo si hace falta medir silencios
  .transcriptor/catalog.json           # caché reconstruible, rutas relativas
```

El descubrimiento examina masters en la carpeta del medio y las carpetas vecinas
del conjunto, compara los tres componentes del fingerprint, nunca nombres. El
catálogo acelera lecturas y se reconstruye al faltar o cambiar sus archivos; no es
autoridad. Empates de metadata distinta se muestran para selección explícita.
Mover/copiar el conjunto conserva las rutas relativas. Un clip suelto requiere que
su metadata siga accesible: el contenido del video no contiene la transcripción.

`editorial-layer/1`: `layer_id`, `kind`, `name`, `color`, `media_fingerprint`,
`source_master_digest`, `revision`, `items`. Cada item: `item_id`, `label`,
`comment`, `state` (`proposed/accepted/disabled`), `edited`, `parent_id` opcional y
`ranges: [{t_ini,t_fin}]`. Varios rangos pertenecen a UN item lógico.

Las capas de usuario y temas se guardan en `layers/`. Bloques, recortes y marcas
son adaptadores de sus documentos actuales, sin duplicar almacenamiento editable.
Las marcas siguen usando exclusivamente `marcas.registro_compartido`; sus prompts
y decisiones se exponen como comentario y estado. El editor común proporciona
selección, rangos, movimiento, bordes, tooltip y edición de comentarios; los
adaptadores persisten mediante sus validadores y operaciones existentes.

Las vistas entregan a la AI `views/layers.json` (snapshot derivado) junto al digest
de revisión. Respuestas `editorial-layers-proposal/1` referencian ambos digests;
nunca pisan modificaciones humanas ni aceptan órdenes dentro del transcript.

Temas: `editorial-topics-proposal/1`, `source_master_digest`, `source_layers_digest`,
`pass` (1 o 2), `previous_pass_digest` (obligatorio en 2), `items` con el formato
de capa y jerarquía `parent_id`. Primera pasada cronológica; segunda lee el mapa
completo y unifica recurrencias bajo un ID con varios rangos. Solo la segunda
publica la capa revisable. Una nueva revisión parte de la capa corregida. Se validan
identidad, números finitos, IDs únicos, jerarquía acíclica, contención de subtemas,
rangos ordenados sin solapes internos y bordes ajustados ≤1,5 s contra palabras y
risas de todas las pistas. Cada desplazamiento conserva diagnóstico.

## Verificación y entrega

Implementar A, B, C, D y E en commits separados con suite completa y smoke Tk real
antes de cada commit. Para E medir arranque y navegación en varios puntos de un
medio largo, distinguir spawn de audio de reloj de reproducción y conservar el
esquema actual salvo evidencia que justifique reemplazo. Registrar límites de la
medición: un smoke automatizado no demuestra sincronía perceptual por sí solo.
Actualizar instalación administrada, versión y release únicamente tras verificar.
