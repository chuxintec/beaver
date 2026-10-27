# Derived from frozen monkey source; MIT notice in licenses/MONKEY-MIT.txt.
"""One bounded view of package and Unity-source export inventories."""
import re
import unicodedata
from .io import safe, inside, read, load, sha
from .output_limits import MANIFEST_BYTES, MANIFEST_NODES, check_inventory, digest_file
from .errors import fail

PROFILES = {'unity-6000.6.2f1-ugui', 'cocos-3.8.8'}


def snapshot(candidate):
    root = safe(candidate)
    names = [name for name in ('manifest.json','recovery-manifest.json') if (root/name).is_file()]
    if len(names) != 1: fail('CANDIDATE_MANIFEST', str(root), 'Expected one unambiguous owning manifest')
    raw = read(root/names[0], max_bytes=MANIFEST_BYTES)
    original = load(raw, max_nodes=MANIFEST_NODES)
    if original.get('schemaVersion') != 1: fail('UNSUPPORTED_CANDIDATE', str(root), 'Expected schema version 1')
    manifest = dict(original)
    if names[0] == 'recovery-manifest.json':
        identity = original.get('source',{}).get('sha256')
        if original.get('targetVersion') != '6000.6.2f1' or not re.fullmatch('[0-9a-f]{64}', identity or ''):
            fail('UNSUPPORTED_CANDIDATE', str(root), 'Unity recovery requires exact source identity and target version')
        manifest.update(targetProfile='unity-6000.6.2f1-ugui', namespace=original.get('namespace') or 'unity-source/'+identity, scopeMode='whole-input')
    if manifest.get('targetProfile') not in PROFILES or not isinstance(manifest.get('namespace'),str) or not manifest['namespace']:
        fail('UNSUPPORTED_CANDIDATE', str(root), 'Expected a supported target profile and namespace')
    manifest['_manifestName'] = names[0]
    files = {}; folded = {}; checked_dirs={''}
    def check_directory(relative):
        if relative in checked_dirs:return
        check_directory(relative.rpartition('/')[0])
        if (root/relative).is_symlink():fail('SYMLINK_PATH',relative,'Manifest directory is a symbolic link')
        checked_dirs.add(relative)
    for item in manifest.get('files',[]):
        row = dict(item); path = row.get('path')
        if not isinstance(path,str) or '\\' in path or any(ord(c)<32 for c in path) or ':' in path or any(x in ('','.','..') for x in path.split('/')):
            fail('UNSAFE_PATH', str(path), 'Expected canonical relative path')
        check_directory(path.rpartition('/')[0])
        if (root/path).is_symlink():fail('SYMLINK_PATH',path,'Manifest file is a symbolic link')
        key = unicodedata.normalize('NFC',path).casefold()
        if path in files or path == names[0] or key in folded:
            fail('INVALID_MANIFEST', path, 'Duplicate, case-colliding or reserved path')
        folded[key] = path
        row.setdefault('owner','generated')
        if row['owner'] not in ('generated','retained','manual') or not re.fullmatch('[0-9a-f]{64}',row.get('sha256','')):
            fail('INVALID_MANIFEST',path,'Invalid owner or digest')
        files[path] = row
    if not files: fail('INVALID_MANIFEST',str(root),'Empty candidate inventory')
    manifest['limits'] = check_inventory(list(files.values()),manifest,str(root))
    return root,manifest,files,sha(raw)


def verify(root, files):
    from .owned_fs import RootReader
    allowed=set(files)|{'manifest.json','recovery-manifest.json'}
    for path in root.rglob('*'):
        if path.is_symlink():fail('SYMLINK_PATH',str(path),'Frozen candidate contains a symbolic link')
        if path.is_file() and path.relative_to(root).as_posix() not in allowed:
            fail('CANDIDATE_UNOWNED',path.relative_to(root).as_posix(),'Unknown file in frozen candidate')
    with RootReader(root) as view:
        for name,row in files.items():
            try:digest,size=view.digest(name)
            except FileNotFoundError:fail('CANDIDATE_DRIFT',name,'Owned file missing')
            if size != row['bytes'] or digest != row['sha256']:
                fail('CANDIDATE_DRIFT',name,'Owned content differs from the frozen manifest')
