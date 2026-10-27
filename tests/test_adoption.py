# Adapted from monkey tests; MIT license retained.
import tempfile
import unittest
import shutil
from pathlib import Path
from unittest.mock import patch
from beaver.io import write_json,read,load,sha
from helpers import finish
from beaver.adoption import preview,build_plan,prepare,commit_state,STATE
from beaver.errors import BeaverError as ParseError

GUID='11111111111111111111111111111111'
GUID2='22222222222222222222222222222222'
PREFIX='Assets/Game/Content/Data/Test/'


def candidate(root,path='A.txt',content=b'old',guid=GUID,namespace='game'):
    root.mkdir();asset=root/(PREFIX+path);asset.parent.mkdir(parents=True);asset.write_bytes(content)
    Path(str(asset)+'.meta').write_text('fileFormatVersion: 2\nguid: '+guid+'\n')
    (root/'ProjectSettings').mkdir();(root/'ProjectSettings/ProjectVersion.txt').write_text('m_EditorVersion: 6000.6.2f1\n')
    (root/'Packages').mkdir();write_json(root/'Packages/manifest.json',{'dependencies':{}})
    finish(root,dict(schemaVersion=1,targetProfile='unity-6000.6.2f1-ugui',namespace=namespace,projectLayout='monkey-project/2.0.0',scopeMode='whole-input',diagnostics=[]))
    return root


class AdoptionTests(unittest.TestCase):
    def test_preview_move_preserves_unknown_files_and_retains_removed_assets(self):
        with tempfile.TemporaryDirectory() as temp:
            w=Path(temp).resolve();b=candidate(w/'base');a=candidate(w/'after','B.txt');t=w/'target';shutil.copytree(b,t)
            human=t/'Assets/Game/Scripts/Human.cs';human.parent.mkdir(parents=True);human.write_bytes(b'human')
            result=preview(a,t,w/'plan',b);self.assertEqual(result['status'],'planned')
            p=load(read(w/'plan/adoption-plan.json'));self.assertEqual(p['operations'][0]['action'],'move');self.assertEqual(p['operations'][0]['guid'],GUID)
            self.assertEqual((t/(PREFIX+'A.txt')).read_bytes(),b'old');self.assertEqual(human.read_bytes(),b'human')
            changed=candidate(w/'different','Other.txt',guid=GUID2)
            p=build_plan(changed,t,b);self.assertEqual(len(p['retainedRemoved']),1)
            self.assertEqual((t/(PREFIX+'A.txt')).read_bytes(),b'old')

    def test_three_way_unchanged_incoming_keeps_local_edits_but_overlapping_changes_block(self):
        with tempfile.TemporaryDirectory() as temp:
            w=Path(temp).resolve();b=candidate(w/'base');a=candidate(w/'after',content=b'new');t=w/'target';shutil.copytree(b,t)
            (t/(PREFIX+'A.txt')).write_bytes(b'human')
            p=build_plan(b,t,b);self.assertFalse(p['operations']);self.assertFalse(p['conflicts'])
            p=build_plan(a,t,b);self.assertEqual(p['conflicts'][0]['reason'],'locally-modified')
            preview(a,t,w/'blocked',b)
            with self.assertRaises(ParseError):prepare(w/'blocked/adoption-plan.json',w/'execute')
            self.assertFalse((w/'execute').exists())

    def test_stale_plan_tamper_foreign_namespace_and_destination_collision_reject(self):
        with tempfile.TemporaryDirectory() as temp:
            w=Path(temp).resolve();b=candidate(w/'base');a=candidate(w/'after','B.txt');t=w/'target';shutil.copytree(b,t)
            result=preview(a,t,w/'plan',b);(t/(PREFIX+'B.txt')).write_bytes(b'unknown')
            with self.assertRaises(ParseError):prepare(w/'plan/adoption-plan.json',w/'execute',result['planSha256'])
            self.assertEqual(build_plan(a,t,b)['conflicts'][0]['reason'],'occupied-destination')
            wrong=candidate(w/'wrong',namespace='another')
            with self.assertRaises(ParseError):build_plan(wrong,t,b)
            with self.assertRaises(ParseError):prepare(w/'plan/adoption-plan.json',w/'tamper','0'*64)

    def test_payload_staging_is_independent_and_commit_catches_unrelated_manual_drift(self):
        with tempfile.TemporaryDirectory() as temp:
            w=Path(temp).resolve();b=candidate(w/'base');a=candidate(w/'after',content=b'new');t=w/'target';shutil.copytree(b,t)
            human=t/'Assets/human.txt';human.write_bytes(b'work');preview(a,t,w/'plan',b)
            stage,plan,journal=prepare(w/'plan/adoption-plan.json',w/'transaction')
            self.assertEqual((stage/'before/0/file').read_bytes(),b'old');self.assertNotEqual((stage/'after/0/file').stat().st_ino,(a/(PREFIX+'A.txt')).stat().st_ino)
            (t/(PREFIX+'A.txt')).write_bytes(b'new');human.write_bytes(b'later')
            with self.assertRaises(ParseError):commit_state(stage,plan,journal,{'status':'verified'})
            self.assertFalse((t/STATE).exists());self.assertEqual((stage/'before/0/file').read_bytes(),b'old')

    def test_saved_generation_supports_next_preview_and_rejects_a_stale_common_baseline(self):
        with tempfile.TemporaryDirectory() as temp:
            w=Path(temp).resolve();b=candidate(w/'base');a=candidate(w/'after',content=b'new');t=w/'target';shutil.copytree(b,t)
            preview(a,t,w/'plan',b);stage,plan,journal=prepare(w/'plan/adoption-plan.json',w/'transaction')
            (t/(PREFIX+'A.txt')).write_bytes(b'new');commit_state(stage,plan,journal,{'status':'verified'})
            p=build_plan(a,t);self.assertFalse(p['operations']);self.assertFalse(p['conflicts'])
            with self.assertRaises(ParseError):build_plan(a,t,b)

    def test_locally_moved_assets_and_duplicate_guid_are_not_silently_claimed(self):
        with tempfile.TemporaryDirectory() as temp:
            w=Path(temp).resolve();b=candidate(w/'base');a=candidate(w/'after','B.txt');t=w/'target';shutil.copytree(b,t)
            (t/(PREFIX+'A.txt')).rename(t/(PREFIX+'Human.txt'));(t/(PREFIX+'A.txt.meta')).rename(t/(PREFIX+'Human.txt.meta'))
            self.assertEqual(build_plan(a,t,b)['conflicts'][0]['reason'],'locally-moved')
            shutil.copyfile(t/(PREFIX+'Human.txt'),t/(PREFIX+'Dup.txt'));shutil.copyfile(t/(PREFIX+'Human.txt.meta'),t/(PREFIX+'Dup.txt.meta'))
            with self.assertRaises(ParseError):build_plan(a,t,b)

    def test_algorithm_change_and_manual_ownership_are_explicit_blocks(self):
        with tempfile.TemporaryDirectory() as temp:
            w=Path(temp).resolve();b=candidate(w/'base');a=candidate(w/'after',content=b'new');t=w/'target';shutil.copytree(b,t)
            preview(a,t,w/'plan',b)
            with patch('beaver.adoption.algorithm_id',return_value='f'*64):
                with self.assertRaises(ParseError):prepare(w/'plan/adoption-plan.json',w/'transaction')
            manifest=load(read(a/'manifest.json'))
            for row in manifest['files']:
                if row['path']==PREFIX+'A.txt':row['owner']='manual'
            write_json(a/'manifest.json',manifest)
            self.assertEqual(build_plan(a,t,b)['conflicts'][0]['reason'],'non-generated-owner')

    def test_busy_editor_markers_are_preserved_and_real_processes_block(self):
        from beaver.adoption import native_lock
        import subprocess
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp).resolve();(root/'Temp').mkdir();marker=root/'Temp/UnityLockfile';marker.write_text('marker')
            with patch('subprocess.run',return_value=subprocess.CompletedProcess([],0,stdout='')):native_lock(root)
            self.assertTrue(marker.exists())
            process='/Applications/Unity/Hub/Editor/6000.6.2f1/Unity.app/Contents/MacOS/Unity -projectPath '+str(root)
            with patch('subprocess.run',return_value=subprocess.CompletedProcess([],0,stdout=process)):
                with self.assertRaises(ParseError):native_lock(root)
            self.assertEqual(marker.read_text(),'marker')

    def test_dependencies_and_settings_drift_block_before_an_editor_launch(self):
        with tempfile.TemporaryDirectory() as temp:
            w=Path(temp).resolve();b=candidate(w/'base');a=candidate(w/'after');t=w/'target';shutil.copytree(b,t)
            write_json(t/'Packages/manifest.json',{'dependencies':{'own.package':'1.0.0'}})
            preview(a,t,w/'plan',b)
            write_json(t/'Packages/manifest.json',{'dependencies':{'own.package':'2.0.0'}})
            with self.assertRaises(ParseError):prepare(w/'plan/adoption-plan.json',w/'execute')
            write_json(a/'Packages/manifest.json',{'dependencies':{'required.package':'1.0.0'}})
            finish(a,dict(schemaVersion=1,targetProfile='unity-6000.6.2f1-ugui',namespace='game',projectLayout='monkey-project/2.0.0',scopeMode='whole-input',diagnostics=[]))
            self.assertTrue(any(c['reason']=='package-requirement' for c in build_plan(a,t,b)['conflicts']))

    def test_explicit_unity_source_lineage_survives_new_package_bytes(self):
        with tempfile.TemporaryDirectory() as temp:
            w=Path(temp).resolve();b=candidate(w/'base');a=candidate(w/'after');t=w/'target';shutil.copytree(b,t)
            for root,digest in [(b,'a'*64),(a,'b'*64)]:
                m=load(read(root/'manifest.json'));m.pop('namespace');m['source']={'sha256':digest};m['targetVersion']='6000.6.2f1';m['sourceProfile']='unityfs-unitypy-1.25.3-assetripper-2.0.0';write_json(root/'recovery-manifest.json',m);(root/'manifest.json').unlink()
            with self.assertRaises(ParseError):build_plan(a,t,b)
            p=build_plan(a,t,b,lineage='stable-game');self.assertEqual(p['namespace'],'stable-game');self.assertFalse(p['conflicts'])
