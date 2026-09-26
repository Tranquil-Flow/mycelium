# SPDX-License-Identifier: AGPL-3.0-or-later
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

from collections.abc import Mapping
import hashlib
from pathlib import Path
import re

from .platform_admission import ADMITTED_ARCHITECTURES, ADMITTED_DISTRIBUTIONS

PROTOCOL = "mycelium.a13_linux_install_source.v1"
PLAN_PROTOCOL = "mycelium.a13_linux_install_plan.v1"

CHANNELS: frozenset[str] = frozenset({"owner_private_media", "signed_update_channel"})
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

_PUBLISHER_FIELDS = frozenset({"publisher_id", "endpoint_id", "verification_key_record"})
_PACKAGE_CLASS = "signed_linux_package"
#: The A13 normal-user exclusions.  Any declared expert dependency refuses.
EXPERT_DEPENDENCIES: frozenset[str] = frozenset(
    {
        "ssh",
        "tailscale",
        "termux",
        "adb",
        "source checkout",
        "repository knowledge",
        "seed url",
        "endpoint id",
        "sidecar",
        "shell",
        "cli setup",
        "manual model copy",
    }
)

_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_SEGMENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_ARTIFACT_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_URL = re.compile(r"[A-Za-z][A-Za-z0-9+.\-]*://")


class InstallSourceError(ValueError):
    """Fail-closed install-source error carrying a stable code."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _fail(code: str) -> None:
    raise InstallSourceError(code)


def _version(value: object) -> tuple[int, ...]:
    if not isinstance(value, str) or not value:
        _fail("install_source_rollback_floor_invalid")
    parts = value.split(".")
    if not all(part.isdigit() for part in parts):
        _fail("install_source_rollback_floor_invalid")
    return tuple(int(part) for part in parts)


def _walk_strings(value: object):
    if isinstance(value, str):
        yield value
    elif isinstance(value, Mapping):
        for item in value.values():
            yield from _walk_strings(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _walk_strings(item)


def validate_install_source(source: object) -> dict:
    """Closed-shape validation of one declared Linux install source."""

    if not isinstance(source, Mapping) or set(source) != FIELDS:
        _fail("install_source_shape_invalid")
    value = dict(source)
    if value["protocol"] != PROTOCOL:
        _fail("install_source_protocol_invalid")
    for text in _walk_strings(value):
        if _URL.search(text) is not None:
            _fail("install_source_hidden_url")
    if value["channel"] not in CHANNELS:
        _fail("install_source_channel_invalid")
    if value["package_class"] != _PACKAGE_CLASS:
        _fail("install_source_package_class_invalid")
    if (
        not isinstance(value["artifact_name"], str)
        or _ARTIFACT_NAME.fullmatch(value["artifact_name"]) is None
        or "/" in value["artifact_name"]
    ):
        _fail("install_source_artifact_invalid")
    if not isinstance(value["artifact_digest"], str) or _DIGEST.fullmatch(
        value["artifact_digest"]
    ) is None:
        _fail("install_source_digest_invalid")
    publisher = value["publisher_identity"]
    if not isinstance(publisher, Mapping) or set(publisher) != _PUBLISHER_FIELDS:
        _fail("install_source_publisher_invalid")
    if not _SEGMENT.fullmatch(str(publisher["publisher_id"])) or not _SEGMENT.fullmatch(
        str(publisher["endpoint_id"])
    ):
        _fail("install_source_publisher_invalid")
    record = publisher["verification_key_record"]
    if (
        not isinstance(record, Mapping)
        or record.get("algorithm") != "ed25519"
        or not _DIGEST.fullmatch(str(record.get("verification_key_digest")))
    ):
        _fail("install_source_publisher_invalid")
    if not _SEGMENT.fullmatch(str(value["minimum_os"])):
        _fail("install_source_minimum_os_invalid")
    if value["platform_architecture"] not in ADMITTED_ARCHITECTURES:
        _fail("install_source_platform_unsupported")
    if value["service_manager"] != "systemd":
        _fail("install_source_lifecycle_support_missing")
    if value["install_mode"] not in INSTALL_MODES:
        _fail("install_source_install_mode_invalid")
    _version(value["rollback_floor"])
    if value["lifecycle_support"] is not True:
        _fail("install_source_lifecycle_support_missing")
    tools = value["expert_tools_required"]
    if (
        not isinstance(tools, (list, tuple))
        or any(not isinstance(item, str) or not item for item in tools)
        or len(tools) != 0
    ):
        _fail("install_source_expert_tools_required")
    return value


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
    if isinstance(now, bool) or not isinstance(now, (int, float)):
        _fail("install_source_time_invalid")
    publisher = value["publisher_identity"]
    if publisher["publisher_id"] != expected_publisher:
        _fail("install_source_publisher_mismatch")
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
    package_floor = _version(value["rollback_floor"])
    pin = _version(rollback_floor_pin)
    installed = _version(installed_version)
    if package_floor < pin or installed < package_floor:
        _fail("install_source_downgrade_rejected")
    artifact_verified = False
    if artifact_path is not None:
        path = Path(artifact_path)
        if path.is_symlink() or not path.is_file():
            _fail("install_source_artifact_unavailable")
        try:
            raw = path.read_bytes()
        except OSError:
            _fail("install_source_artifact_unavailable")
        if "sha256:" + hashlib.sha256(raw).hexdigest() != value["artifact_digest"]:
            _fail("install_source_digest_mismatch")
        artifact_verified = True
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

    if (
        not isinstance(verified, Mapping)
        or verified.get("verified") is not True
        or not isinstance(verified.get("artifact_digest"), str)
    ):
        _fail("install_source_not_verified")
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
    return {
        "protocol": PLAN_PROTOCOL,
        "steps": steps,
        "expert_tools_required": [],
        "public_listener": False,
        "identity_creation_allowed": True,
        "activation": "none",
        "inference_eligibility": False,
        "route_ready": False,
    }


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
