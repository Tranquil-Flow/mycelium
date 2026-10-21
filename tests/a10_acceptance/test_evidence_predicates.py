# SPDX-License-Identifier: AGPL-3.0-only
from __future__ import annotations

from tests.a10_acceptance.evidence_predicates import (
    physical_compute_overlap_observed,
    runtime_batch_observed,
)


def _batch_record() -> dict[str, object]:
    return {
        "source_kind": "physical_runtime",
        "ordinary_gateway_product_path": True,
        "runtime_invocation_id": "invocation-7",
        "actual_member_ids": ["request-a", "request-b"],
        "actual_batch_size": 2,
        "scatter_result_count": 2,
        "backend_invocation_counter_before": 40,
        "backend_invocation_counter_after": 41,
        "device_compute_counter_before": 90,
        "device_compute_counter_after": 91,
    }


def test_one_measured_backend_invocation_can_satisfy_observation_predicate_only() -> None:
    result = runtime_batch_observed(_batch_record())

    assert result.observed is True
    assert result.qualification_evaluated is False


def test_concurrent_requests_with_separate_backend_calls_are_not_runtime_batching() -> None:
    record = _batch_record()
    record["backend_invocation_counter_after"] = 42

    result = runtime_batch_observed(record)

    assert result.observed is False
    assert "single_backend_invocation_delta_required" in result.reasons
    assert result.qualification_evaluated is False


def test_fixture_label_cannot_satisfy_runtime_batch_observation() -> None:
    record = _batch_record()
    record["source_kind"] = "fixture"

    result = runtime_batch_observed(record)

    assert result.observed is False
    assert "physical_runtime_source_required" in result.reasons


def _compute_intervals() -> list[dict[str, object]]:
    return [
        {
            "source_kind": "physical_runtime",
            "ordinary_gateway_product_path": True,
            "interval_kind": "device_observed_stage_compute",
            "microbatch_id": "microbatch-a",
            "stage_id": "stage-1",
            "device_id": "device-1",
            "compute_start_monotonic_ns": 100,
            "compute_end_monotonic_ns": 200,
            "clock_uncertainty_ns": 5,
            "predecessor_completion_monotonic_ns": 100,
        },
        {
            "source_kind": "physical_runtime",
            "ordinary_gateway_product_path": True,
            "interval_kind": "device_observed_stage_compute",
            "microbatch_id": "microbatch-b",
            "stage_id": "stage-2",
            "device_id": "device-2",
            "compute_start_monotonic_ns": 150,
            "compute_end_monotonic_ns": 250,
            "clock_uncertainty_ns": 5,
            "predecessor_completion_monotonic_ns": 140,
        },
    ]


def test_uncertainty_adjusted_device_compute_overlap_is_observed_not_qualified() -> None:
    result = physical_compute_overlap_observed(_compute_intervals())

    assert result.observed is True
    assert result.qualification_evaluated is False


def test_queue_overlap_and_clock_uncertainty_do_not_masquerade_as_compute_overlap() -> None:
    intervals = _compute_intervals()
    intervals[0]["interval_kind"] = "queue"
    intervals[1]["compute_start_monotonic_ns"] = 190

    result = physical_compute_overlap_observed(intervals)

    assert result.observed is False
    assert "device_stage_compute_interval_required" in result.reasons
    assert "positive_uncertainty_adjusted_compute_overlap_required" in result.reasons
