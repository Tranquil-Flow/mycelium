# SPDX-License-Identifier: AGPL-3.0-only
"""Closed ``mycelium.installation_package_manifest.v1`` for macOS (A13).

The macOS wrapper over the shared core (``mycelium_a13_packaging.manifest``):
the same envelope, twelve bindings, embedded-material scan, Ed25519 signature,
publisher key pin and rollback floor as Windows and Linux.  macOS-specific:

* the platform block is ``{system: "macos", machine: "arm64", macos_version}``,
  admitted against the app's exact compatibility targets (``ADMITTED_VERSIONS``);
* the publisher carries ``developer_id_team``.  Until the owner rules on Apple
  Developer ID, the pilot carries the explicit sentinel
  :data:`DEVELOPER_ID_UNSIGNED` -- a stated absence, never a fabricated Team ID;
  a real Team ID is exactly ten upper-case letters/digits;
* the service descriptor is a launchd **user** agent (``launchd_label``,
  install mode ``launchd_user``).

Verification returns member-only facts; it never grants eligibility.
"""

from __future__ import annotations

from collections.abc import Mapping
import re
from typing import Any

from mycelium_a13_packaging.manifest import (
    BINDINGS,
    PROTOCOL,
    ManifestProfile,
    PackageManifestError,
    build_manifest,
    check_rollback_floor,
    fail as _fail,
    validate_manifest,
    verify_signed,
)

#: Owner has not ruled on Apple Developer ID (2026-10-07): the pilot is unsigned.
DEVELOPER_ID_UNSIGNED = "developer-id-unsigned:owner_private_media"
#: The exact compatibility targets the app's preflight admits.
ADMITTED_VERSIONS = frozenset({"26.5.1", "15.6.1"})
ADMITTED_ARCHITECTURES = frozenset({"arm64"})
_TEAM_ID = re.compile(r"^[A-Z0-9]{10}$")


def _validate_platform(platform_architecture: Mapping[str, Any]) -> None:
    version = platform_architecture["macos_version"]
    if (
        platform_architecture["system"] != "macos"
        or platform_architecture["machine"] not in ADMITTED_ARCHITECTURES
        or not isinstance(version, str)
        or not re.fullmatch(r"\d+(\.\d+){1,2}", version)
    ):
        _fail("package_platform_invalid")


def _validate_publisher_extra(publisher: Mapping[str, Any]) -> None:
    team = publisher["developer_id_team"]
    if team != DEVELOPER_ID_UNSIGNED and (not isinstance(team, str) or not _TEAM_ID.fullmatch(team)):
        _fail("package_publisher_invalid")


PROFILE = ManifestProfile(
    platform_fields=frozenset({"system", "machine", "macos_version"}),
    validate_platform=_validate_platform,
    extra_publisher_fields=frozenset({"developer_id_team"}),
    validate_publisher_extra=_validate_publisher_extra,
    descriptor_name_field="launchd_label",
    install_modes=frozenset({"launchd_user"}),
)


def validate_macos_package_manifest(document: object) -> dict[str, Any]:
    return validate_manifest(document, PROFILE)


def build_macos_package_manifest(*, signer, issued_at, expires_at, **bindings: Any) -> dict[str, Any]:
    return build_manifest(PROFILE, signer=signer, issued_at=issued_at, expires_at=expires_at, **bindings)


def verify_macos_package(
    document: object,
    *,
    expected_publisher: str,
    expected_developer_id_team: str,
    expected_architecture: str,
    observed_macos_version: str,
    installed_version: str,
    rollback_floor_pin: str,
    publisher_key_digest_pin: str,
    now: float,
) -> dict[str, Any]:
    """Verify one offered macOS package against this Mac; member-only facts or refuse."""

    value = validate_macos_package_manifest(document)
    publisher = verify_signed(value, expected_publisher=expected_publisher,
                              publisher_key_digest_pin=publisher_key_digest_pin, now=now)
    if publisher["developer_id_team"] != expected_developer_id_team:
        _fail("package_publisher_mismatch")
    platform_architecture = value["platform_architecture"]
    if (
        platform_architecture["machine"] != expected_architecture
        or observed_macos_version not in ADMITTED_VERSIONS
    ):
        _fail("package_platform_unsupported")
    check_rollback_floor(value, rollback_floor_pin=rollback_floor_pin, installed_version=installed_version)
    return {
        "verified": True,
        "protocol": PROTOCOL,
        "source_revision": value["source_revision"],
        "platform": "macos",
        "platform_architecture": platform_architecture["machine"],
        "package_digest": value["package_digest"],
        "publisher_id": publisher["publisher_id"],
        "developer_id_team": publisher["developer_id_team"],
        "publisher_key_digest": publisher_key_digest_pin,
        "minimum_os": value["minimum_os"],
        "service_descriptor": dict(value["service_descriptor"]),
        "update_channel": value["update_channel"],
        "rollback_floor": value["rollback_floor"],
        "capability_protocol": value["capability_protocol"],
        "identity_creation_allowed": True,
        "eligibility": "member_only",
        "activation": "none",
        "inference_eligibility": False,
        "route_ready": False,
    }


__all__ = [
    "ADMITTED_VERSIONS",
    "BINDINGS",
    "DEVELOPER_ID_UNSIGNED",
    "PROTOCOL",
    "PackageManifestError",
    "build_macos_package_manifest",
    "validate_macos_package_manifest",
    "verify_macos_package",
]
