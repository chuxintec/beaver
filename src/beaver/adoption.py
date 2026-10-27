# Derived from frozen monkey source; MIT notice in licenses/MONKEY-MIT.txt.
"""Three-way, manifest-bound adoption of exported assets into an owned project.

Only native Editor backends mutate assets. Unknown project files and removed
source assets are retained. Candidate snapshots remain immutable.
"""
from pathlib import Path
from collections import Counter
import re
import uuid
import unicodedata
from .io import safe,inside,read,load,sha,encoded,write_json,new_output
from .candidate_snapshot import snapshot,verify
from .output_limits import digest_file,copy_verified,MANIFEST_BYTES,MANIFEST_NODES
from .errors import fail
from .owned_fs import RootReader
from . import __version__

STATE = '.beaver/adoption/state.json'
GUID = re.compile(rb'^guid:\s*([a-f0-9]{32})\s*$',re.M)


def algorithm_id():
    root=Path(__file__).parent
    names=['__init__.py','native_environment.py','adoption.py','adoption_native.py','candidate_snapshot.py','output_limits.py','owned_fs.py','io.py','errors.py','transaction_fs.py','targets/BeaverAdoption.cs']
    return sha(encoded({name:digest_file(root/name) for name in names}))


def asset_root(profile): return 'Assets' if profile.startswith('unity-') else 'assets'


def identity(meta, engine):
    if engine == 'Assets':
        found = GUID.search(meta)
        return found.group(1).decode() if found else None
    document = load(meta)
    value = document.get('uuid')
    return value if isinstance(value,str) and value else None


def inventory_assets(root,files,profile):
    """Bind root asset GUIDs to exact owned file/meta pairs; directories aren't assets."""
    prefix = asset_root(profile) + '/'; units = {}; paths = {}
    with RootReader(root) as view:
        for path,row in files.items():
            if not path.startswith(prefix) or path.endswith('.meta'): continue
            meta = files.get(path+'.meta')
            if meta:
                raw=view.read(path+'.meta')
                if sha(raw)!=meta['sha256']:fail('CANDIDATE_DRIFT',path+'.meta','Metadata changed during identity read')
                guid=identity(raw,prefix[:-1])
            else:guid=None
            key = 'guid:'+guid if guid else 'path:'+path
            if key in units: fail('ADOPT_DUPLICATE_IDENTITY',path,'Multiple owned assets have the same GUID/UUID')
            units[key] = dict(path=path,guid=guid,file=row,meta=meta)
            paths[path] = key
    return units


def project_files(root,profile):
    prefix=asset_root(profile); directory=inside(root,prefix)
    if not directory.is_dir():fail('ADOPT_TARGET_PROJECT',str(root),'Expected an existing native project')
    files={};folded={}
    with RootReader(root) as view:
        for path in sorted(directory.rglob('*')):
            if path.is_symlink():fail('SYMLINK_PATH',str(path),'Target contains a symbolic link')
            if not path.is_file(): continue
            relative=path.relative_to(root).as_posix();key=unicodedata.normalize('NFC',relative).casefold()
            if key in folded:fail('ADOPT_PATH_COLLISION',relative,'Case/Unicode collision in target')
            folded[key]=relative
            digest,size=view.digest(relative)
            files[relative]=dict(path=relative,sha256=digest,bytes=size)
    from .output_limits import check_inventory
    check_inventory(list(files.values()),dict(scopeMode='whole-input'),str(root))
    return files


def same_payload(left,right):
    def shape(unit):return tuple((unit.get(k) or {}).get('sha256') for k in ('file','meta')) if unit else None
    return shape(left)==shape(right)


def native_lock(root):
    markers=[name for name in ('Temp/UnityLockfile','temp/editor.lock') if inside(root,name).exists()]
    if not markers:return
    # Some Editor failure exits retain their marker. Never delete it: check
    # the real process inventory, then let the Editor acquire its own OS lock.
    import subprocess
    try:
        processes=subprocess.run(['ps','-axo','command='],stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,text=True,timeout=10,check=True).stdout.splitlines()
    except (OSError,subprocess.SubprocessError):
        fail('ADOPT_EDITOR_STATE_UNKNOWN',str(root),'Cannot verify an existing Editor marker; preserve it and inspect the target')
    if any(str(root) in command and any(token in command for token in ('Unity.app/Contents/MacOS/Unity','Unity.exe','CocosCreator.app/Contents/MacOS/CocosCreator')) for command in processes):
        fail('ADOPT_EDITOR_BUSY',str(root),'Target is open in an Editor; close it normally before adoption')


def project_requirements(incoming,target,profile):
    observed={};conflicts=[];requirements={}
    if profile.startswith('unity-'):
        version_file=inside(target,'ProjectSettings/ProjectVersion.txt')
        version=version_file.read_text() if version_file.is_file() else ''
        if not re.search(r'^m_EditorVersion: 6000\.6\.2f1\s*$',version,re.M):
            conflicts.append(dict(identity='project-version',reason='target-editor-version',current=None,desired='6000.6.2f1'))
        for relative in ('Packages/manifest.json','ProjectSettings/ProjectVersion.txt'):
            file=inside(target,relative);observed[relative]=digest_file(file) if file.is_file() else None
        source=inside(incoming,'Packages/manifest.json');local=inside(target,'Packages/manifest.json')
        required=load(read(source)).get('dependencies',{}) if source.is_file() else {}
        present=load(read(local)).get('dependencies',{}) if local.is_file() else {}
        requirements=required
        for name,version in required.items():
            if present.get(name)!=version:
                conflicts.append(dict(identity='package:'+name,reason='package-requirement',current=present.get(name),desired=version))
    else:
        file=inside(target,'package.json');document=load(read(file)) if file.is_file() else {}
        observed['package.json']=digest_file(file) if file.is_file() else None
        if document.get('creator',{}).get('version')!='3.8.8':
            conflicts.append(dict(identity='project-version',reason='target-editor-version',current=None,desired='3.8.8'))
    return dict(files=observed,packages=requirements,policy='preserve target settings; never install or overwrite dependencies'),conflicts


def build_plan(candidate,target,baseline=None,lineage=None):
    incoming,im,ifiles,ih=snapshot(candidate);verify(incoming,ifiles)
    target=safe(target)
    if target==incoming or target.is_relative_to(incoming) or incoming.is_relative_to(target):
        fail('OUTPUT_OVERLAP',str(target),'Adoption target and candidate must be independent')
    if inside(target,'.monkey/adoption/state.json').exists() and not inside(target,STATE).exists():
        fail('ADOPT_LEGACY_STATE',str(target),'Legacy monkey state is retained; v0.1 does not silently reinterpret it. Use monkey recovery for old transactions and an explicit independent baseline in a fresh owned copy')
    if lineage and not re.fullmatch('[A-Za-z0-9][A-Za-z0-9_.-]{0,127}',lineage):fail('ADOPT_LINEAGE',str(lineage),'Expected explicit stable project namespace')
    state_path=inside(target,STATE);state=load(read(state_path,max_bytes=MANIFEST_BYTES),max_nodes=MANIFEST_NODES) if state_path.exists() else None
    explicit_lineage=lineage or (state or {}).get('lineageOverride')
    if explicit_lineage:
        if im['_manifestName']!='recovery-manifest.json' or 'namespace' in load(read(incoming/im['_manifestName'],max_bytes=MANIFEST_BYTES),max_nodes=MANIFEST_NODES):
            fail('ADOPT_LINEAGE',str(incoming),'An existing declared namespace cannot be overridden')
        im['namespace']=explicit_lineage
    if state and state.get('namespace')!=im['namespace']:fail('ADOPT_LINEAGE',str(target),'Saved project namespace differs')
    if baseline:
        old,bm,bfiles,bh=snapshot(baseline)
        if old not in (incoming,target):verify(old,bfiles)
        if old==target or old.is_relative_to(target) or target.is_relative_to(old):fail('OUTPUT_OVERLAP',str(old),'Baseline must be an immutable independent export')
        if explicit_lineage and bm['_manifestName']=='recovery-manifest.json':bm['namespace']=explicit_lineage
        if (bm['namespace'],bm['targetProfile'])!=(im['namespace'],im['targetProfile']):
            fail('ADOPT_LINEAGE',str(old),'Baseline and candidate target/namespace differ')
        before=inventory_assets(old,bfiles,bm['targetProfile'])
        if state and state.get('incomingManifestSha256')!=bh:
            fail('ADOPT_BASELINE_STALE',str(old),'Baseline differs from the previous adopted generation')
    elif state:
        if state.get('targetRoot')!=str(target) or (state.get('namespace'),state.get('targetProfile'))!=(im['namespace'],im['targetProfile']):
            fail('ADOPT_LINEAGE',str(target),'Saved adoption state belongs to another project/namespace')
        before=state['generation'];bh=state['incomingManifestSha256']
    else:fail('ADOPT_BASELINE_REQUIRED',str(target),'First adoption requires an explicit frozen baseline')
    profile=im['targetProfile'];actual=project_files(target,profile);current=inventory_assets(target,actual,profile);desired=inventory_assets(incoming,ifiles,profile)
    requirements,conflicts=project_requirements(incoming,target,profile)
    operations=[];preserved=[];removed=[];used=set()
    def conflict(key,reason,left,right):conflicts.append(dict(identity=key,reason=reason,current=left.get('path') if left else None,desired=right.get('path') if right else None))
    for key,unit in sorted(desired.items()):
        base=before.get(key);live=current.get(key);dest=unit['path']
        if live:used.add(live['path'])
        # A missing source meta has no proven identity. Its existing path may not
        # be claimed under a newly invented GUID after native import.
        if live is None and key.startswith('path:') and dest in actual:
            live=dict(path=dest,guid=None,file=actual[dest],meta=actual.get(dest+'.meta'));used.add(dest)
        if base and unit['path']==base['path'] and same_payload(base,unit):
            preserved.append(dict(identity=key,path=live['path'] if live else base['path'],reason='incoming-unchanged',present=live is not None));continue
        if unit['file'].get('owner')!='generated' or base and base['file'].get('owner') in ('manual','retained'):
            conflict(key,'non-generated-owner',live,unit);continue
        if not base:
            if live or dest in actual or dest+'.meta' in actual:
                conflict(key,'unowned-destination-or-identity',live,unit);continue
            operations.append(dict(action='add',identity=key,source=None,target=dest,guid=unit['guid'],before=None,after=unit));continue
        if not live:
            conflict(key,'locally-deleted',None,unit);continue
        if live['path']!=base['path'] and live['path']!=dest:
            conflict(key,'locally-moved',live,unit);continue
        if not same_payload(live,base):
            if live['path']==dest and same_payload(live,unit):
                preserved.append(dict(identity=key,path=dest,reason='already-applied',present=True));continue
            conflict(key,'locally-modified',live,unit);continue
        if dest!=live['path'] and (dest in actual or dest+'.meta' in actual):
            conflict(key,'occupied-destination',live,unit);continue
        if not unit['guid'] and dest!=live['path']:
            conflict(key,'unproven-move-identity',live,unit);continue
        operations.append(dict(action='move' if dest!=live['path'] else 'update',identity=key,source=live['path'],target=dest,guid=unit['guid'],before=live,after=unit))
    for key,unit in before.items():
        if key not in desired:
            live=current.get(key);removed.append(dict(identity=key,path=live['path'] if live else unit['path'],present=live is not None))
    # Moving parent directories is deliberately not implicit: each asset has its
    # own native move and folder GUIDs owned by the developer stay untouched.
    return dict(schemaVersion=1,contract='beaver-adopt/1',producer=dict(name='beaver',version=__version__),algorithmSha256=algorithm_id(),targetRoot=str(target),candidateRoot=str(incoming),
                candidateManifestName=im['_manifestName'],incomingManifestSha256=ih,beforeManifestSha256=bh,
                targetProfile=profile,namespace=im['namespace'],lineageOverride=explicit_lineage,projectRequirements=requirements,targetStateSha256=digest_file(state_path) if state else None,
                targetFiles=actual,generation=desired,operations=operations,conflicts=conflicts,preserved=preserved,
                retainedRemoved=removed,counts=dict(operations=len(operations),conflicts=len(conflicts),preserved=len(preserved),retainedRemoved=len(removed)),
                identityReadback='pending-native',business='not_run',visual='not_run')


def preview(candidate,target,output,baseline=None,lineage=None):
    plan=build_plan(candidate,target,baseline,lineage)
    out=new_output(output,[safe(candidate),safe(target),*([safe(baseline)] if baseline else [])])
    write_json(out/'adoption-plan.json',plan)
    identity=sha(encoded(plan));write_json(out/'plan-identity.json',dict(schemaVersion=1,sha256=identity))
    result=dict(status='blocked' if plan['conflicts'] else 'planned',counts=plan['counts'],planSha256=identity,report=str(out/'adoption-plan.json'))
    write_json(out/'adoption-result.json',result);return result


def checked_plan(path,expected=None):
    path=safe(path);raw=read(path,max_bytes=MANIFEST_BYTES);plan=load(raw,max_nodes=MANIFEST_NODES)
    saved=load(read(path.parent/'plan-identity.json'))
    if saved.get('sha256')!=sha(raw) or expected and expected!=sha(raw):fail('ADOPT_PLAN_CHANGED',str(path),'Plan identity differs')
    if plan.get('contract')!='beaver-adopt/1':fail('ADOPT_PLAN_CONTRACT',str(path),'Unknown adoption contract')
    return plan,sha(raw)


def prepare(plan_path,output,expected=None):
    """Recheck, stage bounded immutable payloads and before-images before Editor launch."""
    plan,identity_hash=checked_plan(plan_path,expected);target=safe(plan['targetRoot']);native_lock(target)
    if plan['conflicts']:fail('ADOPT_CONFLICT',str(plan_path),'Resolve conflicts with a new reviewed generation before execution')
    if plan.get('algorithmSha256')!=algorithm_id():fail('ADOPT_ALGORITHM_CHANGED',str(plan_path),'Adoption implementation changed; generate a new preview')
    incoming,m,files,h=snapshot(plan['candidateRoot'])
    if h!=plan['incomingManifestSha256']:fail('ADOPT_SOURCE_CHANGED',str(incoming),'Candidate manifest changed')
    verify(incoming,files)
    for relative,expected in plan['projectRequirements']['files'].items():
        file=inside(target,relative)
        if (digest_file(file) if file.is_file() else None)!=expected:fail('ADOPT_STALE_TARGET',relative,'Target project settings changed after preview')
    actual=project_files(target,plan['targetProfile'])
    if actual!=plan['targetFiles']:fail('ADOPT_STALE_TARGET',str(target),'Target changed after preview')
    state=inside(target,STATE)
    if (digest_file(state) if state.exists() else None)!=plan['targetStateSha256']:fail('ADOPT_BASELINE_STALE',str(target),'Adoption state changed')
    out=new_output(output,[target,incoming,safe(plan_path).parent])
    write_json(out/'adoption-plan.json',plan);write_json(out/'plan-identity.json',dict(schemaVersion=1,sha256=identity_hash))
    for index,op in enumerate(plan['operations']):
        for kind,suffix in (('file',''),('meta','.meta')):
            after=op['after'].get(kind)
            if after:copy_verified(inside(incoming,op['target']+suffix),out/f'after/{index}/{kind}',after['sha256'],after['bytes'])
            before=(op.get('before') or {}).get(kind)
            if before:copy_verified(inside(target,op['source']+suffix),out/f'before/{index}/{kind}',before['sha256'],before['bytes'])
    if state.exists():copy_verified(state,out/'before-state.json',digest_file(state),state.stat().st_size)
    journal=dict(schemaVersion=1,contract='beaver-adopt/1',planSha256=identity_hash,state='prepared',targetRoot=str(target),written=[],restored=[],processExited=True)
    write_json(out/'journal.json',journal)
    return out,plan,journal


def commit_state(stage,plan,journal,readback):
    target=safe(plan['targetRoot']);after=project_files(target,plan['targetProfile']);changed=set()
    for relative,expected in plan['projectRequirements']['files'].items():
        file=inside(target,relative)
        if (digest_file(file) if file.is_file() else None)!=expected:fail('ADOPT_PROJECT_DRIFT',relative,'Target project settings changed during adoption')
    for op in plan['operations']:
        for kind,suffix in (('file',''),('meta','.meta')):
            expected=op['after'].get(kind)
            if expected:
                path=op['target']+suffix;changed.add(path)
                if path not in after or after[path]['sha256']!=expected['sha256']:fail('ADOPT_READBACK',path,'Applied file differs from staged candidate')
            if op['source']:changed.add(op['source']+suffix)
    for path,before in plan['targetFiles'].items():
        if path not in changed and after.get(path)!=before:fail('ADOPT_MANUAL_DRIFT',path,'Untouched target file changed during adoption')
    state=dict(schemaVersion=1,targetRoot=str(target),namespace=plan['namespace'],lineageOverride=plan.get('lineageOverride'),targetProfile=plan['targetProfile'],
               incomingManifestSha256=plan['incomingManifestSha256'],generation=plan['generation'],planSha256=journal['planSha256'],
               retainedRemoved=plan['retainedRemoved'],transaction=str(stage),nativeReadback=readback)
    from .transaction_fs import atomic
    atomic(inside(target,STATE),encoded(state))
    journal.update(state='complete',afterStateSha256=sha(encoded(state)),targetFilesAfter=after,readback=readback)
    write_json(stage/'journal.json',journal)
    return dict(status='adopted',counts=plan['counts'],transaction=str(stage),planSha256=journal['planSha256'],nativeReadback=readback,report=str(stage/'journal.json'))
