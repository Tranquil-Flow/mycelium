# SPDX-License-Identifier: AGPL-3.0-only
"""Owner disk: no second full-size materialization of the weights.

An owner compile in slice mode writes the stage shards once (the builder's
``--emit-stage-shards``). Every later owner-side copy of those bytes -- the
compiled transfer bundle's deployment files, the owner's own slice and the
staged copy its worker loads -- must be an APFS clone (shared data blocks, its
own inode, ``st_nlink`` 1; never a hardlink) whose bytes are still hashed
against the pinned digest. Whole mode likewise clones the source snapshot.

Physical sharing is measured with ``fcntl(F_LOG2PHYS_EXT)``: blocks are counted
once by device offset, so clones are shared and real copies are not. The
builder parts are the builder's own code (``_prepare_model_representation``,
the slice compile through ``start_compile``); the staging is the real canonical
staging script in its real subprocess. No model load, device or network.
"""
from __future__ import annotations

import fcntl
import hashlib
import io
import os
from pathlib import Path
import stat
import struct
import sys
import tarfile

import pytest

from mycelium_a13_macos import managed_staging, slice_acquisition
from mycelium_a13_macos.managed_staging import StagingError, TargetStaging
from mycelium_a13_macos.slice_acquisition import fetch_node_slice, node_slice_plan

pytestmark = pytest.mark.skipif(sys.platform != "darwin", reason="APFS clone semantics")

_F_LOG2PHYS_EXT = 65
_L2P = "=Iqq"  # struct log2phys, #pragma pack(4)


def _extents(path: Path) -> list[tuple[int, int]]:
    """Physical (device offset, length) extents of one file."""
    size = path.stat().st_size
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
        result, offset = [], 0
        while offset < size:
            raw = fcntl.fcntl(fd, _F_LOG2PHYS_EXT, struct.pack(_L2P, 0, size - offset, offset))
            _, length, device = struct.unpack(_L2P, raw)
            assert length > 0
            result.append((device, length))
            offset += length
        return result
    finally:
        os.close(fd)


def _physical_bytes(files) -> int:
    """Bytes of distinct physical blocks behind ``files`` (clones counted once)."""
    intervals = sorted((d, d + n) for f in files for d, n in _extents(Path(f)))
    total, end = 0, None
    for start, stop in intervals:
        if end is None or start >= end:
            total += stop - start
            end = stop
        elif stop > end:
            total += stop - end
            end = stop
    return total


def _weights(*roots: Path) -> list[Path]:
    return sorted(p for r in roots for p in Path(r).rglob("*.safetensors") if p.is_file())


def _sha(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _archive():
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w") as tar:
        item = tarfile.TarInfo("physical_inference_node.py")
        payload = b"# fixture bytes, never executed\n"
        item.size = len(payload)
        tar.addfile(item, io.BytesIO(payload))
    data = stream.getvalue()
    return data, "sha256:" + hashlib.sha256(data).hexdigest()


def _stager(root: Path, node: str, cache: Path) -> TargetStaging:
    return TargetStaging(root, node_id=node, max_archive_bytes=1 << 20, max_disk_bytes=1 << 30,
                         current_member=lambda: True, authorize=lambda intent: True,
                         artifact_cache_root=cache)


def _preposition(rows):
    return {"protocol": "mycelium.controller_prepositioned_member_artifacts.v1",
            "files": sorted(rows, key=lambda r: r["destination_path"])}


def _random_file(path: Path, size: int) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as handle:
        handle.write(os.urandom(size))
        handle.flush()
        os.fsync(handle.fileno())
    path.chmod(0o600)
    return path


# -- canonical staging -------------------------------------------------------

def test_staging_prepositioned_artifact_is_a_clone_not_a_second_copy(tmp_path):
    cache = (tmp_path / "cache").resolve()
    source = _random_file(cache / "deployment/model-stage-002-of-002.safetensors", 4 << 20)
    stage = _stager(tmp_path / "mycelium-target", "owner-node", cache)
    archive, digest = _archive()
    row = {"content_digest": _sha(source), "destination_path": "deployment/" + source.name,
           "source_path": str(source), "size_bytes": source.stat().st_size}
    stage.prepare(archive=archive, archive_digest=digest, preposition=_preposition([row]))
    staged = stage.staging_root / row["destination_path"]
    info = staged.lstat()
    assert staged.read_bytes() == source.read_bytes()
    assert stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and stat.S_IMODE(info.st_mode) == 0o600
    assert (info.st_dev, info.st_ino) != (source.stat().st_dev, source.stat().st_ino)
    assert source.stat().st_nlink == 1
    # One physical copy for both paths: the staged file shares the source's blocks.
    assert _physical_bytes([source, staged]) == _physical_bytes([source])


def test_staging_disk_preflight_does_not_charge_clonable_prepositions(tmp_path, monkeypatch):
    cache = (tmp_path / "cache").resolve()
    source = _random_file(cache / "deployment/w.safetensors", 1 << 20)
    stage = _stager(tmp_path / "mycelium-target", "owner-node", cache)
    archive, digest = _archive()
    row = {"content_digest": _sha(source), "destination_path": "deployment/w.safetensors",
           "source_path": str(source), "size_bytes": source.stat().st_size}
    usage = managed_staging.shutil.disk_usage(tmp_path)
    # Less free disk than the prepositioned file, more than archive + document.
    monkeypatch.setattr(managed_staging.shutil, "disk_usage",
                        lambda path: usage._replace(free=64 * 1024))
    stage.prepare(archive=archive, archive_digest=digest, preposition=_preposition([row]))
    assert (stage.staging_root / row["destination_path"]).read_bytes() == source.read_bytes()


def test_staging_clone_still_fails_closed_on_a_digest_mismatch(tmp_path):
    cache = (tmp_path / "cache").resolve()
    source = _random_file(cache / "deployment/w.safetensors", 1 << 20)
    stage = _stager(tmp_path / "mycelium-target", "owner-node", cache)
    archive, digest = _archive()
    row = {"content_digest": "sha256:" + "0" * 64, "destination_path": "deployment/w.safetensors",
           "source_path": str(source), "size_bytes": source.stat().st_size}
    with pytest.raises(StagingError):
        stage.prepare(archive=archive, archive_digest=digest, preposition=_preposition([row]))
    assert not stage.staging_root.exists()


# -- the owner's own slice -----------------------------------------------------

class _CountingTransport:
    """Ranged source reads (stand-in for HF) plus the owner's local candidates."""

    def __init__(self, snapshot: Path, candidates: Path | None) -> None:
        self.snapshot, self.candidates, self.requests = snapshot, candidates, 0

    def read_range(self, model_id, commit, path, offset, length):
        self.requests += 1
        with (self.snapshot / path).open("rb") as handle:
            handle.seek(offset)
            return handle.read(length)

    def clone_candidate(self, path):
        if self.candidates is None:
            return None
        candidate = self.candidates / path
        return candidate if candidate.is_file() else None


def _compiled(tmp_path, monkeypatch):
    from test_slice_real_builder import _slice_compile
    app, captured, nodes, snapshot, _state, plan = _slice_compile(
        tmp_path, monkeypatch, "float32+int8-weight-only")
    shards = app.root / "chain-runs/test-nhost/stage-shards/model"
    return captured["deployment"], shards, nodes, snapshot, plan


def test_owner_slice_compile_and_staging_hold_one_physical_copy_of_the_shards(tmp_path, monkeypatch):
    deployment, shards, nodes, snapshot, plan = _compiled(tmp_path, monkeypatch)
    one_copy = _physical_bytes(_weights(shards))
    assert one_copy >= sum(p.stat().st_size for p in _weights(shards))
    # The transfer bundle is a clone of the emitted shards (builder).
    assert _physical_bytes(_weights(shards, deployment)) == one_copy
    owner = nodes[-1]
    node_plan = node_slice_plan(plan, owner)
    slice_root = (tmp_path / "owner-state/slices/test-nhost")
    slice_root.mkdir(parents=True)
    slice_root = slice_root.resolve()
    total = sum(r["size_bytes"] for r in node_plan["files"])
    transport = _CountingTransport(snapshot, deployment.parent)
    report = fetch_node_slice(node_plan, expected_node_plan_digest=node_plan["node_plan_digest"],
                              destination_root=slice_root, transport=transport,
                              max_download_bytes=total, max_disk_bytes=total)
    assert transport.requests == 0 and report["downloaded_bytes"] == 0
    assert report["cloned_bytes"] == total
    for row in node_plan["files"]:
        published = slice_root / row["path"]
        assert published.lstat().st_nlink == 1 and _sha(published) == row["content_digest"]
    stage = _stager(tmp_path / "owner-state/managed/mycelium-stage-root", owner, slice_root)
    archive, digest = _archive()
    stage.prepare(archive=archive, archive_digest=digest, preposition=_preposition([
        {"content_digest": r["content_digest"], "destination_path": r["path"],
         "source_path": str(slice_root / r["path"]), "size_bytes": r["size_bytes"]}
        for r in node_plan["files"]]))
    # Shards, bundle, own slice and staged copy: still exactly one physical copy.
    assert _physical_bytes(_weights(shards, deployment, slice_root, stage.staging_root)) == one_copy


def test_owner_slice_needs_no_free_disk_when_every_file_clones(tmp_path, monkeypatch):
    deployment, _, nodes, snapshot, plan = _compiled(tmp_path, monkeypatch)
    node_plan = node_slice_plan(plan, nodes[0])
    root = (tmp_path / "slice")
    root.mkdir()
    root = root.resolve()
    usage = slice_acquisition.shutil.disk_usage(tmp_path)
    monkeypatch.setattr(slice_acquisition.shutil, "disk_usage", lambda path: usage._replace(free=0))
    total = sum(r["size_bytes"] for r in node_plan["files"])
    report = fetch_node_slice(node_plan, expected_node_plan_digest=node_plan["node_plan_digest"],
                              destination_root=root, transport=_CountingTransport(snapshot, deployment.parent),
                              max_download_bytes=total, max_disk_bytes=total)
    assert report["cloned_bytes"] == total


def test_a_wrong_clone_candidate_falls_back_to_the_verified_ranged_fetch(tmp_path, monkeypatch):
    deployment, _, nodes, snapshot, plan = _compiled(tmp_path, monkeypatch)
    node_plan = node_slice_plan(plan, nodes[0])
    candidates = tmp_path / "tampered"
    for row in node_plan["files"]:
        target = candidates / row["path"]
        target.parent.mkdir(parents=True, exist_ok=True)
        data = bytearray((deployment / Path(row["path"]).name).read_bytes())
        data[-1] ^= 0xFF  # same size, wrong bytes
        target.write_bytes(bytes(data))
    root = (tmp_path / "slice")
    root.mkdir()
    root = root.resolve()
    transport = _CountingTransport(snapshot, candidates)
    total = sum(r["size_bytes"] for r in node_plan["files"])
    report = fetch_node_slice(node_plan, expected_node_plan_digest=node_plan["node_plan_digest"],
                              destination_root=root, transport=transport,
                              max_download_bytes=total, max_disk_bytes=total)
    assert report["cloned_bytes"] == 0 and transport.requests > 0
    assert report["downloaded_bytes"] == node_plan["download_bytes"]
    for row in node_plan["files"]:
        assert _sha(root / row["path"]) == row["content_digest"]
    assert not (root / ".slice-work").exists()


def test_a_hardlinked_clone_candidate_is_never_used(tmp_path, monkeypatch):
    deployment, _, nodes, snapshot, plan = _compiled(tmp_path, monkeypatch)
    node_plan = node_slice_plan(plan, nodes[0])
    candidates = tmp_path / "linked"
    for row in node_plan["files"]:
        target = candidates / row["path"]
        target.parent.mkdir(parents=True, exist_ok=True)
        os.link(deployment / Path(row["path"]).name, target)
    root = (tmp_path / "slice")
    root.mkdir()
    root = root.resolve()
    total = sum(r["size_bytes"] for r in node_plan["files"])
    report = fetch_node_slice(node_plan, expected_node_plan_digest=node_plan["node_plan_digest"],
                              destination_root=root, transport=_CountingTransport(snapshot, candidates),
                              max_download_bytes=total, max_disk_bytes=total)
    assert report["cloned_bytes"] == 0
    assert all((root / r["path"]).lstat().st_nlink == 1 for r in node_plan["files"])


def test_owner_transport_offers_this_runs_compiled_bundle(tmp_path):
    from types import SimpleNamespace as NS
    from mycelium_a13_macos.demo_chain import _owner_compiled_bundle
    from mycelium_a13_macos.demo_target import SnapshotRangeTransport
    app = NS(root=tmp_path / "app", _observation_context=NS(run_id="a13-x-qwen-20261013T000000Z"))
    bundle = _owner_compiled_bundle(app)
    assert bundle == tmp_path / "app/chain-runs/a13-x-qwen-20261013T000000Z/compiled/transfer-bundle"
    assert _owner_compiled_bundle(NS(root=tmp_path, _observation_context=None)) is None
    (bundle / "deployment").mkdir(parents=True)
    (bundle / "deployment/w.safetensors").write_bytes(b"w")
    (bundle / "deployment/link.safetensors").symlink_to(bundle / "deployment/w.safetensors")
    transport = SnapshotRangeTransport(tmp_path, model_id="m/x", resolved_commit="c" * 40,
                                       clone_root=bundle)
    assert transport.clone_candidate("deployment/w.safetensors") == bundle / "deployment/w.safetensors"
    for refused in ("deployment/link.safetensors", "deployment/missing.safetensors",
                    "../deployment/w.safetensors", "/etc/passwd"):
        assert transport.clone_candidate(refused) is None
    assert SnapshotRangeTransport(tmp_path, model_id="m/x", resolved_commit="c" * 40
                                  ).clone_candidate("deployment/w.safetensors") is None


# -- whole mode ----------------------------------------------------------------

def test_whole_mode_deployment_clones_the_source_snapshot(tmp_path):
    from argparse import Namespace
    from scripts import build_qwen_live_route as builder
    from mycelium_qualification.physical_deployment import LocalModelSource, prepare_assignment_artifacts
    from _nhost_fixtures import COMMIT, tiny_dense_snapshot
    snapshot = tiny_dense_snapshot(tmp_path / "hf", num_layers=4, model_type="qwen3")
    deployment = tmp_path / "compiled/transfer-bundle/deployment"
    deployment.mkdir(parents=True)
    nodes = ("node-a", "node-b")

    def prepare(model_root):
        return prepare_assignment_artifacts(
            deployment, node_ids=nodes,
            model_source=LocalModelSource(root=model_root, model_id="test-org/tiny-dense",
                                          requested_revision="main", resolved_commit=COMMIT),
            runtime_dtype="float32", runtime_backends_by_node={n: "mlx" for n in nodes},
            runtime_quantization="int8-weight-only", layer_ranges=[range(0, 2), range(2, 4)])

    args = Namespace(model_root=snapshot, stage_sharded=False, emit_stage_shards=None,
                     reuse_stage_sharded_root=None, temporary_root=None)
    _, stage_sharding = builder._prepare_model_representation(
        args, prepare, shard_count=2, layer_ranges=[range(0, 2), range(2, 4)])
    assert stage_sharding is None
    source = [p.resolve() for p in _weights(snapshot)]
    assert _weights(deployment)
    assert all(p.lstat().st_nlink == 1 for p in _weights(deployment))
    assert _physical_bytes(source + _weights(deployment)) == _physical_bytes(source)
