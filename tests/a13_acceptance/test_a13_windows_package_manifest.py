# SPDX-License-Identifier: AGPL-3.0-only
"""A13 Windows installation package manifest — signed, closed, fail-closed.

The manifest bindings are cross-checked against the frozen A13 acceptance
inventory (read-only), so the Windows tranche cannot invent its own binding set
or drift from the landed Linux slice's closed protocol.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from mycelium_a13_linux.package_manifest import (
    PROTOCOL as LINUX_PROTOCOL,
)
from mycelium_a13_windows.install_source import AUTHENTICODE_UNSIGNED_SUBJECT
from mycelium_a13_windows.package_manifest import (
    BINDINGS,
    PROTOCOL,
    PackageManifestError,
    build_windows_package_manifest,
    validate_windows_package_manifest,
    verify_windows_package,
)
from mycelium_qualification.signing import generate_ed25519_signer

INVENTORY = Path(__file__).with_name("inventory.v1.json")
NOW = 2_000_000_000.0
PUBLISHER = "mycelium-release"
PUBLISHER_SUBJECT = "CN=Mycelium Release, O=Mycelium"
PUBLISHER_SIGNER = generate_ed25519_signer(endpoint_id="release-signer")
PUBLISHER_KEY_PIN = PUBLISHER_SIGNER.verification_key_digest
SERVICE_DESCRIPTOR = {
    "service_name": "mycelium-win-1-agent",
    "sha256": "sha256:" + "b" * 64,
    "install_mode": "windows_service",
    "lifecycle_support": True,
}
PLATFORM_ARCHITECTURE = {
    "system": "windows",
    "machine": "x86_64",
    "windows_version": "10.0",
    "windows_build": "22631",
}


def _manifest(**overrides):
    signer = overrides.pop("signer", None) or PUBLISHER_SIGNER
    values = {
        "signer": signer,
        "source_revision": "a" * 40,
        "build_inputs": {"builder_id": "mycelium-ci", "build_digest": "sha256:" + "c" * 64},
        "platform_architecture": dict(PLATFORM_ARCHITECTURE),
        "package_digest": "sha256:" + "d" * 64,
        "publisher_identity": {
            "publisher_id": PUBLISHER,
            "authenticode_subject": PUBLISHER_SUBJECT,
            "endpoint_id": signer.endpoint_id,
            "verification_key_record": signer.public_key_record(),
        },
        "minimum_os": "windows-10-22631",
        "service_descriptor": dict(SERVICE_DESCRIPTOR),
        "update_channel": "stable-windows",
        "rollback_floor": "0.1.0",
        "capability_protocol": "mycelium.capability_report.v1",
        "privacy_notice": "mycelium-a13-privacy-v1",
        "expiry_revocation_policy": "expire_after_90d_or_revocation_list",
        "issued_at": NOW,
        "expires_at": NOW + 3600,
    }
    values.update(overrides)
    return build_windows_package_manifest(**values)


def _verify(document, **overrides):
    arguments = {
        "expected_publisher": PUBLISHER,
        "expected_authenticode_subject": PUBLISHER_SUBJECT,
        "expected_architecture": "x86_64",
        "expected_windows_version": "10.0",
        "expected_windows_build": "22631",
        "installed_version": "0.1.0",
        "rollback_floor_pin": "0.1.0",
        "publisher_key_digest_pin": PUBLISHER_KEY_PIN,
        "now": NOW + 10,
    }
    arguments.update(overrides)
    return verify_windows_package(document, **arguments)


def _code(call):
    with pytest.raises(PackageManifestError) as caught:
        call()
    return caught.value.code


def test_bindings_and_protocol_match_the_frozen_inventory_and_the_linux_slice() -> None:
    document = json.loads(INVENTORY.read_text("utf-8"))
    declared = document["package_update_requirements"]["manifest_bindings"]
    assert list(BINDINGS) == list(declared)
    assert len(BINDINGS) == 12
    # One frozen package-manifest contract serves every platform tranche.
    assert PROTOCOL == LINUX_PROTOCOL == "mycelium.installation_package_manifest.v1"


def test_signed_windows_manifest_verifies_and_allows_identity_only_afterwards() -> None:
    document = _manifest()
    assert document["protocol"] == PROTOCOL
    assert set(document) == set(BINDINGS) | {"protocol", "issued_at", "expires_at", "signature"}
    verified = _verify(document)
    assert verified["verified"] is True
    assert verified["platform"] == "windows"
    assert verified["identity_creation_allowed"] is True
    assert verified["eligibility"] == "member_only"
    assert verified["activation"] == "none"
    assert verified["route_ready"] is False
    assert verified["service_descriptor"]["install_mode"] == "windows_service"
    assert verified["rollback_floor"] == "0.1.0"


def test_validate_is_closed_over_unknown_and_missing_fields() -> None:
    document = _manifest()
    extra = copy.deepcopy(document)
    extra["hidden_download_url"] = "https://example.invalid/package.msi"
    assert _code(lambda: validate_windows_package_manifest(extra)) == (
        "package_manifest_shape_invalid"
    )
    missing = {key: value for key, value in document.items() if key != "privacy_notice"}
    assert _code(lambda: validate_windows_package_manifest(missing)) == (
        "package_manifest_shape_invalid"
    )
    assert _code(lambda: validate_windows_package_manifest("not-a-record")) == (
        "package_manifest_shape_invalid"
    )


def test_embedded_invitation_model_or_url_material_is_refused() -> None:
    assert (
        _code(
            lambda: _manifest(
                build_inputs={
                    "builder_id": "mycelium-ci",
                    "build_digest": "sha256:" + "c" * 64,
                    "seed_private_key": 1,
                }
            )
        )
        == "package_embedded_secret"
    )
    assert (
        _code(
            lambda: _manifest(
                build_inputs={
                    "builder_id": "mycelium-ci",
                    "build_digest": "sha256:" + "c" * 64,
                    "model_bytes": 1,
                }
            )
        )
        == "package_embedded_secret"
    )
    assert (
        _code(lambda: _manifest(update_channel="https://example.invalid/stable"))
        == "package_hidden_download_url"
    )
    assert _code(lambda: _manifest(privacy_notice="a/../b")) == (
        "package_privacy_notice_invalid"
    )


def test_platform_architecture_must_declare_the_exact_windows_target() -> None:
    assert (
        _code(
            lambda: _manifest(
                platform_architecture={
                    "system": "windows",
                    "machine": "arm64",
                    "windows_version": "10.0",
                    "windows_build": "22631",
                }
            )
        )
        == "package_platform_invalid"
    )
    document = _manifest()
    for field, value in (
        ("expected_architecture", "arm64"),
        ("expected_windows_version", "6.3"),
        ("expected_windows_build", "19045"),
    ):
        assert _code(lambda: _verify(copy.deepcopy(document), **{field: value})) == (
            "package_platform_unsupported"
        )


def test_service_descriptor_must_prove_windows_lifecycle_support() -> None:
    for descriptor in (
        {**SERVICE_DESCRIPTOR, "install_mode": "systemd_user"},
        {**SERVICE_DESCRIPTOR, "lifecycle_support": False},
        {**SERVICE_DESCRIPTOR, "unit_name": "mycelium.service"},
        {**SERVICE_DESCRIPTOR, "service_name": "mycelium win"},
    ):
        assert (
            _code(lambda: _manifest(service_descriptor=descriptor))
            == "package_lifecycle_support_missing"
        )


def test_publisher_identity_signature_and_subject_fail_closed() -> None:
    document = _manifest()
    other = generate_ed25519_signer(endpoint_id="other-signer")
    forged = _manifest(
        signer=other,
        publisher_identity={
            "publisher_id": PUBLISHER,
            "authenticode_subject": PUBLISHER_SUBJECT,
            "endpoint_id": other.endpoint_id,
            "verification_key_record": other.public_key_record(),
        },
    )
    assert _code(lambda: _verify(forged)) == "package_publisher_mismatch"
    assert (
        _code(lambda: _verify(copy.deepcopy(document), expected_publisher="other-release"))
        == "package_publisher_mismatch"
    )
    assert (
        _code(
            lambda: _verify(
                copy.deepcopy(document),
                expected_authenticode_subject="CN=Impostor, O=Impostor",
            )
        )
        == "package_publisher_mismatch"
    )
    assert (
        _code(
            lambda: _verify(
                copy.deepcopy(document), publisher_key_digest_pin="sha256:" + "0" * 64
            )
        )
        == "package_publisher_mismatch"
    )
    tampered = copy.deepcopy(document)
    tampered["package_digest"] = "sha256:" + "e" * 64
    assert _code(lambda: _verify(tampered)) == "package_signature_invalid"


def test_expiry_and_downgrade_are_rejected_before_identity_work() -> None:
    document = _manifest()
    assert (
        _code(lambda: _verify(copy.deepcopy(document), now=NOW + 4000)) == "package_expired"
    )
    assert (
        _code(lambda: _verify(copy.deepcopy(document), installed_version="0.0.9"))
        == "package_downgrade_rejected"
    )
    assert (
        _code(
            lambda: _verify(
                copy.deepcopy(document),
                rollback_floor_pin="0.2.0",
                installed_version="0.2.0",
            )
        )
        == "package_downgrade_rejected"
    )
    assert _code(lambda: _manifest(rollback_floor="0.1.x")) == (
        "package_rollback_floor_invalid"
    )
    assert _code(lambda: _manifest(rollback_floor="")) == "package_rollback_floor_invalid"


def test_owner_private_media_pilot_manifest_carries_the_unsigned_sentinel() -> None:
    """Owner ruling 2026-09-27 #4: no certificate for owner_private_media.

    The pilot channel's manifest may state "no Authenticode identity" via the
    explicit sentinel; every other publisher/signature/platform check still
    applies exactly as it does for a fully-signed manifest.
    """

    document = _manifest(
        update_channel="owner_private_media",
        publisher_identity={
            "publisher_id": PUBLISHER,
            "authenticode_subject": AUTHENTICODE_UNSIGNED_SUBJECT,
            "endpoint_id": PUBLISHER_SIGNER.endpoint_id,
            "verification_key_record": PUBLISHER_SIGNER.public_key_record(),
        },
    )
    verified = _verify(document, expected_authenticode_subject=AUTHENTICODE_UNSIGNED_SUBJECT)
    assert verified["authenticode_subject"] == AUTHENTICODE_UNSIGNED_SUBJECT
    assert verified["verified"] is True
    assert verified["route_ready"] is False

    # An empty/whitespace-only "subject" is still refused -- the sentinel is the
    # only accepted way to state absence, not a loophole for blank text.
    assert (
        _code(
            lambda: _manifest(
                publisher_identity={
                    "publisher_id": PUBLISHER,
                    "authenticode_subject": "  ",
                    "endpoint_id": PUBLISHER_SIGNER.endpoint_id,
                    "verification_key_record": PUBLISHER_SIGNER.public_key_record(),
                }
            )
        )
        == "package_publisher_invalid"
    )


def test_verification_never_claims_activation_or_route_readiness() -> None:
    verified = _verify(_manifest())
    rendered = json.dumps(verified).lower()
    for forbidden in ("qualified", "accelerated", "inference_eligibility\": true"):
        assert forbidden not in rendered
    assert verified["route_ready"] is False
    assert verified["activation"] == "none"
