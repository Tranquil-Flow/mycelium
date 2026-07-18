from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any


MANIFEST_PROTOCOL = "mycelium.lane_audit_manifest.v1"
_NAME_RE = re.compile(r"[a-z0-9][a-z0-9._-]*\Z")
_SHA_RE = re.compile(r"[0-9a-f]{40}\Z")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")


class ManifestError(ValueError):
    """Raised when a lane-audit manifest is not canonical or safe to evaluate."""


@dataclass(frozen=True, slots=True)
class LaneSpec:
    name: str
    branch: str
    expected_base: str
    allowed_paths: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class AuditManifest:
    protocol: str
    target_branch: str
    lanes: tuple[LaneSpec, ...]


def _require_exact_keys(value: dict[str, Any], expected: set[str], context: str) -> None:
    actual = set(value)
    missing = sorted(expected - actual)
    unknown = sorted(actual - expected)
    if missing:
        raise ManifestError(f"{context} missing fields: {', '.join(missing)}")
    if unknown:
        raise ManifestError(f"{context} has unknown fields: {', '.join(unknown)}")


def _validate_branch(value: Any, context: str) -> str:
    if not isinstance(value, str) or not value:
        raise ManifestError(f"{context} must be a non-empty string")
    if _CONTROL_RE.search(value) or any(character.isspace() for character in value):
        raise ManifestError(f"{context} contains whitespace or control characters")
    if (
        value.startswith(("-", "/", "."))
        or value.endswith(("/", ".", ".lock"))
        or "//" in value
        or ".." in value
        or "@{" in value
        or any(character in value for character in "\\~^:?*[")
        or any(part.startswith(".") for part in value.split("/"))
    ):
        raise ManifestError(f"{context} is not a canonical local branch name")
    return value


def _validate_allowed_path(value: Any, context: str) -> str:
    if not isinstance(value, str) or not value:
        raise ManifestError(f"{context} allowed path must be a non-empty string")
    if _CONTROL_RE.search(value) or "\\" in value or value.startswith("/"):
        raise ManifestError(f"{context} allowed path is not canonical relative POSIX syntax")
    parts = value.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise ManifestError(f"{context} allowed path is not canonical relative POSIX syntax")
    return value


def manifest_from_dict(payload: Any) -> AuditManifest:
    if not isinstance(payload, dict):
        raise ManifestError("manifest must be a JSON object")
    _require_exact_keys(payload, {"protocol", "target_branch", "lanes"}, "manifest")

    if payload["protocol"] != MANIFEST_PROTOCOL:
        raise ManifestError(f"manifest protocol must be {MANIFEST_PROTOCOL!r}")
    target_branch = _validate_branch(payload["target_branch"], "target_branch")

    lanes_raw = payload["lanes"]
    if not isinstance(lanes_raw, list) or not lanes_raw:
        raise ManifestError("manifest lanes must be a non-empty array")

    lanes: list[LaneSpec] = []
    names: set[str] = set()
    branches: set[str] = set()
    for index, lane_raw in enumerate(lanes_raw):
        context = f"lane[{index}]"
        if not isinstance(lane_raw, dict):
            raise ManifestError(f"{context} must be a JSON object")
        _require_exact_keys(
            lane_raw,
            {"name", "branch", "expected_base", "allowed_paths"},
            context,
        )

        name = lane_raw["name"]
        if not isinstance(name, str) or not _NAME_RE.fullmatch(name):
            raise ManifestError(f"{context} name must use lowercase canonical identifier syntax")
        if name in names:
            raise ManifestError(f"duplicate lane name: {name}")
        names.add(name)

        branch = _validate_branch(lane_raw["branch"], f"{context} branch")
        if branch in branches:
            raise ManifestError(f"duplicate lane branch: {branch}")
        branches.add(branch)

        expected_base = lane_raw["expected_base"]
        if not isinstance(expected_base, str) or not _SHA_RE.fullmatch(expected_base):
            raise ManifestError(f"{context} expected_base must be a full lowercase commit SHA")

        allowed_raw = lane_raw["allowed_paths"]
        if not isinstance(allowed_raw, list) or not allowed_raw:
            raise ManifestError(f"{context} allowed_paths must be a non-empty array")
        allowed_paths = tuple(
            _validate_allowed_path(path, context) for path in allowed_raw
        )
        if len(set(allowed_paths)) != len(allowed_paths):
            raise ManifestError(f"{context} contains duplicate allowed path patterns")

        lanes.append(
            LaneSpec(
                name=name,
                branch=branch,
                expected_base=expected_base,
                allowed_paths=tuple(sorted(allowed_paths)),
            )
        )

    return AuditManifest(
        protocol=MANIFEST_PROTOCOL,
        target_branch=target_branch,
        lanes=tuple(sorted(lanes, key=lambda lane: lane.name)),
    )


def load_manifest(path: str | Path) -> AuditManifest:
    source = Path(path)
    try:
        payload = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ManifestError(f"could not read manifest: {exc}") from exc
    return manifest_from_dict(payload)
