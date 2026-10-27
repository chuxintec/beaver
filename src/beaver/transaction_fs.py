"""Atomic state files and conservative per-project locking (from monkey)."""
from contextlib import contextmanager
import os
import uuid
from .io import safe, inside
from .errors import fail


def atomic(path, raw):
    path = safe(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = inside(path.parent, "." + path.name + ".beaver-" + uuid.uuid4().hex + ".tmp")
    try:
        with tmp.open("xb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()


@contextmanager
def lock(project):
    root = safe(project)
    state = inside(root, ".beaver")
    state.mkdir(exist_ok=True)
    path = inside(state, "operation.lock")
    try:
        with path.open("x") as stream:
            stream.write(str(os.getpid()))
    except FileExistsError:
        fail("PROJECT_LOCKED", str(path), "Inspect the existing operation journal before recovery")
    try:
        yield
    finally:
        path.unlink()
