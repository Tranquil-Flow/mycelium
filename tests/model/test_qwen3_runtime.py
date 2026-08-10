"""M17: Qwen3 dense execution agrees across monolithic, MLX, and KV paths."""

from __future__ import annotations

import mlx.core as mx
import numpy as np

from model_manifest import compile_model_manifest, verify_manifest_digest
from mycelium_router.mlx_runtime import (
    _qwen2_block_with_kv,
    _qwen2_embedding,
    _qwen2_linear as _cached_qwen2_linear,
    _rms_norm,
)
from numpy_runtime import NumpyQwen3Runtime
from runtime_contracts import normalize_qwen3_model_config
from runtime_loader import _qwen2_block, _qwen2_linear
from runtime_loader import _quantize_qwen2_mlx_tensors


def _runtime(backend: str, quantization: str = "none") -> dict[str, object]:
    return {
        "backend": backend,
        "dtype": "float32",
        "quantization": quantization,
        "architecture": "qwen3",
        "model_config": {
            "n_layer": 2,
            "n_embd": 8,
            "n_head": 2,
            "n_kv_head": 1,
            "n_inner": 16,
            "vocab_size": 32,
            "n_positions": 64,
            "rms_norm_epsilon": 1e-6,
            "rope_theta": 1_000_000.0,
            "head_dim": 4,
            "activation_function": "silu",
            "tie_word_embeddings": True,
        },
    }


def _tensors() -> dict[str, np.ndarray]:
    rng = np.random.default_rng(20260810)
    tensors: dict[str, np.ndarray] = {
        "model.embed_tokens.weight": rng.normal(0.0, 0.05, (32, 8)).astype(
            np.float32
        ),
        "model.norm.weight": rng.normal(1.0, 0.02, (8,)).astype(np.float32),
    }
    shapes = {
        "input_layernorm.weight": (8,),
        "self_attn.q_proj.weight": (8, 8),
        "self_attn.q_norm.weight": (4,),
        "self_attn.k_proj.weight": (4, 8),
        "self_attn.k_norm.weight": (4,),
        "self_attn.v_proj.weight": (4, 8),
        "self_attn.o_proj.weight": (8, 8),
        "post_attention_layernorm.weight": (8,),
        "mlp.gate_proj.weight": (16, 8),
        "mlp.up_proj.weight": (16, 8),
        "mlp.down_proj.weight": (8, 16),
    }
    for layer in range(2):
        for suffix, shape in shapes.items():
            if suffix.endswith("norm.weight") or suffix.endswith("layernorm.weight"):
                value = rng.normal(1.0, 0.02, shape)
            else:
                value = rng.normal(0.0, 0.04, shape)
            tensors[f"model.layers.{layer}.{suffix}"] = value.astype(np.float32)
    return tensors


def test_qwen3_normalizes_real_dense_config_contract() -> None:
    normalized = normalize_qwen3_model_config(
        {
            "model_type": "qwen3",
            "num_hidden_layers": 36,
            "hidden_size": 4096,
            "num_attention_heads": 32,
            "num_key_value_heads": 8,
            "intermediate_size": 12288,
            "vocab_size": 151936,
            "max_position_embeddings": 40960,
            "rms_norm_eps": 1e-6,
            "rope_theta": 1_000_000,
            "head_dim": 128,
            "hidden_act": "silu",
            "tie_word_embeddings": False,
            "attention_bias": False,
            "attention_dropout": 0.0,
            "rope_scaling": None,
        },
        expected_layers=36,
    )

    assert normalized["n_layer"] == 36
    assert normalized["n_embd"] == 4096
    assert normalized["n_kv_head"] == 8
    assert normalized["head_dim"] == 128


def test_qwen3_manifest_owns_exact_bias_free_decoder_tensors() -> None:
    suffixes = (
        "input_layernorm.weight",
        "self_attn.q_proj.weight",
        "self_attn.q_norm.weight",
        "self_attn.k_proj.weight",
        "self_attn.k_norm.weight",
        "self_attn.v_proj.weight",
        "self_attn.o_proj.weight",
        "post_attention_layernorm.weight",
        "mlp.gate_proj.weight",
        "mlp.up_proj.weight",
        "mlp.down_proj.weight",
    )
    weight_map = {
        "model.embed_tokens.weight": "model.safetensors",
        "model.norm.weight": "model.safetensors",
        "lm_head.weight": "model.safetensors",
        **{
            f"model.layers.0.{suffix}": "model.safetensors"
            for suffix in suffixes
        },
    }
    manifest = compile_model_manifest(
        model_id="Qwen/Qwen3-8B",
        requested_revision="main",
        resolved_commit="b" * 40,
        config={
            "model_type": "qwen3",
            "architectures": ["Qwen3ForCausalLM"],
            "num_hidden_layers": 1,
            "hidden_size": 8,
            "num_attention_heads": 2,
            "num_key_value_heads": 1,
            "intermediate_size": 16,
            "vocab_size": 32,
            "max_position_embeddings": 64,
            "rms_norm_eps": 1e-6,
            "rope_theta": 1_000_000,
            "head_dim": 4,
            "hidden_act": "silu",
            "tie_word_embeddings": False,
            "attention_bias": False,
            "attention_dropout": 0.0,
            "rope_scaling": None,
        },
        checkpoint_index={"weight_map": weight_map},
        file_metadata={
            "model.safetensors": {
                "size_bytes": 1234,
                "sha256": "a" * 64,
                "source_etag": None,
            }
        },
    )

    assert verify_manifest_digest(manifest)
    assert manifest["runtime_model"]["architecture"] == "qwen3"
    assert manifest["tensor_keys_by_layer"]["0"] == sorted(
        f"model.layers.0.{suffix}" for suffix in suffixes
    )


def test_qwen3_monolithic_mlx_numpy_parity() -> None:
    tensors = _tensors()
    ids = np.array([[1, 7, 11, 5]], dtype=np.int64)
    expected = NumpyQwen3Runtime(
        runtime=_runtime("numpy"), tensors=tensors
    ).forward_token_ids(ids)

    mlx_tensors = {name: mx.array(value) for name, value in tensors.items()}
    config = _runtime("mlx")["model_config"]
    hidden = mlx_tensors["model.embed_tokens.weight"][mx.array(ids)]
    for layer in range(2):
        hidden = _qwen2_block(
            hidden,
            mlx_tensors,
            f"model.layers.{layer}.",
            config,
            mx,
            "qwen3",
        )
    hidden = _rms_norm(
        hidden,
        mlx_tensors["model.norm.weight"],
        float(config["rms_norm_epsilon"]),
    )
    actual = _qwen2_linear(hidden, mlx_tensors["model.embed_tokens.weight"], mx)
    mx.eval(actual)

    np.testing.assert_allclose(np.asarray(actual), expected, rtol=2e-5, atol=2e-6)
    assert int(mx.argmax(actual[0, -1]).item()) == int(np.argmax(expected[0, -1]))


def test_qwen3_int8_weight_only_mlx_numpy_parity() -> None:
    tensors = _tensors()
    ids = np.array([[1, 7, 11, 5]], dtype=np.int64)
    expected = NumpyQwen3Runtime(
        runtime=_runtime("numpy", "int8-weight-only"), tensors=tensors
    ).forward_token_ids(ids)
    mlx_tensors = _quantize_qwen2_mlx_tensors(
        {name: mx.array(value) for name, value in tensors.items()}, mx
    )
    config = _runtime("mlx", "int8-weight-only")["model_config"]
    embedding = mlx_tensors["model.embed_tokens.weight"]
    hidden = embedding.values[mx.array(ids)].astype(mx.float32)
    hidden = hidden * embedding.scales[mx.array(ids), None]
    for layer in range(2):
        hidden = _qwen2_block(
            hidden, mlx_tensors, f"model.layers.{layer}.", config, mx, "qwen3"
        )
    hidden = _rms_norm(
        hidden,
        mlx_tensors["model.norm.weight"],
        float(config["rms_norm_epsilon"]),
    )
    actual = _qwen2_linear(hidden, embedding, mx)
    mx.eval(actual)

    np.testing.assert_allclose(np.asarray(actual), expected, rtol=5e-3, atol=3e-3)
    assert int(mx.argmax(actual[0, -1]).item()) == int(np.argmax(expected[0, -1]))


def test_qwen3_stage_local_kv_matches_complete_context() -> None:
    config = _runtime("mlx")["model_config"]
    tensors = {name: mx.array(value) for name, value in _tensors().items()}
    prompt = (1, 7, 11, 5)
    next_token = 9

    reference = _qwen2_embedding(
        tensors["model.embed_tokens.weight"],
        mx.array((prompt + (next_token,),), dtype=mx.uint32),
    )
    for layer in range(2):
        reference = _qwen2_block(
            reference,
            tensors,
            f"model.layers.{layer}.",
            config,
            mx,
            "qwen3",
        )
    reference = _rms_norm(
        reference,
        tensors["model.norm.weight"],
        float(config["rms_norm_epsilon"]),
    )
    reference = _cached_qwen2_linear(
        reference[:, -1:, :], tensors["model.embed_tokens.weight"]
    )

    cached = _qwen2_embedding(
        tensors["model.embed_tokens.weight"],
        mx.array((prompt,), dtype=mx.uint32),
    )
    cache: dict[int, tuple[mx.array, mx.array]] = {}
    for layer in range(2):
        cached, cache[layer] = _qwen2_block_with_kv(
            cached,
            tensors,
            f"model.layers.{layer}.",
            config,
            0,
            None,
            "qwen3",
        )
    cached = _qwen2_embedding(
        tensors["model.embed_tokens.weight"],
        mx.array(((next_token,),), dtype=mx.uint32),
    )
    for layer in range(2):
        cached, _ = _qwen2_block_with_kv(
            cached,
            tensors,
            f"model.layers.{layer}.",
            config,
            len(prompt),
            cache[layer],
            "qwen3",
        )
    cached = _rms_norm(
        cached,
        tensors["model.norm.weight"],
        float(config["rms_norm_epsilon"]),
    )
    cached = _cached_qwen2_linear(cached, tensors["model.embed_tokens.weight"])
    mx.eval(reference, cached)

    np.testing.assert_allclose(
        np.asarray(cached), np.asarray(reference), rtol=2e-5, atol=2e-6
    )
    assert int(mx.argmax(cached[0, -1]).item()) == int(
        mx.argmax(reference[0, -1]).item()
    )
