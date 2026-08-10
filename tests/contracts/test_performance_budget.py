from __future__ import annotations

import copy

import pytest

from mycelium_performance_budget import (
    PerformanceBudgetError,
    validate_performance_budget,
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
