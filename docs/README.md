# Documentación de Transcriptor

Toda la documentación funcional del proyecto vive en esta carpeta, salvo tres archivos que
por convención quedan en la raíz: `README.md` (portada), `INSTALL-WINDOWS.md` (instalación
administrada y publicación de releases) y las licencias. La skill que usa la AI externa
está en `skills/transcriptor/SKILL.md` porque se distribuye como skill, no como documento.

## Qué leer según lo que necesites

| Quiero… | Documento |
|---|---|
| Usar el modo Automático (podcast: pistas → bloques → recortes → AI → exportar) | [guia-automatico.md](guia-automatico.md) |
| Usar el modo Manual (Transcribir, Limpiar audio, Extraer metadata, Marcar) | [guia-manual.md](guia-manual.md) |
| Saber qué decisiones de producto están vigentes y cuáles quedaron fuera | [especificacion-editorial.md](especificacion-editorial.md) |
| Entender cómo está implementado, qué no romper y qué falta | [referencia-tecnica.md](referencia-tecnica.md) |
| Saber por qué algo es como es (cronología de decisiones) | [historial.md](historial.md) |
| Instalar, actualizar o publicar una versión de Windows | [../INSTALL-WINDOWS.md](../INSTALL-WINDOWS.md) |
| Darle instrucciones a la AI que propone bloques y recortes | [../skills/transcriptor/SKILL.md](../skills/transcriptor/SKILL.md) |

## Cómo mantenerla

- **Al cerrar una sesión de trabajo** se actualizan `referencia-tecnica.md` §7 (estado y
  pendientes) e `historial.md` (una entrada por período, del más viejo al más nuevo).
- **Cuando cambia una decisión de producto** se edita `especificacion-editorial.md`; ese
  documento contiene solo lo vigente, no el historial de ideas.
- **Cuando cambia lo que ve el usuario** se actualiza la guía correspondiente. Las guías
  describen la app instalada tal cual está; no prometen funciones futuras.
- Los tres documentos históricos conservan sus nombres anteriores como nota al inicio
  (`HANDOFF.md`, `AUTOCLIP_HANDOFF.md`, `CAMBIO-INTERFAZ-Y-DISTRIBUCION.md`) para que las
  referencias viejas se puedan seguir.

## Convenciones

- Idioma: español; los nombres de archivos, claves JSON y código se citan tal cual.
- Los tiempos son siempre **segundos absolutos de la timeline canónica del medio** (T0),
  la misma para todas las pistas, los bloques y los recortes.
- Los documentos de diseño y las reviews con Codex (`three-brain-out/<fecha-tema>/`) y los
  originales pre-reescritura (`docs-archivo/`) viven en el checkout Linux de desarrollo;
  están en `.gitignore` y no forman parte del repositorio ni de la release.
- `README.md`, `INSTALL-WINDOWS.md`, las licencias, `docs/*.md` y la skill viajan dentro
  de cada release de Windows (`tools/build_release.py`).
