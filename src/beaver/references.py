"""Bounded, static Unity reference inventory. This does not edit project files."""
from pathlib import Path
import hashlib
import json
import re
import time

from .errors import fail
from .io import safe, read, encoded, write_json

SCHEMA = "beaver-reference-cache/2"
REPORT_SCHEMA = "beaver-reference-graph/2"
MAX_FILES = 250_000
MAX_TEXT_BYTES = 16 * 1024 * 1024
MAX_TOTAL_BYTES = 2 * 1024 * 1024 * 1024
GUID_RE = re.compile(rb"\bguid:\s*([0-9a-fA-F]{32})\b")
YAML_GUID_RE = re.compile(rb"\bguid:\s*([0-9a-fA-F]{32})\b")
YAML_SUFFIXES = {".prefab", ".unity", ".asset", ".mat", ".controller", ".overridecontroller", ".anim", ".mask", ".rendertexture"}
TEXT_SUFFIXES = YAML_SUFFIXES | {".meta", ".cs", ".json"}
IDENTIFIER_RE = re.compile(r"[A-Za-z_$][\w$]*")


def _stat_key(path):
    st = path.stat(follow_symlinks=False)
    return {"size": st.st_size, "mtimeNs": st.st_mtime_ns,
            "ctimeNs": st.st_ctime_ns, "inode": st.st_ino}


def _read_text(path):
    raw = read(path, MAX_TEXT_BYTES)
    if b"\x00" in raw:
        return None, raw
    try:
        return raw.decode("utf-8-sig"), raw
    except UnicodeDecodeError:
        return None, raw


def _csharp_tokens(source):
    """Tokenize enough C# to ignore comments/strings and retain source spans."""
    tokens = []
    i, n = 0, len(source)
    while i < n:
        if source[i].isspace():
            i += 1
            continue
        start = i
        if source.startswith("//", i):
            end = source.find("\n", i + 2)
            i = n if end < 0 else end + 1
            continue
        if source.startswith("/*", i):
            end = source.find("*/", i + 2)
            i = n if end < 0 else end + 2
            continue

        # Ordinary, verbatim, interpolated, and raw strings are opaque tokens.
        prefix = None
        for marker in ('$@"', '@$"', '$"', '@"', '"""', '"'):
            if source.startswith(marker, i):
                prefix = marker
                break
        if prefix is not None:
            if prefix == '"""':
                delim = '"""'
                end = source.find(delim, i + len(delim))
                i = n if end < 0 else end + len(delim)
                tokens.append(("opaque", source[start:i], start, i))
                continue
            verbatim = "@" in prefix
            interpolated = "$" in prefix
            quote = prefix[-1]
            i += len(prefix)
            body_start = i
            while i < n:
                if verbatim and source.startswith('""', i):
                    i += 2
                    continue
                if source[i] == quote:
                    if not verbatim:
                        slashes, j = 0, i - 1
                        while j >= body_start and source[j] == "\\":
                            slashes += 1
                            j -= 1
                        if slashes % 2:
                            i += 1
                            continue
                    i += 1
                    break
                if not verbatim and source[i] in "\r\n":
                    break
                i += 1
            kind = "string" if prefix == '"' and i <= n and source[i - 1:i] == '"' else "opaque"
            tokens.append((kind, source[start:i], start, i))
            continue

        if source[i] == "'":  # char literal; never scan its contents as code
            i += 1
            escaped = False
            while i < n:
                c = source[i]
                i += 1
                if c == "'" and not escaped:
                    break
                if c == "\\" and not escaped:
                    escaped = True
                else:
                    escaped = False
            tokens.append(("opaque", source[start:i], start, i))
            continue
        m = IDENTIFIER_RE.match(source, i)
        if m:
            i = m.end()
            tokens.append(("id", m.group(), start, i))
            continue
        i += 1
        tokens.append(("sym", source[start:i], start, i))
    return tokens


def _decode_csharp_string(token):
    body = token[1:-1]
    out, i = [], 0
    simple = {"\\": "\\", '"': '"', "'": "'", "0": "\0", "a": "\a", "b": "\b",
              "f": "\f", "n": "\n", "r": "\r", "t": "\t", "v": "\v"}
    while i < len(body):
        if body[i] != "\\":
            out.append(body[i]); i += 1; continue
        i += 1
        if i >= len(body): return None
        esc = body[i]; i += 1
        if esc in simple:
            out.append(simple[esc]); continue
        digits = 4 if esc == "u" else 8 if esc == "U" else None
        if digits:
            raw = body[i:i + digits]
            if len(raw) != digits or not re.fullmatch(r"[0-9a-fA-F]+", raw): return None
            try: out.append(chr(int(raw, 16)))
            except ValueError: return None
            i += digits; continue
        if esc == "x":
            m = re.match(r"[0-9a-fA-F]{1,4}", body[i:])
            if not m: return None
            out.append(chr(int(m.group(), 16))); i += len(m.group()); continue
        return None
    return "".join(out)


def _literal_loads(path, source):
    found, unknown = [], []
    tokens = _csharp_tokens(source)
    i = 0
    while i + 3 < len(tokens):
        if not (tokens[i][0] == "id" and tokens[i][1] in ("Resources", "Addressables") and
                tokens[i + 1][:2] == ("sym", ".") and tokens[i + 2][0] == "id" and
                tokens[i + 2][1] == ("Load" if tokens[i][1] == "Resources" else "LoadAssetAsync")):
            i += 1
            continue
        kind = "Resources.Load" if tokens[i][1] == "Resources" else "Addressables.LoadAssetAsync"
        j = i + 3
        if j < len(tokens) and tokens[j][:2] == ("sym", "<"):
            angle = 0
            while j < len(tokens):
                if tokens[j][:2] == ("sym", "<"): angle += 1
                elif tokens[j][:2] == ("sym", ">"): angle -= 1
                j += 1
                if angle == 0: break
        if j >= len(tokens) or tokens[j][:2] != ("sym", "("):
            i += 1
            continue
        arg = j + 1
        if arg >= len(tokens):
            i = j + 1
            continue
        start = tokens[arg][2]
        depth = 0
        end_index = arg
        while end_index < len(tokens):
            tok = tokens[end_index]
            if tok[:2] == ("sym", "(") or tok[:2] == ("sym", "["): depth += 1
            elif tok[:2] == ("sym", ")"):
                if depth == 0: break
                depth -= 1
            elif tok[:2] == ("sym", "]") and depth: depth -= 1
            elif tok[:2] == ("sym", ",") and depth == 0: break
            end_index += 1
        end = tokens[end_index][2] if end_index < len(tokens) else tokens[-1][3]
        expression = source[start:end].strip()
        terminal = tokens[end_index][:2] if end_index < len(tokens) else None
        exact_string = (tokens[arg][0] == "string" and end_index == arg + 1 and
                        terminal in (("sym", ","), ("sym", ")")))
        value = _decode_csharp_string(tokens[arg][1]) if exact_string else None
        row = {"file": path, "span": [tokens[arg][2], tokens[arg][3]] if exact_string else [start, end],
               "key": value if exact_string and value is not None else expression, "type": kind}
        (found if exact_string and value is not None else unknown).append(row)
        i = max(i + 1, end_index + 1)
    return found, unknown


def _walk(project):
    roots = [project / name for name in ("Assets", "Packages", "ProjectSettings")]
    files = []
    for root in roots:
        if not root.exists():
            continue
        root = safe(root)
        if not root.is_dir():
            continue
        for item in root.rglob("*"):
            # Reject symlink components before inspecting any candidate.
            path = safe(item)
            if not path.is_file():
                continue
            if len(files) >= MAX_FILES:
                fail("REFERENCE_BUDGET", str(path), "Project scan exceeds file-count budget")
            files.append(path)
    return sorted(files)


def inspect_graph(project, output, cache=None):
    """Create a static reference report; cache is optional and must be outside project."""
    started = time.perf_counter()
    project = safe(project)
    if not project.is_dir():
        fail("PROJECT_PATH", str(project), "Project must be a directory")
    output = safe(output)
    report_path = output / "reference-graph.json" if output.suffix == "" or output.is_dir() else output
    for candidate in (report_path, safe(cache) if cache else None):
        if candidate is not None and (candidate == project or candidate.is_relative_to(project) or project.is_relative_to(candidate)):
            fail("OUTPUT_OVERLAP", str(candidate), "Report and cache must be outside the source project")
    cache_path = safe(cache) if cache else None
    if cache_path and (cache_path == report_path or cache_path.is_relative_to(report_path) or report_path.is_relative_to(cache_path)):
        fail("OUTPUT_OVERLAP", str(cache_path), "Report and cache paths must be separate")
    if report_path.exists():
        fail("OUTPUT_EXISTS", str(report_path), "Report output is create-only")
    if cache_path and cache_path.exists() and not cache_path.is_file():
        fail("CACHE_PATH", str(cache_path), "Cache must be a file path")
    root_id = str(project)
    cached = {}
    if cache_path and cache_path.is_file():
        try:
            doc = json.loads(read(cache_path, 512 * 1024 * 1024).decode("utf-8"))
            if (doc.get("schema") == SCHEMA and doc.get("root") == root_id and
                    doc.get("implementation") == "reference-graph/3" and isinstance(doc.get("entries"), dict)):
                cached = doc["entries"]
        except (ValueError, UnicodeDecodeError):
            cached = {}

    entries = {}
    counts = {"stat": 0, "sourceBytes": 0, "textReadBytes": 0, "hash": 0, "parse": 0, "cacheHit": 0,
              "skippedBinary": 0, "skippedLarge": 0}
    guid_paths, edges, loads, unknown, json_strings = {}, {}, [], [], []
    files = _walk(project)
    for path in files:
        rel = path.relative_to(project).as_posix()
        counts["stat"] += 1
        key = _stat_key(path)
        counts["sourceBytes"] += key["size"]
        prior = cached.get(rel)
        if isinstance(prior, dict) and prior.get("stat") == key and isinstance(prior.get("sha256"), str):
            entry = prior
            counts["cacheHit"] += 1
        else:
            if path.suffix.lower() not in TEXT_SUFFIXES:
                counts["skippedBinary"] += 1
                continue
            if key["size"] > MAX_TEXT_BYTES:
                counts["skippedLarge"] += 1
                continue
            if counts['textReadBytes'] + key['size'] > MAX_TOTAL_BYTES:
                fail('REFERENCE_TEXT_BUDGET',rel,'Actual text reads exceed 2 GiB; use a narrower source snapshot')
            source, raw = _read_text(path)
            counts['textReadBytes'] += len(raw)
            if source is None:
                counts["skippedBinary"] += 1
                continue
            counts["hash"] += 1
            counts["parse"] += 1
            entry = {"stat": key, "sha256": hashlib.sha256(raw).hexdigest(), "guid": None,
                     "references": [], "loads": [], "unknown": [], "jsonStrings": []}
            if path.name.endswith(".meta"):
                m = GUID_RE.search(raw)
                if m: entry["guid"] = m.group(1).decode("ascii").lower()
            if path.suffix.lower() in YAML_SUFFIXES and source.startswith("%YAML"):
                entry["references"] = [m.group(1).decode("ascii").lower() for m in YAML_GUID_RE.finditer(raw)]
            if path.suffix.lower() == ".cs":
                entry["loads"], entry["unknown"] = _literal_loads(rel, source)
            if path.suffix.lower() == ".json":
                try:
                    value = json.loads(source)
                    stack = [value]
                    while stack:
                        node = stack.pop()
                        if isinstance(node, dict): stack.extend(node.values())
                        elif isinstance(node, list): stack.extend(node)
                        elif isinstance(node, str): entry["jsonStrings"].append(node)
                except ValueError:
                    pass
            entries[rel] = entry
        entries[rel] = entry
        if rel.endswith(".meta") and entry.get("guid"):
            asset_rel = rel[:-5]
            guid_paths.setdefault(entry["guid"], []).append(asset_rel)
        if entry.get("references"):
            edges[rel] = sorted({(guid_paths[guid][0] if len(guid_paths.get(guid, [])) == 1 else
                                  ("ambiguous:" if len(guid_paths.get(guid, [])) > 1 else "unresolved:") + guid)
                                 for guid in entry["references"]})
        loads.extend(entry.get("loads", []))
        unknown.extend(entry.get("unknown", []))
        json_strings.extend({"file": rel, "value": value, "status": "unresolved", "reason": "JSON string is not assumed to be an asset reference"}
                            for value in entry.get("jsonStrings", []))

    # Resolve GUID edges after indexing all metadata, including forward references.
    guids = {guid: paths[0] for guid, paths in guid_paths.items() if len(paths) == 1}
    duplicate_guids = {guid: sorted(paths) for guid, paths in guid_paths.items() if len(paths) > 1}
    for rel, entry in entries.items():
        if entry.get("references"):
            edges[rel] = sorted({(guid_paths[guid][0] if len(guid_paths.get(guid, [])) == 1 else
                                  ("ambiguous:" if len(guid_paths.get(guid, [])) > 1 else "unresolved:") + guid)
                                 for guid in entry["references"]})
    reverse = {}
    for source, targets in edges.items():
        for target in targets:
            if not target.startswith(("unresolved:", "ambiguous:")):
                reverse.setdefault(target, []).append(source)
    report_status = "blocked" if duplicate_guids else "partial" if unknown or any(
        t.startswith(("unresolved:", "ambiguous:")) for ts in edges.values() for t in ts) else "ok"
    report = {"schema": REPORT_SCHEMA, "status": report_status,
              "root": root_id, "guidToPath": dict(sorted(guids.items())), "duplicateGuids": duplicate_guids,
              "dependencies": dict(sorted(edges.items())),
              "reverseDependencies": {k: sorted(v) for k, v in sorted(reverse.items())},
              "literalLoads": loads, "unknownDynamicLoads": unknown, "unresolvedJsonStrings": json_strings,
              "counts": counts, "timingMs": round((time.perf_counter() - started) * 1000, 3),
              "limitations": ["Static Unity YAML GUID references and exact C# Resources.Load/Addressables.LoadAssetAsync string literals only.",
                             "Stat-based cache is a performance hint, not a safety boundary; selected writes must re-hash source immediately before the transaction."]}
    report_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with report_path.open("xb") as stream:
            stream.write(encoded(report))
    except FileExistsError:
        fail("OUTPUT_EXISTS", str(report_path), "Report output is create-only")
    if cache_path:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        write_json(cache_path, {"schema": SCHEMA, "root": root_id, "implementation": "reference-graph/3", "entries": entries})
    report["report"] = str(report_path)
    return report
