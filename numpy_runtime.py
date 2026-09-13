#!/usr/bin/env python3
"""Strict CPU NumPy monolithic GPT-2 runtime for cross-backend parity gates."""

from __future__ import annotations

import copy
import hashlib
import importlib.metadata
import json
import math
from types import MappingProxyType
from typing import Any, Callable, Mapping, NoReturn

import numpy as np

from model_adapters import (
    QWEN3_5_FULL_ATTENTION_TENSOR_SUFFIXES,
    QWEN3_5_LINEAR_ATTENTION_TENSOR_SUFFIXES,
)
from runtime_contracts import (
    GPT2_DECODER_TENSOR_SUFFIXES,
    QWEN2_DECODER_TENSOR_SUFFIXES,
    QWEN3_DECODER_TENSOR_SUFFIXES,
    assignment_stage_role,
    validate_assignment_stage_boundaries,
    validate_loaded_stage_authentication,
    validate_normalized_numpy_runtime,
)
from weight_quantization import Int8RowwiseWeight


_CANCELLABLE_SEQUENCE_CHUNK = 32
_CANCELLABLE_OUTPUT_CHUNK = 1024


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


def quantize_qwen2_numpy_tensor(key: str, raw: Any) -> Any:
    """Quantize one Qwen matrix without retaining its float source."""

    value = np.asarray(raw)
    if not key.endswith(".weight") or value.ndim != 2:
        return raw
    compute = value.astype(np.float32)
    scales = np.max(np.abs(compute), axis=1) / 127.0
    scales = np.where(scales == 0.0, 1.0, scales).astype(np.float32)
    scaled = compute / scales[:, None]
    rounded = np.sign(scaled) * np.floor(np.abs(scaled) + 0.5)
    quantized = np.clip(rounded, -127, 127).astype(np.int8)
    quantized.flags.writeable = False
    scales.flags.writeable = False
    return Int8RowwiseWeight(quantized, scales)


def quantize_qwen2_numpy_tensors(
    tensors: Mapping[str, Any],
) -> dict[str, Any]:
    """Replace Qwen2 matrix weights with deterministic symmetric int8 rows."""

    return {
        key: quantize_qwen2_numpy_tensor(key, raw)
        for key, raw in tensors.items()
    }


def _qwen2_linear(hidden: np.ndarray, weight: Any) -> np.ndarray:
    if isinstance(weight, Int8RowwiseWeight):
        projected = np.matmul(hidden, weight.values.astype(np.float32).T)
        return projected * weight.scales
    return np.matmul(hidden, weight.T)


def _qwen2_linear_checkpointed(
    hidden: np.ndarray,
    weight: Any,
    *,
    checkpoint: Callable[[], None] | None,
) -> np.ndarray:
    """Project large matrices in bounded, cancellable row/sequence blocks."""

    output_features = int(
        weight.values.shape[0]
        if isinstance(weight, Int8RowwiseWeight)
        else weight.shape[0]
    )
    sequence = int(hidden.shape[-2])
    if checkpoint is None or (
        output_features <= _CANCELLABLE_OUTPUT_CHUNK
        and sequence <= _CANCELLABLE_SEQUENCE_CHUNK
    ):
        return _qwen2_linear(hidden, weight)
    output_chunks: list[np.ndarray] = []
    for output_start in range(0, output_features, _CANCELLABLE_OUTPUT_CHUNK):
        output_end = min(
            output_start + _CANCELLABLE_OUTPUT_CHUNK,
            output_features,
        )
        if isinstance(weight, Int8RowwiseWeight):
            matrix = weight.values[output_start:output_end].astype(np.float32)
            scales = weight.scales[output_start:output_end]
        else:
            matrix = weight[output_start:output_end]
            scales = None
        sequence_chunks: list[np.ndarray] = []
        for sequence_start in range(0, sequence, _CANCELLABLE_SEQUENCE_CHUNK):
            sequence_end = min(
                sequence_start + _CANCELLABLE_SEQUENCE_CHUNK,
                sequence,
            )
            projected = np.matmul(
                hidden[..., sequence_start:sequence_end, :],
                matrix.T,
            )
            if scales is not None:
                projected = projected * scales
            sequence_chunks.append(projected)
            checkpoint()
        output_chunks.append(np.concatenate(sequence_chunks, axis=-2))
    return np.concatenate(output_chunks, axis=-1)


def _qwen2_embedding(weight: Any, ids: np.ndarray) -> np.ndarray:
    if isinstance(weight, Int8RowwiseWeight):
        return weight.values[ids].astype(np.float32) * weight.scales[ids, None]
    return weight[ids]


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


def _rms_norm(hidden: np.ndarray, weight: np.ndarray, epsilon: float) -> np.ndarray:
    dtype = hidden.dtype
    compute = hidden.astype(np.float32)
    normalized = compute * np.reciprocal(
        np.sqrt(np.mean(np.square(compute), axis=-1, keepdims=True) + epsilon)
    )
    return (normalized * weight.astype(np.float32)).astype(dtype)


def _silu(hidden: np.ndarray) -> np.ndarray:
    dtype = hidden.dtype
    compute = hidden.astype(np.float32)
    return (compute / (1.0 + np.exp(-compute))).astype(dtype)


def _qwen2_rope(
    query: np.ndarray,
    key: np.ndarray,
    *,
    theta: float,
) -> tuple[np.ndarray, np.ndarray]:
    sequence = query.shape[2]
    head_dim = query.shape[3]
    inv_freq = 1.0 / (
        theta ** (np.arange(0, head_dim, 2, dtype=np.float32) / head_dim)
    )
    frequencies = np.outer(np.arange(sequence, dtype=np.float32), inv_freq)
    embedding = np.concatenate((frequencies, frequencies), axis=-1)
    cosine = np.cos(embedding)[None, None, :, :]
    sine = np.sin(embedding)[None, None, :, :]

    def rotate_half(value: np.ndarray) -> np.ndarray:
        first, second = np.split(value, 2, axis=-1)
        return np.concatenate((-second, first), axis=-1)

    return (
        query * cosine + rotate_half(query) * sine,
        key * cosine + rotate_half(key) * sine,
    )


def _qwen2_rope_at_position(
    query: np.ndarray,
    key: np.ndarray,
    *,
    position: int,
    theta: float,
) -> tuple[np.ndarray, np.ndarray]:
    """Apply Qwen rotary embeddings at an absolute cache position."""

    sequence = int(query.shape[2])
    head_dim = int(query.shape[3])
    inv_freq = 1.0 / (
        theta ** (np.arange(0, head_dim, 2, dtype=np.float32) / head_dim)
    )
    positions = np.arange(position, position + sequence, dtype=np.float32)
    frequencies = positions[:, None] * inv_freq[None, :]
    embedding = np.concatenate((frequencies, frequencies), axis=-1)
    cosine = np.cos(embedding)[None, None, :, :]
    sine = np.sin(embedding)[None, None, :, :]

    def rotate_half(value: np.ndarray) -> np.ndarray:
        first, second = np.split(value, 2, axis=-1)
        return np.concatenate((-second, first), axis=-1)

    return (
        query * cosine + rotate_half(query) * sine,
        key * cosine + rotate_half(key) * sine,
    )


# --- Qwen3.5 hybrid decoder reference execution ------------------------------
#
# The verified qwen3_5 route artifact (mlx-community/Qwen3.8-27B-4bit) is a
# hybrid decoder.  ``linear_attention`` layers are gated delta-net recurrent
# mixers whose quantized projections carry first-class ``.scales``/``.biases``
# companions (materialized to dense floats by the loader); ``full_attention``
# layers are gated self-attention (q/k RMS norms on the head dimension,
# partial rotary embeddings, sigmoid output gate).  These reference paths
# mirror the artifact's native runtime semantics (mlx_lm ``qwen3_5`` /
# transformers ``modeling_qwen3_5``): recurrent (not chunked) gated delta
# rule, explicit causal depthwise convolution, no KV cache.  Correctness
# first; speed is deliberately not a goal of this path.

_QWEN3_5_AFFINE4BIT_GROUP_SIZE = 64


def _sigmoid(hidden: np.ndarray) -> np.ndarray:
    dtype = hidden.dtype
    compute = hidden.astype(np.float32)
    return np.exp(-np.logaddexp(np.zeros_like(compute), -compute)).astype(dtype)


def _qwen3_5_layer_tensor_shapes(
    config: Mapping[str, Any], layer_type: str
) -> dict[str, tuple[int, ...]]:
    """Exact per-layer-type tensor shapes for the qwen3_5 hybrid decoder.

    The quantized projections always carry their ``.scales``/``.biases``
    companion tensors (affine 4-bit, group size 64); ``conv1d``, ``A_log``,
    ``dt_bias``, norms, and the embedding/lm_head companions are validated by
    their own entries.  Unsupported head/group layouts fail closed.
    """

    hidden = int(config["n_embd"])
    inner = int(config["n_inner"])
    group = _QWEN3_5_AFFINE4BIT_GROUP_SIZE

    def grouped(
        prefix: str, rows: int, columns: int
    ) -> None:
        if columns % group:
            _reject("unsupported_qwen3_5_group_layout")
        shapes[f"{prefix}.weight"] = (rows, columns)
        shapes[f"{prefix}.scales"] = (rows, columns // group)
        shapes[f"{prefix}.biases"] = (rows, columns // group)

    shapes: dict[str, tuple[int, ...]] = {
        "input_layernorm.weight": (hidden,),
        "post_attention_layernorm.weight": (hidden,),
    }
    if layer_type == "linear_attention":
        nk = int(config["linear_num_key_heads"])
        nv = int(config["linear_num_value_heads"])
        kd = int(config["linear_key_head_dim"])
        dv = int(config["linear_value_head_dim"])
        conv_kernel = int(config["linear_conv_kernel_dim"])
        if nk <= 0 or nv % nk:
            _reject("unsupported_qwen3_5_linear_heads")
        key_dim = kd * nk
        value_dim = dv * nv
        conv_dim = 2 * key_dim + value_dim
        shapes["linear_attn.A_log"] = (nv,)
        shapes["linear_attn.dt_bias"] = (nv,)
        shapes["linear_attn.norm.weight"] = (dv,)
        shapes["linear_attn.conv1d.weight"] = (conv_dim, conv_kernel, 1)
        grouped("linear_attn.in_proj_qkv", conv_dim, hidden)
        grouped("linear_attn.in_proj_z", value_dim, hidden)
        grouped("linear_attn.in_proj_a", nv, hidden)
        grouped("linear_attn.in_proj_b", nv, hidden)
        grouped("linear_attn.out_proj", hidden, value_dim)
        expected = QWEN3_5_LINEAR_ATTENTION_TENSOR_SUFFIXES
    elif layer_type == "full_attention":
        n_head = int(config["n_head"])
        n_kv_head = int(config["n_kv_head"])
        head_dim = int(config["head_dim"])
        if n_kv_head <= 0 or n_head % n_kv_head:
            _reject("unsupported_qwen3_5_attention_heads")
        shapes["self_attn.q_norm.weight"] = (head_dim,)
        shapes["self_attn.k_norm.weight"] = (head_dim,)
        grouped("self_attn.q_proj", 2 * n_head * head_dim, hidden)
        grouped("self_attn.k_proj", n_kv_head * head_dim, hidden)
        grouped("self_attn.v_proj", n_kv_head * head_dim, hidden)
        grouped("self_attn.o_proj", hidden, n_head * head_dim)
        expected = QWEN3_5_FULL_ATTENTION_TENSOR_SUFFIXES
    else:
        _reject("unsupported_qwen3_5_layer_type")
    grouped("mlp.gate_proj", inner, hidden)
    grouped("mlp.up_proj", inner, hidden)
    grouped("mlp.down_proj", hidden, inner)
    if set(shapes) != set(expected):
        _reject("internal_decoder_tensor_contract_mismatch")
    return shapes


def _qwen3_5_rope(
    query: np.ndarray,
    key: np.ndarray,
    *,
    theta: float,
    rotary_dim: int,
) -> tuple[np.ndarray, np.ndarray]:
    """Partial rotary embedding (rotate_half style) over the first ``rotary_dim`` dims."""

    if rotary_dim < 2 or rotary_dim % 2 or rotary_dim > int(query.shape[-1]):
        _reject("unsupported_qwen3_5_rotary_dim")
    sequence = int(query.shape[2])
    inv_freq = 1.0 / (
        theta ** (np.arange(0, rotary_dim, 2, dtype=np.float32) / rotary_dim)
    )
    frequencies = np.outer(np.arange(sequence, dtype=np.float32), inv_freq)
    embedding = np.concatenate((frequencies, frequencies), axis=-1)
    cosine = np.cos(embedding)[None, None, :, :]
    sine = np.sin(embedding)[None, None, :, :]

    def rotate_half(value: np.ndarray) -> np.ndarray:
        first, second = np.split(value, 2, axis=-1)
        return np.concatenate((-second, first), axis=-1)

    query_rotary = query[..., :rotary_dim]
    key_rotary = key[..., :rotary_dim]
    return (
        np.concatenate(
            (
                query_rotary * cosine + rotate_half(query_rotary) * sine,
                query[..., rotary_dim:],
            ),
            axis=-1,
        ),
        np.concatenate(
            (
                key_rotary * cosine + rotate_half(key_rotary) * sine,
                key[..., rotary_dim:],
            ),
            axis=-1,
        ),
    )


def _qwen3_5_full_attention(
    hidden: np.ndarray,
    tensors: Mapping[str, Any],
    prefix: str,
    config: Mapping[str, Any],
) -> np.ndarray:
    """Gated full attention: q/k RMS norms, partial rotary, sigmoid output gate."""

    n_head = int(config["n_head"])
    n_kv_head = int(config["n_kv_head"])
    head_dim = int(config["head_dim"])
    epsilon = float(config["rms_norm_epsilon"])
    batch, sequence = int(hidden.shape[0]), int(hidden.shape[1])
    q_out = _qwen2_linear(hidden, tensors[prefix + "self_attn.q_proj.weight"])
    q_out = q_out.reshape(batch, sequence, n_head, 2 * head_dim)
    query, gate = np.split(q_out, 2, axis=-1)
    gate = gate.reshape(batch, sequence, n_head * head_dim)
    key = _qwen2_linear(
        hidden, tensors[prefix + "self_attn.k_proj.weight"]
    ).reshape(batch, sequence, n_kv_head, head_dim)
    value = _qwen2_linear(
        hidden, tensors[prefix + "self_attn.v_proj.weight"]
    ).reshape(batch, sequence, n_kv_head, head_dim)
    query = _rms_norm(
        query, tensors[prefix + "self_attn.q_norm.weight"], epsilon
    ).transpose(0, 2, 1, 3)
    key = _rms_norm(
        key, tensors[prefix + "self_attn.k_norm.weight"], epsilon
    ).transpose(0, 2, 1, 3)
    value = value.transpose(0, 2, 1, 3)
    query, key = _qwen3_5_rope(
        query,
        key,
        theta=float(config["rope_theta"]),
        rotary_dim=int(head_dim * float(config["partial_rotary_factor"])),
    )
    repeats = n_head // n_kv_head
    if repeats > 1:
        key = np.repeat(key, repeats, axis=1)
        value = np.repeat(value, repeats, axis=1)
    scores = np.matmul(query, key.transpose(0, 1, 3, 2)) / math.sqrt(head_dim)
    positions = np.arange(sequence)
    causal = positions[:, None] >= positions[None, :]
    scores = np.where(
        causal[None, None, :, :], scores, np.float32("-inf")
    )
    probabilities = _softmax(scores, axis=-1)
    attended = (
        np.matmul(probabilities, value)
        .transpose(0, 2, 1, 3)
        .reshape(batch, sequence, n_head * head_dim)
    )
    attended = attended * _sigmoid(gate)
    return _qwen2_linear(attended, tensors[prefix + "self_attn.o_proj.weight"])


def _qwen3_5_gated_delta_net(
    hidden: np.ndarray,
    tensors: Mapping[str, Any],
    prefix: str,
    config: Mapping[str, Any],
) -> np.ndarray:
    """Gated delta-net recurrent mixer (reference implementation, no KV cache)."""

    num_k_heads = int(config["linear_num_key_heads"])
    num_v_heads = int(config["linear_num_value_heads"])
    key_head_dim = int(config["linear_key_head_dim"])
    value_head_dim = int(config["linear_value_head_dim"])
    conv_kernel = int(config["linear_conv_kernel_dim"])
    epsilon = float(config["rms_norm_epsilon"])
    key_dim = key_head_dim * num_k_heads
    value_dim = value_head_dim * num_v_heads
    conv_dim = 2 * key_dim + value_dim
    batch, sequence = int(hidden.shape[0]), int(hidden.shape[1])

    mixed = _qwen2_linear(hidden, tensors[prefix + "linear_attn.in_proj_qkv.weight"])
    z = _qwen2_linear(hidden, tensors[prefix + "linear_attn.in_proj_z.weight"]).reshape(
        batch, sequence, num_v_heads, value_head_dim
    )
    a = _qwen2_linear(hidden, tensors[prefix + "linear_attn.in_proj_a.weight"])
    b = _qwen2_linear(hidden, tensors[prefix + "linear_attn.in_proj_b.weight"])

    conv_weight = tensors[prefix + "linear_attn.conv1d.weight"].reshape(
        conv_dim, conv_kernel
    )
    padded = np.concatenate(
        (
            np.zeros((batch, conv_kernel - 1, conv_dim), dtype=mixed.dtype),
            mixed,
        ),
        axis=1,
    )
    convolved = np.zeros_like(mixed)
    for tap in range(conv_kernel):
        convolved = convolved + padded[
            :, tap : tap + sequence, :
        ] * conv_weight[None, None, :, tap]
    mixed = _silu(convolved)

    query = mixed[:, :, :key_dim].reshape(batch, sequence, num_k_heads, key_head_dim)
    key = mixed[:, :, key_dim : 2 * key_dim].reshape(
        batch, sequence, num_k_heads, key_head_dim
    )
    value = mixed[:, :, 2 * key_dim :].reshape(
        batch, sequence, num_v_heads, value_head_dim
    )

    beta = _sigmoid(b)
    activation = a.astype(np.float32) + tensors[
        prefix + "linear_attn.dt_bias"
    ].astype(np.float32)
    decay = -np.exp(tensors[prefix + "linear_attn.A_log"].astype(np.float32)) * np.logaddexp(
        activation, np.zeros_like(activation)
    )

    repeats = num_v_heads // num_k_heads
    if repeats > 1:
        query = np.repeat(query, repeats, axis=2)
        key = np.repeat(key, repeats, axis=2)
    query = query.transpose(0, 2, 1, 3)
    key = key.transpose(0, 2, 1, 3)
    value = value.transpose(0, 2, 1, 3)

    # q/k are RMS-normalized on the head dimension; mlx_lm's qwen3_5 folds the
    # delta-rule scale 1/sqrt(d_k) into q as (inv_scale**2) * rms_norm(q).
    def rms_normalize(value: np.ndarray) -> np.ndarray:
        compute = value.astype(np.float32)
        return compute * np.reciprocal(
            np.sqrt(
                np.mean(np.square(compute), axis=-1, keepdims=True)
                + np.float32(1e-6)
            )
        )

    inv_scale = key_head_dim**-0.5
    query = (inv_scale**2) * rms_normalize(query)
    key = inv_scale * rms_normalize(key)
    decay = decay.transpose(0, 2, 1)
    beta = beta.transpose(0, 2, 1)

    state = np.zeros((batch, num_v_heads, key_head_dim, value_head_dim), dtype=np.float32)
    outputs = []
    for step in range(sequence):
        step_decay = np.exp(decay[:, :, step])[:, :, None, None]
        state = state * step_decay
        key_step = key[:, :, step]
        value_step = value[:, :, step]
        kv_memory = np.sum(state * key_step[:, :, :, None], axis=-2)
        delta = (value_step - kv_memory) * beta[:, :, step, None]
        state = state + key_step[:, :, :, None] * delta[:, :, None, :]
        outputs.append(np.sum(state * query[:, :, step, :, None], axis=-2))
    core = np.stack(outputs, axis=2).transpose(0, 2, 1, 3).reshape(
        batch * sequence * num_v_heads, value_head_dim
    )
    z_rows = z.reshape(batch * sequence * num_v_heads, value_head_dim)
    gated = _rms_norm(
        core, tensors[prefix + "linear_attn.norm.weight"], epsilon
    ) * (z_rows * _sigmoid(z_rows))
    output = gated.reshape(batch, sequence, value_dim)
    return _qwen2_linear(output, tensors[prefix + "linear_attn.out_proj.weight"])


def _qwen3_5_block(
    hidden: np.ndarray,
    tensors: Mapping[str, Any],
    prefix: str,
    config: Mapping[str, Any],
    layer_type: str,
) -> np.ndarray:
    """Execute one qwen3_5 hybrid decoder layer with the reference path."""

    epsilon = float(config["rms_norm_epsilon"])
    residual = hidden
    normalized = _rms_norm(hidden, tensors[prefix + "input_layernorm.weight"], epsilon)
    if layer_type == "linear_attention":
        attended = _qwen3_5_gated_delta_net(normalized, tensors, prefix, config)
    elif layer_type == "full_attention":
        attended = _qwen3_5_full_attention(normalized, tensors, prefix, config)
    else:
        _reject("unsupported_qwen3_5_layer_type")
    hidden = residual + attended
    residual = hidden
    normalized = _rms_norm(
        hidden, tensors[prefix + "post_attention_layernorm.weight"], epsilon
    )
    gate = _qwen2_linear(normalized, tensors[prefix + "mlp.gate_proj.weight"])
    up = _qwen2_linear(normalized, tensors[prefix + "mlp.up_proj.weight"])
    return residual + _qwen2_linear(
        (gate * _sigmoid(gate)) * up, tensors[prefix + "mlp.down_proj.weight"]
    )


def _qwen2_block_with_kv(
    hidden: np.ndarray,
    tensors: Mapping[str, Any],
    prefix: str,
    config: Mapping[str, Any],
    position: int,
    past: tuple[np.ndarray, np.ndarray] | None,
    architecture: str = "qwen2",
    *,
    checkpoint: Callable[[], None] | None = None,
) -> tuple[np.ndarray, tuple[np.ndarray, np.ndarray]]:
    """Execute one dense Qwen decoder block while retaining local K/V state."""

    def checked() -> None:
        if checkpoint is not None:
            checkpoint()

    def linear(value: np.ndarray, weight: Any) -> np.ndarray:
        output_features = int(
            weight.values.shape[0]
            if isinstance(weight, Int8RowwiseWeight)
            else weight.shape[0]
        )
        if (
            checkpoint is not None
            and output_features > _CANCELLABLE_OUTPUT_CHUNK
        ):
            return _qwen2_linear_checkpointed(
                value,
                weight,
                checkpoint=checkpoint,
            )
        sequence = int(value.shape[-2])
        if checkpoint is None or sequence <= _CANCELLABLE_SEQUENCE_CHUNK:
            return _qwen2_linear(value, weight)
        if isinstance(weight, Int8RowwiseWeight):
            matrix = weight.values.astype(np.float32).T

            def project(chunk: np.ndarray) -> np.ndarray:
                return np.matmul(chunk, matrix) * weight.scales

        else:
            matrix = weight.T

            def project(chunk: np.ndarray) -> np.ndarray:
                return np.matmul(chunk, matrix)

        chunks = []
        for start in range(0, sequence, _CANCELLABLE_SEQUENCE_CHUNK):
            chunks.append(
                project(value[..., start : start + _CANCELLABLE_SEQUENCE_CHUNK, :])
            )
            checked()
        return np.concatenate(chunks, axis=-2)

    n_head = int(config["n_head"])
    n_kv_head = int(config["n_kv_head"])
    head_dim = int(config["head_dim"])
    epsilon = float(config["rms_norm_epsilon"])
    residual = hidden
    normalized = _rms_norm(
        hidden, tensors[prefix + "input_layernorm.weight"], epsilon
    )
    checked()
    query = linear(normalized, tensors[prefix + "self_attn.q_proj.weight"])
    checked()
    key = linear(normalized, tensors[prefix + "self_attn.k_proj.weight"])
    checked()
    value = linear(normalized, tensors[prefix + "self_attn.v_proj.weight"])
    checked()
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
            epsilon,
        )
        key = _rms_norm(
            key,
            tensors[prefix + "self_attn.k_norm.weight"],
            epsilon,
        )
    query, key = _qwen2_rope_at_position(
        query,
        key,
        position=position,
        theta=float(config["rope_theta"]),
    )
    checked()
    if past is None:
        all_key = key
        all_value = value
    else:
        all_key = np.concatenate((past[0], key), axis=2)
        all_value = np.concatenate((past[1], value), axis=2)
    repeats = n_head // n_kv_head
    attention_key = np.repeat(all_key, repeats, axis=1)
    attention_value = np.repeat(all_value, repeats, axis=1)
    transposed_key = attention_key.transpose(0, 1, 3, 2)
    query_positions = np.arange(position, position + sequence)
    key_positions = np.arange(int(all_key.shape[2]))
    if checkpoint is not None and sequence > _CANCELLABLE_SEQUENCE_CHUNK:
        attended_chunks = []
        for start in range(0, sequence, _CANCELLABLE_SEQUENCE_CHUNK):
            end = min(start + _CANCELLABLE_SEQUENCE_CHUNK, sequence)
            scores = (
                np.matmul(query[:, :, start:end, :], transposed_key)
                / math.sqrt(head_dim)
            )
            checked()
            causal = query_positions[start:end, None] >= key_positions[None, :]
            probabilities = _softmax(
                np.where(causal[None, None, :, :], scores, -np.inf),
                axis=-1,
            )
            checked()
            attended_chunks.append(np.matmul(probabilities, attention_value))
            checked()
        attended = np.concatenate(attended_chunks, axis=2)
    else:
        scores = np.matmul(query, transposed_key) / math.sqrt(head_dim)
        checked()
        causal = query_positions[:, None] >= key_positions[None, :]
        probabilities = _softmax(
            np.where(causal[None, None, :, :], scores, -np.inf), axis=-1
        )
        checked()
        attended = np.matmul(probabilities, attention_value)
    checked()
    attended = attended.transpose(0, 2, 1, 3).reshape(batch, sequence, -1)
    hidden = residual + linear(
        attended, tensors[prefix + "self_attn.o_proj.weight"]
    )
    checked()

    residual = hidden
    normalized = _rms_norm(
        hidden, tensors[prefix + "post_attention_layernorm.weight"], epsilon
    )
    checked()
    gate = linear(
        normalized, tensors[prefix + "mlp.gate_proj.weight"]
    )
    checked()
    gate = _silu(gate)
    up = linear(normalized, tensors[prefix + "mlp.up_proj.weight"])
    checked()
    hidden = residual + linear(
        gate * up, tensors[prefix + "mlp.down_proj.weight"]
    )
    return hidden, (all_key, all_value)


def _qwen2_block(
    hidden: np.ndarray,
    tensors: Mapping[str, np.ndarray],
    prefix: str,
    config: Mapping[str, Any],
    architecture: str = "qwen2",
) -> np.ndarray:
    n_head = int(config["n_head"])
    n_kv_head = int(config["n_kv_head"])
    head_dim = int(config["head_dim"])
    epsilon = float(config["rms_norm_epsilon"])
    residual = hidden
    normalized = _rms_norm(
        hidden, tensors[prefix + "input_layernorm.weight"], epsilon
    )
    query = _qwen2_linear(normalized, tensors[prefix + "self_attn.q_proj.weight"])
    key = _qwen2_linear(normalized, tensors[prefix + "self_attn.k_proj.weight"])
    value = _qwen2_linear(normalized, tensors[prefix + "self_attn.v_proj.weight"])
    if architecture == "qwen2":
        query += tensors[prefix + "self_attn.q_proj.bias"]
        key += tensors[prefix + "self_attn.k_proj.bias"]
        value += tensors[prefix + "self_attn.v_proj.bias"]
    batch, sequence, _ = hidden.shape
    query = query.reshape(batch, sequence, n_head, head_dim).transpose(0, 2, 1, 3)
    key = key.reshape(batch, sequence, n_kv_head, head_dim).transpose(0, 2, 1, 3)
    value = value.reshape(batch, sequence, n_kv_head, head_dim).transpose(0, 2, 1, 3)
    if architecture == "qwen3":
        query = _rms_norm(
            query,
            tensors[prefix + "self_attn.q_norm.weight"],
            epsilon,
        )
        key = _rms_norm(
            key,
            tensors[prefix + "self_attn.k_norm.weight"],
            epsilon,
        )
    query, key = _qwen2_rope(query, key, theta=float(config["rope_theta"]))
    repeats = n_head // n_kv_head
    key = np.repeat(key, repeats, axis=1)
    value = np.repeat(value, repeats, axis=1)
    scores = np.matmul(query, key.transpose(0, 1, 3, 2)) / math.sqrt(head_dim)
    positions = np.arange(sequence)
    causal = positions[:, None] >= positions[None, :]
    probabilities = _softmax(
        np.where(causal[None, None, :, :], scores, -np.inf), axis=-1
    )
    attended = np.matmul(probabilities, value)
    attended = attended.transpose(0, 2, 1, 3).reshape(batch, sequence, -1)
    hidden = residual + _qwen2_linear(
        attended, tensors[prefix + "self_attn.o_proj.weight"]
    )
    residual = hidden
    normalized = _rms_norm(
        hidden, tensors[prefix + "post_attention_layernorm.weight"], epsilon
    )
    gated = _silu(
        _qwen2_linear(normalized, tensors[prefix + "mlp.gate_proj.weight"])
    ) * _qwen2_linear(normalized, tensors[prefix + "mlp.up_proj.weight"])
    return residual + _qwen2_linear(
        gated, tensors[prefix + "mlp.down_proj.weight"]
    )


def _qwen2_expected_shapes(
    config: Mapping[str, Any], architecture: str = "qwen2"
) -> dict[str, tuple[int, ...]]:
    hidden = int(config["n_embd"])
    inner = int(config["n_inner"])
    head_dim = int(config["head_dim"])
    kv = int(config["n_kv_head"]) * head_dim
    shapes: dict[str, tuple[int, ...]] = {
        "model.embed_tokens.weight": (int(config["vocab_size"]), hidden),
        "model.norm.weight": (hidden,),
    }
    suffix_shapes: dict[str, tuple[int, ...]] = {
        "input_layernorm.weight": (hidden,),
        "self_attn.q_proj.weight": (hidden, hidden),
        "self_attn.k_proj.weight": (kv, hidden),
        "self_attn.v_proj.weight": (kv, hidden),
        "self_attn.o_proj.weight": (hidden, hidden),
        "post_attention_layernorm.weight": (hidden,),
        "mlp.gate_proj.weight": (inner, hidden),
        "mlp.up_proj.weight": (inner, hidden),
        "mlp.down_proj.weight": (hidden, inner),
    }
    if architecture == "qwen2":
        suffix_shapes.update(
            {
                "self_attn.q_proj.bias": (hidden,),
                "self_attn.k_proj.bias": (kv,),
                "self_attn.v_proj.bias": (kv,),
            }
        )
        expected_suffixes = QWEN2_DECODER_TENSOR_SUFFIXES
    elif architecture == "qwen3":
        suffix_shapes.update(
            {
                "self_attn.q_norm.weight": (head_dim,),
                "self_attn.k_norm.weight": (head_dim,),
            }
        )
        expected_suffixes = QWEN3_DECODER_TENSOR_SUFFIXES
    else:
        _reject("unsupported_qwen_architecture")
    if set(suffix_shapes) != set(expected_suffixes):
        _reject("internal_decoder_tensor_contract_mismatch")
    for layer in range(int(config["n_layer"])):
        prefix = f"model.layers.{layer}."
        for suffix, shape in suffix_shapes.items():
            shapes[prefix + suffix] = shape
    if not bool(config["tie_word_embeddings"]):
        shapes["lm_head.weight"] = (int(config["vocab_size"]), hidden)
    return shapes


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
        if normalized["quantization"] == "int8-weight-only":
            materialized = quantize_qwen2_numpy_tensors(materialized)
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


class NumpyQwen2Runtime:
    """Full-context NumPy Qwen2 oracle used by the M5 parity gate."""

    backend = "numpy"

    def __init__(self, *, runtime: Mapping[str, Any], tensors: Mapping[str, Any]):
        try:
            normalized = validate_normalized_numpy_runtime(runtime)
        except (TypeError, ValueError) as exc:
            raise NumpyRuntimeError("invalid_numpy_runtime") from exc
        if normalized["architecture"] != "qwen2":
            _reject("invalid_numpy_runtime")
        shapes = _qwen2_expected_shapes(normalized["model_config"])
        if not isinstance(tensors, Mapping) or set(tensors) != set(shapes):
            _reject("tensor_inventory_mismatch")
        dtype = np.dtype(normalized["dtype"])
        materialized: dict[str, np.ndarray] = {}
        for key in sorted(shapes):
            raw = np.asarray(tensors[key])
            if raw.shape != shapes[key] or raw.dtype.kind not in {"f", "i", "u"}:
                _reject("tensor_shape_mismatch")
            value = np.array(raw, dtype=dtype, order="C", copy=True)
            if not np.isfinite(value).all():
                _reject("nonfinite_tensor")
            value.flags.writeable = False
            materialized[key] = value
        if normalized["quantization"] == "int8-weight-only":
            materialized = quantize_qwen2_numpy_tensors(materialized)
        self._runtime = copy.deepcopy(normalized)
        self._tensors = MappingProxyType(materialized)
        self._dtype = dtype
        self._identity = MappingProxyType(
            {
                "backend": "numpy",
                "backend_version": importlib.metadata.version("numpy"),
                "device": "cpu",
                "dtype": normalized["dtype"],
                "quantization": normalized["quantization"],
                "architecture": "qwen2",
                "route_ready": False,
                "claim_boundary": "monolithic NumPy parity runtime; no route claim",
            }
        )

    @property
    def runtime_identity(self) -> Mapping[str, Any]:
        return self._identity

    def forward_token_ids(self, token_ids: Any) -> np.ndarray:
        ids = _validated_token_ids(token_ids, self._runtime["model_config"])
        config = self._runtime["model_config"]
        hidden = _qwen2_embedding(
            self._tensors["model.embed_tokens.weight"], ids
        ).astype(self._dtype, copy=False)
        for layer in range(int(config["n_layer"])):
            hidden = _qwen2_block(
                hidden, self._tensors, f"model.layers.{layer}.", config
            )
        hidden = _rms_norm(
            hidden,
            self._tensors["model.norm.weight"],
            float(config["rms_norm_epsilon"]),
        )
        head = (
            self._tensors["model.embed_tokens.weight"]
            if config["tie_word_embeddings"]
            else self._tensors["lm_head.weight"]
        )
        logits = _qwen2_linear(hidden, head)
        if not np.isfinite(logits).all():
            _reject("nonfinite_logits")
        result = np.ascontiguousarray(logits, dtype=self._dtype)
        result.flags.writeable = False
        return result


class NumpyQwen3Runtime:
    """Full-context NumPy Qwen3 oracle used by adapter parity gates."""

    backend = "numpy"

    def __init__(self, *, runtime: Mapping[str, Any], tensors: Mapping[str, Any]):
        try:
            normalized = validate_normalized_numpy_runtime(runtime)
        except (TypeError, ValueError) as exc:
            raise NumpyRuntimeError("invalid_numpy_runtime") from exc
        if normalized["architecture"] != "qwen3":
            _reject("invalid_numpy_runtime")
        shapes = _qwen2_expected_shapes(normalized["model_config"], "qwen3")
        if not isinstance(tensors, Mapping) or set(tensors) != set(shapes):
            _reject("tensor_inventory_mismatch")
        dtype = np.dtype(normalized["dtype"])
        materialized: dict[str, np.ndarray] = {}
        for key in sorted(shapes):
            raw = np.asarray(tensors[key])
            if raw.shape != shapes[key] or raw.dtype.kind not in {"f", "i", "u"}:
                _reject("tensor_shape_mismatch")
            value = np.array(raw, dtype=dtype, order="C", copy=True)
            if not np.isfinite(value).all():
                _reject("nonfinite_tensor")
            value.flags.writeable = False
            materialized[key] = value
        if normalized["quantization"] == "int8-weight-only":
            materialized = quantize_qwen2_numpy_tensors(materialized)
        self._runtime = copy.deepcopy(normalized)
        self._tensors = MappingProxyType(materialized)
        self._dtype = dtype
        self._identity = MappingProxyType(
            {
                "backend": "numpy",
                "backend_version": importlib.metadata.version("numpy"),
                "device": "cpu",
                "dtype": normalized["dtype"],
                "quantization": normalized["quantization"],
                "architecture": "qwen3",
                "route_ready": False,
                "claim_boundary": "monolithic NumPy parity runtime; no route claim",
            }
        )

    @property
    def runtime_identity(self) -> Mapping[str, Any]:
        return self._identity

    def forward_token_ids(self, token_ids: Any) -> np.ndarray:
        ids = _validated_token_ids(token_ids, self._runtime["model_config"])
        config = self._runtime["model_config"]
        hidden = _qwen2_embedding(
            self._tensors["model.embed_tokens.weight"], ids
        ).astype(self._dtype, copy=False)
        for layer in range(int(config["n_layer"])):
            hidden = _qwen2_block(
                hidden,
                self._tensors,
                f"model.layers.{layer}.",
                config,
                "qwen3",
            )
        hidden = _rms_norm(
            hidden,
            self._tensors["model.norm.weight"],
            float(config["rms_norm_epsilon"]),
        )
        head = (
            self._tensors["model.embed_tokens.weight"]
            if config["tie_word_embeddings"]
            else self._tensors["lm_head.weight"]
        )
        logits = _qwen2_linear(hidden, head)
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
        value = tensors[key]
        arrays = (
            (("values", value.values), ("scales", value.scales))
            if isinstance(value, Int8RowwiseWeight)
            else (("value", value),)
        )
        for part, raw in arrays:
            array = np.ascontiguousarray(np.asarray(raw))
            metadata = _canonical_json(
                {
                    "dtype": str(array.dtype),
                    "name": key,
                    "part": part,
                    "shape": list(array.shape),
                }
            ).encode("utf-8")
            payload = array.tobytes(order="C")
            digest.update(len(metadata).to_bytes(8, "big"))
            digest.update(metadata)
            digest.update(len(payload).to_bytes(8, "big"))
            digest.update(payload)
    return "sha256:" + digest.hexdigest()


def _stage_namespace(
    tensors: Mapping[str, Any], start: int, architecture: str
) -> str:
    if architecture in {"qwen2", "qwen3"}:
        if f"model.layers.{start}.input_layernorm.weight" in tensors:
            return "model."
        _reject("invalid_loaded_stage_namespace")
    if architecture == "qwen3_5":
        if f"language_model.model.layers.{start}.input_layernorm.weight" in tensors:
            return "language_model.model."
        _reject("invalid_loaded_stage_namespace")
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
    architecture: str,
) -> dict[str, tuple[int, ...]]:
    if architecture in {"qwen2", "qwen3"}:
        all_shapes = _qwen2_expected_shapes(config, architecture)
        shapes: dict[str, tuple[int, ...]] = {}
        if "input_embedding" in components:
            shapes["model.embed_tokens.weight"] = all_shapes[
                "model.embed_tokens.weight"
            ]
        for layer in range(start, end):
            prefix = f"model.layers.{layer}."
            suffixes = (
                QWEN2_DECODER_TENSOR_SUFFIXES
                if architecture == "qwen2"
                else QWEN3_DECODER_TENSOR_SUFFIXES
            )
            for suffix in suffixes:
                shapes[prefix + suffix] = all_shapes[prefix + suffix]
        if "final_norm" in components:
            shapes["model.norm.weight"] = all_shapes["model.norm.weight"]
        if "lm_head" in components:
            alias = aliases.get("lm_head")
            head_key = (
                alias["tensor_keys"][0]
                if isinstance(alias, Mapping)
                else "lm_head.weight"
            )
            shapes[head_key] = (int(config["vocab_size"]), int(config["n_embd"]))
        return shapes
    if architecture == "qwen3_5":
        group = _QWEN3_5_AFFINE4BIT_GROUP_SIZE
        hidden = int(config["n_embd"])
        vocabulary = int(config["vocab_size"])
        if hidden % group:
            _reject("unsupported_qwen3_5_group_layout")
        shapes = {}
        if "input_embedding" in components:
            for suffix, shape in (
                ("weight", (vocabulary, hidden)),
                ("scales", (vocabulary, hidden // group)),
                ("biases", (vocabulary, hidden // group)),
            ):
                shapes[f"language_model.model.embed_tokens.{suffix}"] = shape
        layer_types = config["layer_types"]
        for layer in range(start, end):
            prefix = f"language_model.model.layers.{layer}."
            for suffix, shape in _qwen3_5_layer_tensor_shapes(
                config, layer_types[layer]
            ).items():
                shapes[prefix + suffix] = shape
        if "final_norm" in components:
            shapes["language_model.model.norm.weight"] = (hidden,)
        if "lm_head" in components:
            alias = aliases.get("lm_head")
            if alias is None:
                head_key = "language_model.lm_head.weight"
            else:
                if not isinstance(alias, Mapping):
                    _reject("invalid_loaded_stage_aliases")
                head_keys = alias.get("tensor_keys")
                if (
                    not isinstance(head_keys, (list, tuple))
                    or len(head_keys) != 1
                    or not isinstance(head_keys[0], str)
                ):
                    _reject("invalid_loaded_stage_aliases")
                head_key = head_keys[0]
            if not head_key.endswith(".weight"):
                _reject("invalid_loaded_stage_aliases")
            base = head_key[: -len(".weight")]
            shapes[head_key] = (vocabulary, hidden)
            shapes[f"{base}.scales"] = (vocabulary, hidden // group)
            shapes[base + ".biases"] = (vocabulary, hidden // group)
        return shapes
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
        if alias is None:
            head_key = "lm_head.weight"
        else:
            if not isinstance(alias, Mapping):
                _reject("invalid_loaded_stage_aliases")
            head_keys = alias.get("tensor_keys")
            if (
                not isinstance(head_keys, (list, tuple))
                or len(head_keys) != 1
                or not isinstance(head_keys[0], str)
            ):
                _reject("invalid_loaded_stage_aliases")
            head_key = head_keys[0]
        shapes[head_key] = (int(config["vocab_size"]), hidden)
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
    try:
        runtime = validate_normalized_numpy_runtime(
            json.loads(_canonical_json(proof.get("runtime")))
        )
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise NumpyRuntimeError("invalid_loaded_stage_runtime") from exc
    try:
        validate_loaded_stage_authentication(
            proof,
            authenticated_assignment_id=getattr(
                loaded_stage, "authenticated_assignment_id", None
            ),
            authenticated_load_generation=getattr(
                loaded_stage, "authenticated_load_generation", None
            ),
            authenticated_loaded_components=getattr(
                loaded_stage, "authenticated_loaded_components", None
            ),
            authenticated_loaded_range=getattr(
                loaded_stage, "authenticated_loaded_range", None
            ),
            resolved_aliases=aliases,
            authenticated_resolved_aliases=getattr(
                loaded_stage, "authenticated_resolved_aliases", None
            ),
            authenticated_runtime=getattr(
                loaded_stage, "authenticated_runtime", None
            ),
            authenticated_runtime_identity=getattr(
                loaded_stage, "authenticated_runtime_identity", None
            ),
            normalized_runtime=runtime,
        )
    except ValueError as exc:
        raise NumpyRuntimeError(str(exc)) from exc
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
    try:
        validate_assignment_stage_boundaries(
            components,
            start_layer=start,
            end_layer_exclusive=end,
            total_layers=config["n_layer"],
        )
    except ValueError as exc:
        raise NumpyRuntimeError("invalid_loaded_stage_boundaries") from exc
    if not isinstance(tensors, Mapping):
        _reject("invalid_loaded_stage_tensors")
    namespace = _stage_namespace(tensors, start, runtime["architecture"])
    shapes = _stage_shapes(
        config=config,
        start=start,
        end=end,
        namespace=namespace,
        components=components,
        aliases=aliases,
        architecture=runtime["architecture"],
    )
    loaded_keys = proof.get("loaded_tensor_keys")
    if (
        not isinstance(loaded_keys, (list, tuple))
        or list(loaded_keys) != sorted(shapes)
        or set(tensors) != set(shapes)
    ):
        _reject("loaded_tensor_inventory_mismatch")
    expected_dtype = np.dtype(runtime["dtype"])
    materialized: dict[str, Any] = {}
    for key in sorted(shapes):
        raw = tensors[key]
        if isinstance(raw, Int8RowwiseWeight):
            if runtime["quantization"] != "int8-weight-only":
                _reject("unsupported_tensor_dtype")
            if raw.shape != shapes[key] or np.asarray(raw.values).dtype != np.int8:
                _reject("tensor_shape_mismatch")
            scales = np.asarray(raw.scales)
            if scales.shape != (shapes[key][0],) or scales.dtype != np.float32:
                _reject("tensor_shape_mismatch")
            if not np.isfinite(scales).all() or np.any(scales <= 0):
                _reject("nonfinite_tensor")
            materialized[key] = raw
            continue
        value = np.asarray(raw)
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
    try:
        actual_digest = tensor_digest(materialized)
    except Exception:
        raise NumpyRuntimeError("loaded_tensor_digest_mismatch") from None
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
        if runtime["architecture"] in {"qwen2", "qwen3"}:
            hidden = _qwen2_embedding(
                tensors["model.embed_tokens.weight"], ids
            ).astype(dtype, copy=False)
        elif runtime["architecture"] == "qwen3_5":
            hidden = tensors["language_model.model.embed_tokens.weight"][
                ids
            ].astype(dtype, copy=False)
        else:
            positions = np.arange(ids.shape[1], dtype=np.int64)
            hidden = (
                tensors[f"{namespace}wte.weight"][ids]
                + tensors[f"{namespace}wpe.weight"][positions]
            ).astype(dtype, copy=False)
    else:
        if hidden_states is None or token_ids is not None:
            _reject("non_entry_stage_requires_hidden_states")
        hidden = _validated_hidden_states(hidden_states, config, dtype)

    for layer in range(start, end):
        if runtime["architecture"] in {"qwen2", "qwen3"}:
            hidden = _qwen2_block(
                hidden,
                tensors,
                f"model.layers.{layer}.",
                config,
                runtime["architecture"],
            )
        elif runtime["architecture"] == "qwen3_5":
            hidden = _qwen3_5_block(
                hidden,
                tensors,
                f"language_model.model.layers.{layer}.",
                config,
                config["layer_types"][layer],
            )
        else:
            hidden = _gpt2_block(
                hidden,
                tensors,
                f"{namespace}h.{layer}.",
                int(config["n_head"]),
                float(config["layer_norm_epsilon"]),
            )
    if "final_norm" in components:
        if runtime["architecture"] in {"qwen2", "qwen3"}:
            hidden = _rms_norm(
                hidden,
                tensors["model.norm.weight"],
                float(config["rms_norm_epsilon"]),
            )
        elif runtime["architecture"] == "qwen3_5":
            hidden = _rms_norm(
                hidden,
                tensors["language_model.model.norm.weight"],
                float(config["rms_norm_epsilon"]),
            )
        else:
            hidden = _layer_norm(
                hidden,
                tensors[f"{namespace}ln_f.weight"],
                tensors[f"{namespace}ln_f.bias"],
                float(config["layer_norm_epsilon"]),
            )
    if "lm_head" in components:
        alias = aliases.get("lm_head")
        if isinstance(alias, Mapping):
            head_key = alias["tensor_keys"][0]
        elif runtime["architecture"] == "qwen3_5":
            head_key = "language_model.lm_head.weight"
        else:
            head_key = "lm_head.weight"
        hidden = (
            _qwen2_linear(hidden, tensors[head_key])
            if runtime["architecture"] in {"qwen2", "qwen3", "qwen3_5"}
            else np.matmul(hidden, tensors[head_key].transpose(1, 0))
        )
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
