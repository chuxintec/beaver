import os
from pathlib import Path

# Tools are also executed by file path in subprocesses; make the package importable there.
_SRC = str(Path(__file__).resolve().parents[2]/'src')
os.environ['PYTHONPATH'] = _SRC + (os.pathsep + os.environ['PYTHONPATH'] if os.environ.get('PYTHONPATH') else '')
