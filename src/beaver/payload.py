"""Frozen file/alias identities, independent materialization and atomic promotion.

Mechanisms reviewed from the colleague handoff; implementation uses beaver's
existing bounded I/O. No foreign runtime, payloads, vendor files or game rules.
"""
from pathlib import Path
import ctypes
import errno
import hashlib
import os
import re
import stat
import sys
import unicodedata
import uuid
from . import __version__
from .errors import fail
from .io import safe, inside, read, load, sha, encoded, write_json, new_output
from .owned_fs import RootReader
from .output_limits import MANIFEST_BYTES, MANIFEST_NODES, FILE_BYTES, BUILD_FILE_BYTES, WHOLE_BYTES, WHOLE_FILES, CHUNK_BYTES, check_inventory, digest_file


def relative(value):
    if not isinstance(value,str) or not value or value.startswith('/') or '\\' in value or ':' in value or any(ord(c)<32 for c in value) or any(p in ('','.','..') for p in value.split('/')):
        fail('PAYLOAD_PATH',str(value),'Expected canonical portable root-relative path')
    for part in value.split('/'):
        if part.endswith((' ','.')) or any(c in part for c in '<>"|?*') or re.match(r'(?i)^(con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\.|$)',part):
            fail('PAYLOAD_PORTABLE_NAME',value,'Nonportable or reserved filename')
    return value


def key(path):
    return unicodedata.normalize('NFC',path).casefold()


def path_set(paths):
    spelling={};kinds={}
    for path in paths:
        parts=relative(path).split('/')
        for i in range(1,len(parts)+1):
            part='/'.join(parts[:i]);normalized=key(part);kind='file' if i==len(parts) else 'directory'
            if normalized in spelling and spelling[normalized]!=part or normalized in kinds and kinds[normalized]!=kind:
                fail('PAYLOAD_COLLISION',path,'Path spelling or file/directory prefix collision')
            spelling[normalized]=part;kinds[normalized]=kind


def artifact_limit(doc,artifact):
    if artifact and doc.get('artifactPolicy') not in (None,'beaver-unity-build/2'):
        fail('PAYLOAD_ARTIFACT_POLICY','manifest','Unsupported Unity artifact budget contract')
    return BUILD_FILE_BYTES if artifact and doc.get('artifactPolicy')=='beaver-unity-build/2' else FILE_BYTES


def manifest(path, allow_links=False,artifact=False):
    path=safe(path);raw=read(path,max_bytes=MANIFEST_BYTES);doc=load(raw,max_nodes=MANIFEST_NODES)
    if not isinstance(doc,dict) or doc.get('schemaVersion')!=1 or not isinstance(doc.get('files'),list):
        fail('PAYLOAD_MANIFEST',str(path),'Expected version-1 files inventory; foreign protocols are not inferred')
    max_file=artifact_limit(doc,artifact);files={};folded={}
    for row in doc['files']:
        if not isinstance(row,dict):fail('PAYLOAD_ROW',str(path),'Expected a file identity record')
        rel=relative(row.get('path'));normalized=key(rel)
        if normalized in folded:fail('PAYLOAD_COLLISION',rel,'Duplicate or case/Unicode-colliding destination')
        folded[normalized]=rel
        if 'symlink' in row:
            if not allow_links or not isinstance(row['symlink'],str):
                fail('PAYLOAD_SYMLINK',rel,'Declared symlinks are disabled for this operation')
            if set(row)!={'path','symlink'}:fail('PAYLOAD_ROW',rel,'Ambiguous symlink identity')
        else:
            if set(row)-{'path','sha256','bytes','owner'} or not re.fullmatch('[a-f0-9]{64}',row.get('sha256','')) or type(row.get('bytes'))is not int or not 0<=row['bytes']<=max_file:
                fail('PAYLOAD_ROW',rel,'Explicit size/SHA256 identity required')
        files[rel]=dict(row)
    path_set(files)
    regular=[r for r in files.values() if 'sha256'in r]
    if max_file==FILE_BYTES:check_inventory(regular,dict(scopeMode='whole-input'),str(path))
    elif sum(r['bytes'] for r in regular)>WHOLE_BYTES or len(files)>WHOLE_FILES:fail('PAYLOAD_ARTIFACT_BUDGET',str(path),'Unity artifact exceeds 8 GiB/500000 file policy')
    aliases=doc.get('aliases',[]);seen=set()
    if not isinstance(aliases,list) or len(aliases)>500000:fail('PAYLOAD_ALIASES',str(path),'Expected bounded explicit aliases')
    for row in aliases:
        if not isinstance(row,dict) or set(row)!={'id','path'} or not isinstance(row['id'],str) or not 1<=len(row['id'])<=4096 or any(ord(c)<32 for c in row['id']):
            fail('PAYLOAD_ALIAS',str(path),'Expected opaque alias ID and canonical file path')
        if row['id'] in seen:fail('PAYLOAD_ALIAS_COLLISION',row['id'],'Duplicate alias ID')
        seen.add(row['id'])
        if row['path'] not in files or 'sha256' not in files[row['path']]:
            fail('PAYLOAD_ALIAS_MISSING',row['id'],'Alias must name a declared regular file')
    return doc,files,sha(raw)


def stream_identity(stream,location,independent=True,output=None,max_file_bytes=None):
    before=os.fstat(stream.fileno())
    if not stat.S_ISREG(before.st_mode) or independent and before.st_nlink!=1:
        fail('PAYLOAD_SHARED_FILE',location,'Expected independent regular file (no hardlinks)')
    digest=hashlib.sha256();size=0
    for chunk in iter(lambda:stream.read(CHUNK_BYTES),b''):
        size+=len(chunk)
        if size>(FILE_BYTES if max_file_bytes is None else max_file_bytes):fail('PAYLOAD_FILE_BUDGET',location,'File exceeds streaming policy')
        digest.update(chunk)
        if output is not None:output.write(chunk)
    after=os.fstat(stream.fileno())
    shape=lambda s:(s.st_dev,s.st_ino,s.st_size,s.st_mtime_ns,s.st_ctime_ns,s.st_nlink)
    if shape(before)!=shape(after):fail('PAYLOAD_SOURCE_DRIFT',location,'Source changed during bounded read')
    return digest.hexdigest(),size,(before.st_dev,before.st_ino)


def verify_manifest(root,path,scope=None,allow_links=False,allow_unlisted=False,artifact=False):
    root=safe(root);path=safe(path);doc,files,manifest_sha=manifest(path,allow_links,artifact)
    prefix=relative(scope).rstrip('/')+'/' if scope else ''
    if prefix and any(not p.startswith(prefix) for p in files):
        fail('PAYLOAD_SCOPE',str(path),'Manifest contains paths outside the explicit artifact scope')
    start=inside(root,scope) if scope else root
    if not start.is_dir():fail('PAYLOAD_ROOT',str(start),'Expected existing inventory root')
    actual=set();folded={}
    own_manifest=path.relative_to(root).as_posix() if path.is_relative_to(root) else None
    for parent,dirs,names in os.walk(start,followlinks=False):
        for name in dirs+names:
            p=Path(parent)/name;rel=p.relative_to(root).as_posix();normalized=key(rel)
            if normalized in folded:fail('PAYLOAD_COLLISION',rel,'Filesystem has case/Unicode-colliding entries')
            folded[normalized]=rel
            if p.is_symlink():
                row=files.get(rel)
                if not allow_links or row is None or 'symlink'not in row or os.readlink(p)!=row['symlink'] or not p.resolve().is_relative_to(start):
                    fail('PAYLOAD_SYMLINK',rel,'Unowned, changed or escaping symbolic link')
                if name in dirs:dirs.remove(name)
                actual.add(rel)
            elif p.is_file():actual.add(rel)
            elif not p.is_dir():fail('PAYLOAD_FILE_TYPE',rel,'Only regular files and declared internal links are allowed')
        if len(actual)>500000:fail('PAYLOAD_FILE_BUDGET',str(start),'Too many files')
    if own_manifest:actual.discard(own_manifest)
    missing=sorted(set(files)-actual);unlisted=sorted(actual-set(files))
    if missing:fail('PAYLOAD_MISSING',missing[0],f'Missing {len(missing)} declared files')
    if unlisted and not allow_unlisted:fail('PAYLOAD_UNLISTED',unlisted[0],f'{len(unlisted)} files have no manifest identity')
    total=0
    with RootReader(root) as view:
        for rel,row in files.items():
            if 'symlink'in row:continue
            with view.open(rel) as stream:digest,size,_=stream_identity(stream,rel,max_file_bytes=artifact_limit(doc,artifact))
            if (digest,size)!=(row['sha256'],row['bytes']):fail('PAYLOAD_DRIFT',rel,'File size or SHA256 differs from the frozen manifest')
            total+=size
    if digest_file(path,max_bytes=MANIFEST_BYTES)!=manifest_sha:fail('PAYLOAD_MANIFEST_DRIFT',str(path),'Manifest changed during verification')
    links=sum('symlink'in row for row in files.values())
    return dict(status='partial' if unlisted else 'ok',manifestSha256=manifest_sha,files=len(files),bytes=total,
                aliases=len(doc.get('aliases',[])),independentFiles=links==0,independentRegularFiles=True,declaredSymlinks=links,scope=str(start),
                unlistedCount=len(unlisted),unlisted=unlisted[:100],unlistedReportTruncated=len(unlisted)>100,
                limitations=['Only declared file/alias identities; no gameplay or foreign protocol validation'])


def algorithm():
    root=Path(__file__).parent
    return sha(encoded({name:digest_file(root/name) for name in ('payload.py','io.py','owned_fs.py','output_limits.py','errors.py','__init__.py')}))


def separated(target,roots):
    target=safe(target)
    for root in roots:
        root=safe(root)
        if target==root or target.is_relative_to(root) or root.is_relative_to(target):
            fail('PAYLOAD_OVERLAP',str(target),'Source, target and receipt directories must be independent')
    return target


def plan(root,manifest_path,target,output,overrides=None,override_root=None):
    root=safe(root);manifest_path=safe(manifest_path)
    checked=verify_manifest(root,manifest_path)
    doc,files,digest=manifest(manifest_path)
    if 'payload-manifest.json' in files:fail('PAYLOAD_RESERVED_PATH','payload-manifest.json','Reserved output manifest path')
    if doc.get('contract') not in (None,'beaver-payload/1'):
        fail('PAYLOAD_CONTRACT',str(manifest_path),'Materialization accepts declared generic payloads only')
    target=separated(target,[root,manifest_path])
    if target.exists() or not target.parent.is_dir():fail('PAYLOAD_DESTINATION',str(target),'Use a fresh target under an existing parent')
    replacements=[];override_sha=None
    if bool(overrides)!=bool(override_root):fail('PAYLOAD_OVERRIDES','input','Supply both override manifest and root')
    if overrides:
        override_root=safe(override_root);overrides=safe(overrides);target=separated(target,[override_root,overrides])
        raw=read(overrides);o=load(raw);override_sha=sha(raw)
        if set(o)!={'schemaVersion','entries'} or o['schemaVersion']!=1 or not isinstance(o['entries'],list):
            fail('PAYLOAD_OVERRIDES',str(overrides),'Expected explicit version-1 override entries')
        old_paths=set();targets=set();path_keys={key(p):p for p in files}
        with RootReader(override_root) as view:
            for row in o['entries']:
                if not isinstance(row,dict) or set(row)!={'path','target','source','expectedSha256','sha256','bytes'}:
                    fail('PAYLOAD_OVERRIDE','input','Explicit baseline/new identity and paths required')
                old=relative(row['path']);dest=relative(row['target']);source=relative(row['source'])
                if old not in files or files[old]['sha256']!=row['expectedSha256']:
                    fail('PAYLOAD_OVERRIDE_BASELINE',old,'Override baseline is not the frozen content identity')
                if old in old_paths or key(dest)in targets or key(dest)in path_keys and path_keys[key(dest)]!=old:
                    fail('PAYLOAD_OVERRIDE_COLLISION',dest,'Duplicate/occupied override destination')
                if key(dest)==key(old) and dest!=old:fail('PAYLOAD_OVERRIDE_COLLISION',dest,'Case-only override rename is ambiguous')
                old_paths.add(old);targets.add(key(dest))
                if type(row['bytes'])is not int or not 0<=row['bytes']<=FILE_BYTES or not re.fullmatch('[a-f0-9]{64}',row['sha256']):
                    fail('PAYLOAD_OVERRIDE_IDENTITY',dest,'Invalid new file identity')
                with view.open(source) as stream:h,size,_=stream_identity(stream,source)
                if (h,size)!=(row['sha256'],row['bytes']):fail('PAYLOAD_OVERRIDE_DRIFT',source,'Override differs from declared content')
                replacements.append(dict(row))
    path_set(set(files)|{r['target'] for r in replacements})
    out=new_output(output,[root,target,manifest_path,*([override_root,overrides] if overrides else [])])
    data=dict(schemaVersion=1,contract='beaver-materialize/1',producer=dict(name='beaver',version=__version__),algorithmSha256=algorithm(),
              sourceRoot=str(root),manifestPath=str(manifest_path),manifestSha256=digest,targetRoot=str(target),
              overrideRoot=str(override_root) if override_root else None,overrideManifest=str(overrides) if overrides else None,
              overrideManifestSha256=override_sha,replacements=replacements,sourceVerification=checked,
              policy='independent streaming copies; preserve baseline files unless same-path override; redirect all declared aliases; atomic no-replace promotion')
    write_json(out/'payload-plan.json',data);identity=sha(encoded(data));write_json(out/'plan-identity.json',dict(sha256=identity))
    return dict(status='planned',planSha256=identity,report=str(out/'payload-plan.json'),files=len(files),overrides=len(replacements))


def promote(stage,target):
    """Atomic no-replace directory rename; refuse unsupported filesystems/platforms."""
    stage=safe(stage);target=safe(target)
    if sys.platform=='darwin':
        library=ctypes.CDLL(None,use_errno=True);function=library.renamex_np
        function.argtypes=[ctypes.c_char_p,ctypes.c_char_p,ctypes.c_uint];function.restype=ctypes.c_int
        code=function(os.fsencode(stage),os.fsencode(target),0x00000004) # SDK RENAME_EXCL
    elif sys.platform.startswith('linux'):
        library=ctypes.CDLL(None,use_errno=True);function=getattr(library,'renameat2',None)
        if function is None:fail('PAYLOAD_PROMOTION_UNAVAILABLE',str(target),'Atomic no-replace rename is unavailable')
        function.argtypes=[ctypes.c_int,ctypes.c_char_p,ctypes.c_int,ctypes.c_char_p,ctypes.c_uint];function.restype=ctypes.c_int
        code=function(-100,os.fsencode(stage),-100,os.fsencode(target),1)
    elif os.name=='nt':
        os.rename(stage,target);return
    else:fail('PAYLOAD_PROMOTION_UNAVAILABLE',str(target),'Unsupported atomic promotion backend')
    if code!=0:
        number=ctypes.get_errno()
        if number in (errno.EEXIST,errno.ENOTEMPTY):fail('PAYLOAD_TARGET_EXISTS',str(target),'Concurrent destination preserved; staging retained')
        raise OSError(number,os.strerror(number),str(target))


def copy_owned(view,relative_path,destination,expected,size):
    destination.parent.mkdir(parents=True,exist_ok=True)
    with view.open(relative_path) as source,destination.open('xb') as out:
        digest,actual,source_id=stream_identity(source,relative_path,output=out)
        out.flush();os.fsync(out.fileno());dest=os.fstat(out.fileno())
    if (digest,actual)!=(expected,size):fail('PAYLOAD_COPY_DRIFT',relative_path,'Actual copied stream differs from the plan')
    if source_id==(dest.st_dev,dest.st_ino) or dest.st_nlink!=1:fail('PAYLOAD_SHARED_COPY',relative_path,'Destination is not an independent file')


def apply(plan_path,output,expected):
    path=safe(plan_path);raw=read(path,max_bytes=MANIFEST_BYTES);data=load(raw,max_nodes=MANIFEST_NODES)
    if sha(raw)!=expected or load(read(path.parent/'plan-identity.json')).get('sha256')!=expected:
        fail('PAYLOAD_PLAN_DRIFT',str(path),'Expected exact plan bytes')
    if data.get('contract')!='beaver-materialize/1' or data.get('algorithmSha256')!=algorithm():
        fail('PAYLOAD_PLAN_IMPLEMENTATION',str(path),'Generate a plan with this implementation')
    root=safe(data['sourceRoot']);target=separated(data['targetRoot'],[root,path.parent])
    if data['overrideRoot']:separated(target,[data['overrideRoot']])
    transaction=new_output(output,[root,target,path.parent,*([safe(data['overrideRoot'])] if data['overrideRoot'] else [])])
    journal=dict(status='prepared',planSha256=expected,targetRoot=str(target),copied=0,promoted=False)
    write_json(transaction/'payload-plan.json',data);write_json(transaction/'journal.json',journal)
    stage=None
    try:
        if target.exists():fail('PAYLOAD_TARGET_EXISTS',str(target),'Existing target is never overwritten')
        report=verify_manifest(root,data['manifestPath'])
        if report['manifestSha256']!=data['manifestSha256']:fail('PAYLOAD_MANIFEST_DRIFT',data['manifestPath'],'Source manifest changed')
        doc,files,current_manifest=manifest(data['manifestPath'])
        if current_manifest!=data['manifestSha256']:fail('PAYLOAD_MANIFEST_DRIFT',data['manifestPath'],'Source manifest changed before copy')
        if data['overrideManifest'] and digest_file(data['overrideManifest'])!=data['overrideManifestSha256']:
            fail('PAYLOAD_OVERRIDE_DRIFT',data['overrideManifest'],'Override declaration changed')
        stage=inside(target.parent,'.'+target.name+'.beaver-stage-'+uuid.uuid4().hex);stage.mkdir(mode=0o700)
        journal.update(status='copying',stagingRoot=str(stage));write_json(transaction/'journal.json',journal)
        with RootReader(root) as source:
            for rel,row in files.items():
                copy_owned(source,rel,inside(stage,rel),row['sha256'],row['bytes']);journal['copied']+=1
                if journal['copied']%256==0:write_json(transaction/'journal.json',journal)
        aliases=[dict(row) for row in doc.get('aliases',[])];changes=[]
        if data['replacements']:
            with RootReader(data['overrideRoot']) as source:
                for row in data['replacements']:
                    dest=inside(stage,row['target'])
                    if row['target']==row['path']:
                        if digest_file(dest)!=row['expectedSha256']:fail('PAYLOAD_STAGING_DRIFT',row['target'],'Staged baseline changed')
                        temporary=inside(stage,row['target']+'.beaver-override-'+uuid.uuid4().hex)
                        copy_owned(source,row['source'],temporary,row['sha256'],row['bytes']);os.replace(temporary,dest)
                    else:copy_owned(source,row['source'],dest,row['sha256'],row['bytes'])
                    files[row['target']]=dict(path=row['target'],sha256=row['sha256'],bytes=row['bytes'])
                    redirected=[]
                    for alias in aliases:
                        if alias['path']==row['path']:alias['path']=row['target'];redirected.append(alias['id'])
                    changes.append(dict(path=row['path'],target=row['target'],beforeSha256=row['expectedSha256'],afterSha256=row['sha256'],aliases=redirected))
        reserved='payload-manifest.json'
        if reserved in files:fail('PAYLOAD_RESERVED_PATH',reserved,'Output manifest path reserved')
        output_manifest=dict(schemaVersion=1,contract='beaver-payload/1',producer=data['producer'],files=list(files.values()),aliases=aliases,
                             provenance=dict(inputManifestSha256=data['manifestSha256'],planSha256=expected),overrides=changes)
        write_json(stage/reserved,output_manifest);verified=verify_manifest(stage,stage/reserved)
        journal.update(status='verified-staging',verification=verified,overrides=changes);write_json(transaction/'journal.json',journal)
        if digest_file(data['manifestPath'],max_bytes=MANIFEST_BYTES)!=data['manifestSha256']:
            fail('PAYLOAD_MANIFEST_DRIFT',data['manifestPath'],'Source manifest changed before promotion')
        if data['overrideManifest'] and digest_file(data['overrideManifest'])!=data['overrideManifestSha256']:
            fail('PAYLOAD_OVERRIDE_DRIFT',data['overrideManifest'],'Override declaration changed before promotion')
        promote(stage,target)
        journal.update(status='complete',promoted=True,manifestPath=str(target/reserved),manifestSha256=digest_file(target/reserved))
        write_json(transaction/'journal.json',journal)
        return dict(status='ok',report=str(transaction/'journal.json'),targetRoot=str(target),files=verified['files'],bytes=verified['bytes'],aliases=verified['aliases'],overrides=changes,independentCopies=True,atomicPromotion=True)
    except BaseException as error:
        journal.update(status='promoted-receipt-failed' if journal.get('promoted') else 'failed-staging' if stage else 'blocked-before-copy',error=str(error))
        write_json(transaction/'journal.json',journal)
        if hasattr(error,'code'):fail(error.code,str(transaction),str(error))
        if isinstance(error,(OSError,ValueError,KeyError,TypeError)):fail('PAYLOAD_MATERIALIZE_FAILED',str(transaction),str(error))
        raise
