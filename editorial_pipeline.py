"""Orquestador transaccional del perfil editorial multipista.

La API pública es :func:`run`. Cada etapa publica artefactos solo después de validarlos
y conserva un manifest por contenido para reanudar sin repetir inferencia.
"""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import threading
import time
from contextlib import AbstractContextManager
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

import codex_chunker
import editorial_chunks
import editorial_master
import medios
import prosodia
from editorial_io import (atomic_write_json, atomic_write_text, digest_json, hash_file,
                          read_json, slug, track_id)


PIPELINE_VERSION = "editorial-pipeline/1"


class PipelineCancelled(InterruptedError):
    pass


def _emit(callback, event_type: str, **payload) -> None:
    if callback:
        callback({"tipo": event_type, **payload})


def _validate_spec(spec: dict, info: dict) -> dict:
    if not isinstance(spec, dict):
        raise ValueError("spec debe ser un objeto")
    source = Path(spec.get("source") or spec.get("fuente") or "").expanduser()
    if not source.is_file():
        raise ValueError(f"medio inexistente: {source}")
    project_dir = Path(spec.get("project_dir") or spec.get("outdir") or source.parent / source.stem)
    selected = spec.get("tracks")
    if not isinstance(selected, list) or not selected:
        raise ValueError("selecciona al menos una pista de voz")
    available = {int(item["idx"]): item for item in info.get("pistas") or []}
    indexes = set()
    tracks = []
    for position, item in enumerate(selected):
        index = int(item.get("stream_index", item.get("idx", -1)))
        if index not in available:
            raise ValueError(f"la pista de audio {index} no existe")
        if index in indexes:
            raise ValueError(f"la pista de audio {index} está repetida")
        indexes.add(index)
        identifier = str(item.get("track_id") or track_id(position)).upper()
        if not identifier.replace("-", "").isalnum():
            raise ValueError(f"track_id inválido: {identifier}")
        label = str(item.get("label") or available[index].get("titulo")
                    or f"Voz {identifier}").strip()
        tracks.append({"track_id": identifier, "stream_index": index, "label": label,
                       "media_track": available[index]})
    identifiers = [item["track_id"] for item in tracks]
    if len(set(identifiers)) != len(identifiers):
        raise ValueError("los track_id deben ser únicos")
    transcription = spec.get("transcription") or {}
    chunking = spec.get("chunking") or {}
    if not isinstance(chunking, dict):
        raise ValueError("chunking debe ser un objeto")
    chunking_mode = str(chunking.get("mode", "codex")).lower()
    if chunking_mode not in ("codex", "local"):
        raise ValueError("chunking.mode debe ser codex o local")
    timeout_seconds = float(chunking.get("timeout_seconds", 1_800))
    if timeout_seconds <= 0:
        raise ValueError("chunking.timeout_seconds debe ser positivo")
    return {
        "source": source.resolve(),
        "project_dir": project_dir.expanduser().resolve(),
        "project_name": slug(str(spec.get("project_name") or source.stem)),
        "tracks": tracks,
        "transcription": {
            "model": str(transcription.get("model", "medium")),
            "language": str(transcription.get("language", "es")),
            "device": str(transcription.get("device", "auto")),
            "align": True,
        },
        "chunk_count": spec.get("chunk_count"),
        "chunking": {
            "mode": chunking_mode,
            "fallback_local": bool(chunking.get("fallback_local", True)),
            "binary": chunking.get("binary"),
            "model": chunking.get("model"),
            "timeout_seconds": timeout_seconds,
        },
        "rebuild": bool(spec.get("rebuild", False)),
    }


def _preflight() -> None:
    import align
    import laughter

    missing = []
    if not align.available():
        missing.append("MMS/torchaudio")
    if not prosodia.available():
        missing.append("prosodia (torch + transformers + librosa)")
    if not laughter.available():
        missing.append("detector de risa LaughterSegmentation")
    if missing:
        raise RuntimeError("Faltan dependencias del perfil editorial: " + ", ".join(missing))


class _RunLock(AbstractContextManager):
    def __init__(self, work: Path):
        self.path = work / "run.lock"
        self.acquired = False

    @staticmethod
    def _alive(pid: int) -> bool:
        try:
            import psutil
            return psutil.pid_exists(pid)
        except Exception:
            try:
                os.kill(pid, 0)
                return True
            except OSError:
                return False

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            descriptor = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            try:
                current = read_json(self.path)
                pid = int(current.get("pid", -1))
            except Exception:
                pid = -1
            if pid > 0 and self._alive(pid):
                raise RuntimeError(f"ya hay un pipeline editorial activo (PID {pid})")
            self.path.unlink(missing_ok=True)
            descriptor = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        payload = json.dumps({"pid": os.getpid(), "started_at": datetime.now(timezone.utc).isoformat()})
        os.write(descriptor, payload.encode("utf-8"))
        os.close(descriptor)
        self.acquired = True
        return self

    def __exit__(self, exc_type, exc, traceback):
        if self.acquired:
            self.path.unlink(missing_ok=True)
        return False


class _StepStore:
    def __init__(self, root: Path, source_fingerprint: dict, *, rebuild: bool,
                 cancel, event_cb):
        self.root = root
        self.work = root / ".work"
        self.manifests = self.work / "manifests"
        self.staging = self.work / "staging"
        self.source_fingerprint = source_fingerprint
        self.rebuild = rebuild
        self.cancel = cancel
        self.event_cb = event_cb
        self.manifests.mkdir(parents=True, exist_ok=True)
        self.staging.mkdir(parents=True, exist_ok=True)

    def _manifest_path(self, step_id: str) -> Path:
        return self.manifests / f"{step_id}.json"

    def manifest(self, step_id: str) -> dict | None:
        try:
            return read_json(self._manifest_path(step_id))
        except Exception:
            return None

    def _key(self, params: dict, dependencies: list[str]) -> str:
        dep_data = {dependency: (self.manifest(dependency) or {}).get("result_digest")
                    for dependency in dependencies}
        return digest_json({"pipeline": PIPELINE_VERSION, "source": self.source_fingerprint,
                            "params": params, "dependencies": dep_data})

    def _reusable(self, manifest: dict | None, key: str, outputs: list[str]) -> bool:
        if self.rebuild or not manifest or manifest.get("key") != key:
            return False
        recorded = manifest.get("outputs") or {}
        for relative in outputs:
            path = self.root / relative
            data = recorded.get(relative)
            if not path.is_file() or not data or path.stat().st_size != data.get("size"):
                return False
            if hash_file(path) != data.get("sha256"):
                return False
        return True

    def run(self, step_id: str, label: str, *, params: dict, dependencies: list[str],
            outputs: list[str], action: Callable[[Path, Callable[[float], None]], dict | None],
            validate: Callable[[Path], None] | None = None,
            reusable_if: Callable[[dict], bool] | None = None) -> dict:
        if self.cancel is not None and self.cancel.is_set():
            raise PipelineCancelled("pipeline cancelado")
        key = self._key(params, dependencies)
        previous = self.manifest(step_id)
        if self._reusable(previous, key, outputs) \
                and (reusable_if is None or reusable_if(previous)):
            _emit(self.event_cb, "step", step=step_id, label=label, status="reused")
            return previous

        _emit(self.event_cb, "step", step=step_id, label=label, status="running")
        temporary = Path(tempfile.mkdtemp(prefix=f"{step_id}-", dir=self.staging))
        started = time.monotonic()

        def progress(fraction: float) -> None:
            _emit(self.event_cb, "progress", step=step_id,
                  fraction=max(0.0, min(1.0, float(fraction))))
            if self.cancel is not None and self.cancel.is_set():
                raise PipelineCancelled("pipeline cancelado")

        try:
            extra = action(temporary, progress) or {}
            for relative in outputs:
                staged = temporary / relative
                if not staged.is_file() or staged.stat().st_size == 0:
                    raise RuntimeError(f"{step_id} no produjo {relative}")
                if validate:
                    validate(staged)
            output_data = {}
            for relative in outputs:
                staged = temporary / relative
                destination = self.root / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                os.replace(staged, destination)
                output_data[relative] = {"size": destination.stat().st_size,
                                         "sha256": hash_file(destination)}
            manifest = {
                "schema": "editorial-step/1", "step": step_id, "key": key,
                "completed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "elapsed_seconds": round(time.monotonic() - started, 3),
                "outputs": output_data, "extra": extra,
            }
            manifest["result_digest"] = digest_json(manifest)
            atomic_write_json(self._manifest_path(step_id), manifest)
            _emit(self.event_cb, "step", step=step_id, label=label, status="ok",
                  elapsed=manifest["elapsed_seconds"])
            return manifest
        except InterruptedError as error:
            raise PipelineCancelled(str(error)) from error
        finally:
            shutil.rmtree(temporary, ignore_errors=True)


def _validate_json(path: Path) -> None:
    read_json(path)


def _validate_audio(path: Path) -> None:
    if not medios.decodifica_ventanas(path):
        raise RuntimeError("FLAC inválido")


def _validate_words(path: Path) -> None:
    words = read_json(path)
    if not isinstance(words, list):
        raise ValueError(f"{path.name} no es una lista")
    previous = -1.0
    for word in words:
        start = float(word.get("start", word.get("t_ini", -1)))
        end = float(word.get("end", word.get("t_fin", -1)))
        if start < previous - 0.1 or end < start:
            raise ValueError(f"timestamps inválidos en {path.name}")
        previous = start


def _track_paths(root: Path, identifier: str) -> dict[str, Path]:
    folder = root / "tracks" / identifier
    return {
        "folder": folder, "audio": folder / "audio.flac",
        "aligned_words": folder / "words.aligned.json", "words": folder / "words.json",
        "utterances": folder / "utterances.json", "laughter": folder / "laughter.json",
        "arousal": folder / "arousal.json", "intensity": folder / "intensity.json",
    }


def run(spec: dict, *, event_cb=None, cancel: threading.Event | None = None) -> dict:
    """Ejecuta/reanuda la Fase 1 local y devuelve rutas de sus artefactos principales."""
    cancel = cancel or threading.Event()
    if not isinstance(spec, dict):
        raise ValueError("spec debe ser un objeto")
    source_hint = spec.get("source") or spec.get("fuente")
    source = Path(source_hint or "").expanduser()
    if not source.is_file():
        raise ValueError(f"medio inexistente: {source}")
    _emit(event_cb, "log", message="Inspeccionando medio y timeline canónica…")
    info = medios.inspeccionar(source)
    resolved = _validate_spec(spec, info)
    _preflight()
    fingerprint = medios.fingerprint(resolved["source"], info)
    root = resolved["project_dir"] / "editorial"
    root.mkdir(parents=True, exist_ok=True)

    with _RunLock(root / ".work"):
        store = _StepStore(root, fingerprint, rebuild=resolved["rebuild"],
                           cancel=cancel, event_cb=event_cb)
        total_steps = len(resolved["tracks"]) * 4 + 3
        completed_steps = 0

        def completed() -> None:
            nonlocal completed_steps
            completed_steps += 1
            _emit(event_cb, "overall", fraction=completed_steps / total_steps)

        # 1. Extraer todas las pistas antes de cargar modelos.
        for track in resolved["tracks"]:
            identifier, media_track = track["track_id"], track["media_track"]
            relative = f"tracks/{identifier}/audio.flac"

            def extract_action(stage, progress, *, media_track=media_track, relative=relative):
                return medios.extraer_pista(resolved["source"], media_track, stage / relative,
                                             mono=True, cancel=cancel,
                                             log_cb=lambda message: _emit(event_cb, "log", message=message),
                                             progress_cb=progress)

            store.run(f"extract_{identifier}", f"Extraer voz {identifier}",
                      params={"track": media_track, "mono": True}, dependencies=[],
                      outputs=[relative], action=extract_action,
                      validate=_validate_audio)
            completed()

        # 2. Whisper + MMS obligatorio por pista.
        for track in resolved["tracks"]:
            identifier = track["track_id"]
            paths = _track_paths(root, identifier)
            words_rel = f"tracks/{identifier}/words.aligned.json"
            utterances_rel = f"tracks/{identifier}/utterances.json"

            def transcribe_action(stage, progress, *, paths=paths, words_rel=words_rel,
                                  utterances_rel=utterances_rel, identifier=identifier):
                import core
                destination = stage / f"tracks/{identifier}"
                result = core.transcribe(
                    paths["audio"], destination, model_name=resolved["transcription"]["model"],
                    lang=resolved["transcription"]["language"],
                    device=resolved["transcription"]["device"], want_segments=True,
                    want_srt=False, want_cues=False, want_align=True, align_required=True,
                    output_stem="", cancel=cancel,
                    log_cb=lambda message: _emit(event_cb, "log", message=message),
                    progress_cb=lambda fraction, eta=None: progress(fraction),
                )
                if result is None:
                    raise PipelineCancelled("transcripción cancelada")
                os.replace(destination / "words.json", stage / words_rel)
                os.replace(destination / "segments.json", stage / utterances_rel)
                return {key: result.get(key) for key in (
                    "device", "duration", "language", "n_words", "n_segments", "aligned")}

            store.run(f"transcribe_{identifier}", f"Whisper + MMS · {identifier}",
                      params=resolved["transcription"], dependencies=[f"extract_{identifier}"],
                      outputs=[words_rel, utterances_rel], action=transcribe_action,
                      validate=_validate_json)
            completed()

        # 3. Arousal e intensidad. El modelo permanece cargado entre pistas.
        for track in resolved["tracks"]:
            identifier = track["track_id"]
            paths = _track_paths(root, identifier)
            outputs = [f"tracks/{identifier}/{name}" for name in
                       ("words.json", "arousal.json", "intensity.json")]

            def prosody_action(stage, progress, *, identifier=identifier, paths=paths, outputs=outputs):
                aligned_words = read_json(paths["aligned_words"])
                regions = prosodia.speech_regions(aligned_words)
                arousal = prosodia.extract_arousal(
                    paths["audio"], speech_intervals=regions, cancel=cancel,
                    log_cb=lambda message: _emit(event_cb, "log", message=message),
                    progress_cb=lambda fraction, eta=None: progress(fraction * 0.85),
                )
                intensity = prosodia.extract_word_intensity(
                    paths["audio"], aligned_words, cancel=cancel,
                    progress_cb=lambda fraction, eta=None: progress(0.85 + fraction * 0.15),
                )
                enriched = prosodia.enrich_words(aligned_words, intensity, arousal)
                for relative, value in zip(outputs, (enriched, arousal, intensity)):
                    atomic_write_json(stage / relative, value)
                return {"arousal_windows": len(arousal["events"]), "words": len(enriched)}

            store.run(f"prosody_{identifier}", f"Arousal + intensidad · {identifier}",
                      params={"arousal_window": 4.0, "arousal_hop": 2.0,
                              "arousal_scope": "speech-regions/1",
                              "intensity": "word-mms/1"},
                      dependencies=[f"extract_{identifier}", f"transcribe_{identifier}"],
                      outputs=outputs, action=prosody_action,
                      validate=_validate_json)
            completed()
        prosodia.unload()

        # 4. Risa frame-level. El modelo también se reutiliza entre pistas.
        for track in resolved["tracks"]:
            identifier = track["track_id"]
            paths = _track_paths(root, identifier)
            relative = f"tracks/{identifier}/laughter.json"

            def laughter_action(stage, progress, *, paths=paths, relative=relative):
                import laughter
                events = laughter.detect(
                    paths["audio"], cancel=cancel,
                    log_cb=lambda message: _emit(event_cb, "log", message=message),
                    progress_cb=lambda fraction, eta=None: progress(fraction),
                )
                atomic_write_json(stage / relative, events)
                return {"events": len(events)}

            store.run(f"laughter_{identifier}", f"Risa · {identifier}",
                      params={"detector": "omine", "threshold": 0.5},
                      dependencies=[f"extract_{identifier}"], outputs=[relative],
                      action=laughter_action, validate=_validate_json)
            completed()
        try:
            import laughter
            laughter.unload()
        except Exception:
            pass

        # 5. Master y vistas. No reejecuta audio al cambiar el contrato de lectura.
        master_relative = f"{resolved['project_name']}.editorial.master.json"
        base_master_relative = f".work/{resolved['project_name']}.editorial.base.json"
        view_outputs = [base_master_relative, "views/conversation.md", "views/conversation-signals.md",
                        "views/map.json", "views/chunk-agent-request.md"]
        dependencies = [f"{kind}_{track['track_id']}" for track in resolved["tracks"]
                        for kind in ("transcribe", "prosody", "laughter")]

        def master_action(stage, progress):
            tracks = []
            for track in resolved["tracks"]:
                paths = _track_paths(root, track["track_id"])
                tracks.append({
                    "track_id": track["track_id"], "stream_index": track["stream_index"],
                    "label": track["label"], "offset": track["media_track"].get("delta", 0.0),
                    "words_path": paths["words"], "utterances_path": paths["utterances"],
                    "laughter_path": paths["laughter"], "arousal_path": paths["arousal"],
                    "intensity_path": paths["intensity"],
                })
            master = editorial_master.build_master(
                media=info, tracks=tracks, project_name=resolved["project_name"],
                fingerprint=fingerprint, transcription=resolved["transcription"],
                provenance={"pipeline": PIPELINE_VERSION},
            )
            editorial_master.write_package(stage, master)
            generated = stage / master_relative
            base_destination = stage / base_master_relative
            base_destination.parent.mkdir(parents=True, exist_ok=True)
            os.replace(generated, base_destination)
            progress(1.0)
            return {"tracks": len(tracks), "utterances": len(master["conversation"]["utterances"])}

        store.run("master", "Master + conversación global", params={"schema": "editorial-master/1"},
                  dependencies=dependencies, outputs=view_outputs, action=master_action)
        completed()

        # 6. Codex lee la conversación completa y propone los chunks semánticos. Este
        # paso queda cacheado por separado: jamás repite audio al cambiar el plan.
        chunk_outputs = ["views/chunks.json", "views/chunks.md"]
        base_master_path = root / base_master_relative
        master_path = root / master_relative

        selected_plan_path = root / ".work" / "chunks.selected.json"
        base_master = read_json(base_master_path)
        selected_plan = None
        if selected_plan_path.is_file():
            try:
                stored_plan = read_json(selected_plan_path)
                expected_digest = editorial_chunks.source_master_digest(base_master)
                if stored_plan.get("source_master_digest") != expected_digest:
                    raise ValueError("fue creado para otra revisión del master")
                selected_plan = editorial_chunks.validate_plan(stored_plan, base_master)
                _emit(event_cb, "log", message="Reutilizando el plan de chunks revisado/importado.")
            except Exception as error:
                _emit(event_cb, "log", message=f"El plan guardado ya no corresponde al medio: {error}")
        if selected_plan is None:
            proposed_relative = ".work/chunks.proposed.json"
            trace_relative = ".work/chunks.plan.trace.json"
            count = editorial_chunks.desired_count(base_master, resolved["chunk_count"])

            def plan_action(stage, progress):
                trace = {"schema": "editorial-chunk-plan-trace/1"}
                try:
                    if resolved["chunking"]["mode"] == "local":
                        plan = editorial_chunks.propose_local(base_master, count=count)
                        plan = editorial_chunks.snap_plan_to_safe_boundaries(
                            plan, base_master)
                        trace.update({"planner": plan["planner"], "fallback": False})
                    else:
                        raw, codex_trace = codex_chunker.plan(
                            root, base_master, count=count, work_dir=stage / ".work" / "codex",
                            binary=resolved["chunking"]["binary"],
                            model=resolved["chunking"]["model"],
                            timeout_seconds=resolved["chunking"]["timeout_seconds"],
                            cancel=cancel,
                            log_cb=lambda message: _emit(event_cb, "log", message=message),
                        )
                        semantic = editorial_chunks.validate_plan(raw, base_master)
                        plan = editorial_chunks.snap_plan_to_safe_boundaries(
                            semantic, base_master)
                        trace.update({"planner": plan["planner"], "fallback": False,
                                      "codex": codex_trace})
                        _emit(event_cb, "log", message=(
                            f"Codex propuso {len(plan['chunks'])} chunks; "
                            "límites ajustados contra todas las pistas."))
                except InterruptedError:
                    raise
                except Exception as error:
                    if (resolved["chunking"]["mode"] != "codex"
                            or not resolved["chunking"]["fallback_local"]):
                        raise
                    _emit(event_cb, "log", message=(
                        f"Codex no estuvo disponible ({error}). Usando fallback local explícito."))
                    plan = editorial_chunks.propose_local(base_master, count=count)
                    plan = {**plan, "planner": "local-fallback/1"}
                    for chunk in plan["chunks"]:
                        chunk["warnings"] = [
                            *(chunk.get("warnings") or []),
                            "Codex no estuvo disponible; este límite no proviene de análisis global.",
                        ]
                    plan = editorial_chunks.snap_plan_to_safe_boundaries(plan, base_master)
                    trace.update({"planner": plan["planner"], "fallback": True,
                                  "fallback_reason": str(error)})
                atomic_write_json(stage / proposed_relative, plan)
                atomic_write_json(stage / trace_relative, trace)
                progress(1.0)
                return {"planner": plan["planner"], "chunks": len(plan["chunks"]),
                        "fallback": trace.get("fallback", False)}

            store.run("chunk_plan", "Codex · separación semántica global",
                      params={"planner": codex_chunker.PLANNER_VERSION,
                              "mode": resolved["chunking"]["mode"],
                              "model": resolved["chunking"]["model"],
                              "count": count,
                              "boundary_snap": editorial_chunks.BOUNDARY_SNAP_VERSION},
                      dependencies=["master"], outputs=[proposed_relative, trace_relative],
                      action=plan_action, validate=_validate_json,
                      reusable_if=lambda manifest: not bool(
                          (manifest.get("extra") or {}).get("fallback")))
            selected_plan = editorial_chunks.validate_plan(
                read_json(root / proposed_relative), base_master)
        else:
            _emit(event_cb, "step", step="chunk_plan",
                  label="Plan de chunks revisado/importado", status="reused")
        completed()

        def chunks_action(stage, progress):
            master = read_json(base_master_path)
            staged_master = stage / master_relative
            atomic_write_json(staged_master, master)
            editorial_chunks.apply_plan(stage, staged_master, selected_plan)
            progress(1.0)
            return {"chunks": len(selected_plan["chunks"]),
                    "planner": selected_plan.get("planner", "agent")}

        # El paso publica master actualizado, vistas y toda la carpeta chunks.
        materialized = []
        for chunk in selected_plan["chunks"]:
            materialized.extend(f"chunks/{chunk['chunk_id']}/{name}" for name in (
                "transcript.md", "signals-summary.json", "laughter.json", "arousal.json",
                "intensity.json", "analysis-request.md"))
        store.run("chunks", "Chunks macro + vistas por chunk",
                  params={"plan_digest": digest_json(selected_plan)},
                  dependencies=["master"], outputs=[master_relative, *chunk_outputs, *materialized],
                  action=chunks_action)
        # moments.md pertenece al agente/editor, no al cache de derivados. Se crea una
        # sola vez y nunca se pisa al reanudar o cambiar otros artefactos.
        for chunk in selected_plan["chunks"]:
            moments = root / "chunks" / chunk["chunk_id"] / "moments.md"
            if not moments.exists():
                atomic_write_text(moments, f"# Momentos — {chunk['title']}\n\n"
                                  "Pendiente de análisis profundo por el agente.\n")
        completed()

        state = {
            "schema": "editorial-run/1", "status": "ok",
            "completed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "source": str(resolved["source"]), "root": str(root),
            "master": str(master_path), "tracks": resolved["tracks"],
            "chunk_planner": selected_plan.get("planner"),
        }
        # Evitar serializar el inventario ffprobe duplicado dentro de media_track.
        state["tracks"] = [{key: value for key, value in item.items() if key != "media_track"}
                           for item in resolved["tracks"]]
        atomic_write_json(root / "run.json", state)
        _emit(event_cb, "done", result=state)
        return state


def apply_agent_chunks(master_path: str | Path, document_path: str | Path) -> dict:
    """Aplica una respuesta de Codex/agente y regenera únicamente derivados de chunks."""
    master_path = Path(master_path)
    root = master_path.parent
    master = read_json(master_path)
    document = read_json(document_path)
    document = editorial_chunks.snap_plan_to_safe_boundaries(document, master)
    return editorial_chunks.apply_plan(root, master_path, document,
                                       persist_selection=True)


def _parse_track(value: str) -> dict:
    index_text, separator, label = value.partition("=")
    try:
        index = int(index_text)
    except ValueError as error:
        raise ValueError(f"pista inválida «{value}»; usa ÍNDICE=ETIQUETA") from error
    return {"stream_index": index, "label": label.strip() or f"Voz {index + 1}"}


def main(argv=None) -> int:
    """CLI headless para que Codex y scripts operen el pipeline sin automatizar Tk."""
    import argparse

    parser = argparse.ArgumentParser(description="Pipeline editorial multipista")
    subcommands = parser.add_subparsers(dest="command", required=True)
    run_parser = subcommands.add_parser("run", help="procesar un medio")
    run_parser.add_argument("source")
    run_parser.add_argument("--project-dir", required=True)
    run_parser.add_argument("--project-name")
    run_parser.add_argument("--track", action="append", required=True,
                            metavar="ÍNDICE=ETIQUETA",
                            help="pista de audio marcada como voz; se puede repetir")
    run_parser.add_argument("--model", default="medium")
    run_parser.add_argument("--language", default="es")
    run_parser.add_argument("--device", choices=("auto", "cpu", "cuda"), default="auto")
    run_parser.add_argument("--chunks", type=int, choices=(3, 4), default=None)
    run_parser.add_argument("--chunker", choices=("codex", "local"), default="codex")
    run_parser.add_argument("--codex-model")
    run_parser.add_argument("--codex-timeout", type=float, default=1_800,
                            help="límite de la lectura semántica, en segundos")
    run_parser.add_argument("--no-chunk-fallback", action="store_true",
                            help="fallar si Codex no está disponible")
    run_parser.add_argument("--rebuild", action="store_true")
    apply_parser = subcommands.add_parser("apply-chunks", help="validar y aplicar JSON del agente")
    apply_parser.add_argument("master")
    apply_parser.add_argument("document")
    arguments = parser.parse_args(argv)

    if arguments.command == "apply-chunks":
        result = apply_agent_chunks(arguments.master, arguments.document)
        print(json.dumps({"status": "ok", "chunks": len(result["chunks"])}, ensure_ascii=False))
        return 0

    try:
        tracks = [_parse_track(value) for value in arguments.track]
    except ValueError as error:
        parser.error(str(error))
    spec = {
        "source": arguments.source, "project_dir": arguments.project_dir,
        "project_name": arguments.project_name, "tracks": tracks,
        "transcription": {"model": arguments.model, "language": arguments.language,
                          "device": arguments.device},
        "chunk_count": arguments.chunks, "rebuild": arguments.rebuild,
        "chunking": {"mode": arguments.chunker, "model": arguments.codex_model,
                     "timeout_seconds": arguments.codex_timeout,
                     "fallback_local": not arguments.no_chunk_fallback},
    }

    def report(event: dict) -> None:
        if event["tipo"] == "log":
            print(event["message"], flush=True)
        elif event["tipo"] == "step":
            print(f"[{event['status']}] {event['label']}", flush=True)

    result = run(spec, event_cb=report)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
