#!/usr/bin/env python3
"""
ava.py — detección de la PALABRA DE ACTIVACIÓN «Ava» en el transcript: instrucciones
habladas para la IA que corta los clips, dichas DURANTE la grabación.

La idea (del usuario): mientras graba, puede decir "hey Ava", "Ava, escuchame", "Ava,
acá quiero que cortes…" y eso queda en el words.json como cualquier otra palabra. Este
módulo escanea la timeline entera y emite UN EVENTO por invocación → stream
`voz.instrucciones` en el master. El orquestador (la IA que clipea) los trata como
PROMPTS del autor: máxima prioridad editorial, y el tramo se EXCLUYE de los clips
(está hablándole a la IA, no a la audiencia).

Filosofía (la misma de extraer_pausas.py — heurística, no interpretación):

  · EL EVENTO ES EL TIMESTAMP. Determinar dónde "termina" una instrucción hablada de
    verdad es un problema de lenguaje (lo resuelve el orquestador leyendo el transcript
    alrededor de t_ini). Acá solo damos un fin BARATO y honesto: el primer silencio
    largo (>= FIN_GAP) después de la palabra de activación, con tope MAX_DUR. El texto
    capturado viaja en el evento como conveniencia, no como verdad.

  · SALE GRATIS DE LO QUE YA TENEMOS: puro words.json realineado con MMS (~50 ms).
    Sin audio, sin modelos, sin deps. Corre en milisegundos sobre horas de video.

  · NO FILTRAMOS FALSOS POSITIVOS, LOS DESCRIBIMOS. Si el video menciona a una persona
    llamada Eva, el match sale igual pero con sus HECHOS: ¿hubo pausa antes? ¿qué
    palabra venía antes? ¿qué prob le dio whisper? El orquestador (que ve el contexto
    completo) decide si es una invocación o una mención. Filtrar acá con reglas sería
    interpretación frágil.

  · VARIANTES: en español «Ava» y «Eva» suenan casi igual (/ˈaβa/ vs /ˈeβa/) y whisper
    además escribe b/v como quiere → se matchea {ava, eva, aba, eba} normalizado
    (minúsculas, sin acentos, sin puntuación). "hey"/"oye" NO forman parte del match
    (son palabras separadas en el words.json); quedan visibles en `palabra_previa`.

Salida: {"events": [...], "n": N, "params": {...}}, clave "instrucciones" del
<nombre>.metadata.json (pestaña Metadata) → `voz.instrucciones` en el master.
Uso CLI:  python ava.py archivo.words.json
"""
from __future__ import annotations

import json
import unicodedata
from pathlib import Path

# Formas en que whisper puede escribir la palabra de activación (ver docstring).
VARIANTES = {"ava", "eva", "aba", "eba"}
FIN_GAP = 1.2       # s — un silencio >= esto después de la invocación cierra la instrucción
MAX_DUR = 20.0      # s — tope duro del span (una instrucción no debería ser un monólogo)
REPETICION = 2.0    # s — una 2ª invocación a <= esto de la 1ª es énfasis ("Ava, Ava…") y se
                    # absorbe; más lejos es una invocación NUEVA (cierra el span anterior)
PAUSA_PREVIA = 0.4  # s — hueco antes de la palabra que registramos como "pausa_previa"
N_CONTEXTO = 5      # palabras previas que se guardan como contexto legible


def _norm(word: str) -> str:
    """minúsculas + sin acentos + solo letras (quita ¡!¿?,. etc.)."""
    s = unicodedata.normalize("NFD", (word or "").lower())
    return "".join(c for c in s if c.isalpha() and not unicodedata.combining(c))


def load_words(words_json) -> list[dict]:
    words = json.loads(Path(words_json).read_text(encoding="utf-8"))
    if not words:
        raise RuntimeError("El words.json está vacío.")
    return words


def extract(words, *, fin_gap=FIN_GAP, max_dur=MAX_DUR, log_cb=None) -> dict:
    """Escanea el words.json entero buscando la palabra de activación. Un evento por
    invocación:
      · t_ini            — inicio de la palabra «Ava» (el timestamp que importa, ~50 ms)
      · t_fin / dur      — fin heurístico: 1er silencio >= fin_gap tras la invocación
                           (tope max_dur), con `fin_causa`: "silencio" | "tope" |
                           "nueva_invocacion" | "fin_audio"
      · palabra / prob   — la forma exacta que escribió whisper y su confianza
      · texto            — las palabras capturadas DESPUÉS de la invocación (conveniencia;
                           la verdad es el transcript en t_ini)
      · pausa_previa     — s de silencio antes de la invocación (0 si venía hablando;
                           una invocación real suele arrancar tras una pausa)
      · palabra_previa / contexto_previo — qué venía antes ("hey", o mitad de frase)
    Si una invocación cae DENTRO del span de la anterior ("Ava, Ava, escuchame") se
    absorbe en la primera (no genera evento propio)."""
    def log(m):
        if log_cb:
            log_cb(m)

    # saneo defensivo (fix review Codex): el aligner puede dejar palabras con end < start
    # → se normaliza end=max(start,end) en COPIAS (no se muta la entrada).
    ws = sorted(({**w, "start": float(w["start"]),
                  "end": max(float(w["start"]), float(w["end"]))} for w in words),
                key=lambda w: w["start"])
    events = []
    fin_prev = -1.0                      # t_fin del último evento (absorber repeticiones)
    for i, w in enumerate(ws):
        if _norm(w["word"]) not in VARIANTES:
            continue
        if w["start"] < fin_prev:        # "Ava, Ava…" → ya está dentro de la anterior
            continue
        # --- span de la instrucción: hasta el 1er silencio largo, con tope. Una NUEVA
        # invocación lejana también lo cierra (solo "Ava, Ava…" inmediato se absorbe) ---
        t_fin, fin_causa = w["end"], "fin_audio"
        texto, cursor = [], w["end"]
        for nx in ws[i + 1:]:
            gap = nx["start"] - cursor
            if gap >= fin_gap:
                fin_causa = "silencio"
                break
            if (_norm(nx["word"]) in VARIANTES
                    and nx["start"] - w["start"] > REPETICION):
                fin_causa = "nueva_invocacion"
                # cerrar ANTES de la nueva invocación: con palabras solapadas t_fin podía
                # quedar >= nx.start y el loop exterior descartaba la nueva Ava (se perdía
                # una orden — fix review Codex ronda 2)
                t_fin = min(t_fin, nx["start"])
                break
            if nx["end"] - w["start"] > max_dur:
                fin_causa = "tope"
                break
            texto.append(nx["word"])
            cursor = max(cursor, nx["end"])   # máximo histórico: una palabra solapada más
            t_fin = cursor                    # corta NO retrocede t_fin (fix review Codex)
        if t_fin - w["start"] > max_dur:      # tope DURO aunque la propia palabra de
            t_fin, fin_causa = w["start"] + max_dur, "tope"   # activación venga anómala
        # --- hechos de contexto (el orquestador decide con esto) ---
        prev = ws[i - 1] if i > 0 else None
        pausa = round(w["start"] - prev["end"], 3) if prev else round(w["start"], 3)
        p = w.get("prob")                 # prob faltante = None (un hecho ausente no se
        # inventa como confianza perfecta — fix review Codex)
        events.append({
            "t_ini": round(w["start"], 3), "t_fin": round(t_fin, 3),
            "dur": round(t_fin - w["start"], 3), "tipo": "instruccion",
            "palabra": w["word"].strip(),
            "prob": round(float(p), 3) if isinstance(p, (int, float)) else None,
            "pausa_previa": max(pausa, 0.0),
            "palabra_previa": prev["word"] if prev else None,
            "contexto_previo": " ".join(x["word"] for x in ws[max(0, i - N_CONTEXTO):i]).strip() or None,
            "texto": " ".join(texto).strip(),
            "fin_causa": fin_causa,
        })
        fin_prev = t_fin
    log(f"Instrucciones a Ava: {len(events)} invocación(es) encontrada(s)."
        + ("" if not events else " " + " · ".join(f"[{e['t_ini']:.1f}s]" for e in events[:8])))
    return {"events": events, "n": len(events),
            "params": {"variantes": sorted(VARIANTES), "fin_gap": fin_gap,
                       "max_dur": max_dur, "pausa_previa_min": PAUSA_PREVIA}}


def default_output(words_json) -> Path:
    p = Path(words_json)
    stem = p.name[:-len(".words.json")] if p.name.endswith(".words.json") else p.stem
    return p.with_name(stem + ".instrucciones.json")


def run(words_json, out_json=None, *, fin_gap=FIN_GAP, max_dur=MAX_DUR, log_cb=None) -> dict:
    """Lee un words.json, detecta invocaciones y escribe <nombre>.instrucciones.json."""
    result = extract(load_words(words_json), fin_gap=fin_gap, max_dur=max_dur, log_cb=log_cb)
    out = Path(out_json) if out_json else default_output(words_json)
    out.write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
    if log_cb:
        log_cb(f"Escrito: {out.name}")
    return result


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="Detecta instrucciones habladas a «Ava» en un words.json.")
    ap.add_argument("words_json", help="el <nombre>.words.json (alineado) a escanear")
    ap.add_argument("--out", default=None, help="salida (default: <nombre>.instrucciones.json)")
    ap.add_argument("--fin-gap", type=float, default=FIN_GAP,
                    help=f"silencio que cierra la instrucción, s (default {FIN_GAP})")
    a = ap.parse_args()
    r = run(a.words_json, a.out, fin_gap=a.fin_gap, log_cb=print)
    print(json.dumps(r["events"], ensure_ascii=False, indent=1))
