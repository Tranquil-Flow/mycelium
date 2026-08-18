from __future__ import annotations

import json
from pathlib import Path


INVENTORY = Path(__file__).with_name("inventory.v1.json")
SPECIFICATION = (
    Path(__file__).parents[2]
    / "docs/superpowers/specs/2026-08-18-mycelium-a6-full-context-replay-recovery.md"
)
PROTOCOL = "mycelium.a6_replay_acceptance_inventory.v1"
TOP_LEVEL_FIELDS = {
    "protocol",
    "gate_state",
    "claim_boundary",
    "authority_owners",
    "request_bindings",
    "state_machine",
    "workspace_projections",
    "required_coverage",
    "scenarios",
    "qualification_claim",
    "promotion_authorized",
}
WORKSPACES = {
    "inference",
    "device_lab",
    "network",
    "nodes",
    "plans",
    "readiness",
    "incidents",
    "settings",
}
AUTHORITY_OWNERS = {
    "canonical_encoded_prompt": "request_gateway",
    "committed_generated_prefix": "request_gateway",
    "delivery_ledger_and_watermark": "request_gateway",
    "logical_terminal_result": "request_gateway",
    "request_attempt_and_immutable_path": "router",
    "request_scoped_cutover": "router",
    "observed_failure_and_scope": "a4_liveness_authority",
    "successor_compatibility_and_readiness": "qualifier",
    "future_request_deployment_selection": "deployment_registry",
}
REQUEST_BINDINGS = [
    "session_id",
    "request_id",
    "source_attempt",
    "source_path_generation",
    "checkpoint_digest",
    "prompt_digest",
    "generation_settings_digest",
    "committed_token_count",
    "committed_prefix_digest",
    "delivery_cursor",
    "recovery_attempt",
    "successor_attempt",
    "successor_path_generation",
    "successor_qualification_generation",
]
STATE_MACHINE = {
    "states": [
        "active",
        "failure_observed",
        "checkpoint_frozen",
        "successor_pending",
        "recovery_prefill",
        "cutover_pending",
        "recovered_decode",
        "cleanup",
        "terminal",
        "aborted",
    ],
    "recovery_chain": [
        "active",
        "failure_observed",
        "checkpoint_frozen",
        "successor_pending",
        "recovery_prefill",
        "cutover_pending",
        "recovered_decode",
        "cleanup",
        "terminal",
    ],
    "abortable_states": [
        "failure_observed",
        "checkpoint_frozen",
        "successor_pending",
        "recovery_prefill",
        "cutover_pending",
        "recovered_decode",
    ],
    "abort_chain": ["cleanup", "aborted"],
    "cutover_scope": "request",
    "cutover_operation": "durable_compare_and_swap",
    "generation_rule": "increment_request_attempt_and_path_generation",
    "stale_generation_action": "reject_without_publication_or_ledger_advance",
}
REQUIRED_COVERAGE = {
    "gateway_prompt_authority",
    "committed_prefix_authority",
    "request_scoped_cutover",
    "generation_fencing",
    "duplicate_output_prevention",
    "independently_qualified_successor",
    "cancellation_and_cleanup",
    "no_successor_abort",
    "ordinary_browser_path_continuation",
}
REQUIRED_SCENARIOS = {
    "gateway_checkpoint_authority",
    "committed_prefix_replay_exactness",
    "request_scoped_cutover_isolation",
    "stale_generation_fencing",
    "duplicate_delivery_prevention",
    "independently_qualified_successor",
    "unqualified_successor_rejection",
    "cancel_before_cutover",
    "cancel_after_cutover",
    "successor_failure_cleanup",
    "no_successor_explicit_abort",
    "ordinary_browser_replay_continuation",
}
SCENARIO_FIELDS = {
    "scenario_id",
    "gate_kind",
    "stimulus",
    "expected_scope",
    "coverage",
    "required_invariants",
}
GATE_KINDS = {
    "deterministic_positive",
    "deterministic_negative",
    "physical_browser_positive",
    "physical_browser_negative",
}
MANDATORY_INVARIANTS = {
    "gateway_checkpoint_authority": {
        "gateway_is_only_canonical_prompt_authority",
        "gateway_is_only_committed_prefix_authority",
        "browser_cursor_and_reconstructed_text_are_not_authority",
        "invalid_checkpoint_fails_before_successor_model_work",
        "private_prompt_and_prefix_absent_from_public_records",
    },
    "committed_prefix_replay_exactness": {
        "exact_committed_prefix_count_and_digest_replayed",
        "reconstructed_prefix_output_fully_suppressed",
        "first_publication_is_strictly_after_committed_watermark",
        "regressed_skipped_repeated_or_conflicting_watermark_rejected",
    },
    "request_scoped_cutover_isolation": {
        "durable_cutover_compare_and_swap_is_request_scoped",
        "only_bound_request_attempt_and_path_change",
        "future_request_deployment_selection_unchanged",
        "unrelated_request_authority_and_progress_unchanged",
    },
    "stale_generation_fencing": {
        "request_attempt_and_path_generation_increment",
        "old_output_terminal_receipt_cancel_and_cleanup_rejected",
        "stale_events_do_not_advance_delivery_ledger",
        "one_logical_terminal_result",
    },
    "duplicate_delivery_prevention": {
        "delivery_identity_binds_session_request_and_event_sequence",
        "ledger_append_and_watermark_advance_are_atomic",
        "duplicate_event_never_appended_twice",
        "reconnect_reconstructs_without_duplicate_history",
    },
    "independently_qualified_successor": {
        "successor_has_current_exact_compatibility_qualification",
        "successor_has_own_assignment_load_graph_path_and_cleanup_proofs",
        "a5_replica_membership_is_not_required_or_sufficient",
        "historical_qualification_and_candidate_intent_are_insufficient",
    },
    "cancel_before_cutover": {
        "current_cancel_generation_terminates_request_once",
        "source_and_successor_work_interrupted",
        "source_and_successor_resources_return_to_baseline",
    },
    "cancel_after_cutover": {
        "successor_current_generation_cancelled_once",
        "old_attempt_cancel_cannot_mutate_successor",
        "all_source_and_successor_live_resources_released",
    },
    "successor_failure_cleanup": {
        "source_and_successor_path_reservations_released_exactly_once",
        "capacity_commands_receipts_streams_replay_commands_and_kv_return_to_baseline",
        "cleanup_ownership_failure_does_not_widen_scope",
    },
    "no_successor_explicit_abort": {
        "request_explicitly_aborts_with_bounded_reason",
        "no_silent_restart_from_prompt",
        "no_replay_or_continuity_success_claim",
        "one_terminal_history",
        "all_request_owned_resources_return_to_baseline",
    },
    "ordinary_browser_replay_continuation": {
        "ordinary_gateway_browser_submission",
        "no_direct_router_or_test_only_seam",
        "nonzero_gateway_committed_prefix",
        "full_context_replay_on_current_qualified_successor",
        "request_scoped_generation_fenced_cutover",
        "no_duplicate_or_missing_logical_output_event",
        "exactly_one_terminal_result",
        "zero_terminal_live_resource_delta",
    },
}


def _inventory() -> dict:
    value = json.loads(INVENTORY.read_text("utf-8"))
    assert isinstance(value, dict)
    return value


def _scenarios_by_id() -> dict[str, dict]:
    return {
        scenario["scenario_id"]: scenario for scenario in _inventory()["scenarios"]
    }


def test_inventory_is_closed_and_permanently_design_only() -> None:
    inventory = _inventory()

    assert set(inventory) == TOP_LEVEL_FIELDS
    assert inventory["protocol"] == PROTOCOL
    assert inventory["gate_state"] == "design_only"
    assert inventory["claim_boundary"] == (
        "frozen acceptance inventory only; no replay implementation, physical evidence, "
        "qualification, or promotion claim"
    )
    assert inventory["qualification_claim"] is False
    assert inventory["promotion_authorized"] is False


def test_authority_bindings_and_state_machine_are_frozen() -> None:
    inventory = _inventory()

    assert inventory["authority_owners"] == AUTHORITY_OWNERS
    assert inventory["request_bindings"] == REQUEST_BINDINGS
    assert inventory["state_machine"] == STATE_MACHINE
    assert len(REQUEST_BINDINGS) == len(set(REQUEST_BINDINGS))
    assert STATE_MACHINE["cutover_scope"] == "request"
    assert STATE_MACHINE["abort_chain"] == ["cleanup", "aborted"]


def test_workspace_projection_matrix_is_exact_and_nonempty() -> None:
    projections = _inventory()["workspace_projections"]
    assert set(projections) == WORKSPACES
    for requirements in projections.values():
        assert isinstance(requirements, list) and requirements
        assert len(requirements) == len(set(requirements))


def test_scenario_inventory_is_closed_bounded_unique_and_complete() -> None:
    inventory = _inventory()
    scenarios = inventory["scenarios"]

    assert set(inventory["required_coverage"]) == REQUIRED_COVERAGE
    assert len(inventory["required_coverage"]) == len(REQUIRED_COVERAGE)
    assert isinstance(scenarios, list) and 1 <= len(scenarios) <= 32
    assert {scenario["scenario_id"] for scenario in scenarios} == REQUIRED_SCENARIOS
    assert len(scenarios) == len(REQUIRED_SCENARIOS)

    covered: set[str] = set()
    for scenario in scenarios:
        assert isinstance(scenario, dict)
        assert set(scenario) == SCENARIO_FIELDS
        assert scenario["gate_kind"] in GATE_KINDS
        assert scenario["expected_scope"] == "request"
        assert isinstance(scenario["stimulus"], str) and scenario["stimulus"]
        assert scenario["stimulus"] == scenario["stimulus"].lower()
        coverage = scenario["coverage"]
        assert isinstance(coverage, list) and 1 <= len(coverage) <= len(REQUIRED_COVERAGE)
        assert len(coverage) == len(set(coverage))
        assert set(coverage) <= REQUIRED_COVERAGE
        covered.update(coverage)
        invariants = scenario["required_invariants"]
        assert isinstance(invariants, list) and 1 <= len(invariants) <= 16
        assert len(invariants) == len(set(invariants))
        assert all(
            isinstance(invariant, str)
            and invariant
            and invariant == invariant.lower()
            and len(invariant) <= 112
            for invariant in invariants
        )

    assert covered == REQUIRED_COVERAGE


def test_each_authority_and_safety_scenario_keeps_its_required_invariants() -> None:
    scenarios = _scenarios_by_id()

    assert set(MANDATORY_INVARIANTS) <= set(scenarios)
    for scenario_id, invariants in MANDATORY_INVARIANTS.items():
        assert invariants <= set(scenarios[scenario_id]["required_invariants"])


def test_physical_replay_gates_require_the_ordinary_browser_gateway_path() -> None:
    scenarios = _scenarios_by_id()
    physical = [
        scenario
        for scenario in scenarios.values()
        if scenario["gate_kind"].startswith("physical_browser_")
    ]

    assert {scenario["scenario_id"] for scenario in physical} == {
        "independently_qualified_successor",
        "unqualified_successor_rejection",
        "no_successor_explicit_abort",
        "ordinary_browser_replay_continuation",
    }
    for scenario in physical:
        invariants = set(scenario["required_invariants"])
        assert "ordinary_gateway_browser_submission" in invariants
        assert "no_direct_router_or_test_only_seam" in invariants


def test_no_successor_is_abort_not_continuity_or_silent_retry() -> None:
    scenario = _scenarios_by_id()["no_successor_explicit_abort"]

    assert scenario["gate_kind"] == "physical_browser_negative"
    assert set(scenario["coverage"]) >= {
        "no_successor_abort",
        "cancellation_and_cleanup",
    }
    assert {
        "request_explicitly_aborts_with_bounded_reason",
        "no_silent_restart_from_prompt",
        "no_replay_or_continuity_success_claim",
        "one_terminal_history",
        "all_request_owned_resources_return_to_baseline",
        "incumbent_and_unrelated_requests_unchanged",
    } <= set(scenario["required_invariants"])


def test_specification_and_inventory_share_the_same_claim_boundary() -> None:
    text = " ".join(SPECIFICATION.read_text("utf-8").split())

    for phrase in (
        "sole authority for the canonical encoded prompt",
        "Cutover is request-scoped",
        "request-attempt and path generation advance",
        "Duplicate or conflicting delivery",
        "current, exact compatibility and physical qualification",
        "Cancellation before or after cutover",
        "ordinary browser and gateway path",
        "explicit bounded abort",
        "`gate_state=design_only`",
        "`qualification_claim=false`",
        "`promotion_authorized=false`",
    ):
        assert phrase in text
