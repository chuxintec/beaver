# C# project maintenance

The `beaver.csharp` module produces candidate artifacts only. It never edits the Unity project, its `.meta` files, or Unity YAML. Every operation writes into a new output directory and rejects an output that overlaps its project input. The production consumer must re-hash every source and adjacent `.meta` immediately before applying a selected candidate; the plan hashes document identity but do not make `stat` or an earlier read an authorization boundary.

## Interfaces

```python
inspect(project, output, toolchain=None)
rename_plan(project, spec, output, toolchain=None)
rename_sequence_plan(project, specs, output, toolchain=None)
module_generate(spec, output)
```

`inspect` reads generated root `.csproj` files by default, or accepts explicit compile inputs in `toolchain.assemblies`. It returns an inventory report at `report.json`, including assembly/source lists, defines, references, compiler diagnostics and a recorded local SDK/Roslyn identity. `rename_plan` accepts one exact symbol operation and writes `plan.json` plus candidate `.cs` files below `files/`. The report includes `fileEdits` with an absolute source path, project-relative `Assets/...` target path, expected old and new hashes, and candidate artifact path. For a Unity script filename move it also describes a native `asset.move` from old asset path to target so Unity retains the existing `.cs.meta` GUID; no `.meta` file is fabricated or written. A blocked plan contains diagnostics and no file edits. `rename_sequence_plan` applies each requested semantic rename to an isolated source snapshot and recompiles between stages; it emits per-stage evidence and one final unique `fileEdits` set whose `expectedSha256` values refer to the original project. It never installs candidates into the source project. `module_generate` writes a five-layer module candidate plus `manifest.json` with file hashes, ownership, and added/changed/unchanged/unowned diff rows.

Explicit source compile input example:

```json
{
  "assemblies": [
    {
      "name": "Assembly-CSharp",
      "sourceFiles": ["Assets/Scripts/Inventory.cs"],
      "references": ["Library/ScriptAssemblies/UnityEngine.CoreModule.dll"],
      "projectReferences": [],
      "defines": ["UNITY_EDITOR"],
      "languageVersion": "9.0",
      "nullable": "disable"
    }
  ]
}
```

For Unity Bee compilation, `toolchain.responseFiles` accepts one or more exact `.rsp` paths inside the selected project. The parser pairs each response file with its sibling Bee `<build-id>-inputdata.json`, verifies the assembly name, project root, defines, and language version, and takes its sources and indexed reference graph from `ScriptCompilationData.Assemblies`. It follows source-reference indices transitively and reads the listed response files for the selected roots and their dependencies. When a rename could affect callers outside the target's dependency closure, include those consumer assembly response files in the same `responseFiles` list. All selected response files must pair to the same input-data hash. A Bee logical `Packages/<id>/...` source is resolved only when the exact `packages-lock.json` entry and exactly one `Library/PackageCache` package manifest agree on package id and version; the logical and physical path, lock hash, and package-manifest hash are recorded. Missing or ambiguous sources and metadata references refuse the compile context.

`toolchain.sourceAssemblies` may further select an explicit allowlist of source assemblies from those exact response files. This is useful when all project source consumers must be bound while package source is represented by Unity's exact `.ref.dll` metadata references. Excluded source assemblies remain visible as dependencies and their direct metadata references must be present and hash-checked; the report does not claim to inspect excluded package source, its analyzers, or its dynamic reflection behavior. Without this explicit allowlist, discovered source assemblies remain included.

The helper executes exact RSP analyzer DLLs through Roslyn `AnalyzerFileReference`, runs source generators with the exact additional files, and records hashes for analyzer and additional-file inputs, analyzer/generator diagnostics, loaded analyzer/generator identities, and generated-source fingerprints for both the baseline and rename candidate. Analyzer load/runtime failure or generator failure blocks the operation. A matching string or unresolved identifier in generated output blocks a rename; generated files are never emitted as source patches. Unknown compiler options remain an explicit incomplete-context diagnostic and block rename.

Unity type/namespace migrations add `MovedFromAttribute` only when the candidate can preserve the source migration identity. When the target type or an affected Unity type already has `MovedFromAttribute` and the actual compilation metadata declares `AllowMultiple=false`, the automatic rename is refused with `MOVED_FROM_MIGRATION_CHAIN_UNSUPPORTED`; the existing migration attribute is never replaced or silently duplicated.

Paths are resolved relative to the selected project unless absolute. Project source files must be regular `.cs` files inside the project; locked Unity package sources must resolve inside that project's `Library/PackageCache`. Source symlinks and paths outside the project are refused. Roslyn inputs are bounded to 40,000 unique sources, 32 MiB per file, and 512 MiB total source bytes. `references` are exact existing DLL paths. `projectReferences` name other supplied source assemblies. Without an explicit list, a root `.csproj` is required. Unknown MSBuild expressions, unresolved references, missing source, or baseline compiler errors block semantic rename rather than falling back to text replacement. The local installed .NET SDK supplies Roslyn at runtime; no NuGet package is fetched and third-party compiler binaries are not included in Beaver outputs.

Rename specs use these fields:

```json
{"kind":"type","symbol":"Game.UI.InventoryPanel","newName":"InventoryView","assembly":"Assembly-CSharp"}
{"kind":"namespace","symbol":"Game.UI","newName":"Game.Presentation","assembly":"Assembly-CSharp"}
{"kind":"serialized_field","symbol":"items","containingType":"Game.InventoryPanel","newName":"entries","assembly":"Assembly-CSharp"}
```

Type, namespace, and field references are resolved with Roslyn symbols and rewritten only when bound to the selected symbol. A Unity `MonoBehaviour`/`ScriptableObject` type rename adds `MovedFromAttribute`; a field must be a supported public, `[SerializeField]`, or `[SerializeReference]` instance field and receives `FormerlySerializedAsAttribute`. A Unity script file whose basename matches the old type is emitted at the new filename, with the adjacent `.meta` GUID hash and planned meta move recorded. The candidate does not include or mutate a `.meta` file. Namespace moves add `MovedFromAttribute` to affected Unity object classes and leave `.cs` filenames unchanged. All selected source patches are compiled again with the same supplied context before a plan reports success.

The checked subset supports ordinary class/struct/interface/enum/delegate and namespace declarations, partial type declarations, using aliases, inheritance/type references, and serialized field references when Roslyn binds them in supplied source assemblies. A partial Unity type gets one migration attribute on the first source declaration. Nested declarations inside a namespace that is itself being moved retain their relative segment; a matching namespace declaration nested below an unrelated namespace is refused because changing it safely requires restructuring its container. Matching symbol strings, disabled conditional text, ambiguous or missing symbols, generic type rename, multi-variable serialized field declarations, destination collisions, and compile errors block automatic plans. A reflection wrapper parameter is treated as closed only when every source callsite in its compilation supplies a compile-time string that cannot name the selected symbol; otherwise dynamic reflection is unknown and blocks. Recognized asset-address APIs preserve their string keys. Comments and unrelated string content are preserved. Conditional compilation is evaluated only for the exact supplied define set; matching disabled text is conservatively reported. The plan does not migrate serialized Unity YAML or establish Player/runtime behavior. Inheritance is covered through semantic type references in the supplied compile graph, not through assemblies whose source was not supplied.

## Five-layer module template

`module_generate` defaults to the versioned Unity profile `grea-unity-project/1.0.0`; its `references` map is keyed by `Bootstrap`, `Domain`, `Gameplay`, `Presentation`, and `Services`. Those lists contain external references only. The profile supplies canonical internal asmdef edges: Gameplay→Domain; Services→Gameplay+Domain; Presentation→Gameplay+Domain; Bootstrap→all four runtime layers; Domain has no generated-layer or Unity-engine reference. It drives paths, namespaces, asmdef names, and `noEngineReferences` for pure layers together. Default paths are `Assets/Game/<Layer>/<Module>`, namespaces are `<namespaceRoot>.<Layer>.<Module>`, and assembly names are `<assemblyRoot>.<Layer>`. Optional `codeRoot` and `layerMap` only adapt physical placement; they do not change the profile's logical namespaces, assembly identities, or dependency graph. Use the explicit `beaver-legacy-five-layer/1.0.0` profile to reproduce the earlier `Domain/Application/Infrastructure/Presentation/Bootstrap` contract; its references and output are kept separate from the standard profile. A mixed layer/reference map is rejected. It creates module-owned asmdefs only; shared project asmdefs are not altered. Existing paths cannot be claimed or overwritten without an explicit matching prior owner and owned-path list; stale owned paths are reported for owner review, never deleted.

It generates a serializable JSON data class, a `TextAsset` reader that validates a flat JSON object for duplicate, unknown, missing, and primitive-type-invalid fields before calling `JsonUtility`, an explicitly serialized View with a `ValidateBindings()` entry point, a context wrapper in Gameplay (Application in the explicit legacy profile), and a `Bind(context, view)` bootstrap that validates references. The validator is generated from the same finite flat-field spec as the DTO; it does not maintain a second value table. For nested schemas, defaults, enums, or cross-row constraints, use the existing configuration-v3 pipeline instead of expanding this flat template. It generates no empty callbacks or gameplay rules. Generated output is still a candidate and needs Unity import, asmdef graph review, and consumer-side prefab binding. It does not write into the baseline or claim Unity compilation.

## Mechanism lineage and boundary

This module borrows the use of symbol-aware candidate migration and explicit old-to-new identity preservation from the ExampleGame tools `Tools/StandardTypeRename.cs` (SHA-256 `3ba24f36550de146c1e864b9ecb4fe9c5630994002464689cb404f5f039bb52a6`) and `Tools/StandardNamespaceMigration.cs` (SHA-256 `3f15f16b558a888292d6f05f544cdb8d361a7c58e77eb06278b907dc864c5ba6`). Those files were read-only references; no game constants or implementation files are copied. C# binding and candidate compilation are provided by local Roslyn. The tool does not claim complete Unity API migration, YAML identity migration, reflection discovery, or runtime equivalence.
