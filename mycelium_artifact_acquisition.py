"""Assignment-owned, content-addressed local artifact acquisition for M17.

This module never contacts a model registry.  It materializes only the immutable
whole files named by one layer assignment from an already-present revision-local
snapshot into the exact artifact root consumed by verification and runtime load.
"""

from __future__ import annotations

import ctypes
import errno
import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import stat
import sys
import time
from typing import Any, Callable
import uuid

from weight_provisioning import validate_provisioning_assignment


ACQUISITION_PROTOCOL = "mycelium.assignment_acquisition.v1"
_CHUNK_BYTES = 4 * 1024 * 1024
_SAFE_RELATIVE = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._-/")


class AcquisitionError(RuntimeError):
    """Bounded acquisition failure with an explicit retry classification."""

    def __init__(self, code: str, *, retryable: bool) -> None:
        super().__init__(code)
        self.code = code
        self.retryable = retryable


def _canonical(value: object) -> bytes:
    return (
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(_CHUNK_BYTES):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_relative(raw: object) -> Path:
    if not isinstance(raw, str) or not raw or len(raw) > 1_024:
        raise AcquisitionError("artifact_path_invalid", retryable=False)
    if any(character not in _SAFE_RELATIVE for character in raw):
        raise AcquisitionError("artifact_path_invalid", retryable=False)
    pure = PurePosixPath(raw)
    if pure.is_absolute() or any(part in {"", ".", ".."} for part in pure.parts):
        raise AcquisitionError("artifact_path_invalid", retryable=False)
    return Path(*pure.parts)


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _atomic_json(path: Path, value: object) -> None:
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("xb") as handle:
            handle.write(_canonical(value))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _assignment_marker(assignment: dict[str, Any]) -> dict[str, object]:
    return {
        "protocol": "mycelium.assignment_artifact_root.v1",
        "assignment_id": assignment["assignment_id"],
        "deployment_id": assignment["deployment_id"],
        "deployment_epoch": assignment["deployment_epoch"],
        "node_id": assignment["node_id"],
        "model_id": assignment["model_id"],
        "resolved_commit": assignment["resolved_commit"],
        "manifest_digest": assignment["manifest_digest"],
        "route_ready": False,
    }


def _claim_root(root: Path, assignment: dict[str, Any]) -> tuple[int, Path, Path]:
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    resolved = root.resolve(strict=True)
    if resolved != root or root.is_symlink():
        raise AcquisitionError("artifact_root_not_canonical", retryable=False)
    control = root / ".mycelium"
    objects = control / "objects" / "sha256"
    quarantine = control / "quarantine"
    objects.mkdir(parents=True, exist_ok=True, mode=0o700)
    quarantine.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock_path = control / "acquire.lock"
    descriptor = os.open(
        lock_path,
        os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    metadata = os.fstat(descriptor)
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.geteuid():
        os.close(descriptor)
        raise AcquisitionError("artifact_lock_invalid", retryable=False)
    fcntl.flock(descriptor, fcntl.LOCK_EX)
    marker_path = control / "assignment.json"
    expected = _assignment_marker(assignment)
    if marker_path.exists():
        try:
            actual = json.loads(marker_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)
            raise AcquisitionError("artifact_root_marker_invalid", retryable=False) from exc
        if actual != expected:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)
            raise AcquisitionError("artifact_root_identity_conflict", retryable=False)
    else:
        _atomic_json(marker_path, expected)
    return descriptor, objects, quarantine


def _release_root(descriptor: int) -> None:
    fcntl.flock(descriptor, fcntl.LOCK_UN)
    os.close(descriptor)


def _quarantine(path: Path, quarantine: Path, reason: str) -> str:
    if not path.exists():
        return ""
    name = f"{path.name}.{reason}.{time.time_ns()}"
    destination = quarantine / name
    os.replace(path, destination)
    _fsync_directory(quarantine)
    return name


def _clone_file(source: Path, destination: Path) -> bool:
    """Create an APFS copy-on-write clone when the host supports clonefile(2)."""

    if sys.platform != "darwin":
        return False
    libc = ctypes.CDLL(None, use_errno=True)
    clonefile = getattr(libc, "clonefile", None)
    if clonefile is None:
        return False
    clonefile.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_int]
    clonefile.restype = ctypes.c_int
    result = clonefile(os.fsencode(source), os.fsencode(destination), 0)
    if result == 0:
        return True
    error = ctypes.get_errno()
    if error in {errno.ENOTSUP, errno.EXDEV, errno.EINVAL, errno.ENOSYS}:
        return False
    raise OSError(error, os.strerror(error), destination)


def _matching_prefix(source: Path, partial: Path) -> int:
    size = partial.stat().st_size
    if size > source.stat().st_size:
        return -1
    remaining = size
    with source.open("rb") as expected, partial.open("rb") as actual:
        while remaining:
            amount = min(_CHUNK_BYTES, remaining)
            if expected.read(amount) != actual.read(amount):
                return -1
            remaining -= amount
    return size


def _copy_resumable(
    source: Path,
    partial: Path,
    *,
    expected_size: int,
    fault_after_bytes: int | None,
) -> tuple[int, int]:
    resumed = 0
    if partial.exists():
        resumed = _matching_prefix(source, partial)
        if resumed < 0:
            raise AcquisitionError("partial_content_mismatch", retryable=False)
    mode = "ab" if resumed else "xb"
    copied = 0
    with source.open("rb") as source_handle, partial.open(mode) as target_handle:
        source_handle.seek(resumed)
        while chunk := source_handle.read(_CHUNK_BYTES):
            if fault_after_bytes is not None:
                remaining = fault_after_bytes - copied
                if remaining <= 0:
                    target_handle.flush()
                    os.fsync(target_handle.fileno())
                    raise AcquisitionError("transfer_interrupted", retryable=True)
                if len(chunk) > remaining:
                    target_handle.write(chunk[:remaining])
                    copied += remaining
                    target_handle.flush()
                    os.fsync(target_handle.fileno())
                    raise AcquisitionError("transfer_interrupted", retryable=True)
            target_handle.write(chunk)
            copied += len(chunk)
        target_handle.flush()
        os.fsync(target_handle.fileno())
    if resumed + copied != expected_size:
        raise AcquisitionError("source_size_changed", retryable=False)
    return copied, resumed


def _verified(path: Path, *, expected_size: int, expected_digest: str) -> bool:
    return (
        path.is_file()
        and not path.is_symlink()
        and path.stat().st_size == expected_size
        and _sha256(path) == expected_digest
    )


def acquire_assignment_from_snapshot(
    assignment: dict[str, Any],
    snapshot_root: str | Path,
    *,
    allow_clone: bool = True,
    fault_after_bytes: int | None = None,
) -> dict[str, object]:
    """Acquire exactly one assignment's files from a present immutable snapshot."""

    try:
        validate_provisioning_assignment(assignment)
    except (TypeError, ValueError) as exc:
        raise AcquisitionError("assignment_invalid", retryable=False) from exc
    snapshot = Path(snapshot_root).expanduser().resolve(strict=True)
    if not snapshot.is_dir() or snapshot.name != assignment["resolved_commit"]:
        raise AcquisitionError("snapshot_revision_mismatch", retryable=False)
    root = Path(assignment["artifact_cache_root"])
    if root != root.expanduser().absolute():
        raise AcquisitionError("artifact_root_not_canonical", retryable=False)
    descriptor, objects, quarantine = _claim_root(root, assignment)
    copied_bytes = 0
    cloned_bytes = 0
    reused_bytes = 0
    resumed_bytes = 0
    quarantined: list[str] = []
    records: list[dict[str, object]] = []
    try:
        for record in assignment["files"]:
            relative = _safe_relative(record["path"])
            source = snapshot / relative
            if not source.is_file():
                raise AcquisitionError("source_file_missing", retryable=False)
            source = source.resolve(strict=True)
            expected_size = int(record["size_bytes"])
            expected_digest = str(record["content_digest"]).removeprefix("sha256:")
            if source.stat().st_size != expected_size or _sha256(source) != expected_digest:
                raise AcquisitionError("source_integrity_mismatch", retryable=False)

            object_path = objects / expected_digest
            object_reused = _verified(
                object_path,
                expected_size=expected_size,
                expected_digest=expected_digest,
            )
            if object_path.exists() and not object_reused:
                quarantined.append(_quarantine(object_path, quarantine, "integrity"))
            if not object_reused:
                partial = object_path.with_suffix(".partial")
                if partial.exists() and _matching_prefix(source, partial) < 0:
                    quarantined.append(_quarantine(partial, quarantine, "partial"))
                temporary = object_path.with_name(f".{object_path.name}.{uuid.uuid4().hex}.tmp")
                cloned = False
                try:
                    if allow_clone and not partial.exists():
                        cloned = _clone_file(source, temporary)
                    if cloned:
                        cloned_bytes += expected_size
                    else:
                        copied, resumed = _copy_resumable(
                            source,
                            partial,
                            expected_size=expected_size,
                            fault_after_bytes=fault_after_bytes,
                        )
                        copied_bytes += copied
                        resumed_bytes += resumed
                        os.replace(partial, temporary)
                    os.chmod(temporary, 0o400)
                    if not _verified(
                        temporary,
                        expected_size=expected_size,
                        expected_digest=expected_digest,
                    ):
                        raise AcquisitionError("acquired_integrity_mismatch", retryable=False)
                    os.replace(temporary, object_path)
                    _fsync_directory(objects)
                finally:
                    temporary.unlink(missing_ok=True)
            else:
                reused_bytes += expected_size

            destination = root / relative
            destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            destination_reused = _verified(
                destination,
                expected_size=expected_size,
                expected_digest=expected_digest,
            )
            if destination.exists() and not destination_reused:
                quarantined.append(_quarantine(destination, quarantine, "materialized"))
            if not destination_reused:
                temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")
                try:
                    if not (allow_clone and _clone_file(object_path, temporary)):
                        _copy_resumable(
                            object_path,
                            temporary,
                            expected_size=expected_size,
                            fault_after_bytes=None,
                        )
                    os.chmod(temporary, 0o400)
                    os.replace(temporary, destination)
                    _fsync_directory(destination.parent)
                finally:
                    temporary.unlink(missing_ok=True)
            records.append(
                {
                    "path": record["path"],
                    "size_bytes": expected_size,
                    "content_digest": record["content_digest"],
                    "object_reused": object_reused,
                    "materialized_reused": destination_reused,
                }
            )
        report: dict[str, object] = {
            "protocol": ACQUISITION_PROTOCOL,
            "assignment_id": assignment["assignment_id"],
            "deployment_id": assignment["deployment_id"],
            "deployment_epoch": assignment["deployment_epoch"],
            "node_id": assignment["node_id"],
            "model_id": assignment["model_id"],
            "resolved_commit": assignment["resolved_commit"],
            "manifest_digest": assignment["manifest_digest"],
            "state": "acquired",
            "files": records,
            "copied_bytes": copied_bytes,
            "cloned_bytes": cloned_bytes,
            "reused_bytes": reused_bytes,
            "resumed_bytes": resumed_bytes,
            "quarantined": [name for name in quarantined if name],
            "download_bytes": 0,
            "download_policy": "operator_approval_required",
            "route_ready": False,
        }
        report["acquisition_digest"] = "sha256:" + hashlib.sha256(
            _canonical(report)
        ).hexdigest()
        return report
    finally:
        _release_root(descriptor)


def retry_acquisition(
    operation: Callable[[], dict[str, object]],
    *,
    max_attempts: int = 3,
    base_delay_seconds: float = 0.05,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, object]:
    """Retry only explicitly transient acquisition failures with bounded backoff."""

    if type(max_attempts) is not int or not 1 <= max_attempts <= 8:
        raise ValueError("max_attempts must be between 1 and 8")
    if not 0 <= base_delay_seconds <= 60:
        raise ValueError("base_delay_seconds must be between 0 and 60")
    for attempt in range(1, max_attempts + 1):
        try:
            return operation()
        except AcquisitionError as exc:
            if not exc.retryable or attempt == max_attempts:
                raise
            sleep(base_delay_seconds * (2 ** (attempt - 1)))
    raise AssertionError("bounded acquisition retry exhausted without an outcome")
