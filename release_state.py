"""Estado transaccional del release activo y confirmación de arranque."""
from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


SCHEMA = 1


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def read_json(path: Path, default: Any = None) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return default


def write_json_atomic(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


def current_file(root: Path) -> Path:
    return root / "state" / "current.json"


def load_current(root: Path) -> dict:
    state = read_json(current_file(root), {})
    return state if isinstance(state, dict) and state.get("schema") == SCHEMA else {
        "schema": SCHEMA, "current": None, "previous": None, "pending": None,
    }


def activate(root: Path, version: str, runtime_id: str) -> dict:
    state = load_current(root)
    previous = state.get("current")
    nonce = os.urandom(16).hex()
    state.update({
        "schema": SCHEMA,
        "previous": previous if previous != version else state.get("previous"),
        "current": version,
        "runtime_id": runtime_id,
        "pending": {"version": version, "nonce": nonce, "activated_at": utc_now()},
    })
    write_json_atomic(current_file(root), state)
    return state

def health_file(root: Path, version: str) -> Path:
    return root / "state" / "health" / f"{version}.json"


def mark_healthy_from_environment() -> bool:
    root_value = os.environ.get("TRANSCRIPTOR_ROOT", "").strip()
    version = os.environ.get("TRANSCRIPTOR_RELEASE_VERSION", "").strip()
    nonce = os.environ.get("TRANSCRIPTOR_RELEASE_NONCE", "").strip()
    if not (root_value and version and nonce):
        return False
    root = Path(root_value).resolve()
    state = load_current(root)
    pending = state.get("pending") or {}
    if pending.get("version") != version or pending.get("nonce") != nonce:
        return False
    write_json_atomic(health_file(root, version), {
        "schema": SCHEMA, "version": version, "nonce": nonce, "healthy_at": utc_now(),
    })
    return True


def confirm_if_healthy(root: Path) -> bool:
    state = load_current(root)
    pending = state.get("pending") or {}
    version, nonce = pending.get("version"), pending.get("nonce")
    if not (version and nonce):
        return True
    health = read_json(health_file(root, version), {})
    if health.get("version") != version or health.get("nonce") != nonce:
        return False
    state["pending"] = None
    state["confirmed_at"] = utc_now()
    write_json_atomic(current_file(root), state)
    return True


def rollback(root: Path, failed_version: str, reason: str) -> dict:
    state = load_current(root)
    if state.get("current") != failed_version or not state.get("previous"):
        raise RuntimeError("no existe una release anterior válida para rollback")
    previous = state["previous"]
    previous_manifest = read_json(root / "releases" / previous / "release-manifest.json", {})
    runtime_id = (previous_manifest.get("runtime") or {}).get("id")
    if not runtime_id:
        raise RuntimeError("la release anterior no declara un runtime válido")
    state.update({
        "current": previous,
        "previous": failed_version,
        "runtime_id": runtime_id,
        "pending": None,
        "rollback": {"failed_version": failed_version, "reason": reason, "at": utc_now()},
    })
    write_json_atomic(current_file(root), state)
    return state
