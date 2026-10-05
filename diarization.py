"""Local speaker diarization and word-level reconciliation with Whisper/MMS."""
from __future__ import annotations

from collections import defaultdict
from importlib.util import find_spec
import math
import os
from torch_worker import isolated


MODEL = "pyannote/speaker-diarization-community-1"
_PIPELINE = None


def available() -> bool:
    try:
        return find_spec("pyannote.audio") is not None
    except (ImportError, ValueError):
        return False


def unload() -> None:
    global _PIPELINE
    _PIPELINE = None


def check_access() -> None:
    """Fail before long transcription if the first model download needs credentials."""
    import hardware
    from huggingface_hub import get_token, try_to_load_from_cache
    token = os.environ.get("HF_TOKEN") or hardware.load().get("huggingface_token") or get_token()
    if not token and not isinstance(try_to_load_from_cache(MODEL, "config.yaml"), str):
        raise RuntimeError("Configura el token de lectura en Ajustes → Diarización local y acepta "
                           "las condiciones de Community-1 antes de procesar: "
                           "https://huggingface.co/pyannote/speaker-diarization-community-1")


def speaker_count(value) -> int | None:
    if value in (None, "", "auto", "Auto"):
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise ValueError("Número de hablantes inválido") from None
    if isinstance(value, bool) or str(number) != str(value) or not 1 <= number <= 32:
        raise ValueError("Número de hablantes: usa Auto o un entero entre 1 y 32")
    return number


@isolated
def detect(audio, *, num_speakers=None, cancel=None, log_cb=None, progress_cb=None) -> dict:
    """Return regular and exclusive speaker turns; inference stays on this computer."""
    global _PIPELINE
    check_access()
    import hardware
    import audiocache
    import torch
    os.environ.setdefault("PYANNOTE_METRICS_ENABLED", "false")
    from pyannote.audio import Pipeline

    def check_cancel():
        if cancel is not None and cancel.is_set():
            raise InterruptedError("diarización cancelada")

    check_cancel()
    number = speaker_count(num_speakers)
    if _PIPELINE is None:
        if log_cb:
            log_cb("Cargando diarización local pyannote Community-1…")
        token = os.environ.get("HF_TOKEN") or hardware.load().get("huggingface_token") or None
        try:
            _PIPELINE = Pipeline.from_pretrained(MODEL, token=token)
        except Exception:
            raise RuntimeError(
                "No se pudo cargar Community-1. Acepta sus condiciones en "
                "https://huggingface.co/pyannote/speaker-diarization-community-1 y configura "
                "un token de lectura de Hugging Face en Ajustes → Diarización local. "
                "Si ya lo hiciste, revisa la conexión y el acceso del token.") from None
        if _PIPELINE is None:
            raise RuntimeError("Community-1 no está disponible; revisa el acceso de Hugging Face")
        _PIPELINE.to(torch.device(hardware.torch_device()))
        if hardware.torch_device() == "cuda":
            _PIPELINE.segmentation_batch_size = 1
            _PIPELINE.embedding_batch_size = 1
    check_cancel()
    if log_cb:
        log_cb("Identificando quién habla en la pista…")
    # Decode with the existing audio cache: Windows does not need FFmpeg shared DLLs.
    waveform = torch.from_numpy(audiocache.load(audio, sr=16000).copy()).unsqueeze(0)

    def hook(step_name, _artifact=None, *, total=None, completed=None, **_kwargs):
        check_cancel()
        if progress_cb:
            progress_cb(min(0.95, completed / total) if total and completed is not None else 0.0)

    options = {"num_speakers": number} if number is not None else {}
    output = _PIPELINE({"waveform": waveform, "sample_rate": 16000}, hook=hook, **options)
    check_cancel()

    def turns(annotation):
        return [{"start": round(turn.start, 3), "end": round(turn.end, 3), "speaker_id": speaker}
                for turn, _, speaker in annotation.itertracks(yield_label=True)]

    regular = turns(output.speaker_diarization)
    exclusive = turns(output.exclusive_speaker_diarization)
    if not exclusive:
        raise RuntimeError("La diarización no detectó hablantes; no se publicó una salida sin identificar")
    if progress_cb:
        progress_cb(1.0)
    if log_cb:
        log_cb(f"Diarización lista: {len({turn['speaker_id'] for turn in regular})} hablantes detectados.")
    return {"schema": "transcriptor-diarization/1", "model": MODEL,
            "num_speakers": number, "turns": regular, "exclusive_turns": exclusive}


def assign_speakers(words: list[dict], segments: list[dict], document: dict,
                    *, max_gap: float = 0.5) -> tuple[list[dict], list[dict]]:
    """Assign each word once and split phrases at speaker changes without changing times."""
    turns = sorted(document.get("exclusive_turns") or document.get("turns") or [],
                   key=lambda turn: (float(turn["start"]), float(turn["end"])))
    for turn in turns:
        start, end = float(turn["start"]), float(turn["end"])
        if not math.isfinite(start + end) or end <= start or not turn.get("speaker_id"):
            raise ValueError("Turno de diarización inválido")
    assigned = []
    cursor = 0
    for source in words:
        word = dict(source)
        start, end = float(word["start"]), float(word["end"])
        midpoint = (start + end) / 2
        while cursor < len(turns) and float(turns[cursor]["end"]) < start - max_gap:
            cursor += 1
        scores = defaultdict(float)
        nearest = None
        index = cursor
        while index < len(turns) and float(turns[index]["start"]) <= end + max_gap:
            turn = turns[index]
            left, right = float(turn["start"]), float(turn["end"])
            overlap = max(0.0, min(end, right) - max(start, left))
            scores[turn["speaker_id"]] += overlap
            distance = max(left - midpoint, midpoint - right, 0.0)
            if nearest is None or distance < nearest[0]:
                nearest = distance, turn["speaker_id"]
            index += 1
        best = max(scores, key=scores.get) if scores else None
        if best is not None and scores[best] > 0:
            word["speaker_id"] = best
            word["speaker_assignment"] = "overlap"
        elif nearest is not None and nearest[0] <= max_gap:
            word["speaker_id"] = nearest[1]
            word["speaker_assignment"] = "nearest"
        else:
            word["speaker_id"] = None
            word["speaker_assignment"] = "unknown"
        assigned.append(word)

    # Preserve original phrase boundaries as well as speaker changes. Membership uses
    # each word's midpoint, so words crossing a phrase boundary are never duplicated.
    split = []
    segment_index = 0
    last_group = None
    populated_segments = set()
    for index, word in enumerate(assigned):
        midpoint = (float(word["start"]) + float(word["end"])) / 2
        while segment_index + 1 < len(segments) and midpoint >= float(segments[segment_index]["end"]):
            segment_index += 1
        group = (segment_index, word["speaker_id"])
        populated_segments.add(segment_index)
        if group != last_group:
            split.append({"id": len(split), "start": word["start"], "end": word["end"],
                          "speaker_id": word["speaker_id"], "text": "", "words": [],
                          "word_indices": []})
            last_group = group
        phrase = split[-1]
        phrase["end"] = max(phrase["end"], word["end"])
        phrase["words"].append(word)
        phrase["word_indices"].append(index)
        phrase["text"] = " ".join(item["word"] for item in phrase["words"]).strip()
    for index, segment in enumerate(segments):
        if index not in populated_segments and segment.get("text", "").strip():
            split.append({**segment, "speaker_id": None, "words": [], "word_indices": []})
    split.sort(key=lambda segment: (float(segment["start"]), float(segment["end"])))
    for index, segment in enumerate(split):
        segment["id"] = index
    return assigned, split
