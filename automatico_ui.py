"""Workspace Automático funcional para la Fase 1 editorial."""
from __future__ import annotations

import queue
import threading
from pathlib import Path
from tkinter import messagebox

import customtkinter as ctk

import dialogs
import editorial_chunks
import editorial_pipeline
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


class ChunkReviewDialog(ctk.CTkToplevel):
    def __init__(self, parent, master_path: Path, on_saved=None):
        super().__init__(parent)
        self.master_path = master_path
        self.root_path = master_path.parent
        self.on_saved = on_saved
        self.master = read_json(master_path)
        self.document = read_json(self.root_path / "views" / "chunks.json")
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
            ctk.CTkLabel(body, text=chunk["chunk_id"], text_color=TEXT).grid(
                row=index, column=0, sticky="w", padx=8, pady=6)
            title = ctk.CTkEntry(body)
            title.insert(0, chunk["title"])
            title.grid(row=index, column=1, sticky="ew", padx=8, pady=6)
            start = ctk.CTkEntry(body, width=128)
            start.insert(0, format_time(chunk["t_ini"]))
            start.grid(row=index, column=2, padx=8, pady=6)
            end = ctk.CTkEntry(body, width=128)
            end.insert(0, format_time(chunk["t_fin"]))
            end.grid(row=index, column=3, padx=8, pady=6)
            confidence = ctk.CTkEntry(body, width=72)
            confidence.insert(0, str(chunk.get("confidence", 0.0)))
            confidence.grid(row=index, column=4, padx=8, pady=6)
            if index == 1:
                start.configure(state="disabled")
            if index == len(self.document["chunks"]):
                end.configure(state="disabled")
            self.rows.append({"title": title, "start": start, "end": end,
                              "confidence": confidence})

        footer = ctk.CTkFrame(self, fg_color="transparent")
        footer.grid(row=2, column=0, sticky="ew", padx=18, pady=(8, 16))
        footer.grid_columnconfigure(0, weight=1)
        self.status = ctk.CTkLabel(footer, text="", text_color="#e6b85c")
        self.status.grid(row=0, column=0, sticky="w")
        ctk.CTkButton(footer, text="Cancelar", width=92, fg_color=SURFACE_RAISED,
                      hover_color="#2a322d", command=self.destroy).grid(row=0, column=1, padx=6)
        ctk.CTkButton(footer, text="Guardar límites", width=132, fg_color=ACCENT,
                      hover_color=ACCENT_HOVER, command=self._save).grid(row=0, column=2, padx=(6, 0))
        self.transient(parent.winfo_toplevel())
        self.grab_set()

    def _save(self):
        try:
            chunks = []
            previous_end = 0.0
            duration = float(self.master["media"]["duration"])
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
            document = editorial_chunks.snap_plan_to_safe_boundaries(
                document, self.master)
            editorial_chunks.apply_plan(self.root_path, self.master_path, document,
                                         persist_selection=True)
        except Exception as error:
            self.status.configure(text=str(error))
            return
        if self.on_saved:
            self.on_saved()
        self.destroy()


class AutomaticWorkspace:
    """Importación, selección multipista y ejecución del perfil editorial."""

    def __init__(self, parent):
        self.info = None
        self.fingerprint = None
        self.track_widgets: list[dict] = []
        self.events: queue.Queue = queue.Queue()
        self.cancel = threading.Event()
        self.worker: threading.Thread | None = None
        self.result: dict | None = None

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
                                   on_video_cargado=self._on_media_loaded)
        self.editor.f.grid(row=0, column=0, sticky="nsew", padx=(0, 10))

        panel = ctk.CTkFrame(body, width=292, fg_color=SURFACE, corner_radius=10,
                             border_width=1, border_color=BORDER)
        panel.grid(row=0, column=1, sticky="nsew")
        panel.grid_propagate(False)
        panel.grid_columnconfigure(0, weight=1)
        panel.grid_rowconfigure(9, weight=1)
        ctk.CTkLabel(panel, text="PIPELINE EDITORIAL", text_color=MUTED,
                     font=ctk.CTkFont(size=10, weight="bold")).grid(
                         row=0, column=0, sticky="w", padx=14, pady=(14, 5))
        self.pipeline_title = ctk.CTkLabel(panel, text="Importa un medio", text_color=TEXT,
                                           font=ctk.CTkFont(size=14, weight="bold"))
        self.pipeline_title.grid(row=1, column=0, sticky="w", padx=14, pady=(0, 10))

        self.stage_labels = {}
        stages = (("extract", "Preparar pistas"), ("transcribe", "Whisper + MMS"),
                  ("signals", "Risa + prosodia"), ("master", "Master + Codex"))
        for row, (key, label) in enumerate(stages, 2):
            line = ctk.CTkFrame(panel, fg_color="transparent")
            line.grid(row=row, column=0, sticky="ew", padx=14, pady=2)
            line.grid_columnconfigure(0, weight=1)
            ctk.CTkLabel(line, text=label, text_color="#c6cec9", anchor="w").grid(
                row=0, column=0, sticky="ew")
            state = ctk.CTkLabel(line, text="EN ESPERA", text_color="#69756e",
                                 font=ctk.CTkFont(size=9, weight="bold"))
            state.grid(row=0, column=1)
            self.stage_labels[key] = state

        self.progress = ctk.CTkProgressBar(panel, progress_color=ACCENT)
        self.progress.set(0)
        self.progress.grid(row=6, column=0, sticky="ew", padx=14, pady=(12, 7))
        self.output_entry = ctk.CTkEntry(panel, placeholder_text="Carpeta del proyecto")
        self.output_entry.grid(row=7, column=0, sticky="ew", padx=14, pady=4)
        ctk.CTkButton(panel, text="Elegir carpeta", height=28, fg_color=SURFACE_RAISED,
                      hover_color="#2a322d", command=self._pick_output).grid(
                          row=8, column=0, sticky="ew", padx=14, pady=(2, 7))
        self.log = ctk.CTkTextbox(panel, wrap="word", font=ctk.CTkFont(size=10))
        self.log.grid(row=9, column=0, sticky="nsew", padx=14, pady=7)
        self.log.configure(state="disabled")

        actions = ctk.CTkFrame(panel, fg_color="transparent")
        actions.grid(row=10, column=0, sticky="ew", padx=14, pady=(4, 6))
        actions.grid_columnconfigure((0, 1), weight=1)
        self.view_button = ctk.CTkButton(actions, text="Conversación", height=28,
                                         state="disabled", fg_color=SURFACE_RAISED,
                                         hover_color="#2a322d", command=self._show_conversation)
        self.view_button.grid(row=0, column=0, sticky="ew", padx=(0, 3))
        self.chunks_button = ctk.CTkButton(actions, text="Revisar chunks", height=28,
                                           state="disabled", fg_color=SURFACE_RAISED,
                                           hover_color="#2a322d", command=self._review_chunks)
        self.chunks_button.grid(row=0, column=1, sticky="ew", padx=(3, 0))
        self.agent_button = ctk.CTkButton(panel, text="Importar plan JSON externo", height=28,
                                          state="disabled", fg_color=SURFACE_RAISED,
                                          hover_color="#2a322d", command=self._import_agent_chunks)
        self.agent_button.grid(row=11, column=0, sticky="ew", padx=14, pady=(0, 6))
        self.run_button = ctk.CTkButton(panel, text="Procesar pistas de voz", height=38,
                                        state="disabled", fg_color=ACCENT,
                                        hover_color=ACCENT_HOVER, command=self._run_or_cancel,
                                        font=ctk.CTkFont(size=13, weight="bold"))
        self.run_button.grid(row=12, column=0, sticky="ew", padx=14, pady=(0, 14))

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
        self.track_widgets = []
        source = Path(info["path"])
        self.project_title.configure(text=source.stem)
        self.project_status.configure(text=f"AUTOMÁTICO · {len(info['pistas'])} PISTAS DE AUDIO")
        default = source.parent / source.stem
        self.output_entry.delete(0, "end")
        self.output_entry.insert(0, str(default))
        self.pipeline_title.configure(text="Selecciona las pistas de voz")
        self.run_button.configure(state="normal")
        for button in (self.view_button, self.chunks_button, self.agent_button):
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
        for widget in self.track_widgets:
            widget["selected"].configure(state=state)
            widget["label"].configure(state=state)

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
        spec = {"source": self.info["path"], "project_dir": project_dir,
                "project_name": Path(self.info["path"]).stem, "tracks": tracks,
                "transcription": {"model": "medium", "language": "es", "device": "auto"}}
        self.cancel = threading.Event()
        self.progress.set(0)
        self.result = None
        self._set_processing(True)
        self.run_button.configure(text="Cancelar", state="normal")
        self.pipeline_title.configure(text="Procesando…")
        for label in self.stage_labels.values():
            label.configure(text="EN ESPERA", text_color="#69756e")
        self._append_log("\n══════════ Nueva corrida editorial ══════════")

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
        if step.startswith("extract_"):
            return "extract"
        if step.startswith("transcribe_"):
            return "transcribe"
        if step.startswith(("prosody_", "laughter_")):
            return "signals"
        return "master"

    def _append_log(self, message: str):
        self.log.configure(state="normal")
        self.log.insert("end", message.rstrip() + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def _pump(self):
        try:
            while True:
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
                                    "reused": ("REUTILIZADO", ACCENT)}.get(
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
                    for button in (self.view_button, self.chunks_button, self.agent_button):
                        button.configure(state="normal")
                    self._append_log(f"Listo: {self.result['master']}")
                    self._append_log(f"Planificador: {self.result.get('chunk_planner', 'desconocido')}")
                elif kind == "ui_error":
                    cancelled = self.cancel.is_set()
                    self._set_processing(False)
                    self.pipeline_title.configure(text="Cancelado" if cancelled else "Error")
                    self.run_button.configure(text="Reanudar", state="normal")
                    self._append_log(("Cancelado: " if cancelled else "ERROR: ") + event["error"])
        except queue.Empty:
            pass
        self.f.after(100, self._pump)

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
            ChunkReviewDialog(self.f, master, on_saved=lambda: self._append_log(
                "Chunks revisados y ajustados a bordes seguros; vistas regeneradas "
                "sin repetir audio."))

    def _import_agent_chunks(self):
        master = self._master_path()
        if not master:
            return
        path = dialogs.open_file("JSON de chunks generado por el agente",
                                 [("JSON", ["*.json"]), ("Todos", ["*"])],
                                 remember="editorial_chunks")
        if not path:
            return
        try:
            editorial_pipeline.apply_agent_chunks(master, path)
        except Exception as error:
            messagebox.showerror("Plan inválido", str(error))
            return
        self._append_log("Plan del agente validado y materializado sin repetir análisis de audio.")

    def activar(self):
        self.editor.activar()

    def desactivar(self):
        self.editor.desactivar()

    def cerrar(self):
        self.cancel.set()
        self.editor.cerrar()
