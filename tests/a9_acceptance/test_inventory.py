from __future__ import annotations

import json
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
INVENTORY = Path(__file__).with_name("inventory.v1.json")
SPECIFICATION = (
    ROOT
    / "docs/superpowers/specs/2026-08-18-mycelium-a9-platform-neutral-peer-capability.md"
)
PROTOCOL = "mycelium.a9_acceptance_inventory.v1"
CLAIM_BOUNDARY = (
    "frozen authority, migration, qualification, and projection acceptance inputs "
    "only; no schema, implementation, migration execution, device evidence, readiness, "
    "or completion claim"
)
REQUIRED_DECISIONS = {
    "separated_capability_authority",
    "signed_capability_binding",
    "unknown_remains_ineligible",
    "native_synthetic_evidence_separation",
    "identity_preserving_v2_migration",
    "class_specific_qualification",
    "dynamic_ui_projection",
}
REQUIRED_CASES = {
    "authority_dimension_independence",
    "signed_capability_tamper_or_staleness",
    "unknown_future_platform_or_runtime",
    "synthetic_native_substitution",
    "identity_continuity_v2_migration",
    "identity_discontinuity_migration",
    "eligibility_carry_over_rejection",
    "v1_write_after_cutover",
    "role_specific_qualification_isolation",
    "dynamic_projection_withdrawal",
    "dynamic_unknown_registry_value",
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
DECISION_FIELDS = {
    "decision_id",
    "authorities",
    "required_invariants",
    "forbidden_shortcuts",
}
CASE_FIELDS = {
    "case_id",
    "gate_kind",
    "setup",
    "stimulus",
    "required_outcomes",
    "forbidden_side_effects",
}
PROJECTION_FIELDS = {
    "workspace_id",
    "required_projection",
    "dynamic_behavior",
    "forbidden_claims",
}
GATE_KINDS = {
    "deterministic_negative",
    "physical_positive",
    "physical_negative",
    "browser_positive",
    "browser_negative",
}
FORBIDDEN_PUBLIC_VALUES = re.compile(
    r"(?:https?://|iroh://|tailscale\.com|100\.\d{1,3}\.\d{1,3}\.\d{1,3}|"
    r"(?:token|secret|password|credential)\s*[:=])",
    re.IGNORECASE,
)


def _inventory() -> dict:
    value = json.loads(INVENTORY.read_text("utf-8"))
    assert isinstance(value, dict)
    return value


def _bounded_unique_names(values: object, *, maximum: int = 16) -> set[str]:
    assert isinstance(values, list) and 1 <= len(values) <= maximum
    assert len(values) == len(set(values))
    assert all(
        isinstance(value, str)
        and re.fullmatch(r"[a-z][a-z0-9_]{0,95}", value) is not None
        for value in values
    )
    return set(values)


def test_a9_inventory_is_closed_design_only_private_and_specification_bound() -> None:
    inventory = _inventory()
    assert set(inventory) == {
        "protocol",
        "gate",
        "state",
        "claim_boundary",
        "source_specification",
        "decisions",
        "acceptance_cases",
        "ui_projections",
    }
    assert inventory["protocol"] == PROTOCOL
    assert inventory["gate"] == "A9"
    assert inventory["state"] == "design_only"
    assert inventory["claim_boundary"] == CLAIM_BOUNDARY
    assert inventory["source_specification"] == str(
        SPECIFICATION.relative_to(ROOT)
    )

    specification = SPECIFICATION.read_text("utf-8")
    assert "**Status:** `design_only`;" in specification
    assert f"`{INVENTORY.relative_to(ROOT)}`" in specification
    assert f"`{PROTOCOL}`" in specification
    for decision_id in REQUIRED_DECISIONS:
        assert f"`{decision_id}`" in specification

    assert FORBIDDEN_PUBLIC_VALUES.search(INVENTORY.read_text("utf-8")) is None


def test_a9_decisions_separate_authority_and_reject_eligibility_shortcuts() -> None:
    decisions = _inventory()["decisions"]
    assert isinstance(decisions, list) and len(decisions) == len(REQUIRED_DECISIONS)
    assert {decision["decision_id"] for decision in decisions} == REQUIRED_DECISIONS

    authorities: set[str] = set()
    invariants: set[str] = set()
    shortcuts: set[str] = set()
    for decision in decisions:
        assert isinstance(decision, dict) and set(decision) == DECISION_FIELDS
        authorities.update(_bounded_unique_names(decision["authorities"]))
        invariants.update(_bounded_unique_names(decision["required_invariants"]))
        shortcuts.update(_bounded_unique_names(decision["forbidden_shortcuts"]))

    assert {
        "membership_identity",
        "peer_profile_declaration",
        "capability_evidence",
        "class_qualification",
        "computed_eligibility",
        "planner_placement",
        "artifact_grant",
        "deployment_qualification",
    } <= authorities
    assert {
        "platform_transport_runtime_capability_separate",
        "profile_signed_by_member",
        "unknown_activation_ineligible",
        "native_gate_requires_native_evidence",
        "every_role_re_evaluated_without_carry_over",
        "qualification_bound_to_exact_role",
        "all_workspaces_share_authority_generation",
    } <= invariants
    assert {
        "compound_device_class",
        "unsigned_capability",
        "unknown_as_supported",
        "synthetic_as_native",
        "eligibility_copy",
        "platform_wide_qualification",
        "hard_coded_device_inventory",
    } <= shortcuts


def test_a9_acceptance_cases_cover_migration_fail_closed_and_dynamic_truth() -> None:
    cases = _inventory()["acceptance_cases"]
    assert isinstance(cases, list) and 1 <= len(cases) <= 32
    assert {case["case_id"] for case in cases} == REQUIRED_CASES

    outcomes: set[str] = set()
    side_effects: set[str] = set()
    for case in cases:
        assert isinstance(case, dict) and set(case) == CASE_FIELDS
        assert case["gate_kind"] in GATE_KINDS
        assert isinstance(case["setup"], str) and 20 <= len(case["setup"]) <= 256
        assert isinstance(case["stimulus"], str) and 10 <= len(case["stimulus"]) <= 256
        outcomes.update(_bounded_unique_names(case["required_outcomes"]))
        side_effects.update(_bounded_unique_names(case["forbidden_side_effects"]))

    assert {
        "dependent_eligibility_withdrawn",
        "capability_report_rejected",
        "registered_unknown_visible_and_ineligible",
        "native_gate_remains_open",
        "node_id_and_verification_key_preserved",
        "generation_advances_exactly_once",
        "fresh_reenrollment_required",
        "legacy_eligibility_not_copied",
        "v1_write_rejected",
        "unqualified_role_rejected",
        "all_workspaces_share_new_generation",
        "no_hard_coded_inventory_change_required",
    } <= outcomes
    assert {
        "qualification_issued",
        "activation_admitted",
        "synthetic_promoted_to_native",
        "eligibility_carried_over",
        "operator_identity_mapping_used",
        "artifact_disclosed",
        "dual_write_created",
        "cross_role_grant",
        "browser_state_preserves_eligibility",
        "eligibility_inferred",
    } <= side_effects


def test_a9_ui_inventory_is_dynamic_generation_bound_and_all_workspace_complete() -> None:
    projections = _inventory()["ui_projections"]
    assert isinstance(projections, list) and len(projections) == len(WORKSPACES)
    assert {projection["workspace_id"] for projection in projections} == WORKSPACES

    required: set[str] = set()
    dynamic: set[str] = set()
    forbidden: set[str] = set()
    for projection in projections:
        assert isinstance(projection, dict) and set(projection) == PROJECTION_FIELDS
        required.update(_bounded_unique_names(projection["required_projection"]))
        dynamic.update(_bounded_unique_names(projection["dynamic_behavior"]))
        forbidden.update(_bounded_unique_names(projection["forbidden_claims"]))

    assert {
        "source_kind",
        "blockers",
        "transport_capabilities",
        "platform_family",
        "runtimes",
        "qualification",
        "eligibility",
        "exclusion_reason",
        "migration_state",
        "supported_registries",
    } <= required
    assert {
        "live_authority_generation",
        "registry_driven_roles",
        "registered_transport_values",
        "registered_values_without_brand_branch",
        "stale_input_withdrawn",
        "proofs_independently_fresh",
        "terminal_history_retained",
        "registry_updates_without_ui_inventory_edit",
    } <= dynamic
    assert {
        "browser_state_grants_role",
        "synthetic_as_native",
        "platform_implies_connectivity",
        "compound_legacy_class",
        "eligibility_implies_placement",
        "peer_qualification_is_route_ready",
        "private_device_detail",
        "operator_mapping_as_identity",
    } <= forbidden
