"""Run Torch inference in a fresh process, away from Whisper's cuDNN DLLs."""
from __future__ import annotations

import functools
import importlib
import inspect
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

OPERATIONS = {"align.align_words", "diarization.detect", "prosodia.extract_arousal", "laughter.detect"}
DEVICE_ENV = "TRANSCRIPTOR_TORCH_WORKER_DEVICE"


def child_environment(device):
    env = os.environ.copy()
    # core adds pip NVIDIA DLL directories for CTranslate2. Torch must use its own.
    env["PATH"] = os.pathsep.join(p for p in env.get("PATH", "").split(os.pathsep)
                                if "/nvidia/" not in p.replace("\\", "/").lower())
    env[DEVICE_ENV] = device
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def should_isolate():
    import hardware
    return (os.name == "nt" and DEVICE_ENV not in os.environ and hardware.gpu_prefer()
            and bool(hardware._gpu_nvidia_smi().get("name")))


def isolated(function):
    @functools.wraps(function)
    def wrapped(*args, **kwargs):
        if not should_isolate():
            return function(*args, **kwargs)
        bound = inspect.signature(function).bind(*args, **kwargs)
        params = dict(bound.arguments)
        callbacks = {key: params.pop(key, None) for key in ("log_cb", "progress_cb", "cancel")}
        return run(f"{function.__module__}.{function.__name__}", params, **callbacks)
    return wrapped


def run(operation, params, *, log_cb=None, progress_cb=None, cancel=None):
    if operation not in OPERATIONS and operation != "probe":
        raise ValueError("Operación Torch no permitida")
    with tempfile.TemporaryDirectory(prefix="transcriptor-torch-") as directory:
        directory = Path(directory)
        request, result, events = (directory / name for name in ("request.json", "result.json", "events.jsonl"))
        request.write_text(json.dumps({"operation": operation, "params": params}, default=str), encoding="utf-8")
        for device in ("cuda", "cpu"):
            if cancel is not None and cancel.is_set():
                raise InterruptedError("inferencia cancelada")
            result.unlink(missing_ok=True)
            events.write_text("", encoding="utf-8")
            if log_cb:
                log_cb(f"{operation}: {device.upper()} en proceso aislado")
            with (directory / "diagnostics.log").open("w", encoding="utf-8") as diagnostics:
                process = subprocess.Popen(
                    [sys.executable, str(Path(__file__).resolve()), str(request), str(result), str(events)],
                    env=child_environment(device), stdout=diagnostics, stderr=diagnostics,
                    **({"creationflags": 0x08000000} if os.name == "nt" else {}))
                try:
                    with events.open(encoding="utf-8") as stream:
                        while True:
                            if cancel is not None and cancel.is_set():
                                raise InterruptedError("inferencia cancelada")
                            offset = stream.tell()
                            line = stream.readline()
                            if line and line.endswith("\n"):
                                event = json.loads(line)
                                if event["kind"] == "log" and log_cb:
                                    log_cb(*event["args"])
                                elif event["kind"] == "progress" and progress_cb:
                                    progress_cb(*event["args"])
                            else:
                                stream.seek(offset)
                                if process.poll() is not None:
                                    break
                                time.sleep(0.1)
                    process.wait()
                finally:
                    if process.poll() is None:
                        process.terminate()
                        process.wait()
            output = json.loads(result.read_text(encoding="utf-8")) if result.exists() else {}
            if process.returncode == 0 and output.get("ok"):
                return output["value"]
            if device == "cuda" and (output.get("cuda_error") or not output):
                if log_cb:
                    log_cb("CUDA no pudo completar la etapa (memoria o runtime); reintentando en CPU en un proceso nuevo.")
                continue
            # No exception text: model libraries may include credentials in download errors.
            raise RuntimeError(f"Falló {operation} en {device.upper()} ({output.get('error_type', 'proceso terminado')}).")


def main(request, result, events):
    import app_paths  # configure persistent caches before importing Torch
    import hardware
    def emit(kind, *args):
        with Path(events).open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"kind": kind, "args": args}, ensure_ascii=False) + "\n")
    try:
        import torch  # first CUDA library loaded in this process
        device = os.environ[DEVICE_ENV]
        if device == "cuda" and not torch.cuda.is_available():
            raise RuntimeError("CUDA driver unavailable")
        payload = json.loads(Path(request).read_text(encoding="utf-8"))
        operation = payload["operation"]
        if operation == "probe":
            x = torch.randn(1, 1, 256, device=device)
            y = torch.nn.functional.conv1d(x, torch.ones(1, 1, 3, device=device))
            value = {"device": device, "finite": bool(torch.isfinite(y).all().item()),
                     "cudnn": torch.backends.cudnn.version()}
        elif operation in OPERATIONS:
            module, name = operation.split(".")
            params = payload["params"]
            # Conservative batches fit smaller laptop GPUs; CPU keeps existing defaults.
            if device == "cuda" and module in ("prosodia", "laughter"):
                params["batch_size"] = min(int(params.get("batch_size", 1)), 1)
            params.update(log_cb=lambda *a: emit("log", *a), progress_cb=lambda *a: emit("progress", *a))
            value = getattr(importlib.import_module(module), name)(**params)
        else:
            raise ValueError("Operación no permitida")
        output = {"ok": True, "value": value}
        code = 0
    except Exception as error:
        output = {"ok": False, "cuda_error": hardware.is_cuda_oom(error), "error_type": type(error).__name__}
        code = 1
    Path(result).write_text(json.dumps(output, ensure_ascii=False), encoding="utf-8")
    return code


if __name__ == "__main__":
    raise SystemExit(main(*sys.argv[1:]))
