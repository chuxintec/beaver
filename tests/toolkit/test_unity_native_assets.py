import copy
import json
from pathlib import Path
import tempfile
import unittest
from PIL import Image
from beaver.toolkit.unity_native_assets import prepare, spine_closure


class NativeAssetCandidateTests(unittest.TestCase):
    def test_trim_rotation_border_and_conflict(self):
        with tempfile.TemporaryDirectory() as t:
            root=Path(t).resolve();image=Image.new('RGBA',(2,3));image.putdata([(255,0,0,255),(0,255,0,255),(0,0,255,255),(255,255,0,255),(255,0,255,255),(0,255,255,255)])
            image.save(root/'atlas.png'); recipe=dict(schema_version=1,sprites=[dict(name='Trim',image='atlas.png',rect=[0,0,2,3],rotated_cw=True,original_size=[5,4],offset=[1,1],pivot=[0.25,0.75],border=[1,1,1,1],pixels_per_unit=100)],targets=[],clips=[])
            path=root/'recipe.json';path.write_text(json.dumps(recipe));out=root/'candidate';prepare(path,out)
            actual=Image.open(out/'Trim.png');self.assertEqual(actual.size,(5,4));self.assertEqual(actual.getpixel((1,1)),image.rotate(90,expand=True).getpixel((0,0)))
            self.assertEqual(actual.getpixel((0,0)),(0,0,0,0));self.assertEqual(prepare(path,out)['status'],'unchanged')
            (out/'Trim.png').write_bytes(b'changed')
            with self.assertRaisesRegex(ValueError,'conflict'):prepare(path,out)

    def test_explicit_targets_and_curve_boundaries_fail_before_output(self):
        good=dict(schema_version=1,sprites=[],targets=[dict(path='Widget',scale=[2,3,1])],clips=[dict(name='Move',duration=1,loop=True,curves=[dict(target='Widget',property='m_LocalPosition.x',keys=[[0,0],[1,4]])],events=[dict(time=0.5,function='Pulse')])])
        variants=[]
        for target in ['Missing','Widget/../X']:
            value=copy.deepcopy(good);value['clips'][0]['curves'][0]['target']=target;variants.append(value)
        value=copy.deepcopy(good);value['targets']*=2;variants.append(value)
        value=copy.deepcopy(good);value['clips'][0]['curves'][0]['keys']=[[0,0],[1.1,4]];variants.append(value)
        value=copy.deepcopy(good);value['clips'][0]['events'][0]['function']='Unknown';variants.append(value)
        for value in variants:
            with tempfile.TemporaryDirectory() as t:
                root=Path(t).resolve();path=root/'recipe.json';path.write_text(json.dumps(value))
                with self.assertRaises(ValueError):prepare(path,root/'out')
                self.assertFalse((root/'out').exists())

    def test_spine_two_page_and_missing_page(self):
        with tempfile.TemporaryDirectory() as t:
            root=Path(t).resolve(); (root/'skeleton.json').write_text(json.dumps({'skeleton':{'spine':'3.8.99'},'bones':[{'name':'root'}]}))
            (root/'skeleton.atlas').write_text('page-a.png\nsize: 2,2\nformat: RGBA8888\nfilter: Linear,Linear\nrepeat: none\nregion-a\n  rotate: false\n\npage-b.png\nsize: 2,2\nformat: RGBA8888\nfilter: Linear,Linear\nrepeat: none\nregion-b\n  rotate: false\n')
            for page in ['page-a.png','page-b.png']:Image.new('RGBA',(2,2)).save(root/page)
            r=spine_closure(root/'skeleton.json',root/'skeleton.atlas','3.8');self.assertEqual(len(r['pages']),2);self.assertFalse(r['missing_pages']);self.assertEqual(r['runtime_import'],'not_run')
            (root/'page-b.png').unlink();self.assertEqual(spine_closure(root/'skeleton.json',root/'skeleton.atlas','3.8')['missing_pages'],['page-b.png'])
            with self.assertRaises(ValueError):spine_closure(root/'skeleton.json',root/'skeleton.atlas','4.2')
