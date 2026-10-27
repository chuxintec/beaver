"""Read-only project metadata and bounded asset identity inventory."""
from pathlib import Path
import os
import re
from .io import safe, inside, load, read
from .errors import fail
from .output_limits import digest_file


def inspect(project, assets=False):
    root = safe(project)
    if not root.is_dir():
        fail("PROJECT_MISSING", str(root), "Expected existing project directory")
    unity = inside(root, "ProjectSettings/ProjectVersion.txt")
    creator = inside(root, "package.json")
    if unity.is_file() and (root / "Assets").is_dir():
        match = re.search(r"^m_EditorVersion: (\S+)", read(unity).decode(), re.M)
        engine, version = "unity", match.group(1) if match else None
        marker = unity
        packages = inside(root, "Packages/manifest.json")
        deps = load(read(packages)).get("dependencies", {}) if packages.is_file() else {}
        asset_dir = "Assets"
    elif creator.is_file() and (root / "assets").is_dir():
        data = load(read(creator))
        version = data.get("creator", {}).get("version")
        if not version:
            fail("PROJECT_UNKNOWN", str(root), "package.json lacks Creator version")
        engine, marker, deps, asset_dir = "creator", creator, data.get("dependencies", {}), "assets"
    else:
        fail("PROJECT_UNKNOWN", str(root), "Expected Unity or Creator project markers")
    result = dict(projectRoot=str(root), engine=engine, editorVersion=version,
                  markerSha256=digest_file(marker), packages=deps,
                  assetRoot=asset_dir, writableRoots=[], nativeConnection="not_probed",
                  structureContract="monkey-project/2.0.0 (consumer; no new path algorithm)",
                  assetInventory="not_requested")
    if assets:
        rows, identities, duplicates = [], {}, []
        total = 0
        for parent, dirs, files in os.walk(root / asset_dir, followlinks=False):
            for name in dirs + files:
                p = Path(parent) / name
                if p.is_symlink():
                    fail("SYMLINK_PATH", str(p), "Asset tree contains symbolic link")
            for name in files:
                p = Path(parent) / name
                total += p.stat().st_size
                if len(rows) >= 500000:
                    fail("OUTPUT_BUDGET", str(root), "Asset inventory exceeds 500000 files")
                relative = p.relative_to(root).as_posix()
                rows.append(dict(path=relative, bytes=p.stat().st_size))
                if name.endswith(".meta"):
                    raw = read(p)
                    if engine == "unity":
                        match = re.search(rb"^guid:\s*([a-f0-9]{32})\s*$", raw, re.M)
                        identity = match.group(1).decode() if match else None
                    else:
                        identity = load(raw).get("uuid")
                    if identity:
                        if identity in identities:
                            duplicates.append(dict(identity=identity, paths=[identities[identity], relative]))
                        identities[identity] = relative
        result["assetInventory"] = dict(fileCount=len(rows), bytes=total, identities=len(identities),
                                        duplicateIdentities=duplicates,
                                        payloadHashing="not_requested; use adoption plan for exact comparison")
    return result
