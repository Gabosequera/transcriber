#!/usr/bin/env python3
"""
vistas.py — genera las SUPERFICIES DE LECTURA para la AI orquestadora (Ava) desde el
master.json. Diseño consensuado en 2 rondas con Codex gpt-5.6-sol (2026-07-16, ver
three-brain-out/2026-07-16-orquestador-metadata/DISENO-final.md).

Principio RAW vs VISTA: el master.json es la fuente de verdad (hechos); estas vistas son
DERIVADAS, deterministas (script, no LLM) y desechables — se regeneran, nunca se editan.
Cada vista declara el sha256 del master del que salió: si no coincide, REGENERAR.

Superficies (bundle atómico en <master>/../vistas/):
  · inventario.json      — diagnóstico: duración, streams, densidades, percentiles,
                           baselines, procedencia. NO es una vista editorial.
  · transcript.md        — 1ª LECTURA (limpia): IDs estables + timestamps + texto. Sin
                           señales (no contaminar la lectura semántica). Única marca
                           inline: las INSTRUCCIONES habladas a Ava (son órdenes, no
                           señales).
  · transcript_anotado.md— 2ª LECTURA: mismo texto e IDs + carril lateral por modalidad
                           con TIERS (·/1/2/3) — dónde hubo actividad y cuánta, SIN
                           z-scores ni etiquetas de emoción. Huecos sin voz con actividad
                           = línea propia ⟨sin voz⟩ (la sonrisa muda no se pierde).
  · indice_senales.md    — semillas compactas por CARRIL con cuotas (no top-K global) +
                           frontera de descartes. La AI ya generó sus candidatos
                           semánticos ANTES de abrir esto (protocolo de la skill).
Consulta por demanda (no archivo):
  · dossier(t0,t1|seed)  — el detalle completo de UN momento: transcript local, todas
                           las señales cruzadas, bordes candidatos, procedencia.

TIERS por modalidad (deterministas, relativos al PROPIO video): la "fuerza" de cada
evento (métrica por stream, abajo) se rankea contra los eventos del mismo stream:
  tier 1 = hay actividad (≥ p50) · tier 2 = notable (≥ p80) · tier 3 = ultra (≥ p95).
Métrica de fuerza por stream: voz.emocion=arousal_z · voz.risa=conf · voz.pausas=dur_z ·
cara.reaccion=z_max · cara.emocion=z_max · fondo.transitorios=fuerza_z ·
fondo.escenas=energia_z · mirada(camara)=dur.

Uso CLI:
  python vistas.py generar <master.json> [--outdir DIR]
  python vistas.py dossier <master.json> --rango T0 T1
  python vistas.py dossier <master.json> --seed sig-012
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path

VERSION = "vistas-1.2"      # 1.2 (2026-07-17): contexto visual VLM en el dossier
                            # 1.1 (2026-07-16): directivas.md + marcas del autor

# Carriles del transcript anotado y qué streams alimentan cada uno.
CARRILES = ["voz", "risa", "pausa", "cara", "camara", "juego"]

# Cuotas del índice de señales por 25 min de video (se escalan por duración).
CUOTAS_25MIN = {"multimodal": 10, "risa": 6, "voz": 5, "cara_muda": 5,
                "camara": 4, "valencia": 4, "pausa_reaccion": 4}
COOCURRENCIA_S = 3.0        # eventos a ≤ esto = mismo núcleo multimodal
BUILD_UP_S = 12.0           # pausa/tensión a ≤ esto ANTES de un pico = build_up (enlace)
DESCARTES_N = 8             # frontera de descartes visible


# ------------------------------------------------------------------ utilidades --
def _sha256(p) -> str:
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


def _fmt_t(s: float) -> str:
    m, seg = divmod(float(s), 60.0)
    return f"{int(m):02d}:{seg:06.3f}"


def _ms(s: float) -> int:
    return int(round(float(s) * 1000))


def cargar(master_path) -> tuple[dict, str]:
    p = Path(master_path)
    return json.loads(p.read_text(encoding="utf-8")), _sha256(p)


def _pctl(valores: list[float]) -> dict:
    """Umbrales p50/p80/p95 de una lista (deterministas; listas cortas incluidas).
    Incluye n: con muestra chica (<5) los percentiles no calibran → tier se degrada a 1."""
    import numpy as np
    if not valores:
        return {"p50": 0.0, "p80": 0.0, "p95": 0.0, "n": 0}
    v = np.array(valores, dtype=float)
    d = {k: float(np.percentile(v, q)) for k, q in (("p50", 50), ("p80", 80), ("p95", 95))}
    d["n"] = len(valores)
    return d


def _fuerza(stream: str, e: dict) -> float | None:
    """Métrica de fuerza de un evento según su stream (ver docstring)."""
    if stream == "voz.emocion":
        return e.get("arousal_z")
    if stream == "voz.risa":
        return e.get("conf")
    if stream == "voz.pausas":
        return e.get("dur_z")
    if stream == "video.cara.reaccion":
        return e.get("z_max")
    if stream == "video.cara.emocion":
        return e.get("z_max")
    if stream == "fondo.transitorios":
        return e.get("fuerza_z")
    if stream == "fondo.escenas":
        return e.get("energia_z")
    if stream == "video.cara.mirada":
        return (e["t_fin"] - e["t_ini"]) if e.get("direccion") == "camara" else None
    if stream == "fondo.sonidos":
        return e.get("conf")
    return None


def _tier(v: float | None, umbrales: dict) -> int:
    if v is None:
        return 0
    if umbrales.get("n", 0) < 5:   # muestra chica: un evento único NO es "ultra" — con
        return 1                   # n<5 los percentiles no significan nada (fix r3)
    if v >= umbrales["p95"]:
        return 3
    if v >= umbrales["p80"]:
        return 2
    return 1                       # hay evento → hay actividad (piso tier 1)


def _t_pico(e: dict) -> float:
    return float(e.get("t_pico", (float(e["t_ini"]) + float(e.get("t_fin", e["t_ini"]))) / 2))


def _solapa(e: dict, t0: float, t1: float) -> bool:
    return float(e.get("t_ini", 1e18)) < t1 and float(e.get("t_fin", e.get("t_ini", -1e18))) > t0


class _Ctx:
    """Master cargado + umbrales por stream + índice del transcript."""

    def __init__(self, master: dict, sha: str):
        self.m, self.sha = master, sha
        self.streams = master["streams"]
        self.dur = float(master["duracion"])
        self.umbrales = {k: _pctl([f for e in evs if (f := _fuerza(k, e)) is not None])
                         for k, evs in self.streams.items()}
        self.transcript = sorted(self.streams.get("voz.transcript", []),
                                 key=lambda e: e["t_ini"])

    def eventos(self, stream: str, t0: float, t1: float) -> list[dict]:
        return [e for e in self.streams.get(stream, []) if _solapa(e, t0, t1)]

    def transcript_ids(self, t0: float, t1: float) -> list[str]:
        return [s["id"] for s in self.transcript if _solapa(s, t0, t1)]

    def linea(self, t0: float, t1: float, max_len: int = 90) -> str:
        txt = " ".join(s["texto"] for s in self.transcript if _solapa(s, t0, t1))
        return (txt[:max_len] + "…") if len(txt) > max_len else txt


def _provenance(ctx: _Ctx, extra: str = "") -> str:
    return (f"<!-- {VERSION} · master sha256={ctx.sha[:16]}… · "
            f"generado {datetime.now(timezone.utc).isoformat(timespec='seconds')}{extra} -->\n"
            "<!-- VISTA DERIVADA: se regenera desde el master; NO editar a mano. Si el "
            "sha256 no coincide con el master actual, esta vista está STALE: regenerar "
            "con `vistas.py generar` antes de usarla. -->\n")


# ------------------------------------------------------------- transcript (1ª) --
def _linea_marca(mk: dict) -> str:
    """Línea inline de una marca del autor. OJO semántica (consenso r1 h.6): a
    diferencia del tramo hablado a Ava (que SE EXCLUYE porque contiene la orden), la
    región marcada por UI es el OBJETO de la orden."""
    dec = (mk.get("decision") or "nota").upper()
    rango = (f"[{_fmt_t(mk['t_ini'])}–{_fmt_t(mk['t_fin'])}]"
             if mk.get("t_fin") != mk.get("t_ini") else f"[{_fmt_t(mk['t_ini'])}]")
    txt = f" — «{mk['prompt']}»" if mk.get("prompt") else ""
    return f"»» MARCA DEL AUTOR ({mk['id']}) {rango} {dec}{txt} (ver directivas.md)"


def gen_transcript(ctx: _Ctx) -> str:
    out = [_provenance(ctx),
           "# TRANSCRIPT — 1ª lectura (limpia)\n",
           "Leélo COMPLETO y formate el cuadro general ANTES de mirar cualquier señal.\n"
           "Los IDs (S####) son estables: usalos para candidatos, dossiers y la EDL.\n"]
    instrucciones = ctx.streams.get("voz.instrucciones", [])
    marcas = ctx.streams.get("autor.marcas", [])
    vistos = set()
    for s in ctx.transcript:
        for ins in instrucciones:      # las órdenes del autor SÍ van inline (no son señales)
            if abs(float(ins["t_ini"]) - float(s["t_ini"])) < 0.5 or \
               (float(s["t_ini"]) <= float(ins["t_ini"]) < float(s["t_fin"])):
                out.append(f"»» INSTRUCCIÓN A AVA ({ins['id']}) — el tramo "
                           f"[{_fmt_t(ins['t_ini'])}–{_fmt_t(ins['t_fin'])}] es una orden "
                           f"del autor (ver ledger) y SE EXCLUYE de los clips.")
                break
        for mk in marcas:              # la marca aparece donde ARRANCA
            if mk["id"] not in vistos and \
               float(s["t_ini"]) <= float(mk["t_ini"]) < float(s["t_fin"]):
                vistos.add(mk["id"])
                out.append(_linea_marca(mk))
        out.append(f"{s['id']}  [{_fmt_t(s['t_ini'])}–{_fmt_t(s['t_fin'])}] {s['texto']}")
    faltan = [mk for mk in marcas if mk["id"] not in vistos]
    if faltan:                         # marcas en huecos sin voz — igual visibles
        out.append("\n## Marcas del autor fuera de frases (huecos sin voz)")
        out.extend(_linea_marca(mk) for mk in faltan)
    return "\n".join(out) + "\n"


# ---------------------------------------------------- transcript anotado (2ª) --
def _tiers_rango(ctx: _Ctx, t0: float, t1: float) -> dict[str, int]:
    """Tier por carril en el rango [t0,t1] (máximo de los eventos que solapan)."""
    t = dict.fromkeys(CARRILES, 0)

    def sube(carril, stream, filtro=None):
        for e in ctx.eventos(stream, t0, t1):
            if filtro and not filtro(e):
                continue
            t[carril] = max(t[carril], _tier(_fuerza(stream, e), ctx.umbrales[stream]))

    sube("voz", "voz.emocion")
    sube("risa", "voz.risa")
    sube("pausa", "voz.pausas")
    sube("cara", "video.cara.reaccion")
    sube("cara", "video.cara.emocion")
    sube("camara", "video.cara.mirada", lambda e: e.get("direccion") == "camara")
    # juego = ACTIVIDAD (golpes secos + energía de la escena), NO confianza de tags:
    # "música detectada con conf alta" no es gameplay intenso (fix r3 — un tag de música
    # de 18 min pintaba juego:2 en todas las líneas)
    sube("juego", "fondo.transitorios")
    sube("juego", "fondo.escenas")
    return t


def _carriles_activos(ctx: "_Ctx") -> list[str]:
    """Carriles CON ocurrencias en este video — los vacíos se declaran una vez en el
    header y no ensucian cada línea (fix r3: camara:· repetido 595 veces era ruido)."""
    FUENTES = {"voz": ["voz.emocion"], "risa": ["voz.risa"], "pausa": ["voz.pausas"],
               "cara": ["video.cara.reaccion", "video.cara.emocion"],
               "camara": ["video.cara.mirada"],
               "juego": ["fondo.transitorios", "fondo.escenas"]}
    activos = []
    for c in CARRILES:
        evs = [e for s in FUENTES[c] for e in ctx.streams.get(s, [])]
        if c == "camara":
            evs = [e for e in evs if e.get("direccion") == "camara"]
        if evs:
            activos.append(c)
    return activos


def _carril_str(tiers: dict[str, int], activos: list[str]) -> str:
    return " ".join(f"{c}:{tiers[c] if tiers[c] else '·'}" for c in activos
                    if c != "pausa" or tiers[c])   # pausa solo si tiene algo


def gen_transcript_anotado(ctx: _Ctx) -> str:
    activos = _carriles_activos(ctx)
    vacios = [c for c in CARRILES if c not in activos]
    out = [_provenance(ctx),
           "# TRANSCRIPT ANOTADO — 2ª lectura (mismo texto e IDs + actividad por carril)\n",
           "Solo se abre DESPUÉS de sellar tus candidatos semánticos (checkpoint del\n"
           "protocolo). Tiers por carril: 1 = actividad · 2 = notable para este video ·\n"
           "3 = ultra (percentiles del propio video). SIN z-scores ni emociones: qué tan\n"
           "fuerte y dónde — el QUÉ está en el índice de señales y los dossiers."
           + (f"\nCarriles SIN ocurrencias en este video (omitidos): {', '.join(vacios)}."
              if vacios else "") + "\n"]
    prev_fin = 0.0
    gap_n = 0

    def gap_line(t0, t1):
        nonlocal gap_n
        tg = _tiers_rango(ctx, t0, t1)
        # una reacción de CARA o RISA en un hueco sin voz es exactamente lo que no hay
        # que perder → alcanza tier 1; el resto de carriles exige ≥2 (fix r4: con
        # streams chicos el tier se degrada a 1 y el hueco desaparecía)
        if tg.get("cara", 0) >= 1 or tg.get("risa", 0) >= 1 \
           or any(v >= 2 for v in tg.values()):
            gap_n += 1
            out.append(f"G{gap_n:04d}  [{_fmt_t(t0)}–{_fmt_t(t1)}] "
                       f"⟨sin voz⟩  │ {_carril_str(tg, activos)}")

    for s in ctx.transcript:
        # hueco sin voz con actividad → línea propia (no perder la reacción muda;
        # umbral ≥1.2s: una reacción muda de 1.5s también cuenta — fix r3/r4)
        if float(s["t_ini"]) - prev_fin >= 1.2:
            gap_line(prev_fin, float(s["t_ini"]))
        ts = _tiers_rango(ctx, float(s["t_ini"]), float(s["t_fin"]))
        marca = " │ " + _carril_str(ts, activos) if any(ts.values()) else ""
        out.append(f"{s['id']}  [{_fmt_t(s['t_ini'])}–{_fmt_t(s['t_fin'])}] {s['texto']}{marca}")
        prev_fin = max(prev_fin, float(s["t_fin"]))
    if ctx.dur - prev_fin >= 1.2:              # el hueco FINAL también (fix r3)
        gap_line(prev_fin, ctx.dur)
    return "\n".join(out) + "\n"


# ------------------------------------------------------------ índice de señales --
def _pool(ctx: _Ctx) -> list[dict]:
    """Eventos-pico candidatos a semilla, con fuerza normalizada (rank 0-1 en su stream)."""
    import numpy as np
    pool = []
    FUENTES = [("voz.emocion", "voz", 1.2), ("voz.risa", "risa", None),
               ("video.cara.reaccion", "cara", None), ("video.cara.emocion", "valencia", None),
               ("fondo.transitorios", "juego", 3.0)]
    for stream, moda, minimo in FUENTES:
        evs = ctx.streams.get(stream, [])
        fzs = [f for e in evs if (f := _fuerza(stream, e)) is not None]
        if not fzs:
            continue
        orden = np.sort(np.array(fzs, dtype=float))
        for e in evs:
            f = _fuerza(stream, e)
            if f is None or (minimo is not None and f < minimo):
                continue
            rank = float(np.searchsorted(orden, f, side="right")) / len(orden)
            pool.append({"stream": stream, "moda": moda, "id": e["id"],
                         "t_pico": _t_pico(e), "t_ini": float(e["t_ini"]),
                         "t_fin": float(e.get("t_fin", e["t_ini"])),
                         "valor": round(float(f), 2), "rank": rank})
    return sorted(pool, key=lambda x: x["t_pico"])


def _nucleos(pool: list[dict]) -> list[list[dict]]:
    """Clusters de picos a ≤ COOCURRENCIA_S (núcleos; multimodal si ≥2 modalidades)."""
    grupos, cur = [], []
    for p in pool:
        if cur and p["t_pico"] - cur[-1]["t_pico"] > COOCURRENCIA_S:
            grupos.append(cur)
            cur = []
        cur.append(p)
    if cur:
        grupos.append(cur)
    return grupos


def _mk_seed(ctx: _Ctx, sid: int, carril: str, miembros: list[dict],
             fuerza: float, build_up: list) -> dict:
    t0 = min(m["t_ini"] for m in miembros)
    t1 = max(m["t_fin"] for m in miembros)
    return {
        "seed_id": f"sig-{sid:03d}", "carril": carril, "fuerza": round(min(fuerza, 1.0), 2),
        "nucleo": {"t_ini_ms": _ms(t0), "t_fin_ms": _ms(t1),
                   "transcript_ids": ctx.transcript_ids(t0 - 2, t1 + 2)},
        "coocurrencias": [{"evento_id": m["id"], "tipo": m["stream"],
                           "t_pico_ms": _ms(m["t_pico"]), "valor": m["valor"]}
                          for m in miembros],
        "build_up": build_up,
        "linea": ctx.linea(t0 - 2, t1 + 2) or "⟨sin voz⟩",
    }


def _build_ups(ctx: _Ctx, t_pico: float) -> list:
    """Pausas notables / tensión facial a ≤ BUILD_UP_S antes del pico → enlaces."""
    out = []
    for e in ctx.eventos("voz.pausas", t_pico - BUILD_UP_S, t_pico):
        if (e.get("dur_z") or 0) >= 1.0 and float(e["t_fin"]) <= t_pico:
            out.append({"relacion": "build_up_de", "evento_id": e["id"],
                        "tipo": "voz.pausas", "t_ini_ms": _ms(e["t_ini"]),
                        "t_fin_ms": _ms(e["t_fin"]), "dur": e.get("dur")})
    return out[:3]


def gen_indice(ctx: _Ctx) -> str:
    escala = max(ctx.dur / 1500.0, 0.5)
    cuotas = {k: max(1, int(round(v * escala))) for k, v in CUOTAS_25MIN.items()}
    pool = _pool(ctx)
    usados: list[tuple[float, float, frozenset]] = []   # (t0, t1, transcript_ids) aceptados
    seeds, descartes = [], []
    sid = 0

    def duplicada(t0, t1, ids):
        """Dup = cerca en el tiempo (<5s de gap) O comparte frases del transcript con
        una semilla aceptada — dedup SEMÁNTICO, no solo solape físico (fix r3: dos
        fragmentos del mismo beat a 452ms entraban como semillas distintas)."""
        for a, b, aids in usados:
            if t0 < b + 5.0 and a - 5.0 < t1:
                return True
            if ids and aids & ids:
                return True
        return False

    def acepta(carril, miembros, fuerza):
        nonlocal sid
        t0 = min(m["t_ini"] for m in miembros); t1 = max(m["t_fin"] for m in miembros)
        ids = frozenset(ctx.transcript_ids(t0 - 2, t1 + 2))
        if duplicada(t0, t1, ids):
            descartes.append((carril, t0, t1, fuerza, "duplica una semilla previa (mismo beat)"))
            return
        if sum(1 for s in seeds if s["carril"] == carril) >= cuotas.get(carril, 99):
            descartes.append((carril, t0, t1, fuerza, f"cuota de «{carril}» llena"))
            return
        sid += 1
        seeds.append(_mk_seed(ctx, sid, carril, miembros, fuerza,
                              _build_ups(ctx, min(m["t_pico"] for m in miembros))))
        usados.append((t0, t1, ids))

    # 0.a) marcas del autor (timeline): SIEMPRE, sin cuota (órdenes, no señales)
    for e in ctx.streams.get("autor.marcas", []):
        sid += 1
        dec = e.get("decision") or "nota"
        seeds.append({"seed_id": f"sig-{sid:03d}", "carril": "marca_autor", "fuerza": 1.0,
                      "nucleo": {"t_ini_ms": _ms(e["t_ini"]), "t_fin_ms": _ms(e["t_fin"]),
                                 "transcript_ids": ctx.transcript_ids(e["t_ini"], e["t_fin"])},
                      "coocurrencias": [{"evento_id": e["id"], "tipo": "autor.marcas",
                                         "t_pico_ms": _ms(e["t_ini"]), "valor": dec}],
                      "build_up": [],
                      "linea": f"[{dec.upper()}] «{(e.get('prompt') or '')[:70]}»"})
        usados.append((float(e["t_ini"]), float(e["t_fin"]),
                       frozenset(ctx.transcript_ids(e["t_ini"], e["t_fin"]))))

    # 0.b) instrucciones de Ava: SIEMPRE, sin cuota (órdenes, no señales)
    for e in ctx.streams.get("voz.instrucciones", []):
        sid += 1
        seeds.append({"seed_id": f"sig-{sid:03d}", "carril": "instruccion_ava", "fuerza": 1.0,
                      "nucleo": {"t_ini_ms": _ms(e["t_ini"]), "t_fin_ms": _ms(e["t_fin"]),
                                 "transcript_ids": ctx.transcript_ids(e["t_ini"], e["t_fin"])},
                      "coocurrencias": [{"evento_id": e["id"], "tipo": "voz.instrucciones",
                                         "t_pico_ms": _ms(e["t_ini"]), "valor": e.get("prob")}],
                      "build_up": [], "linea": f"«{e.get('palabra','')} {e.get('texto','')[:70]}»"})
        usados.append((float(e["t_ini"]), float(e["t_fin"]),
                       frozenset(ctx.transcript_ids(e["t_ini"], e["t_fin"]))))

    # 1) núcleos multimodales (≥2 modalidades a ≤3s), por fuerza
    nucleos = [g for g in _nucleos(pool) if len({m["moda"] for m in g}) >= 2]
    for g in sorted(nucleos, key=lambda g: -sum(sorted((m["rank"] for m in g), reverse=True)[:2]) / 2):
        acepta("multimodal", g, sum(sorted((m["rank"] for m in g), reverse=True)[:2]) / 2)

    # 2) carriles individuales (lo que no cayó en un núcleo aceptado)
    def carril_simple(carril, moda, filtro=None):
        cands = [p for p in pool if p["moda"] == moda and (not filtro or filtro(p))]
        for p in sorted(cands, key=lambda x: -x["rank"]):
            acepta(carril, [p], p["rank"])

    carril_simple("risa", "risa")
    carril_simple("voz", "voz")
    # cara MUDA: reacción SIN HABLA (ni transcript ni pico vocal cerca) — la sonrisa
    # que el audio no ve. Fix r3: "sin pico vocal" no era "sin voz".
    def es_muda(p):
        con_habla = bool(ctx.transcript_ids(p["t_ini"], p["t_fin"]))
        con_pico = any(q["moda"] == "voz" and abs(q["t_pico"] - p["t_pico"]) <= COOCURRENCIA_S
                       for q in pool)
        return not con_habla and not con_pico
    carril_simple("cara_muda", "cara", es_muda)
    carril_simple("valencia", "valencia")
    # camara: mirada a cámara sostenida (≥1.5s)
    for e in sorted(ctx.streams.get("video.cara.mirada", []),
                    key=lambda e: -(e["t_fin"] - e["t_ini"])):
        if e.get("direccion") == "camara" and (e["t_fin"] - e["t_ini"]) >= 1.5:
            acepta("camara", [{"stream": "video.cara.mirada", "moda": "camara", "id": e["id"],
                               "t_pico": _t_pico(e), "t_ini": float(e["t_ini"]),
                               "t_fin": float(e["t_fin"]),
                               "valor": round(e["t_fin"] - e["t_ini"], 1),
                               "rank": min((e["t_fin"] - e["t_ini"]) / 10.0, 1.0)}],
                   min((e["t_fin"] - e["t_ini"]) / 10.0, 1.0))
    # pausa → reacción (build-up clásico de gameplay): el MEJOR pico de la ventana,
    # no el primero (fix r3: entraba una semilla de fuerza 0.09 ocupando cuota)
    for e in ctx.streams.get("voz.pausas", []):
        if (e.get("dur_z") or 0) < 1.5:
            continue
        cands = [p for p in pool
                 if float(e["t_fin"]) <= p["t_pico"] <= float(e["t_fin"]) + BUILD_UP_S]
        if cands:
            pico = max(cands, key=lambda p: p["rank"])
            if pico["rank"] >= 0.5:              # un pico débil no justifica la cuota
                acepta("pausa_reaccion", [pico], pico["rank"])

    seeds.sort(key=lambda s: s["nucleo"]["t_ini_ms"])
    for i, s in enumerate(seeds, 1):             # re-numerar cronológico
        s["seed_id"] = f"sig-{i:03d}"

    out = [_provenance(ctx), "# ÍNDICE DE SEÑALES — semillas por carril (cuotas, no top-K)\n",
           f"Cuotas para {ctx.dur/60:.0f} min: {cuotas}. Las semillas NO son decisiones: "
           "son lugares donde las MEDICIONES vieron algo. Tus candidatos semánticos del "
           "transcript valen igual o más — fusioná ambos. Dossier de un momento: "
           "`vistas.py dossier <master> --seed <id>` (o --rango).\n"]
    for s in seeds:
        out.append(json.dumps(s, ensure_ascii=False))
    out.append(f"\n## Frontera de descartes (casi entraron — {min(len(descartes), DESCARTES_N)} "
               f"de {len(descartes)}; si un carril entero quedó afuera, acá se nota)")
    for carril, t0, t1, fz, razon in sorted(descartes, key=lambda d: -d[3])[:DESCARTES_N]:
        out.append(f"- {carril} [{_fmt_t(t0)}–{_fmt_t(t1)}] fuerza={fz:.2f} → {razon} · "
                   f"«{ctx.linea(t0, t1, 60) or '⟨sin voz⟩'}»")
    return "\n".join(out) + "\n"


# ----------------------------------------------------------------- directivas --
def gen_directivas(ctx: _Ctx) -> str | None:
    """El LEDGER de órdenes del autor (consenso timeline-marcas r2 h.6): marcas del
    timeline + instrucciones habladas a Ava, unificadas y ordenadas. Se lee ANTES de
    señales y candidatos (protocolo /clipear). None si no hay ninguna (r2-r5: no
    generar un ledger vacío)."""
    marcas = ctx.streams.get("autor.marcas", [])
    ava = ctx.streams.get("voz.instrucciones", [])
    if not marcas and not ava:
        return None
    aut = (ctx.m.get("modalidades") or {}).get("autor") or {}
    out = [_provenance(ctx),
           "# DIRECTIVAS DEL AUTOR — leer ANTES de señales y candidatos\n",
           "Órdenes explícitas del autor: restricciones DURAS de la EDL (incluir/excluir)\n"
           "y directivas editoriales de máxima prioridad (prompts). Las marcas de UI\n"
           "señalan el OBJETO de la orden; el tramo hablado a Ava se EXCLUYE de los\n"
           "clips (contiene la orden, no es contenido).\n"]
    if marcas:
        out.append("## Marcas del timeline (wizard)")
        for e in sorted(marcas, key=lambda x: float(x["t_ini"])):
            dec = e.get("decision")
            regla = {"incluir": "⛔ RESTRICCIÓN DURA: este tramo VA en el video final",
                     "excluir": "⛔ RESTRICCIÓN DURA: este tramo NO va en el video final",
                     None: "directiva editorial (anchor del autor)"}[dec]
            rango = (f"[{_fmt_t(e['t_ini'])}–{_fmt_t(e['t_fin'])}]"
                     if e["t_fin"] != e["t_ini"] else f"[{_fmt_t(e['t_ini'])}] (punto)")
            out.append(f"- **{e['id']}** {rango} `{(dec or 'nota').upper()}` — {regla}")
            if e.get("prompt"):
                out.append(f"  > {e['prompt']}")
            ctxlinea = ctx.linea(float(e["t_ini"]), float(e["t_fin"]), 80)
            if ctxlinea:
                out.append(f"  · contexto: «{ctxlinea}»")
    conf = aut.get("conflictos") or []
    if conf:
        out.append("\n## ⚠ CONFLICTOS incluir∩excluir (regla declarada: EXCLUIR GANA)")
        out.append("No los resuelvas en silencio: aplicá la regla y AVISALE al autor.")
        for c in conf:
            out.append(f"- {' ∩ '.join(c['ids'])} → [{_fmt_t(c['t_ini'])}–{_fmt_t(c['t_fin'])}]")
    if ava:
        out.append("\n## Instrucciones habladas a Ava (el tramo se EXCLUYE de los clips)")
        for e in sorted(ava, key=lambda x: float(x["t_ini"])):
            out.append(f"- **{e['id']}** [{_fmt_t(e['t_ini'])}–{_fmt_t(e['t_fin'])}] "
                       f"«{e.get('texto', '')}»")
    return "\n".join(out) + "\n"


# ----------------------------------------------------------------- inventario --
def gen_inventario(ctx: _Ctx) -> dict:
    inv = {
        "vista_version": VERSION,
        "master_sha256": ctx.sha,
        "generado": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "video": ctx.m.get("video"),
        "duracion": ctx.dur,
        "header_master": ctx.m.get("header", {}),
        # las superficies del bundle (directivas.md solo si hay órdenes del autor —
        # el ledger se declara acá para que el protocolo sepa que existe, impl h.8)
        "superficies": ["transcript.md", "transcript_anotado.md", "indice_senales.md"]
        + (["directivas.md"] if (ctx.streams.get("autor.marcas")
                                 or ctx.streams.get("voz.instrucciones")) else []),
        "streams": {},
        "anomalias": [],
    }
    for k, evs in ctx.streams.items():
        inv["streams"][k] = {"n": len(evs), "por_min": round(len(evs) / (ctx.dur / 60), 2),
                             "fuerza_pctl": {q: round(v, 2)
                                             for q, v in ctx.umbrales[k].items()}}
    if ctx.m.get("header", {}).get("duracion_fuente") == "max_t_fin":
        inv["anomalias"].append("duración estimada por max_t_fin (no ffprobe)")
    if not ctx.streams.get("voz.transcript"):
        inv["anomalias"].append("SIN transcript — el protocolo de lectura no aplica")
    return inv


# -------------------------------------------------------------------- dossier --
def dossier(master_path, t0: float | None = None, t1: float | None = None,
            seed: str | None = None) -> str:
    m, sha = cargar(master_path)
    ctx = _Ctx(m, sha)
    if seed:
        idx = gen_indice(ctx)                    # determinista → mismos seeds
        s = next((json.loads(l) for l in idx.splitlines()
                  if l.startswith("{") and f'"{seed}"' in l), None)
        if not s:
            return f"⚠ no existe la semilla {seed}"
        t0, t1 = s["nucleo"]["t_ini_ms"] / 1000, s["nucleo"]["t_fin_ms"] / 1000
    c0, c1 = t0 - 15, t1 + 15
    out = [_provenance(ctx, extra=f" · dossier [{_fmt_t(t0)}–{_fmt_t(t1)}]"),
           f"# DOSSIER [{_fmt_t(t0)}–{_fmt_t(t1)}] (contexto ±15s)\n",
           "## Transcript (▶ = dentro del rango)"]
    for s in ctx.transcript:
        if _solapa(s, c0, c1):
            marca = "▶" if _solapa(s, t0, t1) else " "
            out.append(f"{marca} {s['id']}  [{_fmt_t(s['t_ini'])}–{_fmt_t(s['t_fin'])}] {s['texto']}")
    # contexto visual VLM: renderer PROPIO (el resumen genérico descarta listas/dicts
    # y perdía los eventos internos — consenso vision r1.6)
    vlm = [(k, e) for k in ("video.juego.vlm", "video.camara.vlm")
           for e in ctx.eventos(k, t0, t1)]
    if vlm:
        out.append("\n## Contexto visual (VLM — descripciones de MUESTRAS del video)")
        for k, e in vlm:
            lane = "JUEGO" if "juego" in k else "CÁMARA"
            extra = (f" escena={e.get('escena')}" if lane == "JUEGO"
                     else f" emoción_aparente={e.get('emocion_aparente')}"
                          f" presencia={e.get('presencia')}")
            out.append(f"### {e['id']} {lane} [{_fmt_t(e['t_ini'])}–{_fmt_t(e['t_fin'])}]"
                       f"{extra} · obs={e.get('observabilidad')}"
                       f" conf={e.get('confianza')} ({e.get('frames')} frame(s))")
            if e.get("descripcion"):
                out.append(f"  {e['descripcion']}")
            if e.get("texto_relevante"):
                out.append(f"  · texto en pantalla: «{e['texto_relevante']}»")
            for ev in e.get("eventos") or []:
                out.append(f"  · {_fmt_t(ev['t'])} [{ev.get('tipo')}] {ev.get('detalle')}")
    out.append("\n## Señales en el rango (±10s)")
    for k, evs in ctx.streams.items():
        if k == "voz.transcript" or k.endswith(".vlm"):
            continue
        hits = [e for e in evs if _solapa(e, t0 - 10, t1 + 10)]
        if not hits:
            continue
        out.append(f"### {k} ({len(hits)})")
        for e in hits:
            resumen = {kk: vv for kk, vv in e.items()
                       if kk not in ("t_ini", "t_fin") and not isinstance(vv, (list, dict))}
            out.append(f"- {e.get('id','?')} [{_fmt_t(e['t_ini'])}–"
                       f"{_fmt_t(e.get('t_fin', e['t_ini']))}] "
                       + " ".join(f"{kk}={vv}" for kk, vv in list(resumen.items())[:8]))
    out.append("\n## Bordes candidatos (pausas ≥0.45s / fin de frase — cortá ACÁ)")
    for e in ctx.eventos("voz.pausas", c0, c1):
        if float(e.get("dur", 0)) >= 0.45:
            ff = " (fin de frase)" if e.get("fin_frase_previa") else ""
            out.append(f"- {e['id']} [{_fmt_t(e['t_ini'])}–{_fmt_t(e['t_fin'])}] "
                       f"dur {e['dur']}s{ff} · …{e.get('palabra_previa')} | "
                       f"{e.get('palabra_sig')}…")
    ins = ctx.eventos("voz.instrucciones", c0, c1)
    if ins:
        out.append("\n## ⚠ INSTRUCCIONES DE AVA EN EL RANGO (excluir de los clips)")
        for e in ins:
            out.append(f"- {e['id']} [{_fmt_t(e['t_ini'])}–{_fmt_t(e['t_fin'])}] «{e.get('texto','')}»")
    mks = ctx.eventos("autor.marcas", c0, c1)
    if mks:
        out.append("\n## ⚠ MARCAS DEL AUTOR EN EL RANGO (directivas.md manda)")
        for e in mks:
            out.append(f"- {e['id']} [{_fmt_t(e['t_ini'])}–{_fmt_t(e['t_fin'])}] "
                       f"{(e.get('decision') or 'nota').upper()}"
                       + (f" «{e['prompt']}»" if e.get("prompt") else ""))
    return "\n".join(out) + "\n"


# -------------------------------------------------------------------- generar --
def _recuperar_swap(outdir: Path) -> None:
    """Auto-reparación del swap de generar(): si un kill duro cayó ENTRE los dos renames,
    el bundle quedó como .old y outdir ausente — se restaura. Idempotente y barato."""
    viejo = outdir.parent / f".{outdir.name}.old"
    if viejo.exists() and not outdir.exists():
        try:
            viejo.rename(outdir)
        except OSError:
            pass


def generar(master_path, outdir=None, log_cb=None) -> Path:
    """Genera el bundle completo de vistas ATÓMICAMENTE (tmp → rename)."""
    def log(msg):
        if log_cb:
            log_cb(msg)

    master_path = Path(master_path)
    m, sha = cargar(master_path)
    if m.get("header", {}).get("schema_version", 1) < 2:
        raise RuntimeError("El master es schema v1 (sin header/IDs) — regenerá el master "
                           "con consolidar.py actual antes de generar vistas.")
    ctx = _Ctx(m, sha)
    outdir = Path(outdir) if outdir else master_path.parent / "vistas"
    tmp = outdir.parent / f".{outdir.name}.tmp"
    if tmp.exists():
        shutil.rmtree(tmp)
    tmp.mkdir(parents=True)
    (tmp / "inventario.json").write_text(
        json.dumps(gen_inventario(ctx), ensure_ascii=False, indent=1), encoding="utf-8")
    (tmp / "transcript.md").write_text(gen_transcript(ctx), encoding="utf-8")
    (tmp / "transcript_anotado.md").write_text(gen_transcript_anotado(ctx), encoding="utf-8")
    (tmp / "indice_senales.md").write_text(gen_indice(ctx), encoding="utf-8")
    dirs = gen_directivas(ctx)         # solo si hay marcas del autor o Ava (consenso r3.5)
    if dirs:
        (tmp / "directivas.md").write_text(dirs, encoding="utf-8")
    # swap SIN pérdida: el bundle viejo se APARTA (rename), entra el nuevo, y recién
    # entonces se borra el viejo. No existe swap atómico de directorios multiplataforma,
    # así que entre los dos renames queda una ventana de microsegundos SI el proceso es
    # MATADO justo ahí — por eso el estado es AUTO-REPARABLE: _recuperar_swap() (acá y
    # al inicio de generar) restaura el .old si el bundle quedó ausente.
    viejo = outdir.parent / f".{outdir.name}.old"
    _recuperar_swap(outdir)                    # por si un crash anterior dejó .old colgado
    if viejo.exists():
        shutil.rmtree(viejo)
    if outdir.exists():
        outdir.rename(viejo)
    try:
        tmp.rename(outdir)
    except Exception:
        if viejo.exists() and not outdir.exists():
            viejo.rename(outdir)               # rollback: restaurar el bundle anterior
        raise
    if viejo.exists():
        shutil.rmtree(viejo, ignore_errors=True)
    n_seeds = sum(1 for l in (outdir / "indice_senales.md").read_text(encoding="utf-8")
                  .splitlines() if l.startswith("{"))
    log(f"vistas → {outdir} (transcript {len(ctx.transcript)} frases · {n_seeds} semillas)")
    return outdir


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Vistas de lectura para la AI orquestadora.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    g = sub.add_parser("generar", help="genera el bundle vistas/ junto al master")
    g.add_argument("master"); g.add_argument("--outdir", default=None)
    d = sub.add_parser("dossier", help="dossier de un momento (por rango o semilla)")
    d.add_argument("master")
    d.add_argument("--rango", nargs=2, type=float, metavar=("T0", "T1"))
    d.add_argument("--seed", default=None)
    a = ap.parse_args()
    if a.cmd == "generar":
        generar(a.master, a.outdir, log_cb=print)
    else:
        if not a.rango and not a.seed:
            ap.error("dossier necesita --rango T0 T1 o --seed sig-###")
        print(dossier(a.master, *(a.rango or (None, None)), seed=a.seed))
