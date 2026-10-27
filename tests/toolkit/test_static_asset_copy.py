"""Copy semantics, preflight boundaries and the unified tool entry."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from beaver.toolkit.static_asset_copy import copy_assets



class StaticAssetCopyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve(); self.source = self.root/'source'; self.source.mkdir()
        (self.source/'index.html').write_bytes(b'<p>unchanged</p>\n')
        (self.source/'assets/empty').mkdir(parents=True)
        (self.source/'assets/中文.bin').write_bytes(bytes(range(256)))
        (self.source/'run.sh').write_text('#!/bin/sh\nexit 0\n'); (self.source/'run.sh').chmod(0o755)
        self.output = self.root/'new output'

    def test_nested_unicode_binary_empty_directories_and_independent_bytes(self):
        result = copy_assets(self.source,self.output,['index.html=home.html','assets=data','run.sh=bin/run.sh'])
        self.assertEqual(result['output'],str(self.output))
        self.assertEqual(result['mappings'][1],{'from':'assets','to':'data'})
        self.assertEqual((self.output/'data/中文.bin').read_bytes(),bytes(range(256)))
        self.assertTrue((self.output/'data/empty').is_dir())
        if os.name!='nt':self.assertTrue((self.output/'bin/run.sh').stat().st_mode & 0o111)
        (self.output/'home.html').write_text('changed')
        self.assertEqual((self.source/'index.html').read_bytes(),b'<p>unchanged</p>\n')

    def test_empty_output_reuse_and_nonempty_output_preservation(self):
        self.output.mkdir();copy_assets(self.source,self.output,['index.html=index.html'])
        before=(self.output/'index.html').read_bytes()
        with self.assertRaises(ValueError):copy_assets(self.source,self.output,['assets=assets'])
        self.assertEqual((self.output/'index.html').read_bytes(),before)

    def test_all_mappings_checked_before_output_creation(self):
        for bad in ['missing=late','index.html','index.html=','../index.html=x','index.html=../x',
                    'index.html=/absolute','index.html=C:\\outside','index.html=\\absolute','assets/../index.html=x','index.html=..\\x']:
            with self.subTest(mapping=bad):
                with self.assertRaises((ValueError,OSError)):copy_assets(self.source,self.output,['index.html=good',bad])
                self.assertFalse(self.output.exists())

    def test_source_output_and_target_overlap_rejected(self):
        for output in [self.source,self.source/'child',self.root]:
            with self.assertRaises(ValueError):copy_assets(self.source,output,['index.html=a'])
        for maps in [['index.html=x','run.sh=x'],['assets=x','index.html=x/nested'],['index.html=x/nested','assets=x']]:
            with self.assertRaises(ValueError):copy_assets(self.source,self.output,maps)
            self.assertFalse(self.output.exists())

    def test_source_symlinks_and_output_alias_cannot_bypass_boundaries(self):
        link=self.source/'linked'
        try:link.symlink_to(self.source/'assets',target_is_directory=True)
        except OSError:self.skipTest('Symlinks unavailable')
        with self.assertRaises(ValueError):copy_assets(self.source,self.output,['linked/中文.bin=copy'])
        (self.source/'assets/link').symlink_to(self.source/'index.html')
        with self.assertRaises(ValueError):copy_assets(self.source,self.output,['assets=data'])
        alias=self.root/'alias';alias.symlink_to(self.source,target_is_directory=True)
        with self.assertRaises(ValueError):copy_assets(self.source,alias/'inside',['index.html=x'])
        self.assertFalse((self.source/'inside').exists());self.assertFalse(self.output.exists())

    def test_special_file_rejected_without_opening_it(self):
        if not hasattr(os,'mkfifo'):self.skipTest('FIFO unavailable')
        os.mkfifo(self.source/'pipe')
        with self.assertRaises(ValueError):copy_assets(self.source,self.output,['index.html=first','pipe=second'])
        self.assertFalse(self.output.exists())

    def test_module_cli_exit_codes(self):
        from beaver.cli import main
        self.assertEqual(main(['tool','static_asset_copy','--source',str(self.source),'--output',str(self.output),'--map','assets=payload']),0)
        self.assertTrue((self.output/'payload/中文.bin').is_file())
        self.assertNotEqual(main(['tool','static_asset_copy','--unknown']),0)


if __name__=='__main__':unittest.main()
