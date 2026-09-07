"""Estado del ciclo con la AI externa, leído de `views/` (plan-montaje-ai.md §4.1). Puro.

La app prepara pedidos (`topics-request.json`, `trim-agent-request.md`,
`editorial-agent-request.md`, `montaje-request.json`), la AI responde con
`*.proposed.json` y la app valida e importa. Hasta ahora solo la consola decía en qué
punto estaba el ciclo; `status()` lo resume en una frase para la etiqueta del panel:

    {"stage": "topics_pass1", "text": "Pedido de temas listo…", "stale": False,
     "kind": "full" | "topics" | "trims" | "montage" | None, "error": None}

Es barato (unos `stat` y JSON chicos) y se llama en cada sondeo de la UI. No importa
Tk. La regla de «pedido viejo»: si el `source_layers_digest` del pedido vigente no es
el digest de la foto actual de capas (`layers_digest`), cualquier respuesta se
rechazará, así que se avisa antes de que llegue.
"""
from __future__ import annotations

import re
from pathlib import Path

from editorial_io import digest_json, read_json

STALE_TEXT = ("El pedido quedó viejo: editaste capas o recortes después de prepararlo. "
              "Pulsa Preparar de nuevo.")
_DIGEST_LINE = re.compile(r"^source_layers_digest:\s*(\S+)\s*$", re.MULTILINE)
_MODE_LINE = re.compile(r"^mode:\s*(\w+)\s*$", re.MULTILINE)


def _mtime(path: Path) -> float | None:
    try:
        return path.stat().st_mtime
    except OSError:
        return None


def _json(path: Path):
    try:
        return read_json(path) if path.is_file() else None
    except (OSError, ValueError):
        return None


def _text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8") if path.is_file() else ""
    except OSError:
        return ""


def topic_counts(validated: dict | None) -> tuple[int, int]:
    """(temas, subtemas) de una pasada validada (`topics-pass1/2.json`)."""
    items = (validated or {}).get("items") or []
    parents = sum(1 for i in items if not i.get("parent_id"))
    return parents, len(items) - parents


def _result(stage, text, *, kind=None, stale=False, error=None) -> dict:
    if error:
        text = text.rstrip() + "\nÚltimo error: " + " ".join(str(error).split())
    return {"stage": stage, "text": text, "stale": bool(stale), "kind": kind, "error": error}


def status(views_dir, *, layers_digest: str | None = None, last_error: str | None = None) -> dict:
    """Estado del ciclo AI a partir de los archivos de `views/`.

    `layers_digest`: digest de la foto actual de capas (sin escribirla); si el pedido
    vigente lleva otro, el estado es «pedido viejo». `last_error`: último error de
    importación que mostró la consola (se añade como segunda línea hasta que llegue
    un import válido)."""
    views = Path(views_dir)
    topics_request = _json(views / "topics-request.json")
    trim_request_text = _text(views / "trim-agent-request.md")
    trim_request_at = _mtime(views / "trim-agent-request.md")
    editorial_at = _mtime(views / "editorial-agent-request.md")
    topics_at = _mtime(views / "topics-request.json")
    montage_request = _json(views / "montaje-request.json")
    montage_at = _mtime(views / "montaje-request.json")

    candidates = [t for t in (trim_request_at, topics_at, montage_at) if t is not None]
    if not candidates:
        return _result("none", "Sin pedido preparado. Pulsa «Preparar para la AI».", error=last_error)
    newest = max(candidates)

    # ---- montaje (Fase E): el pedido más reciente manda ----
    if montage_request is not None and montage_at is not None and montage_at >= newest - 2.0:
        return _montage_status(views, montage_request, montage_at, layers_digest, last_error)

    # ---- ¿qué ciclo está vigente? completo (temas + recortes), solo temas o solo recortes ----
    full = (editorial_at is not None and topics_request is not None and trim_request_at is not None
            and abs(editorial_at - trim_request_at) <= 5.0 and topics_at is not None
            and topics_at >= trim_request_at - 5.0)
    if topics_request is not None and (trim_request_at is None or full
                                        or (topics_at or 0) > trim_request_at + 2.0):
        # el ciclo de temas (solo o como primera parte de la revisión completa)
        return _topics_status(views, topics_request, full=full, layers_digest=layers_digest,
                              last_error=last_error, trim_request_text=trim_request_text)
    return _trims_status(views, trim_request_text, trim_request_at, layers_digest, last_error)


def _stale(request_digest, layers_digest) -> bool:
    return bool(request_digest) and bool(layers_digest) and request_digest != layers_digest


def _topics_status(views, request, *, full, layers_digest, last_error, trim_request_text):
    kind = "full" if full else "topics"
    request_id = request.get("request_id")
    pass1 = _json(views / "topics-pass1.json")
    pass2 = _json(views / "topics-pass2.json")
    pass1_ok = bool(pass1) and pass1.get("request_id") == request_id
    pass2_ok = bool(pass2) and pass2.get("request_id") == request_id
    stale = _stale(request.get("source_layers_digest"), layers_digest)
    if pass2_ok:
        topics, subtopics = topic_counts(pass2)
        if full:
            trims = _trims_status(views, trim_request_text, _mtime(views / "trim-agent-request.md"),
                                  layers_digest, last_error, after_topics=(topics, subtopics))
            return trims
        return _result("topics_done", f"Capa de temas creada: {topics} temas y {subtopics} subtemas. "
                       "Corrige en el timeline; otro «Preparar» empieza un ciclo nuevo.",
                       kind=kind, error=last_error)
    if stale:
        return _result("stale", STALE_TEXT, kind=kind, stale=True, error=last_error)
    if pass1_ok:
        topics, subtopics = topic_counts(pass1)
        return _result("topics_pass2", f"Pasada 1 validada ({topics} temas, {subtopics} subtemas). "
                       "Esperando la pasada 2 en topics.proposed.json.", kind=kind, error=last_error)
    proposed_at = _mtime(views / "topics.proposed.json")
    topics_at = _mtime(views / "topics-request.json") or 0.0
    if proposed_at is not None and proposed_at >= topics_at - 1.0 and last_error:
        return _result("topics_rejected", "topics.proposed.json apareció pero no se validó.",
                       kind=kind, error=last_error)
    return _result("topics_pass1", "Pedido de temas listo (pasada 1). Esperando topics.proposed.json."
                   + (" Después vienen los recortes." if full else ""), kind=kind, error=last_error)


def _trims_status(views, request_text, request_at, layers_digest, last_error, *, after_topics=None):
    kind = "full" if after_topics else "trims"
    mode = (_MODE_LINE.search(request_text or "") or [None, "content"])[1]
    deep = mode == "deep"
    match = _DIGEST_LINE.search(request_text or "")
    stale = _stale(match.group(1) if match else None, layers_digest)
    lane_name = "Cortes profundos (AI)" if deep else "Cortes sugeridos (AI)"
    proposed_path = views / "trims.proposed.json"
    proposed_at = _mtime(proposed_path)
    trims = _json(views / "trims.json")
    imported = None
    if proposed_at is not None and request_at is not None and proposed_at >= request_at - 1.0:
        proposal = _json(proposed_path)
        ai = (trims or {}).get("ai") or {}
        if proposal is not None and ai.get("proposal_digest") == digest_json(proposal):
            imported = ai
    if imported is not None:
        count = int(imported.get("count") or 0)
        lane = imported.get("lane") or ("ai-deep" if deep else "ai")
        name = next((l.get("name") for l in (trims or {}).get("lanes") or [] if l.get("lane_id") == lane),
                    lane_name)
        return _result("trims_done", f"Recortes de la AI importados: {count} en «{name}». "
                       "Revísalos en el timeline antes de exportar.", kind=kind)
    if stale:
        return _result("stale", STALE_TEXT, kind=kind, stale=True, error=last_error)
    if proposed_at is not None and request_at is not None and proposed_at >= request_at - 1.0 and last_error:
        return _result("trims_rejected", "trims.proposed.json apareció pero no se importó.",
                       kind=kind, error=last_error)
    if after_topics:
        topics, subtopics = after_topics
        return _result("trims_wait", f"Capa de temas creada ({topics} temas). Esperando trims.proposed.json.",
                       kind=kind, error=last_error)
    what = "recortes profundos" if deep else "recortes"
    return _result("trims_request", f"Pedido de {what} listo. Esperando trims.proposed.json.",
                   kind=kind, error=last_error)


def _montage_status(views, request, request_at, layers_digest, last_error):
    """Fase E: pedido de montaje → propuesta → importada (pasada n)."""
    kind = "montage"
    stale = _stale(request.get("source_layers_digest"), layers_digest)
    wanted = int(request.get("pass_required") or 1)
    target = float(request.get("target_seconds") or 0)
    minutes = f"{target / 60:.0f} min" if target else ""
    proposed_path = views / "montaje.proposed.json"
    proposed_at = _mtime(proposed_path)
    imported = _json(views / f"montaje-pass{wanted - 1}.json") if wanted > 1 else None
    if imported is not None and imported.get("request_id") == request.get("request_id"):
        clips = len(imported.get("clips") or [])
        total = float(imported.get("total_seconds") or 0)
        return _result("montage_done", f"Montaje importado (pasada {wanted - 1}): {clips} clips, "
                       f"{total / 60:.1f} min. Corrige en modo Montaje; otro «Montaje por temas» "
                       "pide la pasada siguiente.", kind=kind)
    if stale:
        return _result("stale", STALE_TEXT, kind=kind, stale=True, error=last_error)
    if proposed_at is not None and request_at is not None and proposed_at >= request_at - 1.0 and last_error:
        return _result("montage_rejected", "montaje.proposed.json apareció pero no se importó.",
                       kind=kind, error=last_error)
    return _result("montage_request", f"Pedido de montaje listo (pasada {wanted}"
                   + (f", objetivo {minutes}" if minutes else "") + "). Esperando montaje.proposed.json.",
                   kind=kind, error=last_error)
