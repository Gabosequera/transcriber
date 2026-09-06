"""Contrato de capas y adaptadores: cada dato mantiene una sola fuente editable."""
from __future__ import annotations

import copy
import re
import uuid
from pathlib import Path

from editorial_io import atomic_write_json, digest_json, finite_time, read_json
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
    if layer.get("kind") not in ("user", "topics"):
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
        layer("recortes", "Recortes", "#728bd0", [dict(item_id=c["cut_id"], label=c["cut_id"],
            comment=c["reason"], state="proposed" if c["enabled"] else "disabled", edited=c["edited"],
            origin=c["origin"], ranges=[dict(t_ini=c["t_ini"], t_fin=c["t_fin"])]) for c in trims["cuts"]])
    return result


def write_snapshot(root, master, layers, *, master_digest=None):
    value = {"schema": "editorial-layers-view/1", "source_master_digest": master_digest or source_master_digest(master),
             "layers": layers}
    value["source_layers_digest"] = digest_json(value)
    atomic_write_json(Path(root) / "views" / "layers.json", value)
    return value


def merge_response(store, proposal, snapshot):
    if proposal.get("schema") != PROPOSAL:
        raise ValueError("schema de respuesta de capas desconocido")
    if proposal.get("source_master_digest") != store.source_digest:
        raise ValueError("propuesta para otro master")
    if proposal.get("source_layers_digest") != snapshot["source_layers_digest"]:
        raise ValueError("las capas cambiaron; prepara otra revisión AI")
    layer = validate_layer(proposal["layer"], store.master)
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
