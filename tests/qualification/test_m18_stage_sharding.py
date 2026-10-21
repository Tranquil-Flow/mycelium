# SPDX-License-Identifier: AGPL-3.0-only
from __future__ import annotations

import json
from pathlib import Path
import struct

from scripts import build_qwen_live_route as builder

_LAYERS = 4


def _write_checkpoint(root: Path) -> None:
    root.mkdir()
    values = {"model.embed_tokens.weight": b"e", "model.norm.weight": b"n"}
    values.update({f"model.layers.{i}.weight": str(i).encode() for i in range(_LAYERS)})
    header, offset = {}, 0
    for name, payload in sorted(values.items()):
        header[name] = {"dtype": "U8", "shape": [1], "data_offsets": [offset, offset + len(payload)]}
        offset += len(payload)
    encoded = json.dumps(header, sort_keys=True, separators=(",", ":")).encode()
    body = b"".join(values[name] for name in sorted(values))
    (root / "model.safetensors").write_bytes(struct.pack("<Q", len(encoded)) + encoded + body)
    (root / "config.json").write_text(
        json.dumps({"model_type": "qwen2", "num_hidden_layers": _LAYERS}), encoding="utf-8"
    )


def _m18_document() -> dict:
    # A5 shape: node-0 primary of stage-000 with node-2 as its replica, node-3 the tail.
    return {
        "protocol": "mycelium.m18_physical_candidate.v1",
        "route_plan": {
            "legal_tracks": [
                {"placement_ids": ["stage-000-primary", "stage-001-primary"]},
                {"placement_ids": ["stage-000-replica-node-2", "stage-001-primary"]},
            ],
            "placements": [
                {"placement_id": "stage-000-primary", "node_id": "node-0",
                 "layer_range": {"start": 0, "end": 2}},
                {"placement_id": "stage-001-primary", "node_id": "node-3",
                 "layer_range": {"start": 2, "end": _LAYERS}},
                {"placement_id": "stage-000-replica-node-2", "node_id": "node-2",
                 "layer_range": {"start": 0, "end": 2}},
            ],
        },
    }


def test_m18_preparation_uses_the_primary_track_not_every_topology_node() -> None:
    nodes, ranges = builder._preparation_selection(
        _m18_document(),
        node_ids=("node-0", "node-2", "node-3"),
        planned_ranges=None,
        preparation_ranges=None,
    )
    assert nodes == ("node-0", "node-3")
    assert ranges == (range(0, 2), range(2, _LAYERS))


def test_m18_replica_plan_stage_shards_one_shard_per_primary_stage(tmp_path: Path) -> None:
    source = tmp_path / "source"
    _write_checkpoint(source)
    nodes, ranges = builder._preparation_selection(
        _m18_document(),
        node_ids=("node-0", "node-2", "node-3"),
        planned_ranges=None,
        preparation_ranges=None,
    )
    report = builder._shard_stages(source, tmp_path / "shards", nodes, ranges)
    assert [(item["start_layer"], item["end_layer_exclusive"]) for item in report["layer_ranges"]] == [
        (0, 2),
        (2, _LAYERS),
    ]


def test_no_stage_sharding_call_counts_topology_nodes() -> None:
    source = Path(builder.__file__).read_text(encoding="utf-8")
    assert "shard_count=len(node_ids)" not in source
