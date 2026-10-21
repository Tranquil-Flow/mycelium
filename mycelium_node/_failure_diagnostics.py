# SPDX-License-Identifier: AGPL-3.0-only
"""One-use, opt-in private failure record for the node entrypoint.

An exclusive target is consumed by one invocation, not a persistent restart log.
The sink is deliberately POSIX-only until a Windows ACL/handle equivalent exists.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import stat

import mycelium_host_platform
from mycelium_seed.http import JOIN_ROUTE_ERROR_STATUSES, SeedHTTPError

from .process import private_directory_lease

_PROTOCOL = "mycelium.node_private_failure.v1"
_CLASSES = frozenset({ValueError, TypeError, RuntimeError, OSError, KeyError,
                      AssertionError, TimeoutError, PermissionError, FileNotFoundError,
                      SeedHTTPError})
_CODES = frozenset(JOIN_ROUTE_ERROR_STATUSES) | frozenset({
    "seed_http_unreachable", "seed_http_response_too_large", "seed_http_remote_error",
    "seed_http_response_invalid", "seed_http_json_invalid", "seed_http_noncanonical",
    "seed_http_seed_pin_mismatch", "seed_http_seed_signature_invalid",
    "seed_http_seed_envelope_invalid", "seed_http_seed_time_invalid",
    "seed_http_lease_renewal_mismatch", "seed_http_receipt_mismatch",
    "seed_http_member_envelope_invalid", "seed_http_body_invalid",
    "seed_http_body_truncated", "seed_http_content_length_invalid",
    "seed_http_content_length_required", "seed_http_content_type_invalid",
    "seed_http_frame_too_large", "seed_http_protocol_invalid", "seed_http_route_unknown",
    "seed_http_seed_key_invalid", "seed_http_transfer_encoding_unsupported",
    "seed_operator_rotation_record_invalid",
    "seed_member_unknown", "seed_message_replayed",
    "seed_resume_conflict", "seed_resume_generation_stale",
    "seed_resume_identity_mismatch", "seed_resume_member_revoked",
    "seed_resume_request_invalid", "seed_resume_retry_mismatch",
})
_PHASES = frozenset({"initial_identity", "local_setup", "membership_exchange",
                     "lease_renewal", "runtime", "state_scope", "preflight"})


def open_failure_sink(value: str) -> int:
    """Create a non-following 0600 file in an existing owner-only directory."""
    if mycelium_host_platform.is_win32() or not os.O_NOFOLLOW or not os.O_DIRECTORY:
        raise ValueError("failure diagnostics sink unavailable")
    if (not isinstance(value, str) or not value
            or any(ord(character) < 0x20 or ord(character) == 0x7f for character in value)):
        raise ValueError("failure diagnostics sink invalid")
    path = Path(value)
    if (not path.is_absolute() or path.name in {"", ".", ".."}
            or value != os.path.abspath(value) or path == Path(path.anchor)):
        raise ValueError("failure diagnostics sink invalid")
    parent = private_directory_lease(path.parent, create=False)
    fd: int | None = None
    try:
        parent.revalidate()
        fd = os.open(path.name, os.O_WRONLY | os.O_CREAT | os.O_EXCL |
                     os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0), 0o600,
                     dir_fd=parent._require_open_descriptor())
        os.fchmod(fd, 0o600)
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != 0o600):
            raise ValueError("failure diagnostics sink invalid")
        parent.revalidate()
        return fd
    except BaseException:
        if fd is not None:
            os.close(fd)
            fd = None
        raise
    finally:
        try:
            parent.close()
        except BaseException:
            if fd is not None:
                os.close(fd)
            raise


@dataclass(frozen=True)
class FailureRecord:
    phase: str
    exception_class: str
    seed_code: str | None = None
    http_status: int | None = None


def describe_failure(phase: str, exc: BaseException | None) -> FailureRecord:
    """Detach allowlisted scalar metadata; never retain the exception graph."""
    code = None
    status = None
    if type(exc) is SeedHTTPError:
        if type(exc.code) is str and exc.code in _CODES:
            code = exc.code
        if type(exc.status) is int and 400 <= exc.status <= 599:
            status = exc.status
    return FailureRecord(
        phase if phase in _PHASES else "runtime",
        type(exc).__name__ if type(exc) in _CLASSES else "Other",
        code, status,
    )


def write_failure(fd: int, record: FailureRecord) -> None:
    """Emit only fixed schema values; caller treats write errors as secondary."""
    names = {kind.__name__ for kind in _CLASSES}
    data: dict[str, str | int] = {
        "protocol": _PROTOCOL,
        "phase": record.phase if record.phase in _PHASES else "runtime",
        "exception_class": record.exception_class if record.exception_class in names else "Other",
    }
    if type(record.seed_code) is str and record.seed_code in _CODES:
        data["seed_code"] = record.seed_code
    if type(record.http_status) is int and 400 <= record.http_status <= 599:
        data["http_status"] = record.http_status
    payload = (json.dumps(data, sort_keys=True, separators=(",", ":")) + "\n").encode("ascii")
    if len(payload) > 256:
        return
    os.write(fd, payload)
