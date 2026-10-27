import base64
import os
from pathlib import Path
import unittest
from unittest.mock import patch

from beaver.errors import BeaverError
from beaver.native_environment import editor_environment


class NativeEnvironmentTests(unittest.TestCase):
    def test_non_ascii_job_roundtrips_without_entering_environment_as_raw_text(self):
        job = Path('/workspace/示例项目/证据/native-job.json')
        parent = {'PATH': '/usr/bin', 'HOME': '/workspace/example', 'PWD': '/workspace/示例项目',
                  'OLDPWD': '/旧目录', 'UNRELATED_SECRET': 'private',
                  'BEAVER_NATIVE_JOB': '/stale', 'BEAVER_BATCH_JOB_BASE64': 'stale',
                  'HTTPS_PROXY': 'http://localhost:8000', 'LC_ALL': 'C.UTF-8'}
        with patch.dict(os.environ, parent, clear=True):
            before = dict(os.environ)
            for control in ('BEAVER_BATCH_JOB', 'BEAVER_NATIVE_JOB', 'BEAVER_ADOPT_JOB', 'BEAVER_UI_JOB'):
                env = editor_environment(control, job)
                self.assertEqual(base64.b64decode(env[control + '_BASE64']).decode('utf-8'), str(job))
                self.assertEqual(env['HTTPS_PROXY'], parent['HTTPS_PROXY'])
                self.assertEqual(set(env), {'PATH', 'HOME', 'HTTPS_PROXY', 'LC_ALL', control + '_BASE64'})
                self.assertTrue(all(k.isascii() and v.isascii() for k, v in env.items()))
            self.assertEqual(dict(os.environ), before)

    def test_invalid_required_value_is_rejected_without_value_disclosure(self):
        with patch.dict(os.environ, {'PATH': 'private\udcff'}, clear=True):
            with self.assertRaises(BeaverError) as error:
                editor_environment('BEAVER_BATCH_JOB', '/job.json')
        self.assertEqual(error.exception.code, 'NATIVE_ENVIRONMENT_ENCODING')
        self.assertNotIn('private', str(error.exception))

    def test_all_companions_decode_the_same_explicit_base64_protocol(self):
        root = Path(__file__).resolve().parents[1] / 'src/beaver/targets'
        for name, control in [('BeaverBatch', 'BEAVER_BATCH_JOB'),
                              ('BeaverNative', 'BEAVER_NATIVE_JOB'),
                              ('BeaverAdoption', 'BEAVER_ADOPT_JOB')]:
            source = (root / (name + '.cs')).read_text()
            self.assertIn(control + '_BASE64', source)
            self.assertIn('Convert.FromBase64String(encoded)', source)
            self.assertIn('new System.Text.UTF8Encoding(false,true)', source)
            self.assertNotIn('GetEnvironmentVariable("' + control + '")', source)


if __name__ == '__main__':
    unittest.main()
