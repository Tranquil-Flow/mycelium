from __future__ import annotations

import copy

import pytest

from conftest import ROOT, canonical_bytes, refresh_authorization


def _reject(plan: dict[str, object], code: str) -> None:
    from mycelium_physical_preflight import PreflightValidationError, validate_and_generate

    with pytest.raises(PreflightValidationError, match=code):
        validate_and_generate(canonical_bytes(plan), source_tree_root=ROOT)


def test_authorization_statement_must_exactly_bind_all_side_effect_scope(
    plan: dict[str, object],
) -> None:
    plan["authorization_statement"] = "I authorize testing both Macs."
    _reject(plan, "authorization_statement_mismatch")

    changed = copy.deepcopy(plan)
    refresh_authorization(changed)
    changed["coordinator"]["port"] += 1
    _reject(changed, "authorization_statement_mismatch")


def test_requires_exactly_two_named_hosts_users_roles_and_peer_bindings(
    plan: dict[str, object],
) -> None:
    one_host = copy.deepcopy(plan)
    one_host["hosts"] = one_host["hosts"][:1]
    _reject(one_host, "invalid_host_count")

    duplicate = copy.deepcopy(plan)
    duplicate["hosts"][1]["host_name"] = duplicate["hosts"][0]["host_name"]
    refresh_authorization(duplicate)
    _reject(duplicate, "duplicate_host_name")

    root_user = copy.deepcopy(plan)
    root_user["hosts"][1]["ssh_user"] = "root"
    refresh_authorization(root_user)
    _reject(root_user, "unsafe_ssh_user")

    stale = copy.deepcopy(plan)
    stale["hosts"][0]["expected_generation"] = 0
    refresh_authorization(stale)
    _reject(stale, "invalid_expected_generation")

    duplicate_endpoint = copy.deepcopy(plan)
    duplicate_endpoint["hosts"][1]["endpoint_id"] = duplicate_endpoint["hosts"][0]["endpoint_id"]
    refresh_authorization(duplicate_endpoint)
    _reject(duplicate_endpoint, "duplicate_endpoint_id")


def test_rejects_mutable_or_malformed_model_assignment_route_identities(
    plan: dict[str, object],
) -> None:
    bad_commit = copy.deepcopy(plan)
    bad_commit["identities"]["resolved_commit"] = "main"
    _reject(bad_commit, "invalid_resolved_commit")

    bad_digest = copy.deepcopy(plan)
    bad_digest["identities"]["route_plan_digest"] = "sha256:" + "A" * 64
    _reject(bad_digest, "invalid_digest")

    duplicate_assignment = copy.deepcopy(plan)
    duplicate_assignment["hosts"][1]["assignment_id"] = duplicate_assignment["hosts"][0]["assignment_id"]
    refresh_authorization(duplicate_assignment)
    _reject(duplicate_assignment, "duplicate_assignment_id")

    mutable_revision = copy.deepcopy(plan)
    mutable_revision["identities"]["model_id"] = "https://example.invalid/model?revision=main"
    _reject(mutable_revision, "invalid_model_id")


def test_rejects_inline_credentials_secret_fields_and_secret_bearing_cli_arguments(
    plan: dict[str, object],
) -> None:
    inline = copy.deepcopy(plan)
    inline["token"] = "not-allowed"
    _reject(inline, "forbidden_credential_field")

    secret_value = copy.deepcopy(plan)
    secret_value["identities"]["model_id"] = "gh" + "p_" + "A" * 36
    _reject(secret_value, "inline_credential")

    secret_cli = copy.deepcopy(plan)
    secret_cli["identities"]["route_id"] = "runner---api-key=forbidden"
    _reject(secret_cli, "secret_cli_argument")

    token_cli = copy.deepcopy(plan)
    token_cli["identities"]["route_id"] = "runner---token=forbidden"
    _reject(token_cli, "secret_cli_argument")

    for credential_key in ("apitoken", "cookie_session", "privatekey"):
        disguised_field = copy.deepcopy(plan)
        disguised_field[credential_key] = "not-allowed"
        _reject(disguised_field, "forbidden_credential_field")


def test_only_token_file_indirection_is_accepted(plan: dict[str, object]) -> None:
    from mycelium_physical_preflight import validate_and_generate

    result = validate_and_generate(canonical_bytes(plan), source_tree_root=ROOT)
    rendered = canonical_bytes(result)

    assert b"token_file_path" in rendered
    assert b'"token"' not in rendered
    assert b"authorization_header" not in rendered


def test_rejects_unsafe_coordinator_addresses_and_privileged_ports(
    plan: dict[str, object],
) -> None:
    loopback = copy.deepcopy(plan)
    loopback["coordinator"]["address"] = "127.0.0.1"
    refresh_authorization(loopback)
    _reject(loopback, "unsafe_coordinator_address")

    privileged = copy.deepcopy(plan)
    privileged["coordinator"]["port"] = 443
    refresh_authorization(privileged)
    _reject(privileged, "invalid_coordinator_port")
