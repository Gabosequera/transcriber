import copy
import tempfile
import unittest

import editorial_layers as layers
import editorial_topics as topics
from editorial_io import read_json, digest_json
from test_projects import fixture


class TopicsTests(unittest.TestCase):
    def setUp(self):
        self.master=fixture()
        # Mapa sin obstáculos para comprobar recurrencia con tiempos conocidos.
        for t in self.master['tracks'].values():
            t['words']=[]
            t['laughter']=[]
        self.master['conversation']['utterances']=[]
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store=layers.LayerStore(self.tmp.name,self.master)
        self.snapshot=layers.write_snapshot(self.tmp.name,self.master,[])
        self.request=topics.prepare(self.tmp.name,self.master,self.snapshot)

    def first(self):
        one=layers.new_item(1,3,'X inicial')
        two=layers.new_item(8,10,'X vuelve')
        return dict(schema=topics.SCHEMA,request_id=self.request['request_id'],
                    source_master_digest=self.request['source_master_digest'],
                    source_layers_digest=self.request['source_layers_digest'],
                    complete=True,items=[one,two],**{'pass':1})

    def second(self,first):
        old=read_json(self.store.root/'views/topics-pass1.json')
        item=layers.new_item(1,3,'X recurrente')
        item['ranges']=[r for i in old['items'] for r in i['ranges']]
        item['source_item_ids']=[i['item_id'] for i in old['items']]
        return {**first,'pass':2,'previous_pass_digest':digest_json(old),'items':[item]}

    def test_two_pass_loop_publishes_only_unified_map(self):
        first=self.first()
        topics.import_proposal(self.store,first,self.snapshot)
        self.assertEqual(self.store.visible(),[])
        second=self.second(first)
        topics.import_proposal(self.store,second,self.snapshot)
        layer=self.store.visible()[0]
        self.assertEqual(len(layer['items']),1)
        self.assertEqual(len(layer['items'][0]['ranges']),2)
        layer['items'][0].update(comment='Corrección humana',edited=True)
        self.store.save(layer)
        snapshot=layers.write_snapshot(self.tmp.name,self.master,self.store.visible())
        request=topics.prepare(self.tmp.name,self.master,snapshot)
        self.assertNotEqual(request['request_id'],self.request['request_id'])
        self.assertEqual(self.store.visible()[0]['items'][0]['comment'],'Corrección humana')

    def test_missing_pass_and_unread_items_rejected(self):
        first=self.first()
        with self.assertRaisesRegex(ValueError,'orden'):
            topics.import_proposal(self.store,{**first,'pass':2},self.snapshot)
        topics.import_proposal(self.store,first,self.snapshot)
        second=self.second(first)
        for changed in ({**second,'previous_pass_digest':'viejo'},
                        {**second,'complete':False},
                        {**second,'source_master_digest':'ajeno'}):
            with self.assertRaises(ValueError):
                topics.import_proposal(self.store,changed,self.snapshot)
        second['items'][0]['source_item_ids'].pop()
        with self.assertRaises(ValueError):
            topics.import_proposal(self.store,second,self.snapshot)

    def test_edits_during_analysis_invalidate_response(self):
        first=self.first()
        snapshot={**self.snapshot,'source_layers_digest':'nuevo'}
        with self.assertRaisesRegex(ValueError,'cambiaron'):
            topics.import_proposal(self.store,first,snapshot)

    def test_rejects_nonfinite_and_outside_scope(self):
        first=self.first()
        first['items'][0]['ranges'][0]['t_fin']=float('inf')
        with self.assertRaises(ValueError):
            topics.import_proposal(self.store,first,self.snapshot)
