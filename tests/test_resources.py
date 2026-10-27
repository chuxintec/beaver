import hashlib
import os
import tempfile
import unittest
from pathlib import Path

from beaver.errors import BeaverError
from beaver.resources import validate_step, declared_paths, inspect_image, produce_image


def sprite_step():
    return {"kind": "resource.sprites", "assetPath": "Assets/UI/sheet.png", "pixelsPerUnit": 100,
            "maxSize": 2048, "alphaIsTransparency": True,
            "slices": [{"name": "button", "spriteId": "0123456789abcdef0123456789abcdef", "fileId": 123,
                        "rect": [0, 0, 32, 16], "border": [0, 0, 0, 0], "pivot": [.5, .5]}]}


def serialized_asset_step():
    return {"kind":"resource.serialized-asset","assetPath":"Assets/Game/Content/Data/Mainline/Mainline.asset",
            "assetType":"ExampleGame.Presentation.Content.Mainline.MainlineAsset","expectedGuid":"a"*32,
            "references":[{"property":"areaTextureAtlas","guid":"b"*32,"localId":123456789}]}


class ResourceTests(unittest.TestCase):
    def test_sprite_contract_and_explicit_writes(self):
        step = validate_step(sprite_step())
        with tempfile.TemporaryDirectory(dir="/private/tmp") as directory:
            project=Path(directory);packages=project/"Packages";packages.mkdir()
            (packages/"manifest.json").write_text('{"dependencies":{"com.unity.2d.sprite":"1.0.0"}}',encoding="utf-8")
            self.assertEqual(declared_paths(project, step), {"reads": ["Assets/UI/sheet.png"], "writes": ["Assets/UI/sheet.png", "Assets/UI/sheet.png.meta"]})

    def test_sprites_require_resolved_package_before_file_edits(self):
        step=sprite_step()
        with tempfile.TemporaryDirectory(dir="/private/tmp") as directory:
            project=Path(directory);(project/"Packages").mkdir()
            with self.assertRaisesRegex(BeaverError,"RESOURCE_SPRITE_PACKAGE_REQUIRED") as caught:
                declared_paths(project,step)
            self.assertEqual(caught.exception.status,"blocked")
            (project/"Packages"/"packages-lock.json").write_text('{"dependencies":{"com.unity.2d.sprite":{"version":"1.0.0"}}}',encoding="utf-8")
            self.assertIn("Assets/UI/sheet.png",declared_paths(project,step)["writes"])

    def test_embedded_sprite_package_is_accepted(self):
        with tempfile.TemporaryDirectory(dir="/private/tmp") as directory:
            project=Path(directory);embedded=project/"Packages"/"com.unity.2d.sprite";embedded.mkdir(parents=True)
            (embedded/"package.json").write_text('{"name":"com.unity.2d.sprite","version":"1.0.0"}',encoding="utf-8")
            self.assertIn("Assets/UI/sheet.png",declared_paths(project,sprite_step())["writes"])

    def test_serialized_asset_has_closed_asset_only_write_set(self):
        step=validate_step(serialized_asset_step())
        self.assertEqual(declared_paths("ignored",step),{
            "reads":["Assets/Game/Content/Data/Mainline/Mainline.asset","Assets/Game/Content/Data/Mainline/Mainline.asset.meta"],
            "writes":["Assets/Game/Content/Data/Mainline/Mainline.asset","Assets/Game/Content/Data/Mainline/Mainline.asset.meta"]})

    def test_serialized_asset_contract_rejects_open_or_invalid_references(self):
        base=serialized_asset_step()
        cases=(dict(base,extra="x"),dict(base,references=[]),dict(base,assetPath="Assets/Game/Mainline.prefab"),
               dict(base,expectedGuid="bad"),dict(base,references=[dict(base["references"][0],localId=0)]),
               dict(base,references=[dict(base["references"][0],extra=True)]),
               dict(base,references=[base["references"][0],base["references"][0]]))
        for payload in cases:
            with self.subTest(payload=payload),self.assertRaises(BeaverError):
                validate_step(payload)

    def test_closed_schema_rejects_ambiguous_payload(self):
        for change in ({"unexpected": True}, {"slices": [{"name": "missing-id", "rect": [0, 0, 1, 1]}]}, {"assetPath": "../escape.png"}):
            payload = sprite_step(); payload.update(change)
            with self.subTest(change=change), self.assertRaises(BeaverError):
                validate_step(payload)

    def test_new_sprite_id_is_identity_source_and_file_id_optional(self):
        payload = sprite_step(); payload["slices"][0].pop("fileId")
        self.assertEqual(validate_step(payload)["slices"][0]["spriteId"], "0123456789abcdef0123456789abcdef")

    def test_transform_needs_explicit_loss_bound(self):
        payload = sprite_step(); payload["transform"] = {"mode": "resize-lanczos", "width": 10, "height": 10}
        with self.assertRaises(BeaverError):
            validate_step(payload)

    def test_text_font_requires_nonempty_corpus(self):
        payload={"kind":"resource.font","assetPath":"Assets/Fonts/Generated.asset","sourceFontPath":"Assets/Fonts/Source.ttf","characterSet":"texts","texts":[],"samplingPointSize":48,"atlasPadding":6,"renderMode":"SDF16","atlasSize":1024,"fallbackPaths":[]}
        with self.assertRaises(BeaverError):
            validate_step(payload)

    def test_atlas_pack_requires_target(self):
        payload = {"kind": "resource.atlas", "assetPath": "Assets/UI/Main.spriteatlasv2", "spritePaths": ["Assets/UI/a.png"], "includeInBuild": True, "packing": {"padding": 4, "tight": False, "rotation": False, "alphaDilation": True}, "pack": True}
        with self.assertRaises(BeaverError):
            validate_step(payload)
        payload["buildTarget"] = "StandaloneOSX"
        self.assertTrue(validate_step(payload)["pack"])
        wrong = dict(payload, assetPath="Assets/UI/Main.spriteatlas")
        with self.assertRaises(BeaverError):
            validate_step(wrong)

    def test_only_explicit_sprite_packer_project_setting_is_declared(self):
        step = {"kind": "resource.project-settings", "assetPath": "ProjectSettings/EditorSettings.asset", "setting": "spritePackerMode", "expectedValue": "Disabled", "value": "AlwaysOnAtlas"}
        with tempfile.TemporaryDirectory(dir="/private/tmp") as directory:
            project = Path(directory); (project / "Assets").mkdir()
            self.assertEqual(declared_paths(project, step), {"reads": [step["assetPath"]], "writes": [step["assetPath"]]})
        invalid = dict(step, assetPath="ProjectSettings/ProjectSettings.asset")
        with self.assertRaises(BeaverError):
            validate_step(invalid)
        invalid = dict(step, value="NotAnEnum")
        with self.assertRaises(BeaverError):
            validate_step(invalid)

    def test_v1_atlas_blocks_only_v2_migration_modes(self):
        with tempfile.TemporaryDirectory(dir="/private/tmp") as directory:
            project=Path(directory); assets=project / "Assets/UI"; assets.mkdir(parents=True)
            (assets / "Legacy.spriteatlas").write_text("local fixture")
            base={"kind":"resource.project-settings","assetPath":"ProjectSettings/EditorSettings.asset","setting":"spritePackerMode","expectedValue":"Disabled"}
            for value in ("SpriteAtlasV2", "SpriteAtlasV2Build"):
                with self.subTest(value=value), self.assertRaisesRegex(BeaverError,"RESOURCE_V1_ATLAS_MIGRATION_BLOCKED"):
                    declared_paths(project, dict(base,value=value))
            safe=declared_paths(project,dict(base,value="AlwaysOnAtlas"))
            self.assertEqual(safe["writes"],["ProjectSettings/EditorSettings.asset"])

    def _image_test(self, operation):
        try:
            from PIL import Image
        except ImportError:
            self.skipTest("Pillow optional dependency unavailable")
        previous = Path.cwd()
        try:
            with tempfile.TemporaryDirectory(dir="/private/tmp") as directory:
                root = Path(directory)
                source_dir=root / "source"; output_dir=root / "candidate"; unrelated_cwd=root / "cwd"
                source_dir.mkdir(); output_dir.mkdir(); unrelated_cwd.mkdir(); os.chdir(unrelated_cwd)
                source = source_dir / "source.png"
                Image.new("RGBA", (10, 10), (1, 2, 3, 128)).save(source)
                digest = hashlib.sha256(source.read_bytes()).hexdigest()
                operation(Image, output_dir, source, digest)
        finally:
            os.chdir(previous)

    def test_lossless_png_candidate(self):
        def run(Image, work, source, digest):
            self.assertEqual(inspect_image(source)["partialAlphaPixels"], 100)
            output = work / "candidate.png"
            report = produce_image(source, output, {"mode": "lossless-png"}, digest)
            self.assertFalse(report["installed"]); self.assertEqual(report["pixelAreaReductionPercent"], 0)
            self.assertEqual(report["lossMetric"], "source_pixel_area_removed_percent")
            self.assertEqual(report["lossRangePercent"], [0.0, 0.0])
            self.assertTrue(output.exists())
        self._image_test(run)

    def test_resize_enforces_pixel_area_loss_bound(self):
        def run(Image, work, source, digest):
            with self.assertRaisesRegex(BeaverError, "RESOURCE_LOSS_EXCEEDED"):
                produce_image(source, work / "candidate.png", {"mode": "resize-lanczos", "width": 5, "height": 5, "maxLossPercent": 50}, digest)
        self._image_test(run)


if __name__ == "__main__":
    unittest.main()
