from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import hashlib
from pathlib import Path
import stat
import uuid

import pytest

from layer_assignment import assignment_id_for
from mycelium_artifact_acquisition import (
    ACQUISITION_PROTOCOL,
    AcquisitionError,
    acquire_assignment_from_snapshot,
    retry_acquisition,
)


REVISION = "b" * 40


def _digest(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _assignment(root: Path, files: dict[str, bytes], *, node_id: str = "node-a") -> dict:
    semantic = {
        "protocol": "mycelium.layer_assignment.v2",
        "deployment_id": str(uuid.UUID("12345678-1234-5678-9234-abcdefabcdef")),
        "deployment_epoch": 1,
        "node_id": node_id,
        "manifest_digest": "sha256:" + "a" * 64,
        "model_id": "Qwen/Qwen3-8B",
        "resolved_commit": REVISION,
        "range": {
            "start_layer": 0,
            "end_layer_exclusive": 1,
            "layer_count": 1,
        },
        "components": ["decoder"],
        "component_tensor_keys": {
            "decoder": ["model.layers.0.self_attn.q_proj.weight"],
        },
        "component_aliases": {},
        "expected_tensor_prefixes": ["model.layers.0."],
        "expected_tensor_keys": ["model.layers.0.self_attn.q_proj.weight"],
        "files": [
            {
                "path": name,
                "size_bytes": len(value),
                "content_digest": _digest(value),
            }
            for name, value in sorted(files.items())
        ],
        "artifact_cache_root": str(root.resolve()),
        "runtime": {
            "backend": "mlx",
            "dtype": "bfloat16",
            "quantization": "none",
            "architecture": "qwen3",
            "model_config": {
                "n_layer": 1,
                "n_embd": 8,
                "n_head": 2,
                "n_kv_head": 1,
                "n_inner": 16,
                "vocab_size": 32,
                "n_positions": 64,
                "rms_norm_epsilon": 1e-6,
                "rope_theta": 1_000_000.0,
                "head_dim": 4,
                "activation_function": "silu",
                "tie_word_embeddings": False,
            },
        },
    }
    return {
        "assignment_id": assignment_id_for(semantic),
        **semantic,
        "route_ready": False,
        "claim_boundary": "test assignment",
    }


def _snapshot(tmp_path: Path, files: dict[str, bytes]) -> Path:
    root = tmp_path / "snapshots" / REVISION
    root.mkdir(parents=True)
    for name, value in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(value)
    (root / "unassigned.safetensors").write_bytes(b"must-not-transfer")
    return root


def test_acquires_only_assignment_files_into_the_runtime_owned_root(tmp_path: Path) -> None:
    files = {"model-00001-of-00002.safetensors": b"assigned" * 128}
    snapshot = _snapshot(tmp_path, files)
    root = tmp_path / "assignment-root"
    assignment = _assignment(root, files)

    report = acquire_assignment_from_snapshot(assignment, snapshot)

    assert report["protocol"] == ACQUISITION_PROTOCOL
    assert report["download_bytes"] == 0
    assert report["state"] == "acquired"
    assert (root / "model-00001-of-00002.safetensors").read_bytes() == next(iter(files.values()))
    assert not (root / "unassigned.safetensors").exists()
    artifact = root / "model-00001-of-00002.safetensors"
    assert artifact.stat().st_nlink == 1
    assert stat.S_IMODE(artifact.stat().st_mode) == 0o400


def test_warm_reuse_transfers_zero_duplicate_bytes(tmp_path: Path) -> None:
    files = {"model.safetensors": b"warm" * 1_024}
    snapshot = _snapshot(tmp_path, files)
    assignment = _assignment(tmp_path / "assignment-root", files)
    acquire_assignment_from_snapshot(assignment, snapshot, allow_clone=False)

    warm = acquire_assignment_from_snapshot(assignment, snapshot, allow_clone=False)

    assert warm["copied_bytes"] == 0
    assert warm["cloned_bytes"] == 0
    assert warm["reused_bytes"] == len(files["model.safetensors"])
    assert warm["files"][0]["object_reused"] is True
    assert warm["files"][0]["materialized_reused"] is True


def test_interrupted_transfer_resumes_from_verified_partial_prefix(tmp_path: Path) -> None:
    payload = bytes(range(256)) * 40_000
    files = {"model.safetensors": payload}
    snapshot = _snapshot(tmp_path, files)
    root = tmp_path / "assignment-root"
    assignment = _assignment(root, files)
    interrupted_at = 2 * 1024 * 1024

    with pytest.raises(AcquisitionError, match="transfer_interrupted") as failure:
        acquire_assignment_from_snapshot(
            assignment,
            snapshot,
            allow_clone=False,
            fault_after_bytes=interrupted_at,
        )
    assert failure.value.retryable is True

    resumed = acquire_assignment_from_snapshot(assignment, snapshot, allow_clone=False)

    assert resumed["resumed_bytes"] == interrupted_at
    assert resumed["copied_bytes"] == len(payload) - interrupted_at
    assert (root / "model.safetensors").read_bytes() == payload


def test_corrupt_materialization_is_quarantined_and_restored_from_object(tmp_path: Path) -> None:
    payload = b"integrity" * 1_024
    files = {"model.safetensors": payload}
    snapshot = _snapshot(tmp_path, files)
    root = tmp_path / "assignment-root"
    assignment = _assignment(root, files)
    acquire_assignment_from_snapshot(assignment, snapshot, allow_clone=False)
    materialized = root / "model.safetensors"
    materialized.chmod(0o600)
    materialized.write_bytes(b"corrupt")

    repaired = acquire_assignment_from_snapshot(assignment, snapshot, allow_clone=False)

    assert repaired["quarantined"]
    assert repaired["reused_bytes"] == len(payload)
    assert materialized.read_bytes() == payload
    assert list((root / ".mycelium" / "quarantine").iterdir())


def test_concurrent_acquisition_deduplicates_by_immutable_object(tmp_path: Path) -> None:
    payload = b"concurrent" * 100_000
    files = {"model.safetensors": payload}
    snapshot = _snapshot(tmp_path, files)
    assignment = _assignment(tmp_path / "assignment-root", files)

    with ThreadPoolExecutor(max_workers=2) as executor:
        reports = list(
            executor.map(
                lambda _index: acquire_assignment_from_snapshot(
                    assignment,
                    snapshot,
                    allow_clone=False,
                ),
                range(2),
            )
        )

    assert sorted(report["copied_bytes"] for report in reports) == [0, len(payload)]
    assert sorted(report["reused_bytes"] for report in reports) == [0, len(payload)]


def test_assignment_root_identity_conflict_fails_closed(tmp_path: Path) -> None:
    files = {"model.safetensors": b"identity"}
    snapshot = _snapshot(tmp_path, files)
    root = tmp_path / "assignment-root"
    acquire_assignment_from_snapshot(_assignment(root, files), snapshot)

    with pytest.raises(AcquisitionError, match="artifact_root_identity_conflict") as failure:
        acquire_assignment_from_snapshot(
            _assignment(root, files, node_id="node-b"),
            snapshot,
        )
    assert failure.value.retryable is False


def test_retry_is_bounded_and_only_retries_transient_failures() -> None:
    calls: list[int] = []
    delays: list[float] = []

    def transient() -> dict[str, object]:
        calls.append(len(calls) + 1)
        if len(calls) < 3:
            raise AcquisitionError("temporary_transport", retryable=True)
        return {"state": "acquired"}

    assert retry_acquisition(
        transient,
        max_attempts=3,
        base_delay_seconds=0.25,
        sleep=delays.append,
    ) == {"state": "acquired"}
    assert calls == [1, 2, 3]
    assert delays == [0.25, 0.5]

    with pytest.raises(AcquisitionError, match="integrity"):
        retry_acquisition(
            lambda: (_ for _ in ()).throw(
                AcquisitionError("integrity", retryable=False)
            ),
            sleep=lambda _delay: pytest.fail("permanent failure was retried"),
        )
