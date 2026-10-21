# SPDX-License-Identifier: AGPL-3.0-only
"""macOS install source for A13 (``signed_macos_package``) over the shared core.

Same closed shape, validation order, expert-dependency exclusions and rollback
floor as Windows and Linux (``mycelium_a13_packaging.install_source``).  The
publisher carries ``developer_id_team``: on ``owner_private_media`` it must be
the explicit unsigned sentinel until the owner rules on Apple Developer ID; on
``signed_update_channel`` it must be a real ten-character Team ID.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
import re

from mycelium_a13_packaging.install_source import (
    CHANNELS,
    EXPERT_DEPENDENCIES,
    InstallSourceError,
    InstallSourceProfile,
    check_floor_and_artifact,
    check_time_and_publisher,
    fail as _fail,
    validate_install_source as _validate,
)

from .package_manifest import ADMITTED_ARCHITECTURES, ADMITTED_VERSIONS, DEVELOPER_ID_UNSIGNED

PROTOCOL = "mycelium.a13_macos_install_source.v1"
INSTALL_MODES: frozenset[str] = frozenset({"launchd_user"})
FIELDS: frozenset[str] = frozenset(
    {
        "protocol",
        "channel",
        "package_class",
        "artifact_name",
        "artifact_digest",
        "publisher_identity",
        "minimum_os",
        "platform_architecture",
        "macos_version",
        "service_manager",
        "install_mode",
        "rollback_floor",
        "lifecycle_support",
        "expert_tools_required",
    }
)
_TEAM_ID = re.compile(r"^[A-Z0-9]{10}$")


def _validate_team(value: Mapping, publisher: Mapping) -> None:
    team = publisher["developer_id_team"]
    if value["channel"] == "owner_private_media":
        if team != DEVELOPER_ID_UNSIGNED:
            _fail("install_source_publisher_invalid")
    elif not isinstance(team, str) or not _TEAM_ID.fullmatch(team):
        _fail("install_source_publisher_invalid")


PROFILE = InstallSourceProfile(
    protocol=PROTOCOL,
    fields=FIELDS,
    package_class="signed_macos_package",
    service_manager="launchd",
    install_modes=INSTALL_MODES,
    admitted_architectures=frozenset(ADMITTED_ARCHITECTURES),
    extra_publisher_fields=frozenset({"developer_id_team"}),
    validate_publisher_extra=_validate_team,
)


def validate_macos_install_source(source: object) -> dict:
    return _validate(source, PROFILE)


def verify_macos_install_source(source: object, *, expected_publisher: str, expected_developer_id_team: str,
                                expected_architecture: str, observed_macos_version: str,
                                installed_version: str, rollback_floor_pin: str, now: float,
                                artifact_path: Path | None = None) -> dict:
    value = validate_macos_install_source(source)
    publisher = check_time_and_publisher(value, now=now, expected_publisher=expected_publisher)
    if publisher["developer_id_team"] != expected_developer_id_team:
        _fail("install_source_publisher_mismatch")
    if value["platform_architecture"] != expected_architecture or observed_macos_version not in ADMITTED_VERSIONS:
        _fail("install_source_platform_unsupported")
    artifact_verified = check_floor_and_artifact(value, rollback_floor_pin=rollback_floor_pin,
                                                 installed_version=installed_version, artifact_path=artifact_path)
    return {
        "verified": True,
        "protocol": PROTOCOL,
        "channel": value["channel"],
        "artifact_name": value["artifact_name"],
        "artifact_digest": value["artifact_digest"],
        "artifact_verified": artifact_verified,
        "publisher_id": publisher["publisher_id"],
        "developer_id_team": publisher["developer_id_team"],
        "platform_architecture": value["platform_architecture"],
        "install_mode": value["install_mode"],
        "rollback_floor": value["rollback_floor"],
        "identity_creation_allowed": True,
        "eligibility": "member_only",
        "activation": "none",
        "inference_eligibility": False,
        "route_ready": False,
    }


__all__ = ["CHANNELS", "DEVELOPER_ID_UNSIGNED", "EXPERT_DEPENDENCIES", "FIELDS", "INSTALL_MODES", "PROTOCOL",
           "InstallSourceError", "validate_macos_install_source", "verify_macos_install_source"]
