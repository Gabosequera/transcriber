"""
app.py — App con workspace Automático y modo Manual con pestañas:
  · Transcribir    → audio → words/segments/srt/cues (GPU/CPU, con fallback).
  · Limpiar audio  → gate/ducker de respiraciones usando words.json (misma duración).

Extras:
  · Presets por pestaña (guardar con nombre, cambiar rápido, borrar con triple confirmación).
  · Layout de dos columnas cuando la ventana es ancha (controles a la izquierda, log a la derecha).
  · Selectores de archivo nativos (kdialog/zenity).

Lanzá con ./run.sh (prepara las libs CUDA) o:  .venv/bin/python app.py
"""
from __future__ import annotations

import time
_T0 = time.monotonic()   # PRIMERA línea ejecutable: mide los imports (review r2.17)

import os
os.environ.setdefault("KMP_DUPLICATE_LIB_OK", "TRUE")  # Windows: crash nativo por OpenMP duplicado (ver hardware.py)

import app_paths
app_paths.configure_cache_environment()
app_paths.ensure_persistent_dirs()

import json
import queue
import sys
import threading
import time
import traceback
from pathlib import Path


def _ensure_std_streams() -> None:
    """Lanzada con pythonw (sin consola) la app arranca con sys.stdout/sys.stderr = None.
    Cualquier librería que imprima progreso (tqdm en las descargas de torch.hub / HuggingFace,
    p. ej. la 1ª descarga del modelo MMS) muere con "'NoneType' object has no attribute 'write'".
    Se redirigen a shared/logs/salida.log para que nada falle y quede rastro."""
    if sys.stdout is not None and sys.stderr is not None:
        return
    stream = None
    try:
        app_paths.LOGS_DIR.mkdir(parents=True, exist_ok=True)
        target = app_paths.LOGS_DIR / "salida.log"
        mode = "w" if target.exists() and target.stat().st_size > 5 * 1024 * 1024 else "a"
        stream = open(target, mode, encoding="utf-8", errors="replace", buffering=1)
    except OSError:
        try:
            stream = open(os.devnull, "w", encoding="utf-8")
        except OSError:
            return
    if sys.stdout is None:
        sys.stdout = stream
    if sys.stderr is None:
        sys.stderr = stream
    if sys.__stdout__ is None:
        sys.__stdout__ = stream
    if sys.__stderr__ is None:
        sys.__stderr__ = stream
    # sin consola las barras de progreso solo ensucian el log: refrescar poco.
    os.environ.setdefault("TQDM_MININTERVAL", "5")


_ensure_std_streams()

from tkinter import messagebox

import customtkinter as ctk

import align
import core
import describir
import detect_breaths
import dialogs
import gate
import hardware
import jobs
import models
import presets

CRASH_LOG = app_paths.LOGS_DIR / "crash.log"


def log_crash(exc_type, exc, tb):
    """Escribe cualquier error (incluidos los de callbacks de Tk, que si no se
    tragarían al lanzar desde el menú) en crash.log y en stderr."""
    text = "".join(traceback.format_exception(exc_type, exc, tb))
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    try:
        with open(CRASH_LOG, "a", encoding="utf-8") as f:
            f.write(f"\n===== crash {stamp} =====\n{text}\n")
    except Exception:
        pass
    print(f"[crash {stamp}]\n{text}", file=sys.stderr)

ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("green")

AUDIO_FILTERS = [("Audio/Video", ["*" + e for e in core.AUDIO_EXTS]), ("Todos", ["*"])]
GATE_FILTERS = [("Audio", ["*" + e for e in gate.GATE_EXTS]), ("Todos", ["*"])]

# modos de limpieza (etiqueta visible). "Quirúrgico" = red de respiraciones Respiro-en.
GM_ADV, GM_VAD, GM_SURG = "Avanzado", "VAD", "Quirúrgico"
JSON_FILTERS = [("JSON", ["*.json"]), ("Todos", ["*"])]
WAV_FILTERS = [("WAV", ["*.wav"]), ("FLAC", ["*.flac"]), ("Todos", ["*"])]

WIDE_THRESHOLD = 1000   # px de ancho a partir del cual se usan dos columnas
NO_PRESET = "— presets —"


class App(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.report_callback_exception = log_crash   # captura errores de callbacks de Tk
        self.title("Transcriptor")
        self.geometry("1180x780")
        self.minsize(860, 620)
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(1, weight=1)

        self.msgs: queue.Queue = queue.Queue()
        self.cancel = threading.Event()
        self.worker: threading.Thread | None = None
        self.gate_worker: threading.Thread | None = None
        self._tabrefs: dict = {}
        self._wide: dict = {}

        # detección BARATA (nvidia-smi): cuda_device_count() importaba ctranslate2→torch
        # (~3 s de arranque); el chequeo real sigue en resolve_device() al transcribir
        _gpu = core.gpu_name()
        self.has_gpu = _gpu is not None
        self.gpu_label = _gpu or "GPU NVIDIA"

        self._modo = "Automático"
        self._menu_abierto = False
        self._vista_antes_ajustes = "automatico"
        self._vista_actual = None
        self.configure(fg_color="#111513")
        self._build_app_header()

        self.content = ctk.CTkFrame(self, fg_color="#111513", corner_radius=0)
        self.content.grid(row=1, column=0, sticky="nsew")
        self.content.grid_columnconfigure(0, weight=1)
        self.content.grid_rowconfigure(0, weight=1)

        self.tabs = ctk.CTkTabview(self.content, command=self._al_cambiar_tab)
        self._build_transcribe(self.tabs.add("Transcribir"))
        self._build_gate(self.tabs.add("Limpiar audio"))
        # Metadata + Audio de fondo + Cara + Master → UNA pestaña (wizard de 3 pasos)
        import wizard_extraer
        self.wizard = wizard_extraer.WizardExtraer(self.tabs.add("Extraer metadata"), self)
        # revisión POST-extracción: metadata sobre el timeline + guion para la AI
        # (diseño three-brain-out/2026-07-21-tab-marcar)
        import marcar
        self.marcar = marcar.TabMarcar(self.tabs.add("Marcar"), self)

        self.settings_page = ctk.CTkFrame(self.content, fg_color="#111513", corner_radius=0)
        self.settings_page.grid_columnconfigure(0, weight=1)
        self.settings_page.grid_rowconfigure(1, weight=1)
        settings_head = ctk.CTkFrame(self.settings_page, fg_color="transparent")
        settings_head.grid(row=0, column=0, sticky="ew", padx=22, pady=(14, 4))
        settings_head.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(settings_head, text="Ajustes",
                     font=ctk.CTkFont(size=20, weight="bold")).grid(row=0, column=0, sticky="w")
        ctk.CTkButton(settings_head, text="Volver", width=84, fg_color="#242b27",
                      hover_color="#303833", command=self._volver_de_ajustes).grid(
            row=0, column=1, sticky="e")
        settings_body = ctk.CTkFrame(self.settings_page, fg_color="transparent")
        settings_body.grid(row=1, column=0, sticky="nsew", padx=18, pady=(0, 12))
        self._build_settings(settings_body)

        from automatico_ui import AutomaticWorkspace
        self.automatico = AutomaticWorkspace(self.content)
        self._tab_activa = self.tabs.get()
        self._mostrar_vista("automatico")

        self.bind("<Configure>", self._on_resize)
        self.protocol("WM_DELETE_WINDOW", self._cerrar)
        self.after(120, self._pump)
        self.after(120, self._on_resize)

    def _build_app_header(self):
        """Header común: identidad a la izquierda y menú global a la derecha."""
        header = ctk.CTkFrame(self, height=58, fg_color="#181d1a", corner_radius=0,
                              border_width=0)
        header.grid(row=0, column=0, sticky="ew")
        header.grid_propagate(False)
        header.grid_columnconfigure(1, weight=1)

        mark = ctk.CTkFrame(header, width=28, height=28, fg_color="#35a978", corner_radius=7)
        mark.grid(row=0, column=0, padx=(18, 10), pady=15)
        mark.grid_propagate(False)
        ctk.CTkLabel(mark, text="T", text_color="#f1f6f2",
                     font=ctk.CTkFont(size=14, weight="bold")).place(relx=.5, rely=.5, anchor="center")

        identity = ctk.CTkFrame(header, fg_color="transparent")
        identity.grid(row=0, column=1, sticky="w")
        ctk.CTkLabel(identity, text="Transcriptor", text_color="#eef3ef",
                     font=ctk.CTkFont(size=14, weight="bold")).grid(row=0, column=0, sticky="w")
        self.mode_label = ctk.CTkLabel(identity, text="MODO AUTOMÁTICO", text_color="#7e8a83",
                                       font=ctk.CTkFont(size=9, weight="bold"))
        self.mode_label.grid(row=1, column=0, sticky="w")

        self.menu_btn = ctk.CTkButton(
            header, text="⋮", width=38, height=34, corner_radius=8,
            fg_color="transparent", hover_color="#2a322d", text_color="#dfe6e1",
            font=ctk.CTkFont(size=24), command=self._toggle_menu,
        )
        self.menu_btn.grid(row=0, column=2, padx=(8, 16), pady=12)

        self.menu_panel = ctk.CTkFrame(
            self, width=268, fg_color="#202622", corner_radius=10,
            border_width=1, border_color="#39423d",
        )
        self.menu_panel.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(self.menu_panel, text="MODO DE TRABAJO", text_color="#8b9790",
                     font=ctk.CTkFont(size=10, weight="bold")).grid(
            row=0, column=0, sticky="w", padx=14, pady=(14, 8))
        self.mode_switch = ctk.CTkSegmentedButton(
            self.menu_panel, values=["Automático", "Manual"], command=self._cambiar_modo,
            selected_color="#35a978", selected_hover_color="#2d9168",
            unselected_color="#171c19", unselected_hover_color="#2a322d",
        )
        self.mode_switch.set("Automático")
        self.mode_switch.grid(row=1, column=0, sticky="ew", padx=12, pady=(0, 12))
        sep = ctk.CTkFrame(self.menu_panel, height=1, fg_color="#39423d")
        sep.grid(row=2, column=0, sticky="ew", padx=12)
        ctk.CTkButton(
            self.menu_panel, text="Ajustes", anchor="w", height=38,
            fg_color="transparent", hover_color="#2a322d", command=self._abrir_ajustes,
        ).grid(row=3, column=0, sticky="ew", padx=8, pady=8)
        ctk.CTkFrame(self.menu_panel, height=1, fg_color="#39423d").grid(
            row=4, column=0, sticky="ew", padx=12)
        self.version_label = ctk.CTkLabel(
            self.menu_panel, text=f"Versión {self._app_version()}", anchor="w",
            text_color="#aeb8b2", font=ctk.CTkFont(size=11),
        )
        self.version_label.grid(row=5, column=0, sticky="ew", padx=14, pady=(9, 2))
        self.update_status_label = ctk.CTkLabel(
            self.menu_panel, text=self._update_status_text(), anchor="w", justify="left",
            wraplength=238, text_color="#7e8a83", font=ctk.CTkFont(size=10),
        )
        self.update_status_label.grid(row=6, column=0, sticky="ew", padx=14, pady=(0, 5))
        self.update_button = ctk.CTkButton(
            self.menu_panel, text="Buscar actualización", anchor="w", height=34,
            fg_color="transparent", hover_color="#2a322d", command=self._buscar_actualizacion,
        )
        self.update_button.grid(row=7, column=0, sticky="ew", padx=8, pady=(0, 8))

    @staticmethod
    def _app_version():
        value = os.environ.get("TRANSCRIPTOR_RELEASE_VERSION", "").strip()
        if value:
            return value
        try:
            return (Path(__file__).with_name("VERSION")).read_text(encoding="utf-8").strip()
        except OSError:
            return "desarrollo"

    @staticmethod
    def _update_status_text():
        if not app_paths.MANAGED_INSTALL:
            return "Actualizaciones: instalación de desarrollo"
        try:
            import release_state
            data = release_state.read_json(app_paths.STATE_DIR / "update-status.json", {})
            return str(data.get("message") or "Actualizaciones automáticas activas")
        except Exception:
            return "Actualizaciones automáticas activas"

    def _buscar_actualizacion(self):
        if not app_paths.MANAGED_INSTALL:
            self.update_status_label.configure(text="Disponible en instalaciones administradas de Windows")
            return
        self.update_button.configure(state="disabled", text="Buscando…")

        def work():
            try:
                import updater
                cfg = updater.load_update_config(app_paths.INSTALL_ROOT, Path(__file__).parent)
                if not cfg["repository"]:
                    result = "Falta configurar el repositorio GitHub"
                else:
                    client = updater.GitHubClient(cfg["repository"], cfg["check_timeout_seconds"])
                    candidate = updater.check_for_update(client, self._app_version())
                    result = (f"Versión {candidate.version} disponible; se instalará al reiniciar"
                              if candidate else "La aplicación está actualizada")
            except Exception as error:
                result = f"No se pudo consultar GitHub: {error}"
            self.msgs.put(("update_status", result))

        threading.Thread(target=work, daemon=True, name="update-check").start()

    def _toggle_menu(self):
        self._menu_abierto = not self._menu_abierto
        if self._menu_abierto:
            self.menu_panel.place(relx=1, x=-16, y=54, anchor="ne")
            self.menu_panel.lift()
        else:
            self.menu_panel.place_forget()

    def _cerrar_menu(self):
        self._menu_abierto = False
        self.menu_panel.place_forget()

    def _mostrar_vista(self, vista):
        if self._vista_actual == "automatico" and vista != "automatico":
            try:
                self.automatico.desactivar()
            except Exception:
                pass
        if self._vista_actual == "manual" and vista != "manual":
            try:
                if self.tabs.get() == "Marcar":
                    self.marcar.desactivar()
                elif self.tabs.get() == "Extraer metadata" and self.wizard.paso == 0:
                    self.wizard.ed.desactivar()
            except Exception:
                pass
        self.automatico.f.grid_forget()
        self.tabs.grid_forget()
        self.settings_page.grid_forget()
        if vista == "automatico":
            self.automatico.f.grid(row=0, column=0, sticky="nsew")
            try:
                self.automatico.activar()
            except Exception:
                pass
        elif vista == "manual":
            self.tabs.grid(row=0, column=0, sticky="nsew", padx=10, pady=(2, 10))
        else:
            self.settings_page.grid(row=0, column=0, sticky="nsew")
        self._vista_actual = vista
        if vista == "manual":
            try:
                if self.tabs.get() == "Marcar":
                    self.marcar.activar()
                elif self.tabs.get() == "Extraer metadata" and self.wizard.paso == 0:
                    self.wizard.ed.activar()
            except Exception:
                pass

    def _cambiar_modo(self, modo):
        self._modo = modo
        self.mode_label.configure(text=f"MODO {modo.upper()}")
        self._mostrar_vista("automatico" if modo == "Automático" else "manual")
        self._cerrar_menu()

    def _abrir_ajustes(self):
        self._vista_antes_ajustes = "automatico" if self._modo == "Automático" else "manual"
        self._mostrar_vista("ajustes")
        self._cerrar_menu()

    def _volver_de_ajustes(self):
        self._mostrar_vista(self._vista_antes_ajustes)

    def _al_cambiar_tab(self):
        """Cambio de pestaña (h.11): la que SALE pausa su reproducción/timers; la que
        entra refresca. Solo wizard y Marcar tienen editor de medios."""
        nueva = self.tabs.get()
        vieja, self._tab_activa = self._tab_activa, nueva
        try:
            if vieja == "Extraer metadata" and vieja != nueva:
                self.wizard.ed.desactivar()
            elif vieja == "Marcar" and vieja != nueva:
                self.marcar.desactivar()
            if nueva == "Marcar":
                self.marcar.activar()
            elif nueva == "Extraer metadata" and self.wizard.paso == 0:
                self.wizard.ed.activar()       # contrato simétrico (review impl r1.14)
        except Exception:
            pass

    def _cerrar(self):
        """Cierre limpio: corta la audición de wizard Y Marcar, y pide cancelar un run
        activo (el estado queda committeado — se retoma al reabrir)."""
        for comp in (getattr(self, "automatico", None), getattr(self, "wizard", None),
                     getattr(self, "marcar", None)):
            try:
                comp is not None and comp.cerrar()
            except Exception:
                pass
        self.destroy()

    # ============================================================ helpers UI --
    def _slider(self, parent, row, label, frm, to, default, hint, unit="s", step=0.01):
        if unit == "s":
            fmt = lambda v: f"{v:.2f} s"
        elif unit == "dB":
            fmt = lambda v: f"{v:.0f} dB"
        elif unit == "ms":
            fmt = lambda v: f"{v:.0f} ms"
        else:                       # número crudo (p.ej. threshold): 3 decimales
            fmt = lambda v: f"{v:.3f}"
        lab = ctk.CTkLabel(parent, text=label)
        lab.grid(row=row, column=0, padx=(12, 8), pady=(10, 0), sticky="w")
        vlab = ctk.CTkLabel(parent, text=fmt(default), width=64, font=ctk.CTkFont(size=12, weight="bold"))
        vlab.grid(row=row, column=2, padx=(8, 12), pady=(10, 0), sticky="e")
        s = ctk.CTkSlider(parent, from_=frm, to=to, number_of_steps=max(1, int(round((to - frm) / step))),
                          command=lambda v, l=vlab, f=fmt: l.configure(text=f(float(v))))
        s.set(default)
        s.grid(row=row, column=1, padx=8, pady=(10, 0), sticky="ew")
        hint_lab = ctk.CTkLabel(parent, text=hint, text_color="gray55", font=ctk.CTkFont(size=11))
        hint_lab.grid(row=row + 1, column=0, columnspan=3, padx=12, pady=(0, 6), sticky="w")
        s._vlab, s._vfmt = vlab, fmt
        s._row = (lab, vlab, s, hint_lab)
        return s

    @staticmethod
    def _set_slider(s, v):
        s.set(v)
        s._vlab.configure(text=s._vfmt(float(v)))

    @staticmethod
    def _show_slider(s, show):
        for w in s._row:
            w.grid() if show else w.grid_remove()

    @staticmethod
    def _setcb(cb, val):
        cb.select() if val else cb.deselect()

    def _preset_bar(self, parent, row, section, get_cfg, set_cfg):
        fr = ctk.CTkFrame(parent)
        fr.grid(row=row, column=0, sticky="ew", padx=12, pady=(10, 10))
        fr.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(fr, text="Preset").grid(row=0, column=0, padx=(12, 6), pady=10)
        menu = ctk.CTkOptionMenu(fr, values=[NO_PRESET] + presets.names(section),
                                 command=lambda n, s=section, f=set_cfg: self._apply_preset(s, n, f))
        menu.set(NO_PRESET)
        menu.grid(row=0, column=1, padx=6, pady=10, sticky="ew")
        ctk.CTkButton(fr, text="Guardar", width=76,
                      command=lambda s=section, g=get_cfg, m=menu: self._save_preset(s, g, m)).grid(
            row=0, column=2, padx=6, pady=10)
        ctk.CTkButton(fr, text="Eliminar", width=76, fg_color="#8a2f2f", hover_color="#6f2525",
                      command=lambda s=section, m=menu: self._delete_preset(s, m)).grid(
            row=0, column=3, padx=(6, 12), pady=10)
        return menu

    def _apply_preset(self, section, name, set_cfg):
        if name == NO_PRESET:
            return
        cfg = presets.get(section, name)
        if cfg:
            set_cfg(cfg)

    def _save_preset(self, section, get_cfg, menu):
        name = ctk.CTkInputDialog(text="Nombre del preset:", title="Guardar preset").get_input()
        if not name or not name.strip():
            return
        name = name.strip()
        if name == NO_PRESET:
            return
        presets.put(section, name, get_cfg())
        menu.configure(values=[NO_PRESET] + presets.names(section))
        menu.set(name)

    def _delete_preset(self, section, menu):
        name = menu.get()
        if name == NO_PRESET:
            messagebox.showinfo("Eliminar preset", "Elegí un preset primero.")
            return
        # triple confirmación
        if not messagebox.askyesno("Eliminar preset (1/3)", f"¿Eliminar el preset «{name}»?"):
            return
        if not messagebox.askyesno("Confirmar (2/3)", "Esto no se puede deshacer. ¿Seguro?"):
            return
        if not messagebox.askokcancel("Última confirmación (3/3)",
                                      f"Se borrará «{name}» definitivamente. ¿Confirmás?"):
            return
        presets.delete(section, name)
        menu.configure(values=[NO_PRESET] + presets.names(section))
        menu.set(NO_PRESET)

    # =============================================================== reflow --
    def _on_resize(self, _evt=None):
        # TODAS las pestañas registradas — una lista hardcodeada acá dejó la pestaña
        # "Cara" sin reflow inicial (= tab en blanco) cuando se agregaron tabs nuevas
        for key in self._tabrefs:
            self._reflow(key)

    def _reflow(self, key):
        refs = self._tabrefs.get(key)
        if not refs:
            return
        p, left, right = refs
        wide = self.winfo_width() >= WIDE_THRESHOLD
        if self._wide.get(key) == wide:
            return
        self._wide[key] = wide
        left.grid_forget(); right.grid_forget()
        if wide:  # dos columnas: controles | log
            p.grid_columnconfigure(0, weight=1)
            p.grid_columnconfigure(1, weight=1)
            p.grid_rowconfigure(0, weight=1)
            p.grid_rowconfigure(1, weight=0)
            left.grid(row=0, column=0, sticky="nsew", padx=(0, 5))
            right.grid(row=0, column=1, sticky="nsew", padx=(5, 0))
        else:     # una columna: controles arriba, log abajo
            p.grid_columnconfigure(1, weight=0)
            p.grid_columnconfigure(0, weight=1)
            p.grid_rowconfigure(0, weight=1)
            p.grid_rowconfigure(1, weight=1)
            left.grid(row=0, column=0, sticky="nsew")
            right.grid(row=1, column=0, sticky="nsew")

    # ====================================================== TAB: AJUSTES --
    def _build_settings(self, p):
        """Panel de configuración GLOBAL: detecta el hardware (CPU/RAM/GPU) y deja
        elegir cuántos hilos de CPU usar y si preferir la GPU para todo lo posible.
        Se guarda en config.json (hardware.py) y lo leen todos los módulos."""
        p.grid_columnconfigure(0, weight=1)
        wrap = ctk.CTkScrollableFrame(p, fg_color="transparent")
        wrap.grid(row=0, column=0, sticky="nsew", padx=2, pady=2)
        wrap.grid_columnconfigure(0, weight=1)
        p.grid_rowconfigure(0, weight=1)
        pad = {"padx": 6, "pady": (0, 12)}

        ctk.CTkLabel(wrap, text="Configuración del sistema",
                     font=ctk.CTkFont(size=18, weight="bold")).grid(
            row=0, column=0, sticky="w", padx=8, pady=(6, 2))
        ctk.CTkLabel(wrap, text="Se detecta automáticamente en cada equipo. Los cambios se guardan y "
                     "los usan todas las pestañas.", text_color="gray60",
                     font=ctk.CTkFont(size=12), justify="left", anchor="w", wraplength=640).grid(
            row=1, column=0, sticky="w", padx=8, pady=(0, 12))

        # ---- hardware detectado ----
        hw = ctk.CTkFrame(wrap); hw.grid(row=2, column=0, sticky="ew", **pad)
        hw.grid_columnconfigure(1, weight=1)
        self._hw_frame = hw
        ctk.CTkLabel(hw, text="Hardware detectado", font=ctk.CTkFont(size=14, weight="bold")).grid(
            row=0, column=0, columnspan=2, sticky="w", padx=12, pady=(10, 6))
        self._hw_rows = ctk.CTkFrame(hw, fg_color="transparent")
        self._hw_rows.grid(row=1, column=0, columnspan=2, sticky="ew", padx=12, pady=(0, 6))
        self._hw_rows.grid_columnconfigure(1, weight=1)
        ctk.CTkButton(hw, text="↻ Redetectar", width=110, command=self._settings_detect).grid(
            row=2, column=0, columnspan=2, sticky="w", padx=12, pady=(0, 10))

        # ---- CPU ----
        cf = ctk.CTkFrame(wrap); cf.grid(row=3, column=0, sticky="ew", **pad)
        cf.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(cf, text="CPU", font=ctk.CTkFont(size=14, weight="bold")).grid(
            row=0, column=0, sticky="w", padx=12, pady=(10, 2))
        self.cpu_max = ctk.CTkCheckBox(
            cf, text="Usar todos los núcleos/hilos de la CPU (máximo rendimiento)",
            command=self._on_cpu_max)
        self.cpu_max.grid(row=1, column=0, sticky="w", padx=12, pady=(2, 4))
        # slider de nº de hilos (visible solo si el máximo está desactivado)
        self.cpu_thr_frame = ctk.CTkFrame(cf, fg_color="transparent")
        self.cpu_thr_frame.grid(row=2, column=0, sticky="ew", padx=12, pady=(0, 4))
        self.cpu_thr_frame.grid_columnconfigure(0, weight=1)
        n_log = hardware.cpu_logical()
        self.cpu_thr_lbl = ctk.CTkLabel(self.cpu_thr_frame, text="", font=ctk.CTkFont(size=12))
        self.cpu_thr_lbl.grid(row=0, column=0, sticky="w")
        self.cpu_thr = ctk.CTkSlider(self.cpu_thr_frame, from_=1, to=max(1, n_log),
                                     number_of_steps=max(1, n_log - 1), command=self._on_cpu_thr)
        self.cpu_thr.grid(row=1, column=0, sticky="ew", pady=(0, 2))
        ctk.CTkLabel(cf, text="Menos hilos = más liviano si querés usar la PC para otra cosa mientras procesa.",
                     text_color="gray55", font=ctk.CTkFont(size=11), justify="left",
                     anchor="w", wraplength=640).grid(row=3, column=0, sticky="w", padx=12, pady=(0, 10))

        # ---- GPU ----
        gf = ctk.CTkFrame(wrap); gf.grid(row=4, column=0, sticky="ew", **pad)
        gf.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(gf, text="GPU", font=ctk.CTkFont(size=14, weight="bold")).grid(
            row=0, column=0, sticky="w", padx=12, pady=(10, 2))
        # lista de GPUs detectadas (dedicada + integrada)
        self._gpu_list = ctk.CTkFrame(gf, fg_color="transparent")
        self._gpu_list.grid(row=1, column=0, sticky="ew", padx=12, pady=(0, 6))
        self._gpu_list.grid_columnconfigure(0, weight=1)
        # carril CUDA (whisper + torch)
        self.gpu_prefer = ctk.CTkCheckBox(
            gf, text="Preferir la GPU para todo lo posible  (Whisper + modelos de IA · carril CUDA)",
            command=self._on_gpu_prefer)
        self.gpu_prefer.grid(row=2, column=0, sticky="w", padx=12, pady=(4, 4))
        self.gpu_note = ctk.CTkLabel(gf, text="", text_color="gray55", font=ctk.CTkFont(size=11),
                                     justify="left", anchor="w", wraplength=640)
        self.gpu_note.grid(row=3, column=0, sticky="w", padx=12, pady=(0, 8))
        # carril Vulkan (modelo de descripción / LALM) — acá SÍ se puede elegir la iGPU
        self.llama_row = ctk.CTkFrame(gf, fg_color="transparent")
        self.llama_row.grid(row=4, column=0, sticky="ew", padx=12, pady=(0, 4))
        self.llama_row.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(self.llama_row, text="GPU del modelo de descripción (Vulkan)").grid(
            row=0, column=0, sticky="w", pady=2)
        self.llama_menu = ctk.CTkOptionMenu(self.llama_row, values=["Automática"],
                                            command=self._on_llama_dev, dynamic_resizing=False)
        self.llama_menu.grid(row=0, column=1, sticky="e", padx=(10, 0), pady=2)
        self._llama_dev_map = {"Automática (recomendada)": "auto"}
        ctk.CTkLabel(gf, text="El modelo de descripción corre por Vulkan → puede usar CUALQUIER GPU, "
                     "incluida la integrada (iGPU). 'Automática': si el modelo ENTERO entra en una GPU "
                     "dedicada, va todo ahí (lo más rápido); si no, solo el encoder va a la GPU con más "
                     "memoria libre y el texto se genera en CPU.", text_color="gray55",
                     font=ctk.CTkFont(size=11), justify="left", anchor="w", wraplength=640).grid(
            row=5, column=0, sticky="w", padx=12, pady=(0, 10))

        # ---- Whisper: modelo por defecto ----
        wf = ctk.CTkFrame(wrap); wf.grid(row=5, column=0, sticky="ew", **pad)
        wf.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(wf, text="Whisper", font=ctk.CTkFont(size=14, weight="bold")).grid(
            row=0, column=0, columnspan=2, sticky="w", padx=12, pady=(10, 2))
        ctk.CTkLabel(wf, text="Modelo por defecto").grid(row=1, column=0, sticky="w", padx=12, pady=4)
        self.whisper_model_menu = ctk.CTkOptionMenu(wf, values=core.MODELS, width=180,
                                                    command=self._on_whisper_model)
        self.whisper_model_menu.set(hardware.whisper_model())
        self.whisper_model_menu.grid(row=1, column=1, sticky="w", padx=(10, 12), pady=4)
        ctk.CTkLabel(wf, text="Lo usan Transcribir, Automático y el wizard al arrancar. large-v3-turbo: "
                     "calidad de large a mucha más velocidad (recomendado). Sugerido según el hardware "
                     f"detectado: {hardware.hardware_whisper_suggestion()}.",
                     text_color="gray55", font=ctk.CTkFont(size=11), justify="left",
                     anchor="w", wraplength=640).grid(row=2, column=0, columnspan=2, sticky="w",
                                                      padx=12, pady=(0, 10))

        # ---- memoria ----
        mem = ctk.CTkFrame(wrap); mem.grid(row=6, column=0, sticky="ew", **pad)
        mem.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(mem, text="Memoria", font=ctk.CTkFont(size=14, weight="bold")).grid(
            row=0, column=0, sticky="w", padx=12, pady=(10, 2))
        ctk.CTkButton(mem, text="🧹 Liberar memoria", width=150, command=self._free_memory).grid(
            row=1, column=0, padx=12, pady=(0, 4), sticky="w")
        ctk.CTkLabel(mem, text="Descarga los modelos de IA cargados (libera RAM/VRAM). Se recargan solos "
                     "cuando los vuelvas a usar.", text_color="gray55", font=ctk.CTkFont(size=11),
                     justify="left", anchor="w", wraplength=560).grid(
            row=1, column=1, padx=(4, 12), pady=(0, 8), sticky="w")

        # ---- atajos del editor (keymap.json por máquina; aplica al instante) ----
        from keymap_ui import KeymapSettings
        self.keymap_settings = KeymapSettings(wrap, row=7, padx=6, pady=(0, 12))

        # ---- estado guardado ----
        self.settings_status = ctk.CTkLabel(wrap, text="", text_color="#2fa572",
                                            font=ctk.CTkFont(size=12))
        self.settings_status.grid(row=8, column=0, sticky="w", padx=10, pady=(0, 8))

        # cargar valores actuales + poblar detección
        cfg = hardware.load()
        self._setcb(self.cpu_max, cfg.get("cpu_max", True))
        n = int(cfg.get("cpu_threads") or 0) or n_log
        self.cpu_thr.set(min(max(1, n), n_log))
        self._settings_detect(save=False)

    def _settings_detect(self, save=True):
        """(Re)detecta el hardware SIN congelar la GUI: las sondas lentas (nvidia-smi, vulkaninfo,
        llama --list-devices) corren en un hilo; la actualización de widgets vuelve al hilo principal."""
        if save:                                     # "Redetectar" → invalidar caches para ver cambios
            try:
                describir._DEV_LIST_CACHE = None
            except Exception:
                pass
        self._detect_seq = getattr(self, "_detect_seq", 0) + 1   # guard: solo la última detección aplica
        seq = self._detect_seq
        for w in self._hw_rows.winfo_children():
            w.destroy()
        ctk.CTkLabel(self._hw_rows, text="Detectando hardware…", text_color="gray60",
                     font=ctk.CTkFont(size=12)).grid(row=0, column=0, sticky="w")

        def work():
            # dict COMPLETO por defecto → _settings_apply nunca falla por key faltante (no mata _pump)
            data = {"ram": (0.0, 0.0), "cpu_name": "CPU", "cpu_phys": 0, "cpu_log": 0, "gpus": [], "devs": [],
                    "gpu": {"name": None, "vram_gb": 0.0, "torch_cuda": False, "ct2_cuda": 0}}
            try:
                data["ram"] = hardware.ram_gb()
                data["gpu"] = (hardware.gpu_info() if save else
                               {**hardware._gpu_nvidia_smi(), "torch_cuda": None, "ct2_cuda": None})
                data["gpus"] = hardware.list_gpus()          # UNA sola vez (antes se llamaba 2x)
                data["cpu_name"] = hardware.cpu_name()
                data["cpu_phys"], data["cpu_log"] = hardware.cpu_physical(), hardware.cpu_logical()
                try:
                    data["devs"] = describir.list_devices() if describir.available() else []
                except Exception:
                    data["devs"] = []
            except Exception:
                pass
            self.msgs.put(("settings_apply", data, save, seq))   # marshal al hilo principal (thread-safe)

        threading.Thread(target=work, daemon=True).start()

    def _settings_apply(self, data, save, seq=None):
        """Puebla los widgets de Ajustes con lo detectado (corre en el hilo principal)."""
        if not data or (seq is not None and seq != getattr(self, "_detect_seq", seq)):
            return                                   # detección vieja (Redetect rápido) → ignorar
        tot, avail = data.get("ram", (0.0, 0.0))
        g = data.get("gpu", {"name": None, "vram_gb": 0.0, "torch_cuda": False, "ct2_cuda": 0})
        gpus_all = data.get("gpus", [])
        gpu_txt = g["name"] or "no se detectó GPU NVIDIA"
        if g["name"] and g["vram_gb"]:
            gpu_txt += f"  ·  {g['vram_gb']:g} GB VRAM"
        back = []
        if g["ct2_cuda"]:
            back.append("Whisper✓")
        if g["torch_cuda"]:
            back.append("torch/IA✓")
        if g["torch_cuda"] is None:
            back.append("se comprobarán al procesar o redetectar")
        elif g["name"] and not g["torch_cuda"]:
            back.append("torch en CPU")
        for w in self._hw_rows.winfo_children():
            w.destroy()
        rows = [
            ("Procesador", data["cpu_name"]),
            ("Núcleos / hilos", f"{data['cpu_phys']} núcleos · {data['cpu_log']} hilos"),
            ("Memoria RAM", f"{tot:g} GB totales · {avail:g} GB libres" if tot else "desconocida"),
            ("Tarjeta gráfica", gpu_txt),
            ("Backends GPU", ", ".join(back) if back else "ninguno (todo corre en CPU)"),
        ]
        for i, (k, v) in enumerate(rows):
            ctk.CTkLabel(self._hw_rows, text=k, text_color="gray60",
                         font=ctk.CTkFont(size=12)).grid(row=i, column=0, sticky="w", pady=1)
            ctk.CTkLabel(self._hw_rows, text=v, font=ctk.CTkFont(size=12, weight="bold"),
                         justify="left", anchor="w", wraplength=460).grid(
                row=i, column=1, sticky="w", padx=(16, 0), pady=1)

        # ¿hay GPU para el carril CUDA (whisper+torch) y/o para el carril Vulkan (iGPU incluida)?
        has_cuda = bool(g["ct2_cuda"] or g["torch_cuda"])
        has_vulkan_gpu = any(gg["kind"] in ("integrada", "dedicada") for gg in gpus_all)
        if not has_cuda:
            self.gpu_prefer.deselect(); self.gpu_prefer.configure(state="disabled")
            if has_vulkan_gpu:      # iGPU-only: sin CUDA pero con GPU Vulkan usable por el LALM
                self.gpu_note.configure(text="No hay GPU NVIDIA (CUDA): Whisper y los modelos de IA "
                    "corren en CPU. Pero tu GPU (integrada/Vulkan) SÍ se usa para el modelo de "
                    "descripción — elegila abajo.")
            else:
                self.gpu_note.configure(text="No hay GPU utilizable en este equipo → todo corre en CPU.")
        else:
            self.gpu_prefer.configure(state="normal")
            self._setcb(self.gpu_prefer, hardware.load().get("gpu_prefer", True))
            if hardware.use_gpu_torch():
                note = ("Con esto marcado, Whisper y todos los modelos de IA (alineación, emoción, risa, "
                        "escenas, respiraciones) corren en la GPU.")
            elif os.name == "nt" and g["torch_cuda"]:
                note = ("Whisper corre en GPU. Los modelos de IA de las otras pestañas corren en CPU: en "
                        "Windows torch-GPU choca con las libs cuDNN de Whisper y crashea, así que van por "
                        "CPU (rápido y estable, igual que en Linux).")
            else:
                note = ("Con esto marcado, cada módulo usa la GPU si puede. En este equipo torch está en "
                        "versión +cpu, así que solo Whisper corre en GPU; los modelos de IA de las otras "
                        "pestañas van por CPU. En una PC con torch+CUDA, todos usan la GPU.")
            self.gpu_note.configure(text=note + "  Los modelos ya cargados cambian de dispositivo al reiniciar la app.")

        # ---- lista de todas las GPUs (dedicada + integrada) ----
        for w in self._gpu_list.winfo_children():
            w.destroy()
        gpus = [gg for gg in gpus_all if gg["kind"] != "cpu"]
        if not gpus and g["name"]:   # fallback si no hay vulkaninfo: al menos la NVIDIA
            gpus = [{"name": g["name"], "kind": "dedicada",
                     "cuda": bool(g["ct2_cuda"] or g["torch_cuda"])}]
        if gpus:
            for i, gg in enumerate(gpus):
                kind = ("integrada (iGPU)" if gg["kind"] == "integrada"
                        else "dedicada" if gg["kind"] == "dedicada" else gg["kind"])
                caps = (["CUDA"] if gg.get("cuda") else []) + ["Vulkan"]
                ctk.CTkLabel(self._gpu_list, text=f"•  {gg['name']}   —   {kind}   ·   {'/'.join(caps)}",
                             font=ctk.CTkFont(size=12), justify="left", anchor="w",
                             wraplength=620).grid(row=i, column=0, sticky="w", pady=1)
        else:
            ctk.CTkLabel(self._gpu_list, text="(no se pudieron enumerar GPUs — instalá 'vulkaninfo' para verlas)",
                         text_color="gray55", font=ctk.CTkFont(size=11)).grid(row=0, column=0, sticky="w")

        # ---- selector de GPU del LALM (Vulkan) — describir.list_devices() es la fuente de verdad ----
        devs = data.get("devs") or []
        if devs:
            self.llama_row.grid()
            kind_by_name = {gg["name"]: gg["kind"] for gg in gpus_all}
            options = ["Automática (recomendada)"]
            self._llama_dev_map = {"Automática (recomendada)": "auto"}
            for d in devs:
                k = kind_by_name.get(d["name"], "")
                ktxt = " (iGPU)" if k == "integrada" else " (dedicada)" if k == "dedicada" else ""
                label = f"{d['name']}{ktxt} · {d['total_mib'] / 1024:.0f} GB"
                options.append(label)
                self._llama_dev_map[label] = d["id"]
            options.append("CPU (sin GPU)")
            self._llama_dev_map["CPU (sin GPU)"] = "cpu"
            self.llama_menu.configure(values=options)
            cur = hardware.llama_device()
            self.llama_menu.set(next((lbl for lbl, v in self._llama_dev_map.items() if v == cur),
                                     options[0]))
        else:
            self.llama_row.grid_remove()

        self._on_cpu_max()
        if save:
            self._settings_flash("Hardware redetectado.")

    def _on_cpu_max(self):
        """Muestra/oculta el slider de hilos según el checkbox de 'usar todos'."""
        use_max = bool(self.cpu_max.get())
        if use_max:
            self.cpu_thr_frame.grid_remove()
        else:
            self.cpu_thr_frame.grid()
        self._refresh_cpu_thr_lbl()
        hardware.set_(cpu_max=use_max, cpu_threads=int(self.cpu_thr.get()))
        self._settings_flash()

    def _on_cpu_thr(self, _v=None):
        self._refresh_cpu_thr_lbl()
        hardware.set_(cpu_threads=int(self.cpu_thr.get()))
        self._settings_flash()

    def _refresh_cpu_thr_lbl(self):
        if bool(self.cpu_max.get()):
            self.cpu_thr_lbl.configure(text=f"Hilos: todos ({hardware.cpu_logical()})")
        else:
            self.cpu_thr_lbl.configure(text=f"Hilos a usar: {int(self.cpu_thr.get())} de {hardware.cpu_logical()}")

    def _on_gpu_prefer(self):
        hardware.set_(gpu_prefer=bool(self.gpu_prefer.get()))
        self._settings_flash()

    def _on_llama_dev(self, label):
        hardware.set_(llama_device=self._llama_dev_map.get(label, "auto"))
        self._settings_flash()

    def _on_whisper_model(self, value):
        hardware.set_(whisper_model=value)
        # los selectores de las demás vistas siguen al default (sin tocar una corrida en curso)
        try:
            if not (self.worker and self.worker.is_alive()):
                self.model_menu.set(value)
            self.automatico.set_default_model(value)
        except Exception:
            pass
        self._settings_flash(f"Modelo por defecto: {value} ✓")

    def _free_memory(self):
        if jobs.busy():                              # no descargar modelos en medio de un job
            self._settings_flash("Hay un proceso pesado en curso — esperá a que termine.")
            return
        models.unload_all()
        self._settings_flash("🧹 Memoria liberada (modelos descargados).")

    def _settings_flash(self, msg="Guardado ✓"):
        self.settings_status.configure(text=msg)
        self.after(2500, lambda: self.settings_status.configure(text=""))

    # ============================================================ TAB 1: TX --
    def _build_transcribe(self, p):
        left = ctk.CTkFrame(p, fg_color="transparent")
        right = ctk.CTkFrame(p, fg_color="transparent")
        self._tabrefs["tx"] = (p, left, right)
        left.grid_columnconfigure(0, weight=1)
        right.grid_columnconfigure(0, weight=1)
        pad = {"padx": 4, "pady": (0, 10)}

        self.tx_preset = self._preset_bar(left, 0, "transcribe", self._tx_get_cfg, self._tx_set_cfg)

        dev = ctk.CTkFrame(left); dev.grid(row=1, column=0, sticky="ew", **pad)
        dev.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(dev, text="Dispositivo").grid(row=0, column=0, padx=12, pady=10, sticky="w")
        opts = ["GPU", "CPU"] if self.has_gpu else ["CPU"]
        self.dev_seg = ctk.CTkSegmentedButton(dev, values=opts, command=self._on_dev)
        self.dev_seg.set("GPU" if (self.has_gpu and hardware.gpu_prefer()) else "CPU")
        self.dev_seg.grid(row=0, column=1, padx=8, pady=10, sticky="e")
        self.dev_status = ctk.CTkLabel(dev, text="", text_color="gray60", font=ctk.CTkFont(size=12))
        self.dev_status.grid(row=1, column=0, columnspan=2, padx=12, pady=(0, 10), sticky="w")
        self._on_dev(self.dev_seg.get())

        af = ctk.CTkFrame(left); af.grid(row=2, column=0, sticky="ew", **pad)
        af.grid_columnconfigure(0, weight=1)
        self.audio_entry = ctk.CTkEntry(af, placeholder_text="Ningún audio seleccionado…")
        self.audio_entry.grid(row=0, column=0, padx=(12, 8), pady=12, sticky="ew")
        ctk.CTkButton(af, text="Importar audio", width=130, command=self._pick_audio).grid(
            row=0, column=1, padx=(0, 12), pady=12)

        mf = ctk.CTkFrame(left); mf.grid(row=3, column=0, sticky="ew", **pad)
        mf.grid_columnconfigure((1, 3), weight=1)
        ctk.CTkLabel(mf, text="Modelo").grid(row=0, column=0, padx=(12, 6), pady=12, sticky="w")
        self.model_menu = ctk.CTkOptionMenu(mf, values=core.MODELS)
        self.model_menu.set(hardware.recommend_whisper_model())   # default acorde al hardware
        self.model_menu.grid(row=0, column=1, padx=6, pady=12, sticky="ew")
        ctk.CTkLabel(mf, text="Idioma").grid(row=0, column=2, padx=(12, 6), pady=12, sticky="w")
        self.lang_entry = ctk.CTkEntry(mf, width=70); self.lang_entry.insert(0, "es")
        self.lang_entry.grid(row=0, column=3, padx=(6, 12), pady=12, sticky="w")

        of = ctk.CTkFrame(left); of.grid(row=4, column=0, sticky="ew", **pad)
        ctk.CTkLabel(of, text="Salidas").grid(row=0, column=0, padx=12, pady=(10, 4), sticky="w")
        ctk.CTkLabel(of, text="words.json siempre se genera (base para sync y limpieza)",
                     text_color="gray55", font=ctk.CTkFont(size=11)).grid(
            row=0, column=1, columnspan=3, padx=6, pady=(10, 4), sticky="w")
        self.cb_seg = ctk.CTkCheckBox(of, text="segments.json"); self.cb_seg.select()
        self.cb_srt = ctk.CTkCheckBox(of, text="subtítulos .srt"); self.cb_srt.select()
        self.cb_cue = ctk.CTkCheckBox(of, text="cues.md"); self.cb_cue.select()
        self.cb_seg.grid(row=1, column=0, padx=12, pady=(0, 12), sticky="w")
        self.cb_srt.grid(row=1, column=1, padx=6, pady=(0, 12), sticky="w")
        self.cb_cue.grid(row=1, column=2, padx=6, pady=(0, 12), sticky="w")

        # ---- alineación forzada: 2º modelo que ajusta los timestamps (recuadro propio, destacado) ----
        alf = ctk.CTkFrame(left, border_width=2, border_color="#2fa572")
        alf.grid(row=5, column=0, sticky="ew", **pad)
        alf.grid_columnconfigure(0, weight=1)
        self.cb_align = ctk.CTkCheckBox(alf, text="  Ajustar timestamps con IA de alineación  (recomendado)",
                                        font=ctk.CTkFont(size=13, weight="bold"))
        self.cb_align.select()
        self.cb_align.grid(row=0, column=0, padx=12, pady=(10, 2), sticky="w")
        self.cb_align_hint = ctk.CTkLabel(
            alf, text="Al terminar la transcripción, un 2º modelo (MMS) re-alinea las palabras al audio → "
            "tiempos precisos en TODAS las salidas (.json/.srt/.md). Mejor para limpiar respiraciones y "
            "para sincronizar. Corre en CPU (~2-3 min en audios largos; la 1ª vez descarga el modelo).",
            text_color="gray60", font=ctk.CTkFont(size=11), justify="left", anchor="w", wraplength=600)
        self.cb_align_hint.grid(row=1, column=0, padx=(36, 12), pady=(0, 10), sticky="ew")
        if not align.available():
            self.cb_align.deselect(); self.cb_align.configure(state="disabled")
            self.cb_align_hint.configure(text="(alineación no disponible: falta torchaudio con MMS)")

        df = ctk.CTkFrame(left); df.grid(row=6, column=0, sticky="ew", **pad)
        df.grid_columnconfigure(0, weight=1)
        self.out_entry = ctk.CTkEntry(df, placeholder_text="Carpeta de destino (por defecto: la del audio)")
        self.out_entry.grid(row=0, column=0, padx=(12, 8), pady=12, sticky="ew")
        ctk.CTkButton(df, text="Elegir carpeta", width=130, command=self._pick_out).grid(
            row=0, column=1, padx=(0, 12), pady=12)

        self.run_btn = ctk.CTkButton(left, text="Convertir", height=42,
                                     font=ctk.CTkFont(size=16, weight="bold"), command=self._start)
        self.run_btn.grid(row=7, column=0, sticky="ew", padx=4, pady=(4, 8))

        # ---- RIGHT: monitoreo ----
        self.progress = ctk.CTkProgressBar(right); self.progress.set(0)
        self.progress.grid(row=0, column=0, sticky="ew", padx=4, pady=(2, 4))
        self.eta_label = ctk.CTkLabel(right, text="", text_color="gray60", font=ctk.CTkFont(size=12))
        self.eta_label.grid(row=1, column=0, sticky="w", padx=6, pady=(0, 6))
        self.log = ctk.CTkTextbox(right, height=150, font=ctk.CTkFont(size=12))
        self.log.grid(row=2, column=0, sticky="nsew", padx=4, pady=(0, 4))
        right.grid_rowconfigure(2, weight=1)
        self.log.configure(state="disabled")

    def _on_dev(self, choice):
        if choice == "GPU":
            self.dev_status.configure(text=f"✓ GPU · {self.gpu_label}  (int8_float16, cae a CPU si falta VRAM)")
        else:
            extra = "" if self.has_gpu else "  (no se detectó GPU NVIDIA)"
            self.dev_status.configure(text=f"CPU  (int8){extra}")

    def _pick_audio(self):
        f = dialogs.open_file("Elegí un audio", AUDIO_FILTERS, remember="tx_audio")
        if f:
            self.audio_entry.delete(0, "end"); self.audio_entry.insert(0, f)
            if not self.out_entry.get().strip():
                self.out_entry.insert(0, str(Path(f).parent))

    def _pick_out(self):
        d = dialogs.open_dir("Carpeta de destino", remember="tx_out")
        if d:
            self.out_entry.delete(0, "end"); self.out_entry.insert(0, d)

    def _tx_get_cfg(self):
        return {"model": self.model_menu.get(), "lang": self.lang_entry.get().strip(),
                "device": self.dev_seg.get(), "segments": bool(self.cb_seg.get()),
                "srt": bool(self.cb_srt.get()), "cues": bool(self.cb_cue.get()),
                "align": bool(self.cb_align.get())}

    def _tx_set_cfg(self, c):
        if c.get("model") in core.MODELS:
            self.model_menu.set(c["model"])
        if "lang" in c:
            self.lang_entry.delete(0, "end"); self.lang_entry.insert(0, c["lang"])
        dev = c.get("device")
        if dev in (["GPU", "CPU"] if self.has_gpu else ["CPU"]):
            self.dev_seg.set(dev); self._on_dev(dev)
        self._setcb(self.cb_seg, c.get("segments", True))
        self._setcb(self.cb_srt, c.get("srt", True))
        self._setcb(self.cb_cue, c.get("cues", True))
        if "align" in c and align.available():
            self._setcb(self.cb_align, c["align"])

    def _start(self):
        if self.worker and self.worker.is_alive():
            self.cancel.set()
            self.run_btn.configure(text="Cancelando…", state="disabled")
            return
        path = self.audio_entry.get().strip()
        if not path or not Path(path).exists():
            self._txlog("⚠ Elegí un archivo de audio válido primero."); return
        out = self.out_entry.get().strip() or str(Path(path).parent)
        args = dict(
            audio=path, outdir=out, model_name=self.model_menu.get(),
            lang=self.lang_entry.get().strip() or "es",
            device="cuda" if self.dev_seg.get() == "GPU" else "cpu",
            want_segments=bool(self.cb_seg.get()), want_srt=bool(self.cb_srt.get()),
            want_cues=bool(self.cb_cue.get()), want_align=bool(self.cb_align.get()))
        self.cancel.clear(); self.progress.set(0)
        self.eta_label.configure(text="Preparando…")
        self.run_btn.configure(text="Cancelar")
        self._txlog("─" * 44)
        self.worker = threading.Thread(target=self._work_tx, kwargs=args, daemon=True)
        self.worker.start()

    def _work_tx(self, **kw):
        t0 = time.time()
        try:
            log = lambda m: self.msgs.put(("tx_log", m))
            with jobs.heavy(log):                       # serializa jobs pesados (no sobre-suscribir)
                models.free_if_tight(log=log)
                res = core.transcribe(
                    kw.pop("audio"), kw.pop("outdir"),
                    progress_cb=lambda f, eta: self.msgs.put(("tx_progress", f, eta)),
                    log_cb=log, cancel=self.cancel, **kw)
            self.msgs.put(("tx_done", res, time.time() - t0))
        except Exception as e:
            self.msgs.put(("tx_error", str(e)))

    # ============================================================ TAB 2: GATE --
    def _build_gate(self, p):
        left = ctk.CTkFrame(p, fg_color="transparent")
        right = ctk.CTkFrame(p, fg_color="transparent")
        self._tabrefs["gate"] = (p, left, right)
        left.grid_columnconfigure(0, weight=1)
        right.grid_columnconfigure(0, weight=1)
        pad = {"padx": 4, "pady": (0, 10)}

        self.gate_preset = self._preset_bar(left, 0, "gate", self._gate_get_cfg, self._gate_set_cfg)

        gaf = ctk.CTkFrame(left); gaf.grid(row=1, column=0, sticky="ew", **pad)
        gaf.grid_columnconfigure(0, weight=1)
        self.g_audio = ctk.CTkEntry(gaf, placeholder_text="Audio a limpiar (wav/flac/mp3…)")
        self.g_audio.grid(row=0, column=0, padx=(12, 8), pady=12, sticky="ew")
        ctk.CTkButton(gaf, text="Importar audio", width=130, command=self._g_pick_audio).grid(
            row=0, column=1, padx=(0, 12), pady=12)

        wf = ctk.CTkFrame(left); wf.grid(row=2, column=0, sticky="ew", **pad)
        wf.grid_columnconfigure(0, weight=1)
        self.g_words = ctk.CTkEntry(wf, placeholder_text="words.json (se busca automáticamente)")
        self.g_words.grid(row=0, column=0, padx=(12, 8), pady=(12, 2), sticky="ew")
        ctk.CTkButton(wf, text="Elegir words.json", width=130, command=self._g_pick_words).grid(
            row=0, column=1, padx=(0, 12), pady=(12, 2))
        self.g_words_status = ctk.CTkLabel(wf, text="", text_color="gray60", font=ctk.CTkFont(size=11))
        self.g_words_status.grid(row=1, column=0, columnspan=2, padx=12, pady=(0, 10), sticky="w")

        # ---- selector de modo: Avanzado (heurístico, 7 ajustes) · VAD · Quirúrgico (respiraciones) ----
        vf = ctk.CTkFrame(left); vf.grid(row=3, column=0, sticky="ew", **pad)
        vf.grid_columnconfigure(0, weight=1)
        modes = [GM_ADV]
        if gate.vad_available():
            modes.append(GM_VAD)
        if detect_breaths.available():
            modes.append(GM_SURG)
        self.g_mode = ctk.CTkSegmentedButton(vf, values=modes, command=lambda _=None: self._on_mode_change())
        self.g_mode.set(GM_ADV)
        self.g_mode.grid(row=0, column=0, padx=12, pady=(10, 2), sticky="ew")
        self.g_vad_note = ctk.CTkLabel(vf, text="", text_color="gray55", font=ctk.CTkFont(size=11),
                                       justify="left", anchor="w", wraplength=560)
        self.g_vad_note.grid(row=1, column=0, padx=12, pady=(0, 6), sticky="ew")
        # escudo de palabras (solo modo quirúrgico): on = protege consonantes; off = mutea todo
        self.g_shield = ctk.CTkSwitch(vf, text="Escudo de palabras (protege consonantes) · off = mutea toda respiración",
                                      command=self._on_mode_change)
        self.g_shield.select()
        self.g_shield.grid(row=2, column=0, padx=12, pady=(0, 4), sticky="w")
        # red de seguridad: caza aire (broadband) en las pausas que Respiro no detecta. APAGADA por defecto.
        self.g_safety = ctk.CTkSwitch(vf, text="Red de seguridad (aire en pausas) · caza soplidos que la red no ve",
                                      command=self._on_mode_change)
        self.g_safety.grid(row=3, column=0, padx=12, pady=(0, 10), sticky="w")

        psf = ctk.CTkScrollableFrame(left, height=160, label_text="Ajustes (calibrá con el oído)")
        psf.grid(row=4, column=0, sticky="nsew", **pad)
        left.grid_rowconfigure(4, weight=1)
        psf.grid_columnconfigure(1, weight=1)
        self.g_pre = self._slider(psf, 0, "Margen antes (pre)", 0.0, 0.30, 0.08,
                                  "conservar antes de cada palabra (chico = come consonantes f/s)")
        self.g_post = self._slider(psf, 2, "Margen después (post)", 0.0, 0.40, 0.22,
                                   "conservar después (colas de s, n finales)")
        self.g_mingap = self._slider(psf, 4, "Pausa 'larga' (min-gap)", 0.10, 1.20, 0.40,
                                     "huecos ≥ esto = respiración (largo); menores = corto")
        self.g_attack = self._slider(psf, 6, "Attack (vuelve la voz)", 0.005, 0.10, 0.02,
                                     "subida rápida justo antes del habla", step=0.005)
        self.g_release = self._slider(psf, 8, "Release (cola de palabra)", 0.05, 0.60, 0.25,
                                      "qué tan RÁPIDO baja el volumen · corto (0.08) = respiros cortos "
                                      "desaparecen más · largo = colas de palabra más naturales")
        self.g_floor = self._slider(psf, 10, "Nivel huecos largos (floor)", -60, 0, -26,
                                    "-26 = ducking (audio vivo) · -60 ≈ silencio total", unit="dB", step=1)
        self.g_short = self._slider(psf, 12, "Nivel huecos cortos (short)", -30, 0, 0,
                                   "respiración pegada entre frases: 0 = no tocar · probá -8 a -12",
                                   unit="dB", step=1)
        self.g_threshold = self._slider(psf, 14, "Sensibilidad Respiro (threshold)", 0.01, 0.90, 0.064,
                                        "solo modo quirúrgico · más BAJO = detecta más respiraciones · "
                                        "0.064 = default · subí a 0.3-0.5 si agarra eses/jotas",
                                        unit="", step=0.002)
        self.g_shrink = self._slider(psf, 16, "Encoger bordes whisper", 0, 150, 60,
                                     "solo con escudo ON · recorta cada palabra hacia adentro antes de "
                                     "comparar → libera respiraciones que whisper 'estiró' sobre el aire",
                                     unit="ms", step=5)
        self.g_minlen = self._slider(psf, 18, "Duración mínima respiración", 50, 400, 200,
                                     "solo quirúrgico · las respiraciones más cortas que esto se ignoran "
                                     "(filtro anti-falsos-positivos) · bajalo para cazar inhalaciones rápidas",
                                     unit="ms", step=10)
        self.g_safety_thr = self._slider(psf, 20, "Sensibilidad aire (red de seguridad)", 0.02, 0.10, 0.04,
                                         "solo con red de seguridad ON · más BAJO = caza aire más sutil · "
                                         "subilo si llegara a tocar algún resto de voz",
                                         unit="", step=0.005)

        gof = ctk.CTkFrame(left); gof.grid(row=5, column=0, sticky="ew", **pad)
        gof.grid_columnconfigure(0, weight=1)
        self.g_out = ctk.CTkEntry(gof, placeholder_text="Salida (por defecto: <nombre>_gated.wav)")
        self.g_out.grid(row=0, column=0, padx=(12, 8), pady=12, sticky="ew")
        ctk.CTkButton(gof, text="Elegir salida", width=130, command=self._g_pick_out).grid(
            row=0, column=1, padx=(0, 12), pady=12)

        bf = ctk.CTkFrame(left, fg_color="transparent"); bf.grid(row=6, column=0, sticky="ew", padx=4, pady=(4, 6))
        bf.grid_columnconfigure((0, 1), weight=1)
        ctk.CTkButton(bf, text="Analizar huecos", height=42, fg_color="gray30", hover_color="gray25",
                      command=self._g_analyze).grid(row=0, column=0, sticky="ew", padx=(0, 6))
        self.g_btn = ctk.CTkButton(bf, text="Limpiar audio", height=42,
                                   font=ctk.CTkFont(size=15, weight="bold"), command=self._g_start)
        self.g_btn.grid(row=0, column=1, sticky="ew", padx=(6, 0))

        # ---- RIGHT: log ----
        self.g_status = ctk.CTkLabel(right, text="", text_color="gray60", font=ctk.CTkFont(size=12))
        self.g_status.grid(row=0, column=0, sticky="w", padx=6, pady=(2, 6))
        self.g_log = ctk.CTkTextbox(right, height=150, font=ctk.CTkFont(size=12))
        self.g_log.grid(row=1, column=0, sticky="nsew", padx=4, pady=(0, 4))
        right.grid_rowconfigure(1, weight=1)
        self.g_log.configure(state="disabled")

        self._on_mode_change()   # estado inicial (avanzado: todos los sliders visibles)

    def _current_mode(self) -> str:
        return self.g_mode.get() or GM_ADV

    def _on_mode_change(self):
        mode = self._current_mode()
        # Avanzado usa las 7 perillas; VAD y Quirúrgico tienen fronteras exactas → solo floor + release
        heuristic = mode == GM_ADV
        for s in (self.g_pre, self.g_post, self.g_mingap, self.g_short, self.g_attack):
            self._show_slider(s, heuristic)
        surg = mode == GM_SURG
        self._show_slider(self.g_threshold, surg)              # threshold solo aplica a Respiro
        self._show_slider(self.g_minlen, surg)
        self.g_shield.grid() if surg else self.g_shield.grid_remove()
        self.g_safety.grid() if surg else self.g_safety.grid_remove()
        # el encogido solo tiene sentido con el escudo activo; la sensibilidad, con la red ON
        self._show_slider(self.g_shrink, surg and bool(self.g_shield.get()))
        self._show_slider(self.g_safety_thr, surg and bool(self.g_safety.get()))
        if mode == GM_VAD:
            self.g_vad_note.configure(text="VAD: Silero encuentra la voz acústicamente y la cruza con las "
                "palabras de whisper → gatea todo lo que no es voz (respiraciones + ruiditos: sillas, clicks, "
                "papeles). Solo floor y release.")
        elif mode == GM_SURG:
            if bool(self.g_shield.get()):
                self.g_vad_note.configure(text="Quirúrgico · escudo ON: Respiro-en detecta respiraciones y "
                    "descarta/recorta las que solapan una palabra de whisper (protege consonantes). Si se "
                    "cuela un respiro pegado a una palabra, subí «Encoger bordes».")
            else:
                self.g_vad_note.configure(text="Quirúrgico · escudo OFF: se atenúa TODA respiración que "
                    "detecta Respiro-en, whisper queda fuera del loop. Máxima limpieza; si toca alguna "
                    "consonante, subí el threshold.")
        else:
            self.g_vad_note.configure(text="Avanzado: bordes por palabra + heurísticas "
                "(pre/post/min-gap/short). Más control, pero hay que afinarlo con el oído.")

    def _g_pick_audio(self):
        f = dialogs.open_file("Audio a limpiar", GATE_FILTERS, remember="g_audio")
        if not f:
            return
        self.g_audio.delete(0, "end"); self.g_audio.insert(0, f)
        self.g_out.delete(0, "end"); self.g_out.insert(0, str(gate.default_output(f)))
        found = gate.find_words_json(f)
        if found:
            self.g_words.delete(0, "end"); self.g_words.insert(0, str(found))
            self.g_words_status.configure(text=f"✓ words.json encontrado automáticamente: {found.name}")
        else:
            self.g_words_status.configure(
                text="⚠ no encontré <nombre>.words.json junto al audio — elegilo a mano o transcribí primero")

    def _g_pick_words(self):
        a = self.g_audio.get().strip()
        start = str(Path(a).parent) if a and Path(a).exists() else None
        f = dialogs.open_file("words.json", JSON_FILTERS, start=start, remember="g_words")
        if f:
            self.g_words.delete(0, "end"); self.g_words.insert(0, f)
            self.g_words_status.configure(text="words.json elegido a mano.")

    def _g_pick_out(self):
        default = self.g_out.get().strip()
        if not default:
            a = self.g_audio.get().strip()
            default = str(gate.default_output(a)) if a else None
        f = dialogs.save_file("Guardar audio limpio", default=default, filters=WAV_FILTERS, remember="g_out")
        if f:
            self.g_out.delete(0, "end"); self.g_out.insert(0, f)

    def _g_params(self):
        return dict(pre=round(self.g_pre.get(), 3), post=round(self.g_post.get(), 3),
                    min_gap=round(self.g_mingap.get(), 3), attack=round(self.g_attack.get(), 3),
                    release=round(self.g_release.get(), 3),
                    floor_db=round(self.g_floor.get()), short_floor_db=round(self.g_short.get()))

    def _mode_kwargs(self, mode, audio, log, threshold=0.064, min_length=20):
        """Traduce el modo elegido a los kwargs de gate.process/analyze. En modo
        quirúrgico corre Respiro-en (en el hilo worker) y escribe <audio>.breaths.json.
        `threshold`/`min_length` se leen en el hilo principal y se pasan acá (Tk no es
        thread-safe). `min_length` está en frames de 10 ms."""
        if mode == GM_VAD:
            return {"use_vad": True}
        if mode == GM_SURG:
            log(f"Detectando respiraciones (threshold={threshold:.3f}, mín {min_length*10}ms)…")
            breaths = detect_breaths.detect(audio, threshold=threshold, min_length=min_length, log_cb=log)
            bp = Path(audio).with_suffix(".breaths.json")
            bp.write_text(json.dumps(breaths, ensure_ascii=False, indent=2), encoding="utf-8")
            log(f"{len(breaths)} respiración(es) candidatas → {bp.name}")
            return {"breaths_json": str(bp)}
        return {}

    def _gate_get_cfg(self):
        return {**self._g_params(), "mode": self._current_mode(),
                "threshold": round(self.g_threshold.get(), 3),
                "min_length": int(round(self.g_minlen.get() / 10)),
                "use_shield": bool(self.g_shield.get()),
                "word_shrink": round(self.g_shrink.get() / 1000.0, 3),
                "spectral_net": bool(self.g_safety.get()),
                "flatness_thr": round(self.g_safety_thr.get(), 3)}

    def _gate_set_cfg(self, c):
        mode = c.get("mode")
        if mode is None and "use_vad" in c:            # back-compat: presets viejos (bool)
            mode = GM_VAD if c["use_vad"] else GM_ADV
        if "use_shield" in c:
            self._setcb(self.g_shield, c["use_shield"])
        if "spectral_net" in c:
            self._setcb(self.g_safety, c["spectral_net"])
        if "flatness_thr" in c:
            self._set_slider(self.g_safety_thr, c["flatness_thr"])
        if "word_shrink" in c:                         # guardado en segundos → slider en ms
            self._set_slider(self.g_shrink, round(c["word_shrink"] * 1000))
        if "min_length" in c:                          # guardado en frames → slider en ms
            self._set_slider(self.g_minlen, round(c["min_length"] * 10))
        if mode in self.g_mode.cget("values"):
            self.g_mode.set(mode)
        self._on_mode_change()                         # refresca visibilidad (modo + escudo)
        for key, sl in (("pre", self.g_pre), ("post", self.g_post), ("min_gap", self.g_mingap),
                        ("attack", self.g_attack), ("release", self.g_release),
                        ("floor_db", self.g_floor), ("short_floor_db", self.g_short),
                        ("threshold", self.g_threshold)):
            if key in c:
                self._set_slider(sl, c[key])

    def _g_check(self):
        audio = self.g_audio.get().strip(); words = self.g_words.get().strip()
        if not audio or not Path(audio).exists():
            self._glog("⚠ Elegí un audio válido."); return None
        if not words or not Path(words).exists():
            self._glog("⚠ Falta el words.json (transcribí el audio primero en la otra pestaña)."); return None
        return audio, words

    def _g_analyze(self):
        if self.gate_worker and self.gate_worker.is_alive():
            return
        chk = self._g_check()
        if not chk:
            return
        audio, words = chk
        prm = self._g_params()
        prm["use_shield"] = bool(self.g_shield.get())
        prm["word_shrink"] = round(self.g_shrink.get() / 1000.0, 3)
        prm["spectral_net"] = bool(self.g_safety.get())
        prm["flatness_thr"] = round(self.g_safety_thr.get(), 3)
        mode = self._current_mode()
        threshold = round(self.g_threshold.get(), 3)
        min_length = int(round(self.g_minlen.get() / 10))
        self.g_status.configure(text="Analizando…")

        def work():
            try:
                log = lambda m: self.msgs.put(("g_log", m))
                with jobs.heavy(log):              # serializa: en quirúrgico corre Respiro (pesado)
                    models.free_if_tight(log=log)
                    prm.update(self._mode_kwargs(mode, audio, log, threshold, min_length))
                    gaps, total = gate.analyze(audio, words, **prm)
                self.msgs.put(("g_analyzed", gaps, total))
            except Exception as e:
                self.msgs.put(("g_error", str(e)))
        self.gate_worker = threading.Thread(target=work, daemon=True)
        self.gate_worker.start()

    def _gate_analyzed(self, gaps, total):
        touched = [g for g in gaps if g["gain_db"] < -0.1]
        self.g_status.configure(text=f"Análisis: {len(gaps)} huecos · {len(touched)} se tocarían")
        self._glog("─" * 44)
        self._glog(f"Análisis · {len(gaps)} huecos, {len(touched)} se tocarían (audio {total:.1f}s)"
                   "   [♪ = con sonido audible]:")
        for g in gaps:
            mark = "▸" if g["gain_db"] < -0.1 else " "
            aud = "♪" if g.get("audible") else " "
            self._glog(f" {mark}{aud}[{g['start']:6.2f}→{g['end']:6.2f}] {g['dur']:.2f}s {g['tipo']:5s} "
                       f"{g['gain_db']:+.0f}dB  …{g['despues_de']} | {g['antes_de']}…")

    def _g_start(self):
        if self.gate_worker and self.gate_worker.is_alive():
            return
        chk = self._g_check()
        if not chk:
            return
        audio, words = chk
        out = self.g_out.get().strip() or str(gate.default_output(audio))
        prm = self._g_params()
        prm["use_shield"] = bool(self.g_shield.get())
        prm["word_shrink"] = round(self.g_shrink.get() / 1000.0, 3)
        prm["spectral_net"] = bool(self.g_safety.get())
        prm["flatness_thr"] = round(self.g_safety_thr.get(), 3)
        mode = self._current_mode()
        threshold = round(self.g_threshold.get(), 3)
        min_length = int(round(self.g_minlen.get() / 10))
        self.g_btn.configure(text="Procesando…", state="disabled")
        self.g_status.configure(text="Procesando…")
        self._glog("─" * 44)
        if mode == GM_VAD:
            self._glog(f"modo VAD · floor={prm['floor_db']}dB · release={prm['release']}")
        elif mode == GM_SURG:
            escudo = f"escudo ON (encoge {int(prm['word_shrink']*1000)}ms)" if prm["use_shield"] else "escudo OFF"
            red = f" · red de seguridad ON (sens {prm['flatness_thr']:.3f})" if prm["spectral_net"] else ""
            self._glog(f"modo quirúrgico (respiraciones) · floor={prm['floor_db']}dB · "
                       f"release={prm['release']} · threshold={threshold:.3f} · mín {min_length*10}ms · {escudo}{red}")
        else:
            self._glog(f"modo avanzado · floor={prm['floor_db']}dB release={prm['release']} "
                       f"pre={prm['pre']} post={prm['post']} min-gap={prm['min_gap']} short={prm['short_floor_db']}dB")
        self.gate_worker = threading.Thread(
            target=self._work_gate,
            kwargs=dict(audio=audio, words_json=words, out=out, mode=mode, threshold=threshold,
                        min_length=min_length, **prm),
            daemon=True)
        self.gate_worker.start()

    def _work_gate(self, *, audio, words_json, out, mode, threshold=0.064, min_length=20, **kw):
        try:
            log = lambda m: self.msgs.put(("g_log", m))
            with jobs.heavy(log):
                models.free_if_tight(log=log)
                kw.update(self._mode_kwargs(mode, audio, log, threshold, min_length))
                rep = gate.process(audio, words_json, out, log_cb=log, **kw)
            self.msgs.put(("g_done", rep))
        except Exception as e:
            self.msgs.put(("g_error", str(e)))

    # ============================================================ TAB 3: META --
    # (Metadata / Audio de fondo / Cara / Master viven ahora en wizard_extraer.py)

    # =================================================================== pump --
    def _txlog(self, t): self._append(self.log, t)
    def _glog(self, t): self._append(self.g_log, t)

    @staticmethod
    def _append(box, t):
        box.configure(state="normal"); box.insert("end", t + "\n"); box.see("end")
        box.configure(state="disabled")

    def _pump(self):
        try:
            while True:
                m = self.msgs.get_nowait()
                k = m[0]
                if k == "tx_log":
                    self._txlog(m[1])
                elif k == "tx_progress":
                    frac, eta = m[1], m[2]; self.progress.set(frac); pct = int(frac * 100)
                    self.eta_label.configure(
                        text=f"{pct}% · estimando…" if eta is None else f"{pct}% · faltan ~{self._eta(eta)}")
                elif k == "tx_done":
                    self._tx_done(m[1], m[2])
                elif k == "tx_error":
                    self.run_btn.configure(text="Convertir", state="normal"); self.progress.set(0)
                    self.eta_label.configure(text="Error."); self._txlog(f"✗ {m[1]}")
                elif k == "g_log":
                    self._glog(m[1])
                elif k == "g_analyzed":
                    self._gate_analyzed(m[1], m[2])
                elif k == "g_done":
                    self._gate_done(m[1])
                elif k == "g_error":
                    self.g_btn.configure(text="Limpiar audio", state="normal")
                    self.g_status.configure(text="Error."); self._glog(f"✗ {m[1]}")
                elif k == "settings_apply":
                    self._settings_apply(m[1], m[2], m[3] if len(m) > 3 else None)
                elif k == "update_status":
                    self.update_status_label.configure(text=m[1])
                    self.update_button.configure(state="normal", text="Buscar actualización")
        except queue.Empty:
            pass
        self.after(80, self._pump)

    def _tx_done(self, res, took):
        self.run_btn.configure(text="Convertir", state="normal")
        if res is None:
            self.eta_label.configure(text="Cancelado."); self.progress.set(0); return
        self.progress.set(1)
        self.eta_label.configure(text=f"✓ Listo en {self._eta(took)} · {res['device'].upper()}")
        self._txlog(f"\n✓ {res['n_words']} palabras · {res['n_segments']} frases · "
                    f"{res['language']} · {res['device'].upper()}")
        self._txlog(f"Guardado en: {Path(list(res['written'].values())[0]).parent}")
        for pth in res["written"].values():
            self._txlog(f"   • {Path(pth).name}")
        if res["low_conf"]:
            self._txlog(f"⚠ {len(res['low_conf'])} palabras con confianza < 0.5 — revisá en words.json")

    def _gate_done(self, rep):
        self.g_btn.configure(text="Limpiar audio", state="normal")
        self.g_status.configure(text=f"✓ Listo · {rep['touched']} huecos tratados ({rep['muted_total']:.1f}s)")
        self._glog(f"\n✓ Guardado: {rep['out']}")
        self._glog("Duración sin cambios → el mismo words.json sigue sirviendo para el sync.")

    @staticmethod
    def _eta(sec: float) -> str:
        sec = max(0, int(round(sec)))
        return f"{sec}s" if sec < 60 else f"{sec // 60}m {sec % 60:02d}s"


if __name__ == "__main__":
    sys.excepthook = log_crash
    _t_imports = time.monotonic() - _T0
    _t1 = time.monotonic()
    _app = App()
    import release_state
    release_state.mark_healthy_from_environment()
    _t_ui = time.monotonic() - _t1
    # timings de arranque (review r2.17): a stderr (diagnostico.bat) Y a
    # logs/arranque.log (visible aunque se lance con pythonw). Vigila regresiones —
    # los available() del wizard importaban torch/mediapipe y costaban ~6s (2026-07-20).
    _linea = f"[arranque] imports {_t_imports:.1f}s · UI {_t_ui:.1f}s"
    print(_linea, file=sys.stderr)
    try:
        _ldir = app_paths.LOGS_DIR
        _ldir.mkdir(parents=True, exist_ok=True)
        with open(_ldir / "arranque.log", "a", encoding="utf-8") as _f:
            _f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {_linea}\n")
    except Exception:
        pass
    _app.mainloop()
