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
        self.plan: dict | None = None
        self._last_plan_stamp = None
        self._poll_counter = 0
        self.review_dialog = None

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
                                   carriles_extra=self._cut_lanes)
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
                  ("signals", "Risa + intensidad + emoción"), ("master", "Metadata para AI externa"))
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
        self.accept_button = ctk.CTkButton(panel, text="Aceptar y exportar cortes", height=32,
                                           state="disabled", fg_color=ACCENT,
                                           command=self._accept_cuts)
        self.accept_button.grid(row=12, column=0, sticky="ew", padx=14, pady=(0, 6))
        self.open_project_button = ctk.CTkButton(panel, text="Abrir proyecto existente", height=28,
                                                fg_color=SURFACE_RAISED,
                                                command=self._open_project)
        self.open_project_button.grid(row=13, column=0, sticky="ew", padx=14, pady=(0, 6))
        self.run_button = ctk.CTkButton(panel, text="Procesar pistas de voz", height=38,
                                        state="disabled", fg_color=ACCENT,
                                        hover_color=ACCENT_HOVER, command=self._run_or_cancel,
                                        font=ctk.CTkFont(size=13, weight="bold"))
        self.run_button.grid(row=14, column=0, sticky="ew", padx=14, pady=(0, 14))

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
        self.track_widgets = []
        source = Path(info["path"])
        self.project_title.configure(text=source.stem)
        self.project_status.configure(text=f"AUTOMÁTICO · {len(info['pistas'])} PISTAS DE AUDIO")
        default = source.parent / source.stem
        self.output_entry.delete(0, "end")
        self.output_entry.insert(0, str(default))
        self.pipeline_title.configure(text="Selecciona las pistas de voz")
        self.run_button.configure(state="normal")
        for button in (self.view_button, self.chunks_button, self.agent_button, self.accept_button):
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
        for button in (self.view_button, self.chunks_button, self.agent_button, self.accept_button):
            button.configure(state="disabled")
        if not active:
            self._refresh_plan_buttons()
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
                "project_name": (Path(self.result["master"]).name.removesuffix(".editorial.master.json")
                                 if self.result else Path(self.info["path"]).stem), "tracks": tracks,
                "transcription": {"model": "medium", "language": "es", "device": "auto"},
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
                    self._load_saved_plan()
                    self._refresh_plan_buttons()
                    self._append_log(f"Listo: {self.result['master']}")
                    self._append_log(f"Planificador: {self.result.get('chunk_planner', 'desconocido')}")
                elif kind == "review_ready":
                    self._set_processing(False)
                    self.run_button.configure(text="Reanudar / actualizar", state="normal")
                    self.review_dialog = ChunkReviewDialog(self.f, event["path"],
                        on_saved=self._load_saved_plan, master=event["master"], document=self.plan)
                elif kind == "plan_loaded":
                    self.plan = event["plan"]
                    self.editor.refrescar_layout()
                    self._set_processing(False)
                    self.run_button.configure(text="Reanudar / actualizar", state="normal")
                    self._append_log(f"Propuesta lista: {len(self.plan['chunks'])} bloques. Revisa el carril de colores antes de aceptar.")
                elif kind == "export_done":
                    self._set_processing(False)
                    self.run_button.configure(text="Reanudar / actualizar", state="normal")
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
            path = Path(self.result["master"]).parent / "views" / "cuts.proposed.json"
            try:
                stat = path.stat()
                stamp = (str(path), stat.st_mtime_ns, stat.st_size)
                if stamp != self._last_plan_stamp:
                    self._last_plan_stamp = stamp
                    self._import_plan(path, reuse_proposal=True)
            except OSError:
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
            self._background(lambda: self.events.put({"tipo": "review_ready", "path": master,
                                                      "master": read_json(master)}))

    def _import_agent_chunks(self):
        master = self._master_path()
        if not master:
            return
        path = dialogs.open_file("JSON de chunks generado por el agente",
                                 [("JSON", ["*.json"]), ("Todos", ["*"])],
                                 remember="editorial_chunks")
        if not path:
            return
        self._import_plan(Path(path))

    def _refresh_plan_buttons(self):
        ready = bool(self.result)
        self.view_button.configure(state="normal" if ready else "disabled")
        self.agent_button.configure(state="normal" if ready else "disabled")
        for button in (self.chunks_button, self.accept_button):
            button.configure(state="normal" if ready and self.plan else "disabled")

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
                        {"tipo": "overall", "fraction": fraction}))
            self.events.put({"tipo": "export_done", "path": str(destination)})
        self._background(work)

    def _cut_lanes(self):
        return [{"alto": 36, "dibujar": self._draw_cuts}] if self.plan else []

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

    def activar(self):
        self.editor.activar()

    def desactivar(self):
        self.editor.desactivar()

    def cerrar(self):
        self.cancel.set()
        self.editor.cerrar()
