#!/usr/bin/env python3
"""
align.py — Corrige los timestamps de palabra de whisper con ALINEACIÓN FORZADA.

whisper transcribe bien las palabras pero sus tiempos son flojos (a veces pega
varias palabras + silencio + respiración en una sola "palabra" de varios
segundos). Eso rompe la detección de respiraciones (el respiro queda "dentro" de
una palabra en vez de en un hueco). Este módulo re-alinea el texto de whisper
contra el audio a nivel acústico usando el modelo MMS de torchaudio (multilingüe,
soporta español, corre en CPU) → bordes de palabra precisos (~20 ms).

Beneficio doble: además de arreglar la detección de respiraciones, deja los
timestamps precisos para sincronizar motion-graphics.

No agrega dependencias: usa torchaudio (ya instalado). Descarga el modelo MMS
(~1.2 GB) la primera vez, a ~/.cache/torch.

API:  align_words(audio, words, log_cb=None) -> words con start/end corregidos.
"""
from __future__ import annotations

import unicodedata

_MODEL = _TOK = _ALN = None


def unload():
    """Suelta el modelo MMS cacheado (liberar RAM/VRAM)."""
    global _MODEL, _TOK, _ALN
    _MODEL = _TOK = _ALN = None


def available() -> bool:
    # find_spec en vez de importar: torchaudio arrastra torch (~2 s) y la GUI llama
    # esto al construirse (arranque — 2026-07-20). Los atributos forced_align/MMS_FA
    # existen en todo torchaudio >= 2.1 (pin del proyecto); si faltaran, _load()
    # falla con error claro al alinear.
    from importlib.util import find_spec
    try:
        return find_spec("torchaudio") is not None
    except Exception:
        return False


def _load():
    global _MODEL, _TOK, _ALN
    if _MODEL is None:
        import torchaudio
        import hardware
        if not hasattr(torchaudio.pipelines, "MMS_FA") or not hasattr(torchaudio.functional, "forced_align"):
            raise RuntimeError("MMS necesita torch/torchaudio 2.8.0; repara la instalación.")
        b = torchaudio.pipelines.MMS_FA
        model, _dev = hardware.load_model_safe(lambda d: b.get_model().to(d))
        _MODEL, _TOK, _ALN = model, b.get_tokenizer(), b.get_aligner()
    return _MODEL, _TOK, _ALN


def _norm(word: str) -> str:
    """Normaliza a la tabla del MMS: minúsculas, sin acentos (ñ→n), solo [a-z']."""
    w = unicodedata.normalize("NFKD", word)
    w = "".join(c for c in w if not unicodedata.combining(c))
    return "".join(c for c in w.lower() if ("a" <= c <= "z") or c == "'")


def align_words(audio, words, *, batch=120, margin=0.5, log_cb=None,
                progress_cb=None, cancel=None) -> list[dict]:
    """Devuelve una copia de `words` con start/end re-alineados. Procesa por lotes
    de `batch` palabras (ventana de audio acotada → memoria y CPU controladas).
    Las palabras que no se pueden alinear (números, puntuación) se interpolan entre
    vecinas. Si un lote falla, conserva los tiempos originales de ese lote.
    `progress_cb(frac 0..1, eta_seg|None)` se llama por lote."""
    import time
    import torch
    import hardware

    # Hilos de CPU y dispositivo salen de la config global (pestaña Ajustes): por defecto
    # TODOS los hilos lógicos (torch de fábrica usa un nº conservador). Este paso es
    # CPU/GPU-bound y pesado, saturar la CPU acelera mucho. (set_num_interop NO se toca:
    # puede tirar si torch ya hizo trabajo paralelo en el proceso.)
    hardware.apply_torch_threads()

    model, tok, aln = _load()
    dev = next(model.parameters()).device
    if log_cb:
        log_cb(f"Alineador MMS en {dev.type.upper()} · usando {torch.get_num_threads()} hilos.")
    import audiocache
    wav = audiocache.load(audio, sr=16000)          # caché compartida (evita re-decodificar)
    total = len(wav) / 16000.0
    out: list[dict] = []
    n_words = len(words)
    t_start = time.time()

    i = 0
    while i < len(words):
        if cancel is not None and cancel.is_set():
            raise InterruptedError("alineación MMS cancelada")
        grp = words[i:i + batch]
        # Un lote contado por palabras podía abarcar minutos de silencio.
        while len(grp) > 1 and grp[-1]["end"] - grp[0]["start"] > 30.0:
            grp = grp[:-1]
        consumed = len(grp)
        norms = [_norm(w["word"]) for w in grp]
        idxs = [k for k, n in enumerate(norms) if n]        # posiciones alineables
        if not idxs:
            out.extend({**w, "alignment_source": "whisper_unalignable"} for w in grp)
            i += consumed
            continue

        t0 = max(0.0, grp[0]["start"] - margin)
        t1 = min(total, grp[-1]["end"] + margin)
        if t1 - t0 > 32.0:
            raise RuntimeError(f"Palabra con duración anómala en {t0:.3f}s; revisa la transcripción.")
        seg = torch.tensor(wav[int(t0 * 16000):int(t1 * 16000)]).unsqueeze(0).to(dev)
        trans = [norms[k] for k in idxs]

        newtimes: dict[int, tuple] = {}
        try:
            with torch.inference_mode():
                emission, _ = model(seg)
            emission = emission.cpu()   # el aligner trabaja en CPU
            spans = aln(emission[0], tok(trans))
            ratio = seg.size(1) / emission.size(1) / 16000.0
            for j, k in enumerate(idxs):
                s = spans[j]
                newtimes[k] = (round(s[0].start * ratio + t0, 3),
                               round(s[-1].end * ratio + t0, 3))
        except Exception as e:
            if log_cb:
                log_cb(f"(lote {i}: alineación falló, conservo tiempos de whisper — {e})")

        for k, w in enumerate(grp):
            nw = dict(w)
            if k in newtimes:
                nw["start"], nw["end"] = newtimes[k]
                nw["alignment_source"] = "mms"
            elif newtimes:                                  # interpolar no-alineable
                prev = max([x for x in newtimes if x < k], default=None)
                nxt = min([x for x in newtimes if x > k], default=None)
                if prev is not None:
                    nw["start"] = newtimes[prev][1]
                    nw["end"] = round((newtimes[prev][1] + (newtimes[nxt][0] if nxt is not None
                                       else newtimes[prev][1] + 0.1)) / 2, 3)
                    nw["alignment_source"] = "mms_interpolated"
                else:
                    nw["alignment_source"] = "whisper_unalignable"
            else:
                nw["alignment_source"] = "whisper_fallback"
            out.append(nw)

        i += consumed
        done = min(i, n_words)
        if progress_cb:
            frac = done / n_words
            el = time.time() - t_start
            eta = (el / frac - el) if frac > 0.05 else None
            progress_cb(frac, eta)
        if log_cb:
            log_cb(f"Alineando timestamps… {done}/{n_words} palabras")
    return out
