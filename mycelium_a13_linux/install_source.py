# SPDX-License-Identifier: AGPL-3.0-only
"""Linux install source for A13 onboarding (source tranche).

The normal-user path: one reviewed, signed Linux artifact, its digest verified
locally, installed as a hardened systemd *user* service, with no SSH, Tailscale,
Termux, ADB, source checkout, repository knowledge, seed URL, EndpointID,
sidecar, shell, or CLI setup anywhere in the plan.

Verification is a prerequisite of the plan: ``install_plan`` refuses a source
that has not been verified, so target identity can only be created after the
package boundary has been checked.
"""

from __future__ import annotations

from pathlib import Path

from mycelium_a13_packaging.install_source import (
    CHANNELS,
    EXPERT_DEPENDENCIES,
    InstallSourceError,
    InstallSourceProfile,
    check_floor_and_artifact,
    check_time_and_publisher,
    dotted_version as _version,
    fail as _fail,
    plan as _plan,
    require_verified,
    validate_install_source as _validate,
)

from .platform_admission import ADMITTED_ARCHITECTURES, ADMITTED_DISTRIBUTIONS

PROTOCOL = "mycelium.a13_linux_install_source.v1"
PLAN_PROTOCOL = "mycelium.a13_linux_install_plan.v1"

INSTALL_MODES: frozenset[str] = frozenset({"systemd_user", "systemd_system"})

FIELDS: frozenset[str] = frozenset(
    {
        "protocol",
        "channel",
        "package_class",
        "artifact_name",
        "artifact_digest",
        "publisher_identity",
        "minimum_os",
        "platform_architecture",
        "distribution_id",
        "distribution_version",
        "service_manager",
        "install_mode",
        "rollback_floor",
        "lifecycle_support",
        "expert_tools_required",
    }
)


PROFILE = InstallSourceProfile(
    protocol=PROTOCOL,
    fields=FIELDS,
    package_class="signed_linux_package",
    service_manager="systemd",
    install_modes=INSTALL_MODES,
    admitted_architectures=frozenset(ADMITTED_ARCHITECTURES),
)


def validate_install_source(source: object) -> dict:
    """Closed-shape validation of one declared Linux install source."""

    return _validate(source, PROFILE)


def verify_install_source(
    source: object,
    *,
    expected_publisher: str,
    expected_architecture: str,
    expected_distribution: str,
    installed_version: str,
    rollback_floor_pin: str,
    now: float,
    artifact_path: Path | None = None,
) -> dict:
    """Verify the declared source against the local boundary and the artifact."""

    value = validate_install_source(source)
    publisher = check_time_and_publisher(value, now=now, expected_publisher=expected_publisher)
    declared = ADMITTED_DISTRIBUTIONS.get(value["distribution_id"])
    try:
        offered = _version(value["distribution_version"])
        floor = None if declared is None else _version(declared["minimum_version"])
    except InstallSourceError:
        _fail("install_source_platform_unsupported")
    if (
        declared is None
        or floor is None
        or offered < floor
        or value["platform_architecture"] != expected_architecture
        or value["distribution_id"] != expected_distribution
    ):
        _fail("install_source_platform_unsupported")
    artifact_verified = check_floor_and_artifact(
        value, rollback_floor_pin=rollback_floor_pin, installed_version=installed_version,
        artifact_path=artifact_path)
    return {
        "verified": True,
        "protocol": PROTOCOL,
        "channel": value["channel"],
        "artifact_name": value["artifact_name"],
        "artifact_digest": value["artifact_digest"],
        "artifact_verified": artifact_verified,
        "publisher_id": publisher["publisher_id"],
        "platform_architecture": value["platform_architecture"],
        "distribution_id": value["distribution_id"],
        "install_mode": value["install_mode"],
        "rollback_floor": value["rollback_floor"],
        "update_floor_ok": True,
        "identity_creation_allowed": True,
        "eligibility": "member_only",
        "activation": "none",
        "inference_eligibility": False,
        "route_ready": False,
    }


def install_plan(verified: object) -> dict:
    """The bounded normal-user install plan; requires a verified source."""

    require_verified(verified)
    steps = [
        {
            "index": 1,
            "action": "verify_artifact_digest",
            "requires": "artifact_digest",
        },
        {
            "index": 2,
            "action": "verify_publisher_signature",
            "requires": "installation_package_manifest",
        },
        {
            "index": 3,
            "action": "install_systemd_unit",
            "requires": "hardened_systemd_unit",
        },
        {"index": 4, "action": "enable_user_service", "requires": "systemd_user"},
        {
            "index": 5,
            "action": "start_service_and_check_loopback_health",
            "requires": "service_lifecycle",
        },
        {
            "index": 6,
            "action": "create_target_identity",
            "requires": "verified_package",
        },
        {
            "index": 7,
            "action": "create_pairing_request",
            "requires": "target_identity",
        },
    ]
    return _plan(PLAN_PROTOCOL, steps)


__all__ = [
    "CHANNELS",
    "EXPERT_DEPENDENCIES",
    "FIELDS",
    "INSTALL_MODES",
    "PLAN_PROTOCOL",
    "PROTOCOL",
    "InstallSourceError",
    "install_plan",
    "validate_install_source",
    "verify_install_source",
]
