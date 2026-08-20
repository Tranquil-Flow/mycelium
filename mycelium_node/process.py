# SPDX-License-Identifier: AGPL-3.0-or-later
"""Fail-closed supervision for the physical inference node JSONL service."""

from __future__ import annotations

from collections import OrderedDict, deque
from collections.abc import Mapping, Sequence
from contextlib import contextmanager
import ctypes
from dataclasses import dataclass
import json
import math
import os
from pathlib import Path
from queue import Empty, Queue
import re
import selectors
import shutil
import signal
import stat
import subprocess
import sys
import threading
import time
from typing import Any, Iterator
import uuid

from mycelium_qualification.evidence import canonical_json_bytes


NODE_CONTROL_PROTOCOL = "mycelium.physical_node_control.v1"
MAX_CONTROL_FRAME_BYTES = 1024 * 1024
MAX_STDERR_BYTES = 16 * 1024
_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,254}$")
_OPERATION_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_EOF = object()
_DIRECTORY_OPEN_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_CLOEXEC", 0)
    | getattr(os, "O_DIRECTORY", 0)
    | getattr(os, "O_NOFOLLOW", 0)
)
_PRIVATE_PATH_ANCHOR_ENV = "MYCELIUM_PRIVATE_PATH_ANCHOR"
_FILE_OPEN_FLAGS = (
    os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
)
_CWD_LEASE_LOCK = threading.RLock()
_CWD_LEASE_STACK: list[_WorkingDirectoryToken] = []
_CWD_RESTORE_ATTEMPTS = 3
_CWD_RESTORATION_ERROR = "working directory restoration failed"
_LAUNCH_HANDSHAKE_SECONDS = 2.0
_LAUNCH_READY = b"R"
_LAUNCH_RELEASE = b"G"
_LAUNCHER_SOURCE = """import os
import sys

ready = int(sys.argv[1])
release = int(sys.argv[2])
exec_status = int(sys.argv[3])
target = sys.argv[4:]
try:
    os.set_inheritable(exec_status, False)
    os.write(ready, b"R")
    os.close(ready)
    if os.read(release, 1) != b"G":
        os._exit(125)
    os.close(release)
    os.execv(target[0], target)
except BaseException:
    try:
        os.write(exec_status, b"E")
    except BaseException:
        pass
    os._exit(126)
"""


@dataclass
class _WorkingDirectoryToken:
    original_descriptor: int | None
    active: bool = True
    body_failure: BaseException | None = None


def _deactivate_working_directory_token(token: _WorkingDirectoryToken) -> None:
    token.active = False
    if not _CWD_LEASE_STACK or _CWD_LEASE_STACK[-1] is not token:
        return
    retired: list[_WorkingDirectoryToken] = []
    while _CWD_LEASE_STACK and not _CWD_LEASE_STACK[-1].active:
        retired.append(_CWD_LEASE_STACK.pop())
    restoration_descriptor = retired[-1].original_descriptor
    descriptor: int | None = None
    try:
        restored = False
        close_failed = False
        try:
            if restoration_descriptor is not None:
                for _attempt in range(_CWD_RESTORE_ATTEMPTS):
                    try:
                        os.fchdir(restoration_descriptor)
                    except OSError:
                        continue
                    restored = True
                    break
        finally:
            for retired_token in retired:
                descriptor = retired_token.original_descriptor
                retired_token.original_descriptor = None
                if descriptor is not None:
                    try:
                        os.close(descriptor)
                    except OSError:
                        close_failed = True
        if restored and not close_failed:
            return
        body_failures = [
            retired_token.body_failure
            for retired_token in retired
            if retired_token.body_failure is not None
        ]
        if body_failures:
            for body_failure in body_failures:
                if _CWD_RESTORATION_ERROR not in getattr(
                    body_failure,
                    "__notes__",
                    (),
                ):
                    body_failure.add_note(_CWD_RESTORATION_ERROR)
            return
        raise ValueError(_CWD_RESTORATION_ERROR) from None
    finally:
        descriptor = None
        restoration_descriptor = None


class NodeProcessError(RuntimeError):
    """Stable, fail-closed node-process failure."""

    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        self.detail = detail
        super().__init__(code if not detail else f"{code}:{detail}")


class _RemoteNodeError(NodeProcessError):
    pass


@dataclass(frozen=True)
class _ReaderError:
    code: str


@dataclass(frozen=True)
class _CommandInterrupted:
    code: str


@dataclass(frozen=True)
class _ExecutableIdentity:
    path: str
    device: int
    inode: int
    mode: int
    uid: int
    size: int
    mtime_ns: int
    ctime_ns: int


@dataclass(frozen=True)
class _ProcessIdentity:
    pid: int
    parent_pid: int
    process_group: int
    session_id: int
    start_token: str
    executable: _ExecutableIdentity


@dataclass(frozen=True)
class _PreExecOwnership:
    pid: int
    parent_pid: int
    process_group: int
    session_id: int
    start_token: str
    launcher_executable: _ExecutableIdentity


@dataclass(frozen=True)
class _ProcessGroupMember:
    pid: int
    process_group: int
    session_id: int


def _absolute_path(value: str | Path) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    return Path(os.path.abspath(path))


def _validate_walk_component(metadata: os.stat_result) -> None:
    if not stat.S_ISDIR(metadata.st_mode):
        raise ValueError("path component is invalid")
    if metadata.st_uid not in {0, os.getuid()}:
        raise ValueError("path component is invalid")
    writable_by_others = stat.S_IMODE(metadata.st_mode) & 0o022
    if writable_by_others and not (
        metadata.st_uid == 0 and metadata.st_mode & stat.S_ISVTX
    ):
        raise ValueError("path component is invalid")


def _descriptor_is_writable(metadata: os.stat_result) -> bool:
    if os.geteuid() == 0:
        return True
    mode = stat.S_IMODE(metadata.st_mode)
    if metadata.st_uid == os.geteuid():
        return mode & 0o300 == 0o300
    if metadata.st_gid in os.getgroups():
        return mode & 0o030 == 0o030
    return mode & 0o003 == 0o003


def _walk_start(path: Path) -> tuple[int, tuple[str, ...]]:
    """Open the trusted walk anchor and return path components below it."""

    configured = os.environ.get(_PRIVATE_PATH_ANCHOR_ENV)
    if configured is None:
        return os.open("/", _DIRECTORY_OPEN_FLAGS), tuple(path.parts[1:])
    anchor = Path(configured)
    if (
        not anchor.is_absolute()
        or anchor == Path("/")
        or anchor != _absolute_path(anchor)
        or not path.is_relative_to(anchor)
    ):
        raise ValueError("private path anchor is invalid")
    try:
        descriptor = os.open(anchor, _DIRECTORY_OPEN_FLAGS)
        metadata = os.fstat(descriptor)
        _validate_walk_component(metadata)
    except OSError as exc:
        raise ValueError("private path anchor is unavailable") from exc
    except Exception:
        if "descriptor" in locals():
            os.close(descriptor)
        raise
    return descriptor, tuple(path.relative_to(anchor).parts)


class PrivateDirectoryLease:
    """Live descriptor binding for one private directory used by an entrypoint."""

    def __init__(
        self,
        path: Path,
        *,
        parent_descriptor: int,
        descriptor: int | None,
    ) -> None:
        self.path = path
        self._parent_descriptor = parent_descriptor
        self._descriptor = descriptor
        self._closed = False
        if descriptor is None:
            self._device = None
            self._inode = None
        else:
            metadata = os.fstat(descriptor)
            self._device = metadata.st_dev
            self._inode = metadata.st_ino

    @property
    def exists(self) -> bool:
        return self._descriptor is not None

    def _require_open_descriptor(self) -> int:
        if self._closed or self._descriptor is None:
            raise ValueError("data directory is unavailable")
        return self._descriptor

    @staticmethod
    def _same_directory(
        left: os.stat_result,
        right: os.stat_result,
    ) -> bool:
        return (
            stat.S_ISDIR(left.st_mode)
            and stat.S_ISDIR(right.st_mode)
            and left.st_dev == right.st_dev
            and left.st_ino == right.st_ino
            and left.st_uid == right.st_uid == os.getuid()
            and stat.S_IMODE(left.st_mode) == 0o700
            and stat.S_IMODE(right.st_mode) == 0o700
        )

    def revalidate(self) -> None:
        """Require the absolute path and retained parent to name the leased inode."""

        descriptor = self._require_open_descriptor()
        try:
            retained = os.fstat(descriptor)
            retained_parent = os.fstat(self._parent_descriptor)
            retained_entry = os.stat(
                self.path.name,
                dir_fd=self._parent_descriptor,
                follow_symlinks=False,
            )
            fresh_parent_descriptor = private_directory_parent_fd(self.path)
            try:
                fresh_parent = os.fstat(fresh_parent_descriptor)
                fresh_entry = os.stat(
                    self.path.name,
                    dir_fd=fresh_parent_descriptor,
                    follow_symlinks=False,
                )
            finally:
                os.close(fresh_parent_descriptor)
        except OSError as exc:
            raise ValueError("data directory binding changed") from exc
        if (
            self._device is None
            or self._inode is None
            or not self._same_directory(retained, retained_entry)
            or not self._same_directory(retained, fresh_entry)
            or retained.st_dev != self._device
            or retained.st_ino != self._inode
            or retained_parent.st_dev != fresh_parent.st_dev
            or retained_parent.st_ino != fresh_parent.st_ino
        ):
            raise ValueError("data directory binding changed")

    @staticmethod
    def _relative_component(value: str) -> str:
        if not isinstance(value, str) or not value:
            raise ValueError("data directory child is invalid")
        candidate = Path(value)
        if (
            candidate.is_absolute()
            or candidate.parts != (value,)
            or value in {".", ".."}
        ):
            raise ValueError("data directory child is invalid")
        return value

    def private_subdirectory(self, name: str) -> "PrivateDirectoryLease":
        """Create/open one private child relative to the retained directory fd."""

        name = self._relative_component(name)
        descriptor = self._require_open_descriptor()
        self.revalidate()
        child: int | None = None
        parent_copy: int | None = None
        try:
            try:
                os.mkdir(name, mode=0o700, dir_fd=descriptor)
            except FileExistsError:
                pass
            child = os.open(name, _DIRECTORY_OPEN_FLAGS, dir_fd=descriptor)
            metadata = os.fstat(child)
            if (
                not stat.S_ISDIR(metadata.st_mode)
                or metadata.st_uid != os.getuid()
                or stat.S_IMODE(metadata.st_mode) != 0o700
            ):
                raise ValueError("data directory is invalid")
            parent_copy = os.dup(descriptor)
            lease = PrivateDirectoryLease(
                self.path / name,
                parent_descriptor=parent_copy,
                descriptor=child,
            )
            parent_copy = None
            child = None
            try:
                self.revalidate()
                lease.revalidate()
            except BaseException:
                lease.close()
                raise
            return lease
        except OSError as exc:
            raise ValueError("data directory is unavailable") from exc
        finally:
            if child is not None:
                os.close(child)
            if parent_copy is not None:
                os.close(parent_copy)

    @contextmanager
    def working_directory(self) -> Iterator[None]:
        """Pin relative downstream pathname APIs to this live directory fd."""

        descriptor = self._require_open_descriptor()
        with _CWD_LEASE_LOCK:
            original = os.open(".", _DIRECTORY_OPEN_FLAGS)
            token: _WorkingDirectoryToken | None = None
            body_failure: BaseException | None = None
            try:
                self.revalidate()
                os.fchdir(descriptor)
                token = _WorkingDirectoryToken(original_descriptor=original)
                _CWD_LEASE_STACK.append(token)
                try:
                    yield
                except BaseException as exc:
                    body_failure = exc
                    token.body_failure = exc
                    raise
            finally:
                if token is None:
                    os.close(original)
                else:
                    restoration_failure: BaseException | None = None
                    try:
                        _deactivate_working_directory_token(token)
                    except BaseException as exc:
                        restoration_failure = exc
                    try:
                        self.revalidate()
                    except ValueError:
                        if body_failure is None and restoration_failure is None:
                            raise
                    if restoration_failure is not None:
                        raise restoration_failure

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        failures: list[OSError] = []
        for descriptor in (self._descriptor, self._parent_descriptor):
            if descriptor is None:
                continue
            try:
                os.close(descriptor)
            except OSError as exc:
                failures.append(exc)
        self._descriptor = None
        if failures:
            raise ValueError("data directory close failed") from failures[0]

    def __enter__(self) -> "PrivateDirectoryLease":
        if self._closed:
            raise ValueError("data directory is unavailable")
        return self

    def __exit__(self, _type: Any, _value: Any, _traceback: Any) -> None:
        self.close()


def private_directory_lease(
    value: str | Path,
    *,
    create: bool = True,
) -> PrivateDirectoryLease:
    """Open a private directory while retaining its parent and final descriptors."""

    if not getattr(os, "O_NOFOLLOW", 0) or not getattr(os, "O_DIRECTORY", 0):
        raise ValueError("data directory is unavailable")
    path = _absolute_path(value)
    if path == Path("/"):
        raise ValueError("data directory is invalid")
    descriptor, components = _walk_start(path)
    try:
        _validate_walk_component(os.fstat(descriptor))
        for index, component in enumerate(components):
            final = index == len(components) - 1
            child: int | None = None
            try:
                try:
                    child = os.open(
                        component,
                        _DIRECTORY_OPEN_FLAGS,
                        dir_fd=descriptor,
                    )
                except FileNotFoundError:
                    if not create:
                        if not _descriptor_is_writable(os.fstat(descriptor)):
                            raise ValueError("data directory is unavailable")
                        lease = PrivateDirectoryLease(
                            path,
                            parent_descriptor=descriptor,
                            descriptor=None,
                        )
                        descriptor = -1
                        return lease
                    created = False
                    try:
                        os.mkdir(component, mode=0o700, dir_fd=descriptor)
                        created = True
                    except FileExistsError:
                        pass
                    child = os.open(
                        component,
                        _DIRECTORY_OPEN_FLAGS,
                        dir_fd=descriptor,
                    )
                    created_metadata = os.fstat(child)
                    if created:
                        if (
                            not stat.S_ISDIR(created_metadata.st_mode)
                            or created_metadata.st_uid != os.getuid()
                        ):
                            raise ValueError("data directory is invalid")
                        os.fchmod(child, 0o700)
                metadata = os.fstat(child)
                if final:
                    if (
                        not stat.S_ISDIR(metadata.st_mode)
                        or metadata.st_uid != os.getuid()
                        or stat.S_IMODE(metadata.st_mode) != 0o700
                    ):
                        raise ValueError("data directory is invalid")
                else:
                    _validate_walk_component(metadata)
                if final:
                    lease = PrivateDirectoryLease(
                        path,
                        parent_descriptor=descriptor,
                        descriptor=child,
                    )
                    descriptor = -1
                    child = None
                    try:
                        lease.revalidate()
                    except BaseException:
                        lease.close()
                        raise
                    return lease
                previous = descriptor
                descriptor = child
                child = None
                os.close(previous)
            finally:
                if child is not None:
                    os.close(child)
        raise ValueError("data directory is invalid")
    except OSError as exc:
        raise ValueError("data directory is unavailable") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def private_directory_path(
    value: str | Path,
    *,
    create: bool = True,
) -> Path:
    """Compatibility wrapper returning a path after descriptor-bound validation."""

    lease = private_directory_lease(value, create=create)
    try:
        return lease.path
    finally:
        lease.close()


def _identity_from_stat(path: Path, metadata: os.stat_result) -> _ExecutableIdentity:
    return _ExecutableIdentity(
        path=str(path),
        device=metadata.st_dev,
        inode=metadata.st_ino,
        mode=metadata.st_mode,
        uid=metadata.st_uid,
        size=metadata.st_size,
        mtime_ns=metadata.st_mtime_ns,
        ctime_ns=metadata.st_ctime_ns,
    )


def capture_executable_identity(
    value: str | Path,
    *,
    require_canonical: bool = False,
    require_private_owner: bool = False,
    require_executable: bool = True,
) -> _ExecutableIdentity:
    """Open and fingerprint one executable without following untrusted symlinks."""

    supplied = Path(value).expanduser()
    if require_canonical:
        if not supplied.is_absolute() or supplied != _absolute_path(supplied):
            raise ValueError("executable path is not canonical")
        path = supplied
    else:
        if supplied.parent == Path(".") and not supplied.is_absolute():
            located = shutil.which(str(supplied))
            if located is None:
                raise ValueError("executable is unavailable")
            supplied = Path(located)
        try:
            path = supplied.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise ValueError("executable is unavailable") from exc
    parent = private_directory_parent_fd(path, strict_components=False)
    try:
        descriptor = os.open(path.name, _FILE_OPEN_FLAGS, dir_fd=parent)
    except OSError as exc:
        os.close(parent)
        raise ValueError("executable is unavailable") from exc
    try:
        metadata = os.fstat(descriptor)
        executable_by_caller = (
            metadata.st_mode & stat.S_IXUSR
            if metadata.st_uid == os.geteuid()
            else (
                metadata.st_mode & stat.S_IXGRP
                if metadata.st_gid in os.getgroups()
                else metadata.st_mode & stat.S_IXOTH
            )
        )
        if (
            not stat.S_ISREG(metadata.st_mode)
            or (require_executable and not executable_by_caller)
            or (require_private_owner and metadata.st_uid != os.getuid())
            or (require_private_owner and stat.S_IMODE(metadata.st_mode) & 0o022)
        ):
            raise ValueError("executable is invalid")
        return _identity_from_stat(path, metadata)
    finally:
        os.close(descriptor)
        os.close(parent)


def private_directory_parent_fd(
    path: Path,
    *,
    strict_components: bool = True,
) -> int:
    """Open an absolute path's parent with a descriptor-relative no-follow walk."""

    if not path.is_absolute() or not getattr(os, "O_NOFOLLOW", 0):
        raise ValueError("path is invalid")
    descriptor, relative_parts = _walk_start(path)
    try:
        for component in relative_parts[:-1]:
            child: int | None = None
            try:
                if descriptor is None:
                    raise ValueError("path is invalid")
                parent = descriptor
                child = os.open(
                    component,
                    _DIRECTORY_OPEN_FLAGS,
                    dir_fd=parent,
                )
                metadata = os.fstat(child)
                if strict_components:
                    _validate_walk_component(metadata)
                elif (
                    not stat.S_ISDIR(metadata.st_mode)
                    or metadata.st_uid not in {0, os.getuid()}
                    or stat.S_IMODE(metadata.st_mode) & 0o002
                ):
                    raise ValueError("path component is invalid")
                descriptor = child
                child = None
                os.close(parent)
            finally:
                if child is not None:
                    os.close(child)
        result = descriptor
        descriptor = None
        return result
    finally:
        if descriptor is not None:
            os.close(descriptor)


def revalidate_executable_identity(identity: _ExecutableIdentity) -> bool:
    try:
        current = capture_executable_identity(
            identity.path,
            require_canonical=True,
            require_executable=False,
        )
    except (OSError, RuntimeError, TypeError, ValueError):
        return False
    return current == identity


def physical_service_interpreter_identity() -> _ExecutableIdentity:
    if sys.platform == "darwin":
        try:
            homebrew = capture_executable_identity("/opt/homebrew/bin/python3")
            framework = (
                Path(homebrew.path).parent.parent
                / "Resources"
                / "Python.app"
                / "Contents"
                / "MacOS"
                / "Python"
            )
            return capture_executable_identity(framework)
        except (OSError, RuntimeError, TypeError, ValueError):
            pass
    return capture_executable_identity(sys.executable)


def _inventory_linux_process(pid: int) -> _ProcessIdentity:
    stat_path = Path("/proc") / str(pid) / "stat"
    raw = stat_path.read_text(encoding="ascii")
    close_paren = raw.rfind(")")
    if close_paren < 0:
        raise ProcessLookupError(pid)
    fields = raw[close_paren + 2 :].split()
    if len(fields) < 20:
        raise ProcessLookupError(pid)
    parent_pid = int(fields[1])
    process_group = int(fields[2])
    session_id = int(fields[3])
    start_token = fields[19]
    executable_link = Path("/proc") / str(pid) / "exe"
    executable_path = Path(os.readlink(executable_link))
    metadata = os.stat(executable_link)
    return _ProcessIdentity(
        pid=pid,
        parent_pid=parent_pid,
        process_group=process_group,
        session_id=session_id,
        start_token=start_token,
        executable=_identity_from_stat(executable_path, metadata),
    )


def _linux_parent_and_group(pid: int) -> tuple[int, int]:
    """Read only ancestry fields when a hardened procfs hides ``/proc/PID/exe``."""

    raw = (Path("/proc") / str(pid) / "stat").read_text(encoding="ascii")
    close_paren = raw.rfind(")")
    if close_paren < 0:
        raise ProcessLookupError(pid)
    fields = raw[close_paren + 2 :].split()
    if len(fields) < 4:
        raise ProcessLookupError(pid)
    return int(fields[1]), int(fields[2])


class _DarwinBSDInfo(ctypes.Structure):
    _fields_ = [
        ("flags", ctypes.c_uint32),
        ("status", ctypes.c_uint32),
        ("xstatus", ctypes.c_uint32),
        ("pid", ctypes.c_uint32),
        ("ppid", ctypes.c_uint32),
        ("uid", ctypes.c_uint32),
        ("gid", ctypes.c_uint32),
        ("ruid", ctypes.c_uint32),
        ("rgid", ctypes.c_uint32),
        ("svuid", ctypes.c_uint32),
        ("svgid", ctypes.c_uint32),
        ("reserved", ctypes.c_uint32),
        ("comm", ctypes.c_char * 16),
        ("name", ctypes.c_char * 32),
        ("nfiles", ctypes.c_uint32),
        ("pgid", ctypes.c_uint32),
        ("pjobc", ctypes.c_uint32),
        ("controlling_device", ctypes.c_uint32),
        ("foreground_pgid", ctypes.c_uint32),
        ("nice", ctypes.c_int32),
        ("start_seconds", ctypes.c_uint64),
        ("start_microseconds", ctypes.c_uint64),
    ]


def _darwin_process_snapshot(pid: int) -> tuple[int, int, int, str]:
    library = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
    proc_pidinfo = library.proc_pidinfo
    proc_pidinfo.argtypes = [
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_uint64,
        ctypes.c_void_p,
        ctypes.c_int,
    ]
    proc_pidinfo.restype = ctypes.c_int
    information = _DarwinBSDInfo()
    size = ctypes.sizeof(information)
    received = proc_pidinfo(
        pid,
        3,
        0,
        ctypes.byref(information),
        size,
    )
    if received != size or information.pid != pid:
        raise ProcessLookupError(pid)
    return (
        int(information.pid),
        int(information.ppid),
        int(information.pgid),
        f"{information.start_seconds}:{information.start_microseconds}",
    )


def _darwin_executable_path(pid: int) -> Path:
    library = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
    proc_pidpath = library.proc_pidpath
    proc_pidpath.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32]
    proc_pidpath.restype = ctypes.c_int
    buffer = ctypes.create_string_buffer(4096)
    length = proc_pidpath(pid, buffer, len(buffer))
    if length <= 0:
        raise ProcessLookupError(pid)
    return Path(os.fsdecode(buffer.value)).resolve(strict=True)


def _inventory_darwin_process(pid: int) -> _ProcessIdentity:
    first = _darwin_process_snapshot(pid)
    session_id = os.getsid(pid)
    executable_path = _darwin_executable_path(pid)
    metadata = executable_path.stat()
    second = _darwin_process_snapshot(pid)
    second_path = _darwin_executable_path(pid)
    if (
        first != second
        or executable_path != second_path
        or os.getsid(pid) != session_id
    ):
        raise ProcessLookupError(pid)
    return _ProcessIdentity(
        pid=pid,
        parent_pid=first[1],
        process_group=first[2],
        session_id=session_id,
        start_token=first[3],
        executable=_identity_from_stat(executable_path, metadata),
    )


def _inventory_process(pid: int) -> _ProcessIdentity:
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 1:
        raise ProcessLookupError(pid)
    if sys.platform == "linux":
        return _inventory_linux_process(pid)
    if sys.platform == "darwin":
        return _inventory_darwin_process(pid)
    raise ProcessLookupError(pid)


def _deadline_remaining(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("process discovery deadline expired")
    return remaining


def _darwin_parent_and_group(pid: int, deadline: float) -> tuple[int, int]:
    output = subprocess.check_output(
        ["/bin/ps", "-o", "pid=,ppid=,pgid=", "-p", str(pid)],
        text=True,
        timeout=min(1.0, _deadline_remaining(deadline)),
    ).split()
    if len(output) != 3 or int(output[0]) != pid:
        raise ProcessLookupError(pid)
    return int(output[1]), int(output[2])


def _protected_process_groups(deadline: float) -> set[int]:
    _deadline_remaining(deadline)
    groups = {os.getpgrp()}
    process_ids: set[int] = set()
    pid = os.getpid()
    while pid > 1:
        _deadline_remaining(deadline)
        if pid in process_ids:
            raise ProcessLookupError(pid)
        process_ids.add(pid)
        try:
            identity = _inventory_process(pid)
        except (OSError, subprocess.SubprocessError, ValueError):
            if sys.platform == "darwin":
                parent_pid, process_group = _darwin_parent_and_group(pid, deadline)
            elif sys.platform == "linux":
                parent_pid, process_group = _linux_parent_and_group(pid)
            else:
                raise
            groups.add(process_group)
            pid = parent_pid
        else:
            groups.add(identity.process_group)
            pid = identity.parent_pid
        _deadline_remaining(deadline)
    if any(group <= 1 for group in groups):
        raise ProcessLookupError("protected process group is invalid")
    return groups


def _handshake_pipes() -> dict[str, int | None]:
    descriptors: dict[str, int | None] = {
        "ready_read": None,
        "ready_write": None,
        "release_read": None,
        "release_write": None,
        "exec_read": None,
        "exec_write": None,
    }
    opened: list[int] = []
    try:
        for read_name, write_name in (
            ("ready_read", "ready_write"),
            ("release_read", "release_write"),
            ("exec_read", "exec_write"),
        ):
            read_descriptor, write_descriptor = os.pipe()
            opened.extend((read_descriptor, write_descriptor))
            os.set_inheritable(read_descriptor, False)
            os.set_inheritable(write_descriptor, False)
            descriptors[read_name] = read_descriptor
            descriptors[write_name] = write_descriptor
        return descriptors
    except BaseException:
        for descriptor in opened:
            try:
                os.close(descriptor)
            except OSError:
                pass
        raise


def _close_handshake_descriptor(
    descriptors: dict[str, int | None],
    name: str,
) -> None:
    descriptor = descriptors[name]
    if descriptor is None:
        return
    descriptors[name] = None
    try:
        os.close(descriptor)
    except OSError:
        pass


def _handshake_descriptor(
    descriptors: Mapping[str, int | None],
    name: str,
) -> int:
    descriptor = descriptors.get(name)
    if (
        not isinstance(descriptor, int)
        or isinstance(descriptor, bool)
        or descriptor < 0
    ):
        raise RuntimeError("launcher handshake descriptor is unavailable")
    return descriptor


def _await_handshake_byte(
    descriptor: int,
    deadline: float,
    *,
    expected: bytes | None,
) -> None:
    selector = selectors.DefaultSelector()
    try:
        selector.register(descriptor, selectors.EVENT_READ)
        ready = selector.select(timeout=_deadline_remaining(deadline))
        if not ready:
            raise TimeoutError("launcher handshake timed out")
        value = os.read(descriptor, 1)
    finally:
        selector.close()
    if expected is None:
        if value:
            raise RuntimeError("launcher exec failed")
    elif value != expected:
        raise RuntimeError("launcher readiness failed")


def _await_pre_exec_launcher(
    descriptor: int,
    process: subprocess.Popen[bytes],
    deadline: float,
) -> None:
    _await_handshake_byte(descriptor, deadline, expected=_LAUNCH_READY)
    if process.poll() is not None:
        raise RuntimeError("launcher exited before ownership capture")


def _release_pre_exec_launcher(descriptor: int) -> None:
    if os.write(descriptor, _LAUNCH_RELEASE) != len(_LAUNCH_RELEASE):
        raise OSError("launcher release failed")


def _await_target_exec(
    descriptor: int,
    process: subprocess.Popen[bytes],
    deadline: float,
) -> None:
    _await_handshake_byte(descriptor, deadline, expected=None)
    if process.poll() is not None:
        raise RuntimeError("target exited during exec")


def _capture_pre_exec_ownership(
    process: subprocess.Popen[bytes],
    launcher_executable: _ExecutableIdentity,
    deadline: float,
) -> _PreExecOwnership:
    protected_groups = _protected_process_groups(deadline)
    current = _inventory_process(process.pid)
    _deadline_remaining(deadline)
    if (
        current.pid != process.pid
        or current.parent_pid != os.getpid()
        or current.process_group != current.pid
        or current.session_id != current.pid
        or current.pid <= 1
        or current.pid in {os.getpid(), os.getppid()}
        or current.process_group in protected_groups
        or current.session_id in protected_groups
        or current.executable != launcher_executable
    ):
        raise NodeProcessError("node_process_identity_invalid")
    return _PreExecOwnership(
        pid=current.pid,
        parent_pid=current.parent_pid,
        process_group=current.process_group,
        session_id=current.session_id,
        start_token=current.start_token,
        launcher_executable=current.executable,
    )


def _identity_matches_pre_exec_ownership(
    current: _ProcessIdentity,
    ownership: _PreExecOwnership,
    target_executable: _ExecutableIdentity,
) -> bool:
    return (
        current.pid == ownership.pid
        and current.parent_pid == ownership.parent_pid
        and current.process_group == ownership.process_group
        and current.session_id == ownership.session_id
        and current.start_token == ownership.start_token
        and current.executable
        in {ownership.launcher_executable, target_executable}
    )


def _inventory_linux_process_group(
    process_group: int,
    deadline: float,
) -> tuple[_ProcessGroupMember, ...]:
    members: list[_ProcessGroupMember] = []
    for candidate in Path("/proc").iterdir():
        _deadline_remaining(deadline)
        if not candidate.name.isdigit():
            continue
        try:
            raw = (candidate / "stat").read_text(encoding="ascii")
            close_paren = raw.rfind(")")
            fields = raw[close_paren + 2 :].split()
            if close_paren < 0 or len(fields) < 4:
                continue
            candidate_group = int(fields[2])
            session_id = int(fields[3])
            pid = int(candidate.name)
        except (FileNotFoundError, PermissionError, ProcessLookupError, ValueError):
            continue
        if candidate_group == process_group:
            members.append(
                _ProcessGroupMember(
                    pid=pid,
                    process_group=candidate_group,
                    session_id=session_id,
                )
            )
    return tuple(members)


def _inventory_darwin_process_group(
    process_group: int,
    deadline: float,
) -> tuple[_ProcessGroupMember, ...]:
    output = subprocess.check_output(
        ["/bin/ps", "-axo", "pid=,pgid=,state="],
        text=True,
        timeout=min(1.0, _deadline_remaining(deadline)),
    )
    members: list[_ProcessGroupMember] = []
    for line in output.splitlines():
        _deadline_remaining(deadline)
        fields = line.split()
        if len(fields) < 3:
            continue
        try:
            pid = int(fields[0])
            candidate_group = int(fields[1])
        except ValueError:
            continue
        if candidate_group != process_group:
            continue
        try:
            session_id = os.getsid(pid)
        except ProcessLookupError:
            continue
        members.append(
            _ProcessGroupMember(
                pid=pid,
                process_group=candidate_group,
                session_id=session_id,
            )
        )
    return tuple(members)


def _inventory_process_group(
    process_group: int,
    deadline: float,
) -> tuple[_ProcessGroupMember, ...]:
    if (
        not isinstance(process_group, int)
        or isinstance(process_group, bool)
        or process_group <= 1
    ):
        raise ProcessLookupError(process_group)
    if sys.platform == "linux":
        return _inventory_linux_process_group(process_group, deadline)
    if sys.platform == "darwin":
        return _inventory_darwin_process_group(process_group, deadline)
    raise ProcessLookupError(process_group)


def _canonical_json_loads(raw: bytes) -> Any:
    def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        document: dict[str, Any] = {}
        for key, value in pairs:
            if key in document:
                raise ValueError("duplicate JSON key")
            document[key] = value
        return document

    def reject_constant(value: str) -> None:
        raise ValueError(f"non-finite JSON value: {value}")

    try:
        document = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=reject_duplicates,
            parse_constant=reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("invalid canonical JSON") from exc
    if canonical_json_bytes(document) != raw:
        raise ValueError("non-canonical JSON")
    return document


def _required_identifier(value: Any, field: str) -> str:
    if not isinstance(value, str) or not _IDENTIFIER_RE.fullmatch(value):
        raise ValueError(f"{field} is invalid")
    return value


def _positive_seconds(value: Any, field: str) -> float:
    if (
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or not math.isfinite(value)
        or value <= 0
    ):
        raise ValueError(f"{field} must be finite and positive")
    return float(value)


def _existing_path(value: str | Path, field: str, *, directory: bool) -> Path:
    try:
        path = Path(value).expanduser().resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ValueError(f"{field} is unavailable") from exc
    if directory and not path.is_dir():
        raise ValueError(f"{field} must be a directory")
    if not directory and not path.is_file():
        raise ValueError(f"{field} must be a regular file")
    return path


def validate_physical_node_launch_shape(
    *,
    python_executable: str | Path,
    service_script: str | Path,
    run_id: str,
    deployment_id: str,
    node_id: str,
    sidecar_binary: str | Path,
    sidecar_local_only: bool,
    command_timeout_seconds: float = 30.0,
) -> tuple[_ExecutableIdentity, _ExecutableIdentity, _ExecutableIdentity]:
    """Apply the same side-effect-free launch validators used by real startup."""

    python_identity = capture_executable_identity(python_executable)
    service_identity = capture_executable_identity(
        service_script,
        require_executable=False,
    )
    sidecar_identity = capture_executable_identity(
        sidecar_binary,
        require_canonical=True,
        require_private_owner=True,
    )
    _required_identifier(run_id, "run_id")
    _required_identifier(deployment_id, "deployment_id")
    _required_identifier(node_id, "node_id")
    _positive_seconds(command_timeout_seconds, "command_timeout_seconds")
    if not isinstance(sidecar_local_only, bool):
        raise ValueError("sidecar_local_only must be a boolean")
    return python_identity, service_identity, sidecar_identity


def build_physical_node_command(
    *,
    python_executable: str | Path,
    service_script: str | Path,
    run_id: str,
    deployment_id: str,
    node_id: str,
    artifact_root: str | Path,
    socket_root: str | Path,
    sidecar_binary: str | Path,
    sidecar_local_only: bool,
    command_timeout_seconds: float = 30.0,
    descriptor_relative_artifact_root: bool = False,
) -> tuple[str, ...]:
    """Build an argv-only physical-node launch command with no secret material."""

    python_path = _existing_path(
        python_executable, "python_executable", directory=False
    )
    service_path = _existing_path(service_script, "service_script", directory=False)
    artifacts = _existing_path(artifact_root, "artifact_root", directory=True)
    sockets = _existing_path(socket_root, "socket_root", directory=True)
    sidecar = _existing_path(sidecar_binary, "sidecar_binary", directory=False)
    run_id = _required_identifier(run_id, "run_id")
    deployment_id = _required_identifier(deployment_id, "deployment_id")
    node_id = _required_identifier(node_id, "node_id")
    timeout = _positive_seconds(command_timeout_seconds, "command_timeout_seconds")
    if not isinstance(sidecar_local_only, bool):
        raise ValueError("sidecar_local_only must be a boolean")
    if not isinstance(descriptor_relative_artifact_root, bool):
        raise ValueError("descriptor_relative_artifact_root must be a boolean")
    artifact_argument = artifacts
    if descriptor_relative_artifact_root:
        supplied_artifacts = Path(artifact_root)
        if (
            supplied_artifacts.is_absolute()
            or len(supplied_artifacts.parts) != 1
            or supplied_artifacts.parts[0] in {"", ".", ".."}
        ):
            raise ValueError("artifact_root must be descriptor-relative")
        artifact_argument = supplied_artifacts

    command = [
        str(python_path),
        str(service_path),
        "--run-id",
        run_id,
        "--deployment-id",
        deployment_id,
        "--node-id",
        node_id,
        "--artifact-root",
        str(artifact_argument),
        "--socket-root",
        str(sockets),
        "--sidecar-binary",
        str(sidecar),
        "--command-timeout",
        str(timeout),
    ]
    if sidecar_local_only:
        command.append("--sidecar-local-only")
    return tuple(command)


class PhysicalNodeProcess:
    """One strictly ordered request/response client for ``PhysicalNodeService``."""

    def __init__(
        self,
        *,
        command: Sequence[str],
        node_id: str,
        run_id: str,
        deployment_id: str,
        response_timeout_seconds: float = 35.0,
        shutdown_timeout_seconds: float = 5.0,
        max_frame_bytes: int = MAX_CONTROL_FRAME_BYTES,
        expected_executables: Sequence[_ExecutableIdentity] = (),
    ) -> None:
        if (
            not isinstance(command, Sequence)
            or isinstance(command, (str, bytes))
            or not command
            or not all(isinstance(item, str) and item for item in command)
        ):
            raise ValueError("command must be a non-empty argv sequence")
        self.node_id = _required_identifier(node_id, "node_id")
        self.run_id = _required_identifier(run_id, "run_id")
        self.deployment_id = _required_identifier(deployment_id, "deployment_id")
        self.response_timeout_seconds = _positive_seconds(
            response_timeout_seconds, "response_timeout_seconds"
        )
        self.shutdown_timeout_seconds = _positive_seconds(
            shutdown_timeout_seconds, "shutdown_timeout_seconds"
        )
        if (
            not isinstance(max_frame_bytes, int)
            or isinstance(max_frame_bytes, bool)
            or max_frame_bytes <= 0
            or max_frame_bytes > MAX_CONTROL_FRAME_BYTES
        ):
            raise ValueError("max_frame_bytes is invalid")
        self.max_frame_bytes = max_frame_bytes
        self._command = tuple(command)
        # A4: only a single canonical frame write is serialized. Responses are
        # correlated to bounded per-command waiters by the dedicated reader.
        self._write_lock = threading.Lock()
        self._waiters_lock = threading.RLock()
        self._close_lock = threading.Lock()
        self._waiters: dict[
            str,
            Queue[bytes | object | _ReaderError | _CommandInterrupted],
        ] = {}
        self._retired_command_ids: OrderedDict[str, None] = OrderedDict()
        self._state_lock = threading.RLock()
        self._closed = False
        self._cleanup_complete = False
        self._stderr_lock = threading.Lock()
        self._stderr_chunks: deque[bytes] = deque()
        self._stderr_size = 0
        if (
            not isinstance(expected_executables, Sequence)
            or isinstance(expected_executables, (str, bytes))
            or not all(
                isinstance(identity, _ExecutableIdentity)
                for identity in expected_executables
            )
        ):
            raise ValueError("expected_executables is invalid")
        command_identity = capture_executable_identity(self._command[0])
        launch_command = self._command
        if (
            len(self._command) > 1
            and Path(self._command[1]).name == "physical_inference_node.py"
        ):
            try:
                physical_interpreter = physical_service_interpreter_identity()
            except (OSError, RuntimeError, TypeError, ValueError):
                pass
            else:
                command_identity = physical_interpreter
                launch_command = (physical_interpreter.path, *self._command[1:])
        try:
            current_interpreter = capture_executable_identity(sys.executable)
        except (OSError, RuntimeError, TypeError, ValueError):
            current_interpreter = None
        if launch_command == self._command and current_interpreter == command_identity:
            if sys.platform == "darwin" and sys.prefix == sys.base_prefix:
                running_interpreter = _inventory_process(os.getpid()).executable
                command_identity = running_interpreter
                launch_command = (running_interpreter.path, *self._command[1:])
            else:
                launch_command = (sys.executable, *self._command[1:])
        elif launch_command == self._command:
            # Execute the exact PATH-resolved object that was fingerprinted.
            # Leaving a bare command name here would allow PATH resolution to
            # diverge between validation and the descriptor-bound launch.
            launch_command = (command_identity.path, *self._command[1:])
        try:
            launcher_identity = _inventory_process(os.getpid()).executable
        except (OSError, RuntimeError, TypeError, ValueError):
            launcher_identity = capture_executable_identity(sys.executable)
        identities = tuple(expected_executables)
        if command_identity not in identities:
            identities = (command_identity, *identities)
        if not all(
            revalidate_executable_identity(identity)
            for identity in (launcher_identity, *identities)
        ):
            raise NodeProcessError("node_process_executable_changed")
        self._target_executable_identity = command_identity
        self._launcher_executable_identity = launcher_identity
        self._pre_exec_ownership: _PreExecOwnership | None = None
        self._launch_identity: _ProcessIdentity | None = None
        self._stdout_thread: threading.Thread | None = None
        self._stderr_thread: threading.Thread | None = None
        try:
            handshake = _handshake_pipes()
        except Exception as exc:
            self._closed = True
            raise NodeProcessError("node_process_start_failed") from exc
        try:
            ready_write = _handshake_descriptor(handshake, "ready_write")
            release_read = _handshake_descriptor(handshake, "release_read")
            exec_write = _handshake_descriptor(handshake, "exec_write")
            launcher_command = (
                launcher_identity.path,
                "-I",
                "-S",
                "-c",
                _LAUNCHER_SOURCE,
                str(ready_write),
                str(release_read),
                str(exec_write),
                *launch_command,
            )
            self._process = subprocess.Popen(
                launcher_command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=0,
                shell=False,
                close_fds=True,
                pass_fds=(ready_write, release_read, exec_write),
                start_new_session=True,
            )
        except Exception as exc:
            for name in tuple(handshake):
                _close_handshake_descriptor(handshake, name)
            self._closed = True
            raise NodeProcessError("node_process_start_failed") from exc
        try:
            startup_deadline = time.monotonic() + _LAUNCH_HANDSHAKE_SECONDS
            _close_handshake_descriptor(handshake, "ready_write")
            _close_handshake_descriptor(handshake, "release_read")
            _close_handshake_descriptor(handshake, "exec_write")
            ready_read = _handshake_descriptor(handshake, "ready_read")
            release_write = _handshake_descriptor(handshake, "release_write")
            exec_read = _handshake_descriptor(handshake, "exec_read")
            _await_pre_exec_launcher(
                ready_read,
                self._process,
                startup_deadline,
            )
            _close_handshake_descriptor(handshake, "ready_read")
            self._pre_exec_ownership = _capture_pre_exec_ownership(
                self._process,
                launcher_identity,
                startup_deadline,
            )
            _release_pre_exec_launcher(release_write)
            _close_handshake_descriptor(handshake, "release_write")
            _await_target_exec(
                exec_read,
                self._process,
                startup_deadline,
            )
            _close_handshake_descriptor(handshake, "exec_read")
            launch = _inventory_process(self._process.pid)
            if (
                not _identity_matches_pre_exec_ownership(
                    launch,
                    self._pre_exec_ownership,
                    command_identity,
                )
                or launch.executable != command_identity
            ):
                raise NodeProcessError("node_process_identity_invalid")
            self._launch_identity = launch
            self._stdout_thread = threading.Thread(
                target=self._read_stdout,
                name=f"mycelium-node-stdout-{self.node_id}",
                daemon=False,
            )
            self._stderr_thread = threading.Thread(
                target=self._read_stderr,
                name=f"mycelium-node-stderr-{self.node_id}",
                daemon=False,
            )
            self._stdout_thread.start()
            self._stderr_thread.start()
        except Exception as exc:
            if self._pre_exec_ownership is None:
                try:
                    self._pre_exec_ownership = _capture_pre_exec_ownership(
                        self._process,
                        launcher_identity,
                        time.monotonic() + self.shutdown_timeout_seconds,
                    )
                except Exception:
                    pass
            self._closed = True
            cleaned = self._cleanup_resources(
                time.monotonic() + self.shutdown_timeout_seconds,
                terminate=True,
            )
            code = (
                "node_process_start_failed"
                if cleaned
                else "node_process_cleanup_failed"
            )
            raise NodeProcessError(code) from exc
        finally:
            for name in tuple(handshake):
                _close_handshake_descriptor(handshake, name)

    @property
    def pid(self) -> int:
        return self._process.pid

    @property
    def running(self) -> bool:
        with self._state_lock:
            return not self._closed and self._process.poll() is None

    @property
    def returncode(self) -> int | None:
        return self._process.poll()

    @property
    def stderr_tail(self) -> str:
        with self._stderr_lock:
            return b"".join(self._stderr_chunks).decode("utf-8", errors="replace")

    def _fail_waiters(self, item: object | _ReaderError) -> None:
        with self._waiters_lock:
            waiters = tuple(self._waiters.values())
        for waiter in waiters:
            try:
                waiter.put_nowait(item)
            except Exception:
                continue

    def _deliver_response(self, raw: bytes) -> bool:
        try:
            document = _canonical_json_loads(raw)
        except (TypeError, ValueError):
            self._fail_waiters(_ReaderError("invalid_node_response"))
            return False
        command_id = document.get("command_id") if isinstance(document, dict) else None
        if not isinstance(command_id, str) or not command_id:
            self._fail_waiters(_ReaderError("invalid_node_response"))
            return False
        with self._waiters_lock:
            waiter = self._waiters.get(command_id)
        if waiter is None:
            # A timed-out command may still finish cooperatively.  Its late
            # result is fenced by command ID and can be discarded without
            # failing unrelated waiters or stopping this shared response loop.
            with self._waiters_lock:
                retired = command_id in self._retired_command_ids
            if retired:
                return True
            # A genuinely unsolicited command ID is still a protocol violation.
            self._fail_waiters(_ReaderError("response_command_mismatch"))
            return False
        try:
            waiter.put_nowait(raw)
        except Exception:
            self._fail_waiters(_ReaderError("invalid_node_response"))
            return False
        return True

    def _read_stdout(self) -> None:
        stream = self._process.stdout
        assert stream is not None
        try:
            while True:
                line = stream.readline(self.max_frame_bytes + 2)
                if not line:
                    self._fail_waiters(_EOF)
                    return
                if len(line) > self.max_frame_bytes + 1 or not line.endswith(b"\n"):
                    self._fail_waiters(_ReaderError("invalid_node_response_frame"))
                    return
                if not self._deliver_response(line[:-1]):
                    return
        except (OSError, ValueError):
            self._fail_waiters(_ReaderError("node_response_read_failed"))

    def _read_stderr(self) -> None:
        stream = self._process.stderr
        assert stream is not None
        try:
            while True:
                chunk = stream.read(4096)
                if not chunk:
                    return
                with self._stderr_lock:
                    self._stderr_chunks.append(chunk)
                    self._stderr_size += len(chunk)
                    while self._stderr_size > MAX_STDERR_BYTES and self._stderr_chunks:
                        excess = self._stderr_size - MAX_STDERR_BYTES
                        first = self._stderr_chunks[0]
                        if len(first) <= excess:
                            self._stderr_chunks.popleft()
                            self._stderr_size -= len(first)
                        else:
                            self._stderr_chunks[0] = first[excess:]
                            self._stderr_size -= excess
        except (OSError, ValueError):
            return

    def _signal_process_group(
        self,
        signum: int,
        deadline: float | None = None,
    ) -> bool:
        process = self._process
        ownership = getattr(self, "_pre_exec_ownership", None)
        launch = getattr(self, "_launch_identity", None)
        if ownership is None and isinstance(launch, _ProcessIdentity):
            ownership = _PreExecOwnership(
                pid=launch.pid,
                parent_pid=launch.parent_pid,
                process_group=launch.process_group,
                session_id=launch.session_id,
                start_token=launch.start_token,
                launcher_executable=launch.executable,
            )
        if not isinstance(ownership, _PreExecOwnership):
            return False
        target_executable = getattr(
            self,
            "_target_executable_identity",
            ownership.launcher_executable,
        )
        if deadline is None:
            deadline = time.monotonic() + self.shutdown_timeout_seconds
        try:
            protected_groups = _protected_process_groups(deadline)
            stopped = process.poll() is not None
            _deadline_remaining(deadline)
            members: tuple[_ProcessGroupMember, ...] | None = None
            if stopped:
                members = _inventory_process_group(
                    ownership.process_group,
                    deadline,
                )
            else:
                try:
                    current = _inventory_process(process.pid)
                except (
                    OSError,
                    RuntimeError,
                    subprocess.SubprocessError,
                    ValueError,
                ):
                    members = _inventory_process_group(
                        ownership.process_group,
                        deadline,
                    )
                else:
                    _deadline_remaining(deadline)
                    if (
                        isinstance(launch, _ProcessIdentity)
                        and current != launch
                        or not _identity_matches_pre_exec_ownership(
                            current,
                            ownership,
                            target_executable,
                        )
                    ):
                        return False
            if members is not None:
                _deadline_remaining(deadline)
                if not members:
                    return True
                if any(
                    member.process_group != ownership.process_group
                    or member.session_id != ownership.session_id
                    or member.pid in {os.getpid(), os.getppid()}
                    for member in members
                ):
                    return False
        except (
            OSError,
            RuntimeError,
            subprocess.SubprocessError,
            TimeoutError,
            ValueError,
        ):
            return False
        if (
            ownership.pid <= 1
            or ownership.parent_pid <= 1
            or ownership.process_group <= 1
            or ownership.session_id <= 1
            or ownership.pid in {os.getpid(), os.getppid()}
            or ownership.parent_pid != os.getpid()
            or ownership.process_group != ownership.pid
            or ownership.session_id != ownership.pid
            or ownership.process_group in protected_groups
            or ownership.session_id in protected_groups
        ):
            return False
        try:
            os.killpg(ownership.process_group, signum)
        except ProcessLookupError:
            return False
        except OSError:
            return False
        return True

    def _owned_group_state(self, deadline: float) -> str:
        ownership = getattr(self, "_pre_exec_ownership", None)
        launch = getattr(self, "_launch_identity", None)
        if ownership is None and isinstance(launch, _ProcessIdentity):
            ownership = _PreExecOwnership(
                pid=launch.pid,
                parent_pid=launch.parent_pid,
                process_group=launch.process_group,
                session_id=launch.session_id,
                start_token=launch.start_token,
                launcher_executable=launch.executable,
            )
        if not isinstance(ownership, _PreExecOwnership):
            return "unsafe"
        target_executable = getattr(
            self,
            "_target_executable_identity",
            ownership.launcher_executable,
        )
        try:
            protected_groups = _protected_process_groups(deadline)
            stopped = self._process.poll() is not None
            _deadline_remaining(deadline)
            members: tuple[_ProcessGroupMember, ...] | None = None
            if not stopped:
                try:
                    current = _inventory_process(self._process.pid)
                except (
                    OSError,
                    RuntimeError,
                    subprocess.SubprocessError,
                    ValueError,
                ):
                    pass
                else:
                    _deadline_remaining(deadline)
                    if (
                        isinstance(launch, _ProcessIdentity)
                        and current != launch
                        or not _identity_matches_pre_exec_ownership(
                            current,
                            ownership,
                            target_executable,
                        )
                    ):
                        return "unsafe"
            members = _inventory_process_group(ownership.process_group, deadline)
            if not members and not stopped:
                stopped = self._process.poll() is not None
                _deadline_remaining(deadline)
        except (
            OSError,
            RuntimeError,
            subprocess.SubprocessError,
            TimeoutError,
            ValueError,
        ):
            return "unsafe"
        if (
            ownership.pid <= 1
            or ownership.parent_pid != os.getpid()
            or ownership.process_group != ownership.pid
            or ownership.session_id != ownership.pid
            or ownership.pid in {os.getpid(), os.getppid()}
            or ownership.process_group in protected_groups
            or ownership.session_id in protected_groups
            or any(
                member.process_group != ownership.process_group
                or member.session_id != ownership.session_id
                or member.pid in {os.getpid(), os.getppid()}
                for member in members
            )
        ):
            return "unsafe"
        if stopped and not members:
            return "empty"
        return "owned"

    def _wait_owned_group_exit(self, deadline: float) -> bool:
        while True:
            state = self._owned_group_state(deadline)
            if state == "empty":
                return True
            if state == "unsafe":
                return False
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            interval = min(0.02, remaining)
            if self._process.poll() is None:
                try:
                    self._process.wait(timeout=interval)
                except subprocess.TimeoutExpired:
                    continue
                except OSError:
                    pass
            else:
                threading.Event().wait(interval)

    def _terminate_process(self, deadline: float) -> bool:
        term_sent = self._signal_process_group(signal.SIGTERM, deadline)
        remaining = max(0.0, deadline - time.monotonic())
        term_deadline = min(
            deadline,
            time.monotonic() + min(0.25, remaining / 2),
        )
        if term_sent and self._wait_owned_group_exit(term_deadline):
            return True
        if not term_sent and self._owned_group_state(term_deadline) == "empty":
            return True
        if time.monotonic() >= deadline:
            return False
        if not self._signal_process_group(signal.SIGKILL, deadline):
            return self._owned_group_state(deadline) == "empty"
        return self._wait_owned_group_exit(deadline)

    @staticmethod
    def _close_stream(stream: Any) -> bool:
        if stream is None or stream.closed:
            return True
        try:
            stream.close()
        except (OSError, ValueError):
            return False
        return True

    def _join_readers(self, deadline: float) -> bool:
        complete = True
        current = threading.current_thread()
        for reader in (self._stdout_thread, self._stderr_thread):
            if reader is None or reader is current:
                continue
            remaining = max(0.0, deadline - time.monotonic())
            reader.join(timeout=remaining)
            if reader.is_alive():
                complete = False
        return complete

    def _cleanup_resources(self, deadline: float, *, terminate: bool) -> bool:
        complete = True
        complete = self._close_stream(self._process.stdin) and complete
        if terminate:
            graceful_deadline = min(deadline, time.monotonic() + 0.05)
            group_complete = self._wait_owned_group_exit(graceful_deadline)
            if not group_complete:
                group_complete = self._terminate_process(deadline)
        else:
            group_complete = self._owned_group_state(deadline) == "empty"
        complete = group_complete and complete
        complete = self._close_stream(self._process.stdout) and complete
        complete = self._close_stream(self._process.stderr) and complete
        complete = self._join_readers(deadline) and complete
        self._cleanup_complete = complete
        return complete

    def _abort(self) -> None:
        with self._state_lock:
            self._closed = True
        self._cleanup_resources(
            time.monotonic() + self.shutdown_timeout_seconds,
            terminate=True,
        )

    def _validate_response(self, raw: bytes, command_id: str) -> Any:
        try:
            response = _canonical_json_loads(raw)
        except (TypeError, ValueError) as exc:
            raise NodeProcessError("invalid_node_response") from exc
        if not isinstance(response, dict):
            raise NodeProcessError("invalid_node_response")
        if response.get("command_id") != command_id:
            raise NodeProcessError("response_command_mismatch")
        if (
            response.get("protocol") != NODE_CONTROL_PROTOCOL
            or response.get("node_id") != self.node_id
            or response.get("route_ready") is not False
            or not isinstance(response.get("ok"), bool)
        ):
            raise NodeProcessError("invalid_node_response")
        if response["ok"]:
            if set(response) != {
                "protocol",
                "command_id",
                "node_id",
                "ok",
                "route_ready",
                "result",
            }:
                raise NodeProcessError("invalid_node_response")
            return response["result"]
        if set(response) != {
            "protocol",
            "command_id",
            "node_id",
            "ok",
            "route_ready",
            "error",
        }:
            raise NodeProcessError("invalid_node_response")
        error = response.get("error")
        if (
            not isinstance(error, dict)
            or set(error) != {"code"}
            or not isinstance(error.get("code"), str)
            or not _OPERATION_RE.fullmatch(error["code"])
        ):
            raise NodeProcessError("invalid_node_response")
        raise _RemoteNodeError(error["code"])

    def _exchange_frame(
        self,
        *,
        command_id: str,
        frame: bytes,
        timeout: float,
        terminate_on_timeout: bool,
    ) -> Any:
        waiter: Queue[bytes | object | _ReaderError | _CommandInterrupted] = Queue(
            maxsize=1
        )
        with self._waiters_lock:
            if command_id in self._waiters:
                raise NodeProcessError("duplicate_command_id")
            self._waiters[command_id] = waiter
        try:
            stdin = self._process.stdin
            assert stdin is not None
            try:
                with self._write_lock:
                    with self._state_lock:
                        if self._closed:
                            raise NodeProcessError("node_process_closed")
                        if self._process.poll() is not None:
                            self._closed = True
                            raise NodeProcessError("node_process_exited")
                    stdin.write(frame + b"\n")
                    stdin.flush()
            except NodeProcessError:
                raise
            except (BrokenPipeError, OSError, ValueError) as exc:
                self._abort()
                raise NodeProcessError("node_process_unavailable") from exc
            try:
                item = waiter.get(timeout=timeout)
            except Empty as exc:
                if terminate_on_timeout:
                    self._abort()
                raise NodeProcessError("node_response_timeout") from exc
            if item is _EOF:
                self._abort()
                raise NodeProcessError("node_process_exited")
            if isinstance(item, _CommandInterrupted):
                raise NodeProcessError(item.code)
            if isinstance(item, _ReaderError):
                self._abort()
                raise NodeProcessError(item.code)
            assert isinstance(item, bytes)
            try:
                return self._validate_response(item, command_id)
            except _RemoteNodeError:
                raise
            except NodeProcessError:
                self._abort()
                raise
        finally:
            with self._waiters_lock:
                self._waiters.pop(command_id, None)
                self._retired_command_ids[command_id] = None
                self._retired_command_ids.move_to_end(command_id)
                while len(self._retired_command_ids) > 1_024:
                    self._retired_command_ids.popitem(last=False)

    def command(
        self,
        operation: str,
        payload: Mapping[str, Any] | None = None,
        *,
        timeout_seconds: float | None = None,
        terminate_on_timeout: bool = True,
        command_id: str | None = None,
    ) -> Any:
        if not isinstance(operation, str) or not _OPERATION_RE.fullmatch(operation):
            raise ValueError("operation is invalid")
        if payload is None:
            payload_document: dict[str, Any] = {}
        elif not isinstance(payload, Mapping) or not all(
            isinstance(key, str) for key in payload
        ):
            raise ValueError("payload must be a JSON object")
        else:
            payload_document = dict(payload)
        timeout = self.response_timeout_seconds if timeout_seconds is None else _positive_seconds(
            timeout_seconds, "timeout_seconds"
        )
        if not isinstance(terminate_on_timeout, bool):
            raise ValueError("terminate_on_timeout is invalid")
        command_id = (
            str(uuid.uuid4())
            if command_id is None
            else _required_identifier(command_id, "command_id")
        )
        document = {
            "protocol": NODE_CONTROL_PROTOCOL,
            "command_id": command_id,
            "run_id": self.run_id,
            "deployment_id": self.deployment_id,
            "command": operation,
            "payload": payload_document,
        }
        try:
            frame = canonical_json_bytes(document)
        except (TypeError, ValueError) as exc:
            raise ValueError("command fields are not canonical JSON") from exc
        if len(frame) + 1 > self.max_frame_bytes:
            raise NodeProcessError("node_command_too_large")
        return self._exchange_frame(
            command_id=command_id,
            frame=frame,
            timeout=timeout,
            terminate_on_timeout=terminate_on_timeout,
        )

    def interrupt_command(self, command_id: str, *, code: str) -> bool:
        """Retire one correlated waiter without stopping the shared node process."""

        command_id = _required_identifier(command_id, "command_id")
        if not isinstance(code, str) or not _OPERATION_RE.fullmatch(code):
            raise ValueError("interrupt code is invalid")
        with self._waiters_lock:
            waiter = self._waiters.get(command_id)
        if waiter is None:
            return False
        try:
            waiter.put_nowait(_CommandInterrupted(code))
        except Exception:
            return False
        return True

    def close(self) -> None:
        with self._close_lock:
            deadline = time.monotonic() + self.shutdown_timeout_seconds
            with self._state_lock:
                if self._cleanup_complete:
                    return
            if self._process.poll() is None:
                stop_timeout = min(
                    self.response_timeout_seconds,
                    0.5,
                    max(0.0, deadline - time.monotonic()),
                )
                if stop_timeout > 0:
                    try:
                        self._command_stop(timeout=stop_timeout)
                    except NodeProcessError:
                        pass
            with self._state_lock:
                self._closed = True
            self._fail_waiters(_ReaderError("node_process_closed"))
            cleaned = self._cleanup_resources(deadline, terminate=True)
        if not cleaned:
            raise NodeProcessError("node_process_cleanup_failed")

    def _command_stop(self, *, timeout: float | None = None) -> None:
        if self._process.poll() is not None:
            return
        command_id = str(uuid.uuid4())
        frame = canonical_json_bytes(
            {
                "protocol": NODE_CONTROL_PROTOCOL,
                "command_id": command_id,
                "run_id": self.run_id,
                "deployment_id": self.deployment_id,
                "command": "stop",
                "payload": {},
            }
        )
        response_timeout = (
            self.response_timeout_seconds if timeout is None else float(timeout)
        )
        try:
            self._exchange_frame(
                command_id=command_id,
                frame=frame,
                timeout=response_timeout,
                terminate_on_timeout=False,
            )
        except NodeProcessError as exc:
            raise NodeProcessError("node_stop_failed") from exc

    def __enter__(self) -> "PhysicalNodeProcess":
        return self

    def __exit__(self, _type: Any, _value: Any, _traceback: Any) -> None:
        self.close()
