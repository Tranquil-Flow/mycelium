from __future__ import annotations

import json
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
INVENTORY = Path(__file__).with_name("inventory.v1.json")
SPECIFICATION = (
    ROOT
    / "docs/superpowers/specs/2026-08-18-mycelium-a13-cross-platform-onboarding.md"
)
PROTOCOL = "mycelium.a13_onboarding_acceptance_inventory.v1"
CLAIM_BOUNDARY = (
    "frozen identity, invitation, package, consent, lifecycle, clean-device, and "
    "projection acceptance inputs only; no installer, signing operation, external "
    "account, device execution, service, evidence, qualification, or completion claim"
)
REQUIRED_DECISIONS = {
    "target_device_owned_identity",
    "recipient_bound_single_use_invitation",
    "platform_signed_package_update",
    "explicit_consent_resource_limits",
    "normal_user_no_expert_tools",
    "dynamic_onboarding_ladder",
    "managed_lifecycle_revocation_removal",
    "unrelated_network_onboarding",
    "live_all_workspace_projection",
}
REQUIRED_CASES = {
    "target_identity_creation_and_durability",
    "identity_retry_substitution_rejection",
    "signed_package_clean_install",
    "invalid_package_or_update_rejection",
    "encrypted_recipient_pairing",
    "expired_reused_or_forged_invitation",
    "recipient_nonce_seed_substitution",
    "deep_link_secret_sink_rejection",
    "explicit_consent_and_resource_policy",
    "consent_or_resource_limit_withdrawal",
    "clean_device_normal_user_onboarding",
    "expert_dependency_rejection",
    "unrelated_network_join_and_reconnect",
    "dynamic_ladder_authority_isolation",
    "managed_update_requalification",
    "revocation_during_managed_work",
    "online_and_offline_uninstall",
    "interrupted_onboarding_recovery",
    "all_workspace_live_reconstruction",
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
ONBOARDING_LADDER = [
    "package_verification",
    "target_identity",
    "pairing_request",
    "owner_consent_and_invite_authorization",
    "encrypted_invitation_handoff",
    "seed_pin_and_atomic_join",
    "signed_membership",
    "lease",
    "software_update",
    "capability_probe",
    "class_qualification",
    "directed_link_evidence",
    "assignment",
    "artifact_acquisition",
    "load",
    "startup_challenge",
    "deployment_qualification",
    "selection",
]
MANAGED_LIFECYCLE_STATES = {
    "install",
    "lease_renewal",
    "reconnect",
    "capability_refresh",
    "update",
    "rollback",
    "restart",
    "suspend",
    "network_loss",
    "low_storage",
    "revoked_membership",
    "drain",
    "uninstall",
    "retained_identity_reinstall",
    "removed_identity_reenrollment",
}
PLATFORM_EXECUTION_FIELDS = {
    "platform_path",
    "claim_level",
    "physical_execution_required",
    "positive_case_ids",
    "negative_case_ids",
}


def _inventory() -> dict:
    value = json.loads(INVENTORY.read_text("utf-8"))
    assert isinstance(value, dict)
    return value


def _bounded_unique_names(values: object, *, maximum: int = 24) -> set[str]:
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


def test_a13_inventory_is_closed_and_design_only() -> None:
    inventory = _inventory()
    assert set(inventory) == {
        "protocol",
        "gate",
        "state",
        "claim_boundary",
        "source_specification",
        "identity_requirements",
        "invitation_requirements",
        "package_update_requirements",
        "physical_execution_matrix",
        "consent_requirements",
        "normal_user_path_requirements",
        "onboarding_ladder",
        "managed_lifecycle_states",
        "decisions",
        "acceptance_cases",
        "ui_projections",
    }
    assert inventory["protocol"] == PROTOCOL
    assert inventory["gate"] == "A13"
    assert inventory["state"] == "design_only"
    assert inventory["claim_boundary"] == CLAIM_BOUNDARY
    assert inventory["source_specification"] == str(SPECIFICATION.relative_to(ROOT))


def test_a13_target_owned_identity_and_invitation_boundaries_are_frozen() -> None:
    inventory = _inventory()
    assert inventory["identity_requirements"] == {
        "creator": "target_device",
        "algorithm": "ed25519",
        "creation_precedes_invitation_minting": True,
        "platform_protected_storage_required_when_available": True,
        "private_key_export_allowed": False,
        "silent_regeneration_on_retry_allowed": False,
        "forbidden_private_key_consumers": [
            "browser_ui",
            "logs",
            "diagnostics",
            "qr_payload",
            "coordinator",
            "owner_device",
        ],
    }
    invitation = inventory["invitation_requirements"]
    assert invitation["pairing_request_contains_swarm_secret"] is False
    assert invitation["recipient_bound_encryption_required"] is True
    assert invitation["short_lived_required"] is True
    assert invitation["single_use_required"] is True
    assert invitation["identity_nonce_platform_and_seed_binding_required"] is True
    assert invitation["target_local_decryption_required"] is True
    assert invitation["atomic_join_required"] is True
    assert invitation["plaintext_handoff_erasure_required"] is True
    assert set(invitation["allowed_return_forms"]) == {
        "qr",
        "registered_deep_link",
        "owner_private_recovery_file",
    }
    assert {
        "url_query",
        "url_fragment",
        "shell_argument",
        "clipboard_projection",
        "log",
        "web_redirect",
        "third_party_tracker",
        "pasteboard",
        "cookie",
        "analytics_sdk",
    } == set(invitation["forbidden_invitation_sinks"])

    cases = _cases_by_id()
    assert {
        "identity_created_on_target_before_invite",
        "identity_digest_stable_across_restart",
        "private_key_never_exported",
    } <= set(cases["target_identity_creation_and_durability"]["required_outcomes"])
    assert {
        "pairing_request_contains_no_swarm_secret",
        "invitation_ciphertext_recipient_bound",
        "single_use_join_commits_one_member_generation",
    } <= set(cases["encrypted_recipient_pairing"]["required_outcomes"])
    assert "invite_reuse_accepted" in cases["expired_reused_or_forged_invitation"][
        "forbidden_side_effects"
    ]


def test_a13_signed_package_update_consent_and_resource_limits_are_frozen() -> None:
    inventory = _inventory()
    package = inventory["package_update_requirements"]
    assert set(package["supported_package_classes"]) == {
        "signed_macos_package",
        "signed_linux_package",
        "signed_windows_installer",
        "signed_android_application",
        "signed_ios_ipados_application",
        "limited_browser_member",
    }
    assert {
        "source_revision",
        "build_inputs",
        "platform_architecture",
        "package_digest",
        "publisher_identity",
        "minimum_os",
        "service_descriptor",
        "update_channel",
        "rollback_floor",
        "capability_protocol",
        "privacy_notice",
        "expiry_revocation_policy",
    } == set(package["manifest_bindings"])
    assert package["signature_and_publisher_verification_required"] is True
    assert package["downgrade_below_rollback_floor_allowed"] is False
    assert package["update_may_change_device_or_swarm_identity"] is False
    assert package["runtime_change_requires_capability_requalification"] is True
    assert package["package_may_embed_invite_or_owner_credential"] is False

    consent = inventory["consent_requirements"]
    assert consent["assigned_peer_visibility_disclosure_required"] is True
    assert set(consent["resource_policy_dimensions"]) == {
        "battery_power",
        "metered_network",
        "background_operation",
        "storage_budget",
        "thermal_limits",
        "update_channel",
        "diagnostics",
        "revocation_consequences",
    }
    assert consent["invitation_class_ceiling_is_qualification"] is False
    assert consent["installation_implies_inference_eligibility"] is False

    cases = _cases_by_id()
    assert "package_signature_and_publisher_verified" in cases[
        "signed_package_clean_install"
    ]["required_outcomes"]
    assert "unsigned_update_applied" in cases["invalid_package_or_update_rejection"][
        "forbidden_side_effects"
    ]
    assert "resource_dimensions_stored_separately" in cases[
        "explicit_consent_and_resource_policy"
    ]["required_outcomes"]


def test_a13_requires_physical_execution_for_every_advertised_platform_path() -> None:
    inventory = _inventory()
    advertised = set(
        inventory["package_update_requirements"]["supported_package_classes"]
    )
    matrix = inventory["physical_execution_matrix"]
    assert isinstance(matrix, list) and len(matrix) == len(advertised)
    assert {entry["platform_path"] for entry in matrix} == advertised

    cases = _cases_by_id()
    for entry in matrix:
        assert set(entry) == PLATFORM_EXECUTION_FIELDS
        assert entry["claim_level"] in {
            "managed_native_host",
            "a12_qualified_mobile_level",
            "limited_probe_only",
        }
        assert entry["physical_execution_required"] is True
        positive_ids = entry["positive_case_ids"]
        negative_ids = entry["negative_case_ids"]
        assert isinstance(positive_ids, list) and positive_ids
        assert isinstance(negative_ids, list) and negative_ids
        assert len(positive_ids) == len(set(positive_ids))
        assert len(negative_ids) == len(set(negative_ids))
        assert all(cases[case_id]["gate_kind"] == "physical_positive" for case_id in positive_ids)
        assert all(cases[case_id]["gate_kind"] == "physical_negative" for case_id in negative_ids)


def test_a13_normal_user_unrelated_network_and_clean_device_gates_are_frozen() -> None:
    requirements = _inventory()["normal_user_path_requirements"]
    assert requirements["clean_device_required"] is True
    assert requirements["unrelated_networks_required"] is True
    assert set(requirements["excluded_dependencies"]) == {
        "ssh",
        "tailscale",
        "termux",
        "adb",
        "source_checkout",
        "repository_knowledge",
        "seed_url",
        "endpoint_id",
        "sidecar",
        "shell",
        "cli_setup",
        "manual_model_copy",
    }
    assert requirements["a8_https_membership_control_required"] is True
    assert requirements["authenticated_activation_plane_required"] is True
    assert requirements["public_admin_endpoint_allowed"] is False
    assert requirements["unencrypted_control_allowed"] is False

    cases = _cases_by_id()
    assert {
        "signed_package_installed_through_normal_channel",
        "target_owned_identity_created",
        "encrypted_single_use_pairing_completed",
        "unrelated_network_join_completed",
        "signed_member_visible_but_unqualified",
    } <= set(cases["clean_device_normal_user_onboarding"]["required_outcomes"])
    assert {
        "ssh_as_normal_path",
        "tailscale_as_normal_path",
        "termux_as_normal_path",
        "adb_as_normal_path",
        "repository_knowledge_as_normal_path",
    } <= set(cases["expert_dependency_rejection"]["forbidden_side_effects"])
    assert {
        "https_bootstrap_succeeds_without_same_lan",
        "network_loss_and_reconnect_are_durable_states",
    } <= set(cases["unrelated_network_join_and_reconnect"]["required_outcomes"])


def test_a13_decisions_and_cases_cover_dynamic_lifecycle_revocation_and_removal() -> None:
    inventory = _inventory()
    assert inventory["onboarding_ladder"] == ONBOARDING_LADDER
    assert set(inventory["managed_lifecycle_states"]) == MANAGED_LIFECYCLE_STATES

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
        "target_creates_identity_before_owner_mints_invite",
        "handoff_is_short_lived_single_use_and_recipient_bound",
        "update_verifies_signature_publisher_digest_channel_and_rollback_floor",
        "resource_policy_dimensions_are_separate_and_explicit",
        "clean_device_normal_user_completes_without_expert_setup",
        "every_rung_has_live_authority_provenance_and_blockers",
        "revocation_blocks_future_work_and_withdraws_eligibility",
        "a8_https_bootstrap_works_across_unrelated_networks",
        "all_workspaces_share_current_public_generation",
    } <= invariants
    assert {
        "owner_generated_target_identity",
        "reused_or_substituted_invitation",
        "unsigned_update_applied",
        "implicit_consent",
        "ssh_dependency",
        "hard_coded_member_or_platform_inventory",
        "offline_uninstall_as_revocation",
        "tailscale_fallback",
        "browser_state_grants_membership_or_eligibility",
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
        "dependent_later_rungs_withdraw",
        "signed_update_verified_and_applied",
        "future_work_and_lease_renewal_blocked",
        "online_uninstall_requests_bounded_drain",
        "offline_uninstall_does_not_claim_revocation",
        "last_durable_rung_reconstructs",
    } <= outcomes
    assert {
        "transitive_rung_grant",
        "eligibility_carried_across_changed_runtime",
        "revoked_member_reconnects_as_current",
        "offline_revocation_fabricated",
        "progress_guessed_from_browser",
    } <= side_effects


def test_a13_ui_inventory_is_live_all_workspace_and_private() -> None:
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
        "membership_not_capacity",
        "installation_pairing_wizard",
        "membership_reachability",
        "pseudonymous_member_generation",
        "candidate_member_input",
        "target_identity",
        "invitation",
        "update",
        "invite_approve_revoke",
    } <= required
    assert {
        "reconnect_reconstructs",
        "live_authority_provenance",
        "unrelated_network_state",
        "update_requalification_live",
        "rungs_independently_fresh",
        "terminal_history_retained",
    } <= dynamic
    assert {
        "joined_member_is_serving",
        "fixed_supported_device_list",
        "onboarding_traffic_is_inference",
        "membership_implies_eligibility",
        "new_member_auto_placed",
        "rung_success_grants_later_rung",
        "plaintext_invitation",
        "owner_can_supply_target_private_key",
    } <= forbidden

    case = _cases_by_id()["all_workspace_live_reconstruction"]
    assert {
        "all_workspaces_share_current_generation",
        "private_pairing_state_confined_to_owning_session",
        "clean_second_session_reconstructs_public_truth",
    } <= set(case["required_outcomes"])


def test_a13_specification_is_design_only_and_inventory_bound() -> None:
    specification = SPECIFICATION.read_text("utf-8")
    assert "**Status:** `design_only`;" in specification
    assert "Until then it remains `design_only`" in specification
    assert f"`{INVENTORY.relative_to(ROOT)}`" in specification
    assert f"`{PROTOCOL}`" in specification
    for decision_id in REQUIRED_DECISIONS:
        assert f"`{decision_id}`" in specification
