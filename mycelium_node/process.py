# SPDX-License-Identifier: AGPL-3.0-only
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

import mycelium_deadlines
import mycelium_host_platform
from mycelium_host_platform import HostPlatformError
from mycelium_qualification.evidence import canonical_json_bytes


NODE_CONTROL_PROTOCOL = "mycelium.physical_node_control.v1"
CLEANUP_CONTROL_FRAME_PREFIX = b"MYCELIUM_CLEANUP_RESPONSE_V1 "
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
_LAUNCH_HANDSHAKE_SECONDS = mycelium_deadlines.NODE_LAUNCH_HANDSHAKE_SECONDS
#: How long a failed start waits to collect the exit status of a launcher it
#: already saw exit.  A dying process closes its descriptors before its status
#: becomes waitable, so the ready pipe can report EOF while ``poll()`` still
#: has no status; under load that gap is real.  Bounded so a diagnostic never
#: holds a failed start open.
_LAUNCHER_EXIT_STATUS_SECONDS = 0.5
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


#: Closed set of launch steps a ``node_process_start_failed`` can name.  The
#: POSIX launch runs them in this order; ``win32_job`` is the win32 spawn.
NODE_START_FAILURE_PHASES: frozenset[str] = frozenset({
    "handshake_pipes", "popen", "launcher_ready", "pre_exec_ownership",
    "launcher_release", "target_exec", "inventory", "identity",
    "reader_threads", "win32_job", "unknown",
})
#: Closed outcome of the failed step: the launch budget ran out, the launcher
#: (or its exec'd target) was already gone, an identity/ownership check
#: refused, or anything else.
NODE_START_FAILURE_KINDS: frozenset[str] = frozenset({
    "timeout", "exited", "identity_invalid", "error",
})
_LAUNCH_TIMING_PHASES: tuple[str, ...] = (
    "launcher_ready", "pre_exec_ownership", "launcher_release", "target_exec",
    "inventory", "identity",
)
_EXCEPTION_TYPE_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
_INNER_CODE_RE = re.compile(r"^node_process_[a-z_]{1,48}$")
_MAX_DIAGNOSTIC_MS = 3_600_000


def _bounded_ms(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(value):
        return None
    return max(0, min(int(value), _MAX_DIAGNOSTIC_MS))


@dataclass(frozen=True)
class NodeStartFailure:
    """Bounded cause of one failed node launch.

    Closed codes and small integers only: no path, argv, environment,
    exception message, pid or identifier, so the record can be persisted next
    to the existing private diagnostics and its ``public_reason`` can travel in
    a member report.  ``elapsed_ms`` and every ``phase_ms`` mark are measured
    from the moment the launch budget (``budget_ms``) started, i.e. right
    after ``Popen`` returned; ``popen_ms`` is the spawn itself, which the
    budget does not cover.  ``launcher_reaped`` says whether the launcher's
    exit status was collected when the record was made: ``False`` means
    ``launcher_returncode`` is unknown (not "no exit"), ``None`` that no
    launcher was spawned or the caller did not say.  ``identity_changed`` is
    ``True`` when the spawned, still unreaped child was seen running an image
    other than the pinned one (the launch check refused it, or cleanup
    stopped it anyway), ``None`` when no child was spawned or the caller did
    not say.
    """

    phase: str
    kind: str
    elapsed_ms: int | None
    budget_ms: int
    popen_ms: int | None
    exception_type: str
    errno: int | None
    inner_code: str | None
    launcher_exited: bool | None
    launcher_returncode: int | None
    launcher_reaped: bool | None
    identity_changed: bool | None
    phase_ms: tuple[tuple[str, int], ...]

    @classmethod
    def from_exception(
        cls,
        *,
        phase: str,
        exception: BaseException,
        started: float | None,
        budget_seconds: float,
        popen_ms: Any,
        phase_ms: Mapping[str, Any],
        launcher_returncode: Any,
        launcher_exited: bool | None,
        failed_at: float | None = None,
        launcher_reaped: bool | None = None,
        identity_changed: bool | None = None,
    ) -> "NodeStartFailure":
        if phase not in NODE_START_FAILURE_PHASES:
            phase = "unknown"
        if failed_at is None:
            failed_at = time.monotonic()
        code = getattr(exception, "code", None)
        inner_code = (
            code
            if isinstance(exception, NodeProcessError)
            and isinstance(code, str)
            and _INNER_CODE_RE.fullmatch(code)
            else None
        )
        if inner_code == "node_process_identity_invalid":
            kind = "identity_invalid"
        elif isinstance(exception, _HandshakeClosed):
            kind = "exited"
        elif isinstance(exception, (TimeoutError, subprocess.TimeoutExpired)):
            kind = "timeout"
        elif launcher_exited:
            kind = "exited"
        else:
            kind = "error"
        exception_type = type(exception).__name__
        if not _EXCEPTION_TYPE_RE.fullmatch(exception_type):
            exception_type = "Exception"
        error_number = getattr(exception, "errno", None)
        if (
            isinstance(error_number, bool)
            or not isinstance(error_number, int)
            or not 0 < error_number <= 4096
        ):
            error_number = None
        if (
            isinstance(launcher_returncode, bool)
            or not isinstance(launcher_returncode, int)
            or not -255 <= launcher_returncode <= 255
        ):
            launcher_returncode = None
        marks = tuple(
            (name, bounded)
            for name in _LAUNCH_TIMING_PHASES
            if name in phase_ms
            and (bounded := _bounded_ms(phase_ms[name])) is not None
        )
        return cls(
            phase=phase,
            kind=kind,
            elapsed_ms=(
                None if started is None
                else _bounded_ms((failed_at - started) * 1000)
            ),
            budget_ms=int(budget_seconds * 1000),
            popen_ms=_bounded_ms(popen_ms),
            exception_type=exception_type,
            errno=error_number,
            inner_code=inner_code,
            launcher_exited=(
                True if kind == "exited"
                else None if launcher_exited is None
                else bool(launcher_exited)
            ),
            launcher_returncode=launcher_returncode,
            launcher_reaped=(
                launcher_reaped if isinstance(launcher_reaped, bool) else None
            ),
            identity_changed=(
                identity_changed if isinstance(identity_changed, bool) else None
            ),
            phase_ms=marks,
        )

    @property
    def public_reason(self) -> str:
        """Closed ``<phase>_<kind>`` token, safe for a member report."""

        return f"{self.phase}_{self.kind}"

    def as_record(self) -> dict[str, Any]:
        return {
            "phase": self.phase,
            "kind": self.kind,
            "elapsed_ms": self.elapsed_ms,
            "budget_ms": self.budget_ms,
            "popen_ms": self.popen_ms,
            "exception_type": self.exception_type,
            "errno": self.errno,
            "inner_code": self.inner_code,
            "launcher_exited": self.launcher_exited,
            "launcher_returncode": self.launcher_returncode,
            "launcher_reaped": self.launcher_reaped,
            "identity_changed": self.identity_changed,
            "phase_ms": dict(self.phase_ms),
        }


class NodeProcessError(RuntimeError):
    """Stable, fail-closed node-process failure."""

    def __init__(
        self, code: str, detail: str = "", *,
        cleanup_owner: "PhysicalNodeProcess | None" = None,
        start_failure: NodeStartFailure | None = None,
    ) -> None:
        self.code = code
        self.detail = detail
        # Preserve incarnation-bound teardown when a failed constructor cannot
        # prove that the child it spawned has gone.
        self.cleanup_owner = cleanup_owner
        # Bounded cause of a failed launch.  Never part of ``code`` or the
        # message: callers compare both, and the cause is diagnostics only.
        self.start_failure = start_failure
        super().__init__(code if not detail else f"{code}:{detail}")

    @property
    def public_code(self) -> str:
        """``code``, suffixed with the closed launch cause when one is known.

        E.g. ``node_process_start_failed_launcher_ready_timeout``.  It still
        matches the ``[a-z][a-z0-9_]{0,95}`` member-report code contract.
        """

        failure = self.start_failure
        if failure is None:
            return self.code
        return f"{self.code}_{failure.public_reason}"


class _RemoteNodeError(NodeProcessError):
    pass


class _ControlFrameWriteTimeout(NodeProcessError):
    def __init__(self, *, partial: bool) -> None:
        self.partial = partial
        super().__init__("node_response_timeout")


def _win32_write_control_frame_before(
    stream: Any, payload: bytes, deadline: float
) -> None:
    """win32 anonymous pipes cannot be selected: write on a bounded thread.

    The write progress of a thread that misses the deadline is unknown, so a
    timeout is always reported as partial and the caller fails closed (the
    job is killed, which breaks the pipe and releases the thread).
    """

    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise _ControlFrameWriteTimeout(partial=False)
    done = threading.Event()
    failures: list[BaseException] = []

    def write() -> None:
        try:
            descriptor = stream.fileno()
            offset = 0
            while offset < len(payload):
                written = os.write(descriptor, payload[offset:])
                if written <= 0:
                    raise BrokenPipeError("node control pipe write failed")
                offset += written
        except BaseException as exc:  # noqa: BLE001 - reported to the owner
            failures.append(exc)
        finally:
            done.set()

    writer = threading.Thread(
        target=write, name="mycelium-node-control-writer", daemon=True
    )
    writer.start()
    if not done.wait(remaining):
        raise _ControlFrameWriteTimeout(partial=True)
    if failures:
        failure = failures[0]
        if isinstance(failure, (OSError, ValueError)):
            raise failure
        raise BrokenPipeError("node control pipe write failed") from failure


def _write_control_frame_before(stream: Any, payload: bytes, deadline: float) -> None:
    """Write one owned JSONL frame without exceeding its command deadline."""

    if mycelium_host_platform.is_win32():
        return _win32_write_control_frame_before(stream, payload, deadline)
    descriptor = stream.fileno()
    os.set_blocking(descriptor, False)
    offset = 0
    selector = selectors.DefaultSelector()
    try:
        selector.register(descriptor, selectors.EVENT_WRITE)
        while offset < len(payload):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise _ControlFrameWriteTimeout(partial=offset > 0)
            if not selector.select(timeout=remaining):
                raise _ControlFrameWriteTimeout(partial=offset > 0)
            try:
                written = os.write(descriptor, payload[offset:])
            except (BlockingIOError, InterruptedError):
                continue
            if written <= 0:
                raise BrokenPipeError("node control pipe write failed")
            offset += written
    finally:
        selector.close()


_STDERR_TAIL_CHARS = 4_000


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


class TransientIdentityObservation(ProcessLookupError):
    """An owned incarnation was not stably observable across two snapshots.

    Darwin reports one identity per process, but a process legitimately replaces
    its own image (the pinned launcher exec's the interpreter, and a framework
    interpreter may exec its own stub).  While that replacement is in flight the
    two snapshots taken by ``_inventory_darwin_process`` can disagree about the
    executable, and the second one can even fail outright.

    That disagreement is *not* evidence that the incarnation is gone, so it must
    not be reported as an unconditional absence.  This subclass keeps every
    existing fail-closed caller unchanged (it IS a ``ProcessLookupError``) while
    letting an owner that still holds the child unreaped retry the observation
    inside its own original bounded deadline instead of treating a legitimate
    exec transition as a vanished pid.
    """


def _absolute_path(value: str | Path) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    return Path(os.path.abspath(path))


def _root_owned_sticky(metadata: os.stat_result) -> bool:
    """Return whether one walk component is a trusted root-owned sticky directory.

    ``/tmp``-style roots (mode 1777) are the single world-writable directory a
    walk may accept: the sticky bit stops other users renaming or replacing an
    existing entry, which is the escape the world-writable check defends
    against.  Every walk variant must decide this here, so the strict and the
    lenient walks cannot disagree about the same component again.
    """

    return metadata.st_uid == 0 and bool(metadata.st_mode & stat.S_ISVTX)


def _validate_walk_component(metadata: os.stat_result) -> None:
    if not stat.S_ISDIR(metadata.st_mode):
        raise ValueError("path component is invalid")
    if metadata.st_uid not in {0, os.getuid()}:
        raise ValueError("path component is invalid")
    writable_by_others = stat.S_IMODE(metadata.st_mode) & 0o022
    if writable_by_others and not _root_owned_sticky(metadata):
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


class _Win32PrivateDirectoryLease:
    """win32 lease: the same interface as ``PrivateDirectoryLease`` over handles.

    The directory and its parent are held open without ``FILE_SHARE_DELETE``
    (so neither can be renamed or removed while leased) and every
    revalidation re-checks handle identity, the final path and the owner-only
    DACL through :mod:`mycelium_host_platform`.  ``working_directory`` pins
    the process cwd by path; the held handle keeps that path bound to the
    leased directory for the whole block.
    """

    def __init__(self, path: Path, lease: Any) -> None:
        self.path = path
        self._lease = lease
        self._closed = False

    @property
    def exists(self) -> bool:
        return bool(self._lease.exists) and not self._closed

    def _require_open(self) -> None:
        if self._closed or not self._lease.exists:
            raise ValueError("data directory is unavailable")

    def revalidate(self) -> None:
        self._require_open()
        try:
            self._lease.revalidate()
        except HostPlatformError as exc:
            raise ValueError("data directory binding changed") from exc

    def private_subdirectory(self, name: str) -> "_Win32PrivateDirectoryLease":
        name = PrivateDirectoryLease._relative_component(name)
        self.revalidate()
        try:
            child = self._lease.subdirectory(name)
        except HostPlatformError as exc:
            raise ValueError("data directory is unavailable") from exc
        lease = _Win32PrivateDirectoryLease(self.path / name, child)
        try:
            self.revalidate()
            lease.revalidate()
        except BaseException:
            lease.close()
            raise
        return lease

    @contextmanager
    def working_directory(self) -> Iterator[None]:
        self._require_open()
        with _CWD_LEASE_LOCK:
            original = os.getcwd()
            self.revalidate()
            os.chdir(self.path)
            body_failure: BaseException | None = None
            try:
                yield
            except BaseException as exc:
                body_failure = exc
                raise
            finally:
                restored = False
                for _attempt in range(_CWD_RESTORE_ATTEMPTS):
                    try:
                        os.chdir(original)
                    except OSError:
                        continue
                    restored = True
                    break
                if not restored:
                    if body_failure is not None:
                        body_failure.add_note(_CWD_RESTORATION_ERROR)
                    else:
                        raise ValueError(_CWD_RESTORATION_ERROR)
                try:
                    self.revalidate()
                except ValueError:
                    if body_failure is None:
                        raise

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._lease.close()
        except HostPlatformError as exc:
            raise ValueError("data directory close failed") from exc

    def __enter__(self) -> "_Win32PrivateDirectoryLease":
        if self._closed:
            raise ValueError("data directory is unavailable")
        return self

    def __exit__(self, _type: Any, _value: Any, _traceback: Any) -> None:
        self.close()


def _win32_private_directory_lease(
    value: str | Path, *, create: bool
) -> _Win32PrivateDirectoryLease:
    path = _absolute_path(value)
    if path == Path(path.anchor):
        raise ValueError("data directory is invalid")
    try:
        lease = mycelium_host_platform.private_directory(path, create=create)
    except HostPlatformError as exc:
        if exc.code == "host_permissions_invalid":
            raise ValueError("data directory is invalid") from exc
        raise ValueError("data directory is unavailable") from exc
    return _Win32PrivateDirectoryLease(path, lease)


def private_directory_lease(
    value: str | Path,
    *,
    create: bool = True,
) -> PrivateDirectoryLease:
    """Open a private directory while retaining its parent and final descriptors."""

    if mycelium_host_platform.is_win32():
        return _win32_private_directory_lease(value, create=create)  # type: ignore[return-value]
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


_WIN32_EXECUTABLE_SUFFIXES = frozenset({".exe", ".com"})


def _win32_capture_executable_identity(
    value: str | Path,
    *,
    require_canonical: bool,
    require_private_owner: bool,
    require_executable: bool,
) -> _ExecutableIdentity:
    """Handle-bound executable fingerprint on win32 (reparse points refused)."""

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
    if require_executable and path.suffix.lower() not in _WIN32_EXECUTABLE_SUFFIXES:
        raise ValueError("executable is invalid")
    try:
        fingerprint = mycelium_host_platform.executable_fingerprint(
            path, require_private_owner=require_private_owner
        )
    except HostPlatformError as exc:
        if exc.code in {"host_permissions_invalid", "host_file_invalid", "host_path_invalid"}:
            raise ValueError("executable is invalid") from exc
        raise ValueError("executable is unavailable") from exc
    return _ExecutableIdentity(
        path=str(path),
        device=fingerprint.volume,
        inode=fingerprint.index,
        mode=stat.S_IFREG | 0o700,
        uid=0,
        size=fingerprint.size,
        mtime_ns=fingerprint.last_write_ns,
        ctime_ns=fingerprint.creation_ns,
    )


def capture_executable_identity(
    value: str | Path,
    *,
    require_canonical: bool = False,
    require_private_owner: bool = False,
    require_executable: bool = True,
) -> _ExecutableIdentity:
    """Open and fingerprint one executable without following untrusted symlinks."""

    if mycelium_host_platform.is_win32():
        return _win32_capture_executable_identity(
            value,
            require_canonical=require_canonical,
            require_private_owner=require_private_owner,
            require_executable=require_executable,
        )
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

    if mycelium_host_platform.is_win32():
        # win32 has no directory descriptors; callers dispatch to the host
        # platform seam instead of walking by fd.
        raise ValueError("path is invalid")
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
                    or (
                        stat.S_IMODE(metadata.st_mode) & 0o002
                        and not _root_owned_sticky(metadata)
                    )
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


# macOS order is unchanged from before the cross-platform work: the owner-configured interpreter
# (opt-in), then Apple Silicon Homebrew's python3, then plain ``sys.executable`` as the last fallback.
_MACOS_LEGACY_PYTHON = "/opt/homebrew/bin/python3"
PHYSICAL_SERVICE_PYTHON_ENV = "MYCELIUM_PHYSICAL_SERVICE_PYTHON"


def _macos_framework_image(interpreter: str | Path) -> _ExecutableIdentity:
    """The ``Python.app`` image beside a framework ``bin/pythonX`` (raises if absent)."""
    base = capture_executable_identity(interpreter)
    framework = (
        Path(base.path).parent.parent
        / "Resources"
        / "Python.app"
        / "Contents"
        / "MacOS"
        / "Python"
    )
    return capture_executable_identity(framework)


def physical_service_interpreter_identity(
    *,
    platform: str | None = None,
    executable: str | None = None,
    environ: Mapping[str, str] | None = None,
) -> _ExecutableIdentity:
    environment = os.environ if environ is None else environ
    running = sys.executable if executable is None else executable
    if (sys.platform if platform is None else platform) == "darwin":
        for candidate in (environment.get(PHYSICAL_SERVICE_PYTHON_ENV), _MACOS_LEGACY_PYTHON):
            if not candidate:
                continue
            try:
                return _macos_framework_image(candidate)
            except (OSError, RuntimeError, TypeError, ValueError):
                continue
    return capture_executable_identity(running)


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
        # The incarnation was not stably observable across the two snapshots.
        # Do not claim the pid is gone: an owned child that replaces its own
        # image mid-exec produces exactly this disagreement.  Callers that hold
        # the child unreaped retry this within their own bounded deadline (see
        # ``observe_owned_process_identity``); every other caller keeps its
        # existing fail-closed behaviour through the ProcessLookupError base.
        raise TransientIdentityObservation(pid)
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


class _DarwinBSDShortInfo(ctypes.Structure):
    _fields_ = [
        ("pid", ctypes.c_uint32),
        ("ppid", ctypes.c_uint32),
        ("pgid", ctypes.c_uint32),
        ("status", ctypes.c_uint32),
        ("comm", ctypes.c_char * 16),
        ("flags", ctypes.c_uint32),
        ("uid", ctypes.c_uint32),
        ("gid", ctypes.c_uint32),
        ("ruid", ctypes.c_uint32),
        ("rgid", ctypes.c_uint32),
        ("svuid", ctypes.c_uint32),
        ("svgid", ctypes.c_uint32),
        ("reserved", ctypes.c_uint32),
    ]


def _darwin_short_parent_and_group(pid: int) -> tuple[int, int] | None:
    """Parent and group of ``pid`` via PROC_PIDT_SHORTBSDINFO, without a fork.

    PROC_PIDTBSDINFO refuses processes of another user (a root-owned
    ``login`` above a Terminal shell); the short flavour does not.
    """

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
    information = _DarwinBSDShortInfo()
    size = ctypes.sizeof(information)
    received = proc_pidinfo(pid, 13, 0, ctypes.byref(information), size)
    if received != size or information.pid != pid:
        return None
    return int(information.ppid), int(information.pgid)


def _darwin_parent_and_group(pid: int, deadline: float) -> tuple[int, int]:
    # Cleanup walks the ancestors on every 20 ms poll. Forking /bin/ps for
    # each one the full flavour refuses cost up to the whole bounded budget
    # under process churn, so ps is only the last resort.
    short = _darwin_short_parent_and_group(pid)
    if short is not None:
        return short
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


class _HandshakeClosed(RuntimeError):
    """The launcher closed its ready pipe without signalling: it is gone."""


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
    elif not value:
        raise _HandshakeClosed("launcher readiness failed")
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


def _identity_matches_owned_incarnation(
    current: _ProcessIdentity,
    ownership: _PreExecOwnership,
) -> bool:
    """Same process incarnation as the pinned one, whatever image it runs."""

    return (
        current.pid == ownership.pid
        and current.parent_pid == ownership.parent_pid
        and current.process_group == ownership.process_group
        and current.session_id == ownership.session_id
        and current.start_token == ownership.start_token
    )


def _identity_matches_pre_exec_ownership(
    current: _ProcessIdentity,
    ownership: _PreExecOwnership,
    target_executable: _ExecutableIdentity,
) -> bool:
    return (
        _identity_matches_owned_incarnation(current, ownership)
        and current.executable in {ownership.launcher_executable, target_executable}
    )


def observe_owned_process_identity(
    process: subprocess.Popen[bytes],
    ownership: _PreExecOwnership,
    deadline: float,
    *,
    interval: float = 0.01,
) -> _ProcessIdentity:
    """Observe an owned child that is still in flight, without racing its exec.

    An owned child legitimately replaces its own image while it runs: the pinned
    pre-exec launcher exec's the target, and a framework interpreter may exec its
    own stub afterwards.  During that window ``_inventory_darwin_process`` cannot
    always take two consistent snapshots, and it reports that as a
    ``ProcessLookupError`` (``TransientIdentityObservation``).  For a child this
    owner started, that is not an absence: as long as the child has not been
    reaped the incarnation still exists, so re-observe it instead of concluding
    it is gone.

    Retrying is bounded and cannot hide a real disappearance:

    * the retry stops the moment ``process.poll()`` reports the child reaped --
      then a ``ProcessLookupError`` really does mean the pid is gone (the
      already-reaped case), and it propagates;
    * the retry stops when ``deadline`` expires, so a caller's original bounded
      window is never extended;
    * the returned identity is a complete observation and is NOT validated here.
      The caller must still apply ``_identity_matches_pre_exec_ownership`` (or
      its own checks) to it, so tolerating a transient observation never turns an
      unowned incarnation into an accepted one.
    """

    while True:
        try:
            return _inventory_process(ownership.pid)
        except ProcessLookupError:
            if process.poll() is not None:
                # The child this owner started has been reaped; absence is now
                # the true answer, so never retry past that point.
                raise
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise
            time.sleep(min(interval, remaining))


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
    # ESRCH from a group-wide null signal is the kernel saying no process is
    # in the group: nothing to list, nothing to signal. It needs no fork, so a
    # reaped child's empty group is confirmed even when /bin/ps cannot finish
    # inside the remaining cleanup budget. Any other answer takes the listing.
    try:
        os.kill(-process_group, 0)
    except ProcessLookupError:
        return ()
    except OSError:
        pass
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


def _check_sidecar_lan(sidecar_lan: bool, sidecar_local_only: bool) -> None:
    if not isinstance(sidecar_lan, bool):
        raise ValueError("sidecar_lan must be a boolean")
    if sidecar_lan and sidecar_local_only:
        raise ValueError("sidecar_lan and sidecar_local_only are mutually exclusive")


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
    sidecar_lan: bool = False,
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
    _check_sidecar_lan(sidecar_lan, sidecar_local_only)
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
    sidecar_lan: bool = False,
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
    _check_sidecar_lan(sidecar_lan, sidecar_local_only)
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
    if sidecar_lan:
        command.append("--sidecar-lan")
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
        environment: Mapping[str, str] | None = None,
    ) -> None:
        """``environment`` replaces the inherited environment of the node
        (``None``, the default, inherits the parent's unchanged)."""
        if environment is not None and (
            not isinstance(environment, Mapping)
            or not all(isinstance(k, str) and isinstance(v, str) for k, v in environment.items())
        ):
            raise ValueError("environment must map strings to strings")
        self._environment = None if environment is None else dict(environment)
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
        # Cancellation cleanup has a bounded owner deadline, so its control
        # frames must not sit behind already-queued inference/health writes.
        # The active frame remains atomic; priority only chooses the next
        # writer after the current frame has completed.
        self._write_condition = threading.Condition()
        self._write_active = False
        self._priority_writers_waiting = 0
        self._control_frame_writer = _write_control_frame_before
        self._waiters_lock = threading.RLock()
        self._close_lock = threading.Lock()
        self._waiters: dict[
            str,
            Queue[bytes | object | _ReaderError | _CommandInterrupted],
        ] = {}
        self._pending_interruptions: OrderedDict[str, str] = OrderedDict()
        self._retired_command_ids: OrderedDict[str, None] = OrderedDict()
        self._reader_failure_code: str | None = None
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
        interpreter_shim_identity = None
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
            if sys.platform == "darwin":
                running_interpreter = _inventory_process(os.getpid()).executable
                if sys.prefix != sys.base_prefix and command_identity != running_interpreter:
                    # Homebrew's venv executable is a re-exec shim. Keep its path
                    # (venv site configuration), but require the final observed
                    # executable to be the exact running framework interpreter.
                    interpreter_shim_identity = command_identity
                    launch_command = (sys.executable, *self._command[1:])
                else:
                    launch_command = (running_interpreter.path, *self._command[1:])
                command_identity = running_interpreter
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
        if interpreter_shim_identity is not None and interpreter_shim_identity not in identities:
            identities = (interpreter_shim_identity, *identities)
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
        self._owned_identity_changed = False
        self._stdout_thread: threading.Thread | None = None
        self._stderr_thread: threading.Thread | None = None
        self._win32_job: Any = None
        if mycelium_host_platform.is_win32():
            self._start_win32_job(launch_command, command_identity)
            return
        try:
            handshake = _handshake_pipes()
        except Exception as exc:
            self._closed = True
            raise NodeProcessError(
                "node_process_start_failed",
                start_failure=NodeStartFailure.from_exception(
                    phase="handshake_pipes", exception=exc, started=None,
                    budget_seconds=_LAUNCH_HANDSHAKE_SECONDS, popen_ms=None,
                    phase_ms={}, launcher_returncode=None, launcher_exited=None,
                ),
            ) from exc
        popen_started = time.monotonic()
        try:
            ready_write = _handshake_descriptor(handshake, "ready_write")
            release_read = _handshake_descriptor(handshake, "release_read")
            exec_write = _handshake_descriptor(handshake, "exec_write")
            launcher_command = (
                launcher_identity.path,
                *(("--node-process-launcher",) if getattr(sys, "frozen", False)
                  else ("-I", "-S", "-c", _LAUNCHER_SOURCE)),
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
                # _read_stdout uses bounded newline framing.  Leaving the pipe
                # unbuffered turns FileIO.readline() into raw byte-at-a-time
                # reads; under sustained physical inference, one large JSON
                # response can then leave already-flushed cancellation
                # receipts queued behind seconds of parent-side pipe draining.
                # BufferedReader still enforces max_frame_bytes below, while
                # draining each SSH stdout burst in blocks so command receipt
                # order reaches the correlated waiter inside its original
                # immutable deadline.
                bufsize=-1,
                shell=False,
                close_fds=True,
                pass_fds=(ready_write, release_read, exec_write),
                start_new_session=True,
                env=self._environment,
            )
        except Exception as exc:
            popen_failed_at = time.monotonic()
            for name in tuple(handshake):
                _close_handshake_descriptor(handshake, name)
            self._closed = True
            raise NodeProcessError(
                "node_process_start_failed",
                start_failure=NodeStartFailure.from_exception(
                    phase="popen", exception=exc, started=None,
                    budget_seconds=_LAUNCH_HANDSHAKE_SECONDS,
                    popen_ms=(popen_failed_at - popen_started) * 1000,
                    phase_ms={}, launcher_returncode=None, launcher_exited=None,
                ),
            ) from exc
        # Every launch step below runs inside ONE immutable budget.  ``phase``
        # names the step in flight and ``marks`` the ms (from budget start) at
        # which each earlier step completed, so a failure names its own step
        # and how much of the budget the previous ones used.
        budget_started = time.monotonic()
        popen_ms = (budget_started - popen_started) * 1000
        phase = "launcher_ready"
        marks: dict[str, float] = {}

        def mark(completed: str, following: str) -> str:
            marks[completed] = (time.monotonic() - budget_started) * 1000
            return following

        try:
            startup_deadline = budget_started + _LAUNCH_HANDSHAKE_SECONDS
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
            phase = mark("launcher_ready", "pre_exec_ownership")
            _close_handshake_descriptor(handshake, "ready_read")
            self._pre_exec_ownership = _capture_pre_exec_ownership(
                self._process,
                launcher_identity,
                startup_deadline,
            )
            phase = mark("pre_exec_ownership", "launcher_release")
            _release_pre_exec_launcher(release_write)
            phase = mark("launcher_release", "target_exec")
            _close_handshake_descriptor(handshake, "release_write")
            _await_target_exec(
                exec_read,
                self._process,
                startup_deadline,
            )
            phase = mark("target_exec", "inventory")
            _close_handshake_descriptor(handshake, "exec_read")
            while True:
                try:
                    launch = _inventory_process(self._process.pid)
                    if interpreter_shim_identity is not None and launch.executable == interpreter_shim_identity:
                        if not _identity_matches_pre_exec_ownership(
                            launch, self._pre_exec_ownership, interpreter_shim_identity
                        ):
                            raise NodeProcessError("node_process_identity_invalid")
                        # Only this exact pinned shim may be transient; never
                        # wait through an unrelated executable/ownership change.
                        remaining = _deadline_remaining(startup_deadline)
                        time.sleep(min(0.01, remaining))
                        continue
                    break
                except ProcessLookupError:
                    # Darwin may observe the exec transition between the two
                    # identity snapshots. Retry only the observation of this
                    # unreaped owned child inside the ORIGINAL launch deadline.
                    # All birth/session/executable checks below remain binding.
                    if self._process.poll() is not None:
                        raise
                    remaining = _deadline_remaining(startup_deadline)
                    time.sleep(min(0.01, remaining))
            phase = mark("inventory", "identity")
            if (
                not _identity_matches_pre_exec_ownership(
                    launch,
                    self._pre_exec_ownership,
                    command_identity,
                )
                or launch.executable != command_identity
            ):
                if (
                    launch.pid == self._process.pid
                    and _identity_matches_owned_incarnation(
                        launch, self._pre_exec_ownership
                    )
                ):
                    # Our own child, already running another image: refuse
                    # the start, but cleanup below still stops and reaps it.
                    self._owned_identity_changed = True
                raise NodeProcessError("node_process_identity_invalid")
            phase = mark("identity", "reader_threads")
            self._launch_identity = launch
            self._launch_timing = {
                "budget_ms": int(_LAUNCH_HANDSHAKE_SECONDS * 1000),
                "popen_ms": _bounded_ms(popen_ms),
                "phase_ms": {
                    name: _bounded_ms(marks[name]) for name in _LAUNCH_TIMING_PHASES
                },
            }
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
            failed_at = time.monotonic()
            # Observed before any cleanup signal: was the launcher (or its
            # exec'd target) already gone when this step failed?
            launcher_exited = self._process.poll() is not None
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
            if (
                launcher_exited or isinstance(exc, _HandshakeClosed)
            ) and self._process.returncode is None:
                # The launcher was seen gone (EOF on its ready pipe) before its
                # status was waitable, and a cleanup without pre-exec
                # ownership never reaps it: collect the status, bounded.
                self._collect_exit_status(
                    min(_LAUNCHER_EXIT_STATUS_SECONDS, self.shutdown_timeout_seconds)
                )
            raise NodeProcessError(
                code, cleanup_owner=None if cleaned else self,
                start_failure=NodeStartFailure.from_exception(
                    phase=phase, exception=exc, started=budget_started,
                    budget_seconds=_LAUNCH_HANDSHAKE_SECONDS, popen_ms=popen_ms,
                    phase_ms=marks,
                    # After cleanup: the launcher's own status when it had
                    # already exited, else the cleanup signal's.
                    launcher_returncode=self._process.returncode,
                    launcher_exited=launcher_exited, failed_at=failed_at,
                    launcher_reaped=self._process.returncode is not None,
                    identity_changed=self._owned_identity_changed,
                ),
            ) from exc
        finally:
            for name in tuple(handshake):
                _close_handshake_descriptor(handshake, name)

    def _collect_exit_status(self, timeout: float) -> None:
        """Wait at most ``timeout`` for an exited launcher's status."""

        try:
            self._process.wait(timeout=timeout)
        except (subprocess.TimeoutExpired, OSError):
            pass

    @property
    def owned_identity_changed(self) -> bool:
        """Whether our unreaped child was seen running an unpinned image."""

        return bool(getattr(self, "_owned_identity_changed", False))

    def _is_own_unreaped_child(
        self,
        current: _ProcessIdentity,
        ownership: _PreExecOwnership,
    ) -> bool:
        """Accept our own unreaped child after it exec'd an unpinned image.

        The caller has just seen ``poll()`` return no status, and only this
        owner reaps the child it spawned, so its pid -- and therefore its
        process group and session, which it leads -- cannot have been reused.
        Only the executable may differ: the incarnation must still be the
        pinned one.  A reaped child, or one this owner did not spawn, never
        reaches here and keeps every identity check.
        """

        if (
            current.pid != self._process.pid
            or not _identity_matches_owned_incarnation(current, ownership)
        ):
            return False
        self._owned_identity_changed = True
        return True

    @property
    def launch_timing(self) -> dict[str, Any] | None:
        """Bounded ms marks of the successful launch, against its budget."""

        timing = getattr(self, "_launch_timing", None)
        if timing is None:
            return None
        return {**timing, "phase_ms": dict(timing["phase_ms"])}

    def _start_win32_job(
        self,
        launch_command: Sequence[str],
        command_identity: _ExecutableIdentity,
    ) -> None:
        """Suspended spawn -> job assign -> identity check -> resume (win32)."""

        from mycelium_host_platform._win32_job import Win32JobProcess

        def image_matches(image: str) -> bool:
            if os.path.normcase(os.path.normpath(image)).casefold() != os.path.normcase(
                os.path.normpath(command_identity.path)
            ).casefold():
                return False
            try:
                observed = capture_executable_identity(image, require_canonical=True)
            except (OSError, RuntimeError, TypeError, ValueError):
                return False
            return observed == command_identity

        try:
            api = mycelium_host_platform.win32_api()
            job = Win32JobProcess.start(
                api,
                list(launch_command),
                image_matches=image_matches,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=-1,
                shell=False,
                close_fds=True,
                env=self._environment,
            )
        except HostPlatformError as exc:
            self._closed = True
            code = (
                "node_process_identity_invalid"
                if exc.code == "host_process_identity_invalid"
                else "node_process_start_failed"
            )
            raise NodeProcessError(
                code,
                start_failure=None if code != "node_process_start_failed"
                else NodeStartFailure.from_exception(
                    phase="win32_job", exception=exc, started=None,
                    budget_seconds=_LAUNCH_HANDSHAKE_SECONDS, popen_ms=None,
                    phase_ms={}, launcher_returncode=None, launcher_exited=None,
                ),
            ) from exc
        self._process = job.process
        self._win32_job = job
        try:
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
            self._closed = True
            cleaned = self._cleanup_resources(
                time.monotonic() + self.shutdown_timeout_seconds,
                terminate=True,
            )
            raise NodeProcessError(
                "node_process_start_failed" if cleaned else "node_process_cleanup_failed",
                cleanup_owner=None if cleaned else self,
                start_failure=NodeStartFailure.from_exception(
                    phase="reader_threads", exception=exc, started=None,
                    budget_seconds=_LAUNCH_HANDSHAKE_SECONDS, popen_ms=None,
                    phase_ms={}, launcher_returncode=None, launcher_exited=None,
                ),
            ) from exc

    def win32_job_resident_bytes(self) -> int:
        """Working-set bytes of every process in this node's job (win32 only)."""

        job = getattr(self, "_win32_job", None)
        if job is None or job.closed:
            raise NodeProcessError("node_process_job_unavailable")
        return job.resident_bytes()

    def win32_job_owned(self) -> bool:
        """The node still runs inside the job this owner created (win32 only)."""

        job = getattr(self, "_win32_job", None)
        return job is not None and job.state() == "owned"

    def _win32_owned_group_state(self, deadline: float) -> str:
        job = getattr(self, "_win32_job", None)
        if job is None or time.monotonic() > deadline:
            return "unsafe"
        state = job.state()
        if state == "empty":
            job.close()
        return state

    @property
    def pid(self) -> int:
        return self._process.pid

    @property
    def running(self) -> bool:
        with self._state_lock:
            return not self._closed and self._process.poll() is None

    @property
    def reader_failure_code(self) -> str | None:
        """Return the first fatal response-reader code for this process."""

        with self._waiters_lock:
            return getattr(self, "_reader_failure_code", None)

    @property
    def returncode(self) -> int | None:
        return self._process.poll()

    @property
    def stderr_tail(self) -> str:
        with self._stderr_lock:
            return b"".join(self._stderr_chunks).decode("utf-8", errors="replace")

    def _bounded_stderr_tail(self, chars: int = _STDERR_TAIL_CHARS) -> str | None:
        """Bounded operator-facing tail of this node's stderr, or None.

        The rejection envelope only carries an error code; the node's
        own stderr carries the real reason (traceback, verification
        failure). Attach a bounded tail so callers can surface the
        evidence without leaking unbounded process output.
        """
        with self._stderr_lock:
            payload = b"".join(self._stderr_chunks)
        if not payload:
            return None
        tail = payload[-chars:].decode("utf-8", errors="replace").rstrip()
        if not tail:
            return None
        return f"node {self.node_id} stderr (tail):\n{tail}"

    def _fail_waiters(self, item: object | _ReaderError) -> None:
        with self._waiters_lock:
            if (
                isinstance(item, _ReaderError)
                and item.code != "node_process_closed"
                and getattr(self, "_reader_failure_code", None) is None
            ):
                self._reader_failure_code = item.code
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
        # Correlating the waiter and publishing its response are one lifecycle
        # operation.  In particular, do not release this lock between lookup
        # and put: interrupt_command may otherwise fill and retire the one-slot
        # waiter while this reader still holds its stale reference.  Treating
        # the resulting Full as corrupt input poisons every unrelated waiter
        # and stops the shared response demultiplexer.
        with self._waiters_lock:
            waiter = self._waiters.get(command_id)
            if waiter is None:
                # A timed-out command may still finish cooperatively.  Its late
                # result is fenced by command ID and can be discarded without
                # failing unrelated waiters or stopping this shared response
                # loop.  Consuming the exact response also acknowledges and
                # removes its tombstone.  Keeping acknowledged tombstones used
                # to turn sustained cancellation churn into an eviction queue:
                # after 1,024 later interruptions, a genuinely outstanding ID
                # was forgotten and its valid reply killed the demultiplexer.
                if command_id in self._retired_command_ids:
                    self._retired_command_ids.pop(command_id, None)
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
                line = stream.readline(
                    self.max_frame_bytes + len(CLEANUP_CONTROL_FRAME_PREFIX) + 2
                )
                if not line:
                    return
                if line.startswith(CLEANUP_CONTROL_FRAME_PREFIX):
                    raw = line[len(CLEANUP_CONTROL_FRAME_PREFIX) :]
                    if len(raw) > self.max_frame_bytes + 1 or not raw.endswith(b"\n"):
                        self._fail_waiters(_ReaderError("invalid_node_response_frame"))
                        return
                    if not self._deliver_response(raw[:-1]):
                        return
                    continue
                with self._stderr_lock:
                    self._stderr_chunks.append(line)
                    self._stderr_size += len(line)
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
        if mycelium_host_platform.is_win32():
            # No signals reach a job: every hard stop is TerminateJobObject.
            job = getattr(self, "_win32_job", None)
            if job is None:
                return False
            if deadline is None:
                deadline = time.monotonic() + self.shutdown_timeout_seconds
            return job.terminate(deadline)
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
                    # macOS framework Python can perform a second, legitimate
                    # exec after the launch handshake: the captured target stub
                    # becomes the already-pinned Python.app launcher binary.
                    # Bind cleanup to the unchanged process incarnation and the
                    # two pre-captured executables rather than one transient
                    # post-handshake executable observation.
                    if not _identity_matches_pre_exec_ownership(
                        current,
                        ownership,
                        target_executable,
                    ) and not self._is_own_unreaped_child(current, ownership):
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
        if mycelium_host_platform.is_win32():
            return self._win32_owned_group_state(deadline)
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
            members: tuple[_ProcessGroupMember, ...] = ()
            leader_owned_and_live = False
            awaiting_reap = False
            if not stopped:
                try:
                    current = _inventory_process(self._process.pid)
                except (
                    OSError,
                    RuntimeError,
                    subprocess.SubprocessError,
                    ValueError,
                ):
                    # A just-killed child can be a zombie that libproc cannot
                    # inspect, while Popen.poll() has not reaped it yet. Its
                    # PID is still reserved. Do not fork /bin/ps for that
                    # transient observation: wait for the next bounded poll,
                    # then census the group after reaping. Neither call it
                    # empty nor signal an identity we could not observe.
                    stopped = self._process.poll() is not None
                    awaiting_reap = not stopped
                else:
                    _deadline_remaining(deadline)
                    if not _identity_matches_pre_exec_ownership(
                        current,
                        ownership,
                        target_executable,
                    ) and not self._is_own_unreaped_child(current, ownership):
                        return "unsafe"
                    leader_owned_and_live = True
            # Our live, unreaped leader keeps its group non-empty, so listing
            # the group cannot turn this poll into "empty"; signalling such a
            # leader's group needs no listing either.  The /bin/ps fork could
            # overrun the remaining budget under process churn, so list only
            # once the leader is gone (or was already reaped).
            # While our child is unreaped, a failed identity read gives no
            # permission to signal or claim emptiness. Retest within the
            # existing deadline instead of making an unbounded ps fork.
            if not leader_owned_and_live and not awaiting_reap:
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
        if awaiting_reap:
            return "unobserved"
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
        if mycelium_host_platform.is_win32():
            job = getattr(self, "_win32_job", None)
            return job is not None and job.terminate(deadline)
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
        if not group_complete:
            # BufferedReader.close() takes the same internal lock as the
            # blocking readline() in each reader thread.  If the bounded
            # process-group cleanup deadline expires while the child is still
            # alive, closing either output stream here can therefore wait
            # forever behind its reader.  Preserve the streams for the next
            # idempotent cleanup attempt; once the owned group is gone, EOF
            # releases both readers without an unbounded cross-thread close.
            self._cleanup_complete = False
            return False
        readers_complete = self._join_readers(deadline)
        complete = readers_complete and complete
        if readers_complete:
            complete = self._close_stream(self._process.stdout) and complete
            complete = self._close_stream(self._process.stderr) and complete
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
        raise _RemoteNodeError(
            error["code"],
            detail=self._bounded_stderr_tail() or "",
        )

    def _exchange_frame(
        self,
        *,
        command_id: str,
        frame: bytes,
        timeout: float,
        terminate_on_timeout: bool,
        priority_write: bool = False,
    ) -> Any:
        deadline = time.monotonic() + timeout
        waiter: Queue[bytes | object | _ReaderError | _CommandInterrupted] = Queue(
            maxsize=1
        )
        with self._waiters_lock:
            reader_failure_code = getattr(self, "_reader_failure_code", None)
            if reader_failure_code is not None:
                raise NodeProcessError(reader_failure_code)
            if command_id in self._waiters:
                raise NodeProcessError("duplicate_command_id")
            pending_interruptions = getattr(self, "_pending_interruptions", None)
            if pending_interruptions is None:
                pending_interruptions = OrderedDict()
                self._pending_interruptions = pending_interruptions
            pending_code = pending_interruptions.pop(command_id, None)
            if pending_code is not None:
                # The route published this command as request-inflight just
                # before command() registered its correlated waiter. Preserve
                # an interrupt that won in that narrow handoff and reject the
                # command before it can recreate state after cancellation.
                raise NodeProcessError(pending_code)
            self._waiters[command_id] = waiter
        response_received = False
        interrupted = False
        try:
            stdin = self._process.stdin
            assert stdin is not None
            try:
                with self._write_condition:
                    if priority_write:
                        self._priority_writers_waiting += 1
                    try:
                        while self._write_active or (
                            not priority_write and self._priority_writers_waiting > 0
                        ):
                            remaining = deadline - time.monotonic()
                            if remaining <= 0:
                                raise NodeProcessError("node_response_timeout")
                            self._write_condition.wait(timeout=remaining)
                        self._write_active = True
                    finally:
                        if priority_write:
                            self._priority_writers_waiting -= 1
                try:
                    # interrupt_command retires the waiter before its
                    # cancellation frame is queued. A normal command that was
                    # still waiting for write ownership must therefore not
                    # publish after cleanup overtakes it: doing so can recreate
                    # request/transport state after an exact receipt has been
                    # sealed. Once this thread owns the wire, the check and
                    # write are ordered before any priority writer by
                    # _write_active, so an interrupt racing after this point is
                    # a normal late response lifecycle.
                    with self._waiters_lock:
                        if self._waiters.get(command_id) is not waiter:
                            try:
                                retired = waiter.get_nowait()
                            except Empty as exc:
                                raise NodeProcessError(
                                    "command_interrupted_before_write"
                                ) from exc
                            if isinstance(retired, _CommandInterrupted):
                                interrupted = True
                                # No frame reached the child, so no response can
                                # acknowledge this retirement tombstone.
                                self._retired_command_ids.pop(command_id, None)
                                raise NodeProcessError(retired.code)
                            raise NodeProcessError("command_interrupted_before_write")
                    with self._state_lock:
                        if self._closed:
                            raise NodeProcessError("node_process_closed")
                        if self._process.poll() is not None:
                            self._closed = True
                            raise NodeProcessError("node_process_exited")
                    try:
                        frame_writer = getattr(
                            self,
                            "_control_frame_writer",
                            _write_control_frame_before,
                        )
                        frame_writer(stdin, frame + b"\n", deadline)
                    except _ControlFrameWriteTimeout as exc:
                        if exc.partial or terminate_on_timeout:
                            self._abort()
                        raise NodeProcessError("node_response_timeout") from exc
                finally:
                    with self._write_condition:
                        self._write_active = False
                        self._write_condition.notify_all()
            except NodeProcessError as exc:
                if exc.code == "node_response_timeout" and terminate_on_timeout:
                    self._abort()
                raise
            except (BrokenPipeError, OSError, ValueError) as exc:
                self._abort()
                raise NodeProcessError("node_process_unavailable") from exc
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                if terminate_on_timeout:
                    self._abort()
                raise NodeProcessError("node_response_timeout")
            try:
                item = waiter.get(timeout=remaining)
            except Empty as exc:
                if terminate_on_timeout:
                    self._abort()
                raise NodeProcessError("node_response_timeout") from exc
            if item is _EOF:
                self._abort()
                raise NodeProcessError("node_process_exited")
            if isinstance(item, _CommandInterrupted):
                interrupted = True
                raise NodeProcessError(item.code)
            if isinstance(item, _ReaderError):
                self._abort()
                raise NodeProcessError(item.code)
            assert isinstance(item, bytes)
            response_received = True
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
                # Queue.get may report its timeout just before the response
                # reader wins this same lock and publishes the correlated
                # bytes.  That response has already been consumed from the
                # wire, so retiring its ID would leave an unanswerable
                # tombstone.  Inspect the one-slot handoff while publication is
                # excluded and retire only when a response can still arrive.
                response_published = False
                if not response_received:
                    try:
                        response_published = isinstance(
                            waiter.get_nowait(),
                            bytes,
                        )
                    except Empty:
                        pass
                # Only an interrupted/timed-out command can legally answer
                # after its waiter has retired. Successful correlated replies
                # are complete and must not churn this bounded late-response
                # ledger: doing so used to evict a genuinely delayed inference
                # command after 1,024 unrelated health/cleanup replies. Its
                # eventual valid response was then mistaken for unsolicited
                # traffic, stopping the sole stdout demultiplexer while leaving
                # the child alive and able to deadlock on a full stdout pipe.
                # interrupt_command already performs the retirement while it
                # atomically removes the waiter.  Its exact late response can
                # acknowledge and remove that tombstone before this command
                # thread consumes the queued interrupt marker.  Re-adding the
                # ID here would resurrect an already-acknowledged lifecycle;
                # sustained cancellation then fills the bounded ledger with
                # impossible tombstones and can evict a genuinely outstanding
                # response ID.  Timeout owns no earlier retirement and still
                # uses the publication check above.
                if not interrupted and not response_received and not response_published:
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
        timeout = (
            self.response_timeout_seconds
            if timeout_seconds is None
            else _positive_seconds(timeout_seconds, "timeout_seconds")
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
            priority_write=(
                operation == "infer_cancel_wait"
                or (
                    operation == "snapshot"
                    and payload_document.get("receipt_only") is True
                )
            ),
        )

    def interrupt_command(self, command_id: str, *, code: str) -> bool:
        """Retire one correlated waiter without stopping the shared node process."""

        command_id = _required_identifier(command_id, "command_id")
        if not isinstance(code, str) or not _OPERATION_RE.fullmatch(code):
            raise ValueError("interrupt code is invalid")
        with self._waiters_lock:
            waiter = self._waiters.get(command_id)
            if waiter is None:
                pending_interruptions = getattr(self, "_pending_interruptions", None)
                if pending_interruptions is None:
                    pending_interruptions = OrderedDict()
                    self._pending_interruptions = pending_interruptions
                prior = pending_interruptions.get(command_id)
                if prior is not None:
                    return prior == code
                pending_interruptions[command_id] = code
                pending_interruptions.move_to_end(command_id)
                while len(pending_interruptions) > 1_024:
                    pending_interruptions.popitem(last=False)
                return True
            try:
                waiter.put_nowait(_CommandInterrupted(code))
            except Exception:
                return False
            # Publish retirement before the response reader can observe the
            # full waiter. A cooperatively cancelled node command may answer
            # immediately; leaving its interrupt marker registered used to
            # make that valid late response look like a duplicate/corrupt
            # frame and permanently stop the sole stdout demultiplexer.
            self._waiters.pop(command_id, None)
            self._retired_command_ids[command_id] = None
            self._retired_command_ids.move_to_end(command_id)
            while len(self._retired_command_ids) > 1_024:
                self._retired_command_ids.popitem(last=False)
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
