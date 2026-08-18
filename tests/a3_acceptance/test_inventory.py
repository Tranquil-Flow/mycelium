from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
INVENTORY = Path(__file__).with_name("inventory.v1.json")
SPECIFICATION = (
    ROOT
    / "docs/superpowers/specs/2026-08-15-mycelium-a3-useful-local-model-qualification.md"
)
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
GATE_FAMILIES = {
    "physical_positive",
    "physical_negative",
    "selector",
    "browser",
    "warm_reuse",
    "incumbent_switchback",
}
PHYSICAL_NEGATIVE_REASONS = {
    "stale_authority_or_evidence",
    "incomplete_files_or_mixed_revisions",
    "identity_or_manifest_drift",
    "insufficient_resource_or_connectivity",
    "unsupported_runtime_combination",
    "unauthorized_conversion",
    "parity_or_kv_cleanup_failure",
    "dead_peer_fatal_route_or_stalled_counter",
    "startup_challenge_failure",
    "not_currently_qualifier_approved",
}
REQUIRED_CASES = {
    "qualified_7b_physical_browser_request",
    "physical_rejection_preserves_incumbent",
    "selector_requires_current_qualification",
    "browser_lifecycle_reconstruction",
    "exact_warm_reacquisition",
    "warm_reuse_mismatch_fails_closed",
    "incumbent_switchback_future_request",
}
CASE_FIELDS = {
    "case_id",
    "gate_kind",
    "gate_families",
    "required_outcomes",
    "forbidden_side_effects",
}


def _load(path: Path) -> dict:
    value = json.loads(path.read_text("utf-8"))
    assert isinstance(value, dict)
    return value


def _inventory() -> dict:
    return _load(INVENTORY)


def _cases() -> dict[str, dict]:
    return {case["case_id"]: case for case in _inventory()["acceptance_cases"]}


def test_a3_inventory_is_closed_non_evidentiary_and_specification_bound() -> None:
    inventory = _inventory()
    assert set(inventory) == {
        "protocol",
        "gate",
        "state",
        "claim_boundary",
        "source_specification",
        "required_gate_families",
        "physical_negative_reasons",
        "workspace_projection_matrix",
        "acceptance_cases",
        "qualification_claim",
        "completion_claim",
    }
    assert inventory["protocol"] == "mycelium.a3_acceptance_inventory.v1"
    assert inventory["gate"] == "A3"
    assert inventory["state"] == "acceptance_inventory_only"
    assert inventory["source_specification"] == str(SPECIFICATION.relative_to(ROOT))
    assert inventory["qualification_claim"] is False
    assert inventory["completion_claim"] is False


def test_a3_gate_families_and_physical_negative_reasons_are_exact() -> None:
    inventory = _inventory()
    assert set(inventory["required_gate_families"]) == GATE_FAMILIES
    assert len(inventory["required_gate_families"]) == len(GATE_FAMILIES)
    assert set(inventory["physical_negative_reasons"]) == PHYSICAL_NEGATIVE_REASONS
    assert len(inventory["physical_negative_reasons"]) == len(
        PHYSICAL_NEGATIVE_REASONS
    )

    cases = inventory["acceptance_cases"]
    assert {case["case_id"] for case in cases} == REQUIRED_CASES
    covered: set[str] = set()
    for case in cases:
        assert set(case) == CASE_FIELDS
        assert case["gate_kind"] in {
            "physical_positive",
            "physical_negative",
            "selector_contract",
            "browser_positive",
            "warm_reuse_positive",
            "warm_reuse_negative",
        }
        families = case["gate_families"]
        assert len(families) == len(set(families))
        assert set(families) <= GATE_FAMILIES
        covered.update(families)
        for field in ("required_outcomes", "forbidden_side_effects"):
            values = case[field]
            assert isinstance(values, list) and values
            assert len(values) == len(set(values))
    assert covered == GATE_FAMILIES


def test_a3_positive_negative_selector_browser_warm_and_switchback_are_bound() -> None:
    cases = _cases()
    assert {
        "same_run_qualifier_acceptance",
        "ordinary_browser_prompt_streams_output",
        "positive_per_peer_counters",
        "terminal_stage_local_kv_cleanup",
    } <= set(cases["qualified_7b_physical_browser_request"]["required_outcomes"])
    assert {
        "rejected_before_selection",
        "incumbent_remains_usable",
        "candidate_not_published",
    } <= set(cases["physical_rejection_preserves_incumbent"]["required_outcomes"])
    assert "only_current_qualified_deployments_listed" in cases[
        "selector_requires_current_qualification"
    ]["required_outcomes"]
    assert "clean_second_session_reconstructs_public_truth" in cases[
        "browser_lifecycle_reconstruction"
    ]["required_outcomes"]
    assert {
        "cached_verified_bytes_equal_total_bytes",
        "zero_transferred_origin_and_peer_bytes",
        "same_promotion_digest",
    } <= set(cases["exact_warm_reacquisition"]["required_outcomes"])
    assert {
        "already_admitted_request_unchanged",
        "next_browser_request_uses_incumbent",
        "incumbent_request_completes",
    } <= set(cases["incumbent_switchback_future_request"]["required_outcomes"])


def _workspace_ids(path: str, projection_kind: str) -> set[str]:
    document = _load(ROOT / path)
    if projection_kind == "keys":
        return set(document["workspace_requirements"])
    if projection_kind == "packet_keys":
        return set(document["primary_integrator_ui"]["workspace_bindings"])
    if projection_kind == "projection_keys":
        return set(document["workspace_projections"])
    if projection_kind == "ui_ids":
        return {item["workspace_id"] for item in document["ui_projections"]}
    raise AssertionError(f"unknown projection kind: {projection_kind}")


def test_a4_through_a14_inventories_use_exact_canonical_workspace_set() -> None:
    inventories = {
        "A4 scenarios": ("tests/a4_acceptance/scenarios.v1.json", "keys"),
        "A4 packet": (
            "tests/a4_acceptance/implementation_packet.v1.json",
            "packet_keys",
        ),
        "A5": ("tests/a5_acceptance/benchmark_protocol.v1.json", "projection_keys"),
        "A6": ("tests/a6_acceptance/inventory.v1.json", "projection_keys"),
        "A7": ("tests/a7_acceptance/inventory.v1.json", "projection_keys"),
        "A8": ("tests/a8_acceptance/inventory.v1.json", "projection_keys"),
        "A9": ("tests/a9_acceptance/inventory.v1.json", "ui_ids"),
        "A10": ("tests/a10_acceptance/scenarios.v1.json", "keys"),
        "A11": ("tests/a11_acceptance/scenarios.v1.json", "keys"),
        "A12": ("tests/a12_acceptance/inventory.v1.json", "ui_ids"),
        "A13": ("tests/a13_acceptance/inventory.v1.json", "ui_ids"),
        "A14": ("tests/a14_acceptance/inventory.v1.json", "projection_keys"),
    }
    for gate, (path, projection_kind) in inventories.items():
        assert _workspace_ids(path, projection_kind) == WORKSPACES, gate
