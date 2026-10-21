# SPDX-License-Identifier: AGPL-3.0-only
from __future__ import annotations

import fnmatch
import json
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
PACKET_PATH = Path(__file__).with_name("implementation_packet.v1.json")
INVENTORY_PATH = Path(__file__).with_name("inventory.v1.json")
HANDOVER_PATH = (
    ROOT / "docs/handover/2026-08-18-mycelium-a8-implementation-packet.md"
)
SPEC_PATH = (
    ROOT
    / "docs/superpowers/specs/2026-08-18-mycelium-a8-internet-native-control.md"
)
PLAN_PATH = (
    ROOT / "docs/superpowers/plans/2026-08-11-mycelium-completion-plan.md"
)
A4_PACKET_PATH = (
    ROOT / "docs/handover/2026-08-18-mycelium-a4-implementation-packet.md"
)
PROTOCOL = "mycelium.a8_implementation_packet.v1"
PACKET_BASE = "181dc07e06978dd45281434acf64460b61ea54a9"
CLAIM_BOUNDARY = (
    "machine-checked A8 leaf implementation handover only; no production "
    "implementation, shared wiring, external listener, network contact, physical "
    "evidence, qualification, readiness, or completion claim"
)
SOURCE_INPUTS = [
    "docs/superpowers/specs/2026-08-18-mycelium-a8-internet-native-control.md",
    "tests/a8_acceptance/inventory.v1.json",
    "docs/handover/2026-08-18-mycelium-a4-implementation-packet.md",
    "docs/superpowers/plans/2026-08-11-mycelium-completion-plan.md",
]
TOP_LEVEL_FIELDS = {
    "protocol",
    "gate_state",
    "packet_base_commit",
    "claim_boundary",
    "source_inputs",
    "dependency_gate",
    "leaf_ownership",
    "prohibited_leaf_ownership",
    "https_bootstrap_boundary",
    "membership_authority_boundaries",
    "nullable_observation_boundary",
    "relay_projection_boundary",
    "direct_relay_boundary",
    "deterministic_negative_cases",
    "focused_tests",
    "focused_regression_commands",
    "unresolved_shared_surfaces",
    "later_integration_handoff",
    "qualification_claim",
    "promotion_authorized",
}
EXISTING_SOURCE_PATTERNS = {
    "mycelium_seed/http.py",
    "mycelium_bootstrap_preflight/**",
}
EXISTING_TEST_PATTERNS = {
    "tests/seed_coordinator/test_http.py",
    "tests/bootstrap_preflight/**",
}
NEW_PACKAGE_PATHS = {
    "mycelium_internet_control/__init__.py",
    "mycelium_internet_control/observation.py",
    "mycelium_internet_control/relay.py",
}
TEMPLATE_PATHS = {
    "config/internet-bootstrap/README.md",
    "config/internet-bootstrap/bootstrap.env.template",
    "config/internet-bootstrap/reverse-proxy.conf.template",
}
PROHIBITED_PATHS = {
    "mycelium_request_gateway/**",
    "mycelium_membership/contracts.py",
    "mycelium_seed/state.py",
    "mycelium_router/transports/iroh.py",
    "mycelium_live/supervisor.py",
    "mycelium_product_spine/**",
    "contracts/**",
    "ui/contracts/**",
    "mycelium_ui_gateway/**",
    "ui/**",
    "mycelium_physical_runner/**",
    "tests/evidence/**",
    "docs/qualification/**",
}
PROHIBITED_CATEGORIES = {
    "supervisor_or_shared_composition",
    "generated_contract_or_compatibility_fixture",
    "shared_ui_or_ui_gateway",
    "external_listener",
    "credential_or_certificate_private_key",
    "physical_device",
    "shared_fleet_state",
    "provider_account_or_sdk",
    "physical_or_browser_qualification_evidence",
}
ROUTES = {
    ("GET", "/seed/identity", "signed_seed_identity"),
    ("GET", "/seed/rotation", "dual_signed_seed_key_transition"),
    (
        "POST",
        "/seed/join",
        "single_use_invitation_and_signed_generation_zero_join",
    ),
    ("POST", "/seed/resume", "signed_current_member_resume"),
    ("POST", "/seed/message", "signed_current_member_message"),
}
LIMITS = {
    "maximum_canonical_json_body_bytes": 1_048_576,
    "connection_read_timeout_ms": 2_000,
    "maximum_concurrent_requests_default": 32,
    "maximum_concurrent_requests_minimum": 1,
    "maximum_concurrent_requests_maximum": 256,
    "per_invite_attempt_limit_default": 8,
    "per_invite_attempt_limit_minimum": 1,
    "per_invite_attempt_limit_maximum": 32,
    "per_invite_attempt_window_ms": 300_000,
    "maximum_error_body_bytes": 4_096,
}
NULLABLE_FIELDS = {
    "rtt_ms",
    "warm_rtt_ms",
    "jitter_ms",
    "goodput_bytes_per_second",
    "loss_ratio",
    "sample_count",
    "connection_generation",
    "observed_at",
    "fresh_until",
}
NEGATIVE_CASES = {
    "non_https_or_noncanonical_origin",
    "redirect_downgrade_or_route_method_mismatch",
    "tls_success_with_wrong_seed_pin_or_signature",
    "invite_in_query_fragment_cookie_header_or_redirect_body_replay",
    "expired_reused_forged_wrong_swarm_wrong_seed_or_changed_retry_invitation",
    "stale_changed_or_revoked_resume_and_member_generation",
    "missing_stale_nonfinite_negative_or_default_zero_measurement",
    "path_class_inferred_without_bound_live_observation",
    "forced_relay_configuration_claimed_as_observation",
    "raw_relay_network_identity_or_credential_projection",
    "template_contains_provider_account_endpoint_or_secret",
    "leaf_attempts_network_contact_external_bind_shared_state_or_qualification_write",
}
FOCUSED_TESTS = {
    "tests/seed_coordinator/test_http.py::test_a8_https_origin_and_public_route_allowlist_fail_closed",
    "tests/seed_coordinator/test_http.py::test_a8_seed_pin_and_signature_precede_invite_secret_transmission",
    "tests/seed_coordinator/test_http.py::test_a8_join_resume_and_revocation_boundaries_delegate_without_partial_mutation",
    "tests/seed_coordinator/test_http.py::test_a8_request_and_invite_attempt_bounds_fail_closed",
    "tests/bootstrap_preflight/test_preflight.py::test_a8_external_inputs_are_nullable_offline_and_never_claim_physical_readiness",
    "tests/bootstrap_preflight/test_preflight.py::test_a8_template_inventory_is_exact_provider_neutral_and_secret_free",
    "tests/bootstrap_preflight/test_internet_control.py::test_missing_stale_and_rejected_measurements_remain_unknown_not_zero",
    "tests/bootstrap_preflight/test_internet_control.py::test_zero_requires_current_explicit_positive_sample_observation",
    "tests/bootstrap_preflight/test_internet_control.py::test_direct_relay_unknown_requires_bound_live_transport_observation",
    "tests/bootstrap_preflight/test_internet_control.py::test_relay_projection_is_stable_separated_and_rejects_raw_public_identity",
}
SHARED_SURFACES = {
    "membership_state_invite_consumption_resume_lease_and_revocation",
    "activation_admission_open_connection_revocation_and_live_path_observation",
    "browser_gateway_and_public_projection",
    "supervisor_shared_composition_projection_key_and_configuration_injection",
    "versioned_contract_generation_and_compatibility",
    "external_listener_devices_fleet_and_physical_browser_evidence",
}
HANDOFF = [
    "primary_integrator_publishes_and_verifies_one_exact_atomic_A4_commit",
    "A8_leaf_lane_rebases_its_complete_reviewed_history_onto_that_exact_A4_commit",
    "rerun_A8_packet_checks_and_all_deterministic_loopback_leaf_tests_on_the_rebased_tree",
    "primary_integrator_reserves_membership_transport_gateway_supervisor_contract_and_UI_surfaces",
    "integrator_wires_existing_membership_authority_and_detached_Iroh_observations_without_moving_authority_into_the_leaf_package",
    "contract_integrator_generates_closed_contracts_and_compatibility_fixtures_after_leaf_shapes_stabilize",
    "integrator_injects_owner_private_projection_key_and_provider_neutral_configuration_without_committing_credentials",
    "physical_gate_owner_runs_external_preflight_then_unrelated_network_direct_forced_relay_transition_reconnect_revocation_browser_and_privacy_gates",
    "owner_reviews_bound_evidence_before_any_A8_qualification_or_promotion_claim",
]
PROVIDER_NAMES = re.compile(
    r"(?:amazon|aws|azure|cloudflare|digitalocean|fastly|gcp|google cloud|"
    r"hetzner|linode|tailscale)",
    re.IGNORECASE,
)


def _packet() -> dict:
    value = json.loads(PACKET_PATH.read_text("utf-8"))
    assert isinstance(value, dict)
    return value


def _matches(path: str, pattern: str) -> bool:
    if pattern.endswith("/**"):
        return path == pattern[:-3] or path.startswith(pattern[:-2])
    return fnmatch.fnmatchcase(path, pattern)


def _bounded_unique_names(values: object, *, maximum: int = 32) -> set[str]:
    assert isinstance(values, list) and 1 <= len(values) <= maximum
    assert len(values) == len(set(values))
    assert all(
        isinstance(value, str)
        and re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,127}", value) is not None
        for value in values
    )
    return set(values)


def test_a8_leaf_ownership_is_exact_default_deny_and_nonoverlapping() -> None:
    ownership = _packet()["leaf_ownership"]
    assert set(ownership) == {
        "existing_source_patterns",
        "existing_test_patterns",
        "planned_new_package",
        "planned_template_directory",
        "default_rule",
    }
    assert set(ownership["existing_source_patterns"]) == EXISTING_SOURCE_PATTERNS
    assert set(ownership["existing_test_patterns"]) == EXISTING_TEST_PATTERNS

    package = ownership["planned_new_package"]
    assert set(package) == {
        "root",
        "exact_paths",
        "responsibilities",
        "forbidden_dependencies",
    }
    assert package["root"] == "mycelium_internet_control"
    assert set(package["exact_paths"]) == NEW_PACKAGE_PATHS
    _bounded_unique_names(package["responsibilities"])
    assert {
        "membership_state_mutation",
        "iroh_transport_control",
        "supervisor_composition",
        "generated_contracts",
        "provider_sdk",
        "network_io",
    } <= _bounded_unique_names(package["forbidden_dependencies"])

    templates = ownership["planned_template_directory"]
    assert set(templates) == {
        "root",
        "exact_paths",
        "template_only",
        "provider_neutral",
        "may_contain_real_endpoint",
        "may_contain_credential",
        "may_activate_listener",
    }
    assert templates["root"] == "config/internet-bootstrap"
    assert set(templates["exact_paths"]) == TEMPLATE_PATHS
    assert templates["template_only"] is True
    assert templates["provider_neutral"] is True
    assert templates["may_contain_real_endpoint"] is False
    assert templates["may_contain_credential"] is False
    assert templates["may_activate_listener"] is False
    assert PROVIDER_NAMES.search(json.dumps(templates, sort_keys=True)) is None

    allowed = (
        EXISTING_SOURCE_PATTERNS
        | EXISTING_TEST_PATTERNS
        | NEW_PACKAGE_PATHS
        | TEMPLATE_PATHS
    )
    for path in allowed:
        assert not any(_matches(path, pattern) for pattern in PROHIBITED_PATHS)
    for path in EXISTING_SOURCE_PATTERNS | EXISTING_TEST_PATTERNS:
        root = ROOT / path.removesuffix("/**")
        assert root.exists()
    assert "default_deny" in ownership["default_rule"]


def test_a8_prohibited_leaf_ownership_covers_every_shared_surface() -> None:
    prohibited = _packet()["prohibited_leaf_ownership"]
    assert set(prohibited) == {
        "path_patterns",
        "surface_categories",
        "discovery_rule",
    }
    assert set(prohibited["path_patterns"]) == PROHIBITED_PATHS
    assert set(prohibited["surface_categories"]) == PROHIBITED_CATEGORIES
    assert prohibited["discovery_rule"].startswith("stop_for_primary_integrator")

    required_explicit = {
        "mycelium_request_gateway/**",
        "mycelium_membership/contracts.py",
        "mycelium_seed/state.py",
        "mycelium_router/transports/iroh.py",
        "mycelium_live/supervisor.py",
        "contracts/**",
        "mycelium_ui_gateway/**",
        "ui/**",
    }
    assert required_explicit <= set(prohibited["path_patterns"])


def test_a8_dependency_gate_allows_only_loopback_until_atomic_a4_rebase() -> None:
    gate = _packet()["dependency_gate"]
    assert set(gate) == {
        "leaf_work_before_a4",
        "allowed_execution_before_a4",
        "shared_wiring_prerequisite",
        "physical_qualification_prerequisite",
        "rebase_rule",
        "unknown_a4_commit_policy",
        "guessed_interface_policy",
    }
    assert gate["allowed_execution_before_a4"] == "deterministic_loopback_tests_only"
    assert gate["shared_wiring_prerequisite"] == "A4_closed_with_one_atomic_commit"
    assert "exact_atomic_A4_commit" in gate["physical_qualification_prerequisite"]
    assert "before_the_first_shared_wiring_or_physical_preflight_change" in gate["rebase_rule"]
    assert gate["unknown_a4_commit_policy"].startswith("stop_until")
    assert gate["guessed_interface_policy"] == "forbidden"


def test_a8_https_bootstrap_is_closed_bounded_and_authority_separated() -> None:
    boundary = _packet()["https_bootstrap_boundary"]
    assert set(boundary) == {
        "public_origin",
        "leaf_listener",
        "tls_authority",
        "seed_authority",
        "membership_authority",
        "route_allowlist",
        "limits",
        "response_requirements",
        "rejected_inputs",
    }
    assert boundary["public_origin"].startswith("one_canonical_https_origin")
    assert boundary["leaf_listener"].startswith("loopback_http_only")
    assert "only_the_canonical_hostname" in boundary["tls_authority"]
    assert "before_invite_secret_transmission" in boundary["seed_authority"]
    assert "signed_member_envelopes" in boundary["membership_authority"]

    routes = boundary["route_allowlist"]
    assert isinstance(routes, list) and len(routes) == len(ROUTES)
    assert all(set(route) == {"method", "path", "authority"} for route in routes)
    assert {
        (route["method"], route["path"], route["authority"])
        for route in routes
    } == ROUTES
    assert boundary["limits"] == LIMITS
    assert LIMITS["maximum_concurrent_requests_minimum"] <= LIMITS[
        "maximum_concurrent_requests_default"
    ] <= LIMITS["maximum_concurrent_requests_maximum"]
    assert LIMITS["per_invite_attempt_limit_minimum"] <= LIMITS[
        "per_invite_attempt_limit_default"
    ] <= LIMITS["per_invite_attempt_limit_maximum"]
    assert {
        "canonical_json_only",
        "cache_control_no_store",
        "bounded_reason_code_only",
        "no_sensitive_access_log_fields",
    } <= _bounded_unique_names(boundary["response_requirements"])
    assert {
        "cleartext_public_origin",
        "redirect_or_downgrade",
        "query_or_fragment",
        "cookie_or_bearer_membership",
        "transfer_encoding",
        "websocket_upgrade",
        "public_operator_administration",
    } <= _bounded_unique_names(boundary["rejected_inputs"])


def test_a8_join_resume_and_revocation_authorities_do_not_move_into_leaf() -> None:
    boundaries = _packet()["membership_authority_boundaries"]
    assert set(boundaries) == {"invitation", "resume", "revocation"}
    invitation = boundaries["invitation"]
    assert invitation["owner"] == "owner_private_seed_administration"
    assert invitation["secret_location"] == "canonical_POST_seed_join_JSON_body_only"
    assert invitation["required_precondition"].startswith("valid_TLS_hostname")
    assert invitation["atomicity_owner"].endswith("outside_A8_leaf_ownership")

    resume = boundaries["resume"]
    assert resume["owner"] == "signed_current_membership_generation"
    assert set(resume["ambient_authority_forbidden"]) == {
        "ip_address",
        "tls_session",
        "cookie",
        "connection_reuse",
    }

    revocation = boundaries["revocation"]
    assert revocation["owner"] == "membership_state_and_activation_admission_integrators"
    assert "shared_integration_work" in revocation["deferred_effect"]
    assert revocation["forbidden_leaf_effect"] == "mutate_membership_state_or_Iroh_admission"


def test_a8_nullable_observation_and_relay_privacy_rules_are_exact() -> None:
    observation = _packet()["nullable_observation_boundary"]
    assert set(observation) == {
        "path_class_values",
        "nullable_fields",
        "unknown_rule",
        "zero_rule",
        "planning_rule",
        "nonfinite_numbers_allowed",
        "negative_measurements_allowed",
        "raw_endpoint_or_address_allowed",
    }
    assert observation["path_class_values"] == ["direct", "relay", "unknown"]
    assert set(observation["nullable_fields"]) == NULLABLE_FIELDS
    assert "are_null_and_path_class_is_unknown" in observation["unknown_rule"]
    assert "positive_sample_count" in observation["zero_rule"]
    assert "cannot_satisfy" in observation["planning_rule"]
    assert observation["nonfinite_numbers_allowed"] is False
    assert observation["negative_measurements_allowed"] is False
    assert observation["raw_endpoint_or_address_allowed"] is False

    relay = _packet()["relay_projection_boundary"]
    assert set(relay) == {
        "algorithm",
        "domain_separator",
        "key_authority",
        "leaf_key_storage",
        "public_fields",
        "coarse_region_authority",
        "forbidden_public_inputs",
        "continuity_rule",
        "inference_rule",
    }
    assert relay["algorithm"] == "HMAC-SHA256"
    assert relay["domain_separator"] == "mycelium.relay_projection.v1\0"
    assert relay["key_authority"].startswith("owner_private_persistent")
    assert relay["leaf_key_storage"] == "none"
    assert relay["public_fields"] == ["relay_reference", "coarse_region"]
    assert {
        "relay_url",
        "dns_name",
        "ip_address",
        "port",
        "credential",
        "provider_account",
        "exact_location",
        "canonical_relay_identity",
    } == _bounded_unique_names(relay["forbidden_public_inputs"])


def test_a8_direct_relay_distinction_rejects_configuration_and_inference() -> None:
    boundary = _packet()["direct_relay_boundary"]
    assert set(boundary) == {
        "accepted_source",
        "leaf_input",
        "forbidden_sources",
        "forced_relay_rule",
        "transport_wiring_owner",
    }
    assert "bound_live_Iroh_connection_observation" in boundary["accepted_source"]
    assert "detached_provider_neutral" in boundary["leaf_input"]
    assert {
        "same_lan_or_different_network",
        "operating_system_or_peer_class",
        "configured_force_relay_setting",
        "address_or_hostname",
        "participation",
        "operator_typed_EndpointID",
    } == _bounded_unique_names(boundary["forbidden_sources"])
    assert "not_itself_an_observation" in boundary["forced_relay_rule"]
    assert boundary["transport_wiring_owner"].endswith("after_A4_atomic_close")


def test_a8_negative_cases_and_focused_tests_are_exact_leaf_only() -> None:
    packet = _packet()
    assert set(packet["deterministic_negative_cases"]) == NEGATIVE_CASES
    assert len(packet["deterministic_negative_cases"]) == len(NEGATIVE_CASES)

    focused = packet["focused_tests"]
    assert isinstance(focused, list) and len(focused) == len(FOCUSED_TESTS)
    assert all(
        set(item) == {"case_id", "nodeid", "kind", "owned_paths"}
        for item in focused
    )
    assert {item["nodeid"] for item in focused} == FOCUSED_TESTS
    assert len({item["case_id"] for item in focused}) == len(focused)
    assert {item["kind"] for item in focused} <= {
        "deterministic_loopback",
        "deterministic_offline",
    }
    assert all(1 <= len(item["owned_paths"]) <= 2 for item in focused)

    allowed_patterns = (
        EXISTING_SOURCE_PATTERNS
        | EXISTING_TEST_PATTERNS
        | {"mycelium_internet_control/**", "config/internet-bootstrap/**"}
    )
    for item in focused:
        test_path = item["nodeid"].split("::", 1)[0]
        assert any(_matches(test_path, pattern) for pattern in EXISTING_TEST_PATTERNS)
        for path in item["owned_paths"]:
            assert path in allowed_patterns
            assert not any(_matches(path, pattern) for pattern in PROHIBITED_PATHS)

    commands = packet["focused_regression_commands"]
    assert commands == [
        "python3.14 -m pytest -q tests/a8_acceptance",
        "python3.14 -m pytest -q tests/seed_coordinator/test_http.py tests/bootstrap_preflight",
        "ruff check tests/a8_acceptance mycelium_seed/http.py mycelium_bootstrap_preflight tests/seed_coordinator/test_http.py tests/bootstrap_preflight",
    ]
    assert all("physical" not in command and "e2e" not in command for command in commands)


def test_a8_shared_surfaces_and_later_handoff_are_complete_and_ordered() -> None:
    packet = _packet()
    unresolved = packet["unresolved_shared_surfaces"]
    assert isinstance(unresolved, list) and len(unresolved) == len(SHARED_SURFACES)
    assert all(
        set(item) == {"surface", "paths", "owner", "handoff"}
        for item in unresolved
    )
    assert {item["surface"] for item in unresolved} == SHARED_SURFACES
    assert all(item["paths"] and item["owner"] and item["handoff"] for item in unresolved)
    unresolved_paths = {path for item in unresolved for path in item["paths"]}
    assert {
        "mycelium_request_gateway/**",
        "mycelium_membership/contracts.py",
        "mycelium_seed/state.py",
        "mycelium_router/transports/iroh.py",
        "mycelium_live/supervisor.py",
        "contracts/**",
        "ui/**",
    } <= unresolved_paths

    assert packet["later_integration_handoff"] == HANDOFF
    assert HANDOFF.index(
        "A8_leaf_lane_rebases_its_complete_reviewed_history_onto_that_exact_A4_commit"
    ) < HANDOFF.index(
        "primary_integrator_reserves_membership_transport_gateway_supervisor_contract_and_UI_surfaces"
    )
    assert HANDOFF.index(
        "primary_integrator_reserves_membership_transport_gateway_supervisor_contract_and_UI_surfaces"
    ) < HANDOFF.index(
        "physical_gate_owner_runs_external_preflight_then_unrelated_network_direct_forced_relay_transition_reconnect_revocation_browser_and_privacy_gates"
    )
