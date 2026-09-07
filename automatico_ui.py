"""Workspace Automático funcional para la Fase 1 editorial."""
from __future__ import annotations

import copy
import queue
import threading
import time
from bisect import bisect_left, bisect_right
from pathlib import Path
from tkinter import messagebox

import customtkinter as ctk

import dialogs
import editorial_chunks
import editorial_layers
import editorial_pipeline
import editorial_trims
import hardware
import podcast_export
import medios
from editorial_io import digest_json, format_time, parse_time, read_json
from editor_medios import EditorMedios, LANE_H


BG = "#111513"
SURFACE = "#181d1a"
SURFACE_RAISED = "#202622"
BORDER = "#303833"
MUTED = "#8b9790"
TEXT = "#eef3ef"
ACCENT = "#35a978"
ACCENT_HOVER = "#2d9168"
MEDIA_FILTERS = [("Audio o video", ["*.mp4", "*.mov", "*.mkv", "*.webm", "*.wav",
                                      "*.flac", "*.m4a", "*.mp3", "*.ogg", "*.opus"]),
                 ("Todos", ["*"])]

# ---- carril «recortes»: colores por origen (silencio / AI / usuario) ----
TRIMS_H = 26
COL_TRIM = {"silence": "#3b8fb3", "ai": "#a06cd5", "user": "#e8a33d"}
COL_TRIM_LOD = {True: "#3b8fb3", False: "#2b3d46"}
TRIM_ORIGIN_LABEL = {"silence": "silencio", "ai": "AI", "user": "tuyo"}
TRIM_LOD_MAX = 400                     # hasta acá se dibuja cada recorte por separado

# ---- panel derecho: ancho inicial, límites y divisor arrastrable ----
PANEL_WIDTH = 292                      # ancho por defecto (doble click en el divisor lo restaura)
PANEL_MIN = 292                        # por debajo se recortan los pasos del pipeline y recortes
EDITOR_MIN = 620                       # el editor conserva sus controles (390) + timeline útil
SASH_W = 10


class ChunkReviewDialog(ctk.CTkToplevel):
    def __init__(self, parent, master_path: Path, on_saved=None, *, master=None, document=None):
        super().__init__(parent)
        self.master_path = master_path
        self.root_path = master_path.parent
        self.on_saved = on_saved
        self.editorial_master = master if master is not None else read_json(master_path)
        self.document = document if document is not None else read_json(self.root_path / "views" / "chunks.json")
        self.rows: list[dict] = []
        self.title("Revisar bloques")
        self.geometry("920x520")
        self.minsize(760, 420)
        self.configure(fg_color=BG)
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(1, weight=1)

        header = ctk.CTkFrame(self, fg_color="transparent")
        header.grid(row=0, column=0, sticky="ew", padx=18, pady=(16, 8))
        header.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(header, text="Chunks macro", text_color=TEXT,
                     font=ctk.CTkFont(size=20, weight="bold")).grid(row=0, column=0, sticky="w")
        ctk.CTkLabel(header, text="Los límites son continuos y cubren toda la timeline.",
                     text_color=MUTED).grid(row=1, column=0, sticky="w")
        planner = self.document.get("planner", "desconocido")
        ctk.CTkLabel(header, text=f"Planificador: {planner}", text_color=ACCENT,
                     font=ctk.CTkFont(size=10, weight="bold")).grid(
                         row=2, column=0, sticky="w", pady=(3, 0))

        body = ctk.CTkScrollableFrame(self, fg_color=SURFACE, border_width=1,
                                      border_color=BORDER)
        body.grid(row=1, column=0, sticky="nsew", padx=18, pady=8)
        body.grid_columnconfigure(1, weight=1)
        for column, label in enumerate(("ID", "Título", "Inicio", "Final", "Confianza")):
            ctk.CTkLabel(body, text=label.upper(), text_color=MUTED,
                         font=ctk.CTkFont(size=10, weight="bold")).grid(
                             row=0, column=column, sticky="w", padx=8, pady=(8, 5))
        for index, chunk in enumerate(self.document["chunks"], 1):
            grid_row = index * 2 - 1
            ctk.CTkLabel(body, text=chunk["chunk_id"], text_color=TEXT).grid(
                row=grid_row, column=0, sticky="w", padx=8, pady=6)
            title = ctk.CTkEntry(body)
            title.insert(0, chunk["title"])
            title.grid(row=grid_row, column=1, sticky="ew", padx=8, pady=6)
            start = ctk.CTkEntry(body, width=128)
            start.insert(0, format_time(chunk["t_ini"]))
            start.grid(row=grid_row, column=2, padx=8, pady=6)
            end = ctk.CTkEntry(body, width=128)
            end.insert(0, format_time(chunk["t_fin"]))
            end.grid(row=grid_row, column=3, padx=8, pady=6)
            confidence = ctk.CTkEntry(body, width=72)
            confidence.insert(0, str(chunk.get("confidence", 0.0)))
            confidence.grid(row=grid_row, column=4, padx=8, pady=6)
            if index == 1:
                start.configure(state="disabled")
            if index == len(self.document["chunks"]):
                end.configure(state="disabled")
            self.rows.append({"title": title, "start": start, "end": end,
                              "confidence": confidence})

            explanation = " · ".join(filter(None, (chunk.get("summary"),
                chunk.get("end_reason"), " / ".join(chunk.get("warnings") or []))))
            if explanation:
                ctk.CTkLabel(body, text=explanation, text_color=MUTED, wraplength=780,
                             anchor="w", justify="left").grid(
                                 row=grid_row + 1, column=0, columnspan=5,
                                 sticky="ew", padx=8, pady=(0, 8))

        footer = ctk.CTkFrame(self, fg_color="transparent")
        footer.grid(row=2, column=0, sticky="ew", padx=18, pady=(8, 16))
        footer.grid_columnconfigure(0, weight=1)
        self.status = ctk.CTkLabel(footer, text="", text_color="#e6b85c")
        self.status.grid(row=0, column=0, sticky="w")
        ctk.CTkButton(footer, text="Cancelar", width=92, fg_color=SURFACE_RAISED,
                      hover_color="#2a322d", command=self.destroy).grid(row=0, column=1, padx=6)
        self.save_button = ctk.CTkButton(footer, text="Guardar límites", width=132, fg_color=ACCENT,
                                         hover_color=ACCENT_HOVER, command=self._save)
        self.save_button.grid(row=0, column=2, padx=(6, 0))
        self.transient(parent.winfo_toplevel())
        self.grab_set()

    def _save(self):
        try:
            chunks = []
            previous_end = 0.0
            duration = float(self.editorial_master["media"]["duration"])
            for index, (chunk, row) in enumerate(zip(self.document["chunks"], self.rows)):
                start = 0.0 if index == 0 else parse_time(row["start"].get())
                end = duration if index == len(self.rows) - 1 else parse_time(row["end"].get())
                if index and abs(start - previous_end) > 0.011:
                    raise ValueError(f"{chunk['chunk_id']}: el inicio debe coincidir con el final anterior")
                copy = {**chunk, "title": row["title"].get().strip() or chunk["chunk_id"],
                        "t_ini": start, "t_fin": end,
                        "confidence": float(row["confidence"].get())}
                chunks.append(copy)
                previous_end = end
            document = {**self.document, "planner": "manual-review", "chunks": chunks}
        except Exception as error:
            self.status.configure(text=str(error))
            return
        self.save_button.configure(state="disabled")
        self.status.configure(text="Validando y guardando…")
        result = queue.Queue()
        def work():
            try:
                with editorial_pipeline._RunLock(self.root_path / ".work"):
                    current_master = read_json(self.master_path)
                    saved = editorial_chunks.snap_plan_to_safe_boundaries(document, current_master)
                    editorial_chunks.apply_plan(self.root_path, self.master_path, saved,
                                                 persist_selection=True)
                result.put(None)
            except Exception as error:
                result.put(str(error))
        def poll():
            try:
                error = result.get_nowait()
            except queue.Empty:
                self.after(100, poll)
                return
            if error:
                self.status.configure(text=error)
                self.save_button.configure(state="normal")
            else:
                if self.on_saved:
                    self.on_saved()
                self.destroy()
        threading.Thread(target=work, daemon=True).start()
        self.after(100, poll)


class ResumeDialog(ctk.CTkToplevel):
    """Al reanudar con trabajo previo en la carpeta: ¿retomar lo ya extraído o reescribir todo?"""

    def __init__(self, parent, done: list[dict]):
        super().__init__(parent)
        self.choice = None
        self.title("Trabajo anterior encontrado")
        self.resizable(False, False)
        self.configure(fg_color=BG)
        self.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(self, text="Esta carpeta ya tiene pasos completados", text_color=TEXT,
                     font=ctk.CTkFont(size=16, weight="bold"), anchor="w").grid(
                         row=0, column=0, sticky="w", padx=20, pady=(18, 4))
        ctk.CTkLabel(self, text=("Se puede retomar usando la metadata ya extraída (solo se "
                                 "ejecuta lo que falta o lo que cambió), o borrar ese avance y "
                                 "empezar desde cero."),
                     text_color=MUTED, wraplength=440, justify="left", anchor="w").grid(
                         row=1, column=0, sticky="w", padx=20, pady=(0, 10))
        box = ctk.CTkFrame(self, fg_color=SURFACE, corner_radius=8, border_width=1,
                           border_color=BORDER)
        box.grid(row=2, column=0, sticky="ew", padx=20)
        box.grid_columnconfigure(0, weight=1)
        shown = done[:10]
        lines = [f"✓ {item['label']}" for item in shown]
        if len(done) > len(shown):
            lines.append(f"… y {len(done) - len(shown)} más")
        ctk.CTkLabel(box, text="\n".join(lines), text_color="#c6cec9", justify="left",
                     anchor="w", font=ctk.CTkFont(size=12)).grid(
                         row=0, column=0, sticky="w", padx=14, pady=10)
        buttons = ctk.CTkFrame(self, fg_color="transparent")
        buttons.grid(row=3, column=0, sticky="ew", padx=20, pady=(14, 18))
        buttons.grid_columnconfigure((0, 1, 2), weight=1)
        ctk.CTkButton(buttons, text="Retomar con lo ya hecho", height=34, fg_color=ACCENT,
                      hover_color=ACCENT_HOVER, command=lambda: self._pick("resume")).grid(
                          row=0, column=0, sticky="ew", padx=(0, 4))
        ctk.CTkButton(buttons, text="Empezar de cero (reescribir)", height=34,
                      fg_color="#7a3b3b", hover_color="#8f4646",
                      command=lambda: self._pick("rebuild")).grid(
                          row=0, column=1, sticky="ew", padx=4)
        ctk.CTkButton(buttons, text="Cancelar", height=34, fg_color=SURFACE_RAISED,
                      hover_color="#2a322d", command=lambda: self._pick(None)).grid(
                          row=0, column=2, sticky="ew", padx=(4, 0))
        self.protocol("WM_DELETE_WINDOW", lambda: self._pick(None))
        self.transient(parent.winfo_toplevel())

    def _pick(self, choice):
        self.choice = choice
        self.destroy()

    def ask(self):
        self.update_idletasks()
        self.grab_set()
        self.focus_set()
        self.wait_window()
        return self.choice


class AutomaticWorkspace:
    """Importación, selección multipista, ejecución del perfil editorial, bloques y recortes."""

    def __init__(self, parent):
        from editorial_layers_ui import LayersController
        self.layers = LayersController(self)
        self._last_layers_stamp = None
        self._last_topics_stamp = None
        self.info = None
        self.fingerprint = None
        self.track_widgets: list[dict] = []
        self.events: queue.Queue = queue.Queue()
        self.cancel = threading.Event()
        self.worker: threading.Thread | None = None
        self.result: dict | None = None
        self.plan: dict | None = None
        self._last_plan_stamp = None
        self._last_trims_stamp = None
        self._poll_counter = 0
        self.review_dialog = None
        # ---- recortes (carril interactivo; documento views/trims.json) ----
        self.trims: dict | None = None
        self.trims_path: Path | None = None
        self.sel_cut: dict | None = None
        self._drag_cut: dict | None = None
        self._trim_starts: list[float] = []
        self._trim_pme: list[float] = []
        self._boundary_index: editorial_trims.BoundaryIndex | None = None
        self._skip_intervals: list[tuple[float, float]] = []
        self._last_skip: tuple[int, float] | None = None
        self._review_stale = False
        self._tt_items: list = []
        self._worker_label = None
        self._cycle_state = None

        self.f = ctk.CTkFrame(parent, fg_color=BG, corner_radius=0)
        self.f.grid_columnconfigure(0, weight=1)
        self.f.grid_rowconfigure(1, weight=1)
        self._build_project_bar()
        self._build_workspace()
        self.f.after(100, self._pump)

    def _build_project_bar(self):
        bar = ctk.CTkFrame(self.f, fg_color="transparent")
        bar.grid(row=0, column=0, sticky="ew", padx=18, pady=(12, 8))
        bar.grid_columnconfigure(0, weight=1)
        self.project_title = ctk.CTkLabel(
            bar, text="Proyecto sin nombre", text_color=TEXT,
            font=ctk.CTkFont(size=18, weight="bold"))
        self.project_title.grid(row=0, column=0, sticky="w")
        self.project_status = ctk.CTkLabel(
            bar, text="AUTOMÁTICO · PERFIL EDITORIAL", text_color=MUTED,
            font=ctk.CTkFont(size=10, weight="bold"))
        self.project_status.grid(row=1, column=0, sticky="w", pady=(2, 0))
        self.import_button = ctk.CTkButton(
            bar, text="Importar video", width=138, height=34, fg_color=ACCENT,
            hover_color=ACCENT_HOVER, command=self._pick_media)
        self.import_button.grid(row=0, column=1, rowspan=2, sticky="e")

    def _build_workspace(self):
        from editorial_layers_ui import LayerDetailBar
        body = ctk.CTkFrame(self.f, fg_color="transparent")
        body.grid(row=1, column=0, sticky="nsew", padx=14, pady=(0, 14))
        body.grid_columnconfigure(0, weight=1)
        body.grid_rowconfigure(0, weight=1)
        self.body = body

        self.editor = EditorMedios(body, ancho_ctl=390,
                                   controles_pista_extra=self._build_track_controls,
                                   on_video_cargado=self._on_media_loaded,
                                   carriles_extra=self._cut_lanes,
                                   on_playhead=self._on_playhead,
                                   acciones_extra=self.layers.action, marcas_en_capas=True)
        self.editor.f.grid(row=0, column=0, sticky="nsew")
        # las marcas que escribe el editor (M, I/O, X, prompt, arrastre) entran al
        # historial de deshacer del proyecto (diseño §4)
        self.editor.transaccion = self.layers.transact
        # barra de herramientas (§7) a la izquierda del transporte, como en un NLE:
        # Selección / Corte con tooltip (atajo vigente), botones de edición y capas
        import toolbar_ui
        import keymap
        self.tool_bar = ctk.CTkSegmentedButton(
            self.editor.fr_tools, values=["Selección", "Corte"], height=28,
            font=ctk.CTkFont(size=11), command=self._tool_picked)
        self.tool_bar.set("Selección")
        self.tool_bar.grid(row=0, column=0, padx=(0, 8))
        toolbar_ui.Tooltip(self.tool_bar, lambda: "Herramientas: " + keymap.tooltip_text("tools.select")
                           + "  ·  " + keymap.tooltip_text("tools.cut"))
        self.layers.on_tool_change = lambda tool: self.tool_bar.set("Corte" if tool == "cut" else "Selección")
        self.tool_buttons = {}
        for col, (glyph, action) in enumerate((("↶", "edit.undo"), ("↷", "edit.redo"),
                                                ("✂", "edit.split"), ("⇤", "edit.trim_start"),
                                                ("⇥", "edit.trim_end"), ("✓", "edit.accept"),
                                                ("◉", "edit.activate"), ("⊘", "edit.toggle"),
                                                ("🗑", "edit.delete"),
                                                ("＋", "layers.new_lane")), start=1):
            button = toolbar_ui.tool_button(self.editor.fr_tools, glyph, action, self.editor.ejecutar, width=30)
            button.grid(row=0, column=col, padx=1)
            self.tool_buttons[action] = button
        # el menú contextual del editor (click derecho y ⋮) muestra primero el item
        # bajo el cursor y después todas las acciones que este dueño atiende
        self.editor.menu_extra = self.layers.menu_items
        self.editor.acciones_soportadas = self.layers.supported_actions
        # detalle del item de capa bajo el mouse / seleccionado: barra de altura FIJA
        # en la fila libre del editor (entre el timeline y el status) — nada de
        # escribirlo en el status, cuyo wrap movía timeline y preview con cada hover
        self.layers.detail = LayerDetailBar(self.editor.f, row=4, colors=dict(
            bg=BG, surface=SURFACE, raised=SURFACE_RAISED, border=BORDER, muted=MUTED,
            text=TEXT, text_soft="#c6cec9"))
        # hover = tooltip + barra de detalle · salir del timeline vuelve a la selección
        # · click derecho = menú · click en MARCAS deselecciona
        self.editor.tl.bind("<Motion>", self.layers.hover, add=True)
        self.editor.tl.bind("<Leave>", self.layers.leave, add=True)

        self._build_sash(body)
        self._panel_width = self._clamp_panel(hardware.load().get("automatico_panel_width"))
        panel = ctk.CTkScrollableFrame(body, width=self._panel_width, fg_color=SURFACE,
                                       corner_radius=10, border_width=1, border_color=BORDER)
        panel.grid(row=0, column=2, sticky="nsew")
        self.panel = panel
        panel.grid_columnconfigure(0, weight=1)
        panel.grid_rowconfigure(6, weight=1)
        ctk.CTkLabel(panel, text="PIPELINE EDITORIAL", text_color=MUTED,
                     font=ctk.CTkFont(size=10, weight="bold")).grid(
                         row=0, column=0, sticky="w", padx=14, pady=(14, 5))
        self.pipeline_title = ctk.CTkLabel(panel, text="Importa un medio", text_color=TEXT,
                                           font=ctk.CTkFont(size=14, weight="bold"))
        self.pipeline_title.grid(row=1, column=0, sticky="w", padx=14, pady=(0, 8))

        # Ajustes de la corrida: modelo de Whisper y pasos marcables (todos activos por defecto).
        options = ctk.CTkFrame(panel, fg_color="transparent")
        options.grid(row=2, column=0, sticky="ew", padx=14, pady=(0, 4))
        options.grid_columnconfigure(0, weight=1)
        model_row = ctk.CTkFrame(options, fg_color="transparent")
        model_row.grid(row=0, column=0, sticky="ew", pady=(0, 6))
        model_row.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(model_row, text="Modelo Whisper", text_color="#c6cec9", anchor="w").grid(
            row=0, column=0, sticky="ew")
        self.model_menu = ctk.CTkOptionMenu(model_row, values=hardware.WHISPER_MODELS, width=132,
                                            height=26, font=ctk.CTkFont(size=11),
                                            dynamic_resizing=False)
        self.model_menu.set(hardware.whisper_model())
        self.model_menu.grid(row=0, column=1, sticky="e")

        self.stage_labels = {}
        self.step_checks = {}
        stages = (("extract", "Preparar pistas", False), ("whisper", "Whisper", False),
                  ("align", "Alineación MMS (timestamps)", True),
                  ("prosody", "Intensidad + emoción", True), ("laughter", "Risa", True),
                  ("master", "Metadata para AI externa", False))
        for row, (key, label, optional) in enumerate(stages, 1):
            line = ctk.CTkFrame(options, fg_color="transparent")
            line.grid(row=row, column=0, sticky="ew", pady=1)
            line.grid_columnconfigure(0, weight=1)
            if optional:
                check = ctk.CTkCheckBox(line, text=label, text_color="#c6cec9", height=22,
                                        checkbox_width=16, checkbox_height=16,
                                        font=ctk.CTkFont(size=12))
                check.select()
                check.grid(row=0, column=0, sticky="ew")
                self.step_checks[key] = check
            else:
                ctk.CTkLabel(line, text=label, text_color="#c6cec9", anchor="w",
                             font=ctk.CTkFont(size=12)).grid(row=0, column=0, sticky="ew",
                                                             padx=(24, 0))
            state = ctk.CTkLabel(line, text="EN ESPERA", text_color="#69756e",
                                 font=ctk.CTkFont(size=9, weight="bold"))
            state.grid(row=0, column=1)
            self.stage_labels[key] = state
        ctk.CTkLabel(options, text="Desmarca un paso para omitirlo en esta corrida.",
                     text_color=MUTED, anchor="w", font=ctk.CTkFont(size=10)).grid(
                         row=len(stages) + 1, column=0, sticky="w", pady=(2, 0))

        self.progress = ctk.CTkProgressBar(panel, progress_color=ACCENT)
        self.progress.set(0)
        self.progress.grid(row=3, column=0, sticky="ew", padx=14, pady=(8, 7))
        self.output_entry = ctk.CTkEntry(panel, placeholder_text="Carpeta del proyecto")
        self.output_entry.grid(row=4, column=0, sticky="ew", padx=14, pady=4)
        ctk.CTkButton(panel, text="Elegir carpeta", height=28, fg_color=SURFACE_RAISED,
                      hover_color="#2a322d", command=self._pick_output).grid(
                          row=5, column=0, sticky="ew", padx=14, pady=(2, 7))
        self.log = ctk.CTkTextbox(panel, wrap="word", font=ctk.CTkFont(size=10))
        self.log.grid(row=6, column=0, sticky="nsew", padx=14, pady=7)
        self.log.configure(state="disabled")

        # ---- panel de acciones (plan-montaje-ai.md §4): de arriba abajo, en el orden
        # real del flujo; UN botón principal para la AI con sus variantes en un
        # desplegable y, debajo, la etiqueta con el estado del ciclo ----
        tip = toolbar_ui.Tooltip
        actions = ctk.CTkFrame(panel, fg_color="transparent")
        actions.grid(row=7, column=0, sticky="ew", padx=14, pady=(4, 6))
        actions.grid_columnconfigure((0, 1), weight=1)
        self.view_button = ctk.CTkButton(actions, text="Conversación", height=28,
                                         state="disabled", fg_color=SURFACE_RAISED,
                                         hover_color="#2a322d", command=self._show_conversation)
        self.view_button.grid(row=0, column=0, sticky="ew", padx=(0, 3))
        tip(self.view_button, "Abre views/conversation.md: todas las pistas intercaladas con "
                              "timecodes e IDs de intervención (solo lectura).")
        self.chunks_button = ctk.CTkButton(actions, text="Revisar bloques", height=28,
                                           state="disabled", fg_color=SURFACE_RAISED,
                                           hover_color="#2a322d", command=self._review_chunks)
        self.chunks_button.grid(row=0, column=1, sticky="ew", padx=(3, 0))
        tip(self.chunks_button, "Edita títulos y límites de los bloques propuestos por la AI "
                                "(Tarea 1). Guardar vuelve a ajustar los bordes.")
        self.agent_button = ctk.CTkButton(panel, text="Importar JSON de la AI", height=28,
                                          state="disabled", fg_color=SURFACE_RAISED,
                                          hover_color="#2a322d", command=self._import_external_json)
        self.agent_button.grid(row=8, column=0, sticky="ew", padx=14, pady=(0, 6))
        tip(self.agent_button, "Importa a mano un *.proposed.json de la AI. Normalmente no hace "
                               "falta: la app detecta sola los archivos en views/ cada 2 s.")
        self._build_trims_panel(panel, row=9)
        # Formato de salida: vale para los dos botones de exportación; se recuerda.
        export_row = ctk.CTkFrame(panel, fg_color="transparent")
        export_row.grid(row=10, column=0, sticky="ew", padx=14, pady=(0, 2))
        export_row.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(export_row, text="Salida", text_color="#c6cec9", anchor="w",
                     font=ctk.CTkFont(size=12)).grid(row=0, column=0, sticky="ew")
        self.format_menu = ctk.CTkOptionMenu(
            export_row, values=[spec["label"] for spec in podcast_export.FORMATS.values()],
            width=196, height=26, font=ctk.CTkFont(size=11), dynamic_resizing=False,
            command=self._format_changed)
        saved = hardware.load().get("export_format")
        if saved not in podcast_export.FORMATS:
            saved = podcast_export.DEFAULT_FORMAT
        self.format_menu.set(podcast_export.FORMATS[saved]["label"])
        self.format_menu.grid(row=0, column=1, sticky="e")
        self.format_help = ctk.CTkLabel(panel, text=podcast_export.FORMATS[saved]["help"],
                                        text_color=MUTED, anchor="w", justify="left",
                                        wraplength=max(140, self._panel_width - 48),
                                        font=ctk.CTkFont(size=10))
        self.format_help.grid(row=11, column=0, sticky="ew", padx=14, pady=(0, 6))
        self.accept_button = ctk.CTkButton(panel, text="Exportar bloques", height=32,
                                           state="disabled", fg_color=ACCENT,
                                           command=self._accept_cuts)
        self.accept_button.grid(row=12, column=0, sticky="ew", padx=14, pady=(0, 6))
        tip(self.accept_button, "Exporta un video por bloque del plan (Tarea 1), SIN aplicar "
                                "recortes. Solo en el proyecto padre y con plan.")
        self.open_project_button = ctk.CTkButton(panel, text="Abrir proyecto existente", height=28,
                                                fg_color=SURFACE_RAISED,
                                                command=self._open_project)
        self.open_project_button.grid(row=13, column=0, sticky="ew", padx=14, pady=(0, 6))
        tip(self.open_project_button, "Carga un master editorial por ruta. Al importar un video la "
                                      "app ya busca sola su proyecto por huella del contenido.")
        self.run_button = ctk.CTkButton(panel, text="Procesar pistas de voz", height=38,
                                        state="disabled", fg_color=ACCENT,
                                        hover_color=ACCENT_HOVER, command=self._run_or_cancel,
                                        font=ctk.CTkFont(size=13, weight="bold"))
        self.run_button.grid(row=14, column=0, sticky="ew", padx=14, pady=(0, 14))
        tip(self.run_button, "Transcribe y analiza las pistas marcadas como VOZ (Whisper, MMS, "
                             "risa, intensidad). Reanudar retoma lo ya hecho. En un video "
                             "recortado no hace falta: la metadata viene heredada.")
        self.layers_button = ctk.CTkButton(panel, text="Capas…", command=self.layers.manage)
        self.layers_button.grid(row=15, column=0, sticky="ew", padx=14, pady=5)
        tip(self.layers_button, "Añade, renombra, reordena o borra carriles del timeline "
                                "(recortes tuyos, pedidos para la AI, capas de la AI).")
        ai_row = ctk.CTkFrame(panel, fg_color="transparent")
        ai_row.grid(row=16, column=0, sticky="ew", padx=14, pady=(5, 2))
        ai_row.grid_columnconfigure(0, weight=1)
        self.ai_button = ctk.CTkButton(ai_row, text="Preparar para la AI", height=34,
                                       fg_color="#4a3a5e", hover_color="#5a4772",
                                       font=ctk.CTkFont(size=13, weight="bold"),
                                       command=lambda: self._ai_option(self.AI_DEFAULT))
        self.ai_button.grid(row=0, column=0, sticky="ew")
        tip(self.ai_button, lambda: f"Prepara el pedido «{self.AI_OPTIONS[self.AI_DEFAULT][0]}» "
                                    "(la opción por defecto). La flecha muestra las demás variantes. "
                                    "Todas escriben views/layers.json y esperan la respuesta de la AI.")
        self.ai_arrow = ctk.CTkButton(ai_row, text="▾", width=34, height=34, fg_color="#4a3a5e",
                                      hover_color="#5a4772", font=ctk.CTkFont(size=14, weight="bold"),
                                      command=self._ai_menu)
        self.ai_arrow.grid(row=0, column=1, padx=(3, 0))
        tip(self.ai_arrow, "Variantes: revisión completa, solo temas, solo recortes, "
                           "recortes profundos, montaje por temas.")
        self.cycle_label = ctk.CTkLabel(panel, text="Sin pedido preparado.", text_color=MUTED,
                                        anchor="w", justify="left",
                                        wraplength=max(140, self._panel_width - 48),
                                        font=ctk.CTkFont(size=10))
        self.cycle_label.grid(row=17, column=0, sticky="ew", padx=14, pady=(0, 12))
        self._last_import_error = None
        self._cycle_text = None

    def _tool_picked(self, label):
        self.layers.set_tool("cut" if label == "Corte" else "select")
        self.editor.tl.focus_set()

    # ---- formato de salida ----
    def _export_format(self) -> str:
        label = self.format_menu.get()
        return next((key for key, spec in podcast_export.FORMATS.items() if spec["label"] == label),
                    podcast_export.DEFAULT_FORMAT)

    def _format_changed(self, _label=None):
        fmt = self._export_format()
        self.format_help.configure(text=podcast_export.FORMATS[fmt]["help"])
        hardware.set_(export_format=fmt)

    # ---- divisor arrastrable entre el editor y el panel derecho ----
    def _build_sash(self, body):
        """El ancho del panel se cambia arrastrando el divisor (cursor ↔, agarradera
        que se enciende al pasar); doble click restaura el ancho inicial. El valor
        se recuerda en config.json al soltar. Mover el divisor pasa por el mismo
        camino que redimensionar la ventana (el editor ya lo debounce-a)."""
        sash = ctk.CTkFrame(body, width=SASH_W, fg_color="transparent",
                            cursor="sb_h_double_arrow")
        sash.grid(row=0, column=1, sticky="ns")
        self._sash_grip = ctk.CTkFrame(sash, width=3, height=40, corner_radius=2,
                                       fg_color=BORDER, cursor="sb_h_double_arrow")
        self._sash_grip.place(relx=.5, rely=.5, anchor="center")
        self._sash_drag = None
        for widget in (sash, self._sash_grip):
            widget.bind("<Enter>", lambda e: self._sash_grip.configure(fg_color=ACCENT))
            widget.bind("<Leave>", self._sash_leave)
            widget.bind("<Button-1>", self._sash_press)
            widget.bind("<B1-Motion>", self._sash_move)
            widget.bind("<ButtonRelease-1>", self._sash_release)
            widget.bind("<Double-Button-1>", self._sash_reset)

    def _clamp_panel(self, width) -> int:
        try:
            width = int(width)
        except (TypeError, ValueError):
            width = PANEL_WIDTH
        body_width = self.body.winfo_width()
        limit = max(PANEL_MIN, body_width - EDITOR_MIN) if body_width > 1 else 900
        return int(max(PANEL_MIN, min(limit, width)))

    def _apply_panel_width(self, width: int):
        self._panel_width = width
        self.panel.configure(width=width)
        for label in (self.trims_status, self.format_help, self.cycle_label):
            label.configure(wraplength=max(140, width - 48))

    def _sash_leave(self, _e=None):
        if self._sash_drag is None:
            self._sash_grip.configure(fg_color=BORDER)

    def _sash_press(self, e):
        # e.x_root está en píxeles reales; el ancho del panel en unidades de CTk
        self._sash_drag = (e.x_root, self._panel_width,
                           ctk.ScalingTracker.get_widget_scaling(self.panel) or 1.0)

    def _sash_move(self, e):
        if self._sash_drag is None:
            return
        x0, width0, scale = self._sash_drag
        width = self._clamp_panel(width0 - (e.x_root - x0) / scale)
        if width != self._panel_width:
            self._apply_panel_width(width)

    def _sash_release(self, _e=None):
        if self._sash_drag is None:
            return
        self._sash_drag = None
        self._sash_grip.configure(fg_color=BORDER)
        hardware.set_(automatico_panel_width=self._panel_width)

    def _sash_reset(self, _e=None):
        self._sash_drag = None
        self._apply_panel_width(self._clamp_panel(PANEL_WIDTH))
        hardware.set_(automatico_panel_width=self._panel_width)

    def _build_trims_panel(self, panel, *, row: int):
        """Sección RECORTES: heurística de silencios, revisión para la AI y corte final.
        Nada de esto toca el video hasta «Cortar y exportar»."""
        box = ctk.CTkFrame(panel, fg_color=SURFACE_RAISED, corner_radius=8)
        box.grid(row=row, column=0, sticky="ew", padx=14, pady=(0, 6))
        box.grid_columnconfigure(0, weight=1)
        head = ctk.CTkFrame(box, fg_color="transparent")
        head.grid(row=0, column=0, sticky="ew", padx=10, pady=(6, 2))
        head.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(head, text="RECORTES", text_color=MUTED,
                     font=ctk.CTkFont(size=10, weight="bold")).grid(row=0, column=0, sticky="w")
        for column, (origin, label) in enumerate((("silence", "silencio"), ("ai", "AI"),
                                                  ("user", "tuyos")), 1):
            ctk.CTkLabel(head, text=f"■ {label}", text_color=COL_TRIM[origin],
                         font=ctk.CTkFont(size=9, weight="bold")).grid(row=0, column=column,
                                                                        padx=(6, 0))
        line = ctk.CTkFrame(box, fg_color="transparent")
        line.grid(row=1, column=0, sticky="ew", padx=10, pady=(0, 4))
        line.grid_columnconfigure(0, weight=1)
        self.silence_button = ctk.CTkButton(line, text="Analizar silencios", height=26,
                                            state="disabled", fg_color="#24566a",
                                            hover_color="#2b6a82", command=self._analyze_silences)
        self.silence_button.grid(row=0, column=0, sticky="ew", padx=(0, 4))
        ctk.CTkLabel(line, text="mín", text_color=MUTED, font=ctk.CTkFont(size=10)).grid(
            row=0, column=1, padx=(2, 2))
        self.min_gap_entry = ctk.CTkEntry(line, width=40, height=24, font=ctk.CTkFont(size=11))
        self.min_gap_entry.insert(0, "1.0")
        self.min_gap_entry.grid(row=0, column=2)
        ctk.CTkLabel(line, text="margen", text_color=MUTED, font=ctk.CTkFont(size=10)).grid(
            row=0, column=3, padx=(4, 2))
        self.margin_entry = ctk.CTkEntry(line, width=40, height=24, font=ctk.CTkFont(size=11))
        self.margin_entry.insert(0, "0.3")
        self.margin_entry.grid(row=0, column=4)
        self.trims_status = ctk.CTkLabel(box, text="Sin recortes. Analiza silencios o arrastra "
                                                   "en el carril «recortes» del timeline.",
                                         text_color=MUTED, anchor="w", justify="left",
                                         wraplength=max(140, self._panel_width - 48),
                                         font=ctk.CTkFont(size=10))
        self.trims_status.grid(row=2, column=0, sticky="ew", padx=10, pady=(0, 4))
        import toolbar_ui
        toolbar_ui.Tooltip(self.silence_button, "Propone recortes en los huecos sin palabras ni risas "
                           "de ninguna pista (mín = hueco mínimo, margen = silencio que se "
                           "conserva). Nada se corta: los revisas en el carril «Recortes».")
        self.trim_export_button = ctk.CTkButton(box, text="Exportar con recortes", height=28,
                                                state="disabled", fg_color="#8a5a24",
                                                hover_color="#a06a2b", command=self._export_trims)
        self.trim_export_button.grid(row=3, column=0, sticky="ew", padx=10, pady=(0, 4))
        toolbar_ui.Tooltip(self.trim_export_button, "Aplica los recortes activos y crea un video "
                           "nuevo con su proyecto hijo (temas y capas heredados). El original "
                           "no se toca.")
        self.skip_check = ctk.CTkCheckBox(box, text="Saltar recortes al reproducir", height=20,
                                          checkbox_width=14, checkbox_height=14,
                                          text_color="#c6cec9", font=ctk.CTkFont(size=10))
        self.skip_check.grid(row=4, column=0, sticky="w", padx=10, pady=(0, 6))
        toolbar_ui.Tooltip(self.skip_check, "Al reproducir, salta los tramos recortados activos "
                           "(re-arranca la sesión al final de cada uno). Ayuda de revisión, "
                           "no el render.")

    # ---- botón principal de la AI y estado del ciclo (plan §4) ----
    AI_DEFAULT = "full"
    AI_OPTIONS = {
        "full": ("Revisión completa (temas + recortes)", "_prepare_editorial"),
        "topics": ("Solo temas", "_prepare_topics"),
        "trims": ("Solo recortes", "_prepare_review"),
    }

    def _ai_menu(self):
        """La flecha del botón principal: las variantes de «Preparar para la AI»."""
        import tkinter as tk
        menu = tk.Menu(self.f, tearoff=False)
        for key, (label, _) in self.AI_OPTIONS.items():
            menu.add_command(label=label + ("   (por defecto)" if key == self.AI_DEFAULT else ""),
                             command=lambda k=key: self._ai_option(k))
        x = self.ai_button.winfo_rootx()
        y = self.ai_button.winfo_rooty() + self.ai_button.winfo_height()
        try:
            menu.tk_popup(x, y)
        finally:
            menu.grab_release()

    def _ai_option(self, key):
        if self.worker and self.worker.is_alive():
            self._append_log("Espera a que termine la operación en curso.")
            return
        label, handler = self.AI_OPTIONS[key]
        self._append_log(f"Preparar para la AI → {label}")
        getattr(self, handler)()

    def _is_child(self) -> bool:
        store = self.layers.store
        return bool(store and store.master.get("derivation"))

    def _refresh_child_mode(self):
        """En un proyecto hijo (video ya recortado) «Procesar pistas» y «Exportar
        bloques» no hacen nada: se OCULTAN. El botón de procesar vuelve a verse solo
        mientras hay un trabajo en curso, porque ahí es el botón «Cancelar»."""
        child = self._is_child()
        busy = bool(self.worker and self.worker.is_alive())
        for button in (self.run_button, self.accept_button):
            if child and not (busy and button is self.run_button):
                button.grid_remove()
            else:
                button.grid()

    def _refresh_cycle_label(self):
        """Etiqueta bajo el botón principal: en qué punto del ciclo con la AI estamos
        (`editorial_cycle.status`, puro). Barata: se llama en cada sondeo."""
        master = self._master_path()
        if not master or not self.result:
            text = "Sin pedido preparado."
        else:
            import editorial_cycle
            digest = None
            if self.layers.store:
                try:
                    digest = editorial_layers.snapshot_value(
                        self.layers.store.master, self.layers.all(),
                        master_digest=self.layers.store.source_digest)["source_layers_digest"]
                except Exception:
                    digest = None
            state = editorial_cycle.status(master.parent / "views", layers_digest=digest,
                                           last_error=self._last_import_error)
            text = state["text"]
            self._cycle_state = state
        if text != self._cycle_text:
            self._cycle_text = text
            self.cycle_label.configure(text=text, text_color="#e6b85c" if "viejo" in text else MUTED)

    def set_default_model(self, value: str):
        """Sigue al modelo por defecto de Ajustes (sin tocar una corrida en curso)."""
        if value in hardware.WHISPER_MODELS and not (self.worker and self.worker.is_alive()):
            self.model_menu.set(value)

    def _run_options(self) -> dict:
        return {"model": self.model_menu.get(),
                "steps": {key: bool(check.get()) for key, check in self.step_checks.items()}}

    def _build_track_controls(self, row, track, index):
        selected = ctk.CTkCheckBox(row, text="VOZ", width=54, height=26)
        if index < 2:
            selected.select()
        selected.grid(row=0, column=3, padx=(3, 0), pady=(LANE_H // 2 - 13, 0))
        label = ctk.CTkEntry(row, width=172, height=26,
                             placeholder_text=track.get("titulo") or f"Voz {index + 1}")
        if track.get("titulo"):
            label.insert(0, track["titulo"])
        label.grid(row=0, column=0, padx=(0, 4), pady=(LANE_H // 2 - 13, 0))
        self.track_widgets.append({"idx": track["idx"], "selected": selected, "label": label})

    def _pick_media(self):
        path = dialogs.open_file("Video largo", MEDIA_FILTERS, remember="editorial_source")
        if path:
            self.editor.cargar(path)

    def _pick_output(self):
        start = self.output_entry.get().strip() or None
        path = dialogs.open_dir("Carpeta del proyecto", start=start, remember="editorial_output")
        if path:
            self.output_entry.delete(0, "end")
            self.output_entry.insert(0, path)

    def _on_media_loaded(self, info, fingerprint):
        self.layers.store = None
        self.layers.selected = None
        self.layers.sync_detail()
        self._last_layers_stamp = None
        self._last_topics_stamp = None
        self.info, self.fingerprint = info, fingerprint
        self.result = None
        self.plan = None
        self._last_plan_stamp = None
        self._last_trims_stamp = None
        self.track_widgets = []
        self.trims = None
        self.trims_path = None
        self.sel_cut = None
        self._drag_cut = None
        self._boundary_index = None
        self._review_stale = False
        self._reindex_trims()
        self._refresh_trims_status()
        source = Path(info["path"])
        self.project_title.configure(text=source.stem)
        self.project_status.configure(text=f"AUTOMÁTICO · {len(info['pistas'])} PISTAS DE AUDIO")
        default = source.parent / source.stem
        self.output_entry.delete(0, "end")
        self.output_entry.insert(0, str(default))
        # Las marcas y los pedidos manuales existen aun antes de transcribir.
        import editorial_layers
        try:
            self.layers.store = editorial_layers.LayerStore(default / "editorial",
                editorial_layers.media_context(info, fingerprint))
        except (ValueError, OSError) as error:
            self._append_log(f"No se pudieron abrir las capas previas: {error}")
        self.editor.refrescar_layout()
        self.pipeline_title.configure(text="Selecciona las pistas de voz")
        self.run_button.configure(state="normal")
        for button in (self.view_button, self.chunks_button, self.agent_button, self.accept_button,
                       self.silence_button, self.trim_export_button, self.ai_button, self.ai_arrow):
            button.configure(state="disabled")
        self._last_import_error = None
        self._refresh_child_mode()
        self._refresh_cycle_label()
        generation = self.editor._gen
        def discover():
            import editorial_catalog
            try:
                candidates, warnings = editorial_catalog.discover(source, fingerprint)
                self.events.put({"tipo":"discovered","generation":generation,
                                 "candidates":candidates,"warnings":warnings})
            except Exception as error:
                self.events.put({"tipo":"log","message":f"Descubrimiento: {error}"})
        threading.Thread(target=discover,daemon=True,name="catalog-discovery").start()

    def _selected_tracks(self) -> list[dict]:
        selected = []
        for widget in self.track_widgets:
            if not widget["selected"].get():
                continue
            label = widget["label"].get().strip() or f"Voz {len(selected) + 1}"
            selected.append({"stream_index": widget["idx"], "label": label})
        return selected

    def _set_processing(self, active: bool):
        state = "disabled" if active else "normal"
        self.import_button.configure(state=state)
        self.output_entry.configure(state=state)
        self.open_project_button.configure(state=state)
        self.model_menu.configure(state=state)
        for check in self.step_checks.values():
            check.configure(state=state)
        for button in (self.view_button, self.chunks_button, self.agent_button, self.accept_button,
                       self.silence_button, self.trim_export_button, self.ai_button, self.ai_arrow):
            button.configure(state="disabled")
        if not active:
            self._refresh_plan_buttons()
        for widget in self.track_widgets:
            widget["selected"].configure(state=state)
            widget["label"].configure(state=state)
        self._refresh_child_mode()

    def _background_done(self):
        self._set_processing(False)
        self.run_button.configure(text="Reanudar", state="normal")

    def _run_or_cancel(self):
        if self.worker and self.worker.is_alive():
            self.cancel.set()
            self.run_button.configure(text="Cancelando…", state="disabled")
            return
        if not self.info:
            messagebox.showwarning("Falta el video", "Importa un video o audio primero.")
            return
        if self.layers.store and self.layers.store.master.get("derivation"):
            self._append_log("Proyecto derivado: la metadata ya está disponible. Usa Analizar silencios o Analizar temas.")
            return
        tracks = self._selected_tracks()
        if not tracks:
            messagebox.showwarning("Faltan voces", "Marca al menos una pista como VOZ.")
            return
        project_dir = self.output_entry.get().strip()
        if not project_dir:
            messagebox.showwarning("Falta la salida", "Elige una carpeta para el proyecto.")
            return
        options = self._run_options()
        rebuild = False
        done = editorial_pipeline.completed_work(project_dir)
        if done:
            choice = ResumeDialog(self.f, done).ask()
            if choice is None:
                return
            rebuild = choice == "rebuild"
        spec = {"source": self.info["path"], "project_dir": project_dir,
                "project_name": (Path(self.result["master"]).name.removesuffix(".editorial.master.json")
                                 if self.result else Path(self.info["path"]).stem), "tracks": tracks,
                "transcription": {"model": options["model"], "language": "es", "device": "auto"},
                "steps": options["steps"], "rebuild": rebuild,
                "chunking": {"mode": "external"}}
        self.cancel = threading.Event()
        self.progress.set(0)
        self.result = None
        self.plan = None
        self.editor.refrescar_layout()
        self._set_processing(True)
        self.run_button.configure(text="Cancelar", state="normal")
        self.pipeline_title.configure(text="Procesando…")
        for label in self.stage_labels.values():
            label.configure(text="EN ESPERA", text_color="#69756e")
        self._append_log("\n══════════ Nueva corrida editorial ══════════")
        skipped = [key for key, on in options["steps"].items() if not on]
        self._append_log(f"Modelo Whisper: {options['model']}"
                         + (f" · pasos omitidos: {', '.join(skipped)}" if skipped else "")
                         + (" · reescribiendo desde cero" if rebuild else
                            " · retomando lo ya hecho" if done else ""))

        def work():
            try:
                result = editorial_pipeline.run(
                    spec, event_cb=lambda event: self.events.put(event), cancel=self.cancel)
                self.events.put({"tipo": "ui_done", "result": result})
            except Exception as error:
                self.events.put({"tipo": "ui_error", "error": str(error)})

        self.worker = threading.Thread(target=work, daemon=True, name="editorial-pipeline")
        self.worker.start()

    @staticmethod
    def _stage_group(step: str) -> str:
        for prefix, group in (("extract_", "extract"), ("whisper_", "whisper"),
                              ("align_", "align"), ("transcribe_", "align"),
                              ("prosody_", "prosody"), ("laughter_", "laughter")):
            if step.startswith(prefix):
                return group
        return "master"

    def _append_log(self, message: str):
        self.log.configure(state="normal")
        self.log.insert("end", message.rstrip() + "\n")
        if int(self.log.index("end-1c").split(".")[0]) > 2000:
            self.log.delete("1.0", "500.0")
        self.log.see("end")
        self.log.configure(state="disabled")

    def _pump(self):
        try:
            for _ in range(200):
                event = self.events.get_nowait()
                kind = event.get("tipo")
                if kind == "log":
                    self._append_log(event.get("message", ""))
                elif kind == "overall":
                    self.progress.set(event.get("fraction", 0.0))
                elif kind == "step":
                    group = self._stage_group(event.get("step", ""))
                    status = event.get("status")
                    text, color = ({"running": ("PROCESANDO", "#e6b85c"),
                                    "ok": ("LISTO", ACCENT),
                                    "reused": ("REUTILIZADO", ACCENT),
                                    "skipped": ("OMITIDO", MUTED)}.get(
                                        status, (str(status).upper(), MUTED)))
                    self.stage_labels[group].configure(text=text, text_color=color)
                elif kind == "ui_done":
                    self.result = event["result"]
                    self._set_processing(False)
                    self.progress.set(1)
                    self.pipeline_title.configure(text="Paquete editorial listo")
                    planner = self.result.get("chunk_planner", "desconocido")
                    planner_status = ("CODEX GLOBAL" if planner == "codex-global-semantic/1"
                                      else "FALLBACK LOCAL" if planner == "local-fallback/1"
                                      else planner.upper())
                    self.project_status.configure(text=f"AUTOMÁTICO · {planner_status}")
                    self.run_button.configure(text="Reanudar", state="normal")
                    self._load_saved_plan()
                    self._refresh_plan_buttons()
                    self._load_trims_async()
                    self._append_log(f"Listo: {self.result['master']}")
                    self._append_log(f"Planificador: {self.result.get('chunk_planner', 'desconocido')}")
                elif kind == "review_ready":
                    self._background_done()
                    self.review_dialog = ChunkReviewDialog(self.f, event["path"],
                        on_saved=self._plan_reviewed, master=event["master"], document=self.plan)
                elif kind == "plan_loaded":
                    plan_before = event.get("plan_before")
                    self.plan = event["plan"]
                    if event.get("history"):   # importar una propuesta se puede deshacer
                        self.layers.history.record(event["history"], {"plan": plan_before},
                                                   {"plan": self.plan},
                                                   {"plan": self.layers._doc_revision("plan")})
                    self._review_stale = bool(self.trims and self.trims["cuts"])
                    self.editor.refrescar_layout()
                    self._background_done()
                    self._refresh_trims_status()
                    self._append_log(f"Propuesta lista: {len(self.plan['chunks'])} bloques. Revisa el carril de colores antes de aceptar.")
                elif kind == "trims_loaded":
                    self._on_trims_loaded(event)
                elif kind == "layers_loaded":
                    if str(self._master_path()) == event["master"]:
                        self.layers.store = event["store"]
                        self.layers.lane_order = editorial_layers.load_lane_order(event["store"].root)
                        self.layers.history.clear()          # historial por medio cargado
                        self.editor.refrescar_layout()
                        self._refresh_child_mode()
                        self._refresh_cycle_label()
                elif kind == "layers_imported":
                    self._last_import_error = None
                    self._background_done()
                    self._record_layers(event.get("before") or {}, "importar propuesta de capa")
                    self.layers.snapshot()
                    self.editor.refrescar_layout()
                    self._append_log("Propuesta de capa importada; revisa sus tramos en el timeline.")
                elif kind == "topics_imported":
                    self._last_import_error = None
                    self._background_done()
                    if event.get("pass") == 2:
                        self._record_layers(event.get("before") or {}, "importar temas de la AI")
                    self.editor.refrescar_layout()
                    self._append_log(event["message"])
                    self._refresh_cycle_label()
                elif kind == "review_written":
                    self._review_stale = False
                    self._background_done()
                    self._refresh_trims_status()
                    self._append_log("Revisión para la AI lista: " + str(event["request"]))
                    self._append_log(event.get("hint") or
                                     "Pide a la AI la Tarea 2 de la skill transcriptor; su "
                                     "trims.proposed.json se importa solo al aparecer.")
                    self._refresh_cycle_label()
                elif kind == "export_done":
                    self._background_done()
                    self.progress.set(1)
                    self._append_log(f"Videos exportados: {event['path']}")
                    self.pipeline_title.configure(text="Cortes exportados")
                elif kind == "project_loaded":
                    if event.get("generation",self.editor._gen) != self.editor._gen:
                        continue
                    self.result = event["result"]
                    self.plan = event["plan"]
                    self._set_processing(False)
                    tracks = {track["stream_index"]: track for track in event["tracks"]}
                    for widget in self.track_widgets:
                        track = tracks.get(widget["idx"])
                        widget["selected"].select() if track else widget["selected"].deselect()
                        if track:
                            widget["label"].delete(0, "end")
                            widget["label"].insert(0, track["label"])
                    self.output_entry.delete(0, "end")
                    self.output_entry.insert(0, str(Path(self.result["master"]).parent.parent))
                    self.editor.refrescar_layout()
                    self.run_button.configure(text="Reanudar", state="normal")
                    self._load_trims_async()
                    self._refresh_plan_buttons()
                    self.pipeline_title.configure(text="Proyecto detectado y cargado")
                    self._append_log("Metadata recuperada sin inferencia: " + str(self.result['master']))
                    if event.get("derived"):
                        self._append_log("Video recortado (proyecto hijo): la metadata y las capas "
                                         "vienen del padre; «Procesar pistas» y «Exportar bloques» "
                                         "no aplican aquí.")
                elif kind == "discovered":
                    if event["generation"] != self.editor._gen or self.result:
                        continue
                    for warning in event["warnings"][:8]:
                        self._append_log(warning)
                    if len(event["candidates"]) == 1:
                        self._load_project(event["candidates"][0]["path"])
                    elif event["candidates"]:
                        self._choose_discovered(event["candidates"])
                elif kind == "ui_error":
                    cancelled = self.cancel.is_set()
                    self._set_processing(False)
                    self.pipeline_title.configure(text="Cancelado" if cancelled else "Error")
                    self.run_button.configure(text="Reanudar", state="normal")
                    self._append_log(("Cancelado: " if cancelled else "ERROR: ") + event["error"])
                    if not cancelled and str(self._worker_label or "").startswith("import"):
                        self._last_import_error = event["error"]
                    self._refresh_cycle_label()
        except queue.Empty:
            pass
        self._poll_counter += 1
        if (self._poll_counter % 20 == 0 and self.result
                and not (self.worker and self.worker.is_alive())
                and not (self.review_dialog and self.review_dialog.winfo_exists())):
            views = Path(self.result["master"]).parent / "views"
            self._poll_proposal(views / "cuts.proposed.json", "_last_plan_stamp",
                                lambda path: self._import_plan(path, reuse_proposal=True))
            self._poll_proposal(views / "trims.proposed.json", "_last_trims_stamp",
                                self._import_trims)
            self._poll_proposal(views / "layers.proposed.json", "_last_layers_stamp", self._import_layers)
            self._poll_proposal(views / "topics.proposed.json", "_last_topics_stamp", self._import_topics)
            self._refresh_cycle_label()
        self.f.after(100, self._pump)

    def _poll_proposal(self, path: Path, attribute: str, action):
        """La AI escribe su JSON fuera de la app: se importa solo cuando aparece o cambia."""
        if self.worker and self.worker.is_alive():
            return
        stamp = self._stamp_of(path)
        if stamp is not None and stamp != getattr(self, attribute):
            setattr(self, attribute, stamp)
            action(path)

    @staticmethod
    def _stamp_of(path: Path):
        try:
            stat = Path(path).stat()
        except OSError:
            return None
        return (str(path), stat.st_mtime_ns, stat.st_size)

    def _mark_imported(self, path, attribute: str):
        """Un archivo importado a mano (o por un test) no debe volver a importarse en el
        sondeo siguiente: se sella con el mismo stamp que usa `_poll_proposal`."""
        stamp = self._stamp_of(Path(path))
        if stamp is not None:
            setattr(self, attribute, stamp)

    def _master_path(self) -> Path | None:
        if self.result:
            return Path(self.result["master"])
        root = Path(self.output_entry.get().strip()) / "editorial"
        candidates = list(root.glob("*.editorial.master.json")) if root.is_dir() else []
        return candidates[0] if len(candidates) == 1 else None

    def _show_text(self, title: str, path: Path):
        if not path.is_file():
            messagebox.showerror("No disponible", f"No existe {path}")
            return
        window = ctk.CTkToplevel(self.f)
        window.title(title)
        window.geometry("900x650")
        window.grid_columnconfigure(0, weight=1)
        window.grid_rowconfigure(0, weight=1)
        textbox = ctk.CTkTextbox(window, wrap="word")
        textbox.grid(row=0, column=0, sticky="nsew", padx=12, pady=12)
        textbox.insert("1.0", path.read_text(encoding="utf-8"))
        textbox.configure(state="disabled")

    def _show_conversation(self):
        master = self._master_path()
        if master:
            self._show_text("Conversación global", master.parent / "views" / "conversation.md")

    def _review_chunks(self):
        master = self._master_path()
        if master:
            self._background(lambda: self.events.put({"tipo": "review_ready", "path": master,
                                                      "master": read_json(master)}))

    def _import_external_json(self):
        """Un solo botón para los dos contratos: el schema del archivo decide."""
        master = self._master_path()
        if not master:
            return
        path = dialogs.open_file("JSON generado por la AI (bloques o recortes)",
                                 [("JSON", ["*.json"]), ("Todos", ["*"])],
                                 remember="editorial_chunks")
        if not path:
            return
        try:
            schema = read_json(path).get("schema")
        except Exception as error:
            messagebox.showerror("JSON inválido", str(error))
            return
        if schema == editorial_trims.SCHEMA_PROPOSAL:
            self._import_trims(Path(path))
        elif schema == "editorial-layers-proposal/1":
            self._import_layers(Path(path))
        elif schema == "editorial-topics-proposal/1":
            self._import_topics(Path(path))
        else:
            self._import_plan(Path(path))

    def _refresh_plan_buttons(self):
        ready = bool(self.result)
        self.view_button.configure(state="normal" if ready else "disabled")
        self.agent_button.configure(state="normal" if ready else "disabled")
        for button in (self.chunks_button, self.accept_button):
            button.configure(state="normal" if ready and self.plan else "disabled")
        self.silence_button.configure(state="normal" if ready else "disabled")
        has_trims = ready and self.trims is not None
        enabled = editorial_trims.stats(self.trims)["enabled"] if has_trims else 0
        self.trim_export_button.configure(state="normal" if enabled else "disabled")
        ai_ready = ready and self.layers.store is not None
        for button in (self.ai_button, self.ai_arrow):
            button.configure(state="normal" if ai_ready else "disabled")
        self._refresh_cycle_label()

    def _background(self, work, *, label=None):
        if self.worker and self.worker.is_alive():
            return
        self.cancel = threading.Event()
        self._worker_label = label
        self._set_processing(True)
        self.run_button.configure(text="Cancelar", state="normal")
        def guarded():
            try:
                work()
            except Exception as error:
                self.events.put({"tipo": "ui_error", "error": str(error)})
        self.worker = threading.Thread(target=guarded, daemon=True)
        self.worker.start()
        self._refresh_child_mode()             # en un hijo, «Cancelar» se ve mientras trabaja

    def _import_plan(self, path, *, reuse_proposal=False):
        master = self._master_path()
        if master:
            self._mark_imported(path, "_last_plan_stamp")
            plan_before = copy.deepcopy(self.plan)
            self.plan = None
            self.editor.refrescar_layout()
            def work():
                plan = editorial_pipeline.apply_agent_chunks(master, path, reuse_proposal=reuse_proposal)
                self.events.put({"tipo": "plan_loaded", "plan": plan, "history": "importar bloques",
                                 "plan_before": plan_before})
            self._background(work, label="import:plan")

    def _plan_reviewed(self):
        """«Revisar bloques» guardó el plan: recargarlo y registrarlo en el historial."""
        before = copy.deepcopy(self.plan)
        self._load_saved_plan()
        after = self.plan
        if after is not None and before != after:
            self.layers.history.record("revisar bloques", {"plan": before}, {"plan": after},
                                       {"plan": self.layers._doc_revision("plan")})

    def _load_saved_plan(self):
        master = self._master_path()
        path = master.parent / "views" / "chunks.json" if master else None
        try:
            self.plan = read_json(path) if path and path.is_file() else None
        except (ValueError, OSError) as error:
            self.plan = None
            self._append_log(f"No se pudo cargar la propuesta: {error}")
        self.editor.refrescar_layout()
        self._refresh_plan_buttons()

    def _open_project(self):
        path = dialogs.open_file("Master editorial del proyecto", [("JSON", ["*.json"])],
                                 remember="editorial_project")
        if not path:
            return
        self._load_project(path)

    def _choose_discovered(self, candidates):
        window=ctk.CTkToplevel(self.f)
        window.title("Varias versiones de metadata para este clip")
        window.geometry("750x300")
        for candidate in candidates:
            def choose(c=candidate):
                window.destroy()
                self._load_project(c['path'])
            ctk.CTkButton(window,text=candidate['name']+' · '+candidate['path'],command=choose).pack(
                fill="x",padx=12,pady=6)

    def _load_project(self, path):
        # El medio debe estar cargado: la comparación por fingerprint permite mover
        # carpetas entre Windows y Linux sin confiar en rutas absolutas antiguas.
        if not self.info:
            messagebox.showwarning("Falta el medio", "Importa primero el video de este proyecto.")
            return
        source = self.info["path"]
        generation=self.editor._gen
        def work():
            master = read_json(path)
            if master.get("schema") != "editorial-master/1":
                raise ValueError("El archivo no es un master editorial.")
            if not podcast_export.source_matches(master, source, medios.inspeccionar(source)):
                raise ValueError("El proyecto pertenece a otro video.")
            saved = Path(path).parent / "views" / "chunks.json"
            plan = editorial_chunks.validate_plan(read_json(saved), master) if saved.is_file() else None
            self.events.put({"tipo": "project_loaded", "plan": plan,
                             "generation":generation, "derived": bool(master.get("derivation")),
                             "tracks": list(master["tracks"].values()),
                             "result": {"master": path, "source": source, "chunk_planner": "external"}})
        self._background(work, label="load:project")

    def _accept_cuts(self):
        if not self.plan or not self.info:
            return
        master, plan, source = self._master_path(), self.plan, self.info["path"]
        fmt = self._export_format()
        output = dialogs.open_dir("Carpeta para los videos cortados", remember="podcast_exports")
        if not output:
            return
        self.progress.set(0)
        self.pipeline_title.configure(text="Exportando cortes…")
        def work():
            with editorial_pipeline._RunLock(master.parent / ".work"):
                destination = podcast_export.export_plan(master, plan, source, output, fmt=fmt,
                    cancel=self.cancel, progress_cb=lambda fraction: self.events.put(
                        {"tipo": "overall", "fraction": fraction}),
                    log_cb=lambda message: self.events.put({"tipo": "log", "message": message}))
            self.events.put({"tipo": "export_done", "path": str(destination)})
        self._background(work)

    # ================================================================ recortes ----
    def _trims_file(self) -> Path | None:
        master = self._master_path()
        return master.parent / "views" / "trims.json" if master else None

    def _load_trims_async(self):
        """Carga silenciosa (sin bloquear la UI): el master es grande; el documento de
        recortes se valida contra la identidad del medio y se arma el índice de bordes."""
        master = self._master_path()
        if not master:
            return
        path = master.parent / "views" / "trims.json"

        def work():
            try:
                data = read_json(master)
                duration = float(data["media"]["duration"])
                fingerprint = data["media"].get("fingerprint") or {}
                document = editorial_trims.load_document(path, fingerprint=fingerprint,
                                                         duration=duration)
                if document is None:
                    document = editorial_trims.new_document(fingerprint, duration)
                index = editorial_trims.BoundaryIndex(data)
                import editorial_layers
                store = editorial_layers.LayerStore(master.parent, data)
                self.events.put({"tipo": "layers_loaded", "master": str(master), "store": store})
                self.events.put({"tipo": "trims_loaded", "master": str(master), "path": str(path),
                                 "doc": document, "index": index, "quiet": True})
            except Exception as error:
                self.events.put({"tipo": "log", "message": f"Recortes: {error}"})
        threading.Thread(target=work, daemon=True, name="trims-load").start()

    def _record_layers(self, before: dict, label: str):
        """Registra en el historial las capas que una importación (worker) tocó:
        `before` = snapshots tomados en el hilo de UI antes de lanzar el trabajo."""
        controller = self.layers
        if not controller.store or not before:
            return
        after = {doc: controller._doc_snapshot(doc) for doc in before}
        controller.history.record(label, before, after,
                                  {doc: controller._doc_revision(doc) for doc in before})

    def _on_trims_loaded(self, event: dict):
        master = self._master_path()
        if not master or event.get("master") != str(master):
            return                             # llegó tarde: ya se abrió otro proyecto
        incoming = event["doc"]
        if (self.trims is not None and self.trims_path == Path(event["path"])
                and int(incoming.get("revision") or 0) < int(self.trims.get("revision") or 0)):
            # El worker leyó el documento y el timeline lo editó ANTES de que este evento
            # se procesara (la cola se vacía cada 100 ms): el vivo es más nuevo, no se pisa.
            if not event.get("quiet"):
                self._background_done()
            return
        trims_before = event.get("trims_before")
        self.trims = incoming
        if event.get("history") and trims_before is not None:
            self.layers.history.record(event["history"], {"trims": trims_before}, {"trims": self.trims},
                                       {"trims": self.layers._doc_revision("trims")})
        if str(event.get("history") or "").startswith("importar"):
            self._last_import_error = None
        self.trims_path = Path(event["path"])
        if event.get("index") is not None:
            self._boundary_index = event["index"]
        self.sel_cut = None
        self._drag_cut = None
        if "review_stale" in event:
            self._review_stale = bool(event["review_stale"])
        self._reindex_trims()
        self._refresh_trims_status()
        self.editor.refrescar_layout()
        if not event.get("quiet"):
            self._background_done()
        else:
            self._refresh_plan_buttons()
        if event.get("message"):
            self._append_log(event["message"])

    def _reindex_trims(self):
        cuts = self.trims["cuts"] if self.trims else []
        self._trim_starts = [cut["t_ini"] for cut in cuts]
        prefix = []
        maximum = float("-inf")
        for cut in cuts:
            maximum = max(maximum, cut["t_fin"])
            prefix.append(maximum)
        self._trim_pme = prefix
        self._skip_intervals = editorial_trims.enabled_intervals(self.trims)
        self._last_skip = None

    def _refresh_trims_status(self):
        if self.trims is None:
            self.trims_status.configure(text="Sin recortes. Analiza silencios o arrastra en el "
                                             "carril «recortes» del timeline.")
            return
        summary = editorial_trims.stats(self.trims)
        origins = summary["by_origin"]
        text = (f"{summary['total']} recortes · {summary['enabled']} activos · "
                f"{format_time(summary['removed_seconds'])[3:]} a quitar "
                f"(silencio {origins['silence']} · AI {origins['ai']} · tuyos {origins['user']})")
        if self._review_stale:
            text += " · revisión AI desactualizada"
        self.trims_status.configure(text=text)

    def _commit_trims(self, *, message: str | None = None):
        """Persiste tras cada mutación del usuario (JSON chico, escritura atómica)."""
        if self.trims is None or self.trims_path is None:
            return
        editorial_trims.sort_cuts(self.trims)
        try:
            editorial_trims.save_document(self.trims_path, self.trims)
        except Exception as error:
            self.editor.status(f"✗ no pude guardar los recortes: {error}")
        self._review_stale = True
        self._reindex_trims()
        self._refresh_trims_status()
        self._refresh_plan_buttons()
        self.editor._dibujar_timeline()
        if message:
            self.editor.status(message)

    def _silence_params(self) -> dict:
        min_gap = float(self.min_gap_entry.get().strip().replace(",", "."))
        margin = float(self.margin_entry.get().strip().replace(",", "."))
        if min_gap <= 0 or margin < 0:
            raise ValueError("el hueco mínimo debe ser positivo y el margen no negativo")
        return {"min_gap": min_gap, "keep_before": margin, "keep_after": margin}

    def _analyze_silences(self):
        master = self._master_path()
        if not master or not self.info:
            return
        try:
            params = self._silence_params()
        except ValueError as error:
            messagebox.showwarning("Parámetros", f"Revisa mín/margen: {error}")
            return
        path = master.parent / "views" / "trims.json"
        plan = self.plan
        self.progress.set(0)
        self.pipeline_title.configure(text="Analizando silencios…")
        self._append_log(f"Analizando huecos sin voz (mín {params['min_gap']:.1f}s, "
                         f"margen {params['keep_before']:.1f}s)…")

        def work():
            data = read_json(master)
            duration = float(data["media"]["duration"])
            fingerprint = data["media"].get("fingerprint") or {}
            document = editorial_trims.load_document(path, fingerprint=fingerprint, duration=duration)
            if document is None:
                document = editorial_trims.new_document(fingerprint, duration)
            audio = {track_id: master.parent / "tracks" / track_id / "audio.flac"
                     for track_id in data["tracks"]}
            if data.get("derivation"):
                from editorial_projects import ensure_track_audio
                audio = ensure_track_audio(master, self.info["path"], cancel=self.cancel)
            analysis = editorial_trims.analyze_silences(
                data, audio_paths=audio, params=params, cancel=self.cancel,
                progress_cb=lambda fraction: self.events.put({"tipo": "overall", "fraction": fraction}),
                log_cb=lambda message: self.events.put({"tipo": "log", "message": message}))
            trims_before = copy.deepcopy(document)
            editorial_trims.apply_silence_analysis(document, analysis)
            editorial_trims.save_document(path, document)
            editorial_trims.write_review_package(master.parent, data, plan, document)
            index = editorial_trims.BoundaryIndex(data)
            summary = analysis["stats"]
            self.events.put({"tipo": "trims_loaded", "master": str(master), "path": str(path),
                             "doc": document, "index": index, "quiet": False, "review_stale": False,
                             "history": "analizar silencios", "trims_before": trims_before,
                             "message": (f"Silencios: {summary['cuts']} recortes propuestos, "
                                         f"{summary['enabled']} activos. Nada se cortó: revisa el "
                                         "carril «recortes» y ajusta con el mouse.")})
        self._background(work)

    def _import_trims(self, path):
        master = self._master_path()
        if not master:
            return
        self._mark_imported(path, "_last_trims_stamp")
        trims_path = master.parent / "views" / "trims.json"
        plan = self.plan

        def work():
            data = read_json(master)
            trims_before = editorial_trims.load_document(
                trims_path, fingerprint=data["media"].get("fingerprint"),
                duration=float(data["media"]["duration"]))
            document, proposal = editorial_trims.import_proposal(
                trims_path, path, data, plan, fingerprint=data["media"].get("fingerprint"))
            index = editorial_trims.BoundaryIndex(data)
            flagged = sum(1 for cut in proposal["cuts"] if cut["warnings"])
            self.events.put({"tipo": "trims_loaded", "master": str(master), "path": str(trims_path),
                             "doc": document, "index": index, "quiet": False,
                             "history": "importar recortes de la AI", "trims_before": trims_before,
                             "message": (f"Propuesta de la AI ({proposal['planner']}): "
                                         f"{len(proposal['cuts'])} recortes de contenido"
                                         + (f", {flagged} con avisos" if flagged else "")
                                         + ". Aparecen en violeta; revísalos antes de cortar.")})
        self._background(work, label="import:trims")

    def _prepare_review(self):
        """«Preparar para la AI → Solo recortes»: el paquete de la Tarea 2."""
        master = self._master_path()
        if not master or self.trims is None:
            self._append_log("Para pedir recortes hace falta la metadata y el documento de recortes "
                             "(Analizar silencios lo crea).")
            return
        plan, document = self.plan, self.trims
        digest = self.layers.snapshot()["source_layers_digest"] if self.layers.store else None

        def work():
            data = read_json(master)
            paths = editorial_trims.write_review_package(master.parent, data, plan, document,
                                                         layers_digest=digest)
            self.events.put({"tipo": "review_written", "request": paths["request"]})
        self._background(work, label="prepare:trims")

    def _export_trims(self):
        if not self.info or self.trims is None:
            return
        if not editorial_trims.stats(self.trims)["enabled"]:
            messagebox.showinfo("Sin recortes activos", "Activa o crea al menos un recorte.")
            return
        fmt = self._export_format()
        if fmt == "copy":
            messagebox.showinfo("Copia exacta", "La copia sin recodificar no puede aplicar recortes: "
                                "elige otro formato de salida, o usa «Aceptar y exportar cortes» "
                                "para partir en bloques sin recortar.")
            return
        master, plan, trims, source = self._master_path(), self.plan, self.trims, self.info["path"]
        output = dialogs.open_dir("Carpeta para los videos recortados", remember="podcast_exports")
        if not output:
            return
        self.progress.set(0)
        self.pipeline_title.configure(text="Cortando y exportando…")
        summary = editorial_trims.stats(trims)
        self._append_log(f"Cortando {summary['enabled']} recortes "
                         f"({format_time(summary['removed_seconds'])}) "
                         + (f"en {len(plan['chunks'])} bloques…" if plan else "sobre el video completo…"))

        def work():
            with editorial_pipeline._RunLock(master.parent / ".work"):
                destination = podcast_export.export_plan(
                    master, plan, source, output, trims=trims, fmt=fmt, cancel=self.cancel,
                    progress_cb=lambda fraction: self.events.put({"tipo": "overall", "fraction": fraction}),
                    log_cb=lambda message: self.events.put({"tipo": "log", "message": message}))
            self.events.put({"tipo": "export_done", "path": str(destination)})
        self._background(work)

    # ---- carriles: bloques (read-only) + recortes (interactivo) ----
    def _cut_lanes(self):
        return self.layers.lanes()

    def _import_layers(self, path):
        if not self.layers.store:
            return
        self._mark_imported(path, "_last_layers_stamp")
        snapshot = self.layers.snapshot()
        proposal = read_json(path)
        ids = [layer.get("layer_id") for layer in
               ([proposal.get("layer")] if proposal.get("layer") else []) + list(proposal.get("layers") or [])
               if isinstance(layer, dict) and layer.get("layer_id")]
        before = {f"layer:{identifier}": self.layers._doc_snapshot(f"layer:{identifier}") for identifier in ids}
        def work():
            editorial_layers.merge_response(self.layers.store, proposal, snapshot)
            self.events.put({"tipo": "layers_imported", "before": before})
        self._background(work, label="import:layers")

    def _prepare_topics(self):
        """«Preparar para la AI → Solo temas»: Tarea 3 (ámbito = bloque seleccionado)."""
        if not self.layers.store or not self._master_path():
            self._append_log("Para analizar temas, abre o genera primero la metadata del medio.")
            return
        import editorial_topics
        scope = None
        if self.layers.selected and self.layers.selected[0] == "bloques" and self.layers.selected[1]:
            _, item = self.layers.find(*self.layers.selected[:2])
            scope = item["ranges"][0]
        snapshot = self.layers.snapshot()
        editorial_topics.prepare(self.layers.store.root,self.layers.store.master,snapshot,scope=scope)
        self._last_topics_stamp = None
        self._append_log("Tarea 3 lista: views/topics-agent-request.md. "
                         + ("Ámbito: bloque seleccionado." if scope else "Ámbito: medio completo.")
                         + " Pide a la AI ambas pasadas; la app valida el mapa entre ellas.")
        self._refresh_cycle_label()

    def _prepare_editorial(self):
        """«Preparar para la AI» (por defecto): temas (pasada 1) + paquete de revisión
        de recortes + views/editorial-agent-request.md con el orden de la Tarea 4."""
        master = self._master_path()
        if not self.layers.store or not master:
            self._append_log("Para preparar la revisión hace falta la metadata del medio.")
            return
        if self.trims is None:
            self._append_log("Todavía no hay documento de recortes: pulsa Analizar silencios primero "
                             "(o usa «Solo temas»).")
            return
        import editorial_topics
        snapshot = self.layers.snapshot()
        root = self.layers.store.root
        request = editorial_topics.prepare(root, self.layers.store.master, snapshot)
        self._last_topics_stamp = None
        plan, document = self.plan, self.trims
        digest = snapshot["source_layers_digest"]

        def work():
            data = read_json(master)
            editorial_trims.write_review_package(root, data, plan, document, layers_digest=digest)
            path = editorial_trims.write_editorial_request(root, data, plan, document, request)
            self.events.put({"tipo": "review_written", "request": path,
                             "hint": "Pide a la AI la Tarea 4 de la skill transcriptor (temas en dos "
                                     "pasadas y después recortes); cada respuesta se importa sola."})
        self._review_stale = False
        self._background(work, label="prepare:editorial")

    def _import_topics(self, path):
        if not self.layers.store:
            return
        import editorial_topics
        self._mark_imported(path, "_last_topics_stamp")
        snapshot = self.layers.snapshot()
        store = self.layers.store
        before = {}
        try:
            request = read_json(store.root / "views" / "topics-request.json")
            doc = "layer:" + (request.get("layer_id") or "")
            before = {doc: self.layers._doc_snapshot(doc)} if request.get("layer_id") else {}
        except (OSError, ValueError):
            pass
        def work():
            result = editorial_topics.import_proposal(store, read_json(path), snapshot)
            self.events.put({"tipo":"topics_imported", "before": before, **result})
        self._background(work, label="import:topics")

    def _on_playhead(self, t: float):
        """Reproducción con «saltar recortes»: al entrar en un recorte activo se re-arranca la
        sesión al final del recorte (una vez por recorte; el re-arranque tarda unos cientos
        de ms — es una ayuda de revisión, no el render)."""
        if not self._skip_intervals or not self.skip_check.get():
            return
        if not self.editor._playback_activo() or self.editor._warmup is not None:
            return
        starts = [start for start, _ in self._skip_intervals]
        index = bisect_right(starts, t) - 1
        if index < 0:
            return
        start, end = self._skip_intervals[index]
        if not (start <= t < end):
            return
        now = time.monotonic()
        if self._last_skip and self._last_skip[0] == index and now - self._last_skip[1] < 1.5:
            return
        self._last_skip = (index, now)
        self.editor._play(reiniciar=True, desde=min(end + 0.02, float(self.info["duracion"])))

    def activar(self):
        self.editor.activar()

    def desactivar(self):
        self.editor.desactivar()

    def cerrar(self):
        self.cancel.set()
        self.editor.cerrar()
