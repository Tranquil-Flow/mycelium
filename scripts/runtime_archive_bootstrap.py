# SPDX-License-Identifier: AGPL-3.0-only
"""Standalone verified-tree bootstrap, embedded in emitted node entrypoints.

Only stdlib imports before validation. No zipimport: __file__ resource consumers
see the exact archived layout. Each invocation owns a private temporary tree and
removes it after the runtime (including its owned sidecar) has stopped.
"""
from contextlib import contextmanager
import hashlib
import os
from pathlib import Path, PurePosixPath
import runpy
import stat
import sys
import tempfile
import zipfile

# The producer appends RUNTIME_ARCHIVE_SHA256 and RUNTIME_MEMBERS to these bytes.


def _refuse():
    raise RuntimeError('runtime_archive_invalid')


@contextmanager
def runtime_tree():
    archive_path = Path(__file__).with_name('mycelium-runtime.zip')
    try:
        fd = os.open(archive_path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
        with os.fdopen(fd, 'rb') as stream:
            opened=os.fstat(stream.fileno())
            named=archive_path.lstat()
            if (not stat.S_ISREG(opened.st_mode) or not stat.S_ISREG(named.st_mode)
                    or (opened.st_dev,opened.st_ino)!=(named.st_dev,named.st_ino)):
                _refuse()
            if hashlib.file_digest(stream, 'sha256').hexdigest() != RUNTIME_ARCHIVE_SHA256:
                _refuse()
            stream.seek(0)
            with zipfile.ZipFile(stream) as archive, tempfile.TemporaryDirectory(prefix='mycelium-runtime-') as tmp:
                root = Path(tmp).resolve()
                entries = archive.infolist()
                if (len(entries) > 4096 or sum(i.file_size for i in entries) > 256*1024*1024):
                    _refuse()
                if len(entries) != len(RUNTIME_MEMBERS) or {i.filename for i in entries} != set(RUNTIME_MEMBERS):
                    _refuse()
                for item in entries:
                    relative = PurePosixPath(item.filename)
                    expected = RUNTIME_MEMBERS[item.filename]
                    mode = item.external_attr >> 16
                    if (relative.is_absolute() or str(relative) != item.filename or '..' in relative.parts
                            or '\\' in item.filename or not relative.parts or item.is_dir()
                            or stat.S_IFMT(mode) not in (0,stat.S_IFREG)
                            or item.file_size != expected['size'] or item.flag_bits & 1):
                        _refuse()
                    destination = root.joinpath(*relative.parts)
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    digest = hashlib.sha256()
                    total = 0
                    with archive.open(item) as source, destination.open('xb') as target:
                        while chunk := source.read(1024*1024):
                            total += len(chunk)
                            if total > expected['size']:
                                _refuse()
                            digest.update(chunk)
                            target.write(chunk)
                    if total != expected['size'] or digest.hexdigest() != expected['sha256']:
                        _refuse()
                    destination.chmod(expected['mode'])
                yield root
    except (OSError, zipfile.BadZipFile, KeyError, ValueError) as exc:
        raise RuntimeError('runtime_archive_invalid') from exc


def main():
    with runtime_tree() as root:
        # Isolate project imports from checkout/cwd; normal installed dependencies
        # remain available. The transfer-manifest binds this bootstrap and archive.
        sys.path.insert(0, str(root))
        sys.dont_write_bytecode = True
        runpy.run_path(str(root/'physical_inference_node.py'), run_name='__main__')
