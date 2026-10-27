#!/usr/bin/env python3
"""Isolated standard-library tests. All writes are confined to TemporaryDirectory."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'src/beaver/toolkit'))
from unity_asset_ledger import snapshot, compare, check_plan, verify_moves

SCRIPT = (Path(__file__).resolve().parents[2] / 'src/beaver/toolkit/unity_asset_ledger.py')
class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name); self.assets = self.root/'Assets'; self.assets.mkdir()
    def file(self, path, guid, value=b'data', root=None):
        root = root or self.assets; p = root/path; p.parent.mkdir(parents=True, exist_ok=True); p.write_bytes(value)
        Path(str(p)+'.meta').write_text('fileFormatVersion: 2\nguid: '+guid+'\n')
        return p
    def folder(self, path, guid, root=None):
        p=(root or self.assets)/path;p.mkdir(parents=True,exist_ok=True);Path(str(p)+'.meta').write_text('guid: '+guid+'\n')
    def cli(self, *args):
        return subprocess.run([sys.executable,str(SCRIPT),*map(str,args)],cwd=self.root,capture_output=True,text=True)
    def test_two_layouts_and_guid_move(self):
        self.folder('UI','a'*32);self.file('UI/'+'f'*32+'.png','b'*32)
        before=snapshot(self.assets);self.assertEqual(len(before['naming_candidates']),1)
        other=self.root/'OtherAssets';other.mkdir();self.folder('Textures','a'*32,other);self.file('Textures/暂停按钮.png','b'*32,root=other)
        after=snapshot(other);self.assertFalse(after['naming_candidates']);report=compare(before,after)
        self.assertEqual(len(report['moved']),2);self.assertFalse(report['content_changed']);self.assertFalse(report['removed'])
    def test_content_and_importer_changes_are_separate(self):
        p=self.file('Model.asset','a'*32);before=snapshot(self.assets)
        p.write_bytes(b'new');Path(str(p)+'.meta').write_text('guid: '+'a'*32+'\nsettings: updated\n')
        result=compare(before,snapshot(self.assets));self.assertEqual(len(result['content_changed']),1);self.assertEqual(len(result['meta_changed']),1)
    def test_same_path_lost_guid(self):
        p=self.file('Model.asset','a'*32);before=snapshot(self.assets);Path(str(p)+'.meta').write_text('guid: '+'b'*32+'\n')
        result=compare(before,snapshot(self.assets));self.assertEqual(len(result['same_path_guid_changed']),1);self.assertEqual(len(result['removed']),1)
    def test_orphans_empty_folders_and_missing_meta(self):
        self.folder('Empty','a'*32);(self.assets/'unimported.cs').write_text('class X {}');(self.assets/'gone.png.meta').write_text('guid: '+'b'*32)
        data=snapshot(self.assets);self.assertIn('Empty',data['empty_folders']);self.assertEqual({i['kind'] for i in data['issues']},{'orphan_meta','missing_meta'})
    def test_duplicate_guid_is_not_resolved_by_guess(self):
        self.file('A.asset','a'*32);self.file('B.asset','a'*32);data=snapshot(self.assets)
        self.assertIn('duplicate_guid',[x['kind'] for x in data['issues']])
        with self.assertRaises(ValueError):compare(data,data)
    def test_case_collision_and_retirement_reason(self):
        self.file('A.png','a'*32);self.file('B.png','b'*32)
        data=snapshot(self.assets);plan={'schema':'unity-file-move-plan-v1','moves':[{'from':'A.png','to':'Icon.png','guid':'a'*32},{'from':'B.png','to':'icon.png','guid':'b'*32}]}
        self.assertIn('duplicate_target',[i['kind'] for i in check_plan(data,plan)['issues']])
        plan={'schema':'unity-file-move-plan-v1','moves':[],'retire':[{'path':'A.png','guid':'a'*32}]}
        self.assertIn('retirement_reason_missing',[i['kind'] for i in check_plan(data,plan)['issues']])
    def test_safe_plan_and_occupied_case_only_name(self):
        self.file('A.png','a'*32);data=snapshot(self.assets)
        plan={'schema':'unity-file-move-plan-v1','moves':[{'from':'A.png','to':'UI/PauseButton.png','guid':'a'*32}]}
        self.assertEqual(check_plan(data,plan)['status'],'static_preflight_clean')
        plan['moves'][0]['to']='a.png';self.assertEqual(check_plan(data,plan)['issues'][0]['kind'],'occupied_target_requires_staging')
    def test_portability_and_parent_file_collision(self):
        self.file('A.png','a'*32);self.file('B.png','b'*32);data=snapshot(self.assets)
        plan={'schema':'unity-file-move-plan-v1','moves':[{'from':'A.png','to':'Folder','guid':'a'*32},{'from':'B.png','to':'Folder/CON.png','guid':'b'*32}]}
        kinds={x['kind'] for x in check_plan(data,plan)['issues']};self.assertIn('planned_target_is_parent_file',kinds);self.assertIn('nonportable_target_name',kinds)
    def test_unicode_normalization_collision(self):
        self.file('é.png','a'*32);data=snapshot(self.assets)
        plan={'schema':'unity-file-move-plan-v1','moves':[{'from':'é.png','to':'e\u0301.png','guid':'a'*32}]}
        self.assertIn('occupied_target_requires_staging',[x['kind'] for x in check_plan(data,plan)['issues']])
    def test_unsafe_paths_rejected(self):
        self.file('A.png','a'*32);data=snapshot(self.assets)
        for target in ['../escape.png','/tmp/escape.png','UI\\escape.png','UI//x.png']:
            with self.subTest(target=target),self.assertRaises(ValueError):check_plan(data,{'schema':'unity-file-move-plan-v1','moves':[{'from':'A.png','to':target,'guid':'a'*32}]})
    def test_cli_snapshot_and_no_overwrite(self):
        self.file('A.png','a'*32);output=self.root/'before.json'
        self.assertEqual(self.cli('snapshot','--assets',self.assets,'--output',output).returncode,0)
        previous=output.read_bytes();self.assertEqual(self.cli('snapshot','--assets',self.assets,'--output',output).returncode,2);self.assertEqual(output.read_bytes(),previous)
        self.assertEqual(self.cli('snapshot','--assets',self.assets,'--output',self.assets/'report.json').returncode,2)
    def test_cli_compare_and_plan_outside_source(self):
        self.file('A.png','a'*32);before=self.root/'before.json';before.write_text(json.dumps(snapshot(self.assets)))
        self.assertEqual(self.cli('compare','--before',before,'--after',before,'--output',self.root/'comparison.json').returncode,0)
        self.assertEqual(self.cli('compare','--before',before,'--after',before,'--output',self.assets/'report.json').returncode,2)
        plan=self.root/'plan.json';plan.write_text(json.dumps({'schema':'unity-file-move-plan-v1','moves':[{'from':'A.png','to':'UI/Pause.png','guid':'a'*32}]}))
        self.assertEqual(self.cli('check-plan','--snapshot',before,'--plan',plan,'--output',self.root/'plan-report.json').returncode,0)
    def test_cli_reports_issues_and_differences(self):
        p=self.file('A.png','a'*32);before=self.root/'before.json';before.write_text(json.dumps(snapshot(self.assets)))
        p.write_bytes(b'changed');after=self.root/'after.json';after.write_text(json.dumps(snapshot(self.assets)))
        self.assertEqual(self.cli('compare','--before',before,'--after',after,'--output',self.root/'changed.json').returncode,1)
        (self.assets/'unknown.asset').write_bytes(b'no metadata')
        self.assertEqual(self.cli('snapshot','--assets',self.assets,'--output',self.root/'issues.json').returncode,1)
    def test_empty_guid_is_not_an_unchanged_identity(self):
        (self.assets/'unknown.asset').write_bytes(b'data');data=snapshot(self.assets)
        self.assertEqual(compare(data,data)['status'],'changes_or_issues')
    def test_symlink_roots_and_descendants_refused(self):
        link=self.root/'linked';link.symlink_to(self.assets,target_is_directory=True)
        self.assertEqual(self.cli('snapshot','--assets',link,'--output',self.root/'link.json').returncode,2)
        (self.assets/'linked').symlink_to(self.root/'outside')
        self.assertEqual(self.cli('snapshot','--assets',self.assets,'--output',self.root/'descendant.json').returncode,2)
    def test_invalid_json_and_help(self):
        bad=self.root/'bad.json';bad.write_text('[]')
        self.assertEqual(self.cli('compare','--before',bad,'--after',bad,'--output',self.root/'out.json').returncode,2)
        self.assertEqual(self.cli('--help').returncode,0)
    def migrated_pair(self, resource=False):
        old='Resources/Icon.asset' if resource else 'Old.asset'
        new='UI/Resources/Icon.asset' if resource else 'New.asset'
        self.file(old,'a'*32)
        if resource:self.folder('Resources','b'*32)
        before=snapshot(self.assets)
        other=self.root/'MovedAssets';other.mkdir();self.file(new,'a'*32,root=other)
        if resource:
            self.folder('UI','c'*32,other);self.folder('UI/Resources','b'*32,other)
        plan={'schema':'unity-file-move-plan-v1','moves':[{'from':old,'to':new,'guid':'a'*32}]}
        return before,snapshot(other),plan
    def test_verify_file_and_resources_layouts(self):
        for resource in (False,True):
            with self.subTest(resource=resource):
                # Separate trees avoid carrying the first fixture into the second.
                if resource:
                    self.assets=self.root/'SecondAssets';self.assets.mkdir()
                    (self.root/'MovedAssets').rename(self.root/'FirstMoved')
                b,a,p=self.migrated_pair(resource)
                self.assertEqual(verify_moves(b,a,p)['status'],'planned_files_match')
    def test_verify_rejects_changed_content_meta_and_key(self):
        b,a,p=self.migrated_pair(True);target=p['moves'][0]['to']
        a['assets'][target]['sha256']='f'*64;a['assets'][target]['meta_sha256']='e'*64
        failures=verify_moves(b,a,p)['issues'][0]['failures']
        self.assertIn('sha256_changed',failures);self.assertIn('meta_sha256_changed',failures)
        p['moves'][0]['to']='UI/Resources/Renamed.asset'
        a['assets'][p['moves'][0]['to']]=a['assets'].pop(target)
        self.assertIn('resources_key_changed',verify_moves(b,a,p)['issues'][0]['failures'])
    def test_verify_missing_target_and_empty_plan(self):
        b,a,p=self.migrated_pair();a['assets'].clear()
        self.assertEqual(verify_moves(b,a,p)['status'],'needs_review')
        with self.assertRaises(ValueError):verify_moves(b,a,{'schema':'unity-file-move-plan-v1','moves':[]})
    def test_verify_cli_and_output_guard(self):
        b,a,p=self.migrated_pair()
        for name,value in [('b',b),('a',a),('p',p)]: (self.root/(name+'.json')).write_text(json.dumps(value))
        args=('verify-moves','--before',self.root/'b.json','--after',self.root/'a.json','--plan',self.root/'p.json','--output',self.root/'result.json')
        self.assertEqual(self.cli(*args).returncode,0);self.assertEqual(self.cli(*args).returncode,2)
if __name__=='__main__':unittest.main()
