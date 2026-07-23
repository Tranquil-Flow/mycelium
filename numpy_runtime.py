#!/usr/bin/env python3
"""Strict CPU NumPy monolithic GPT-2 runtime for cross-backend parity gates."""

from __future__ import annotations

import copy
import hashlib
import importlib.metadata
import json
import math
import uuid
from types import MappingProxyType
from typing import Any, Mapping, NoReturn

import numpy as np

from runtime_contracts import (
    GPT2_DECODER_TENSOR_SUFFIXES,
    assignment_stage_role,
    validate_normalized_numpy_runtime,
)


class NumpyRuntimeError(ValueError):
    """Fail-closed NumPy runtime validation or execution error."""


def _reject(code: str) -> NoReturn:
    raise NumpyRuntimeError(code)


def _expected_shapes(config: Mapping[str, Any]) -> dict[str, tuple[int, ...]]:
    n_layer = int(config["n_layer"])
    hidden = int(config["n_embd"])
    inner = int(config["n_inner"])
    shapes: dict[str, tuple[int, ...]] = {
        "transformer.wte.weight": (int(config["vocab_size"]), hidden),
        "transformer.wpe.weight": (int(config["n_positions"]), hidden),
        "transformer.ln_f.weight": (hidden,),
        "transformer.ln_f.bias": (hidden,),
    }
    suffix_shapes = {
        "ln_1.weight": (hidden,),
        "ln_1.bias": (hidden,),
        "attn.c_attn.weight": (hidden, 3 * hidden),
        "attn.c_attn.bias": (3 * hidden,),
        "attn.c_proj.weight": (hidden, hidden),
        "attn.c_proj.bias": (hidden,),
        "ln_2.weight": (hidden,),
        "ln_2.bias": (hidden,),
        "mlp.c_fc.weight": (hidden, inner),
        "mlp.c_fc.bias": (inner,),
        "mlp.c_proj.weight": (inner, hidden),
        "mlp.c_proj.bias": (hidden,),
    }
    if set(suffix_shapes) != set(GPT2_DECODER_TENSOR_SUFFIXES):
        _reject("internal_decoder_tensor_contract_mismatch")
    for layer in range(n_layer):
        prefix = f"transformer.h.{layer}."
        for suffix, shape in suffix_shapes.items():
            shapes[prefix + suffix] = shape
    return shapes


def _layer_norm(
    hidden: np.ndarray,
    weight: np.ndarray,
    bias: np.ndarray,
    epsilon: float,
) -> np.ndarray:
    mean = np.mean(hidden, axis=-1, keepdims=True)
    variance = np.mean(np.square(hidden - mean), axis=-1, keepdims=True)
    return (hidden - mean) / np.sqrt(variance + epsilon) * weight + bias


def _gelu_new(hidden: np.ndarray) -> np.ndarray:
    dtype = hidden.dtype
    compute = hidden.astype(np.float32)
    result = 0.5 * compute * (
        1.0
        + np.tanh(
            math.sqrt(2.0 / math.pi)
            * (compute + 0.044715 * np.power(compute, 3))
        )
    )
    return result.astype(dtype)


def _softmax(value: np.ndarray, axis: int) -> np.ndarray:
    dtype = value.dtype
    compute = value.astype(np.float32)
    shifted = compute - np.max(compute, axis=axis, keepdims=True)
    exponent = np.exp(shifted)
    return (exponent / np.sum(exponent, axis=axis, keepdims=True)).astype(dtype)


def _gpt2_block(
    hidden: np.ndarray,
    tensors: Mapping[str, np.ndarray],
    prefix: str,
    n_head: int,
    epsilon: float,
) -> np.ndarray:
    residual = hidden
    normalized = _layer_norm(
        hidden,
        tensors[prefix + "ln_1.weight"],
        tensors[prefix + "ln_1.bias"],
        epsilon,
    )
    qkv = (
        np.matmul(normalized, tensors[prefix + "attn.c_attn.weight"])
        + tensors[prefix + "attn.c_attn.bias"]
    )
    query, key, value = np.split(qkv, 3, axis=-1)
    batch, sequence, hidden_size = hidden.shape
    head_size = hidden_size // n_head

    def split_heads(array: np.ndarray) -> np.ndarray:
        return array.reshape(batch, sequence, n_head, head_size).transpose(0, 2, 1, 3)

    query = split_heads(query)
    key = split_heads(key)
    value = split_heads(value)
    scores = np.matmul(query, key.transpose(0, 1, 3, 2)) / math.sqrt(head_size)
    positions = np.arange(sequence)
    causal = positions[:, None] >= positions[None, :]
    scores = np.where(causal[None, None, :, :], scores, -np.inf)
    probabilities = _softmax(scores, axis=-1)
    attended = np.matmul(probabilities, value)
    attended = attended.transpose(0, 2, 1, 3).reshape(batch, sequence, hidden_size)
    attended = (
        np.matmul(attended, tensors[prefix + "attn.c_proj.weight"])
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
        np.matmul(normalized, tensors[prefix + "mlp.c_fc.weight"])
        + tensors[prefix + "mlp.c_fc.bias"]
    )
    feed_forward = _gelu_new(feed_forward)
    feed_forward = (
        np.matmul(feed_forward, tensors[prefix + "mlp.c_proj.weight"])
        + tensors[prefix + "mlp.c_proj.bias"]
    )
    return residual + feed_forward


class NumpyGPT2Runtime:
    """Full-context NumPy GPT-2 oracle; no KV cache or distributed claim."""

    backend = "numpy"

    def __init__(
        self,
        *,
        runtime: Mapping[str, Any],
        tensors: Mapping[str, Any],
    ):
        try:
            normalized = validate_normalized_numpy_runtime(runtime)
        except (TypeError, ValueError) as exc:
            raise NumpyRuntimeError("invalid_numpy_runtime") from exc
        if not isinstance(tensors, Mapping):
            _reject("invalid_tensor_mapping")
        config = normalized["model_config"]
        shapes = _expected_shapes(config)
        if set(tensors) != set(shapes):
            _reject("tensor_inventory_mismatch")
        dtype = np.dtype(normalized["dtype"])
        materialized: dict[str, np.ndarray] = {}
        for key in sorted(shapes):
            raw = np.asarray(tensors[key])
            if raw.shape != shapes[key]:
                _reject("tensor_shape_mismatch")
            if raw.dtype.kind not in {"f", "i", "u"}:
                _reject("unsupported_tensor_dtype")
            value = np.array(raw, dtype=dtype, order="C", copy=True)
            if not np.isfinite(value).all():
                _reject("nonfinite_tensor")
            value.flags.writeable = False
            materialized[key] = value
        self._runtime = copy.deepcopy(normalized)
        self._tensors = MappingProxyType(materialized)
        self._dtype = dtype
        self._identity = MappingProxyType(
            {
                "backend": "numpy",
                "backend_version": importlib.metadata.version("numpy"),
                "device": "cpu",
                "dtype": normalized["dtype"],
                "quantization": "none",
                "architecture": "gpt2",
                "route_ready": False,
                "claim_boundary": (
                    "monolithic NumPy parity runtime; no stage transport or route claim"
                ),
            }
        )

    @property
    def runtime_identity(self) -> Mapping[str, Any]:
        return self._identity

    def forward_token_ids(self, token_ids: Any) -> np.ndarray:
        ids = np.asarray(token_ids)
        config = self._runtime["model_config"]
        if ids.ndim != 2 or ids.shape[0] <= 0 or ids.shape[1] <= 0:
            _reject("invalid_token_id_shape")
        if ids.dtype.kind not in {"i", "u"}:
            _reject("invalid_token_id_dtype")
        if ids.shape[1] > config["n_positions"]:
            _reject("position_bounds_exceeded")
        if np.any(ids < 0) or np.any(ids >= config["vocab_size"]):
            _reject("token_bounds_exceeded")
        positions = np.arange(ids.shape[1], dtype=np.int64)
        hidden = (
            self._tensors["transformer.wte.weight"][ids.astype(np.int64)]
            + self._tensors["transformer.wpe.weight"][positions]
        ).astype(self._dtype)
        epsilon = float(config["layer_norm_epsilon"])
        for layer in range(config["n_layer"]):
            hidden = _gpt2_block(
                hidden,
                self._tensors,
                f"transformer.h.{layer}.",
                config["n_head"],
                epsilon,
            )
        hidden = _layer_norm(
            hidden,
            self._tensors["transformer.ln_f.weight"],
            self._tensors["transformer.ln_f.bias"],
            epsilon,
        )
        logits = np.matmul(hidden, self._tensors["transformer.wte.weight"].T)
        if not np.isfinite(logits).all():
            _reject("nonfinite_logits")
        result = np.ascontiguousarray(logits, dtype=self._dtype)
        result.flags.writeable = False
        return result


def _plain(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    return value


def _canonical_json(value: Any) -> str:
    return json.dumps(
        _plain(value),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def tensor_digest(tensors: Mapping[str, Any]) -> str:
    """Digest a materialized tensor inventory independently of its proof."""

    digest = hashlib.sha256()
    for key in sorted(tensors):
        array = np.ascontiguousarray(np.asarray(tensors[key]))
        metadata = _canonical_json(
            {
                "dtype": str(array.dtype),
                "name": key,
                "shape": list(array.shape),
            }
        ).encode("utf-8")
        payload = array.tobytes(order="C")
        digest.update(len(metadata).to_bytes(8, "big"))
        digest.update(metadata)
        digest.update(len(payload).to_bytes(8, "big"))
        digest.update(payload)
    return "sha256:" + digest.hexdigest()


def _canonical_assignment_id(value: Any) -> str:
    try:
        canonical = str(uuid.UUID(str(value)))
    except (TypeError, ValueError) as exc:
        raise NumpyRuntimeError("assignment_id_mismatch") from exc
    if value != canonical:
        _reject("assignment_id_mismatch")
    return canonical


def _stage_namespace(tensors: Mapping[str, Any], start: int) -> str:
    transformer_key = f"transformer.h.{start}.ln_1.weight"
    plain_key = f"h.{start}.ln_1.weight"
    if transformer_key in tensors and plain_key not in tensors:
        return "transformer."
    if plain_key in tensors and transformer_key not in tensors:
        return ""
    _reject("invalid_loaded_stage_namespace")


def _stage_shapes(
    *,
    config: Mapping[str, Any],
    start: int,
    end: int,
    namespace: str,
    components: list[str],
    aliases: Mapping[str, Any],
) -> dict[str, tuple[int, ...]]:
    hidden = int(config["n_embd"])
    all_shapes = _expected_shapes(config)

    def stage_key(canonical: str) -> str:
        if namespace:
            return canonical
        return canonical.removeprefix("transformer.")

    shapes: dict[str, tuple[int, ...]] = {}
    if "input_embedding" in components:
        for canonical in (
            "transformer.wte.weight",
            "transformer.wpe.weight",
        ):
            shapes[stage_key(canonical)] = all_shapes[canonical]
    for layer in range(start, end):
        canonical_prefix = f"transformer.h.{layer}."
        for suffix in GPT2_DECODER_TENSOR_SUFFIXES:
            canonical = canonical_prefix + suffix
            shapes[stage_key(canonical)] = all_shapes[canonical]
    if "final_norm" in components:
        for canonical in (
            "transformer.ln_f.weight",
            "transformer.ln_f.bias",
        ):
            shapes[stage_key(canonical)] = all_shapes[canonical]
    if "lm_head" in components:
        alias = aliases.get("lm_head")
        if not isinstance(alias, Mapping):
            _reject("invalid_loaded_stage_aliases")
        head_keys = alias.get("tensor_keys")
        if (
            not isinstance(head_keys, (list, tuple))
            or len(head_keys) != 1
            or not isinstance(head_keys[0], str)
        ):
            _reject("invalid_loaded_stage_aliases")
        shapes[head_keys[0]] = (int(config["vocab_size"]), hidden)
    return shapes


def _validated_stage(
    loaded_stage: Any,
) -> tuple[
    dict[str, Any],
    int,
    int,
    list[str],
    str,
    dict[str, np.ndarray],
    Mapping[str, Any],
]:
    proof = getattr(loaded_stage, "proof", None)
    tensors = getattr(loaded_stage, "tensors", None)
    aliases = getattr(loaded_stage, "resolved_aliases", None)
    if not isinstance(proof, Mapping):
        _reject("invalid_loaded_stage_proof")
    if proof.get("protocol") != "mycelium.layer_load_proof.v1":
        _reject("invalid_loaded_stage_proof")
    if proof.get("route_ready") is not False:
        _reject("invalid_loaded_stage_route_claim")
    assignment_id = _canonical_assignment_id(proof.get("assignment_id"))
    if getattr(loaded_stage, "authenticated_assignment_id", None) != assignment_id:
        _reject("assignment_id_mismatch")
    try:
        runtime = validate_normalized_numpy_runtime(
            json.loads(_canonical_json(proof.get("runtime")))
        )
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise NumpyRuntimeError("invalid_loaded_stage_runtime") from exc
    config = runtime["model_config"]
    layer_range = proof.get("loaded_range")
    if not isinstance(layer_range, Mapping):
        _reject("invalid_loaded_stage_range")
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
        _reject("invalid_loaded_stage_range")
    raw_components = proof.get("loaded_components")
    if not isinstance(raw_components, (list, tuple)):
        _reject("invalid_loaded_stage_components")
    components = list(raw_components)
    try:
        assignment_stage_role(components)
    except ValueError as exc:
        raise NumpyRuntimeError("invalid_loaded_stage_components") from exc
    if (
        len(components) != len(set(components))
        or "decoder" not in components
    ):
        _reject("invalid_loaded_stage_components")
    if not isinstance(tensors, Mapping):
        _reject("invalid_loaded_stage_tensors")
    if not isinstance(aliases, Mapping):
        _reject("invalid_loaded_stage_aliases")
    if _plain(aliases) != _plain(proof.get("resolved_component_aliases")):
        _reject("invalid_loaded_stage_aliases")
    namespace = _stage_namespace(tensors, start)
    shapes = _stage_shapes(
        config=config,
        start=start,
        end=end,
        namespace=namespace,
        components=components,
        aliases=aliases,
    )
    loaded_keys = proof.get("loaded_tensor_keys")
    if (
        not isinstance(loaded_keys, (list, tuple))
        or list(loaded_keys) != sorted(shapes)
        or set(tensors) != set(shapes)
    ):
        _reject("loaded_tensor_inventory_mismatch")
    expected_dtype = np.dtype(runtime["dtype"])
    materialized: dict[str, np.ndarray] = {}
    for key in sorted(shapes):
        value = np.asarray(tensors[key])
        if value.shape != shapes[key]:
            _reject("tensor_shape_mismatch")
        if value.dtype != expected_dtype:
            _reject("unsupported_tensor_dtype")
        if not np.isfinite(value).all():
            _reject("nonfinite_tensor")
        materialized[key] = value
    proof_digest = proof.get("loaded_tensor_digest")
    authenticated_digest = getattr(
        loaded_stage, "authenticated_tensor_digest", None
    )
    actual_digest = tensor_digest(materialized)
    if (
        not isinstance(proof_digest, str)
        or proof_digest != authenticated_digest
        or proof_digest != actual_digest
    ):
        _reject("loaded_tensor_digest_mismatch")
    return (
        runtime,
        start,
        end,
        components,
        namespace,
        materialized,
        aliases,
    )


def _validated_token_ids(token_ids: Any, config: Mapping[str, Any]) -> np.ndarray:
    ids = np.asarray(token_ids)
    if ids.ndim != 2 or ids.shape[0] <= 0 or ids.shape[1] <= 0:
        _reject("invalid_token_id_shape")
    if ids.dtype.kind not in {"i", "u"}:
        _reject("invalid_token_id_dtype")
    if ids.shape[1] > config["n_positions"]:
        _reject("position_bounds_exceeded")
    if np.any(ids < 0) or np.any(ids >= config["vocab_size"]):
        _reject("token_bounds_exceeded")
    return ids.astype(np.int64, copy=False)


def _validated_hidden_states(
    hidden_states: Any,
    config: Mapping[str, Any],
    dtype: np.dtype[Any],
) -> np.ndarray:
    hidden = np.asarray(hidden_states)
    if hidden.ndim != 3:
        _reject("invalid_hidden_state_rank")
    if (
        hidden.shape[0] <= 0
        or hidden.shape[1] <= 0
        or hidden.shape[2] != config["n_embd"]
    ):
        _reject("invalid_hidden_state_shape")
    if hidden.shape[1] > config["n_positions"]:
        _reject("position_bounds_exceeded")
    if hidden.dtype != dtype:
        _reject("hidden_state_dtype_mismatch")
    if not np.isfinite(hidden).all():
        _reject("nonfinite_hidden_states")
    return hidden


def execute_loaded_stage(
    loaded_stage: Any,
    *,
    token_ids: Any | None = None,
    hidden_states: Any | None = None,
) -> np.ndarray:
    """Execute an authenticated assignment-local stage with NumPy."""

    (
        runtime,
        start,
        end,
        components,
        namespace,
        tensors,
        aliases,
    ) = _validated_stage(loaded_stage)
    config = runtime["model_config"]
    dtype = np.dtype(runtime["dtype"])
    role = assignment_stage_role(components)

    if role == "entry":
        if token_ids is None or hidden_states is not None:
            _reject("entry_stage_requires_token_ids")
        ids = _validated_token_ids(token_ids, config)
        positions = np.arange(ids.shape[1], dtype=np.int64)
        hidden = (
            tensors[f"{namespace}wte.weight"][ids]
            + tensors[f"{namespace}wpe.weight"][positions]
        ).astype(dtype, copy=False)
    else:
        if hidden_states is None or token_ids is not None:
            _reject("non_entry_stage_requires_hidden_states")
        hidden = _validated_hidden_states(hidden_states, config, dtype)

    epsilon = float(config["layer_norm_epsilon"])
    for layer in range(start, end):
        hidden = _gpt2_block(
            hidden,
            tensors,
            f"{namespace}h.{layer}.",
            int(config["n_head"]),
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
        head_key = aliases["lm_head"]["tensor_keys"][0]
        hidden = np.matmul(hidden, tensors[head_key].transpose(1, 0))
    if not np.isfinite(hidden).all():
        _reject("nonfinite_stage_output")
    result = np.ascontiguousarray(hidden, dtype=dtype)
    result.flags.writeable = False
    return result


class NumpyStageBackend:
    """Assignment-local CPU stage adapter with no route or physical claim."""

    backend = "numpy"

    def execute_loaded_stage(
        self,
        loaded_stage: Any,
        *,
        token_ids: Any | None = None,
        hidden_states: Any | None = None,
    ) -> np.ndarray:
        return execute_loaded_stage(
            loaded_stage,
            token_ids=token_ids,
            hidden_states=hidden_states,
        )

    def runtime_identity(self) -> Mapping[str, Any]:
        return MappingProxyType(
            {
                "backend": "numpy",
                "backend_version": importlib.metadata.version("numpy"),
                "device": "cpu",
                "route_ready": False,
                "claim_boundary": (
                    "assignment-bound local NumPy stage; no route challenge "
                    "or physical execution claim"
                ),
            }
        )
