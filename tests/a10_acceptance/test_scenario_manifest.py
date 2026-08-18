from __future__ import annotations

import json
from pathlib import Path


MANIFEST = Path(__file__).with_name("scenarios.v1.json")
SPECIFICATION = (
    Path(__file__).parents[2]
    / "docs/superpowers/specs/2026-08-18-mycelium-a10-runtime-batching-overlap-target-verification.md"
)
PROTOCOL = "mycelium.a10_acceptance_scenarios.v1"
CLAIM_BOUNDARY = (
    "frozen acceptance inputs only; no product implementation, runtime batching, "
    "overlap, verifier, benchmark, or qualification claim"
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
    "real_runtime_batch_membership",
    "continuous_late_arrival",
    "causal_pipeline_overlap",
    "overlap_trace_rejection",
    "bounded_queue_qos_aging",
    "slow_consumer_isolation",
    "cancellation_boundary_isolation",
    "batch_fail_closed_rollback",
    "cross_member_kv_isolation",
    "target_verifier_transaction",
    "target_verifier_rollback_to_prefix",
    "target_verifier_sampling_rejection",
    "same_route_confidence_benchmark",
    "invalid_benchmark_pair",
    "runtime_exit_shutdown_cleanup",
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
    "deterministic_negative",
    "physical_positive",
    "physical_negative",
    "browser_positive",
}
ACCEPTANCE_SCOPES = {
    "session",
    "request",
    "batch",
    "placement",
    "path",
    "verifier_operation",
    "benchmark",
}
RUNTIME_BATCH_REQUIREMENTS = {
    "final_membership_authority": "stage_runtime",
    "minimum_physical_batch_members": 2,
    "backend_invocations_per_observed_batch": 1,
    "candidate_membership_alone_qualifies": False,
    "concurrent_admission_alone_qualifies": False,
    "sequential_loop_alone_qualifies": False,
}
TARGET_VERIFIER_REQUIREMENTS = {
    "minimum_operations": 100,
    "qualified_width_classes": ["one", "qualified_default", "qualified_maximum"],
    "minimum_prefix_length_buckets": 2,
    "generation_policy": "deterministic_greedy",
    "transaction_modes": [
        "rollback_to_prefix",
        "commit_verified_prefix",
        "commit_verified_prefix_plus_target_correction",
    ],
    "single_backend_call_required": True,
    "sequential_target_parity_required": True,
}
BENCHMARK_REQUIREMENTS = {
    "paired_window_order": [
        "baseline_candidate",
        "candidate_baseline",
        "baseline_candidate",
    ],
    "measured_windows": 6,
    "warmup_windows_per_mode": 1,
    "minimum_completed_requests_per_window": 60,
    "minimum_interactive_requests_per_window": 20,
    "minimum_late_arrivals_per_window": 20,
    "minimum_prompt_length_buckets": 2,
    "minimum_output_length_buckets": 2,
    "throughput_improvement_percent": 10,
    "paired_bootstrap_confidence_percent": 95,
    "throughput_lower_bound_percent": 10,
    "maximum_interactive_p95_ttft_regression_percent": 10,
    "maximum_interactive_p95_tpot_regression_percent": 10,
    "same_route_bindings": [
        "target",
        "representation",
        "route",
        "product_sessions",
        "workload",
        "arrival_schedule",
        "token_limits",
        "measurement_window",
        "instrumentation",
    ],
    "failed_cancelled_overflowed_and_slow_consumers_remain_in_denominator": True,
    "exact_output_parity_required": True,
    "zero_residual_resources_required": True,
}
WORKSPACE_SHARED_INVARIANTS = [
    "same_current_public_generation",
    "live_stale_historical_fixture_modeled_unknown_disabled_and_failed_are_distinct",
    "missing_observations_never_default_to_zero_or_success",
    "private_prompt_output_and_token_data_are_excluded",
    "navigation_reconnect_terminal_history_and_second_session_reconstruct",
]
WORKSPACE_REQUIREMENTS = {
    "inference": [
        "actual_batch_identity_size_member_position_qos_and_collection_reason",
        "slow_consumer_cancellation_verifier_use_and_terminal_attribution",
    ],
    "device_lab": [
        "backend_batch_and_verifier_limits_parity_cleanup_incarnation_and_freshness",
        "synthetic_workers_remain_nonqualifying",
    ],
    "network": [
        "physical_stage_batch_flow_and_uncertainty_aware_overlap",
        "modeled_queue_and_transport_only_concurrency_are_distinct",
    ],
    "nodes": [
        "runtime_mode_actual_size_bytes_limits_queue_reservation_kv_and_utilization",
        "verifier_support_and_freshness",
    ],
    "plans": [
        "modeled_choice_observed_membership_workload_exclusions_and_bottleneck",
        "predicted_observed_gain_and_same_route_baseline_comparison",
    ],
    "readiness": [
        "independent_batch_arrival_overlap_parity_verifier_latency_and_throughput_proofs",
        "cleanup_and_freshness_proofs",
    ],
    "incidents": [
        "overflow_starvation_slow_consumer_cancellation_partial_failure_and_stale_result",
        "verifier_rollback_and_bounded_cleanup_with_narrow_scope",
    ],
    "settings": [
        "qualified_future_request_qos_batch_window_slow_consumer_and_verifier_limits",
        "unsafe_or_unqualified_values_disabled_with_reason",
    ],
}
REQUIRED_CONCERN_INVARIANTS = {
    "real_runtime_batch_membership": {
        "one_real_backend_invocation_has_multiple_members",
        "runtime_observation_owns_final_membership",
        "candidate_and_actual_membership_are_distinct",
        "sequential_loop_and_concurrent_admission_do_not_pass",
    },
    "continuous_late_arrival": {
        "late_arrival_joins_only_at_iteration_boundary",
        "existing_prefixes_and_event_order_remain_unchanged",
        "one_true_backend_batch_executes_per_iteration",
    },
    "causal_pipeline_overlap": {
        "compute_intersection_is_positive_after_clock_uncertainty",
        "every_stage_predecessor_completes_before_successor_starts",
        "final_loopback_starts_only_after_target_commit",
        "queue_transport_and_ui_overlap_alone_do_not_pass",
    },
    "bounded_queue_qos_aging": {
        "member_byte_and_active_batch_limits_are_finite",
        "overflow_rejects_before_runtime_dispatch",
        "aged_eligible_batch_work_cannot_starve",
    },
    "slow_consumer_isolation": {
        "bounded_gateway_buffer_fills_without_unbounded_growth",
        "batch_peers_continue_and_receive_one_result",
        "request_owned_resources_return_to_baseline",
    },
    "cancellation_boundary_isolation": {
        "cancellation_checked_before_collection_and_backend_launch",
        "post_launch_cancelled_result_is_discarded",
        "only_cancelled_member_kv_rolls_back",
        "remaining_members_receive_exactly_one_result",
    },
    "cross_member_kv_isolation": {
        "member_cannot_address_retain_commit_or_rollback_peer_kv",
        "padding_and_shape_metadata_never_become_generated_positions",
        "zero_kv_or_result_contamination",
    },
    "target_verifier_transaction": {
        "one_target_backend_call_evaluates_all_bounded_positions_causally",
        "every_position_decision_matches_sequential_target_execution",
        "commit_is_atomic_monotonic_and_idempotent_for_identical_digest",
        "final_committed_kv_position_matches_sequential_target",
    },
    "target_verifier_rollback_to_prefix": {
        "transaction_rolls_back_to_exact_original_prefix",
        "no_tentative_kv_or_uncommitted_result_escapes",
        "rollback_is_request_local",
    },
    "same_route_confidence_benchmark": {
        "baseline_and_candidate_share_every_same_route_binding",
        "throughput_gain_and_paired_lower_confidence_bound_meet_ten_percent",
        "interactive_p95_ttft_and_tpot_regress_no_more_than_ten_percent",
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


def test_a10_acceptance_manifest_is_closed_bounded_and_complete() -> None:
    manifest = _manifest()
    assert set(manifest) == {
        "protocol",
        "claim_boundary",
        "runtime_batch_requirements",
        "target_verifier_requirements",
        "benchmark_requirements",
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
        assert latency is None or type(latency) is int and 1 <= latency <= 60_000
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


def test_a10_runtime_truth_and_required_concerns_are_frozen() -> None:
    manifest = _manifest()
    assert manifest["runtime_batch_requirements"] == RUNTIME_BATCH_REQUIREMENTS
    scenarios = _scenarios_by_id()
    for scenario_id, required in REQUIRED_CONCERN_INVARIANTS.items():
        assert required <= set(scenarios[scenario_id]["required_invariants"])

    assert scenarios["real_runtime_batch_membership"]["gate_kind"] == (
        "physical_positive"
    )
    assert scenarios["causal_pipeline_overlap"]["acceptance_scope"] == "path"
    assert scenarios["overlap_trace_rejection"]["gate_kind"] == (
        "deterministic_negative"
    )


def test_a10_target_verifier_transaction_and_rollback_are_frozen() -> None:
    manifest = _manifest()
    assert manifest["target_verifier_requirements"] == TARGET_VERIFIER_REQUIREMENTS
    scenarios = _scenarios_by_id()
    assert scenarios["target_verifier_transaction"]["gate_kind"] == "physical_positive"
    assert scenarios["target_verifier_rollback_to_prefix"]["gate_kind"] == (
        "physical_negative"
    )
    assert {
        "unsupported_sampling_fails_admission",
        "generation_policy_never_silently_switches_to_greedy",
        "no_verifier_transaction_or_backend_call_starts",
    } <= set(
        scenarios["target_verifier_sampling_rejection"]["required_invariants"]
    )


def test_a10_same_route_confidence_benchmark_is_frozen() -> None:
    manifest = _manifest()
    assert manifest["benchmark_requirements"] == BENCHMARK_REQUIREMENTS
    scenarios = _scenarios_by_id()
    assert scenarios["same_route_confidence_benchmark"]["gate_kind"] == (
        "physical_positive"
    )
    assert {
        "missing_measurement_invalidates_pair_instead_of_becoming_zero",
        "failed_cancelled_overflowed_and_slow_consumers_remain_in_denominator",
        "confidence_uses_paired_windows_not_individual_tokens",
        "posthoc_threshold_and_exclusion_changes_are_rejected",
        "invalid_pair_cannot_publish_material_gain",
    } <= set(scenarios["invalid_benchmark_pair"]["required_invariants"])


def test_a10_workspace_inventory_is_closed_and_cross_workspace_state_is_private() -> None:
    manifest = _manifest()
    assert manifest["workspace_shared_invariants"] == WORKSPACE_SHARED_INVARIANTS
    assert manifest["workspace_requirements"] == WORKSPACE_REQUIREMENTS
    assert set(manifest["workspace_requirements"]) == ALL_WORKSPACES
    scenario = _scenarios_by_id()["all_workspace_reconstruction_privacy"]
    assert set(scenario["required_workspaces"]) == ALL_WORKSPACES
    assert {
        "all_eight_workspaces_use_same_current_public_generation",
        "clean_second_session_cannot_read_private_prompt_output_or_tokens",
        "live_batch_cancellation_overlap_verifier_and_benchmark_attribution_is_truthful",
    } <= set(scenario["required_invariants"])


def test_a10_specification_remains_design_only_and_names_the_inventory() -> None:
    specification = SPECIFICATION.read_text("utf-8")
    assert "**Status:** `design_only`; dependency-ready acceptance boundary" in specification
    assert "Until then it remains `design_only`" in specification
    assert all(f"`{scenario_id}`" in specification for scenario_id in REQUIRED_SCENARIOS)
