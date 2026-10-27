import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from beaver.toolkit.evidence_manifest import verify


class EvidenceManifestTests(unittest.TestCase):
    def fixture(self,root):
        (root/'cdn').mkdir();(root/'cdn/a.bin').write_bytes(b'own');return dict(id='asset@sub',layer='cdn',variant='png',path='cdn/a.bin',bytes=3,sha256=hashlib.sha256(b'own').hexdigest())
    def test_two_formats_and_previous_change(self):
        for adapter in ['flat-v1','layers-v1']:
            with tempfile.TemporaryDirectory() as t:
                root=Path(t).resolve();row=self.fixture(root);doc=dict(schema_version=1,scope='own-v1',entries=[row]) if adapter=='flat-v1' else dict(schema_version=1,scope='own-v1',layers={'cdn':[dict(row)]})
                report=verify(root,doc,adapter);self.assertEqual(report['status'],'declared_closure_verified');self.assertEqual(report['counts']['verified'],1)
                (root/'cdn/a.bin').write_bytes(b'ow');self.assertEqual(verify(root,doc,adapter)['changed'],['cdn/a.bin'])
                previous=dict(schema_version=1,scope='old',entries=[dict(row,sha256='0'*64)])
                self.assertTrue(verify(root,doc,adapter,previous)['changed_since_previous'])
    def test_unverified_missing_extra_identity_and_no_payload(self):
        with tempfile.TemporaryDirectory() as t:
            root=Path(t).resolve();row=self.fixture(root);(root/'unexpected').write_text('own')
            doc=dict(schema_version=1,scope='declared',entries=[row,dict(row,path='missing'),dict(id='logical',layer='cdn',payload=False),dict(id='unknown',layer='cache',path='unverified')]);r=verify(root,doc,'flat-v1')
            self.assertEqual(r['missing'],['missing','unverified']);self.assertEqual(r['extra'],['unexpected']);self.assertEqual(r['counts']['no_payload'],1);self.assertTrue(r['duplicate_identities']);self.assertTrue(r['unverified']);self.assertFalse(r['server_completeness_proven'])
    def test_escape_alias_link_budget(self):
        with tempfile.TemporaryDirectory() as t:
            root=Path(t).resolve();row=self.fixture(root)
            for path in ['../outside','/outside','cdn/../a','A\\b']:
                with self.assertRaises(ValueError):verify(root,dict(schema_version=1,scope='own',entries=[dict(row,path=path)]),'flat-v1')
            with self.assertRaises(ValueError):verify(root,dict(schema_version=1,scope='own',entries=[row,dict(row,path='CDN/A.bin')]),'flat-v1')
            (root/'linked').symlink_to(root/'cdn/a.bin')
            with self.assertRaises(ValueError):verify(root,dict(schema_version=1,scope='own',entries=[row]),'flat-v1')
