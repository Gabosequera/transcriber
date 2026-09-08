# Transcriptor 0.4.0

Publicación del avance de la línea V1 (Python). Conserva PolyForm Noncommercial1.0.0 y la licencia comercial separada.

Incluye los cambios acumulados desde la última release pública: recortes profundosAI y su carril, botón Preparar para la AI con estado del ciclo, herencia de temas/capas en proyectos hijos, apertura del vídeo recortado, timeline de montaje con pistas/preview/export, Tarea5 Montaje por temas y propuestas validadas, y export de EDL CMX3600/FCPXML junto al montaje. Los cambios funcionales ya estaban implementados y registrados antes de esta publicación.

Validación local de publicación: Python del runtime administrado3.13, FFmpeg8.0.1, `python -m unittest discover -s tests -v`:150 pruebas,148 pasan y2 omitidas. No equivale a una nueva instalación de todos los modelos ni una prueba completa de inferencia. Los checks de GitHub Actions comprueban Linux/Windows y el instalador desde cero antes de su publicación automatizada.

Instalación/actualización: consultar INSTALL-WINDOWS.md. VERSION declara0.4.0 y update-channel.json apunta a Gabosequera/transcriber, canal stable. No se modificaron medios, modelos o configuración personal durante la preparación.
