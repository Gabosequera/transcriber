#!/usr/bin/env python3
"""
fondo.py — Pipeline completo para la pista de AUDIO DE FONDO: el juego, o el video que se
mira, o lo que sea que NO es la voz del creador. Encadena todo lo construido, alineado a UNA
sola línea de tiempo, para el master.json que ve la IA que corta clips.

  Capa 0  — Whisper (VAD + aligner MMS) sobre la pista del juego, con ANTI-ALUCINACIÓN
            (filtro de confianza + detector de loops), idioma AUTO, marcado fuente:"juego".
            Modelo elegible (default large-v3-turbo). Reutiliza `core.transcribe` tal cual.
  Capa 1  — AST → tags de sonido (18 familias).            \
  Capa 1.5— onsets → transitorios (golpes secos, ~10 ms).   > `escena_audio.analizar`
  Capa 2  — segmentación por escena.                        /
  Capa 3  — LALM (Qwen-Omni vía llama.cpp) → descripción por chunk (escena + solape + contexto).

Cada capa es OPCIONAL (los checkboxes de la GUI la encienden). Salida: <fondo>.fondo.json.

Principio de timestamps (lo más importante): TODO sale con tiempos que ponemos NOSOTROS
—Whisper alineado con MMS, onsets ~10 ms, y la descripción atada a la ventana que le mandamos
al modelo— nunca a lo que el modelo diga de su propio tiempo.
"""
from __future__ import annotations

import json
from pathlib import Path


def _anti_alucinacion(segments, *, min_prob=0.4, max_repeticiones=2) -> list[dict]:
    """Limpia la salida de Whisper sobre audio con poco/nada de habla (una pista de juego es
    80-95% no-voz → Whisper alucina texto fantasma y loops). Dos filtros:
      · confianza: descarta segmentos cuya prob media de palabra < `min_prob`.
      · loops: si el MISMO texto aparece repetido, descarta a partir de la 3ª vez seguida.
    Devuelve el stream de diálogo del juego: {t_ini,t_fin,texto,conf} (la fuente 'juego' va
    una sola vez en el header del .fondo.json, no repetida por item)."""
    out, prev, rep = [], None, 0
    for s in segments:
        texto = (s.get("text") or "").strip()
        words = s.get("words") or []
        conf = (sum(w.get("prob", 0.0) for w in words) / len(words)) if words else 0.0
        if texto and texto == prev:
            rep += 1
            if rep >= max_repeticiones:      # 3ª repetición idéntica → alucinación
                continue
        else:
            rep, prev = 0, texto
        if not texto or conf < min_prob:
            continue
        out.append({"t_ini": round(float(s["start"]), 2), "t_fin": round(float(s["end"]), 2),
                    "texto": texto, "conf": round(conf, 2)})
    return out


def transcribir_juego(audio, *, model_name="large-v3-turbo", device="auto", outdir=None,
                      min_prob=0.4, cancel=None, log_cb=None, progress_cb=None) -> list[dict]:
    """Capa 0. Corre Whisper (VAD + MMS) sobre la pista del juego, idioma AUTO, y aplica
    anti-alucinación. Reutiliza `core.transcribe` (no duplica el pipeline de la voz).
    Escribe words.json/segments.json en `outdir` (o junto al audio si es None)."""
    import core
    audio = Path(audio)
    outdir = Path(outdir) if outdir else audio.parent
    res = core.transcribe(audio, outdir, model_name=model_name, lang=None, device=device,
                          want_segments=True, want_srt=False, want_cues=False, want_align=True,
                          cancel=cancel, log_cb=log_cb, progress_cb=progress_cb)
    if not res:                        # None = cancelado, o sin voz
        return []
    segs = json.loads(Path(res["written"]["segments.json"]).read_text(encoding="utf-8"))
    dialogo = _anti_alucinacion(segs, min_prob=min_prob)
    if log_cb:
        log_cb(f"Diálogo del juego: {len(dialogo)} segmento(s) tras anti-alucinación (de {len(segs)}).")
    return dialogo


def _sin(lst, *drop) -> list[dict]:
    """Copia la lista de eventos quitando claves redundantes (el nombre del stream y el
    header ya dicen el tipo/fuente/modelo → no hace falta repetirlos por item)."""
    return [{k: v for k, v in e.items() if k not in drop} for e in (lst or [])]


def analizar_fondo(audio, *, do_dialogo=True, whisper_model="large-v3-turbo",
                   do_sonidos=True, do_descripcion=True, modelo_desc="qwen2.5-omni-3b",
                   ngl_desc=None, device="auto", lalm_backend="local",
                   lalm_online=None, lalm_api_key=None, outdir=None, cancel=None,
                   log_cb=None, progress_cb=None, capas=None) -> dict:
    """Corre las capas encendidas sobre la pista de fondo y escribe <fondo>.fondo.json con una
    estructura LIMPIA: un header (audio + modelos usados) y un stream por capa, sin claves
    repetidas por item. Streams: dialogo (Whisper del juego), sonidos (tags AST), transitorios
    (golpes secos), escenas (segmentación), descripciones (relato del LALM). Todo en la misma
    línea de tiempo (t_ini/t_fin que ponemos nosotros). Todas las salidas van a `outdir` (o junto
    al audio si es None).

    `capas` (opcional): dict que el CALLER pasa y esta función LLENA con el resultado
    EXPLÍCITO por capa — {"dialogo"|"sonidos"|"descripcion": {"status": "ok|failed|
    skipped|cancelled", "reason": str, "requested": bool, "n": int}}. Es el contrato del
    orquestador (pipeline.py): los estados se DECLARAN acá, nunca se infieren por claves
    presentes en el JSON (una capa vacía puede ser un éxito legítimo)."""
    def log(m):
        if log_cb:
            log_cb(m)

    def _capa(nombre, status, reason="", n=None):
        if capas is not None:
            r = {"status": status, "reason": reason, "requested": True}
            if n is not None:
                r["n"] = n
            capas[nombre] = r

    def cancelado():
        return cancel is not None and cancel.is_set()

    audio = Path(audio)
    out_dir = Path(outdir) if outdir else audio.parent
    out_dir.mkdir(parents=True, exist_ok=True)
    outp = out_dir / (audio.stem + ".fondo.json")
    # Arrancar del .fondo.json previo (si existe) → NO pisar lo ya generado; cada capa actualiza
    # solo su parte, y podemos REUSAR escenas ya calculadas para la descripción.
    out = {"audio": audio.name, "tipo": "audio_de_fondo", "modelos": {}}
    if outp.exists():
        try:
            prev = json.loads(outp.read_text(encoding="utf-8"))
            if isinstance(prev, dict):
                out = prev
                out["audio"], out["tipo"] = audio.name, "audio_de_fondo"
                out.setdefault("modelos", {})
        except Exception:
            pass

    def _publicar(clave, nuevo, completo=True) -> bool:
        """Escribe un stream en `out` sin DEGRADAR lo ya generado: un resultado COMPLETO
        siempre reemplaza; uno incompleto (la corrida se canceló a mitad) solo entra si
        aporta MÁS datos que lo que ya había. Devuelve True si se escribió."""
        prev = out.get(clave)
        if not completo:
            if prev and len(nuevo) <= len(prev):
                log(f"⏹ «{clave}» quedó incompleto ({len(nuevo)} item(s)) — se conserva lo "
                    f"generado antes ({len(prev)} item(s)).")
                return False
            if not nuevo and prev is None:         # cancelado sin producir nada y sin previo:
                return False                       # no inventar un stream vacío "procesado"
        out[clave] = nuevo
        return True

    def _guardar():
        outp.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")

    # Cada capa se corre en su PROPIO try/except: si una falla o no detecta nada (p.ej. Whisper
    # sin voz en la pista), se LOGUEA el aviso, se SALTEA y se sigue con la siguiente — el pipeline
    # nunca se frena en seco. La cancelación sí corta (los `not cancelado()` de abajo saltean el
    # resto), y el .fondo.json se persiste al TERMINAR cada capa (crash-safe: un crash o un corte
    # en una capa posterior no pierde lo ya calculado en esta corrida ni en las anteriores).
    if do_dialogo and not cancelado():
        log("── Capa 0: Whisper sobre la pista del juego ──")
        try:
            dialogo = transcribir_juego(audio, model_name=whisper_model, device=device,
                                        outdir=out_dir, cancel=cancel,
                                        log_cb=log, progress_cb=progress_cb)
            # cancelado a mitad → transcribir_juego devuelve [] → NO pisar el stream previo
            if _publicar("dialogo", dialogo, completo=not cancelado()):
                out["modelos"]["whisper"] = whisper_model
            _guardar()
            if cancelado():
                _capa("dialogo", "cancelled", "cancelado a mitad")
            else:
                _capa("dialogo", "ok", n=len(dialogo))
        except Exception as e:                     # p.ej. "No se detectó voz" → NO frenar todo
            if cancelado():
                log("⏹ Cancelado durante el diálogo.")
                _capa("dialogo", "cancelled", str(e)[:200])
            else:
                out.setdefault("dialogo", [])
                log(f"⚠ Sin diálogo utilizable en la pista (¿poca o ninguna voz?) — se saltea y sigue. ({e})")
                # "No se detectó voz" = resultado legítimo (una pista de juego puede no tener
                # habla) → ok con n=0; cualquier OTRA excepción = failure real declarado.
                if "no se detectó voz" in str(e).lower():
                    _capa("dialogo", "ok", "la pista no tiene voz", n=0)
                else:
                    _capa("dialogo", "failed", str(e)[:200])

    escena = None
    if do_sonidos and not cancelado():             # el AST solo se corre si pediste sonidos
        log("── Capas 1/1.5/2: sonidos + transitorios + escenas ──")
        try:
            import escena_audio
            res = escena_audio.analizar(str(audio), cancel=cancel, log_cb=log, progress_cb=progress_cb)
            # cancelado a mitad → analizar devuelve listas VACÍAS → NO pisar los streams previos
            completo = not cancelado()
            _publicar("sonidos", _sin(res["events"], "tipo", "transitoria"), completo)
            _publicar("transitorios", _sin(res["transitorios"], "tipo"), completo)
            _publicar("escenas", _sin(res["segments"], "tipo"), completo)
            escena = res if completo else None
            _guardar()
            if completo:
                _capa("sonidos", "ok", n=len(res["events"]))
            else:
                _capa("sonidos", "cancelled", "cancelado a mitad")
        except Exception as e:
            if cancelado():
                log("⏹ Cancelado durante el análisis de sonidos.")
                _capa("sonidos", "cancelled", str(e)[:200])
            else:
                log(f"⚠ No se pudieron analizar sonidos/escenas — se saltea y sigue. ({e})")
                _capa("sonidos", "failed", str(e)[:200])

    if do_descripcion and not cancelado():
        # La descripción es POR ESCENA. Usa las escenas recién calculadas, o las que YA estén en el
        # .fondo.json previo (no regenera). Si no hay ninguna, NO inventa ventanas: avisa y saltea.
        segs = escena["segments"] if escena else out.get("escenas")
        if not segs:
            log("⚠ La descripción es POR ESCENA y no hay escenas generadas. Marcá 'Sonidos + "
                "transitorios + escenas' para generarlas (o generalas antes). Salteo la descripción.")
            _capa("descripcion", "skipped", "no hay escenas generadas (requiere la capa de sonidos)")
        else:
            fuente = "recién calculadas" if escena else "reusadas del .fondo.json existente"
            log(f"── Capa 3: descripción por escena · {len(segs)} escena(s) ({fuente}) ──")
            try:
                import describir
                desc = describir.describir(str(audio), segs, modelo=modelo_desc, n_gpu_layers=ngl_desc,
                                           backend=lalm_backend, modelo_online=lalm_online,
                                           api_key=lalm_api_key,
                                           cancel=cancel, log_cb=log, progress_cb=progress_cb)
                # cancelado a mitad → describir devuelve lo PARCIAL → solo entra si supera lo previo
                if _publicar("descripciones", _sin(desc, "tipo", "modelo"), completo=not cancelado()):
                    out["modelos"]["descripcion"] = (f"online:{lalm_online or describir.ONLINE_DEFAULT}"
                                                     if lalm_backend == "online" else modelo_desc)
                _guardar()
                if cancelado():
                    _capa("descripcion", "cancelled", "cancelado a mitad")
                else:
                    _capa("descripcion", "ok", n=len(desc))
            except Exception as e:                 # falla del LALM (o cancelación) → saltear, no frenar
                if cancelado():
                    log("⏹ Cancelado durante la descripción.")
                    _capa("descripcion", "cancelled", str(e)[:200])
                else:
                    log(f"⚠ No se pudo generar la descripción — se saltea. ({e})")
                    _capa("descripcion", "failed", str(e)[:200])

    if cancelado():
        log("⏹ Cancelado — se guarda lo procesado; los streams que quedaron incompletos "
            "conservan lo que ya estaba generado antes.")
    _guardar()
    log(f"Listo → {outp}")
    return out


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="Pipeline de la pista de audio de fondo (juego).")
    ap.add_argument("audio")
    ap.add_argument("--whisper-model", default="large-v3-turbo")
    ap.add_argument("--modelo-desc", default="qwen2.5-omni-3b")
    ap.add_argument("--device", default="auto")
    ap.add_argument("--outdir", default=None, help="carpeta de salida (default: junto al audio)")
    ap.add_argument("--no-dialogo", action="store_true")
    ap.add_argument("--no-sonidos", action="store_true")
    ap.add_argument("--no-descripcion", action="store_true")
    a = ap.parse_args()
    r = analizar_fondo(a.audio, do_dialogo=not a.no_dialogo, whisper_model=a.whisper_model,
                       do_sonidos=not a.no_sonidos, do_descripcion=not a.no_descripcion,
                       modelo_desc=a.modelo_desc, device=a.device, outdir=a.outdir, log_cb=print)
    print("\nResumen:",
          {k: (len(v) if isinstance(v, list) else v) for k, v in r.items()})
