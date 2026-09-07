"""Recortes: heurística de silencios, documento revisable, propuesta de la AI y corte real."""
import copy
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import editorial_chunks
import editorial_io
import editorial_trims
import medios
import podcast_export


FINGERPRINT = {"size": 123, "hash_muestreado": "abc", "inventario_sha256": "def"}


def word(track, start, end, text):
    return {"word_id": f"{track}-w-{int(start * 100):06d}", "track_id": track,
            "t_ini": start, "t_fin": end, "text": text}


def master_fixture(duration=60.0):
    """Dos pistas: A habla al inicio y al final, B dice una palabra y se ríe a los 20 s."""
    words_a = [word("A", 2.0, 2.3, "hola"), word("A", 2.5, 2.9, "mundo"),
               word("A", 10.0, 10.4, "bien"), word("A", 40.0, 40.5, "fin")]
    words_b = [word("B", 12.0, 12.3, "sí")]
    utterances = [
        {"utterance_id": "A-u-000001", "track_id": "A", "t_ini": 2.0, "t_fin": 2.9,
         "text": "hola mundo", "word_ids": [w["word_id"] for w in words_a[:2]], "signals": {}},
        {"utterance_id": "A-u-000002", "track_id": "A", "t_ini": 10.0, "t_fin": 10.4,
         "text": "bien", "word_ids": [words_a[2]["word_id"]], "signals": {}},
        {"utterance_id": "B-u-000001", "track_id": "B", "t_ini": 12.0, "t_fin": 12.3,
         "text": "sí", "word_ids": [words_b[0]["word_id"]], "signals": {}},
        {"utterance_id": "A-u-000003", "track_id": "A", "t_ini": 40.0, "t_fin": 40.5,
         "text": "fin", "word_ids": [words_a[3]["word_id"]], "signals": {}},
    ]
    return {
        "schema": "editorial-master/1", "project": {"name": "demo", "profile": "editorial_voz"},
        "media": {"path": "demo.mkv", "duration": duration, "t0": 0.0, "fingerprint": dict(FINGERPRINT)},
        "transcription": {"model": "tiny"},
        "tracks": {
            "A": {"track_id": "A", "label": "Gabriel", "words": words_a, "laughter": [], "arousal": []},
            "B": {"track_id": "B", "label": "Amigo", "words": words_b,
                  "laughter": [{"event_id": "B-laugh-00001", "track_id": "B", "t_ini": 20.0,
                                "t_fin": 21.0, "conf": 0.9, "max_conf": 0.9}], "arousal": []},
        },
        "conversation": {"utterances": utterances,
                         "clean_utterance_ids": [u["utterance_id"] for u in utterances],
                         "overlap_groups": [], "duplicate_groups": []},
        "chunks": [],
    }


def plan_fixture(master, boundaries):
    return {"schema": "editorial-chunks/1", "planner": "test",
            "source_master_digest": editorial_chunks.source_master_digest(master),
            "chunks": [{"chunk_id": f"chunk-{i:03d}", "t_ini": a, "t_fin": b, "title": f"Tema {i}",
                        "summary": f"resumen {i}", "topics": [f"tema {i}"], "confidence": 0.8,
                        "warnings": []}
                       for i, (a, b) in enumerate(zip(boundaries, boundaries[1:]), 1)]}


class SilenceHeuristicTests(unittest.TestCase):
    def test_gaps_skip_words_and_laughter_of_every_track_and_keep_margins(self):
        master = master_fixture()
        analysis = editorial_trims.analyze_silences(master, params={"min_gap": 1.0})
        cuts = analysis["cuts"]
        self.assertEqual(len(cuts), 6)
        self.assertEqual(cuts[0]["t_ini"], 0.0)                       # aire inicial desde 0
        self.assertEqual(cuts[-1]["t_fin"], 60.0)                     # aire final hasta el fin
        self.assertAlmostEqual(cuts[1]["t_ini"], 2.9 + 0.2 + 0.3, places=3)
        self.assertAlmostEqual(cuts[1]["t_fin"], 10.0 - 0.1 - 0.3, places=3)
        self.assertEqual(cuts[1]["evidence"]["prev_word"]["text"], "mundo")
        self.assertEqual(cuts[1]["evidence"]["next_word"]["text"], "bien")
        self.assertTrue(all(cut["enabled"] and cut["origin"] == "silence" for cut in cuts))
        index = editorial_trims.BoundaryIndex(master)
        for cut in cuts:
            self.assertEqual(index.cut_warnings(cut), [])
            # ningún recorte toca la risa de B [20, 21]
            self.assertFalse(cut["t_ini"] < 21.25 and cut["t_fin"] > 19.75)
        self.assertEqual(analysis["stats"]["cuts"], 6)
        self.assertGreater(analysis["stats"]["removed_seconds"], 40)

    def test_min_gap_and_min_cut_filter_short_gaps(self):
        master = master_fixture()
        analysis = editorial_trims.analyze_silences(master, params={"min_gap": 5.0})
        self.assertTrue(all(cut["evidence"]["gap_seconds"] >= 5.0 for cut in analysis["cuts"]))
        with self.assertRaises(ValueError):
            editorial_trims.analyze_silences(master, params={"min_gap": 0})

    def test_activity_in_a_gap_leaves_the_cut_disabled_but_visible(self):
        master = master_fixture()
        buckets = 1200
        envelope = [(-0.001, 0.001, 0.001)] * buckets
        for bucket in range(int(5 / 60 * buckets), int(8 / 60 * buckets)):
            envelope[bucket] = (-0.2, 0.2, 0.1)
        with tempfile.TemporaryDirectory() as tmp:
            audio = Path(tmp) / "audio.flac"
            audio.write_bytes(b"x")
            with mock.patch("medios.envolvente", return_value=envelope):
                analysis = editorial_trims.analyze_silences(
                    master, audio_paths={"A": audio, "B": Path(tmp) / "missing.flac"})
        self.assertEqual(analysis["tracks_measured"], ["A"])
        active = [cut for cut in analysis["cuts"] if not cut["enabled"]]
        self.assertEqual(len(active), 1)
        self.assertLess(active[0]["t_ini"], 5.0)
        self.assertGreater(active[0]["t_fin"], 8.0)
        self.assertIn("actividad", active[0]["reason"])
        self.assertEqual(active[0]["evidence"]["active_tracks"], ["A"])
        self.assertEqual(active[0]["confidence"], 0.4)

    def test_reanalysis_keeps_user_edits_and_disabled_state(self):
        master = master_fixture()
        document = editorial_trims.new_document(FINGERPRINT, 60.0)
        analysis = editorial_trims.analyze_silences(master)
        editorial_trims.apply_silence_analysis(document, analysis)
        self.assertEqual(len(document["cuts"]), 6)
        ids = [cut["cut_id"] for cut in document["cuts"]]
        document["cuts"][1]["enabled"] = False                       # desactivado (sin editar)
        moved = document["cuts"][3]
        moved["t_ini"] += 1.0
        moved["edited"] = True                                       # movido por el usuario
        editorial_trims.add_cut(document, 25.0, 26.0, origin="user", reason="manual")   # dentro de un silencio
        editorial_trims.add_cut(document, 2.1, 2.6, origin="user", reason="aparte")     # en zona con voz
        editorial_trims.apply_silence_analysis(document, editorial_trims.analyze_silences(master))
        cuts = {cut["cut_id"]: cut for cut in document["cuts"]}
        # el corte manual solapado se funde con el silencio del mismo carril (§10); el otro sigue aparte
        self.assertEqual(len(document["cuts"]), 7)
        self.assertFalse(cuts[ids[1]]["enabled"])
        self.assertTrue(cuts[ids[3]]["edited"])
        self.assertEqual(cuts[ids[3]]["t_ini"], moved["t_ini"])
        self.assertEqual(sum(cut["origin"] == "user" for cut in document["cuts"]), 1)
        self.assertEqual(sum(cut["origin"] == "silence" for cut in document["cuts"]), 6)
        absorbed = next(cut for cut in document["cuts"] if cut["t_ini"] <= 25.0 <= cut["t_fin"])
        self.assertEqual(absorbed["origin"], "silence")
        self.assertIn("manual", absorbed["reason"])
        self.assertTrue(all(cut["lane"] == "main" for cut in document["cuts"]))


class DocumentTests(unittest.TestCase):
    def test_roundtrip_identity_and_revision(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "trims.json"
            document = editorial_trims.new_document(FINGERPRINT, 60.0)
            editorial_trims.add_cut(document, 30.0, 31.0, origin="user")
            editorial_trims.add_cut(document, 5.0, 6.0, origin="user")
            editorial_trims.save_document(path, document)
            self.assertEqual(document["revision"], 1)
            loaded = editorial_trims.load_document(path, fingerprint=FINGERPRINT, duration=60.0)
            self.assertEqual([cut["t_ini"] for cut in loaded["cuts"]], [5.0, 30.0])
            with self.assertRaisesRegex(ValueError, "otro video"):
                editorial_trims.load_document(path, fingerprint={**FINGERPRINT, "size": 1})
            with self.assertRaisesRegex(ValueError, "otra duración"):
                editorial_trims.load_document(path, fingerprint=FINGERPRINT, duration=90.0)
            self.assertIsNone(editorial_trims.load_document(Path(tmp) / "nada.json"))

    def test_validation_rejects_bad_cuts(self):
        document = editorial_trims.new_document(FINGERPRINT, 60.0)
        with self.assertRaisesRegex(ValueError, "al menos"):
            editorial_trims.add_cut(document, 10.0, 10.01)
        document["cuts"].append({"cut_id": "cut-000009", "t_ini": 1.0, "t_fin": 2.0, "origin": "magia"})
        with self.assertRaisesRegex(ValueError, "origen"):
            editorial_trims.validate_document(document)

    def test_union_kept_segments_and_stats(self):
        document = editorial_trims.new_document(FINGERPRINT, 12.0)
        editorial_trims.add_cut(document, 1.0, 3.0)
        editorial_trims.add_cut(document, 2.0, 4.0)
        disabled = editorial_trims.add_cut(document, 5.0, 6.0)
        disabled["enabled"] = False
        editorial_trims.add_cut(document, 9.98, 10.05)
        editorial_trims.add_cut(document, 10.05, 11.0)
        self.assertEqual(editorial_trims.enabled_intervals(document), [(1.0, 4.0), (9.98, 11.0)])
        self.assertEqual(editorial_trims.kept_segments(editorial_trims.enabled_intervals(document), 0, 12),
                         [(0.0, 1.0), (4.0, 9.98), (11.0, 12.0)])
        self.assertEqual(editorial_trims.kept_segments([(4.0, 9.98), (10.05, 12.0)], 0, 12),
                         [(0.0, 4.0)])                               # resto de 0.07 s se funde
        summary = editorial_trims.stats(document)
        self.assertEqual((summary["total"], summary["enabled"], summary["disabled"]), (5, 4, 1))
        self.assertAlmostEqual(summary["removed_seconds"], 3.0 + 1.02, places=3)
        self.assertNotEqual(editorial_trims.export_identity(document),
                            editorial_trims.export_identity(None))


class ProposalTests(unittest.TestCase):
    def proposal(self, master, cuts):
        return {"schema": editorial_trims.SCHEMA_PROPOSAL, "planner": "test-ai",
                "source_master_digest": editorial_chunks.source_master_digest(master), "cuts": cuts}

    def test_boundaries_snap_out_of_words_and_bad_references_are_rejected(self):
        master = master_fixture()
        plan = plan_fixture(master, [0, 30, 60])
        proposal = self.proposal(master, [{"chunk_id": "chunk-001", "t_ini": 2.1, "t_fin": 12.1,
                                           "first_utterance_id": "A-u-000001",
                                           "reason": "tangente", "confidence": 0.7}])
        validated = editorial_trims.validate_proposal(proposal, master, plan)
        cut = validated["cuts"][0]
        self.assertNotEqual(cut["t_ini"], 2.1)
        self.assertNotEqual(cut["t_fin"], 12.1)
        for edge in (cut["t_ini"], cut["t_fin"]):
            safety = editorial_chunks.boundary_safety(master, edge)
            self.assertEqual(safety["word_conflicts"] + safety["laughter_conflicts"], 0)
        self.assertEqual(cut["semantic_t_ini"], 2.1)
        self.assertEqual(cut["chunk_id"], "chunk-001")
        self.assertEqual(cut["warnings"], [])
        # sin chunk_id se deduce por solape; sin motivo se avisa
        loose = editorial_trims.validate_proposal(
            self.proposal(master, [{"t_ini": 31.0, "t_fin": 35.0, "confidence": 0.5}]), master, plan)
        self.assertEqual(loose["cuts"][0]["chunk_id"], "chunk-002")
        self.assertTrue(any("no explicó" in w for w in loose["cuts"][0]["warnings"]))
        with self.assertRaisesRegex(ValueError, "source_master_digest"):
            editorial_trims.validate_proposal({**proposal, "source_master_digest": "x"}, master, plan)
        with self.assertRaisesRegex(ValueError, "chunk_id desconocido"):
            editorial_trims.validate_proposal(
                self.proposal(master, [{"chunk_id": "nope", "t_ini": 3, "t_fin": 4}]), master, plan)
        with self.assertRaisesRegex(ValueError, "desconocido"):
            editorial_trims.validate_proposal(
                self.proposal(master, [{"t_ini": 3, "t_fin": 4, "first_utterance_id": "Z-u-9"}]),
                master, plan)
        with self.assertRaisesRegex(ValueError, "rango inválido"):
            editorial_trims.validate_proposal(
                self.proposal(master, [{"t_ini": 3, "t_fin": 3.05}]), master, plan)

    def test_merge_replaces_only_untouched_ai_cuts_and_is_idempotent(self):
        master = master_fixture()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            document = editorial_trims.new_document(FINGERPRINT, 60.0)
            old_ai = editorial_trims.add_cut(document, 45.0, 46.0, origin="ai", reason="viejo")
            kept_ai = editorial_trims.add_cut(document, 50.0, 51.0, origin="ai", reason="movido")
            kept_ai["edited"] = True
            user = editorial_trims.add_cut(document, 55.0, 56.0, origin="user")
            trims_path = root / "trims.json"
            editorial_trims.save_document(trims_path, document)
            proposal_path = root / "trims.proposed.json"
            proposal_path.write_text(json.dumps(self.proposal(master, [
                {"t_ini": 30.5, "t_fin": 33.0, "reason": "balbuceo", "confidence": 0.6}])),
                encoding="utf-8-sig")
            merged, proposal = editorial_trims.import_proposal(trims_path, proposal_path, master, None,
                                                               fingerprint=FINGERPRINT)
            ids = {cut["cut_id"] for cut in merged["cuts"]}
            self.assertNotIn(old_ai["cut_id"], ids)
            self.assertIn(kept_ai["cut_id"], ids)
            self.assertIn(user["cut_id"], ids)
            new = [cut for cut in merged["cuts"] if cut["origin"] == "ai" and not cut["edited"]]
            self.assertEqual(len(new), 1)
            self.assertEqual(new[0]["evidence"]["planner"], "test-ai")
            self.assertEqual(new[0]["reason"], "balbuceo")
            revision = merged["revision"]
            again, _ = editorial_trims.import_proposal(trims_path, proposal_path, master, None,
                                                       fingerprint=FINGERPRINT)
            self.assertEqual(again["revision"], revision)               # mismo digest: no-op
            self.assertEqual(len(again["cuts"]), len(merged["cuts"]))


class ReviewPackageTests(unittest.TestCase):
    def test_review_marks_enabled_cuts_and_request_carries_the_rules(self):
        master = master_fixture()
        plan = plan_fixture(master, [0, 30, 60])
        document = editorial_trims.new_document(FINGERPRINT, 60.0)
        editorial_trims.apply_silence_analysis(document, editorial_trims.analyze_silences(master))
        document["cuts"][1]["enabled"] = False
        with tempfile.TemporaryDirectory() as tmp:
            paths = editorial_trims.write_review_package(tmp, master, plan, document)
            review = paths["chunk-001"].read_text(encoding="utf-8")
            self.assertIn("chunks/chunk-001/trim-review.md", str(paths["chunk-001"]).replace("\\", "/"))
            self.assertIn("⟂ RECORTE `cut-000001`", review)
            self.assertNotIn(f"⟂ RECORTE `{document['cuts'][1]['cut_id']}`", review)
            self.assertIn("Temas: tema 1", review)
            self.assertIn("`A-u-000001`", review)
            self.assertNotIn("`A-u-000003`", review)                   # pertenece al bloque 2
            request = paths["request"].read_text(encoding="utf-8")
            self.assertIn(editorial_chunks.source_master_digest(master), request)
            self.assertIn("NO propongas recortes por humor", request)
            self.assertIn("trims.proposed.json", request)
            single = editorial_trims.write_review_package(tmp, master, None, document)
            self.assertTrue(str(single["completo"]).endswith("trim-review.md"))
            self.assertIn("views", str(single["completo"]))


class FilterScriptTests(unittest.TestCase):
    def test_script_covers_every_stream_and_segment(self):
        script = podcast_export.filter_script([(0.0, 1.0), (2.0, 4.0)], 2, True)
        self.assertIn("[0:v:0]split=2[v_0][v_1]", script)
        self.assertIn("[0:a:1]asplit=2", script)
        self.assertIn("atrim=start=2.000000:end=4.000000,asetpts=PTS-2.000000/TB", script)
        self.assertIn("concat=n=2:v=1:a=2[vc][ac0][ac1]", script)
        self.assertIn("[vout]", script)
        audio_only = podcast_export.filter_script([(0.0, 1.0)], 1, False)
        self.assertIn("concat=n=1:v=0:a=1[ac0]", audio_only)
        self.assertNotIn("split=", audio_only.replace("asplit=", ""))
        with self.assertRaises(ValueError):
            podcast_export.filter_script([], 1, True)


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg no instalado")
class TrimExportTests(unittest.TestCase):
    def synthetic(self, root):
        source = root / "podcast con recortes.mkv"
        subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i",
                        "testsrc2=size=96x64:rate=25:duration=6", "-f", "lavfi", "-i",
                        "sine=frequency=440:duration=6", "-itsoffset", "0.4", "-f", "lavfi", "-i",
                        "sine=frequency=880:duration=5.6", "-map", "0:v", "-map", "1:a",
                        "-map", "2:a", "-c:v", "libx264", "-g", "150", "-c:a", "pcm_s16le",
                        str(source)], check=True, **medios.flags_subprocess())
        info = medios.inspeccionar(source)
        master = {"schema": "editorial-master/1", "project": {"name": "demo"},
                  "media": {"duration": info["duracion"], "fingerprint": medios.fingerprint(source, info)},
                  "conversation": {"utterances": [], "clean_utterance_ids": []}, "tracks": {}}
        return source, info, editorial_io.atomic_write_json(root / "master.json", master), master

    @staticmethod
    def frame(path, time):
        return subprocess.run(["ffmpeg", "-v", "error", "-ss", str(time), "-i", str(path),
                               "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "gray", "-"],
                              capture_output=True, check=True, **medios.flags_subprocess()).stdout

    def test_cuts_are_removed_frame_accurately_with_all_audio_tracks(self):
        import numpy as np
        with tempfile.TemporaryDirectory(prefix="recortes ") as tmp:
            root = Path(tmp)
            source, info, master_path, master = self.synthetic(root)
            trims = editorial_trims.new_document(master["media"]["fingerprint"], info["duracion"])
            editorial_trims.add_cut(trims, 1.0, 2.0, origin="silence", reason="hueco")
            editorial_trims.add_cut(trims, 4.0, 4.5, origin="ai", reason="tangente")
            ignored = editorial_trims.add_cut(trims, 5.0, 5.5, origin="user")
            ignored["enabled"] = False
            logs = []
            output = podcast_export.export_plan(master_path, None, source, root / "salidas",
                                                trims=trims, log_cb=logs.append)
            exports = editorial_io.read_json(output / "exports.json")
            self.assertTrue(exports["trimmed"])
            accepted = editorial_io.read_json(output / "accepted-trims.json")
            self.assertEqual(accepted["intervals"], [[1.0, 2.0], [4.0, 4.5]])
            self.assertEqual(len(accepted["cuts"]), 2)
            exported = medios.inspeccionar(output / "001.mp4")
            self.assertAlmostEqual(exported["duracion"], info["duracion"] - 1.5, delta=0.2)
            self.assertEqual(len(exported["pistas"]), 2)
            self.assertAlmostEqual(exported["pistas"][1]["delta"], 0.4, delta=0.06)
            self.assertEqual(exports["files"][0]["segments"], [[0.0, 1.0], [2.0, 4.0], [4.5, 6.0]])
            # el segundo segmento arranca en la fuente a 2.0 s: en la salida vive en 1.0 s
            expected = np.frombuffer(self.frame(source, 2.5), dtype=np.uint8).astype(float)
            actual = np.frombuffer(self.frame(output / "001.mp4", 1.5), dtype=np.uint8).astype(float)
            self.assertLess(np.abs(expected - actual).mean(), 6)
            wrong = np.frombuffer(self.frame(source, 1.5), dtype=np.uint8).astype(float)
            self.assertGreater(np.abs(wrong - actual).mean(), 6)
            self.assertFalse(list(output.glob("*.filters.txt")))
            self.assertTrue(any("segmentos" in line for line in logs))

    def test_cut_across_block_boundary_and_plain_export_stays_untouched(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source, info, master_path, master = self.synthetic(root)
            plan = plan_fixture(master, [0, 3.0, info["duracion"]])
            trims = editorial_trims.new_document(master["media"]["fingerprint"], info["duracion"])
            editorial_trims.add_cut(trims, 2.5, 3.5, origin="user")
            output = podcast_export.export_plan(master_path, plan, source, root / "out", trims=trims)
            first = medios.inspeccionar(output / "001.mp4")
            second = medios.inspeccionar(output / "002.mp4")
            self.assertAlmostEqual(first["duracion"], 2.5, delta=0.15)
            self.assertAlmostEqual(second["duracion"], info["duracion"] - 3.5, delta=0.15)
            # el mismo plan sin recortes va a otra carpeta y no se recorta
            plain = podcast_export.export_plan(master_path, plan, source, root / "out")
            self.assertNotEqual(plain, output)
            self.assertAlmostEqual(medios.inspeccionar(plain / "001.mp4")["duracion"], 3.0, delta=0.15)
            self.assertFalse(editorial_io.read_json(plain / "exports.json")["trimmed"])
            with self.assertRaisesRegex(ValueError, "otro video"):
                podcast_export.export_plan(master_path, plan, source, root / "x",
                                           trims={**trims, "media": {"size": 1}})


if __name__ == "__main__":
    unittest.main()
