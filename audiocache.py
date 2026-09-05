#!/usr/bin/env python3
"""
audiocache.py — caché LRU del audio DECODIFICADO, para no re-decodificar el mismo archivo
en cada módulo.

Una sola corrida de metadata/limpieza puede decodificar el audio de 1h MUCHAS veces (Whisper
internamente, MMS-align, emoción, risa, respiros, AST…). Cada `librosa.load(sr=16000)` vuelve
a leer y resamplear el archivo entero. Acá se decodifica UNA vez por (path, sr, mono) y se
reusa entre módulos.

Uso: reemplazar `librosa.load(path, sr=16000, mono=True)[0]` por `audiocache.load(path)`.

Guardas (para "cualquier hardware", sin reventar 8 GB de RAM):
  · LRU chico (1 entrada por defecto) — un audio de 1h mono 16k son ~230 MB.
  · invalida si el archivo cambió (mtime + size).
  · NO cachea arrays enormes ni si la RAM libre está baja (solo devuelve, sin guardar).
  · thread-safe; solo cachea cargas COMPLETAS (una carga cancelada/fallida no ensucia).
"""
from __future__ import annotations

import threading
from collections import OrderedDict
from pathlib import Path

_LOCK = threading.Lock()
_CACHE: "OrderedDict" = OrderedDict()   # key -> np.ndarray (orden = LRU)
_MAXSIZE = 1               # nº de audios en caché (subir si el flujo lo justifica)
_MAX_CACHE_MB = 800        # no cachear un array más grande que esto (~1h stereo)


def _key(path, sr, mono):
    p = Path(path)
    try:
        st = p.stat()
        return (str(p.resolve()), int(sr), bool(mono), int(st.st_mtime_ns), int(st.st_size))
    except OSError:
        return (str(p), int(sr), bool(mono), 0, 0)


def _ram_ok() -> bool:
    """¿Hay RAM de sobra para cachear? (>1.5 GB libres). Sin psutil, asume que sí."""
    try:
        import psutil
        return psutil.virtual_memory().available > int(1.5 * 2**30)
    except Exception:
        return True


def load(path, sr=16000, mono=True):
    """Audio decodificado como np.float32 mono a `sr`. Cacheado por (path, mtime, size, sr, mono).
    Mismo contrato que `librosa.load(path, sr=sr, mono=mono)[0]`.

    ⚠ INVARIANTE: el array devuelto es COMPARTIDO (cacheado) y de SOLO LECTURA (write=False).
    Los llamadores no deben mutarlo in-place — usar slicing (vistas) y copiar al transformar.
    Verificado que los 5 módulos actuales cumplen (slicing + copia). El job-lock además serializa
    los jobs; y si dos hilos cargan el mismo archivo a la vez, el 2º reusa el del 1º (sin corromper)."""
    import numpy as np
    k = _key(path, sr, mono)
    with _LOCK:
        arr = _CACHE.get(k)
        if arr is not None:
            _CACHE.move_to_end(k)                 # touch (LRU)
            return arr
    # decodificar FUERA del lock (puede tardar; no bloquea a otros lectores)
    import soundfile as sf
    try:
        info = sf.info(str(path))
    except (RuntimeError, OSError):
        info = None
    if info is not None and info.samplerate == sr and info.channels == 1:
        # Las pistas editoriales ya son mono 16 kHz: sin resampleo ni copia a 48 kHz.
        arr = sf.read(str(path), dtype="float32")[0]
    else:
        import librosa
        arr = librosa.load(str(path), sr=sr, mono=mono)[0]
    arr = np.ascontiguousarray(arr, dtype=np.float32)
    arr.setflags(write=False)                     # solo lectura → mutar in-place por error tira loud
    if arr.nbytes / 2**20 <= _MAX_CACHE_MB and _ram_ok():
        with _LOCK:
            existing = _CACHE.get(k)              # re-chequear: otro hilo pudo cargarlo mientras
            if existing is not None:
                _CACHE.move_to_end(k)
                return existing                   # reusar el suyo (estado consistente)
            _CACHE[k] = arr
            _CACHE.move_to_end(k)
            while len(_CACHE) > _MAXSIZE:
                _CACHE.popitem(last=False)        # evict el menos usado
    return arr


def clear() -> None:
    """Vacía la caché (p.ej. al 'Liberar memoria')."""
    with _LOCK:
        _CACHE.clear()
