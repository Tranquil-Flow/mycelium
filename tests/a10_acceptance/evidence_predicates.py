# SPDX-License-Identifier: AGPL-3.0-only
"""Deterministic predicates for classifying A10 evidence without qualifying A10."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Sequence


@dataclass(frozen=True)
class PredicateResult:
    observed: bool
    reasons: tuple[str, ...]
    qualification_evaluated: bool = False


def _integer(record: Mapping[str, object], name: str) -> int | None:
    value = record.get(name)
    return value if type(value) is int and value >= 0 else None


def runtime_batch_observed(record: Mapping[str, object]) -> PredicateResult:
    """Distinguish one measured backend batch from request-level concurrency."""

    reasons: list[str] = []
    if record.get("source_kind") != "physical_runtime":
        reasons.append("physical_runtime_source_required")
    if record.get("ordinary_gateway_product_path") is not True:
        reasons.append("ordinary_gateway_product_path_required")
    if not isinstance(record.get("runtime_invocation_id"), str) or not record.get(
        "runtime_invocation_id"
    ):
        reasons.append("runtime_invocation_id_required")
    members = record.get("actual_member_ids")
    if (
        not isinstance(members, Sequence)
        or isinstance(members, (str, bytes))
        or len(members) < 2
        or not all(isinstance(member, str) and member for member in members)
        or len(set(members)) != len(members)
    ):
        reasons.append("multiple_unique_actual_members_required")
        member_count = None
    else:
        member_count = len(members)
    actual_size = _integer(record, "actual_batch_size")
    scatter_count = _integer(record, "scatter_result_count")
    if (
        member_count is None
        or actual_size != member_count
        or scatter_count != member_count
    ):
        reasons.append("actual_member_batch_scatter_count_mismatch")
    backend_before = _integer(record, "backend_invocation_counter_before")
    backend_after = _integer(record, "backend_invocation_counter_after")
    if (
        backend_before is None
        or backend_after is None
        or backend_after - backend_before != 1
    ):
        reasons.append("single_backend_invocation_delta_required")
    compute_before = _integer(record, "device_compute_counter_before")
    compute_after = _integer(record, "device_compute_counter_after")
    if (
        compute_before is None
        or compute_after is None
        or compute_after <= compute_before
    ):
        reasons.append("positive_device_compute_delta_required")
    return PredicateResult(not reasons, tuple(sorted(reasons)))


def physical_compute_overlap_observed(
    intervals: Sequence[Mapping[str, object]],
) -> PredicateResult:
    """Require uncertainty-adjusted device compute overlap for two microbatches."""

    reasons: list[str] = []
    if len(intervals) != 2:
        return PredicateResult(False, ("exactly_two_compute_intervals_required",))
    microbatches: list[object] = []
    stages: list[object] = []
    devices: list[object] = []
    normalized: list[tuple[int, int, int]] = []
    for interval in intervals:
        if interval.get("source_kind") != "physical_runtime":
            reasons.append("physical_runtime_source_required")
        if interval.get("ordinary_gateway_product_path") is not True:
            reasons.append("ordinary_gateway_product_path_required")
        if interval.get("interval_kind") != "device_observed_stage_compute":
            reasons.append("device_stage_compute_interval_required")
        microbatches.append(interval.get("microbatch_id"))
        stages.append(interval.get("stage_id"))
        devices.append(interval.get("device_id"))
        start = _integer(interval, "compute_start_monotonic_ns")
        end = _integer(interval, "compute_end_monotonic_ns")
        uncertainty = _integer(interval, "clock_uncertainty_ns")
        predecessor = _integer(interval, "predecessor_completion_monotonic_ns")
        if (
            start is None
            or end is None
            or uncertainty is None
            or predecessor is None
            or uncertainty < 0
            or end <= start
        ):
            reasons.append("valid_monotonic_compute_interval_required")
            continue
        if predecessor > start:
            reasons.append("stage_predecessor_must_complete_before_compute")
        normalized.append((start, end, uncertainty))
    if any(not isinstance(value, str) or not value for value in microbatches):
        reasons.append("microbatch_identity_required")
    elif microbatches[0] == microbatches[1]:
        reasons.append("different_microbatches_required")
    if any(not isinstance(value, str) or not value for value in stages):
        reasons.append("stage_identity_required")
    elif stages[0] == stages[1]:
        reasons.append("different_stages_required")
    if any(not isinstance(value, str) or not value for value in devices):
        reasons.append("device_identity_required")
    if len(normalized) == 2:
        first, second = normalized
        raw_intersection = min(first[1], second[1]) - max(first[0], second[0])
        proven_intersection = raw_intersection - first[2] - second[2]
        if proven_intersection < 1:
            reasons.append("positive_uncertainty_adjusted_compute_overlap_required")
    return PredicateResult(not reasons, tuple(sorted(set(reasons))))
