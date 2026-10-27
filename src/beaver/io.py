# Extracted bounded file/JSON helpers from monkey; MIT notice retained.
import hashlib
import json
from pathlib import Path
from .errors import budget, fail
MAX_BYTES = 32 * 1024 * 1024

def sha(data):
    return hashlib.sha256(data).hexdigest()

def safe(path):
    path = Path(path).absolute()
    if any(p.is_symlink() for p in (path, *path.parents)):
        fail('SYMLINK_PATH', str(path), 'Symbolic links are not accepted')
    return path.resolve()

def inside(root, relative):
    if not isinstance(relative, str) or Path(relative).is_absolute():
        fail('UNSAFE_PATH', str(relative), 'Expected relative path')
    path = safe(root / relative)
    if not path.is_relative_to(root):
        fail('UNSAFE_PATH', relative, 'Path escapes root')
    return path

def read(path, max_bytes=MAX_BYTES):
    path = safe(path)
    with path.open('rb') as stream:
        raw = stream.read(max_bytes + 1)
    if len(raw) > max_bytes:
        fail('BYTE_BUDGET', str(path), 'Input exceeds declared byte budget')
    return raw

def load(raw, max_nodes=3_000_000, max_depth=96):
    def pairs(items):
        out = {}
        for key, value in items:
            if key in out:
                fail('DUPLICATE_KEY', '$', key)
            out[key] = value
        return out
    result = json.loads(raw.decode('utf-8-sig'), object_pairs_hook=pairs,
                        parse_constant=lambda x: fail('NONFINITE_NUMBER', '$', x))
    budget(result, max_nodes=max_nodes, max_depth=max_depth)
    return result

def encoded(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2,
                       allow_nan=False) + '\n').encode('utf-8')

def write_json(path, value):
    path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(encoded(value))

def new_output(path, inputs=()):
    path = safe(path)
    for item in inputs:
        item = safe(item)
        if path == item or path.is_relative_to(item) or item.is_relative_to(path):
            fail('OUTPUT_OVERLAP', str(path), 'Output overlaps an input')
    if path.exists():
        fail('OUTPUT_EXISTS', str(path), 'Use a fresh candidate directory')
    path.mkdir(parents=True, exist_ok=False)
    return path
