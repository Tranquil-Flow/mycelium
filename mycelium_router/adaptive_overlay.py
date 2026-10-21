# SPDX-License-Identifier: AGPL-3.0-only
"""A16 adaptive host-resource overlay: the one small file the owner process publishes and
the node's KV reservation port reads.

The file lives in the node's private ``capacity_root``.  It is an *expiring, allow-listed*
document: a coarse mode/reason, a generation, whether new work may be admitted, and the
byte budget of *new* KV this host may still reserve.  It never carries application
identity, process names, window titles or a raw memory map.

Semantics (fail closed, opt-out unchanged):

* no file                -> A16 is not active; ``read_gate`` returns ``None`` and the
                            node behaves exactly as it did before A16.
* valid, unexpired file  -> the gate dict (``admit``, ``refusal``, ``new_kv_budget_bytes``,
                            ``epoch``).
* anything else present  -> a refusing gate (``adaptive_controller_stale``): a symlink, a
                            loose mode, a foreign owner, oversize, malformed, schema
                            mismatch or an expired lease all fence *new* reservations.
                            Existing reservations are never revoked by this module.
* withdraw marker present -> ``adaptive-withdraw-marker.json`` (same directory) records that
                            this node withdrew.  *Any* marker, valid or not, fences new
                            reservations (``adaptive_host_withdrawn``) even over a permissive
                            overlay; it is removed only by the owner process once re-entry is
                            complete (or A16 is switched off), and a restarted owner process
                            reads it to start fenced.
"""
from __future__ import annotations

import json
import math
import os
import stat
import time
from pathlib import Path
from typing import Any, Callable, Final, Mapping

OVERLAY_FILENAME: Final = "adaptive-host-overlay.json"
MAX_OVERLAY_BYTES: Final = 2048
MODES: Final = ("normal", "reduced", "drain", "withdraw")
SCALES: Final = ("zero", "committed_only", "reduced", "full")
REFUSALS: Final = frozenset({
    "adaptive_host_withdrawn", "adaptive_host_draining", "adaptive_controller_stale",
    "adaptive_signal_unknown", "adaptive_policy_invalid",
})
MARKER_FILENAME: Final = "adaptive-withdraw-marker.json"
MAX_MARKER_BYTES: Final = 512
MARKER_TTL_S: Final = 30.0
MARKER_KEYS: Final = frozenset({"schema", "reason", "withdrawn_at", "expires_at"})
OVERLAY_KEYS: Final = frozenset({
    "schema", "mode", "reason", "generation", "admit", "refusal", "capacity_scale",
    "new_kv_budget_bytes", "epoch", "issued_at", "expires_at",
})
_REASONS: Final = frozenset({
    "none", "owner_game_mode", "owner_stop", "host_memory_pressure", "host_cpu_saturation",
})
_MAX_BYTES: Final = 1 << 50
_FUTURE_TOLERANCE_S: Final = 2.0


class AdaptiveOverlayError(ValueError):
    def __init__(self, code: str = "adaptive_overlay_invalid") -> None:
        super().__init__(code)
        self.code = code


def _number(value: object) -> bool:
    return type(value) in (int, float) and math.isfinite(value)


def validate_overlay(doc: object) -> dict[str, Any]:
    bad = AdaptiveOverlayError()
    if not isinstance(doc, Mapping) or set(doc) != OVERLAY_KEYS:
        raise bad
    if doc["schema"] != 1 or type(doc["schema"]) is not int:
        raise bad
    if doc["mode"] not in MODES or doc["reason"] not in _REASONS or doc["capacity_scale"] not in SCALES:
        raise bad
    if type(doc["generation"]) is not int or doc["generation"] < 0 or type(doc["admit"]) is not bool:
        raise bad
    if doc["refusal"] is not None and doc["refusal"] not in REFUSALS:
        raise bad
    if doc["admit"] == (doc["refusal"] is not None):
        raise bad  # admit <=> no refusal
    budget = doc["new_kv_budget_bytes"]
    if budget is not None and (type(budget) is not int or not 0 <= budget <= _MAX_BYTES):
        raise bad
    if not (_number(doc["epoch"]) and _number(doc["issued_at"]) and _number(doc["expires_at"])):
        raise bad
    return dict(doc)


def _directory_file(directory: str | os.PathLike[str]) -> Path:
    return Path(directory) / OVERLAY_FILENAME


def write_overlay(directory: str | os.PathLike[str], doc: Mapping[str, Any]) -> None:
    """Atomically publish ``doc`` (0600); refuses any non-allow-listed document."""
    raw = json.dumps(validate_overlay(doc), sort_keys=True, allow_nan=False).encode("utf-8")
    path = _directory_file(directory)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def clear_overlay(directory: str | os.PathLike[str]) -> None:
    _directory_file(directory).unlink(missing_ok=True)


def fence_document(refusal: str, *, wall_now: float, generation: int = 0) -> dict[str, Any]:
    """A refusing overlay that is already at its expiry bound (used on publisher failure)."""
    return {
        "schema": 1, "mode": "drain", "reason": "none", "generation": generation, "admit": False,
        "refusal": refusal, "capacity_scale": "zero", "new_kv_budget_bytes": 0,
        "epoch": wall_now, "issued_at": wall_now, "expires_at": wall_now + 6.0,
    }


def _refusing(refusal: str = "adaptive_controller_stale") -> dict[str, Any]:
    return {"admit": False, "refusal": refusal, "new_kv_budget_bytes": 0, "epoch": None}


def _marker_file(directory: str | os.PathLike[str]) -> Path:
    return Path(directory) / MARKER_FILENAME


def write_marker(directory: str | os.PathLike[str], *, reason: object, wall_now: float) -> None:
    """Atomically (re)write the 0600 withdraw marker; refuses any non-allow-listed value."""
    if reason not in _REASONS or not _number(wall_now):
        raise AdaptiveOverlayError()
    doc = {"schema": 1, "reason": reason, "withdrawn_at": float(wall_now),
           "expires_at": float(wall_now) + MARKER_TTL_S}
    raw = json.dumps(doc, sort_keys=True, allow_nan=False).encode("utf-8")
    path = _marker_file(directory)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def clear_marker(directory: str | os.PathLike[str]) -> None:
    _marker_file(directory).unlink(missing_ok=True)  # never follows a symlink


def marker_present(directory: str | os.PathLike[str]) -> bool:
    return os.path.lexists(_marker_file(directory))


def read_marker(
    directory: str | os.PathLike[str], *, wall_clock: Callable[[], float] = time.time
) -> dict[str, Any] | None:
    """``None`` when no marker exists, else ``{"valid", "reason"}``; any doubt is ``valid=False``."""
    path = _marker_file(directory)
    unusable = {"valid": False, "reason": "none"}
    if not os.path.lexists(path):
        return None
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError:
        return unusable
    try:
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o600 or info.st_size > MAX_MARKER_BYTES
        ):
            return unusable
        raw = os.read(fd, MAX_MARKER_BYTES + 1)
    except OSError:
        return unusable
    finally:
        os.close(fd)
    try:
        doc = json.loads(raw)
        now = wall_clock()
        if (
            not isinstance(doc, Mapping) or set(doc) != MARKER_KEYS or doc["schema"] != 1
            or type(doc["schema"]) is not int or doc["reason"] not in _REASONS
            or not _number(doc["withdrawn_at"]) or not _number(doc["expires_at"])
            or now > doc["expires_at"] or doc["withdrawn_at"] > now + _FUTURE_TOLERANCE_S
        ):
            return unusable
    except (ValueError, TypeError):
        return unusable
    return {"valid": True, "reason": doc["reason"]}


def read_gate(
    directory: str | os.PathLike[str], *, wall_clock: Callable[[], float] = time.time
) -> dict[str, Any] | None:
    """``None`` when A16 is inactive here, else the gate (refusing on any doubt)."""
    gate = _read_overlay_gate(directory, wall_clock)
    if marker_present(directory) and (gate is None or gate["admit"]):
        return _refusing("adaptive_host_withdrawn")  # a withdraw marker outranks a permissive overlay
    return gate


def _read_overlay_gate(
    directory: str | os.PathLike[str], wall_clock: Callable[[], float]
) -> dict[str, Any] | None:
    path = _directory_file(directory)
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return None
    except OSError:
        return _refusing()
    try:
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o600 or info.st_size > MAX_OVERLAY_BYTES
        ):
            return _refusing()
        raw = os.read(fd, MAX_OVERLAY_BYTES + 1)
    except OSError:
        return _refusing()
    finally:
        os.close(fd)
    try:
        doc = validate_overlay(json.loads(raw))
        now = wall_clock()
        if now > doc["expires_at"] or doc["issued_at"] > now + _FUTURE_TOLERANCE_S:
            return _refusing()
    except (ValueError, TypeError):
        return _refusing()
    return {
        "admit": doc["admit"], "refusal": doc["refusal"],
        "new_kv_budget_bytes": doc["new_kv_budget_bytes"], "epoch": doc["epoch"],
    }
