"""
gate.py — Gate/ducker de respiraciones dirigido por transcripción.

Dos modos:
  · Avanzado (word-based): bordes de palabra de whisper + márgenes/heurísticas
    (pre/post/min_gap/short) → 7 ajustes. Compensa la imprecisión de whisper en los bordes.
  · VAD (Silero × whisper): fronteras acústicas exactas (~30 ms). Un segmento de voz del
    VAD se conserva sólo si contiene una palabra de whisper → las respiraciones (que
    disparan el VAD pero no tienen palabra) se gatean. Elimina pre/post/min_gap/short.

En ambos: no cambia la duración → el mismo words.json sigue sirviendo para el sync.
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import soundfile as sf

GATE_EXTS = [".wav", ".flac", ".ogg", ".aiff", ".mp3"]


# --------------------------------------------------------------------- helpers --
def find_words_json(audio) -> Path | None:
    audio = Path(audio)
    cand = audio.with_name(audio.stem + ".words.json")
    return cand if cand.exists() else None


def default_output(audio) -> Path:
    audio = Path(audio)
    return audio.with_name(audio.stem + "_gated" + audio.suffix)


def db_to_gain(db: float) -> float:
    return 10.0 ** (db / 20.0)


def load_words(words_json) -> list[dict]:
    words = json.loads(Path(words_json).read_text(encoding="utf-8"))
    if not words:
        raise RuntimeError("El words.json está vacío.")
    return words


def load_breaths(breaths_json) -> list[dict]:
    """Lee un breaths.json de detect_breaths.py: [{"start","end", ...}, ...]."""
    return json.loads(Path(breaths_json).read_text(encoding="utf-8"))


def vad_available() -> bool:
    try:
        import silero_vad  # noqa: F401
        import torch  # noqa: F401
        return True
    except Exception:
        return False


# --------------------------------------------------- gaps: modo word-based -----
def _find_all_gaps(words, pre, post, total):
    raw = sorted((max(0.0, w["start"] - pre), min(total, w["end"] + post)) for w in words)
    merged = [list(raw[0])]
    for start, end in raw[1:]:
        if start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    gaps = []
    cursor = 0.0
    for start, end in merged:
        if start - cursor >= 0.05:
            gaps.append({"start": round(cursor, 3), "end": round(start, 3)})
        cursor = end
    if total - cursor >= 0.05:
        gaps.append({"start": round(cursor, 3), "end": round(total, 3)})
    return gaps


def _annotate(gaps, words, min_gap, floor_db, short_floor_db):
    for g in gaps:
        g["dur"] = round(g["end"] - g["start"], 2)
        before = [w for w in words if w["end"] <= g["start"] + 0.01]
        after = [w for w in words if w["start"] >= g["end"] - 0.01]
        g["despues_de"] = " ".join(w["word"] for w in before[-4:]) if before else "(inicio)"
        g["antes_de"] = " ".join(w["word"] for w in after[:4]) if after else "(fin)"
        g["tipo"] = "largo" if g["dur"] >= min_gap else "corto"
        g["gain_db"] = floor_db if g["tipo"] == "largo" else short_floor_db
    return gaps


# ------------------------------------------------ gaps: modo quirúrgico (Respiro) --
def breath_gaps(breaths, words, total, min_keep=0.05, use_shield=True, word_shrink=0.0):
    """Modo quirúrgico (Respiro-en × whisper). Toma cada intervalo de respiración
    que detectó la red y decide qué atenuar según el escudo de palabras:

      · use_shield=False → se atenúa TODA respiración detectada (whisper no protege
        nada). Máxima limpieza; riesgo: si la red marca una fricativa dentro de una
        palabra, se ataca. Se compensa subiendo el threshold de detección.
      · use_shield=True  → a cada respiración se le RESTAN las palabras de whisper
        que la solapan y se conservan los trozos libres (≥ `min_keep`). Antes de
        comparar, cada palabra se ENCOGE `word_shrink` s hacia adentro (start+shrink,
        end-shrink): whisper suele "estirar" el final de una palabra sobre el aire,
        así que encoger libera las respiraciones escondidas en ese padding y sigue
        protegiendo el núcleo del habla.

    Nunca se atenúa un sample dentro de una palabra (ya encogida) → no se toca el
    habla real. Devuelve (gaps, n_respiraciones_descartadas_por_completo)."""
    if use_shield:
        word_iv = sorted((w["start"] + word_shrink, w["end"] - word_shrink) for w in words)
        word_iv = [(ws, we) for ws, we in word_iv if we > ws]   # descartar palabras que colapsan
    else:
        word_iv = []

    gaps, dropped = [], 0
    for b in breaths:
        s, e = max(0.0, float(b["start"])), min(total, float(b["end"]))
        if e <= s:
            continue
        if not word_iv:                       # sin escudo (o sin palabras) → mutear entero
            gaps.append({"start": round(s, 3), "end": round(e, 3)})
            continue
        free = [(s, e)]
        for ws, we in word_iv:
            if we <= s or ws >= e:            # palabra fuera de esta respiración
                continue
            nxt = []
            for fs, fe in free:
                if we <= fs or ws >= fe:      # no toca este trozo
                    nxt.append((fs, fe)); continue
                if ws > fs:                   # queda un trozo libre antes de la palabra
                    nxt.append((fs, min(ws, fe)))
                if we < fe:                   # queda un trozo libre después de la palabra
                    nxt.append((max(we, fs), fe))
            free = nxt
        kept = [(fs, fe) for fs, fe in free if fe - fs >= min_keep]
        if not kept:
            dropped += 1
            continue
        for fs, fe in kept:
            gaps.append({"start": round(fs, 3), "end": round(fe, 3)})
    return gaps, dropped


# ---------------------------------------- red de seguridad espectral (sin IA) --
def spectral_breath_gaps(data, sr, words, total, *, flatness_thr=0.04,
                         rms_floor_db=-45.0, min_dur=0.08, gap_margin=0.05, min_gap=0.20):
    """Red de seguridad DSP (sin red neuronal): caza ruido de aire que Respiro no ve.
    Sólo actúa DENTRO de los huecos entre palabras de whisper (con margen), y sólo
    marca zonas que cumplen LAS DOS cosas: energía sobre el piso (`rms_floor_db`) Y
    planitud espectral suavizada alta (`flatness_thr` → ruido broadband, no voz). Así
    una pausa callada (room tone bajo el piso) o un resto de voz (tonal) NUNCA se tocan
    → no recrea el problema del modo avanzado de gatear pausas normales.

    Trabaja a 16 kHz (resamplea una vez): la planitud es sensible al sample rate — a
    44.1 kHz las frecuencias altas vacías la arrastran hacia abajo. A 16 kHz el aire
    da ~0.10 y la voz ~0.005 → separación limpia. Devuelve [{"start","end"}, ...]."""
    import librosa
    mono = data.mean(axis=1) if getattr(data, "ndim", 1) > 1 else np.asarray(data)
    m16 = librosa.resample(np.ascontiguousarray(mono, dtype=np.float32),
                           orig_sr=sr, target_sr=16000)
    sr16, frame = 16000, int(0.03 * 16000)
    win = np.hanning(frame)

    gaps, cur = [], 0.0
    for w in sorted(words, key=lambda x: x["start"]):
        if w["start"] - cur >= min_gap:
            gaps.append((cur, w["start"]))
        cur = max(cur, w["end"])
    if total - cur >= min_gap:
        gaps.append((cur, total))

    out = []
    for g0, g1 in gaps:
        a, b = g0 + gap_margin, g1 - gap_margin
        if b - a < min_dur:
            continue
        times = np.arange(a, b, 0.01)                 # frames cada 10 ms
        fl = np.zeros(len(times)); rz = np.full(len(times), -120.0)
        for j, t in enumerate(times):
            i0 = int(t * sr16); ch = m16[i0:i0 + frame]
            if len(ch) < frame:
                continue
            spec = np.abs(np.fft.rfft(ch * win)) ** 2 + 1e-10
            fl[j] = np.exp(np.mean(np.log(spec))) / np.mean(spec)
            rz[j] = 20 * np.log10(max(float(np.sqrt(np.mean(ch ** 2))), 1e-9))
        if len(fl) >= 5:                              # suavizar ~50 ms (mata picos aislados de voz)
            fl = np.convolve(fl, np.ones(5) / 5, mode="same")
        mask = (fl > flatness_thr) & (rz > rms_floor_db)
        run = None
        for j, ok in enumerate(mask):
            if ok:
                t = times[j]; run = [t, t + 0.01] if run is None else [run[0], t + 0.01]
            elif run is not None:
                if run[1] - run[0] >= min_dur:
                    out.append({"start": round(run[0], 3), "end": round(min(run[1], b), 3)})
                run = None
        if run is not None and run[1] - run[0] >= min_dur:
            out.append({"start": round(run[0], 3), "end": round(min(run[1], b), 3)})
    return out


def _merge_gaps(gaps):
    """Fusiona una lista de {start,end} solapados (mismo criterio que el resto)."""
    gaps = sorted(gaps, key=lambda g: g["start"])
    merged = []
    for g in gaps:
        if merged and g["start"] <= merged[-1]["end"]:
            merged[-1]["end"] = max(merged[-1]["end"], g["end"])
        else:
            merged.append({"start": g["start"], "end": g["end"]})
    return merged


# --------------------------------------------------------- gaps: modo VAD ------
def vad_speech_gaps(data, sr, words, total, margin=0.04):
    """Silero VAD validado contra las palabras de whisper. Devuelve (gaps, dropped)."""
    import torch
    from silero_vad import load_silero_vad, get_speech_timestamps
    import hardware

    hardware.apply_torch_threads()   # hilos según config global (Silero corre en CPU: es diminuto)
    mono = data.mean(axis=1).astype(np.float32)
    if sr != 16000:  # resample lineal → suficiente para el VAD
        n16 = int(len(mono) * 16000 / sr)
        mono = np.interp(np.linspace(0, len(mono) - 1, n16),
                         np.arange(len(mono)), mono).astype(np.float32)
    wav = torch.from_numpy(mono)

    model = load_silero_vad()
    segs = get_speech_timestamps(wav, model, sampling_rate=16000, return_seconds=True,
                                 min_silence_duration_ms=250, speech_pad_ms=30)
    word_iv = [(w["start"], w["end"]) for w in words]

    kept, dropped = [], []
    for seg in segs:
        s, e = float(seg["start"]), float(seg["end"])
        if any(ws < e and we > s for ws, we in word_iv):   # contiene alguna palabra
            kept.append([max(0.0, s - margin), min(total, e + margin)])
        else:                                               # respiración/ruido → gatear
            dropped.append([s, e])

    # garantizar que toda palabra quede cubierta aunque el VAD la perdiera
    for ws, we in word_iv:
        if not any(k[0] <= ws and we <= k[1] for k in kept):
            kept.append([max(0.0, ws - 0.08), min(total, we + 0.15)])
    kept.sort()
    speech = [list(kept[0])] if kept else []
    for s, e in kept[1:]:
        if s <= speech[-1][1] + 0.05:
            speech[-1][1] = max(speech[-1][1], e)
        else:
            speech.append([s, e])

    gaps = []
    cursor = 0.0
    for s, e in speech:
        if s - cursor >= 0.05:
            gaps.append({"start": round(cursor, 3), "end": round(s, 3)})
        cursor = e
    if total - cursor >= 0.05:
        gaps.append({"start": round(cursor, 3), "end": round(total, 3)})
    return gaps, dropped


# --------------------------------------------------------- energía por hueco ---
def _gap_energy(data, sr, gaps):
    """RMS del audio original en cada hueco → marca cuáles tienen sonido audible."""
    mono = data.mean(axis=1)
    for g in gaps:
        i0, i1 = int(g["start"] * sr), min(int(g["end"] * sr), len(mono))
        chunk = mono[i0:i1]
        if len(chunk) == 0:
            g["rms_db"] = -120.0; g["audible"] = False; continue
        rms = float(np.sqrt(np.mean(chunk ** 2)))
        g["rms_db"] = round(20 * np.log10(max(rms, 1e-6)), 1)
        g["audible"] = g["rms_db"] > -45.0
    return gaps


# ------------------------------------------------------------------- envelope --
def _build_envelope(n, sr, gaps, attack, release):
    env = np.ones(n, dtype=np.float32)
    att_n = max(1, int(attack * sr))
    rel_n = max(1, int(release * sr))
    for g in gaps:
        gain = db_to_gain(g["gain_db"])
        if gain >= 0.999:
            continue
        i0, i1 = int(g["start"] * sr), min(int(g["end"] * sr), n)
        span = i1 - i0
        if span <= 0:
            continue
        rel = min(rel_n, span // 2)
        att = min(att_n, span - rel)
        if rel > 0:
            env[i0:i0 + rel] = np.minimum(env[i0:i0 + rel],
                                          np.linspace(1.0, gain, rel, dtype=np.float32))
        env[i0 + rel:i1 - att] = np.minimum(env[i0 + rel:i1 - att], gain)
        if att > 0:
            env[i1 - att:i1] = np.minimum(env[i1 - att:i1],
                                          np.linspace(gain, 1.0, att, dtype=np.float32))
    return env


# ------------------------------------------------------- cálculo de huecos -----
def _compute(data, sr, words, total, *, use_vad, pre, post, min_gap,
             floor_db, short_floor_db, breaths=None, use_shield=True, word_shrink=0.0,
             spectral_net=False, flatness_thr=0.04, log_cb=None):
    if breaths is not None:
        if log_cb:
            escudo = (f"escudo ON (encogido {word_shrink*1000:.0f}ms)" if use_shield
                      else "escudo OFF (mutea todo)")
            log_cb(f"Modo quirúrgico: respiraciones de Respiro-en · {escudo}…")
        gaps, dropped = breath_gaps(breaths, words, total,
                                    use_shield=use_shield, word_shrink=word_shrink)
        n_net = 0
        if spectral_net:
            extra = spectral_breath_gaps(data, sr, words, total, flatness_thr=flatness_thr)
            n_net = len(extra)
            gaps = _merge_gaps(gaps + extra)     # sumar el aire hallado en las pausas
        # toda respiración confirmada usa un solo piso (sin heurística de largo/corto)
        gaps = _annotate(gaps, words, min_gap=0.0, floor_db=floor_db, short_floor_db=floor_db)
        if log_cb:
            extra_txt = f" · red de seguridad: +{n_net} zona(s) de aire en pausas" if spectral_net else ""
            log_cb(f"Respiro: {len(gaps)} zona(s) a atenuar · "
                   f"{dropped} descartada(s) por solapar palabras{extra_txt}.")
    elif use_vad:
        if not vad_available():
            raise RuntimeError("Modo VAD no disponible: falta silero-vad/torch. "
                               "Instalá con:  pip install silero-vad")
        if log_cb:
            log_cb("Detectando fronteras acústicas con Silero VAD…")
        gaps, dropped = vad_speech_gaps(data, sr, words, total)
        # en VAD todo hueco es no-voz confirmada → un solo piso, sin heurística
        gaps = _annotate(gaps, words, min_gap=0.0, floor_db=floor_db, short_floor_db=floor_db)
        if log_cb:
            log_cb(f"VAD: {len(dropped)} segmento(s) de no-voz pura (respiraciones) detectados.")
    else:
        gaps = _find_all_gaps(words, pre, post, total)
        gaps = _annotate(gaps, words, min_gap, floor_db, short_floor_db)
    return _gap_energy(data, sr, gaps)


# --------------------------------------------------------------------- public --
def analyze(audio, words_json, *, use_vad=False, breaths_json=None,
            use_shield=True, word_shrink=0.0, spectral_net=False, flatness_thr=0.04,
            pre=0.08, post=0.22, min_gap=0.4,
            floor_db=-26.0, short_floor_db=0.0, **_ignore):
    """Detecta y devuelve (gaps anotados, duración) sin escribir audio."""
    words = load_words(words_json)
    breaths = load_breaths(breaths_json) if breaths_json else None
    data, sr = sf.read(str(Path(audio)), always_2d=True)
    total = len(data) / sr
    gaps = _compute(data, sr, words, total, use_vad=use_vad, breaths=breaths,
                    use_shield=use_shield, word_shrink=word_shrink,
                    spectral_net=spectral_net, flatness_thr=flatness_thr, pre=pre,
                    post=post, min_gap=min_gap, floor_db=floor_db, short_floor_db=short_floor_db)
    return gaps, total


def process(audio, words_json, out, *, use_vad=False, breaths_json=None,
            use_shield=True, word_shrink=0.0, spectral_net=False, flatness_thr=0.04,
            pre=0.08, post=0.22, min_gap=0.4, attack=0.02, release=0.25,
            floor_db=-26.0, short_floor_db=0.0, log_cb=None) -> dict:
    """Detecta huecos y aplica el gate/ducker. Devuelve un reporte."""
    audio, out = Path(audio), Path(out)

    def log(m):
        if log_cb:
            log_cb(m)

    words = load_words(words_json)
    breaths = load_breaths(breaths_json) if breaths_json else None
    log(f"Leyendo {audio.name}…")
    data, sr = sf.read(str(audio), always_2d=True)
    total = len(data) / sr

    gaps = _compute(data, sr, words, total, use_vad=use_vad, breaths=breaths,
                    use_shield=use_shield, word_shrink=word_shrink,
                    spectral_net=spectral_net, flatness_thr=flatness_thr, pre=pre,
                    post=post, min_gap=min_gap, floor_db=floor_db,
                    short_floor_db=short_floor_db, log_cb=log)

    log("Construyendo envolvente y aplicando…")
    env = _build_envelope(len(data), sr, gaps, attack, release)
    sf.write(str(out), data * env[:, None], sr)

    touched = [g for g in gaps if g["gain_db"] < -0.1]
    muted_total = sum(g["dur"] for g in touched)
    modo = ("quirúrgico (Respiro-en × whisper)" if breaths is not None
            else "VAD (Silero × whisper)" if use_vad else "word-based")
    log(f"Duración: {total:.2f}s (sin cambios) · modo {modo} · huecos tratados: "
        f"{len(touched)}/{len(gaps)} · floor {floor_db:.0f}dB · release {release*1000:.0f}ms")
    for g in touched:
        aud = " ♪audible" if g.get("audible") else ""
        log(f"  [{g['start']:7.2f}s → {g['end']:7.2f}s] {g['gain_db']:+.0f}dB{aud} "
            f"…{g['despues_de']} | {g['antes_de']}…")

    return {"out": str(out), "duration": total, "gaps": gaps, "touched": len(touched),
            "muted_total": muted_total, "use_vad": use_vad,
            "use_breaths": breaths is not None, "modo": modo}
