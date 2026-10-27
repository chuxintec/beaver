import ast
import unittest
from pathlib import Path


class ImportBoundaries(unittest.TestCase):
    def test_evidence_and_authoring_utilities_do_not_import_engine_adapters(self):
        root = Path(__file__).resolve().parents[2]/'src/beaver/toolkit'
        for name in ('evidence_manifest.py', 'unity_asset_ledger.py', 'config_preflight.py', 'performance_compare.py', 'presentation_audit.py'):
            for node in ast.walk(ast.parse((root/name).read_text())):
                if isinstance(node, ast.ImportFrom):
                    self.assertNotIn(node.module, ('beaver.toolkit.cocos_assets', 'beaver.toolkit.unity_config_candidate'), name)


if __name__ == '__main__':
    unittest.main()
