#!/usr/bin/env python3
"""
marcar.py — la pestaña «Marcar»: revisión POST-extracción. Importás un video ya
procesado; la app encuentra su master.json (identidad fuerte `fuentes.media` o match
legacy conservador), pinta la metadata como carriles READ-ONLY sobre el timeline del
mini-editor (editor_medios.py), dejás marcas del autor viendo esa metadata (el MISMO
sidecar/Registro de marcas.py que el wizard), y el panel derecho muestra el GUION
(`<video>.guion.md`, guion.py): JSON manda, el .md es vista + tu texto libre.

«Preparar paquete para la AI» = guardar guion → pipeline.actualizar_derivados()
(solo consolidar+vistas, con snapshot nueva de marcas) → estado del paquete al día.

Diseño consensuado con Codex gpt-5.6-sol (5 rondas, READY):
three-brain-out/2026-07-21-tab-marcar/DISENO-final.md.
"""
from __future__ import annotations

import bisect
import hashlib
import json
import queue
import threading
from pathlib import Path

import customtkinter as ctk

import dialogs
import editor_medios
import guion as guion_mod
import marcas as marcas_mod
import medios
import app_paths

FUENTES_FILE = app_paths.MARK_SOURCES_FILE   # asociación recordada
LANE_META_H = 14                               # px por carril de metadata
LANE_GHOST_H = 12                              # carril fantasma (autor.marcas del master)
LOD_MAX_VECTORIAL = 400                        # >N visibles → coalescing por píxel (h.7)
TOL_DUR_LEGACY = 0.5                           # s — match blando de masters viejos (h.5)

# paleta del sketch de Gabriel (h.12); lo no listado cae al color genérico
COL_STREAM = {
    "voz.transcript": "#e05f9e", "voz.risa": "#4fce5d", "voz.emocion": "#f08c2e",
    "voz.pausas": "#8a8a8a", "voz.instrucciones": "#ff7b7b",
    "fondo.dialogo": "#4db8d8", "fondo.sonidos": "#3a9ec2",
    "fondo.transitorios": "#2e7fa0", "fondo.escenas": "#9b6dd8",
}
COL_GENERICO = "#c9a94a"
COL_GHOST = {None: "#6b5a2e", "incluir": "#2e5a3c", "excluir": "#6b3030"}
CAMPOS_TOOLTIP = ("texto", "text", "familia", "emotion", "emocion", "direccion",
                  "etiqueta", "label", "descripcion", "tipo")


def _leer_fuentes() -> dict:
    try:
        return json.loads(FUENTES_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _recordar_fuente(fp: dict, master_path: Path):
    d = _leer_fuentes()
    d[fp["hash_muestreado"]] = str(master_path)
    try:
        marcas_mod._escribir_atomico(FUENTES_FILE, json.dumps(d, ensure_ascii=False, indent=1))
    except Exception:
        pass


# ------------------------------------------------- master: descubrimiento + saneo --
def _identidad_media(master: dict, fp: dict) -> bool:
    fm = ((master.get("header") or {}).get("fuentes") or {}).get("media") or {}
    return (fm.get("size") == fp.get("size")
            and fm.get("hash_muestreado") == fp.get("hash_muestreado")
            and fm.get("inventario_sha256") == fp.get("inventario_sha256"))


def _match_legacy(master: dict, video: Path, dur: float) -> bool:
    """Masters sin fuentes.media (< v2.3): nombre del medio en duracion_fuente +
    duración ±0.5 s. Evidencia débil → SOLO se usa si no hay match fuerte (h.5)."""
    hdr = master.get("header") or {}
    df = str(hdr.get("duracion_fuente") or "")
    if not df.startswith("ffprobe:") or df[len("ffprobe:"):] != video.name:
        return False
    try:
        return abs(float(hdr.get("duracion") or master.get("duracion")) - dur) <= TOL_DUR_LEGACY
    except (TypeError, ValueError):
        return False


def buscar_masters(video, fp: dict, dur: float) -> tuple[list[Path], list[Path]]:
    """(fuertes, legacy): candidatos *.master.json en la carpeta del video y sus
    subcarpetas de PRIMER nivel (cubre el default metadata/ del pipeline)."""
    video = Path(video)
    cands = sorted(video.parent.glob("*.master.json")) \
        + sorted(video.parent.glob("*/*.master.json"))
    fuertes, legacy = [], []
    for p in cands:
        try:
            master = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(master, dict):
            continue
        if _identidad_media(master, fp):
            fuertes.append(p)
        elif _match_legacy(master, video, dur):
            legacy.append(p)
    return fuertes, legacy


def sanear_master(master, dur_video: float) -> tuple[dict, dict, list[str]]:
    """(streams_sanos, master, avisos) — h.8: copia ORDENADA por stream; eventos
    inválidos o fuera de [0, dur] descartados con conteo; master sin streams = vacío
    válido; solo se RECHAZA (ValueError) una raíz incompatible. Nada se re-escribe."""
    if not isinstance(master, dict) or not isinstance(master.get("streams"), dict):
        raise ValueError("ese archivo no es un master.json (sin `streams`)")
    avisos = []
    dur = master.get("duracion")
    if not isinstance(dur, (int, float)) or not (dur == dur) or dur <= 0:
        avisos.append(f"duración del master inválida ({dur!r}) — uso la del video")
    streams = {}
    for nombre, evs in master["streams"].items():
        if not isinstance(evs, list):
            avisos.append(f"{nombre}: no es una lista — ignorado")
            continue
        sanos, malos = [], 0
        for e in evs:
            if not isinstance(e, dict):
                malos += 1
                continue
            try:
                t0 = float(e["t_ini"])
                t1 = float(e.get("t_fin", t0))
            except (KeyError, TypeError, ValueError):
                malos += 1
                continue
            if not (t0 == t0 and t1 == t1) or t1 < t0 or t0 < 0 or t1 > dur_video + 0.75:
                malos += 1                     # fuera de rango: descarte, sin clamp (h.8)
                continue
            sanos.append(e)
        if malos:
            avisos.append(f"{nombre}: {malos} evento(s) inválido(s) ignorados")
        if sanos:
            sanos.sort(key=lambda e: float(e["t_ini"]))
            streams[nombre] = sanos
    return streams, master, avisos


class _Indice:
    """Índice de intervalos de UN stream (h.7/r2.4): candidatos por
    lo = bisect_right(prefix_max_end, t_ini_vis), hi = bisect_left(starts, t_fin_vis)
    y filtro final t_fin > t_ini_vis."""

    def __init__(self, evs: list[dict]):
        self.evs = evs
        self.starts = [float(e["t_ini"]) for e in evs]
        self.pme = []
        mx = float("-inf")
        for e in evs:
            mx = max(mx, float(e.get("t_fin", e["t_ini"])))
            self.pme.append(mx)

    def visibles(self, t0: float, t1: float) -> list[dict]:
        lo = bisect.bisect_right(self.pme, t0)
        hi = bisect.bisect_left(self.starts, t1)
        return [e for e in self.evs[lo:hi] if float(e.get("t_fin", e["t_ini"])) > t0]


def _tooltip_texto(nombre: str, e: dict) -> str:
    for k in CAMPOS_TOOLTIP:
        v = e.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()[:120]
    pares = [f"{k}:{v}" for k, v in e.items()
             if k not in ("t_ini", "t_fin", "id") and isinstance(v, (str, int, float))][:2]
    return " · ".join(pares) if pares else nombre


def _fuerza(e: dict) -> float:
    """Intensidad 0..1 para el alto del bracket (arousal_z/z/conf clampeados — h.12:
    el valor bruto no es comparable entre archivos)."""
    for k in ("arousal_z", "z", "conf", "fuerza"):
        v = e.get(k)
        if isinstance(v, (int, float)):
            return max(0.35, min(1.0, 0.35 + abs(float(v)) / 3.0 * 0.65))
    return 1.0


# ================================================================== la pestaña --
class TabMarcar:
    """UI de la pestaña «Marcar». El estado del medio vive en su EditorMedios; el de
    las marcas en el Registro COMPARTIDO (marcas.py); el del guion en guion.Documento."""

    def __init__(self, tab, app):
        self.tab, self.app = tab, app
        self.q: queue.Queue = queue.Queue()    # eventos de descubrimiento/derivados
        self.master: dict | None = None
        self.master_path: Path | None = None
        self.streams: dict[str, list] = {}     # saneados (h.8)
        self._indices: dict[str, _Indice] = {}
        self._ghost: list[dict] = []           # autor.marcas del master sin equivalente vivo
        self._lanes_off: set[str] = set()      # toggles de visibilidad
        self.doc: guion_mod.Documento | None = None
        self._sync_interno = False             # guard de reentrada del Text (r2.5)
        self._autosave_after = None
        self._derivados_vivo = False
        self._deriv_job = 0                    # id del JOB de derivados (r1.11: separado
                                               # de _gen — el fin SIEMPRE limpia el estado)
        self._gen = 0                          # generación de video (descarta workers viejos)

        tab.grid_columnconfigure(0, weight=1)
        tab.grid_rowconfigure(1, weight=1)

        # ---- barra superior: video + master + paquete ----
        top = ctk.CTkFrame(tab)
        top.grid(row=0, column=0, sticky="ew", padx=4, pady=(2, 4))
        top.grid_columnconfigure(1, weight=1)
        ctk.CTkButton(top, text="Abrir video procesado…", width=170,
                      command=self._pick_video).grid(row=0, column=0, padx=(10, 8), pady=8)
        self.lbl_master = ctk.CTkLabel(top, text="Importá un video que ya pasó por "
                                                 "«Extraer metadata».",
                                       anchor="w", text_color="gray60")
        self.lbl_master.grid(row=0, column=1, sticky="ew")
        self.lbl_paquete = ctk.CTkLabel(top, text="", width=170, anchor="e")
        self.lbl_paquete.grid(row=0, column=2, padx=(8, 6))
        self.btn_paquete = ctk.CTkButton(top, text="Preparar paquete para la AI",
                                         width=200, command=self._preparar_paquete,
                                         state="disabled")
        self.btn_paquete.grid(row=0, column=3, padx=(0, 10))

        # ---- split editor | guion (h.11: pane ajustable con mínimos) ----
        import tkinter as tk
        self.pw = tk.PanedWindow(tab, orient="horizontal", bg="#1a1a1a", bd=0,
                                 sashwidth=6, sashrelief="flat")
        self.pw.grid(row=1, column=0, sticky="nsew", padx=4, pady=(0, 4))

        izq = ctk.CTkFrame(self.pw, fg_color="transparent")
        izq.grid_columnconfigure(0, weight=1)
        izq.grid_rowconfigure(0, weight=1)
        # la reacción a cambios de MARCAS va por la suscripción al Registro compartido
        # (r1.7: cubre también mutaciones hechas desde el wizard y el menú contextual)
        self.ed = editor_medios.EditorMedios(
            izq, carriles_extra=self._carriles, on_video_cargado=self._on_video_cargado)
        self.ed.f.grid(row=0, column=0, sticky="nsew")
        # toggles de carriles (compactos, sobre el timeline)
        self.f_toggles = ctk.CTkFrame(izq, fg_color="transparent")
        self.f_toggles.grid(row=1, column=0, sticky="ew", padx=8)
        # metadata: hover=tooltip · click derecho=crear marca (el click IZQ ya hace
        # seek vía el _scrub del editor — no hay binding extra que duplique eso)
        self.ed.tl.bind("<Motion>", self._meta_hover, add=True)
        self.ed.tl.bind("<Button-3>", self._meta_menu, add=True)
        self.ed.menu_contextual = False        # Marcar tiene su propio menú de metadata
        self._tt_items = []

        der = ctk.CTkFrame(self.pw)
        der.grid_columnconfigure(0, weight=1)
        der.grid_rowconfigure(1, weight=1)
        barra = ctk.CTkFrame(der, fg_color="transparent")
        barra.grid(row=0, column=0, sticky="ew", padx=6, pady=(6, 2))
        barra.grid_columnconfigure(3, weight=1)
        ctk.CTkLabel(barra, text="GUION", font=ctk.CTkFont(size=13, weight="bold")).grid(
            row=0, column=0, padx=(6, 10))
        self.btn_guardar = ctk.CTkButton(barra, text="Guardar", width=76,
                                         command=self._guardar_guion, state="disabled")
        self.btn_guardar.grid(row=0, column=1, padx=3)
        self.btn_reparar = ctk.CTkButton(barra, text="Reparar", width=76,
                                         fg_color="#7a5a1e", hover_color="#8f6a24",
                                         command=self._reparar_guion)
        self.btn_reparar.grid(row=0, column=2, padx=3)
        self.btn_reparar.grid_remove()
        self.lbl_guion = ctk.CTkLabel(barra, text="", anchor="e", text_color="gray55",
                                      font=ctk.CTkFont(size=11))
        self.lbl_guion.grid(row=0, column=3, sticky="ew", padx=(8, 6))
        self.txt = ctk.CTkTextbox(der, wrap="word", font=ctk.CTkFont(size=13))
        self.txt.grid(row=1, column=0, sticky="nsew", padx=6, pady=(0, 6))
        self._armar_proteccion_texto()
        self.lbl_aviso = ctk.CTkLabel(der, text="", anchor="w", justify="left",
                                      text_color="#e8b34b", wraplength=380,
                                      font=ctk.CTkFont(size=11))
        self.lbl_aviso.grid(row=2, column=0, sticky="ew", padx=10, pady=(0, 6))

        self.pw.add(izq, minsize=700, stretch="always")
        self.pw.add(der, minsize=320, stretch="never")
        # ventana angosta → panel colapsado por defecto (h.11)
        tab.after(300, self._colapsar_si_angosta)
        tab.after(120, self._pump)

    # ---------------------------------------------------------------- ciclo de vida --
    def activar(self):
        self.ed.activar()
        # cambio externo del guion al volver a la tab (h.9)
        if self.doc is not None and self.doc.cambio_externo():
            self._aviso("El guion cambió FUERA de la app — «Guardar» lo pisaría. "
                        "Recargá con el botón Reparar→Recargar o revisá el archivo.")

    def desactivar(self):
        self.ed.desactivar()

    def cerrar(self):
        if self.ed.reg is not None:
            self.ed.reg.desuscribir(self._al_cambiar_marcas)
        self.ed.cerrar()

    def _sha_marcas(self) -> str | None:
        """sha256 de los bytes del candidato ganador del sidecar de marcas (para el
        header del guion y el contrato de staleness — h.10/r1.12)."""
        if self.ed.info is None:
            return None
        try:
            r = marcas_mod.resolver(self.ed.info["path"], self.ed.fp,
                                    self.ed.info["duracion"])
        except Exception:
            return None
        return hashlib.sha256(r["bytes"]).hexdigest() if r else None

    def _colapsar_si_angosta(self):
        try:
            if self.tab.winfo_width() < 1100:
                self.pw.paneconfigure(self.pw.panes()[1], width=0)
        except Exception:
            pass

    # ---------------------------------------------------------------- carga de video --
    def _pick_video(self):
        f = dialogs.open_file("Video procesado",
                              [("Video", ["*.mp4", "*.mov", "*.mkv", "*.avi", "*.webm"]),
                               ("Todos", ["*"])], remember="marcar_video")
        if not f:
            return
        self._gen += 1
        if self.ed.reg is not None:            # instancia compartida: soltar suscripción
            self.ed.reg.desuscribir(self._al_cambiar_marcas)
        self.master = self.master_path = None
        self.streams, self._indices, self._ghost = {}, {}, []
        self.doc = None
        self.btn_paquete.configure(state="disabled")
        self.btn_guardar.configure(state="disabled")
        self.lbl_paquete.configure(text="")
        self.lbl_master.configure(text="Inspeccionando…")
        self.ed.cargar(f)

    def _on_video_cargado(self, info, fp):
        """Hook del editor tras la inspección (el Registro YA está cargado): suscribir
        la reacción de la tab y buscar master + guion en workers — nada de I/O pesado
        en el hilo de Tk."""
        if self.ed.reg is not None:
            self.ed.reg.suscribir(self._al_cambiar_marcas)
        gen = self._gen

        def work():
            try:
                rec = _leer_fuentes().get(fp["hash_muestreado"])
                fuertes, legacy = buscar_masters(info["path"], fp, info["duracion"])
                if rec and Path(rec).exists() and Path(rec) not in fuertes + legacy:
                    fuertes = [Path(rec)] + fuertes      # asociación recordada primero
                self.q.put(("masters", gen, fuertes, legacy))
            except Exception as e:
                self.q.put(("err", gen, f"buscando el master: {e}"))

            try:
                doc = guion_mod.Documento(info["path"], fp)
                self.q.put(("guion", gen, doc))
            except Exception as e:
                self.q.put(("err", gen, f"cargando el guion: {e}"))
        threading.Thread(target=work, daemon=True).start()

    def _elegir_master(self, cands: list[Path], fuerte: bool):
        """Varios candidatos del MISMO nivel → selector explícito, jamás por fecha (h.5)."""
        if len(cands) == 1:
            self._usar_master(cands[0], fuerte)
            return
        import tkinter as tk
        win = ctk.CTkToplevel(self.tab)
        win.title("Elegí el master.json")
        win.geometry("640x300")
        win.transient(self.tab.winfo_toplevel())
        ctk.CTkLabel(win, text="Hay varios master.json que matchean este video "
                               f"({'identidad fuerte' if fuerte else 'match por duración'}). "
                               "Elegí cuál usar:", wraplength=600, justify="left").pack(
            padx=12, pady=(12, 6), anchor="w")
        lb = tk.Listbox(win, bg="#1d1d1d", fg="#ddd", selectbackground="#2e6b45",
                        font=("TkDefaultFont", 10))
        lb.pack(fill="both", expand=True, padx=12, pady=6)
        for p in cands:
            try:
                mt = p.stat().st_mtime
                from datetime import datetime
                fecha = datetime.fromtimestamp(mt).strftime("%Y-%m-%d %H:%M")
            except OSError:
                fecha = "?"
            lb.insert("end", f"{p}  ·  {fecha}")

        def ok():
            sel = lb.curselection()
            if sel:
                win.destroy()
                self._usar_master(cands[sel[0]], fuerte)
        ctk.CTkButton(win, text="Usar este master", command=ok).pack(pady=(0, 12))

    def _usar_master(self, path: Path, fuerte: bool):
        gen = self._gen
        info = self.ed.info

        def work():
            try:
                master = json.loads(Path(path).read_text(encoding="utf-8"))
                streams, master, avisos = sanear_master(master, info["duracion"])
                self.q.put(("master", gen, Path(path), master, streams, avisos, fuerte))
            except Exception as e:
                self.q.put(("err", gen, f"cargando {Path(path).name}: {e}"))
        threading.Thread(target=work, daemon=True).start()

    def _pick_master_manual(self):
        f = dialogs.open_file("master.json del video",
                              [("Master", ["*.master.json"]), ("JSON", ["*.json"])],
                              remember="marcar_master")
        if f:
            self._usar_master(Path(f), fuerte=False)

    # ------------------------------------------------------------------- carriles --
    def _streams_visibles(self) -> list[str]:
        orden = list(COL_STREAM) + sorted(k for k in self.streams if k not in COL_STREAM
                                          and k != "autor.marcas")
        return [k for k in orden if k in self.streams and k not in self._lanes_off
                and k != "autor.marcas"]

    def _carriles(self) -> list[dict]:
        """Hook `carriles_extra` del editor: fantasma + un carril por stream visible."""
        out = []
        if self._ghost:
            out.append({"alto": LANE_GHOST_H, "dibujar": self._dibujar_ghost})
        for nombre in self._streams_visibles():
            out.append({"alto": LANE_META_H,
                        "dibujar": (lambda tl, g, y0, n=nombre: self._dibujar_lane(tl, g, y0, n))})
        return out

    def _dibujar_lane(self, tl, g, y0, nombre):
        ed = self.ed
        t0, span = ed.view
        t1 = t0 + span
        col = COL_STREAM.get(nombre, COL_GENERICO)
        x0g, ancho = g
        tl.create_rectangle(x0g, y0, x0g + ancho, y0 + LANE_META_H,
                            fill="#161616", outline="#222")
        tl.create_text(x0g + 3, y0 + LANE_META_H / 2, text=nombre.split(".", 1)[-1][:10],
                       anchor="w", fill="#555", font=("TkDefaultFont", 7))
        idx = self._indices.get(nombre)
        if idx is None:
            return
        vis = idx.visibles(t0, t1)
        if nombre == "fondo.escenas":          # fronteras verticales, no brackets (h.12)
            for e in vis[:LOD_MAX_VECTORIAL]:
                x = ed._t2x(float(e["t_ini"]), g)
                tl.create_line(x, y0, x, y0 + LANE_META_H, fill=col, width=2)
            return
        if len(vis) <= LOD_MAX_VECTORIAL:      # vectorial por evento
            for e in vis:
                xa = ed._t2x(max(float(e["t_ini"]), t0), g)
                xb = ed._t2x(min(float(e.get("t_fin", e["t_ini"])), t1), g)
                h = (LANE_META_H - 4) * _fuerza(e)
                ym = y0 + LANE_META_H / 2
                tl.create_rectangle(xa, ym - h / 2, max(xb, xa + 2), ym + h / 2,
                                    fill=col, outline="")
            return
        # LOD denso (h.7): cobertura POR PÍXEL → un rect por corrida contigua — cero
        # items por evento; hit-test siempre contra el índice, no contra items
        cubierto = bytearray(int(ancho) + 1)
        for e in vis:
            pa = int((max(float(e["t_ini"]), t0) - t0) / span * ancho)
            pb = int((min(float(e.get("t_fin", e["t_ini"])), t1) - t0) / span * ancho)
            for px in range(max(0, pa), min(int(ancho), pb + 1)):
                cubierto[px] = 1
        px = 0
        while px <= int(ancho):
            if cubierto[px]:
                ini = px
                while px <= int(ancho) and cubierto[px]:
                    px += 1
                tl.create_rectangle(x0g + ini, y0 + 3, x0g + px, y0 + LANE_META_H - 3,
                                    fill=col, outline="")
            px += 1

    def _dibujar_ghost(self, tl, g, y0):
        """autor.marcas del master SIN equivalente vivo (h.10): atenuado, NO interactivo."""
        ed = self.ed
        t0, span = ed.view
        x0g, ancho = g
        tl.create_rectangle(x0g, y0, x0g + ancho, y0 + LANE_GHOST_H,
                            fill="#141414", outline="#222")
        tl.create_text(x0g + 3, y0 + LANE_GHOST_H / 2, text="master",
                       anchor="w", fill="#4a4a4a", font=("TkDefaultFont", 7))
        for e in self._ghost:
            ta, tb = float(e["t_ini"]), float(e.get("t_fin", e["t_ini"]))
            if tb < t0 or ta > t0 + span:
                continue
            xa = ed._t2x(max(ta, t0), g)
            xb = ed._t2x(min(tb, t0 + span), g)
            col = COL_GHOST.get(e.get("decision"), COL_GHOST[None])
            tl.create_rectangle(xa, y0 + 2, max(xb, xa + 2), y0 + LANE_GHOST_H - 2,
                                fill=col, outline="", stipple="gray50")

    def _lane_en(self, y) -> tuple[str, int] | None:
        """(stream, y0) del carril de metadata bajo el cursor (para hover/menú)."""
        base = editor_medios.RULER_H + editor_medios.MARKS_H
        if self._ghost:
            base += LANE_GHOST_H
        for nombre in self._streams_visibles():
            if base <= y < base + LANE_META_H:
                return nombre, base
            base += LANE_META_H
        return None

    def _evento_en(self, nombre, x, g) -> dict | None:
        idx = self._indices.get(nombre)
        if idx is None:
            return None
        t = self.ed._x2t(x, g)
        holgura = self.ed.view[1] / max(g[1], 1) * 3        # ±3 px en segundos
        vis = idx.visibles(t - holgura, t + holgura)
        return vis[0] if vis else None

    def _meta_hover(self, e):
        for it in self._tt_items:
            try:
                self.ed.tl.delete(it)
            except Exception:
                pass
        self._tt_items = []
        g = self.ed._tl_geo()
        if not g or not self.streams:
            return
        hit = self._lane_en(e.y)
        if not hit:
            return
        nombre, _ = hit
        ev = self._evento_en(nombre, e.x, g)
        if not ev:
            return
        txt = f"{nombre} · [{ev['t_ini']:.1f}–{float(ev.get('t_fin', ev['t_ini'])):.1f}s] · " \
              + _tooltip_texto(nombre, ev)
        tl = self.ed.tl
        x = min(e.x + 10, max(tl.winfo_width() - 320, 10))
        t_id = tl.create_text(x, e.y - 14, text=txt[:150], anchor="w", fill="#eee",
                              font=("TkDefaultFont", 9), width=320)
        caja = tl.bbox(t_id)
        if caja:
            r_id = tl.create_rectangle(caja[0] - 4, caja[1] - 2, caja[2] + 4, caja[3] + 2,
                                       fill="#262626", outline="#444")
            tl.tag_lower(r_id, t_id)
            self._tt_items = [r_id, t_id]
        else:
            self._tt_items = [t_id]

    def _meta_menu(self, e):
        """Click derecho sobre un evento de metadata → «Crear marca desde este evento»
        (rango exacto; duración cero → punto) — h.12."""
        g = self.ed._tl_geo()
        if not g or self.ed.reg is None:
            return
        hit = self._lane_en(e.y)
        if not hit:
            return
        ev = self._evento_en(hit[0], e.x, g)
        if not ev:
            return
        import tkinter as tk
        menu = tk.Menu(self.ed.tl, tearoff=0, bg="#222", fg="#ddd",
                       activebackground="#2e6b45")
        t0, t1 = float(ev["t_ini"]), float(ev.get("t_fin", ev["t_ini"]))

        def crear():
            try:
                if t1 - t0 < 0.05:
                    m = self.ed.reg.agregar_punto(t0)
                else:
                    m = self.ed.reg.agregar_region(t0, t1)
                self.ed._seleccionar(m, foco_prompt=True)
                self.ed._dibujar_timeline()
            except Exception as ex:
                self.ed.status(f"⚠ {ex}")
        menu.add_command(label=f"Crear marca desde este evento "
                               f"[{t0:.1f}–{t1:.1f}s]", command=crear)
        menu.tk_popup(e.x_root, e.y_root)

    def _armar_toggles(self):
        for w in self.f_toggles.winfo_children():
            w.destroy()
        if not self.streams:
            return
        ctk.CTkLabel(self.f_toggles, text="Carriles:", text_color="gray55",
                     font=ctk.CTkFont(size=11)).grid(row=0, column=0, padx=(4, 6))
        col = 1
        orden = list(COL_STREAM) + sorted(k for k in self.streams if k not in COL_STREAM
                                          and k != "autor.marcas")
        for nombre in [k for k in orden if k in self.streams and k != "autor.marcas"]:
            var = ctk.BooleanVar(value=nombre not in self._lanes_off)

            def flip(n=nombre, v=None):
                if n in self._lanes_off:
                    self._lanes_off.discard(n)
                else:
                    self._lanes_off.add(n)
                self.ed.refrescar_layout()
            cb = ctk.CTkCheckBox(self.f_toggles, text=nombre.split(".", 1)[-1], width=20,
                                 checkbox_width=14, checkbox_height=14, variable=var,
                                 font=ctk.CTkFont(size=11), command=flip,
                                 text_color=COL_STREAM.get(nombre, COL_GENERICO))
            cb.grid(row=0, column=col, padx=3)
            col += 1

    def _calcular_ghost(self):
        """MK#### del master sin marca viva EQUIVALENTE (dedup por número + contenido —
        h.10). Nunca comparten carril con las vivas."""
        self._ghost = []
        evs = self.streams.get("autor.marcas") or []
        reg = self.ed.reg
        vivos = {m["id"]: m for m in (reg.marcas if reg else [])}
        for e in evs:
            mid = "m" + str(e.get("id", ""))[2:]
            m = vivos.get(mid)
            if m is not None:
                t0v = float(m.get("t", m.get("t_ini", -1)))
                t1v = float(m.get("t_fin", t0v))
                if (abs(t0v - float(e["t_ini"])) < 0.002
                        and abs(t1v - float(e.get("t_fin", e["t_ini"]))) < 0.002
                        and (m.get("decision") == e.get("decision"))
                        and ((m.get("prompt") or None) == (e.get("prompt") or None))):
                    continue                   # idéntica a la viva: no ensucia
            self._ghost.append(e)

    # -------------------------------------------------------------------- guion --
    def _armar_proteccion_texto(self):
        """Proxy del comando Tcl del Text interno (r2.5): insert/delete/replace que
        TOQUEN un rango gestionado se rechazan ENTERAS (regla determinista); las
        regeneraciones de la app pasan con el guard _sync_interno."""
        t = self.txt._textbox
        self._tk_text = t
        orig = str(t) + "_orig"
        t.tk.call("rename", str(t), orig)

        def proxy(cmd, *args):
            if cmd in ("insert", "delete", "replace") and not self._sync_interno:
                try:
                    toca = self._toca_protegido(cmd, args)
                except Exception:
                    toca = True                # fail CLOSED (r1.5): si no puedo analizar
                if toca:                       # la operación, se rechaza entera
                    self.lbl_guion.configure(
                        text="los bloques ⛭ se editan desde el timeline")
                    return ""
            return t.tk.call((orig, cmd) + args)
        t.tk.createcommand(str(t), proxy)

    def _toca_protegido(self, cmd, args) -> bool:
        """r1.5: los índices llegan como marks/tags (`sel.first`, `insert`), no solo
        numéricos — SIEMPRE se resuelven con t.index(); delete acepta varios pares."""
        t = self._tk_text
        if cmd == "insert":
            idx = t.index(str(args[0]))
            aqui = "protegido" in t.tag_names(idx)
            antes = "protegido" in t.tag_names(f"{idx} -1c")
            return aqui and antes              # estrictamente ADENTRO (bordes = libre)
        if cmd == "replace":
            rangos = [(t.index(str(args[0])), t.index(str(args[1])))]
        else:                                  # delete: índices sueltos o en pares
            idxs = [t.index(str(a)) for a in args]
            rangos, i = [], 0
            while i < len(idxs):
                if i + 1 < len(idxs):
                    rangos.append((idxs[i], idxs[i + 1])); i += 2
                else:
                    rangos.append((idxs[i], t.index(f"{idxs[i]} +1c"))); i += 1
        for i1, i2 in rangos:
            if t.compare(i1, ">", i2):
                i1, i2 = i2, i1
            if t.tag_nextrange("protegido", i1, i2):
                return True
        return False

    def _refrescar_guion(self, mantener_scroll=True):
        if self.doc is None:
            return
        self._sync_interno = True
        try:
            pos = self.txt.yview()[0] if mantener_scroll else 0.0
            t = self._tk_text
            t.delete("1.0", "end")
            t.tag_configure("protegido", background="#20261f")
            # slice por slice, taggeando DESDE el widget (r1.6: nada de offsets Python
            # convertidos a `+Nc` — con caracteres astrales Tcl cuenta distinto)
            for protegido, trozo in self.doc.slices_render():
                t.insert("end", trozo, ("protegido",) if protegido else ())
            self.txt.yview_moveto(pos)
        finally:
            self._sync_interno = False
        self.lbl_guion.configure(
            text=f"rev {self.doc.guion_revision} · {self.doc.origen or 'nuevo'}")
        self.btn_reparar.grid() if self.doc.roto else self.btn_reparar.grid_remove()
        if self.doc.avisos:
            self._aviso(" · ".join(self.doc.avisos[-2:]))
            self.doc.avisos = []
        self._tk_text.edit_modified(False)
        self._tk_text.bind("<<Modified>>", self._al_modificar, add=False)

    def _al_modificar(self, _e=None):
        if self._sync_interno or self.doc is None or not self._tk_text.edit_modified():
            return
        self._tk_text.edit_modified(False)
        if self._autosave_after:
            try:
                self.tab.after_cancel(self._autosave_after)
            except Exception:
                pass
        self._autosave_after = self.tab.after(1500, self._guardar_guion)

    def _texto_widget(self) -> str:
        return self.txt.get("1.0", "end-1c")

    def _guardar_guion(self) -> bool:
        """True si el guion quedó GUARDADO (r1.8: «Preparar paquete» aborta si no).
        El guard de cambio externo vive en Documento.guardar (r1.3)."""
        self._autosave_after = None
        if self.doc is None:
            return False
        self.doc.texto = self._texto_widget()
        try:
            destino = self.doc.guardar()
        except guion_mod.CambioExterno:
            self._aviso("⚠ el guion cambió fuera de la app — NO guardé. «Reparar» "
                        "recarga del disco; guardar de nuevo tras revisar lo pisa.")
            return False
        except Exception as e:
            self._aviso(f"✗ no pude guardar el guion: {e}")
            return False
        self.lbl_guion.configure(
            text=f"rev {self.doc.guion_revision} · guardado en {destino}")
        return True

    def _reparar_guion(self):
        if self.doc is None or self.ed.reg is None:
            return
        if self.doc.roto is not None:
            self.doc.reparar(self.ed.reg)
        else:
            self.doc.recargar()                # doble uso: recargar tras cambio externo
            if self.doc.roto is None:
                try:
                    self.doc.sincronizar(self.ed.reg, marcas_sha256=self._sha_marcas())
                except guion_mod.SentinelasRotos:
                    pass
        self._refrescar_guion()
        if self.doc.roto is None:
            try:                               # reparación EXPLÍCITA → puede forzar
                self.doc.guardar(forzar=True)
            except Exception as e:
                self._aviso(f"✗ no pude guardar el guion reparado: {e}")

    def _al_cambiar_marcas(self):
        """Suscripción al Registro compartido (r1.7): CUALQUIER mutación de marcas —
        desde esta tab, el wizard o el menú contextual — re-sincroniza guion, carril
        fantasma y estado del paquete. Ghost y staleness se recalculan SIEMPRE
        (r2/r1.7: también cuando el guion no se pudo tocar). JSON manda: el .md se
        repinta preservando el texto libre del widget."""
        try:
            if self.doc is None or self.ed.reg is None or self.doc.roto is not None:
                return
            self.doc.texto = self._texto_widget()
            try:
                self.doc.sincronizar(self.ed.reg, marcas_sha256=self._sha_marcas())
                self.doc.guardar()
            except guion_mod.CambioExterno:
                self._aviso("⚠ el guion cambió fuera de la app — la marca quedó "
                            "guardada en su sidecar, pero el .md NO se pisó. Tocá "
                            "«Reparar» para recargar y re-sincronizar.")
                return
            except guion_mod.SentinelasRotos:
                self._refrescar_guion()
                return
            except Exception as e:
                self._aviso(f"✗ guion: {e}")
                return
            self._refrescar_guion()
        finally:
            self._calcular_ghost()
            self._estado_paquete()

    def _aviso(self, m):
        self.lbl_aviso.configure(text=m)

    # ------------------------------------------------------------ paquete / derivados --
    def _estado_paquete(self):
        """Staleness por HASH (h.10): fuentes.marcas.sha256 del master vs sha256 de los
        bytes del candidato ganador de marcas.resolver()."""
        if self.master is None or self.ed.info is None:
            self.lbl_paquete.configure(text="")
            return
        reg = self.ed.reg
        r = None
        try:
            r = marcas_mod.resolver(self.ed.info["path"], self.ed.fp,
                                    self.ed.info["duracion"])
        except Exception:
            pass
        fm = ((self.master.get("header") or {}).get("fuentes") or {}).get("marcas") or {}
        sha_master = fm.get("sha256")
        if reg is not None and reg.estado_error:
            estado, colr = "✗ marcas sin guardar", "#ff5252"
        elif r is None and sha_master:
            estado, colr = "⚠ estado ambiguo (master con marcas, sidecar ausente)", "#e8b34b"
        elif r is None and not sha_master:
            estado, colr = "✓ paquete al día", "#4ade80"
        elif r is not None and not sha_master:
            estado, colr = "⟳ master sin tus marcas", "#e8b34b"
        elif hashlib.sha256(r["bytes"]).hexdigest() == sha_master:
            estado, colr = "✓ paquete al día", "#4ade80"
        else:
            estado, colr = "⟳ master desactualizado", "#e8b34b"
        self.lbl_paquete.configure(text=estado, text_color=colr)

    def _preparar_paquete(self):
        """Guardar guion → pipeline.actualizar_derivados (thread) → refrescar estado.
        Con marcas en error de guardado o guion SIN guardar NO se prepara (r2.6/r1.8)."""
        if self._derivados_vivo or self.master_path is None:
            return
        reg = self.ed.reg
        if reg is not None and reg.estado_error:
            self._aviso(f"✗ no se puede preparar: {reg.estado_error}")
            return
        if not self._guardar_guion():
            self._aviso("✗ paquete NO preparado: el guion no quedó guardado (mirá el "
                        "aviso de arriba).")
            return
        outdir = self.master_path.parent
        fuente_actual = self.ed.info["path"] if self.ed.info else None
        gen = self._gen
        self._deriv_job += 1
        job = self._deriv_job
        self._derivados_vivo = True
        self.btn_paquete.configure(text="Preparando…", state="disabled")

        def work():
            import pipeline
            try:
                # la ruta ACTUAL del video viaja como localizador: si lo moviste
                # después de extraer, el fingerprint sigue validando la identidad
                rep = pipeline.actualizar_derivados(
                    outdir, event_cb=lambda ev: self.q.put(("deriv_ev", job, gen, ev)),
                    fuente=fuente_actual)
                self.q.put(("deriv_fin", job, gen, rep, None, False))
            except pipeline.SpecNoDisponible as e:
                self.q.put(("deriv_fin", job, gen, None, str(e), True))
            except Exception as e:
                self.q.put(("deriv_fin", job, gen, None, str(e), False))
        threading.Thread(target=work, daemon=True).start()

    # ---------------------------------------------------------------------- pump --
    def _pump(self):
        try:
            while True:
                m = self.q.get_nowait()
                k = m[0]
                # ---- derivados PRIMERO (r1.11): el fin de un job SIEMPRE limpia el
                # estado del worker, aunque el video haya cambiado; la generación solo
                # decide si el RESULTADO se aplica a la UI actual
                if k == "deriv_ev":
                    _, job, gen, ev = m
                    if job != self._deriv_job or gen != self._gen:
                        continue
                    if ev.get("tipo") == "paso":
                        self.lbl_paquete.configure(
                            text=f"{ev.get('etiqueta', '')}: {ev.get('status')}",
                            text_color="gray70")
                    continue
                if k == "deriv_fin":
                    _, job, gen, rep, err, legacy = m
                    if job == self._deriv_job:
                        self._derivados_vivo = False
                        self.btn_paquete.configure(text="Preparar paquete para la AI",
                                                   state="normal")
                    if gen != self._gen:
                        continue
                    if legacy:
                        # r1.12: el estado legacy/stale se DECLARA de verdad en el
                        # header del guion — y solo se ANUNCIA si persistió (r2)
                        declarado, motivo_decl = False, "guion no disponible"
                        if self.doc is not None and self.doc.roto is None:
                            self.doc.paquete_estado = "stale_legacy"
                            try:
                                self.doc.guardar()
                                self._refrescar_guion()
                                declarado = True
                            except Exception as e2:
                                motivo_decl = str(e2)
                        if declarado:
                            self._aviso(f"⚠ proyecto viejo: {err} — el paquete quedó "
                                        "declarado stale_legacy en el header del "
                                        "guion (manda el guion + sidecar).")
                        else:
                            self._aviso(f"⚠ proyecto viejo: {err} — y NO pude "
                                        f"declararlo en el guion ({motivo_decl}); "
                                        "avisale a la AI que manda el guion + sidecar.")
                    elif err:
                        self._aviso(f"⚠ preparar paquete: {err}")
                    elif rep and rep.get("status") == "ok":
                        limpio = True
                        if self.doc is not None and self.doc.paquete_estado:
                            self.doc.paquete_estado = None
                            try:
                                self.doc.guardar()
                                self._refrescar_guion()
                            except Exception:
                                self.doc.paquete_estado = "stale_legacy"  # sigue en disco
                                limpio = False
                        if self.master_path:   # recargar el master fresco
                            self._usar_master(self.master_path, fuerte=True)
                        self._aviso("✓ paquete al día: master + vistas regenerados con "
                                    "tus marcas." if limpio else
                                    "✓ master + vistas regenerados — pero no pude "
                                    "limpiar el stale_legacy del header del guion: "
                                    "guardalo de nuevo desde el panel.")
                    else:
                        self._aviso(f"✗ preparar paquete: {(rep or {}).get('status')} — "
                                    "mirá el log de derivados.")
                    self._estado_paquete()
                    continue
                gen = m[1]
                if gen != self._gen:
                    continue
                if k == "masters":
                    _, _, fuertes, legacy = m
                    if fuertes:
                        self._elegir_master(fuertes, True)
                    elif legacy:
                        self._elegir_master(legacy, False)
                    else:
                        self.lbl_master.configure(
                            text="No encontré un master.json para este video — "
                                 "¿lo elegís a mano?")
                        self._aviso("Sin master: podés marcar y escribir el guion igual; "
                                    "la metadata aparece cuando cargues el master.")
                        self.tab.after(50, self._pick_master_manual)
                elif k == "master":
                    _, _, path, master, streams, avisos, fuerte = m
                    self.master, self.master_path = master, path
                    self.streams = streams
                    self._indices = {n: _Indice(evs) for n, evs in streams.items()}
                    self._calcular_ghost()
                    _recordar_fuente(self.ed.fp, path) if self.ed.fp else None
                    n_ev = sum(len(v) for v in streams.values())
                    hdr = master.get("header") or {}
                    self.lbl_master.configure(
                        text=f"✓ {path.name} · {len(streams)} stream(s) · {n_ev} eventos"
                             + ("" if fuerte else " · match por duración (master viejo)"))
                    if avisos:
                        self._aviso(" · ".join(avisos[:3]))
                    self.btn_paquete.configure(state="normal")
                    self._armar_toggles()
                    self.ed.refrescar_layout()
                    self._estado_paquete()
                elif k == "guion":
                    _, _, doc = m
                    self.doc = doc
                    self.btn_guardar.configure(state="normal")
                    if self.ed.reg is not None and doc.roto is None:
                        try:
                            doc.sincronizar(self.ed.reg, marcas_sha256=self._sha_marcas())
                        except guion_mod.SentinelasRotos:
                            pass
                    self._refrescar_guion(mantener_scroll=False)
                elif k == "err":
                    self._aviso(f"✗ {m[2]}")
        except queue.Empty:
            pass
        self.tab.after(120, self._pump)
