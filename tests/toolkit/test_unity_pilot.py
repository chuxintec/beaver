import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

ROOT=Path(__file__).resolve().parents[2]
spec=importlib.util.spec_from_file_location('pilot',ROOT/'src/beaver/toolkit/unity_pilot.py');pilot=importlib.util.module_from_spec(spec);spec.loader.exec_module(pilot)

class PilotIsolationTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name).resolve();self.source=self.root/'original'
        for name in ['Assets','Packages','ProjectSettings']:(self.source/name).mkdir(parents=True)
        (self.source/'Assets/script.cs').write_text('original');(self.source/'Assets/script.cs.meta').write_text('guid: synthetic')
        (self.source/'ProjectSettings/ProjectVersion.txt').write_text('locked-editor')
        self.manifest=self.root/'snapshot.json'
    def freeze(self):pilot.snapshot(self.source,'.',self.manifest)
    def test_independent_copies_and_rollback_keep_original(self):
        self.freeze();pilot.clone(self.manifest,self.root/'baseline');pilot.clone(self.manifest,self.root/'candidate')
        (self.root/'candidate/Assets/script.cs').write_text('migration')
        self.assertTrue(pilot.verify(self.manifest)['unchanged']);self.assertTrue(pilot.verify(self.manifest,self.root/'baseline')['unchanged'])
        self.assertEqual(pilot.verify(self.manifest,self.root/'candidate')['changes'],['Assets/script.cs'])
        pilot.clone(self.manifest,self.root/'rollback');self.assertTrue(pilot.verify(self.manifest,self.root/'rollback')['unchanged'])
    def test_stale_and_new_files_rejected_before_clone(self):
        self.freeze();(self.source/'Assets/new.cs').write_text('concurrent')
        with self.assertRaises(ValueError):pilot.clone(self.manifest,self.root/'candidate')
        self.assertFalse((self.root/'candidate').exists())
    def test_existing_and_nested_outputs_are_never_overwritten(self):
        self.freeze()
        for output in [self.source/'child',self.root,self.source/'Assets/../../original/new']:
            with self.assertRaises(ValueError):pilot.clone(self.manifest,output)
        self.assertTrue(pilot.verify(self.manifest)['unchanged'])
    def test_symlink_inputs_and_ancestors_rejected(self):
        (self.source/'Assets/link').symlink_to(self.source/'ProjectSettings',target_is_directory=True)
        with self.assertRaises(ValueError):self.freeze()
        (self.source/'Assets/link').unlink();self.freeze()
        (self.root/'linked').symlink_to(self.root/'target',target_is_directory=True)
        with self.assertRaises(ValueError):pilot.clone(self.manifest,self.root/'linked/new')
    def test_manifest_tamper_and_paths_rejected(self):
        self.freeze();data=json.loads(self.manifest.read_text());data['files']['../outside']={};self.manifest.write_text(json.dumps(data))
        with self.assertRaises(ValueError):pilot.clone(self.manifest,self.root/'candidate')
        self.assertFalse((self.root/'candidate').exists())
    def test_explicit_oracle_input_included_and_caches_excluded(self):
        extra=self.source/'Evidence/frozen.json';extra.parent.mkdir();extra.write_text('fixture')
        pilot.snapshot(self.source,'.',self.manifest,['Evidence/frozen.json']);pilot.clone(self.manifest,self.root/'candidate')
        self.assertEqual((self.root/'candidate/Evidence/frozen.json').read_text(),'fixture')
        cache=self.source/'Assets/Library';cache.mkdir();(cache/'cache').write_text('cache')
        with self.assertRaises(ValueError):pilot.snapshot(self.source,'.',self.root/'other.json')

if __name__=='__main__':unittest.main()
