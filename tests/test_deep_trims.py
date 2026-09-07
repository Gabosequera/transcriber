"""Tarea 2 «modo profundo» (plan-montaje-ai.md §5): pedido con `mode`/`lane`, carril
`ai-deep` declarado al importar, la AI nunca escribe en «main», archivos viejos sin ese
carril siguen cargando. Sin Tk."""
import json
import tempfile
import unittest
from pathlib import Path

import editorial_chunks
import editorial_layers as layers
import editorial_trims
from test_trims import FINGERPRINT, master_fixture, plan_fixture


def proposal(master, cuts, **header):
    return {"schema": editorial_trims.SCHEMA_PROPOSAL, "planner": "deep-ai",
            "source_master_digest": editorial_chunks.source_master_digest(master),
            "cuts": cuts, **header}


class DeepRequestTests(unittest.TestCase):
    def test_deep_request_carries_mode_lane_and_rules(self):
        master = master_fixture()
        plan = plan_fixture(master, [0, 30, 60])
        document = editorial_trims.new_document(FINGERPRINT, 60.0)
        cut = editorial_trims.add_cut(document, 5, 8, origin="silence", reason="hueco")
        cut["accepted"] = True
        with tempfile.TemporaryDirectory() as tmp:
            paths = editorial_trims.write_review_package(tmp, master, plan, document, mode="deep",
                                                         layers_digest="abc123")
            text = paths["request"].read_text(encoding="utf-8")
            self.assertIn("MODO PROFUNDO", text)
            self.assertIn("\nmode: deep\n", text)
            self.assertIn("\nlane: ai-deep\n", text)
            self.assertIn("source_layers_digest: abc123", text)
            self.assertIn("Tarea 2 · modo profundo", text)
            self.assertIn("1 aceptados", text)
            self.assertIn('"mode": "deep"', text)
            self.assertIn("Cortes profundos (AI)", text)
            self.assertIn("lisuras, insultos, humor negro", text)
            normal = editorial_trims.write_review_package(tmp, master, plan, document)["request"].read_text(encoding="utf-8")
            self.assertNotIn("mode: deep", normal)
        with self.assertRaises(ValueError):
            editorial_trims.agent_request_markdown(master, [], document, mode="loco")


class DeepProposalTests(unittest.TestCase):
    def test_deep_proposal_lands_in_its_own_lane_and_keeps_the_first_pass(self):
        master = master_fixture()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            document = editorial_trims.new_document(FINGERPRINT, 60.0)
            first = editorial_trims.add_cut(document, 45.0, 46.0, origin="ai", reason="primera pasada")
            user = editorial_trims.add_cut(document, 55.0, 56.0, origin="user")
            trims_path = root / "trims.json"
            editorial_trims.save_document(trims_path, document)
            self.assertEqual([l["lane_id"] for l in document["lanes"]], ["main", "ai"])
            deep_path = root / "trims.proposed.json"
            deep_path.write_text(json.dumps(proposal(master, [
                {"t_ini": 30.5, "t_fin": 36.0, "reason": "leen el chat y no lo comentan", "confidence": 0.6}],
                mode="deep", lane="ai-deep")), encoding="utf-8")
            merged, validated = editorial_trims.import_proposal(trims_path, deep_path, master, None,
                                                                fingerprint=FINGERPRINT)
            self.assertEqual((validated["mode"], validated["lane"]), ("deep", "ai-deep"))
            lanes = {l["lane_id"]: l for l in merged["lanes"]}
            self.assertEqual(lanes["ai-deep"]["name"], "Cortes profundos (AI)")
            self.assertEqual(lanes["ai-deep"]["color"], "#b5638a")
            deep = [c for c in merged["cuts"] if c["lane"] == "ai-deep"]
            self.assertEqual(len(deep), 1)
            self.assertTrue(deep[0]["reason"].startswith("[profundo] leen el chat"))
            self.assertEqual(deep[0]["evidence"]["mode"], "deep")
            ids = {c["cut_id"] for c in merged["cuts"]}
            self.assertIn(first["cut_id"], ids)                  # la primera pasada no se toca
            self.assertIn(user["cut_id"], ids)
            self.assertEqual(merged["ai"]["lane"], "ai-deep")
            # una pasada normal posterior reemplaza SOLO los de «ai» y conserva los profundos
            normal_path = root / "trims.proposed.json"
            normal_path.write_text(json.dumps(proposal(master, [
                {"t_ini": 10.6, "t_fin": 11.9, "reason": "balbuceo", "confidence": 0.7}])), encoding="utf-8")
            again, validated = editorial_trims.import_proposal(trims_path, normal_path, master, None,
                                                               fingerprint=FINGERPRINT)
            self.assertEqual(validated["lane"], "ai")
            self.assertNotIn(first["cut_id"], {c["cut_id"] for c in again["cuts"]})
            self.assertEqual(len([c for c in again["cuts"] if c["lane"] == "ai-deep"]), 1)
            self.assertEqual(len([c for c in again["cuts"] if c["lane"] == "ai"]), 1)
            # la UI ve tres carriles: el profundo entre «ai» y «main»
            view = layers.order_layers(layers.adapters(master, trims=again), [])
            self.assertEqual([l["layer_id"] for l in view], ["trims:ai", "trims:ai-deep", "trims:main"])
            # el marcador de la revisión distingue la pasada profunda
            review = editorial_trims.review_markdown(master, editorial_trims.review_blocks(master, None)[0], again)
            self.assertIn("propuesto por la AI (profundo)", review)
            self.assertIn("· AI ·", review)
            # la exportación sigue uniendo los activos de TODOS los carriles
            self.assertEqual(len(editorial_trims.enabled_intervals(again)), 3)
            self.assertIn((55.0, 56.0), editorial_trims.enabled_intervals(again))

    def test_ai_never_writes_in_main_or_user_lanes_and_mode_is_validated(self):
        master = master_fixture()
        for lane in ("main", "lane-abc", "MAIN"):
            with self.subTest(lane=lane), self.assertRaisesRegex(ValueError, "ai\\*"):
                editorial_trims.validate_proposal(proposal(master, [], lane=lane), master, None)
        with self.assertRaisesRegex(ValueError, "mode desconocido"):
            editorial_trims.validate_proposal(proposal(master, [], mode="agresivo"), master, None)
        # deep sin lane explícita cae en ai-deep; content sin nada cae en ai
        self.assertEqual(editorial_trims.validate_proposal(proposal(master, [], mode="deep"), master, None)["lane"],
                         "ai-deep")
        self.assertEqual(editorial_trims.validate_proposal(proposal(master, []), master, None)["lane"], "ai")

    def test_old_document_without_deep_lane_loads_unchanged(self):
        legacy = {"schema": editorial_trims.SCHEMA_TRIMS, "media": dict(FINGERPRINT), "duration": 60.0,
                  "cuts": [{"cut_id": "cut-000001", "t_ini": 1.0, "t_fin": 2.0, "origin": "ai"}]}
        loaded = editorial_trims.validate_document(legacy, fingerprint=FINGERPRINT, duration=60.0)
        self.assertEqual([l["lane_id"] for l in loaded["lanes"]], ["main", "ai"])
        editorial_trims.ensure_lane(loaded, "ai-deep")
        self.assertEqual([l["lane_id"] for l in loaded["lanes"]], ["main", "ai", "ai-deep"])
        editorial_trims.ensure_lane(loaded, "ai-deep")                # idempotente
        self.assertEqual(len(loaded["lanes"]), 3)


if __name__ == "__main__":
    unittest.main()
