# SPDX-License-Identifier: AGPL-3.0-only
"""Hardened Linux service lifecycle for the A13 installed agent (source tranche).

The systemd unit is produced by the repository's existing deterministic renderer
(``mycelium_service_package``), so the Linux tranche adds no second packaging
engine.  What this module adds is the Linux-specific boundary:

* the systemd member is the shipped member (the launchd member is inert here);
* every required hardening directive must be present;
* no root execution, no public listener, and no secret-bearing environment name;
* the fifteen managed lifecycle states of the frozen A13 acceptance inventory,
  each with one bounded public outcome;
* an uninstall decision that never claims a coordinator revocation.
"""

from __future__ import annotations

from collections.abc import Mapping
import hashlib
import re

from mycelium_service_package import (
    ServicePackageError,
    build_service_package,
)

PROTOCOL = "mycelium.a13_linux_service_lifecycle.v1"

#: The frozen A13 acceptance inventory's ``managed_lifecycle_states``, in order.
LIFECYCLE_STATES: tuple[str, ...] = (
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
)

#: One bounded public outcome per state: what the user is told, and nothing else.
LIFECYCLE_OUTCOMES: dict[str, str] = {
    "install": "signed_package_installed_for_the_declared_linux_platform",
    "lease_renewal": "lease_renewed_or_explicitly_blocked_by_current_authority",
    "reconnect": "bootstrap_reconnected_through_the_authenticated_control_plane_only",
    "capability_refresh": "capability_refreshed_and_requalification_requested_when_changed",
    "update": "reviewed_signed_artifact_installed_after_digest_and_publisher_check",
    "rollback": "previous_digest_pinned_package_restored_or_the_change_refused",
    "restart": "bounded_restart_inside_the_persistent_restart_budget",
    "suspend": "admission_stopped_and_in_flight_work_drained_locally",
    "network_loss": "control_plane_loss_reported_without_claiming_revocation",
    "low_storage": "artifact_acquisition_refused_before_exceeding_the_consent_budget",
    "revoked_membership": "admission_stopped_and_request_owned_resources_cleaned",
    "drain": "in_flight_work_finished_or_bounded_abandonment_reported",
    "uninstall": "service_removed_and_identity_policy_applied_per_explicit_choice",
    "retained_identity_reinstall": "resume_allowed_only_while_generation_and_lease_remain_valid",
    "removed_identity_reenrollment": "reenrollment_requires_a_new_owner_minted_invitation",
}

#: Directives the shipped systemd member must carry.
REQUIRED_HARDENING: tuple[str, ...] = (
    "Type=simple",
    "Restart=on-failure",
    "RestartSec=",
    "TimeoutStopSec=",
    "StartLimitBurst=",
    "NoNewPrivileges=true",
    "UMask=0077",
    "RestrictSUIDSGID=true",
    "LockPersonality=true",
    "StandardOutput=journal",
    "StandardError=journal",
)

_FORBIDDEN_PRIVILEGE: tuple[str, ...] = (
    "User=root",
    "Group=root",
    "NoNewPrivileges=false",
    "RestrictSUIDSGID=false",
    "LockPersonality=false",
)
_FORBIDDEN_PUBLIC_LISTENER: tuple[str, ...] = (
    "0.0.0.0",
    "[::]",
    "--public",
    "ListenStream=0.0.0.0",
)
_SECRET_ENVIRONMENT = re.compile(
    r"(?:TOKEN|SECRET|PASSWORD|PRIVATE|CREDENTIAL|API_KEY|INVITE)"
)
_ENVIRONMENT_LINE = re.compile(r'^Environment=(?:"?)([A-Z][A-Z0-9_]{0,63})=')
_INSTALL_MODES = frozenset({"systemd_user", "systemd_system"})


class ServiceLifecycleError(ValueError):
    """Fail-closed Linux lifecycle error carrying a stable code."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def validate_hardened_systemd_unit(raw: object) -> dict:
    """Check one systemd unit against the Linux hardening boundary."""

    if not isinstance(raw, str) or not raw or "\x00" in raw:
        raise ServiceLifecycleError("service_unit_invalid")
    # Semantic violations are reported before a missing directive, so a unit that
    # actively escalates, listens publicly, or carries a secret is never described
    # as merely "incomplete".
    for pattern in _FORBIDDEN_PRIVILEGE:
        if pattern in raw:
            raise ServiceLifecycleError("service_privilege_escalation")
    for pattern in _FORBIDDEN_PUBLIC_LISTENER:
        if pattern in raw:
            raise ServiceLifecycleError("service_public_listener_forbidden")
    for line in raw.splitlines():
        match = _ENVIRONMENT_LINE.match(line.strip())
        if match is not None and _SECRET_ENVIRONMENT.search(match.group(1)):
            raise ServiceLifecycleError("service_environment_secret")
    for directive in REQUIRED_HARDENING:
        if directive not in raw:
            raise ServiceLifecycleError("service_hardening_incomplete")
    return {"directives": sorted(REQUIRED_HARDENING), "unit_bytes": len(raw.encode("utf-8"))}


def build_linux_service_package(
    config: Mapping[str, object],
    *,
    package_root,
    install_mode: str = "systemd_user",
) -> dict:
    """Render the deterministic package and ship its hardened systemd member."""

    if install_mode not in _INSTALL_MODES:
        raise ServiceLifecycleError("service_install_mode_invalid")
    try:
        # A systemd *user* unit must not order on the system-only network-online.target.
        package = build_service_package(config, package_root=package_root,
                                        systemd_user_unit=install_mode == "systemd_user")
    except ServicePackageError as error:  # shared renderer refusal, same vocabulary
        raise ServiceLifecycleError(str(error)) from None
    unit_name = f"mycelium-{package['service_id']}.service"
    raw = package["_payloads"][unit_name]
    report = validate_hardened_systemd_unit(raw.decode("utf-8"))
    return {
        "protocol": PROTOCOL,
        "service_id": package["service_id"],
        "package_version": package["package_version"],
        "role": package["role"],
        "install_mode": install_mode,
        "systemd_unit": {
            "name": unit_name,
            "sha256": "sha256:" + hashlib.sha256(raw).hexdigest(),
            "bytes": len(raw),
            "text": raw.decode("utf-8"),
        },
        "launchd_member_inert": True,
        "hardening": report["directives"],
        "health": {"url": package["health"]["url"], "loopback_only": True},
        "lifecycle": dict(LIFECYCLE_OUTCOMES),
        "identity_or_eligibility_claim": False,
    }


def uninstall_plan(
    *,
    retain_identity: bool,
    reachable: bool,
    generation_valid: bool,
    lease_valid: bool,
) -> dict:
    """Bounded uninstall decision; never claims a coordinator revocation."""

    for value in (retain_identity, reachable, generation_valid, lease_valid):
        if not isinstance(value, bool):
            raise ServiceLifecycleError("service_uninstall_plan_invalid")
    drained = reachable
    return {
        "protocol": PROTOCOL,
        "drain_requested": drained,
        "admission_stopped": True,
        "request_owned_resources_cleaned": True,
        # Revocation is an owner action on the coordinator: an uninstall, online
        # or offline, cannot claim it.
        "coordinator_revocation_claimed": False,
        "owner_revoke_instruction_required": True,
        "identity_policy": "retained" if retain_identity else "deleted",
        "retained_identity_resume_allowed": bool(
            retain_identity and generation_valid and lease_valid
        ),
        "route_ready": False,
    }


__all__ = [
    "LIFECYCLE_OUTCOMES",
    "LIFECYCLE_STATES",
    "PROTOCOL",
    "REQUIRED_HARDENING",
    "ServiceLifecycleError",
    "build_linux_service_package",
    "uninstall_plan",
    "validate_hardened_systemd_unit",
]
