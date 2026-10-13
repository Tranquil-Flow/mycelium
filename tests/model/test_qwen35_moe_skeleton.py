"""qwen3_5_moe (Qwen3.6-35B-A3B class) — adapter skeleton and RED placeholders.

The MoE text decoder is the Qwen3.5 hybrid with the dense MLP replaced by a
256-way top-8 router, packed 3-D expert tensors and a sigmoid-gated shared
expert.  This lane ships only the manifest-level adapter (recognising every
tensor, excluding vision/MTP) and RED tests for the runtime work, marked
``xfail(strict=True)`` so they flip loudly when the implementation lands.
Nothing here claims MoE execution.
"""

from __future__ import annotations

import pytest

from model_adapters import ADAPTERS, adapter_for_config, adapter_for_runtime
from model_manifest import compile_model_manifest

COMMIT = "e" * 40
_LAYER_TYPES = ["linear_attention"] * 3 + ["full_attention"]
_TEXT_35B = {
    "model_type": "qwen3_5_moe_text",
    "attention_bias": False,
    "attention_dropout": 0.0,
    "attn_output_gate": True,
    "full_attention_interval": 4,
    "head_dim": 256,
    "hidden_act": "silu",
    "hidden_size": 2048,
    "layer_types": _LAYER_TYPES * 10,
    "linear_conv_kernel_dim": 4,
    "linear_key_head_dim": 128,
    "linear_num_key_heads": 16,
    "linear_num_value_heads": 32,
    "linear_value_head_dim": 128,
    "max_position_embeddings": 262144,
    "mlp_only_layers": [],
    "moe_intermediate_size": 512,
    "num_attention_heads": 16,
    "num_experts": 256,
    "num_experts_per_tok": 8,
    "num_hidden_layers": 40,
    "num_key_value_heads": 2,
    "rms_norm_eps": 1e-6,
    "shared_expert_intermediate_size": 512,
    "vocab_size": 248320,
    "mamba_ssm_dtype": "float32",
    "rope_parameters": {
        "mrope_interleaved": True,
        "mrope_section": [11, 11, 10],
        "rope_type": "default",
        "rope_theta": 10000000,
        "partial_rotary_factor": 0.25,
    },
}


def _config(layers: int = 40) -> dict:
    text = {**_TEXT_35B, "num_hidden_layers": layers, "layer_types": (_LAYER_TYPES * 10)[:layers]}
    return {
        "architectures": ["Qwen3_5MoeForConditionalGeneration"],
        "model_type": "qwen3_5_moe",
        "tie_word_embeddings": False,
        "text_config": text,
    }


def _weight_map(layers: int) -> dict[str, str]:
    adapter = ADAPTERS["qwen3_5_moe"]
    keys = [
        "model.language_model.embed_tokens.weight",
        "model.language_model.norm.weight",
        "lm_head.weight",
        "model.visual.blocks.0.attn.qkv.weight",
        "mtp.fc.weight",
        "mtp.layers.0.mlp.experts.0.down_proj.weight",
        "mtp.layers.0.mlp.experts.255.gate_proj.weight",
    ]
    for layer in range(layers):
        prefix = f"model.language_model.layers.{layer}."
        kind = _LAYER_TYPES[layer % 4]
        keys += [prefix + suffix for suffix in adapter.decoder_suffixes_for_layer(kind)]
    return {key: "model.safetensors" for key in keys}


def test_qwen35_moe_adapter_is_a_manifest_only_skeleton() -> None:
    adapter = adapter_for_config(_config())

    assert adapter.architecture == "qwen3_5_moe"
    assert adapter.runtime_supported is False
    assert "mlp.experts.gate_up_proj" in adapter.decoder_tensor_suffixes
    assert "mlp.shared_expert_gate.weight" in adapter.decoder_tensor_suffixes
    with pytest.raises(ValueError, match="runtime adapter unavailable"):
        adapter_for_runtime("qwen3_5_moe")


def test_qwen35_moe_manifest_recognises_packed_experts_and_excludes_mtp() -> None:
    manifest = compile_model_manifest(
        model_id="Qwen/Qwen3.5-35B-A3B",
        requested_revision="main",
        resolved_commit=COMMIT,
        config=_config(8),
        checkpoint_index={"weight_map": _weight_map(8)},
        file_metadata={
            "model.safetensors": {"size_bytes": 1234, "sha256": "a" * 64, "source_etag": None}
        },
    )

    assert manifest["architecture"] == "qwen3_5_moe"
    assert "runtime_model" not in manifest  # no executable contract yet
    assert "mtp.layers.0.mlp.experts.255.gate_proj.weight" in manifest["excluded_tensor_keys"]
    assert "model.language_model.layers.0.mlp.experts.down_proj" in (
        manifest["tensor_keys_by_layer"]["0"]
    )


@pytest.mark.xfail(
    strict=True,
    reason="qwen3_5_moe runtime contract not implemented: router/expert config "
    "normalization (256 experts, top-8, norm_topk_prob, shared expert) is design-only",
)
def test_qwen35_moe_runtime_contract_normalizes_35b_a3b() -> None:
    from runtime_contracts import normalize_qwen35_moe_model_config  # noqa: F401

    normalized = normalize_qwen35_moe_model_config(_config(), expected_layers=40)
    assert normalized["num_experts"] == 256
    assert normalized["num_experts_per_tok"] == 8


@pytest.mark.xfail(
    strict=True,
    reason="no MLX sparse-MoE block: float32 router softmax, top-8 with lowest-index "
    "tie-break, renormalized weights, packed [E, 2*mi, H] experts, sigmoid shared expert",
)
def test_qwen35_moe_mlx_block_exists() -> None:
    import qwen35_mlx

    assert callable(qwen35_mlx.sparse_moe_block)


@pytest.mark.xfail(
    strict=True,
    reason="qwen3_5_moe is not runtime-supported: no adapter backends, no loader "
    "shape table, no per-expert row-wise int8 for 3-D packed expert tensors",
)
def test_qwen35_moe_is_runtime_supported_on_mlx() -> None:
    assert ADAPTERS["qwen3_5_moe"].runtime_backends == ("mlx",)
