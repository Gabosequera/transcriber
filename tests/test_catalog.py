import copy
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest import mock

import editorial_catalog as catalog
from editorial_io import atomic_write_json, read_json
from test_projects import fixture


class CatalogTests(unittest.TestCase):
    def test_move_copy_rebuild_and_changed_master(self):
        master=fixture()
        fp=master['media']['fingerprint']
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)/'conjunto'
            path=atomic_write_json(root/'proyecto/editorial/original.editorial.master.json',master)
            source=root/'renombrado.mp4'
            source.write_bytes(b'fixture')
            found,warnings=catalog.discover(source,fp,root=root)
            self.assertEqual(len(found),1)
            self.assertEqual(warnings,[])
            shutil.copytree(root,Path(tmp)/'copia')
            moved=Path(tmp)/'copia'
            found,_=catalog.discover(moved/'renombrado.mp4',fp,root=moved)
            self.assertTrue(found[0]['path'].startswith(str(moved)))
            (moved/'.transcriptor/catalog.json').unlink()
            self.assertEqual(len(catalog.discover(moved/'renombrado.mp4',fp,root=moved)[0]),1)
            other=copy.deepcopy(master)
            other['media']['fingerprint']['hash_muestreado']='distinto'
            atomic_write_json(path,other)
            self.assertEqual(catalog.discover(source,fp,root=root)[0],[])

    def test_ambiguous_metadata_is_not_chosen_and_copies_collapse(self):
        master=fixture()
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            atomic_write_json(root/'one.editorial.master.json',master)
            atomic_write_json(root/'two.editorial.master.json',master)
            self.assertEqual(len(catalog.discover(root/'video.mp4',master['media']['fingerprint'],root=root)[0]),1)
            other=copy.deepcopy(master)
            other['project']['name']='otra interpretación'
            atomic_write_json(root/'three.editorial.master.json',other)
            self.assertEqual(len(catalog.discover(root/'video.mp4',master['media']['fingerprint'],root=root)[0]),2)

    def test_corrupt_catalog_rebuilds_and_corrupt_master_is_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            master=fixture()
            atomic_write_json(root/'one.editorial.master.json',master)
            (root/'broken.editorial.master.json').write_text('{')
            catalog.scan(root)
            (root/'.transcriptor/catalog.json').write_text('{')
            found,warnings=catalog.discover(root/'video.mp4',master['media']['fingerprint'],root=root)
            self.assertEqual(len(found),1)
            self.assertTrue(warnings)
            self.assertEqual(read_json(root/'.transcriptor/catalog.json')['schema'],'editorial-catalog/1')
