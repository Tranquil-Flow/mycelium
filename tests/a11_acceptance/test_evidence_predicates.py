# SPDX-License-Identifier: AGPL-3.0-only
from __future__ import annotations

from tests.a11_acceptance.evidence_predicates import (
    MATERIALITY_METRIC,
    exact_target_output_parity_observed,
    material_speedup_observed,
    target_authoritative_cycle_observed,
)


def _mismatch_cycle() -> dict[str, object]:
    return {
        "source_kind": "physical_runtime",
        "target_runtime_incarnation": "target-runtime-4",
        "a10_verifier_binding": "verifier-binding-9",
        "target_logits_and_sampler_authority": True,
        "proposed_positions": 4,
        "target_verified_positions": 4,
        "accepted_positions": 2,
        "rejected_positions": 2,
        "target_correction_positions": 1,
        "rolled_back_target_positions": 2,
        "committed_target_positions": 3,
        "emitted_target_positions": 3,
        "target_authorized_positions": 3,
        "start_target_watermark": 10,
        "rollback_from_watermark": 14,
        "rollback_to_watermark": 12,
        "end_target_watermark": 13,
        "output_released_after_target_commit": True,
    }


def test_target_authority_and_rejection_rollback_accounting_are_observed_only() -> None:
    result = target_authoritative_cycle_observed(_mismatch_cycle())

    assert result.observed is True
    assert result.qualification_evaluated is False


def test_unaccounted_rejection_or_early_output_fails_cycle_predicate() -> None:
    record = _mismatch_cycle()
    record["rejected_positions"] = 1
    record["output_released_after_target_commit"] = False

    result = target_authoritative_cycle_observed(record)

    assert result.observed is False
    assert "accepted_plus_rejected_must_equal_target_verified" in result.reasons
    assert "rollback_count_must_match_rejected_suffix" in result.reasons
    assert "output_release_requires_target_commit" in result.reasons


def test_complete_token_terminal_and_kv_parity_are_all_required() -> None:
    target_only = {
        "complete_target_token_ids": [10, 20, 30],
        "terminal_status": "completed",
        "finish_reason": "length",
        "target_committed_kv_watermark": 31,
    }
    speculative = dict(target_only)
    assert exact_target_output_parity_observed(target_only, speculative).observed is True

    speculative["complete_target_token_ids"] = [10, 20, 31]
    speculative["target_committed_kv_watermark"] = 30
    result = exact_target_output_parity_observed(target_only, speculative)

    assert result.observed is False
    assert "parity_mismatch:complete_target_token_ids" in result.reasons
    assert "parity_mismatch:target_committed_kv_watermark" in result.reasons
    assert result.qualification_evaluated is False


def _materiality_summary() -> dict[str, object]:
    return {
        "source_kind": "physical_runtime",
        "metric": MATERIALITY_METRIC,
        "measured_windows": 6,
        "paired_window_order": [
            "target_only_speculative",
            "speculative_target_only",
            "target_only_speculative",
        ],
        "warmup_windows_per_mode": 1,
        "minimum_completed_requests_in_any_window": 12,
        "minimum_target_committed_output_tokens_in_any_window": 384,
        "wall_clock_reduction_percent": 14.0,
        "paired_95_percent_lower_bound_percent": 11.0,
        "complete_target_output_parity": True,
        "all_outcomes_and_work_in_denominator": True,
        "same_route_session_bindings_unchanged": True,
        "interactive_p95_ttft_regression_percent": 4.0,
        "zero_target_kv_cross_request_state_and_residue": True,
        "rejected_positions": 8,
        "fallback_cycles": 1,
        "timing_basis": "physical_gateway_admission_to_terminal",
    }


def test_honest_physical_materiality_summary_is_observed_not_qualified() -> None:
    result = material_speedup_observed(_materiality_summary())

    assert result.observed is True
    assert result.qualification_evaluated is False


def test_draft_only_fixture_timing_and_missing_rejections_cannot_claim_speedup() -> None:
    summary = _materiality_summary()
    summary["source_kind"] = "fixture"
    summary["metric"] = "draft_generation_time"
    summary["timing_basis"] = "local_fixture"
    summary["rejected_positions"] = 0
    summary["fallback_cycles"] = 0

    result = material_speedup_observed(summary)

    assert result.observed is False
    assert "physical_runtime_source_required" in result.reasons
    assert "end_to_end_target_committed_token_metric_required" in result.reasons
    assert "physical_end_to_end_timing_required" in result.reasons
    assert "nonzero_rejection_or_fallback_required" in result.reasons
