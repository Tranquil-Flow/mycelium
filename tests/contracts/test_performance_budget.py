from __future__ import annotations

import copy

import pytest

from mycelium_performance_budget import (
    PerformanceBudgetError,
    validate_performance_budget,
    validate_performance_budget_v2,
)


def _budget() -> dict:
    return {
        "protocol": "mycelium.performance_budget.v1",
        "budget_id": "m12-interactive-v1",
        "workload_label": "interactive_chat_v1",
        "minimum_sample_size": 5,
        "ttft_ms": {"maximum_p50": 5_000.0, "maximum_p95": 10_000.0},
        "tpot_ms": {"maximum_p50": 2_000.0, "maximum_p95": 4_000.0},
        "minimum_output_tokens_per_second": 0.1,
        "maximum_peak_rss_bytes_by_member": {
            "member-a": 20_000_000_000,
            "member-b": 20_000_000_000,
        },
        "maximum_frames_per_request_by_stage": {
            "stage-a": 512,
            "stage-b": 512,
        },
        "execution_scope": "sequential_observed",
        "queueing_budget_state": "deferred_to_m16",
    }


def test_performance_budget_is_closed_and_detached() -> None:
    budget = _budget()
    validated = validate_performance_budget(budget)

    assert validated == budget
    assert validated is not budget


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("minimum_sample_size", True),
        ("minimum_output_tokens_per_second", float("nan")),
        ("execution_scope", "modeled_concurrent"),
        ("queueing_budget_state", "met"),
    ],
)
def test_performance_budget_rejects_unfrozen_or_nonfinite_values(
    field: str,
    value: object,
) -> None:
    budget = _budget()
    budget[field] = value

    with pytest.raises(PerformanceBudgetError):
        validate_performance_budget(budget)


def test_performance_budget_rejects_unknown_fields_and_inverted_percentiles() -> None:
    unknown = {**_budget(), "concurrency": 4}
    with pytest.raises(PerformanceBudgetError):
        validate_performance_budget(unknown)

    inverted = copy.deepcopy(_budget())
    inverted["ttft_ms"] = {"maximum_p50": 10_000.0, "maximum_p95": 5_000.0}
    with pytest.raises(PerformanceBudgetError):
        validate_performance_budget(inverted)


def _budget_v2() -> dict:
    return {
        "protocol": "mycelium.performance_budget.v2",
        "budget_id": "m15-sequential-calibration-v1",
        "profile_id": "interactive_chat_v1",
        "minimum_sample_size": 1,
        "ttft_ms_maximum": 5_000.0,
        "tpot_ms_maximum": 2_000.0,
        "minimum_output_tokens_per_second": 0.5,
        "maximum_frames_per_request": 64,
        "maximum_relative_model_error": {
            "ttft": 1.0,
            "tpot": 1.0,
            "throughput": 1.0,
        },
        "execution_scope": "sequential_observed",
        "peak_memory_budget_state": "approved_exclusion",
        "energy_thermal_budget_state": "approved_exclusion",
        "reconnect_budget_state": "approved_exclusion",
        "queueing_budget_state": "deferred_to_m16",
        "admission_latency_budget_state": "deferred_to_m16",
        "concurrency_budget_state": "deferred_to_m16",
        "batch_shape_budget_state": "deferred_to_m16",
    }


def test_performance_budget_v2_is_closed_and_preserves_m16_boundary() -> None:
    budget = _budget_v2()
    assert validate_performance_budget_v2(copy.deepcopy(budget)) == budget

    budget["queueing_budget_state"] = "met"
    with pytest.raises(PerformanceBudgetError):
        validate_performance_budget_v2(budget)


def test_performance_budget_v2_rejects_unknown_and_nonfinite_values() -> None:
    unknown = {**_budget_v2(), "surprise": True}
    with pytest.raises(PerformanceBudgetError):
        validate_performance_budget_v2(unknown)

    nonfinite = _budget_v2()
    nonfinite["maximum_relative_model_error"]["ttft"] = float("inf")
    with pytest.raises(PerformanceBudgetError):
        validate_performance_budget_v2(nonfinite)
