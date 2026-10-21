# SPDX-License-Identifier: AGPL-3.0-only
"""POSIX backend: descriptor-bound private directories and atomic replacement.

This backend exposes the same operations as :mod:`._win32` using the POSIX
primitives the existing node modules already use (``O_NOFOLLOW`` +
``O_DIRECTORY`` walks, retained directory descriptors, mode ``0700``/``0600``
plus owner uid, ``O_EXCL`` temp + ``fsync`` + ``rename`` + directory
``fsync``).  The existing modules keep their own POSIX bodies unchanged; this
backend lets new, backend-neutral callers use one API on both hosts and is
held to parity with those bodies by ``tests/host_platform``.
"""

from __future__ import annotations

import os
from pathlib import Path
import stat
from contextlib import contextmanager
from typing import BinaryIO, Iterator
import uuid

from ._common import ExecutableFingerprint, FileIdentity, HostPlatformError


_DIRECTORY_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_CLOEXEC", 0)
    | getattr(os, "O_DIRECTORY", 0)
    | getattr(os, "O_NOFOLLOW", 0)
)
_FILE_READ_FLAGS = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
_FILE_CREATE_FLAGS = (
    os.O_WRONLY
    | os.O_CREAT
    | os.O_EXCL
    | getattr(os, "O_CLOEXEC", 0)
    | getattr(os, "O_NOFOLLOW", 0)
)


def _component(name: str) -> str:
    if (
        not isinstance(name, str)
        or not name
        or name in {".", ".."}
        or "/" in name
        or "\x00" in name
    ):
        raise HostPlatformError("host_path_invalid", "component")
    return name


def _private_directory_metadata(metadata: os.stat_result) -> bool:
    return (
        stat.S_ISDIR(metadata.st_mode)
        and metadata.st_uid == os.getuid()
        and stat.S_IMODE(metadata.st_mode) == 0o700
    )


class PosixPrivateDirectory:
    """One leased ``0700`` directory held open by descriptor."""

    def __init__(self, path: str, *, parent_fd: int, fd: int | None) -> None:
        self.path = path
        self._parent_fd: int | None = parent_fd
        self._fd = fd
        self._closed = False
        self._identity: FileIdentity | None = None
        if fd is not None:
            metadata = os.fstat(fd)
            self._identity = FileIdentity(metadata.st_dev, metadata.st_ino)

    @property
    def exists(self) -> bool:
        return self._fd is not None

    @property
    def identity(self) -> FileIdentity | None:
        return self._identity

    def _require_fd(self) -> int:
        if self._closed or self._fd is None:
            raise HostPlatformError("host_path_invalid", "directory_unavailable")
        return self._fd

    def revalidate(self) -> None:
        descriptor = self._require_fd()
        assert self._parent_fd is not None
        try:
            retained = os.fstat(descriptor)
            entry = os.stat(
                Path(self.path).name, dir_fd=self._parent_fd, follow_symlinks=False
            )
        except OSError as exc:
            raise HostPlatformError("host_path_invalid", "identity_changed") from exc
        if (
            not _private_directory_metadata(retained)
            or FileIdentity(retained.st_dev, retained.st_ino) != self._identity
            or (entry.st_dev, entry.st_ino) != (retained.st_dev, retained.st_ino)
        ):
            raise HostPlatformError("host_permissions_invalid", "directory_changed")

    def subdirectory(self, name: str) -> "PosixPrivateDirectory":
        self.revalidate()
        return open_private_directory(
            os.path.join(self.path, _component(name)), create=True
        )

    def read_file(self, name: str, *, max_bytes: int) -> bytes:
        descriptor = self._require_fd()
        self.revalidate()
        name = _component(name)
        try:
            metadata = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
        except FileNotFoundError as exc:
            raise HostPlatformError("host_file_missing") from exc
        except OSError as exc:
            raise HostPlatformError("host_io_failed", "stat") from exc
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise HostPlatformError("host_file_invalid", "not_plain_file")
        if metadata.st_uid != os.getuid() or stat.S_IMODE(metadata.st_mode) != 0o600:
            raise HostPlatformError("host_permissions_invalid", "file_mode")
        if metadata.st_size > max_bytes:
            raise HostPlatformError("host_file_invalid", "size")
        try:
            file_fd = os.open(name, _FILE_READ_FLAGS, dir_fd=descriptor)
        except OSError as exc:
            raise HostPlatformError("host_io_failed", "open") from exc
        try:
            opened = os.fstat(file_fd)
            if (opened.st_dev, opened.st_ino) != (metadata.st_dev, metadata.st_ino):
                raise HostPlatformError("host_file_invalid", "changed_during_open")
            chunks: list[bytes] = []
            remaining = max_bytes + 1
            while remaining > 0:
                chunk = os.read(file_fd, min(64 * 1024, remaining))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            raw = b"".join(chunks)
            after = os.fstat(file_fd)
        except OSError as exc:
            raise HostPlatformError("host_io_failed", "read") from exc
        finally:
            os.close(file_fd)
        if (
            len(raw) != opened.st_size
            or len(raw) > max_bytes
            or (after.st_size, after.st_mtime_ns, after.st_ctime_ns)
            != (opened.st_size, opened.st_mtime_ns, opened.st_ctime_ns)
        ):
            raise HostPlatformError("host_file_invalid", "changed_during_read")
        return raw

    def _write_new(self, name: str, data: bytes) -> None:
        descriptor = self._require_fd()
        try:
            file_fd = os.open(name, _FILE_CREATE_FLAGS, 0o600, dir_fd=descriptor)
        except FileExistsError as exc:
            raise HostPlatformError("host_file_exists") from exc
        except OSError as exc:
            raise HostPlatformError("host_io_failed", "create") from exc
        try:
            os.fchmod(file_fd, 0o600)
            written = 0
            while written < len(data):
                count = os.write(file_fd, data[written:])
                if count <= 0:
                    raise OSError("short write")
                written += count
            os.fsync(file_fd)
        except OSError as exc:
            raise HostPlatformError("host_io_failed", "write") from exc
        finally:
            os.close(file_fd)

    def _unlink_quietly(self, name: str) -> None:
        try:
            os.unlink(name, dir_fd=self._require_fd())
        except (OSError, HostPlatformError):
            pass

    @contextmanager
    def exclusive_lock(self, name: str, *, blocking: bool = True) -> Iterator[None]:
        """Hold an exclusive advisory lock on the owner-only file ``name``.

        Non-blocking contention raises ``host_lock_busy``.  The lock file is
        created ``0600`` beneath this directory and never followed as a link.
        """

        import fcntl

        descriptor_dir = self._require_fd()
        self.revalidate()
        name = _component(name)
        try:
            descriptor = os.open(
                name,
                os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0),
                0o600,
                dir_fd=descriptor_dir,
            )
        except OSError as exc:
            raise HostPlatformError("host_io_failed", "lock_open") from exc
        try:
            metadata = os.fstat(descriptor)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_uid != os.getuid()
                or stat.S_IMODE(metadata.st_mode) & 0o077
            ):
                raise HostPlatformError("host_permissions_invalid", "lock_file")
            try:
                fcntl.flock(
                    descriptor, fcntl.LOCK_EX if blocking else fcntl.LOCK_EX | fcntl.LOCK_NB
                )
            except BlockingIOError as exc:
                raise HostPlatformError("host_lock_busy") from exc
            except OSError as exc:
                raise HostPlatformError("host_io_failed", "lock") from exc
            yield
        finally:
            os.close(descriptor)

    def create_file_exclusive(self, name: str, data: bytes) -> None:
        self.revalidate()
        name = _component(name)
        try:
            self._write_new(name, data)
        except HostPlatformError as exc:
            if exc.code != "host_file_exists":
                self._unlink_quietly(name)
            raise
        self.flush()

    def atomic_replace(self, name: str, data: bytes) -> None:
        self.revalidate()
        name = _component(name)
        temporary = f".{name}.{uuid.uuid4().hex}.tmp"
        descriptor = self._require_fd()
        try:
            self._write_new(temporary, data)
            try:
                os.replace(
                    temporary, name, src_dir_fd=descriptor, dst_dir_fd=descriptor
                )
            except OSError as exc:
                raise HostPlatformError("host_io_failed", "replace") from exc
            self.flush()
        finally:
            self._unlink_quietly(temporary)
        self.revalidate()

    def flush(self) -> None:
        try:
            os.fsync(self._require_fd())
        except OSError as exc:
            raise HostPlatformError("host_io_failed", "flush") from exc

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        failures: list[OSError] = []
        for descriptor in (self._fd, self._parent_fd):
            if descriptor is None:
                continue
            try:
                os.close(descriptor)
            except OSError as exc:
                failures.append(exc)
        self._fd = None
        self._parent_fd = None
        if failures:
            raise HostPlatformError("host_io_failed", "close") from failures[0]

    def __enter__(self) -> "PosixPrivateDirectory":
        if self._closed:
            raise HostPlatformError("host_path_invalid", "directory_unavailable")
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


def open_private_directory(
    path: str | os.PathLike[str], *, create: bool
) -> PosixPrivateDirectory:
    if not getattr(os, "O_NOFOLLOW", 0) or not getattr(os, "O_DIRECTORY", 0):
        raise HostPlatformError("host_platform_unavailable", "no_follow")
    target = os.path.normpath(os.fspath(path))
    if not os.path.isabs(target) or target == "/":
        raise HostPlatformError("host_path_invalid", "path")
    parts = Path(target).parts
    descriptor: int | None = None
    try:
        descriptor = os.open("/", _DIRECTORY_FLAGS)
        for index, part in enumerate(parts[1:], start=1):
            final = index == len(parts) - 1
            try:
                child = os.open(part, _DIRECTORY_FLAGS, dir_fd=descriptor)
            except FileNotFoundError:
                if final and not create:
                    lease = PosixPrivateDirectory(target, parent_fd=descriptor, fd=None)
                    descriptor = None
                    return lease
                if not create:
                    raise HostPlatformError("host_path_invalid", "ancestor") from None
                try:
                    os.mkdir(part, mode=0o700, dir_fd=descriptor)
                except FileExistsError:
                    pass
                child = os.open(part, _DIRECTORY_FLAGS, dir_fd=descriptor)
                if os.fstat(child).st_uid == os.getuid():
                    os.fchmod(child, 0o700)
            metadata = os.fstat(child)
            if final:
                if not _private_directory_metadata(metadata):
                    os.close(child)
                    raise HostPlatformError("host_permissions_invalid", "directory_mode")
                lease = PosixPrivateDirectory(target, parent_fd=descriptor, fd=child)
                descriptor = None
                try:
                    lease.revalidate()
                except BaseException:
                    lease.close()
                    raise
                return lease
            if not stat.S_ISDIR(metadata.st_mode):
                os.close(child)
                raise HostPlatformError("host_path_invalid", "ancestor")
            os.close(descriptor)
            descriptor = child
        raise HostPlatformError("host_path_invalid", "path")
    except OSError as exc:
        raise HostPlatformError("host_path_invalid", "walk") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)


def open_beneath(root: str | os.PathLike[str], relative_parts: tuple[str, ...]) -> BinaryIO:
    if not relative_parts:
        raise HostPlatformError("host_path_invalid", "empty")
    try:
        root_fd = os.open(os.fspath(root), _DIRECTORY_FLAGS)
    except OSError as exc:
        raise HostPlatformError("host_path_invalid", "root") from exc
    current_fd = root_fd
    file_fd: int | None = None
    try:
        for part in relative_parts[:-1]:
            child_fd = os.open(_component(part), _DIRECTORY_FLAGS, dir_fd=current_fd)
            if current_fd != root_fd:
                os.close(current_fd)
            current_fd = child_fd
        file_fd = os.open(
            _component(relative_parts[-1]), _FILE_READ_FLAGS, dir_fd=current_fd
        )
        if not stat.S_ISREG(os.fstat(file_fd).st_mode):
            raise HostPlatformError("host_file_invalid", "not_plain_file")
        handle = os.fdopen(file_fd, "rb")
        file_fd = None
        return handle
    except FileNotFoundError as exc:
        raise HostPlatformError("host_file_missing") from exc
    except OSError as exc:
        raise HostPlatformError("host_path_invalid", "walk") from exc
    finally:
        if file_fd is not None:
            os.close(file_fd)
        if current_fd != root_fd:
            os.close(current_fd)
        os.close(root_fd)


def read_owner_file(path: str | os.PathLike[str], *, max_bytes: int) -> bytes:
    target = os.fspath(path)
    try:
        descriptor = os.open(target, _FILE_READ_FLAGS)
    except FileNotFoundError as exc:
        raise HostPlatformError("host_file_missing") from exc
    except OSError as exc:
        raise HostPlatformError("host_io_failed", "open") from exc
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise HostPlatformError("host_file_invalid", "not_plain_file")
        if before.st_uid != os.getuid() or stat.S_IMODE(before.st_mode) & 0o077:
            raise HostPlatformError("host_permissions_invalid", "file_mode")
        if before.st_size <= 0 or before.st_size > max_bytes:
            raise HostPlatformError("host_file_invalid", "size")
        raw = os.read(descriptor, max_bytes + 1)
        after = os.fstat(descriptor)
    except OSError as exc:
        raise HostPlatformError("host_io_failed", "read") from exc
    finally:
        os.close(descriptor)
    if len(raw) != before.st_size or (after.st_size, after.st_mtime_ns) != (
        before.st_size,
        before.st_mtime_ns,
    ):
        raise HostPlatformError("host_file_invalid", "changed_during_read")
    return raw


def executable_fingerprint(
    path: str | os.PathLike[str], *, require_private_owner: bool
) -> ExecutableFingerprint:
    target = os.path.normpath(os.fspath(path))
    try:
        descriptor = os.open(target, _FILE_READ_FLAGS)
    except FileNotFoundError as exc:
        raise HostPlatformError("host_file_missing") from exc
    except OSError as exc:
        raise HostPlatformError("host_io_failed", "open") from exc
    try:
        metadata = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    if not stat.S_ISREG(metadata.st_mode):
        raise HostPlatformError("host_file_invalid", "not_plain_file")
    if require_private_owner and (
        metadata.st_uid != os.getuid() or stat.S_IMODE(metadata.st_mode) & 0o022
    ):
        raise HostPlatformError("host_permissions_invalid", "executable_mode")
    return ExecutableFingerprint(
        path=target,
        volume=metadata.st_dev,
        index=metadata.st_ino,
        size=metadata.st_size,
        last_write_ns=metadata.st_mtime_ns,
        creation_ns=metadata.st_ctime_ns,
    )


__all__ = [
    "PosixPrivateDirectory",
    "executable_fingerprint",
    "open_beneath",
    "open_private_directory",
    "read_owner_file",
]
