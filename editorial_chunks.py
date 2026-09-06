"""Planificación local, validación y materialización de chunks editoriales.

El plan local es un borrador reproducible basado en cambios léxicos y pausas. El mismo
contrato acepta después una propuesta de Codex u otro agente sin repetir audio.
"""
from __future__ import annotations

import math
import re
from collections import Counter
from pathlib import Path

from editorial_io import (SCHEMA_CHUNKS, atomic_write_json, atomic_write_text,
                          digest_json, finite_time, format_time, normalized_tokens,
                          overlaps, read_json, slice_events)


STOPWORDS = {
    "que", "como", "para", "pero", "porque", "con", "una", "uno", "por", "del",
    "las", "los", "esto", "esta", "ese", "esa", "muy", "mas", "hay", "ya", "no",
    "si", "de", "la", "el", "en", "y", "a", "un", "se", "lo", "es", "me", "te",
    "yo", "tu", "le", "al", "nos", "su", "mi",
}
CHUNK_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z")
BOUNDARY_SNAP_VERSION = "global-safe/2"
MAX_CHUNK_SECONDS = 3000.0
TARGET_CHUNK_SECONDS = 2700.0


def _tokens(utterances: list[dict]) -> Counter:
    return Counter(token for utterance in utterances for token in normalized_tokens(utterance["text"])
                   if token not in STOPWORDS)


def _cosine(left: Counter, right: Counter) -> float:
    if not left or not right:
        return 0.0
    dot = sum(value * right.get(key, 0) for key, value in left.items())
    norm_left = math.sqrt(sum(value * value for value in left.values()))
    norm_right = math.sqrt(sum(value * value for value in right.values()))
    return dot / max(1e-9, norm_left * norm_right)


def _boundary_score(utterances: list[dict], index: int, target: float,
                    ideal_span: float, context: float = 480.0) -> float:
    boundary = utterances[index]["t_ini"]
    left = [item for item in utterances[:index] if item["t_fin"] >= boundary - context]
    right = [item for item in utterances[index:] if item["t_ini"] <= boundary + context]
    dissimilarity = 1.0 - _cosine(_tokens(left), _tokens(right))
    previous_end = utterances[index - 1]["t_fin"] if index else boundary
    silence = min(1.0, max(0.0, boundary - previous_end) / 4.0)
    distance = abs(boundary - target) / max(1.0, ideal_span)
    signals = utterances[index]["signals"]
    active_penalty = max(0.0, float(signals.get("arousal_z_mean") or 0.0)) * 0.05
    active_penalty += float(signals.get("laughter_max") or 0.0) * 0.12
    return 0.72 * dissimilarity + 0.20 * silence - 0.35 * distance - active_penalty


def _title(utterances: list[dict], ordinal: int) -> str:
    common = [token for token, _ in _tokens(utterances).most_common(3)]
    return f"Chunk {ordinal}: {' · '.join(common)}" if common else f"Chunk {ordinal}"


def desired_count(master: dict, requested: int | None = None) -> int:
    duration = finite_time(master["media"]["duration"], name="media.duration")
    if duration <= 0:
        raise ValueError("media.duration debe ser positivo")
    minimum = math.ceil(duration / MAX_CHUNK_SECONDS)
    if requested is not None:
        return max(minimum, int(requested), 1)
    return max(minimum, math.ceil(duration / TARGET_CHUNK_SECONDS))


def source_master_digest(master: dict) -> str:
    """Identidad editorial del master, excluyendo campos derivados o de ejecución."""
    canonical = {key: value for key, value in master.items()
                 if key not in ("generated_at", "chunks")}
    return digest_json(canonical)


def propose_local(master: dict, *, count: int | None = None) -> dict:
    """Crea un borrador semántico. Es deliberadamente reemplazable por un agente."""
    duration = float(master["media"]["duration"])
    clean = set(master["conversation"]["clean_utterance_ids"])
    utterances = [item for item in master["conversation"]["utterances"]
                  if item["utterance_id"] in clean and item.get("text")]
    count = desired_count(master, count)
    ideal = duration / count
    boundaries = [0.0]
    for ordinal in range(1, count):
        target = ideal * ordinal
        radius = max(300.0, ideal * 0.28)
        remaining = count - ordinal
        minimum = boundaries[-1] + min(120.0, ideal * 0.20)
        minimum = max(minimum, duration - remaining * MAX_CHUNK_SECONDS)
        maximum = min(boundaries[-1] + MAX_CHUNK_SECONDS,
                      duration - remaining * min(120.0, ideal * 0.20))
        candidates = [index for index in range(1, len(utterances))
                      if abs(utterances[index]["t_ini"] - target) <= radius
                      and minimum < utterances[index]["t_ini"] < maximum]
        if not candidates:
            candidates = [index for index in range(1, len(utterances))
                          if minimum < utterances[index]["t_ini"] < maximum]
        if candidates:
            chosen = max(candidates, key=lambda index: _boundary_score(
                utterances, index, target, ideal))
            boundary = utterances[chosen]["t_ini"]
        else:
            # Una conversación extremadamente dispersa puede no ofrecer suficientes
            # bordes de intervención. En ese caso, el objetivo cae necesariamente en
            # silencio; se conserva la cobertura en vez de duplicar un límite.
            boundary = max(minimum, min(maximum, target))
        boundaries.append(round(boundary, 3))
    boundaries.append(duration)

    chunks = []
    for index, (start, end) in enumerate(zip(boundaries, boundaries[1:]), 1):
        inside = [item for item in utterances if item["t_ini"] < end and item["t_fin"] > start]
        first = inside[0]["utterance_id"] if inside else None
        last = inside[-1]["utterance_id"] if inside else None
        summary_text = " ".join(item["text"] for item in inside[:3]).strip()
        chunks.append({
            "chunk_id": f"chunk-{index:03d}",
            "t_ini": round(start, 3), "t_fin": round(end, 3),
            "title": _title(inside, index),
            "summary": (summary_text[:360] + ("…" if len(summary_text) > 360 else "")),
            "start_reason": "inicio de la grabación" if index == 1 else
                            "borde local con cambio léxico/pausa cercano al objetivo",
            "end_reason": "fin de la grabación" if index == count else
                          "borde local con cambio léxico/pausa cercano al objetivo",
            "first_utterance_id": first, "last_utterance_id": last,
            "confidence": 0.45,
            "warnings": ["Borrador local: conviene que Codex revise semántica y límites."],
        })
    document = {"schema": SCHEMA_CHUNKS, "planner": "local-semantic-draft/1",
                "duration": duration, "chunks": chunks}
    return validate_plan(document, master)


def validate_plan(document: dict, master: dict) -> dict:
    if not isinstance(document, dict) or document.get("schema") != SCHEMA_CHUNKS:
        raise ValueError(f"schema de chunks debe ser {SCHEMA_CHUNKS}")
    chunks = document.get("chunks")
    if not isinstance(chunks, list) or not chunks:
        raise ValueError("chunks debe ser una lista no vacía")
    duration = finite_time(master["media"]["duration"], name="media.duration")
    if duration <= 0:
        raise ValueError("media.duration debe ser positivo")
    if document.get("source_master_digest") is not None and document["source_master_digest"] != source_master_digest(master):
        raise ValueError("el plan pertenece a otro master o a metadata desactualizada")
    known_utterances = {item["utterance_id"] for item in master["conversation"]["utterances"]}
    previous_end = 0.0
    identifiers = set()
    normalized = []
    for index, chunk in enumerate(chunks):
        if not isinstance(chunk, dict):
            raise ValueError(f"chunk {index + 1} no es un objeto")
        chunk_id = str(chunk.get("chunk_id") or f"chunk-{index + 1}")
        if (not CHUNK_ID_PATTERN.fullmatch(chunk_id) or
                chunk_id.upper() in {"CON", "PRN", "AUX", "NUL",
                                     *(f"COM{i}" for i in range(1, 10)),
                                     *(f"LPT{i}" for i in range(1, 10))}):
            raise ValueError(f"chunk_id inválido: {chunk_id}")
        if chunk_id in identifiers:
            raise ValueError(f"chunk_id duplicado: {chunk_id}")
        identifiers.add(chunk_id)
        start = finite_time(chunk.get("t_ini"), name=f"{chunk_id}.t_ini")
        end = finite_time(chunk.get("t_fin"), name=f"{chunk_id}.t_fin")
        if start < 0 or abs(start - previous_end) > 0.001:
            raise ValueError(f"{chunk_id}: hueco/solape en {previous_end:.3f}→{start:.3f}")
        start = previous_end
        if abs(end - duration) <= 0.001:
            end = duration
        if round(end, 3) <= round(start, 3) or end > duration:
            raise ValueError(f"{chunk_id}: rango inválido {start:.3f}..{end:.3f}")
        if end - start > MAX_CHUNK_SECONDS + 0.001:
            raise ValueError(f"{chunk_id}: supera el máximo de 50 minutos")
        if not isinstance(chunk.get("warnings", []), list):
            raise ValueError(f"{chunk_id}: warnings debe ser una lista")
        for key in ("first_utterance_id", "last_utterance_id"):
            if chunk.get(key) is not None and chunk[key] not in known_utterances:
                raise ValueError(f"{chunk_id}: {key} desconocido {chunk[key]}")
        confidence = finite_time(chunk.get("confidence", 0.0),
                                 name=f"{chunk_id}.confidence")
        normalized.append({
            **chunk, "chunk_id": chunk_id, "t_ini": round(start, 3), "t_fin": round(end, 3),
            "title": str(chunk.get("title") or chunk_id),
            "summary": str(chunk.get("summary") or ""),
            "start_reason": str(chunk.get("start_reason") or ""),
            "end_reason": str(chunk.get("end_reason") or ""),
            "confidence": max(0.0, min(1.0, confidence)),
            "warnings": [str(value) for value in (chunk.get("warnings") or [])],
        })
        previous_end = end
    if abs(previous_end - duration) > 0.001:
        raise ValueError(f"los chunks terminan en {previous_end:.3f}, no en {duration:.3f}")
    return {**document, "duration": duration, "chunks": normalized}


def _boundary_intervals(master: dict) -> tuple[list[dict], list[dict]]:
    hard = []
    for track_id, track in master["tracks"].items():
        for word in track.get("words") or []:
            hard.append({"kind": "word", "track_id": track_id,
                         "t_ini": max(0.0, float(word["t_ini"]) - 0.04),
                         "t_fin": float(word["t_fin"]) + 0.04})
        for event in track.get("laughter") or []:
            hard.append({"kind": "laughter", "track_id": track_id,
                         "t_ini": max(0.0, float(event["t_ini"]) - 0.12),
                         "t_fin": float(event["t_fin"]) + 0.12})
    utterances = [{"kind": "utterance", **item}
                  for item in master["conversation"].get("utterances") or []]
    hard.sort(key=lambda item: (item["t_ini"], item["t_fin"]))
    return hard, utterances


def _merged_ranges(events: list[dict], lower: float, upper: float) -> list[tuple[float, float]]:
    merged: list[list[float]] = []
    for event in events:
        start, end = max(lower, float(event["t_ini"])), min(upper, float(event["t_fin"]))
        if end <= lower or start >= upper:
            continue
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return [(start, end) for start, end in merged]


def _boundary_safety(hard: list[dict], utterances: list[dict], timestamp: float) -> dict:
    active_hard = [event for event in hard
                   if event["t_ini"] < timestamp < event["t_fin"]]
    active_utterances = [event for event in utterances
                         if float(event["t_ini"]) < timestamp < float(event["t_fin"])]
    clearance = min((min(abs(timestamp - float(event["t_ini"])),
                         abs(timestamp - float(event["t_fin"])))
                     for event in hard
                     if not event["t_ini"] < timestamp < event["t_fin"]), default=60.0)
    return {
        "word_conflicts": sum(event["kind"] == "word" for event in active_hard),
        "laughter_conflicts": sum(event["kind"] == "laughter" for event in active_hard),
        "utterance_conflicts": len(active_utterances),
        "active_tracks": sorted({event["track_id"] for event in
                                  (*active_hard, *active_utterances)}),
        "hard_clearance_seconds": round(clearance, 3),
    }


def boundary_safety(master: dict, timestamp: float) -> dict:
    hard, utterances = _boundary_intervals(master)
    return _boundary_safety(hard, utterances, timestamp)


def boundary_intervals(master: dict) -> tuple[list[dict], list[dict]]:
    """(intervalos duros palabra/risa, intervenciones) de todas las pistas — precalcular
    una vez cuando se ajustan muchos bordes (recortes de la AI)."""
    return _boundary_intervals(master)


def snap_boundary(master: dict, target: float, lower: float, upper: float, *,
                  intervals: tuple[list[dict], list[dict]] | None = None) -> tuple[float, dict]:
    """Punto de corte más seguro dentro de [lower, upper] cercano a `target`: primero
    sin palabras ni risas de ninguna pista, después fuera de intervenciones, después el
    más cercano. Devuelve (timestamp, diagnóstico)."""
    return _snap_boundary(master, target, lower, upper, intervals=intervals)


def _snap_boundary(master: dict, target: float, lower: float, upper: float,
                   intervals: tuple[list[dict], list[dict]] | None = None) -> tuple[float, dict]:
    hard, utterances = intervals if intervals is not None else _boundary_intervals(master)
    # Solo intervalos cercanos: el costo no depende del podcast completo por candidato.
    hard = [event for event in hard if event["t_fin"] >= lower - 1 and event["t_ini"] <= upper + 1]
    utterances = [event for event in utterances if event["t_fin"] >= lower and event["t_ini"] <= upper]
    candidates = {target, lower, upper}
    for event in (*hard, *utterances):
        start, end = float(event["t_ini"]), float(event["t_fin"])
        if lower <= start <= upper:
            candidates.update((max(lower, start - 0.001), min(upper, start + 0.001)))
        if lower <= end <= upper:
            candidates.update((max(lower, end - 0.001), min(upper, end + 0.001)))

    cursor = lower
    for start, end in _merged_ranges(hard, lower, upper):
        if start > cursor:
            candidates.add((cursor + start) / 2)
        cursor = max(cursor, end)
    if cursor < upper:
        candidates.add((cursor + upper) / 2)

    evaluated = []
    for candidate in candidates:
        timestamp = round(max(lower, min(upper, candidate)), 3)
        safety = _boundary_safety(hard, utterances, timestamp)
        hard_conflicts = safety["word_conflicts"] + safety["laughter_conflicts"]
        score = (
            hard_conflicts,
            safety["utterance_conflicts"],
            -min(0.75, safety["hard_clearance_seconds"]),
            abs(timestamp - target),
            timestamp,
        )
        evaluated.append((score, timestamp, safety))
    _, timestamp, safety = min(evaluated, key=lambda item: item[0])
    return timestamp, safety


def snap_plan_to_safe_boundaries(document: dict, master: dict, *,
                                 radius_seconds: float = 15.0) -> dict:
    """Ajusta límites semánticos sin atravesar palabras/risas de ninguna pista."""
    if radius_seconds <= 0:
        raise ValueError("radio de ajuste de bordes inválido")
    semantic = validate_plan(document, master)
    duration = float(master["media"]["duration"])
    targets = [float(chunk["t_fin"]) for chunk in semantic["chunks"][:-1]]
    boundaries = [0.0]
    adjustments = []
    for index, target in enumerate(targets):
        next_target = targets[index + 1] if index + 1 < len(targets) else duration
        lower = max(boundaries[-1] + 0.001, target - radius_seconds,
                    duration - (len(targets) - index) * MAX_CHUNK_SECONDS)
        upper = min(next_target - 0.001, target + radius_seconds,
                    boundaries[-1] + MAX_CHUNK_SECONDS)
        if upper < lower:
            raise ValueError(f"no hay espacio para ajustar el borde cercano a {target:.3f}")
        final, safety = _snap_boundary(master, target, lower, upper)
        if safety["word_conflicts"] or safety["laughter_conflicts"]:
            raise ValueError(f"No hay un corte seguro cerca de {target:.3f}s; revisa el plan.")
        boundaries.append(final)
        adjustments.append({
            "boundary_index": index + 1,
            "proposed": round(target, 3),
            "final": final,
            "delta_seconds": round(final - target, 3),
            "search_radius_seconds": radius_seconds,
            "safety": safety,
        })
    boundaries.append(duration)

    clean = set(master["conversation"].get("clean_utterance_ids") or [])
    utterances = [item for item in master["conversation"].get("utterances") or []
                  if item["utterance_id"] in clean]
    chunks = []
    for index, chunk in enumerate(semantic["chunks"]):
        start, end = boundaries[index], boundaries[index + 1]
        inside = [item for item in utterances
                  if float(item["t_ini"]) < end and float(item["t_fin"]) > start]
        warnings = list(chunk.get("warnings") or [])
        if index and adjustments[index - 1]["safety"]["utterance_conflicts"]:
            warnings.append("El borde inicial cae dentro de una intervención porque no "
                            "existía silencio global dentro del radio de búsqueda.")
        if index < len(adjustments) and adjustments[index]["safety"]["utterance_conflicts"]:
            warnings.append("El borde final cae dentro de una intervención porque no "
                            "existía silencio global dentro del radio de búsqueda.")
        chunks.append({
            **chunk,
            "semantic_t_ini": round(float(chunk["t_ini"]), 3),
            "semantic_t_fin": round(float(chunk["t_fin"]), 3),
            "t_ini": round(start, 3),
            "t_fin": round(end, 3),
            "first_utterance_id": inside[0]["utterance_id"] if inside else None,
            "last_utterance_id": inside[-1]["utterance_id"] if inside else None,
            "warnings": warnings,
        })
    snapped = {
        **semantic,
        "boundary_snap": BOUNDARY_SNAP_VERSION,
        "boundary_adjustments": adjustments,
        "chunks": chunks,
    }
    return validate_plan(snapped, master)


def chunks_markdown(document: dict) -> str:
    lines = ["# Chunks editoriales", "", f"Planner: `{document.get('planner', 'agent')}`", ""]
    for chunk in document["chunks"]:
        lines.extend((f"## {chunk['title']} `{chunk['chunk_id']}`", "",
                      f"{format_time(chunk['t_ini'])}–{format_time(chunk['t_fin'])} "
                      f"({(chunk['t_fin'] - chunk['t_ini']) / 60:.1f} min)", "",
                      chunk.get("summary") or "Sin resumen.", "",
                      f"Inicio: {chunk.get('start_reason') or '—'}", "",
                      f"Final: {chunk.get('end_reason') or '—'}", ""))
        if chunk.get("warnings"):
            lines.append("Advertencias: " + " · ".join(chunk["warnings"]))
            lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _chunk_transcript(master: dict, start: float, end: float) -> str:
    clean = set(master["conversation"]["clean_utterance_ids"])
    tracks = master["tracks"]
    lines = []
    for utterance in master["conversation"]["utterances"]:
        if utterance["utterance_id"] not in clean or overlaps(start, end, utterance) <= 0:
            continue
        label = tracks[utterance["track_id"]]["label"]
        lines.append(f"{format_time(utterance['t_ini'])}–{format_time(utterance['t_fin'])} "
                     f"[{utterance['track_id']} · {label}] `{utterance['utterance_id']}`")
        lines.extend((utterance.get("text") or "", ""))
    return "\n".join(lines).rstrip() + "\n"


def _signal_summary(master: dict, start: float, end: float) -> dict:
    tracks = {}
    for track_id, track in master["tracks"].items():
        words = [word for word in track["words"] if overlaps(start, end, word) > 0]
        laughter = slice_events(track["laughter"], start, end)
        arousal = slice_events(track["arousal"], start, end)
        tracks[track_id] = {
            "label": track["label"], "word_count": len(words), "laughter_count": len(laughter),
            "laughter_max": max((float(item.get("max_conf", item.get("conf", 0.0)))
                                  for item in laughter), default=None),
            "arousal_z_max": max((float(item["arousal_z"]) for item in arousal), default=None),
            "intensity_z_max": max((float(item["intensity_z"]) for item in words
                                    if item.get("intensity_z") is not None), default=None),
            "emphasis_max": max((float(item["emphasis_score"]) for item in words
                                 if item.get("emphasis_score") is not None), default=None),
        }
    return {"t_ini": start, "t_fin": end, "tracks": tracks}


def materialize(root: str | Path, master: dict, document: dict) -> None:
    root = Path(root)
    for chunk in document["chunks"]:
        start, end = chunk["t_ini"], chunk["t_fin"]
        folder = root / "chunks" / chunk["chunk_id"]
        folder.mkdir(parents=True, exist_ok=True)
        atomic_write_text(folder / "transcript.md", _chunk_transcript(master, start, end))
        atomic_write_json(folder / "signals-summary.json", _signal_summary(master, start, end))
        laughter = {track_id: slice_events(track["laughter"], start, end)
                    for track_id, track in master["tracks"].items()}
        arousal = {track_id: slice_events(track["arousal"], start, end)
                   for track_id, track in master["tracks"].items()}
        intensity = {track_id: [{key: word.get(key) for key in (
            "word_id", "t_ini", "t_fin", "text", "rms_dbfs", "peak_dbfs", "intensity_z",
            "emphasis_score")}
                                for word in track["words"] if overlaps(start, end, word) > 0]
                     for track_id, track in master["tracks"].items()}
        atomic_write_json(folder / "laughter.json", laughter)
        atomic_write_json(folder / "arousal.json", arousal)
        atomic_write_json(folder / "intensity.json", intensity)
        atomic_write_text(folder / "analysis-request.md", f"""# Análisis profundo: {chunk['title']}

Lee `transcript.md` completo y después `signals-summary.json`. Usa risa, arousal e
intensidad como evidencia secundaria. Identifica historias, intercambios, reacciones,
picos y partes prescindibles. Para cada candidato escribe timecodes, qué sucede, por qué
funciona, evidencia textual/acústica por pista, confianza y problemas posibles.
""")
        moments_path = folder / "moments.md"
        if not moments_path.exists():
            atomic_write_text(moments_path, f"# Momentos — {chunk['title']}\n\n"
                              "Pendiente de análisis profundo por el agente.\n")


def apply_plan(root: str | Path, master_path: str | Path, document: dict, *,
               persist_selection: bool = False) -> dict:
    root, master_path = Path(root), Path(master_path)
    master = read_json(master_path)
    validated = validate_plan(document, master)
    validated["source_master_digest"] = source_master_digest(master)
    master["chunks"] = validated["chunks"]
    atomic_write_json(root / "views" / "chunks.json", validated)
    atomic_write_text(root / "views" / "chunks.md", chunks_markdown(validated))
    atomic_write_json(master_path, master)
    materialize(root, master, validated)
    if persist_selection:
        selection = {**validated, "source_master_digest": source_master_digest(master)}
        atomic_write_json(root / ".work" / "chunks.selected.json", selection)
    return validated
