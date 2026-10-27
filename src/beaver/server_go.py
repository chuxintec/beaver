"""Read-only Go server inspection and create-only candidate generators."""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import shlex
import subprocess
import tempfile
from pathlib import Path
from typing import Any

SERVICES = ("gateway", "chat", "server", "login", "world", "billing")
OWNERS = ("login", "server", "chat", "world", "billing")
MYSQL_IMAGE = "mysql@sha256:2952e3be7807f06fc18de50b3ea1a632d5c70d63482ff7d7376fe3aa8999babf"
REDIS_IMAGE = "public.ecr.aws/docker/library/redis:7.4@sha256:4fa24486b8bcca8eec45ee0eb166edc674795e53a2b53d1a9ef263eecebaac85"
BUILDER_IMAGE = "public.ecr.aws/docker/library/golang:1.26.2@sha256:b54cbf583d390341599d7bcbc062425c081105cc5ef6d170ced98ef9d047c716"


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _file(path: Path, root: Path) -> dict[str, Any]:
    raw = path.read_bytes()
    return {"path": path.relative_to(root).as_posix(), "bytes": len(raw), "sha256": _sha(raw)}


def _inside(path: Path) -> bool:
    return not path.is_symlink()


def inspect(project: str | os.PathLike[str]) -> dict[str, Any]:
    """Inspect a Go server tree without executing Go, protoc, Docker or project code."""
    root = Path(project).expanduser().resolve(strict=True)
    if not root.is_dir():
        raise ValueError("project must be a directory")
    files = [p for p in root.rglob("*") if p.is_file() and _inside(p)]
    rels = {p.relative_to(root).as_posix(): p for p in files}
    go_mod = rels.get("go.mod")
    mod_text = go_mod.read_text("utf-8", errors="replace") if go_mod else ""
    proto = sorted(p for name, p in rels.items() if name.endswith(".proto"))
    json_files = sorted(p for name, p in rels.items() if name.endswith(".json"))
    roles = {name for name in SERVICES if any(x.startswith(f"cmd/{name}/") for x in rels)}
    modules = sorted({m.group(1) for name in rels for m in [re.match(r"internal/games/([^/]+)/", name)] if m})
    versions = {dep: (re.search(r"(?m)^\s*" + re.escape(dep) + r"\s+(v[^\s]+)", mod_text) or [None, None])[1]
                for dep in ("google.golang.org/grpc", "google.golang.org/protobuf", "github.com/go-sql-driver/mysql", "github.com/redis/go-redis/v9")}
    return {
        "schemaVersion": 1, "project": str(root), "readOnly": True,
        "goMod": _file(go_mod, root) if go_mod else None,
        "modulePath": (re.search(r"(?m)^module\s+(\S+)", mod_text) or [None, None])[1],
        "goDirective": (re.search(r"(?m)^go\s+(\S+)", mod_text) or [None, None])[1],
        "dependencies": versions,
        "serviceDirectories": sorted(roles), "missingServices": [s for s in SERVICES if s not in roles],
        "protoFiles": [_file(p, root) for p in proto], "generatedProtoFiles": [n for n in rels if n.endswith((".pb.go", "_grpc.pb.go"))],
        "jsonFiles": [_file(p, root) for p in json_files], "gameModules": modules,
        "dataOwnerDirectories": [o for o in OWNERS if any(n.startswith(f"internal/services/{o}/") or n.startswith(f"internal/owners/{o}/") for n in rels)],
        "dockerFiles": [n for n in rels if Path(n).name in ("Dockerfile", "compose.yaml", "docker-compose.yml")],
        "fileCount": len(files), "limitations": ["静态目录检查；不运行Go、protoc、容器或项目代码", "目录存在不证明职责/权限实现正确"]
    }


def _module_name(spec: dict[str, Any]) -> str:
    name = spec.get("module") or spec.get("name")
    if not isinstance(name, str) or not re.fullmatch(r"[a-z][a-z0-9_]{1,47}", name):
        raise ValueError("spec.module must be a lowercase Go package-safe identifier (2-48 chars)")
    return name


def _go_mod(module_path: str) -> str:
    if not re.fullmatch(r"(?=.+\.)[A-Za-z0-9][A-Za-z0-9./_-]*", module_path):
        raise ValueError("spec.modulePath must be a valid module path")
    return f'''module {module_path}\n\ngo 1.26.0\n\nrequire (\n\tgithub.com/go-sql-driver/mysql v1.10.0\n\tgithub.com/redis/go-redis/v9 v9.23.0\n\tgoogle.golang.org/grpc v1.83.2\n\tgoogle.golang.org/protobuf v1.36.12\n)\n\nrequire (\n\tfilippo.io/edwards25519 v1.2.0 // indirect\n\tgithub.com/cespare/xxhash/v2 v2.3.0 // indirect\n\tgo.uber.org/atomic v1.12.0 // indirect\n\tgolang.org/x/net v0.58.0 // indirect\n\tgolang.org/x/sys v0.48.0 // indirect\n\tgolang.org/x/text v0.41.0 // indirect\n\tgoogle.golang.org/genproto/googleapis/rpc v0.0.0-20260526163538-3dc84a4a5aaa // indirect\n)\n'''


def _svc_main(role: str, module_path: str) -> str:
    # A small executable health shell; business behavior must be supplied by explicit modules.
    return f'''package main

import (
    "context"
    "log"
    "net/http"
    "os"
    "time"
)

func main() {{
    addr := os.Getenv("{role.upper()}_ADDR")
    if addr == "" {{ addr = ":8080" }}
    mux := http.NewServeMux()
    mux.HandleFunc("/health/live", func(w http.ResponseWriter, _ *http.Request) {{ w.WriteHeader(http.StatusOK); _, _ = w.Write([]byte("ok")) }})
    mux.HandleFunc("/health/ready", func(w http.ResponseWriter, _ *http.Request) {{ w.WriteHeader(http.StatusOK); _, _ = w.Write([]byte("ready")) }})
    srv := &http.Server{{Addr: addr, Handler: mux, ReadHeaderTimeout: 5*time.Second}}
    log.Printf("service={role} mode=development health_addr=%s", addr)
    if err := srv.ListenAndServe(); err != nil && err != http.ErrServerClosed {{ log.Fatal(err) }}
    _ = srv.Shutdown(context.Background())
}}
'''


def _domain(name: str) -> str:
    return f'''package domain

import "errors"

var ErrNotImplemented = errors.New("module {name}: business rule and repository are not supplied by scaffold")

// Command carries immutable operation identity and optimistic concurrency metadata.
type Command struct {{ OperationID string; ExpectedRevision int64; ConfigVersion string; Action string }}
type Result struct {{ Code string; Revision int64; Payload []byte }}

// Apply deliberately refuses to invent an outcome. A game module supplies its own pure rule.
func Apply(Command) (Result, error) {{ return Result{{Code: "UNAVAILABLE"}}, ErrNotImplemented }}
'''


def _json_loader() -> str:
    return '''package snapshot

import (
    "crypto/sha256"
    "encoding/hex"
    "encoding/json"
    "errors"
    "fmt"
    "os"
)

type Envelope struct { Version string `json:"version"`; SHA256 string `json:"sha256"`; Data json.RawMessage `json:"data"` }
type Snapshot[T any] struct { Version string; SHA256 string; Value T }

// Load verifies the exact data bytes before decoding into a strongly typed value.
func Load[T any](path string) (Snapshot[T], error) {
    raw, err := os.ReadFile(path); if err != nil { return Snapshot[T]{}, err }
    var env Envelope; if err = json.Unmarshal(raw, &env); err != nil { return Snapshot[T]{}, err }
    if env.Version == "" || len(env.Data) == 0 { return Snapshot[T]{}, errors.New("snapshot version and data are required") }
    sum := sha256.Sum256(env.Data); got := hex.EncodeToString(sum[:])
    if got != env.SHA256 { return Snapshot[T]{}, fmt.Errorf("snapshot hash mismatch: got %s", got) }
    var value T; if err = json.Unmarshal(env.Data, &value); err != nil { return Snapshot[T]{}, err }
    return Snapshot[T]{Version: env.Version, SHA256: got, Value: value}, nil
}
'''


def _write_new(output: Path, files: dict[str, str | bytes]) -> list[dict[str, Any]]:
    out = output.expanduser().absolute()
    if out.exists() or not out.parent.is_dir():
        raise ValueError("output must be a new directory with an existing parent")
    # Validate the entire file set before the first write.
    for name in files:
        p = Path(name)
        if p.is_absolute() or ".." in p.parts or not name or "\\" in name:
            raise ValueError(f"unsafe generated path: {name}")
    out.mkdir()
    manifest = []
    try:
        for name, body in sorted(files.items()):
            raw = body.encode("utf-8") if isinstance(body, str) else body
            target = out / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(raw)
            manifest.append({"path": name, "bytes": len(raw), "sha256": _sha(raw)})
        return manifest
    except Exception:
        # Keep partial output for explicit inspection rather than deleting evidence.
        raise


def _format_generated_go(output: Path) -> bool:
    root = output.expanduser().absolute()
    gofmt = shutil.which("gofmt")
    if not gofmt: return False
    paths = [str(p) for p in root.rglob("*.go") if p.is_file() and not p.is_symlink()]
    if paths: subprocess.run([gofmt, "-w", *paths], check=True, capture_output=True, text=True, timeout=30)
    return True


def _manifest(output: Path, exclude: tuple[str, ...] = ()) -> list[dict[str, Any]]:
    root = output.expanduser().absolute()
    return [{"path": p.relative_to(root).as_posix(), "bytes": p.stat().st_size, "sha256": _sha(p.read_bytes())}
            for p in sorted(root.rglob("*")) if p.is_file() and p.name not in exclude]


def _configured_seed(spec: dict[str, Any], temp_parent: Path) -> tuple[dict[str, str | bytes], dict[str, Any]]:
    configuration = spec.get("configuration")
    if not isinstance(configuration, dict) or "schema" not in configuration or "data" not in configuration:
        raise ValueError("generate requires the single shared configuration schema and formal JSON input")
    if not isinstance(configuration["schema"], (str, os.PathLike, bytes)) or not isinstance(configuration["data"], (str, os.PathLike, bytes)):
        raise ValueError("schema and data must be the original JSON files/bytes so cross-language source hashes remain stable")
    from . import configuration_v3
    import tempfile
    temp_parent = temp_parent.absolute()
    if not temp_parent.is_dir() or temp_parent.is_symlink(): raise ValueError("candidate output parent must be an existing non-symlink directory")
    with tempfile.TemporaryDirectory(prefix="beaver-server-config-", dir=temp_parent) as td:
        generated = configuration_v3.generate(configuration["schema"], Path(td) / "generated")
        if generated.get("status") != "ok":
            raise ValueError("shared configuration generator did not return an owned output")
        gen_root = Path(generated["output"])
        manifest = json.loads((gen_root / "generation-manifest.json").read_text("utf-8"))
        owned = manifest.get("ownedFiles", {})
        for name, entry in owned.items():
            item = gen_root / name
            if not item.is_file() or _sha(item.read_bytes()) != entry.get("sha256"):
                raise ValueError("shared configuration generated-file identity mismatch")
        go_types = (gen_root / "ConfigurationTypes.go").read_text("utf-8")
        root_type = re.search(r"(?m)^type RootType = (\w+)$", go_types)
        if not root_type:
            raise ValueError("shared configuration output has no RootType alias")
        if not re.search(r"(?m)^\s*Version\s+string\b", go_types):
            raise ValueError("shared configuration root must provide the version string required by the server contract")
        data_value = configuration["data"]
        if isinstance(data_value, bytes): data_raw = data_value
        else: data_raw = Path(data_value).expanduser().resolve(strict=True).read_bytes()
        schema_value = configuration["schema"]
        if isinstance(schema_value, bytes): schema_input = schema_value
        else: schema_input = Path(schema_value).expanduser().resolve(strict=True).read_bytes()
        try:
            data_object = json.loads(data_raw)
            schema_object = json.loads(schema_input)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("configuration source JSON is invalid") from exc
        amount_path = configuration.get("demoAmountPath")
        if not isinstance(amount_path, str) or not amount_path.startswith("/") or amount_path == "/":
            raise ValueError("configuration.demoAmountPath must select one integer from the shared game JSON using a JSON Pointer")
        selected: Any = data_object
        try:
            for part in amount_path[1:].split("/"):
                key = part.replace("~1", "/").replace("~0", "~")
                selected = selected[int(key)] if isinstance(selected, list) else selected[key]
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise ValueError("configuration.demoAmountPath does not resolve in formal JSON") from exc
        if isinstance(selected, bool) or not isinstance(selected, int) or selected <= 0 or selected > (1 << 63) - 1:
            raise ValueError("configuration.demoAmountPath must resolve to a positive int64")
        schema_node = schema_object.get("root", {})
        for part in amount_path[1:].split("/"):
            key = part.replace("~1", "/").replace("~0", "~")
            if isinstance(schema_node, dict) and schema_node.get("type") == "object":
                schema_node = schema_node.get("properties", {}).get(key, {})
            elif isinstance(schema_node, dict) and schema_node.get("type") == "array" and key.isdecimal():
                schema_node = schema_node.get("items", {})
            else:
                schema_node = {}
        if not (isinstance(schema_node, dict) and schema_node.get("type") == "integer" and schema_node.get("bits", 64) == 64):
            raise ValueError("configuration.demoAmountPath must point to a schema-declared int64 value")
        generated_files = {"internal/configv3/ConfigurationTypes.go": (gen_root / "ConfigurationTypes.go").read_bytes(),
                           "internal/configv3/BeaverJsonRuntime.go": (gen_root / "BeaverJsonRuntime.go").read_bytes(),
                           "internal/configv3/configuration.schema.json": (gen_root / "configuration.schema.json").read_bytes(),
                           "internal/configv3/generation-manifest.json": (gen_root / "generation-manifest.json").read_bytes(),
                           "unity/Runtime/Configuration/ConfigurationDtos.g.cs": (gen_root / "ConfigurationDtos.g.cs").read_bytes(),
                           "unity/Runtime/Configuration/ConfigurationJsonRuntime.g.cs": (gen_root / "ConfigurationJsonRuntime.g.cs").read_bytes()}
        if len(data_raw) > 2 * 1024 * 1024: raise ValueError("configuration JSON exceeds 2 MiB")
        schema_raw = (gen_root / "configuration.schema.json").read_bytes()
        generated_files["config/game.json"] = data_raw
        generated_files["config/configuration.schema.json"] = schema_raw
        generated_files["internal/configv3/load.go"] = "\n".join([
            "package configv3", "", "import (\"os\"; \"fmt\"; \"reflect\"; \"strconv\"; \"strings\")", "",
            "// LoadFiles reads the official JSON and generated schema; no second numeric DTO or table is maintained here.",
            "func LoadFiles(dataPath, schemaPath string) (RootType, Snapshot, error) {",
            "    data, err := os.ReadFile(dataPath); if err != nil { return RootType{}, Snapshot{}, err }",
            "    schema, err := os.ReadFile(schemaPath); if err != nil { return RootType{}, Snapshot{}, err }",
            "    return ReadConfiguration[RootType](data, schema)", "}",
            "// IntegerAt reads a configured sample amount from the generated strong type via its JSON field names.",
            "// The path is selected by the adopting game's module spec; this helper introduces no second value source.",
            "func IntegerAt(value any, pointer string) (int64, error) {",
            "    if len(pointer) < 2 || pointer[0] != '/' { return 0, fmt.Errorf(\"invalid JSON pointer\") }",
            "    current := reflect.ValueOf(value)",
            "    for _, raw := range strings.Split(pointer[1:], \"/\") {",
            "        part := strings.ReplaceAll(strings.ReplaceAll(raw, \"~1\", \"/\"), \"~0\", \"~\")",
            "        for current.Kind() == reflect.Pointer || current.Kind() == reflect.Interface { if current.IsNil() { return 0, fmt.Errorf(\"nil at %s\", part) }; current = current.Elem() }",
            "        switch current.Kind() {",
            "        case reflect.Struct:",
            "            typ := current.Type(); found := false",
            "            for i := 0; i < current.NumField(); i++ { if strings.Split(typ.Field(i).Tag.Get(\"json\"), \",\")[0] == part { current = current.Field(i); found = true; break } }",
            "            if !found { return 0, fmt.Errorf(\"unknown field %s\", part) }",
            "        case reflect.Slice, reflect.Array:",
            "            index, err := strconv.Atoi(part); if err != nil || index < 0 || index >= current.Len() { return 0, fmt.Errorf(\"invalid index %s\", part) }; current = current.Index(index)",
            "        default: return 0, fmt.Errorf(\"non-container at %s\", part)",
            "        }",
            "    }",
            "    for current.Kind() == reflect.Pointer || current.Kind() == reflect.Interface { if current.IsNil() { return 0, fmt.Errorf(\"nil numeric value\") }; current = current.Elem() }",
            "    switch current.Kind() { case reflect.Int, reflect.Int8, reflect.Int16, reflect.Int32, reflect.Int64: return current.Int(), nil; case reflect.Uint, reflect.Uint8, reflect.Uint16, reflect.Uint32, reflect.Uint64: n := current.Uint(); if n > uint64(^uint64(0)>>1) { return 0, fmt.Errorf(\"int64 overflow\") }; return int64(n), nil; default: return 0, fmt.Errorf(\"value is not integer\") }",
            "}", ""])
        generated_files["internal/configv3/load_test.go"] = "\n".join([
            "package configv3", "", "import \"testing\"", "",
            "func TestFormalConfigurationSnapshot(t *testing.T) {",
            "    value, snapshot, err := LoadFiles(\"../../config/game.json\", \"../../config/configuration.schema.json\")",
            "    if err != nil { t.Fatal(err) }",
            f"    if snapshot.SchemaID != {json.dumps(str(manifest.get('schemaId') or ''))} {{ t.Fatalf(\"schema ID mismatch: %s\", snapshot.SchemaID) }}",
            f"    if snapshot.InputSHA256 != {json.dumps(_sha(data_raw))} {{ t.Fatalf(\"input snapshot hash mismatch: %s\", snapshot.InputSHA256) }}",
            f"    if snapshot.SchemaSHA256 != {json.dumps(_sha(schema_raw))} {{ t.Fatalf(\"schema snapshot hash mismatch: %s\", snapshot.SchemaSHA256) }}",
            f"    if amount, err := IntegerAt(value, {json.dumps(amount_path)}); err != nil || amount <= 0 {{ t.Fatalf(\"configured demo amount invalid: %d, %v\", amount, err) }}",
            "}", ""])
        return generated_files, {"schemaId": manifest.get("schemaId"), "schemaSha256": manifest.get("schemaSha256"),
                                "generationManifestSha256": _sha((gen_root / "generation-manifest.json").read_bytes()),
                                "dataSha256": _sha(data_raw), "rootType": root_type[1],
                                "demoAmountPath": amount_path, "demoAmount": selected}


def _starter_module_files() -> dict[str, str]:
    return {
      "internal/games/starter/domain/command.go": '''package domain

import "errors"

var ErrUnsupportedAction = errors.New("unsupported starter demo action")
var ErrInvalidReward = errors.New("starter demo amount must be positive")
var ErrConfigurationVersion = errors.New("configuration snapshot version mismatch")

const ActionClaimStarter = "claim_starter"
type Command struct { OperationID string; Action string; ConfigVersion string; ExpectedRevision int64 }
type State struct { Revision int64; Balance int64; Claimed bool }
type Result struct { Code string; Revision int64; Balance int64 }

// Transition is pure demo-domain logic; product rules belong to the adopting game module.
func Transition(state State, command Command, amount int64) (State, string, error) {
    if command.Action != ActionClaimStarter { return state, "", ErrUnsupportedAction }
    if amount <= 0 { return state, "", ErrInvalidReward }
    if command.ExpectedRevision != state.Revision { return state, "REVISION_CONFLICT", nil }
    if state.Claimed { return state, "REJECTED", nil }
    if state.Balance > int64(^uint64(0)>>1)-amount { return state, "", errors.New("balance overflow") }
    state.Balance += amount; state.Revision++; state.Claimed = true
    return state, "APPLIED", nil
}
''',
      "internal/games/starter/application/service.go": '''package application

import (
    "context"
    "errors"
    "strings"
    "example.com/game/server/internal/games/starter/domain"
)

var ErrInvalidCommand = errors.New("command identity required")

var ErrConfigurationVersion = domain.ErrConfigurationVersion
var ErrUnsupportedAction = domain.ErrUnsupportedAction
type Repository interface { Execute(context.Context, string, domain.Command, int64, string) (domain.Result, error) }
type Command = domain.Command
type Service struct { repository Repository; configVersion string; amount int64 }
func New(repository Repository, configVersion string, amount int64) *Service { return &Service{repository: repository, configVersion: configVersion, amount: amount} }
func (s *Service) Execute(ctx context.Context, player string, command domain.Command) (domain.Result, error) {
    if s == nil || s.repository == nil || player == "" || command.OperationID == "" || len(command.OperationID) > 80 || strings.TrimSpace(command.OperationID) != command.OperationID || command.ConfigVersion == "" { return domain.Result{}, ErrInvalidCommand }
    return s.repository.Execute(ctx, player, command, s.amount, s.configVersion)
}
''',
      "internal/games/starter/repository/repository.go": '''package repository

import (
    "context"
    "example.com/game/server/internal/games/starter/domain"
)

// Repository is implemented by the server service's own MySQL transaction adapter.
type Repository interface { Execute(context.Context, string, domain.Command, int64, string) (domain.Result, error) }
''',
      "internal/games/starter/transport/handler.go": '''package transport

import (
    "context"
    "example.com/game/server/internal/games/starter/application"
    "example.com/game/server/internal/games/starter/domain"
)

// Handler binds the game command contract at compile time; it accepts verified player scope only.
type Handler struct { service *application.Service }
func New(service *application.Service) *Handler { return &Handler{service: service} }
func (h *Handler) Execute(ctx context.Context, player string, command domain.Command) (domain.Result, error) { return h.service.Execute(ctx, player, command) }
''',
      "internal/games/starter/domain/command_test.go": '''package domain

import "testing"

func TestTransitionRevisionAndSingleClaim(t *testing.T) {
    initial := State{}
    command := Command{Action: ActionClaimStarter, ExpectedRevision: 0}
    next, code, err := Transition(initial, command, 7)
    if err != nil || code != "APPLIED" || next.Balance != 7 || next.Revision != 1 || !next.Claimed { t.Fatalf("unexpected transition: %#v %s %v", next, code, err) }
    command.ExpectedRevision = 1
    after, code, err := Transition(next, command, 7)
    if err != nil || code != "REJECTED" || after.Balance != 7 { t.Fatalf("duplicate effect: %#v %s %v", after, code, err) }
}
'''}


def _starter_migrations() -> dict[str, str]:
    owners = {
      "login": "CREATE TABLE sessions(token_hash CHAR(64) PRIMARY KEY, player VARCHAR(80) NOT NULL, expires_ms BIGINT NOT NULL) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;",
      "server": "CREATE TABLE players(player VARCHAR(80) PRIMARY KEY, revision BIGINT NOT NULL, balance BIGINT NOT NULL, starter_claimed BOOLEAN NOT NULL) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;\nCREATE TABLE receipts(player VARCHAR(80) NOT NULL, operation_id VARCHAR(80) NOT NULL, payload_hash CHAR(64) NOT NULL, config_sha256 CHAR(64) NOT NULL, code VARCHAR(40) NOT NULL, revision BIGINT NOT NULL, balance BIGINT NOT NULL, PRIMARY KEY(player,operation_id)) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;",
      "world": "CREATE TABLE routes(player VARCHAR(80) PRIMARY KEY, endpoint VARCHAR(180) NOT NULL) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;",
      "chat": "CREATE TABLE messages(player VARCHAR(80) NOT NULL, operation_id VARCHAR(80) NOT NULL, content VARCHAR(500) NOT NULL, created_ms BIGINT NOT NULL, PRIMARY KEY(player,operation_id)) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;",
      "billing": "CREATE TABLE orders(player VARCHAR(80) NOT NULL, operation_id VARCHAR(80) NOT NULL, sku VARCHAR(80) NOT NULL, status VARCHAR(40) NOT NULL, PRIMARY KEY(player,operation_id)) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;"}
    files = {}
    for owner, ddl in owners.items():
        files[f"migrations/{owner}/0001_initial.sql"] = "CREATE TABLE schema_migrations(version VARCHAR(64) PRIMARY KEY, applied_at TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6)) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;\n" + ddl + f"\nINSERT INTO schema_migrations(version) VALUES('0001_initial');\n"
    return files


def _healthcheck_source() -> str:
    return '''package main

import (
    "context"
    "fmt"
    "io"
    "net/http"
    "os"
    "strings"
    "time"

    "google.golang.org/grpc"
    "google.golang.org/grpc/credentials/insecure"
    "google.golang.org/grpc/metadata"
    healthpb "google.golang.org/grpc/health/grpc_health_v1"
)

func main() {
    if len(os.Args) != 3 { fmt.Fprintln(os.Stderr, "usage: healthcheck http|grpc address"); os.Exit(2) }
    ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second); defer cancel()
    switch os.Args[1] {
    case "http":
        req, err := http.NewRequestWithContext(ctx, http.MethodGet, os.Args[2], nil); if err != nil { fail(err) }
        response, err := http.DefaultClient.Do(req); if err != nil { fail(err) }; defer response.Body.Close()
        _, _ = io.Copy(io.Discard, io.LimitReader(response.Body, 4096)); if response.StatusCode != http.StatusOK { fail(fmt.Errorf("gateway health status %d", response.StatusCode)) }
    case "grpc":
        secretPath := os.Getenv("BEAVER_GO_INTERNAL_FILE"); key, err := os.ReadFile(secretPath); if err != nil || len(strings.TrimSpace(string(key))) < 16 { fail(fmt.Errorf("internal key unavailable")) }
        conn, err := grpc.NewClient(os.Args[2], grpc.WithTransportCredentials(insecure.NewCredentials()), grpc.WithUnaryInterceptor(func(ctx context.Context, method string, req, reply any, cc *grpc.ClientConn, invoke grpc.UnaryInvoker, opts ...grpc.CallOption) error { return invoke(metadata.AppendToOutgoingContext(ctx, "x-beaver-go-internal", strings.TrimSpace(string(key))), method, req, reply, cc, opts...) }))
        if err != nil { fail(err) }; defer conn.Close()
        result, err := healthpb.NewHealthClient(conn).Check(ctx, &healthpb.HealthCheckRequest{}); if err != nil || result.GetStatus() != healthpb.HealthCheckResponse_SERVING { fail(fmt.Errorf("gRPC health unavailable")) }
    default: fail(fmt.Errorf("unknown probe mode"))
    }
}
func fail(err error) { fmt.Fprintln(os.Stderr, err); os.Exit(1) }
'''


def generate(spec: dict[str, Any], output: str | os.PathLike[str]) -> dict[str, Any]:
    'Create a working six-process development example from the reviewed GREA seed.'
    if not isinstance(spec, dict): raise ValueError("spec must be an object")
    module_path = spec.get("modulePath", "example.com/game/server")
    _go_mod(module_path)
    files: dict[str, str | bytes] = {}
    seed = Path(__file__).parent / "server_templates" / "grea_seed"
    excluded = {"SOURCE-MANIFEST.json", "DEPENDENCIES.md"}
    replacements = (("example.com/grea/server", module_path),
                    ("GREA_", "BEAVER_GO_"), ("GREA", "BEAVER_GO"), ("grea_example_", "beaver_go_example_"),
                    ("grea:rate:", "beaver:rate:"), ("grea-owned-", "beaver-owned-"),
                    ("x-grea-internal", "x-beaver-go-internal"))
    for source in sorted(seed.rglob("*")):
        if not source.is_file() or source.name in excluded: continue
        relative = source.relative_to(seed).as_posix()
        if relative.startswith("cmd/role/") or relative.startswith("internal/demo/") or relative == "config/game.json": continue
        if relative in ("api/game.pb.go", "api/game_grpc.pb.go"): continue
        raw = source.read_bytes()
        if source.suffix in (".go", ".proto", ".cs", ".json", ".md", ".mod", ".gitignore", ".dockerignore"):
            content = raw.decode("utf-8")
            for old, new_value in replacements: content = content.replace(old, new_value)
            files[relative] = content
        else: files[relative] = raw
    config_files, config_identity = _configured_seed(spec, Path(output).expanduser().absolute().parent)
    files.update(config_files)
    files.update({path: content.replace("example.com/game/server", module_path) for path, content in _starter_module_files().items()})
    files.update(_starter_migrations())
    files["cmd/healthcheck/main.go"] = _healthcheck_source()
    proto = str(files["api/game.proto"])
    proto = re.sub(r'option go_package = "[^"]+";', f'option go_package = "{module_path}/api;api";', proto)
    files["api/game.proto"] = proto
    proto_files, proto_identity = _generate_proto(proto, spec.get("toolchain"))
    files.update(proto_files)
    files["Dockerfile"] = "FROM scratch\nWORKDIR /app\nCOPY bin/ /app/bin/\nCOPY config/ /app/config/\nUSER 65532:65532\nENTRYPOINT [\"/app/bin/gateway\"]\n"
    files["deploy/mysql/Dockerfile"] = (f"FROM {MYSQL_IMAGE}\n"
        "COPY deploy/mysql/001-create-databases.sh /docker-entrypoint-initdb.d/001-create-databases.sh\n"
        "RUN chmod 0555 /docker-entrypoint-initdb.d/001-create-databases.sh\n"
        "LABEL beaver.owner=beaver-v03-server-go beaver.mysql.base.digest=" + MYSQL_IMAGE.split("@", 1)[1] + "\n")
    files[".dockerignore"] = ".git\n.private\n**/*.secret\n**/validation/**\n"
    files["deploy/mysql/001-create-databases.sh"] = '''#!/bin/bash
set -euo pipefail
root_password=$(cat /run/secrets/mysql_root)
mysql=(mysql --protocol=socket -uroot -p"${root_password}")
for owner in login server chat world billing; do
  secret_name="mysql_${owner}_password"
  password=$(cat "/run/secrets/${secret_name}")
  if [[ ! "$password" =~ ^[a-f0-9]{32,64}$ ]]; then echo "invalid generated password format for ${owner}" >&2; exit 1; fi
  database="beaver_go_example_${owner}"
  user="beaver_${owner}"
  "${mysql[@]}" -e "CREATE DATABASE IF NOT EXISTS ${database} CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci; CREATE USER IF NOT EXISTS '${user}'@'%' IDENTIFIED BY '${password}'; ALTER USER '${user}'@'%' IDENTIFIED BY '${password}'; GRANT ALL PRIVILEGES ON ${database}.* TO '${user}'@'%';"
done
'''
    files["deploy/README.md"] = """# 私有开发候选部署说明\n\n该Compose仅供具名隔离开发栈。先创建 `.private/secrets/` 中的只读secret文件；各MySQL用户密码由32–64位小写十六进制随机值组成，脚本据此安全引用。Compose项目名、网络、volume和gateway回环端口必须唯一，并由操作者先检查当前占用。\n\nMySQL bootstrap 脚本由 `deploy/mysql/Dockerfile` 从固定 MySQL digest 镜像层内复制，并在镜像构建时设置可执行权限；不要将脚本 bind mount 到官方 entrypoint 目录，以免宿主 noexec 阻止运行。首次空库启动时MySQL entrypoint会执行该脚本，只建立五个具名owner数据库和独立账号（gateway 无数据库）。然后按 `migrations/{login,server,chat,world,billing}/*.sql` 逐库应用唯一owner迁移，并读回 `schema_migrations`；应用服务缺迁移时拒绝ready。`compose stop` / `compose up --build` 保留具名MySQL卷。升级不得使用 `down --volumes`；Redis可重建且不含资产或命令回执。\n\n`billing`的协议入口固定返回Unavailable，直到真实支付渠道、验签、订单对账与受保护发货状态机另行接入。development明文gRPC仅限本隔离internal network，不能用于生产。\n"""
    if "internal/platform/run.go" in files:
        run = str(files["internal/platform/run.go"])
        run = run.replace(f'"{module_path}/internal/demo"', f'"{module_path}/internal/configv3"')
        run = run.replace('cfg, e := demo.LoadConfig(env("BEAVER_GO_GAME_CONFIG", "config/game.json"))', 'cfg, configSnapshot, e := configv3.LoadFiles(env("BEAVER_GO_GAME_CONFIG", "config/game.json"), env("BEAVER_GO_GAME_SCHEMA", "config/configuration.schema.json"))')
        run = run.replace('"fmt"', '"fmt"\n\t"log"')
        run = run.replace('key := secret("BEAVER_GO_INTERNAL")', 'log.Printf("configuration schema=%s schema_sha256=%s input_sha256=%s", configSnapshot.SchemaID, configSnapshot.SchemaSHA256, configSnapshot.InputSHA256)\n\tkey := secret("BEAVER_GO_INTERNAL")')
        run = run.replace(f'"{module_path}/internal/configv3"', f'configv3 "{module_path}/internal/configv3"\n\tstarterapp "{module_path}/internal/games/starter/application"')
        run = run.replace('s := &Service{Role: role, DB: db, Config: cfg, Login: pb.NewLoginClient(login), GameAddress: env("BEAVER_GO_SERVER", "server:50051")}', 'demoAmount, amountErr := configv3.IntegerAt(cfg, env("BEAVER_GO_DEMO_AMOUNT_PATH", "' + str(config_identity["demoAmountPath"]) + '"))\n\tif amountErr != nil { return amountErr }\n\tactiveConfigVersion := cfg.Version + "@" + configSnapshot.InputSHA256 + ":" + configSnapshot.SchemaSHA256\n\tstarter := starterapp.New(&MySQLStarterRepository{DB: db}, activeConfigVersion, demoAmount)\n\ts := &Service{Role: role, DB: db, Config: cfg, Starter: starter, Login: pb.NewLoginClient(login), GameAddress: env("BEAVER_GO_SERVER", "server:50051")}')
        files["internal/platform/run.go"] = run
    if "internal/platform/store.go" in files:
        store = str(files["internal/platform/store.go"])
        store = re.sub(r"\tdefinitions := map\[string\]\[\]string\{.*?\n\treturn db, nil", "\tvar applied string\n\tif e = db.QueryRowContext(ctx, `SELECT version FROM schema_migrations ORDER BY applied_at DESC LIMIT 1`).Scan(&applied); e != nil { db.Close(); return nil, errors.New(\"owner migration 0001_initial must be applied explicitly\") }\n\treturn db, nil", store, flags=re.S)
        store = store.replace('"errors"', '"errors"\n\tstarterdomain "' + module_path + '/internal/games/starter/domain"')
        signature = 'func Command(ctx context.Context, db *sql.DB, player, id, action string, expected, reward int64) (code string, revision, balance int64, err error) {'
        store = store.replace(signature, 'func CommandWithConfig(ctx context.Context, db *sql.DB, player, id, action string, expected, reward int64, configVersion, activeConfigVersion string) (code string, revision, balance int64, err error) {')
        store = store.replace('payload := Hash(action + ":" + fmtInt(expected))', 'payload := Hash(action + ":" + fmtInt(expected) + ":" + configVersion)\n\tconfigDigest := Hash(configVersion)')
        store = store.replace('var priorHash string', 'var priorHash, priorConfig string')
        store = store.replace('`SELECT payload_hash,code,revision,balance FROM receipts WHERE player=? AND operation_id=?`', '`SELECT payload_hash,config_sha256,code,revision,balance FROM receipts WHERE player=? AND operation_id=?`')
        store = store.replace('.Scan(&priorHash, &code, &revision, &balance)', '.Scan(&priorHash, &priorConfig, &code, &revision, &balance)')
        store = store.replace('if priorHash != payload {', 'if priorHash != payload || priorConfig != configDigest {')
        store = store.replace('`INSERT INTO receipts VALUES(?,?,?,?,?,?)`', '`INSERT INTO receipts VALUES(?,?,?,?,?,?,?)`')
        store = store.replace('player, id, payload, code, revision, balance)', 'player, id, payload, configDigest, code, revision, balance)')
        old_transition = '''code = "REJECTED"
	if expected != revision {
		code = "REVISION_CONFLICT"
	} else if action == "claim_starter" && !claimed {
		if balance > 9223372036854775807-reward {
			return "", 0, 0, errors.New("balance overflow")
		}
		balance += reward
		revision++
		code = "APPLIED"
		if _, e = tx.ExecContext(ctx, `UPDATE players SET revision=?,balance=?,starter_claimed=TRUE WHERE player=?`, revision, balance, player); e != nil {
			return "", 0, 0, e
		}
	}'''
        new_transition = '''if configVersion != activeConfigVersion { return "", 0, 0, starterdomain.ErrConfigurationVersion }
	state, code, ruleErr := starterdomain.Transition(starterdomain.State{Revision: revision, Balance: balance, Claimed: claimed}, starterdomain.Command{OperationID: id, Action: action, ConfigVersion: configVersion, ExpectedRevision: expected}, reward)
	if ruleErr != nil { return "", 0, 0, ruleErr }
	revision, balance, claimed = state.Revision, state.Balance, state.Claimed
	if code == "APPLIED" {
		if _, e = tx.ExecContext(ctx, `UPDATE players SET revision=?,balance=?,starter_claimed=TRUE WHERE player=?`, revision, balance, player); e != nil {
			return "", 0, 0, e
		}
	}'''
        store = store.replace(old_transition, new_transition)
        store += '''

func Command(ctx context.Context, db *sql.DB, player, id, action string, expected, reward int64) (string, int64, int64, error) {
    return CommandWithConfig(ctx, db, player, id, action, expected, reward, "", "")
}

type MySQLStarterRepository struct { DB *sql.DB }
func (r *MySQLStarterRepository) Execute(ctx context.Context, player string, command starterdomain.Command, amount int64, activeConfigVersion string) (starterdomain.Result, error) {
    code, revision, balance, err := CommandWithConfig(ctx, r.DB, player, command.OperationID, command.Action, command.ExpectedRevision, amount, command.ConfigVersion, activeConfigVersion)
    return starterdomain.Result{Code: code, Revision: revision, Balance: balance}, err
}
'''
        files["internal/platform/store.go"] = store
    if "internal/platform/rpc.go" in files:
        rpc = str(files["internal/platform/rpc.go"])
        rpc = rpc.replace(f'"{module_path}/internal/demo"', f'"{module_path}/internal/configv3"')
        rpc = rpc.replace(f'"{module_path}/internal/configv3"', f'configv3 "{module_path}/internal/configv3"\n\tstarterapp "{module_path}/internal/games/starter/application"')
        rpc = rpc.replace("demo.Config", "configv3.RootType")
        rpc = rpc.replace('''		if q.ConfigVersion != s.Config.Version {
			return nil, status.Error(codes.FailedPrecondition, "configuration version mismatch")
		}
''', '')
        rpc = rpc.replace('''		if q.Action != "claim_starter" {
			return nil, status.Error(codes.Unimplemented, "game method")
		}
''', '')
        rpc = rpc.replace("DB                    *sql.DB", "DB                    *sql.DB\n\tStarter               *starterapp.Service")
        old_command = '''code, rev, balance, e := Command(ctx, s.DB, player, q.OperationId, q.Action, q.ExpectedRevision, s.Config.StarterReward)
		if e != nil {
			return nil, Unavailable()
		}
		return &pb.Reply{Code: code, Revision: rev, Balance: balance, ConfigVersion: s.Config.Version}, nil'''
        new_command = '''if s.Starter == nil { return nil, Unavailable() }
		result, e := s.Starter.Execute(ctx, player, starterapp.Command{OperationID: q.OperationId, Action: q.Action, ConfigVersion: q.ConfigVersion, ExpectedRevision: q.ExpectedRevision})
		if errors.Is(e, starterapp.ErrConfigurationVersion) { return nil, status.Error(codes.FailedPrecondition, "configuration version mismatch") }
        if errors.Is(e, starterapp.ErrUnsupportedAction) { return nil, status.Error(codes.Unimplemented, "game method") }
		if e != nil { return nil, Unavailable() }
		return &pb.Reply{Code: result.Code, Revision: result.Revision, Balance: result.Balance, ConfigVersion: s.Config.Version}, nil'''
        rpc = rpc.replace(old_command, new_command)
        files["internal/platform/rpc.go"] = rpc
    files["DEPENDENCIES.md"] = (seed / "DEPENDENCIES.md").read_text("utf-8") + "\n\nGenerated from the MIT-licensed GREA finite development template. GREA-specific package/env/database identifiers were parameterized. The synthetic `claim_starter` sample exists only to demonstrate transaction idempotency; it is not a product policy.\n"
    files["clients/UnityGateway.cs"] = _unity_client()
    if "internal/platform/rpc.go" in files:
        rpc = str(files["internal/platform/rpc.go"])
        rpc = re.sub(r'case "billing":.*?\n\t}\n\treturn nil, status.Error\(codes.Unimplemented, "role"\)', 'case "billing":\n\t\treturn nil, status.Error(codes.Unavailable, "payment provider and fulfillment integration unavailable")\n\t}\n\treturn nil, status.Error(codes.Unimplemented, "role")', rpc, flags=re.S)
        files["internal/platform/rpc.go"] = rpc
    files["LICENSES/GREA-MIT.txt"] = (Path(__file__).parent / "licenses" / "GREA-MIT.txt").read_bytes()
    manifest = _write_new(Path(output), files)
    formatted = _format_generated_go(Path(output))
    if formatted: manifest = _manifest(Path(output))
    receipt = {"schemaVersion": 1, "status": "generated_candidate_not_runtime_verified", "modulePath": module_path,
               "services": list(SERVICES), "internalProtocol": "grea.example.v1", "payment": "unavailable",
               "unityClient": {"contract": "beaver.unity-gateway/1", "path": "clients/UnityGateway.cs",
                               "sha256": _sha(str(files["clients/UnityGateway.cs"]).encode("utf-8")),
                               "capabilities": ["login", "state_read", "command", "in_memory_raw_reply_sha256"]},
               "configuration": config_identity, "protocolGeneration": proto_identity, "gofmtApplied": formatted,
               "seedManifestSha256": _sha((seed / "SOURCE-MANIFEST.json").read_bytes()), "files": manifest}
    (Path(output).expanduser().absolute() / "beaver-server-go.json").write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
    return receipt


def _module_files(name: str, owner: str = "server", module_path: str = "example.invalid/game/server") -> dict[str, str]:
    root = f"internal/games/{name}"
    return {
        f"{root}/domain/command.go": _domain(name),
        f"{root}/domain/command_test.go": '''package domain

import "testing"

func TestMissingRuleNeverReturnsSuccess(t *testing.T) {
    result, err := Apply(Command{})
    if err == nil || result.Code != "UNAVAILABLE" { t.Fatalf("missing rule fabricated a result: %#v %v", result, err) }
}
''',
        f"{root}/application/service.go": f'''package application

import (
    "context"
    "errors"
    "strings"
    "{module_path}/internal/games/{name}/domain"
    "{module_path}/internal/games/{name}/repository"
)

var ErrInvalidCommand = errors.New("operation ID, expected revision, config version and action are required")
type Service struct {{ repo repository.Repository }}
func New(repo repository.Repository) *Service {{ return &Service{{repo: repo}} }}
func (s *Service) Execute(ctx context.Context, verifiedPlayer string, command domain.Command) (domain.Result, error) {{
    if s == nil || s.repo == nil || strings.TrimSpace(verifiedPlayer) == "" || strings.TrimSpace(command.OperationID) == "" || len(command.OperationID) > 96 || command.ExpectedRevision < 0 || command.ConfigVersion == "" || command.Action == "" {{ return domain.Result{{}}, ErrInvalidCommand }}
    return s.repo.Execute(ctx, verifiedPlayer, command)
}}
''',
        f"{root}/repository/repository.go": f'''package repository

import (
    "context"
    "{module_path}/internal/games/{name}/domain"
)

// Implement in the module's declared data owner using one MySQL transaction for state and receipt.
// Do not issue cross-owner SQL; other services communicate over the versioned gRPC contract.
type Repository interface {{ Execute(context.Context, string, domain.Command) (domain.Result, error) }}
''',
        f"{root}/transport/register.go": f'''package transport

import (
    "context"
    "errors"
    "{module_path}/internal/games/{name}/application"
    "{module_path}/internal/games/{name}/domain"
)

const ModuleName = "{name}"
const DataOwner = "{owner}"
var ErrDuplicateRegistration = errors.New("game module already registered")
type Handler struct {{ service *application.Service }}
func New(service *application.Service) *Handler {{ return &Handler{{service: service}} }}
func (h *Handler) Execute(ctx context.Context, verifiedPlayer string, command domain.Command) (domain.Result, error) {{ if h == nil || h.service == nil {{ return domain.Result{{}}, application.ErrInvalidCommand }}; return h.service.Execute(ctx, verifiedPlayer, command) }}
type Registry struct {{ handlers map[string]*Handler }}
func NewRegistry() *Registry {{ return &Registry{{handlers: map[string]*Handler{{}}}} }}
func (r *Registry) Register(name string, handler *Handler) error {{
    if r == nil || name == "" || handler == nil {{ return errors.New("valid module name and handler required") }}
    if r.handlers == nil {{ r.handlers = map[string]*Handler{{}} }}
    if _, exists := r.handlers[name]; exists {{ return ErrDuplicateRegistration }}
    r.handlers[name] = handler; return nil
}}
func (r *Registry) Resolve(name string) (*Handler, bool) {{ if r == nil {{ return nil, false }}; handler, ok := r.handlers[name]; return handler, ok }}
''',
        f"{root}/transport/register_test.go": '''package transport

import "testing"

func TestDuplicateModuleRegistrationIsRejected(t *testing.T) {
    registry := NewRegistry()
    if err := registry.Register(ModuleName, &Handler{}); err != nil { t.Fatal(err) }
    if err := registry.Register(ModuleName, &Handler{}); err != ErrDuplicateRegistration { t.Fatalf("duplicate registration accepted: %v", err) }
}
''',
        f"migrations/{owner}/0001_{name}_receipts.sql": f'''-- Add owner-specific state tables and indexes in a reviewed follow-up migration.
-- This migration records the module command fingerprint/result in the declared MySQL owner only.
CREATE TABLE {name}_command_receipts (
  player_id VARCHAR(96) NOT NULL,
  operation_id VARCHAR(96) NOT NULL,
  payload_sha256 CHAR(64) NOT NULL,
  config_sha256 CHAR(64) NOT NULL,
  result_json JSON NULL,
  committed_at TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  PRIMARY KEY (player_id, operation_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
''',
        f"{root}/README.md": f"# {name} module candidate\n\nData owner: `{owner}`. No game rules or numeric configuration values are generated. The shared configuration-v3 output supplies typed configuration; implement the repository with a single owner-scoped MySQL transaction and persist command fingerprint, configuration snapshot hash, state changes and result together. Register this module explicitly at compile time. This candidate does not modify an existing project.\n"
    }


def _unity_client() -> str:
    return r"""using System;
using System.Threading.Tasks;
using System.Security.Cryptography;
using UnityEngine.Networking;

namespace Beaver.ServerGo
{
    // Names intentionally match legacy ProtoJSON fields in api/game.proto.
    [Serializable] public sealed class CommandRequest
    {
        public string operationId;
        public string expectedRevision; // int64 is a decimal JSON string
        public string action;
        public string configVersion;
    }
    [Serializable] public sealed class CommandReply
    {
        public string code;
        public string revision; // int64 is a decimal JSON string
        public string balance;
        public string configVersion;
        public string sessionId;
    }
    [Serializable] public sealed class LoginRequest { public string credential; }

    // Persist this immutable request before sending. After an unknown response, reuse the same body and operationId.
    public sealed class UnityGateway
    {
        private readonly string root;
        private readonly string session;
        public string LastReplyBodySha256 { get; private set; }
        public UnityGateway(string root, string verifiedSession)
        {
            this.root = string.IsNullOrWhiteSpace(root) ? throw new ArgumentException("gateway URL required") : root.TrimEnd('/');
            session = string.IsNullOrWhiteSpace(verifiedSession) ? throw new ArgumentException("verified session required") : verifiedSession;
        }
        public static string SnapshotVersion(string version, string inputSha256, string schemaSha256)
        {
            if (string.IsNullOrWhiteSpace(version) || inputSha256 == null || inputSha256.Length != 64 || schemaSha256 == null || schemaSha256.Length != 64)
                throw new ArgumentException("Version and configuration snapshot hashes are required");
            return version + "@" + inputSha256 + ":" + schemaSha256;
        }
        public static async Task<string> Login(string root, string developmentCredential)
        {
            using (var request = new UnityWebRequest(root.TrimEnd('/') + "/v1/login", "POST"))
            {
                request.uploadHandler = new UploadHandlerRaw(System.Text.Encoding.UTF8.GetBytes(UnityEngine.JsonUtility.ToJson(new LoginRequest { credential = developmentCredential })));
                request.downloadHandler = new DownloadHandlerBuffer(); request.timeout = 5;
                request.SetRequestHeader("Content-Type", "application/json");
                var operation = request.SendWebRequest(); while (!operation.isDone) await Task.Yield();
                if (request.result != UnityWebRequest.Result.Success) throw new Exception("Development login transport failed");
                var reply = UnityEngine.JsonUtility.FromJson<CommandReply>(request.downloadHandler.text);
                if (reply == null || reply.code != "OK" || string.IsNullOrWhiteSpace(reply.sessionId)) throw new Exception("Development login rejected");
                return reply.sessionId;
            }
        }
        public async Task<CommandReply> Command(string immutableJson)
        {
            using (var request = new UnityWebRequest(root + "/v1/command", "POST"))
            {
                request.uploadHandler = new UploadHandlerRaw(System.Text.Encoding.UTF8.GetBytes(immutableJson));
                request.downloadHandler = new DownloadHandlerBuffer(); request.timeout = 5;
                request.SetRequestHeader("Content-Type", "application/json");
                request.SetRequestHeader("Authorization", "Bearer " + session);
                var operation = request.SendWebRequest(); while (!operation.isDone) await Task.Yield();
                if (request.result != UnityWebRequest.Result.Success)
                    throw new Exception("Result may be unknown; retain and retry the same operationId and exact request content");
                var replyBody = request.downloadHandler.text;
                LastReplyBodySha256 = Sha256(replyBody);
                var reply = UnityEngine.JsonUtility.FromJson<CommandReply>(replyBody);
                return reply ?? throw new Exception("Empty gateway reply");
            }
        }

        public async Task<CommandReply> State()
        {
            using (var request = new UnityWebRequest(root + "/v1/state", "POST"))
            {
                request.uploadHandler = new UploadHandlerRaw(System.Text.Encoding.UTF8.GetBytes("{}"));
                request.downloadHandler = new DownloadHandlerBuffer(); request.timeout = 5;
                request.SetRequestHeader("Content-Type", "application/json");
                request.SetRequestHeader("Authorization", "Bearer " + session);
                var operation = request.SendWebRequest(); while (!operation.isDone) await Task.Yield();
                if (request.result != UnityWebRequest.Result.Success)
                    throw new Exception("State preflight transport failed");
                var replyBody = request.downloadHandler.text;
                LastReplyBodySha256 = Sha256(replyBody);
                var reply = UnityEngine.JsonUtility.FromJson<CommandReply>(replyBody);
                return reply ?? throw new Exception("Empty state reply");
            }
        }

        // Only raw non-login command reply bytes are exposed as a digest; response bodies and session material stay in memory.
    private static string Sha256(string value)
    {
        using (var sha = SHA256.Create())
        {
            var hash = sha.ComputeHash(System.Text.Encoding.UTF8.GetBytes(value));
            var result = new System.Text.StringBuilder(hash.Length * 2);
            foreach (var item in hash) result.Append(item.ToString("x2"));
            return result.ToString();
        }
    }
    }
}
"""


def _receipt_migration() -> str:
    return '''CREATE TABLE command_receipts (
  scope_id VARCHAR(96) NOT NULL,
  command_id VARCHAR(96) NOT NULL,
  payload_sha256 CHAR(64) NOT NULL,
  result_json JSON NULL,
  committed_at TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  PRIMARY KEY (scope_id, command_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
'''


def _idempotency() -> str:
    return '''package transaction

import (
    "context"
    "crypto/sha256"
    "database/sql"
    "encoding/hex"
    "encoding/json"
    "errors"
    "fmt"
)

var ErrCommandIDConflict = errors.New("command ID reused with different content")

type Command struct { ScopeID string; CommandID string; CanonicalPayload []byte }
type Apply func(context.Context, *sql.Tx) (any, error)

// Execute persists the operation result in the same MySQL transaction as state changes.
// Callers must pass canonical typed payload bytes, stable for the life of this intent.
func Execute(ctx context.Context, db *sql.DB, command Command, apply Apply) (json.RawMessage, error) {
    if command.ScopeID == "" || command.CommandID == "" || len(command.CanonicalPayload) == 0 || apply == nil { return nil, errors.New("incomplete command") }
    sum := sha256.Sum256(command.CanonicalPayload); digest := hex.EncodeToString(sum[:])
    tx, err := db.BeginTx(ctx, &sql.TxOptions{Isolation: sql.LevelReadCommitted}); if err != nil { return nil, err }
    defer tx.Rollback()
    _, err = tx.ExecContext(ctx, `INSERT INTO command_receipts(scope_id,command_id,payload_sha256,result_json) VALUES(?,?,?,NULL) ON DUPLICATE KEY UPDATE command_id=VALUES(command_id)`, command.ScopeID, command.CommandID, digest)
    if err != nil { return nil, err }
    var storedHash string; var storedResult sql.NullString
    err = tx.QueryRowContext(ctx, `SELECT payload_sha256,result_json FROM command_receipts WHERE scope_id=? AND command_id=? FOR UPDATE`, command.ScopeID, command.CommandID).Scan(&storedHash, &storedResult)
    if err != nil { return nil, err }
    if storedHash != digest { return nil, ErrCommandIDConflict }
    if storedResult.Valid {
        result := json.RawMessage(storedResult.String)
        if err = tx.Commit(); err != nil { return nil, err }
        return result, nil
    }
    value, err := apply(ctx, tx); if err != nil { return nil, err }
    result, err := json.Marshal(value); if err != nil { return nil, err }
    if _, err = tx.ExecContext(ctx, `UPDATE command_receipts SET result_json=? WHERE scope_id=? AND command_id=?`, string(result), command.ScopeID, command.CommandID); err != nil { return nil, err }
    if err = tx.Commit(); err != nil { return nil, fmt.Errorf("commit outcome unknown; query or retry with same intent ID and content: %w", err) }
    return result, nil
}
'''


def generate_module(spec: dict[str, Any], output: str | os.PathLike[str]) -> dict[str, Any]:
    name = _module_name(spec)
    module_path = spec.get("modulePath", "example.invalid/game/server")
    _go_mod(module_path)
    owner = spec.get("owner", "server")
    if owner not in OWNERS: raise ValueError("spec.owner must be one of login/server/chat/world/billing")
    files = _module_files(name, owner, module_path)
    manifest = _write_new(Path(output), files)
    formatted = _format_generated_go(Path(output))
    if formatted: manifest = _manifest(Path(output))
    result = {"schemaVersion": 1, "status": "generated_candidate_not_runtime_verified", "module": name, "modulePath": module_path, "gofmtApplied": formatted, "files": manifest, "limitations": ["无玩法规则", "接入点需由服务所有者显式注册", "不写现有工程"]}
    (Path(output).expanduser().absolute() / "beaver-module.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    return result


def module_plan(project: str | os.PathLike[str], spec: dict[str, Any], output: str | os.PathLike[str]) -> dict[str, Any]:
    """Describe an additive module proposal; never writes to project."""
    report = inspect(project)
    name = _module_name(spec)
    root = Path(project).expanduser().resolve()
    owner = spec.get("owner", "server")
    if owner not in OWNERS: raise ValueError("spec.owner must be one of login/server/chat/world/billing")
    reads = []
    for p in sorted(root.rglob("*")):
        if not p.is_file() or p.is_symlink(): continue
        rel = p.relative_to(root).as_posix()
        relevant = (rel in {"go.mod", "go.sum"} or rel.startswith(f"internal/games/{name}/") or
                    rel.startswith("internal/services/server/") or rel.startswith("cmd/server/") or
                    rel.startswith(f"migrations/{owner}/") or
                    rel.endswith(".proto") or (rel.startswith("configs/") and ("schema" in Path(rel).name or "manifest" in Path(rel).name)))
        if relevant:
            reads.append(_file(p, root))
    module_path = spec.get("modulePath", report.get("modulePath") or "example.invalid/game/server")
    _go_mod(module_path)
    proposed = _module_files(name, owner, module_path)
    result = {"schemaVersion": 1, "status": "plan_only", "project": str(root), "module": name,
              "projectInspectionSha256": _sha(json.dumps(report, sort_keys=True).encode()), "reads": reads,
              "writes": [{"path": path, "bytes": len(body.encode("utf-8")), "sha256": _sha(body.encode("utf-8")), "content": body} for path, body in sorted(proposed.items())], "requiresOwnerRegistration": True,
              "limitations": ["No project mutation", "Primary service registration and migration owners must approve generated file contents"]}
    raw = json.dumps(result, indent=2, ensure_ascii=False).encode("utf-8")
    out = Path(output).expanduser().absolute()
    if out.exists() or not out.parent.is_dir(): raise ValueError("plan output must be a new file with an existing parent")
    out.write_bytes(raw)
    result["planSha256"] = _sha(raw)
    return result


_FIELD = re.compile(r"(?:(?<=^)|(?<=[;{}]))\s*(?:(?:optional|required|repeated)\s+)?([.\w<> ,]+?)\s+(\w+)\s*=\s*(\d+)\s*(?:\[(.*?)\])?\s*;", re.M)
_MESSAGE = re.compile(r"\bmessage\s+(\w+)\s*\{")
_SERVICE = re.compile(r"\bservice\s+(\w+)\s*\{")
_RPC = re.compile(r"\brpc\s+(\w+)\s*\(\s*(stream\s+)?([.\w]+)\s*\)\s*returns\s*\(\s*(stream\s+)?([.\w]+)\s*\)")


def _proto_blocks(text: str, pattern: re.Pattern[str]) -> list[tuple[re.Match[str], str]]:
    """Extract balanced proto blocks after removing line comments; bounded parser, not a compiler."""
    text = re.sub(r"(?m)//.*$", "", text)
    found = []
    for match in pattern.finditer(text):
        start = match.end()
        depth = 1
        end = start
        while end < len(text) and depth:
            if text[end] == "{": depth += 1
            elif text[end] == "}": depth -= 1
            end += 1
        if depth == 0: found.append((match, text[start:end - 1]))
    return found


def _proto(path: str | os.PathLike[str]) -> dict[str, Any]:
    text = Path(path).read_text("utf-8")
    text = re.sub(r"(?m)//.*$", "", text)
    package = (re.search(r"(?m)^\s*package\s+([.\w]+)\s*;", text) or [None, ""])[1]
    fields = []
    reserved_numbers: set[int] = set()
    reserved_names: set[str] = set()
    reserved_by_message: dict[str, dict[str, list[Any]]] = {}
    messages = _proto_blocks(text, _MESSAGE)
    for match, body in messages:
        message = match.group(1)
        message_numbers: set[int] = set(); message_names: set[str] = set()
        for field in _FIELD.finditer(body):
            fields.append({"message": message, "type": " ".join(field[1].split()), "name": field[2], "number": int(field[3]), "options": field[4] or ""})
        for statement in re.findall(r"\breserved\s+([^;]+);", body):
            for value in re.findall(r'"([^"\\]*(?:\\.[^"\\]*)*)"', statement):
                reserved_names.add(value); message_names.add(value)
            numeric = re.sub(r'"[^"\\]*(?:\\.[^"\\]*)*"', "", statement)
            for lo, hi in re.findall(r"(\d+)(?:\s+to\s+(\d+|max))?", numeric):
                first = int(lo); last = 536870911 if hi == "max" else int(hi or lo)
                if last - first > 100000: continue
                reserved_numbers.update(range(first, last + 1)); message_numbers.update(range(first, last + 1))
        reserved_by_message[message] = {"numbers": sorted(message_numbers), "names": sorted(message_names)}
    methods = []
    for match, body in _proto_blocks(text, _SERVICE):
        service = match.group(1)
        for rpc in _RPC.finditer(body):
            methods.append({"service": service, "method": rpc[1], "input": rpc[3], "inputStream": bool(rpc[2]), "output": rpc[5], "outputStream": bool(rpc[4])})
    return {"package": package, "fields": sorted(fields, key=lambda f: (f["message"], f["number"])),
            "reservedNumbers": sorted(reserved_numbers), "reservedNames": sorted(reserved_names), "reservedByMessage": reserved_by_message,
            "methods": sorted(methods, key=lambda m: (m["service"], m["method"]))}


def protocol_diff(before: str | os.PathLike[str], after: str | os.PathLike[str]) -> dict[str, Any]:
    a, b = _proto(before), _proto(after)
    af = {(f["message"], f["number"]): f for f in a["fields"]}; bf = {(f["message"], f["number"]): f for f in b["fields"]}
    issues = []
    if a["package"] != b["package"]: issues.append({"severity": "breaking", "kind": "package_changed", "before": a["package"], "after": b["package"]})
    for key, old in af.items():
        new = bf.get(key)
        message_reserved = b["reservedByMessage"].get(old["message"], {"numbers": [], "names": []})
        if new is None and old["number"] not in message_reserved["numbers"] and old["name"] not in message_reserved["names"]:
            issues.append({"severity": "breaking", "kind": "deleted_field_not_reserved", "message": old["message"], "number": old["number"], "name": old["name"]})
        elif new and (new["name"] != old["name"] or new["type"] != old["type"]):
            issues.append({"severity": "breaking", "kind": "field_number_reused_or_changed", "message": old["message"], "number": old["number"], "before": old, "after": new})
    for field in b["fields"]:
        previous_reserved = a["reservedByMessage"].get(field["message"], {"numbers": [], "names": []})
        if field["number"] in previous_reserved["numbers"] or field["name"] in previous_reserved["names"]:
            issues.append({"severity": "breaking", "kind": "reserved_field_number_or_name_reused", "message": field["message"], "number": field["number"], "name": field["name"]})
    for field in b["fields"]:
        current_reserved = b["reservedByMessage"].get(field["message"], {"numbers": [], "names": []})
        if (field["message"], field["number"]) not in af and field["number"] not in current_reserved["numbers"] and field["name"] not in current_reserved["names"]:
            issues.append({"severity": "compatible_addition_review", "kind": "added_field", "message": field["message"], "number": field["number"], "name": field["name"]})
    oldm = {(m["service"], m["method"]): m for m in a["methods"]}; newm = {(m["service"], m["method"]): m for m in b["methods"]}
    for key, old in oldm.items():
        new = newm.get(key)
        if new is None: issues.append({"severity": "breaking", "kind": "removed_rpc_method", "service": key[0], "method": key[1]})
        elif new != old: issues.append({"severity": "breaking", "kind": "rpc_signature_changed", "before": old, "after": new})
    for key in sorted(set(newm) - set(oldm)):
        issues.append({"severity": "compatible_addition_review", "kind": "added_rpc_method", "service": key[0], "method": key[1]})
    return {"schemaVersion": 1, "status": "breaking" if any(x["severity"] == "breaking" for x in issues) else "compatible_or_review", "before": a, "after": b, "issues": issues, "limitations": ["Finite proto parser; use fixed protoc descriptor checks for compiler-grade validation", "Proto imports/options/oneof/map semantics require descriptor validation"]}


def migration_plan(project: str | os.PathLike[str], output: str | os.PathLike[str]) -> dict[str, Any]:
    """Read migration files and history identity, report risk; never execute SQL."""
    root = Path(project).expanduser().resolve(strict=True)
    migration_root = root / "migrations"
    migrations = []
    if migration_root.is_dir():
        for path in sorted(migration_root.rglob("*.sql")):
            if path.is_symlink() or not path.is_file(): continue
            raw = path.read_bytes(); sql = raw.decode("utf-8", errors="replace")
            risk = []
            for pattern, label in ((r"(?i)\bDROP\s+(TABLE|DATABASE|COLUMN)\b", "destructive_drop"),
                                   (r"(?i)\bTRUNCATE\b", "destructive_truncate"),
                                   (r"(?i)\bRENAME\s+TABLE\b", "rename_requires_rollback_plan"),
                                   (r"(?i)\b(?:UPDATE|DELETE)\b", "data_mutation_requires_restore_plan")):
                if re.search(pattern, sql): risk.append(label)
            relative = path.relative_to(root).as_posix()
            parts = Path(relative).parts
            migrations.append({"path": relative, "owner": parts[1] if len(parts) > 2 and parts[0] == "migrations" else None,
                               "bytes": len(raw), "sha256": _sha(raw), "risks": risk})
    history = next((p for p in (root / "migrations").glob("*history*") if p.is_file() and not p.is_symlink()), None) if migration_root.is_dir() else None
    result = {"schemaVersion": 1, "status": "plan_only", "project": str(root), "migrations": migrations,
              "history": _file(history, root) if history else None, "execution": "not_run",
              "requiredBeforeApply": ["isolated development database identity", "backup and restore proof for destructive operations", "migration version ledger", "explicit plan hash and target database identity"],
              "limitations": ["Static SQL risk scan only", "No SQL connection or migration execution"]}
    out = Path(output).expanduser().absolute()
    if out.exists() or not out.parent.is_dir(): raise ValueError("migration plan output must be a new file")
    plan_bytes = (json.dumps(result, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
    result["planSha256"] = _sha(plan_bytes)
    out.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return result


def _no_symlink_path(value: str | os.PathLike[str], *, strict: bool = True) -> Path:
    path = Path(value).expanduser().absolute()
    cursor = Path(path.anchor)
    for part in path.parts[1:]:
        cursor = cursor / part
        if cursor.is_symlink(): raise ValueError("candidate, plan, and Compose paths may not traverse symlinks")
    return path.resolve(strict=strict)


def _candidate_binding(project: str | os.PathLike[str]) -> tuple[Path, str]:
    root = _no_symlink_path(project)
    marker_path = root / "beaver-server-go.json"
    if not marker_path.is_file(): raise ValueError("candidate root lacks beaver-server-go.json")
    marker = json.loads(marker_path.read_text("utf-8"))
    listed = marker.get("files")
    if not isinstance(listed, list) or not listed: raise ValueError("candidate manifest has no generated file inventory")
    for item in listed:
        rel = item.get("path")
        if not isinstance(rel, str) or Path(rel).is_absolute() or ".." in Path(rel).parts:
            raise ValueError("candidate manifest contains an unsafe path")
        path = _no_symlink_path(root / rel)
        if not path.is_file() or _sha(path.read_bytes()) != item.get("sha256"):
            raise ValueError(f"candidate file differs from manifest: {rel}")
    return root, _sha(marker_path.read_bytes())


def _compose_target(compose_file: str | os.PathLike[str], project_name: str, owner: str) -> tuple[Path, str]:
    path = _no_symlink_path(compose_file)
    if not re.fullmatch(r"beaver-go-[a-z0-9-]{6,32}", project_name): raise ValueError("migration requires a unique beaver-go project name")
    if owner not in OWNERS: raise ValueError("migration owner must be login/server/chat/world/billing")
    compose = json.loads(path.read_text("utf-8"))
    if compose.get("name") != project_name: raise ValueError("Compose project identity does not match explicit target")
    mysql = compose.get("services", {}).get("mysql", {})
    private_network = compose.get("networks", {}).get("private", {})
    binding = compose.get("x-beaver-candidate", {})
    expected_mysql_image = f"{project_name}:mysql-bootstrap"
    mysql_build = mysql.get("build", {})
    dockerfile = path.parent / "deploy/mysql/Dockerfile"
    docker_text = dockerfile.read_text("utf-8") if dockerfile.is_file() else ""
    if (mysql.get("image") != expected_mysql_image or mysql_build.get("context") != "." or
            mysql_build.get("dockerfile") != "deploy/mysql/Dockerfile" or f"FROM {MYSQL_IMAGE}" not in docker_text or
            "RUN chmod 0555 /docker-entrypoint-initdb.d/001-create-databases.sh" not in docker_text or
            "mysql_data:/var/lib/mysql" not in mysql.get("volumes", []) or
            mysql.get("ports") or mysql.get("networks") != ["private"] or not private_network.get("internal") or
            compose.get("networks", {}).get("edge", {}).get("internal") is not False or
            compose.get("networks", {}).get("edge", {}).get("driver") != "bridge" or
            compose.get("networks", {}).get("edge", {}).get("labels", {}).get("beaver.owner") != "beaver-v03-server-go" or
            compose.get("networks", {}).get("edge", {}).get("labels", {}).get("beaver.project") != project_name or
            compose.get("services", {}).get("gateway", {}).get("networks") != ["private", "edge"]):
        raise ValueError("migration Compose target is not the isolated pinned Beaver MySQL service")
    volume = compose.get("volumes", {}).get("mysql_data", {}).get("labels", {})
    if volume.get("beaver.owner") != "beaver-v03-server-go" or volume.get("beaver.project") != project_name:
        raise ValueError("migration target volume owner/project identity mismatch")
    candidate, manifest_sha = _candidate_binding(binding.get("root", ""))
    if binding.get("manifestSha256") != manifest_sha or path.parent != candidate:
        raise ValueError("Compose plan is not bound to this exact candidate root and manifest")
    return path, f"beaver_go_example_{owner}"


def _verify_live_compose_target(compose_path: Path, project_name: str) -> tuple[dict[str, Any] | None, str | None]:
    """Verify actual Compose container, named volume and private network labels before any SQL."""
    def run(args: list[str]) -> Any:
        result = subprocess.run(args, capture_output=True, text=True, timeout=20)
        if result.returncode: raise RuntimeError((result.stderr or result.stdout)[-2000:])
        return result.stdout.strip()
    base = ["docker", "compose", "--project-name", project_name, "-f", str(compose_path)]
    try:
        raw = run([*base, "ps", "--format", "json", "mysql"])
        rows = [json.loads(line) for line in raw.splitlines() if line.strip()]
        if len(rows) != 1: raise ValueError("expected exactly one running Compose mysql container")
        container_id = rows[0].get("ID") or rows[0].get("Id")
        if not container_id: raise ValueError("Compose mysql row has no container ID")
        container = json.loads(run(["docker", "inspect", container_id]))[0]
        labels = container.get("Config", {}).get("Labels", {})
        if (labels.get("com.docker.compose.project") != project_name or
                labels.get("com.docker.compose.service") != "mysql" or
                labels.get("beaver.owner") != "beaver-v03-server-go" or labels.get("beaver.project") != project_name):
            raise ValueError("live mysql container ownership labels do not match target")
        if container.get("Config", {}).get("Image") != project_name + ":mysql-bootstrap":
            raise ValueError("live mysql container is not using the candidate-bound bootstrap image")
        image = json.loads(run(["docker", "image", "inspect", container.get("Image", "")]))[0]
        image_labels = image.get("Config", {}).get("Labels", {})
        if (image_labels.get("beaver.owner") != "beaver-v03-server-go" or
                image_labels.get("beaver.mysql.base.digest") != MYSQL_IMAGE.split("@", 1)[1]):
            raise ValueError("live MySQL image labels do not prove the pinned base and owner")
        mounts = [m for m in container.get("Mounts", []) if m.get("Destination") == "/var/lib/mysql" and m.get("Type") == "volume"]
        if len(mounts) != 1: raise ValueError("live mysql container does not have exactly one named data volume")
        volume_name = mounts[0].get("Name")
        volume = json.loads(run(["docker", "volume", "inspect", volume_name]))[0]
        vl = volume.get("Labels", {})
        if (vl.get("beaver.owner") != "beaver-v03-server-go" or vl.get("beaver.project") != project_name or
                vl.get("com.docker.compose.project") != project_name or vl.get("com.docker.compose.volume") != "mysql_data"):
            raise ValueError("live MySQL volume ownership labels do not match target")
        networks = container.get("NetworkSettings", {}).get("Networks", {})
        if len(networks) != 1: raise ValueError("live mysql must attach to exactly the isolated private network")
        network_name = next(iter(networks))
        network = json.loads(run(["docker", "network", "inspect", network_name]))[0]
        nl = network.get("Labels", {})
        if (not network.get("Internal") or nl.get("beaver.owner") != "beaver-v03-server-go" or
                nl.get("beaver.project") != project_name or nl.get("com.docker.compose.project") != project_name or
                nl.get("com.docker.compose.network") != "private"):
            raise ValueError("live private network ownership/internal labels do not match target")
        return {"containerId": container_id, "volume": volume_name, "network": network_name}, None
    except (OSError, subprocess.TimeoutExpired, RuntimeError, ValueError, KeyError, IndexError, json.JSONDecodeError) as exc:
        return None, f"{type(exc).__name__}: {str(exc)[:1500]}"


def inspect_compose_runtime(compose_file: str | os.PathLike[str], project_name: str, expected_gateway_port: int) -> dict[str, Any]:
    """Read actual container health and Docker host-port bindings; this is not an application/business probe."""
    compose_path, _ = _compose_target(compose_file, project_name, "server")
    if not isinstance(expected_gateway_port, int) or isinstance(expected_gateway_port, bool) or not 1024 <= expected_gateway_port <= 65535:
        raise ValueError("expected gateway port must be an unprivileged TCP port")
    base = ["docker", "compose", "--project-name", project_name, "-f", str(compose_path)]
    expected_services = (*SERVICES, "mysql", "redis")
    try:
        ps = subprocess.run([*base, "ps", "--format", "json"], capture_output=True, text=True, timeout=20)
        if ps.returncode: raise RuntimeError((ps.stderr or ps.stdout)[-2000:])
        rows = [json.loads(line) for line in ps.stdout.splitlines() if line.strip()]
        by_service = {row.get("Service"): row for row in rows}
        statuses = []
        host_publishers = []
        network_names: set[str] = set()
        network_memberships: dict[str, list[str]] = {}
        not_ready = []
        for service in expected_services:
            row = by_service.get(service)
            if not row:
                statuses.append({"service": service, "containerStatus": "missing", "healthStatus": "unknown"})
                not_ready.append(service)
                continue
            container_id = row.get("ID") or row.get("Id")
            if not container_id: raise ValueError(f"Compose ps row missing ID for {service}")
            inspected = subprocess.run(["docker", "inspect", container_id], capture_output=True, text=True, timeout=20)
            if inspected.returncode: raise RuntimeError((inspected.stderr or inspected.stdout)[-2000:])
            container = json.loads(inspected.stdout)[0]
            labels = container.get("Config", {}).get("Labels", {})
            if (labels.get("com.docker.compose.project") != project_name or labels.get("com.docker.compose.service") != service or
                    labels.get("beaver.owner") != "beaver-v03-server-go" or labels.get("beaver.project") != project_name):
                raise ValueError(f"live {service} container ownership labels do not match target")
            state = container.get("State", {})
            health = state.get("Health", {}).get("Status", "not_configured")
            container_status = state.get("Status", "unknown")
            statuses.append({"service": service, "containerStatus": container_status, "healthStatus": health})
            if container_status != "running" or health not in ("healthy", "not_configured"):
                not_ready.append(service)
            ports = container.get("NetworkSettings", {}).get("Ports", {}) or {}
            attached = sorted(container.get("NetworkSettings", {}).get("Networks", {}))
            network_memberships[service] = attached
            network_names.update(attached)
            for binding, published in ports.items():
                if not published: continue
                container_port, _, protocol = binding.partition("/")
                for item in published:
                    host_publishers.append({"service": service, "hostIp": item.get("HostIp"),
                        "hostPort": item.get("HostPort"), "containerPort": container_port, "protocol": protocol or "tcp"})
        network_evidence = []
        network_failures = []
        for network_name in sorted(network_names):
            inspected = subprocess.run(["docker", "network", "inspect", network_name], capture_output=True, text=True, timeout=20)
            if inspected.returncode: raise RuntimeError((inspected.stderr or inspected.stdout)[-2000:])
            network = json.loads(inspected.stdout)[0]
            labels = network.get("Labels", {})
            network_type = labels.get("com.docker.compose.network")
            valid = (network_type in ("private", "edge") and labels.get("com.docker.compose.project") == project_name and
                labels.get("beaver.owner") == "beaver-v03-server-go" and labels.get("beaver.project") == project_name and
                network.get("Internal") == (network_type == "private"))
            network_evidence.append({"name": network_name, "composeNetwork": network_type, "internal": network.get("Internal"), "labelsVerified": valid})
            if not valid: network_failures.append(network_name)
        expected_private = f"{project_name}_private"
        expected_edge = f"{project_name}_edge"
        for service, actual in network_memberships.items():
            expected = sorted((expected_private, expected_edge)) if service == "gateway" else [expected_private]
            if actual != expected: network_failures.append(service + ":membership")
        if network_failures: not_ready.append("networks")
        gateway_bindings = [x for x in host_publishers if x["service"] == "gateway"]
        expected_binding = {"service": "gateway", "hostIp": "127.0.0.1", "hostPort": str(expected_gateway_port), "containerPort": "8080", "protocol": "tcp"}
        gateway_publish_ready = gateway_bindings == [expected_binding]
        unexpected = [x for x in host_publishers if x["service"] != "gateway"]
        if not gateway_publish_ready or unexpected: not_ready.append("host_publishers")
        return {"schemaVersion": 1, "status": "runtime_preflight_passed" if not not_ready else "runtime_preflight_failed",
            "projectName": project_name, "services": statuses, "hostPublishers": host_publishers,
            "networks": network_evidence,
            "expectedGatewayBinding": expected_binding, "gatewayPublishReady": gateway_publish_ready,
            "unexpectedHostPublishers": unexpected, "notReady": sorted(set(not_ready)),
            "applicationBusinessProbe": "not_run", "execution": "read_only_runtime_inspection"}
    except (OSError, subprocess.TimeoutExpired, RuntimeError, ValueError, KeyError, IndexError, json.JSONDecodeError) as exc:
        return {"schemaVersion": 1, "status": "needs_action", "projectName": project_name,
            "reason": type(exc).__name__, "diagnostic": str(exc)[:2000], "applicationBusinessProbe": "not_run",
            "execution": "read_only_runtime_inspection"}


def inspect_migration_state(compose_file: str | os.PathLike[str], project_name: str, owner: str) -> dict[str, Any]:
    """Read and hash one explicitly owned schema through its named Compose mysql service."""
    compose_path, database = _compose_target(compose_file, project_name, owner)
    live, live_error = _verify_live_compose_target(compose_path, project_name)
    if live_error:
        return {"schemaVersion": 1, "status": "needs_action", "projectName": project_name, "owner": owner,
                "database": database, "reason": "live_target_verification_failed", "diagnostic": live_error, "execution": "not_run"}
    query = ("SELECT CONCAT('TABLE:',TABLE_NAME) FROM information_schema.tables WHERE table_schema=DATABASE() AND table_type='BASE TABLE' "
             "UNION ALL SELECT CONCAT('COLUMN:',TABLE_NAME,':',ORDINAL_POSITION,':',COLUMN_NAME,':',COLUMN_TYPE,':',IS_NULLABLE,':',COALESCE(COLUMN_DEFAULT,'<NULL>')) FROM information_schema.columns WHERE table_schema=DATABASE() "
             "UNION ALL SELECT CONCAT('INDEX:',TABLE_NAME,':',INDEX_NAME,':',NON_UNIQUE,':',SEQ_IN_INDEX,':',COALESCE(COLUMN_NAME,'')) FROM information_schema.statistics WHERE table_schema=DATABASE() ORDER BY 1")
    account = "beaver_" + owner
    command = "MYSQL_PWD=\"$(cat /run/secrets/mysql_" + owner + "_password)\" mysql --protocol=socket -u" + account + " --database=" + database + " --batch --raw --skip-column-names -e " + shlex.quote(query)
    args = ["docker", "compose", "--project-name", project_name, "-f", str(compose_path), "exec", "-T", "mysql", "sh", "-c", command]
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"schemaVersion": 1, "status": "needs_action", "projectName": project_name, "owner": owner,
                "database": database, "reason": type(exc).__name__, "execution": "not_run"}
    if result.returncode:
        return {"schemaVersion": 1, "status": "needs_action", "projectName": project_name, "owner": owner,
                "database": database, "exitCode": result.returncode, "diagnostic": (result.stderr or result.stdout)[-3000:], "execution": "not_run"}
    lines = sorted(line for line in result.stdout.splitlines() if line)
    tables = sorted(line.removeprefix("TABLE:") for line in lines if line.startswith("TABLE:"))
    # Ledger values are queried separately only when the ledger exists.
    ledger = []
    if "schema_migrations" in tables:
        ledger_query = "SELECT version FROM schema_migrations ORDER BY version"
        ledger_command = "MYSQL_PWD=\"$(cat /run/secrets/mysql_" + owner + "_password)\" mysql --protocol=socket -u" + account + " --database=" + database + " --batch --raw --skip-column-names -e " + shlex.quote(ledger_query)
        try:
            ledger_result = subprocess.run(["docker", "compose", "--project-name", project_name, "-f", str(compose_path), "exec", "-T", "mysql", "sh", "-c", ledger_command], capture_output=True, text=True, timeout=30)
        except (OSError, subprocess.TimeoutExpired) as exc:
            return {"schemaVersion": 1, "status": "needs_action", "projectName": project_name, "owner": owner,
                    "database": database, "reason": type(exc).__name__, "execution": "not_run"}
        if ledger_result.returncode:
            return {"schemaVersion": 1, "status": "needs_action", "projectName": project_name, "owner": owner,
                    "database": database, "diagnostic": (ledger_result.stderr or ledger_result.stdout)[-3000:], "execution": "not_run"}
        ledger = sorted(x for x in ledger_result.stdout.splitlines() if x)
    identity = {"projectName": project_name, "owner": owner, "database": database, "schemaRows": lines,
                "migrationVersions": ledger, "composeSha256": _sha(compose_path.read_bytes())}
    return {"schemaVersion": 1, "status": "read_only_snapshot", **identity,
            "stateSha256": _sha(json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()), "execution": "not_run"}


def execute_migrations(project: str | os.PathLike[str], plan_file: str | os.PathLike[str], compose_file: str | os.PathLike[str],
                       project_name: str, owner: str, expected_state_sha256: str) -> dict[str, Any]:
    """Apply one frozen migration only when source plan and current isolated owner-state hashes match."""
    root, candidate_manifest_sha = _candidate_binding(project)
    plan_path = _no_symlink_path(plan_file)
    plan = json.loads(plan_path.read_text("utf-8"))
    plan_hash = plan.pop("planSha256", None)
    expected_plan_bytes = (json.dumps(plan, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
    if not isinstance(plan_hash, str) or _sha(expected_plan_bytes) != plan_hash:
        raise ValueError("migration plan hash mismatch; regenerate and review the plan")
    if plan.get("status") != "plan_only" or Path(plan.get("project", "")).resolve() != root:
        raise ValueError("migration plan does not authorize this exact candidate")
    compose_path, database = _compose_target(compose_file, project_name, owner)
    migrations = [x for x in plan.get("migrations", []) if x.get("owner") == owner]
    if not migrations: raise ValueError("migration plan has no migrations for this owner")
    inventory = []
    for path in sorted((root / "migrations").rglob("*.sql")):
        raw = path.read_bytes(); text = raw.decode("utf-8", errors="replace")
        rel = path.relative_to(root).as_posix(); parts = Path(rel).parts
        risks = [label for pattern, label in ((r"(?i)\bDROP\s+(TABLE|DATABASE|COLUMN)\b", "destructive_drop"),
                 (r"(?i)\bTRUNCATE\b", "destructive_truncate"), (r"(?i)\bRENAME\s+TABLE\b", "rename_requires_rollback_plan"),
                 (r"(?i)\b(?:UPDATE|DELETE)\b", "data_mutation_requires_restore_plan")) if re.search(pattern, text)]
        inventory.append({"path": rel, "owner": parts[1] if len(parts) > 2 and parts[0] == "migrations" else None,
                          "bytes": len(raw), "sha256": _sha(raw), "risks": risks})
    if inventory != plan.get("migrations"):
        raise ValueError("migration source changed since the reviewed plan")
    selected = sorted(migrations, key=lambda row: row["path"])
    if len(selected) != 1:
        raise ValueError("executor applies one reviewed owner migration per call")
    migration = selected[0]
    if migration.get("risks"): raise ValueError("migration has destructive or rollback-sensitive static risks")
    path = root / migration["path"]
    raw = path.read_bytes(); sql = raw.decode("utf-8")
    if re.search(r"(?i)\b(?:USE|CREATE\s+DATABASE|DROP\s+DATABASE)\b|\b[a-z0-9_]+\s*\.\s*[a-z0-9_]+", sql):
        raise ValueError("cross-schema or database-selection SQL is outside owner-only migration scope")
    scrubbed_sql = re.sub(r"/\*.*?\*/", "", sql, flags=re.S)
    scrubbed_sql = re.sub(r"(?m)^\s*--.*$", "", scrubbed_sql)
    for statement in (part.strip() for part in scrubbed_sql.split(";") if part.strip()):
        if re.search(r"(?i)\b(?:DROP|TRUNCATE|RENAME|UPDATE|DELETE|REPLACE|GRANT|REVOKE|CALL|LOAD\s+DATA)\b", statement):
            raise ValueError("only additive DDL and the migration ledger INSERT are accepted by this executor")
        if re.match(r"(?i)^INSERT\b", statement) and not re.match(r"(?i)^INSERT\s+INTO\s+schema_migrations\b", statement):
            raise ValueError("migration executor permits data writes only to its own version ledger")
        if not re.match(r"(?i)^(CREATE\s+TABLE|ALTER\s+TABLE|CREATE\s+(?:UNIQUE\s+)?INDEX|INSERT\s+INTO\s+schema_migrations)\b", statement):
            raise ValueError("migration contains an unsupported SQL statement")
    versions = re.findall(r"(?is)INSERT\s+INTO\s+schema_migrations\s*\(\s*version\s*\)\s*VALUES\s*\(\s*'([^']+)'\s*\)", sql)
    if len(versions) != 1: raise ValueError("migration must declare exactly one schema_migrations version")
    version = versions[0]
    if not re.fullmatch(r"[0-9]{4,}_[a-z0-9_]+", version): raise ValueError("migration version must use a stable ordered identifier")
    state = inspect_migration_state(compose_path, project_name, owner)
    if state.get("status") != "read_only_snapshot": return {"schemaVersion": 1, "status": "needs_action", "stage": "pre_state", "state": state, "execution": "not_run"}
    if state.get("stateSha256") != expected_state_sha256:
        return {"schemaVersion": 1, "status": "stale_plan", "expectedStateSha256": expected_state_sha256,
                "currentStateSha256": state.get("stateSha256"), "execution": "not_run"}
    if version in state.get("migrationVersions", []):
        return {"schemaVersion": 1, "status": "already_applied", "owner": owner, "database": database, "version": version,
                "migrationSha256": migration["sha256"], "stateSha256": expected_state_sha256, "execution": "not_run"}
    if not state.get("migrationVersions") and state.get("tables"):
        return {"schemaVersion": 1, "status": "partial_or_untracked_database", "owner": owner, "database": database,
                "tables": state["tables"], "stateSha256": expected_state_sha256, "execution": "not_run"}
    if not state.get("migrationVersions") and version != "0001_initial":
        return {"schemaVersion": 1, "status": "migration_order_blocked", "owner": owner, "database": database,
                "required": "0001_initial", "requested": version, "execution": "not_run"}
    live, live_error = _verify_live_compose_target(compose_path, project_name)
    if live_error:
        return {"schemaVersion": 1, "status": "needs_action", "stage": "live_target_verification", "diagnostic": live_error, "execution": "not_run"}
    shell = "MYSQL_PWD=\"$(cat /run/secrets/mysql_" + owner + "_password)\" mysql --protocol=socket -ubeaver_" + owner + " --database=" + database + " --batch --raw"
    args = ["docker", "compose", "--project-name", project_name, "-f", str(compose_path), "exec", "-T", "mysql", "sh", "-c", shell]
    try:
        applied = subprocess.run(args, input=raw, capture_output=True, timeout=120)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"schemaVersion": 1, "status": "execution_unknown_or_failed", "owner": owner, "database": database,
                "version": version, "reason": type(exc).__name__, "stateBeforeSha256": expected_state_sha256,
                "execution": "attempted; inspect database state before retry"}
    after = inspect_migration_state(compose_path, project_name, owner)
    if applied.returncode or after.get("status") != "read_only_snapshot" or version not in after.get("migrationVersions", []):
        return {"schemaVersion": 1, "status": "execution_unknown_or_partial", "owner": owner, "database": database,
                "version": version, "exitCode": applied.returncode, "diagnostic": applied.stderr.decode("utf-8", "replace")[-3000:],
                "stateBeforeSha256": expected_state_sha256, "stateAfter": after,
                "execution": "attempted; do not blindly replay DDL; inspect/restore explicitly"}
    return {"schemaVersion": 1, "status": "applied", "owner": owner, "database": database, "version": version,
            "migrationSha256": migration["sha256"], "planSha256": plan_hash,
            "stateBeforeSha256": expected_state_sha256, "stateAfterSha256": after["stateSha256"],
            "composeProject": project_name, "execution": "verified_migration_ledger_and_schema_state"}


def build_diagnostics(project: str | os.PathLike[str], packages: list[str] | None = None) -> dict[str, Any]:
    """Run bounded local Go formatting/test/build with module downloads explicitly disabled."""
    root = Path(project).expanduser().resolve(strict=True)
    if not (root / "go.mod").is_file(): raise ValueError("project has no go.mod")
    scope = packages or ["./..."]
    if any(not isinstance(x, str) or not re.fullmatch(r"(?:\./)?[A-Za-z0-9_./*+-]+", x) or ".." in Path(x).parts for x in scope):
        raise ValueError("invalid package scope")
    import tempfile
    cache = tempfile.TemporaryDirectory(prefix="beaver-go-build-cache-")
    env = dict(os.environ, GOTOOLCHAIN="local", GOPROXY="off", GOSUMDB="off", GOCACHE=cache.name)
    results = []
    go_files = sorted(str(p) for p in root.rglob("*.go") if p.is_file() and not p.is_symlink())
    commands = [["gofmt", "-d", *go_files], ["go", "test", *scope], ["go", "build", *scope]]
    for args in commands:
        try:
            proc = subprocess.run(args, cwd=root, env=env, capture_output=True, text=True, timeout=180)
            results.append({"command": args, "exitCode": proc.returncode, "stdout": proc.stdout[-12000:], "stderr": proc.stderr[-12000:]})
        except (OSError, subprocess.TimeoutExpired) as exc:
            results.append({"command": args, "error": type(exc).__name__, "detail": str(exc)[:500]})
            break
    cache.cleanup()
    ok = len(results) == 3 and all(x.get("exitCode") == 0 for x in results)
    return {"schemaVersion": 1, "status": "passed" if ok else "failed_or_needs_action", "project": str(root), "packages": scope,
            "networkAccess": "disabled", "goToolchain": results, "sourceSha256": _sha(json.dumps(inspect(root), sort_keys=True).encode()),
            "limitations": ["No integration services started", "No protoc generation or Unity Player check"]}


def build_candidate(project: str | os.PathLike[str], architecture: str = "arm64") -> dict[str, Any]:
    """Build all Linux binaries in staging and atomically no-replace promote the fresh bin/."""
    root, manifest_sha = _candidate_binding(project)
    if architecture not in {"arm64", "amd64"}: raise ValueError("supported container architectures are arm64 and amd64")
    marker = json.loads((root / "beaver-server-go.json").read_text("utf-8"))
    owned = {item["path"] for item in marker["files"]}
    extra_go = sorted(path.relative_to(root).as_posix() for path in root.rglob("*.go")
                      if path.is_file() and not path.is_symlink() and not any(part.startswith(".") for part in path.relative_to(root).parts)
                      and path.relative_to(root).as_posix() not in owned)
    if extra_go: raise ValueError("unmanifested Go source is present: " + ", ".join(extra_go[:12]))
    before = _sha(json.dumps([{"path": item["path"], "sha256": item["sha256"]} for item in marker["files"]],
                             sort_keys=True, separators=(",", ":")).encode())
    bin_dir = root / "bin"
    if bin_dir.is_symlink() or bin_dir.exists():
        raise ValueError("build_candidate is create-only; bin/ already exists or is a symlink")
    env = dict(os.environ, GOTOOLCHAIN="local", GOPROXY="off", GOSUMDB="off", GOOS="linux", GOARCH=architecture, CGO_ENABLED="0")
    cache = tempfile.TemporaryDirectory(prefix="beaver-go-build-cache-")
    env["GOCACHE"] = cache.name
    staging = tempfile.TemporaryDirectory(prefix=".beaver-go-build-", dir=root)
    stage = Path(staging.name)
    built = []
    try:
        for role in (*SERVICES, "healthcheck"):
            output = stage / role
            proc = subprocess.run(["go", "build", "-trimpath", "-ldflags=-buildid=", "-o", str(output), f"./cmd/{role}"],
                                  cwd=root, env=env, capture_output=True, text=True, timeout=180)
            if proc.returncode:
                return {"schemaVersion": 1, "status": "failed_or_needs_action", "role": role,
                        "exitCode": proc.returncode, "diagnostic": (proc.stderr or proc.stdout)[-6000:],
                        "networkAccess": "disabled", "sourceSha256": before, "binCreated": False}
            built.append({"path": f"bin/{role}", "bytes": output.stat().st_size, "sha256": _sha(output.read_bytes())})
        _, after_manifest_sha = _candidate_binding(root)
        if after_manifest_sha != manifest_sha: raise ValueError("candidate manifest changed while binaries were building")
        if bin_dir.is_symlink() or bin_dir.exists():
            return {"schemaVersion": 1, "status": "blocked_existing_output", "project": str(root),
                    "reason": "bin/ appeared while building; staged output discarded", "binCreated": False,
                    "networkAccess": "disabled", "sourceSha256": before}
        from .payload import promote
        promote(stage, bin_dir)
    except (OSError, subprocess.TimeoutExpired, ValueError) as exc:
        return {"schemaVersion": 1, "status": "failed_or_needs_action", "project": str(root),
                "reason": type(exc).__name__, "diagnostic": str(exc)[:4000], "binCreated": False,
                "networkAccess": "disabled", "sourceSha256": before}
    finally:
        staging.cleanup()
        cache.cleanup()
    after = before
    return {"schemaVersion": 1, "status": "built_candidate", "project": str(root), "architecture": architecture,
            "platform": "linux", "binaries": built, "sourceSha256": before, "sourceTreeAfterSha256": after,
            "manifestSha256": manifest_sha, "binCreated": True,
            "toolchain": subprocess.run(["go", "version"], capture_output=True, text=True, timeout=8).stdout.strip(),
            "networkAccess": "disabled", "integration": "not_run"}


def toolchain_inspect() -> dict[str, Any]:
    """Report fixed tool requirements without installing or downloading anything."""
    private_bin = Path(__file__).resolve().parents[2] / ".private/v03/server-go/toolchain-bin"
    protoc_gen_go = shutil.which("protoc-gen-go") or (str(private_bin / "protoc-gen-go") if (private_bin / "protoc-gen-go").is_file() else "protoc-gen-go")
    protoc_gen_go_grpc = shutil.which("protoc-gen-go-grpc") or (str(private_bin / "protoc-gen-go-grpc") if (private_bin / "protoc-gen-go-grpc").is_file() else "protoc-gen-go-grpc")
    checks = {"go": ["go", "version"], "protoc": ["protoc", "--version"],
              "protoc-gen-go": [protoc_gen_go, "--version"],
              "protoc-gen-go-grpc": [protoc_gen_go_grpc, "--version"],
              "docker": ["docker", "version", "--format", "{{.Client.Version}}"],
              "docker-compose": ["docker", "compose", "version"]}
    found = {}
    for key, command in checks.items():
        binary = command[0] if Path(command[0]).is_file() else shutil.which(command[0])
        if not binary:
            found[key] = {"status": "needs_action", "reason": "executable_missing"}
            continue
        try:
            result = subprocess.run(command, capture_output=True, text=True, timeout=8)
            found[key] = {"status": "available" if result.returncode == 0 else "unavailable",
                          "version": (result.stdout or result.stderr).strip()[:400], "exitCode": result.returncode}
        except (OSError, subprocess.TimeoutExpired) as exc:
            found[key] = {"status": "unavailable", "reason": type(exc).__name__}
    expected = {"go": "1.26.2", "protoc": "34.1", "protoc-gen-go": "1.36.12", "protoc-gen-go-grpc": "1.6.2"}
    for key, version in expected.items():
        item = found[key]
        if item["status"] == "available" and version not in item.get("version", ""):
            item["status"] = "needs_action"
            item["reason"] = "version_mismatch"
    return {"schemaVersion": 1, "status": "needs_action" if any(x["status"] != "available" for x in found.values()) else "available",
            "tools": found, "fixedVersions": expected, "downloads": "disabled"}


def prepare_protocol_tools(output: str | os.PathLike[str]) -> dict[str, Any]:
    """Build pinned protoc plugins from already-cached Go modules into the explicit output directory."""
    out = Path(output).expanduser().absolute()
    if out.exists() and out.is_symlink(): raise ValueError("protocol plugin output may not be a symlink")
    out.mkdir(parents=True, exist_ok=True)
    build_temp = tempfile.TemporaryDirectory(prefix="beaver-proto-tools-")
    build_root = Path(build_temp.name)
    go_mod = "module beaver.local/proto-tools\\n\\ngo 1.26.0\\n\\nrequire (\\n\\tgoogle.golang.org/grpc v1.83.2\\n\\tgoogle.golang.org/grpc/cmd/protoc-gen-go-grpc v1.6.2\\n\\tgoogle.golang.org/protobuf v1.36.12\\n)\\n"
    mod_path = build_root / "go.mod"
    if mod_path.exists() and mod_path.read_text("utf-8") != go_mod.replace("\\n", "\n").replace("\\t", "\t"):
        raise ValueError("private protocol tool build manifest differs; review before replacement")
    if not mod_path.exists(): mod_path.write_text(go_mod.replace("\\n", "\n").replace("\\t", "\t"), encoding="utf-8")
    sum_path = build_root / "go.sum"
    seed_sum = Path(__file__).parent / "server_templates/grea_seed/go.sum"
    if not sum_path.exists(): shutil.copyfile(seed_sum, sum_path)
    cache = tempfile.TemporaryDirectory(prefix="beaver-proto-cache-")
    env = dict(os.environ, GOTOOLCHAIN="local", GOPROXY="off", GOSUMDB="off", GOCACHE=cache.name)
    built = []
    for package, filename, mode in (("google.golang.org/protobuf/cmd/protoc-gen-go", "protoc-gen-go", "readonly"),
                                    ("google.golang.org/grpc/cmd/protoc-gen-go-grpc", "protoc-gen-go-grpc", "mod")):
        path = out / filename
        try:
            result = subprocess.run(["go", "build", f"-mod={mode}", "-o", str(path), package], cwd=build_root, env=env,
                                    capture_output=True, text=True, timeout=180)
        except (OSError, subprocess.TimeoutExpired) as exc:
            cache.cleanup(); build_temp.cleanup()
            return {"status": "needs_action", "tool": filename, "reason": type(exc).__name__, "network": "disabled"}
        if result.returncode:
            cache.cleanup(); build_temp.cleanup()
            return {"status": "needs_action", "tool": filename, "reason": "cached_module_missing_or_build_failed",
                    "diagnostic": (result.stderr or result.stdout)[-3000:], "network": "disabled"}
        version = subprocess.run([str(path), "--version"], capture_output=True, text=True, timeout=10)
        built.append({"path": str(path), "sha256": _sha(path.read_bytes()), "version": (version.stdout or version.stderr).strip()})
    cache.cleanup(); build_temp.cleanup()
    return {"schemaVersion": 1, "status": "ready", "tools": built, "network": "disabled",
            "sourceGoModSha256": _sha(mod_path.read_bytes()), "sourceGoSumSha256": _sha(sum_path.read_bytes())}


def _generate_proto(proto_text: str, toolchain: dict[str, str] | None) -> tuple[dict[str, bytes], dict[str, Any]]:
    values = toolchain or {}
    local_bin = Path(__file__).resolve().parents[2] / ".private/v03/server-go/toolchain-bin"
    protoc = values.get("protoc") or shutil.which("protoc")
    go_plugin = values.get("protocGenGo") or shutil.which("protoc-gen-go") or (str(local_bin / "protoc-gen-go") if (local_bin / "protoc-gen-go").is_file() else None)
    grpc_plugin = values.get("protocGenGoGrpc") or shutil.which("protoc-gen-go-grpc") or (str(local_bin / "protoc-gen-go-grpc") if (local_bin / "protoc-gen-go-grpc").is_file() else None)
    missing = [name for name, path in (("protoc", protoc), ("protoc-gen-go", go_plugin), ("protoc-gen-go-grpc", grpc_plugin)) if not path or not Path(path).is_file()]
    if missing: raise ValueError("needs_action: fixed protocol tools missing: " + ", ".join(missing) + "; no downloads attempted")
    def version(path: str, flag: str) -> str:
        result = subprocess.run([path, flag], capture_output=True, text=True, timeout=10)
        if result.returncode: raise ValueError("needs_action: tool version check failed: " + path)
        return (result.stdout or result.stderr).strip()
    versions = {"protoc": version(protoc, "--version"), "protoc-gen-go": version(go_plugin, "--version"), "protoc-gen-go-grpc": version(grpc_plugin, "--version")}
    expected = {"protoc": "34.1", "protoc-gen-go": "v1.36.12", "protoc-gen-go-grpc": "1.6.2"}
    for name, pin in expected.items():
        if pin not in versions[name]: raise ValueError(f"needs_action: {name} pin mismatch; expected {pin}, found {versions[name]}")
    import tempfile
    with tempfile.TemporaryDirectory(prefix="beaver-proto-") as td:
        root = Path(td); proto_dir = root / "api"; proto_dir.mkdir()
        (proto_dir / "game.proto").write_text(proto_text, encoding="utf-8")
        env = dict(os.environ, PATH=str(Path(go_plugin).parent) + os.pathsep + str(Path(grpc_plugin).parent) + os.pathsep + os.environ.get("PATH", ""))
        result = subprocess.run([protoc, "-I", str(proto_dir), f"--go_out={proto_dir}", "--go_opt=paths=source_relative",
                                 f"--go-grpc_out={proto_dir}", "--go-grpc_opt=paths=source_relative", str(proto_dir / "game.proto")],
                                cwd=root, env=env, capture_output=True, text=True, timeout=60)
        if result.returncode: raise ValueError("protoc generation failed: " + (result.stderr or result.stdout)[-3000:])
        files = {"api/game.pb.go": (proto_dir / "game.pb.go").read_bytes(),
                 "api/game_grpc.pb.go": (proto_dir / "game_grpc.pb.go").read_bytes()}
        evidence = {"status": "generated", "versions": versions,
                    "protoSha256": _sha(proto_text.encode("utf-8")),
                    "generated": {name: _sha(raw) for name, raw in files.items()}}
        return files, evidence


def compose_plan(spec: dict[str, Any], output: str | os.PathLike[str]) -> dict[str, Any]:
    """Write a reviewable isolated Compose JSON plan; does not call Docker or create secrets."""
    if not isinstance(spec, dict): raise ValueError("spec must be an object")
    name = spec.get("projectName")
    port = spec.get("gatewayPort")
    if not isinstance(name, str) or not re.fullmatch(r"beaver-go-[a-z0-9-]{6,32}", name): raise ValueError("projectName must be a unique beaver-go-<name>")
    if not isinstance(port, int) or isinstance(port, bool) or not 1024 <= port <= 65535: raise ValueError("gatewayPort must be an unprivileged loopback port")
    owner = "beaver-v03-server-go"
    if not isinstance(spec.get("candidateRoot"), str) or not spec["candidateRoot"]:
        raise ValueError("candidateRoot must bind this Compose plan to an explicit generated candidate")
    candidate_root, candidate_manifest_sha = _candidate_binding(spec["candidateRoot"])
    secrets = {"mysql_root": {"file": ".private/secrets/mysql-root.secret"},
               "internal_key": {"file": ".private/secrets/internal-key.secret"},
               "redis_password": {"file": ".private/secrets/redis-password.secret"},
               "development_credential": {"file": ".private/secrets/development-credential.secret"}}
    for role in OWNERS:
        secrets[f"mysql_{role}_password"] = {"file": f".private/secrets/mysql-{role}-password.secret"}
        secrets[f"mysql_{role}_dsn"] = {"file": f".private/secrets/mysql-{role}-dsn.secret"}
    compose: dict[str, Any] = {"name": name, "x-beaver-candidate": {"root": str(candidate_root), "manifestSha256": candidate_manifest_sha},
        "services": {}, "volumes": {"mysql_data": {"labels": {"beaver.owner": owner, "beaver.project": name}}}}
    compose["services"]["mysql"] = {"image": f"{name}:mysql-bootstrap", "build": {"context": ".", "dockerfile": "deploy/mysql/Dockerfile"},
        "environment": {"MYSQL_ROOT_PASSWORD_FILE": "/run/secrets/mysql_root"},
        "secrets": ["mysql_root", *[f"mysql_{o}_password" for o in OWNERS]],
        "volumes": ["mysql_data:/var/lib/mysql"], "networks": ["private"],
        "healthcheck": {"test": ["CMD", "mysqladmin", "ping", "-h", "127.0.0.1"], "interval": "3s", "timeout": "2s", "retries": 40},
        "mem_limit": "512m", "cpus": 0.4, "labels": {"beaver.owner": owner, "beaver.project": name}}
    compose["services"]["redis"] = {"image": REDIS_IMAGE,
        "command": ["/bin/sh", "-c", 'exec redis-server --requirepass "$$(cat /run/secrets/redis_password)" --maxmemory 16mb --maxmemory-policy noeviction'],
        "secrets": ["redis_password"], "healthcheck": {"test": ["CMD-SHELL", "redis-cli -a \"$$(cat /run/secrets/redis_password)\" ping | grep -q PONG"], "interval": "3s", "timeout": "2s", "retries": 20},
        "networks": ["private"], "mem_limit": "48m", "cpus": 0.1, "labels": {"beaver.owner": owner, "beaver.project": name}}
    image = f"{name}:candidate"
    for role in SERVICES:
        env = {"BEAVER_GO_MODE": "development", "BEAVER_GO_LISTEN": ":8080" if role == "gateway" else ":50051",
               "BEAVER_GO_GAME_CONFIG": "/app/config/game.json", "BEAVER_GO_GAME_SCHEMA": "/app/config/configuration.schema.json",
               "BEAVER_GO_INTERNAL_FILE": "/run/secrets/internal_key"}
        svc: dict[str, Any] = {"image": image, "build": {"context": ".", "dockerfile": "Dockerfile"},
              "entrypoint": [f"/app/bin/{role}"], "environment": env, "secrets": ["internal_key"],
              "networks": ["private"], "read_only": True, "cap_drop": ["ALL"],
              "security_opt": ["no-new-privileges:true"], "pids_limit": 64, "mem_limit": "64m", "cpus": 0.1,
              "labels": {"beaver.owner": owner, "beaver.project": name}}
        if role in OWNERS:
            env["BEAVER_GO_MYSQL_DSN_FILE"] = f"/run/secrets/mysql_{role}_dsn"
            svc["secrets"].append(f"mysql_{role}_dsn")
            svc["secrets"].append(f"mysql_{role}_password")
            svc["depends_on"] = {"mysql": {"condition": "service_healthy"}}
        if role == "gateway":
            svc["networks"] = ["private", "edge"]
            env.update({"BEAVER_GO_REDIS": "redis:6379", "BEAVER_GO_REDIS_PASSWORD_FILE": "/run/secrets/redis_password"})
            svc["secrets"].append("redis_password")
            svc["ports"] = [f"127.0.0.1:{port}:8080"]
            svc["depends_on"] = {"redis": {"condition": "service_healthy"}, "login": {"condition": "service_started"},
                                 "server": {"condition": "service_started"}, "world": {"condition": "service_started"},
                                 "chat": {"condition": "service_started"}, "billing": {"condition": "service_started"}}
        if role == "login":
            svc["secrets"].append("development_credential")
        compose["services"][role] = svc
        compose["services"][role]["healthcheck"] = {"test": ["CMD", "/app/bin/healthcheck", "http", "http://127.0.0.1:8080/healthz"]} if role == "gateway" else {"test": ["CMD", "/app/bin/healthcheck", "grpc", "127.0.0.1:50051"], "interval": "3s", "timeout": "2s", "retries": 20}
    compose["services"]["login"]["environment"]["BEAVER_GO_DEVELOPMENT_CREDENTIAL_FILE"] = "/run/secrets/development_credential"
    compose["services"]["gateway"]["environment"]["BEAVER_GO_LOGIN"] = "login:50051"
    compose["services"]["gateway"]["environment"]["BEAVER_GO_SERVER"] = "server:50051"
    compose["services"]["gateway"]["environment"]["BEAVER_GO_WORLD"] = "world:50051"
    compose["services"]["gateway"]["environment"]["BEAVER_GO_CHAT"] = "chat:50051"
    compose["services"]["gateway"]["environment"]["BEAVER_GO_BILLING"] = "billing:50051"
    compose["services"]["world"]["environment"]["BEAVER_GO_SERVER"] = "server:50051"
    compose["services"]["gateway"]["depends_on"] = {k: {"condition": "service_healthy"} for k in ("redis", "login", "server", "world", "chat", "billing")}
    compose["networks"] = {
        "private": {"internal": True, "labels": {"beaver.owner": owner, "beaver.project": name}},
        "edge": {"driver": "bridge", "internal": False, "labels": {"beaver.owner": owner, "beaver.project": name}}}
    compose["secrets"] = secrets
    raw = json.dumps(compose, indent=2, ensure_ascii=False).encode("utf-8")
    out = Path(output).expanduser().absolute()
    if out.parent.resolve() != candidate_root:
        raise ValueError("Compose plan must be written in the bound candidate root")
    if out.exists() or not out.parent.is_dir(): raise ValueError("compose plan output must be a new file")
    out.write_bytes(raw)
    return {"schemaVersion": 1, "status": "plan_only", "projectName": name, "gatewayBind": f"127.0.0.1:{port}",
            "composeSha256": _sha(raw), "composePath": str(out), "images": {"candidate": image, "mysql": f"{name}:mysql-bootstrap", "mysqlBase": MYSQL_IMAGE, "redis": REDIS_IMAGE},
            "secretFiles": sorted({entry["file"] for entry in secrets.values()}),
            "lifecycle": {"start": "docker compose -p <projectName> -f <plan> up -d --build", "stop": "docker compose -p <projectName> -f <plan> stop", "dataPolicy": "named volume preserved; never use down --volumes for stop/upgrade", "redisLoss": "reconstructible cache only; MySQL is command/state source of truth"},
            "migration": "manual explicit per-owner SQL application after MySQL bootstrap and before service readiness; no automatic SQL execution by generator",
            "execution": "not_run", "requires": ["unique project/port and current Docker occupancy check", "secret files generated by authorized isolated setup; owner passwords 32-64 lowercase hex", "build_candidate passed on generated private candidate", "review and explicitly apply five owner migrations", "unique compose writer and owner verification"]}
