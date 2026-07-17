"""Fail-closed descriptor-relative I/O for generated contract artifacts."""
from __future__ import annotations

import errno
import os
import stat
import uuid
from pathlib import Path


def _close_all(*descriptors: int | None) -> None:
    """Attempt every close, then re-raise the first cleanup error."""
    first_error: BaseException | None = None
    for descriptor in descriptors:
        if descriptor is None:
            continue
        try:
            os.close(descriptor)
        except BaseException as exc:
            if first_error is None:
                first_error = exc
    if first_error is not None:
        raise first_error


def _absolute(path: Path) -> Path:
    return Path(os.path.abspath(os.fspath(path)))


def _directory_flags() -> int:
    flags = os.O_RDONLY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    return flags


def _file_flags() -> int:
    flags = os.O_RDONLY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    if hasattr(os, "O_NONBLOCK"):
        flags |= os.O_NONBLOCK
    return flags


def _open_directory_at(parent_fd: int, name: str, display: Path) -> int:
    try:
        descriptor = os.open(name, _directory_flags(), dir_fd=parent_fd)
    except OSError as error:
        if error.errno in {errno.ELOOP, errno.ENOTDIR}:
            raise ValueError(f"contract path contains symlink or non-directory: {display}") from error
        raise ValueError(f"contract directory is unavailable: {display}") from error
    try:
        if not stat.S_ISDIR(os.fstat(descriptor).st_mode):
            raise ValueError(f"contract path component is not a directory: {display}")
        return descriptor
    except Exception:
        os.close(descriptor)
        raise


def _open_absolute_directory(path: Path) -> int:
    absolute = _absolute(path)
    if not absolute.is_absolute():
        raise ValueError(f"contract directory must be absolute: {path}")
    descriptor = os.open(os.sep, _directory_flags())
    current = Path(os.sep)
    try:
        for part in absolute.parts[1:]:
            current /= part
            child = _open_directory_at(descriptor, part, current)
            os.close(descriptor)
            descriptor = child
        return descriptor
    except Exception:
        os.close(descriptor)
        raise


def _relative_parts(root: Path, target: Path, *, allow_directory: bool = False) -> tuple[Path, tuple[str, ...]]:
    root_absolute = _absolute(root)
    target_absolute = _absolute(target)
    try:
        relative = target_absolute.relative_to(root_absolute)
    except ValueError as error:
        raise ValueError(f"contract path escapes canonical root: {target}") from error
    parts = relative.parts
    if not parts and not allow_directory:
        raise ValueError("contract path must identify a file")
    if any(part in {"", ".", ".."} for part in parts):
        raise ValueError(f"contract path is not canonical: {target}")
    return root_absolute, parts


def _open_directory_under_root(root: Path, directory: Path) -> int:
    root_absolute, parts = _relative_parts(root, directory, allow_directory=True)
    descriptor = _open_absolute_directory(root_absolute)
    current = root_absolute
    try:
        for part in parts:
            current /= part
            child = _open_directory_at(descriptor, part, current)
            os.close(descriptor)
            descriptor = child
        return descriptor
    except Exception:
        os.close(descriptor)
        raise


def _open_parent_under_root(root: Path, target: Path) -> tuple[int, str]:
    root_absolute, parts = _relative_parts(root, target)
    descriptor = _open_absolute_directory(root_absolute)
    current = root_absolute
    try:
        for part in parts[:-1]:
            current /= part
            child = _open_directory_at(descriptor, part, current)
            os.close(descriptor)
            descriptor = child
        return descriptor, parts[-1]
    except Exception:
        os.close(descriptor)
        raise


def read_under_root(root: Path, target: Path) -> bytes:
    """Read one regular file without following any path-component symlink."""
    parent_fd, name = _open_parent_under_root(root, target)
    descriptor: int | None = None
    operation_failed = False
    try:
        try:
            descriptor = os.open(name, _file_flags(), dir_fd=parent_fd)
        except OSError as error:
            if error.errno in {errno.ELOOP, errno.ENOTDIR}:
                raise ValueError(f"contract input contains symlink: {target}") from error
            raise ValueError(f"contract input is unavailable: {target}") from error
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise ValueError(f"contract input must be a regular non-symlink file: {target}")
        chunks: list[bytes] = []
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                return b"".join(chunks)
            chunks.append(chunk)
    except BaseException:
        operation_failed = True
        raise
    finally:
        try:
            _close_all(descriptor, parent_fd)
        except BaseException:
            if not operation_failed:
                raise


def list_names_under_root(root: Path, directory: Path) -> set[str]:
    """List a real directory reached without following symlinks."""
    descriptor = _open_directory_under_root(root, directory)
    try:
        return set(os.listdir(descriptor))
    except OSError as error:
        raise ValueError(f"contract directory cannot be listed: {directory}") from error
    finally:
        os.close(descriptor)


def atomic_write_under_root(root: Path, target: Path, payload: bytes) -> None:
    """Atomically replace a regular file inside one already-open trusted parent."""
    parent_fd, name = _open_parent_under_root(root, target)
    temporary_name = f".{name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
    descriptor: int | None = None
    temporary_exists = False
    operation_failed = False
    try:
        try:
            existing = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            existing = None
        except OSError as error:
            raise ValueError(f"contract output cannot be inspected: {target}") from error
        if existing is not None and not stat.S_ISREG(existing.st_mode):
            raise ValueError(f"contract output must be a regular non-symlink file: {target}")

        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_CLOEXEC"):
            flags |= os.O_CLOEXEC
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            descriptor = os.open(temporary_name, flags, 0o644, dir_fd=parent_fd)
        except OSError as error:
            raise ValueError(f"contract temporary output cannot be created: {target}") from error
        temporary_exists = True
        offset = 0
        while offset < len(payload):
            written = os.write(descriptor, payload[offset:])
            if written <= 0:
                raise OSError("short write while generating contract artifact")
            offset += written
        os.fsync(descriptor)

        written_identity = os.fstat(descriptor)
        named_identity = os.stat(temporary_name, dir_fd=parent_fd, follow_symlinks=False)
        if (
            written_identity.st_dev != named_identity.st_dev
            or written_identity.st_ino != named_identity.st_ino
            or not stat.S_ISREG(named_identity.st_mode)
        ):
            raise ValueError("contract temporary output identity changed before replace")

        os.replace(temporary_name, name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
        temporary_exists = False
        installed_identity = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if (
            written_identity.st_dev != installed_identity.st_dev
            or written_identity.st_ino != installed_identity.st_ino
            or not stat.S_ISREG(installed_identity.st_mode)
        ):
            raise ValueError("contract output identity changed during replace")
        os.fsync(parent_fd)
    except BaseException:
        operation_failed = True
        raise
    finally:
        cleanup_error: BaseException | None = None
        try:
            _close_all(descriptor)
        except BaseException as exc:
            cleanup_error = exc
        if temporary_exists:
            try:
                os.unlink(temporary_name, dir_fd=parent_fd)
            except FileNotFoundError:
                pass
            except BaseException as exc:
                if cleanup_error is None:
                    cleanup_error = exc
        try:
            _close_all(parent_fd)
        except BaseException as exc:
            if cleanup_error is None:
                cleanup_error = exc
        if cleanup_error is not None and not operation_failed:
            raise cleanup_error
