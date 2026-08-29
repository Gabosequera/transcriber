#!/usr/bin/env python3
"""
marcas.py — MARCAS DEL AUTOR sobre el timeline: puntos y regiones con prompts para la
AI que edita, creadas desde el mini-editor del wizard ANTES de extraer la metadata.

Diseño consensuado con Codex gpt-5.6-sol (3 rondas, READY):
three-brain-out/2026-07-16-timeline-marcas/DISENO-final.md.

Modelo (sidecar JSON, schema 1):
  · marca PUNTO :  {id, tipo:"punto",  t,            decision:null,           prompt?, creado}
  · marca REGIÓN: {id, tipo:"region", t_ini, t_fin, decision:null|"incluir"|"excluir",
                    prompt?, creado}
  Ejes SEPARADOS: `tipo` es geometría; `decision` es la orden de corte (keep/remove);
  `prompt` es la directiva editorial — cualquier combinación válida. REGLA DURA: una
  decision no-null exige región con t_fin−t_ini ≥ 0.2 s (un punto es siempre anchor
  blando; "no usar exactamente este instante" no corta nada de una EDL continua).

Persistencia (dos candidatos, gana la REVISIÓN):
  · preferido: `<video sin extensión>.marcas.json` (hermano del video);
  · fallback:  `<app>/marcas_store/<hash_muestreado completo>.marcas.json` (carpeta del
    video de solo lectura / en red). Al CARGAR se evalúan SIEMPRE ambos, se valida la
    identidad COMPLETA (size + hash_muestreado + inventario_sha256) y entre los válidos
    gana el de mayor `revision` (empate → sidecar). Al GUARDAR se intenta el sidecar y
    si falla se usa el store; si el sidecar anda y el store existe, se ESPEJA (mismos
    bytes) para que nunca diverjan hacia atrás.

Contrato temporal ESTRICTO (sin clamp silencioso): números finitos, 0 ≤ t ≤ dur,
t_fin > t_ini. Lo que no cumple va a CUARENTENA con motivo (queda en el archivo, no
entra al master). timebase declarada: "media_elapsed_v1" (segundos desde el inicio
canónico T0 del medio — la misma línea de tiempo de todos los sidecars).

Los IDs `m0001…` son la identidad CANÓNICA (contador next_id, nunca se reusa); el
master deriva `MK0001` del mismo número — consolidar.py NO los renumera.
"""
from __future__ import annotations

import contextlib
import json
import math
import os
import re
from datetime import datetime, timezone
from pathlib import Path

import app_paths

SCHEMA = 1
TIMEBASE = "media_elapsed_v1"
DECISIONES = (None, "incluir", "excluir")
MIN_REGION_DECISION = 0.2      # s — duración mínima de una región con decisión de corte
STORE_DIR = app_paths.MARKS_STORE_DIR
_RE_ID = re.compile(r"^m\d{4,}$")


# ------------------------------------------------------------------------ rutas --
def sidecar_path(video) -> Path:
    v = Path(video)
    return v.parent / f"{v.stem}.marcas.json"


def store_path(fp: dict) -> Path:
    return STORE_DIR / f"{fp['hash_muestreado']}.marcas.json"


# -------------------------------------------------------------------- validación --
def _fin(x) -> bool:
    # bool es subtipo de int: True como timestamp NO es un tiempo (review impl h.7)
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


def validar_marca(m: dict, dur: float) -> str | None:
    """None si la marca cumple el contrato; si no, el MOTIVO de cuarentena."""
    if not isinstance(m, dict):
        return "no es un objeto"
    if not _RE_ID.match(str(m.get("id", ""))):
        return f"id inválido ({m.get('id')!r})"
    tipo = m.get("tipo")
    dec = m.get("decision")
    if dec not in DECISIONES:
        return f"decision desconocida ({dec!r})"
    if m.get("prompt") is not None and not isinstance(m["prompt"], str):
        return "prompt no es texto"
    if tipo == "punto":
        if not _fin(m.get("t")):
            return "t no es un número finito"
        if not (0.0 <= m["t"] <= dur):
            return f"fuera_de_rango (t={m['t']:.2f}, dur={dur:.2f})"
        if dec is not None:
            return "un punto no puede llevar decisión de corte (solo regiones)"
        return None
    if tipo == "region":
        if not (_fin(m.get("t_ini")) and _fin(m.get("t_fin"))):
            return "t_ini/t_fin no son números finitos"
        if not (m["t_fin"] > m["t_ini"]):
            return "t_fin debe ser mayor que t_ini"
        if not (0.0 <= m["t_ini"] and m["t_fin"] <= dur):
            return f"fuera_de_rango ([{m['t_ini']:.2f}–{m['t_fin']:.2f}], dur={dur:.2f})"
        if dec is not None and (m["t_fin"] - m["t_ini"]) < MIN_REGION_DECISION:
            return f"región con decisión demasiado corta (<{MIN_REGION_DECISION}s)"
        return None
    return f"tipo desconocido ({tipo!r})"


def _identidad_ok(doc: dict, fp: dict) -> bool:
    f = doc.get("fuente") or {}
    return (f.get("size") == fp.get("size")
            and f.get("hash_muestreado") == fp.get("hash_muestreado")
            and f.get("inventario_sha256") == fp.get("inventario_sha256"))


def _entero(x) -> bool:
    return isinstance(x, int) and not isinstance(x, bool)


def _doc_valido(doc) -> bool:
    """Estructura del documento (review impl h.3/h.7 + r2.2): un sidecar malformado
    (marcas que no es lista, timebase ajena, fuente ausente, revision/next_id no
    enteros) se descarta ENTERO como candidato — nunca debe poder abortar el pipeline
    ni colar tiempos con otra base. Los campos son OBLIGATORIOS, sin defaults."""
    return (isinstance(doc, dict) and doc.get("schema") == SCHEMA
            and doc.get("timebase") == TIMEBASE
            and isinstance(doc.get("marcas"), list)
            and isinstance(doc.get("fuente"), dict)
            and _entero(doc.get("revision"))
            and _entero(doc.get("next_id")))


def _leer(path: Path) -> dict | None:
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
        return doc if _doc_valido(doc) else None
    except Exception:
        return None


def _validar_lista(items, dur: float) -> tuple[list[dict], list[dict]]:
    """(válidas, cuarentena) de una lista de marcas: contrato temporal + IDs ÚNICOS
    (un id repetido rompería la estabilidad de los MK#### — review impl h.7)."""
    marcas, cuarentena, vistos = [], [], set()
    for m in items or []:
        err = validar_marca(m, dur)
        if err is None and m["id"] in vistos:
            err = f"id duplicado ({m['id']})"
        if err:
            cuarentena.append({"marca": m, "motivo": err})
        else:
            vistos.add(m["id"])
            marcas.append(m)
    return marcas, cuarentena


# ---------------------------------------------------------------------- Registro --
class Registro:
    """El estado de marcas de UN video, con carga/guardado resueltos. La UI lo usa
    directo (agregar/editar/borrar guardan al toque); el pipeline usa `resolver()`."""

    def __init__(self, video, fp: dict, dur: float):
        self.video, self.fp, self.dur = Path(video), fp, float(dur)
        self.marcas: list[dict] = []           # solo las VÁLIDAS
        self.cuarentena: list[dict] = []       # [{marca, motivo}]
        self.avisos: list[str] = []            # para el banner de la UI
        self.origen: str | None = None         # "sidecar" | "store" | None
        self.next_id = 1
        self.revision = 0
        # ---- instancia COMPARTIDA entre tabs (diseño tab-marcar h.1/r3/r4) ----
        self.suscriptores: list = []           # callbacks() en el hilo Tk, post-persistencia
        self.rutas_secundarias: list[Path] = []       # localizadores; JAMÁS se escriben
        self.conflicto_pendiente: dict | None = None  # doc secundario divergente (misma rev)
        self.estado_error: str | None = None   # último guardado fallido (bloquea «Preparar»)
        self._cargar()

    # ---- suscripción (wizard + tab Marcar comparten ESTA instancia) ----
    def suscribir(self, cb):
        if cb not in self.suscriptores:
            self.suscriptores.append(cb)

    def desuscribir(self, cb):
        with contextlib.suppress(ValueError):
            self.suscriptores.remove(cb)

    def notificar(self):
        """SOLO tras persistir OK (consenso r2.6): las vistas redibujan del estado real."""
        for cb in list(self.suscriptores):
            try:
                cb()
            except Exception:
                pass

    @contextlib.contextmanager
    def _transaccion(self):
        """Snapshot ANTES de mutar (consenso r3): si la persistencia falla, marcas/
        cuarentena/revision vuelven (misma IDENTIDAD de objeto — la UI tiene refs a los
        dicts); `next_id` NUNCA retrocede (huecos de ID inocuos — consenso r4)."""
        refs = [(m, dict(m)) for m in self.marcas]
        lista = list(self.marcas)
        cuar = list(self.cuarentena)
        rev = self.revision
        try:
            yield
        except Exception:
            for m, contenido in refs:
                m.clear(); m.update(contenido)
            self.marcas[:] = lista
            self.cuarentena[:] = cuar
            self.revision = rev
            raise

    # ---- carga: ambos candidatos, gana la revisión (consenso r3.1) ----
    def _cargar(self):
        cands = []
        sc = _leer(sidecar_path(self.video))
        st = _leer(store_path(self.fp))
        if sc is not None:
            if _identidad_ok(sc, self.fp):
                cands.append(("sidecar", sc))
            else:
                self.avisos.append(
                    f"{sidecar_path(self.video).name} es de OTRO video (fingerprint "
                    "distinto) — quedó en cuarentena; usá «Reasociar» si corresponde.")
        if st is not None and _identidad_ok(st, self.fp):
            cands.append(("store", st))
        if not cands:
            return
        cands.sort(key=lambda c: (int(c[1].get("revision") or 0), c[0] == "sidecar"))
        self.origen, doc = cands[-1]
        self.revision = int(doc.get("revision") or 0)
        self.next_id = max(int(doc.get("next_id") or 1), 1)
        self.marcas, self.cuarentena = _validar_lista(doc.get("marcas"), self.dur)
        if self.cuarentena:
            self.avisos.append(f"{len(self.cuarentena)} marca(s) en cuarentena "
                               f"({self.cuarentena[0]['motivo']}…) — no entran al master.")
        # ids ya usados jamás se reusan, aunque next_id venga corrupto (las de
        # cuarentena pueden NO ser dicts — review impl h.3)
        todas = self.marcas + [c["marca"] for c in self.cuarentena]
        usados = [int(m["id"][1:]) for m in todas
                  if isinstance(m, dict) and _RE_ID.match(str(m.get("id", "")))]
        if usados:
            self.next_id = max(self.next_id, max(usados) + 1)

    # ---- mutaciones (la UI llama; cada una PERSISTE transaccionalmente y notifica) ----
    def agregar_punto(self, t: float, prompt: str | None = None) -> dict:
        m = {"id": f"m{self.next_id:04d}", "tipo": "punto",
             "t": round(min(max(0.0, t), self.dur), 3), "decision": None,
             "prompt": prompt or None,
             "creado": datetime.now(timezone.utc).isoformat(timespec="seconds")}
        self.next_id += 1                      # avanza aunque el guardado falle (r4)
        with self._transaccion():
            self.marcas.append(m)
            self.guardar()
        self.notificar()
        return m

    def agregar_region(self, t0: float, t1: float, decision=None,
                       prompt: str | None = None) -> dict:
        t0, t1 = sorted((min(max(0.0, t0), self.dur), min(max(0.0, t1), self.dur)))
        m = {"id": f"m{self.next_id:04d}", "tipo": "region",
             "t_ini": round(t0, 3), "t_fin": round(t1, 3), "decision": decision,
             "prompt": prompt or None,
             "creado": datetime.now(timezone.utc).isoformat(timespec="seconds")}
        err = validar_marca(m, self.dur)
        if err:
            raise ValueError(err)
        self.next_id += 1
        with self._transaccion():
            self.marcas.append(m)
            self.guardar()
        self.notificar()
        return m

    def a_region(self, m: dict, radio: float = 2.0) -> dict:
        """Convierte un PUNTO en REGIÓN [t−radio, t+radio] clampeada (regla consensuada:
        una decisión de corte exige región — la UI convierte, el modelo nunca ve un
        punto con decisión). Devuelve la MISMA marca mutada."""
        if m.get("tipo") != "punto":
            return m
        with self._transaccion():
            t = float(m.pop("t"))
            m["tipo"] = "region"
            m["t_ini"] = round(max(0.0, t - radio), 3)
            m["t_fin"] = round(min(self.dur, t + radio), 3)
            if m["t_fin"] - m["t_ini"] < MIN_REGION_DECISION:      # video ultracorto
                m["t_fin"] = min(self.dur, m["t_ini"] + MIN_REGION_DECISION)
            self.guardar()
        self.notificar()
        return m

    def editar(self, m: dict, **campos):
        with self._transaccion():
            m.update(campos)
            err = validar_marca(m, self.dur)
            if err:
                raise ValueError(err)          # la transacción restaura el contenido
            self.guardar()
        self.notificar()

    def borrar(self, m: dict):
        if m not in self.marcas:
            return
        with self._transaccion():
            self.marcas.remove(m)
            self.guardar()
        self.notificar()

    def reasociar_previa(self) -> tuple[int, int] | None:
        """(entrarían, en_cuarentena) si se reasociara el sidecar ajeno — para que la
        UI muestre el conteo ANTES de confirmar (consenso r2 q.1). El segundo número es
        TODA la cuarentena (fuera de rango, inválidas, duplicadas — el motivo exacto va
        por marca). None si no hay sidecar ajeno."""
        doc = _leer(sidecar_path(self.video))
        if doc is None or _identidad_ok(doc, self.fp):
            return None
        ok, cuar = _validar_lista(doc.get("marcas"), self.dur)
        return len(ok), len(cuar)

    def reasociar(self):
        """Adopta las marcas de un sidecar con fingerprint ajeno (decisión EXPLÍCITA del
        usuario). Las fuera de rango del video actual quedan en cuarentena."""
        doc = _leer(sidecar_path(self.video))
        if doc is None:
            return 0
        with self._transaccion():
            self.marcas, self.cuarentena = _validar_lista(doc.get("marcas"), self.dur)
            self.next_id = max(int(doc.get("next_id") or 1), self.next_id)
            self.revision = max(int(doc.get("revision") or 0), self.revision)
            todas = self.marcas + [c["marca"] for c in self.cuarentena]
            usados = [int(m["id"][1:]) for m in todas
                      if isinstance(m, dict) and _RE_ID.match(str(m.get("id", "")))]
            if usados:
                self.next_id = max(self.next_id, max(usados) + 1)
            self.guardar()
        self.notificar()
        return len(self.marcas)

    # ---- segunda ruta / adopción (diseño tab-marcar h.1 + r3 + r4 + r5) ----
    def adjuntar_ruta(self, ruta2) -> list[str]:
        """El MISMO contenido se abrió por OTRA ruta (copia física del video). La ruta
        se registra como LOCALIZADOR (jamás se escribe); su sidecar, si existe y es de
        esta identidad, se reconcilia: revisión mayor → se ADOPTA (persistido antes de
        notificar); misma revisión y bytes distintos → conflicto VISIBLE (nunca
        elección silenciosa); menor → se ignora con aviso. Devuelve avisos para la UI."""
        avisos = []
        ruta2 = Path(ruta2)
        if ruta2 == self.video:
            return avisos
        if ruta2 not in self.rutas_secundarias:
            self.rutas_secundarias.append(ruta2)
        sc2 = sidecar_path(ruta2)
        try:
            datos = sc2.read_bytes()           # UNA sola lectura (sin TOCTOU — r4)
            doc = json.loads(datos.decode("utf-8"))
        except Exception:
            return avisos                      # sin sidecar propio: solo localizador
        if not _doc_valido(doc) or not _identidad_ok(doc, self.fp):
            avisos.append(f"{sc2.name} no corresponde a este contenido — se ignora.")
            return avisos
        rev2 = int(doc.get("revision") or 0)
        if rev2 > self.revision:
            n = self.adoptar(doc)
            avisos.append(f"marcas más nuevas junto a {ruta2.name} (rev {rev2}) — "
                          f"{n} adoptada(s).")
        elif rev2 < self.revision:
            avisos.append(f"el sidecar junto a {ruta2.name} está atrás "
                          f"(rev {rev2} < {self.revision}) — gana la revisión mayor.")
        else:
            # misma revisión: ¿son los MISMOS bytes que el candidato cargado?
            propios = b""
            for p in (sidecar_path(self.video), store_path(self.fp)):
                try:
                    propios = p.read_bytes()
                    break
                except OSError:
                    continue
            if datos != propios:
                self.conflicto_pendiente = doc
                avisos.append(f"⚠ el sidecar de {ruta2.name} divergió (misma revisión "
                              f"{rev2}, contenido distinto) — se sigue con el cargado; "
                              "usá «Adoptar el de esta copia» si corresponde.")
        return avisos

    def adoptar(self, doc: dict) -> int:
        """Adopta el documento de un sidecar SECUNDARIO como estado nuevo. Es una
        mutación TRANSACCIONAL PERSISTIDA (consenso r3): el candidato se escribe al
        sidecar primario/store ANTES de tocar la memoria y de notificar — resolver()
        y el pipeline ven exactamente lo que la UI muestra. La revisión se PROMUEVE a
        max(actual, adoptada)+1 (una sola vez); next_id absorbe también el declarado
        por el doc (IDs emitidos-y-borrados jamás se reusan — consenso r5)."""
        marcas, cuar = _validar_lista(doc.get("marcas"), self.dur)
        todas = marcas + [c["marca"] for c in cuar]
        max_visto = max((int(m["id"][1:]) for m in todas
                         if isinstance(m, dict) and _RE_ID.match(str(m.get("id", "")))),
                        default=0)
        next_id = max(self.next_id, int(doc.get("next_id") or 1), max_visto + 1)
        revision = max(self.revision, int(doc.get("revision") or 0)) + 1
        candidato = {"schema": SCHEMA, "timebase": TIMEBASE,
                     "revision": revision, "next_id": next_id,
                     "fuente": {"nombre": self.video.name, "size": self.fp.get("size"),
                                "hash_muestreado": self.fp.get("hash_muestreado"),
                                "inventario_sha256": self.fp.get("inventario_sha256"),
                                "duracion": round(self.dur, 3)},
                     "actualizado": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                     "marcas": sorted(marcas,
                                      key=lambda m: float(m.get("t", m.get("t_ini", 0)))),
                     "cuarentena": cuar}
        self.guardar(candidato=candidato)      # si falla, la memoria queda INTACTA
        self.marcas, self.cuarentena = marcas, cuar
        self.revision, self.next_id = revision, next_id
        self.conflicto_pendiente = None
        self.notificar()
        return len(marcas)

    # ---- persistencia ----
    def _doc(self) -> dict:
        return {"schema": SCHEMA, "timebase": TIMEBASE,
                "revision": self.revision, "next_id": self.next_id,
                "fuente": {"nombre": self.video.name, "size": self.fp.get("size"),
                           "hash_muestreado": self.fp.get("hash_muestreado"),
                           "inventario_sha256": self.fp.get("inventario_sha256"),
                           "duracion": round(self.dur, 3)},
                "actualizado": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "marcas": sorted(self.marcas,
                                 key=lambda m: float(m.get("t", m.get("t_ini", 0)))),
                "cuarentena": self.cuarentena}

    def guardar(self, candidato: dict | None = None) -> str:
        """Persiste (revision+1) y devuelve dónde quedó: 'sidecar' o 'store'. Con
        `candidato` (adoptar) escribe ESE doc tal cual — ya trae la revisión promovida,
        sin re-incrementar (consenso r5). Si fallan AMBOS destinos la excepción sube y
        queda `estado_error` (bloquea «Preparar paquete»); las transacciones de los
        métodos de mutación restauran la memoria."""
        if candidato is None:
            self.revision += 1
            datos = json.dumps(self._doc(), ensure_ascii=False, indent=1)
        else:
            datos = json.dumps(candidato, ensure_ascii=False, indent=1)
        sp = sidecar_path(self.video)
        try:
            try:
                _escribir_atomico(sp, datos)
                destino = "sidecar"
                # espejo: si el store existe, que nunca quede con una revisión más nueva
                stp = store_path(self.fp)
                if stp.exists():
                    try:
                        _escribir_atomico(stp, datos)
                    except Exception:
                        pass
            except OSError:
                stp = store_path(self.fp)
                _escribir_atomico(stp, datos)  # si esto también falla, la excepción SUBE
                destino = "store"
                if self.origen != "store":
                    self.avisos.append(f"La carpeta del video no es escribible — las "
                                       f"marcas se guardan en {stp}.")
        except Exception as e:
            self.estado_error = f"no pude guardar las marcas: {e}"
            raise
        self.estado_error = None
        self.origen = destino
        return destino


def _escribir_atomico(path: Path, texto: str):
    """tmp + os.replace con RETRY de PermissionError (auditoría Windows pre-ship): un
    antivirus/indexador puede tener el destino abierto un instante — sin el retry, un
    lock transitorio del sidecar desviaba el guardado al store sin necesidad. Si tras
    los reintentos sigue bloqueado, la excepción SUBE (y guardar() recién ahí cae al
    store). Mismo patrón que pipeline._replace_retry."""
    import time
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(texto, encoding="utf-8")
    for i in range(4):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if i == 3:
                raise
            time.sleep(0.15 * (2 ** i))


# ------------------------------------------------ instancia COMPARTIDA entre tabs --
# (diseño tab-marcar h.1/r3: wizard y tab «Marcar» abiertas a la vez NUNCA deben tener
# dos Registros del mismo contenido pisándose los guardados)
_REGISTROS: dict[tuple, Registro] = {}


def registro_compartido(video, fp: dict, dur: float) -> tuple[Registro, list[str]]:
    """(registro, avisos): UNA instancia por identidad de CONTENIDO — la tupla completa
    (size, hash_muestreado, inventario_sha256), la misma de _identidad_ok, sin mtime.
    Mismo contenido por otra ruta → la MISMA instancia, reconciliando el sidecar de la
    ruta nueva vía adjuntar_ruta() (consenso r3)."""
    clave = (fp.get("size"), fp.get("hash_muestreado"), fp.get("inventario_sha256"))
    r = _REGISTROS.get(clave)
    if r is None:
        r = Registro(video, fp, dur)
        _REGISTROS[clave] = r
        return r, list(r.avisos)
    if Path(video) != r.video:
        return r, r.adjuntar_ruta(Path(video))
    return r, []


# ------------------------------------------------------- para el PIPELINE (snapshot) --
def resolver(video, fp: dict, dur: float, estricto: bool = False) -> dict | None:
    """Resuelve el candidato ganador SIN mutar nada (para _snapshot_marcas del pipeline):
    {bytes, marcas (válidas), cuarentena, revision, origen, path} o None si no hay.
    Los BYTES se leen UNA sola vez y todo (identidad, validación, sha de la snapshot)
    sale de ESA lectura — releer al final podía devolver una revisión distinta si la UI
    guardó en el medio (review impl h.4, el corazón del consenso h.1).
    `estricto` (derivados — review impl tab-marcar r2/r1.2): un sidecar que EXISTE pero
    no se puede leer o está malformado LANZA en vez de volverse indistinguible de
    «no hay marcas» — solo la ausencia del archivo o la identidad ajena son None."""
    cands = []
    for origen, p in (("sidecar", sidecar_path(video)), ("store", store_path(fp))):
        try:
            datos = p.read_bytes()
        except FileNotFoundError:
            continue
        except OSError as e:
            if estricto:
                raise RuntimeError(f"sidecar de marcas ilegible ({p}): {e}")
            continue
        try:
            doc = json.loads(datos)
        except Exception as e:
            if estricto:
                raise RuntimeError(f"sidecar de marcas malformado ({p}): {e}")
            continue
        if not _doc_valido(doc):
            if estricto:
                # estructura inválida en un archivo EXISTENTE: no se puede probar de
                # quién es ni qué marcas tenía — jamás «cero marcas» en silencio
                raise RuntimeError(f"sidecar de marcas con estructura inválida ({p})")
            continue
        if _identidad_ok(doc, fp):
            cands.append((int(doc.get("revision") or 0), origen == "sidecar",
                          origen, p, doc, datos))
    if not cands:
        return None
    _, _, origen, path, doc, datos = sorted(cands, key=lambda c: c[:2])[-1]
    marcas, cuarentena = _validar_lista(doc.get("marcas"), dur)
    if not marcas and not cuarentena:
        return None
    return {"bytes": datos, "marcas": marcas, "cuarentena": cuarentena,
            "revision": int(doc.get("revision") or 0), "origen": origen, "path": path}


def a_eventos(marcas: list[dict]) -> list[dict]:
    """Marcas válidas → eventos normalizados del master (stream `autor.marcas`), con el
    ID DERIVADO MK#### (misma numeración canónica — consolidar NO renumera este stream)."""
    evs = []
    for m in sorted(marcas, key=lambda m: float(m.get("t", m.get("t_ini", 0)))):
        t0 = float(m.get("t", m.get("t_ini")))
        t1 = float(m.get("t_fin", t0))
        evs.append({"id": "MK" + m["id"][1:], "t_ini": round(t0, 3), "t_fin": round(t1, 3),
                    "tipo": m["tipo"], "decision": m.get("decision"),
                    "prompt": m.get("prompt")})
    return evs


def conflictos(eventos: list[dict]) -> list[dict]:
    """Solapes incluir∩excluir (política declarada: EXCLUIR GANA en la intersección —
    acá solo se DETECTAN y publican; nunca se resuelven en silencio)."""
    incl = [e for e in eventos if e.get("decision") == "incluir"]
    excl = [e for e in eventos if e.get("decision") == "excluir"]
    out = []
    for a in incl:
        for b in excl:
            t0, t1 = max(a["t_ini"], b["t_ini"]), min(a["t_fin"], b["t_fin"])
            if t1 > t0:
                out.append({"ids": [a["id"], b["id"]], "t_ini": round(t0, 3),
                            "t_fin": round(t1, 3), "regla": "excluir_gana"})
    return out


if __name__ == "__main__":
    import argparse
    import medios
    ap = argparse.ArgumentParser(description="Inspección de marcas del autor de un video.")
    ap.add_argument("video")
    a = ap.parse_args()
    info = medios.inspeccionar(a.video)
    fp = medios.fingerprint(a.video, info)
    r = resolver(a.video, fp, info["duracion"])
    if not r:
        print("sin marcas")
    else:
        print(f"origen={r['origen']} revision={r['revision']} "
              f"válidas={len(r['marcas'])} cuarentena={len(r['cuarentena'])}")
        for e in a_eventos(r["marcas"]):
            print(f"  {e['id']} [{e['t_ini']:.1f}–{e['t_fin']:.1f}] {e['tipo']}"
                  + (f" {e['decision'].upper()}" if e["decision"] else "")
                  + (f" «{e['prompt'][:60]}»" if e["prompt"] else ""))
        for c in r["cuarentena"]:
            mid = (c["marca"].get("id", "?") if isinstance(c["marca"], dict)
                   else repr(c["marca"])[:30])
            print(f"  ⚠ cuarentena {mid}: {c['motivo']}")
