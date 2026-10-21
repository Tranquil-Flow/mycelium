# SPDX-License-Identifier: AGPL-3.0-only
"""Declared Linux platform admission for A13 onboarding (source tranche).

Mirrors the committed macOS slice's admission seam
(``mycelium_a13_macos.app.preflight``): the same ``unsupported_platform``
refusal code, an ``observed_platform`` detail on the refusal, and a report that
never claims a qualification, a launch, or an execution.

The declared distribution set is this tranche's own declaration (the A13
specification requires "signed Linux packages for the declared distributions").
It is declared, not exercised: ``physical_execution`` stays ``not_executed``
because no package has been built, signed, installed, or run on any Linux
member.  The physical target is a device CLASS (any admitted Linux member),
not a named host.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any
import platform
import re

PROTOCOL = "mycelium.a13_linux_platform_admission.v1"

CLAIM_BOUNDARY = (
    "source-only Linux admission: no package was built, signed, published, "
    "deployed, launched or exercised on hardware"
)

#: The distributions a signed Linux package is declared for, with the lowest
#: admitted version and the lifecycle manager the declaration depends on.
ADMITTED_DISTRIBUTIONS: dict[str, dict[str, str]] = {
    "ubuntu": {
        "minimum_version": "24.04",
        "service_manager": "systemd",
        "package_class": "signed_linux_package",
    }
}

ADMITTED_ARCHITECTURES: frozenset[str] = frozenset({"x86_64", "aarch64"})

#: The future physical target of this tranche: any member of the admitted Linux
#: class (owner direction 2026-10-13: gates verify features on whatever
#: eligible devices are present).  Declaring it is a plan, not an observation:
#: ``physical_execution`` stays ``not_executed``.
DECLARED_TARGET: dict[str, Any] = {
    "device_label": "linux-member",
    "device_class": {"os_family": "linux", "architectures": sorted(ADMITTED_ARCHITECTURES)},
    "claim_level": "managed_native_host",
    "physical_execution": "not_executed",
}

REJECTION_CODES: frozenset[str] = frozenset(
    {
        "unsupported_platform",
        "unsupported_architecture",
        "unsupported_distribution",
        "missing_lifecycle_support",
        "platform_observation_invalid",
    }
)

_OS_RELEASE_KEY = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
_DEVICE_LABEL = re.compile(r"^[a-z][a-z0-9-]{0,62}$")


class PlatformAdmissionError(ValueError):
    """Fail-closed admission refusal carrying a stable code and no secret."""

    def __init__(self, code: str, observed_platform: dict | None = None) -> None:
        if code not in REJECTION_CODES:
            raise AssertionError(f"unknown admission code: {code}")
        self.code = code
        self.observed_platform = observed_platform
        super().__init__(code)


def parse_os_release(raw: object) -> dict[str, str]:
    """Parse caller-supplied ``os-release`` text; reject anything ambiguous."""

    if not isinstance(raw, str) or not raw or "\x00" in raw:
        raise PlatformAdmissionError("platform_observation_invalid")
    values: dict[str, str] = {}
    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if "=" not in stripped:
            raise PlatformAdmissionError("platform_observation_invalid")
        key, _, value = stripped.partition("=")
        if _OS_RELEASE_KEY.fullmatch(key) is None:
            raise PlatformAdmissionError("platform_observation_invalid")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if any(character in value for character in "\"'\n\x00"):
            raise PlatformAdmissionError("platform_observation_invalid")
        if key in values:
            raise PlatformAdmissionError("platform_observation_invalid")
        values[key] = value
    if "ID" not in values:
        raise PlatformAdmissionError("platform_observation_invalid")
    return values


def _version_tuple(value: object) -> tuple[int, ...]:
    if not isinstance(value, str) or not value:
        raise PlatformAdmissionError("platform_observation_invalid")
    parts = value.split(".")
    if not all(part.isdigit() for part in parts):
        raise PlatformAdmissionError("platform_observation_invalid")
    return tuple(int(part) for part in parts)


def _at_least(candidate: tuple[int, ...], floor: tuple[int, ...]) -> bool:
    width = max(len(candidate), len(floor))
    return candidate + (0,) * (width - len(candidate)) >= floor + (0,) * (
        width - len(floor)
    )


def linux_platform_declaration() -> dict:
    """The declared Linux boundary of this tranche, with no execution claim."""

    return {
        "protocol": PROTOCOL,
        "declared_distributions": {
            name: dict(spec) for name, spec in sorted(ADMITTED_DISTRIBUTIONS.items())
        },
        "declared_architectures": sorted(ADMITTED_ARCHITECTURES),
        "declared_service_manager": "systemd",
        "package_class": "signed_linux_package",
        "claim_level": DECLARED_TARGET["claim_level"],
        "target_device_label": DECLARED_TARGET["device_label"],
        "physical_execution": DECLARED_TARGET["physical_execution"],
        "claim_boundary": CLAIM_BOUNDARY,
    }


def admit_linux_platform(
    *,
    system: str | None = None,
    machine: str | None = None,
    os_release: object = None,
    service_manager: str | None = None,
    device_label: str | None = None,
) -> dict:
    """Decide whether a declared Linux platform is inside this tranche.

    Caller-supplied observations only.  A refusal happens before any identity or
    membership work, matching the A13 specification's ordering requirement.
    """

    observed_system = platform.system() if system is None else system
    observed_machine = platform.machine() if machine is None else machine
    try:
        parsed: dict[str, str] | None = parse_os_release(os_release)
    except PlatformAdmissionError:
        parsed = None
    observed_platform = {
        "system": observed_system if isinstance(observed_system, str) else None,
        "machine": observed_machine if isinstance(observed_machine, str) else None,
        "distribution_id": None if parsed is None else parsed.get("ID"),
        "distribution_version": None if parsed is None else parsed.get("VERSION_ID"),
        "service_manager": (
            service_manager if isinstance(service_manager, str) else None
        ),
    }

    def refuse(code: str) -> None:
        raise PlatformAdmissionError(code, observed_platform)

    if observed_system != "Linux":
        refuse("unsupported_platform")
    if observed_machine not in ADMITTED_ARCHITECTURES:
        refuse("unsupported_architecture")
    if parsed is None:
        refuse("platform_observation_invalid")
    distribution_id = parsed.get("ID")
    declared = ADMITTED_DISTRIBUTIONS.get(distribution_id)
    if declared is None:
        refuse("unsupported_distribution")
    version = parsed.get("VERSION_ID")
    try:
        candidate = _version_tuple(version)
        floor = _version_tuple(declared["minimum_version"])
    except PlatformAdmissionError:
        refuse("unsupported_distribution")
    if not _at_least(candidate, floor):
        refuse("unsupported_distribution")
    if service_manager != declared["service_manager"]:
        refuse("missing_lifecycle_support")
    label = DECLARED_TARGET["device_label"] if device_label is None else device_label
    if not isinstance(label, str) or _DEVICE_LABEL.fullmatch(label) is None:
        refuse("platform_observation_invalid")
    return {
        "protocol": PROTOCOL,
        "platform": "linux",
        "architecture": observed_machine,
        "distribution_id": distribution_id,
        "distribution_version": version,
        "service_manager": declared["service_manager"],
        "lifecycle_support": True,
        "package_class": declared["package_class"],
        "target_device_label": label,
        "eligibility": "member_only",
        "route_ready": False,
        "physical_execution": DECLARED_TARGET["physical_execution"],
        "claim_boundary": CLAIM_BOUNDARY,
    }


__all__ = [
    "ADMITTED_ARCHITECTURES",
    "ADMITTED_DISTRIBUTIONS",
    "CLAIM_BOUNDARY",
    "DECLARED_TARGET",
    "PROTOCOL",
    "REJECTION_CODES",
    "PlatformAdmissionError",
    "admit_linux_platform",
    "linux_platform_declaration",
    "parse_os_release",
]
