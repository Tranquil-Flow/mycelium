# SPDX-License-Identifier: AGPL-3.0-only
"""A13 Linux installation package manifest — signed, closed, fail-closed.

The manifest bindings are cross-checked against the frozen A13 acceptance
inventory (read-only) so the Linux tranche cannot invent its own binding set.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from mycelium_a13_linux.package_manifest import (
    BINDINGS,
    PROTOCOL,
    PackageManifestError,
    build_linux_package_manifest,
    validate_linux_package_manifest,
    verify_linux_package,
)
from mycelium_qualification.signing import generate_ed25519_signer

INVENTORY = Path(__file__).with_name("inventory.v1.json")
NOW = 2_000_000_000.0
PUBLISHER = "mycelium-release"
# The publisher key is pinned independently of the publisher label: a manifest
# signed by another key that merely claims the same label must fail closed.
PUBLISHER_SIGNER = generate_ed25519_signer(endpoint_id="release-signer")
PUBLISHER_KEY_PIN = PUBLISHER_SIGNER.verification_key_digest
SERVICE_DESCRIPTOR = {
    "unit_name": "mycelium-node2.service",
    "sha256": "sha256:" + "b" * 64,
    "install_mode": "systemd_user",
    "lifecycle_support": True,
}
PLATFORM_ARCHITECTURE = {
    "system": "linux",
    "machine": "x86_64",
    "distribution_id": "ubuntu",
    "distribution_version": "24.04",
}


def _manifest(**overrides):
    signer = overrides.pop("signer", None) or PUBLISHER_SIGNER
    values = {
        "signer": signer,
        "source_revision": "a" * 40,
        "build_inputs": {
            "builder_id": "mycelium-ci",
            "build_digest": "sha256:" + "c" * 64,
        },
        "platform_architecture": dict(PLATFORM_ARCHITECTURE),
        "package_digest": "sha256:" + "d" * 64,
        "publisher_identity": {
            "publisher_id": PUBLISHER,
            "endpoint_id": signer.endpoint_id,
            "verification_key_record": signer.public_key_record(),
        },
        "minimum_os": "ubuntu-24.04",
        "service_descriptor": dict(SERVICE_DESCRIPTOR),
        "update_channel": "stable-linux",
        "rollback_floor": "0.1.0",
        "capability_protocol": "mycelium.capability_report.v1",
        "privacy_notice": "mycelium-a13-privacy-v1",
        "expiry_revocation_policy": "expire_after_90d_or_revocation_list",
        "issued_at": NOW,
        "expires_at": NOW + 3600,
    }
    values.update(overrides)
    return build_linux_package_manifest(**values)


def _verify(document, **overrides):
    arguments = {
        "expected_publisher": PUBLISHER,
        "expected_architecture": "x86_64",
        "expected_distribution": "ubuntu",
        "installed_version": "0.1.0",
        "rollback_floor_pin": "0.1.0",
        "publisher_key_digest_pin": PUBLISHER_KEY_PIN,
        "now": NOW + 10,
    }
    arguments.update(overrides)
    return verify_linux_package(document, **arguments)


def _code(call):
    with pytest.raises(PackageManifestError) as caught:
        call()
    return caught.value.code


def test_bindings_match_the_frozen_a13_acceptance_inventory() -> None:
    document = json.loads(INVENTORY.read_text("utf-8"))
    declared = document["package_update_requirements"]["manifest_bindings"]
    assert list(BINDINGS) == list(declared)
    assert len(BINDINGS) == 12


def test_signed_linux_manifest_verifies_and_allows_identity_only_afterwards() -> None:
    document = _manifest()
    assert document["protocol"] == PROTOCOL
    assert set(document) == set(BINDINGS) | {"protocol", "issued_at", "expires_at", "signature"}
    verified = _verify(document)
    assert verified["verified"] is True
    assert verified["identity_creation_allowed"] is True
    assert verified["eligibility"] == "member_only"
    assert verified["activation"] == "none"
    assert verified["route_ready"] is False
    assert verified["update_channel"] == "stable-linux"
    assert verified["rollback_floor"] == "0.1.0"
    assert verified["package_digest"] == "sha256:" + "d" * 64


def test_validate_is_closed_over_unknown_and_missing_fields() -> None:
    document = _manifest()
    extra = copy.deepcopy(document)
    extra["secret_install_token"] = "nope"
    assert _code(lambda: validate_linux_package_manifest(extra)) == (
        "package_manifest_shape_invalid"
    )
    missing = copy.deepcopy(document)
    missing.pop("rollback_floor")
    assert _code(lambda: validate_linux_package_manifest(missing)) == (
        "package_manifest_shape_invalid"
    )
    assert _code(lambda: validate_linux_package_manifest("not-a-document")) == (
        "package_manifest_shape_invalid"
    )


def test_wrong_protocol_is_rejected() -> None:
    document = _manifest()
    document["protocol"] = "mycelium.installation_package_manifest.v2"
    assert _code(lambda: _verify(document)) == "package_manifest_protocol_invalid"


def test_tampered_statement_fails_signature_verification() -> None:
    document = _manifest()
    document["package_digest"] = "sha256:" + "e" * 64
    assert _code(lambda: _verify(document)) == "package_signature_invalid"


def test_signature_from_a_different_key_is_rejected() -> None:
    impostor = generate_ed25519_signer(endpoint_id="impostor-release")
    document = _manifest()
    document["publisher_identity"] = {
        "publisher_id": PUBLISHER,
        "endpoint_id": impostor.endpoint_id,
        "verification_key_record": impostor.public_key_record(),
    }
    assert _code(lambda: _verify(document)) == "package_signature_invalid"


def test_expired_package_fails_before_identity_mutation() -> None:
    document = _manifest()
    assert _code(lambda: _verify(document, now=NOW + 4000)) == "package_expired"


def test_downgrade_below_the_rollback_floor_is_rejected() -> None:
    # (a) the running version is below the floor the offered package pins.
    assert _code(
        lambda: _verify(
            _manifest(rollback_floor="0.2.0"),
            installed_version="0.1.0",
            rollback_floor_pin="0.2.0",
        )
    ) == "package_downgrade_rejected"
    # (b) a manifest whose floor drops below our own pinned floor is a downgrade vector.
    assert _code(
        lambda: _verify(
            _manifest(rollback_floor="0.0.9"),
            installed_version="0.3.0",
            rollback_floor_pin="0.1.0",
        )
    ) == "package_downgrade_rejected"
    # (c) a package at or above the pinned floor still verifies.
    verified = _verify(
        _manifest(rollback_floor="0.2.0"),
        installed_version="0.2.0",
        rollback_floor_pin="0.2.0",
    )
    assert verified["verified"] is True
    assert verified["rollback_floor"] == "0.2.0"


def test_changed_publisher_identity_is_rejected() -> None:
    other = generate_ed25519_signer(endpoint_id="other-release")
    document = _manifest(signer=other)
    # Signature is internally consistent for the impostor key, but it is not the
    # pinned publisher key, so the pin refuses it.
    assert _code(lambda: _verify(document)) == "package_publisher_mismatch"


@pytest.mark.parametrize(
    "platform_architecture",
    [
        {"system": "darwin", "machine": "arm64", "distribution_id": "macos", "distribution_version": "26.5.1"},
        {"system": "windows", "machine": "x86_64", "distribution_id": "windows", "distribution_version": "11"},
        {"system": "linux", "machine": "riscv64", "distribution_id": "ubuntu", "distribution_version": "24.04"},
        {"system": "linux", "machine": "x86_64", "distribution_id": "arch", "distribution_version": "1.0"},
        {"system": "linux", "machine": "x86_64", "distribution_id": "ubuntu", "distribution_version": "22.04"},
    ],
)
def test_unsupported_package_platform_is_rejected(platform_architecture) -> None:
    document = _manifest(platform_architecture=platform_architecture)
    assert _code(lambda: _verify(document)) == "package_platform_unsupported"
    assert _code(lambda: _verify(document, expected_architecture="aarch64")) == (
        "package_platform_unsupported"
    )


def test_missing_lifecycle_support_is_rejected() -> None:
    document = _manifest()
    document["service_descriptor"] = {**SERVICE_DESCRIPTOR, "lifecycle_support": False}
    assert _code(lambda: _verify(document)) == "package_lifecycle_support_missing"
    broken = _manifest()
    broken["service_descriptor"] = {"unit_name": "mycelium-node2.service"}
    assert _code(lambda: _verify(broken)) == "package_lifecycle_support_missing"


@pytest.mark.parametrize(
    "key",
    ["invite", "invite_bundle", "seed_private_key", "owner_credential", "model", "fixed_peer", "topology"],
)
def test_package_may_never_embed_invite_credential_model_peer_or_topology(key: str) -> None:
    document = _manifest()
    document["build_inputs"] = {**document["build_inputs"], key: "x"}
    assert _code(lambda: validate_linux_package_manifest(document)) == (
        "package_embedded_secret"
    )


def test_hidden_download_url_is_rejected() -> None:
    document = _manifest()
    document["update_channel"] = "https://example.invalid/mycelium.tar.gz"
    assert _code(lambda: validate_linux_package_manifest(document)) == (
        "package_hidden_download_url"
    )


def test_malformed_package_digest_is_rejected() -> None:
    document = _manifest()
    document["package_digest"] = "sha256:short"
    assert _code(lambda: validate_linux_package_manifest(document)) == (
        "package_digest_invalid"
    )


def test_oversized_manifest_is_rejected() -> None:
    document = _manifest()
    document["privacy_notice"] = "p" * 70000
    assert _code(lambda: validate_linux_package_manifest(document)) == (
        "package_manifest_too_large"
    )


def test_verification_never_grants_eligibility_or_route_readiness() -> None:
    verified = _verify(_manifest())
    assert verified["route_ready"] is False
    assert verified["eligibility"] == "member_only"
    assert verified["activation"] == "none"
    rendered = repr(verified).lower()
    assert "qualified" not in rendered
    assert "serving" not in rendered
