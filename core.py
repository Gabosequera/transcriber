"""
core.py — Transcripción word-level con faster-whisper. Sin GUI: solo la lógica.

La app (app.py) importa esto y llama a `transcribe(...)`. También se puede usar suelto
desde la consola. Genera:
  <nombre>.words.json     → cada palabra con start/end (la fuente de verdad para sync)
  <nombre>.segments.json  → frases con timestamps
  <nombre>.srt            → subtítulos
  <nombre>.cues.md        → guion legible para anotar dónde entra cada gráfica
"""
from __future__ import annotations

import ctypes
import glob
import json
import os
import site
import subprocess
import time
from pathlib import Path

import align
import hardware

MODELS = ["tiny", "base", "small", "medium", "large-v3", "large-v3-turbo"]
AUDIO_EXTS = [".wav", ".mp3", ".flac", ".m4a", ".ogg", ".opus", ".aac", ".wma", ".mp4", ".mov"]


# ------------------------------------------------------------------ CUDA libs --
def _preload_cuda_libs() -> None:
    """
    Deja las libs CUDA de los paquetes pip (nvidia-cublas / nvidia-cudnn) accesibles
    para CTranslate2, aunque no se haya preparado el entorno. MULTIPLATAFORMA:
      · Linux:   libs `.so` en `nvidia/*/lib`  → LD_LIBRARY_PATH + preload con ctypes.
      · Windows: DLLs `.dll` en `nvidia/*/bin` → PATH + os.add_dll_directory().
    Silencioso si no hay GPU / no están las libs.
    """
    is_win = os.name == "nt"
    subdir = "bin" if is_win else "lib"       # layout de las wheels según SO
    pattern = "*.dll" if is_win else "*.so*"

    dirs: list[str] = []
    bases = list(site.getsitepackages())
    try:
        bases.append(site.getusersitepackages())
    except Exception:
        pass
    for base in bases:
        nv = Path(base) / "nvidia"
        if not nv.is_dir():
            continue
        # descubrir TODOS los paquetes nvidia/* (cublas, cudnn, cuda_runtime, cufft…), no solo
        # cublas/cudnn: más robusto si CTranslate2 necesita otra lib o cambia el layout de las wheels.
        # Ordenado alfabético → cublas antes que cudnn (orden de dependencias en el preload de Linux).
        for pkgdir in sorted(nv.iterdir()):
            d = pkgdir / subdir
            if d.is_dir():
                dirs.append(str(d))
    if not dirs:
        return

    if is_win:
        # Windows: basta con dejar las carpetas en la ruta de búsqueda de DLLs. No se
        # hace el preload con ctypes/RTLD_GLOBAL (es de Linux) ni existe el problema de
        # NVBLAS que había allá.
        os.environ["PATH"] = os.pathsep.join(dirs + [os.environ.get("PATH", "")])
        for d in dirs:
            try:
                os.add_dll_directory(d)   # py3.8+: necesario para que CTranslate2 las encuentre
            except (OSError, AttributeError):
                pass
        return

    # Linux: LD_LIBRARY_PATH + preload con ctypes (cublas primero, luego cudnn)
    os.environ["LD_LIBRARY_PATH"] = os.pathsep.join(dirs + [os.environ.get("LD_LIBRARY_PATH", "")])
    for d in dirs:
        for so in sorted(glob.glob(os.path.join(d, pattern))):
            # NUNCA precargar NVBLAS con RTLD_GLOBAL: es un shim que intercepta el BLAS
            # de CPU (sgemm…) para reenviarlo a la GPU. Con sus símbolos en el namespace
            # global, torch-CPU (el VAD Silero) resuelve su BLAS a NVBLAS, que sin un
            # "CPU Blas library" configurado segfaultea el proceso entero (SIGSEGV, exit
            # 139) sin traceback → la app se cerraba sola. CTranslate2 no usa NVBLAS.
            if "nvblas" in os.path.basename(so).lower():
                continue
            try:
                ctypes.CDLL(so, mode=ctypes.RTLD_GLOBAL)
            except OSError:
                pass


_preload_cuda_libs()


# -------------------------------------------------------------- device probing --
def cuda_device_count() -> int:
    """Nº de GPUs que CTranslate2 puede usar (0 = solo CPU)."""
    try:
        import ctranslate2
        return int(ctranslate2.get_cuda_device_count())
    except Exception:
        return 0


def gpu_name() -> str | None:
    """Nombre de la GPU vía nvidia-smi, o None. Es también la detección BARATA de GPU
    para la GUI (arranque): importar ctranslate2 solo para contar GPUs arrastra torch
    (~3 s medidos 2026-07-20). Si nvidia-smi ve una GPU se OFRECE el modo GPU; el
    chequeo real (cuda_device_count) corre recién al transcribir en resolve_device,
    que cae a CPU si CTranslate2 no puede usarla."""
    flags = {"creationflags": 0x08000000} if os.name == "nt" else {}   # sin flash de consola
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"],
            capture_output=True, text=True, timeout=5, **flags,
        )
        name = out.stdout.strip().splitlines()[0].strip() if out.stdout.strip() else ""
        return name or None
    except Exception:
        return None


def resolve_device(device: str) -> str:
    """'auto' → según la config global (preferir GPU) y disponibilidad. Devuelve el
    device efectivo ('cuda' o 'cpu')."""
    if device == "auto":
        return hardware.whisper_auto_device()
    if device == "cuda" and cuda_device_count() == 0:
        return "cpu"
    return device


# ------------------------------------------------------------------ formatting --
def fmt_srt(t: float) -> str:
    h = int(t // 3600); m = int((t % 3600) // 60); s = int(t % 60)
    ms = int(round((t - int(t)) * 1000))
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def fmt_cue(t: float) -> str:
    return f"{int(t // 60):02d}:{t % 60:04.1f}"


# ------------------------------------------------------------------ alignment --
def align_transcription(audio, words: list[dict], segments: list[dict], *, required: bool = False,
                        log_cb=None, progress_cb=None, cancel=None) -> bool:
    """Corrige IN PLACE los start/end de `words` (y de los words/bordes de `segments`) con la
    alineación forzada MMS. Sirve tanto recién transcrito como sobre un words.json/segments.json
    cargados de disco (reanudar sin repetir Whisper). Devuelve True si se aplicó.
    `required=True` → cualquier fallo es error (perfil editorial); si no, se conservan los
    tiempos de Whisper y se avisa por log."""
    def log(msg):
        if log_cb:
            log_cb(msg)

    if not align.available():
        if required:
            raise RuntimeError("MMS es obligatorio para el perfil editorial, pero torchaudio no está disponible.")
        log("⚠ MMS no está disponible; se conservan los timestamps de Whisper.")
        return False
    log("Corrigiendo timestamps con alineación forzada (MMS)… (más lento, la 1ª vez descarga el modelo)")
    if progress_cb:
        progress_cb(0.0, None)
    try:
        aligned = align.align_words(audio, words, log_cb=log, progress_cb=progress_cb, cancel=cancel)
        fallback = sum(word.get("alignment_source") == "whisper_fallback" for word in aligned)
        covered = sum(str(word.get("alignment_source", "")).startswith("mms") for word in aligned)
        coverage = covered / max(1, len(aligned))
        if required and fallback:
            raise RuntimeError(f"MMS dejó {fallback} palabra(s) en fallback de Whisper")
        for orig, new in zip(words, aligned):
            orig["start"], orig["end"] = new["start"], new["end"]
            orig["alignment_source"] = new.get("alignment_source")
        # Recién transcrito, los dicts de cada segmento SON los de `words` (ya corregidos). Cargados
        # de JSON son copias → se propagan por orden (misma secuencia de palabras en ambas salidas).
        seg_words = [w for seg in segments for w in (seg.get("words") or [])]
        if seg_words and words and seg_words[0] is not words[0] and len(seg_words) == len(words):
            for copy, src in zip(seg_words, words):
                copy["start"], copy["end"] = src["start"], src["end"]
                copy["alignment_source"] = src.get("alignment_source")
        for seg in segments:
            sw = seg.get("words") or []
            if sw:
                seg["start"] = sw[0]["start"]
                seg["end"] = sw[-1]["end"]
        log(f"Timestamps corregidos (MMS {coverage:.1%}; aplicados a todas las salidas).")
        return True
    except Exception as e:
        if isinstance(e, InterruptedError):
            raise
        if required:
            raise RuntimeError(f"MMS es obligatorio para el perfil editorial y falló: {e}") from e
        log(f"⚠ La alineación falló, se usan los timestamps de whisper: {e}")
        return False


# ------------------------------------------------------------------ transcribe --
def transcribe(
    audio, outdir, *,
    model_name: str = "medium",
    lang: str = "es",
    device: str = "auto",
    want_segments: bool = True,
    want_srt: bool = True,
    want_cues: bool = True,
    want_align: bool = False,   # corregir timestamps con alineación forzada (MMS)
    align_required: bool = False,
    output_stem: str | None = None,
    progress_cb=None,   # progress_cb(fraccion: float 0..1, eta_seg: float|None)
    log_cb=None,        # log_cb(texto: str)
    cancel=None,        # threading.Event: si .is_set() → aborta limpio
) -> dict | None:
    """Transcribe `audio` y escribe las salidas en `outdir`. Devuelve un dict con
    los archivos escritos y metadatos, o None si se canceló."""
    audio = Path(audio)
    outdir = Path(outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    stem = audio.stem if output_stem is None else output_stem.strip()

    def log(msg):
        if log_cb:
            log_cb(msg)

    dev = resolve_device(device)

    def _is_gpu_oom(err) -> bool:
        m = str(err).lower()
        return any(k in m for k in ("out of memory", "cuda", "cudnn", "cublas", "cufft", "oom"))

    def _is_download_error(err) -> bool:
        m = str(err).lower()
        return any(k in m for k in ("connection", "timed out", "timeout", "network", "max retries",
                                    "temporarily", "name or service", "getaddrinfo", "ssl",
                                    "failed to download", "couldn't connect", "connectionerror"))

    def _attempt(use_dev: str):
        """Carga + transcribe en `use_dev`. Devuelve la tupla de resultados, o
        el string 'cancel' si el usuario canceló."""
        compute = "int8_float16" if use_dev == "cuda" else "int8"
        threads = hardware.cpu_threads()   # en GPU se ignora; en CPU usa todos los hilos
        extra = "" if use_dev == "cuda" else f", {threads} hilos"
        log(f"Cargando modelo «{model_name}» en {use_dev.upper()} ({compute}{extra})…")
        if use_dev == "cuda":
            log("(primer uso de un modelo nuevo: se descarga ~una vez)")
        from faster_whisper import WhisperModel
        try:
            model = WhisperModel(model_name, device=use_dev, compute_type=compute,
                                 cpu_threads=threads)
        except Exception as e:
            if _is_download_error(e):
                raise RuntimeError(
                    f"No se pudo descargar el modelo «{model_name}» (la 1ª vez se baja de internet). "
                    f"Revisá tu conexión y reintentá. Detalle: {e}") from e
            raise

        log(f"Transcribiendo {audio.name}…")
        seg_iter, info = model.transcribe(
            str(audio), language=lang or None, word_timestamps=True,
            vad_filter=True, beam_size=5)

        total = float(getattr(info, "duration", 0.0) or 0.0)
        t0 = time.time()
        words: list[dict] = []
        segments: list[dict] = []
        for seg in seg_iter:
            if cancel is not None and cancel.is_set():
                return "cancel"
            seg_words = []
            for w in (seg.words or []):
                e = {"word": w.word.strip(), "start": round(w.start, 3),
                     "end": round(w.end, 3), "prob": round(w.probability, 3)}
                words.append(e); seg_words.append(e)
            segments.append({"id": seg.id, "start": round(seg.start, 3),
                             "end": round(seg.end, 3), "text": seg.text.strip(),
                             "words": seg_words})
            log(f"[{fmt_cue(seg.start)}] {seg.text.strip()}")
            if progress_cb and total > 0:
                frac = min(seg.end / total, 1.0)
                el = time.time() - t0
                eta = (el / frac - el) if frac > 0.02 else None
                progress_cb(frac, eta)
        return words, segments, info, total, use_dev

    # intento en el dispositivo elegido; si la GPU se queda sin memoria, cae a CPU solo
    try:
        out = _attempt(dev)
    except Exception as e:
        if dev == "cuda" and _is_gpu_oom(e):
            log("⚠ La GPU se quedó sin memoria — reintentando en CPU (más lento pero seguro)…")
            if progress_cb:
                progress_cb(0.0, None)
            out = _attempt("cpu")
        else:
            raise

    if out == "cancel":
        log("Cancelado.")
        return None
    words, segments, info, total, dev = out

    if not segments:
        raise RuntimeError("No se detectó voz en el audio.")

    # Salidas: se definen UNA vez y se escriben DOS veces — antes de alinear (transcripción cruda,
    # crash-safe: si la alineación muere de forma nativa, al menos queda esto en disco) y después de
    # alinear (con los tiempos corregidos).
    written: dict[str, str] = {}

    def emit(name: str, data: str):
        p = outdir / (f"{stem}.{name}" if stem else name)
        p.write_text(data, encoding="utf-8")
        written[name] = str(p)

    def emit_all():
        emit("words.json", json.dumps(words, ensure_ascii=False, indent=2))
        if want_segments:
            emit("segments.json", json.dumps(segments, ensure_ascii=False, indent=2))
        if want_srt:
            srt = []
            for i, s in enumerate(segments, 1):
                srt += [str(i), f"{fmt_srt(s['start'])} --> {fmt_srt(s['end'])}", s["text"], ""]
            emit("srt", "\n".join(srt))
        if want_cues:
            dm, ds = int(total // 60), int(total % 60)
            lines = [f"# Cues — {audio.name}",
                     f"Duración: {dm}:{ds:02d} · Idioma: {info.language} "
                     f"(p={info.language_probability:.2f}) · Modelo: {model_name} · {dev.upper()}",
                     "", "Anotá al lado de cada frase qué gráfica/escena entra y usá el",
                     "timestamp como `data-start` de la composición.", ""]
            for s in segments:
                lines.append(f"- **[{fmt_cue(s['start'])}]** {s['text']}")
            emit("cues.md", "\n".join(lines))

    emit_all()   # transcripción CRUDA primero → nunca te quedás sin nada si la alineación crashea

    # Alineación forzada: whisper transcribe bien pero sus tiempos de palabra son flojos (pega
    # palabras+silencio+respiración en una sola). MMS re-alinea el texto al audio → bordes precisos.
    aligned_ok = False
    if want_align:
        aligned_ok = align_transcription(audio, words, segments, required=align_required,
                                         log_cb=log, progress_cb=progress_cb, cancel=cancel)
        if aligned_ok:
            emit_all()   # re-escribir todas las salidas con los tiempos corregidos

    if progress_cb:
        progress_cb(1.0, 0.0)

    low_conf = [w for w in words if w["prob"] < 0.5]
    return {
        "written": written,
        "device": dev,
        "duration": total,
        "language": info.language,
        "n_words": len(words),
        "n_segments": len(segments),
        "aligned": aligned_ok,
        "low_conf": low_conf,
    }
