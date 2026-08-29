"""presets.py — guarda/lee/borra presets de configuración por sección
('transcribe' y 'gate') en un JSON junto a la app."""
from __future__ import annotations

import json
from pathlib import Path

import app_paths

PRESETS_FILE = app_paths.PRESETS_FILE


def load() -> dict:
    if PRESETS_FILE.exists():
        try:
            d = json.loads(PRESETS_FILE.read_text(encoding="utf-8"))
            if isinstance(d, dict):
                return d
        except Exception:
            pass
    return {"transcribe": {}, "gate": {}}


def _save(d: dict) -> None:
    # escritura ATÓMICA (tmp + os.replace): un crash a mitad no corrompe presets.json
    import os
    tmp = PRESETS_FILE.with_name(PRESETS_FILE.name + ".tmp")
    tmp.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, PRESETS_FILE)


def names(section: str) -> list[str]:
    return sorted(load().get(section, {}).keys())


def get(section: str, name: str) -> dict | None:
    return load().get(section, {}).get(name)


def put(section: str, name: str, config: dict) -> None:
    d = load()
    d.setdefault(section, {})[name] = config
    _save(d)


def delete(section: str, name: str) -> None:
    d = load()
    if name in d.get(section, {}):
        del d[section][name]
        _save(d)
