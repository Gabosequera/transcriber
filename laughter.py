#!/usr/bin/env python3
"""
laughter.py — Detección de RISA con omine-me/LaughterSegmentation (Interspeech 2024),
un fine-tune de wav2vec2 (Wav2Vec2ForAudioFrameClassification) que predice risa POR
FRAME (~20 ms). Verificado que carga y corre en nuestro entorno (torch 2.13 CPU,
transformers 4.57, py3.13).

Estrategia (consensuada con Codex vía three-brain):
  · Se usa el windowing interno de omine: ventanas de 7 s con 2 s de solape → NO se
    super-chunkea (para <=1h el audio entra en RAM ~230MB y menos fronteras = menos
    riesgo de offsets). El preproceso `custom_amplituder` corre GLOBAL (consistente).
  · En vez del merge por intervalos de omine, se arma un TIMELINE GLOBAL de
    probabilidad (max-pool de las ventanas solapadas) → timestamps ABSOLUTOS por
    construcción, sin duplicados, y con confianza por evento gratis.
  · Se validan invariantes (0<=t_ini<t_fin<=dur, min 0.2s) y se dan stats por evento.

Risa = stream propio de eventos {t_ini, t_fin, tipo:"laughter", conf, ...}, para
mergear por tiempo con la metadata de emoción (que va por segmento).

Setup (una vez): git clone del repo en ./LaughterSegmentation ; el modelo
(model.safetensors, research-only) se baja solo de HuggingFace la 1ª vez.
"""
from __future__ import annotations

import os
import sys

import numpy as np

import app_paths

REPO = str(app_paths.LAUGHTER_REPO)
BASE = "jonatasgrosman/wav2vec2-large-xlsr-53-english"
HF_MODEL = "omine-me/LaughterSegmentation"

_MODEL = None


def unload():
    """Suelta el modelo de risa cacheado (liberar RAM/VRAM)."""
    global _MODEL
    _MODEL = None


def available() -> bool:
    # find_spec en vez de importar (arranque de la GUI — ver metadata.available)
    from importlib.util import find_spec
    try:
        if any(find_spec(m) is None for m in
               ("torch", "transformers", "safetensors", "pydub", "scipy",
                "huggingface_hub")):
            return False
        return os.path.exists(os.path.join(REPO, "train", "model.py"))
    except Exception:
        return False


def _load():
    global _MODEL
    if _MODEL is None:
        import torch
        import safetensors.torch
        from huggingface_hub import hf_hub_download
        if REPO not in sys.path:
            sys.path.insert(0, REPO)
        from train.model import Model
        # neutralizar el `kill_subprocess_randomly` de omine (mata procesos hijos al
        # azar; era un hack de su entorno de training, peligroso en inferencia)
        Model.kill_subprocess_randomly = lambda self: None
        import hardware
        mp = hf_hub_download(repo_id=HF_MODEL, filename="model.safetensors")
        state = safetensors.torch.load_file(mp, "cpu")

        def _build(d):
            dev = torch.device(d)
            m = Model(BASE, dev, 16000).to(dev)
            m.load_state_dict(state)
            m.eval()
            return m

        model, _dev = hardware.load_model_safe(_build)
        _MODEL = model
    return _MODEL


def _amplitude_boost(array, sr, mul_fac=5):
    """Preproceso de omine: amplifica x`mul_fac` las zonas silenciosas (con fade) para
    que la risa suave se detecte mejor. Global sobre todo el audio → consistente."""
    import librosa
    from pydub import AudioSegment
    from pydub.silence import detect_silence

    dub = AudioSegment((array * 32767).astype("int16").tobytes(),
                       sample_width=2, frame_rate=sr, channels=1).set_frame_rate(sr)
    silent = detect_silence(dub, min_silence_len=270, silence_thresh=-35)
    sr_mul = sr // 1000
    out = array.copy()
    for s0, s1 in silent:
        fade = int(sr * 0.15)
        a, b = s0 * sr_mul, s1 * sr_mul
        if (s1 - s0) * sr_mul > fade * 2:
            out[a:a + fade] *= np.linspace(1, mul_fac, fade)
            out[a + fade:b - fade] *= mul_fac
            if b < len(out):
                out[b - fade:b] *= np.linspace(mul_fac, 1, fade)
        else:
            out[a:b] *= mul_fac
    return librosa.util.normalize(out)


def detect(audio, *, threshold=0.5, amplitude_boost=True, min_dur=0.2, merge_gap=0.2,
           input_sec=7, overlap_sec=2.0, batch_size=10, log_cb=None, progress_cb=None,
           cancel=None) -> list[dict]:
    """Devuelve [{t_ini, t_fin, tipo:'laughter', conf, mean_conf, max_conf, dur}, ...]
    con timestamps ABSOLUTOS. Ventanas de `input_sec` con `overlap_sec` de solape."""
    import torch
    import librosa
    import hardware

    hardware.apply_torch_threads()   # hilos según config global (default: todos)

    def log(m):
        if log_cb:
            log_cb(m)

    model = _load()
    dev = next(model.parameters()).device
    log(f"Cargando modelo de risa (omine)… · {dev.type.upper()} · {torch.get_num_threads()} hilos CPU")
    sr = 16000
    import audiocache
    wav = audiocache.load(audio, sr=sr, mono=True)   # caché compartida (evita re-decodificar)
    total = len(wav) / sr
    if amplitude_boost:
        log("Preproceso (amplificando zonas suaves)…")
        wav = _amplitude_boost(wav, sr)

    log("Detectando risa (ventanas de 7 s)…")
    hop = int(sr * (input_sec - overlap_sec))     # 5 s de paso entre ventanas
    win = sr * input_sec
    n = len(wav)

    # timeline global de probabilidad: bin = duración de frame del modelo (~20 ms).
    # se resuelve con la 1ª ventana; para 7 s el modelo da 349 frames → 20.06 ms/frame.
    frame_dur = None
    timeline = None

    step = hop * batch_size
    for array_idx in range(0, n, step):
        if cancel is not None and cancel.is_set():
            raise InterruptedError("detección de risa cancelada")
        batched, should_break = [], False
        for b in range(batch_size):
            s = array_idx + b * hop
            arr = wav[s:s + win]
            if len(arr) < win:
                arr = np.append(arr, np.zeros(win - len(arr)))
                should_break = True
            batched.append(arr)
            if should_break:
                break
        inp = torch.from_numpy(np.asarray(batched)).float().to(dev)
        with torch.no_grad():
            logits = model(input_values=inp)[1]
        probs = torch.sigmoid(logits.float()).cpu().numpy()   # [B, frames]

        if timeline is None:
            fcount = probs.shape[1]
            frame_dur = input_sec / fcount
            timeline = np.zeros(int(total / frame_dur) + 4, dtype=np.float32)

        for b, pr in enumerate(probs):
            base_t = (array_idx + b * hop) / float(sr)
            base = int(round(base_t / frame_dur))
            end = min(base + len(pr), len(timeline))
            k = end - base
            if k > 0:
                timeline[base:end] = np.maximum(timeline[base:end], pr[:k])   # max-pool solapes

        if progress_cb:
            progress_cb(min(1.0, (array_idx + step) / n), None)
        if log_cb:
            log(f"Risa… {min(int((array_idx + step) / sr), int(total))}s / {int(total)}s")

    if timeline is None:
        return []

    # umbral + extracción de segmentos sobre el timeline global (timestamps absolutos)
    mask = timeline >= threshold
    events = []
    k = 0
    n_bins = len(timeline)
    while k < n_bins:
        if mask[k]:
            j = k
            while j < n_bins and mask[j]:
                j += 1
            seg = timeline[k:j]
            t0, t1 = k * frame_dur, j * frame_dur
            events.append({"t_ini": round(t0, 3), "t_fin": round(min(t1, total), 3),
                           "tipo": "laughter",
                           "conf": round(float(seg.max()), 3),
                           "mean_conf": round(float(seg.mean()), 3),
                           "max_conf": round(float(seg.max()), 3)})
            k = j
        else:
            k += 1

    # unir eventos muy cercanos (gap < merge_gap) y descartar los muy cortos
    merged = []
    for e in events:
        if merged and e["t_ini"] - merged[-1]["t_fin"] < merge_gap:
            merged[-1]["t_fin"] = e["t_fin"]
            merged[-1]["conf"] = max(merged[-1]["conf"], e["conf"])
            merged[-1]["max_conf"] = max(merged[-1]["max_conf"], e["max_conf"])
            merged[-1]["mean_conf"] = round((merged[-1]["mean_conf"] + e["mean_conf"]) / 2, 3)
        else:
            merged.append(e)
    out = []
    for e in merged:
        dur = e["t_fin"] - e["t_ini"]
        if dur < min_dur:                         # invariante: descartar < min_dur
            continue
        if not (0 <= e["t_ini"] < e["t_fin"] <= total + 0.01):   # invariante de tiempo
            continue
        e["dur"] = round(dur, 3)
        out.append(e)
    log(f"Listo: {len(out)} risa(s) detectada(s).")
    return out
