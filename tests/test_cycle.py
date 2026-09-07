"""Estado del ciclo con la AI (editorial_cycle.py): cada estado con una carpeta views/
reproducida a mano, incluido el «pedido viejo». Sin Tk."""
import json
import os
import tempfile
import unittest
from pathlib import Path

import editorial_cycle as cycle
from editorial_io import digest_json


class CycleStatusTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.views = Path(self.tmp.name) / "views"
        self.views.mkdir()
        self.clock = 1_700_000_000.0

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, name, content, *, later=1.0):
        """Escribe un archivo con mtime creciente (el orden de los pedidos importa)."""
        path = self.views / name
        if isinstance(content, (dict, list)):
            path.write_text(json.dumps(content), encoding="utf-8")
        else:
            path.write_text(content, encoding="utf-8")
        self.clock += later
        os.utime(path, (self.clock, self.clock))
        return path

    def topics_request(self, digest="d1", pass_required=1, request_id="req-1"):
        return {"schema": "editorial-topics-request/1", "request_id": request_id,
                "source_master_digest": "m", "source_layers_digest": digest,
                "pass_required": pass_required, "scope": {"t_ini": 0, "t_fin": 60}}

    def test_no_request(self):
        result = cycle.status(self.views)
        self.assertEqual(result["stage"], "none")
        self.assertIn("Sin pedido", result["text"])
        self.assertFalse(result["stale"])

    def test_topics_only_cycle_walks_pass1_pass2_and_done(self):
        self.write("topics-request.json", self.topics_request())
        result = cycle.status(self.views, layers_digest="d1")
        self.assertEqual(result["stage"], "topics_pass1")
        self.assertEqual(result["kind"], "topics")
        self.assertIn("pasada 1", result["text"])
        # pasada 1 validada por la app: la solicitud sube a 2 y existe topics-pass1.json
        self.write("topics-pass1.json", {"request_id": "req-1", "items": [
            {"item_id": "t1", "parent_id": None}, {"item_id": "t1a", "parent_id": "t1"},
            {"item_id": "t2", "parent_id": None}]})
        self.write("topics-request.json", self.topics_request(pass_required=2))
        result = cycle.status(self.views, layers_digest="d1")
        self.assertEqual(result["stage"], "topics_pass2")
        self.assertIn("2 temas, 1 subtemas", result["text"])
        # pasada 2 importada → capa creada; la revisión de solo temas termina ahí
        self.write("topics-pass2.json", {"request_id": "req-1", "items": [
            {"item_id": "t1", "parent_id": None}, {"item_id": "t1a", "parent_id": "t1"}]})
        result = cycle.status(self.views, layers_digest="otro")     # editar después ya no es «viejo»
        self.assertEqual(result["stage"], "topics_done")
        self.assertIn("1 temas y 1 subtemas", result["text"])
        self.assertFalse(result["stale"])

    def test_stale_when_layers_changed_after_prepare(self):
        self.write("topics-request.json", self.topics_request(digest="d1"))
        result = cycle.status(self.views, layers_digest="d2")
        self.assertEqual(result["stage"], "stale")
        self.assertTrue(result["stale"])
        self.assertIn("quedó viejo", result["text"])
        # sin digest actual no se puede afirmar nada: no está viejo
        self.assertFalse(cycle.status(self.views)["stale"])

    def test_full_review_goes_from_topics_to_trims_and_reports_import(self):
        self.write("topics-request.json", self.topics_request())
        self.write("trim-agent-request.md", "# Solicitud\nsource_layers_digest: d1\n")
        self.write("editorial-agent-request.md", "# Tarea 4\n")
        result = cycle.status(self.views, layers_digest="d1")
        self.assertEqual((result["stage"], result["kind"]), ("topics_pass1", "full"))
        self.assertIn("Después vienen los recortes", result["text"])
        self.write("topics-pass1.json", {"request_id": "req-1", "items": [{"item_id": "t1"}]})
        self.write("topics-request.json", self.topics_request(pass_required=2))
        self.write("topics-pass2.json", {"request_id": "req-1", "items": [{"item_id": "t1"}]})
        result = cycle.status(self.views, layers_digest="d1")
        self.assertEqual(result["stage"], "trims_wait")
        self.assertIn("Esperando trims.proposed.json", result["text"])
        # la propuesta aparece y la app la importa (digest registrado en trims.json)
        proposal = {"schema": "editorial-trims-proposal/1", "cuts": [{"t_ini": 1, "t_fin": 2}]}
        self.write("trims.proposed.json", proposal)
        self.write("trims.json", {"schema": "editorial-trims/1", "cuts": [],
                                  "lanes": [{"lane_id": "ai", "name": "Cortes sugeridos (AI)"}],
                                  "ai": {"proposal_digest": digest_json(proposal), "count": 3}})
        result = cycle.status(self.views, layers_digest="d9")   # editar después del import no es viejo
        self.assertEqual(result["stage"], "trims_done")
        self.assertIn("3 en «Cortes sugeridos (AI)»", result["text"])
        self.assertFalse(result["stale"])

    def test_trims_only_cycle_deep_mode_and_rejected_proposal_shows_error(self):
        self.write("trim-agent-request.md", "# Solicitud\nmode: deep\nlane: ai-deep\nsource_layers_digest: d1\n")
        result = cycle.status(self.views, layers_digest="d1")
        self.assertEqual((result["stage"], result["kind"]), ("trims_request", "trims"))
        self.assertIn("recortes profundos", result["text"])
        self.write("trims.proposed.json", {"schema": "editorial-trims-proposal/1", "cuts": []})
        result = cycle.status(self.views, layers_digest="d1", last_error="source_master_digest no coincide")
        self.assertEqual(result["stage"], "trims_rejected")
        self.assertIn("Último error: source_master_digest", result["text"])
        # sin error registrado, una propuesta no importada sigue «esperando»
        self.assertEqual(cycle.status(self.views, layers_digest="d1")["stage"], "trims_request")
        # editar un recorte después de preparar → viejo
        self.assertTrue(cycle.status(self.views, layers_digest="d2")["stale"])

    def test_newer_topics_request_wins_over_an_old_trims_request(self):
        self.write("trim-agent-request.md", "# viejo\n")
        self.write("topics-request.json", self.topics_request(), later=10)
        self.assertEqual(cycle.status(self.views)["kind"], "topics")

    def test_montage_cycle(self):
        self.write("topics-request.json", self.topics_request())
        self.write("montaje-request.json", {"request_id": "m-1", "source_layers_digest": "d1",
                                            "pass_required": 1, "target_seconds": 900}, later=5)
        result = cycle.status(self.views, layers_digest="d1")
        self.assertEqual((result["stage"], result["kind"]), ("montage_request", "montage"))
        self.assertIn("objetivo 15 min", result["text"])
        self.assertTrue(cycle.status(self.views, layers_digest="d2")["stale"])
        self.write("montaje-pass1.json", {"request_id": "m-1", "clips": [{}, {}], "total_seconds": 870})
        self.write("montaje-request.json", {"request_id": "m-1", "source_layers_digest": "d1",
                                            "pass_required": 2, "target_seconds": 900})
        result = cycle.status(self.views, layers_digest="d1")
        self.assertEqual(result["stage"], "montage_done")
        self.assertIn("2 clips", result["text"])
        self.assertIn("14.5 min", result["text"])


if __name__ == "__main__":
    unittest.main()
