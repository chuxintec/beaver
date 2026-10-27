"""Identity for all shipped executable sources, templates and dependency notices."""
from pathlib import Path
from .io import encoded,sha
from .output_limits import digest_file


def identity():
    root=Path(__file__).parent
    files={p.relative_to(root).as_posix():digest_file(p) for p in sorted(root.rglob('*'))
           if p.is_file() and '__pycache__' not in p.parts and p.suffix not in ('.pyc','.pyo')}
    return sha(encoded(files))
