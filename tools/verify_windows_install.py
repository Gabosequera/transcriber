#!/usr/bin/env python3
"""Verifica una instalación administrada de Transcriptor, incluida su interfaz."""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path


REQUIRED_IMPORTS = (
    "ctranslate2",
    "customtkinter",
    "faster_whisper",
    "intervaltree",
    "librosa",
    "mediapipe",
    "numpy",
    "onnx",
    "PIL",
    "psutil",
    "pydub",
    "pysentimiento",
    "requests",
    "silero_vad",
    "soundfile",
    "torch",
    "torchaudio",
    "transformers",
)


def verify_podcast_ui(window):
    """Ejercita el carril, aceptación habilitada y guardado sin inferencia de modelos."""
    import tempfile
    import time
    from automatico_ui import ChunkReviewDialog
    from editorial_io import atomic_write_json, read_json
    import editorial_chunks

    with tempfile.TemporaryDirectory(prefix="transcriptor-ui-") as temporary:
        root = Path(temporary)
        master = {"media": {"duration": 60.0}, "tracks": {},
                  "conversation": {"utterances": [], "clean_utterance_ids": []}}
        master_path = atomic_write_json(root / "test.editorial.master.json", master)
        plan = {"schema": "editorial-chunks/1", "planner": "installer-check",
                "chunks": [{"chunk_id": "chunk-001", "t_ini": 0.0, "t_fin": 60.0,
                            "title": "Prueba", "confidence": 0.8}]}
        editorial_chunks.apply_plan(root, master_path, plan, persist_selection=True)
        workspace = window.automatico
        workspace.result = {"master": str(master_path)}
        workspace._load_saved_plan()
        assert workspace.accept_button.cget("state") == "normal"
        workspace.editor.info = {"duracion": 60.0, "pistas": []}
        workspace.editor.view = [0.0, 60.0]
        workspace.editor.refrescar_layout()
        window.update()
        assert workspace.editor.tl.find_all()
        dialog = ChunkReviewDialog(workspace.f, master_path)
        workspace.review_dialog = dialog
        dialog.rows[0]["title"].delete(0, "end")
        dialog.rows[0]["title"].insert(0, "Título revisado")
        dialog._save()
        deadline = time.monotonic() + 10
        while dialog.winfo_exists() and time.monotonic() < deadline:
            window.update()
            time.sleep(0.02)
        assert not dialog.winfo_exists(), "El diálogo no terminó de guardar"
        assert read_json(root / "views/chunks.json")["chunks"][0]["title"] == "Título revisado"
        workspace.result = None
        workspace.plan = None


def load_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeError(f"no se pudo leer {path}") from error
    if not isinstance(value, dict):
        raise RuntimeError(f"{path} no contiene un objeto JSON")
    return value


def verify(root: Path) -> None:
    root = root.resolve()
    state = load_json(root / "state" / "current.json")
    version = str(state.get("current") or "")
    runtime_id = str(state.get("runtime_id") or "")
    if not version or not runtime_id:
        raise RuntimeError("la instalación no tiene una release activa")

    release = root / "releases" / version
    runtime = root / "runtimes" / runtime_id
    runtime_python = runtime / "Scripts" / "python.exe"
    manifest = load_json(release / "release-manifest.json")
    runtime_state = load_json(runtime / "runtime.json")
    if manifest.get("version") != version:
        raise RuntimeError("la versión activa no coincide con su manifiesto")
    if (manifest.get("runtime") or {}).get("id") != runtime_id:
        raise RuntimeError("el manifiesto apunta a otro runtime")
    if runtime_state.get("id") != runtime_id or not runtime_python.is_file():
        raise RuntimeError("el runtime activo está incompleto")

    ffmpeg = root / "shared" / "tools" / "ffmpeg" / "ffmpeg.exe"
    laughter = root / "shared" / "components" / "LaughterSegmentation" / "train" / "model.py"
    if not ffmpeg.is_file():
        raise RuntimeError("ffmpeg.exe no quedó instalado")
    if not laughter.is_file():
        raise RuntimeError("LaughterSegmentation no quedó instalado")

    environment = dict(os.environ)
    environment["TRANSCRIPTOR_ROOT"] = str(root)
    environment["TRANSCRIPTOR_RELEASE_VERSION"] = version
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    environment["PATH"] = str(ffmpeg.parent) + os.pathsep + environment.get("PATH", "")
    torch_contract = manifest.get("runtime", {}).get("torch", {})
    expected_torch = str(torch_contract.get("version") or "")
    expected_cuda = "12.8" if str(torch_contract.get("index_url") or "").endswith("/cu128") else ""
    if not expected_torch or not expected_cuda:
        raise RuntimeError("el contrato Torch/CUDA de la release no es válido")
    import_code = (
        "import importlib, sys; "
        f"sys.path.insert(0, {str(release)!r}); "
        f"mods={REQUIRED_IMPORTS!r}; "
        "[importlib.import_module(name) for name in mods]; "
        "import torch, torchaudio; "
        "assert hasattr(torchaudio.pipelines, 'MMS_FA'); "
        "torchaudio.functional.forced_align(torch.log_softmax(torch.randn(1, 5, 3), -1), torch.tensor([[1, 2]], dtype=torch.int32)); "
        f"assert torch.__version__.split('+')[0] == {expected_torch!r}, torch.__version__; "
        f"assert torch.version.cuda == {expected_cuda!r}, torch.version.cuda; "
        "import app, app_paths, core, hardware, updater; "
        "window=app.App(); "
        "window.update_idletasks(); "
        "window.update(); "
        f"sys.path.insert(0, {str(Path(__file__).resolve().parent)!r}); "
        "from verify_windows_install import verify_podcast_ui; "
        "verify_podcast_ui(window); "
        "window.automatico.cerrar(); "
        "window.destroy(); "
        f"updater.verify_installed_release(Path({str(release)!r}), {version!r}); "
        "print('Interfaz Windows OK')"
    )
    command = [
        str(runtime_python), "-B", "-W", "ignore::SyntaxWarning", "-c",
        "from pathlib import Path; " + import_code,
    ]
    subprocess.run(
        command,
        cwd=release,
        env=environment,
        check=True,
        timeout=180,
    )
    subprocess.run(
        [str(ffmpeg), "-version"],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.STDOUT,
        timeout=30,
    )
    print(f"Transcriptor {version} verificado ({runtime_id})")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    args = parser.parse_args()
    verify(args.root)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
