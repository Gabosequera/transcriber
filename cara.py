#!/usr/bin/env python3
"""
cara.py — análisis de la CARA (facecam) para la modalidad de VIDEO del master.json.

Capa 1 (medida, local, determinista): **MediaPipe Face Landmarker** (Tasks API) por frame:
  · 52 BLENDSHAPES — intensidades continuas de expresión (mouthSmile*, jawOpen, browInnerUp,
    eyeWide*, …). Es "medir, no interpretar": curvas de sonrisa/boca/cejas, no etiquetas.
  · POSE DE CABEZA — yaw/pitch/roll desde la matriz de transformación facial → hacia dónde
    mira (a cámara / izquierda / derecha / arriba / abajo / de frente).
  · PRESENCIA + TAMAÑO — bbox de la cara (para el reencuadre 9:16 futuro) y distancia
    interocular relativa (proxy de lean-in/lean-out).

MODO --live: abre la cámara con overlay (malla, barras de blendshapes, yaw/pitch/roll,
tamaño, indicador "A CÁMARA") para PROBAR y CALIBRAR qué detectamos. La GUI lo lanza como
SUBPROCESO (si algo de la cámara falla, no toca la app). Los signos de yaw/pitch se
verifican de oído/vista en este modo antes de fijar umbrales del pipeline offline.

Capa 2 (emoción, opcional): **EmotiEffLib** (`enet_b0_8_va_mtl`, ONNX) sobre el MISMO recorte
de cara que ya sale de MediaPipe → por muestra: VALENCE (positivo↔negativo) + AROUSAL
(activado↔calmado) — el mismo esquema dimensional que la voz (audeering en metadata.py) — y
las 8 clases de AffectNet (Happiness/Anger/Surprise/…) como ETIQUETA del pico, no como verdad
frame a frame. Se persigue con la misma maquinaria cara-v1 (baseline mediana/MAD del propio
video + picos con prominencia) → stream `emocion` en el cara.json (→ video.cara.emocion).

Sin torch ni transformers: MediaPipe trae su propio runtime (tflite) y EmotiEffLib corre por
onnxruntime (CPUExecutionProvider) → no tocan los pins ni el carril CUDA de whisper (en
Windows tampoco chocan con cuDNN). CPU en tiempo real.

El modelo (`face_landmarker.task`, ~3.6 MB) vive en models/; se descarga solo si falta. El de
emoción (`enet_b0_8_va_mtl.onnx`, ~16 MB) se auto-descarga a ~/.emotiefflib/ al primer uso.
"""
from __future__ import annotations

import json
import math
import os
import time
from pathlib import Path

import app_paths

PROJ = app_paths.SOURCE_DIR
MODEL_PATH = app_paths.MODELS_DIR / "face_landmarker.task"
MODEL_URL = ("https://storage.googleapis.com/mediapipe-models/face_landmarker/"
             "face_landmarker/float16/latest/face_landmarker.task")

# Umbrales de mirada (grados) — calibrados en modo --live con el usuario (2026-07-14).
# "A cámara" es ESTRICTO a propósito: tiene que ser intencional (hablarle a la audiencia),
# no un barrido casual. En el pipeline offline además se exigirá duración mínima sostenida.
# Zona "a cámara" — RECTÁNGULO LIBRE en grados de pose (recalibrado en vivo 2026-07-16
# con la guía verde; EDITABLE con el mouse en el modo --live: arrastrar adentro = mover,
# arrastrar un borde = resize; al soltar se persiste en config.json clave "mira_zona").
# Semántica: yaw NEGATIVO = SU derecha (YAW_POSITIVO_ES_DERECHA=False), pitch POSITIVO =
# abajo. El default está corrido a su derecha/abajo (cámara montada arriba-izquierda).
MIRA_ZONA_DEFAULT = {"yaw_min": -7.5, "yaw_max": 4.5, "pitch_min": -4.0, "pitch_max": 6.0}
CONFIG_PATH = app_paths.CONFIG_FILE


def _cfg_leer(clave: str, default: dict) -> dict:
    """Lee un dict de config.json validando que tenga las claves del default."""
    try:
        v = json.loads(CONFIG_PATH.read_text(encoding="utf-8")).get(clave)
        if v and all(k in v for k in default):
            return {k: float(v[k]) for k in default}
    except Exception:
        pass
    return dict(default)


def _cfg_guardar(clave: str, valor: dict) -> None:
    """Escribe una clave de config.json SIN pisar las demás (cpu/gpu/llama…)."""
    try:
        cfg = {}
        if CONFIG_PATH.exists():
            cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        cfg[clave] = {k: round(float(v), 3) for k, v in valor.items()}
        CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception:
        pass


def _cargar_mira_zona() -> dict:
    """Zona calibrada desde config.json (clave "mira_zona"); default si falta/corrupta."""
    return _cfg_leer("mira_zona", MIRA_ZONA_DEFAULT)


MIRA_ZONA = _cargar_mira_zona()

# Sesgo del estimador de IRIS, calibrado con la tecla `c` del modo --live (mirando FIJO
# al lente): con la cámara montada sobre los ojos, ojos_gaze lee "abajo" aun mirando al
# lente → este offset se RESTA de toda lectura. Persistido en config.json ("ojos_cal"),
# POR MÁQUINA (depende de la posición física de la cámara).
OJOS_CAL_DEFAULT = {"x": 0.0, "y": 0.0}
OJOS_CAL = _cfg_leer("ojos_cal", OJOS_CAL_DEFAULT)

# Confirmador OCULAR de "a cámara" (2026-07-16, pedido del usuario): la malla de MediaPipe
# también estima la posición del IRIS (blendshapes eyeLookIn/Out/Up/Down, ya calculados) →
# si la CABEZA está en la zona pero los OJOS están claramente desviados a un costado
# (mirando la pantalla de reojo), NO cuenta como a-cámara. Es un VETO (confirmador), no un
# detector: ojos centrados no alcanzan si la cabeza no está en zona.
OJOS_A_GRADOS = 20.0              # conversión score→grados de la deflexión ocular. Es LA
                                  # MISMA constante que dibuja el punto celeste en la guía
                                  # → la regla es literalmente "el punto celeste (cabeza +
                                  # ojos) también debe caer DENTRO de la caja verde"
                                  # (feedback en vivo 2026-07-16: con cabeza en zona y
                                  # ojos claramente abajo, seguía activando a-cámara)
# Calibración fina con el usuario en vivo (2026-07-16):
OJOS_PARPADEO_MAX = 0.4           # eyeBlink medio > esto = párpado cerrando → la red
                                  # dispara eyeLookDown FALSO ("la señal viene de abajo"
                                  # al parpadear) → lectura inválida ese frame (None: un
                                  # parpadeo ni veta ni confirma — decide la cabeza)
OJOS_GAN_ARRIBA = 1.3             # eyeLookUp* mide un toque corto → ganancia
OJOS_GAN_ABAJO = 1.15             # eyeLookDown* apenas corto → ganancia suave


def ojos_gaze(bs: dict | None) -> tuple[float, float] | None:
    """Deflexión de la mirada OCULAR (iris vs. cabeza) desde los 8 blendshapes eyeLook*
    de la red: (x, y) en scores 0-1; x positivo = SU izquierda (misma convención que el
    yaw), y positivo = abajo (como el pitch). (0,0) = ojos centrados en la cabeza.
    Durante un PARPADEO (eyeBlink alto) devuelve None: el cierre del párpado contamina
    la estimación vertical del iris."""
    if not bs:
        return None
    if (bs.get("eyeBlinkLeft", 0.0) + bs.get("eyeBlinkRight", 0.0)) / 2 > OJOS_PARPADEO_MAX:
        return None
    izq = (bs.get("eyeLookOutLeft", 0.0) + bs.get("eyeLookInRight", 0.0)) / 2
    der = (bs.get("eyeLookOutRight", 0.0) + bs.get("eyeLookInLeft", 0.0)) / 2
    arriba = (bs.get("eyeLookUpLeft", 0.0) + bs.get("eyeLookUpRight", 0.0)) / 2
    abajo = (bs.get("eyeLookDownLeft", 0.0) + bs.get("eyeLookDownRight", 0.0)) / 2
    return (round(izq - der - OJOS_CAL["x"], 3),
            round(abajo * OJOS_GAN_ABAJO - arriba * OJOS_GAN_ARRIBA - OJOS_CAL["y"], 3))


def _guardar_mira_zona() -> None:
    _cfg_guardar("mira_zona", MIRA_ZONA)


# ---- PRESETS de calibración de mirada (zona de cabeza + sesgo de ojos) ----
# Viven en config.json: {"mira_presets": {nombre: {mira_zona, ojos_cal}},
# "mira_preset_activo": nombre}. Lo ACTIVO sigue siendo mira_zona/ojos_cal (lo que leen
# el live y el offline); un preset es una foto con nombre que se puede restaurar.
def mira_presets_listar() -> tuple[list[str], str | None]:
    """→ (nombres ordenados, nombre del activo o None)."""
    try:
        cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        return sorted(cfg.get("mira_presets", {})), cfg.get("mira_preset_activo")
    except Exception:
        return [], None


def mira_preset_guardar(nombre: str) -> str:
    """Guarda la calibración ACTUAL (zona + ojos) como preset `nombre` y lo marca activo.
    Relee config.json primero: la calibración viva pudo hacerse en la ventana --live
    (otro proceso) — el preset captura lo que el usuario calibró de verdad."""
    MIRA_ZONA.update(_cargar_mira_zona())
    OJOS_CAL.update(_cfg_leer("ojos_cal", OJOS_CAL_DEFAULT))
    try:
        cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8")) if CONFIG_PATH.exists() else {}
        cfg.setdefault("mira_presets", {})[nombre] = {
            "mira_zona": {k: round(float(v), 3) for k, v in MIRA_ZONA.items()},
            "ojos_cal": {k: round(float(v), 3) for k, v in OJOS_CAL.items()}}
        cfg["mira_preset_activo"] = nombre
        CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=1), encoding="utf-8")
        return f"✓ preset de mirada «{nombre}» guardado (y activo)"
    except Exception as e:
        return f"⚠ no pude guardar el preset: {e}"


def mira_aplicar_valores(zona: dict | None, ojos: dict | None) -> str:
    """Aplica VALORES de calibración directos (no un preset con nombre): los presets
    GLOBALES del wizard guardan la calibración resuelta y la restauran con esto — así el
    preset reproduce la calibración de cuando se guardó, aunque después se haya
    recalibrado otra cámara. Persiste en config.json (lo relee el próximo análisis)."""
    try:
        if zona and all(k in zona for k in MIRA_ZONA_DEFAULT):
            MIRA_ZONA.update({k: float(zona[k]) for k in MIRA_ZONA_DEFAULT})
            _guardar_mira_zona()
        if ojos and all(k in ojos for k in OJOS_CAL_DEFAULT):
            OJOS_CAL.update({k: float(ojos[k]) for k in OJOS_CAL_DEFAULT})
            _cfg_guardar("ojos_cal", OJOS_CAL)
        return "✓ calibración de mirada del preset aplicada"
    except Exception as e:
        return f"⚠ no pude aplicar la calibración del preset: {e}"


def mira_preset_cargar(nombre: str) -> str:
    """Aplica el preset `nombre`: copia su zona+ojos a lo ACTIVO (config.json) y en este
    proceso. El próximo análisis (y una ventana --live nueva) lo usan sin reiniciar."""
    try:
        cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        p = cfg["mira_presets"][nombre]
        MIRA_ZONA.update({k: float(v) for k, v in p["mira_zona"].items()})
        OJOS_CAL.update({k: float(v) for k, v in p["ojos_cal"].items()})
        _guardar_mira_zona()
        _cfg_guardar("ojos_cal", OJOS_CAL)
        _cfg_guardar_str("mira_preset_activo", nombre)
        return f"✓ preset «{nombre}» aplicado (zona + ojos)"
    except KeyError:
        return f"⚠ no existe el preset «{nombre}»"
    except Exception as e:
        return f"⚠ no pude aplicar el preset: {e}"


def _cfg_guardar_str(clave: str, valor: str) -> None:
    try:
        cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8")) if CONFIG_PATH.exists() else {}
        cfg[clave] = valor
        CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception:
        pass


def calibrar_centro(muestras: list) -> str:
    """Calibración de un toque (tecla `c` del --live, mirando FIJO al lente): `muestras`
    = [(yaw, pitch, ojos_x, ojos_y), …] de ~1 s. Con la MEDIANA: (a) recentra MIRA_ZONA
    en la pose de cabeza real (conserva el tamaño de la caja) y (b) acumula el sesgo de
    ojos restante en OJOS_CAL (las lecturas ya venían corregidas por el offset previo).
    Persiste ambos en config.json. Devuelve el mensaje para el log."""
    import numpy as np
    arr = np.array(muestras, dtype=float)
    yaw0, pit0, ox, oy = (float(v) for v in np.median(arr, axis=0))
    w2 = (MIRA_ZONA["yaw_max"] - MIRA_ZONA["yaw_min"]) / 2
    h2 = (MIRA_ZONA["pitch_max"] - MIRA_ZONA["pitch_min"]) / 2
    MIRA_ZONA.update(yaw_min=round(yaw0 - w2, 1), yaw_max=round(yaw0 + w2, 1),
                     pitch_min=round(pit0 - h2, 1), pitch_max=round(pit0 + h2, 1))
    OJOS_CAL["x"] = round(OJOS_CAL["x"] + ox, 3)
    OJOS_CAL["y"] = round(OJOS_CAL["y"] + oy, 3)
    _guardar_mira_zona()
    _cfg_guardar("ojos_cal", OJOS_CAL)
    return (f"✓ centro calibrado con {len(muestras)} lecturas: caja recentrada en "
            f"yaw {yaw0:+.1f}° / pitch {pit0:+.1f}° · sesgo de ojos anulado "
            f"(x{OJOS_CAL['x']:+.2f} y{OJOS_CAL['y']:+.2f}) — guardado en config.json")
MIRADA_IZQ_MIN = 18.0             # |yaw| ≥ esto (hacia SU izquierda) → "izquierda"
MIRADA_DER_MIN = 24.0             # |yaw| ≥ esto (hacia SU derecha) → "derecha" — más duro:
                                  # la cámara está a su izquierda → la pose neutral ya viene
                                  # girada hacia la derecha (afinado en vivo con el usuario)
MIRADA_ARRIBA_MIN = 14.0          # pitch ≤ -esto → "arriba"
MIRADA_ABAJO_MIN = 30.0           # pitch ≥ +esto → "abajo" (más duro: la pose neutral ya
                                  # tiene pitch positivo con la cámara sobre los ojos;
                                  # 14→24→30 afinado en vivo con el usuario)
YAW_POSITIVO_ES_DERECHA = False   # semántica del signo (SU derecha) — VERIFICADO en vivo con el
                                  # usuario (2026-07-14): yaw NEGATIVO = él mira a SU derecha


def _en_zona(yaw: float, pitch: float) -> bool:
    return (MIRA_ZONA["yaw_min"] <= yaw <= MIRA_ZONA["yaw_max"]
            and MIRA_ZONA["pitch_min"] <= pitch <= MIRA_ZONA["pitch_max"])


def _es_camara(yaw: float, pitch: float, ojos: tuple | None = None) -> bool:
    """«A cámara» = la CABEZA dentro del rectángulo calibrado (MIRA_ZONA, en grados de
    yaw/pitch crudos; es donde cae la pose del usuario mirando al lente, calibrado en vivo
    con la guía) Y, si hay lectura de ojos (ojos_gaze), la MIRADA COMBINADA (cabeza +
    deflexión ocular × OJOS_A_GRADOS — el punto celeste de la guía) TAMBIÉN dentro del
    mismo rectángulo. Ojos apuntando fuera del recuadro = no está mirando el lente aunque
    la cabeza sí. Sin lectura (None, p.ej. parpadeo) decide solo la cabeza."""
    if not _en_zona(yaw, pitch):
        return False
    if ojos is not None and not _en_zona(yaw + ojos[0] * OJOS_A_GRADOS,
                                         pitch + ojos[1] * OJOS_A_GRADOS):
        return False
    return True


def clasificar_mirada(yaw: float, pitch: float, ojos: tuple | None = None) -> str:
    """Etiqueta discreta de hacia dónde mira: 'camara' (estricto — intencional), 'izquierda'/
    'derecha' (SU izquierda/derecha), 'arriba'/'abajo', o 'frente' (zona neutra: adelante,
    sin giro claro ni a-cámara). QUÉ hay adelante no lo sabemos ni lo decimos — medir, no
    interpretar. Signos verificados en vivo con el usuario (2026-07-14)."""
    if _es_camara(yaw, pitch, ojos):
        return "camara"
    der = (yaw > 0) == YAW_POSITIVO_ES_DERECHA
    if abs(yaw) >= (MIRADA_DER_MIN if der else MIRADA_IZQ_MIN) and abs(yaw) >= abs(pitch):
        return "derecha" if der else "izquierda"
    if pitch <= -MIRADA_ARRIBA_MIN:                 # signos verificados en vivo
        return "arriba"
    if pitch >= MIRADA_ABAJO_MIN:
        return "abajo"
    return "frente"

_DETECTORES: dict[str, object] = {}     # cache por modo ("VIDEO"/"IMAGE"), un proceso
_EMO: list = []                         # cache del reconocedor de emociones (0 o 1 elemento)

# ---- emoción (EmotiEffLib) — modelo y semántica de la salida ----
# enet_b0_8_va_mtl = EfficientNet-B0 multi-task: 8 clases AffectNet + valence + arousal.
# Elegido B0 (no B2): la cara va a pantalla grande (1920x1080) → condiciones ideales; B2 solo
# suma ~2 puntos de precisión por 3x el costo. Backend ONNX = CPU puro, determinista, sin torch.
EMO_MODEL = "enet_b0_8_va_mtl"
EMO_MARGEN = 0.25                 # margen sobre el bbox de landmarks (AffectNet se entrenó con
                                  # crops más holgados que la malla; medido: 0.25 anda bien)


def available() -> bool:
    """¿Está mediapipe instalado? (el modelo se baja solo al primer uso).
    find_spec en vez de importar (arranque de la GUI — ver metadata.available)."""
    from importlib.util import find_spec
    try:
        return find_spec("mediapipe") is not None
    except Exception:
        return False


def emocion_available() -> bool:
    """¿Está emotiefflib instalado? (su modelo se baja solo al primer uso)."""
    from importlib.util import find_spec
    try:
        return find_spec("emotiefflib") is not None
    except Exception:
        return False


def _load_emo():
    """Reconocedor de emociones cacheado (EmotiEffLib, backend ONNX → siempre CPU)."""
    if not _EMO:
        from emotiefflib.facial_analysis import EmotiEffLibRecognizer
        _EMO.append(EmotiEffLibRecognizer(engine="onnx", model_name=EMO_MODEL, device="cpu"))
    return _EMO[0]


def _ensure_model() -> Path:
    if not MODEL_PATH.exists() or MODEL_PATH.stat().st_size < 1_000_000:
        MODEL_PATH.parent.mkdir(parents=True, exist_ok=True)
        import urllib.request
        urllib.request.urlretrieve(MODEL_URL, MODEL_PATH)
    return MODEL_PATH


def _load(mode: str = "VIDEO"):
    """Detector cacheado por modo. VIDEO = con tracking entre frames (video/cámara);
    IMAGE = frames sueltos."""
    if mode in _DETECTORES:
        return _DETECTORES[mode]
    import mediapipe as mp
    from mediapipe.tasks.python import vision
    opts = vision.FaceLandmarkerOptions(
        base_options=mp.tasks.BaseOptions(model_asset_path=str(_ensure_model())),
        running_mode=getattr(vision.RunningMode, mode),
        output_face_blendshapes=True,
        output_facial_transformation_matrixes=True,
        num_faces=1,                       # la facecam es UNA persona (Gabriel)
    )
    _DETECTORES[mode] = vision.FaceLandmarker.create_from_options(opts)
    return _DETECTORES[mode]


def unload() -> None:
    """Suelta los detectores cacheados (registrado en models.unload_all)."""
    for d in _DETECTORES.values():
        try:
            d.close()
        except Exception:
            pass
    _DETECTORES.clear()
    _EMO.clear()


def emocion_en_crop(frame, bbox) -> dict | None:
    """Corre EmotiEffLib sobre el recorte de cara de `frame` (RGB) dado el `bbox` de
    landmarks (coords del MISMO frame), con margen EMO_MARGEN. Devuelve
    {emocion, emo_p, valencia, excitacion} o None si el recorte quedó vacío.
    valence/arousal son las 2 últimas salidas del modelo MTL (rango ~[-1,1])."""
    x0, y0, x1, y1 = bbox
    mx, my = (x1 - x0) * EMO_MARGEN, (y1 - y0) * EMO_MARGEN
    H, W = frame.shape[:2]
    crop = frame[max(0, int(y0 - my)):min(H, int(y1 + my)),
                 max(0, int(x0 - mx)):min(W, int(x1 + mx))]
    if crop.size == 0 or crop.shape[0] < 8 or crop.shape[1] < 8:
        return None
    labels, scores = _load_emo().predict_emotions(crop, logits=False)
    s = scores[0]
    # TODA salida consumida debe ser finita y con la forma esperada (8 clases + val + aro);
    # si no, excepción CONTROLADA → _muestrear la cuenta para el corte de degradación
    # (un None silencioso no activaría el corte de 8 fallos — review Codex ronda 2).
    if len(s) < 10 or not all(math.isfinite(float(v)) for v in (*s[:8], s[-2], s[-1])):
        raise ValueError("salida inválida del modelo de emoción (forma o valores no finitos)")
    return {"emocion": labels[0], "emo_p": round(float(max(s[:8])), 3),
            "valencia": round(float(s[-2]), 3), "excitacion": round(float(s[-1]), 3)}


def _euler(mat) -> tuple[float, float, float]:
    """(yaw, pitch, roll) en grados desde la matriz de transformación facial (4x4).
    Descomposición Tait-Bryan ZYX sobre la rotación. Los SIGNOS/semántica se validan en
    modo --live (el overlay los muestra en crudo) antes de fijar umbrales offline."""
    r = mat
    sy = math.sqrt(r[0][0] ** 2 + r[1][0] ** 2)
    pitch = math.degrees(math.atan2(r[2][1], r[2][2]))
    yaw = math.degrees(math.atan2(-r[2][0], sy))
    roll = math.degrees(math.atan2(r[1][0], r[0][0]))
    return round(yaw, 1), round(pitch, 1), round(roll, 1)


def medidas(result, w: int, h: int) -> dict:
    """Convierte un FaceLandmarkerResult en las MEDIDAS por frame que consume el pipeline:
    {presente, bbox, tam, yaw, pitch, roll, mira_camara, blendshapes{...}}. bbox en píxeles
    [x0,y0,x1,y1]; `tam` = distancia interocular / ancho de frame (proxy de qué tan cerca
    está de la cámara)."""
    if not result.face_landmarks:
        return {"presente": False}
    lms = result.face_landmarks[0]
    xs = [p.x for p in lms]; ys = [p.y for p in lms]
    bbox = [int(min(xs) * w), int(min(ys) * h), int(max(xs) * w), int(max(ys) * h)]
    # 33 / 263 = comisuras externas de los ojos (malla canónica de MediaPipe)
    tam = math.dist((lms[33].x, lms[33].y * h / max(w, 1)),
                    (lms[263].x, lms[263].y * h / max(w, 1)))
    out = {"presente": True, "bbox": bbox, "tam": round(tam, 4)}
    if result.face_blendshapes:                  # primero: los ojos confirman la mirada
        out["blendshapes"] = {c.category_name: round(float(c.score), 3)
                              for c in result.face_blendshapes[0]
                              if c.category_name != "_neutral"}
        g = ojos_gaze(out["blendshapes"])
        if g:
            out["ojos"] = list(g)
    if result.facial_transformation_matrixes:
        yaw, pitch, roll = _euler(result.facial_transformation_matrixes[0])
        ojos = tuple(out["ojos"]) if "ojos" in out else None
        mirada = clasificar_mirada(yaw, pitch, ojos)
        out.update(yaw=yaw, pitch=pitch, roll=roll,
                   mirada=mirada, mira_camara=mirada == "camara")
    return out


def buscar_region(rgb) -> tuple[int, int, int, int] | None:
    """Encuentra la REGIÓN de la facecam en un frame de grabación. El detector de MediaPipe
    reescala el frame → una facecam chica en un 1080p NO se detecta directo (verificado con
    el VOD real). Estrategia: probar el frame ENTERO (caso cámara a pantalla completa) y si
    no hay cara, los 4 CUADRANTES (las facecams viven en esquinas), donde la cara ocupa
    proporcionalmente más. Devuelve (x0,y0,x1,y1) con margen sobre el frame completo, o None.
    El pipeline offline la busca UNA vez y la fija (la facecam no se mueve)."""
    import mediapipe as mp
    h, w = rgb.shape[:2]
    det = _load("IMAGE")
    # multi-escala: el detector solo ve caras que ocupan una fracción razonable del tile
    # (medido en el VOD real: la facecam de ~32% del ancho aparece con tiles de 33%, no de
    # 40%). Se barre de tiles grandes a chicos, con 50% de solape, ESQUINAS primero (ahí
    # viven las facecams). Se corre UNA vez por video → el costo (hasta ~3 s) no importa.
    zonas = [(0, 0, w, h)]                                # frame entero (cámara full-screen)
    for esc in (0.5, 0.33, 0.25):
        tw, th = int(w * esc), int(h * esc)
        xs = sorted({0, *range(tw // 2, w - tw, tw // 2), w - tw})
        ys = sorted({0, *range(th // 2, h - th, th // 2), h - th})
        tiles = [(x, y, x + tw, y + th) for y in ys for x in xs]
        tiles.sort(key=lambda t: min((t[0] - cx) ** 2 + (t[1] - cy) ** 2
                                     for cx in (0, w - tw) for cy in (0, h - th)))
        zonas += tiles
    for x0, y0, x1, y1 in zonas:
        crop = rgb[y0:y1, x0:x1]
        res = det.detect(mp.Image(image_format=mp.ImageFormat.SRGB, data=crop.copy()))
        if not res.face_landmarks:
            continue
        lms = res.face_landmarks[0]
        cw, ch = x1 - x0, y1 - y0
        fx0 = x0 + min(p.x for p in lms) * cw; fx1 = x0 + max(p.x for p in lms) * cw
        fy0 = y0 + min(p.y for p in lms) * ch; fy1 = y0 + max(p.y for p in lms) * ch
        mx, my = (fx1 - fx0) * 0.6, (fy1 - fy0) * 0.6      # margen: la cara se mueve dentro de la cam
        return (max(0, int(fx0 - mx)), max(0, int(fy0 - my)),
                min(w, int(fx1 + mx)), min(h, int(fy1 + my)))
    return None


def analizar_imagen(path, region: tuple | None = None, emociones: bool = True) -> dict:
    """Analiza UNA imagen (tests/verificación headless). Si `region` es None la busca sola
    (`buscar_region`). Devuelve `medidas` calculadas SOBRE el recorte de la facecam (bbox
    remapeado al frame completo; `tam` relativo al recorte) + la clave `region` usada."""
    import cv2
    import mediapipe as mp
    img = cv2.imread(str(path))
    if img is None:
        raise RuntimeError(f"no pude leer la imagen: {path}")
    rgb = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
    region = region or buscar_region(rgb)
    if region is None:
        return {"presente": False, "region": None}
    x0, y0, x1, y1 = region
    crop = rgb[y0:y1, x0:x1].copy()
    det = _load("IMAGE")
    res = det.detect(mp.Image(image_format=mp.ImageFormat.SRGB, data=crop))
    m = medidas(res, x1 - x0, y1 - y0)
    if m.get("bbox") and emociones and emocion_available():
        e = emocion_en_crop(crop, m["bbox"])
        if e:
            m.update(e)
    if m.get("bbox"):
        m["bbox"] = [m["bbox"][0] + x0, m["bbox"][1] + y0, m["bbox"][2] + x0, m["bbox"][3] + y0]
    m["region"] = list(region)
    return m


# ------------------------------------------------------------- modo LIVE (cámara) --
_GAUGE: dict = {}      # geometría del indicador de mirada (la usa el editor de mouse)


def _mouse_zona(event, mx, my, flags, estado):
    """Editor de la zona «a cámara» con el mouse, sobre el indicador: arrastrar DENTRO
    de la caja = moverla; arrastrar un BORDE (±7 px) = redimensionar ese lado; soltar =
    guardar en config.json. Convierte px del gauge ↔ grados con la geometría de _GAUGE."""
    import cv2
    g = _GAUGE
    if not g:
        return
    z, esc, M = MIRA_ZONA, g["esc"], 7
    x1 = g["cx"] + int(-z["yaw_max"] * esc); x2 = g["cx"] + int(-z["yaw_min"] * esc)
    y1 = g["cy"] + int(z["pitch_min"] * esc); y2 = g["cy"] + int(z["pitch_max"] * esc)
    if event == cv2.EVENT_LBUTTONDOWN:
        bordes = set()
        if abs(mx - x1) <= M and y1 - M <= my <= y2 + M:
            bordes.add("izq")
        if abs(mx - x2) <= M and y1 - M <= my <= y2 + M:
            bordes.add("der")
        if abs(my - y1) <= M and x1 - M <= mx <= x2 + M:
            bordes.add("arriba")
        if abs(my - y2) <= M and x1 - M <= mx <= x2 + M:
            bordes.add("abajo")
        if bordes:
            estado.update(modo="resize", bordes=bordes)
        elif x1 < mx < x2 and y1 < my < y2:
            estado.update(modo="mover", prev=(mx, my))
    elif event == cv2.EVENT_MOUSEMOVE and estado.get("modo"):
        R = g["rango"]
        if estado["modo"] == "mover":
            dx, dy = mx - estado["prev"][0], my - estado["prev"][1]
            estado["prev"] = (mx, my)
            dyaw, dpitch = -dx / esc, dy / esc
            if -R <= z["yaw_min"] + dyaw and z["yaw_max"] + dyaw <= R:
                z["yaw_min"] += dyaw; z["yaw_max"] += dyaw
            if -R <= z["pitch_min"] + dpitch and z["pitch_max"] + dpitch <= R:
                z["pitch_min"] += dpitch; z["pitch_max"] += dpitch
        else:
            MIN = 1.0                              # ancho/alto mínimo de la zona (grados)
            yaw_m = max(-R, min(R, -(mx - g["cx"]) / esc))
            pitch_m = max(-R, min(R, (my - g["cy"]) / esc))
            if "izq" in estado["bordes"]:
                z["yaw_max"] = max(yaw_m, z["yaw_min"] + MIN)
            if "der" in estado["bordes"]:
                z["yaw_min"] = min(yaw_m, z["yaw_max"] - MIN)
            if "arriba" in estado["bordes"]:
                z["pitch_min"] = min(pitch_m, z["pitch_max"] - MIN)
            if "abajo" in estado["bordes"]:
                z["pitch_max"] = max(pitch_m, z["pitch_min"] + MIN)
    elif event == cv2.EVENT_LBUTTONUP and estado.get("modo"):
        estado["modo"] = None
        _guardar_mira_zona()
        print("zona a-cámara guardada en config.json: "
              + " ".join(f"{k}={v:+.1f}°" for k, v in MIRA_ZONA.items()))


def _draw_guia_camara(frame, m: dict):
    """Indicador de mirada (esquina superior derecha): la CAJA VERDE es la zona de
    tolerancia de «a cámara» (rectángulo ASIMÉTRICO: más ancho hacia SU derecha y hacia
    abajo — ver _es_camara) y el punto es tu pose actual — punto DENTRO de la caja =
    cuenta como mirando a cámara. El eje horizontal va espejado igual que la imagen
    (mirás a tu derecha → el punto va a la derecha). Cuando está activo, además se
    enciende un borde verde en todo el frame."""
    import cv2
    import numpy as np
    h, w = frame.shape[:2]
    S, R = 170, 30.0                       # lado del indicador (px) · rango en grados (±)
    x0, y0 = w - S - 14, 14
    cx, cy = x0 + S // 2, y0 + S // 2
    VERDE, GRIS, BLANCO = (80, 230, 80), (110, 110, 110), (240, 240, 240)
    activo = m.get("mirada") == "camara"
    sub = frame[y0:y0 + S, x0:x0 + S]      # fondo oscurecido para que se lea
    cv2.addWeighted(sub, 0.35, np.zeros_like(sub), 0.65, 0, dst=sub)
    cv2.rectangle(frame, (x0, y0), (x0 + S, y0 + S), GRIS, 1)
    cv2.line(frame, (cx, y0 + 4), (cx, y0 + S - 4), GRIS, 1)          # cruz = lente (0,0)
    cv2.line(frame, (x0 + 4, cy), (x0 + S - 4, cy), GRIS, 1)
    # la caja verde = MIRA_ZONA, en coords del punto (x = -yaw espejado, y = pitch).
    # yaw_max (izquierda) → borde IZQUIERDO en pantalla; yaw_min (derecha) → DERECHO.
    esc = (S / 2) / R
    zx1 = cx + int(-MIRA_ZONA["yaw_max"] * esc)
    zx2 = cx + int(-MIRA_ZONA["yaw_min"] * esc)
    zy1 = cy + int(MIRA_ZONA["pitch_min"] * esc)
    zy2 = cy + int(MIRA_ZONA["pitch_max"] * esc)
    cv2.rectangle(frame, (zx1, zy1), (zx2, zy2), VERDE, 2 if activo else 1)
    _GAUGE.update(cx=cx, cy=cy, esc=esc, rango=R)     # geometría para el editor de mouse
    if m.get("presente") and "yaw" in m:
        px = cx + int(np.clip(-m["yaw"], -R, R) / R * (S / 2))        # -yaw: espejo natural
        py = cy + int(np.clip(m["pitch"], -R, R) / R * (S / 2))
        cv2.circle(frame, (px, py), 6, VERDE if activo else BLANCO, -1)
        cv2.circle(frame, (px, py), 6, (30, 30, 30), 1)
        if "ojos" in m:            # punto chico celeste = cabeza + IRIS (a dónde miran
            gv = OJOS_A_GRADOS     # los ojos) — la MISMA conversión que usa _es_camara:
            # este punto también tiene que caer dentro de la caja verde
            ex = px + int(np.clip(-m["ojos"][0] * gv, -R, R) / R * (S / 2))
            ey = py + int(np.clip(m["ojos"][1] * gv, -R, R) / R * (S / 2))
            ex = max(x0 + 4, min(x0 + S - 4, ex)); ey = max(y0 + 4, min(y0 + S - 4, ey))
            cv2.line(frame, (px, py), (ex, ey), (255, 200, 120), 1, cv2.LINE_AA)
            cv2.circle(frame, (ex, ey), 3, (255, 200, 120), -1)
    cv2.putText(frame, "zona A CAMARA (arrastrame)", (x0 + 6, y0 + S - 22),
                cv2.FONT_HERSHEY_SIMPLEX, 0.38, VERDE if activo else GRIS, 1, cv2.LINE_AA)
    cv2.putText(frame, f"yaw {MIRA_ZONA['yaw_min']:+.0f}..{MIRA_ZONA['yaw_max']:+.0f}  "
                f"pitch {MIRA_ZONA['pitch_min']:+.0f}..{MIRA_ZONA['pitch_max']:+.0f}",
                (x0 + 6, y0 + S - 7), cv2.FONT_HERSHEY_SIMPLEX, 0.36,
                (170, 170, 170), 1, cv2.LINE_AA)
    if activo:
        cv2.rectangle(frame, (0, 0), (w - 1, h - 1), VERDE, 4)


def _draw_overlay(frame, m: dict, fps: float):
    """Dibuja el overlay de calibración sobre el frame YA espejado. Nota: como el frame se
    espeja para verse natural, el bbox/puntos se dibujan ANTES del espejado (en crudo) y
    este panel de texto DESPUÉS (para que se lea)."""
    import cv2
    h, w = frame.shape[:2]
    y = 26
    def txt(s, color=(240, 240, 240), scale=0.55):
        nonlocal y
        cv2.putText(frame, s, (10, y), cv2.FONT_HERSHEY_SIMPLEX, scale, (0, 0, 0), 3, cv2.LINE_AA)
        cv2.putText(frame, s, (10, y), cv2.FONT_HERSHEY_SIMPLEX, scale, color, 1, cv2.LINE_AA)
        y += 22
    txt(f"FPS {fps:4.1f}   ·   q = salir · s = snapshot", (180, 180, 180), 0.5)
    if not m.get("presente"):
        txt("SIN CARA", (60, 60, 255), 0.9)
        return
    txt(f"yaw {m.get('yaw', 0):+6.1f}   pitch {m.get('pitch', 0):+6.1f}   roll {m.get('roll', 0):+6.1f}")
    if "ojos" in m:
        ox, oy = m["ojos"]
        veta = ("yaw" in m and _en_zona(m["yaw"], m["pitch"])
                and not _en_zona(m["yaw"] + ox * OJOS_A_GRADOS,
                                 m["pitch"] + oy * OJOS_A_GRADOS))
        txt(f"ojos x{ox:+.2f} y{oy:+.2f}"
            + ("  << FUERA del recuadro (vetan a-camara)" if veta else ""),
            (120, 200, 255) if veta else (255, 200, 120), 0.5)
    elif m.get("blendshapes"):
        txt("ojos: — (parpadeo: lectura descartada)", (150, 150, 150), 0.5)
    txt(f"tam (interocular/ancho) {m.get('tam', 0):.4f}")
    mirada = m.get("mirada", "?")
    ETIQ = {"camara": ("MIRANDO A CAMARA", (80, 230, 80)),
            "izquierda": ("<-- IZQUIERDA (tu izq)", (70, 200, 255)),
            "derecha": ("DERECHA (tu der) -->", (70, 200, 255)),
            "arriba": ("ARRIBA", (200, 200, 120)),
            "abajo": ("ABAJO", (200, 200, 120)),
            "frente": ("de frente", (160, 160, 160))}
    e, color = ETIQ.get(mirada, (mirada, (160, 160, 160)))
    txt(e, color, 0.7)
    if "emocion" in m:                      # EmotiEffLib (si está instalado)
        txt(f"{m['emocion']} p={m['emo_p']:.2f}   val {m['valencia']:+.2f}   "
            f"aro {m['excitacion']:+.2f}", (120, 220, 220), 0.6)
    # top blendshapes como barras
    bs = sorted((m.get("blendshapes") or {}).items(), key=lambda kv: -kv[1])[:9]
    y += 6
    for name, score in bs:
        cv2.rectangle(frame, (10, y - 12), (10 + int(220 * score), y), (90, 200, 90), -1)
        cv2.rectangle(frame, (10, y - 12), (230, y), (70, 70, 70), 1)
        cv2.putText(frame, f"{name} {score:.2f}", (238, y - 1),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.45, (230, 230, 230), 1, cv2.LINE_AA)
        y += 20


def live(cam: int = 0, ancho: int = 1280, alto: int = 720, emociones: bool = True) -> None:
    """Ventana de cámara en vivo con overlay de todo lo que detectamos, para probar y
    calibrar. q = salir, s = guardar snapshot anotado en media/."""
    import cv2
    import mediapipe as mp
    cap = cv2.VideoCapture(cam)
    if not cap.isOpened():
        raise RuntimeError(f"No pude abrir la cámara {cam} (¿existe /dev/video{cam}?).")
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, ancho)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, alto)
    det = _load("VIDEO")
    emo_ok = emociones and emocion_available()
    if emo_ok:
        try:
            _load_emo()
            print(f"Emociones en vivo: EmotiEffLib {EMO_MODEL} (cada 3 frames)")
        except Exception as e:
            print(f"EmotiEffLib no cargó ({e}) — sigo sin emociones."); emo_ok = False
    win = "Cara - prueba en vivo"
    cv2.namedWindow(win, cv2.WINDOW_NORMAL)
    cv2.setMouseCallback(win, _mouse_zona, {"modo": None})   # editor de la zona a-cámara
    t_prev, fps = time.monotonic(), 0.0
    n_frame, ultimo_emo, cal = 0, None, None
    print(f"Cámara {cam} abierta · q salir · s snapshot · "
          "c = CALIBRAR CENTRO (mirá fijo al lente ~1s: recentra la caja y anula el "
          "sesgo de ojos) · r reset zona · mouse sobre el indicador: arrastrar adentro "
          "= mover la caja verde, un borde = resize (al soltar se guarda en config.json)")
    while True:
        ok, frame = cap.read()
        if not ok:
            print("La cámara dejó de entregar frames."); break
        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        res = det.detect_for_video(mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb),
                                   int(time.monotonic() * 1000))
        h, w = frame.shape[:2]
        m = medidas(res, w, h)
        n_frame += 1
        if emo_ok and m.get("presente"):        # cada 3 frames: no comerle fps a la malla
            if n_frame % 3 == 0 or ultimo_emo is None:
                try:
                    ultimo_emo = emocion_en_crop(rgb, m["bbox"])
                except Exception as ex:
                    print(f"EmotiEffLib falló ({ex}) — sigo sin emociones."); emo_ok = False
                    ultimo_emo = None
            if ultimo_emo:
                m.update(ultimo_emo)
        elif not m.get("presente"):
            ultimo_emo = None
        if cal is not None:                    # calibración de centro en curso (tecla c)
            if m.get("presente") and "yaw" in m and "ojos" in m:
                cal["muestras"].append((m["yaw"], m["pitch"], m["ojos"][0], m["ojos"][1]))
            cal["restantes"] -= 1
            if cal["restantes"] <= 0:
                print(calibrar_centro(cal["muestras"]) if len(cal["muestras"]) >= 8 else
                      "⚠ calibración fallida: no junté lecturas (¿cara visible, sin parpadear?)")
                cal = None
        # puntos + bbox sobre el frame CRUDO (se espejan junto con la imagen)
        if m.get("presente"):
            for p in res.face_landmarks[0]:
                cv2.circle(frame, (int(p.x * w), int(p.y * h)), 1, (0, 200, 255), -1)
            x0, y0, x1, y1 = m["bbox"]
            cv2.rectangle(frame, (x0, y0), (x1, y1), (0, 200, 255), 1)
        frame = cv2.flip(frame, 1)                 # espejo → verse natural
        t = time.monotonic()
        fps = 0.9 * fps + 0.1 * (1.0 / max(t - t_prev, 1e-6)); t_prev = t
        _draw_overlay(frame, m, fps)
        _draw_guia_camara(frame, m)
        if cal is not None:
            h2 = frame.shape[0]
            cv2.putText(frame, f"CALIBRANDO... mira FIJO al lente ({cal['restantes']})",
                        (10, h2 - 16), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 0, 0), 4, cv2.LINE_AA)
            cv2.putText(frame, f"CALIBRANDO... mira FIJO al lente ({cal['restantes']})",
                        (10, h2 - 16), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (80, 230, 80), 2, cv2.LINE_AA)
        cv2.imshow(win, frame)
        k = cv2.waitKey(1) & 0xFF
        if k == ord("q") or cv2.getWindowProperty(win, cv2.WND_PROP_VISIBLE) < 1:
            break
        if k == ord("s"):
            snap = app_paths.MEDIA_DIR / f"cara_snapshot_{int(time.time())}.png"
            snap.parent.mkdir(exist_ok=True)
            cv2.imwrite(str(snap), frame)
            print(f"snapshot → {snap}")
        if k == ord("c") and cal is None:
            cal = {"muestras": [], "restantes": 40}    # ~1.3s a 30fps
            print("Calibrando centro: mirá FIJO al lente de la cámara…")
        if k == ord("r"):
            MIRA_ZONA.update(MIRA_ZONA_DEFAULT)
            OJOS_CAL.update(OJOS_CAL_DEFAULT)
            _guardar_mira_zona(); _cfg_guardar("ojos_cal", OJOS_CAL)
            print(f"zona y calibración de ojos reseteadas al default: {MIRA_ZONA}")
    cap.release()
    cv2.destroyAllWindows()


# ═══════════════════ pipeline OFFLINE (video → eventos, esquema "cara-v1") ═══════════════════
# Diseño consensuado con Codex/gpt-5.6-sol (3 rondas, 2026-07-14). Principios:
#   · Las CURVAS son internas; lo que se persiste son EVENTOS. La unidad es el PICO con
#     prominencia (no el "tramo sobre umbral": un tramo puede contener varios picos).
#   · El master.json recibe SOLO: reaccion (episodios con secuencia de ápices — el índice
#     que usa el orquestador), mirada y presencia (ausencias). Los eventos atómicos por
#     canal y las muestras completas van al sidecar <video>.cara.detalle.json (drill-down).
#   · <video>.facecam.json (región + bbox 1/s) es producción (reencuadre 9:16): NO va al master.
#   · Baseline robusto: mediana/MAD del propio video + piso de escala + condición dual
#     (z ≥ z_on Y activación absoluta mínima) → sin eventos fantasma en videos "planos".
#   · Canales de vocabulario CERRADO (simétricos fusionados antes). Sin canal "uff" en v1.
#   · 'frente' no genera eventos (es el estado default); 'camara' solo ≥0.7 s (intencional,
#     calibrado en vivo). Gaps de fuente cierran eventos (fin_causa) — nada los atraviesa.

PARAMS_ID = "cara-v1"
FPS_MUESTREO = 8.0
GAP_FUENTE_S = 2.5 / FPS_MUESTREO          # separación entre muestras > esto = gap de fuente
REACCION_GAP = 0.75                        # eventos a ≤ esto se agrupan en un episodio
PRESENCIA_DEBOUNCE = 0.50                  # s: parpadeos de detección no fragmentan presencia

# Canales cerrados: fórmula (sobre blendshapes fusionando simétricos, o derivadas de pose).
# piso = piso de la escala MAD · abs_min = activación absoluta mínima (condición dual).
CANALES = {
    "sonrisa":            dict(piso=0.02, abs_min=0.15, min_dur=0.50, merge=0.50, grupo="boca"),
    "cejas_arriba":       dict(piso=0.02, abs_min=0.12, min_dur=0.25, merge=0.50, grupo="cejas"),
    "ceno_fruncido":      dict(piso=0.02, abs_min=0.12, min_dur=0.50, merge=0.50, grupo="cejas"),
    "ojos_abiertos":      dict(piso=0.02, abs_min=0.12, min_dur=0.25, merge=0.25, grupo="ojos"),
    "ojos_cerrados":      dict(piso=0.03, abs_min=0.50, min_dur=0.50, merge=0.25, grupo="ojos"),
    "boca_abierta":       dict(piso=0.02, abs_min=0.18, min_dur=0.25, merge=0.50, grupo="boca"),
    "giro_cabeza":        dict(piso=3.0,  abs_min=18.0, min_dur=0.25, merge=0.25, grupo="cabeza"),
    "cabeceo":            dict(piso=3.0,  abs_min=15.0, min_dur=0.25, merge=0.25, grupo="cabeza"),
    "inclinacion_cabeza": dict(piso=3.0,  abs_min=15.0, min_dur=0.25, merge=0.25, grupo="cabeza"),
    "acercamiento":       dict(piso=0.02, abs_min=0.10, min_dur=0.25, merge=0.25, grupo="cabeza"),
}
Z_ON, Z_OFF, MIN_PROM_Z = 3.0, 1.5, 2.0
MOVIMIENTO = {"giro_cabeza", "cabeceo", "inclinacion_cabeza", "acercamiento"}
# Canales EMOCIONALES (EmotiEffLib) — misma maquinaria (z vs baseline propio + condición dual)
# pero VOCABULARIO APARTE: van al stream `emocion`, NO entran en reaccion ni en actividad
# (reaccion = músculos medidos; emocion = interpretación de un modelo — separar ambos niveles).
#   valencia_pos  = curva de valence      → picos de emoción POSITIVA (alegría, celebración)
#   valencia_neg  = curva de -valence     → picos de emoción NEGATIVA (frustración, tensión)
#   excitacion    = curva de arousal      → picos de ACTIVACIÓN (independiente del signo)
# Calibrado sobre el VOD real (2026-07-16): valence baseline -0.34/MAD 0.13 (la cara de
# concentración gamer lee levemente negativa), gol = +0.96 (z≈+7); arousal med +0.16/MAD
# 0.10/max +0.59 (rango comprimido). z_on/z_off POR CANAL (fix review Codex): con el z=3
# global, valencia_neg exigía valence ≤ -0.91 (inalcanzable: la mediana ya es negativa y el
# canal está invertido) y excitacion ≤ nunca (0.62 > max medido). En estos canales el umbral
# ABSOLUTO (abs_min — la escala [-1,1] del modelo es calibrada, a diferencia de los
# blendshapes) hace el trabajo duro y el z bajo solo exige "inusual para este video".
CANALES_EMO = {
    "valencia_pos": dict(piso=0.05, abs_min=0.20, min_dur=0.50, merge=0.75),
    "valencia_neg": dict(piso=0.05, abs_min=0.45, min_dur=0.50, merge=0.75, z_on=1.8, z_off=1.0),
    "excitacion":   dict(piso=0.05, abs_min=0.35, min_dur=0.50, merge=0.75, z_on=2.5, z_off=1.25),
}
EMO_SUAV_K = 5                    # mediana móvil (muestras): la emoción por frame es más
                                  # ruidosa que los blendshapes → ventana de ~0.6s a 8fps
# mirada: (min_dur, merge_gap) por dirección. 'camara' es dura a propósito (intencional).
MIRADA_CFG = {"camara": (0.70, 0.25), "izquierda": (0.50, 0.50), "derecha": (0.50, 0.50),
              "arriba": (0.50, 0.50), "abajo": (0.50, 0.50)}
# OFFLINE: los lados/vertical se clasifican RELATIVOS a la pose neutral del video (mediana).
# Medido en el VOD real: la posición de la cámara cambia por sesión (neutral yaw +24°) y los
# umbrales absolutos calibrados en vivo daban 141 falsos "izquierda". 'camara' sigue ABSOLUTA
# (mirar la cámara física es yaw≈0/pitch≈0, no importa el neutral). Deltas simétricos: la
# asimetría de los umbrales del live compensaba justamente ese corrimiento del neutral.
MIRADA_REL = {"lado": 18.0, "arriba": 14.0, "abajo": 18.0}


def clasificar_mirada_rel(yaw: float, pitch: float, yaw0: float, pitch0: float,
                          ojos: tuple | None = None) -> str:
    """Clasificación OFFLINE: 'camara' absoluta (con veto ocular si hay lectura de ojos);
    izquierda/derecha/arriba/abajo relativas a la pose neutral (yaw0/pitch0 = medianas)."""
    if _es_camara(yaw, pitch, ojos):
        return "camara"
    dy, dp = yaw - yaw0, pitch - pitch0
    der = (dy > 0) == YAW_POSITIVO_ES_DERECHA
    if abs(dy) >= MIRADA_REL["lado"] and abs(dy) >= abs(dp):
        return "derecha" if der else "izquierda"
    if dp <= -MIRADA_REL["arriba"]:
        return "arriba"
    if dp >= MIRADA_REL["abajo"]:
        return "abajo"
    return "frente"


def _run_flags() -> dict:
    """Flags de subprocess de medios (CREATE_NO_WINDOW en Windows): sin esto, cada
    ffprobe/ffmpeg del análisis offline FLASHEA una consola cuando la app corre como
    GUI (auditoría Windows pre-ship). Con fallback vacío para el uso CLI standalone."""
    try:
        import medios
        return medios.flags_subprocess()
    except ImportError:
        return {}


def _ffprobe_dur(video) -> float:
    import subprocess
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                        "-of", "csv=p=0", str(video)], capture_output=True, text=True,
                       timeout=30, **_run_flags())
    return float(r.stdout.strip())


def _leer_frame(video, t: float):
    """UN frame RGB del video en el instante t (para buscar la región de la facecam)."""
    import subprocess
    import numpy as np
    import cv2
    r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0",
                        "-show_entries", "stream=width,height", "-of", "csv=p=0", str(video)],
                       capture_output=True, text=True, timeout=30, **_run_flags())
    w, h = (int(x) for x in r.stdout.strip().split(",")[:2])
    p = subprocess.run(["ffmpeg", "-v", "error", "-ss", f"{t:.2f}", "-i", str(video),
                        "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
                       capture_output=True, timeout=120, **_run_flags())
    if len(p.stdout) < w * h * 3:
        return None
    return np.frombuffer(p.stdout[:w * h * 3], dtype=np.uint8).reshape(h, w, 3)


def encontrar_region(video, n=9, log_cb=None) -> tuple[int, int, int, int] | None:
    """Región de la facecam para TODO el video: muestrea `n` frames repartidos, corre
    `buscar_region` en cada uno y toma la mediana de las cajas encontradas (+15% de margen).
    La facecam no se mueve → se busca UNA vez y se fija (queda registrada en facecam.json)."""
    import numpy as np
    dur = _ffprobe_dur(video)
    cajas = []
    for f in np.linspace(0.05, 0.95, n):
        rgb = _leer_frame(video, f * dur)
        if rgb is None:
            continue
        r = buscar_region(rgb)
        if r:
            cajas.append(r)
    if not cajas:
        return None
    c = np.median(np.array(cajas), axis=0)
    mx, my = (c[2] - c[0]) * 0.15, (c[3] - c[1]) * 0.15
    h, w = rgb.shape[:2]
    reg = (max(0, int(c[0] - mx)), max(0, int(c[1] - my)),
           min(w, int(c[2] + mx)), min(h, int(c[3] + my)))
    if log_cb:
        log_cb(f"Facecam encontrada en {len(cajas)}/{n} frames → región {reg}")
    return reg


def _muestrear(video, region, fps, dur, cancel=None, log_cb=None, progress_cb=None,
               emo=False) -> list[dict]:
    """Muestrea el video a `fps` recortado a la región de la facecam (ffmpeg → rawvideo por
    pipe: decodifica una sola vez, sin archivos temporales) y corre el Face Landmarker en
    modo VIDEO (tracking). t = n/fps sobre la grilla uniforme del filtro fps de ffmpeg
    (desvío < 1/(2·fps) respecto del PTS real — suficiente y documentado).
    Con `emo=True` corre además EmotiEffLib sobre el bbox de cada muestra con cara
    (~10-20 ms extra por muestra en CPU) → claves emocion/emo_p/valencia/excitacion."""
    import subprocess
    import numpy as np
    import mediapipe as mp
    x0, y0, x1, y1 = region
    w, h = x1 - x0, y1 - y0
    det = _load("VIDEO")
    cmd = ["ffmpeg", "-v", "error", "-i", str(video),
           "-vf", f"fps={fps},crop={w}:{h}:{x0}:{y0}",
           "-f", "rawvideo", "-pix_fmt", "rgb24", "-"]
    try:
        # flags Windows (sin ventana de consola) + Job Object: si la GUI muere, el SO
        # mata este ffmpeg largo (antes quedaba huérfano — review ronda 3, h.9)
        import medios
        proc = medios.popen_gestionado(cmd, stdout=subprocess.PIPE, bufsize=w * h * 3 * 8)
    except ImportError:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, bufsize=w * h * 3 * 8)
    muestras, n, total = [], 0, int(dur * fps)
    emo_fallos = 0                     # errores de inferencia de emoción (tolerados por muestra)
    try:
        while True:
            if cancel is not None and cancel.is_set():
                proc.kill()
                return []
            buf = proc.stdout.read(w * h * 3)
            if len(buf) < w * h * 3:
                break
            t = n / fps
            frame = np.frombuffer(buf, dtype=np.uint8).reshape(h, w, 3)
            res = det.detect_for_video(mp.Image(image_format=mp.ImageFormat.SRGB, data=frame),
                                       int(t * 1000))
            m = medidas(res, w, h)
            m["t"] = round(t, 3)
            if emo and m.get("bbox"):            # emoción sobre el bbox LOCAL (frame = recorte)
                try:                             # un fallo de emoción NUNCA tumba el pipeline
                    e = emocion_en_crop(frame, m["bbox"])
                    if e:
                        m.update(e)
                except Exception as ex:          # (fix review Codex 2026-07-16)
                    emo_fallos += 1
                    if emo_fallos >= 8:
                        emo = False
                        if log_cb:
                            log_cb(f"⚠ EmotiEffLib falló {emo_fallos} veces ({ex}) — "
                                   "sigo sin emociones de acá en adelante.")
            if m.get("bbox"):                    # bbox → coords del frame COMPLETO
                m["bbox"] = [m["bbox"][0] + x0, m["bbox"][1] + y0,
                             m["bbox"][2] + x0, m["bbox"][3] + y0]
            muestras.append(m)
            n += 1
            if progress_cb and total and n % 40 == 0:
                progress_cb(min(n / total, 1.0), None)
            if log_cb and n % 2400 == 0:
                log_cb(f"Analizando cara… {n}/{total} muestras ({n / fps:.0f}s de video)")
    finally:
        try:
            proc.stdout.close(); proc.wait(timeout=5)
        except Exception:
            proc.kill()
    return muestras


# --------------------------------------------------- curvas, baseline y eventos --
def _curvas(muestras) -> tuple:
    """Muestras → curvas por canal (np, NaN donde no hay cara) + arrays de t/validez."""
    import numpy as np
    n = len(muestras)
    ts = np.array([m["t"] for m in muestras])
    ok = np.array([bool(m.get("presente")) for m in muestras])
    yaw = np.array([m.get("yaw", np.nan) if k else np.nan for m, k in zip(muestras, ok)])
    pit = np.array([m.get("pitch", np.nan) if k else np.nan for m, k in zip(muestras, ok)])
    rol = np.array([m.get("roll", np.nan) if k else np.nan for m, k in zip(muestras, ok)])
    tam = np.array([m.get("tam", np.nan) if k else np.nan for m, k in zip(muestras, ok)])
    ojx = np.array([(m.get("ojos") or (np.nan, np.nan))[0] if k else np.nan
                    for m, k in zip(muestras, ok)])
    ojy = np.array([(m.get("ojos") or (np.nan, np.nan))[1] if k else np.nan
                    for m, k in zip(muestras, ok)])

    def bs(nombre):
        return np.array([(m.get("blendshapes") or {}).get(nombre, np.nan) if k else np.nan
                         for m, k in zip(muestras, ok)])

    def deriv(x):
        """|dx/dt| por diferencia entre muestras consecutivas VÁLIDAS (NaN si no)."""
        d = np.full(n, np.nan)
        dx, dt = np.diff(x), np.diff(ts)
        v = ~np.isnan(dx) & (dt > 0) & (dt <= GAP_FUENTE_S)
        d[1:][v] = np.abs(dx[v] / dt[v])
        return d

    def suav(x, k=3):
        """Mediana móvil corta (mata el chatter de 1 frame sin comerse los ápices)."""
        y = x.copy()
        for i in range(n):
            seg = x[max(0, i - k // 2):i + k // 2 + 1]
            seg = seg[~np.isnan(seg)]
            if len(seg):
                y[i] = np.median(seg)
        return y

    curvas = {
        "sonrisa": suav((bs("mouthSmileLeft") + bs("mouthSmileRight")) / 2),
        "cejas_arriba": suav(np.fmax(bs("browInnerUp"),
                                     (bs("browOuterUpLeft") + bs("browOuterUpRight")) / 2)),
        "ceno_fruncido": suav((bs("browDownLeft") + bs("browDownRight")) / 2),
        "ojos_abiertos": suav((bs("eyeWideLeft") + bs("eyeWideRight")) / 2),
        "ojos_cerrados": suav((bs("eyeBlinkLeft") + bs("eyeBlinkRight")) / 2),
        "boca_abierta": suav(bs("jawOpen")),
        "giro_cabeza": suav(deriv(yaw)),
        "cabeceo": suav(deriv(pit)),
        "inclinacion_cabeza": suav(deriv(rol)),
        "acercamiento": suav(deriv(np.log(np.clip(tam, 1e-4, None)))),
    }
    return ts, ok, curvas, (yaw, pit, rol, tam, ojx, ojy)


def _baseline(curvas, canales=None) -> dict:
    """Mediana/MAD robustos por canal (solo muestras válidas) + p95, con piso de escala."""
    import numpy as np
    canales = canales or CANALES
    base = {}
    for c, x in curvas.items():
        v = x[~np.isnan(x)]
        if not len(v):
            base[c] = {"mediana": 0.0, "escala": canales[c]["piso"], "p95": 0.0}
            continue
        med = float(np.median(v))
        mad = float(np.median(np.abs(v - med)))
        base[c] = {"mediana": round(med, 4),
                   "escala": round(max(1.4826 * mad, canales[c]["piso"]), 4),
                   "p95": round(float(np.percentile(v, 95)), 4)}
    return base


def _eventos_canal(ts, vals, z, canal, cfg=None) -> list[dict]:
    """Eventos atómicos de un canal: regiones por histéresis (cortadas en gaps) → merge de
    regiones cercanas → PICOS con prominencia dentro de cada región (un pico = un evento;
    la frontera entre picos es el mínimo entre ellos)."""
    import numpy as np
    from scipy.signal import find_peaks
    cfg = cfg or CANALES[canal]
    z_on, z_off = cfg.get("z_on", Z_ON), cfg.get("z_off", Z_OFF)
    n, out = len(ts), []
    valido = ~np.isnan(z)
    regs, i = [], 0
    while i < n:
        if valido[i] and z[i] >= z_on and vals[i] >= cfg["abs_min"]:
            j = i
            while (j + 1 < n and valido[j + 1] and z[j + 1] > z_off
                   and ts[j + 1] - ts[j] <= GAP_FUENTE_S):
                j += 1
            gap_fin = j + 1 < n and (not valido[j + 1] or ts[j + 1] - ts[j] > GAP_FUENTE_S)
            regs.append([i, j, gap_fin])
            i = j + 1
        else:
            i += 1
    fus = []
    for r in regs:                              # merge de regiones separadas < merge_gap
        if fus and not fus[-1][2] and ts[r[0]] - ts[fus[-1][1]] <= cfg["merge"]:
            fus[-1][1], fus[-1][2] = r[1], r[2]
        else:
            fus.append(r)
    for a, b, gap_fin in fus:
        seg = z[a:b + 1]
        pk, props = (find_peaks(seg, prominence=MIN_PROM_Z) if len(seg) >= 3
                     else (np.array([], int), {}))
        if len(pk) == 0:
            pk = np.array([int(np.argmax(seg))])
            proms = [round(float(np.nanmax(seg) - z_off), 2)]
        else:
            proms = [round(float(p), 2) for p in props["prominences"]]
        cortes = [a]
        for p1, p2 in zip(pk[:-1], pk[1:]):
            cortes.append(a + p1 + int(np.argmin(seg[p1:p2 + 1])))
        cortes.append(b)
        for k, (p, prom) in enumerate(zip(pk, proms)):
            i0, i1, ip = cortes[k], cortes[k + 1], a + int(p)
            if ts[i1] - ts[i0] < cfg["min_dur"] - 1e-9:
                continue
            tramo = slice(i0, i1 + 1)
            e = {"t_ini": round(ts[i0], 2), "t_fin": round(ts[i1], 2), "canal": canal,
                 "t_pico": round(ts[ip], 2),
                 "valor_max": round(float(np.nanmax(vals[tramo])), 2),
                 "z_max": round(float(z[ip]), 2), "prom_z": prom,
                 "area_z": round(float(np.nansum(np.clip(z[tramo] - z_on, 0, None))
                                 / FPS_MUESTREO), 2),
                 "subida": round(ts[ip] - ts[i0], 2)}
            if gap_fin and i1 == b:
                e["fin_causa"] = "gap_fuente"
            out.append(e)
    return out


def _curvas_emo(muestras) -> tuple:
    """Muestras → curvas de los canales EMOCIONALES (NaN donde no hay cara/emoción) +
    etiquetas/probs por muestra (para etiquetar los picos). Mediana móvil EMO_SUAV_K."""
    import numpy as np
    n = len(muestras)
    val = np.array([m.get("valencia", np.nan) for m in muestras], dtype=float)
    aro = np.array([m.get("excitacion", np.nan) for m in muestras], dtype=float)
    lab = [m.get("emocion", "") for m in muestras]
    pro = [m.get("emo_p", 0.0) for m in muestras]

    def puentea(x, max_hueco=2):
        """Interpola huecos de NaN de ≤ max_hueco muestras (parpadeos de detección de 1-2
        frames — mismo espíritu que PRESENCIA_DEBOUNCE; medido en el VOD real: 1 muestra
        por motion blur en plena celebración). Huecos más largos = ausencia real → NaN."""
        y = x.copy()
        i = 0
        while i < n:
            if np.isnan(y[i]):
                j = i
                while j < n and np.isnan(y[j]):
                    j += 1
                if 0 < i and j < n and (j - i) <= max_hueco:
                    y[i:j] = np.interp(np.arange(i, j), [i - 1, j], [y[i - 1], y[j]])
                i = j
            else:
                i += 1
        return y

    def suav(x, k=EMO_SUAV_K):
        xp = puentea(x)
        y = xp.copy()
        for i in range(n):
            seg = xp[max(0, i - k // 2):i + k // 2 + 1]
            seg = seg[~np.isnan(seg)]
            if len(seg):
                y[i] = np.median(seg)
        y[np.isnan(xp)] = np.nan    # el suavizado NO inventa muestras donde no hubo cara/
        return y                    # emoción — solo puentea parpadeos (fix Codex 2026-07-16)

    curvas = {"valencia_pos": suav(val), "valencia_neg": suav(-val), "excitacion": suav(aro)}
    return curvas, lab, pro, (val, aro)


def _eventos_emocion(ts, curvas_emo, base_emo, lab, pro, crudas) -> list[dict]:
    """Stream `emocion`: eventos por pico de los 3 canales emocionales, cada uno etiquetado
    con la CLASE dominante de AffectNet en el EXTREMO CRUDO del evento (emocion/emo_p) — el
    frame que aportó la evidencia, no el pico suavizado (fix review Codex 2026-07-16). La
    etiqueta contextualiza el pico dimensional ('este pico de excitación fue Surprise'), no
    es verdad frame a frame.
    NOTA valor_max: en valencia_neg la curva es -valence → valor_max 0.52 = valence -0.52."""
    import numpy as np
    val, aro = crudas
    CRUDA = {"valencia_pos": val, "valencia_neg": -val, "excitacion": aro}
    out = []
    for c, cfg in CANALES_EMO.items():
        x = curvas_emo[c]
        z = (x - base_emo[c]["mediana"]) / base_emo[c]["escala"]
        cruda = CRUDA[c]
        for e in _eventos_canal(ts, x, z, c, cfg=cfg):
            # índices del evento por muestra MÁS CERCANA (t_ini/t_fin vienen redondeados a
            # 2 decimales — un searchsorted directo puede caer en la muestra vecina)
            i0 = int(np.argmin(np.abs(ts - e["t_ini"])))
            i1 = int(np.argmin(np.abs(ts - e["t_fin"])))
            seg = cruda[i0:i1 + 1]
            if np.any(~np.isnan(seg)):
                ip = i0 + int(np.nanargmax(seg))
                if lab[ip]:
                    e["emocion"], e["emo_p"] = lab[ip], pro[ip]
            out.append(e)
    return sorted(out, key=lambda e: e["t_ini"])


def _actividad(curvas, base) -> "np.ndarray":
    """Actividad facial 0-1 por muestra: score=clip(z/8,0,1) por canal → máx por GRUPO
    (boca/cejas/ojos/cabeza, para no sobrecontar canales correlacionados) → media de grupos."""
    import numpy as np
    grupos = {}
    for c, x in curvas.items():
        zc = (x - base[c]["mediana"]) / base[c]["escala"]
        s = np.clip(np.nan_to_num(zc, nan=0.0) / 8.0, 0, 1)
        g = CANALES[c]["grupo"]
        grupos[g] = np.fmax(grupos[g], s) if g in grupos else s
    return np.mean(np.stack(list(grupos.values())), axis=0)


def _eventos_mirada(ts, ok, yaw, pit, ojx=None, ojy=None) -> list[dict]:
    """Eventos de mirada: clasificación por muestra (RELATIVA a la pose neutral del video,
    ver clasificar_mirada_rel; 'camara' con veto ocular si hay lectura de iris) → corridas
    del mismo estado → merge de interrupciones cortas → min_dur por dirección. 'frente' NO
    se emite (estado default)."""
    import numpy as np
    n = len(ts)
    yaw0, pit0 = float(np.nanmedian(yaw)), float(np.nanmedian(pit))
    lab = []
    for i in range(n):
        if ok[i] and not np.isnan(yaw[i]):
            ojos = None
            if ojx is not None and not np.isnan(ojx[i]):
                ojos = (float(ojx[i]), float(ojy[i]))
            lab.append(clasificar_mirada_rel(float(yaw[i]), float(pit[i]), yaw0, pit0, ojos))
        else:
            lab.append("")
    runs, i = [], 0
    while i < n:
        j = i
        while j + 1 < n and lab[j + 1] == lab[i] and ts[j + 1] - ts[j] <= GAP_FUENTE_S:
            j += 1
        runs.append((lab[i], i, j))
        i = j + 1
    out = []
    for d, (min_dur, merge) in MIRADA_CFG.items():
        evs = [[a, b] for l, a, b in runs if l == d]
        fus = []
        for r in evs:
            if fus and ts[r[0]] - ts[fus[-1][1]] <= merge:
                fus[-1][1] = r[1]
            else:
                fus.append(r)
        for a, b in fus:
            if ts[b] - ts[a] >= min_dur:
                out.append({"t_ini": round(ts[a], 2), "t_fin": round(ts[b], 2), "direccion": d})
    return sorted(out, key=lambda e: e["t_ini"])


def _ausencias(ts, ok, dur_video) -> list[dict]:
    """Presencia: solo se guardan las AUSENCIAS (la presencia es el estado implícito).
    Debounce: corridas < PRESENCIA_DEBOUNCE no cambian el estado."""
    n = len(ts)
    runs, i = [], 0
    while i < n:
        j = i
        while j + 1 < n and ok[j + 1] == ok[i]:
            j += 1
        runs.append([bool(ok[i]), i, j])
        i = j + 1
    fus = [runs[0]] if runs else []
    for r in runs[1:]:                         # debounce: absorber corridas cortas
        if ts[r[2]] - ts[r[1]] < PRESENCIA_DEBOUNCE and fus:
            fus[-1][2] = r[2]
        elif fus and r[0] == fus[-1][0]:
            fus[-1][2] = r[2]
        else:
            fus.append(r)
    out = []
    for pres, a, b in fus:
        if not pres:
            fin = round(ts[b], 2)
            out.append({"t_ini": round(ts[a], 2), "t_fin": fin, "estado": "ausente",
                        "causa": "no_deteccion",
                        "fin_causa": "fin_video" if b == n - 1 else "reaparicion"})
    return out


def _episodios(atomicos, ts, actividad) -> list[dict]:
    """`video.cara.reaccion`: agrupa eventos atómicos separados ≤ REACCION_GAP en episodios
    con la SECUENCIA ordenada de ápices — el índice facial que ve el orquestador. Compresión
    estructural, no interpretación: conserva el arco (cejas→boca→sonrisa) explícito."""
    import numpy as np
    if not atomicos:
        return []
    evs = sorted(atomicos, key=lambda e: e["t_ini"])
    grupos, cur = [], [evs[0]]
    for e in evs[1:]:
        if e["t_ini"] <= max(x["t_fin"] for x in cur) + REACCION_GAP:
            cur.append(e)
        else:
            grupos.append(cur); cur = [e]
    grupos.append(cur)
    out = []
    for g in grupos:
        t0, t1 = min(e["t_ini"] for e in g), max(e["t_fin"] for e in g)
        i0, i1 = np.searchsorted(ts, t0), np.searchsorted(ts, t1, side="right")
        act = actividad[i0:max(i1, i0 + 1)]
        ip = i0 + int(np.argmax(act)) if len(act) else i0
        ep = {"t_ini": t0, "t_fin": t1, "t_pico": round(float(ts[min(ip, len(ts) - 1)]), 2),
              "z_max": max(e["z_max"] for e in g),
              "actividad_max": round(float(np.max(act)) if len(act) else 0.0, 2),
              "apices": [{"t": e["t_pico"], "canal": e["canal"], "z": e["z_max"]}
                         for e in sorted(g, key=lambda x: x["t_pico"])]}
        if any(e.get("fin_causa") for e in g):
            ep["fin_causa"] = "gap_fuente"
        out.append(ep)
    return out


def analizar_video(video, *, outdir=None, fps=FPS_MUESTREO, cancel=None,
                   log_cb=None, progress_cb=None, emociones=True,
                   region: tuple | None = None) -> dict | None:
    """Pipeline offline completo: video → región de facecam → muestreo 8 fps → curvas →
    eventos → escribe <stem>.cara.json (streams del master: reaccion/mirada/presencia +
    emocion si EmotiEffLib está disponible) + <stem>.cara.detalle.json (muestras + eventos
    atómicos) + <stem>.facecam.json (región y bbox 1/s, producción). Cancelado ⇒ NO escribe
    nada (no pisar datos buenos con parciales). `emociones=False` lo apaga aunque esté
    instalado. Devuelve el dict del cara.json, o None si se canceló / no hay cara."""
    import numpy as np

    def log(m):
        if log_cb:
            log_cb(m)

    # releer la calibración (zona a-cámara + sesgo de ojos): pudo editarse en la ventana
    # --live (subproceso) DESPUÉS de que la GUI importó este módulo — así el análisis
    # siempre usa la última
    MIRA_ZONA.update(_cargar_mira_zona())
    OJOS_CAL.update(_cfg_leer("ojos_cal", OJOS_CAL_DEFAULT))
    video = Path(video)
    out_dir = Path(outdir) if outdir else video.parent
    out_dir.mkdir(parents=True, exist_ok=True)
    dur = _ffprobe_dur(video)
    log("Zona a-cámara calibrada: " + " ".join(f"{k}={v:+.1f}°" for k, v in MIRA_ZONA.items()))
    region_fuente = "usuario" if region is not None else "auto"
    if region is not None:
        # región elegida por el USUARIO (rect del wizard) — validar y usar tal cual,
        # sin autodetección. (x0, y0, x1, y1) en px del frame original.
        x0, y0, x1, y1 = (int(v) for v in region)
        if x1 - x0 < 32 or y1 - y0 < 32:
            log(f"⚠ La región indicada es demasiado chica ({x1 - x0}×{y1 - y0}px).")
            return None
        region = (x0, y0, x1, y1)
        log(f"Video: {video.name} · {dur:.0f}s · región de facecam del usuario: {region}")
    else:
        log(f"Video: {video.name} · {dur:.0f}s · buscando la facecam…")
        region = encontrar_region(video, log_cb=log)
        if region is None:
            log("⚠ No encontré ninguna cara en el video — no hay nada que analizar.")
            return None
    emo = False
    if emociones and emocion_available():
        try:
            _load_emo()                            # carga/descarga acá → error visible y temprano
            emo = True
            log(f"Emociones: EmotiEffLib {EMO_MODEL} (ONNX/CPU) sobre cada muestra.")
        except Exception as e:
            log(f"⚠ EmotiEffLib no cargó ({e}) — sigo sin emociones.")
    elif emociones:
        log("Emociones: emotiefflib no está instalado (pip install --no-deps emotiefflib) — sigo sin.")
    muestras = _muestrear(video, region, fps, dur, cancel=cancel, log_cb=log,
                          progress_cb=progress_cb, emo=emo)
    if cancel is not None and cancel.is_set():
        log("⏹ Cancelado — no se guardó nada.")
        return None
    if not muestras:
        log("⚠ El muestreo no produjo frames.")
        return None
    ts, ok, curvas, (yaw, pit, rol, tam, ojx, ojy) = _curvas(muestras)
    base = _baseline(curvas)
    log(f"{len(muestras)} muestras · {int(100 * ok.mean())}% con cara · armando eventos…")

    atomicos = []
    for c in CANALES:
        z = (curvas[c] - base[c]["mediana"]) / base[c]["escala"]
        atomicos += _eventos_canal(ts, curvas[c], z, c)
    actividad = _actividad(curvas, base)
    reaccion = _episodios(atomicos, ts, actividad)
    mirada = _eventos_mirada(ts, ok, yaw, pit, ojx, ojy)
    presencia = _ausencias(ts, ok, dur)
    emocion, base_emo = [], {}
    emo = emo and any("valencia" in m for m in muestras)   # pudo degradarse en _muestrear
    if emo:
        curvas_e, emo_lab, emo_pro, crudas = _curvas_emo(muestras)
        base_emo = _baseline(curvas_e, CANALES_EMO)
        emocion = _eventos_emocion(ts, curvas_e, base_emo, emo_lab, emo_pro, crudas)
    expresion = sorted((e for e in atomicos if e["canal"] not in MOVIMIENTO),
                       key=lambda e: e["t_ini"])
    movimiento = sorted((e for e in atomicos if e["canal"] in MOVIMIENTO),
                        key=lambda e: e["t_ini"])

    import mediapipe
    extractor = {"nombre": "mediapipe_face_landmarker", "version": mediapipe.__version__,
                 "modelo": MODEL_PATH.name}
    if emo:
        import emotiefflib
        extractor["emocion"] = {"nombre": "emotiefflib", "modelo": EMO_MODEL,
                                "engine": "onnx",
                                "version": getattr(emotiefflib, "__version__", "?")}
    header = {
        "schema_version": 1, "video": video.name, "params_id": PARAMS_ID,
        "extractor": extractor,
        "fps_muestreo": fps,
        "fps_efectivo": round((len(ts) - 1) / max(ts[-1] - ts[0], 1e-6), 2),
        "cobertura": round(float(ok.mean()), 3),
        **({"cobertura_emocion": round(sum("valencia" in m for m in muestras)
                                       / max(int(ok.sum()), 1), 3)} if emo else {}),
        "fuente_mirada": "pose_cabeza+iris",
        "region_fuente": region_fuente,            # "usuario" (rect del wizard) o "auto"
        "mirada_preset": mira_presets_listar()[1],
        "pose_neutral": {"yaw_med": round(float(np.nanmedian(yaw)), 1),
                         "pitch_med": round(float(np.nanmedian(pit)), 1),
                         "roll_med": round(float(np.nanmedian(rol)), 1)},
        "baseline": {**base, **base_emo},
        "params": {"z_on": Z_ON, "z_off": Z_OFF, "min_prom_z": MIN_PROM_Z,
                   "reaccion_gap": REACCION_GAP, "canales": CANALES, "mirada": MIRADA_CFG,
                   "mirada_rel": MIRADA_REL,
                   "mira_zona": {k: round(float(v), 1) for k, v in MIRA_ZONA.items()},
                   "ojos": {"a_grados": OJOS_A_GRADOS, "parpadeo_max": OJOS_PARPADEO_MAX,
                            "gan_arriba": OJOS_GAN_ARRIBA, "gan_abajo": OJOS_GAN_ABAJO,
                            "cal": dict(OJOS_CAL)},
                   **({"canales_emo": CANALES_EMO, "emo_suav_k": EMO_SUAV_K} if emo else {})},
        "sidecars": {"detalle": f"{video.stem}.cara.detalle.json",
                     "reencuadre": f"{video.stem}.facecam.json"},
    }

    streams = {"reaccion": reaccion, "mirada": mirada, "presencia": presencia}
    if emo:
        streams["emocion"] = emocion               # → video.cara.emocion en el master
    cara_json = {"header": header, "streams": streams}
    outp = out_dir / f"{video.stem}.cara.json"
    outp.write_text(json.dumps(cara_json, ensure_ascii=False, indent=1), encoding="utf-8")

    def _lite(m):
        d = {"t": m["t"], "presente": m.get("presente", False)}
        if d["presente"]:
            d.update(tam=round(m["tam"], 3), yaw=m.get("yaw"), pitch=m.get("pitch"),
                     roll=m.get("roll"),
                     blendshapes={k: round(v, 2) for k, v in (m.get("blendshapes") or {}).items()
                                  if v >= 0.01})
            if "ojos" in m:
                d["ojos"] = m["ojos"]
            if "valencia" in m:
                d.update(valencia=m["valencia"], excitacion=m["excitacion"],
                         emocion=m["emocion"], emo_p=m["emo_p"])
        return d
    detalle = {"header": header,
               "muestras": [_lite(m) for m in muestras],
               "expresion": expresion, "movimiento": movimiento}
    (out_dir / f"{video.stem}.cara.detalle.json").write_text(
        json.dumps(detalle, ensure_ascii=False), encoding="utf-8")

    paso = max(1, int(fps))                    # bbox a ~1/s para el reencuadre 9:16
    facecam = {"video": video.name, "region": list(region),
               "muestras": [{"t": m["t"], "bbox": m["bbox"]}
                            for m in muestras[::paso] if m.get("presente")]}
    (out_dir / f"{video.stem}.facecam.json").write_text(
        json.dumps(facecam, ensure_ascii=False), encoding="utf-8")

    log(f"Listo → {outp.name}: {len(reaccion)} reacciones · {len(mirada)} miradas · "
        f"{len(presencia)} ausencias"
        + (f" · {len(emocion)} emociones" if emo else "")
        + f" · (detalle: {len(expresion)}+{len(movimiento)} eventos "
        f"atómicos, {len(muestras)} muestras)")
    return cara_json


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="Análisis de cara (MediaPipe Face Landmarker).")
    ap.add_argument("--live", action="store_true", help="prueba en vivo con la cámara")
    ap.add_argument("--cam", type=int, default=0, help="índice de cámara (default 0)")
    ap.add_argument("--frame", default=None, help="analizar UNA imagen y imprimir el JSON")
    ap.add_argument("--video", default=None, help="pipeline offline: video → cara.json + sidecars")
    ap.add_argument("--outdir", default=None, help="carpeta de salida (default: junto al video)")
    ap.add_argument("--sin-emociones", action="store_true",
                    help="no correr EmotiEffLib aunque esté instalado")
    a = ap.parse_args()
    if a.frame:
        print(json.dumps(analizar_imagen(a.frame, emociones=not a.sin_emociones),
                         ensure_ascii=False, indent=1))
    elif a.video:
        analizar_video(a.video, outdir=a.outdir, log_cb=print, emociones=not a.sin_emociones)
    elif a.live:
        live(cam=a.cam, emociones=not a.sin_emociones)
    else:
        ap.print_help()
