from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from beaver.io import write_json
from beaver.errors import BeaverError
from beaver.unity import plan, apply
from beaver.codecheck import inspect_code
from test_adoption import candidate


class NativePlanTests(unittest.TestCase):
    def test_native_plan_rejects_paths_unknown_fields_and_drift_before_editor(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp).resolve();project=candidate(root/'project')
            spec=root/'spec.json'
            write_json(spec,dict(schemaVersion=1,operation='prefab.inspect',prefabPath='Assets/../Escape.prefab'))
            with self.assertRaises(BeaverError):plan(project,spec,root/'unsafe')
            asset=project/'Assets/Test.prefab';asset.write_text('synthetic plan input')
            write_json(spec,dict(schemaVersion=1,operation='prefab.inspect',prefabPath='Assets/Test.prefab'))
            result=plan(project,spec,root/'preview')
            asset.write_text('human-later')
            with patch('beaver.unity.editor_path',return_value=root/'Unity'),patch('beaver.unity.native_lock'):
                with self.assertRaises(BeaverError) as error:
                    apply(result['report'],root/'transaction',root/'Unity',result['planSha256'])
            self.assertEqual(error.exception.code,'UNITY_SOURCE_DRIFT')
            self.assertFalse((project/'Assets/BeaverTools').exists())

    def test_asmdef_unknown_dependency_is_partial_instead_of_compile_pass(self):
        with tempfile.TemporaryDirectory() as temp:
            project=candidate(Path(temp).resolve()/'project')
            write_json(project/'Assets/Example.asmdef',dict(name='Example',references=['Missing.Assembly']))
            result=inspect_code(project)
            self.assertEqual(result['status'],'partial')
            self.assertEqual(result['nativeCompilation'],'not_run')
            self.assertEqual(result['diagnostics'][0]['code'],'UNRESOLVED_ASMDEF_REFERENCE')
