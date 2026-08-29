"""Launcher de una release: actualiza, valida el arranque y aplica rollback."""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import release_state
import updater


SOURCE_DIR = Path(__file__).resolve().parent


def _write_status(root: Path, *, state: str, message: str, version: str | None = None) -> None:
    release_state.write_json_atomic(root / "state" / "update-status.json", {
        "schema": 1, "state": state, "message": message, "version": version,
        "updated_at": release_state.utc_now(),
    })


class _UpdateWindow:
    """Ventana mínima sólo cuando existe una descarga; no retrasa el arranque normal."""
    def __init__(self, version: str):
        self.root = self.label = self.bar = None
        if os.name != "nt" or os.environ.get("TRANSCRIPTOR_DIAGNOSTIC"):
            return
        try:
            import tkinter as tk
            from tkinter import ttk
            root = tk.Tk()
            root.title("Actualizando Transcriptor")
            root.geometry("420x130")
            root.resizable(False, False)
            tk.Label(root, text=f"Instalando Transcriptor {version}",
                     font=("Segoe UI", 12, "bold")).pack(pady=(18, 8))
            self.label = tk.Label(root, text="Preparando actualización…", font=("Segoe UI", 9))
            self.label.pack()
            self.bar = ttk.Progressbar(root, length=350, mode="indeterminate")
            self.bar.pack(pady=12)
            self.bar.start(12)
            root.update()
            self.root = root
        except Exception:
            self.root = self.label = self.bar = None

    def progress(self, message: str, done: int | None, total: int | None) -> None:
        if os.environ.get("TRANSCRIPTOR_DIAGNOSTIC"):
            suffix = f" ({done}/{total})" if done is not None and total else ""
            print(f"[actualización] {message}{suffix}", flush=True)
        if not self.root:
            return
        try:
            self.label.configure(text=message)
            if done is not None and total:
                self.bar.stop()
                self.bar.configure(mode="determinate", maximum=total, value=done)
            else:
                self.bar.configure(mode="indeterminate")
                self.bar.start(12)
            self.root.update()
        except Exception:
            pass

    def close(self) -> None:
        if self.root:
            try:
                self.root.destroy()
            except Exception:
                pass


def _current_version(root: Path) -> str:
    state = release_state.load_current(root)
    version = state.get("current")
    if version:
        return updater.normalized_version(str(version))
    return updater.normalized_version((SOURCE_DIR / "VERSION").read_text(encoding="utf-8").strip())


def auto_update(root: Path) -> None:
    config = updater.load_update_config(root, SOURCE_DIR)
    repository = config["repository"]
    if not (config["auto_update"] and repository):
        message = "Repositorio GitHub pendiente de configurar" if not repository else "Actualización automática desactivada"
        _write_status(root, state="not_configured", message=message)
        return
    try:
        client = updater.GitHubClient(repository, timeout=config["check_timeout_seconds"])
        candidate = updater.check_for_update(client, _current_version(root))
        if candidate is None:
            _write_status(root, state="current", message="La aplicación está actualizada",
                          version=_current_version(root))
            return
        window = _UpdateWindow(candidate.version)
        try:
            updater.download_and_install(root, client, candidate, progress=window.progress)
        finally:
            window.close()
        _write_status(root, state="installed", message="Actualización instalada",
                      version=candidate.version)
    except updater.UpdateError as error:
        _write_status(root, state="error", message=str(error), version=_current_version(root))
        if os.environ.get("TRANSCRIPTOR_DIAGNOSTIC"):
            print(f"[actualización] {error}; se conserva la versión instalada", file=sys.stderr)


def _release_command(root: Path, state: dict) -> tuple[list[str], dict[str, str]]:
    version = updater.normalized_version(str(state.get("current") or ""))
    release_dir = root / "releases" / version
    manifest = updater.verify_installed_release(release_dir, version)
    runtime_id = (manifest.get("runtime") or {}).get("id")
    if runtime_id != state.get("runtime_id"):
        raise updater.UpdateError("el runtime activo no coincide con la release")
    runtime = root / "runtimes" / runtime_id
    python = runtime / ("Scripts/pythonw.exe" if os.name == "nt" else "bin/python")
    if os.environ.get("TRANSCRIPTOR_DIAGNOSTIC"):
        python = runtime / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    app = release_dir / "app.py"
    if not python.is_file() or not app.is_file():
        raise updater.UpdateError("la release activa o su runtime están incompletos")
    env = dict(os.environ)
    env.update({
        "TRANSCRIPTOR_ROOT": str(root),
        "TRANSCRIPTOR_SHARED_DIR": str(root / "shared"),
        "TRANSCRIPTOR_RELEASE_VERSION": version,
        "PYTHONUTF8": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
    })
    pending = state.get("pending") or {}
    if pending.get("version") == version:
        env["TRANSCRIPTOR_RELEASE_NONCE"] = str(pending.get("nonce") or "")
    return [str(python), str(app)], env


def _spawn(root: Path, state: dict) -> subprocess.Popen:
    command, env = _release_command(root, state)
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" and hasattr(subprocess, "CREATE_NO_WINDOW") else 0
    return subprocess.Popen(command, cwd=root / "releases" / state["current"], env=env,
                            creationflags=flags)


def launch_active(root: Path) -> int:
    state = release_state.load_current(root)
    if not state.get("current"):
        raise updater.UpdateError("no hay una release instalada")
    pending = state.get("pending") or {}
    if not pending:
        try:
            process = _spawn(root, state)
        except updater.UpdateError as error:
            if not state.get("previous"):
                raise
            state = release_state.rollback(root, str(state["current"]), str(error))
            _write_status(root, state="rolled_back", message=str(error), version=state["current"])
            process = _spawn(root, state)
        if os.environ.get("TRANSCRIPTOR_DIAGNOSTIC"):
            return process.wait()
        return 0

    version = str(state["current"])
    release_state.health_file(root, version).unlink(missing_ok=True)
    process = _spawn(root, state)
    deadline = time.monotonic() + 45.0
    while time.monotonic() < deadline:
        if release_state.confirm_if_healthy(root):
            return process.wait() if os.environ.get("TRANSCRIPTOR_DIAGNOSTIC") else 0
        code = process.poll()
        if code is not None:
            reason = f"la aplicación terminó antes de confirmar el arranque (código {code})"
            break
        time.sleep(0.2)
    else:
        reason = "la aplicación no confirmó un arranque saludable en 45 segundos"
        process.terminate()

    if not state.get("previous"):
        raise updater.UpdateError(reason)
    rollback_state = release_state.rollback(root, version, reason)
    _write_status(root, state="rolled_back", message=reason, version=rollback_state["current"])
    fallback = _spawn(root, rollback_state)
    return fallback.wait() if os.environ.get("TRANSCRIPTOR_DIAGNOSTIC") else 0


def main() -> int:
    root_value = os.environ.get("TRANSCRIPTOR_ROOT", "").strip()
    if not root_value:
        raise updater.UpdateError("launcher.py requiere TRANSCRIPTOR_ROOT")
    root = Path(root_value).resolve()
    auto_update(root)
    return launch_active(root)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except updater.UpdateError as error:
        print(f"No se pudo iniciar Transcriptor: {error}", file=sys.stderr)
        raise SystemExit(2)
