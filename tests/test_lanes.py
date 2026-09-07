"""Carriles de la AI y lanes de trims.json (diseño §9/§10, Fase 7): puro, sin Tk."""
import copy
import json
import tempfile
import unittest
from pathlib import Path

import editorial_layers as layers
import editorial_trims
from editorial_io import read_json
from test_projects import fixture
from test_trims import FINGERPRINT, master_fixture


class TrimLanesTests(unittest.TestCase):
    def test_three_origins_split_in_two_lanes_and_export_union_is_unchanged(self):
        document = editorial_trims.new_document(FINGERPRINT, 60.0)
        editorial_trims.add_cut(document, 1, 2, origin="silence")
        editorial_trims.add_cut(document, 1.5, 3, origin="ai")
        editorial_trims.add_cut(document, 10, 11, origin="user")
        self.assertEqual([c["lane"] for c in document["cuts"]], ["main", "ai", "main"])
        view = layers.adapters(master_fixture(), trims=document)
        self.assertEqual([l["layer_id"] for l in view], ["trims:main", "trims:ai"])
        self.assertEqual([len(l["items"]) for l in view], [2, 1])
        self.assertEqual(view[1]["items"][0]["origin"], "ai")
        self.assertEqual(editorial_trims.enabled_intervals(document), [(1.0, 3.0), (10.0, 11.0)])
        self.assertEqual(layers.lane_of("trims:ai"), "ai")
        self.assertIsNone(layers.lane_of("autor"))

    def test_old_files_without_lane_load_and_export_the_same(self):
        legacy = {"schema": editorial_trims.SCHEMA_TRIMS, "media": dict(FINGERPRINT), "duration": 60.0,
                  "revision": 3, "next_id": 3, "silence": None, "ai": None,
                  "cuts": [{"cut_id": "cut-000001", "t_ini": 1.0, "t_fin": 2.0, "origin": "silence"},
                           {"cut_id": "cut-000002", "t_ini": 5.0, "t_fin": 6.0, "origin": "ai", "enabled": False}]}
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "trims.json"
            path.write_text(json.dumps(legacy), encoding="utf-8")
            loaded = editorial_trims.load_document(path, fingerprint=FINGERPRINT, duration=60.0)
        self.assertEqual([c["lane"] for c in loaded["cuts"]], ["main", "ai"])
        self.assertEqual([l["lane_id"] for l in loaded["lanes"]], ["main", "ai"])
        self.assertEqual(editorial_trims.enabled_intervals(loaded), [(1.0, 2.0)])
        self.assertEqual(editorial_trims.enabled_intervals(legacy | {"cuts": [
            dict(c, enabled=c.get("enabled", True)) for c in legacy["cuts"]]}), [(1.0, 2.0)])
        with self.assertRaisesRegex(ValueError, "carril inválido"):
            editorial_trims.validate_document({**legacy, "cuts": [{**legacy["cuts"][0], "lane": "Mal Id"}]})

    def test_user_lane_receives_cuts_and_export_unions_all_lanes(self):
        document = editorial_trims.new_document(FINGERPRINT, 60.0)
        lane = editorial_trims.add_lane(document, "Chistes", color="#aa5533")
        editorial_trims.add_cut(document, 1, 2, origin="silence")
        editorial_trims.add_cut(document, 1.5, 4, origin="user", lane=lane["lane_id"])
        disabled = editorial_trims.add_cut(document, 20, 21, origin="user", lane=lane["lane_id"])
        disabled["enabled"] = False
        validated = editorial_trims.validate_document(document)
        self.assertEqual([l["lane_id"] for l in validated["lanes"]], ["main", "ai", lane["lane_id"]])
        self.assertEqual(editorial_trims.enabled_intervals(validated), [(1.0, 4.0)])
        view = {l["layer_id"]: l for l in layers.adapters(master_fixture(), trims=validated)}
        self.assertEqual(len(view[f"trims:{lane['lane_id']}"]["items"]), 2)
        self.assertEqual(view[f"trims:{lane['lane_id']}"]["color"], "#aa5533")
        # solapes: nunca cruzan carriles
        editorial_trims.coalesce(validated)
        self.assertEqual(len(validated["cuts"]), 3)
        # borrar el carril moviendo sus cortes a main / borrándolos
        moved = copy.deepcopy(validated)
        self.assertEqual(editorial_trims.remove_lane(moved, lane["lane_id"]), 2)
        self.assertEqual([c["lane"] for c in moved["cuts"]], ["main"] * 3)
        self.assertEqual([l["lane_id"] for l in moved["lanes"]], ["main", "ai"])
        dropped = copy.deepcopy(validated)
        editorial_trims.remove_lane(dropped, lane["lane_id"], move_to=None)
        self.assertEqual(len(dropped["cuts"]), 1)
        with self.assertRaisesRegex(ValueError, "fábrica"):
            editorial_trims.remove_lane(validated, "main")
        with self.assertRaises(ValueError):
            editorial_trims.add_lane(document, "otra", lane_id=lane["lane_id"])
        with self.assertRaises(ValueError):
            editorial_trims.add_lane(document, "otra", color="rojo")
        # una lane usada por un corte pero no declarada se añade sola
        self.assertEqual([l["lane_id"] for l in editorial_trims.lanes(
            {**document, "lanes": None, "cuts": [dict(c, lane="extra") for c in document["cuts"][:1]]})],
            ["main", "ai", "extra"])


class LaneOrderTests(unittest.TestCase):
    def ui(self):
        return [dict(layer_id="trims:main", kind="recortes"), dict(layer_id="trims:ai", kind="recortes"),
                dict(layer_id="autor"), dict(layer_id="bloques"),
                dict(layer_id="topics:t1:1", depth=1, source_layer_id="t1"),
                dict(layer_id="topics:t1:0", depth=0, source_layer_id="t1"),
                dict(layer_id="layer-b"), dict(layer_id="layer-a")]

    def test_default_order_and_saved_order_with_unknown_ids(self):
        ordered = [l["layer_id"] for l in layers.order_layers(self.ui(), None)]
        self.assertEqual(ordered, ["autor", "bloques", "topics:t1:0", "topics:t1:1", "trims:ai", "trims:main",
                                   "layer-a", "layer-b"])
        saved = ["layer-b", "no-existe", "trims:main", "autor"]
        ordered = [l["layer_id"] for l in layers.order_layers(self.ui(), saved)]
        self.assertEqual(ordered[:4], ["layer-b", "trims:main", "autor", "bloques"])
        self.assertNotIn("no-existe", ordered)
        self.assertEqual(len(ordered), 8)

    def test_insert_above_selected_and_move(self):
        order = ["autor", "bloques", "trims:ai", "trims:main", "layer-a"]
        self.assertEqual(layers.insert_above(order, "nuevo", "trims:main"),
                         ["autor", "bloques", "trims:ai", "nuevo", "trims:main", "layer-a"])
        self.assertEqual(layers.insert_above(order, "nuevo", None),
                         ["autor", "bloques", "nuevo", "trims:ai", "trims:main", "layer-a"])
        self.assertEqual(layers.insert_above(["autor"], "nuevo", "x"), ["autor", "nuevo"])
        self.assertEqual(layers.move_in_order(order, "layer-a", -2), ["autor", "bloques", "layer-a", "trims:ai", "trims:main"])
        self.assertEqual(layers.move_in_order(order, "autor", -1), order)
        self.assertEqual(layers.move_in_order(order, "nope", 1), order)
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(layers.load_lane_order(tmp), [])
            layers.save_lane_order(tmp, order)
            self.assertEqual(layers.load_lane_order(tmp), order)
            self.assertEqual(read_json(Path(tmp) / "views" / "lanes.json")["schema"], layers.LANES_VIEW)
            (Path(tmp) / "views" / "lanes.json").write_text("{", encoding="utf-8")
            self.assertEqual(layers.load_lane_order(tmp), [])


class TopicsDepthTests(unittest.TestCase):
    def test_two_levels_become_two_lanes_pointing_to_the_same_layer(self):
        master = fixture()
        layer = layers.new_layer(master, "Temas y subtemas", kind="topics", layer_id="topics-abc")
        parent = layers.new_item(0, 6, "Tema")
        child = layers.new_item(1, 2, "Sub")
        child["parent_id"] = parent["item_id"]
        grandchild = layers.new_item(1, 1.5, "Sub-sub")
        grandchild["parent_id"] = child["item_id"]
        layer["items"] = [parent, child, grandchild]
        lanes = layers.split_by_depth(layer)
        self.assertEqual([l["layer_id"] for l in lanes], ["topics:topics-abc:0", "topics:topics-abc:1", "topics:topics-abc:2"])
        self.assertEqual([l["name"] for l in lanes], ["Temas", "Subtemas", "Subtemas 2"])
        self.assertEqual([[i["label"] for i in l["items"]] for l in lanes], [["Tema"], ["Sub"], ["Sub-sub"]])
        self.assertTrue(all(l["source_layer_id"] == "topics-abc" for l in lanes))
        self.assertEqual(layers.source_layer_id("topics:topics-abc:1"), "topics-abc")
        self.assertEqual(layers.source_layer_id("layer-x"), "layer-x")
        self.assertEqual(layers.split_by_depth(layers.new_layer(master, "Pedidos"))[0]["layer_id"][:6], "layer-")
        self.assertEqual(layers.split_by_depth({**layer, "items": []})[0]["name"], "Temas")


class MultiLayerResponseTests(unittest.TestCase):
    def test_two_layers_merge_respecting_edits_and_deletions(self):
        master = fixture()
        with tempfile.TemporaryDirectory() as tmp:
            store = layers.LayerStore(tmp, master)
            existing = layers.new_layer(master, "Notas")
            human = layers.new_item(1, 2, "Humano")
            gone = layers.new_item(3, 4, "Borrado")
            existing["items"] = [human]
            existing["deleted_item_ids"] = [gone["item_id"]]
            saved = store.save(existing)
            snapshot = layers.write_snapshot(tmp, master, store.visible())
            momentos = layers.new_layer(master, "Momentos", kind="ai", layer_id="ai-momentos")
            momentos["items"] = [layers.new_item(5, 6, "Pico")]
            response = dict(schema=layers.PROPOSAL, source_master_digest=snapshot["source_master_digest"],
                            source_layers_digest=snapshot["source_layers_digest"],
                            layers=[{**copy.deepcopy(saved), "items": [dict(human, label="AI"), gone,
                                                                        layers.new_item(8, 9, "Nueva")]},
                                    momentos])
            merged = layers.merge_responses(store, response, snapshot)
            self.assertEqual({l["layer_id"] for l in merged}, {saved["layer_id"], "ai-momentos"})
            notas = store.layers[saved["layer_id"]]
            self.assertEqual({i["label"] for i in notas["items"]}, {"Humano", "Nueva"})
            self.assertEqual(store.layers["ai-momentos"]["kind"], "ai")
            self.assertFalse(store.layers["ai-momentos"]["items"][0]["edited"])
            # `layer` sigue aceptado; `layer` + `layers` repetidos se rechazan
            single = dict(response, layer=momentos, layers=None)
            self.assertEqual(layers.merge_response(store, single, snapshot)["layer_id"], "ai-momentos")
            with self.assertRaisesRegex(ValueError, "repetido"):
                layers.merge_responses(store, dict(response, layer=momentos), snapshot)
            with self.assertRaisesRegex(ValueError, "ninguna capa"):
                layers.merge_responses(store, dict(response, layers=[]), snapshot)
            # una capa ai borrada por el usuario no resucita
            store.delete("ai-momentos")
            self.assertEqual([l["layer_id"] for l in store.visible()], [saved["layer_id"]])
            with self.assertRaisesRegex(ValueError, "borrada"):
                layers.merge_responses(store, dict(response, layer=None, layers=[momentos]), snapshot)
            self.assertTrue(layers.LayerStore(tmp, master).layers["ai-momentos"]["deleted"])
            with self.assertRaises(ValueError):
                layers.validate_layer({**momentos, "kind": "robot"}, master)


class EditorialRequestTests(unittest.TestCase):
    def test_request_orders_tasks_and_exposes_accepted_cuts(self):
        master = master_fixture()
        document = editorial_trims.new_document(FINGERPRINT, 60.0)
        cut = editorial_trims.add_cut(document, 5, 8, origin="silence", reason="hueco")
        cut["accepted"] = True
        editorial_trims.add_cut(document, 30, 31, origin="ai", reason="tangente")
        with tempfile.TemporaryDirectory() as tmp:
            request = {"request_id": "req-1"}
            path = editorial_trims.write_editorial_request(tmp, master, None, document, request)
            text = path.read_text(encoding="utf-8")
            self.assertTrue(str(path).endswith("editorial-agent-request.md"))
            self.assertLess(text.index("Tarea 3"), text.index("Tarea 2"))
            self.assertIn("req-1", text)
            self.assertIn("1 aceptados", text)
            self.assertIn("«Cortes sugeridos (AI)» (1)", text)
            self.assertIn('layers: [...]', text)
            review = editorial_trims.write_review_package(tmp, master, None, document)["completo"].read_text(encoding="utf-8")
            self.assertIn("aceptado por el editor", review)
            self.assertIn("· AI ·", review)


if __name__ == "__main__":
    unittest.main()
