import tempfile
import unittest
import json
import os
import subprocess
from unittest.mock import patch
from pathlib import Path

from beaver.server_go import _unity_client, build_candidate, build_diagnostics, compose_plan, execute_migrations, generate, generate_module, inspect, inspect_compose_runtime, migration_plan, module_plan, protocol_diff, toolchain_inspect


class ServerGoTests(unittest.TestCase):
  def temporary_directory(self):
    base = Path(__file__).resolve().parents[1] / ".private/v03/server-go/test-fixtures"
    base.mkdir(parents=True, exist_ok=True)
    return tempfile.TemporaryDirectory(dir=base)

  def configuration(self, root):
    schema = {
      "schemaVersion": 2, "id": "beaver-self-owned-server-demo", "rootName": "GameConfig", "namespace": "Beaver.ServerDemo",
      "goPackage": "configv3",
      "root": {"type": "object", "additionalProperties": False, "required": ["version", "starterReward"],
        "properties": {"version": {"type": "string"}, "starterReward": {"type": "array", "minItems": 1, "items": {"type": "object", "required": ["itemId", "amount"], "additionalProperties": False, "properties": {"itemId": {"type": "string"}, "amount": {"type": "integer", "bits": 64, "minimum": 1}}}}}}
    }
    schema_path = root / "schema.json"
    data_path = root / "game.json"
    schema_path.write_text(json.dumps(schema))
    data_path.write_text('{"version":"demo-v1","starterReward":[{"itemId":"demo-token","amount":7}]}')
    return {"schema": str(schema_path), "data": str(data_path), "demoAmountPath": "/starterReward/0/amount"}

  def test_generate_is_create_only_and_has_six_roles(self):
    with self.temporary_directory() as td:
      out = Path(td) / "candidate"
      config = self.configuration(Path(td))
      receipt = generate({"configuration": config}, out)
      self.assertEqual(receipt["status"], "generated_candidate_not_runtime_verified")
      self.assertEqual(set(receipt["services"]), {"gateway", "chat", "server", "login", "world", "billing"})
      for role in receipt["services"]: self.assertTrue((out / "cmd" / role / "main.go").is_file())
      self.assertIn("payment provider and fulfillment integration unavailable", (out / "internal/platform/rpc.go").read_text())
      self.assertTrue((out / "internal/platform/store.go").is_file())
      self.assertIn("Config", (out / "internal/configv3/ConfigurationTypes.go").read_text())
      self.assertIn("ReadConfiguration[RootType]", (out / "internal/configv3/load.go").read_text())
      self.assertIn("grea.example.v1", (out / "api/game.proto").read_text())
      self.assertIn("operation_id = 2", (out / "api/game.proto").read_text())
      self.assertIn("grea.example.v1", (out / "api/game.pb.go").read_text())
      self.assertIn("config_sha256", (out / "migrations/server/0001_initial.sql").read_text())
      self.assertIn("starterReward", (out / "internal/configv3/ConfigurationTypes.go").read_text())
      self.assertIn('public string operationId;', (out / "clients/UnityGateway.cs").read_text())
      client_bytes=(out / "clients/UnityGateway.cs").read_bytes()
      self.assertEqual(client_bytes,_unity_client().encode("utf-8"))
      self.assertEqual(receipt["unityClient"]["contract"],"beaver.unity-gateway/1")
      self.assertEqual(receipt["unityClient"]["sha256"],__import__("hashlib").sha256(client_bytes).hexdigest())
      self.assertIn("state_read",receipt["unityClient"]["capabilities"])
      self.assertIn("in_memory_raw_reply_sha256",receipt["unityClient"]["capabilities"])
      self.assertIn("LastReplyBodySha256",client_bytes.decode())
      self.assertIn("Task<CommandReply> State()",client_bytes.decode())
      self.assertIn("payment provider and fulfillment integration unavailable", (out / "internal/platform/rpc.go").read_text())
      self.assertIn("/app/bin/gateway", (out / "Dockerfile").read_text())
      mysql_dockerfile = (out / "deploy/mysql/Dockerfile").read_text()
      self.assertIn("FROM mysql@sha256:2952e3be", mysql_dockerfile)
      self.assertIn("RUN chmod 0555 /docker-entrypoint-initdb.d/001-create-databases.sh", mysql_dockerfile)
      self.assertNotIn("deploy/mysql/001-create-databases.sh:/docker-entrypoint-initdb.d", json.dumps(receipt))
      store = (out / "internal/platform/store.go").read_text()
      migration = (out / "migrations/server/0001_initial.sql").read_text()
      self.assertIn("SELECT payload_hash,config_sha256,code,revision,balance", store)
      self.assertIn("Scan(&priorHash, &priorConfig, &code, &revision, &balance)", store)
      self.assertIn("if priorHash != payload || priorConfig != configDigest", store)
      self.assertIn("INSERT INTO receipts VALUES(?,?,?,?,?,?,?)", store)
      self.assertEqual(migration.count("CREATE TABLE receipts("), 1)
      receipt_columns = migration.split("CREATE TABLE receipts(", 1)[1].split(") ENGINE", 1)[0].split(", PRIMARY KEY", 1)[0]
      self.assertEqual(receipt_columns.count(",") + 1, 7)
      self.assertIn("activeConfigVersion := cfg.Version + \"@\" + configSnapshot.InputSHA256", (out / "internal/platform/run.go").read_text())
      self.assertIn("configVersion != activeConfigVersion", store)
      self.assertIn("if priorHash != payload", store)
      self.assertNotIn("q.ConfigVersion != s.Config.Version", (out / "internal/platform/rpc.go").read_text())
      self.assertIn("SnapshotVersion(string version", (out / "clients/UnityGateway.cs").read_text())
      self.assertTrue((out / "beaver-server-go.json").is_file())
      with self.assertRaises(ValueError): generate({}, out)


  def test_inspect_and_module_plan_are_read_only_and_hash_exact_contents(self):
    with self.temporary_directory() as td:
      root = Path(td) / "server"
      generate({"configuration": self.configuration(Path(td))}, root)
      before = {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*") if p.is_file()}
      report = inspect(root)
      self.assertEqual(report["missingServices"], [])
      self.assertEqual(report["modulePath"], "example.com/game/server")
      plan = module_plan(root, {"module": "inventory"}, Path(td) / "plan.json")
      self.assertTrue(any(x["path"] == "internal/games/inventory/domain/command.go" for x in plan["writes"]))
      self.assertTrue(all(len(x["sha256"]) == 64 and x["content"] for x in plan["writes"]))
      after = {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*") if p.is_file()}
      self.assertEqual(before, after)


  def test_module_generator_and_protocol_compatibility(self):
    with self.temporary_directory() as td:
      root = Path(td) / "addon"
      result = generate_module({"module": "inventory", "owner": "server", "modulePath": "example.com/beaver/addon-test"}, root)
      self.assertEqual(result["module"], "inventory")
      self.assertTrue((root / "internal/games/inventory/application/service.go").is_file())
      self.assertTrue((root / "migrations/server/0001_inventory_receipts.sql").is_file())
      self.assertFalse((root / "internal/games/inventory/config/schema.go").exists())
      (root / "go.mod").write_text("module example.com/beaver/addon-test\n\ngo 1.26.0\n")
      env = dict(os.environ, GOTOOLCHAIN="local", GOPROXY="off", GOSUMDB="off", GOCACHE=str(root / ".cache"))
      go = subprocess.run(["go", "test", "./internal/games/inventory/..."], cwd=root, env=env, capture_output=True, text=True, timeout=60)
      self.assertEqual(go.returncode, 0, go.stderr or go.stdout)
      old, new = Path(td) / "old.proto", Path(td) / "new.proto"
      old.write_text('syntax="proto3"; message M { string id = 1; string old = 2; } service S { rpc Get(M) returns (M); }')
      new.write_text('syntax="proto3"; message M { string id = 1; } service S { rpc Get(M) returns (M); }')
      report = protocol_diff(old, new)
      self.assertEqual(report["status"], "breaking")
      self.assertEqual(report["issues"][0]["kind"], "deleted_field_not_reserved")


  def test_protocol_requires_reserved_and_detects_method_removal(self):
    with self.temporary_directory() as td:
      a, b = Path(td) / "a.proto", Path(td) / "b.proto"
      a.write_text('message M { int32 id = 5; } service S { rpc Put(M) returns (M); }')
      b.write_text('message M { reserved 5; } service S {}')
      report = protocol_diff(a, b)
      self.assertEqual(report["status"], "breaking")
      self.assertIn("removed_rpc_method", {x["kind"] for x in report["issues"]})
      self.assertNotIn("deleted_field_not_reserved", {x["kind"] for x in report["issues"]})

  def test_protocol_diff_detects_same_number_reuse_name_reuse_and_rpc_signature_change(self):
    with self.temporary_directory() as td:
      old, new = Path(td) / "old.proto", Path(td) / "new.proto"
      old.write_text('syntax="proto3"; package game.v1; message M { string id = 1; int64 score = 2; } service S { rpc Put(M) returns (M); }')
      new.write_text('syntax="proto3"; package game.v1; message M { string id = 1; string score = 2; } service S { rpc Put(M) returns (Other); } message Other {}')
      report = protocol_diff(old, new)
      kinds = {x["kind"] for x in report["issues"]}
      self.assertIn("field_number_reused_or_changed", kinds)
      self.assertIn("rpc_signature_changed", kinds)
      old.write_text('syntax="proto3"; package game.v1; message M { string id = 1; reserved 3; } service S { rpc Put(M) returns (M); }')
      new.write_text('syntax="proto3"; package game.v1; message M { string id = 1; int64 score = 3; } service S { rpc Put(M) returns (M); }')
      report = protocol_diff(old, new)
      self.assertIn("reserved_field_number_or_name_reused", {x["kind"] for x in report["issues"]})

  def test_build_diagnostics_and_migration_compose_plans_are_bounded(self):
    with self.temporary_directory() as td:
      root = Path(td) / "server"
      generate({"configuration": self.configuration(Path(td))}, root)
      report = build_diagnostics(root)
      self.assertEqual(report["status"], "passed", report)
      self.assertEqual(report["networkAccess"], "disabled")
      build = build_candidate(root)
      self.assertEqual(build["status"], "built_candidate", build)
      self.assertEqual(len(build["binaries"]), 7)
      migrations = root / "migrations/server"
      migrations.mkdir(parents=True, exist_ok=True)
      (migrations / "0002_drop.sql").write_text("DROP TABLE old_data;")
      plan = migration_plan(root, Path(td) / "migration-plan.json")
      self.assertIn("destructive_drop", next(x for x in plan["migrations"] if x["path"].endswith("0002_drop.sql"))["risks"])
      self.assertEqual(len(plan["planSha256"]), 64)
      saved_plan = Path(td) / "migration-plan.json"
      tampered = json.loads(saved_plan.read_text()); tampered["migrations"][0]["sha256"] = "0" * 64
      saved_plan.write_text(json.dumps(tampered, indent=2, ensure_ascii=False) + "\n")
      compose_path = root / "compose.json"
      compose_plan({"projectName": "beaver-go-localtest", "gatewayPort": 18291, "candidateRoot": str(root)}, compose_path)
      with self.assertRaisesRegex(ValueError, "plan hash mismatch"):
        execute_migrations(root, saved_plan, compose_path, "beaver-go-localtest", "server", "0" * 64)
      fresh_plan = Path(td) / "migration-plan-fresh.json"
      migration_plan(root, fresh_plan)
      with self.assertRaisesRegex(ValueError, "Compose project identity"):
        execute_migrations(root, fresh_plan, compose_path, "beaver-go-otherxx", "server", "0" * 64)
      compose = compose_plan({"projectName": "beaver-go-localtest", "gatewayPort": 18291, "candidateRoot": str(root)}, root / "compose-second.json")
      compose_text = Path(compose["composePath"]).read_text()
      self.assertNotIn("--volumes", compose_text)
      plan_data = json.loads(compose_text)
      self.assertEqual(plan_data["services"]["gateway"]["ports"], ["127.0.0.1:18291:8080"])
      self.assertIn("mysql_server_dsn", plan_data["secrets"])
      self.assertEqual(len(plan_data["secrets"]), 14)
      self.assertIn("mysql_data:/var/lib/mysql", plan_data["services"]["mysql"]["volumes"])
      self.assertEqual(plan_data["services"]["mysql"]["image"], "beaver-go-localtest:mysql-bootstrap")
      self.assertEqual(plan_data["services"]["mysql"]["build"]["dockerfile"], "deploy/mysql/Dockerfile")
      self.assertEqual(len(plan_data["services"]["mysql"]["volumes"]), 1)
      self.assertIn("mysqlBase", compose["images"])
      self.assertIn("--requirepass", " ".join(plan_data["services"]["redis"]["command"]))
      self.assertIn('"$$(cat /run/secrets/redis_password)"', " ".join(plan_data["services"]["redis"]["command"]))
      self.assertNotIn("ports", plan_data["services"]["mysql"])
      self.assertEqual(plan_data["services"]["mysql"]["networks"], ["private"])
      self.assertEqual(plan_data["services"]["redis"]["networks"], ["private"])
      self.assertEqual(plan_data["services"]["gateway"]["networks"], ["private", "edge"])
      self.assertTrue(plan_data["networks"]["private"]["internal"])
      self.assertEqual(plan_data["networks"]["edge"]["driver"], "bridge")
      self.assertFalse(plan_data["networks"]["edge"]["internal"])
      self.assertEqual(compose["execution"], "not_run")
      self.assertEqual(toolchain_inspect()["downloads"], "disabled")

  def test_live_target_rejects_unowned_mysql_before_sql(self):
    from beaver.server_go import _verify_live_compose_target
    class Result:
      returncode = 0
      stderr = ""
      def __init__(self, stdout): self.stdout = stdout
    responses = [Result('{"ID":"container1"}'), Result(json.dumps([{"Config":{"Labels":{
      "com.docker.compose.project":"other", "com.docker.compose.service":"mysql",
      "beaver.owner":"beaver-v03-server-go", "beaver.project":"beaver-go-localtest"}}, "Mounts":[], "NetworkSettings":{"Networks":{}}}]))]
    with patch("beaver.server_go.subprocess.run", side_effect=responses) as run:
      target, error = _verify_live_compose_target(Path("/tmp/compose.json"), "beaver-go-localtest")
    self.assertIsNone(target)
    self.assertIn("ownership labels", error)
    self.assertEqual(run.call_count, 2)

  def test_live_target_accepts_only_owner_volume_and_internal_network(self):
    from beaver.server_go import _verify_live_compose_target
    class Result:
      returncode = 0
      stderr = ""
      def __init__(self, stdout): self.stdout = stdout
    project = "beaver-go-localtest"
    responses = [
      Result('{"ID":"container1"}'),
      Result(json.dumps([{"Config":{"Image":project+":mysql-bootstrap","Labels":{"com.docker.compose.project":project,"com.docker.compose.service":"mysql","beaver.owner":"beaver-v03-server-go","beaver.project":project}},
                         "Image":"sha256:mysql-image-id",
                         "Mounts":[{"Destination":"/var/lib/mysql","Type":"volume","Name":"proj_mysql_data"}],
                         "NetworkSettings":{"Networks":{"proj_private":{}}}}])),
      Result(json.dumps([{"Config":{"Labels":{"beaver.owner":"beaver-v03-server-go","beaver.mysql.base.digest":"sha256:2952e3be7807f06fc18de50b3ea1a632d5c70d63482ff7d7376fe3aa8999babf"}}}])),
      Result(json.dumps([{"Labels":{"beaver.owner":"beaver-v03-server-go","beaver.project":project,"com.docker.compose.project":project,"com.docker.compose.volume":"mysql_data"}}])),
      Result(json.dumps([{"Internal":True,"Labels":{"beaver.owner":"beaver-v03-server-go","beaver.project":project,"com.docker.compose.project":project,"com.docker.compose.network":"private"}}]))]
    with patch("beaver.server_go.subprocess.run", side_effect=responses) as run:
      target, error = _verify_live_compose_target(Path("/tmp/compose.json"), project)
    self.assertIsNone(error)
    self.assertEqual(target, {"containerId":"container1","volume":"proj_mysql_data","network":"proj_private"})
    self.assertEqual(run.call_count, 5)

  def test_compose_requires_manifest_bound_nonlink_candidate(self):
    from beaver.server_go import _compose_target
    with self.temporary_directory() as td:
      root = Path(td) / "candidate"
      generate({"configuration": self.configuration(Path(td))}, root)
      with self.assertRaisesRegex(ValueError, "candidateRoot"):
        compose_plan({"projectName": "beaver-go-localtest", "gatewayPort": 18291}, root / "compose.json")
      compose_plan({"projectName": "beaver-go-localtest", "gatewayPort": 18291, "candidateRoot": str(root)}, root / "compose.json")
      self.assertEqual(_compose_target(root / "compose.json", "beaver-go-localtest", "server")[1], "beaver_go_example_server")
      proto = root / "api/game.proto"
      saved_proto = Path(td) / "game.proto"
      proto.rename(saved_proto)
      proto.symlink_to(saved_proto)
      with self.assertRaisesRegex(ValueError, "symlinks"):
        compose_plan({"projectName": "beaver-go-localtest", "gatewayPort": 18291, "candidateRoot": str(root)}, root / "compose.json")

  def test_runtime_inspection_reports_real_publishers_without_claiming_business_success(self):
    with self.temporary_directory() as td:
      root = Path(td) / "candidate"
      generate({"configuration": self.configuration(Path(td))}, root)
      compose_plan({"projectName":"beaver-go-runtimecheck","gatewayPort":18295,"candidateRoot":str(root)},root/"compose.json")
      services = ("gateway","chat","server","login","world","billing","mysql","redis")
      rows = "\n".join(json.dumps({"Service":s,"ID":"id-"+s}) for s in services)
      class Result:
        returncode = 0
        stderr = ""
        def __init__(self, stdout): self.stdout = stdout
      # Return Docker responses based on command shape, including real published host-port data.
      def run(args, **kw):
        if args[0:2] == ["docker","compose"]: return Result(rows)
        if args[0:3] == ["docker","network","inspect"]:
          name=args[-1]; kind="edge" if name.endswith("_edge") else "private"
          return Result(json.dumps([{"Internal":kind=="private","Labels":{"com.docker.compose.project":"beaver-go-runtimecheck","com.docker.compose.network":kind,"beaver.owner":"beaver-v03-server-go","beaver.project":"beaver-go-runtimecheck"}}]))
        service = args[-1].removeprefix("id-")
        ports = {"8080/tcp":[{"HostIp":"127.0.0.1","HostPort":"18295"}]} if service == "gateway" else {}
        networks={"beaver-go-runtimecheck_private":{}}
        if service=="gateway": networks["beaver-go-runtimecheck_edge"]={}
        return Result(json.dumps([{"Config":{"Labels":{"com.docker.compose.project":"beaver-go-runtimecheck","com.docker.compose.service":service,"beaver.owner":"beaver-v03-server-go","beaver.project":"beaver-go-runtimecheck"}},
          "State":{"Status":"running","Health":{"Status":"healthy"}},"NetworkSettings":{"Ports":ports,"Networks":networks}}]))
      with patch("beaver.server_go.subprocess.run", side_effect=run):
        report = inspect_compose_runtime(root/"compose.json","beaver-go-runtimecheck",18295)
      self.assertEqual(report["status"],"runtime_preflight_passed",report)
      self.assertEqual(report["hostPublishers"],[{"service":"gateway","hostIp":"127.0.0.1","hostPort":"18295","containerPort":"8080","protocol":"tcp"}])
      self.assertEqual(report["applicationBusinessProbe"],"not_run")

  def test_build_candidate_refuses_existing_files_and_symlinks_without_overwrite(self):
    with self.temporary_directory() as td:
      base=Path(td)
      config=self.configuration(base)
      existing=base/"existing"
      generate({"configuration":config},existing)
      (existing/"bin").mkdir()
      sentinel=existing/"bin/gateway"
      sentinel.write_bytes(b"hand-owned")
      with self.assertRaisesRegex(ValueError,"create-only"):
        build_candidate(existing)
      self.assertEqual(sentinel.read_bytes(),b"hand-owned")
      linked=base/"linked"
      generate({"configuration":config},linked)
      target=base/"outside-bin"
      target.mkdir()
      (target/"gateway").write_bytes(b"keep")
      (linked/"bin").symlink_to(target,target_is_directory=True)
      with self.assertRaisesRegex(ValueError,"create-only"):
        build_candidate(linked)
      self.assertEqual((target/"gateway").read_bytes(),b"keep")

  def test_build_candidate_failure_discards_all_staged_binaries(self):
    with self.temporary_directory() as td:
      root=Path(td)/"candidate"
      generate({"configuration":self.configuration(Path(td))},root)
      calls=[]
      def fake_run(args,**kwargs):
        calls.append(args)
        if args[1]=="build":
          if args[-1]=="./cmd/chat": return subprocess.CompletedProcess(args,1,"","synthetic offline failure")
          Path(args[args.index("-o")+1]).write_bytes(b"staged-binary")
          return subprocess.CompletedProcess(args,0,"","")
        return subprocess.run(args,**kwargs)
      with patch("beaver.server_go.subprocess.run",side_effect=fake_run):
        result=build_candidate(root)
      self.assertEqual(result["status"],"failed_or_needs_action")
      self.assertEqual(result["role"],"chat")
      self.assertFalse(result["binCreated"])
      self.assertFalse((root/"bin").exists())
      self.assertEqual(list(root.glob(".beaver-go-build-*")),[])

  def test_build_candidate_uses_atomic_no_replace_promotion(self):
    from beaver.payload import promote as real_promote
    with self.temporary_directory() as td:
      root=Path(td)/"candidate"
      generate({"configuration":self.configuration(Path(td))},root)
      def fake_run(args,**kwargs):
        if args[1]=="build":
          Path(args[args.index("-o")+1]).write_bytes(b"staged-binary")
          return subprocess.CompletedProcess(args,0,"","")
        if args==["go","version"]: return subprocess.CompletedProcess(args,0,"go version go1.26.2 darwin/arm64","")
        raise AssertionError(args)
      def race(stage,target):
        target.mkdir()
        (target/"sentinel").write_bytes(b"concurrent-owner")
        return real_promote(stage,target)
      with patch("beaver.server_go.subprocess.run",side_effect=fake_run), patch("beaver.payload.promote",side_effect=race):
        report=build_candidate(root)
      self.assertEqual(report["status"],"failed_or_needs_action")
      self.assertEqual((root/"bin/sentinel").read_bytes(),b"concurrent-owner")
      self.assertFalse((root/"bin/gateway").exists())
      self.assertEqual(list(root.glob(".beaver-go-build-*")),[])
