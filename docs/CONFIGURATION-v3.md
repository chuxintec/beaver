# Beaver configuration v3

`beaver.configuration_v3` validates one versioned JSON Schema contract and generates matching C# and Go DTOs plus strict readers. The product JSON remains the only maintained numeric data source; generated code contains types and validation rules, never copied rows or values.

## Schema contract

Schema documents use `schemaVersion: 2` and an object `root`. `id`, `rootName`, `namespace`, and `goPackage` name the contract and generated types. `definitions` can be referenced with local `#/definitions/<name>` references. Supported node types are `object`, `array`, `dictionary`, `string`, `integer`, `decimal`, `boolean`, and `enum`.

Objects list `properties`, `required`, and `additionalProperties` (`false`, `true`, or a value schema). Every non-required property must declare a default policy:

- `{"policy":"preserve"}` keeps absence distinct from null. Generated C# uses `OptionalValue<T>.HasValue`; Go uses `Optional[T].Present`.
- `{"policy":"null"}` inserts an explicit null after validation. The property must permit null.
- `{"policy":"value","value":...}` inserts and validates the declared schema default.

`nullable: true` permits explicit JSON `null`; it does not make a missing property optional. Arrays support `items`, `minItems`, and `maxItems`; dictionaries have string keys and a `values` schema. Numeric constraints include `minimum`, `maximum`, exclusive bounds, integer `bits` (32/64/128), and decimal `maxScale` (0–28). Enums declare a string or signed 64-bit integer `base` and a non-empty unique `values` list. Decimals are exact JSON numbers bounded to the common `System.Decimal` range and scale so the Go and C# readers cannot silently disagree.

Top-level `tables` declare `{name,path,id}`. `references` declare `{path,table}` plus optional `skip` sentinel values. Dotted paths expand arrays with `[]` and dictionary members with `*`. `resources` declare `{path,kind}` where `kind` is `assetPath` or `assetGuid`; project-aware validation checks safe Assets-relative paths and GUID uniqueness. Resource validation needs an explicit project root. Input parsing rejects duplicate keys, invalid UTF-8, non-finite numbers, trailing data, and inputs beyond the documented byte/node/depth bounds.

## Python API

```python
from beaver.configuration_v3 import validate, generate, compare

report = validate(schema, input_json_or_path, tables=None, project=None)
manifest = generate(schema, output_directory)
compatibility = compare(before_schema, after_schema,
                        before_input=None, after_input=None)
```

`validate` returns a JSON-safe `beaver-config-report/2` envelope with `status`, source SHA-256 when bytes/path were supplied, table counts, explicit defaults, and path-addressed diagnostics. It never returns the full input values. `compare` reports shape, enum, bounds, nullability, and relation-contract changes; it does not infer save migration safety or gameplay parity.

## Generated runtime API

C# output contains `ConfigurationDtos.g.cs` and `ConfigurationJsonRuntime.g.cs` in the schema namespace. The preferred reader takes the exact input and generated schema bytes:

```csharp
var configuration = ConfigurationJsonRuntime.Read<MainlineConfiguration>(
    inputJsonBytes, schemaJsonBytes, out ConfigSnapshot snapshot);
```

`ConfigSnapshot` hashes the raw input/schema bytes and includes `SchemaID`. The runtime rejects duplicate members, comments, trailing JSON, unknown fields, type/null/enum/range errors, unresolved declared references, and unsafe declared resource references. Failures are `ConfigValidationException{Code,Path}` (with JSON parsing failures mapped to `CONFIG_INVALID`). `ValidationOptions` supplies external table identities, a project root, and a caller-built GUID index when those checks are needed.

Go output contains `ConfigurationTypes.go` and `BeaverJsonRuntime.go` in `goPackage` (default `configv3`). The root alias is `RootType`:

```go
cfg, snapshot, err := configv3.ReadConfiguration[configv3.RootType](inputJSON, schemaJSON)
```

`ReadConfigurationWithOptions` adds `ValidationOptions{ProjectRoot, ExternalTables, AssetGUIDs}`. `Snapshot` exposes `InputSHA256`, `SchemaSHA256`, and `SchemaID`. Errors implement `error` as `*ValidationError{Code, Path, Message}`; malformed JSON is reported as `CONFIG_JSON` and field diagnostics use stable codes. The adapter should read the same maintained JSON and schema, call the generated reader, then inject the typed value into its service constructor. Config DTOs are not persisted player-save types. The generated reader is a loader, not a second JSON writer; do not serialize its DTO back as a replacement data source.

## Generation ownership and integration

`generate` writes `ConfigurationDtos.g.cs`, `ConfigurationJsonRuntime.g.cs`, `ConfigurationTypes.go`, `BeaverJsonRuntime.go`, and `configuration.schema.json`, plus `generation-manifest.json`. It refuses an unowned output directory, collisions, or modifications to previously owned files. Regeneration returns added/updated/removed/unowned paths and does not delete retired files. Copy generated files to a production transaction as explicit file edits with their manifest hashes; this module does not write into a Unity project.

## Read-only Mainline exercise

`.private/v03/config/mainline.schema.json` and `mainline.call.json` record a private call spec derived from the frozen ExampleGame `MainlineRules` DTOs and `MainlineRuleIndex` checks. The active Mainline JSON and frozen copy had the same SHA-256 (`de216f...3577d`); validation returned zero diagnostics across 45 root properties, including 1,293 elements, 3,432 orders, 954 builds, 732 cooking recipes, and declared cross-table references. `.private/v03/config/mainline-generated/` contains the generated C#/Go candidates and ownership manifest. No code or asset was installed into the game. The private report is structural validation evidence, not proof that Unity Player or server consumers have loaded it.

`.private/v03/config/example-game.*` is an independent, self-owned demonstration schema and JSON input with generated output in `example-game-generated/`; its sample values are illustrative and are not product configuration.

Python unit tests exercise strict JSON, nested/optional/nullable values, exact numeric limits, enum and foreign-key errors, resource checks, compatibility reports, output ownership, and generated API contracts. Go generation was compiled offline, and the same Mainline JSON/schema were loaded into the generated typed root in an isolated temporary module. C# was generated, compiled offline with .NET 8 against the already-cached Unity Newtonsoft.Json assembly, and loaded the same Mainline JSON/schema into a typed root (1,293 elements and 3,432 orders). This is isolated runtime evidence, not a Unity Editor or Player compile/run; the main developer should still perform native compilation and consumer verification before adopting the production artifact.
