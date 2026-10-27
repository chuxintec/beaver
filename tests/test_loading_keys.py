import unittest

from beaver.errors import BeaverError
from beaver.loading_keys import patch_script
from beaver.references import _literal_loads


class LoadingKeyPatchTests(unittest.TestCase):
    def patch_for(self, source, call_index=0, before='old/key', after='new/key'):
        found, _ = _literal_loads('fixture.cs', source)
        row = found[call_index]
        return dict(start=row['span'][0], end=row['span'][1], before=before, after=after)

    def test_patch_exact_proven_literal_preserves_bom_crlf_and_other_bytes(self):
        source = '// 😀 before span\r\nvar a = Resources.Load<Texture2D>("old\\u002fkey", typeof(Texture2D));\r\n'
        raw = b'\xef\xbb\xbf' + source.encode('utf-8')
        patch = self.patch_for(source)
        result = patch_script(raw, [patch])
        self.assertTrue(result.startswith(b'\xef\xbb\xbf'))
        decoded = result[3:].decode('utf-8')
        self.assertIn('Resources.Load<Texture2D>("new/key", typeof(Texture2D));\r\n', decoded)
        self.assertTrue(decoded.startswith('// 😀 before span\r\n'))
        self.assertTrue(decoded.endswith('\r\n'))

    def test_json_safe_escape_subset_and_unicode_replacement(self):
        source = 'var a = Addressables.LoadAssetAsync<Sprite>("old\\u004bey");'
        patch = self.patch_for(source, before='oldKey', after='UI/图 "new"\n')
        result = patch_script(source.encode(), [patch]).decode()
        self.assertIn('"UI/\\u56fe \\\"new\\\"\\n"', result)

    def test_noop_preserves_bytes_but_invalid_utf8_rejects(self):
        source = b'\xef\xbb\xbf// untouched\r\n'
        self.assertEqual(patch_script(source, []), source)
        with self.assertRaisesRegex(BeaverError, 'LOADING_KEY_UTF8'):
            patch_script(b'\xff', [])

    def test_only_exact_proven_call_spans_are_accepted(self):
        source = ('// Resources.Load("old/key");\n'
                  'var comment = "Resources.Load(\\"old/key\\")";\n'
                  'var concat = Resources.Load("old/" + suffix);\n'
                  'var dynamic = Resources.Load(GetKey());\n'
                  'var verbatim = Resources.Load(@"old/key");\n'
                  'var interpolation = Resources.Load($"old/{suffix}");')
        with self.assertRaisesRegex(BeaverError, 'LOADING_KEY_NOT_LITERAL_LOAD'):
            patch_script(source.encode(), [dict(start=source.index('"old/"'), end=source.index('"old/"')+6,
                                                       before='old/', after='new/')])

    def test_before_mismatch_unsupported_csharp_escape_and_invalid_output_reject(self):
        source = 'Resources.Load("actual");'
        patch = self.patch_for(source, before='expected')
        with self.assertRaisesRegex(BeaverError, 'LOADING_KEY_BEFORE_MISMATCH'):
            patch_script(source.encode(), [patch])

        source = 'Resources.Load("key\\x41");'
        patch = self.patch_for(source, before='keyA')
        with self.assertRaisesRegex(BeaverError, 'LOADING_KEY_ENCODING'):
            patch_script(source.encode(), [patch])

        source = 'Resources.Load("key");'
        patch = self.patch_for(source, before='key', after='bad\ud800')
        with self.assertRaisesRegex(BeaverError, 'LOADING_KEY_PATCH'):
            patch_script(source.encode(), [patch])

    def test_repeated_overlap_and_reordered_spans_reject(self):
        source = 'Resources.Load("first"); Addressables.LoadAssetAsync<Sprite>("second");'
        first = self.patch_for(source, 0, 'first', 'one')
        second = self.patch_for(source, 1, 'second', 'two')
        with self.assertRaisesRegex(BeaverError, 'LOADING_KEY_DUPLICATE'):
            patch_script(source.encode(), [first, dict(first)])
        with self.assertRaisesRegex(BeaverError, 'LOADING_KEY_ORDER'):
            patch_script(source.encode(), [second, first])
        overlap = dict(first, start=first['start']+1, end=first['end']+1)
        with self.assertRaisesRegex(BeaverError, 'LOADING_KEY_OVERLAP'):
            patch_script(source.encode(), [first, overlap])


if __name__ == '__main__':
    unittest.main()
