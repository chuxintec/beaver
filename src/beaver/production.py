"""Project-bound resource/configuration/code installation into one native batch."""
from pathlib import Path
import re
from .io import safe, inside, read, load
from .errors import fail
from .output_limits import digest_file
from .project_profile import check, relative


def validate(spec):
    fields={'schemaVersion','operation','steps','readPaths','runtimeRoot','profile','fileEdits','requiredPackages'}
    if not isinstance(spec,dict) or set(spec)-fields or spec.get('schemaVersion')!=3 or spec.get('operation')!='production':
        fail('PRODUCTION_SPEC','spec','Closed schemaVersion 3 production spec required')
    if not isinstance(spec.get('profile'),str) or not Path(spec['profile']).is_absolute():fail('PRODUCTION_PROFILE','profile','Explicit absolute project profile required; no cwd inference')
    packages=spec.get('requiredPackages',{})
    if not isinstance(packages,dict) or len(packages)>64 or any(not isinstance(k,str) or not re.fullmatch(r'[a-z0-9]+(?:[.-][a-z0-9]+)+',k) or not isinstance(v,str) or not re.fullmatch(r'\d+\.\d+\.\d+(?:[-+][A-Za-z0-9.-]+)?',v) for k,v in packages.items()):fail('PRODUCTION_PACKAGES','requiredPackages','Explicit package IDs and exact resolved versions required')
    if not isinstance(spec.get('steps'),list) or len(spec['steps'])>256:fail('PRODUCTION_STEPS','steps','At most 256 native steps')
    edits=spec.get('fileEdits',[])
    if not isinstance(edits,list) or len(edits)>10000:fail('PRODUCTION_FILES','fileEdits','Bounded explicit file edits required')
    seen=set()
    for edit in edits:
        if not isinstance(edit,dict) or set(edit)!={'path','source','sha256','expectedSha256'}:fail('PRODUCTION_FILE','fileEdits','Explicit path/source/sha256/expectedSha256 required')
        path=relative(edit['path'])
        if not path.startswith('Assets/') or path.endswith('.meta'):fail('PRODUCTION_FILE_PATH',path,'Native Assets files only; metadata belongs to the Editor')
        if path in seen:fail('PRODUCTION_DUPLICATE',path,'One explicit file edit per path')
        seen.add(path)
        if not isinstance(edit['source'],str) or not Path(edit['source']).is_absolute():fail('PRODUCTION_SOURCE',path,'Explicit absolute independent source required')
        for field in ('sha256','expectedSha256'):
            if edit[field] is None and field=='expectedSha256':continue
            if not isinstance(edit[field],str) or not re.fullmatch('[a-f0-9]{64}',edit[field]):fail('PRODUCTION_HASH',path,'Exact source and existing destination hash (or absent null) required')
    if not spec['steps'] and not edits:fail('PRODUCTION_EMPTY','spec','At least one product file or native step required')
    from .batch import validate as batch_validate
    read_paths=spec.get('readPaths',[])
    if not isinstance(read_paths,list) or len(read_paths)>10000 or any(not isinstance(p,str) for p in read_paths):fail('PRODUCTION_READ_PATHS','readPaths','Bounded explicit source path array required')
    batch_validate(dict(schemaVersion=2,operation='batch',steps=[dict(kind='prefab.inspect',assetPath='Assets/ContractCheck.prefab')],readPaths=read_paths,**({k:spec[k] for k in ('runtimeRoot',) if k in spec})))
    bindings={}
    for step in spec['steps']:
        if not isinstance(step,dict):fail('PRODUCTION_STEP','steps','Object required')
        if str(step.get('kind','')).startswith('resource.'):
            from .resources import validate_step
            validate_step(step)
            for binding in step.get('consumerBindings',[]):
                key=tuple(binding[k] for k in ('assetPath','nodePath','componentType','property'))
                target=tuple(binding[k] for k in ('objectPath','objectName'))
                if key in bindings and bindings[key]!=target:
                    fail('PRODUCTION_BINDING_CONFLICT',':'.join(key),'One final object reference per consumer property is required; conflicting resource steps cannot both verify')
                bindings[key]=target
        else:
            batch_validate(dict(schemaVersion=2,operation='batch',steps=[step],**({k:spec[k] for k in ('runtimeRoot',) if k in spec})))
        if step.get('kind') in ('text.key','script.key') and step.get('assetPath') in seen:
            fail('PRODUCTION_EDIT_CONFLICT',step['assetPath'],'Do not combine installation and text transform on the same file')


def paths(project,spec):
    reads=set();writes=set()
    for edit in spec.get('fileEdits',[]):writes.update((edit['path'],edit['path']+'.meta'))
    for step in spec['steps']:
        if step['kind'].startswith('resource.'):
            from .resources import declared_paths
            declared=declared_paths(project,step)
            if not isinstance(declared,dict) or set(declared)!={'reads','writes'}:fail('PRODUCTION_RESOURCE_PATHS',step['kind'],'Exact declared reads and writes required')
            for key,target in (('reads',reads),('writes',writes)):
                for path in declared[key]:
                    relative(path)
                    if not path.startswith('Assets/') and path!='ProjectSettings/EditorSettings.asset':fail('PRODUCTION_RESOURCE_PATH',path,'Assets resources or explicitly selected Sprite Packer project setting only')
                    target.add(path)
            writes.update(p+'.meta' for p in tuple(writes) if p.startswith('Assets/') and not p.endswith('.meta'))
    return reads,writes


def binding(project,spec,writes=()):
    result=check(project,spec['profile'],writes=writes)
    if result['profile']['engine']!='unity':fail('PRODUCTION_ENGINE',str(project),'Use Go tools for a Go project binding')
    required=spec.get('requiredPackages',{})
    if required:
        manifest=load(read(inside(project,'Packages/manifest.json'))).get('dependencies',{})
        lock_path=inside(project,'Packages/packages-lock.json')
        resolved=load(read(lock_path)).get('dependencies',{}) if lock_path.is_file() else {}
        for package,version in required.items():
            actual=resolved.get(package,{}).get('version',manifest.get(package))
            if actual!=version:fail('PRODUCTION_PACKAGE_REQUIRED',package,'Required resolved package '+version+'; observed '+str(actual)+'; enable/review it before generating the transaction',status='blocked')
    return dict(path=result['path'],sha256=result['sha256'],projectId=result['profile']['projectId'])


def verify_sources(project,spec):
    for edit in spec.get('fileEdits',[]):
        source=safe(edit['source']);target=inside(project,edit['path'])
        if source==target:fail('PRODUCTION_SOURCE_OVERLAP',str(source),'Independent output is required; source is not edited in place')
        if digest_file(source)!=edit['sha256']:fail('PRODUCTION_SOURCE_DRIFT',str(source),'Generated input bytes changed')
        current=digest_file(target) if target.is_file() else None
        if current!=edit['expectedSha256']:fail('PRODUCTION_TARGET_DRIFT',edit['path'],'Explicit old content or absent destination does not match')


def plan(project,spec_path,output):
    from .batch import plan as batch_plan
    return batch_plan(project,spec_path,output)


def apply(plan_path,output,editor,expected,timeout=600):
    from .batch import apply as batch_apply
    return batch_apply(plan_path,output,editor,expected,timeout)


def recover(transaction,output,editor,timeout=600):
    from .batch import recover as batch_recover
    return batch_recover(transaction,output,editor,timeout)
