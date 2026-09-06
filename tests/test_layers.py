import copy
from pathlib import Path
import tempfile
import unittest

import editorial_layers as layers
from editorial_io import atomic_write_json
from test_projects import fixture


class LayersTests(unittest.TestCase):
    def test_manual_layer_before_pipeline_survives_real_master(self):
        master = fixture()
        context = layers.media_context(dict(path='video.mkv', duracion=12),master['media']['fingerprint'])
        with tempfile.TemporaryDirectory() as tmp:
            store = layers.LayerStore(tmp,context)
            layer = layers.new_layer(context,'Pedido previo')
            layer['items'] = [layers.new_item(1,2,comment='Busca el contexto después de transcribir')]
            store.save(layer)
            loaded = layers.LayerStore(tmp,master)
            self.assertEqual(loaded.visible()[0]['items'],layer['items'])
            self.assertNotEqual(loaded.source_digest,store.source_digest)

    def test_ai_cannot_restore_deleted_items_or_their_children(self):
        master=fixture()
        with tempfile.TemporaryDirectory() as tmp:
            store=layers.LayerStore(tmp,master)
            layer=layers.new_layer(master,'Temas')
            parent=layers.new_item(0,3)
            child=layers.new_item(1,2)
            child['parent_id']=parent['item_id']
            layer['deleted_item_ids']=[parent['item_id']]
            saved=store.save(layer)
            snapshot=layers.write_snapshot(tmp,master,store.visible())
            response=dict(schema=layers.PROPOSAL,source_master_digest=snapshot['source_master_digest'],
                          source_layers_digest=snapshot['source_layers_digest'],
                          layer={**saved,'items':[parent,child]})
            self.assertEqual(layers.merge_response(store,response,snapshot)['items'],[])

    def test_roundtrip_revision_and_external_conflict(self):
        master = fixture()
        with tempfile.TemporaryDirectory() as tmp:
            store = layers.LayerStore(tmp, master)
            layer = layers.new_layer(master, "Preguntas")
            layer["items"] = [layers.new_item(2, 3, "Pedido", "Busca dónde retoman esto")]
            saved = store.save(layer)
            other = layers.LayerStore(tmp, master)
            self.assertEqual(other.visible(), [saved])
            other.save({**saved, "name": "Cambio externo"})
            with self.assertRaisesRegex(ValueError, "fuera"):
                store.save(saved)
            other.delete(saved["layer_id"])
            self.assertEqual(layers.LayerStore(tmp, master).visible(), [])

    def test_bad_identity_ranges_and_paths_rejected(self):
        master = fixture()
        valid = layers.new_layer(master, "Prueba")
        valid["items"] = [layers.new_item(1, 2)]
        for key, value in (("layer_id", "../foo"), ("layer_id", "CON"), ("color", "red"),
                           ("media_fingerprint", {})):
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                layers.validate_layer({**valid, key:value}, master)
        for a,b in ((True,2), (1,float('nan')), (3,2), (-1,2), (1,13)):
            bad = copy.deepcopy(valid)
            bad["items"][0]["ranges"] = [dict(t_ini=a,t_fin=b)]
            with self.assertRaises(ValueError):
                layers.validate_layer(bad, master)

    def test_recurrence_hierarchy_and_cycles(self):
        topic = layers.new_item(0,3,"Tema")
        topic["ranges"].append(dict(t_ini=7,t_fin=12))
        sub = layers.new_item(8,9,"Subtema")
        sub["parent_id"] = topic["item_id"]
        self.assertEqual(len(layers.validate_items([topic,sub],12)),2)
        sub["ranges"][0] = dict(t_ini=4,t_fin=5)
        with self.assertRaisesRegex(ValueError,"fuera"):
            layers.validate_items([topic,sub],12)
        topic["parent_id"] = sub["item_id"]
        with self.assertRaisesRegex(ValueError,"cíclica"):
            layers.validate_items([topic,sub],12)

    def test_response_keeps_human_edits_and_checks_digests(self):
        master = fixture()
        with tempfile.TemporaryDirectory() as tmp:
            store = layers.LayerStore(tmp,master)
            layer = layers.new_layer(master,"Notas")
            layer["items"] = [layers.new_item(1,2,"Humano")]
            saved=store.save(layer)
            snapshot=layers.write_snapshot(tmp,master,store.visible())
            response=dict(schema=layers.PROPOSAL, source_master_digest=snapshot['source_master_digest'],
                          source_layers_digest=snapshot['source_layers_digest'],layer=copy.deepcopy(saved))
            response['layer']['items'][0]['label']='AI'
            response['layer']['items'].append(layers.new_item(8,9,'AI nueva'))
            result=layers.merge_response(store,response,snapshot)
            self.assertEqual({i['label'] for i in result['items']},{'Humano','AI nueva'})
            response['source_layers_digest']='viejo'
            with self.assertRaisesRegex(ValueError,'cambiaron'):
                layers.merge_response(store,response,snapshot)

    def test_author_adapter_is_a_view_of_original_prompts(self):
        marks=[dict(id='m0001',tipo='region',t_ini=1,t_fin=3,decision='excluir',prompt='Revisar tangente')]
        before=copy.deepcopy(marks)
        view=layers.adapters(fixture(),marks=marks)[0]
        self.assertEqual(view['items'][0]['state'],'disabled')
        self.assertEqual(view['items'][0]['comment'],'Revisar tangente')
        view['items'][0]['comment']='solo snapshot'
        self.assertEqual(marks,before)


class LayerDetailBarHelpersTests(unittest.TestCase):
    """Helpers puros de la barra de detalle (sin Tk)."""

    def test_elide_fits_and_marks_the_cut(self):
        measure=lambda s:7*len(s)
        self.assertEqual(layers.elide(measure,'corto',100),'corto')
        cut=layers.elide(measure,'comentario bastante largo para la AI',100)
        self.assertTrue(cut.endswith('…') and measure(cut)<=100 and len(cut)>5,cut)
        self.assertEqual(layers.elide(measure,'nada',3),'')
        self.assertEqual(layers.elide(measure,'x',None),'x')

    def test_ranges_summary_formats(self):
        self.assertEqual(layers.clock(3725.25),'1:02:05.2')
        self.assertEqual(layers.ranges_summary([dict(t_ini=12,t_fin=40.5)]),'0:12.0 – 0:40.5 · 28.5 s')
        self.assertEqual(layers.ranges_summary([dict(t_ini=62,t_fin=62)]),'1:02.0 · punto')
        self.assertEqual(layers.ranges_summary([dict(t_ini=12,t_fin=20),dict(t_ini=300,t_fin=340.5)]),
                         '2 tramos · 0:12.0 – 5:40.5 · 48.5 s')
