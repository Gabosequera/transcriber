"""Workspace Automático funcional para la Fase 1 editorial."""
from __future__ import annotations

import queue
import threading
import time
from bisect import bisect_left, bisect_right
from pathlib import Path
from tkinter import messagebox

import customtkinter as ctk

import dialogs
import editorial_chunks
import editorial_pipeline
import editorial_trims
import hardware
import podcast_export
import medios
from editorial_io import format_time, parse_time, read_json
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


class ChunkReviewDialog(ctk.CTkToplevel):
    def __init__(self, parent, master_path: Path, on_saved=None, *, master=None, document=None):
        super().__init__(parent)
        self.master_path = master_path
        self.root_path = master_path.parent
        self.on_saved = on_saved
        self.editorial_master = master if master is not None else read_json(master_path)
        self.document = document if document is not None else read_json(self.root_path / "views" / "chunks.json")
        self.rows: list[dict] = []
        self.title("Revisar chunks")
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
        body = ctk.CTkFrame(self.f, fg_color="transparent")
        body.grid(row=1, column=0, sticky="nsew", padx=14, pady=(0, 14))
        body.grid_columnconfigure(0, weight=1)
        body.grid_rowconfigure(0, weight=1)

        self.editor = EditorMedios(body, ancho_ctl=390,
                                   controles_pista_extra=self._build_track_controls,
                                   on_video_cargado=self._on_media_loaded,
                                   carriles_extra=self._cut_lanes,
                                   on_playhead=self._on_playhead,
                                   teclas_extra=self._trim_keys)
        self.editor.f.grid(row=0, column=0, sticky="nsew", padx=(0, 10))
        # hover = tooltip del recorte · click derecho = menú · click en MARCAS deselecciona
        self.editor.tl.bind("<Motion>", self._trim_hover, add=True)
        self.editor.tl.bind("<Button-3>", self._trim_menu, add=True)
        self.editor.tl.bind("<Button-1>", self._after_editor_press, add=True)

        panel = ctk.CTkFrame(body, width=292, fg_color=SURFACE, corner_radius=10,
                             border_width=1, border_color=BORDER)
        panel.grid(row=0, column=1, sticky="nsew")
        panel.grid_propagate(False)
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

        actions = ctk.CTkFrame(panel, fg_color="transparent")
        actions.grid(row=7, column=0, sticky="ew", padx=14, pady=(4, 6))
        actions.grid_columnconfigure((0, 1), weight=1)
        self.view_button = ctk.CTkButton(actions, text="Conversación", height=28,
                                         state="disabled", fg_color=SURFACE_RAISED,
                                         hover_color="#2a322d", command=self._show_conversation)
        self.view_button.grid(row=0, column=0, sticky="ew", padx=(0, 3))
        self.chunks_button = ctk.CTkButton(actions, text="Revisar chunks", height=28,
                                           state="disabled", fg_color=SURFACE_RAISED,
                                           hover_color="#2a322d", command=self._review_chunks)
        self.chunks_button.grid(row=0, column=1, sticky="ew", padx=(3, 0))
        self.agent_button = ctk.CTkButton(panel, text="Importar JSON de la AI", height=28,
                                          state="disabled", fg_color=SURFACE_RAISED,
                                          hover_color="#2a322d", command=self._import_external_json)
        self.agent_button.grid(row=8, column=0, sticky="ew", padx=14, pady=(0, 6))
        self._build_trims_panel(panel, row=9)
        self.accept_button = ctk.CTkButton(panel, text="Aceptar y exportar cortes", height=32,
                                           state="disabled", fg_color=ACCENT,
                                           command=self._accept_cuts)
        self.accept_button.grid(row=10, column=0, sticky="ew", padx=14, pady=(0, 6))
        self.open_project_button = ctk.CTkButton(panel, text="Abrir proyecto existente", height=28,
                                                fg_color=SURFACE_RAISED,
                                                command=self._open_project)
        self.open_project_button.grid(row=11, column=0, sticky="ew", padx=14, pady=(0, 6))
        self.run_button = ctk.CTkButton(panel, text="Procesar pistas de voz", height=38,
                                        state="disabled", fg_color=ACCENT,
                                        hover_color=ACCENT_HOVER, command=self._run_or_cancel,
                                        font=ctk.CTkFont(size=13, weight="bold"))
        self.run_button.grid(row=12, column=0, sticky="ew", padx=14, pady=(0, 14))

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
                                         wraplength=246, font=ctk.CTkFont(size=10))
        self.trims_status.grid(row=2, column=0, sticky="ew", padx=10, pady=(0, 4))
        buttons = ctk.CTkFrame(box, fg_color="transparent")
        buttons.grid(row=3, column=0, sticky="ew", padx=10, pady=(0, 4))
        buttons.grid_columnconfigure((0, 1), weight=1)
        self.review_button = ctk.CTkButton(buttons, text="Preparar revisión AI", height=26,
                                           state="disabled", fg_color="#4a3a5e",
                                           hover_color="#5a4772", command=self._prepare_review)
        self.review_button.grid(row=0, column=0, sticky="ew", padx=(0, 3))
        self.trim_export_button = ctk.CTkButton(buttons, text="✂ Cortar y exportar", height=26,
                                                state="disabled", fg_color="#8a5a24",
                                                hover_color="#a06a2b", command=self._export_trims)
        self.trim_export_button.grid(row=0, column=1, sticky="ew", padx=(3, 0))
        self.skip_check = ctk.CTkCheckBox(box, text="Saltar recortes al reproducir", height=20,
                                          checkbox_width=14, checkbox_height=14,
                                          text_color="#c6cec9", font=ctk.CTkFont(size=10))
        self.skip_check.grid(row=4, column=0, sticky="w", padx=10, pady=(0, 6))

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
        self.pipeline_title.configure(text="Selecciona las pistas de voz")
        self.run_button.configure(state="normal")
        for button in (self.view_button, self.chunks_button, self.agent_button, self.accept_button,
                       self.silence_button, self.review_button, self.trim_export_button):
            button.configure(state="disabled")

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
                       self.silence_button, self.review_button, self.trim_export_button):
            button.configure(state="disabled")
        if not active:
            self._refresh_plan_buttons()
        for widget in self.track_widgets:
            widget["selected"].configure(state=state)
            widget["label"].configure(state=state)

    def _background_done(self):
        self._set_processing(False)
        self.run_button.configure(text="Reanudar / actualizar", state="normal")

    def _run_or_cancel(self):
        if self.worker and self.worker.is_alive():
            self.cancel.set()
            self.run_button.configure(text="Cancelando…", state="disabled")
            return
        if not self.info:
            messagebox.showwarning("Falta el video", "Importa un video o audio primero.")
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
                    self.run_button.configure(text="Reanudar / actualizar", state="normal")
                    self._load_saved_plan()
                    self._refresh_plan_buttons()
                    self._load_trims_async()
                    self._append_log(f"Listo: {self.result['master']}")
                    self._append_log(f"Planificador: {self.result.get('chunk_planner', 'desconocido')}")
                elif kind == "review_ready":
                    self._background_done()
                    self.review_dialog = ChunkReviewDialog(self.f, event["path"],
                        on_saved=self._load_saved_plan, master=event["master"], document=self.plan)
                elif kind == "plan_loaded":
                    self.plan = event["plan"]
                    self._review_stale = bool(self.trims and self.trims["cuts"])
                    self.editor.refrescar_layout()
                    self._background_done()
                    self._refresh_trims_status()
                    self._append_log(f"Propuesta lista: {len(self.plan['chunks'])} bloques. Revisa el carril de colores antes de aceptar.")
                elif kind == "trims_loaded":
                    self._on_trims_loaded(event)
                elif kind == "review_written":
                    self._review_stale = False
                    self._background_done()
                    self._refresh_trims_status()
                    self._append_log("Revisión para la AI lista: " + str(event["request"]))
                    self._append_log("Pide a la AI la Tarea 2 de la skill transcriptor; su "
                                     "trims.proposed.json se importa solo al aparecer.")
                elif kind == "export_done":
                    self._background_done()
                    self.progress.set(1)
                    self._append_log(f"Videos exportados: {event['path']}")
                    self.pipeline_title.configure(text="Cortes exportados")
                elif kind == "project_loaded":
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
                    self.run_button.configure(text="Reanudar / actualizar", state="normal")
                    self._load_trims_async()
                elif kind == "ui_error":
                    cancelled = self.cancel.is_set()
                    self._set_processing(False)
                    self.pipeline_title.configure(text="Cancelado" if cancelled else "Error")
                    self.run_button.configure(text="Reanudar", state="normal")
                    self._append_log(("Cancelado: " if cancelled else "ERROR: ") + event["error"])
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
        self.f.after(100, self._pump)

    def _poll_proposal(self, path: Path, attribute: str, action):
        """La AI escribe su JSON fuera de la app: se importa solo cuando aparece o cambia."""
        try:
            stat = path.stat()
        except OSError:
            return
        stamp = (str(path), stat.st_mtime_ns, stat.st_size)
        if stamp != getattr(self, attribute):
            setattr(self, attribute, stamp)
            action(path)

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
        self.review_button.configure(state="normal" if has_trims else "disabled")
        enabled = editorial_trims.stats(self.trims)["enabled"] if has_trims else 0
        self.trim_export_button.configure(state="normal" if enabled else "disabled")

    def _background(self, work):
        if self.worker and self.worker.is_alive():
            return
        self.cancel = threading.Event()
        self._set_processing(True)
        self.run_button.configure(text="Cancelar", state="normal")
        def guarded():
            try:
                work()
            except Exception as error:
                self.events.put({"tipo": "ui_error", "error": str(error)})
        self.worker = threading.Thread(target=guarded, daemon=True)
        self.worker.start()

    def _import_plan(self, path, *, reuse_proposal=False):
        master = self._master_path()
        if master:
            self.plan = None
            self.editor.refrescar_layout()
            def work():
                plan = editorial_pipeline.apply_agent_chunks(master, path, reuse_proposal=reuse_proposal)
                self.events.put({"tipo": "plan_loaded", "plan": plan})
            self._background(work)

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
        # El medio debe estar cargado: la comparación por fingerprint permite mover
        # carpetas entre Windows y Linux sin confiar en rutas absolutas antiguas.
        if not self.info:
            messagebox.showwarning("Falta el medio", "Importa primero el video de este proyecto.")
            return
        source = self.info["path"]
        def work():
            master = read_json(path)
            if master.get("schema") != "editorial-master/1":
                raise ValueError("El archivo no es un master editorial.")
            if not podcast_export.source_matches(master, source, medios.inspeccionar(source)):
                raise ValueError("El proyecto pertenece a otro video.")
            saved = Path(path).parent / "views" / "chunks.json"
            plan = editorial_chunks.validate_plan(read_json(saved), master) if saved.is_file() else None
            self.events.put({"tipo": "project_loaded", "plan": plan,
                             "tracks": list(master["tracks"].values()),
                             "result": {"master": path, "source": source, "chunk_planner": "external"}})
        self._background(work)

    def _accept_cuts(self):
        if not self.plan or not self.info:
            return
        master, plan, source = self._master_path(), self.plan, self.info["path"]
        output = dialogs.open_dir("Carpeta para los videos cortados", remember="podcast_exports")
        if not output:
            return
        self.progress.set(0)
        self.pipeline_title.configure(text="Exportando cortes…")
        def work():
            with editorial_pipeline._RunLock(master.parent / ".work"):
                destination = podcast_export.export_plan(master, plan, source, output,
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
                self.events.put({"tipo": "trims_loaded", "master": str(master), "path": str(path),
                                 "doc": document, "index": index, "quiet": True})
            except Exception as error:
                self.events.put({"tipo": "log", "message": f"Recortes: {error}"})
        threading.Thread(target=work, daemon=True, name="trims-load").start()

    def _on_trims_loaded(self, event: dict):
        master = self._master_path()
        if not master or event.get("master") != str(master):
            return                             # llegó tarde: ya se abrió otro proyecto
        self.trims = event["doc"]
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
            analysis = editorial_trims.analyze_silences(
                data, audio_paths=audio, params=params, cancel=self.cancel,
                progress_cb=lambda fraction: self.events.put({"tipo": "overall", "fraction": fraction}),
                log_cb=lambda message: self.events.put({"tipo": "log", "message": message}))
            editorial_trims.apply_silence_analysis(document, analysis)
            editorial_trims.save_document(path, document)
            editorial_trims.write_review_package(master.parent, data, plan, document)
            index = editorial_trims.BoundaryIndex(data)
            summary = analysis["stats"]
            self.events.put({"tipo": "trims_loaded", "master": str(master), "path": str(path),
                             "doc": document, "index": index, "quiet": False, "review_stale": False,
                             "message": (f"Silencios: {summary['cuts']} recortes propuestos, "
                                         f"{summary['enabled']} activos. Nada se cortó: revisa el "
                                         "carril «recortes» y ajusta con el mouse.")})
        self._background(work)

    def _import_trims(self, path):
        master = self._master_path()
        if not master:
            return
        trims_path = master.parent / "views" / "trims.json"
        plan = self.plan

        def work():
            data = read_json(master)
            document, proposal = editorial_trims.import_proposal(
                trims_path, path, data, plan, fingerprint=data["media"].get("fingerprint"))
            index = editorial_trims.BoundaryIndex(data)
            flagged = sum(1 for cut in proposal["cuts"] if cut["warnings"])
            self.events.put({"tipo": "trims_loaded", "master": str(master), "path": str(trims_path),
                             "doc": document, "index": index, "quiet": False,
                             "message": (f"Propuesta de la AI ({proposal['planner']}): "
                                         f"{len(proposal['cuts'])} recortes de contenido"
                                         + (f", {flagged} con avisos" if flagged else "")
                                         + ". Aparecen en violeta; revísalos antes de cortar.")})
        self._background(work)

    def _prepare_review(self):
        master = self._master_path()
        if not master or self.trims is None:
            return
        plan, document = self.plan, self.trims

        def work():
            data = read_json(master)
            paths = editorial_trims.write_review_package(master.parent, data, plan, document)
            self.events.put({"tipo": "review_written", "request": paths["request"]})
        self._background(work)

    def _export_trims(self):
        if not self.info or self.trims is None:
            return
        if not editorial_trims.stats(self.trims)["enabled"]:
            messagebox.showinfo("Sin recortes activos", "Activa o crea al menos un recorte.")
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
                    master, plan, source, output, trims=trims, cancel=self.cancel,
                    progress_cb=lambda fraction: self.events.put({"tipo": "overall", "fraction": fraction}),
                    log_cb=lambda message: self.events.put({"tipo": "log", "message": message}))
            self.events.put({"tipo": "export_done", "path": str(destination)})
        self._background(work)

    # ---- carriles: bloques (read-only) + recortes (interactivo) ----
    def _cut_lanes(self):
        lanes = []
        if self.plan:
            lanes.append({"alto": 36, "dibujar": self._draw_cuts})
        if self.trims is not None:
            lanes.append({"nombre": "recortes", "alto": TRIMS_H, "dibujar": self._draw_trims,
                          "gesto": self._trim_gesture})
        return lanes

    def _draw_cuts(self, canvas, geometry, y):
        x0, width = geometry
        view_start, span = self.editor.view
        colors = ("#286f59", "#375c8c", "#78578c", "#926e34", "#397a83", "#8c515a")
        for index, chunk in enumerate(self.plan["chunks"]):
            if chunk["t_fin"] <= view_start or chunk["t_ini"] >= view_start + span:
                continue
            left = max(x0, self.editor._t2x(chunk["t_ini"], geometry))
            right = min(x0 + width, self.editor._t2x(chunk["t_fin"], geometry))
            canvas.create_rectangle(left, y + 2, right, y + 34,
                                    fill=colors[index % len(colors)], outline="#cbd6d0")
            if right - left > 45:
                label = f"{index + 1}. {chunk['title']}"
                canvas.create_text(left + 5, y + 18, text=label[:max(1, int((right-left-10)/7))],
                                   anchor="w", fill="white", font=("TkDefaultFont", 10))
            if index and chunk["t_ini"] >= view_start:
                canvas.create_line(left, 0, left, y + 34, fill="#ffd27a", dash=(4, 3), width=2)

    def _visible_cuts(self, start: float, end: float) -> list[dict]:
        cuts = self.trims["cuts"] if self.trims else []
        low = bisect_right(self._trim_pme, start)
        high = bisect_left(self._trim_starts, end)
        visible = [cut for cut in cuts[low:high] if cut["t_fin"] > start]
        dragging = (self._drag_cut or {}).get("cut")
        if dragging is not None and dragging not in visible and dragging["t_fin"] > start \
                and dragging["t_ini"] < end:
            visible.append(dragging)           # el índice no sigue al drag en vivo
        return visible

    def _draw_trims(self, canvas, geometry, y):
        x0, width = geometry
        view_start, span = self.editor.view
        view_end = view_start + span
        y1 = y + TRIMS_H
        bottom = self.editor._alto_total()
        canvas.create_rectangle(x0, y, x0 + width, y1, fill="#141917", outline="#26302a")
        canvas.create_text(x0 + 3, y + TRIMS_H / 2, text="recortes", anchor="w", fill="#4f5f56",
                           font=("TkDefaultFont", 7))
        if self.trims is None:
            return
        cuts = self._visible_cuts(view_start, view_end)
        drag = self._drag_cut
        if drag and drag["mode"] == "create":
            a, b = sorted((drag["t0"], drag["t1"]))
            xa, xb = self.editor._t2x(max(a, view_start), geometry), self.editor._t2x(min(b, view_end), geometry)
            canvas.create_rectangle(xa, y + 3, max(xb, xa + 1), y1 - 3, fill="",
                                    outline=COL_TRIM["user"], dash=(3, 2))
        if len(cuts) > TRIM_LOD_MAX:
            # LOD denso: cobertura por píxel (activos y desactivados por separado), cero
            # items por recorte; el hit-test sigue contra el documento, no contra items.
            for enabled in (True, False):
                covered = bytearray(int(width) + 1)
                for cut in cuts:
                    if cut["enabled"] != enabled:
                        continue
                    pa = int((max(cut["t_ini"], view_start) - view_start) / span * width)
                    pb = int((min(cut["t_fin"], view_end) - view_start) / span * width)
                    for px in range(max(0, pa), min(int(width), pb + 1)):
                        covered[px] = 1
                px = 0
                while px <= int(width):
                    if covered[px]:
                        first = px
                        while px <= int(width) and covered[px]:
                            px += 1
                        canvas.create_rectangle(x0 + first, y + 4, x0 + px, y1 - 4,
                                                fill=COL_TRIM_LOD[enabled], outline="")
                        if enabled:
                            canvas.create_rectangle(x0 + first, y1, x0 + px, bottom,
                                                    fill=COL_TRIM_LOD[True], stipple="gray12",
                                                    outline="")
                    px += 1
            return
        for cut in cuts:
            xa = self.editor._t2x(max(cut["t_ini"], view_start), geometry)
            xb = max(self.editor._t2x(min(cut["t_fin"], view_end), geometry), xa + 2)
            color = COL_TRIM[cut["origin"]]
            selected = cut is self.sel_cut
            outline = "#ffffff" if selected else color
            if cut["enabled"]:
                canvas.create_rectangle(xa, y + 3, xb, y1 - 3, fill=color, outline=outline,
                                        width=2 if selected else 1)
                # proyección sobre las pistas: lo que se va (un item con stipple)
                canvas.create_rectangle(xa, y1, xb, bottom, fill=color, stipple="gray12", outline="")
            else:
                canvas.create_rectangle(xa, y + 3, xb, y1 - 3, fill="#1c211e", outline=outline,
                                        width=2 if selected else 1, dash=() if selected else (3, 2))
            if self._boundary_index is not None:
                for xe, timestamp in ((xa, cut["t_ini"]), (xb, cut["t_fin"])):
                    hit = self._boundary_index.conflicts(timestamp)
                    if hit["words"] or hit["laughter"]:
                        canvas.create_line(xe, y + 1, xe, y1 - 1, fill="#ff5252", width=2)
            if xb - xa > 34:
                canvas.create_text((xa + xb) / 2, (y + y1) / 2,
                                   text=f"{cut['t_fin'] - cut['t_ini']:.1f}s",
                                   fill=TEXT if cut["enabled"] else MUTED, font=("TkDefaultFont", 8))
            if selected:
                for xh in (xa, xb):
                    canvas.create_rectangle(xh - 2, y + 2, xh + 2, y1 - 2, fill="#ffffff", outline="")

    def _trim_hit(self, x, geometry):
        """(recorte, modo) bajo el cursor: bordes del seleccionado (±4 px) primero, después
        el seleccionado, después el más angosto — mismo criterio que las marcas."""
        view_start, span = self.editor.view
        visible = self._visible_cuts(view_start, view_start + span)
        boxes = []
        for cut in visible:
            xa = self.editor._t2x(max(cut["t_ini"], view_start), geometry)
            xb = max(self.editor._t2x(min(cut["t_fin"], view_start + span), geometry), xa + 2)
            boxes.append((cut, xa, xb))
        for cut, xa, xb in boxes:
            if cut is self.sel_cut:
                if abs(x - xa) <= 4:
                    return cut, "edge_start"
                if abs(x - xb) <= 4:
                    return cut, "edge_end"
        hits = [(cut is not self.sel_cut, xb - xa, cut) for cut, xa, xb in boxes if xa - 2 <= x <= xb + 2]
        if not hits:
            return None
        hits.sort(key=lambda item: (item[0], item[1]))
        return hits[0][2], "move"

    def _select_cut(self, cut):
        self.sel_cut = cut
        if cut is None:
            return
        self.editor._seleccionar(None)         # una sola selección visible a la vez
        self._describe_cut(cut)

    def _describe_cut(self, cut):
        text = (f"✂ {cut['cut_id']} · {TRIM_ORIGIN_LABEL[cut['origin']]} · "
                f"{format_time(cut['t_ini'])[3:-1]}–{format_time(cut['t_fin'])[3:-1]} "
                f"({cut['t_fin'] - cut['t_ini']:.1f} s) · {'ACTIVO' if cut['enabled'] else 'desactivado'}"
                " · X activa/desactiva · Supr borra · arrastra bordes o el cuerpo")
        if cut.get("reason"):
            text += f"\n{cut['reason'][:160]}"
        if self._boundary_index is not None:
            warnings = self._boundary_index.cut_warnings(cut)
            if warnings:
                text += "\n⚠ " + "; ".join(warnings)
        self.editor.status(text)

    def _trim_gesture(self, phase, e, geometry, y0):
        if self.trims is None or geometry is None:
            return False
        if phase == "press":
            t = self.editor._x2t(e.x, geometry)
            hit = self._trim_hit(e.x, geometry)
            if hit:
                cut, mode = hit
                self._select_cut(cut)
                self._drag_cut = {"mode": mode, "cut": cut, "base": (dict(cut), t), "moved": False}
            else:
                self._select_cut(None)
                self._drag_cut = {"mode": "create", "t0": t, "t1": t, "moved": False}
            self.editor._dibujar_timeline()
            return True
        if phase == "motion":
            drag = self._drag_cut
            if not drag:
                return True
            duration = self.info["duracion"] if self.info else float(self.trims["duration"])
            t = min(max(self.editor._x2t(e.x, geometry), 0.0), duration)
            drag["moved"] = True
            if drag["mode"] == "create":
                drag["t1"] = t
            else:
                cut = drag["cut"]
                original, t_start = drag["base"]
                minimum = editorial_trims.MIN_CUT_SECONDS
                if drag["mode"] == "move":
                    width = original["t_fin"] - original["t_ini"]
                    start = min(max(0.0, original["t_ini"] + (t - t_start)), duration - width)
                    cut["t_ini"], cut["t_fin"] = round(start, 3), round(start + width, 3)
                elif drag["mode"] == "edge_start":
                    cut["t_ini"] = round(min(t, cut["t_fin"] - minimum), 3)
                else:
                    cut["t_fin"] = round(max(t, cut["t_ini"] + minimum), 3)
            self.editor._dibujar_timeline()
            return True
        if phase == "release":
            drag, self._drag_cut = self._drag_cut, None
            if not drag:
                return True
            if drag["mode"] == "create":
                pixels = abs(drag["t1"] - drag["t0"]) * (geometry[1] / max(self.editor.view[1], 1e-9))
                if drag["moved"] and pixels >= 4:
                    try:
                        cut = editorial_trims.add_cut(self.trims, drag["t0"], drag["t1"],
                                                      origin="user", reason="Recorte manual.")
                    except ValueError as error:
                        self.editor.status(f"⚠ {error}")
                        self.editor._dibujar_timeline()
                        return True
                    self._select_cut(cut)
                    self._commit_trims()
                    self._describe_cut(cut)
                else:
                    self.editor._dibujar_timeline()
                return True
            cut = drag["cut"]
            if drag["moved"]:
                original = drag["base"][0]
                if cut["t_fin"] - cut["t_ini"] < editorial_trims.MIN_CUT_SECONDS:
                    cut.clear()
                    cut.update(original)
                    self.editor.status("⚠ recorte demasiado corto — volvió a su estado anterior.")
                else:
                    cut["edited"] = True
                self._commit_trims()
                self._describe_cut(cut)
            return True
        if phase == "doble":
            hit = self._trim_hit(e.x, geometry)
            if hit:
                self._toggle_cut(hit[0])
            return True
        return False

    def _toggle_cut(self, cut):
        cut["enabled"] = not cut["enabled"]
        cut["edited"] = True
        self._select_cut(cut)
        self._commit_trims()
        self._describe_cut(cut)

    def _delete_cut(self, cut):
        editorial_trims.remove_cut(self.trims, cut)
        if self.sel_cut is cut:
            self.sel_cut = None
        self._commit_trims(message=f"🗑 {cut['cut_id']} borrado.")

    def _trim_keys(self, e) -> bool:
        if self.sel_cut is None or self.trims is None or self.editor.sel_marca is not None:
            return False
        if e.keysym in ("Delete", "BackSpace"):
            self._delete_cut(self.sel_cut)
            return True
        if e.keysym == "x":
            self._toggle_cut(self.sel_cut)
            return True
        if e.keysym == "Escape":
            self.sel_cut = None
            self.editor._dibujar_timeline()
            return True
        return False

    def _after_editor_press(self, e):
        """Click en el carril de MARCAS (lo atiende el editor): el recorte deja de estar
        seleccionado para que Supr/X no actúen sobre dos cosas."""
        from editor_medios import MARKS_H, RULER_H
        if self.sel_cut is not None and RULER_H <= e.y <= RULER_H + MARKS_H:
            self.sel_cut = None
            self.editor._dibujar_timeline()

    def _trims_lane_at(self, y) -> tuple[dict, int] | None:
        hit = self.editor._carril_en(y)
        # por NOMBRE: un método ligado es un objeto nuevo en cada acceso (`is` fallaría)
        if hit and hit[0].get("nombre") == "recortes":
            return hit
        return None

    def _trim_hover(self, e):
        tl = self.editor.tl
        for item in self._tt_items:
            try:
                tl.delete(item)
            except Exception:
                pass
        self._tt_items = []
        geometry = self.editor._tl_geo()
        if not geometry or self.trims is None or self._drag_cut is not None:
            return
        if self._trims_lane_at(e.y) is None:
            return
        hit = self._trim_hit(e.x, geometry)
        if not hit:
            return
        cut = hit[0]
        text = (f"{cut['cut_id']} · {TRIM_ORIGIN_LABEL[cut['origin']]}"
                f"{'' if cut['enabled'] else ' · DESACTIVADO'} · "
                f"{format_time(cut['t_ini'])[3:-1]}–{format_time(cut['t_fin'])[3:-1]} "
                f"({cut['t_fin'] - cut['t_ini']:.1f} s)")
        if cut.get("reason"):
            text += " · " + cut["reason"][:110]
        x = min(e.x + 10, max(tl.winfo_width() - 330, 10))
        text_id = tl.create_text(x, e.y - 14, text=text[:170], anchor="w", fill="#eee",
                                 font=("TkDefaultFont", 9), width=330)
        box = tl.bbox(text_id)
        if box:
            rect_id = tl.create_rectangle(box[0] - 4, box[1] - 2, box[2] + 4, box[3] + 2,
                                          fill="#262626", outline="#444")
            tl.tag_lower(rect_id, text_id)
            self._tt_items = [rect_id, text_id]
        else:
            self._tt_items = [text_id]

    def _trim_menu(self, e):
        geometry = self.editor._tl_geo()
        if not geometry or self.trims is None or self._trims_lane_at(e.y) is None:
            return
        import tkinter as tk
        menu = tk.Menu(self.editor.tl, tearoff=0, bg="#222", fg="#ddd", activebackground="#2e6b45")
        hit = self._trim_hit(e.x, geometry)
        if hit:
            cut = hit[0]
            self._select_cut(cut)
            self.editor._dibujar_timeline()
            menu.add_command(label=("Desactivar" if cut["enabled"] else "Activar") + f" {cut['cut_id']}",
                             command=lambda c=cut: self._toggle_cut(c))
            menu.add_command(label="Borrar", command=lambda c=cut: self._delete_cut(c))
            menu.add_separator()
            menu.add_command(label="Ir al inicio",
                             command=lambda c=cut: self.editor._set_playhead(c["t_ini"]))
            menu.add_command(label="Ir al final",
                             command=lambda c=cut: self.editor._set_playhead(c["t_fin"]))
            menu.add_command(label="Escuchar desde 2 s antes",
                             command=lambda c=cut: self.editor._play(reiniciar=True,
                                                                     desde=max(0.0, c["t_ini"] - 2.0)))
        else:
            t = self.editor._x2t(e.x, geometry)

            def create(seconds=1.0):
                try:
                    cut = editorial_trims.add_cut(self.trims, t, t + seconds, origin="user",
                                                  reason="Recorte manual.")
                except ValueError as error:
                    self.editor.status(f"⚠ {error}")
                    return
                self._select_cut(cut)
                self._commit_trims()
            menu.add_command(label=f"Crear recorte de 1 s en {format_time(t)[3:-1]}", command=create)
        menu.tk_popup(e.x_root, e.y_root)

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
