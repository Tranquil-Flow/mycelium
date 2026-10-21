# SPDX-License-Identifier: AGPL-3.0-only
"""Shared core of the A13 install-source checks (Windows and Linux).

The closed-shape validation order, the normal-user expert-dependency exclusions,
the rollback floor and the artifact digest check are identical on every
platform; an :class:`InstallSourceProfile` supplies the platform's protocol,
field set, package class, service manager, install modes and any extra
publisher rule.  Refusal codes are the ones both tranches already froze.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
import hashlib
from pathlib import Path
import re

CHANNELS: frozenset[str] = frozenset({"owner_private_media", "signed_update_channel"})

#: The A13 normal-user exclusions.  Any declared expert dependency refuses.
EXPERT_DEPENDENCIES: frozenset[str] = frozenset(
    {
        "ssh",
        "tailscale",
        "termux",
        "adb",
        "source checkout",
        "repository knowledge",
        "seed url",
        "endpoint id",
        "sidecar",
        "shell",
        "cli setup",
        "manual model copy",
    }
)

_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_SEGMENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_ARTIFACT_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_URL = re.compile(r"[A-Za-z][A-Za-z0-9+.\-]*://")
_BASE_PUBLISHER_FIELDS = frozenset({"publisher_id", "endpoint_id", "verification_key_record"})


class InstallSourceError(ValueError):
    """Fail-closed install-source error carrying a stable code."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def fail(code: str) -> None:
    raise InstallSourceError(code)


def dotted_version(value: object, *, code: str = "install_source_rollback_floor_invalid") -> tuple[int, ...]:
    if not isinstance(value, str) or not value:
        fail(code)
    parts = value.split(".")
    if not all(part.isdigit() for part in parts):
        fail(code)
    return tuple(int(part) for part in parts)


def _walk_strings(value: object):
    if isinstance(value, str):
        yield value
    elif isinstance(value, Mapping):
        for item in value.values():
            yield from _walk_strings(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _walk_strings(item)


@dataclass(frozen=True)
class InstallSourceProfile:
    protocol: str
    fields: frozenset[str]
    package_class: str
    service_manager: str
    install_modes: frozenset[str]
    admitted_architectures: frozenset[str]
    extra_publisher_fields: frozenset[str] = frozenset()
    #: Runs after the publisher id/endpoint checks and before the key record check.
    validate_publisher_extra: Callable[[Mapping, Mapping], None] = lambda _value, _publisher: None
    #: Windows artifact names may not contain a backslash either.
    refuse_backslash_in_artifact_name: bool = False

    @property
    def publisher_fields(self) -> frozenset[str]:
        return _BASE_PUBLISHER_FIELDS | self.extra_publisher_fields


def validate_install_source(source: object, profile: InstallSourceProfile) -> dict:
    """Closed-shape validation of one declared install source."""

    if not isinstance(source, Mapping) or set(source) != profile.fields:
        fail("install_source_shape_invalid")
    value = dict(source)
    if value["protocol"] != profile.protocol:
        fail("install_source_protocol_invalid")
    for text in _walk_strings(value):
        if _URL.search(text) is not None:
            fail("install_source_hidden_url")
    if value["channel"] not in CHANNELS:
        fail("install_source_channel_invalid")
    if value["package_class"] != profile.package_class:
        fail("install_source_package_class_invalid")
    if (
        not isinstance(value["artifact_name"], str)
        or _ARTIFACT_NAME.fullmatch(value["artifact_name"]) is None
        or "/" in value["artifact_name"]
        or (profile.refuse_backslash_in_artifact_name and "\\" in value["artifact_name"])
    ):
        fail("install_source_artifact_invalid")
    if not isinstance(value["artifact_digest"], str) or _DIGEST.fullmatch(
        value["artifact_digest"]
    ) is None:
        fail("install_source_digest_invalid")
    publisher = value["publisher_identity"]
    if not isinstance(publisher, Mapping) or set(publisher) != profile.publisher_fields:
        fail("install_source_publisher_invalid")
    if not _SEGMENT.fullmatch(str(publisher["publisher_id"])) or not _SEGMENT.fullmatch(
        str(publisher["endpoint_id"])
    ):
        fail("install_source_publisher_invalid")
    profile.validate_publisher_extra(value, publisher)
    record = publisher["verification_key_record"]
    if (
        not isinstance(record, Mapping)
        or record.get("algorithm") != "ed25519"
        or not _DIGEST.fullmatch(str(record.get("verification_key_digest")))
    ):
        fail("install_source_publisher_invalid")
    if not _SEGMENT.fullmatch(str(value["minimum_os"])):
        fail("install_source_minimum_os_invalid")
    if value["platform_architecture"] not in profile.admitted_architectures:
        fail("install_source_platform_unsupported")
    if value["service_manager"] != profile.service_manager:
        fail("install_source_lifecycle_support_missing")
    if value["install_mode"] not in profile.install_modes:
        fail("install_source_install_mode_invalid")
    dotted_version(value["rollback_floor"])
    if value["lifecycle_support"] is not True:
        fail("install_source_lifecycle_support_missing")
    tools = value["expert_tools_required"]
    if (
        not isinstance(tools, (list, tuple))
        or any(not isinstance(item, str) or not item for item in tools)
        or len(tools) != 0
    ):
        fail("install_source_expert_tools_required")
    return value


def check_time_and_publisher(value: Mapping, *, now: float, expected_publisher: str) -> Mapping:
    if isinstance(now, bool) or not isinstance(now, (int, float)):
        fail("install_source_time_invalid")
    publisher = value["publisher_identity"]
    if publisher["publisher_id"] != expected_publisher:
        fail("install_source_publisher_mismatch")
    return publisher


def check_floor_and_artifact(
    value: Mapping, *, rollback_floor_pin: str, installed_version: str, artifact_path: Path | None
) -> bool:
    """Rollback floor, then the optional artifact digest; returns whether the artifact was checked."""

    package_floor = dotted_version(value["rollback_floor"])
    pin = dotted_version(rollback_floor_pin)
    installed = dotted_version(installed_version)
    if package_floor < pin or installed < package_floor:
        fail("install_source_downgrade_rejected")
    if artifact_path is None:
        return False
    path = Path(artifact_path)
    if path.is_symlink() or not path.is_file():
        fail("install_source_artifact_unavailable")
    try:
        raw = path.read_bytes()
    except OSError:
        fail("install_source_artifact_unavailable")
    if "sha256:" + hashlib.sha256(raw).hexdigest() != value["artifact_digest"]:
        fail("install_source_digest_mismatch")
    return True


def plan(protocol: str, steps: list[dict]) -> dict:
    """The bounded normal-user plan envelope shared by every platform."""

    return {
        "protocol": protocol,
        "steps": steps,
        "expert_tools_required": [],
        "public_listener": False,
        "identity_creation_allowed": True,
        "activation": "none",
        "inference_eligibility": False,
        "route_ready": False,
    }


def require_verified(verified: object) -> None:
    if (
        not isinstance(verified, Mapping)
        or verified.get("verified") is not True
        or not isinstance(verified.get("artifact_digest"), str)
    ):
        fail("install_source_not_verified")


__all__ = [
    "CHANNELS",
    "EXPERT_DEPENDENCIES",
    "InstallSourceError",
    "InstallSourceProfile",
    "check_floor_and_artifact",
    "check_time_and_publisher",
    "dotted_version",
    "fail",
    "plan",
    "require_verified",
    "validate_install_source",
]
