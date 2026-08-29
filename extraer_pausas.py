#!/usr/bin/env python3
"""
extraer_pausas.py — Extrae las PAUSAS del habla como un stream de hechos crudos.

Filosofía (a propósito, para que sea robusto y no se pudra en 6 meses):

  · SÓLO MEDIMOS, NO INTERPRETAMOS. No hay clasificador de "función" de la pausa
    (duda / atencional / pre-reacción / boundary). Esa etiqueta es una interpretación
    que depende del formato (guion vs gameplay) y de umbrales calibrados a mano → se
    contamina cada vez que cambia el contenido. En vez de eso, exponemos los hechos
    y dejamos que el MODELO ORQUESTADOR (que ve el transcript completo + los otros
    streams: arousal, risa, y a futuro el AST de la pista del juego, todos en la MISMA
    línea de tiempo) decida qué significa cada silencio. La pausa sola no significa
    nada; la pausa cruzada con lo que la rodea lo significa todo → ese cruce lo hace
    el orquestador, no una heurística frágil acá.

  · SALE GRATIS DE LO QUE YA TENEMOS. Una pausa es un hueco entre dos palabras
    consecutivas del words.json ya realineado con MMS (~50 ms de precisión). No hace
    falta abrir el audio ni correr ningún modelo: reusamos la joya de la corona (los
    timestamps precisos) y la misma lógica de huecos de gate.py.

  · FORMATO DE EVENTOS CONSISTENTE con el resto del pipeline ({t_ini, t_fin, dur,
    tipo, ...}) → mergear por tiempo al futuro master.json es trivial.

Salida: {"events": [...], "baseline": {...}, "n": N}, escrita como <nombre>.pausas.json.
Uso CLI:  python extraer_pausas.py archivo.words.json [--min-dur 0.3]
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np

# Caracteres con que puede TERMINAR una palabra cuando whisper cerró una frase.
# Es un HECHO ortográfico (no una interpretación): sólo registramos si el texto
# venía puntuado. La puntuación de whisper es señal débil en habla espontánea, así
# que la guardamos como dato y que el orquestador le ponga el peso que quiera.
_FIN_FRASE = (".", "!", "?", "…")


def load_words(words_json) -> list[dict]:
    words = json.loads(Path(words_json).read_text(encoding="utf-8"))
    if not words:
        raise RuntimeError("El words.json está vacío.")
    return words


def _termina_frase(word: str) -> bool:
    """¿La palabra previa cerró una frase? (quita comillas/paréntesis de cola)."""
    w = (word or "").rstrip('"\')»”').rstrip()
    return w.endswith(_FIN_FRASE)


def extract(words, *, min_dur=0.3, log_cb=None) -> dict:
    """Detecta las pausas (huecos entre palabras >= min_dur) sobre un words.json ya
    alineado. Devuelve {"events", "baseline", "n"}.

    Cada evento es SÓLO medición:
      · t_ini/t_fin/dur  — el hueco en sí (borde de una palabra al inicio de la siguiente)
      · palabra_previa/palabra_sig — las palabras que enmarcan el silencio (contexto legible)
      · fin_frase_previa — si la palabra previa venía con . ? ! … (whisper creyó cerrar frase)
      · dur_z — cuán larga es ESTA pausa relativa a las pausas típicas del MISMO video
                (una pausa de 2 s pesa distinto en alguien que habla rápido vs lento)
    """
    def log(m):
        if log_cb:
            log_cb(m)

    ws = sorted(words, key=lambda w: w["start"])
    events = []

    # Pausa inicial: aire antes de la primera palabra (dead air al arrancar).
    if ws and ws[0]["start"] >= min_dur:
        events.append({
            "t_ini": 0.0, "t_fin": round(ws[0]["start"], 3),
            "dur": round(ws[0]["start"], 3), "tipo": "pausa",
            "palabra_previa": None, "palabra_sig": ws[0]["word"],
            "fin_frase_previa": False,
        })

    # Huecos entre palabras. Cursor = máximo end visto (robusto ante solapes raros
    # del aligner, igual que gate._find_all_gaps).
    cursor = ws[0]["end"] if ws else 0.0
    prev = ws[0] if ws else None
    for w in ws[1:]:
        gap = w["start"] - cursor
        if gap >= min_dur:
            events.append({
                "t_ini": round(cursor, 3), "t_fin": round(w["start"], 3),
                "dur": round(gap, 3), "tipo": "pausa",
                "palabra_previa": prev["word"], "palabra_sig": w["word"],
                "fin_frase_previa": _termina_frase(prev["word"]),
            })
        if w["end"] > cursor:
            cursor = w["end"]
            prev = w

    # z-score de la duración contra el baseline de pausas del propio video (picos
    # relativos a vos, igual criterio que metadata.py con arousal/f0/wps).
    durs = np.array([e["dur"] for e in events], dtype=float)
    if len(durs):
        mu = float(durs.mean()); sd = float(durs.std()) or 1.0
        for e in events:
            e["dur_z"] = round((e["dur"] - mu) / sd, 2)
    else:
        mu, sd = 0.0, 0.0
    baseline = {"dur": {"mean": round(mu, 3), "std": round(sd, 3), "min_dur": min_dur}}

    log(f"Pausas (>= {min_dur:.2f}s): {len(events)} encontradas · "
        f"dur media {mu:.2f}s.")
    return {"events": events, "baseline": baseline, "n": len(events)}


def default_output(words_json) -> Path:
    p = Path(words_json)
    stem = p.name[:-len(".words.json")] if p.name.endswith(".words.json") else p.stem
    return p.with_name(stem + ".pausas.json")


def run(words_json, out_json=None, *, min_dur=0.3, log_cb=None) -> dict:
    """Lee un words.json, extrae las pausas y escribe <nombre>.pausas.json."""
    result = extract(load_words(words_json), min_dur=min_dur, log_cb=log_cb)
    out = Path(out_json) if out_json else default_output(words_json)
    out.write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
    if log_cb:
        log_cb(f"Escrito: {out.name}")
    return result


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="Extrae pausas de un words.json alineado.")
    ap.add_argument("words_json", help="ruta al <nombre>.words.json")
    ap.add_argument("-o", "--out", default=None, help="salida (default <nombre>.pausas.json)")
    ap.add_argument("--min-dur", type=float, default=0.3,
                    help="pausa mínima en segundos (default 0.3)")
    a = ap.parse_args()
    run(a.words_json, a.out, min_dur=a.min_dur, log_cb=print)
