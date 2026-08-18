from __future__ import annotations

import json
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
INVENTORY = Path(__file__).with_name("inventory.v1.json")
SPECIFICATION = (
    ROOT
    / "docs/superpowers/specs/2026-08-18-mycelium-a8-internet-native-control.md"
)
PROTOCOL = "mycelium.a8_acceptance_inventory.v1"
CLAIM_BOUNDARY = (
    "frozen infrastructure decisions and future acceptance inputs only; no "
    "implementation, network contact, physical evidence, readiness, or completion claim"
)
REQUIRED_DECISIONS = {
    "public_https_bootstrap_authentication",
    "seed_key_pinning",
    "invitation_and_revocation",
    "iroh_direct_relay_activation",
    "unknown_not_zero_measurements",
    "privacy_safe_relay_projection",
    "ssh_tailscale_independence",
}
REQUIRED_NEGATIVE_CASES = {
    "cleartext_or_redirect_bootstrap",
    "certificate_without_seed_authority",
    "invalid_or_replayed_invitation",
    "revoked_active_member",
    "endpoint_identity_mismatch",
    "missing_or_stale_path_measurements",
    "raw_relay_identity_injection",
    "unqualified_external_member",
    "tailscale_unavailable",
    "ssh_unavailable",
}
REQUIRED_POSITIVE_CASES = {
    "unrelated_https_invite_without_tailscale",
    "direct_path_qualified_browser_inference",
    "forced_relay_privacy_reduced_browser_inference",
    "observed_path_transition_and_reconnect",
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
    "decision",
    "authorities",
    "required_acceptance",
    "forbidden_fallbacks",
}
NEGATIVE_FIELDS = {
    "case_id",
    "gate_kind",
    "setup",
    "stimulus",
    "required_outcomes",
    "forbidden_side_effects",
}
REQUIRED_OUTCOMES = {
    "no_cleartext_fallback",
    "seed_pin_mismatch_before_invite_transmission",
    "no_partial_member",
    "revoked_connection_removed_from_activation_admission",
    "endpoint_mismatch_rejected",
    "missing_metrics_remain_unknown",
    "raw_relay_identity_rejected",
    "all_serve_authorities_rejected",
    "supported_path_works_without_tailscale",
    "supported_path_works_without_ssh",
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


def test_a8_inventory_is_closed_bounded_design_only_and_specification_bound() -> None:
    inventory = _inventory()
    assert set(inventory) == {
        "protocol",
        "gate",
        "state",
        "claim_boundary",
        "source_specification",
        "decisions",
        "workspace_projections",
        "physical_positive_cases",
        "physical_negative_cases",
    }
    assert inventory["protocol"] == PROTOCOL
    assert inventory["gate"] == "A8"
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

    encoded = INVENTORY.read_text("utf-8")
    assert FORBIDDEN_PUBLIC_VALUES.search(encoded) is None


def test_a8_workspace_projection_matrix_is_exact_and_nonempty() -> None:
    projections = _inventory()["workspace_projections"]
    assert set(projections) == WORKSPACES
    for requirements in projections.values():
        _bounded_unique_names(requirements)


def test_a8_decision_inventory_has_exact_coverage_and_fail_closed_choices() -> None:
    decisions = _inventory()["decisions"]
    assert isinstance(decisions, list) and len(decisions) == len(REQUIRED_DECISIONS)
    assert {decision["decision_id"] for decision in decisions} == REQUIRED_DECISIONS

    acceptance: set[str] = set()
    forbidden: set[str] = set()
    for decision in decisions:
        assert isinstance(decision, dict) and set(decision) == DECISION_FIELDS
        assert isinstance(decision["decision"], str)
        assert 40 <= len(decision["decision"]) <= 512
        _bounded_unique_names(decision["authorities"])
        acceptance.update(_bounded_unique_names(decision["required_acceptance"]))
        forbidden.update(_bounded_unique_names(decision["forbidden_fallbacks"]))

    assert {
        "seed_signature_required_after_tls",
        "pin_checked_before_secret_transmission",
        "revoked_open_activation_removed",
        "forced_relay_observed_not_configured",
        "missing_metrics_remain_unknown",
        "raw_relay_identity_rejected",
        "tailscale_disabled_before_join",
        "ssh_absent_on_external_peer",
    } <= acceptance
    assert {
        "cleartext_http",
        "trust_on_first_use",
        "revoked_connection_survival",
        "address_only_trust",
        "default_zero_coercion",
        "raw_relay_url",
        "tailnet_fallback",
        "ssh_serving",
    } <= forbidden


def test_a8_physical_negative_inventory_is_exact_and_side_effect_bounded() -> None:
    cases = _inventory()["physical_negative_cases"]
    assert isinstance(cases, list) and 1 <= len(cases) <= 32
    assert {case["case_id"] for case in cases} == REQUIRED_NEGATIVE_CASES

    outcomes: set[str] = set()
    side_effects: set[str] = set()
    for case in cases:
        assert isinstance(case, dict) and set(case) == NEGATIVE_FIELDS
        assert case["gate_kind"] == "physical_negative"
        assert isinstance(case["setup"], str) and 20 <= len(case["setup"]) <= 256
        assert isinstance(case["stimulus"], str) and 10 <= len(case["stimulus"]) <= 256
        outcomes.update(_bounded_unique_names(case["required_outcomes"]))
        side_effects.update(_bounded_unique_names(case["forbidden_side_effects"]))

    assert REQUIRED_OUTCOMES <= outcomes
    assert {
        "invite_secret_transmitted",
        "member_created",
        "route_mutated",
        "silent_repin",
        "lease_extended",
        "new_request_admitted",
        "zero_metrics_fabricated",
        "raw_value_logged",
        "artifact_disclosed",
        "tailnet_fallback",
        "ssh_fallback",
    } <= side_effects


def test_a8_physical_positive_inventory_is_exact_and_ordinary_path_bound() -> None:
    cases = _inventory()["physical_positive_cases"]
    assert isinstance(cases, list) and len(cases) == len(REQUIRED_POSITIVE_CASES)
    assert {case["case_id"] for case in cases} == REQUIRED_POSITIVE_CASES

    outcomes: set[str] = set()
    side_effects: set[str] = set()
    for case in cases:
        assert isinstance(case, dict) and set(case) == NEGATIVE_FIELDS
        assert case["gate_kind"] == "physical_positive"
        assert isinstance(case["setup"], str) and 20 <= len(case["setup"]) <= 256
        assert isinstance(case["stimulus"], str) and 10 <= len(case["stimulus"]) <= 256
        outcomes.update(_bounded_unique_names(case["required_outcomes"]))
        side_effects.update(_bounded_unique_names(case["forbidden_side_effects"]))

    assert {
        "https_bootstrap_succeeds",
        "browser_inference_completes",
        "positive_physical_counters",
        "privacy_safe_relay_reference_only",
        "transition_generations_retained",
        "subsequent_request_completes",
    } <= outcomes
    assert {
        "tailnet_fallback",
        "unqualified_member_serves",
        "raw_relay_identity_emitted",
        "prior_observation_rewritten",
    } <= side_effects
