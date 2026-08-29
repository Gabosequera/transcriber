#!/usr/bin/env python3
"""
consolidar.py — Fusiona TODA la metadata de un video en UN master.json normalizado,
alineado a una sola línea de tiempo. Es la fuente de verdad que ve la IA que corta clips.

Principio (el mismo de todo el proyecto): guardamos HECHOS normalizados, no una vista. Todos
los eventos de todas las modalidades, cada uno {t_ini,t_fin,...}, agrupados por stream con
nombre `<modalidad>.<stream>`. ESCALABLE: agregar una modalidad nueva (video: cara, pantalla)
= agregar claves `video.*`, sin rediseñar nada. La vista legible "de un vistazo por momento"
(timeline.md) se genera aparte desde este archivo (raw vs vista).

Entrada (hermanos del audio/video, cualquiera puede faltar):
  <base>.metadata.json  → tu voz: emotion / laughter / pausas / instrucciones (a Ava)
  <base>.fondo.json     → el juego: dialogo / sonidos / transitorios / escenas / descripciones
  <base>.segments.json  → transcript de tu voz a nivel frase
  <base>.cara.json      → la facecam: reaccion / mirada / presencia (esquema cara-v1; los
                          sidecars .cara.detalle.json y .facecam.json NO se fusionan — el
                          detalle es drill-down y el facecam es producción/9:16)
Salida: <base>.master.json
"""
from __future__ import annotations

import hashlib
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

SCHEMA_VERSION = 2          # v2 (2026-07-16): header global + IDs estables por evento
GENERADOR = {"nombre": "consolidar.py", "version": "2.3"}   # 2.3: + fuentes.media (identidad
#                                        del medio para la tab «Marcar» — diseño 2026-07-21);
#                                        2.2: + visión VLM; 2.1: marcas

# Prefijos de los IDs estables por stream (S0441 = frase 441 del transcript, etc.).
# Deterministas: orden cronológico dentro del stream. Los usan las vistas (vistas.py),
# el índice de señales, las junction cards y el candidate registry.
PREFIJOS = {
    "voz.transcript": "S", "voz.emocion": "E", "voz.risa": "R", "voz.pausas": "P",
    "voz.instrucciones": "AVA",
    "fondo.dialogo": "FD", "fondo.sonidos": "FS", "fondo.transitorios": "FT",
    "fondo.escenas": "FE", "fondo.descripciones": "FX",
    "video.cara.reaccion": "CR", "video.cara.mirada": "CM",
    "video.cara.presencia": "CP", "video.cara.emocion": "CE",
}

# Streams cuyos IDs vienen DERIVADOS de una identidad canónica externa y NO se
# renumeran (autor.marcas: m0007 → MK0007 aunque se agregue una marca anterior —
# renumerar cronológicamente rompería la estabilidad prometida; consenso r1 h.5.
# Visión: vj0007 → VJ0007, ídem — consenso vision r1.7).
IDS_EXTERNOS = {"autor.marcas", "video.juego.vlm", "video.camara.vlm"}


def _load(p) -> dict | list | None:
    p = Path(p)
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None


def _sha256(p) -> str | None:
    try:
        return hashlib.sha256(Path(p).read_bytes()).hexdigest()
    except Exception:
        return None


def _ffprobe_dur_raw(media) -> float | None:
    """Duración REAL del archivo de medios SIN redondear (la validación de marcas
    compara contra ESTA: redondear a 2 decimales ponía en cuarentena una marca légitima
    en t=dur — p.ej. 17.924 > 17.92). CREATE_NO_WINDOW: sin él, cada ffprobe FLASHEA
    una consola cuando la app corre como GUI en Windows (auditoría pre-ship)."""
    import os
    kw = {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)} if os.name == "nt" else {}
    try:
        r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                            "-of", "csv=p=0", str(media)],
                           capture_output=True, text=True, timeout=30, **kw)
        return float(r.stdout.strip())
    except Exception:
        return None


def _ffprobe_dur(media) -> float | None:
    """Duración redondeada (para el header; un video puede terminar con minutos sin
    eventos — el mayor t_fin NO es la duración)."""
    d = _ffprobe_dur_raw(media)
    return round(d, 2) if d is not None else None


def _norm_transcript(segs) -> list[dict]:
    """segments.json → stream de frases {t_ini,t_fin,texto}."""
    return [{"t_ini": round(float(s["start"]), 2), "t_fin": round(float(s["end"]), 2),
             "texto": (s.get("text") or "").strip()} for s in (segs or [])]


def _dur(streams) -> float:
    """Duración = el mayor t_fin visto en cualquier stream."""
    m = 0.0
    for evs in streams.values():
        for e in evs:
            m = max(m, float(e.get("t_fin", e.get("t_ini", 0.0))))
    return round(m, 2)


def consolidar(salida, *, voz=None, fondo=None, cara=None, media=None, layout=None,
               marcas=None, vision_juego=None, vision_camara=None, log_cb=None) -> dict:
    """Junta la metadata de VOZ, FONDO y CARA (archivos con nombres distintos) en un master.json.
      · voz    → ruta al audio de tu voz: busca <voz>.metadata.json + <voz>.segments.json.
      · fondo  → ruta al audio del juego: busca <fondo>.fondo.json.
      · cara   → ruta al VIDEO analizado: busca <cara>.cara.json (modalidad de video).
      · salida → ruta/base del proyecto: escribe <salida>.master.json.
      · layout → (opcional) extensión DELIBERADA del schema v2 (consenso wizard 2026-07-16):
                 header.media_layout = {rects (juego/cámara, normalizados), pistas (roles →
                 índice de stream), timeline (t0 + offsets aplicados por pista)}. Lo escribe
                 el pipeline del wizard; los consumidores viejos lo ignoran sin romperse.
      · marcas → (opcional) ruta a la SNAPSHOT del sidecar de marcas del autor (el
                 pipeline la congela al inicio del run — marcas.py) → stream
                 `autor.marcas` + modalidad `autor` (con conflictos incluir∩excluir
                 declarados; regla: excluir gana). IDs derivados MK#### SIN renumerar.
    Si voz/fondo/cara se omiten, se usa `salida` como base única (modo un-solo-archivo)."""
    def log(m):
        if log_cb:
            log_cb(m)

    def _stem(p):
        p = Path(p)
        return p.parent / p.stem                 # <dir>/<nombre-sin-extensión>

    salida = Path(salida)
    vz = _stem(voz) if voz else _stem(salida)
    fd = _stem(fondo) if fondo else _stem(salida)
    cr = _stem(cara) if cara else _stem(salida)
    medios = [p for p in (cara, voz, fondo) if p]   # rutas de MEDIOS (antes de reasignar)
    meta = _load(f"{vz}.metadata.json")          # tu voz
    segs = _load(f"{vz}.segments.json")          # transcript (frases)
    fondo = _load(f"{fd}.fondo.json")            # el juego
    cara_j = _load(f"{cr}.cara.json")            # la facecam (esquema cara-v1)

    streams: dict[str, list] = {}
    modalidades: dict[str, dict] = {}

    # ---- modalidad VOZ (tu micrófono) --------------------------------------
    voz_streams = {}
    if segs:
        voz_streams["voz.transcript"] = _norm_transcript(segs)
    if meta:
        if meta.get("emotion"):
            voz_streams["voz.emocion"] = meta["emotion"].get("events", [])
        if meta.get("laughter"):
            voz_streams["voz.risa"] = meta["laughter"]
        if meta.get("pausas"):
            voz_streams["voz.pausas"] = meta["pausas"].get("events", [])
        if meta.get("instrucciones", {}).get("events"):
            voz_streams["voz.instrucciones"] = meta["instrucciones"]["events"]
    if voz_streams:
        streams.update(voz_streams)
        modalidades["voz"] = {"presente": True, "streams": sorted(voz_streams)}
    if meta and "instrucciones" in meta:
        # trazabilidad: distinguir "detector corrido sin invocaciones" (n=0, sin stream)
        # de "detector no corrido" (esta clave ausente)
        modalidades.setdefault("voz", {"presente": bool(voz_streams)})["instrucciones"] = {
            "corrido": True, "n": meta["instrucciones"].get("n", 0),
            "params": meta["instrucciones"].get("params", {})}

    # ---- modalidad FONDO (el juego / lo que suena) -------------------------
    fondo_streams = {}
    if fondo:
        for k in ("dialogo", "sonidos", "transitorios", "escenas", "descripciones"):
            if k in fondo:
                fondo_streams[f"fondo.{k}"] = fondo[k]
    if fondo_streams:
        streams.update(fondo_streams)
        modalidades["fondo"] = {"presente": True, "streams": sorted(fondo_streams),
                                "modelos": (fondo or {}).get("modelos", {})}

    # ---- modalidad VIDEO: la CARA (facecam) --------------------------------
    cara_streams = {}
    if cara_j and isinstance(cara_j.get("streams"), dict):
        for k, evs in cara_j["streams"].items():
            if evs:                              # streams vacíos no ensucian el master
                cara_streams[f"video.cara.{k}"] = evs
    if cara_streams:
        streams.update(cara_streams)
        hdr = cara_j.get("header", {})
        modalidades["video"] = {
            "presente": True, "streams": sorted(cara_streams),
            "cara": {k: hdr.get(k) for k in ("schema_version", "extractor", "fps_muestreo",
                                             "cobertura", "fuente_mirada", "pose_neutral",
                                             "sidecars") if k in hdr}}
    else:
        modalidades.setdefault("video", {"presente": False})   # placeholder escalable

    # ---- modalidad AUTOR: marcas del timeline (wizard) ----------------------
    marcas_doc = None
    if marcas and Path(marcas).exists():
        import marcas as marcas_mod
        try:
            marcas_doc = _load(marcas)
        except Exception:
            marcas_doc = None
        if not marcas_mod._doc_valido(marcas_doc):
            marcas_doc = None
            log("⚠ snapshot de marcas malformada — se ignora (el master sale sin autor.marcas)")
    if marcas_doc is not None:
        dur_ref = _ffprobe_dur_raw(media) if media else None
        evs_autor, cuar = marcas_mod._validar_lista(
            marcas_doc.get("marcas"), dur_ref if dur_ref else float("inf"))
        cuarentena = [{"id": (c["marca"].get("id", "?") if isinstance(c["marca"], dict)
                              else "?"), "motivo": c["motivo"]} for c in cuar]
        if evs_autor:
            eventos = marcas_mod.a_eventos(evs_autor)
            streams["autor.marcas"] = eventos
            conf = marcas_mod.conflictos(eventos)
            modalidades["autor"] = {"presente": True, "streams": ["autor.marcas"],
                                    "n": len(eventos),
                                    "regla_solapes": "excluir_gana",
                                    **({"conflictos": conf} if conf else {}),
                                    **({"cuarentena": cuarentena} if cuarentena else {})}
            if conf:
                log(f"⚠ {len(conf)} conflicto(s) incluir∩excluir en las marcas del autor "
                    "(regla declarada: excluir gana) — quedan visibles en el master.")

    # ---- modalidad VIDEO (visión VLM): descripciones por chunk ---------------
    # (se FUSIONA con la modalidad video de cara — consenso vision r1.7)
    fuentes_vision = {}
    for lane, ruta in (("juego", vision_juego), ("camara", vision_camara)):
        if not ruta or not Path(ruta).exists():
            continue
        try:
            vdoc = _load(ruta)
        except Exception:
            vdoc = None
        if not isinstance(vdoc, dict) or not isinstance(vdoc.get("chunks"), list):
            log(f"⚠ visión {lane}: artefacto malformado — se ignora")
            continue
        evs = []
        for ch in vdoc["chunks"]:
            r = ch.get("respuesta")
            if not isinstance(r, dict):
                continue                       # chunk fallado: no entra al master
            t0c, t1c = float(ch["t_ini"]), float(ch["t_fin"])
            evs_ok = []
            for e in (r.get("eventos") or []):
                try:                           # defensa en profundidad (vision valida,
                    off = float(e["offset_s"])  # pero un artefacto viejo/editado no)
                except (KeyError, TypeError, ValueError):
                    continue
                if 0 <= off <= (t1c - t0c) + 1e-6:
                    evs_ok.append({"t": round(t0c + off, 2), "tipo": e.get("tipo"),
                                   "detalle": e.get("detalle")})
            ev = {"id": ch["id"].upper(),      # vj0007 → VJ0007, SIN renumerar
                  "t_ini": t0c, "t_fin": t1c,
                  "descripcion": r.get("descripcion"),
                  "eventos": evs_ok,
                  "observabilidad": r.get("observabilidad"),
                  "frames": len(ch.get("frames_ts") or []),
                  "corte_escena": bool(ch.get("corte_escena")),
                  "confianza": r.get("confianza")}
            if lane == "juego":
                ev["escena"] = r.get("escena")
                if r.get("texto_relevante"):
                    ev["texto_relevante"] = r["texto_relevante"]
            else:
                ev["accion_visible"] = r.get("accion_visible")
                ev["pose"] = r.get("pose")
                ev["atencion"] = r.get("atencion")
                ev["hablando_a_camara"] = r.get("hablando_a_camara")
                ev["emocion_aparente"] = r.get("emocion_aparente")
                ev["presencia"] = r.get("presencia")
                if r.get("vestimenta"):        # solo si al modelo le llamó la atención
                    ev["vestimenta"] = r["vestimenta"]
            evs.append(ev)
        if not evs:
            continue
        stream = f"video.{lane}.vlm"
        streams[stream] = evs
        vid_mod = modalidades.setdefault("video", {"presente": True, "streams": []})
        vid_mod["presente"] = True
        vid_mod.setdefault("streams", []).append(stream)
        vid_mod[f"n_{lane}_vlm"] = len(evs)
        fallos = (vdoc.get("stats") or {}).get("fallos", 0)
        if fallos:
            vid_mod[f"vlm_{lane}_incompleto"] = f"{fallos} chunk(s) sin respuesta"
        fuentes_vision[f"vision_{lane}"] = {"archivo": Path(ruta).name,
                                            "sha256": _sha256(ruta),
                                            "modelo": vdoc.get("modelo")}

    # ---- IDs ESTABLES por evento (orden cronológico dentro de cada stream) ----------
    # (los streams de IDS_EXTERNOS ya traen su ID derivado y NO se renumeran)
    for k, evs in streams.items():
        if k in IDS_EXTERNOS:
            evs.sort(key=lambda x: float(x.get("t_ini", 0)))
            continue
        pref = PREFIJOS.get(k, "X")
        for i, e in enumerate(sorted(evs, key=lambda x: float(x.get("t_ini", 0)))):
            e["id"] = f"{pref}{i:04d}"

    # ---- HEADER global (schema, duración real, hashes de fuentes, baselines) --------
    dur_media, dur_fuente, media_hit = None, "max_t_fin", None
    for m in ([media] if media else []) + medios:  # media= explícito gana; después cara/voz
        if not m:
            continue
        # el medio puede estar junto a la salida o un nivel arriba (layout media/metadata/)
        cands = [Path(m), salida.parent / Path(m).name, salida.parent.parent / Path(m).name]
        hit = next((c for c in cands if c.exists() and c.is_file()), None)
        if hit:
            dur_media = _ffprobe_dur(hit)
            if dur_media:
                dur_fuente = f"ffprobe:{hit.name}"
                media_hit = hit
                break
    baselines = {}
    if meta:
        if meta.get("emotion", {}).get("baseline"):
            baselines["voz.emocion"] = meta["emotion"]["baseline"]
        if meta.get("pausas", {}).get("baseline"):
            baselines["voz.pausas"] = meta["pausas"]["baseline"]
    if cara_j and cara_j.get("header", {}).get("baseline"):
        baselines["video.cara"] = cara_j["header"]["baseline"]
    fuentes = {}
    for nombre, p in (("metadata", f"{vz}.metadata.json"), ("segments", f"{vz}.segments.json"),
                      ("fondo", f"{fd}.fondo.json"), ("cara", f"{cr}.cara.json")):
        if Path(p).exists():
            fuentes[nombre] = {"archivo": Path(p).name, "sha256": _sha256(p)}
    if marcas_doc is not None:
        fuentes["marcas"] = {"archivo": Path(marcas).name, "sha256": _sha256(marcas),
                             "revision": marcas_doc.get("revision")}
    # identidad FUERTE del medio (v2.3, diseño tab-marcar h.5/B3): la tab «Marcar»
    # matchea video↔master por este fingerprint; sin mtime (no es identidad). Si el
    # medio no está o falla, el master sale igual (los viejos matchean por duración).
    if media_hit is not None:
        try:
            import medios as medios_mod
            fp_media = medios_mod.fingerprint(media_hit)
            fuentes["media"] = {"nombre": media_hit.name, "size": fp_media["size"],
                                "hash_muestreado": fp_media["hash_muestreado"],
                                "inventario_sha256": fp_media["inventario_sha256"],
                                "duracion": dur_media}
        except Exception as e:
            log(f"⚠ no pude tomar el fingerprint del medio ({e}) — el master sale sin "
                "fuentes.media (la tab «Marcar» matcheará por duración)")
    fuentes.update(fuentes_vision)
    header = {
        "schema_version": SCHEMA_VERSION,
        "generado": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "generador": GENERADOR,
        "duracion": dur_media if dur_media else _dur(streams),
        "duracion_fuente": dur_fuente,
        "fuentes": fuentes,
        "baselines": baselines,
        **({"media_layout": layout} if layout else {}),
    }

    master = {
        "video": salida.name,
        "duracion": header["duracion"],
        "header": header,
        "modalidades": modalidades,
        "streams": streams,
    }
    outp = Path(f"{_stem(salida)}.master.json")
    outp.write_text(json.dumps(master, ensure_ascii=False, indent=1), encoding="utf-8")
    log(f"master.json v{SCHEMA_VERSION}: {len(streams)} stream(s) · "
        f"{sum(len(v) for v in streams.values())} eventos · {master['duracion']}s "
        f"({dur_fuente}) → {outp.name}")
    return master


# ------------------------------------------------------ consultas por tiempo --
def en(master, t: float) -> dict:
    """TODO lo activo en el instante `t` (de todas las modalidades), listo para el vistazo."""
    out = {}
    for name, evs in master["streams"].items():
        hits = [e for e in evs
                if float(e.get("t_ini", 1e18)) <= t <= float(e.get("t_fin", e.get("t_ini", -1e18)))]
        if hits:
            out[name] = hits
    return out


def ventana(master, t0: float, t1: float) -> dict:
    """Todo lo que solapa la ventana [t0,t1] (para armar un clip alrededor de un momento)."""
    out = {}
    for name, evs in master["streams"].items():
        hits = [e for e in evs
                if float(e.get("t_ini", 1e18)) < t1 and float(e.get("t_fin", e.get("t_ini", -1e18))) > t0]
        if hits:
            out[name] = hits
    return out


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="Consolida la metadata de un video en master.json.")
    ap.add_argument("salida", help="ruta/base del proyecto → escribe <salida>.master.json")
    ap.add_argument("--voz", default=None, help="audio de tu voz (busca <voz>.metadata.json + .segments.json)")
    ap.add_argument("--fondo", default=None, help="audio del juego (busca <fondo>.fondo.json)")
    ap.add_argument("--cara", default=None, help="video analizado (busca <cara>.cara.json)")
    ap.add_argument("--media", default=None,
                    help="video/audio master real (para la duración por ffprobe)")
    ap.add_argument("--marcas", default=None,
                    help="sidecar/snapshot de marcas del autor → stream autor.marcas")
    ap.add_argument("--vision-juego", default=None, help="artefacto de visión del juego")
    ap.add_argument("--vision-camara", default=None, help="artefacto de visión de la cámara")
    ap.add_argument("--en", type=float, default=None, help="mostrar todo lo activo en el segundo t")
    a = ap.parse_args()
    m = consolidar(a.salida, voz=a.voz, fondo=a.fondo, cara=a.cara, media=a.media,
                   marcas=a.marcas, vision_juego=a.vision_juego,
                   vision_camara=a.vision_camara, log_cb=print)
    print("modalidades:", {k: v.get("presente") for k, v in m["modalidades"].items()})
    print("streams:", list(m["streams"]))
    if a.en is not None:
        print(f"\n== todo lo activo en t={a.en}s ==")
        print(json.dumps(en(m, a.en), ensure_ascii=False, indent=1))
