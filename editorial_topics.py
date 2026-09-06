"""Bucle externo de temas: mapa cronológico, recurrencias y revisión humana."""
from __future__ import annotations

import copy
from pathlib import Path

import editorial_chunks
import editorial_layers as layers
from editorial_io import atomic_write_json, atomic_write_text, digest_json, read_json, validate_range

SCHEMA = "editorial-topics-proposal/1"


def prepare(root, master, snapshot, *, scope=None):
    root = Path(root)
    scope = scope or dict(t_ini=0, t_fin=master["media"]["duration"])
    validate_range(scope["t_ini"], scope["t_fin"], master["media"]["duration"], label="ámbito")
    request = dict(schema="editorial-topics-request/1", scope=scope,
                   source_master_digest=snapshot["source_master_digest"],
                   source_layers_digest=snapshot["source_layers_digest"], pass_required=1)
    # Un nuevo ciclo no adopta una propuesta anterior por accidente.
    import uuid
    request["request_id"] = uuid.uuid4().hex
    request["layer_id"] = "topics-" + digest_json(scope)[:12]
    existing=root/'layers'/(request['layer_id']+'.json')
    if existing.exists() and read_json(existing).get('deleted'):
        request['layer_id'] += '-'+request['request_id'][:8]
    atomic_write_text(root / "views" / "topics-transcript.md",
                      editorial_chunks._chunk_transcript(master, scope["t_ini"], scope["t_fin"]))
    atomic_write_json(root / "views" / "topics-request.json", request)
    write_request(root, request)
    return request


def write_request(root, request):
    phase = request["pass_required"]
    previous = request.get("previous_pass_digest")
    text = f"""# Tarea 3 — Temas y subtemas (pasada {phase})

Lee `topics-request.json`, `layers.json` y TODO `topics-transcript.md`.
Los tiempos corresponden al T0 del medio abierto (hijo o padre).
Ámbito: {request['scope']['t_ini']:.3f}–{request['scope']['t_fin']:.3f} s.
request_id: {request['request_id']}
source_master_digest: {request['source_master_digest']}
source_layers_digest: {request['source_layers_digest']}
previous_pass_digest: {previous}

Escribe atómicamente `views/topics.proposed.json`, schema `{SCHEMA}`, los
identificadores de arriba, `pass: {phase}`, `complete: true` y `items`.
Cada item lleva item_id, label, comment, state=proposed, parent_id (null para tema),
ranges=[{{t_ini,t_fin}}]. IDs locales únicos. No inventes hablantes ni timestamps.

Pasada 1: avanza por toda la conversación y anota temas/subtemas cronológicamente.
Pasada 2: lee TODO `topics-pass1.json`, busca recurrencias y unifica cada tema
recurrente en UN item con varios rangos. Usa `source_item_ids` para indicar qué
items del primer mapa unificas: cada ID del mapa debe aparecer exactamente una
vez. Conserva los rangos del mapa validado; puedes unir rangos contiguos.
Los subtemas usan parent_id del tema unificado y quedan dentro de sus rangos.
Relee el contexto de los tramos, protege preguntas/respuestas, risas y setups.
Si falta texto, no declares complete. La app valida y conserva lo editado a mano.
Solo la segunda pasada crea la capa de temas. Para otro ciclo, el humano pulsa
de nuevo Analizar temas; las correcciones actuales viajan en layers.json.
"""
    return atomic_write_text(Path(root) / "views" / "topics-agent-request.md", text)


def _union(ranges):
    result = []
    for r in sorted(ranges, key=lambda r: r["t_ini"]):
        a, b = r["t_ini"], r["t_fin"]
        if result and a <= result[-1][1] + .001:
            result[-1][1] = max(result[-1][1], b)
        else:
            result.append([a, b])
    return result


def validate(proposal, master, request, snapshot, *, previous=None):
    if proposal.get("schema") != SCHEMA:
        raise ValueError("schema de temas desconocido")
    if proposal.get("complete") is not True:
        raise ValueError("la AI debe completar la lectura del ámbito")
    for key in ("request_id", "source_master_digest", "source_layers_digest"):
        if proposal.get(key) != request.get(key):
            raise ValueError(f"{key} no corresponde al ciclo actual")
    if request["source_master_digest"] != editorial_chunks.source_master_digest(master):
        raise ValueError("el master cambió durante el análisis")
    if request["source_layers_digest"] != snapshot["source_layers_digest"]:
        raise ValueError("las capas cambiaron durante el análisis; prepara otro ciclo")
    phase = proposal.get("pass")
    if isinstance(phase, bool) or phase not in (1,2) or phase != request["pass_required"]:
        raise ValueError("pasada fuera de orden")
    items = layers.validate_items(proposal.get("items"), master["media"]["duration"])
    a, b = request["scope"]["t_ini"], request["scope"]["t_fin"]
    for item in items:
        for r in item["ranges"]:
            if r["t_ini"] < a or r["t_fin"] > b:
                raise ValueError("tema fuera del ámbito solicitado")
    if phase == 1:
        intervals = editorial_chunks.boundary_intervals(master)
        by_id = {i["item_id"]: i for i in items}
        def depth(item):
            return 0 if not item.get("parent_id") else 1 + depth(by_id[item["parent_id"]])
        for item in sorted(items, key=depth):
            for index, r in enumerate(item["ranges"]):
                lower, upper = a, b
                if item.get("parent_id"):
                    parent = next((p for p in by_id[item["parent_id"]]["ranges"]
                                  if p["t_ini"] <= r["t_ini"] and p["t_fin"] >= r["t_fin"]), None)
                    if parent is None:
                        raise ValueError("el ajuste del tema padre deja un subtema fuera; revisa sus bordes")
                    lower, upper = parent["t_ini"], parent["t_fin"]
                if index:
                    lower = max(lower, item["ranges"][index-1]["t_fin"])
                if index+1 < len(item["ranges"]):
                    upper = min(upper, item["ranges"][index+1]["t_ini"])
                old = dict(r)
                diagnostics = {}
                for key in ("t_ini", "t_fin"):
                    lo = max(lower, old[key]-1.5)
                    hi = min(upper, old[key]+1.5)
                    # Evitar que dos bordes de un tramo corto se crucen.
                    if key == "t_ini":
                        hi = min(hi, old["t_fin"]-.001)
                    else:
                        lo = max(lo, r["t_ini"]+.001)
                    if lo > hi:
                        raise ValueError("no queda espacio para ajustar el tema")
                    r[key], diagnostics[key] = editorial_chunks.snap_boundary(master,old[key],lo,hi,intervals=intervals)
                r["proposed"] = old
                r["boundary_diagnostics"] = diagnostics
        # Ajustar padre primero puede encogerlo sobre subtemas: rechazar sin publicar.
        items = layers.validate_items(items,master["media"]["duration"])
    else:
        if not previous or proposal.get("previous_pass_digest") != digest_json(previous):
            raise ValueError("la segunda pasada debe referenciar el mapa completo vigente")
        known = {i["item_id"]: i for i in previous["items"]}
        used = []
        for item in items:
            sources = item.get("source_item_ids")
            if not isinstance(sources,list) or not sources or any(s not in known for s in sources):
                raise ValueError("source_item_ids inexistentes o vacíos")
            used.extend(sources)
            if _union(item["ranges"]) != _union([r for s in sources for r in known[s]["ranges"]]):
                raise ValueError("los rangos unificados deben conservar el mapa de la primera pasada")
        if sorted(used) != sorted(known):
            raise ValueError("la segunda pasada omitió o duplicó elementos del mapa")
    return {**copy.deepcopy(proposal),"items":items}


def import_proposal(store, proposal, snapshot):
    root, master = store.root, store.master
    request = read_json(root / "views" / "topics-request.json")
    previous = read_json(root / "views" / "topics-pass1.json") if request["pass_required"] == 2 else None
    validated = validate(proposal,master,request,snapshot,previous=previous)
    if validated["pass"] == 1:
        atomic_write_json(root / "views" / "topics-pass1.json",validated)
        request.update(pass_required=2,previous_pass_digest=digest_json(validated))
        atomic_write_json(root / "views" / "topics-request.json",request)
        write_request(root,request)
        return {"pass":1,"message":"Mapa de temas validado. Pide la segunda pasada de recurrencias a la AI."}
    identifier = request.get("layer_id") or "topics-" + digest_json(request["scope"])[:12]
    layer = layers.new_layer(master,"Temas y subtemas",kind="topics",layer_id=identifier)
    layer.update(color="#9a70bc",items=validated["items"],analysis={"request_id":request["request_id"],
                 "pass1_digest":digest_json(previous),"pass2_digest":digest_json(validated),"scope":request["scope"]})
    response = dict(schema=layers.PROPOSAL,source_master_digest=request["source_master_digest"],
                    source_layers_digest=request["source_layers_digest"],layer=layer)
    layers.merge_response(store,response,snapshot)
    atomic_write_json(root / "views" / "topics-pass2.json",validated)
    return {"pass":2,"message":"Temas recurrentes unificados: revisa y corrige sus tramos en la capa."}
