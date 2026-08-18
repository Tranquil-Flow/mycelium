from __future__ import annotations

import json
from pathlib import Path


MANIFEST = Path(__file__).with_name("scenarios.v1.json")
SPECIFICATION = (
    Path(__file__).parents[2]
    / "docs/superpowers/specs/2026-08-17-mycelium-a4-product-concurrency-liveness.md"
)
PROTOCOL = "mycelium.a4_acceptance_scenarios.v1"
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
    "overlapping_requests",
    "route_global_lock_absence",
    "lock_order_deadlock_detection",
    "cancel_isolation",
    "participating_active_disconnect",
    "participating_idle_staleness",
    "nonparticipating_peer_exit",
    "one_missed_receipt",
    "stale_incarnation_receipt",
    "late_command_result",
    "queue_saturation",
    "worker_exit",
    "bounded_shutdown",
    "fatal_allowlist_rejection",
    "active_request_reconnect",
    "state_subset_ownership_boundaries",
    "second_session_privacy",
}
SCENARIO_FIELDS = {
    "scenario_id",
    "gate_kind",
    "fault_kind",
    "expected_scope",
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
SCOPES = {"session", "request", "edge", "placement", "peer", "deployment"}
STATE_OWNERSHIP = {
    "deployment_readiness": "deployment_qualifier",
    "future_request_model_selection": "deployment_registry",
    "browser_session_replay_cursor_and_private_content": "request_gateway",
    "request_path_dispatch_and_event_sequence": "router",
    "complete_path_reservations_and_terminal_cleanup": "admission_ledger",
    "stage_command_deadline_and_cancellation_generation": "command_controller",
    "stage_placement_capacity_and_kv_state": "placement_runtime",
    "physical_send_connection_and_receipt_observations": "transport",
    "peer_lease_and_incarnation_generation": "membership",
    "subject_freshness_and_scoped_incidents": "liveness_detector",
}
WORKSPACE_SHARED_INVARIANTS = [
    "same_backend_generation",
    "capability_language_without_milestone_labels",
    "privacy_reduced_owner_projection_only",
    "stale_degraded_unavailable_and_unknown_are_explicit",
    "missing_measurements_never_default_to_zero_or_healthy",
]
WORKSPACE_REQUIREMENTS = {
    "inference": [
        "request_phase_and_independent_progress",
        "bounded_retry_interruption_and_terminal_reason",
        "active_reconnect_replay_and_cancellation",
    ],
    "device_lab": [
        "interruptible_command_receipt_keepalive_and_generation_fencing_capabilities",
        "maximum_concurrent_commands",
        "synthetic_browser_workers_remain_model_stage_ineligible",
    ],
    "network": [
        "directed_edge_freshness_observation_source_and_connection_reuse",
        "suspect_quarantine_and_affected_current_paths",
    ],
    "nodes": [
        "incarnation_detector_command_queue_reservation_and_recovery_state",
        "private_identity_is_excluded",
    ],
    "plans": [
        "immutable_current_paths_and_retained_legal_paths",
        "narrow_failure_scope_and_successor_recovery_deferral",
    ],
    "readiness": [
        "qualification_dispatcher_interruption_cleanup_liveness_and_fatal_checks",
        "observed_budgets",
    ],
    "incidents": [
        "source_scope_affected_owners_action_latency_cleanup_and_outcome",
        "active_failure_and_idle_staleness_are_separate",
    ],
    "settings": [
        "bounded_worker_queue_and_detector_policy",
        "future_only_edits_do_not_mutate_active_paths",
    ],
}
CROSS_WORKSPACE_SCENARIOS = {
    "active_request_reconnect",
    "state_subset_ownership_boundaries",
    "second_session_privacy",
}
FOCUSED_WORKSPACE_SCENARIOS = {
    "inference": {
        "overlapping_requests",
        "route_global_lock_absence",
        "lock_order_deadlock_detection",
        "cancel_isolation",
        "participating_active_disconnect",
        "late_command_result",
        "queue_saturation",
        "worker_exit",
    },
    "device_lab": {
        "overlapping_requests",
        "participating_active_disconnect",
        "participating_idle_staleness",
        "one_missed_receipt",
        "stale_incarnation_receipt",
        "late_command_result",
        "bounded_shutdown",
    },
    "network": {
        "overlapping_requests",
        "participating_active_disconnect",
        "participating_idle_staleness",
        "nonparticipating_peer_exit",
        "one_missed_receipt",
        "stale_incarnation_receipt",
    },
    "nodes": {
        "overlapping_requests",
        "route_global_lock_absence",
        "cancel_isolation",
        "participating_active_disconnect",
        "nonparticipating_peer_exit",
        "stale_incarnation_receipt",
        "late_command_result",
        "queue_saturation",
        "worker_exit",
        "bounded_shutdown",
    },
    "plans": {
        "overlapping_requests",
        "cancel_isolation",
        "participating_active_disconnect",
        "participating_idle_staleness",
        "nonparticipating_peer_exit",
        "one_missed_receipt",
        "fatal_allowlist_rejection",
    },
    "readiness": REQUIRED_SCENARIOS - CROSS_WORKSPACE_SCENARIOS,
    "incidents": {
        "route_global_lock_absence",
        "lock_order_deadlock_detection",
        "cancel_isolation",
        "participating_active_disconnect",
        "participating_idle_staleness",
        "nonparticipating_peer_exit",
        "one_missed_receipt",
        "stale_incarnation_receipt",
        "late_command_result",
        "queue_saturation",
        "worker_exit",
        "bounded_shutdown",
        "fatal_allowlist_rejection",
    },
    "settings": {
        "overlapping_requests",
        "route_global_lock_absence",
        "participating_idle_staleness",
        "one_missed_receipt",
        "queue_saturation",
        "bounded_shutdown",
    },
}
REQUIRED_GAP_INVARIANTS = {
    "route_global_lock_absence": {
        "no_generation_long_route_global_lock",
        "metadata_lock_released_before_blocking_work",
        "blocked_request_does_not_stop_other_dispatch",
        "cancel_during_blocking_work_terminates_once",
        "request_owned_cleanup_exact",
        "no_cross_request_ownership_mutation",
    },
    "lock_order_deadlock_detection": {
        "lock_cycle_rejected_before_physical_execution",
        "held_and_requested_scopes_reported_without_private_data",
        "no_physical_command_issued",
        "owned_locks_and_request_resources_released",
        "unaffected_dispatch_continues",
        "deployment_fatal_rejected",
    },
    "active_request_reconnect": {
        "request_continues_while_stream_is_detached",
        "replay_resumes_after_last_applied_sequence",
        "no_duplicate_or_missing_output",
        "one_live_subscriber_enforced",
        "invalid_future_or_expired_cursor_fails_closed",
        "reconnect_cannot_cross_session_boundary",
        "cancel_after_reconnect_terminates_once",
        "request_owned_cleanup_exact",
        "private_prompt_output_remains_session_scoped",
        "other_request_dispatch_continues",
    },
    "state_subset_ownership_boundaries": {
        "only_declared_owner_mutates_each_state_subset",
        "readers_use_detached_same_generation_snapshots",
        "public_projection_is_strict_privacy_reduced_subset",
        "missing_or_unknown_state_never_becomes_healthy",
        "cross_owner_mutation_fails_closed",
        "non_owner_cleanup_rejected",
        "unowned_resources_not_released",
        "unaffected_state_subsets_unchanged",
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


def test_a4_acceptance_manifest_is_closed_bounded_and_complete() -> None:
    manifest = _manifest()
    assert set(manifest) == {
        "protocol",
        "claim_boundary",
        "state_ownership",
        "workspace_shared_invariants",
        "workspace_requirements",
        "scenarios",
    }
    assert manifest["protocol"] == PROTOCOL
    assert manifest["claim_boundary"] == (
        "frozen acceptance inputs only; no product implementation or qualification claim"
    )
    assert manifest["state_ownership"] == STATE_OWNERSHIP
    assert manifest["workspace_shared_invariants"] == WORKSPACE_SHARED_INVARIANTS
    assert manifest["workspace_requirements"] == WORKSPACE_REQUIREMENTS
    scenarios = manifest["scenarios"]
    assert isinstance(scenarios, list)
    assert 1 <= len(scenarios) <= 64
    assert {scenario["scenario_id"] for scenario in scenarios} == REQUIRED_SCENARIOS

    covered_workspaces: set[str] = set()
    invariant_names: set[str] = set()
    for scenario in scenarios:
        assert isinstance(scenario, dict)
        assert set(scenario) == SCENARIO_FIELDS
        assert scenario["gate_kind"] in GATE_KINDS
        assert scenario["expected_scope"] in SCOPES
        assert isinstance(scenario["fault_kind"], str) and scenario["fault_kind"]
        latency = scenario["maximum_latency_ms"]
        assert latency is None or type(latency) is int and 1 <= latency <= 15_000
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
        invariant_names.update(invariants)
        workspaces = scenario["required_workspaces"]
        assert isinstance(workspaces, list) and 1 <= len(workspaces) <= 8
        assert len(workspaces) == len(set(workspaces))
        assert set(workspaces) <= ALL_WORKSPACES
        covered_workspaces.update(workspaces)

    assert covered_workspaces == ALL_WORKSPACES
    assert {
        "zero_resource_delta_after_cleanup",
        "no_generation_long_route_global_lock",
        "lock_cycle_rejected_before_physical_execution",
        "blocked_command_interrupted_before_old_timeout",
        "two_misses_only_suspect",
        "selected_deployment_not_failed",
        "deployment_fatal_rejected",
        "request_continues_while_stream_is_detached",
        "cross_owner_mutation_fails_closed",
        "private_prompt_output_isolated",
        "cooperative_request_scoped_cancellation",
        "bounded_prefill_decode_work_units",
        "correlated_command_ids",
        "attempt_and_generation_fence_late_work",
        "shared_node_process_survives_cancel",
        "interrupt_and_cleanup_complete_within_budget",
        "backend_ineligible_without_bounded_proof",
    } <= invariant_names


def test_a4_gap_scenarios_freeze_ownership_cleanup_privacy_and_fail_closed() -> None:
    scenarios = _scenarios_by_id()
    for scenario_id, required_invariants in REQUIRED_GAP_INVARIANTS.items():
        assert set(scenarios[scenario_id]["required_invariants"]) == required_invariants

    assert scenarios["route_global_lock_absence"]["expected_scope"] == "request"
    assert scenarios["lock_order_deadlock_detection"]["expected_scope"] == "request"
    assert scenarios["active_request_reconnect"]["expected_scope"] == "session"
    assert scenarios["state_subset_ownership_boundaries"]["expected_scope"] == (
        "request"
    )
    assert set(scenarios["active_request_reconnect"]["required_workspaces"]) == (
        ALL_WORKSPACES
    )
    assert set(
        scenarios["state_subset_ownership_boundaries"]["required_workspaces"]
    ) == ALL_WORKSPACES


def test_a4_workspace_inventory_maps_every_frozen_ui_requirement() -> None:
    manifest = _manifest()
    assert set(manifest["workspace_requirements"]) == ALL_WORKSPACES
    assert set(manifest["workspace_shared_invariants"]) == set(
        WORKSPACE_SHARED_INVARIANTS
    )

    for workspace, requirements in manifest["workspace_requirements"].items():
        assert workspace in ALL_WORKSPACES
        assert isinstance(requirements, list) and 1 <= len(requirements) <= 8
        assert len(requirements) == len(set(requirements))
        assert all(
            isinstance(requirement, str)
            and requirement
            and requirement == requirement.lower()
            and len(requirement) <= 96
            for requirement in requirements
        )

    scenarios = _scenarios_by_id()
    scenarios_by_workspace = {
        workspace: {
            scenario_id
            for scenario_id, scenario in scenarios.items()
            if workspace in scenario["required_workspaces"]
        }
        for workspace in ALL_WORKSPACES
    }
    assert scenarios_by_workspace == {
        workspace: focused_scenarios | CROSS_WORKSPACE_SCENARIOS
        for workspace, focused_scenarios in FOCUSED_WORKSPACE_SCENARIOS.items()
    }


def test_a4_specification_remains_design_only() -> None:
    specification = SPECIFICATION.read_text("utf-8")
    assert (
        "**Status:** `design_only`; approved dependency-ready acceptance boundary; "
        "implementation waits for A3 closure"
    ) in specification
    assert "Until then A4 remains `design_only`" in specification
    assert all(f"`{scenario_id}`" in specification for scenario_id in REQUIRED_SCENARIOS)
    assert all(
        f"| {workspace_label} |" in specification
        for workspace_label in (
            "Inference",
            "Device Lab",
            "Network",
            "Nodes",
            "Plans",
            "Readiness",
            "Incidents",
            "Settings",
        )
    )


def test_a4_latency_budgets_are_frozen_on_the_owning_scenarios() -> None:
    scenarios = _scenarios_by_id()
    assert scenarios["cancel_isolation"]["maximum_latency_ms"] == 2_000
    assert (
        scenarios["participating_active_disconnect"]["maximum_latency_ms"] == 2_000
    )
    assert scenarios["participating_idle_staleness"]["maximum_latency_ms"] == 15_000
    assert scenarios["one_missed_receipt"]["maximum_latency_ms"] == 5_000
    assert scenarios["worker_exit"]["maximum_latency_ms"] == 2_000
    assert scenarios["bounded_shutdown"]["maximum_latency_ms"] == 4_000


def test_a4_cancel_gate_is_one_request_scoped_budget() -> None:
    scenarios = _scenarios_by_id()
    cancellation = set(scenarios["cancel_isolation"]["required_invariants"])
    assert scenarios["cancel_isolation"]["expected_scope"] == "request"
    assert scenarios["cancel_isolation"]["maximum_latency_ms"] == 2_000
    assert {
        "cooperative_request_scoped_cancellation",
        "bounded_prefill_decode_work_units",
        "correlated_command_ids",
        "attempt_and_generation_fence_late_work",
        "shared_node_process_survives_cancel",
        "interrupt_and_cleanup_complete_within_budget",
        "backend_ineligible_without_bounded_proof",
    } <= cancellation
