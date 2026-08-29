#!/usr/bin/env python3
"""
guion.py — el GUION del video: `<video>.guion.md`, el documento que Gabriel escribe y
que (junto al master.json) se le entrega a la AI orquestadora que edita el video.

Regla de oro (decisión del usuario, re-confirmada 2×): **JSON manda, el .md es vista**.
El sidecar de marcas (marcas.py) es la ÚNICA fuente de verdad de las marcas; acá los
bloques de marca se REGENERAN; lo único que pertenece al .md es el texto libre del
guion. No hay canal .md→sidecar. Diseño consensuado con Codex gpt-5.6-sol (5 rondas,
READY): three-brain-out/2026-07-21-tab-marcar/DISENO-final.md, puntos E/h.2/h.3/h.9.

Estructura del documento — una secuencia de SLICES con orden PROPIEDAD del usuario
(nunca se reordena solo; los bloques gestionados se actualizan IN SITU por ID):

    <!-- g<ns8>:guion schema=1 guion_revision=N marcas_revision=M marcas_sha256=… -->
    (texto libre…)
    <!-- g<ns8>:indice -->                       ← índice cronológico, regenerado entero
    …
    <!-- /g<ns8>:indice -->
    (texto libre…)
    <!-- g<ns8>:marca:m0003 sha=xxxxxxxx -->     ← bloque gestionado (heading + prompt)
    ## ⛭ m0003 · [00:12:03.4 – 00:12:45.1] · región · EXCLUIR
    …prompt renderizado…
    <!-- /g<ns8>:marca:m0003 -->

· Marca nueva → su bloque se inserta UNA vez tras el bloque de la marca cronológica
  anterior (o tras el índice); después no se mueve solo. Mover una marca cambia su
  heading, no su posición. Marca borrada o ID huérfano (en carga inicial Y en recarga
  externa, antes de regenerar el índice) → texto libre in situ con nota de
  conservación — NUNCA se pierde texto (consenso r2/r3 bloqueante 2).
· Sentinelas rotos/duplicados/anidados → SentinelasRotos: sync DETENIDO, backup
  `.bak-<ts>` antes de cualquier reparación, nunca reparación silenciosa (h.3/h.9).
· Persistencia dual con `guion_revision` propia: sidecar `<video>.guion.md` +
  store `marcas_store/<hash>.guion.md`; al cargar gana la revisión mayor (empate →
  sidecar); al guardar sidecar-primero con espejo. Cambio externo por sha256 del
  CONTENIDO, no mtime (h.9).
"""
from __future__ import annotations

import hashlib
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path

from marcas import STORE_DIR, _escribir_atomico

SCHEMA = 1
_RE_ID = re.compile(r"^m\d{4,}$")
# el header es lo ÚNICO que se busca sin conocer el ns (lo declara)
_RE_HEADER = re.compile(r"<!--\s*g([0-9a-f]{8}):guion\s+([^>]*?)\s*-->")
_NOTA_HUERFANO = "> _Instrucción conservada de la marca eliminada {mid} {rango}:_"


class SentinelasRotos(RuntimeError):
    """El documento tiene sentinelas dañados (sin cierre, duplicados, anidados o con
    gramática inválida): la sincronización se DETIENE; el llamador hace backup y
    ofrece «Reparar»."""


class CambioExterno(RuntimeError):
    """El archivo del guion cambió FUERA de la app desde la última lectura/escritura:
    guardar lo pisaría. Solo «Sobrescribir»/reparación explícita usa forzar=True
    (review impl r1.3 — el guard vive en Documento.guardar, no en cada caller)."""


def sidecar_path(video) -> Path:
    v = Path(video)
    return v.parent / f"{v.stem}.guion.md"


def store_path(fp: dict) -> Path:
    return STORE_DIR / f"{fp['hash_muestreado']}.guion.md"


# ------------------------------------------------------------------ formato --
def _fmt_t(s: float) -> str:
    h, r = divmod(float(s), 3600)
    m, sec = divmod(r, 60)
    if h >= 1:
        return f"{int(h):02d}:{int(m):02d}:{sec:04.1f}"
    return f"{int(m):02d}:{sec:04.1f}"


def _rango(m: dict) -> str:
    if m.get("tipo") == "punto":
        return f"[{_fmt_t(m['t'])}]"
    return f"[{_fmt_t(m['t_ini'])} – {_fmt_t(m['t_fin'])}]"


def _sha8(texto: str) -> str:
    return hashlib.sha256(texto.replace("\r\n", "\n").rstrip().encode("utf-8")).hexdigest()[:8]


def _cuerpo_marca(m: dict) -> str:
    """El CONTENIDO regenerado del bloque (heading + prompt) — JSON manda."""
    dec = (m.get("decision") or "").upper()
    tipo = "punto" if m.get("tipo") == "punto" else "región"
    head = f"## ⛭ {m['id']} · {_rango(m)} · {tipo}" + (f" · {dec}" if dec else "")
    prompt = (m.get("prompt") or "").strip() \
        or "_(sin prompt — escribilo desde el panel del timeline)_"
    return f"{head}\n\n{prompt}"


def _cuerpo_indice(marcas: list[dict]) -> str:
    filas = ["### Índice cronológico de marcas"]
    if not marcas:
        filas.append("_(sin marcas todavía — creá regiones/puntos en el timeline)_")
    for m in sorted(marcas, key=lambda m: float(m.get("t", m.get("t_ini", 0)))):
        dec = (m.get("decision") or "").upper()
        tipo = "punto" if m.get("tipo") == "punto" else "región"
        linea1 = (m.get("prompt") or "").strip().splitlines()
        filas.append(f"- **{m['id']}** · {_rango(m)} · {tipo}"
                     + (f" · **{dec}**" if dec else "")
                     + (f" — {linea1[0][:90]}" if linea1 else ""))
    return "\n".join(filas)


# ------------------------------------------------------------------- parseo --
def _parsear(texto: str, ns: str) -> list[list]:
    """texto → slices [["libre", txt] | ["marca", mid, attrs, cuerpo] | ["indice", cuerpo]].
    Sin pérdida: join(render(slices)) == texto. Malformado → SentinelasRotos."""
    tok = re.compile(rf"<!--\s*(/?)g{ns}:(marca:(m\d{{4,}})|indice)((?:\s+\w+=\S+)*)\s*-->")
    # gramática ESTRICTA (review impl r1.10): cualquier comment del namespace que NO
    # cumpla el token exacto (p.ej. marca:BAD, atributos rotos) es un sentinela dañado,
    # no texto libre — aceptarlo dejaría luego bloques duplicados/fantasma
    validos = {m.span() for m in tok.finditer(texto)}
    for suelto in re.finditer(rf"<!--\s*/?g{ns}:[^>]*-->", texto):
        if suelto.span() not in validos:
            raise SentinelasRotos(f"sentinela con gramática inválida: "
                                  f"{suelto.group(0)[:60]!r}")
    slices, pos, vistos = [], 0, set()
    abierto = None                             # (tipo, mid, attrs, inicio_cuerpo)
    for mt in tok.finditer(texto):
        cierra, que, mid = bool(mt.group(1)), mt.group(2), mt.group(3)
        if abierto is None:
            if cierra:
                raise SentinelasRotos(f"cierre sin apertura: {mt.group(0)!r}")
            if pos < mt.start():
                slices.append(["libre", texto[pos:mt.start()]])
            # atributos con gramática ESTRICTA (review impl r2/r1.10 + r3): marca
            # admite EXACTAMENTE cero atributos o UN `sha=<8 hex>`; índice ninguno.
            # Se valida sobre la LISTA de pares (un dict tragaba duplicados — r3).
            pares = [kv.split("=", 1) for kv in mt.group(4).split()]
            if mid is None and pares:
                raise SentinelasRotos(f"índice con atributos: {mt.group(0)!r}")
            if mid is not None and pares and (
                    len(pares) != 1 or pares[0][0] != "sha"
                    or not re.fullmatch(r"[0-9a-f]{8}", pares[0][1])):
                raise SentinelasRotos(f"atributos inválidos: {mt.group(0)!r}")
            abierto = (que, mid, dict(pares), mt.end())
        else:
            if not cierra or que != abierto[0]:
                raise SentinelasRotos(f"sentinela anidado o cruzado: {mt.group(0)!r}")
            if mt.group(4).strip():
                raise SentinelasRotos(f"cierre con atributos: {mt.group(0)!r}")
            cuerpo = texto[abierto[3]:mt.start()].strip("\n")
            if abierto[0] == "indice":
                if "indice" in vistos:
                    raise SentinelasRotos("índice duplicado")
                vistos.add("indice")
                slices.append(["indice", cuerpo])
            else:
                if mid in vistos:
                    raise SentinelasRotos(f"bloque duplicado: {mid}")
                vistos.add(mid)
                slices.append(["marca", mid, abierto[2], cuerpo])
            abierto = None
        pos = mt.end()
    if abierto is not None:
        raise SentinelasRotos(f"bloque sin cierre: {abierto[0]}"
                              + (f":{abierto[1]}" if abierto[1] else ""))
    if pos < len(texto):
        slices.append(["libre", texto[pos:]])
    return slices


def _render(slices: list[list], ns: str) -> str:
    partes = []
    for s in slices:
        if s[0] == "libre":
            partes.append(s[1])
        elif s[0] == "indice":
            partes.append(f"<!-- g{ns}:indice -->\n{s[1]}\n<!-- /g{ns}:indice -->")
        else:
            _, mid, attrs, cuerpo = s
            a = " ".join(f"{k}={v}" for k, v in attrs.items())
            partes.append(f"<!-- g{ns}:marca:{mid}{' ' + a if a else ''} -->\n"
                          f"{cuerpo}\n<!-- /g{ns}:marca:{mid} -->")
    return "".join(partes)


def _leer_header(texto: str) -> tuple[str, dict] | None:
    mt = _RE_HEADER.search(texto)
    if not mt:
        return None
    attrs = {}
    for kv in mt.group(2).split():
        if "=" in kv:
            k, v = kv.split("=", 1)
            attrs[k] = v
    return mt.group(1), attrs


# ---------------------------------------------------------------- Documento --
class Documento:
    """El guion de UN video, cargado/sincronizado/persistido. La tab lo usa directo."""

    def __init__(self, video, fp: dict):
        self.video, self.fp = Path(video), fp
        self.ns = uuid.uuid4().hex[:8]
        self.guion_revision = 0
        self.marcas_revision = 0
        self.marcas_sha256 = ""
        self.texto = ""                        # SIN el header (se re-arma al render)
        self.origen: str | None = None         # "sidecar" | "store" | None (nuevo)
        self.avisos: list[str] = []
        self.roto: SentinelasRotos | None = None      # sync detenido hasta «Reparar»
        self.backup: Path | None = None
        self.paquete_estado: str | None = None        # p.ej. "stale_legacy" (r1.12)
        # estado del DISCO como la app lo dejó/leyó — AMBOS candidatos (r1.4: si gana
        # el store, un cambio externo del store también tiene que detectarse; y la
        # desaparición de un candidato que existía cuenta como cambio)
        self._estado_disco: dict[str, str | None] = {"sidecar": None, "store": None}
        self._cargar()

    # ---- carga (dual, gana guion_revision; empate → sidecar — h.9) ----
    def _cargar(self):
        cands = []
        for origen, p in (("sidecar", sidecar_path(self.video)), ("store", store_path(self.fp))):
            try:
                crudo = p.read_text(encoding="utf-8")
            except OSError:
                continue
            h = _leer_header(crudo)
            if h is None:
                if origen == "sidecar" and crudo.strip():
                    # archivo ajeno/manual: se ADOPTA entero como texto libre (jamás
                    # se pisa en silencio — el header aparece al primer guardado)
                    cands.append((0, True, origen, crudo, self.ns, {}))
                continue
            ns, attrs = h
            try:
                rev = int(attrs.get("guion_revision") or 0)
            except ValueError:
                rev = 0
            cands.append((rev, origen == "sidecar", origen,
                          crudo[_RE_HEADER.search(crudo).end():].lstrip("\n"), ns, attrs))
        self._estado_disco = self._shas_disco()
        if not cands:
            return
        rev, _, origen, cuerpo, ns, attrs = sorted(cands, key=lambda c: c[:2])[-1]
        self.origen, self.ns, self.guion_revision = origen, ns, rev
        try:
            self.marcas_revision = int(attrs.get("marcas_revision") or 0)
        except ValueError:
            self.marcas_revision = 0
        self.marcas_sha256 = attrs.get("marcas_sha256") or ""
        self.paquete_estado = attrs.get("paquete") or None
        self.texto = cuerpo
        try:
            _parsear(self.texto, self.ns)
        except SentinelasRotos as e:
            self.roto = e
            self.backup = self._backup()
            self.avisos.append(f"⚠ sentinelas dañados ({e}) — sincronización DETENIDA; "
                               f"copia de seguridad en {self.backup.name if self.backup else '—'}. "
                               "Usá «Reparar».")

    def _shas_disco(self) -> dict[str, str | None]:
        out = {}
        for clave, p in (("sidecar", sidecar_path(self.video)), ("store", store_path(self.fp))):
            try:
                out[clave] = hashlib.sha256(p.read_bytes()).hexdigest()
            except OSError:
                out[clave] = None
        return out

    def cambio_externo(self) -> bool:
        """True si ALGÚN candidato (sidecar O store) difiere de como la app lo dejó
        (sha de contenido, no mtime — h.9/r1.4); borrar un candidato que existía
        también cuenta. El llamador decide: Recargar / Sobrescribir."""
        return self._shas_disco() != self._estado_disco

    def _backup(self) -> Path | None:
        ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        for p in (sidecar_path(self.video), store_path(self.fp)):
            if p.exists():
                try:
                    b = p.with_name(p.name + f".bak-{ts}")
                    b.write_bytes(p.read_bytes())
                    return b
                except OSError:
                    continue
        return None

    # ---- sincronización (el corazón: JSON manda, orden documental estable) ----
    def sincronizar(self, reg, marcas_sha256: str | None = None) -> list[str]:
        """Regenera los bloques gestionados desde el Registro (in situ, sin reordenar),
        convierte huérfanos a texto libre, inserta marcas nuevas tras su vecina
        cronológica y regenera el índice. `marcas_sha256` = sha de los bytes del
        candidato ganador de marcas.resolver() (r1.12: el header lo declara para el
        contrato de staleness). Devuelve avisos (p.ej. ediciones manuales dentro de
        bloques gestionados, que se PISAN — política declarada)."""
        if self.roto is not None:
            raise self.roto
        if marcas_sha256 is not None:
            self.marcas_sha256 = marcas_sha256
        avisos = []
        slices = _parsear(self.texto, self.ns)
        vivos = {m["id"]: m for m in reg.marcas}

        # 1) huérfanos → texto libre IN SITU con nota (bloqueante 2 de r2/r3)
        for i, s in enumerate(slices):
            if s[0] == "marca" and s[1] not in vivos:
                rango = ""
                mt = re.search(r"\[[^\]]+\]", s[3])
                if mt:
                    rango = mt.group(0)
                cuerpo = re.sub(r"^##[^\n]*\n+", "", s[3])
                slices[i] = ["libre", "\n" + _NOTA_HUERFANO.format(mid=s[1], rango=rango)
                             + "\n\n" + cuerpo + "\n"]
                avisos.append(f"la marca {s[1]} ya no existe — su texto quedó como "
                              "guion libre (nota de conservación).")

        # 2) bloques vivos: regenerar IN SITU. Edición manual (r1.10): el sha del
        #    sentinela es el hash del bloque COMO LA APP LO ESCRIBIÓ — cualquier
        #    diferencia (cuerpo, heading, sha ausente) es edición manual y se avisa;
        #    los sentinelas mismos los cubre la gramática estricta del parseo.
        presentes = set()
        for s in slices:
            if s[0] != "marca":
                continue
            mid = s[1]
            presentes.add(mid)
            nuevo = _cuerpo_marca(vivos[mid])
            if s[2].get("sha") != _sha8(s[3]):
                avisos.append(f"⚠ el bloque de {mid} fue editado a mano — se repintó "
                              "desde el sidecar (los bloques ⛭ se editan en la app).")
            s[3] = nuevo
            s[2] = {"sha": _sha8(nuevo)}

        # 3) marcas sin bloque: insertar UNA vez tras la vecina cronológica anterior
        #    presente (o tras el índice / al principio) — h.2: después no se mueven
        def t_de(mid):
            m = vivos[mid]
            return float(m.get("t", m.get("t_ini", 0)))
        for mid in sorted(vivos, key=t_de):
            if mid in presentes:
                continue
            previas = [p for p in presentes if t_de(p) <= t_de(mid)]
            nuevo = ["marca", mid, {"sha": _sha8(_cuerpo_marca(vivos[mid]))},
                     _cuerpo_marca(vivos[mid])]
            if previas:
                ancla = max(previas, key=t_de)
                idx = next(i for i, s in enumerate(slices)
                           if s[0] == "marca" and s[1] == ancla)
            else:
                idx = next((i for i, s in enumerate(slices) if s[0] == "indice"), -1)
                if idx < 0:
                    slices.insert(0, nuevo)
                    slices.insert(1, ["libre", "\n\n"])
                    presentes.add(mid)
                    continue
            slices.insert(idx + 1, ["libre", "\n\n"])
            slices.insert(idx + 2, nuevo)
            presentes.add(mid)

        # 4) índice cronológico regenerado (después de normalizar — nunca IDs muertos)
        idx_i = next((i for i, s in enumerate(slices) if s[0] == "indice"), None)
        if idx_i is None:
            slices.insert(0, ["indice", _cuerpo_indice(reg.marcas)])
            slices.insert(1, ["libre", "\n\n"])
        else:
            slices[idx_i][1] = _cuerpo_indice(reg.marcas)

        self.texto = _render(slices, self.ns)
        self.marcas_revision = reg.revision
        self.avisos.extend(avisos)
        return avisos

    def reparar(self, reg) -> None:
        """Reconstrucción EXPLÍCITA tras SentinelasRotos (h.3): el backup ya existe;
        todo lo no reconocido queda como texto libre (se quitan solo los sentinelas
        de este ns) y los bloques se regeneran del sidecar."""
        crudo = re.sub(rf"<!--\s*/?g{self.ns}:[^>]*-->", "", self.texto)
        self.texto = crudo.strip("\n") + "\n"
        self.roto = None
        self.sincronizar(reg)

    # ---- persistencia (dual, espejo, atómica — h.9) ----
    def _header(self) -> str:
        return (f"<!-- g{self.ns}:guion schema={SCHEMA} "
                f"guion_revision={self.guion_revision} "
                f"marcas_revision={self.marcas_revision} "
                + (f"marcas_sha256={self.marcas_sha256} " if self.marcas_sha256 else "")
                + (f"paquete={self.paquete_estado} " if self.paquete_estado else "")
                + f"video={self.video.name!r} "
                f"hash={str(self.fp.get('hash_muestreado'))[:12]} -->")

    def guardar(self, forzar: bool = False) -> str:
        """Persiste (guion_revision+1). Devuelve 'sidecar' o 'store'. GUARD único de
        cambio externo (r1.3): si el disco difiere de como la app lo dejó, lanza
        CambioExterno — TODOS los caminos (autosave, sync por marca, preparar
        paquete) quedan protegidos; solo «Sobrescribir»/reparar usa forzar=True."""
        if not forzar and self.cambio_externo():
            raise CambioExterno("el guion cambió fuera de la app — Recargar o "
                                "Sobrescribir, no pisar en silencio")
        self.guion_revision += 1
        contenido = (self._header() + "\n" + self.texto.lstrip("\n")).rstrip() + "\n"
        sp = sidecar_path(self.video)
        try:
            _escribir_atomico(sp, contenido)
            destino = "sidecar"
            stp = store_path(self.fp)
            if stp.exists():
                try:
                    _escribir_atomico(stp, contenido)
                except Exception:
                    pass
        except OSError:
            stp = store_path(self.fp)
            try:
                _escribir_atomico(stp, contenido)
            except Exception:
                self.guion_revision -= 1       # rollback: el disco no cambió
                raise
            destino = "store"
            self.avisos.append(f"La carpeta del video no es escribible — el guion se "
                               f"guarda en {stp}.")
        self.origen = destino
        self._estado_disco = self._shas_disco()
        return destino

    def recargar(self):
        """Adopta la versión del DISCO (el usuario eligió «Recargar» ante un cambio
        externo). RESETEA el estado antes (review r2 nuevo-1: con ambos candidatos
        borrados, _cargar no encuentra nada y el estado viejo resucitaba el documento
        que el usuario borró). Los huérfanos/repintados se resuelven en el próximo
        sincronizar(). El ns se conserva (si el disco trae otro, _cargar lo pisa)."""
        self.avisos = []
        self.roto = None
        self.backup = None
        self.guion_revision = 0
        self.marcas_revision = 0
        self.marcas_sha256 = ""
        self.paquete_estado = None
        self.texto = ""
        self.origen = None
        self._cargar()

    # ---- para la UI: slices renderizados (h.3/r2.5) ----
    def slices_render(self) -> list[tuple[bool, str]]:
        """[(protegido, texto)] en orden: la UI inserta slice por slice y taggea los
        protegidos DESDE EL WIDGET (r1.6: nada de transportar offsets Python como
        índices `+Nc` de Tcl — con caracteres astrales no coinciden)."""
        if self.roto is not None:
            return [(False, self.texto)]
        return [(s[0] != "libre", _render([s], self.ns))
                for s in _parsear(self.texto, self.ns)]

    def rangos_protegidos(self) -> list[tuple[int, int]]:
        """[(inicio, fin)] en offsets de self.texto de cada bloque gestionado (para
        TESTS/diagnóstico; la UI usa slices_render — ver r1.6)."""
        if self.roto is not None:
            return []
        out, pos = [], 0
        for s in _parsear(self.texto, self.ns):
            largo = len(_render([s], self.ns))
            if s[0] != "libre":
                out.append((pos, pos + largo))
            pos += largo
        return out
