# SPDX-License-Identifier: AGPL-3.0-only
"""Windows install source for A13 onboarding (source tranche).

The normal-user path: one reviewed, signed Windows installer artifact, its digest
verified locally, installed as a least-privilege Windows service, with no SSH,
Tailscale, Termux, ADB, source checkout, repository knowledge, seed URL,
EndpointID, sidecar, shell, or CLI setup anywhere in the plan.

Verification is a prerequisite of the plan: ``install_plan`` refuses a source
that has not been verified, so target identity can only be created after the
package boundary has been checked.

Per owner ruling 2026-09-27 #4 (no certificate for owner_private_media), the
pilot ``owner_private_media`` channel carries no Authenticode publisher
identity: ``publisher_identity.authenticode_subject`` must then equal
``AUTHENTICODE_UNSIGNED_SUBJECT``, an explicit statement of absence rather than
a fabricated ``CN=...`` subject. Integrity still comes from the pinned Ed25519
verification key. ``signed_update_channel`` is unaffected and still requires a
real Authenticode subject.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from mycelium_a13_packaging.install_source import (
    CHANNELS,
    EXPERT_DEPENDENCIES,
    InstallSourceError,
    InstallSourceProfile,
    check_floor_and_artifact,
    check_time_and_publisher,
    dotted_version,
    fail as _fail,
    plan as _plan,
    require_verified,
    validate_install_source,
)

from .platform_admission import ADMITTED_ARCHITECTURES, ADMITTED_RELEASES

PROTOCOL = "mycelium.a13_windows_install_source.v1"
PLAN_PROTOCOL = "mycelium.a13_windows_install_plan.v1"

INSTALL_MODES: frozenset[str] = frozenset(
    {"windows_service", "windows_service_per_user"}
)

#: Owner ruling 2026-09-27 #4 (no certificate for owner_private_media): the pilot
#: channel ships with no Authenticode code-signing identity at all.  A declared
#: source on that channel must carry this explicit sentinel rather than a
#: fabricated ``CN=...`` subject, so the absence is a stated fact, never a lie of
#: omission.  ``signed_update_channel`` still requires a real Authenticode
#: subject -- a purchased certificate is only reconsidered before any public
#: Windows distribution.
AUTHENTICODE_UNSIGNED_SUBJECT = "authenticode-unsigned:owner_private_media"

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
        "windows_version",
        "windows_build",
        "service_manager",
        "install_mode",
        "rollback_floor",
        "lifecycle_support",
        "expert_tools_required",
    }
)



def _platform_version(value: object) -> tuple[int, ...]:
    return dotted_version(value, code="install_source_platform_unsupported")


def _validate_authenticode(value: Mapping, publisher: Mapping) -> None:
    subject = publisher["authenticode_subject"]
    if value["channel"] == "owner_private_media":
        # Owner ruling 2026-09-27 #4: no certificate for owner_private_media.
        if subject != AUTHENTICODE_UNSIGNED_SUBJECT:
            _fail("install_source_publisher_invalid")
    else:
        if (
            not isinstance(subject, str)
            or not subject
            or subject != subject.strip()
            or len(subject) > 256
            or subject == AUTHENTICODE_UNSIGNED_SUBJECT
        ):
            _fail("install_source_publisher_invalid")


PROFILE = InstallSourceProfile(
    protocol=PROTOCOL,
    fields=FIELDS,
    package_class="signed_windows_installer",
    service_manager=ADMITTED_RELEASES["windows"]["service_manager"],
    install_modes=INSTALL_MODES,
    admitted_architectures=frozenset(ADMITTED_ARCHITECTURES),
    extra_publisher_fields=frozenset({"authenticode_subject"}),
    validate_publisher_extra=_validate_authenticode,
    refuse_backslash_in_artifact_name=True,
)


def validate_windows_install_source(source: object) -> dict:
    """Closed-shape validation of one declared Windows install source."""

    return validate_install_source(source, PROFILE)


def verify_windows_install_source(
    source: object,
    *,
    expected_publisher: str,
    expected_authenticode_subject: str,
    expected_architecture: str,
    expected_windows_version: str,
    expected_windows_build: str,
    installed_version: str,
    rollback_floor_pin: str,
    now: float,
    artifact_path: Path | None = None,
) -> dict:
    """Verify the declared Windows source against the local boundary and artifact."""

    value = validate_windows_install_source(source)
    publisher = check_time_and_publisher(value, now=now, expected_publisher=expected_publisher)
    if publisher["authenticode_subject"] != expected_authenticode_subject:
        _fail("install_source_publisher_mismatch")
    declared = ADMITTED_RELEASES["windows"]
    floor_version = tuple(int(part) for part in declared["minimum_version"].split("."))
    offered = _platform_version(value["windows_version"])
    if (
        value["platform_architecture"] != expected_architecture
        or value["platform_architecture"] not in ADMITTED_ARCHITECTURES
        or offered < floor_version
        or value["windows_version"] != expected_windows_version
        or value["windows_build"] != expected_windows_build
        or not isinstance(value["windows_build"], str)
        or not value["windows_build"].isdigit()
        or int(value["windows_build"]) < int(declared["minimum_build"])
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
        "authenticode_subject": publisher["authenticode_subject"],
        "platform_architecture": value["platform_architecture"],
        "windows_version": value["windows_version"],
        "windows_build": value["windows_build"],
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
    per_user = verified.get("install_mode") == "windows_service_per_user"
    register = (
        {"action": "register_per_user_logon_task_opt_in", "requires": "windows_service_per_user_member"}
        if per_user
        else {"action": "register_least_privilege_windows_service", "requires": "windows_service_descriptor"}
    )
    start = (
        {"action": "open_app_and_check_loopback_ui", "requires": "per_user_installation"}
        if per_user
        else {"action": "start_service_and_check_loopback_health", "requires": "service_lifecycle"}
    )
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
            "action": "install_windows_installer_package",
            "requires": "signed_windows_installer",
        },
        {"index": 4, **register},
        {"index": 5, **start},
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
    "AUTHENTICODE_UNSIGNED_SUBJECT",
    "CHANNELS",
    "EXPERT_DEPENDENCIES",
    "FIELDS",
    "INSTALL_MODES",
    "PLAN_PROTOCOL",
    "PROTOCOL",
    "InstallSourceError",
    "install_plan",
    "validate_windows_install_source",
    "verify_windows_install_source",
]
