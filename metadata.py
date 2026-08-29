#!/usr/bin/env python3
"""
metadata.py — Extrae metadata emocional/paralingüística por segmento, fusionando
DOS señales (así es como lo hacen los sistemas multimodales serios):

  · AROUSAL desde el AUDIO — modelo dimensional de audeering (wav2vec2). El arousal
    es casi puramente paralingüístico (pitch, energía, ritmo, tensión vocal), así que
    transfiere bien del inglés al español: el modelo no necesita entender QUÉ decís
    para saber que estás acelerado.
  · VALENCE desde el TEXTO — pysentimiento (RoBERTuito, español real). El valence
    cruza mal entre idiomas desde el audio (depende del léxico), así que se saca del
    transcript ya alineado → sentiment/emoción en español nativo, 1:1 con timestamps.

Extras por segmento: f0_std (variación de tono) y wps (palabras/seg = velocidad).
Todo se z-score-a contra el baseline del video completo → buscás PICOS relativos a
vos, no valores absolutos. Salida pensada para dársela a una IA que corte clips.

Requiere (aparte del core): transformers (4.x), pysentimiento. `available()` chequea.
"""
from __future__ import annotations

import numpy as np

AUDEERING = "audeering/wav2vec2-large-robust-12-ft-emotion-msp-dim"

_EMO = None      # (processor, model) audeering
_SENT = None     # pysentimiento sentiment
_EMOT = None     # pysentimiento emotion


def unload():
    """Suelta los modelos de emoción/sentimiento cacheados (liberar RAM/VRAM)."""
    global _EMO, _SENT, _EMOT
    _EMO = _SENT = _EMOT = None


def available() -> bool:
    # find_spec en vez de importar: los import de torch/transformers acá tardaban
    # segundos y la GUI llama esto al CONSTRUIRSE (arranque lento, medido 2026-07-20).
    # Una instalación rota (paquete presente que no importa) se detecta al correr el paso.
    from importlib.util import find_spec
    try:
        return all(find_spec(m) is not None
                   for m in ("transformers", "pysentimiento", "torch"))
    except Exception:
        return False


def _emotion_class():
    """Clase custom del modelo de audeering (no es arquitectura estándar de HF)."""
    import torch
    import torch.nn as nn
    from transformers.models.wav2vec2.modeling_wav2vec2 import (
        Wav2Vec2Model, Wav2Vec2PreTrainedModel)

    class RegressionHead(nn.Module):
        def __init__(self, config):
            super().__init__()
            self.dense = nn.Linear(config.hidden_size, config.hidden_size)
            self.dropout = nn.Dropout(config.final_dropout)
            self.out_proj = nn.Linear(config.hidden_size, config.num_labels)

        def forward(self, x, **kw):
            x = self.dropout(x); x = self.dense(x); x = torch.tanh(x)
            x = self.dropout(x); return self.out_proj(x)

    class EmotionModel(Wav2Vec2PreTrainedModel):
        def __init__(self, config):
            super().__init__(config)
            self.config = config
            self.wav2vec2 = Wav2Vec2Model(config)
            self.classifier = RegressionHead(config)
            self.init_weights()

        def forward(self, x):
            h = torch.mean(self.wav2vec2(x)[0], dim=1)
            return h, self.classifier(h)

    return EmotionModel


def _load_audio():
    global _EMO
    if _EMO is None:
        import hardware
        from transformers import Wav2Vec2Processor
        proc = Wav2Vec2Processor.from_pretrained(AUDEERING)
        cls = _emotion_class()
        model, _dev = hardware.load_model_safe(lambda d: cls.from_pretrained(AUDEERING).eval().to(d))
        _EMO = (proc, model)
    return _EMO


def _load_text():
    global _SENT, _EMOT
    if _SENT is None:
        from pysentimiento import create_analyzer
        _SENT = create_analyzer(task="sentiment", lang="es")
        _EMOT = create_analyzer(task="emotion", lang="es")
    return _SENT, _EMOT


def _f0_std(chunk, sr=16000) -> float:
    import librosa
    try:
        f0 = librosa.yin(chunk, fmin=70, fmax=400, sr=sr, frame_length=1024)
        f0 = f0[(f0 > 75) & (f0 < 380)]
        return round(float(np.std(f0)), 1) if len(f0) > 3 else 0.0
    except Exception:
        return 0.0


def extract(audio, segments, *, log_cb=None, progress_cb=None) -> dict:
    """Devuelve {"events":[...], "baseline":{...}, "n":N}. `segments` = lista de
    {start, end, text, words?} (el segments.json de la transcripción)."""
    import torch
    import librosa
    import hardware

    hardware.apply_torch_threads()   # hilos según config global (default: todos)

    def log(m):
        if log_cb:
            log_cb(m)

    proc, model = _load_audio()
    dev = next(model.parameters()).device
    log(f"Cargando modelos de emoción (audio + texto)… · {dev.type.upper()} · {torch.get_num_threads()} hilos CPU")
    sent, emot = _load_text()
    import audiocache
    wav = audiocache.load(audio, sr=16000)          # caché compartida (evita re-decodificar)

    events = []
    n = len(segments)
    for i, s in enumerate(segments):
        t0, t1 = float(s["start"]), float(s["end"])
        dur = max(1e-3, t1 - t0)
        chunk = wav[int(t0 * 16000):int(t1 * 16000)]
        if len(chunk) < 400:               # < 25 ms: nada útil
            continue

        y = proc(chunk, sampling_rate=16000)["input_values"][0]
        with torch.no_grad():
            _, lg = model(torch.tensor(y).unsqueeze(0).to(dev))
        arousal, dominance, valence_a = (round(float(x), 3) for x in lg[0].cpu().numpy())

        text = (s.get("text") or "").strip()
        sr_ = sent.predict(text) if text else None
        er_ = emot.predict(text) if text else None
        nwords = len(s.get("words") or text.split())

        events.append({
            "t_ini": round(t0, 2), "t_fin": round(t1, 2), "text": text,
            "arousal": arousal, "dominance": dominance, "valence_audio": valence_a,
            "valence_texto": (sr_.output if sr_ else None),
            "sent_probas": ({k: round(float(v), 2) for k, v in sr_.probas.items()} if sr_ else None),
            "emotion": (er_.output if er_ else None),
            "f0_std": _f0_std(chunk), "wps": round(nwords / dur, 2),
        })
        if progress_cb:
            progress_cb((i + 1) / n, None)
        if log_cb and (i % 20 == 0 or i == n - 1):
            log(f"Analizando emoción… {i + 1}/{n} segmentos")

    # z-scores contra el baseline del video completo (picos relativos a vos)
    def zscore(key):
        vals = np.array([e[key] for e in events], dtype=float)
        mu = float(vals.mean()); sd = float(vals.std()) or 1.0
        for e in events:
            e[key + "_z"] = round((e[key] - mu) / sd, 2)
        return {"mean": round(mu, 3), "std": round(sd, 3)}

    baseline = {k: zscore(k) for k in ("arousal", "f0_std", "wps")}
    log(f"Listo: {len(events)} segmentos analizados.")
    return {"events": events, "baseline": baseline, "n": len(events)}
