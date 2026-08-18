from __future__ import annotations

import json
from pathlib import Path


MANIFEST = Path(__file__).with_name("scenarios.v1.json")
SPECIFICATION = (
    Path(__file__).parents[2]
    / "docs/superpowers/specs/2026-08-18-mycelium-a11-target-authoritative-speculative-decoding.md"
)
PROTOCOL = "mycelium.a11_acceptance_scenarios.v1"
CLAIM_BOUNDARY = (
    "frozen acceptance inputs only; no product implementation, speculative decoding, "
    "benefit, enabled decision, measured-disabled evaluation, or qualification claim"
)
ALL_WORKSPACES = {
    "inference",
    "device_lab",
    "network",
    "nodes",
    "plans",
    "readiness",
    "incidents",
    "settings",
}
REQUIRED_SCENARIOS = {
    "exact_target_draft_verifier_identity",
    "tokenizer_position_compatibility",
    "incompatible_admission_target_only",
    "separate_target_draft_kv",
    "target_authoritative_all_match_commit",
    "target_mismatch_correction_rollback",
    "cycle_conflict_rollback_to_watermark",
    "cancellation_transaction_cleanup",
    "adaptive_deadline_target_fallback",
    "circuit_breaker_and_cooldown",
    "draft_loss_target_only_fallback",
    "target_loss_explicit_outcome",
    "target_only_same_route_baseline",
    "confidence_bound_material_benefit",
    "invalid_benchmark_pair",
    "measured_disabled_outcome",
    "qualified_enabled_off_by_default",
    "all_workspace_reconstruction_privacy",
}
SCENARIO_FIELDS = {
    "scenario_id",
    "gate_kind",
    "fault_kind",
    "acceptance_scope",
    "maximum_latency_ms",
    "required_invariants",
    "required_workspaces",
}
GATE_KINDS = {
    "deterministic_positive",
    "deterministic_negative",
    "physical_positive",
    "physical_negative",
    "browser_positive",
}
ACCEPTANCE_SCOPES = {
    "overlay",
    "request",
    "cycle",
    "benchmark",
    "decision",
    "session",
}
IDENTITY_COMPATIBILITY_REQUIREMENTS = {
    "target_identity_fields": [
        "model",
        "revision",
        "manifest",
        "representation",
        "graph",
        "assignments",
        "path_generation",
        "load_proofs",
        "qualification",
        "runtime_incarnations",
    ],
    "draft_identity_fields": [
        "model",
        "revision",
        "manifest",
        "representation",
        "artifact_authorization",
        "runtime",
        "load_proof",
        "draft_role_qualification",
        "resource_reservation",
    ],
    "tokenizer_compatibility_fields": [
        "implementation_digest",
        "canonical_token_id_mapping",
        "canonical_token_count",
        "special_token_semantics",
        "normalization",
        "byte_fallback",
    ],
    "position_compatibility_fields": [
        "position_indexing",
        "rotary_position_semantics",
        "attention_mask",
        "context_truncation",
    ],
    "initial_generation_policy": "deterministic_greedy",
    "padded_head_sizes_may_differ_with_full_canonical_coverage": True,
    "noncanonical_and_reserved_ids_must_be_masked_identically": True,
    "unsupported_or_unknown_generation_policy_uses_target_only": True,
}
KV_AUTHORITY_REQUIREMENTS = {
    "target_kv_owner": "target_runtime",
    "draft_kv_owner": "draft_runtime",
    "temporary_verifier_kv_owner": "a10_target_verifier",
    "output_and_committed_watermark_authority": "target_runtime",
    "target_and_draft_kv_may_alias": False,
    "target_and_draft_kv_may_transfer": False,
    "draft_cleanup_may_rollback_target_kv": False,
}
ADAPTIVE_POLICY_REQUIREMENTS = {
    "deadline_formula": "min(100_ms,max(5_ms,0.75*warm_target_only_p95_tpot))",
    "minimum_proposal_deadline_ms": 5,
    "maximum_proposal_deadline_ms": 100,
    "warm_target_only_p95_tpot_multiplier": 0.75,
    "request_remaining_deadline_clamp_required": True,
    "consecutive_deadlines_to_open_breaker": 3,
    "rolling_performance_window_cycles": 20,
    "performance_and_thermal_cooldown_seconds": 300,
    "compatibility_or_parity_violation_revokes_new_admission": True,
}
BENCHMARK_REQUIREMENTS = {
    "paired_window_order": [
        "target_only_speculative",
        "speculative_target_only",
        "target_only_speculative",
    ],
    "measured_windows": 6,
    "warmup_windows_per_mode": 1,
    "minimum_completed_requests_per_window": 12,
    "minimum_target_committed_output_tokens_per_window": 384,
    "minimum_prompt_length_buckets": 2,
    "minimum_output_length_buckets": 2,
    "wall_clock_time_reduction_percent": 10,
    "paired_bootstrap_confidence_percent": 95,
    "improvement_lower_bound_percent": 10,
    "maximum_interactive_p95_ttft_regression_percent": 10,
    "same_route_session_bindings": [
        "target",
        "draft",
        "representations",
        "verifier_capability",
        "route",
        "product_session",
        "generation",
        "stage_runtimes",
        "workload",
        "arrival_schedule",
        "generation_policy",
        "proposal_width",
        "proposal_deadline",
        "circuit_breaker",
        "token_limits",
        "instrumentation",
    ],
    "error_timeout_cancellation_fallback_and_rejection_remain_in_accounting": True,
    "nonzero_proposal_verification_acceptance_and_rejection_or_fallback_required": True,
    "exact_target_only_output_parity_required": True,
    "zero_target_kv_corruption_cross_request_state_and_residue_required": True,
}
DECISION_REQUIREMENTS = {
    "enabled_decision": "qualified_enabled",
    "disabled_decision": "measured_disabled",
    "default_mode": "target_only",
    "enabled_preference_is_off_by_default": True,
    "enabled_preference_applies_to_future_requests_only": True,
    "disabled_reasons": [
        "no_authorized_local_draft",
        "incompatible_token_semantics",
        "target_verifier_unavailable",
        "parity_failed",
        "insufficient_acceptance",
        "draft_deadline_exceeded",
        "material_gain_not_observed",
        "confidence_bound_not_met",
    ],
    "capability_unavailable_may_fabricate_performance_sample": False,
    "target_only_remains_independently_available": True,
}
WORKSPACE_SHARED_INVARIANTS = [
    "same_current_public_generation",
    "live_stale_historical_fixture_modeled_unknown_enabled_disabled_fallback_and_failed_are_distinct",
    "missing_measurements_never_default_to_zero_acceptance_zero_cost_or_success",
    "private_prompt_output_token_kv_and_sampler_state_are_excluded",
    "navigation_reconnect_terminal_history_and_second_session_reconstruct",
]
WORKSPACE_REQUIREMENTS = {
    "inference": [
        "target_mode_preference_proposal_verified_accepted_rollback_and_fallback_state",
        "target_owned_progress_deadline_and_exact_disabled_or_fallback_reason",
    ],
    "device_lab": [
        "draft_compatibility_verifier_width_deadline_parity_lifecycle_and_cleanup",
        "platform_qualification_and_synthetic_worker_ineligibility",
    ],
    "network": [
        "target_draft_roles_private_proposal_movement_and_verification_spans",
        "selected_path_and_fallback_are_distinct_from_modeled_animation",
    ],
    "nodes": [
        "exact_target_draft_identity_authorization_load_qualification_and_kv_ownership",
        "reservations_proposals_acceptance_deadline_thermal_state_and_freshness",
    ],
    "plans": [
        "compatibility_workload_width_deadline_acceptance_cost_and_same_route_baseline",
        "confidence_materiality_promotion_and_target_only_fallback",
    ],
    "readiness": [
        "separate_target_verifier_draft_compatibility_parity_rollback_and_fallback_proofs",
        "cleanup_benchmark_confidence_freshness_and_promotion_proofs",
    ],
    "incidents": [
        "incompatibility_timeout_low_acceptance_draft_loss_verifier_and_target_loss",
        "rollback_cancellation_circuit_break_revocation_and_target_only_outcome",
    ],
    "settings": [
        "target_only_default_and_qualified_future_request_preference",
        "bounded_width_deadline_privacy_retention_and_human_readable_disabled_reason",
    ],
}
REQUIRED_CONCERN_INVARIANTS = {
    "exact_target_draft_verifier_identity": {
        "target_identity_matches_selected_target_only_binding_exactly",
        "draft_identity_and_authorization_are_exact_and_distinct",
        "a10_verifier_matches_exact_target_runtime_and_width",
    },
    "tokenizer_position_compatibility": {
        "tokenizer_implementation_mapping_count_special_tokens_normalization_and_bytes_match",
        "position_rotary_attention_mask_and_context_truncation_match",
        "active_position_behavior_difference_rejects_speculation",
    },
    "separate_target_draft_kv": {
        "target_draft_and_temporary_verifier_kv_have_separate_owners",
        "matching_schema_digest_does_not_authorize_alias_or_transfer",
        "draft_cannot_address_commit_or_rollback_target_kv",
        "zero_kv_contamination_or_residue",
    },
    "target_authoritative_all_match_commit": {
        "all_matching_positions_are_target_verified",
        "target_commit_is_atomic_monotonic_and_idempotent",
        "output_releases_only_after_target_commit",
    },
    "target_mismatch_correction_rollback": {
        "tentative_target_state_after_first_mismatch_rolls_back",
        "accepted_prefix_plus_one_target_correction_commits_atomically",
        "browser_output_matches_target_only_execution_without_duplicate_or_gap",
    },
    "adaptive_deadline_target_fallback": {
        "proposal_deadline_uses_frozen_formula_and_exact_route_measurement",
        "target_does_not_wait_past_deadline",
        "ordinary_target_step_continues_from_committed_watermark",
    },
    "circuit_breaker_and_cooldown": {
        "three_consecutive_deadlines_open_request_breaker",
        "twenty_cycle_nonbeneficial_window_opens_request_breaker",
        "performance_or_thermal_cause_uses_five_minute_cooldown",
    },
    "target_only_same_route_baseline": {
        "target_only_and_speculative_modes_share_one_stable_product_session",
        "route_target_workload_generation_and_instrumentation_are_identical",
        "target_only_output_and_committed_kv_are_baseline_authority",
    },
    "confidence_bound_material_benefit": {
        "wall_clock_per_target_committed_token_improves_at_least_ten_percent",
        "paired_ninety_five_percent_lower_bound_is_at_least_ten_percent",
        "target_only_output_parity_and_reliability_hold",
    },
    "measured_disabled_outcome": {
        "failed_applicable_gate_publishes_measured_disabled_not_enabled",
        "capability_unavailable_never_fabricates_performance_sample",
        "disabled_evaluation_makes_no_benefit_claim",
        "target_only_remains_qualified_selectable_and_available",
    },
}


def _manifest() -> dict:
    value = json.loads(MANIFEST.read_text("utf-8"))
    assert isinstance(value, dict)
    return value


def _scenarios_by_id() -> dict[str, dict]:
    return {
        scenario["scenario_id"]: scenario for scenario in _manifest()["scenarios"]
    }


def test_a11_acceptance_manifest_is_closed_bounded_and_complete() -> None:
    manifest = _manifest()
    assert set(manifest) == {
        "protocol",
        "claim_boundary",
        "identity_compatibility_requirements",
        "kv_authority_requirements",
        "adaptive_policy_requirements",
        "benchmark_requirements",
        "decision_requirements",
        "workspace_shared_invariants",
        "workspace_requirements",
        "scenarios",
    }
    assert manifest["protocol"] == PROTOCOL
    assert manifest["claim_boundary"] == CLAIM_BOUNDARY
    scenarios = manifest["scenarios"]
    assert isinstance(scenarios, list)
    assert 1 <= len(scenarios) <= 64
    assert {scenario["scenario_id"] for scenario in scenarios} == REQUIRED_SCENARIOS

    covered_workspaces: set[str] = set()
    for scenario in scenarios:
        assert isinstance(scenario, dict)
        assert set(scenario) == SCENARIO_FIELDS
        assert scenario["gate_kind"] in GATE_KINDS
        assert scenario["acceptance_scope"] in ACCEPTANCE_SCOPES
        assert isinstance(scenario["fault_kind"], str) and scenario["fault_kind"]
        latency = scenario["maximum_latency_ms"]
        assert latency is None or type(latency) is int and 1 <= latency <= 100
        invariants = scenario["required_invariants"]
        assert isinstance(invariants, list) and 1 <= len(invariants) <= 16
        assert len(invariants) == len(set(invariants))
        assert all(
            isinstance(invariant, str)
            and invariant
            and invariant == invariant.lower()
            and len(invariant) <= 96
            for invariant in invariants
        )
        workspaces = scenario["required_workspaces"]
        assert isinstance(workspaces, list) and 1 <= len(workspaces) <= 8
        assert len(workspaces) == len(set(workspaces))
        assert set(workspaces) <= ALL_WORKSPACES
        covered_workspaces.update(workspaces)

    assert covered_workspaces == ALL_WORKSPACES


def test_a11_exact_identity_tokenizer_and_position_requirements_are_frozen() -> None:
    manifest = _manifest()
    assert (
        manifest["identity_compatibility_requirements"]
        == IDENTITY_COMPATIBILITY_REQUIREMENTS
    )
    scenarios = _scenarios_by_id()
    for scenario_id in (
        "exact_target_draft_verifier_identity",
        "tokenizer_position_compatibility",
    ):
        assert REQUIRED_CONCERN_INVARIANTS[scenario_id] <= set(
            scenarios[scenario_id]["required_invariants"]
        )
    assert {
        "unsupported_sampling_never_silently_switches_to_greedy",
        "target_only_admission_remains_available",
    } <= set(scenarios["incompatible_admission_target_only"]["required_invariants"])


def test_a11_separate_kv_target_commit_and_rollback_are_frozen() -> None:
    manifest = _manifest()
    assert manifest["kv_authority_requirements"] == KV_AUTHORITY_REQUIREMENTS
    scenarios = _scenarios_by_id()
    for scenario_id in (
        "separate_target_draft_kv",
        "target_authoritative_all_match_commit",
        "target_mismatch_correction_rollback",
    ):
        assert REQUIRED_CONCERN_INVARIANTS[scenario_id] <= set(
            scenarios[scenario_id]["required_invariants"]
        )
    assert {
        "target_rolls_back_to_previous_committed_watermark",
        "no_uncommitted_token_is_emitted_or_persisted",
        "newer_cycle_and_unrelated_requests_remain_unchanged",
    } <= set(scenarios["cycle_conflict_rollback_to_watermark"]["required_invariants"])


def test_a11_adaptive_deadline_circuit_breaker_and_fallback_are_frozen() -> None:
    manifest = _manifest()
    assert manifest["adaptive_policy_requirements"] == ADAPTIVE_POLICY_REQUIREMENTS
    scenarios = _scenarios_by_id()
    for scenario_id in (
        "adaptive_deadline_target_fallback",
        "circuit_breaker_and_cooldown",
    ):
        assert REQUIRED_CONCERN_INVARIANTS[scenario_id] <= set(
            scenarios[scenario_id]["required_invariants"]
        )
    assert {
        "fallback_starts_from_exact_target_committed_watermark",
        "draft_resources_cleanup_without_rolling_back_target",
    } <= set(scenarios["draft_loss_target_only_fallback"]["required_invariants"])
    assert {
        "target_or_immutable_path_loss_is_never_labelled_draft_fallback",
        "missing_committed_watermark_proof_terminates_instead_of_guessing",
    } <= set(scenarios["target_loss_explicit_outcome"]["required_invariants"])


def test_a11_target_only_baseline_confidence_and_decisions_are_frozen() -> None:
    manifest = _manifest()
    assert manifest["benchmark_requirements"] == BENCHMARK_REQUIREMENTS
    assert manifest["decision_requirements"] == DECISION_REQUIREMENTS
    scenarios = _scenarios_by_id()
    for scenario_id in (
        "target_only_same_route_baseline",
        "confidence_bound_material_benefit",
        "measured_disabled_outcome",
    ):
        assert REQUIRED_CONCERN_INVARIANTS[scenario_id] <= set(
            scenarios[scenario_id]["required_invariants"]
        )
    assert {
        "missing_sample_invalidates_pair_instead_of_becoming_zero",
        "posthoc_threshold_workload_width_deadline_and_exclusion_changes_are_rejected",
        "invalid_pair_cannot_publish_enabled_or_benefit_decision",
    } <= set(scenarios["invalid_benchmark_pair"]["required_invariants"])
    assert {
        "speculation_remains_off_globally_and_for_existing_requests",
        "explicit_preference_applies_only_to_future_request",
    } <= set(scenarios["qualified_enabled_off_by_default"]["required_invariants"])


def test_a11_workspace_inventory_is_closed_and_private() -> None:
    manifest = _manifest()
    assert manifest["workspace_shared_invariants"] == WORKSPACE_SHARED_INVARIANTS
    assert manifest["workspace_requirements"] == WORKSPACE_REQUIREMENTS
    assert set(manifest["workspace_requirements"]) == ALL_WORKSPACES
    scenario = _scenarios_by_id()["all_workspace_reconstruction_privacy"]
    assert set(scenario["required_workspaces"]) == ALL_WORKSPACES
    assert {
        "all_eight_workspaces_use_same_current_public_generation",
        "clean_second_session_cannot_read_prompt_output_tokens_kv_or_sampler_state",
        "target_authority_fallback_benchmark_and_decision_attribution_is_truthful",
    } <= set(scenario["required_invariants"])


def test_a11_specification_remains_design_only_and_names_the_inventory() -> None:
    specification = SPECIFICATION.read_text("utf-8")
    assert "**Status:** `design_only`; dependency-ready acceptance boundary" in specification
    assert "capability remains `design_only`" in specification
    assert all(f"`{scenario_id}`" in specification for scenario_id in REQUIRED_SCENARIOS)
