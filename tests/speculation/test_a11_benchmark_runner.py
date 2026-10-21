# SPDX-License-Identifier: AGPL-3.0-only
"""A11 Wave D: the frozen benchmark arithmetic of ``scripts/run_a11_route_benchmark.py``.

Only the decision path is exercised here (synthetic windows); the live harnesses
need real models and are run by the operator, never in this suite.
"""

from __future__ import annotations

import pytest

import mycelium_speculative_decode as sd
from scripts.run_a11_route_benchmark import (
    WINDOW_ORDER,
    decide,
    paired_lower_bound,
    window_workload,
)

PLAN = "sha256:" + "e" * 64


def _window(mode, wall, ttft=500.0):
    return {
        "mode": mode,
        "completed_requests": 12,
        "target_committed_output_tokens": 384,
        "prompt_length_buckets": 2,
        "output_length_buckets": 2,
        "wall_clock_ms": wall,
        "ttft_p95_ms": ttft,
        "error_timeout_cancellation_fallback_count": 0,
    }


def _windows(target_walls, speculative_walls, *, target_ttft=500.0, speculative_ttft=500.0):
    target, speculative = iter(target_walls), iter(speculative_walls)
    return [
        _window(mode, next(target) if mode == "target_only" else next(speculative),
                target_ttft if mode == "target_only" else speculative_ttft)
        for mode in WINDOW_ORDER
    ]


def test_workload_meets_the_frozen_window_minimums():
    shapes = window_workload()
    assert len(shapes) == sd.BENCHMARK_MINIMUM_COMPLETED_REQUESTS
    assert sum(shape.max_new_tokens for shape in shapes) >= sd.BENCHMARK_MINIMUM_COMMITTED_TOKENS
    assert {shape.prompt_bucket for shape in shapes} == {"short", "long"}
    assert {shape.output_bucket for shape in shapes} == {"short", "long"}
    assert window_workload() == shapes  # frozen and deterministic


def test_material_gain_with_a_tight_pair_spread_is_enabled_and_validates():
    document = decide(_windows([1000, 1010, 990], [600, 610, 590]), plan_digest=PLAN)
    sd.validate_speculative_decode_benchmark(document)
    decision = document["decision"]
    assert decision["state"] == sd.ENABLED_DECISION and decision["blocking_conditions"] == []
    assert decision["paired_lower_bound_fraction"] <= decision["observed_reduction_fraction"]
    assert decision["observed_reduction_fraction"] == pytest.approx(0.4)


def test_small_gain_is_measured_disabled_material_gain_not_observed():
    document = decide(_windows([1000, 1000, 1000], [950, 960, 940]), plan_digest=PLAN)
    sd.validate_speculative_decode_benchmark(document)
    assert document["decision"]["state"] == sd.DISABLED_DECISION
    assert document["decision"]["reason"] == "material_gain_not_observed"


def test_three_pair_bootstrap_bound_is_two_worst_plus_middle_over_three():
    # With three pairs P(resample = worst three times) = 1/27 < 5%, so the 5th
    # percentile is (2 * worst + middle) / 3: one bad pair alone does not block.
    one_bad = _windows([1000, 1000, 1000], [600, 620, 960])
    assert paired_lower_bound(one_bad) == pytest.approx((2 * 0.04 + 0.38) / 3)


def test_two_bad_pairs_drag_the_bootstrap_bound_below_the_bar():
    windows = _windows([1000, 1000, 1000], [600, 960, 960])
    assert paired_lower_bound(windows) < 0.10
    document = decide(windows, plan_digest=PLAN)
    sd.validate_speculative_decode_benchmark(document)
    assert document["decision"]["reason"] == "confidence_bound_not_met"
    assert document["decision"]["blocking_conditions"] == ["paired_lower_bound_below_threshold"]


def test_ttft_regression_blocks_an_otherwise_material_gain():
    document = decide(_windows([1000] * 3, [600] * 3, speculative_ttft=600.0), plan_digest=PLAN)
    sd.validate_speculative_decode_benchmark(document)
    assert document["decision"]["blocking_conditions"] == ["ttft_regression_above_limit"]
    assert document["decision"]["reason"] == "confidence_bound_not_met"


def test_parity_or_order_failure_invalidates_the_pair_without_measurements():
    document = decide(_windows([1000] * 3, [600] * 3), plan_digest=PLAN, invalidation_reasons=["configuration_mismatch"])
    sd.validate_speculative_decode_benchmark(document)
    assert document["valid"] is False
    assert document["decision"]["observed_reduction_fraction"] is None
    assert document["decision"]["blocking_conditions"] == ["benchmark_pair_invalid"]
    reordered = _windows([1000] * 3, [600] * 3)
    reordered[0], reordered[1] = reordered[1], reordered[0]
    assert decide(reordered, plan_digest=PLAN)["invalidation_reasons"] == ["missing_samples"]
