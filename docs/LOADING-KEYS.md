# Exact C# resource loading key patches

`beaver.loading_keys.patch_script(raw_bytes, patches)` returns new source bytes in memory. It does not write files or establish a transaction. Each patch has exactly `start`, `end`, `before`, and `after`; offsets are Unicode code-point offsets in the UTF-8 source with any UTF-8 BOM excluded, matching `references.literalLoads.span`.

Every span must exactly match one `Resources.Load` or `Addressables.LoadAssetAsync` ordinary-string argument reported by `references._literal_loads`. The original token is decoded with `json.loads` and must equal the expected `before` key. This admits only the JSON-safe subset shared by ordinary C# strings and JSON; verbatim, interpolated, concatenated, dynamic, unsupported C# escapes, comments, and non-load literals are rejected. Patch input must be in ascending, non-overlapping source order, with unique spans. UTF-8 is strict. Replacement strings are emitted as JSON-safe ordinary C# string tokens. Other source bytes, line endings, and a leading BOM are preserved.

The owning transaction must hash and recheck the original file, apply the returned bytes before launching Unity Editor, compile/read back, and recover through its journal. This helper does not write into a project and does not authorize an Editor launch or source installation.

Focused verification:

```sh
# Run from the beaver repository root.
PYTHONPATH=src python3 -m unittest discover -s tests -p 'test_loading_keys.py' -v
```
