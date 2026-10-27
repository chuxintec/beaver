"""C# semantic maintenance and module template contract tests (stdlib only)."""
import json
from pathlib import Path
import shutil
import sys
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from beaver.csharp import inspect, module_generate, rename_plan, rename_sequence_plan, _resolve_toolchain, _parse_response_files  # noqa: E402
from beaver.errors import BeaverError  # noqa: E402


FIXTURES = ROOT / ".private" / "v03" / "csharp" / "fixtures"
RUNS = ROOT / ".private" / "v03" / "csharp" / "test-runs"
FRAMEWORK_REFS = Path("/opt/homebrew/Cellar/dotnet@8/8.0.130/libexec/packs/Microsoft.NETCore.App.Ref/8.0.30/ref/net8.0")


class CSharpMaintenanceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if FIXTURES.exists():
            shutil.rmtree(FIXTURES)
        FIXTURES.mkdir(parents=True)
        (FIXTURES / "Assets" / "Scripts").mkdir(parents=True)
        (FIXTURES / "EngineStubs.cs").write_text(
            """using System;
namespace UnityEngine { public class MonoBehaviour { } public class ScriptableObject { } public class RectTransform : MonoBehaviour { } public class TextAsset { public string text; } public static class Resources { public static object Load(string path) => null; } public static class JsonUtility { public static T FromJson<T>(string source) where T : class => null; }
[AttributeUsage(AttributeTargets.Field)] public sealed class SerializeField : Attribute { } }
namespace UnityEngine.Serialization { [AttributeUsage(AttributeTargets.Field)] public sealed class FormerlySerializedAsAttribute : Attribute { public FormerlySerializedAsAttribute(string oldName) { } } }
namespace UnityEngine.Scripting.APIUpdating { [AttributeUsage(AttributeTargets.Class | AttributeTargets.Struct, AllowMultiple=false)] public sealed class MovedFromAttribute : Attribute { public MovedFromAttribute(bool auto, string ns, string asm, string name) { } } }
""", encoding="utf-8")
        (FIXTURES / "Assets" / "Scripts" / "Widget.cs").write_text(
            """using UnityEngine;
namespace Legacy.UI {
    // Widget and oldValue in comments are not references.
    public partial class Widget : MonoBehaviour {
        [SerializeField] private int oldValue;
        public int Read() => oldValue;
        public const string Note = "unrelated literal";
    }
}
""", encoding="utf-8")
        (FIXTURES / "Assets" / "Scripts" / "Widget.Part.cs").write_text(
            "namespace Legacy { namespace UI { public partial class Widget { public int AlsoRead() => oldValue; } } }\n", encoding="utf-8")
        (FIXTURES / "Assets" / "Scripts" / "Widget.cs.meta").write_text("fileFormatVersion: 2\nguid: 0123456789abcdef0123456789abcdef\n", encoding="utf-8")
        (FIXTURES / "Assets" / "Scripts" / "UseWidget.cs").write_text(
            "using Legacy.UI;\nusing UiAlias = Legacy.UI;\npublic static class UseWidget { public static Widget Make() => new Widget(); public static UiAlias.Widget MakeAlias() => new UiAlias.Widget(); }\n", encoding="utf-8")
        cls.assemblies = [
            {"name": "UnityStubs", "sourceFiles": ["EngineStubs.cs"], "references": sorted(str(p) for p in FRAMEWORK_REFS.glob("*.dll"))},
            {"name": "Assembly-CSharp", "sourceFiles": ["Assets/Scripts/Widget.cs", "Assets/Scripts/Widget.Part.cs", "Assets/Scripts/UseWidget.cs"], "projectReferences": ["UnityStubs"],
             "references": sorted(str(p) for p in FRAMEWORK_REFS.glob("*.dll"))},
        ]

    def setUp(self):
        RUNS.mkdir(parents=True, exist_ok=True)
        for path in RUNS.iterdir():
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink()

    def plan(self, name, spec, assemblies=None):
        return rename_plan(FIXTURES, dict(spec, assembly="Assembly-CSharp"), RUNS / name,
                          {"assemblies": assemblies or self.assemblies})

    def test_semantic_type_rename_preserves_guid_and_ignores_comments(self):
        result = self.plan("type", {"kind": "type", "symbol": "Legacy.UI.Widget", "newName": "WidgetView"})
        self.assertEqual(result["status"], "ok")
        report = json.loads(Path(result["report"]).read_text())
        self.assertEqual(len(report["patches"]), 3)
        moved = next(p for p in report["patches"] if p["sourcePath"] != p["targetPath"])
        self.assertEqual(moved["sourcePath"], "Assets/Scripts/Widget.cs")
        self.assertTrue(moved["preserveMetaGuid"])
        edit = next(row for row in report["fileEdits"] if row.get("nativeAssetMove"))
        self.assertTrue(Path(edit["sourcePath"]).is_absolute())
        self.assertEqual(edit["targetPath"], "Assets/Scripts/WidgetView.cs")
        self.assertEqual(edit["expectedSha256"], moved["expectedSha256"])
        self.assertEqual(edit["newSha256"], moved["afterSha256"])
        self.assertTrue(edit["nativeAssetMove"]["preserveMetaGuid"])
        content = "\n".join(p.read_text() for p in (Path(result["report"]).parent / "files").rglob("*.cs"))
        self.assertEqual(content.count("MovedFromAttribute"), 1)
        self.assertIn("Widget and oldValue in comments", content)
        self.assertIn("UiAlias.WidgetView", content)

    def test_serialized_field_adds_compatibility_attribute_and_rewrites_bound_use(self):
        result = self.plan("field", {"kind": "serialized_field", "symbol": "oldValue", "containingType": "Legacy.UI.Widget", "newName": "currentValue"})
        self.assertEqual(result["status"], "ok")
        content = (Path(result["report"]).parent / "files" / "Assets" / "Scripts" / "Widget.cs").read_text()
        self.assertIn("FormerlySerializedAsAttribute(\"oldValue\")", content)
        self.assertIn("private int currentValue", content)
        self.assertIn("=> currentValue", content)
        self.assertIn("oldValue in comments", content)

    def test_matching_string_literal_blocks_instead_of_rewriting_it(self):
        usage = FIXTURES / "Assets" / "Scripts" / "UseWidget.cs"
        usage.write_text('using Legacy.UI;\nusing UiAlias = Legacy.UI;\npublic static class UseWidget { public const string Name = "Widget"; public static Widget Make() => new Widget(); public static UiAlias.Widget MakeAlias() => new UiAlias.Widget(); }\n', encoding="utf-8")
        try:
            result = self.plan("literal", {"kind": "type", "symbol": "Legacy.UI.Widget", "newName": "WidgetView"})
            self.assertEqual(result["status"], "blocked")
            report = json.loads(Path(result["report"]).read_text())
            self.assertTrue(any(item.get("code") == "UNKNOWN_STRING_REFERENCE" for item in report["unknownReferences"]))
            self.assertEqual(report["patches"], [])
        finally:
            usage.write_text("using Legacy.UI;\nusing UiAlias = Legacy.UI;\npublic static class UseWidget { public static Widget Make() => new Widget(); public static UiAlias.Widget MakeAlias() => new UiAlias.Widget(); }\n", encoding="utf-8")

    def test_ambiguous_nonpartial_type_declaration_is_refused(self):
        (FIXTURES / "DuplicateWidget.cs").write_text("namespace Legacy.UI { public class Widget { } }\n", encoding="utf-8")
        assemblies = [dict(item) for item in self.assemblies]
        app = next(row for row in assemblies if row["name"] == "Assembly-CSharp")
        app["sourceFiles"] = [*app["sourceFiles"], "DuplicateWidget.cs"]
        try:
            result = self.plan("ambiguous", {"kind": "type", "symbol": "Legacy.UI.Widget", "newName": "WidgetView"}, assemblies)
            self.assertEqual(result["status"], "blocked")
            report = json.loads(Path(result["report"]).read_text())
            self.assertTrue(any(item.get("code") == "CS0260" for item in report["diagnostics"]))
            self.assertEqual(report["patches"], [])
        finally:
            (FIXTURES / "DuplicateWidget.cs").unlink(missing_ok=True)

    def test_type_rename_preserves_single_use_existing_moved_from_identity(self):
        source = FIXTURES / "Assets" / "Scripts" / "MigratedWidget.cs"
        source.write_text(
            'using UnityEngine; using UnityEngine.Scripting.APIUpdating; namespace Legacy.UI { '
            '[MovedFrom(true, "ExampleGame.Configuration", "ExampleGame.Configuration", "OriginalWidget")] '
            'public sealed class MigratedWidget : MonoBehaviour { } }\n', encoding="utf-8")
        assemblies = [dict(item) for item in self.assemblies]
        app = next(row for row in assemblies if row["name"] == "Assembly-CSharp")
        app["sourceFiles"] = [*app["sourceFiles"], "Assets/Scripts/MigratedWidget.cs"]
        try:
            result = self.plan("moved-from-chain", {"kind": "type", "symbol": "Legacy.UI.MigratedWidget",
                              "newName": "MigratedWidgetAsset"}, assemblies)
            self.assertEqual(result["status"], "blocked")
            report = json.loads(Path(result["report"]).read_text())
            self.assertEqual(report["fileEdits"], [])
            issue = next(row for row in report["unknownReferences"] if row.get("code") == "MOVED_FROM_MIGRATION_CHAIN_UNSUPPORTED")
            self.assertFalse(issue["allowMultiple"])
            self.assertIn("OriginalWidget", issue["existingMovedFrom"][0])
            namespace_result = self.plan("moved-from-namespace", {"kind": "namespace", "symbol": "Legacy.UI",
                                           "newName": "Game.UI"}, assemblies)
            self.assertEqual(namespace_result["status"], "blocked")
            namespace_report = json.loads(Path(namespace_result["report"]).read_text())
            self.assertTrue(any(row.get("code") == "MOVED_FROM_MIGRATION_CHAIN_UNSUPPORTED"
                                for row in namespace_report["unknownReferences"]))
        finally:
            source.unlink(missing_ok=True)

    def test_type_rename_updates_constructor_and_destructor_declarations(self):
        source = FIXTURES / "Assets" / "Scripts" / "LifecycleThing.cs"
        source.write_text("namespace Legacy.UI { public class LifecycleThing { public LifecycleThing() { } ~LifecycleThing() { } } }\n",
                          encoding="utf-8")
        usage = FIXTURES / "Assets" / "Scripts" / "UseWidget.cs"
        original = usage.read_text(encoding="utf-8")
        usage.write_text(original + "public sealed class UseLifecycle { public Legacy.UI.LifecycleThing Create() => new Legacy.UI.LifecycleThing(); }\n",
                         encoding="utf-8")
        assemblies = [dict(item) for item in self.assemblies]
        app = next(row for row in assemblies if row["name"] == "Assembly-CSharp")
        app["sourceFiles"] = [*app["sourceFiles"], "Assets/Scripts/LifecycleThing.cs"]
        try:
            result = self.plan("constructor-rename", {"kind": "type", "symbol": "Legacy.UI.LifecycleThing",
                              "newName": "SessionThing"}, assemblies)
            self.assertEqual(result["status"], "ok", Path(result["report"]).read_text())
            report = json.loads(Path(result["report"]).read_text())
            content = (Path(result["report"]).parent / "files" / "Assets" / "Scripts" / "LifecycleThing.cs").read_text()
            self.assertIn("public SessionThing()", content)
            self.assertIn("~SessionThing()", content)
            self.assertTrue(report["boundReferenceCount"] >= 3)
        finally:
            source.unlink(missing_ok=True)
            usage.write_text(original, encoding="utf-8")

    def test_type_rename_rewrites_reference_in_dependent_assembly(self):
        domain = FIXTURES / "Assets" / "Domain" / "DomainSession.cs"
        consumer = FIXTURES / "Assets" / "Presentation" / "SessionConsumer.cs"
        domain.parent.mkdir(parents=True, exist_ok=True)
        consumer.parent.mkdir(parents=True, exist_ok=True)
        domain.write_text("namespace Game.Domain { public sealed class DomainSession { public DomainSession() { } } }\n", encoding="utf-8")
        consumer.write_text("public sealed class SessionConsumer { public Game.Domain.DomainSession Make() => new Game.Domain.DomainSession(); }\n",
                            encoding="utf-8")
        framework = sorted(str(p) for p in FRAMEWORK_REFS.glob("*.dll"))
        assemblies = [
            {"name": "Domain", "sourceFiles": ["Assets/Domain/DomainSession.cs"], "references": framework},
            {"name": "Presentation", "sourceFiles": ["Assets/Presentation/SessionConsumer.cs"],
             "references": framework, "projectReferences": ["Domain"]},
        ]
        try:
            result = rename_plan(FIXTURES, {"kind": "type", "symbol": "Game.Domain.DomainSession",
                                "newName": "DomainRuntimeSession", "assembly": "Domain"},
                                RUNS / "cross-assembly-type", {"assemblies": assemblies})
            self.assertEqual(result["status"], "ok", Path(result["report"]).read_text())
            report = json.loads(Path(result["report"]).read_text())
            self.assertEqual({row["sourceAssetPath"] for row in report["fileEdits"]},
                             {"Assets/Domain/DomainSession.cs", "Assets/Presentation/SessionConsumer.cs"})
            content = (Path(result["report"]).parent / "files" / "Assets" / "Presentation" / "SessionConsumer.cs").read_text()
            self.assertIn("Game.Domain.DomainRuntimeSession", content)
        finally:
            domain.unlink(missing_ok=True)
            consumer.unlink(missing_ok=True)

    def test_type_rename_preserves_fully_qualified_constructor_and_var_keyword(self):
        source = FIXTURES / "Assets" / "Scripts" / "QualifiedUse.cs"
        source.write_text(
            "public static class QualifiedUse { public static object Make() { var value = new global::Legacy.UI.Widget(); return value; } }\n",
            encoding="utf-8")
        assemblies = [dict(item) for item in self.assemblies]
        app = next(row for row in assemblies if row["name"] == "Assembly-CSharp")
        app["sourceFiles"] = [*app["sourceFiles"], "Assets/Scripts/QualifiedUse.cs"]
        try:
            result = self.plan("qualified-type", {"kind": "type", "symbol": "Legacy.UI.Widget",
                               "newName": "WidgetView"}, assemblies)
            self.assertEqual(result["status"], "ok", Path(result["report"]).read_text())
            candidate = Path(result["report"]).parent / "files" / "Assets" / "Scripts" / "QualifiedUse.cs"
            content = candidate.read_text(encoding="utf-8")
            self.assertIn("var value = new global::Legacy.UI.WidgetView()", content)
            self.assertNotIn("WidgetView value", content)
        finally:
            source.unlink(missing_ok=True)

    def test_rename_sequence_rebinds_each_stage_on_private_snapshot(self):
        source = FIXTURES / "Assets" / "Scripts" / "SequenceWidget.cs"
        usage = FIXTURES / "Assets" / "Scripts" / "SequenceUse.cs"
        source.write_text("namespace Legacy.Sequence { public sealed class Widget { } }\n", encoding="utf-8")
        usage.write_text("public sealed class SequenceUse { public Legacy.Sequence.Widget Make() => new Legacy.Sequence.Widget(); }\n",
                         encoding="utf-8")
        assemblies = [dict(item) for item in self.assemblies]
        app = next(row for row in assemblies if row["name"] == "Assembly-CSharp")
        app["sourceFiles"] = [*app["sourceFiles"], "Assets/Scripts/SequenceWidget.cs", "Assets/Scripts/SequenceUse.cs"]
        try:
            result = rename_sequence_plan(FIXTURES, [
                {"kind": "namespace", "assembly": "Assembly-CSharp", "symbol": "Legacy.Sequence", "newName": "New.Sequence"},
                {"kind": "type", "assembly": "Assembly-CSharp", "symbol": "New.Sequence.Widget", "newName": "WidgetView"},
            ], RUNS / "sequence", {"assemblies": assemblies})
            self.assertEqual(result["status"], "ok", Path(result["report"]).read_text())
            report = json.loads(Path(result["report"]).read_text())
            self.assertEqual(len(report["stages"]), 2)
            self.assertEqual({edit["sourceAssetPath"] for edit in report["fileEdits"]},
                             {"Assets/Scripts/SequenceWidget.cs", "Assets/Scripts/SequenceUse.cs"})
            self.assertTrue(all((Path(result["report"]).parent / edit["artifactPath"]).is_file()
                                for edit in report["fileEdits"]))
            self.assertEqual(source.read_text(encoding="utf-8"), "namespace Legacy.Sequence { public sealed class Widget { } }\n")
        finally:
            source.unlink(missing_ok=True)
            usage.unlink(missing_ok=True)

    def test_inspect_returns_compilation_inventory(self):
        result = inspect(FIXTURES, RUNS / "inspect", {"assemblies": self.assemblies})
        self.assertEqual(result["status"], "ok")
        report = json.loads(Path(result["report"]).read_text())
        self.assertEqual(report["contract"], "beaver-csharp-inspect/1")
        self.assertEqual({row["name"] for row in report["assemblies"]}, {"UnityStubs", "Assembly-CSharp"})

    def test_namespace_rename_rewrites_bound_declarations_and_usings(self):
        result = self.plan("namespace", {"kind": "namespace", "symbol": "Legacy", "newName": "Game.UI"})
        self.assertEqual(result["status"], "ok")
        root = Path(result["report"]).parent / "files"
        widget = (root / "Assets" / "Scripts" / "Widget.cs").read_text()
        usage = (root / "Assets" / "Scripts" / "UseWidget.cs").read_text()
        self.assertIn("namespace Game.UI.UI", widget)
        self.assertIn("using global::Game.UI.UI", usage)
        self.assertIn("using UiAlias = global::Game.UI.UI", usage)
        combined = "\n".join(path.read_text() for path in (root / "Assets" / "Scripts").glob("*.cs"))
        self.assertIn("namespace UI", combined)
        self.assertEqual(combined.count("MovedFromAttribute"), 1)

    def test_nested_namespace_under_unrelated_parent_is_refused(self):
        nested = FIXTURES / "Assets" / "Scripts" / "Nested.cs"
        nested.write_text("namespace Outer { namespace Legacy { public class NestedType { } } }\n", encoding="utf-8")
        assemblies = [dict(item) for item in self.assemblies]
        app = next(row for row in assemblies if row["name"] == "Assembly-CSharp")
        app["sourceFiles"] = [*app["sourceFiles"], "Assets/Scripts/Nested.cs"]
        try:
            result = self.plan("nested-namespace", {"kind": "namespace", "symbol": "Outer.Legacy", "newName": "Game.UI"}, assemblies)
            self.assertEqual(result["status"], "blocked")
            report = json.loads(Path(result["report"]).read_text())
            self.assertTrue(any(item.get("code") == "NESTED_NAMESPACE_MOVE_UNSUPPORTED" for item in report["unknownReferences"]))
        finally:
            nested.unlink(missing_ok=True)

    def test_namespace_strings_distinguish_asset_keys_from_reflection_names(self):
        namespace_file = FIXTURES / "Assets" / "Scripts" / "NamespaceMarker.cs"
        usage = FIXTURES / "Assets" / "Scripts" / "NamespaceUsage.cs"
        namespace_file.write_text("namespace Legacy.UI { public sealed class Marker { } }\n", encoding="utf-8")
        base = ('using System; public static class NamespaceUsage { public const string SceneKey = "Mainline"; '
                'public static object LoadAsset() => UnityEngine.Resources.Load("Mainline"); }\n')
        usage.write_text(base, encoding="utf-8")
        framework = sorted(str(p) for p in FRAMEWORK_REFS.glob("*.dll"))
        assemblies = [
            {"name": "UnityStubs", "sourceFiles": ["EngineStubs.cs"], "references": framework},
            {"name": "Assembly-CSharp", "sourceFiles": ["Assets/Scripts/NamespaceMarker.cs", "Assets/Scripts/NamespaceUsage.cs"],
             "references": framework, "projectReferences": ["UnityStubs"]},
        ]
        try:
            normal = self.plan("namespace-resource-keys", {"kind": "namespace", "symbol": "Legacy.UI", "newName": "Game.UI"}, assemblies)
            self.assertEqual(normal["status"], "ok", Path(normal["report"]).read_text())
            usage.write_text(base.replace('public const string SceneKey', 'const string prefix = "Legacy."; public static Type Reflected = Type.GetType(prefix + "UI.Marker, Assembly-CSharp"); public const string SceneKey'), encoding="utf-8")
            constant = self.plan("namespace-reflection-constant", {"kind": "namespace", "symbol": "Legacy.UI", "newName": "Game.UI"}, assemblies)
            self.assertEqual(constant["status"], "blocked")
            constant_report = json.loads(Path(constant["report"]).read_text())
            self.assertTrue(any(row.get("code") == "UNKNOWN_STRING_REFERENCE" and row.get("value") == "Legacy.UI.Marker, Assembly-CSharp"
                                for row in constant_report["unknownReferences"]))
            usage.write_text('using System; public static class NamespaceUsage { public static string GetSuffix()=>"Marker"; public static Type Reflected=>Type.GetType("Legacy.UI."+GetSuffix()); }\n', encoding="utf-8")
            dynamic = self.plan("namespace-reflection-dynamic", {"kind": "namespace", "symbol": "Legacy.UI", "newName": "Game.UI"}, assemblies)
            self.assertEqual(dynamic["status"], "blocked")
            dynamic_report = json.loads(Path(dynamic["report"]).read_text())
            self.assertTrue(any(row.get("code") == "UNKNOWN_DYNAMIC_REFLECTION_REFERENCE"
                                for row in dynamic_report["unknownReferences"]))
        finally:
            namespace_file.unlink(missing_ok=True)
            usage.unlink(missing_ok=True)

    def test_unity_response_file_uses_paired_bee_sources_and_transitive_assemblies(self):
        project = ROOT / ".private" / "v03" / "csharp" / "rsp-fixture"
        if project.exists():
            shutil.rmtree(project)
        (project / "Assets").mkdir(parents=True)
        (project / "Library" / "Bee" / "artifacts" / "TEST.dag").mkdir(parents=True)
        package_source = project / "Library" / "PackageCache" / "com.example.fixture@1.2.3" / "Runtime" / "Base.cs"
        package_source.parent.mkdir(parents=True)
        package_source.write_text("public class BaseType { }\n", encoding="utf-8")
        package_dir = package_source.parents[1]
        (package_dir / "package.json").write_text(json.dumps({"name": "com.example.fixture", "version": "1.2.3"}), encoding="utf-8")
        (project / "Packages").mkdir(parents=True)
        (project / "Packages" / "packages-lock.json").write_text(json.dumps({"dependencies": {
            "com.example.fixture": {"version": "1.2.3", "source": "registry"}}}), encoding="utf-8")
        (project / "Assets" / "Child.cs").write_text("public class ChildType : BaseType { }\n", encoding="utf-8")
        reference_lines = "".join(f'-r:"{path}"\n' for path in FRAMEWORK_REFS.glob("*.dll"))
        defines = ["TEST_CONTEXT", "UNITY_EDITOR"]
        for name, extra in (("Base", ""), ("Child", '-r:"Library/Bee/artifacts/TEST.dag/Base.ref.dll"\n')):
            source = "Base.cs" if name == "Base" else "Child.cs"
            index = 0 if name == "Base" else 1
            args = (f'-target:library\n-out:"Library/Bee/artifacts/TEST.dag/{name}.dll"\n' +
                    "".join(f"-define:{value}\n" for value in defines) + "-langversion:9.0\n" + extra + reference_lines)
            (project / "Library" / "Bee" / "artifacts" / "TEST.dag" / f"{name}.rsp").write_text(args, encoding="utf-8")
        rows = [
            {"Name": "Base", "Path": str(project), "SourceFiles": ["Packages/com.example.fixture/Runtime/Base.cs"], "Defines": defines,
             "References": [], "LanguageVersion": "9.0"},
            {"Name": "Child", "Path": str(project), "SourceFiles": ["Assets/Child.cs"], "Defines": defines,
             "References": [0], "LanguageVersion": "9.0"},
        ]
        (project / "Library" / "Bee" / "TEST-inputdata.json").write_text(
            json.dumps({"ScriptCompilationBuildProgram.Data.ScriptCompilationData": {"Assemblies": rows}}), encoding="utf-8")
        result = inspect(project, RUNS / "rsp-inspect", {"responseFiles": ["Library/Bee/artifacts/TEST.dag/Child.rsp"]})
        self.assertEqual(result["status"], "ok", Path(result["report"]).read_text())
        report = json.loads(Path(result["report"]).read_text())
        self.assertEqual({row["name"] for row in report["assemblies"]}, {"Base", "Child"})
        self.assertEqual({row["name"] for row in report["compileContext"]}, {"Base", "Child"})
        self.assertTrue(all(row["responseFileSha256"] for row in report["compileContext"]))
        base_ref = project / "Library" / "Bee" / "artifacts" / "TEST.dag" / "Base.ref.dll"
        base_ref.write_bytes(b"metadata fixture")
        filtered = _parse_response_files(project, [str(project / "Library" / "Bee" / "artifacts" / "TEST.dag" / "Child.rsp")], ["Child"])
        self.assertEqual([row["name"] for row in filtered], ["Child"])
        self.assertEqual(filtered[0]["excludedSourceDependencies"], ["Base"])
        self.assertTrue(any(Path(path) == base_ref for path in filtered[0]["references"]))
        base_context = next(row for row in report["compileContext"] if row["name"] == "Base")
        package_origin = base_context["logicalPackageSources"][0]
        self.assertEqual(package_origin["logicalPath"], "Packages/com.example.fixture/Runtime/Base.cs")
        self.assertEqual(package_origin["version"], "1.2.3")
        self.assertTrue(package_origin["lockSha256"])
        child_rsp = project / "Library" / "Bee" / "artifacts" / "TEST.dag" / "Child.rsp"
        original = child_rsp.read_text(encoding="utf-8")
        try:
            child_rsp.write_text(original + "-features:unknownFeature\n", encoding="utf-8")
            planned = rename_plan(project, {"kind": "type", "symbol": "ChildType", "newName": "RenamedChild", "assembly": "Child"},
                                  RUNS / "rsp-rename", {"responseFiles": ["Library/Bee/artifacts/TEST.dag/Child.rsp"]})
            self.assertEqual(planned["status"], "blocked")
            plan_report = json.loads(Path(planned["report"]).read_text())
            self.assertEqual(plan_report["fileEdits"], [])
            self.assertTrue(any(item.get("code") == "CSHARP_COMPILE_CONTEXT_INCOMPLETE" for item in plan_report["unknownReferences"]))
        finally:
            child_rsp.write_text(original, encoding="utf-8")

    def test_response_file_analyzer_runs_generator_with_hashed_additional_file(self):
        project = ROOT / ".private" / "v03" / "csharp" / "analyzer-fixture"
        if project.exists():
            shutil.rmtree(project)
        (project / "Assets").mkdir(parents=True)
        (project / "Library" / "Bee" / "artifacts" / "GEN.dag").mkdir(parents=True)
        generator_root = project / "Generator"
        generator_root.mkdir()
        toolchain = _resolve_toolchain()
        roslyn = Path(toolchain["roslynDirectory"])
        (generator_root / "FixtureGenerator.csproj").write_text(
            f'''<Project Sdk="Microsoft.NET.Sdk"><PropertyGroup><TargetFramework>net8.0</TargetFramework><ImplicitUsings>disable</ImplicitUsings><Nullable>disable</Nullable></PropertyGroup><ItemGroup>
<Reference Include="Microsoft.CodeAnalysis"><HintPath>{roslyn / 'Microsoft.CodeAnalysis.dll'}</HintPath><Private>false</Private></Reference>
<Reference Include="Microsoft.CodeAnalysis.CSharp"><HintPath>{roslyn / 'Microsoft.CodeAnalysis.CSharp.dll'}</HintPath><Private>false</Private></Reference>
</ItemGroup></Project>''', encoding="utf-8")
        (generator_root / "FixtureGenerator.cs").write_text(
            '''using System.Linq; using Microsoft.CodeAnalysis;
[Generator] public sealed class FixtureGenerator : ISourceGenerator {
 public void Initialize(GeneratorInitializationContext context) { }
 public void Execute(GeneratorExecutionContext context) {
  var value = context.AdditionalFiles.Single().GetText(context.CancellationToken).ToString().Trim();
  if (value == "Throw") throw new System.Exception("fixture generator failure");
  context.AddSource("Generated.g.cs", "namespace GeneratedFixture { public static class Marker { public const string Value = \\"" + value + "\\"; } }");
 }
}''', encoding="utf-8")
        (generator_root / "NuGet.Config").write_text(
            '<?xml version="1.0" encoding="utf-8"?><configuration><packageSources><clear /></packageSources></configuration>', encoding="utf-8")
        restore = subprocess.run([toolchain["dotnet"], "restore", "FixtureGenerator.csproj", "--configfile", "NuGet.Config"],
                                 cwd=generator_root, capture_output=True, text=True, timeout=60)
        self.assertEqual(restore.returncode, 0, restore.stderr or restore.stdout)
        build = subprocess.run([toolchain["dotnet"], "build", "FixtureGenerator.csproj", "--no-restore", "-c", "Release"],
                               cwd=generator_root, capture_output=True, text=True, timeout=60)
        self.assertEqual(build.returncode, 0, build.stderr or build.stdout)
        analyzer = generator_root / "bin" / "Release" / "net8.0" / "FixtureGenerator.dll"
        (project / "Assets" / "Consumer.cs").write_text(
            "public class Consumer { public static string Read() => GeneratedFixture.Marker.Value; }\n", encoding="utf-8")
        (project / "Assets" / "Usage.cs").write_text("public sealed class Usage { public Consumer value; }\n", encoding="utf-8")
        additional = project / "Assets" / "Data.txt"
        additional.write_text("frozen-value\n", encoding="utf-8")
        refs = "".join(f'-r:"{path}"\n' for path in FRAMEWORK_REFS.glob("*.dll"))
        rsp = project / "Library" / "Bee" / "artifacts" / "GEN.dag" / "Fixture.rsp"
        rsp.write_text(f'-target:library\n-out:"Library/Bee/artifacts/GEN.dag/Fixture.dll"\n-langversion:9.0\n'
                       f'-analyzer:"{analyzer}"\n-additionalfile:"Assets/Data.txt"\n' + refs, encoding="utf-8")
        rows = [{"Name": "Fixture", "Path": str(project), "SourceFiles": ["Assets/Consumer.cs", "Assets/Usage.cs"],
                 "Defines": [], "References": [], "LanguageVersion": "9.0"}]
        (project / "Library" / "Bee" / "GEN-inputdata.json").write_text(
            json.dumps({"ScriptCompilationBuildProgram.Data.ScriptCompilationData": {"Assemblies": rows}}), encoding="utf-8")
        result = inspect(project, RUNS / "analyzer-inspect", {"responseFiles": [str(rsp)]})
        self.assertEqual(result["status"], "ok", Path(result["report"]).read_text())
        report = json.loads(Path(result["report"]).read_text())
        fixture = next(row for row in report["assemblies"] if row["name"] == "Fixture")
        self.assertEqual(len(fixture["generatedSources"]), 1)
        self.assertTrue(fixture["generatedSources"][0]["sha256"])
        self.assertIn("FixtureGenerator", fixture["loadedGenerators"])
        self.assertEqual(fixture["analyzerDiagnostics"], [])
        context = next(row for row in report["compileContext"] if row["name"] == "Fixture")
        import hashlib
        self.assertEqual(context["additionalFiles"][0]["sha256"], hashlib.sha256(additional.read_bytes()).hexdigest())
        planned = rename_plan(project, {"kind": "type", "symbol": "Consumer", "newName": "RenamedConsumer", "assembly": "Fixture"},
                              RUNS / "analyzer-rename", {"responseFiles": [str(rsp)]})
        self.assertEqual(planned["status"], "ok", Path(planned["report"]).read_text())
        plan_report = json.loads(Path(planned["report"]).read_text())
        baseline = next(row for row in plan_report["assemblies"] if row["name"] == "Fixture")
        candidate = next(row for row in plan_report["candidateAssemblies"] if row["name"] == "Fixture")
        self.assertEqual(len(baseline["generatedSources"]), 1)
        self.assertEqual(candidate["generatedSources"], baseline["generatedSources"])
        additional.write_text("Consumer\n", encoding="utf-8")
        blocked = rename_plan(project, {"kind": "type", "symbol": "Consumer", "newName": "IgnoredConsumer", "assembly": "Fixture"},
                              RUNS / "analyzer-generated-string", {"responseFiles": [str(rsp)]})
        self.assertEqual(blocked["status"], "blocked")
        blocked_report = json.loads(Path(blocked["report"]).read_text())
        self.assertTrue(any(row.get("code") == "UNKNOWN_GENERATED_STRING_REFERENCE"
                            for row in blocked_report["unknownReferences"]))
        additional.write_text("Throw\n", encoding="utf-8")
        failed_generation = inspect(project, RUNS / "analyzer-failed-generation", {"responseFiles": [str(rsp)]})
        self.assertEqual(failed_generation["status"], "blocked")
        failed_report = json.loads(Path(failed_generation["report"]).read_text())
        self.assertTrue(any(row.get("code") == "SOURCE_GENERATOR_FAILED" for row in failed_report["diagnostics"]))

    def test_module_template_has_owned_five_layers_and_real_binding_entry(self):
        module_spec = {
            "profile": "beaver-legacy-five-layer/1.0.0",
            "name": "Inventory", "namespace": "Game.UI", "assembly": "Game.Inventory", "owner": "inventory-team",
            "fields": [{"name": "capacity", "type": "int"}],
            "references": {"Domain": [], "Application": ["Game.Inventory.Domain"],
                           "Infrastructure": ["Game.Inventory.Domain"], "Presentation": [],
                           "Bootstrap": ["Game.Inventory.Application", "Game.Inventory.Presentation"]},
            "viewBindings": [{"name": "root", "type": "RectTransform"}],
        }
        result = module_generate(module_spec, RUNS / "module")
        self.assertEqual(result["status"], "ok")
        manifest = json.loads(Path(result["report"]).read_text())
        self.assertEqual(len(manifest["files"]), 10)
        self.assertEqual(manifest["owner"], "inventory-team")
        source = (Path(result["report"]).parent / "Assets" / "Game" / "Bootstrap" / "Inventory" / "InventoryModule.cs").read_text()
        self.assertIn("ValidateBindings()", source)
        self.assertNotIn("TODO", source)
        reader = (Path(result["report"]).parent / "Assets" / "Game" / "Infrastructure" / "Inventory" / "InventoryJsonReader.cs").read_text()
        self.assertIn("duplicate property", reader)
        self.assertIn("unknown property", reader)
        self.assertIn("missing property", reader)
        self.assertIn("ValidateInt", reader)
        compile_root = RUNS / "module-compile"
        shutil.copytree(Path(result["report"]).parent / "Assets", compile_root / "Assets")
        shutil.copy2(FIXTURES / "EngineStubs.cs", compile_root / "UnityStubs.cs")
        framework = sorted(str(p) for p in FRAMEWORK_REFS.glob("*.dll"))
        project_names = {layer: "Game.Inventory." + layer for layer in
                         ("Domain", "Application", "Infrastructure", "Presentation", "Bootstrap")}
        dependency_map = {"Domain": [], "Application": ["Domain"], "Infrastructure": ["Domain"],
                          "Presentation": [], "Bootstrap": ["Application", "Presentation"]}
        assemblies = [{"name": "UnityStubs", "sourceFiles": ["UnityStubs.cs"], "references": framework}]
        for layer, assembly_name in project_names.items():
            source_files = sorted(path.relative_to(compile_root).as_posix()
                                  for path in (compile_root / "Assets" / "Game" / layer / "Inventory").glob("*.cs"))
            unity_reference = ["UnityStubs"] if layer in ("Infrastructure", "Presentation", "Bootstrap") else []
            assemblies.append({"name": assembly_name, "sourceFiles": source_files, "references": framework,
                               "projectReferences": [*unity_reference, *[project_names[item] for item in dependency_map[layer]]]})
        compiled = inspect(compile_root, RUNS / "module-compile-report", {"assemblies": assemblies})
        self.assertEqual(compiled["status"], "ok", Path(compiled["report"]).read_text())
        update = dict(module_spec, fields=[{"name": "capacity", "type": "long"}],
                      baselinePath=str(Path(result["report"]).parent), previousOwner="inventory-team",
                      previousOwnedFiles=manifest["ownership"]["ownedPaths"])
        updated = module_generate(update, RUNS / "module-updated")
        updated_manifest = json.loads(Path(updated["report"]).read_text())
        self.assertIn("Assets/Game/Domain/Inventory/InventoryData.cs", updated_manifest["diff"]["changed"])
        self.assertTrue(updated_manifest["reads"])
        with self.assertRaises(BeaverError) as raised:
            module_generate(dict(module_spec, baselinePath=str(Path(result["report"]).parent)), RUNS / "module-unowned")
        self.assertEqual(raised.exception.code, "CSHARP_MODULE_UNOWNED_COLLISION")

    def test_unity_standard_profile_drives_layer_paths_namespaces_and_dependencies(self):
        module_spec = {
            "name": "Inventory", "namespace": "Game", "assembly": "Game.Inventory", "owner": "inventory-team",
            "fields": [{"name": "capacity", "type": "int"}],
            "references": {layer: [] for layer in ("Bootstrap", "Domain", "Gameplay", "Presentation", "Services")},
        }
        result = module_generate(module_spec, RUNS / "module-standard")
        self.assertEqual(result["status"], "ok")
        root = Path(result["report"]).parent
        manifest = json.loads(Path(result["report"]).read_text())
        self.assertEqual(manifest["profile"], "grea-unity-project/1.0.0")
        self.assertEqual(manifest["layers"], ["Bootstrap", "Domain", "Gameplay", "Presentation", "Services"])
        expected = {
            "Domain": [], "Gameplay": ["Game.Inventory.Domain"],
            "Services": ["Game.Inventory.Domain", "Game.Inventory.Gameplay"],
            "Presentation": ["Game.Inventory.Domain", "Game.Inventory.Gameplay"],
            "Bootstrap": ["Game.Inventory.Domain", "Game.Inventory.Gameplay", "Game.Inventory.Presentation", "Game.Inventory.Services"],
        }
        for layer, deps in expected.items():
            asmdef_path = root / "Assets" / "Game" / layer / "Inventory" / f"Game.Inventory.{layer}.asmdef"
            asmdef = json.loads(asmdef_path.read_text())
            self.assertEqual(asmdef["name"], f"Game.Inventory.{layer}")
            self.assertEqual(asmdef["rootNamespace"], f"Game.{layer}.Inventory")
            self.assertEqual(asmdef["references"], deps)
            self.assertEqual(asmdef["noEngineReferences"], layer in ("Domain", "Gameplay"))
        self.assertTrue((root / "Assets/Game/Gameplay/Inventory/InventoryGameplayContext.cs").is_file())
        self.assertTrue((root / "Assets/Game/Services/Inventory/InventoryJsonReader.cs").is_file())
        with self.assertRaises(BeaverError) as raised:
            module_generate(dict(module_spec, references={"Domain": [], "Application": [], "Infrastructure": [],
                                                           "Presentation": [], "Bootstrap": []}), RUNS / "module-mixed-profile")
        self.assertEqual(raised.exception.code, "CSHARP_MODULE_SPEC")
        bad_direction = dict(module_spec, references={**module_spec["references"],
                                                       "Gameplay": ["Game.Inventory.Bootstrap"]})
        with self.assertRaises(BeaverError) as raised:
            module_generate(bad_direction, RUNS / "module-bad-direction")
        self.assertEqual(raised.exception.code, "CSHARP_MODULE_PROFILE_MIXED")


if __name__ == "__main__":
    unittest.main()
