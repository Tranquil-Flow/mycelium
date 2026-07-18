"""Qualifier-owned, in-memory current-record authority.

This module validates evidence through :func:`qualify_route` before publishing
an immutable ``RouteQualificationV1``. It stores no evidence bytes and exposes
only the exact current object required by the request gateway's read-only
``QualificationSource`` protocol.
"""
from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
import threading
from typing import Any

from .contracts import RouteQualificationV1
from .evidence import canonical_json_bytes, canonical_json_loads
from .qualifier import qualify_route


class QualificationAuthorityError(ValueError):
    """Fail-closed authority lifecycle error carrying a stable code."""

    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        self.detail = detail
        super().__init__(code if not detail else f"{code}: {detail}")


@dataclass(frozen=True, slots=True)
class _PublishedQualification:
    record: RouteQualificationV1
    valid_until_unix_ms: int


class QualificationAuthority:
    """Atomically publish, expire, and drop qualifier-produced records.

    The authority is deliberately memory-only. A restart begins with no current
    route, and a clock rollback drops any current route before failing closed.
    """

    def __init__(self, *, clock_unix_ms: Callable[[], int]) -> None:
        if not callable(clock_unix_ms):
            raise TypeError("clock_unix_ms must be callable")
        self._clock_unix_ms = clock_unix_ms
        self._lock = threading.RLock()
        self._published: _PublishedQualification | None = None
        self._last_observed_unix_ms: int | None = None
        self._clock_fence = 0

    def current(self) -> RouteQualificationV1 | None:
        """Return the exact current record, dropping it at evidence expiry."""
        now_unix_ms = self._observe_time()
        with self._lock:
            self._expire_locked(now_unix_ms)
            if self._published is None:
                return None
            return self._published.record

    def qualify_and_publish(
        self,
        *,
        evidence_files: Mapping[str, bytes],
        evidence_manifest: Mapping[str, Any],
        verify_gossip_signature: Callable[[bytes, dict[str, Any]], bool],
        verify_load_proof_signature: Callable[[bytes, dict[str, Any]], bool],
    ) -> RouteQualificationV1:
        """Validate one immutable evidence snapshot and publish it atomically."""
        started_at_unix_ms, started_clock_fence = self._observe_time_snapshot()
        files_snapshot = self._snapshot_files(evidence_files)
        manifest_snapshot = self._snapshot_manifest(evidence_manifest)
        record = qualify_route(
            evidence_files=files_snapshot,
            evidence_manifest=manifest_snapshot,
            now_unix_ms=started_at_unix_ms,
            verify_gossip_signature=verify_gossip_signature,
            verify_load_proof_signature=verify_load_proof_signature,
        )
        valid_until_unix_ms = self._validated_expiry(files_snapshot)

        candidate = _PublishedQualification(
            record=record,
            valid_until_unix_ms=valid_until_unix_ms,
        )
        with self._lock:
            completed_at_unix_ms = self._observe_time_locked()
            if started_clock_fence != self._clock_fence:
                raise QualificationAuthorityError("authority_clock_fence_changed")
            if completed_at_unix_ms >= valid_until_unix_ms:
                raise QualificationAuthorityError("qualification_expired_before_publication")
            self._expire_locked(completed_at_unix_ms)
            current = self._published
            if current is not None:
                if current.record.qualification_id == record.qualification_id:
                    return current.record
                self._require_successor(current.record, record)
            self._published = candidate
            return record

    def drop(self, *, expected_qualification_id: str) -> bool:
        """Drop only the exact expected current record."""
        if not isinstance(expected_qualification_id, str) or not expected_qualification_id:
            raise QualificationAuthorityError("invalid_qualification_id")
        now_unix_ms = self._observe_time()
        with self._lock:
            self._expire_locked(now_unix_ms)
            current = self._published
            if current is None or current.record.qualification_id != expected_qualification_id:
                return False
            self._published = None
            return True

    def _observe_time(self) -> int:
        return self._observe_time_snapshot()[0]

    def _observe_time_snapshot(self) -> tuple[int, int]:
        with self._lock:
            observed = self._observe_time_locked()
            return observed, self._clock_fence

    def _observe_time_locked(self) -> int:
        try:
            observed = self._clock_unix_ms()
        except Exception as exc:
            self._published = None
            self._clock_fence += 1
            raise QualificationAuthorityError("authority_clock_unavailable") from exc
        if type(observed) is not int or observed < 0:
            self._published = None
            self._clock_fence += 1
            raise QualificationAuthorityError("invalid_authority_time")
        previous = self._last_observed_unix_ms
        if previous is not None and observed < previous:
            self._published = None
            self._clock_fence += 1
            raise QualificationAuthorityError("authority_clock_rollback")
        self._last_observed_unix_ms = observed
        return observed

    def _expire_locked(self, now_unix_ms: int) -> None:
        current = self._published
        if current is not None and now_unix_ms >= current.valid_until_unix_ms:
            self._published = None

    @classmethod
    def _require_successor(
        cls,
        current: RouteQualificationV1,
        candidate: RouteQualificationV1,
    ) -> None:
        if candidate.deployment_epoch == current.deployment_epoch:
            frozen_identity = (
                "deployment_id",
                "model_id",
                "resolved_commit",
                "manifest_digest",
            )
            if any(
                getattr(candidate, field) != getattr(current, field)
                for field in frozen_identity
            ):
                raise QualificationAuthorityError("qualification_identity_conflict")
        candidate_key = cls._ordering_key(candidate)
        current_key = cls._ordering_key(current)
        if candidate_key < current_key:
            raise QualificationAuthorityError("stale_qualification")
        if candidate_key == current_key:
            raise QualificationAuthorityError("qualification_identity_conflict")

    @staticmethod
    def _ordering_key(record: RouteQualificationV1) -> tuple[int, int, int]:
        return (
            record.deployment_epoch,
            record.topology_version,
            record.issued_at_unix_ms,
        )

    @staticmethod
    def _snapshot_files(evidence_files: Mapping[str, bytes]) -> dict[str, bytes]:
        if not isinstance(evidence_files, Mapping):
            raise QualificationAuthorityError("invalid_evidence_files")
        snapshot: dict[str, bytes] = {}
        try:
            items = tuple(evidence_files.items())
        except Exception as exc:
            raise QualificationAuthorityError("invalid_evidence_files") from exc
        for path, content in items:
            if not isinstance(path, str) or type(content) is not bytes:
                raise QualificationAuthorityError("invalid_evidence_files")
            snapshot[path] = bytes(content)
        return snapshot

    @staticmethod
    def _snapshot_manifest(evidence_manifest: Mapping[str, Any]) -> dict[str, Any]:
        if not isinstance(evidence_manifest, Mapping):
            raise QualificationAuthorityError("invalid_evidence_manifest")
        try:
            snapshot = canonical_json_loads(
                canonical_json_bytes(dict(evidence_manifest)),
                path="<evidence-manifest>",
            )
        except Exception as exc:
            raise QualificationAuthorityError("invalid_evidence_manifest") from exc
        if not isinstance(snapshot, dict):
            raise QualificationAuthorityError("invalid_evidence_manifest")
        return snapshot

    @staticmethod
    def _validated_expiry(evidence_files: Mapping[str, bytes]) -> int:
        try:
            challenge = canonical_json_loads(
                evidence_files["run/route-challenge.json"],
                path="run/route-challenge.json",
            )
            gossip_signature = canonical_json_loads(
                evidence_files["control/gossip-signature.json"],
                path="control/gossip-signature.json",
            )
            max_age_ms = challenge["max_load_proof_age_ms"]
            deadlines = [challenge["valid_until_unix_ms"]]
            deadlines.extend(
                hop["reservation_expires_at_unix_ms"]
                for hop in challenge["path_manifest"]["ordered_hops"]
            )
            deadlines.extend(
                stage["load_proof_generated_at_unix_ms"] + max_age_ms + 1
                for stage in challenge["stage_evidence"]
            )
            deadlines.append(
                gossip_signature["statement"]["captured_at_unix_ms"]
                + max_age_ms
                + 1
            )
        except Exception as exc:
            raise QualificationAuthorityError("invalid_validated_route_expiry") from exc
        if (
            type(max_age_ms) is not int
            or max_age_ms < 1
            or not deadlines
            or any(type(deadline) is not int or deadline < 0 for deadline in deadlines)
        ):
            raise QualificationAuthorityError("invalid_validated_route_expiry")
        return min(deadlines)
