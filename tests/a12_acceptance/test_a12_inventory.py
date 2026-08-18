from __future__ import annotations

import json
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
INVENTORY = Path(__file__).with_name("inventory.v1.json")
SPECIFICATION = (
    ROOT
    / "docs/superpowers/specs/2026-08-18-mycelium-a12-generic-mobile-activation.md"
)
PROTOCOL = "mycelium.a12_mobile_acceptance_inventory.v1"
A9_ACCEPTANCE_COMMIT = "0f79a2b1a7f553499579b24c6c9362e801809dab"
CLAIM_BOUNDARY = (
    "frozen mobile authority, platform, native-path, qualification, and projection "
    "acceptance inputs only; no application, runtime, device evidence, eligibility, "
    "readiness, or completion claim"
)
RUNG_IDS = [
    "signed_membership",
    "capability_eligibility",
    "artifact_readiness",
    "operation_qualification",
]
REQUIRED_DECISIONS = {
    "four_rung_authority",
    "generic_platform_architecture",
    "two_android_family_proof",
    "apple_foreground_initial",
    "sustainable_mobile_gates",
    "a11_bound_optional_draft",
    "native_product_path_only",
    "dynamic_mobile_projection",
}
REQUIRED_CASES = {
    "rung_authority_isolation",
    "signed_member_only",
    "capability_without_artifact",
    "artifact_without_operation",
    "first_android_family_insufficient",
    "two_android_family_native_proof",
    "brand_specific_shortcut_rejection",
    "android_sustainability_withdrawal",
    "android_lifecycle_network_transitions",
    "apple_foreground_operation",
    "apple_background_drain_and_fence",
    "apple_platform_substitution_rejection",
    "sustainability_gate_matrix_fail_closed",
    "artifact_assignment_integrity",
    "native_parity_and_operation_binding",
    "optional_draft_requires_a11",
    "mobile_draft_loss_target_fallback",
    "development_tool_path_rejection",
    "dynamic_mobile_projection_reconstruction",
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
REQUIRED_GATE_FAMILIES = {
    "thermal",
    "power",
    "memory",
    "lifecycle",
    "network",
    "artifact",
    "parity",
}
EXACT_GATE_DEPENDENCIES = {"A4", "A9"}
RUNG_FIELDS = {"rung_id", "authority", "requires", "grants", "cannot_grant"}
DECISION_FIELDS = {"decision_id", "required_invariants", "forbidden_shortcuts"}
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
}


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


def _cases_by_id() -> dict[str, dict]:
    return {case["case_id"]: case for case in _inventory()["acceptance_cases"]}


def _dependency_findings(
    inventory: dict, closed_gates: set[str], requested_claims: set[str]
) -> set[str]:
    findings: set[str] = set()
    policy = inventory.get("dependency_policy")
    if not isinstance(policy, dict):
        return {"a12_dependency_policy_invalid"}
    actual = set(policy.get("exact_gate_dependencies", []))
    if actual != EXACT_GATE_DEPENDENCIES:
        findings.add("a12_gate_dependencies_not_exact")
    for dependency in actual - closed_gates:
        findings.add(f"a12_gate_dependency_incomplete:{dependency}")

    rows = policy.get("optional_claim_prerequisites")
    if not isinstance(rows, list):
        return findings | {"a12_optional_claim_policy_invalid"}
    claims = {
        row.get("claim_id"): row
        for row in rows
        if isinstance(row, dict) and isinstance(row.get("claim_id"), str)
    }
    draft = claims.get("speculative_draft_worker")
    if (
        set(claims) != {"speculative_draft_worker"}
        or not isinstance(draft, dict)
        or set(draft.get("dependencies", [])) != {"A11"}
        or draft.get("required_dependency_state") != "physically_closed"
        or draft.get("failure_mode") != "claim_rejected_gate_closure_unchanged"
    ):
        findings.add("a12_claim_dependency_policy_not_exact")
    for claim_id in requested_claims:
        claim = claims.get(claim_id)
        if claim is None:
            findings.add(f"a12_claim_unknown:{claim_id}")
            continue
        dependencies = set(claim.get("dependencies", []))
        for dependency in dependencies - closed_gates:
            findings.add(f"a12_claim_dependency_incomplete:{claim_id}:{dependency}")
    return findings


def test_a12_inventory_is_closed_design_only_and_a9_input_bound() -> None:
    inventory = _inventory()
    assert set(inventory) == {
        "protocol",
        "gate",
        "state",
        "claim_boundary",
        "source_specification",
        "reviewed_design_inputs",
        "dependency_policy",
        "eligibility_rungs",
        "platform_requirements",
        "required_gate_families",
        "decisions",
        "acceptance_cases",
        "ui_projections",
    }
    assert inventory["protocol"] == PROTOCOL
    assert inventory["gate"] == "A12"
    assert inventory["state"] == "design_only"
    assert inventory["claim_boundary"] == CLAIM_BOUNDARY
    assert inventory["source_specification"] == str(SPECIFICATION.relative_to(ROOT))
    assert inventory["reviewed_design_inputs"] == [
        {
            "gate": "A9",
            "commit": A9_ACCEPTANCE_COMMIT,
            "protocol": "mycelium.a9_acceptance_inventory.v1",
            "adopted_boundaries": [
                "separated_capability_authority",
                "signed_capability_binding",
                "unknown_remains_ineligible",
                "native_synthetic_evidence_separation",
                "class_specific_qualification",
                "dynamic_ui_projection",
            ],
        }
    ]


def test_a12_generic_closure_requires_exactly_a4_and_a9_not_a11() -> None:
    inventory = _inventory()
    policy = inventory["dependency_policy"]
    assert policy["exact_gate_dependencies"] == ["A4", "A9"]
    assert set(policy["forbidden_blanket_dependencies"]) >= {"A11"}

    assert _dependency_findings(inventory, {"A4", "A9"}, set()) == set()
    assert "a12_gate_dependency_incomplete:A4" in _dependency_findings(
        inventory, {"A9", "A11"}, set()
    )


def test_a12_optional_draft_claim_fails_closed_until_a11_closes() -> None:
    inventory = _inventory()
    claim = "speculative_draft_worker"
    assert _dependency_findings(inventory, {"A4", "A9"}, {claim}) == {
        "a12_claim_dependency_incomplete:speculative_draft_worker:A11"
    }
    assert _dependency_findings(inventory, {"A4", "A9", "A11"}, {claim}) == set()


def test_a12_dependency_mutations_reject_blanket_a11_and_missing_claim_guard() -> None:
    inventory = _inventory()
    inventory["dependency_policy"]["exact_gate_dependencies"].append("A11")
    assert "a12_gate_dependencies_not_exact" in _dependency_findings(
        inventory, {"A4", "A9", "A11"}, set()
    )

    inventory = _inventory()
    inventory["dependency_policy"]["optional_claim_prerequisites"][0][
        "dependencies"
    ] = []
    assert "a12_claim_dependency_policy_not_exact" in _dependency_findings(
        inventory, {"A4", "A9"}, {"speculative_draft_worker"}
    )


def test_a12_four_rungs_are_separate_and_fail_closed() -> None:
    rungs = _inventory()["eligibility_rungs"]
    assert isinstance(rungs, list) and len(rungs) == 4
    assert [rung["rung_id"] for rung in rungs] == RUNG_IDS
    assert len({rung["authority"] for rung in rungs}) == 4

    requires: set[str] = set()
    grants: set[str] = set()
    forbidden: set[str] = set()
    for rung in rungs:
        assert isinstance(rung, dict) and set(rung) == RUNG_FIELDS
        assert re.fullmatch(r"[a-z][a-z0-9_]{0,95}", rung["authority"])
        requires.update(_bounded_unique_names(rung["requires"]))
        grants.update(_bounded_unique_names(rung["grants"]))
        forbidden.update(_bounded_unique_names(rung["cannot_grant"]))

    assert {
        "durable_device_owned_key",
        "signed_a9_v2_profile",
        "fresh_native_observations",
        "assignment_scoped_grant",
        "digest_and_component_integrity",
        "exact_native_runtime_build_and_operation",
        "physical_positive_and_negative_evidence",
    } <= requires
    assert {
        "current_signed_membership",
        "exact_role_capability_eligibility",
        "assignment_local_artifact_ready",
        "exact_platform_runtime_build_and_role_operation_qualification",
    } == grants
    assert {
        "capability_eligibility",
        "artifact_readiness",
        "operation_qualification",
        "planner_placement",
        "deployment_qualification",
        "registry_selection",
    } <= forbidden

    cases = _cases_by_id()
    assert {
        "dependent_later_rungs_withdraw",
        "independent_prior_rungs_unchanged",
    } <= set(cases["rung_authority_isolation"]["required_outcomes"])
    assert {
        "capability_artifact_and_operation_remain_unearned",
        "no_model_content_or_inference_traffic",
    } <= set(cases["signed_member_only"]["required_outcomes"])
    assert "artifact_chunk_disclosed" in cases["capability_without_artifact"][
        "forbidden_side_effects"
    ]
    assert "operation_qualification_blocked" in cases["artifact_without_operation"][
        "required_outcomes"
    ]


def test_a12_decisions_cover_generic_mobile_native_paths_and_sustainable_gates() -> None:
    inventory = _inventory()
    decisions = inventory["decisions"]
    assert isinstance(decisions, list) and len(decisions) == len(REQUIRED_DECISIONS)
    assert {decision["decision_id"] for decision in decisions} == REQUIRED_DECISIONS

    invariants: set[str] = set()
    shortcuts: set[str] = set()
    for decision in decisions:
        assert isinstance(decision, dict) and set(decision) == DECISION_FIELDS
        invariants.update(_bounded_unique_names(decision["required_invariants"]))
        shortcuts.update(_bounded_unique_names(decision["forbidden_shortcuts"]))

    assert {
        "membership_capability_artifact_and_operation_are_separate",
        "platform_runtime_transport_operation_and_qualification_drive_behavior",
        "two_independently_manufactured_android_families_required",
        "initial_ios_ipados_inference_is_foreground_active_only",
        "thermal_power_memory_lifecycle_network_artifact_and_parity_are_independent",
        "a11_must_be_physically_closed_for_exact_target_workload",
        "termux_adb_and_ssh_are_development_or_conformance_only",
    } <= invariants
    assert {
        "pixel_specific_product_path",
        "one_phone_generic_android_claim",
        "background_inference_assumed",
        "missing_api_as_healthy",
        "a11_design_only_as_draft_authority",
        "termux_as_product_runtime",
        "adb_dependency_during_gate",
        "ssh_dependency_during_gate",
    } <= shortcuts

    assert set(inventory["required_gate_families"]) == REQUIRED_GATE_FAMILIES


def test_a12_platform_requirements_freeze_two_android_families_and_apple_foreground() -> None:
    requirements = _inventory()["platform_requirements"]
    assert requirements == {
        "android": {
            "architecture": "generic_registered_native_android",
            "minimum_device_families": 2,
            "different_manufacturers_required": True,
            "brand_or_model_specific_branching_allowed": False,
            "signed_native_application_required": True,
        },
        "apple_mobile": {
            "platform_families": ["ios", "ipados"],
            "initial_inference_eligibility": "foreground_active_only",
            "physical_device_required": True,
            "simulator_may_qualify": False,
            "ios_ipados_qualification_carryover_allowed": False,
        },
        "excluded_normal_product_paths": ["termux", "adb", "ssh"],
    }

    cases = _cases_by_id()
    assert {
        "generic_android_claim_blocked",
        "second_independent_family_required",
    } <= set(cases["first_android_family_insufficient"]["required_outcomes"])
    assert {
        "two_family_diversity_proved_privately",
        "both_family_role_gates_pass",
    } <= set(cases["two_android_family_native_proof"]["required_outcomes"])
    assert {
        "foreground_active_only_constraint_enforced",
        "exact_platform_family_and_role_recorded",
    } <= set(cases["apple_foreground_operation"]["required_outcomes"])
    assert {
        "old_incarnation_fenced",
        "foreground_return_uses_fresh_incarnation",
    } <= set(cases["apple_background_drain_and_fence"]["required_outcomes"])


def test_a12_acceptance_cases_cover_gates_artifacts_parity_a11_and_tool_exclusion() -> None:
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
        "failed_gate_named_exactly",
        "unknown_or_stale_gate_blocks_dependent_rung",
        "no_atomic_promotion_without_full_integrity",
        "parity_or_binding_mismatch_rejected",
        "mobile_draft_operation_blocked",
        "target_resumes_from_committed_watermark",
        "normal_product_path_gate_remains_open",
    } <= outcomes
    assert {
        "missing_gate_as_healthy",
        "unassigned_chunk_disclosed",
        "partial_artifact_promoted",
        "a11_design_input_as_authority",
        "draft_rolls_back_target_kv",
        "termux_as_normal_path",
        "adb_gate_dependency_accepted",
        "ssh_gate_dependency_accepted",
    } <= side_effects


def test_a12_ui_inventory_is_all_workspace_dynamic_private_and_rung_explicit() -> None:
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
        "four_rung_ladder",
        "role_qualification",
        "pseudonymous_platform_family",
        "signed_membership",
        "capability_eligibility",
        "artifact_readiness",
        "operation_qualification",
        "thermal_power_withdrawal",
        "role_opt_in",
    } <= required
    assert {
        "native_synthetic_separate",
        "registered_values_without_brand_branch",
        "proofs_independently_fresh",
        "reconnect_reconstructs",
    } <= dynamic
    assert {
        "role_ladder_collapses_rungs",
        "pixel_specific_backend",
        "eligibility_implies_placement",
        "rung_collapse",
        "development_tool_as_product_path",
    } <= forbidden


def test_a12_specification_is_design_only_and_inventory_bound() -> None:
    specification = SPECIFICATION.read_text("utf-8")
    assert "**Status:** `design_only`;" in specification
    assert "Until then A12 remains `design_only`" in specification
    assert A9_ACCEPTANCE_COMMIT in specification
    assert f"`{INVENTORY.relative_to(ROOT)}`" in specification
    assert f"`{PROTOCOL}`" in specification
    for decision_id in REQUIRED_DECISIONS:
        assert f"`{decision_id}`" in specification
