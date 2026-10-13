#!/usr/bin/env python3
"""Qwen3.5 hybrid text decoder layers for the MLX stage path.

A Qwen3.5 (``model_type: qwen3_5``) decoder layer mixes tokens with either a
Gated DeltaNet ("linear_attention") or gated full attention, chosen by the
normalized ``layer_types``.  Every layer keeps a stage-local, layer-local state
2-tuple, so stage boundaries may fall on any layer:

* ``full_attention``: ``(key, value)``, each ``[B, kv_heads, T, head_dim]`` and
  growing with the context (keys are stored after ``k_norm`` and RoPE);
* ``linear_attention``: ``(conv_state, recurrent_state)`` with shapes
  ``[B, K - 1, conv_dim]`` and ``[B, Hv, Dv, Dk]`` in float32, constant size.

The recurrent state is a running summary: it cannot be sliced back to an earlier
position, so callers must never rewind it (see the runtime refusals).

Numerics follow the Hugging Face ``Qwen3_5`` text model: zero-centred RMSNorm
``(1 + w)`` for the layer/final/q/k norms, partial rotate-half RoPE on the first
``rotary_dim`` channels (text-only positions make interleaved mRoPE exactly 1-D
RoPE), ``o_proj(attn * sigmoid(gate))`` with the gate taken per head from
``q_proj``, and the delta rule with ``g = -exp(A_log) * softplus(a + dt_bias)``,
``beta = sigmoid(b)``, L2-normalised q/k (q scaled by ``Dk ** -0.5``) and a
gated RMSNorm ``w * rmsnorm(o) * silu(z)``.  Activations are float32 here; the
module takes the stage's linear projection function so row-wise int8 weights
reuse the incumbent kernels.
"""
from __future__ import annotations

import math
from typing import Any, Callable, Mapping

import mlx.core as mx

from qwen35_shapes import (
    EMBED_KEY as EMBED_KEY,
    FINAL_NORM_KEY as FINAL_NORM_KEY,
    FULL_ATTENTION,
    LINEAR_ATTENTION,
    TEXT_PREFIX as TEXT_PREFIX,
    expected_qwen35_shapes as expected_shapes,
    layer_prefix,
)

__all__ = ["EMBED_KEY", "FINAL_NORM_KEY", "TEXT_PREFIX", "expected_shapes", "decoder_layer"]
_L2_EPSILON = 1e-6

Linear = Callable[[mx.array, Any], mx.array]


class Qwen35ExecutionError(ValueError):
    """A Qwen3.5 layer refused its inputs; ``str(exc)`` is the stable code."""


def rms_norm_offset(hidden: mx.array, weight: mx.array, epsilon: float) -> mx.array:
    """Zero-centred RMSNorm: ``x * rsqrt(mean(x^2) + eps) * (1 + w)``."""

    dtype = hidden.dtype
    compute = hidden.astype(mx.float32)
    normalized = compute * mx.rsqrt(
        mx.mean(mx.square(compute), axis=-1, keepdims=True) + epsilon
    )
    return (normalized * (1.0 + weight.astype(mx.float32))).astype(dtype)


def _silu(value: mx.array) -> mx.array:
    return value * mx.sigmoid(value)


def _softplus(value: mx.array) -> mx.array:
    return mx.logaddexp(value, mx.zeros_like(value))


def _partial_rope(value: mx.array, *, position: int, rotary_dim: int, theta: float) -> mx.array:
    sequence = int(value.shape[2])
    exponent = mx.arange(0, rotary_dim, 2, dtype=mx.float32) / rotary_dim
    inv_freq = 1.0 / mx.power(mx.array(theta, dtype=mx.float32), exponent)
    positions = mx.arange(position, position + sequence, dtype=mx.float32)
    frequencies = positions.reshape(-1, 1) * inv_freq.reshape(1, -1)
    embedding = mx.concatenate((frequencies, frequencies), axis=-1)
    cosine = mx.cos(embedding)[None, None, :, :]
    sine = mx.sin(embedding)[None, None, :, :]
    rotary = value[..., :rotary_dim]
    passthrough = value[..., rotary_dim:]
    first, second = mx.split(rotary, 2, axis=-1)
    rotated = mx.concatenate((-second, first), axis=-1)
    return mx.concatenate((rotary * cosine + rotated * sine, passthrough), axis=-1)


def gated_attention(
    normalized: mx.array,
    tensors: Mapping[str, Any],
    prefix: str,
    config: Mapping[str, Any],
    position: int,
    past: tuple[mx.array, mx.array] | None,
    linear: Linear,
) -> tuple[mx.array, tuple[mx.array, mx.array]]:
    n_head = int(config["n_head"])
    n_kv_head = int(config["n_kv_head"])
    head_dim = int(config["head_dim"])
    epsilon = float(config["rms_norm_epsilon"])
    batch, sequence = int(normalized.shape[0]), int(normalized.shape[1])
    projected = linear(normalized, tensors[prefix + "self_attn.q_proj.weight"])
    projected = projected.reshape(batch, sequence, n_head, 2 * head_dim)
    query = projected[..., :head_dim]
    gate = projected[..., head_dim:].reshape(batch, sequence, n_head * head_dim)
    key = linear(normalized, tensors[prefix + "self_attn.k_proj.weight"])
    value = linear(normalized, tensors[prefix + "self_attn.v_proj.weight"])
    query = rms_norm_offset(query, tensors[prefix + "self_attn.q_norm.weight"], epsilon)
    key = rms_norm_offset(
        key.reshape(batch, sequence, n_kv_head, head_dim),
        tensors[prefix + "self_attn.k_norm.weight"],
        epsilon,
    )
    query = query.transpose(0, 2, 1, 3)
    key = key.transpose(0, 2, 1, 3)
    value = value.reshape(batch, sequence, n_kv_head, head_dim).transpose(0, 2, 1, 3)
    rotary_dim = int(config["rotary_dim"])
    theta = float(config["rope_theta"])
    query = _partial_rope(query, position=position, rotary_dim=rotary_dim, theta=theta)
    key = _partial_rope(key, position=position, rotary_dim=rotary_dim, theta=theta)
    if past is None:
        all_key, all_value = key, value
    else:
        all_key = mx.concatenate((past[0], key), axis=2)
        all_value = mx.concatenate((past[1], value), axis=2)
    repeats = n_head // n_kv_head
    attention_key = mx.repeat(all_key, repeats, axis=1)
    attention_value = mx.repeat(all_value, repeats, axis=1)
    scores = mx.matmul(query, attention_key.transpose(0, 1, 3, 2)) / math.sqrt(head_dim)
    query_positions = mx.arange(position, position + sequence)
    key_positions = mx.arange(int(all_key.shape[2]))
    causal = query_positions[:, None] >= key_positions[None, :]
    probabilities = mx.softmax(
        mx.where(causal[None, None, :, :], scores, mx.array(-math.inf, dtype=scores.dtype)),
        axis=-1,
    )
    attended = mx.matmul(probabilities, attention_value)
    attended = attended.transpose(0, 2, 1, 3).reshape(batch, sequence, n_head * head_dim)
    output = linear(attended * mx.sigmoid(gate), tensors[prefix + "self_attn.o_proj.weight"])
    return output, (all_key, all_value)


def gated_delta_net(
    normalized: mx.array,
    tensors: Mapping[str, Any],
    prefix: str,
    config: Mapping[str, Any],
    past: tuple[mx.array, mx.array] | None,
    linear: Linear,
) -> tuple[mx.array, tuple[mx.array, mx.array]]:
    hk = int(config["linear_num_key_heads"])
    dk = int(config["linear_key_head_dim"])
    hv = int(config["linear_num_value_heads"])
    dv = int(config["linear_value_head_dim"])
    kernel = int(config["linear_conv_kernel_dim"])
    epsilon = float(config["rms_norm_epsilon"])
    key_dim = hk * dk
    value_dim = hv * dv
    conv_dim = 2 * key_dim + value_dim
    batch, sequence = int(normalized.shape[0]), int(normalized.shape[1])
    mixed = linear(normalized, tensors[prefix + "linear_attn.in_proj_qkv.weight"]).astype(
        mx.float32
    )
    z = linear(normalized, tensors[prefix + "linear_attn.in_proj_z.weight"]).astype(mx.float32)
    b = linear(normalized, tensors[prefix + "linear_attn.in_proj_b.weight"]).astype(mx.float32)
    a = linear(normalized, tensors[prefix + "linear_attn.in_proj_a.weight"]).astype(mx.float32)
    if past is None:
        conv_state = mx.zeros((batch, kernel - 1, conv_dim), dtype=mx.float32)
        recurrent = mx.zeros((batch, hv, dv, dk), dtype=mx.float32)
    else:
        conv_state, recurrent = past
        if (
            tuple(conv_state.shape) != (batch, kernel - 1, conv_dim)
            or tuple(recurrent.shape) != (batch, hv, dv, dk)
        ):
            raise Qwen35ExecutionError("qwen35_linear_state_shape_mismatch")
    # Causal depthwise convolution over the last ``kernel`` projected inputs.
    conv_input = mx.concatenate((conv_state, mixed), axis=1)
    next_conv_state = conv_input[:, -(kernel - 1) :, :] if kernel > 1 else conv_state
    weight = tensors[prefix + "linear_attn.conv1d.weight"].astype(mx.float32)
    weight = weight.reshape(conv_dim, kernel).transpose(1, 0)
    convolved = conv_input[:, 0:sequence, :] * weight[0]
    for tap in range(1, kernel):
        convolved = convolved + conv_input[:, tap : tap + sequence, :] * weight[tap]
    convolved = _silu(convolved)
    query = convolved[..., :key_dim].reshape(batch, sequence, hk, dk)
    key = convolved[..., key_dim : 2 * key_dim].reshape(batch, sequence, hk, dk)
    value = convolved[..., 2 * key_dim :].reshape(batch, sequence, hv, dv)
    if hv != hk:
        query = mx.repeat(query, hv // hk, axis=2)
        key = mx.repeat(key, hv // hk, axis=2)
    query = query * mx.rsqrt(mx.sum(mx.square(query), axis=-1, keepdims=True) + _L2_EPSILON)
    query = query * (dk ** -0.5)
    key = key * mx.rsqrt(mx.sum(mx.square(key), axis=-1, keepdims=True) + _L2_EPSILON)
    beta = mx.sigmoid(b)
    a_log = tensors[prefix + "linear_attn.A_log"].astype(mx.float32)
    dt_bias = tensors[prefix + "linear_attn.dt_bias"].astype(mx.float32)
    decay = mx.exp(-mx.exp(a_log) * _softplus(a + dt_bias))
    outputs = []
    state = recurrent
    for step in range(sequence):
        q_t = query[:, step]
        k_t = key[:, step]
        state = state * decay[:, step][..., None, None]
        remembered = mx.sum(state * k_t[..., None, :], axis=-1)
        delta = (value[:, step] - remembered) * beta[:, step][..., None]
        state = state + delta[..., None] * k_t[..., None, :]
        outputs.append(mx.sum(state * q_t[..., None, :], axis=-1))
    core = mx.stack(outputs, axis=1)
    norm_weight = tensors[prefix + "linear_attn.norm.weight"].astype(mx.float32)
    core = core * mx.rsqrt(mx.mean(mx.square(core), axis=-1, keepdims=True) + epsilon)
    core = core * norm_weight * _silu(z.reshape(batch, sequence, hv, dv))
    output = linear(
        core.reshape(batch, sequence, value_dim).astype(normalized.dtype),
        tensors[prefix + "linear_attn.out_proj.weight"],
    )
    return output, (next_conv_state, state)


def decoder_layer(
    hidden: mx.array,
    tensors: Mapping[str, Any],
    layer: int,
    config: Mapping[str, Any],
    position: int,
    past: tuple[mx.array, mx.array] | None,
    linear: Linear,
) -> tuple[mx.array, tuple[mx.array, mx.array]]:
    """One Qwen3.5 decoder layer; returns the residual stream and its state."""

    if not isinstance(position, int) or isinstance(position, bool) or position < 0:
        raise Qwen35ExecutionError("invalid_kv_position")
    prefix = layer_prefix(layer)
    epsilon = float(config["rms_norm_epsilon"])
    kind = config["layer_types"][layer]
    normalized = rms_norm_offset(hidden, tensors[prefix + "input_layernorm.weight"], epsilon)
    if kind == LINEAR_ATTENTION:
        mixed, state = gated_delta_net(normalized, tensors, prefix, config, past, linear)
    elif kind == FULL_ATTENTION:
        mixed, state = gated_attention(
            normalized, tensors, prefix, config, position, past, linear
        )
    else:
        raise Qwen35ExecutionError("unsupported_qwen35_layer_type")
    hidden = hidden + mixed.astype(hidden.dtype)
    normalized = rms_norm_offset(
        hidden, tensors[prefix + "post_attention_layernorm.weight"], epsilon
    )
    gate = linear(normalized, tensors[prefix + "mlp.gate_proj.weight"])
    up = linear(normalized, tensors[prefix + "mlp.up_proj.weight"])
    hidden = hidden + linear(_silu(gate) * up, tensors[prefix + "mlp.down_proj.weight"])
    return hidden, state
