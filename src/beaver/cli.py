"""Versioned JSON CLI. No shell passthrough and no automatic provider installs."""
import argparse
import importlib.util
import json
from pathlib import Path
import shutil
import sys
import uuid
from . import __version__
from .errors import BeaverError, fail
from .io import sha, encoded


def implementation_identity():
    from .implementation import identity
    return identity()


def capabilities():
    capabilities = [
        dict(id='workspace.plan/apply/inspect/rebind',state='implemented',effects='explicit-project-local-binding',verification='content-pinned tool/engine/authority, reviewed writes and relocation'),
        dict(id='toolkit.grea',state='implemented',effects='explicit-output-write',tools=list(__import__('beaver.toolkit',fromlist=['TOOLS']).TOOLS),verification='fixed toolkit from commit 6791ddf; original outputs and exit codes retained'),
        dict(id='ui.inspect/analyze/repair-plan',state='requires_provider',provider='Unity 6000.6.2f1 uGUI/TMP for collection',effects='read-temporary-helper-and-report',verification='native geometry/compiled fields; static, runtime and caller-supplied provenance separated'),
        dict(id='wiring.plan',state='implemented',effects='plan-output',verification='compiled types and exact identities; ambiguous targets blocked; existing batch apply required'),
        dict(id='diagnostics.summarize',state='implemented',effects='read-and-report',verification='bounded evidence and explicit execution state; never replays writes'),
        dict(id='production.plan/apply/recover',state='requires_provider',provider='Unity 6000.6.2f1 + explicit project profile',effects='declared-resource-config-code-writes',verification='schema3; native apply/reopen/recover verified on private fixtures; formal consumer evidence is project-specific'),
        dict(id='config3.validate/generate/compare',state='implemented',effects='read-and-create-only-generated-code',verification='strict shared schema; actual C# and Go JSON readers; caller must run generated consumer'),
        dict(id='csharp.inspect/rename/rename-sequence',state='requires_provider',provider='matching Unity Bee context + .NET Roslyn SDK',effects='read-and-create-only-source-candidates',verification='ordered semantic rename, analyzers/generators, exact final fileEdits; dynamic unknowns block'),
        dict(id='csharp.module',state='implemented',effects='create-only-module-candidate',verification='owned five-layer templates and strict flat JSON reader; no existing assembly mutation'),
        dict(id='resource.image-inspect/image-produce/validate-step',state='implemented',effects='read-and-create-only-image-variants',verification='closed resource contracts; Pillow required for image operations; native resource execution remains provider-bound'),
        dict(id='server.generate/module-generate/module-plan/protocol-diff/migration-plan',state='implemented',effects='create-only-go-candidate-and-plans',verification='six roles, explicit ownership, same JSON schema; no production deploy or payment success'),
        dict(id='server.toolchain/protocol-tools/check/build',state='requires_provider',provider='Go + protoc + pinned Go plugins',effects='local-toolchain-build-output',verification='actual compiler/protocol generation and atomic candidate binaries; no global install'),
        dict(id='server.compose-plan/migration-state/migration-apply/runtime-inspect',state='requires_provider',provider='explicit owned Docker Compose target + MySQL + Redis',effects='plan-or-explicit-owner-schema-mutation',verification='candidate/compose/container/network/volume identity and current schema SHA; partial or unknown execution requires inspection'),
        dict(id='batch.plan',state='implemented',effects='plan-output',verification='scoped hashes, dependency/compilation inputs; cache is not authority'),
        dict(id='batch.apply',state='requires_provider',provider='Unity 6000.6.2f1 + project uGUI/TMP',effects='declared-project-write',verification='one Editor process; native checkpoint and serialized save/reload checks'),
        dict(id='batch.recover',state='requires_provider',provider='same pinned beaver + Unity 6000.6.2f1',effects='hash-owned-restore',verification='exact witnessed after state required; later edits and missing checkpoints block'),
        dict(id='references',state='implemented',effects='read-and-index-output',verification='static GUID graph, literal spans, unknown dynamic keys; incremental cache'),
        dict(id='debug.start',state='implemented',effects='owned-budgeted-process',verification='fixed candidate manifest; asynchronous worker'),
        dict(id='debug.inspect',state='implemented',effects='read',verification='PID/executable/start/marker identity; unknown probes remain partial'),
        dict(id='debug.target',state='implemented',effects='read',verification='pinned Editor/PlayMode transaction evidence; no external attachment'),
        dict(id='debug.stop',state='implemented',effects='own-worker-stop-request',verification='worker Popen child ownership; no arbitrary PID signals'),
        dict(id='debug.tail',state='implemented',effects='read',verification='bounded redacted own session log'),
        dict(id='debug.screenshot',state='implemented',effects='evidence-registration',verification='caller supplied hash only; screenshot origin not authenticated'),
        dict(id="project.inspect", state="implemented", effects="read", verification="python"),
        dict(id="adoption.plan", state="implemented", effects="plan-output", engines=["unity-6000.6.2f1", "creator-3.8.8"], verification="python; large isolated Unity manifest preview"),
        dict(id="adoption.apply", state="requires_provider", provider="Unity 6000.6.2f1", effects="project-write", verification="native add/move/save/reopen and interrupted-import fixture"),
        dict(id="adoption.rollback", state="requires_provider", provider="Unity 6000.6.2f1", effects="project-write", verification="native normal/failed transaction restore; later-edit rejection"),
        dict(id="unity.plan", state="implemented", effects="plan-output", verification="python; exact source and implementation identity"),
        dict(id="unity.ui.create", state="requires_provider", provider="Unity 6000.6.2f1 + uGUI/TMP Essentials + explicit font", effects="project-write", verification="native save/reopen/source audit"),
        dict(id="unity.prefab.inspect", state="requires_provider", provider="Unity 6000.6.2f1", effects="temporary-editor-helper", verification="native hierarchy/localID/component/importer readback"),
        dict(id="unity.prefab.edit", state="requires_provider", provider="Unity 6000.6.2f1", effects="project-write", verification="native text/component/Sprite binding save/reopen"),
        dict(id="unity.assets.move", state="requires_provider", provider="Unity 6000.6.2f1", effects="project-write", verification="native GUID/localID and save/reopen"),
        dict(id="unity.project.import", state="requires_provider", provider="Unity 6000.6.2f1", effects="declared-meta-initialization", verification="native generated C# compile and import"),
        dict(id="unity.sprite.configure", state="requires_provider", provider="Unity 6000.6.2f1", effects="selected-importer-meta", verification="native importer/GUID/localID and save/reopen"),
        dict(id="unity.validate.play", state="requires_provider", provider="Unity 6000.6.2f1", effects="bounded-playmode", verification="3-second isolated scene; zero runtime errors"),
        dict(id="unity.build.macos", state="requires_provider", provider="Unity 6000.6.2f1 + Mac module", effects="create-only-artifact", verification="native BuildPipeline; unchanged source; artifact manifest"),
        dict(id="player.run", state="implemented", effects="bounded-owned-process", verification="hash-bound macOS artifact launch; application verdict remains separate"),
        dict(id="image.inspect", state="requires_provider", provider="Pillow", effects="read", verification="exact RGBA/alpha fixture"),
        dict(id="image.compress", state="requires_provider", provider="Pillow", effects="create-only", verification="lossless PNG stream and exact decoded RGBA"),
        dict(id="config.validate", state="implemented", effects="read", verification="strict scalar JSON; native runtime rejects missing/wrong-type fields"),
        dict(id="config.generate", state="implemented", effects="create-only", verification="generated C# compiled; runtime TextAsset JSON reader"),
        dict(id="code.inspect", state="implemented", effects="read", verification="static asmdef/using inventory; unresolved optional constraints remain partial"),
        dict(id="payload.verify", state="implemented", effects="read", verification="shared file/alias/inode/collision checks; also used by build and Player"),
        dict(id="payload.plan", state="implemented", effects="plan-output", verification="explicit source/override identities and alias redirection"),
        dict(id="payload.apply", state="implemented", effects="new-independent-payload", verification="macOS independent-copy and atomic no-replace fixtures; Linux/Windows promotion not_run"),
        dict(id="creator.apply", state="not_supported", effects="none", verification="explicit rejection before provider lookup"),
        dict(id="creator.build", state="not_supported", effects="none", verification="not_run")
    ]
    return dict(contract="beaver-capabilities/1", capabilities=capabilities, requiredProviders=[],
                nativeOperationContract="beaver-unity/1", batchOperationContract='beaver-batch/2',batchTransactionContract='beaver-batch-transaction/2',adoptionContract="beaver-adopt/1",
                productionOperationContract='beaver-production/3',productionTransactionContract='beaver-production-transaction/3',projectProfileContract='beaver-project/1',
                limitations=["Provider readiness is not Editor connection or gameplay", "Native tests cover Unity 6000.6.2f1 only", "No source package parser; candidates are produced by monkey", "No global install, release, account/service/savegame writes", "Native receipts do not prove physical input, visual parity or game business acceptance"])


class Parser(argparse.ArgumentParser):
    def error(self, message):
        fail("CLI_USAGE", "arguments", message)


def parser():
    p = Parser(prog="beaver")
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("version")
    sub.add_parser("capabilities")
    doctor = sub.add_parser("doctor")
    doctor.add_argument("--editor-path")
    project = sub.add_parser("project").add_subparsers(dest="action", required=True)
    inspect = project.add_parser("inspect")
    inspect.add_argument("--project", required=True)
    inspect.add_argument("--assets", action="store_true")
    adopt = sub.add_parser("adopt").add_subparsers(dest="action", required=True)
    plan = adopt.add_parser("plan")
    plan.add_argument("--candidate", required=True)
    plan.add_argument("--baseline")
    plan.add_argument("--target", required=True)
    plan.add_argument("--lineage")
    plan.add_argument("--out", required=True)
    apply = adopt.add_parser("apply")
    apply.add_argument("--plan", required=True)
    apply.add_argument("--expected-plan", required=True)
    apply.add_argument("--out", required=True)
    apply.add_argument("--editor-path", required=True)
    apply.add_argument("--timeout", type=int, default=600)
    recover = adopt.add_parser("rollback")
    recover.add_argument("--transaction", required=True)
    recover.add_argument("--editor-path", required=True)
    recover.add_argument("--timeout", type=int, default=600)
    image = sub.add_parser("image").add_subparsers(dest="action", required=True)
    image_inspect = image.add_parser("inspect")
    image_inspect.add_argument("--input", required=True)
    compress = image.add_parser("compress")
    compress.add_argument("--input", required=True)
    compress.add_argument("--output", required=True)
    compress.add_argument("--apply", action="store_true")
    compress.add_argument("--expected-source")
    config = sub.add_parser("config").add_subparsers(dest="action", required=True)
    validate = config.add_parser("validate")
    validate.add_argument("--schema", required=True)
    validate.add_argument("--input", required=True)
    generate = config.add_parser("generate")
    generate.add_argument("--schema", required=True)
    generate.add_argument("--out", required=True)
    code=sub.add_parser('code').add_subparsers(dest='action',required=True)
    code_inspect=code.add_parser('inspect')
    code_inspect.add_argument('--project',required=True)
    unity = sub.add_parser("unity").add_subparsers(dest="action", required=True)
    unity_plan = unity.add_parser("plan")
    unity_plan.add_argument("--project", required=True)
    unity_plan.add_argument("--spec", required=True)
    unity_plan.add_argument("--out", required=True)
    unity_apply = unity.add_parser("apply")
    unity_apply.add_argument("--plan", required=True)
    unity_apply.add_argument("--expected-plan", required=True)
    unity_apply.add_argument("--out", required=True)
    unity_apply.add_argument("--editor-path", required=True)
    unity_apply.add_argument("--timeout", type=int, default=600)
    batch = sub.add_parser('batch').add_subparsers(dest='action',required=True)
    bp=batch.add_parser('plan');bp.add_argument('--project',required=True);bp.add_argument('--spec',required=True);bp.add_argument('--out',required=True)
    ba=batch.add_parser('apply');ba.add_argument('--plan',required=True);ba.add_argument('--expected-plan',required=True);ba.add_argument('--out',required=True);ba.add_argument('--editor-path',required=True);ba.add_argument('--timeout',type=int,default=600)
    br=batch.add_parser('recover');br.add_argument('--transaction',required=True);br.add_argument('--out',required=True);br.add_argument('--editor-path',required=True);br.add_argument('--timeout',type=int,default=600)
    workspace=sub.add_parser('workspace').add_subparsers(dest='action',required=True)
    wp=workspace.add_parser('plan');wp.add_argument('--project',required=True);wp.add_argument('--spec',required=True);wp.add_argument('--out',required=True)
    wa=workspace.add_parser('apply');wa.add_argument('--plan',required=True);wa.add_argument('--expected-plan',required=True);wa.add_argument('--out',required=True)
    wi=workspace.add_parser('inspect');wi.add_argument('--project',required=True);wi.add_argument('--profile')
    wr=workspace.add_parser('rebind');wr.add_argument('--project',required=True);wr.add_argument('--profile',required=True);wr.add_argument('--expected-profile',required=True);wr.add_argument('--out',required=True);wr.add_argument('--accept-authority-change',action='store_true')
    wr.add_argument('--spec')
    production=sub.add_parser('production').add_subparsers(dest='action',required=True)
    pp=production.add_parser('plan');pp.add_argument('--project',required=True);pp.add_argument('--spec',required=True);pp.add_argument('--out',required=True)
    pa=production.add_parser('apply');pa.add_argument('--plan',required=True);pa.add_argument('--expected-plan',required=True);pa.add_argument('--out',required=True);pa.add_argument('--editor-path',required=True);pa.add_argument('--timeout',type=int,default=600)
    pr=production.add_parser('recover');pr.add_argument('--transaction',required=True);pr.add_argument('--out',required=True);pr.add_argument('--editor-path',required=True);pr.add_argument('--timeout',type=int,default=600)
    config3=sub.add_parser('config3').add_subparsers(dest='action',required=True)
    cv=config3.add_parser('validate');cv.add_argument('--schema',required=True);cv.add_argument('--input',required=True);cv.add_argument('--tables');cv.add_argument('--project')
    cg=config3.add_parser('generate');cg.add_argument('--schema',required=True);cg.add_argument('--out',required=True)
    cc=config3.add_parser('compare');cc.add_argument('--before-schema',required=True);cc.add_argument('--after-schema',required=True);cc.add_argument('--before-input');cc.add_argument('--after-input')
    csharp=sub.add_parser('csharp').add_subparsers(dest='action',required=True)
    ci=csharp.add_parser('inspect');ci.add_argument('--project',required=True);ci.add_argument('--out',required=True);ci.add_argument('--toolchain')
    cr=csharp.add_parser('rename');cr.add_argument('--project',required=True);cr.add_argument('--spec',required=True);cr.add_argument('--out',required=True);cr.add_argument('--toolchain')
    cs=csharp.add_parser('rename-sequence');cs.add_argument('--project',required=True);cs.add_argument('--spec',required=True);cs.add_argument('--out',required=True);cs.add_argument('--toolchain')
    cm=csharp.add_parser('module');cm.add_argument('--spec',required=True);cm.add_argument('--out',required=True)
    resources=sub.add_parser('resource').add_subparsers(dest='action',required=True)
    ri=resources.add_parser('image-inspect');ri.add_argument('--input',required=True)
    rp=resources.add_parser('image-produce');rp.add_argument('--input',required=True);rp.add_argument('--out',required=True);rp.add_argument('--policy',required=True);rp.add_argument('--expected-source',required=True)
    rv=resources.add_parser('validate-step');rv.add_argument('--project',required=True);rv.add_argument('--spec',required=True)
    server=sub.add_parser('server').add_subparsers(dest='action',required=True)
    server.add_parser('toolchain')
    si=server.add_parser('inspect');si.add_argument('--project',required=True)
    for name in ('generate','module-generate','compose-plan'):
        item=server.add_parser(name);item.add_argument('--spec',required=True);item.add_argument('--out',required=True)
    sm=server.add_parser('module-plan');sm.add_argument('--project',required=True);sm.add_argument('--spec',required=True);sm.add_argument('--out',required=True)
    sd=server.add_parser('protocol-diff');sd.add_argument('--before',required=True);sd.add_argument('--after',required=True)
    sg=server.add_parser('migration-plan');sg.add_argument('--project',required=True);sg.add_argument('--out',required=True)
    sb=server.add_parser('check');sb.add_argument('--project',required=True);sb.add_argument('--package',action='append')
    scb=server.add_parser('build');scb.add_argument('--project',required=True);scb.add_argument('--architecture',choices=('arm64','amd64'),default='arm64')
    spt=server.add_parser('protocol-tools');spt.add_argument('--out',required=True)
    sms=server.add_parser('migration-state');sms.add_argument('--compose',required=True);sms.add_argument('--project-name',required=True);sms.add_argument('--owner',required=True,choices=('login','server','chat','world','billing'))
    sri=server.add_parser('runtime-inspect');sri.add_argument('--compose',required=True);sri.add_argument('--project-name',required=True);sri.add_argument('--gateway-port',required=True,type=int)
    sma=server.add_parser('migration-apply');sma.add_argument('--project',required=True);sma.add_argument('--plan',required=True);sma.add_argument('--compose',required=True);sma.add_argument('--project-name',required=True);sma.add_argument('--owner',required=True,choices=('login','server','chat','world','billing'));sma.add_argument('--expected-state',required=True)
    refs=sub.add_parser('references');refs.add_argument('--project',required=True);refs.add_argument('--out',required=True);refs.add_argument('--cache')
    debug=sub.add_parser('debug').add_subparsers(dest='action',required=True)
    ds=debug.add_parser('start');ds.add_argument('--manifest',required=True);ds.add_argument('--expected',required=True);ds.add_argument('--out',required=True);ds.add_argument('--duration',type=int,default=3600);ds.add_argument('--width',type=int);ds.add_argument('--height',type=int)
    for name in ('inspect','stop','tail','screenshot'):
        d=debug.add_parser(name);d.add_argument('--session',required=True)
        if name=='tail':d.add_argument('--lines',type=int,default=80)
        if name=='screenshot':d.add_argument('--input',required=True);d.add_argument('--expected',required=True)
    dt=debug.add_parser('target');dt.add_argument('--transaction',required=True)
    player=sub.add_parser('player').add_subparsers(dest='action',required=True)
    player_run=player.add_parser('run')
    player_run.add_argument('--manifest',required=True)
    player_run.add_argument('--out',required=True)
    player_run.add_argument('--duration',type=int,default=10)
    player_run.add_argument('--expected-manifest',required=True)
    payload=sub.add_parser('payload').add_subparsers(dest='action',required=True)
    payload_verify=payload.add_parser('verify')
    payload_verify.add_argument('--root',required=True)
    payload_verify.add_argument('--manifest',required=True)
    payload_verify.add_argument('--scope')
    payload_verify.add_argument('--allow-unlisted',action='store_true')
    payload_verify.add_argument('--unity-artifact',action='store_true')
    payload_plan=payload.add_parser('plan')
    payload_plan.add_argument('--root',required=True)
    payload_plan.add_argument('--manifest',required=True)
    payload_plan.add_argument('--target',required=True)
    payload_plan.add_argument('--out',required=True)
    payload_plan.add_argument('--overrides')
    payload_plan.add_argument('--override-root')
    payload_apply=payload.add_parser('apply')
    payload_apply.add_argument('--plan',required=True)
    payload_apply.add_argument('--expected-plan',required=True)
    payload_apply.add_argument('--out',required=True)
    ui = sub.add_parser('ui').add_subparsers(dest='action',required=True)
    uinspect = ui.add_parser('inspect')
    for flag in ('project','spec','out','editor-path'):uinspect.add_argument('--'+flag,required=True)
    uinspect.add_argument('--timeout',type=int,default=600)
    urepair = ui.add_parser('repair-plan')
    for flag in ('project','snapshot','spec','out'):urepair.add_argument('--'+flag,required=True)
    uanalyze = ui.add_parser('analyze')
    for flag in ('snapshot','spec','out'):uanalyze.add_argument('--'+flag,required=True)
    wiring = sub.add_parser('wiring').add_subparsers(dest='action',required=True).add_parser('plan')
    for flag in ('project','snapshot','spec','out'):wiring.add_argument('--'+flag,required=True)
    diagnostic = sub.add_parser('diagnostics').add_subparsers(dest='action',required=True).add_parser('summarize')
    diagnostic.add_argument('--input',action='append',required=True)
    diagnostic.add_argument('--out',required=True)
    diagnostic.add_argument('--limit',type=int,default=100)
    return p


def dispatch(a):
    if a.command=='ui':
        if a.action=='repair-plan':
            from .ui_repairs import plan
            return plan(a.project,a.snapshot,a.spec,a.out)
        from .ui_inspection import inspect,analyze
        if a.action=='inspect':return inspect(a.project,a.spec,a.out,a.editor_path,a.timeout)
        from .io import read,load,new_output,write_json
        data=analyze(load(read(a.snapshot)),load(read(a.spec)))
        data['provenance']='caller-supplied snapshot; collection origin not authenticated'
        stage=new_output(a.out,[a.snapshot,a.spec]);write_json(stage/'ui-report.json',data)
        return dict(data,report=str(stage/'ui-report.json'))
    if a.command=='wiring':
        from .wiring import plan
        return plan(a.project,a.snapshot,a.spec,a.out)
    if a.command=='diagnostics':
        from .diagnostics import summarize
        result=summarize(a.input,a.out,a.limit)
        report=result.get('report',result)
        return dict(status='partial' if report.get('status') in ('unknown','execution_unknown','postexecution_outer_schema_rejection') else report.get('status'),diagnosticReport=report,report=result.get('path'))
    if a.command == "version":
        return dict(version=__version__)
    if a.command == "capabilities":
        return capabilities()
    if a.command == "doctor":
        from .io import safe
        editor = safe(a.editor_path) if a.editor_path else None
        return dict(python=sys.version.split()[0], unityCLI=shutil.which("unity"),
                    pillow="available" if importlib.util.find_spec("PIL") else "not_available",
                    editor=dict(path=str(editor) if editor else None, present=editor.is_file() if editor else False),
                    nativeConnection="not_probed", autoInstall=False)
    if a.command == "project":
        from .context import inspect
        return inspect(a.project, a.assets)
    if a.command == "adopt":
        if a.action == "plan":
            from .adoption import preview
            return preview(a.candidate, a.target, a.out, a.baseline, a.lineage)
        if a.action == "apply":
            from .adoption_native import execute
            return execute(a.plan, a.out, a.editor_path, a.timeout, a.expected_plan)
        from .adoption_native import recover
        return recover(a.transaction, a.editor_path, a.timeout)
    if a.command == "image":
        from .images import inspect, compress
        return inspect(a.input) if a.action == "inspect" else compress(a.input, a.output, a.apply, a.expected_source)
    if a.command == "config":
        from .configuration import validate, generate
        return validate(a.schema, a.input) if a.action == "validate" else generate(a.schema, a.out)
    if a.command == "unity":
        from .unity import plan, apply
        return plan(a.project,a.spec,a.out) if a.action == "plan" else apply(a.plan,a.out,a.editor_path,a.expected_plan,a.timeout)
    if a.command=='batch':
        from .batch import plan,apply,recover
        if a.action=='plan':return plan(a.project,a.spec,a.out)
        if a.action=='apply':return apply(a.plan,a.out,a.editor_path,a.expected_plan,a.timeout)
        return recover(a.transaction,a.out,a.editor_path,a.timeout)
    if a.command=='workspace':
        from .project_profile import setup_plan,setup_apply,rebind_plan,inspect
        if a.action=='plan':return setup_plan(a.project,a.spec,a.out)
        if a.action=='apply':return setup_apply(a.plan,a.expected_plan,a.out)
        if a.action=='inspect':return inspect(a.project,a.profile)
        return rebind_plan(a.project,a.profile,a.expected_profile,a.out,a.accept_authority_change,a.spec)
    if a.command=='production':
        from .production import plan,apply,recover
        if a.action=='plan':return plan(a.project,a.spec,a.out)
        if a.action=='apply':return apply(a.plan,a.out,a.editor_path,a.expected_plan,a.timeout)
        return recover(a.transaction,a.out,a.editor_path,a.timeout)
    if a.command=='config3':
        from .configuration_v3 import validate,generate,compare
        from .io import load,read
        if a.action=='validate':return validate(a.schema,a.input,load(read(a.tables)) if a.tables else None,a.project)
        if a.action=='generate':return generate(a.schema,a.out)
        return compare(a.before_schema,a.after_schema,a.before_input,a.after_input)
    if a.command=='csharp':
        from .csharp import inspect,rename_plan,rename_sequence_plan,module_generate
        from .io import load,read
        if a.action=='module':return module_generate(a.spec,a.out)
        toolchain=load(read(a.toolchain)) if a.toolchain else None
        if a.action=='rename-sequence':return rename_sequence_plan(a.project,load(read(a.spec)),a.out,toolchain)
        return inspect(a.project,a.out,toolchain) if a.action=='inspect' else rename_plan(a.project,a.spec,a.out,toolchain)
    if a.command=='resource':
        from .resources import inspect_image,produce_image,validate_step,declared_paths
        from .io import load,read
        if a.action=='image-inspect':return dict(status='ok',**inspect_image(a.input))
        if a.action=='image-produce':return produce_image(a.input,a.out,load(read(a.policy)),a.expected_source)
        step=validate_step(load(read(a.spec)))
        return dict(status='ok',step=step,paths=declared_paths(a.project,step),native='not_run')
    if a.command=='server':
        from . import server_go as server
        from .io import load,read
        if a.action=='toolchain':return server.toolchain_inspect()
        if a.action=='inspect':return server.inspect(a.project)
        if a.action=='generate':return server.generate(load(read(a.spec)),a.out)
        if a.action=='module-generate':return server.generate_module(load(read(a.spec)),a.out)
        if a.action=='module-plan':return server.module_plan(a.project,load(read(a.spec)),a.out)
        if a.action=='compose-plan':return server.compose_plan(load(read(a.spec)),a.out)
        if a.action=='protocol-diff':return server.protocol_diff(a.before,a.after)
        if a.action=='migration-plan':return server.migration_plan(a.project,a.out)
        if a.action=='build':return server.build_candidate(a.project,a.architecture)
        if a.action=='protocol-tools':return server.prepare_protocol_tools(a.out)
        if a.action=='migration-state':return server.inspect_migration_state(a.compose,a.project_name,a.owner)
        if a.action=='runtime-inspect':return server.inspect_compose_runtime(a.compose,a.project_name,a.gateway_port)
        if a.action=='migration-apply':return server.execute_migrations(a.project,a.plan,a.compose,a.project_name,a.owner,a.expected_state)
        return server.build_diagnostics(a.project,a.package)
    if a.command=='references':
        from .references import inspect_graph
        result=inspect_graph(a.project,a.out,a.cache)
        return dict(status=result['status'],report=result['report'],counts=result['counts'],timingMs=result['timingMs'],
                    indexedGuids=len(result['guidToPath']),dependencySources=len(result['dependencies']),literalLoads=len(result['literalLoads']),
                    unknownDynamicLoads=len(result['unknownDynamicLoads']),unresolvedJsonStrings=len(result['unresolvedJsonStrings']),
                    limitations=result['limitations'])
    if a.command=='debug':
        from .debug import start,inspect_session,stop,tail,screenshot_record,inspect_native
        if a.action=='target':return inspect_native(a.transaction)
        if a.action=='start':return start(a.manifest,a.out,a.expected,a.duration,a.width,a.height)
        if a.action=='inspect':return inspect_session(a.session)
        if a.action=='stop':return stop(a.session)
        if a.action=='tail':return tail(a.session,a.lines)
        return screenshot_record(a.session,a.input,a.expected)
    if a.command == 'code':
        from .codecheck import inspect_code
        return inspect_code(a.project)
    if a.command == 'player':
        from .player import run
        return run(a.manifest,a.out,a.duration,a.expected_manifest)
    if a.command=='payload':
        from .payload import verify_manifest,plan,apply
        if a.action=='verify':return verify_manifest(a.root,a.manifest,a.scope,allow_unlisted=a.allow_unlisted,artifact=a.unity_artifact)
        if a.action=='plan':return plan(a.root,a.manifest,a.target,a.out,a.overrides,a.override_root)
        return apply(a.plan,a.out,a.expected_plan)


def main(argv=None):
    argv=list(sys.argv[1:] if argv is None else argv)
    if argv==['--version']:
        argv=['version']
    if argv[:1]==["tool"]:
        from .toolkit.runner import main as run_tool
        return run_tool(argv[1:])
    run_id = uuid.uuid4().hex
    base = dict(schemaVersion=1, contract="beaver-result/1", operationId=run_id,
                producer=dict(name="beaver", version=__version__, implementationSha256=implementation_identity()),
                actualChanges=[], artifacts=[], diagnostics=[], limitations=[],
                verification=dict(native="not_run", player="not_run", visual="not_run", business="not_run"))
    try:
        a = parser().parse_args(argv)
        data = dispatch(a)
        status = data.get("status") if data.get("status") in ("blocked","partial","failed","not_run") else "ok"
        if a.command=='ui' and data.get('status')=='issues':status='partial'
        if data.get('status') in ('identity_unknown','starting','stopping','not_available'):status='partial'
        if data.get('status')=='needs_action':status='blocked'
        if data.get('status') in ('stale_plan','migration_order_blocked','blocked_existing_output','partial_or_untracked_database'):status='blocked'
        if data.get('status')=='failed_or_needs_action':status='failed'
        if data.get('status') in ('execution_unknown_or_failed','execution_unknown_or_partial'):status='partial'
        base.update(status=status, data=data)
        result_path = data.get("report")
        if result_path and Path(result_path).is_file():
            base["artifacts"].append(dict(path=result_path, sha256=sha(Path(result_path).read_bytes())))
        if data.get("nativeReadback"):
            base["verification"]["native"] = data["nativeReadback"]
        if a.command == "adopt" and a.action == "apply":
            base["actualChanges"] = [dict(operation='adoption.apply',transaction=data['transaction'],counts=data.get('counts',{}))]
        if a.command == "unity" and a.action == "apply":
            base["actualChanges"] = data.get("changes", [])
        if a.command in ('batch','production') and a.action in ('apply','recover'):
            base['actualChanges']=data.get('changes',[dict(operation=a.command+'.recover',restored=data.get('restored'))])
        if a.command=='workspace' and a.action=='apply':
            base['actualChanges']=[dict(operation='workspace.binding',path=data['profile'],sha256=data['profileSha256'])]
        if a.command=='server' and a.action=='migration-apply' and data.get('execution')!='not_run':
            base['actualChanges']=[dict(operation='server.migration',projectName=a.project_name,owner=a.owner,database=data.get('database'),status=data.get('status'),stateBeforeSha256=data.get('stateBeforeSha256'),stateAfterSha256=data.get('stateAfterSha256'))]
        if a.command=='server':
            output_path=data.get('composePath') or (a.out if a.action=='migration-plan' else None)
            if output_path and Path(output_path).is_file():base['artifacts'].append(dict(path=str(Path(output_path).resolve()),sha256=sha(Path(output_path).read_bytes())))
        if a.command=='adopt' and a.action=='rollback':
            base['verification']['native']='rolled_back'
            base['actualChanges']=[dict(operation='adoption.rollback',restored=data.get('restored'),report=data.get('report'))]
        if a.command=='player':
            base['verification']['player']='launched_and_exited' if not data.get('stoppedByBudget') else 'launched_and_stopped_by_budget'
        if a.command=='payload' and a.action=='apply':
            base['actualChanges']=[dict(operation='payload.materialize',targetRoot=data['targetRoot'],files=data['files'],overrides=len(data['overrides']))]
        print(json.dumps(base, ensure_ascii=False, allow_nan=False))
        return {'blocked':3,'partial':1,'failed':2,'not_run':3}.get(status,0)
    except (BeaverError, OSError, ValueError, TypeError, KeyError, ImportError, OverflowError) as error:
        blocked=isinstance(error,BeaverError) and error.status=='blocked'
        base.update(status="blocked" if blocked else "failed",
                    diagnostics=[error.as_dict() if isinstance(error, BeaverError) else dict(code="IO_OR_INPUT", location="input", message=str(error))])
        if isinstance(error,BeaverError):
            location=Path(error.location)
            if location.is_dir() and (location/'journal.json').is_file():
                base['artifacts'].append(dict(path=str(location/'journal.json'),sha256=sha((location/'journal.json').read_bytes())))
                base['actualChanges']=[dict(status='inspect_transaction',transaction=str(location),reason='Failure can follow partial native writes; no zero-write claim')]
                if '_NATIVE_FAILED' in error.code:
                    base['verification']['native']='failed'
        print(json.dumps(base, ensure_ascii=False))
        return 3 if blocked else 2


if __name__ == "__main__":
    sys.exit(main())
