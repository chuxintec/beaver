# Derived from frozen monkey source; MIT notice in licenses/MONKEY-MIT.txt.
"""Bounded, root-relative reads with no-follow directory descriptors on POSIX.

Directory handles are cached only inside one read pass. Renaming a directory
cannot redirect an already opened handle outside the declared root.
"""
from collections import OrderedDict
from contextlib import contextmanager
from pathlib import Path
import hashlib
import os
import stat
from .io import safe
from .errors import fail
from .output_limits import FILE_BYTES,CHUNK_BYTES


class RootReader:
    def __init__(self,root):
        self.root=safe(root);self.cache=OrderedDict();self.fd=None
        self.native=os.open in os.supports_dir_fd and hasattr(os,'O_NOFOLLOW') and hasattr(os,'O_DIRECTORY')
        if self.native:self.fd=os.open(self.root,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)

    def __enter__(self):return self

    def __exit__(self,*args):
        for fd in self.cache.values():os.close(fd)
        self.cache.clear()
        if self.fd is not None:os.close(self.fd);self.fd=None

    def parts(self,relative):
        if not isinstance(relative,str) or relative.startswith('/') or '\\' in relative or ':' in relative or any(ord(c)<32 for c in relative):
            fail('UNSAFE_PATH',str(relative),'Expected canonical root-relative file')
        parts=relative.split('/')
        if any(p in ('','.','..') for p in parts):fail('UNSAFE_PATH',relative,'Unsafe relative component')
        return parts

    def directory(self,parts):
        if not parts:return self.fd
        key='/'.join(parts)
        if key in self.cache:
            fd=self.cache.pop(key);self.cache[key]=fd;return fd
        parent=self.directory(parts[:-1])
        fd=os.open(parts[-1],os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=parent)
        self.cache[key]=fd
        while len(self.cache)>128:os.close(self.cache.popitem(last=False)[1])
        return fd

    @contextmanager
    def open(self,relative):
        parts=self.parts(relative)
        if self.native:
            try:fd=os.open(parts[-1],os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=self.directory(parts[:-1]))
            except OSError as error:
                # Surface an explicit boundary failure without following the link.
                if error.errno in (40,62,20):fail('SYMLINK_PATH',relative,'Symbolic link or non-directory boundary rejected')
                raise
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                os.close(fd);fail('OWNED_FILE_TYPE',relative,'Only regular owned files may be read')
            stream=os.fdopen(fd,'rb')
        else:
            path=safe(self.root.joinpath(*parts))
            if not path.is_relative_to(self.root):fail('UNSAFE_PATH',relative,'Path escaped root')
            stream=path.open('rb')
        try:yield stream
        finally:stream.close()

    def read(self,relative,max_bytes=32*1024**2):
        with self.open(relative) as stream:data=stream.read(max_bytes+1)
        if len(data)>max_bytes:fail('BYTE_BUDGET',relative,'Owned document exceeds byte budget')
        return data

    def digest(self,relative,max_bytes=FILE_BYTES):
        h=hashlib.sha256();size=0
        with self.open(relative) as stream:
            for chunk in iter(lambda:stream.read(CHUNK_BYTES),b''):
                size+=len(chunk)
                if size>max_bytes:fail('OUTPUT_FILE_BUDGET',relative,'Owned file exceeds bounded byte budget')
                h.update(chunk)
        return h.hexdigest(),size
