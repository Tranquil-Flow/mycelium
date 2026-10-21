# SPDX-License-Identifier: AGPL-3.0-only
"""A13 Linux managed service lifecycle — hardened systemd unit, bounded outcomes.

The lifecycle state set is cross-checked against the frozen A13 acceptance
inventory (read-only); the hardened unit is produced by the repository's
existing deterministic systemd renderer (``mycelium_service_package``).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from mycelium_a13_linux.service_lifecycle import (
    LIFECYCLE_OUTCOMES,
    LIFECYCLE_STATES,
    PROTOCOL,
    REQUIRED_HARDENING,
    ServiceLifecycleError,
    build_linux_service_package,
    uninstall_plan,
    validate_hardened_systemd_unit,
)

INVENTORY = Path(__file__).with_name("inventory.v1.json")


def _config(package_root: Path, **overrides):
    values = {
        "protocol": "mycelium.service_package.v1",
        # The shared renderer's id grammar requires a lowercase-letter prefix.
        "package_version": "v0.1.0",
        "service_id": "node2",
        "role": "node",
        "argv": ["/usr/bin/python3.12"],
        "working_directory": str(package_root),
        "state_directory": str(package_root / "state"),
        "log_directory": str(package_root / "log"),
        "environment": {"MYCELIUM_ROLE": "node"},
        "health_url": "http://127.0.0.1:14351/health",
        "restart_limit": 5,
        "restart_window_seconds": 300,
        "restart_delay_seconds": 5,
        "stop_timeout_seconds": 30,
    }
    values.update(overrides)
    return values


def _code(call):
    with pytest.raises(ServiceLifecycleError) as caught:
        call()
    return caught.value.code


def test_lifecycle_states_match_the_frozen_a13_acceptance_inventory() -> None:
    document = json.loads(INVENTORY.read_text("utf-8"))
    assert list(LIFECYCLE_STATES) == list(document["managed_lifecycle_states"])
    assert len(LIFECYCLE_STATES) == 15


def test_every_lifecycle_state_has_one_bounded_public_outcome() -> None:
    assert set(LIFECYCLE_OUTCOMES) == set(LIFECYCLE_STATES)
    outcomes = list(LIFECYCLE_OUTCOMES.values())
    assert len(set(outcomes)) == len(outcomes)
    for state, outcome in LIFECYCLE_OUTCOMES.items():
        assert isinstance(outcome, str) and 0 < len(outcome) <= 200, state
        lowered = outcome.lower()
        for forbidden in ("ssh", "tailscale", "password", "token", "/users/", "http://", "https://"):
            assert forbidden not in lowered, (state, forbidden)


def test_built_systemd_unit_carries_every_required_hardening_directive(tmp_path: Path) -> None:
    package = build_linux_service_package(_config(tmp_path), package_root=tmp_path)
    assert package["protocol"] == PROTOCOL
    unit = package["systemd_unit"]
    assert unit["name"] == "mycelium-node2.service"
    text = unit["text"]
    for directive in REQUIRED_HARDENING:
        assert directive in text, directive
    assert package["install_mode"] == "systemd_user"
    assert package["hardening"] == sorted(REQUIRED_HARDENING)
    # The macOS member of the shared package is inert on Linux and says so.
    assert package["launchd_member_inert"] is True
    assert package["identity_or_eligibility_claim"] is False


def test_built_unit_never_runs_as_root_or_listens_publicly(tmp_path: Path) -> None:
    package = build_linux_service_package(_config(tmp_path), package_root=tmp_path)
    text = package["systemd_unit"]["text"]
    assert "User=root" not in text
    assert "0.0.0.0" not in text
    assert package["health"]["loopback_only"] is True
    assert package["health"]["url"] == "http://127.0.0.1:14351/health"


def test_unit_missing_a_hardening_directive_is_rejected(tmp_path: Path) -> None:
    package = build_linux_service_package(_config(tmp_path), package_root=tmp_path)
    weakened = package["systemd_unit"]["text"].replace("LockPersonality=true\n", "")
    assert _code(lambda: validate_hardened_systemd_unit(weakened)) == (
        "service_hardening_incomplete"
    )


def test_unit_that_escalates_privilege_is_rejected(tmp_path: Path) -> None:
    package = build_linux_service_package(_config(tmp_path), package_root=tmp_path)
    text = package["systemd_unit"]["text"]
    assert _code(lambda: validate_hardened_systemd_unit(text + "User=root\n")) == (
        "service_privilege_escalation"
    )
    assert _code(lambda: validate_hardened_systemd_unit(text.replace(
        "NoNewPrivileges=true", "NoNewPrivileges=false"
    ))) == "service_privilege_escalation"


def test_unit_that_opens_a_public_listener_is_rejected(tmp_path: Path) -> None:
    package = build_linux_service_package(_config(tmp_path), package_root=tmp_path)
    text = package["systemd_unit"]["text"] + "ExecStartPre=/usr/bin/nc -l 0.0.0.0 14351\n"
    assert _code(lambda: validate_hardened_systemd_unit(text)) == (
        "service_public_listener_forbidden"
    )


def test_unit_carrying_environment_secret_names_is_rejected(tmp_path: Path) -> None:
    package = build_linux_service_package(_config(tmp_path), package_root=tmp_path)
    text = package["systemd_unit"]["text"] + 'Environment="AUTH_TOKEN=abc"\n'
    assert _code(lambda: validate_hardened_systemd_unit(text)) == (
        "service_environment_secret"
    )


def test_non_systemd_install_modes_are_rejected(tmp_path: Path) -> None:
    assert _code(lambda: build_linux_service_package(
        _config(tmp_path), package_root=tmp_path, install_mode="launchd"
    )) == "service_install_mode_invalid"
    assert _code(lambda: build_linux_service_package(
        _config(tmp_path, health_url="http://0.0.0.0:14351/health"), package_root=tmp_path
    )) == "service_health_url_invalid"


def test_online_uninstall_drains_but_never_claims_coordinator_revocation() -> None:
    plan = uninstall_plan(retain_identity=True, reachable=True, generation_valid=True, lease_valid=True)
    assert plan["protocol"] == PROTOCOL
    assert plan["drain_requested"] is True
    assert plan["admission_stopped"] is True
    assert plan["request_owned_resources_cleaned"] is True
    assert plan["coordinator_revocation_claimed"] is False
    assert plan["owner_revoke_instruction_required"] is True
    assert plan["identity_policy"] == "retained"
    assert plan["retained_identity_resume_allowed"] is True
    assert plan["route_ready"] is False


def test_offline_uninstall_cannot_claim_revocation() -> None:
    plan = uninstall_plan(retain_identity=True, reachable=False, generation_valid=True, lease_valid=True)
    assert plan["drain_requested"] is False
    assert plan["coordinator_revocation_claimed"] is False
    assert plan["owner_revoke_instruction_required"] is True


@pytest.mark.parametrize(
    "generation_valid,lease_valid", [(False, True), (True, False), (False, False)]
)
def test_retained_identity_resumes_only_with_a_valid_generation_and_lease(
    generation_valid: bool, lease_valid: bool
) -> None:
    plan = uninstall_plan(
        retain_identity=True,
        reachable=True,
        generation_valid=generation_valid,
        lease_valid=lease_valid,
    )
    assert plan["retained_identity_resume_allowed"] is False
    assert plan["identity_policy"] == "retained"


def test_deleted_identity_never_resumes() -> None:
    plan = uninstall_plan(retain_identity=False, reachable=True, generation_valid=True, lease_valid=True)
    assert plan["identity_policy"] == "deleted"
    assert plan["retained_identity_resume_allowed"] is False


def test_uninstall_plan_flags_must_be_booleans() -> None:
    assert _code(lambda: uninstall_plan(
        retain_identity="yes", reachable=True, generation_valid=True, lease_valid=True
    )) == "service_uninstall_plan_invalid"
    assert _code(lambda: uninstall_plan(
        retain_identity=True, reachable=True, generation_valid=True, lease_valid=None
    )) == "service_uninstall_plan_invalid"
