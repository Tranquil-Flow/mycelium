# SPDX-License-Identifier: AGPL-3.0-only
"""The POSIX backend makes the same decisions as the existing node primitives.

Each case runs the backend-neutral API and the existing POSIX body it mirrors
on the same inputs, so the two cannot drift apart silently.
"""

from __future__ import annotations

import sys
import json
import os
from pathlib import Path, PurePosixPath
import stat

import pytest

import mycelium_host_platform
from mycelium_host_platform import HostPlatformError
from mycelium_node import durable_membership, identity, process
import stage_pack


def _code(call) -> str:
    with pytest.raises(HostPlatformError) as caught:
        call()
    return caught.value.code


def _state(tmp_path: Path) -> dict:
    return {
        "protocol": durable_membership.PROTOCOL,
        "node_id": "node-a",
        "swarm_id": "swarm-a",
        "seed_node_id": "seed-a",
        "seed_url": "https://127.0.0.1:4443",
        "seed_key_digest": "sha256:" + "a" * 64,
        "seed_key_records": [{"verification_key_digest": "sha256:" + "a" * 64}],
        "endpoint_id": "endpoint-a",
        "incarnation": "inc-a",
        "membership_generation": 1,
        "restart_count": 0,
    }


def test_private_directory_creation_matches_the_node_lease(tmp_path) -> None:
    ours = tmp_path / "ours" / "state"
    theirs = tmp_path / "theirs" / "state"
    with mycelium_host_platform.private_directory(ours, create=True) as lease:
        lease.revalidate()
    with process.private_directory_lease(theirs, create=True) as lease:
        lease.revalidate()
    if sys.platform == "win32":
        return  # no mode bits or uids: the DACL is owner-only (test_win32_private_directory.py)
    for path in (ours, theirs):
        metadata = path.stat()
        assert stat.S_IMODE(metadata.st_mode) == 0o700
        assert metadata.st_uid == os.getuid()


@pytest.mark.parametrize("mode", [0o755, 0o750, 0o705])
def test_a_widened_directory_is_refused_by_both(tmp_path, mode) -> None:
    target = tmp_path / "state"
    target.mkdir()
    target.chmod(mode)
    assert _code(lambda: mycelium_host_platform.private_directory(target, create=False)) == (
        "host_permissions_invalid"
    )
    with pytest.raises(ValueError):
        process.private_directory_lease(target, create=False)


def test_a_symlinked_directory_is_refused_by_both(tmp_path) -> None:
    real = tmp_path / "real"
    real.mkdir(mode=0o700)
    (tmp_path / "link").symlink_to(real, target_is_directory=True)
    assert _code(
        lambda: mycelium_host_platform.private_directory(tmp_path / "link", create=False)
    ) == "host_path_invalid"
    with pytest.raises(ValueError):
        process.private_directory_lease(tmp_path / "link", create=False)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX mkdir(0o700) setup: on win32 that directory is not owner-only")
def test_atomic_replace_matches_the_membership_state_writer(tmp_path) -> None:
    ours = tmp_path / "ours"
    theirs = tmp_path / "theirs"
    for path in (ours, theirs):
        path.mkdir(mode=0o700)
    value = _state(tmp_path)
    raw = durable_membership.canonical_json_bytes(value)
    with mycelium_host_platform.private_directory(ours, create=False) as lease:
        lease.atomic_replace(durable_membership.FILENAME, raw)
        assert lease.read_file(durable_membership.FILENAME, max_bytes=64 * 1024) == raw
    durable_membership.save_membership_state(theirs, value)
    ours_file = ours / durable_membership.FILENAME
    theirs_file = theirs / durable_membership.FILENAME
    assert ours_file.read_bytes() == theirs_file.read_bytes()
    assert stat.S_IMODE(ours_file.stat().st_mode) == stat.S_IMODE(theirs_file.stat().st_mode) == 0o600
    assert sorted(os.listdir(ours)) == sorted(os.listdir(theirs)) == [durable_membership.FILENAME]
    # Each side reads what the other wrote.
    assert durable_membership.load_membership_state(ours) == json.loads(raw)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX chmod widening: no mode bits on win32")
@pytest.mark.parametrize("mode", [0o644, 0o640, 0o604])
def test_a_widened_file_is_refused_by_both_readers(tmp_path, mode) -> None:
    root = tmp_path / "state"
    root.mkdir(mode=0o700)
    durable_membership.save_membership_state(root, _state(tmp_path))
    (root / durable_membership.FILENAME).chmod(mode)
    with mycelium_host_platform.private_directory(root, create=False) as lease:
        assert _code(
            lambda: lease.read_file(durable_membership.FILENAME, max_bytes=64 * 1024)
        ) == "host_permissions_invalid"
    with pytest.raises(durable_membership.DurableMembershipError):
        durable_membership.load_membership_state(root)


def test_exclusive_create_matches_the_identity_writer(tmp_path) -> None:
    root = tmp_path / "identity"
    signer = identity.load_or_create_node_signer(root / "node.key")
    assert signer is not None
    with mycelium_host_platform.private_directory(root, create=False) as lease:
        assert _code(lambda: lease.create_file_exclusive("node.key", b"x" * 32)) == "host_file_exists"
        lease.create_file_exclusive("other.key", b"y" * 32)
        assert lease.read_file("node.key", max_bytes=33) == (root / "node.key").read_bytes()
    if sys.platform != "win32":  # no mode bits on win32
        for name in ("node.key", "other.key"):
            assert stat.S_IMODE((root / name).stat().st_mode) == 0o600


def test_open_beneath_matches_the_stage_pack_walk(tmp_path) -> None:
    root = tmp_path / "artifacts"
    (root / "a").mkdir(parents=True)
    (root / "a" / "weights.bin").write_bytes(b"w")
    (tmp_path / "outside.bin").write_bytes(b"o")
    (root / "a" / "escape.bin").symlink_to(tmp_path / "outside.bin")
    (root / "b").symlink_to(root / "a", target_is_directory=True)
    with mycelium_host_platform.open_beneath(root, ("a", "weights.bin")) as handle:
        assert handle.read() == b"w"
    with stage_pack._open_beneath(root, PurePosixPath("a/weights.bin")) as handle:
        assert handle.read() == b"w"
    for relative in ("a/escape.bin", "b/weights.bin"):
        with pytest.raises(HostPlatformError):
            mycelium_host_platform.open_beneath(root, tuple(relative.split("/")))
        with pytest.raises(ValueError):
            stage_pack._open_beneath(root, PurePosixPath(relative))
