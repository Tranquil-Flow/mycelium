from __future__ import annotations

import hashlib
import json
from typing import Any

import pytest

MANIFEST_PROTOCOL = "mycelium.route_qualification_evidence_manifest.v1"


def canonical(value: Any) -> bytes:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def sha256(content: bytes) -> str:
    return "sha256:" + hashlib.sha256(content).hexdigest()


def manifest_bytes(
    files: dict[str, bytes],
    *,
    run_id: str = "qualification-diff-test",
    evidence_class: str = "physical_qualification",
) -> bytes:
    entries = [
        {"path": path, "sha256": sha256(content), "size_bytes": len(content)}
        for path, content in sorted(files.items())
    ]
    return canonical(
        {
            "evidence_class": evidence_class,
            "file_count": len(entries),
            "files": entries,
            "protocol": MANIFEST_PROTOCOL,
            "run_id": run_id,
            "total_size_bytes": sum(len(content) for content in files.values()),
        }
    )


def make_bundle(
    documents: dict[str, Any] | None = None,
    *,
    raw_files: dict[str, bytes] | None = None,
    run_id: str = "qualification-diff-test",
    evidence_class: str = "physical_qualification",
) -> tuple[bytes, dict[str, bytes]]:
    files = {
        path: canonical(document)
        for path, document in (documents or {}).items()
    }
    files.update(raw_files or {})
    return (
        manifest_bytes(files, run_id=run_id, evidence_class=evidence_class),
        files,
    )


@pytest.fixture
def small_bundle() -> tuple[bytes, dict[str, bytes]]:
    return make_bundle({"run/evidence.json": {"kind": "fixture", "value": 1}})
