#!/usr/bin/env python3
"""
medios.py — inspección y manipulación de MEDIA para la pestaña «Extraer metadata».

Todo lo que el wizard necesita saber/hacer con el archivo fuente ANTES de correr el
pipeline: inspección ffprobe (streams, resolución, rotación, offsets de timeline),
fingerprint de la fuente (identidad para el resume), extracción de pistas a FLAC
normalizadas a la línea de tiempo canónica T0, frame de preview, envolvente min/max
para la forma de onda, y audición con mute/solo real (ffplay + amix).

Principios (spec: three-brain-out/2026-07-16-tab-unificada/DISENO-final.md):
  · ffmpeg/ffprobe/ffplay se resuelven del PATH (hardware.py ya antepone tools\\ffmpeg).
  · subprocess SIN shell; en Windows sin ventanas de consola y con Job Object
    KILL_ON_JOB_CLOSE (best-effort) para no dejar ffmpeg/ffplay huérfanos si muere la GUI.
  · Timeline canónica: T0 = start_time del stream de video (o del contenedor). Cada pista
    extraída se corrige por Δ = start_time − T0 (silencio antepuesto si Δ>0, recorte si
    Δ<0) y el offset aplicado queda registrado — los sidecars quedan todos en la misma
    línea de tiempo desde 0.
  · La identidad de la fuente NO es el path: size + hash muestreado (8MB al inicio/medio/
    fin) + inventario ffprobe canónico.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import threading
import time
from collections import deque
from pathlib import Path

import hardware  # noqa: F401  (importarlo antepone tools\ffmpeg al PATH en el bundle)

_BLOQUE_HASH = 8 * 1024 * 1024          # 8 MB por bloque muestreado
FLAC_VOZ_MONO = True                    # la voz se downmixea a mono (documentado en el manifest)


# ------------------------------------------------------------------ subprocess helpers --
def _flags() -> dict:
    """kwargs comunes para subprocess: sin shell, sin ventana de consola en Windows."""
    kw: dict = {}
    if os.name == "nt":
        kw["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return kw


_JOB = None
_JOB_LOCK = threading.Lock()


def _job_object():
    """Windows: Job Object con KILL_ON_JOB_CLOSE — si muere la app, el SO mata a los hijos
    (ffmpeg/ffplay no quedan huérfanos). Best-effort: si algo falla, se sigue sin job.
    Con LOCK (review reproductor r1.12): dos hilos creando el primer subprocess a la vez
    podían crear DOS jobs y repartir los hijos entre handles distintos."""
    global _JOB
    if os.name != "nt":
        return None
    with _JOB_LOCK:
        return _job_object_locked()


def _job_object_locked():
    global _JOB
    if _JOB is not None:
        return _JOB
    try:
        import ctypes
        from ctypes import wintypes

        class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
            _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64),
                        ("PerJobUserTimeLimit", ctypes.c_int64),
                        ("LimitFlags", wintypes.DWORD),
                        ("MinimumWorkingSetSize", ctypes.c_size_t),
                        ("MaximumWorkingSetSize", ctypes.c_size_t),
                        ("ActiveProcessLimit", wintypes.DWORD),
                        ("Affinity", ctypes.c_size_t),
                        ("PriorityClass", wintypes.DWORD),
                        ("SchedulingClass", wintypes.DWORD)]

        class IO_COUNTERS(ctypes.Structure):
            _fields_ = [(n, ctypes.c_uint64) for n in
                        ("ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                         "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

        class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
            _fields_ = [("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
                        ("IoInfo", IO_COUNTERS),
                        ("ProcessMemoryLimit", ctypes.c_size_t),
                        ("JobMemoryLimit", ctypes.c_size_t),
                        ("PeakProcessMemoryUsed", ctypes.c_size_t),
                        ("PeakJobMemoryUsed", ctypes.c_size_t)]

        # use_last_error=True: sin esto ctypes.get_last_error() puede devolver 0 o un
        # valor viejo (windll.kernel32 no captura el last-error — review ronda 5)
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        globals()["_K32"] = k32
        # restype/argtypes EXPLÍCITOS: en x64 el default c_int TRUNCA el HANDLE (review
        # Codex ronda 3, hallazgo 9) — con esto el handle de 64 bits viaja entero.
        k32.CreateJobObjectW.restype = wintypes.HANDLE
        k32.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
        k32.SetInformationJobObject.restype = wintypes.BOOL
        k32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int,
                                                wintypes.LPVOID, wintypes.DWORD]
        k32.AssignProcessToJobObject.restype = wintypes.BOOL
        k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        job = k32.CreateJobObjectW(None, None)
        if not job:
            raise OSError("CreateJobObjectW falló")
        info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        info.BasicLimitInformation.LimitFlags = 0x2000   # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not k32.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info)):
            raise OSError("SetInformationJobObject falló")
        _JOB = job
    except Exception:
        _JOB = None
    return _JOB


_JOB_AVISADO = False


def _popen(cmd, **kw) -> subprocess.Popen:
    """Popen con los flags comunes + asignación al Job Object en Windows. Si la
    asignación FALLA no se esconde: se avisa (una vez) por stderr — ese hijo quedaría
    fuera del kill-on-close y puede sobrevivir a la GUI."""
    global _JOB_AVISADO
    p = subprocess.Popen(cmd, **_flags(), **kw)
    job = _job_object()
    if job is not None:
        try:
            import ctypes
            from ctypes import wintypes
            k32 = globals().get("_K32")        # la WinDLL con use_last_error=True
            ok = k32.AssignProcessToJobObject(job, wintypes.HANDLE(int(p._handle)))
            err = ctypes.get_last_error()      # leer INMEDIATAMENTE tras el fallo
            if not ok and not _JOB_AVISADO:
                _JOB_AVISADO = True
                import sys
                print(f"[medios] AssignProcessToJobObject falló (GetLastError={err}): "
                      f"los subprocesos no quedan atados a la app — pueden sobrevivir "
                      f"si la GUI muere.", file=sys.stderr)
        except Exception:
            pass
    return p


# API pública para que otros módulos (cara.py) lancen sus subprocesos LARGOS con los
# mismos flags Windows + Job Object (si la GUI muere, el SO los mata igual).
popen_gestionado = _popen
flags_subprocess = _flags


_FFPLAY_OK: bool | None = None


def ffplay_disponible() -> bool:
    """Memoizado en ambos sentidos (review reproductor r2.14): el spawn de
    `ffplay -version` corría en el hilo Tk EN CADA play/re-mezcla (en Windows: proceso
    + antivirus cada vez). Si ffplay se instala con la app abierta: reabrirla."""
    global _FFPLAY_OK
    if _FFPLAY_OK is not None:                 # memoiza también el False (r2.14):
        return _FFPLAY_OK                      # reintentar = reabrir la app
    try:
        r = subprocess.run(["ffplay", "-version"], capture_output=True, timeout=10, **_flags())
        _FFPLAY_OK = r.returncode == 0
    except Exception:
        _FFPLAY_OK = False
    return _FFPLAY_OK


# ------------------------------------------------------------------------- inspección --
def inspeccionar(path) -> dict:
    """ffprobe completo de la fuente. Devuelve un dict canónico:
      {path, size, mtime_ns, duracion, t0, video: {width, height, rotacion, fps, codec},
       pistas: [{idx (relativo a audio), codec, canales, layout, sample_rate, start_time,
                 delta (vs t0), duracion, titulo}]}
    Lanza RuntimeError con mensaje claro si ffprobe falla o no hay streams."""
    path = Path(path)
    if not path.exists():
        raise RuntimeError(f"No existe el archivo: {path}")
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-print_format", "json", "-show_format",
         "-show_streams", str(path)],
        capture_output=True, text=True, encoding="utf-8", errors="replace", **_flags())
    if r.returncode != 0:
        raise RuntimeError(f"ffprobe no pudo leer el archivo: {(r.stderr or '').strip()[:300]}")
    data = json.loads(r.stdout or "{}")
    fmt = data.get("format", {})
    streams = data.get("streams", [])
    vs = next((s for s in streams if s.get("codec_type") == "video"), None)
    dur = float(fmt.get("duration") or 0.0)

    video = None
    t0 = float(fmt.get("start_time") or 0.0)
    if vs:
        t0 = float(vs.get("start_time") or t0)
        rot = 0
        for sd in vs.get("side_data_list") or []:
            if "rotation" in sd:
                rot = int(sd["rotation"]) % 360
        # fps: r_frame_rate "60/1"
        fps = 0.0
        try:
            num, den = (vs.get("avg_frame_rate") or vs.get("r_frame_rate") or "0/1").split("/")
            fps = float(num) / float(den) if float(den) else 0.0
        except Exception:
            pass
        w, h = int(vs.get("width") or 0), int(vs.get("height") or 0)
        # dims de DISPLAY: lo que se VE tras la autorrotación de ffmpeg — el preview, los
        # rects del wizard y el crop de cara trabajan TODOS en este espacio (ronda 3, h.7)
        dw, dh = (h, w) if rot in (90, 270) else (w, h)
        video = {"width": w, "height": h, "display_width": dw, "display_height": dh,
                 "rotacion": rot, "fps": round(fps, 3), "codec": vs.get("codec_name"),
                 "pix_fmt": vs.get("pix_fmt")}

    pistas = []
    aidx = 0
    for s in streams:
        if s.get("codec_type") != "audio":
            continue
        st = float(s.get("start_time") or 0.0)
        pistas.append({
            "idx": aidx,                                   # índice RELATIVO de audio (0:a:N)
            "codec": s.get("codec_name"),
            "canales": int(s.get("channels") or 0),
            "layout": s.get("channel_layout") or "",
            "sample_rate": int(s.get("sample_rate") or 0),
            "start_time": st,
            "delta": round(st - t0, 6),                    # offset vs la timeline canónica
            "duracion": float(s.get("duration") or fmt.get("duration") or 0.0),
            "titulo": (s.get("tags") or {}).get("title") or "",
        })
        aidx += 1

    st = path.stat()
    return {"path": str(path), "size": st.st_size, "mtime_ns": st.st_mtime_ns,
            "duracion": dur, "t0": t0, "video": video, "pistas": pistas}


def _hash_muestreado(path: Path, size: int) -> str:
    """SHA-256 de 3 bloques de 8 MB (inicio/medio/fin) + el size. Barato aun con 30 GB."""
    h = hashlib.sha256()
    h.update(str(size).encode())
    with open(path, "rb") as f:
        for off in (0, max(0, size // 2 - _BLOQUE_HASH // 2), max(0, size - _BLOQUE_HASH)):
            f.seek(off)
            h.update(f.read(min(_BLOQUE_HASH, size)))
    return h.hexdigest()


def fingerprint(path, info: dict | None = None) -> dict:
    """Identidad de la fuente para el resume: size + hash muestreado + hash del inventario
    ffprobe canónico. El path es solo un localizador (no forma parte de la identidad)."""
    path = Path(path)
    info = info or inspeccionar(path)
    inv = {"duracion": info["duracion"], "t0": info["t0"], "video": info["video"],
           "pistas": [{k: p[k] for k in ("idx", "codec", "canales", "sample_rate",
                                         "start_time", "duracion")} for p in info["pistas"]]}
    inv_hash = hashlib.sha256(json.dumps(inv, sort_keys=True).encode()).hexdigest()
    return {"size": info["size"], "mtime_ns": info["mtime_ns"],
            "hash_muestreado": _hash_muestreado(path, info["size"]),
            "inventario_sha256": inv_hash}


def archivo_estable(path, intervalo: float = 1.5) -> bool:
    """True si el tamaño no cambió entre dos lecturas separadas por `intervalo` (el archivo
    no se está copiando/grabando todavía)."""
    p = Path(path)
    s1 = p.stat().st_size
    time.sleep(max(0.2, intervalo))
    return p.stat().st_size == s1


def hash_archivo(path) -> str:
    """SHA-256 completo (para artefactos chicos: JSONs, FLACs de pista)."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


# ------------------------------------------------------------------------- extracción --
def extraer_pista(video, pista: dict, destino, *, mono=False, sample_rate=None, cancel=None,
                  log_cb=None, progress_cb=None) -> dict:
    """Extrae UNA pista de audio a FLAC, normalizada a la timeline canónica T0:
    Δ = pista['delta']; Δ>0 → silencio antepuesto; Δ<0 → recorte del arranque. Devuelve
    {"delta_aplicado", "operacion", "duracion", "sha256"}. Escribe a `destino` (el caller
    decide staging). Cancelable (mata el ffmpeg)."""
    video, destino = Path(video), Path(destino)
    destino.parent.mkdir(parents=True, exist_ok=True)
    delta = float(pista.get("delta") or 0.0)
    af, op = [], "ninguna"
    if delta > 0:
        af.append(f"adelay={int(round(delta * 1000))}:all=1")
        op = f"silencio antepuesto {delta * 1000:.0f} ms"
    elif delta < 0:
        af.append(f"atrim=start={-delta:.6f},asetpts=PTS-STARTPTS")
        op = f"recorte inicial {-delta * 1000:.0f} ms"
    cmd = ["ffmpeg", "-hide_banner", "-nostdin", "-y", "-i", str(video),
           "-map", f"0:a:{pista['idx']}", "-vn"]
    if af:
        cmd += ["-af", ",".join(af)]
    if mono:
        # -ac 1 = downmix ESTÁNDAR de ffmpeg según el channel layout (incluye el canal
        # CENTRAL de un 5.1 — un pan a mano con FL/FR lo perdería; review ronda 3, h.10)
        cmd += ["-ac", "1"]
    if sample_rate is not None:
        cmd += ["-ar", str(int(sample_rate))]
    cmd += ["-c:a", "flac", "-progress", "pipe:1", "-loglevel", "error", str(destino)]
    if log_cb:
        log_cb(f"Extrayendo pista a:{pista['idx']} → {destino.name}"
               + (f" ({op})" if op != "ninguna" else "") + (" · mono" if mono else ""))

    total_us = max(pista.get("duracion") or 0.0, 0.001) * 1e6
    p = _popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
               text=True, encoding="utf-8", errors="replace")
    try:
        for line in p.stdout:                        # -progress pipe:1 → key=value por línea
            if cancel is not None and cancel.is_set():
                p.terminate()
                raise InterruptedError("extracción cancelada")
            if line.startswith("out_time_us=") and progress_cb:
                try:
                    progress_cb(min(1.0, int(line.split("=", 1)[1]) / total_us))
                except ValueError:
                    pass
        p.wait()
    finally:
        if p.poll() is None:
            p.kill()
        p.wait()
        p.stdout.close()
        extraction_error = (p.stderr.read() or "").strip()[:300]
        p.stderr.close()
    if p.returncode != 0:
        raise RuntimeError(f"ffmpeg falló extrayendo la pista a:{pista['idx']}: {extraction_error}")
    # validación mínima acá (el validador del pipeline decodifica ventanas además)
    if not destino.exists() or destino.stat().st_size == 0:
        raise RuntimeError(f"la extracción no produjo datos ({destino.name})")
    dur = _dur_audio(destino)
    esperada = max(0.0, (pista.get("duracion") or 0.0) + max(delta, 0.0) - max(-delta, 0.0))
    if esperada > 1.0 and abs(dur - esperada) > 2.0:
        raise RuntimeError(f"duración extraída incompatible con la timeline "
                           f"({dur:.1f}s vs {esperada:.1f}s esperados) — ¿pista truncada?")
    return {"delta_aplicado": delta, "operacion": op, "duracion": dur,
            "sha256": hash_archivo(destino), "mono": bool(mono)}


def _dur_audio(path) -> float:
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                        "-of", "default=nw=1:nk=1", str(path)],
                       capture_output=True, text=True, **_flags())
    try:
        return float((r.stdout or "0").strip())
    except ValueError:
        return 0.0


def decodifica_ventanas(path, dur: float | None = None) -> bool:
    """Valida que el audio DECODIFICA de verdad: una ventana al inicio y otra cerca del
    final (leer solo el header no detecta truncamientos). True si ambas decodifican."""
    dur = dur or _dur_audio(path)
    if dur <= 0:
        return False
    for ss in (0.0, max(0.0, dur - 2.0)):
        r = subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-ss", f"{ss:.3f}",
                            "-i", str(path), "-t", "1", "-f", "null", "-"],
                           capture_output=True, **_flags())
        if r.returncode != 0:
            return False
    return True


# ------------------------------------------------------------------ preview / waveform --
def frame_preview(video, t: float, info: dict | None = None, max_w: int = 1024):
    """Un frame del video en `t` segundos como PIL.Image, con la rotación de metadata YA
    aplicada (lo que se ve = lo que se recorta). None si no decodifica.
    RENDIMIENTO (consenso timeline-marcas): se decodifica ESCALADO en ffmpeg
    (max_w px de ancho) y a MJPEG — el encode PNG de un frame 1440p/4K era el 90% del
    costo del scrub; el caller ya no necesita thumbnail() después."""
    from io import BytesIO
    from PIL import Image
    cmd = ["ffmpeg", "-v", "error", "-nostdin", "-ss", f"{max(0.0, t):.3f}", "-i", str(video),
           "-frames:v", "1", "-an", "-sn", "-dn",
           "-vf", f"scale='min({int(max_w)},iw)':-2",
           "-f", "image2pipe", "-c:v", "mjpeg", "-q:v", "4", "-"]
    r = subprocess.run(cmd, capture_output=True, timeout=60, **_flags())
    if r.returncode != 0 or not r.stdout:
        return None
    img = Image.open(BytesIO(r.stdout)); img.load()
    v = (info or {}).get("video") or {}
    rot = v.get("rotacion", 0)
    # ffmpeg ≥5 autorota por default al DECODIFICAR. Solo si el build no autorota (raro)
    # haría falta girar — con el frame ya escalado se detecta por ASPECTO: si el jpeg
    # conserva la orientación CRUDA (w/h) en vez de la de display (h/w), se gira acá.
    if rot in (90, 270) and v.get("width") and v.get("height") and img.height:
        crudo = v["width"] / v["height"]
        if abs(img.width / img.height - crudo) < abs(img.width / img.height - 1 / crudo):
            img = img.rotate(-rot, expand=True)
    return img


class FrameWorker:
    """UN hilo persistente de frames con «último pedido gana» (consenso timeline-marcas
    q.7): un slot de tamaño 1 — el pedido nuevo PISA al pendiente y TERMINA el ffmpeg
    activo (un scrub rápido no encola decodificaciones muertas). El resultado vuelve por
    callback `cb(token, t, img, costo_s)` DESDE EL HILO DEL WORKER (el caller re-postea
    a su cola de UI; acá no se toca Tk)."""

    def __init__(self):
        self._cond = threading.Condition()
        self._pedido = None
        self._proc: subprocess.Popen | None = None
        threading.Thread(target=self._loop, daemon=True, name="frames").start()

    def pedir(self, video, t: float, info: dict | None, token, cb, max_w: int = 1024):
        with self._cond:
            self._pedido = (str(video), float(t), info, token, cb, int(max_w))
            p = self._proc
            self._cond.notify()
        if p is not None and p.poll() is None:
            try:
                p.terminate()
            except Exception:
                pass

    def cancelar(self):
        """Descarta el pedido pendiente y mata el ffmpeg activo (cambio de video —
        review impl h.6: el token solo filtraba el CALLBACK; el proceso viejo seguía
        decodificando hasta 60s)."""
        with self._cond:
            self._pedido = None
            p = self._proc
        if p is not None and p.poll() is None:
            try:
                p.terminate()
            except Exception:
                pass

    def _loop(self):
        from io import BytesIO
        from PIL import Image
        while True:
            with self._cond:
                while self._pedido is None:
                    self._cond.wait()
                video, t, info, token, cb, max_w = self._pedido
                self._pedido = None
            t0 = time.monotonic()
            img = None
            try:
                cmd = ["ffmpeg", "-v", "error", "-nostdin", "-ss", f"{max(0.0, t):.3f}",
                       "-i", video, "-frames:v", "1", "-an", "-sn", "-dn",
                       "-vf", f"scale='min({max_w},iw)':-2",
                       "-f", "image2pipe", "-c:v", "mjpeg", "-q:v", "4", "-"]
                # el Popen se publica BAJO el lock y se re-chequea el slot: sin esto había
                # una ventana en la que un pedir()/cancelar() concurrente no encontraba
                # proceso que terminar (review impl h.6)
                with self._cond:
                    p = self._proc = _popen(cmd, stdout=subprocess.PIPE,
                                            stderr=subprocess.DEVNULL)
                    superado = self._pedido is not None
                if superado:
                    p.terminate()
                data, _ = p.communicate(timeout=60)
                if p.returncode == 0 and data:
                    img = Image.open(BytesIO(data)); img.load()
                    v = (info or {}).get("video") or {}
                    rot = v.get("rotacion", 0)
                    if rot in (90, 270) and v.get("width") and v.get("height") and img.height:
                        crudo = v["width"] / v["height"]
                        asp = img.width / img.height
                        if abs(asp - crudo) < abs(asp - 1 / crudo):
                            img = img.rotate(-rot, expand=True)
            except Exception:
                img = None
            finally:
                with self._cond:
                    if self._proc is not None and self._proc.poll() is None:
                        self._proc.kill()
                    self._proc = None
            try:
                cb(token, t, img, time.monotonic() - t0)
            except Exception:
                pass


# ------------------------------------------------------------------ video streaming --
# Diseño three-brain-out/2026-07-20-reproductor-optimizacion/DISENO.md (v2, r2 READY):
# el playback del preview deja de ser "un ffmpeg por frame cada 500 ms" (que en Windows
# con un video de 2 h superaba el umbral y se auto-apagaba) y pasa a UN decodificador
# continuo por sesión de play, con prefetch de scrub por ventana.

VS_BUF_MAX = 8                     # frames decodificados en espera (backpressure)
VS_FPS = 15.0                      # fps del playback del preview
VS_FPS_DEGRADADO = 10.0
VS_GRACIA_S = 8.0                  # STARTING: tope hasta el primer frame
VS_HAMBRE_S = 1.5                  # RUNNING: sin frames nuevos y buffer vacío → atraso
VS_COOLDOWN_S = 3.0                # entre respawns
VS_RESPAWNS_MAX = 4                # por sesión de play


class VideoStream:
    """UN ffmpeg persistente que decodifica DESDE t0 HACIA ADELANTE a fps capado y
    tamaño FIJO (W,H pares que pasa el caller), entregando rawvideo RGB24 por pipe.
    Framing por tamaño fijo (W*H*3 bytes/frame) — sin parsear MJPEG (r1.6).

    Buffer: deque + Condition (r1.1/r1.2). El lector NUNCA hace un put bloqueante:
    con el buffer lleno espera en cond.wait(0.2) chequeando _stop; el backpressure
    real lo pone el pipe del SO cuando nadie lee. Timestamps SINTÉTICOS
    ts = t0 + n/fps — solo para ELEGIR qué frame mostrar (error ≤ ~1 período por la
    fase del filtro fps); el diagnóstico de atraso NO los usa (r1.5): usa el reloj
    monotónico de arribo (`hambre()`).

    Estados (r1.7/r2.7, transiciones bajo el lock): starting → running (1er frame);
    lector termina rc=0 → eof, rc≠0 → failed (err_tail); parar() → stopped (terminal
    propio: el lector NO publica eof/failed tras stop). `parar()` es idempotente, no
    bloquea más de ~1 s y mata el proceso INDIVIDUALMENTE (no toca el Job global)."""

    STARTING, RUNNING, EOF, FAILED, STOPPED = ("starting", "running", "eof",
                                               "failed", "stopped")

    def __init__(self, video, t0: float, w: int, h: int, fps: float = VS_FPS,
                 dur: float | None = None):
        self.t0, self.fps = max(0.0, float(t0)), float(fps)
        self.w, self.h = int(w) // 2 * 2, int(h) // 2 * 2
        if self.w <= 0 or self.h <= 0 or self.fps <= 0:
            raise ValueError(f"dims/fps inválidos: {w}×{h}@{fps}")
        self._video, self._dur = str(video), dur
        self._buf: deque = deque()             # (ts, PIL.Image)
        self._cond = threading.Condition()
        self._stop = False
        self._proc: subprocess.Popen | None = None
        self._estado = self.STARTING
        self._err_tail = ""
        self._nacido = time.monotonic()
        self._arribo = self._nacido            # último frame que ENTRÓ al buffer
        self.primer_frame_s: float | None = None
        self._hilo = threading.Thread(target=self._loop, daemon=True, name="vstream")
        self._hilo.start()

    # ---- API (hilo de UI / consumidor) ----
    def frame_hasta(self, t: float):
        """Atómico: descarta del frente todo frame con ts ≤ t salvo el último y lo
        devuelve como (ts, img); los frames FUTUROS quedan. None si no hay vencidos."""
        out = None
        with self._cond:
            while self._buf and self._buf[0][0] <= t:
                out = self._buf.popleft()
            if out is not None:
                self._cond.notify_all()        # hay lugar: despierta al lector
        return out

    def sacar(self):
        """Saca UN frame (el más viejo) sin filtrar por tiempo — consumidor de
        prefetch, que quiere TODOS los frames. None si el buffer está vacío."""
        with self._cond:
            if not self._buf:
                return None
            f = self._buf.popleft()
            self._cond.notify_all()
            return f

    def estado(self) -> str:
        with self._cond:
            return self._estado

    def hambre(self) -> float:
        """Segundos desde el último frame que ENTRÓ al buffer (diagnóstico de atraso:
        solo significa algo con el buffer vacío y el proceso vivo)."""
        with self._cond:
            vacio = not self._buf
        return (time.monotonic() - self._arribo) if vacio else 0.0

    def edad(self) -> float:
        return time.monotonic() - self._nacido

    def err_tail(self) -> str:
        with self._cond:
            return self._err_tail

    def parar(self):
        with self._cond:
            self._stop = True
            p = self._proc
            self._cond.notify_all()
        if p is not None and p.poll() is None:
            try:
                p.kill()                       # el lector despierta por EOF del pipe
            except Exception:
                pass
        self._hilo.join(timeout=1.0)
        with self._cond:
            self._buf.clear()
            self._estado = self.STOPPED
            self._cond.notify_all()

    # ---- hilo lector ----
    def _loop(self):
        from PIL import Image
        import tempfile
        fsize = self.w * self.h * 3
        cmd = ["ffmpeg", "-v", "error", "-nostdin", "-ss", f"{self.t0:.3f}",
               "-i", self._video, "-an", "-sn", "-dn"]
        if self._dur is not None:
            cmd += ["-t", f"{max(0.0, float(self._dur)):.3f}"]
        cmd += ["-vf", f"fps={self.fps:g},scale={self.w}:{self.h},setsar=1",
                "-pix_fmt", "rgb24", "-f", "rawvideo", "-"]
        # stderr a un tempfile (r1.7: capturado para diagnóstico) — un PIPE sin lector
        # podría bloquear a ffmpeg si spamea errores
        errf = tempfile.TemporaryFile()
        p = None
        try:
            with self._cond:
                if self._stop:
                    return
                p = self._proc = _popen(cmd, stdout=subprocess.PIPE, stderr=errf)
            n = 0
            buf = bytearray()
            while not self._stop:
                chunk = p.stdout.read(fsize - len(buf))
                if not chunk:
                    break                      # EOF (fin de archivo, error o kill)
                buf += chunk
                if len(buf) < fsize:
                    continue
                img = Image.frombytes("RGB", (self.w, self.h), bytes(buf))
                buf.clear()
                ts = self.t0 + n / self.fps
                n += 1
                with self._cond:
                    while len(self._buf) >= VS_BUF_MAX and not self._stop:
                        self._cond.wait(0.2)   # jamás un put bloqueante (r1.1)
                    if self._stop:
                        return
                    self._buf.append((ts, img))
                    self._arribo = time.monotonic()
                    if self.primer_frame_s is None:
                        self.primer_frame_s = self._arribo - self._nacido
                        self._estado = self.RUNNING
        except Exception:
            pass
        finally:
            rc = None
            if p is not None:
                # SIEMPRE recolectar (review impl r3.1): si el lector murió por
                # excepción con ffmpeg vivo, sin kill el hijo quedaba bloqueado
                # escribiendo al pipe para siempre (fuga confirmada).
                if p.poll() is None:
                    try:
                        p.stdout.close()
                    except Exception:
                        pass
                    try:
                        p.kill()
                    except Exception:
                        pass
                try:
                    rc = p.wait(timeout=2)
                except Exception:
                    rc = None
            tail = ""
            try:
                errf.seek(0, 2)
                errf.seek(max(0, errf.tell() - 800))
                tail = errf.read().decode("utf-8", "replace").strip()
            except Exception:
                pass
            finally:
                try:
                    errf.close()
                except Exception:
                    pass
            with self._cond:
                if not self._stop:
                    self._estado = self.EOF if rc == 0 else self.FAILED
                    self._err_tail = tail
                self._proc = None
                self._cond.notify_all()


class SesionVideo:
    """Política de UNA sesión de playback (diseño v2 A/B): warm-up, diagnóstico de
    atraso, respawn con cooldown y degradación. Se usa desde el hilo de UI (el tick
    de animación); `log` recibe mensajes para la consola."""

    def __init__(self, video, t0: float, w: int, h: int, info: dict | None = None,
                 log=None):
        self._video, self._info, self._log = str(video), info, (log or (lambda m: None))
        self.w, self.h = int(w), int(h)
        self.fps = VS_FPS
        self._respawns = 0
        self._ultimo_respawn = 0.0
        self._degradado = False
        self.fallida = False
        self._stream = VideoStream(self._video, t0, self.w, self.h, self.fps)

    def lista(self) -> bool:
        """¿Ya llegó el primer frame? (warm-up A/V: el audio arranca recién acá o al
        vencer el tope del caller)."""
        return self._stream.primer_frame_s is not None

    def primer_frame_s(self):
        return self._stream.primer_frame_s

    def frame_para(self, t: float):
        """El frame a mostrar para el reloj `t` (o None: conservar el último mostrado).
        Aplica la política de atraso/respawn/degradación."""
        s = self._stream
        f = s.frame_hasta(t)
        if f is not None:
            return f[1]
        if self.fallida:
            return None
        est = s.estado()
        ahora = time.monotonic()
        if est == VideoStream.STARTING:
            if s.edad() > VS_GRACIA_S:
                self._fallar(f"el video no arranca (>{VS_GRACIA_S:.0f}s) — "
                             f"{s.err_tail() or 'sin detalle'}")
            return None
        if est == VideoStream.EOF:
            return None                        # fin del archivo: queda el último frame
        atrasado = (est == VideoStream.RUNNING and s.hambre() > VS_HAMBRE_S)
        if est == VideoStream.FAILED or atrasado:
            if self._respawns >= VS_RESPAWNS_MAX:
                self._fallar("el decodificador no da abasto; el audio sigue solo"
                             + (f" — {s.err_tail()}" if s.err_tail() else ""))
            elif ahora - self._ultimo_respawn >= VS_COOLDOWN_S:
                self._respawn(t)
        return None

    def _respawn(self, t: float):
        self._respawns += 1
        self._ultimo_respawn = time.monotonic()
        if self._respawns >= 2 and not self._degradado:
            self._degradado = True
            self.fps = VS_FPS_DEGRADADO
            # -25% conservando el ASPECTO y sin mínimo que agrande (review r3.2)
            nw = max(2, int(self.w * 0.75)) // 2 * 2
            self.h = max(2, int(round(self.h * nw / max(self.w, 1)))) // 2 * 2
            self.w = nw
            self._log(f"⚠ video atrasado; re-sincronizando (bajé a "
                      f"{self.fps:g}fps@{self.w}px)")
        self._stream.parar()
        # re-ancla los ts sintéticos al t actual (r1.5: sin drift acumulado)
        self._stream = VideoStream(self._video, t, self.w, self.h, self.fps)

    def _fallar(self, motivo: str):
        if not self.fallida:
            self.fallida = True
            self._log(f"⚠ preview de video detenido: {motivo}")
        self._stream.parar()

    def parar(self):
        self._stream.parar()


class Prefetcher:
    """Prefetch del scrub (diseño v2 C, r1.10): UN VideoStream lento (0.5 fps) por
    VENTANA [t-atras, t+adelante] — ~35 frames secuenciales de UN proceso, con sesgo
    hacia adelante. `apuntar()` es last-wins (mata la ventana anterior); `apuntar(None)`
    detiene (playback/drag). Los frames salen por `cb(token, t_grid, w, img)` DESDE el
    hilo del prefetcher (el caller postea a su cola de UI)."""

    GRID = 2.0                                 # s entre frames (fps 0.5)
    ATRAS, ADELANTE = 10.0, 60.0

    def __init__(self, cb):
        self._cb = cb
        self._cond = threading.Condition()
        self._pedido = None                    # (video, t, w, h, token) | None
        self._despierta = False
        self._stream: VideoStream | None = None
        threading.Thread(target=self._loop, daemon=True, name="prefetch").start()

    def apuntar(self, video, t: float, w: int, h: int, token, dur_total: float = 0.0):
        with self._cond:
            self._pedido = None if video is None else (str(video), float(t), int(w),
                                                       int(h), token, float(dur_total))
            self._despierta = True
            s = self._stream
            self._cond.notify_all()
        if s is not None:
            s.parar()                          # last-wins: la ventana vieja muere YA

    def parar(self):
        self.apuntar(None, 0.0, 0, 0, None)

    def _loop(self):
        while True:
            with self._cond:
                while not self._despierta:
                    self._cond.wait()
                self._despierta = False
                pedido = self._pedido
            if pedido is None:
                continue
            video, t, w, h, token, dur_total = pedido
            # bordes sobre la timeline real (r2.20): [max(0,t-10), min(dur,t+60)]
            t0 = max(0.0, t - self.ATRAS)
            fin = (min(dur_total, t + self.ADELANTE) if dur_total > 0
                   else t + self.ADELANTE)
            dur = fin - t0
            if dur < self.GRID:
                continue                       # ventana vacía (playhead al final)
            try:
                s = VideoStream(video, t0, w, h, fps=1.0 / self.GRID, dur=dur)
            except ValueError:
                continue
            with self._cond:
                if self._despierta:            # ya hay un pedido más nuevo
                    pass
                self._stream = s
            try:
                while True:
                    with self._cond:
                        if self._despierta:
                            break              # pedido nuevo → abandonar esta ventana
                    f = s.sacar()
                    if f is None:
                        if s.estado() in (VideoStream.EOF, VideoStream.FAILED,
                                          VideoStream.STOPPED):
                            break
                        time.sleep(0.05)
                        continue
                    ts, img = f
                    t_grid = round(ts / self.GRID) * self.GRID
                    try:
                        self._cb(token, t_grid, img.width, img)
                    except Exception:
                        pass
            finally:
                s.parar()
                with self._cond:
                    if self._stream is s:
                        self._stream = None


def _envolvente_stream(cmd: list, total: int, buckets: int,
                       cancel=None, progreso=None) -> list[tuple[float, float, float]]:
    """Núcleo NumPy del cálculo de envolvente (consenso timeline-marcas r2 h.3: el loop
    por muestra en Python eran 43M de iteraciones por pista de 90 min peleando el GIL —
    min/max/RMS vectorizados por chunk con reduceat y CARRY del bucket parcial).

    PARTICIÓN EXACTA (review impl h.1 + r2.1): produce EXACTAMENTE `buckets`
    particiones temporales de [0, total) — bucket de la muestra i = i*buckets//total
    (tamaños que alternan cuando la división no es exacta). Los tiles del zoom dependen
    de esa densidad exacta: compactar buckets vacíos o de menos desalineaba la waveform
    del playhead. Muestras de más del decoder se clampean al último bucket; buckets
    salteados o faltantes (undershoot) se rellenan con silencio (0,0,0) — el resultado
    SIEMPRE tiene `buckets` entradas, salvo cancelación (parcial)."""
    import numpy as np
    p = _popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    out: list[tuple[float, float, float]] = []
    pos = 0                                    # muestras ya consumidas
    _prog_last = [0.0, 0.0]                    # [frac, t] del último reporte de progreso
    pend = None                                # bucket parcial: [id, min, max, sumsq, n]
    done = threading.Event()
    if cancel is not None:
        # VIGILANTE (review impl-r3.1): read() es bloqueante — sin esto, cancelar con el
        # productor lento tardaba hasta llenar los 256 KiB. Al activarse el cancel se
        # TERMINA el proceso ya, el pipe cierra y el read despierta con EOF.
        def _vigilar():
            while not done.is_set():
                if cancel.wait(0.15):
                    if p.poll() is None:
                        try:
                            p.terminate()
                        except Exception:
                            pass
                    return
        threading.Thread(target=_vigilar, daemon=True).start()

    def _flush(pend_):
        # los slots VACÍOS entre el último bucket emitido y este se rellenan con
        # silencio (0,0,0): el consumidor indexa el resultado como secuencia temporal
        # DENSA — compactar los huecos re-desplazaba la waveform (review impl-r2 h.1)
        while len(out) < pend_[0]:
            out.append((0.0, 0.0, 0.0))
        out.append((pend_[1] / 32768.0, pend_[2] / 32768.0,
                    (pend_[3] / pend_[4]) ** 0.5 / 32768.0))

    try:
        while True:
            if cancel is not None and cancel.is_set():
                p.terminate()
                return out
            chunk = p.stdout.read(262144)
            if not chunk:
                break
            a = np.frombuffer(chunk[: len(chunk) // 2 * 2], dtype="<i2")
            if not a.size:
                continue
            # progreso (review reproductor r1.15): desde muestras consumidas/total,
            # throttled por tiempo Y delta; silencio tras cancelación (chequeo arriba)
            if progreso is not None and total > 0:
                frac = min(1.0, pos / total)
                ahora = time.monotonic()
                if frac - _prog_last[0] >= 0.05 and ahora - _prog_last[1] >= 0.5:
                    _prog_last[0], _prog_last[1] = frac, ahora
                    try:
                        progreso(frac)
                    except Exception:
                        pass
            idx = (pos + np.arange(a.size, dtype=np.int64)) * buckets // max(total, 1)
            np.clip(idx, 0, buckets - 1, out=idx)
            pos += a.size
            af = a.astype(np.float32)
            starts = np.concatenate(([0], np.flatnonzero(np.diff(idx)) + 1))
            mins = np.minimum.reduceat(af, starts)
            maxs = np.maximum.reduceat(af, starts)
            sums = np.add.reduceat(af * af, starts)
            cnts = np.diff(np.concatenate((starts, [a.size])))
            ids = idx[starts]
            for i in range(len(starts)):       # ~1 iteración por bucket TOCADO (no por muestra)
                bid = int(ids[i])
                if pend is not None and pend[0] == bid:
                    pend[1] = min(pend[1], float(mins[i]))
                    pend[2] = max(pend[2], float(maxs[i]))
                    pend[3] += float(sums[i]); pend[4] += int(cnts[i])
                else:
                    if pend is not None:
                        _flush(pend)
                    pend = [bid, float(mins[i]), float(maxs[i]),
                            float(sums[i]), int(cnts[i])]
        if cancel is not None and cancel.is_set():
            return out                         # cancelado: parcial, SIN rellenar
        if pend is not None:
            _flush(pend)
        while len(out) < buckets:              # undershoot del decoder / buckets > total:
            out.append((0.0, 0.0, 0.0))        # EXACTAMENTE `buckets` salvo cancelación
    finally:
        done.set()                             # apaga el vigilante
        if p.poll() is None:
            p.kill()
    return out


def envolvente(video, pista_idx: int, buckets: int = 2400, dur: float | None = None,
               cancel=None, progreso=None) -> list[tuple[float, float, float]]:
    """Envolvente por bucket para dibujar la forma de onda SIN aliasing (no se decima:
    se acumula el min/max de cada ventana) + RMS por bucket (el «cuerpo» de la onda —
    picos y energía dibujados por separado = el detalle de un editor de audio).
    Decodifica mono a 8 kHz por pipe (streaming, ~poca RAM); acumulación NumPy.
    Devuelve [(min, max, rms)] en [-1, 1]."""
    sr = 8000
    dur = dur or 0.0
    total = int(dur * sr) if dur > 0 else buckets * (sr // 4)   # sin dur: ~0.25s/bucket
    cmd = ["ffmpeg", "-v", "error", "-nostdin", "-i", str(video), "-map", f"0:a:{pista_idx}",
           "-ac", "1", "-ar", str(sr), "-f", "s16le", "-"]
    return _envolvente_stream(cmd, total, max(1, buckets), cancel, progreso)


def envolvente_ventana(video, pista_idx: int, t0: float, dur_v: float, buckets: int,
                       cancel=None) -> list[tuple[float, float, float]]:
    """Envolvente de UNA VENTANA [t0, t0+dur_v] — los TILES del zoom del timeline
    (consenso q.5): mismo cálculo que `envolvente` pero decodificando solo la ventana
    (`-ss` de contenedor: exactitud de ~un frame de audio, sobra para display)."""
    sr = 8000
    total = max(1, int(dur_v * sr))
    cmd = ["ffmpeg", "-v", "error", "-nostdin", "-ss", f"{max(0.0, t0):.3f}",
           "-i", str(video), "-map", f"0:a:{pista_idx}", "-t", f"{dur_v:.3f}",
           "-ac", "1", "-ar", str(sr), "-f", "s16le", "-"]
    return _envolvente_stream(cmd, total, max(1, buckets), cancel)


# ----------------------------------------------------------------------- reproducción --
class Reproductor:
    """Audición de pistas con MUTE/SOLO real: si hay solos, suenan los solos no muteados;
    si no, todas las no muteadas. La mezcla la hace FFMPEG (amix) y la reproduce FFPLAY
    leyendo el WAV por pipe (ffplay NO soporta -map/-filter_complex — verificado). Una
    instancia por wizard; `play` corta lo anterior."""

    def __init__(self):
        self._procs: tuple[subprocess.Popen, subprocess.Popen] | None = None
        self._lock = threading.Lock()

    def play(self, video, pistas_activas: list[int], t: float = 0.0) -> str | None:
        """Reproduce las pistas (índices 0:a:N) desde `t`. Devuelve None si arrancó, o un
        motivo si no se pudo."""
        self.stop()
        if not pistas_activas:
            return "no hay pistas activas (todo muteado)"
        if not ffplay_disponible():
            return "ffplay no está disponible en este equipo"
        mix = ["ffmpeg", "-v", "error", "-nostdin", "-ss", f"{max(0.0, t):.3f}",
               "-i", str(video)]
        if len(pistas_activas) == 1:
            mix += ["-map", f"0:a:{pistas_activas[0]}"]
        else:
            ins = "".join(f"[0:a:{i}]" for i in pistas_activas)
            mix += ["-filter_complex",
                    f"{ins}amix=inputs={len(pistas_activas)}:normalize=1[mix]",
                    "-map", "[mix]"]
        mix += ["-f", "wav", "-"]
        with self._lock:
            pf = _popen(mix, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            pp = _popen(["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet", "-"],
                        stdin=pf.stdout, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            pf.stdout.close()          # el pipe queda entre los dos procesos
            self._procs = (pf, pp)
        return None

    def playing(self) -> bool:
        with self._lock:
            return self._procs is not None and self._procs[1].poll() is None

    def stop(self):
        with self._lock:
            procs, self._procs = self._procs, None
        if not procs:
            return
        pf, pp = procs
        for p in (pp, pf):             # primero ffplay; ffmpeg muere solo por EPIPE
            if p.poll() is None:
                p.terminate()
                try:
                    p.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    p.kill()


import atexit

_REPRO_GLOBAL: list[Reproductor] = []


def reproductor() -> Reproductor:
    r = Reproductor()
    _REPRO_GLOBAL.append(r)
    return r


@atexit.register
def _cleanup():
    for r in _REPRO_GLOBAL:
        try:
            r.stop()
        except Exception:
            pass
