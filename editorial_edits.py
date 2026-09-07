"""Aritmética de las acciones de edición del timeline (diseño §3 y §7). Puro: opera
sobre copias de los documentos y devuelve el resultado; las validaciones de verdad
(`validate_items`, bloques contiguos, un rango por item) siguen en `persist`.

Convenciones: los tiempos son segundos absolutos; `min_len` es la duración mínima de
un tramo resultante (un fotograma por defecto).
"""
from __future__ import annotations

import copy
import re

import editorial_trims

FRAME = 1.0 / 30.0
_CHUNK_ID = re.compile(r"chunk-(\d+)\Z")


# ---- rangos ----
def split_range(part: dict, t: float, *, min_len: float = FRAME) -> tuple[dict, dict]:
    """[a, b] en t → ([a, t], [t, b]); t debe caer estrictamente dentro dejando al
    menos `min_len` a cada lado."""
    a, b = float(part["t_ini"]), float(part["t_fin"])
    t = float(t)
    if not (a + min_len <= t <= b - min_len):
        raise ValueError("el playhead debe estar dentro del tramo (al menos un fotograma "
                         "a cada lado)")
    return {**part, "t_ini": a, "t_fin": round(t, 3)}, {**part, "t_ini": round(t, 3), "t_fin": b}


def trim_range(part: dict, edge: str, t: float, *, min_len: float = FRAME) -> dict:
    """Lleva el inicio (`edge="start"`) o el fin (`"end"`) del tramo al tiempo t sin
    cruzar el otro borde."""
    a, b = float(part["t_ini"]), float(part["t_fin"])
    t = round(float(t), 3)
    if edge == "start":
        if t > b - min_len:
            raise ValueError("el inicio no puede cruzar el fin del tramo")
        return {**part, "t_ini": max(0.0, t)}
    if edge == "end":
        if t < a + min_len:
            raise ValueError("el fin no puede cruzar el inicio del tramo")
        return {**part, "t_fin": t}
    raise ValueError(f"borde desconocido: {edge}")


def shift_ranges(ranges: list[dict], delta: float, duration: float) -> list[dict]:
    """Mueve todos los tramos `delta` segundos, acotando para que ninguno salga del
    medio (el delta efectivo es el mismo para todos)."""
    if not ranges:
        return []
    lo = min(float(r["t_ini"]) for r in ranges)
    hi = max(float(r["t_fin"]) for r in ranges)
    delta = max(-lo, min(float(delta), float(duration) - hi))
    return [{**r, "t_ini": round(float(r["t_ini"]) + delta, 3),
             "t_fin": round(float(r["t_fin"]) + delta, 3)} for r in ranges]


def range_at(ranges: list[dict], t: float, *, preferred: int | None = None) -> int | None:
    """Índice del tramo que contiene t (el `preferred` si lo contiene), o None."""
    if preferred is not None and 0 <= preferred < len(ranges):
        r = ranges[preferred]
        if float(r["t_ini"]) <= t <= float(r["t_fin"]):
            return preferred
    for index, r in enumerate(ranges):
        if float(r["t_ini"]) <= t <= float(r["t_fin"]):
            return index
    return None


# ---- estados ----
# Tres teclas explícitas (decisión de Gabriel, 2026-09-07): E acepta, X desactiva,
# P activa (vuelve a propuesto). Ninguna alterna: pulsar dos veces deja lo mismo, y
# sobre varios items todos reciben el MISMO estado. Propuesto y aceptado se cortan
# igual; aceptado es solo la marca de revisión humana.
STATE_ACTIONS = {"edit.accept": "accepted", "edit.toggle": "disabled", "edit.activate": "proposed"}


def state_for(action: str) -> str:
    """Estado que impone una acción de estado (E/X/P), sobre uno o varios items."""
    try:
        return STATE_ACTIONS[action]
    except KeyError:
        raise ValueError(f"acción de estado desconocida: {action}") from None


# ---- items de una capa ----
def new_item_id() -> str:
    import uuid
    return "item-" + uuid.uuid4().hex[:12]


def split_layer_item(layer: dict, item_id: str, t: float, *, segment: int | None = None,
                     min_len: float = FRAME) -> tuple[dict, dict]:
    """Divide el tramo del item que contiene t en dos ITEMS (el original conserva
    [a, t] y sus demás tramos; el nuevo recibe [t, b] con la misma etiqueta,
    comentario, estado y padre). Devuelve (capa nueva, item nuevo)."""
    layer = copy.deepcopy(layer)
    item = next((i for i in layer["items"] if i["item_id"] == item_id), None)
    if item is None:
        raise ValueError("el item ya no existe")
    index = range_at(item["ranges"], t, preferred=segment)
    if index is None:
        raise ValueError("el playhead no está dentro de un tramo del item")
    left, right = split_range(item["ranges"][index], t, min_len=min_len)
    item["ranges"][index] = left
    item["edited"] = True
    twin = {**copy.deepcopy(item), "item_id": new_item_id(), "ranges": [right], "edited": True}
    layer["items"].insert(layer["items"].index(item) + 1, twin)
    return layer, twin


def next_item(items: list[dict], current_id: str | None, direction: int) -> dict | None:
    """Item anterior/siguiente por tiempo de inicio dentro de un carril; sin actual,
    el primero (o el último)."""
    ordered = sorted(items, key=lambda i: (min(r["t_ini"] for r in i["ranges"]), i["item_id"]))
    if not ordered:
        return None
    ids = [i["item_id"] for i in ordered]
    if current_id not in ids:
        return ordered[0] if direction > 0 else ordered[-1]
    index = ids.index(current_id) + (1 if direction > 0 else -1)
    if not 0 <= index < len(ordered):
        return None
    return ordered[index]


# ---- recortes (trims.json) ----
def split_cut(document: dict, cut_id: str, t: float, *, min_len: float = FRAME) -> tuple[dict, dict]:
    """Un corte → dos cortes en t; el nuevo hereda origen, motivo, estado y `accepted`.
    Devuelve (documento nuevo, corte nuevo)."""
    document = copy.deepcopy(document)
    cut = next((c for c in document["cuts"] if c["cut_id"] == cut_id), None)
    if cut is None:
        raise ValueError("el recorte ya no existe")
    left, right = split_range(cut, t, min_len=max(min_len, editorial_trims.MIN_CUT_SECONDS))
    cut.update(t_ini=left["t_ini"], t_fin=left["t_fin"], edited=True)
    twin = editorial_trims.add_cut(document, right["t_ini"], right["t_fin"], origin=cut["origin"],
                                   reason=cut.get("reason", ""), enabled=cut["enabled"],
                                   accepted=cut.get("accepted", False), edited=True,
                                   confidence=cut.get("confidence"), chunk_id=cut.get("chunk_id"),
                                   evidence=copy.deepcopy(cut.get("evidence") or {}),
                                   warnings=list(cut.get("warnings") or []))
    for key in ("lane",):
        if key in cut:
            twin[key] = cut[key]
    return document, twin


# ---- bloques (plan) ----
def next_chunk_id(plan: dict) -> str:
    used = [int(m.group(1)) for c in plan.get("chunks") or []
            if (m := _CHUNK_ID.match(str(c.get("chunk_id", ""))))]
    return f"chunk-{(max(used) + 1) if used else 1:03d}"


def split_chunk(plan: dict, chunk_id: str, t: float, *, min_len: float = 1.0) -> tuple[dict, dict]:
    """Un bloque → dos bloques con un límite nuevo en t (antes de `snap_plan_to_safe_
    boundaries`, que valida y ajusta). Devuelve (plan nuevo, bloque nuevo)."""
    plan = copy.deepcopy(plan)
    chunks = plan["chunks"]
    index = next((i for i, c in enumerate(chunks) if c["chunk_id"] == chunk_id), None)
    if index is None:
        raise ValueError("el bloque ya no existe")
    chunk = chunks[index]
    left, right = split_range(chunk, t, min_len=min_len)
    chunk.update(t_ini=left["t_ini"], t_fin=left["t_fin"], edited=True)
    twin = {**copy.deepcopy(chunk), "chunk_id": next_chunk_id(plan), "t_ini": right["t_ini"],
            "t_fin": right["t_fin"], "title": f"{chunk.get('title') or chunk_id} (2)",
            "edited": True, "warnings": []}
    for key in ("first_utterance_id", "last_utterance_id", "semantic_t_ini", "semantic_t_fin"):
        twin.pop(key, None)
    chunks.insert(index + 1, twin)
    return plan, twin


# ---- gestos de la herramienta Corte (§7): caja que crea/estira o resta ----
# `parts` = [(item_id, t_ini, t_fin)] de un carril (un rango por item). Devuelven
# operaciones que el controlador aplica en UNA escritura:
#   ("create", a, b) · ("update", id, a, b) · ("delete", id) · ("split", id, (a, t), (t, b))
#   ("merge", survivor_id, [ids absorbidos], a, b)
def _overlapping(parts, a, b):
    return [(i, x, y) for i, x, y in parts if float(x) < b and float(y) > a]


def box_add(parts, a, b, *, min_len: float = FRAME) -> list[tuple]:
    """Caja [a, b]: si no toca ningún item nace uno; si toca uno se estira a la unión;
    si toca varios se funden en el primero (unión de todos). Los que solo se tocan por
    el borde siguen separados."""
    a, b = sorted((float(a), float(b)))
    if b - a < min_len:
        raise ValueError("la caja es demasiado corta")
    hits = sorted(_overlapping(parts, a, b), key=lambda p: (float(p[1]), float(p[2]), p[0]))
    if not hits:
        return [("create", round(a, 3), round(b, 3))]
    lo = min(a, *(float(x) for _, x, _ in hits))
    hi = max(b, *(float(y) for _, _, y in hits))
    survivor = hits[0][0]
    if len(hits) == 1:
        return [("update", survivor, round(lo, 3), round(hi, 3))]
    return [("merge", survivor, [i for i, _, _ in hits[1:]], round(lo, 3), round(hi, 3))]


def box_subtract(parts, a, b, *, min_len: float = FRAME) -> list[tuple]:
    """Shift + caja [a, b]: cada item que corte la caja se borra (cubierto entero), se
    recorta (toca un solo borde) o se divide en dos (la caja queda estrictamente
    dentro). Los restos más cortos que `min_len` se descartan con el item."""
    a, b = sorted((float(a), float(b)))
    if b - a < min_len:
        raise ValueError("la caja es demasiado corta")
    ops = []
    for item_id, x, y in _overlapping(parts, a, b):
        x, y = float(x), float(y)
        left = (x, a) if a - x >= min_len else None
        right = (b, y) if y - b >= min_len else None
        if left and right:
            ops.append(("split", item_id, (round(x, 3), round(a, 3)), (round(b, 3), round(y, 3))))
        elif left:
            ops.append(("update", item_id, round(x, 3), round(a, 3)))
        elif right:
            ops.append(("update", item_id, round(b, 3), round(y, 3)))
        else:
            ops.append(("delete", item_id))
    return ops


def marquee_select(lanes: dict, t0: float, t1: float) -> tuple[str | None, list[str]]:
    """Marquesina: `lanes` = {lid: parts} de los carriles que el rectángulo cubre.
    Selecciona en el carril con MÁS items dentro del intervalo (solo se seleccionan
    items de un mismo carril). Devuelve (lid, [item_ids ordenados por tiempo])."""
    t0, t1 = sorted((float(t0), float(t1)))
    best, best_ids = None, []
    for lid, parts in lanes.items():
        hits = sorted(_overlapping(parts, t0, t1), key=lambda p: (float(p[1]), p[0]))
        ids = list(dict.fromkeys(i for i, _, _ in hits))
        if len(ids) > len(best_ids):
            best, best_ids = lid, ids
    return best, best_ids
