# SPDX-License-Identifier: AGPL-3.0-only
"""A deterministic stand-in for the Win32 API used by the win32 host backend.

The fake is backed by the real (POSIX) test filesystem for bytes, directory
entries and handle identity, and models the Windows-only parts in memory:

* a handle table with ``CreateFileW`` access and share modes, including
  sharing violations between open handles;
* security descriptors (owner SID, DACL protection, ACEs) keyed by file
  identity so they follow a ``MoveFileExW`` rename, created from the same SDDL
  strings the backend passes;
* reparse points (a POSIX symlink is reported as ``FILE_ATTRIBUTE_REPARSE_POINT``
  when opened with ``FILE_FLAG_OPEN_REPARSE_POINT``);
* injectable one-shot failures per operation and queued ``MoveFileExW`` errors.

Every call is appended to ``calls`` so tests can assert exact ordering.

Because the bytes live on a POSIX filesystem (directory descriptors,
``O_NOFOLLOW``, ``F_GETPATH`` or ``/proc/self/fd``), the fake cannot run on a
real Windows host.  The first operation that needs that backing skips the test
there (:func:`require_posix_backing`); the real Win32 bindings are exercised by
the real-host suites instead.  A test that never reaches such an operation
still runs on Windows.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import os
import re
import stat
from typing import Any

from mycelium_host_platform import _win32_api as w


USER_SID = "S-1-5-21-1111111111-2222222222-3333333333-1001"
OTHER_USER_SID = "S-1-5-21-1111111111-2222222222-3333333333-1002"
USERS_SID = "S-1-5-32-545"
EVERYONE_SID = "S-1-1-0"

_READ_RIGHTS = w.GENERIC_READ | w.GENERIC_ALL | w.FILE_READ_DATA
_WRITE_RIGHTS = w.GENERIC_WRITE | w.GENERIC_ALL | w.FILE_WRITE_DATA | w.FILE_APPEND_DATA
_DELETE_RIGHTS = w.DELETE | w.GENERIC_ALL
_SDDL_ALIASES = {
    "SY": w.SYSTEM_SID,
    "BA": w.ADMINISTRATORS_SID,
    "BU": USERS_SID,
    "WD": EVERYONE_SID,
}
_SDDL_RIGHTS = {"FA": w.FILE_ALL_ACCESS, "FR": 0x120089, "GA": w.GENERIC_ALL}
_ACE_RE = re.compile(r"\((A|D);([A-Z]*);([A-Z]+|0x[0-9a-fA-F]+);;;([A-Z]{2}|S-1-[0-9-]+)\)")


def parse_sddl(sddl: str) -> w.SecurityDescriptor:
    owner = re.search(r"O:(S-1-[0-9-]+|[A-Z]{2})", sddl)
    dacl = re.search(r"D:(P?)((?:\([^)]*\))*)", sddl)
    if owner is None or dacl is None:
        raise ValueError(f"unsupported SDDL: {sddl}")
    aces: list[w.Ace] = []
    for kind, flags, rights, sid in _ACE_RE.findall(dacl.group(2)):
        flag_bits = 0
        if "OI" in flags:
            flag_bits |= 0x01
        if "CI" in flags:
            flag_bits |= 0x02
        if "IO" in flags:
            flag_bits |= 0x08
        mask = int(rights, 16) if rights.startswith("0x") else _SDDL_RIGHTS[rights]
        aces.append(
            w.Ace(
                ace_type=w.ACCESS_ALLOWED_ACE_TYPE if kind == "A" else w.ACCESS_DENIED_ACE_TYPE,
                flags=flag_bits,
                mask=mask,
                sid=_SDDL_ALIASES.get(sid, sid),
            )
        )
    owner_sid = _SDDL_ALIASES.get(owner.group(1), owner.group(1))
    return w.SecurityDescriptor(owner_sid, dacl.group(1) == "P", tuple(aces))


def owner_only(user_sid: str = USER_SID) -> w.SecurityDescriptor:
    return parse_sddl(w.owner_only_directory_sddl(user_sid))


def inherited_profile_acl(user_sid: str = USER_SID) -> w.SecurityDescriptor:
    """What a directory under ``%LOCALAPPDATA%`` normally inherits."""

    return w.SecurityDescriptor(
        owner_sid=user_sid,
        dacl_protected=False,
        aces=(
            w.Ace(w.ACCESS_ALLOWED_ACE_TYPE, 0x13, w.FILE_ALL_ACCESS, w.SYSTEM_SID),
            w.Ace(w.ACCESS_ALLOWED_ACE_TYPE, 0x13, w.FILE_ALL_ACCESS, w.ADMINISTRATORS_SID),
            w.Ace(w.ACCESS_ALLOWED_ACE_TYPE, 0x13, w.FILE_ALL_ACCESS, user_sid),
        ),
    )


def require_posix_backing(operation: str) -> None:
    """Skip the running test on a real Windows host: ``operation`` needs POSIX backing.

    ``pytest.skip`` raises a ``BaseException``, so product code that maps
    ``Exception``/``OSError`` to its own refusal codes cannot turn it into a
    misleading failure.
    """

    if os.name == "nt":
        import pytest

        pytest.skip(
            f"FakeWin32Api simulates win32 over a POSIX filesystem ({operation}); "
            "on a real Windows host the real Win32 API suites run instead"
        )


def final_path_of_descriptor(descriptor: int) -> str:
    require_posix_backing("final path of a descriptor")
    # ``sys.platform`` is monkeypatched to ``win32`` while the fake runs, so
    # ask the kernel which host this really is.
    if os.uname().sysname == "Darwin":
        import fcntl

        raw = fcntl.fcntl(descriptor, 50, b"\0" * 1024)  # F_GETPATH
        return os.fsdecode(raw.split(b"\0", 1)[0])
    return os.readlink(f"/proc/self/fd/{descriptor}")


@dataclass
class _Handle:
    path: str
    fd: int | None
    access: int
    share: int
    identity: tuple[int, int]
    kind: str  # "file" | "directory" | "reparse"
    extra: dict[str, Any] = field(default_factory=dict)


class FakeWin32Api:
    def __init__(self, user_sid: str = USER_SID) -> None:
        self.user_sid = user_sid
        self.calls: list[tuple[Any, ...]] = []
        self.handles: dict[int, _Handle] = {}
        self._next_handle = 0x100
        self.security_by_identity: dict[tuple[int, int], w.SecurityDescriptor] = {}
        self.default_security = inherited_profile_acl(user_sid)
        self.failures: dict[str, list[int]] = {}
        self.move_failures: list[int] = []
        self.final_path_overrides: dict[str, str] = {}
        self.short_writes = False
        self.closed_jobs: list[int] = []
        self._init_processes()

    # ------------------------------------------------------------ helpers

    def fail_next(self, operation: str, winerror: int, times: int = 1) -> None:
        self.failures.setdefault(operation, []).extend([winerror] * times)

    def _maybe_fail(self, operation: str) -> None:
        queued = self.failures.get(operation)
        if queued:
            raise w.Win32Error(queued.pop(0), operation)

    def set_security(self, path: str | os.PathLike[str], descriptor: w.SecurityDescriptor) -> None:
        metadata = os.lstat(os.fspath(path))
        self.security_by_identity[(metadata.st_dev, metadata.st_ino)] = descriptor

    def security_of(self, path: str | os.PathLike[str]) -> w.SecurityDescriptor:
        metadata = os.lstat(os.fspath(path))
        return self.security_by_identity.get(
            (metadata.st_dev, metadata.st_ino), self.default_security
        )

    def open_handles_for(self, path: str | os.PathLike[str]) -> list[_Handle]:
        metadata = os.lstat(os.fspath(path))
        identity = (metadata.st_dev, metadata.st_ino)
        return [handle for handle in self.handles.values() if handle.identity == identity]

    def _record(self, handle: _Handle) -> int:
        self._next_handle += 4
        self.handles[self._next_handle] = handle
        return self._next_handle

    def _handle(self, handle: int) -> _Handle:
        record = self.handles.get(handle)
        if record is None:
            raise w.Win32Error(w.ERROR_INVALID_HANDLE, "handle")
        return record

    @staticmethod
    def _parent_missing(path: str) -> bool:
        return not os.path.isdir(os.path.dirname(path))

    def _check_sharing(self, identity: tuple[int, int], access: int, share: int) -> None:
        for other in self.handles.values():
            if other.identity != identity:
                continue
            if access & _READ_RIGHTS and not other.share & w.FILE_SHARE_READ:
                raise w.Win32Error(w.ERROR_SHARING_VIOLATION, "CreateFileW")
            if access & _WRITE_RIGHTS and not other.share & w.FILE_SHARE_WRITE:
                raise w.Win32Error(w.ERROR_SHARING_VIOLATION, "CreateFileW")
            if access & _DELETE_RIGHTS and not other.share & w.FILE_SHARE_DELETE:
                raise w.Win32Error(w.ERROR_SHARING_VIOLATION, "CreateFileW")
            if other.access & _READ_RIGHTS and not share & w.FILE_SHARE_READ:
                raise w.Win32Error(w.ERROR_SHARING_VIOLATION, "CreateFileW")
            if other.access & _WRITE_RIGHTS and not share & w.FILE_SHARE_WRITE:
                raise w.Win32Error(w.ERROR_SHARING_VIOLATION, "CreateFileW")
            if other.access & _DELETE_RIGHTS and not share & w.FILE_SHARE_DELETE:
                raise w.Win32Error(w.ERROR_SHARING_VIOLATION, "CreateFileW")

    def _blocks_rename(self, path: str) -> bool:
        try:
            metadata = os.lstat(path)
        except FileNotFoundError:
            return False
        identity = (metadata.st_dev, metadata.st_ino)
        return any(
            handle.identity == identity and not handle.share & w.FILE_SHARE_DELETE
            for handle in self.handles.values()
        )

    # --------------------------------------------------------------- api

    def current_user_sid(self) -> str:
        self.calls.append(("current_user_sid",))
        self._maybe_fail("current_user_sid")
        return self.user_sid

    def create_directory(self, path: str, sddl: str) -> None:
        self.calls.append(("create_directory", path, sddl))
        self._maybe_fail("create_directory")
        if os.path.lexists(path):
            raise w.Win32Error(w.ERROR_ALREADY_EXISTS, "CreateDirectoryW")
        if self._parent_missing(path):
            raise w.Win32Error(w.ERROR_PATH_NOT_FOUND, "CreateDirectoryW")
        os.mkdir(path, 0o700)
        self.set_security(path, parse_sddl(sddl))

    def open(
        self,
        path: str,
        *,
        access: int,
        share: int,
        disposition: int,
        flags: int,
        sddl: str | None = None,
    ) -> int:
        self.calls.append(("open", path, access, share, disposition, flags, sddl))
        self._maybe_fail("open")
        exists = os.path.lexists(path)
        if disposition == w.CREATE_NEW:
            if exists:
                raise w.Win32Error(w.ERROR_FILE_EXISTS, "CreateFileW")
            if self._parent_missing(path):
                raise w.Win32Error(w.ERROR_PATH_NOT_FOUND, "CreateFileW")
            require_posix_backing("O_NOFOLLOW exclusive create")
            descriptor = os.open(
                path, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
            )
            metadata = os.fstat(descriptor)
            identity = (metadata.st_dev, metadata.st_ino)
            self.security_by_identity[identity] = (
                parse_sddl(sddl) if sddl is not None else self.default_security
            )
            return self._record(_Handle(path, descriptor, access, share, identity, "file"))
        if disposition != w.OPEN_EXISTING:
            raise NotImplementedError(disposition)
        if not exists:
            code = (
                w.ERROR_PATH_NOT_FOUND
                if self._parent_missing(path)
                else w.ERROR_FILE_NOT_FOUND
            )
            raise w.Win32Error(code, "CreateFileW")
        link = os.path.islink(path)
        if link and flags & w.FILE_FLAG_OPEN_REPARSE_POINT:
            metadata = os.lstat(path)
            identity = (metadata.st_dev, metadata.st_ino)
            self._check_sharing(identity, access, share)
            return self._record(
                _Handle(path, None, access, share, identity, "reparse")
            )
        target = os.path.realpath(path) if link else path
        if not os.path.exists(target):
            raise w.Win32Error(w.ERROR_FILE_NOT_FOUND, "CreateFileW")
        if os.path.isdir(target):
            if not flags & w.FILE_FLAG_BACKUP_SEMANTICS:
                raise w.Win32Error(w.ERROR_ACCESS_DENIED, "CreateFileW")
            require_posix_backing("directory descriptor")
            descriptor = os.open(target, os.O_RDONLY)
            kind = "directory"
        else:
            mode = os.O_RDWR if access & _WRITE_RIGHTS else os.O_RDONLY
            descriptor = os.open(target, mode)
            kind = "file"
        metadata = os.fstat(descriptor)
        identity = (metadata.st_dev, metadata.st_ino)
        try:
            self._check_sharing(identity, access, share)
        except w.Win32Error:
            os.close(descriptor)
            raise
        return self._record(_Handle(target, descriptor, access, share, identity, kind))

    def lock_file(self, handle: int, *, blocking: bool) -> None:
        """LockFileEx on byte 0; contention fails fast (a fake cannot block)."""

        self.calls.append(("lock_file", handle, blocking))
        self._maybe_fail("lock_file")
        record = self.handles.get(handle)
        if record is None or record.kind != "file":
            raise w.Win32Error(w.ERROR_INVALID_HANDLE, "LockFileEx")
        locks = self.__dict__.setdefault("file_locks", {})
        holder = locks.get(record.identity)
        if holder is not None and holder != handle:
            raise w.Win32Error(w.ERROR_LOCK_VIOLATION, "LockFileEx")
        locks[record.identity] = handle

    def unlock_file(self, handle: int) -> None:
        self.calls.append(("unlock_file", handle))
        record = self.handles.get(handle)
        locks = self.__dict__.setdefault("file_locks", {})
        if record is None or locks.get(record.identity) != handle:
            raise w.Win32Error(w.ERROR_LOCK_VIOLATION, "UnlockFileEx")
        del locks[record.identity]

    def close(self, handle: int) -> None:
        self.calls.append(("close", handle))
        if self.close_process_or_job(handle):
            return
        record = self.handles.get(handle)
        if record is not None:
            locks = self.__dict__.setdefault("file_locks", {})
            if locks.get(record.identity) == handle:
                del locks[record.identity]
        record = self.handles.pop(handle, None)
        if record is None:
            raise w.Win32Error(w.ERROR_INVALID_HANDLE, "CloseHandle")
        if record.fd is not None:
            os.close(record.fd)

    def information(self, handle: int) -> w.FileInformation:
        self.calls.append(("information", handle))
        self._maybe_fail("information")
        record = self._handle(handle)
        if record.fd is None:
            metadata = os.lstat(record.path)
            attributes = w.FILE_ATTRIBUTE_REPARSE_POINT
            if os.path.isdir(record.path):
                attributes |= w.FILE_ATTRIBUTE_DIRECTORY
        else:
            metadata = os.fstat(record.fd)
            attributes = (
                w.FILE_ATTRIBUTE_DIRECTORY
                if stat.S_ISDIR(metadata.st_mode)
                else w.FILE_ATTRIBUTE_NORMAL
            )
        return w.FileInformation(
            attributes=attributes,
            volume_serial=metadata.st_dev,
            file_index=metadata.st_ino,
            number_of_links=metadata.st_nlink if not stat.S_ISDIR(metadata.st_mode) else 1,
            size=metadata.st_size if not stat.S_ISDIR(metadata.st_mode) else 0,
            creation_ns=metadata.st_ctime_ns,
            last_write_ns=metadata.st_mtime_ns,
        )

    def final_path(self, handle: int) -> str:
        self.calls.append(("final_path", handle))
        self._maybe_fail("final_path")
        record = self._handle(handle)
        if record.fd is None:
            observed = record.path
        else:
            observed = final_path_of_descriptor(record.fd)
        return self.final_path_overrides.get(observed, observed)

    def security(self, handle: int) -> w.SecurityDescriptor:
        self.calls.append(("security", handle))
        self._maybe_fail("security")
        record = self._handle(handle)
        return self.security_by_identity.get(record.identity, self.default_security)

    def read(self, handle: int, size: int) -> bytes:
        self.calls.append(("read", handle, size))
        self._maybe_fail("read")
        record = self._handle(handle)
        assert record.fd is not None
        return os.read(record.fd, size)

    def write(self, handle: int, data: bytes) -> int:
        self.calls.append(("write", handle, len(data)))
        self._maybe_fail("write")
        record = self._handle(handle)
        assert record.fd is not None
        if self.short_writes:
            return 0
        return os.write(record.fd, data)

    def flush(self, handle: int) -> None:
        self.calls.append(("flush", handle))
        self._maybe_fail("flush")
        record = self._handle(handle)
        assert record.fd is not None
        os.fsync(record.fd)

    def move_replace(self, source: str, target: str) -> None:
        self.calls.append(("move_replace", source, target))
        if self.move_failures:
            raise w.Win32Error(self.move_failures.pop(0), "MoveFileExW")
        if self._blocks_rename(source) or self._blocks_rename(target):
            raise w.Win32Error(w.ERROR_SHARING_VIOLATION, "MoveFileExW")
        os.replace(source, target)

    def delete(self, path: str) -> None:
        self.calls.append(("delete", path))
        if not os.path.lexists(path):
            raise w.Win32Error(w.ERROR_FILE_NOT_FOUND, "DeleteFileW")
        if self._blocks_rename(path):
            raise w.Win32Error(w.ERROR_SHARING_VIOLATION, "DeleteFileW")
        os.unlink(path)

    def handle_to_fd(self, handle: int) -> int:
        self.calls.append(("handle_to_fd", handle))
        record = self.handles.pop(handle)
        assert record.fd is not None
        return record.fd

    # ------------------------------------------------- processes and jobs
    #
    # A spawned child is a real (POSIX) child process.  ``image_for`` maps the
    # argv[0] the backend asked for to the image name the fake reports, and
    # ``interpreter`` is the real program actually executed for it.

    CURRENT_PROCESS = -1

    def _init_processes(self) -> None:
        if hasattr(self, "jobs"):
            return
        self.jobs: dict[int, dict[str, Any]] = {}
        self.process_handles: dict[int, int] = {}
        self.popen_by_pid: dict[int, Any] = {}
        self.images: dict[int, str] = {}
        self.image_override: str | None = None
        self.interpreter: str | None = None
        self.current_process_in_job: set[int] = set()
        self.resume_count = 1
        self.limit_flags_override: int | None = None

    def spawn_suspended(self, argv: list[str], *, creationflags: int, **popen: Any) -> Any:
        import subprocess

        self._init_processes()
        self.calls.append(("spawn_suspended", tuple(argv), creationflags))
        self._maybe_fail("spawn_suspended")
        real = [self.interpreter or argv[0], *argv[1:]]
        process = subprocess.Popen(real, **popen)
        self.popen_by_pid[process.pid] = process
        self.images[process.pid] = argv[0]
        return process

    def open_process(self, pid: int) -> int:
        self._init_processes()
        self.calls.append(("open_process", pid))
        self._maybe_fail("open_process")
        self._next_handle += 4
        self.process_handles[self._next_handle] = pid
        return self._next_handle

    def current_process(self) -> int:
        return self.CURRENT_PROCESS

    def _pid(self, process: int) -> int:
        self._init_processes()
        if process not in self.process_handles:
            raise w.Win32Error(w.ERROR_INVALID_HANDLE, "process")
        return self.process_handles[process]

    def process_image_name(self, process: int) -> str:
        self.calls.append(("process_image_name", process))
        self._maybe_fail("process_image_name")
        pid = self._pid(process)
        return self.image_override or self.images[pid]

    def process_creation_time(self, process: int) -> int:
        self.calls.append(("process_creation_time", process))
        return 1_000_000 + self._pid(process)

    def resume_process(self, pid: int) -> int:
        self.calls.append(("resume_process", pid))
        self._maybe_fail("resume_process")
        return self.resume_count

    def terminate_process(self, process: int, exit_code: int) -> None:
        self.calls.append(("terminate_process", process, exit_code))
        popen = self.popen_by_pid.get(self._pid(process))
        if popen is not None and popen.poll() is None:
            popen.kill()

    def create_job(self) -> int:
        self._init_processes()
        self.calls.append(("create_job",))
        self._maybe_fail("create_job")
        self._next_handle += 4
        self.jobs[self._next_handle] = {"limits": 0, "pids": set(), "terminated": False}
        return self._next_handle

    def _job(self, job: int) -> dict[str, Any]:
        self._init_processes()
        if job not in self.jobs:
            raise w.Win32Error(w.ERROR_INVALID_HANDLE, "job")
        return self.jobs[job]

    def set_job_limits(self, job: int, limit_flags: int) -> None:
        self.calls.append(("set_job_limits", job, limit_flags))
        self._maybe_fail("set_job_limits")
        self._job(job)["limits"] = limit_flags

    def job_limit_flags(self, job: int) -> int:
        self.calls.append(("job_limit_flags", job))
        if self.limit_flags_override is not None:
            return self.limit_flags_override
        return self._job(job)["limits"]

    def assign_process(self, job: int, process: int) -> None:
        self.calls.append(("assign_process", job, process))
        self._maybe_fail("assign_process")
        self._job(job)["pids"].add(self._pid(process))

    def is_process_in_job(self, process: int, job: int) -> bool:
        self.calls.append(("is_process_in_job", process, job))
        if process == self.CURRENT_PROCESS:
            return job in self.current_process_in_job
        return self._pid(process) in self._job(job)["pids"]

    def job_active_processes(self, job: int) -> int:
        self.calls.append(("job_active_processes", job))
        return sum(
            1
            for pid in self._job(job)["pids"]
            if self.popen_by_pid[pid].poll() is None
        )

    def job_process_ids(self, job: int) -> list[int]:
        self.calls.append(("job_process_ids", job))
        self._maybe_fail("job_process_ids")
        return [pid for pid in self._job(job)["pids"] if self.popen_by_pid[pid].poll() is None]

    def process_working_set_bytes_of(self, process: int) -> int:
        self.calls.append(("process_working_set_bytes_of", process))
        self._maybe_fail("process_working_set_bytes_of")
        pid = self._pid(process)
        return int(getattr(self, "working_set_by_pid", {}).get(pid, 10 * 1024 * 1024))

    def terminate_job(self, job: int, exit_code: int) -> None:
        self.calls.append(("terminate_job", job, exit_code))
        self._maybe_fail("terminate_job")
        record = self._job(job)
        record["terminated"] = True
        for pid in record["pids"]:
            popen = self.popen_by_pid[pid]
            if popen.poll() is None:
                popen.kill()

    def close_process_or_job(self, handle: int) -> bool:
        self._init_processes()
        if handle in getattr(self, "event_handles", {}):
            del self.event_handles[handle]
            return True
        if handle in self.process_handles:
            del self.process_handles[handle]
            return True
        if handle in self.jobs:
            record = self.jobs.pop(handle)
            # KILL_ON_JOB_CLOSE.
            if record["limits"] & w.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE:
                for pid in record["pids"]:
                    popen = self.popen_by_pid[pid]
                    if popen.poll() is None:
                        popen.kill()
            self.closed_jobs.append(handle)
            return True
        return False

    # ------------------------------------------------ memory and events

    working_set_bytes = 123_456_789
    available_bytes = 8 * 1024**3

    def process_working_set_bytes(self) -> int:
        self.calls.append(("process_working_set_bytes",))
        self._maybe_fail("process_working_set_bytes")
        return self.working_set_bytes

    def total_physical_bytes(self) -> int:
        self.calls.append(("total_physical_bytes",))
        return int(getattr(self, "total_bytes", 16 * 1024 ** 3))

    def available_physical_bytes(self) -> int:
        self.calls.append(("available_physical_bytes",))
        self._maybe_fail("available_physical_bytes")
        return self.available_bytes

    def _events_table(self) -> dict[str, Any]:
        if not hasattr(self, "events"):
            import threading

            self.events: dict[str, Any] = {}
            self.event_handles: dict[int, str] = {}
            self._threading = threading
        return self.events

    def create_named_event(self, name: str) -> Any:
        """Test helper: the supervisor side of a named event."""

        events = self._events_table()
        events.setdefault(name, self._threading.Event())
        return events[name]

    def open_event(self, name: str) -> int:
        self.calls.append(("open_event", name))
        self._maybe_fail("open_event")
        events = self._events_table()
        if name not in events:
            raise w.Win32Error(w.ERROR_FILE_NOT_FOUND, "OpenEventW")
        self._next_handle += 4
        self.event_handles[self._next_handle] = name
        return self._next_handle

    def create_event(self, name: str) -> tuple[int, bool]:
        self.calls.append(("create_event", name))
        self._maybe_fail("create_event")
        events = self._events_table()
        existed = name in events
        events.setdefault(name, self._threading.Event())
        self._next_handle += 4
        self.event_handles[self._next_handle] = name
        return self._next_handle, existed

    def set_event(self, event: int) -> None:
        self.calls.append(("set_event", event))
        self._maybe_fail("set_event")
        name = self.event_handles.get(event)
        if name is None:
            raise w.Win32Error(w.ERROR_INVALID_HANDLE, "SetEvent")
        self.events[name].set()

    def wait_event(self, event: int, timeout_ms: int) -> bool:
        name = self.event_handles.get(event)
        if name is None:
            raise w.Win32Error(w.ERROR_INVALID_HANDLE, "WaitForSingleObject")
        return self.events[name].wait(timeout_ms / 1000)

    # --------------------------------------------------------- inspection

    def call_names(self) -> list[str]:
        return [call[0] for call in self.calls]


def simulate_win32(monkeypatch: Any, api: FakeWin32Api | None = None) -> FakeWin32Api:
    """Report ``sys.platform == "win32"`` and inject ``api`` for one test."""

    import mycelium_host_platform

    fake = api if api is not None else FakeWin32Api()
    monkeypatch.setattr(mycelium_host_platform.sys, "platform", "win32")
    monkeypatch.setattr(mycelium_host_platform, "_API_OVERRIDE", fake)
    return fake
