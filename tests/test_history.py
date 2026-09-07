"""Deshacer/rehacer (editorial_history.py), aritmética de edición (editorial_edits.py),
`accepted` en los recortes y `Registro.reemplazar` — todo sin Tk."""
import copy
import json
import tempfile
import unittest
from pathlib import Path

import editorial_edits as edits
import editorial_history as history
import editorial_layers as layers
import editorial_trims
import marcas
from test_trims import FINGERPRINT, master_fixture, plan_fixture


class HistoryStackTests(unittest.TestCase):
    def test_undo_redo_restore_snapshots_and_new_action_discards_redo(self):
        stack = history.HistoryStack(depth=3)
        self.assertFalse(stack.can_undo())
        self.assertIsNone(stack.record("nada", {"trims": {"revision": 1}}, {"trims": {"revision": 1}},
                                       {"trims": 1}))
        one = stack.record("mover", {"trims": {"cuts": [1]}}, {"trims": {"cuts": [2]}}, {"trims": 2})
        self.assertEqual(one.expect, {"trims": 2})
        stack.record("borrar", {"trims": {"cuts": [2]}}, {"trims": {"cuts": []}}, {"trims": 3})
        self.assertEqual(len(stack), 2)
        entry = stack.undo({"trims": 3})
        self.assertEqual((entry.label, entry.before), ("borrar", {"trims": {"cuts": [2]}}))
        stack.settle(entry, {"trims": 4})
        self.assertTrue(stack.can_redo())
        again = stack.redo({"trims": 4})
        self.assertIs(again, entry)
        self.assertEqual(again.after, {"trims": {"cuts": []}})
        stack.settle(again, {"trims": 5})
        stack.undo({"trims": 5}); stack.settle(entry, {"trims": 6})
        stack.record("crear", {"trims": {"cuts": [2]}}, {"trims": {"cuts": [2, 3]}}, {"trims": 7})
        self.assertFalse(stack.can_redo())                    # la rama de redo se descarta
        with self.assertRaises(LookupError):
            stack.redo({})
        # snapshots profundos: mutar lo que se pasó no toca la entrada
        before = {"layer:x": {"items": [{"a": 1}]}}
        entry = stack.record("editar", before, {"layer:x": {"items": []}}, {"layer:x": 1})
        before["layer:x"]["items"][0]["a"] = 99
        self.assertEqual(entry.before["layer:x"]["items"][0]["a"], 1)
        # profundidad: quedan las 3 últimas
        for i in range(5):
            stack.record(f"op{i}", {"trims": {"n": i}}, {"trims": {"n": i + 1}}, {"trims": 10 + i})
        self.assertEqual(len(stack), 3)
        self.assertEqual(stack.peek_undo().label, "op4")

    def test_entry_is_discarded_when_a_document_changed_outside(self):
        stack = history.HistoryStack()
        stack.record("mover", {"trims": {"n": 1}, "lanes": ["a"]}, {"trims": {"n": 2}, "lanes": ["b"]},
                     {"trims": 2, "lanes": "abc"})
        with self.assertRaisesRegex(history.Stale, "cambió por fuera"):
            stack.undo({"trims": 9, "lanes": "abc"})            # la AI escribió trims.json
        self.assertFalse(stack.can_undo())
        self.assertFalse(stack.can_redo())
        with self.assertRaises(ValueError):
            stack.record("mal", {"a": 1}, {"b": 2}, {})
        stack.record("multi", {"trims": {"n": 1}, "lanes": ["a"]}, {"trims": {"n": 2}, "lanes": ["b"]},
                     {"trims": 2, "lanes": "abc"})
        self.assertEqual(stack.peek_undo().docs, ("trims", "lanes"))   # una sola entrada
        stack.discard(stack.peek_undo())
        self.assertEqual(len(stack), 0)


class EditArithmeticTests(unittest.TestCase):
    def test_split_trim_and_shift_ranges(self):
        left, right = edits.split_range(dict(t_ini=10, t_fin=20), 14.5)
        self.assertEqual((left["t_ini"], left["t_fin"], right["t_ini"], right["t_fin"]), (10, 14.5, 14.5, 20))
        for bad in (10, 20, 10.01, 25):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                edits.split_range(dict(t_ini=10, t_fin=20), bad)
        self.assertEqual(edits.trim_range(dict(t_ini=10, t_fin=20), "start", 12)["t_ini"], 12)
        self.assertEqual(edits.trim_range(dict(t_ini=10, t_fin=20), "end", 15)["t_fin"], 15)
        with self.assertRaises(ValueError):
            edits.trim_range(dict(t_ini=10, t_fin=20), "start", 19.99)
        with self.assertRaises(ValueError):
            edits.trim_range(dict(t_ini=10, t_fin=20), "end", 10)
        moved = edits.shift_ranges([dict(t_ini=1, t_fin=2), dict(t_ini=5, t_fin=8)], 3, 10)
        self.assertEqual([(r["t_ini"], r["t_fin"]) for r in moved], [(3, 4), (7, 10)])   # acotado
        back = edits.shift_ranges(moved, -100, 10)
        self.assertEqual([(r["t_ini"], r["t_fin"]) for r in back], [(0, 1), (4, 7)])
        self.assertEqual(edits.range_at([dict(t_ini=1, t_fin=2), dict(t_ini=2, t_fin=4)], 2, preferred=1), 1)
        self.assertEqual(edits.range_at([dict(t_ini=1, t_fin=2), dict(t_ini=2, t_fin=4)], 2), 0)
        self.assertIsNone(edits.range_at([dict(t_ini=1, t_fin=2)], 5))

    def test_state_actions_are_explicit_not_toggles(self):
        self.assertEqual(edits.state_for("edit.accept"), "accepted")
        self.assertEqual(edits.state_for("edit.toggle"), "disabled")
        self.assertEqual(edits.state_for("edit.activate"), "proposed")
        with self.assertRaises(ValueError):
            edits.state_for("edit.split")

    def test_split_layer_item_keeps_metadata_and_orders_next_item(self):
        layer = layers.new_layer(master_fixture(), "Pedidos")
        item = layers.new_item(10, 20, "Tema", "pedido")
        item["ranges"].append(dict(t_ini=30, t_fin=40))
        item["state"] = "accepted"
        layer["items"] = [item]
        updated, twin = edits.split_layer_item(layer, item["item_id"], 15)
        self.assertEqual([(r["t_ini"], r["t_fin"]) for r in updated["items"][0]["ranges"]], [(10, 15), (30, 40)])
        self.assertEqual(twin["ranges"], [dict(t_ini=15, t_fin=20)])
        self.assertEqual((twin["label"], twin["comment"], twin["state"]), ("Tema", "pedido", "accepted"))
        self.assertNotEqual(twin["item_id"], item["item_id"])
        self.assertEqual(len(layers.validate_items(updated["items"], 60)), 2)
        self.assertEqual(layer["items"][0]["ranges"][0]["t_fin"], 20)     # el original no se toca
        with self.assertRaises(ValueError):
            edits.split_layer_item(layer, item["item_id"], 25)
        ordered = updated["items"]
        self.assertEqual(edits.next_item(ordered, item["item_id"], +1)["item_id"], twin["item_id"])
        self.assertIsNone(edits.next_item(ordered, twin["item_id"], +1))
        self.assertEqual(edits.next_item(ordered, None, +1)["item_id"], item["item_id"])
        self.assertEqual(edits.next_item(ordered, None, -1)["item_id"], twin["item_id"])

    def test_split_cut_and_chunk(self):
        document = editorial_trims.new_document(FINGERPRINT, 60.0)
        cut = editorial_trims.add_cut(document, 10, 20, origin="silence", reason="hueco", accepted=True)
        updated, twin = edits.split_cut(document, cut["cut_id"], 12.5)
        by_id = {c["cut_id"]: c for c in updated["cuts"]}
        self.assertEqual((by_id[cut["cut_id"]]["t_ini"], by_id[cut["cut_id"]]["t_fin"]), (10, 12.5))
        self.assertEqual((twin["t_ini"], twin["t_fin"], twin["origin"], twin["accepted"]), (12.5, 20, "silence", True))
        self.assertTrue(by_id[cut["cut_id"]]["edited"] and twin["edited"])
        self.assertEqual(document["cuts"][0]["t_fin"], 20)                 # copia
        self.assertEqual(editorial_trims.enabled_intervals(updated), [(10.0, 20.0)])   # la unión no cambia
        plan = plan_fixture(master_fixture(), [0, 30, 60])
        new_plan, chunk = edits.split_chunk(plan, "chunk-001", 12)
        self.assertEqual([(c["chunk_id"], c["t_ini"], c["t_fin"]) for c in new_plan["chunks"]],
                         [("chunk-001", 0, 12), ("chunk-003", 12, 30), ("chunk-002", 30, 60)])
        self.assertEqual(chunk["title"], "Tema 1 (2)")
        self.assertEqual(edits.next_chunk_id(new_plan), "chunk-004")


class AcceptedTests(unittest.TestCase):
    def test_accepted_is_additive_and_export_ignores_it(self):
        document = editorial_trims.new_document(FINGERPRINT, 60.0)
        cut = editorial_trims.add_cut(document, 10, 20)
        self.assertFalse(cut["accepted"])
        legacy = editorial_trims.validate_document({**document, "cuts": [
            {"cut_id": "cut-000009", "t_ini": 1.0, "t_fin": 2.0, "origin": "user"}]})
        self.assertFalse(legacy["cuts"][0]["accepted"])            # archivos viejos cargan igual
        cut["accepted"] = True
        self.assertEqual(layers.cut_state(cut), "accepted")
        cut["enabled"] = False
        self.assertEqual(layers.cut_state(cut), "disabled")
        cut["enabled"] = True
        self.assertEqual(editorial_trims.enabled_intervals(document), [(10.0, 20.0)])
        view = layers.adapters(master_fixture(), trims=document)[0]
        self.assertEqual(view["items"][0]["state"], "accepted")
        cut["accepted"] = False
        self.assertEqual(layers.adapters(master_fixture(), trims=document)[0]["items"][0]["state"], "proposed")

    def test_reanalysis_and_split_keep_accepted(self):
        master = master_fixture()
        document = editorial_trims.new_document(FINGERPRINT, 60.0)
        editorial_trims.apply_silence_analysis(document, editorial_trims.analyze_silences(master))
        document["cuts"][1]["accepted"] = True
        ids = [c["cut_id"] for c in document["cuts"]]
        editorial_trims.apply_silence_analysis(document, editorial_trims.analyze_silences(master))
        cuts = {c["cut_id"]: c for c in document["cuts"]}
        self.assertTrue(cuts[ids[1]]["accepted"])
        self.assertFalse(cuts[ids[0]]["accepted"])
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "trims.json"
            editorial_trims.save_document(path, document)
            raw = json.loads(path.read_text(encoding="utf-8"))
            self.assertTrue(next(c for c in raw["cuts"] if c["cut_id"] == ids[1])["accepted"])
            paths = editorial_trims.write_review_package(tmp, master, None, document)
            review = paths["completo"].read_text(encoding="utf-8")
            self.assertIn(f"⟂ RECORTE `{ids[1]}` · silencio · aceptado por el editor", review)


class RegistroReemplazarTests(unittest.TestCase):
    def test_replace_revalidates_saves_keeps_identity_and_notifies(self):
        with tempfile.TemporaryDirectory() as tmp:
            video = Path(tmp) / "clip.mp4"
            video.write_bytes(b"x")
            fp = dict(size=1, hash_muestreado="a", inventario_sha256="b")
            reg = marcas.Registro(video, fp, 60.0)
            first = reg.agregar_region(1, 3, decision="incluir", prompt="uno")
            reg.agregar_punto(10)
            snapshot = copy.deepcopy(reg.marcas)
            revision = reg.revision
            reg.editar(first, t_ini=2, prompt="cambiado")
            reg.borrar(reg.marcas[-1])
            events = []
            reg.suscribir(lambda: events.append(1))
            self.assertEqual(reg.reemplazar(snapshot), 2)
            self.assertEqual(reg.marcas, snapshot)
            self.assertIs(reg.marcas[0], first)                        # misma identidad de objeto
            self.assertEqual(first["prompt"], "uno")
            self.assertGreater(reg.revision, revision)
            self.assertEqual(events, [1])
            self.assertEqual(json.loads(marcas.sidecar_path(video).read_text(encoding="utf-8"))["revision"],
                             reg.revision)
            self.assertGreaterEqual(reg.next_id, 3)
            with self.assertRaisesRegex(ValueError, "inválida"):
                reg.reemplazar([dict(id="m0001", tipo="region", t_ini=5, t_fin=1)])
            self.assertEqual(reg.marcas, snapshot)                     # la transacción restauró


if __name__ == "__main__":
    unittest.main()
