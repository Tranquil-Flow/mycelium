"""Deterministic privacy-reduced source/binary/model release manifest."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from mycelium_qualification.evidence import canonical_json_bytes


PROTOCOL = "mycelium.m22_sbom.v1"


def file_component(*, kind: str, name: str, path: Path) -> dict[str, Any]:
    if not kind or not name or path.is_symlink() or not path.is_file():
        raise ValueError("m22_sbom_component_invalid")
    raw = path.read_bytes()
    return {
        "kind": kind,
        "name": name,
        "version": None,
        "size_bytes": len(raw),
        "sha256": "sha256:" + hashlib.sha256(raw).hexdigest(),
    }


def version_component(*, kind: str, name: str, version: str) -> dict[str, Any]:
    if not kind or not name or not version or any("\n" in item for item in (kind, name, version)):
        raise ValueError("m22_sbom_component_invalid")
    return {"kind": kind, "name": name, "version": version, "size_bytes": None, "sha256": None}


def build_sbom(*, revision: str, components: list[dict[str, Any]]) -> dict[str, Any]:
    if not revision or not components:
        raise ValueError("m22_sbom_invalid")
    ordered = sorted(components, key=lambda item: (item["kind"], item["name"], item.get("version") or ""))
    identities = [(item["kind"], item["name"]) for item in ordered]
    if len(identities) != len(set(identities)):
        raise ValueError("m22_sbom_duplicate_component")
    body = {
        "protocol": PROTOCOL,
        "revision": revision,
        "components": ordered,
        "network_download_performed": False,
        "privacy": "logical names, versions, byte counts, and digests only; no private paths or credentials",
    }
    body["sbom_digest"] = "sha256:" + hashlib.sha256(canonical_json_bytes(body)).hexdigest()
    return body


def validate_sbom(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {"protocol", "revision", "components", "network_download_performed", "privacy", "sbom_digest"}:
        raise ValueError("m22_sbom_invalid")
    supplied = value.get("sbom_digest")
    unsigned = dict(value)
    unsigned.pop("sbom_digest", None)
    if (
        value.get("protocol") != PROTOCOL
        or value.get("network_download_performed") is not False
        or value.get("privacy") != "logical names, versions, byte counts, and digests only; no private paths or credentials"
        or not isinstance(supplied, str)
        or supplied != "sha256:" + hashlib.sha256(canonical_json_bytes(unsigned)).hexdigest()
    ):
        raise ValueError("m22_sbom_invalid")
    rebuilt = build_sbom(revision=value["revision"], components=list(value["components"]))
    if rebuilt != value:
        raise ValueError("m22_sbom_invalid")
    return json.loads(canonical_json_bytes(value))


__all__ = ["PROTOCOL", "build_sbom", "file_component", "validate_sbom", "version_component"]
