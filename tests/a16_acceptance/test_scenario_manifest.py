# SPDX-License-Identifier: AGPL-3.0-only
"""A16 acceptance inventory shape (design only; nothing here qualifies A16)."""
from __future__ import annotations

import json
from pathlib import Path

MANIFEST = Path(__file__).with_name("scenarios.v1.json")
ROOT = Path(__file__).parents[2]
PROTOCOL = "mycelium.a16_acceptance_scenarios.v1"
GATE_KINDS = {
    "deterministic_negative", "deterministic_positive", "physical_positive",
    "physical_negative", "browser_positive",
}
EXPECTED = {
    "a16_opt_in_and_consent_migration": "deterministic_negative",
    "a16_signal_unknown_privacy": "deterministic_negative",
    "a16_reduced_budget_kv_isolation": "deterministic_positive",
    "a16_hysteresis_and_capacity_fence": "deterministic_negative",
    "a16_memory_heavy_app_two_mac": "physical_positive",
    "a16_game_mode_two_mac": "physical_positive",
    "a16_cpu_saturation_without_app_identity": "physical_positive",
    "a16_inflight_successor_or_abort": "physical_negative",
    "a16_probe_loss_and_controller_partition": "physical_negative",
    "a16_thermal_battery_precedence": "physical_negative",
    "a16_restart_reentry_two_mac": "physical_positive",
    "a16_product_status_privacy": "browser_positive",
}
FORBIDDEN_WORDS = ("process_name", "bundle_id", "window_title", "foreground_app", "app_identity_allowed")


def _load() -> dict:
    return json.loads(MANIFEST.read_text())


def test_inventory_is_design_only_and_claims_nothing():
    data = _load()
    assert data["protocol"] == PROTOCOL
    assert data["gate_state"] == "design_only"
    assert data["qualification_claim"] is False
    assert data["promotion_authorized"] is False
    assert "no physical" in data["claim_boundary"] and "qualification" in data["claim_boundary"]
    assert data["dependency_requirements"] == {
        "deterministic_tests_may_close_a16": False,
        "a13_resource_guard_refusal_alone_may_close_a16": False,
        "a6_handoff_requires_current_qualified_successor": True,
        "a13_power_thermal_gates_may_be_softened": False,
        "app_identity_may_be_collected": False,
        "non_macos_ports_in_scope": False,
    }


def test_scenarios_are_exactly_the_drafted_cases_with_their_gate_kinds():
    scenarios = _load()["scenarios"]
    ids = [row["scenario_id"] for row in scenarios]
    assert len(ids) == len(set(ids))
    assert {row["scenario_id"]: row["gate_kind"] for row in scenarios} == EXPECTED
    assert {row["gate_kind"] for row in scenarios} <= GATE_KINDS


def test_each_scenario_has_unique_invariants_and_no_executed_evidence():
    for row in _load()["scenarios"]:
        invariants = row["required_invariants"]
        assert invariants and len(invariants) == len(set(invariants))
        assert all(isinstance(item, str) and item.isidentifier() for item in invariants)
        physical = row["gate_kind"].startswith(("physical", "browser"))
        assert row["execution_state"] == ("unexecuted" if physical else "deterministic_only")
        assert row["physical_evidence"] is None
        assert row["required_workspaces"] and set(row["required_workspaces"]) <= {
            "inference", "device_lab", "network", "nodes", "plans", "readiness", "incidents", "settings"}


def test_deterministic_coverage_points_at_real_test_files_and_known_gaps_are_named():
    for row in _load()["scenarios"]:
        assert row["deterministic_coverage"], row["scenario_id"]
        for path in row["deterministic_coverage"]:
            assert (ROOT / path).is_file(), path
        assert isinstance(row["known_gaps"], list)
    gaps = " ".join(g for row in _load()["scenarios"] for g in row["known_gaps"])
    assert "a1_workspace_publication" in gaps  # the unimplemented A1 rendering is not hidden


def _keys(value):
    if isinstance(value, dict):
        for key, item in value.items():
            yield key
            yield from _keys(item)
    elif isinstance(value, list):
        for item in value:
            yield from _keys(item)


def test_inventory_has_no_app_identity_fields():
    # Invariant names may state the prohibition; no field may carry application identity.
    assert not any(word in key for key in _keys(_load()) for word in FORBIDDEN_WORDS)
