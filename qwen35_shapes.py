#!/usr/bin/env python3
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


def expected_qwen35_shapes(
    config: Mapping[str, Any], start: int, end: int, components: list[str] | tuple[str, ...]
) -> dict[str, tuple[int, ...]]:
    """Exact tensor shapes one Qwen3.5 stage must hold (head key excluded)."""

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
