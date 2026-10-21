# SPDX-License-Identifier: AGPL-3.0-only
"""Tiny random ``qwen3_5_moe`` checkpoint with the published tensor layout.

The real Qwen3.6-35B-A3B checkpoint (``Qwen3_5MoeForConditionalGeneration``)
is ~72 GB.  This module builds a deterministic stand-in with exactly its
layout so the MoE runtime can be proven without the weights:

* text decoder under ``model.language_model.``; untied ``lm_head.weight``;
* per layer a Gated DeltaNet (``linear_attention``) or gated full attention
  token mixer, chosen by ``layer_types`` (period 4: three linear, one full);
* the sparse MoE MLP: router ``mlp.gate.weight [E, H]``, packed 3-D experts
  ``mlp.experts.gate_up_proj [E, 2*mi, H]`` (gate rows first, then up rows)
  and ``mlp.experts.down_proj [E, H, mi]`` (no ``.weight`` suffix), a shared
  expert ``mlp.shared_expert.{gate,up,down}_proj.weight`` and its scalar gate
  ``mlp.shared_expert_gate.weight [1, H]``;
* vision-encoder and MTP tensors (including MTP packed experts) in the same
  file, which Mycelium must recognise and exclude;
* every tensor stored as BF16 (values are bf16-representable, so float32
  casts are exact on every consumer).

Dimensions are chosen so every int8 matrix width is a multiple of the packed
int8 group size (128): the persistent packed kernels run, not a fallback.

``python -m tests.model.qwen35_moe_tiny --write DIR`` writes ``DIR/bf16`` (the
checkpoint) and ``DIR/int8-dequant`` (float32 weights equal to Mycelium's
row-wise / per-expert int8 values times scales) for the external mlx-lm
reference run.  Never downloads anything.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

import numpy as np

SEED = 20261013
PREFIX = "model.language_model."
LAYER_TYPES = ["linear_attention"] * 3 + ["full_attention"]

TEXT_CONFIG = {
    "model_type": "qwen3_5_moe_text",
    "attention_bias": False,
    "attention_dropout": 0.0,
    "attn_output_gate": True,
    "dtype": "bfloat16",
    "eos_token_id": 127,
    "full_attention_interval": 4,
    "head_dim": 64,
    "hidden_act": "silu",
    "hidden_size": 128,
    "initializer_range": 0.02,
    "layer_types": list(LAYER_TYPES),
    "linear_conv_kernel_dim": 4,
    "linear_key_head_dim": 32,
    "linear_num_key_heads": 2,
    "linear_num_value_heads": 4,
    "linear_value_head_dim": 32,
    "mamba_ssm_dtype": "float32",
    "max_position_embeddings": 256,
    "moe_intermediate_size": 128,
    "mtp_num_hidden_layers": 1,
    "mtp_use_dedicated_embeddings": False,
    "num_attention_heads": 2,
    "num_experts": 8,
    "num_experts_per_tok": 2,
    "num_hidden_layers": 4,
    "num_key_value_heads": 1,
    "output_router_logits": False,
    "partial_rotary_factor": 0.25,
    "rms_norm_eps": 1e-6,
    "rope_parameters": {
        "mrope_interleaved": True,
        "mrope_section": [3, 3, 2],
        "partial_rotary_factor": 0.25,
        "rope_theta": 10_000_000,
        "rope_type": "default",
    },
    "router_aux_loss_coef": 0.001,
    "shared_expert_intermediate_size": 256,
    "tie_word_embeddings": False,
    "use_cache": True,
    "vocab_size": 128,
}
CONFIG = {
    "architectures": ["Qwen3_5MoeForConditionalGeneration"],
    "model_type": "qwen3_5_moe",
    "tie_word_embeddings": False,
    "text_config": TEXT_CONFIG,
    "vision_config": {"depth": 1, "hidden_size": 16, "model_type": "qwen3_5_moe"},
}
PROMPT = [3, 17, 5, 9, 22, 41, 77, 100, 12, 64]


def _bf16_round(value: np.ndarray) -> np.ndarray:
    """Round float32 values to the nearest bf16 (ties to even), kept as float32."""

    bits = np.ascontiguousarray(value, dtype=np.float32).view(np.uint32).astype(np.uint64)
    rounded = (bits + 0x7FFF + ((bits >> 16) & 1)) & 0xFFFF0000
    return rounded.astype(np.uint32).view(np.float32)


def tensors(seed: int = SEED) -> dict[str, np.ndarray]:
    """All checkpoint tensors (text, vision, MTP) as bf16-representable float32."""

    rng = np.random.default_rng(seed)
    t = TEXT_CONFIG
    hidden = t["hidden_size"]
    experts = t["num_experts"]
    mi = t["moe_intermediate_size"]
    si = t["shared_expert_intermediate_size"]
    head_dim = t["head_dim"]
    q_dim = t["num_attention_heads"] * head_dim
    kv_dim = t["num_key_value_heads"] * head_dim
    key_dim = t["linear_num_key_heads"] * t["linear_key_head_dim"]
    value_dim = t["linear_num_value_heads"] * t["linear_value_head_dim"]
    conv_dim = 2 * key_dim + value_dim
    hv = t["linear_num_value_heads"]
    vocab = t["vocab_size"]

    def matrix(shape: tuple[int, ...], gain: float = 0.9) -> np.ndarray:
        return rng.normal(0.0, gain / np.sqrt(shape[-1]), shape)

    out: dict[str, np.ndarray] = {
        PREFIX + "embed_tokens.weight": rng.normal(0.0, 0.6, (vocab, hidden)),
        PREFIX + "norm.weight": rng.normal(0.0, 0.05, (hidden,)),
        "lm_head.weight": matrix((vocab, hidden), 2.0),
        "model.visual.blocks.0.attn.qkv.weight": rng.normal(0.0, 0.1, (48, 16)),
        "model.visual.merger.linear_fc1.weight": rng.normal(0.0, 0.1, (16, 16)),
        "mtp.fc.weight": matrix((hidden, 2 * hidden)),
        "mtp.norm.weight": rng.normal(0.0, 0.05, (hidden,)),
        "mtp.layers.0.mlp.gate.weight": matrix((experts, hidden)),
        "mtp.layers.0.mlp.experts.gate_up_proj": matrix((experts, 2 * mi, hidden)),
        "mtp.layers.0.mlp.experts.down_proj": matrix((experts, hidden, mi)),
    }
    for layer, kind in enumerate(t["layer_types"]):
        p = f"{PREFIX}layers.{layer}."
        out[p + "input_layernorm.weight"] = rng.normal(0.0, 0.05, (hidden,))
        out[p + "post_attention_layernorm.weight"] = rng.normal(0.0, 0.05, (hidden,))
        if kind == "linear_attention":
            out[p + "linear_attn.in_proj_qkv.weight"] = matrix((conv_dim, hidden))
            out[p + "linear_attn.in_proj_z.weight"] = matrix((value_dim, hidden))
            out[p + "linear_attn.in_proj_b.weight"] = matrix((hv, hidden))
            out[p + "linear_attn.in_proj_a.weight"] = matrix((hv, hidden))
            out[p + "linear_attn.conv1d.weight"] = rng.normal(
                0.0, 0.4, (conv_dim, 1, t["linear_conv_kernel_dim"])
            )
            out[p + "linear_attn.dt_bias"] = rng.normal(0.0, 0.5, (hv,))
            out[p + "linear_attn.A_log"] = np.log(rng.uniform(1.0, 16.0, (hv,)))
            out[p + "linear_attn.norm.weight"] = rng.normal(
                1.0, 0.05, (t["linear_value_head_dim"],)
            )
            out[p + "linear_attn.out_proj.weight"] = matrix((hidden, value_dim))
        else:
            out[p + "self_attn.q_proj.weight"] = matrix((2 * q_dim, hidden))
            out[p + "self_attn.k_proj.weight"] = matrix((kv_dim, hidden))
            out[p + "self_attn.v_proj.weight"] = matrix((kv_dim, hidden))
            out[p + "self_attn.o_proj.weight"] = matrix((hidden, q_dim))
            out[p + "self_attn.q_norm.weight"] = rng.normal(0.0, 0.05, (head_dim,))
            out[p + "self_attn.k_norm.weight"] = rng.normal(0.0, 0.05, (head_dim,))
        # A wide router so the top-k choice is decisive on random weights.
        out[p + "mlp.gate.weight"] = matrix((experts, hidden), 3.0)
        out[p + "mlp.experts.gate_up_proj"] = matrix((experts, 2 * mi, hidden), 1.2)
        out[p + "mlp.experts.down_proj"] = matrix((experts, hidden, mi), 1.2)
        out[p + "mlp.shared_expert.gate_proj.weight"] = matrix((si, hidden))
        out[p + "mlp.shared_expert.up_proj.weight"] = matrix((si, hidden))
        out[p + "mlp.shared_expert.down_proj.weight"] = matrix((hidden, si))
        out[p + "mlp.shared_expert_gate.weight"] = matrix((1, hidden), 1.5)
    return {name: _bf16_round(value.astype(np.float32)) for name, value in out.items()}


def text_tensors() -> dict[str, np.ndarray]:
    return {
        name: value
        for name, value in tensors().items()
        if not name.startswith(("model.visual.", "mtp."))
    }


def tensor_digest(values: dict[str, np.ndarray]) -> str:
    """sha256 over (name, shape, float32 little-endian bytes), names sorted."""

    digest = hashlib.sha256()
    for name in sorted(values):
        array = np.ascontiguousarray(values[name], dtype="<f4")
        digest.update(json.dumps([name, list(array.shape)]).encode())
        digest.update(array.tobytes())
    return "sha256:" + digest.hexdigest()


def write_checkpoint(root: Path) -> Path:
    """Write ``config.json`` + BF16 ``model.safetensors`` (needs MLX)."""

    import mlx.core as mx

    root.mkdir(parents=True, exist_ok=True)
    (root / "config.json").write_text(json.dumps(CONFIG, indent=1), encoding="utf-8")
    mx.save_safetensors(
        str(root / "model.safetensors"),
        {name: mx.array(value).astype(mx.bfloat16) for name, value in tensors().items()},
        metadata={"format": "pt"},
    )
    return root


def int8_dequantized_text_tensors() -> dict[str, np.ndarray]:
    """Float32 tensors equal to Mycelium's int8-weight-only codes times scales."""

    from numpy_runtime import quantize_qwen2_numpy_tensor
    from weight_quantization import Int8RowwiseWeight

    dequantized: dict[str, np.ndarray] = {}
    for name, value in text_tensors().items():
        converted = quantize_qwen2_numpy_tensor(name, value)
        if isinstance(converted, Int8RowwiseWeight):
            dequantized[name] = (
                converted.values.astype(np.float32) * converted.scales[..., None]
            ).astype(np.float32)
        else:
            dequantized[name] = value
    return dequantized


def write_int8_dequantized_checkpoint(root: Path) -> Path:
    import mlx.core as mx

    root.mkdir(parents=True, exist_ok=True)
    (root / "config.json").write_text(json.dumps(CONFIG, indent=1), encoding="utf-8")
    mx.save_safetensors(
        str(root / "model.safetensors"),
        {name: mx.array(value) for name, value in int8_dequantized_text_tensors().items()},
        metadata={"format": "pt"},
    )
    return root


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", type=Path, required=True)
    args = parser.parse_args()
    write_checkpoint(args.write / "bf16")
    write_int8_dequantized_checkpoint(args.write / "int8-dequant")
    summary = {
        "text_tensor_digest": tensor_digest(text_tensors()),
        "int8_dequantized_digest": tensor_digest(int8_dequantized_text_tensors()),
        "prompt": PROMPT,
    }
    (args.write / "digests.json").write_text(json.dumps(summary, indent=1), encoding="utf-8")
    print(json.dumps(summary))


if __name__ == "__main__":
    main()
