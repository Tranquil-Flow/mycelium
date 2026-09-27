# SPDX-License-Identifier: AGPL-3.0-or-later
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
import re
from typing import Any

from mycelium_qualification.evidence import canonical_json_bytes
from mycelium_qualification.signing import (
    SIGNATURE_FIELDS,
    build_ed25519_verifier,
)

from .install_source import AUTHENTICODE_UNSIGNED_SUBJECT
from .platform_admission import ADMITTED_ARCHITECTURES, ADMITTED_RELEASES

PROTOCOL = "mycelium.installation_package_manifest.v1"

#: Exactly the frozen A13 acceptance inventory's ``manifest_bindings``.
BINDINGS: tuple[str, ...] = (
    "source_revision",
    "build_inputs",
    "platform_architecture",
    "package_digest",
    "publisher_identity",
    "minimum_os",
    "service_descriptor",
    "update_channel",
    "rollback_floor",
    "capability_protocol",
    "privacy_notice",
    "expiry_revocation_policy",
)

_ENVELOPE_FIELDS = frozenset({"protocol", "issued_at", "expires_at", "signature"})
_DOCUMENT_FIELDS = frozenset(BINDINGS) | _ENVELOPE_FIELDS
_BUILD_INPUT_FIELDS = frozenset({"builder_id", "build_digest"})
_PLATFORM_FIELDS = frozenset(
    {"system", "machine", "windows_version", "windows_build"}
)
_PUBLISHER_FIELDS = frozenset(
    {"publisher_id", "authenticode_subject", "endpoint_id", "verification_key_record"}
)
_SERVICE_DESCRIPTOR_FIELDS = frozenset(
    {"service_name", "sha256", "install_mode", "lifecycle_support"}
)
_INSTALL_MODES = frozenset({"windows_service", "windows_service_per_user"})

#: Material a package may never carry (A13 §2: no invite, seed key, owner
#: credential, model, fixed peer, fixed topology or hidden download URL).
_FORBIDDEN_MATERIAL = frozenset(
    {
        "invite",
        "invite_bundle",
        "invite_token",
        "swarm_secret",
        "seed_private_key",
        "private_key",
        "owner_credential",
        "credential",
        "api_key",
        "model",
        "model_bytes",
        "model_weights",
        "fixed_peer",
        "fixed_topology",
        "topology",
    }
)

_MAX_BYTES = 65536
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_SEGMENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_SOURCE_REVISION = re.compile(r"^[0-9a-f]{40}$")
_URL = re.compile(r"[A-Za-z][A-Za-z0-9+.\-]*://")


class PackageManifestError(ValueError):
    """Fail-closed package-manifest error carrying a stable code."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _fail(code: str) -> None:
    raise PackageManifestError(code)


def _digest(value: Any) -> bool:
    return isinstance(value, str) and _DIGEST.fullmatch(value) is not None


def _segment(value: Any) -> bool:
    return isinstance(value, str) and _SEGMENT.fullmatch(value) is not None


def _text(value: Any, *, maximum: int = 256) -> bool:
    return (
        isinstance(value, str)
        and bool(value)
        and value == value.strip()
        and len(value) <= maximum
        and "\x00" not in value
    )


def _version(value: Any) -> tuple[int, ...]:
    if not isinstance(value, str) or not value:
        _fail("package_rollback_floor_invalid")
    parts = value.split(".")
    if not all(part.isdigit() for part in parts):
        _fail("package_rollback_floor_invalid")
    return tuple(int(part) for part in parts)


def _platform_version(value: Any) -> tuple[int, ...]:
    if not isinstance(value, str) or not value:
        _fail("package_platform_invalid")
    parts = value.split(".")
    if not all(part.isdigit() for part in parts):
        _fail("package_platform_invalid")
    return tuple(int(part) for part in parts)


def _walk(value: Any):
    if isinstance(value, Mapping):
        for key, item in value.items():
            yield key
            yield from _walk(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _walk(item)


def _walk_strings(value: Any):
    if isinstance(value, str):
        yield value
    elif isinstance(value, Mapping):
        for item in value.values():
            yield from _walk_strings(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _walk_strings(item)


def _check_no_embedded_material(document: Mapping[str, Any]) -> None:
    for key in _walk(document):
        if isinstance(key, str) and key.lower() in _FORBIDDEN_MATERIAL:
            _fail("package_embedded_secret")
    for text in _walk_strings(document):
        if _URL.search(text) is not None:
            _fail("package_hidden_download_url")


def _validate_bindings(document: Mapping[str, Any]) -> None:
    if not isinstance(document["source_revision"], str) or _SOURCE_REVISION.fullmatch(
        document["source_revision"]
    ) is None:
        _fail("package_source_revision_invalid")
    build_inputs = document["build_inputs"]
    if not isinstance(build_inputs, Mapping) or set(build_inputs) != _BUILD_INPUT_FIELDS:
        _fail("package_build_inputs_invalid")
    if not _segment(build_inputs["builder_id"]) or not _digest(build_inputs["build_digest"]):
        _fail("package_build_inputs_invalid")
    platform_architecture = document["platform_architecture"]
    if (
        not isinstance(platform_architecture, Mapping)
        or set(platform_architecture) != _PLATFORM_FIELDS
    ):
        _fail("package_platform_invalid")
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
    if not _digest(document["package_digest"]):
        _fail("package_digest_invalid")
    publisher = document["publisher_identity"]
    if not isinstance(publisher, Mapping) or set(publisher) != _PUBLISHER_FIELDS:
        _fail("package_publisher_invalid")
    if not _segment(publisher["publisher_id"]) or not _segment(publisher["endpoint_id"]):
        _fail("package_publisher_invalid")
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
    try:
        build_ed25519_verifier([dict(publisher["verification_key_record"])])
    except Exception:  # noqa: BLE001 - fail closed on any key-record defect
        _fail("package_publisher_invalid")
    if not _segment(document["minimum_os"]):
        _fail("package_minimum_os_invalid")
    descriptor = document["service_descriptor"]
    if (
        not isinstance(descriptor, Mapping)
        or set(descriptor) != _SERVICE_DESCRIPTOR_FIELDS
        or not _segment(descriptor["service_name"])
        or not _digest(descriptor["sha256"])
        or descriptor["install_mode"] not in _INSTALL_MODES
        or descriptor["lifecycle_support"] is not True
    ):
        _fail("package_lifecycle_support_missing")
    if not _segment(document["update_channel"]):
        _fail("package_update_channel_invalid")
    _version(document["rollback_floor"])
    if not _segment(document["capability_protocol"]) or not document[
        "capability_protocol"
    ].startswith("mycelium."):
        _fail("package_capability_protocol_invalid")
    if not _segment(document["privacy_notice"]):
        _fail("package_privacy_notice_invalid")
    if not _segment(document["expiry_revocation_policy"]):
        _fail("package_expiry_policy_invalid")


def _validate_times(document: Mapping[str, Any]) -> None:
    issued_at = document["issued_at"]
    expires_at = document["expires_at"]
    if (
        isinstance(issued_at, bool)
        or not isinstance(issued_at, (int, float))
        or isinstance(expires_at, bool)
        or not isinstance(expires_at, (int, float))
        or float(expires_at) <= float(issued_at)
    ):
        _fail("package_manifest_shape_invalid")


def _validate_envelope(document: Mapping[str, Any]) -> None:
    _validate_times(document)
    signature = document["signature"]
    if not isinstance(signature, Mapping) or set(signature) != SIGNATURE_FIELDS:
        _fail("package_signature_invalid")


def validate_windows_package_manifest(document: object) -> dict[str, Any]:
    """Closed-shape validation of one installed Windows package manifest."""

    if not isinstance(document, Mapping) or set(document) != _DOCUMENT_FIELDS:
        _fail("package_manifest_shape_invalid")
    try:
        size = len(canonical_json_bytes(dict(document)))
    except Exception:  # noqa: BLE001 - non-canonical input fails closed
        _fail("package_manifest_shape_invalid")
    if size > _MAX_BYTES:
        _fail("package_manifest_too_large")
    if document["protocol"] != PROTOCOL:
        _fail("package_manifest_protocol_invalid")
    _check_no_embedded_material(document)
    _validate_bindings(document)
    _validate_envelope(document)
    return dict(document)


def build_windows_package_manifest(
    *, signer, issued_at, expires_at, **bindings: Any
) -> dict[str, Any]:
    """Assemble and sign one Windows manifest from the twelve frozen bindings."""

    missing = set(BINDINGS) - set(bindings)
    if missing:
        _fail("package_manifest_shape_invalid")
    extra = set(bindings) - set(BINDINGS)
    if extra:
        _fail("package_manifest_shape_invalid")
    if not hasattr(signer, "sign") or not hasattr(signer, "public_key_record"):
        _fail("package_publisher_invalid")
    document = {
        "protocol": PROTOCOL,
        **{name: bindings[name] for name in BINDINGS},
        "issued_at": issued_at,
        "expires_at": expires_at,
    }
    _check_no_embedded_material(document)
    _validate_bindings(document)
    _validate_times(document)
    statement = dict(document)
    return {**statement, "signature": signer.sign(statement)}


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
    publisher = value["publisher_identity"]
    key_record = dict(publisher["verification_key_record"])
    try:
        verify = build_ed25519_verifier([key_record])
    except Exception:  # noqa: BLE001 - fail closed on any key-record defect
        _fail("package_publisher_invalid")
    statement = {key: item for key, item in value.items() if key != "signature"}
    if not verify(canonical_json_bytes(statement), dict(value["signature"])):
        _fail("package_signature_invalid")
    if isinstance(now, bool) or not isinstance(now, (int, float)):
        _fail("package_manifest_shape_invalid")
    if float(now) > float(value["expires_at"]):
        _fail("package_expired")
    if publisher["publisher_id"] != expected_publisher:
        _fail("package_publisher_mismatch")
    if key_record["verification_key_digest"] != publisher_key_digest_pin:
        _fail("package_publisher_mismatch")
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
    package_floor = _version(value["rollback_floor"])
    pin = _version(rollback_floor_pin)
    installed = _version(installed_version)
    if package_floor < pin or installed < package_floor:
        _fail("package_downgrade_rejected")
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
