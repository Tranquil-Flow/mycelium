# SPDX-License-Identifier: AGPL-3.0-only
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
from .stop_request import StopRequest


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


def process_resident_bytes() -> int:
    """Resident memory of this process (win32: working set via GetProcessMemoryInfo)."""

    if not is_win32():
        raise HostPlatformError("host_platform_unavailable", "win32_only")
    try:
        value = int(win32_api().process_working_set_bytes())
    except OSError as exc:
        raise HostPlatformError("host_io_failed", "memory") from exc
    if value <= 0:
        raise HostPlatformError("host_io_failed", "memory")
    return value


def available_memory_bytes() -> int:
    """Available physical memory (win32: GlobalMemoryStatusEx.ullAvailPhys)."""

    if not is_win32():
        raise HostPlatformError("host_platform_unavailable", "win32_only")
    try:
        return int(win32_api().available_physical_bytes())
    except OSError as exc:
        raise HostPlatformError("host_io_failed", "memory") from exc


STOP_EVENT_ENV = "MYCELIUM_STOP_EVENT"
_STOP_EVENT_POLL_MS = 250


def start_stop_event_watcher(
    on_stop: Any,
    *,
    environ: Any = None,
) -> threading.Event | None:
    """Watch the named stop event a win32 supervisor passes in ``MYCELIUM_STOP_EVENT``.

    Returns a ``threading.Event`` that stops the watcher when set, or ``None``
    when no event is configured.  A configured event that cannot be opened
    raises ``host_platform_unavailable``: a supervisor that asked for a
    graceful-stop channel must not be silently ignored.
    """

    source = os.environ if environ is None else environ
    name = source.get(STOP_EVENT_ENV)
    if not name:
        return None
    if not is_win32():
        raise HostPlatformError("host_platform_unavailable", "stop_event_win32_only")
    api = win32_api()
    try:
        event = api.open_event(name)
    except OSError as exc:
        raise HostPlatformError("host_platform_unavailable", "stop_event") from exc
    cancelled = threading.Event()

    def watch() -> None:
        try:
            while not cancelled.is_set():
                try:
                    signalled = api.wait_event(event, _STOP_EVENT_POLL_MS)
                except OSError:
                    signalled = True  # an unwatchable channel stops, never hangs
                if signalled:
                    if not cancelled.is_set():
                        on_stop()
                    return
        finally:
            try:
                api.close(event)
            except OSError:
                pass

    threading.Thread(target=watch, name="mycelium-stop-event", daemon=True).start()
    return cancelled


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


class HeldLock:
    """A long-lived exclusive lock on ``root/name`` held until :meth:`release`.

    ``root`` must already be an owner-only directory; the lease on it stays
    open while the lock is held.  Non-blocking contention raises
    ``host_lock_busy``.
    """

    def __init__(self, root: str | os.PathLike[str], name: str, *, blocking: bool = True) -> None:
        from contextlib import ExitStack

        self._stack = ExitStack()
        try:
            directory = self._stack.enter_context(private_directory(root, create=False))
            self._stack.enter_context(directory.exclusive_lock(name, blocking=blocking))
        except BaseException:
            self._stack.close()
            raise
        self.held = True

    def release(self) -> None:
        if self.held:
            self.held = False
            self._stack.close()

    def __enter__(self) -> "HeldLock":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.release()


def hold_exclusive_lock(
    root: str | os.PathLike[str], name: str, *, blocking: bool = True
) -> HeldLock:
    return HeldLock(root, name, blocking=blocking)


__all__ = [
    "HeldLock",
    "hold_exclusive_lock",
    "ERROR_CODES",
    "available_memory_bytes",
    "ExecutableFingerprint",
    "FileIdentity",
    "HostPlatformError",
    "executable_fingerprint",
    "is_win32",
    "open_beneath",
    "private_directory",
    "process_resident_bytes",
    "start_stop_event_watcher",
    "StopRequest",
    "read_owner_file",
    "use_win32_api",
    "win32_api",
]
