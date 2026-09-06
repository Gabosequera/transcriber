"""Contratos del podcast y exportación real con FFmpeg, sin descargar modelos."""
import copy
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock

import editorial_chunks
import editorial_io
import editorial_pipeline
import medios
import podcast_export
import test_editorial_phase1 as fixtures


def plan_for(master, boundaries):
    return {"schema": "editorial-chunks/1", "planner": "external-test",
            "source_master_digest": editorial_chunks.source_master_digest(master),
            "chunks": [{"chunk_id": f"chunk-{i:03d}", "t_ini": a, "t_fin": b,
                        "title": f"Tema {i}", "summary": "", "confidence": 0.8, "warnings": []}
                       for i, (a, b) in enumerate(zip(boundaries, boundaries[1:]), 1)]}


class PodcastContractTests(unittest.TestCase):
    def test_variable_number_and_hard_duration_limit(self):
        master = fixtures.ChunkTests().master()
        plan = plan_for(master, [0, 1800, 3600, 5400, 7200, 9000, 10800])
        self.assertEqual(len(editorial_chunks.validate_plan(plan, master)["chunks"]), 6)
        master["media"]["duration"] = 21600
        draft = editorial_chunks.propose_local(master)
        self.assertTrue(all(c["t_fin"] - c["t_ini"] <= 3000 for c in draft["chunks"]))
        self.assertEqual(draft["chunks"][-1]["t_fin"], 21600)

    def test_rejects_wrong_master_and_boolean_times(self):
        master = fixtures.ChunkTests().master()
        plan = plan_for(master, [0, 2700, 5400, 8100, 10800])
        wrong = copy.deepcopy(master)
        wrong["conversation"]["utterances"][0]["text"] = "otra conversación"
        with self.assertRaisesRegex(ValueError, "otro master"):
            editorial_chunks.validate_plan(plan, wrong)
        plan["chunks"][0]["t_ini"] = False
        with self.assertRaisesRegex(ValueError, "finito"):
            editorial_chunks.validate_plan(plan, master)

    def test_snap_never_exceeds_maximum(self):
        master = fixtures.ChunkTests().master()
        master["media"]["duration"] = 6000
        plan = plan_for(master, [0, 3000, 6000])
        result = editorial_chunks.snap_plan_to_safe_boundaries(plan, master)
        self.assertEqual(result["chunks"][0]["t_fin"], 3000)
        master["tracks"]["A"]["words"] = [{"t_ini": 2999, "t_fin": 3001}]
        plan = plan_for(master, [0, 3000, 6000])
        with self.assertRaisesRegex(ValueError, "corte seguro"):
            editorial_chunks.snap_plan_to_safe_boundaries(plan, master)

    def test_import_requires_identity_and_accepts_windows_bom(self):
        master = fixtures.ChunkTests().master()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            master_path = editorial_io.atomic_write_json(root / "demo.editorial.master.json", master)
            plan = plan_for(master, [0, 2700, 5400, 8100, 10800])
            proposal = root / "propuesta.json"
            proposal.write_text(json.dumps(plan), encoding="utf-8-sig")
            imported = editorial_pipeline.apply_agent_chunks(master_path, proposal)
            self.assertEqual(len(imported["chunks"]), 4)
            selected = editorial_io.read_json(root / ".work" / "chunks.selected.json")
            self.assertEqual(selected["source_master_digest"], plan["source_master_digest"])
            reviewed = {**imported, "planner": "manual-review"}
            reviewed["chunks"][0]["title"] = "Mi revisión"
            editorial_chunks.apply_plan(root, master_path, reviewed, persist_selection=True)
            reloaded = editorial_pipeline.apply_agent_chunks(master_path, proposal, reuse_proposal=True)
            self.assertEqual(reloaded["chunks"][0]["title"], "Mi revisión")
            del plan["source_master_digest"]
            editorial_io.atomic_write_json(proposal, plan)
            with self.assertRaisesRegex(ValueError, "source_master_digest"):
                editorial_pipeline.apply_agent_chunks(master_path, proposal)

    def test_temporal_index_keeps_nested_and_overlapping_events(self):
        import editorial_master
        events = [{"t_ini": 0, "t_fin": 100}, {"t_ini": 10, "t_fin": 12},
                  {"t_ini": 20, "t_fin": 30}, {"t_ini": 30, "t_fin": 40}]
        index = editorial_master._EventIndex(events)
        self.assertEqual(index.between(25, 30), [events[0], events[2]])
        self.assertEqual(index.between(100, 101), [])

    def test_windows_reserved_ids_and_submillisecond_chunks_are_rejected(self):
        master = fixtures.ChunkTests().master()
        plan = plan_for(master, [0, 2700, 5400, 8100, 10800])
        plan["chunks"][0]["chunk_id"] = "CON"
        with self.assertRaisesRegex(ValueError, "chunk_id"):
            editorial_chunks.validate_plan(plan, master)
        tiny = plan_for(master, [0, 0.0001, 2700, 5400, 8100, 10800])
        with self.assertRaisesRegex(ValueError, "rango inválido"):
            editorial_chunks.validate_plan(tiny, master)

    def test_cancel_encoder_without_waiting_for_progress(self):
        cancel = threading.Event()
        timer = threading.Timer(0.2, cancel.set)
        timer.start()
        try:
            with self.assertRaises(InterruptedError):
                podcast_export._encode([sys.executable, "-c", "import time; time.sleep(60)"],
                                       cancel, lambda _: None, 60)
        finally:
            timer.cancel()


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg no instalado")
class PodcastExportTests(unittest.TestCase):
    def test_video_split_between_keyframes_preserves_all_audio_tracks(self):
        with tempfile.TemporaryDirectory(prefix="podcast á espacios ") as tmp:
            root = Path(tmp)
            source = root / "conversación original.mkv"
            subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i",
                            "testsrc2=size=96x64:rate=25:duration=6", "-f", "lavfi", "-i",
                            "sine=frequency=440:duration=6", "-itsoffset", "0.4", "-f", "lavfi", "-i",
                            "sine=frequency=880:duration=5.6", "-map", "0:v", "-map", "1:a",
                            "-map", "2:a", "-c:v", "libx264", "-g", "150", "-c:a", "pcm_s16le",
                            str(source)], check=True, **medios.flags_subprocess())
            info = medios.inspeccionar(source)
            master = {"media": {"duration": info["duracion"], "fingerprint": medios.fingerprint(source, info)},
                      "conversation": {"utterances": [], "clean_utterance_ids": []}, "tracks": {}}
            master_path = editorial_io.atomic_write_json(root / "master.json", master)
            plan = plan_for(master, [0, 2.32, info["duracion"]])
            before = editorial_io.hash_file(source)
            # Una copia entre máquinas puede cambiar el mtime, pero no su identidad.
            os.utime(source, (100, 100))
            output = podcast_export.export_plan(master_path, plan, source, root / "salidas")
            self.assertTrue((output / "accepted-plan.json").is_file())
            self.assertEqual(editorial_io.hash_file(source), before)
            for filename, expected in [("001.mp4", 2.32), ("002.mp4", 3.68)]:
                exported = medios.inspeccionar(output / filename)
                self.assertAlmostEqual(exported["duracion"], expected, delta=0.1)
                self.assertEqual(len(exported["pistas"]), 2)
                expected_delay = 0.4 if filename == "001.mp4" else 0.0
                self.assertAlmostEqual(exported["pistas"][1]["delta"], expected_delay, delta=0.06)
            # El primer frame del segundo bloque debe corresponder al corte, no al keyframe 0.
            def frame(path, time):
                return subprocess.run(["ffmpeg", "-v", "error", "-ss", str(time), "-i", str(path),
                    "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "gray", "-"],
                    capture_output=True, check=True, **medios.flags_subprocess()).stdout
            import numpy as np
            expected = np.frombuffer(frame(source, 2.32), dtype=np.uint8).astype(float)
            actual = np.frombuffer(frame(output / "002.mp4", 0), dtype=np.uint8).astype(float)
            self.assertLess(np.abs(expected - actual).mean(), 6)
            with self.assertRaises(FileExistsError):
                podcast_export.export_plan(master_path, plan, source, root / "salidas")
            cancel = threading.Event()
            cancel.set()
            with self.assertRaises(InterruptedError):
                podcast_export.export_plan(master_path, plan, source, root / "canceladas", cancel=cancel)
            self.assertEqual(list((root / "canceladas").iterdir()), [])

    @staticmethod
    def _source_with_gop(root, name, gop):
        source = root / name
        subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i",
                        "testsrc2=size=96x64:rate=25:duration=6", "-f", "lavfi", "-i",
                        "sine=frequency=440:duration=6", "-map", "0:v", "-map", "1:a",
                        "-c:v", "libx264", "-g", str(gop), "-keyint_min", str(gop),
                        "-sc_threshold", "0", "-c:a", "aac", str(source)],
                       check=True, **medios.flags_subprocess())
        info = medios.inspeccionar(source)
        master = {"media": {"duration": info["duracion"], "fingerprint": medios.fingerprint(source, info)},
                  "conversation": {"utterances": [], "clean_utterance_ids": []}, "tracks": {}}
        return source, info, editorial_io.atomic_write_json(root / "master.json", master), master

    def test_copy_mode_keeps_codec_and_moves_boundaries_to_keyframes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source, info, master_path, master = self._source_with_gop(root, "original.mp4", 25)
            plan = plan_for(master, [0, 2.32, info["duracion"]])
            output = podcast_export.export_plan(master_path, plan, source, root / "out", fmt="copy")
            exports = editorial_io.read_json(output / "exports.json")
            self.assertEqual(exports["format"], "copy")
            first, second = exports["files"]
            # El límite pedido (2.32) cae al fotograma clave anterior (2.0), compartido.
            self.assertAlmostEqual(second["t_ini"], 2.0, delta=0.05)
            self.assertEqual(first["t_fin"], second["t_ini"])
            self.assertAlmostEqual(second["shift_seconds"], 0.32, delta=0.05)
            for entry in (first, second):
                exported = medios.inspeccionar(output / entry["file"])
                self.assertTrue(entry["file"].endswith(".mp4"))
                self.assertEqual(exported["video"]["codec"], "h264")
                self.assertEqual(exported["pistas"][0]["codec"], "aac")
                self.assertAlmostEqual(exported["duracion"], entry["t_fin"] - entry["t_ini"], delta=0.15)
            # El hijo hereda el tiempo REAL del corte, no el pedido.
            child = editorial_io.read_json(output / second["project_master"])
            self.assertAlmostEqual(child["derivation"]["segments"][0]["source_ini"], 2.0, delta=0.05)
            # El mismo plan en H.264 va a otra carpeta (otro id), sin chocar con la copia.
            plain = podcast_export.export_plan(master_path, plan, source, root / "out")
            self.assertNotEqual(plain, output)
            self.assertEqual(medios.inspeccionar(plain / "002.mp4")["video"]["codec"], "h264")
            import editorial_trims
            trims = editorial_trims.new_document(master["media"]["fingerprint"], info["duracion"])
            editorial_trims.add_cut(trims, 1.0, 1.5)
            with self.assertRaisesRegex(ValueError, "recortes"):
                podcast_export.export_plan(master_path, plan, source, root / "out2", fmt="copy", trims=trims)
            with self.assertRaisesRegex(ValueError, "desconocido"):
                podcast_export.export_plan(master_path, plan, source, root / "out3", fmt="vhs")

    def test_reencoding_formats_cut_exactly(self):
        encoders = subprocess.run(["ffmpeg", "-hide_banner", "-encoders"], capture_output=True,
                                  text=True, **medios.flags_subprocess()).stdout
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source, info, master_path, master = self._source_with_gop(root, "original.mkv", 150)
            plan = plan_for(master, [0, 2.32, info["duracion"]])
            for fmt, encoder, codec, extension in (("prores", "prores_ks", "prores", "mov"),
                                                   ("hevc", "libx265", "hevc", "mp4")):
                if f" {encoder} " not in encoders:
                    continue
                output = podcast_export.export_plan(master_path, plan, source, root / "out", fmt=fmt)
                exports = editorial_io.read_json(output / "exports.json")
                self.assertEqual(exports["format"], fmt)
                self.assertEqual(exports["files"][1]["t_ini"], 2.32)
                exported = medios.inspeccionar(output / f"002.{extension}")
                self.assertEqual(exported["video"]["codec"], codec)
                self.assertAlmostEqual(exported["duracion"], info["duracion"] - 2.32, delta=0.1)
                if fmt == "prores":
                    self.assertEqual(exported["video"]["pix_fmt"], "yuv422p10le")
                    self.assertEqual(exported["pistas"][0]["codec"], "pcm_s24le")

    def test_failed_encoder_never_publishes_partial_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "audio.wav"
            subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i",
                            "sine=duration=2", str(source)], check=True, **medios.flags_subprocess())
            info = medios.inspeccionar(source)
            master = {"media": {"duration": 2, "fingerprint": medios.fingerprint(source, info)},
                      "conversation": {"utterances": [], "clean_utterance_ids": []}, "tracks": {}}
            master_path = editorial_io.atomic_write_json(root / "master.json", master)
            plan = plan_for(master, [0, 1, 2])
            with mock.patch("podcast_export._encode", side_effect=RuntimeError("disco lleno")):
                with self.assertRaisesRegex(RuntimeError, "disco lleno"):
                    podcast_export.export_plan(master_path, plan, source, root / "out")
            self.assertEqual(list((root / "out").iterdir()), [])
            output = podcast_export.export_plan(master_path, plan, source, root / "out")
            self.assertEqual(len(list(output.glob("*.m4a"))), 2)


if __name__ == "__main__":
    unittest.main()
