# SPDX-License-Identifier: AGPL-3.0-only
"""Shared core of the closed ``mycelium.installation_package_manifest.v1``.

Everything here is identical across platforms: the envelope, the twelve frozen
bindings of the A13 acceptance inventory, the embedded-material scan (no invite,
seed key, owner credential, model, fixed peer, fixed topology or hidden download
URL), signing, and the signature / expiry / publisher / key-pin / rollback-floor
checks.  A :class:`ManifestProfile` supplies only what differs per platform.
Refusal codes are the ones the Windows and Linux tranches already froze.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
import re
from typing import Any

from mycelium_qualification.evidence import canonical_json_bytes
from mycelium_qualification.signing import SIGNATURE_FIELDS, build_ed25519_verifier

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
DOCUMENT_FIELDS = frozenset(BINDINGS) | _ENVELOPE_FIELDS
_BUILD_INPUT_FIELDS = frozenset({"builder_id", "build_digest"})
_BASE_PUBLISHER_FIELDS = frozenset({"publisher_id", "endpoint_id", "verification_key_record"})

#: Material a package may never carry (A13 §2).
FORBIDDEN_MATERIAL = frozenset(
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

MAX_BYTES = 65536
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_SEGMENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_SOURCE_REVISION = re.compile(r"^[0-9a-f]{40}$")
_URL = re.compile(r"[A-Za-z][A-Za-z0-9+.\-]*://")


class PackageManifestError(ValueError):
    """Fail-closed package-manifest error carrying a stable code."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def fail(code: str) -> None:
    raise PackageManifestError(code)


def is_digest(value: Any) -> bool:
    return isinstance(value, str) and _DIGEST.fullmatch(value) is not None


def is_segment(value: Any) -> bool:
    return isinstance(value, str) and _SEGMENT.fullmatch(value) is not None


def is_text(value: Any, *, maximum: int = 256) -> bool:
    return (
        isinstance(value, str)
        and bool(value)
        and value == value.strip()
        and len(value) <= maximum
        and "\x00" not in value
    )


def dotted_version(value: Any, *, code: str = "package_rollback_floor_invalid") -> tuple[int, ...]:
    if not isinstance(value, str) or not value:
        fail(code)
    parts = value.split(".")
    if not all(part.isdigit() for part in parts):
        fail(code)
    return tuple(int(part) for part in parts)


@dataclass(frozen=True)
class ManifestProfile:
    """What one platform's manifest adds to the shared core."""

    #: Exact key set of ``platform_architecture``.
    platform_fields: frozenset[str]
    #: Raises (via :func:`fail`) when the platform block's values are invalid.
    validate_platform: Callable[[Mapping[str, Any]], None]
    #: Publisher fields beyond publisher_id / endpoint_id / verification_key_record.
    extra_publisher_fields: frozenset[str]
    #: Raises when an extra publisher field is invalid (runs before the key check).
    validate_publisher_extra: Callable[[Mapping[str, Any]], None]
    #: ``service_name`` (Windows) or ``unit_name`` (Linux).
    descriptor_name_field: str
    install_modes: frozenset[str]

    @property
    def publisher_fields(self) -> frozenset[str]:
        return _BASE_PUBLISHER_FIELDS | self.extra_publisher_fields

    @property
    def descriptor_fields(self) -> frozenset[str]:
        return frozenset({self.descriptor_name_field, "sha256", "install_mode", "lifecycle_support"})


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


def check_no_embedded_material(document: Mapping[str, Any]) -> None:
    for key in _walk(document):
        if isinstance(key, str) and key.lower() in FORBIDDEN_MATERIAL:
            fail("package_embedded_secret")
    for text in _walk_strings(document):
        if _URL.search(text) is not None:
            fail("package_hidden_download_url")


def validate_bindings(document: Mapping[str, Any], profile: ManifestProfile) -> None:
    if not isinstance(document["source_revision"], str) or _SOURCE_REVISION.fullmatch(
        document["source_revision"]
    ) is None:
        fail("package_source_revision_invalid")
    build_inputs = document["build_inputs"]
    if not isinstance(build_inputs, Mapping) or set(build_inputs) != _BUILD_INPUT_FIELDS:
        fail("package_build_inputs_invalid")
    if not is_segment(build_inputs["builder_id"]) or not is_digest(build_inputs["build_digest"]):
        fail("package_build_inputs_invalid")
    platform_architecture = document["platform_architecture"]
    if (
        not isinstance(platform_architecture, Mapping)
        or set(platform_architecture) != profile.platform_fields
    ):
        fail("package_platform_invalid")
    profile.validate_platform(platform_architecture)
    if not is_digest(document["package_digest"]):
        fail("package_digest_invalid")
    publisher = document["publisher_identity"]
    if not isinstance(publisher, Mapping) or set(publisher) != profile.publisher_fields:
        fail("package_publisher_invalid")
    if not is_segment(publisher["publisher_id"]) or not is_segment(publisher["endpoint_id"]):
        fail("package_publisher_invalid")
    profile.validate_publisher_extra(publisher)
    try:
        build_ed25519_verifier([dict(publisher["verification_key_record"])])
    except Exception:  # noqa: BLE001 - fail closed on any key-record defect
        fail("package_publisher_invalid")
    if not is_segment(document["minimum_os"]):
        fail("package_minimum_os_invalid")
    descriptor = document["service_descriptor"]
    if (
        not isinstance(descriptor, Mapping)
        or set(descriptor) != profile.descriptor_fields
        or not is_segment(descriptor[profile.descriptor_name_field])
        or not is_digest(descriptor["sha256"])
        or descriptor["install_mode"] not in profile.install_modes
        or descriptor["lifecycle_support"] is not True
    ):
        fail("package_lifecycle_support_missing")
    if not is_segment(document["update_channel"]):
        fail("package_update_channel_invalid")
    dotted_version(document["rollback_floor"])
    if not is_segment(document["capability_protocol"]) or not document[
        "capability_protocol"
    ].startswith("mycelium."):
        fail("package_capability_protocol_invalid")
    if not is_segment(document["privacy_notice"]):
        fail("package_privacy_notice_invalid")
    if not is_segment(document["expiry_revocation_policy"]):
        fail("package_expiry_policy_invalid")


def validate_times(document: Mapping[str, Any]) -> None:
    issued_at = document["issued_at"]
    expires_at = document["expires_at"]
    if (
        isinstance(issued_at, bool)
        or not isinstance(issued_at, (int, float))
        or isinstance(expires_at, bool)
        or not isinstance(expires_at, (int, float))
        or float(expires_at) <= float(issued_at)
    ):
        fail("package_manifest_shape_invalid")


def validate_manifest(document: object, profile: ManifestProfile) -> dict[str, Any]:
    """Closed-shape validation of one installed-package manifest."""

    if not isinstance(document, Mapping) or set(document) != DOCUMENT_FIELDS:
        fail("package_manifest_shape_invalid")
    try:
        size = len(canonical_json_bytes(dict(document)))
    except Exception:  # noqa: BLE001 - non-canonical input fails closed
        fail("package_manifest_shape_invalid")
    if size > MAX_BYTES:
        fail("package_manifest_too_large")
    if document["protocol"] != PROTOCOL:
        fail("package_manifest_protocol_invalid")
    check_no_embedded_material(document)
    validate_bindings(document, profile)
    validate_times(document)
    signature = document["signature"]
    if not isinstance(signature, Mapping) or set(signature) != SIGNATURE_FIELDS:
        fail("package_signature_invalid")
    return dict(document)


def build_manifest(profile: ManifestProfile, *, signer, issued_at, expires_at, **bindings: Any) -> dict[str, Any]:
    """Assemble and sign one manifest from the twelve frozen bindings."""

    if set(BINDINGS) - set(bindings) or set(bindings) - set(BINDINGS):
        fail("package_manifest_shape_invalid")
    if not hasattr(signer, "sign") or not hasattr(signer, "public_key_record"):
        fail("package_publisher_invalid")
    document = {
        "protocol": PROTOCOL,
        **{name: bindings[name] for name in BINDINGS},
        "issued_at": issued_at,
        "expires_at": expires_at,
    }
    check_no_embedded_material(document)
    validate_bindings(document, profile)
    validate_times(document)
    statement = dict(document)
    return {**statement, "signature": signer.sign(statement)}


def verify_signed(
    value: Mapping[str, Any], *, expected_publisher: str, publisher_key_digest_pin: str, now: float
) -> Mapping[str, Any]:
    """Signature, time, publisher label and pinned key, in the frozen order; returns the publisher."""

    publisher = value["publisher_identity"]
    key_record = dict(publisher["verification_key_record"])
    try:
        verify = build_ed25519_verifier([key_record])
    except Exception:  # noqa: BLE001 - fail closed on any key-record defect
        fail("package_publisher_invalid")
    statement = {key: item for key, item in value.items() if key != "signature"}
    if not verify(canonical_json_bytes(statement), dict(value["signature"])):
        fail("package_signature_invalid")
    if isinstance(now, bool) or not isinstance(now, (int, float)):
        fail("package_manifest_shape_invalid")
    if float(now) > float(value["expires_at"]):
        fail("package_expired")
    if publisher["publisher_id"] != expected_publisher:
        fail("package_publisher_mismatch")
    if key_record["verification_key_digest"] != publisher_key_digest_pin:
        fail("package_publisher_mismatch")
    return publisher


def check_rollback_floor(value: Mapping[str, Any], *, rollback_floor_pin: str, installed_version: str) -> None:
    package_floor = dotted_version(value["rollback_floor"])
    pin = dotted_version(rollback_floor_pin)
    installed = dotted_version(installed_version)
    if package_floor < pin or installed < package_floor:
        fail("package_downgrade_rejected")


__all__ = [
    "BINDINGS",
    "DOCUMENT_FIELDS",
    "FORBIDDEN_MATERIAL",
    "PROTOCOL",
    "ManifestProfile",
    "PackageManifestError",
    "build_manifest",
    "check_no_embedded_material",
    "check_rollback_floor",
    "dotted_version",
    "fail",
    "is_digest",
    "is_segment",
    "is_text",
    "validate_manifest",
    "verify_signed",
]
