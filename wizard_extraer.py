#!/usr/bin/env python3
"""
wizard_extraer.py — la pestaña «Extraer metadata»: UN wizard de 3 pasos que reemplaza a
las viejas pestañas Metadata / Audio de fondo / Cara / Master.

  Paso 1 — INPUTS: el video de la grabación → lienzo con preview y DOS rectángulos
           (juego / cámara) editables con el mouse + las pistas de audio con forma de
           onda, mute/solo REAL (mezcla), nombre y ROL (voz / juego / …).
  Paso 2 — CONFIGURACIÓN: todas las opciones de los análisis en secciones, con presets
           GLOBALES (valores resueltos, no referencias mutables).
  Paso 3 — SALIDA: dónde se guarda todo → correr con barra TOTAL + barra de la TOOL
           actual + consola en vivo. Reanudable (banner si quedó una corrida a medias).

La ejecución real es 100% de pipeline.py (headless); este archivo es SOLO la UI.
Spec/diseño consensuado: three-brain-out/2026-07-16-tab-unificada/DISENO-final.md.
"""
from __future__ import annotations

import json
import queue
import sys
import threading
from pathlib import Path

import customtkinter as ctk

import dialogs
import editor_medios
import hardware
import marcas as marcas_mod
import medios
import pipeline
import app_paths

VIDEO_FILTERS = [("Video", ["*.mp4", "*.mov", "*.mkv", "*.avi", "*.webm"]), ("Todos", ["*"])]
ROLES = ["—", "voz", "juego", "chat", "aux", "ignorar"]
ROL_UNICO = {"voz", "juego"}                   # roles singleton
FUENTES_FILE = app_paths.WIZARD_SOURCES_FILE   # mapeo recordado por fingerprint
COLORES_RECT = {"juego": "#38b6ff", "camara": "#4ade80"}
HANDLE = 8                                     # px del handle de resize (en px LÓGICOS del canvas)
LANE_H = editor_medios.LANE_H                  # (el resto de las constantes del editor viven
                                               # en editor_medios.py — etapa 1 del refactor A1)


def _leer_fuentes() -> dict:
    try:
        return json.loads(FUENTES_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _guardar_fuentes(d: dict):
    # con retry de PermissionError (marcas._escribir_atomico): un lock transitorio de
    # AV en Windows tiraba excepción de callback Tk y perdía roles/rects recordados
    marcas_mod._escribir_atomico(FUENTES_FILE, json.dumps(d, ensure_ascii=False, indent=1))


class WizardExtraer:
    """La UI del wizard. `tab` = frame de la pestaña; `app` = la App (para _preset_bar,
    has_gpu, etc.). Maneja su PROPIA cola de eventos (los threads nunca tocan Tk)."""

    def __init__(self, tab, app):
        self.tab, self.app = tab, app
        self.q: queue.Queue = queue.Queue()    # eventos del PIPELINE (paso 3)
        self.rects = {"juego": {"norm": [0.0, 0.0, 0.5, 1.0], "confirmado": False},
                      "camara": {"norm": [0.5, 0.0, 1.0, 1.0], "confirmado": False}}
        self._pista_extra: list[dict] = []     # nombre/rol por pista (hook del editor)
        self._rec_roles: dict[int, str] = {}   # roles/nombres recordados (wizard_fuentes)
        self._rec_nombres: dict[int, str] = {}
        self.worker: threading.Thread | None = None
        self.cancel: threading.Event | None = None
        self._drag = None                      # gesto activo sobre los rects del preview
        self._consola_buf: list[str] = []

        tab.grid_columnconfigure(0, weight=1)
        tab.grid_rowconfigure(1, weight=1)

        # ---- header de navegación ----
        head = ctk.CTkFrame(tab, fg_color="transparent")
        head.grid(row=0, column=0, sticky="ew", padx=4, pady=(2, 6))
        head.grid_columnconfigure(1, weight=1)
        self.lbl_paso = ctk.CTkLabel(head, text="", font=ctk.CTkFont(size=14, weight="bold"))
        self.lbl_paso.grid(row=0, column=1)
        self.btn_atras = ctk.CTkButton(head, text="← Atrás", width=90, command=lambda: self._nav(-1))
        self.btn_atras.grid(row=0, column=0, sticky="w")
        self.btn_sig = ctk.CTkButton(head, text="Siguiente →", width=110, command=lambda: self._nav(+1))
        self.btn_sig.grid(row=0, column=2, sticky="e")

        # ---- los 3 pasos (frames apilados; se muestra uno) ----
        self.pasos = [ctk.CTkFrame(tab, fg_color="transparent") for _ in range(3)]
        for f in self.pasos:
            f.grid(row=1, column=0, sticky="nsew")
            f.grid_columnconfigure(0, weight=1)
        self._build_paso1(self.pasos[0])
        self._build_paso2(self.pasos[1])
        self._build_paso3(self.pasos[2])
        self.paso = 0
        self._mostrar_paso(0)
        tab.after(80, self._pump)

    # el ESTADO del medio vive en el EditorMedios (etapa 1 del refactor A1 —
    # three-brain-out/2026-07-21-tab-marcar); los pasos 2/3 siguen leyendo
    # self.info / self.fp / self.reg como siempre
    @property
    def info(self):
        return self.ed.info

    @property
    def fp(self):
        return self.ed.fp

    @property
    def reg(self):
        return self.ed.reg

    # ================================================================ navegación --
    def _mostrar_paso(self, n):
        if n != 0:
            # salir del paso 1 apaga TODO el preview aunque esté pausado (r3.3):
            # también el prefetch y su timer, que si no compiten con el pipeline
            self.ed.desactivar()
        else:
            self.ed.activar()                  # contrato simétrico (review impl r1.14)
        self.paso = n
        for i, f in enumerate(self.pasos):
            f.grid() if i == n else f.grid_remove()
        titulos = ["Paso 1 · Inputs (video, regiones y pistas)",
                   "Paso 2 · Configuración de los análisis",
                   "Paso 3 · Salida y ejecución"]
        self.lbl_paso.configure(text=titulos[n])
        self.btn_atras.configure(state="normal" if n > 0 else "disabled")
        self.btn_sig.configure(state="normal" if n < 2 else "disabled")
        if n == 2:
            self._chequear_reanudar()

    def _nav(self, delta):
        n = self.paso + delta
        if delta > 0 and self.paso == 0:
            err = self._validar_paso1()
            if err:
                self._log1(f"⚠ {err}")
                return
            self._recordar_fuente()
        self._mostrar_paso(max(0, min(2, n)))

    def _validar_paso1(self) -> str | None:
        if not self.info:
            return "Elegí un video primero."
        roles = self._roles_actuales()
        for rol in ROL_UNICO:
            n = sum(1 for r in roles.values() if r == rol)
            if n > 1:
                return f"El rol «{rol}» solo puede tener UNA pista (hay {n})."
        if "voz" not in roles.values() and "juego" not in roles.values():
            return "Asigná al menos una pista a «voz» o «juego»."
        if self.info.get("video") and not self.chk_conf.get():
            return "Confirmá las regiones del lienzo (revisá los rectángulos y marcá el check)."
        return None

    # ==================================================================== paso 1 --
    def _build_paso1(self, p):
        p.grid_rowconfigure(1, weight=1)       # el editor crece con la ventana

        ff = ctk.CTkFrame(p); ff.grid(row=0, column=0, sticky="ew", padx=4, pady=(0, 6))
        ff.grid_columnconfigure(0, weight=1)
        self.e_video = ctk.CTkEntry(ff, placeholder_text="Video de la grabación (multitrack de OBS)…")
        self.e_video.grid(row=0, column=0, padx=(12, 8), pady=10, sticky="ew")
        ctk.CTkButton(ff, text="Importar video", width=130, command=self._pick_video).grid(
            row=0, column=1, padx=(0, 12), pady=10)

        # ---- el MINI-EDITOR compartido (etapa 1 del refactor A1) ----
        self.ed = editor_medios.EditorMedios(
            p,
            controles_pista_extra=self._pista_extra_build,   # nombre + rol por pista
            overlay_preview=self._overlay_rects,             # rects JUEGO/CÁMARA
            on_video_cargado=self._on_video_cargado)         # roles/rects recordados
        self.ed.f.grid(row=1, column=0, sticky="nsew")

        # rects del preview: la INTERACCIÓN es del wizard (el editor solo dibuja el
        # frame y llama al overlay) — B1 sobre el canvas no choca con los bindings
        # de teclado/Configure del editor
        self.ed.canvas.bind("<Button-1>", self._rect_down)
        self.ed.canvas.bind("<B1-Motion>", self._rect_drag)
        self.ed.canvas.bind("<ButtonRelease-1>", self._rect_up)

        # el check de confirmación vive en el TRANSPORTE del editor (columna libre 4)
        self.chk_conf = ctk.CTkCheckBox(
            self.ed.fr_transporte, text="Confirmo las regiones (azul=JUEGO · verde=CÁMARA)",
            command=self._conf_rects)
        self.chk_conf.grid(row=0, column=4, padx=(10, 0), sticky="e")

    def _log1(self, m):
        self.ed.status(m)

    def _pick_video(self):
        f = dialogs.open_file("Video de la grabación", VIDEO_FILTERS, remember="wz_video")
        if not f:
            return
        self.e_video.delete(0, "end"); self.e_video.insert(0, f)
        self._cargar_video(f)

    def _cargar_video(self, path):
        self.chk_conf.deselect()
        self._conf_rects()
        self.ed.cargar(path)

    def _on_video_cargado(self, info, fp):
        """Hook del editor tras la inspección, ANTES de armar las pistas: recuperar
        roles/nombres/rects recordados por fingerprint (wizard_fuentes.json)."""
        recordado = _leer_fuentes().get(fp["hash_muestreado"][:12], {})
        self._rec_roles = {int(k): v for k, v in (recordado.get("roles") or {}).items()}
        self._rec_nombres = {int(k): v for k, v in (recordado.get("nombres") or {}).items()}
        if recordado.get("rects"):
            self.rects = recordado["rects"]
            for r in self.rects.values():
                r["confirmado"] = False        # igual exige re-confirmar (consenso: sugerencia)
        self._pista_extra = []

    def _pista_extra_build(self, fila, pista, i):
        """Hook del editor por pista: nombre + rol (columnas 0 y 3 — mute/solo del
        editor ocupan 1 y 2)."""
        w: dict = {"idx": pista["idx"]}
        w["nombre"] = ctk.CTkEntry(fila, width=120, height=26,
                                   placeholder_text=f"Pista {pista['idx'] + 1}")
        if self._rec_nombres.get(pista["idx"]) or pista["titulo"]:
            w["nombre"].insert(0, self._rec_nombres.get(pista["idx"]) or pista["titulo"])
        w["nombre"].grid(row=0, column=0, padx=(0, 4), pady=(LANE_H // 2 - 13, 0))
        w["rol"] = ctk.CTkOptionMenu(fila, values=ROLES, width=96, height=26)
        w["rol"].set(self._rec_roles.get(pista["idx"],
                                         "voz" if pista["idx"] == 0 else
                                         ("juego" if pista["idx"] == 1 else "—")))
        w["rol"].grid(row=0, column=3, padx=(2, 0), pady=(LANE_H // 2 - 13, 0))
        self._pista_extra.append(w)

    def _conf_rects(self):
        c = bool(self.chk_conf.get())
        for r in self.rects.values():
            r["confirmado"] = c

    # ---- pistas UI: nombre/rol viven en los widgets del hook (_pista_extra) ----
    def _roles_actuales(self) -> dict[int, str]:
        out = {}
        for w in self._pista_extra:
            rol = w["rol"].get()
            if rol != "—":
                out[w["idx"]] = rol
        return out

    def _recordar_fuente(self):
        if not self.fp:
            return
        d = _leer_fuentes()
        d[self.fp["hash_muestreado"][:12]] = {
            "roles": {str(k): v for k, v in self._roles_actuales().items()},
            "nombres": {str(w["idx"]): w["nombre"].get().strip()
                        for w in self._pista_extra if w["nombre"].get().strip()},
            "rects": self.rects,
        }
        _guardar_fuentes(d)

    # ---- lienzo: overlay + interacción de rects (la geometría es del editor) ----
    @staticmethod
    def _interseccion(a, b) -> list | None:
        """Intersección de dos rects normalizados [x0,y0,x1,y1]; None si no se tocan."""
        x0, y0 = max(a[0], b[0]), max(a[1], b[1])
        x1, y1 = min(a[2], b[2]), min(a[3], b[3])
        return [x0, y0, x1, y1] if (x1 - x0 > 0.005 and y1 - y0 > 0.005) else None

    def _overlay_rects(self, canvas, g):
        """Hook `overlay_preview` del editor: dibuja los rects JUEGO/CÁMARA sobre el
        frame (el editor ya pintó la imagen; acá van SOLO los overlays del wizard)."""
        ox, oy, iw, ih = g

        def a_canvas(n):
            return (ox + n[0] * iw, oy + n[1] * ih, ox + n[2] * iw, oy + n[3] * ih)

        for nombre, r in self.rects.items():
            cx0, cy0, cx1, cy1 = a_canvas(r["norm"])
            col = COLORES_RECT[nombre]
            canvas.create_rectangle(cx0, cy0, cx1, cy1, outline=col, width=2)
            canvas.create_text(cx0 + 6, cy0 + 10, text=nombre.upper(), fill=col,
                               anchor="w", font=("TkDefaultFont", 10, "bold"))
            for hx, hy in ((cx0, cy0), (cx1, cy0), (cx0, cy1), (cx1, cy1)):
                canvas.create_rectangle(hx - HANDLE / 2, hy - HANDLE / 2,
                                        hx + HANDLE / 2, hy + HANDLE / 2,
                                        fill=col, outline="")
        # solape cámara∩juego = HUECO: esa zona se le RESTA al juego (se registra en el
        # master y el futuro análisis de pantalla la excluye) — se muestra rayada
        inter = self._interseccion(self.rects["juego"]["norm"], self.rects["camara"]["norm"])
        if inter:
            ix0, iy0, ix1, iy1 = a_canvas(inter)
            canvas.create_rectangle(ix0, iy0, ix1, iy1, fill="#000000",
                                    stipple="gray50", outline="#ffb84d", dash=(4, 3))
            canvas.create_text((ix0 + ix1) / 2, (iy0 + iy1) / 2,
                               text="− excluido del juego", fill="#ffb84d",
                               font=("TkDefaultFont", 9, "bold"))

    def _hit(self, x, y):
        """Qué agarró el click: (rect, 'esquina', cual) o (rect, 'mover', None)."""
        g = self.ed.geo()
        if not g:
            return None
        ox, oy, iw, ih = g
        for nombre, r in self.rects.items():
            x0, y0, x1, y1 = r["norm"]
            cx0, cy0, cx1, cy1 = ox + x0 * iw, oy + y0 * ih, ox + x1 * iw, oy + y1 * ih
            for i, (hx, hy) in enumerate(((cx0, cy0), (cx1, cy0), (cx0, cy1), (cx1, cy1))):
                if abs(x - hx) <= HANDLE and abs(y - hy) <= HANDLE:
                    return nombre, "esquina", i
        for nombre, r in self.rects.items():
            x0, y0, x1, y1 = r["norm"]
            if ox + x0 * iw <= x <= ox + x1 * iw and oy + y0 * ih <= y <= oy + y1 * ih:
                return nombre, "mover", None
        return None

    def _rect_down(self, e):
        self._drag = None
        hit = self._hit(e.x, e.y)
        if hit:
            self._drag = (*hit, e.x, e.y, [list(r["norm"]) for r in self.rects.values()])

    def _rect_drag(self, e):
        if not self._drag or not self.info:
            return
        nombre, modo, esq, x0, y0, _ = self._drag
        g = self.ed.geo()
        if not g:
            return
        ox, oy, iw, ih = g
        dx, dy = (e.x - x0) / iw, (e.y - y0) / ih
        r = self.rects[nombre]["norm"]
        base = dict(zip(self.rects, self._drag[5]))[nombre]
        if modo == "mover":
            w, h = base[2] - base[0], base[3] - base[1]
            nx0 = min(max(0.0, base[0] + dx), 1.0 - w)
            ny0 = min(max(0.0, base[1] + dy), 1.0 - h)
            r[:] = [nx0, ny0, nx0 + w, ny0 + h]
        else:
            # esquinas: 0=NO 1=NE 2=SO 3=SE
            nx = min(max(0.0, (base[0] if esq in (0, 2) else base[2]) + dx), 1.0)
            ny = min(max(0.0, (base[1] if esq in (0, 1) else base[3]) + dy), 1.0)
            if esq in (0, 2):
                r[0] = min(nx, base[2] - 0.02)
            else:
                r[2] = max(nx, base[0] + 0.02)
            if esq in (0, 1):
                r[1] = min(ny, base[3] - 0.02)
            else:
                r[3] = max(ny, base[1] + 0.02)
        self.chk_conf.deselect()
        self._conf_rects()
        self.ed.redibujar()

    def _rect_up(self, _e):
        self._drag = None

    # ==================================================================== paso 2 --
    def _build_paso2(self, p):
        p.grid_rowconfigure(0, weight=1)
        sc = ctk.CTkScrollableFrame(p)
        sc.grid(row=0, column=0, sticky="nsew", padx=4, pady=(0, 4))
        sc.grid_columnconfigure(0, weight=1)
        self._sc2 = sc
        row = 0

        def seccion(titulo):
            nonlocal row
            ctk.CTkLabel(sc, text=titulo, font=ctk.CTkFont(size=14, weight="bold")
                         ).grid(row=row, column=0, padx=8, pady=(14 if row else 4, 2), sticky="w")
            row += 1
            fr = ctk.CTkFrame(sc)
            fr.grid(row=row, column=0, sticky="ew", padx=4, pady=(0, 2))
            fr.grid_columnconfigure(0, weight=1)
            row += 1
            return fr

        import core
        import hardware

        # ---- presets globales (guarda el estado de TODAS las secciones) ----
        bar = ctk.CTkFrame(sc, fg_color="transparent")
        bar.grid(row=row, column=0, sticky="ew"); bar.grid_columnconfigure(0, weight=1); row += 1
        self.app._preset_bar(bar, 0, "extraer", self._get_cfg, self._set_cfg)

        # ---- transcripción (de la voz; solo corre si falta words/segments sellados) ----
        fr = seccion("Transcripción de la voz")
        f1 = ctk.CTkFrame(fr, fg_color="transparent"); f1.grid(row=0, column=0, sticky="w", padx=8, pady=6)
        ctk.CTkLabel(f1, text="Modelo:").grid(row=0, column=0, padx=(4, 6))
        self.c_tx_modelo = ctk.CTkOptionMenu(f1, values=core.MODELS, width=150)
        self.c_tx_modelo.set(hardware.recommend_whisper_model())
        self.c_tx_modelo.grid(row=0, column=1, padx=(0, 14))
        ctk.CTkLabel(f1, text="Device:").grid(row=0, column=2, padx=(0, 6))
        dev = ["auto", "cpu"] + (["cuda"] if self.app.has_gpu else [])
        self.c_tx_dev = ctk.CTkSegmentedButton(f1, values=dev); self.c_tx_dev.set("auto")
        self.c_tx_dev.grid(row=0, column=3, padx=(0, 14))
        self.c_tx_align = ctk.CTkCheckBox(f1, text="Timestamps precisos (MMS)")
        self.c_tx_align.select()
        self.c_tx_align.grid(row=0, column=4)

        # ---- voz ----
        fr = seccion("Análisis de la voz")
        self.c_v = {}
        checks = [("emocion", "Emoción — arousal (audio) + sentiment (texto)"),
                  ("risa", "Risa — modelo dedicado"),
                  ("pausas", "Pausas — silencios del habla"),
                  ("ava", "Instrucciones a Ava — «hey Ava, …» dicho al grabar")]
        disponibles = self._disp_voz()
        for i, (k, txt) in enumerate(checks):
            cb = ctk.CTkCheckBox(fr, text=txt + ("" if disponibles[k] is True
                                                 else f"  ({disponibles[k]})"))
            cb.grid(row=i, column=0, padx=12, pady=(8 if i == 0 else 2, 2), sticky="w")
            if disponibles[k] is True:
                cb.select()
            else:
                cb.configure(state="disabled")
            self.c_v[k] = cb
        ctk.CTkLabel(fr, text="", height=4).grid(row=len(checks), column=0)

        # ---- fondo ----
        fr = seccion("Audio de fondo (pista «juego»)")
        self.c_f = {}
        import describir
        import escena_audio
        # descripcion NUNCA se deshabilita por falta de llama.cpp: el carril ONLINE
        # (OpenRouter/Alibaba) no lo necesita — solo se anota que local no está
        f_disp = {"dialogo": True,
                  "sonidos": True if escena_audio.available() else "falta transformers/librosa",
                  "descripcion": True}
        f_nota = {"descripcion": "" if describir.available()
                  else "  (sin llama.cpp local → usá online)"}
        ftxt = [("dialogo", "Diálogo del juego · Whisper + anti-alucinación"),
                ("sonidos", "Sonidos + transitorios + escenas · AST"),
                ("descripcion", "Descripción por escena · LALM")]
        for i, (k, txt) in enumerate(ftxt):
            cb = ctk.CTkCheckBox(fr, text=txt + ("" if f_disp[k] is True else f"  ({f_disp[k]})")
                                 + f_nota.get(k, ""))
            cb.grid(row=i, column=0, padx=12, pady=(8 if i == 0 else 2, 2), sticky="w")
            if f_disp[k] is True:
                # sin llama.cpp, descripcion queda ELEGIBLE (online) pero no pre-tildada
                if not (k == "descripcion" and not describir.available()):
                    cb.select()
            else:
                cb.configure(state="disabled")
            self.c_f[k] = cb
        f2 = ctk.CTkFrame(fr, fg_color="transparent"); f2.grid(row=3, column=0, sticky="w", padx=8, pady=6)
        ctk.CTkLabel(f2, text="Whisper juego:").grid(row=0, column=0, padx=(4, 6))
        self.c_f_wmodel = ctk.CTkOptionMenu(f2, values=core.MODELS, width=150)
        self.c_f_wmodel.set(hardware.recommend_whisper_model())
        self.c_f_wmodel.grid(row=0, column=1, padx=(0, 14))
        ctk.CTkLabel(f2, text="LALM:").grid(row=0, column=2, padx=(0, 6))
        self.c_f_dmodel = ctk.CTkOptionMenu(f2, values=list(describir.MODELS), width=150)
        self.c_f_dmodel.set(describir.DEFAULT_MODEL)
        self.c_f_dmodel.grid(row=0, column=3, padx=(0, 14))
        # cpu/gpu = llama.cpp local · online = OpenRouter (misma API key que visión)
        self.c_f_lalm = ctk.CTkSegmentedButton(f2, values=["cpu", "gpu", "online"],
                                               command=self._lalm_modo)
        self.c_f_lalm.set("cpu")
        self.c_f_lalm.grid(row=0, column=4)
        f2b = ctk.CTkFrame(fr, fg_color="transparent")
        f2b.grid(row=4, column=0, sticky="w", padx=8, pady=(0, 6))
        ctk.CTkLabel(f2b, text="Modelo online:").grid(row=0, column=0, padx=(4, 6))
        self.c_f_online = ctk.CTkOptionMenu(f2b, values=list(describir.MODELS_ONLINE),
                                            width=330, command=lambda _v: self._lalm_modo())
        self.c_f_online.set(describir.ONLINE_DEFAULT)
        self.c_f_online.grid(row=0, column=1, padx=(0, 10))
        self.lbl_f_online = ctk.CTkLabel(f2b, text="", text_color="gray55", anchor="w")
        self.lbl_f_online.grid(row=0, column=2)
        # key de Alibaba Model Studio (familia Qwen-Omni) — aparece solo si el modelo
        # elegido es de ese proveedor; la de OpenRouter vive en la sección Visión
        self.lbl_f_dskey = ctk.CTkLabel(f2b, text="API key Alibaba:")
        self.c_f_dskey = ctk.CTkEntry(
            f2b, width=330, show="•",          # enmascarada como la de OpenRouter (r1.4)
            placeholder_text="API key de Alibaba Model Studio (dashscope) — se guarda local…")
        if hardware.load().get("dashscope_api_key"):
            self.c_f_dskey.insert(0, hardware.load()["dashscope_api_key"])
        self.c_f_dskey.bind("<FocusOut>", self._dashscope_guardar_key)
        self.c_f_dskey.bind("<Return>", self._dashscope_guardar_key)
        f2b.grid_remove()
        self._f2b_online = f2b

        # ---- cara ----
        fr = seccion("Cara (facecam — región CÁMARA del lienzo)")
        import cara as cara_mod
        self.c_c_activar = ctk.CTkCheckBox(fr, text="Analizar la cara (MediaPipe)")
        self.c_c_activar.grid(row=0, column=0, padx=12, pady=(8, 2), sticky="w")
        self.c_c_emo = ctk.CTkCheckBox(fr, text="Emociones (EmotiEffLib): valence/arousal + etiqueta")
        self.c_c_emo.grid(row=1, column=0, padx=12, pady=2, sticky="w")
        if cara_mod.available():
            self.c_c_activar.select()
            if cara_mod.emocion_available():
                self.c_c_emo.select()
            else:
                self.c_c_emo.configure(state="disabled",
                                       text="Emociones: falta emotiefflib (pip install --no-deps emotiefflib)")
        else:
            for cb in (self.c_c_activar, self.c_c_emo):
                cb.configure(state="disabled")
            self.c_c_activar.configure(text="Analizar la cara — falta mediapipe (pip install mediapipe)")
        f3 = ctk.CTkFrame(fr, fg_color="transparent"); f3.grid(row=2, column=0, sticky="w", padx=8, pady=6)
        ctk.CTkLabel(f3, text="Preset mirada:").grid(row=0, column=0, padx=(4, 6))
        self.c_c_preset = ctk.CTkOptionMenu(f3, values=["(sin presets)"], width=140,
                                            command=self._mirada_preset)
        self.c_c_preset.grid(row=0, column=1, padx=(0, 10))
        ctk.CTkLabel(f3, text="Cámara:").grid(row=0, column=2, padx=(0, 6))
        self.c_c_cam = ctk.CTkOptionMenu(f3, values=["0", "1", "2", "3"], width=64)
        self.c_c_cam.set("0")
        self.c_c_cam.grid(row=0, column=3, padx=(0, 10))
        ctk.CTkButton(f3, text="⚙ Calibrar mirada en vivo", command=self._mirada_live).grid(
            row=0, column=4)
        self._cara_proc = None
        self._mirada_refresh()

        # ---- visión VLM (consenso three-brain-out/2026-07-17-vision-video) ----
        fr = seccion("Visión del video (VLM online — OpenRouter)")
        self.c_vis = {}
        for i, (k, txt) in enumerate([
                ("juego", "Interpretar el JUEGO (pantalla — región JUEGO, cámara enmascarada)"),
                ("camara", "Interpretar la CÁMARA (vos — región CÁMARA)")]):
            cb = ctk.CTkCheckBox(fr, text=txt, command=self._vision_estimar)
            cb.grid(row=i, column=0, columnspan=2, padx=12, pady=(8 if i == 0 else 2, 2),
                    sticky="w")
            self.c_vis[k] = cb                 # apagados por default: cuesta plata
        f5 = ctk.CTkFrame(fr, fg_color="transparent")
        f5.grid(row=2, column=0, sticky="ew", padx=8, pady=(6, 2))
        f5.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(f5, text="API key OpenRouter:").grid(row=0, column=0, padx=(4, 6))
        self.c_vis_key = ctk.CTkEntry(f5, show="•",
                                      placeholder_text="sk-or-v1-… (se guarda en config.json local)")
        self.c_vis_key.grid(row=0, column=1, sticky="ew", padx=(0, 10))
        if hardware.load().get("openrouter_api_key"):
            self.c_vis_key.insert(0, hardware.load()["openrouter_api_key"])
        self.c_vis_key.bind("<FocusOut>", self._vision_guardar_key)
        ctk.CTkLabel(f5, text="Modelo:").grid(row=0, column=2, padx=(0, 6))
        import vision as vision_mod
        self.c_vis_modelo = ctk.CTkEntry(f5, width=250)
        self.c_vis_modelo.insert(0, vision_mod.MODELO_DEFAULT)
        self.c_vis_modelo.grid(row=0, column=3)
        f6 = ctk.CTkFrame(fr, fg_color="transparent")
        f6.grid(row=3, column=0, sticky="w", padx=8, pady=(2, 2))
        self.c_vis_pin = ctk.CTkCheckBox(f6, text="Provider fijo (determinista)")
        self.c_vis_pin.select()
        self.c_vis_pin.grid(row=0, column=0, padx=(4, 14))
        self.c_vis_recache = ctk.CTkCheckBox(f6, text="Ignorar caché VLM (re-consultar)")
        self.c_vis_recache.grid(row=0, column=1, padx=(0, 14))
        ctk.CTkLabel(f6, text="Tope de chunks:").grid(row=0, column=2, padx=(0, 6))
        self.c_vis_tope = ctk.CTkEntry(f6, width=70)
        self.c_vis_tope.insert(0, "500")
        self.c_vis_tope.grid(row=0, column=3, padx=(0, 14))
        ctk.CTkLabel(f6, text="Frames máx/chunk:").grid(row=0, column=4, padx=(0, 6))
        import vision as _vm
        self.c_vis_frames = ctk.CTkOptionMenu(
            f6, values=[str(n) for n in range(4, _vm.MAX_FRAMES_TOPE + 1)], width=70)
        self.c_vis_frames.set(str(_vm.MAX_FRAMES))
        self.c_vis_frames.grid(row=0, column=5)
        self.lbl_vis = ctk.CTkLabel(
            fr, text="⚠ Envía imágenes del video a OpenRouter (tercero). El muestreo es "
                     "adaptativo: tramos quietos = 1 frame; tramos movidos = hasta el "
                     "máximo configurado por chunk.",
            text_color="gray55", anchor="w", justify="left", wraplength=760)
        self.lbl_vis.grid(row=4, column=0, sticky="ew", padx=12, pady=(4, 8))

        # ---- master ----
        fr = seccion("Master")
        f4 = ctk.CTkFrame(fr, fg_color="transparent"); f4.grid(row=0, column=0, sticky="ew", padx=8, pady=8)
        f4.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(f4, text="Nombre de salida:").grid(row=0, column=0, padx=(4, 8))
        self.c_m_nombre = ctk.CTkEntry(f4, placeholder_text="(default: el nombre del video)")
        self.c_m_nombre.grid(row=0, column=1, sticky="ew")

    def _disp_voz(self) -> dict:
        out = {"pausas": True, "ava": True}
        try:
            import metadata
            out["emocion"] = True if metadata.available() else "falta transformers/pysentimiento"
        except Exception:
            out["emocion"] = "no importa metadata"
        try:
            import laughter
            out["risa"] = True if laughter.available() else "falta el repo/deps de risa"
        except Exception:
            out["risa"] = "no importa laughter"
        return out

    def _mirada_refresh(self):
        try:
            import cara as cara_mod
            nombres, activo = cara_mod.mira_presets_listar()
            vals = nombres or ["(sin presets)"]
            self.c_c_preset.configure(values=vals)
            self.c_c_preset.set(activo if activo in vals else vals[0])
        except Exception:
            pass

    def _mirada_preset(self, nombre):
        if nombre.startswith("("):
            return
        import cara as cara_mod
        self._log3(cara_mod.mira_preset_cargar(nombre))

    def _mirada_live(self):
        if self._cara_proc is not None and self._cara_proc.poll() is None:
            return
        cmd = [sys.executable, str(Path(__file__).parent / "cara.py"),
               "--live", "--cam", self.c_c_cam.get()]
        if not self.c_c_emo.get():
            cmd.append("--sin-emociones")
        # popen_gestionado = flags Windows + Job Object: si la GUI muere, el SO mata a
        # cara.py --live (con Popen pelado quedaba huérfano — auditoría Windows pre-ship)
        self._cara_proc = medios.popen_gestionado(cmd, cwd=str(Path(__file__).parent))

    # ---- presets globales: VALORES RESUELTOS (consenso P3) ----
    def _get_cfg(self) -> dict:
        cfg = {"schema": 1, "config": self._config_actual(),
               "rects": {k: {"norm": list(v["norm"])} for k, v in self.rects.items()}}
        v = (self.info or {}).get("video")
        if v:
            cfg["rects_ref"] = {"width": v["width"], "height": v["height"]}
        try:                                   # calibración de mirada POR VALOR
            import cara as cara_mod
            cfg["mirada"] = {"zona": dict(cara_mod.MIRA_ZONA), "ojos": dict(cara_mod.OJOS_CAL)}
        except Exception:
            pass
        return cfg

    def _set_cfg(self, cfg: dict):
        c = cfg.get("config", {})
        tx = c.get("transcripcion", {})
        if tx.get("modelo"):
            self.c_tx_modelo.set(tx["modelo"])
        if tx.get("device"):
            self.c_tx_dev.set(tx["device"])
        self.app._setcb(self.c_tx_align, tx.get("align", True))
        for k, cb in self.c_v.items():
            if cb.cget("state") != "disabled":
                self.app._setcb(cb, c.get("voz", {}).get(k, bool(cb.get())))
        for k, cb in self.c_f.items():
            if cb.cget("state") != "disabled":
                self.app._setcb(cb, c.get("fondo", {}).get(k, bool(cb.get())))
        f = c.get("fondo", {})
        if f.get("wmodel"):
            self.c_f_wmodel.set(f["wmodel"])
        if f.get("dmodel"):
            self.c_f_dmodel.set(f["dmodel"])
        self.c_f_lalm.set("online" if f.get("lalm_backend") == "online"
                          else ("gpu" if f.get("lalm_gpu") else "cpu"))
        if f.get("lalm_online"):
            import describir as _d
            if f["lalm_online"] in _d.MODELS_ONLINE:
                self.c_f_online.set(f["lalm_online"])
        self._lalm_modo()
        cc = c.get("cara", {})
        if self.c_c_activar.cget("state") != "disabled":
            self.app._setcb(self.c_c_activar, cc.get("activar", True))
        if self.c_c_emo.cget("state") != "disabled":
            self.app._setcb(self.c_c_emo, cc.get("emociones", True))
        vis = c.get("vision", {})
        for k, cb in self.c_vis.items():
            self.app._setcb(cb, vis.get(k, False))
        if vis.get("modelo"):
            self.c_vis_modelo.delete(0, "end"); self.c_vis_modelo.insert(0, vis["modelo"])
        self.app._setcb(self.c_vis_pin, vis.get("provider_pin", True))
        self.app._setcb(self.c_vis_recache, vis.get("ignorar_cache", False))
        if vis.get("max_chunks"):
            self.c_vis_tope.delete(0, "end"); self.c_vis_tope.insert(0, str(vis["max_chunks"]))
        if vis.get("max_frames"):
            self.c_vis_frames.set(str(vis["max_frames"]))
        self._vision_estimar()
        # calibración de mirada POR VALOR: el preset la guardó resuelta → RESTAURARLA
        # (sin esto el run usaba la calibración global más reciente, no la del preset)
        mir = cfg.get("mirada") or {}
        if mir.get("zona") or mir.get("ojos"):
            try:
                import cara as cara_mod
                self._log3(cara_mod.mira_aplicar_valores(mir.get("zona"), mir.get("ojos")))
            except Exception:
                pass
        # rects del preset: SOLO si la resolución de referencia coincide (consenso P3)
        ref, v = cfg.get("rects_ref"), (self.info or {}).get("video")
        if cfg.get("rects") and ref and v and (ref["width"], ref["height"]) == (v["width"], v["height"]):
            for k in self.rects:
                if k in cfg["rects"]:
                    self.rects[k]["norm"] = list(cfg["rects"][k]["norm"])
                    self.rects[k]["confirmado"] = False
            self.chk_conf.deselect()
            self.ed.redibujar()

    def _config_actual(self) -> dict:
        return {
            "transcripcion": {"modelo": self.c_tx_modelo.get(), "idioma": "es",
                              "device": self.c_tx_dev.get(),
                              "align": bool(self.c_tx_align.get())},
            "voz": {k: bool(cb.get()) for k, cb in self.c_v.items()},
            "fondo": {**{k: bool(cb.get()) for k, cb in self.c_f.items()},
                      "wmodel": self.c_f_wmodel.get(), "dmodel": self.c_f_dmodel.get(),
                      "device": "auto", "lalm_gpu": self.c_f_lalm.get() == "gpu",
                      "lalm_backend": "online" if self.c_f_lalm.get() == "online" else "local",
                      "lalm_online": self.c_f_online.get()},
            "cara": {"activar": bool(self.c_c_activar.get()),
                     "emociones": bool(self.c_c_emo.get())},
            # la API key NO va acá (spec/presets/manifests): vive en config.json
            "vision": {"juego": bool(self.c_vis["juego"].get()),
                       "camara": bool(self.c_vis["camara"].get()),
                       "modelo": self.c_vis_modelo.get().strip() or None,
                       "provider_pin": bool(self.c_vis_pin.get()),
                       "ignorar_cache": bool(self.c_vis_recache.get()),
                       "max_chunks": int(self.c_vis_tope.get() or 500)
                       if (self.c_vis_tope.get() or "500").isdigit() else 500,
                       "max_frames": int(self.c_vis_frames.get())},
            "master": {"nombre": self.c_m_nombre.get().strip() or None},
        }

    def _lalm_modo(self, _v=None):
        """Muestra/oculta la fila del modelo online + estimación de costo (catálogo
        2026-07; lo real lo factura el proveedor del modelo: OpenRouter exige saldo
        ≥ $0.50 para audio; la familia Qwen-Omni va contra Alibaba con su propia key)."""
        import describir
        if self.c_f_lalm.get() == "online":
            self._f2b_online.grid()
            mod = self.c_f_online.get()
            prov = describir.proveedor_de(mod)
            dur = (self.info or {}).get("duracion") or 5400.0   # sin video: asume 90 min
            usd, detalle = describir.estimar_online(dur, mod)
            base = "este video" if self.info else "90 min (estimación sin video)"
            self.lbl_f_online.configure(
                text=f"≈ {detalle} para {base} · ⚠ sube el audio a {prov['nombre']}")
            if prov is describir.PROVEEDORES["dashscope"]:
                self.lbl_f_dskey.grid(row=1, column=0, padx=(4, 6), pady=(4, 0))
                self.c_f_dskey.grid(row=1, column=1, padx=(0, 10), pady=(4, 0), sticky="w")
            else:
                self.lbl_f_dskey.grid_remove()
                self.c_f_dskey.grid_remove()
        else:
            self._f2b_online.grid_remove()

    def _dashscope_guardar_key(self, _e=None):
        key = self.c_f_dskey.get().strip()
        if key != hardware.load().get("dashscope_api_key"):
            hardware.set_(dashscope_api_key=key)
            self._log3("API key de Alibaba (dashscope) guardada en config.json (local).")

    def _vision_guardar_key(self, _e=None):
        import hardware
        key = self.c_vis_key.get().strip()
        if key != hardware.load().get("openrouter_api_key"):
            hardware.set_(openrouter_api_key=key)
            self._log3("API key de OpenRouter guardada en config.json (local).")

    def _vision_estimar(self):
        """Estimación gruesa al tildar (chunks≈dur/18s, ~3.5 frames/chunk)."""
        lanes = [k for k, cb in self.c_vis.items() if cb.get()]
        if not lanes or not self.info:
            return
        dur = self.info["duracion"]
        ch = max(1, int(dur / 18)) * len(lanes)
        self.lbl_vis.configure(
            text=f"⚠ Envía imágenes del video a OpenRouter (tercero). Estimación para "
                 f"{'+'.join(lanes)}: ~{ch} chunks · ~{int(ch * 3.5)} frames "
                 f"(exacto en el log del paso 3 antes de la primera request; los tramos "
                 f"quietos van con 1 frame).")

    def _vision_disponible(self) -> bool:
        import hardware
        return bool(hardware.load().get("openrouter_api_key")
                    or __import__("os").environ.get("OPENROUTER_API_KEY"))

    # ==================================================================== paso 3 --
    def _build_paso3(self, p):
        p.grid_rowconfigure(4, weight=1)

        of = ctk.CTkFrame(p); of.grid(row=0, column=0, sticky="ew", padx=4, pady=(0, 6))
        of.grid_columnconfigure(0, weight=1)
        self.e_outdir = ctk.CTkEntry(of, placeholder_text="Carpeta de salida (toda la metadata + master.json)…")
        self.e_outdir.grid(row=0, column=0, padx=(12, 8), pady=10, sticky="ew")
        ctk.CTkButton(of, text="Elegir carpeta", width=120, command=self._pick_outdir).grid(
            row=0, column=1, padx=(0, 12), pady=10)

        self.lbl_reanudar = ctk.CTkLabel(p, text="", text_color="#e8b34b", anchor="w")
        self.lbl_reanudar.grid(row=1, column=0, sticky="ew", padx=10)

        bf = ctk.CTkFrame(p, fg_color="transparent"); bf.grid(row=2, column=0, sticky="ew", padx=4, pady=4)
        bf.grid_columnconfigure(0, weight=1)
        self.btn_correr = ctk.CTkButton(bf, text="▶ Extraer TODA la metadata", height=42,
                                        font=ctk.CTkFont(size=15, weight="bold"),
                                        command=self._correr)
        self.btn_correr.grid(row=0, column=0, sticky="ew")
        self.chk_rehacer = ctk.CTkCheckBox(bf, text="Re-hacer todo (ignorar lo ya calculado)")
        self.chk_rehacer.grid(row=0, column=1, padx=(12, 0))

        pf = ctk.CTkFrame(p); pf.grid(row=3, column=0, sticky="ew", padx=4, pady=(0, 6))
        pf.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(pf, text="Total", width=60).grid(row=0, column=0, padx=(12, 6), pady=(10, 2))
        self.pb_total = ctk.CTkProgressBar(pf); self.pb_total.set(0)
        self.pb_total.grid(row=0, column=1, sticky="ew", padx=(0, 8), pady=(10, 2))
        self.lbl_total = ctk.CTkLabel(pf, text="", width=110, anchor="e")
        self.lbl_total.grid(row=0, column=2, padx=(0, 12), pady=(10, 2))
        ctk.CTkLabel(pf, text="Paso", width=60).grid(row=1, column=0, padx=(12, 6), pady=(0, 10))
        self.pb_paso = ctk.CTkProgressBar(pf, progress_color="#38b6ff"); self.pb_paso.set(0)
        self.pb_paso.grid(row=1, column=1, sticky="ew", padx=(0, 8), pady=(0, 10))
        self.lbl_paso3 = ctk.CTkLabel(pf, text="", width=110, anchor="e")
        self.lbl_paso3.grid(row=1, column=2, padx=(0, 12), pady=(0, 10))

        self.consola = ctk.CTkTextbox(p, font=ctk.CTkFont(size=12))
        self.consola.grid(row=4, column=0, sticky="nsew", padx=4, pady=(0, 4))
        self.consola.configure(state="disabled")

    def _log3(self, m):
        self._consola_buf.append(m)

    def _pick_outdir(self):
        v = self.e_video.get().strip()
        start = str(Path(v).parent) if v and Path(v).exists() else None
        d = dialogs.open_dir("Carpeta de salida", start=start, remember="wz_outdir")
        if d:
            self.e_outdir.delete(0, "end"); self.e_outdir.insert(0, d)
            self._chequear_reanudar()

    def _outdir_default(self) -> str:
        v = self.e_video.get().strip()
        if v:
            return str(Path(v).parent / "metadata")
        return ""

    def _chequear_reanudar(self):
        out = self.e_outdir.get().strip() or self._outdir_default()
        if not out:
            self.lbl_reanudar.configure(text="")
            return
        est = pipeline.estado_previo(out)
        if est and est.get("status") in ("running", "cancelled", "failed_parcial"):
            self.lbl_reanudar.configure(
                text=f"↻ Hay una corrida anterior ({est.get('status')}) en esa carpeta — "
                     "al correr se RETOMA desde lo ya calculado (o marcá «Re-hacer todo»).")
        else:
            self.lbl_reanudar.configure(text="")

    def _spec(self) -> dict:
        outdir = self.e_outdir.get().strip() or self._outdir_default()
        rects = {k: dict(v, norm=list(v["norm"])) for k, v in self.rects.items()}
        # SUSTRACCIÓN: si la cámara solapa al juego, el juego registra ese HUECO — el
        # análisis de pantalla (futuro) recibe juego MENOS la caja de la cámara; queda
        # declarado en el master (header.media_layout.rects.juego.menos)
        inter = self._interseccion(rects["juego"]["norm"], rects["camara"]["norm"])
        if inter:
            rects["juego"]["menos"] = [inter]
        return {"fuente": self.info["path"], "outdir": outdir,
                "roles": {v: k for k, v in self._roles_actuales().items()
                          if v in ("voz", "juego")},
                "otros_roles": {str(k): v for k, v in self._roles_actuales().items()
                                if v not in ("voz", "juego")},
                "rects": rects,
                "config": self._config_actual(),
                "rehacer": bool(self.chk_rehacer.get())}

    def _correr(self):
        if self.worker and self.worker.is_alive():
            if self.cancel:
                self.cancel.set()
                self.btn_correr.configure(text="Cancelando…", state="disabled")
            return
        if not self.info:
            self._log3("⚠ Volvé al paso 1 y elegí un video.")
            return
        outdir = self.e_outdir.get().strip() or self._outdir_default()
        self.e_outdir.delete(0, "end"); self.e_outdir.insert(0, outdir)
        self.cancel = threading.Event()
        self.btn_correr.configure(text="⏹ Cancelar")
        self.pb_total.set(0); self.pb_paso.set(0)
        self._log3("═" * 52)
        spec = self._spec()

        def work():
            try:
                rep = pipeline.correr(spec, event_cb=lambda ev: self.q.put(("pipe", ev)),
                                      cancel=self.cancel)
                self.q.put(("fin", rep))
            except Exception as e:
                self.q.put(("fatal", str(e)))
        self.worker = threading.Thread(target=work, daemon=True)
        self.worker.start()

    def _reporte(self, rep):
        self.btn_correr.configure(text="▶ Extraer TODA la metadata", state="normal")
        self.chk_rehacer.deselect()
        icon = {"ok": "✔", "cancelled": "⏹", "failed_parcial": "⚠", "failed": "✗"}
        self._log3("")
        self._log3(f"{icon.get(rep['status'], '·')} RESULTADO: {rep['status'].upper()}")
        saltados = [p for p in rep["pasos"] if p["status"] == "skipped_unavailable"]
        fallidos = [p for p in rep["pasos"] if p["status"] == "failed"]
        for p in rep["pasos"]:
            marca = {"ok": "✔", "reused": "↻", "failed": "✗", "cancelled": "⏹",
                     "skipped_unavailable": "⤼", "skipped_not_requested": "·"}.get(p["status"], "?")
            extra = f" — {p['motivo']}" if p["motivo"] else ""
            self._log3(f"  {marca} {p['etiqueta']}{extra}")
        if saltados or fallidos:
            self._log3("")
            for p in fallidos:
                self._log3(f"✗ FALLÓ: {p['etiqueta'].strip()} — {p['motivo']}")
            for p in saltados:
                self._log3(f"⚠ NO SE EJECUTÓ: {p['etiqueta'].strip()} — {p['motivo']}")
        self._log3("")
        self._log3(f"Salida: {rep['outputs']['outdir']}")
        self._log3(f"Log completo: {rep['outputs']['log']}")
        self._chequear_reanudar()

    # ====================================================================== pump --
    def _pump(self):
        """Eventos del PIPELINE (paso 3). Los eventos del medio (insp/frame/env/tiles)
        viven en la cola PROPIA del EditorMedios — etapa 1 del refactor A1."""
        try:
            while True:
                m = self.q.get_nowait()
                k = m[0]
                if k == "pipe":
                    ev = m[1]
                    if ev["tipo"] == "log":
                        self._log3(ev["linea"])
                    elif ev["tipo"] == "prog":
                        self.pb_total.set(ev.get("total_frac") or 0)
                        self.lbl_total.configure(text=f"{int((ev.get('total_frac') or 0) * 100)}%")
                        if ev.get("paso_frac") is not None:
                            self.pb_paso.set(ev["paso_frac"])
                            self.lbl_paso3.configure(
                                text=f"{ev.get('paso', '')} {int(ev['paso_frac'] * 100)}%")
                    elif ev["tipo"] == "paso":
                        if ev["status"] == "running":
                            self.pb_paso.set(0)
                            self.lbl_paso3.configure(text=ev.get("etiqueta", ev["id"]))
                elif k == "fin":
                    self._reporte(m[1])
                elif k == "fatal":
                    self.btn_correr.configure(text="▶ Extraer TODA la metadata", state="normal")
                    self._log3(f"✗ ERROR: {m[1]}")
        except queue.Empty:
            pass
        # consola con BATCHING (una inserción por ciclo — no congela Tk durante Whisper)
        if self._consola_buf:
            txt = "\n".join(self._consola_buf) + "\n"
            self._consola_buf = []
            self.consola.configure(state="normal")
            self.consola.insert("end", txt); self.consola.see("end")
            self.consola.configure(state="disabled")
        self.tab.after(80, self._pump)

    def cerrar(self):
        """Cleanup al cerrar la app: apagar el editor (audición + stream + prefetch +
        envolventes/tiles) y cancelar el run si está vivo."""
        self.ed.cerrar()
        if self.cancel:
            self.cancel.set()
