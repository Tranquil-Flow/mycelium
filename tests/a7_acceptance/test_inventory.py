from __future__ import annotations

import json
from pathlib import Path


INVENTORY = Path(__file__).with_name("inventory.v1.json")
SPECIFICATION = (
    Path(__file__).parents[2]
    / "docs/superpowers/specs/2026-08-18-mycelium-a7-fenced-kv-successor-recovery.md"
)
PROTOCOL = "mycelium.a7_kv_successor_acceptance_inventory.v1"
TOP_LEVEL_FIELDS = {
    "protocol",
    "gate_state",
    "claim_boundary",
    "authority_owners",
    "watermark_contract",
    "compatibility_fields",
    "fencing_contract",
    "outcomes",
    "browser_evidence",
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
    "gateway_committed_output_watermark": "request_gateway",
    "stage_local_kv_checkpoint": "source_runtime",
    "stored_checkpoint_acknowledgement": "standby_runtime",
    "route_wide_acknowledged_watermark": "checkpoint_coordinator",
    "request_attempt_path_and_fencing_generation": "checkpoint_coordinator_router",
    "observed_failure_and_scope": "a4_liveness_authority",
    "successor_compatibility_and_readiness": "qualifier",
    "full_context_replay_fallback": "a6_recovery_authority",
    "future_request_deployment_selection": "deployment_registry",
}
WATERMARK_CONTRACT = {
    "route_advance_requires": [
        "all_replaced_stages_acknowledged",
        "same_request_attempt",
        "same_committed_token_count",
        "same_committed_prefix_digest",
        "contiguous_checkpoint_predecessor_chain",
        "current_standby_membership_incarnations",
        "stored_checkpoint_digests_verified",
    ],
    "gateway_equality_requires": [
        "route_acknowledged_token_count_equals_gateway_committed_token_count",
        "route_acknowledged_prefix_digest_equals_gateway_committed_prefix_digest",
    ],
    "partial_acknowledgement_action": "report_lag_without_cutover_authority",
    "ahead_of_gateway_action": "reject_kv_successor",
    "behind_gateway_action": "require_a6_fallback_or_abort",
    "exact_repeat_action": "idempotent_without_authority_advance",
    "skipped_regressed_or_conflicting_action": "reject_kv_successor",
}
COMPATIBILITY_FIELDS = {
    "model": [
        "model_id",
        "immutable_revision",
        "representation_manifest_digest",
        "architecture",
        "tokenizer_semantics",
        "checkpoint_schema_version",
    ],
    "layer_assignment": [
        "half_open_layer_range",
        "component_roles",
        "placement_assignment_id",
        "complete_model_operation_path",
    ],
    "runtime": [
        "backend",
        "runtime_family",
        "runtime_build",
        "decode_mode",
        "device_layout_encoding",
        "byte_order",
    ],
    "cache": [
        "cache_implementation",
        "attention_layout",
        "query_head_count",
        "kv_head_count",
        "head_dimension",
        "batch_shape",
        "dtype",
        "quantization_effect",
        "rope_configuration_and_scaling",
        "position_semantics",
        "sequence_semantics",
        "context_policy",
    ],
    "authority_and_freshness": [
        "session_id",
        "request_id",
        "source_attempt",
        "source_path_generation",
        "source_membership_incarnation",
        "successor_membership_incarnation",
        "checkpoint_sequence_and_digest",
        "source_load_and_qualification_generation",
        "successor_load_and_qualification_generation",
        "directed_path_evidence_generation",
        "resource_reservation_generation",
    ],
}
FENCING_CONTRACT = {
    "monotonic_fields": [
        "request_attempt",
        "path_generation",
        "fencing_generation",
    ],
    "installers": [
        "request_gateway",
        "router",
        "surviving_stage_runtimes",
        "transport_ingress",
        "standby_runtime",
    ],
    "advance_timing": "durable_before_successor_decode_or_publication",
    "old_generation_action": "reject_without_ledger_or_authority_mutation",
    "failed_install_action": "withhold_cutover_and_require_a6_fallback_or_abort",
    "maximum_publishers": 1,
}
OUTCOMES = {
    "fenced_kv_successor": [
        "exact_route_and_gateway_watermark_equality",
        "current_exact_compatibility_and_qualification",
        "fence_installed_by_all_participants",
        "no_recovery_prefill",
        "no_prompt_or_committed_prefix_transfer",
        "next_contiguous_position_only",
    ],
    "full_context_replay": [
        "ordinary_a6_product_interface",
        "kv_outcome_rejected_or_unavailable",
        "replay_label_visible",
        "never_relabelled_as_kv_continuation",
    ],
    "aborted": [
        "neither_a7_nor_a6_has_current_qualified_authority",
        "explicit_bounded_terminal_reason",
        "no_silent_prompt_restart",
        "no_recovery_success_claim",
    ],
}
BROWSER_EVIDENCE = {
    "required_public_fields": [
        "request_id",
        "recovery_mode",
        "recovery_phase",
        "gateway_committed_token_count",
        "route_acknowledged_token_count",
        "watermark_lag",
        "checkpoint_freshness",
        "compatibility_state",
        "successor_qualification_state",
        "current_attempt",
        "fencing_generation",
        "fallback_or_abort_reason",
        "cancellation_state",
        "cleanup_state",
        "terminal_outcome",
    ],
    "forbidden_public_fields": [
        "raw_kv",
        "encoded_prompt",
        "token_ids",
        "decoded_output",
        "activations",
        "encryption_keys",
        "network_addresses",
        "private_paths",
    ],
    "required_behaviors": [
        "no_replay_and_replay_are_distinct",
        "unknown_lag_compatibility_or_cleanup_never_defaults_ready",
        "refresh_reconnect_and_terminal_history_reconstruct_same_generation",
        "tab_private_prompt_and_output_remain_session_scoped",
    ],
}
REQUIRED_COVERAGE = {
    "route_wide_acknowledged_watermark",
    "gateway_watermark_exact_equality",
    "model_layer_runtime_cache_compatibility",
    "monotonic_fencing",
    "old_generation_rejection",
    "no_replay_continuation",
    "a6_fallback",
    "honest_abort",
    "cancellation",
    "exact_cleanup",
    "browser_visible_recovery_evidence",
}
REQUIRED_SCENARIOS = {
    "route_wide_ack_requires_every_stage",
    "gateway_watermark_exact_equality",
    "watermark_monotonicity_and_conflict",
    "exact_compatibility_identity",
    "monotonic_fence_installation",
    "old_generation_rejection",
    "ordinary_browser_no_replay_continuation",
    "ordinary_browser_a6_fallback",
    "ordinary_browser_honest_abort",
    "cancel_before_kv_cutover",
    "cancel_after_kv_cutover",
    "terminal_cleanup_all_outcomes",
    "browser_visible_recovery_evidence",
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
    "physical_browser_fallback",
    "physical_browser_negative",
    "browser_contract",
}
MANDATORY_INVARIANTS = {
    "route_wide_ack_requires_every_stage": {
        "every_replaced_stage_acknowledges_same_attempt_count_and_digest",
        "checkpoint_predecessor_chain_is_contiguous",
        "partial_acknowledgement_reports_lag_only",
        "route_watermark_does_not_advance_without_complete_acknowledgement_set",
        "no_kv_cutover_authority",
    },
    "gateway_watermark_exact_equality": {
        "gateway_committed_state_is_frozen_before_decision",
        "route_and_gateway_token_counts_equal",
        "route_and_gateway_prefix_digests_equal",
        "ahead_checkpoint_rejected",
        "behind_checkpoint_requires_a6_fallback_or_abort",
        "digest_mismatch_rejected_before_successor_work",
    },
    "watermark_monotonicity_and_conflict": {
        "exact_sequence_and_digest_repeat_is_idempotent",
        "conflicting_repeat_rejected",
        "skipped_predecessor_rejected",
        "regressed_position_rejected",
        "old_attempt_generation_or_incarnation_rejected",
        "acknowledged_watermark_never_regresses",
    },
    "exact_compatibility_identity": {
        "model_revision_representation_and_tokenizer_match_exactly",
        "half_open_layer_range_and_component_roles_match_exactly",
        "backend_runtime_family_and_build_match_exactly",
        "cache_schema_layout_dtype_shape_rope_and_position_semantics_match_exactly",
        "unknown_or_converted_compatibility_is_incompatible",
        "current_qualifier_result_required_for_complete_replacement_path",
    },
    "monotonic_fence_installation": {
        "request_attempt_path_and_fencing_generations_advance_monotonically",
        "generation_advance_is_durable_before_successor_decode",
        "gateway_router_runtimes_transport_and_standby_install_fence",
        "failed_fence_withholds_cutover",
        "at_most_one_generation_can_publish",
    },
    "old_generation_rejection": {
        "old_checkpoint_ack_frame_output_cancel_cleanup_and_terminal_rejected",
        "old_process_return_cannot_reclaim_publication_authority",
        "gateway_delivery_ledger_does_not_advance",
        "current_successor_authority_unchanged",
        "one_publisher_and_one_terminal_result",
    },
    "ordinary_browser_no_replay_continuation": {
        "route_acknowledged_count_and_digest_equal_gateway_committed_state",
        "current_exact_qualified_successor",
        "no_recovery_prefill",
        "no_prompt_or_committed_prefix_transfer",
        "first_successor_publication_is_next_contiguous_position",
        "old_generation_publication_rejected",
        "no_duplicate_or_missing_output",
        "one_publisher_and_terminal_result",
    },
    "ordinary_browser_a6_fallback": {
        "kv_successor_work_and_publication_withheld",
        "ordinary_a6_product_interface_invoked",
        "kv_outcome_rejected_or_unavailable",
        "browser_evidence_labels_full_context_replay",
        "fallback_never_relabelled_as_kv_continuation",
        "one_terminal_result",
    },
    "ordinary_browser_honest_abort": {
        "explicit_bounded_abort_reason",
        "no_silent_prompt_restart",
        "no_kv_recovery_or_replay_success_claim",
        "one_terminal_history",
        "all_request_owned_resources_return_to_baseline",
    },
    "cancel_before_kv_cutover": {
        "current_cancel_generation_terminates_request_once",
        "successor_decode_and_publication_withheld",
        "source_standby_and_transfer_work_interrupted",
        "checkpoint_buffers_shadow_kv_and_reservations_released",
    },
    "cancel_after_kv_cutover": {
        "successor_current_generation_cancelled_once",
        "old_generation_cancel_cannot_mutate_successor",
        "one_terminal_cancel_result",
        "source_and_successor_kv_and_live_resources_released",
    },
    "terminal_cleanup_all_outcomes": {
        "checkpoint_buffers_shadow_source_and_successor_kv_released_exactly_once",
        "path_capacity_and_memory_reservations_released_exactly_once",
        "commands_acknowledgements_receipts_and_streams_released_exactly_once",
        "source_and_standby_live_counters_return_to_baseline",
        "cleanup_ownership_failure_does_not_widen_scope",
    },
    "browser_visible_recovery_evidence": {
        "real_committed_acknowledged_counts_and_lag_visible",
        "recovery_mode_phase_attempt_and_fence_generation_visible",
        "compatibility_qualification_fallback_cancel_cleanup_and_terminal_state_visible",
        "no_replay_replay_and_abort_are_distinct",
        "unknown_values_never_default_ready_or_recovered",
        "raw_kv_prompt_tokens_output_activations_keys_addresses_and_paths_absent",
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
        "frozen acceptance inventory only; no KV transport, runtime recovery, Router "
        "wiring, UI implementation, physical evidence, qualification, or promotion claim"
    )
    assert inventory["qualification_claim"] is False
    assert inventory["promotion_authorized"] is False


def test_authority_watermark_and_exact_gateway_equality_are_frozen() -> None:
    inventory = _inventory()

    assert inventory["authority_owners"] == AUTHORITY_OWNERS
    assert inventory["watermark_contract"] == WATERMARK_CONTRACT
    assert WATERMARK_CONTRACT["gateway_equality_requires"] == [
        "route_acknowledged_token_count_equals_gateway_committed_token_count",
        "route_acknowledged_prefix_digest_equals_gateway_committed_prefix_digest",
    ]
    assert WATERMARK_CONTRACT["partial_acknowledgement_action"] == (
        "report_lag_without_cutover_authority"
    )


def test_compatibility_fencing_and_disjoint_outcomes_are_frozen() -> None:
    inventory = _inventory()

    assert inventory["compatibility_fields"] == COMPATIBILITY_FIELDS
    assert inventory["fencing_contract"] == FENCING_CONTRACT
    assert inventory["outcomes"] == OUTCOMES
    assert set(OUTCOMES) == {
        "fenced_kv_successor",
        "full_context_replay",
        "aborted",
    }
    assert FENCING_CONTRACT["maximum_publishers"] == 1
    assert len(FENCING_CONTRACT["monotonic_fields"]) == len(
        set(FENCING_CONTRACT["monotonic_fields"])
    )


def test_browser_evidence_is_closed_privacy_reduced_and_truthful() -> None:
    evidence = _inventory()["browser_evidence"]

    assert evidence == BROWSER_EVIDENCE
    assert not set(evidence["required_public_fields"]) & set(
        evidence["forbidden_public_fields"]
    )
    assert {
        "recovery_mode",
        "gateway_committed_token_count",
        "route_acknowledged_token_count",
        "watermark_lag",
        "fencing_generation",
        "fallback_or_abort_reason",
        "cleanup_state",
        "terminal_outcome",
    } <= set(evidence["required_public_fields"])


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
            and len(invariant) <= 128
            for invariant in invariants
        )

    assert covered == REQUIRED_COVERAGE


def test_each_safety_scenario_keeps_its_required_invariants() -> None:
    scenarios = _scenarios_by_id()

    assert set(MANDATORY_INVARIANTS) == set(scenarios)
    for scenario_id, invariants in MANDATORY_INVARIANTS.items():
        assert invariants <= set(scenarios[scenario_id]["required_invariants"])


def test_physical_gates_require_the_ordinary_browser_gateway_path() -> None:
    physical = [
        scenario
        for scenario in _scenarios_by_id().values()
        if scenario["gate_kind"].startswith("physical_browser_")
    ]

    assert {scenario["scenario_id"] for scenario in physical} == {
        "ordinary_browser_no_replay_continuation",
        "ordinary_browser_a6_fallback",
        "ordinary_browser_honest_abort",
    }
    for scenario in physical:
        invariants = set(scenario["required_invariants"])
        assert "ordinary_gateway_browser_submission" in invariants
        assert "no_direct_router_runtime_or_test_only_seam" in invariants


def test_fallback_abort_and_kv_continuation_remain_honestly_distinct() -> None:
    scenarios = _scenarios_by_id()
    no_replay = set(
        scenarios["ordinary_browser_no_replay_continuation"]["required_invariants"]
    )
    fallback = set(scenarios["ordinary_browser_a6_fallback"]["required_invariants"])
    abort = set(scenarios["ordinary_browser_honest_abort"]["required_invariants"])

    assert "no_recovery_prefill" in no_replay
    assert "no_prompt_or_committed_prefix_transfer" in no_replay
    assert "ordinary_a6_product_interface_invoked" in fallback
    assert "fallback_never_relabelled_as_kv_continuation" in fallback
    assert "explicit_bounded_abort_reason" in abort
    assert "no_kv_recovery_or_replay_success_claim" in abort


def test_specification_and_inventory_share_the_same_claim_boundary() -> None:
    text = " ".join(SPECIFICATION.read_text("utf-8").split())

    for phrase in (
        "route-wide acknowledged KV watermark",
        "exact count and digest equality",
        "model and immutable revision",
        "fencing generation advance monotonically",
        "Old-generation checkpoint",
        "performs no recovery prefill",
        "qualified A6 `full_context_replay` interface",
        "request aborts explicitly",
        "Cancellation before or after cutover",
        "Browser-visible evidence is privacy-reduced",
        "`gate_state=design_only`",
        "`qualification_claim=false`",
        "`promotion_authorized=false`",
    ):
        assert phrase in text
