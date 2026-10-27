from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import contextlib
import io
import json
from beaver.batch import plan,apply,recover,state,text_changes
from beaver.cli import main
from beaver.io import write_json,encoded,sha
from beaver.errors import BeaverError
from test_adoption import candidate


class BatchTests(unittest.TestCase):
    def test_editor_exit_record_preserves_launch_identity_and_start_time(self):
        from beaver.batch import launch
        from unittest.mock import Mock
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp).resolve();stage=root/'run';stage.mkdir();seen=[]
            proc=Mock(pid=123,returncode=0)
            def wait(**kwargs):
                seen.append(json.loads((stage/'process.json').read_text()));return 0
            proc.wait.side_effect=wait
            write_json(stage/'native-result.json',dict(status='verified'))
            with patch('beaver.batch.subprocess.Popen',return_value=proc):
                code,report=launch(root/'Unity',root,stage,{},10)
            final=json.loads((stage/'process.json').read_text())
            self.assertEqual(code,0);self.assertEqual(report['status'],'verified')
            self.assertFalse(seen[0]['exited']);self.assertTrue(final['exited'])
            for field in ('startedUtc','pid','executable','launchArguments'):
                self.assertEqual(final[field],seen[0][field])
            self.assertEqual(final['launchArguments'][0],str(root/'Unity'))
            self.assertEqual(final['exitCode'],0)

    def fixture(self,root):
        project=candidate(root/'project')
        p=project/'Assets/Window.prefab';p.write_text('%YAML 1.1\n--- !u!1 &1\nGameObject: {}\n')
        (project/'Assets/Window.prefab.meta').write_text('guid: '+'a'*32+'\n')
        (project/'Assets/Unrelated.png').write_bytes(b'unrelated-large-resource')
        spec=root/'spec.json';write_json(spec,dict(schemaVersion=2,operation='batch',steps=[dict(kind='prefab.patch',assetPath='Assets/Window.prefab',actions=[dict(kind='property.set',path='',component='Example',property='text',mode='string',value='new')])]))
        return project,spec

    def test_incremental_scope_hashes_write_dependencies_not_unrelated_binary(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp).resolve();project,spec=self.fixture(root);result=plan(project,spec,root/'plan')
            data=json.loads(Path(result['report']).read_text())
            self.assertNotIn('Assets/Unrelated.png',data['before'])
            self.assertEqual(data['before']['Assets/Window.prefab']['sha256'],sha((project/'Assets/Window.prefab').read_bytes()))
            self.assertEqual(result['metrics']['editorStarts'],0)
            (project/'Assets/Window.prefab').write_text('later manual')
            with patch('beaver.batch.editor_path',return_value=root/'Unity'),patch('beaver.batch.native_lock'):
                with self.assertRaises(BeaverError) as e:apply(result['report'],root/'run',root/'Unity',result['planSha256'])
            self.assertEqual(e.exception.code,'BATCH_SOURCE_DRIFT')

    def test_recovery_rejects_later_edit_before_any_editor_launch(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp).resolve();project,spec=self.fixture(root);result=plan(project,spec,root/'plan')
            data=json.loads(Path(result['report']).read_text());tx=root/'tx';tx.mkdir();write_json(tx/'batch-plan.json',data)
            before={p:data['before'][p] for p in data['writeSet']}
            for p,v in before.items():
                if v:
                    dest=tx/'before'/p;dest.parent.mkdir(parents=True,exist_ok=True);dest.write_bytes((project/p).read_bytes())
            witnessed=state(project,data['writeSet']);rows=[dict(path=p,exists=v is not None,sha256=v['sha256'] if v else '',bytes=v['bytes'] if v else 0) for p,v in witnessed.items()]
            from beaver.unity import identity
            write_json(tx/'journal.json',dict(contract='beaver-batch-transaction/2',status='complete',projectRoot=str(project),planSha256=result['planSha256'],implementationSha256=identity(),writeSet=data['writeSet'],before=before,after=witnessed,unplannedPaths=[],native=dict(planSha256=result['planSha256'],files=rows)))
            (project/'Assets/Window.prefab').write_text('later user work')
            with patch('beaver.batch.editor_path',return_value=root/'Unity'),patch('beaver.batch.native_lock'),patch('beaver.batch.launch') as launch:
                with self.assertRaises(BeaverError) as e:recover(tx,root/'recovery',root/'Unity')
            self.assertEqual(e.exception.code,'BATCH_LATER_EDIT');launch.assert_not_called()

    def test_json_exact_pointer_and_status_exit_contract(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp).resolve();(root/'Assets').mkdir();write_json(root/'Assets/Keys.json',{'key':'old','description':'old'})
            step=dict(kind='text.key',assetPath='Assets/Keys.json',format='json-pointer',pointer='/key',before='old',after='new')
            data=json.loads(text_changes(root,dict(steps=[step]))['Assets/Keys.json'])
            self.assertEqual(data,dict(key='new',description='old'))
        stream=io.StringIO()
        with contextlib.redirect_stdout(stream):code=main(['nonsense'])
        self.assertEqual(code,3);self.assertEqual(json.loads(stream.getvalue())['status'],'blocked')
        with patch('beaver.cli.dispatch',side_effect=BeaverError('NATIVE_FAILED','fixture','failed')):
            stream=io.StringIO()
            with contextlib.redirect_stdout(stream):code=main(['version'])
        self.assertEqual(code,2);self.assertEqual(json.loads(stream.getvalue())['status'],'failed')

    def test_unity_artifact_budget_is_explicit_and_keeps_regular_payload_limit(self):
        from beaver.payload import verify_manifest
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp).resolve();(root/'player').mkdir();raw=b'123456';(root/'player/data').write_bytes(raw)
            manifest=root/'artifact-manifest.json';write_json(manifest,dict(schemaVersion=1,artifactPolicy='beaver-unity-build/2',files=[dict(path='player/data',bytes=6,sha256=sha(raw))]))
            with patch('beaver.payload.FILE_BYTES',4),patch('beaver.payload.BUILD_FILE_BYTES',8):
                with self.assertRaises(BeaverError):verify_manifest(root,manifest,'player')
                self.assertEqual(verify_manifest(root,manifest,'player',artifact=True)['bytes'],6)
                (root/'player/data').write_bytes(b'12345x')
                with self.assertRaises(BeaverError) as error:verify_manifest(root,manifest,'player',artifact=True)
                self.assertEqual(error.exception.code,'PAYLOAD_DRIFT')
