from contextlib import ExitStack
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from types import SimpleNamespace

import core
import diarization
import editorial_master
import editorial_pipeline
import editorial_projects
from editorial_io import atomic_write_json, read_json


def sample_words():
    return [{"word": "Hola", "start": 1.0, "end": 1.8, "prob": 0.95},
            {"word": "sí", "start": 1.7, "end": 2.4, "prob": 0.9},
            {"word": "claro", "start": 2.4, "end": 2.9, "prob": 0.9}]


def sample_document():
    return {"schema": "transcriptor-diarization/1", "model": diarization.MODEL,
            "turns": [{"start": 0.8, "end": 1.9, "speaker_id": "SPEAKER_00"},
                      {"start": 1.7, "end": 3.0, "speaker_id": "SPEAKER_01"}],
            "exclusive_turns": [{"start": 0.8, "end": 1.8, "speaker_id": "SPEAKER_00"},
                                {"start": 1.8, "end": 3.0, "speaker_id": "SPEAKER_01"}]}


class AssignmentTests(unittest.TestCase):
    def test_split_at_speaker_change_keeps_each_word_once(self):
        raw = sample_words()
        words, segments = diarization.assign_speakers(raw, [
            {"start": 1, "end": 3, "text": "Hola sí claro"}], sample_document())
        self.assertEqual([w["speaker_id"] for w in words], ["SPEAKER_00", "SPEAKER_01", "SPEAKER_01"])
        self.assertEqual([s["text"] for s in segments], ["Hola", "sí claro"])
        self.assertEqual([i for s in segments for i in s["word_indices"]], [0, 1, 2])
        self.assertEqual([(w["start"], w["end"]) for w in words],
                         [(w["start"], w["end"]) for w in raw])
        self.assertNotIn("speaker_id", raw[0])

    def test_unmatched_word_stays_unknown_instead_of_inventing_speaker(self):
        words, segments = diarization.assign_speakers(
            [{"word": "fuera", "start": 10, "end": 11}], [], sample_document())
        self.assertIsNone(words[0]["speaker_id"])
        self.assertEqual(words[0]["speaker_assignment"], "unknown")
        self.assertEqual(segments[0]["text"], "fuera")

    def test_small_timestamp_gap_uses_nearest_turn(self):
        words, _ = diarization.assign_speakers(
            [{"word": "fin", "start": 3.1, "end": 3.2}], [], sample_document())
        self.assertEqual(words[0]["speaker_id"], "SPEAKER_01")
        self.assertEqual(words[0]["speaker_assignment"], "nearest")

    def test_speaker_count_rejects_invalid_values(self):
        self.assertIsNone(diarization.speaker_count("Auto"))
        self.assertEqual(diarization.speaker_count("2"), 2)
        for value in (-1, 0, 33, 1.5, True, "oops"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                diarization.speaker_count(value)

    def test_segment_without_word_timestamps_keeps_its_text(self):
        words, segments = diarization.assign_speakers([], [
            {"start": 1, "end": 2, "text": "Sin tiempos de palabra"}], sample_document())
        self.assertEqual(words, [])
        self.assertEqual(segments[0]["text"], "Sin tiempos de palabra")
        self.assertIsNone(segments[0]["speaker_id"])

    def test_master_preserves_speakers_and_exact_word_membership(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            words, segments = diarization.assign_speakers(sample_words(), [
                {"start": 1, "end": 3, "text": "Hola sí claro"}], sample_document())
            tracks = []
            for track_id in ("A", "B"):
                folder = root / track_id
                tracks.append({"track_id": track_id, "stream_index": len(tracks), "label": "Mezcla",
                               "words_path": atomic_write_json(folder / "words.json", words),
                               "utterances_path": atomic_write_json(folder / "segments.json", segments),
                               "diarization_path": atomic_write_json(folder / "diarization.json", sample_document())})
            master = editorial_master.build_master(media={"path": "audio.wav", "duracion": 4},
                tracks=tracks, project_name="mixed", fingerprint={}, transcription={})
        self.assertEqual(set(master["speakers"]), {
            "A-SPEAKER_00", "A-SPEAKER_01", "B-SPEAKER_00", "B-SPEAKER_01"})
        utterances = master["tracks"]["A"]["utterances"]
        self.assertEqual([w for u in utterances for w in u["word_ids"]],
                         ["A-w-000001", "A-w-000002", "A-w-000003"])
        self.assertEqual(master["tracks"]["A"]["diarization"]["turns"][1]["start"], 1.7)
        self.assertIn("Hablante 2 (", editorial_master.conversation_markdown(master))
        child = editorial_projects.derive_master(master,
            {"path": "clip.wav", "duracion": 1.5, "t0": 0, "pistas": [
                {"idx": 0, "delta": 0}, {"idx": 1, "delta": 0}]}, {}, [(1.5, 3)], name="clip")
        self.assertEqual(child["speakers"], master["speakers"])
        self.assertEqual(child["tracks"]["A"]["diarization"]["exclusive_turns"][0]["start"], 0)
        self.assertTrue(all(0 <= t["start"] < t["end"] <= 1.5
            for t in child["tracks"]["A"]["diarization"]["turns"]))


class PipelineTests(unittest.TestCase):
    def test_manual_transcription_writes_speakers_to_json_and_subtitles(self):
        segment = SimpleNamespace(id=0, start=1, end=3, text="Hola sí claro",
            words=[SimpleNamespace(word=w["word"], start=w["start"], end=w["end"],
                                   probability=w["prob"]) for w in sample_words()])
        whisper_model = mock.Mock()
        whisper_model.transcribe.return_value = ([segment], SimpleNamespace(
            duration=4, language="es", language_probability=1.0))
        with tempfile.TemporaryDirectory() as temporary, \
                mock.patch("faster_whisper.WhisperModel", return_value=whisper_model), \
                mock.patch("core.resolve_device", return_value="cpu"), \
                mock.patch("diarization.check_access"), \
                mock.patch("diarization.detect", return_value=sample_document()):
            result = core.transcribe("source.wav", temporary, want_diarization=True, num_speakers=2)
            words = read_json(result["written"]["words.json"])
            phrases = read_json(result["written"]["segments.json"])
            self.assertEqual(words[0]["speaker_id"], "SPEAKER_00")
            self.assertEqual(phrases[1]["speaker_id"], "SPEAKER_01")
            self.assertIn("[SPEAKER_01] sí claro", Path(result["written"]["srt"]).read_text(encoding="utf-8"))
            self.assertEqual(read_json(result["written"]["diarization.json"])["model"], diarization.MODEL)

    def test_missing_hugging_face_access_fails_before_transcription(self):
        with mock.patch.dict("os.environ", {"HF_TOKEN": ""}), \
                mock.patch("hardware.load", return_value={}), \
                mock.patch("huggingface_hub.get_token", return_value=None), \
                mock.patch("huggingface_hub.try_to_load_from_cache", return_value=None), \
                mock.patch("diarization.available", return_value=True):
            with self.assertRaisesRegex(RuntimeError, "Configura el token"):
                editorial_pipeline._preflight({"diarization": True})

    def test_enabling_changing_and_disabling_diarization_reuses_whisper(self):
        with tempfile.TemporaryDirectory() as temporary, ExitStack() as stack:
            root = Path(temporary)
            source = root / "source.wav"
            source.write_bytes(b"audio")
            info = {"path": str(source), "duracion": 4, "pistas": [{"idx": 0, "delta": 0}]}

            def extract(_source, _track, destination, **_kwargs):
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(b"flac")
                return {}

            def transcribe(_audio, destination, **_kwargs):
                atomic_write_json(destination / "words.json", sample_words())
                atomic_write_json(destination / "segments.json", [
                    {"start": 1, "end": 3, "text": "Hola sí claro"}])
                return {}

            stack.enter_context(mock.patch("medios.inspeccionar", return_value=info))
            stack.enter_context(mock.patch("medios.fingerprint", return_value={"hash": "x"}))
            stack.enter_context(mock.patch("medios.extraer_pista", side_effect=extract))
            stack.enter_context(mock.patch("medios.decodifica_ventanas", return_value=True))
            stack.enter_context(mock.patch("editorial_pipeline._preflight"))
            whisper = stack.enter_context(mock.patch("core.transcribe", side_effect=transcribe))
            detector = stack.enter_context(mock.patch("diarization.detect", return_value=sample_document()))
            spec = {"source": str(source), "project_dir": str(root / "project"),
                    "tracks": [{"stream_index": 0}],
                    "steps": {"align": False, "prosody": False, "laughter": False}}
            first = editorial_pipeline.run(spec)
            self.assertEqual(read_json(first["master"])["speakers"], {})
            spec["steps"]["diarization"] = True
            spec["diarization"] = {"num_speakers": 2}
            result = editorial_pipeline.run(spec)
            master = read_json(result["master"])
            self.assertEqual(len(master["speakers"]), 2)
            self.assertEqual(len(master["conversation"]["utterances"]), 2)
            editorial_pipeline.run(spec)
            self.assertEqual(detector.call_count, 1)
            spec["diarization"]["num_speakers"] = "Auto"
            editorial_pipeline.run(spec)
            self.assertEqual(detector.call_count, 2)
            spec["steps"]["diarization"] = False
            result = editorial_pipeline.run(spec)
            master = read_json(result["master"])
            self.assertEqual(master["speakers"], {})
            self.assertTrue(all(w["speaker_id"] is None for w in master["tracks"]["A"]["words"]))
            self.assertEqual(whisper.call_count, 1)
