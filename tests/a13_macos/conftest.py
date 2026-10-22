# SPDX-License-Identifier: AGPL-3.0-only
"""Desk-only safety: real quarantine writes are forbidden, even on data files."""
from __future__ import annotations

import ctypes
import ctypes.util
import os
from pathlib import Path
import re
import subprocess
import sys

import pytest

QUARANTINE = "com.apple.quarantine"  # guard/fake identifier, never written to the filesystem
_GETXATTR = None
if sys.platform == "darwin":
    _GETXATTR = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True).getxattr
    _GETXATTR.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_void_p,
                        ctypes.c_size_t, ctypes.c_uint32, ctypes.c_int]
    _GETXATTR.restype = ctypes.c_ssize_t


def check_clean_path(path):
    """Read-only check, including app ancestors; never launch marked inputs."""
    if _GETXATTR is None:
        return
    for target in {path.absolute(), path.resolve()}:
        for parent in (target, *target.parents):
            if _GETXATTR(os.fsencode(parent), QUARANTINE.encode(), None, 0, 0, 1) >= 0:
                raise RuntimeError(f"QUARANTINED INPUT FORBIDDEN: {parent}")


def refuse_quarantine(name):
    if os.fsdecode(name) == QUARANTINE:
        raise RuntimeError("REAL QUARANTINE WRITE FORBIDDEN: inject the in-memory xattr fake")


def check_command(command):
    parts = [command] if isinstance(command, (str, bytes, os.PathLike)) else command
    text = " ".join(os.fsdecode(p) for p in parts)
    # Inspect script operands too: shell=True and `sh script` must not bypass the guard.
    for part in parts:
        try:
            path = Path(os.fsdecode(part))
            if path.exists():
                check_clean_path(path)
            if path.is_file() and path.stat().st_size < 1_000_000:
                text += "\n" + path.read_text(errors="ignore")
        except (OSError, ValueError):
            pass
    if QUARANTINE in text and (
        re.search(r"(?:^|[\s\"'])-(?:[a-zA-Z]*w|--?write)", text)
        or "setxattr" in text or ".set(" in text or "__setitem__" in text
    ):
        raise RuntimeError("REAL QUARANTINE WRITE FORBIDDEN: inject the in-memory xattr fake")


@pytest.fixture(scope="session", autouse=True)
def no_real_quarantine():
    """Block Python APIs and all subprocess entry points before any test body runs."""
    with pytest.MonkeyPatch.context() as patch:
        if hasattr(os, "setxattr"):
            original = os.setxattr

            def guarded_setxattr(path, attribute, *args, **kwargs):
                refuse_quarantine(attribute)
                return original(path, attribute, *args, **kwargs)

            patch.setattr(os, "setxattr", guarded_setxattr)
        try:
            import xattr
        except ImportError:
            pass
        else:
            def wrap(function):
                def guarded(path, name, *args, **kwargs):
                    refuse_quarantine(name)
                    return function(path, name, *args, **kwargs)
                return guarded
            for name in ("setxattr",):
                if hasattr(xattr, name):
                    patch.setattr(xattr, name, wrap(getattr(xattr, name)))
            for name in ("set", "__setitem__"):
                patch.setattr(xattr.xattr, name, wrap(getattr(xattr.xattr, name)))
        original_popen = subprocess.Popen.__init__

        def guarded_popen(self, args, *pos, **kwargs):
            check_command(args)
            return original_popen(self, args, *pos, **kwargs)

        patch.setattr(subprocess.Popen, "__init__", guarded_popen)
        original_system = os.system

        def guarded_system(command):
            check_command(command)
            return original_system(command)

        patch.setattr(os, "system", guarded_system)
        # Audit hooks also cover aliases imported before the fixture and os.exec/spawn.
        active = True

        def audit(event, args):
            if not active:
                return
            if event == "os.setxattr":
                refuse_quarantine(args[1])
            elif event == "subprocess.Popen":
                check_command(args[1])
            elif event in ("os.exec", "os.posix_spawn"):
                check_command(args[1])
            elif event == "os.system":
                check_command(args[0])

        sys.addaudithook(audit)
        try:
            yield
        finally:
            active = False


@pytest.fixture
def fake_xattrs(monkeypatch, request, no_real_quarantine):
    from fake_xattrs import FakeXattrs

    fake = FakeXattrs()
    for name, function in (("_xattr", fake.attribute), ("_ditto", fake.copy), ("_copy", fake.copy)):
        if hasattr(request.module, name):
            monkeypatch.setattr(request.module, name, function)
    return fake


@pytest.fixture
def fake_xattr_command(fake_xattrs, tmp_path):
    client, server, thread = fake_xattrs.shell_adapter(tmp_path)
    try:
        yield client
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        assert not thread.is_alive()
