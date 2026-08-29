#!/usr/bin/env python3
"""
edl.py — la EDL (Edit Decision List) como ARTEFACTO DE PRIMERA CLASE + el loop de
revisión del rough cut. Diseño consensuado con Codex gpt-5.6-sol (2026-07-16, ver
three-brain-out/2026-07-16-orquestador-metadata/DISENO-final.md).

La AI orquestadora NO "ejecuta cortes": produce una EDL (json, tiempos en MS ENTEROS,
bordes anclados a palabras del words.json). De la EDL salen, deterministas:
  · validar  — GATE MECÁNICO de bordes: ningún corte dentro de una palabra (±120 ms de
               su audio), ni partiendo diálogo del juego; frontera NATURAL (puntuación de
               cierre o pausa ≥450 ms) o join_policy declarada. Mid-frase sin policy =
               error. `editorial_exception` queda en frontera de revisión (NO
               auto-aprobable). Resuelve y escribe word_in_id/word_out_id.
  · render   — el rough cut reproducible (reusa tools/cortar_clip.py: frame-accurate).
  · cutview  — lo que Ava ve de SU PROPIA edición (nunca un master_cut completo):
               - cut_timeline.json    (items con rango CUT y rango SOURCE)
               - transcript_cut.md    (NUNCA prosa continua: separadores [SEGMENTO]/[CORTE]
                                       imposibles de ocultar, con los scores por junta)
               - junction_cards.json  (una card por junta: últimas/primeras palabras,
                                       pausas, contexto ORIGINAL de ambos lados, salto
                                       temporal firmado, checks mecánicos, riesgo_base)
               - eventos_cut.json     (remap con SEMÁNTICA DE INTERVALOS: recorte/split
                                       en bordes, half-open [in,out), instancias con
                                       source_event_id — los z originales se conservan)
               - intensity_curve.json (curva de intensidad en tiempo del CUT — pacing)
               - provenance.json      (hashes de edl/master/words)

DOS SCORES POR JUNTA, nunca promediados:
  · riesgo_base (0-100, determinista, acá):
      +30 borde izquierdo mid-frase · +30 derecho mid-frase · +15 starter dependiente
      +15 cláusula abierta a la izquierda · +10 cambio de escena ·
      salto temporal: +0 ≤12s / +8 ≤60s / +15 ≤300s / +22 >300s ·
      +25 no-cronológico sin role=teaser
  · riesgo_semantico (0-100, JUICIO DE AVA con la junction card — el campo
      `semantic_review` viene null y lo completa ella; ver skill /clipear).
  Gates: regla dura falla → reject · base ≥50 → revisión obligatoria ·
         semántico ≥60 → revisar · cualquiera ≥80 → bloquear.

Uso CLI:
  python edl.py validar  <edl.json> --master <master.json> --words <words.json>
  python edl.py render   <edl.json> --fuente <video> --salida <out.mp4> [--pistas 0,1]
  python edl.py cutview  <edl.json> --master <master.json> --words <words.json> [--outdir]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

VERSION = "edl-1.0"
PAUSA_NATURAL_MS = 450      # pausa que hace "natural" un borde (450, no 300: 300 aparece
                            # dentro de locuciones normales — decisión de la ronda 2)
CLEARANCE_MS = 120          # distancia mínima del corte al audio de cualquier palabra
FIN_FRASE = (".", "!", "?", "…")
# Arranques que dependen de contexto previo (lista cerrada de la ronda 2): si el clip B
# empieza así, la junta huele a falsa continuidad → +15 de riesgo_base (solo alarma).
STARTERS_DEPENDIENTES = {
    "y", "pero", "porque", "entonces", "por", "así", "asi",
    "él", "el", "ella", "ellos", "ellas", "eso", "esto", "aquello",
    "ahí", "ahi", "allí", "alli", "acá", "aca", "también", "tambien", "tampoco",
    "otra", "como", "lo",
}
POLICIES = {"hard_cut", "intentional_compression", "editorial_exception"}


# ------------------------------------------------------------------ utilidades --
def _sha256(p) -> str | None:
    try:
        return hashlib.sha256(Path(p).read_bytes()).hexdigest()
    except Exception:
        return None


def _hash_editorial(edl: dict) -> str:
    """Hash del CONTENIDO EDITORIAL de la EDL (source + sequence, canónico). La
    validación lo sella; render/cutview lo recomputan — editar la EDL después de
    validar la vuelve stale y se rechaza (fix review r3)."""
    contenido = {"source": edl.get("source"), "sequence": edl.get("sequence")}
    return hashlib.sha256(json.dumps(contenido, ensure_ascii=False,
                                     sort_keys=True).encode()).hexdigest()


def _exigir_validada(edl: dict, quien: str) -> None:
    v = edl.get("validacion") or {}
    if v.get("resultado") != "OK":
        raise RuntimeError(f"{quien}: la EDL no está validada (corré `edl.py validar`).")
    if v.get("edl_hash") != _hash_editorial(edl):
        raise RuntimeError(f"{quien}: la EDL fue EDITADA después de validar (hash "
                           "editorial no coincide) — re-validá antes de seguir.")


def _norm_w(w: str) -> str:
    import unicodedata
    s = unicodedata.normalize("NFD", (w or "").lower())
    return "".join(c for c in s if c.isalpha() and not unicodedata.combining(c))


def _fmt_t(ms: int) -> str:
    m, s = divmod(ms / 1000.0, 60.0)
    return f"{int(m):02d}:{s:06.3f}"


def cargar_words(words_json) -> list[dict]:
    ws = json.loads(Path(words_json).read_text(encoding="utf-8"))
    ws = sorted(ws, key=lambda w: w["start"])
    for i, w in enumerate(ws):
        w["wid"] = f"w{i:06d}"
        w["start_ms"], w["end_ms"] = round(w["start"] * 1000), round(w["end"] * 1000)
    return ws


def _solapa_ms(e: dict, t0_ms: int, t1_ms: int) -> bool:
    """Solape HALF-OPEN [t0, t1): un evento puntual exactamente en el borde derecho NO
    entra (convención de la ronda 2 para no duplicar instancias entre segmentos)."""
    e0 = round(float(e.get("t_ini", 0)) * 1000)
    e1 = round(float(e.get("t_fin", e.get("t_ini", 0))) * 1000)
    return e0 < t1_ms and (e1 > t0_ms or (e1 == e0 and e0 >= t0_ms))


# ------------------------------------------------------------ gate de bordes --
def _borde(words: list[dict], t_ms: int, lado: str) -> dict:
    """Analiza UN borde de corte contra words.json. lado = 'in' | 'out'."""
    r = {"t_ms": t_ms, "lado": lado, "dentro_de_palabra": False, "clearance_ok": True,
         "natural": False, "motivo_natural": None, "palabra_cercana": None}
    prev = nxt = None
    for w in words:
        if w["start_ms"] - CLEARANCE_MS < t_ms < w["end_ms"] + CLEARANCE_MS:
            r["dentro_de_palabra"] = w["start_ms"] < t_ms < w["end_ms"]
            r["clearance_ok"] = False
            r["palabra_cercana"] = {"wid": w["wid"], "word": w["word"].strip(),
                                    "start_ms": w["start_ms"], "end_ms": w["end_ms"]}
        if w["end_ms"] <= t_ms:
            prev = w
        if nxt is None and w["start_ms"] >= t_ms:
            nxt = w
    # frontera natural
    if lado == "out":
        if prev is None or nxt is None:
            r["natural"], r["motivo_natural"] = True, "borde_de_archivo"
        elif prev["word"].rstrip("\"')»”").rstrip().endswith(FIN_FRASE):
            r["natural"], r["motivo_natural"] = True, "puntuacion_de_cierre"
        elif nxt["start_ms"] - prev["end_ms"] >= PAUSA_NATURAL_MS:
            r["natural"], r["motivo_natural"] = True, f"pausa_{nxt['start_ms']-prev['end_ms']}ms"
    else:
        if prev is None or nxt is None:
            r["natural"], r["motivo_natural"] = True, "borde_de_archivo"
        elif prev["word"].rstrip("\"')»”").rstrip().endswith(FIN_FRASE):
            r["natural"], r["motivo_natural"] = True, "frase_previa_cerrada"
        elif nxt["start_ms"] - prev["end_ms"] >= PAUSA_NATURAL_MS:
            r["natural"], r["motivo_natural"] = True, f"pausa_{nxt['start_ms']-prev['end_ms']}ms"
    r["palabra_previa"] = prev and {"wid": prev["wid"], "word": prev["word"].strip(),
                                    "end_ms": prev["end_ms"]}
    r["palabra_sig"] = nxt and {"wid": nxt["wid"], "word": nxt["word"].strip(),
                                "start_ms": nxt["start_ms"]}
    return r


def validar(edl_path, master_path, words_json, log_cb=print) -> dict:
    """Gate mecánico completo. Escribe word_in_id/word_out_id resueltos en la EDL y un
    reporte <edl>.validacion.json. Exit duro si hay errores."""
    edl_path = Path(edl_path)
    edl = json.loads(edl_path.read_text(encoding="utf-8"))
    master = json.loads(Path(master_path).read_text(encoding="utf-8"))
    words = cargar_words(words_json)
    dur_ms = round(float(master["duracion"]) * 1000)
    dialogo = master["streams"].get("fondo.dialogo", [])
    errores, avisos, frontera_revision = [], [], []

    # ---- schema mínimo (fix review r3) ----
    if edl.get("schema") != "edl/1.0":
        errores.append(f"schema desconocido: {edl.get('schema')!r} (esperado edl/1.0)")
    rid = edl.get("revision_id")
    if not (isinstance(rid, str) and rid.strip()):
        errores.append(f"revision_id inválido: {rid!r} (string no vacío, ej. edl-r0001)")
    src = edl.get("source")
    if not (isinstance(src, dict) and isinstance(src.get("path"), str)
            and Path(src["path"].strip()).name):
        errores.append("source.path faltante o sin nombre de archivo "
                       "(la ruta del medio que se corta)")

    seq = edl.get("sequence", [])
    if not seq:
        errores.append("EDL sin sequence")
    vistos = set()
    for i, it in enumerate(seq):
        iid = it.get("item_id") or f"?{len(vistos)}"
        if iid in vistos:
            errores.append(f"{iid}: item_id duplicado")
        vistos.add(iid)
        t0, t1 = it.get("source_in_ms"), it.get("source_out_ms")
        if not (isinstance(t0, int) and isinstance(t1, int)):
            errores.append(f"{iid}: source_in_ms/source_out_ms deben ser MS ENTEROS")
            continue
        if not (0 <= t0 < t1 <= dur_ms + 500):
            errores.append(f"{iid}: rango inválido [{t0},{t1}] (dur={dur_ms})")
            continue
        if t1 - t0 < 1000:
            avisos.append(f"{iid}: tramo muy corto ({t1-t0} ms)")
        # join_policy = propiedad de la JUNTA que PRECEDE a este item (gobierna el out
        # del item anterior y el in de este — fix review r3). En el primer item gobierna
        # solo su in (el arranque del clip).
        pol = it.get("join_policy", "hard_cut")
        if pol not in POLICIES:
            errores.append(f"{iid}: join_policy desconocida «{pol}»")
        if pol != "hard_cut" and not it.get("exception_reason"):
            errores.append(f"{iid}: join_policy={pol} requiere exception_reason")
        if pol == "editorial_exception":
            frontera_revision.append(iid)
        # intentional_compression = comprimir DENTRO de la misma locución: exige junta
        # cronológica con salto corto (≤12s). Todo lo demás es editorial_exception (r4).
        if pol == "intentional_compression":
            if i == 0:
                errores.append(f"{iid}: intentional_compression sin junta previa "
                               "(primer item) — usá editorial_exception")
            else:
                prev_out = seq[i - 1].get("source_out_ms")
                if not isinstance(prev_out, int):
                    errores.append(f"{iid}: intentional_compression con item previo "
                                   "de tiempos inválidos")
                elif not (0 <= t0 - prev_out <= 12_000):
                    errores.append(f"{iid}: intentional_compression con salto de "
                                   f"{t0 - prev_out} ms (debe ser cronológico y ≤12000) "
                                   "— usá editorial_exception")
        # bordes contra words: el IN se juzga con la policy propia; el OUT con la policy
        # del item SIGUIENTE (misma junta); el out del ÚLTIMO exige natural u out_policy.
        pol_out = (seq[i + 1].get("join_policy", "hard_cut") if i + 1 < len(seq)
                   else it.get("out_policy", "hard_cut"))
        if i + 1 >= len(seq):
            if pol_out not in ("hard_cut", "editorial_exception"):
                errores.append(f"{iid}: out_policy inválida «{pol_out}» (el out final "
                               "solo admite hard_cut o editorial_exception)")
            elif pol_out != "hard_cut" and not it.get("out_exception_reason"):
                errores.append(f"{iid}: out_policy={pol_out} requiere out_exception_reason")
        for lado, t, pol_lado in (("in", t0, pol), ("out", t1, pol_out)):
            b = _borde(words, t, lado)
            it[f"_borde_{lado}"] = b
            if b["dentro_de_palabra"]:
                errores.append(f"{iid}.{lado}: el corte cae DENTRO de la palabra "
                               f"{b['palabra_cercana']}")
            elif not b["clearance_ok"]:
                errores.append(f"{iid}.{lado}: corte a <{CLEARANCE_MS}ms del audio de "
                               f"{b['palabra_cercana']} — movelo a la pausa")
            if not b["natural"] and pol_lado == "hard_cut":
                errores.append(f"{iid}.{lado}: borde MID-FRASE sin join_policy declarada "
                               "(usa intentional_compression o editorial_exception con "
                               "exception_reason)")
            for d in dialogo:
                d0, d1 = round(d["t_ini"] * 1000), round(d["t_fin"] * 1000)
                if d0 < t < d1:
                    errores.append(f"{iid}.{lado}: corta diálogo del juego "
                                   f"«{d.get('texto','')[:40]}» — mové el borde")
        # anclas de palabra OBLIGATORIAS (o tramo declarado mudo — fix review r3)
        dentro = [w for w in words if t0 <= w["start_ms"] and w["end_ms"] <= t1]
        it["word_in_id"] = dentro[0]["wid"] if dentro else None
        it["word_out_id"] = dentro[-1]["wid"] if dentro else None
        if not dentro and not it.get("no_speech"):
            errores.append(f"{iid}: tramo sin NINGUNA palabra — si es a propósito "
                           "declaralo con \"no_speech\": true")

    # ---- gate ENTRE items: orden, solape y teasers (bloqueante r3) ----
    ok_items = [it for it in seq if isinstance(it.get("source_in_ms"), int)
                and isinstance(it.get("source_out_ms"), int)]
    for i in range(1, len(ok_items)):
        A, B = ok_items[i - 1], ok_items[i]
        if B["source_in_ms"] < A["source_out_ms"]:
            if A.get("role") == "teaser":
                if B.get("join_policy", "hard_cut") == "hard_cut":
                    errores.append(f"{B['item_id']}: regreso temporal tras teaser "
                                   f"{A['item_id']} exige join_policy explícita")
            else:
                errores.append(f"{A['item_id']}→{B['item_id']}: NO cronológico "
                               f"({B['source_in_ms']} < {A['source_out_ms']}) sin "
                               "role=teaser en el item anterior")
    for i, A in enumerate(ok_items):
        for B in ok_items[i + 1:]:
            a0, a1 = A["source_in_ms"], A["source_out_ms"]
            b0, b1 = B["source_in_ms"], B["source_out_ms"]
            if a0 < b1 and b0 < a1:              # intersección de origen
                if (a0, a1) == (b0, b1):
                    errores.append(f"{A['item_id']}/{B['item_id']}: mismo tramo EXACTO "
                                   "dos veces (un teaser adelanta una PARTE, no duplica)")
                elif A.get("role") != "teaser" and B.get("role") != "teaser":
                    errores.append(f"{A['item_id']}/{B['item_id']}: tramos de origen "
                                   "SOLAPADOS sin role=teaser")

    reporte = {
        "edl": edl_path.name, "revision_id": edl.get("revision_id"),
        "validado": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "resultado": "RECHAZADA" if errores else "OK",
        "errores": errores, "avisos": avisos,
        "frontera_revision": frontera_revision,
    }
    if not errores:                       # solo una EDL válida se escribe normalizada
        for it in seq:                    # limpiar análisis ANTES del hash editorial
            it.pop("_borde_in", None); it.pop("_borde_out", None)
        edl["validacion"] = {"resultado": "OK", "validado": reporte["validado"],
                             "edl_hash": _hash_editorial(edl),
                             "master_sha256": _sha256(master_path),
                             "words_sha256": _sha256(words_json)}
        edl_path.write_text(json.dumps(edl, ensure_ascii=False, indent=1), encoding="utf-8")
    edl_path.with_suffix(".validacion.json").write_text(
        json.dumps(reporte, ensure_ascii=False, indent=1), encoding="utf-8")
    log_cb(f"validación: {reporte['resultado']} · {len(errores)} error(es) · "
           f"{len(avisos)} aviso(s)"
           + (f" · frontera de revisión: {frontera_revision}" if frontera_revision else ""))
    for e in errores:
        log_cb(f"  ✗ {e}")
    for a in avisos:
        log_cb(f"  ⚠ {a}")
    return reporte


# ------------------------------------------------------------------- render --
def render(edl_path, fuente, salida, pistas="0,1", log_cb=print) -> Path:
    """Rough cut reproducible desde la EDL (reusa cortar_clip.py: frame-accurate)."""
    edl = json.loads(Path(edl_path).read_text(encoding="utf-8"))
    _exigir_validada(edl, "render")
    declarado = Path(edl.get("source", {}).get("path", "")).name
    if declarado and Path(fuente).name != declarado:
        raise RuntimeError(f"render: la fuente «{Path(fuente).name}» no es la declarada "
                           f"en la EDL («{declarado}») — cortarías otro video.")
    cortar = next((p for p in (Path("tools/cortar_clip.py"), Path("../tools/cortar_clip.py"),
                               Path("/mnt/data/productions/tools/cortar_clip.py")) if p.exists()),
                  None)
    if not cortar:
        raise RuntimeError("No encuentro cortar_clip.py")
    tramos = [f"{it['source_in_ms']/1000:.3f}-{it['source_out_ms']/1000:.3f}"
              for it in edl["sequence"]]
    cmd = [sys.executable, str(cortar), str(fuente), str(salida), *tramos, "--pistas", pistas]
    log_cb("render: " + " ".join(cmd[1:]))
    # encoding explícito: en Windows text=True decodifica con la página ANSI y un
    # nombre de archivo no representable en stderr rompía el reporte del fallo
    r = subprocess.run(cmd, capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    if r.returncode != 0:
        raise RuntimeError(f"render falló: {r.stderr[-800:]}")
    return Path(salida)


# ------------------------------------------------------------------ cut_view --
def _texto_rango(master: dict, t0_ms: int, t1_ms: int) -> str:
    """Transcript ORIGINAL del rango (frases completas de whisper — para CONTEXTO de las
    junction cards, donde el diseño pide extender hasta cerrar la unidad verbal)."""
    segs = [s for s in master["streams"].get("voz.transcript", [])
            if _solapa_ms(s, t0_ms, t1_ms)]
    return " ".join(s["texto"] for s in segs)


def _texto_audible(words: list[dict], t0_ms: int, t1_ms: int) -> str:
    """SOLO las palabras cuyo audio queda dentro del tramo — lo que de verdad se OYE en
    el cut (fix review r3: los segmentos de whisper mostraban texto recortado como si
    sonara)."""
    return " ".join(w["word"].strip() for w in words
                    if w["start_ms"] >= t0_ms and w["end_ms"] <= t1_ms)


def _riesgo_base(card: dict) -> tuple[int, list[str]]:
    r, razones = 0, []
    if card["left"]["mid_phrase"]:
        r += 30; razones.append("borde izquierdo a mitad de frase")
    if card["right"]["mid_phrase"]:
        r += 30; razones.append("borde derecho a mitad de frase")
    if card["right"]["dependent_starter"]:
        r += 15; razones.append(f"B arranca con dependiente «{card['right']['first_words'][0]['text'] if card['right']['first_words'] else '?'}»")
    if card["left"]["clausula_abierta"]:
        r += 15; razones.append("A queda con cláusula abierta (sin puntuación de cierre)")
    if not card["transition"]["same_scene"]:
        r += 10; razones.append("cambia de escena de fondo")
    salto = abs(card["transition"]["source_jump_ms"])
    if salto > 300_000:
        r += 22; razones.append(f"salto de {salto//1000}s en el origen")
    elif salto > 60_000:
        r += 15; razones.append(f"salto de {salto//1000}s")
    elif salto > 12_000:
        r += 8; razones.append(f"salto de {salto//1000}s")
    # teaser válido = el item ANTERIOR es el teaser y este regresa al desarrollo (fix r3:
    # antes se miraba el role del lado derecho y el caso típico cobraba +25 injustamente)
    if not card["transition"]["chronological"] and card.get("role_left") != "teaser":
        r += 25; razones.append("NO cronológico sin role=teaser en el item anterior")
    return min(r, 100), razones


def cut_view(edl_path, master_path, words_json, outdir=None, log_cb=print) -> Path:
    """Genera cut_view/ — la superficie con la que Ava revisa SU edición."""
    edl_path = Path(edl_path)
    edl = json.loads(edl_path.read_text(encoding="utf-8"))
    _exigir_validada(edl, "cutview")
    v = edl["validacion"]
    if v.get("master_sha256") != _sha256(master_path):
        raise RuntimeError("cutview: el master cambió después de validar la EDL — re-validá.")
    if v.get("words_sha256") != _sha256(words_json):
        raise RuntimeError("cutview: el words.json cambió después de validar — re-validá.")
    master = json.loads(Path(master_path).read_text(encoding="utf-8"))
    words = cargar_words(words_json)
    seq = edl["sequence"]
    outdir = Path(outdir) if outdir else edl_path.parent / f"cut_view_{edl.get('revision_id','r0')}"
    outdir.mkdir(parents=True, exist_ok=True)

    # ---- timeline del cut + remap de eventos (semántica de intervalos) ----
    timeline, eventos_cut = [], {}
    offset = 0
    for it in seq:
        t0, t1 = it["source_in_ms"], it["source_out_ms"]
        dur = t1 - t0
        timeline.append({"item_id": it["item_id"], "cut_in_ms": offset,
                         "cut_out_ms": offset + dur, "source_in_ms": t0,
                         "source_out_ms": t1, "role": it.get("role"),
                         "join_policy": it.get("join_policy", "hard_cut")})
        for k, evs in master["streams"].items():
            for e in evs:
                if not _solapa_ms(e, t0, t1):
                    continue
                e0 = round(float(e["t_ini"]) * 1000)
                e1 = round(float(e.get("t_fin", e["t_ini"])) * 1000)
                inst = {"source_event_id": e.get("id"), "segment_id": it["item_id"],
                        "cut_t_ini_ms": max(e0, t0) - t0 + offset,
                        "cut_t_fin_ms": min(e1, t1) - t0 + offset}
                if e0 < t0:
                    inst["clipped_left"] = True
                if e1 > t1:
                    inst["clipped_right"] = True
                # TODOS los campos escalares del evento fuente se conservan (fix r3:
                # los source_z son la autoridad — no se filtra una lista cerrada)
                for campo, valor in e.items():
                    if campo not in ("t_ini", "t_fin", "id") and \
                       not isinstance(valor, (list, dict)):
                        inst[campo] = valor
                eventos_cut.setdefault(k, []).append(inst)
        offset += dur

    # ---- junction cards ----
    cards = []
    for i in range(len(seq) - 1):
        A, B = seq[i], seq[i + 1]
        bA = _borde(words, A["source_out_ms"], "out")
        bB = _borde(words, B["source_in_ms"], "in")
        # palabras SOLO de dentro del tramo (fix r3: un tramo mudo mostraba palabras de
        # otro momento como si estuvieran en su borde)
        ult = [w for w in words if A["source_in_ms"] <= w["start_ms"]
               and w["end_ms"] <= A["source_out_ms"]][-4:]
        pri = [w for w in words if B["source_in_ms"] <= w["start_ms"]
               and w["end_ms"] <= B["source_out_ms"]][:4]
        escA = [e.get("id") for e in master["streams"].get("fondo.escenas", [])
                if _solapa_ms(e, A["source_out_ms"] - 1000, A["source_out_ms"])]
        escB = [e.get("id") for e in master["streams"].get("fondo.escenas", [])
                if _solapa_ms(e, B["source_in_ms"], B["source_in_ms"] + 1000)]
        card = {
            "schema": "junction-card/1.0",
            "junction_id": f"junc-{edl.get('revision_id','r0')}-{i+1:03d}",
            "cut_t_ms": timeline[i]["cut_out_ms"],
            "left": {
                "item_id": A["item_id"],
                "last_words": [{"wid": w["wid"], "text": w["word"].strip(),
                                "end_ms": w["end_ms"]} for w in ult],
                "terminal_punctuation": bool(ult and ult[-1]["word"].rstrip("\"')»”")
                                             .rstrip().endswith(FIN_FRASE)),
                "pause_after_ms": (A["source_out_ms"] - ult[-1]["end_ms"]) if ult else None,
                "mid_phrase": not bA["natural"],
                "clausula_abierta": bA["natural"] and "pausa" in (bA["motivo_natural"] or "")
                                    and not (ult and ult[-1]["word"].rstrip("\"')»”")
                                             .rstrip().endswith(FIN_FRASE)),
                "contexto_original": _texto_rango(master, A["source_out_ms"] - 20_000,
                                                  A["source_out_ms"] + 5_000),
            },
            "right": {
                "item_id": B["item_id"],
                "first_words": [{"wid": w["wid"], "text": w["word"].strip(),
                                 "start_ms": w["start_ms"]} for w in pri],
                "preceding_pause_ms": (pri[0]["start_ms"] - B["source_in_ms"]) if pri else None,
                "dependent_starter": bool(pri and _norm_w(pri[0]["word"])
                                          in STARTERS_DEPENDIENTES),
                "mid_phrase": not bB["natural"],
                "contexto_original": _texto_rango(master, B["source_in_ms"] - 5_000,
                                                  B["source_in_ms"] + 20_000),
            },
            "transition": {
                "source_jump_ms": B["source_in_ms"] - A["source_out_ms"],
                "chronological": B["source_in_ms"] >= A["source_out_ms"],
                "same_scene": bool(set(escA) & set(escB)) if (escA or escB) else True,
                "scene_ids": {"left": escA, "right": escB},
                "join_policy": B.get("join_policy", "hard_cut"),
                "declared_relation": B.get("declared_relation"),
            },
            "role_left": A.get("role"),
            "role_right": B.get("role"),
            "mechanical_checks": {"left_natural": bA["natural"],
                                  "left_motivo": bA["motivo_natural"],
                                  "right_natural": bB["natural"],
                                  "right_motivo": bB["motivo_natural"]},
            "semantic_review": None,     # ← lo completa AVA (riesgo_semantico 0-100 +
        }                                #   dimensiones + decision; ver skill /clipear)
        card["riesgo_base"], card["riesgo_base_razones"] = _riesgo_base(card)
        cards.append(card)

    # ---- transcript_cut.md (NUNCA prosa continua) ----
    lineas = [f"<!-- {VERSION} · EDL {edl.get('revision_id')} · master "
              f"sha256={_sha256(master_path)[:16]}… -->",
              f"# TRANSCRIPT DEL CUT — {edl_path.name}",
              "Cada [CORTE] es una junta REAL del rough cut: leé su junction card y",
              "completá semantic_review ANTES de aprobar. Un texto que fluye a través",
              "de un [CORTE] no es continuidad: es una costura que suena bien.\n"]
    for i, tl in enumerate(timeline):
        lineas.append(f"[SEGMENTO {tl['item_id']} · CUT {_fmt_t(tl['cut_in_ms'])}–"
                      f"{_fmt_t(tl['cut_out_ms'])} · SOURCE {_fmt_t(tl['source_in_ms'])}–"
                      f"{_fmt_t(tl['source_out_ms'])} · role={tl.get('role')}]")
        # texto AUDIBLE (solo palabras cuyo audio entra en el tramo), no frases de whisper
        lineas.append(_texto_audible(words, tl["source_in_ms"], tl["source_out_ms"])
                      or "⟨sin voz⟩")
        if i < len(cards):
            c = cards[i]
            signo = "+" if c["transition"]["source_jump_ms"] >= 0 else "−"
            lineas.append(f"\n[CORTE {c['junction_id']} · SALTO SOURCE {signo}"
                          f"{_fmt_t(abs(c['transition']['source_jump_ms']))} · "
                          f"riesgo_base={c['riesgo_base']} · riesgo_semantico=PENDIENTE]\n")

    # ---- cut_z: re-normalización DENTRO del cut (solo pacing; source_z = autoridad) ----
    import numpy as np
    CAMPO_FUERZA = {"voz.emocion": "arousal_z", "voz.risa": "conf", "voz.pausas": "dur_z",
                    "video.cara.reaccion": "z_max", "video.cara.emocion": "z_max",
                    "fondo.transitorios": "fuerza_z", "fondo.escenas": "energia_z"}
    for k, campo in CAMPO_FUERZA.items():
        vals = [i.get(campo) for i in eventos_cut.get(k, []) if i.get(campo) is not None]
        if len(vals) >= 3:
            med = float(np.median(vals))
            mad = float(np.median(np.abs(np.array(vals) - med))) or 0.1
            for i in eventos_cut.get(k, []):
                if i.get(campo) is not None:
                    i["cut_z"] = round((i[campo] - med) / (1.4826 * mad), 2)

    # ---- signal_summary: por segmento, qué señales tiene (resumen sin abrir eventos) ----
    summary = []
    for tl in timeline:
        s = {"item_id": tl["item_id"], "streams": {}}
        for k, insts in eventos_cut.items():
            mios = [i for i in insts if i["segment_id"] == tl["item_id"]]
            if mios:
                campo = CAMPO_FUERZA.get(k)
                s["streams"][k] = {"n": len(mios),
                                   "max": max((i.get(campo) or 0) for i in mios)
                                   if campo else None}
        summary.append(s)

    # ---- curva de intensidad (tiempo del CUT, 1s) — pacing, no autoridad ----
    total_ms = timeline[-1]["cut_out_ms"] if timeline else 0
    curva = []
    for t in range(0, total_ms, 1000):
        punto = {"t_s": t // 1000}
        for k, campo in (("voz.emocion", "arousal_z"), ("video.cara.reaccion", "z_max"),
                         ("voz.risa", "conf")):
            vals = [inst.get(campo, 0) or 0 for inst in eventos_cut.get(k, [])
                    if inst["cut_t_ini_ms"] < t + 1000 and inst["cut_t_fin_ms"] > t]
            if vals:
                punto[campo] = round(max(vals), 2)
        curva.append(punto)

    (outdir / "cut_timeline.json").write_text(
        json.dumps(timeline, ensure_ascii=False, indent=1), encoding="utf-8")
    (outdir / "junction_cards.json").write_text(
        json.dumps(cards, ensure_ascii=False, indent=1), encoding="utf-8")
    (outdir / "eventos_cut.json").write_text(
        json.dumps(eventos_cut, ensure_ascii=False), encoding="utf-8")
    (outdir / "transcript_cut.md").write_text("\n".join(lineas) + "\n", encoding="utf-8")
    (outdir / "intensity_curve.json").write_text(
        json.dumps(curva, ensure_ascii=False), encoding="utf-8")
    (outdir / "signal_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8")
    (outdir / "provenance.json").write_text(json.dumps({
        "version": VERSION, "generado": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "edl": edl_path.name, "edl_sha256": _sha256(edl_path),
        "master_sha256": _sha256(master_path), "words_sha256": _sha256(words_json),
    }, ensure_ascii=False, indent=1), encoding="utf-8")
    peor = max((c["riesgo_base"] for c in cards), default=0)
    log_cb(f"cut_view → {outdir} · {len(timeline)} segmentos · {len(cards)} junta(s) · "
           f"peor riesgo_base={peor}"
           + (" ⚠ (≥50: revisión obligatoria)" if peor >= 50 else ""))
    return outdir


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="EDL: validar bordes, render y cut_view.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    v = sub.add_parser("validar"); v.add_argument("edl")
    v.add_argument("--master", required=True); v.add_argument("--words", required=True)
    r = sub.add_parser("render"); r.add_argument("edl")
    r.add_argument("--fuente", required=True); r.add_argument("--salida", required=True)
    r.add_argument("--pistas", default="0,1")
    c = sub.add_parser("cutview"); c.add_argument("edl")
    c.add_argument("--master", required=True); c.add_argument("--words", required=True)
    c.add_argument("--outdir", default=None)
    a = ap.parse_args()
    if a.cmd == "validar":
        rep = validar(a.edl, a.master, a.words)
        sys.exit(1 if rep["errores"] else 0)
    elif a.cmd == "render":
        print("→", render(a.edl, a.fuente, a.salida, a.pistas))
    else:
        cut_view(a.edl, a.master, a.words, a.outdir)
