# SPDX-License-Identifier: AGPL-3.0-only
"""wp8-02 Tranche F — the per-file transfer bound must cover a real 7B shard.

`physical_inference_qualification._MAX_TRANSFER_BYTES` was a 0.5B-era
``2 * 1024 * 1024 * 1024``.  The 0.5B's single shard is 988,097,824 B, so the
bound had never been exercised above 2 GiB; the 7B round's four real shards are
3.5-3.9 GB, and its chain stopped at the base-manifest size guard with
``ValueError: node_transfer_base_invalid`` (rca/wp8-02-node-transfer-cap.md,
compile-error.private.log at :1021).

This file is RED before the fix and GREEN after it, and it keeps the guard
meaningful: above the new bound the same code path still fails closed.

Evidence for the numbers below: the round's own bundle listing,
``runs/wp8-02-useful-two-mac/round/owner/chain-run/transfer-bundle-listing.txt``.
Their sum, 15,231,271,888 B, is the same weight total the 7B resource proposal
already records (``mycelium_a13_macos/serving.py``, ``RESOURCE_PROPOSAL``).

The real ~15 GB bundle is not copied into this tree, so the records here carry
format-valid ``sha256:`` digests rather than the real content digests:
``node_transfer_manifests`` validates the digest *format* only and never reads
the bytes.  The multi-gigabyte files themselves are never materialised — the
sibling-site probes shrink the bound instead of inflating the file.
"""
from __future__ import annotations

import hashlib
import types
from pathlib import Path

import pytest

import physical_inference_qualification as controller_module
from physical_inference_qualification import ControllerError, node_transfer_manifests

# The four source shards of the wp8-02 compile bundle, sorted by path.
SHARD_SIZES = {
    "deployment/model-00001-of-00004.safetensors": 3_945_441_440,
    "deployment/model-00002-of-00004.safetensors": 3_864_726_352,
    "deployment/model-00003-of-00004.safetensors": 3_864_726_424,
    "deployment/model-00004-of-00004.safetensors": 3_556_377_672,
}
BUNDLE_WEIGHT_BYTES = 15_231_271_888
OLD_BOUND_BYTES = 2 * 1024 * 1024 * 1024
NEW_BOUND_BYTES = 8 * 1024 * 1024 * 1024
NODE_SCRIPT_BYTES = 30_004
TOKENIZER_BYTES = 7_031_645


def _digest(label: str) -> str:
    return "sha256:" + hashlib.sha256(label.encode("utf-8")).hexdigest()


def _seven_b_manifest() -> dict:
    """The real 7B base manifest shape: four shards plus shared files."""

    records = [
        {
            "path": path,
            "size_bytes": size,
            "content_digest": _digest(path),
        }
        for path, size in SHARD_SIZES.items()
    ]
    records.append(
        {
            "path": "deployment/tokenizer.json",
            "size_bytes": TOKENIZER_BYTES,
            "content_digest": _digest("tokenizer.json"),
        }
    )
    records.append(
        {
            "path": "physical_inference_node.py",
            "size_bytes": NODE_SCRIPT_BYTES,
            "content_digest": _digest("physical_inference_node.py"),
        }
    )
    assert [record["path"] for record in records] == sorted(
        record["path"] for record in records
    )
    return {
        "protocol": "mycelium.controller_transfer_manifest.v1",
        "files": records,
    }


def _two_mac_selections() -> dict:
    """Four shards over two Macs, every shared file to both (the real split)."""

    shards = sorted(SHARD_SIZES)
    shared = ["deployment/tokenizer.json", "physical_inference_node.py"]
    return {
        "node-a": sorted(shards[:2] + shared),
        "node-b": sorted(shards[2:] + shared),
    }


def _manifest_with_single_shard(size_bytes: int) -> dict:
    return {
        "protocol": "mycelium.controller_transfer_manifest.v1",
        "files": [
            {
                "path": "deployment/model-00001-of-00004.safetensors",
                "size_bytes": size_bytes,
                "content_digest": _digest("shard"),
            },
            {
                "path": "physical_inference_node.py",
                "size_bytes": NODE_SCRIPT_BYTES,
                "content_digest": _digest("physical_inference_node.py"),
            },
        ],
    }


def _single_shard_selections() -> dict:
    return {
        "node-a": [
            "deployment/model-00001-of-00004.safetensors",
            "physical_inference_node.py",
        ]
    }


def test_the_0_5b_era_bound_could_not_hold_any_real_7b_shard() -> None:
    """Documents why the round stopped: every real shard breached the old bound."""

    assert BUNDLE_WEIGHT_BYTES == sum(SHARD_SIZES.values())
    assert min(SHARD_SIZES.values()) > OLD_BOUND_BYTES
    assert max(SHARD_SIZES.values()) == 3_945_441_440


def test_seven_b_base_manifest_is_accepted_by_node_transfer_manifests() -> None:
    """RED before the fix: this is the raise that stopped wp8-02 chain 62r1."""

    manifest = _seven_b_manifest()
    selections = _two_mac_selections()

    result = node_transfer_manifests(manifest, selections)

    assert result["protocol"] == "mycelium.controller_node_transfer_manifests.v1"
    assert set(result["manifests"]) == {"node-a", "node-b"}
    by_path = {record["path"]: record for record in manifest["files"]}
    for node_id, selected in selections.items():
        assert result["manifests"][node_id] == {
            "protocol": "mycelium.controller_transfer_manifest.v1",
            "files": [dict(by_path[path]) for path in selected],
        }


def test_the_per_file_bound_is_eight_gib_with_headroom_over_the_largest_shard() -> None:
    assert controller_module._MAX_TRANSFER_BYTES == NEW_BOUND_BYTES
    assert NEW_BOUND_BYTES > 2 * max(SHARD_SIZES.values())


def test_the_new_bound_still_accepts_exactly_the_maximum() -> None:
    result = node_transfer_manifests(
        _manifest_with_single_shard(NEW_BOUND_BYTES),
        _single_shard_selections(),
    )

    assert result["manifests"]["node-a"]["files"][0]["size_bytes"] == NEW_BOUND_BYTES


def test_the_new_bound_still_fails_closed_one_byte_above_it() -> None:
    with pytest.raises(ValueError, match="node_transfer_base_invalid"):
        node_transfer_manifests(
            _manifest_with_single_shard(NEW_BOUND_BYTES + 1),
            _single_shard_selections(),
        )


def test_the_new_bound_still_fails_closed_for_negative_and_non_int_sizes() -> None:
    for size in (-1, True, 1_000.0):
        with pytest.raises(ValueError, match="node_transfer_base_invalid"):
            node_transfer_manifests(
                _manifest_with_single_shard(size),  # type: ignore[arg-type]
                _single_shard_selections(),
            )


def test_receiver_expected_size_check_uses_the_same_bound(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Sibling site 1: ``_read_verified_transfer_file``'s expected-size guard."""

    boundary = 4_096
    monkeypatch.setattr(controller_module, "_MAX_TRANSFER_BYTES", boundary)
    root = tmp_path / "source"
    root.mkdir()
    payload = b"x" * boundary
    (root / "artifact.bin").write_bytes(payload)
    record = {
        "path": "artifact.bin",
        "size_bytes": boundary,
        "content_digest": "sha256:" + hashlib.sha256(payload).hexdigest(),
    }

    assert controller_module._verify_transfer_file(root, record) == record

    with pytest.raises(ControllerError, match="transfer_size_invalid"):
        controller_module._verify_transfer_file(
            root, {**record, "size_bytes": boundary + 1}
        )


def test_streaming_reader_uses_the_same_cumulative_bound(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Sibling site 2: the streaming loop's cumulative byte bound.

    The loop's bound is the backstop for a file that changes after its size was
    checked, so the probe supplies an over-long read through a module-local
    ``os`` proxy instead of materialising a multi-gigabyte file.
    """

    boundary = 4_096
    monkeypatch.setattr(controller_module, "_MAX_TRANSFER_BYTES", boundary)
    root = tmp_path / "source"
    root.mkdir()
    payload = b"x" * boundary
    (root / "artifact.bin").write_bytes(payload)
    record = {
        "path": "artifact.bin",
        "size_bytes": boundary,
        "content_digest": "sha256:" + hashlib.sha256(payload).hexdigest(),
    }

    real_os = controller_module.os

    class _OverlongReadOS:
        def __getattr__(self, name: str):
            return getattr(real_os, name)

        @staticmethod
        def read(fd: int, count: int) -> bytes:
            chunk = real_os.read(fd, count)
            return chunk if not chunk else chunk + b"\x00" * 1_024

    monkeypatch.setattr(controller_module, "os", _OverlongReadOS())

    with pytest.raises(ControllerError, match="transfer_size_invalid"):
        controller_module._read_verified_transfer_file(root, record)

    # Restoring the real ``os`` shows the same record is accepted at the bound.
    monkeypatch.setattr(controller_module, "os", real_os)
    assert controller_module._verify_transfer_file(root, record) == record


def test_preposition_comparison_uses_the_same_bound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sibling site 3: the node-vs-base prepositioned-record comparison."""

    boundary = 4_096
    monkeypatch.setattr(controller_module, "_MAX_TRANSFER_BYTES", boundary)
    digest = _digest("destination")
    verified = (
        {
            "path": "deployment/artifact.bin",
            "size_bytes": boundary,
            "content_digest": digest,
        },
    )

    def _stub(size_bytes: int) -> types.SimpleNamespace:
        return types.SimpleNamespace(
            peers=(types.SimpleNamespace(node_id="node-a"),),
            _prepositioned_artifacts={
                "protocol": "mycelium.controller_prepositioned_artifacts.v1",
                "members": {
                    "node-a": [
                        {
                            "destination_path": "deployment/artifact.bin",
                            "source_path": "/Users/operator/models/artifact.bin",
                            "size_bytes": size_bytes,
                            "content_digest": digest,
                        }
                    ]
                },
            },
        )

    accepted = controller_module.QualificationController._validate_prepositioned_artifacts(
        _stub(boundary), verified  # type: ignore[arg-type]
    )
    assert accepted["node-a"][0]["size_bytes"] == boundary

    with pytest.raises(ControllerError, match="prepositioned_artifact_binding_invalid"):
        controller_module.QualificationController._validate_prepositioned_artifacts(
            _stub(boundary + 1),  # type: ignore[arg-type]
            verified,
        )


def test_stage_timeout_covers_a_real_7b_shard_archive() -> None:
    """The stage timeout is byte-derived, not model-size-derived: unchanged."""

    per_node_archive = 2 * max(SHARD_SIZES.values())
    timeout = controller_module._stage_timeout_seconds(per_node_archive)

    assert controller_module._MIN_STAGE_TIMEOUT_SECONDS < timeout
    assert timeout <= controller_module._MAX_STAGE_TIMEOUT_SECONDS
    assert timeout == pytest.approx(60.0 + per_node_archive / (512 * 1024))
    assert (
        controller_module._stage_timeout_seconds(BUNDLE_WEIGHT_BYTES)
        == controller_module._MAX_STAGE_TIMEOUT_SECONDS
    )


def test_the_bound_module_constant_is_referenced_by_all_four_sites() -> None:
    """Coherence: one constant, four call sites, no second literal bound."""

    source = Path(controller_module.__file__).read_text(encoding="utf-8")
    assert source.count("_MAX_TRANSFER_BYTES") == 5
    assert "_MAX_TRANSFER_BYTES = 8 * 1024 * 1024 * 1024" in source
