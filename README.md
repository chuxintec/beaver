# beaver

Controlled native game-project tooling: inspect a target, produce an explicit plan, apply bounded changes and retain readback/recovery evidence. GREA supplies workflow guidance; monkey supplies recovery candidates; beaver operates on adopted or existing development projects.

## Install 0.5.0

Download the wheel and SHA256SUMS from the [v0.5.0 release](https://github.com/chuxintec/beaver/releases/tag/v0.5.0). Verify the checksum before installing into an isolated environment:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install 'beaver-game-tools[toolkit] @ https://github.com/chuxintec/beaver/releases/download/v0.5.0/beaver_game_tools-0.5.0-py3-none-any.whl'
.venv/bin/beaver version
.venv/bin/beaver capabilities
```

Python 3.10+ is required. Native operations require an explicitly selected, already-installed Unity 6000.6.2f1 Editor and the target's uGUI/TMP dependencies. The tool does not install an engine. Image/toolkit extras require Pillow and jsonschema; external Go/protocol tools remain explicit providers.

## Capabilities

- Immutable project adoption, batch/production plans, exact preconditions, bounded native execution and guarded recovery.
- Native Prefab/UI and resource operations, stable asset references, JSON configuration generation and C# maintenance.
- Selected UI inspection, explicit field repairs and typed reference/event wiring through the existing batch transaction.
- Unified compiler/import/provider/Play/Player diagnostics with evidence locations and preserved execution state.
- Bounded artifact/debug sessions and optional Go development scaffolding and tools.
- The inherited execution toolkit: `beaver tool <id> [args...]` preserves its original JSON/output and exit codes. List IDs with `beaver tool --help`.

Normal operations return `beaver-result/1`; exit codes are 0=ok, 1=partial, 2=failed, 3=blocked/not_run. Toolkit commands retain their own contracts. Native writes require explicit target/plan identities; later manual changes are protected. A postexecution consumer-schema error is not evidence of zero writes and must not trigger automatic replay.

## Scope and evidence

Read [usage](docs/DEVELOPMENT-v0.5.md), [CLI contracts](docs/CLI-CONTRACT.md), [UI inspection](docs/UI-INSPECTION.md), [diagnostics](docs/DIAGNOSTICS.md), [public validation summary](docs/VALIDATION-v0.5.json) and [provenance](docs/PROVENANCE.md).

Private-fixture checks include native save/reload, typed references and exact Sprite subasset identity, text overflow/missing-glyph diagnostics and a live button-center raycast. Static inspection, caller-supplied snapshots and a raycast sample do not establish physical input, visual parity, gameplay acceptance or universal engine/platform support. Production-project adoption remains separately pending. Cocos native execution and production-service/payment operations are outside this release.

The public repository starts from a complete sanitized source snapshot. Earlier development history and private evidence remain archived locally and are not included in the public Git history or distribution.

MIT; required upstream notices are included. No original game project, credential, personal configuration, raw log or private candidate is distributed.
