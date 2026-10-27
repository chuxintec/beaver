from pathlib import Path
import shutil
import tempfile
import unittest
from beaver.io import write_json
from beaver.output_limits import digest_file
from beaver.adoption import preview
from beaver.adoption_native import execute
from beaver.errors import BeaverError


def creator(root,value):
    (root/'assets/resources/Data').mkdir(parents=True)
    p=root/'assets/resources/Data/Sample.txt';p.write_text(value)
    write_json(Path(str(p)+'.meta'),dict(uuid='11111111-1111-4111-8111-111111111111'))
    write_json(root/'package.json',dict(creator=dict(version='3.8.8')))
    rows=[dict(path=p.relative_to(root).as_posix(),sha256=digest_file(p),bytes=p.stat().st_size,owner='generated') for p in root.rglob('*') if p.is_file()]
    write_json(root/'manifest.json',dict(schemaVersion=1,targetProfile='cocos-3.8.8',namespace='self-authored-creator',scopeMode='whole-input',files=rows))


class CreatorBoundaryTests(unittest.TestCase):
    def test_creator_has_static_threeway_preview_but_rejects_native_before_provider_lookup(self):
        with tempfile.TemporaryDirectory() as temp:
            r=Path(temp).resolve();creator(r/'old','old');creator(r/'next','next');shutil.copytree(r/'old',r/'target')
            result=preview(r/'next',r/'target',r/'plan',r/'old')
            self.assertEqual(result['counts']['operations'],1)
            with self.assertRaises(BeaverError) as error:
                execute(r/'plan/adoption-plan.json',r/'transaction',r/'missing-editor',expected=result['planSha256'])
            self.assertEqual(error.exception.code,'ADOPT_NATIVE_UNSUPPORTED')
            self.assertFalse((r/'transaction').exists())
