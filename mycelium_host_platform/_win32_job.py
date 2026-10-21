# SPDX-License-Identifier: AGPL-3.0-only
"""Child processes bound to a kill-on-close Windows job object.

Windows has no process groups, ``killpg``, ``SIGKILL`` or ``pass_fds``.  The
POSIX supervisor's guarantees are rebuilt from a job object:

1. the child is created suspended (``CREATE_SUSPENDED |
   CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW``), so it runs no code before
   it is contained;
2. a fresh job gets ``KILL_ON_JOB_CLOSE | DIE_ON_UNHANDLED_EXCEPTION`` and no
   breakaway permission, read back before use;
3. the child is assigned to the job and membership is confirmed;
4. its identity (full image path and creation time) is captured and checked
   by the caller before any code runs;
5. only then are its threads resumed.

Any failure before resume terminates the suspended child.  Hard stop is
``TerminateJobObject``, complete only when the job reports zero active
processes and the child is reaped.  A job that contains the current process
is never terminated.
"""

from __future__ import annotations

from dataclasses import dataclass
import subprocess
import time
from typing import Any, Callable

from . import _win32_api as w
from ._common import HostPlatformError


CREATION_FLAGS = w.CREATE_SUSPENDED | w.CREATE_NEW_PROCESS_GROUP | w.CREATE_NO_WINDOW
REQUIRED_LIMITS = (
    w.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE | w.JOB_OBJECT_LIMIT_DIE_ON_UNHANDLED_EXCEPTION
)
FORBIDDEN_LIMITS = w.JOB_OBJECT_LIMIT_BREAKAWAY_OK | w.JOB_OBJECT_LIMIT_SILENT_BREAKAWAY_OK
_KILL_EXIT_CODE = 1
_REAP_SECONDS = 5.0


@dataclass(frozen=True)
class JobProcessIdentity:
    pid: int
    image_path: str
    creation_ns: int


def _close_quietly(api: Any, handle: int | None) -> None:
    if handle is None:
        return
    try:
        api.close(handle)
    except w.Win32Error:
        pass


class Win32JobProcess:
    """One child process contained in its own kill-on-close job."""

    def __init__(
        self,
        api: Any,
        process: subprocess.Popen[bytes],
        *,
        job: int,
        process_handle: int,
        identity: JobProcessIdentity,
    ) -> None:
        self._api = api
        self.process = process
        self._job: int | None = job
        self._process_handle: int | None = process_handle
        self.identity = identity

    @classmethod
    def start(
        cls,
        api: Any,
        argv: list[str] | tuple[str, ...],
        *,
        image_matches: Callable[[str], bool],
        **popen: Any,
    ) -> "Win32JobProcess":
        """Spawn suspended, contain, verify identity, then resume.

        Raises ``host_job_unavailable`` when containment cannot be proven and
        ``host_process_identity_invalid`` when the image is not the expected
        executable; in both cases the child never ran and has been killed.
        """

        try:
            process = api.spawn_suspended(list(argv), creationflags=CREATION_FLAGS, **popen)
        except (OSError, ValueError) as exc:
            raise HostPlatformError("host_job_unavailable", "spawn") from exc
        job: int | None = None
        handle: int | None = None
        try:
            try:
                job = api.create_job()
                api.set_job_limits(job, REQUIRED_LIMITS)
                flags = api.job_limit_flags(job)
                if flags & REQUIRED_LIMITS != REQUIRED_LIMITS or flags & FORBIDDEN_LIMITS:
                    raise HostPlatformError("host_job_unavailable", "limits")
                handle = api.open_process(process.pid)
                api.assign_process(job, handle)
                if not api.is_process_in_job(handle, job):
                    raise HostPlatformError("host_job_unavailable", "membership")
                image = api.process_image_name(handle)
                creation = api.process_creation_time(handle)
            except w.Win32Error as exc:
                raise HostPlatformError("host_job_unavailable", exc.operation) from exc
            if not isinstance(image, str) or not image or not image_matches(image):
                raise HostPlatformError("host_process_identity_invalid", "image")
            identity = JobProcessIdentity(process.pid, image, int(creation))
            try:
                resumed = api.resume_process(process.pid)
            except w.Win32Error as exc:
                raise HostPlatformError("host_job_unavailable", "resume") from exc
            if resumed < 1:
                raise HostPlatformError("host_job_unavailable", "resume")
        except BaseException:
            cls._kill_unstarted(api, process, job, handle)
            raise
        return cls(api, process, job=job, process_handle=handle, identity=identity)

    @staticmethod
    def _kill_unstarted(
        api: Any,
        process: subprocess.Popen[bytes],
        job: int | None,
        handle: int | None,
    ) -> None:
        """Kill a child that never resumed; prefer the job, else the process."""

        killed = False
        if job is not None and handle is not None:
            try:
                if api.is_process_in_job(handle, job) and not api.is_process_in_job(
                    api.current_process(), job
                ):
                    api.terminate_job(job, _KILL_EXIT_CODE)
                    killed = True
            except w.Win32Error:
                killed = False
        if not killed:
            try:
                if handle is not None:
                    api.terminate_process(handle, _KILL_EXIT_CODE)
                else:
                    process.kill()
            except (OSError, w.Win32Error):
                try:
                    process.kill()
                except OSError:
                    pass
        try:
            process.wait(timeout=_REAP_SECONDS)
        except subprocess.TimeoutExpired:
            pass
        for stream in (process.stdin, process.stdout, process.stderr):
            if stream is not None:
                try:
                    stream.close()
                except (OSError, ValueError):
                    pass
        _close_quietly(api, handle)
        _close_quietly(api, job)

    # ------------------------------------------------------------ queries

    @property
    def closed(self) -> bool:
        return self._job is None

    def owns_current_process(self) -> bool:
        if self._job is None:
            return False
        return bool(self._api.is_process_in_job(self._api.current_process(), self._job))

    def active_processes(self) -> int:
        if self._job is None:
            return 0
        return int(self._api.job_active_processes(self._job))

    def resident_bytes(self) -> int:
        """Sum of the working sets of every process currently in the job.

        A process that exits between the listing and its query no longer
        counts; any other failure raises, so the job is never reported empty.
        """

        if self._job is None:
            return 0
        total = 0
        for pid in self._api.job_process_ids(self._job):
            try:
                handle = self._api.open_process(pid)
            except w.Win32Error as exc:
                if exc.winerror == w.ERROR_INVALID_PARAMETER:
                    continue  # exited since the listing
                raise
            try:
                total += int(self._api.process_working_set_bytes_of(handle))
            finally:
                _close_quietly(self._api, handle)
        return total

    def state(self) -> str:
        """``empty`` (reaped, no job members), ``owned`` or ``unsafe``."""

        if self._job is None:
            return "empty" if self.process.poll() is not None else "unsafe"
        try:
            if self.owns_current_process():
                return "unsafe"
            active = self.active_processes()
        except w.Win32Error:
            return "unsafe"
        if active == 0 and self.process.poll() is not None:
            return "empty"
        return "owned"

    # ------------------------------------------------------------ control

    def terminate(self, deadline: float) -> bool:
        """``TerminateJobObject``; True only once the job is empty and reaped."""

        if self._job is None:
            return self.process.poll() is not None
        try:
            if self.owns_current_process():
                return False
            self._api.terminate_job(self._job, _KILL_EXIT_CODE)
        except w.Win32Error:
            return False
        return self.wait_empty(deadline)

    def wait_empty(self, deadline: float) -> bool:
        while True:
            state = self.state()
            if state == "empty":
                self.close()
                return True
            if state == "unsafe":
                return False
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            try:
                self.process.wait(timeout=min(0.02, remaining))
            except subprocess.TimeoutExpired:
                continue
            except OSError:
                time.sleep(min(0.02, max(0.0, remaining)))

    def close(self) -> None:
        """Release the handles; closing the job kills anything still in it."""

        handle, job = self._process_handle, self._job
        self._process_handle = None
        self._job = None
        _close_quietly(self._api, handle)
        _close_quietly(self._api, job)


__all__ = [
    "CREATION_FLAGS",
    "FORBIDDEN_LIMITS",
    "JobProcessIdentity",
    "REQUIRED_LIMITS",
    "Win32JobProcess",
]
