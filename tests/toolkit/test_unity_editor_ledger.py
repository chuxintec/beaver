import copy
import unittest
from beaver.toolkit.unity_asset_ledger import compare_editor


class EditorLedgerTests(unittest.TestCase):
    def test_identity_path_type_geometry_dependency_changes(self):
        row=dict(path='Assets/a.png',guid='a'*32,local_id=21300000,type='UnityEngine.Sprite',name='a',sprite_geometry=[0,0,8,8,4,4,0,0,0,0,100],dependencies=['b'*32+':1'])
        before=dict(schema_version=1,status='editor_observed',asset_objects=[row],asset_inputs=[],issues=[])
        self.assertEqual(compare_editor(before,copy.deepcopy(before))['status'],'unchanged')
        for field,value in [('path','Assets/b.png'),('type','UnityEngine.Texture2D'),('sprite_geometry',[0,0,7,8]),('dependencies',[])]:
            after=copy.deepcopy(before);after['asset_objects'][0][field]=value
            self.assertIn(field,compare_editor(before,after)['changed'][0]['fields'])
        after=copy.deepcopy(before);after['asset_objects'][0]['local_id']=99
        result=compare_editor(before,after);self.assertEqual(len(result['removed']),1);self.assertEqual(len(result['added']),1)
        after=copy.deepcopy(before);after['asset_objects'].append(row)
        with self.assertRaises(ValueError):compare_editor(before,after)
