import json,hashlib,tempfile,unittest
from pathlib import Path
from PIL import Image
from beaver.toolkit.image_alpha_audit import inspect_image
from beaver.toolkit.image_contact_sheet import create
class AssetPreview(unittest.TestCase):
 def test_palette_limit_plus_one_is_rejected_and_size_explicit(self):
  with tempfile.TemporaryDirectory() as td:
   p=Path(td)/'four.png';im=Image.new('RGBA',(4,1));im.putdata([(i*40,0,0,255) for i in range(4)]);im.save(p)
   r=inspect_image(p,max_colors=3,expect_size=[4,1]);self.assertEqual(r['color_count'],4);self.assertFalse(r['colors_within_limit']);self.assertTrue(r['size_matches'])
   self.assertTrue(inspect_image(p,max_colors=4)['colors_within_limit']);self.assertFalse(inspect_image(p,expect_size=[3,1])['size_matches']);self.assertIsNone(inspect_image(p)['colors_within_limit'])
 def test_sheet_preserves_inputs_ratio_identity_and_refuses_duplicates(self):
  with tempfile.TemporaryDirectory() as td:
   root=Path(td).resolve();image=root/'wide.png';Image.new('RGBA',(80,40),(1,2,3,120)).save(image);before=image.read_bytes();m=root/'input.json';spec=dict(schema_version=1,images=[dict(id='wide',path='wide.png')]);m.write_text(json.dumps(spec))
   r=create(m,root/'out',cell_size=[40,40]);self.assertEqual(r['images'][0]['display_size'],[40,20]);self.assertEqual(image.read_bytes(),before);self.assertEqual(r['images'][0]['source']['sha256'],hashlib.sha256(before).hexdigest())
   with self.assertRaises(ValueError):create(m,root/'out')
   spec['images'].append(dict(id='WIDE',path='wide.png'));m.write_text(json.dumps(spec))
   with self.assertRaises(ValueError):create(m,root/'dup')
   spec['images']=spec['images'][:1];spec['images'][0]['id']='中文';m.write_text(json.dumps(spec))
   with self.assertRaises(ValueError):create(m,root/'no-font')
   spec['images'][0]['id']='owned';spec['images'][0]['path']='../outside.png';m.write_text(json.dumps(spec))
   with self.assertRaises(ValueError):create(m,root/'escape')
 def test_sheet_pixel_budget_and_native_size(self):
  with tempfile.TemporaryDirectory() as td:
   root=Path(td).resolve();Image.new('RGBA',(50,80),'red').save(root/'im.png');m=root/'m.json';m.write_text(json.dumps(dict(schema_version=1,images=[dict(id='own',path='im.png')])))
   r=create(m,root/'native',native_size=True);self.assertEqual(r['images'][0]['display_size'],[50,80]);self.assertEqual(r['images'][0]['scale'],1)
   with self.assertRaises(ValueError):create(m,root/'large',cell_size=[8192,8192])
