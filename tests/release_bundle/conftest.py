from __future__ import annotations

import copy
import hashlib
import json
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import pytest

MANIFEST_FILENAME = "release-evidence-manifest.json"
MANIFEST_PROTOCOL = "mycelium.immutable_release_evidence_bundle.v1"
REQUIRED_GATES = (
    "assignment",
    "dependency_lock",
    "deployment",
    "deployment_epoch",
    "endpoint_id",
    "model_manifest",
    "negative_run",
    "parity",
    "path",
    "qualification",
    "source_commit",
    "stage_load_proof",
    "transport",
)


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def sha256_bytes(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _summary() -> dict[str, Any]:
    zero = "sha256:" + "0" * 64
    return {
        "assignment_id": "synthetic-test-assignment",
        "deployment_epoch": 0,
        "deployment_id": "synthetic-test-deployment",
        "endpoint_id": "synthetic-test-endpoint-id",
        "evidence_class": "synthetic_test_fixture",
        "manifest_digest": zero,
        "negative_run_digest": zero,
        "parity_digest": zero,
        "path_id": "synthetic-test-path",
        "qualification_digest": zero,
        "route_ready": False,
        "source_commit": "0" * 40,
        "stage": {
            "load_proof_digest": zero,
            "stage_id": "synthetic-test-stage",
        },
        "synthetic_fixture": True,
        "transport_digest": zero,
    }


_BINDING_POINTERS = {
    "assignment": "/assignment_id",
    "deployment": "/deployment_id",
    "deployment_epoch": "/deployment_epoch",
    "endpoint_id": "/endpoint_id",
    "model_manifest": "/manifest_digest",
    "negative_run": "/negative_run_digest",
    "parity": "/parity_digest",
    "path": "/path_id",
    "qualification": "/qualification_digest",
    "source_commit": "/source_commit",
    "stage_load_proof": "/stage/load_proof_digest",
    "transport": "/transport_digest",
}


def write_synthetic_bundle(
    root: Path,
    *,
    gates: Iterable[str] = REQUIRED_GATES,
) -> dict[str, Any]:
    """Write an unmistakably synthetic, never-accepted verifier fixture."""
    root.mkdir(parents=True, exist_ok=True)
    summary = _summary()
    summary_path = "qualification/synthetic-summary.json"
    lock_path = "provenance/synthetic-dependencies.lock"
    contents = {
        summary_path: canonical_bytes(summary),
        lock_path: b"synthetic-test-dependency-lock\n",
    }
    for relative, content in contents.items():
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)

    file_entries = [
        {
            "media_type": (
                "application/json"
                if path.endswith(".json")
                else "application/vnd.mycelium.dependency-lock"
            ),
            "path": path,
            "sha256": sha256_bytes(content),
            "size_bytes": len(content),
            "synthetic_fixture": True,
        }
        for path, content in sorted(contents.items())
    ]
    selected = tuple(sorted(gates))
    declared_gates = []
    bindings = []
    for gate in selected:
        path = lock_path if gate == "dependency_lock" else summary_path
        declared_gates.append({"evidence_paths": [path], "gate": gate})
        if gate == "dependency_lock":
            bindings.append(
                {
                    "expected": sha256_bytes(contents[lock_path]),
                    "json_pointer": None,
                    "kind": gate,
                    "path": lock_path,
                }
            )
        else:
            pointer = _BINDING_POINTERS[gate]
            value: Any = summary
            for part in pointer.removeprefix("/").split("/"):
                value = value[part]
            bindings.append(
                {
                    "expected": copy.deepcopy(value),
                    "json_pointer": pointer,
                    "kind": gate,
                    "path": summary_path,
                }
            )
    bindings.sort(key=lambda item: (item["kind"], item["path"], item["json_pointer"] or ""))
    body = {
        "bindings": bindings,
        "bundle_id": "synthetic-test-fixture:release-evidence-verifier",
        "declared_gates": declared_gates,
        "evidence_class": "synthetic_test_fixture",
        "file_count": len(file_entries),
        "files": file_entries,
        "synthetic_fixture": True,
        "total_size_bytes": sum(len(content) for content in contents.values()),
    }
    manifest = {
        "body": body,
        "body_sha256": sha256_bytes(canonical_bytes(body)),
        "protocol": MANIFEST_PROTOCOL,
    }
    (root / MANIFEST_FILENAME).write_bytes(canonical_bytes(manifest))
    return manifest


def rewrite_manifest(root: Path, manifest: dict[str, Any]) -> None:
    manifest["body_sha256"] = sha256_bytes(canonical_bytes(manifest["body"]))
    (root / MANIFEST_FILENAME).write_bytes(canonical_bytes(manifest))


def replace_artifact(
    root: Path,
    manifest: dict[str, Any],
    relative_path: str,
    content: bytes,
) -> None:
    (root / relative_path).write_bytes(content)
    body = manifest["body"]
    entry = next(item for item in body["files"] if item["path"] == relative_path)
    entry["sha256"] = sha256_bytes(content)
    entry["size_bytes"] = len(content)
    for binding in body["bindings"]:
        if binding["path"] == relative_path and binding["json_pointer"] is None:
            binding["expected"] = entry["sha256"]
    body["total_size_bytes"] = sum(item["size_bytes"] for item in body["files"])
    rewrite_manifest(root, manifest)


@pytest.fixture
def synthetic_bundle(tmp_path: Path) -> tuple[Path, dict[str, Any]]:
    root = tmp_path / "synthetic-release-bundle"
    return root, write_synthetic_bundle(root)
