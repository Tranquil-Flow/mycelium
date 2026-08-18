from __future__ import annotations

import json
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
INVENTORY = Path(__file__).with_name("inventory.v1.json")
SPECIFICATION = (
    ROOT / "docs/superpowers/specs/2026-08-18-mycelium-a14-route-explorer.md"
)
PROTOCOL = "mycelium.a14_acceptance_inventory.v1"
CLAIM_BOUNDARY = (
    "frozen split-delivery, region, privacy, accessibility, and read-only acceptance "
    "inputs only; no production ui, map asset, coordinate, network observation, "
    "membership record, evidence, readiness, or completion claim"
)
REGIONS = {
    "africa",
    "americas_north",
    "americas_south",
    "asia",
    "europe",
    "oceania",
    "polar",
    "unknown",
}
REQUIRED_DECISIONS = {
    "coarse_region_authority",
    "unknown_region_handling",
    "direct_relay_transition_evidence",
    "independent_path_transport_dimensions",
    "privacy_no_ip_coordinates",
    "accessible_alternatives",
    "reduced_motion_low_power",
    "read_only_projection_authority",
}
REQUIRED_CASES = {
    "a14a_without_geographic_region",
    "a14b_before_a14a_rejected",
    "authorized_region_normalization",
    "unknown_conflicting_or_stale_region",
    "network_derived_location_rejected",
    "direct_relay_transition_history",
    "forged_or_stale_transition_rejected",
    "path_transport_filter_independence",
    "sealed_or_historical_not_live",
    "accessible_presentation_equivalence",
    "reduced_motion_and_low_power",
    "projection_mutation_attempt",
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
PRESENTATIONS = {
    "accessible_route_table_timeline",
    "logical_route_graph",
    "coarse_globe_region_visualization",
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
PRESENTATION_FIELDS = {
    "presentation_id",
    "slice_id",
    "normative",
    "requires_region",
    "requires_motion",
    "required_semantics",
    "fallback_behavior",
}
GATE_KINDS = {
    "deterministic_positive",
    "deterministic_negative",
    "physical_positive",
    "physical_negative",
    "browser_positive",
    "browser_negative",
}
FORBIDDEN_PUBLIC_VALUES = re.compile(
    r"(?:https?://|iroh://|tailscale\.com|100\.\d{1,3}\.\d{1,3}\.\d{1,3}|"
    r"-?\d{1,3}\.\d{4,}\s*[,/]\s*-?\d{1,3}\.\d{4,}|"
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


def test_a14_inventory_is_closed_design_only_private_and_specification_bound() -> None:
    inventory = _inventory()
    assert set(inventory) == {
        "protocol",
        "gate",
        "state",
        "claim_boundary",
        "source_specification",
        "slices",
        "region_vocabulary",
        "decisions",
        "workspace_projections",
        "physical_gate_matrix",
        "acceptance_cases",
        "presentations",
    }
    assert inventory["protocol"] == PROTOCOL
    assert inventory["gate"] == "A14"
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
    for region in REGIONS:
        assert f"`{region}`" in specification

    assert FORBIDDEN_PUBLIC_VALUES.search(INVENTORY.read_text("utf-8")) is None


def test_a14_workspace_and_physical_gate_matrices_are_exact() -> None:
    inventory = _inventory()
    projections = inventory["workspace_projections"]
    assert set(projections) == WORKSPACES
    for requirements in projections.values():
        _bounded_unique_names(requirements)

    matrix = inventory["physical_gate_matrix"]
    assert matrix == {
        "positive": ["direct_relay_transition_history"],
        "negative": ["forged_or_stale_transition_rejected"],
        "live_transport_observation_required": True,
        "browser_projection_required": True,
    }
    cases = {case["case_id"]: case for case in inventory["acceptance_cases"]}
    assert all(cases[case_id]["gate_kind"] == "physical_positive" for case_id in matrix["positive"])
    assert all(cases[case_id]["gate_kind"] == "physical_negative" for case_id in matrix["negative"])


def test_a14_slice_order_keeps_accessible_explorer_independent_of_globe() -> None:
    slices = _inventory()["slices"]
    assert isinstance(slices, list) and len(slices) == 2
    by_id = {item["slice_id"]: item for item in slices}
    assert set(by_id) == {"A14a", "A14b"}
    assert by_id["A14a"]["state"] == by_id["A14b"]["state"] == "design_only"
    assert by_id["A14a"]["dependencies"] == ["A8"]
    assert by_id["A14b"]["dependencies"] == ["A14a"]
    assert "complete_semantics_without_globe" in by_id["A14a"]["required_acceptance"]
    assert "geographic_region" in by_id["A14a"]["forbidden_dependencies"]
    assert "a14a_semantics_remain_normative" in by_id["A14b"]["required_acceptance"]
    assert "globe_as_authority" in by_id["A14b"]["forbidden_dependencies"]


def test_a14_region_vocabulary_is_exact_coarse_and_never_network_derived() -> None:
    vocabulary = _inventory()["region_vocabulary"]
    assert set(vocabulary) == {
        "protocol",
        "values",
        "unknown_value",
        "geographic_authority_sources",
        "non_geographic_sources",
        "normalization_rules",
        "forbidden_derivations",
    }
    assert vocabulary["protocol"] == "mycelium.coarse_region.v1"
    assert set(vocabulary["values"]) == REGIONS
    assert vocabulary["unknown_value"] == "unknown"
    assert set(vocabulary["geographic_authority_sources"]) == {
        "owner_declared",
        "reviewed_relay_metadata",
    }
    assert vocabulary["non_geographic_sources"] == ["latency_distance_class"]
    assert {
        "unmapped_to_unknown",
        "ambiguous_to_unknown",
        "conflicting_to_unknown",
        "stale_to_unknown",
        "unauthorized_to_unknown",
    } <= _bounded_unique_names(vocabulary["normalization_rules"])
    assert {
        "ip_address",
        "dns_name",
        "hostname",
        "endpoint_id",
        "rtt",
        "jitter",
        "loss",
        "goodput",
        "route_shape",
        "time_zone",
        "locale",
        "device_setting",
    } == _bounded_unique_names(vocabulary["forbidden_derivations"])


def test_a14_decisions_and_cases_preserve_dimensions_privacy_and_read_only_truth() -> None:
    inventory = _inventory()
    decisions = inventory["decisions"]
    assert isinstance(decisions, list) and len(decisions) == len(REQUIRED_DECISIONS)
    assert {decision["decision_id"] for decision in decisions} == REQUIRED_DECISIONS

    invariants: set[str] = set()
    shortcuts: set[str] = set()
    for decision in decisions:
        assert isinstance(decision, dict) and set(decision) == DECISION_FIELDS
        _bounded_unique_names(decision["authorities"])
        invariants.update(_bounded_unique_names(decision["required_invariants"]))
        shortcuts.update(_bounded_unique_names(decision["forbidden_shortcuts"]))
    assert {
        "missing_region_remains_unknown",
        "transition_creates_new_segment",
        "path_status_independent_of_transport",
        "projection_contains_no_coordinates",
        "table_contains_complete_semantics",
        "reduced_motion_disables_automatic_animation",
        "projection_never_mutates_source",
    } <= invariants
    assert {
        "nearest_region_guess",
        "configured_path_as_observed",
        "selected_implies_observed",
        "ip_geolocation",
        "canvas_only_semantics",
        "motion_required_for_state",
        "map_action_mutates_plan",
    } <= shortcuts

    cases = inventory["acceptance_cases"]
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
        "no_globe_dependency",
        "a14b_blocked",
        "region_unknown",
        "projection_contains_no_coordinates",
        "direct_segment_retained",
        "relay_segment_added",
        "each_dimension_filters_independently",
        "historical_generation_separate",
        "table_semantics_complete",
        "automatic_animation_disabled",
        "source_generation_unchanged",
    } <= outcomes
    assert {
        "default_region_assigned",
        "region_authority_created",
        "coordinate_emitted",
        "raw_network_identity_emitted",
        "direct_segment_rewritten",
        "selected_promoted_to_observed",
        "sealed_record_rendered_live",
        "globe_only_fact",
        "motion_required_for_meaning",
        "planner_input_mutated",
        "readiness_changed",
    } <= side_effects


def test_a14_presentations_keep_table_normative_and_motion_optional() -> None:
    presentations = _inventory()["presentations"]
    assert isinstance(presentations, list) and len(presentations) == len(PRESENTATIONS)
    by_id = {item["presentation_id"]: item for item in presentations}
    assert set(by_id) == PRESENTATIONS

    for presentation in presentations:
        assert isinstance(presentation, dict) and set(presentation) == PRESENTATION_FIELDS
        assert presentation["slice_id"] in {"A14a", "A14b"}
        assert type(presentation["normative"]) is bool
        assert type(presentation["requires_region"]) is bool
        assert presentation["requires_motion"] is False
        _bounded_unique_names(presentation["required_semantics"])
        _bounded_unique_names(presentation["fallback_behavior"])

    table = by_id["accessible_route_table_timeline"]
    graph = by_id["logical_route_graph"]
    globe = by_id["coarse_globe_region_visualization"]
    assert table["slice_id"] == graph["slice_id"] == "A14a"
    assert table["normative"] is True
    assert graph["normative"] is globe["normative"] is False
    assert table["requires_region"] is graph["requires_region"] is False
    assert globe["slice_id"] == "A14b" and globe["requires_region"] is True
    assert "works_without_globe" in table["fallback_behavior"]
    assert "table_contains_complete_semantics" in graph["fallback_behavior"]
    assert "globe_can_be_disabled" in globe["fallback_behavior"]
