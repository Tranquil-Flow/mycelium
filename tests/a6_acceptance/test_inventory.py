# SPDX-License-Identifier: AGPL-3.0-only
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
    "authority_consumption",
    "canonical_identity_bindings",
    "unresolved_bindings",
    "delivery_authority",
    "cleanup_and_terminal_ordering",
    "deadline_semantics",
    "liveness_consumption",
    "privacy_and_persistence",
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
    "committed_delivery_ledger_and_watermark": "request_gateway",
    "publisher_generation": "request_gateway",
    "logical_terminal_result": "request_gateway",
    "canonical_path_manifest_identity": "m16_admission_ledger",
    "complete_path_reservation_and_cleanup_record": "m16_admission_ledger",
    "request_attempt": "router",
    "request_scoped_cutover": "router",
    "command_identity": "a4_command_controller",
    "absolute_cancellation_deadline": "a4_command_controller",
    "cancellation_generation": "a4_command_controller",
    "terminal_revision": "a4_command_controller",
    "cleanup_receipt_revision": "a4_command_controller",
    "observed_failure_and_scope": "a4_liveness_authority",
    "deployment_fatal_allowlist": "a4_liveness_authority",
    "successor_compatibility_and_readiness": "qualifier",
    "future_request_deployment_selection": "deployment_registry",
}
FORBIDDEN_SELF_MINTED_AUTHORITY = [
    "request_attempt",
    "path_attempt",
    "path_generation",
    "canonical_path_manifest_identity",
    "command_id",
    "cancellation_generation",
    "publisher_generation",
    "terminal_revision",
    "cleanup_authority",
]
CANONICAL_IDENTITY_BINDINGS = [
    "deployment_id",
    "deployment_epoch",
    "qualification_digest",
    "request_id",
    "request_attempt",
    "path_id",
    "path_digest",
    "path_attempt",
    "topology_generation",
    "command_id",
    "cancellation_generation",
    "publisher_generation",
    "recovery_transition_id",
    "recovery_revision",
]
REQUEST_BINDINGS = [
    "session_id",
    "request_id",
    "deployment_id",
    "deployment_epoch",
    "qualification_digest",
    "topology_generation",
    "source_attempt",
    "source_path_id",
    "source_path_digest",
    "source_path_attempt",
    "checkpoint_digest",
    "prompt_digest",
    "generation_settings_digest",
    "committed_token_count",
    "committed_prefix_digest",
    "delivery_cursor",
    "publisher_generation",
    "command_id",
    "cancellation_generation",
    "recovery_attempt",
    "recovery_transition_id",
    "recovery_revision",
    "successor_attempt",
    "successor_path_id",
    "successor_path_digest",
    "successor_path_attempt",
    "successor_qualification_generation",
    "a4_atomic_commit",
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
    "cutover_operation": "owner_issued_expected_revision_compare_and_swap",
    "authority_rule": "a6_proposes_owner_issues",
    "generation_rule": "consume_owner_issued_request_attempt_and_canonical_path_attempt",
    "fence_before_successor_publication": True,
    "failed_cutover_rule": "single_current_authority_without_partial_ledger_advance",
    "stale_generation_action": "reject_without_publication_or_ledger_advance",
}
REQUIRED_COVERAGE = {
    "gateway_prompt_authority",
    "committed_prefix_authority",
    "owner_issued_authority_consumption",
    "canonical_identity_binding",
    "request_scoped_cutover",
    "cutover_fencing_cas",
    "generation_fencing",
    "duplicate_output_prevention",
    "committed_delivery_watermark",
    "independently_qualified_successor",
    "cancellation_and_cleanup",
    "original_cancellation_deadline",
    "exact_cleanup_and_terminal_ordering",
    "scoped_liveness_consumption",
    "restart_reconciliation",
    "privacy_bounded_checkpoint",
    "no_successor_abort",
    "ordinary_browser_path_continuation",
    "unresolved_a4_binding_fails_closed",
}
REQUIRED_SCENARIOS = {
    "gateway_checkpoint_authority",
    "stale_owner_issued_transition",
    "stale_a4_generation_rejection",
    "stale_publisher_generation_rejection",
    "canonical_path_identity_mismatch",
    "committed_prefix_replay_exactness",
    "committed_watermark_integrity",
    "output_before_fencing_rejection",
    "request_scoped_cutover_isolation",
    "repeated_or_conflicting_cutover",
    "stale_generation_fencing",
    "duplicate_delivery_prevention",
    "reconnect_across_publisher_reset",
    "independently_qualified_successor",
    "unqualified_successor_rejection",
    "cancel_during_successor_selection",
    "cancel_during_replay_prefill",
    "cancel_after_cutover",
    "cleanup_before_terminal_ordering",
    "exact_cleanup_with_unrelated_request_active",
    "late_source_and_successor_result_isolation",
    "successor_failure_cleanup",
    "scoped_liveness_and_fatal_allowlist",
    "restart_reconciliation_every_nonterminal_phase",
    "no_successor_explicit_abort",
    "ordinary_browser_replay_continuation",
    "unresolved_a4_commit_binding",
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
    "stale_owner_issued_transition": {
        "recovery_transition_is_proposed_not_minted",
        "stale_owner_issued_transition_rejected",
        "expected_revision_mismatch_rejected",
        "no_publication_watermark_or_reservation_change",
    },
    "stale_a4_generation_rejection": {
        "stale_request_attempt_rejected",
        "stale_topology_or_deployment_generation_rejected",
        "stale_cancellation_generation_rejected",
        "rejection_precedes_successor_model_work",
    },
    "stale_publisher_generation_rejection": {
        "publisher_generation_is_gateway_owned",
        "stale_publisher_generation_event_rejected",
        "stale_cursor_persistence_and_reducer_rejected",
        "ledger_and_watermark_unchanged",
    },
    "canonical_path_identity_mismatch": {
        "canonical_m16_path_identity_is_consumed_not_reconstructed",
        "path_identity_never_derived_from_placement_data",
        "hardcoded_path_attempt_rejected",
        "path_id_digest_or_attempt_mismatch_rejected",
    },
    "committed_prefix_replay_exactness": {
        "exact_committed_prefix_count_and_digest_replayed",
        "reconstructed_prefix_output_fully_suppressed",
        "first_publication_is_strictly_after_committed_watermark",
        "regressed_skipped_repeated_or_conflicting_watermark_rejected",
    },
    "committed_watermark_integrity": {
        "gateway_ledger_is_the_only_watermark_authority",
        "duplicate_position_rejected",
        "skipped_position_rejected",
        "regressed_position_rejected",
        "late_or_conflicting_position_rejected",
    },
    "output_before_fencing_rejection": {
        "old_attempt_is_fenced_before_successor_publication",
        "successor_output_before_fencing_rejected",
        "stale_source_cannot_publish_tokens_or_terminal_state",
        "ledger_and_watermark_unchanged",
    },
    "request_scoped_cutover_isolation": {
        "durable_cutover_compare_and_swap_is_request_scoped",
        "only_bound_request_attempt_and_canonical_path_change",
        "future_request_deployment_selection_unchanged",
        "unrelated_request_authority_and_progress_unchanged",
    },
    "repeated_or_conflicting_cutover": {
        "cutover_requires_owner_issued_authority_and_expected_revision",
        "repeated_identical_cutover_is_idempotent",
        "conflicting_cutover_rejected",
        "failed_cutover_preserves_one_current_authority",
        "failed_cutover_does_not_partially_advance_delivery_ledger",
    },
    "stale_generation_fencing": {
        "owner_issued_request_attempt_and_path_attempt_advance",
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
    "reconnect_across_publisher_reset": {
        "publisher_reset_advances_gateway_owned_generation_by_cas",
        "reconnect_replay_does_not_advance_committed_watermark",
        "old_and_new_publisher_generations_are_never_unioned",
        "one_live_subscriber_and_no_duplicate_history",
    },
    "independently_qualified_successor": {
        "successor_has_current_exact_compatibility_qualification",
        "successor_has_own_assignment_load_graph_path_and_cleanup_proofs",
        "a5_replica_membership_is_not_required_or_sufficient",
        "historical_qualification_and_candidate_intent_are_insufficient",
    },
    "cancel_during_successor_selection": {
        "cancellation_preempts_pending_recovery",
        "original_absolute_cancellation_deadline_governs",
        "no_new_two_second_cancellation_budget",
        "cancellation_not_reported_as_recovery_failure",
        "source_and_candidate_resources_return_to_baseline",
    },
    "cancel_during_replay_prefill": {
        "cancellation_preempts_pending_recovery",
        "original_absolute_cancellation_deadline_governs",
        "no_new_two_second_cancellation_budget",
        "replay_prefill_interrupted_at_bounded_step",
        "cutover_not_published",
    },
    "cancel_after_cutover": {
        "successor_current_generation_cancelled_once",
        "old_attempt_cancel_cannot_mutate_successor",
        "original_absolute_cancellation_deadline_governs",
        "all_source_and_successor_live_resources_released",
    },
    "cleanup_before_terminal_ordering": {
        "cleanup_receipt_recorded_before_terminal_publication",
        "cleanup_failure_publishes_explicit_a4_authorized_terminal",
        "terminal_never_assumes_or_skips_cleanup",
        "shared_node_termination_is_not_cleanup_proof",
    },
    "exact_cleanup_with_unrelated_request_active": {
        "cleanup_is_exact_request_attempt_and_path_specific",
        "owner_issued_cleanup_receipt_and_expected_revision_required",
        "unrelated_requests_may_remain_active",
        "node_global_zero_counters_are_not_cleanup_proof",
    },
    "late_source_and_successor_result_isolation": {
        "late_fenced_source_result_discarded_as_bounded_rejection",
        "late_superseded_successor_result_discarded",
        "unrelated_waiters_are_not_failed",
        "shared_node_process_is_not_terminated",
        "current_attempt_and_ledger_unchanged",
    },
    "successor_failure_cleanup": {
        "source_and_successor_path_reservations_released_exactly_once",
        "capacity_commands_receipts_streams_replay_commands_and_kv_return_to_baseline",
        "cleanup_ownership_failure_does_not_widen_scope",
    },
    "scoped_liveness_and_fatal_allowlist": {
        "a4_liveness_owns_the_observed_failure_and_subject",
        "a6_cannot_author_the_failure_granting_its_own_authority",
        "request_edge_and_placement_failures_remain_scoped",
        "deployment_fatal_requires_a4_fatal_allowlist",
        "failed_successor_does_not_poison_incumbent_or_unrelated_requests",
    },
    "restart_reconciliation_every_nonterminal_phase": {
        "every_nonterminal_phase_reconciles_exactly_once",
        "outcome_is_resumed_aborted_or_already_terminal",
        "browser_authored_state_is_not_trusted",
        "orphaned_checkpoint_file_is_not_trusted",
        "stale_process_cannot_reacquire_publication_authority",
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
    "unresolved_a4_commit_binding": {
        "a4_atomic_commit_binding_is_unresolved",
        "integration_authorization_fails_closed",
        "no_placeholder_or_dirty_worktree_digest_accepted",
        "resolution_is_an_owner_action_after_atomic_a4_closure",
    },
}


def _inventory() -> dict:
    value = json.loads(INVENTORY.read_text("utf-8"))
    assert isinstance(value, dict)
    return value


def _scenarios_by_id() -> dict[str, dict]:
    return {scenario["scenario_id"]: scenario for scenario in _inventory()["scenarios"]}


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
    assert inventory["canonical_identity_bindings"] == CANONICAL_IDENTITY_BINDINGS
    assert inventory["state_machine"] == STATE_MACHINE
    assert len(REQUEST_BINDINGS) == len(set(REQUEST_BINDINGS))
    assert len(CANONICAL_IDENTITY_BINDINGS) == len(set(CANONICAL_IDENTITY_BINDINGS))
    assert STATE_MACHINE["cutover_scope"] == "request"
    assert STATE_MACHINE["abort_chain"] == ["cleanup", "aborted"]


def test_a6_consumes_authority_and_mints_none() -> None:
    consumption = _inventory()["authority_consumption"]

    assert consumption["a6_role"] == "propose_or_request_only"
    assert consumption["a6_may_mint"] == []
    assert (
        consumption["forbidden_self_minted_authority"]
        == FORBIDDEN_SELF_MINTED_AUTHORITY
    )
    assert "self_minted_authority" in consumption["rejection_reasons"]
    assert "unresolved_a4_atomic_commit_binding" in consumption["rejection_reasons"]
    assert set(consumption["required_grant_fields"]) == {
        "issuer",
        "canonical_identity",
        "expected_revision",
        "a4_atomic_commit",
    }
    assert set(consumption["fail_closed_effects"]) == {
        "no_publication",
        "no_watermark_advance",
        "no_reservation_release",
        "no_terminal_result",
        "no_state_advance",
    }

    minted_by_a6 = {
        owner for owner in _inventory()["authority_owners"].values() if owner == "a6"
    }
    assert minted_by_a6 == set()


def test_unresolved_a4_commit_binding_is_null_and_fails_closed() -> None:
    unresolved = _inventory()["unresolved_bindings"]

    assert unresolved["a4_atomic_commit"] is None
    assert unresolved["fail_closed_reason"] == "unresolved_a4_atomic_commit_binding"
    assert set(unresolved["blocks"]) == {
        "a6_implementation",
        "a6_integration",
        "a6_physical_execution",
        "a6_promotion",
    }
    assert unresolved["resolution_owner"] == "atomic_a4_closure"
    provisional = unresolved["provisional_a4_surfaces"]
    assert isinstance(provisional, list) and 1 <= len(provisional) <= 16
    assert len(provisional) == len(set(provisional))


def test_delivery_authority_is_gateway_owned_and_fenced() -> None:
    delivery = _inventory()["delivery_authority"]

    assert delivery["ledger_owner"] == "request_gateway"
    assert delivery["reconnect_replay_advances_watermark"] is False
    assert delivery["successor_output_start"] == "strictly_after_committed_watermark"
    assert set(delivery["fail_closed_positions"]) == {
        "duplicate",
        "skipped",
        "regressed",
        "conflicting",
        "late",
    }
    assert set(delivery["publisher_generation_fences"]) == {
        "sse_events",
        "replay_cursors",
        "event_persistence",
        "reconnect_authority",
        "ui_reducers",
    }


def test_cleanup_terminal_and_deadline_semantics_are_frozen() -> None:
    inventory = _inventory()
    cleanup = inventory["cleanup_and_terminal_ordering"]
    deadline = inventory["deadline_semantics"]

    assert cleanup["cleanup_scope"] == "exact_request_attempt_path"
    assert cleanup["cleanup_authority"] == "owner_issued_receipt_with_expected_revision"
    assert cleanup["node_global_zero_counters_are_proof"] is False
    assert cleanup["unrelated_requests_may_remain_active"] is True
    assert cleanup["terminal_after_cleanup"] is True
    assert cleanup["cleanup_failure_terminal"] == "explicit_a4_authorized"
    assert cleanup["shared_node_termination_is_cleanup"] is False
    assert set(cleanup["late_result_forbidden_effects"]) == {
        "fail_unrelated_waiter",
        "stop_response_reader",
        "release_other_owner_resources",
        "mutate_current_attempt",
        "terminate_shared_node",
    }

    assert deadline["recovery_policy"] == "bounded_owner_issued"
    assert deadline["on_cancellation_observed"] == (
        "original_a4_absolute_cancellation_deadline_governs"
    )
    assert set(deadline["governed_phases"]) == {
        "interruption",
        "cleanup",
        "backend_release",
        "terminal_publication",
    }
    assert deadline["additional_cancellation_budget_ms"] == 0
    assert deadline["budget_restart_allowed"] is False
    assert deadline["cancellation_precedence_over_recovery"] is True
    assert deadline["cancellation_may_be_reported_as_recovery_failure"] is False


def test_liveness_consumption_and_privacy_are_frozen() -> None:
    inventory = _inventory()
    liveness = inventory["liveness_consumption"]
    privacy = inventory["privacy_and_persistence"]

    assert liveness["observed_failure_owner"] == "a4_liveness_authority"
    assert liveness["a6_may_author_failure"] is False
    assert set(liveness["scoped_subjects"]) == {"request", "edge", "placement"}
    assert liveness["deployment_fatal_requires"] == "a4_fatal_allowlist"
    assert liveness["failed_successor_poisons_incumbent"] is False
    assert liveness["failed_successor_affects_unrelated_requests"] is False

    assert privacy["private_store"] == "bounded_owner_private_checkpoint"
    assert set(privacy["private_fields"]) == {
        "prompt_tokens",
        "committed_token_prefix",
    }
    assert set(privacy["public_contract_exclusions"]) == {
        "prompt_text",
        "output_text",
        "token_ids",
        "kv",
        "activations",
        "raw_addresses",
        "credentials",
        "private_paths",
    }
    assert set(privacy["restart_reconciliation_outcomes"]) == {
        "resumed",
        "aborted",
        "already_terminal",
    }
    assert privacy["restart_reconciliation_exactly_once"] is True
    assert set(privacy["untrusted_restart_inputs"]) == {
        "browser_authored_cursor",
        "reconstructed_text",
        "orphaned_checkpoint_file",
        "partially_written_record",
    }


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
        assert isinstance(coverage, list) and 1 <= len(coverage) <= len(
            REQUIRED_COVERAGE
        )
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
        "A6 is an authority consumer, not an authority issuer",
        "Cutover is request-scoped and is an owner-issued, expected-revision",
        "The owner advances request attempt and canonical path attempt; "
        "recovery consumes those values and never mints them.",
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


def test_specification_states_the_corrected_authority_and_deadline_rules() -> None:
    text = " ".join(SPECIFICATION.read_text("utf-8").split())

    for phrase in (
        "unresolved_bindings.a4_atomic_commit",
        "unresolved_a4_atomic_commit_binding",
        "the original A4 absolute cancellation deadline governs interruption, cleanup, "
        "backend release, and terminal publication",
        "add a second 2,000 ms cancellation budget",
        "Node-global or deployment-global zero counters are not cleanup proof",
        "never terminate a shared node process as proof of cleanup",
        "Reconnect replay reconstructs a session from the ledger; it never advances the "
        "committed watermark.",
        "A6 consumes that observation and may not author",
        "deployment-fatal transition requires A4's explicit fatal allowlist",
        "## 12. Unresolved interfaces requiring reconciliation with atomic A4",
    ):
        assert phrase in text
