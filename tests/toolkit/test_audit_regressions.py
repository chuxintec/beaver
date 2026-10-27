"""Independent counterexamples retained from the GREA product audit (evidence_manifest)."""
import hashlib
import tempfile
import unittest
from pathlib import Path

from beaver.toolkit.evidence_manifest import verify


class AuditRegressions(unittest.TestCase):
    def test_removed_identity_is_not_an_unchanged_manifest(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            (root / 'a').write_bytes(b'a')
            def entry(name):
                return dict(id=name, layer='source', path=name, bytes=1,
                            sha256=hashlib.sha256(name.encode()).hexdigest())
            previous = dict(schema_version=1, scope='same', entries=[entry('a'), entry('b')])
            current = dict(schema_version=1, scope='same', entries=[entry('a')])
            report = verify(root, current, 'flat-v1', previous)
            self.assertEqual(report['current_closure'], 'verified')
            self.assertEqual(report['manifest_diff']['removed'][0]['id'], 'b')
            self.assertEqual(report['status'], 'needs_review')


if __name__ == '__main__':
    unittest.main()
