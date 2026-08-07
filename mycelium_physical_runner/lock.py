"""Private, no-follow cross-process lock for one physical run."""
from __future__ import annotations

import errno
import fcntl
import os
import stat
from pathlib import Path

from .errors import RunnerError
from .private_fs import prepare_private_parent, verify_private_parent

LOCK_FILE_MODE = 0o600


class ExclusiveLock:
    def __init__(self, lock_path: str | os.PathLike[str]) -> None:
        self._path = Path(lock_path)
        self._fd: int | None = None

    @property
    def held(self) -> bool:
        return self._fd is not None

    def acquire(self) -> None:
        if self._fd is not None:
            raise RunnerError("runner_lock_held")
        try:
            parent_identity = prepare_private_parent(
                self._path.parent,
                error_code="runner_lock_unavailable",
            )
            flags = os.O_CREAT | os.O_RDWR
            if hasattr(os, "O_CLOEXEC"):
                flags |= os.O_CLOEXEC
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            fd = os.open(self._path, flags, LOCK_FILE_MODE)
            metadata = os.fstat(fd)
            if not stat.S_ISREG(metadata.st_mode):
                os.close(fd)
                raise RunnerError("runner_lock_unavailable")
            os.fchmod(fd, LOCK_FILE_MODE)
        except RunnerError:
            raise
        except OSError as exc:
            raise RunnerError("runner_lock_unavailable") from exc
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            os.close(fd)
            if exc.errno in {errno.EACCES, errno.EAGAIN}:
                raise RunnerError("runner_lock_held") from exc
            raise RunnerError("runner_lock_unavailable") from exc
        try:
            path_metadata = self._path.lstat()
            opened_metadata = os.fstat(fd)
            verify_private_parent(
                self._path.parent,
                parent_identity,
                error_code="runner_lock_unavailable",
            )
            if (
                stat.S_ISLNK(path_metadata.st_mode)
                or (path_metadata.st_dev, path_metadata.st_ino)
                != (opened_metadata.st_dev, opened_metadata.st_ino)
            ):
                raise RunnerError("runner_lock_unavailable")
        except Exception:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)
            raise
        self._fd = fd

    def release(self) -> None:
        fd = self._fd
        if fd is None:
            return
        self._fd = None
        error: OSError | None = None
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError as exc:
            error = exc
        try:
            os.close(fd)
        except OSError as exc:
            error = error or exc
        if error is not None:
            raise RunnerError("runner_lock_release_failed") from error

    def __enter__(self) -> "ExclusiveLock":
        self.acquire()
        return self

    def __exit__(self, _exc_type: object, _exc: object, _tb: object) -> None:
        self.release()


__all__ = ["ExclusiveLock", "LOCK_FILE_MODE"]
