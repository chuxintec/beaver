# Derived from frozen monkey source; MIT notice in licenses/MONKEY-MIT.txt.
"""Shared bounded output policy, independent from source decoder budgets."""
from pathlib import Path
import hashlib
import os
import tempfile
from .errors import fail

MANIFEST_BYTES = 128 * 1024**2
MANIFEST_NODES = 8_000_000
FILE_BYTES = 512 * 1024**2
BUILD_FILE_BYTES = 4 * 1024**3
WHOLE_BYTES = 8 * 1024**3
WHOLE_FILES = 500_000
SELECTION_BYTES = 256 * 1024**2
SELECTION_FILES = 20_000
CHUNK_BYTES = 1024**2


def policy(manifest):
    whole = manifest.get('scopeMode') == 'whole-input'
    return dict(maxOwnedOutputBytes=WHOLE_BYTES if whole else SELECTION_BYTES,
                maxOwnedOutputFiles=WHOLE_FILES if whole else SELECTION_FILES,
                maxOwnedFileBytes=FILE_BYTES, maxManifestBytes=MANIFEST_BYTES)


def check_inventory(files, manifest, location):
    limits = policy(manifest)
    total = 0
    for row in files:
        size = row.get('bytes')
        if type(size) is not int or not 0 <= size <= limits['maxOwnedFileBytes']:
            fail('OUTPUT_FILE_BUDGET', location, 'Invalid or oversized owned file')
        total += size
    if total > limits['maxOwnedOutputBytes'] or len(files) > limits['maxOwnedOutputFiles']:
        fail('OUTPUT_BUDGET', location, 'Candidate exceeds shared output byte/file policy')
    return limits


def digest_file(path, max_bytes=FILE_BYTES):
    """Hash without materializing file bytes; reject growth beyond the policy."""
    from .io import safe
    path = safe(path)
    digest = hashlib.sha256(); size = 0
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(CHUNK_BYTES), b''):
            size += len(chunk)
            if size > max_bytes:
                fail('OUTPUT_FILE_BUDGET', str(path), 'File exceeds streaming byte budget')
            digest.update(chunk)
    return digest.hexdigest()


def copy_verified(source, destination, expected, expected_bytes, atomic=True):
    """Bounded copy, verifying the actual stream before publishing it."""
    from .io import safe
    source, destination = safe(source), safe(destination)
    if type(expected_bytes) is not int or not 0 <= expected_bytes <= FILE_BYTES:
        fail('OUTPUT_FILE_BUDGET', str(source), 'Invalid copy size')
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.beaver-copy-', dir=destination.parent)
    temporary = Path(temporary); digest = hashlib.sha256(); size = 0
    try:
        with os.fdopen(fd, 'wb') as output, source.open('rb') as stream:
            for chunk in iter(lambda: stream.read(CHUNK_BYTES), b''):
                size += len(chunk)
                if size > expected_bytes:
                    fail('SOURCE_CHANGED', str(source), 'Copy exceeds declared file size')
                digest.update(chunk); output.write(chunk)
            output.flush(); os.fsync(output.fileno())
        if size != expected_bytes or digest.hexdigest() != expected:
            fail('SOURCE_CHANGED', str(source), 'Copied bytes differ from declared identity')
        if not atomic and destination.exists():
            fail('OUTPUT_EXISTS', str(destination), 'Create-only copy destination exists')
        os.replace(temporary, destination)
    finally:
        if temporary.exists(): temporary.unlink()
