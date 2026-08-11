"""Deterministic credential-free onboarding bundle for a supported external Mac."""

from __future__ import annotations

import hashlib
import io
import json
import os
from pathlib import Path
import tarfile
from typing import Any

from mycelium_qualification.evidence import canonical_json_bytes


PROTOCOL = "mycelium.astra_reviewer_bundle.v1"


def _tar_info(name: str, size: int, mode: int) -> tarfile.TarInfo:
    info = tarfile.TarInfo(name)
    info.size = size
    info.mode = mode
    info.uid = info.gid = 0
    info.uname = info.gname = ""
    info.mtime = 0
    return info


def build_reviewer_bundle(
    *,
    version: str,
    files: dict[str, Path],
    output: Path,
) -> dict[str, Any]:
    if (
        not version
        or not files
        or any(
            not name or name.startswith("/") or ".." in name.split("/")
            for name in files
        )
    ):
        raise ValueError("reviewer_bundle_invalid")
    components: list[dict[str, Any]] = []
    payloads: list[tuple[str, bytes, int]] = []
    for name, path in sorted(files.items()):
        resolved = path.resolve(strict=True)
        if not resolved.is_file():
            raise ValueError("reviewer_bundle_invalid")
        raw = resolved.read_bytes()
        executable = os.access(resolved, os.X_OK) or name.endswith(".py")
        mode = 0o700 if executable else 0o600
        digest = "sha256:" + hashlib.sha256(raw).hexdigest()
        components.append(
            {
                "name": name,
                "size_bytes": len(raw),
                "sha256": digest,
                "mode": format(mode, "04o"),
            }
        )
        payloads.append((name, raw, mode))
    manifest = {
        "protocol": PROTOCOL,
        "version": version,
        "components": components,
        "contains_credentials": False,
        "contains_invitation": False,
        "tailscale_required": False,
        "product_transport": "endpointid_authenticated_iroh",
    }
    manifest["manifest_digest"] = (
        "sha256:" + hashlib.sha256(canonical_json_bytes(manifest)).hexdigest()
    )
    manifest_raw = canonical_json_bytes(manifest)
    output.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(output, "w", format=tarfile.PAX_FORMAT) as archive:
        for name, raw, mode in payloads:
            archive.addfile(
                _tar_info(f"runtime/{name}", len(raw), mode), io.BytesIO(raw)
            )
        archive.addfile(
            _tar_info("reviewer-bundle-manifest.json", len(manifest_raw), 0o600),
            io.BytesIO(manifest_raw),
        )
    return manifest


def verify_reviewer_bundle(path: Path) -> dict[str, Any]:
    with tarfile.open(path, "r") as archive:
        members = {member.name: member for member in archive.getmembers()}
        manifest_member = members.get("reviewer-bundle-manifest.json")
        if manifest_member is None or not manifest_member.isfile():
            raise ValueError("reviewer_bundle_invalid")
        stream = archive.extractfile(manifest_member)
        if stream is None:
            raise ValueError("reviewer_bundle_invalid")
        manifest = json.loads(stream.read())
        if not isinstance(manifest, dict) or manifest.get("protocol") != PROTOCOL:
            raise ValueError("reviewer_bundle_invalid")
        supplied = manifest.get("manifest_digest")
        unsigned = dict(manifest)
        unsigned.pop("manifest_digest", None)
        if (
            supplied
            != "sha256:" + hashlib.sha256(canonical_json_bytes(unsigned)).hexdigest()
        ):
            raise ValueError("reviewer_bundle_invalid")
        expected = {f"runtime/{item['name']}": item for item in manifest["components"]}
        if set(members) != {*expected, "reviewer-bundle-manifest.json"}:
            raise ValueError("reviewer_bundle_invalid")
        for name, item in expected.items():
            member = members[name]
            stream = archive.extractfile(member)
            if stream is None:
                raise ValueError("reviewer_bundle_invalid")
            raw = stream.read()
            if (
                member.size != item["size_bytes"]
                or "sha256:" + hashlib.sha256(raw).hexdigest() != item["sha256"]
            ):
                raise ValueError("reviewer_bundle_invalid")
    return manifest


__all__ = ["PROTOCOL", "build_reviewer_bundle", "verify_reviewer_bundle"]
