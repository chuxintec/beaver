import json
from pathlib import Path
import tempfile
import unittest
from beaver.io import write_json, sha
from beaver.wiring import plan
from test_adoption import candidate


class WiringTests(unittest.TestCase):
    def fixture(self, root):
        project=candidate(root/'project')
        asset=project/'Assets/Panel.prefab';asset.write_text('%YAML 1.1\n--- !u!1 &1\nGameObject: {}\n')
        Path(str(asset)+'.meta').write_text('guid: '+'a'*32+'\n')
        button=dict(nodePath='Submit',type='UnityEngine.UI.Button',assignableTypes=['UnityEngine.UI.Button','UnityEngine.Component','UnityEngine.Object'],fields=[])
        owner=dict(nodePath='',type='Example.Panel',assignableTypes=['Example.Panel','UnityEngine.MonoBehaviour','UnityEngine.Object'],fields=[dict(name='submit',propertyPath='submit',acceptedType='UnityEngine.UI.Button',referenceState='null',objectReference=None)],methods=[dict(name='Submit',returnType='System.Void',parameterTypes=[],public=True,static=False)])
        snapshot=dict(schemaVersion=1,mode='static-prefab',assets=[dict(path='Assets/Panel.prefab',sourceSha256=sha(asset.read_bytes()),metaSha256=sha(Path(str(asset)+'.meta').read_bytes()),nodes=[dict(path=''),dict(path='Submit')],components=[owner,button],events=[dict(nodePath='Submit',field='onClick',persistentCalls=[])])])
        row=dict(asset='Assets/Panel.prefab',nodePath='',componentType='Example.Panel',field='submit',source='approved UI spec panel.submit role')
        return project,snapshot,dict(schemaVersion=1,bindings=[row])

    def run_plan(self, root, project, snapshot, spec):
        write_json(root/'snapshot.json',snapshot);write_json(root/'spec.json',spec)
        return plan(project,root/'snapshot.json',root/'spec.json',root/'output')

    def test_unique_compiled_type_generates_guarded_existing_transaction(self):
        with tempfile.TemporaryDirectory() as d:
            r=Path(d).resolve();project,snapshot,spec=self.fixture(r)
            spec['bindings'].append(dict(asset='Assets/Panel.prefab',nodePath='Submit',componentType='UnityEngine.UI.Button',field='onClick',kind='event',target=dict(nodePath='',componentType='Example.Panel'),method='Submit',source='approved Submit action'))
            result=self.run_plan(r,project,snapshot,spec)
            self.assertEqual(result['status'],'planned')
            batch=json.loads(Path(result['batch']['report']).read_text())
            actions=batch['spec']['steps'][0]['actions']
            self.assertEqual([a['kind'] for a in actions],['binding.reference','binding.event'])
            self.assertEqual(actions[0]['value'],'null')
            self.assertFalse(actions[0]['flag'])
            self.assertEqual(actions[1]['value'],'empty')
            self.assertEqual(batch['writeSet'],['Assets/Panel.prefab','Assets/Panel.prefab.meta'])

    def test_ambiguous_type_does_not_pick_first_or_create_executable_plan(self):
        with tempfile.TemporaryDirectory() as d:
            r=Path(d).resolve();project,snapshot,spec=self.fixture(r)
            snapshot['assets'][0]['components'].append(dict(snapshot['assets'][0]['components'][1],nodePath='Another'))
            result=self.run_plan(r,project,snapshot,spec)
            self.assertEqual(result['status'],'blocked');self.assertNotIn('batch',result)
            self.assertIn('observed 2',result['bindings'][0]['reason'])

    def test_nonempty_binding_requires_explicit_replacement(self):
        with tempfile.TemporaryDirectory() as d:
            r=Path(d).resolve();project,snapshot,spec=self.fixture(r)
            field=snapshot['assets'][0]['components'][0]['fields'][0]
            field.update(referenceState='bound',objectReference=dict(scope='node',nodePath='OldSubmit',componentType='UnityEngine.UI.Button'))
            result=self.run_plan(r,project,snapshot,spec)
            self.assertEqual(result['status'],'blocked');self.assertIn('protected',result['bindings'][0]['reason'])

    def test_stale_native_snapshot_cannot_generate_plan(self):
        with tempfile.TemporaryDirectory() as d:
            r=Path(d).resolve();project,snapshot,spec=self.fixture(r)
            (project/'Assets/Panel.prefab').write_text('later manual edit')
            result=self.run_plan(r,project,snapshot,spec)
            self.assertEqual(result['status'],'blocked');self.assertNotIn('batch',result)
            self.assertIn('drift',result['bindings'][0]['reason'])

    def test_empty_method_or_existing_event_is_not_replaced_with_stub(self):
        with tempfile.TemporaryDirectory() as d:
            r=Path(d).resolve();project,snapshot,spec=self.fixture(r)
            spec['bindings']=[dict(asset='Assets/Panel.prefab',nodePath='Submit',componentType='UnityEngine.UI.Button',field='onClick',kind='event',target=dict(nodePath='',componentType='Example.Panel'),method='MissingMethod',source='approved callback')]
            result=self.run_plan(r,project,snapshot,spec)
            self.assertEqual(result['status'],'blocked');self.assertIn('observed 0',result['bindings'][0]['reason'])


if __name__=='__main__':unittest.main()
