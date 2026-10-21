# SPDX-License-Identifier: AGPL-3.0-only
"""Closed ``mycelium.installation_package_manifest.v1`` for Linux (A13).

One signed record binds every package identity input the A13 specification and
its frozen acceptance inventory require -- source revision, build inputs,
platform/architecture, package digest, publisher identity, minimum OS, service
descriptor, update channel, rollback floor, capability protocol, privacy notice,
and expiry/revocation policy -- and verification fails closed on unsupported
platform/architecture, invalid signature, expiry, a publisher key that is not the
pinned one, a downgrade below the rollback floor, missing lifecycle support, and
any embedded invitation/credential/model/peer/topology material.

Verification returns member-only facts.  It never returns ``route_ready``,
never grants eligibility, and never claims that a package was installed or
exercised.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from mycelium_a13_packaging.manifest import (
    BINDINGS,
    PROTOCOL,
    ManifestProfile,
    PackageManifestError,
    build_manifest,
    check_rollback_floor,
    dotted_version as _version,
    fail as _fail,
    is_segment as _segment,
    validate_manifest,
    verify_signed,
)

from .platform_admission import ADMITTED_ARCHITECTURES, ADMITTED_DISTRIBUTIONS


def _validate_platform(platform_architecture: Mapping[str, Any]) -> None:
    if not all(
        _segment(platform_architecture[field])
        for field in ("system", "machine", "distribution_id", "distribution_version")
    ):
        _fail("package_platform_invalid")


PROFILE = ManifestProfile(
    platform_fields=frozenset({"system", "machine", "distribution_id", "distribution_version"}),
    validate_platform=_validate_platform,
    extra_publisher_fields=frozenset(),
    validate_publisher_extra=lambda _publisher: None,
    descriptor_name_field="unit_name",
    install_modes=frozenset({"systemd_user", "systemd_system"}),
)


def validate_linux_package_manifest(document: object) -> dict[str, Any]:
    """Closed-shape validation of one installed-package manifest."""

    return validate_manifest(document, PROFILE)


def build_linux_package_manifest(
    *, signer, issued_at, expires_at, **bindings: Any
) -> dict[str, Any]:
    """Assemble and sign one manifest from the twelve frozen bindings."""

    return build_manifest(PROFILE, signer=signer, issued_at=issued_at, expires_at=expires_at, **bindings)


def verify_linux_package(
    document: object,
    *,
    expected_publisher: str,
    expected_architecture: str,
    expected_distribution: str,
    installed_version: str,
    rollback_floor_pin: str,
    publisher_key_digest_pin: str,
    now: float,
) -> dict[str, Any]:
    """Verify one offered Linux package; return member-only facts or refuse."""

    value = validate_linux_package_manifest(document)
    publisher = verify_signed(value, expected_publisher=expected_publisher,
                              publisher_key_digest_pin=publisher_key_digest_pin, now=now)
    platform_architecture = value["platform_architecture"]
    declared = ADMITTED_DISTRIBUTIONS.get(platform_architecture["distribution_id"])
    try:
        offered = _version(platform_architecture["distribution_version"])
        floor = (
            None if declared is None else _version(declared["minimum_version"])
        )
    except PackageManifestError:
        _fail("package_platform_unsupported")
    if (
        platform_architecture["system"] != "linux"
        or declared is None
        or floor is None
        or offered < floor
        or platform_architecture["machine"] != expected_architecture
        or platform_architecture["machine"] not in ADMITTED_ARCHITECTURES
        or platform_architecture["distribution_id"] != expected_distribution
    ):
        _fail("package_platform_unsupported")
    check_rollback_floor(value, rollback_floor_pin=rollback_floor_pin, installed_version=installed_version)
    return {
        "verified": True,
        "protocol": PROTOCOL,
        "source_revision": value["source_revision"],
        "platform_architecture": platform_architecture["machine"],
        "distribution_id": platform_architecture["distribution_id"],
        "package_digest": value["package_digest"],
        "publisher_id": publisher["publisher_id"],
        "publisher_key_digest": publisher_key_digest_pin,
        "minimum_os": value["minimum_os"],
        "service_descriptor": dict(value["service_descriptor"]),
        "update_channel": value["update_channel"],
        "rollback_floor": value["rollback_floor"],
        "capability_protocol": value["capability_protocol"],
        "identity_creation_allowed": True,
        "eligibility": "member_only",
        "activation": "none",
        "route_ready": False,
    }


__all__ = [
    "BINDINGS",
    "PROTOCOL",
    "PackageManifestError",
    "build_linux_package_manifest",
    "validate_linux_package_manifest",
    "verify_linux_package",
]
