import json
from pathlib import Path
import sys
import tempfile
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from beaver.toolkit.unity_native_exit import inspect

class NativeExitTests(unittest.TestCase):
    def project(self,root,folder):
        project=root/folder;assets=project/'Assets';assets.mkdir(parents=True)
        (assets/'Entry.unity').write_text('%YAML 1.1\n')
        return project

    def test_explicit_mechanism_is_not_a_waiver_and_renaming_never_passes(self):
        with tempfile.TemporaryDirectory() as t:
            root=Path(t).resolve();p=self.project(root,'A');source=p/'Assets/Legacy.cs'
            source.write_text('class Legacy { public object Replay() { return null; } }')
            policy={'schema_version':1,'exits':[dict(id='source-graph',relative_path='Assets/Legacy.cs',symbol='Legacy',mechanism='full_source_graph',reason='Interprets the source node graph at runtime')]}
            result=inspect(p,['Entry.unity'],policy);self.assertEqual(result['status'],'fail')
            source.rename(p/'Assets/Renamed.cs');(p/'Assets/Renamed.cs').write_text('class Renamed { public object Replay() { return null; } }')
            result=inspect(p,['Entry.unity'],policy);self.assertEqual(result['status'],'not_run')
            self.assertEqual(result['exit_registry'][0]['status'],'requires_editor_confirmation')
            policy['exits'][0]['allow']=True
            with self.assertRaises(ValueError):inspect(p,['Entry.unity'],policy)

    def test_different_layout_legal_dto_editor_only_and_computed_loading_review(self):
        with tempfile.TemporaryDirectory() as t:
            root=Path(t).resolve();p=self.project(root,'Other');code=p/'Assets/Features';code.mkdir()
            (code/'Save.cs').write_text('class Save { public string json; public int key; string s="Spine cc.Node Resources.Load"; /* Assembly.Load() */ }')
            (code/'Load.cs').write_text('class Loader { void Run() { Resources.Load(prefix + state); } }')
            editor=code/'Editor';editor.mkdir();(editor/'Bake.cs').write_text('class Baker { void Run(){ Resources.Load(dynamicKey); } }')
            (editor/'Tools.asmdef').write_text(json.dumps({'name':'Own.Baker','includePlatforms':['Editor']}))
            resources=p/'Assets/Resources';resources.mkdir();(resources/'data.txt').write_text('own data')
            result=inspect(p,['Entry.unity'],{'schema_version':1,'exits':[]})
            self.assertEqual(result['status'],'not_run')
            self.assertEqual([q['path'] for q in result['dynamic_review_queue']],['Assets/Features/Load.cs'])
            self.assertIn('Resources/data.txt',result['roots'])
            self.assertEqual(result['evidence_layers']['editor'],'not_run')
            self.assertFalse(result['native_exit_passed'])

if __name__=='__main__':unittest.main()
