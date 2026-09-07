"""Herramientas de mouse (§7) y fusión de solapes (§10): aritmética pura."""
import random
import unittest

import editorial_edits as edits
import editorial_trims
from test_trims import FINGERPRINT


PARTS = [("a", 10, 20), ("b", 30, 40), ("c", 50, 60)]


class BoxTests(unittest.TestCase):
    def test_box_add_creates_extends_or_merges(self):
        self.assertEqual(edits.box_add(PARTS, 22, 28), [("create", 22, 28)])
        self.assertEqual(edits.box_add(PARTS, 15, 25), [("update", "a", 10, 25)])       # estira
        self.assertEqual(edits.box_add(PARTS, 5, 12), [("update", "a", 5, 20)])
        self.assertEqual(edits.box_add(PARTS, 12, 18), [("update", "a", 10, 20)])       # dentro: nada cambia
        self.assertEqual(edits.box_add(PARTS, 15, 55), [("merge", "a", ["b", "c"], 10, 60)])
        self.assertEqual(edits.box_add(PARTS, 20, 30), [("create", 20, 30)])            # solo toca bordes
        self.assertEqual(edits.box_add([], 1, 2), [("create", 1, 2)])
        with self.assertRaises(ValueError):
            edits.box_add(PARTS, 1, 1.01)

    def test_box_subtract_trims_splits_or_deletes(self):
        self.assertEqual(edits.box_subtract(PARTS, 5, 15), [("update", "a", 15, 20)])   # recorta inicio
        self.assertEqual(edits.box_subtract(PARTS, 15, 25), [("update", "a", 10, 15)])  # recorta fin
        self.assertEqual(edits.box_subtract(PARTS, 12, 18), [("split", "a", (10, 12), (18, 20))])
        self.assertEqual(edits.box_subtract(PARTS, 5, 25), [("delete", "a")])
        self.assertEqual(edits.box_subtract(PARTS, 15, 55),
                         [("update", "a", 10, 15), ("delete", "b"), ("update", "c", 55, 60)])
        self.assertEqual(edits.box_subtract(PARTS, 20, 30), [])                        # solo bordes
        # resto más corto que un fotograma: se descarta con el item
        self.assertEqual(edits.box_subtract(PARTS, 10.01, 25), [("delete", "a")])

    def test_marquee_selects_the_lane_with_most_items(self):
        lanes = {"recortes": PARTS, "capa": [("x", 12, 14), ("y", 16, 18), ("z", 100, 110)]}
        self.assertEqual(edits.marquee_select(lanes, 11, 19), ("capa", ["x", "y"]))
        self.assertEqual(edits.marquee_select(lanes, 25, 65), ("recortes", ["b", "c"]))
        self.assertEqual(edits.marquee_select(lanes, 70, 90), (None, []))
        self.assertEqual(edits.marquee_select({}, 0, 1), (None, []))


class CoalesceTests(unittest.TestCase):
    def document(self, cuts):
        document = editorial_trims.new_document(FINGERPRINT, 1000.0)
        for spec in cuts:
            editorial_trims.add_cut(document, spec[0], spec[1], origin=spec[2] if len(spec) > 2 else "user",
                                    reason=spec[3] if len(spec) > 3 else "", enabled=spec[4] if len(spec) > 4 else True)
        return document

    def test_strict_overlap_merges_with_actor_rules_and_edges_do_not(self):
        document = self.document([(10, 20, "silence", "hueco"), (15, 30, "user", "mío"), (30, 40, "user", "otro")])
        actor = document["cuts"][1]
        actor["accepted"] = True
        removed = editorial_trims.coalesce(document, actor_id=actor["cut_id"])
        self.assertEqual([c["cut_id"] for c in removed], ["cut-000001"])
        self.assertEqual([(c["t_ini"], c["t_fin"]) for c in document["cuts"]], [(10.0, 30.0), (30.0, 40.0)])
        merged = document["cuts"][0]
        self.assertEqual((merged["cut_id"], merged["origin"], merged["accepted"], merged["reason"]),
                         (actor["cut_id"], "user", True, "mío · hueco"))
        self.assertTrue(merged["edited"])
        self.assertEqual(editorial_trims.coalesce(document), [])                     # idempotente
        # sin actor gana el más largo y conserva la evidencia de la AI
        document = self.document([(10, 20, "ai", "tangente"), (12, 40, "silence", "hueco")])
        document["cuts"][0]["evidence"] = {"planner": "ai"}
        document["cuts"][0]["lane"] = "main"                                         # mismo carril
        editorial_trims.coalesce(document)
        self.assertEqual(len(document["cuts"]), 1)
        self.assertEqual(document["cuts"][0]["origin"], "silence")
        self.assertEqual(document["cuts"][0]["evidence"], {"planner": "ai"})

    def test_lanes_and_enabled_state_never_cross(self):
        document = self.document([(10, 20, "ai", "ai"), (15, 30, "silence", "sil")])
        editorial_trims.coalesce(document)
        self.assertEqual(len(document["cuts"]), 2)                                   # ai vs main
        self.assertEqual(editorial_trims.cut_lane(document["cuts"][0]), "ai")
        document = self.document([(10, 20, "user", "", True), (15, 30, "user", "", False)])
        editorial_trims.coalesce(document)
        self.assertEqual(len(document["cuts"]), 2)                                   # activo vs desactivado
        self.assertEqual(editorial_trims.enabled_intervals(document), [(10.0, 20.0)])

    def test_random_documents_keep_enabled_intervals_and_are_idempotent(self):
        random.seed(7)
        for _ in range(200):
            document = editorial_trims.new_document(FINGERPRINT, 1000.0)
            for _ in range(random.randint(1, 25)):
                a = random.uniform(0, 990)
                cut = editorial_trims.add_cut(document, a, a + random.uniform(0.1, 30),
                                              origin=random.choice(editorial_trims.ORIGINS))
                cut["enabled"] = random.random() < .7
                if random.random() < .3:
                    cut["lane"] = random.choice(["main", "ai", "extra"])
            before = editorial_trims.enabled_intervals(document)
            lanes_before = {lane: editorial_trims.merge_intervals(
                (c["t_ini"], c["t_fin"]) for c in document["cuts"] if editorial_trims.cut_lane(c) == lane and c["enabled"])
                for lane in {editorial_trims.cut_lane(c) for c in document["cuts"]}}
            editorial_trims.coalesce(document)
            self.assertEqual(editorial_trims.enabled_intervals(document), before)
            for lane, intervals in lanes_before.items():
                self.assertEqual(editorial_trims.merge_intervals(
                    (c["t_ini"], c["t_fin"]) for c in document["cuts"]
                    if editorial_trims.cut_lane(c) == lane and c["enabled"]), intervals)
            snapshot = [dict(c) for c in document["cuts"]]
            self.assertEqual(editorial_trims.coalesce(document), [])
            self.assertEqual([dict(c) for c in document["cuts"]], snapshot)
            for lane in lanes_before:
                for enabled in (True, False):
                    group = sorted((c for c in document["cuts"] if editorial_trims.cut_lane(c) == lane
                                    and c["enabled"] == enabled), key=lambda c: c["t_ini"])
                    for left, right in zip(group, group[1:]):
                        self.assertLessEqual(left["t_fin"], right["t_ini"])           # sin solapes estrictos
            editorial_trims.validate_document(document)


if __name__ == "__main__":
    unittest.main()
