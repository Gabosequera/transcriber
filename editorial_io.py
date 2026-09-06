"""Utilidades compartidas por el pipeline editorial.

Este módulo concentra escritura atómica, hashes y normalización temporal. No importa
Tk ni modelos de ML, por lo que también puede usarse desde tests y herramientas CLI.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import tempfile
import time
import unicodedata
from pathlib import Path
from typing import Any, Iterable


SCHEMA_MASTER = "editorial-master/1"
SCHEMA_CHUNKS = "editorial-chunks/1"


def json_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")


def digest_json(value: Any) -> str:
    hasher = hashlib.sha256()
    encoder = json.JSONEncoder(ensure_ascii=False, sort_keys=True,
                               separators=(",", ":"))
    for chunk in encoder.iterencode(value):
        hasher.update(chunk.encode("utf-8"))
    return hasher.hexdigest()


def hash_file(path: str | Path, *, full_limit: int = 512 * 1024 * 1024) -> str:
    """Hash estable de un artefacto.

    Los JSON se hashean completos. Para medios mayores de 512 MiB se usa tamaño,
    mtime y tres bloques; evita releer varios GiB en cada reanudación sin confundir
    archivos pequeños modificados.
    """
    source = Path(path)
    stat = source.stat()
    hasher = hashlib.sha256()
    hasher.update(str(stat.st_size).encode("ascii"))
    if stat.st_size <= full_limit:
        with source.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                hasher.update(block)
        return hasher.hexdigest()

    hasher.update(str(stat.st_mtime_ns).encode("ascii"))
    block_size = 4 * 1024 * 1024
    with source.open("rb") as handle:
        for offset in (0, max(0, stat.st_size // 2 - block_size // 2),
                       max(0, stat.st_size - block_size)):
            handle.seek(offset)
            hasher.update(handle.read(block_size))
    return hasher.hexdigest()


def read_json(path: str | Path) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def atomic_write_text(path: str | Path, text: str) -> Path:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{destination.name}.", suffix=".tmp",
                                     dir=destination.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        for attempt in range(6):
            try:
                os.replace(temporary, destination)
                break
            except PermissionError:
                if attempt == 5:
                    raise
                time.sleep(.02 * 2 ** attempt)
    except Exception:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise
    return destination


def atomic_write_json(path: str | Path, value: Any, *, indent: int = 2) -> Path:
    return atomic_write_text(path, json.dumps(value, ensure_ascii=False, indent=indent) + "\n")


def finite_time(value: Any, *, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
        raise ValueError(f"{name} debe ser un número finito")
    return float(value)


def validate_range(start: Any, end: Any, duration: float, *, label: str) -> tuple[float, float]:
    start_f = finite_time(start, name=f"{label}.t_ini")
    end_f = finite_time(end, name=f"{label}.t_fin")
    if start_f < 0 or end_f <= start_f or end_f > duration + 0.01:
        raise ValueError(f"{label}: rango inválido {start_f:.3f}..{end_f:.3f} "
                         f"para duración {duration:.3f}")
    return start_f, min(end_f, duration)


def format_time(seconds: float) -> str:
    milliseconds = int(round(max(0.0, seconds) * 1000))
    hours, remainder = divmod(milliseconds, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    secs, millis = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}.{millis:03d}"


def parse_time(value: str | float | int) -> float:
    if isinstance(value, (float, int)):
        return float(value)
    text = value.strip()
    if not text:
        raise ValueError("timecode vacío")
    if ":" not in text:
        return float(text)
    parts = text.split(":")
    if len(parts) == 2:
        hours, minutes, seconds = 0, int(parts[0]), float(parts[1])
    elif len(parts) == 3:
        hours, minutes, seconds = int(parts[0]), int(parts[1]), float(parts[2])
    else:
        raise ValueError(f"timecode inválido: {value}")
    if hours < 0 or not 0 <= minutes < 60 or not 0 <= seconds < 60:
        raise ValueError(f"timecode inválido: {value}")
    return hours * 3600 + minutes * 60 + seconds


def slug(value: str, *, fallback: str = "proyecto") -> str:
    normalized = unicodedata.normalize("NFKD", value)
    ascii_text = "".join(char for char in normalized if not unicodedata.combining(char))
    clean = re.sub(r"[^A-Za-z0-9._-]+", "-", ascii_text).strip("-._").lower()
    return clean or fallback


def track_id(position: int) -> str:
    if position < 0:
        raise ValueError("position no puede ser negativo")
    result = ""
    number = position
    while True:
        number, remainder = divmod(number, 26)
        result = chr(65 + remainder) + result
        if number == 0:
            return result
        number -= 1


def normalized_tokens(text: str) -> list[str]:
    normalized = unicodedata.normalize("NFKD", (text or "").lower())
    normalized = "".join(char for char in normalized if not unicodedata.combining(char))
    return re.findall(r"[a-z0-9]{2,}", normalized)


def overlaps(start: float, end: float, event: dict) -> float:
    event_start = float(event.get("t_ini", event.get("start", 0.0)))
    event_end = float(event.get("t_fin", event.get("end", event_start)))
    return max(0.0, min(end, event_end) - max(start, event_start))


def slice_events(events: Iterable[dict], start: float, end: float) -> list[dict]:
    return [event for event in events if overlaps(start, end, event) > 0]
