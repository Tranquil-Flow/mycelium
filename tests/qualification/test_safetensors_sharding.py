from __future__ import annotations

import json
from pathlib import Path
import struct

import pytest

from mycelium_qualification.safetensors_sharding import (
    SafetensorsShardingError,
    _header,
    shard_qwen2_checkpoint,
)


def _write_checkpoint(root: Path) -> dict[str, bytes]:
    root.mkdir()
    values = {
        "model.embed_tokens.weight": b"e",
        "model.layers.0.weight": b"0",
        "model.layers.1.weight": b"1",
        "model.layers.2.weight": b"2",
        "model.layers.3.weight": b"3",
        "model.norm.weight": b"n",
    }
    header = {}
    offset = 0
    for name, payload in sorted(values.items()):
        header[name] = {
            "dtype": "U8",
            "shape": [1],
            "data_offsets": [offset, offset + len(payload)],
        }
        offset += len(payload)
    encoded = json.dumps(header, sort_keys=True, separators=(",", ":")).encode()
    encoded += b" " * ((8 - len(encoded) % 8) % 8)
    weight_file = root / "model.safetensors"
    weight_file.write_bytes(
        struct.pack("<Q", len(encoded))
        + encoded
        + b"".join(values[name] for name in sorted(values))
    )
    (root / "model.safetensors.index.json").write_text(
        json.dumps(
            {
                "metadata": {"total_size": sum(map(len, values.values()))},
                "weight_map": {name: weight_file.name for name in values},
            }
        ),
        encoding="utf-8",
    )
    (root / "config.json").write_text(
        json.dumps({"model_type": "qwen2", "num_hidden_layers": 4}),
        encoding="utf-8",
    )
    (root / "tokenizer.json").write_text("{}", encoding="utf-8")
    return values


def _tensor_bytes(path: Path) -> dict[str, bytes]:
    header, data_start = _header(path)
    payload = path.read_bytes()
    return {
        name: payload[data_start + record["data_offsets"][0] : data_start + record["data_offsets"][1]]
        for name, record in header.items()
    }


def test_shards_qwen_checkpoint_by_contiguous_layer_range(tmp_path: Path) -> None:
    source = tmp_path / "source"
    values = _write_checkpoint(source)
    destination = tmp_path / "sharded"

    report = shard_qwen2_checkpoint(source, destination, shard_count=2)

    assert report["protocol"] == "mycelium.stage_sharded_checkpoint.v1"
    assert report["layer_ranges"] == [
        {"start_layer": 0, "end_layer_exclusive": 2, "layer_count": 2},
        {"start_layer": 2, "end_layer_exclusive": 4, "layer_count": 2},
    ]
    index = json.loads((destination / "model.safetensors.index.json").read_text())
    weight_map = index["weight_map"]
    assert weight_map["model.embed_tokens.weight"] == "model-static.safetensors"
    assert weight_map["model.norm.weight"] == "model-static.safetensors"
    assert weight_map["model.layers.0.weight"] == "model-stage-001-of-002.safetensors"
    assert weight_map["model.layers.3.weight"] == "model-stage-002-of-002.safetensors"
    recovered = {}
    for filename in sorted(set(weight_map.values())):
        recovered.update(_tensor_bytes(destination / filename))
    assert recovered == values
    assert (destination / "tokenizer.json").read_text() == "{}"


def test_shards_single_file_checkpoint_without_index(tmp_path: Path) -> None:
    source = tmp_path / "source"
    values = _write_checkpoint(source)
    (source / "model.safetensors.index.json").unlink()

    destination = tmp_path / "sharded"
    shard_qwen2_checkpoint(source, destination, shard_count=2)

    index = json.loads((destination / "model.safetensors.index.json").read_text())
    recovered = {}
    for filename in sorted(set(index["weight_map"].values())):
        recovered.update(_tensor_bytes(destination / filename))
    assert recovered == values
    assert index["metadata"]["total_size"] == sum(map(len, values.values()))


def test_shards_exact_planner_ranges_and_rejects_non_covering_ranges(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    values = _write_checkpoint(source)
    destination = tmp_path / "planner-sharded"

    report = shard_qwen2_checkpoint(
        source,
        destination,
        shard_count=2,
        layer_ranges=(range(0, 3), range(3, 4)),
    )

    assert report["layer_ranges"] == [
        {"start_layer": 0, "end_layer_exclusive": 3, "layer_count": 3},
        {"start_layer": 3, "end_layer_exclusive": 4, "layer_count": 1},
    ]
    index = json.loads((destination / "model.safetensors.index.json").read_text())
    assert index["weight_map"]["model.layers.2.weight"].startswith(
        "model-stage-001"
    )
    assert index["weight_map"]["model.layers.3.weight"].startswith(
        "model-stage-002"
    )
    recovered = {}
    for filename in sorted(set(index["weight_map"].values())):
        recovered.update(_tensor_bytes(destination / filename))
    assert recovered == values

    with pytest.raises(SafetensorsShardingError, match="stage_shard_ranges_invalid"):
        shard_qwen2_checkpoint(
            source,
            tmp_path / "invalid-ranges",
            shard_count=2,
            layer_ranges=(range(0, 2), range(3, 4)),
        )


def test_rejects_non_qwen_and_existing_destination(tmp_path: Path) -> None:
    source = tmp_path / "source"
    _write_checkpoint(source)
    (source / "config.json").write_text(
        json.dumps({"model_type": "gpt2", "num_hidden_layers": 4}),
        encoding="utf-8",
    )
    with pytest.raises(
        SafetensorsShardingError,
        match="^stage_shard_architecture_unsupported$",
    ):
        shard_qwen2_checkpoint(source, tmp_path / "output", shard_count=2)

    (tmp_path / "exists").mkdir()
    with pytest.raises(
        SafetensorsShardingError,
        match="^stage_shard_destination_exists$",
    ):
        shard_qwen2_checkpoint(source, tmp_path / "exists", shard_count=2)
