import json
from pathlib import Path
import tempfile
import unittest
from beaver.errors import BeaverError
from beaver.io import write_json
from beaver.configuration import validate, generate
from beaver.images import inspect, compress


class DevelopmentTests(unittest.TestCase):
    def test_png_compression_preserves_pixels_and_source_and_rejects_stale_source(self):
        from PIL import Image
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            source = root/'source.png'
            image = Image.new('RGBA', (128, 128), (30, 90, 180, 0))
            for x in range(16, 112):
                for y in range(16, 112):
                    image.putpixel((x, y), (30, 90, 180, 128))
            image.save(source, compress_level=0)
            original = source.read_bytes()
            info = inspect(source)
            self.assertTrue(info['partialAlphaPixels'])
            plan = compress(source, root/'out.png')
            self.assertEqual(plan['status'], 'planned')
            self.assertFalse((root/'out.png').exists())
            with self.assertRaises(BeaverError):
                compress(source, root/'bad.png', True, '0'*64)
            result = compress(source, root/'out.png', True, info['sha256'])
            self.assertGreater(result['savedBytes'], 0)
            self.assertEqual(source.read_bytes(), original)
            self.assertEqual(inspect(root/'out.png')['decodedRGBASha256'], info['decodedRGBASha256'])

    def test_strict_json_rejects_missing_fields_duplicates_and_coercion(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            schema = root/'schema.json'
            data = root/'data.json'
            write_json(schema, dict(schemaVersion=1, name='Item', key='id', fields=[dict(name='id', type='string'), dict(name='price', type='int', min=1)]))
            write_json(data, dict(rows=[dict(id='test', price=3)]))
            self.assertEqual(validate(schema, data)['rows'], 1)
            for rows in ([dict(id='test')], [dict(id='test',price='3')], [dict(id='test',price=0)], [dict(id='test',price=3)]*2):
                write_json(data, dict(rows=rows))
                with self.assertRaises(BeaverError):
                    validate(schema, data)
            data.write_text('{"rows":[],"rows":[]}')
            with self.assertRaises(BeaverError):
                validate(schema, data)
            result = generate(schema, root/'generated')
            source = Path(result['report']).read_text()
            self.assertIn('DuplicatePropertyNameHandling.Error', source)
            self.assertIn('UnityEngine.TextAsset', source)
            self.assertNotIn('= 3;', source)

    def test_dto_template_preserves_identifier_tokens_in_declared_fields(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp).resolve();path=root/'schema.json'
            write_json(path,dict(schemaVersion=1,name='interface',key='KEYNAME',fields=[dict(name='KEYNAME',type='string'),dict(name='FIELDS',type='int')]))
            result=generate(path,root/'out');source=Path(result['report']).read_text()
            self.assertIn('class @interface',source)
            self.assertIn('public string @KEYNAME;',source)
            self.assertIn('t["KEYNAME"]',source)
            self.assertIn('public int @FIELDS;',source)
