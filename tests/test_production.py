from pathlib import Path
import tempfile
import unittest
import json
from unittest.mock import patch
from beaver.io import write_json,sha
from beaver.production import plan,apply,validate
from beaver.project_profile import setup_plan,setup_apply,rebind_plan,PROFILE
from beaver.errors import BeaverError
from test_adoption import candidate


class ProductionTests(unittest.TestCase):
    def fixture(self,root,target='Assets/Game/New.json'):
        project=candidate(root/'project');spec=root/'binding.json';write_json(spec,dict(schemaVersion=1,engine='unity',name='Example',writableRoots=['Assets/Game']))
        p=setup_plan(project,spec,root/'bind-plan');setup_apply(p['report'],p['planSha256'],root/'bind-run')
        source=root/'Generated.json';source.write_text('{"version":"example-v1","items":[{"id":1}]}\n')
        spec=root/'production.json';write_json(spec,dict(schemaVersion=3,operation='production',profile=str(project/PROFILE),steps=[],fileEdits=[dict(path=target,source=str(source),sha256=sha(source.read_bytes()),expectedSha256=None)]))
        return project,spec,source

    def test_file_install_is_planned_with_meta_and_scoped_binding(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp).resolve();project,spec,source=self.fixture(root);r=plan(project,spec,root/'plan');d=json.loads(Path(r['report']).read_text())
            self.assertEqual(d['contract'],'beaver-production/3');self.assertIn('Assets/Game/New.json.meta',d['writeSet']);self.assertIsNone(d['before']['Assets/Game/New.json'])
            self.assertFalse((project/'Assets/Game/New.json').exists());self.assertEqual(d['projectBinding']['path'],str(project/PROFILE))

    def test_later_generated_input_drift_is_blocked_before_editor_or_target_write(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp).resolve();project,spec,source=self.fixture(root);r=plan(project,spec,root/'plan');source.write_text('later changed generator output')
            with patch('beaver.batch.editor_path',return_value=root/'Unity'),patch('beaver.batch.launch') as launch:
                with self.assertRaises(BeaverError) as e:apply(r['report'],root/'run',root/'Unity',r['planSha256'])
            self.assertEqual(e.exception.code,'PRODUCTION_SOURCE_DRIFT');launch.assert_not_called();self.assertFalse((project/'Assets/Game/New.json').exists())

    def test_outside_write_root_and_unknown_spec_fail_without_writes(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp).resolve();project,spec,_=self.fixture(root,'Assets/Human/New.json')
            with self.assertRaises(BeaverError) as e:plan(project,spec,root/'plan')
            self.assertEqual(e.exception.code,'PROJECT_WRITE_DENIED')
        with self.assertRaises(BeaverError):validate(dict(schemaVersion=3,operation='production',profile='relative',steps=[]))

    def test_missing_declared_runtime_package_blocks_before_installation(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp).resolve();project,spec,_=self.fixture(root)
            data=json.loads(spec.read_text());data['requiredPackages']={'com.unity.modules.unitywebrequest':'1.0.0'};write_json(spec,data)
            with self.assertRaises(BeaverError) as error:plan(project,spec,root/'plan')
            self.assertEqual(error.exception.code,'PRODUCTION_PACKAGE_REQUIRED')
            self.assertEqual(error.exception.status,'blocked')
            self.assertFalse((project/'Assets/Game/New.json').exists())
            self.assertFalse((root/'plan').exists())

    def test_conflicting_resource_bindings_reject_before_editor(self):
        binding=dict(assetPath='Assets/Game/Window.prefab',nodePath='',componentType='Example.View',property='font',objectPath='Assets/Game/A.asset',objectName='A')
        font=dict(kind='resource.font',assetPath='Assets/Game/A.asset',sourceFontPath='Assets/Game/Source.ttf',characterSet='texts',texts=['A'],samplingPointSize=48,atlasPadding=5,renderMode='SDFAA',atlasSize=1024,fallbackPaths=[],consumerBindings=[binding])
        other=dict(font,assetPath='Assets/Game/B.asset',consumerBindings=[dict(binding,objectPath='Assets/Game/B.asset',objectName='B')])
        with self.assertRaises(BeaverError) as error:validate(dict(schemaVersion=3,operation='production',profile='/unused/profile.json',steps=[font,other]))
        self.assertEqual(error.exception.code,'PRODUCTION_BINDING_CONFLICT')

    def test_deferred_settings_need_independent_strict_verdict(self):
        from beaver.batch import state
        for first_status,second_status in (('pending_editor_reopen','verified'),('pending_editor_reopen','failed'),('failed','checkpoint_verified')):
            with self.subTest(first_status=first_status,second_status=second_status),tempfile.TemporaryDirectory() as temp:
                root=Path(temp).resolve();project,spec,_=self.fixture(root)
                settings=project/'ProjectSettings/EditorSettings.asset';settings.write_text('before')
                policy=root/'policy.json';write_json(policy,dict(schemaVersion=1,engine='unity',name='Example',writableRoots=['Assets/Game','ProjectSettings/EditorSettings.asset']))
                profile=project/PROFILE;p=rebind_plan(project,profile,sha(profile.read_bytes()),root/'rebind',spec_path=policy);setup_apply(p['report'],p['planSha256'],root/'rebind-run')
                d=json.loads(spec.read_text());d['steps']=[dict(kind='resource.project-settings',assetPath='ProjectSettings/EditorSettings.asset',setting='spritePackerMode',expectedValue='Disabled',value='SpriteAtlasV2Build')];write_json(spec,d)
                p=plan(project,spec,root/'plan');modes=[]
                def native(editor,target,stage,job,timeout):
                    modes.append(job['mode'])
                    if job['mode']=='apply':settings.write_text('flushed on exit')
                    rows=[dict(path=k,exists=v is not None,sha256=v['sha256'] if v else '',bytes=v['bytes'] if v else 0) for k,v in state(project,job['writes']).items()]
                    return (1 if job['mode']=='apply' and first_status=='failed' else 0),dict(status=first_status if job['mode']=='apply' else second_status,planSha256=job['planSha256'],files=rows,error='' if second_status=='verified' else 'strict reopen failed')
                with patch('beaver.batch.editor_path',return_value=root/'Unity'),patch('beaver.batch.native_lock'),patch('beaver.batch.launch',side_effect=native):
                    if second_status!='verified':
                        with self.assertRaises(BeaverError):apply(p['report'],root/'run',root/'Unity',p['planSha256'])
                        self.assertEqual(json.loads((root/'run/journal.json').read_text())['status'],'recovery-required')
                    else:
                        result=apply(p['report'],root/'run',root/'Unity',p['planSha256']);self.assertEqual(result['nativeReadback'],'saved_and_reopened_independent_editor')
                self.assertEqual(modes,['apply','checkpoint' if first_status=='failed' else 'verify'])
