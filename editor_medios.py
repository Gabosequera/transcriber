#!/usr/bin/env python3
"""
editor_medios.py — el MINI-EDITOR reutilizable: preview de video + transporte +
timeline multi-pista (waveform blitteada + tiles por zoom) + carril de MARCAS del
autor + reproducción real (SesionVideo + mezcla ffplay) + prefetch de scrub.

Extraído TAL CUAL de wizard_extraer.WizardExtraer (etapa 1 del refactor A1,
consenso Codex three-brain-out/2026-07-21-tab-marcar/DISENO-final.md): el wizard
y la tab «Marcar» instancian ESTE componente; toda la lógica de reproducción,
navegación y marcas vive acá y en medios.py/marcas.py. Los diseños previos que
gobiernan este código: 2026-07-16-timeline-marcas (marcas + viewport + tiles) y
2026-07-20-reproductor-optimizacion (sesión de video, warm-up, prefetch, blit).

Contrato con el dueño (wizard / tab Marcar):
  · `EditorMedios(parent, ...)` crea `self.f` (frame); el dueño lo grid-ea.
  · `cargar(path)` inspecciona y arma todo (async);  `activar()`/`desactivar()`
    al entrar/salir de la vista (desactivar PAUSA reproducción y timers);
    `cerrar()` al cerrar la app.
  · hooks:  `controles_pista_extra(fila, pista, i)` — widgets extra por pista
    (nombre/rol del wizard);  `overlay_preview(canvas, geo)` — dibujo encima del
    frame (rects del wizard);  `carriles_extra()` → [{"alto", "dibujar", "gesto"?}] —
    carriles entre el carril de marcas y las pistas (metadata de la tab Marcar,
    bloques y recortes de Automático). Un carril con `gesto(fase, e, g, y0)`
    (fase: press/motion/release/doble) recibe el botón izquierdo cuando cae sobre
    él; si `press` devuelve True el gesto queda CAPTURADO por ese carril hasta
    soltar y el playhead no se mueve;  `acciones_extra(action, e)` → True si el
    dueño consumió la ACCIÓN del keymap (se consulta ANTES que las marcas; los ids
    están en keymap.ACTIONS);  `on_video_cargado(info,
    fp)` — tras la inspección, ANTES de armar pistas;  `on_playhead(t)` — cambio
    del playhead. La columna 4 del transporte y la fila 4 de `self.f` (entre el
    timeline y el status, que tiene altura FIJA) quedan libres para widgets del
    dueño (chk_conf del wizard; barra de detalle de capas de Automático). Los cambios de MARCAS se
    observan suscribiéndose al Registro compartido (Registro.suscribir) — no hay
    hook propio (review impl r1.7: un solo canal, sin duplicados).
"""
from __future__ import annotations

import math
import queue
import threading
import time
from collections import OrderedDict
from pathlib import Path

import customtkinter as ctk

import hardware
import marcas as marcas_mod
import medios

PREVIEW_H = 240                                # altura INICIAL del preview (después es responsivo)
RULER_H = 20                                   # altura del ruler del timeline
MARKS_H = 22                                   # carril de MARCAS del autor (bajo el ruler)
LANE_H = 46                                    # altura de cada carril de pista

# ---- marcas / viewport (diseño consensuado: three-brain-out/2026-07-16-timeline-marcas) --
COL_MARCA = {None: "#e8b34b", "incluir": "#4ade80", "excluir": "#ff5252"}
SPAN_MIN = 2.0                                 # s — zoom máximo (viewport mínimo)
TILE_B = 1024                                  # buckets por tile de envolvente
NIVEL_MAX = 12                                 # 2^12 = 4096 buckets/s (techo del pipe 8 kHz)
TILES_LRU = 256                                # tiles cacheados (consenso r2 q.5)
FCACHE_MB = 64                                 # presupuesto del caché de frames (r1.11)
WARMUP_MAX_S = 10.0                            # al vencer se detiene; nunca audio sin video listo
RADIO_PUNTO = 2.0                              # s — al convertir punto→región (regla `x`)
SPEEDS = (1.0, 2.0, 3.0, 4.0, 8.0)             # velocidades de reproducción (diseño §1)
RATE_DEBOUNCE_MS = 150                         # pulsar L tres veces seguidas = UNA re-sesión


class EditorMedios:
    """Mini-editor autocontenido. Maneja su PROPIA cola de eventos (los threads
    nunca tocan Tk) y su propio ciclo `after`."""

    def __init__(self, parent, *, ancho_ctl=330, controles_pista_extra=None,
                 overlay_preview=None, carriles_extra=None, on_video_cargado=None,
                 on_playhead=None, acciones_extra=None, marcas_en_capas=False):
        self._marks_height = 0 if marcas_en_capas else MARKS_H
        self.controles_pista_extra = controles_pista_extra
        self.overlay_preview = overlay_preview
        self.carriles_extra = carriles_extra
        self.on_video_cargado = on_video_cargado
        self.on_playhead = on_playhead
        self.acciones_extra = acciones_extra
        self._drag_extra = None                # (gesto, y0) del carril extra que capturó B1

        self.q: queue.Queue = queue.Queue()
        self._gen = 0                          # token de generación (cambio de video)
        self.info: dict | None = None          # inspección del video actual
        self.fp: dict | None = None
        self.repro = medios.reproductor()
        self._frame_pil = None                 # frame ACTUAL en PIL (se re-escala al resize)
        self._frame_img = None                 # referencia viva del PhotoImage (si no, blanco)
        self.t_play = 0.0                      # el PLAYHEAD: un solo tiempo para audio+video
        self.rate = 1.0                        # velocidad de reproducción (SPEEDS)
        self._rate_after = None                # debounce del cambio de velocidad
        self._envs: dict[int, list] = {}       # envolventes GLOBALES por pista (fit)
        self._ancla = (0.0, 0.0)               # (t, reloj) del último play
        self._ph = None                        # item del playhead en el canvas del timeline
        # ---- viewport + marcas + workers (consenso timeline-marcas) ----
        self.view = [0.0, 0.0]                 # [t0, span] visible del timeline
        self.reg: marcas_mod.Registro | None = None   # marcas del autor del video actual
        self.sel_marca: dict | None = None
        self._pend_in: float | None = None     # in pendiente (tecla i)
        self._drag_marca = None                # gesto activo sobre el carril de marcas
        self._tiles: OrderedDict = OrderedDict()      # (pista,nivel,idx) → env (LRU)
        self._tiles_ver = 0                    # versión de datos (invalida el cache de imgs)
        self._tile_req = None
        self._tile_cond = threading.Condition()
        self._cancel_medios = threading.Event()       # cancela envolventes/tiles del video VIEJO
        self._frames = medios.FrameWorker()    # UN worker de frames, último pedido gana
        # ---- reproductor de video real + prefetch (diseño 2026-07-20-reproductor) ----
        self._vses: medios.SesionVideo | None = None   # sesión de STREAMING del play
        self._warmup = None                    # (t, reloj): esperando 1er frame
        self._frame_rev = 0                    # clave del cache de resize (r1.9, no id())
        self._canvas_img = None                # item persistente de imagen del preview
        self._fcache: OrderedDict = OrderedDict()      # (t_grid, W, H) → PIL (scrub)
        self._fcache_bytes = 0
        from preview_cache import FrameCache
        self._exact_frames = FrameCache()
        self._wave_view_cache = OrderedDict()
        self._prefetch = medios.Prefetcher(
            lambda tok, tg, w, img: self.q.put(("fcache", tok, tg, w, img)))
        self._prefetch_after = None
        self._env_prog: dict[int, float] = {}  # progreso de envolvente por pista
        self._env_draw_t = 0.0                 # throttle de redibujo por progreso
        self._info_line = ""                   # línea base del status (se le anexa estado)
        self._carga_t0 = 0.0
        self._preview_s = None                 # s hasta «preview listo» (r1.16)
        self._wave_s = None                    # s hasta waveforms completas
        self._preview_epoch = 0                # token de sesión de preview (r2.3)
        self._play_metrics = {}
        self._wave_imgs: dict[int, object] = {}       # PhotoImage por fila (refs VIVAS)
        self._wave_keys: dict[int, tuple] = {}
        self._pista_ui: list[dict] = []        # widgets por pista (mute/solo + extra del dueño)
        self._resize_after = None
        self._resize_activo = False            # gesto de resize vivo → BILINEAR (r4)
        self._reasoc_armado = False
        threading.Thread(target=self._tile_loop, daemon=True, name="tiles").start()

        # ================================================================ UI ----
        import tkinter as tk
        self.f = ctk.CTkFrame(parent, fg_color="transparent")
        self.f.grid_columnconfigure(0, weight=1)
        self.f.grid_rowconfigure(0, weight=3)  # el preview crece con la ventana
        self.f.grid_rowconfigure(2, weight=1)  # el timeline también

        # ---- lienzo (preview) — altura RESPONSIVA al resize ----
        self.canvas = tk.Canvas(self.f, height=PREVIEW_H, bg="#101010", highlightthickness=0)
        self.canvas.grid(row=0, column=0, sticky="nsew", padx=8, pady=(0, 4))
        # PRIMERO el debounce (marca _resize_activo → el redibujo inmediato re-escala
        # con BILINEAR, r5), después el redibujo del layout; _resize_fire purga el
        # caché y reinicia el stream si el letterbox cambió de verdad (r3.4)
        self.canvas.bind("<Configure>", lambda e: (self._tl_resize(), self._redibujar()))

        # ---- transporte (un solo play para la mezcla; el playhead manda) ----
        self.fr_transporte = ctk.CTkFrame(self.f, fg_color="transparent")
        self.fr_transporte.grid(row=1, column=0, sticky="ew", padx=8)
        self.fr_transporte.grid_columnconfigure(3, weight=1)
        self.btn_play = ctk.CTkButton(self.fr_transporte, text="▶", width=44, command=self._play)
        self.btn_play.grid(row=0, column=0, padx=(0, 8), pady=2)
        self.lbl_t = ctk.CTkLabel(self.fr_transporte, text="0:00.0", width=70,
                                  font=ctk.CTkFont(size=13, weight="bold"))
        self.lbl_t.grid(row=0, column=1, padx=(0, 10))
        ctk.CTkLabel(self.fr_transporte,
                     text="←/→ mover (Shift ±5s) · espacio play · J/K/L velocidad · +/− zoom "
                          "· rueda pan (Ctrl=zoom) · Shift+Z todo · M marca · I/O región "
                          "· X decisión · Supr borra · atajos en Ajustes",
                     text_color="gray55", font=ctk.CTkFont(size=11)).grid(row=0, column=2)
        # (columna 4 del fr_transporte queda LIBRE para widgets del dueño — chk_conf)

        # ---- TIMELINE unificado (estilo editor): controles por pista | carriles ----
        tlf = ctk.CTkFrame(self.f)
        tlf.grid(row=2, column=0, sticky="nsew", padx=4, pady=(4, 4))
        tlf.grid_columnconfigure(1, weight=1)
        tlf.grid_rowconfigure(0, weight=1)
        self.f_ctl = ctk.CTkFrame(tlf, fg_color="transparent", width=ancho_ctl)
        self.f_ctl.grid(row=0, column=0, sticky="ns", padx=(8, 4), pady=6)
        self.f_ctl.grid_propagate(False)
        self._ancho_ctl = ancho_ctl
        self.tl = tk.Canvas(tlf, height=RULER_H + self._marks_height + LANE_H, bg="#121212",
                            highlightthickness=0)
        self.tl.grid(row=0, column=1, sticky="nsew", padx=(0, 8), pady=6)
        self.tl.bind("<Button-1>", self._tl_press)
        self.tl.bind("<B1-Motion>", self._tl_motion)
        self.tl.bind("<ButtonRelease-1>", self._tl_release)
        self.tl.bind("<Double-Button-1>", self._tl_doble)
        # pan con arrastre del botón del medio + rueda (pan; Ctrl = zoom en el cursor)
        self.tl.bind("<Button-2>", self._tl_pan_ini)
        self.tl.bind("<B2-Motion>", self._tl_pan_mov)
        self.tl.bind("<MouseWheel>", self._tl_rueda)            # Windows/macOS
        self.tl.bind("<Button-4>", self._tl_rueda)              # Linux ↑
        self.tl.bind("<Button-5>", self._tl_rueda)              # Linux ↓
        # resize con DEBOUNCE (no redibujar por cada pixel del drag — consenso q.6)
        self.tl.bind("<Configure>", self._tl_resize)
        # ---- teclado: keymap configurable (diseño §2) ----
        # Un solo <Key> en el TOPLEVEL (add=True, nunca bind_all) con GUARDA DE FOCO:
        # solo despacha si el foco está en un Canvas/Frame/Label o el propio toplevel;
        # nunca en un Entry/Text (m/i/o/x/letras/números se siguen escribiendo) ni en
        # Button/Checkbox/OptionMenu (space/Return los activan). Así las teclas
        # funcionan sin hacer click en el canvas antes. Solo el editor ACTIVO despacha
        # (hay tres en la app: wizard, Marcar, Automático).
        self._keys_activos = False
        self._handlers = self._armar_handlers()
        top = self.f.winfo_toplevel()
        top.bind("<Key>", self._key_toplevel, add=True)
        # click izquierdo en cualquier parte NO interactiva del editor → foco al timeline
        top.bind("<Button-1>", self._click_toplevel, add=True)

        # ---- panel NO MODAL de la marca seleccionada (consenso r2 h.8) ----
        self.f_marca = ctk.CTkFrame(self.f)
        self.f_marca.grid(row=3, column=0, sticky="ew", padx=4, pady=(0, 2))
        self.f_marca.grid_columnconfigure(2, weight=1)
        self.lbl_marca = ctk.CTkLabel(self.f_marca, text="", width=170, anchor="w",
                                      font=ctk.CTkFont(size=12, weight="bold"))
        self.lbl_marca.grid(row=0, column=0, padx=(12, 8), pady=8)
        self.seg_marca = ctk.CTkSegmentedButton(self.f_marca,
                                                values=["nota", "incluir", "excluir"],
                                                command=self._marca_decision)
        self.seg_marca.grid(row=0, column=1, padx=(0, 10))
        self.e_prompt = ctk.CTkEntry(self.f_marca,
                                     placeholder_text="Prompt para la AI (opcional): "
                                                      "«de aquí a aquí quiero que…»")
        self.e_prompt.grid(row=0, column=2, sticky="ew", padx=(0, 10))
        self.e_prompt.bind("<Return>", lambda e: (self._marca_prompt(), self.tl.focus_set()))
        self.e_prompt.bind("<FocusOut>", lambda e: self._marca_prompt())
        self.e_prompt.bind("<Escape>", lambda e: self.tl.focus_set())
        ctk.CTkButton(self.f_marca, text="🗑 Borrar", width=80, fg_color="#7a2e2e",
                      hover_color="#8f3838", command=self._marca_borrar).grid(
            row=0, column=3, padx=(0, 12))
        self.f_marca.grid_remove()             # aparece con la selección

        # (la fila 4 de self.f queda LIBRE para un widget del dueño — la barra de
        #  detalle de capas de Automático va ahí, entre el timeline y el status)

        # ---- status (línea de estado del editor) + reasociación ----
        # Altura FIJA (dos renglones): el texto cambia todo el tiempo (carga,
        # progreso, reproducción) y si el pie creciera o encogiera con cada mensaje,
        # el timeline y el preview (filas elásticas) saltarían. Lo que no entra se
        # recorta abajo; nunca empuja el layout.
        self._status_wrap_w = 0
        pie = ctk.CTkFrame(self.f, fg_color="transparent",
                           height=2 * ctk.CTkFont().metrics("linespace") + 4)
        pie.grid(row=5, column=0, sticky="ew", padx=10, pady=(0, 6))
        pie.grid_propagate(False)
        pie.grid_columnconfigure(0, weight=1)
        pie.grid_rowconfigure(0, weight=1)
        self.lbl_status = ctk.CTkLabel(pie, text="Elegí el video: se detectan las pistas "
                                                 "y se arma el timeline.",
                                       text_color="gray60", anchor="nw", justify="left",
                                       wraplength=680)
        self.lbl_status.grid(row=0, column=0, sticky="nsew")
        # el wrap sigue al ancho real de la celda (ventana ancha → un renglón; angosta
        # → no desborda), con umbral para no realimentar <Configure> por píxel
        self.lbl_status.bind("<Configure>", self._status_wrap)
        # botón de REASOCIACIÓN (impl h.5): aparece solo si el sidecar es de otro video;
        # 2 clicks — el primero muestra el conteo (consenso r2 q.1), el segundo ejecuta
        self.btn_reasociar = ctk.CTkButton(pie, text="Reasociar marcas a este video",
                                           width=210, fg_color="#7a5a1e",
                                           hover_color="#8f6a24",
                                           command=self._reasociar_click)
        self.btn_reasociar.grid(row=0, column=1, padx=(8, 0))
        self.btn_reasociar.grid_remove()
        # conflicto de sidecar SECUNDARIO (misma revisión, bytes distintos — r1.9):
        # la adopción es SIEMPRE explícita, nunca silenciosa (consenso r3)
        self.btn_adoptar = ctk.CTkButton(pie, text="Adoptar el de esta copia",
                                         width=190, fg_color="#8f3838",
                                         hover_color="#a34242",
                                         command=self._adoptar_click)
        self.btn_adoptar.grid(row=0, column=2, padx=(8, 0))
        self.btn_adoptar.grid_remove()

        self.f.after(80, self._pump)

    def _status_wrap(self, _e=None):
        w = max(200, self.lbl_status.winfo_width() - 8)
        if abs(w - self._status_wrap_w) > 12:
            self._status_wrap_w = w
            self.lbl_status.configure(wraplength=w)

    # ============================================================ API pública ----
    def status(self, m):
        self.lbl_status.configure(text=m)

    def geo(self):
        return self._geo()

    def redibujar(self):
        self._redibujar()

    def refrescar_layout(self):
        """El dueño cambió los carriles extra (p.ej. cargó el master): recalcular
        alturas del espaciador/canvas y redibujar."""
        if self.info:
            self._ajustar_altura()
            self._dibujar_timeline()

    def activar(self):
        """La vista del editor volvió a estar visible: despacha las teclas."""
        self._keys_activos = True
        if self.info:
            self._dibujar_timeline()

    def desactivar(self):
        """Salir de la vista apaga TODO el preview aunque esté pausado (r3.3):
        también el prefetch y su timer, que si no compiten con otros trabajos."""
        self._keys_activos = False
        self._stop_preview()
        self.btn_play.configure(text="▶")

    def cerrar(self):
        """Cleanup al cerrar la app: cortar audición + stream + prefetch y cancelar
        envolventes/tiles."""
        try:
            self._stop_preview()               # audio, stream, prefetch, timers (r1.13)
        except Exception:
            pass
        self._cancel_medios.set()
        if self.reg is not None:
            self.reg.desuscribir(self._on_reg_cambio)

    # ================================================================= carga ----
    def _on_reg_cambio(self):
        """Suscripción al Registro compartido: OTRA vista mutó las marcas (o esta —
        redibujo redundante barato). Si la seleccionada ya no existe, deseleccionar."""
        if self.reg is not None and self.sel_marca is not None \
                and self.sel_marca not in self.reg.marcas:
            self._seleccionar(None)
        if self.info:
            self._dibujar_timeline()

    def cargar(self, path):
        self._gen += 1                 # token: descarta callbacks del video anterior
        gen = self._gen
        self._cancel_medios.set()      # cancela DE VERDAD los ffmpeg del video viejo
        self._cancel_medios = threading.Event()
        cancel = self._cancel_medios
        self._stop_preview()           # audio + stream + prefetch + timers (r1.13)
        self.btn_play.configure(text="▶")
        self.info = self.fp = None
        self._frame_pil = None
        self.t_play = 0.0
        self.view = [0.0, 0.0]
        if self.reg is not None:       # la instancia es compartida: soltar la suscripción
            self.reg.desuscribir(self._on_reg_cambio)
        self.reg = None
        self._seleccionar(None)
        self._pend_in = None
        self._tiles.clear()
        self._tiles_ver += 1
        self._wave_keys.clear()
        self._wave_imgs.clear()        # refs de PhotoImage del video anterior (impl h.9)
        self._fcache.clear()
        self._fcache_bytes = 0
        self._exact_frames.clear()
        self._wave_view_cache.clear()
        self._envs.clear()
        self._env_prog = {}
        self._info_line = ""
        self._preview_s = self._wave_s = None
        self._carga_t0 = time.monotonic()
        self.btn_reasociar.grid_remove()
        self.btn_adoptar.grid_remove()
        self.status("Inspeccionando el video…")
        epoch = self._preview_epoch

        def work():
            try:
                info = medios.inspeccionar(path)
                fp = medios.fingerprint(path, info)
                self.q.put(("insp", gen, info, fp))
                if info.get("video"):
                    # el frame inicial TAMBIÉN va por el worker (cancelable/last-wins);
                    # el frame_preview síncrono acá dejaba ffmpeg de hasta 60s vivos al
                    # cambiar rápido de video (review impl h.6)
                    self._frames.pedir(path, max(0.0, info["duracion"] * 0.1), info,
                                       (gen, epoch),
                                       lambda tok, tr, img, costo:
                                       self.q.put(("frame", tok, None, img, costo)))
                for pista in info["pistas"]:
                    # 4000 buckets globales (vista «todo»); el ZOOM pide tiles aparte.
                    # Con PROGRESO a la consola (r1.15) — throttled en medios.
                    from preview_cache import envelope
                    env = envelope(path, pista["idx"], fp, info["duracion"], buckets=4000,
                                            cancel=cancel,
                                            progress=lambda f, i=pista["idx"]:
                                            self.q.put(("envprog", gen, i, f)))
                    if cancel.is_set():
                        return
                    self.q.put(("env", gen, pista["idx"], env))
                self.q.put(("envfin", gen))
            except Exception as e:
                self.q.put(("err1", gen, str(e)))
        threading.Thread(target=work, daemon=True).start()

    def _refrescar_status(self):
        """Estado COMPUESTO (r1.15/r1.16): línea del archivo + readiness interactiva
        («preview listo»: play/scrub ya andan) + progreso/fin de las waveforms (que
        cargan en segundo plano y NO bloquean nada)."""
        if not self._info_line:
            return
        partes = [self._info_line]
        estado = []
        if self._preview_s is not None:
            estado.append(f"✓ preview listo en {self._preview_s:.1f}s")
        if self.info:
            pistas = self.info["pistas"]
            hechas = sum(1 for p in pistas if p["idx"] in self._envs)
            if hechas < len(pistas):
                cur = next((p["idx"] for p in pistas if p["idx"] not in self._envs), None)
                frac = self._env_prog.get(cur, 0.0)
                txt = f"⏳ waveforms {hechas}/{len(pistas)} · pista {cur + 1}: {frac:.0%}"
                if len(pistas) - hechas > 1:
                    txt += " · resto esperando"
                estado.append(txt)
            elif self._wave_s is not None:
                estado.append(f"✓ waveforms completas en {self._wave_s:.0f}s")
        if estado:
            partes.append("   ".join(estado))
        self.lbl_status.configure(text="\n".join(partes))

    # ---- playhead / timeline (UNA línea de tiempo para audio y video) ----
    def _set_playhead(self, t, frame=True):
        """Mueve el playhead a `t` segundos: actualiza el reloj, la línea en el timeline
        y (con debounce corto) el frame del preview — audio y video comparten ESTE tiempo.
        Pausado: primero intenta el CACHÉ de prefetch (display instantáneo del scrub) y
        re-apunta la ventana de prefetch con debounce."""
        dur = self.info["duracion"] if self.info else 0.0
        self.t_play = min(max(0.0, t), max(dur - 0.05, 0.0))
        self.lbl_t.configure(text=self._texto_reloj())
        if self.on_playhead:
            self.on_playhead(self.t_play)
        if self._asegurar_visible(self.t_play):
            self._dibujar_timeline()
        else:
            self._mover_linea_playhead()
        if frame and self.info and self.info.get("video") and not self._playback_activo():
            self._t_pedido = self.t_play
            W,_ = self._dims_preview()
            width=min(max(480,W),self.info['video'].get('display_width',self.info['video']['width']))
            exact=self._exact_frames.get(self.t_play,width)
            if exact is not None:
                self._mostrar_frame(exact)
                return
            hit = self._fcache_hit(self.t_play)
            if hit is not None:
                self._frame_pil = hit
                self._frame_rev += 1
                self._redibujar()              # aproximado YA; el exacto llega y pisa
            self._t_pedido = self.t_play
            self.f.after(120, self._pedir_frame, self.t_play)
            self._reapuntar_prefetch()

    def _texto_reloj(self) -> str:
        """«0:12.3», y «0:12.3 ×2» cuando la velocidad no es la normal (diseño §1)."""
        base = f"{int(self.t_play // 60)}:{self.t_play % 60:04.1f}"
        return base if abs(self.rate - 1.0) < 1e-9 else f"{base} ×{self.rate:g}"

    # ---- velocidad de reproducción (diseño docs/diseno-navegacion-editor.md §1) ----
    def set_rate(self, rate):
        """Fija la velocidad (una de SPEEDS). Pausado: la guarda y la muestra.
        Reproduciendo: UNA re-sesión desde `t_play` con debounce de 150 ms (pulsar L
        tres veces seguidas = un solo reinicio), por el mismo camino y token que
        `_remezclar_debounced`."""
        rate = min(SPEEDS, key=lambda s: abs(s - float(rate)))
        if abs(rate - self.rate) < 1e-9:
            return
        self.rate = rate
        self.lbl_t.configure(text=self._texto_reloj())
        if not self._playback_activo():
            self.status(f"velocidad ×{rate:g} (al reproducir)")
            return
        self._remix_n = getattr(self, "_remix_n", 0) + 1
        n = self._remix_n
        destino = self.t_play

        def fire():
            if n == self._remix_n and self._playback_activo():
                self._play(reiniciar=True, desde=destino)
        self.f.after(RATE_DEBOUNCE_MS, fire)

    def _rate_step(self, delta: int):
        """L (+1) / J (−1) sobre SPEEDS: pausado, L reproduce a ×1; a ×1, J pausa."""
        if not self.info:
            return
        if delta > 0 and not self._playback_activo():
            self.rate = 1.0
            self._play()
            return
        i = SPEEDS.index(self.rate) if self.rate in SPEEDS else 0
        j = i + delta
        if j < 0:
            if self._playback_activo():
                self._play()                   # ×1 y J = pausa
            return
        self.set_rate(SPEEDS[min(j, len(SPEEDS) - 1)])

    def _pedir_frame(self, t):
        """Frame EXACTO del preview vía el WORKER único (último pedido gana, mata el
        ffmpeg activo — consenso q.7). No-op durante playback (r1.3: ahí alimenta el
        stream). El token viaja con (gen, epoch) — r2.3."""
        if not self.info or not self.info.get("video") or getattr(self, "_t_pedido", None) != t \
                or self._playback_activo():
            return
        W, _ = self._dims_preview()
        self._frames.pedir(self.info["path"], t, self.info,
                           (self._gen, self._preview_epoch),
                           lambda tok, tr, img, costo:
                           self.q.put(("frame", tok, tr, img, costo)),
                           max_w=max(480, W))

    # ---- reproductor de video real: sesión, apagado único y prefetch ----
    # (diseño three-brain-out/2026-07-20-reproductor-optimizacion/, v2 + addendum v3)
    def _playback_activo(self) -> bool:
        return self._vses is not None or self.repro.playing() or self._warmup is not None

    def _stop_preview(self, audio=True):
        """Rutina ÚNICA de apagado del preview (r1.13/r2.13): invalida el tick y el
        epoch, cancela los debounce del preview, mata stream + prefetch y (opcional)
        el audio. La llaman: play/stop/replay, cambio de video, desactivar() y el
        cierre de la app."""
        self._anim_tok = getattr(self, "_anim_tok", 0) + 1   # mata tick + warm-up
        self._preview_epoch += 1               # invalida callbacks tardíos (r2.3)
        self._warmup = None
        self._t_pedido = None                  # invalida el after de _pedir_frame
        self._remix_n = getattr(self, "_remix_n", 0) + 1     # invalida re-mezclas
        self._frames.cancelar()
        if self._vses is not None:
            self._vses.parar()
            self._vses = None
        self._prefetch.parar()
        if self._prefetch_after:
            try:
                self.f.after_cancel(self._prefetch_after)
            except Exception:
                pass
            self._prefetch_after = None
        if audio:
            self.repro.stop()

    def _dims_preview(self):
        """(W,H) objetivo del preview = rect del letterbox EXACTO en pares (r2.8), con
        solo un MÁXIMO de 1440. Fallback 960×540 si el canvas aún no midió (sin mapear
        winfo_width da 1 y _geo lo clampea a ~50 — umbral 100 lo cubre)."""
        g = self._geo()
        if not g or g[2] < 100:
            return 960, 540
        _, _, iw, ih = g
        W = int(min(iw, 1440)) // 2 * 2
        H = max(2, int(round(ih * (W / iw))) // 2 * 2)
        return max(2, W), H

    def _mostrar_frame(self, img):
        """Frame del STREAM durante playback: actualiza SOLO el item de imagen del
        canvas (r1.9) — los overlays no se recrean. Si el frame no coincide con el
        letterbox (degradación, tope de 1440 o resize aún no asentado) se escala
        BARATO (BILINEAR) y se sigue por el item persistente — NUNCA un redibujo
        total por frame (r4). LANCZOS queda para la pausa."""
        from PIL import Image, ImageTk
        self._frame_pil = img
        self._frame_rev += 1
        g = self._geo()
        if not g:
            return
        ox, oy, iw, ih = g
        dims = (max(1, int(iw)), max(1, int(ih)))
        if img.size != dims:
            img = img.resize(dims, Image.BILINEAR)
        self._frame_img = ImageTk.PhotoImage(img)             # ref viva
        self._frame_clave = (self._frame_rev, int(iw), int(ih))
        if self._canvas_img is None:
            self._redibujar()                  # (re)crea la pila de items una vez
        else:
            self.canvas.coords(self._canvas_img, ox, oy)
            self.canvas.itemconfigure(self._canvas_img, image=self._frame_img)

    def _restart_audio(self):
        """Mute/solo DURANTE playback (r2.22): re-arranca SOLO la mezcla desde el
        playhead actual y re-ancla; el stream de video y el tick vivos siguen. Mezcla
        vacía o ffplay caído → sesión entera abajo (nunca un stream sin dueño)."""
        pistas = self._activas()
        motivo = self.repro.play(self.info["path"], pistas, self.t_play, rate=self.rate) \
            if pistas else "no hay pistas activas (todo muteado)"
        if motivo:
            self._stop_preview()
            self.btn_play.configure(text="▶")
            self.status(f"⚠ No puedo reproducir: {motivo}")
            return
        self._ancla = (self.t_play, time.monotonic())
        self._play_metrics = dict(start=self.t_play,rate=self.rate,audio_clock_ms=None)

    def _fcache_hit(self, t):
        """Frame prefeteado más cercano al playhead (±1 celda del grid) con las dims
        actuales — display instantáneo del drag. El frame EXACTO del worker lo pisa."""
        W, H = self._dims_preview()
        gr = medios.Prefetcher.GRID
        tg = round(t / gr) * gr
        for cand in (tg, tg - gr, tg + gr):
            img = self._fcache.get((cand, W, H))
            if img is not None and abs(cand - t) <= gr / 2 + 0.6:
                self._fcache.move_to_end((cand, W, H))
                return img
        return None

    def _fcache_put(self, t_grid, img):
        """LRU por BYTES reales (r1.11): presupuesto FCACHE_MB, purga desde el más viejo."""
        clave = (t_grid, img.width, img.height)
        viejo = self._fcache.pop(clave, None)
        if viejo is not None:
            self._fcache_bytes -= viejo.width * viejo.height * 3
        self._fcache[clave] = img
        self._fcache_bytes += img.width * img.height * 3
        while self._fcache_bytes > FCACHE_MB * 1024 * 1024 and len(self._fcache) > 1:
            _, v = self._fcache.popitem(last=False)
            self._fcache_bytes -= v.width * v.height * 3

    def _fcache_purgar_dims(self):
        """Resize del preview: las entradas con dims viejas ya no sirven (r1.11)."""
        W, H = self._dims_preview()
        muertas = [k for k in self._fcache if (k[1], k[2]) != (W, H)]
        for k in muertas:
            v = self._fcache.pop(k)
            self._fcache_bytes -= v.width * v.height * 3

    def _reapuntar_prefetch(self):
        """Debounce de 600 ms tras el último movimiento del playhead PARADO."""
        if self._prefetch_after:
            try:
                self.f.after_cancel(self._prefetch_after)
            except Exception:
                pass
        self._prefetch_after = self.f.after(600, self._prefetch_fire)

    def _prefetch_fire(self):
        self._prefetch_after = None
        if not self.info or not self.info.get("video") or self._playback_activo() \
                or self._drag_marca is not None:
            return
        W, H = self._dims_preview()
        self._prefetch.apuntar(self.info["path"], self.t_play, W, H,
                               (self._gen, self._preview_epoch),
                               dur_total=self.info["duracion"])

    # ---- viewport (zoom/pan estilo editor — consenso q.4/q.5) ----
    def _tl_geo(self):
        """Mapeo x↔tiempo del área de carriles (margen fijo a la izquierda del ruler)."""
        if not self.info or not self.info["duracion"]:
            return None
        w = max(self.tl.winfo_width(), 60)
        return 4, max(w - 8, 10)               # x0, ancho útil

    def _t2x(self, t, g):
        x0, ancho = g
        t0, span = self.view
        return x0 + (t - t0) / span * ancho

    def _x2t(self, x, g):
        x0, ancho = g
        t0, span = self.view
        return t0 + (x - x0) / ancho * span

    def _clamp_view(self):
        dur = self.info["duracion"]
        self.view[1] = min(max(self.view[1], SPAN_MIN), dur) if dur > SPAN_MIN else dur
        self.view[0] = min(max(0.0, self.view[0]), dur - self.view[1])

    def _zoom(self, factor, centro=None):
        """factor >1 acerca. `centro` (s) queda FIJO en pantalla (playhead o cursor)."""
        if not self.info:
            return
        t0, span = self.view
        c = self.t_play if centro is None else centro
        c = min(max(c, t0), t0 + span)
        nuevo = span / factor
        self.view = [c - (c - t0) * (nuevo / span), nuevo]
        self._clamp_view()
        self._dibujar_timeline()

    def _pan(self, dt):
        if not self.info:
            return
        self.view[0] += dt
        self._clamp_view()
        self._dibujar_timeline()

    def _fit(self):
        if self.info:
            self.view = [0.0, self.info["duracion"]]
            self._dibujar_timeline()

    def _asegurar_visible(self, t) -> bool:
        """Auto-scroll por SALTOS de página (no re-render continuo — consenso q.6):
        corre la vista solo cuando el playhead se acerca al borde. True si movió."""
        if not self.info:
            return False
        t0, span = self.view
        if span <= 0 or span >= self.info["duracion"] - 1e-6:
            return False
        if t > t0 + span * 0.92:
            self.view[0] = t - span * 0.1
        elif t < t0:
            self.view[0] = max(0.0, t - span * 0.1)
        else:
            return False
        self._clamp_view()
        return True

    def _tl_resize(self, _e=None):
        """<Configure> con debounce: el drag de resize dispara decenas de eventos —
        se redibuja UNA vez cuando el gesto se asienta (consenso r1 E.2). Mientras
        el gesto está vivo, _redibujar re-escala con BILINEAR (r4: nada de LANCZOS
        síncrono por evento)."""
        self._resize_activo = True
        if self._resize_after:
            self.f.after_cancel(self._resize_after)
        self._resize_after = self.f.after(120, self._resize_fire)

    def _resize_fire(self):
        self._resize_after = None
        self._resize_activo = False
        self._dibujar_timeline()
        self._frame_clave = None               # re-render final del frame con LANCZOS
        self._redibujar()
        self._fcache_purgar_dims()             # el caché con dims viejas no sirve (r1.11)
        # resize GRANDE con play activo: la sesión re-arranca al nuevo tamaño (diseño D);
        # cambios chicos siguen con el stream actual (PhotoImage 1px off es invisible)
        if self._vses is not None and not self._vses.fallida:
            W, _ = self._dims_preview()
            if abs(W - self._vses.w) > self._vses.w * 0.15:
                self._play(reiniciar=True, desde=self.t_play)

    def _remezclar_debounced(self, destino):
        """Scrub/teclas DURANTE playback: la re-mezcla (matar y recrear ffmpeg+ffplay)
        se hace UNA vez cuando el gesto se asienta (300ms), no por cada pixel del drag
        (review timeline, h.1). `destino` viaja CAPTURADO: durante la espera _anim_tick
        sigue pisando t_play desde el reloj — sin esto se remezclaba desde el tiempo
        avanzado, no desde donde el usuario apuntó (review, ronda 2)."""
        self._remix_n = getattr(self, "_remix_n", 0) + 1
        n = self._remix_n

        def fire():
            if n == self._remix_n and self._playback_activo():
                self._play(reiniciar=True, desde=destino)
        self.f.after(300, fire)

    # ---- interacción del timeline: B1 rutea por carril (consenso q.4) ----
    def _tl_press(self, e):
        g = self._tl_geo()
        if not g:
            return
        self.tl.focus_set()                    # habilita el teclado del editor
        self._drag_marca = None
        if self._marks_height and RULER_H <= e.y <= RULER_H + self._marks_height and self.reg is not None:
            t = self._x2t(e.x, g)
            hit = self._marca_hit(e.x, e.y, g)
            if hit:
                m, modo = hit
                self._seleccionar(m)
                base = (dict(m), t)
                self._drag_marca = {"modo": modo, "m": m, "base": base, "movio": False}
            else:
                self._seleccionar(None)
                self._drag_marca = {"modo": "crear", "t0": t, "t1": t, "movio": False}
            self._dibujar_timeline()
            return
        hit = self._carril_en(e.y)
        if hit is not None and hit[0].get("gesto"):
            try:
                consumido = bool(hit[0]["gesto"]("press", e, g, hit[1]))
            except Exception:
                consumido = False
            if consumido:                      # el carril del dueño capturó el gesto
                self._drag_extra = (hit[0]["gesto"], hit[1])
                return
        self._scrub(e, g)

    def _carril_en(self, y):
        """(carril, y0) del carril EXTRA del dueño bajo la coordenada `y`, o None."""
        y0 = RULER_H + self._marks_height
        for c in self._carriles():
            alto = int(c.get("alto", 0))
            if y0 <= y < y0 + alto:
                return c, y0
            y0 += alto
        return None

    def _scrub(self, e, g):
        t = self._x2t(e.x, g)
        self._set_playhead(t)
        if self._playback_activo():            # saltar ahí, con debounce del gesto
            self._remezclar_debounced(t)

    def _tl_motion(self, e):
        g = self._tl_geo()
        if not g:
            return
        if self._drag_extra is not None:
            gesto, y0 = self._drag_extra
            try:
                gesto("motion", e, g, y0)
            except Exception:
                pass
            return
        d = self._drag_marca
        if d is None:
            self._scrub(e, g)
            return
        t = min(max(self._x2t(e.x, g), 0.0), self.info["duracion"])
        d["movio"] = True
        if d["modo"] == "crear":
            d["t1"] = t
        else:
            m, (orig, t_ini_drag) = d["m"], d["base"]
            if d["modo"] == "mover":
                dt = t - t_ini_drag
                if m["tipo"] == "punto":
                    m["t"] = min(max(0.0, orig["t"] + dt), self.info["duracion"])
                else:
                    w = orig["t_fin"] - orig["t_ini"]
                    ini = min(max(0.0, orig["t_ini"] + dt), self.info["duracion"] - w)
                    m["t_ini"], m["t_fin"] = round(ini, 3), round(ini + w, 3)
            elif d["modo"] == "borde_ini":
                m["t_ini"] = round(min(t, m["t_fin"] - 0.05), 3)
            elif d["modo"] == "borde_fin":
                m["t_fin"] = round(max(t, m["t_ini"] + 0.05), 3)
        self._dibujar_timeline()

    def _tl_release(self, _e):
        extra, self._drag_extra = self._drag_extra, None
        if extra is not None:
            gesto, y0 = extra
            try:
                gesto("release", _e, self._tl_geo(), y0)
            except Exception:
                pass
            return
        d, self._drag_marca = self._drag_marca, None
        if d is None or self.reg is None:
            return
        if d["modo"] == "crear":
            g = self._tl_geo()
            px = abs(d["t1"] - d["t0"]) * (g[1] / max(self.view[1], 1e-9)) if g else 0
            if d["movio"] and px >= 4:         # ≥4 px = región; menos = click (deselección)
                m = self.reg.agregar_region(d["t0"], d["t1"])
                self._seleccionar(m, foco_prompt=True)
            self._dibujar_timeline()
            return
        m = d["m"]
        if d["movio"]:
            # el drag mutó la marca EN VIVO; el commit valida y persiste al SOLTAR
            # (consenso r1 h.8). Región con decisión que quedó corta → estirar al mínimo.
            if m["tipo"] == "region" and m.get("decision") and \
                    m["t_fin"] - m["t_ini"] < marcas_mod.MIN_REGION_DECISION:
                m["t_fin"] = round(min(self.info["duracion"],
                                       m["t_ini"] + marcas_mod.MIN_REGION_DECISION), 3)
            try:
                self.reg.editar(m)             # sin campos: valida el estado y guarda
            except ValueError as err:
                # ROLLBACK al estado pre-drag (review impl h.2): el editar() de marcas.py
                # no puede revertir porque el objeto YA venía mutado por el drag — sin
                # esto la marca inválida quedaba viva y un guardado posterior la persistía
                orig = d["base"][0]
                m.clear(); m.update(orig)
                self.status(f"⚠ {err} — la marca volvió a su estado anterior.")
            self._dibujar_timeline()

    def _tl_doble(self, e):
        g = self._tl_geo()
        if self._marks_height and g and RULER_H <= e.y <= RULER_H + self._marks_height and self.reg is not None:
            hit = self._marca_hit(e.x, e.y, g)
            if hit:
                self._seleccionar(hit[0], foco_prompt=True)
            return
        carril = self._carril_en(e.y) if g else None
        if carril is not None and carril[0].get("gesto"):
            try:
                carril[0]["gesto"]("doble", e, g, carril[1])
            except Exception:
                pass

    def _tl_pan_ini(self, e):
        self._pan_ancla = (e.x, self.view[0])

    def _tl_pan_mov(self, e):
        g = self._tl_geo()
        if not g or not getattr(self, "_pan_ancla", None):
            return
        x0, ancho = g
        xa, t0a = self._pan_ancla
        self.view[0] = t0a - (e.x - xa) / ancho * self.view[1]
        self._clamp_view()
        self._dibujar_timeline()

    def _tl_rueda(self, e):
        """Rueda = pan · Ctrl+rueda = zoom en el cursor. Delta normalizado: Windows/mac
        mandan <MouseWheel> (±120 o valores chicos), Linux Button-4/5 (consenso q.4)."""
        g = self._tl_geo()
        if not g:
            return "break"
        if getattr(e, "num", None) == 4:
            d = 1
        elif getattr(e, "num", None) == 5:
            d = -1
        else:
            d = 1 if e.delta > 0 else -1
        if e.state & 0x4:                      # Ctrl → zoom centrado en el cursor
            self._zoom(1.3 if d > 0 else 1 / 1.3, centro=self._x2t(e.x, g))
        else:                                  # pan (Shift o a secas): 10% del span
            self._pan(-d * self.view[1] * 0.1)
        return "break"

    # ---- teclado: despacho por keymap (diseño §2) ----
    @staticmethod
    def _foco_permite_teclas(w, top) -> bool:
        """Guarda de foco: True si el widget con foco es un Canvas, un Frame, un Label
        o el propio toplevel. Entry/Text (escribir) y Button/Checkbox/Scale/OptionMenu
        (space/Return los activan) nunca despachan."""
        import tkinter as tk
        if w is None:
            return False
        if w is top:
            return True
        if isinstance(w, (tk.Entry, tk.Text, tk.Spinbox, tk.Listbox, tk.Button,
                          tk.Checkbutton, tk.Radiobutton, tk.Menubutton, tk.Scale,
                          tk.Scrollbar)):
            return False
        try:
            from tkinter import ttk
            if isinstance(w, (ttk.Entry, ttk.Combobox, ttk.Spinbox, ttk.Button,
                              ttk.Checkbutton, ttk.Radiobutton, ttk.Scale)):
                return False
        except Exception:
            pass
        return isinstance(w, (tk.Canvas, tk.Frame, tk.Label, tk.Toplevel, tk.Tk))

    def _key_toplevel(self, e):
        if not self._keys_activos:
            return None
        try:
            top = self.f.winfo_toplevel()
            if getattr(e, "widget", None) is not None and e.widget.winfo_toplevel() is not top:
                return None                    # diálogo modal / otra ventana
        except Exception:
            return None
        if not self._foco_permite_teclas(getattr(e, "widget", None), top):
            return None
        return self._dispatch(e)

    def _click_toplevel(self, e):
        """Click izquierdo en un widget NO interactivo del editor → foco al timeline
        (las teclas funcionan sin hacer click en el canvas antes)."""
        if not self._keys_activos:
            return None
        w = getattr(e, "widget", None)
        try:
            top = self.f.winfo_toplevel()
            if w is None or w.winfo_toplevel() is not top:
                return None
            if not self._foco_permite_teclas(w, top):
                return None
            if w is not self.tl and not str(w).startswith(str(self.f)):
                return None                    # fuera del editor (panel, barra…)
            self.tl.focus_set()
        except Exception:
            pass
        return None

    def _dispatch(self, e):
        """Acorde del evento → acción del keymap (lookup O(1)) → el dueño primero
        (`acciones_extra(action, e)`), después la tabla de handlers. `"break"` SOLO si
        se consumió una acción; una tecla sin acción sigue su propagación normal."""
        if not self.info:
            return None
        import keymap
        action = keymap.resolve_event(getattr(e, "keysym", ""), getattr(e, "state", 0))
        if action is None:
            return None
        return "break" if self.ejecutar(action, e) else None

    def ejecutar(self, action: str, e=None) -> bool:
        """Ejecuta una acción por id (teclas, menús, tests). True si alguien la atendió."""
        if self.acciones_extra:                # el dueño primero (items de capa seleccionados)
            try:
                if self.acciones_extra(action, e):
                    return True
            except Exception as error:
                self.status(f"⚠ {error}")
                return True
        handler = self._handlers.get(action)
        if handler is None:
            return False
        handler()
        return True

    def _mover_playhead(self, t):
        """Teclas de navegación: mueve el playhead y, durante playback, salta de verdad
        (re-mezcla con debounce)."""
        self._set_playhead(t)
        if self._playback_activo():
            self._remezclar_debounced(self.t_play)

    def _rate_exact(self, rate):
        """Velocidad exacta (1-4, Shift+L = ×8): reproduciendo cambia; pausado arranca."""
        if self._playback_activo():
            self.set_rate(rate)
        else:
            self.rate = min(SPEEDS, key=lambda s: abs(s - float(rate)))
            self._play()

    def _marca_in(self):
        self._pend_in = self.t_play
        self.status(f"◀ IN marcado en {self.t_play:.1f}s — «O» en el out cierra la región.")
        self._dibujar_timeline()

    def _armar_handlers(self) -> dict:
        """Tabla id de acción → handler (migración 1:1 de las teclas anteriores más
        la velocidad). Las fases siguientes añaden las suyas."""
        h = {
            "transport.play_pause": lambda: self._play(),
            "transport.pause": lambda: self._play() if self._playback_activo() else None,
            "transport.faster": lambda: self._rate_step(+1),
            "transport.slower": lambda: self._rate_step(-1),
            "transport.skim": lambda: self._rate_exact(SPEEDS[-1]),
            "nav.step_prev": lambda: self._mover_playhead(self.t_play - 0.5),
            "nav.step_next": lambda: self._mover_playhead(self.t_play + 0.5),
            "nav.step_prev_5": lambda: self._mover_playhead(self.t_play - 5.0),
            "nav.step_next_5": lambda: self._mover_playhead(self.t_play + 5.0),
            "nav.home": lambda: self._mover_playhead(0.0),
            "nav.end": lambda: self._mover_playhead(self.info["duracion"]),
            "view.zoom_in": lambda: self._zoom(1.5),
            "view.zoom_out": lambda: self._zoom(1 / 1.5),
            "view.fit": self._fit,
            "marks.point": self._marca_punto,
            "marks.in": self._marca_in,
            "marks.out": self._marca_out,
            "edit.toggle": self._marca_ciclar,
            "edit.delete": self._marca_borrar,
        }
        for n in (1, 2, 3, 4):
            h[f"transport.rate_{n}"] = lambda r=float(n): self._rate_exact(r)
        return h

    # ---- alturas: carriles extra del dueño entre MARCAS y las pistas ----
    def _carriles(self) -> list[dict]:
        if not self.carriles_extra:
            return []
        try:
            return self.carriles_extra() or []
        except Exception:
            return []

    def _alto_extra(self) -> int:
        return sum(int(c.get("alto", 0)) for c in self._carriles())

    def _y0_pistas(self) -> int:
        return RULER_H + self._marks_height + self._alto_extra()

    def _alto_total(self) -> int:
        n = max(len(self.info["pistas"]), 1) if self.info else 1
        return self._y0_pistas() + LANE_H * n

    def _ajustar_altura(self):
        """La ALTURA del canvas se fija acá (una vez por video / cambio de carriles),
        no en cada redibujo — setearla dentro de _dibujar_timeline realimentaba
        <Configure> (consenso q.6)."""
        if getattr(self, "_esp", None) is not None:
            self._esp.configure(height=RULER_H + self._marks_height + self._alto_extra())
        alto = self._alto_total()
        if self.tl.winfo_height() != alto:
            self.tl.configure(height=alto)

    def _dibujar_timeline(self):
        """Redibuja el timeline COMPLETO para el viewport actual: ruler adaptativo +
        carril de MARCAS + carriles EXTRA del dueño (metadata read-only) + un carril
        por pista (waveform BLITTEADA: una imagen por carril en vez de ~2 items de
        canvas por pixel por pista — consenso r1 E.1) + overlays de marcas + playhead."""
        tl = self.tl
        tl.delete("all")
        g = self._tl_geo()
        if not g or not self.view[1]:
            return
        x0, ancho = g
        t0, span = self.view
        alto = self._alto_total()

        # ---- ruler adaptativo (serie 1–2–5×10ⁿ, consenso q.5) ----
        pxseg = ancho / span
        paso = next((p for p in (0.5, 1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 1800, 3600)
                     if p * pxseg >= 70), 7200)
        t = math.floor(t0 / paso) * paso
        while t <= t0 + span:
            if t >= -1e-9:
                x = self._t2x(t, g)
                tl.create_line(x, RULER_H - 5, x, RULER_H, fill="#666")
                if paso < 1:
                    txt = f"{int(t // 60)}:{t % 60:04.1f}"
                elif paso >= 60:
                    txt = f"{int(t // 3600)}:{int(t % 3600 // 60):02d}:00" if t >= 3600 \
                        else f"{int(t // 60)}:00" if paso >= 3600 else f"{int(t // 60)}:{int(t % 60):02d}"
                else:
                    txt = f"{int(t // 60)}:{int(t % 60):02d}"
                tl.create_text(x + 2, 2, text=txt, anchor="nw", fill="#888",
                               font=("TkDefaultFont", 8))
            t += paso

        # ---- carril de MARCAS (fondo) ----
        tl.create_rectangle(x0, RULER_H, x0 + ancho, RULER_H + self._marks_height,
                            fill="#1b1b1b", outline="#2a2a2a")

        # ---- carriles EXTRA del dueño (read-only; la tab Marcar pinta metadata) ----
        y = RULER_H + self._marks_height
        for c in self._carriles():
            try:
                c["dibujar"](tl, g, y)
            except Exception:
                pass
            y += int(c.get("alto", 0))

        # ---- carriles de pistas: imagen blitteada por carril ----
        y_pistas = self._y0_pistas()
        for fila, pista in enumerate(self.info["pistas"]):
            y0 = y_pistas + fila * LANE_H
            img = self._wave_img(fila, pista, int(ancho))
            if img is not None:
                tl.create_image(x0, y0, image=img, anchor="nw")
            else:
                tl.create_rectangle(x0, y0 + 2, x0 + ancho, y0 + LANE_H - 2,
                                    fill="#181818", outline="#242424")
                frac = self._env_prog.get(pista["idx"])
                tl.create_text(x0 + 8, y0 + LANE_H / 2,
                               text=f"cargando… {frac:.0%}" if frac else "esperando…",
                               anchor="w", fill="#555", font=("TkDefaultFont", 9))
                if frac:                       # barra de progreso sutil en el carril
                    tl.create_rectangle(x0, y0 + LANE_H - 4, x0 + ancho * frac,
                                        y0 + LANE_H - 2, fill="#2e6b45", outline="")
        self._pedir_tiles(int(ancho))          # tiles del zoom que falten (async)

        # ---- overlays: marcas + IN pendiente + playhead ----
        if self._marks_height:
            self._dibujar_marcas(g, alto)
        if self._pend_in is not None and t0 <= self._pend_in <= t0 + span:
            x = self._t2x(self._pend_in, g)
            tl.create_line(x, RULER_H, x, alto, fill="#e8b34b", dash=(5, 3))
            tl.create_text(x + 3, RULER_H + 2, text="IN", anchor="nw",
                           fill="#e8b34b", font=("TkDefaultFont", 8, "bold"))
        self._ph = tl.create_line(0, 0, 0, 0, fill="#ff5252", width=2)
        self._mover_linea_playhead()

    def _mover_linea_playhead(self):
        g = self._tl_geo()
        if not g or getattr(self, "_ph", None) is None:
            return
        t0, span = self.view
        if not span:
            return
        x = self._t2x(self.t_play, g) if t0 <= self.t_play <= t0 + span else -10
        self.tl.coords(self._ph, x, 0, x, self._alto_total())

    # ---- waveform blitteada (consenso q.6: refs vivas, PhotoImage solo en el hilo Tk) --
    def _wave_img(self, fila, pista, ancho):
        env = self._envs.get(pista["idx"])
        if not env:
            return None
        clave = (round(self.view[0], 4), round(self.view[1], 4), ancho,
                 self._tiles_ver, pista["idx"])
        if self._wave_keys.get(fila) == clave:
            return self._wave_imgs.get(fila)
        if clave in self._wave_view_cache:
            self._wave_view_cache.move_to_end(clave)
            self._wave_keys[fila]=clave
            self._wave_imgs[fila]=self._wave_view_cache[clave]
            return self._wave_imgs[fila]
        from PIL import Image, ImageDraw, ImageTk
        alto = LANE_H
        img = Image.new("RGB", (max(ancho, 1), alto), "#181818")
        dr = ImageDraw.Draw(img)
        mid = alto / 2
        amp = (alto / 2) - 4
        dr.line([(0, mid), (ancho, mid)], fill="#262626")
        datos = self._datos_vista(pista["idx"], ancho)
        pico = max((max(abs(lo), abs(hi)) for lo, hi, _ in datos if lo is not None),
                   default=0.0) or 1.0
        gan = min(8.0, 0.95 / pico) if pico < 0.95 else 1.0
        for px, (lo, hi, rms) in enumerate(datos):
            if lo is None:
                continue
            dr.line([(px, mid - hi * gan * amp), (px, mid - lo * gan * amp)], fill="#2e6b45")
            r = rms * gan
            if r > 0.002:                      # cuerpo RMS simétrico, más brillante
                dr.line([(px, mid - r * amp), (px, mid + r * amp)], fill="#4ade80")
        if gan > 1.05:
            dr.text((ancho - 30, 3), f"×{gan:.0f}", fill="#777")
        ph = ImageTk.PhotoImage(img)           # ref VIVA en self._wave_imgs (si no: blanco)
        self._wave_imgs[fila] = ph
        self._wave_keys[fila] = clave
        self._wave_view_cache[clave]=ph
        while len(self._wave_view_cache)>24:
            self._wave_view_cache.popitem(last=False)
        return ph

    def _nivel_zoom(self, ancho) -> int:
        """Nivel de densidad de tiles: 2^nivel buckets/s apuntando a ≥1 bucket/px
        (consenso r2 h.4 — derivado de px_por_seg, no una escala fija corta)."""
        pxseg = ancho / max(self.view[1], 1e-9)
        return min(max(math.ceil(math.log2(max(pxseg, 1e-9))), 0), NIVEL_MAX)

    def _datos_vista(self, idx, ancho) -> list:
        """(lo,hi,rms) POR PIXEL del viewport: tiles del nivel actual donde estén,
        envolvente global estirada donde falten (progresivo, sin bloqueo)."""
        env = self._envs.get(idx) or []
        dur = self.info["duracion"]
        t0, span = self.view
        nivel = self._nivel_zoom(ancho)
        dens = float(2 ** nivel)               # buckets/s de los tiles
        dens_g = len(env) / dur if dur else 0  # buckets/s de la global
        usar_tiles = dens > dens_g * 1.5
        dur_tile = TILE_B / dens
        out = []
        for px in range(ancho):
            ta = t0 + px / ancho * span
            tb = t0 + (px + 1) / ancho * span
            seg = None
            if usar_tiles:
                seg = self._buckets_tiles(idx, nivel, dens, dur_tile, ta, tb)
            if seg is None:                    # fallback: la global estirada
                b0 = int(ta / dur * len(env)) if dur else 0
                b1 = max(b0 + 1, int(tb / dur * len(env)) if dur else 0)
                seg = env[max(0, b0):min(b1, len(env))]
            if not seg:
                out.append((None, None, None))
                continue
            out.append((min(e[0] for e in seg), max(e[1] for e in seg),
                        sum(e[2] for e in seg) / len(seg)))
        return out

    def _buckets_tiles(self, idx, nivel, dens, dur_tile, ta, tb) -> list | None:
        """Los buckets de tiles que cubren [ta,tb); None si falta alguno (→ fallback)."""
        out = []
        i0, i1 = int(ta / dur_tile), int(max(ta, tb - 1e-9) / dur_tile)
        for ti in range(i0, i1 + 1):
            tile = self._tiles.get((idx, nivel, ti))
            if tile is None:
                return None
            base = ti * dur_tile
            b0 = int((max(ta, base) - base) * dens)
            b1 = int(math.ceil((min(tb, base + dur_tile) - base) * dens))
            out.extend(tile[max(0, b0):max(b0 + 1, min(b1, len(tile)))])
        return out or None

    def _pedir_tiles(self, ancho):
        """Encola los tiles VISIBLES que faltan (latest-wins: el pedido nuevo reemplaza
        al pendiente; el worker es único y cancelable — consenso q.5)."""
        if not self.info:
            return
        nivel = self._nivel_zoom(ancho)
        dens = float(2 ** nivel)
        dur = self.info["duracion"]
        env0 = next(iter(self._envs.values()), None)
        if env0 and dens <= (len(env0) / dur) * 1.5:
            return                             # la global ya alcanza a este zoom
        dur_tile = TILE_B / dens
        t0, span = self.view
        faltan = []
        for pista in self.info["pistas"]:
            idx = pista["idx"]
            if idx not in self._envs:
                continue                       # la global primero (sigue cargando)
            for ti in range(int(t0 / dur_tile), int((t0 + span) / dur_tile) + 1):
                clave = (idx, nivel, ti)
                if clave in self._tiles:
                    self._tiles.move_to_end(clave)      # LRU touch
                elif ti * dur_tile < dur:
                    faltan.append(clave)
        if faltan:
            with self._tile_cond:
                self._tile_req = (self._gen, faltan, self.info["path"], dur,
                                  self._cancel_medios)
                self._tile_cond.notify()

    def _tile_loop(self):
        """Worker ÚNICO de tiles (hilo daemon): atiende el último pedido, un tile por
        vez, cancelable por generación (el cambio de video mata el ffmpeg en curso)."""
        while True:
            with self._tile_cond:
                while self._tile_req is None:
                    self._tile_cond.wait()
                gen, faltan, path, dur, cancel = self._tile_req
                self._tile_req = None
            for (idx, nivel, ti) in faltan:
                if gen != self._gen or cancel.is_set():
                    break
                with self._tile_cond:
                    if self._tile_req is not None:
                        break                  # llegó un pedido más nuevo → prioridad
                dens = float(2 ** nivel)
                dur_tile = TILE_B / dens
                t0 = ti * dur_tile
                largo = min(dur_tile, dur - t0)
                if largo <= 0:
                    continue
                try:
                    env = medios.envolvente_ventana(path, idx, t0, largo,
                                                    buckets=max(1, int(round(largo * dens))),
                                                    cancel=cancel)
                except Exception:
                    continue
                if env and not cancel.is_set():
                    self.q.put(("tile", gen, (idx, nivel, ti), env))

    # ---- marcas: dibujo + hit test + acciones (consenso D / r2 h.8) ----
    def _marcas_visibles(self, g):
        """[(marca, xa, xb)] de las que tocan el viewport (xa=xb para puntos)."""
        if self.reg is None:
            return []
        t0, span = self.view
        out = []
        for m in self.reg.marcas:
            if m["tipo"] == "punto":
                if t0 <= m["t"] <= t0 + span:
                    x = self._t2x(m["t"], g)
                    out.append((m, x, x))
            elif m["t_fin"] > t0 and m["t_ini"] < t0 + span:
                out.append((m, self._t2x(max(m["t_ini"], t0), g),
                            self._t2x(min(m["t_fin"], t0 + span), g)))
        return out

    def _dibujar_marcas(self, g, alto):
        tl = self.tl
        y0, y1 = RULER_H + 2, RULER_H + self._marks_height - 2
        for m, xa, xb in self._marcas_visibles(g):
            col = COL_MARCA[m.get("decision")]
            sel = m is self.sel_marca
            if m["tipo"] == "punto":
                tl.create_polygon(xa - 5, y0, xa + 5, y0, xa, y1,
                                  fill=col, outline="#fff" if sel else col)
                tl.create_line(xa, y1, xa, alto, fill=col, dash=(2, 4))
            else:
                tl.create_rectangle(xa, y0, xb, y1, fill=col,
                                    outline="#fff" if sel else col,
                                    width=2 if sel else 1)
                # proyección sobre los carriles (patrón in/out de editor): rayado barato
                # (UN item con stipple, no cientos de líneas — consenso q.6)
                tl.create_rectangle(xa, RULER_H + self._marks_height, xb, alto, fill=col,
                                    stipple="gray12" if m.get("decision") != "excluir"
                                    else "gray25", outline="")
                if m.get("decision") == "excluir":
                    tl.create_line(xa, RULER_H + self._marks_height, xb, alto, fill=col, dash=(6, 4))
                    tl.create_line(xa, alto, xb, RULER_H + self._marks_height, fill=col, dash=(6, 4))
                if sel:                        # handles de los bordes
                    for xh in (xa, xb):
                        tl.create_rectangle(xh - 2, y0, xh + 2, y1, fill="#fff", outline="")
            if m.get("prompt"):
                tl.create_text((xa + xb) / 2, (y0 + y1) / 2, text="✎",
                               fill="#101010", font=("TkDefaultFont", 8, "bold"))

    def _marca_hit(self, x, y, g):
        """(marca, modo) bajo el cursor en el carril de marcas: bordes primero (±4px),
        después la SELECCIONADA, después la más ANGOSTA (consenso q.4)."""
        vis = self._marcas_visibles(g)
        for m, xa, xb in vis:
            if m is self.sel_marca and m["tipo"] == "region":
                if abs(x - xa) <= 4:
                    return m, "borde_ini"
                if abs(x - xb) <= 4:
                    return m, "borde_fin"
        hits = []
        for m, xa, xb in vis:
            if m["tipo"] == "punto":
                if abs(x - xa) <= 6:
                    hits.append((0.0, m))
            elif xa - 2 <= x <= xb + 2:
                hits.append((xb - xa, m))
        if not hits:
            return None
        hits.sort(key=lambda h: (h[1] is not self.sel_marca, h[0]))
        return hits[0][1], "mover"

    def _seleccionar(self, m, foco_prompt=False):
        """Selección NO MODAL: el panel aparece/desaparece; el foco queda en el timeline
        salvo pedido explícito (doble click / Enter) — consenso r2 h.8."""
        self.sel_marca = m
        if m is None:
            self.f_marca.grid_remove()
            return
        rango = (f"[{m['t']:.1f}s]" if m["tipo"] == "punto"
                 else f"[{m['t_ini']:.1f}–{m['t_fin']:.1f}s]")
        self.lbl_marca.configure(text=f"{m['id'].upper()} {rango}")
        self.seg_marca.set(m.get("decision") or "nota")
        self.e_prompt.delete(0, "end")
        if m.get("prompt"):
            self.e_prompt.insert(0, m["prompt"])
        self.f_marca.grid()
        if foco_prompt:
            self.e_prompt.focus_set()

    def _marca_punto(self):
        if self.reg is None:
            return
        m = self.reg.agregar_punto(self.t_play)
        self._seleccionar(m, foco_prompt=True)
        self._dibujar_timeline()

    def _marca_out(self):
        if self.reg is None or self._pend_in is None:
            return
        t0, t1 = self._pend_in, self.t_play
        self._pend_in = None
        if abs(t1 - t0) < 0.05:
            self.status("⚠ IN y OUT casi iguales — región descartada.")
        else:
            m = self.reg.agregar_region(t0, t1)
            self._seleccionar(m, foco_prompt=True)
        self._dibujar_timeline()

    def _marca_bajo_playhead(self):
        if self.reg is None:
            return None
        cands = [m for m in self.reg.marcas
                 if (m["tipo"] == "punto" and abs(m["t"] - self.t_play) < 0.5)
                 or (m["tipo"] == "region" and m["t_ini"] <= self.t_play <= m["t_fin"])]
        if self.sel_marca in cands:
            return self.sel_marca
        cands.sort(key=lambda m: 0 if m["tipo"] == "punto" else m["t_fin"] - m["t_ini"])
        return cands[0] if cands else None

    def _marca_ciclar(self):
        """`x`: cicla la decisión nota→incluir→excluir→nota de la marca seleccionada o
        bajo el playhead. Un PUNTO se convierte primero en región ±2s (regla dura del
        consenso: el modelo nunca ve un punto con decisión)."""
        m = self.sel_marca or self._marca_bajo_playhead()
        if m is None or self.reg is None:
            return
        orden = [None, "incluir", "excluir"]
        nueva = orden[(orden.index(m.get("decision")) + 1) % 3]
        self._aplicar_decision(m, nueva)

    def _aplicar_decision(self, m, decision):
        if m["tipo"] == "punto" and decision is not None:
            self.reg.a_region(m, radio=RADIO_PUNTO)
            self.status(f"○→▭ {m['id']} pasó a región [{m['t_ini']:.1f}–{m['t_fin']:.1f}s] "
                        f"(una decisión de corte exige región — ajustá los bordes).")
        try:
            self.reg.editar(m, decision=decision)
        except ValueError as err:
            self.status(f"⚠ {err}")
            return
        self._seleccionar(m)
        self._dibujar_timeline()

    def _marca_decision(self, valor):
        """El segmented del panel — MISMA regla que `x` (consenso r2 h.5)."""
        if self.sel_marca is not None:
            self._aplicar_decision(self.sel_marca, None if valor == "nota" else valor)

    def _marca_prompt(self):
        if self.sel_marca is None or self.reg is None:
            return
        txt = self.e_prompt.get().strip() or None
        if txt != self.sel_marca.get("prompt"):
            self.reg.editar(self.sel_marca, prompt=txt)
            self._dibujar_timeline()

    def _marca_borrar(self):
        m = self.sel_marca or self._marca_bajo_playhead()
        if m is None or self.reg is None:
            return
        self.reg.borrar(m)
        self._seleccionar(None)
        self._dibujar_timeline()

    def _adoptar_click(self):
        """Resuelve el conflicto de sidecar secundario adoptando SU documento (r1.9):
        mutación transaccional persistida de marcas.adoptar — errores visibles."""
        if self.reg is None or self.reg.conflicto_pendiente is None:
            self.btn_adoptar.grid_remove()
            return
        try:
            n = self.reg.adoptar(self.reg.conflicto_pendiente)
        except Exception as e:
            self.status(f"✗ no pude adoptar las marcas de la copia: {e}")
            return
        self.btn_adoptar.grid_remove()
        self.status(f"✓ adoptadas las marcas de la copia divergente: {n} marca(s) "
                    f"(revisión promovida a {self.reg.revision}).")
        self._dibujar_timeline()

    def _reasociar_click(self):
        """Reasociar un sidecar con fingerprint ajeno a ESTE video (2 pasos: conteo →
        confirmación explícita — consenso r2 q.1 / review impl h.5)."""
        if self.reg is None:
            return
        prev = self.reg.reasociar_previa()
        if prev is None:
            self.btn_reasociar.grid_remove()
            return
        entran, cuar = prev
        if not self._reasoc_armado:
            self._reasoc_armado = True
            self.btn_reasociar.configure(
                text=f"¿Confirmar? entran {entran}, quedan en cuarentena {cuar}",
                fg_color="#8f3838")
            return
        n = self.reg.reasociar()
        self._reasoc_armado = False
        self.btn_reasociar.grid_remove()
        self.status(f"✓ Marcas reasociadas a este video: {n} adoptada(s)"
                    + (f", {cuar} en cuarentena (motivo por marca en el sidecar)."
                       if cuar else "."))
        self._dibujar_timeline()

    # ---- ciclo de playback ----
    def _anim_tick(self, tok=None):
        """El ciclo de playback (diseño v2 B): primero el WARM-UP (espera el primer
        frame del stream, tope WARMUP_MAX_S, y recién ahí arranca el audio y fija el
        ancla); después el reloj avanza y cada tick muestra el frame del stream que
        corresponde (drop de atrasados en frame_hasta). `tok` ata el ciclo a UNA
        sesión: _stop_preview lo invalida (sin ciclos duplicados)."""
        if tok != getattr(self, "_anim_tok", None):
            return                             # ciclo viejo (hubo otro play/stop) → morir
        # ---- fase warm-up: audio aún no arrancó (r1.4/r2.4) ----
        if self._warmup is not None:
            t0, ini = self._warmup
            espera = time.monotonic() - ini
            listo = self._vses is None or self._vses.lista()
            if self._vses is not None:         # mostrar el 1er frame apenas exista
                img = self._vses.frame_para(t0)
                if img is not None:
                    self._mostrar_frame(img)
            if not listo and espera < WARMUP_MAX_S and not (self._vses and self._vses.fallida):
                self.f.after(50, self._anim_tick, tok)
                return
            self._warmup = None
            if not listo:
                self._stop_preview()
                self.btn_play.configure(text="▶")
                self.status("No se pudo preparar el video; reproducción detenida.")
                return
            ta = time.monotonic()
            pistas = self._activas()           # mute/solo DURANTE el warm-up vale (r3.5)
            motivo = self.repro.play(self.info["path"], pistas, t0, rate=self.rate) \
                if pistas else "no hay pistas activas (todo muteado)"
            if motivo:
                self._stop_preview()
                self.btn_play.configure(text="▶")
                self.status(f"⚠ No puedo reproducir: {motivo}")
                return
            audio_ms = (time.monotonic() - ta) * 1000
            self._ancla = (t0, time.monotonic())
            pf = self._vses.primer_frame_s() if self._vses else None
            self._play_metrics = dict(start=t0,rate=self.rate,
                                      first_frame_ms=round(pf*1000,1) if pf else None,
                                      audio_spawn_ms=round(audio_ms,1),audio_clock_ms=None)
            self.status(f"▶ {int(t0 // 60)}:{t0 % 60:04.1f} · pistas "
                        + ("+".join(str(p + 1) for p in pistas))
                        + (f" · video {self._vses.fps * self._vses.rate:g}fps@{self._vses.w}px"
                           + (f", 1er frame {pf * 1000:.0f}ms" if pf is not None else "")
                           if self._vses else " · sin pista de video")
                        + f" · audio spawn {audio_ms:.0f}ms"
                        + (f" · ×{self.rate:g}" if abs(self.rate - 1.0) > 1e-9 else "")
                        + (" (audio mudo)" if self.repro.mudo else ""))
        # ---- fase playback ----
        if not self.repro.playing():
            self._stop_preview(audio=False)    # baja stream/prefetch; epoch nuevo
            self.btn_play.configure(text="▶")
            self._set_playhead(self.t_play, frame=True)   # frame EXACTO de pausa
            self._refrescar_status()           # vuelve el estado del archivo al status
            return
        clock_position = self.repro.position()
        if clock_position is None:
            if time.monotonic()-self._ancla[1] > 10:
                error=self.repro.error_tail
                self._stop_preview()
                self.btn_play.configure(text="▶")
                self.status("No hay reloj de audio fiable; reproducción detenida. " + error[-200:])
                return
            self.f.after(30,self._anim_tick,tok)
            return
        self.t_play = clock_position
        if self._play_metrics.get("audio_clock_ms") is None:
            self._play_metrics["audio_clock_ms"] = round(self.repro.clock.first_clock_s*1000,1)
            self.status(f"▶ reloj de audio · 1er frame {self._play_metrics.get('first_frame_ms')} ms"
                        f" · audio spawn {self._play_metrics.get('audio_spawn_ms')} ms"
                        f" · audio listo {self._play_metrics['audio_clock_ms']} ms")
            import json, app_paths
            try:
                app_paths.LOGS_DIR.mkdir(parents=True,exist_ok=True)
                with (app_paths.LOGS_DIR/'reproductor.jsonl').open('a',encoding='utf-8') as log:
                    log.write(json.dumps(self._play_metrics)+'\n')
            except OSError:
                pass
        if self.info and self.t_play >= self.info["duracion"]:
            self.repro.stop()
        self.lbl_t.configure(text=self._texto_reloj())
        if self.on_playhead:
            self.on_playhead(self.t_play)
        if self._asegurar_visible(self.t_play):
            self._dibujar_timeline()           # salto de página del viewport
        else:
            self._mover_linea_playhead()
        if self._vses is not None:
            img = self._vses.frame_para(self.t_play)
            if img is not None:
                self._mostrar_frame(img)
            if self._vses.fallida:
                self._stop_preview()
                self.btn_play.configure(text="▶")
                self.status("El video no pudo seguir al audio; reproducción detenida.")
                return
        self.f.after(16, self._anim_tick, tok)

    # ---- pistas UI (fila de controles ALINEADA con su carril del timeline) ----
    def _armar_pistas(self):
        for w in self._pista_ui:
            if "fila" in w:
                w["fila"].destroy()
        if getattr(self, "_esp", None) is not None:
            self._esp.destroy()
        self._pista_ui = []
        self._envs = {}
        # espaciador con la altura del ruler + carril de marcas (+ carriles extra):
        # cada fila de controles queda a la par de su carril de pista
        self._esp = ctk.CTkFrame(self.f_ctl, fg_color="transparent",
                                 height=RULER_H + self._marks_height + self._alto_extra())
        self._esp.grid(row=0, column=0)
        self._esp.grid_propagate(False)
        for i, pista in enumerate(self.info["pistas"]):
            fila = ctk.CTkFrame(self.f_ctl, fg_color="transparent", height=LANE_H,
                                width=self._ancho_ctl - 4)
            fila.grid(row=i + 1, column=0, sticky="w")
            fila.grid_propagate(False)
            w: dict = {"fila": fila}
            w["mute"] = ctk.CTkButton(fila, text="M", width=28, height=26, fg_color="gray25",
                                      command=lambda i=i: self._toggle(i, "mute"))
            w["mute"].grid(row=0, column=1, padx=2, pady=(LANE_H // 2 - 13, 0))
            w["solo"] = ctk.CTkButton(fila, text="S", width=28, height=26, fg_color="gray25",
                                      command=lambda i=i: self._toggle(i, "solo"))
            w["solo"].grid(row=0, column=2, padx=2, pady=(LANE_H // 2 - 13, 0))
            w["_mute"], w["_solo"] = False, False
            # widgets extra del dueño (columna 0 = nombre, 3+ = rol, etc.)
            if self.controles_pista_extra:
                try:
                    self.controles_pista_extra(fila, pista, i)
                except Exception:
                    pass
            self._pista_ui.append(w)
        self.t_play = 0.0
        self._ajustar_altura()
        self._dibujar_timeline()

    def _toggle(self, i, cual):
        w = self._pista_ui[i]
        w[f"_{cual}"] = not w[f"_{cual}"]
        w[cual].configure(fg_color="#2f8a4a" if w[f"_{cual}"] else "gray25")
        if self.repro.playing():
            self._restart_audio()              # SOLO la mezcla; el video sigue (r2.22)

    def _activas(self) -> list[int]:
        """Mute/solo real: con ≥1 solo suenan los solos no muteados; si no, las no muteadas."""
        solos = [i for i, w in enumerate(self._pista_ui) if w["_solo"] and not w["_mute"]]
        if solos:
            return [self.info["pistas"][i]["idx"] for i in solos]
        return [self.info["pistas"][i]["idx"] for i, w in enumerate(self._pista_ui)
                if not w["_mute"]]

    def _play(self, reiniciar=False, desde=None, rate=None):
        """Play/stop de la sesión (diseño v2 B): arranca PRIMERO el stream de video
        continuo; el audio y el ancla se fijan en el warm-up de _anim_tick (al llegar
        el primer frame o al tope). `reiniciar` = seek durante playback (re-arma la
        sesión desde `desde`); el mute/solo NO pasa por acá (usa _restart_audio).
        `rate` fija la velocidad de la sesión (si no, sigue `self.rate`)."""
        if rate is not None:
            self.rate = min(SPEEDS, key=lambda s: abs(s - float(rate)))
        if self._playback_activo() and not reiniciar:
            self.repro.stop()                  # stop del usuario: si el audio ya sonaba,
            if self._warmup is not None:       # el tick detecta y baja todo; en warm-up
                self._stop_preview()           # el tick aún no manda → bajar acá
                self.btn_play.configure(text="▶")
                self._set_playhead(self.t_play, frame=True)
            return
        if not self.info:
            return
        t = self.t_play if desde is None else max(0.0, min(desde, self.info["duracion"]))
        self._stop_preview()                   # sesión anterior (si reiniciar) + epoch
        tok = self._anim_tok
        self.t_play = t
        if self.info.get("video"):
            W, H = self._dims_preview()
            try:
                self._vses = medios.SesionVideo(self.info["path"], t, W, H,
                                                info=self.info, log=self.status,
                                                rate=self.rate)
            except ValueError:
                self._vses = None
        self._warmup = (t, time.monotonic())   # las pistas se leen AL arrancar el
        self.btn_play.configure(text="⏹")      # audio (r3.5: mute durante el warm-up)
        self.f.after(30, self._anim_tick, tok)

    # ---- lienzo del preview: geometría + redibujo (el dueño superpone con su hook) --
    def _geo(self):
        """Escala y offsets del frame dentro del canvas (letterbox). Trabaja en dims de
        DISPLAY (post-rotación) — el mismo espacio del preview y del crop de cara
        (ffmpeg autorrota antes de los filtros), así los rects caen donde se ven."""
        v = (self.info or {}).get("video") or {}
        w = v.get("display_width") or v.get("width")
        h = v.get("display_height") or v.get("height")
        if not w:
            return None
        cw = max(self.canvas.winfo_width(), 50)
        ch = max(self.canvas.winfo_height(), 50)
        s = min(cw / w, ch / h)
        iw, ih = w * s, h * s
        return (cw - iw) / 2, (ch - ih) / 2, iw, ih

    def _redibujar(self):
        self.canvas.delete("all")
        self._canvas_img = None                # los items se recrean acá (r1.9)
        g = self._geo()
        if not g:
            return
        ox, oy, iw, ih = g
        # frame desde el PIL cacheado, re-escalado SOLO si cambió el frame o el tamaño
        # (arrastrar un rect redibuja sin re-escalar — review timeline, h.5). Clave por
        # CONTADOR de revisión (r1.9: id() colisionaba al reciclarse ids de PIL).
        if self._frame_pil is not None and iw > 1:
            clave = (self._frame_rev, int(iw), int(ih))
            if getattr(self, "_frame_clave", None) != clave:
                from PIL import Image, ImageTk
                dims = (max(1, int(iw)), max(1, int(ih)))
                img = self._frame_pil
                if img.size != dims:           # re-escala solo si hace falta; durante el
                    # gesto de resize BILINEAR (r4), LANCZOS al asentarse/pausa
                    metodo = Image.BILINEAR if getattr(self, "_resize_activo", False) \
                        else Image.LANCZOS
                    img = img.resize(dims, metodo)
                self._frame_img = ImageTk.PhotoImage(img)
                self._frame_clave = clave
            self._canvas_img = self.canvas.create_image(ox, oy, image=self._frame_img,
                                                        anchor="nw")
        if self.overlay_preview:
            try:
                self.overlay_preview(self.canvas, g)
            except Exception:
                pass

    # ====================================================================== pump --
    def _pump(self):
        try:
            while True:
                m = self.q.get_nowait()
                k = m[0]
                if k == "insp":
                    _, gen, info, fp = m
                    if gen != self._gen:
                        continue
                    self.info, self.fp = info, fp
                    self.view = [0.0, info["duracion"]]
                    v = info.get("video")
                    pistas = len(info["pistas"])
                    # marcas del autor: instancia COMPARTIDA entre tabs (diseño
                    # tab-marcar h.1 — el wizard y la tab Marcar ven el mismo Registro)
                    avisos_reg = []
                    try:
                        self.reg, avs = marcas_mod.registro_compartido(
                            info["path"], fp, info["duracion"])
                        avisos_reg.extend(avs)
                        self.reg.suscribir(self._on_reg_cambio)
                    except Exception as e:
                        self.reg = None
                        avisos_reg.append(str(e))
                    extra = ""
                    if self.reg and (self.reg.marcas or self.reg.avisos):
                        extra = f" · {len(self.reg.marcas)} marca(s) del autor"
                        avisos_reg.extend(self.reg.avisos)
                    # sidecar de OTRO video → ofrecer la reasociación explícita (h.5)
                    self._reasoc_armado = False
                    if self.reg and self.reg.reasociar_previa() is not None:
                        self.btn_reasociar.configure(
                            text="Reasociar marcas a este video", fg_color="#7a5a1e")
                        self.btn_reasociar.grid()
                    # copia divergente (misma revisión, bytes distintos) → botón (r1.9)
                    if self.reg and self.reg.conflicto_pendiente is not None:
                        self.btn_adoptar.grid()
                    else:
                        self.btn_adoptar.grid_remove()
                    self._info_line = (f"✓ {Path(info['path']).name}: "
                               + (f"{v['width']}×{v['height']} @{v['fps']:.0f}fps · " if v else "")
                               + f"{info['duracion']:.0f}s · {pistas} pista(s) de audio"
                               + (f" · offsets normalizados a T0" if any(p['delta'] for p in info['pistas']) else "")
                               + extra)
                    if avisos_reg:
                        self._info_line += "\n" + "\n".join(f"⚠ marcas: {a}" for a in avisos_reg)
                    if self.on_video_cargado:
                        try:
                            self.on_video_cargado(info, fp)
                        except Exception:
                            pass
                    self._armar_pistas()
                    self._redibujar()
                    self._refrescar_status()
                    self._reapuntar_prefetch()         # ventana inicial desde t=0
                elif k == "frame":
                    _, tok, t_req, img, costo = m
                    if tok != (self._gen, self._preview_epoch) or img is None:
                        continue                       # video O sesión de preview viejos (r2.3)
                    if self._playback_activo():
                        continue                       # durante play alimenta el STREAM (r1.3)
                    # descartar un frame VIEJO que llegó tarde (h.6): solo entra el del
                    # último pedido (t_req=None = frame inicial de la carga)
                    if t_req is not None and t_req != getattr(self, "_t_pedido", None):
                        continue
                    self._frame_pil = img      # PIL cacheado: el resize responsivo re-escala
                    if t_req is not None:
                        self._exact_frames.put(t_req,img)
                    self._frame_rev += 1
                    self._redibujar()
                    if t_req is None and self._carga_t0 and self._preview_s is None:
                        self._preview_s = time.monotonic() - self._carga_t0
                        self._refrescar_status()       # «preview listo» (r1.16)
                elif k == "env":
                    _, gen, idx, env = m
                    if gen != self._gen:
                        continue
                    self._envs[idx] = env
                    self._env_prog[idx] = 1.0
                    self._tiles_ver += 1       # invalida el cache de imágenes de onda
                    self._dibujar_timeline()
                    self._refrescar_status()
                elif k == "envprog":
                    _, gen, idx, frac = m
                    if gen != self._gen:
                        continue
                    self._env_prog[idx] = frac
                    ahora = time.monotonic()
                    if ahora - self._env_draw_t >= 0.4:            # throttle del redraw
                        self._env_draw_t = ahora
                        self._refrescar_status()
                        self._dibujar_timeline()       # carril «cargando… 35%»
                elif k == "envfin":
                    if m[1] == self._gen and self._carga_t0:
                        self._wave_s = time.monotonic() - self._carga_t0
                        self._refrescar_status()       # «waveforms completas» (r1.16)
                elif k == "fcache":
                    _, tok, tg, w, img = m
                    if tok == (self._gen, self._preview_epoch):
                        self._fcache_put(tg, img)      # prefetch del scrub (diseño C)
                elif k == "tile":
                    _, gen, clave, env = m
                    if gen != self._gen:
                        continue
                    self._tiles[clave] = env
                    self._tiles.move_to_end(clave)
                    while len(self._tiles) > TILES_LRU:
                        self._tiles.popitem(last=False)
                    self._tiles_ver += 1
                    self._dibujar_timeline()
                elif k == "err1":
                    if m[1] == self._gen:
                        self.status(f"✗ {m[2]}")
        except queue.Empty:
            pass
        self.f.after(80, self._pump)
