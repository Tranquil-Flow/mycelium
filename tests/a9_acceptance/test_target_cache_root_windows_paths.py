# SPDX-License-Identifier: AGPL-3.0-only
"""A stage target's artifact cache root may be a Windows path.

The coordinator validates a target-owned cache root without resolving it, so the
rule is pure path logic and runs on every host.  Windows devices must be able to
own a stage cache exactly like macOS and Linux devices: a canonical local drive
path is accepted with the same absolute/canonical/no-traversal checks as a POSIX
path.  The target then refuses a root that is not absolute in its own flavor
before it creates anything.
"""
from __future__ import annotations

import hashlib
import json
import os
import struct
import sys
import tempfile
from pathlib import Path

import pytest

import layer_assignment as la
import weight_provisioning as wp


@pytest.mark.parametrize(
    "value",
    [
        "C:\\",
        "C:\\Mycelium\\cache",
        "d:\\cache",
        "C:\\Users\\My PC\\AppData\\Local\\Temp\\pytest-of-My PC\\pytest-0\\case0",
        "C:\\models\\qwen2.5.int8\\v1",
        "/srv/mycelium/cache",
        "/",
    ],
)
def test_canonical_absolute_roots_of_either_flavor_are_accepted(value: str) -> None:
    assert la.validate_target_cache_root(value, "node-a") == value


@pytest.mark.parametrize(
    ("value", "reason"),
    [
        ("C:\\cache\\..\\escape", "canonical"),
        ("C:\\cache\\.\\x", "canonical"),
        ("C:\\cache\\", "canonical"),
        ("C:\\cache\\\\x", "canonical"),
        ("C:\\cache/x", "canonical"),
        ("C:\\cache\\NUL", "canonical"),
        ("C:\\cache\\con.txt", "canonical"),
        ("C:\\cache\\COM1", "canonical"),
        ("C:\\cache\\x.", "canonical"),
        ("C:\\cache\\x ", "canonical"),
        ("C:\\cache:stream", "canonical"),
        ("C:\\cache\\a*b", "canonical"),
        ("C:\\cache\\a\x01b", "canonical"),
        ("C:/cache", "absolute"),
        ("C:cache", "absolute"),
        ("cache\\x", "absolute"),
        ("\\cache", "local drive"),
        ("\\\\server\\share\\cache", "local drive"),
        ("\\\\?\\C:\\cache", "local drive"),
        ("\\\\.\\pipe\\cache", "local drive"),
        ("/tmp/../escape", "canonical"),
        ("/tmp/cache/", "canonical"),
        ("relative", "absolute"),
        ("", "absolute path"),
        ("C:\\cache\x00", "absolute path"),
    ],
)
def test_non_canonical_or_non_local_roots_fail_closed(value: str, reason: str) -> None:
    with pytest.raises(ValueError, match=reason):
        la.validate_target_cache_root(value, "node-a")


def test_a_non_string_root_fails_closed() -> None:
    with pytest.raises(ValueError, match="absolute path"):
        la.validate_target_cache_root(None, "node-a")  # type: ignore[arg-type]


def test_this_hosts_own_temp_root_is_accepted() -> None:
    # Real host spelling: a POSIX path on macOS/Linux, a drive path on Windows.
    root = str(Path(tempfile.gettempdir()).resolve())
    assert la.validate_target_cache_root(root, "node-a") == root


@pytest.mark.skipif(sys.platform != "win32", reason="real Windows path spelling")
def test_a_real_windows_tmp_path_is_accepted(tmp_path: Path) -> None:
    root = str(tmp_path.resolve())
    assert root[1:3] == ":\\"
    assert la.validate_target_cache_root(root, "node-a") == root


def _safetensors(path: Path, names: list[str]) -> None:
    header, payload, offset = {}, bytearray(), 0
    for index, name in enumerate(names):
        data = bytes([index + 1]) * 4
        header[name] = {"dtype": "F32", "shape": [1], "data_offsets": [offset, offset + 4]}
        payload.extend(data)
        offset += 4
    encoded = json.dumps(header, separators=(",", ":")).encode()
    encoded += b" " * ((8 - len(encoded) % 8) % 8)
    path.write_bytes(struct.pack("<Q", len(encoded)) + encoded + payload)


def _assignment(source: Path, cache_root: str) -> dict[str, object]:
    shard = source / "shard.safetensors"
    _safetensors(shard, ["h.0.attn.weight"])
    assignment: dict[str, object] = {
        "protocol": "mycelium.layer_assignment.v2",
        "deployment_id": "12345678-1234-5678-1234-567812345678",
        "deployment_epoch": 1,
        "assignment_id": "87654321-4321-8765-4321-876543218765",
        "node_id": "node-a",
        "manifest_digest": "sha256:" + "a" * 64,
        "model_id": "org/model",
        "resolved_commit": "b" * 40,
        "range": {"start_layer": 0, "end_layer_exclusive": 1, "layer_count": 1},
        "components": ["decoder"],
        "component_tensor_keys": {"decoder": ["h.0.attn.weight"]},
        "component_aliases": {},
        "expected_tensor_prefixes": ["h.0."],
        "expected_tensor_keys": ["h.0.attn.weight"],
        "files": [
            {
                "path": shard.name,
                "size_bytes": shard.stat().st_size,
                "content_digest": "sha256:" + hashlib.sha256(shard.read_bytes()).hexdigest(),
            }
        ],
        "artifact_cache_root": cache_root,
        "runtime": {"backend": "artifact_verifier", "dtype": "source", "quantization": "none"},
        "route_ready": False,
    }
    assignment["assignment_id"] = la.assignment_id_for(assignment)
    return assignment


def _fetch_from(shard: Path):
    def fetch(model_id, revision, filename, cache_root, local_files_only=False):
        assert filename == shard.name
        return shard, True

    return fetch


def test_the_target_refuses_a_root_of_the_other_flavor_before_writing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source"
    work = tmp_path / "cwd"
    source.mkdir()
    work.mkdir()
    foreign = "/srv/mycelium-cache" if os.name == "nt" else "C:\\mycelium-cache"
    assignment = _assignment(source, foreign)
    wp.validate_provisioning_assignment(assignment)  # the coordinator-side rule accepts it
    monkeypatch.chdir(work)
    with pytest.raises(ValueError, match="not absolute on this host"):
        wp.provision_assignment(
            assignment, fetch_file=_fetch_from(source / "shard.safetensors"), local_files_only=True
        )
    assert list(work.iterdir()) == []


def test_the_target_provisions_into_its_own_absolute_root(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    cache = tmp_path / "cache"
    assignment = _assignment(source, str(cache.resolve().parent / "cache"))
    report = wp.provision_assignment(
        assignment, fetch_file=_fetch_from(source / "shard.safetensors"), local_files_only=True
    )
    assert report["ready_for_load"] is True
    assert report["artifact_cache_root"] == assignment["artifact_cache_root"]
    assert cache.is_dir()
