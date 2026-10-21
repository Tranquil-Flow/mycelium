# SPDX-License-Identifier: AGPL-3.0-only
"""Declared Windows platform admission for A13 onboarding (source tranche).

Mirrors the landed Linux slice (``mycelium_a13_linux.platform_admission``) and
the committed macOS slice's admission seam: the same fail-closed refusal style,
an ``observed_platform`` detail on the refusal, and a report that never claims a
qualification, an installation, or an execution.

The declared release floor, architecture set and lifecycle manager are this
tranche's own declaration (the A13 specification requires "a signed Windows
installer plus a least-privilege Windows service").  They are declared, not
exercised: ``physical_execution`` stays ``not_executed`` because no installer has
been built, signed, installed, or run on Windows hardware.
"""

from __future__ import annotations

from collections.abc import Mapping
import re

PROTOCOL = "mycelium.a13_windows_platform_admission.v1"

CLAIM_BOUNDARY = (
    "source-only Windows admission: no installer was built, signed, published, "
    "deployed, launched or exercised on Windows hardware; per owner ruling "
    "2026-09-27 #4 (no certificate for owner_private_media), any installer this "
    "tranche builds ships through the owner_private_media pilot channel and is "
    "Authenticode-UNSIGNED (expect an unknown-publisher/SmartScreen warning) -- "
    "integrity is bound by the pinned Ed25519 verification key, never a "
    "code-signing certificate"
)

#: The declared Windows release with the lowest admitted version and build, the
#: lifecycle manager the declaration depends on, and the package class.
ADMITTED_RELEASES: dict[str, dict[str, str]] = {
    "windows": {
        "minimum_version": "10.0",
        "minimum_build": "19045",
        "service_manager": "windows_service",
        "package_class": "signed_windows_installer",
    }
}

ADMITTED_ARCHITECTURES: frozenset[str] = frozenset({"x86_64"})

#: The named future physical target of this tranche.  Naming it is a plan, not an
#: observation: ``physical_execution`` stays ``not_executed``.
DECLARED_TARGET: dict[str, str] = {
    "device_label": "mycelium-win-1",
    "claim_level": "managed_native_host",
    "physical_execution": "not_executed",
}

REJECTION_CODES: frozenset[str] = frozenset(
    {
        "unsupported_platform",
        "unsupported_architecture",
        "unsupported_release",
        "missing_lifecycle_support",
        "platform_observation_invalid",
    }
)

#: The registry values an installer reads from
#: ``HKLM\\SOFTWARE\\Microsoft\\Windows NT\\CurrentVersion``.
_VERSION_KEYS: tuple[str, ...] = (
    "CurrentMajorVersionNumber",
    "CurrentMinorVersionNumber",
)
_BUILD_KEY = "CurrentBuildNumber"
_OBSERVATION_KEYS: frozenset[str] = frozenset(
    {
        "CurrentMajorVersionNumber",
        "CurrentMinorVersionNumber",
        "CurrentBuildNumber",
        "UBR",
        "ProductName",
        "DisplayVersion",
        "EditionID",
    }
)
_KEY = re.compile(r"^[A-Z][A-Za-z0-9]{0,63}$")
_DEVICE_LABEL = re.compile(r"^[a-z][a-z0-9-]{0,62}$")
_MACHINES = frozenset({"AMD64", "x86_64"})


class PlatformAdmissionError(ValueError):
    """Fail-closed admission refusal carrying a stable code and no secret."""

    def __init__(self, code: str, observed_platform: dict | None = None) -> None:
        if code not in REJECTION_CODES:
            raise AssertionError(f"unknown admission code: {code}")
        self.code = code
        self.observed_platform = observed_platform
        super().__init__(code)


def parse_windows_registry_values(raw: object) -> dict[str, str]:
    """Parse caller-supplied Windows registry text; reject anything ambiguous."""

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
        if _KEY.fullmatch(key) is None or key not in _OBSERVATION_KEYS:
            raise PlatformAdmissionError("platform_observation_invalid")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if any(character in value for character in "\"'\n\x00"):
            raise PlatformAdmissionError("platform_observation_invalid")
        if key in values:
            raise PlatformAdmissionError("platform_observation_invalid")
        values[key] = value
    if not values:
        raise PlatformAdmissionError("platform_observation_invalid")
    return values


def _digits(value: object) -> str | None:
    if not isinstance(value, str) or not value or not value.isdigit():
        return None
    return value


def _version_tuple(major: object, minor: object) -> tuple[int, int] | None:
    major_digits = _digits(major)
    minor_digits = _digits(minor)
    if major_digits is None or minor_digits is None:
        return None
    return (int(major_digits), int(minor_digits))


def windows_platform_declaration() -> dict:
    """The declared Windows boundary of this tranche, with no execution claim."""

    return {
        "protocol": PROTOCOL,
        "declared_releases": {
            name: dict(spec) for name, spec in sorted(ADMITTED_RELEASES.items())
        },
        "declared_architectures": sorted(ADMITTED_ARCHITECTURES),
        "declared_service_manager": "windows_service",
        "package_class": ADMITTED_RELEASES["windows"]["package_class"],
        "claim_level": DECLARED_TARGET["claim_level"],
        "target_device_label": DECLARED_TARGET["device_label"],
        "physical_execution": DECLARED_TARGET["physical_execution"],
        "claim_boundary": CLAIM_BOUNDARY,
    }


def admit_windows_platform(
    *,
    system: str | None = None,
    machine: str | None = None,
    registry_values: object = None,
    service_manager: str | None = None,
    device_label: str | None = None,
) -> dict:
    """Decide whether a declared Windows platform is inside this tranche.

    Caller-supplied observations only.  A refusal happens before any identity or
    membership work, matching the A13 specification's ordering requirement.
    """

    observed_system = "Windows" if system is None else system
    observed_machine = machine
    parsed: dict[str, str] | None
    try:
        parsed = parse_windows_registry_values(registry_values)
    except PlatformAdmissionError:
        parsed = None
    major = None if parsed is None else parsed.get("CurrentMajorVersionNumber")
    minor = None if parsed is None else parsed.get("CurrentMinorVersionNumber")
    version = _version_tuple(major, minor)
    observed_platform = {
        "system": observed_system if isinstance(observed_system, str) else None,
        "machine": observed_machine if isinstance(observed_machine, str) else None,
        "release_version": None if version is None else f"{version[0]}.{version[1]}",
        "release_build": None if parsed is None else parsed.get(_BUILD_KEY),
        "service_manager": (
            service_manager if isinstance(service_manager, str) else None
        ),
    }

    def refuse(code: str) -> None:
        raise PlatformAdmissionError(code, observed_platform)

    if observed_system != "Windows":
        refuse("unsupported_platform")
    if observed_machine not in _MACHINES:
        refuse("unsupported_architecture")
    if parsed is None:
        refuse("platform_observation_invalid")
    declared = ADMITTED_RELEASES["windows"]
    if version is None:
        refuse("unsupported_release")
    floor_version = tuple(
        int(part) for part in declared["minimum_version"].split(".")
    )
    if version < floor_version:
        refuse("unsupported_release")
    build = _digits(parsed.get(_BUILD_KEY))
    if build is None or int(build) < int(declared["minimum_build"]):
        refuse("unsupported_release")
    if service_manager != declared["service_manager"]:
        refuse("missing_lifecycle_support")
    label = DECLARED_TARGET["device_label"] if device_label is None else device_label
    if not isinstance(label, str) or _DEVICE_LABEL.fullmatch(label) is None:
        refuse("platform_observation_invalid")
    product_name = parsed.get("ProductName")
    return {
        "protocol": PROTOCOL,
        "platform": "windows",
        "architecture": "x86_64",
        "release_id": "windows",
        "release_version": f"{version[0]}.{version[1]}",
        "release_build": build,
        "product_name": product_name if isinstance(product_name, str) else None,
        "service_manager": declared["service_manager"],
        "lifecycle_support": True,
        "package_class": declared["package_class"],
        "target_device_label": label,
        "eligibility": "member_only",
        "route_ready": False,
        "physical_execution": DECLARED_TARGET["physical_execution"],
        "claim_boundary": CLAIM_BOUNDARY,
    }


def release_floor_for(document: Mapping[str, object]) -> tuple[tuple[int, ...], int] | None:
    """The declared version/build floor, for callers that validate a package."""

    if not isinstance(document, Mapping):
        return None
    declared = ADMITTED_RELEASES.get("windows")
    if declared is None:
        return None
    return (
        tuple(int(part) for part in declared["minimum_version"].split(".")),
        int(declared["minimum_build"]),
    )


__all__ = [
    "ADMITTED_ARCHITECTURES",
    "ADMITTED_RELEASES",
    "CLAIM_BOUNDARY",
    "DECLARED_TARGET",
    "PROTOCOL",
    "REJECTION_CODES",
    "PlatformAdmissionError",
    "admit_windows_platform",
    "parse_windows_registry_values",
    "release_floor_for",
    "windows_platform_declaration",
]
