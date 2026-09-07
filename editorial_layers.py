"""Contrato de capas y adaptadores: cada dato mantiene una sola fuente editable."""
from __future__ import annotations

import copy
import re
import uuid
from pathlib import Path

from editorial_io import atomic_write_json, digest_json, finite_time, read_json
import editorial_trims
from editorial_trims import identity, same_identity
from editorial_chunks import source_master_digest

SCHEMA = "editorial-layer/1"
PROPOSAL = "editorial-layers-proposal/1"
STATES = ("proposed", "accepted", "disabled")
ID = re.compile(r"[a-zA-Z][a-zA-Z0-9_-]{0,79}\Z")
RESERVED = {"autor", "bloques", "recortes", "CON", "PRN", "AUX", "NUL",
            *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}


def new_layer(master, name, *, kind="user", layer_id=None, master_digest=None):
    return dict(schema=SCHEMA, layer_id=layer_id or "layer-" + uuid.uuid4().hex[:12],
                kind=kind, name=name, color="#d09947", media_fingerprint=identity(master["media"]["fingerprint"]),
                source_master_digest=master_digest or source_master_digest(master), revision=0, items=[])


def media_context(info, fingerprint):
    """Contexto de edición previo al pipeline; nunca se publica como master inferido."""
    return dict(schema="editorial-layer-context/1", media=dict(path=info["path"],
                duration=info["duracion"], fingerprint=fingerprint), tracks={})


def new_item(start, end, label="Tramo", comment=""):
    return dict(item_id="item-" + uuid.uuid4().hex[:12], label=label, comment=comment,
                state="proposed", edited=True, parent_id=None, ranges=[dict(t_ini=start, t_fin=end)])


# ---- texto para la UI (puro: la barra de detalle del timeline lo usa y los tests
#      lo cubren sin Tk) ----
def clock(seconds):
    """m:ss.d, o h:mm:ss.d a partir de la hora (compacto, como el reloj del transporte)."""
    seconds = max(0.0, float(seconds))
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    if hours >= 1:
        return f"{int(hours)}:{int(minutes):02d}:{secs:04.1f}"
    return f"{int(minutes)}:{secs:04.1f}"


def ranges_summary(ranges):
    """«0:12.0 – 0:40.5 · 28.5 s», «1:02.0 · punto» o «3 tramos · 0:12.0 – 5:40.5 · 41.0 s»."""
    start = min(r["t_ini"] for r in ranges)
    end = max(r["t_fin"] for r in ranges)
    total = sum(r["t_fin"] - r["t_ini"] for r in ranges)
    if len(ranges) == 1:
        if end - start < .0005:
            return f"{clock(start)} · punto"
        return f"{clock(start)} – {clock(end)} · {total:.1f} s"
    return f"{len(ranges)} tramos · {clock(start)} – {clock(end)} · {total:.1f} s"


def elide(measure, text, max_width):
    """Recorta `text` con «…» para que entre en `max_width` píxeles según `measure`
    (normalmente `font.measure`). Sin lugar ni para la elipsis devuelve ''."""
    if max_width is None or measure(text) <= max_width:
        return text
    if measure("…") > max_width:
        return ""
    low, high = 0, len(text)
    while low < high:
        middle = (low + high + 1) // 2
        if measure(text[:middle] + "…") <= max_width:
            low = middle
        else:
            high = middle - 1
    return text[:low].rstrip() + "…"


def validate_items(items, duration, *, allow_points=False):
    if not isinstance(items, list):
        raise ValueError("items debe ser una lista")
    result, seen = copy.deepcopy(items), set()
    for item in result:
        identifier = item.get("item_id")
        if not isinstance(identifier, str) or not ID.fullmatch(identifier) or identifier in seen:
            raise ValueError("item_id inválido o duplicado")
        seen.add(identifier)
        for key in ("label", "comment"):
            if not isinstance(item.get(key, ""), str):
                raise ValueError(f"{key} debe ser texto")
            item.setdefault(key, "")
        if item.get("state", "proposed") not in STATES:
            raise ValueError("estado de capa inválido")
        item.setdefault("state", "proposed")
        if not isinstance(item.get("edited", False), bool):
            raise ValueError("edited debe ser booleano")
        ranges = item.get("ranges")
        if not isinstance(ranges, list) or not ranges:
            raise ValueError("un item necesita rangos")
        previous = -1.0
        for part in ranges:
            a = finite_time(part.get("t_ini"), name="t_ini")
            b = finite_time(part.get("t_fin"), name="t_fin")
            if a < 0 or b > duration or b < a or (b == a and not allow_points) or a < previous:
                raise ValueError("rango fuera del medio, vacío o solapado")
            part.update(t_ini=a, t_fin=b)
            previous = b
    by_id = {item["item_id"]: item for item in result}
    for item in result:
        parent_id = item.get("parent_id")
        visited = {item["item_id"]}
        while parent_id:
            if parent_id not in by_id or parent_id in visited:
                raise ValueError("jerarquía de subtemas desconocida o cíclica")
            visited.add(parent_id)
            if len(visited) > 32:
                raise ValueError("jerarquía demasiado profunda (máximo 32 niveles)")
            parent_id = by_id[parent_id].get("parent_id")
        if item.get("parent_id"):
            parent = by_id[item["parent_id"]]
            for part in item["ranges"]:
                if not any(r["t_ini"] <= part["t_ini"] and part["t_fin"] <= r["t_fin"]
                           for r in parent["ranges"]):
                    raise ValueError("subtema fuera de los rangos de su tema")
    return result


def validate_layer(layer, master):
    if layer.get("schema") != SCHEMA:
        raise ValueError("schema de capa desconocido")
    identifier = layer.get("layer_id", "")
    if not ID.fullmatch(identifier) or identifier in RESERVED or identifier.upper() in RESERVED:
        raise ValueError("layer_id inválido o reservado")
    if not same_identity(layer.get("media_fingerprint"), master["media"]["fingerprint"]):
        raise ValueError("la capa pertenece a otro medio")
    if layer.get("kind") not in ("user", "topics", "ai"):
        raise ValueError("tipo de capa desconocido")
    if not isinstance(layer.get("name"), str) or not layer["name"].strip():
        raise ValueError("falta el nombre de la capa")
    if not re.fullmatch(r"#[0-9a-fA-F]{6}", layer.get("color", "")):
        raise ValueError("color inválido (#RRGGBB)")
    return {**copy.deepcopy(layer), "items": validate_items(layer.get("items"), master["media"]["duration"])}


class LayerStore:
    def __init__(self, root, master):
        self.root, self.master = Path(root), master
        self.source_digest = source_master_digest(master)
        self.layers, self.stamps = {}, {}
        for path in sorted((self.root / "layers").glob("*.json")):
            layer = validate_layer(read_json(path), master)
            if path.stem != layer["layer_id"]:
                raise ValueError(f"nombre de archivo de capa incoherente: {path.name}")
            self.layers[layer["layer_id"]] = layer
            self.stamps[layer["layer_id"]] = digest_json(layer)

    def save(self, layer):
        value = validate_layer(layer, self.master)
        identifier = value["layer_id"]
        path = self.root / "layers" / (identifier + ".json")
        if path.exists() and digest_json(read_json(path)) != self.stamps.get(identifier):
            raise ValueError("la capa cambió fuera de esta ventana; vuelve a abrir el proyecto")
        value["revision"] = int(value.get("revision", 0)) + 1
        atomic_write_json(path, value)
        self.layers[identifier] = value
        self.stamps[identifier] = digest_json(value)
        return value

    def delete(self, identifier):
        # Tumba persistente: una respuesta AI antigua no resucita una capa borrada.
        self.save({**self.layers[identifier], "deleted": True})

    def visible(self):
        return [copy.deepcopy(v) for v in self.layers.values() if not v.get("deleted")]


def adapters(master, *, plan=None, trims=None, marks=None):
    """Snapshots derivados; las mutaciones se escriben mediante cada adaptador de UI."""
    result = []
    def layer(identifier, name, color, items):
        result.append(dict(schema=SCHEMA, layer_id=identifier, kind=identifier, name=name,
                           color=color, items=items, media_fingerprint=identity(master["media"]["fingerprint"])))
    if marks is not None:
        items = []
        for mark in marks:
            a, b = ((mark["t"], mark["t"]) if mark["tipo"] == "punto"
                    else (mark["t_ini"], mark["t_fin"]))
            items.append(dict(item_id=mark["id"], label=mark.get("label") or mark["id"],
                comment=mark.get("prompt") or "", state={"incluir": "accepted", "excluir": "disabled"}.get(
                    mark.get("decision"), "proposed"), edited=True, ranges=[dict(t_ini=a, t_fin=b)]))
        layer("autor", "Marcas del autor", "#d09947", items)
    if plan:
        layer("bloques", "Bloques", "#478baf", [dict(item_id=c["chunk_id"], label=c["title"],
            comment=c.get("comment", c.get("summary", "")), state="proposed", edited=c.get("edited", False),
            ranges=[dict(t_ini=c["t_ini"], t_fin=c["t_fin"])]) for c in plan["chunks"]])
    if trims is not None:
        # un carril de UI por `lane` del mismo trims.json (§10): «ai» encima de «main»
        by_lane = {}
        for c in trims["cuts"]:
            by_lane.setdefault(editorial_trims.cut_lane(c), []).append(c)
        for lane in editorial_trims.lanes(trims):
            items = [dict(item_id=c["cut_id"], label=c["cut_id"], comment=c["reason"],
                          state=cut_state(c), edited=c["edited"], origin=c["origin"],
                          ranges=[dict(t_ini=c["t_ini"], t_fin=c["t_fin"])])
                     for c in by_lane.get(lane["lane_id"], [])]
            result.append(dict(schema=SCHEMA, layer_id=trims_lane_id(lane["lane_id"]), kind="recortes",
                               lane=lane["lane_id"], name=lane["name"], color=lane["color"], items=items,
                               media_fingerprint=identity(master["media"]["fingerprint"])))
    return result


def trims_lane_id(lane_id: str) -> str:
    return f"trims:{lane_id}"


def lane_of(layer_id: str) -> str | None:
    """`trims:<lane>` → `<lane>`; otro id → None."""
    return layer_id[6:] if isinstance(layer_id, str) and layer_id.startswith("trims:") else None


def item_depth(items_by_id: dict, item: dict) -> int:
    depth, parent = 0, item.get("parent_id")
    seen = set()
    while parent and parent in items_by_id and parent not in seen:
        seen.add(parent)
        depth += 1
        parent = items_by_id[parent].get("parent_id")
    return depth


def split_by_depth(layer: dict) -> list[dict]:
    """Una capa `topics` se PRESENTA como varios carriles por profundidad (§9):
    «Temas» (sin padre), «Subtemas» (1), «Subtemas 2»… Cada carril sabe a qué
    `layer_id` pertenece (`source_layer_id`); la capa guardada sigue siendo una."""
    if layer.get("kind") != "topics":
        return [layer]
    by_id = {i["item_id"]: i for i in layer["items"]}
    buckets = {}
    for item in layer["items"]:
        buckets.setdefault(item_depth(by_id, item), []).append(item)
    depths = sorted(buckets) or [0]
    result = []
    for depth in depths:
        name = "Temas" if depth == 0 else "Subtemas" if depth == 1 else f"Subtemas {depth}"
        result.append({**layer, "layer_id": f"topics:{layer['layer_id']}:{depth}",
                       "source_layer_id": layer["layer_id"], "depth": depth, "name": name,
                       "items": buckets.get(depth, [])})
    return result


def source_layer_id(layer_id: str) -> str:
    """`topics:<id>:<n>` → `<id>`; cualquier otro id de capa propia se devuelve tal cual."""
    if isinstance(layer_id, str) and layer_id.startswith("topics:"):
        return layer_id.split(":")[1]
    return layer_id


# ---- orden de carriles (views/lanes.json, §10): solo presentación, reconstruible ----
LANES_VIEW = "editorial-lanes-view/1"


def _default_rank(layer: dict) -> tuple:
    lid = layer["layer_id"]
    if lid == "autor":
        return (0, 0, "")
    if lid == "bloques":
        return (1, 0, "")
    if lid.startswith("topics:"):
        return (2, int(layer.get("depth", 0)), layer.get("source_layer_id", ""))
    if lid == "trims:ai":
        return (3, 0, "")
    if lid == "trims:main":
        return (4, 0, "")
    if lid.startswith("trims:"):
        return (3, 1, lid)
    return (5, 0, lid)


def order_layers(ui_layers: list[dict], saved_order: list | None) -> list[dict]:
    """Ordena los carriles de UI según `saved_order` (ids); los desconocidos del orden
    se descartan y los carriles sin entrada van a su posición por defecto (Marcas del
    autor · Bloques · Temas · Subtemas · Cortes sugeridos (AI) · Recortes · capas)."""
    by_id = {l["layer_id"]: l for l in ui_layers}
    ordered = [by_id[i] for i in dict.fromkeys(saved_order or []) if i in by_id]
    placed = {l["layer_id"] for l in ordered}
    for layer in sorted(ui_layers, key=_default_rank):
        if layer["layer_id"] in placed:
            continue
        rank = _default_rank(layer)
        # detrás del ÚLTIMO carril ya colocado de rango menor o igual (su vecino
        # natural); si no hay ninguno, al principio
        index = next((k + 1 for k in range(len(ordered) - 1, -1, -1)
                      if _default_rank(ordered[k]) <= rank), 0)
        ordered.insert(index, layer)
        placed.add(layer["layer_id"])
    return ordered


def insert_above(order: list, new_id: str, selected_id) -> list:
    """Posición de una capa nueva: justo antes del carril seleccionado; sin selección,
    arriba de los carriles de recortes (`trims:*`); si no hay, al final."""
    order = [i for i in order if i != new_id]
    if selected_id in order:
        index = order.index(selected_id)
    else:
        index = next((k for k, i in enumerate(order) if str(i).startswith("trims:")), len(order))
    return order[:index] + [new_id] + order[index:]


def move_in_order(order: list, layer_id: str, delta: int) -> list:
    order = list(order)
    if layer_id not in order:
        return order
    index = order.index(layer_id)
    target = max(0, min(len(order) - 1, index + delta))
    order.insert(target, order.pop(index))
    return order


def load_lane_order(root) -> list:
    path = Path(root) / "views" / "lanes.json"
    try:
        data = read_json(path) if path.is_file() else None
    except (OSError, ValueError):
        return []
    if not isinstance(data, dict) or not isinstance(data.get("order"), list):
        return []
    return [str(i) for i in data["order"]]


def save_lane_order(root, order: list) -> Path:
    return atomic_write_json(Path(root) / "views" / "lanes.json",
                             {"schema": LANES_VIEW, "order": [str(i) for i in order]})


def cut_state(cut: dict) -> str:
    """Estado de UI de un recorte (§3.1): desactivado si no está `enabled`; aceptado si
    lleva la marca de revisión humana `accepted`; si no, propuesto. La exportación
    sigue mirando solo `enabled`."""
    if not cut.get("enabled", True):
        return "disabled"
    return "accepted" if cut.get("accepted") else "proposed"


def write_snapshot(root, master, layers, *, master_digest=None):
    value = {"schema": "editorial-layers-view/1", "source_master_digest": master_digest or source_master_digest(master),
             "layers": layers}
    value["source_layers_digest"] = digest_json(value)
    atomic_write_json(Path(root) / "views" / "layers.json", value)
    return value


def proposal_layers(proposal) -> list:
    """`layer` (compatibilidad) y/o `layers: [...]` de una respuesta (§9)."""
    result = []
    if isinstance(proposal.get("layer"), dict):
        result.append(proposal["layer"])
    extra = proposal.get("layers")
    if extra is not None:
        if not isinstance(extra, list) or not all(isinstance(l, dict) for l in extra):
            raise ValueError("layers debe ser una lista de capas")
        result.extend(extra)
    if not result:
        raise ValueError("la respuesta no trae ninguna capa")
    seen = set()
    for layer in result:
        if layer.get("layer_id") in seen:
            raise ValueError("layer_id repetido en la respuesta")
        seen.add(layer.get("layer_id"))
    return result


def merge_responses(store, proposal, snapshot) -> list:
    """Funde CADA capa de la respuesta con las mismas protecciones que una sola
    (items editados o borrados por el humano nunca se pisan; tumba persistente)."""
    if proposal.get("schema") != PROPOSAL:
        raise ValueError("schema de respuesta de capas desconocido")
    if proposal.get("source_master_digest") != store.source_digest:
        raise ValueError("propuesta para otro master")
    if proposal.get("source_layers_digest") != snapshot["source_layers_digest"]:
        raise ValueError("las capas cambiaron; prepara otra revisión AI")
    validated = [validate_layer(layer, store.master) for layer in proposal_layers(proposal)]
    for layer in validated:
        old = store.layers.get(layer["layer_id"])
        if old and old.get("deleted"):
            raise ValueError(f"la capa {layer['layer_id']} fue borrada por el usuario")
    return [_merge_layer(store, layer) for layer in validated]


def merge_response(store, proposal, snapshot):
    """Compatibilidad: devuelve la capa fundida cuando la respuesta trae una sola."""
    merged = merge_responses(store, proposal, snapshot)
    return merged[0] if len(merged) == 1 else merged


def _merge_layer(store, layer):
    old = store.layers.get(layer["layer_id"])
    if old:
        if old.get("deleted"):
            raise ValueError("la capa fue borrada por el usuario")
        protected = {i["item_id"]: i for i in old["items"] if i.get("edited")}
        deleted = set(old.get("deleted_item_ids", []))
        # Borrar un padre también protege sus descendientes propuestos.
        while True:
            descendants = {i["item_id"] for i in layer["items"] if i.get("parent_id") in deleted}
            if descendants <= deleted:
                break
            deleted |= descendants
        layer["deleted_item_ids"] = sorted(deleted)
        layer["items"] = [dict(i, edited=False) for i in layer["items"]
                          if i["item_id"] not in protected and i["item_id"] not in deleted]
        layer["items"].extend(protected.values())
        layer["revision"] = old["revision"]
    else:
        layer["items"] = [dict(i, edited=False) for i in layer["items"]]
    return store.save(layer)
