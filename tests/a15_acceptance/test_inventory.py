from __future__ import annotations

import copy
import json
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
INVENTORY = Path(__file__).with_name("inventory.v1.json")
SPECIFICATION = (
    ROOT
    / "docs/superpowers/specs/2026-08-18-mycelium-a15-executed-release-closure.md"
)
PROTOCOL = "mycelium.a15_acceptance_inventory.v1"
CLAIM_BOUNDARY = (
    "frozen release-closure acceptance inputs and rejection behavior only; no assembled "
    "result graph, signature, governance mutation, release artifact, readiness decision, "
    "or completion claim"
)
ATOMIC_GATES = tuple(f"A{index}" for index in range(3, 15))
DIRECT_PREREQUISITES = {
    "A3": {"A2"},
    "A4": {"A3"},
    "A5": {"A4"},
    "A6": {"A4"},
    "A7": {"A6"},
    "A8": {"A3"},
    "A9": {"A8"},
    "A10": {"A4"},
    "A11": {"A10"},
    "A12": {"A4", "A9"},
    "A13": {"A12"},
    "A14": {"A8"},
}
PROVENANCE_KINDS = {"live", "replay", "fixture", "historical"}
DIGEST_CLASSES = {
    "test",
    "audit",
    "physical",
    "browser",
    "model",
    "contract",
    "package",
    "sbom",
}
REQUIRED_DECISIONS = {
    "content_addressed_executed_result_graph",
    "exact_source_clean_tree_binding",
    "atomic_gate_commit_graph",
    "provenance_non_substitution",
    "complete_digest_binding",
    "bounded_signed_exclusions",
    "external_reviewer_reproduction",
    "automatic_revocation",
    "derived_decision_only",
}
REQUIRED_CASES = {
    "canonical_graph_accepts",
    "missing_gate_edge_rejected",
    "extra_gate_edge_rejected",
    "missing_result_dependency_rejected",
    "extra_result_dependency_rejected",
    "dirty_tree_rejected",
    "provenance_substitution_rejected",
    "digest_class_omission_rejected",
    "unsigned_exclusion_rejected",
    "external_reproduction_missing_rejected",
    "expired_evidence_revokes",
    "inconsistent_evidence_revokes",
    "handwritten_boolean_rejected",
    "unsupported_completion_rejected",
}
TOP_FIELDS = {
    "protocol",
    "gate",
    "state",
    "claim_boundary",
    "source_specification",
    "result_graph",
    "source_binding",
    "gate_graph",
    "provenance_policy",
    "digest_policy",
    "exclusion_policy",
    "reviewer_reproduction",
    "revocation_policy",
    "decision_policy",
    "acceptance_cases",
}
FORBIDDEN_VALUES = re.compile(
    r"(?:https?://|iroh://|tailscale\.com|"
    r"(?:token|secret|password|credential)\s*[:=]|"
    r"(?:release_ready|complete)\s*[\"']?\s*:\s*(?:true|false))",
    re.IGNORECASE,
)


def _strict_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        assert key not in result, f"duplicate key: {key}"
        result[key] = value
    return result


def _inventory() -> dict[str, object]:
    value = json.loads(
        INVENTORY.read_text("utf-8"), object_pairs_hook=_strict_object
    )
    assert isinstance(value, dict)
    return value


def _string_set(value: object, *, minimum: int = 1, maximum: int = 32) -> set[str]:
    assert isinstance(value, list) and minimum <= len(value) <= maximum
    assert all(
        isinstance(item, str)
        and re.fullmatch(r"[a-z][a-z0-9_]{0,95}", item) is not None
        for item in value
    )
    assert len(value) == len(set(value))
    return set(value)


def _walk_values(value: object):
    if isinstance(value, dict):
        for item in value.values():
            yield from _walk_values(item)
    elif isinstance(value, list):
        for item in value:
            yield from _walk_values(item)
    else:
        yield value


def _gate_graph_findings(inventory: dict[str, object]) -> set[str]:
    findings: set[str] = set()
    graph = inventory.get("gate_graph")
    if not isinstance(graph, dict):
        return {"gate_graph_shape_invalid"}

    required_gates = graph.get("required_atomic_gates")
    if required_gates != list(ATOMIC_GATES):
        findings.add("atomic_gate_set_mismatch")
    if graph.get("release_root_dependencies") != list(ATOMIC_GATES):
        findings.add("release_root_dependency_set_mismatch")
    if graph.get("allowed_external_prerequisites") != ["A2"]:
        findings.add("external_prerequisite_set_mismatch")

    rows = graph.get("direct_prerequisites")
    if not isinstance(rows, list):
        return findings | {"gate_graph_shape_invalid"}
    actual: dict[str, set[str]] = {}
    for row in rows:
        if (
            not isinstance(row, dict)
            or set(row) != {"gate_id", "dependencies"}
            or not isinstance(row.get("gate_id"), str)
            or not isinstance(row.get("dependencies"), list)
        ):
            findings.add("gate_graph_shape_invalid")
            continue
        gate_id = row["gate_id"]
        dependencies = row["dependencies"]
        if gate_id in actual or any(not isinstance(item, str) for item in dependencies):
            findings.add("gate_graph_shape_invalid")
            continue
        actual[gate_id] = set(dependencies)
        if len(dependencies) != len(actual[gate_id]):
            findings.add("gate_graph_shape_invalid")
    if actual != DIRECT_PREREQUISITES:
        findings.add("gate_graph_mismatch")

    allowed = set(ATOMIC_GATES) | {"A2"}
    if any(dependency not in allowed for deps in actual.values() for dependency in deps):
        findings.add("unknown_gate_dependency")
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(gate_id: str) -> None:
        if gate_id in visiting:
            findings.add("gate_graph_cycle")
            return
        if gate_id in visited or gate_id == "A2":
            return
        visiting.add(gate_id)
        for dependency in actual.get(gate_id, set()):
            visit(dependency)
        visiting.remove(gate_id)
        visited.add(gate_id)

    for gate_id in ATOMIC_GATES:
        visit(gate_id)
    return findings


def test_a15_inventory_is_strict_design_only_private_and_specification_bound() -> None:
    inventory = _inventory()
    assert set(inventory) == TOP_FIELDS
    assert inventory["protocol"] == PROTOCOL
    assert inventory["gate"] == "A15"
    assert inventory["state"] == "design_only"
    assert inventory["claim_boundary"] == CLAIM_BOUNDARY
    assert inventory["source_specification"] == str(SPECIFICATION.relative_to(ROOT))
    assert all(type(value) is not bool for value in _walk_values(inventory))
    assert FORBIDDEN_VALUES.search(INVENTORY.read_text("utf-8")) is None

    specification = SPECIFICATION.read_text("utf-8")
    assert "**Status:** `design_only`;" in specification
    assert f"`{INVENTORY.relative_to(ROOT)}`" in specification
    assert f"`{PROTOCOL}`" in specification
    for decision in REQUIRED_DECISIONS:
        assert f"`{decision}`" in specification


def test_content_addressed_result_graph_requires_exact_verified_dependencies() -> None:
    graph = _inventory()["result_graph"]
    assert isinstance(graph, dict)
    assert set(graph) == {
        "protocol",
        "node_protocol",
        "addressing",
        "root_protocol",
        "required_node_fields",
        "required_root_fields",
        "required_invariants",
        "forbidden_inputs",
    }
    assert graph["protocol"] == "mycelium.executed_result_graph.v1"
    assert graph["node_protocol"] == "mycelium.executed_gate_result.v1"
    assert graph["addressing"] == "sha256_canonical_node_bytes"
    assert graph["root_protocol"] == "mycelium.executed_release_decision.v1"
    assert {
        "node_digest",
        "source_commit",
        "source_tree_digest",
        "tree_state",
        "provenance",
        "artifact_digest",
        "subject",
        "artifact_reference",
        "dependency_gate_ids",
        "dependency_digests",
        "verifier_policy_digest",
        "fresh_until",
        "contract_digest",
        "model_digest",
        "representation_digest",
        "runtime_digest",
        "environment_digest",
        "authority_generation",
    } <= _string_set(graph["required_node_fields"])
    assert {
        "root_digest",
        "gate_commit_digests",
        "result_node_digests",
        "digest_class_roots",
        "reviewer_reproduction_digest",
    } <= _string_set(graph["required_root_fields"])
    assert {
        "node_digest_matches_content",
        "root_digest_matches_content",
        "acyclic_dependencies",
        "dependency_digests_exact",
        "all_references_resolve",
        "unknown_fields_rejected",
        "verified_outcome_only",
    } <= _string_set(graph["required_invariants"])
    assert {
        "handwritten_complete_boolean",
        "handwritten_release_ready_boolean",
        "operator_pass_affidavit",
        "unsupported_completion_label",
    } <= _string_set(graph["forbidden_inputs"])


def test_source_binding_requires_exact_commit_and_clean_tree() -> None:
    binding = _inventory()["source_binding"]
    assert isinstance(binding, dict)
    assert set(binding) == {
        "commit_format",
        "required_tree_state",
        "required_bindings",
        "forbidden_substitutions",
    }
    assert binding["commit_format"] == "git_commit_sha1_40_lower_hex"
    assert binding["required_tree_state"] == "clean"
    assert {
        "exact_source_commit",
        "tracked_tree_digest",
        "submodule_commit_set",
        "dependency_lock_digest_set",
        "result_graph_root",
    } == _string_set(binding["required_bindings"])
    assert {
        "branch_name",
        "abbreviated_commit",
        "dirty_tree_claimed_clean",
        "untracked_build_input",
        "later_equivalent_commit",
    } <= _string_set(binding["forbidden_substitutions"])


def test_gate_graph_is_the_corrected_exact_atomic_a3_through_a14_graph() -> None:
    inventory = _inventory()
    assert _gate_graph_findings(inventory) == set()
    graph = inventory["gate_graph"]
    assert isinstance(graph, dict)
    assert graph["atomic_commit_policy"] == "exactly_one_distinct_commit_per_gate"
    assert {
        "gate_id",
        "commit_digest",
        "parent_commit_digests",
        "source_tree_digest",
        "result_node_digests",
    } == _string_set(graph["required_commit_bindings"])
    assert {
        "missing_gate",
        "extra_gate",
        "missing_direct_edge",
        "extra_direct_edge",
        "duplicate_gate_commit",
        "shared_gate_commit",
        "non_ancestor_prerequisite",
        "cycle",
    } == _string_set(graph["rejected_graph_shapes"])


def test_gate_graph_mutations_reject_missing_and_extra_edges() -> None:
    inventory = _inventory()

    missing = copy.deepcopy(inventory)
    missing_rows = missing["gate_graph"]["direct_prerequisites"]  # type: ignore[index]
    a12 = next(row for row in missing_rows if row["gate_id"] == "A12")
    a12["dependencies"].remove("A4")
    assert "gate_graph_mismatch" in _gate_graph_findings(missing)

    extra = copy.deepcopy(inventory)
    extra_rows = extra["gate_graph"]["direct_prerequisites"]  # type: ignore[index]
    a14 = next(row for row in extra_rows if row["gate_id"] == "A14")
    a14["dependencies"].append("A3")
    assert "gate_graph_mismatch" in _gate_graph_findings(extra)


def test_provenance_kinds_are_exact_and_never_substitute() -> None:
    policy = _inventory()["provenance_policy"]
    assert isinstance(policy, dict)
    assert set(policy) == {"kinds", "rules", "required_invariants"}
    assert set(policy["kinds"]) == PROVENANCE_KINDS
    rules = policy["rules"]
    assert isinstance(rules, list) and len(rules) == len(PROVENANCE_KINDS)
    by_kind = {rule["kind"]: rule for rule in rules}
    assert set(by_kind) == PROVENANCE_KINDS
    assert by_kind["live"]["eligible_scope"] == "current_execution_only"
    for kind in PROVENANCE_KINDS:
        assert PROVENANCE_KINDS - {kind} <= set(
            by_kind[kind]["cannot_substitute_for"]
        )
    assert {"live", "physical"} <= set(by_kind["replay"]["cannot_substitute_for"])
    assert {"live", "physical", "browser", "external_reviewer"} <= set(
        by_kind["fixture"]["cannot_substitute_for"]
    )
    assert {"current", "live", "physical", "browser", "external_reviewer"} <= set(
        by_kind["historical"]["cannot_substitute_for"]
    )
    assert {
        "provenance_explicit",
        "required_kind_exact",
        "fixture_never_promoted",
        "historical_never_current",
        "unknown_provenance_rejected",
    } <= _string_set(policy["required_invariants"])


def test_all_required_digest_classes_are_exact_and_bound() -> None:
    policy = _inventory()["digest_policy"]
    assert isinstance(policy, dict)
    assert policy["algorithm"] == "sha256_lower_hex"
    assert set(policy["required_classes"]) == DIGEST_CLASSES
    bindings = policy["class_bindings"]
    assert isinstance(bindings, list) and len(bindings) == len(DIGEST_CLASSES)
    assert {item["digest_class"] for item in bindings} == DIGEST_CLASSES
    for item in bindings:
        assert set(item) == {"digest_class", "binds"}
        _string_set(item["binds"])
    assert "missing_digest_class" in _string_set(policy["rejected_states"])
    assert "digest_mismatch" in set(policy["rejected_states"])


def test_exclusions_are_signed_bounded_expiring_and_never_turn_into_pass() -> None:
    policy = _inventory()["exclusion_policy"]
    assert isinstance(policy, dict)
    assert policy["protocol"] == "mycelium.signed_release_exclusion.v1"
    assert 1 <= policy["maximum_exclusions_per_generation"] <= 32
    assert 1 <= policy["maximum_requirements_per_exclusion"] <= 16
    assert 1 <= policy["maximum_reason_utf8_bytes"] <= 4096
    assert 1 <= policy["maximum_lifetime_hours"] <= 24 * 31
    assert {"signer_key_id", "expires_at", "signature", "scope", "requirement_ids"} <= (
        _string_set(policy["required_fields"])
    )
    assert {
        "source_integrity",
        "clean_tree",
        "content_digest_integrity",
        "privacy_and_credential_safety",
        "provenance_truth",
        "external_reviewer_reproduction",
    } <= _string_set(policy["non_excludable_requirements"])
    assert {
        "signature_verified",
        "scope_exact",
        "requirement_count_bounded",
        "expiry_bounded",
        "exclusion_never_becomes_pass",
    } <= _string_set(policy["required_invariants"])
    assert {"unsigned_exclusion", "unbounded_scope", "expired_exclusion"} <= _string_set(
        policy["rejected_states"]
    )


def test_external_reviewer_reproduction_is_independent_and_reproducible() -> None:
    reviewer = _inventory()["reviewer_reproduction"]
    assert isinstance(reviewer, dict)
    assert reviewer["protocol"] == "mycelium.external_reviewer_reproduction.v1"
    assert {
        "independent_reviewer",
        "clean_checkout",
        "exact_source_commit",
        "clean_tree",
        "approved_offline_inputs",
        "no_operator_private_state",
    } == _string_set(reviewer["required_environment"])
    assert {
        "verify_source_binding",
        "verify_result_graph",
        "verify_gate_commit_graph",
        "verify_contract_manifest",
        "verify_package_digest",
        "verify_sbom_digest",
        "rerun_required_deterministic_checks",
        "reproduce_required_physical_positive",
        "reproduce_required_physical_negative",
        "record_signed_reproduction_result",
    } == _string_set(reviewer["required_steps"])
    assert {"private_ssh_dependency", "tailscale_dependency", "copied_pass_boolean"} <= (
        _string_set(reviewer["forbidden_shortcuts"])
    )


def test_revocation_is_append_only_for_expiry_and_inconsistency() -> None:
    policy = _inventory()["revocation_policy"]
    assert isinstance(policy, dict)
    assert policy["protocol"] == "mycelium.release_revocation.v1"
    assert {
        "mandatory_evidence_expired",
        "exclusion_expired",
        "graph_digest_inconsistent",
        "source_binding_inconsistent",
        "dependency_edge_inconsistent",
        "artifact_digest_inconsistent",
        "provenance_inconsistent",
        "reviewer_result_invalidated",
    } <= _string_set(policy["triggers"])
    assert {
        "append_new_generation",
        "derive_not_ready",
        "retain_historical_generation",
        "identify_invalidated_nodes",
        "propagate_to_dependents",
        "withhold_affected_claims",
    } == _string_set(policy["required_actions"])
    assert {
        "edit_historical_decision",
        "retain_cached_ready_state",
        "silently_extend_expiry",
        "rewrite_provenance",
        "ignore_inconsistent_dependency",
    } == _string_set(policy["forbidden_actions"])


def test_decision_is_derived_and_manual_or_unsupported_claims_are_rejected() -> None:
    policy = _inventory()["decision_policy"]
    graph = _inventory()["result_graph"]
    assert isinstance(policy, dict) and isinstance(graph, dict)
    assert policy["derivation"] == "verified_graph_only"
    assert set(policy["decision_states"]) == {"ready", "not_ready"}
    assert {
        "validated_result_graph",
        "exact_source_binding",
        "validated_gate_graph",
        "complete_digest_classes",
        "valid_exclusions",
        "external_reviewer_reproduction",
    } == _string_set(policy["required_inputs"])
    assert {
        "handwritten_boolean",
        "manual_checkbox",
        "operator_affidavit",
        "ui_state",
        "milestone_label",
        "unsigned_summary",
        "unverified_link",
    } == _string_set(policy["unsupported_claim_inputs"])
    assert {
        "unsupported_completion_claim",
        "missing_required_result",
        "extra_unknown_result",
        "stale_required_result",
        "inconsistent_required_result",
        "manually_overridden_decision",
    } == _string_set(policy["required_rejections"])
    assert "handwritten_release_ready_boolean" in graph["forbidden_inputs"]


def test_acceptance_case_inventory_covers_positive_mutations_and_revocation() -> None:
    cases = _inventory()["acceptance_cases"]
    assert isinstance(cases, list) and 1 <= len(cases) <= 24
    assert {case["case_id"] for case in cases} == REQUIRED_CASES
    assert len(cases) == len(REQUIRED_CASES)
    for case in cases:
        assert set(case) == {
            "case_id",
            "mutation",
            "expected_result",
            "required_findings",
        }
        assert isinstance(case["mutation"], str) and case["mutation"]
        assert case["expected_result"] in {
            "definition_valid",
            "definition_invalid",
            "release_rejected",
            "new_not_ready_generation",
        }
        assert isinstance(case["required_findings"], list)
        if case["case_id"] == "canonical_graph_accepts":
            assert case["required_findings"] == []
        else:
            _string_set(case["required_findings"])
