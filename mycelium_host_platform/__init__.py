# SPDX-License-Identifier: AGPL-3.0-or-later
"""Host platform seam: one API over POSIX and native win32 primitives.

Callers ask :func:`is_win32` at function entry and dispatch; their POSIX
bodies stay exactly as they were.  The win32 backend runs over one
:class:`~mycelium_host_platform._win32_api.Win32Api` object:

* on a real Windows host, the lazily bound ``ctypes`` implementation;
* in tests, an injected fake (see :func:`use_win32_api`).

When ``sys.platform`` reports ``win32`` but no real API can be bound and none
is injected, every operation raises ``host_platform_unavailable``: the seam
fails closed and never falls back to a POSIX primitive.
"""

from __future__ import annotations

from contextlib import contextmanager
import os
import sys
import threading
from typing import Any, BinaryIO, Iterator

from ._common import (
    ERROR_CODES,
    ExecutableFingerprint,
    FileIdentity,
    HostPlatformError,
)


_API_LOCK = threading.Lock()
_API_OVERRIDE: Any | None = None
_REAL_API: Any | None = None


def is_win32() -> bool:
    """Whether the win32 backend owns this call (read at call time)."""

    return sys.platform == "win32"


def win32_api() -> Any:
    """The injected Win32 API, else the real binding; refuses off Windows."""

    global _REAL_API
    with _API_LOCK:
        if _API_OVERRIDE is not None:
            return _API_OVERRIDE
        if _REAL_API is None:
            from ._win32_api import load_real_api

            _REAL_API = load_real_api()
        return _REAL_API


@contextmanager
def use_win32_api(api: Any) -> Iterator[Any]:
    """Install ``api`` as the Win32 API for the duration of the block."""

    global _API_OVERRIDE
    with _API_LOCK:
        previous = _API_OVERRIDE
        _API_OVERRIDE = api
    try:
        yield api
    finally:
        with _API_LOCK:
            _API_OVERRIDE = previous


def private_directory(path: str | os.PathLike[str], *, create: bool) -> Any:
    """Lease one owner-only directory (``Win32PrivateDirectory`` or POSIX)."""

    if is_win32():
        from . import _win32

        return _win32.open_private_directory(win32_api(), path, create=create)
    from . import _posix

    return _posix.open_private_directory(path, create=create)


def open_beneath(
    root: str | os.PathLike[str], relative_parts: tuple[str, ...]
) -> BinaryIO:
    """Open a regular file below ``root`` without following any link."""

    if is_win32():
        from . import _win32

        return _win32.open_beneath(win32_api(), root, tuple(relative_parts))
    from . import _posix

    return _posix.open_beneath(root, tuple(relative_parts))


def read_owner_file(path: str | os.PathLike[str], *, max_bytes: int) -> bytes:
    """Read one single-link file that only the current user can access."""

    if is_win32():
        from . import _win32

        return _win32.read_owner_file(win32_api(), path, max_bytes=max_bytes)
    from . import _posix

    return _posix.read_owner_file(path, max_bytes=max_bytes)


def executable_fingerprint(
    path: str | os.PathLike[str], *, require_private_owner: bool = False
) -> ExecutableFingerprint:
    """Handle-bound fingerprint of one executable file."""

    if is_win32():
        from . import _win32

        return _win32.executable_fingerprint(
            win32_api(), path, require_private_owner=require_private_owner
        )
    from . import _posix

    return _posix.executable_fingerprint(path, require_private_owner=require_private_owner)


__all__ = [
    "ERROR_CODES",
    "ExecutableFingerprint",
    "FileIdentity",
    "HostPlatformError",
    "executable_fingerprint",
    "is_win32",
    "open_beneath",
    "private_directory",
    "read_owner_file",
    "use_win32_api",
    "win32_api",
]
