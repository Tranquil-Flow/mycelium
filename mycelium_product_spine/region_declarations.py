# SPDX-License-Identifier: AGPL-3.0-only
"""Operator-declared coarse-region evidence for the live route explorer (A14b).

The A14b reduction (``coarse_region.project_coarse_regions``) and the optional
``regions`` projection exist, but the live serve path never fed them a record:
every peer reduced to ``unknown``/``missing``. This module is the producer for
the one source the live product may publish without a review pipeline, the
owner's own declaration, and nothing more:

* the declaration is an owner-private file (a regular file, never a symlink,
  mode 0600, owned by the serving user) with a closed shape: an operator label,
  an issue/expiry window and a bounded list of ``node_id`` to region
  statements. Any other key (coordinates, addresses, hostnames, provider
  regions, free-form location, ...) rejects the whole file;
* each region goes through the closed ``mycelium.coarse_region.v1`` alias
  table; anything unmapped is ``unknown``, never the nearest region;
* each statement becomes one ``mycelium.coarse_region_evidence.v1`` record for
  the member's pseudonymous route-explorer node, with ``owner_declared`` source
  and ``declared_unverified`` uncertainty. The authority digest binds the
  operator label and the exact declaration bytes, so editing or withdrawing the
  file replaces the whole accepted authority;
* a missing, unreadable or invalid file publishes no evidence at all, which
  leaves the A14a projection (every peer ``unknown``) unchanged.

A declared region is a claim by the owner, not a verified location. Nothing
here reads a network identity, timing, locale or device setting.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from pathlib import Path
from typing import Any, Callable, Mapping

from .coarse_region import (
    EVIDENCE_PROTOCOL,
    PROTOCOL as VOCABULARY,
    normalize_region,
    validate_coarse_region_evidence,
)
from .route_explorer import MAX_NODES, digest

PROTOCOL = "mycelium.operator_region_declaration.v1"
MAX_BYTES = 256 * 1024
_FIELDS = {"protocol", "operator", "issued_at_unix_ms", "expires_at_unix_ms", "declarations"}
_DECLARATION_FIELDS = {"node_id", "region"}
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:~-]{0,127}\Z")


class RegionDeclarationError(ValueError):
    """The owner declaration is refused as a whole."""


def _integer(value: object) -> bool:
    return type(value) is int and 0 <= value <= 9007199254740991


def _read_owner_private(path: Path) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise RegionDeclarationError("region_declaration_unavailable") from exc
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) & 0o077
                or info.st_uid != os.getuid() or not 0 < info.st_size <= MAX_BYTES):
            raise RegionDeclarationError("region_declaration_not_owner_private")
        raw = os.read(fd, MAX_BYTES + 1)
    finally:
        os.close(fd)
    if len(raw) > MAX_BYTES:
        raise RegionDeclarationError("region_declaration_not_owner_private")
    return raw


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise RegionDeclarationError("region_declaration_invalid")
        result[key] = value
    return result


def parse_region_declaration(raw: bytes) -> dict[str, Any]:
    """Validate the closed declaration; returns normalized statements."""

    try:
        document = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object,
                              parse_constant=lambda _value: (_ for _ in ()).throw(
                                  RegionDeclarationError("region_declaration_invalid")))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RegionDeclarationError("region_declaration_invalid") from exc
    if not isinstance(document, dict) or set(document) != _FIELDS:
        raise RegionDeclarationError("region_declaration_invalid")
    if (document["protocol"] != PROTOCOL
            or not isinstance(document["operator"], str)
            or _IDENTIFIER.fullmatch(document["operator"]) is None
            or not _integer(document["issued_at_unix_ms"])
            or not _integer(document["expires_at_unix_ms"])
            or document["expires_at_unix_ms"] < document["issued_at_unix_ms"]):
        raise RegionDeclarationError("region_declaration_invalid")
    declarations = document["declarations"]
    if not isinstance(declarations, list) or len(declarations) > MAX_NODES:
        raise RegionDeclarationError("region_declaration_invalid")
    statements: dict[str, str] = {}
    for item in declarations:
        if (not isinstance(item, dict) or set(item) != _DECLARATION_FIELDS
                or not isinstance(item["node_id"], str)
                or _IDENTIFIER.fullmatch(item["node_id"]) is None
                or item["node_id"] in statements
                or not isinstance(item["region"], str) or len(item["region"]) > 64):
            raise RegionDeclarationError("region_declaration_invalid")
        statements[item["node_id"]] = normalize_region(item["region"])
    return {
        "operator": document["operator"],
        "issued_at_unix_ms": document["issued_at_unix_ms"],
        "expires_at_unix_ms": document["expires_at_unix_ms"],
        "statements": statements,
        "authority_digest": digest({
            "protocol": PROTOCOL,
            "operator": document["operator"],
            "declaration_digest": "sha256:" + hashlib.sha256(raw).hexdigest(),
        }),
    }


def evidence_records(declaration: Mapping[str, Any],
                     member_reference: Callable[[str], str]) -> list[dict[str, Any]]:
    """One owner-declared evidence record per declared member node."""

    records = []
    for node_id, region in sorted(declaration["statements"].items()):
        record = {
            "protocol": EVIDENCE_PROTOCOL,
            "subject_ref": member_reference(node_id),
            "subject_kind": "member",
            "source_kind": "owner_declared",
            "region": region,
            "distance_class": None,
            "vocabulary_version": VOCABULARY,
            "model_version": None,
            "uncertainty": "declared_unverified",
            "issued_at_unix_ms": declaration["issued_at_unix_ms"],
            "expires_at_unix_ms": declaration["expires_at_unix_ms"],
            "authority_digest": declaration["authority_digest"],
        }
        record["digest"] = digest(record)
        records.append(validate_coarse_region_evidence(record))
    return records


class OperatorRegionEvidenceSource:
    """Read the owner declaration at each publication; refuse it as a whole."""

    def __init__(self, path: str | os.PathLike[str], *,
                 member_entity_id: Callable[[str], str]) -> None:
        self._path = Path(path)
        self._member_entity_id = member_entity_id
        self.last_error: str | None = None

    def _member_reference(self, node_id: str) -> str:
        # The route explorer's member node id for this peer's product entity.
        return digest(["member", self._member_entity_id(node_id)])

    def __call__(self) -> list[dict[str, Any]]:
        try:
            declaration = parse_region_declaration(_read_owner_private(self._path))
            records = evidence_records(declaration, self._member_reference)
        except (RegionDeclarationError, ValueError) as error:
            self.last_error = str(error) if isinstance(error, RegionDeclarationError) else (
                "region_declaration_invalid")
            return []
        self.last_error = None
        return records


__all__ = [
    "MAX_BYTES",
    "OperatorRegionEvidenceSource",
    "PROTOCOL",
    "RegionDeclarationError",
    "evidence_records",
    "parse_region_declaration",
]
