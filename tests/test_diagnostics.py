import json
import tempfile
import unittest
from pathlib import Path

from beaver.diagnostics import summarize


class DiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.root = Path(self.temp.name)

    def tearDown(self): self.temp.cleanup()

    def put(self, name, value):
        p = self.root / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(value if isinstance(value, str) else json.dumps(value), encoding="utf-8")
        return str(p)

    def test_json_property_names_and_verification_not_run_do_not_override_success(self):
        p = self.put("ok.json", {"status":"verified", "executed":True, "exitCode":0, "actualChanges":["A.cs"],
                                  "verification":{"player":"not_run"}, "limitations":["previous failure was not_run"]})
        report=summarize([p])
        self.assertEqual(report["status"], "ok")
        self.assertEqual(report["diagnostics"][0]["state"], "not_run")
        self.assertEqual(report["diagnostics"][0]["stage"], "player")

    def test_schema_rejection_unknown_execution_is_not_post_execution(self):
        p=self.put("unknown.json", {"status":"needs_action", "diagnostic":"Invalid schema at $.actualChanges; actualChanges rejected"})
        r=summarize([p])
        self.assertEqual(r["status"], "execution_unknown")
        self.assertEqual(r["diagnostics"][0]["execution"]["executed"], "unknown")

    def test_postexecution_only_when_explicit_executed_true(self):
        p=self.put("executed.json", {"executed":True,"status":"needs_action","exitCode":0,
            "actualChanges":["Assets/A.prefab"],"transaction":"txn-1",
            "diagnostic":"Invalid schema at $.actualChanges; actualChanges rejected"})
        r=summarize([p]); d=r["diagnostics"][0]
        self.assertEqual(r["status"], "postexecution_outer_schema_rejection")
        self.assertEqual(d["execution"]["applied"],["Assets/A.prefab"])
        self.assertEqual(d["execution"]["checkpoint"],"txn-1")

    def test_unrelated_write_set_and_exit_code_are_never_joined(self):
        a=self.put("failed-result.json", {"status":"needs_action","diagnostic":"schema rejected actualChanges"})
        b=self.put("other.json", {"status":"complete","executed":True,"exit_code":0,"writeSet":["planned.cs"],"planSha256":"other"})
        r=summarize([a,b]); d=r["diagnostics"][0]
        self.assertEqual(r["status"],"execution_unknown")
        self.assertEqual(d["execution"]["exitCode"],"unknown")
        self.assertEqual(d["execution"]["applied"],"unknown")

    def test_explicit_invocation_artifact_path_and_hash_link_carries_success_facts(self):
        journal=self.root/"journal.json"
        journal.write_text(json.dumps({"contract":"beaver-transaction/1","status":"verified","planSha256":"plan-a",
                                       "changedPaths":["Assets/A.prefab"]}),encoding="utf-8")
        digest=__import__("hashlib").sha256(journal.read_bytes()).hexdigest()
        stdout={"status":"ok","actualChanges":["Assets/A.prefab"],"data":{"transaction":"txn-a"},
                "artifacts":[{"path":str(journal),"sha256":digest}]}
        invocation=self.put("invocation.json",{"status":"ok","exit_code":0,"stdout":json.dumps(stdout)})
        rejection=self.put("rejection.json",{"status":"needs_action","executed":True,
            "diagnostic":"Invalid beaver-result schema at $.actualChanges[0]: actualChanges rejected"})
        r=summarize([invocation,rejection,str(journal)])
        d=next(x for x in r["diagnostics"] if x["stage"]=="outer_schema")
        self.assertEqual(r["status"],"postexecution_outer_schema_rejection")
        self.assertEqual(d["execution"]["exitCode"],0)
        self.assertEqual(d["execution"]["applied"],["Assets/A.prefab"])
        self.assertEqual(d["execution"]["checkpoint"],"txn-a")
        self.assertGreaterEqual(len(d["occurrences"]),3)

    def test_log_compiler_error_warning_and_spaced_stack_path(self):
        p=self.put("build.log", "Assets/My Folder/Foo.cs(12,3): error CS1002: ; expected\nAssets/Other.cs(2,1): warning CS0168: unused\nat X.Run() in /tmp/Project With Spaces/Some File.cs:line 42\n")
        r=summarize([p]); errors=[d for d in r["diagnostics"] if d["code"]=="CS1002"]
        warns=[d for d in r["diagnostics"] if d["code"]=="CS0168"]
        stack=[d for d in r["diagnostics"] if d["stage"]=="play"]
        self.assertEqual(errors[0]["location"]["line"],12)
        self.assertEqual(errors[0]["state"],"failed")
        self.assertEqual(warns[0]["state"],"unknown")
        self.assertTrue(stack)

    def test_duplicate_preserves_evidence_occurrences_and_limit_counts(self):
        a=self.put("one.log","Compilation failed\n")
        b=self.put("two.log","Compilation failed\nCompilation failed\nPlayer process timed out\n")
        r=summarize([a,b],max_items=1)
        self.assertEqual(r["counts"]["total"],2)
        self.assertEqual(r["counts"]["truncated"],1)
        self.assertEqual(len(r["diagnostics"][0]["occurrences"]),3)

    def test_ui_and_wiring_structures_keep_asset_node_field_certainty(self):
        p=self.put("ui.json", {"status":"issues","mode":"static-prefab","issues":[{"code":"BUTTON_UNWIRED","asset":"Assets/A.prefab","nodePath":"Root/Play","field":"onClick","certainty":"high"}],
                                "bindings":[{"status":"blocked","request":{"asset":"Assets/A.prefab","nodePath":"Root/Play","field":"target"},"reason":"ambiguous target"}]})
        r=summarize([p]); ui=[d for d in r["diagnostics"] if d["stage"]=="ui_inspection"][0]
        wiring=[d for d in r["diagnostics"] if d["stage"]=="ui_wiring"][0]
        self.assertEqual((ui["location"]["asset"],ui["location"]["node"],ui["location"]["field"]),("Assets/A.prefab","Root/Play","onClick"))
        self.assertEqual(ui["certainty"],"high")
        self.assertEqual(ui["state"],"unknown")
        self.assertEqual(len([d for d in r["diagnostics"] if d["stage"]=="ui_inspection"]),1)
        self.assertEqual(wiring["state"],"blocked")

    def test_ui_explicit_failure_classification_controls_state_not_certainty(self):
        p=self.put("ui-failure.json", {"issues":[
            {"code":"BLOCKED_BY_OVERLAY","certainty":"high","classification":"observation","severity":"warning"},
            {"code":"FAILED_EXPECTATION","certainty":"low","classification":"failure","severity":"error"},
            {"code":"PARTIAL_SCAN","classification":"partial","severity":"warning"}]})
        r=summarize([p]); states={d["code"]:d["state"] for d in r["diagnostics"] if d["stage"]=="ui_inspection"}
        self.assertEqual(states,{"BLOCKED_BY_OVERLAY":"unknown","FAILED_EXPECTATION":"failed","PARTIAL_SCAN":"partial"})

    def test_environment_license_log_retains_observation_and_exit_code(self):
        p=self.put("editor.log","attempt to write a readonly database\nError: 'com.unity.editor.headless' was not found.\nApplication will exit with return code 198\n")
        r=summarize([p]); items=[d for d in r["diagnostics"] if d["stage"]=="environment_license"]
        self.assertEqual(len(items),2)
        self.assertTrue(all(d["state"]=="not_run" and d["execution"]["exitCode"]==198 for d in items))

    def test_output_directory_create_only(self):
        p=self.put("log.txt","Compilation failed")
        out=self.root/"out"; result=summarize([p],str(out))
        self.assertTrue((out/"diagnostics.json").is_file())
        with self.assertRaises(Exception): summarize([p],str(out))


if __name__=="__main__": unittest.main()
