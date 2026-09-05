"""Prosodia acústica para el perfil ``editorial_voz``.

Extrae únicamente arousal acústico e intensidad por palabra. Deliberadamente no
carga modelos de sentimiento o emoción textual.
"""
from __future__ import annotations

import math
from importlib.util import find_spec
from pathlib import Path

import numpy as np


AUDEERING = "audeering/wav2vec2-large-robust-12-ft-emotion-msp-dim"
SAMPLE_RATE = 16_000
_MODEL = None


def available() -> bool:
    try:
        return all(find_spec(name) is not None for name in ("torch", "transformers", "librosa"))
    except Exception:
        return False


def unload() -> None:
    global _MODEL
    _MODEL = None


def _model_class():
    import torch
    import torch.nn as nn
    from transformers.models.wav2vec2.modeling_wav2vec2 import (
        Wav2Vec2Model, Wav2Vec2PreTrainedModel,
    )

    class RegressionHead(nn.Module):
        def __init__(self, config):
            super().__init__()
            self.dense = nn.Linear(config.hidden_size, config.hidden_size)
            self.dropout = nn.Dropout(config.final_dropout)
            self.out_proj = nn.Linear(config.hidden_size, config.num_labels)

        def forward(self, values):
            values = self.dropout(values)
            values = torch.tanh(self.dense(values))
            return self.out_proj(self.dropout(values))

    class EmotionModel(Wav2Vec2PreTrainedModel):
        def __init__(self, config):
            super().__init__(config)
            self.wav2vec2 = Wav2Vec2Model(config)
            self.classifier = RegressionHead(config)
            self.init_weights()

        def forward(self, values):
            hidden = self.wav2vec2(values)[0].mean(dim=1)
            return self.classifier(hidden)

    return EmotionModel


def _load_model():
    global _MODEL
    if _MODEL is None:
        import hardware
        from transformers import Wav2Vec2Processor

        processor = Wav2Vec2Processor.from_pretrained(AUDEERING)
        model_class = _model_class()
        model, _ = hardware.load_model_safe(
            lambda device: model_class.from_pretrained(AUDEERING).eval().to(device)
        )
        _MODEL = processor, model
    return _MODEL


def _zscore(events: list[dict], key: str, target: str) -> dict:
    values = np.asarray([event[key] for event in events], dtype=np.float64)
    if values.size == 0:
        return {"mean": 0.0, "std": 1.0}
    mean = float(values.mean())
    std = float(values.std()) or 1.0
    for event in events:
        event[target] = round((float(event[key]) - mean) / std, 3)
    return {"mean": round(mean, 6), "std": round(std, 6)}


def speech_regions(words: list[dict], *, gap_seconds: float = 1.0,
                   padding_seconds: float = 0.5,
                   duration: float | None = None) -> list[tuple[float, float]]:
    """Agrupa palabras alineadas en regiones de habla para evitar analizar silencios."""
    if gap_seconds < 0 or padding_seconds < 0:
        raise ValueError("gap/padding de regiones de habla inválidos")
    intervals = []
    for word in words:
        start = max(0.0, float(word.get("start", word.get("t_ini", 0.0))))
        end = max(start, float(word.get("end", word.get("t_fin", start))))
        if end > start:
            intervals.append((start, end))
    if not intervals:
        return []

    merged: list[list[float]] = []
    for start, end in sorted(intervals):
        if merged and start <= merged[-1][1] + gap_seconds:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])

    limit = float("inf") if duration is None else max(0.0, float(duration))
    padded: list[list[float]] = []
    for start, end in merged:
        region = [max(0.0, start - padding_seconds), min(limit, end + padding_seconds)]
        if region[1] <= region[0]:
            continue
        if padded and region[0] <= padded[-1][1]:
            padded[-1][1] = max(padded[-1][1], region[1])
        else:
            padded.append(region)
    return [(round(start, 3), round(end, 3)) for start, end in padded]


def _window_offsets(length: int, hop: int,
                    regions: list[tuple[float, float]] | None) -> list[int]:
    if regions is None:
        return list(range(0, max(1, length), hop))
    offsets: set[int] = set()
    for start_seconds, end_seconds in regions:
        start = max(0, int(math.floor(start_seconds * SAMPLE_RATE / hop)) * hop)
        end = min(length, max(start, int(math.ceil(end_seconds * SAMPLE_RATE))))
        offsets.update(range(start, end, hop))
    return sorted(offsets)


def extract_arousal(audio: str | Path, *, window_seconds: float = 4.0,
                    hop_seconds: float = 2.0, batch_size: int = 8,
                    speech_intervals: list[tuple[float, float]] | None = None,
                    cancel=None, log_cb=None, progress_cb=None) -> dict:
    if window_seconds <= 0 or hop_seconds <= 0 or hop_seconds > window_seconds:
        raise ValueError("ventana/hop de arousal inválidos")
    import audiocache
    import hardware
    import torch

    hardware.apply_torch_threads()
    waveform = np.asarray(audiocache.load(audio, sr=SAMPLE_RATE, mono=True), dtype=np.float32)
    duration = len(waveform) / SAMPLE_RATE
    window = max(1, int(round(window_seconds * SAMPLE_RATE)))
    hop = max(1, int(round(hop_seconds * SAMPLE_RATE)))
    starts = _window_offsets(len(waveform), hop, speech_intervals)
    analyzed_regions = ([list(region) for region in speech_intervals]
                        if speech_intervals is not None else [[0.0, round(duration, 3)]])
    if not starts:
        if log_cb:
            log_cb("Arousal acústico · sin regiones de habla; no hay inferencias que ejecutar")
        if progress_cb:
            progress_cb(1.0, None)
        return {"schema": "editorial-arousal/1", "model": AUDEERING,
                "window_seconds": window_seconds, "hop_seconds": hop_seconds,
                "scope": "speech-regions" if speech_intervals is not None else "full-audio",
                "analyzed_regions": analyzed_regions,
                "baseline": {"mean": 0.0, "std": 1.0}, "events": []}
    processor, model = _load_model()
    device = next(model.parameters()).device
    if log_cb:
        log_cb(f"Arousal acústico · {device.type.upper()} · ventanas de "
               f"{window_seconds:g}s/{hop_seconds:g}s")

    events: list[dict] = []
    for batch_start in range(0, len(starts), max(1, batch_size)):
        if cancel is not None and cancel.is_set():
            raise InterruptedError("arousal cancelado")
        offsets = starts[batch_start:batch_start + max(1, batch_size)]
        chunks = []
        valid_lengths = []
        for offset in offsets:
            chunk = waveform[offset:min(len(waveform), offset + window)]
            valid_lengths.append(len(chunk))
            if len(chunk) < window:
                chunk = np.pad(chunk, (0, window - len(chunk)))
            chunks.append(chunk)
        inputs = processor(chunks, sampling_rate=SAMPLE_RATE, padding=True,
                           return_tensors="pt")["input_values"].to(device)
        with torch.inference_mode():
            logits = model(inputs).detach().cpu().numpy()
        for offset, valid_length, values, chunk in zip(offsets, valid_lengths, logits, chunks):
            end = min(duration, (offset + valid_length) / SAMPLE_RATE)
            if end <= offset / SAMPLE_RATE:
                continue
            rms = float(np.sqrt(np.mean(np.square(chunk[:valid_length], dtype=np.float64))))
            events.append({
                "t_ini": round(offset / SAMPLE_RATE, 3),
                "t_fin": round(end, 3),
                "arousal": round(float(values[0]), 6),
                "dominance": round(float(values[1]), 6),
                "valence": round(float(values[2]), 6),
                "rms_dbfs": round(20 * math.log10(max(rms, 1e-9)), 3),
            })
        fraction = min(1.0, (batch_start + len(offsets)) / max(1, len(starts)))
        if progress_cb:
            progress_cb(fraction, None)
        if log_cb and (batch_start == 0 or batch_start + len(offsets) >= len(starts)
                       or batch_start % (max(1, batch_size) * 20) == 0):
            log_cb(f"Arousal… {fraction:.0%}")
    baseline = _zscore(events, "arousal", "arousal_z")
    return {"schema": "editorial-arousal/1", "model": AUDEERING,
            "window_seconds": window_seconds, "hop_seconds": hop_seconds,
            "scope": "speech-regions" if speech_intervals is not None else "full-audio",
            "analyzed_regions": analyzed_regions,
            "baseline": baseline, "events": events}


def _prefix_energy(waveform: np.ndarray) -> np.ndarray:
    squared = np.square(waveform, dtype=np.float64)
    return np.concatenate((np.zeros(1, dtype=np.float64), np.cumsum(squared)))


def _rms_db(prefix: np.ndarray, start: int, end: int) -> float:
    if end <= start:
        return -120.0
    energy = max(0.0, float(prefix[end] - prefix[start])) / (end - start)
    return 10 * math.log10(max(energy, 1e-12))


def extract_word_intensity(audio: str | Path, words: list[dict], *,
                           local_context_seconds: float = 0.35,
                           cancel=None, progress_cb=None) -> dict:
    import audiocache

    waveform = np.asarray(audiocache.load(audio, sr=SAMPLE_RATE, mono=True), dtype=np.float32)
    context = int(round(local_context_seconds * SAMPLE_RATE))
    events: list[dict] = []
    total = len(words)
    for index, word in enumerate(words):
        if cancel is not None and cancel.is_set():
            raise InterruptedError("intensidad cancelada")
        start_time = max(0.0, float(word.get("start", word.get("t_ini", 0.0))))
        end_time = max(start_time, float(word.get("end", word.get("t_fin", start_time))))
        start = min(len(waveform), max(0, int(round(start_time * SAMPLE_RATE))))
        end = min(len(waveform), max(start + 1, int(round(end_time * SAMPLE_RATE))))
        end = min(end, len(waveform))
        before_start, after_end = max(0, start - context), min(len(waveform), end + context)
        # Solo la palabra y su contexto: evita varios GB de prefijos float64
        # para podcasts de tres horas.
        prefix = _prefix_energy(waveform[before_start:after_end])
        local_start, local_end = start - before_start, end - before_start
        floor_parts = []
        if start > before_start:
            floor_parts.append(_rms_db(prefix, 0, local_start))
        if after_end > end:
            floor_parts.append(_rms_db(prefix, local_end, after_end - before_start))
        rms_dbfs = _rms_db(prefix, local_start, local_end)
        peak = float(np.max(np.abs(waveform[start:end]))) if end > start else 0.0
        peak_dbfs = 20 * math.log10(max(peak, 1e-9))
        local_floor = float(np.mean(floor_parts)) if floor_parts else -120.0
        events.append({
            "word_index": index,
            "t_ini": round(start_time, 3),
            "t_fin": round(end_time, 3),
            "rms_dbfs": round(rms_dbfs, 3),
            "peak_dbfs": round(peak_dbfs, 3),
            "local_floor_dbfs": round(local_floor, 3),
            "local_contrast_db": round(rms_dbfs - local_floor, 3),
            "duration": round(max(0.0, end_time - start_time), 3),
        })
        if progress_cb and (index % 500 == 0 or index + 1 == total):
            progress_cb((index + 1) / max(1, total), None)

    baseline = _zscore(events, "rms_dbfs", "intensity_z")
    durations = np.asarray([event["duration"] for event in events], dtype=np.float64)
    duration_mean = float(durations.mean()) if durations.size else 0.0
    duration_std = (float(durations.std()) or 1.0) if durations.size else 1.0
    for event in events:
        duration_z = (event["duration"] - duration_mean) / duration_std
        linear = 0.72 * event["intensity_z"] + 0.18 * duration_z \
            + 0.10 * min(3.0, max(-3.0, event["local_contrast_db"] / 6.0))
        event["emphasis_score"] = round(1.0 / (1.0 + math.exp(-linear)), 3)
    return {"schema": "editorial-intensity/1", "sample_rate": SAMPLE_RATE,
            "local_context_seconds": local_context_seconds, "baseline": baseline,
            "duration_baseline": {"mean": round(duration_mean, 6),
                                  "std": round(duration_std, 6)},
            "events": events}


def associate_arousal(words: list[dict], arousal_events: list[dict]) -> list[dict]:
    """Devuelve copias de las palabras enriquecidas con arousal ponderado por solape."""
    enriched: list[dict] = []
    first_candidate = 0
    for word in words:
        start = float(word.get("start", word.get("t_ini", 0.0)))
        end = float(word.get("end", word.get("t_fin", start)))
        while (first_candidate < len(arousal_events)
               and float(arousal_events[first_candidate]["t_fin"]) <= start):
            first_candidate += 1
        weight = value = value_z = 0.0
        cursor = first_candidate
        while cursor < len(arousal_events):
            event = arousal_events[cursor]
            event_start, event_end = float(event["t_ini"]), float(event["t_fin"])
            if event_start >= end:
                break
            overlap = max(0.0, min(end, event_end) - max(start, event_start))
            if overlap:
                weight += overlap
                value += overlap * float(event["arousal"])
                value_z += overlap * float(event["arousal_z"])
            cursor += 1
        copy = dict(word)
        if weight:
            copy["arousal"] = round(value / weight, 6)
            copy["arousal_z"] = round(value_z / weight, 3)
        else:
            copy["arousal"] = copy["arousal_z"] = None
        enriched.append(copy)
    return enriched


def enrich_words(words: list[dict], intensity: dict, arousal: dict) -> list[dict]:
    events = intensity.get("events") or []
    if len(events) != len(words):
        raise ValueError("la intensidad no corresponde 1:1 con words")
    merged = []
    for word, event in zip(words, events):
        copy = dict(word)
        for key in ("rms_dbfs", "peak_dbfs", "local_floor_dbfs", "local_contrast_db",
                    "intensity_z", "emphasis_score"):
            copy[key] = event[key]
        merged.append(copy)
    return associate_arousal(merged, arousal.get("events") or [])
