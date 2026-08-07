"""Owner-only directory checks for runner lock and state artifacts."""
from __future__ import annotations

import os
import stat
from pathlib import Path

from .errors import RunnerError

PRIVATE_DIRECTORY_MODE = 0o700


def prepare_private_parent(parent: Path, *, error_code: str) -> tuple[int, int]:
    try:
        parent.mkdir(mode=PRIVATE_DIRECTORY_MODE, parents=True, exist_ok=True)
        metadata = parent.lstat()
        if (
            stat.S_ISLNK(metadata.st_mode)
            or not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid != os.getuid()
        ):
            raise RunnerError(error_code)
        parent.chmod(PRIVATE_DIRECTORY_MODE, follow_symlinks=False)
        metadata = parent.lstat()
        if stat.S_IMODE(metadata.st_mode) != PRIVATE_DIRECTORY_MODE:
            raise RunnerError(error_code)
        return metadata.st_dev, metadata.st_ino
    except RunnerError:
        raise
    except OSError as exc:
        raise RunnerError(error_code) from exc


def verify_private_parent(
    parent: Path,
    identity: tuple[int, int],
    *,
    error_code: str,
) -> None:
    try:
        metadata = parent.lstat()
    except OSError as exc:
        raise RunnerError(error_code) from exc
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or stat.S_IMODE(metadata.st_mode) != PRIVATE_DIRECTORY_MODE
        or (metadata.st_dev, metadata.st_ino) != identity
    ):
        raise RunnerError(error_code)


__all__ = ["PRIVATE_DIRECTORY_MODE", "prepare_private_parent", "verify_private_parent"]
