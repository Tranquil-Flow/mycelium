# SPDX-License-Identifier: AGPL-3.0-only
"""Windows managed-host staging: the macOS TargetStaging contract, in process, with a logical root.

Run on POSIX (the adapter is portable) and with the private-directory and lock
seams on the fake win32 API.  The acknowledgements are compared with the macOS
canonical-script adapter on the same bytes and parsed by the real controller.
"""

from __future__ import annotations

import sys
import hashlib
import io
import json
import os
import tarfile

import pytest

import mycelium_host_platform
from mycelium_a13_macos.managed_staging import TargetStaging
from mycelium_a13_windows.managed_host.staging import (
    StagingError,
    WindowsTargetStaging,
    logical_staging_root,
)
from physical_inference_qualification import CommandCapture, PeerIdentity, QualificationController

from tests.host_platform.fake_win32 import simulate_win32

NODE = "managed-node"
RUN = "run-one"


def bundle(*entries):
    entries = entries or (("physical_inference_node.py", b"# fixture bytes, never executed\n"),)
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w") as tar:
        for name, content in entries:
            item = tarfile.TarInfo(name)
            item.size = len(content)
            tar.addfile(item, io.BytesIO(content))
    data = stream.getvalue()
    return data, "sha256:" + hashlib.sha256(data).hexdigest()


def stager(tmp_path, *, member=True, grant=True, **kw):
    return WindowsTargetStaging(
        tmp_path / "mycelium-target", node_id=NODE, run_id=RUN, max_archive_bytes=100_000,
        max_disk_bytes=200_000, current_member=lambda: member, authorize=lambda intent: grant, **kw)


def _peer(root):
    return PeerIdentity(node_id=NODE, ssh_target=None, host_id="host-one", boot_id="boot-one",
                        staging_root=root, process_transport="managed_https")


def _controller_parses(tmp_path, ack, archive, digest):
    target = _peer(ack["staging_root"])
    controller = QualificationController(mode="dry-run", peers=(target,), source_root=tmp_path,
                                         transfer_manifest={}, membership_snapshot={}, now=1.0)
    raw = (json.dumps(ack, sort_keys=True, separators=(",", ":")) + "\n").encode()
    return controller._parse_stage_ack(CommandCapture(argv=(), returncode=0, stdout=raw, stderr=b""),
                                       peer=target, archive_digest=digest, archive_size=len(archive))


def test_logical_root_satisfies_the_controller_peer_contract():
    root = logical_staging_root(NODE, RUN)
    assert root == "/mycelium-managed/managed-node/run-one/mycelium-stage"
    assert _peer(root).staging_root == root
    with pytest.raises(StagingError):
        logical_staging_root("../x", RUN)


def test_stage_ack_matches_the_macos_adapter_and_the_controller_accepts_it(tmp_path):
    archive, digest = bundle(("a/one.bin", b"1" * 10), ("physical_inference_node.py", b"# n\n"))
    windows = stager(tmp_path / "w")
    ack = windows.prepare(archive=archive, archive_digest=digest)
    # The macOS reference adapter is POSIX-only (O_NOFOLLOW, mode bits): compare
    # against it off Windows; on a real Windows host check the Windows side alone.
    windows_host = sys.platform == "win32"
    if not windows_host:
        mac = TargetStaging(tmp_path / "m" / "mycelium-target", node_id=NODE, max_archive_bytes=100_000,
                            max_disk_bytes=200_000, current_member=lambda: True, authorize=lambda i: True)
        mac_ack = mac.prepare(archive=archive, archive_digest=digest)
        assert {**ack, "staging_root": None} == {**mac_ack, "staging_root": None}
    assert ack["staging_root"] == logical_staging_root(NODE, RUN)
    assert _controller_parses(tmp_path, ack, archive, digest) == ack

    def tree(root):
        return sorted((p.relative_to(root).as_posix(), p.read_bytes()) for p in root.rglob("*") if p.is_file())

    if windows_host:
        assert {("a/one.bin", b"1" * 10), ("physical_inference_node.py", b"# n\n")} <= set(
            tree(windows.staging_root)
        )
    else:
        assert tree(windows.staging_root) == tree(mac.staging_root)
    assert str(windows.staging_root).startswith(str(tmp_path / "w"))
    assert windows.prepare(archive=archive, archive_digest=digest) == ack  # exact retry
    assert windows.verify_prepared(archive=archive, archive_digest=digest) == {
        "node_id": NODE, "archive_digest": digest}
    removed = windows.cleanup(archive_digest=digest)
    assert removed == {"protocol": "mycelium.controller_remote_cleanup_ack.v1", "node_id": NODE,
                       "staging_root": logical_staging_root(NODE, RUN), "removed": True}
    assert not windows.staging_root.exists()
    assert windows.cleanup(archive_digest=digest)["removed"] is False


def test_digest_rejection_and_wrong_cleanup_preserve_the_stage(tmp_path):
    stage = stager(tmp_path)
    data, digest = bundle()
    stage.prepare(archive=data, archive_digest=digest)
    with pytest.raises(StagingError, match="stage_archive_digest_mismatch"):
        stage.prepare(archive=data + b"x", archive_digest=digest)
    with pytest.raises(StagingError, match="stage_cleanup_binding_mismatch"):
        stage.cleanup(archive_digest="sha256:" + "0" * 64)
    assert (stage.staging_root / "physical_inference_node.py").is_file()
    other, other_digest = bundle(("other.py", b"x"))
    with pytest.raises(StagingError, match="stage_existing_binding_mismatch"):
        stage.prepare(archive=other, archive_digest=other_digest)


@pytest.mark.parametrize("name", ["../escaped", "/absolute", ".mycelium-stage.json", "x/../../escaped",
                                  "C:/drive", "a\\b"])
def test_unsafe_archive_rejected_without_target_write(tmp_path, name):
    stage = stager(tmp_path)
    data, digest = bundle((name, b"x"))
    with pytest.raises(StagingError):
        stage.prepare(archive=data, archive_digest=digest)
    assert not stage.staging_root.exists()


def test_member_and_approval_required_before_any_staging(tmp_path):
    data, digest = bundle()
    for suffix, member, grant in (("revoked", False, True), ("unapproved", True, False)):
        stage = stager(tmp_path / suffix, member=member, grant=grant)
        with pytest.raises(StagingError, match="stage_not_authorized"):
            stage.prepare(archive=data, archive_digest=digest)
        assert not stage.staging_root.exists()


def test_preposition_copies_only_from_the_approved_cache(tmp_path):
    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / "tensor").write_bytes(b"not a model")
    data, digest = bundle()
    stage = stager(tmp_path, artifact_cache_root=cache)
    record = {"content_digest": "sha256:" + hashlib.sha256(b"not a model").hexdigest(),
              "destination_path": "deployment/tensor", "source_path": str(cache / "tensor"), "size_bytes": 11}
    doc = {"protocol": "mycelium.controller_prepositioned_member_artifacts.v1", "files": [record]}
    ack = stage.prepare(archive=data, archive_digest=digest, preposition=doc)
    assert (stage.staging_root / "deployment" / "tensor").read_bytes() == b"not a model"
    stage.cleanup(archive_digest=digest, preposition_digest=ack["preposition_digest"])
    for bad in (str(tmp_path / "outside"), str(cache / "missing")):
        with pytest.raises(StagingError, match="stage_preposition_invalid"):
            stage.prepare(archive=data, archive_digest=digest, preposition={**doc, "files": [{**record, "source_path": bad}]})
    os.symlink(cache / "tensor", cache / "link")
    with pytest.raises(StagingError, match="stage_preposition_invalid"):
        stage.prepare(archive=data, archive_digest=digest,
                      preposition={**doc, "files": [{**record, "source_path": str(cache / "link")}]})
    wrong = {**record, "content_digest": "sha256:" + "0" * 64}
    with pytest.raises(StagingError, match="stage_preposition_invalid"):
        stage.prepare(archive=data, archive_digest=digest, preposition={**doc, "files": [wrong]})
    assert not stage.staging_root.exists()


def test_a_symlinked_stage_root_is_refused(tmp_path):
    stage = stager(tmp_path)
    data, digest = bundle()
    os.symlink(tmp_path, stage.staging_root)
    with pytest.raises(StagingError):
        stage.prepare(archive=data, archive_digest=digest)
    with pytest.raises(StagingError):
        stage.cleanup(archive_digest=digest)


def test_concurrent_staging_is_refused_not_interleaved(tmp_path):
    stage = stager(tmp_path)
    data, digest = bundle()
    held = mycelium_host_platform.hold_exclusive_lock(stage.run_root, "stage.lock")
    try:
        with pytest.raises(StagingError, match="stage_busy"):
            stage.prepare(archive=data, archive_digest=digest)
    finally:
        held.release()
    assert stage.prepare(archive=data, archive_digest=digest)["archive_digest"] == digest


def test_stage_and_cleanup_on_the_win32_backend(monkeypatch, tmp_path):
    fake = simulate_win32(monkeypatch)
    stage = stager(tmp_path)
    data, digest = bundle()
    ack = stage.prepare(archive=data, archive_digest=digest)
    assert ack["staging_root"] == logical_staging_root(NODE, RUN)
    assert any(call[0] == "lock_file" for call in fake.calls)
    held = mycelium_host_platform.hold_exclusive_lock(stage.run_root, "stage.lock")
    try:
        with pytest.raises(StagingError, match="stage_busy"):
            stage.cleanup(archive_digest=digest)
    finally:
        held.release()
    assert stage.cleanup(archive_digest=digest)["removed"] is True
