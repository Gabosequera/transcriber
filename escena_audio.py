#!/usr/bin/env python3
"""
escena_audio.py — Análisis de una pista de audio GENÉRICA (el juego, un video que
estés viendo, lo que sea) para el auto-clipping. NO es tu voz: es la "otra" pista.

Tres capas (esta primera entrega = Capa 1):

  · CAPA 1 — ETIQUETADO (este módulo, `tag()`): "qué suena", con timestamps.
    Clasificador AST entrenado en AudioSet (527 clases genéricas → sirve para
    cualquier audio, no hay que preparar nada por juego). Colapsamos esas 527 en
    ~18 FAMILIAS útiles para clips (disparo, explosión, música, motor, vidrio,
    alarma, risa, grito, multitud, agua…). Vocabulario CERRADO → los tags siempre
    caen dentro de etiquetas que elegimos nosotros (candado #1 contra descripciones
    que se van de rango).

  · CAPA 2 — SEGMENTACIÓN por escena (a futuro): cortar la timeline donde el CARÁCTER
    del sonido cambia (deriva de los tags + RMS), en vez de ventanas fijas.

  · CAPA 3 — DESCRIPCIÓN (a futuro, backend enchufable: LALM grande en CPU/batch, o
    captioner chico en GPU): describe cada segmento de la Capa 2, con los tags de la
    Capa 1 como contexto y salida estructurada → la descripción queda anclada a los
    tags (candado #2). Los tags son la verdad; la descripción agrega color.

Sobre las ventanas (por qué NO 1s): el AST tiene positional embeddings fijos de
~10.24s → SIEMPRE procesa una ventana de ~10s (si le das menos, rellena con silencio
y diluye la lectura). Así que la ventana base es ~10s deslizándose con hop corto
(lectura de textura/escena estable cada `hop` s). Los transitorios secos (un disparo
de 200ms) necesitarán un pase fino guiado por energía — Capa 1.5, aún no acá.

Formato de salida consistente con el resto ({t_ini,t_fin,tipo,...}) para el master.json.
Requiere (aparte del core): transformers (<5), torch (CPU), librosa, numpy. No toca el
core (whisper usa ctranslate2, no transformers).
"""
from __future__ import annotations

import numpy as np

AST_MODEL = "MIT/ast-finetuned-audioset-10-10-0.4593"

# 527 clases de AudioSet → ~18 familias útiles. Cada familia matchea por substring
# (case-insensitive) contra los nombres de clase del modelo; la prob de la familia es
# el MAX de sus clases miembro (una explosión puede caer en «Explosion» o «Boom»).
FAMILIAS = {
    "habla":        ["speech", "conversation", "narration", "monologue"],
    "musica":       ["music"],
    "disparo":      ["gunshot", "gunfire", "machine gun", "artillery", "fusillade", "cap gun"],
    "explosion":    ["explosion", "boom", "eruption", "burst"],
    "vehiculo":     ["vehicle", "car", "truck", "motorcycle", "aircraft", "helicopter", "race car"],
    "motor":        ["engine", "idling", "accelerating", "revving"],
    "vidrio_impacto": ["glass", "shatter", "smash", "crash", "breaking", "crushing"],
    "alarma":       ["alarm", "siren", "buzzer", "beep", "bleep", "fire alarm"],
    "risa":         ["laughter", "giggle", "chuckle", "chortle", "snicker"],
    "grito":        ["screaming", "shout", "yell", "bellow", "children shouting"],
    "multitud":     ["cheering", "applause", "crowd", "clapping", "chatter"],
    "agua":         ["water", "rain", "stream", "waves", "splash", "gurgling"],
    "viento":       ["wind", "rustling"],
    "fuego":        ["fire", "crackle", "flame"],
    "pasos":        ["walk, footsteps", "footsteps", "run"],
    "animal":       ["animal", "dog", "cat", "bird", "growling", "roar"],
    "silencio":     ["silence"],
}
# Familias "secas"/transitorias: eventos cortos donde ~10s es demasiado grueso →
# candidatas al pase fino guiado por energía (Capa 1.5, aún no implementada).
TRANSITORIAS = {"disparo", "explosion", "vidrio_impacto"}

_MODEL = None   # (feature_extractor, model, fam_idx)


def unload():
    """Suelta el modelo AST cacheado (liberar RAM/VRAM)."""
    global _MODEL
    _MODEL = None


def available() -> bool:
    # find_spec en vez de importar (arranque de la GUI — ver metadata.available)
    from importlib.util import find_spec
    try:
        return all(find_spec(m) is not None
                   for m in ("transformers", "torch", "librosa"))
    except Exception:
        return False


def _build_fam_index(id2label: dict) -> dict:
    """Mapea cada familia → lista de índices de clase de AudioSet que la componen."""
    fam_idx = {f: [] for f in FAMILIAS}
    for idx, name in id2label.items():
        low = name.lower()
        for fam, keys in FAMILIAS.items():
            if any(k in low for k in keys):
                fam_idx[fam].append(int(idx))
    return {f: ix for f, ix in fam_idx.items() if ix}   # descartar familias sin match


def _load():
    global _MODEL
    if _MODEL is None:
        import hardware
        from transformers import ASTForAudioClassification, ASTFeatureExtractor
        fe = ASTFeatureExtractor.from_pretrained(AST_MODEL)
        model, _dev = hardware.load_model_safe(
            lambda d: ASTForAudioClassification.from_pretrained(AST_MODEL).eval().to(d))
        fam_idx = _build_fam_index(model.config.id2label)
        _MODEL = (fe, model, fam_idx)
    return _MODEL


def tag(audio, *, win=10.0, hop=2.0, smooth=3, threshold=0.35,
        cancel=None, log_cb=None, progress_cb=None) -> dict:
    """CAPA 1. Devuelve {"events":[...], "familias":[...], "params":{...}}.

    Desliza ventanas de ~`win` s (nativas del AST) con paso `hop`, saca la prob por
    familia (sigmoide → multi-etiqueta), arma un timeline por familia a resolución
    `hop`, lo suaviza (mediana de `smooth` ventanas) y umbraliza → corridas contiguas
    = eventos {t_ini,t_fin,tipo:"sonido",familia,conf}. conf = prob máxima en la corrida.
    """
    import torch
    import librosa
    import hardware

    hardware.apply_torch_threads()   # hilos según config global (default: todos)

    def log(m):
        if log_cb:
            log_cb(m)

    fe, model, fam_idx = _load()
    dev = next(model.parameters()).device
    log(f"AST cargado · {len(fam_idx)} familias activas · ventana {win:.0f}s / hop {hop:.0f}s "
        f"· {dev.type.upper()} · {torch.get_num_threads()} hilos CPU")
    import audiocache
    wav = audiocache.load(audio, sr=16000)          # caché compartida (evita re-decodificar)
    dur = len(wav) / 16000.0
    fams = list(fam_idx.keys())

    centers, probs = [], []           # probs[i] = vector de prob por familia (en fams)
    t = 0.0
    starts = list(np.arange(0.0, max(dur - 0.5, 0.01), hop))
    for k, t in enumerate(starts):
        if cancel is not None and cancel.is_set():
            log("⏹ Etiquetado cancelado."); break
        chunk = wav[int(t * 16000):int((t + win) * 16000)]
        if len(chunk) < 1600:         # < 100 ms: nada útil
            continue
        inp = fe(chunk, sampling_rate=16000, return_tensors="pt").to(dev)
        with torch.no_grad():
            logits = model(**inp).logits[0]
        p = torch.sigmoid(logits).cpu().numpy()
        centers.append(min(t + win / 2, dur))
        probs.append([float(p[ix].max()) for ix in fam_idx.values()])
        if progress_cb:
            progress_cb((k + 1) / len(starts), None)
        if log_cb and (k % 25 == 0 or k == len(starts) - 1):
            log(f"Etiquetando audio… {k + 1}/{len(starts)} ventanas")

    if not probs:
        return {"events": [], "familias": fams, "params": {"win": win, "hop": hop}}

    P = np.array(probs)               # [n_ventanas, n_familias]
    C = np.array(centers)

    # suavizado temporal por familia (mediana) → mata parpadeos aislados
    def med(v, k):
        if k <= 1 or len(v) < k:
            return v
        pad = k // 2
        vp = np.pad(v, pad, mode="edge")
        return np.array([np.median(vp[i:i + k]) for i in range(len(v))])

    S = np.column_stack([med(P[:, j], smooth) for j in range(len(fams))])  # grilla suavizada

    events = []
    for j, fam in enumerate(fams):
        mask = S[:, j] >= threshold
        i = 0
        while i < len(mask):
            if not mask[i]:
                i += 1; continue
            j0 = i
            while i < len(mask) and mask[i]:
                i += 1
            j1 = i - 1
            t0 = max(0.0, float(C[j0]) - hop / 2)
            t1 = min(dur, float(C[j1]) + hop / 2)
            events.append({
                "t_ini": round(t0, 2), "t_fin": round(t1, 2),
                "tipo": "sonido", "familia": fam,
                "conf": round(float(S[j0:j1 + 1, j].max()), 3),
                "transitoria": fam in TRANSITORIAS,
            })
    events.sort(key=lambda e: (e["t_ini"], e["familia"]))
    log(f"Capa 1 lista: {len(events)} evento(s) de sonido en {len(fams)} familias.")
    return {"events": events, "familias": fams,
            "params": {"win": win, "hop": hop, "smooth": smooth, "threshold": threshold},
            # grilla suavizada para la Capa 2 (no se persiste al master.json)
            "_grid": {"centers": C.tolist(), "S": S.tolist(), "dur": dur}}


# ============================================================ CAPA 2 — escenas ==
def _rms_db_grid(audio, centers, half):
    """RMS en dB por frame (ventana ±`half` s alrededor de cada center)."""
    import librosa
    import audiocache
    wav = audiocache.load(audio, sr=16000)          # caché compartida (evita re-decodificar)
    out = []
    for c in centers:
        i0, i1 = int((c - half) * 16000), int((c + half) * 16000)
        chunk = wav[max(0, i0):max(0, i1)]
        rms = float(np.sqrt(np.mean(chunk ** 2))) if len(chunk) else 0.0
        out.append(20.0 * np.log10(max(rms, 1e-6)))
    return np.array(out)


def segmentar(tag_result, audio, *, W=3, min_seg=4.0, w_energia=3.0,
              sens=2.0, presencia=0.20, log_cb=None) -> dict:
    """CAPA 2. Convierte la grilla de tags (+ energía RMS) en segmentos contiguos que
    tilan todo el audio, cortando donde el CARÁCTER del sonido cambia.

    Método: por cada frame mido la distancia entre el carácter medio de los `W` frames
    previos y los `W` siguientes (curva de novedad); los picos por encima de un umbral
    robusto (mediana + MAD) son fronteras, respetando `min_seg` s de largo mínimo. Cada
    segmento se describe con MEDICIONES (familias dominantes, energía, densidad de
    transitorios) — no con etiquetas de escena (eso lo nombra la Capa 3/orquestador).
    """
    def log(m):
        if log_cb:
            log_cb(m)

    grid = tag_result.get("_grid")
    fams = tag_result["familias"]
    if not grid or len(grid["centers"]) < 2:
        dur = grid["dur"] if grid else 0.0
        return {"segments": [{"t_ini": 0.0, "t_fin": round(dur, 2), "dur": round(dur, 2),
                              "familias": [], "energia_db": None, "energia_z": 0.0,
                              "densidad_transitorios": 0.0, "tipo": "escena"}],
                "n": 1, "params": {"W": W, "min_seg": min_seg}}

    C = np.array(grid["centers"]); S = np.array(grid["S"]); dur = grid["dur"]
    hop = float(np.median(np.diff(C))) if len(C) > 1 else 2.0
    energia = _rms_db_grid(audio, C, half=hop)

    # normalizo energía a [0,1] (recortada a percentiles 5-95 → robusta a outliers)
    lo, hi = np.percentile(energia, 5), np.percentile(energia, 95)
    e_norm = np.clip((energia - lo) / (hi - lo + 1e-9), 0.0, 1.0)

    # sólo familias que aparecen alguna vez → matriz de carácter compacta
    activas = [j for j in range(S.shape[1]) if S[:, j].max() >= 0.15]
    F = np.column_stack([S[:, activas], w_energia * e_norm]) if activas else \
        (w_energia * e_norm)[:, None]

    # curva de novedad: |media(previos W) − media(siguientes W)|
    n = len(C)
    nov = np.zeros(n)
    for i in range(1, n):
        a = F[max(0, i - W):i].mean(axis=0)
        b = F[i:min(n, i + W)].mean(axis=0)
        nov[i] = np.linalg.norm(a - b)

    # umbral robusto + picos locales, respetando min_seg
    med = np.median(nov)
    mad = np.median(np.abs(nov - med)) + 1e-9
    thr = med + sens * 1.4826 * mad     # sens ↓ = más fronteras (más sensible)
    cand = [i for i in range(1, n - 1)
            if nov[i] >= thr and nov[i] >= nov[i - 1] and nov[i] >= nov[i + 1]]
    cand.sort(key=lambda i: -nov[i])
    bounds = []
    for i in cand:
        t = float(C[i]) - hop / 2
        if all(abs(t - bt) >= min_seg for bt in bounds):
            bounds.append(t)
    bounds = sorted([0.0] + [b for b in bounds if min_seg <= b <= dur - min_seg] + [dur])

    transit = [e for e in tag_result["events"] if e.get("transitoria")]
    mu_e, sd_e = float(energia.mean()), float(energia.std()) or 1.0

    segments = []
    for a, b in zip(bounds[:-1], bounds[1:]):
        sel = (C >= a) & (C < b) if b < dur else (C >= a)
        if not sel.any():
            sel = np.argmin(np.abs(C - (a + b) / 2))[None]
        seg_S = S[sel]
        dom = sorted(
            ({"familia": fams[j], "prob": round(float(seg_S[:, j].mean()), 2)}
             for j in range(len(fams)) if seg_S[:, j].mean() >= presencia),
            key=lambda d: -d["prob"])
        seg_e = float(energia[sel].mean())
        dseg = max(1e-3, b - a)
        n_tr = sum(1 for e in transit if a <= (e["t_ini"] + e["t_fin"]) / 2 < b)
        segments.append({
            "t_ini": round(a, 2), "t_fin": round(b, 2), "dur": round(dseg, 2),
            "tipo": "escena", "familias": dom,
            "energia_db": round(seg_e, 1), "energia_z": round((seg_e - mu_e) / sd_e, 2),
            "densidad_transitorios": round(n_tr / dseg * 10, 2),   # por 10 s
        })
    log(f"Capa 2 lista: {len(segments)} escena(s) · {len(bounds) - 2} frontera(s).")
    return {"segments": segments, "n": len(segments),
            "params": {"W": W, "min_seg": min_seg, "w_energia": w_energia}}


# ======================================================= CAPA 1.5 — transitorios ==
def transitorios(tag_result, audio, *, k=1.5, min_gap=0.15, confirm=0.10,
                 solo_confirmados=True, log_cb=None) -> list[dict]:
    """CAPA 1.5. Clava los golpes SECOS (disparo/explosión/vidrio) que el AST diluye en
    su ventana de 10s. Separa el CUÁNDO del QUÉ:

      · CUÁNDO — detección de onsets (flujo espectral, ~10ms): cada ataque brusco de
        energía es un instante preciso, independiente del AST. Pico local del onset
        sobre un umbral robusto (mediana + k·MAD), separados por `min_gap`.
      · QUÉ — pista de clase REUSANDO la grilla del AST de la Capa 1 (cero inferencia
        nueva): la familia transitoria más fuerte que el AST veía cerca del onset.

    No fuerza la etiqueta: el onset es un hecho acústico; la familia es una pista. Con
    `solo_confirmados=True` se quedan los onsets donde el AST corrobora una familia seca
    (>= `confirm`) → limpio, pocos falsos positivos. En False devuelve todos los ataques
    con la pista de familia (o None) para que decida el orquestador.
    """
    import librosa

    def log(m):
        if log_cb:
            log_cb(m)

    grid = tag_result.get("_grid")
    fams = tag_result["familias"]
    tj = [j for j, f in enumerate(fams) if f in TRANSITORIAS]
    C = np.array(grid["centers"]) if grid else np.array([])
    S = np.array(grid["S"]) if grid else np.zeros((0, len(fams)))
    hop = float(np.median(np.diff(C))) if len(C) > 1 else 2.0

    import audiocache
    wav = audiocache.load(audio, sr=16000)          # caché compartida (evita re-decodificar)
    hl = 160                                   # 10 ms
    env = librosa.onset.onset_strength(y=wav, sr=16000, hop_length=hl)
    times = librosa.frames_to_time(np.arange(len(env)), sr=16000, hop_length=hl)
    if len(env) < 3:
        return []

    med = float(np.median(env)); mad = float(np.median(np.abs(env - med))) + 1e-9
    thr = med + k * 1.4826 * mad

    # picos locales sobre el umbral, elegidos por fuerza y separados por min_gap
    cand = [i for i in range(1, len(env) - 1)
            if env[i] >= thr and env[i] >= env[i - 1] and env[i] >= env[i + 1]]
    cand.sort(key=lambda i: -env[i])
    chosen, chosen_t = [], []
    for i in cand:
        t = float(times[i])
        if all(abs(t - ct) >= min_gap for ct in chosen_t):
            chosen.append(i); chosen_t.append(t)

    out = []
    for i in sorted(chosen):
        t = float(times[i])
        familia, conf = None, 0.0
        if tj and len(C):                       # pista de familia desde la grilla del AST
            gi = np.where((C >= t - hop) & (C <= t + hop))[0]
            if not len(gi):
                gi = [int(np.argmin(np.abs(C - t)))]
            for j in tj:
                p = float(S[gi, j].max())
                if p > conf:
                    familia, conf = fams[j], p
        if solo_confirmados and conf < confirm:
            continue
        out.append({
            "t_ini": round(t, 3), "t_fin": round(t + 0.1, 3), "tipo": "transitorio",
            "familia": familia, "conf": round(conf, 3),
            "fuerza_z": round((env[i] - med) / (1.4826 * mad), 2),
        })
    log(f"Capa 1.5 lista: {len(out)} transitorio(s)"
        + (" confirmados por el AST." if solo_confirmados else " (todos los ataques)."))
    return out


def analizar(audio, *, win=10.0, hop=2.0, smooth=3, threshold=0.35,
             W=3, min_seg=4.0, sens=2.0, confirm=0.10, solo_confirmados=True,
             cancel=None, log_cb=None, progress_cb=None) -> dict:
    """Capas 1 + 1.5 + 2 encadenadas. Devuelve {"events", "transitorios", "segments",
    "familias", ...} listo para el master.json (sin la grilla interna)."""
    t = tag(audio, win=win, hop=hop, smooth=smooth, threshold=threshold,
            cancel=cancel, log_cb=log_cb, progress_cb=progress_cb)
    if cancel is not None and cancel.is_set():
        return {"events": [], "transitorios": [], "segments": [], "familias": t.get("familias", []),
                "n_eventos": 0, "n_transitorios": 0, "n_escenas": 0, "params": {}}
    tr = transitorios(t, audio, confirm=confirm, solo_confirmados=solo_confirmados, log_cb=log_cb)
    seg = segmentar(t, audio, W=W, min_seg=min_seg, sens=sens, log_cb=log_cb)
    return {"events": t["events"], "transitorios": tr, "segments": seg["segments"],
            "familias": t["familias"], "n_eventos": len(t["events"]),
            "n_transitorios": len(tr), "n_escenas": seg["n"],
            "params": {**t["params"], **seg["params"]}}


if __name__ == "__main__":
    import argparse, json
    from pathlib import Path
    ap = argparse.ArgumentParser(description="Capas 1+2: etiquetado AST + segmentación por escena.")
    ap.add_argument("audio")
    ap.add_argument("-o", "--out", default=None)
    ap.add_argument("--win", type=float, default=10.0)
    ap.add_argument("--hop", type=float, default=2.0)
    ap.add_argument("--threshold", type=float, default=0.35)
    ap.add_argument("--min-seg", type=float, default=4.0, help="duración mínima de escena (s)")
    ap.add_argument("--sens", type=float, default=2.0, help="sensibilidad de fronteras (↓ = más cortes)")
    ap.add_argument("--todos-transitorios", action="store_true",
                    help="devolver todos los ataques (no solo los que el AST confirma como secos)")
    a = ap.parse_args()
    res = analizar(a.audio, win=a.win, hop=a.hop, threshold=a.threshold,
                   min_seg=a.min_seg, sens=a.sens,
                   solo_confirmados=not a.todos_transitorios, log_cb=print)
    out = Path(a.out) if a.out else Path(a.audio).with_suffix(".escena.json")
    out.write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
    print("Escrito:", out.name)
