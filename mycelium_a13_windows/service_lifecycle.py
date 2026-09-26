# SPDX-License-Identifier: AGPL-3.0-or-later
"""Least-privilege Windows service lifecycle for the A13 installed agent.

The repository's deterministic service renderer (``mycelium_service_package``)
emits launchd and systemd members only.  This module therefore does *two*
honest things instead of inventing a Windows unit:

* it declares and validates the least-privilege **Windows service descriptor**
  the signed installer would register — own-process service, least-privilege or
  virtual service account, restricted token, no public listener, no
  secret-bearing environment name, bounded restart policy;
* :func:`windows_service_member_from_shared_renderer` proves by calling the shared
  renderer that no Windows member exists and reports that absence, so a Windows
  service member can never be claimed from the launchd/systemd renderer.

It also carries the fifteen managed lifecycle states of the frozen A13
acceptance inventory, each with one bounded public outcome, and an uninstall
decision that never claims a coordinator revocation.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
import re

from mycelium_service_package import (
    PROTOCOL as SHARED_PACKAGE_PROTOCOL,
    ServicePackageError,
    build_service_package,
)

PROTOCOL = "mycelium.a13_windows_service_lifecycle.v1"

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
    "install": "signed_installer_registered_a_least_privilege_windows_service",
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

#: Closed field set of one declared Windows service descriptor.
REQUIRED_SERVICE_FIELDS: frozenset[str] = frozenset(
    {
        "protocol",
        "service_name",
        "display_name",
        "start_type",
        "account_class",
        "launch_privilege",
        "restricted_token",
        "public_listener",
        "environment",
        "install_mode",
        "restart_policy",
    }
)
_RESTART_POLICY_FIELDS = frozenset(
    {"restart_limit", "restart_delay_seconds", "stop_timeout_seconds"}
)

#: The least-privilege account classes this tranche declares.
ACCOUNT_CLASSES: frozenset[str] = frozenset(
    {"least_privilege_service_account", "virtual_service_account"}
)
START_TYPES: frozenset[str] = frozenset({"automatic", "manual"})
_LAUNCH_PRIVILEGE = "as_invoker_least_privilege"
_INSTALL_MODES = frozenset({"windows_service", "windows_service_per_user"})
#: Directives a registered Windows service member must carry.
REQUIRED_HARDENING: tuple[str, ...] = (
    "SERVICE_WIN32_OWN_PROCESS",
    "restricted_token",
    "least_privilege_account",
    "no_public_listener",
    "bounded_restart_budget",
    "loopback_health_check",
)
_SHARED_RENDERER_MEMBERS: tuple[str, ...] = ("launchd", "systemd")

_SEGMENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_ENV = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
_SECRET_ENV = re.compile(r"(?:TOKEN|SECRET|PASSWORD|PRIVATE|CREDENTIAL|API_KEY|INVITE)")


class ServiceLifecycleError(ValueError):
    """Fail-closed Windows lifecycle error carrying a stable code."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def validate_least_privilege_windows_service(descriptor: object) -> dict:
    """Check one declared Windows service against the least-privilege boundary."""

    if not isinstance(descriptor, Mapping) or set(descriptor) != REQUIRED_SERVICE_FIELDS:
        raise ServiceLifecycleError("windows_service_invalid")
    value = dict(descriptor)
    if value["protocol"] != PROTOCOL:
        raise ServiceLifecycleError("windows_service_invalid")
    if not _SEGMENT.fullmatch(str(value["service_name"])) or not isinstance(
        value["display_name"], str
    ) or not value["display_name"]:
        raise ServiceLifecycleError("windows_service_invalid")
    # Semantic violations are reported before a merely missing setting: a
    # descriptor that actively escalates is never described as "incomplete".
    if value["account_class"] not in ACCOUNT_CLASSES:
        raise ServiceLifecycleError("windows_service_privilege_escalation")
    if value["launch_privilege"] != _LAUNCH_PRIVILEGE or value["restricted_token"] is not True:
        raise ServiceLifecycleError("windows_service_privilege_escalation")
    if value["public_listener"] is not False:
        raise ServiceLifecycleError("windows_service_public_listener_forbidden")
    environment = value["environment"]
    if not isinstance(environment, Mapping):
        raise ServiceLifecycleError("windows_service_invalid")
    for key, item in environment.items():
        if (
            not isinstance(key, str)
            or _ENV.fullmatch(key) is None
            or _SECRET_ENV.search(key)
            or not isinstance(item, str)
            or "\x00" in item
            or "\n" in item
        ):
            raise ServiceLifecycleError("windows_service_environment_secret")
    if value["install_mode"] not in _INSTALL_MODES:
        raise ServiceLifecycleError("windows_service_install_mode_invalid")
    if value["start_type"] not in START_TYPES:
        raise ServiceLifecycleError("windows_service_hardening_incomplete")
    policy = value["restart_policy"]
    if not isinstance(policy, Mapping) or set(policy) != _RESTART_POLICY_FIELDS:
        raise ServiceLifecycleError("windows_service_hardening_incomplete")
    for field, minimum in (
        ("restart_limit", 1),
        ("restart_delay_seconds", 1),
        ("stop_timeout_seconds", 1),
    ):
        item = policy[field]
        if isinstance(item, bool) or not isinstance(item, int) or item < minimum:
            raise ServiceLifecycleError("windows_service_hardening_incomplete")
    value["environment"] = dict(environment)
    value["restart_policy"] = dict(policy)
    return value


def windows_service_member(descriptor: object) -> dict:
    """Bounded facts of the declared member; never a qualification or a run."""

    value = validate_least_privilege_windows_service(descriptor)
    return {
        "protocol": PROTOCOL,
        "service_name": value["service_name"],
        "display_name": value["display_name"],
        "install_mode": value["install_mode"],
        "start_type": value["start_type"],
        "account_class": value["account_class"],
        "launch_privilege": value["launch_privilege"],
        "restricted_token": True,
        "public_listener": False,
        "restart_policy": value["restart_policy"],
        "hardening": list(REQUIRED_HARDENING),
        "renderer": "declared_windows_service_descriptor",
        "lifecycle": dict(LIFECYCLE_OUTCOMES),
        "identity_or_eligibility_claim": False,
    }


def _shared_renderer_config(descriptor: Mapping[str, object]) -> dict:
    """A minimal valid config for the repository's shared launchd/systemd renderer."""

    value = validate_least_privilege_windows_service(descriptor)
    service_id = str(value["service_name"])[:64].strip("-") or "mycelium-node"
    if _SEGMENT.fullmatch(service_id) is None:  # pragma: no cover - defensive
        raise ServiceLifecycleError("windows_service_invalid")
    return {
        "protocol": SHARED_PACKAGE_PROTOCOL,
        "package_version": "v0-1-0",
        "service_id": service_id,
        "role": "node",
        "argv": ["/usr/bin/python3", "-m", "mycelium_node.cli"],
        "working_directory": "/var/lib/mycelium",
        "state_directory": "/var/lib/mycelium/state",
        "log_directory": "/var/log/mycelium",
        "environment": dict(value["environment"]),
        "health_url": "http://127.0.0.1:14001/health",
        "restart_limit": int(value["restart_policy"]["restart_limit"]),
        "restart_window_seconds": 60,
        "restart_delay_seconds": int(value["restart_policy"]["restart_delay_seconds"]),
        "stop_timeout_seconds": int(value["restart_policy"]["stop_timeout_seconds"]),
    }


def windows_service_member_from_shared_renderer(
    descriptor: object, *, package_root: Path | None = None
) -> dict:
    """Prove that the shared renderer emits no Windows member, and report it.

    This is a refusal in report form: the tranche declares a Windows service
    descriptor but does not claim a rendered Windows service member, because the
    shared renderer only produces launchd and systemd members.
    """

    config = _shared_renderer_config(descriptor)
    root = Path("/var/lib/mycelium/packages") if package_root is None else Path(package_root)
    try:
        package = build_service_package(config, package_root=root)
    except ServicePackageError as error:  # shared renderer refusal, same vocabulary
        raise ServiceLifecycleError(str(error)) from None
    payloads = [name for name in package["_payloads"]]
    windows_member = next(
        (
            name
            for name in payloads
            if name.lower().endswith((".msi", ".exe", ".xml", ".windows"))
        ),
        None,
    )
    return {
        "protocol": PROTOCOL,
        "renderer": "shared_service_package",
        "declared_members": list(_SHARED_RENDERER_MEMBERS),
        "rendered_members": sorted(payloads),
        "windows_member_absent": windows_member is None,
        "windows_member_rendered": windows_member is not None,
        "declared_windows_service_name": config["service_id"],
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
            raise ServiceLifecycleError("windows_service_uninstall_plan_invalid")
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
    "ACCOUNT_CLASSES",
    "LIFECYCLE_OUTCOMES",
    "LIFECYCLE_STATES",
    "PROTOCOL",
    "REQUIRED_HARDENING",
    "REQUIRED_SERVICE_FIELDS",
    "START_TYPES",
    "ServiceLifecycleError",
    "uninstall_plan",
    "validate_least_privilege_windows_service",
    "windows_service_member",
    "windows_service_member_from_shared_renderer",
]
