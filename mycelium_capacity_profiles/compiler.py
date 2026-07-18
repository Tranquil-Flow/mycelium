from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any, Iterable

from .contracts import (
    CapacityObservation,
    CapacityProfileKey,
    CapacityProfilePolicy,
    EvaluatedCapacityObservation,
    canonical_json_bytes,
)


PROTOCOL = "mycelium.capacity_profile.v1"
EVIDENCE_SCOPE = "bounded_local_samples"


def _validate_sequence(
    points: tuple[EvaluatedCapacityObservation, ...],
    policy: CapacityProfilePolicy,
) -> None:
    if not points:
        raise ValueError("at least one capacity observation is required")
    concurrencies = [point.concurrency for point in points]
    if len(set(concurrencies)) != len(concurrencies):
        raise ValueError("capacity observation concurrency values must be unique")
    if concurrencies[0] != 1:
        raise ValueError("capacity observations must start at 1")
    if concurrencies != list(range(1, concurrencies[-1] + 1)):
        raise ValueError("capacity observations must cover contiguous concurrency")

    for point in points:
        expected = EvaluatedCapacityObservation.from_observation(
            point.as_observation(), policy
        )
        if point != expected:
            raise ValueError("evaluated capacity point does not match measurements")
        if point.safe and point.sample_count < policy.min_samples:
            raise ValueError("every safe observation must satisfy policy min_samples")


def _derive_limits(
    points: tuple[EvaluatedCapacityObservation, ...],
) -> tuple[int, int, int]:
    if not points[0].safe:
        raise ValueError("an observed safe concurrency=1 baseline is required")

    safe_prefix: list[EvaluatedCapacityObservation] = []
    for point in points:
        if not point.safe:
            break
        safe_prefix.append(point)

    interactive_prefix: list[EvaluatedCapacityObservation] = []
    for point in safe_prefix:
        if not point.interactive_slo_met:
            break
        interactive_prefix.append(point)
    if not interactive_prefix:
        raise ValueError("safe concurrency=1 must satisfy interactive SLOs")

    def batch_key(point: EvaluatedCapacityObservation) -> tuple[float, int]:
        assert point.aggregate_output_tps is not None
        return point.aggregate_output_tps, -point.concurrency

    batch = max(safe_prefix, key=batch_key)
    return (
        safe_prefix[-1].concurrency,
        interactive_prefix[-1].concurrency,
        batch.concurrency,
    )


def _unsafe_reasons(point: EvaluatedCapacityObservation) -> list[str]:
    reasons: list[str] = []
    if point.oom:
        reasons.append("oom")
    if point.thermal_throttled:
        reasons.append("thermal_throttled")
    if point.peak_memory_bytes > point.memory_budget_bytes:
        reasons.append("memory_budget_exceeded")
    return reasons


def _slo_reasons(
    point: EvaluatedCapacityObservation,
    policy: CapacityProfilePolicy,
) -> list[str]:
    reasons: list[str] = []
    if point.p95_ttft_ms is not None and point.p95_ttft_ms > policy.ttft_p95_slo_ms:
        reasons.append("ttft_p95_slo")
    if point.p95_tpot_ms is not None and point.p95_tpot_ms > policy.tpot_p95_slo_ms:
        reasons.append("tpot_p95_slo")
    return reasons


def _boundary_document(
    points: tuple[EvaluatedCapacityObservation, ...],
    limit: int,
    policy: CapacityProfilePolicy,
    *,
    interactive: bool,
) -> dict[str, Any]:
    next_index = limit
    if next_index >= len(points):
        return {"kind": "highest_observed", "concurrency": limit}
    point = points[next_index]
    if not point.safe:
        return {
            "kind": "first_unsafe_observation",
            "concurrency": point.concurrency,
            "reasons": _unsafe_reasons(point),
        }
    if interactive:
        return {
            "kind": "first_slo_miss",
            "concurrency": point.concurrency,
            "reasons": _slo_reasons(point, policy),
        }
    raise AssertionError("safe point cannot terminate safety prefix")


@dataclass(frozen=True)
class CapacityProfile:
    key: CapacityProfileKey
    policy: CapacityProfilePolicy
    points: tuple[EvaluatedCapacityObservation, ...]
    max_safe_concurrency: int
    interactive_concurrency_limit: int
    batch_concurrency_limit: int

    def __post_init__(self) -> None:
        _validate_sequence(self.points, self.policy)
        supplied = (
            self.max_safe_concurrency,
            self.interactive_concurrency_limit,
            self.batch_concurrency_limit,
        )
        if any(type(limit) is not int for limit in supplied):
            raise ValueError("capacity limits must be exact integers")
        derived = _derive_limits(self.points)
        if supplied != derived:
            raise ValueError("supplied values do not match derived capacity limits")

    def _unsigned_document(self) -> dict[str, Any]:
        return {
            "protocol": PROTOCOL,
            "key": self.key.to_document(),
            "policy": self.policy.to_document(),
            "points": [point.to_document() for point in self.points],
            "max_safe_concurrency": self.max_safe_concurrency,
            "interactive_concurrency_limit": self.interactive_concurrency_limit,
            "batch_concurrency_limit": self.batch_concurrency_limit,
            "safety_boundary": _boundary_document(
                self.points,
                self.max_safe_concurrency,
                self.policy,
                interactive=False,
            ),
            "interactive_boundary": _boundary_document(
                self.points,
                self.interactive_concurrency_limit,
                self.policy,
                interactive=True,
            ),
            "evidence_scope": EVIDENCE_SCOPE,
            "qualification_evaluated": False,
            "route_ready": False,
            "release_ready": False,
        }

    @property
    def profile_digest(self) -> str:
        payload = canonical_json_bytes(self._unsigned_document())
        return "sha256:" + hashlib.sha256(payload).hexdigest()

    def to_document(self) -> dict[str, Any]:
        document = self._unsigned_document()
        document["profile_digest"] = self.profile_digest
        return document

    def canonical_json_bytes(self) -> bytes:
        return canonical_json_bytes(self.to_document())


def compile_capacity_profile(
    key: CapacityProfileKey,
    observations: Iterable[CapacityObservation],
    policy: CapacityProfilePolicy,
) -> CapacityProfile:
    ordered = sorted(tuple(observations), key=lambda point: point.concurrency)
    evaluated = tuple(
        EvaluatedCapacityObservation.from_observation(point, policy)
        for point in ordered
    )
    _validate_sequence(evaluated, policy)
    max_safe, interactive, batch = _derive_limits(evaluated)
    return CapacityProfile(
        key=key,
        policy=policy,
        points=evaluated,
        max_safe_concurrency=max_safe,
        interactive_concurrency_limit=interactive,
        batch_concurrency_limit=batch,
    )
