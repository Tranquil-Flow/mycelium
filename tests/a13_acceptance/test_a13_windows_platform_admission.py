# SPDX-License-Identifier: AGPL-3.0-only
"""A13 Windows platform admission — source tranche.

The Windows half of the A13 cross-platform admission boundary.  Every platform
fact is a caller-supplied observation (the registry values an installer would
read), so the suite never reads the host's identity and never claims a run.
"""

from __future__ import annotations

import pytest

from mycelium_a13_windows.platform_admission import (
    ADMITTED_ARCHITECTURES,
    ADMITTED_RELEASES,
    CLAIM_BOUNDARY,
    DECLARED_TARGET,
    PROTOCOL,
    PlatformAdmissionError,
    admit_windows_platform,
    parse_windows_registry_values,
    windows_platform_declaration,
)

REGISTRY_VALUES = (
    "CurrentMajorVersionNumber=10\n"
    "CurrentMinorVersionNumber=0\n"
    "CurrentBuildNumber=22631\n"
    "UBR=3155\n"
    'ProductName="Windows 11 Pro"\n'
    "DisplayVersion=23H2\n"
    "EditionID=Professional\n"
)

REJECTION_CODES = {
    "unsupported_platform",
    "unsupported_architecture",
    "unsupported_release",
    "missing_lifecycle_support",
    "platform_observation_invalid",
}


def _admit(**overrides):
    arguments = {
        "system": "Windows",
        "machine": "AMD64",
        "registry_values": REGISTRY_VALUES,
        "service_manager": "windows_service",
    }
    arguments.update(overrides)
    return admit_windows_platform(**arguments)


def _code(call):
    with pytest.raises(PlatformAdmissionError) as caught:
        call()
    assert caught.value.code in REJECTION_CODES
    return caught.value.code


def test_declaration_names_the_windows_target_and_claims_no_execution() -> None:
    declaration = windows_platform_declaration()
    assert declaration["protocol"] == PROTOCOL
    assert declaration["package_class"] == "signed_windows_installer"
    assert declaration["claim_level"] == "managed_native_host"
    assert declaration["target_device_label"] == "mycelium-win-1"
    assert declaration["physical_execution"] == "not_executed"
    assert declaration["claim_boundary"] == CLAIM_BOUNDARY
    assert declaration["declared_service_manager"] == "windows_service"
    assert declaration["declared_architectures"] == sorted(ADMITTED_ARCHITECTURES)
    rendered = repr(declaration).lower()
    assert "qualified" not in rendered
    assert "installed" not in rendered


def test_claim_boundary_states_the_installer_is_authenticode_unsigned() -> None:
    """Owner ruling 2026-09-27 #4: the claim boundary must say so, not imply it."""

    assert "authenticode-unsigned" in CLAIM_BOUNDARY.lower()
    assert "owner_private_media" in CLAIM_BOUNDARY
    assert "ed25519" in CLAIM_BOUNDARY.lower()


def test_admitted_windows_platform_returns_member_only_source_facts() -> None:
    admission = _admit()
    assert admission["protocol"] == PROTOCOL
    assert admission["platform"] == "windows"
    assert admission["architecture"] == "x86_64"
    assert admission["release_id"] == "windows"
    assert admission["release_version"] == "10.0"
    assert admission["release_build"] == "22631"
    assert admission["service_manager"] == "windows_service"
    assert admission["lifecycle_support"] is True
    assert admission["package_class"] == "signed_windows_installer"
    assert admission["target_device_label"] == "mycelium-win-1"
    assert admission["eligibility"] == "member_only"
    assert admission["route_ready"] is False
    assert admission["physical_execution"] == "not_executed"


def test_declared_release_floor_is_windows_10_build_19045() -> None:
    assert set(ADMITTED_RELEASES) == {"windows"}
    assert ADMITTED_RELEASES["windows"]["minimum_version"] == "10.0"
    assert ADMITTED_RELEASES["windows"]["minimum_build"] == "19045"
    assert ADMITTED_RELEASES["windows"]["service_manager"] == "windows_service"


def test_windows_10_hosts_are_admitted_without_claiming_a_run() -> None:
    admission = _admit(
        registry_values=(
            "CurrentMajorVersionNumber=10\n"
            "CurrentMinorVersionNumber=0\n"
            "CurrentBuildNumber=19045\n"
            'ProductName="Windows 10 Pro"\n'
        )
    )
    assert admission["release_build"] == "19045"
    assert admission["physical_execution"] == "not_executed"


@pytest.mark.parametrize("system", ["Darwin", "Linux", "FreeBSD", "windows"])
def test_non_windows_systems_are_rejected_before_any_identity_work(system: str) -> None:
    with pytest.raises(PlatformAdmissionError) as caught:
        _admit(system=system)
    error = caught.value
    assert error.code == "unsupported_platform"
    assert set(error.observed_platform) == {
        "system",
        "machine",
        "release_version",
        "release_build",
        "service_manager",
    }
    assert error.observed_platform["system"] == system
    assert "/" not in repr(error.observed_platform)


@pytest.mark.parametrize("machine", ["ARM64", "arm64", "i686", "aarch64", "unknown"])
def test_undeclared_architectures_are_rejected(machine: str) -> None:
    assert _code(lambda: _admit(machine=machine)) == "unsupported_architecture"


@pytest.mark.parametrize("version", [("6", "3"), ("6", "1"), ("5", "1")])
def test_releases_below_the_declared_floor_are_rejected(version: tuple[str, str]) -> None:
    raw = (
        f"CurrentMajorVersionNumber={version[0]}\n"
        f"CurrentMinorVersionNumber={version[1]}\n"
        "CurrentBuildNumber=9600\n"
    )
    assert _code(lambda: _admit(registry_values=raw)) == "unsupported_release"


@pytest.mark.parametrize("build", ["17763", "18363", "19044"])
def test_builds_below_the_declared_floor_are_rejected(build: str) -> None:
    raw = (
        "CurrentMajorVersionNumber=10\n"
        "CurrentMinorVersionNumber=0\n"
        f"CurrentBuildNumber={build}\n"
    )
    assert _code(lambda: _admit(registry_values=raw)) == "unsupported_release"


def test_a_release_without_a_version_or_build_is_rejected() -> None:
    assert _code(
        lambda: _admit(registry_values='ProductName="Windows 11 Pro"\n')
    ) == "unsupported_release"
    assert _code(
        lambda: _admit(
            registry_values=(
                "CurrentMajorVersionNumber=10\n"
                "CurrentMinorVersionNumber=0\n"
                "CurrentBuildNumber=not-a-build\n"
            )
        )
    ) == "unsupported_release"


def test_non_service_lifecycle_is_rejected() -> None:
    for manager in ("systemd", "launchd", "scheduled_task", "none", None):
        arguments = {
            "system": "Windows",
            "machine": "AMD64",
            "registry_values": REGISTRY_VALUES,
        }
        if manager is not None:
            arguments["service_manager"] = manager
        assert _code(lambda: admit_windows_platform(**arguments)) == (
            "missing_lifecycle_support"
        )


def test_unavailable_or_malformed_platform_observation_is_rejected() -> None:
    assert _code(lambda: _admit(registry_values=None)) == "platform_observation_invalid"
    assert _code(lambda: _admit(registry_values=12345)) == "platform_observation_invalid"
    malformed = [
        "CurrentBuildNumber=22631\nCurrentBuildNumber=19045\n",
        "broken line without separator\n",
        "=22631\n",
        "CurrentBuildNumber=22631\x00\n",
        "CurrentBuild Number=22631\n",
    ]
    for raw in malformed:
        assert _code(lambda: _admit(registry_values=raw)) == (
            "platform_observation_invalid"
        ), raw


def test_parse_windows_registry_values_accepts_quotes_comments_and_bare_values() -> None:
    parsed = parse_windows_registry_values(
        "# comment\n"
        'ProductName="Windows 11 Pro"\n'
        "CurrentBuildNumber=22631\n"
        "EditionID=Professional\n"
    )
    assert parsed == {
        "ProductName": "Windows 11 Pro",
        "CurrentBuildNumber": "22631",
        "EditionID": "Professional",
    }


def test_device_label_is_recorded_but_must_be_a_bounded_segment() -> None:
    assert _admit(device_label="mycelium-win-1")["target_device_label"] == "mycelium-win-1"
    assert _admit()["target_device_label"] == DECLARED_TARGET["device_label"]
    assert _code(lambda: _admit(device_label="..\\..\\Windows\\System32")) == (
        "platform_observation_invalid"
    )
    assert _code(lambda: _admit(device_label="Mycelium Win 1")) == (
        "platform_observation_invalid"
    )


def test_admission_never_asserts_qualification_or_serving() -> None:
    admission = _admit()
    rendered = repr(admission).lower()
    for forbidden in ("qualified", "serving", "accelerated"):
        assert forbidden not in rendered
    assert admission["route_ready"] is False
    assert admission["eligibility"] == "member_only"
