#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
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

``qwen3_5_moe`` (Qwen3.6-35B-A3B class) replaces the dense SwiGLU MLP with
:func:`sparse_moe_block`: a float32 router, deterministic top-k
(:func:`route_top_k`, ties to the lowest expert index), renormalised weights,
packed 3-D experts executed per (token, expert) by ``gather_mm`` (or the
stage's packed-int8 ``gather_qmm`` kernel) and a sigmoid-gated shared expert.
Experts are never split across stages: a stage holds every expert of its
layers (pipeline-only sharding).
"""
from __future__ import annotations

import math
from typing import Any, Callable, Mapping

import mlx.core as mx

from weight_quantization import Int8RowwiseWeight, int8_float32_values

from qwen35_shapes import (
    EMBED_KEY as EMBED_KEY,
    FINAL_NORM_KEY as FINAL_NORM_KEY,
    FULL_ATTENTION,
    LINEAR_ATTENTION,
    TEXT_PREFIX as TEXT_PREFIX,
    expected_qwen35_shapes as expected_shapes,
    layer_prefix,
)

__all__ = [
    "EMBED_KEY",
    "FINAL_NORM_KEY",
    "TEXT_PREFIX",
    "decoder_layer",
    "expected_shapes",
    "expert_linear",
    "route_top_k",
    "sparse_moe_block",
]
_L2_EPSILON = 1e-6

Linear = Callable[[mx.array, Any], mx.array]
ExpertLinear = Callable[[mx.array, Any, mx.array], mx.array]


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


def route_top_k(probabilities: mx.array, top_k: int) -> tuple[mx.array, mx.array]:
    """Deterministic top-k over float32 router probabilities ``[N, E]``.

    Experts rank by probability, descending; equal probabilities rank the lower
    expert index first.  The selection never depends on a sort's stability or
    on reduction order:

    1. ``t`` = the k-th largest probability (the minimum of ``mx.topk``'s
       values -- a multiset, so exact);
    2. every expert with ``p > t`` is selected, then the lowest-index experts
       with ``p == t`` fill the remaining slots;
    3. the k selected experts are put in rank order by their pairwise rank
       (``#{j: p_j > p_i or (p_j == p_i and j < i)}``, a permutation of
       ``0..k-1``).

    Memory is ``O(N * E + N * k^2)``.  Returns the indices in rank order
    (``uint32``) and their float32 weights renormalised to sum to one
    (``norm_topk_prob``).  The routed sum is accumulated in this order.
    """

    compute = probabilities.astype(mx.float32)
    experts = int(compute.shape[-1])
    order = mx.arange(experts, dtype=mx.int32)
    threshold = mx.min(mx.topk(compute, top_k, axis=-1), axis=-1, keepdims=True)
    above = compute > threshold
    tied = compute == threshold
    missing = top_k - mx.sum(above.astype(mx.int32), axis=-1, keepdims=True)
    chosen = above | (tied & (mx.cumsum(tied.astype(mx.int32), axis=-1) <= missing))
    # Selected experts in ascending index order (unique sort keys).
    ascending = mx.argsort(mx.where(chosen, order, order + experts), axis=-1)[..., :top_k]
    picked = mx.take_along_axis(compute, ascending, axis=-1)
    mine = picked[..., :, None]
    other = picked[..., None, :]
    slot = mx.arange(top_k, dtype=mx.int32)
    beaten = (other > mine) | ((other == mine) & (slot[None, :] < slot[:, None]))
    rank = mx.sum(beaten.astype(mx.int32), axis=-1)
    by_rank = mx.argsort(rank, axis=-1)
    indices = mx.take_along_axis(ascending, by_rank, axis=-1)
    selected = mx.take_along_axis(picked, by_rank, axis=-1)
    weights = selected / mx.sum(selected, axis=-1, keepdims=True)
    return indices.astype(mx.uint32), weights


def expert_linear(hidden: mx.array, weight: Any, indices: mx.array) -> mx.array:
    """Per-token expert projections: ``hidden[n, s] @ weight[indices[n, k]].T``.

    ``hidden`` is ``[N, S, in]`` with ``S`` either 1 (same input for every
    selected expert) or ``top_k`` (one input per selected expert); ``weight``
    is a packed ``[E, out, in]`` tensor (float, or row-wise int8 with scales
    ``[E, out]``).  Every (token, expert) product is its own single-row
    product, so a token's result does not depend on how many tokens share the
    call.  Returns ``[N, top_k, out]``.
    """

    lhs = hidden[:, :, None, :]
    if isinstance(weight, Int8RowwiseWeight):
        product = mx.gather_mm(
            lhs, int8_float32_values(weight).swapaxes(-1, -2), rhs_indices=indices
        )
        scales = weight.scales[indices]
        return product[:, :, 0, :] * scales
    product = mx.gather_mm(lhs, weight.swapaxes(-1, -2), rhs_indices=indices)
    return product[:, :, 0, :]


def sparse_moe_block(
    normalized: mx.array,
    tensors: Mapping[str, Any],
    prefix: str,
    config: Mapping[str, Any],
    linear: Linear,
    expert_projection: ExpertLinear | None = None,
) -> mx.array:
    """Qwen3.5 sparse MoE MLP (``qwen3_5_moe``), float32 throughout.

    ``p = softmax(x Wg^T)`` in float32, :func:`route_top_k` selects
    ``num_experts_per_tok`` experts and renormalises their weights,
    ``y = sum_k w_k * down_k(silu(gate_k x) * up_k x)`` over the packed experts
    (gate rows first, then up rows, in ``gate_up_proj``), plus the shared
    expert scaled by ``sigmoid(x w_s^T)``.  The routed sum runs in rank order.
    """

    project = expert_projection or expert_linear
    batch, sequence, hidden = (int(extent) for extent in normalized.shape)
    tokens = normalized.reshape(batch * sequence, hidden).astype(mx.float32)
    logits = linear(tokens, tensors[prefix + "mlp.gate.weight"]).astype(mx.float32)
    indices, weights = route_top_k(
        mx.softmax(logits, axis=-1), int(config["num_experts_per_tok"])
    )
    intermediate = int(config["moe_intermediate_size"])
    projected = project(tokens[:, None, :], tensors[prefix + "mlp.experts.gate_up_proj"], indices)
    activated = _silu(projected[..., :intermediate]) * projected[..., intermediate:]
    routed = project(activated, tensors[prefix + "mlp.experts.down_proj"], indices)
    routed = mx.sum(routed * weights[..., None], axis=1)
    shared = linear(
        _silu(linear(tokens, tensors[prefix + "mlp.shared_expert.gate_proj.weight"]))
        * linear(tokens, tensors[prefix + "mlp.shared_expert.up_proj.weight"]),
        tensors[prefix + "mlp.shared_expert.down_proj.weight"],
    )
    shared_gate = mx.sigmoid(linear(tokens, tensors[prefix + "mlp.shared_expert_gate.weight"]))
    output = routed + shared_gate * shared
    return output.reshape(batch, sequence, hidden).astype(normalized.dtype)


def decoder_layer(
    hidden: mx.array,
    tensors: Mapping[str, Any],
    layer: int,
    config: Mapping[str, Any],
    position: int,
    past: tuple[mx.array, mx.array] | None,
    linear: Linear,
    expert_projection: ExpertLinear | None = None,
) -> tuple[mx.array, tuple[mx.array, mx.array]]:
    """One Qwen3.5 decoder layer; returns the residual stream and its state.

    A ``qwen3_5_moe`` config (``num_experts`` present) runs the sparse MoE MLP;
    ``expert_projection`` lets the stage supply its packed-int8 expert kernel.
    """

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
    if "num_experts" in config:
        mlp = sparse_moe_block(normalized, tensors, prefix, config, linear, expert_projection)
        return hidden + mlp.astype(hidden.dtype), state
    gate = linear(normalized, tensors[prefix + "mlp.gate_proj.weight"])
    up = linear(normalized, tensors[prefix + "mlp.up_proj.weight"])
    hidden = hidden + linear(_silu(gate) * up, tensors[prefix + "mlp.down_proj.weight"])
    return hidden, state
