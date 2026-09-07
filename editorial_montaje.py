"""Montaje por temas: la secuencia de clips que se reordena en el timeline (plan §7).

`views/montaje.json` (`editorial-montaje/1`) es una SECUENCIA con pistas de video
apiladas (`V1` abajo, `V2` encima, …). Cada clip toma un rango del medio abierto
(normalmente el hijo recortado) y lo coloca en un tiempo de secuencia; el audio sigue
al video (un clip lleva todas las pistas de audio de su rango). Reglas:

  · `seq_fin = seq_ini + (source_fin − source_ini)`; en una misma pista los clips no se
    solapan. Entre pistas sí: la de ARRIBA tapa a la de abajo durante su intervalo (video
    y audio).
  · `flatten(doc)` es la secuencia lineal resultante, sin huecos (el montaje no tiene
    negro): lo que reproduce el preview y lo que exporta ffmpeg, en ORDEN DE SECUENCIA.
  · Estados: `proposed` (de la AI), `accepted` (E), `disabled` (X: no se reproduce ni
    exporta, se conserva); `edited: true` cuando el humano lo tocó (la AI no lo pisa).
  · Todas las operaciones son puras: devuelven (documento nuevo, clips tocados).

Los tiempos son segundos: `source_*` del medio abierto (T0), `seq_*` de la secuencia.
Módulo puro (sin Tk), como `editorial_trims`.
"""
from __future__ import annotations

import copy
import math
import re
from bisect import bisect_right
from datetime import datetime, timezone
from pathlib import Path

from editorial_io import atomic_write_json, digest_json, finite_time, read_json
from editorial_trims import IDENTITY_KEYS, identity, same_identity

SCHEMA = "editorial-montaje/1"
CLIP_ID = re.compile(r"clip-\d{6,}\Z")
TRACK_ID = re.compile(r"V[1-9]\d{0,2}\Z")
STATES = ("proposed", "accepted", "disabled")
ORIGINS = ("ai", "user")
MIN_CLIP_SECONDS = 1.0 / 30.0             # un fotograma
DEFAULT_TARGET_SECONDS = 900              # 15 min (Gabriel dijo «14 o 16»)
EPS = 1e-3


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _round(value: float) -> float:
    return round(float(value), 3)


# ------------------------------------------------------------------ documento --
def new_document(fingerprint: dict, duration: float, *, target_seconds: float = DEFAULT_TARGET_SECONDS) -> dict:
    return {"schema": SCHEMA, "media": identity(fingerprint),
            "duration_source": _round(finite_time(duration, name="duration")),
            "target_seconds": _round(target_seconds), "revision": 0, "next_id": 1,
            "updated_at": _now(), "tracks": [{"track_id": "V1", "name": "V1"}], "clips": [],
            "analysis": {"request_id": None, "pass": 0}}


def track_number(track_id: str) -> int:
    return int(track_id[1:])


def track_id(number: int) -> str:
    return f"V{int(number)}"


def seq_fin(clip: dict) -> float:
    return _round(clip["seq_ini"] + clip["source_fin"] - clip["source_ini"])


def clip_seconds(clip: dict) -> float:
    return _round(clip["source_fin"] - clip["source_ini"])


def _normalize_clip(clip: dict, duration: float, index: int) -> dict:
    if not isinstance(clip, dict):
        raise ValueError(f"clip {index + 1} no es un objeto")
    clip_id = str(clip.get("clip_id") or "")
    if not CLIP_ID.fullmatch(clip_id):
        raise ValueError(f"clip_id inválido: {clip_id!r}")
    track = str(clip.get("track_id") or "V1")
    if not TRACK_ID.fullmatch(track):
        raise ValueError(f"{clip_id}: pista inválida {track!r}")
    a = finite_time(clip.get("source_ini"), name=f"{clip_id}.source_ini")
    b = finite_time(clip.get("source_fin"), name=f"{clip_id}.source_fin")
    if a < 0 or b > duration + 0.001 or b - a < MIN_CLIP_SECONDS - 1e-9:
        raise ValueError(f"{clip_id}: rango fuente inválido {a:.3f}..{b:.3f}")
    seq = finite_time(clip.get("seq_ini", 0.0), name=f"{clip_id}.seq_ini")
    if seq < 0:
        raise ValueError(f"{clip_id}: seq_ini negativo")
    origin = str(clip.get("origin") or "user")
    if origin not in ORIGINS:
        raise ValueError(f"{clip_id}: origen desconocido {origin!r}")
    state = str(clip.get("state") or "proposed")
    if state not in STATES:
        raise ValueError(f"{clip_id}: estado desconocido {state!r}")
    topics = clip.get("topic_ids") or []
    if not isinstance(topics, list) or not all(isinstance(t, str) for t in topics):
        raise ValueError(f"{clip_id}: topic_ids debe ser una lista de textos")
    confidence = clip.get("confidence")
    if confidence is not None:
        confidence = max(0.0, min(1.0, finite_time(confidence, name=f"{clip_id}.confidence")))
    return {**clip, "clip_id": clip_id, "track_id": track, "source_ini": _round(a),
            "source_fin": _round(min(b, duration)), "seq_ini": _round(seq),
            "label": str(clip.get("label") or ""), "topic_ids": list(topics), "origin": origin,
            "state": state, "edited": bool(clip.get("edited", False)),
            "reason": str(clip.get("reason") or ""), "confidence": confidence,
            "junction_note": str(clip.get("junction_note") or "")}


def _normalize_tracks(tracks, clips) -> list[dict]:
    """`tracks` ausente (documento viejo) = las que usan los clips, más `V1`."""
    result, seen = [], set()
    for index, track in enumerate(tracks or []):
        if not isinstance(track, dict):
            raise ValueError(f"pista {index + 1} no es un objeto")
        tid = str(track.get("track_id") or "")
        if not TRACK_ID.fullmatch(tid):
            raise ValueError(f"track_id inválido: {tid!r}")
        if tid in seen:
            raise ValueError(f"track_id duplicado: {tid}")
        seen.add(tid)
        result.append({**track, "track_id": tid, "name": str(track.get("name") or tid)})
    for tid in ["V1"] + [c["track_id"] for c in clips]:
        if tid not in seen:
            seen.add(tid)
            result.append({"track_id": tid, "name": tid})
    result.sort(key=lambda t: track_number(t["track_id"]))
    return result


def validate_document(document: dict, *, fingerprint: dict | None = None,
                      duration: float | None = None) -> dict:
    """Misma disciplina que `editorial_trims.validate_document`: identidad por
    fingerprint, números finitos, ids únicos, `next_id`, sin solapes en la misma pista."""
    if not isinstance(document, dict) or document.get("schema") != SCHEMA:
        raise ValueError(f"schema del montaje debe ser {SCHEMA}")
    if fingerprint is not None and not same_identity(document.get("media"), fingerprint):
        raise ValueError("el montaje pertenece a otro video")
    length = finite_time(document.get("duration_source", duration), name="duration_source")
    if duration is not None and abs(length - float(duration)) > 0.5:
        raise ValueError("el montaje fue creado para un medio de otra duración")
    clips = document.get("clips")
    if not isinstance(clips, list):
        raise ValueError("clips debe ser una lista")
    seen, normalized = set(), []
    for index, clip in enumerate(clips):
        item = _normalize_clip(clip, length, index)
        if item["clip_id"] in seen:
            raise ValueError(f"clip_id duplicado: {item['clip_id']}")
        seen.add(item["clip_id"])
        normalized.append(item)
    tracks = _normalize_tracks(document.get("tracks"), normalized)
    order = {t["track_id"]: i for i, t in enumerate(tracks)}
    normalized.sort(key=lambda c: (order[c["track_id"]], c["seq_ini"], c["clip_id"]))
    previous = {}
    for clip in normalized:
        last = previous.get(clip["track_id"])
        if last is not None and clip["seq_ini"] < seq_fin(last) - EPS:
            raise ValueError(f"{clip['clip_id']} se solapa con {last['clip_id']} en {clip['track_id']}")
        previous[clip["track_id"]] = clip
    used = [int(c["clip_id"][5:]) for c in normalized]
    next_id = max(int(document.get("next_id") or 1), (max(used) + 1) if used else 1)
    target = finite_time(document.get("target_seconds", DEFAULT_TARGET_SECONDS), name="target_seconds")
    analysis = document.get("analysis") if isinstance(document.get("analysis"), dict) else {}
    return {**document, "duration_source": _round(length), "target_seconds": _round(target),
            "revision": int(document.get("revision") or 0), "next_id": next_id,
            "tracks": tracks, "clips": normalized,
            "analysis": {"request_id": None, "pass": 0, **analysis}}


def load_document(path, *, fingerprint=None, duration=None) -> dict | None:
    path = Path(path)
    if not path.is_file():
        return None
    return validate_document(read_json(path), fingerprint=fingerprint, duration=duration)


def save_document(path, document: dict) -> Path:
    """Persiste validado (revisión +1), atómico, conservando la identidad de los dicts
    de clip que la UI tenga referenciados (como `editorial_trims.save_document`)."""
    validated = validate_document(document)
    live = {c["clip_id"]: c for c in document["clips"] if isinstance(c, dict)}
    clips = []
    for normalized in validated["clips"]:
        clip = live.get(normalized["clip_id"])
        if clip is None:
            clip = normalized
        else:
            clip.clear()
            clip.update(normalized)
        clips.append(clip)
    validated["clips"] = clips
    validated["revision"] = int(validated.get("revision") or 0) + 1
    validated["updated_at"] = _now()
    document.update(validated)
    return atomic_write_json(path, validated)


def content_digest(document: dict | None) -> str | None:
    """Digest del contenido sin campos volátiles (para el historial y el pedido)."""
    if not document:
        return None
    return digest_json({k: v for k, v in document.items() if k not in ("revision", "updated_at")})


# ------------------------------------------------------------------- consultas --
def clip_by_id(document: dict, clip_id: str) -> dict:
    clip = next((c for c in document["clips"] if c["clip_id"] == clip_id), None)
    if clip is None:
        raise ValueError(f"el clip {clip_id} ya no existe")
    return clip


def track_clips(document: dict, track: str, *, exclude: str | None = None) -> list[dict]:
    return sorted((c for c in document["clips"] if c["track_id"] == track and c["clip_id"] != exclude),
                  key=lambda c: c["seq_ini"])


def track_ids(document: dict) -> list[str]:
    return [t["track_id"] for t in document["tracks"]]


def extent(document: dict) -> float:
    """Fin de la secuencia tal como está colocada (con huecos, si los hay)."""
    return max((seq_fin(c) for c in document["clips"]), default=0.0)


def flatten(document: dict, *, gaps: bool = False) -> list[dict]:
    """Secuencia lineal: en cada instante manda el clip ACTIVO de la pista más alta.
    Devuelve tramos `{seq_ini, seq_fin, source_ini, source_fin, clip_id, track_id}` en
    orden de secuencia. Sin `gaps`, los huecos se eliminan y los `seq_*` se recompactan
    (es lo que exporta ffmpeg y reproduce el preview); con `gaps=True` los huecos
    aparecen como `{"gap": True, seq_ini, seq_fin}` y los `seq_*` son los colocados
    (es lo que dibuja y recorre el timeline)."""
    active = [c for c in document["clips"] if c["state"] != "disabled"]
    if not active:
        return []
    rank = {t: i for i, t in enumerate(track_ids(document))}
    points = sorted({c["seq_ini"] for c in active} | {seq_fin(c) for c in active})
    pieces = []
    for a, b in zip(points, points[1:]):
        if b - a <= EPS:
            continue
        mid = (a + b) / 2
        covering = [c for c in active if c["seq_ini"] - EPS <= mid <= seq_fin(c) + EPS
                    and c["seq_ini"] < b and seq_fin(c) > a]
        if not covering:
            if gaps:
                pieces.append({"gap": True, "seq_ini": _round(a), "seq_fin": _round(b)})
            continue
        top = max(covering, key=lambda c: rank.get(c["track_id"], 0))
        offset = a - top["seq_ini"]
        piece = {"seq_ini": _round(a), "seq_fin": _round(b), "clip_id": top["clip_id"],
                 "track_id": top["track_id"],
                 "source_ini": _round(top["source_ini"] + offset),
                 "source_fin": _round(top["source_ini"] + offset + (b - a))}
        last = pieces[-1] if pieces else None
        if (last and not last.get("gap") and last["clip_id"] == piece["clip_id"]
                and abs(last["source_fin"] - piece["source_ini"]) <= EPS
                and abs(last["seq_fin"] - piece["seq_ini"]) <= EPS):
            last["seq_fin"], last["source_fin"] = piece["seq_fin"], piece["source_fin"]
        else:
            pieces.append(piece)
    if gaps:
        return pieces
    cursor, compact = 0.0, []
    for piece in pieces:
        if piece.get("gap"):
            continue
        length = piece["source_fin"] - piece["source_ini"]
        compact.append({**piece, "seq_ini": _round(cursor), "seq_fin": _round(cursor + length)})
        cursor += length
    return compact


def total_seconds(document: dict) -> float:
    """Duración del montaje resultante (sin huecos)."""
    return _round(sum(p["source_fin"] - p["source_ini"] for p in flatten(document)))


def seq_to_source(document: dict, t: float) -> dict | None:
    """El tramo aplanado (con huecos) que contiene el tiempo de secuencia `t`, con
    `source_t` añadido; None en un hueco o fuera de la secuencia."""
    for piece in flatten(document, gaps=True):
        if piece.get("gap"):
            continue
        if piece["seq_ini"] - EPS <= t < piece["seq_fin"] + EPS:
            return {**piece, "source_t": _round(piece["source_ini"] + (t - piece["seq_ini"]))}
    return None


def source_to_seq(document: dict, t: float) -> list[float]:
    """Tiempos de secuencia en los que suena el instante fuente `t` (varios si un
    tramo fuente se usa dos veces; ninguno si quedó tapado o fuera del montaje)."""
    out = []
    for piece in flatten(document, gaps=True):
        if piece.get("gap"):
            continue
        if piece["source_ini"] - EPS <= t < piece["source_fin"] + EPS:
            out.append(_round(piece["seq_ini"] + (t - piece["source_ini"])))
    return out


def stats(document: dict | None) -> dict:
    clips = (document or {}).get("clips") or []
    by_origin = {o: sum(1 for c in clips if c["origin"] == o) for o in ORIGINS}
    by_state = {s: sum(1 for c in clips if c["state"] == s) for s in STATES}
    topics = {}
    for clip in clips:
        for topic in clip.get("topic_ids") or []:
            topics[topic] = topics.get(topic, 0) + 1
    return {"total": len(clips), "by_origin": by_origin, "by_state": by_state,
            "enabled": sum(1 for c in clips if c["state"] != "disabled"),
            "total_seconds": total_seconds(document) if document else 0.0,
            "extent": extent(document) if document else 0.0, "topics": topics,
            "tracks": len((document or {}).get("tracks") or [])}


class SequenceMap:
    """Mapa secuencia↔fuente para el reproductor (plan §7.3): construido una vez por
    cambio del documento; `to_source(seq)` → tramo con `source_t`; `siguiente(tramo)`
    → el tramo que sigue (saltando huecos) o None; `from_source(src, tramo)` → seq."""

    def __init__(self, document: dict):
        self.pieces = [p for p in flatten(document, gaps=True) if not p.get("gap")]
        self.starts = [p["seq_ini"] for p in self.pieces]
        self.extent = extent(document)
        self.total = _round(sum(p["seq_fin"] - p["seq_ini"] for p in self.pieces))

    def to_source(self, t: float):
        index = bisect_right(self.starts, float(t) + EPS) - 1
        if index < 0:
            return None
        piece = self.pieces[index]
        if t > piece["seq_fin"] + EPS:
            return None
        return {**piece, "index": index, "source_t": _round(piece["source_ini"] + max(0.0, t - piece["seq_ini"]))}

    def siguiente(self, piece_or_t):
        """El tramo posterior a `piece` (o al tiempo de secuencia `t`)."""
        if isinstance(piece_or_t, dict):
            index = piece_or_t.get("index", -1) + 1
        else:
            index = bisect_right(self.starts, float(piece_or_t) + EPS)
        if 0 <= index < len(self.pieces):
            return {**self.pieces[index], "index": index, "source_t": self.pieces[index]["source_ini"]}
        return None

    @staticmethod
    def from_source(source_t: float, piece: dict) -> float:
        return _round(piece["seq_ini"] + (float(source_t) - piece["source_ini"]))


# ---------------------------------------------------------------- operaciones --
def _clone(document: dict) -> dict:
    return copy.deepcopy(document)


def ensure_track(document: dict, number: int) -> dict:
    """Crea `V<number>` (y las intermedias) si faltan. Muta y devuelve el documento."""
    have = {t["track_id"] for t in document["tracks"]}
    for n in range(1, int(number) + 1):
        if track_id(n) not in have:
            document["tracks"].append({"track_id": track_id(n), "name": track_id(n)})
            have.add(track_id(n))
    document["tracks"].sort(key=lambda t: track_number(t["track_id"]))
    return document


def ensure_spare_track(document: dict) -> dict:
    """Siempre hay una pista vacía por encima de la última usada (para «arrastrar
    hacia arriba»); las vacías de más se quitan (nunca `V1`)."""
    used = {c["track_id"] for c in document["clips"]}
    top = max((track_number(t) for t in used), default=0)
    ensure_track(document, top + 1)
    document["tracks"] = [t for t in document["tracks"]
                          if track_number(t["track_id"]) <= top + 1]
    return document


def _new_clip(document: dict, source_ini: float, source_fin: float, seq_ini: float, *, track: str,
              origin: str, label: str = "", topic_ids=None, state: str = "proposed",
              edited: bool = False, **extra) -> dict:
    duration = float(document["duration_source"])
    a, b = sorted((max(0.0, float(source_ini)), min(duration, float(source_fin))))
    if b - a < MIN_CLIP_SECONDS - 1e-9:
        raise ValueError("un clip debe durar al menos un fotograma")
    clip = {"clip_id": f"clip-{int(document['next_id']):06d}", "track_id": track,
            "source_ini": _round(a), "source_fin": _round(b), "seq_ini": _round(max(0.0, seq_ini)),
            "label": label or "", "topic_ids": list(topic_ids or []), "origin": origin,
            "state": state, "edited": bool(edited), "reason": str(extra.pop("reason", "") or ""),
            "confidence": extra.pop("confidence", None),
            "junction_note": str(extra.pop("junction_note", "") or ""), "created_at": _now()}
    clip.update(extra)
    document["next_id"] = int(document["next_id"]) + 1
    return clip


def _check_track_free(document: dict, clip: dict, *, exclude: str | None = None):
    end = seq_fin(clip)
    for other in track_clips(document, clip["track_id"], exclude=exclude or clip["clip_id"]):
        if other["seq_ini"] < end - EPS and seq_fin(other) > clip["seq_ini"] + EPS:
            raise ValueError(f"el clip se solaparía con {other['clip_id']} en {clip['track_id']}")


def add_clip(document: dict, source_ini: float, source_fin: float, *, at: float | None = None,
             track: str = "V1", origin: str = "user", **extra) -> tuple[dict, dict]:
    """Añade un clip: `at=None` → al final de la pista. Devuelve (documento, clip)."""
    doc = _clone(document)
    ensure_track(doc, track_number(track))
    if at is None:
        at = max((seq_fin(c) for c in track_clips(doc, track)), default=0.0)
    clip = _new_clip(doc, source_ini, source_fin, at, track=track, origin=origin, **extra)
    _check_track_free(doc, clip)
    doc["clips"].append(clip)
    ensure_spare_track(doc)
    return validate_document(doc), clip


def split(document: dict, clip_id: str, seq_t: float) -> tuple[dict, list[dict]]:
    """Corta un clip en el tiempo de secuencia `seq_t` → dos clips (el nuevo hereda
    todo). Devuelve (documento, [izquierdo, derecho])."""
    doc = _clone(document)
    clip = clip_by_id(doc, clip_id)
    t = float(seq_t)
    if not (clip["seq_ini"] + MIN_CLIP_SECONDS <= t <= seq_fin(clip) - MIN_CLIP_SECONDS):
        raise ValueError("el punto de corte debe caer dentro del clip (al menos un fotograma a cada lado)")
    offset = t - clip["seq_ini"]
    right = {**copy.deepcopy(clip), "clip_id": f"clip-{int(doc['next_id']):06d}",
             "source_ini": _round(clip["source_ini"] + offset), "seq_ini": _round(t), "edited": True}
    doc["next_id"] = int(doc["next_id"]) + 1
    clip["source_fin"] = _round(clip["source_ini"] + offset)
    clip["edited"] = True
    doc["clips"].append(right)
    return validate_document(doc), [clip, right]


def ripple_close_gaps(document: dict, track: str) -> dict:
    """Pega los clips de una pista uno tras otro desde el primero (sin huecos)."""
    doc = _clone(document)
    cursor = None
    for clip in track_clips(doc, track):
        if cursor is None:
            cursor = clip["seq_ini"]
        clip["seq_ini"] = _round(cursor)
        cursor = seq_fin(clip)
    return validate_document(doc)


def remove(document: dict, clip_id: str, *, ripple: bool = True) -> tuple[dict, dict]:
    doc = _clone(document)
    clip = clip_by_id(doc, clip_id)
    doc["clips"] = [c for c in doc["clips"] if c["clip_id"] != clip_id]
    if ripple:
        length = clip_seconds(clip)
        for other in track_clips(doc, clip["track_id"]):
            if other["seq_ini"] >= seq_fin(clip) - EPS:
                other["seq_ini"] = _round(other["seq_ini"] - length)
    ensure_spare_track(doc)
    return validate_document(doc), clip


def _overwrite(doc: dict, clip: dict):
    """Modo overwrite: lo que el clip tapa en su pista se recorta, se borra o se parte."""
    a, b = clip["seq_ini"], seq_fin(clip)
    for other in track_clips(doc, clip["track_id"], exclude=clip["clip_id"]):
        oa, ob = other["seq_ini"], seq_fin(other)
        if ob <= a + EPS or oa >= b - EPS:
            continue
        if oa >= a - EPS and ob <= b + EPS:            # cubierto entero
            doc["clips"].remove(other)
        elif oa < a and ob > b:                        # lo parte en dos
            tail = {**copy.deepcopy(other), "clip_id": f"clip-{int(doc['next_id']):06d}",
                    "source_ini": _round(other["source_ini"] + (b - oa)), "seq_ini": _round(b), "edited": True}
            doc["next_id"] = int(doc["next_id"]) + 1
            other["source_fin"] = _round(other["source_ini"] + (a - oa))
            other["edited"] = True
            doc["clips"].append(tail)
        elif oa < a:                                   # toca el final del otro
            other["source_fin"] = _round(other["source_ini"] + (a - oa))
            other["edited"] = True
        else:                                          # toca el inicio del otro
            shift = b - oa
            other["source_ini"] = _round(other["source_ini"] + shift)
            other["seq_ini"] = _round(b)
            other["edited"] = True
    doc["clips"] = [c for c in doc["clips"] if clip_seconds(c) >= MIN_CLIP_SECONDS - 1e-9]


def move(document: dict, clip_id: str, seq_ini: float, track: str | None = None, *,
         mode: str = "insert") -> tuple[dict, dict]:
    """Mueve un clip a `seq_ini` (y a otra pista si se pide). `insert` (por defecto):
    el destino se ajusta al borde de clip más cercano si cae dentro de uno, los clips
    posteriores de esa pista se desplazan y la pista de origen cierra el hueco.
    `overwrite`: se coloca tal cual y recorta lo que tapa."""
    if mode not in ("insert", "overwrite"):
        raise ValueError(f"modo desconocido: {mode}")
    doc = _clone(document)
    clip = clip_by_id(doc, clip_id)
    origin_track, origin_start = clip["track_id"], clip["seq_ini"]
    target_track = track or clip["track_id"]
    ensure_track(doc, track_number(target_track))
    length = clip_seconds(clip)
    target = max(0.0, float(seq_ini))
    if mode == "insert":
        # el clip sale de su sitio (los de después en su pista cierran el hueco)…
        for other in track_clips(doc, origin_track, exclude=clip_id):
            if other["seq_ini"] >= origin_start + length - EPS:
                other["seq_ini"] = _round(other["seq_ini"] - length)
        # …y entra en un borde de la pista destino: dentro de un clip, al borde más cercano
        for other in track_clips(doc, target_track, exclude=clip_id):
            oa, ob = other["seq_ini"], seq_fin(other)
            if oa < target < ob:
                target = oa if target - oa < ob - target else ob
                break
        for other in track_clips(doc, target_track, exclude=clip_id):
            if other["seq_ini"] >= target - EPS:
                other["seq_ini"] = _round(other["seq_ini"] + length)
        clip.update(track_id=target_track, seq_ini=_round(target), edited=True)
    else:
        clip.update(track_id=target_track, seq_ini=_round(target), edited=True)
        _overwrite(doc, clip)
    ensure_spare_track(doc)
    return validate_document(doc), clip


def shift_clips(document: dict, clip_ids, delta: float, track_delta: int = 0) -> tuple[dict, list[dict]]:
    """Desplaza un CONJUNTO de clips `delta` s (y `track_delta` pistas) sin ripple,
    acotando para que ninguno quede antes de 0 ni por debajo de `V1`; si el resultado
    se solapa con un clip que no se mueve, falla sin tocar nada (arrastre del conjunto,
    Alt+flechas, subir/bajar de pista)."""
    ids = list(dict.fromkeys(clip_ids))
    if not ids:
        return document, []
    doc = _clone(document)
    moving = [clip_by_id(doc, i) for i in ids]
    delta = max(float(delta), -min(c["seq_ini"] for c in moving))
    lowest = min(track_number(c["track_id"]) for c in moving)
    track_delta = max(int(track_delta), 1 - lowest)
    for clip in moving:
        clip["seq_ini"] = _round(clip["seq_ini"] + delta)
        clip["track_id"] = track_id(track_number(clip["track_id"]) + track_delta)
        clip["edited"] = True
        ensure_track(doc, track_number(clip["track_id"]))
    ensure_spare_track(doc)
    return validate_document(doc), moving


def edge_limits(document: dict, clip_id: str, edge: str) -> tuple[float, float]:
    """(delta mínimo, delta máximo) que admite `trim_edge` sin salir del medio, sin
    cruzar el otro borde ni pisar al vecino de la pista."""
    clip = clip_by_id(document, clip_id)
    duration = float(document["duration_source"])
    length = clip_seconds(clip)
    neighbours = track_clips(document, clip["track_id"], exclude=clip_id)
    if edge == "start":
        previous = max((seq_fin(c) for c in neighbours if seq_fin(c) <= clip["seq_ini"] + EPS), default=0.0)
        low = max(-clip["source_ini"], previous - clip["seq_ini"])
        high = length - MIN_CLIP_SECONDS
        return _round(low), _round(high)
    if edge == "end":
        following = min((c["seq_ini"] for c in neighbours if c["seq_ini"] >= seq_fin(clip) - EPS),
                        default=math.inf)
        high = min(duration - clip["source_fin"], following - seq_fin(clip))
        low = -(length - MIN_CLIP_SECONDS)
        return _round(low), (_round(high) if math.isfinite(high) else high)
    raise ValueError(f"borde desconocido: {edge}")


def trim_edge(document: dict, clip_id: str, edge: str, delta: float) -> tuple[dict, dict]:
    """Mueve el inicio (`source_ini` y `seq_ini` juntos: el fin queda quieto) o el fin
    (`source_fin`) del clip `delta` segundos, acotado a `edge_limits`."""
    low, high = edge_limits(document, clip_id, edge)
    delta = max(low, min(float(delta), high))
    doc = _clone(document)
    clip = clip_by_id(doc, clip_id)
    if edge == "start":
        clip["source_ini"] = _round(clip["source_ini"] + delta)
        clip["seq_ini"] = _round(clip["seq_ini"] + delta)
    else:
        clip["source_fin"] = _round(clip["source_fin"] + delta)
    clip["edited"] = True
    return validate_document(doc), clip


def set_state(document: dict, clip_ids, state: str) -> tuple[dict, list[dict]]:
    if state not in STATES:
        raise ValueError(f"estado desconocido: {state}")
    doc = _clone(document)
    touched = []
    for clip_id in clip_ids:
        clip = clip_by_id(doc, clip_id)
        if clip["state"] != state:
            clip["state"] = state
            clip["edited"] = True
            touched.append(clip)
    return validate_document(doc), touched


def update_clip(document: dict, clip_id: str, **fields) -> tuple[dict, dict]:
    """Etiqueta, motivo, temas… (campos de texto) del clip; marca `edited`."""
    doc = _clone(document)
    clip = clip_by_id(doc, clip_id)
    for key in ("label", "reason", "topic_ids", "junction_note", "state"):
        if key in fields:
            clip[key] = fields[key]
    clip["edited"] = True
    return validate_document(doc), clip


def clip_edges(document: dict) -> list[float]:
    """Bordes de todos los clips en tiempo de secuencia (para ↑/↓ y el imán)."""
    out = []
    for clip in document["clips"]:
        out.extend((clip["seq_ini"], seq_fin(clip)))
    return sorted(set(out))


def ordered_clips(document: dict) -> list[dict]:
    """Los clips en orden de secuencia (todas las pistas), para Tab / Shift+Tab."""
    rank = {t: i for i, t in enumerate(track_ids(document))}
    return sorted(document["clips"], key=lambda c: (c["seq_ini"], -rank.get(c["track_id"], 0), c["clip_id"]))
