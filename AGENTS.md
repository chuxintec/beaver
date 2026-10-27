# beaver development

Independent game-project tooling: Python CLI and native Unity Editor companions. Source-package recovery belongs to upstream tools.

- Keep product code, tests and public documentation self-contained; use docs/PLAN.md as the single progress entry and docs/CLI-CONTRACT.md for public contracts.
- Default to inspect/plan. Native writes require an explicit target, immutable plan, implementation identity and content preconditions; preserve later manual edits.
- Edit Unity assets through native Editor APIs. Do not author serialized scenes or Prefabs by hand, unpack variants implicitly or guess subasset identifiers.
- Keep runtime numeric configuration in UTF-8 JSON; object references may remain native assets.
- Keep raw evidence, credentials, local environment data, original game files and generated candidates under ignored local directories. Publish only neutral examples and factual validation summaries.
- Preserve required copyright and third-party license notices. Toolkit subcommands retain their original output and exit-code contracts.
- Separate capability presence, provider readiness, native execution, runtime observations, physical input, visual parity and business acceptance.
- Run checks appropriate to the affected paths. Do not repeat unrelated engine or full-application suites for documentation changes.
- Project/service/player-data writes and installation operations require their own applicable authorization; a tool release does not grant them.
- Module contributors use disjoint files and do not start recursive agents; one owner controls shared interfaces, the Editor, desktop input and release state.
