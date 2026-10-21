#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Assignment-bound, local-only stage loading and load-proof emission.

The MLX and NumPy backends are lazily resolved so that this module imports
even when ``mlx`` cannot be imported. The NumPy fallback is a fully
self-contained, assignment-local executor; the MLX executor preserves the
existing protocol. Backend selection, assignment-id integrity, and
role-based dispatch are all fail-closed.
"""

from __future__ import annotations

import copy
import importlib
import importlib.metadata
import importlib.util
import hashlib
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

import numpy as np

from layer_assignment import validate_assignment_identity
from model_adapters import adapter_for_runtime
from numpy_runtime import (
    NumpyRuntimeError,
    NumpyStageBackend,
    execute_loaded_stage as _execute_loaded_numpy_stage,
    tensor_digest as _numpy_tensor_digest,
    quantize_qwen2_numpy_tensor,
)
from runtime_contracts import (
    SUPPORTED_NUMPY_DTYPES,
    validate_assignment_stage_boundaries,
    validate_loaded_stage_authentication,
    validate_normalized_mlx_runtime,
    validate_normalized_numpy_runtime,
)
from weight_provisioning import artifact_report_errors
from weight_quantization import (
    Int8RowwiseWeight,
    PackedInt8RowwiseWeight,
    ROWWISE_INT8_CONVERSION_CHUNK_FLOAT_BYTES,
    int8_float32_values,
    int8_rows,
    packed_int8_packable,
    packed_int8_weight,
    rowwise_int8_kind,
)


LAYER_LOAD_PROOF_PROTOCOL = "mycelium.layer_load_proof.v1"
_CONTROL_PLANE_BINDING_PROTOCOL = "mycelium.control_plane_binding.v1"
_NUMPY_RUNTIME_BACKEND = "numpy"
_MLX_RUNTIME_BACKEND = "mlx"
_VALID_STAGE_PREFERS = frozenset({"auto", "mlx", "numpy"})
_SHA256_REF_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
_SAFE_ARTIFACT_RE = re.compile(r"^[A-Za-z0-9._/-]+$")
_MAX_SAFETENSORS_HEADER_BYTES = 100 * 1024 * 1024

# ---------------------------------------------------------------------------
# int8 execution kernel selection for the runtime path
#
# The runtime's ``int8-weight-only`` quantization is implemented by exactly one
# kernel per load.  ``weight-quantization`` is the qualified row-wise
# representation path (``weight_quantization.Int8RowwiseWeight``, dequantized
# inside ``numpy_runtime``) and remains the default, so the qualified path's
# numerics and load-proof bytes are unchanged.  ``platform-neutral-int8``
# additionally dispatches the A9 platform-neutral persistent int8 kernel
# (``mycelium_platform_neutral.int8_execution``) over the loaded weights and
# drives the reduced-precision activation frame codec
# (``mycelium_platform_neutral.activation_transport``) on the probe activation.
#
# The A9 selection makes that source tranche *reachable* from a real runtime
# load.  It claims nothing more: no physical round has been executed, nothing
# is qualified, eligible, placed or ready, and the accelerated Windows x86_64
# device blocker stays open (``int8_execution.ACCEPTANCE_COVERAGE`` travels
# with the emitted report so the boundary is machine-readable).
# ---------------------------------------------------------------------------
QUALIFIED_INT8_EXECUTION = "weight-quantization"
PLATFORM_NEUTRAL_INT8_EXECUTION = "platform-neutral-int8"
SUPPORTED_INT8_EXECUTIONS = frozenset(
    {QUALIFIED_INT8_EXECUTION, PLATFORM_NEUTRAL_INT8_EXECUTION}
)
DEFAULT_INT8_EXECUTION = QUALIFIED_INT8_EXECUTION
#: Reduced-precision activation frame dtypes the A9 codec can transport.
SUPPORTED_ACTIVATION_FRAME_DTYPES = frozenset({"float16", "bfloat16"})
DEFAULT_ACTIVATION_FRAME_DTYPE = "float16"
#: Load-proof key carrying the A9 execution report.  Emitted only when the
#: platform-neutral selection is active, so every other load proof keeps its
#: exact field set.
A9_INT8_EXECUTION_PROOF_KEY = "a9_int8_execution"
A9_INT8_RUNTIME_EXECUTION_PROTOCOL = "mycelium.a9_int8_runtime_execution.v1"
#: Sequence positions of the deterministic probe activation the A9 kernel decodes.
A9_PROBE_ACTIVATION_ROWS = 3
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


class RuntimeLoadError(ValueError):
    """Permanent, fail-closed assignment loading failure."""


def _mlx_find_spec() -> Any | None:
    """Return the import spec for ``mlx`` or ``None`` if unavailable."""

    try:
        return importlib.util.find_spec("mlx")
    except (ImportError, ModuleNotFoundError, ValueError):
        return None


def _mlx_module() -> Any:
    """Return the ``mlx.core`` module, raising ``RuntimeLoadError`` when missing."""

    spec = _mlx_find_spec()
    if spec is None:
        raise RuntimeLoadError("backend_unavailable: mlx is not importable")
    try:
        return importlib.import_module("mlx.core")
    except (ImportError, ModuleNotFoundError) as exc:
        raise RuntimeLoadError(
            "backend_unavailable: mlx is not importable"
        ) from exc


def _runtime_dtypes() -> dict[str, Any]:
    """Map canonical runtime dtype strings to MLX dtype objects (lazy)."""

    mx = _mlx_module()
    return {
        "bfloat16": mx.bfloat16,
        "float16": mx.float16,
        "float32": mx.float32,
    }


def _numpy_runtime_dtypes() -> frozenset[str]:
    """Return the canonical NumPy runtime dtype set (backend-neutral)."""

    return SUPPORTED_NUMPY_DTYPES


class RuntimeExecutionError(ValueError):
    """Fail-closed execution error for an already loaded local stage."""


@dataclass(frozen=True)
class LoadedStage:
    """Materialized assignment tensors plus deterministic proof evidence."""

    tensors: Mapping[str, Any]
    resolved_aliases: Mapping[str, Any]
    probe_output: Any
    proof: Mapping[str, Any]
    authenticated_assignment_id: str | None = None
    authenticated_tensor_digest: str | None = None
    authenticated_resolved_aliases: Mapping[str, Any] | None = None
    authenticated_load_generation: int | None = None
    authenticated_loaded_components: tuple[str, ...] | None = None
    authenticated_loaded_range: Mapping[str, Any] | None = None
    authenticated_runtime: Mapping[str, Any] | None = None
    authenticated_runtime_identity: Mapping[str, Any] | None = None


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
    if not isinstance(runtime, Mapping):
        raise _fail("runtime identity must be an object")
    backend = runtime.get("backend")
    try:
        if backend == _MLX_RUNTIME_BACKEND:
            normalized = validate_normalized_mlx_runtime(runtime)
            return normalized, _runtime_dtypes()[normalized["dtype"]]
        if backend == _NUMPY_RUNTIME_BACKEND:
            normalized = validate_normalized_numpy_runtime(runtime)
            return normalized, np.dtype(normalized["dtype"])
    except (TypeError, ValueError) as exc:
        raise _fail(str(exc)) from exc
    raise _fail(f"unsupported runtime backend: {backend!r}")


def _validate_range_and_prefixes(
    assignment: dict[str, Any],
    architecture: str,
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
    try:
        adapter = adapter_for_runtime(architecture)
    except ValueError as exc:
        raise _fail("runtime architecture adapter unavailable") from exc
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
            f"{architecture} expected tensor prefixes do not match the assigned layer range"
        )
    if architecture == "gpt2":
        namespace = "transformer." if selected.startswith("transformer.") else ""
    elif architecture in {"qwen3_5", "qwen3_5_moe"}:
        namespace = "model.language_model."
    else:
        namespace = "model."
    return start, end, list(prefixes), namespace


def _resolve_aliases(
    components: list[str],
    component_keys: dict[str, list[str]],
    aliases: Any,
    namespace: str,
    architecture: str,
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
        expected_source = (
            [f"{namespace}wte.weight"]
            if architecture == "gpt2"
            else [f"{namespace}embed_tokens.weight"]
            if architecture in {"qwen3_5", "qwen3_5_moe"}
            else ["model.embed_tokens.weight"]
        )
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
    architecture: str,
    layer_types: list[str] | None = None,
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
        raise _fail(f"unsupported {architecture} components: {', '.join(unsupported)}")
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
        architecture,
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
    try:
        adapter = adapter_for_runtime(architecture)
    except ValueError as exc:
        raise _fail("runtime architecture adapter unavailable") from exc
    if adapter.layer_type_tensor_suffixes:
        # Heterogeneous hybrid decoder: per-layer ownership from layer_types.
        start = assignment["range"]["start_layer"]
        if not isinstance(layer_types, list) or len(layer_types) < start + len(prefixes):
            raise _fail(f"{architecture} layer_types do not cover the assigned range")
        try:
            expected_decoder_keys = sorted(
                prefix + suffix
                for offset, prefix in enumerate(prefixes)
                for suffix in adapter.decoder_suffixes_for_layer(layer_types[start + offset])
            )
        except ValueError as exc:
            raise _fail(str(exc)) from exc
    else:
        suffixes = adapter.decoder_tensor_suffixes
        expected_decoder_keys = sorted(
            prefix + suffix for prefix in prefixes for suffix in suffixes
        )
    if decoder_keys != expected_decoder_keys:
        raise _fail(f"{architecture} decoder tensor ownership is missing, extra, or mismatched")
    expected_static: dict[str, list[str]] = (
        {
            "input_embedding": sorted((f"{namespace}wpe.weight", f"{namespace}wte.weight")),
            "final_norm": sorted((f"{namespace}ln_f.bias", f"{namespace}ln_f.weight")),
        }
        if architecture == "gpt2"
        else {
            "input_embedding": [f"{namespace}embed_tokens.weight"],
            "final_norm": [f"{namespace}norm.weight"],
        }
    )
    for component, keys in expected_static.items():
        if component in component_keys and component_keys[component] != keys:
            raise _fail(
                f"{architecture} {component} tensor ownership is missing, extra, or mismatched"
            )
    if "lm_head" in component_keys and "lm_head" not in aliases:
        if component_keys["lm_head"] != ["lm_head.weight"]:
            raise _fail(
                f"{architecture} lm_head tensor ownership is missing, extra, or mismatched"
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
    try:
        validate_assignment_stage_boundaries(
            components,
            start_layer=start,
            end_layer_exclusive=end,
            total_layers=total_layers,
        )
    except ValueError as exc:
        raise _fail(str(exc)) from exc


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
    start, end, prefixes, namespace = _validate_range_and_prefixes(
        assignment, runtime["architecture"]
    )
    expected_keys, aliases = _validate_component_ownership(
        assignment,
        prefixes,
        namespace,
        runtime["architecture"],
        runtime["model_config"].get("layer_types"),
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


def _validated_stage_pack_binding(
    assignment: dict[str, Any], report: dict[str, Any]
) -> tuple[str, str] | None:
    """Authenticate embedded stage-pack evidence before load-proof propagation."""

    fields = {
        "stage_pack",
        "stage_pack_manifest",
        "stage_pack_verification",
        "stage_pack_digest",
        "stage_pack_verification_digest",
    }
    present = fields & set(report)
    if not present:
        return None
    if present != fields:
        raise _fail("stage-pack evidence must be complete")
    pack = report.get("stage_pack")
    manifest = report.get("stage_pack_manifest")
    verification = report.get("stage_pack_verification")
    if not all(
        isinstance(document, dict)
        for document in (pack, manifest, verification)
    ):
        raise _fail("stage-pack evidence documents must be objects")
    try:
        from stage_pack import validate_stage_pack_evidence

        binding = validate_stage_pack_evidence(
            pack,
            verification,
            assignment=assignment,
            manifest=manifest,
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise _fail(f"stage-pack evidence rejected: {exc}") from exc
    if binding != (
        report.get("stage_pack_digest"),
        report.get("stage_pack_verification_digest"),
    ):
        raise _fail("stage-pack evidence digest mismatch")
    return binding


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


def _artifact_fingerprint(metadata: os.stat_result) -> tuple[int, ...]:
    return (
        metadata.st_mode,
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
        metadata.st_nlink,
    )


def _win32_open_verified_artifact(
    cache_root: Path,
    relative: PurePosixPath,
    assigned_record: dict[str, Any],
) -> tuple[BinaryIO, tuple[int, ...]]:
    """Handle-bound, reparse-refusing open of one verified artifact on win32."""

    import mycelium_host_platform

    try:
        handle = mycelium_host_platform.open_beneath(cache_root, tuple(relative.parts))
    except mycelium_host_platform.HostPlatformError as exc:
        raise _fail(f"unable to open verified artifact: {relative}") from exc
    try:
        metadata = os.fstat(handle.fileno())
        fingerprint = _artifact_fingerprint(metadata)
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
        if _artifact_fingerprint(os.fstat(handle.fileno())) != fingerprint:
            raise _fail(f"verified artifact changed during hashing: {relative}")
        actual_digest = "sha256:" + digest.hexdigest()
        if actual_digest != assigned_record.get("content_digest"):
            raise _fail(f"verified artifact digest mismatch: {relative}")
        handle.seek(0)
        return handle, fingerprint
    except RuntimeLoadError:
        handle.close()
        raise
    except OSError as exc:
        handle.close()
        raise _fail(f"unable to open verified artifact: {relative}") from exc


def _open_verified_artifact(
    assignment: dict[str, Any],
    report: dict[str, Any],
    assigned_record: dict[str, Any],
    verified_record: dict[str, Any],
) -> tuple[BinaryIO, tuple[int, ...]]:
    relative = _safe_relative_artifact_path(assigned_record.get("path"))
    cache_root_raw = assignment.get("artifact_cache_root")
    if not isinstance(cache_root_raw, str) or not Path(cache_root_raw).is_absolute():
        raise _fail("assignment artifact_cache_root must be absolute")
    cache_root_path = Path(cache_root_raw)
    try:
        cache_root = cache_root_path.resolve(strict=True)
    except OSError as exc:
        raise _fail("assignment artifact_cache_root is unavailable") from exc
    if report.get("resolved_artifact_cache_root") != str(cache_root):
        raise _fail("artifact report resolved cache root mismatch")
    reported_path = Path(verified_record["local_path"])
    expected_paths = {
        str(cache_root_path.joinpath(*relative.parts)),
        str(cache_root.joinpath(*relative.parts)),
    }
    if str(reported_path) not in expected_paths:
        raise _fail(
            f"verified local path escapes artifact cache root or mismatches assignment: {relative}"
        )

    import mycelium_host_platform

    if mycelium_host_platform.is_win32():
        return _win32_open_verified_artifact(cache_root, relative, assigned_record)
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    cloexec = getattr(os, "O_CLOEXEC", 0)
    directory = getattr(os, "O_DIRECTORY", 0)
    root_fd: int | None = None
    current_fd: int | None = None
    file_fd: int | None = None
    handle: BinaryIO | None = None
    try:
        root_before = os.stat(cache_root, follow_symlinks=False)
        root_fd = os.open(cache_root, os.O_RDONLY | directory | nofollow | cloexec)
        current_fd = root_fd
        opened_root = os.fstat(root_fd)
        if (
            not stat.S_ISDIR(opened_root.st_mode)
            or _artifact_fingerprint(root_before)
            != _artifact_fingerprint(opened_root)
        ):
            raise _fail("artifact cache root changed before open")
        for part in relative.parts[:-1]:
            child_fd = os.open(
                part,
                os.O_RDONLY | directory | nofollow | cloexec,
                dir_fd=current_fd,
            )
            if current_fd != root_fd:
                os.close(current_fd)
            current_fd = child_fd
        file_fd = os.open(
            relative.parts[-1],
            os.O_RDONLY | nofollow | cloexec,
            dir_fd=current_fd,
        )
        root_after = os.stat(cache_root, follow_symlinks=False)
        if _artifact_fingerprint(root_after) != _artifact_fingerprint(opened_root):
            raise _fail("artifact cache root changed during open")
        handle = os.fdopen(file_fd, "rb")
        file_fd = None
        metadata = os.fstat(handle.fileno())
        fingerprint = _artifact_fingerprint(metadata)
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
        if _artifact_fingerprint(os.fstat(handle.fileno())) != fingerprint:
            raise _fail(f"verified artifact changed during hashing: {relative}")
        actual_digest = "sha256:" + digest.hexdigest()
        if actual_digest != assigned_record.get("content_digest"):
            raise _fail(f"verified artifact digest mismatch: {relative}")
        handle.seek(0)
        return handle, fingerprint
    except RuntimeLoadError:
        if handle is not None:
            handle.close()
        raise
    except OSError as exc:
        if handle is not None:
            handle.close()
        raise _fail(f"unable to open verified artifact: {relative}") from exc
    finally:
        if file_fd is not None:
            os.close(file_fd)
        if current_fd is not None and current_fd != root_fd:
            os.close(current_fd)
        if root_fd is not None:
            os.close(root_fd)


def _validate_safetensors_header(
    handle: BinaryIO, path: str
) -> tuple[dict[str, dict[str, Any]], int]:
    """Validate canonical, non-aliased Safetensors storage before loading it."""
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
        return header, 8 + header_length
    except RuntimeLoadError:
        raise
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, struct.error) as exc:
        raise _fail(f"invalid Safetensors header: {path}") from exc
    finally:
        handle.seek(0)


def _decode_float_payload(payload: bytes, source_dtype: str) -> np.ndarray:
    if source_dtype == "BF16":
        words = np.frombuffer(payload, dtype="<u2")
        return (words.astype(np.uint32) << 16).view(np.float32)
    dtype = "<f2" if source_dtype == "F16" else "<f4"
    return np.frombuffer(payload, dtype=dtype)


def _load_rowwise_int8_weight(
    handle: BinaryIO,
    *,
    data_offset: int,
    start: int,
    end: int,
    source_dtype: str,
    shape: tuple[int, ...],
    key: str,
) -> Int8RowwiseWeight:
    values, scales = _stream_rowwise_int8_codes(
        handle,
        data_offset=data_offset,
        start=start,
        end=end,
        source_dtype=source_dtype,
        shape=shape,
        key=key,
        offset_binary=False,
    )
    values.flags.writeable = False
    scales.flags.writeable = False
    return Int8RowwiseWeight(values, scales)


def _stream_rowwise_int8_codes(
    handle: BinaryIO,
    *,
    data_offset: int,
    start: int,
    end: int,
    source_dtype: str,
    shape: tuple[int, ...],
    key: str,
    offset_binary: bool,
) -> tuple[np.ndarray, np.ndarray]:
    """Row-wise int8 codes and scales of one matrix, streamed in bounded chunks.

    ``offset_binary`` writes ``value + 128`` as ``uint8`` (the packed MLX
    layout's byte order) straight into the only full-size buffer, so the
    packed MLX loader never holds a signed copy next to the packed one.
    """

    if len(shape) != 2 or not key.endswith(".weight"):
        raise _fail(f"invalid row-wise int8 tensor shape: {key}")
    rows, columns = shape
    source_item_bytes = _SAFE_DTYPE_BYTES[source_dtype]
    row_source_bytes = columns * source_item_bytes
    if rows * row_source_bytes != end - start:
        raise _fail(f"Safetensors byte length does not match tensor {key}")
    rows_per_chunk = max(
        1,
        ROWWISE_INT8_CONVERSION_CHUNK_FLOAT_BYTES // max(columns * 4, 1),
    )
    values = np.empty(shape, dtype=np.uint8 if offset_binary else np.int8)
    scales = np.empty((rows,), dtype=np.float32)
    for row_start in range(0, rows, rows_per_chunk):
        row_end = min(rows, row_start + rows_per_chunk)
        byte_count = (row_end - row_start) * row_source_bytes
        handle.seek(data_offset + start + row_start * row_source_bytes)
        payload = handle.read(byte_count)
        if len(payload) != byte_count:
            raise _fail(f"truncated Safetensors data for tensor {key}")
        source = _decode_float_payload(payload, source_dtype)
        chunk = np.array(
            source.reshape((row_end - row_start, columns)),
            dtype=np.float32,
            order="C",
            copy=True,
        )
        converted = quantize_qwen2_numpy_tensor(key, chunk)
        if not isinstance(converted, Int8RowwiseWeight):
            raise _fail(f"row-wise int8 conversion failed for tensor {key}")
        if offset_binary:
            np.bitwise_xor(
                converted.values.view(np.uint8),
                np.uint8(0x80),
                out=values[row_start:row_end],
            )
        else:
            values[row_start:row_end] = converted.values
        scales[row_start:row_end] = converted.scales
    return values, scales


def _load_packed_int8_mlx_weight(
    handle: BinaryIO,
    *,
    data_offset: int,
    start: int,
    end: int,
    source_dtype: str,
    shape: tuple[int, ...],
    key: str,
    kind: str,
    mx: Any,
) -> Int8RowwiseWeight:
    """One row-wise int8 tensor, packed ONCE at load into its MLX execution layout.

    The packed ``uint32`` codes (plus row scales and group parameters) are the
    only resident copy: the NumPy staging buffer is released before the next
    tensor is read, so the load peak stays one tensor above the packed total.
    A tensor whose input width is not a multiple of the packing group keeps
    plain ``int8`` codes (and the port then runs the stage per call).
    """

    if kind == "experts":
        if len(shape) != 3 or rowwise_int8_kind(key, 3) != "experts":
            raise _fail(f"invalid packed expert int8 tensor shape: {key}")
        matrix_shape = (shape[0] * shape[1], shape[2])
        matrix_key = key + ".rows.weight"
    else:
        matrix_shape = shape
        matrix_key = key
    packable = packed_int8_packable(shape)
    codes, scales = _stream_rowwise_int8_codes(
        handle,
        data_offset=data_offset,
        start=start,
        end=end,
        source_dtype=source_dtype,
        shape=matrix_shape,
        key=matrix_key,
        offset_binary=packable,
    )
    row_scales = mx.array(scales.reshape(shape[:-1]))
    if not packable:
        weight = Int8RowwiseWeight(mx.array(codes.reshape(shape)), row_scales)
        mx.eval(weight.values, weight.scales)
        return weight
    packed = mx.array(codes.reshape(shape).view(np.uint32))
    mx.eval(packed)
    del codes
    return packed_int8_weight(packed, row_scales)


def _load_expert_rowwise_int8_weight(
    handle: BinaryIO,
    *,
    data_offset: int,
    start: int,
    end: int,
    source_dtype: str,
    shape: tuple[int, ...],
    key: str,
) -> Int8RowwiseWeight:
    """Quantize a packed ``[E, out, in]`` expert tensor per expert and output row.

    The bytes of a C-ordered ``[E, out, in]`` tensor are exactly those of an
    ``[E * out, in]`` matrix, so this streams it through the incumbent
    row-wise converter (bounded chunks, one float32 scale per row) and
    reshapes: codes ``[E, out, in]`` int8, scales ``[E, out]`` float32 --
    identical to quantizing every expert's matrix on its own.
    """

    if len(shape) != 3 or rowwise_int8_kind(key, 3) != "experts":
        raise _fail(f"invalid packed expert int8 tensor shape: {key}")
    experts, rows, columns = shape
    flat = _load_rowwise_int8_weight(
        handle,
        data_offset=data_offset,
        start=start,
        end=end,
        source_dtype=source_dtype,
        shape=(experts * rows, columns),
        key=key + ".rows.weight",
    )
    values = flat.values.reshape(shape)
    scales = flat.scales.reshape(experts, rows)
    values.flags.writeable = False
    scales.flags.writeable = False
    return Int8RowwiseWeight(values, scales)


def _load_numpy_safetensors(
    handle: BinaryIO,
    header: Mapping[str, Mapping[str, Any]],
    data_offset: int,
    selected: set[str],
    runtime_dtype: np.dtype[Any],
    *,
    quantize_qwen: bool = False,
) -> dict[str, Any]:
    """Materialize only selected floating tensors without importing MLX."""

    loaded: dict[str, Any] = {}
    for key in sorted(selected):
        entry = header[key]
        source_dtype = entry["dtype"]
        if source_dtype not in {"BF16", "F16", "F32"}:
            raise _fail(
                f"unverified or quantized source dtype for tensor {key}: "
                f"{source_dtype}"
            )
        start, end = entry["data_offsets"]
        shape = tuple(int(dimension) for dimension in entry["shape"])
        int8_kind = rowwise_int8_kind(key, len(shape)) if quantize_qwen else None
        if int8_kind == "experts":
            loaded[key] = _load_expert_rowwise_int8_weight(
                handle,
                data_offset=data_offset,
                start=start,
                end=end,
                source_dtype=source_dtype,
                shape=shape,
                key=key,
            )
            continue
        if int8_kind == "matrix":
            loaded[key] = _load_rowwise_int8_weight(
                handle,
                data_offset=data_offset,
                start=start,
                end=end,
                source_dtype=source_dtype,
                shape=shape,
                key=key,
            )
            continue
        handle.seek(data_offset + start)
        payload = handle.read(end - start)
        if len(payload) != end - start:
            raise _fail(f"truncated Safetensors data for tensor {key}")
        source = _decode_float_payload(payload, source_dtype)
        value = np.array(
            source.reshape(shape),
            dtype=runtime_dtype,
            order="C",
            copy=True,
        )
        value.flags.writeable = False
        loaded[key] = value
    return loaded


def _load_exact_tensors(
    assignment: dict[str, Any],
    report: dict[str, Any],
    verified_by_path: dict[str, dict[str, Any]],
    expected_keys: list[str],
    runtime_dtype: Any,
    runtime_backend: str,
    *,
    quantize_qwen: bool = False,
) -> dict[str, Any]:
    """Load exactly the assignment-owned tensors from verified safetensors shards."""

    mx = _mlx_module() if runtime_backend == _MLX_RUNTIME_BACKEND else None
    files = assignment.get("files")
    if not isinstance(files, list) or not files:
        raise _fail("assignment requires verified artifact files")
    expected_set = set(expected_keys)
    loaded: dict[str, Any] = {}
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
        handle, fingerprint = _open_verified_artifact(
            assignment, report, assigned_record, verified_record
        )
        try:
            inode = (fingerprint[1], fingerprint[2])
            if inode in seen_inodes:
                raise _fail(f"duplicate verified artifact inode: {path}")
            seen_inodes.add(inode)
            header, data_offset = _validate_safetensors_header(handle, path)
            selected = expected_set.intersection(header)
            if runtime_backend == _MLX_RUNTIME_BACKEND and quantize_qwen:
                # One tensor at a time: read, quantize, pack into the MLX
                # execution layout, release the staging copy, continue.
                selected_tensors = {}
                for key in sorted(selected):
                    entry = header[key]
                    shape = tuple(int(dimension) for dimension in entry["shape"])
                    int8_kind = rowwise_int8_kind(key, len(shape))
                    if int8_kind is not None:
                        source_dtype = entry["dtype"]
                        if source_dtype not in {"BF16", "F16", "F32"}:
                            raise _fail(
                                "unverified or quantized source dtype for tensor "
                                f"{key}: {source_dtype}"
                            )
                        tensor_start, tensor_end = entry["data_offsets"]
                        selected_tensors[key] = _load_packed_int8_mlx_weight(
                            handle,
                            data_offset=data_offset,
                            start=tensor_start,
                            end=tensor_end,
                            source_dtype=source_dtype,
                            shape=shape,
                            key=key,
                            kind=int8_kind,
                            mx=mx,
                        )
                        continue
                    value = _load_numpy_safetensors(
                        handle,
                        header,
                        data_offset,
                        {key},
                        np.dtype("float32"),
                    )[key]
                    converted = mx.array(value).astype(runtime_dtype)
                    mx.eval(converted)
                    del value
                    selected_tensors[key] = converted
            elif runtime_backend == _MLX_RUNTIME_BACKEND:
                try:
                    source_tensors = mx.load(handle, format="safetensors")
                except Exception as exc:
                    raise _fail(
                        f"MLX could not load verified Safetensors artifact: {path}"
                    ) from exc
                if not isinstance(source_tensors, dict):
                    raise _fail(f"MLX artifact did not contain named tensors: {path}")
                if set(source_tensors) != set(header):
                    raise _fail(
                        f"MLX tensor names do not match verified header: {path}"
                    )
                selected_tensors: dict[str, Any] = {}
                for key in sorted(selected):
                    source = source_tensors[key]
                    if str(source.dtype) not in _SUPPORTED_SOURCE_DTYPES:
                        raise _fail(
                            "unverified or quantized source dtype for tensor "
                            f"{key}: {source.dtype}"
                        )
                    converted = source.astype(runtime_dtype)
                    if isinstance(converted, Int8RowwiseWeight):
                        mx.eval(converted.values, converted.scales)
                    else:
                        mx.eval(converted)
                    selected_tensors[key] = converted
            else:
                selected_tensors = _load_numpy_safetensors(
                    handle,
                    header,
                    data_offset,
                    selected,
                    runtime_dtype,
                    quantize_qwen=quantize_qwen,
                )
            for key in selected:
                if key in loaded:
                    raise _fail(
                        f"duplicate assigned tensor across verified files: {key}"
                    )
                loaded[key] = selected_tensors[key]
            if _artifact_fingerprint(os.fstat(handle.fileno())) != fingerprint:
                raise _fail(f"verified artifact changed during load: {path}")
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


def _shape(array: Any) -> tuple[int, ...]:
    return tuple(int(value) for value in array.shape)


def _expect_shape(
    tensors: Mapping[str, Any], key: str, expected: tuple[int, ...]
) -> None:
    actual = _shape(tensors[key])
    if actual != expected:
        raise _fail(
            f"gpt2 tensor shape mismatch for {key}: expected {expected}, got {actual}"
        )


def _validate_gpt2_shapes(
    tensors: Mapping[str, Any],
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


def _validate_qwen2_shapes(
    tensors: Mapping[str, Any],
    runtime: dict[str, Any],
    start: int,
    end: int,
    components: list[str],
    aliases: Mapping[str, dict[str, Any]],
) -> None:
    config = runtime["model_config"]
    hidden = config["n_embd"]
    inner = config["n_inner"]
    kv_hidden = config["n_kv_head"] * config["head_dim"]
    # Qwen3 may project attention wider than the residual stream.
    q_hidden = config["n_head"] * config["head_dim"]
    if "input_embedding" in components:
        _expect_shape(tensors, "model.embed_tokens.weight", (config["vocab_size"], hidden))
    suffix_shapes: dict[str, tuple[int, ...]] = {
        "input_layernorm.weight": (hidden,),
        "self_attn.q_proj.weight": (q_hidden, hidden),
        "self_attn.k_proj.weight": (kv_hidden, hidden),
        "self_attn.v_proj.weight": (kv_hidden, hidden),
        "self_attn.o_proj.weight": (hidden, q_hidden),
        "post_attention_layernorm.weight": (hidden,),
        "mlp.gate_proj.weight": (inner, hidden),
        "mlp.up_proj.weight": (inner, hidden),
        "mlp.down_proj.weight": (hidden, inner),
    }
    if runtime["architecture"] == "qwen2":
        suffix_shapes.update(
            {
                "self_attn.q_proj.bias": (q_hidden,),
                "self_attn.k_proj.bias": (kv_hidden,),
                "self_attn.v_proj.bias": (kv_hidden,),
            }
        )
    else:
        suffix_shapes.update(
            {
                "self_attn.q_norm.weight": (config["head_dim"],),
                "self_attn.k_norm.weight": (config["head_dim"],),
            }
        )
    for layer in range(start, end):
        prefix = f"model.layers.{layer}."
        for suffix, shape in suffix_shapes.items():
            _expect_shape(tensors, prefix + suffix, shape)
    if "final_norm" in components:
        _expect_shape(tensors, "model.norm.weight", (hidden,))
    if "lm_head" in components:
        head_key = aliases.get("lm_head", {}).get("tensor_keys", ["lm_head.weight"])[0]
        _expect_shape(tensors, head_key, (config["vocab_size"], hidden))


def _validate_qwen35_shapes(
    tensors: Mapping[str, Any],
    runtime: dict[str, Any],
    start: int,
    end: int,
    components: list[str],
    aliases: Mapping[str, dict[str, Any]],
) -> None:
    from qwen35_shapes import expected_qwen35_shapes

    config = runtime["model_config"]
    for key, shape in expected_qwen35_shapes(config, start, end, components).items():
        _expect_shape(tensors, key, shape)
        value = tensors[key]
        if isinstance(value, Int8RowwiseWeight) and _shape(value.scales) != shape[:-1]:
            raise _fail(f"row-wise int8 scale shape mismatch for {key}")
    if "lm_head" in components:
        head_key = aliases.get("lm_head", {}).get("tensor_keys", ["lm_head.weight"])[0]
        _expect_shape(tensors, head_key, (config["vocab_size"], config["n_embd"]))


def _run_qwen35_probe(
    tensors: Mapping[str, Any],
    runtime: dict[str, Any],
    start: int,
    end: int,
    components: list[str],
    aliases: Mapping[str, dict[str, Any]],
) -> Any:
    import qwen35_mlx

    mx = _mlx_module()
    config = runtime["model_config"]
    if "input_embedding" in components:
        hidden = _qwen2_embedding(
            tensors[qwen35_mlx.EMBED_KEY], mx.array([[0, 1, 2]], dtype=mx.int32), mx
        )
    else:
        positions = mx.arange(1, 4, dtype=mx.float32).reshape(1, 3, 1)
        channels = mx.arange(1, config["n_embd"] + 1, dtype=mx.float32).reshape(
            1, 1, config["n_embd"]
        )
        hidden = (
            mx.sin(positions * channels)
            + positions * mx.square(channels) / (config["n_embd"] ** 2)
        ).astype(_runtime_dtypes()[runtime["dtype"]])

    def linear(value: Any, weight: Any) -> Any:
        return _qwen2_linear(value, weight, mx)

    for layer in range(start, end):
        hidden, _state = qwen35_mlx.decoder_layer(
            hidden, tensors, layer, config, 0, None, linear
        )
        # Evaluate per layer: the probe's per-call float32 weight images are
        # then released layer by layer instead of being scheduled together
        # (same kernels, same bytes; only the load peak changes).
        mx.eval(hidden)
    if "final_norm" in components:
        hidden = qwen35_mlx.rms_norm_offset(
            hidden, tensors[qwen35_mlx.FINAL_NORM_KEY], float(config["rms_norm_epsilon"])
        )
    if "lm_head" in components:
        head_key = aliases.get("lm_head", {}).get("tensor_keys", ["lm_head.weight"])[0]
        hidden = _qwen2_linear(hidden, tensors[head_key], mx)
    mx.eval(hidden)
    if not bool(mx.all(mx.isfinite(hidden)).item()):
        raise _fail("deterministic functional probe produced non-finite output")
    return hidden


def _layer_norm(
    hidden: Any,
    weight: Any,
    bias: Any,
    epsilon: float,
    mx: Any | None = None,
) -> Any:
    if mx is None:
        mx = _mlx_module()
    compute = hidden.astype(mx.float32)
    mean = mx.mean(compute, axis=-1, keepdims=True)
    variance = mx.mean(mx.square(compute - mean), axis=-1, keepdims=True)
    normalized = (compute - mean) * mx.rsqrt(variance + epsilon)
    return (normalized * weight.astype(mx.float32) + bias.astype(mx.float32)).astype(
        hidden.dtype
    )


def _gelu_new(value: Any, mx: Any | None = None) -> Any:
    if mx is None:
        mx = _mlx_module()
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
    hidden: Any,
    tensors: Mapping[str, Any],
    prefix: str,
    n_head: int,
    epsilon: float,
    mx: Any | None = None,
) -> Any:
    if mx is None:
        mx = _mlx_module()
    residual = hidden
    normalized = _layer_norm(
        hidden,
        tensors[prefix + "ln_1.weight"],
        tensors[prefix + "ln_1.bias"],
        epsilon,
        mx,
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
        mx,
    )
    feed_forward = (
        mx.matmul(normalized, tensors[prefix + "mlp.c_fc.weight"])
        + tensors[prefix + "mlp.c_fc.bias"]
    )
    feed_forward = _gelu_new(feed_forward, mx)
    feed_forward = (
        mx.matmul(feed_forward, tensors[prefix + "mlp.c_proj.weight"])
        + tensors[prefix + "mlp.c_proj.bias"]
    )
    return residual + feed_forward


def _rms_norm(value: Any, weight: Any, epsilon: float, mx: Any) -> Any:
    dtype = value.dtype
    compute = value.astype(mx.float32)
    normalized = compute * mx.rsqrt(mx.mean(mx.square(compute), axis=-1, keepdims=True) + epsilon)
    return (normalized * weight.astype(mx.float32)).astype(dtype)


def _qwen2_rope(query: Any, key: Any, theta: float, mx: Any) -> tuple[Any, Any]:
    sequence = int(query.shape[2])
    head_dim = int(query.shape[3])
    exponent = mx.arange(0, head_dim, 2, dtype=mx.float32) / head_dim
    inv_freq = 1.0 / mx.power(mx.array(theta, dtype=mx.float32), exponent)
    frequencies = mx.arange(sequence, dtype=mx.float32).reshape(-1, 1) * inv_freq.reshape(1, -1)
    embedding = mx.concatenate((frequencies, frequencies), axis=-1)
    cosine = mx.cos(embedding)[None, None, :, :]
    sine = mx.sin(embedding)[None, None, :, :]

    def rotate_half(value: Any) -> Any:
        first, second = mx.split(value, 2, axis=-1)
        return mx.concatenate((-second, first), axis=-1)

    return (
        query * cosine + rotate_half(query) * sine,
        key * cosine + rotate_half(key) * sine,
    )


def _quantize_qwen2_mlx_tensor(key: str, value: Any, mx: Any) -> Any:
    if not key.endswith(".weight") or len(value.shape) != 2:
        return value
    compute = value.astype(mx.float32)
    scales = mx.max(mx.abs(compute), axis=1) / 127.0
    scales = mx.where(scales == 0.0, mx.ones_like(scales), scales)
    scaled = compute / scales[:, None]
    rounded = mx.sign(scaled) * mx.floor(mx.abs(scaled) + 0.5)
    quantized = mx.clip(rounded, -127, 127).astype(mx.int8)
    mx.eval(quantized, scales)
    return Int8RowwiseWeight(quantized, scales)


def _quantize_qwen2_mlx_tensors(tensors: Mapping[str, Any], mx: Any) -> dict[str, Any]:
    return {
        key: _quantize_qwen2_mlx_tensor(key, value, mx)
        for key, value in tensors.items()
    }


def _qwen2_linear(hidden: Any, weight: Any, mx: Any) -> Any:
    if isinstance(weight, Int8RowwiseWeight):
        projected = mx.matmul(hidden, int8_float32_values(weight).transpose(1, 0))
        return projected * weight.scales
    return mx.matmul(hidden, weight.transpose(1, 0))


def _qwen2_embedding(weight: Any, token_ids: Any, mx: Any) -> Any:
    if isinstance(weight, Int8RowwiseWeight):
        return (
            int8_rows(weight, token_ids).astype(mx.float32)
            * weight.scales[token_ids, None]
        )
    return weight[token_ids]


def _qwen2_block(
    hidden: Any,
    tensors: Mapping[str, Any],
    prefix: str,
    config: Mapping[str, Any],
    mx: Any,
    architecture: str = "qwen2",
) -> Any:
    n_head = int(config["n_head"])
    n_kv_head = int(config["n_kv_head"])
    head_dim = int(config["head_dim"])
    residual = hidden
    normalized = _rms_norm(
        hidden,
        tensors[prefix + "input_layernorm.weight"],
        float(config["rms_norm_epsilon"]),
        mx,
    )
    query = _qwen2_linear(normalized, tensors[prefix + "self_attn.q_proj.weight"], mx)
    key = _qwen2_linear(normalized, tensors[prefix + "self_attn.k_proj.weight"], mx)
    value = _qwen2_linear(normalized, tensors[prefix + "self_attn.v_proj.weight"], mx)
    if architecture == "qwen2":
        query = query + tensors[prefix + "self_attn.q_proj.bias"]
        key = key + tensors[prefix + "self_attn.k_proj.bias"]
        value = value + tensors[prefix + "self_attn.v_proj.bias"]
    batch, sequence = int(hidden.shape[0]), int(hidden.shape[1])
    query = query.reshape(batch, sequence, n_head, head_dim).transpose(0, 2, 1, 3)
    key = key.reshape(batch, sequence, n_kv_head, head_dim).transpose(0, 2, 1, 3)
    value = value.reshape(batch, sequence, n_kv_head, head_dim).transpose(0, 2, 1, 3)
    if architecture == "qwen3":
        query = _rms_norm(
            query,
            tensors[prefix + "self_attn.q_norm.weight"],
            float(config["rms_norm_epsilon"]),
            mx,
        )
        key = _rms_norm(
            key,
            tensors[prefix + "self_attn.k_norm.weight"],
            float(config["rms_norm_epsilon"]),
            mx,
        )
    query, key = _qwen2_rope(query, key, float(config["rope_theta"]), mx)
    repeats = n_head // n_kv_head
    key = mx.repeat(key, repeats, axis=1)
    value = mx.repeat(value, repeats, axis=1)
    scores = mx.matmul(query, key.transpose(0, 1, 3, 2)) / math.sqrt(head_dim)
    positions = mx.arange(sequence)
    causal = positions[:, None] >= positions[None, :]
    probabilities = mx.softmax(
        mx.where(causal[None, None, :, :], scores, mx.array(-math.inf, dtype=scores.dtype)),
        axis=-1,
    )
    attended = mx.matmul(probabilities, value)
    attended = attended.transpose(0, 2, 1, 3).reshape(batch, sequence, -1)
    hidden = residual + _qwen2_linear(
        attended, tensors[prefix + "self_attn.o_proj.weight"], mx
    )
    residual = hidden
    normalized = _rms_norm(
        hidden,
        tensors[prefix + "post_attention_layernorm.weight"],
        float(config["rms_norm_epsilon"]),
        mx,
    )
    gate = _qwen2_linear(normalized, tensors[prefix + "mlp.gate_proj.weight"], mx)
    gate = gate * mx.sigmoid(gate)
    up = _qwen2_linear(normalized, tensors[prefix + "mlp.up_proj.weight"], mx)
    return residual + _qwen2_linear(
        gate * up, tensors[prefix + "mlp.down_proj.weight"], mx
    )


def _run_gpt2_probe(
    tensors: Mapping[str, Any],
    runtime: dict[str, Any],
    start: int,
    end: int,
    namespace: str,
    components: list[str],
    aliases: Mapping[str, dict[str, Any]],
) -> Any:
    mx = _mlx_module()
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
        ).astype(_runtime_dtypes()[runtime["dtype"]])

    config = runtime["model_config"]
    for layer in range(start, end):
        hidden = _gpt2_block(
            hidden,
            tensors,
            f"{namespace}h.{layer}.",
            config["n_head"],
            float(config["layer_norm_epsilon"]),
            mx,
        )
    if "final_norm" in components:
        hidden = _layer_norm(
            hidden,
            tensors[f"{namespace}ln_f.weight"],
            tensors[f"{namespace}ln_f.bias"],
            float(config["layer_norm_epsilon"]),
            mx,
        )
    if "lm_head" in components:
        head_key = aliases.get("lm_head", {}).get("tensor_keys", ["lm_head.weight"])[0]
        hidden = mx.matmul(hidden, tensors[head_key].transpose(1, 0))
    mx.eval(hidden)
    if not bool(mx.all(mx.isfinite(hidden)).item()):
        raise _fail("deterministic functional probe produced non-finite output")
    return hidden


def _run_qwen2_probe(
    tensors: Mapping[str, Any],
    runtime: dict[str, Any],
    start: int,
    end: int,
    components: list[str],
    aliases: Mapping[str, dict[str, Any]],
) -> Any:
    mx = _mlx_module()
    config = runtime["model_config"]
    if "input_embedding" in components:
        hidden = _qwen2_embedding(
            tensors["model.embed_tokens.weight"],
            mx.array([[0, 1, 2]], dtype=mx.int32),
            mx,
        )
    else:
        positions = mx.arange(1, 4, dtype=mx.float32).reshape(1, 3, 1)
        channels = mx.arange(1, config["n_embd"] + 1, dtype=mx.float32).reshape(
            1, 1, config["n_embd"]
        )
        hidden = (
            mx.sin(positions * channels)
            + positions * mx.square(channels) / (config["n_embd"] ** 2)
        ).astype(_runtime_dtypes()[runtime["dtype"]])
    for layer in range(start, end):
        hidden = _qwen2_block(
            hidden,
            tensors,
            f"model.layers.{layer}.",
            config,
            mx,
            runtime["architecture"],
        )
        # Per-layer evaluation bounds the probe's float32 weight transients
        # (same kernels, same bytes; only the load peak changes).
        mx.eval(hidden)
    if "final_norm" in components:
        hidden = _rms_norm(
            hidden,
            tensors["model.norm.weight"],
            float(config["rms_norm_epsilon"]),
            mx,
        )
    if "lm_head" in components:
        head_key = aliases.get("lm_head", {}).get("tensor_keys", ["lm_head.weight"])[0]
        hidden = _qwen2_linear(hidden, tensors[head_key], mx)
    mx.eval(hidden)
    if not bool(mx.all(mx.isfinite(hidden)).item()):
        raise _fail("deterministic functional probe produced non-finite output")
    return hidden


def execute_loaded_stage(
    loaded_stage: LoadedStage,
    *,
    token_ids: Any | None = None,
    hidden_states: Any | None = None,
) -> Any:
    """Execute exactly the GPT-2 components bound by one ``LoadedStage``.

    Runtime identity, layer range, and component roles are authenticated against
    immutable loader-held evidence rather than trusted from the proof alone.
    Entry stages accept rank-two integer token IDs; all other stages accept
    rank-three hidden states. There is intentionally no KV-cache interface, so
    callers must pass the complete sequence on every invocation.
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
    try:
        validate_loaded_stage_authentication(
            proof,
            authenticated_assignment_id=loaded_stage.authenticated_assignment_id,
            authenticated_load_generation=loaded_stage.authenticated_load_generation,
            authenticated_loaded_components=(
                loaded_stage.authenticated_loaded_components
            ),
            authenticated_loaded_range=loaded_stage.authenticated_loaded_range,
            resolved_aliases=loaded_stage.resolved_aliases,
            authenticated_resolved_aliases=(
                loaded_stage.authenticated_resolved_aliases
            ),
            authenticated_runtime=loaded_stage.authenticated_runtime,
            authenticated_runtime_identity=(
                loaded_stage.authenticated_runtime_identity
            ),
            normalized_runtime=runtime,
        )
    except ValueError as exc:
        raise RuntimeExecutionError(str(exc)) from exc
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
    try:
        validate_assignment_stage_boundaries(
            components,
            start_layer=start,
            end_layer_exclusive=end,
            total_layers=config["n_layer"],
        )
    except ValueError:
        reject("invalid_loaded_stage_boundaries")

    tensors = loaded_stage.tensors
    if not isinstance(tensors, Mapping):
        reject("invalid_loaded_stage_tensors")
    transformer_key = f"transformer.h.{start}.ln_1.weight"
    plain_key = f"h.{start}.ln_1.weight"
    qwen_key = f"model.layers.{start}.input_layernorm.weight"
    if runtime["architecture"] in {"qwen2", "qwen3"} and qwen_key in tensors:
        namespace = "model."
    elif transformer_key in tensors and plain_key not in tensors:
        namespace = "transformer."
    elif plain_key in tensors and transformer_key not in tensors:
        namespace = ""
    else:
        reject("invalid_loaded_stage_namespace")

    mx = _mlx_module()
    expected_dtype = _runtime_dtypes()[runtime["dtype"]]
    expected_dtype_name = str(expected_dtype)
    proof_digest = proof.get("loaded_tensor_digest")
    authenticated_digest = loaded_stage.authenticated_tensor_digest
    if (
        not isinstance(proof_digest, str)
        or _SHA256_REF_RE.fullmatch(proof_digest) is None
        or not isinstance(authenticated_digest, str)
        or _SHA256_REF_RE.fullmatch(authenticated_digest) is None
    ):
        reject("loaded_tensor_digest_mismatch")
    try:
        materialized_digest = _digest_arrays(tensors)
    except Exception:
        raise RuntimeExecutionError("loaded_tensor_digest_mismatch") from None
    if (
        not isinstance(materialized_digest, str)
        or _SHA256_REF_RE.fullmatch(materialized_digest) is None
        or proof_digest != authenticated_digest
        or proof_digest != materialized_digest
    ):
        reject("loaded_tensor_digest_mismatch")
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
        if runtime["architecture"] in {"qwen2", "qwen3"}:
            hidden = _qwen2_embedding(
                tensors["model.embed_tokens.weight"], token_ids, mx
            )
        else:
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
    for layer in range(start, end):
        if runtime["architecture"] in {"qwen2", "qwen3"}:
            hidden = _qwen2_block(
                hidden,
                tensors,
                f"model.layers.{layer}.",
                config,
                mx,
                runtime["architecture"],
            )
        else:
            hidden = _gpt2_block(
                hidden,
                tensors,
                f"{namespace}h.{layer}.",
                config["n_head"],
                float(config["layer_norm_epsilon"]),
                mx,
            )
    if "final_norm" in components:
        if runtime["architecture"] in {"qwen2", "qwen3"}:
            hidden = _rms_norm(
                hidden,
                tensors["model.norm.weight"],
                float(config["rms_norm_epsilon"]),
                mx,
            )
        else:
            hidden = _layer_norm(
                hidden,
                tensors[f"{namespace}ln_f.weight"],
                tensors[f"{namespace}ln_f.bias"],
                float(config["layer_norm_epsilon"]),
                mx,
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
        hidden = (
            _qwen2_linear(hidden, tensors[head_keys[0]], mx)
            if runtime["architecture"] in {"qwen2", "qwen3"}
            else mx.matmul(hidden, tensors[head_keys[0]].transpose(1, 0))
        )
    mx.eval(hidden)
    if not bool(mx.all(mx.isfinite(hidden)).item()):
        reject("nonfinite_stage_output")
    return hidden


@dataclass(frozen=True)
class MLXStageBackend:
    """Protocol adapter preserving the existing assignment-bound MLX executor."""

    backend: str = "mlx"

    def execute_loaded_stage(
        self,
        loaded_stage: Any,
        *,
        token_ids: Any | None = None,
        hidden_states: Any | None = None,
    ) -> Any:
        return execute_loaded_stage(
            loaded_stage,
            token_ids=token_ids,
            hidden_states=hidden_states,
        )


def _digest_arrays(tensors: Mapping[str, Any]) -> str:
    digest = hashlib.sha256()
    for key in sorted(tensors):
        value = tensors[key]
        if isinstance(value, PackedInt8RowwiseWeight):
            # The signed codes' exact bytes, read from the single packed copy.
            arrays = (("values", value), ("scales", value.scales))
        elif isinstance(value, Int8RowwiseWeight):
            arrays = (("values", value.values), ("scales", value.scales))
        else:
            arrays = (("value", value),)
        for part, array in arrays:
            metadata = canonical_json(
                {
                    "dtype": str(array.dtype),
                    "name": key,
                    "part": part,
                    "shape": list(_shape(array)),
                }
            ).encode("utf-8")
            payload = (
                memoryview(array.host_values()).cast("B")
                if isinstance(array, PackedInt8RowwiseWeight)
                else bytes(array)
            )
            digest.update(len(metadata).to_bytes(8, "big"))
            digest.update(metadata)
            digest.update(len(payload).to_bytes(8, "big"))
            digest.update(payload)
    return "sha256:" + digest.hexdigest()


def _digest_array(array: Any) -> str:
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


#: Probe outputs digested by ranking instead of raw float bytes, per backend.
_RANKED_PROBE_ARCHITECTURES = {
    "numpy": frozenset({"qwen2", "qwen3"}),
    "mlx": frozenset({"qwen2", "qwen3", "qwen3_5", "qwen3_5_moe"}),
}


def _digest_probe_output(array: Any, runtime: Mapping[str, Any]) -> str:
    """Digest probe semantics without binding device float rounding noise.

    The digest lands in the load proof whose canonical digest the compiling
    owner publishes as ``placement.load_proof_digest``; the member that later
    loads the stage must reproduce it on ITS machine.  Raw float32 probe bytes
    are not reproducible across machines: Qwen's final NumPy stage differs by a
    few ULPs across equivalent CPU math libraries, and MLX selects its GEMM
    tiling per GPU class (M1 ``applegpu_g13g`` vs M4 Pro ``applegpu_g16s``
    accumulate in a different order), so a byte-identical stage loaded on an
    M1 failed ``placement_load_proof_mismatch`` against an M4-compiled proof
    (two-Mac run 14).  The ordered top eight indices at every probe position
    are the stable, inference-relevant result of the probe, so those are bound
    instead.  The loaded weight bytes stay bound exactly by
    ``loaded_tensor_digest``.
    """

    if runtime["architecture"] not in _RANKED_PROBE_ARCHITECTURES.get(
        runtime["backend"], ()
    ):
        return _digest_array(array)
    if isinstance(array, np.ndarray):
        values = array
    else:
        mx = _mlx_module()
        values = np.asarray(array.astype(mx.float32))
    top_count = min(8, values.shape[-1])
    ranked = np.argsort(values, axis=-1, kind="stable")[..., -top_count:]
    return _digest_array(ranked.astype(np.dtype("<i8"), copy=False))


def _actual_runtime_identity(runtime: dict[str, Any]) -> dict[str, Any]:
    backend = runtime["backend"]
    if backend == _NUMPY_RUNTIME_BACKEND:
        try:
            version = importlib.metadata.version("numpy")
        except importlib.metadata.PackageNotFoundError:
            version = "unknown"
        return {
            "backend": "numpy",
            "backend_version": version,
            "device": "cpu",
            "dtype": runtime["dtype"],
            "quantization": runtime["quantization"],
            "architecture": runtime["architecture"],
        }
    mx = _mlx_module()
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


def _run_numpy_probe(
    *,
    tensors: Mapping[str, Any],
    runtime: dict[str, Any],
    assignment: Mapping[str, Any],
    aliases: Mapping[str, Any],
    tensor_digest: str,
    load_generation: int,
    runtime_identity: Mapping[str, Any],
) -> np.ndarray:
    """Run the same deterministic local probe through the NumPy stage adapter."""

    proof = {
        "protocol": LAYER_LOAD_PROOF_PROTOCOL,
        "assignment_id": assignment["assignment_id"],
        "loaded_range": copy.deepcopy(assignment["range"]),
        "loaded_components": list(assignment["components"]),
        "loaded_tensor_keys": sorted(tensors),
        "loaded_tensor_digest": tensor_digest,
        "resolved_component_aliases": copy.deepcopy(aliases),
        "runtime": runtime,
        "runtime_identity": runtime_identity,
        "load_generation": load_generation,
        "route_ready": False,
    }
    frozen_aliases = _deep_freeze(copy.deepcopy(aliases))
    authenticated_aliases = _deep_freeze(copy.deepcopy(aliases))
    stage = LoadedStage(
        tensors=tensors,
        resolved_aliases=frozen_aliases,
        probe_output=np.empty((0,), dtype=np.dtype(runtime["dtype"])),
        proof=proof,
        authenticated_assignment_id=assignment["assignment_id"],
        authenticated_tensor_digest=tensor_digest,
        authenticated_resolved_aliases=authenticated_aliases,
        authenticated_load_generation=load_generation,
        authenticated_loaded_components=tuple(assignment["components"]),
        authenticated_loaded_range=_deep_freeze(assignment["range"]),
        authenticated_runtime=_deep_freeze(runtime),
        authenticated_runtime_identity=_deep_freeze(runtime_identity),
    )
    components = assignment["components"]
    if "input_embedding" in components:
        return _execute_loaded_numpy_stage(
            stage,
            token_ids=np.array([[0, 1, 2]], dtype=np.int64),
        )
    hidden_size = runtime["model_config"]["n_embd"]
    positions = np.arange(1, 4, dtype=np.float32).reshape(1, 3, 1)
    channels = np.arange(1, hidden_size + 1, dtype=np.float32).reshape(
        1, 1, hidden_size
    )
    hidden = (
        np.sin(positions * channels)
        + positions * np.square(channels) / max(hidden_size * hidden_size, 1)
    ).astype(np.dtype(runtime["dtype"]))
    return _execute_loaded_numpy_stage(stage, hidden_states=hidden)


def _a9_int8_kernel_module() -> Any:
    """The A9 platform-neutral int8 kernel module, imported lazily.

    Lazy for the same reason the MLX backend is: ``runtime_loader`` must import
    and serve every other runtime path even when the A9 source tranche cannot be
    imported.
    """

    try:
        return importlib.import_module("mycelium_platform_neutral.int8_execution")
    except (ImportError, ModuleNotFoundError) as exc:
        raise _fail(
            "a9_platform_neutral_unavailable: "
            "mycelium_platform_neutral.int8_execution is not importable"
        ) from exc


def _a9_activation_transport_module() -> Any:
    """The A9 reduced-precision activation frame codec module, imported lazily."""

    try:
        return importlib.import_module(
            "mycelium_platform_neutral.activation_transport"
        )
    except (ImportError, ModuleNotFoundError) as exc:
        raise _fail(
            "a9_platform_neutral_unavailable: "
            "mycelium_platform_neutral.activation_transport is not importable"
        ) from exc


def resolve_int8_execution(
    selection: Any = DEFAULT_INT8_EXECUTION,
    *,
    activation_frame_dtype: Any = DEFAULT_ACTIVATION_FRAME_DTYPE,
) -> tuple[str, str]:
    """Validate one runtime int8 execution selection, fail-closed.

    Returns ``(selection, activation_frame_dtype)``.  An unregistered selection,
    an unsupported frame dtype, or a frame dtype requested while the qualified
    default kernel is selected is refused rather than defaulted: the qualified
    ``weight-quantization`` path is never silently replaced.
    """

    if not isinstance(selection, str) or selection not in SUPPORTED_INT8_EXECUTIONS:
        raise _fail(
            "unknown_int8_execution: int8 execution selection must be "
            f"{QUALIFIED_INT8_EXECUTION!r} or {PLATFORM_NEUTRAL_INT8_EXECUTION!r}"
        )
    if (
        not isinstance(activation_frame_dtype, str)
        or activation_frame_dtype not in SUPPORTED_ACTIVATION_FRAME_DTYPES
    ):
        raise _fail(
            "unknown_activation_frame_dtype: activation frame dtype must be "
            "float16 or bfloat16"
        )
    if (
        selection == QUALIFIED_INT8_EXECUTION
        and activation_frame_dtype != DEFAULT_ACTIVATION_FRAME_DTYPE
    ):
        raise _fail(
            "activation_frame_dtype requires the "
            f"{PLATFORM_NEUTRAL_INT8_EXECUTION} int8 execution selection"
        )
    return selection, activation_frame_dtype


def _a9_probe_activation(
    width: int, *, rows: int = A9_PROBE_ACTIVATION_ROWS
) -> np.ndarray:
    """Deterministic finite probe activation with ``width`` features.

    The same sin/quadratic ramp family the NumPy probe uses, so the A9 decode
    runs on the loader's deterministic probe activation and not on a
    model-generated one.  The emitted report names that source instead of
    implying a real activation.
    """

    if not isinstance(width, int) or isinstance(width, bool) or width <= 0:
        raise _fail("a9_activation_width_invalid: probe width must be positive")
    positions = np.arange(1, rows + 1, dtype=np.float32).reshape(rows, 1)
    channels = np.arange(1, width + 1, dtype=np.float32).reshape(1, width)
    return (
        np.sin(positions * channels)
        + positions * np.square(channels) / max(width * width, 1)
    ).astype(np.float32)


def _a9_workspace_bytes(
    *,
    out_features: int,
    in_features: int,
    accumulator_bytes: int,
    budget: int,
) -> int:
    """Bounded transient budget that keeps one decode off the whole weight.

    Capped by the declared ``budget``; for a packed weight smaller than the
    budget the budget is halved so a decode still converts the weight in bounded
    slabs rather than in one whole-matrix slab.  ``Int8LinearKernel`` reports
    either outcome in its ledger and :meth:`assert_warm_decode` refuses the warm
    claim when the whole matrix was materialized.
    """

    per_row_bytes = max(in_features * accumulator_bytes, 1)
    total_bytes = per_row_bytes * out_features
    if out_features <= 1:
        return max(per_row_bytes, min(budget, total_bytes))
    return max(per_row_bytes, min(budget, total_bytes // 2))


def _a9_platform_neutral_execution_report(
    *,
    tensors: Mapping[str, Any],
    runtime: Mapping[str, Any],
    activation_frame_dtype: str,
) -> dict[str, Any]:
    """Dispatch one loaded stage to the A9 int8 kernel and activation codec.

    This is the platform-neutral dispatch target: the loaded row-wise int8
    weights and the runtime identity are handed to the A9 persistent int8 kernel
    (bounded slab execution, monotonic materialization ledger, integer reference
    parity) and the probe activation is carried across the reduced-precision
    activation frame codec.  Every refusal the A9 modules raise is converted into
    a fail-closed ``RuntimeLoadError`` instead of being reported as success, and
    the returned report keeps the honest boundary: reachable, not qualified.
    """

    kernel_module = _a9_int8_kernel_module()
    codec_module = _a9_activation_transport_module()
    quantized = {
        key: tensor
        for key, tensor in sorted(tensors.items())
        if isinstance(tensor, Int8RowwiseWeight)
    }
    if not quantized:
        raise _fail(
            "platform_neutral_int8_requires_quantized_weights: the A9 int8 kernel "
            "executes the runtime's int8-weight-only row-wise weights"
        )

    kernel_rows: list[dict[str, Any]] = []
    budgets: set[int] = set()
    decode_calls = 0
    for key, tensor in quantized.items():
        values = np.ascontiguousarray(tensor.values, dtype=np.int8)
        scales = np.ascontiguousarray(tensor.scales, dtype=np.float32)
        out_features, in_features = int(values.shape[0]), int(values.shape[1])
        workspace_bytes = _a9_workspace_bytes(
            out_features=out_features,
            in_features=in_features,
            accumulator_bytes=int(kernel_module.ACCUMULATOR_BYTES),
            budget=int(kernel_module.DEFAULT_WORKSPACE_BYTES),
        )
        activations = _a9_probe_activation(in_features)
        try:
            kernel = kernel_module.Int8LinearKernel(
                values,
                scales,
                workspace_bytes=workspace_bytes,
                boundary="load",
            )
            kernel.warmup(activations)
            decoded = kernel.linear(activations)
            kernel.assert_warm_decode()
        except kernel_module.Int8KernelUnavailable as refusal:
            raise _fail(
                f"a9_int8_kernel_refused:{refusal.code}:{key}"
            ) from refusal
        reference = kernel_module.quantized_reference_linear(
            activations, values, scales
        )
        if not np.array_equal(decoded, reference):
            raise _fail(f"a9_int8_kernel_reference_mismatch: {key}")
        ledger = kernel.ledger()
        budgets.add(workspace_bytes)
        decode_calls += int(ledger.decode_calls)
        kernel_rows.append(
            {
                "tensor_key": key,
                "out_features": out_features,
                "in_features": in_features,
                "packed_state_bytes": int(kernel.packed_state_bytes),
                "packed_state_is_stable": bool(kernel.packed_state_is_stable()),
                "workspace_bytes": int(workspace_bytes),
                "slab_rows": int(kernel.slab_rows),
                "warm_decode_proven": True,
                "integer_reference_parity": True,
                "output_digest": _digest_array(decoded),
                "ledger": {
                    "full_weight_materializations": int(
                        ledger.full_weight_materializations
                    ),
                    "bounded_slab_conversions": int(
                        ledger.bounded_slab_conversions
                    ),
                    "peak_weight_workspace_bytes": int(
                        ledger.peak_weight_workspace_bytes
                    ),
                    "peak_activation_workspace_bytes": int(
                        ledger.peak_activation_workspace_bytes
                    ),
                    "decode_calls": int(ledger.decode_calls),
                    "warmup_complete": bool(ledger.warmup_complete),
                },
            }
        )

    hidden = int(runtime["model_config"]["n_embd"])
    frame_values = _a9_probe_activation(hidden).reshape(
        1, A9_PROBE_ACTIVATION_ROWS, hidden
    )
    codec = codec_module.ActivationFrameCodec(
        dtype=activation_frame_dtype, generation=1
    )
    try:
        frame = codec.encode(frame_values, frame_id="a9-runtime-probe-activation")
        decoded_frame = codec.decode(
            frame,
            expect_shape=frame_values.shape,
            expect_dtype=activation_frame_dtype,
        )
    except codec_module.ActivationFrameRejected as refusal:
        raise _fail(f"a9_activation_frame_refused:{refusal.code}") from refusal
    if activation_frame_dtype == "float16":
        reduced = (
            np.ascontiguousarray(frame_values).astype("float16").astype(np.float32)
        )
    else:
        reduced = codec_module.decode_bfloat16(
            codec_module.encode_bfloat16(frame_values)
        ).reshape(frame_values.shape)
    reduced_precision_parity = bool(np.array_equal(decoded_frame, reduced))
    if not reduced_precision_parity:
        raise _fail(
            "a9_activation_frame_parity_failed: the decoded frame is not the "
            "reduced-precision cast of the encoded activation"
        )
    reduction = codec.byte_reduction()
    byte_reduction_ratio = float(codec.byte_reduction_ratio())
    projection = codec.projection()
    projection_is_privacy_reduced = bool(
        codec_module.frame_projection_is_privacy_reduced(projection)
    )
    codec.cleanup()
    scope_cleaned = bool(
        codec.outstanding_frames == 0 and codec.pending_scratch_bytes() == 0
    )
    if not scope_cleaned:
        raise _fail(
            "a9_activation_frame_scope_not_cleaned: the request scope retained frames"
        )

    host_report = dict(kernel_module.reference_host_report(sorted(budgets)))
    return {
        "protocol": A9_INT8_RUNTIME_EXECUTION_PROTOCOL,
        "selection": PLATFORM_NEUTRAL_INT8_EXECUTION,
        "activation_frame_dtype": activation_frame_dtype,
        "reachable": True,
        "qualified": False,
        "physical_round_executed": False,
        "boundary": "reachable_not_qualified",
        "boundary_note": (
            "A9 platform-neutral int8 weight execution and reduced-precision "
            "activation transport are reachable from runtime_loader's runtime path "
            "on this host: the loaded row-wise int8 weights are executed by the A9 "
            "kernel (bounded slabs, monotonic materialization ledger, integer "
            "reference parity) and the probe activation crosses the reduced-precision "
            "frame codec. Nothing here is qualified, eligible, placed or ready, no "
            "physical round has been executed, and the accelerated Windows x86_64 "
            "device blocker stays open."
        ),
        "kernel_host": {
            **host_report,
            "kernel_host_backend": str(kernel_module.KERNEL_HOST_BACKEND),
            "workspace_budget_bytes": sorted(budgets),
        },
        "kernels_executed": len(kernel_rows),
        "decode_calls": decode_calls,
        "activation_source": "loader_deterministic_probe_ramp",
        "activation_rows": A9_PROBE_ACTIVATION_ROWS,
        "kernels": kernel_rows,
        "activation_transport": {
            "dtype": activation_frame_dtype,
            "generation": int(codec.generation),
            "frames_encoded": int(projection["frames_encoded"]),
            "frames_decoded": int(projection["frames_decoded"]),
            "frames_rejected": int(projection["frames_rejected"]),
            "bytes_before": int(reduction["bytes_before"]),
            "bytes_after": int(reduction["bytes_after"]),
            "byte_reduction_ratio": byte_reduction_ratio,
            "counter_digest": str(reduction["counter_digest"]),
            "frame_bytes": len(frame),
            "frame_digest": "sha256:" + hashlib.sha256(frame).hexdigest(),
            "reduced_precision_parity": reduced_precision_parity,
            "projection": projection,
            "projection_is_privacy_reduced": projection_is_privacy_reduced,
            "request_scope_cleaned": scope_cleaned,
            "outstanding_frames_after_cleanup": int(codec.outstanding_frames),
            "pending_scratch_bytes_after_cleanup": int(codec.pending_scratch_bytes()),
            "rejection_battery": (
                "not_run_on_the_runtime_path: the seven-class rejection battery stays "
                "with the codec acceptance suite (tests/a9_acceptance/"
                "test_reduced_precision_activation_transport.py)"
            ),
        },
        "acceptance_coverage": {
            "int8_execution": dict(kernel_module.ACCEPTANCE_COVERAGE),
            "activation_transport": dict(codec_module.ACCEPTANCE_COVERAGE),
        },
    }


def load_assignment_stage(
    assignment: dict[str, Any],
    artifact_report: dict[str, Any],
    *,
    load_generation: int,
    int8_execution: str = DEFAULT_INT8_EXECUTION,
    activation_frame_dtype: str = DEFAULT_ACTIVATION_FRAME_DTYPE,
) -> LoadedStage:
    """Load and probe one exact assignment from already verified local artifacts.

    This path has no network fallback. It emits local stage-load evidence only and
    deliberately keeps ``route_ready`` false pending a separate route challenge.

    ``int8_execution`` selects which kernel implements the runtime's declared
    ``int8-weight-only`` quantization:

    * ``"weight-quantization"`` (default) is the qualified row-wise int8
      representation path; its tensors, numerics and load-proof field set are
      exactly as before.
    * ``"platform-neutral-int8"`` additionally dispatches the A9
      platform-neutral persistent int8 kernel and the reduced-precision
      activation frame codec for a runtime that declares ``int8-weight-only``
      quantization (``activation_frame_dtype`` selects ``float16`` or
      ``bfloat16``).  The measured A9 result is emitted under the
      ``a9_int8_execution`` load-proof key.  This makes A9 *reachable*; it claims
      nothing qualified, and no physical round is executed.
    """
    try:
        from stage_pack import canonicalize_stage_pack_assignment

        selection, frame_dtype = resolve_int8_execution(
            int8_execution, activation_frame_dtype=activation_frame_dtype
        )

        try:
            assignment = canonicalize_stage_pack_assignment(assignment)
        except ValueError:
            raise _fail(
                "stage-pack evidence rejected: "
                "stage pack assignment files are invalid"
            ) from None
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
        stage_pack_binding = _validated_stage_pack_binding(
            assignment,
            artifact_report,
        )
        quantize_during_load = (
            runtime["architecture"] in {"qwen2", "qwen3", "qwen3_5", "qwen3_5_moe"}
            and runtime["quantization"] == "int8-weight-only"
        )
        if (
            selection == PLATFORM_NEUTRAL_INT8_EXECUTION
            and runtime["architecture"] == "qwen3_5_moe"
        ):
            # The A9 kernel executes 2-D row-wise matrices only; packed 3-D
            # experts run on the MLX stage kernels.
            raise _fail(
                "platform_neutral_int8_packed_experts_unsupported: qwen3_5_moe "
                "packed experts execute on the MLX stage path only"
            )
        if selection == PLATFORM_NEUTRAL_INT8_EXECUTION and not quantize_during_load:
            raise _fail(
                "platform_neutral_int8_requires_int8_weight_only: the A9 int8 "
                "kernel executes a runtime that declares int8-weight-only "
                "quantization"
            )
        tensors = _load_exact_tensors(
            assignment,
            artifact_report,
            verified_by_path,
            expected_keys,
            runtime_dtype,
            runtime["backend"],
            quantize_qwen=quantize_during_load,
        )
        components = list(assignment["components"])
        if runtime["architecture"] in {"qwen3_5", "qwen3_5_moe"}:
            _validate_qwen35_shapes(
                tensors, runtime, start, end, components, aliases
            )
        elif runtime["architecture"] in {"qwen2", "qwen3"}:
            _validate_qwen2_shapes(
                tensors, runtime, start, end, components, aliases
            )
        else:
            _validate_gpt2_shapes(
                tensors, runtime, start, end, namespace, components, aliases
            )
        mx = (
            _mlx_module()
            if runtime["backend"] == _MLX_RUNTIME_BACKEND
            else None
        )
        for key, tensor in tensors.items():
            if isinstance(tensor, Int8RowwiseWeight):
                if str(tensor.dtype) not in {"mlx.core.int8", "int8"}:
                    raise _fail(f"runtime quantized dtype mismatch for tensor {key}")
                finite = (
                    bool(mx.all(mx.isfinite(tensor.scales)).item())
                    if mx is not None
                    else bool(np.isfinite(tensor.scales).all())
                )
            else:
                if str(tensor.dtype) != str(runtime_dtype):
                    raise _fail(f"runtime dtype mismatch for tensor {key}")
                finite = (
                    bool(mx.all(mx.isfinite(tensor)).item())
                    if mx is not None
                    else bool(np.isfinite(tensor).all())
                )
            if not finite:
                raise _fail(f"loaded tensor contains non-finite values: {key}")
        a9_int8_execution = (
            _a9_platform_neutral_execution_report(
                tensors=tensors,
                runtime=runtime,
                activation_frame_dtype=frame_dtype,
            )
            if selection == PLATFORM_NEUTRAL_INT8_EXECUTION
            else None
        )
        loaded_tensor_digest = (
            _digest_arrays(tensors)
            if runtime["backend"] == _MLX_RUNTIME_BACKEND
            else _numpy_tensor_digest(tensors)
        )
        runtime_identity = _actual_runtime_identity(runtime)
        if runtime["backend"] == _MLX_RUNTIME_BACKEND:
            if runtime["architecture"] in {"qwen3_5", "qwen3_5_moe"}:
                probe_output = _run_qwen35_probe(
                    tensors, runtime, start, end, components, aliases
                )
            elif runtime["architecture"] in {"qwen2", "qwen3"}:
                probe_output = _run_qwen2_probe(
                    tensors, runtime, start, end, components, aliases
                )
            else:
                probe_output = _run_gpt2_probe(
                    tensors,
                    runtime,
                    start,
                    end,
                    namespace,
                    components,
                    aliases,
                )
        else:
            probe_output = _run_numpy_probe(
                tensors=tensors,
                runtime=runtime,
                assignment=assignment,
                aliases=aliases,
                tensor_digest=loaded_tensor_digest,
                load_generation=load_generation,
                runtime_identity=runtime_identity,
            )
        claim_backend = "MLX" if runtime["backend"] == "mlx" else "NumPy"
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
            "loaded_tensor_digest": loaded_tensor_digest,
            "resolved_component_aliases": copy.deepcopy(aliases),
            "runtime": runtime,
            "runtime_identity": runtime_identity,
            "probe_shape": list(_shape(probe_output)),
            "probe_digest": _digest_probe_output(probe_output, runtime),
            "load_generation": load_generation,
            "control_plane_binding": binding,
            "route_ready": False,
            "claim_boundary": (
                f"assignment-bound local {claim_backend} stage loaded and "
                "deterministically probed; "
                "no route challenge or distributed inference claim"
            ),
        }
        if stage_pack_binding is not None:
            proof["stage_pack_digest"] = stage_pack_binding[0]
            proof["stage_pack_verification_digest"] = stage_pack_binding[1]
        if a9_int8_execution is not None:
            proof[A9_INT8_EXECUTION_PROOF_KEY] = a9_int8_execution
        frozen_proof = _deep_freeze(proof)
        frozen_aliases = _deep_freeze(copy.deepcopy(aliases))
        authenticated_aliases = _deep_freeze(copy.deepcopy(aliases))
        # Force canonical serialization of the immutable evidence before returning it.
        canonical_json(frozen_proof)
        return LoadedStage(
            tensors=MappingProxyType(tensors),
            resolved_aliases=frozen_aliases,
            probe_output=probe_output,
            proof=frozen_proof,
            authenticated_assignment_id=assignment["assignment_id"],
            authenticated_tensor_digest=loaded_tensor_digest,
            authenticated_resolved_aliases=authenticated_aliases,
            authenticated_load_generation=load_generation,
            authenticated_loaded_components=tuple(components),
            authenticated_loaded_range=_deep_freeze(assignment["range"]),
            authenticated_runtime=_deep_freeze(runtime),
            authenticated_runtime_identity=_deep_freeze(runtime_identity),
        )
    except RuntimeLoadError:
        raise
    except Exception as exc:
        raise _fail(f"runtime load rejected: {exc}") from exc


def execute_loaded_numpy_stage(
    loaded_stage: Any,
    *,
    token_ids: Any | None = None,
    hidden_states: Any | None = None,
) -> np.ndarray:
    """Execute an authenticated NumPy stage using loader-stable errors."""

    try:
        return _execute_loaded_numpy_stage(
            loaded_stage,
            token_ids=token_ids,
            hidden_states=hidden_states,
        )
    except NumpyRuntimeError as exc:
        raise _fail(str(exc)) from exc


def select_stage_backend(*, runtime: Any, prefer: str) -> Any:
    """Select the backend bound by a normalized runtime identity."""

    if not isinstance(prefer, str) or prefer not in _VALID_STAGE_PREFERS:
        raise _fail(
            "unknown_prefer: stage backend preference must be auto, mlx, or numpy"
        )
    if not isinstance(runtime, Mapping):
        raise _fail("runtime identity must be an object")
    backend = runtime.get("backend")
    if backend == "mobile-rust-qwen2":
        raise _fail("external_runtime_requires_phone_load_proof")
    if backend == _NUMPY_RUNTIME_BACKEND:
        if prefer == _MLX_RUNTIME_BACKEND:
            raise _fail(
                "runtime_mismatch: numpy runtime cannot use the MLX backend"
            )
        try:
            validate_normalized_numpy_runtime(runtime)
        except (TypeError, ValueError) as exc:
            raise _fail(str(exc)) from exc
        return NumpyStageBackend()
    if backend == _MLX_RUNTIME_BACKEND:
        if prefer == _NUMPY_RUNTIME_BACKEND:
            raise _fail(
                "runtime_mismatch: mlx runtime cannot use the NumPy backend"
            )
        try:
            validate_normalized_mlx_runtime(runtime)
        except (TypeError, ValueError) as exc:
            raise _fail(str(exc)) from exc
        _mlx_module()
        return MLXStageBackend()
    raise _fail(f"unsupported runtime backend: {backend!r}")
