# SPDX-License-Identifier: AGPL-3.0-only
"""Closed ``mycelium.installation_package_manifest.v1`` for Windows (A13).

One signed record binds every package identity input the A13 specification and
its frozen acceptance inventory require -- source revision, build inputs,
platform/architecture, package digest, publisher identity, minimum OS, service
descriptor, update channel, rollback floor, capability protocol, privacy notice,
and expiry/revocation policy -- and verification fails closed on unsupported
platform/architecture, invalid signature, expiry, a publisher key or Authenticode
subject that is not the pinned one, a downgrade below the rollback floor, missing
lifecycle support, and any embedded invitation/credential/model/peer/topology
material.

Windows-specific: the publisher identity binds the pinned Ed25519 verification
key, and, when the package is signed for the ``signed_update_channel``, an
Authenticode subject too.  Per owner ruling 2026-09-27 #4 (no certificate for
owner_private_media), the pilot channel's package instead carries the explicit
``mycelium_a13_windows.install_source.AUTHENTICODE_UNSIGNED_SUBJECT`` sentinel
in that field -- a stated absence, never a fabricated ``CN=...`` identity --
and integrity still comes from the pinned Ed25519 key alone.  The platform
block declares the exact Windows version/build, and the service descriptor
declares a least-privilege Windows service install mode.  Verification returns
member-only facts.  It never returns ``route_ready``, never grants eligibility,
and never claims that a package was installed or exercised.
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
    dotted_version,
    fail as _fail,
    is_text as _text,
    validate_manifest,
    verify_signed,
)

from .install_source import AUTHENTICODE_UNSIGNED_SUBJECT
from .platform_admission import ADMITTED_ARCHITECTURES, ADMITTED_RELEASES


def _platform_version(value: Any) -> tuple[int, ...]:
    return dotted_version(value, code="package_platform_invalid")


def _validate_platform(platform_architecture: Mapping[str, Any]) -> None:
    if (
        platform_architecture["system"] != "windows"
        or platform_architecture["machine"] not in ADMITTED_ARCHITECTURES
    ):
        _fail("package_platform_invalid")
    _platform_version(platform_architecture["windows_version"])
    if not isinstance(
        platform_architecture["windows_build"], str
    ) or not platform_architecture["windows_build"].isdigit():
        _fail("package_platform_invalid")


def _validate_publisher_extra(publisher: Mapping[str, Any]) -> None:
    # The Authenticode subject is a display identity, not a path segment: it is
    # bounded text, and the pinned key digest remains the cryptographic identity.
    # Owner ruling 2026-09-27 #4 (no certificate for owner_private_media): the
    # pilot channel's package instead carries the explicit unsigned sentinel,
    # never a fabricated CN=... subject -- every other publisher check still
    # applies unchanged.
    if publisher["authenticode_subject"] != AUTHENTICODE_UNSIGNED_SUBJECT and not _text(
        publisher["authenticode_subject"]
    ):
        _fail("package_publisher_invalid")


PROFILE = ManifestProfile(
    platform_fields=frozenset({"system", "machine", "windows_version", "windows_build"}),
    validate_platform=_validate_platform,
    extra_publisher_fields=frozenset({"authenticode_subject"}),
    validate_publisher_extra=_validate_publisher_extra,
    descriptor_name_field="service_name",
    install_modes=frozenset({"windows_service", "windows_service_per_user"}),
)


def validate_windows_package_manifest(document: object) -> dict[str, Any]:
    """Closed-shape validation of one installed Windows package manifest."""

    return validate_manifest(document, PROFILE)


def build_windows_package_manifest(
    *, signer, issued_at, expires_at, **bindings: Any
) -> dict[str, Any]:
    """Assemble and sign one Windows manifest from the twelve frozen bindings."""

    return build_manifest(PROFILE, signer=signer, issued_at=issued_at, expires_at=expires_at, **bindings)


def verify_windows_package(
    document: object,
    *,
    expected_publisher: str,
    expected_authenticode_subject: str,
    expected_architecture: str,
    expected_windows_version: str,
    expected_windows_build: str,
    installed_version: str,
    rollback_floor_pin: str,
    publisher_key_digest_pin: str,
    now: float,
) -> dict[str, Any]:
    """Verify one offered Windows installer; return member-only facts or refuse."""

    value = validate_windows_package_manifest(document)
    publisher = verify_signed(value, expected_publisher=expected_publisher,
                              publisher_key_digest_pin=publisher_key_digest_pin, now=now)
    if publisher["authenticode_subject"] != expected_authenticode_subject:
        _fail("package_publisher_mismatch")
    platform_architecture = value["platform_architecture"]
    declared = ADMITTED_RELEASES["windows"]
    floor_version = tuple(int(part) for part in declared["minimum_version"].split("."))
    offered = _platform_version(platform_architecture["windows_version"])
    floor_build = int(declared["minimum_build"])
    if (
        platform_architecture["machine"] != expected_architecture
        or platform_architecture["machine"] not in ADMITTED_ARCHITECTURES
        or offered < floor_version
        or platform_architecture["windows_version"] != expected_windows_version
        or platform_architecture["windows_build"] != expected_windows_build
        or int(platform_architecture["windows_build"]) < floor_build
    ):
        _fail("package_platform_unsupported")
    check_rollback_floor(value, rollback_floor_pin=rollback_floor_pin, installed_version=installed_version)
    return {
        "verified": True,
        "protocol": PROTOCOL,
        "source_revision": value["source_revision"],
        "platform": "windows",
        "platform_architecture": platform_architecture["machine"],
        "windows_version": platform_architecture["windows_version"],
        "windows_build": platform_architecture["windows_build"],
        "package_digest": value["package_digest"],
        "publisher_id": publisher["publisher_id"],
        "authenticode_subject": publisher["authenticode_subject"],
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
    "BINDINGS",
    "PROTOCOL",
    "PackageManifestError",
    "build_windows_package_manifest",
    "validate_windows_package_manifest",
    "verify_windows_package",
]
