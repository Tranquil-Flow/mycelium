# SPDX-License-Identifier: AGPL-3.0-only
"""A13 Linux install source — normal-user, no expert tools, verify-then-identity."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from mycelium_a13_linux.install_source import (
    CHANNELS,
    FIELDS,
    INSTALL_MODES,
    PROTOCOL,
    InstallSourceError,
    install_plan,
    validate_install_source,
    verify_install_source,
)
from mycelium_qualification.signing import generate_ed25519_signer

NOW = 2_000_000_000.0
PUBLISHER = "mycelium-release"


def _source(**overrides):
    values = {
        "protocol": PROTOCOL,
        "channel": "owner_private_media",
        "package_class": "signed_linux_package",
        "artifact_name": "mycelium-a13-0.1.0-linux-x86_64.tar.zst",
        "artifact_digest": "sha256:" + "a" * 64,
        "publisher_identity": {
            "publisher_id": PUBLISHER,
            "endpoint_id": "release-signer",
            "verification_key_record": generate_ed25519_signer(
                endpoint_id="release-signer"
            ).public_key_record(),
        },
        "minimum_os": "ubuntu-24.04",
        "platform_architecture": "x86_64",
        "distribution_id": "ubuntu",
        "distribution_version": "24.04",
        "service_manager": "systemd",
        "install_mode": "systemd_user",
        "rollback_floor": "0.1.0",
        "lifecycle_support": True,
        "expert_tools_required": [],
    }
    values.update(overrides)
    return values


def _verify(source, **overrides):
    arguments = {
        "expected_publisher": PUBLISHER,
        "expected_architecture": "x86_64",
        "expected_distribution": "ubuntu",
        "installed_version": "0.1.0",
        "rollback_floor_pin": "0.1.0",
        "now": NOW,
    }
    arguments.update(overrides)
    return verify_install_source(source, **arguments)


def _code(call):
    with pytest.raises(InstallSourceError) as caught:
        call()
    return caught.value.code


def test_declared_channels_and_install_modes_are_closed() -> None:
    assert CHANNELS == frozenset({"owner_private_media", "signed_update_channel"})
    assert INSTALL_MODES == frozenset({"systemd_user", "systemd_system"})
    assert set(_source()) == FIELDS


def test_valid_source_verifies_without_granting_eligibility() -> None:
    verified = _verify(_source())
    assert verified["verified"] is True
    assert verified["identity_creation_allowed"] is True
    assert verified["eligibility"] == "member_only"
    assert verified["activation"] == "none"
    assert verified["inference_eligibility"] is False
    assert verified["route_ready"] is False
    assert verified["install_mode"] == "systemd_user"
    assert verified["update_floor_ok"] is True


def test_shape_and_protocol_are_closed() -> None:
    extra = {**_source(), "swarm_invite": "nope"}
    assert _code(lambda: validate_install_source(extra)) == "install_source_shape_invalid"
    missing = _source()
    missing.pop("artifact_digest")
    assert _code(lambda: validate_install_source(missing)) == "install_source_shape_invalid"
    wrong = {**_source(), "protocol": "mycelium.a13_linux_install_source.v2"}
    assert _code(lambda: validate_install_source(wrong)) == "install_source_protocol_invalid"
    assert _code(lambda: validate_install_source([1, 2, 3])) == "install_source_shape_invalid"


def test_expert_tools_are_forbidden_on_the_normal_user_path() -> None:
    for tools in (["ssh"], ["tailscale"], ["adb"], ["source checkout"], ["cli setup"]):
        assert _code(
            lambda: _verify(_source(expert_tools_required=tools))
        ) == "install_source_expert_tools_required"
    assert _code(lambda: validate_install_source(_source(expert_tools_required="ssh"))) == (
        "install_source_expert_tools_required"
    )


def test_hidden_download_urls_are_rejected() -> None:
    assert _code(
        lambda: validate_install_source(
            _source(artifact_name="https://example.invalid/mycelium.tar.zst")
        )
    ) == "install_source_hidden_url"
    assert _code(
        lambda: validate_install_source(
            _source(minimum_os="http://mirror.invalid/ubuntu")
        )
    ) == "install_source_hidden_url"


def test_artifact_name_must_be_a_bare_bounded_file_name() -> None:
    assert _code(
        lambda: validate_install_source(_source(artifact_name="../../etc/passwd"))
    ) == "install_source_artifact_invalid"
    assert _code(lambda: validate_install_source(_source(artifact_name=""))) == (
        "install_source_artifact_invalid"
    )


def test_channel_and_install_mode_are_validated() -> None:
    assert _code(lambda: validate_install_source(_source(channel="nightly"))) == (
        "install_source_channel_invalid"
    )
    assert _code(lambda: validate_install_source(_source(install_mode="launchd"))) == (
        "install_source_install_mode_invalid"
    )


def test_malformed_digest_is_rejected() -> None:
    assert _code(
        lambda: validate_install_source(_source(artifact_digest="sha256:short"))
    ) == "install_source_digest_invalid"
    assert _code(
        lambda: validate_install_source(_source(artifact_digest="md5:" + "a" * 32))
    ) == "install_source_digest_invalid"


def test_artifact_bytes_are_verified_against_the_declared_digest(tmp_path: Path) -> None:
    artifact = tmp_path / "mycelium.tar.zst"
    artifact.write_bytes(b"mycelium package bytes")
    digest = "sha256:" + hashlib.sha256(artifact.read_bytes()).hexdigest()
    source = _source(artifact_digest=digest)
    assert _verify(source, artifact_path=artifact)["artifact_verified"] is True
    assert _code(lambda: _verify(source, artifact_path=tmp_path / "missing.tar.zst")) == (
        "install_source_artifact_unavailable"
    )
    other = tmp_path / "other.tar.zst"
    other.write_bytes(b"different bytes")
    assert _code(lambda: _verify(source, artifact_path=other)) == (
        "install_source_digest_mismatch"
    )


def test_publisher_identity_is_verified() -> None:
    assert _code(lambda: _verify(_source(), expected_publisher="other-publisher")) == (
        "install_source_publisher_mismatch"
    )
    assert _code(
        lambda: validate_install_source(
            _source(publisher_identity={"publisher_id": PUBLISHER})
        )
    ) == "install_source_publisher_invalid"


def test_platform_boundary_is_enforced() -> None:
    assert _code(lambda: _verify(_source(distribution_id="arch"))) == (
        "install_source_platform_unsupported"
    )
    assert _code(lambda: _verify(_source(distribution_version="22.04"))) == (
        "install_source_platform_unsupported"
    )
    assert _code(
        lambda: _verify(_source(), expected_architecture="aarch64")
    ) == "install_source_platform_unsupported"
    assert _code(lambda: validate_install_source(_source(service_manager="openrc"))) == (
        "install_source_lifecycle_support_missing"
    )


def test_lifecycle_support_is_required() -> None:
    assert _code(lambda: validate_install_source(_source(lifecycle_support=False))) == (
        "install_source_lifecycle_support_missing"
    )
    assert _code(lambda: validate_install_source(_source(lifecycle_support="yes"))) == (
        "install_source_lifecycle_support_missing"
    )


def test_downgrade_vector_is_rejected() -> None:
    assert _code(
        lambda: _verify(_source(rollback_floor="0.0.9"), rollback_floor_pin="0.1.0")
    ) == "install_source_downgrade_rejected"
    assert _code(
        lambda: _verify(_source(rollback_floor="0.2.0"), installed_version="0.1.0", rollback_floor_pin="0.2.0")
    ) == "install_source_downgrade_rejected"


def test_install_plan_requires_a_verified_source() -> None:
    assert _code(lambda: install_plan(_source())) == "install_source_not_verified"


def test_install_plan_creates_identity_only_after_verification() -> None:
    plan = install_plan(_verify(_source()))
    assert plan["protocol"] == "mycelium.a13_linux_install_plan.v1"
    actions = [step["action"] for step in plan["steps"]]
    assert actions[:2] == ["verify_artifact_digest", "verify_publisher_signature"]
    assert "create_target_identity" in actions
    assert actions.index("create_target_identity") > actions.index("verify_publisher_signature")
    assert actions.index("create_target_identity") > actions.index("verify_artifact_digest")
    assert plan["identity_creation_allowed"] is True
    assert plan["public_listener"] is False
    assert plan["activation"] == "none"
    assert plan["inference_eligibility"] is False
    assert plan["expert_tools_required"] == []


def test_install_plan_never_reaches_for_an_expert_tool_or_a_public_listener() -> None:
    plan = install_plan(_verify(_source()))
    rendered = repr(plan).lower()
    for forbidden in ("ssh", "scp", "rsync", "tailscale", "adb", "termux", "0.0.0.0", "share --public"):
        assert forbidden not in rendered, forbidden
    assert _verify(_source())["route_ready"] is False
    assert plan["route_ready"] is False
