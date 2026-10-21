# SPDX-License-Identifier: AGPL-3.0-only
"""Backend-neutral error and value types for the host platform seam."""

from __future__ import annotations

from dataclasses import dataclass


#: Every code a backend may raise.  Callers map these onto their own stable
#: error vocabulary; an unmapped code is a programming error, never a pass.
ERROR_CODES: frozenset[str] = frozenset(
    {
        "host_path_invalid",
        "host_permissions_invalid",
        "host_file_missing",
        "host_file_exists",
        "host_file_invalid",
        "host_io_failed",
        "host_replace_contended",
        "host_platform_unavailable",
        "host_job_unavailable",
        "host_process_identity_invalid",
        "host_lock_busy",
    }
)


class HostPlatformError(RuntimeError):
    """Stable, fail-closed host platform failure."""

    def __init__(self, code: str, detail: str = "") -> None:
        if code not in ERROR_CODES:
            raise ValueError(f"unknown host platform error code: {code}")
        self.code = code
        self.detail = detail
        super().__init__(code if not detail else f"{code}:{detail}")


@dataclass(frozen=True)
class FileIdentity:
    """Stable identity of one opened filesystem object on either backend.

    POSIX: ``(st_dev, st_ino)``.  win32: ``(dwVolumeSerialNumber,
    nFileIndexHigh << 32 | nFileIndexLow)`` read from an open handle.
    """

    volume: int
    index: int


@dataclass(frozen=True)
class ExecutableFingerprint:
    """Handle-bound fingerprint of one executable file."""

    path: str
    volume: int
    index: int
    size: int
    last_write_ns: int
    creation_ns: int


__all__ = ["ERROR_CODES", "ExecutableFingerprint", "FileIdentity", "HostPlatformError"]
