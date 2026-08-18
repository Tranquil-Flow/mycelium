"""Durable Provisioner-owned acquisition of assignment-local stage-pack chunks."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
import copy
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import threading
import time
from typing import Any
import uuid

from mycelium_swarm_artifacts import (
    ACQUISITION_PROTOCOL,
    LEDGER_PROTOCOL,
    select_transfer_sources,
    source_ref,
    validate_acquisition_status,
    validate_acquisition_ledger,
    validate_policy,
    validate_stage_pack_manifest,
)


ChunkReader = Callable[[str, str, int, int, Mapping[str, Any]], Iterable[bytes]]
OriginReader = Callable[[str, int, int], Iterable[bytes]]
Clock = Callable[[], int]
_PUBLIC_CODE = re.compile(r"[a-z][a-z0-9_]{0,127}\Z")


class ArtifactProvisioningError(RuntimeError):
    """Bounded acquisition failure safe for a public reason code."""

    def __init__(
        self,
        code: str,
        *,
        retryable: bool = False,
        quarantined_bytes: int = 0,
        duplicate_bytes_prevented: int = 0,
        source_rotations: int = 0,
    ) -> None:
        self.code = code
        self.retryable = retryable
        self.quarantined_bytes = quarantined_bytes
        self.duplicate_bytes_prevented = duplicate_bytes_prevented
        self.source_rotations = source_rotations
        super().__init__(code)


class _RateBudget:
    """Thread-safe leaky-bucket schedule with no unbounded initial burst."""

    def __init__(self, bytes_per_second: int) -> None:
        self._rate = bytes_per_second
        self._next = time.monotonic()
        self._lock = threading.Lock()

    def reserve(self, size: int) -> float:
        with self._lock:
            now = time.monotonic()
            self._next = max(now, self._next) + size / self._rate
            return self._next


def _wait_for_budgets(
    payload: bytes,
    *,
    budgets: Sequence[_RateBudget],
    cancel: threading.Event,
) -> None:
    if not payload:
        return
    deadline = max(budget.reserve(len(payload)) for budget in budgets)
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        if cancel.wait(min(remaining, 0.1)):
            raise ArtifactProvisioningError("acquisition_cancelled")


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
    ).encode()


def _fsync_directory(directory: Path) -> None:
    descriptor = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _atomic_json(destination: Path, document: object) -> None:
    temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("xb") as handle:
            handle.write(_canonical(document))
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, destination)
        _fsync_directory(destination.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _private_root(root: Path) -> Path:
    candidate = Path(root)
    if not candidate.is_absolute():
        raise ArtifactProvisioningError("artifact_state_root_unsafe")
    candidate.mkdir(parents=True, mode=0o700, exist_ok=True)
    metadata = candidate.lstat()
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) & 0o022
    ):
        raise ArtifactProvisioningError("artifact_state_root_unsafe")
    resolved = candidate.resolve(strict=True)
    if resolved != candidate:
        raise ArtifactProvisioningError("artifact_state_root_unsafe")
    for name in ("objects", "partials", "quarantine", "promoted", "state"):
        (candidate / name).mkdir(mode=0o700, exist_ok=True)
    return candidate


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(4 * 1024 * 1024):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def _verified(path: Path, *, digest: str, size: int) -> bool:
    try:
        return (
            path.is_file()
            and not path.is_symlink()
            and path.stat().st_size == size
            and _sha256(path) == digest
        )
    except OSError:
        return False


def _promotion_digest(manifest: Mapping[str, Any]) -> str:
    return "sha256:" + hashlib.sha256(_canonical(manifest)).hexdigest()


def _verified_promotion(promotion: Path, manifest: Mapping[str, Any]) -> bool:
    if not promotion.is_dir() or promotion.is_symlink():
        return False
    manifest_path = promotion / "manifest.json"
    try:
        if (
            not manifest_path.is_file()
            or manifest_path.is_symlink()
            or manifest_path.read_bytes() != _canonical(manifest)
        ):
            return False
    except OSError:
        return False
    files_root = promotion / "files"
    pack_digest = hashlib.sha256()
    total = 0
    for record in manifest["files"]:
        path = files_root / record["relative_path"]
        if not _verified(
            path,
            digest=record["content_digest"],
            size=record["size_bytes"],
        ):
            return False
        try:
            with path.open("rb") as handle:
                while block := handle.read(4 * 1024 * 1024):
                    pack_digest.update(block)
                    total += len(block)
        except OSError:
            return False
    return (
        total == manifest["total_size_bytes"]
        and "sha256:" + pack_digest.hexdigest() == manifest["stage_pack_digest"]
    )


def _detached(value: object) -> Any:
    return copy.deepcopy(value)


class ArtifactAcquisitionStore:
    """Content-addressed chunks plus durable privacy-reduced status history."""

    def __init__(self, root: Path, *, history_limit: int = 64) -> None:
        if type(history_limit) is not int or not 1 <= history_limit <= 256:
            raise ValueError("history_limit must be between 1 and 256")
        self.root = _private_root(root)
        self.history_limit = history_limit
        self._state_lock = threading.RLock()
        self._current_path = self.root / "state" / "current.json"
        self._history_path = self.root / "state" / "history.json"
        self._consumed_grants_path = self.root / "state" / "consumed-grants.json"
        self._lock_path = self.root / "state" / "acquisition.lock"
        self._generation_path = self.root / "state" / "generation"
        self._public_ledger: dict[str, Any] | None = None
        self._recover_interrupted()
        self._public_ledger = self.ledger()

    def object_path(self, digest: str) -> Path:
        return self.root / "objects" / digest.removeprefix("sha256:")

    def partial_path(self, digest: str) -> Path:
        return self.root / "partials" / (digest.removeprefix("sha256:") + ".partial")

    def quarantine(self, path: Path, reason: str) -> int:
        if not path.exists():
            return 0
        try:
            size = path.stat().st_size
        except OSError:
            size = 0
        destination = (
            self.root / "quarantine" / (f"{path.name}.{reason}.{time.time_ns()}")
        )
        os.replace(path, destination)
        os.chmod(destination, 0o400)
        _fsync_directory(destination.parent)
        return size

    def acquire_writer(self) -> int:
        descriptor = os.open(
            self._lock_path,
            os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            os.close(descriptor)
            raise ArtifactProvisioningError("concurrent_staging_conflict") from exc
        return descriptor

    @staticmethod
    def release_writer(descriptor: int) -> None:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)

    def consume_grant(self, grant: Mapping[str, Any], *, now_unix_ms: int) -> None:
        """Durably consume one signed peer grant before any acquisition bytes."""

        grant_fields = ("grant_id", "nonce", "expires_at_unix_ms")
        present = [field in grant for field in grant_fields]
        if not any(present):
            return
        grant_id = grant.get("grant_id")
        nonce = grant.get("nonce")
        expires = grant.get("expires_at_unix_ms")
        if (
            not all(present)
            or not isinstance(grant_id, str)
            or not 1 <= len(grant_id) <= 256
            or not isinstance(nonce, str)
            or not 1 <= len(nonce) <= 256
            or type(expires) is not int
            or expires < now_unix_ms
        ):
            raise ArtifactProvisioningError("artifact_grant_invalid")
        with self._state_lock:
            records: list[dict[str, Any]] = []
            if self._consumed_grants_path.exists():
                try:
                    document = json.loads(self._consumed_grants_path.read_text())
                    if (
                        not isinstance(document, dict)
                        or set(document) != {"protocol", "grants"}
                        or document.get("protocol")
                        != "mycelium.consumed_artifact_grants.v1"
                        or not isinstance(document.get("grants"), list)
                    ):
                        raise ValueError
                    seen: set[tuple[str, str]] = set()
                    for item in document["grants"]:
                        if (
                            not isinstance(item, dict)
                            or set(item)
                            != {"grant_id", "nonce", "expires_at_unix_ms"}
                            or not isinstance(item.get("grant_id"), str)
                            or not 1 <= len(item["grant_id"]) <= 256
                            or not isinstance(item.get("nonce"), str)
                            or not 1 <= len(item["nonce"]) <= 256
                            or type(item.get("expires_at_unix_ms")) is not int
                        ):
                            raise ValueError
                        key = (item["grant_id"], item["nonce"])
                        if key in seen:
                            raise ValueError
                        seen.add(key)
                        if item["expires_at_unix_ms"] >= now_unix_ms:
                            records.append(item)
                except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
                    raise ArtifactProvisioningError("artifact_state_corrupt") from exc
            if any(
                item["grant_id"] == grant_id and item["nonce"] == nonce
                for item in records
            ):
                raise ArtifactProvisioningError("artifact_grant_replay")
            if len(records) >= 8_192:
                raise ArtifactProvisioningError("artifact_grant_registry_full")
            records.append(
                {
                    "grant_id": grant_id,
                    "nonce": nonce,
                    "expires_at_unix_ms": expires,
                }
            )
            records.sort(key=lambda item: (item["expires_at_unix_ms"], item["grant_id"], item["nonce"]))
            _atomic_json(
                self._consumed_grants_path,
                {
                    "protocol": "mycelium.consumed_artifact_grants.v1",
                    "grants": records,
                },
            )

    def next_generation(self) -> int:
        with self._state_lock:
            generation = 1
            if self._generation_path.exists():
                try:
                    generation = int(self._generation_path.read_text()) + 1
                except (OSError, UnicodeError, ValueError) as exc:
                    raise ArtifactProvisioningError("artifact_state_corrupt") from exc
            temporary = self._generation_path.with_name(
                f".{self._generation_path.name}.{uuid.uuid4().hex}.tmp"
            )
            try:
                with temporary.open("x", encoding="ascii") as handle:
                    handle.write(str(generation))
                    handle.flush()
                    os.fsync(handle.fileno())
                os.chmod(temporary, 0o600)
                os.replace(temporary, self._generation_path)
                _fsync_directory(self._generation_path.parent)
            finally:
                temporary.unlink(missing_ok=True)
            return generation

    def write_current(self, status: Mapping[str, Any]) -> None:
        validated = validate_acquisition_status(status)
        with self._state_lock:
            _atomic_json(self._current_path, validated)
            history = (
                self._read_history()
                if self._public_ledger is None
                else self._public_ledger["history"]
            )
            self._public_ledger = validate_acquisition_ledger(
                {
                    "protocol": LEDGER_PROTOCOL,
                    "generation": max(
                        [validated["generation"]]
                        + [item["generation"] for item in history]
                    ),
                    "current": validated,
                    "history": history,
                }
            )

    def _read_history(self) -> list[dict[str, Any]]:
        if not self._history_path.exists():
            return []
        try:
            raw = json.loads(self._history_path.read_text())
            if not isinstance(raw, list):
                raise ValueError
            return [validate_acquisition_status(item) for item in raw]
        except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
            raise ArtifactProvisioningError("artifact_history_corrupt") from exc

    def finish(self, status: Mapping[str, Any]) -> None:
        validated = validate_acquisition_status(status)
        if validated["terminal_at_unix_ms"] is None:
            raise ArtifactProvisioningError("artifact_terminal_status_invalid")
        with self._state_lock:
            history = self._read_history()
            history.append(validated)
            history = history[-self.history_limit :]
            _atomic_json(self._history_path, history)
            self._current_path.unlink(missing_ok=True)
            _fsync_directory(self._current_path.parent)
            self._public_ledger = validate_acquisition_ledger(
                {
                    "protocol": LEDGER_PROTOCOL,
                    "generation": max(item["generation"] for item in history),
                    "current": None,
                    "history": history,
                }
            )

    def import_member_terminal(self, status: Mapping[str, Any]) -> dict[str, Any]:
        """Append a validated remote terminal result to the product ledger."""

        validated = validate_acquisition_status(status)
        if validated["terminal_at_unix_ms"] is None:
            raise ArtifactProvisioningError("artifact_terminal_status_invalid")
        descriptor = self.acquire_writer()
        try:
            if any(
                item["acquisition_id"] == validated["acquisition_id"]
                for item in self._read_history()
            ):
                raise ArtifactProvisioningError("artifact_member_status_replay")
            imported = {**validated, "generation": self.next_generation()}
            imported = validate_acquisition_status(imported)
            self.finish(imported)
            return imported
        finally:
            self.release_writer(descriptor)

    def ledger(self) -> dict[str, Any]:
        """Reread and validate the authoritative durable ledger."""

        with self._state_lock:
            current = None
            if self._current_path.exists():
                try:
                    current = validate_acquisition_status(
                        json.loads(self._current_path.read_text())
                    )
                except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
                    raise ArtifactProvisioningError("artifact_state_corrupt") from exc
            history = self._read_history()
            generation = max(
                [0]
                + [item["generation"] for item in history]
                + ([] if current is None else [current["generation"]])
            )
            return validate_acquisition_ledger(
                {
                    "protocol": LEDGER_PROTOCOL,
                    "generation": generation,
                    "current": current,
                    "history": history,
                }
            )

    def public_ledger(self) -> dict[str, Any]:
        """Return the last validated progress snapshot without blocking on storage."""

        with self._state_lock:
            if self._public_ledger is None:
                self._public_ledger = self.ledger()
            return _detached(self._public_ledger)

    def _recover_interrupted(self) -> None:
        if not self._current_path.exists():
            return
        try:
            current = validate_acquisition_status(
                json.loads(self._current_path.read_text())
            )
        except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
            raise ArtifactProvisioningError("artifact_state_corrupt") from exc
        if current["terminal_at_unix_ms"] is not None:
            self.finish(current)
            return
        now = int(time.time() * 1_000)
        current.update(
            state="failed",
            phase=None,
            reason_code="interrupted_transfer",
            retryable=True,
            updated_at_unix_ms=max(now, current["updated_at_unix_ms"]),
            terminal_at_unix_ms=max(now, current["updated_at_unix_ms"]),
        )
        self.finish(current)


class SwarmArtifactProvisioner:
    """Acquire one exact manifest with chunk-level resume and atomic promotion."""

    def __init__(
        self,
        store: ArtifactAcquisitionStore,
        *,
        clock_unix_ms: Clock | None = None,
        disk_free_bytes: Callable[[Path], int] | None = None,
    ) -> None:
        self.store = store
        self._clock = clock_unix_ms or (lambda: int(time.time() * 1_000))
        self._disk_free = disk_free_bytes or (lambda path: shutil.disk_usage(path).free)
        self._status_lock = threading.RLock()

    @staticmethod
    def _chunk_records(manifest: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
        return {item["content_digest"]: item for item in manifest["chunks"]}

    def _initial_status(
        self,
        *,
        manifest: Mapping[str, Any],
        generation: int,
        acquisition_id: str,
        cached_bytes: int,
        cached_chunks: int,
        sources: Sequence[str],
    ) -> dict[str, Any]:
        now = self._clock()
        total = int(manifest["total_size_bytes"])
        status = {
            "protocol": ACQUISITION_PROTOCOL,
            "generation": generation,
            "acquisition_id": acquisition_id,
            "state": "reserving",
            "phase": "reserving",
            "model_id": manifest["model_id"],
            "model_revision": manifest["model_revision"],
            "representation": (
                f"{manifest['serving_quantization']} · {manifest['serving_dtype']}"
            ),
            "assignment_id": manifest["assignment_id"],
            "placement_id": manifest["placement_id"],
            "stage_id": manifest["stage_id"],
            "layer_start": manifest["layer_start"],
            "layer_end_exclusive": manifest["layer_end_exclusive"],
            "total_bytes": total,
            "cached_verified_bytes": cached_bytes,
            "transferred_verified_bytes": 0,
            "missing_bytes": total - cached_bytes,
            "quarantined_bytes": 0,
            "duplicate_bytes_prevented": cached_bytes,
            "eligible_source_count": len(sources),
            "active_source_count": 0,
            "sources": sorted(
                [
                    {
                        "source_ref": source_ref(source, acquisition_id),
                        "state": "eligible",
                        "verified_bytes": 0,
                    }
                    for source in sources
                ],
                key=lambda item: item["source_ref"],
            ),
            "origin_bytes": 0,
            "aggregate_bytes_per_second": 0.0,
            "eta_seconds": None,
            "chunk_count": len(manifest["chunks"]),
            "verified_chunk_count": cached_chunks,
            "resumed_chunk_count": cached_chunks,
            "source_rotation_count": 0,
            "manifest_digest": manifest["manifest_digest"],
            "assignment_digest": manifest["assignment_digest"],
            "representation_digest": manifest["representation_digest"],
            "feasibility_digest": manifest["feasibility_digest"],
            "evidence_generation": manifest["evidence_generation"],
            "promotion_digest": None,
            "reason_code": None,
            "retryable": False,
            "started_at_unix_ms": now,
            "updated_at_unix_ms": now,
            "terminal_at_unix_ms": None,
        }
        return validate_acquisition_status(status)

    def _publish(self, status: dict[str, Any], *, state: str) -> None:
        with self._status_lock:
            status["state"] = state
            status["phase"] = state
            status["updated_at_unix_ms"] = max(
                self._clock(), status["updated_at_unix_ms"]
            )
            self.store.write_current(status)

    @staticmethod
    def _consume_stream(
        stream: Iterable[bytes],
        handle: Any,
        *,
        expected: int,
    ) -> int:
        written = 0
        try:
            for chunk in stream:
                if not isinstance(chunk, bytes) or not chunk:
                    raise ArtifactProvisioningError(
                        "source_response_invalid", retryable=True
                    )
                if written + len(chunk) > expected:
                    raise ArtifactProvisioningError("source_response_invalid")
                handle.write(chunk)
                written += len(chunk)
        except ArtifactProvisioningError:
            raise
        except BaseException as exc:
            code = getattr(exc, "code", None)
            if isinstance(code, str) and _PUBLIC_CODE.fullmatch(code) is not None:
                raise ArtifactProvisioningError(
                    code, retryable=getattr(exc, "retryable", False) is True
                ) from exc
            raise ArtifactProvisioningError(
                "source_disappeared", retryable=True
            ) from exc
        if written != expected:
            raise ArtifactProvisioningError("source_disappeared", retryable=True)
        return written

    def _fetch_chunk(
        self,
        *,
        digest: str,
        size: int,
        sources: Sequence[str],
        grant: Mapping[str, Any],
        reader: ChunkReader,
        origin: OriginReader | None,
        cancel: threading.Event,
        maximum_rotations: int,
        maximum_retries: int,
    ) -> tuple[str | None, int, int, int]:
        partial = self.store.partial_path(digest)
        rotations = 0
        quarantined = 0
        duplicate = 0
        base_attempts: list[str | None] = list(sources)
        if grant["origin_fallback_allowed"] and origin is not None:
            base_attempts.append(None)
        if not base_attempts:
            raise ArtifactProvisioningError("no_authorized_source")
        attempts = [
            source for source in base_attempts for _ in range(maximum_retries + 1)
        ]
        sentinel = object()
        previous_source: str | None | object = sentinel
        for source in attempts:
            if cancel.is_set():
                raise ArtifactProvisioningError("acquisition_cancelled")
            if previous_source is not sentinel and previous_source != source:
                rotations += 1
                if rotations > maximum_rotations:
                    break
            previous_source = source
            existing = partial.stat().st_size if partial.exists() else 0
            if existing > size:
                quarantined += self.store.quarantine(partial, "partial_oversize")
                existing = 0
            try:
                if existing:
                    prefix_reader = (
                        origin(digest, 0, existing)
                        if source is None
                        else reader(source, digest, 0, existing, grant)
                    )
                    with partial.open("rb") as local_prefix:
                        expected_prefix = local_prefix.read()
                    received_prefix = b"".join(prefix_reader)
                    if received_prefix != expected_prefix:
                        quarantined += self.store.quarantine(
                            partial, "partial_prefix_mismatch"
                        )
                        existing = 0
                    else:
                        duplicate += existing
                # A failed authenticated read may leave an empty, writer-owned
                # partial behind. Reopen that file for the next bounded attempt;
                # exclusive creation would turn a safe retry into EEXIST.
                mode = "ab" if partial.exists() else "xb"
                with partial.open(mode) as handle:
                    remaining = size - existing
                    stream = (
                        origin(digest, existing, remaining)
                        if source is None
                        else reader(source, digest, existing, remaining, grant)
                    )
                    self._consume_stream(stream, handle, expected=remaining)
                    handle.flush()
                    os.fsync(handle.fileno())
                if not _verified(partial, digest=digest, size=size):
                    quarantined += self.store.quarantine(
                        partial, "chunk_integrity_failure"
                    )
                    continue
                destination = self.store.object_path(digest)
                os.chmod(partial, 0o400)
                os.replace(partial, destination)
                _fsync_directory(destination.parent)
                return source, rotations, quarantined, duplicate
            except ArtifactProvisioningError as exc:
                if not exc.retryable or exc.code == "acquisition_cancelled":
                    exc.quarantined_bytes += quarantined
                    exc.duplicate_bytes_prevented += duplicate
                    exc.source_rotations += rotations
                    raise
                continue
            except OSError as exc:
                raise ArtifactProvisioningError("artifact_storage_failure") from exc
        if grant["origin_fallback_allowed"] and origin is None:
            raise ArtifactProvisioningError(
                "origin_fallback_unavailable",
                retryable=True,
                quarantined_bytes=quarantined,
                duplicate_bytes_prevented=duplicate,
                source_rotations=rotations,
            )
        raise ArtifactProvisioningError(
            "bounded_retry_exhaustion",
            retryable=True,
            quarantined_bytes=quarantined,
            duplicate_bytes_prevented=duplicate,
            source_rotations=rotations,
        )

    def _promote(
        self,
        *,
        manifest: Mapping[str, Any],
        acquisition_id: str,
    ) -> str:
        promotion = self.store.root / "promoted" / manifest["manifest_id"]
        if promotion.exists():
            if _verified_promotion(promotion, manifest):
                return _promotion_digest(manifest)
            raise ArtifactProvisioningError("promotion_conflict")
        temporary = promotion.with_name(f".{promotion.name}.{acquisition_id}.tmp")
        temporary.mkdir(mode=0o700)
        try:
            files_root = temporary / "files"
            files_root.mkdir(mode=0o700)
            chunk_index = 0
            chunk_handle = None
            try:
                for record in manifest["files"]:
                    destination = files_root / record["relative_path"]
                    destination.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
                    remaining = record["size_bytes"]
                    with destination.open("xb") as output:
                        while remaining:
                            if chunk_handle is None:
                                if chunk_index >= len(manifest["chunks"]):
                                    raise ArtifactProvisioningError(
                                        "pack_integrity_failure"
                                    )
                                chunk = manifest["chunks"][chunk_index]
                                source = self.store.object_path(chunk["content_digest"])
                                if not _verified(
                                    source,
                                    digest=chunk["content_digest"],
                                    size=chunk["size_bytes"],
                                ):
                                    raise ArtifactProvisioningError(
                                        "pack_integrity_failure"
                                    )
                                chunk_handle = source.open("rb")
                                chunk_index += 1
                            block = chunk_handle.read(min(4 * 1024 * 1024, remaining))
                            if not block:
                                chunk_handle.close()
                                chunk_handle = None
                                continue
                            output.write(block)
                            remaining -= len(block)
                        output.flush()
                        os.fsync(output.fileno())
                    if not _verified(
                        destination,
                        digest=record["content_digest"],
                        size=record["size_bytes"],
                    ):
                        raise ArtifactProvisioningError("file_integrity_failure")
                    os.chmod(destination, 0o400)
                if chunk_handle is not None:
                    if chunk_handle.read(1):
                        raise ArtifactProvisioningError("pack_integrity_failure")
                    chunk_handle.close()
                    chunk_handle = None
                if chunk_index != len(manifest["chunks"]):
                    raise ArtifactProvisioningError("pack_integrity_failure")
            finally:
                if chunk_handle is not None:
                    chunk_handle.close()
            manifest_path = temporary / "manifest.json"
            manifest_path.write_bytes(_canonical(manifest))
            with manifest_path.open("rb") as handle:
                os.fsync(handle.fileno())
            os.chmod(manifest_path, 0o400)
            for directory in sorted(
                (item for item in temporary.rglob("*") if item.is_dir()),
                key=lambda item: len(item.parts),
                reverse=True,
            ):
                _fsync_directory(directory)
            _fsync_directory(temporary)
            os.replace(temporary, promotion)
            _fsync_directory(promotion.parent)
            if not _verified_promotion(promotion, manifest):
                raise ArtifactProvisioningError("promotion_integrity_failure")
            return _promotion_digest(manifest)
        finally:
            if temporary.exists():
                shutil.rmtree(temporary, ignore_errors=True)

    def acquire(
        self,
        *,
        manifest: Mapping[str, Any],
        expected_binding: Mapping[str, Any],
        grant: Mapping[str, Any],
        advertisements: Sequence[Mapping[str, Any]],
        policy: Mapping[str, Any],
        reader: ChunkReader,
        origin: OriginReader | None = None,
        predicted_improvement_ratio: float = 0.0,
        serving_reserve_satisfied: bool = True,
        cancel: threading.Event | None = None,
    ) -> dict[str, Any]:
        checked_manifest = validate_stage_pack_manifest(
            manifest, expected_binding=expected_binding
        )
        checked_policy = validate_policy(policy)
        cancel_event = cancel or threading.Event()
        descriptor = self.store.acquire_writer()
        status: dict[str, Any] | None = None
        try:
            if (
                grant.get("manifest_digest") != checked_manifest["manifest_digest"]
                or grant.get("assignment_digest")
                != checked_manifest["assignment_digest"]
                or grant.get("representation_digest")
                != checked_manifest["representation_digest"]
                or grant.get("feasibility_digest")
                != checked_manifest["feasibility_digest"]
                or grant.get("recipient_member_id")
                != checked_manifest["recipient_member_id"]
                or grant.get("recipient_membership_generation")
                != checked_manifest["recipient_membership_generation"]
                or checked_policy["chunk_size_bytes"]
                != checked_manifest["chunk_size_bytes"]
            ):
                raise ArtifactProvisioningError("authorization_drift")
            allowed = grant.get("allowed_chunk_digests")
            authorized_sources = grant.get("authorized_source_member_ids")
            if not isinstance(allowed, list) or not isinstance(
                authorized_sources, list
            ):
                raise ArtifactProvisioningError("artifact_grant_invalid")
            if (
                type(grant.get("maximum_total_bytes")) is not int
                or checked_manifest["total_size_bytes"] > grant["maximum_total_bytes"]
                or type(grant.get("maximum_concurrency")) is not int
                or checked_policy["aggregate_concurrency"]
                > grant["maximum_concurrency"]
                or type(grant.get("maximum_bytes_per_second")) is not int
                or checked_policy["aggregate_bytes_per_second"]
                > grant["maximum_bytes_per_second"]
            ):
                raise ArtifactProvisioningError("artifact_grant_budget_exceeded")
            chunk_records = self._chunk_records(checked_manifest)
            if set(allowed) != set(chunk_records):
                raise ArtifactProvisioningError("assignment_scope_violation")
            self.store.consume_grant(grant, now_unix_ms=self._clock())
            advertisements_by_source = {
                item["source_member_id"]: item
                for item in advertisements
                if item.get("source_member_id") in authorized_sources
                and item.get("manifest_digest") == checked_manifest["manifest_digest"]
                and item.get("transfer_health") in {"healthy", "degraded"}
            }
            cached: set[str] = set()
            quarantined_bytes = 0
            for digest, record in chunk_records.items():
                path = self.store.object_path(digest)
                if _verified(path, digest=digest, size=record["size_bytes"]):
                    cached.add(digest)
                elif path.exists():
                    quarantined_bytes += self.store.quarantine(
                        path, "cache_integrity_failure"
                    )
            cached_bytes = sum(chunk_records[item]["size_bytes"] for item in cached)
            missing = [
                chunk["content_digest"]
                for chunk in checked_manifest["chunks"]
                if chunk["content_digest"] not in cached
            ]
            required_space = (
                sum(chunk_records[item]["size_bytes"] for item in missing)
                + (
                    0
                    if _verified_promotion(
                        self.store.root / "promoted" / checked_manifest["manifest_id"],
                        checked_manifest,
                    )
                    else checked_manifest["total_size_bytes"]
                )
                + checked_policy["disk_reserve_bytes"]
            )
            if self._disk_free(self.store.root) < required_space:
                raise ArtifactProvisioningError("insufficient_disk")
            acquisition_id = "acquisition-" + uuid.uuid4().hex
            sources = sorted(advertisements_by_source)
            status = self._initial_status(
                manifest=checked_manifest,
                generation=self.store.next_generation(),
                acquisition_id=acquisition_id,
                cached_bytes=cached_bytes,
                cached_chunks=len(cached),
                sources=sources,
            )
            status["quarantined_bytes"] = quarantined_bytes
            self.store.write_current(status)
            self._publish(status, state="discovering_sources")
            selected = select_transfer_sources(
                missing_chunk_digests=missing,
                advertisements=list(advertisements_by_source.values()),
                policy=checked_policy,
                predicted_improvement_ratio=predicted_improvement_ratio,
                serving_reserve_satisfied=serving_reserve_satisfied,
            )
            primary_for_chunk = {
                digest: source
                for source, digests in selected.items()
                for digest in digests
            }
            if (
                missing
                and not selected
                and not (grant.get("origin_fallback_allowed") and origin is not None)
            ):
                raise ArtifactProvisioningError("no_authorized_source")
            self._publish(status, state="transferring")
            started = time.monotonic()
            source_counts = {source: 0 for source in sources}
            aggregate_semaphore = threading.BoundedSemaphore(
                checked_policy["aggregate_concurrency"]
            )
            aggregate_rate = _RateBudget(
                min(
                    checked_policy["aggregate_bytes_per_second"],
                    grant["maximum_bytes_per_second"],
                )
            )
            source_semaphores = {
                source: threading.BoundedSemaphore(
                    checked_policy["per_source_concurrency"]
                )
                for source in sources
            }
            source_rates = {
                source: _RateBudget(
                    min(
                        checked_policy["per_source_bytes_per_second"],
                        advertisements_by_source[source]["max_bytes_per_second"],
                    )
                )
                for source in sources
            }

            def bounded_reader(
                source: str,
                digest: str,
                offset: int,
                length: int,
                bound_grant: Mapping[str, Any],
            ) -> Iterable[bytes]:
                aggregate_semaphore.acquire()
                source_semaphores[source].acquire()
                try:
                    for payload in reader(
                        source, digest, offset, length, bound_grant
                    ):
                        if isinstance(payload, bytes):
                            _wait_for_budgets(
                                payload,
                                budgets=(aggregate_rate, source_rates[source]),
                                cancel=cancel_event,
                            )
                        yield payload
                finally:
                    source_semaphores[source].release()
                    aggregate_semaphore.release()

            def bounded_origin(
                digest: str, offset: int, length: int
            ) -> Iterable[bytes]:
                assert origin is not None
                aggregate_semaphore.acquire()
                try:
                    for payload in origin(digest, offset, length):
                        if isinstance(payload, bytes):
                            _wait_for_budgets(
                                payload,
                                budgets=(aggregate_rate,),
                                cancel=cancel_event,
                            )
                        yield payload
                finally:
                    aggregate_semaphore.release()

            def transfer_one(
                digest: str,
            ) -> tuple[str, str | None, int, int, int]:
                record = chunk_records[digest]
                candidates = [
                    source
                    for source in sources
                    if digest
                    in advertisements_by_source[source]["available_chunk_digests"]
                ]
                primary = primary_for_chunk.get(digest)
                if primary is not None:
                    candidates.remove(primary)
                    candidates.insert(0, primary)
                winner, rotations, quarantined, duplicate = self._fetch_chunk(
                    digest=digest,
                    size=record["size_bytes"],
                    sources=candidates,
                    grant=grant,
                    reader=bounded_reader,
                    origin=None if origin is None else bounded_origin,
                    cancel=cancel_event,
                    maximum_rotations=checked_policy["maximum_source_rotations"],
                    maximum_retries=checked_policy["maximum_retries_per_chunk"],
                )
                return digest, winner, rotations, quarantined, duplicate

            def record_outcome(outcome: tuple[str, str | None, int, int, int]) -> None:
                digest, winner, rotations, quarantined, duplicate = outcome
                record = chunk_records[digest]
                status["source_rotation_count"] += rotations
                status["quarantined_bytes"] += quarantined
                status["duplicate_bytes_prevented"] += duplicate
                status["transferred_verified_bytes"] += record["size_bytes"]
                status["missing_bytes"] -= record["size_bytes"]
                status["verified_chunk_count"] += 1
                if duplicate:
                    status["resumed_chunk_count"] += 1
                if winner is None:
                    status["origin_bytes"] += record["size_bytes"]
                else:
                    source_counts[winner] += record["size_bytes"]
                elapsed = max(time.monotonic() - started, 1e-9)
                status["aggregate_bytes_per_second"] = (
                    status["transferred_verified_bytes"] / elapsed
                )
                status["eta_seconds"] = (
                    status["missing_bytes"] / status["aggregate_bytes_per_second"]
                    if status["missing_bytes"]
                    else 0.0
                )
                for source_status in status["sources"]:
                    source = next(
                        item
                        for item in sources
                        if source_ref(item, acquisition_id)
                        == source_status["source_ref"]
                    )
                    source_status["verified_bytes"] = source_counts[source]
                    source_status["state"] = (
                        "active" if source_counts[source] else "eligible"
                    )
                status["active_source_count"] = sum(
                    bool(value) for value in source_counts.values()
                )
                status["updated_at_unix_ms"] = max(
                    self._clock(), status["updated_at_unix_ms"]
                )
                self.store.write_current(status)

            transfer_groups: dict[str, list[str]] = {}
            for digest in missing:
                key = primary_for_chunk.get(digest, "~origin")
                transfer_groups.setdefault(key, []).append(digest)
            transfer_order: list[str] = []
            while any(transfer_groups.values()):
                for key in sorted(transfer_groups):
                    if transfer_groups[key]:
                        transfer_order.append(transfer_groups[key].pop(0))
            worker_count = min(
                len(transfer_order), checked_policy["aggregate_concurrency"]
            )
            if worker_count:
                with ThreadPoolExecutor(
                    max_workers=worker_count,
                    thread_name_prefix="mycelium-artifact",
                ) as executor:
                    futures = {
                        executor.submit(transfer_one, digest): digest
                        for digest in transfer_order
                    }
                    try:
                        for future in as_completed(futures):
                            record_outcome(future.result())
                    except BaseException:
                        cancel_event.set()
                        for future in futures:
                            future.cancel()
                        raise
            self._publish(status, state="verifying_chunks")
            for digest, record in chunk_records.items():
                if not _verified(
                    self.store.object_path(digest),
                    digest=digest,
                    size=record["size_bytes"],
                ):
                    raise ArtifactProvisioningError("chunk_integrity_failure")
            self._publish(status, state="verifying_pack")
            self._publish(status, state="promoting")
            try:
                promotion_digest = self._promote(
                    manifest=checked_manifest, acquisition_id=acquisition_id
                )
            except OSError as exc:
                raise ArtifactProvisioningError("artifact_storage_failure") from exc
            now = max(self._clock(), status["updated_at_unix_ms"])
            status.update(
                state="ready",
                phase=None,
                active_source_count=0,
                promotion_digest=promotion_digest,
                reason_code=None,
                retryable=False,
                updated_at_unix_ms=now,
                terminal_at_unix_ms=now,
            )
            for source_status in status["sources"]:
                if source_status["state"] == "active":
                    source_status["state"] = "rotated"
            self.store.finish(status)
            return _detached(status)
        except ArtifactProvisioningError as exc:
            if status is None:
                raise
            status["quarantined_bytes"] += exc.quarantined_bytes
            status["duplicate_bytes_prevented"] += exc.duplicate_bytes_prevented
            status["source_rotation_count"] += exc.source_rotations
            now = max(self._clock(), status["updated_at_unix_ms"])
            state = "cancelled" if exc.code == "acquisition_cancelled" else "failed"
            status.update(
                state=state,
                phase=None,
                active_source_count=0,
                reason_code=None if state == "cancelled" else exc.code,
                retryable=exc.retryable,
                updated_at_unix_ms=now,
                terminal_at_unix_ms=now,
            )
            for source_status in status["sources"]:
                if source_status["state"] == "active":
                    source_status["state"] = "lost"
            self.store.finish(status)
            return _detached(status)
        finally:
            self.store.release_writer(descriptor)


__all__ = [
    "ArtifactAcquisitionStore",
    "ArtifactProvisioningError",
    "LEDGER_PROTOCOL",
    "SwarmArtifactProvisioner",
]
