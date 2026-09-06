# Transcriptor

> **Licencia:** uso no comercial bajo PolyForm Noncommercial 1.0.0. Vender, monetizar o
> incorporar este software a un producto o servicio comercial requiere una licencia
> escrita separada. Ver [LICENSE.md](LICENSE.md) y [COMMERCIAL-LICENSE.md](COMMERCIAL-LICENSE.md).

App de escritorio (Python 3.13 + customtkinter, Windows y Linux) que convierte grabaciones
largas —podcasts y VODs de varias horas, multipista de OBS— en metadata precisa (palabras
con timestamps de ~50 ms, risas, intensidad y emoción acústica) y en propuestas de edición
que una persona revisa sobre un timeline antes de que se corte nada.

## Dos modos de trabajo

- **Automático** (predeterminado): el pipeline editorial para podcasts. Procesa las pistas
  de voz, arma la conversación global, deja que una AI proponga bloques por tema, propone
  recortes de silencios y de contenido, y exporta los videos ya cortados.
  Guía completa: [docs/guia-automatico.md](docs/guia-automatico.md).
- **Manual**: las herramientas por separado — Transcribir, Limpiar audio (respiraciones),
  Extraer metadata (wizard multimodal para auto-clipping) y Marcar (revisión + guion para
  la IA que corta clips). Guía: [docs/guia-manual.md](docs/guia-manual.md).

Ajustes (hardware, modelo Whisper por defecto, memoria, claves de API) vive en el menú ⋮
de la cabecera; no es una pestaña.

## Instalar y ejecutar

- **Windows**: descarga `transcriptor-installer-vX.Y.Z.zip` de la última release, extrae en
  una carpeta permanente, ejecuta `setup-windows.bat` una vez y abre la app con `run.bat`.
  No hace falta instalar Python ni ffmpeg. La app se actualiza sola desde GitHub y vuelve
  a la versión anterior si una actualización no arranca. Detalle: [INSTALL-WINDOWS.md](INSTALL-WINDOWS.md).
- **Linux** (desarrollo): crea el entorno y lanza con `run.sh`.

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
./run.sh
```

## El flujo de podcast en ocho pasos

1. **Importar** el video y marcar como VOZ las pistas con voces (pueden compartir pista).
2. **Procesar pistas de voz**: Whisper + alineación MMS + risa + intensidad/emoción, por
   pista, con reanudación (no repite lo ya hecho).
3. **Bloques por tema**: la AI lee `views/conversation.md` con la skill incluida y escribe
   `views/cuts.proposed.json`; la app lo valida, ajusta bordes y lo pinta en colores.
4. **Revisar chunks**: títulos y límites, sin repetir análisis.
5. **Analizar silencios**: propone recortes en los huecos sin voz de ninguna pista. Nada se
   corta: se ven en el carril «recortes» y se ajustan con el mouse.
6. **Revisión de la AI** (opcional): propone recortes de contenido que no aporta, nunca
   por humor fuerte ni lisuras. También aparecen en el carril para revisar.
7. **Cortar y exportar**: recién aquí se cortan los videos, con todas las pistas de audio
   y precisión de fotograma. El original no se toca.
8. **Volver a un proyecto**: importa el mismo video y abre su master; la identidad es por
   contenido, no por ruta.

## Documentación

| Documento | Para qué |
|---|---|
| [docs/README.md](docs/README.md) | Índice y convenciones de la documentación |
| [docs/guia-automatico.md](docs/guia-automatico.md) | Modo Automático paso a paso: pistas, bloques, recortes, AI, exportación, archivos y CLI |
| [docs/guia-manual.md](docs/guia-manual.md) | Modo Manual: Transcribir, Limpiar audio, Extraer metadata, Marcar, presets |
| [docs/especificacion-editorial.md](docs/especificacion-editorial.md) | Decisiones vigentes del perfil editorial (Fase 1, releases, recortes) |
| [docs/referencia-tecnica.md](docs/referencia-tecnica.md) | Referencia técnica del estado actual: módulos, invariantes, entornos, pendientes |
| [docs/historial.md](docs/historial.md) | Cronología de cómo se construyó el proyecto |
| [INSTALL-WINDOWS.md](INSTALL-WINDOWS.md) | Instalación administrada, actualizaciones y publicación de releases |
| [skills/transcriptor/SKILL.md](skills/transcriptor/SKILL.md) | La skill que usa la AI externa (bloques y recortes de contenido) |

## Estructura del código

| Módulo | Qué hace |
|---|---|
| `app.py` | Interfaz: cabecera, menú ⋮, modo Automático y pestañas del modo Manual |
| `automatico_ui.py` | Workspace Automático: pistas, progreso, carriles de bloques y recortes, exportación |
| `editorial_pipeline.py` | Orquestador transaccional del perfil editorial (reanudable, CLI) |
| `editorial_master.py` · `editorial_chunks.py` · `editorial_trims.py` | Master y vistas · bloques · recortes |
| `podcast_export.py` | Exportación de bloques con o sin recortes (FFmpeg, frame-accurate) |
| `editor_medios.py` · `medios.py` | Mini-editor de timeline y reproductor · ffprobe, extracción, waveform |
| `core.py` · `align.py` · `laughter.py` · `prosodia.py` | Whisper · alineación MMS · risa · arousal e intensidad |
| `wizard_extraer.py` · `marcar.py` · `pipeline.py` · `marcas.py` · `guion.py` | Modo Manual: wizard multimodal, revisión, pipeline DAG, marcas, guion |
| `bootstrap.py` · `launcher.py` · `updater.py` · `app_paths.py` | Instalación administrada de Windows y actualizaciones |

El mapa completo de módulos está en [docs/referencia-tecnica.md](docs/referencia-tecnica.md) §3.

## Desarrollo

```bash
python -m unittest discover -s tests -v
```

Las pruebas usan metadata simulada y medios sintéticos generados con FFmpeg; no certifican
la exactitud de Whisper ni la calidad editorial de una AI sobre un podcast real. Los tests
de exportación se saltan si `ffmpeg` no está en el PATH (en la instalación administrada de
Windows está en `shared\tools\ffmpeg`). El workflow de GitHub corre la suite en Linux y
Windows y prueba una instalación Windows completa antes de publicar.
