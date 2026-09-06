from __future__ import annotations

import hashlib
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

import codex_chunker
import editorial_chunks
import editorial_io
import editorial_master
import editorial_pipeline
import prosodia


def write_json(path: Path, value) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    return path


def track_fixture(root: Path, identifier: str, label: str, offset: float,
                  text: str, *, start: float = 10.0) -> dict:
    folder = root / identifier
    words = [{"word": word, "start": start + index * 0.35,
              "end": start + index * 0.35 + 0.28, "prob": 0.95,
              "rms_dbfs": -18.0 + index, "peak_dbfs": -8.0,
              "local_floor_dbfs": -35.0, "local_contrast_db": 17.0,
              "intensity_z": float(index), "emphasis_score": 0.7,
              "arousal": 0.6, "arousal_z": 0.8}
             for index, word in enumerate(text.split())]
    end = words[-1]["end"]
    segments = [{"start": start, "end": end, "text": text, "words": words}]
    intensity = {"baseline": {"mean": -18.0, "std": 1.0},
                 "events": [{"word_index": index, "t_ini": word["start"],
                              "t_fin": word["end"]} for index, word in enumerate(words)]}
    arousal = {"baseline": {"mean": 0.5, "std": 0.1},
               "events": [{"t_ini": start, "t_fin": end, "arousal": 0.6,
                            "arousal_z": 1.0}]}
    return {
        "track_id": identifier, "stream_index": ord(identifier) - ord("A"),
        "label": label, "offset": offset,
        "words_path": write_json(folder / "words.json", words),
        "utterances_path": write_json(folder / "utterances.json", segments),
        "laughter_path": write_json(folder / "laughter.json", [
            {"t_ini": start + 0.4, "t_fin": start + 0.8, "conf": 0.9,
             "mean_conf": 0.8, "max_conf": 0.9}]),
        "arousal_path": write_json(folder / "arousal.json", arousal),
        "intensity_path": write_json(folder / "intensity.json", intensity),
    }


class EditorialIoTests(unittest.TestCase):
    def test_timecodes_and_track_ids(self):
        self.assertEqual(editorial_io.track_id(0), "A")
        self.assertEqual(editorial_io.track_id(25), "Z")
        self.assertEqual(editorial_io.track_id(26), "AA")
        self.assertAlmostEqual(editorial_io.parse_time("01:02:03.250"), 3723.25)
        self.assertEqual(editorial_io.format_time(3723.25), "01:02:03.250")

    def test_streaming_digest_matches_canonical_json(self):
        value = {"texto": "áéí", "items": list(range(100))}
        expected = hashlib.sha256(editorial_io.json_bytes(value)).hexdigest()
        self.assertEqual(editorial_io.digest_json(value), expected)


class ProsodyTests(unittest.TestCase):
    def test_speech_regions_merge_nearby_words_and_keep_long_pauses(self):
        words = [
            {"start": 1.0, "end": 1.4},
            {"start": 2.0, "end": 2.2},
            {"start": 5.0, "end": 5.5},
        ]
        self.assertEqual(
            prosodia.speech_regions(words, gap_seconds=1.0, padding_seconds=0.5,
                                    duration=6.0),
            [(0.5, 2.7), (4.5, 6.0)],
        )

    def test_arousal_window_offsets_cover_only_speech_regions(self):
        hop = 2 * prosodia.SAMPLE_RATE
        self.assertEqual(
            prosodia._window_offsets(20 * prosodia.SAMPLE_RATE, hop,
                                     [(5.0, 7.0), (15.0, 16.0)]),
            [4 * prosodia.SAMPLE_RATE, 6 * prosodia.SAMPLE_RATE,
             14 * prosodia.SAMPLE_RATE],
        )

    def test_intensity_is_one_to_one_and_finite(self):
        waveform = np.zeros(16_000, dtype=np.float32)
        waveform[1_600:4_800] = 0.25
        waveform[6_400:11_200] = 0.5
        words = [{"word": "hola", "start": 0.1, "end": 0.3},
                 {"word": "mundo", "start": 0.4, "end": 0.7}]
        with mock.patch("audiocache.load", return_value=waveform):
            result = prosodia.extract_word_intensity("ignored.flac", words)
        self.assertEqual(len(result["events"]), len(words))
        self.assertGreater(result["events"][1]["rms_dbfs"], result["events"][0]["rms_dbfs"])
        self.assertTrue(all(0 <= item["emphasis_score"] <= 1 for item in result["events"]))


class MasterTests(unittest.TestCase):
    def test_combines_tracks_and_preserves_duplicate_audit(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            tracks = [track_fixture(root, "A", "Gabriel + amigo", 0.0, "mira detras de ti"),
                      track_fixture(root, "B", "Amigo 2", 0.0, "mira detras de ti", start=10.02)]
            master = editorial_master.build_master(
                media={"path": "video.mkv", "duracion": 60.0, "t0": 0.0}, tracks=tracks,
                project_name="demo", fingerprint={"hash": "x"},
                transcription={"model": "medium", "align": True})
        self.assertEqual(set(master["tracks"]), {"A", "B"})
        self.assertEqual(len(master["conversation"]["utterances"]), 2)
        self.assertEqual(len(master["conversation"]["duplicate_groups"]), 1)
        self.assertEqual(len(master["conversation"]["clean_utterance_ids"]), 1)
        overlap = master["conversation"]["overlap_groups"][0]
        self.assertEqual(overlap["t_ini"], 10.0)
        self.assertEqual(overlap["t_fin"], 11.35)
        self.assertTrue(overlap["intersections"])
        for track in master["tracks"].values():
            self.assertTrue(all(word["track_id"] == track["track_id"] for word in track["words"]))


class ChunkTests(unittest.TestCase):
    def master(self):
        utterances = []
        for index in range(36):
            start = index * 300.0 + 15.0
            topic = ("inicio presentacion" if index < 12 else
                     "combate estrategia" if index < 24 else "cierre resultados")
            utterances.append({"utterance_id": f"A-u-{index:04d}", "track_id": "A",
                               "t_ini": start, "t_fin": start + 45.0,
                               "text": f"{topic} parte {index}", "word_ids": [],
                               "signals": {"laughter_max": None, "arousal_z_mean": 0.0}})
        return {"media": {"duration": 10800.0},
                "conversation": {"utterances": utterances,
                                 "clean_utterance_ids": [item["utterance_id"] for item in utterances]},
                "tracks": {"A": {"label": "Voz", "words": [], "laughter": [], "arousal": []}}}

    def test_local_plan_covers_full_timeline(self):
        master = self.master()
        plan = editorial_chunks.propose_local(master, count=4)
        self.assertEqual(len(plan["chunks"]), 4)
        self.assertEqual(plan["chunks"][0]["t_ini"], 0.0)
        self.assertEqual(plan["chunks"][-1]["t_fin"], 10800.0)
        for left, right in zip(plan["chunks"], plan["chunks"][1:]):
            self.assertEqual(left["t_fin"], right["t_ini"])

    def test_source_digest_ignores_derived_chunks_but_detects_conversation_changes(self):
        master = self.master()
        original = editorial_chunks.source_master_digest(master)
        master["chunks"] = [{"chunk_id": "derivado"}]
        self.assertEqual(editorial_chunks.source_master_digest(master), original)
        master["conversation"]["utterances"][0]["text"] = "texto cambiado"
        self.assertNotEqual(editorial_chunks.source_master_digest(master), original)

    def test_rejects_gap(self):
        master = self.master()
        plan = editorial_chunks.propose_local(master, count=3)
        plan["chunks"][1]["t_ini"] += 1.0
        with self.assertRaisesRegex(ValueError, "hueco/solape"):
            editorial_chunks.validate_plan(plan, master)

    def test_rejects_unsafe_chunk_id(self):
        master = self.master()
        plan = editorial_chunks.propose_local(master, count=3)
        plan["chunks"][0]["chunk_id"] = "../../afuera"
        with self.assertRaisesRegex(ValueError, "chunk_id inválido"):
            editorial_chunks.validate_plan(plan, master)

    def test_rejects_non_finite_timestamps(self):
        master = self.master()
        plan = editorial_chunks.propose_local(master, count=3)
        plan["chunks"][0]["t_fin"] = float("nan")
        with self.assertRaisesRegex(ValueError, "número finito"):
            editorial_chunks.validate_plan(plan, master)

    def test_long_recording_rejects_chunks_over_fifty_minutes(self):
        master = self.master()
        plan = editorial_chunks.propose_local(master, count=4)
        plan["chunks"] = plan["chunks"][:2]
        plan["chunks"][-1]["t_fin"] = master["media"]["duration"]
        with self.assertRaisesRegex(ValueError, "50 minutos"):
            editorial_chunks.validate_plan(plan, master)

    def test_sparse_conversation_still_has_unique_boundaries(self):
        master = self.master()
        master["conversation"]["utterances"] = master["conversation"]["utterances"][:2]
        master["conversation"]["clean_utterance_ids"] = [
            item["utterance_id"] for item in master["conversation"]["utterances"]]
        plan = editorial_chunks.propose_local(master, count=4)
        boundaries = [chunk["t_ini"] for chunk in plan["chunks"]] + [plan["chunks"][-1]["t_fin"]]
        self.assertEqual(boundaries, sorted(set(boundaries)))

    def test_safe_snap_avoids_words_laughter_and_simultaneous_utterances(self):
        master = {
            "media": {"duration": 30.0},
            "conversation": {
                "utterances": [
                    {"utterance_id": "A-u-1", "track_id": "A", "t_ini": 8.0,
                     "t_fin": 12.0, "text": "primera idea", "signals": {}},
                    {"utterance_id": "B-u-1", "track_id": "B", "t_ini": 9.0,
                     "t_fin": 13.0, "text": "respuesta", "signals": {}},
                    {"utterance_id": "A-u-2", "track_id": "A", "t_ini": 16.0,
                     "t_fin": 18.0, "text": "tema siguiente", "signals": {}},
                ],
                "clean_utterance_ids": ["A-u-1", "B-u-1", "A-u-2"],
            },
            "tracks": {
                "A": {"label": "A", "words": [
                    {"t_ini": 9.8, "t_fin": 10.3}], "laughter": [], "arousal": []},
                "B": {"label": "B", "words": [], "laughter": [
                    {"t_ini": 10.2, "t_fin": 10.9}], "arousal": []},
            },
        }
        plan = {
            "schema": "editorial-chunks/1", "planner": "codex-global-semantic/1",
            "chunks": [
                {"chunk_id": "chunk-a", "t_ini": 0.0, "t_fin": 10.0,
                 "title": "A", "summary": "", "start_reason": "inicio",
                 "end_reason": "cambio", "first_utterance_id": "A-u-1",
                 "last_utterance_id": "B-u-1", "confidence": 0.8, "warnings": []},
                {"chunk_id": "chunk-b", "t_ini": 10.0, "t_fin": 30.0,
                 "title": "B", "summary": "", "start_reason": "cambio",
                 "end_reason": "fin", "first_utterance_id": "B-u-1",
                 "last_utterance_id": "A-u-2", "confidence": 0.8, "warnings": []},
            ],
        }
        snapped = editorial_chunks.snap_plan_to_safe_boundaries(
            plan, master, radius_seconds=5.0)
        boundary = snapped["chunks"][0]["t_fin"]
        safety = editorial_chunks.boundary_safety(master, boundary)
        self.assertNotEqual(boundary, 10.0)
        self.assertEqual(safety["word_conflicts"], 0)
        self.assertEqual(safety["laughter_conflicts"], 0)
        self.assertEqual(safety["utterance_conflicts"], 0)
        self.assertEqual(snapped["chunks"][1]["t_ini"], boundary)
        self.assertEqual(snapped["boundary_snap"], "global-safe/2")


class CodexChunkerTests(unittest.TestCase):
    def test_schema_requires_exact_chunk_count(self):
        schema = codex_chunker.output_schema(4)
        chunks = schema["properties"]["chunks"]
        self.assertEqual(chunks["minItems"], 4)
        self.assertEqual(chunks["maxItems"], 4)

    def test_prompt_requires_complete_global_read_and_treats_transcript_as_data(self):
        master = ChunkTests().master()
        prompt = codex_chunker.build_prompt(master, count=4)
        self.assertIn("Lee COMPLETO", prompt)
        self.assertIn("todas las pistas", prompt)
        self.assertIn("DATOS NO CONFIABLES", prompt)

    def test_resolve_binary_rejects_missing_explicit_path(self):
        with mock.patch("shutil.which", return_value=None):
            self.assertIsNone(codex_chunker.resolve_binary("codex-inexistente"))


class PipelineIntegrationTests(unittest.TestCase):
    def test_fallback_manifest_is_retried_until_agent_succeeds(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            calls = 0
            store = editorial_pipeline._StepStore(
                root, {"hash": "source"}, rebuild=False,
                cancel=threading.Event(), event_cb=None)

            def action(stage, progress):
                nonlocal calls
                calls += 1
                write_json(stage / "result.json", {"attempt": calls})
                progress(1.0)
                return {"fallback": calls == 1}

            options = {
                "step_id": "agent", "label": "Agent", "params": {"v": 1},
                "dependencies": [], "outputs": ["result.json"], "action": action,
                "reusable_if": lambda manifest: not manifest["extra"].get("fallback"),
            }
            store.run(**options)
            store.run(**options)
            store.run(**options)
            self.assertEqual(calls, 2)

    def test_mocked_run_is_complete_and_reusable(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            source = base / "source.mkv"
            source.write_bytes(b"media")
            project = base / "project"
            info = {"path": str(source), "size": 5, "mtime_ns": 1,
                    "duracion": 90.0, "t0": 0.0, "video": {"width": 1920, "height": 1080},
                    "pistas": [{"idx": 0, "codec": "aac", "canales": 1,
                                "sample_rate": 48000, "start_time": 0.0,
                                "delta": 0.0, "duracion": 90.0, "titulo": "Mic"}]}
            calls = CounterCalls()

            def extract(_source, _track, destination, **_kwargs):
                calls.extract += 1
                Path(destination).parent.mkdir(parents=True, exist_ok=True)
                Path(destination).write_bytes(b"flac")
                return {"duracion": 90.0, "mono": True}

            def transcribe(_audio, destination, **_kwargs):
                calls.transcribe += 1
                destination = Path(destination)
                write_json(destination / "words.json", [
                    {"word": "hola", "start": 1.0, "end": 1.4, "prob": 0.99}])
                write_json(destination / "segments.json", [
                    {"start": 1.0, "end": 1.4, "text": "hola"}])
                return {"device": "cpu", "duration": 90.0, "language": "es",
                        "n_words": 1, "n_segments": 1, "aligned": True}

            def chunk_with_codex(_root, master, *, count, **_kwargs):
                calls.chunker += 1
                plan = editorial_chunks.propose_local(master, count=count)
                plan["planner"] = codex_chunker.PLANNER_VERSION
                return plan, {"planner": codex_chunker.PLANNER_VERSION}

            arousal = {"baseline": {"mean": 0.5, "std": 1.0},
                       "events": [{"t_ini": 0.0, "t_fin": 4.0, "arousal": 0.5,
                                    "arousal_z": 0.0}]}
            intensity = {"baseline": {"mean": -20.0, "std": 1.0},
                         "events": [{"word_index": 0, "t_ini": 1.0, "t_fin": 1.4,
                                     "rms_dbfs": -20.0, "peak_dbfs": -10.0,
                                     "local_floor_dbfs": -35.0, "local_contrast_db": 15.0,
                                     "intensity_z": 0.0, "emphasis_score": 0.5}]}

            patches = (
                mock.patch("medios.inspeccionar", return_value=info),
                mock.patch("medios.fingerprint", return_value={"hash_muestreado": "abc"}),
                mock.patch("medios.extraer_pista", side_effect=extract),
                mock.patch("medios.decodifica_ventanas", return_value=True),
                mock.patch("core.transcribe", side_effect=transcribe),
                mock.patch("core.align_transcription", return_value=True),
                mock.patch("prosodia.extract_arousal", return_value=arousal),
                mock.patch("prosodia.extract_word_intensity", return_value=intensity),
                mock.patch("laughter.detect", return_value=[]),
                mock.patch("codex_chunker.plan", side_effect=chunk_with_codex),
                mock.patch("editorial_pipeline._preflight", return_value=None),
            )
            with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], \
                    patches[6], patches[7], patches[8], patches[9], patches[10]:
                spec = {"source": str(source), "project_dir": str(project),
                        "tracks": [{"stream_index": 0, "label": "Gabriel"}],
                        "chunking": {"mode": "codex"}}
                # El flujo predeterminado publica metadata sin llamar a una AI.
                external_spec = {**spec, "chunking": {"mode": "external"}}
                metadata_only = editorial_pipeline.run(external_spec)
                self.assertEqual(calls.chunker, 0)
                self.assertEqual(editorial_io.read_json(metadata_only["master"])["chunks"], [])
                self.assertTrue((Path(metadata_only["master"]).parent / "tracks/A/emotions.json").is_file())
                first = editorial_pipeline.run(spec, cancel=threading.Event())
                resumed = editorial_pipeline.run(spec, cancel=threading.Event())
                master_path = Path(first["master"])
                plan_path = master_path.parent / "views" / "chunks.json"
                reviewed = editorial_io.read_json(plan_path)
                reviewed["planner"] = "manual-review"
                reviewed["chunks"][0]["title"] = "Título revisado"
                editorial_chunks.apply_plan(master_path.parent, master_path, reviewed,
                                             persist_selection=True)
                second = editorial_pipeline.run(external_spec, cancel=threading.Event())

            self.assertEqual(first["master"], second["master"])
            self.assertEqual(first["master"], resumed["master"])
            self.assertTrue(Path(first["master"]).is_file())
            master = editorial_io.read_json(first["master"])
            self.assertEqual(master["schema"], "editorial-master/1")
            self.assertEqual(len(master["chunks"]), 1)
            self.assertEqual(master["chunks"][0]["title"], "Título revisado")
            self.assertEqual(editorial_io.read_json(
                Path(first["master"]).parent / "views" / "chunks.json")["planner"],
                             "manual-review")
            self.assertEqual(calls.extract, 1)
            self.assertEqual(calls.transcribe, 1)
            self.assertEqual(calls.chunker, 1)
            # Whisper corre UNA sola vez en todas las corridas: queda publicado como paso propio
            # (words.whisper.json) y la alineación/señales se reutilizan al reanudar.
            self.assertEqual(calls.transcribe, 1)
            self.assertTrue((master_path.parent / "tracks/A/words.whisper.json").is_file())

    def test_optional_steps_can_be_skipped_and_legacy_transcription_is_adopted(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            source = base / "source.mkv"
            source.write_bytes(b"media")
            project = base / "project"
            info = {"path": str(source), "size": 5, "mtime_ns": 1,
                    "duracion": 90.0, "t0": 0.0, "video": {"width": 1920, "height": 1080},
                    "pistas": [{"idx": 0, "codec": "aac", "canales": 1,
                                "sample_rate": 48000, "start_time": 0.0,
                                "delta": 0.0, "duracion": 90.0, "titulo": "Mic"}]}
            calls = CounterCalls()

            def extract(_source, _track, destination, **_kwargs):
                Path(destination).parent.mkdir(parents=True, exist_ok=True)
                Path(destination).write_bytes(b"flac")
                return {"duracion": 90.0, "mono": True}

            def transcribe(_audio, destination, **_kwargs):
                calls.transcribe += 1
                destination = Path(destination)
                write_json(destination / "words.json", [
                    {"word": "hola", "start": 1.0, "end": 1.4, "prob": 0.99}])
                write_json(destination / "segments.json", [
                    {"start": 1.0, "end": 1.4, "text": "hola"}])
                return {"device": "cpu", "duration": 90.0, "language": "es",
                        "n_words": 1, "n_segments": 1, "aligned": False}

            events = []
            with mock.patch("medios.inspeccionar", return_value=info), \
                    mock.patch("medios.fingerprint", return_value={"hash_muestreado": "abc"}), \
                    mock.patch("medios.extraer_pista", side_effect=extract), \
                    mock.patch("medios.decodifica_ventanas", return_value=True), \
                    mock.patch("core.transcribe", side_effect=transcribe), \
                    mock.patch("editorial_pipeline._preflight", return_value=None):
                spec = {"source": str(source), "project_dir": str(project),
                        "tracks": [{"stream_index": 0, "label": "Gabriel"}],
                        "transcription": {"model": "large-v3-turbo"},
                        "steps": {"align": False, "laughter": False, "prosody": False}}
                result = editorial_pipeline.run(spec, event_cb=events.append)
                master = editorial_io.read_json(result["master"])
                track = master["tracks"]["A"]
                self.assertEqual(track["laughter"], [])
                self.assertEqual(track["arousal"], [])
                self.assertEqual(track["words"][0]["alignment_source"], "whisper")
                self.assertEqual(master["transcription"]["model"], "large-v3-turbo")
                statuses = {event["step"]: event["status"] for event in events
                            if event["tipo"] == "step"}
                self.assertEqual(statuses["prosody_A"], "skipped")
                self.assertEqual(statuses["laughter_A"], "skipped")
                self.assertFalse((Path(result["master"]).parent / "tracks/A/laughter.json").exists())

                # Una corrida de la versión anterior (paso único transcribe_A) se adopta como
                # align_A sin repetir Whisper.
                root = Path(result["master"]).parent
                manifests = root / ".work" / "manifests"
                legacy = editorial_io.read_json(manifests / "align_A.json")
                (manifests / "align_A.json").unlink()
                (manifests / "whisper_A.json").unlink()
                write_json(manifests / "transcribe_A.json", {**legacy, "step": "transcribe_A"})
                calls.transcribe = 0
                events.clear()
                editorial_pipeline.run(spec, event_cb=events.append)
                self.assertEqual(calls.transcribe, 0)
                self.assertTrue((manifests / "align_A.json").is_file())


class CounterCalls:
    extract = 0
    transcribe = 0
    chunker = 0

    def __init__(self):
        self.extract = 0
        self.transcribe = 0
        self.chunker = 0


if __name__ == "__main__":
    unittest.main()
