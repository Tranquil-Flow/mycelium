"""Stream a Safetensors checkpoint into deterministic stage-aligned files."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import struct
from typing import Any, BinaryIO, Sequence


_INDEX_FILE = "model.safetensors.index.json"
_MAX_HEADER_BYTES = 100 * 1024 * 1024
_LAYER_KEY = re.compile(r"^model\.layers\.(\d+)\.")
_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_BLOB = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_COPY_ASSETS = (
    "config.json",
    "generation_config.json",
    "tokenizer.json",
    "vocab.json",
    "merges.txt",
    "tokenizer_config.json",
    "special_tokens_map.json",
    "added_tokens.json",
)


class SafetensorsShardingError(RuntimeError):
    """A checkpoint cannot be safely or exactly partitioned."""


def _reject(code: str) -> None:
    raise SafetensorsShardingError(code)


def _load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SafetensorsShardingError("checkpoint_document_invalid") from exc
    if not isinstance(value, dict):
        _reject("checkpoint_document_invalid")
    return value


def _header(path: Path) -> tuple[dict[str, dict[str, Any]], int]:
    try:
        with path.open("rb") as handle:
            raw_length = handle.read(8)
            if len(raw_length) != 8:
                _reject("safetensors_header_invalid")
            length = struct.unpack("<Q", raw_length)[0]
            if length < 2 or length > _MAX_HEADER_BYTES:
                _reject("safetensors_header_invalid")
            raw = handle.read(length)
            if len(raw) != length:
                _reject("safetensors_header_invalid")
    except OSError as exc:
        raise SafetensorsShardingError("safetensors_read_failed") from exc
    try:
        document = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SafetensorsShardingError("safetensors_header_invalid") from exc
    if not isinstance(document, dict):
        _reject("safetensors_header_invalid")
    document.pop("__metadata__", None)
    if not document:
        _reject("safetensors_header_invalid")
    records: dict[str, dict[str, Any]] = {}
    previous_end = 0
    for name, record in document.items():
        if (
            not isinstance(name, str)
            or not name
            or not isinstance(record, dict)
            or set(record) != {"dtype", "shape", "data_offsets"}
            or not isinstance(record["dtype"], str)
            or not isinstance(record["shape"], list)
            or not all(
                isinstance(item, int) and not isinstance(item, bool) and item >= 0
                for item in record["shape"]
            )
            or not isinstance(record["data_offsets"], list)
            or len(record["data_offsets"]) != 2
        ):
            _reject("safetensors_header_invalid")
        start, end = record["data_offsets"]
        if (
            not isinstance(start, int)
            or isinstance(start, bool)
            or not isinstance(end, int)
            or isinstance(end, bool)
            or start != previous_end
            or end <= start
        ):
            _reject("safetensors_offsets_invalid")
        records[name] = record
        previous_end = end
    if path.stat().st_size != 8 + length + previous_end:
        _reject("safetensors_size_invalid")
    return records, 8 + length


def _copy_range(
    source: BinaryIO,
    destination: BinaryIO,
    *,
    offset: int,
    size: int,
) -> None:
    source.seek(offset)
    remaining = size
    while remaining:
        chunk = source.read(min(1_048_576, remaining))
        if not chunk:
            _reject("safetensors_data_truncated")
        destination.write(chunk)
        remaining -= len(chunk)


def _write_shard(
    destination: Path,
    tensors: Sequence[tuple[str, Path, int, dict[str, Any]]],
) -> int:
    header: dict[str, dict[str, Any]] = {}
    cursor = 0
    for name, _source, _data_start, record in tensors:
        start, end = record["data_offsets"]
        size = end - start
        header[name] = {
            "dtype": record["dtype"],
            "shape": record["shape"],
            "data_offsets": [cursor, cursor + size],
        }
        cursor += size
    encoded = json.dumps(
        header,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    encoded += b" " * ((8 - len(encoded) % 8) % 8)
    destination.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
    descriptor = os.open(destination, flags, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as output:
            descriptor = -1
            output.write(struct.pack("<Q", len(encoded)))
            output.write(encoded)
            handles: dict[Path, BinaryIO] = {}
            try:
                for _name, source_path, data_start, record in tensors:
                    source = handles.get(source_path)
                    if source is None:
                        source = source_path.open("rb")
                        handles[source_path] = source
                    start, end = record["data_offsets"]
                    _copy_range(
                        source,
                        output,
                        offset=data_start + start,
                        size=end - start,
                    )
            finally:
                for handle in handles.values():
                    handle.close()
    except BaseException:
        if descriptor >= 0:
            os.close(descriptor)
        destination.unlink(missing_ok=True)
        raise
    return destination.stat().st_size


def _validated_ranges(
    num_layers: int,
    shard_count: int,
    layer_ranges: Sequence[range] | None,
) -> tuple[range, ...]:
    if shard_count < 2 or num_layers < shard_count:
        _reject("stage_shard_count_invalid")
    if layer_ranges is not None:
        ranges = tuple(layer_ranges)
        if len(ranges) != shard_count:
            _reject("stage_shard_ranges_invalid")
        cursor = 0
        for layer_range in ranges:
            if (
                not isinstance(layer_range, range)
                or layer_range.step != 1
                or layer_range.start != cursor
                or layer_range.stop <= layer_range.start
            ):
                _reject("stage_shard_ranges_invalid")
            cursor = layer_range.stop
        if cursor != num_layers:
            _reject("stage_shard_ranges_invalid")
        return ranges
    base, remainder = divmod(num_layers, shard_count)
    result = []
    start = 0
    for index in range(shard_count):
        count = base + (1 if index < remainder else 0)
        result.append(range(start, start + count))
        start += count
    return tuple(result)


def _source_weight_path(source_root: Path, filename: str) -> Path:
    candidate = source_root / filename
    if candidate.is_symlink():
        target = candidate.readlink()
        if (
            source_root.parent.name != "snapshots"
            or _COMMIT.fullmatch(source_root.name) is None
            or target.is_absolute()
            or len(target.parts) != 4
            or target.parts[:3] != ("..", "..", "blobs")
            or _BLOB.fullmatch(target.parts[3]) is None
        ):
            _reject("checkpoint_weight_path_invalid")
        resolved = candidate.resolve(strict=True)
        if resolved.parent != source_root.parent.parent / "blobs":
            _reject("checkpoint_weight_path_invalid")
        return resolved
    resolved = candidate.resolve(strict=True)
    try:
        resolved.relative_to(source_root)
    except ValueError:
        _reject("checkpoint_weight_path_invalid")
    return resolved


def shard_qwen2_checkpoint(
    source_root: Path,
    destination_root: Path,
    *,
    shard_count: int,
    layer_ranges: Sequence[range] | None = None,
) -> dict[str, Any]:
    """Copy a pinned dense Qwen2/Qwen3 checkpoint into static and layer shards."""

    source_root = Path(source_root).resolve(strict=True)
    destination_root = Path(destination_root)
    if destination_root.exists():
        _reject("stage_shard_destination_exists")
    config = _load_json(source_root / "config.json")
    if config.get("model_type") not in {"qwen2", "qwen3"}:
        _reject("stage_shard_architecture_unsupported")
    num_layers = config.get("num_hidden_layers")
    if not isinstance(num_layers, int) or isinstance(num_layers, bool):
        _reject("stage_shard_layer_count_invalid")
    ranges = _validated_ranges(num_layers, shard_count, layer_ranges)
    layer_to_shard = {
        layer: index for index, layers in enumerate(ranges) for layer in layers
    }
    source_headers: dict[Path, tuple[dict[str, dict[str, Any]], int]] = {}
    index_path = source_root / _INDEX_FILE
    if index_path.is_file():
        index = _load_json(index_path)
        weight_map = index.get("weight_map")
        if not isinstance(weight_map, dict) or not weight_map:
            _reject("checkpoint_index_invalid")
    else:
        single_weight_path = _source_weight_path(source_root, "model.safetensors")
        single_header = _header(single_weight_path)
        source_headers[single_weight_path] = single_header
        weight_map = {name: "model.safetensors" for name in single_header[0]}
    records: dict[str, tuple[Path, int, dict[str, Any]]] = {}
    for name, filename in weight_map.items():
        if not isinstance(name, str) or not isinstance(filename, str):
            _reject("checkpoint_index_invalid")
        source_path = _source_weight_path(source_root, filename)
        source_header = source_headers.get(source_path)
        if source_header is None:
            source_header = _header(source_path)
            source_headers[source_path] = source_header
        header, data_start = source_header
        record = header.get(name)
        if record is None or name in records:
            _reject("checkpoint_index_tensor_mismatch")
        records[name] = (source_path, data_start, record)
    if any(set(header) - set(records) for header, _offset in source_headers.values()):
        _reject("checkpoint_index_tensor_mismatch")

    groups: dict[str, list[tuple[str, Path, int, dict[str, Any]]]] = {
        "model-static.safetensors": []
    }
    for index_value in range(shard_count):
        groups[
            f"model-stage-{index_value + 1:03d}-of-{shard_count:03d}.safetensors"
        ] = []
    output_map: dict[str, str] = {}
    for name in sorted(records):
        matched = _LAYER_KEY.match(name)
        if matched is None:
            filename = "model-static.safetensors"
        else:
            layer = int(matched.group(1))
            shard_index = layer_to_shard.get(layer)
            if shard_index is None:
                _reject("checkpoint_layer_out_of_range")
            filename = (
                f"model-stage-{shard_index + 1:03d}-of-{shard_count:03d}.safetensors"
            )
        source_path, data_start, record = records[name]
        groups[filename].append((name, source_path, data_start, record))
        output_map[name] = filename
    if any(not tensors for tensors in groups.values()):
        _reject("stage_shard_empty")

    destination_root.mkdir(parents=True, mode=0o700)
    try:
        for asset in _COPY_ASSETS:
            source = source_root / asset
            if source.is_file():
                shutil.copyfile(source, destination_root / asset)
                os.chmod(destination_root / asset, 0o600)
        file_sizes = {
            filename: _write_shard(destination_root / filename, tensors)
            for filename, tensors in groups.items()
        }
        raw_tensor_bytes = sum(
            record[2]["data_offsets"][1] - record[2]["data_offsets"][0]
            for record in records.values()
        )
        output_index = {
            "metadata": {"total_size": raw_tensor_bytes},
            "weight_map": output_map,
        }
        (destination_root / _INDEX_FILE).write_text(
            json.dumps(output_index, sort_keys=True, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
        os.chmod(destination_root / _INDEX_FILE, 0o600)
    except BaseException:
        shutil.rmtree(destination_root, ignore_errors=True)
        raise
    return {
        "protocol": "mycelium.stage_sharded_checkpoint.v1",
        "shard_count": shard_count,
        "num_layers": num_layers,
        "layer_ranges": [
            {
                "start_layer": layers.start,
                "end_layer_exclusive": layers.stop,
                "layer_count": len(layers),
            }
            for layers in ranges
        ],
        "files": [
            {"path": path, "size_bytes": file_sizes[path]}
            for path in sorted(file_sizes)
        ],
    }


__all__ = [
    "SafetensorsShardingError",
    "shard_qwen2_checkpoint",
]
