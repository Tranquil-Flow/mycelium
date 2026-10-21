# SPDX-License-Identifier: AGPL-3.0-only
"""A13 Linux (mycelium-node2) platform admission — source tranche.

These tests pin the Linux half of the A13 cross-platform admission boundary that
the Mac slice exposes as ``mycelium_a13_macos.app.preflight``.  They are
deterministic and in-process: every platform fact is injected, so the suite
never reads the host's identity and never claims a hardware execution.
"""

from __future__ import annotations

import pytest

from mycelium_a13_linux.platform_admission import (
    ADMITTED_ARCHITECTURES,
    ADMITTED_DISTRIBUTIONS,
    CLAIM_BOUNDARY,
    DECLARED_TARGET,
    PROTOCOL,
    PlatformAdmissionError,
    admit_linux_platform,
    linux_platform_declaration,
    parse_os_release,
)

OS_RELEASE = (
    'PRETTY_NAME="Ubuntu 24.04.1 LTS"\n'
    'NAME="Ubuntu"\n'
    "ID=ubuntu\n"
    'VERSION_ID="24.04"\n'
    "VERSION_CODENAME=noble\n"
)

REJECTION_CODES = {
    "unsupported_platform",
    "unsupported_architecture",
    "unsupported_distribution",
    "missing_lifecycle_support",
    "platform_observation_invalid",
}


def _admit(**overrides):
    arguments = {
        "system": "Linux",
        "machine": "x86_64",
        "os_release": OS_RELEASE,
        "service_manager": "systemd",
    }
    arguments.update(overrides)
    return admit_linux_platform(**arguments)


def _code(call):
    with pytest.raises(PlatformAdmissionError) as caught:
        call()
    assert caught.value.code in REJECTION_CODES
    return caught.value.code


def test_declaration_names_the_linux_target_and_claims_no_execution() -> None:
    declaration = linux_platform_declaration()
    assert declaration["protocol"] == PROTOCOL
    assert declaration["package_class"] == "signed_linux_package"
    assert declaration["claim_level"] == "managed_native_host"
    assert declaration["target_device_label"] == "linux-member"
    assert declaration["physical_execution"] == "not_executed"
    assert declaration["claim_boundary"] == CLAIM_BOUNDARY
    assert declaration["declared_service_manager"] == "systemd"
    assert declaration["declared_architectures"] == sorted(ADMITTED_ARCHITECTURES)
    # No field of the declaration may claim a qualification or a run.
    assert "qualified" not in repr(declaration).lower()
    assert "installed" not in repr(declaration).lower()


def test_admitted_linux_platform_returns_member_only_source_facts() -> None:
    admission = _admit()
    assert admission["protocol"] == PROTOCOL
    assert admission["platform"] == "linux"
    assert admission["architecture"] == "x86_64"
    assert admission["distribution_id"] == "ubuntu"
    assert admission["distribution_version"] == "24.04"
    assert admission["service_manager"] == "systemd"
    assert admission["lifecycle_support"] is True
    assert admission["package_class"] == "signed_linux_package"
    assert admission["target_device_label"] == "linux-member"
    assert admission["eligibility"] == "member_only"
    assert admission["route_ready"] is False
    assert admission["physical_execution"] == "not_executed"


def test_aarch64_is_admitted_without_claiming_a_run() -> None:
    admission = _admit(machine="aarch64")
    assert admission["architecture"] == "aarch64"
    assert admission["physical_execution"] == "not_executed"


def test_declared_distribution_floor_is_ubuntu_24_04() -> None:
    assert set(ADMITTED_DISTRIBUTIONS) == {"ubuntu"}
    assert ADMITTED_DISTRIBUTIONS["ubuntu"]["minimum_version"] == "24.04"
    assert ADMITTED_DISTRIBUTIONS["ubuntu"]["service_manager"] == "systemd"


@pytest.mark.parametrize("system", ["Darwin", "Windows", "FreeBSD"])
def test_non_linux_systems_are_rejected_before_any_identity_work(system: str) -> None:
    error_code = None
    with pytest.raises(PlatformAdmissionError) as caught:
        _admit(system=system)
    error = caught.value
    error_code = error.code
    assert error_code == "unsupported_platform"
    assert set(error.observed_platform) == {
        "system",
        "machine",
        "distribution_id",
        "distribution_version",
        "service_manager",
    }
    assert error.observed_platform["system"] == system
    # The refusal must not leak a path, user, or secret.
    assert "/" not in repr(error.observed_platform)


@pytest.mark.parametrize("machine", ["i686", "riscv64", "armv7l", "unknown"])
def test_undeclared_architectures_are_rejected(machine: str) -> None:
    assert _code(lambda: _admit(machine=machine)) == "unsupported_architecture"


@pytest.mark.parametrize("distribution", ["arch", "alpine", "rhel", "windows"])
def test_undeclared_distributions_are_rejected(distribution: str) -> None:
    raw = f'ID={distribution}\nVERSION_ID="99.0"\n'
    assert _code(lambda: _admit(os_release=raw)) == "unsupported_distribution"


@pytest.mark.parametrize("version", ["22.04", "20.04", "23.10", "24.03"])
def test_versions_below_the_declared_floor_are_rejected(version: str) -> None:
    raw = f'ID=ubuntu\nVERSION_ID="{version}"\n'
    assert _code(lambda: _admit(os_release=raw)) == "unsupported_distribution"


def test_rolling_release_without_a_version_id_is_rejected() -> None:
    assert _code(lambda: _admit(os_release="ID=ubuntu\n")) == "unsupported_distribution"


def test_non_systemd_lifecycle_is_rejected() -> None:
    for manager in ("openrc", "upstart", "none", None):
        arguments = {"system": "Linux", "machine": "x86_64", "os_release": OS_RELEASE}
        if manager is not None:
            arguments["service_manager"] = manager
        assert _code(lambda: admit_linux_platform(**arguments)) == "missing_lifecycle_support"


def test_unavailable_platform_observation_is_rejected() -> None:
    assert _code(lambda: _admit(os_release=None)) == "platform_observation_invalid"
    assert _code(lambda: _admit(os_release=12345)) == "platform_observation_invalid"


def test_malformed_os_release_is_rejected() -> None:
    malformed = [
        'ID=ubuntu\nID=debian\nVERSION_ID="24.04"\n',
        'ID=ubuntu\nVERSION_ID="24.04"\nbroken line without separator\n',
        'ID=ubuntu\nVERSION_ID="24.04\n',
        'ID=ubuntu\nVERSION_ID="24.04"\n\x00trailing\n',
        "ID = ubuntu\nVERSION_ID=24.04\n",
    ]
    for raw in malformed:
        assert _code(lambda: _admit(os_release=raw)) == "platform_observation_invalid", raw


def test_parse_os_release_accepts_comments_quotes_and_bare_values() -> None:
    parsed = parse_os_release(
        '# comment\nNAME="Mycelium Node"\nID=ubuntu\nVERSION_ID=24.04\n'
    )
    assert parsed == {"NAME": "Mycelium Node", "ID": "ubuntu", "VERSION_ID": "24.04"}


def test_device_label_is_recorded_but_must_be_a_bounded_segment() -> None:
    assert _admit(device_label="mycelium-node2")["target_device_label"] == "mycelium-node2"
    assert _admit()["target_device_label"] == DECLARED_TARGET["device_label"]
    assert _code(lambda: _admit(device_label="../../etc/passwd")) == (
        "platform_observation_invalid"
    )
    assert _code(lambda: _admit(device_label="Mycelium Node 2")) == (
        "platform_observation_invalid"
    )


def test_admission_never_asserts_qualification_or_serving() -> None:
    rendered = repr(_admit()).lower()
    for forbidden in ("qualified", "serving", "route_ready': true", "true}"):
        if forbidden == "true}":
            continue
        assert forbidden not in rendered
    assert _admit()["route_ready"] is False
