# SPDX-License-Identifier: AGPL-3.0-only
"""A13 Windows install source — the normal-user, no-expert-tools path (source tranche)."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from mycelium_a13_windows.install_source import (
    AUTHENTICODE_UNSIGNED_SUBJECT,
    CHANNELS,
    EXPERT_DEPENDENCIES,
    FIELDS,
    INSTALL_MODES,
    PLAN_PROTOCOL,
    PROTOCOL,
    InstallSourceError,
    install_plan,
    validate_windows_install_source,
    verify_windows_install_source,
)
from mycelium_qualification.signing import generate_ed25519_signer

NOW = 2_000_000_000.0
PUBLISHER = "mycelium-release"
# Owner ruling 2026-09-27 #4 (no certificate for owner_private_media): the pilot
# channel default fixture carries the explicit unsigned sentinel, never a
# fabricated CN=... subject. See test_signed_update_channel_still_requires_a_
# real_authenticode_subject below for the channel that IS still signed.
PUBLISHER_SUBJECT = AUTHENTICODE_UNSIGNED_SUBJECT
SIGNER = generate_ed25519_signer(endpoint_id="release-signer")
ARTIFACT_BYTES = b"mycelium-windows-installer-payload"


def _source(**overrides):
    values = {
        "protocol": PROTOCOL,
        "channel": "owner_private_media",
        "package_class": "signed_windows_installer",
        "artifact_name": "mycelium-0.1.0-x86_64.msi",
        "artifact_digest": "sha256:" + hashlib.sha256(ARTIFACT_BYTES).hexdigest(),
        "publisher_identity": {
            "publisher_id": PUBLISHER,
            "authenticode_subject": PUBLISHER_SUBJECT,
            "endpoint_id": SIGNER.endpoint_id,
            "verification_key_record": SIGNER.public_key_record(),
        },
        "minimum_os": "windows-10-22631",
        "platform_architecture": "x86_64",
        "windows_version": "10.0",
        "windows_build": "22631",
        "service_manager": "windows_service",
        "install_mode": "windows_service",
        "rollback_floor": "0.1.0",
        "lifecycle_support": True,
        "expert_tools_required": [],
    }
    values.update(overrides)
    return values


def _verify(source=None, **overrides):
    arguments = {
        "expected_publisher": PUBLISHER,
        "expected_authenticode_subject": PUBLISHER_SUBJECT,
        "expected_architecture": "x86_64",
        "expected_windows_version": "10.0",
        "expected_windows_build": "22631",
        "installed_version": "0.1.0",
        "rollback_floor_pin": "0.1.0",
        "now": NOW + 10,
        "artifact_path": None,
    }
    arguments.update(overrides)
    return verify_windows_install_source(source if source is not None else _source(), **arguments)


def _write_artifact(path: Path, raw: bytes = ARTIFACT_BYTES) -> Path:
    path.write_bytes(raw)
    return path


def _code(call):
    with pytest.raises(InstallSourceError) as caught:
        call()
    return caught.value.code


def test_declared_windows_install_source_shape_is_closed() -> None:
    assert set(FIELDS) == set(_source())
    assert CHANNELS == frozenset({"owner_private_media", "signed_update_channel"})
    assert INSTALL_MODES == frozenset({"windows_service", "windows_service_per_user"})
    assert "ssh" in EXPERT_DEPENDENCIES
    assert "cli setup" in EXPERT_DEPENDENCIES
    assert validate_windows_install_source(_source())["install_mode"] == "windows_service"


def test_verify_returns_member_only_facts_and_never_inference_eligibility() -> None:
    verified = _verify()
    assert verified["verified"] is True
    assert verified["artifact_verified"] is False
    assert verified["identity_creation_allowed"] is True
    assert verified["eligibility"] == "member_only"
    assert verified["activation"] == "none"
    assert verified["inference_eligibility"] is False
    assert verified["route_ready"] is False
    assert verified["update_floor_ok"] is True


def test_artifact_bytes_are_digest_verified_when_present(tmp_path) -> None:
    artifact = _write_artifact(tmp_path / "mycelium-0.1.0-x86_64.msi")
    verified = _verify(artifact_path=artifact)
    assert verified["artifact_verified"] is True

    tampered = _write_artifact(tmp_path / "tampered.msi", b"tampered")
    assert _code(lambda: _verify(artifact_path=tampered)) == "install_source_digest_mismatch"

    link = tmp_path / "link.msi"
    link.symlink_to(artifact)
    assert _code(lambda: _verify(artifact_path=link)) == "install_source_artifact_unavailable"
    assert (
        _code(lambda: _verify(artifact_path=tmp_path / "missing.msi"))
        == "install_source_artifact_unavailable"
    )


def test_windows_specific_boundaries_are_enforced() -> None:
    assert _code(lambda: _verify(_source(artifact_name="../../etc/passwd"))) == (
        "install_source_artifact_invalid"
    )
    assert _code(lambda: _verify(_source(channel="public_mirror"))) == (
        "install_source_channel_invalid"
    )
    assert _code(lambda: _verify(_source(package_class="signed_linux_package"))) == (
        "install_source_package_class_invalid"
    )
    assert _code(lambda: _verify(_source(install_mode="systemd_user"))) == (
        "install_source_install_mode_invalid"
    )
    assert _code(lambda: _verify(_source(service_manager="scheduled_task"))) == (
        "install_source_lifecycle_support_missing"
    )
    assert _code(lambda: _verify(_source(lifecycle_support=False))) == (
        "install_source_lifecycle_support_missing"
    )
    assert _code(lambda: _verify(_source(platform_architecture="arm64"))) == (
        "install_source_platform_unsupported"
    )
    assert _code(lambda: _verify(_source(windows_build="19045"))) == (
        "install_source_platform_unsupported"
    )
    assert _code(lambda: _verify(_source(windows_version="6.3"))) == (
        "install_source_platform_unsupported"
    )
    assert _code(lambda: _verify(_source(artifact_digest="sha256:short"))) == (
        "install_source_digest_invalid"
    )


def test_expert_tool_and_secret_material_is_refused() -> None:
    for dependency in sorted(EXPERT_DEPENDENCIES):
        assert _code(lambda: _verify(_source(expert_tools_required=[dependency]))) == (
            "install_source_expert_tools_required"
        )
    assert _code(
        lambda: _verify(_source(update_url="https://example.invalid/pkg.msi"))
    ) == "install_source_shape_invalid"
    assert _code(
        lambda: _verify(
            _source(
                publisher_identity={
                    "publisher_id": PUBLISHER,
                    "authenticode_subject": "https://example.invalid/sign",
                    "endpoint_id": SIGNER.endpoint_id,
                    "verification_key_record": SIGNER.public_key_record(),
                }
            )
        )
    ) == "install_source_hidden_url"


def test_publisher_subject_and_pin_mismatch_fail_closed() -> None:
    assert _code(
        lambda: _verify(expected_publisher="other-release")
    ) == "install_source_publisher_mismatch"
    assert _code(
        lambda: _verify(expected_authenticode_subject="CN=Impostor, O=Impostor")
    ) == "install_source_publisher_mismatch"
    # The install source pins the publisher label and subject; the pinned key
    # digest is the signed package manifest's job (see package_manifest.py).


def test_owner_private_media_requires_the_explicit_unsigned_sentinel() -> None:
    """Owner ruling 2026-09-27 #4: no certificate for owner_private_media.

    A fabricated CN=... subject on the pilot channel is refused, not merely
    tolerated -- the absence of Authenticode identity must be stated, never
    disguised as a real one.
    """
    fabricated = _source(
        publisher_identity={
            "publisher_id": PUBLISHER,
            "authenticode_subject": "CN=Mycelium Release, O=Mycelium",
            "endpoint_id": SIGNER.endpoint_id,
            "verification_key_record": SIGNER.public_key_record(),
        }
    )
    assert _code(lambda: _verify(fabricated)) == "install_source_publisher_invalid"
    assert (
        validate_windows_install_source(_source())["publisher_identity"][
            "authenticode_subject"
        ]
        == AUTHENTICODE_UNSIGNED_SUBJECT
    )


def test_signed_update_channel_still_requires_a_real_authenticode_subject() -> None:
    """``signed_update_channel`` is unaffected by ruling #4 -- Authenticode stays required."""

    real_subject = "CN=Mycelium Release, O=Mycelium"
    signed = _source(
        channel="signed_update_channel",
        publisher_identity={
            "publisher_id": PUBLISHER,
            "authenticode_subject": real_subject,
            "endpoint_id": SIGNER.endpoint_id,
            "verification_key_record": SIGNER.public_key_record(),
        },
    )
    validated = validate_windows_install_source(signed)
    assert validated["publisher_identity"]["authenticode_subject"] == real_subject

    unsigned_on_signed_channel = _source(
        channel="signed_update_channel",
        publisher_identity={
            "publisher_id": PUBLISHER,
            "authenticode_subject": AUTHENTICODE_UNSIGNED_SUBJECT,
            "endpoint_id": SIGNER.endpoint_id,
            "verification_key_record": SIGNER.public_key_record(),
        },
    )
    assert _code(lambda: _verify(unsigned_on_signed_channel)) == (
        "install_source_publisher_invalid"
    )


def test_downgrade_floor_is_enforced_before_identity_or_membership_work() -> None:
    assert _code(lambda: _verify(installed_version="0.0.9")) == (
        "install_source_downgrade_rejected"
    )
    assert _code(
        lambda: _verify(rollback_floor_pin="0.2.0", installed_version="0.2.0")
    ) == "install_source_downgrade_rejected"
    assert _code(lambda: _verify(now="now")) == "install_source_time_invalid"


def test_install_plan_requires_a_verified_source_and_needs_no_expert_tools() -> None:
    verified = _verify()
    plan = install_plan(verified)
    assert plan["protocol"] == PLAN_PROTOCOL
    assert plan["expert_tools_required"] == []
    assert plan["public_listener"] is False
    assert plan["identity_creation_allowed"] is True
    assert plan["inference_eligibility"] is False
    assert plan["route_ready"] is False
    actions = [step["action"] for step in plan["steps"]]
    assert actions[0] == "verify_artifact_digest"
    assert actions[1] == "verify_publisher_signature"
    assert actions[-1] == "create_pairing_request"
    assert "register_least_privilege_windows_service" in actions
    # Identity creation happens only after the package boundary has been checked.
    assert actions.index("create_target_identity") > actions.index(
        "verify_publisher_signature"
    )
    assert _code(lambda: install_plan({"verified": False})) == "install_source_not_verified"
    assert _code(lambda: install_plan(None)) == "install_source_not_verified"


def test_per_user_install_plan_registers_an_opt_in_logon_task_not_an_scm_service() -> None:
    plan = install_plan(_verify(_source(install_mode="windows_service_per_user")))
    actions = [step["action"] for step in plan["steps"]]
    assert "register_per_user_logon_task_opt_in" in actions
    assert "register_least_privilege_windows_service" not in actions
    assert "open_app_and_check_loopback_ui" in actions
    assert actions.index("create_target_identity") > actions.index("verify_publisher_signature")
    assert plan["expert_tools_required"] == [] and plan["public_listener"] is False
