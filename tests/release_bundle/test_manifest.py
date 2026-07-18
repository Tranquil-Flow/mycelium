from __future__ import annotations

import json
from pathlib import Path

from mycelium_release_bundle import (
    REQUIRED_PHYSICAL_INPUTS,
    canonical_json_bytes,
    sha256_bytes,
    verify_bundle,
)

from .conftest import MANIFEST_FILENAME, canonical_bytes


def test_canonical_manifest_and_sha256_verification(
    synthetic_bundle: tuple[Path, dict[str, object]],
) -> None:
    root, manifest = synthetic_bundle

    result = verify_bundle(root)

    manifest_bytes = (root / MANIFEST_FILENAME).read_bytes()
    assert canonical_json_bytes(manifest) == manifest_bytes
    assert result["ok"] is True
    assert result["manifest_sha256"] == sha256_bytes(manifest_bytes)
    assert result["body_sha256"] == manifest["body_sha256"]
    assert result["checks"]["manifest_canonical"] is True
    assert result["checks"]["manifest_digest"] is True
    assert result["route_ready"] is False
    assert result["release_ready"] is False
    assert result["qualification_evaluated"] is False
    assert result["physical_evidence_accepted"] is False
    assert result["physical_input_inventory_complete"] is False
    assert result["missing_physical_inputs"] == list(REQUIRED_PHYSICAL_INPUTS)


def test_noncanonical_manifest_is_rejected(
    synthetic_bundle: tuple[Path, dict[str, object]],
) -> None:
    root, manifest = synthetic_bundle
    (root / MANIFEST_FILENAME).write_text(
        json.dumps(manifest, sort_keys=True, indent=2), encoding="utf-8"
    )

    first = verify_bundle(root)
    second = verify_bundle(root)

    assert first == second
    assert first["ok"] is False
    assert first["findings"] == [
        {"code": "noncanonical_manifest_json", "subject": "manifest"}
    ]
    assert first["route_ready"] is False
    assert first["release_ready"] is False


def test_manifest_body_digest_mismatch_is_rejected(
    synthetic_bundle: tuple[Path, dict[str, object]],
) -> None:
    root, manifest = synthetic_bundle
    manifest["body_sha256"] = "sha256:" + "f" * 64
    (root / MANIFEST_FILENAME).write_bytes(canonical_bytes(manifest))

    result = verify_bundle(root)

    assert result["ok"] is False
    assert result["findings"] == [
        {"code": "manifest_body_digest_mismatch", "subject": "manifest"}
    ]
    assert result["checks"]["manifest_canonical"] is True
    assert result["checks"]["manifest_digest"] is False
