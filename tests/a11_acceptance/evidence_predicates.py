# SPDX-License-Identifier: AGPL-3.0-only
"""Deterministic predicates for A11 evidence; never an A11 qualifier."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Sequence


MATERIALITY_METRIC = (
    "gateway_admission_to_terminal_wall_clock_per_target_committed_token"
)


@dataclass(frozen=True)
class PredicateResult:
    observed: bool
    reasons: tuple[str, ...]
    qualification_evaluated: bool = False


def _count(record: Mapping[str, object], name: str) -> int | None:
    value = record.get(name)
    if type(value) is not int or value < 0:
        return None
    return value


def target_authoritative_cycle_observed(record: Mapping[str, object]) -> PredicateResult:
    """Check target authority plus rejection, rollback, commit, and emission accounting."""

    reasons: list[str] = []
    if record.get("source_kind") != "physical_runtime":
        reasons.append("physical_runtime_source_required")
    for field in ("target_runtime_incarnation", "a10_verifier_binding"):
        if not isinstance(record.get(field), str) or not record.get(field):
            reasons.append(f"{field}_required")
    if record.get("target_logits_and_sampler_authority") is not True:
        reasons.append("target_logits_and_sampler_authority_required")
    names = (
        "proposed_positions",
        "target_verified_positions",
        "accepted_positions",
        "rejected_positions",
        "target_correction_positions",
        "rolled_back_target_positions",
        "committed_target_positions",
        "emitted_target_positions",
        "target_authorized_positions",
        "start_target_watermark",
        "rollback_from_watermark",
        "rollback_to_watermark",
        "end_target_watermark",
    )
    counts = {name: _count(record, name) for name in names}
    if any(value is None for value in counts.values()):
        return PredicateResult(False, ("closed_nonnegative_cycle_accounting_required",))
    values = {name: int(value) for name, value in counts.items() if value is not None}
    if values["proposed_positions"] != values["target_verified_positions"]:
        reasons.append("every_proposal_position_must_be_target_verified")
    if (
        values["accepted_positions"] + values["rejected_positions"]
        != values["target_verified_positions"]
    ):
        reasons.append("accepted_plus_rejected_must_equal_target_verified")
    if (
        values["committed_target_positions"]
        != values["accepted_positions"] + values["target_correction_positions"]
    ):
        reasons.append("commit_must_equal_accepted_plus_target_corrections")
    if not (
        values["emitted_target_positions"]
        == values["committed_target_positions"]
        == values["target_authorized_positions"]
    ):
        reasons.append("emission_commit_and_target_authority_must_match")
    if (
        values["rollback_from_watermark"] - values["rollback_to_watermark"]
        != values["rolled_back_target_positions"]
        or values["rolled_back_target_positions"] != values["rejected_positions"]
    ):
        reasons.append("rollback_count_must_match_rejected_suffix")
    if (
        values["rollback_to_watermark"]
        != values["start_target_watermark"] + values["accepted_positions"]
    ):
        reasons.append("rollback_must_end_at_target_verified_prefix")
    if (
        values["end_target_watermark"]
        != values["start_target_watermark"]
        + values["committed_target_positions"]
    ):
        reasons.append("end_watermark_must_match_target_commit")
    if record.get("output_released_after_target_commit") is not True:
        reasons.append("output_release_requires_target_commit")
    return PredicateResult(not reasons, tuple(sorted(reasons)))


def exact_target_output_parity_observed(
    target_only: Mapping[str, object], speculative: Mapping[str, object]
) -> PredicateResult:
    """Compare the complete target result, not decoded text or a prefix alone."""

    reasons: list[str] = []
    fields = (
        "complete_target_token_ids",
        "terminal_status",
        "finish_reason",
        "target_committed_kv_watermark",
    )
    for field in fields:
        if target_only.get(field) != speculative.get(field):
            reasons.append(f"parity_mismatch:{field}")
    token_ids = target_only.get("complete_target_token_ids")
    if (
        not isinstance(token_ids, Sequence)
        or isinstance(token_ids, (str, bytes))
        or not all(type(token_id) is int and token_id >= 0 for token_id in token_ids)
    ):
        reasons.append("complete_target_token_ids_required")
    return PredicateResult(not reasons, tuple(sorted(reasons)))


def material_speedup_observed(summary: Mapping[str, object]) -> PredicateResult:
    """Require physical paired end-to-end materiality with honest denominators."""

    reasons: list[str] = []
    if summary.get("source_kind") != "physical_runtime":
        reasons.append("physical_runtime_source_required")
    if summary.get("metric") != MATERIALITY_METRIC:
        reasons.append("end_to_end_target_committed_token_metric_required")
    if _count(summary, "measured_windows") != 6:
        reasons.append("six_measured_windows_required")
    if summary.get("paired_window_order") != [
        "target_only_speculative",
        "speculative_target_only",
        "target_only_speculative",
    ]:
        reasons.append("frozen_paired_window_order_required")
    if _count(summary, "warmup_windows_per_mode") != 1:
        reasons.append("one_warmup_window_per_mode_required")
    minimum_requests = _count(summary, "minimum_completed_requests_in_any_window")
    if minimum_requests is None or minimum_requests < 12:
        reasons.append("minimum_completed_requests_per_window_required")
    minimum_tokens = _count(
        summary, "minimum_target_committed_output_tokens_in_any_window"
    )
    if minimum_tokens is None or minimum_tokens < 384:
        reasons.append("minimum_target_committed_tokens_per_window_required")
    point = summary.get("wall_clock_reduction_percent")
    lower = summary.get("paired_95_percent_lower_bound_percent")
    if not isinstance(point, (int, float)) or isinstance(point, bool) or point < 10:
        reasons.append("ten_percent_point_improvement_required")
    if not isinstance(lower, (int, float)) or isinstance(lower, bool) or lower < 10:
        reasons.append("ten_percent_paired_lower_bound_required")
    if summary.get("complete_target_output_parity") is not True:
        reasons.append("complete_target_output_parity_required")
    if summary.get("all_outcomes_and_work_in_denominator") is not True:
        reasons.append("complete_denominator_accounting_required")
    if summary.get("same_route_session_bindings_unchanged") is not True:
        reasons.append("same_route_session_bindings_required")
    ttft = summary.get("interactive_p95_ttft_regression_percent")
    if not isinstance(ttft, (int, float)) or isinstance(ttft, bool) or ttft > 10:
        reasons.append("interactive_ttft_regression_limit_required")
    if summary.get("zero_target_kv_cross_request_state_and_residue") is not True:
        reasons.append("zero_target_kv_cross_request_state_and_residue_required")
    rejected = _count(summary, "rejected_positions")
    fallback = _count(summary, "fallback_cycles")
    if rejected is None or fallback is None or rejected + fallback <= 0:
        reasons.append("nonzero_rejection_or_fallback_required")
    if summary.get("timing_basis") != "physical_gateway_admission_to_terminal":
        reasons.append("physical_end_to_end_timing_required")
    return PredicateResult(not reasons, tuple(sorted(reasons)))
