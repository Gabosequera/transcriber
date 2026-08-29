#!/usr/bin/env python3
"""
jobs.py — gestor mínimo de trabajos PESADOS. Serializa el cómputo pesado (transcribir, limpiar,
metadata, audio de fondo) para que dos pestañas lanzadas a la vez NO sobre-suscriban la CPU/GPU/RAM
(cada tarea ya usa todos los hilos y/o la GPU → correrlas juntas satura y puede dar OOM).

MVP: un único Lock global → un solo job pesado a la vez; los demás esperan su turno. Como la app
es mono-usuario, alcanza. (Clasificación por recurso —permitir GPU+CPU juntos— queda para después.)

Uso, dentro del hilo worker:
    with jobs.heavy(log_cb):
        ...trabajo pesado...
Si hay otro job en curso, `heavy()` avisa por el log y espera a que se libere.
"""
from __future__ import annotations

import threading
from contextlib import contextmanager

_LOCK = threading.Lock()


def busy() -> bool:
    """True si hay un job pesado en curso (para que la GUI pueda avisar antes de encolar)."""
    held = _LOCK.acquire(blocking=False)
    if held:
        _LOCK.release()
        return False
    return True


@contextmanager
def heavy(log_cb=None, cancel=None):
    """Context manager: toma el lock de trabajo pesado (serializa). Si está ocupado, avisa por el
    log y espera. Libera al salir, pase lo que pase.

    `cancel` (threading.Event, opcional): permite CANCELAR mientras se espera el lock —
    la espera es por polling corto; si el evento se setea, levanta InterruptedError sin
    haber tomado el lock (antes la espera era bloqueante e incancelable)."""
    if not _LOCK.acquire(blocking=False):
        if log_cb:
            log_cb("⏳ Hay otro proceso pesado en curso — esperando a que termine…")
        while not _LOCK.acquire(timeout=0.25):
            if cancel is not None and cancel.is_set():
                raise InterruptedError("cancelado mientras esperaba el turno de trabajo pesado")
    try:
        yield
    finally:
        _LOCK.release()
