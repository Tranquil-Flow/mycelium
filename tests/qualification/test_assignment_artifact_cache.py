from __future__ import annotations

import hashlib
from dataclasses import asdict
from pathlib import Path

import pytest

from mycelium_assignment_cache import ArtifactObjectKey, AssignmentArtifactCache


def _source(tmp_path: Path, name: str, payload: bytes) -> tuple[Path, ArtifactObjectKey]:
    path = tmp_path / "source" / name
    path.parent.mkdir(exist_ok=True)
    path.write_bytes(payload)
    digest = "sha256:" + hashlib.sha256(payload).hexdigest()
    return path, ArtifactObjectKey(
        model_revision="a" * 40,
        manifest_digest="sha256:" + "b" * 64,
        format="safetensors",
        quantization="int8-weight-only",
        tensor_digest=digest,
        size_bytes=len(payload),
    )


def test_cache_populates_once_reuses_and_quarantines_corruption(tmp_path: Path) -> None:
    source, key = _source(tmp_path, "stage-a.bin", b"assigned tensor bytes")
    cache = AssignmentArtifactCache(tmp_path / "cache", max_bytes=1_000)

    assert cache.store(key, source) == "populated"
    assert cache.store(key, source) == "reused"
    object_path = cache.root / "objects" / key.object_id[7:]
    object_path.chmod(0o600)
    object_path.write_bytes(b"corrupt")
    assert cache.store(key, source) == "repaired"

    assert object_path.read_bytes() == source.read_bytes()
    assert len(list((cache.root / "quarantine").iterdir())) == 1


def test_assignment_materialization_opens_only_owned_objects(tmp_path: Path) -> None:
    cache = AssignmentArtifactCache(tmp_path / "cache", max_bytes=10_000)
    objects = {}
    for index in range(4):
        source, key = _source(tmp_path, f"stage-{index}.bin", bytes([index + 1]) * 100)
        cache.store(key, source)
        objects[index] = key

    first = cache.materialize_assignment(
        assignment_id="assignment-a",
        required_objects={
            "model-stage-000.safetensors": objects[0],
            "model-static.safetensors": objects[3],
        },
        destination=tmp_path / "assignment-a",
    )
    second = cache.materialize_assignment(
        assignment_id="assignment-b",
        required_objects={
            "model-stage-001.safetensors": objects[1],
            "model-static.safetensors": objects[3],
        },
        destination=tmp_path / "assignment-b",
    )

    assert len(first["opened_object_ids"]) == 2
    assert first["unassigned_object_count"] == 2
    assert len(second["opened_object_ids"]) == 2
    assert not (tmp_path / "assignment-a" / "model-stage-001.safetensors").exists()
    assert not (tmp_path / "assignment-b" / "model-stage-000.safetensors").exists()
    assert cache.status()["pinned_object_count"] == 3


def test_eviction_never_removes_pinned_active_assignment(tmp_path: Path) -> None:
    cache = AssignmentArtifactCache(tmp_path / "cache", max_bytes=150)
    first_source, first_key = _source(tmp_path, "first.bin", b"a" * 100)
    second_source, second_key = _source(tmp_path, "second.bin", b"b" * 100)
    cache.store(first_key, first_source)
    cache.materialize_assignment(
        assignment_id="active",
        required_objects={"first.bin": first_key},
        destination=tmp_path / "active",
    )
    cache.store(second_key, second_source)

    assert (cache.root / "objects" / first_key.object_id[7:]).is_file()
    assert not (cache.root / "objects" / second_key.object_id[7:]).exists()
    assert cache.status()["used_bytes"] == 100


def test_cache_rejects_wrong_digest_unsafe_paths_and_symlink_root(tmp_path: Path) -> None:
    source, key = _source(tmp_path, "object.bin", b"payload")
    cache = AssignmentArtifactCache(tmp_path / "cache", max_bytes=1_000)
    wrong = ArtifactObjectKey(
        **{**asdict(key), "tensor_digest": "sha256:" + "f" * 64}
    )
    with pytest.raises(ValueError, match="does not match"):
        cache.store(wrong, source)
    cache.store(key, source)
    with pytest.raises(ValueError, match="unsafe"):
        cache.materialize_assignment(
            assignment_id="assignment",
            required_objects={"../escape": key},
            destination=tmp_path / "escape",
        )

    target = tmp_path / "target"
    target.mkdir()
    (tmp_path / "linked-cache").symlink_to(target, target_is_directory=True)
    with pytest.raises(ValueError, match="symlink"):
        AssignmentArtifactCache(tmp_path / "linked-cache", max_bytes=1_000)
