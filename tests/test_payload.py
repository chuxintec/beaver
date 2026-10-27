from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
import hashlib
import json
import os
import tempfile
import unittest
from unittest.mock import patch
from beaver.io import write_json,load,read
from beaver.errors import BeaverError
from beaver.payload import manifest,verify_manifest,plan,apply,copy_owned,promote
from beaver.cli import main


def digest(value):return hashlib.sha256(value).hexdigest()


def fixture(root):
    source=root/'source';source.mkdir();(source/'a.bin').write_bytes(b'old');(source/'b.bin').write_bytes(b'stable')
    m=root/'manifest.json';write_json(m,dict(schemaVersion=1,contract='beaver-payload/1',files=[dict(path=name,bytes=len(value),sha256=digest(value)) for name,value in [('a.bin',b'old'),('b.bin',b'stable')]],aliases=[dict(id='alias-a',path='a.bin'),dict(id='alias-b',path='a.bin'),dict(id='stable',path='b.bin')]))
    return source,m


class PayloadTests(unittest.TestCase):
    def test_alias_override_independent_copies_and_atomic_complete_promotion(self):
        with tempfile.TemporaryDirectory() as temp:
            r=Path(temp).resolve();source,m=fixture(r);author=r/'author';author.mkdir();(author/'new.json').write_bytes(b'new')
            o=r/'overrides.json';write_json(o,dict(schemaVersion=1,entries=[dict(path='a.bin',target='payload/new.json',source='new.json',expectedSha256=digest(b'old'),sha256=digest(b'new'),bytes=3)]))
            p=plan(source,m,r/'target',r/'plan',o,author);self.assertFalse((r/'target').exists())
            result=apply(r/'plan/payload-plan.json',r/'transaction',p['planSha256'])
            self.assertTrue(result['atomicPromotion']);self.assertTrue(result['independentCopies'])
            self.assertEqual((source/'a.bin').read_bytes(),b'old')
            self.assertEqual((r/'target/a.bin').read_bytes(),b'old')
            self.assertNotEqual((source/'a.bin').stat().st_ino,(r/'target/a.bin').stat().st_ino)
            out=load(read(r/'target/payload-manifest.json'));aliases={x['id']:x['path'] for x in out['aliases']}
            self.assertEqual(aliases,{'alias-a':'payload/new.json','alias-b':'payload/new.json','stable':'b.bin'})
            self.assertEqual(verify_manifest(r/'target',r/'target/payload-manifest.json')['status'],'ok')

    def test_path_duplicate_parent_case_and_alias_collisions_are_not_collapsed(self):
        with tempfile.TemporaryDirectory() as temp:
            r=Path(temp).resolve();m=r/'bad.json'
            for paths in (['../escape'],['C:/escape'],['dir\\file'],['same','same'],['A/x','a/y'],['file','file/child'],['CON.txt']):
                write_json(m,dict(schemaVersion=1,files=[dict(path=p,sha256='a'*64,bytes=0) for p in paths]))
                with self.assertRaises(BeaverError):manifest(m)
            write_json(m,dict(schemaVersion=1,files=[dict(path='one',sha256='a'*64,bytes=0)],aliases=[dict(id='same',path='one')]*2))
            with self.assertRaises(BeaverError):manifest(m)

    def test_symlink_and_hardlink_rejected_before_any_shared_content_is_adopted(self):
        with tempfile.TemporaryDirectory() as temp:
            r=Path(temp).resolve();source,m=fixture(r)
            os.link(source/'a.bin',r/'external-hardlink')
            with self.assertRaises(BeaverError) as error:verify_manifest(source,m)
            self.assertEqual(error.exception.code,'PAYLOAD_SHARED_FILE')
            (r/'external-hardlink').unlink();(source/'a.bin').unlink();(r/'outside').write_bytes(b'old');(source/'a.bin').symlink_to(r/'outside')
            with self.assertRaises(BeaverError) as error:verify_manifest(source,m)
            self.assertEqual(error.exception.code,'PAYLOAD_SYMLINK')
            self.assertEqual((r/'outside').read_bytes(),b'old')

    def test_unlisted_is_blocked_or_explicit_partial_never_verified(self):
        with tempfile.TemporaryDirectory() as temp:
            r=Path(temp).resolve();source,m=fixture(r);(source/'uncovered').write_text('not claimed')
            with self.assertRaises(BeaverError):verify_manifest(source,m)
            result=verify_manifest(source,m,allow_unlisted=True)
            self.assertEqual(result['status'],'partial');self.assertEqual(result['unlistedCount'],1)
            with redirect_stdout(StringIO()) as stdout:
                code=main(['payload','verify','--root',str(source),'--manifest',str(m),'--allow-unlisted'])
            self.assertEqual(code,1);self.assertEqual(json.loads(stdout.getvalue())['status'],'partial')

    def test_drift_and_midcopy_failure_never_promote_partial_tree(self):
        with tempfile.TemporaryDirectory() as temp:
            r=Path(temp).resolve();source,m=fixture(r);p=plan(source,m,r/'target',r/'plan')
            (source/'a.bin').write_bytes(b'human')
            with self.assertRaises(BeaverError):apply(r/'plan/payload-plan.json',r/'drift',p['planSha256'])
            self.assertFalse((r/'target').exists());(source/'a.bin').write_bytes(b'old')
            calls=[]
            def failing(*args,**kwargs):
                calls.append(1)
                if len(calls)>1:raise OSError('controlled copy failure')
                return copy_owned(*args,**kwargs)
            with patch('beaver.payload.copy_owned',side_effect=failing):
                with self.assertRaises(BeaverError):apply(r/'plan/payload-plan.json',r/'failed',p['planSha256'])
            self.assertFalse((r/'target').exists());self.assertEqual((source/'a.bin').read_bytes(),b'old')
            journal=load(read(r/'failed/journal.json'));self.assertEqual(journal['status'],'failed-staging');self.assertFalse(journal['promoted']);self.assertTrue(Path(journal['stagingRoot']).is_dir())

    def test_concurrent_empty_destination_is_preserved_by_actual_exclusive_rename(self):
        with tempfile.TemporaryDirectory() as temp:
            r=Path(temp).resolve();source,m=fixture(r);p=plan(source,m,r/'target',r/'plan')
            def race(stage,target):
                target.mkdir();return promote(stage,target)
            with patch('beaver.payload.promote',side_effect=race):
                with self.assertRaises(BeaverError) as error:apply(r/'plan/payload-plan.json',r/'transaction',p['planSha256'])
            self.assertEqual(error.exception.code,'PAYLOAD_TARGET_EXISTS')
            self.assertEqual(list((r/'target').iterdir()),[])
            self.assertFalse(load(read(r/'transaction/journal.json'))['promoted'])
