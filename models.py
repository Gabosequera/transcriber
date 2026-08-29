#!/usr/bin/env python3
"""
models.py — registro central para LIBERAR los modelos cacheados (RAM/VRAM).

Cada módulo (align/metadata/laughter/escena_audio/detect_breaths/describir) cachea su modelo en
un global por proceso. Tras usar varias features quedan varios GB ocupados → el siguiente puede dar
OOM en equipos modestos. Este módulo llama al `unload()` de cada uno para soltar todo de una.

Uso:
  · botón "Liberar memoria" en Ajustes → models.unload_all()
  · antes de un job pesado en GPU con poca VRAM libre → models.free_if_tight()
"""
from __future__ import annotations

# módulos que exponen un `unload()` (soltar su modelo cacheado). audiocache también se limpia.
_MODULES = ["align", "metadata", "laughter", "escena_audio", "detect_breaths", "describir", "cara"]


def unload_all(log=None) -> None:
    """Descarga TODOS los modelos cacheados y limpia la caché de audio. Libera RAM y VRAM.
    Solo toca módulos YA importados (no tiene sentido —y cuesta segundos y RAM— importar
    torch/transformers nada más que para 'descargar' un modelo que nunca se cargó)."""
    import sys
    for name in _MODULES:
        try:
            m = sys.modules.get(name)
            fn = getattr(m, "unload", None) if m else None
            if callable(fn):
                fn()
        except Exception:
            pass
    try:
        if "audiocache" in sys.modules:
            sys.modules["audiocache"].clear()
    except Exception:
        pass
    import gc
    gc.collect()
    try:
        if "torch" in sys.modules:
            sys.modules["torch"].cuda.empty_cache()
    except Exception:
        pass
    if log:
        log("🧹 Memoria liberada (modelos descargados).")


def free_if_tight(need_gb: float = 2.0, log=None) -> None:
    """Si la RAM (o VRAM) libre está por debajo de `need_gb`, descarga los modelos para hacer
    lugar antes de un job pesado. No-op si hay memoria de sobra (no penaliza al HW fuerte)."""
    try:
        import hardware
        tight = False
        if hardware.use_gpu_torch():
            free = hardware.vram_free_gb()
            tight = free is not None and free < need_gb
        else:
            _, avail = hardware.ram_gb()
            tight = avail and avail < max(need_gb, 2.0)
        if tight:
            if log:
                log("Poca memoria libre → descargando modelos previos para hacer lugar…")
            unload_all(log=None)
    except Exception:
        pass
