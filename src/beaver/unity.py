"""Bounded native Unity jobs, immutable plans and create-only build artifacts."""
from pathlib import Path
from datetime import datetime, timezone
import os
import plistlib
import signal
import subprocess
import time
from .io import safe, inside, read, load, sha, encoded, write_json, new_output
from .errors import fail,BeaverError
from .output_limits import digest_file
from .adoption import project_files, native_lock
from .transaction_fs import lock
from .context import inspect
from .output_limits import BUILD_FILE_BYTES

HELPER_ROOT = "Assets/BeaverTools"
HELPER = HELPER_ROOT + "/Editor/BeaverNative.cs"
VERSION = "6000.6.2f1"


def identity():
    from .implementation import identity as package_identity
    return package_identity()


def asset_path(value, suffix):
    if not isinstance(value, str) or not value.startswith("Assets/") or not value.endswith(suffix) or "\\" in value or any(p in ("", ".", "..") for p in value.split("/")):
        fail("UNITY_ASSET_PATH", str(value), "Expected canonical Assets path with " + suffix)
    if value.startswith(HELPER_ROOT + "/"):
        fail("UNITY_RESERVED_PATH", value, "Reserved temporary Editor helper root")


def validate_spec(doc):
    operation = doc.get("operation")
    allowed = {"schemaVersion", "operation"}
    if operation == "ui.create":
        allowed |= {"prefabPath", "scenePath", "fontAssetPath", "sourceFontPath", "tmpSettingsPath", "width", "height", "nodes"}
        asset_path(doc.get("prefabPath"), ".prefab")
        asset_path(doc.get("scenePath"), ".unity")
        asset_path(doc.get("fontAssetPath"), ".asset")
        font=doc.get('sourceFontPath')
        if not isinstance(font,str) or not font.endswith(('.ttf','.otf')):
            fail('UI_SOURCE_FONT','spec','Explicit project font source required; no guessed font or modal import')
        asset_path(font,Path(font).suffix)
        if 'tmpSettingsPath' in doc:
            asset_path(doc['tmpSettingsPath'],'.asset')
            if not doc['tmpSettingsPath'].endswith('/Resources/TMP Settings.asset'):
                fail('UI_TMP_SETTINGS','spec','Explicit TMP Settings asset must be in a Resources folder')
        if any(type(doc.get(k)) is not int or not 64 <= doc[k] <= 8192 for k in ("width", "height")):
            fail("UI_RESOLUTION", "spec", "Explicit reference dimensions 64..8192 required")
        nodes = doc.get("nodes")
        if not isinstance(nodes, list) or not 1 <= len(nodes) <= 1000:
            fail("UI_NODES", "spec", "Expected 1..1000 ordered nodes")
        ids, paths = set(), set()
        for n in nodes:
            if set(n) - {"id", "name", "parent", "kind", "text", "x", "y", "width", "height", "fontSize", "color", "spriteGuid", "spriteLocalId"}:
                fail("UI_NODE_FIELD", "spec", "Unknown node field")
            if n.get("kind") not in ("panel", "list", "button", "text", "image") or not isinstance(n.get("id"), str) or not n['id'] or n['id'] in ids:
                fail("UI_NODE", "spec", "Unknown kind or duplicate/missing ID")
            if n.get("parent", "") not in ids | {""}:
                fail("UI_PARENT", n['id'], "Parent must precede child")
            if not isinstance(n.get("name"), str) or not n['name'] or any(c in n['name'] for c in ('/', '\\', '\t', '\n')):
                fail("UI_NAME", n['id'], "Expected simple nonempty node name")
            key=(n.get('parent',''), n['name'])
            if key in paths:
                fail("UI_NAME_COLLISION", n['id'], "Sibling names must be unique")
            paths.add(key)
            if any(type(n.get(k)) not in (int, float) or n[k] <= 0 for k in ("width", "height")):
                fail("UI_SIZE", n['id'], "Positive explicit node size required")
            for k in ('x','y','fontSize'):
                if k in n and type(n[k]) not in (int,float):
                    fail("UI_VALUE", n['id'], "Numeric layout value required")
            if 'color' in n and (not isinstance(n['color'],list) or len(n['color'])!=4 or any(type(x) not in (int,float) or not 0<=x<=1 for x in n['color'])):
                fail("UI_COLOR", n['id'], "Expected RGBA float array 0..1")
            if 'spriteGuid' in n:
                import re
                if not re.fullmatch('[a-f0-9]{32}',n['spriteGuid']) or type(n.get('spriteLocalId')) is not int:
                    fail("UI_SPRITE_ID",n['id'],"Explicit GUID and localID required")
            ids.add(n['id'])
    elif operation == "prefab.edit":
        allowed |= {"prefabPath", "edits"}
        asset_path(doc.get("prefabPath"), ".prefab")
        if not isinstance(doc.get("edits"), list) or not 1 <= len(doc["edits"]) <= 1000:
            fail("PREFAB_EDITS", "spec", "Expected explicit edits")
        for e in doc['edits']:
            if set(e)-{'nodePath','component','property','kind','value','targetNode','targetComponent','assetGuid','localId'} or e.get('kind') not in ('string','bool','int','float','object'):
                fail('PREFAB_EDIT_FIELD','spec','Unknown edit field or value kind')
            if not all(isinstance(e.get(k),str) and e[k] for k in ('component','property')) or not isinstance(e.get('nodePath',''),str):
                fail('PREFAB_EDIT_SHAPE','spec','Explicit component, node path and property required')
            if e['kind']!='object' and not isinstance(e.get('value'),str):
                fail('PREFAB_EDIT_VALUE','spec','Scalar edit value is an explicit invariant string')
            if e['kind']=='object' and not (e.get('assetGuid') or 'targetNode' in e):
                fail('PREFAB_EDIT_REFERENCE','spec','Explicit asset GUID/localID or prefab node reference required')
            if e.get('property','').split('.')[0] in ('m_Script','m_GameObject','m_Father','m_Children','m_CorrespondingSourceObject','m_PrefabInstance','m_PrefabAsset'):
                fail('PREFAB_IDENTITY_FIELD',e.get('property'),'Identity and hierarchy fields are not editable in this subset')
    elif operation == "build.macos":
        allowed |= {"scenes", "productName"}
        if not isinstance(doc.get("scenes"), list) or not doc["scenes"]:
            fail("BUILD_SCENES", "spec", "Explicit nonempty scene paths required")
        for p in doc["scenes"]:
            asset_path(p, ".unity")
        import re
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,63}", doc.get("productName", "")):
            fail("BUILD_PRODUCT", "spec", "Expected simple app artifact name")
    elif operation == "prefab.inspect":
        allowed |= {"prefabPath"}
        asset_path(doc.get('prefabPath'),'.prefab')
    elif operation == 'validate.play':
        allowed |= {'scenePath','durationSeconds'}
        asset_path(doc.get('scenePath'),'.unity')
        if type(doc.get('durationSeconds')) is not int or not 1<=doc['durationSeconds']<=30:
            fail('PLAY_DURATION','spec','Explicit duration 1..30 seconds required')
    elif operation == 'project.import':
        pass
    elif operation == 'sprite.configure':
        allowed |= {'assetPath','pixelsPerUnit','pivot','maxSize','alphaIsTransparency'}
        asset_path(doc.get('assetPath'),'.png')
        if type(doc.get('pixelsPerUnit')) not in (int,float) or not 1<=doc['pixelsPerUnit']<=4096:
            fail('SPRITE_PPU','spec','Explicit pixelsPerUnit 1..4096')
        if not isinstance(doc.get('pivot'),list) or len(doc['pivot'])!=2 or any(type(x) not in (int,float) or not 0<=x<=1 for x in doc['pivot']):
            fail('SPRITE_PIVOT','spec','Explicit normalized pivot')
        if doc.get('maxSize') not in (32,64,128,256,512,1024,2048,4096,8192) or type(doc.get('alphaIsTransparency'))is not bool:
            fail('SPRITE_IMPORT','spec','Explicit maxSize and alphaIsTransparency required')
    elif operation == 'assets.move':
        allowed |= {'moves'}
        if not isinstance(doc.get('moves'),list) or not 1<=len(doc['moves'])<=10000:
            fail('ASSET_MOVES','spec','Expected 1..10000 explicit asset moves')
        sources=set();targets=set()
        for move in doc['moves']:
            if set(move)!={'source','target'}:
                fail('ASSET_MOVE','spec','Expected source and target only')
            for key in ('source','target'):
                asset_path(move[key],Path(move[key]).suffix)
                if move[key].endswith('.meta'):
                    fail('ASSET_MOVE_META',move[key],'Move the native asset, never its meta separately')
            if move['source'] in sources or move['target'] in targets or Path(move['source']).suffix!=Path(move['target']).suffix:
                fail('ASSET_MOVE_AMBIGUOUS','spec','Duplicate endpoints or extension change')
            sources.add(move['source']);targets.add(move['target'])
        if sources & targets:
            fail('ASSET_MOVE_CYCLE','spec','Overlapping move endpoints require separate reviewed batches')
    else:
        fail("UNITY_OPERATION", str(operation), "Unsupported native operation")
    if set(doc) - allowed or doc.get("schemaVersion") != 1:
        fail("UNITY_SPEC", "spec", "Unknown fields or schema version")


def source_files(project):
    files = project_files(project, "unity-6000.6.2f1-ugui")
    for name in ("ProjectSettings", "Packages"):
        for p in sorted(inside(project, name).rglob("*")):
            if p.is_symlink():
                fail("SYMLINK_PATH", str(p), "Source settings contain symlink")
            if p.is_file():
                rel = p.relative_to(project).as_posix()
                files[rel] = dict(path=rel, sha256=digest_file(p), bytes=p.stat().st_size)
    return files


def plan(project, spec, output):
    project = safe(project)
    context = inspect(project)
    if (context['engine'], context['editorVersion']) != ('unity', VERSION):
        fail('UNITY_VERSION', str(project), 'Only existing Unity '+VERSION+' supported; no upgrades')
    raw = read(spec)
    doc = load(raw)
    validate_spec(doc)
    files = source_files(project)
    if doc['operation'] == 'ui.create':
        if doc['sourceFontPath'] not in files:
            fail('UI_FONT_MISSING',doc['sourceFontPath'],'Import an authorized font into the project first')
        if doc['sourceFontPath']+'.meta' not in files:
            fail('UI_FONT_NOT_IMPORTED',doc['sourceFontPath'],'Use project.import before creating a native UI from this font')
        for k in ('prefabPath','scenePath','fontAssetPath','tmpSettingsPath'):
            if k not in doc:continue
            if doc[k] in files or doc[k]+'.meta' in files:
                fail('UNITY_OUTPUT_EXISTS',doc[k],'Create-only output already exists')
    elif doc['operation']=='assets.move':
        for move in doc['moves']:
            if move['source'] not in files or move['source']+'.meta' not in files:
                fail('ASSET_MOVE_SOURCE',move['source'],'Expected imported file with meta; folder moves unsupported')
            if inside(project,move['target']).exists() or inside(project,move['target']+'.meta').exists():
                fail('ASSET_MOVE_DESTINATION',move['target'],'Destination occupied')
    elif doc['operation']=='project.import':
        pass
    elif doc['operation']=='sprite.configure':
        if doc['assetPath'] not in files:
            fail('SPRITE_MISSING',doc['assetPath'],'Source PNG missing')
    else:
        for rel in (doc.get('scenes') or [doc.get('prefabPath') or doc['scenePath']]):
            if rel not in files:
                fail('UNITY_INPUT_MISSING',rel,'Native source asset is missing')
    out = new_output(output,[project,safe(spec)])
    data = dict(contract='beaver-unity/1',schemaVersion=1,implementationSha256=identity(),
                projectRoot=str(project),engine='unity',editorVersion=VERSION,
                writableRoots=['Assets'] if doc['operation'] != 'build.macos' else [],
                before=files,spec=doc,specSha256=sha(raw),
                sourceSha256=sha(encoded(files)),native='not_run')
    data['editorInitializationFiles']=['ProjectSettings/SceneTemplateSettings.json'] if doc['operation']=='ui.create' and 'ProjectSettings/SceneTemplateSettings.json' not in files else []
    data['nativeInitializationPaths']=[]
    if doc['operation']=='project.import':
        for p in inside(project,'Assets').rglob('*'):
            if p.is_symlink():fail('SYMLINK_PATH',str(p),'Native import input contains a symbolic link')
            rel=p.relative_to(project).as_posix()
            if not rel.endswith('.meta') and not inside(project,rel+'.meta').exists():data['nativeInitializationPaths'].append(rel+'.meta')
    write_json(out/'unity-plan.json',data)
    digest=sha(encoded(data))
    write_json(out/'plan-identity.json',dict(sha256=digest))
    return dict(status='planned',report=str(out/'unity-plan.json'),planSha256=digest,sourceSha256=data['sourceSha256'])


def editor_path(path):
    from .native_environment import editor_environment
    editor_environment('BEAVER_NATIVE_JOB', path)
    path = safe(path)
    if path.name not in ('Unity','Unity.exe') or not path.is_file() or not os.access(path,os.X_OK):
        fail('UNITY_EDITOR',str(path),'Expected executable Unity Editor')
    if path.parent.name == 'MacOS':
        metadata=path.parent.parent/'Info.plist'
        if plistlib.loads(read(metadata)).get('CFBundleVersion') != VERSION:
            fail('UNITY_EDITOR_VERSION',str(path),'Wrong Editor version')
    return path


def run(editor, project, stage, mode, plan, timeout):
    from .native_environment import editor_environment
    if type(timeout) is not int or not 1 <= timeout <= 1200:
        fail('UNITY_TIMEOUT',str(timeout),'Expected 1..1200 seconds')
    job=dict(plan['spec'],targetRoot=str(project),stage=str(stage),mode=mode,
             planSha256=sha(encoded(plan)),sourceSha256=plan['sourceSha256'])
    jobfile=stage/('job-'+mode+'.json')
    write_json(jobfile,job)
    args=[str(editor),'-batchmode','-nographics','-projectPath',str(project),'-executeMethod','BeaverNative.Run','-logFile',str(stage/(mode+'.log'))]
    with (stage/(mode+'-console.log')).open('w') as log:
        process=subprocess.Popen(args,cwd=project,env=editor_environment('BEAVER_NATIVE_JOB',jobfile),stdout=log,stderr=subprocess.STDOUT)
        write_json(stage/'process.json',dict(pid=process.pid,mode=mode,owned=True,exited=False))
        try:
            code=process.wait(timeout=timeout)
        except (subprocess.TimeoutExpired,KeyboardInterrupt):
            process.terminate()
            try:process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                process.kill();process.wait(timeout=10)
            write_json(stage/'process.json',dict(pid=process.pid,mode=mode,owned=True,exited=True,exitCode=process.returncode))
            fail('UNITY_TIMEOUT',str(stage),'Owned Editor terminated; inspect journal before recovery')
    write_json(stage/'process.json',dict(pid=process.pid,mode=mode,owned=True,exited=True,exitCode=code))
    path=stage/('native-'+mode+'.json')
    report=load(read(path)) if path.exists() else {}
    if code != 0 or report.get('status') != 'verified' or report.get('planSha256') != job['planSha256']:
        fail('UNITY_NATIVE_FAILED',str(stage),'Inspect native report and logs; no success inferred from process exit')
    return report


def apply(plan_path, output, editor, expected, timeout=600):
    path=safe(plan_path)
    raw=read(path,max_bytes=128*1024**2)
    data=load(raw,max_nodes=8_000_000)
    if sha(raw)!=expected or load(read(path.parent/'plan-identity.json')).get('sha256')!=expected:
        fail('UNITY_PLAN_CHANGED',str(path),'Expected exact plan identity')
    if data.get('contract')!='beaver-unity/1' or data.get('implementationSha256')!=identity():
        fail('UNITY_PLAN_IMPLEMENTATION',str(path),'Generate plan with the current implementation')
    validate_spec(data['spec'])
    project=safe(data['projectRoot']); editor=editor_path(editor)
    stage=new_output(output,[project,path.parent])
    write_json(stage/'unity-plan.json',data)
    journal=dict(status='prepared',planSha256=expected,projectRoot=str(project),operation=data['spec']['operation'],startedAt=datetime.now(timezone.utc).isoformat())
    write_json(stage/'journal.json',journal)
    with lock(project):
        native_lock(project)
        if source_files(project)!=data['before']:
            fail('UNITY_SOURCE_DRIFT',str(project),'Project changed after plan')
        helper=inside(project,HELPER)
        if inside(project,HELPER_ROOT).exists() or inside(project,HELPER_ROOT+'.meta').exists():
            fail('UNITY_HELPER_CONFLICT',HELPER_ROOT,'Reserved helper root occupied')
        for key in ('prefabPath',):
            rel=data['spec'].get(key)
            if rel and rel in data['before']:
                for suffix in ('','.meta'):
                    source=inside(project,rel+suffix)
                    if source.exists():
                        dest=stage/'before'/(rel+suffix);dest.parent.mkdir(parents=True,exist_ok=True);dest.write_bytes(read(source,max_bytes=512*1024**2))
        source=Path(__file__).parent/'targets/BeaverNative.cs'
        helper.parent.mkdir(parents=True);helper.write_bytes(source.read_bytes())
        fingerprint=digest_file(helper)
        try:
            applied=run(editor,project,stage,'apply',data,timeout)
            reopened=run(editor,project,stage,'verify',data,timeout) if data['spec']['operation'] not in ('build.macos','validate.play') else applied
            journal.update(status='native-complete-pending-source-audit',nativeReadback=reopened)
        except BaseException:
            journal['status']='recovery-required'
            raise
        finally:
            if helper.exists() and digest_file(helper)==fingerprint:
                directory=helper.parent.parent
                allowed={HELPER,HELPER+'.meta',HELPER_ROOT+'/Editor.meta'}
                owned=all(not p.is_symlink() and (not p.is_file() or p.relative_to(project).as_posix() in allowed) for p in directory.rglob('*'))
                if owned:
                    for p in sorted(directory.rglob('*'),key=lambda p:len(p.parts),reverse=True):
                        p.unlink() if p.is_file() else p.rmdir()
                    directory.rmdir()
                    meta=inside(project,HELPER_ROOT+'.meta')
                    if meta.exists():meta.unlink()
            write_json(stage/'journal.json',journal)
        after=source_files(project)
        changes=[dict(path=p,before=data['before'].get(p),after=after.get(p)) for p in sorted(set(data['before'])|set(after)) if data['before'].get(p)!=after.get(p)]
        journal.update(after=after,changes=changes,sourceSha256After=sha(encoded(after)))
        write_json(stage/'journal.json',journal)
        def audit_fail(code,location,message):
            journal.update(status='recovery-required',diagnostic=dict(code=code,location=location,message=message))
            write_json(stage/'journal.json',journal)
            fail(code,str(stage),message+' ('+location+')')
        if data['spec']['operation']=='build.macos' and changes:
            audit_fail('BUILD_SOURCE_DRIFT',str(project),'Build changed source; artifact is not certified')
        if data['spec']['operation']=='ui.create':
            allowed={data['spec'][k]+suffix for k in ('prefabPath','scenePath','fontAssetPath','tmpSettingsPath') if k in data['spec'] for suffix in ('','.meta')}
            for path_value in list(allowed):
                for parent in Path(path_value).parents:
                    if parent.as_posix() not in ('.','Assets'):allowed.add(parent.as_posix()+'.meta')
            for row in changes:
                if row['before'] is None and row['path'] in data.get('editorInitializationFiles',[]):continue
                if row['before'] is not None or row['path'] not in allowed:
                    audit_fail('UNITY_UNEXPECTED_CHANGE',row['path'],'Unexpected source mutation; inspect transaction')
        if data['spec']['operation']=='prefab.edit':
            allowed={data['spec']['prefabPath'],data['spec']['prefabPath']+'.meta'}
            if any(row['path'] not in allowed for row in changes):
                audit_fail('UNITY_UNEXPECTED_CHANGE',str(project),'Prefab edit changed another source file')
        if data['spec']['operation']=='prefab.inspect' and changes:
            audit_fail('UNITY_UNEXPECTED_CHANGE',str(project),'Inspection changed a source file')
        if data['spec']['operation']=='validate.play' and changes:
            audit_fail('UNITY_UNEXPECTED_CHANGE',str(project),'Play probe changed a source file')
        if data['spec']['operation']=='project.import':
            allowed=set(data['nativeInitializationPaths'])
            if any(row['before'] is not None or row['path'] not in allowed for row in changes):
                audit_fail('UNITY_UNEXPECTED_CHANGE',str(project),'Native import changed existing source; inspect the transaction')
        if data['spec']['operation']=='sprite.configure':
            if any(row['path']!=data['spec']['assetPath']+'.meta' for row in changes):
                audit_fail('UNITY_UNEXPECTED_CHANGE',str(project),'Sprite configuration changed other source')
        if data['spec']['operation']=='assets.move':
            allowed={move[k]+suffix for move in data['spec']['moves'] for k in ('source','target') for suffix in ('','.meta')}
            for move in data['spec']['moves']:
                for parent in Path(move['target']).parents:
                    if parent.as_posix() not in ('.','Assets'):allowed.add(parent.as_posix()+'.meta')
            for row in changes:
                if row['path'] not in allowed:
                    audit_fail('UNITY_UNEXPECTED_CHANGE',row['path'],'Move changed another source file')
            if applied.get('moves')!=reopened.get('moves'):
                audit_fail('UNITY_MOVE_READBACK',str(stage),'Move GUID/localIDs differ after reopen')
        if data['spec']['operation']=='build.macos':
            try:
                artifacts=[]
                for p in sorted((stage/'player').rglob('*')):
                    if p.is_symlink():artifacts.append(dict(path=p.relative_to(stage).as_posix(),symlink=os.readlink(p)))
                    elif p.is_file():artifacts.append(dict(path=p.relative_to(stage).as_posix(),sha256=digest_file(p,max_bytes=BUILD_FILE_BYTES),bytes=p.stat().st_size))
                if not artifacts:fail('BUILD_EMPTY',str(stage),'No actual build artifact')
                write_json(stage/'artifact-manifest.json',dict(schemaVersion=1,artifactPolicy='beaver-unity-build/2',sourceSha256=data['sourceSha256'],planSha256=expected,files=artifacts))
                from .payload import verify_manifest
                journal['artifactVerification']=verify_manifest(stage,stage/'artifact-manifest.json',scope='player',allow_links=True,artifact=True)
            except (BeaverError,OSError,ValueError) as error:
                journal.update(status='artifact-verification-failed',artifactError=str(error))
                write_json(stage/'journal.json',journal)
                fail('BUILD_ARTIFACT_FAILED',str(stage),str(error))
        journal['status']='complete'
        write_json(stage/'journal.json',journal)
    return dict(status='verified',report=str(stage/'journal.json'),transaction=str(stage),changes=changes,
                nativeReadback=journal['nativeReadback'],sourceSha256=data['sourceSha256'])
