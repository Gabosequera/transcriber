"""Exportación precisa y cancelable de un plan editorial aceptado, con o sin recortes."""
from __future__ import annotations

import functools
import os
import subprocess
import tempfile
import threading
from pathlib import Path

import editorial_chunks
import editorial_trims
import medios
from editorial_io import atomic_write_json, digest_json, read_json


VIDEO_PAD = "pad=ceil(iw/2)*2:ceil(ih/2)*2"


def source_matches(master, source, info=None):
    actual = medios.fingerprint(source, info)
    expected = master["media"]["fingerprint"]
    return all(actual.get(key) == expected.get(key) for key in
               ("size", "hash_muestreado", "inventario_sha256"))


def filter_script(kept, n_audio: int, has_video: bool) -> str:
    """Grafo `trim`/`atrim` + `concat` para conservar SOLO `kept` (tiempos RELATIVOS al
    inicio del bloque: el `-ss` de entrada rebasa los timestamps a 0).

    `setpts=PTS-S/TB` en vez de `PTS-STARTPTS`: una pista de audio que arranca tarde
    (offset OBS) conserva su desfase en el primer segmento y no reaparece en los demás.
    El filtro concat rellena con silencio la diferencia de redondeo audio/video de cada
    segmento (≤ 1 fotograma), así A/V nunca derivan aunque haya cientos de recortes."""
    count = len(kept)
    if count < 1:
        raise ValueError("no hay segmentos que conservar")
    streams = []
    if has_video:
        streams.append(("v", "[0:v:0]", "split", "trim", "setpts"))
    for index in range(n_audio):
        streams.append((f"a{index}", f"[0:a:{index}]", "asplit", "atrim", "asetpts"))
    if not streams:
        raise ValueError("el medio no tiene video ni audio")
    lines = []
    for name, source, split, trim, setpts in streams:
        outputs = "".join(f"[{name}_{k}]" for k in range(count))
        lines.append(f"{source}{split}={count}{outputs}")
        for k, (start, end) in enumerate(kept):
            lines.append(f"[{name}_{k}]{trim}=start={start:.6f}:end={end:.6f},"
                         f"{setpts}=PTS-{start:.6f}/TB[{name}_{k}t]")
    inputs = "".join(f"[{name}_{k}t]" for k in range(count) for name, *_ in streams)
    outputs = ("[vc]" if has_video else "") + "".join(f"[ac{i}]" for i in range(n_audio))
    lines.append(f"{inputs}concat=n={count}:v={int(has_video)}:a={n_audio}{outputs}")
    if has_video:
        lines.append(f"[vc]{VIDEO_PAD}[vout]")
    return ";\n".join(lines) + "\n"


@functools.lru_cache(maxsize=1)
def filter_script_option() -> str:
    """Cómo pasar un grafo de filtros por ARCHIVO a este ffmpeg: FFmpeg ≥ 7 acepta
    `-/filter_complex ruta` (y el 8 ya no tiene `-filter_complex_script`); los más viejos
    solo la forma antigua. Se sondea una vez con un comando trivial."""
    with tempfile.TemporaryDirectory(prefix=".ffprobe-opt-") as temporary:
        script = Path(temporary) / "graph.txt"
        script.write_text("[0:a:0]atrim=start=0:end=0.05,asetpts=PTS-0/TB[ac0]\n", encoding="utf-8")
        try:
            result = subprocess.run(["ffmpeg", "-hide_banner", "-nostdin", "-f", "lavfi", "-i",
                                     "anullsrc=d=0.1", "-/filter_complex", str(script),
                                     "-map", "[ac0]", "-f", "null", "-"],
                                    capture_output=True, timeout=30, **medios.flags_subprocess())
        except (OSError, subprocess.SubprocessError):
            return "-filter_complex_script"
    return "-/filter_complex" if result.returncode == 0 else "-filter_complex_script"


def export_plan(master_path, document, source, output_dir, *, trims=None, cancel=None,
                progress_cb=None, log_cb=None):
    """Publica una carpeta completa; nunca sobrescribe el medio ni una exportación.

    `document` es el plan de bloques (None = todo el medio en un bloque, solo con
    recortes). `trims` es el documento de recortes revisado: se quitan las UNIONES de
    sus recortes activos dentro de cada bloque."""
    master = read_json(master_path)
    duration = float(master["media"]["duration"])
    if document is None:
        if not trims:
            raise ValueError("Sin plan de bloques ni recortes no hay nada que exportar.")
        plan = editorial_trims.whole_plan(master)
    else:
        plan = editorial_chunks.validate_plan(document, master)
        if plan.get("source_master_digest") != editorial_chunks.source_master_digest(master):
            raise ValueError("El plan no corresponde a la metadata actual.")
        # Revalidar los cortes exactos que se mostraron, sin moverlos al aceptar.
        for chunk in plan["chunks"][:-1]:
            safety = editorial_chunks.boundary_safety(master, chunk["t_fin"])
            if safety["word_conflicts"] or safety["laughter_conflicts"]:
                raise ValueError("El plan atraviesa palabras o risas; vuelve a revisar sus límites.")
    intervals = []
    enabled_cuts = []
    if trims is not None:
        trims = editorial_trims.validate_document(
            trims, fingerprint=master["media"]["fingerprint"], duration=duration)
        intervals = editorial_trims.enabled_intervals(trims)
        enabled_cuts = [cut for cut in trims["cuts"] if cut["enabled"]]
        if enabled_cuts and log_cb:
            index = editorial_trims.BoundaryIndex(master)
            risky = [(cut, index.cut_warnings(cut)) for cut in enabled_cuts]
            risky = [(cut, warnings) for cut, warnings in risky if warnings]
            for cut, warnings in risky[:20]:
                log_cb(f"⚠ {cut['cut_id']} [{cut['t_ini']:.1f}–{cut['t_fin']:.1f}s]: "
                       + "; ".join(warnings))
            if len(risky) > 20:
                log_cb(f"⚠ … y {len(risky) - 20} recorte(s) más con bordes dentro de palabras/risas.")
    source = Path(source).resolve()
    info = medios.inspeccionar(source)
    if not source_matches(master, source, info):
        raise ValueError("El video no corresponde a la metadata del proyecto.")
    output = Path(output_dir).resolve()
    output.mkdir(parents=True, exist_ok=True)
    plan_id = (digest_json({"plan": plan, "trims": intervals}) if intervals
               else digest_json(plan))[:12]
    destination = output / f"podcast-{plan_id}"
    if destination.exists():
        raise FileExistsError(f"La exportación ya existe: {destination}")
    cancel = cancel or threading.Event()
    results = []
    origin = info["t0"] - float(_container_start(source))
    has_video = bool(info.get("video"))
    n_audio = len(info["pistas"])
    frame = 1.0 / ((info.get("video") or {}).get("fps") or 25) if has_video else 0.03
    with tempfile.TemporaryDirectory(prefix=".podcast-", dir=output) as temporary:
        stage = Path(temporary)
        for index, chunk in enumerate(plan["chunks"]):
            if cancel.is_set():
                raise InterruptedError("exportación cancelada")
            block = (float(chunk["t_ini"]), float(chunk["t_fin"]))
            kept = (editorial_trims.kept_segments(intervals, *block) if intervals
                    else [block])
            if not kept:
                if log_cb:
                    log_cb(f"Bloque {chunk['chunk_id']} omitido: queda vacío tras los recortes.")
                results.append({"file": None, "chunk_id": chunk["chunk_id"], "title": chunk["title"],
                                "t_ini": chunk["t_ini"], "t_fin": chunk["t_fin"],
                                "skipped": "vacío tras los recortes"})
                continue
            trimmed = (len(kept) != 1 or abs(kept[0][0] - block[0]) > 1e-6
                       or abs(kept[0][1] - block[1]) > 1e-6)
            expected = sum(end - start for start, end in kept)
            block_duration = block[1] - block[0]
            # Nombres numéricos portables: títulos/IDs del agente nunca son rutas Windows.
            filename = f"{index + 1:03d}.mp4" if has_video else f"{index + 1:03d}.m4a"
            target = stage / filename
            # Seek preciso con recodificación: conserva los offsets A/V y no
            # decodifica las horas previas para cada bloque.
            command = ["ffmpeg", "-hide_banner", "-nostdin", "-n",
                       "-ss", f"{origin + block[0]:.6f}"]
            script = None
            if trimmed:
                script = stage / f"{index + 1:03d}.filters.txt"
                relative = [(start - block[0], end - block[0]) for start, end in kept]
                script.write_text(filter_script(relative, n_audio, has_video), encoding="utf-8")
                # `-t` de ENTRADA: solo se lee el bloque; el script evita el límite de
                # longitud de la línea de comandos de Windows con cientos de segmentos.
                command += ["-t", f"{block_duration:.6f}", "-i", str(source),
                            filter_script_option(), str(script)]
                if has_video:
                    command += ["-map", "[vout]"]
                command += [item for i in range(n_audio) for item in ("-map", f"[ac{i}]")]
                command += ["-map_metadata", "0", "-map_chapters", "-1"]
            else:
                command += ["-i", str(source), "-t", f"{block_duration:.6f}",
                            "-map", "0:v:0?", "-map", "0:a?", "-map_metadata", "0",
                            "-map_chapters", "-1"]
                if has_video:
                    command += ["-vf", VIDEO_PAD]
            if has_video:
                command += ["-c:v", "libx264", "-preset", "fast", "-crf", "20",
                            "-pix_fmt", "yuv420p"]
            command += ["-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart",
                        "-progress", "pipe:1", "-loglevel", "error", str(target)]
            if log_cb:
                removed = block_duration - expected
                log_cb(f"Bloque {index + 1}/{len(plan['chunks'])} · {chunk['title']}: "
                       f"{expected / 60:.1f} min"
                       + (f" ({len(kept)} segmentos, se quitan {removed:.0f} s)" if trimmed else ""))
            _encode(command, cancel, lambda fraction: progress_cb(
                (index + fraction) / len(plan["chunks"])) if progress_cb else None, expected)
            if script is not None:
                script.unlink(missing_ok=True)
            actual = medios.inspeccionar(target)
            tolerance = max(0.15, 2 * frame) + (len(kept) * frame if trimmed else 0.0)
            if abs(actual["duracion"] - expected) > tolerance or len(actual["pistas"]) != n_audio:
                raise RuntimeError(f"Duración o pistas incorrectas en {filename}; no se publicó la exportación.")
            if bool(actual.get("video")) != has_video:
                raise RuntimeError(f"Falta el video en {filename}.")
            results.append({"file": filename, "chunk_id": chunk["chunk_id"],
                            "title": chunk["title"], "t_ini": chunk["t_ini"], "t_fin": chunk["t_fin"],
                            "kept_seconds": round(expected, 3),
                            "removed_seconds": round(block_duration - expected, 3),
                            "segments": [[start, end] for start, end in kept]})
        atomic_write_json(stage / "accepted-plan.json", plan)
        if trims is not None:
            atomic_write_json(stage / "accepted-trims.json", {
                "schema": "editorial-trims-export/1", "revision": trims.get("revision"),
                "intervals": [[start, end] for start, end in intervals],
                "removed_seconds": round(sum(end - start for start, end in intervals), 3),
                "cuts": enabled_cuts})
        atomic_write_json(stage / "exports.json", {"schema": "editorial-exports/1",
                                                   "trimmed": bool(intervals), "files": results})
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
