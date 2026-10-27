# Beaver Go server module

The Go module exposes read-only inspection and compatibility analysis, create-only server/module candidates, explicit migration plans, offline build diagnostics, private Linux binary builds, and a Compose plan artifact. Public CLI and schema registration stay with the Beaver main developer.

## Module API

```python
inspect(project)
generate(spec, output)
generate_module(spec, output)
module_plan(project, spec, output)
protocol_diff(before_proto, after_proto)
migration_plan(project, output)
inspect_migration_state(compose_file, project_name, owner)
execute_migrations(project, plan_file, compose_file, project_name, owner, expected_state_sha256)
inspect_compose_runtime(compose_file, project_name, expected_gateway_port)
build_diagnostics(project, packages=None)
build_candidate(project, architecture="arm64")
compose_plan(spec, output)
toolchain_inspect()
prepare_protocol_tools(private_output)
```

`generate` requires an existing parent and a new output directory. Its input contract is:

```python
{
  "modulePath": "example.com/mygame/server",
  "configuration": {
    "schema": "/path/to/the-authoritative.schema.json",
    "data": "/path/to/the-authoritative-game.json",
    "demoAmountPath": "/starterReward/0/amount"
  },
  "toolchain": {
    "protoc": "/absolute/path/to/protoc",
    "protocGenGo": "/absolute/path/to/protoc-gen-go",
    "protocGenGoGrpc": "/absolute/path/to/protoc-gen-go-grpc"
  }
}
```

The schema/data paths are passed unchanged to configuration v3; it generates the sole Go and C# types/loaders. `demoAmountPath` selects a positive schema-declared int64 only for the self-owned starter transaction example. The loaded `InputSHA256` and `SchemaSHA256` are logged and included in the durable command fingerprint. Generated services do not maintain a duplicate numeric DTO or second copy of the JSON values.

## Generated candidate behavior

The candidate keeps the frozen GREA legacy protobuf package `grea.example.v1`, its Request/Reply field numbers 1–7, and the six `Call` methods. Only `go_package` is parameterized in `.proto`; committed/generated `.pb.go` files are discarded and produced from the changed `.proto` by the fixed `protoc` and Go plugins. Generator versions are protoc 34.1, protoc-gen-go 1.36.12 and protoc-gen-go-grpc 1.6.2. A missing or mismatched executable fails as `needs_action`; downloads are disabled.

The starter module shows a complete vertical example without claiming a product rule: Unity POSTs JSON to gateway; gateway validates the development session and routes through gRPC; server reads a config version token assembled as `<Version>@<InputSHA256>:<SchemaSHA256>`, applies a pure domain transition and in one MySQL transaction writes player state and an immutable receipt. A command retry reads the persisted receipt before checking the active config snapshot or current revision. Reusing its operation ID with changed action, expected revision, config version, or config/schema snapshot hash returns `COMMAND_ID_CONFLICT`, even after the server reloads a newer configuration. A stale new command returns and persists `REVISION_CONFLICT`. A commit error is treated as unknown and never returned as a success. The reply carries the stored revision and state, so a retry cannot roll the client back. Unity can construct the token with `UnityGateway.SnapshotVersion(version, inputSha256, schemaSha256)` from the configuration-v3 runtime snapshot.

Five owner migrations are explicit and per-database (`login`, `server`, `chat`, `world`, `billing`); gateway has no database. Each owner schema has its own migration ledger. Services refuse startup without the owner migration. The MySQL initialization script creates exactly five owner databases/users and grants each service access only to its own database. No migration is auto-run by the generator. Billing always returns gRPC Unavailable until a real provider, verification, reconciliation and protected fulfillment flow are implemented.

`build_diagnostics` runs `gofmt -d`, `go test` and `go build` with `GOTOOLCHAIN=local`, `GOPROXY=off` and `GOSUMDB=off`. `build_candidate` first verifies the generated manifest and hashes only its registered source files, excluding secrets and local deployment artifacts. It rejects any existing or symlinked `bin/`, builds all Linux arm64/amd64 role and health binaries in a hidden staging directory, then uses Beaver's atomic no-replace directory promotion only after all seven builds succeed. A compile or promotion failure discards staging without overwriting a concurrent target or leaving mixed usable output. Its application Dockerfile is `scratch`, non-root, and copies only those binaries and generated JSON/schema. Compose builds one local candidate image and selects each independent role executable with its entrypoint.

`compose_plan` writes an independent JSON-form Compose plan (valid Compose input), not a running stack. Example: `compose_plan({"projectName":"beaver-go-<unique>", "gatewayPort":<unused-loopback-port>, "candidateRoot": str(candidate)}, candidate / "compose-plan.json")`. The required candidate root is bound to its generated file-inventory manifest hash in the Compose extension metadata; the candidate, plan and Compose path cannot traverse symlinks. Keep the plan beside the candidate `Dockerfile` so its build context `.` resolves to the candidate root; Compose secret paths are relative to that file and must be created under the candidate's private `.private/secrets/`. It builds a project-named MySQL bootstrap image from the locally reusable pinned MySQL digest, copies the init script into the image layer and applies executable mode there (the script is never bind-mounted into the official entrypoint directory); Redis uses its pinned digest. The stack uses an internal-only named network, fourteen read-only secret file references, distinct MySQL DSNs, Redis auth, a named MySQL volume, resource bounds, container health probes and a gateway host binding on `127.0.0.1` only. Secret contents are never generated or read. Review current Docker occupancy and secret ownership before use. Apply migrations explicitly before starting roles. Stop/rebuild preserves the volume; never use `down --volumes` for ordinary stop/upgrade. Deleting a Redis cache cannot erase player state or idempotency receipts.
Gateway alone attaches to both the labeled `private` internal bridge and a separately labeled `edge` bridge; the other five processes plus MySQL and Redis attach only to `private`. Only gateway maps its HTTP port, bound to `127.0.0.1`.

`inspect_compose_runtime(compose, project, expected_gateway_port)` is a read-only preflight. It reads Docker's actual container health, network attachment labels and `NetworkSettings.Ports`, returning normalized `hostPublishers` rows `{service, hostIp, hostPort, containerPort, protocol}`. It requires the exact gateway loopback binding and no published ports on other roles, MySQL or Redis. `runtime_preflight_passed` means only container/network/port preflight passed; `applicationBusinessProbe` remains `not_run` and Unity HTTP command tests remain a separate gate.

Generated `clients/UnityGateway.cs` follows receipt contract `beaver.unity-gateway/1`. The candidate receipt records its exact `unityClient.sha256` and capabilities. The client exposes read-only `State()` plus `LastReplyBodySha256`, an in-memory digest of successful raw state/command response text; it does not persist response bodies or include the business acceptance probe.

The plan references `.private/secrets/mysql-root.secret`, `internal-key.secret`, `redis-password.secret`, `development-credential.secret`, and per-owner `mysql-{login,server,chat,world,billing}-{password,dsn}.secret`. The gateway and all RPC clients read the internal key by secret file; only login reads the development credential; only each data-owner role mounts its own DSN. The MySQL bootstrap script accepts only 32–64 lowercase-hex owner passwords. Create the files through the authorized private-stack owner; do not place them in generated source or the Compose plan.

## Main-developer live acceptance sequence

This module does not execute it. The main developer is the one Compose writer and runs the sequence only after checking that the unique project name, loopback port and volume/network names are unused.

1. Generate the candidate from the approved shared configuration Schema/JSON and use `build_diagnostics(candidate)` followed by `build_candidate(candidate, "arm64"|"amd64")`. The binary inventory must contain the six roles plus `healthcheck`; source identity before/after must match.
2. Generate a unique `compose_plan({"projectName": "beaver-go-<unique>", "gatewayPort": <unused>, "candidateRoot": str(candidate)}, candidate / "compose-plan.json")`. Supply the referenced secret files, then start only `mysql` and `redis`. Create one `migration_plan(candidate, new_migration_plan_path)` artifact. For each owner in `login`, `server`, `chat`, `world`, `billing` order, call `inspect_migration_state(compose, project, owner)` and pass its state SHA to `execute_migrations(candidate, migration_plan, compose, project, owner, expected_state_sha256)`. Before every schema query or DDL, the executor checks the actual Compose mysql container's project/service and Beaver ownership labels, its mounted named-volume labels and its single internal network labels; it does not trust the Compose JSON as proof of the live target. SQL uses only `beaver_<owner>` with `/run/secrets/mysql_<owner>_password`, scoped to `beaver_go_example_<owner>`. The executor applies one plan-bound additive migration to one fixed owner database, checks the Compose identity and pre-state hash, and verifies the migration ledger and post-schema hash. Re-read state before the next owner. Start the six application roles only after all five migration results say `applied` or a reviewed `already_applied`. Then run `inspect_compose_runtime(compose, project, expected_gateway_port)` and require the actual `hostPublishers` value to show gateway `127.0.0.1:<port> -> 8080/tcp` with no other host-published service. Container health is preflight evidence only and does not prove HTTP business behavior.
3. From Unity, `UnityGateway.Login(root, developmentCredential)` POSTs `{"credential":"<development credential>"}` to `/v1/login` and returns the development session. Send `/v1/command` with JSON `{"operationId":"intent-001","expectedRevision":"0","action":"claim_starter","configVersion":"<Version>@<InputSHA256>:<SchemaSHA256>"}` and the session bearer header. `expectedRevision`, `revision`, and `balance` are decimal strings under protobuf JSON. `APPLIED` is the sample effect. Persist the exact request JSON and operation ID before send.
4. Repeat the identical request after a successful reply and verify the exact stored result/revision is returned with no extra balance. Repeat the same operation ID with a changed `expectedRevision` or action and verify `COMMAND_ID_CONFLICT`. Send a new operation ID with a stale revision and verify `REVISION_CONFLICT` with unchanged MySQL state. Check `POST /v1/state` with `{}` and the same bearer token.
5. Restart only the candidate `server` container and repeat the original request and state query; then recreate only the candidate Redis container without deleting the named MySQL volume and repeat again. Verify state/receipt remains unchanged. Capture Unity's response plus MySQL receipt/state rows and Redis-loss/restart action identities. Do not delete volumes as a recovery shortcut.

MySQL audit readback in `beaver_go_example_server`:

```sql
SELECT version, applied_at FROM schema_migrations ORDER BY version;
SELECT player, revision, balance, starter_claimed FROM players WHERE player = 'example-player';
SELECT operation_id, payload_hash, config_sha256, code, revision, balance
FROM receipts WHERE player = 'example-player' AND operation_id = 'intent-001';
```

`execute_migrations` requires a plan file/hash plus `expected_state_sha256` from `inspect_migration_state`; it re-hashes the unchanged candidate migrations and Compose file, rejects cross-database/DML/destructive SQL, executes only inside the named mysql container with its mounted owner-specific secret, and verifies the ledger/schema afterward. It intentionally does not auto-rollback MySQL DDL on timeout or partial failure; output supplies pre/post state hashes and instructs the owner to inspect and restore explicitly before retrying. It never connects to a host-published MySQL port or uses the MySQL root account for owner migrations. The five current owner migrations are independent, one call per owner; gateway has no migration.

Every live step above remains `not_run` until the main developer records actual output. A compile/test result alone cannot satisfy the Unity-to-gateway-to-Go-to-MySQL-to-Unity or restart/Redis-loss requirement.

## Protocol and integration limits

`protocol_diff` detects package changes, deleted fields without numeric/name reservation, field-number/type/name reuse, reserved number/name reuse, removed RPCs, changed RPC signatures and compatible additions requiring review. It is a bounded text parser; compiler-grade source validation still uses the fixed `protoc`. `migration_plan` hashes SQL and flags destructive statements but never connects to MySQL. `module_plan` records exact project reads and independent proposed writes with hashes; it never mutates the project.

The frozen GREA source is MIT licensed. Exact selected file hashes and filtering decisions are in `src/beaver/server_templates/grea_seed/SOURCE-MANIFEST.json`; MIT text is retained under `src/beaver/licenses/GREA-MIT.txt`. GREA handoff SHA256 is `283f5fca66e95f1fa65a52fa3c43169f40661ded30a13db375a6e0bb37ab92b3`. CocosGateway, the GREA stack/smoke lifecycle, its demo's duplicate config implementation, and checked-in generated bindings are excluded. The original GREA source and legacy consumer are not modified or retired by this module.

The current candidate is development-only: HTTP/JSON (including the C# `UnityWebRequest` adapter), plaintext internal gRPC behind the isolated-network shared development key, and one-node routing. It does not implement production TLS/mTLS, WSS, session refresh/revocation, product owner-epoch/outbox/inbox behavior, full chat, multi-node routing, or real payment. These are reported gaps, not implied capabilities. The module agent performed offline validation; the main developer subsequently ran the isolated stack and Unity integration described below. These results do not authorize formal services.

## Evidence from this turn

`PYTHONPATH=src python3 -m unittest tests.test_server_go -v` passes the focused server-module suite, including candidate generation, fixed protocol generation, module planning, protocol compatibility checks, static migration risk checks, offline `gofmt`/test/build, and private Linux candidate binary build. This is offline tool evidence. The module-agent checks were offline. Root integration then verified the owned Compose stack, gateway HTTP, Unity request/response, MySQL transaction behavior across restart, and Redis-loss behavior; WSS remains unimplemented.

## Main-developer integration evidence

The owned `beaver-go-edge05` stack ran six roles, MySQL and Redis with loopback gateway access and five owner migrations. Unity Play sent the first stable intent and a matching Mac App resumed it after six-service/MySQL restarts and Redis cache loss. Both returned APPLIED, revision 1, balance 10, with the same exact retry response hash. SQL readback retained one APPLIED receipt and one negative REVISION_CONFLICT receipt; conflicting payloads and stale revisions were rejected. Configuration input/schema hashes matched the Unity and Go readers. This is the synthetic owned example, not ExampleGame server adoption.

Private evidence: `.private/v03/go-unity-player-run-05`, `.private/v03/go-unity-play-result/app-result-05.json`, and `.private/v03/server-go/edge05-after-app-replay-mysql-audit-02.json`. The owned stack was stopped normally with all volumes preserved (`edge05-owned-stop.json`). No QA05 lifecycle action occurred. Public evidence identities and remaining limits are indexed with the v0.3 verification record.
