#!/usr/bin/env python3
"""
pipeline.py — orquestador HEADLESS de la pestaña «Extraer metadata».

Corre el DAG de pasos atómicos sobre una grabación (video multitrack) y produce TODA la
metadata en el directorio elegido: pistas FLAC normalizadas a la timeline canónica,
transcripción de la voz, metadata de voz (emoción/risa/pausas/ava), fondo (diálogo/
sonidos/descripción), cara (con la región elegida por el usuario), master.json y vistas.

Diseño consensuado con Codex gpt-5.6-sol (2 rondas):
three-brain-out/2026-07-16-tab-unificada/DISENO-final.md. Puntos clave:

  · WORKSPACE TRANSACCIONAL: cada paso escribe en su STAGING; al terminar se VALIDA,
    se mueve a generations/<paso>/<intento>/ y recién entonces se COMMITEA el manifest
    (os.replace = atómico). Después se sincronizan los ALIAS amigables en el outdir
    (voz.metadata.json, juego.fondo.json, …). Un crash en cualquier punto deja la
    generación anterior ÍNTEGRA; el manifest es la única verdad para el RESUME.
  · RESUME: un paso se reusa SOLO si su manifest committeado valida y coinciden
    fingerprint de entradas + parámetros efectivos + versión de implementación.
    Cambiar una dependencia invalida transitivamente a sus consumidores (el hash de
    entradas incluye los sha de los manifests de los que depende).
  · SKIP-Y-REPORTAR: paso sin dep/input → skipped con motivo textual; el reporte final
    lista SIEMPRE los saltados. Estados: reused / ok / skipped_unavailable /
    skipped_not_requested / failed / cancelled / interrupted.
  · FONDO: un solo runner físico (fondo.analizar_fondo, conserva su reuso de escenas y
    crash-safety por capa) + TRES nodos lógicos con resultado explícito por capa.
  · Progreso: barra total MONOTÓNICA por pesos (priors + mediciones reales en
    pipeline.history.json) + barra del paso actual.
  · Logs: logs/extraer-<run>.log junto a la app; footer de status; se conservan los
    últimos 5 finalizados.

La GUI (wizard_extraer.py) es un cliente de `correr(spec, event_cb, cancel)`; el futuro
supervisor headless (DISENO agente-app) consumirá este MISMO contrato de eventos.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
import traceback
from datetime import datetime
from pathlib import Path

import medios
import app_paths

SCHEMA_STATE = "extraer-pipeline/1"
APP_DIR = app_paths.SOURCE_DIR
LOGS_DIR = app_paths.LOGS_DIR
HISTORY_FILE = app_paths.PIPELINE_HISTORY_FILE
LOGS_CONSERVAR = 5

# Estados de paso (contrato del reporte — consensuados)
REUSED, OK, SKIP_UNAVAIL, SKIP_NOREQ = "reused", "ok", "skipped_unavailable", "skipped_not_requested"
FAILED, CANCELLED, INTERRUPTED, PENDIENTE = "failed", "cancelled", "interrupted", "pendiente"
PARTIAL = "partial"      # terminó con resultados PARCIALES consumibles (visión: algunos
                         # chunks fallaron) — cuenta como dependencia lista; el reuso lo
                         # rechaza `reuso_valido` para reintentar lo fallado (caché mediante)

# Pesos v1 (costo RELATIVO por segundo de media; priors consensuados C7). El costo fijo
# modela la carga del modelo. Cuando pipeline.history.json tiene mediciones para
# (paso, modelo, device, hardware), se usa su mediana en lugar del prior.
PESOS = {                                    # (por_segundo, fijo_seg)
    "extraer_voz": (0.05, 2), "extraer_juego": (0.05, 2),
    "transcribir_voz": (1.0, 15), "emocion_voz": (0.5, 10), "risa": (1.5, 15),
    "pausas": (0.005, 1), "ava": (0.005, 1), "publicar_metadata_voz": (0.0, 1),
    "fondo_dialogo": (1.0, 15), "fondo_sonidos": (0.8, 10), "fondo_descripcion": (2.5, 20),
    "cara": (2.0, 10), "consolidar": (0.01, 1), "vistas": (0.01, 2),
}
PESO_CPU_TRANSCRIBIR = 3.0                   # transcribir en CPU pesa 3× (prior)


# ================================================================== helpers de archivo --
def _escribir_atomico(path: Path, data: str):
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(data, encoding="utf-8")
    _replace_retry(tmp, path)


def _replace_retry(src: Path, dst: Path, intentos: int = 6):
    """os.replace con retry/backoff — en Windows un antivirus/indexador puede tener el
    destino abierto un instante."""
    for i in range(intentos):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if i == intentos - 1:
                raise
            time.sleep(0.25 * (2 ** i))


def _json_hash(obj) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def _leer_json(path: Path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


# ======================================================================== validadores --
# Cada tipo de artefacto tiene un validador VERSIONADO (consenso C4): parsea + schema +
# tipos + tiempos sanos + listas vacías EXPLÍCITAMENTE permitidas. Media: decodifica
# ventanas al inicio y al final (el header solo no detecta truncamiento).

def _v_tiempos(eventos, dur, tol=5.0, ini="t_ini", fin="t_fin") -> str | None:
    import math
    prev = None
    for e in eventos:
        a, b = e.get(ini), e.get(fin, e.get(ini))
        if not isinstance(a, (int, float)) or not isinstance(b, (int, float)):
            return "evento sin tiempos numéricos"
        if not (math.isfinite(a) and math.isfinite(b)):
            return "tiempos NaN/Inf"
        if a < -0.5 or (dur and b > dur + tol) or b < a:
            return f"tiempos fuera de rango ({a}..{b} vs dur {dur})"
        if prev is not None and a < prev - tol:
            return "eventos fuera de orden temporal"
        prev = a
    return None


def _val_flac(path: Path, ctx: "_Ctx") -> str | None:
    if path.stat().st_size == 0:
        return "archivo vacío"
    if not medios.decodifica_ventanas(path):
        return "no decodifica (¿truncado?)"
    return None


def _val_words(path: Path, ctx) -> str | None:
    d = _leer_json(path)
    if not isinstance(d, list):
        return "words.json no es una lista"
    for w in d:                                      # TODAS (truncar en 200 dejaba pasar
        if "word" not in w or "start" not in w or "end" not in w:   # corrupción tardía)
            return "palabra sin word/start/end"
    return _v_tiempos(d, ctx.info["duracion"], ini="start", fin="end")


def _val_segments(path: Path, ctx) -> str | None:
    d = _leer_json(path)
    if not isinstance(d, list):
        return "segments.json no es una lista"
    return _v_tiempos(d, ctx.info["duracion"], ini="start", fin="end")


def _val_metadata(path: Path, ctx) -> str | None:
    d = _leer_json(path)
    if not isinstance(d, dict):
        return "no es un objeto"
    for k in ("emotion", "laughter", "pausas", "instrucciones"):
        v = d.get(k)
        if v is None:
            continue                                     # sección no pedida: válido
        evs = v if isinstance(v, list) else v.get("events", [])
        if not isinstance(evs, list):
            return f"{k}.events no es lista"
        err = _v_tiempos(evs, ctx.info["duracion"])
        if err:
            return f"{k}: {err}"
    return None


def _val_fondo(path: Path, ctx) -> str | None:
    d = _leer_json(path)
    if not isinstance(d, dict) or d.get("tipo") != "audio_de_fondo":
        return "no es un fondo.json"
    for k in ("dialogo", "sonidos", "transitorios", "escenas", "descripciones"):
        if k in d and d[k]:
            err = _v_tiempos(d[k], ctx.info["duracion"])
            if err:
                return f"{k}: {err}"
    return None


def _val_cara(path: Path, ctx) -> str | None:
    d = _leer_json(path)
    if "header" not in d or "streams" not in d:
        return "sin header/streams (¿esquema cara-v1?)"
    for s, evs in d["streams"].items():
        err = _v_tiempos(evs, ctx.info["duracion"])
        if err:
            return f"{s}: {err}"
    return None


def _val_master(path: Path, ctx) -> str | None:
    d = _leer_json(path)
    h = d.get("header") or {}
    if h.get("schema_version") != 2:
        return f"schema_version {h.get('schema_version')} ≠ 2"
    if not d.get("streams"):
        return "master sin streams"
    return None


def _val_json_generico(path: Path, ctx) -> str | None:
    _leer_json(path)
    return None


def _val_sidecar_voz(path: Path, ctx) -> str | None:
    """Sidecars intermedios del análisis de voz (emocion/risa/pausas/ava): dict con
    events (o lista directa en risa) + tiempos sanos. Lista vacía = válido."""
    d = _leer_json(path)
    evs = d if isinstance(d, list) else (d.get("events") if isinstance(d, dict) else None)
    if not isinstance(evs, list):
        return "sin lista de events"
    return _v_tiempos(evs, ctx.info["duracion"])


VALIDADORES = {".flac": _val_flac, "words": _val_words, "segments": _val_segments,
               "metadata": _val_metadata, "fondo": _val_fondo, "cara": _val_cara,
               "master": _val_master, "json": _val_json_generico}


def _validar_artefacto(path: Path, ctx) -> str | None:
    """Elige el validador por el nombre del artefacto. Devuelve None si es válido, o el
    motivo del rechazo."""
    try:
        n = path.name
        if n.endswith(".flac"):
            return _val_flac(path, ctx)
        if n in ("emocion.json", "risa.json", "pausas.json", "ava.json"):
            return _val_sidecar_voz(path, ctx)
        for clave in ("master", "words", "segments", "metadata", "fondo", "cara"):
            if f".{clave}.json" in n or n.endswith(f"{clave}.json"):
                return VALIDADORES[clave](path, ctx)
        if n.endswith(".json"):
            return _val_json_generico(path, ctx)
        return None if path.stat().st_size > 0 else "archivo vacío"
    except Exception as e:
        return f"no valida: {e}"


# ====================================================================== historial/pesos --
def _hw_fingerprint() -> str:
    try:
        import hardware
        g = hardware.gpu_info() or {}
        return f"{hardware.cpu_name()}|{g.get('name', '')}"
    except Exception:
        return "?"


def _hist_leer() -> dict:
    try:
        return _leer_json(HISTORY_FILE)
    except Exception:
        return {}


def _hist_guardar(paso_id: str, clave_modelo: str, unidades: float, seg: float):
    """Registra una medición real (para calibrar la barra en corridas futuras)."""
    try:
        h = _hist_leer()
        k = f"{paso_id}|{clave_modelo}|{_hw_fingerprint()}"
        h.setdefault(k, []).append({"u": round(unidades, 2), "s": round(seg, 2)})
        h[k] = h[k][-20:]
        _escribir_atomico(HISTORY_FILE, json.dumps(h, ensure_ascii=False, indent=1))
    except Exception:
        pass


def _hist_tasa(paso_id: str, clave_modelo: str) -> float | None:
    """Mediana de seg/unidad medida para este paso+modelo+hardware, si hay historia."""
    h = _hist_leer().get(f"{paso_id}|{clave_modelo}|{_hw_fingerprint()}") or []
    tasas = sorted(m["s"] / max(m["u"], 0.01) for m in h if m.get("u"))
    return tasas[len(tasas) // 2] if tasas else None


# ============================================================================= runtime --
class _Ctx:
    """Contexto del run: spec resuelta, workspace, estado, eventos, log."""

    def __init__(self, spec, event_cb, cancel):
        self.spec = spec
        self.event_cb = event_cb or (lambda ev: None)
        self.cancel = cancel
        self.fuente = Path(spec["fuente"])
        self.outdir = Path(spec["outdir"])
        self.info: dict = {}
        self.fp: dict = {}
        self.work = None            # .pipeline-work/<source-id>
        self.state: dict = {}
        self.run_id = datetime.now().strftime("%Y%m%d-%H%M%S")
        # nonce único del run: run_id tiene resolución de SEGUNDOS y dos runs en el
        # mismo segundo colisionan (review vision r2.3 — el salt de «ignorar caché»
        # debe ser fresco de verdad)
        self.run_nonce = __import__("uuid").uuid4().hex[:8]
        self._logf = None
        self._pesos: dict[str, float] = {}
        self._peso_hecho = 0.0
        self._peso_total = 0.0
        self._paso_actual = None

    # ---- eventos / log ----
    def log(self, m, paso=None):
        linea = f"[{paso}] {m}" if paso else m
        self.event_cb({"tipo": "log", "linea": linea})
        if self._logf:
            try:
                self._logf.write(f"{datetime.now().strftime('%H:%M:%S')} {linea}\n")
                self._logf.flush()
            except Exception:
                pass

    def prog_paso(self, frac):
        pid = self._paso_actual
        total = self._frac_total(frac if frac is not None else 0.0)
        self.event_cb({"tipo": "prog", "paso": pid, "paso_frac": frac, "total_frac": total})

    def _frac_total(self, frac_paso) -> float:
        if not self._peso_total:
            return 0.0
        peso_act = self._pesos.get(self._paso_actual, 0.0)
        return min(1.0, (self._peso_hecho + peso_act * max(0.0, min(1.0, frac_paso)))
                   / self._peso_total)

    def cancelado(self) -> bool:
        return self.cancel is not None and self.cancel.is_set()

    # ---- workspace ----
    def dir_staging(self, paso_id, intento) -> Path:
        d = self.work / "staging" / f"{paso_id}-{intento}"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def dir_generacion(self, paso_id, intento) -> Path:
        return self.work / "generations" / paso_id / str(intento)

    def manifest_path(self, paso_id) -> Path:
        return self.work / "manifests" / f"{paso_id}.json"

    def manifest(self, paso_id) -> dict | None:
        try:
            return _leer_json(self.manifest_path(paso_id))
        except Exception:
            return None

    # ---- aliases (paths finales amigables en el outdir) ----
    def alias(self, nombre) -> Path:
        return self.outdir / nombre

    def pista_voz(self) -> Path:
        return self.alias("voz.flac")

    def pista_juego(self) -> Path:
        return self.alias("juego.flac")


def _guardar_estado(ctx: _Ctx):
    _escribir_atomico(ctx.outdir / "pipeline.state.json",
                      json.dumps(ctx.state, ensure_ascii=False, indent=1))


# ================================================================== commit / reuse core --
def _commit_paso(ctx: _Ctx, paso: dict, staging: Path, artefactos: dict, extra: dict,
                 t0: float, unidades: float, clave_modelo: str):
    """Valida artefactos en staging → mueve a la generación → commit del manifest →
    sincroniza aliases → limpia generaciones viejas. El ORDEN es el candado: si algo
    falla antes del manifest, la generación previa sigue activa e íntegra."""
    pid = paso["id"]
    # 1) validar TODO antes de publicar nada
    for rel in artefactos:
        err = _validar_artefacto(staging / rel, ctx)
        if err:
            raise RuntimeError(f"artefacto inválido «{rel}»: {err}")
    # 2) mover staging → generación
    intento = int(time.time())
    gen = ctx.dir_generacion(pid, intento)
    gen.mkdir(parents=True, exist_ok=True)
    hashes = {}
    for rel in artefactos:
        src, dst = staging / rel, gen / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        _replace_retry(src, dst)
        hashes[rel] = {"sha256": medios.hash_archivo(dst), "size": dst.stat().st_size}
    # 3) manifest = commit (os.replace atómico) — antes, recordar qué generación
    #    referenciaba el manifest ANTERIOR (esa es la que se conserva en la limpieza;
    #    ordenar por nombre podía conservar un orphan de un crash — ronda 3, h.15)
    man_prev = ctx.manifest(pid)
    gen_prev = man_prev.get("generacion") if man_prev else None
    man = {"schema": SCHEMA_STATE, "paso": pid, "version": paso.get("version", 1),
           "generacion": str(gen.relative_to(ctx.work)), "intento": intento,
           "run_id": ctx.run_id, "cuando": datetime.now().isoformat(timespec="seconds"),
           "params_hash": _params_hash(ctx, paso), "entradas_hash": _entradas_hash(ctx, paso),
           "artefactos": hashes, "alias": paso.get("alias", {}), "extra": extra}
    mp = ctx.manifest_path(pid)
    mp.parent.mkdir(parents=True, exist_ok=True)
    _escribir_atomico(mp, json.dumps(man, ensure_ascii=False, indent=1))
    # 4) aliases: copiar de la generación al outdir (os.replace por archivo, con retry).
    #    Un crash acá deja aliases mixtos, pero el manifest manda: se re-sincroniza al reanudar.
    _sync_aliases(ctx, man)
    # 5) limpiar: staging del paso + generaciones que NO son ni la nueva ni la que
    #    referenciaba el manifest anterior (orphans de crashes incluidos)
    _limpiar_viejo(ctx, pid, str(gen.relative_to(ctx.work)), gen_prev)
    # 6) medición para calibrar la barra
    _hist_guardar(pid, clave_modelo, unidades, time.time() - t0)
    return man


def _sync_aliases(ctx: _Ctx, man: dict):
    gen = ctx.work / man["generacion"]
    for rel, destino in (man.get("alias") or {}).items():
        src, dst = gen / rel, ctx.alias(destino)
        if not src.exists():
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        tmp = dst.with_name(dst.name + ".alias-tmp")
        import shutil
        shutil.copy2(src, tmp)
        _replace_retry(tmp, dst)


def _aliases_ok(ctx: _Ctx, man: dict) -> bool:
    """True si todos los aliases del manifest existen y su CONTENIDO coincide (sha; los
    gigantes >512MB solo por tamaño)."""
    for rel, destino in (man.get("alias") or {}).items():
        info = man["artefactos"].get(rel)
        dst = ctx.alias(destino)
        if info is None or not dst.exists() or dst.stat().st_size != info["size"]:
            return False
        if info["size"] <= 512 * 1024 * 1024 and medios.hash_archivo(dst) != info["sha256"]:
            return False
    return True


def _limpiar_viejo(ctx: _Ctx, pid: str, gen_actual: str, gen_prev: str | None):
    """Borra staging del paso + toda generación que no sea la ACTUAL ni la PREVIA
    referenciada por el manifest anterior (así un orphan de un crash no desplaza a la
    generación buena que prometimos conservar)."""
    try:
        import shutil
        for d in (ctx.work / "staging").glob(f"{pid}-*"):
            shutil.rmtree(d, ignore_errors=True)
        conservar = {Path(gen_actual).name} | ({Path(gen_prev).name} if gen_prev else set())
        base = ctx.work / "generations" / pid
        if base.exists():
            for g in base.iterdir():
                if g.name not in conservar:
                    shutil.rmtree(g, ignore_errors=True)
    except Exception:
        pass


def _params_hash(ctx: _Ctx, paso: dict) -> str:
    return _json_hash(paso["params"](ctx))


def _entradas_hash(ctx: _Ctx, paso: dict) -> str:
    """Hash de las ENTRADAS efectivas: fingerprint de la fuente + IDENTIDAD COMPLETA de
    los manifests de los que depende — sha de artefactos Y params_hash/version del dep
    (ronda 3, h.5: un output byte-idéntico con parámetros distintos también invalida).
    Solo cuentan deps listas EN ESTA corrida (un manifest viejo de una dep desmarcada
    no debe 'estabilizar' al consumidor). Los pasos pueden declarar `entradas_extra`
    (lambda ctx -> dict) para entradas EXTERNAS adicionales — p.ej. el sha de la
    snapshot de marcas del autor (consenso timeline-marcas r2: es una entrada, no un
    parámetro)."""
    partes = {"fuente": ctx.fp}
    if "entradas_extra" in paso:
        partes["_extra"] = paso["entradas_extra"](ctx)
    for dep in paso.get("requiere", []):
        if not _dep_lista(ctx, dep):
            partes[dep] = None
            continue
        m = ctx.manifest(dep) or {}
        partes[dep] = {"art": {k: v["sha256"] for k, v in m.get("artefactos", {}).items()},
                       "params": m.get("params_hash"), "version": m.get("version")}
    return _json_hash(partes)


_HASH_MAX = 512 * 1024 * 1024        # >512MB: verificar por tamaño (hashear tarda de más)


def _puede_reusar(ctx: _Ctx, paso: dict) -> bool:
    if ctx.spec.get("rehacer"):        # «Re-hacer todo»: ignora manifests (pero NO borra
        return False                   # nada — los outputs viejos siguen hasta el commit nuevo)
    m = ctx.manifest(paso["id"])
    if not m:
        return False
    if (m.get("version") != paso.get("version", 1)
            or m.get("params_hash") != _params_hash(ctx, paso)
            or m.get("entradas_hash") != _entradas_hash(ctx, paso)):
        return False
    if "reuso_valido" in paso and not paso["reuso_valido"](ctx, m):
        return False                   # gate extra del paso (fondo: capas pedidas ok)
    gen = ctx.work / m["generacion"]
    for rel, info in m["artefactos"].items():
        p = gen / rel
        if not p.exists() or p.stat().st_size != info["size"]:
            return False
        # el SHA se verifica de verdad (ronda 3, h.6: corrupción con mismo tamaño);
        # solo los gigantes quedan en chequeo por tamaño
        if info["size"] <= _HASH_MAX and medios.hash_archivo(p) != info["sha256"]:
            return False
    if not _aliases_ok(ctx, m):
        _sync_aliases(ctx, m)                   # aliases rotos/mixtos → re-sincronizar
        if not _aliases_ok(ctx, m):
            return False
    return True


# ======================================================================== definición DAG --
def _cfg(ctx, seccion, clave, default=None):
    return ((ctx.spec.get("config") or {}).get(seccion) or {}).get(clave, default)


def _rol_idx(ctx, rol):
    r = (ctx.spec.get("roles") or {}).get(rol)
    return r if isinstance(r, int) else None


def _pista(ctx, rol):
    idx = _rol_idx(ctx, rol)
    if idx is None:
        return None
    return next((p for p in ctx.info["pistas"] if p["idx"] == idx), None)


def _paso_extraer(rol, alias_flac, mono):
    def run(ctx, staging, log, prog):
        pista = _pista(ctx, rol)
        res = medios.extraer_pista(ctx.fuente, pista, staging / alias_flac, mono=mono,
                                   cancel=ctx.cancel, log_cb=log, progress_cb=prog)
        return {alias_flac: None}, {"extraccion": res}
    return {
        "id": f"extraer_{rol}", "etiqueta": f"Extraer pista ({rol})", "version": 1,
        "requiere": [],
        "pedido": lambda ctx: _rol_idx(ctx, rol) is not None,
        "disponible": lambda ctx: (True, "") if _pista(ctx, rol) else
                                  (False, f"no hay pista asignada al rol «{rol}»"),
        "params": lambda ctx: {"rol": rol, "idx": _rol_idx(ctx, rol), "mono": mono,
                               "delta": (_pista(ctx, rol) or {}).get("delta")},
        "alias": {alias_flac: alias_flac},
        "unidades": lambda ctx: ctx.info["duracion"],
        "modelo": lambda ctx: "ffmpeg",
        "run": run,
    }


def _transcribir_disponible(ctx):
    try:
        import core  # noqa: F401
        return True, ""
    except Exception as e:
        return False, f"no se pudo importar core ({e})"


def _run_transcribir(ctx, staging, log, prog):
    import core
    cfg = ctx.spec.get("config", {}).get("transcripcion", {})
    res = core.transcribe(
        ctx.pista_voz(), staging,
        model_name=cfg.get("modelo", "medium"), lang=cfg.get("idioma", "es"),
        device=cfg.get("device", "auto"),
        want_segments=True, want_srt=False, want_cues=False,
        want_align=cfg.get("align", True),
        cancel=ctx.cancel, log_cb=log,
        progress_cb=lambda f, eta=None: prog(f))
    if res is None:
        raise InterruptedError("transcripción cancelada")
    arte = {"voz.words.json": None, "voz.segments.json": None}
    return arte, {"modelo": cfg.get("modelo"), "align": cfg.get("align", True)}


PASO_TRANSCRIBIR = {
    "id": "transcribir_voz", "etiqueta": "Transcribir la voz", "version": 1,
    "requiere": ["extraer_voz"],
    "pedido": lambda ctx: _rol_idx(ctx, "voz") is not None,
    "disponible": _transcribir_disponible,
    "params": lambda ctx: {k: _cfg(ctx, "transcripcion", k) for k in
                           ("modelo", "idioma", "device", "align")},
    "alias": {"voz.words.json": "voz.words.json", "voz.segments.json": "voz.segments.json"},
    "unidades": lambda ctx: ctx.info["duracion"],
    "modelo": lambda ctx: f"{_cfg(ctx, 'transcripcion', 'modelo', 'medium')}|"
                          f"{_cfg(ctx, 'transcripcion', 'device', 'auto')}",
    "run": _run_transcribir,
}


def _paso_analisis_voz(pid, etiqueta, clave_cfg, disponible_fn, run_fn, unidades=None):
    return {
        "id": pid, "etiqueta": etiqueta, "version": 1,
        "requiere": ["extraer_voz", "transcribir_voz"] if pid != "risa" else ["extraer_voz"],
        "pedido": lambda ctx: bool(_cfg(ctx, "voz", clave_cfg, False)),
        "disponible": disponible_fn,
        "params": lambda ctx: {clave_cfg: True},
        "alias": {},                              # sidecar interno; el merge publica
        "unidades": unidades or (lambda ctx: ctx.info["duracion"]),
        "modelo": lambda ctx: pid,
        "run": run_fn,
    }


def _disp_emocion(ctx):
    try:
        import metadata
        return (True, "") if metadata.available() else (False, "falta transformers/pysentimiento")
    except Exception as e:
        return False, f"metadata no importa ({e})"


def _run_emocion(ctx, staging, log, prog):
    import metadata
    segs = _leer_json(ctx.alias("voz.segments.json"))
    res = metadata.extract(str(ctx.pista_voz()), segs, log_cb=log,
                           progress_cb=lambda f, eta=None: prog(f))
    (staging / "emocion.json").write_text(json.dumps(res, ensure_ascii=False), encoding="utf-8")
    return {"emocion.json": None}, {"n": res.get("n")}


def _disp_risa(ctx):
    try:
        import laughter
        return (True, "") if laughter.available() else (False, "falta el repo/deps de risa")
    except Exception as e:
        return False, f"laughter no importa ({e})"


def _run_risa(ctx, staging, log, prog):
    import laughter
    res = laughter.detect(str(ctx.pista_voz()), log_cb=log,
                          progress_cb=lambda f, eta=None: prog(f))
    (staging / "risa.json").write_text(json.dumps(res, ensure_ascii=False), encoding="utf-8")
    return {"risa.json": None}, {"n": len(res)}


def _run_pausas(ctx, staging, log, prog):
    import extraer_pausas
    res = extraer_pausas.extract(extraer_pausas.load_words(str(ctx.alias("voz.words.json"))),
                                 log_cb=log)
    (staging / "pausas.json").write_text(json.dumps(res, ensure_ascii=False), encoding="utf-8")
    return {"pausas.json": None}, {"n": res.get("n")}


def _run_ava(ctx, staging, log, prog):
    import ava
    res = ava.extract(ava.load_words(str(ctx.alias("voz.words.json"))), log_cb=log)
    (staging / "ava.json").write_text(json.dumps(res, ensure_ascii=False), encoding="utf-8")
    return {"ava.json": None}, {"n": res.get("n")}


def _run_publicar_meta(ctx, staging, log, prog):
    """Merge DETERMINISTA de los sidecars de análisis de voz → voz.metadata.json (el
    formato que consume consolidar). Solo entran las secciones cuyo paso committeó."""
    out = {}
    fuentes = {"emocion_voz": ("emocion.json", "emotion"), "risa": ("risa.json", "laughter"),
               "pausas": ("pausas.json", "pausas"), "ava": ("ava.json", "instrucciones")}
    for pid, (arch, clave) in fuentes.items():
        # SOLO análisis que terminaron ok/reused EN ESTA corrida: un manifest viejo de un
        # análisis desmarcado o fallido no entra al metadata nuevo (ronda 3, h.2)
        if not _dep_lista(ctx, pid):
            continue
        m = ctx.manifest(pid)
        if not m or arch not in m.get("artefactos", {}):
            continue
        out[clave] = _leer_json(ctx.work / m["generacion"] / arch)
    if not out:
        raise RuntimeError("ningún análisis de voz produjo datos (nada que publicar)")
    (staging / "voz.metadata.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"voz.metadata.json": None}, {"secciones": sorted(out)}


PASO_PUBLICAR_META = {
    "id": "publicar_metadata_voz", "etiqueta": "Publicar metadata de voz", "version": 1,
    "requiere": ["emocion_voz", "risa", "pausas", "ava"],
    "requiere_alguna": True,       # basta con que ALGUNA dependencia haya committeado
    "pedido": lambda ctx: any(_cfg(ctx, "voz", k, False)
                              for k in ("emocion", "risa", "pausas", "ava")),
    "disponible": lambda ctx: (True, ""),
    "params": lambda ctx: {k: bool(_cfg(ctx, "voz", k, False))
                           for k in ("emocion", "risa", "pausas", "ava")},
    "alias": {"voz.metadata.json": "voz.metadata.json"},
    "unidades": lambda ctx: 1.0,
    "modelo": lambda ctx: "merge",
    "run": _run_publicar_meta,
}


def _run_fondo(ctx, staging, log, prog):
    """UN runner físico (conserva reuso de escenas y guardado por capa de fondo.py);
    los resultados POR CAPA los emite fondo.py en `capas` (mod C6) y acá se convierten
    en los tres nodos lógicos del reporte."""
    import shutil

    import fondo as fondo_mod
    juego = ctx.pista_juego()
    # arrancar del fondo.json de la generación COMMITTEADA DE ESTA FUENTE (manifest) →
    # fondo.py reusa escenas ya calculadas. NUNCA del alias del outdir: dos videos que
    # comparten carpeta contaminarían sus streams (review ronda 3, h.1 BLOQUEANTE).
    man_prev = ctx.manifest("fondo")
    if man_prev:
        prev = ctx.work / man_prev["generacion"] / "juego.fondo.json"
        if prev.exists():
            shutil.copy2(prev, staging / "juego.fondo.json")
    cfg = ctx.spec.get("config", {}).get("fondo", {})
    capas: dict = {}
    fondo_mod.analizar_fondo(
        str(juego), outdir=staging,
        do_dialogo=cfg.get("dialogo", False), whisper_model=cfg.get("wmodel", "medium"),
        do_sonidos=cfg.get("sonidos", False), do_descripcion=cfg.get("descripcion", False),
        modelo_desc=cfg.get("dmodel", "qwen2.5-omni-3b"),
        ngl_desc=(99 if cfg.get("lalm_gpu") else 0), device=cfg.get("device", "auto"),
        lalm_backend=cfg.get("lalm_backend", "local"),
        lalm_online=cfg.get("lalm_online"),
        # la key correcta depende del PROVEEDOR del modelo elegido (openrouter o
        # dashscope/Alibaba para la familia Qwen-Omni) — describir.key_para resuelve
        lalm_api_key=(__import__("describir").key_para(cfg.get("lalm_online"))
                      if cfg.get("lalm_backend") == "online" else None),
        cancel=ctx.cancel, log_cb=log, progress_cb=lambda f, eta=None: prog(f),
        capas=capas)
    if ctx.cancelado():
        raise InterruptedError("fondo cancelado")
    arte = {"juego.fondo.json": None}
    # el words/segments del juego que escribe transcribir_juego en staging: registrarlos
    for extraj in staging.glob("juego.*.json"):
        arte[extraj.name] = None
    return arte, {"capas": capas}


def _fondo_capas_pedidas(ctx) -> list[str]:
    return [k for k in ("dialogo", "sonidos", "descripcion") if _cfg(ctx, "fondo", k, False)]


def _fondo_estado_final(ctx, extra) -> tuple[str, str]:
    """Una capa PEDIDA que falló ⇒ el paso NO es ok (el manifest igual committea las
    capas buenas, pero el reuse queda vetado y se reintenta — ronda 3, h.3 BLOQUEANTE)."""
    capas = (extra or {}).get("capas") or {}
    malas = [c for c in _fondo_capas_pedidas(ctx)
             if capas.get(c, {}).get("status") not in ("ok",)]
    if malas:
        motivos = "; ".join(f"{c}: {capas.get(c, {}).get('reason', 'sin resultado')}"
                            for c in malas)
        return FAILED, f"capa(s) sin completar → {motivos}"
    return OK, ""


def _fondo_reuso_valido(ctx, man) -> bool:
    """Reusable SOLO si TODAS las capas pedidas AHORA quedaron ok en ese manifest."""
    capas = (man.get("extra") or {}).get("capas") or {}
    return all(capas.get(c, {}).get("status") == "ok" for c in _fondo_capas_pedidas(ctx))


PASO_FONDO = {
    "id": "fondo", "etiqueta": "Audio de fondo (juego)", "version": 1,
    "requiere": ["extraer_juego"],
    "nodos": ["fondo_dialogo", "fondo_sonidos", "fondo_descripcion"],   # nodos LÓGICOS
    "estado_final": _fondo_estado_final,
    "reuso_valido": _fondo_reuso_valido,
    "pedido": lambda ctx: _rol_idx(ctx, "juego") is not None and any(
        _cfg(ctx, "fondo", k, False) for k in ("dialogo", "sonidos", "descripcion")),
    "disponible": lambda ctx: (True, ""),
    "params": lambda ctx: {k: _cfg(ctx, "fondo", k) for k in
                           ("dialogo", "sonidos", "descripcion", "wmodel", "dmodel",
                            "device", "lalm_gpu", "lalm_backend", "lalm_online")},
    "alias": {"juego.fondo.json": "juego.fondo.json"},
    "unidades": lambda ctx: ctx.info["duracion"],
    "modelo": lambda ctx: f"{_cfg(ctx, 'fondo', 'wmodel', 'medium')}|"
                          f"{_cfg(ctx, 'fondo', 'dmodel', '-')}",
    "run": _run_fondo,
}


def _disp_cara(ctx):
    try:
        import cara
        return (True, "") if cara.available() else (False, "falta mediapipe")
    except Exception as e:
        return False, f"cara no importa ({e})"


def _rect_px(ctx, nombre) -> tuple | None:
    r = (ctx.spec.get("rects") or {}).get(nombre)
    if not r or not r.get("confirmado"):
        return None
    v = ctx.info.get("video") or {}
    n = r.get("norm")
    # dims de DISPLAY (post-rotación): el crop de cara corre DESPUÉS de la autorrotación
    # de ffmpeg, así que sus coords viven en el mismo espacio que el preview del wizard
    w = v.get("display_width") or v.get("width")
    h = v.get("display_height") or v.get("height")
    if not n or not w:
        return None
    x0, y0, x1, y1 = n
    return (int(x0 * w), int(y0 * h), int(x1 * w), int(y1 * h))


def _nombre_master(ctx) -> str:
    """Nombre del master SANITIZADO Windows-first: es un NOMBRE de archivo, nunca una
    ruta (review ronda 3, h.11 — traversal, separadores, reservados CON/AUX/…)."""
    import re
    crudo = _cfg(ctx, "master", "nombre", None) or ctx.fuente.stem
    limpio = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", str(crudo)).strip(" .")
    limpio = Path(limpio).name or ctx.fuente.stem
    if limpio.split(".")[0].upper() in ("CON", "PRN", "AUX", "NUL",
                                        *(f"COM{i}" for i in range(1, 10)),
                                        *(f"LPT{i}" for i in range(1, 10))):
        limpio = f"_{limpio}"
    return limpio


def _run_cara(ctx, staging, log, prog):
    import cara
    region = _rect_px(ctx, "camara")
    res = cara.analizar_video(str(ctx.fuente), outdir=staging, cancel=ctx.cancel,
                              log_cb=log, progress_cb=lambda f, eta=None: prog(f),
                              emociones=bool(_cfg(ctx, "cara", "emociones", True)),
                              region=region)
    if ctx.cancelado():
        raise InterruptedError("cara cancelado")
    if res is None:
        raise RuntimeError("el análisis de cara no produjo resultado (¿sin cara en la región?)")
    stem = ctx.fuente.stem
    arte = {f"{stem}.cara.json": None, f"{stem}.cara.detalle.json": None,
            f"{stem}.facecam.json": None}
    return arte, {"region": region}


def _paso_cara():
    def alias(ctx):
        stem = ctx.fuente.stem
        return {f"{stem}.cara.json": f"{stem}.cara.json",
                f"{stem}.cara.detalle.json": f"{stem}.cara.detalle.json",
                f"{stem}.facecam.json": f"{stem}.facecam.json"}
    return {
        "id": "cara", "etiqueta": "Cara (facecam)", "version": 1,
        "requiere": [],
        "pedido": lambda ctx: bool(_cfg(ctx, "cara", "activar", True)),
        "disponible": _disp_cara,
        "params": lambda ctx: {"emociones": bool(_cfg(ctx, "cara", "emociones", True)),
                               "region": _rect_px(ctx, "camara"),
                               "mirada": _mirada_valores()},
        "alias_fn": alias,
        "unidades": lambda ctx: ctx.info["duracion"],
        "modelo": lambda ctx: "mediapipe+emotieff" if _cfg(ctx, "cara", "emociones", True)
                              else "mediapipe",
        "run": _run_cara,
    }


# ---------------------------------------------------------------- visión (VLM) --
def _vision_pedida(ctx, lane: str) -> bool:
    return bool(_cfg(ctx, "vision", lane, False))


def _run_motion(ctx, staging, log, prog):
    import vision
    lanes = [ln for ln in ("juego", "camara") if _vision_pedida(ctx, ln)]
    if "camara" in lanes and not _rect_px(ctx, "camara"):
        lanes.remove("camara")
        log("⚠ lane cámara sin rect confirmado — el scan sigue solo con juego")
    if not lanes:
        raise RuntimeError("visión pedida pero sin lanes utilizables")
    m = vision.motion_scan(ctx.fuente, ctx.info, ctx.spec.get("rects") or {}, lanes,
                           cancel=ctx.cancel, progress_cb=prog, log_cb=log)
    stem = ctx.fuente.stem
    (staging / f"{stem}.motion.json").write_text(
        json.dumps(m, ensure_ascii=False), encoding="utf-8")
    return {f"{stem}.motion.json": None}, {"lanes": lanes}


def _paso_motion():
    return {
        "id": "motion", "etiqueta": "Movimiento del video", "version": 1,
        "requiere": [],
        "pedido": lambda ctx: _vision_pedida(ctx, "juego") or _vision_pedida(ctx, "camara"),
        "disponible": lambda ctx: (True, "") if ctx.info.get("video")
                                  else (False, "la fuente no tiene video"),
        "params": lambda ctx: {"fps": __import__("vision").FPS_SCAN,
                               "umbral": __import__("vision").UMBRAL_PIXEL,
                               "rects": ctx.spec.get("rects"),
                               "lanes": [ln for ln in ("juego", "camara")
                                         if _vision_pedida(ctx, ln)]},
        "alias_fn": lambda ctx: {f"{ctx.fuente.stem}.motion.json":
                                 f"{ctx.fuente.stem}.motion.json"},
        "unidades": lambda ctx: ctx.info["duracion"],
        "modelo": lambda ctx: "motion-scan",
        "run": _run_motion,
    }


def _vision_cfg(ctx) -> dict:
    import vision
    v = (ctx.spec.get("config") or {}).get("vision") or {}
    mf = v.get("max_frames") or vision.MAX_FRAMES
    mf = min(max(int(mf), 1), vision.MAX_FRAMES_TOPE)
    return {"modelo": v.get("modelo") or vision.MODELO_DEFAULT,
            "provider_pin": bool(v.get("provider_pin", True)),
            "max_chunks": int(v.get("max_chunks") or 500),
            "chunk_cfg": {"max_frames": mf},
            # salt FRESCO por corrida (review impl r1.3 + r2.3): «ignorar caché» tiene
            # que re-consultar en CADA run (nonce uuid — run_id colisiona en el mismo
            # segundo), y como entra al params_hash el manifest tampoco se reusa
            "salt": f"ignorar-{ctx.run_nonce}" if v.get("ignorar_cache") else None}


def _disp_vision(ctx):
    try:
        import requests  # noqa: F401
    except Exception:
        return False, "falta requests"
    import hardware
    if not (hardware.load().get("openrouter_api_key")
            or os.environ.get("OPENROUTER_API_KEY")):
        return False, "configurá la API key de OpenRouter en el paso 2"
    return True, ""


def _run_vision(lane):
    def run(ctx, staging, log, prog):
        import hardware
        import vision
        man = ctx.manifest("motion")
        motion = json.loads((ctx.work / man["generacion"] /
                             f"{ctx.fuente.stem}.motion.json").read_text(encoding="utf-8"))
        if lane not in motion.get("lanes", {}):
            raise RuntimeError(f"el scan de movimiento no incluyó la lane {lane} "
                               "(¿rect sin confirmar?) — re-corré con el rect confirmado")
        key = hardware.load().get("openrouter_api_key") or os.environ.get("OPENROUTER_API_KEY")
        res = vision.analizar_lane(ctx.fuente, ctx.fp, motion, lane, _vision_cfg(ctx),
                                   key, ctx.work / "cache" / "vision",
                                   cancel=ctx.cancel, progress_cb=prog, log_cb=log)
        stem = ctx.fuente.stem
        (staging / f"{stem}.vision-{lane}.json").write_text(
            json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
        return {f"{stem}.vision-{lane}.json": None}, dict(res["stats"])
    return run


def _paso_vision(lane):
    def estado_final(ctx, extra):
        f, n = (extra or {}).get("fallos", 0), max((extra or {}).get("chunks", 1), 1)
        if not f:
            return OK, ""
        if f / n <= 0.20:
            return PARTIAL, f"{f} de {n} chunks fallaron (se reintentan al re-correr)"
        return FAILED, f"{f} de {n} chunks fallaron (>20%)"
    def params(ctx):
        import vision
        cfg = _vision_cfg(ctx)
        return {**cfg, "prompt_sha": __import__("hashlib").sha256(
                    (vision.PROMPT_JUEGO if lane == "juego"
                     else vision.PROMPT_CAMARA).encode()).hexdigest()[:16],
                "schema_ver": vision.SCHEMA_VER,
                # el algoritmo del plan invalida por VALOR (review impl r1.7):
                # cambiar B/F/MIN/MAX/valle/sep/max_frames o la versión re-corre
                "plan": {"ver": vision.PLAN_VER, "B": vision.CHUNK_B,
                         "min": vision.CHUNK_MIN, "max": vision.CHUNK_MAX,
                         "max_frames": cfg["chunk_cfg"]["max_frames"],
                         "valle": vision.VALLE_S, "sep": vision.SEP_FRAMES_S},
                "rect": _rect_px(ctx, "camara")
                if lane == "camara" else _rect_px(ctx, "juego")}
    return {
        "id": f"vision_{lane}",
        "etiqueta": f"Visión VLM ({'juego' if lane == 'juego' else 'cámara'})",
        "version": 1,
        "requiere": ["motion"],
        "pedido": lambda ctx: _vision_pedida(ctx, lane),
        "disponible": _disp_vision,
        "params": params,
        "alias_fn": lambda ctx: {f"{ctx.fuente.stem}.vision-{lane}.json":
                                 f"{ctx.fuente.stem}.vision-{lane}.json"},
        # PARTIAL nunca se reusa: re-correr reintenta SOLO los chunks fallados
        # (los ok salen del caché pagado, gratis) — consenso vision r2.5
        "reuso_valido": lambda ctx, man: not (man.get("extra") or {}).get("fallos"),
        "unidades": lambda ctx: ctx.info["duracion"],
        "modelo": lambda ctx: "openrouter",
        "estado_final": estado_final,
        "run": _run_vision(lane),
    }


def _mirada_valores() -> dict:
    """Los VALORES efectivos de calibración de mirada (no el nombre del preset): si la
    calibración cambia, el params_hash cambia y cara se re-corre (consenso P3)."""
    try:
        import cara
        return {"zona": dict(cara.MIRA_ZONA), "ojos": dict(cara.OJOS_CAL)}
    except Exception:
        return {}


def _run_consolidar(ctx, staging, log, prog):
    import consolidar as consolidar_mod
    nombre = _nombre_master(ctx)
    voz = str(ctx.pista_voz()) if _dep_lista(ctx, "extraer_voz") else None
    juego = str(ctx.pista_juego()) if _dep_lista(ctx, "fondo") else None
    cara_ref = str(ctx.outdir / ctx.fuente.name) if _dep_lista(ctx, "cara") else None
    layout = {"rects": ctx.spec.get("rects"), "pistas": ctx.spec.get("roles"),
              "timeline": {"t0": ctx.info.get("t0"),
                           "offsets": {r: (ctx.manifest(f"extraer_{r}") or {})
                                       .get("extra", {}).get("extraccion")
                                       for r in ("voz", "juego")}}}
    snap = getattr(ctx, "marcas", None)
    stem = ctx.fuente.stem
    vis_j = (str(ctx.alias(f"{stem}.vision-juego.json"))
             if _dep_lista(ctx, "vision_juego") else None)
    vis_c = (str(ctx.alias(f"{stem}.vision-camara.json"))
             if _dep_lista(ctx, "vision_camara") else None)
    consolidar_mod.consolidar(str(staging / nombre), voz=voz, fondo=juego, cara=cara_ref,
                              media=str(ctx.fuente), layout=layout,
                              marcas=str(snap["snapshot"]) if snap else None,
                              vision_juego=vis_j, vision_camara=vis_c, log_cb=log)
    return {f"{nombre}.master.json": None}, {"nombre": nombre}


def _paso_consolidar():
    def alias(ctx):
        nombre = _nombre_master(ctx)
        return {f"{nombre}.master.json": f"{nombre}.master.json"}
    return {
        # version 4 (2026-07-21): header.fuentes.media (identidad del medio, tab «Marcar»)
        # (v3 2026-07-17: visión VLM; v2 2026-07-16: autor.marcas) — solo consolidar+vistas
        "id": "consolidar", "etiqueta": "Consolidar master.json", "version": 4,
        "requiere": ["publicar_metadata_voz", "fondo", "cara", "transcribir_voz",
                     "vision_juego", "vision_camara"],
        "requiere_alguna": True,
        "pedido": lambda ctx: True,
        "disponible": lambda ctx: (True, ""),
        "params": lambda ctx: {"nombre": _nombre_master(ctx),
                               "rects": ctx.spec.get("rects"), "roles": ctx.spec.get("roles")},
        # la snapshot de marcas es una ENTRADA EXTERNA: su sha invalida el reuso
        "entradas_extra": lambda ctx: {"marcas_sha":
                                       (getattr(ctx, "marcas", None) or {}).get("sha")},
        "alias_fn": alias,
        "unidades": lambda ctx: 1.0,
        "modelo": lambda ctx: "consolidar",
        "run": _run_consolidar,
    }


def _run_vistas(ctx, staging, log, prog):
    import shutil

    import vistas as vistas_mod
    nombre = _nombre_master(ctx)
    master_final = ctx.alias(f"{nombre}.master.json")
    vistas_mod.generar(str(master_final), log_cb=log)     # escribe vistas/ junto al master
    # el bundle vistas/ ya es atómico por dentro (vistas.py); acá solo se registra un
    # testigo en la generación para el manifest (el bundle en sí vive junto al master)
    inv = master_final.parent / "vistas" / "inventario.json"
    if not inv.exists():
        raise RuntimeError("vistas no produjo inventario.json")
    shutil.copy2(inv, staging / "inventario.json")
    return {"inventario.json": None}, {"vistas_dir": str(master_final.parent / "vistas")}


PASO_VISTAS = {
    # version 3 (2026-07-17): contexto visual VLM en el dossier
    # (v2 2026-07-16: directivas.md — ledger de marcas del autor + Ava)
    "id": "vistas", "etiqueta": "Vistas para la IA", "version": 3,
    "requiere": ["consolidar"],
    "pedido": lambda ctx: True,
    "disponible": lambda ctx: (True, ""),
    "params": lambda ctx: {"master": _nombre_master(ctx)},
    "alias": {},
    "unidades": lambda ctx: 1.0,
    "modelo": lambda ctx: "vistas",
    "run": _run_vistas,
}


def _pasos() -> list[dict]:
    return [
        _paso_extraer("voz", "voz.flac", mono=True),
        _paso_extraer("juego", "juego.flac", mono=False),
        PASO_TRANSCRIBIR,
        _paso_analisis_voz("emocion_voz", "Emoción (voz)", "emocion", _disp_emocion, _run_emocion),
        _paso_analisis_voz("risa", "Risa", "risa", _disp_risa, _run_risa),
        _paso_analisis_voz("pausas", "Pausas", "pausas", lambda ctx: (True, ""), _run_pausas),
        _paso_analisis_voz("ava", "Instrucciones a Ava", "ava", lambda ctx: (True, ""), _run_ava),
        PASO_PUBLICAR_META,
        PASO_FONDO,
        _paso_cara(),
        _paso_motion(),
        _paso_vision("juego"),
        _paso_vision("camara"),
        _paso_consolidar(),
        PASO_VISTAS,
    ]


# ============================================================================== correr --
def _rotar_logs():
    try:
        LOGS_DIR.mkdir(exist_ok=True)
        term = sorted((p for p in LOGS_DIR.glob("extraer-*.log") if _log_finalizado(p)),
                      key=lambda p: p.name, reverse=True)
        for p in term[LOGS_CONSERVAR:]:
            p.unlink(missing_ok=True)
    except Exception:
        pass


def _log_finalizado(p: Path) -> bool:
    try:
        with open(p, "rb") as f:
            f.seek(max(0, p.stat().st_size - 400))
            return b"status=" in f.read()
    except Exception:
        return True


class _LockWorkspace:
    """Lock del workspace: impide que DOS corridas escriban el mismo outdir. Archivo con
    PID; stale si el PID no vive (psutil si está; si no, edad > 12 h)."""

    def __init__(self, work: Path):
        self.path = work / "lock"
        self.tomado = False

    def adquirir(self) -> str | None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        for _ in range(2):
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(fd, str(os.getpid()).encode())
                os.close(fd)
                self.tomado = True
                return None
            except FileExistsError:
                if self._stale():
                    self.path.unlink(missing_ok=True)
                    continue
                return f"otra corrida está usando este destino ({self.path})"
        return f"no pude tomar el lock ({self.path})"

    def _stale(self) -> bool:
        try:
            pid = int(self.path.read_text())
        except Exception:
            return True
        try:
            import psutil
            return not psutil.pid_exists(pid)
        except Exception:
            try:
                os.kill(pid, 0)
                return False
            except (ProcessLookupError, PermissionError, OSError):
                return time.time() - self.path.stat().st_mtime > 12 * 3600
        return False

    def soltar(self):
        if self.tomado:
            self.path.unlink(missing_ok=True)
            self.tomado = False

    def libre(self) -> bool:
        return not self.path.exists() or self._stale()


def estado_previo(outdir) -> dict | None:
    """Para el banner «Reanudar» de la GUI: lee pipeline.state.json si existe."""
    try:
        return _leer_json(Path(outdir) / "pipeline.state.json")
    except Exception:
        return None


def correr(spec: dict, event_cb=None, cancel=None) -> dict:
    """Corre el pipeline. `spec`: {fuente, outdir, roles{voz,juego,...}, rects{juego,camara},
    config{transcripcion,voz,fondo,cara,master}}. `event_cb(dict)` recibe eventos
    {"tipo": "log"|"prog"|"paso"|"fin", ...}. Devuelve el REPORTE:
    {run_id, status, pasos: [{id, etiqueta, status, motivo, dur}], outputs}."""
    import jobs
    import models

    ctx = _Ctx(spec, event_cb, cancel)
    reporte = {"run_id": ctx.run_id, "status": "failed", "pasos": [], "outputs": {}}

    # ---------- preflight ----------
    ctx.outdir.mkdir(parents=True, exist_ok=True)
    try:
        probe = ctx.outdir / ".escribible"
        probe.write_text("x"); probe.unlink()
    except Exception as e:
        raise RuntimeError(f"el destino no es escribible: {e}")
    if not medios.archivo_estable(ctx.fuente):
        raise RuntimeError("el archivo fuente sigue cambiando de tamaño (¿se está copiando?) "
                           "— esperá a que termine y reintentá")
    ctx.info = medios.inspeccionar(ctx.fuente)
    ctx.fp = medios.fingerprint(ctx.fuente, ctx.info)
    libre = __import__("shutil").disk_usage(ctx.outdir).free
    if libre < 2 * ctx.fuente.stat().st_size * 0.15 + 500 * 1024 * 1024:
        ctx.log("⚠ Poco espacio libre en el destino — el run puede fallar a mitad.")
    source_id = ctx.fp["hash_muestreado"][:12]
    ctx.work = ctx.outdir / ".pipeline-work" / source_id

    lock = _LockWorkspace(ctx.work)
    motivo_lock = lock.adquirir()
    if motivo_lock:
        raise RuntimeError(motivo_lock)
    try:
        return _correr_con_lock(ctx, spec, cancel, reporte, lock)
    finally:
        # el lock se suelta SIEMPRE — también si falla abrir el log o guardar el estado
        # ANTES de arrancar (ronda 3, h.12: quedaba retenido con el PID vivo)
        lock.soltar()


def _snapshot_marcas(ctx: _Ctx, estricto: bool = False):
    """Congela las MARCAS DEL AUTOR como ENTRADA EXTERNA del run (consenso
    timeline-marcas r2 h.1): se leen los bytes UNA vez al arrancar, se validan
    identidad + contrato temporal, y consolidar consume SOLO esta copia inmutable —
    si el usuario edita marcas durante el run, entran recién en la corrida siguiente
    (el sha en entradas_extra invalida consolidar+vistas y nada más).
    `estricto` (derivados — review impl r1.2): un ERROR al resolver o congelar
    ABORTA (que no exista candidato sigue siendo válido: cero marcas); en un run
    normal las marcas son opcionales y el run sigue avisando."""
    ctx.marcas = None
    try:
        import marcas as marcas_mod
        # estricto también en resolver (review r2/r1.2): un sidecar ILEGIBLE o
        # malformado lanza — sin esto volvía None, indistinguible de «sin marcas»
        r = marcas_mod.resolver(ctx.fuente, ctx.fp, ctx.info["duracion"],
                                estricto=estricto)
    except Exception as e:
        if estricto:
            raise RuntimeError(f"no pude leer las marcas del autor: {e}")
        ctx.log(f"⚠ No pude leer las marcas del autor: {e} — el run sigue sin ellas.")
        return
    if not r:
        return
    # en un run normal el snapshot NUNCA tumba el run (entrada OPCIONAL): retry de
    # os.replace (lock transitorio de AV/indexador en Windows) y, si aun así falla,
    # se sigue sin marcas avisando (auditoría Windows pre-ship, h.1)
    try:
        sha = hashlib.sha256(r["bytes"]).hexdigest()
        snap = ctx.work / "entradas" / f"marcas-{sha[:12]}.json"
        snap.parent.mkdir(parents=True, exist_ok=True)
        if not snap.exists():
            tmp = snap.with_name(snap.name + ".tmp")
            tmp.write_bytes(r["bytes"])
            _replace_retry(tmp, snap)
    except Exception as e:
        if estricto:
            raise RuntimeError(f"no pude congelar la snapshot de marcas: {e}")
        ctx.log(f"⚠ No pude congelar la snapshot de marcas: {e} — el run sigue sin ellas.")
        return
    ctx.marcas = {"sha": sha, "snapshot": snap, "n": len(r["marcas"]),
                  "cuarentena": r["cuarentena"], "origen": r["origen"]}
    ctx.log(f"Marcas del autor: {len(r['marcas'])} válida(s) ({r['origen']}, "
            f"rev {r['revision']}) → snapshot {snap.name}")
    for c in r["cuarentena"]:
        # la entrada en cuarentena puede NO ser un dict (sidecar malformado — impl h.3)
        mid = c["marca"].get("id", "?") if isinstance(c["marca"], dict) else repr(c["marca"])[:30]
        ctx.log(f"  ⚠ cuarentena {mid}: {c['motivo']} — no entra al master")


def _sin_secretos(obj):
    """Copia profunda del spec SIN claves de API (cualquier clave `*_key`/`api_key`):
    spec.resuelto.json queda en el work dir y no debe filtrar credenciales."""
    if isinstance(obj, dict):
        return {k: _sin_secretos(v) for k, v in obj.items()
                if not (isinstance(k, str) and (k.endswith("_key") or k == "api_key"))}
    if isinstance(obj, list):
        return [_sin_secretos(v) for v in obj]
    return obj


def _persistir_spec(ctx: _Ctx):
    """Persiste el spec RESUELTO del run (sin secretos) en el workspace: es lo que
    permite a `actualizar_derivados()` re-correr consolidar+vistas sin que la tab
    «Marcar» tenga que reconstruir roles/rects/config (diseño tab-marcar h.4).
    Nunca tumba el run: sin spec persistido solo se pierde esa operación."""
    try:
        _escribir_atomico(ctx.work / "spec.resuelto.json",
                          json.dumps(_sin_secretos(ctx.spec), ensure_ascii=False, indent=1))
    except Exception as e:
        ctx.log(f"⚠ No pude persistir spec.resuelto.json: {e} — «Preparar paquete» de la "
                "tab Marcar no estará disponible para esta corrida.")


class SpecNoDisponible(RuntimeError):
    """No hay spec.resuelto.json (corrida anterior a 2026-07-21 o persistencia fallida):
    la tab «Marcar» debe declarar el paquete stale y sugerir re-correr el wizard."""


def actualizar_derivados(outdir, event_cb=None, cancel=None, fuente=None) -> dict:
    """Re-corre SOLO consolidar+vistas sobre los manifests ya committeados, con una
    snapshot NUEVA de las marcas del autor (tab «Marcar» → «Preparar paquete»: el
    master/vistas absorben las marcas post-extracción sin re-análisis pesado).
    Diseño consensuado: three-brain-out/2026-07-21-tab-marcar (h.4 + r3.3).
    `fuente` (opcional): la ruta ACTUAL del medio según quien llama (la tab Marcar
    pasa el video cargado). El path del estado es solo un LOCALIZADOR — si el video
    se movió desde la extracción, se usa el localizador provisto; la identidad la
    valida SIEMPRE el fingerprint (mismo principio que marcas.py).
    Lanza SpecNoDisponible (legacy), RuntimeError (medio cambiado / artefactos rotos /
    workspace ocupado). NUNCA llamar desde el hilo de Tk."""
    outdir = Path(outdir)
    state = estado_previo(outdir)
    if not state or not (state.get("fuente") or {}).get("path"):
        raise SpecNoDisponible("esa carpeta no tiene pipeline.state.json")
    if state.get("status") == "running":
        # review impl r1.1: un run interrumpido deja estados a medias — reconstruir
        # derivados sobre eso puede degradar el master en silencio. Que lo reanude
        # el wizard primero.
        raise RuntimeError("la última corrida del pipeline quedó en curso/interrumpida — "
                           "reanudala (o re-correla) desde el wizard antes de preparar "
                           "el paquete")
    ruta_state = Path(state["fuente"]["path"])
    cands = ([Path(fuente)] if fuente else []) + [ruta_state]
    fuente = next((c for c in cands if c.exists() and c.is_file()), None)
    if fuente is None:
        raise RuntimeError("no encuentro el medio fuente: ni la ruta actual"
                           + (f" ({cands[0]})" if len(cands) > 1 else "")
                           + f" ni la del estado ({ruta_state}) existen")
    source_id = (state["fuente"].get("hash_muestreado") or "")[:12]
    if not source_id:
        raise SpecNoDisponible("el estado no tiene fingerprint de la fuente")
    work = outdir / ".pipeline-work" / source_id
    try:
        spec = _leer_json(work / "spec.resuelto.json")
    except Exception:
        raise SpecNoDisponible("este proyecto no tiene spec.resuelto.json (corrida "
                               "anterior al 2026-07-21) — re-corré el wizard para "
                               "regenerarlo (reusa todo lo ya calculado)")
    if not isinstance(spec, dict) or not spec.get("fuente") or not spec.get("outdir"):
        raise SpecNoDisponible("spec.resuelto.json malformado")
    spec = dict(spec, rehacer=False)
    ctx = _Ctx(spec, event_cb, cancel)
    ctx.outdir = outdir                         # el outdir REAL manda (pudo moverse)
    ctx.fuente = fuente
    ctx.info = medios.inspeccionar(fuente)
    ctx.fp = medios.fingerprint(fuente, ctx.info)
    for k in ("size", "hash_muestreado", "inventario_sha256"):
        if ctx.fp.get(k) != state["fuente"].get(k):
            raise RuntimeError("el video cambió desde la extracción (fingerprint "
                               "distinto) — hay que re-correr el pipeline completo")
    ctx.work = work
    lock = _LockWorkspace(ctx.work)
    motivo = lock.adquirir()
    if motivo:
        raise RuntimeError(motivo)
    try:
        return _derivados_con_lock(ctx)
    finally:
        lock.soltar()


def _derivados_con_lock(ctx: _Ctx) -> dict:
    _rotar_logs()
    LOGS_DIR.mkdir(exist_ok=True)
    log_path = LOGS_DIR / f"derivados-{ctx.run_id}.log"
    ctx._logf = open(log_path, "a", encoding="utf-8")
    try:
        return _derivados_cuerpo(ctx, log_path)
    finally:
        # cierre GARANTIZADO del log ante CUALQUIER salida (review r2 nuevo-2)
        if ctx._logf:
            try:
                ctx._logf.close()
            except Exception:
                pass
            ctx._logf = None


def _derivados_cuerpo(ctx: _Ctx, log_path) -> dict:
    reporte = {"run_id": ctx.run_id, "status": "failed", "pasos": [], "outputs": {}}

    # snapshot NUEVA (r3.3); un ERROR acá ABORTA — jamás un master sin autor.marcas
    # «porque falló leer» (review impl r1.2)
    _snapshot_marcas(ctx, estricto=True)

    pasos = _pasos()
    derivados = [p for p in pasos if p["id"] in ("consolidar", "vistas")]
    upstream = [p for p in pasos if p["id"] not in ("consolidar", "vistas")]
    prev_pasos = (estado_previo(ctx.outdir) or {}).get("pasos") or {}
    ctx.state = {"schema": SCHEMA_STATE, "run_id": ctx.run_id, "status": "derivados",
                 "fuente": {"path": str(ctx.fuente), **ctx.fp},
                 "spec_hash": _json_hash({k: ctx.spec.get(k)
                                          for k in ("roles", "rects", "config")}),
                 "log": str(log_path),
                 "pasos": {p["id"]: dict(prev_pasos.get(p["id"])
                                         or {"status": PENDIENTE}) for p in pasos}}

    # upstream, en ORDEN TOPOLÓGICO (review impl r1.1): lo que terminó bien en el run
    # anterior se re-valida CONTRA EL PLAN (versión + params del spec persistido +
    # entradas encadenadas + artefactos con SHA SIEMPRE + aliases) y se marca REUSED;
    # cualquier regresión = error DURO — jamás un master degradado en silencio. Los
    # skip/failed del run original se conservan tal cual: consolidar reproduce las
    # mismas ausencias que el master original ya declaraba.
    for p in upstream:
        est = ctx.state["pasos"][p["id"]]
        if est.get("status") not in (OK, REUSED, PARTIAL):
            continue
        motivo = _reuso_derivados(ctx, p)
        if motivo:
            raise RuntimeError(f"«{p['etiqueta'].strip()}» no se puede reusar "
                               f"({motivo}) — re-corré el pipeline desde el wizard")
        est["status"] = REUSED

    for p in derivados:
        ctx._pesos[p["id"]] = 1.0
    ctx._peso_total = float(len(derivados))

    resultado = "ok"
    for paso in derivados:
        pid = paso["id"]
        ctx._paso_actual = pid
        if "alias_fn" in paso:
            paso = dict(paso); paso["alias"] = paso["alias_fn"](ctx)
        est = ctx.state["pasos"][pid]

        def _fin(status, motivo="", dur_s=0.0):
            est.update(status=status, motivo=motivo, dur=round(dur_s, 1))
            ctx.event_cb({"tipo": "paso", "id": pid, "etiqueta": paso["etiqueta"],
                          "status": status, "motivo": motivo})
            _guardar_estado(ctx)

        if ctx.cancelado():
            _fin(CANCELLED, "cancelado")
            resultado = "cancelled"
            continue
        okdep, motivo_dep = _deps_ok(ctx, paso)
        if not okdep:
            _fin(SKIP_UNAVAIL, motivo_dep)
            resultado = "failed"
            continue
        if _puede_reusar(ctx, paso):
            ctx.log(f"✓ reusado: {paso['etiqueta']} (las marcas no cambiaron)", pid)
            ctx.prog_paso(1.0)
            ctx._peso_hecho += 1.0
            _fin(REUSED)
            continue
        ctx.log(f"── {paso['etiqueta']} ──", pid)
        ctx.event_cb({"tipo": "paso", "id": pid, "etiqueta": paso["etiqueta"],
                      "status": "running", "motivo": ""})
        t0 = time.time()
        staging = ctx.dir_staging(pid, ctx.run_id)
        try:
            arte, extra = paso["run"](ctx, staging, lambda m: ctx.log(m, pid), ctx.prog_paso)
            _commit_paso(ctx, paso, staging, arte, extra, t0,
                         paso["unidades"](ctx), paso["modelo"](ctx))
            ctx.prog_paso(1.0)
            ctx._peso_hecho += 1.0
            _fin(OK, "", time.time() - t0)
        except Exception as e:
            tb = traceback.format_exc(limit=6)
            ctx.log(f"✗ {paso['etiqueta']}: {e}", pid)
            if ctx._logf:
                ctx._logf.write(tb + "\n")
            _fin(FAILED, str(e)[:400], time.time() - t0)
            resultado = "failed"

    ctx.state["status"] = resultado
    _guardar_estado(ctx)
    reporte["status"] = resultado
    for p in derivados:
        e = ctx.state["pasos"][p["id"]]
        reporte["pasos"].append({"id": p["id"], "etiqueta": p["etiqueta"],
                                 "status": e.get("status"), "motivo": e.get("motivo", ""),
                                 "dur": e.get("dur", 0)})
    nombre = _nombre_master(ctx)
    reporte["outputs"] = {"outdir": str(ctx.outdir),
                          "master": str(ctx.alias(f"{nombre}.master.json")),
                          "vistas": str(ctx.outdir / "vistas"), "log": str(log_path)}
    if ctx._logf:
        ctx._logf.write(f"status={resultado}\n")   # el cierre lo garantiza el finally
    ctx.event_cb({"tipo": "fin", "reporte": reporte})
    return reporte


def _reuso_derivados(ctx: _Ctx, paso: dict) -> str | None:
    """None si el paso upstream es reusable para «Preparar paquete»; si no, el MOTIVO.
    Es _puede_reusar con dos diferencias DELIBERADAS (review impl r1.1/r1.13):
    · sin el gate `reuso_valido` (visión PARTIAL es consumible: acá no se reintenta
      nada, se consume lo committeado — igual que hizo el consolidar original);
    · el SHA se verifica SIEMPRE, también en artefactos gigantes (acá prima la
      integridad sobre la velocidad)."""
    m = ctx.manifest(paso["id"])
    if not m:
        return "sin manifest"
    if m.get("version") != paso.get("version", 1):
        return "la versión del paso cambió"
    if m.get("params_hash") != _params_hash(ctx, paso):
        return "parámetros distintos al spec persistido"
    if m.get("entradas_hash") != _entradas_hash(ctx, paso):
        return "sus entradas cambiaron"
    gen = ctx.work / m.get("generacion", "?")
    for rel, info in (m.get("artefactos") or {}).items():
        p = gen / rel
        if not p.exists() or p.stat().st_size != info.get("size"):
            return f"falta o cambió {rel}"
        if medios.hash_archivo(p) != info.get("sha256"):
            return f"sha distinto en {rel}"
    # los ALIASES también con sha SIEMPRE (r2 sobre r1.13: consolidar consume los
    # aliases del outdir, no la generación — un flac corrupto de igual tamaño no
    # puede pasar); rotos → re-sync desde la generación ya verificada y re-chequear
    if not _aliases_integros(ctx, m):
        _sync_aliases(ctx, m)
        if not _aliases_integros(ctx, m):
            return "aliases rotos"
    return None


def _aliases_integros(ctx: _Ctx, man: dict) -> bool:
    """Como _aliases_ok pero con sha SIEMPRE, también en gigantes (solo para
    «Preparar paquete», donde prima integridad sobre velocidad)."""
    for rel, destino in (man.get("alias") or {}).items():
        info = man["artefactos"].get(rel)
        dst = ctx.alias(destino)
        if info is None or not dst.exists() or dst.stat().st_size != info["size"]:
            return False
        if medios.hash_archivo(dst) != info["sha256"]:
            return False
    return True


def _correr_con_lock(ctx: _Ctx, spec, cancel, reporte, lock) -> dict:
    import jobs
    import models

    # ---------- estado + log ----------
    prev = estado_previo(ctx.outdir) or {}
    interrumpido_previo = prev.get("status") == "running"
    _rotar_logs()
    LOGS_DIR.mkdir(exist_ok=True)
    log_path = LOGS_DIR / f"extraer-{ctx.run_id}.log"
    ctx._logf = open(log_path, "a", encoding="utf-8")

    _snapshot_marcas(ctx)
    _persistir_spec(ctx)

    pasos = _pasos()
    ctx.state = {"schema": SCHEMA_STATE, "run_id": ctx.run_id, "status": "running",
                 "fuente": {"path": str(ctx.fuente), **ctx.fp},
                 "spec_hash": _json_hash({k: spec.get(k) for k in ("roles", "rects", "config")}),
                 "log": str(log_path),
                 "pasos": {p["id"]: {"status": PENDIENTE} for p in pasos}}
    _guardar_estado(ctx)
    if interrumpido_previo:
        ctx.log(f"⚠ La corrida anterior ({prev.get('run_id')}) quedó interrumpida — "
                "retomo desde lo committeado.")

    # ---------- pesos ----------
    dur = ctx.info["duracion"]
    for p in pasos:
        pid = p["id"]
        tasa = _hist_tasa(pid, p["modelo"](ctx))
        if pid == "fondo":
            # el paso físico es "fondo" pero los priors viven por capa: sumar las pedidas
            seg = sum(PESOS[f"fondo_{c}"][0] for c in _fondo_capas_pedidas(ctx)) or 0.1
            fijo = sum(PESOS[f"fondo_{c}"][1] for c in _fondo_capas_pedidas(ctx)) or 1
        else:
            seg, fijo = PESOS.get(pid, (0.5, 5))
        if pid in ("transcribir_voz", "fondo") and _cfg(ctx, "transcripcion", "device", "auto") == "cpu":
            seg *= PESO_CPU_TRANSCRIBIR
        estim = (tasa * p["unidades"](ctx)) if tasa else (seg * dur + fijo)
        ctx._pesos[pid] = max(estim, 0.1)

    ctx._peso_total = sum(ctx._pesos[p["id"]] for p in pasos if p["pedido"](ctx)) or 1.0

    def _peso_descartar(pid):
        """Un paso que NO va a correr (skip/fail/cancel) sale del denominador — la barra
        total representa solo el trabajo que de verdad queda (ronda 3, h.14)."""
        ctx._peso_total = max(ctx._peso_total - ctx._pesos.get(pid, 0), ctx._peso_hecho, 0.001)

    # ---------- ejecución ----------
    resultado_global = "ok"
    try:
        with jobs.heavy(ctx.log, cancel=cancel):
            for paso in pasos:
                pid = paso["id"]
                ctx._paso_actual = pid
                if "alias_fn" in paso:
                    paso = dict(paso); paso["alias"] = paso["alias_fn"](ctx)
                est = ctx.state["pasos"][pid]

                def _fin_paso(status, motivo="", dur_s=0.0, extra=None):
                    est.update(status=status, motivo=motivo, dur=round(dur_s, 1))
                    if extra:
                        est["extra"] = extra
                    ctx.event_cb({"tipo": "paso", "id": pid, "etiqueta": paso["etiqueta"],
                                  "status": status, "motivo": motivo})
                    _guardar_estado(ctx)

                if ctx.cancelado():
                    _fin_paso(CANCELLED, "corrida cancelada")
                    _peso_descartar(pid)
                    resultado_global = "cancelled"
                    continue
                if not paso["pedido"](ctx):
                    _fin_paso(SKIP_NOREQ, "no se pidió")
                    continue                       # nunca estuvo en el denominador
                okdep, motivo_dep = _deps_ok(ctx, paso)
                if not okdep:
                    _fin_paso(SKIP_UNAVAIL, motivo_dep)
                    _peso_descartar(pid)
                    continue
                disp, motivo = paso["disponible"](ctx)
                if not disp:
                    _fin_paso(SKIP_UNAVAIL, motivo)
                    _peso_descartar(pid)
                    continue
                if _puede_reusar(ctx, paso):
                    ctx.log(f"✓ reusado: {paso['etiqueta']} (sin cambios desde la última corrida)", pid)
                    # ORDEN: primero pintar el 100% del paso (con el peso aún como
                    # "actual"), después moverlo a hecho — sumar antes lo contaba DOBLE
                    ctx.prog_paso(1.0)
                    ctx._peso_hecho += ctx._pesos.get(pid, 0)
                    _fin_paso(REUSED)
                    if pid == "fondo":             # nodos lógicos también al reusar
                        _reportar_nodos_fondo(ctx, paso, (ctx.manifest(pid) or {}).get("extra"), est)
                    continue

                ctx.log(f"── {paso['etiqueta']} ──", pid)
                ctx.event_cb({"tipo": "paso", "id": pid, "etiqueta": paso["etiqueta"],
                              "status": "running", "motivo": ""})
                t0 = time.time()
                staging = ctx.dir_staging(pid, ctx.run_id)
                try:
                    models.free_if_tight(log=lambda m: ctx.log(m, pid))
                    arte, extra = paso["run"](ctx, staging,
                                              lambda m: ctx.log(m, pid), ctx.prog_paso)
                    _commit_paso(ctx, paso, staging, arte, extra, t0,
                                 paso["unidades"](ctx), paso["modelo"](ctx))
                    ctx.prog_paso(1.0)
                    ctx._peso_hecho += ctx._pesos.get(pid, 0)
                    estado, motivo_f = (paso["estado_final"](ctx, extra)
                                        if "estado_final" in paso else (OK, ""))
                    _fin_paso(estado, motivo_f, dur_s=time.time() - t0,
                              extra=extra if extra else None)
                    _reportar_nodos_fondo(ctx, paso, extra, est)
                    if estado == FAILED and resultado_global == "ok":
                        resultado_global = "failed_parcial"
                except InterruptedError:
                    _fin_paso(CANCELLED, "cancelado por el usuario", time.time() - t0)
                    _peso_descartar(pid)
                    resultado_global = "cancelled"
                except Exception as e:
                    tb = traceback.format_exc(limit=6)
                    ctx.log(f"✗ {paso['etiqueta']}: {e}", pid)
                    if ctx._logf:
                        ctx._logf.write(tb + "\n")
                    _fin_paso(FAILED, str(e)[:400], time.time() - t0)
                    _peso_descartar(pid)
                    if resultado_global == "ok":
                        resultado_global = "failed_parcial"
    except InterruptedError:
        # cancelado MIENTRAS esperaba el turno de trabajo pesado (jobs.heavy cancelable):
        # es un run CANCELADO limpio, no un error fatal — estado y footer se escriben igual.
        resultado_global = "cancelled"
        ctx.log("⏹ Cancelado antes de arrancar (esperaba el turno de trabajo pesado).")
    finally:
        try:
            models.unload_all(log=ctx.log)         # SIEMPRE descargar al terminar (C8/P6)
        except Exception:
            pass

    # ---------- reporte ----------
    if ctx.cancelado():
        resultado_global = "cancelled"
    ctx.state["status"] = resultado_global
    _guardar_estado(ctx)
    reporte["status"] = resultado_global
    for p in pasos:
        e = ctx.state["pasos"][p["id"]]
        reporte["pasos"].append({"id": p["id"], "etiqueta": p["etiqueta"],
                                 "status": e.get("status"), "motivo": e.get("motivo", ""),
                                 "dur": e.get("dur", 0)})
        if e.get("nodos"):
            for nid, n in e["nodos"].items():
                reporte["pasos"].append({"id": nid, "etiqueta": f"  · {nid.replace('_', ' ')}",
                                         "status": n.get("status"), "motivo": n.get("motivo", ""),
                                         "dur": 0})
    nombre = _nombre_master(ctx)
    reporte["outputs"] = {"outdir": str(ctx.outdir),
                          "master": str(ctx.alias(f"{nombre}.master.json")),
                          "vistas": str(ctx.outdir / "vistas"), "log": str(log_path)}
    if ctx._logf:
        ctx._logf.write(f"status={resultado_global}\n")
        ctx._logf.close()
        ctx._logf = None
    ctx.event_cb({"tipo": "fin", "reporte": reporte})
    return reporte


def _dep_lista(ctx: _Ctx, dep: str) -> bool:
    """Una dependencia está lista SOLO si en ESTA corrida terminó ok/reused/partial. Un
    manifest viejo NO alcanza: si la dep falló o se desmarcó ahora, sus outputs anteriores
    no son entradas válidas del plan actual (review ronda 3, h.2 BLOQUEANTE). PARTIAL es
    consumible por diseño (visión: los chunks ok valen aunque falten otros)."""
    return ctx.state["pasos"].get(dep, {}).get("status") in (OK, REUSED, PARTIAL)


def _deps_ok(ctx: _Ctx, paso) -> tuple[bool, str]:
    """Dependencias satisfechas EN ESTA CORRIDA. `requiere_alguna` = basta una."""
    deps = paso.get("requiere", [])
    if not deps:
        return True, ""
    hechas = [d for d in deps if _dep_lista(ctx, d)]
    if paso.get("requiere_alguna"):
        return (True, "") if hechas else (False, f"ninguna de sus entradas está lista "
                                                 f"({', '.join(deps)})")
    faltan = [d for d in deps if d not in hechas]
    return (True, "") if not faltan else (False, f"depende de: {', '.join(faltan)}")


def _reportar_nodos_fondo(ctx: _Ctx, paso, extra, est):
    """Convierte el resultado por capa de fondo.py en los tres nodos lógicos del reporte
    (consenso C6: PROHIBIDO inferir por claves presentes — fondo.py lo declara)."""
    if paso["id"] != "fondo":
        return
    capas = (extra or {}).get("capas") or {}
    nodos = {}
    for capa, nid in (("dialogo", "fondo_dialogo"), ("sonidos", "fondo_sonidos"),
                      ("descripcion", "fondo_descripcion")):
        r = capas.get(capa)
        if r is None:
            nodos[nid] = {"status": SKIP_NOREQ, "motivo": "no se pidió"}
        else:
            nodos[nid] = {"status": {"ok": OK, "failed": FAILED, "skipped": SKIP_UNAVAIL,
                                     "cancelled": CANCELLED}.get(r.get("status"), FAILED),
                          "motivo": r.get("reason", "")}
    est["nodos"] = nodos
