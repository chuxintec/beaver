"""Real filesystem aliases and hostile package names, with legitimate controls."""
import contextlib
import io
import json
from pathlib import Path
import runpy
import struct
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from beaver.toolkit.support.safe_paths import relative_name, unique_paths
from beaver.toolkit import static_asset_copy as copy, unity_pilot as pilot
from beaver.toolkit import package_backup

ROOT = Path(__file__).resolve().parents[1]


def package(names):
    index_size = 4 + sum(12 + len(n.encode()) for n in names)
    offset = 14 + index_size
    entries = b''
    for name in names:
        data = name.encode()
        entries += struct.pack('>I', len(data)) + data + struct.pack('>II', offset, 1)
        offset += 1
    return b'\xbe' + b'\0'*4 + struct.pack('>II', index_size, len(names)) + b'\xed' + struct.pack('>I', len(names)) + entries + b'x'*len(names)


class WriteBoundaryTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.source = self.root/'Src'; self.source.mkdir()
        (self.source/'a').write_bytes(b'original')
        self.output = self.root/'out'

    def invoke(self, module, *args):
        with patch.object(sys, 'argv', ['tool', *map(str,args)]), contextlib.redirect_stdout(io.StringIO()):
            return module.main()

    def test_native_case_alias_never_writes_source(self):
        alias = self.root/'sRC'
        if not alias.exists(): self.skipTest('Filesystem is case-sensitive')
        self.assertTrue(alias.samefile(self.source))
        for maps in [['a=a'], ['.=nested']]:
            with self.assertRaises(ValueError): copy.copy_assets(self.source,alias/'OUT',maps)
        for d in ['Assets','Packages','ProjectSettings']: (self.source/d).mkdir()
        with self.assertRaises(ValueError): pilot.snapshot(self.source,'.',alias/'snapshot.json')
        manifest = self.root/'snapshot.json'; pilot.snapshot(self.source,'.',manifest)
        with self.assertRaises(ValueError): pilot.clone(manifest,alias/'OUT')
        self.assertFalse((self.source/'OUT').exists())
        self.assertTrue(pilot.verify(manifest)['unchanged'])
        self.assertEqual((self.source/'a').read_bytes(),b'original')

    def test_portable_names_and_aliases(self):
        for name in ['../x','/x','C:/x','C:x','x\\..\\z','x:stream','NUL','COM1.txt','x.','x ','a//b','a/./b','a\x00b']:
            with self.subTest(name=name), self.assertRaises(ValueError): relative_name(name)
        for names in [['a','A'],['é','e\u0301'],['a','a/b'],['a/b','a'],['A/x','a/y']]:
            with self.assertRaises(ValueError): unique_paths(names)
        with self.assertRaises(ValueError): copy.copy_assets(self.source,self.output,['a=A','a=a'])
        self.assertFalse(self.output.exists())

    def test_package_identity_rejected_before_archive_write(self):
        packages = self.source/'Packages'; packages.mkdir()
        (packages/'packages-lock.json').write_text(json.dumps({'dependencies':{'../../escape':{'source':'registry'}}}))
        with self.assertRaises(ValueError): package_backup.snapshot([self.source],self.output)
        self.assertFalse(self.output.exists())

    def test_package_legal_name_and_semver_build_are_preserved(self):
        name='com.example.my_tool';version='1.0.0-beta.1+build.2'
        packages=self.source/'Packages';package_dir=packages/name;package_dir.mkdir(parents=True)
        (package_dir/'package.json').write_text(json.dumps({'name':name,'version':version,'dependencies':{}}))
        (packages/'packages-lock.json').write_text(json.dumps({'dependencies':{name:{'source':'registry','version':version}}}))
        with contextlib.redirect_stdout(io.StringIO()): self.assertEqual(package_backup.snapshot([self.source],self.output),0)
        data=json.loads((self.output/'catalog.json').read_text())
        self.assertEqual(data['packages'][0]['id'],name+'@'+version)


if __name__ == '__main__': unittest.main()
