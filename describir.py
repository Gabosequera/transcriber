#!/usr/bin/env python3
"""
describir.py — CAPA 3 del bloque audio-del-juego: describe cada ESCENA (segmento de la
Capa 2) con un modelo audio-lenguaje (LALM) vía llama.cpp, con salida estructurada
anclada a los tags de la Capa 1.

Diseño:
  · BACKEND ENCHUFABLE / HOT-SWITCH — el modelo NO está hardcodeado: hay un `MODELS`
    registry con perfiles (3B para verificar / 8GB VRAM, 30B para modo calidad en una
    máquina con RAM). Cambiás de modelo pasando otro nombre; el mismo código sirve en el
    laptop (CPU) y en una PC con GPU (offload por `n_gpu_layers`). Portátil porque llama.cpp
    (GGUF) está DESACOPLADO de los pins de torch/transformers del resto del proyecto.
  · POR SEGMENTO, no por ventana fija — describe cada escena homogénea de la Capa 2 →
    cobertura total con costo proporcional a lo que pasa.
  · ANCLADO A LOS TAGS (candado #2) — al modelo se le pasan los tags de la Capa 1 de ese
    segmento como contexto y se le pide JSON con enums. Los tags son la VERDAD; la
    descripción agrega color. Los timestamps los pone el pipeline (la ventana), no el modelo.
  · OMNI = audio Y video — estos modelos también entienden imágenes/cuadros. Hoy pasamos
    solo audio; la interfaz deja lugar para sumar cuadros muestreados por escena después.

Requiere: un binario de llama.cpp con soporte de audio (`llama-server`/`llama-mtmd-cli`,
build de abril-2026+) y el GGUF del modelo + su mmproj. NO toca el core ni los pins.

ESTADO: estructura + backend escritos contra la API documentada de llama-server
(OpenAI /chat/completions con `input_audio`). Falta el smoke test empírico que confirme
la invocación exacta en esta máquina (siguiente paso).
"""
from __future__ import annotations

import base64
import json
import os
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path

import numpy as np
import soundfile as sf

import hardware
import app_paths

PROJ = app_paths.SOURCE_DIR
MODELS_DIR = app_paths.MODELS_DIR

# --------------------------------------------------------- registry de modelos --
# Perfiles para hot-switch. `model`/`mmproj` son nombres de archivo dentro de MODELS_DIR
# (o se bajan del `repo` de HF la 1ª vez). `n_gpu_layers`: 0 = todo CPU; -1 = todo a la
# GPU si entra; un número = offload parcial (para la placa de 8GB con el 30B).
MODELS = {
    "qwen2.5-omni-3b": {
        "repo": "ggml-org/Qwen2.5-Omni-3B-GGUF",
        "model": "Qwen2.5-Omni-3B-Q4_K_M.gguf",       # ~2.1 GB
        "mmproj": "mmproj-Qwen2.5-Omni-3B-Q8_0.gguf",
        "ctx": 8192, "n_gpu_layers": 0,
        "nota": "chico — verificación / laptop CPU / entra sobrado en 8GB VRAM",
    },
    "qwen2.5-omni-7b": {
        "repo": "ggml-org/Qwen2.5-Omni-7B-GGUF",
        "model": "Qwen2.5-Omni-7B-Q4_K_M.gguf",        # ~4.5 GB (verificado en HF)
        "mmproj": "mmproj-Qwen2.5-Omni-7B-Q8_0.gguf",
        "ctx": 8192, "n_gpu_layers": -1,
        "nota": "default portátil — entra ENTERO en 8GB VRAM (Q4)",
    },
    "qwen3-omni-30b": {
        "repo": "ggml-org/Qwen3-Omni-30B-A3B-Instruct-GGUF",
        "model": "Qwen3-Omni-30B-A3B-Instruct-Q4_K_M.gguf",   # verificado en HF
        "mmproj": "mmproj-Qwen3-Omni-30B-A3B-Instruct-Q8_0.gguf",
        "ctx": 8192, "n_gpu_layers": 0,
        "nota": "modo calidad — MoE 3B activos → CPU-rápido con RAM (laptop 64GB)",
    },
}
DEFAULT_MODEL = "qwen2.5-omni-3b"

# -------------------------------------- modelos ONLINE (OpenRouter + Alibaba) ----
# La MISMA capa 3 puede correr contra una API en vez del llama.cpp local (misma forma
# OpenAI con input_audio). DOS proveedores (`prov`), cada uno con SU key en config.json:
#   · openrouter — catálogo multi-vendor. Qwen-Omni NO está ahí (verificado contra
#     /api/v1/models el 2026-07-20): estos son los equivalentes con input de audio.
#     OJO: OpenRouter exige saldo ≥ $0.50 para requests de audio.
#   · dashscope — Alibaba Model Studio (API oficial de Qwen, compatible OpenAI):
#     acá SÍ está la familia Qwen-Omni, incluido el MISMO qwen2.5-omni que corre el
#     carril local. Los omni de DashScope EXIGEN stream=true (documentado) — el
#     transporte lo maneja _clip_online. Precios no publicados de forma estable →
#     usd_*_m=None: la GUI muestra "según catálogo de Alibaba" en vez de inventar.
# `tok_s` = tokens de audio por segundo aprox (Gemini documenta 32/s; OpenAI ~10/s;
# Qwen ~25.6k tokens/hora ≈ 7/s). Los precios OpenRouter son del catálogo 2026-07 y
# sirven para la ESTIMACIÓN de la GUI — lo real lo factura el proveedor.
PROVEEDORES = {
    "openrouter": {
        "nombre": "OpenRouter",
        "url": "https://openrouter.ai/api/v1/chat/completions",
        "key_cfg": "openrouter_api_key", "key_env": "OPENROUTER_API_KEY"},
    "dashscope": {
        "nombre": "Alibaba Model Studio",
        "url": "https://dashscope-intl.aliyuncs.com/compatible-mode/v1/chat/completions",
        "key_cfg": "dashscope_api_key", "key_env": "DASHSCOPE_API_KEY"},
}
MODELS_ONLINE = {
    "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free": {
        "prov": "openrouter", "nota": "GRATIS (rate-limit; razona = lento)", "tok_s": 25,
        "usd_audio_m": 0.0, "usd_in_m": 0.0, "usd_out_m": 0.0},
    "google/gemini-2.5-flash-lite": {
        "prov": "openrouter", "nota": "recomendado — barato y rápido", "tok_s": 32,
        "usd_audio_m": 0.30, "usd_in_m": 0.10, "usd_out_m": 0.40},
    "google/gemini-2.5-flash": {
        "prov": "openrouter", "nota": "más calidad", "tok_s": 32,
        "usd_audio_m": 1.00, "usd_in_m": 0.30, "usd_out_m": 2.50},
    "openai/gpt-audio-mini": {
        "prov": "openrouter", "nota": "alternativa OpenAI", "tok_s": 10,
        "usd_audio_m": 0.60, "usd_in_m": 0.60, "usd_out_m": 2.40},
    # ---- familia Qwen-Omni vía Alibaba (la MISMA familia del carril local) ----
    "qwen2.5-omni-7b": {
        "prov": "dashscope", "nota": "Alibaba — el mismo modelo del carril local (7B)",
        "tok_s": 7, "usd_audio_m": None, "usd_in_m": None, "usd_out_m": None},
    "qwen-omni-turbo": {
        "prov": "dashscope", "nota": "Alibaba — comercial de la familia 2.5 (rápido)",
        "tok_s": 7, "usd_audio_m": None, "usd_in_m": None, "usd_out_m": None},
    "qwen3-omni-flash": {
        "prov": "dashscope", "nota": "Alibaba — Qwen3-Omni (mejor calidad/precio)",
        "tok_s": 7, "usd_audio_m": None, "usd_in_m": None, "usd_out_m": None},
    "qwen3.5-omni-flash": {
        "prov": "dashscope", "nota": "Alibaba — Qwen3.5-Omni flash (nuevo)",
        "tok_s": 7, "usd_audio_m": None, "usd_in_m": None, "usd_out_m": None},
    "qwen3.5-omni-plus": {
        "prov": "dashscope", "nota": "Alibaba — Qwen3.5-Omni plus (tope de línea)",
        "tok_s": 7, "usd_audio_m": None, "usd_in_m": None, "usd_out_m": None},
}
ONLINE_DEFAULT = "google/gemini-2.5-flash-lite"


def proveedor_de(modelo: str | None) -> dict:
    """El proveedor del modelo online (dict de PROVEEDORES). None (→ ONLINE_DEFAULT)
    = openrouter. Un modelo DESCONOCIDO lanza ValueError (review dashscope r1.5: un
    typo mandaba la request al endpoint/key equivocados con error engañoso)."""
    if modelo is None:
        return PROVEEDORES["openrouter"]
    m = MODELS_ONLINE.get(str(modelo).strip())
    if m is None:
        raise ValueError(f"modelo online desconocido: {modelo!r} (ver describir.MODELS_ONLINE)")
    if m.get("prov") not in PROVEEDORES:       # entrada futura mal escrita: error claro
        raise ValueError(f"proveedor desconocido {m.get('prov')!r} para {modelo!r}")
    return PROVEEDORES[m["prov"]]


def key_para(modelo: str | None) -> str | None:
    """La API key correcta para `modelo` según su proveedor: config.json (por máquina)
    o variable de entorno; con strip (una key de solo espacios cae al env). None si no hay."""
    prov = proveedor_de(modelo)
    key = (hardware.load().get(prov["key_cfg"]) or "").strip() \
        or (os.environ.get(prov["key_env"]) or "").strip()
    return key or None


def estimar_online(dur_s: float, modelo: str = ONLINE_DEFAULT) -> tuple[float, str]:
    """(usd, detalle) ESTIMADOS para describir `dur_s` segundos de audio online:
    audio_tokens = dur·tok_s; texto ≈ 700 in + 300 out por chunk de ~30s. Si el
    proveedor no publica precios estables (dashscope), usd=0 y el detalle lo dice."""
    p = MODELS_ONLINE.get(modelo) or MODELS_ONLINE[ONLINE_DEFAULT]
    chunks = max(1, int(dur_s / 30))
    if p.get("usd_audio_m") is None:
        return 0.0, f"~{chunks} chunks · precio según catálogo de Alibaba ({p['nota']})"
    usd = (dur_s * p["tok_s"] * p["usd_audio_m"]
           + chunks * 700 * p["usd_in_m"]
           + chunks * 300 * p["usd_out_m"]) / 1e6
    return usd, f"~{chunks} chunks · ~${usd:.2f} ({p['nota']})"


# --------------------------------------------------------------- localización --
# Preferimos el build VULKAN (corre en la GPU vía el driver, sin CUDA toolkit) que también
# hace CPU; el build CPU-only queda de fallback. Orden de búsqueda (portátil):
#   1. TRANSCRIBER_LLAMA_DIR  (override manual, cualquier SO)
#   2. <proyecto>/llama       (binarios junto a la app → así se distribuye en Windows)
#   3. rutas de desarrollo en esta laptop Linux
_LLAMA_DIRS = [os.environ.get("TRANSCRIBER_LLAMA_DIR"),
               str(app_paths.LLAMA_DIR),              # binarios persistentes de la instalación
               "/data/llama-vulkan/llama-b9986",     # dev Linux: build Vulkan (GPU AMD/NVIDIA)
               "/data/llama.cpp/build/bin"]           # dev Linux: build CPU-only (fallback)
_DEV_LIST_CACHE = None                                 # cache de --list-devices (parseado)


def _bin(name="llama-server") -> str | None:
    """Ubica un binario de llama.cpp: TRANSCRIBER_LLAMA_DIR, <proyecto>/llama, builds de
    dev, o el PATH. En Windows prueba también el nombre con `.exe`."""
    names = [name, name + ".exe"] if os.name == "nt" else [name]
    for base in _LLAMA_DIRS:
        if not base:
            continue
        for nm in names:
            p = Path(base) / nm
            if p.exists():
                return str(p)
    for nm in names:
        w = shutil.which(nm)
        if w:
            return w
    return None


def _env_con_libs(binp) -> dict:
    """Entorno con las libs del build (libggml*/*.dll) en la ruta de búsqueda. En Linux es
    LD_LIBRARY_PATH; en Windows es PATH (las DLLs viven junto al .exe)."""
    env = dict(os.environ)
    d = str(Path(binp).parent)
    var = "PATH" if os.name == "nt" else "LD_LIBRARY_PATH"
    env[var] = d + os.pathsep + env.get(var, "")
    return env


def _wav_bytes(samples, sr) -> bytes:
    """Codifica un slice de audio a WAV en MEMORIA (mismos bytes que escribir el .wav a disco),
    para no escribir/re-leer un archivo temporal por cada chunk."""
    import io
    buf = io.BytesIO()
    sf.write(buf, samples, sr, format="WAV")   # sin subtype = el mismo default que sf.write(path)
    return buf.getvalue()


def _free_port(fallback=8081) -> int:
    """Un puerto libre en localhost. Evita el choque con otro proceso en el 8081 fijo (típico
    en Windows). Hay una carrera mínima entre cerrar el socket y que llama lo tome; aceptable
    para una app de escritorio."""
    import socket
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", 0))
            return int(s.getsockname()[1])
    except OSError:
        return fallback


def list_devices(binp=None, refresh=False) -> list[dict]:
    """Enumera los devices Vulkan que ve llama.cpp (cacheado; `refresh=True` re-consulta —
    importa para `free_mib`, que cambia con lo que ya esté cargado en la GPU). Cada item:
        {"id": "Vulkan0", "name": "AMD Radeon…", "total_mib": 16009, "free_mib": 14440}
    La iGPU y la dedicada aparecen ambas → sirve para que el usuario elija cuál usa el LALM.
    Lista vacía si no hay binario de llama o falla la consulta."""
    global _DEV_LIST_CACHE
    if _DEV_LIST_CACHE is not None and not refresh:
        return _DEV_LIST_CACHE
    binp = binp or _bin("llama-server") or _bin("llama-cli")
    devs: list[dict] = []
    if binp:
        try:
            out = subprocess.run([binp, "--list-devices"], capture_output=True, text=True,
                                 timeout=30, env=_env_con_libs(binp)).stdout
            for line in out.splitlines():
                m = re.search(r"(\w+\d+):\s*(.+?)\s*\((\d+) MiB,\s*(\d+) MiB free\)", line)
                if m:
                    devs.append({"id": m.group(1), "name": m.group(2).strip(),
                                 "total_mib": int(m.group(3)), "free_mib": int(m.group(4))})
        except Exception:
            pass
    _DEV_LIST_CACHE = devs
    return devs


def _pesos_mib(model: Path, mmproj: Path) -> int | None:
    """MiB que pesan los .gguf (LLM + encoder) en disco = el piso duro de memoria de GPU."""
    try:
        return int((model.stat().st_size + mmproj.stat().st_size) / 2**20)
    except OSError:
        return None


def _necesita_mib(model: Path, mmproj: Path) -> int:
    """Estimación CONSERVADORA de la memoria de GPU para cargar el modelo ENTERO: pesos de
    los .gguf + ~10% de overhead de runtime + ~1 GiB para KV-cache (ctx 8192) y buffers de
    cómputo. Si la memoria libre queda ENTRE los pesos y esta estimación, se intenta igual
    (nivel 'justo' de `_gpu_plan`): el colchón sobreestima a propósito, y si de verdad no
    entra, el server muere al cargar y `_Server` degrada solo al plan siguiente."""
    pesos = _pesos_mib(model, mmproj)
    if pesos is None:
        return 1 << 30           # sin tamaños no podemos afirmar que entra → nunca "cabe"
    return int(pesos * 1.10) + 1024


def _gpu_plan(binp, model: Path, mmproj: Path) -> tuple[str | None, bool, str]:
    """Decide `(device Vulkan, full_offload, motivo)` para el modo GPU, respetando la
    selección global del usuario (pestaña Ajustes → 'GPU del modelo de descripción'):
      · 'cpu'      → (None, …): encoder y LLM en CPU.
      · 'VulkanN'  → ese device exacto. TODO el modelo si entra en su memoria libre; si no,
                     solo el encoder (mmproj) ahí y la generación en CPU.
      · 'auto'     → primero una GPU DEDICADA donde el modelo entre ENTERO (generar en la
                     dedicada es lo que de verdad acelera — la iGPU "gana" en memoria libre
                     porque comparte la RAM, pero genera texto LENTO). Si no entra en
                     ninguna dedicada: encoder en la GPU con más memoria libre + LLM en CPU
                     (la config ganadora medida en la laptop de dev, donde nada entra entero).
    Nota: llama.cpp NO separa encoder y LLM en dos GPUs distintas (verificado)."""
    sel = hardware.llama_device()
    if sel == "cpu":
        return None, False, "config: CPU"
    devs = list_devices(binp, refresh=True)     # memoria libre FRESCA, no la del arranque
    if not devs:
        return None, False, "sin devices Vulkan"
    pesos, need = _pesos_mib(model, mmproj), _necesita_mib(model, mmproj)

    def _cabe(d) -> str | None:
        """'sobrado' = entra con colchón; 'justo' = al menos entran los pesos (+256 MiB) —
        se intenta igual porque la estimación sobreestima y, si muere, hay degradación."""
        if d["free_mib"] >= need:
            return "sobrado"
        if pesos is not None and d["free_mib"] >= pesos + 256:
            return "justo"
        return None

    def _det(d, fit) -> str:
        if fit == "sobrado":
            return f"el modelo entero (~{need} MiB estimados) entra en {d['free_mib']} MiB libres"
        return (f"JUSTO: ~{need} MiB estimados vs {d['free_mib']} MiB libres, pero los pesos "
                f"(~{pesos} MiB) entran → pruebo igual (si no entra, degrado solo)")

    if sel != "auto":
        d = next((x for x in devs if x["id"] == sel), None)
        if d:
            fit = _cabe(d)
            if fit:
                return d["id"], True, f"elegida en Ajustes · {_det(d, fit)}"
            return d["id"], False, (f"elegida en Ajustes · ni los pesos (~{pesos} MiB) entran en "
                                    f"{d['free_mib']} MiB libres → solo el encoder")
        # el device guardado ya no existe (cambió driver/hardware) → decidir como 'auto'
    dedicadas = [d for d in devs if hardware._gpu_kind_from_name(d.get("name", "")) == "dedicada"]
    for nivel in ("sobrado", "justo"):          # preferir la que entra con colchón
        cand = [d for d in dedicadas if _cabe(d) == nivel]
        if cand:
            d = max(cand, key=lambda x: x["free_mib"])
            return d["id"], True, f"GPU dedicada · {_det(d, nivel)}"
    d = max(devs, key=lambda x: x["free_mib"])
    return d["id"], False, (f"el modelo (~{need} MiB estimados, pesos ~{pesos} MiB) no entra "
                            f"en ninguna GPU dedicada")


def available() -> bool:
    return _bin("llama-server") is not None or _bin("llama-mtmd-cli") is not None


def model_paths(modelo=DEFAULT_MODEL, *, download=True) -> tuple[Path, Path]:
    """Devuelve (model_gguf, mmproj_gguf) locales; los baja de HF si faltan y download=True."""
    prof = MODELS[modelo]
    mp, pp = MODELS_DIR / prof["model"], MODELS_DIR / prof["mmproj"]
    if download and not (mp.exists() and pp.exists()):
        from huggingface_hub import hf_hub_download
        for f in (prof["model"], prof["mmproj"]):
            hf_hub_download(prof["repo"], f, local_dir=str(MODELS_DIR))
    return mp, pp


# ------------------------------------------------------------------- prompt ----
def _prompt(seg: dict, contexto_previo: str = "") -> str:
    """Instrucción: dar la idea de QUÉ SUCEDE en el audio (eventos sonoros y su dinámica),
    en ORDEN CRONOLÓGICO hacia adelante, SIN interpretar ni clasificar y SIN transcribir el
    habla (de eso se encarga Whisper aparte). Los tags de la Capa 1 se pasan como ayuda de
    precisión, y `contexto_previo` (resumen del chunk anterior) mantiene el hilo en los cortes.
    La interpretación (qué significa, si sirve para un clip) la hace el orquestador."""
    fams = ", ".join(f["familia"] for f in seg.get("familias", [])) or "ninguna"
    ctx = (f"CONTEXTO PREVIO (lo que venía sonando justo antes; SOLO para que entiendas la "
           f"continuidad, NO lo vuelvas a describir): {contexto_previo}\n" if contexto_previo else "")
    return (
        "Sos un DESCRIPTOR de audio. Tu tarea es dar una idea clara de QUÉ SUCEDE en este clip: "
        "los SONIDOS y EVENTOS que ocurren y cómo evolucionan, en ORDEN CRONOLÓGICO HACIA "
        "ADELANTE (lo primero primero, hasta lo último).\n"
        + ctx +
        "Reglas ESTRICTAS:\n"
        "- FIDELIDAD ANTE TODO: describí ÚNICAMENTE lo que estás seguro de oír. NO inventes voces, "
        "personas, música, disparos ni ruido de fondo que no estén claramente presentes. Si solo "
        "hay una voz hablando, decí solo eso. Ante la duda, omití. Una descripción corta y fiel es "
        "MEJOR que una larga con cosas inventadas.\n"
        "- Describí lo que se ESCUCHA como EVENTOS: qué sonido ocurre, su carácter (fuerte/suave, "
        "agudo/grave, seco/sostenido), cómo cambia, y quién o qué lo produce.\n"
        "- NO TRANSCRIBAS lo que se dice. Las palabras exactas ya las captura otro sistema "
        "(Whisper) — repetirlas es inútil. Si hay habla, describí SU CARÁCTER: tono y emoción "
        "(calmo, exaltado, enojado, divertido), cuántas voces hay, si se solapan o se interrumpen, "
        "si la voz sube o se acelera, si hay risa/grito/susurro. NUNCA cites las frases textuales.\n"
        "- Respetá el ORDEN TEMPORAL: poné los eventos en la secuencia exacta en que aparecen. El "
        "ORDEN de la lista YA indica el tiempo → NO agregues marcadores de tiempo, ni números, ni "
        "porcentajes, ni scores, ni '(primero)/(luego)'. Cada entrada es una frase corta y limpia.\n"
        "- Si un sonido se sostiene o se repite, decilo UNA sola vez aclarando que continúa; nunca "
        "repitas la misma frase. Sé CONCISO y FIEL: incluí solo las entradas que de verdad "
        "correspondan (pueden ser pocas), NO rellenes para alargar.\n"
        "- PROHIBIDO interpretar, clasificar o concluir: no digas qué tipo de escena es ni qué pasa "
        "en el juego. Solo describí los sonidos y su evolución.\n"
        f"- Referencia interna (un detector marcó estos sonidos): {fams}. Es SOLO para vos; NO copies "
        "esos nombres ni ningún número/score en la salida.\n"
        "Respondé SOLO un JSON válido con este esquema:\n"
        '{"secuencia": [<cada entrada es UNA FRASE corta de texto plano (sin números, sin tiempos, '
        'sin scores), un evento sonoro, EN ORDEN temporal — describí QUÉ pasa y el carácter del '
        'sonido, NO cites palabras>], '
        '"resumen": "<2-3 frases: la idea general de qué sucede en el audio, de principio a fin>"}'
    )


def _limpiar(x) -> str:
    """Aplana (une listas anidadas) y limpia la entrada: quita el <> del placeholder, los
    corchetes, los prefijos de orden '(primero)/(luego)…' y CUALQUIER número/score entre
    paréntesis (el modelo a veces copia el tag 'habla(0.67)') → frase corta y limpia."""
    if isinstance(x, list):
        x = " ".join(_limpiar(e) for e in x)
    s = str(x).strip().strip("<>").strip()
    s = re.sub(r"\((?:primero|luego|despu[eé]s|al final|al inicio|inicio|fin)\)\s*", "", s, flags=re.I)
    s = re.sub(r"\(\s*[\d.,%]+\s*\)", "", s)        # (0.67), (12%)…
    s = re.sub(r"[\[\]]", "", s)                     # corchetes
    s = re.sub(r"\(\s*\)", "", s)                    # paréntesis vacíos que queden
    return re.sub(r"\s{2,}", " ", s).strip(" ,;·")


def _normalizar(r: dict) -> dict:
    """Deja `secuencia` como lista de strings planas y `resumen` como un string."""
    sec = r.get("secuencia")
    if isinstance(sec, list):
        r["secuencia"] = [_limpiar(e) for e in sec]
    if "resumen" in r:
        r["resumen"] = _limpiar(r["resumen"])
    return r


# --------------------------------------------------- servidor llama.cpp (batch) --
class _Server:
    """Levanta un `llama-server` con el modelo elegido y le manda cada clip por HTTP
    (OpenAI /chat/completions con input_audio). Cargar el modelo UNA vez y reusar es
    clave para batch (cientos de escenas). Context manager: cierra el proceso al salir."""

    def __init__(self, modelo=DEFAULT_MODEL, port=None, n_gpu_layers=None, cancel=None, log_cb=None):
        self.prof = MODELS[modelo]
        self.port, self.log_cb = port, log_cb      # port=None → se elige un puerto libre al arrancar
        self.n_gpu_layers = n_gpu_layers          # override; None = usa el default del perfil
        self.cancel = cancel                       # threading.Event para abortar la carga
        self.proc = None
        self._errlog = None                        # log temporal del stderr del server
        self._ultimo_error = ""                    # tail del stderr del último intento fallido

    def _log(self, m):
        if self.log_cb:
            self.log_cb(m)

    def _stderr_tail(self, n=1500) -> str:
        """Últimos bytes del stderr del server, para diagnosticar por qué murió (Vulkan
        ausente, DLL faltante, modelo corrupto, puerto ocupado, etc.)."""
        try:
            if self._errlog is not None:
                self._errlog.flush()
                with open(self._errlog.name, "r", errors="replace") as f:
                    return f.read()[-n:].strip() or "(sin salida de error)"
        except Exception:
            pass
        return "(no se pudo leer el log del servidor)"

    def __enter__(self):
        binp = _bin("llama-server")
        if not binp:
            raise RuntimeError("No encuentro llama-server (instalá/compilá llama.cpp con audio).")
        model, mmproj = model_paths(next(k for k, v in MODELS.items() if v is self.prof))
        ngl = self.n_gpu_layers if self.n_gpu_layers is not None else self.prof["n_gpu_layers"]
        nthreads = hardware.cpu_threads()          # respeta la config global de hilos
        if self.port is None:
            self.port = _free_port()               # puerto libre (evita choque en Windows)
        base = [binp, "-m", str(model), "--mmproj", str(mmproj),
                "-c", str(self.prof["ctx"]),
                "-t", str(nthreads), "--threads-batch", str(nthreads),
                "--port", str(self.port), "--host", "127.0.0.1"]
        # PLANES en orden de preferencia. Si el server MUERE al cargar con un plan (p.ej. un
        # OOM real aunque la estimación decía que entraba), se degrada al siguiente en vez de
        # tumbar el pipeline: todo-en-GPU → encoder-en-GPU+LLM-CPU → todo-CPU.
        planes: list[tuple[list[str], str]] = []
        if ngl and ngl != 0:
            dev, full, motivo = _gpu_plan(binp, model, mmproj)
            if dev and not full:
                # Antes de renunciar al full offload: liberar NUESTROS modelos cacheados (otra
                # capa pudo dejar VRAM tomada) y re-planear con la memoria libre fresca. La VRAM
                # de OTRAS apps no se puede liberar desde acá (no hay API y matar procesos ajenos
                # no es opción) → abajo solo se informa quién la ocupa para que cierre el usuario.
                try:
                    import models
                    models.unload_all()
                    self._log("🧹 Descargué los modelos propios cacheados para hacer lugar en la GPU…")
                    dev, full, motivo = _gpu_plan(binp, model, mmproj)
                except Exception:
                    pass
            if dev and not full:
                procs = hardware.gpu_processes()
                if procs:
                    top = ", ".join((f"{p['name']} ({p['mib']} MiB)" if p["mib"] else p["name"])
                                    for p in procs[:4])
                    self._log(f"Ocupando la GPU ahora: {top} — cerrá lo que no uses y re-corré "
                              "si querés que el modelo entre entero.")
            if dev and full:
                planes.append((base + ["-ngl", str(ngl if ngl > 0 else 99),
                                       "--device", dev, "--mmproj-offload"],
                               f"TODO en GPU {dev} · {motivo}"))
            if dev:
                planes.append((base + ["-ngl", "0", "--device", dev, "--mmproj-offload"],
                               f"encoder en GPU {dev} + LLM en CPU ({nthreads} hilos)"
                               + ("" if full else f" · {motivo}")))
            else:
                self._log(f"⚠ GPU pedida pero no usable ({motivo}) → CPU.")
        planes.append((base + ["-ngl", "0"], f"{nthreads} hilos CPU"))
        for i, (cmd, modo) in enumerate(planes):
            self._log(f"Levantando llama-server: {self.prof['model']} · {modo} · puerto {self.port}…")
            if self._lanzar_y_esperar(cmd, binp):
                self._log("Servidor listo.")
                return self
            if i + 1 < len(planes):
                # la línea más informativa del stderr: la última que huela a error, o la última
                lines = [ln.strip() for ln in self._ultimo_error.splitlines() if ln.strip()]
                err = next((ln for ln in reversed(lines) if re.search(
                    r"error|failed|out of memory|alloc|abort", ln, re.I)), lines[-1] if lines else "")
                self._log("⚠ El servidor murió al cargar con ese plan"
                          + (f" ({err})" if err else "") + " — pruebo el siguiente.")
        raise RuntimeError("llama-server murió al arrancar:\n"
                           + (self._ultimo_error or "(sin salida de error)"))

    def _lanzar_y_esperar(self, cmd, binp) -> bool:
        """Lanza el server y espera a /health == 200 (el puerto abre ANTES de cargar el modelo
        → da 503 mientras carga, sondear el puerto no alcanza; modelos grandes en CPU tardan →
        margen generoso). True = listo. False = el proceso MURIÓ cargando (el caller puede
        probar otro plan; el motivo queda en `self._ultimo_error`). Cancelación/timeout → raise."""
        import tempfile
        import urllib.request
        # stderr a un archivo temporal: si el server muere, el tail explica por qué (en vez de DEVNULL)
        self._errlog = tempfile.NamedTemporaryFile(mode="w+", suffix="_llama.log", delete=False)
        self.proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=self._errlog,
                                     env=_env_con_libs(binp))
        url = f"http://127.0.0.1:{self.port}/health"
        for _ in range(900):
            if self.proc.poll() is not None:
                self._ultimo_error = self._stderr_tail(); self.__exit__()  # limpiar antes de decidir
                return False
            if self.cancel is not None and self.cancel.is_set():   # cancelado durante la carga
                self.__exit__(); raise RuntimeError("cancelado durante la carga del modelo")
            try:
                with urllib.request.urlopen(url, timeout=2) as r:
                    if r.status == 200:
                        return True
            except Exception:
                pass
            time.sleep(1)
        self._ultimo_error = self._stderr_tail(); self.__exit__()  # timeout: matar proceso + limpiar
        raise RuntimeError("llama-server no cargó el modelo a tiempo.\n" + self._ultimo_error)

    def __exit__(self, *a):
        if self.proc:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except Exception:
                self.proc.kill()
                try:                               # esperar al kill: que el puerto quede libre
                    self.proc.wait(timeout=5)      # antes del retry y sin dejar zombies
                except Exception:
                    pass
        if self._errlog is not None:               # limpiar el log temporal del stderr
            try:
                self._errlog.close()
                os.unlink(self._errlog.name)
            except Exception:
                pass
            self._errlog = None

    def describir_clip(self, wav) -> dict:
        """Manda un clip WAV (bytes en memoria O una ruta a archivo) y devuelve el JSON parseado
        (o {'_raw':...} si no parsea)."""
        import urllib.request
        data = bytes(wav) if isinstance(wav, (bytes, bytearray)) else Path(wav).read_bytes()
        b64 = base64.b64encode(data).decode()
        body = {
            "messages": [{"role": "user", "content": [
                {"type": "input_audio", "input_audio": {"data": b64, "format": "wav"}},
                {"type": "text", "text": self._prompt},
            ]}],
            "temperature": 0.2, "max_tokens": 600,         # baja temp = menos invención
            "repeat_penalty": 1.3,                          # corta loops del modelo chico
            "response_format": {"type": "json_object"},   # fuerza JSON válido
        }
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/v1/chat/completions",
            data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=300) as r:
            out = json.loads(r.read())
        txt = out["choices"][0]["message"]["content"]
        return _parse_lalm(txt)

    _prompt = ""   # se setea por segmento antes de cada llamada


def _parse_lalm(txt: str) -> dict:
    """Parseo tolerante de la respuesta del LALM (local u online): quita fences,
    encuentra el objeto, normaliza; {'_raw':...} si no parsea."""
    t = (txt or "").strip().strip("`").strip()        # quitar fences ```json ... ```
    if t[:4].lower() == "json":
        t = t[4:].strip()
    for cand in (t, t[t.find("{"):t.rfind("}") + 1]):
        try:
            return _normalizar(json.loads(cand))
        except Exception:
            continue
    return {"_raw": txt}


# ------------------------------------------------------- backend ONLINE --------
def _abortar_stream(resp):
    """Aborta un stream HTTP DESDE OTRO HILO: shutdown() del socket subyacente —
    `Response.close()` solo no despierta un recv bloqueado (verificado en el test
    del vigilante). Best-effort sobre los internals de urllib3 v1/v2, con close()
    de respaldo (que al menos evita fugas de conexión)."""
    import socket as _s
    try:
        raw = resp.raw
        conn = getattr(raw, "_connection", None) or getattr(raw, "connection", None)
        sock = getattr(conn, "sock", None)
        if sock is None:
            # urllib3 v2 desengancha conn.sock: el socket real cuelga de
            # raw._fp (http.client.HTTPResponse) → .fp (BufferedReader) → .raw._sock
            fp = getattr(getattr(raw, "_fp", None), "fp", None)
            sock = getattr(getattr(fp, "raw", None), "_sock", None)
        if sock is not None:
            try:
                sock.shutdown(_s.SHUT_RDWR)
            except Exception:
                pass
            try:
                sock.close()
            except Exception:
                pass
    except Exception:
        pass
    try:
        resp.close()
    except Exception:
        pass


class _StreamIncompleto(RuntimeError):
    """Stream SSE cortado/corrupto ANTES de una terminación válida — reintentable
    (el texto acumulado se descarta; aceptarlo consolidaba respuestas truncadas
    como exitosas — review dashscope r1.1)."""


def _leer_sse(r, cancel=None) -> str:
    """Acumula el content de un stream SSE estilo OpenAI. EXIGE terminación válida
    ([DONE] o finish_reason): EOF antes de eso → _StreamIncompleto. Un evento
    {"error":...} mid-stream lanza RuntimeError con código+mensaje (sin contenido ni
    key). Las líneas pueden llegar como bytes (server sin charset): se decodifican
    UTF-8 explícito (r1.2). `cancel` se revisa entre eventos (r1.3)."""
    partes, completo = [], False
    for linea in r.iter_lines():
        if cancel is not None and cancel.is_set():
            raise InterruptedError("descripción cancelada")
        if isinstance(linea, bytes):
            linea = linea.decode("utf-8", "replace")
        linea = linea.lstrip("\ufeff")
        if not linea or not linea.startswith("data:"):
            continue
        data = linea[5:].strip()
        if data == "[DONE]":
            completo = True
            break
        try:
            ev = json.loads(data)
        except Exception:
            raise _StreamIncompleto("evento SSE ilegible (stream corrupto)")
        if isinstance(ev, dict) and ev.get("error"):
            e = ev["error"] if isinstance(ev["error"], dict) else {"message": str(ev["error"])}
            raise RuntimeError("error del proveedor en el stream: "
                               f"{e.get('code') or e.get('type') or '?'} "
                               f"{str(e.get('message', ''))[:120]}")
        ch = ((ev.get("choices") or [{}])[0]) if isinstance(ev, dict) else {}
        delta = ch.get("delta") or {}
        if delta.get("content"):
            partes.append(delta["content"])
        fr = ch.get("finish_reason")
        if fr:
            if fr != "stop":                   # length/content_filter = NO exitoso (r2.1)
                raise RuntimeError(f"el stream terminó por {fr!r} "
                                   "(respuesta truncada/filtrada — no se acepta)")
            completo = True                    # 'stop' válido: algunos cierran sin [DONE]
    if not completo:
        raise _StreamIncompleto("stream SSE incompleto (EOF sin terminación)")
    return "".join(partes)


def _clip_online(clip: bytes, prompt: str, modelo: str, key: str, cancel=None) -> dict:
    """Un chunk contra el proveedor del modelo (misma forma OpenAI input_audio que
    llama-server). openrouter: request normal con response_format json. dashscope
    (Alibaba, familia Qwen-Omni): SIN response_format (no lo soporta el omni), audio
    como data-URI y **stream=true OBLIGATORIO** (documentado) — se acumula el SSE.
    Reintenta 2 veces transporte/429/5xx (Retry-After respetado); 401/402 lanza
    RuntimeError con mensaje claro (la capa entera falla con motivo — sin key o sin
    saldo no tiene sentido seguir chunk a chunk). La key JAMÁS se loguea."""
    import requests
    modelo = str(modelo).strip()               # el ID que VIAJA es el normalizado (r2)
    prov = proveedor_de(modelo)
    ds = prov is PROVEEDORES["dashscope"]
    nombre = prov["nombre"]
    b64 = base64.b64encode(clip).decode()
    audio = {"data": (f"data:;base64,{b64}" if ds else b64), "format": "wav"}
    body = {"model": modelo, "temperature": 0, "top_p": 1, "max_tokens": 600,
            "messages": [{"role": "user", "content": [
                {"type": "input_audio", "input_audio": audio},
                {"type": "text", "text": prompt}]}]}
    if ds:
        body["stream"] = True
        body["modalities"] = ["text"]          # solo texto de vuelta (sin TTS)
    else:
        body["response_format"] = {"type": "json_object"}
    intentos = 0
    while True:
        if cancel is not None and cancel.is_set():
            raise InterruptedError("descripción cancelada")
        try:
            r = requests.post(prov["url"], headers={"Authorization": f"Bearer {key}"},
                              json=body, timeout=(10, 180), stream=ds)
        except requests.RequestException as e:
            intentos += 1
            if intentos > 2:
                raise RuntimeError(f"{nombre} inaccesible ({type(e).__name__})")
            time.sleep(1.5 * intentos)
            continue
        # TODO el ciclo de vida de la Response bajo un finally (review r2: los caminos
        # 401/429/!=200 dejaban la conexión abierta). El VIGILANTE cierra el socket si
        # cancelan mientras iter_lines está bloqueado sin eventos (r2/r1.3).
        done = threading.Event()
        if ds and cancel is not None:
            def _vigilar(resp=r, d=done):
                while not d.is_set():
                    if cancel.wait(0.2):
                        _abortar_stream(resp)  # shutdown del socket: despierta iter_lines
                        return
            threading.Thread(target=_vigilar, daemon=True).start()
        try:
            if r.status_code in (401, 402):
                try:
                    msg = (r.json().get("error") or {}).get("message", "")[:120]
                except Exception:
                    msg = ""
                raise RuntimeError(f"{nombre} HTTP {r.status_code}: "
                                   + (msg or ("API key inválida" if r.status_code == 401
                                              else ("sin saldo" if ds else
                                                    "sin saldo (audio exige ≥ $0.50)"))))
            if r.status_code in (429, 500, 502, 503):
                intentos += 1
                if intentos > 2:
                    raise RuntimeError(f"{nombre} HTTP {r.status_code} tras reintentos")
                try:
                    espera = float(r.headers.get("Retry-After") or 0)
                except ValueError:
                    espera = 0
                time.sleep(max(espera, 1.5 * intentos))
                continue
            if r.status_code != 200:
                try:
                    msg = (r.json().get("error") or {}).get("message", "")[:160]
                except Exception:
                    msg = ""
                raise RuntimeError(f"{nombre} HTTP {r.status_code}"
                                   + (f": {msg}" if msg else "")
                                   + (" — ¿el modelo existe en tu región/cuenta?"
                                      if ds and r.status_code in (400, 404) else ""))
            # consumo DENTRO del loop reintentable (r1.3): un corte mid-stream se
            # reintenta descartando lo acumulado
            try:
                if ds:
                    txt = _leer_sse(r, cancel)
                else:
                    try:
                        txt = r.json()["choices"][0]["message"]["content"]
                    except Exception:
                        raise RuntimeError(f"respuesta de {nombre} sin choices")
            except InterruptedError:
                raise
            except Exception as e:
                if cancel is not None and cancel.is_set():
                    raise InterruptedError("descripción cancelada")
                if isinstance(e, (_StreamIncompleto, requests.RequestException)):
                    intentos += 1
                    if intentos > 2:
                        raise RuntimeError(f"{nombre}: {e}")
                    time.sleep(1.5 * intentos)
                    continue
                raise
        finally:
            done.set()                         # apaga el vigilante
            try:
                r.close()
            except Exception:
                pass
        if not txt:
            raise RuntimeError(f"respuesta de {nombre} vacía")
        return _parse_lalm(txt)


# --------------------------------------------------------------------- público --
def _chunks(segments, max_len, overlap):
    """Parte las escenas de la Capa 2 en chunks para el LALM. Escena corta (<= max_len) = 1
    chunk. Escena larga se sub-divide en ventanas de <= max_len con `overlap` s de SOLAPE (el
    arranque de cada sub-ventana repite los últimos `overlap` s de la anterior → un evento en
    el borde no se pierde). NO se solapa entre escenas distintas: esos cortes caen en bordes de
    escena (donde el sonido ya cambió), y la continuidad narrativa la da el contexto de texto.
    `solape_prev` = segundos compartidos con el chunk anterior (contexto, no núcleo)."""
    chunks = []
    for si, seg in enumerate(segments):
        s0, s1 = float(seg["t_ini"]), float(seg["t_fin"])
        fam = seg.get("familias", [])
        if s1 - s0 <= max_len:
            chunks.append({"t_ini": s0, "t_fin": s1, "solape_prev": 0.0, "escena": si, "familias": fam})
            continue
        w, first = s0, True
        while w < s1 - 0.1:
            tf = min(w + max_len, s1)
            chunks.append({"t_ini": w, "t_fin": tf, "solape_prev": 0.0 if first else float(overlap),
                           "escena": si, "familias": fam})
            if tf >= s1:
                break
            w, first = tf - overlap, False
    return chunks


def describir(audio, segments, *, modelo=DEFAULT_MODEL, max_len=30.0, overlap=4.0,
              n_gpu_layers=None, backend="local", modelo_online=None, api_key=None,
              cancel=None, log_cb=None, progress_cb=None) -> list[dict]:
    """Describe el audio por CHUNKS derivados de las escenas (ver `_chunks`). Cada resultado se
    estampa con la ventana [t_ini,t_fin] que EFECTIVAMENTE se le mandó al modelo — el timestamp
    lo ponemos NOSOTROS, nunca el modelo (sus 'primero/luego' son ordinales dentro de la ventana).
    `solape_prev` marca el arranque compartido con el chunk previo. Un `contexto` rodante (el
    resumen del chunk anterior) viaja como texto para no perder el hilo en los cortes.
    `cancel` (threading.Event): si se setea, corta entre chunks y MATA el server para abortar la
    request en curso al instante (no espera a que termine el chunk lento)."""
    def log(m):
        if log_cb:
            log_cb(m)

    def cancelado():
        return cancel is not None and cancel.is_set()

    if backend == "online" and not api_key:
        raise RuntimeError("descripción online sin API key de "
                           f"{proveedor_de(modelo_online)['nombre']} "
                           "(configurala en el paso 2)")
    wav, sr = sf.read(str(audio), always_2d=True)
    chunks = _chunks(segments, max_len, overlap)
    if not chunks:
        return []
    out, contexto = [], ""

    # ---- backend ONLINE (OpenRouter): mismo loop/contexto/salida, sin _Server ----
    if backend == "online":
        mod = modelo_online or ONLINE_DEFAULT
        log(f"Capa 3 ONLINE: {len(chunks)} chunk(s) → {mod}")
        for i, ch in enumerate(chunks):
            if cancelado():
                log("⏹ Descripción cancelada."); break
            i0, i1 = int(ch["t_ini"] * sr), min(int(ch["t_fin"] * sr), len(wav))
            clip = _wav_bytes(wav[i0:i1], sr)
            prompt = _prompt({"familias": ch["familias"]}, contexto_previo=contexto)
            desc = _clip_online(clip, prompt, mod, api_key, cancel=cancel)
            out.append({"t_ini": round(ch["t_ini"], 2), "t_fin": round(ch["t_fin"], 2),
                        "solape_prev": round(ch["solape_prev"], 2), "escena": ch["escena"],
                        "tipo": "descripcion", "modelo": f"online:{mod}", **desc})
            r = desc.get("resumen") if isinstance(desc, dict) else None
            if r:
                contexto = r
            if progress_cb:
                progress_cb((i + 1) / len(chunks), None)
            if log_cb and (i % 3 == 0 or i == len(chunks) - 1):
                log(f"Describiendo chunks (online)… {i + 1}/{len(chunks)}")
        log(f"Capa 3 lista: {len(out)} chunk(s) descripto(s) con online:{mod}.")
        return out

    with _Server(modelo, n_gpu_layers=n_gpu_layers, cancel=cancel, log_cb=log) as srv:
        # watcher: si cancelan mientras una request está bloqueada, mata el server → la request
        # falla al toque y salimos, sin esperar a que termine el chunk lento.
        stop_watch = threading.Event()
        if cancel is not None:
            def _watch():
                while not stop_watch.wait(0.3):
                    if cancel.is_set():
                        try:
                            if srv.proc:
                                srv.proc.kill()          # SIGKILL: el server ignora SIGTERM mientras infiere
                        except Exception:
                            pass
                        return
            threading.Thread(target=_watch, daemon=True).start()

        for i, ch in enumerate(chunks):
            if cancelado():
                log("⏹ Descripción cancelada."); break
            i0, i1 = int(ch["t_ini"] * sr), min(int(ch["t_fin"] * sr), len(wav))
            clip = _wav_bytes(wav[i0:i1], sr)          # WAV en memoria (sin temp en disco)
            srv._prompt = _prompt({"familias": ch["familias"]}, contexto_previo=contexto)
            try:
                desc = srv.describir_clip(clip)
            except Exception:
                if cancelado():
                    log("⏹ Descripción cancelada."); break
                raise
            out.append({"t_ini": round(ch["t_ini"], 2), "t_fin": round(ch["t_fin"], 2),
                        "solape_prev": round(ch["solape_prev"], 2), "escena": ch["escena"],
                        "tipo": "descripcion", "modelo": modelo, **desc})
            r = desc.get("resumen") if isinstance(desc, dict) else None
            if r:                       # rolling context = resumen del último chunk logrado
                contexto = r
            if progress_cb:
                progress_cb((i + 1) / len(chunks), None)
            if log_cb and (i % 3 == 0 or i == len(chunks) - 1):
                log(f"Describiendo chunks… {i + 1}/{len(chunks)}")
        stop_watch.set()
    log(f"Capa 3 lista: {len(out)} chunk(s) descripto(s) sobre {len(segments)} escena(s) con {modelo}.")
    return out


def smoke_test(audio, *, modelo=DEFAULT_MODEL, log_cb=print) -> dict:
    """Prueba mínima: describe UN clip para confirmar que la cadena de audio funciona."""
    seg = {"t_ini": 0.0, "t_fin": 0.0, "familias": [], "energia_z": 0, "densidad_transitorios": 0}
    with _Server(modelo, log_cb=log_cb) as srv:
        srv._prompt = _prompt(seg)
        return srv.describir_clip(audio)


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="Capa 3: describir audio con un LALM vía llama.cpp.")
    ap.add_argument("audio")
    ap.add_argument("--modelo", default=DEFAULT_MODEL, choices=list(MODELS))
    ap.add_argument("--online", nargs="?", const=ONLINE_DEFAULT, default=None,
                    choices=list(MODELS_ONLINE), metavar="MODELO",
                    help="usar la API online (OpenRouter o Alibaba según el modelo; "
                    "key de config.json). Opciones: " + ", ".join(MODELS_ONLINE)
                    + ". Sin valor = " + ONLINE_DEFAULT)
    ap.add_argument("--smoke", action="store_true", help="solo probar un clip")
    a = ap.parse_args()
    _key = key_para(a.online) or ""
    if a.smoke and a.online:
        # smoke ONLINE: un clip de 8s directo contra la API (valida key/saldo/modelo)
        wav_, sr_ = sf.read(a.audio, always_2d=True)
        clip = _wav_bytes(wav_[: int(8 * sr_)], sr_)
        print(json.dumps(_clip_online(clip, _prompt({"familias": []}), a.online, _key),
                         ensure_ascii=False, indent=1))
    elif a.smoke:
        print(json.dumps(smoke_test(a.audio, modelo=a.modelo), ensure_ascii=False, indent=1))
    else:
        import escena_audio as E
        r = E.analizar(a.audio, log_cb=print)
        d = describir(a.audio, r["segments"], modelo=a.modelo,
                      backend="online" if a.online else "local",
                      modelo_online=a.online, api_key=_key or None, log_cb=print)
        print(json.dumps(d, ensure_ascii=False, indent=1))
