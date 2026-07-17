#!/usr/bin/env python3
"""Assignment-bound, local-only MLX stage loading and load-proof emission."""

from __future__ import annotations

import copy
import hashlib
import importlib.metadata
import json
import math
import os
import re
import stat
import struct
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Any, BinaryIO, Mapping, NoReturn

import mlx.core as mx

from layer_assignment import validate_assignment_identity
from model_adapters import ADAPTERS
from runtime_contracts import (
    GPT2_DECODER_TENSOR_SUFFIXES,
    validate_normalized_mlx_runtime,
)
from weight_provisioning import artifact_report_errors


LAYER_LOAD_PROOF_PROTOCOL = "mycelium.layer_load_proof.v1"
_CONTROL_PLANE_BINDING_PROTOCOL = "mycelium.control_plane_binding.v1"
_SHA256_REF_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
_SAFE_ARTIFACT_RE = re.compile(r"^[A-Za-z0-9._/-]+$")
_MAX_SAFETENSORS_HEADER_BYTES = 100 * 1024 * 1024
_SAFE_DTYPE_BYTES = {
    "BOOL": 1,
    "U8": 1,
    "I8": 1,
    "F8_E4M3": 1,
    "F8_E5M2": 1,
    "I16": 2,
    "U16": 2,
    "F16": 2,
    "BF16": 2,
    "I32": 4,
    "U32": 4,
    "F32": 4,
    "I64": 8,
    "U64": 8,
    "F64": 8,
}
_SUPPORTED_SOURCE_DTYPES = {
    "mlx.core.bfloat16",
    "mlx.core.float16",
    "mlx.core.float32",
}
_RUNTIME_DTYPES = {
    "bfloat16": mx.bfloat16,
    "float16": mx.float16,
    "float32": mx.float32,
}


class RuntimeLoadError(ValueError):
    """Permanent, fail-closed assignment loading failure."""


class RuntimeExecutionError(ValueError):
    """Fail-closed execution error for an already loaded local stage."""


@dataclass(frozen=True)
class LoadedStage:
    """Materialized assignment tensors plus deterministic local proof evidence."""

    tensors: Mapping[str, mx.array]
    resolved_aliases: Mapping[str, Any]
    probe_output: mx.array
    proof: Mapping[str, Any]


def _json_compatible(value: Any) -> Any:
    if isinstance(value, Mapping):
        if not all(isinstance(key, str) for key in value):
            raise TypeError("canonical JSON mappings require string keys")
        return {key: _json_compatible(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_compatible(item) for item in value]
    return value


def _deep_freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType(
            {key: _deep_freeze(item) for key, item in value.items()}
        )
    if isinstance(value, (list, tuple)):
        return tuple(_deep_freeze(item) for item in value)
    return value


def canonical_json(document: Any) -> str:
    """Serialize proof material with one stable JSON representation."""
    return json.dumps(
        _json_compatible(document),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def _fail(message: str) -> RuntimeLoadError:
    return RuntimeLoadError(message)


def _require_nonnegative_int(value: Any, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise _fail(f"{field} must be a non-negative integer")
    return value


def _validate_control_plane_binding(assignment: dict[str, Any]) -> dict[str, Any]:
    binding = assignment.get("control_plane_binding")
    if not isinstance(binding, dict) or not binding:
        raise _fail("control_plane_binding is required for runtime loading")
    if binding.get("protocol") != _CONTROL_PLANE_BINDING_PROTOCOL:
        raise _fail("unsupported control_plane_binding protocol")
    expected_fields = {
        "protocol",
        "evidence_bundle_digest",
        "planner_snapshot_digest",
        "snapshot_generation",
        "swarm_id",
        "deployment_id",
        "deployment_epoch",
    }
    if set(binding) != expected_fields:
        raise _fail("control_plane_binding fields do not match the v1 contract")
    for field in ("evidence_bundle_digest", "planner_snapshot_digest"):
        if not _SHA256_REF_RE.fullmatch(str(binding.get(field, ""))):
            raise _fail(f"control_plane_binding {field} is invalid")
    _require_nonnegative_int(
        binding.get("snapshot_generation"), "control_plane_binding snapshot_generation"
    )
    if not isinstance(binding.get("swarm_id"), str) or not binding["swarm_id"]:
        raise _fail("control_plane_binding swarm_id is required")
    for field in ("deployment_id", "deployment_epoch"):
        if binding.get(field) != assignment.get(field):
            raise _fail(f"control-plane {field} does not match assignment")
    return copy.deepcopy(binding)


def _validate_runtime(runtime: Any) -> tuple[dict[str, Any], Any]:
    try:
        normalized = validate_normalized_mlx_runtime(runtime)
    except (TypeError, ValueError) as exc:
        raise _fail(str(exc)) from exc
    return normalized, _RUNTIME_DTYPES[normalized["dtype"]]


def _validate_range_and_prefixes(
    assignment: dict[str, Any],
) -> tuple[int, int, list[str], str]:
    layer_range = assignment.get("range")
    if not isinstance(layer_range, dict):
        raise _fail("assignment layer range is missing")
    start = layer_range.get("start_layer")
    end = layer_range.get("end_layer_exclusive")
    count = layer_range.get("layer_count")
    if (
        not isinstance(start, int)
        or isinstance(start, bool)
        or start < 0
        or not isinstance(end, int)
        or isinstance(end, bool)
        or end <= start
        or not isinstance(count, int)
        or isinstance(count, bool)
        or count != end - start
    ):
        raise _fail("assignment layer range is invalid")
    prefixes = assignment.get("expected_tensor_prefixes")
    if not isinstance(prefixes, list) or not all(
        isinstance(value, str) and value for value in prefixes
    ):
        raise _fail("assignment expected tensor prefixes are invalid")
    adapter = ADAPTERS["gpt2"]
    candidates = (
        adapter.block_prefix_template,
        *adapter.alternate_block_prefix_templates,
    )
    selected = None
    for template in candidates:
        if prefixes == [template.format(layer=layer) for layer in range(start, end)]:
            selected = template
            break
    if selected is None:
        raise _fail(
            "gpt2 expected tensor prefixes do not match the assigned layer range"
        )
    namespace = "transformer." if selected.startswith("transformer.") else ""
    return start, end, list(prefixes), namespace


def _resolve_aliases(
    components: list[str],
    component_keys: dict[str, list[str]],
    aliases: Any,
    namespace: str,
) -> dict[str, dict[str, Any]]:
    if not isinstance(aliases, dict) or not all(
        isinstance(source, str) and isinstance(target, str) and source and target
        for source, target in aliases.items()
    ):
        raise _fail("component_aliases must map non-empty component names")
    if set(aliases) - set(components):
        raise _fail("component alias source is not assignment-owned")
    resolved: dict[str, dict[str, Any]] = {}
    for source, target in aliases.items():
        if source != "lm_head" or target != "input_embedding":
            raise _fail(f"unsupported component alias: {source} -> {target}")
        expected_source = [f"{namespace}wte.weight"]
        if component_keys.get(source) != expected_source:
            raise _fail("tied gpt2 lm_head alias does not resolve to token embeddings")
        resolved[source] = {
            "target_component": target,
            "tensor_keys": list(expected_source),
        }
    return resolved


def _validate_component_ownership(
    assignment: dict[str, Any],
    prefixes: list[str],
    namespace: str,
) -> tuple[list[str], dict[str, dict[str, Any]]]:
    components = assignment.get("components")
    if (
        not isinstance(components, list)
        or not components
        or not all(isinstance(component, str) and component for component in components)
        or len(components) != len(set(components))
    ):
        raise _fail("assignment components must be a unique non-empty list")
    supported_components = {"input_embedding", "decoder", "final_norm", "lm_head"}
    unsupported = sorted(set(components) - supported_components)
    if unsupported:
        raise _fail(f"unsupported gpt2 components: {', '.join(unsupported)}")
    if "decoder" not in components:
        raise _fail("assignment must own decoder layers")

    raw_component_keys = assignment.get("component_tensor_keys")
    if not isinstance(raw_component_keys, dict) or set(raw_component_keys) != set(
        components
    ):
        raise _fail("component ownership does not match assignment components")
    component_keys: dict[str, list[str]] = {}
    for component in components:
        keys = raw_component_keys.get(component)
        if (
            not isinstance(keys, list)
            or not keys
            or not all(isinstance(key, str) and key for key in keys)
            or keys != sorted(keys)
            or len(keys) != len(set(keys))
        ):
            raise _fail(f"component ownership for {component} is invalid")
        component_keys[component] = list(keys)

    expected_keys = assignment.get("expected_tensor_keys")
    if not isinstance(expected_keys, list) or not all(
        isinstance(key, str) and key for key in expected_keys
    ):
        raise _fail("assignment expected tensor keys are invalid")
    if len(expected_keys) != len(set(expected_keys)):
        raise _fail("duplicate expected tensor key")
    if expected_keys != sorted(expected_keys):
        raise _fail("assignment expected tensor keys must be sorted")
    owned_keys = {key for keys in component_keys.values() for key in keys}
    if owned_keys != set(expected_keys):
        raise _fail("expected tensor keys do not match component ownership")

    aliases = _resolve_aliases(
        components,
        component_keys,
        assignment.get("component_aliases"),
        namespace,
    )
    owners_by_key: dict[str, list[str]] = {}
    for component, keys in component_keys.items():
        for key in keys:
            owners_by_key.setdefault(key, []).append(component)
    for key, owners in owners_by_key.items():
        if len(owners) == 1:
            continue
        if set(owners) != {"input_embedding", "lm_head"} or "lm_head" not in aliases:
            raise _fail(f"duplicate tensor ownership without an explicit alias: {key}")

    decoder_keys = component_keys["decoder"]
    expected_decoder_keys = sorted(
        prefix + suffix
        for prefix in prefixes
        for suffix in GPT2_DECODER_TENSOR_SUFFIXES
    )
    if decoder_keys != expected_decoder_keys:
        raise _fail("gpt2 decoder tensor ownership is missing, extra, or mismatched")
    expected_static: dict[str, list[str]] = {
        "input_embedding": sorted((f"{namespace}wpe.weight", f"{namespace}wte.weight")),
        "final_norm": sorted((f"{namespace}ln_f.bias", f"{namespace}ln_f.weight")),
    }
    for component, keys in expected_static.items():
        if component in component_keys and component_keys[component] != keys:
            raise _fail(
                f"gpt2 {component} tensor ownership is missing, extra, or mismatched"
            )
    if "lm_head" in component_keys and "lm_head" not in aliases:
        if component_keys["lm_head"] != ["lm_head.weight"]:
            raise _fail(
                "gpt2 lm_head tensor ownership is missing, extra, or mismatched"
            )
    return list(expected_keys), aliases


def _validate_stage_boundaries(
    assignment: dict[str, Any], components: list[str], runtime: dict[str, Any]
) -> None:
    layer_range = assignment["range"]
    start = layer_range["start_layer"]
    end = layer_range["end_layer_exclusive"]
    total_layers = runtime["model_config"]["n_layer"]
    if end > total_layers:
        raise _fail("assigned layer range exceeds bound gpt2 model depth")
    if "input_embedding" in components and start != 0:
        raise _fail("input_embedding may only be assigned with the first layer")
    terminal_components = {"final_norm", "lm_head"}.intersection(components)
    if terminal_components and end != total_layers:
        raise _fail("final_norm and lm_head may only be assigned with the final layer")


def _validate_assignment(
    assignment: Any,
    load_generation: Any,
) -> tuple[
    dict[str, Any],
    Any,
    list[str],
    dict[str, dict[str, Any]],
    int,
    int,
    str,
    dict[str, Any],
]:
    if not isinstance(assignment, dict):
        raise _fail("assignment must be an object")
    if assignment.get("protocol") != "mycelium.layer_assignment.v2":
        raise _fail("expected mycelium.layer_assignment.v2")
    try:
        validate_assignment_identity(assignment)
    except (KeyError, TypeError, ValueError) as exc:
        raise _fail(f"invalid assignment identity: {exc}") from exc
    _require_nonnegative_int(load_generation, "load_generation")
    if not isinstance(assignment.get("model_id"), str) or not assignment["model_id"]:
        raise _fail("assignment model_id is required")
    if not _COMMIT_RE.fullmatch(str(assignment.get("resolved_commit", ""))):
        raise _fail("assignment resolved_commit is not immutable 40-hex")
    if not _SHA256_REF_RE.fullmatch(str(assignment.get("manifest_digest", ""))):
        raise _fail("assignment manifest_digest is invalid")
    if not isinstance(assignment.get("node_id"), str) or not assignment["node_id"]:
        raise _fail("assignment node_id is required")
    _require_nonnegative_int(
        assignment.get("deployment_epoch"), "assignment deployment_epoch"
    )
    binding = _validate_control_plane_binding(assignment)
    runtime, runtime_dtype = _validate_runtime(assignment.get("runtime"))
    start, end, prefixes, namespace = _validate_range_and_prefixes(assignment)
    expected_keys, aliases = _validate_component_ownership(
        assignment, prefixes, namespace
    )
    _validate_stage_boundaries(assignment, assignment["components"], runtime)
    return (
        runtime,
        runtime_dtype,
        expected_keys,
        aliases,
        start,
        end,
        namespace,
        binding,
    )


def _validate_artifact_report(
    assignment: dict[str, Any], report: Any
) -> dict[str, dict[str, Any]]:
    if not isinstance(report, dict):
        raise _fail("artifact verification report must be an object")
    errors = artifact_report_errors(assignment, report)
    if errors:
        raise _fail("artifact verification report rejected: " + "; ".join(errors))
    verified_files = report.get("verified_files")
    if not isinstance(verified_files, list):
        raise _fail("artifact verification report lacks verified_files")
    indexed: dict[str, dict[str, Any]] = {}
    for record in verified_files:
        if not isinstance(record, dict) or not isinstance(record.get("path"), str):
            raise _fail("artifact verification report has invalid verified file")
        path = record["path"]
        if path in indexed:
            raise _fail(f"artifact verification report has duplicate file: {path}")
        local_path = record.get("local_path")
        if not isinstance(local_path, str) or not Path(local_path).is_absolute():
            raise _fail(f"verified file lacks absolute local_path: {path}")
        indexed[path] = record
    return indexed


def _safe_relative_artifact_path(value: Any) -> PurePosixPath:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 1024
        or not _SAFE_ARTIFACT_RE.fullmatch(value)
    ):
        raise _fail(f"unsafe assigned artifact path: {value!r}")
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or len(path.parts) > 16
        or any(part in ("", ".", "..") for part in path.parts)
    ):
        raise _fail(f"unsafe assigned artifact path: {value!r}")
    return path


def _open_verified_artifact(
    assignment: dict[str, Any],
    report: dict[str, Any],
    assigned_record: dict[str, Any],
    verified_record: dict[str, Any],
) -> tuple[BinaryIO, tuple[int, int]]:
    relative = _safe_relative_artifact_path(assigned_record.get("path"))
    cache_root_raw = assignment.get("artifact_cache_root")
    if not isinstance(cache_root_raw, str) or not Path(cache_root_raw).is_absolute():
        raise _fail("assignment artifact_cache_root must be absolute")
    try:
        cache_root = Path(cache_root_raw).resolve(strict=True)
    except OSError as exc:
        raise _fail("assignment artifact_cache_root is unavailable") from exc
    if report.get("resolved_artifact_cache_root") != str(cache_root):
        raise _fail("artifact report resolved cache root mismatch")
    reported_path = Path(verified_record["local_path"])
    try:
        resolved_path = reported_path.resolve(strict=True)
        resolved_path.relative_to(cache_root)
    except (OSError, ValueError) as exc:
        raise _fail(
            f"verified local path escapes artifact cache root: {relative}"
        ) from exc
    if reported_path.name != relative.name:
        raise _fail(f"verified local path basename mismatch: {relative}")

    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(resolved_path, flags)
    except OSError as exc:
        raise _fail(f"unable to open verified artifact: {relative}") from exc
    handle = os.fdopen(descriptor, "rb")
    try:
        metadata = os.fstat(handle.fileno())
        if not stat.S_ISREG(metadata.st_mode):
            raise _fail(f"verified artifact is not a regular file: {relative}")
        if metadata.st_nlink != 1:
            raise _fail(f"verified artifact must have exactly one hard link: {relative}")
        if metadata.st_size != assigned_record.get("size_bytes"):
            raise _fail(f"verified artifact size mismatch: {relative}")
        digest = hashlib.sha256()
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
        actual_digest = "sha256:" + digest.hexdigest()
        if actual_digest != assigned_record.get("content_digest"):
            raise _fail(f"verified artifact digest mismatch: {relative}")
        handle.seek(0)
        return handle, (metadata.st_dev, metadata.st_ino)
    except BaseException:
        handle.close()
        raise


def _validate_safetensors_header(handle: BinaryIO, path: str) -> set[str]:
    """Validate canonical, non-aliased Safetensors storage before MLX sees it."""
    try:
        prefix = handle.read(8)
        if len(prefix) != 8:
            raise _fail(f"invalid Safetensors header length: {path}")
        header_length = struct.unpack("<Q", prefix)[0]
        file_size = os.fstat(handle.fileno()).st_size
        if (
            header_length == 0
            or header_length > _MAX_SAFETENSORS_HEADER_BYTES
            or header_length > file_size - 8
        ):
            raise _fail(f"invalid Safetensors header length: {path}")
        header_bytes = handle.read(header_length)
        if len(header_bytes) != header_length:
            raise _fail(f"truncated Safetensors header: {path}")

        def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
            result: dict[str, Any] = {}
            for key, value in pairs:
                if key in result:
                    raise _fail(f"duplicate Safetensors tensor or metadata name: {key}")
                result[key] = value
            return result

        def reject_constant(value: str) -> None:
            raise _fail(f"non-finite Safetensors header value: {value}")

        header = json.loads(
            header_bytes,
            object_pairs_hook=reject_duplicates,
            parse_constant=reject_constant,
        )
        if not isinstance(header, dict) or not header:
            raise _fail(f"invalid Safetensors header object: {path}")
        metadata = header.pop("__metadata__", None)
        if metadata is not None and (
            not isinstance(metadata, dict)
            or not all(
                isinstance(key, str) and isinstance(value, str)
                for key, value in metadata.items()
            )
        ):
            raise _fail(f"invalid Safetensors metadata: {path}")
        if not header:
            raise _fail(f"Safetensors artifact contains no tensors: {path}")

        data_size = file_size - 8 - header_length
        intervals: list[tuple[int, int, str]] = []
        tensor_names: set[str] = set()
        for name, entry in header.items():
            if not isinstance(name, str) or not name or not isinstance(entry, dict):
                raise _fail(f"invalid Safetensors tensor entry: {path}")
            if set(entry) != {"dtype", "shape", "data_offsets"}:
                raise _fail(f"invalid Safetensors tensor metadata: {name}")
            dtype = entry["dtype"]
            shape = entry["shape"]
            offsets = entry["data_offsets"]
            if dtype not in _SAFE_DTYPE_BYTES:
                raise _fail(f"unsupported Safetensors dtype for tensor {name}: {dtype}")
            if not isinstance(shape, list) or any(
                not isinstance(dimension, int)
                or isinstance(dimension, bool)
                or dimension < 0
                for dimension in shape
            ):
                raise _fail(f"invalid Safetensors shape for tensor {name}")
            if (
                not isinstance(offsets, list)
                or len(offsets) != 2
                or any(
                    not isinstance(offset, int) or isinstance(offset, bool)
                    for offset in offsets
                )
            ):
                raise _fail(f"invalid Safetensors offsets for tensor {name}")
            start, end = offsets
            if start < 0 or end < start or end > data_size:
                raise _fail(f"out-of-bounds Safetensors data for tensor {name}")
            element_count = 1
            for dimension in shape:
                element_count *= dimension
            if end - start != element_count * _SAFE_DTYPE_BYTES[dtype]:
                raise _fail(f"Safetensors byte length does not match tensor {name}")
            tensor_names.add(name)
            intervals.append((start, end, name))

        ordered_intervals = sorted(intervals)
        previous_end = 0
        for start, end, name in ordered_intervals:
            if start < previous_end:
                raise _fail(f"overlapping Safetensors data for tensor {name}")
            previous_end = max(previous_end, end)
        cursor = 0
        for start, end, name in ordered_intervals:
            if start > cursor:
                raise _fail(f"unindexed Safetensors data before tensor {name}")
            cursor = max(cursor, end)
        if cursor != data_size:
            raise _fail(f"unindexed trailing Safetensors data: {path}")
        return tensor_names
    except RuntimeLoadError:
        raise
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, struct.error) as exc:
        raise _fail(f"invalid Safetensors header: {path}") from exc
    finally:
        handle.seek(0)


def _load_exact_tensors(
    assignment: dict[str, Any],
    report: dict[str, Any],
    verified_by_path: dict[str, dict[str, Any]],
    expected_keys: list[str],
    runtime_dtype: Any,
) -> dict[str, mx.array]:
    files = assignment.get("files")
    if not isinstance(files, list) or not files:
        raise _fail("assignment requires verified artifact files")
    expected_set = set(expected_keys)
    loaded: dict[str, mx.array] = {}
    seen_inodes: set[tuple[int, int]] = set()
    for assigned_record in files:
        if not isinstance(assigned_record, dict) or not isinstance(
            assigned_record.get("path"), str
        ):
            raise _fail("assignment has invalid artifact file record")
        path = assigned_record["path"]
        verified_record = verified_by_path.get(path)
        if verified_record is None:
            raise _fail(f"assigned artifact is not verified: {path}")
        handle, inode = _open_verified_artifact(
            assignment, report, assigned_record, verified_record
        )
        try:
            if inode in seen_inodes:
                raise _fail(f"duplicate verified artifact inode: {path}")
            seen_inodes.add(inode)
            header_tensor_names = _validate_safetensors_header(handle, path)
            try:
                source_tensors = mx.load(handle, format="safetensors")
            except Exception as exc:
                raise _fail(
                    f"MLX could not load verified Safetensors artifact: {path}"
                ) from exc
            if not isinstance(source_tensors, dict):
                raise _fail(f"MLX artifact did not contain named tensors: {path}")
            if set(source_tensors) != header_tensor_names:
                raise _fail(f"MLX tensor names do not match verified header: {path}")
            selected = expected_set.intersection(source_tensors)
            for key in selected:
                if key in loaded:
                    raise _fail(
                        f"duplicate assigned tensor across verified files: {key}"
                    )
                source = source_tensors[key]
                if str(source.dtype) not in _SUPPORTED_SOURCE_DTYPES:
                    raise _fail(
                        f"unverified or quantized source dtype for tensor {key}: {source.dtype}"
                    )
                loaded[key] = source.astype(runtime_dtype)
            # MLX loads lazily. Evaluate only the selected assignment-owned mapping while
            # this verified descriptor remains open; unassigned shard overfetch is discarded.
            mx.eval({key: loaded[key] for key in selected})
        finally:
            handle.close()
    missing = sorted(expected_set - set(loaded))
    if missing:
        preview = ", ".join(missing[:5])
        raise _fail(f"missing assigned tensors: {preview}")
    extra = sorted(set(loaded) - expected_set)
    if extra:
        raise _fail(f"extra loaded tensors: {', '.join(extra[:5])}")
    if set(loaded) != expected_set:
        raise _fail("loaded tensor set does not exactly match assignment")
    return {key: loaded[key] for key in sorted(loaded)}


def _shape(array: mx.array) -> tuple[int, ...]:
    return tuple(int(value) for value in array.shape)


def _expect_shape(
    tensors: Mapping[str, mx.array], key: str, expected: tuple[int, ...]
) -> None:
    actual = _shape(tensors[key])
    if actual != expected:
        raise _fail(
            f"gpt2 tensor shape mismatch for {key}: expected {expected}, got {actual}"
        )


def _validate_gpt2_shapes(
    tensors: Mapping[str, mx.array],
    runtime: dict[str, Any],
    start: int,
    end: int,
    namespace: str,
    components: list[str],
    aliases: Mapping[str, dict[str, Any]],
) -> None:
    config = runtime["model_config"]
    hidden = config["n_embd"]
    intermediate = config["n_inner"]
    vocabulary = config["vocab_size"]
    positions = config["n_positions"]
    if "input_embedding" in components:
        _expect_shape(tensors, f"{namespace}wte.weight", (vocabulary, hidden))
        _expect_shape(tensors, f"{namespace}wpe.weight", (positions, hidden))
    for layer in range(start, end):
        prefix = f"{namespace}h.{layer}."
        _expect_shape(tensors, prefix + "ln_1.weight", (hidden,))
        _expect_shape(tensors, prefix + "ln_1.bias", (hidden,))
        _expect_shape(tensors, prefix + "attn.c_attn.weight", (hidden, 3 * hidden))
        _expect_shape(tensors, prefix + "attn.c_attn.bias", (3 * hidden,))
        _expect_shape(tensors, prefix + "attn.c_proj.weight", (hidden, hidden))
        _expect_shape(tensors, prefix + "attn.c_proj.bias", (hidden,))
        _expect_shape(tensors, prefix + "ln_2.weight", (hidden,))
        _expect_shape(tensors, prefix + "ln_2.bias", (hidden,))
        _expect_shape(tensors, prefix + "mlp.c_fc.weight", (hidden, intermediate))
        _expect_shape(tensors, prefix + "mlp.c_fc.bias", (intermediate,))
        _expect_shape(tensors, prefix + "mlp.c_proj.weight", (intermediate, hidden))
        _expect_shape(tensors, prefix + "mlp.c_proj.bias", (hidden,))
    if "final_norm" in components:
        _expect_shape(tensors, f"{namespace}ln_f.weight", (hidden,))
        _expect_shape(tensors, f"{namespace}ln_f.bias", (hidden,))
    if "lm_head" in components:
        head_key = aliases.get("lm_head", {}).get("tensor_keys", ["lm_head.weight"])[0]
        _expect_shape(tensors, head_key, (vocabulary, hidden))


def _layer_norm(
    hidden: mx.array, weight: mx.array, bias: mx.array, epsilon: float
) -> mx.array:
    compute = hidden.astype(mx.float32)
    mean = mx.mean(compute, axis=-1, keepdims=True)
    variance = mx.mean(mx.square(compute - mean), axis=-1, keepdims=True)
    normalized = (compute - mean) * mx.rsqrt(variance + epsilon)
    return (normalized * weight.astype(mx.float32) + bias.astype(mx.float32)).astype(
        hidden.dtype
    )


def _gelu_new(value: mx.array) -> mx.array:
    compute = value.astype(mx.float32)
    result = (
        0.5
        * compute
        * (
            1.0
            + mx.tanh(
                math.sqrt(2.0 / math.pi) * (compute + 0.044715 * mx.power(compute, 3))
            )
        )
    )
    return result.astype(value.dtype)


def _gpt2_block(
    hidden: mx.array,
    tensors: Mapping[str, mx.array],
    prefix: str,
    n_head: int,
    epsilon: float,
) -> mx.array:
    residual = hidden
    normalized = _layer_norm(
        hidden,
        tensors[prefix + "ln_1.weight"],
        tensors[prefix + "ln_1.bias"],
        epsilon,
    )
    qkv = (
        mx.matmul(normalized, tensors[prefix + "attn.c_attn.weight"])
        + tensors[prefix + "attn.c_attn.bias"]
    )
    query, key, value = mx.split(qkv, 3, axis=-1)
    batch, sequence, hidden_size = (int(value) for value in hidden.shape)
    head_size = hidden_size // n_head
    query = query.reshape(batch, sequence, n_head, head_size).transpose(0, 2, 1, 3)
    key = key.reshape(batch, sequence, n_head, head_size).transpose(0, 2, 1, 3)
    value = value.reshape(batch, sequence, n_head, head_size).transpose(0, 2, 1, 3)
    scores = mx.matmul(query, key.transpose(0, 1, 3, 2)) / math.sqrt(head_size)
    positions = mx.arange(sequence)
    causal = positions[:, None] >= positions[None, :]
    scores = mx.where(
        causal[None, None, :, :],
        scores,
        mx.array(-math.inf, dtype=scores.dtype),
    )
    probabilities = mx.softmax(scores, axis=-1)
    attended = mx.matmul(probabilities, value)
    attended = attended.transpose(0, 2, 1, 3).reshape(batch, sequence, hidden_size)
    attended = (
        mx.matmul(attended, tensors[prefix + "attn.c_proj.weight"])
        + tensors[prefix + "attn.c_proj.bias"]
    )
    hidden = residual + attended

    residual = hidden
    normalized = _layer_norm(
        hidden,
        tensors[prefix + "ln_2.weight"],
        tensors[prefix + "ln_2.bias"],
        epsilon,
    )
    feed_forward = (
        mx.matmul(normalized, tensors[prefix + "mlp.c_fc.weight"])
        + tensors[prefix + "mlp.c_fc.bias"]
    )
    feed_forward = _gelu_new(feed_forward)
    feed_forward = (
        mx.matmul(feed_forward, tensors[prefix + "mlp.c_proj.weight"])
        + tensors[prefix + "mlp.c_proj.bias"]
    )
    return residual + feed_forward


def _run_gpt2_probe(
    tensors: Mapping[str, mx.array],
    runtime: dict[str, Any],
    start: int,
    end: int,
    namespace: str,
    components: list[str],
    aliases: Mapping[str, dict[str, Any]],
) -> mx.array:
    hidden_size = runtime["model_config"]["n_embd"]
    if "input_embedding" in components:
        token_embedding = tensors[f"{namespace}wte.weight"]
        position_embedding = tensors[f"{namespace}wpe.weight"]
        token_ids = mx.array([[0, 1, 2]], dtype=mx.int32)
        position_ids = mx.array([[0, 1, 2]], dtype=mx.int32)
        hidden = token_embedding[token_ids] + position_embedding[position_ids]
    else:
        positions = mx.arange(1, 4, dtype=mx.float32).reshape(1, 3, 1)
        channels = mx.arange(1, hidden_size + 1, dtype=mx.float32).reshape(
            1, 1, hidden_size
        )
        hidden = (
            mx.sin(positions * channels)
            + positions * mx.square(channels) / max(hidden_size * hidden_size, 1)
        ).astype(_RUNTIME_DTYPES[runtime["dtype"]])

    config = runtime["model_config"]
    for layer in range(start, end):
        hidden = _gpt2_block(
            hidden,
            tensors,
            f"{namespace}h.{layer}.",
            config["n_head"],
            float(config["layer_norm_epsilon"]),
        )
    if "final_norm" in components:
        hidden = _layer_norm(
            hidden,
            tensors[f"{namespace}ln_f.weight"],
            tensors[f"{namespace}ln_f.bias"],
            float(config["layer_norm_epsilon"]),
        )
    if "lm_head" in components:
        head_key = aliases.get("lm_head", {}).get("tensor_keys", ["lm_head.weight"])[0]
        hidden = mx.matmul(hidden, tensors[head_key].transpose(1, 0))
    mx.eval(hidden)
    if not bool(mx.all(mx.isfinite(hidden)).item()):
        raise _fail("deterministic functional probe produced non-finite output")
    return hidden


def execute_loaded_stage(
    loaded_stage: LoadedStage,
    *,
    token_ids: mx.array | None = None,
    hidden_states: mx.array | None = None,
) -> mx.array:
    """Execute exactly the GPT-2 components bound by one ``LoadedStage``.

    Runtime identity, layer range, roles, and aliases come from the immutable
    load proof rather than an unbound caller argument. Entry stages accept
    rank-two integer token IDs; all other stages accept rank-three hidden
    states. There is intentionally no KV-cache interface, so callers must pass
    the complete sequence on every invocation.
    """

    def reject(code: str) -> NoReturn:
        raise RuntimeExecutionError(code)

    if not isinstance(loaded_stage, LoadedStage):
        reject("invalid_loaded_stage")
    proof = loaded_stage.proof
    if not isinstance(proof, Mapping):
        reject("invalid_loaded_stage_proof")
    try:
        runtime = validate_normalized_mlx_runtime(
            json.loads(canonical_json(proof.get("runtime")))
        )
    except (TypeError, ValueError) as exc:
        raise RuntimeExecutionError("invalid_loaded_stage_runtime") from exc
    config = runtime["model_config"]
    layer_range = proof.get("loaded_range")
    if not isinstance(layer_range, Mapping):
        reject("invalid_loaded_stage_range")
    start = layer_range.get("start_layer")
    end = layer_range.get("end_layer_exclusive")
    count = layer_range.get("layer_count")
    if (
        not isinstance(start, int)
        or isinstance(start, bool)
        or start < 0
        or not isinstance(end, int)
        or isinstance(end, bool)
        or end <= start
        or not isinstance(count, int)
        or isinstance(count, bool)
        or count != end - start
        or end > config["n_layer"]
    ):
        reject("invalid_loaded_stage_range")
    raw_components = proof.get("loaded_components")
    if not isinstance(raw_components, (list, tuple)):
        reject("invalid_loaded_stage_components")
    components = list(raw_components)
    if (
        not components
        or len(components) != len(set(components))
        or "decoder" not in components
        or set(components)
        - {"input_embedding", "decoder", "final_norm", "lm_head"}
    ):
        reject("invalid_loaded_stage_components")

    tensors = loaded_stage.tensors
    if not isinstance(tensors, Mapping):
        reject("invalid_loaded_stage_tensors")
    transformer_key = f"transformer.h.{start}.ln_1.weight"
    plain_key = f"h.{start}.ln_1.weight"
    if transformer_key in tensors and plain_key not in tensors:
        namespace = "transformer."
    elif plain_key in tensors and transformer_key not in tensors:
        namespace = ""
    else:
        reject("invalid_loaded_stage_namespace")

    expected_dtype = _RUNTIME_DTYPES[runtime["dtype"]]
    expected_dtype_name = str(expected_dtype)
    has_embedding = "input_embedding" in components
    if has_embedding:
        if token_ids is None or hidden_states is not None:
            reject("entry_stage_requires_token_ids")
        if len(token_ids.shape) != 2 or int(token_ids.shape[1]) <= 0:
            reject("invalid_token_id_shape")
        if str(token_ids.dtype) not in {
            "mlx.core.int8",
            "mlx.core.int16",
            "mlx.core.int32",
            "mlx.core.int64",
            "mlx.core.uint8",
            "mlx.core.uint16",
            "mlx.core.uint32",
            "mlx.core.uint64",
        }:
            reject("invalid_token_id_dtype")
        sequence = int(token_ids.shape[1])
        if sequence > config["n_positions"]:
            reject("position_bounds_exceeded")
        if (
            not bool(mx.all(token_ids >= 0).item())
            or not bool(mx.all(token_ids < config["vocab_size"]).item())
        ):
            reject("token_bounds_exceeded")
        positions = mx.arange(sequence, dtype=mx.int32)
        hidden = (
            tensors[f"{namespace}wte.weight"][token_ids]
            + tensors[f"{namespace}wpe.weight"][positions]
        )
    else:
        if hidden_states is None or token_ids is not None:
            reject("non_entry_stage_requires_hidden_states")
        if len(hidden_states.shape) != 3:
            reject("invalid_hidden_state_rank")
        if (
            int(hidden_states.shape[0]) <= 0
            or int(hidden_states.shape[1]) <= 0
            or int(hidden_states.shape[2]) != config["n_embd"]
        ):
            reject("invalid_hidden_state_shape")
        if int(hidden_states.shape[1]) > config["n_positions"]:
            reject("position_bounds_exceeded")
        if str(hidden_states.dtype) != expected_dtype_name:
            reject("hidden_state_dtype_mismatch")
        hidden = hidden_states

    if str(hidden.dtype) != expected_dtype_name:
        reject("hidden_state_dtype_mismatch")
    if not bool(mx.all(mx.isfinite(hidden)).item()):
        reject("nonfinite_hidden_states")
    epsilon = float(config["layer_norm_epsilon"])
    for layer in range(start, end):
        hidden = _gpt2_block(
            hidden,
            tensors,
            f"{namespace}h.{layer}.",
            config["n_head"],
            epsilon,
        )
    if "final_norm" in components:
        hidden = _layer_norm(
            hidden,
            tensors[f"{namespace}ln_f.weight"],
            tensors[f"{namespace}ln_f.bias"],
            epsilon,
        )
    if "lm_head" in components:
        aliases = loaded_stage.resolved_aliases
        if not isinstance(aliases, Mapping):
            reject("invalid_loaded_stage_aliases")
        alias = aliases.get("lm_head", {})
        if not isinstance(alias, Mapping):
            reject("invalid_loaded_stage_aliases")
        head_keys = alias.get("tensor_keys", ["lm_head.weight"])
        if (
            not isinstance(head_keys, (list, tuple))
            or len(head_keys) != 1
            or not isinstance(head_keys[0], str)
        ):
            reject("invalid_loaded_stage_aliases")
        hidden = mx.matmul(hidden, tensors[head_keys[0]].transpose(1, 0))
    mx.eval(hidden)
    if not bool(mx.all(mx.isfinite(hidden)).item()):
        reject("nonfinite_stage_output")
    return hidden


def _digest_arrays(tensors: Mapping[str, mx.array]) -> str:
    digest = hashlib.sha256()
    for key in sorted(tensors):
        array = tensors[key]
        metadata = canonical_json(
            {
                "dtype": str(array.dtype),
                "name": key,
                "shape": list(_shape(array)),
            }
        ).encode("utf-8")
        payload = bytes(array)
        digest.update(len(metadata).to_bytes(8, "big"))
        digest.update(metadata)
        digest.update(len(payload).to_bytes(8, "big"))
        digest.update(payload)
    return "sha256:" + digest.hexdigest()


def _digest_array(array: mx.array) -> str:
    metadata = canonical_json(
        {
            "dtype": str(array.dtype),
            "shape": list(_shape(array)),
        }
    ).encode("utf-8")
    digest = hashlib.sha256()
    digest.update(len(metadata).to_bytes(8, "big"))
    digest.update(metadata)
    digest.update(bytes(array))
    return "sha256:" + digest.hexdigest()


def _actual_runtime_identity(runtime: dict[str, Any]) -> dict[str, Any]:
    try:
        version = importlib.metadata.version("mlx")
    except importlib.metadata.PackageNotFoundError:
        version = "unknown"
    return {
        "backend": "mlx",
        "backend_version": version,
        "device": str(mx.default_device()),
        "dtype": runtime["dtype"],
        "quantization": runtime["quantization"],
        "architecture": runtime["architecture"],
    }


def load_assignment_stage(
    assignment: dict[str, Any],
    artifact_report: dict[str, Any],
    *,
    load_generation: int,
) -> LoadedStage:
    """Load and probe one exact assignment from already verified local artifacts.

    This path has no network fallback. It emits local stage-load evidence only and
    deliberately keeps ``route_ready`` false pending a separate route challenge.
    """
    try:
        (
            runtime,
            runtime_dtype,
            expected_keys,
            aliases,
            start,
            end,
            namespace,
            binding,
        ) = _validate_assignment(assignment, load_generation)
        verified_by_path = _validate_artifact_report(assignment, artifact_report)
        tensors = _load_exact_tensors(
            assignment,
            artifact_report,
            verified_by_path,
            expected_keys,
            runtime_dtype,
        )
        components = list(assignment["components"])
        _validate_gpt2_shapes(
            tensors, runtime, start, end, namespace, components, aliases
        )
        for key, tensor in tensors.items():
            if str(tensor.dtype) != str(runtime_dtype):
                raise _fail(f"runtime dtype mismatch for tensor {key}")
            if not bool(mx.all(mx.isfinite(tensor)).item()):
                raise _fail(f"loaded tensor contains non-finite values: {key}")
        probe_output = _run_gpt2_probe(
            tensors,
            runtime,
            start,
            end,
            namespace,
            components,
            aliases,
        )
        proof = {
            "protocol": LAYER_LOAD_PROOF_PROTOCOL,
            "deployment_id": assignment["deployment_id"],
            "deployment_epoch": assignment["deployment_epoch"],
            "assignment_id": assignment["assignment_id"],
            "node_id": assignment["node_id"],
            "model_id": assignment["model_id"],
            "manifest_digest": assignment["manifest_digest"],
            "resolved_commit": assignment["resolved_commit"],
            "loaded_range": copy.deepcopy(assignment["range"]),
            "loaded_components": components,
            "loaded_tensor_keys": sorted(tensors),
            "loaded_tensor_digest": _digest_arrays(tensors),
            "resolved_component_aliases": copy.deepcopy(aliases),
            "runtime": runtime,
            "runtime_identity": _actual_runtime_identity(runtime),
            "probe_shape": list(_shape(probe_output)),
            "probe_digest": _digest_array(probe_output),
            "load_generation": load_generation,
            "control_plane_binding": binding,
            "route_ready": False,
            "claim_boundary": (
                "assignment-bound local MLX stage loaded and deterministically probed; "
                "no route challenge or distributed inference claim"
            ),
        }
        frozen_proof = _deep_freeze(proof)
        frozen_aliases = _deep_freeze(aliases)
        # Force canonical serialization of the immutable evidence before returning it.
        canonical_json(frozen_proof)
        return LoadedStage(
            tensors=MappingProxyType(tensors),
            resolved_aliases=frozen_aliases,
            probe_output=probe_output,
            proof=frozen_proof,
        )
    except RuntimeLoadError:
        raise
    except Exception as exc:
        raise _fail(f"runtime load rejected: {exc}") from exc
