"""A13 Windows least-privilege service lifecycle (source tranche).

The Windows member is a closed *declaration* here: the repository's shared
service renderer emits launchd and systemd members only, so this tranche refuses
to fabricate a Windows service member and pins that refusal.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from mycelium_a13_windows.service_lifecycle import (
    LIFECYCLE_OUTCOMES,
    LIFECYCLE_STATES,
    PROTOCOL,
    REQUIRED_SERVICE_FIELDS,
    ServiceLifecycleError,
    uninstall_plan,
    validate_least_privilege_windows_service,
    windows_service_member,
    windows_service_member_from_shared_renderer,
)

INVENTORY = Path(__file__).with_name("inventory.v1.json")
NOW = 2_000_000_000.0


def _descriptor(**overrides):
    values = {
        "protocol": PROTOCOL,
        "service_name": "mycelium-win-1-agent",
        "display_name": "Mycelium agent",
        "start_type": "manual",
        "account_class": "least_privilege_service_account",
        "launch_privilege": "as_invoker_least_privilege",
        "restricted_token": True,
        "public_listener": False,
        "environment": {"MYCELIUM_ROLE": "node"},
        "install_mode": "windows_service",
        "restart_policy": {
            "restart_limit": 3,
            "restart_delay_seconds": 5,
            "stop_timeout_seconds": 30,
        },
    }
    values.update(overrides)
    return values


def _code(call):
    with pytest.raises(ServiceLifecycleError) as caught:
        call()
    return caught.value.code


def test_lifecycle_states_match_the_frozen_a13_inventory_in_order() -> None:
    inventory = json.loads(INVENTORY.read_text("utf-8"))
    declared = inventory["managed_lifecycle_states"]
    assert list(LIFECYCLE_STATES) == list(declared)
    assert len(LIFECYCLE_STATES) == 15
    assert set(LIFECYCLE_OUTCOMES) == set(LIFECYCLE_STATES)
    for outcome in LIFECYCLE_OUTCOMES.values():
        assert outcome and outcome == outcome.strip().lower()


def test_a_compliant_least_privilege_descriptor_is_accepted() -> None:
    validated = validate_least_privilege_windows_service(_descriptor())
    assert validated["account_class"] == "least_privilege_service_account"
    assert validated["restricted_token"] is True
    assert validated["public_listener"] is False
    assert set(validated) == set(REQUIRED_SERVICE_FIELDS)


def test_privilege_escalation_is_refused() -> None:
    for account_class in ("local_system", "administrators", "network_service", "LocalSystem"):
        assert (
            _code(lambda: validate_least_privilege_windows_service(
                _descriptor(account_class=account_class)
            ))
            == "windows_service_privilege_escalation"
        )
    assert (
        _code(lambda: validate_least_privilege_windows_service(
            _descriptor(launch_privilege="run_as_highest")
        ))
        == "windows_service_privilege_escalation"
    )
    assert (
        _code(lambda: validate_least_privilege_windows_service(
            _descriptor(restricted_token=False)
        ))
        == "windows_service_privilege_escalation"
    )


def test_public_listener_and_secret_environment_are_refused() -> None:
    assert (
        _code(lambda: validate_least_privilege_windows_service(
            _descriptor(public_listener=True)
        ))
        == "windows_service_public_listener_forbidden"
    )
    for key in ("MYCELIUM_INVITE", "SEED_PRIVATE_KEY", "API_KEY", "OWNER_CREDENTIAL"):
        assert (
            _code(lambda: validate_least_privilege_windows_service(
                _descriptor(environment={key: "x"})
            ))
            == "windows_service_environment_secret"
        )


def test_shape_install_mode_and_hardening_are_enforced() -> None:
    assert (
        _code(lambda: validate_least_privilege_windows_service({**_descriptor(), "extra": 1}))
        == "windows_service_invalid"
    )
    missing = {
        key: value
        for key, value in _descriptor().items()
        if key != "restart_policy"
    }
    assert (
        _code(lambda: validate_least_privilege_windows_service(missing))
        == "windows_service_invalid"
    )
    assert (
        _code(lambda: validate_least_privilege_windows_service(
            _descriptor(install_mode="systemd_user")
        ))
        == "windows_service_install_mode_invalid"
    )
    assert (
        _code(lambda: validate_least_privilege_windows_service(
            _descriptor(start_type="disabled")
        ))
        == "windows_service_hardening_incomplete"
    )
    assert (
        _code(lambda: validate_least_privilege_windows_service(
            _descriptor(restart_policy={
                "restart_limit": 0,
                "restart_delay_seconds": 5,
                "stop_timeout_seconds": 30,
            })
        ))
        == "windows_service_hardening_incomplete"
    )


def test_windows_service_member_reports_bounded_facts_only() -> None:
    member = windows_service_member(_descriptor())
    assert member["protocol"] == PROTOCOL
    assert member["service_name"] == "mycelium-win-1-agent"
    assert member["install_mode"] == "windows_service"
    assert member["account_class"] == "least_privilege_service_account"
    assert member["restricted_token"] is True
    assert member["public_listener"] is False
    assert member["identity_or_eligibility_claim"] is False
    assert member["lifecycle"] == dict(LIFECYCLE_OUTCOMES)
    rendered = json.dumps(member).lower()
    for forbidden in ("local_system", "qualified", "password"):
        assert forbidden not in rendered


def test_the_shared_renderer_has_no_windows_member_and_is_refused() -> None:
    member = windows_service_member_from_shared_renderer(_descriptor())
    assert member["renderer"] == "shared_service_package"
    assert member["windows_member_absent"] is True
    assert member["declared_members"] == ["launchd", "systemd"]
    assert member["windows_member_rendered"] is False


def test_uninstall_never_claims_a_coordinator_revocation() -> None:
    plan = uninstall_plan(
        retain_identity=True, reachable=True, generation_valid=True, lease_valid=True
    )
    assert plan["drain_requested"] is True
    assert plan["admission_stopped"] is True
    assert plan["coordinator_revocation_claimed"] is False
    assert plan["owner_revoke_instruction_required"] is True
    assert plan["identity_policy"] == "retained"
    assert plan["retained_identity_resume_allowed"] is True
    assert plan["route_ready"] is False

    offline = uninstall_plan(
        retain_identity=False, reachable=False, generation_valid=False, lease_valid=False
    )
    assert offline["drain_requested"] is False
    assert offline["identity_policy"] == "deleted"
    assert offline["retained_identity_resume_allowed"] is False
    assert offline["coordinator_revocation_claimed"] is False

    stale = uninstall_plan(
        retain_identity=True, reachable=False, generation_valid=False, lease_valid=True
    )
    assert stale["retained_identity_resume_allowed"] is False

    assert _code(lambda: uninstall_plan(
        retain_identity="yes", reachable=True, generation_valid=True, lease_valid=True
    )) == "windows_service_uninstall_plan_invalid"
