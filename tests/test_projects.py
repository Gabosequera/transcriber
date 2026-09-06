"""Derivación multipista, exportación real e identidad de los hijos."""
import copy
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

import editorial_chunks
import editorial_io
import editorial_projects as projects
import medios
import podcast_export


def fixture(duration=12):
    tracks = {}
    for i, tid in enumerate(("A", "B")):
        words = [{"word_id": f"{tid}-w-{j}", "track_id": tid, "text": text,
                  "t_ini": a, "t_fin": b, "arousal": .7, "intensity_z": 1.2,
                  "emphasis_score": .8} for j, (a, b, text) in enumerate(
                      [(1, 2, "inicio"), (4, 5, "eliminado"), (8, 9, "retorno")])]
        tracks[tid] = {"track_id": tid, "label": tid, "stream_index": i, "offset": 0,
                       "words": words, "utterances": [{"utterance_id": f"{tid}-u-1",
                           "track_id": tid, "text": "inicio eliminado retorno", "t_ini": 1,
                           "t_fin": 9, "word_ids": [w["word_id"] for w in words], "signals": {}}],
                       "laughter": [{"event_id": f"{tid}-r-1", "t_ini": 2, "t_fin": 8, "max_conf": .9}],
                       "arousal": [{"event_id": f"{tid}-a-1", "t_ini": 0, "t_fin": 10,
                                    "arousal_z": .8, "valence": .6, "dominance": .5}],
                       "emotions": [{"event_id": f"{tid}-e-1", "t_ini": 8, "t_fin": 9, "value": .5}]}
    utterances = [u for t in tracks.values() for u in t["utterances"]]
    return {"schema": "editorial-master/1", "project": {"name": "padre"},
            "media": {"duration": duration, "fingerprint": {"size": 1, "hash_muestreado": "a",
                                                        "inventario_sha256": "b"}},
            "tracks": tracks, "conversation": {"utterances": utterances,
                "clean_utterance_ids": [u["utterance_id"] for u in utterances]}, "chunks": []}


class ProjectTests(unittest.TestCase):
    def test_segment_conversion_keeps_evidence_and_rebuilds_text(self):
        parent = fixture()
        before = copy.deepcopy(parent)
        info = {"path": "hijo.mp4", "duracion": 8, "t0": 0,
                "pistas": [{"idx": i, "delta": 0} for i in range(2)]}
        child = projects.derive_master(parent, info, {"size": 2}, [(0, 3), (7, 12)], name="hijo")
        self.assertEqual(parent, before)
        self.assertEqual(child["derivation"]["segments"][1]["child_ini"], 3)
        for track in child["tracks"].values():
            self.assertEqual([w["text"] for w in track["words"]], ["inicio", "retorno"])
            self.assertEqual(track["words"][1]["t_ini"], 4)
            self.assertEqual([(e["t_ini"], e["t_fin"]) for e in track["laughter"]], [(2, 3), (3, 4)])
            self.assertEqual(track["emotions"][0]["t_ini"], 4)
            self.assertEqual(track["words"][1]["intensity_z"], 1.2)
        text = " ".join(u["text"] for u in child["conversation"]["utterances"])
        self.assertNotIn("eliminado", text)
        grandchildren = projects.derive_master(child, {**info, "duracion": 2}, {}, [(3, 5)], name="nieto")
        self.assertIsNotNone(grandchildren["derivation"]["ancestor"])
        self.assertEqual(grandchildren["tracks"]["A"]["words"][0]["t_ini"], 1)

    def test_invalid_maps(self):
        for segments in ([], [(0, float("nan"))], [(True, 2)], [(0, 5), (4, 6)], [(0, 13)]):
            with self.subTest(segments=segments), self.assertRaises(ValueError):
                projects.time_map(segments, 12)

    @unittest.skipUnless(shutil.which("ffmpeg"), "FFmpeg no instalado")
    def test_real_child_export_and_audio_reuse(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "padre.mkv"
            subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i",
                "testsrc2=size=96x64:rate=25:duration=12", "-f", "lavfi", "-i",
                "sine=frequency=440:duration=12", "-map", "0:v", "-map", "1:a", "-map", "1:a",
                "-c:v", "libx264", "-c:a", "pcm_s16le", str(source)], check=True, **medios.flags_subprocess())
            parent = fixture()
            parent["media"]["fingerprint"] = medios.fingerprint(source)
            path = editorial_io.atomic_write_json(root / "padre.editorial.master.json", parent)
            import editorial_trims
            trims = editorial_trims.new_document(parent["media"]["fingerprint"], 12)
            editorial_trims.add_cut(trims, 3, 7, origin="user", reason="prueba")
            dest = podcast_export.export_plan(path, None, source, root / "salida", trims=trims)
            manifest = editorial_io.read_json(dest / "exports.json")
            entry = manifest["files"][0]
            child_path = dest / entry["project_master"]
            child = editorial_io.read_json(child_path)
            self.assertTrue(podcast_export.source_matches(child, dest / entry["file"]))
            self.assertEqual(child["tracks"]["B"]["words"][-1]["t_ini"], 4)
            paths = projects.ensure_track_audio(child_path, dest / entry["file"])
            with mock.patch.object(medios, "extraer_pista", side_effect=AssertionError("repetición")):
                self.assertEqual(paths, projects.ensure_track_audio(child_path, dest / entry["file"]))


if __name__ == "__main__":
    unittest.main()
