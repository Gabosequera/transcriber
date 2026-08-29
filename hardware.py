#!/usr/bin/env python3
"""
hardware.py — detección de hardware + configuración GLOBAL (CPU / GPU / hilos).

Un único lugar donde el resto de los módulos preguntan:
  · "¿cuántos hilos de CPU uso?"  → cpu_threads() / apply_torch_threads()
  · "¿corro en GPU o CPU?"        → torch_device() / whisper_auto_device()

La GUI (pestaña «Ajustes») lee la detección y escribe las preferencias, que se
guardan en `config.json` para que persistan entre ejecuciones.

Pensado para PORTAR a otra PC (Windows, otra GPU/CPU/RAM): la detección es 100 %
dinámica (no hay nada hardcodeado del equipo actual), y el usuario puede forzar
«usar todos los hilos» y «preferir GPU para todo lo posible». En la PC actual
(torch instalado en versión +cpu) la preferencia de GPU cae sola a CPU sin romper
nada; en una PC con torch+CUDA se activa la GPU en todos los módulos que la soporten.

NOTA: los módulos cachean sus modelos por proceso, así que un cambio de
dispositivo (GPU↔CPU) se aplica del todo al reiniciar la app. Los hilos de CPU sí
se re-aplican en cada operación.
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import app_paths

# Windows: torch, numpy/mkl y numba pueden traer CADA UNO su runtime de OpenMP (libiomp5md.dll).
# Al cargar el 2º, OpenMP aborta el proceso ("OMP: Error #15") SIN traceback = crash nativo (típico
# al usar torch por 1ª vez, p.ej. la alineación después de transcribir). Este flag lo tolera (workaround
# estándar y recomendado). Se setea ANTES de importar torch/numpy. Inofensivo en Linux.
os.environ.setdefault("KMP_DUPLICATE_LIB_OK", "TRUE")

_PROJ = app_paths.SOURCE_DIR
_CONFIG = app_paths.CONFIG_FILE


def _prepend_bundled_to_path() -> None:
    """Deja los binarios que se distribuyen JUNTO al proyecto (ffmpeg, llama.cpp, uv) al
    frente del PATH, para que librosa/pydub/audioread encuentren ffmpeg y describir encuentre
    llama sin que el usuario instale nada en el sistema. Clave en Windows, donde ffmpeg no
    viene de fábrica. Inofensivo en Linux (si las carpetas no existen, no hace nada)."""
    parts = os.environ.get("PATH", "").split(os.pathsep)
    for d in (app_paths.TOOLS_DIR, app_paths.TOOLS_DIR / "ffmpeg",
              app_paths.TOOLS_DIR / "ffmpeg" / "bin", app_paths.LLAMA_DIR):
        if d.is_dir() and str(d) not in parts:
            os.environ["PATH"] = str(d) + os.pathsep + os.environ.get("PATH", "")


_prepend_bundled_to_path()

_DEFAULTS = {
    "cpu_max": True,        # usar todos los hilos lógicos de la CPU
    "cpu_threads": 0,       # nº explícito de hilos si cpu_max=False (0 = todos)
    "gpu_prefer": True,     # preferir GPU para todo lo que se pueda (carril CUDA: whisper + torch)
    "llama_device": "auto",  # GPU del LALM (carril Vulkan): "auto" | "VulkanN" | "cpu"
    "gpu_torch_windows": False,  # experimental: forzar torch-GPU en Windows (choca cuDNN con Whisper)
    # keys de APIs online: viven SOLO acá (config.json, por-máquina) — nunca en
    # presets, spec, manifests ni en el zip de ship.
    "openrouter_api_key": "",   # visión VLM + descripción de audio multi-vendor
    "dashscope_api_key": "",    # Alibaba Model Studio: familia Qwen-Omni por API
}

_cache: dict | None = None


# ============================================================ detección CPU/RAM --
def cpu_logical() -> int:
    """Nº de hilos lógicos (con hyperthreading)."""
    return os.cpu_count() or 4


def cpu_physical() -> int:
    """Nº de núcleos físicos (sin contar hyperthreading). Cae a lógicos si no se sabe."""
    try:
        import psutil
        n = psutil.cpu_count(logical=False)
        return int(n) if n else cpu_logical()
    except Exception:
        return cpu_logical()


def cpu_name() -> str:
    """Nombre legible de la CPU (best-effort, multiplataforma)."""
    # Linux: /proc/cpuinfo
    try:
        for line in Path("/proc/cpuinfo").read_text().splitlines():
            if line.lower().startswith("model name"):
                return line.split(":", 1)[1].strip()
    except Exception:
        pass
    # Windows / otros: platform.processor()
    try:
        import platform
        p = platform.processor()
        if p:
            return p
    except Exception:
        pass
    return "CPU"


def ram_gb() -> tuple[float, float]:
    """(total, disponible) en GB. (0, 0) si psutil no está."""
    try:
        import psutil
        vm = psutil.virtual_memory()
        return round(vm.total / 2**30, 1), round(vm.available / 2**30, 1)
    except Exception:
        return 0.0, 0.0


# ================================================================= detección GPU --
def torch_cuda_available() -> bool:
    """¿La build de torch instalada ve una GPU CUDA? (torch+cpu → False)."""
    try:
        import torch
        return bool(torch.cuda.is_available())
    except Exception:
        return False


def ct2_cuda_count() -> int:
    """Nº de GPUs que CTranslate2 (faster-whisper) puede usar (0 = solo CPU)."""
    try:
        import ctranslate2
        return int(ctranslate2.get_cuda_device_count())
    except Exception:
        return 0


def _gpu_nvidia_smi() -> dict:
    """{name, vram_gb} SOLO vía nvidia-smi (barato, sin importar torch/ctranslate2 —
    apto para el arranque de la GUI)."""
    name, vram = None, 0.0
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,memory.total",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5,
            **({"creationflags": 0x08000000} if os.name == "nt" else {}))
        line = out.stdout.strip().splitlines()[0] if out.stdout.strip() else ""
        if line:
            parts = [p.strip() for p in line.split(",")]
            name = parts[0] or None
            if len(parts) > 1:
                try:
                    vram = round(float(parts[1]) / 1024, 1)   # MiB → GB
                except ValueError:
                    pass
    except Exception:
        pass
    return {"name": name, "vram_gb": vram}


def gpu_info() -> dict:
    """{name, vram_gb, torch_cuda, ct2_cuda}. name/vram vía nvidia-smi si está.
    CARO (importa torch y ctranslate2): no llamar en el camino de arranque de la GUI."""
    g = _gpu_nvidia_smi()
    return {**g, "torch_cuda": torch_cuda_available(), "ct2_cuda": ct2_cuda_count()}


def vram_free_gb() -> float | None:
    """VRAM libre de la GPU NVIDIA (GB) vía nvidia-smi, o None si no se sabe."""
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.free", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5)
        line = out.stdout.strip().splitlines()[0] if out.stdout.strip() else ""
        return round(float(line) / 1024, 1) if line else None   # MiB → GB
    except Exception:
        return None


def gpu_processes(max_n: int = 5) -> list[dict]:
    """Procesos que están usando la GPU NVIDIA ahora (best-effort vía nvidia-smi):
    [{'pid','name','mib'}] ordenado por memoria, para poder DECIRLE al usuario quién ocupa
    la VRAM (liberarla desde acá no se puede: eso lo decide el driver/el usuario).
    En Windows (WDDM) los procesos gráficos suelen reportar memoria 'N/A' → mib=0 pero el
    nombre igual sirve. Lista vacía si no hay nvidia-smi o no reporta nada."""
    procs: list[dict] = []
    try:                                   # 1) apps de cómputo (CUDA): dan memoria exacta
        r = subprocess.run(["nvidia-smi", "--query-compute-apps=pid,process_name,used_gpu_memory",
                            "--format=csv,noheader,nounits"],
                           capture_output=True, text=True, timeout=5)
        for line in r.stdout.strip().splitlines():
            p = [s.strip() for s in line.split(",")]
            if len(p) >= 3 and p[0].isdigit():
                try:
                    mib = int(p[2])
                except ValueError:
                    mib = 0
                procs.append({"pid": p[0], "name": Path(p[1]).name, "mib": mib})
    except Exception:
        pass
    if not procs:                          # 2) tabla general (incluye gráficos C+G en Windows)
        try:
            import re
            out = subprocess.run(["nvidia-smi"], capture_output=True, text=True, timeout=5).stdout
            for line in out.splitlines():
                m = re.search(r"\s(\d{2,7})\s+(?:C\+G|C|G)\s+(.+?)\s+(?:(\d+)MiB|N/A)\s*\|?\s*$",
                              line)
                if m:
                    procs.append({"pid": m.group(1), "name": Path(m.group(2).strip()).name,
                                  "mib": int(m.group(3)) if m.group(3) else 0})
        except Exception:
            pass
    return sorted(procs, key=lambda x: -x["mib"])[:max_n]


def any_gpu() -> bool:
    """¿Hay una GPU utilizable por ALGÚN backend (torch o ctranslate2)?"""
    return torch_cuda_available() or ct2_cuda_count() > 0


def _gpu_kind_from_name(name: str) -> str:
    """Clasifica integrada/dedicada/cpu por el NOMBRE (cuando no hay tipo autoritativo,
    p.ej. en Windows sin vulkaninfo). Heurística de nombres comunes de iGPU vs GPU discreta."""
    n = name.lower()
    if any(k in n for k in ("llvmpipe", "swiftshader", "software", "microsoft basic")):
        return "cpu"
    # discretas (chequear primero: "radeon rx"/"arc" son discretas aunque digan radeon/intel)
    if any(k in n for k in ("geforce", "rtx", "gtx", "quadro", "tesla", "titan",
                            "radeon rx", "radeon pro", "instinct", "intel arc", "arc a", "arc b")):
        return "dedicada"
    # integradas (iGPU): APUs AMD e Intel HD/UHD/Iris
    if any(k in n for k in ("radeon graphics", "radeon(tm) graphics", "vega", "renoir",
                            "cezanne", "raphael", "phoenix", "rembrandt", "lucienne",
                            "uhd graphics", "hd graphics", "iris", "intel(r) graphics")):
        return "integrada"
    return "?"


def _mark_cuda(gpus: list[dict]) -> list[dict]:
    """Marca cuda=True en las NVIDIA dedicadas si algún backend CUDA ve una GPU."""
    has_cuda = ct2_cuda_count() > 0 or torch_cuda_available()
    for g in gpus:
        nv = (g.get("vendor") == "0x10de") or ("nvidia" in g.get("name", "").lower())
        if nv and g.get("kind") == "dedicada" and has_cuda:
            g["cuda"] = True
    return gpus


def _list_gpus_vulkaninfo() -> list[dict]:
    """Enumera GPUs por `vulkaninfo --summary` (Linux, o Windows con Vulkan SDK). Tipo
    autoritativo (integrada/dedicada). Lista vacía si no está vulkaninfo."""
    import re
    try:
        out = subprocess.run(["vulkaninfo", "--summary"],
                             capture_output=True, text=True, timeout=8).stdout
    except Exception:
        return []
    KIND = {"INTEGRATED": "integrada", "DISCRETE": "dedicada",
            "VIRTUAL": "virtual", "CPU": "cpu"}
    gpus, cur = [], None
    for line in out.splitlines():
        h = re.match(r"\s*GPU(\d+):", line)
        if h:
            cur = {"index": int(h.group(1)), "name": "", "kind": "?",
                   "vendor": None, "cuda": False}
            gpus.append(cur)
            continue
        if cur is None or "=" not in line:
            continue
        key, val = (s.strip() for s in line.split("=", 1))
        if key == "deviceName":
            cur["name"] = val
        elif key == "deviceType":
            cur["kind"] = next((v for k, v in KIND.items() if k in val), "?")
        elif key == "vendorID":
            cur["vendor"] = val
    return gpus


def _list_gpus_llama() -> list[dict]:
    """Respaldo (Windows sin vulkaninfo): usa el `--list-devices` de llama.cpp — que viene
    con el build Vulkan empacado — para nombres + VRAM. Clasifica el tipo por el nombre.
    El índice VulkanN coincide con el que usa describir.py para elegir el device."""
    try:
        import describir
        devs = describir.list_devices()
    except Exception:
        return []
    gpus = []
    for d in devs:
        m = __import__("re").match(r"Vulkan(\d+)", d.get("id", ""))
        gpus.append({"index": int(m.group(1)) if m else len(gpus),
                     "name": d.get("name", ""), "kind": _gpu_kind_from_name(d.get("name", "")),
                     "vendor": None, "cuda": False,
                     "vram_gb": round(d.get("total_mib", 0) / 1024, 1)})
    return gpus


def list_gpus() -> list[dict]:
    """Enumera TODAS las GPUs del equipo (integrada + dedicada), MULTIPLATAFORMA. Cada item:
        {index, name, kind ('integrada'|'dedicada'|'cpu'|'virtual'|'?'), vendor, cuda, [vram_gb]}
    `index` = índice Vulkan → coincide con el `VulkanN` de llama.cpp (describir.py), así que
    sirve para SELECCIONAR el device del LALM.

    Cadena de respaldo (para que la iGPU se vea en cualquier SO):
      1. vulkaninfo --summary  (Linux, o Windows con Vulkan SDK) → tipo autoritativo.
      2. llama.cpp --list-devices (build Vulkan empacado) → nombres + VRAM, tipo por nombre.
      3. nvidia-smi → al menos la NVIDIA dedicada.
    Detecta la iGPU (kind='integrada'), usable por Vulkan (encoder del modelo de descripción).
    Para el carril CUDA (whisper + torch) solo sirve la NVIDIA dedicada (cuda=True)."""
    gpus = _list_gpus_vulkaninfo()
    if not gpus:
        gpus = _list_gpus_llama()
    if not gpus:
        g = gpu_info()      # último respaldo: solo la dedicada NVIDIA
        if g["name"]:
            gpus = [{"index": 0, "name": g["name"], "kind": "dedicada", "vendor": "0x10de",
                     "cuda": False, "vram_gb": g["vram_gb"]}]
    return _mark_cuda(gpus)


# ========================================================= settings persistentes --
def load() -> dict:
    """Config global (cacheada). Fusiona defaults + config.json."""
    global _cache
    if _cache is None:
        c = dict(_DEFAULTS)
        try:
            c.update(json.loads(_CONFIG.read_text(encoding="utf-8")))
        except Exception:
            pass
        _cache = c
    return _cache


def save(cfg: dict) -> None:
    """Persiste la config (solo las claves conocidas) y actualiza la cache."""
    global _cache
    c = dict(_DEFAULTS)
    c.update({k: cfg[k] for k in _DEFAULTS if k in cfg})
    _cache = c
    try:
        _CONFIG.write_text(json.dumps(c, indent=2), encoding="utf-8")
    except Exception:
        pass


def set_(**kw) -> None:
    c = dict(load())
    c.update(kw)
    save(c)


# =============================================================== resolución uso --
def cpu_threads() -> int:
    """Hilos de CPU a usar según la config (default: todos los lógicos)."""
    c = load()
    if c.get("cpu_max", True):
        return cpu_logical()
    n = int(c.get("cpu_threads") or 0)
    return max(1, min(n, cpu_logical())) if n > 0 else cpu_logical()


def apply_torch_threads() -> int:
    """Aplica cpu_threads() a torch (intra-op). Devuelve el nº usado. Silencioso si
    torch no está. `set_num_interop_threads` NO se toca (tira si torch ya paralelizó)."""
    n = cpu_threads()
    try:
        import torch
        torch.set_num_threads(n)
    except Exception:
        pass
    # también para libs que leen estas envs (numexpr/OpenMP): asignar SIEMPRE (no setdefault),
    # para que un cambio de hilos en la GUI se refleje y no quede pegado el valor inicial.
    os.environ["OMP_NUM_THREADS"] = str(n)
    return n


def gpu_prefer() -> bool:
    return bool(load().get("gpu_prefer", True))


def use_gpu_torch() -> bool:
    """¿Usar GPU en los módulos torch (align/metadata/risa/escenas/respiros)?
    = el usuario la prefiere Y torch la ve.

    EXCEPCIÓN Windows: torch trae su PROPIO cuDNN y CTranslate2 (Whisper) usa el cuDNN de pip
    (nvidia-cudnn-cu12) que core._preload_cuda_libs deja en el PATH. Al correr torch-GPU DESPUÉS de
    Whisper, torch carga el cuDNN equivocado → crash nativo ("Could not load symbol cudnnGetLibConfig,
    error 127"). Hasta resolver ese conflicto de DLLs, en Windows los modelos torch corren en CPU
    (Whisper sigue en GPU vía CTranslate2 — su carril es independiente). Es el MISMO comportamiento que
    la laptop Linux de dev (torch era +cpu) → misma precisión. Se puede forzar GPU (experimental) con
    config `gpu_torch_windows=True`."""
    if not (gpu_prefer() and torch_cuda_available()):
        return False
    if os.name == "nt" and not load().get("gpu_torch_windows", False):
        return False
    return True


def torch_device() -> str:
    """'cuda' o 'cpu' para los módulos que corren con torch (align/metadata/…)."""
    return "cuda" if use_gpu_torch() else "cpu"


def whisper_auto_device() -> str:
    """Device para faster-whisper cuando se pide 'auto': 'cuda' si el usuario prefiere
    GPU y CTranslate2 la ve, si no 'cpu'."""
    return "cuda" if (gpu_prefer() and ct2_cuda_count() > 0) else "cpu"


def llama_device() -> str:
    """Selección del usuario para la GPU del LALM (carril Vulkan de describir.py):
    'auto' (dedicada si el modelo entra entero, si no encoder en la de más memoria libre —
    ver describir._gpu_plan), un id 'VulkanN', o 'cpu'."""
    return str(load().get("llama_device", "auto") or "auto")


# ==================================================== auto-modelo por hardware --
def recommend_whisper_model() -> str:
    """Modelo de Whisper por defecto adecuado al hardware detectado, para NO reventar por
    memoria en equipos modestos. En GPU manda la VRAM; en CPU, la RAM (y la velocidad).
    El usuario siempre puede cambiarlo en la GUI; esto es solo el default sensato.

    OJO ARRANQUE: esto corre al CONSTRUIR la GUI (3 combos) — acá la GPU se sondea
    solo con nvidia-smi (name/vram). ct2_cuda_count() importaba ctranslate2→torch,
    ~3 s de arranque (medido 2026-07-20); el device real se resuelve al transcribir
    (resolve_device → whisper_auto_device), que sí hace el chequeo caro."""
    g = _gpu_nvidia_smi()
    if gpu_prefer() and g["name"]:
        vram = g["vram_gb"] or 0
        if vram >= 8:
            return "large-v3"
        if vram >= 4:
            return "medium"        # cabe con int8 en 4 GB (el sweet spot medido)
        if vram >= 2.5:
            return "small"
        return "base"
    total, _ = ram_gb()            # CPU: large es lentísimo → limitar por RAM
    if total >= 16:
        return "medium"
    if total >= 8:
        return "small"
    return "base"


# ====================================================== fallback de dispositivo --
def is_cuda_oom(err: BaseException) -> bool:
    """Heurística: ¿el error es por falta de memoria/GPU CUDA (recuperable cayendo a CPU)?"""
    m = str(err).lower()
    return any(k in m for k in (
        "out of memory", "cuda error", "cublas", "cudnn", "cufft", "device-side",
        "cuda out", "no kernel image", "cuda driver", "invalid device"))


def load_model_safe(build, *, log=None):
    """Carga un modelo torch con fallback GPU→CPU. `build(device: str)` debe construir y
    devolver el modelo ya movido a ese device. Intenta el device preferido (torch_device());
    si es 'cuda' y falla por OOM/error CUDA, libera y reintenta en 'cpu'. Devuelve
    (modelo, device_efectivo). Así una laptop con poca VRAM no crashea la feature: degrada a CPU."""
    dev = torch_device()
    try:
        return build(dev), dev
    except Exception as e:                       # noqa: BLE001
        if dev == "cuda" and is_cuda_oom(e):
            if log:
                log(f"⚠ GPU sin memoria al cargar el modelo → usando CPU. ({type(e).__name__})")
            try:
                import torch
                torch.cuda.empty_cache()
            except Exception:
                pass
            return build("cpu"), "cpu"
        raise


# ============================================================== resumen legible --
def summary() -> str:
    """Línea corta para logs / cabeceras."""
    tot, _ = ram_gb()
    g = gpu_info()
    gpu = g["name"] or "sin GPU"
    return (f"CPU {cpu_physical()}c/{cpu_logical()}h · RAM {tot:g} GB · "
            f"GPU {gpu} · usando {cpu_threads()} hilos · device={torch_device()}")


if __name__ == "__main__":
    import pprint
    print(summary())
    pprint.pprint({"cpu_name": cpu_name(), "gpu": gpu_info(),
                   "ram": ram_gb(), "config": load(),
                   "cpu_threads": cpu_threads(), "torch_device": torch_device(),
                   "whisper_auto": whisper_auto_device()})
