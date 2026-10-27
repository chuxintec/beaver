"""Scoped native batches. Preconditions are content hashes, caches never authority."""
from pathlib import Path
from datetime import datetime, timezone
import os
import subprocess
import time
from .io import safe, inside, read, load, sha, encoded, write_json, new_output
from .errors import fail, BeaverError
from .output_limits import digest_file
from .transaction_fs import lock
from .context import inspect
from .unity import VERSION, identity, asset_path, editor_path
from .adoption import native_lock

HELPER_ROOT = 'Assets/BeaverBatchTools'
KINDS = {'node.add','node.remove','node.reparent','node.order','node.active','component.add','component.remove',
         'property.set','binding.reference','binding.event','ui.rect','ui.scroll','ui.grid','ui.mask','ui.text','ui.event','ui.list','ui.safeArea'}
FIELDS = {'kind','path','parent','name','component','property','value','targetNode','targetComponent',
          'assetGuid','localId','method','eventName','mode','index','flag','numbers','items'}


def inventory(root):
    """Cheap mutation discovery, never a content precondition or cache certificate."""
    result = {}
    for name in ('Assets','Packages','ProjectSettings'):
        for base, dirs, files in os.walk(inside(root,name), followlinks=False):
            for child in dirs + files:
                p=Path(base)/child
                if p.is_symlink():fail('BATCH_SYMLINK',str(p),'Source symlinks unsupported')
            for name in files:
                p=Path(base)/name;s=p.stat()
                result[p.relative_to(root).as_posix()]=[s.st_size,s.st_mtime_ns,s.st_ctime_ns,s.st_ino]
    return result


def state(root, paths):
    out={}
    for rel in sorted(paths):
        p=inside(root,rel)
        if p.exists() and not p.is_file():fail('BATCH_FILE_REQUIRED',rel,'Expected file or unoccupied file destination')
        out[rel]=dict(sha256=digest_file(p),bytes=p.stat().st_size) if p.exists() else None
    return out


def validate(spec):
    if isinstance(spec,dict) and spec.get('schemaVersion')==3:
        from .production import validate as production_validate
        return production_validate(spec)
    if not isinstance(spec,dict) or set(spec)-{'schemaVersion','operation','steps','readPaths','runtimeRoot'} or spec.get('schemaVersion')!=2 or spec.get('operation')!='batch':
        fail('BATCH_SPEC','spec','Expected schemaVersion 2, operation batch and explicit steps')
    steps=spec.get('steps')
    if not isinstance(steps,list) or not 1<=len(steps)<=256:fail('BATCH_STEPS','spec','Expected 1..256 ordered steps')
    for step in steps:
        if not isinstance(step,dict):fail('BATCH_STEP','spec','Each step must be an object')
        kind=step.get('kind')
        if kind=='prefab.inspect':
            if set(step)!={'kind','assetPath'}:fail('BATCH_INSPECT','spec','Expected kind and assetPath only')
            asset_path(step['assetPath'],'.prefab')
        elif kind=='prefab.patch':
            if set(step)!={'kind','assetPath','actions'}:fail('BATCH_STEP',str(kind),'Expected kind, assetPath, actions')
            asset_path(step['assetPath'],'.prefab')
            if not isinstance(step['actions'],list) or not 1<=len(step['actions'])<=1000:fail('BATCH_ACTIONS',step['assetPath'],'Expected 1..1000 explicit actions')
            for action in step['actions']:
                if not isinstance(action,dict) or set(action)-FIELDS or action.get('kind') not in KINDS:fail('BATCH_ACTION',str(action),'Unknown action/field')
                for field in FIELDS-{'localId','index','flag','numbers','items'}:
                    if field in action and not isinstance(action[field],str):fail('BATCH_ACTION_TYPE',field,'Expected string')
                for field in ('path','parent','targetNode'):
                    value=action.get(field,'')
                    if value and (value.startswith('/') or '\\' in value or any(p in ('','..','.') for p in value.split('/'))):fail('BATCH_NODE_PATH',value,'Expected canonical relative hierarchy path')
                if 'index' in action and (type(action['index'])is not int or action['index']<0):fail('BATCH_INDEX','index','Nonnegative integer required')
                if 'localId' in action and type(action['localId'])is not int:fail('BATCH_LOCAL_ID','localId','Integer required')
                if 'flag' in action and type(action['flag'])is not bool:fail('BATCH_FLAG','flag','Boolean required')
                if action['kind']=='node.active' and 'flag' not in action:fail('BATCH_FLAG','flag','node.active requires explicit flag')
                if 'numbers' in action and (not isinstance(action['numbers'],list) or any(type(n) not in (float,int) for n in action['numbers'])):fail('BATCH_NUMBERS','numbers','Finite numeric array required')
                if 'items' in action and (not isinstance(action['items'],list) or len(action['items'])>10000 or any(not isinstance(n,str) for n in action['items'])):fail('BATCH_ITEMS','items','Bounded string list required')
                if action['kind'] in ('ui.list','ui.safeArea') and not spec.get('runtimeRoot'):fail('BATCH_RUNTIME_ROOT',action['kind'],'Explicit product runtimeRoot required for persistent generic component')
        elif kind=='asset.move':
            if set(step)!={'kind','source','target'}:fail('BATCH_MOVE','spec','Expected source and target')
            for k in ('source','target'):asset_path(step[k],Path(step[k]).suffix)
            if step['source'].endswith('.meta') or Path(step['source']).suffix!=Path(step['target']).suffix:fail('BATCH_MOVE_KIND','spec','Move imported file, same extension, not meta')
        elif kind=='reference.replace':
            if set(step)!={'kind','assetPath','oldGuid','oldLocalId','newGuid','newLocalId','consumers'}:fail('BATCH_REPLACE','spec','Explicit old/new GUID/localID and complete native consumer set required')
            for k in ('oldGuid','newGuid'):
                import re
                if not re.fullmatch('[a-f0-9]{32}',step[k]):fail('BATCH_GUID',k,'Expected GUID')
            if any(type(step[k])is not int for k in ('oldLocalId','newLocalId')):fail('BATCH_LOCAL_ID','spec','Explicit integer localIDs')
            if not isinstance(step['consumers'],list) or not step['consumers']:fail('BATCH_CONSUMERS','spec','Explicit consumers required')
            for p in step['consumers']:
                asset_path(p,Path(p).suffix)
                if Path(p).suffix not in ('.prefab','.unity','.asset'):fail('BATCH_CONSUMER_TYPE',p,'Native serialized prefab/scene/asset only')
            asset_path(step['assetPath'],Path(step['assetPath']).suffix)
        elif kind=='text.key':
            if set(step)!={'kind','assetPath','format','pointer','before','after'} or step['format']!='json-pointer':fail('BATCH_STRING_KEY','spec','Only explicitly approved JSON pointer string keys; no global script regex')
            asset_path(step['assetPath'],'.json')
            if not all(isinstance(step[k],str) for k in ('pointer','before','after')):fail('BATCH_STRING_KEY','spec','String pointer/before/after required')
        elif kind=='script.key':
            if set(step)!={'kind','assetPath','patches'}:fail('BATCH_SCRIPT_KEY','spec','Expected script assetPath and proven literal span patches')
            asset_path(step['assetPath'],'.cs')
            if not isinstance(step['patches'],list) or not step['patches']:fail('BATCH_SCRIPT_KEY','spec','Nonempty explicit patch array required')
        else:fail('BATCH_STEP_KIND',str(kind),'Unsupported batch step')
    if 'runtimeRoot' in spec:
        asset_path(spec['runtimeRoot']+'/BeaverListView.cs','.cs')
        if 'Editor' in spec['runtimeRoot'].split('/'):fail('BATCH_RUNTIME_EDITOR','runtimeRoot','Runtime components cannot be installed in Editor directory')
    for p in spec.get('readPaths',[]):asset_path(p,Path(p).suffix)


def dependencies(project, roots, explicit_guids=()):
    """Selected GUID closure. Meta discovery reads small identity records only."""
    import re
    guids={};meta_count=0
    for p in inside(project,'Assets').rglob('*.meta'):
        raw=read(p,max_bytes=8*1024**2);meta_count+=1
        found=re.search(rb'^guid: ([a-f0-9]{32})$',raw,re.M)
        if found:
            guid=found[1].decode();rel=p.relative_to(project).as_posix()[:-5]
            if guid in guids:fail('BATCH_DUPLICATE_GUID',guid,'Ambiguous native asset identity')
            guids[guid]=rel
    for guid in explicit_guids:
        if guid not in guids:fail('BATCH_GUID_MISSING',guid,'Explicit reference not in imported Assets identity index')
    pending=list(roots)+[guids[g] for g in explicit_guids];seen=set();bytes_read=0
    while pending:
        rel=pending.pop()
        if rel in seen:continue
        seen.add(rel);p=inside(project,rel)
        if not p.is_file():continue
        if p.suffix.lower() in ('.prefab','.unity','.asset','.mat','.controller','.anim','.overridecontroller'):
            raw=read(p,max_bytes=32*1024**2);bytes_read+=len(raw)
            if raw.startswith(b'%YAML'):
                for guid in set(re.findall(rb'guid: ([a-f0-9]{32})',raw)):
                    dep=guids.get(guid.decode())
                    if dep and inside(project,dep).is_file():pending.append(dep)
        if p.suffix=='.cs':
            # C# semantic dependencies cannot be inferred from using directives.
            # Hash all project scripts/asmdefs, not all textures/audio.
            pass
    # Any script may affect compilation or importer behaviour for this Editor run.
    for pattern in ('*.cs','*.asmdef','*.asmref','*.rsp'):
        seen.update(p.relative_to(project).as_posix() for p in inside(project,'Assets').rglob(pattern))
    seen.update(p.relative_to(project).as_posix() for name in ('Packages','ProjectSettings') for p in inside(project,name).rglob('*') if p.is_file())
    seen|={p+'.meta' for p in tuple(seen) if p.startswith('Assets/') and inside(project,p+'.meta').is_file()}
    return seen,dict(metaIdentityReads=meta_count,dependencyTextBytes=bytes_read)


def plan(project,spec_path,output):
    started=time.monotonic();project=safe(project);context=inspect(project)
    if (context['engine'],context['editorVersion'])!=('unity',VERSION):fail('UNITY_VERSION',str(project),'Only existing Unity '+VERSION)
    spec=load(read(spec_path));validate(spec);listing=inventory(project);writes=set();roots=set(spec.get('readPaths',[]));explicit_guids=set()
    production=spec.get('schemaVersion')==3
    if production:
        from .production import paths,binding,verify_sources
        extra_reads,extra_writes=paths(project,spec);roots.update(extra_reads);writes.update(extra_writes);verify_sources(project,spec)
    for step in spec['steps']:
        kind=step['kind']
        if kind.startswith('resource.'):continue
        paths=[step['assetPath']] if kind in ('prefab.inspect','prefab.patch','text.key','script.key') else step['consumers'] if kind=='reference.replace' else [step['source'],step['target']]
        if kind!='prefab.inspect':writes.update(p+s for p in paths for s in ('','.meta'))
        roots.update(paths)
        if kind=='prefab.patch':explicit_guids.update(a['assetGuid'] for a in step['actions'] if a.get('assetGuid'))
        if kind=='reference.replace':explicit_guids.update((step['oldGuid'],step['newGuid']))
        if kind=='asset.move':
            if step['source'] not in listing or step['source']+'.meta' not in listing:fail('BATCH_MOVE_SOURCE',step['source'],'Imported file required')
            if step['target'] in listing or step['target']+'.meta' in listing:fail('BATCH_MOVE_TARGET',step['target'],'Destination occupied')
        elif any(p not in listing for p in paths):fail('BATCH_INPUT','spec','Existing native consumer/input required')
        if kind=='reference.replace':roots.add(step['assetPath'])
    installs=[]
    if spec.get('runtimeRoot'):
        for name in ('BeaverListView.cs','BeaverSafeArea.cs'):
            rel=spec['runtimeRoot']+'/'+name;source=Path(__file__).parent/'targets'/name
            if not source.is_file():fail('BATCH_RUNTIME_PROVIDER',name,'Runtime template not available')
            fingerprint=digest_file(source)
            if inside(project,rel).exists() and digest_file(inside(project,rel))!=fingerprint:fail('BATCH_RUNTIME_CONFLICT',rel,'Existing runtime component differs; migrate explicitly')
            if not inside(project,rel).exists():installs.append(dict(path=rel,sha256=fingerprint))
            roots.add(rel);writes.update((rel,rel+'.meta'))
    for p in list(writes):
        if not p.startswith('Assets/'):continue
        for parent in Path(p).parents:
            if parent.as_posix() not in ('.','Assets'):
                meta=parent.as_posix()+'.meta'
                if meta not in listing:writes.add(meta)
                else:roots.add(meta)
    profile=binding(project,spec,writes=writes) if production else None
    reads,metrics=dependencies(project,roots,explicit_guids);before=state(project,reads|writes)
    if any(p==HELPER_ROOT or p.startswith(HELPER_ROOT+'/') or p==HELPER_ROOT+'.meta' for p in before):fail('BATCH_RESERVED_PATH',HELPER_ROOT,'Temporary helper root reserved')
    out=new_output(output,[project,safe(spec_path)])
    result=dict(contract='beaver-production/3' if production else 'beaver-batch/2',schemaVersion=3 if production else 2,implementationSha256=identity(),projectRoot=str(project),
                spec=spec,readSet=sorted(reads),writeSet=sorted(writes),before=before,installs=installs,
                metrics=dict(metrics,statFiles=len(listing),hashedFiles=sum(v is not None for v in before.values()),hashedBytes=sum(v['bytes'] for v in before.values() if v),editorStarts=0,seconds=time.monotonic()-started),
                limitations=['Selected GUID closure and compilation inputs are hashed; unrelated asset bytes are not scanned',
                             'Native dependency preflight rejects unlisted consumers before replacement','Dynamic loading keys require explicit consumer patches; no completeness claim'])
    if profile:result['projectBinding']=profile
    write_json(out/'batch-plan.json',result);digest=sha(encoded(result));write_json(out/'plan-identity.json',dict(sha256=digest))
    return dict(status='planned',report=str(out/'batch-plan.json'),planSha256=digest,metrics=result['metrics'])


def check_plan(path,expected):
    raw=read(path,max_bytes=128*1024**2);data=load(raw,max_nodes=8_000_000)
    if sha(raw)!=expected or data.get('contract') not in ('beaver-batch/2','beaver-production/3') or data.get('implementationSha256')!=identity():fail('BATCH_PLAN_CHANGED',str(path),'Regenerate with current implementation; bind exact plan SHA256')
    validate(data['spec']);return data


def launch(editor,project,stage,job,timeout):
    if type(timeout)is not int or not 1<=timeout<=1200:fail('BATCH_TIMEOUT_BUDGET','timeout','1..1200 seconds required')
    write_json(stage/'native-job.json',job)
    args=[str(editor),'-batchmode','-nographics','-projectPath',str(project),'-executeMethod','BeaverBatch.Run','-logFile',str(stage/'editor.log')]
    from .native_environment import editor_environment
    env=editor_environment('BEAVER_BATCH_JOB',stage/'native-job.json')
    started=time.monotonic()
    with (stage/'console.log').open('w') as stream:
        proc=subprocess.Popen(args,cwd=project,env=env,stdout=stream,stderr=subprocess.STDOUT)
        process_record=dict(pid=proc.pid,owned=True,executable=str(editor),launchArguments=args,startedUtc=datetime.now(timezone.utc).isoformat())
        write_json(stage/'process.json',dict(process_record,exited=False))
        try:code=proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.terminate()
            try:proc.wait(timeout=15)
            except subprocess.TimeoutExpired:proc.kill();proc.wait(timeout=10)
            fail('BATCH_TIMEOUT',str(stage),'Owned Editor stopped at budget; inspect native checkpoint before recovery')
        except BaseException:
            proc.terminate()
            try:proc.wait(timeout=15)
            except subprocess.TimeoutExpired:proc.kill();proc.wait(timeout=10)
            raise
        finally:write_json(stage/'process.json',dict(process_record,exited=True,exitCode=proc.returncode,exitedUtc=datetime.now(timezone.utc).isoformat(),seconds=time.monotonic()-started))
    report=load(read(stage/'native-result.json')) if (stage/'native-result.json').exists() else {}
    return code,report


def helper(project, production=False, sprites=False):
    root=inside(project,HELPER_ROOT)
    if root.exists() or inside(project,HELPER_ROOT+'.meta').exists():fail('BATCH_HELPER_CONFLICT',HELPER_ROOT,'Temporary helper root occupied')
    directory=root/'Editor';directory.mkdir(parents=True);owned={}
    names=['BeaverBatch.cs','BeaverBatchUI.cs','BeaverWiring.cs']
    if production and (Path(__file__).parent/'targets/BeaverResourceProduction.cs').is_file():names.append('BeaverResourceProduction.cs')
    if sprites:names.append('BeaverResourceSprites.cs')
    for name in names:
        target=directory/name;target.write_bytes((Path(__file__).parent/'targets'/name).read_bytes());owned[target]=digest_file(target)
    return root,owned


def cleanup(project,root,owned):
    allowed={p for p in owned}|{Path(str(p)+'.meta') for p in owned}|{root/'Editor.meta'}
    if any(not p.exists() or digest_file(p)!=digest for p,digest in owned.items()):return False
    if any(p.is_symlink() or p.is_file() and p not in allowed for p in root.rglob('*')):return False
    for p in sorted(root.rglob('*'),key=lambda p:len(p.parts),reverse=True):p.unlink() if p.is_file() else p.rmdir()
    root.rmdir();meta=Path(str(root)+'.meta')
    if meta.exists():meta.unlink()
    return True


def text_changes(project,spec):
    """Exact JSON pointer transforms; product data values are never guessed."""
    grouped={}
    for step in spec['steps']:
        if step['kind']!='text.key':continue
        rel=step['assetPath'];doc=grouped.setdefault(rel,load(read(inside(project,rel))))
        pointer=step['pointer']
        if not pointer.startswith('/') or '~' in pointer.replace('~0','').replace('~1',''):fail('BATCH_JSON_POINTER',pointer,'Canonical non-root JSON pointer required')
        parts=[p.replace('~1','/').replace('~0','~') for p in pointer[1:].split('/')];current=doc
        for p in parts[:-1]:current=current[int(p)] if isinstance(current,list) else current[p]
        key=int(parts[-1]) if isinstance(current,list) else parts[-1]
        if current[key]!=step['before'] or not isinstance(current[key],str):fail('BATCH_JSON_KEY_DRIFT',rel+pointer,'Exact approved old string not present')
        current[key]=step['after']
    result={p:encoded(doc) for p,doc in grouped.items()}
    from .loading_keys import patch_script
    for step in spec['steps']:
        if step['kind']=='script.key':
            rel=step['assetPath']
            if rel in result:fail('BATCH_SCRIPT_DUPLICATE',rel,'One script.key step per file; provide all spans in one patches array')
            result[rel]=patch_script(read(inside(project,rel)),step['patches'])
    return result


def apply(plan_path,output,editor,expected,timeout=600):
    started=time.monotonic()
    path=safe(plan_path);data=check_plan(path,expected);project=safe(data['projectRoot']);editor=editor_path(editor)
    stage=new_output(output,[project,path.parent]);write_json(stage/'batch-plan.json',data)
    production=data.get('schemaVersion')==3
    if production:
        from .production import binding,verify_sources
        if binding(project,data['spec'],data['writeSet'])!=data['projectBinding']:fail('PRODUCTION_BINDING_DRIFT',str(project),'Project profile changed after plan')
        verify_sources(project,data['spec'])
    journal=dict(contract='beaver-production-transaction/3' if production else 'beaver-batch-transaction/2',status='prepared',projectRoot=str(project),planSha256=expected,
                 implementationSha256=identity(),writeSet=data['writeSet'],before={p:data['before'][p] for p in data['writeSet']},editorStarts=0)
    write_json(stage/'journal.json',journal)
    with lock(project):
        native_lock(project)
        if production:
            if binding(project,data['spec'],data['writeSet'])!=data['projectBinding']:fail('PRODUCTION_BINDING_DRIFT',str(project),'Project profile changed before write lock')
            verify_sources(project,data['spec'])
        if state(project,data['before'])!=data['before']:fail('BATCH_SOURCE_DRIFT',str(project),'Read/write content changed since plan')
        transforms=text_changes(project,data['spec']);listing=inventory(project)
        file_edits={edit['path']:read(edit['source'],max_bytes=512*1024**2) for edit in data['spec'].get('fileEdits',[])}
        for edit in data['spec'].get('fileEdits',[]):
            if sha(file_edits[edit['path']])!=edit['sha256']:fail('PRODUCTION_SOURCE_DRIFT',edit['source'],'Generated bytes changed before staging')
        for rel in data['writeSet']:
            if data['before'][rel] is not None:
                dest=stage/'before'/rel;dest.parent.mkdir(parents=True,exist_ok=True);dest.write_bytes(read(inside(project,rel),max_bytes=512*1024**2))
        patches=[]
        for rel,raw in transforms.items():
            dest=stage/'patches'/rel;dest.parent.mkdir(parents=True,exist_ok=True);dest.write_bytes(raw);patches.append(dict(path=rel,source=str(dest),sha256=sha(raw)))
        root,owned=helper(project,production and any(s['kind'].startswith('resource.') for s in data['spec']['steps']),production and any(s['kind']=='resource.sprites' for s in data['spec']['steps']))
        try:
            # Product runtime script imports must compile before the Editor companion.
            for install in data['installs']:
                source=Path(__file__).parent/'targets'/Path(install['path']).name
                dest=inside(project,install['path']);dest.parent.mkdir(parents=True,exist_ok=True);dest.write_bytes(source.read_bytes())
            preinstalled={rel:raw for rel,raw in transforms.items() if rel.endswith('.cs')}
            preinstalled.update(file_edits)
            for rel,raw in preinstalled.items():
                dest=inside(project,rel);dest.parent.mkdir(parents=True,exist_ok=True);dest.write_bytes(raw)
            installed={i['path']+s for i in data['installs'] for s in ('','.meta')}
            preconditions=[dict(path=p,exists=p in preinstalled or v is not None,sha256=sha(preinstalled[p]) if p in preinstalled else v['sha256'] if v else '') for p,v in data['before'].items() if p not in installed and (v is not None or p in data['writeSet'] and not p.endswith('.meta'))]
            job=dict(data['spec'],projectRoot=str(project),stage=str(stage),mode='apply',planSha256=expected,
                     writes=data['writeSet'],installs=data['installs'],patches=patches,preconditions=preconditions)
            job['requiresReopen']=any(s['kind']=='resource.project-settings' for s in data['spec']['steps'])
            if production:
                import json
                from .resources import validate_step
                job['steps']=[dict(kind=s['kind'],payload=json.dumps(validate_step(s),ensure_ascii=False,allow_nan=False)) if s['kind'].startswith('resource.') else s for s in job['steps']]
            journal['editorStarts']=1;write_json(stage/'journal.json',journal)
            code,report=launch(editor,project,stage,job,timeout)
            journal['native']=report
            complete_checkpoint={r['path'] for r in report.get('files',[])}==set(data['writeSet'])
            if job['requiresReopen'] and report.get('planSha256')==expected and complete_checkpoint and (code==0 and report.get('status')=='pending_editor_reopen' or report.get('status')=='failed'):
                journal['nativeBeforeReopen']=report
                # Unity flushes some ProjectSettings only while exiting. A new
                # process must read the persisted settings and all resource
                # assertions; the first process is deliberately not a verdict.
                saved=state(project,data['writeSet'])
                unchanged={p:v for p,v in data['before'].items() if p not in data['writeSet']}
                if state(project,unchanged)!=unchanged:fail('BATCH_REOPEN_INPUT_DRIFT',str(project),'Read inputs changed before independent native verification')
                reopen=stage/'native-reopen';reopen.mkdir()
                recheck=dict(job,stage=str(reopen),mode='verify' if code==0 and report.get('status')=='pending_editor_reopen' else 'checkpoint',requiresReopen=False,
                             preconditions=[dict(path=p,exists=v is not None,sha256=v['sha256'] if v else '') for p,v in {**unchanged,**saved}.items()])
                journal['editorStarts']=2;write_json(stage/'journal.json',journal)
                code,report=launch(editor,project,reopen,recheck,timeout)
                journal['native']=report
        except BaseException as error:
            journal.update(status='recovery-required',error=str(error));raise
        finally:
            journal['helperRemoved']=cleanup(project,root,owned)
            observed=inventory(project);changed={p for p in set(listing)|set(observed) if listing.get(p)!=observed.get(p)}
            journal['after']=state(project,data['writeSet']);journal['changedPaths']=sorted(changed)
            journal['unplannedPaths']=sorted(changed-set(data['writeSet']))
            journal['metrics']=dict(statFilesPerInventory=len(observed),inventoryPasses=2,
                                    pythonHashReadOperations=sum(v is not None for v in data['before'].values())+sum(v is not None for v in journal['after'].values()),
                                    nativeHashReadOperations=journal.get('native',{}).get('hashReadOperations','not_recorded_by_executor'),
                                    writeSetPaths=len(data['writeSet']),editorStarts=journal['editorStarts'],seconds=time.monotonic()-started)
            write_json(stage/'journal.json',journal)
        witness={row['path']:None if not row['exists'] else dict(sha256=row['sha256'],bytes=row['bytes']) for row in report.get('files',[])}
        journal['recoveryWitnessMatches']=witness==journal['after']
        if code!=0 or report.get('status')!='verified' or report.get('planSha256')!=expected or not journal['recoveryWitnessMatches'] or journal['unplannedPaths']:
            journal['status']='recovery-required';write_json(stage/'journal.json',journal)
            cause=(report.get('error') or journal.get('nativeBeforeReopen',{}).get('error') or 'No verified native report or unexpected source mutation').splitlines()[0][:1000]
            if production and 'RESOURCE_NEEDS_ACTION' in report.get('error',''):
                fail('PRODUCTION_PROVIDER_BLOCKED',str(stage),cause+'; prior steps may have writes; inspect checkpoint before recovery',status='blocked')
            fail('BATCH_NATIVE_FAILED',str(stage),cause+'; inspect journal/native checkpoint; recover only exact witnessed state')
        journal['status']='complete';write_json(stage/'journal.json',journal)
    return dict(status='verified',report=str(stage/'journal.json'),transaction=str(stage),changes=journal['changedPaths'],
                nativeReadback='saved_and_reopened_independent_editor' if journal['editorStarts']==2 else 'saved_and_reloaded_same_editor',metrics=journal['metrics'],diagnostics=report.get('diagnostics',[])[:32],diagnosticCount=len(report.get('diagnostics',[])))


def recover(transaction,output,editor,timeout=600):
    transaction=safe(transaction);journal=load(read(transaction/'journal.json'));data=load(read(transaction/'batch-plan.json',max_bytes=128*1024**2))
    if journal.get('contract') not in ('beaver-batch-transaction/2','beaver-production-transaction/3') or journal.get('status') not in ('complete','recovery-required') or journal.get('implementationSha256')!=identity():fail('BATCH_RECOVERY_CONTRACT',str(transaction),'Current ordinary batch transaction required; no legacy journal replay')
    project=safe(journal['projectRoot']);editor=editor_path(editor)
    if data['projectRoot']!=str(project) or sha(encoded(data))!=journal['planSha256']:fail('BATCH_RECOVERY_PLAN',str(transaction),'Pinned transaction plan changed')
    report=journal.get('native') or (load(read(transaction/'native-result.json')) if (transaction/'native-result.json').exists() else {})
    witness={r['path']:None if not r['exists'] else dict(sha256=r['sha256'],bytes=r['bytes']) for r in report.get('files',[])}
    if report.get('planSha256')!=journal['planSha256'] or witness!=journal.get('after') or journal.get('unplannedPaths'):
        fail('BATCH_RECOVERY_UNCERTAIN',str(transaction),'No complete native hash checkpoint or unexpected writes; inspect manually, no guessed overwrite')
    for rel,before in journal['before'].items():
        if before is not None and state(transaction/'before',[rel])[rel]!=before:fail('BATCH_BACKUP_DRIFT',rel,'Original before-image changed')
    stage=new_output(output,[project,transaction]);write_json(stage/'batch-plan.json',data)
    with lock(project):
        native_lock(project)
        if state(project,journal['writeSet'])!=journal['after']:fail('BATCH_LATER_EDIT',str(transaction),'Later manual changes detected; recovery refuses all writes')
        root,owned=helper(project,'ProjectSettings/EditorSettings.asset' in journal['before'])
        try:
            job=dict(projectRoot=str(project),stage=str(stage),mode='recover',planSha256=journal['planSha256'],writes=journal['writeSet'],steps=data['spec']['steps'],
                     preconditions=[dict(path=p,exists=v is not None,sha256=v['sha256'] if v else '') for p,v in journal['after'].items()],
                     restores=[dict(path=p,source=str(transaction/'before'/p),exists=v is not None,sha256=v['sha256'] if v else '') for p,v in journal['before'].items()])
            code,report=launch(editor,project,stage,job,timeout)
        finally:cleanup(project,root,owned)
        if code!=0 or report.get('status')!='verified' or state(project,journal['writeSet'])!=journal['before']:
            write_json(stage/'recovery.json',dict(status='failed',native=report));fail('BATCH_RECOVERY_FAILED',str(stage),'Restore did not return exact original native files')
        journal['status']='recovered';journal['recoveryReport']=str(stage/'recovery.json');write_json(transaction/'journal.json',journal)
        result=dict(status='verified',nativeReadback='restored_exact_bytes_and_imported',restored=len(journal['writeSet']),report=str(stage/'recovery.json'),editorStarts=1)
        write_json(stage/'recovery.json',result)
    return result
