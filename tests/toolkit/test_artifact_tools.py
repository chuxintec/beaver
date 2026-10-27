#!/usr/bin/env python3
"""Regression tests; default stdlib tests. Optional --integration needs ffmpeg,
ffprobe, makensis, 7zz and --fixture-exe (an existing x64 PE, NEVER executed).
All generated inputs/outputs live in temporary directories outside the skill.
"""
import argparse
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

SCRIPTS = Path(__file__).resolve().parents[2] / 'src/beaver/toolkit'
INTEGRATION = False
EXE = None


class ArtifactTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="game-tool-test-")
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def cli(self, script, *args, expected=0):
        result = subprocess.run([sys.executable, str(SCRIPTS / script), *map(str, args)],
                                cwd=self.root, capture_output=True, text=True, timeout=180)
        self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
        return result

    def tree(self, name, entries):
        root = self.root / name
        root.mkdir()
        for rel, content in entries.items():
            path = root / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
        return root

    def test_two_equal_inputs_and_difference_types(self):
        for i, entries in enumerate(({"a": b"abc"}, {"资源/a file": b"xyz", "zero": b""})):
            a, b = self.tree(f"a{i}", entries), self.tree(f"b{i}", entries)
            report = json.loads(self.cli("verify_payload_tree.py", a, b).stdout)
            self.assertTrue(report["equal"])
        (b / "zero").unlink()
        (b / "extra").write_bytes(b"x")
        (b / "资源/a file").write_bytes(b"changed")
        report = json.loads(self.cli("verify_payload_tree.py", a, b, expected=1).stdout)
        self.assertEqual(report["missing"], ["zero"])
        self.assertEqual(report["extra"], ["extra"])
        self.assertEqual(report["changed"], ["资源/a file"])

    def test_output_and_symlink_guards(self):
        a, b = self.tree("a", {"x": b"x"}), self.tree("b", {"x": b"x"})
        report = self.root / "result.json"
        self.cli("verify_payload_tree.py", a, b, "--output", report)
        saved = report.read_bytes()
        self.cli("verify_payload_tree.py", a, b, "--output", report, expected=2)
        self.assertEqual(saved, report.read_bytes())
        self.cli("verify_payload_tree.py", a, b, "--output", a / "report.json", expected=2)
        self.cli("verify_payload_tree.py", a, a, expected=2)
        (b / "link").symlink_to(a / "x")
        self.cli("verify_payload_tree.py", a, b, expected=2)
        alias = self.root / "alias"
        alias.symlink_to(a, target_is_directory=True)
        self.cli("verify_payload_tree.py", alias, b, expected=2)

    def test_exclusions_are_explicit_and_bilateral(self):
        a, b = self.tree("a", {"x": b"x", "logs/a": b"old"}), self.tree("b", {"x": b"x", "logs/b": b"new"})
        report = json.loads(self.cli("verify_payload_tree.py", a, b, "--exclude", "logs/*").stdout)
        self.assertTrue(report["equal"])
        self.assertEqual(report["excluded_globs"], ["logs/*"])
        self.cli("verify_payload_tree.py", a, b, "--exclude", "../*", expected=2)

    def test_packager_rejects_invalid_pe_and_traversal(self):
        payload = self.tree("payload", {"game.exe": b"not a PE"})
        common = ["--payload", payload, "--title", "Test", "--output", self.root / "game.exe"]
        self.cli("package_windows_portable.py", *common, "--entry", "game.exe", expected=2)
        self.cli("package_windows_portable.py", *common, "--entry", "../game.exe", expected=2)
        self.assertFalse((self.root / "game.exe").exists())

    def test_media_integration(self):
        if not INTEGRATION:
            self.skipTest("use --integration for real ffmpeg/ffprobe checks")
        for tool in ("ffmpeg", "ffprobe"):
            self.assertIsNotNone(shutil.which(tool), tool)
        silent, sound = self.root / "silent.mp4", self.root / "sound.mp4"
        for path, size, audio in ((silent, "96x64", False), (sound, "64x96", True)):
            command = ["ffmpeg", "-v", "error", "-f", "lavfi", "-i", f"color=c=blue:s={size}:r=12:d=0.5"]
            if audio:
                command += ["-f", "lavfi", "-i", "sine=frequency=440:duration=0.5"]
            command += ["-c:v", "mpeg4", "-t", "0.5", str(path)]
            subprocess.run(command, cwd=self.root, check=True, timeout=30)
        output = self.root / "media.json"
        self.cli("video_manifest.py", silent, sound, "--output", output, "--decode")
        rows = json.loads(output.read_text())["recordings"]
        self.assertTrue(all(row["decode_passed"] for row in rows))
        self.assertEqual([row["probe"]["streams"][0]["width"] for row in rows], [96, 64])
        absent = self.root / "absent-audio.json"
        self.cli("video_manifest.py", silent, "--require-audio", "--output", absent, expected=1)
        self.assertEqual(json.loads(absent.read_text())["recordings"][0]["issues"], ["no_audio_stream"])
        bad = self.root / "bad.mp4"
        bad.write_bytes(b"not video")
        self.cli("video_manifest.py", bad, "--output", self.root / "bad.json", expected=1)
        self.cli("video_manifest.py", sound, "--output", output, expected=2)
        self.cli("video_manifest.py", self.root / "missing.mp4", "--output", self.root / "missing.json", expected=2)

    def test_two_real_nsis_packages(self):
        if not INTEGRATION:
            self.skipTest("use --integration --fixture-exe for real NSIS packaging")
        for tool in ("makensis", "7zz"):
            self.assertIsNotNone(shutil.which(tool), tool)
        self.assertTrue(EXE and EXE.is_file(), "--fixture-exe must name an existing x64 PE")
        for i, entry in enumerate(("game.exe", "子目录/Game's $entry.exe")):
            payload = self.tree(f"payload{i}", {entry: EXE.read_bytes(), "数据/info.txt": b"one" if i == 0 else b"two"})
            output = self.root / f"single{i}.exe"
            self.cli("package_windows_portable.py", "--payload", payload, "--entry", entry,
                     "--title", 'Game "$demo" 测试', "--output", output)
            report = json.loads(output.with_suffix(".manifest.json").read_text())
            self.assertTrue(report["verification"]["equal"])
            self.assertEqual(report["verification"]["actual_files"], 2)
            self.assertFalse(report["windows_runtime_tested"])
            self.cli("package_windows_portable.py", "--payload", payload, "--entry", entry,
                     "--title", "Test", "--output", output, expected=2)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--integration", action="store_true")
    parser.add_argument("--fixture-exe", type=Path)
    options = parser.parse_args()
    INTEGRATION, EXE = options.integration, options.fixture_exe
    result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(ArtifactTests))
    sys.exit(0 if result.wasSuccessful() else 1)
