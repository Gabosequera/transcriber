"""Exportación precisa y cancelable de un plan editorial aceptado."""
from __future__ import annotations

import os
import subprocess
import tempfile
import threading
from pathlib import Path

import editorial_chunks
import medios
from editorial_io import atomic_write_json, digest_json, read_json


def source_matches(master, source, info=None):
    actual = medios.fingerprint(source, info)
    expected = master["media"]["fingerprint"]
    return all(actual.get(key) == expected.get(key) for key in
               ("size", "hash_muestreado", "inventario_sha256"))


def export_plan(master_path, document, source, output_dir, *, cancel=None, progress_cb=None):
    """Publica una carpeta completa; nunca sobrescribe el medio ni una exportación."""
    master = read_json(master_path)
    plan = editorial_chunks.validate_plan(document, master)
    if plan.get("source_master_digest") != editorial_chunks.source_master_digest(master):
        raise ValueError("El plan no corresponde a la metadata actual.")
    source = Path(source).resolve()
    info = medios.inspeccionar(source)
    if not source_matches(master, source, info):
        raise ValueError("El video no corresponde a la metadata del proyecto.")
    # Revalidar los cortes exactos que se mostraron, sin moverlos al aceptar.
    for chunk in plan["chunks"][:-1]:
        safety = editorial_chunks.boundary_safety(master, chunk["t_fin"])
        if safety["word_conflicts"] or safety["laughter_conflicts"]:
            raise ValueError("El plan atraviesa palabras o risas; vuelve a revisar sus límites.")
    output = Path(output_dir).resolve()
    output.mkdir(parents=True, exist_ok=True)
    plan_id = digest_json(plan)[:12]
    destination = output / f"podcast-{plan_id}"
    if destination.exists():
        raise FileExistsError(f"La exportación ya existe: {destination}")
    cancel = cancel or threading.Event()
    results = []
    origin = info["t0"] - float(_container_start(source))
    with tempfile.TemporaryDirectory(prefix=".podcast-", dir=output) as temporary:
        stage = Path(temporary)
        for index, chunk in enumerate(plan["chunks"]):
            if cancel.is_set():
                raise InterruptedError("exportación cancelada")
            duration = chunk["t_fin"] - chunk["t_ini"]
            # Nombres numéricos portables: títulos/IDs del agente nunca son rutas Windows.
            filename = f"{index + 1:03d}.mp4" if info.get("video") else f"{index + 1:03d}.m4a"
            target = stage / filename
            # Seek preciso con recodificación: conserva los offsets A/V y no
            # decodifica las horas previas para cada bloque.
            command = ["ffmpeg", "-hide_banner", "-nostdin", "-n",
                       "-ss", f"{origin + chunk['t_ini']:.6f}", "-i", str(source),
                       "-t", f"{duration:.6f}",
                       "-map", "0:v:0?", "-map", "0:a?", "-map_metadata", "0",
                       "-map_chapters", "-1"]
            if info.get("video"):
                command += ["-c:v", "libx264", "-preset", "fast", "-crf", "20",
                            "-pix_fmt", "yuv420p", "-vf", "pad=ceil(iw/2)*2:ceil(ih/2)*2"]
            command += ["-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart",
                        "-progress", "pipe:1", "-loglevel", "error", str(target)]
            _encode(command, cancel, lambda fraction: progress_cb(
                (index + fraction) / len(plan["chunks"])) if progress_cb else None, duration)
            actual = medios.inspeccionar(target)
            tolerance = max(0.15, 2 / (info.get("video", {}).get("fps", 25) or 25)) if info.get("video") else 0.15
            if abs(actual["duracion"] - duration) > tolerance or len(actual["pistas"]) != len(info["pistas"]):
                raise RuntimeError(f"Duración o pistas incorrectas en {filename}; no se publicó la exportación.")
            if bool(actual.get("video")) != bool(info.get("video")):
                raise RuntimeError(f"Falta el video en {filename}.")
            results.append({"file": filename, "chunk_id": chunk["chunk_id"],
                            "title": chunk["title"], "t_ini": chunk["t_ini"], "t_fin": chunk["t_fin"]})
        atomic_write_json(stage / "accepted-plan.json", plan)
        atomic_write_json(stage / "exports.json", {"schema": "editorial-exports/1", "files": results})
        if cancel.is_set():
            raise InterruptedError("exportación cancelada")
        # Renombrado de carpeta en el mismo volumen: no quedan resultados parciales.
        os.rename(stage, destination)
    return destination


def _container_start(source):
    result = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=start_time",
                             "-of", "default=nw=1:nk=1", str(source)], capture_output=True,
                            text=True, check=True, timeout=30, **medios.flags_subprocess())
    return 0.0 if result.stdout.strip() in ("", "N/A") else float(result.stdout)


def _encode(command, cancel, progress, duration):
    # stderr en disco evita deadlocks; lector de progreso separado permite cancelar
    # incluso cuando FFmpeg todavía no ha emitido ninguna línea.
    with tempfile.TemporaryFile(mode="w+b") as errors:
        process = medios.popen_gestionado(command, stdout=subprocess.PIPE, stderr=errors,
                                          text=True, encoding="utf-8", errors="replace")
        def read_progress():
            for line in process.stdout:
                if line.startswith("out_time_us="):
                    try:
                        progress(min(1.0, max(0.0, int(line.split("=", 1)[1]) / 1e6 / duration)))
                    except ValueError:
                        pass
        reader = threading.Thread(target=read_progress, daemon=True)
        reader.start()
        try:
            while process.poll() is None:
                if cancel.wait(0.1):
                    raise InterruptedError("exportación cancelada")
            if process.returncode:
                errors.seek(0, os.SEEK_END)
                errors.seek(max(0, errors.tell() - 2000))
                raise RuntimeError("FFmpeg: " + errors.read().decode("utf-8", errors="replace"))
        finally:
            if process.poll() is None:
                process.kill()
            process.wait()
            reader.join(timeout=5)
            process.stdout.close()
