"""Navegación tipo editor (diseño docs/diseno-navegacion-editor.md §3, Fase 3). Puro.

  · `EdgeIndex(times)`: lista ordenada de tiempos únicos; `prev(t)`/`next(t)` son un
    `bisect` (O(log n)) sobre miles de recortes. Se construye una vez por cambio de
    documentos (el controlador la cachea con las mismas claves que `_cache_key`).
  · `edge_times(layers)`: bordes de todos los items de todas las capas visibles
    (inicio y fin de cada tramo; un punto aporta un solo tiempo).
  · `silence_times(trims)`: bordes de los recortes de origen `silence`.
  · `parse_goto(text, current, duration)`: «1:23:45.6», «5025», «+30», «-10».
"""
from __future__ import annotations

from bisect import bisect_left, bisect_right

from editorial_io import parse_time

EPS = 1e-3


class EdgeIndex:
    def __init__(self, times):
        self.times = sorted({round(float(t), 3) for t in times})

    def __len__(self):
        return len(self.times)

    def prev(self, t: float, *, eps: float = EPS):
        """El mayor borde estrictamente anterior a `t` (con tolerancia), o None."""
        index = bisect_left(self.times, float(t) - eps)
        return self.times[index - 1] if index > 0 else None

    def next(self, t: float, *, eps: float = EPS):
        """El menor borde estrictamente posterior a `t` (con tolerancia), o None."""
        index = bisect_right(self.times, float(t) + eps)
        return self.times[index] if index < len(self.times) else None

    def nearest(self, t: float, *, radius: float):
        """El borde más cercano dentro de ±radius, o None (imán)."""
        t = float(t)
        index = bisect_left(self.times, t)
        candidates = [self.times[i] for i in (index - 1, index) if 0 <= i < len(self.times)]
        best = min(candidates, key=lambda x: abs(x - t), default=None)
        return best if best is not None and abs(best - t) <= radius else None


def edge_times(layers) -> list[float]:
    out = []
    for layer in layers or []:
        for item in layer.get("items") or []:
            for part in item.get("ranges") or []:
                out.append(float(part["t_ini"]))
                if float(part["t_fin"]) != float(part["t_ini"]):
                    out.append(float(part["t_fin"]))
    return out


def silence_times(trims) -> list[float]:
    out = []
    for cut in (trims or {}).get("cuts") or []:
        if cut.get("origin") == "silence":
            out.extend((float(cut["t_ini"]), float(cut["t_fin"])))
    return out


def parse_goto(text: str, current: float, duration: float) -> float:
    """Tiempo destino de «Ir a tiempo»: absoluto («1:23:45.6», «83.5», «5025») o
    relativo al playhead («+30», «-10», «+1:30»). Se acota a [0, duración]."""
    if not isinstance(text, str) or not text.strip():
        raise ValueError("escribe un tiempo: 1:23:45.6, 5025, +30 o -10")
    text = text.strip().replace(",", ".")
    sign = 0
    if text[0] in "+-":
        sign = 1 if text[0] == "+" else -1
        text = text[1:].strip()
    try:
        seconds = parse_time(text)
    except ValueError as error:
        raise ValueError(f"tiempo inválido: {error}") from None
    if seconds < 0:
        raise ValueError("el tiempo no puede ser negativo")
    target = float(current) + sign * seconds if sign else seconds
    return max(0.0, min(float(duration), target))


def frame_step(fps: float | None) -> float:
    """Duración de un fotograma (30 fps si el medio no declara tasa)."""
    fps = float(fps or 0.0)
    return 1.0 / fps if fps > 0 else 1.0 / 30.0
