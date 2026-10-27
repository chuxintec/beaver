"""Roslyn-backed, candidate-only C# maintenance and five-layer module templates."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from pathlib import PurePosixPath
import re
import shlex
import shutil
import subprocess
import tempfile
import xml.etree.ElementTree as ET

from .errors import fail
from .io import encoded, load, new_output, read, safe, sha, write_json

CONTRACT = "beaver-csharp/1"
PLAN_SCHEMA = "beaver-csharp-rename/1"
INSPECT_SCHEMA = "beaver-csharp-inspect/1"
MODULE_SCHEMA = "beaver-module-template/1"
SEQUENCE_SCHEMA = "beaver-csharp-rename-sequence/1"
UNITY_STANDARD_PROFILE = "grea-unity-project/1.0.0"
LEGACY_MODULE_PROFILE = "beaver-legacy-five-layer/1.0.0"
HELPER_NAME = "BeaverSemantic.cs"
MAX_CSHARP_SOURCE_FILE_BYTES = 32 * 1024 * 1024
MAX_CSHARP_SOURCE_TOTAL_BYTES = 512 * 1024 * 1024
MAX_CSHARP_SOURCE_FILES = 40000
IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
QUALIFIED_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*$")
VIEW_TYPE = re.compile(r"^(?:global::)?[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*$")
PRIMITIVES = {"string", "int", "long", "float", "double", "bool"}


def _sha_file(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _read_spec(value):
    if isinstance(value, dict):
        return value
    path = safe(value)
    return load(read(path))


def _resolve_toolchain(toolchain=None):
    requested = toolchain if isinstance(toolchain, dict) else {"dotnet": toolchain} if toolchain else {}
    dotnet = requested.get("dotnet") or shutil.which("dotnet")
    if not dotnet:
        fail("CSHARP_TOOLCHAIN_MISSING", "dotnet", "Install or explicitly select a local .NET SDK; downloads are not performed")
    dotnet = str(Path(dotnet).expanduser().resolve())
    if not Path(dotnet).is_file():
        fail("CSHARP_TOOLCHAIN_MISSING", dotnet, "Selected dotnet executable does not exist")
    sdk_root = requested.get("sdkRoot")
    if sdk_root:
        sdk_root = Path(sdk_root).expanduser().resolve()
        sdk_version = sdk_root.name
    else:
        try:
            listing = subprocess.run([dotnet, "--list-sdks"], check=True, capture_output=True,
                                     text=True, timeout=15).stdout
        except (OSError, subprocess.SubprocessError) as error:
            fail("CSHARP_TOOLCHAIN_PROBE_FAILED", dotnet, str(error))
        sdk_rows = []
        for line in listing.splitlines():
            match = re.match(r"([^ ]+) \[(.+)\]", line.strip())
            if match:
                sdk_rows.append((match.group(1), Path(match.group(2)) / match.group(1)))
        sdk_rows.sort(key=lambda row: tuple(int(x) if x.isdigit() else x for x in re.split(r"[.+-]", row[0])))
        if not sdk_rows:
            fail("CSHARP_TOOLCHAIN_MISSING", dotnet, "No installed SDK was reported by dotnet --list-sdks")
        sdk_version, sdk_root = sdk_rows[-1]
    roslyn = Path(requested.get("roslynDirectory") or (sdk_root / "Roslyn" / "bincore")).resolve()
    compiler = roslyn / "csc.dll"
    code_analysis = roslyn / "Microsoft.CodeAnalysis.dll"
    csharp_analysis = roslyn / "Microsoft.CodeAnalysis.CSharp.dll"
    missing = [str(path) for path in (compiler, code_analysis, csharp_analysis) if not path.is_file()]
    if missing:
        fail("CSHARP_ROSLYN_MISSING", str(roslyn), "Roslyn compiler/runtime assemblies are missing: " + ", ".join(missing))
    return {
        "dotnet": dotnet,
        "sdkVersion": sdk_version,
        "sdkRoot": str(sdk_root),
        "roslynDirectory": str(roslyn),
        "compiler": str(compiler),
        "hashes": {"csc": _sha_file(compiler), "Microsoft.CodeAnalysis": _sha_file(code_analysis),
                   "Microsoft.CodeAnalysis.CSharp": _sha_file(csharp_analysis)},
    }


def _expand_msbuild(text, properties, project_dir):
    if not text:
        return ""
    value = text.strip().replace("%3B", ";")
    value = value.replace("$(MSBuildProjectDirectory)", str(project_dir))
    value = value.replace("$(MSBuildProjectFullPath)", str(project_dir / (properties.get("MSBuildProjectName", "") + ".csproj")))
    value = value.replace("$(MSBuildThisFileDirectory)", str(project_dir) + os.sep)
    for _ in range(8):
        matches = re.findall(r"\$\(([A-Za-z_][A-Za-z0-9_]*)\)", value)
        if not matches:
            return value
        changed = False
        for name in matches:
            if name in properties:
                value = value.replace("$(" + name + ")", properties[name])
                changed = True
        if not changed:
            break
    if "$ (" in value or re.search(r"\$\([^)]+\)", value):
        fail("CSHARP_MSBUILD_EXPRESSION_UNKNOWN", str(project_dir), "MSBuild property cannot be expanded: " + value)
    return value


def _parse_csproj(project, csproj, root_projects):
    try:
        tree = ET.parse(csproj)
    except (ET.ParseError, OSError) as error:
        fail("CSHARP_PROJECT_PARSE_FAILED", str(csproj), str(error))
    project_dir = csproj.parent
    properties = {"MSBuildProjectName": csproj.stem, "Configuration": "Debug", "Platform": "AnyCPU"}
    conditional_groups = []
    for group in tree.getroot().findall("PropertyGroup"):
        condition = group.attrib.get("Condition")
        if condition:
            conditional_groups.append(condition)
            if not all(marker in condition for marker in ("$(Configuration)", "$(Platform)")):
                fail("CSHARP_MSBUILD_CONDITION_UNSUPPORTED", str(csproj), "Property group condition is outside the supported Configuration/Platform subset: " + condition)
            if "$(Configuration)" in condition or "$(Platform)" in condition:
                # Unity project files commonly define Debug/Release separately. Use Debug only when explicit.
                if "Debug" not in condition and "AnyCPU" not in condition:
                    continue
        for item in list(group):
            if item.text and item.tag not in properties:
                properties[item.tag] = _expand_msbuild(item.text, properties, project_dir)
    if conditional_groups and not properties.get("Configuration"):
        fail("CSHARP_CONFIGURATION_UNKNOWN", str(csproj), "Conditional MSBuild property groups need explicit compileInputs")
    sources = []
    references = []
    project_refs = []
    for group in tree.getroot().findall("ItemGroup"):
        if group.attrib.get("Condition"):
            fail("CSHARP_MSBUILD_CONDITION_UNSUPPORTED", str(csproj), "Conditioned compile/reference item groups require explicit toolchain.assemblies")
        if any(item.attrib.get("Condition") for item in list(group)):
            fail("CSHARP_MSBUILD_CONDITION_UNSUPPORTED", str(csproj), "Conditioned compile/reference items require explicit toolchain.assemblies")
    for item in tree.getroot().iter():
        if item.tag == "Compile" and (item.attrib.get("Remove") or item.attrib.get("Update")):
            fail("CSHARP_MSBUILD_ITEM_MUTATION_UNSUPPORTED", str(csproj), "Compile Remove/Update requires explicit sourceFiles")
        if item.tag == "Compile" and item.attrib.get("Include"):
            include = _expand_msbuild(item.attrib["Include"], properties, project_dir)
            for part in include.split(";"):
                candidate = Path(part)
                candidates = sorted(project_dir.glob(part)) if any(char in part for char in "*?[") else [candidate if candidate.is_absolute() else project_dir / candidate]
                for source in candidates:
                    if not source.is_file() or source.suffix.lower() != ".cs":
                        continue
                    source = safe(source)
                    if not source.is_relative_to(project):
                        fail("CSHARP_SOURCE_OUTSIDE_PROJECT", str(source), "Compiled source must remain inside the selected project")
                    sources.append(source)
        elif item.tag == "Reference":
            include = item.attrib.get("Include", "").split(",", 1)[0]
            hint = item.find("HintPath")
            if hint is not None and hint.text:
                raw = _expand_msbuild(hint.text, properties, project_dir)
                path = Path(raw)
                if not path.is_absolute():
                    path = project_dir / path
                path = Path(os.path.normpath(path))
                if not path.is_file():
                    fail("CSHARP_REFERENCE_MISSING", str(path), "Project reference HintPath does not exist")
                references.append(path.resolve())
            elif include:
                fail("CSHARP_REFERENCE_UNRESOLVED", str(csproj), "Reference has no local HintPath: " + include)
        elif item.tag == "ProjectReference" and item.attrib.get("Include"):
            raw = _expand_msbuild(item.attrib["Include"], properties, project_dir)
            path = Path(raw)
            if not path.is_absolute():
                path = project_dir / path
            target = Path(os.path.normpath(path)).resolve()
            project_refs.append(target.stem)
    if not sources:
        fail("CSHARP_SOURCE_SET_EMPTY", str(csproj), "No explicit .cs files were listed in the project")
    constants = _expand_msbuild(properties.get("DefineConstants", ""), properties, project_dir)
    assembly = properties.get("AssemblyName", csproj.stem)
    lang = properties.get("LangVersion", "default")
    nullable = properties.get("Nullable", "disable")
    return {"name": assembly, "sources": [str(p) for p in sorted(set(sources))],
            "references": sorted({str(p) for p in references}), "projectReferences": project_refs,
            "defines": sorted({x.strip() for x in re.split(r"[;,]", constants) if x.strip()}),
            "languageVersion": lang, "nullable": nullable, "allowUnsafe": properties.get("AllowUnsafeBlocks", "false").lower() == "true",
            "configuration": properties.get("Configuration", "Debug"),
            "projectFile": str(csproj), "conditionalGroups": conditional_groups}


def _parse_response_file(project, rsp, source_assemblies=None):
    """Read one Unity/Roslyn response file and its adjacent Bee assembly graph."""
    rsp = safe(rsp if Path(rsp).is_absolute() else project / rsp)
    if not rsp.is_file() or not rsp.is_relative_to(project):
        fail("CSHARP_RSP_PATH", str(rsp), "Response file must be a regular file inside the selected project")
    dag_dir = rsp.parent
    if not dag_dir.name.endswith(".dag"):
        fail("CSHARP_RSP_LAYOUT_UNKNOWN", str(rsp), "Expected a response file in a Bee .dag output directory")
    bee_root = dag_dir.parent.parent
    build_id = dag_dir.name[:-4]
    input_data = safe(bee_root / (build_id + "-inputdata.json"))
    if not input_data.is_file():
        candidates = sorted((project / "Library" / "Bee").glob("*-inputdata.json"))
        matches = []
        for candidate in candidates:
            try:
                payload = load(read(safe(candidate)))
                block = payload.get("ScriptCompilationBuildProgram.Data.ScriptCompilationData", {})
                if any(row.get("Name") == rsp.stem and str(Path(row.get("Path", project)).resolve()) == str(project)
                       for row in block.get("Assemblies", [])):
                    matches.append(candidate)
            except Exception:
                continue
        if len(matches) != 1:
            fail("CSHARP_RSP_INPUTDATA_UNKNOWN", str(rsp), "Could not uniquely pair response file with Bee compilation input data")
        input_data = safe(matches[0])
    if not input_data.is_relative_to(project):
        fail("CSHARP_RSP_INPUTDATA_OUTSIDE_PROJECT", str(input_data), "Bee input data must remain inside the selected project")
    try:
        input_payload = load(read(input_data), max_nodes=2_000_000, max_depth=128)
    except Exception as error:
        fail("CSHARP_RSP_INPUTDATA_INVALID", str(input_data), str(error))
    data = input_payload.get("ScriptCompilationBuildProgram.Data.ScriptCompilationData")
    if not isinstance(data, dict) or not isinstance(data.get("Assemblies"), list):
        fail("CSHARP_RSP_INPUTDATA_INVALID", str(input_data), "Expected Unity ScriptCompilationData.Assemblies")
    all_rows = data["Assemblies"]
    source_filter = None
    if source_assemblies is not None:
        if not isinstance(source_assemblies, list) or not source_assemblies or any(not isinstance(name, str) or not name for name in source_assemblies):
            fail("CSHARP_SOURCE_ASSEMBLY_FILTER_INVALID", str(input_data), "sourceAssemblies must be an explicit nonempty list of assembly names")
        if len(source_assemblies) != len(set(source_assemblies)):
            fail("CSHARP_SOURCE_ASSEMBLY_FILTER_INVALID", str(input_data), "sourceAssemblies cannot contain duplicate names")
        source_filter = set(source_assemblies)
    index_by_name = {}
    for index, row in enumerate(all_rows):
        name = row.get("Name")
        if not isinstance(name, str) or not name or name in index_by_name:
            fail("CSHARP_RSP_ASSEMBLY_AMBIGUOUS", str(input_data), "Bee assembly names must be unique and explicit")
        index_by_name[name] = index

    def parse_args(path):
        try:
            lines = read(path).decode("utf-8-sig").splitlines()
            args = [token for line in lines for token in shlex.split(line, posix=True, comments=True)]
        except Exception as error:
            fail("CSHARP_RSP_PARSE_FAILED", str(path), str(error))
        parsed = {"references": [], "defines": [], "analyzers": [], "additionalFiles": [], "unsupported": [],
                  "name": None, "languageVersion": None, "allowUnsafe": False, "nullable": "disable", "target": None}
        for arg in args:
            lower = arg.lower()
            if lower.startswith(("-out:", "/out:")):
                parsed["name"] = Path(arg.split(":", 1)[1]).stem
            elif lower.startswith(("-r:", "/r:", "-reference:", "/reference:")):
                value = arg.split(":", 1)[1]
                if "=" in value and not Path(value.split("=", 1)[0]).suffix.lower() == ".dll":
                    fail("CSHARP_RSP_REFERENCE_ALIAS_UNSUPPORTED", str(path), "Aliased metadata references need explicit compiler inputs")
                parsed["references"].extend(value.split(";"))
            elif lower.startswith(("-define:", "/define:")):
                parsed["defines"].extend(x for x in re.split(r"[;,]", arg.split(":", 1)[1]) if x)
            elif lower.startswith(("-langversion:", "/langversion:")):
                parsed["languageVersion"] = arg.split(":", 1)[1]
            elif lower.startswith(("-nullable:", "/nullable:")):
                value = arg.split(":", 1)[1].lower()
                if value not in ("enable", "disable", "annotations", "warnings"):
                    fail("CSHARP_RSP_NULLABLE_UNSUPPORTED", str(path), "Unsupported nullable context " + value)
                parsed["nullable"] = value
            elif lower in ("-unsafe", "/unsafe", "-unsafe+", "/unsafe+"):
                parsed["allowUnsafe"] = True
            elif lower in ("-unsafe-", "/unsafe-"):
                parsed["allowUnsafe"] = False
            elif lower.startswith(("-target:", "/target:")):
                parsed["target"] = arg.split(":", 1)[1].lower()
            elif lower.startswith(("-analyzer:", "/analyzer:")):
                parsed["analyzers"].append(arg.split(":", 1)[1])
            elif lower.startswith(("-additionalfile:", "/additionalfile:")):
                parsed["additionalFiles"].append(arg.split(":", 1)[1])
            elif lower.startswith(("-features:", "/features:", "-moduleassemblyname:", "/moduleassemblyname:",
                                  "-usings:", "/usings:", "-recurse:", "/recurse:")):
                parsed["unsupported"].append(arg)
        if parsed["target"] != "library" or not parsed["name"] or not parsed["languageVersion"]:
            fail("CSHARP_RSP_CONTEXT_INCOMPLETE", str(path), "Expected library target, output assembly, and explicit language version")
        return parsed

    parsed_by_name = {}
    def get_rsp(name):
        if name in parsed_by_name:
            return parsed_by_name[name]
        path = dag_dir / (name + ".rsp")
        if not path.is_file():
            fail("CSHARP_RSP_DEPENDENCY_MISSING", str(path), "A source assembly dependency has no matching response file")
        value = parse_args(safe(path))
        if value["name"] != name:
            fail("CSHARP_RSP_ASSEMBLY_MISMATCH", str(path), "Response output name does not match its filename")
        parsed_by_name[name] = (value, path)
        return parsed_by_name[name]

    primary = parse_args(rsp)
    if primary["name"] != rsp.stem:
        fail("CSHARP_RSP_ASSEMBLY_MISMATCH", str(rsp), "Response output assembly does not match its filename")
    parsed_by_name[primary["name"]] = (primary, rsp)
    root_index = index_by_name.get(primary["name"])
    if root_index is None:
        fail("CSHARP_RSP_ASSEMBLY_NOT_FOUND", primary["name"], "Assembly is absent from paired Bee input data")
    if source_filter is not None and primary["name"] not in source_filter:
        fail("CSHARP_RSP_ROOT_NOT_SELECTED", primary["name"], "Every selected response-file root must be included in sourceAssemblies")

    closure = []
    visited = set()
    visiting = set()
    def visit(index):
        if index in visited:
            return
        if index in visiting:
            fail("CSHARP_RSP_REFERENCE_CYCLE", str(input_data), "Bee assembly graph contains a cycle")
        if index < 0 or index >= len(all_rows):
            fail("CSHARP_RSP_REFERENCE_INDEX", str(input_data), "Bee assembly reference index is invalid")
        visiting.add(index)
        row = all_rows[index]
        for dependency in row.get("References", []):
            if not isinstance(dependency, int):
                fail("CSHARP_RSP_REFERENCE_INDEX", row.get("Name", "?"), "Assembly reference index must be an integer")
            if source_filter is None or (0 <= dependency < len(all_rows) and all_rows[dependency].get("Name") in source_filter):
                visit(dependency)
        visiting.remove(index)
        visited.add(index)
        closure.append((index, row))
    visit(root_index)
    source_names = {row.get("Name") for _, row in closure}
    result = []
    package_lock = None
    package_lock_path = project / "Packages" / "packages-lock.json"
    package_lock_sha256 = None
    if package_lock_path.is_file():
        try:
            raw_lock = read(safe(package_lock_path))
            package_lock = load(raw_lock)
            package_lock_sha256 = sha(raw_lock)
        except Exception as error:
            fail("CSHARP_PACKAGE_LOCK_INVALID", str(package_lock_path), str(error))

    def resolve_source(raw):
        logical = Path(raw)
        if logical.is_absolute():
            source = safe(logical)
            if not source.is_file() or not source.is_relative_to(project):
                fail("CSHARP_SOURCE_INVALID", str(source), "Bee source must resolve to an existing source inside the project")
            return source, None
        source = safe(project / logical)
        if source.is_file():
            if not source.is_relative_to(project):
                fail("CSHARP_SOURCE_INVALID", str(source), "Bee source escapes project root")
            return source, None
        # Unity Bee records package sources as Packages/<package-id>/..., while
        # package contents are physically materialized in Library/PackageCache.
        # Resolve only from the exact lock entry and the package's own manifest.
        parts = logical.parts
        if len(parts) < 3 or parts[0] != "Packages" or package_lock is None:
            fail("CSHARP_SOURCE_INVALID", str(source), "Bee source is missing and has no locked Unity package mapping")
        package_id = parts[1]
        lock_entry = package_lock.get("dependencies", {}).get(package_id)
        if not isinstance(lock_entry, dict) or not isinstance(lock_entry.get("version"), str):
            fail("CSHARP_PACKAGE_SOURCE_UNRESOLVED", str(logical), "Package source has no exact packages-lock entry")
        cache_root = safe(project / "Library" / "PackageCache")
        if not cache_root.is_dir() or not cache_root.is_relative_to(project):
            fail("CSHARP_PACKAGE_CACHE_MISSING", str(cache_root), "Locked package source is absent from project PackageCache")
        candidates = []
        for package_dir in cache_root.iterdir():
            if not package_dir.is_dir() or (package_dir.name != package_id and not package_dir.name.startswith(package_id + "@")):
                continue
            package_json = package_dir / "package.json"
            if not package_json.is_file():
                continue
            try:
                package_bytes = read(safe(package_json))
                manifest = load(package_bytes, max_nodes=100_000, max_depth=32)
            except Exception:
                continue
            if manifest.get("name") == package_id and manifest.get("version") == lock_entry["version"]:
                candidates.append((package_dir, package_bytes))
        if len(candidates) != 1:
            fail("CSHARP_PACKAGE_SOURCE_AMBIGUOUS", str(logical),
                 "Expected exactly one PackageCache manifest matching locked package name and version; found " + str(len(candidates)))
        package_dir, package_bytes = candidates[0]
        if package_dir.is_symlink() or not safe(package_dir).is_relative_to(cache_root):
            fail("CSHARP_PACKAGE_SOURCE_UNSAFE", str(package_dir), "PackageCache source must remain a regular in-project directory")
        resolved = safe(package_dir.joinpath(*parts[2:]))
        if not resolved.is_file() or resolved.suffix.lower() != ".cs" or not resolved.is_relative_to(package_dir):
            fail("CSHARP_PACKAGE_SOURCE_MISSING", str(resolved), "Locked package does not contain the exact Bee-listed C# source")
        metadata = {"packageId": package_id, "version": lock_entry["version"],
                    "source": lock_entry.get("source"), "lockPath": str(package_lock_path),
                    "lockSha256": package_lock_sha256, "packageManifestPath": str(package_dir / "package.json"),
                    "packageManifestSha256": sha(package_bytes), "logicalPath": logical.as_posix(),
                    "resolvedPath": str(resolved)}
        return resolved, metadata

    for index, row in closure:
        name = row["Name"]
        options, response_path = get_rsp(name)
        row_project = Path(row.get("Path", ""))
        if str(row_project.resolve()) != str(project):
            fail("CSHARP_RSP_PROJECT_MISMATCH", name, "Assembly row identifies a different project root")
        defines = list(row.get("Defines", []))
        if set(defines) != set(options["defines"]):
            fail("CSHARP_RSP_DEFINE_MISMATCH", name, "Bee assembly defines differ from response-file defines")
        if row.get("LanguageVersion") != options["languageVersion"]:
            fail("CSHARP_RSP_LANGUAGE_MISMATCH", name, "Bee language version differs from response-file setting")
        refs = []
        dependencies = []
        excluded_dependencies = []
        for dependency_index in row.get("References", []):
            dep = all_rows[dependency_index]
            if dep["Name"] in source_names:
                dependencies.append(dep["Name"])
            elif source_filter is not None:
                excluded_dependencies.append(dep["Name"])
        for value in options["references"]:
            path = Path(value)
            if not path.is_absolute():
                path = project / path
            path = Path(os.path.normpath(path))
            ref_name = path.name
            source_stem = ref_name[:-4] if ref_name.lower().endswith(".dll") else ref_name
            if source_stem.endswith(".ref"):
                source_stem = source_stem[:-4]
            if source_stem in source_names:
                continue
            path = safe(path)
            if not path.is_file():
                fail("CSHARP_REFERENCE_MISSING", str(path), "Response-file metadata reference is missing")
            refs.append(str(path))
        reference_stems = set()
        for value in options["references"]:
            stem = Path(value).name
            if stem.lower().endswith(".dll"):
                stem = stem[:-4]
            if stem.endswith(".ref"):
                stem = stem[:-4]
            reference_stems.add(stem)
        unresolved_excluded = sorted(set(excluded_dependencies) - reference_stems)
        if unresolved_excluded:
            fail("CSHARP_RSP_FILTER_REFERENCE_MISSING", name,
                 "Excluded Bee source dependencies lack an exact response-file metadata reference: " + ", ".join(unresolved_excluded))
        sources = []
        package_sources = []
        for raw in row.get("SourceFiles", []):
            source, package_origin = resolve_source(raw)
            if source.suffix.lower() != ".cs":
                fail("CSHARP_SOURCE_INVALID", str(source), "Bee source must be an existing in-project .cs file")
            sources.append(str(source))
            if package_origin:
                package_sources.append(package_origin)
        if not sources:
            fail("CSHARP_SOURCE_SET_EMPTY", name, "Bee assembly has no explicit source files")
        analyzer_inputs = []
        for raw in options["analyzers"]:
            path = Path(raw)
            if not path.is_absolute():
                path = project / path
            path = safe(path)
            if not path.is_file() or path.suffix.lower() != ".dll":
                fail("CSHARP_ANALYZER_MISSING", str(path), "Exact response-file analyzer DLL is missing")
            analyzer_inputs.append({"path": str(path), "sha256": _sha_file(path)})
        additional_inputs = []
        for raw in options["additionalFiles"]:
            path = Path(raw)
            if not path.is_absolute():
                path = project / path
            path = safe(path)
            if not path.is_file():
                fail("CSHARP_ADDITIONAL_FILE_MISSING", str(path), "Exact response-file additional file is missing")
            additional_inputs.append({"path": str(path), "sha256": _sha_file(path)})
        result.append({"name": name, "sources": sorted(set(sources)), "references": sorted(set(refs)),
                       "projectReferences": sorted(set(dependencies)), "defines": sorted(set(defines)),
                       "languageVersion": options["languageVersion"], "nullable": options["nullable"],
                       "allowUnsafe": options["allowUnsafe"], "configuration": "Unity Bee response file",
                       "responseFile": str(response_path), "responseFileSha256": sha(read(response_path)),
                       "inputData": str(input_data), "inputDataSha256": sha(read(input_data)),
                       "logicalPackageSources": package_sources,
                       "sourceAssemblyFilter": sorted(source_filter) if source_filter is not None else None,
                       "excludedSourceDependencies": sorted(set(excluded_dependencies)),
                       "unsupportedCompilerOptions": options["unsupported"],
                       "analyzers": options["analyzers"], "additionalFiles": options["additionalFiles"],
                       "analyzerInputs": analyzer_inputs, "additionalFileInputs": additional_inputs})
    result.sort(key=lambda item: len(item["projectReferences"]))
    return result


def _parse_response_files(project, response_files, source_assemblies=None):
    if not isinstance(response_files, list) or not response_files:
        fail("CSHARP_RSP_INPUT_INVALID", "toolchain.responseFiles", "Provide one or more exact Unity response-file paths")
    merged = {}
    context_identity = None
    for response_file in response_files:
        rows = _parse_response_file(project, response_file, source_assemblies)
        for row in rows:
            identity = (row["inputData"], row["inputDataSha256"])
            if context_identity is None:
                context_identity = identity
            elif identity != context_identity:
                fail("CSHARP_RSP_CONTEXT_MIXED", str(response_file), "Response files must belong to the same Bee input-data snapshot")
            current = merged.get(row["name"])
            if current and (current["sources"] != row["sources"] or current["defines"] != row["defines"] or
                            current["responseFileSha256"] != row["responseFileSha256"]):
                fail("CSHARP_RSP_CONTEXT_MIXED", row["name"], "Assembly compile inputs differ across selected roots")
            merged[row["name"]] = row
    names = set(merged)
    if source_assemblies is not None:
        missing_source_roots = sorted(set(source_assemblies) - names)
        if missing_source_roots:
            fail("CSHARP_SOURCE_ASSEMBLY_FILTER_INCOMPLETE", str(project),
                 "Selected source assemblies are absent from response-file inputs: " + ", ".join(missing_source_roots))
    for row in merged.values():
        filtered = []
        for raw in row["references"]:
            stem = Path(raw).name
            stem = stem[:-4] if stem.lower().endswith(".dll") else stem
            if stem.endswith(".ref"):
                stem = stem[:-4]
            if stem not in names:
                filtered.append(raw)
        row["references"] = filtered
    return sorted(merged.values(), key=lambda item: item["name"])


def _explicit_context_files(values, project, label):
    if not isinstance(values, list):
        fail("CSHARP_COMPILE_INPUT_INVALID", f"toolchain.assemblies.{label}", f"{label} must be a list of exact files")
    result = []
    for value in values:
        raw_path = value.get("path") if isinstance(value, dict) else value
        path = Path(raw_path)
        if not path.is_absolute():
            path = project / path
        path = path.resolve()
        if not path.is_file():
            fail("CSHARP_CONTEXT_FILE_MISSING", str(path), f"Explicit {label} input must exist")
        actual_hash = _sha_file(path)
        expected = value.get("sha256") if isinstance(value, dict) else None
        if expected and expected != actual_hash:
            fail("CSHARP_CONTEXT_HASH_MISMATCH", str(path), f"Explicit {label} input changed")
        result.append({"path": str(path), "sha256": actual_hash})
    return result


def _compile_inputs(project, toolchain):
    response_files = toolchain.get("responseFiles") if isinstance(toolchain, dict) else None
    explicit = toolchain.get("assemblies") if isinstance(toolchain, dict) else None
    if response_files and explicit:
        fail("CSHARP_COMPILE_INPUT_AMBIGUOUS", "toolchain", "Choose responseFiles or explicit assemblies, not both")
    if response_files:
        rows = _parse_response_files(project, response_files, toolchain.get("sourceAssemblies"))
    elif explicit:
        rows = []
        for item in explicit:
            if not isinstance(item, dict) or not item.get("name") or not item.get("sourceFiles"):
                fail("CSHARP_COMPILE_INPUT_INVALID", "toolchain.assemblies", "Each assembly needs a name and explicit sourceFiles")
            sources = []
            for raw in item["sourceFiles"]:
                source = Path(raw)
                if not source.is_absolute():
                    source = project / source
                source = safe(source)
                if not source.is_relative_to(project) or not source.is_file() or source.suffix.lower() != ".cs":
                    fail("CSHARP_SOURCE_INVALID", str(source), "Source must be an existing .cs file inside project")
                sources.append(str(source))
            refs = []
            for raw in item.get("references", []):
                reference = Path(raw)
                if not reference.is_absolute():
                    reference = project / reference
                reference = reference.resolve()
                if not reference.is_file() or reference.suffix.lower() != ".dll":
                    fail("CSHARP_REFERENCE_MISSING", str(reference), "Explicit reference must be an existing DLL")
                refs.append(str(reference))
            nullable = item.get("nullable", "disable")
            if nullable not in ("enable", "disable", "annotations", "warnings"):
                fail("CSHARP_COMPILE_INPUT_INVALID", item["name"], "nullable must be enable, disable, annotations, or warnings")
            analyzer_inputs = _explicit_context_files(item.get("analyzers", []), project, "analyzers")
            additional_inputs = _explicit_context_files(item.get("additionalFiles", []), project, "additionalFiles")
            rows.append({"name": item["name"], "sources": sorted(set(sources)), "references": sorted(set(refs)),
                         "projectReferences": item.get("projectReferences", []), "defines": sorted(set(item.get("defines", []))),
                         "languageVersion": item.get("languageVersion", "default"),
                         "nullable": nullable, "allowUnsafe": bool(item.get("allowUnsafe", False)),
                         "analyzerInputs": analyzer_inputs, "additionalFileInputs": additional_inputs,
                         "configuration": item.get("configuration", "explicit")})
    else:
        projects = sorted(project.glob("*.csproj"))
        if not projects:
            fail("CSHARP_COMPILE_CONTEXT_MISSING", str(project), "No generated .csproj; provide toolchain.assemblies with exact compile inputs")
        rows = [_parse_csproj(project, path, projects) for path in projects]
    names = [row["name"] for row in rows]
    if len(names) != len(set(names)):
        fail("CSHARP_ASSEMBLY_AMBIGUOUS", str(project), "Compilation input contains duplicate assembly names")
    known = set(names)
    by_project_stem = {Path(row.get("projectFile", "")).stem: row["name"] for row in rows if row.get("projectFile")}
    for row in rows:
        retained = []
        for reference in row["references"]:
            source_name = by_project_stem.get(Path(reference).stem)
            if source_name and source_name != row["name"]:
                row["projectReferences"].append(source_name)
            else:
                retained.append(reference)
        row["references"] = retained
        row["projectReferences"] = sorted(set(row["projectReferences"]))
    for row in rows:
        missing = sorted(set(row.get("projectReferences", [])) - known)
        if missing:
            # A project reference that resolves to a non-source assembly must be listed as a metadata DLL.
            fail("CSHARP_PROJECT_REFERENCE_MISSING", row["name"], "Project references not included as source assemblies: " + ", ".join(missing))
    source_paths = sorted({Path(path) for row in rows for path in row["sources"]})
    if len(source_paths) > MAX_CSHARP_SOURCE_FILES:
        fail("CSHARP_SOURCE_BUDGET", str(project), "Source file count exceeds the bounded Roslyn input budget")
    total_bytes = 0
    for source in source_paths:
        size = source.stat().st_size
        if size > MAX_CSHARP_SOURCE_FILE_BYTES:
            fail("CSHARP_SOURCE_BUDGET", str(source), "One C# source file exceeds the bounded Roslyn input budget")
        total_bytes += size
        if total_bytes > MAX_CSHARP_SOURCE_TOTAL_BYTES:
            fail("CSHARP_SOURCE_BUDGET", str(project), "Aggregate C# source bytes exceed the bounded Roslyn input budget")
    return rows


def _build_helper(toolchain):
    from importlib.resources import files
    source = files("beaver").joinpath("targets", HELPER_NAME).read_text(encoding="utf-8")
    helper_hash = hashlib.sha256(source.encode("utf-8")).hexdigest()
    temp = tempfile.TemporaryDirectory(prefix="beaver-csharp-")
    root = Path(temp.name)
    (root / HELPER_NAME).write_text(source, encoding="utf-8")
    roslyn_dir = Path(toolchain["roslynDirectory"])
    csproj = f'''<Project Sdk="Microsoft.NET.Sdk">
  <PropertyGroup><OutputType>Exe</OutputType><TargetFramework>net8.0</TargetFramework><ImplicitUsings>disable</ImplicitUsings><Nullable>disable</Nullable><RestoreIgnoreFailedSources>true</RestoreIgnoreFailedSources></PropertyGroup>
  <ItemGroup>
    <Reference Include="Microsoft.CodeAnalysis"><HintPath>{roslyn_dir / 'Microsoft.CodeAnalysis.dll'}</HintPath><Private>true</Private></Reference>
    <Reference Include="Microsoft.CodeAnalysis.CSharp"><HintPath>{roslyn_dir / 'Microsoft.CodeAnalysis.CSharp.dll'}</HintPath><Private>true</Private></Reference>
  </ItemGroup>
</Project>
'''
    (root / "helper.csproj").write_text(csproj, encoding="utf-8")
    (root / "NuGet.Config").write_text("<?xml version=\"1.0\" encoding=\"utf-8\"?><configuration><packageSources><clear /></packageSources></configuration>", encoding="utf-8")
    try:
        restore = subprocess.run([toolchain["dotnet"], "restore", "helper.csproj", "--configfile", "NuGet.Config"],
                                 cwd=root, capture_output=True, text=True, timeout=90)
        if restore.returncode != 0:
            fail("CSHARP_HELPER_RESTORE_FAILED", str(root), _short_output(restore.stderr or restore.stdout))
        build = subprocess.run([toolchain["dotnet"], "build", "helper.csproj", "--no-restore", "-c", "Release"],
                               cwd=root, capture_output=True, text=True, timeout=120)
        if build.returncode != 0:
            fail("CSHARP_HELPER_BUILD_FAILED", str(root), _short_output(build.stderr or build.stdout))
        dll = root / "bin" / "Release" / "net8.0" / "helper.dll"
        if not dll.is_file():
            fail("CSHARP_HELPER_BUILD_FAILED", str(root), "dotnet build succeeded but helper.dll is missing")
        identity = dict(toolchain, helperSha256=helper_hash, helperBinarySha256=_sha_file(dll),
                        helperBuild="ephemeral offline .NET Release build",
                        distribution="helper source is Beaver package data; compiler and Roslyn DLLs are local toolchain only")
        return temp, dll, identity
    except Exception:
        temp.cleanup()
        raise


def _short_output(value):
    return "\n".join(value.splitlines()[-20:])[:6000]


def _run_semantic(project, mode, assemblies, toolchain, rename=None):
    temp, helper, identity = _build_helper(toolchain)
    try:
        request_path = Path(temp.name) / "request.json"
        response_path = Path(temp.name) / "response.json"
        source_rows = _semantic_input_rows(project, assemblies)
        request = {"mode": mode, "projectRoot": str(project), "assemblies": source_rows, "rename": rename}
        request_path.write_bytes(encoded(request))
        result = subprocess.run([toolchain["dotnet"], str(helper), str(request_path), str(response_path)],
                                cwd=temp.name, capture_output=True, text=True, timeout=180)
        if result.returncode != 0:
            fail("CSHARP_HELPER_RUN_FAILED", str(helper), _short_output(result.stderr or result.stdout))
        response = load(response_path.read_bytes(), max_nodes=1_000_000, max_depth=128)
        return response, identity
    finally:
        temp.cleanup()


def _semantic_input_rows(project, assemblies):
    source_rows = []
    for assembly in assemblies:
        files = []
        for source in assembly["sources"]:
            path = Path(source)
            files.append({"path": path.relative_to(project).as_posix(), "absolutePath": str(path), "sha256": _sha_file(path)})
        reference_hashes = {str(path): _sha_file(path) for path in map(Path, assembly.get("references", []))}
        for kind in ("analyzerInputs", "additionalFileInputs"):
            for item in assembly.get(kind, []):
                if _sha_file(item["path"]) != item["sha256"]:
                    fail("CSHARP_CONTEXT_INPUT_CHANGED", item["path"], "Analyzer/additional-file input changed after inventory")
        source_rows.append(dict(assembly, files=files, referenceHashes=reference_hashes,
                                analyzers=assembly.get("analyzerInputs", []),
                                additionalFiles=assembly.get("additionalFileInputs", [])))
    return source_rows


def _compile_context_rows(assemblies):
    return [{"name": row["name"], "sourceCount": len(row["sources"]),
             "defines": row.get("defines", []), "languageVersion": row.get("languageVersion"),
             "responseFile": row.get("responseFile"), "responseFileSha256": row.get("responseFileSha256"),
             "inputData": row.get("inputData"), "inputDataSha256": row.get("inputDataSha256"),
             "logicalPackageSources": row.get("logicalPackageSources", []),
             "sourceAssemblyFilter": row.get("sourceAssemblyFilter"),
             "excludedSourceDependencies": row.get("excludedSourceDependencies", []),
             "unsupportedCompilerOptions": row.get("unsupportedCompilerOptions", []),
             "analyzers": row.get("analyzerInputs", []), "additionalFiles": row.get("additionalFileInputs", [])}
            for row in assemblies]


def _incomplete_compile_context(assemblies):
    issues = []
    for row in assemblies:
        if row.get("unsupportedCompilerOptions"):
            issues.append({"code": "CSHARP_COMPILE_CONTEXT_INCOMPLETE", "assembly": row["name"],
                           "unsupportedCompilerOptions": row.get("unsupportedCompilerOptions", []),
                           "message": "One or more compiler options are outside the modeled Roslyn input contract."})
    return issues


def _write_outputs(out, files):
    for rel, content in files.items():
        target = out / rel
        if not target.resolve().is_relative_to(out):
            fail("UNSAFE_PATH", str(rel), "Artifact path escapes output")
        target.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, str):
            target.write_text(content, encoding="utf-8", newline="")
        else:
            target.write_bytes(content)


def inspect(project, output, toolchain=None):
    """Read-only syntax/semantic inventory; output is a fresh report directory."""
    project = safe(project)
    if not project.is_dir():
        fail("PROJECT_PATH", str(project), "Project must be a directory")
    output = new_output(output, inputs=(project,))
    try:
        compiler = _resolve_toolchain(toolchain)
        assemblies = _compile_inputs(project, toolchain or {})
        response, identity = _run_semantic(project, "inspect", assemblies, compiler)
        response["compileContext"] = _compile_context_rows(assemblies)
        response["unknownCompileInputs"] = _incomplete_compile_context(assemblies)
        if response.get("status") == "ok" and response["unknownCompileInputs"]:
            response["status"] = "partial"
        response["toolchain"] = identity
        response["contract"] = INSPECT_SCHEMA
        response["report"] = str(output / "report.json")
        response["reads"] = _semantic_read_rows(response)
        response["writes"] = [{"path": "report.json", "effect": "inspect-report"}]
        response.setdefault("unknownReferences", [])
        write_json(output / "report.json", response)
        return dict(status=response.get("status", "ok"), report=str(output / "report.json"),
                    contract=INSPECT_SCHEMA, assemblies=len(assemblies), toolchain=identity)
    except Exception as error:
        report = {"contract": INSPECT_SCHEMA, "status": "blocked", "project": str(project),
                  "diagnostics": [getattr(error, "as_dict", lambda: {"code": "CSHARP_INSPECT_FAILED", "location": str(project), "message": str(error), "status": "blocked"})()]}
        write_json(output / "report.json", report)
        return dict(status="blocked", report=str(output / "report.json"), contract=INSPECT_SCHEMA)


def rename_plan(project, spec, output, toolchain=None):
    """Resolve and validate one semantic rename; write candidate patches only."""
    project = safe(project)
    if not project.is_dir():
        fail("PROJECT_PATH", str(project), "Project must be a directory")
    request = _read_spec(spec)
    _validate_rename_spec(request)
    output = new_output(output, inputs=(project,))
    try:
        compiler = _resolve_toolchain(toolchain)
        assemblies = _compile_inputs(project, toolchain or {})
        incomplete = _incomplete_compile_context(assemblies)
        if incomplete:
            source_rows = _semantic_input_rows(project, assemblies)
            response = {"contract": "beaver-csharp-helper/1", "status": "blocked", "patches": [],
                        "diagnostics": incomplete, "unknownReferences": incomplete,
                        "assemblies": [{"name": row["name"],
                                        "sources": [{"path": file["path"], "sha256": file["sha256"]} for file in row["files"]],
                                        "referenceHashes": row["referenceHashes"]} for row in source_rows]}
            identity = compiler
        else:
            response, identity = _run_semantic(project, "rename", assemblies, compiler, request)
        response["compileContext"] = _compile_context_rows(assemblies)
        response["unknownCompileInputs"] = incomplete
        response["contract"] = PLAN_SCHEMA
        response["spec"] = request
        response["toolchain"] = identity
        response["actualProjectWrites"] = []
        response["yamlModified"] = False
        patches = response.pop("patches", [])
        artifacts = {}
        file_edits = []
        for patch in patches:
            relative = patch["targetPath"]
            if relative.startswith("/") or ".." in Path(relative).parts:
                fail("UNSAFE_PATH", relative, "Semantic patch target escaped project")
            if not relative.startswith("Assets/"):
                fail("CSHARP_SOURCE_NOT_ASSETS", relative, "Production source edits are limited to Assets paths")
            target = project / relative
            if relative != patch["sourcePath"] and target.exists():
                fail("CSHARP_RENAME_TARGET_EXISTS", relative, "Rename destination already exists; no candidate was written")
            if patch.get("metaTargetPath") and patch["metaTargetPath"] != patch.get("metaSourcePath") and (project / patch["metaTargetPath"]).exists():
                fail("CSHARP_RENAME_META_TARGET_EXISTS", patch["metaTargetPath"], "Meta destination already exists; GUID move would be ambiguous")
            source_abs = safe(project / patch["sourcePath"])
            target_abs = safe(target)
            artifacts["files/" + relative] = patch["content"]
            patch["sourceAbsolutePath"] = str(source_abs)
            patch["artifactPath"] = "files/" + relative
            file_edit = {"sourcePath": str(source_abs), "sourceAssetPath": patch["sourcePath"], "targetPath": relative,
                         "expectedSha256": patch["expectedSha256"], "newSha256": patch["afterSha256"],
                         "artifactPath": patch["artifactPath"]}
            if patch.get("preserveMetaGuid"):
                file_edit["nativeAssetMove"] = {"from": patch["sourcePath"], "to": relative,
                                                "expectedSourceSha256": patch["expectedSha256"],
                                                "metaGuidSha256": patch["metaGuidSha256"],
                                                "preserveMetaGuid": True}
            file_edits.append(file_edit)
            patch.pop("content", None)
        response["patches"] = patches
        response["reads"] = _semantic_read_rows(response)
        for patch in patches:
            if patch.get("metaSourcePath") and patch.get("metaGuidSha256"):
                response["reads"].append({"kind": "script-meta", "path": patch["metaSourcePath"], "sha256": patch["metaGuidSha256"]})
        response["writes"] = [dict(path="plan.json", effect="rename-plan-report")]
        response["writes"].extend(dict(path=x["targetPath"], effect="candidate-patch",
                                   moveFrom=x["sourcePath"] if x["sourcePath"] != x["targetPath"] else None,
                                   expectedSourceSha256=x["expectedSha256"], afterSha256=x["afterSha256"],
                                   nativeAssetMove=({"from": x["sourcePath"], "to": x["targetPath"],
                                                     "expectedMetaSha256": x.get("metaGuidSha256"),
                                                     "preserveMetaGuid": True} if x.get("preserveMetaGuid") else None)) for x in patches)
        response["fileEdits"] = file_edits
        response.setdefault("unknownReferences", [])
        response["report"] = str(output / "plan.json")
        _write_outputs(output, artifacts)
        write_json(output / "plan.json", response)
        return dict(status=response.get("status", "blocked"), report=str(output / "plan.json"),
                    contract=PLAN_SCHEMA, patches=len(patches), toolchain=identity)
    except Exception as error:
        report = {"contract": PLAN_SCHEMA, "status": "blocked", "project": str(project), "spec": request,
                  "actualProjectWrites": [], "yamlModified": False,
                  "diagnostics": [getattr(error, "as_dict", lambda: {"code": "CSHARP_RENAME_FAILED", "location": str(project), "message": str(error), "status": "blocked"})()]}
        write_json(output / "plan.json", report)
        return dict(status="blocked", report=str(output / "plan.json"), contract=PLAN_SCHEMA, patches=0)


def _validate_rename_spec(spec):
    if not isinstance(spec, dict):
        fail("CSHARP_RENAME_SPEC", "spec", "Rename spec must be a JSON object")
    kind = spec.get("kind")
    if kind not in ("type", "namespace", "serialized_field"):
        fail("CSHARP_RENAME_SPEC", "kind", "Supported kinds are type, namespace, and serialized_field")
    if not isinstance(spec.get("symbol"), str) or not spec["symbol"]:
        fail("CSHARP_RENAME_SPEC", "symbol", "Exact source symbol is required")
    if not isinstance(spec.get("newName"), str) or not spec["newName"]:
        fail("CSHARP_RENAME_SPEC", "newName", "Destination name is required")
    if not isinstance(spec.get("assembly"), str) or not spec["assembly"]:
        fail("CSHARP_RENAME_SPEC", "assembly", "Source assembly is required")
    if kind in ("type", "serialized_field") and not IDENTIFIER.fullmatch(spec["newName"]):
        fail("CSHARP_RENAME_SPEC", "newName", "Type and field names must be one C# identifier")
    if kind == "namespace" and not all(IDENTIFIER.fullmatch(part) for part in spec["newName"].split(".")):
        fail("CSHARP_RENAME_SPEC", "newName", "Namespace must be a dotted sequence of C# identifiers")
    if kind == "serialized_field" and not spec.get("containingType"):
        fail("CSHARP_RENAME_SPEC", "containingType", "Serialized field rename requires an exact containing type")


def _semantic_read_rows(response):
    rows = []
    for assembly in response.get("assemblies", []):
        for source in assembly.get("sources", assembly.get("sourceFiles", [])):
            if isinstance(source, dict):
                rows.append({"kind": "source", "path": source["path"], "sha256": source["sha256"]})
            else:
                rows.append({"kind": "source", "path": source})
        for path, digest in assembly.get("referenceHashes", {}).items():
            rows.append({"kind": "metadata-reference", "path": path, "sha256": digest})
        for item in assembly.get("analyzerInputs", []):
            rows.append({"kind": "analyzer", "path": item["path"], "sha256": item["sha256"]})
        for item in assembly.get("additionalFileInputs", []):
            rows.append({"kind": "additional-file", "path": item["path"], "sha256": item["sha256"]})
    return sorted(rows, key=lambda row: (row["kind"], row["path"]))


def module_generate(spec, output):
    """Generate a named five-layer module candidate, without writing project sources."""
    request = _read_spec(spec)
    profile = request.get("profile", UNITY_STANDARD_PROFILE)
    if profile not in (UNITY_STANDARD_PROFILE, LEGACY_MODULE_PROFILE):
        fail("CSHARP_MODULE_PROFILE_UNSUPPORTED", "profile", "Select a supported, versioned module profile")
    request["profile"] = profile
    required = ("name", "namespace", "assembly", "owner", "fields", "references")
    missing = [key for key in required if key not in request]
    if missing:
        fail("CSHARP_MODULE_SPEC", "spec", "Required module fields missing: " + ", ".join(missing))
    name, namespace, assembly, owner = (request[k] for k in ("name", "namespace", "assembly", "owner"))
    if not IDENTIFIER.fullmatch(name) or not QUALIFIED_IDENTIFIER.fullmatch(namespace):
        fail("CSHARP_MODULE_SPEC", "name/namespace", "Module and namespace must use valid C# identifiers")
    if not QUALIFIED_IDENTIFIER.fullmatch(assembly):
        fail("CSHARP_MODULE_SPEC", "assembly", "assembly must be a dotted sequence of valid identifiers")
    if not isinstance(owner, str) or not owner.strip():
        fail("CSHARP_MODULE_SPEC", "owner", "Generated files require an explicit owner")
    refs = request["references"]
    standard = profile == UNITY_STANDARD_PROFILE
    layers = ("Bootstrap", "Domain", "Gameplay", "Presentation", "Services") if standard else (
        "Domain", "Application", "Infrastructure", "Presentation", "Bootstrap")
    if not isinstance(refs, dict) or any(layer not in refs or not isinstance(refs[layer], list) for layer in layers):
        fail("CSHARP_MODULE_SPEC", "references", "Each of the five layers needs an explicit asmdef reference list")
    if set(refs) != set(layers):
        fail("CSHARP_MODULE_PROFILE_MIXED", "references", "Reference layer keys must match the selected profile exactly")
    standard_dependencies = {"Domain": set(), "Gameplay": {"Domain"}, "Services": {"Domain", "Gameplay"},
                             "Presentation": {"Domain", "Gameplay"}, "Bootstrap": {"Domain", "Gameplay", "Presentation", "Services"}}
    required_refs = ({"Application": {assembly + ".Domain"}, "Infrastructure": {assembly + ".Domain"},
                      "Bootstrap": {assembly + ".Application", assembly + ".Presentation"}} if not standard else {})
    allowed_internal = (standard_dependencies if standard else
                        {"Domain": set(), "Application": {"Domain"}, "Infrastructure": {"Domain"},
                         "Presentation": set(), "Bootstrap": {"Domain", "Application", "Infrastructure", "Presentation"}})
    for layer in layers:
        layer_refs = refs[layer]
        if any(not isinstance(item, str) or not item.strip() or any(ord(ch) < 32 for ch in item) for item in layer_refs):
            fail("CSHARP_MODULE_SPEC", "references." + layer, "Asmdef references must be explicit non-empty strings")
        if len(layer_refs) != len(set(layer_refs)):
            fail("CSHARP_MODULE_SPEC", "references." + layer, "Asmdef references must not repeat")
        if standard:
            own_layers = {assembly + "." + child: child for child in layers}
            internal = sorted(own_layers[item] for item in layer_refs if item in own_layers)
            if internal:
                fail("CSHARP_MODULE_PROFILE_MIXED", "references." + layer,
                     "Unity standard internal references are profile-owned; provide external references only")
            if layer in ("Domain", "Gameplay"):
                invalid = [item for item in layer_refs if not (item == "Contracts" or item.endswith(".Contracts"))]
                if invalid:
                    fail("CSHARP_MODULE_DEPENDENCY_DIRECTION", "references." + layer,
                         "Pure layers may reference only explicitly named Contracts assemblies: " + ", ".join(sorted(invalid)))
        missing_refs = required_refs.get(layer, set()) - set(layer_refs)
        if missing_refs:
            fail("CSHARP_MODULE_SPEC", "references." + layer, "Required internal layer references are missing: " + ", ".join(sorted(missing_refs)))
    if standard:
        # Internal edges are canonicalized by the profile, never hand-authored per module.
        generated = {layer: set(refs[layer]) for layer in layers}
        for layer, dependencies in standard_dependencies.items():
            generated[layer].update(assembly + "." + dependency for dependency in dependencies)
        request["references"] = {layer: sorted(values) for layer, values in generated.items()}
    fields = request["fields"]
    if not isinstance(fields, list):
        fail("CSHARP_MODULE_SPEC", "fields", "fields must be a list")
    field_names = []
    for row in fields:
        if not isinstance(row, dict) or not IDENTIFIER.fullmatch(str(row.get("name", ""))) or row.get("type") not in PRIMITIVES:
            fail("CSHARP_MODULE_FIELD", "fields", "Each config field needs a valid name and supported primitive type")
        field_names.append(row["name"])
    if len(field_names) != len(set(field_names)):
        fail("CSHARP_MODULE_FIELD", "fields", "Config field names must be unique")
    view_bindings = request.get("viewBindings", [])
    binding_names = []
    for row in view_bindings:
        if not isinstance(row, dict) or not IDENTIFIER.fullmatch(str(row.get("name", ""))) or not isinstance(row.get("type"), str) or not VIEW_TYPE.fullmatch(row["type"]):
            fail("CSHARP_MODULE_BINDING", "viewBindings", "View bindings need a valid name and explicit type")
        binding_names.append(row["name"])
    if len(binding_names) != len(set(binding_names)):
        fail("CSHARP_MODULE_BINDING", "viewBindings", "View binding names must be unique")
    files = _module_files(request, layers)
    baseline = request.get("baselinePath")
    prior_owned = set(request.get("previousOwnedFiles", []))
    prior_owner = request.get("previousOwner")
    if prior_owned and prior_owner != owner:
        fail("CSHARP_MODULE_OWNER_MISMATCH", "previousOwner", "Existing generated paths require explicit matching prior ownership")
    diff = {"added": sorted(files), "changed": [], "unchanged": [], "unowned": [], "retiredOwned": []}
    if baseline:
        baseline = safe(baseline)
        output_path = Path(output).absolute()
        if not baseline.is_dir() or baseline == output_path or baseline.is_relative_to(output_path) or output_path.is_relative_to(baseline):
            fail("CSHARP_MODULE_BASELINE", str(baseline), "Baseline must be a separate existing directory")
        for rel in sorted(files):
            old = baseline / rel
            if old.is_file():
                if rel not in prior_owned:
                    fail("CSHARP_MODULE_UNOWNED_COLLISION", rel, "Generated path already exists outside this module owner's manifest")
                before = read(old)
                if before == files[rel].encode("utf-8"):
                    diff["unchanged"].append(rel)
                    diff["added"].remove(rel)
                else:
                    diff["changed"].append(rel)
                    diff["added"].remove(rel)
        current = {p.relative_to(baseline).as_posix() for p in baseline.rglob("*") if p.is_file()}
        diff["unowned"] = sorted(current - prior_owned - set(files))
        diff["retiredOwned"] = sorted((prior_owned & current) - set(files))
    output = new_output(output, inputs=(baseline,) if baseline else ())
    _write_outputs(output, files)
    manifest = {"schema": MODULE_SCHEMA, "templateVersion": profile, "profile": profile,
                "profileVersion": "1.0.0", "module": name,
                "namespace": namespace, "assembly": assembly, "owner": owner,
                "layers": list(layers), "asmdefReferences": request["references"],
                "codeRoot": request.get("codeRoot", "Assets/Game"), "layerMap": request.get("layerMap", {layer: layer for layer in layers}),
                "files": {rel: sha(content.encode("utf-8")) for rel, content in sorted(files.items())},
                "ownership": {"owner": owner, "ownedPaths": sorted(files), "retireRequiresExplicitOwner": True},
                "diff": diff,
                "reads": ([{"kind": "baseline-file", "path": rel, "sha256": _sha_file(baseline / rel)}
                           for rel in sorted(set(diff["changed"] + diff["unchanged"]))] if baseline else []),
                "writes": ([{"path": rel, "effect": "candidate-file", "sha256": digest}
                            for rel, digest in sorted({rel: sha(content.encode("utf-8")) for rel, content in files.items()}.items())] +
                           [{"path": "manifest.json", "effect": "module-manifest"}]),
                "unknownReferences": [], "actualProjectWrites": [], "needsUnityImport": True,
                "limitations": ["Candidate generation only; no project install or Unity import was performed.",
                                "Config reader uses UTF-8 TextAsset JSON as its sole numeric/config source.",
                                "No gameplay callbacks or placeholder business rules are generated."]}
    write_json(output / "manifest.json", manifest)
    return dict(status="ok", report=str(output / "manifest.json"), contract=MODULE_SCHEMA,
                files=len(files), owner=owner)


def _module_files(spec, layers):
    name, namespace, assembly = spec["name"], spec["namespace"], spec["assembly"]
    code_root = spec.get("codeRoot", "Assets/Game")
    root_parts = PurePosixPath(code_root).parts if isinstance(code_root, str) else ()
    if not root_parts or root_parts[0] != "Assets" or any(part in ("", ".", "..") or not IDENTIFIER.fullmatch(part) for part in root_parts):
        fail("CSHARP_MODULE_SPEC", "codeRoot", "codeRoot must be a safe path below Assets")
    layer_map = spec.get("layerMap", {layer: layer for layer in layers})
    if not isinstance(layer_map, dict) or set(layer_map) != set(layers):
        fail("CSHARP_MODULE_SPEC", "layerMap", "layerMap must map each of the five logical layers exactly once")
    layer_paths = {}
    for layer, mapped in layer_map.items():
        parts = PurePosixPath(mapped).parts if isinstance(mapped, str) else ()
        if not parts or any(part in ("", ".", "..") or not IDENTIFIER.fullmatch(part) for part in parts):
            fail("CSHARP_MODULE_SPEC", "layerMap." + layer, "Layer mappings must be safe relative identifier paths")
        layer_paths[layer] = "/".join(parts)
    if len(set(layer_paths.values())) != len(layer_paths):
        fail("CSHARP_MODULE_SPEC", "layerMap", "Each logical layer must map to a distinct directory")
    code_root = "/".join(root_parts)
    refs = spec["references"]
    files = {}
    standard = spec.get("profile") == UNITY_STANDARD_PROFILE
    logical_namespaces = ({layer: f"{namespace}.{layer}.{name}" for layer in layers} if standard else
                          {layer: f"{namespace}.{layer}" for layer in layers})
    for layer in layers:
        layer_assembly = assembly + "." + layer
        references = refs[layer]
        if any(not isinstance(item, str) or not item for item in references):
            fail("CSHARP_MODULE_SPEC", "references." + layer, "Asmdef references must be non-empty strings")
        asmdef = {"name": layer_assembly, "rootNamespace": logical_namespaces[layer],
                  "references": references, "autoReferenced": True, "overrideReferences": False,
                  "noEngineReferences": standard and layer in ("Domain", "Gameplay")}
        files[f"{code_root}/{layer_paths[layer]}/{name}/{layer_assembly}.asmdef"] = json.dumps(asmdef, ensure_ascii=False, indent=2) + "\n"
    domain_ns = logical_namespaces["Domain"]
    fields = "\n".join(f"        public {row['type']} {row['name']};" for row in spec["fields"])
    if fields:
        fields = "\n" + fields + "\n"
    data_name = name + "Data"
    files[f"{code_root}/{layer_paths['Domain']}/{name}/{data_name}.cs"] = (
        "using System;\n\nnamespace " + domain_ns + "\n{\n    [Serializable]\n    public sealed class " + data_name + "\n    {" + fields + "    }\n}\n")
    infra_layer = "Services" if standard else "Infrastructure"
    infra_ns = logical_namespaces[infra_layer]
    reader = name + "JsonReader"
    strict_members = "\n".join(
        f'                case "{row["name"]}": Validate{row["type"].capitalize()}(value, "{row["name"]}"); break;'
        for row in spec["fields"])
    strict_reader = f'''using System;
using System.Collections.Generic;
using System.Globalization;
using System.Text;
using UnityEngine;
using {domain_ns};

namespace {infra_ns}
{{
    public static class {reader}
    {{
        public static {data_name} Read(TextAsset source)
        {{
            if (source == null) throw new ArgumentNullException(nameof(source));
            ValidateFlatObject(source.text);
            var data = JsonUtility.FromJson<{data_name}>(source.text);
            if (data == null) throw new InvalidOperationException("Configuration JSON produced no data.");
            return data;
        }}

        // Fields are emitted from the same module spec as the DTO, so validation cannot drift
        // into a second editable numeric table.
        static void ValidateFlatObject(string json)
        {{
            if (json == null) throw new ArgumentNullException(nameof(json));
            int i = 0; SkipWhitespace(json, ref i); Require(json, ref i, '{{');
            var seen = new HashSet<string>(StringComparer.Ordinal); SkipWhitespace(json, ref i);
            if (Peek(json, i) != '}}')
            {{
                while (true)
                {{
                    SkipWhitespace(json, ref i); string key = ReadString(json, ref i);
                    if (!seen.Add(key)) throw Invalid("duplicate property: " + key);
                    SkipWhitespace(json, ref i); Require(json, ref i, ':'); SkipWhitespace(json, ref i);
                    string value = ReadScalar(json, ref i);
                    switch (key)
                    {{
{strict_members}
                        default: throw Invalid("unknown property: " + key);
                    }}
                    SkipWhitespace(json, ref i); char separator = Peek(json, i++);
                    if (separator == '}}') break;
                    if (separator != ',') throw Invalid("expected ',' or '}}'");
                }}
            }}
            else i++;
            SkipWhitespace(json, ref i); if (i != json.Length) throw Invalid("trailing content");
            string[] required = new string[] {{ {", ".join(json.dumps(row["name"]) for row in spec["fields"])} }};
            foreach (var key in required) if (!seen.Contains(key)) throw Invalid("missing property: " + key);
        }}
        static char Peek(string text, int index) => index < text.Length ? text[index] : '\\0';
        static void SkipWhitespace(string text, ref int i) {{ while (i < text.Length && char.IsWhiteSpace(text[i])) i++; }}
        static void Require(string text, ref int i, char expected) {{ if (Peek(text, i++) != expected) throw Invalid("expected '" + expected + "'"); }}
        static string ReadString(string text, ref int i)
        {{
            int start = i; Require(text, ref i, '"'); bool escaped = false;
            while (i < text.Length)
            {{
                char c = text[i++];
                if (escaped) {{ if (!(c == '"' || c == '\\\\' || c == '/' || c == 'b' || c == 'f' || c == 'n' || c == 'r' || c == 't' || c == 'u')) throw Invalid("invalid string escape"); escaped = false; }}
                else if (c == '\\\\') escaped = true;
                else if (c == '"')
                {{
                    string wrapper = "{{\\"value\\":" + text.Substring(start, i - start) + "}}";
                    var decoded = JsonUtility.FromJson<StringValue>(wrapper);
                    if (decoded == null || decoded.value == null) throw Invalid("invalid string");
                    return decoded.value;
                }}
                else if (c < 0x20) throw Invalid("control character in string");
            }}
            throw Invalid("unterminated string");
        }}
        static string ReadScalar(string text, ref int i)
        {{
            if (Peek(text, i) == '"') {{ int start = i; ReadString(text, ref i); return text.Substring(start, i - start); }}
            int first = i; while (i < text.Length && text[i] != ',' && text[i] != '}}' && !char.IsWhiteSpace(text[i])) i++;
            if (first == i) throw Invalid("missing scalar value");
            string value = text.Substring(first, i - first);
            if (value == "true" || value == "false" || System.Text.RegularExpressions.Regex.IsMatch(value, @"^-?(0|[1-9][0-9]*)(\\.[0-9]+)?([eE][+-]?[0-9]+)?$")) return value;
            throw Invalid("only scalar JSON values are supported");
        }}
        static void ValidateString(string value, string field) {{ if (value.Length == 0 || value[0] != '"') throw Invalid("string required: " + field); }}
        static void ValidateBool(string value, string field) {{ if (value != "true" && value != "false") throw Invalid("boolean required: " + field); }}
        static void ValidateInt(string value, string field) {{ if (!int.TryParse(value, NumberStyles.AllowLeadingSign, CultureInfo.InvariantCulture, out _)) throw Invalid("integer required: " + field); }}
        static void ValidateLong(string value, string field) {{ if (!long.TryParse(value, NumberStyles.AllowLeadingSign, CultureInfo.InvariantCulture, out _)) throw Invalid("integer required: " + field); }}
        static void ValidateFloat(string value, string field) {{ if (!double.TryParse(value, NumberStyles.Float, CultureInfo.InvariantCulture, out var parsed) || double.IsNaN(parsed) || double.IsInfinity(parsed) || parsed > float.MaxValue || parsed < -float.MaxValue) throw Invalid("finite float required: " + field); }}
        static InvalidOperationException Invalid(string reason) => new InvalidOperationException("Invalid configuration JSON: " + reason);
        [Serializable] sealed class StringValue {{ public string value; }}
    }}
}}
'''
    files[f"{code_root}/{layer_paths[infra_layer]}/{name}/{reader}.cs"] = strict_reader
    presentation_ns = logical_namespaces["Presentation"]
    binding_fields = []
    for row in spec.get("viewBindings", []):
        binding_fields.append(f"        [SerializeField] private {row['type']} {row['name']};")
    validate = ["            var errors = new System.Collections.Generic.List<string>();"]
    validate.extend(f"            if ({row['name']} == null) errors.Add(\"{row['name']}\");" for row in spec.get("viewBindings", []))
    validate.extend(["            return errors;", ""])
    files[f"{code_root}/{layer_paths['Presentation']}/{name}/{name}View.cs"] = (
        "using UnityEngine;\n\nnamespace " + presentation_ns + "\n{\n    public sealed class " + name + "View : MonoBehaviour\n    {\n"+
        ("\n".join(binding_fields) + "\n" if binding_fields else "")+
        "        public System.Collections.Generic.IReadOnlyList<string> ValidateBindings()\n        {\n" +
        "\n".join(validate) + "        }\n    }\n}\n")
    application_layer = "Gameplay" if standard else "Application"
    application_ns = logical_namespaces[application_layer]
    context = name + ("GameplayContext" if standard else "Context")
    files[f"{code_root}/{layer_paths[application_layer]}/{name}/{context}.cs"] = (
        "using System;\nusing " + domain_ns + ";\n\nnamespace " + application_ns + "\n{\n    public sealed class " + context +
        "\n    {\n        public " + data_name + " Data { get; }\n\n        public " + context + "(" + data_name + " data)\n        {\n"+
        "            Data = data ?? throw new ArgumentNullException(nameof(data));\n        }\n    }\n}\n")
    bootstrap_ns = logical_namespaces["Bootstrap"]
    view_name = name + "View"
    files[f"{code_root}/{layer_paths['Bootstrap']}/{name}/{name}Module.cs"] = (
        "using System;\nusing " + application_ns + ";\nusing " + presentation_ns + ";\n\nnamespace " + bootstrap_ns +
        "\n{\n    public static class " + name + "Module\n    {\n        public static " + view_name + " Bind(" + context + " context, " + view_name + " view)\n        {\n"+
        "            if (context == null) throw new ArgumentNullException(nameof(context));\n"+
        "            if (view == null) throw new ArgumentNullException(nameof(view));\n"+
        "            var errors = view.ValidateBindings();\n"+
        "            if (errors.Count != 0) throw new InvalidOperationException(\"Missing view bindings: \" + string.Join(\", \", errors));\n"+
        "            return view;\n        }\n    }\n}\n")
    return files


def rename_sequence_plan(project, specs, output, toolchain=None):
    """Apply ordered Roslyn rename candidates to an isolated source snapshot."""
    project = safe(project)
    if not project.is_dir() or not isinstance(specs, list) or not specs:
        fail("CSHARP_SEQUENCE_INPUT_INVALID", str(project), "Provide a project directory and an ordered non-empty specs list")
    requests = [_read_spec(spec) for spec in specs]
    for request in requests:
        _validate_rename_spec(request)
    output = new_output(output, inputs=(project,))
    snapshot = output / "_source-snapshot"
    try:
        compiler = _resolve_toolchain(toolchain)
        rows = _compile_inputs(project, toolchain or {})
        incomplete = _incomplete_compile_context(rows)
        if incomplete:
            report = {"contract": SEQUENCE_SCHEMA, "status": "blocked", "project": str(project),
                      "diagnostics": incomplete, "stages": [], "fileEdits": [], "actualProjectWrites": []}
            write_json(output / "plan.json", report)
            return {"status": "blocked", "report": str(output / "plan.json"), "contract": SEQUENCE_SCHEMA, "stages": 0}
        snapshot.mkdir()
        baseline = {}
        provenance = {}
        explicit = []
        for row in rows:
            names = []
            for raw in row["sources"]:
                source = Path(raw)
                if not source.is_relative_to(project):
                    fail("CSHARP_SEQUENCE_EXTERNAL_SOURCE", str(source), "Sequence requires project-owned sources")
                relative = source.relative_to(project).as_posix()
                target = snapshot / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)
                names.append(relative)
                if relative.startswith("Assets/"):
                    baseline[relative] = {"path": str(source), "sha256": _sha_file(source)}
                    provenance[relative] = relative
                meta = Path(str(source) + ".meta")
                if meta.is_file():
                    meta_target = Path(str(target) + ".meta")
                    meta_target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(meta, meta_target)
            explicit.append({"name": row["name"], "sourceFiles": names, "references": row["references"],
                             "projectReferences": row["projectReferences"], "defines": row["defines"],
                             "languageVersion": row["languageVersion"], "nullable": row.get("nullable", "disable"),
                             "allowUnsafe": row.get("allowUnsafe", False), "analyzers": row.get("analyzerInputs", []),
                             "additionalFiles": row.get("additionalFileInputs", []), "configuration": "ordered candidate snapshot"})
        stage_toolchain = dict(toolchain or {})
        stage_toolchain.pop("responseFiles", None)
        stage_toolchain.pop("sourceAssemblies", None)
        stage_toolchain["assemblies"] = explicit
        stages = []
        final_destinations = {}
        for index, request in enumerate(requests, 1):
            stage_dir = output / f"stage-{index:02d}"
            result = rename_plan(snapshot, request, stage_dir, stage_toolchain)
            stage = load(read(Path(result["report"])))
            stages.append({"index": index, "spec": request, "status": stage.get("status"),
                           "report": Path(result["report"]).relative_to(output).as_posix(),
                           "diagnostics": stage.get("diagnostics", []),
                           "unknownReferences": stage.get("unknownReferences", []),
                           "sourceHashes": [{"path": edit.get("sourceAssetPath"), "sha256": edit.get("expectedSha256")}
                                            for edit in stage.get("fileEdits", [])]})
            if stage.get("status") != "ok":
                report = {"contract": SEQUENCE_SCHEMA, "status": "blocked", "project": str(project),
                          "toolchain": compiler, "stages": stages, "fileEdits": [], "actualProjectWrites": [],
                          "reads": [{"path": value["path"], "sha256": value["sha256"]} for value in baseline.values()]}
                write_json(output / "plan.json", report)
                return {"status": "blocked", "report": str(output / "plan.json"), "contract": SEQUENCE_SCHEMA, "stages": index}
            files_root = stage_dir / "files"
            for edit in stage.get("fileEdits", []):
                source_rel, target_rel = edit["sourceAssetPath"], edit["targetPath"]
                current = snapshot / source_rel
                if not current.is_file() or _sha_file(current) != edit["expectedSha256"]:
                    fail("CSHARP_SEQUENCE_SOURCE_HASH_MISMATCH", source_rel, "Snapshot stage input differs from its plan hash")
                origin = provenance.get(source_rel, source_rel)
                artifact_rel = edit["artifactPath"].removeprefix("files/")
                artifact = files_root / artifact_rel
                if not artifact.is_file() or _sha_file(artifact) != edit["newSha256"]:
                    fail("CSHARP_SEQUENCE_PATCH_HASH_MISMATCH", artifact_rel, "Roslyn stage artifact differs from its plan hash")
                target = snapshot / target_rel
                if target_rel != source_rel and target.exists():
                    fail("CSHARP_SEQUENCE_DESTINATION_COLLISION", target_rel, "Snapshot destination already exists")
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(artifact, target)
                current_meta, target_meta = Path(str(current) + ".meta"), Path(str(target) + ".meta")
                if target_rel != source_rel:
                    current.unlink()
                    if current_meta.exists():
                        current_meta.rename(target_meta)
                    provenance.pop(source_rel, None)
                    provenance[target_rel] = origin
                final_destinations[origin] = target_rel
        combined = []
        artifact_root = output / "files"
        for origin, target_rel in sorted(final_destinations.items()):
            original = baseline.get(origin)
            target = snapshot / target_rel
            if original is None or not target.is_file():
                fail("CSHARP_SEQUENCE_FINAL_SOURCE_MISSING", target_rel, "Could not map final candidate to an original source")
            artifact = artifact_root / target_rel
            artifact.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(target, artifact)
            combined.append({"sourcePath": original["path"], "sourceAssetPath": origin, "targetPath": target_rel,
                             "expectedSha256": original["sha256"], "newSha256": _sha_file(target),
                             "artifactPath": str(artifact.relative_to(output)),
                             "nativeAssetMove": ({"from": origin, "to": target_rel, "preserveMetaGuid": True}
                                                 if origin != target_rel else None)})
        report = {"contract": SEQUENCE_SCHEMA, "status": "ok", "project": str(project), "toolchain": compiler,
                  "stages": stages, "fileEdits": combined, "actualProjectWrites": [], "yamlModified": False,
                  "reads": [{"path": value["path"], "sha256": value["sha256"]} for value in baseline.values()]}
        write_json(output / "plan.json", report)
        return {"status": "ok", "report": str(output / "plan.json"), "contract": SEQUENCE_SCHEMA,
                "stages": len(stages), "fileEdits": len(combined)}
    except Exception as error:
        report = {"contract": SEQUENCE_SCHEMA, "status": "blocked", "project": str(project),
                  "diagnostics": [getattr(error, "as_dict", lambda: {"code": "CSHARP_SEQUENCE_FAILED", "location": str(project), "message": str(error), "status": "blocked"})()],
                  "stages": [], "fileEdits": [], "actualProjectWrites": []}
        write_json(output / "plan.json", report)
        return {"status": "blocked", "report": str(output / "plan.json"), "contract": SEQUENCE_SCHEMA, "stages": 0}
