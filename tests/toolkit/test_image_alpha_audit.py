import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from PIL import Image
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'src/beaver/toolkit'))
from image_alpha_audit import inspect_image


class AlphaAuditTests(unittest.TestCase):
    def test_cutout_and_opaque_background(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            rgba = Image.new('RGBA', (9, 7), (0, 0, 0, 0))
            rgba.paste((255, 30, 0, 255), (2, 1, 7, 6)); rgba.save(root/'button.png')
            Image.new('RGB', (6, 4), 'pink').save(root/'background.png')
            a = inspect_image(root/'button.png'); b = inspect_image(root/'background.png')
            self.assertEqual(a['visible_bbox'], (2, 1, 7, 6)); self.assertEqual(a['opaque_border_pixels'], 0)
            self.assertEqual(b['transparent_pixels'], 0); self.assertEqual(b['opaque_border_pixels'], 16)

    def test_cli_transparency_failure_and_exclusive_output(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp); path=root/'opaque.png'; Image.new('RGB',(3,3),'white').save(path)
            script=(Path(__file__).resolve().parents[2] / 'src/beaver/toolkit/image_alpha_audit.py')
            args=[sys.executable,str(script),'--images',str(path),'--require-transparency','--output',str(root/'report.json')]
            self.assertEqual(subprocess.run(args,cwd=root,capture_output=True).returncode,1)
            original=(root/'report.json').read_bytes()
            self.assertEqual(subprocess.run(args,cwd=root,capture_output=True).returncode,2)
            self.assertEqual(original,(root/'report.json').read_bytes())

    def test_palette_transparency_and_degenerate_dimensions(self):
        with tempfile.TemporaryDirectory() as temp:
            p=Path(temp)/'pal.png'; im=Image.new('P',(1,3)); im.putpixel((0,1),1); im.save(p,transparency=0)
            a=inspect_image(p); self.assertEqual(a['transparent_pixels'],2); self.assertEqual(a['border_pixels'],3)

    def test_missing_symlink_and_pixel_limit(self):
        with tempfile.TemporaryDirectory() as temp:
            p=Path(temp)/'a.png'; Image.new('RGBA',(3,3)).save(p)
            with self.assertRaises(ValueError):inspect_image(p,4)
            with self.assertRaises(ValueError):inspect_image(Path(temp)/'missing.png')
            link=Path(temp)/'link.png';link.symlink_to(p)
            with self.assertRaises(ValueError):inspect_image(link)

    def test_empty_image_requires_review(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);p=root/'blank.png';Image.new('RGBA',(2,2)).save(p)
            args=[sys.executable,str((Path(__file__).resolve().parents[2] / 'src/beaver/toolkit/image_alpha_audit.py')),'--images',str(p),'--output',str(root/'report.json')]
            self.assertEqual(subprocess.run(args,cwd=root,capture_output=True).returncode,1)
            self.assertEqual(json.loads((root/'report.json').read_text())['issues'][0]['kind'],'fully_transparent_image')

    def test_corrupt_input_is_reported_without_writing_output(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);p=root/'bad.png';p.write_bytes(b'not an image')
            args=[sys.executable,str((Path(__file__).resolve().parents[2] / 'src/beaver/toolkit/image_alpha_audit.py')),'--images',str(p),'--output',str(root/'report.json')]
            self.assertEqual(subprocess.run(args,cwd=root,capture_output=True).returncode,2)
            self.assertFalse((root/'report.json').exists())


if __name__=='__main__':unittest.main()
