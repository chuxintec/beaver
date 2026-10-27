"""Closed, explicit resource-production contracts and isolated image variants."""
from __future__ import annotations

import hashlib
import copy
import math
import os
from pathlib import Path
import re
import string

from .errors import fail
from .io import safe, read as bounded_read, load as bounded_load

KINDS = {"resource.sprites", "resource.atlas", "resource.font", "resource.spine", "resource.import", "resource.project-settings", "resource.serialized-asset"}
_PROJECT_SETTINGS_PATH = "ProjectSettings/EditorSettings.asset"
_SPRITE_PACKER_MODES = {"Disabled", "AlwaysOn", "AlwaysOnAtlas", "BuildTimeOnly", "BuildTimeOnlyAtlas", "SpriteAtlasV2", "SpriteAtlasV2Build"}
_COMMON = {"kind", "assetPath", "consumerBindings"}
_FIELDS = {
    "resource.sprites": _COMMON | {"sourcePath", "transform", "slices", "pixelsPerUnit", "maxSize", "alphaIsTransparency"},
    "resource.atlas": _COMMON | {"spritePaths", "includeInBuild", "packing", "pack", "buildTarget"},
    "resource.font": _COMMON | {"sourceFontPath", "characterSet", "texts", "samplingPointSize", "atlasPadding", "renderMode", "atlasSize", "fallbackPaths"},
    "resource.spine": _COMMON | {"skeletonPath", "atlasAssetPaths", "pagePaths", "materialPaths", "runtimeVersion", "animationNames"},
    "resource.import": _COMMON | {"importerType", "settings"},
    "resource.project-settings": {"kind", "assetPath", "setting", "expectedValue", "value"},
    "resource.serialized-asset": {"kind", "assetPath", "assetType", "expectedGuid", "references"},
}
_SPRITE_FIELDS = {"name", "spriteId", "fileId", "rect", "border", "pivot"}
_BIND_FIELDS = {"assetPath", "nodePath", "componentType", "property", "objectPath", "objectName"}
_REFERENCE_FIELDS = {"property", "guid", "localId"}


def _path(value, name, *, required=True):
    if value is None and not required:
        return None
    if not isinstance(value, str) or not value.startswith("Assets/") or "\\" in value or any(p in ("", ".", "..") for p in value.split("/")):
        fail("RESOURCE_PATH", name, "Expected normalized project-relative Assets/... path")
    return value


def _sha_file(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _asset_guid(value, location):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-fA-F]{32}", value):
        fail("RESOURCE_ASSET_GUID", location, "Expected exact 32-hex Unity asset GUID")
    return value.lower()


def _legacy_v1_atlases(project):
    root = safe(project)
    assets = root / "Assets"
    if assets.is_symlink() or not assets.is_dir():
        fail("RESOURCE_PROJECT_ROOT", str(assets), "Unity project must contain a real Assets directory")
    found = []
    for directory, subdirs, files in os.walk(assets, followlinks=False):
        parent = Path(directory)
        for name in list(subdirs):
            child = parent / name
            if child.is_symlink():
                fail("RESOURCE_SCAN_SYMLINK", str(child), "Cannot prove V1 SpriteAtlas scan completeness through symlink")
        for name in files:
            candidate = parent / name
            if candidate.suffix.lower() == ".spriteatlas":
                if candidate.is_symlink():
                    fail("RESOURCE_SCAN_SYMLINK", str(candidate), "V1 SpriteAtlas path is a symlink")
                found.append(candidate.relative_to(root).as_posix())
    return sorted(found)


def _sprite_editor_package_present(project):
    """Require Unity's already-resolved 2D Sprite package before declaring Sprite API work."""
    root = safe(project)
    packages = root / "Packages"
    if packages.is_symlink() or not packages.is_dir():
        fail("RESOURCE_SPRITE_PACKAGE_REQUIRED", str(packages), "Sprite Data Provider work requires an already-bound com.unity.2d.sprite package; resolve it before transaction file edits")
    manifest = packages / "manifest.json"
    if manifest.is_symlink():
        fail("RESOURCE_SPRITE_PACKAGE_REQUIRED", str(manifest), "Package manifest cannot be a symlink")
    if manifest.is_file():
        value = bounded_load(bounded_read(manifest))
        dependencies = value.get("dependencies", {}) if isinstance(value, dict) else {}
        if isinstance(dependencies, dict) and "com.unity.2d.sprite" in dependencies:
            return True
    lock = packages / "packages-lock.json"
    if lock.is_symlink():
        fail("RESOURCE_SPRITE_PACKAGE_REQUIRED", str(lock), "Package lock cannot be a symlink")
    if lock.is_file():
        value = bounded_load(bounded_read(lock))
        dependencies = value.get("dependencies", {}) if isinstance(value, dict) else {}
        if isinstance(dependencies, dict) and "com.unity.2d.sprite" in dependencies:
            return True
    embedded = packages / "com.unity.2d.sprite"
    embedded_manifest = embedded / "package.json"
    if embedded.is_symlink() or embedded_manifest.is_symlink():
        fail("RESOURCE_SPRITE_PACKAGE_REQUIRED", str(embedded), "Embedded package path cannot be a symlink")
    if embedded_manifest.is_file():
        value = bounded_load(bounded_read(embedded_manifest))
        if isinstance(value, dict) and value.get("name") == "com.unity.2d.sprite":
            return True
    fail("RESOURCE_SPRITE_PACKAGE_REQUIRED", str(packages), "Sprite Data Provider work requires com.unity.2d.sprite in Packages/manifest.json, packages-lock.json or an embedded package before transaction file edits")


def validate_step(step):
    """Return a validated detached step. Unknown keys and ambiguous defaults reject."""
    if not isinstance(step, dict) or step.get("kind") not in KINDS:
        fail("RESOURCE_KIND", "step.kind", "Unsupported resource production kind")
    kind = step["kind"]
    unknown = set(step) - _FIELDS[kind]
    missing = {"kind", "assetPath"} - set(step)
    if unknown or missing:
        fail("RESOURCE_FIELDS", "step", f"unknown={sorted(unknown)} missing={sorted(missing)}")
    out = copy.deepcopy(step)
    if kind == "resource.project-settings":
        if step.get("assetPath") != _PROJECT_SETTINGS_PATH or step.get("setting") != "spritePackerMode":
            fail("RESOURCE_PROJECT_SETTING", "step", "Only ProjectSettings/EditorSettings.asset:spritePackerMode is supported")
        if step.get("expectedValue") not in _SPRITE_PACKER_MODES or step.get("value") not in _SPRITE_PACKER_MODES:
            fail("RESOURCE_PROJECT_SETTING_VALUE", "step", "Use an exact Unity 6000.6.2f1 SpritePackerMode enum name")
        return out
    if kind == "resource.serialized-asset":
        out["assetPath"] = _path(step["assetPath"], "assetPath")
        if not out["assetPath"].endswith(".asset"):
            fail("RESOURCE_SERIALIZED_ASSET_PATH", "assetPath", "Only a native .asset target is supported")
        if not isinstance(step.get("assetType"), str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)+", step["assetType"]):
            fail("RESOURCE_SERIALIZED_ASSET_TYPE", "assetType", "Expected exact fully qualified C# type name")
        out["expectedGuid"] = _asset_guid(step.get("expectedGuid"), "expectedGuid")
        refs = step.get("references")
        if not isinstance(refs, list) or not refs:
            fail("RESOURCE_SERIALIZED_ASSET_REFERENCES", "references", "At least one exact object reference assertion is required")
        seen_properties = set(); normalized_refs = []
        for i, ref in enumerate(refs):
            location = f"references[{i}]"
            if not isinstance(ref, dict) or set(ref) != _REFERENCE_FIELDS:
                fail("RESOURCE_SERIALIZED_ASSET_REFERENCE_FIELDS", location, "Reference assertion requires exactly property, guid, localId")
            prop = ref["property"]
            if not isinstance(prop, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*", prop):
                fail("RESOURCE_SERIALIZED_ASSET_PROPERTY", f"{location}.property", "Expected exact dotted SerializedProperty field path")
            if prop in seen_properties:
                fail("RESOURCE_SERIALIZED_ASSET_PROPERTY", f"{location}.property", "Duplicate property assertion")
            local_id = ref["localId"]
            if not isinstance(local_id, int) or isinstance(local_id, bool) or local_id == 0 or not -(2**63) <= local_id < 2**63:
                fail("RESOURCE_SERIALIZED_ASSET_LOCAL_ID", f"{location}.localId", "Expected nonzero signed 64-bit Unity local fileID")
            seen_properties.add(prop)
            normalized_refs.append({"property": prop, "guid": _asset_guid(ref["guid"], f"{location}.guid"), "localId": local_id})
        out["references"] = normalized_refs
        return out
    out["assetPath"] = _path(step["assetPath"], "assetPath")
    binds = step.get("consumerBindings", [])
    if not isinstance(binds, list):
        fail("RESOURCE_BINDINGS", "consumerBindings", "Expected array")
    for i, binding in enumerate(binds):
        if not isinstance(binding, dict) or set(binding) != _BIND_FIELDS:
            fail("RESOURCE_BINDING_FIELDS", f"consumerBindings[{i}]", "Binding must declare every exact field")
        for key in ("assetPath", "objectPath"):
            _path(binding[key], f"consumerBindings[{i}].{key}")
        if not all(isinstance(binding[k], str) and binding[k] for k in ("componentType", "property", "objectName")) or not isinstance(binding["nodePath"], str):
            fail("RESOURCE_BINDING", f"consumerBindings[{i}]", "Invalid consumer identity")
    out["consumerBindings"] = binds
    if kind == "resource.sprites":
        if "sourcePath" in step:
            out["sourcePath"] = _path(step["sourcePath"], "sourcePath", required=False)
        if not isinstance(step.get("pixelsPerUnit"), (int, float)) or isinstance(step["pixelsPerUnit"], bool) or not math.isfinite(step["pixelsPerUnit"]) or step["pixelsPerUnit"] <= 0:
            fail("RESOURCE_PPU", "pixelsPerUnit", "Explicit positive PPU required")
        slices = step.get("slices")
        if not isinstance(slices, list) or not slices:
            fail("RESOURCE_SLICES", "slices", "At least one explicit SpriteRect required")
        seen_names, seen_ids, seen_files = set(), set(), set()
        for i, item in enumerate(slices):
            loc = f"slices[{i}]"
            if not isinstance(item, dict) or set(item) - _SPRITE_FIELDS or _SPRITE_FIELDS - set(item) not in ({"fileId"}, set()):
                fail("RESOURCE_SLICE_FIELDS", loc, "Slice requires name, spriteId, rect, border, pivot; fileId is an optional expected localID")
            if not isinstance(item["name"], str) or not item["name"] or item["name"] in seen_names:
                fail("RESOURCE_SPRITE_NAME", loc, "Unique non-empty Sprite name required")
            if not isinstance(item["spriteId"], str) or len(item["spriteId"]) != 32 or any(c not in string.hexdigits for c in item["spriteId"]):
                fail("RESOURCE_SPRITE_ID", loc, "Stable 32-hex Sprite GUID required")
            if "fileId" in item and (not isinstance(item["fileId"], int) or isinstance(item["fileId"], bool) or item["fileId"] == 0):
                fail("RESOURCE_SPRITE_ID", loc, "Optional expected fileId must be a nonzero integer")
            if item["spriteId"] in seen_ids or ("fileId" in item and item["fileId"] in seen_files):
                fail("RESOURCE_SPRITE_ID_COLLISION", loc, "Sprite IDs and fileIDs must be unique")
            if not isinstance(item["rect"], list) or not isinstance(item["border"], list) or not isinstance(item["pivot"], list) or len(item["rect"]) != 4 or len(item["border"]) != 4 or len(item["pivot"]) != 2 or any(not isinstance(v, (int, float)) or isinstance(v, bool) or not math.isfinite(v) for v in item["rect"] + item["border"] + item["pivot"]):
                fail("RESOURCE_SPRITE_GEOMETRY", loc, "rect/border/pivot lengths are 4/4/2")
            x, y, w, h = item["rect"]
            border = item["border"]
            if x < 0 or y < 0 or w <= 0 or h <= 0 or any(v < 0 for v in border) or border[0] + border[2] > w or border[1] + border[3] > h or any(not 0 <= v <= 1 for v in item["pivot"]):
                fail("RESOURCE_SPRITE_GEOMETRY", loc, "Invalid rect, border, or normalized pivot")
            seen_names.add(item["name"]); seen_ids.add(item["spriteId"])
            if "fileId" in item: seen_files.add(item["fileId"])
        transform = step.get("transform")
        if transform is not None:
            if not isinstance(transform, dict) or transform.get("mode") not in ("lossless-png", "resize-lanczos", "crop"):
                fail("RESOURCE_TRANSFORM", "transform", "Explicit supported transform policy required")
            mode = transform["mode"]
            required = {"mode"} if mode == "lossless-png" else ({"mode", "width", "height", "maxLossPercent"} if mode == "resize-lanczos" else {"mode", "crop", "maxLossPercent"})
            if set(transform) != required:
                fail("RESOURCE_TRANSFORM_FIELDS", "transform", f"Expected exactly {sorted(required)}")
            if mode == "resize-lanczos" and (not isinstance(transform["width"], int) or not isinstance(transform["height"], int) or min(transform["width"], transform["height"]) <= 0):
                fail("RESOURCE_RESIZE", "transform", "Explicit positive width and height required")
            if mode == "crop" and (not isinstance(transform["crop"], list) or len(transform["crop"]) != 4 or not all(isinstance(v, int) for v in transform["crop"])):
                fail("RESOURCE_CROP", "transform.crop", "Explicit integer crop bounds required")
            if mode != "lossless-png" and (not isinstance(transform["maxLossPercent"], (int, float)) or isinstance(transform["maxLossPercent"], bool) or not math.isfinite(transform["maxLossPercent"]) or not 0 <= transform["maxLossPercent"] <= 100):
                fail("RESOURCE_LOSS_BOUND", "transform.maxLossPercent", "Explicit 0..100 pixel-area loss bound required")
        if not isinstance(step.get("maxSize"), int) or step["maxSize"] < 32:
            fail("RESOURCE_MAX_SIZE", "maxSize", "Explicit texture maximum size required")
    elif kind == "resource.atlas":
        if not out["assetPath"].endswith(".spriteatlasv2"):
            fail("RESOURCE_ATLAS_V2_PATH", "assetPath", "SpriteAtlasAsset V2 path must end in .spriteatlasv2")
        if not isinstance(step.get("includeInBuild"), bool):
            fail("RESOURCE_ATLAS_BUILD", "includeInBuild", "Explicit boolean required")
        if not isinstance(step.get("pack"), bool):
            fail("RESOURCE_ATLAS_PACK", "pack", "Explicit boolean required")
        if step["pack"] and step.get("buildTarget") not in {"StandaloneOSX", "StandaloneWindows64", "StandaloneLinux64", "Android", "iOS", "WebGL"}:
            fail("RESOURCE_ATLAS_TARGET", "buildTarget", "Supported explicit Unity BuildTarget required when pack=true")
        if not step["pack"] and "buildTarget" in step:
            fail("RESOURCE_ATLAS_TARGET", "buildTarget", "Omit buildTarget unless pack=true")
        paths = step.get("spritePaths")
        if not isinstance(paths, list) or not paths or not all(isinstance(p, str) for p in paths) or len(paths) != len(set(paths)):
            fail("RESOURCE_ATLAS_SPRITES", "spritePaths", "Non-empty unique Sprite paths required")
        out["spritePaths"] = [_path(p, f"spritePaths[{i}]") for i, p in enumerate(paths)]
        packing = step.get("packing")
        if not isinstance(packing, dict) or set(packing) != {"padding", "tight", "rotation", "alphaDilation"} or not isinstance(packing["padding"], int) or isinstance(packing["padding"], bool) or packing["padding"] < 0 or not all(isinstance(packing[k], bool) for k in ("tight", "rotation", "alphaDilation")):
            fail("RESOURCE_ATLAS_PACKING", "packing", "Explicit padding/tight/rotation/alphaDilation required")
    elif kind == "resource.font":
        out["sourceFontPath"] = _path(step.get("sourceFontPath"), "sourceFontPath")
        if not isinstance(step.get("texts"), list) or not all(isinstance(x, str) for x in step["texts"]):
            fail("RESOURCE_FONT_TEXTS", "texts", "Explicit text corpus required")
        if step.get("characterSet") not in ("texts", "ascii"):
            fail("RESOURCE_FONT_CHARSET", "characterSet", "Use texts or ascii")
        if step["characterSet"] == "texts" and not any(step["texts"]):
            fail("RESOURCE_FONT_TEXTS", "texts", "texts characterSet requires a non-empty explicit text corpus")
        if not isinstance(step.get("fallbackPaths", []), list):
            fail("RESOURCE_FONT_FALLBACK", "fallbackPaths", "Expected explicit fallback path array")
        out["fallbackPaths"] = [_path(p, f"fallbackPaths[{i}]") for i, p in enumerate(step.get("fallbackPaths", []))]
        for field, minimum in (("samplingPointSize", 8), ("atlasPadding", 0), ("atlasSize", 64)):
            if not isinstance(step.get(field), int) or step[field] < minimum:
                fail("RESOURCE_FONT_SETTING", field, "Explicit supported integer required")
        if step.get("renderMode") not in ("SDF", "SDFAA", "SDF8", "SDF16", "SDF32"):
            fail("RESOURCE_FONT_MODE", "renderMode", "Unsupported explicit SDF render mode")
    elif kind == "resource.spine":
        for field in ("skeletonPath",):
            if field in step: out[field] = _path(step[field], field)
        for field in ("atlasAssetPaths", "pagePaths", "materialPaths"):
            values = step.get(field)
            if not isinstance(values, list) or not values:
                fail("RESOURCE_SPINE_CLOSURE", field, "Explicit non-empty closure list required")
            out[field] = [_path(p, f"{field}[{i}]") for i, p in enumerate(values)]
        if not isinstance(step.get("runtimeVersion"), str) or not step["runtimeVersion"] or not isinstance(step.get("animationNames"), list) or not step["animationNames"] or not all(isinstance(n, str) and n for n in step["animationNames"]):
            fail("RESOURCE_SPINE_CONTRACT", "step", "Exact runtime version and animation names required")
    elif kind == "resource.import":
        allowed = {
            "TextureImporter": {"m_EnableMipMap", "m_IsReadable", "m_AlphaIsTransparency", "m_MaxTextureSize", "m_TextureCompression", "m_FilterMode", "m_WrapMode"},
            "AudioImporter": {"m_ForceToMono", "m_LoadInBackground", "m_PreloadAudioData", "m_LoadType", "m_CompressionFormat", "m_Quality", "m_SampleRateSetting"},
            "ModelImporter": {"m_IsReadable", "m_ImportMaterials", "m_ImportAnimation", "m_ImportCameras", "m_ImportLights"},
            "Material": {"shaderPath"},
        }
        settings = step.get("settings")
        if step.get("importerType") not in allowed or not isinstance(settings, dict) or not settings:
            fail("RESOURCE_IMPORT", "step", "Supported importer and non-empty explicit settings required")
        if set(settings) - allowed[step["importerType"]] or any(not isinstance(v, (str, int, float, bool)) for v in settings.values()):
            fail("RESOURCE_IMPORT_SETTING", "settings", "Unknown or non-scalar settings are outside the supported importer subset")
        if step["importerType"] == "Material":
            if set(settings) != {"shaderPath"}:
                fail("RESOURCE_MATERIAL_SHADER", "settings", "Material requires exactly one existing shaderPath")
            _path(settings["shaderPath"], "settings.shaderPath")
        out["settings"] = [{"key": key, "value": str(value)} for key, value in sorted(settings.items())]
    return out


def declared_paths(project, step):
    """Return a closed project-relative read/write set; no globbing or hidden copies."""
    s = validate_step(step)
    if s["kind"] == "resource.serialized-asset":
        return {"reads": [s["assetPath"], s["assetPath"] + ".meta"], "writes": [s["assetPath"], s["assetPath"] + ".meta"]}
    if s["kind"] == "resource.sprites":
        _sprite_editor_package_present(project)
    if s["kind"] == "resource.project-settings":
        if s["value"] in {"SpriteAtlasV2", "SpriteAtlasV2Build"}:
            legacy = _legacy_v1_atlases(project)
            if legacy:
                fail("RESOURCE_V1_ATLAS_MIGRATION_BLOCKED", _PROJECT_SETTINGS_PATH, "SpriteAtlasV2 setting would migrate/delete legacy V1 atlases; no migration contract exists: " + ", ".join(legacy))
        return {"reads": [_PROJECT_SETTINGS_PATH], "writes": [_PROJECT_SETTINGS_PATH]}
    reads, writes = {s["assetPath"]}, {s["assetPath"], s["assetPath"] + ".meta"}
    for key in ("sourcePath", "sourceFontPath", "skeletonPath"):
        if s.get(key): reads.add(s[key])
    for key in ("spritePaths", "atlasAssetPaths", "pagePaths", "materialPaths", "fallbackPaths"):
        reads.update(s.get(key, []))
    if s["kind"] == "resource.import" and s["importerType"] == "Material":
        reads.add(s["settings"][0]["value"])
    for binding in s["consumerBindings"]:
        reads.add(binding["assetPath"]); writes.update((binding["assetPath"], binding["assetPath"] + ".meta"))
    return {"reads": sorted(reads), "writes": sorted(writes)}


def inspect_image(path):
    """Read-only bounded inspection for static project images up to 20 MP/256 MiB."""
    from PIL import Image
    path = safe(path)
    if path.stat().st_size > 256 * 1024 * 1024:
        fail("RESOURCE_IMAGE_SCOPE", str(path), "Compressed input exceeds 256 MiB")
    before = _sha_file(path)
    with Image.open(path) as image:
        if image.format not in ("PNG", "JPEG", "WEBP") or getattr(image, "n_frames", 1) != 1 or image.width * image.height > 20_000_000:
            fail("RESOURCE_IMAGE_SCOPE", str(path), "Static PNG/JPEG/WebP up to 20 MP required")
        rgba = image.convert("RGBA")
        alpha = rgba.getchannel("A")
        hist = alpha.histogram()
        result = {"path": str(path), "sha256": before, "bytes": path.stat().st_size, "width": image.width, "height": image.height,
                  "format": image.format, "alphaExtrema": alpha.getextrema(), "visibleBounds": alpha.getbbox(),
                  "transparentPixels": hist[0], "partialAlphaPixels": sum(hist[1:255]),
                  "decodedRGBASha256": hashlib.sha256(rgba.tobytes()).hexdigest()}
    if _sha_file(path) != before:
        fail("RESOURCE_SOURCE_CHANGED", str(path), "Source changed during inspection")
    return result


def produce_image(source, output, policy, expected_sha256):
    """Create one independent image candidate; it never installs into a Unity project."""
    from PIL import Image
    source, output = safe(source), safe(output)
    if output.exists() or source == output:
        fail("RESOURCE_OUTPUT", str(output), "Candidate output must be a fresh path distinct from source")
    if output.is_relative_to(source.parent):
        fail("RESOURCE_OUTPUT_SCOPE", str(output), "Candidate must be outside its source directory tree")
    info = inspect_image(source)
    digest = info["sha256"]
    if digest != expected_sha256:
        fail("RESOURCE_SOURCE_CHANGED", str(source), "Expected source SHA-256 mismatch")
    if not isinstance(policy, dict) or policy.get("mode") not in ("lossless-png", "resize-lanczos", "crop"):
        fail("RESOURCE_TRANSFORM", "policy", "Supported explicit transform mode required")
    mode_fields = {"lossless-png": {"mode"}, "resize-lanczos": {"mode", "width", "height", "maxLossPercent"}, "crop": {"mode", "crop", "maxLossPercent"}}
    if set(policy) != mode_fields[policy["mode"]]:
        fail("RESOURCE_TRANSFORM_FIELDS", "policy", f"Expected exactly {sorted(mode_fields[policy['mode']])}")
    with Image.open(source) as original:
        if original.format not in ("PNG", "JPEG", "WEBP") or getattr(original, "n_frames", 1) != 1 or original.width * original.height > 20_000_000:
            fail("RESOURCE_IMAGE_SCOPE", str(source), "Static PNG/JPEG/WebP up to 20 MP required")
        original.load(); image = original.convert("RGBA")
        mode = policy["mode"]
        source_area = image.width * image.height
        if mode == "resize-lanczos":
            w, h = policy.get("width"), policy.get("height")
            if not isinstance(w, int) or not isinstance(h, int) or min(w, h) <= 0 or w*h > 20_000_000:
                fail("RESOURCE_RESIZE", "policy", "Explicit positive width/height <=20MP required")
            if not isinstance(policy.get("maxLossPercent"), (int, float)) or isinstance(policy["maxLossPercent"], bool) or not math.isfinite(policy["maxLossPercent"]) or not 0 <= policy["maxLossPercent"] <= 100:
                fail("RESOURCE_LOSS_BOUND", "policy.maxLossPercent", "Explicit 0..100 percent pixel loss bound required")
            image = image.resize((w, h), Image.Resampling.LANCZOS)
        elif mode == "crop":
            box = policy.get("crop")
            if not isinstance(box, list) or len(box) != 4 or not all(isinstance(v, int) for v in box):
                fail("RESOURCE_CROP", "policy.crop", "Explicit integer left/top/right/bottom required")
            l,t,r,b=box
            if l<0 or t<0 or r<=l or b<=t or r>image.width or b>image.height:
                fail("RESOURCE_CROP", "policy.crop", "Crop must lie within source bounds")
            image=image.crop((l,t,r,b))
        loss = 0.0 if mode == "lossless-png" else max(0.0, 100.0 * (1.0 - (image.width * image.height / source_area)))
        if mode != "lossless-png" and (not isinstance(policy.get("maxLossPercent"), (int, float)) or isinstance(policy["maxLossPercent"], bool) or not math.isfinite(policy["maxLossPercent"]) or not 0 <= policy["maxLossPercent"] <= 100):
            fail("RESOURCE_LOSS_BOUND", "policy.maxLossPercent", "Explicit 0..100 percent pixel-area-loss bound required")
        if loss > policy.get("maxLossPercent", 0):
            fail("RESOURCE_LOSS_EXCEEDED", "policy.maxLossPercent", f"Pixel-area reduction {loss:.4f}% exceeds declared bound")
        output.parent.mkdir(parents=True, exist_ok=True)
        image.save(output, format="PNG", optimize=True, compress_level=9)
    if _sha_file(source) != expected_sha256:
        output.unlink(missing_ok=True)
        fail("RESOURCE_SOURCE_CHANGED", str(source), "Source changed during production")
    with Image.open(output) as generated:
        generated.load()
        if mode == "lossless-png":
            with Image.open(source) as original:
                if generated.convert("RGBA").tobytes() != original.convert("RGBA").tobytes():
                    output.unlink(missing_ok=True)
                    fail("RESOURCE_LOSSLESS_VERIFY", str(output), "Decoded pixel data changed")
    bound = 0 if mode == "lossless-png" else policy["maxLossPercent"]
    return {"sourceSha256": expected_sha256, "output": str(output), "outputSha256": _sha_file(output), "width": image.width, "height": image.height, "mode": mode, "lossMetric": "source_pixel_area_removed_percent", "pixelAreaReductionPercent": loss, "lossRangePercent": [loss, loss], "lossBoundPercent": bound, "installed": False}
