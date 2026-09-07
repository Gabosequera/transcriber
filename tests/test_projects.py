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


class DerivedLayersTests(unittest.TestCase):
    """Fase C: las capas del padre viajan al hijo remapeadas a su reloj."""

    def layer(self):
        return {"schema": "editorial-layer/1", "layer_id": "topics-abc", "kind": "topics",
                "name": "Temas y subtemas", "color": "#9a70bc", "revision": 4,
                "media_fingerprint": {"size": 1, "hash_muestreado": "a", "inventario_sha256": "b"},
                "source_master_digest": "padre", "deleted_item_ids": ["t-viejo"],
                "analysis": {"request_id": "r1"},
                "items": [
                    {"item_id": "t1", "label": "Tema 1", "comment": "", "state": "accepted", "edited": True,
                     "parent_id": None, "ranges": [{"t_ini": 0, "t_fin": 2}, {"t_ini": 6, "t_fin": 10}]},
                    {"item_id": "t1a", "label": "Sub 1", "comment": "sub", "state": "proposed", "edited": False,
                     "parent_id": "t1", "ranges": [{"t_ini": 6.5, "t_fin": 9}]},
                    {"item_id": "t2", "label": "Tema 2 (cae en el recorte)", "comment": "", "state": "proposed",
                     "edited": False, "parent_id": None, "ranges": [{"t_ini": 3.5, "t_fin": 6}]},
                    {"item_id": "t2a", "label": "Sub del recortado", "comment": "", "state": "proposed",
                     "edited": False, "parent_id": "t2", "ranges": [{"t_ini": 4, "t_fin": 5}]},
                ]}

    def test_ranges_are_intersected_split_and_orphans_dropped(self):
        parent = fixture()
        info = {"path": "hijo.mp4", "duracion": 9, "t0": 0,
                "pistas": [{"idx": i, "delta": 0} for i in range(2)]}
        child = projects.derive_master(parent, info, {"size": 2, "hash_muestreado": "h", "inventario_sha256": "i"},
                                       [(0, 3), (6, 12)], name="hijo")
        derived = projects.derive_layers([self.layer(), {**self.layer(), "layer_id": "borrada", "deleted": True}],
                                         child["derivation"]["segments"],
                                         child_fingerprint=child["media"]["fingerprint"],
                                         child_digest="hijo-digest", parent_digest="padre")
        self.assertEqual(len(derived), 1)                                      # la borrada no viaja
        layer = derived[0]
        self.assertEqual(layer["layer_id"], "topics-abc")
        self.assertEqual(layer["media_fingerprint"]["hash_muestreado"], "h")
        self.assertEqual(layer["source_master_digest"], "hijo-digest")
        self.assertEqual(layer["derived_from"]["source_master_digest"], "padre")
        self.assertEqual(layer["derived_from"]["dropped_item_ids"], ["t2", "t2a"])
        self.assertEqual(layer["deleted_item_ids"], ["t-viejo"])
        by_id = {i["item_id"]: i for i in layer["items"]}
        self.assertEqual(sorted(by_id), ["t1", "t1a"])                         # t2 y su hijo desaparecen
        # el primer rango de t1 [0,2] queda igual; [6,10] se traslada a [3,7]
        self.assertEqual([(r["t_ini"], r["t_fin"]) for r in by_id["t1"]["ranges"]], [(0.0, 2.0), (3.0, 7.0)])
        self.assertEqual(by_id["t1"]["ranges"][1]["source_range"], [6.0, 10.0])
        self.assertEqual(by_id["t1"]["source_item_id"], "t1")
        self.assertEqual((by_id["t1"]["state"], by_id["t1"]["edited"]), ("accepted", True))
        self.assertEqual([(r["t_ini"], r["t_fin"]) for r in by_id["t1a"]["ranges"]], [(3.5, 6.0)])
        self.assertEqual(by_id["t1a"]["parent_id"], "t1")
        import editorial_layers
        self.assertEqual(len(editorial_layers.validate_layer(layer, child)["items"]), 2)
        # un rango que CRUZA un recorte se parte en dos rangos contiguos
        crossing = {**self.layer(), "items": [{"item_id": "x", "label": "cruza", "comment": "", "state": "proposed",
                                               "edited": False, "parent_id": None,
                                               "ranges": [{"t_ini": 1, "t_fin": 8}]}]}
        parts = projects.derive_layers([crossing], child["derivation"]["segments"],
                                       child_fingerprint=child["media"]["fingerprint"],
                                       child_digest="d")[0]["items"][0]["ranges"]
        self.assertEqual([(r["t_ini"], r["t_fin"]) for r in parts], [(1.0, 3.0), (3.0, 5.0)])
        self.assertEqual([r["source_range"] for r in parts], [[1.0, 3.0], [6.0, 8.0]])
        self.assertEqual(len(editorial_layers.validate_items(parts and [{"item_id": "x", "ranges": parts}], 9)), 1)

    @unittest.skipUnless(shutil.which("ffmpeg"), "FFmpeg no instalado")
    def test_exported_child_contains_layers_and_lane_order(self):
        import editorial_layers
        import editorial_trims
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
            trims = editorial_trims.new_document(parent["media"]["fingerprint"], 12)
            editorial_trims.add_cut(trims, 3, 7, origin="user", reason="prueba")
            layer = editorial_layers.new_layer(parent, "Pedidos", layer_id="pedidos-1")
            layer["items"] = [editorial_layers.new_item(1, 2, "antes"), editorial_layers.new_item(4, 5, "dentro"),
                              editorial_layers.new_item(8, 10, "después")]
            dest = podcast_export.export_plan(path, None, source, root / "salida", trims=trims,
                                              layers=[layer], lane_order=["autor", "pedidos-1", "trims:main"])
            entry = editorial_io.read_json(dest / "exports.json")["files"][0]
            child_root = (dest / entry["project_master"]).parent
            child = editorial_io.read_json(dest / entry["project_master"])
            store = editorial_layers.LayerStore(child_root, child)
            self.assertEqual(list(store.layers), ["pedidos-1"])
            labels = [(i["label"], i["ranges"][0]["t_ini"], i["ranges"][0]["t_fin"]) for i in store.layers["pedidos-1"]["items"]]
            self.assertEqual(labels, [("antes", 1.0, 2.0), ("después", 4.0, 6.0)])
            self.assertEqual(editorial_layers.load_lane_order(child_root), ["autor", "pedidos-1", "trims:main"])
            self.assertFalse((child_root / "views" / "trims.json").exists())


if __name__ == "__main__":
    unittest.main()
