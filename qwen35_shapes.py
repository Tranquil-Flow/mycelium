#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Pure (no MLX) Qwen3.5 hybrid text-decoder tensor naming and shapes."""
from __future__ import annotations

from typing import Any, Mapping

LINEAR_ATTENTION = "linear_attention"
FULL_ATTENTION = "full_attention"
TEXT_PREFIX = "model.language_model."
EMBED_KEY = TEXT_PREFIX + "embed_tokens.weight"
FINAL_NORM_KEY = TEXT_PREFIX + "norm.weight"


def layer_prefix(layer: int) -> str:
    return f"{TEXT_PREFIX}layers.{layer}."


def _moe_mlp_shapes(
    config: Mapping[str, Any], prefix: str, hidden: int
) -> dict[str, tuple[int, ...]]:
    """``qwen3_5_moe`` MLP: router, packed 3-D experts, shared expert + gate."""

    experts = int(config["num_experts"])
    moe_inner = int(config["moe_intermediate_size"])
    shared_inner = int(config["shared_expert_intermediate_size"])
    return {
        prefix + "mlp.gate.weight": (experts, hidden),
        # gate rows [0, mi) then up rows [mi, 2*mi) of every expert.
        prefix + "mlp.experts.gate_up_proj": (experts, 2 * moe_inner, hidden),
        prefix + "mlp.experts.down_proj": (experts, hidden, moe_inner),
        prefix + "mlp.shared_expert.gate_proj.weight": (shared_inner, hidden),
        prefix + "mlp.shared_expert.up_proj.weight": (shared_inner, hidden),
        prefix + "mlp.shared_expert.down_proj.weight": (hidden, shared_inner),
        prefix + "mlp.shared_expert_gate.weight": (1, hidden),
    }


def expected_qwen35_shapes(
    config: Mapping[str, Any], start: int, end: int, components: list[str] | tuple[str, ...]
) -> dict[str, tuple[int, ...]]:
    """Exact tensor shapes one Qwen3.5 / Qwen3.5-MoE stage must hold (head key excluded)."""

    hidden = int(config["n_embd"])
    inner = int(config["n_inner"])
    head_dim = int(config["head_dim"])
    q_dim = int(config["n_head"]) * head_dim
    kv_dim = int(config["n_kv_head"]) * head_dim
    hk, dk = int(config["linear_num_key_heads"]), int(config["linear_key_head_dim"])
    hv, dv = int(config["linear_num_value_heads"]), int(config["linear_value_head_dim"])
    kernel = int(config["linear_conv_kernel_dim"])
    conv_dim = 2 * hk * dk + hv * dv
    shapes: dict[str, tuple[int, ...]] = {}
    if "input_embedding" in components:
        shapes[EMBED_KEY] = (int(config["vocab_size"]), hidden)
    for layer in range(start, end):
        prefix = layer_prefix(layer)
        shapes.update(
            {
                prefix + "input_layernorm.weight": (hidden,),
                prefix + "post_attention_layernorm.weight": (hidden,),
            }
        )
        if "num_experts" in config:
            shapes.update(_moe_mlp_shapes(config, prefix, hidden))
        else:
            shapes.update(
                {
                    prefix + "mlp.gate_proj.weight": (inner, hidden),
                    prefix + "mlp.up_proj.weight": (inner, hidden),
                    prefix + "mlp.down_proj.weight": (hidden, inner),
                }
            )
        if config["layer_types"][layer] == LINEAR_ATTENTION:
            shapes.update(
                {
                    prefix + "linear_attn.in_proj_qkv.weight": (conv_dim, hidden),
                    prefix + "linear_attn.in_proj_z.weight": (hv * dv, hidden),
                    prefix + "linear_attn.in_proj_b.weight": (hv, hidden),
                    prefix + "linear_attn.in_proj_a.weight": (hv, hidden),
                    prefix + "linear_attn.conv1d.weight": (conv_dim, 1, kernel),
                    prefix + "linear_attn.dt_bias": (hv,),
                    prefix + "linear_attn.A_log": (hv,),
                    prefix + "linear_attn.norm.weight": (dv,),
                    prefix + "linear_attn.out_proj.weight": (hidden, hv * dv),
                }
            )
        else:
            shapes.update(
                {
                    prefix + "self_attn.q_proj.weight": (2 * q_dim, hidden),
                    prefix + "self_attn.k_proj.weight": (kv_dim, hidden),
                    prefix + "self_attn.v_proj.weight": (kv_dim, hidden),
                    prefix + "self_attn.o_proj.weight": (hidden, q_dim),
                    prefix + "self_attn.q_norm.weight": (head_dim,),
                    prefix + "self_attn.k_norm.weight": (head_dim,),
                }
            )
    if "final_norm" in components:
        shapes[FINAL_NORM_KEY] = (hidden,)
    return shapes


def qwen35_stage_resident_bytes(
    config: Mapping[str, Any],
    start: int,
    end: int,
    components: list[str] | tuple[str, ...],
    quantization: str,
) -> int:
    """Resident weight bytes of one stage with this repo's loader rules.

    ``none``: every tensor float32.  ``int8-weight-only``: 2-D ``.weight``
    matrices and packed 3-D experts are int8 codes plus one float32 scale per
    output row (per expert for packed experts), held once in the packed MLX
    layout (plus its float32 group scale and bias per 128 codes, see
    ``weight_quantization.int8_rowwise_resident_bytes``); norms, conv1d,
    ``A_log``, ``dt_bias`` and the MoE routing matrices stay float32.  An
    untied ``lm_head`` counts as a matrix of the embedding's shape.
    """

    from weight_quantization import int8_rowwise_resident_bytes, rowwise_int8_kind

    if quantization not in {"none", "int8-weight-only"}:
        raise ValueError("unsupported quantization")
    shapes = expected_qwen35_shapes(config, start, end, components)
    if "lm_head" in components:
        head_shape = (int(config["vocab_size"]), int(config["n_embd"]))
        if not config["tie_word_embeddings"]:
            shapes["lm_head.weight"] = head_shape
        else:
            # A tied head is the embedding matrix itself: the stage loads it
            # (once, shared with ``input_embedding`` when both are owned).
            shapes.setdefault(EMBED_KEY, head_shape)
    total = 0
    for key, shape in shapes.items():
        elements = 1
        for extent in shape:
            elements *= extent
        if quantization == "int8-weight-only" and rowwise_int8_kind(key, len(shape)):
            total += int8_rowwise_resident_bytes(tuple(shape))
        else:
            total += 4 * elements
    return total
