#!/usr/bin/env python3
"""Strict CPU NumPy monolithic GPT-2 runtime for cross-backend parity gates."""

from __future__ import annotations

import copy
import importlib.metadata
import math
from types import MappingProxyType
from typing import Any, Mapping, NoReturn

import numpy as np

from runtime_contracts import (
    GPT2_DECODER_TENSOR_SUFFIXES,
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
