# Reproductor y navegación — medición Windows, 2026-09-06

VOD real `2026-08-23 01-13-47.mp4`: 10.756,13 s, HEVC 3360×1080 a 30 fps,
tres pistas AAC estéreo de 48 kHz, todas con inicio 0. Equipo de Gabriel:
RTX 4070 Laptop, 32 hilos, 63 GB RAM. Mini-editor Tk real, salida de audio real.

## Causa y corrección

El reproductor anterior avanzaba con `monotonic()` desde el retorno de `Popen`,
restando `av_offset_s=0.25`. Crear FFplay no significa que el dispositivo ya esté
reproduciendo: el reloj aparece entre 1,38 y 1,77 s después, según carga y posición.
Además, el warm-up arrancaba audio aunque no hubiera video al alcanzar dos segundos.
En esta reproducción el video iba adelantado; el reporte original describía audio
adelantado. No se ha reproducido ese mismo signo, pero sí el desfase variable del
reloj independiente y la vía de arranque de audio sin primer frame.

Ahora se exige el primer frame antes de iniciar audio (hasta 10 s, luego se detiene).
`playback_clock.AudioClock` consume el reloj de salida que FFplay publica con
`-sync audio -stats`. El video lo sigue; no usa `av_offset_s`. Sin muestras recientes
se congela y, si no se recupera, se detiene la sesión. El preview usa 30 fps y ticks
de 16 ms, con degradación existente si el decodificador no alcanza. Las latencias de
primer frame, creación del proceso y primer reloj quedan en `logs/reproductor.jsonl`
(en instalación administrada, `shared/logs/`).

## Resultados

Tres segundos de muestras por arranque. Error = timestamp del frame presentado menos
reloj de salida de audio; el percentil 95 usa el valor absoluto. La prueba mide los
relojes del reproductor, no una captura física de altavoz/pantalla ni percepción humana.

| Arranque | Error mediano antes | Error mediano después | P95 absoluto después |
|---|---:|---:|---:|
| 00:00 | +1.186,8 ms | −23,3 ms | 35,0 ms |
| 30:00 | +1.238,1 ms | −23,7 ms | 34,3 ms |
| 90:00 | +1.444,0 ms | −20,3 ms | 33,6 ms |

| Operación | Antes | Después |
|---|---:|---:|
| Salto a posición nueva, sin prefetch | 1.696–1.772 ms | 1.614–1.616 ms |
| Salto repetido a posición exacta | 1.766–1.780 ms | 10–11 ms |
| Par zoom ×2 / ×0,5, mediana | 25,8 ms | 3,9 ms |
| Waveform de una pista completa, misma identidad y parámetros | 10.084 ms (calcular) | 11 ms (leer caché) |

Las cachés son vistas desechables: frames exactos en RAM (48 MiB máximo), hasta
24 vistas de waveform y envelopes persistentes por fingerprint/pista/duración/parámetros.
Cambiar de medio limpia frames y waveforms visibles; corrupción de la caché fuerza
recálculo. No se cambia ninguna metadata del pipeline. Los saltos nuevos siguen
limitados por HEVC/seek. Probar menos threads, menor probesize o PCM en vez de WAV
no redujo de forma consistente el primer frame: no se adoptaron esos cambios.

## Reproducir

Ejecutar `tools/benchmark_preview.py --source <VOD> --output <resultado.json>` con el
runtime de la app y FFmpeg/FFplay en PATH. `--baseline-dir` permite cargar copias de
`editor_medios.py` y `medios.py` anteriores (se usó commit `483f489`), instrumentando
solo stderr de FFplay sin cambiar su política de sincronización. El script abre y
cierra una ventana Tk, reproduce tres posiciones, mide seeks y zoom, y genera JSON.
La caché de waveform del segundo recorrido está caliente; la comparación por pista
de la tabla usa exactamente los mismos 4.000 buckets y resultado.

Los unittest verifican reloj ausente/obsoleto, aislamiento y límites de caché,
reutilización por contenido y cierre real de FFplay. En CI se usa SDL dummy para
verificar el protocolo sin dispositivo de audio; eso no mide sincronización perceptual.
