from __future__ import annotations

import json
from pathlib import Path


MANIFEST = Path(__file__).with_name("scenarios.v1.json")
PROTOCOL = "mycelium.a12_acceptance_scenarios.v1"
SCENARIO_FIELDS = {
    "scenario_id",
    "claim_kind",
    "role",
    "direct_dependencies",
    "a11_required",
    "expected_eligibility",
    "required_invariants",
}
REQUIRED_SCENARIOS = {
    "ordinary_mobile_stage_after_a4_a9",
    "ordinary_mobile_probe_after_a4_a9",
    "mobile_draft_before_a11",
    "mobile_draft_after_a11",
}


def _manifest() -> dict:
    value = json.loads(MANIFEST.read_text("utf-8"))
    assert isinstance(value, dict)
    return value


def test_a12_acceptance_inventory_has_exact_direct_dependencies() -> None:
    manifest = _manifest()
    assert set(manifest) == {"protocol", "claim_boundary", "scenarios"}
    assert manifest["protocol"] == PROTOCOL
    assert manifest["claim_boundary"] == (
        "frozen acceptance inputs only; no mobile implementation or qualification claim"
    )
    scenarios = manifest["scenarios"]
    assert isinstance(scenarios, list)
    assert {scenario["scenario_id"] for scenario in scenarios} == REQUIRED_SCENARIOS
    for scenario in scenarios:
        assert set(scenario) == SCENARIO_FIELDS
        assert scenario["direct_dependencies"] == ["A4", "A9"]
        assert "A11" not in scenario["direct_dependencies"]
        invariants = scenario["required_invariants"]
        assert isinstance(invariants, list) and 1 <= len(invariants) <= 16
        assert len(invariants) == len(set(invariants))


def test_a12_draft_is_optional_but_fails_closed_until_a11() -> None:
    scenarios = {
        scenario["scenario_id"]: scenario for scenario in _manifest()["scenarios"]
    }
    ordinary = [
        scenarios["ordinary_mobile_stage_after_a4_a9"],
        scenarios["ordinary_mobile_probe_after_a4_a9"],
    ]
    assert all(scenario["a11_required"] is False for scenario in ordinary)
    assert all(
        scenario["claim_kind"] == "required_a12_closure" for scenario in ordinary
    )

    before = scenarios["mobile_draft_before_a11"]
    after = scenarios["mobile_draft_after_a11"]
    assert before["claim_kind"] == after["claim_kind"] == "optional_draft_claim"
    assert before["a11_required"] is after["a11_required"] is True
    assert before["expected_eligibility"] == (
        "ineligible_a11_physical_qualification_required"
    )
    assert "a12_can_close_with_draft_role_ineligible" in before["required_invariants"]
