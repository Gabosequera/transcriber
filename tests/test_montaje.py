"""Montaje (editorial_montaje.py, plan §7): aplanado con dos pistas (la de arriba tapa),
insert vs overwrite, split/trim/move/remove con ripple, secuencia↔fuente en tramos
repetidos, validación de solapes en la misma pista, `ensure_track`, `stats`, carga de
un documento viejo sin `tracks`. Sin Tk."""
import json
import tempfile
import unittest
from pathlib import Path

import editorial_montaje as montaje

FP = {"size": 10, "hash_muestreado": "m", "inventario_sha256": "i"}


def doc_with(*ranges, track="V1"):
    doc = montaje.new_document(FP, 100.0)
    for a, b in ranges:
        doc, _ = montaje.add_clip(doc, a, b, track=track)
    return doc


def spans(doc, key="seq"):
    return [(c[f"{key}_ini"], montaje.seq_fin(c) if key == "seq" else c["source_fin"]) for c in doc["clips"]]


class ModelTests(unittest.TestCase):
    def test_add_clips_end_to_end_and_spare_track(self):
        doc = doc_with((10, 15), (40, 42), (70, 80))
        self.assertEqual(spans(doc), [(0.0, 5.0), (5.0, 7.0), (7.0, 17.0)])
        self.assertEqual([c["clip_id"] for c in doc["clips"]], ["clip-000001", "clip-000002", "clip-000003"])
        self.assertEqual(montaje.track_ids(doc), ["V1", "V2"])          # una vacía encima, siempre
        self.assertEqual(montaje.total_seconds(doc), 17.0)
        self.assertEqual(montaje.extent(doc), 17.0)
        summary = montaje.stats(doc)
        self.assertEqual((summary["total"], summary["enabled"], summary["tracks"]), (3, 3, 2))
        with self.assertRaisesRegex(ValueError, "solapar"):
            montaje.add_clip(doc, 0, 3, at=4.0)
        with self.assertRaisesRegex(ValueError, "fotograma"):
            montaje.add_clip(doc, 1, 1.01)
        with self.assertRaisesRegex(ValueError, "rango fuente"):
            montaje.validate_document({**doc, "clips": [{**doc["clips"][0], "source_fin": 120}]})

    def test_flatten_top_track_covers_bottom_at_start_middle_and_end(self):
        doc = doc_with((10, 30))                                          # V1: 0–20
        doc, cover = montaje.add_clip(doc, 50, 53, at=0.0, track="V2")    # tapa el principio
        doc, middle = montaje.add_clip(doc, 60, 62, at=8.0, track="V2")   # tapa el medio
        doc, tail = montaje.add_clip(doc, 70, 74, at=18.0, track="V2")    # cruza el final
        self.assertEqual(montaje.track_ids(doc), ["V1", "V2", "V3"])
        pieces = montaje.flatten(doc)
        self.assertEqual([(p["clip_id"], p["source_ini"], p["source_fin"]) for p in pieces], [
            (cover["clip_id"], 50.0, 53.0), ("clip-000001", 13.0, 18.0), (middle["clip_id"], 60.0, 62.0),
            ("clip-000001", 20.0, 28.0), (tail["clip_id"], 70.0, 74.0)])
        self.assertEqual([(p["seq_ini"], p["seq_fin"]) for p in pieces],
                         [(0.0, 3.0), (3.0, 8.0), (8.0, 10.0), (10.0, 18.0), (18.0, 22.0)])
        self.assertEqual(montaje.total_seconds(doc), 22.0)
        # desactivar el de arriba destapa el de abajo; el desactivado no se reproduce ni exporta
        doc, _ = montaje.set_state(doc, [middle["clip_id"]], "disabled")
        self.assertEqual(len(montaje.flatten(doc)), 3)                    # V1 vuelve a ser un tramo seguido
        self.assertEqual(montaje.flatten(doc)[1], {"seq_ini": 3.0, "seq_fin": 18.0, "clip_id": "clip-000001",
                                                    "track_id": "V1", "source_ini": 13.0, "source_fin": 28.0})

    def test_gaps_are_dropped_when_flattening_but_kept_for_the_timeline(self):
        doc = doc_with((10, 12))
        doc, second = montaje.add_clip(doc, 20, 23, at=5.0)              # hueco 2–5
        self.assertEqual([(p["seq_ini"], p["seq_fin"]) for p in montaje.flatten(doc)], [(0.0, 2.0), (2.0, 5.0)])
        with_gaps = montaje.flatten(doc, gaps=True)
        self.assertEqual([(p.get("gap", False), p["seq_ini"], p["seq_fin"]) for p in with_gaps],
                         [(False, 0.0, 2.0), (True, 2.0, 5.0), (False, 5.0, 8.0)])
        self.assertEqual(montaje.total_seconds(doc), 5.0)
        self.assertEqual(montaje.extent(doc), 8.0)
        closed = montaje.ripple_close_gaps(doc, "V1")
        self.assertEqual(spans(closed), [(0.0, 2.0), (2.0, 5.0)])

    def test_split_trim_move_remove_with_ripple(self):
        doc = doc_with((10, 20), (30, 35), (40, 50))                      # 0–10, 10–15, 15–25
        doc, (left, right) = montaje.split(doc, "clip-000001", 4.0)
        self.assertEqual((left["source_fin"], right["source_ini"], right["seq_ini"]), (14.0, 14.0, 4.0))
        self.assertTrue(left["edited"] and right["edited"])
        self.assertEqual(spans(doc), [(0.0, 4.0), (4.0, 10.0), (10.0, 15.0), (15.0, 25.0)])
        with self.assertRaises(ValueError):
            montaje.split(doc, "clip-000002", 15.0)                       # en el borde
        # recortar el inicio: seq_ini y source_ini se mueven juntos; el fin queda quieto
        doc, clip = montaje.trim_edge(doc, right["clip_id"], "start", 1.5)
        self.assertEqual((clip["source_ini"], clip["seq_ini"], montaje.seq_fin(clip)), (15.5, 5.5, 10.0))
        # recortar el fin no puede pisar al vecino: se acota a los límites
        low, high = montaje.edge_limits(doc, right["clip_id"], "end")
        self.assertEqual(high, 0.0)
        doc, clip = montaje.trim_edge(doc, right["clip_id"], "end", 3.0)
        self.assertEqual(montaje.seq_fin(clip), 10.0)
        doc, clip = montaje.trim_edge(doc, right["clip_id"], "end", -2.0)
        self.assertEqual(clip["source_fin"], 18.0)
        self.assertEqual(montaje.edge_limits(doc, "clip-000003", "end")[1], 50.0)   # hasta el fin del medio
        # mover con insert: el tercero pasa al principio; los demás se corren; su hueco se cierra
        doc, moved = montaje.move(doc, "clip-000003", 0.0)
        self.assertEqual([c["clip_id"] for c in doc["clips"]],
                         ["clip-000003", "clip-000001", right["clip_id"], "clip-000002"])
        self.assertEqual(spans(doc), [(0.0, 10.0), (10.0, 14.0), (15.5, 18.0), (20.0, 25.0)])
        # insert dentro de un clip cae al borde más cercano
        doc, moved = montaje.move(doc, "clip-000002", 11.0)
        self.assertEqual(moved["seq_ini"], 10.0)
        self.assertEqual(doc["clips"][1]["clip_id"], "clip-000002")
        # remove con ripple cierra el hueco; sin ripple lo deja
        before = montaje.extent(doc)
        doc, gone = montaje.remove(doc, "clip-000002")
        self.assertEqual(montaje.extent(doc), before - 5.0)
        doc2, _ = montaje.remove(doc, "clip-000003", ripple=False)
        self.assertEqual(montaje.extent(doc2), montaje.extent(doc))
        self.assertTrue(any(p.get("gap") for p in montaje.flatten(doc2, gaps=True)))

    def test_move_to_upper_track_and_overwrite(self):
        doc = doc_with((10, 20), (30, 40))                                # 0–10, 10–20
        doc, up = montaje.move(doc, "clip-000002", 5.0, "V2")             # arriba, sobre el primero
        self.assertEqual((up["track_id"], up["seq_ini"]), ("V2", 5.0))
        self.assertEqual(montaje.track_ids(doc), ["V1", "V2", "V3"])
        pieces = montaje.flatten(doc)
        self.assertEqual([(p["clip_id"], p["seq_ini"], p["seq_fin"]) for p in pieces],
                         [("clip-000001", 0.0, 5.0), ("clip-000002", 5.0, 15.0)])
        self.assertEqual(pieces[0]["source_fin"], 15.0)
        # overwrite dentro de la misma pista parte al que tapa
        doc = doc_with((10, 30))
        doc, over = montaje.add_clip(doc, 50, 52, at=8.0, track="V2")
        doc, over = montaje.move(doc, over["clip_id"], 8.0, "V1", mode="overwrite")
        self.assertEqual(spans(doc), [(0.0, 8.0), (8.0, 10.0), (10.0, 20.0)])
        self.assertEqual(spans(doc, "source"), [(10.0, 18.0), (50.0, 52.0), (20.0, 30.0)])
        self.assertEqual(montaje.track_ids(doc), ["V1", "V2"])          # la pista vacía sobrante se va
        with self.assertRaises(ValueError):
            montaje.move(doc, over["clip_id"], 0.0, mode="raro")

    def test_seq_source_mapping_with_repeated_source(self):
        doc = doc_with((10, 14), (10, 14), (30, 32))                      # el mismo tramo dos veces
        self.assertEqual(montaje.source_to_seq(doc, 11.0), [1.0, 5.0])
        self.assertEqual(montaje.source_to_seq(doc, 31.0), [9.0])
        self.assertEqual(montaje.source_to_seq(doc, 50.0), [])
        self.assertEqual(montaje.seq_to_source(doc, 5.5)["source_t"], 11.5)
        self.assertIsNone(montaje.seq_to_source(doc, 30.0))
        sm = montaje.SequenceMap(doc)
        piece = sm.to_source(6.0)
        self.assertEqual((piece["clip_id"], piece["source_t"]), ("clip-000002", 12.0))
        following = sm.siguiente(piece)
        self.assertEqual((following["clip_id"], following["source_t"], following["seq_ini"]), ("clip-000003", 30.0, 8.0))
        self.assertIsNone(sm.siguiente(following))
        self.assertEqual(sm.from_source(31.5, following), 9.5)
        self.assertEqual((sm.total, sm.extent), (10.0, 10.0))
        self.assertIsNone(sm.to_source(10.5))

    def test_validation_rejects_overlap_in_the_same_track_and_bad_ids(self):
        doc = doc_with((10, 20))
        bad = {**doc, "clips": doc["clips"] + [{**doc["clips"][0], "clip_id": "clip-000009", "seq_ini": 5.0}]}
        with self.assertRaisesRegex(ValueError, "se solapa"):
            montaje.validate_document(bad)
        for clip in ({"clip_id": "c1"}, {**doc["clips"][0], "track_id": "A1"},
                     {**doc["clips"][0], "state": "raro"}, {**doc["clips"][0], "topic_ids": "t1"}):
            with self.subTest(clip=clip), self.assertRaises(ValueError):
                montaje.validate_document({**doc, "clips": [clip]})
        with self.assertRaisesRegex(ValueError, "otro video"):
            montaje.validate_document(doc, fingerprint={"size": 1, "hash_muestreado": "x", "inventario_sha256": "y"})
        with self.assertRaisesRegex(ValueError, "otra duración"):
            montaje.validate_document(doc, duration=50.0)

    def test_old_document_without_tracks_loads_and_roundtrips(self):
        legacy = {"schema": montaje.SCHEMA, "media": dict(FP), "duration_source": 100.0,
                  "clips": [{"clip_id": "clip-000004", "source_ini": 1.0, "source_fin": 3.0, "seq_ini": 0.0,
                             "track_id": "V2"}]}
        loaded = montaje.validate_document(legacy, fingerprint=FP, duration=100.0)
        self.assertEqual(montaje.track_ids(loaded), ["V1", "V2"])
        self.assertEqual(loaded["next_id"], 5)
        self.assertEqual(loaded["target_seconds"], 900.0)
        self.assertEqual(loaded["analysis"]["pass"], 0)
        self.assertEqual((loaded["clips"][0]["origin"], loaded["clips"][0]["state"]), ("user", "proposed"))
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "montaje.json"
            live = loaded["clips"][0]
            montaje.save_document(path, loaded)
            self.assertIs(loaded["clips"][0], live)                        # identidad conservada
            self.assertEqual(loaded["revision"], 1)
            raw = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(raw["revision"], 1)
            self.assertEqual(montaje.load_document(path, fingerprint=FP, duration=100.0)["clips"][0]["clip_id"],
                             "clip-000004")
            self.assertIsNone(montaje.load_document(Path(tmp) / "no.json"))
        self.assertNotEqual(montaje.content_digest(loaded), montaje.content_digest(doc_with((1, 2))))
        self.assertEqual(montaje.content_digest({**loaded, "revision": 99}), montaje.content_digest(loaded))

    def test_ensure_track_states_edges_and_order(self):
        doc = doc_with((10, 12))
        montaje.ensure_track(doc, 4)
        self.assertEqual(montaje.track_ids(doc), ["V1", "V2", "V3", "V4"])
        montaje.ensure_spare_track(doc)
        self.assertEqual(montaje.track_ids(doc), ["V1", "V2"])
        doc, touched = montaje.set_state(doc, ["clip-000001"], "accepted")
        self.assertEqual((touched[0]["state"], touched[0]["edited"]), ("accepted", True))
        doc, same = montaje.set_state(doc, ["clip-000001"], "accepted")
        self.assertEqual(same, [])
        doc, clip = montaje.update_clip(doc, "clip-000001", label="Bit", topic_ids=["t1"])
        self.assertEqual((clip["label"], clip["topic_ids"]), ("Bit", ["t1"]))
        doc, other = montaje.add_clip(doc, 20, 21, at=0.5, track="V2")
        self.assertEqual(montaje.clip_edges(doc), [0.0, 0.5, 1.5, 2.0])
        self.assertEqual([c["clip_id"] for c in montaje.ordered_clips(doc)], ["clip-000001", other["clip_id"]])
        self.assertEqual(montaje.stats(doc)["topics"], {"t1": 1})
        with self.assertRaises(ValueError):
            montaje.set_state(doc, ["clip-000001"], "raro")


if __name__ == "__main__":
    unittest.main()


class HistoryTests(unittest.TestCase):
    """Deshacer/rehacer de move y split del montaje con la pila de historial (puro)."""

    def test_undo_redo_of_move_and_split(self):
        import editorial_history as history
        stack = history.HistoryStack()
        doc = doc_with((10, 20), (30, 35))
        before = doc
        doc, _ = montaje.split(doc, "clip-000001", 4.0)
        stack.record("montaje: dividir", {"montaje": before}, {"montaje": doc},
                     {"montaje": montaje.content_digest(doc)})
        before = doc
        doc, _ = montaje.move(doc, "clip-000002", 0.0)
        stack.record("montaje: mover", {"montaje": before}, {"montaje": doc},
                     {"montaje": montaje.content_digest(doc)})
        self.assertEqual(doc["clips"][0]["clip_id"], "clip-000002")
        entry = stack.undo({"montaje": montaje.content_digest(doc)})
        doc = entry.before["montaje"]
        stack.settle(entry, {"montaje": montaje.content_digest(doc)})
        self.assertEqual([c["clip_id"] for c in doc["clips"]], ["clip-000001", "clip-000003", "clip-000002"])
        entry = stack.undo({"montaje": montaje.content_digest(doc)})
        doc = entry.before["montaje"]
        stack.settle(entry, {"montaje": montaje.content_digest(doc)})
        self.assertEqual(len(doc["clips"]), 2)
        entry = stack.redo({"montaje": montaje.content_digest(doc)})
        doc = entry.after["montaje"]
        self.assertEqual(len(doc["clips"]), 3)
        # un cambio por fuera (la AI importó) descarta la entrada en vez de pisar
        with self.assertRaises(history.Stale):
            stack.redo({"montaje": "otro"})


@unittest.skipUnless(__import__("shutil").which("ffmpeg"), "FFmpeg no instalado")
class MontageExportTests(unittest.TestCase):
    def test_non_chronological_montage_renders_in_sequence_order_with_child(self):
        import subprocess
        import editorial_io
        import editorial_master
        import medios
        import podcast_export
        from test_projects import fixture
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "fuente.mkv"
            subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i",
                            "testsrc2=size=96x64:rate=25:duration=12", "-f", "lavfi", "-i",
                            "sine=frequency=440:duration=12", "-map", "0:v", "-map", "1:a", "-map", "1:a",
                            "-c:v", "libx264", "-c:a", "pcm_s16le", str(source)], check=True,
                           **medios.flags_subprocess())
            master = fixture()
            master["media"].update(path=str(source), fingerprint=medios.fingerprint(source))
            master_path = editorial_master.write_package(root / "editorial", master)["master"]
            doc = montaje.new_document(master["media"]["fingerprint"], 12)
            for a, b in ((8, 10), (1, 3), (4, 5)):
                doc, _ = montaje.add_clip(doc, a, b, origin="ai", label=f"{a}-{b}")
            with self.assertRaisesRegex(ValueError, "recodifique"):
                podcast_export.export_montage(master_path, doc, source, root / "out", fmt="copy")
            dest = podcast_export.export_montage(master_path, doc, source, root / "out")
            exports = editorial_io.read_json(dest / "exports.json")
            self.assertEqual(exports["schema"], "editorial-montage-export/1")
            self.assertEqual(exports["total_seconds"], 5.0)
            entry = exports["files"][0]
            self.assertEqual([p[2:4] for p in entry["pieces"]], [[8.0, 10.0], [1.0, 3.0], [4.0, 5.0]])
            rendered = medios.inspeccionar(dest / entry["file"])
            self.assertAlmostEqual(rendered["duracion"], 5.0, delta=0.15)
            self.assertEqual(len(rendered["pistas"]), 2)
            child = editorial_io.read_json(dest / entry["project_master"])
            self.assertEqual([(s["source_ini"], s["source_fin"], s["child_ini"]) for s in child["derivation"]["segments"]],
                             [(8.0, 10.0, 0.0), (1.0, 3.0, 2.0), (4.0, 5.0, 4.0)])
            # la palabra «retorno» (8–9 en la fuente) suena al principio del montaje;
            # «inicio» (1–2) a los 2 s; «eliminado» (4–5) al final
            words = [(w["text"], w["t_ini"]) for w in child["tracks"]["A"]["words"]]
            self.assertEqual(words, [("retorno", 0.0), ("inicio", 2.0), ("eliminado", 4.0)])
            frozen = editorial_io.read_json(dest / "accepted-montage.json")
            self.assertEqual(len(frozen["clips"]), 3)
            self.assertIn("TITLE: MONTAJE", (dest / "montaje.edl").read_text(encoding="utf-8"))
            self.assertIn("<fcpxml", (dest / "montaje.fcpxml").read_text(encoding="utf-8"))
            with self.assertRaises(FileExistsError):
                podcast_export.export_montage(master_path, doc, source, root / "out")
            empty = montaje.new_document(master["media"]["fingerprint"], 12)
            with self.assertRaisesRegex(ValueError, "clips activos"):
                podcast_export.export_montage(master_path, empty, source, root / "out2")


class TaskFiveTests(unittest.TestCase):
    """Tarea 5 (plan §8): pedido, transcript con temas, validación, protección y bucle."""

    def setUp(self):
        import editorial_chunks
        import editorial_layers
        from test_projects import fixture
        self.master = fixture()
        self.master["media"]["fingerprint"] = dict(FP)
        self.master["media"]["path"] = "hijo.mp4"
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "views").mkdir()
        self.topics = editorial_layers.new_layer(self.master, "Temas y subtemas", kind="topics", layer_id="topics-x")
        self.topics["items"] = [
            {"item_id": "t1", "label": "Inicio", "comment": "arranque", "state": "proposed", "edited": False,
             "parent_id": None, "ranges": [{"t_ini": 0.5, "t_fin": 3.0}]},
            {"item_id": "t1a", "label": "Sub inicio", "comment": "", "state": "proposed", "edited": False,
             "parent_id": "t1", "ranges": [{"t_ini": 1.0, "t_fin": 2.0}]},
            {"item_id": "t2", "label": "Retorno", "comment": "", "state": "proposed", "edited": False,
             "parent_id": None, "ranges": [{"t_ini": 7.5, "t_fin": 10.0}]}]
        self.snapshot = editorial_layers.write_snapshot(self.root, self.master, [self.topics])
        self.digest = editorial_chunks.source_master_digest(self.master)

    def tearDown(self):
        self.tmp.cleanup()

    def proposal(self, request, clips, **extra):
        return {"schema": montaje.PROPOSAL_SCHEMA, "planner": "test-ai", "request_id": request["request_id"],
                "source_master_digest": request["source_master_digest"],
                "source_layers_digest": request["source_layers_digest"],
                "montage_digest": request["montage_digest"], "pass": request["pass_required"],
                "target_seconds": request["target_seconds"], "title": "Prueba", "sections": [],
                "clips": clips, "notes": "nada", **extra}

    def test_prepare_writes_request_transcript_with_topics_and_signals(self):
        request = montaje.prepare(self.root, self.master, self.snapshot, None, target_seconds=6,
                                  topics_layer=self.topics)
        views = self.root / "views"
        self.assertEqual((request["pass_required"], request["montage_digest"], request["target_seconds"]), (1, None, 6.0))
        transcript = (views / "montaje-transcript.md").read_text(encoding="utf-8")
        self.assertIn("## ▶ Tema: Inicio (t1)", transcript)
        self.assertIn("## ▶ Subtema: Sub inicio (t1a)", transcript)
        self.assertIn("## ◀ fin: Retorno (t2)", transcript)
        self.assertIn("`A-u-1`", transcript)                                   # los IDs siguen ahí
        self.assertLess(transcript.index("▶ Tema: Inicio"), transcript.index("`A-u-1`"))
        signals = (views / "montaje-signals.md").read_text(encoding="utf-8")
        self.assertIn("## Picos", signals)
        self.assertIn("risa de 6.0 s en A", signals)
        self.assertIn("`A-u-1`", signals)
        text = (views / "montaje-agent-request.md").read_text(encoding="utf-8")
        self.assertIn("Tarea 5", text)
        self.assertIn(request["request_id"], text)
        self.assertIn("primera pasada", text)
        self.assertFalse((views / "montaje-current.md").exists())
        with self.assertRaises(ValueError):
            montaje.prepare(self.root, self.master, self.snapshot, None, target_seconds=0)

    def test_validation_tolerance_repeats_protection_and_order(self):
        request = montaje.prepare(self.root, self.master, self.snapshot, None, target_seconds=6,
                                  tolerance=0.5, topics_layer=self.topics)
        clips = [{"clip_id": "c1", "source_ini": 7.5, "source_fin": 10.0, "label": "cierre primero",
                  "topic_ids": ["t2"], "reason": "abre con el remate", "confidence": .8},
                 {"clip_id": "c2", "source_ini": 0.5, "source_fin": 3.0, "label": "inicio",
                  "topic_ids": ["t1"], "reason": "premisa", "confidence": .7,
                  "first_utterance_id": "A-u-1"}]
        validated = montaje.validate_proposal(self.proposal(request, clips), self.master, request, self.snapshot, None)
        self.assertEqual([c["ai_clip_id"] for c in validated["clips"]], ["c1", "c2"])
        self.assertTrue(3.0 <= validated["total_seconds"] <= 5.5, validated["total_seconds"])   # bordes ajustados
        self.assertEqual(validated["warnings"], [])
        # fuera de la tolerancia: se importa igual, con aviso
        long = montaje.validate_proposal(self.proposal(request, clips[:1]), self.master, request, self.snapshot, None)
        self.assertTrue(any("fuera de la tolerancia" in w for w in long["warnings"]))
        # un tramo repetido sin repeat es error; con repeat y motivo, aviso
        twice = clips + [{"clip_id": "c3", "source_ini": 7.5, "source_fin": 9.0, "reason": "callback"}]
        with self.assertRaisesRegex(ValueError, "repite"):
            montaje.validate_proposal(self.proposal(request, twice), self.master, request, self.snapshot, None)
        twice[2]["repeat"] = True
        ok = montaje.validate_proposal(self.proposal(request, twice), self.master, request, self.snapshot, None)
        self.assertTrue(any("callback" in w for w in ok["clips"][2]["warnings"]))
        for bad, message in (({"pass": 2}, "fuera de orden"), ({"request_id": "x"}, "request_id"),
                             ({"montage_digest": "y"}, "montage_digest"), ({"clips": []}, "no vac")):
            with self.subTest(bad=bad), self.assertRaisesRegex(ValueError, message):
                montaje.validate_proposal({**self.proposal(request, clips), **bad}, self.master, request,
                                          self.snapshot, None)
        with self.assertRaisesRegex(ValueError, "desconocido"):
            montaje.validate_proposal(self.proposal(request, [{**clips[0], "last_utterance_id": "Z"}]),
                                      self.master, request, self.snapshot, None)
        with self.assertRaisesRegex(ValueError, "capas cambiaron"):
            montaje.validate_proposal(self.proposal(request, clips), self.master, request,
                                      {"source_layers_digest": "otro"}, None)

    def test_import_loop_protects_accepted_and_edited_and_keeps(self):
        import editorial_io
        request = montaje.prepare(self.root, self.master, self.snapshot, None, target_seconds=6,
                                  topics_layer=self.topics)
        clips = [{"clip_id": "c1", "source_ini": 7.5, "source_fin": 10.0, "label": "cierre", "reason": "r", "confidence": .8},
                 {"clip_id": "c2", "source_ini": 0.5, "source_fin": 3.0, "label": "inicio", "reason": "r", "confidence": .7}]
        doc, validated, message = montaje.import_proposal(self.root, self.master, self.snapshot, None,
                                                           self.proposal(request, clips), topics_layer=self.topics)
        self.assertIn("pasada 1", message)
        self.assertEqual([c["label"] for c in doc["clips"]], ["cierre", "inicio"])
        self.assertEqual(doc["clips"][1]["seq_ini"], montaje.seq_fin(doc["clips"][0]))
        self.assertEqual(doc["analysis"]["pass"], 1)
        self.assertEqual(doc["analysis"]["ai_clip_ids"]["c1"], "clip-000001")
        self.assertTrue((self.root / "views" / "montaje-pass1.json").is_file())
        request2 = editorial_io.read_json(self.root / "views" / "montaje-request.json")
        self.assertEqual(request2["pass_required"], 2)
        self.assertEqual(request2["montage_digest"], montaje.content_digest(doc))
        current = (self.root / "views" / "montaje-current.md").read_text(encoding="utf-8")
        self.assertIn("junta", current)
        self.assertIn("propuesto por la AI", current)
        # la persona acepta el primero y edita el segundo (lo mueve a V2)
        doc, _ = montaje.set_state(doc, ["clip-000001"], "accepted")
        doc, _ = montaje.move(doc, "clip-000002", 5.0, "V2")
        montaje.save_document(self.root / "views" / "montaje.json", doc)
        # una segunda pasada preparada sobre ese montaje: la propuesta vieja ya no vale
        request2 = montaje.prepare(self.root, self.master, self.snapshot, doc, target_seconds=6,
                                   topics_layer=self.topics)
        self.assertEqual(request2["pass_required"], 2)
        self.assertIn("montaje-current.md", (self.root / "views" / "montaje-agent-request.md").read_text(encoding="utf-8"))
        with self.assertRaisesRegex(ValueError, "request_id"):
            montaje.import_proposal(self.root, self.master, self.snapshot, doc, self.proposal(request, clips))
        # pasada 2: conserva un clip con keep, añade uno nuevo; lo aceptado y lo editado no se mueven
        second = [{"clip_id": "k1", "keep": "clip-000001"},
                  {"clip_id": "n1", "source_ini": 4.0, "source_fin": 6.0, "label": "medio", "reason": "puente", "confidence": .6}]
        doc2, validated2, message2 = montaje.import_proposal(self.root, self.master, self.snapshot, doc,
                                                              self.proposal(request2, second), topics_layer=self.topics)
        self.assertIn("pasada 2", message2)
        by_id = {c["clip_id"]: c for c in doc2["clips"]}
        self.assertEqual(by_id["clip-000001"]["state"], "accepted")                # sigue donde estaba
        self.assertEqual(by_id["clip-000001"]["seq_ini"], 0.0)
        self.assertEqual((by_id["clip-000002"]["track_id"], by_id["clip-000002"]["seq_ini"]), ("V2", 5.0))
        new = next(c for c in doc2["clips"] if c["label"] == "medio")
        self.assertEqual(new["track_id"], "V1")
        self.assertGreaterEqual(new["seq_ini"], montaje.seq_fin(by_id["clip-000001"]))     # no pisa lo protegido
        self.assertEqual(doc2["analysis"]["pass"], 2)
        latest = editorial_io.read_json(self.root / "views" / "montaje-request.json")
        with self.assertRaisesRegex(ValueError, "keep apunta"):
            montaje.validate_proposal(self.proposal(latest, [{"clip_id": "k", "keep": "clip-000099"}]),
                                      self.master, latest, self.snapshot, doc2)


class ResolveExportTests(unittest.TestCase):
    """Fase F: EDL CMX3600 y FCPXML del montaje (golden mínimos)."""

    def test_edl_and_fcpxml_reference_the_source_in_sequence_order(self):
        doc = doc_with((8, 10), (1, 3))
        edl = montaje.to_cmx3600(doc, 25, reel="hijo", title="PRUEBA")
        self.assertEqual(edl.splitlines()[0], "TITLE: PRUEBA")
        self.assertIn("001  hijo     V     C        00:00:08:00 00:00:10:00 00:00:00:00 00:00:02:00", edl)
        self.assertIn("002  hijo     AA    C        00:00:01:00 00:00:03:00 00:00:02:00 00:00:04:00", edl)
        self.assertIn("* FROM CLIP NAME: clip-000002", edl)
        fcp = montaje.to_fcpxml(doc, Path("C:/videos/hijo.mp4"), 25)
        self.assertIn('frameDuration="1/25s"', fcp)
        self.assertIn('name="hijo.mp4"', fcp)
        self.assertIn('<asset-clip ref="r2" name="clip-000001" offset="0/25s" start="200/25s" duration="50/25s"/>', fcp)
        self.assertIn('<asset-clip ref="r2" name="clip-000002" offset="50/25s" start="25/25s" duration="50/25s"/>', fcp)
        import xml.dom.minidom
        xml.dom.minidom.parseString(fcp.encode("utf-8"))                 # XML bien formado
        self.assertEqual(montaje._timecode(3661.5, 30), "01:01:01:15")
