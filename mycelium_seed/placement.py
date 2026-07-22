# SPDX-License-Identifier: AGPL-3.0-or-later
"""Placement-source seam for frozen and planner-derived seed decisions."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
from typing import Any, Protocol


FROZEN_PLACEMENT_PROTOCOL = "mycelium.seed.frozen_placement.v1"
_PLACEMENT_PROVENANCES = frozenset({"frozen_fixture", "planner_v2"})
_SEGMENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_MAX_FIXTURE_BYTES = 1 << 20
_MAX_ASSIGNMENTS = 256


class PlacementError(RuntimeError):
    """Stable placement-source or decision validation failure."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _segment(value: Any, *, code: str) -> str:
    if not isinstance(value, str) or _SEGMENT_RE.fullmatch(value) is None:
        raise PlacementError(code)
    return value


def _duplicate_rejecting_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise PlacementError("placement_fixture_duplicate_field")
        result[key] = value
    return result


def _read_regular_file(path: Path) -> bytes:
    flags = os.O_RDONLY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except (OSError, TypeError, ValueError) as exc:
        raise PlacementError("placement_fixture_unreadable") from exc
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_size > _MAX_FIXTURE_BYTES:
            raise PlacementError("placement_fixture_invalid_file")
        chunks: list[bytes] = []
        remaining = _MAX_FIXTURE_BYTES + 1
        while remaining > 0:
            chunk = os.read(descriptor, min(remaining, 64 * 1024))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        after = os.fstat(descriptor)
        if (
            len(raw) > _MAX_FIXTURE_BYTES
            or before.st_dev != after.st_dev
            or before.st_ino != after.st_ino
            or before.st_size != after.st_size
            or before.st_mtime_ns != after.st_mtime_ns
            or len(raw) != after.st_size
        ):
            raise PlacementError("placement_fixture_changed")
        return raw
    except OSError as exc:
        raise PlacementError("placement_fixture_unreadable") from exc
    finally:
        os.close(descriptor)


@dataclass(frozen=True)
class MemberRecord:
    """Backend-neutral member snapshot presented to a placement source."""

    node_id: str
    endpoint_id: str
    peer_class: str
    runtime_capability: Mapping[str, Any]
    generation: int
    lease_expires_at: float
    activation_eligible: bool

    def __post_init__(self) -> None:
        _segment(self.node_id, code="placement_member_invalid")
        _segment(self.endpoint_id, code="placement_member_invalid")
        _segment(self.peer_class, code="placement_member_invalid")
        if not isinstance(self.runtime_capability, Mapping):
            raise PlacementError("placement_member_invalid")
        if (
            isinstance(self.generation, bool)
            or not isinstance(self.generation, int)
            or self.generation < 1
            or isinstance(self.lease_expires_at, bool)
            or not isinstance(self.lease_expires_at, (int, float))
            or not math.isfinite(float(self.lease_expires_at))
            or not isinstance(self.activation_eligible, bool)
        ):
            raise PlacementError("placement_member_invalid")
        try:
            capability = json.loads(
                json.dumps(
                    dict(self.runtime_capability),
                    sort_keys=True,
                    separators=(",", ":"),
                    allow_nan=False,
                )
            )
        except (TypeError, ValueError) as exc:
            raise PlacementError("placement_member_invalid") from exc
        object.__setattr__(self, "runtime_capability", capability)
        object.__setattr__(self, "lease_expires_at", float(self.lease_expires_at))


@dataclass(frozen=True)
class PlacementDecision:
    """Placement intent produced by one named source; never route readiness."""

    placement_provenance: str
    placement_id: str
    assignments: tuple[Mapping[str, Any], ...]
    source_digest: str

    def __post_init__(self) -> None:
        if self.placement_provenance not in _PLACEMENT_PROVENANCES:
            raise PlacementError("placement_provenance_invalid")
        _segment(self.placement_id, code="placement_id_invalid")
        if not isinstance(self.source_digest, str) or _DIGEST_RE.fullmatch(
            self.source_digest
        ) is None:
            raise PlacementError("placement_source_digest_invalid")
        if (
            not isinstance(self.assignments, tuple)
            or not self.assignments
            or len(self.assignments) > _MAX_ASSIGNMENTS
        ):
            raise PlacementError("placement_assignments_invalid")
        normalized: list[dict[str, Any]] = []
        seen_node_ids: set[str] = set()
        for assignment in self.assignments:
            if not isinstance(assignment, Mapping):
                raise PlacementError("placement_assignment_invalid")
            try:
                copied = json.loads(
                    json.dumps(
                        dict(assignment),
                        sort_keys=True,
                        separators=(",", ":"),
                        allow_nan=False,
                    )
                )
            except (TypeError, ValueError) as exc:
                raise PlacementError("placement_assignment_invalid") from exc
            node_id = _segment(
                copied.get("node_id"),
                code="placement_assignment_invalid",
            )
            _segment(
                copied.get("assignment_id"),
                code="placement_assignment_invalid",
            )
            if node_id in seen_node_ids:
                raise PlacementError("placement_assignment_duplicate_member")
            seen_node_ids.add(node_id)
            normalized.append(copied)
        object.__setattr__(self, "assignments", tuple(normalized))


class PlacementSource(Protocol):
    """Compile placement intent from a deterministic member snapshot."""

    def compile(self, members: Sequence[MemberRecord]) -> PlacementDecision: ...


class FrozenPlacementSource:
    """Load and pin a checked-in fixture with frozen-fixture provenance."""

    def __init__(self, fixture_path: str | os.PathLike[str]) -> None:
        self.fixture_path = Path(fixture_path)
        raw = _read_regular_file(self.fixture_path)
        self._source_digest = "sha256:" + hashlib.sha256(raw).hexdigest()
        self._document = self._parse(raw)

    @staticmethod
    def _parse(raw: bytes) -> dict[str, Any]:
        try:
            document = json.loads(
                raw.decode("utf-8"),
                object_pairs_hook=_duplicate_rejecting_object,
            )
        except PlacementError:
            raise
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise PlacementError("placement_fixture_invalid_json") from exc
        if not isinstance(document, dict) or set(document) != {
            "protocol",
            "placement_id",
            "assignments",
        }:
            raise PlacementError("placement_fixture_fields_invalid")
        if document["protocol"] != FROZEN_PLACEMENT_PROTOCOL:
            raise PlacementError("placement_fixture_protocol_invalid")
        _segment(document["placement_id"], code="placement_id_invalid")
        assignments = document["assignments"]
        if (
            not isinstance(assignments, list)
            or not assignments
            or len(assignments) > _MAX_ASSIGNMENTS
        ):
            raise PlacementError("placement_assignments_invalid")
        return document

    @property
    def source_digest(self) -> str:
        return self._source_digest

    def compile(self, members: Sequence[MemberRecord]) -> PlacementDecision:
        raw = _read_regular_file(self.fixture_path)
        if "sha256:" + hashlib.sha256(raw).hexdigest() != self._source_digest:
            raise PlacementError("placement_fixture_changed")
        if isinstance(members, (str, bytes)) or not isinstance(members, Sequence):
            raise PlacementError("placement_members_invalid")
        member_by_id: dict[str, MemberRecord] = {}
        for member in members:
            if not isinstance(member, MemberRecord):
                raise PlacementError("placement_member_invalid")
            if member.node_id in member_by_id:
                raise PlacementError("placement_member_duplicate")
            member_by_id[member.node_id] = member
        decision = PlacementDecision(
            placement_provenance="frozen_fixture",
            placement_id=self._document["placement_id"],
            assignments=tuple(self._document["assignments"]),
            source_digest=self._source_digest,
        )
        for assignment in decision.assignments:
            member = member_by_id.get(assignment["node_id"])
            if member is None:
                raise PlacementError("placement_member_unknown")
            if not member.activation_eligible:
                raise PlacementError("placement_member_activation_ineligible")
        return decision
