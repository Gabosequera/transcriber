#!/usr/bin/env python3
"""
detect_breaths.py — Detecta respiraciones en una grabación de voz usando
Respiro-en (red neuronal frame-wise de detección de breath sounds, Interspeech
2024) y las exporta a un JSON que consume el modo quirúrgico de gate.py.

Es a la vez:
  · módulo importable — `detect(audio) -> [{"start","end","dur"}, ...]` (lo usa la
    app y gate.py); cachea el modelo entre llamadas.
  · script CLI — `python detect_breaths.py voz.wav`  → escribe voz.breaths.json

Setup (una vez, ya hecho en este proyecto):
  git clone https://github.com/ydqmkkx/Respiro-en.git   # queda en ./Respiro-en
  .venv/bin/pip install librosa intervaltree            # torch/torchaudio ya están

Threshold: 0.064 es el del paper (sensible). Si sobre-detecta (fricativas tipo
's' marcadas como respiración), subilo a 0.3-0.5. De todos modos el cruce con
words.json en gate.py descarta las respiraciones que solapan una palabra.

Nota: el modelo se entrenó en inglés (LibriTTS-R), pero una respiración es un
evento acústico, no lingüístico → transfiere bien al español. Verificalo contra
timestamps que ya conozcas la primera vez.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import app_paths

# clon local del repo, junto a este archivo (self-contained)
DEFAULT_REPO = app_paths.BREATH_REPO

_DETECTOR_CACHE: dict = {}


def unload():
    """Suelta el/los detector(es) Respiro cacheados (liberar RAM/VRAM)."""
    _DETECTOR_CACHE.clear()


def available(repo=DEFAULT_REPO) -> bool:
    """True si el repo de Respiro-en está presente Y los pesos son reales (no un puntero
    Git-LFS). Al bajar el repo por ZIP en Windows, `respiro-en.pt` puede venir como un
    puntero LFS de ~130 bytes en vez del modelo (~34 MB) → cargarlo explotaría en uso. Si es
    muy chico, la feature se reporta NO disponible (la GUI oculta el modo Quirúrgico)."""
    repo = Path(repo)
    pt = repo / "respiro-en.pt"
    if not ((repo / "modules.py").exists() and pt.exists()):
        return False
    try:
        return pt.stat().st_size > 1_000_000        # un puntero LFS pesa ~130 bytes
    except OSError:
        return False


def _load_detector(repo, device=None):
    """Carga (y cachea) el modelo Respiro-en. Reutilizado entre audios."""
    import torch
    key = (str(Path(repo).resolve()), str(device))
    if key in _DETECTOR_CACHE:
        return _DETECTOR_CACHE[key]
    repo = Path(repo)
    if str(repo) not in sys.path:
        sys.path.insert(0, str(repo))
    from modules import DetectionNet, BreathDetector
    import hardware

    ckpt = torch.load(repo / "respiro-en.pt", map_location="cpu")

    def _build(d):
        dev = torch.device(d)
        model = DetectionNet().to(dev)
        model.load_state_dict(ckpt["model"])
        model.eval()
        return BreathDetector(model, device=dev)

    if device is not None:                       # device explícito: honrarlo sin fallback
        det = _build(device.type if hasattr(device, "type") else str(device))
    else:                                        # auto: GPU con fallback a CPU si falta VRAM
        det, _dev = hardware.load_model_safe(_build)
    _DETECTOR_CACHE[key] = det
    return det


def detect(audio, *, repo=DEFAULT_REPO, threshold=0.064, min_length=20,
           chunk_s=15.0, overlap_s=2.0, device=None, log_cb=None) -> list[dict]:
    """Detecta respiraciones en `audio`. Devuelve [{"start","end","dur"}, ...] en
    segundos (ordenadas). `min_length` está en frames de 10 ms (20 ≈ 200 ms).

    IMPORTANTE — se procesa por TROZOS (`chunk_s` s, con `overlap_s` de solape):
    Respiro-en se entrenó con frases cortas de LibriTTS; darle un audio largo (varios
    minutos) de una sola pasada degrada mucho la detección (la atención del Conformer
    queda fuera de su distribución) → se pierden respiraciones. Trocear en ventanas
    cortas recupera ~15-20% más respiraciones y además es más rápido. El solape (2 s
    > que cualquier respiración) garantiza que ninguna quede partida en un borde; los
    dobles detectados en la zona de solape se fusionan."""
    import numpy as np
    import torch
    import librosa
    import hardware

    hardware.apply_torch_threads()   # hilos según config global (default: todos)
    repo = Path(repo)
    if not available(repo):
        raise RuntimeError(
            f"No se encuentra Respiro-en en {repo} (falta modules.py o respiro-en.pt).\n"
            "Clonalo con:  git clone https://github.com/ydqmkkx/Respiro-en.git")

    def log(m):
        if log_cb:
            log_cb(m)

    log("Cargando red de respiraciones (Respiro-en)…")
    det = _load_detector(repo, device)
    from modules import feature_extractor
    model, dev = det.model, det.device

    import audiocache
    wav = audiocache.load(audio, sr=16000)        # el modelo trabaja a 16 kHz mono (caché compartida)
    n = len(wav)
    win = max(1600, int(chunk_s * 16000))
    step = max(1, int(max(1.0, chunk_s - overlap_s) * 16000))
    n_chunks = max(1, (n + step - 1) // step)
    log(f"Analizando por trozos ({chunk_s:.0f}s, {n_chunks} ventanas)…")

    ivs = []
    pos = 0
    while pos < n:
        seg = wav[pos:min(n, pos + win)]
        if len(seg) < 1600:          # < 0.1 s de cola: nada útil
            break
        feature, length = feature_extractor(seg, 16000)
        with torch.no_grad():
            prob = model(feature.to(dev), length.to(dev))[0].detach().cpu().numpy()
        off = pos / 16000.0
        idx = np.where(prob > threshold)[0]
        if len(idx) >= 2:            # runs contiguos de frames sobre el umbral
            for run in np.split(idx, np.where(np.diff(idx) != 1)[0] + 1):
                if len(run) > min_length:
                    b, e = run[0] * 0.01 + off, run[-1] * 0.01 + off
                    if e > b:
                        ivs.append([b, e])
        pos += step

    ivs.sort()                       # fusionar solapados (dobles de la zona de overlap)
    merged = []
    for s, e in ivs:
        if merged and s <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], e)
        else:
            merged.append([s, e])
    return [{"start": round(s, 3), "end": round(e, 3), "dur": round(e - s, 2)}
            for s, e in merged]


def main() -> int:
    ap = argparse.ArgumentParser(description="Detección de respiraciones con Respiro-en")
    ap.add_argument("audio", type=Path)
    ap.add_argument("--repo", type=Path, default=DEFAULT_REPO,
                    help=f"carpeta del clon de Respiro-en (default: {DEFAULT_REPO})")
    ap.add_argument("--threshold", type=float, default=0.064,
                    help="umbral de detección (default 0.064, paper); subir si sobre-detecta")
    ap.add_argument("--min-length", type=int, default=20,
                    help="duración mínima en frames de 10 ms (default 20 ≈ 200 ms)")
    ap.add_argument("-o", "--output", type=Path, default=None)
    args = ap.parse_args()

    if not args.audio.exists():
        print(f"error: no existe {args.audio}", file=sys.stderr)
        return 1
    if not available(args.repo):
        print(f"error: {args.repo} no parece el repo de Respiro-en "
              "(falta modules.py o respiro-en.pt)", file=sys.stderr)
        return 1

    breaths = detect(args.audio, repo=args.repo, threshold=args.threshold,
                     min_length=args.min_length, log_cb=print)

    out_path = args.output or args.audio.with_suffix(".breaths.json")
    out_path.write_text(json.dumps(breaths, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"{len(breaths)} respiraciones detectadas:")
    for b in breaths:
        print(f'  [{b["start"]:7.2f}s → {b["end"]:7.2f}s]  {b["dur"]:.2f}s')
    print(f"salida → {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
