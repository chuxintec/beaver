# Static Unity reference graph

`beaver.references.inspect_graph(project, output, cache=None)` inventories Unity asset GUID links and a deliberately small subset of C# loading calls. `output` may be a directory (the report is `reference-graph.json`) or a report filename; the report file is create-only and an existing report is rejected. The optional cache is a separate JSON file outside the project and may be updated. Source files are read only.

The report uses `beaver-reference-graph/2` and contains `guidToPath`, direct `dependencies` (source path to referenced asset paths), `reverseDependencies`, `duplicateGuids`, `literalLoads`, `unknownDynamicLoads`, `unresolvedJsonStrings`, `counts`, and elapsed milliseconds. Duplicate GUIDs set status to `blocked`, are listed under `duplicateGuids`, and produce an ambiguous dependency instead of selecting one path. Unity YAML GUID references are recognized in common text asset types; `.meta` files provide GUID identity. C# scanning tokenizes enough syntax to skip comments and string contents, then recognizes only `Resources.Load` and `Addressables.LoadAssetAsync` calls whose first argument is one ordinary double-quoted string followed by `,` or `)`. Concatenation, verbatim strings, interpolation, and other expressions are reported as unknown dynamic loads. Literal spans are Python character offsets into the source file for a later explicitly authorized, hash-bound update.

Strings found in JSON are recorded as unresolved observations. Their presence does not establish that a value is an asset path, nor that the runtime consumes that configuration. External formats and semantics (including Spine and BMFont) are intentionally outside this graph.

The cache identity includes schema `beaver-reference-cache/2`, canonical project root, and implementation schema `reference-graph/3`. Entries are reused when size, modification time, change time, and inode match; `counts.stat`, `counts.cacheHit`, `counts.hash`, and `counts.parse` distinguish the work performed. `counts.sourceBytes` inventories all file sizes, including skipped binary files; it is not a read-memory budget. The scanner limits file count and the size of any text file it reads, and reports skipped binary and oversized files. Cache metadata is only a performance hint: timestamps and inode are not a content-integrity boundary. Before any selected write, the owning transaction must re-hash the exact source and verify its precondition. Cache and report paths must be separate and outside the source project. Symlinks are rejected.

Focused module verification:

```sh
# Run from the beaver repository root.
PYTHONPATH=src python3 -m unittest discover -s tests -p 'test_references.py' -v
```
