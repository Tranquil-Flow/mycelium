"""Atomic private persistence for bounded runner state."""
from __future__ import annotations

import json
import os
import stat
import tempfile
from pathlib import Path
from typing import Any

from .errors import RunnerError
from .private_fs import prepare_private_parent, verify_private_parent
from .state import RunStateDocument

STATE_FILE_MODE = 0o600
MAX_STATE_BYTES = 64 * 1024


class StateStore:
    def __init__(self, *, state_path: str | os.PathLike[str]) -> None:
        self._state_path = Path(state_path)

    @property
    def state_path(self) -> Path:
        return self._state_path

    def write(self, document: RunStateDocument) -> None:
        if not isinstance(document, RunStateDocument):
            raise RunnerError("state_invalid", "document_type")
        payload = json.dumps(document.to_dict(), sort_keys=True, separators=(",", ":")).encode("utf-8")
        if len(payload) > MAX_STATE_BYTES:
            raise RunnerError("state_too_large")
        parent = self._state_path.parent
        temp_path: Path | None = None
        try:
            parent_identity = prepare_private_parent(parent, error_code="state_write_failed")
            fd, raw_temp = tempfile.mkstemp(prefix=f".{self._state_path.name}.", suffix=".tmp", dir=parent)
            temp_path = Path(raw_temp)
            os.fchmod(fd, STATE_FILE_MODE)
            with os.fdopen(fd, "wb", closefd=True) as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_path, self._state_path)
            temp_path = None
            os.chmod(self._state_path, STATE_FILE_MODE, follow_symlinks=False)
            verify_private_parent(parent, parent_identity, error_code="state_write_failed")
            metadata = self._state_path.lstat()
            if (
                stat.S_ISLNK(metadata.st_mode)
                or not stat.S_ISREG(metadata.st_mode)
                or metadata.st_uid != os.getuid()
                or metadata.st_nlink != 1
                or stat.S_IMODE(metadata.st_mode) != STATE_FILE_MODE
            ):
                raise RunnerError("state_write_failed")
            directory_fd = os.open(parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError as exc:
            if temp_path is not None:
                try:
                    temp_path.unlink(missing_ok=True)
                except OSError:
                    pass
            raise RunnerError("state_write_failed") from exc

    def read(self) -> dict[str, Any] | None:
        flags = os.O_RDONLY
        if hasattr(os, "O_CLOEXEC"):
            flags |= os.O_CLOEXEC
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            fd = os.open(self._state_path, flags)
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise RunnerError("state_unavailable") from exc
        try:
            metadata = os.fstat(fd)
            if not stat.S_ISREG(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) != STATE_FILE_MODE:
                raise RunnerError("state_unavailable")
            with os.fdopen(fd, "rb", closefd=True) as handle:
                fd = -1
                raw = handle.read(MAX_STATE_BYTES + 1)
        finally:
            if fd >= 0:
                os.close(fd)
        if len(raw) > MAX_STATE_BYTES:
            raise RunnerError("state_too_large")
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RunnerError("state_corrupt") from exc
        if not isinstance(value, dict):
            raise RunnerError("state_corrupt")
        canonical = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
        if canonical != raw:
            raise RunnerError("state_corrupt")
        return value


__all__ = ["MAX_STATE_BYTES", "STATE_FILE_MODE", "StateStore"]
