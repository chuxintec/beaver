import json
import os
from pathlib import Path
import tempfile
import unittest

from beaver.errors import BeaverError
from beaver.references import inspect_graph


def _meta(guid):
    return f"fileFormatVersion: 2\nguid: {guid}\n"


class ReferenceGraphTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir="/private/tmp")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.project = self.root / "project"
        self.assets = self.project / "Assets"
        self.assets.mkdir(parents=True)

    def test_guid_dependencies_reverse_edges_and_json_strings(self):
        (self.assets / "A.prefab").write_text("%YAML 1.1\n--- !u!1 &1\nobject: {fileID: 1, guid: " + "b" * 32 + "}\n")
        (self.assets / "A.prefab.meta").write_text(_meta("a" * 32))
        (self.assets / "B.asset").write_text("%YAML 1.1\n--- !u!114 &1\nobject: {fileID: 1, guid: " + "c" * 32 + "}\n")
        (self.assets / "B.asset.meta").write_text(_meta("b" * 32))
        (self.assets / "C.asset.meta").write_text(_meta("c" * 32))
        (self.assets / "config.json").write_text('{"icon":"Assets/A","display":"hello"}')
        result = inspect_graph(self.project, self.root / "out")
        self.assertEqual(result["schema"], "beaver-reference-graph/2")
        self.assertEqual(result["guidToPath"]["a" * 32], "Assets/A.prefab")
        self.assertEqual(result["dependencies"]["Assets/A.prefab"], ["Assets/B.asset"])
        self.assertEqual(result["dependencies"]["Assets/B.asset"], ["Assets/C.asset"])
        self.assertEqual(result["reverseDependencies"]["Assets/B.asset"], ["Assets/A.prefab"])
        self.assertEqual(result["reverseDependencies"]["Assets/C.asset"], ["Assets/B.asset"])
        self.assertTrue(all(row["status"] == "unresolved" for row in result["unresolvedJsonStrings"]))
        saved = json.loads(Path(result["report"]).read_text())
        self.assertEqual(saved["schema"], "beaver-reference-graph/2")

    def test_csharp_lexer_reports_only_exact_literal_arguments(self):
        code = ('// Resources.Load("comment");\n'
                'var text = "Resources.Load<Texture2D>(\\"string\\")";\n'
                'var exact = Resources.Load<Texture2D>("Icons/Map", typeof(Texture2D));\n'
                'var concat = Resources.Load("Icons/" + suffix);\n'
                'var verbatim = Resources.Load(@"Icons/Map");\n'
                'var interpolated = Addressables.LoadAssetAsync<Sprite>($"Icons/{name}");\n')
        (self.assets / "Loader.cs").write_text(code)
        report = inspect_graph(self.project, self.root / "out")
        self.assertEqual(len(report["literalLoads"]), 1)
        literal = report["literalLoads"][0]
        self.assertEqual(literal["key"], "Icons/Map")
        self.assertEqual(literal["type"], "Resources.Load")
        self.assertEqual(code[literal["span"][0]:literal["span"][1]], '"Icons/Map"')
        unknown_keys = [row["key"] for row in report["unknownDynamicLoads"]]
        self.assertEqual(unknown_keys, ['"Icons/" + suffix', '@"Icons/Map"', '$"Icons/{name}"'])

    def test_cache_hits_stat_identity_and_invalidation(self):
        source = self.assets / "Thing.prefab"
        source.write_text("%YAML 1.1\n--- !u!1 &1\nobject: {fileID: 1}\n")
        cache = self.root / "cache.json"
        first = inspect_graph(self.project, self.root / "first", cache)
        second = inspect_graph(self.project, self.root / "second", cache)
        self.assertEqual(first["counts"]["parse"], 1)
        self.assertEqual(second["counts"]["cacheHit"], 1)
        cache_doc = json.loads(cache.read_text())
        self.assertEqual(cache_doc["implementation"], "reference-graph/3")
        self.assertIn("ctimeNs", cache_doc["entries"]["Assets/Thing.prefab"]["stat"])
        self.assertIn("inode", cache_doc["entries"]["Assets/Thing.prefab"]["stat"])
        original_stat = source.stat()
        source.write_text("%YAML 1.1\n--- !u!1 &1\nobject: {fileID: 1, guid: " + "d" * 32 + "}\n")
        os.utime(source, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))
        third = inspect_graph(self.project, self.root / "third", cache)
        self.assertEqual(third["counts"]["parse"], 1)
        cache_doc = json.loads(cache.read_text())
        cache_doc["implementation"] = "reference-graph/old"
        cache.write_text(json.dumps(cache_doc))
        fourth = inspect_graph(self.project, self.root / "fourth", cache)
        self.assertEqual(fourth["counts"]["parse"], 1)

    def test_duplicate_guid_blocks_and_does_not_choose_arbitrary_path(self):
        (self.assets / "A.asset.meta").write_text(_meta("a" * 32))
        (self.assets / "B.asset.meta").write_text(_meta("a" * 32))
        (self.assets / "Ref.prefab").write_text("%YAML 1.1\n--- !u!1 &1\nobject: {fileID: 1, guid: " + "a" * 32 + "}\n")
        result = inspect_graph(self.project, self.root / "out")
        self.assertEqual(result["status"], "blocked")
        self.assertNotIn("a" * 32, result["guidToPath"])
        self.assertEqual(result["duplicateGuids"]["a" * 32], ["Assets/A.asset", "Assets/B.asset"])
        self.assertEqual(result["dependencies"]["Assets/Ref.prefab"], ["ambiguous:" + "a" * 32])

    def test_output_is_create_only_cache_is_distinct_and_source_is_safe(self):
        output = self.root / "out"
        inspect_graph(self.project, output)
        with self.assertRaisesRegex(BeaverError, "OUTPUT_EXISTS"):
            inspect_graph(self.project, output)
        shared = self.root / "shared.json"
        with self.assertRaisesRegex(BeaverError, "OUTPUT_OVERLAP"):
            inspect_graph(self.project, shared, shared)
        with self.assertRaisesRegex(BeaverError, "OUTPUT_OVERLAP"):
            inspect_graph(self.project, self.project / "report.json")
        outside = self.root / "outside"
        outside.mkdir()
        (self.assets / "link").symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(BeaverError, "SYMLINK_PATH"):
            inspect_graph(self.project, self.root / "symlink-out")

    def test_large_binary_contributes_to_inventory_but_is_never_read(self):
        large = self.assets / "large.bundle"
        with large.open("wb") as stream:
            stream.truncate(2 * 1024 * 1024 * 1024 + 1)
        result = inspect_graph(self.project, self.root / "out")
        self.assertEqual(result["counts"]["sourceBytes"], 2 * 1024 * 1024 * 1024 + 1)
        self.assertEqual(result["counts"]["skippedBinary"], 1)
        self.assertEqual(result["counts"]["hash"], 0)

    def test_aggregate_text_read_budget_blocks_before_unbounded_parsing(self):
        from unittest.mock import patch
        (self.assets/'large.json').write_text('{"x":"0123456789"}')
        with patch('beaver.references.MAX_TOTAL_BYTES',4):
            with self.assertRaisesRegex(BeaverError,'REFERENCE_TEXT_BUDGET'):
                inspect_graph(self.project,self.root/'bounded')


if __name__ == "__main__":
    unittest.main()
