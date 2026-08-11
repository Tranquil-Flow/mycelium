"""M5: architecture-selected Qwen2 execution agrees on MLX and NumPy."""

from __future__ import annotations

import mlx.core as mx
import numpy as np
import pytest

from mycelium_router.mlx_runtime import (
    _qwen2_block_with_kv,
    _qwen2_embedding as _cached_qwen2_embedding,
    _qwen2_linear as _cached_qwen2_linear,
    _rms_norm as _cached_rms_norm,
)
from numpy_runtime import (
    NumpyQwen2Runtime,
    _qwen2_block as _numpy_qwen2_block,
    _qwen2_block_with_kv as _numpy_qwen2_block_with_kv,
    _qwen2_embedding as _numpy_qwen2_embedding,
    _qwen2_linear as _numpy_qwen2_linear,
    _rms_norm as _numpy_rms_norm,
    quantize_qwen2_numpy_tensors,
)
from runtime_loader import (
    _digest_probe_output,
    _qwen2_block,
    _qwen2_linear,
    _quantize_qwen2_mlx_tensors,
    _rms_norm,
)


def _runtime(backend: str, quantization: str = "none") -> dict[str, object]:
    return {
        "backend": backend,
        "dtype": "float32",
        "quantization": quantization,
        "architecture": "qwen2",
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
    rng = np.random.default_rng(20260809)
    tensors: dict[str, np.ndarray] = {
        "model.embed_tokens.weight": rng.normal(0.0, 0.05, (32, 8)).astype(
            np.float32
        ),
        "model.norm.weight": rng.normal(1.0, 0.02, (8,)).astype(np.float32),
    }
    shapes = {
        "input_layernorm.weight": (8,),
        "self_attn.q_proj.weight": (8, 8),
        "self_attn.q_proj.bias": (8,),
        "self_attn.k_proj.weight": (4, 8),
        "self_attn.k_proj.bias": (4,),
        "self_attn.v_proj.weight": (4, 8),
        "self_attn.v_proj.bias": (4,),
        "self_attn.o_proj.weight": (8, 8),
        "post_attention_layernorm.weight": (8,),
        "mlp.gate_proj.weight": (16, 8),
        "mlp.up_proj.weight": (16, 8),
        "mlp.down_proj.weight": (8, 16),
    }
    for layer in range(2):
        for suffix, shape in shapes.items():
            if suffix.endswith("layernorm.weight"):
                value = rng.normal(1.0, 0.02, shape)
            elif suffix.endswith(".bias"):
                value = rng.normal(0.0, 0.005, shape)
            else:
                value = rng.normal(0.0, 0.04, shape)
            tensors[f"model.layers.{layer}.{suffix}"] = value.astype(np.float32)
    return tensors


def test_qwen2_monolithic_mlx_numpy_parity() -> None:
    tensors = _tensors()
    ids = np.array([[1, 7, 11, 5]], dtype=np.int64)
    expected = NumpyQwen2Runtime(
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
        )
    hidden = _rms_norm(
        hidden,
        mlx_tensors["model.norm.weight"],
        float(config["rms_norm_epsilon"]),
        mx,
    )
    actual = mx.matmul(
        hidden, mlx_tensors["model.embed_tokens.weight"].transpose(1, 0)
    )
    mx.eval(actual)

    np.testing.assert_allclose(np.asarray(actual), expected, rtol=2e-5, atol=2e-6)
    assert int(mx.argmax(actual[0, -1]).item()) == int(
        np.argmax(expected[0, -1])
    )


def test_qwen2_int8_weight_only_mlx_numpy_parity() -> None:
    tensors = _tensors()
    ids = np.array([[1, 7, 11, 5]], dtype=np.int64)
    expected = NumpyQwen2Runtime(
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
            hidden,
            mlx_tensors,
            f"model.layers.{layer}.",
            config,
            mx,
        )
    hidden = _rms_norm(
        hidden,
        mlx_tensors["model.norm.weight"],
        float(config["rms_norm_epsilon"]),
        mx,
    )
    actual = _qwen2_linear(hidden, embedding, mx)
    mx.eval(actual)

    np.testing.assert_allclose(np.asarray(actual), expected, rtol=5e-3, atol=3e-3)
    assert int(mx.argmax(actual[0, -1]).item()) == int(
        np.argmax(expected[0, -1])
    )


def test_qwen2_numpy_probe_digest_binds_ranking_not_float_roundoff() -> None:
    runtime = _runtime("numpy")
    probe = np.arange(32, dtype=np.float32).reshape(1, 1, 32)
    perturbed = probe.copy()
    perturbed[..., :24] += np.float32(1e-5)

    expected = _digest_probe_output(probe, runtime)
    assert _digest_probe_output(perturbed, runtime) == expected

    perturbed[..., 23] = 100.0
    assert _digest_probe_output(perturbed, runtime) != expected


@pytest.mark.parametrize("quantization", ["none", "int8-weight-only"])
def test_qwen2_stage_local_kv_decode_matches_complete_context(
    quantization: str,
) -> None:
    runtime = _runtime("mlx", quantization)
    config = runtime["model_config"]
    tensors = {name: mx.array(value) for name, value in _tensors().items()}
    if quantization == "int8-weight-only":
        tensors = _quantize_qwen2_mlx_tensors(tensors, mx)
    prompt = (1, 7, 11, 5)
    next_token = 9

    reference = _cached_qwen2_embedding(
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
        )
    reference = _rms_norm(
        reference,
        tensors["model.norm.weight"],
        float(config["rms_norm_epsilon"]),
        mx,
    )
    reference = _qwen2_linear(
        reference[:, -1:, :],
        tensors["model.embed_tokens.weight"],
        mx,
    )

    cached = _cached_qwen2_embedding(
        tensors["model.embed_tokens.weight"],
        mx.array((prompt,), dtype=mx.uint32),
    )
    layer_cache = {}
    for layer in range(2):
        cached, layer_cache[layer] = _qwen2_block_with_kv(
            cached,
            tensors,
            f"model.layers.{layer}.",
            config,
            0,
            None,
        )
    cached = _cached_qwen2_embedding(
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
            layer_cache[layer],
        )
    cached = _cached_rms_norm(
        cached,
        tensors["model.norm.weight"],
        float(config["rms_norm_epsilon"]),
    )
    cached = _cached_qwen2_linear(
        cached,
        tensors["model.embed_tokens.weight"],
    )
    mx.eval(reference, cached)

    tolerance = 3e-3 if quantization == "int8-weight-only" else 2e-6
    np.testing.assert_allclose(
        np.asarray(cached),
        np.asarray(reference),
        rtol=5e-3 if quantization == "int8-weight-only" else 2e-5,
        atol=tolerance,
    )
    assert int(mx.argmax(cached[0, -1]).item()) == int(
        mx.argmax(reference[0, -1]).item()
    )


@pytest.mark.parametrize("quantization", ["none", "int8-weight-only"])
def test_qwen2_numpy_stage_local_kv_matches_complete_context(
    quantization: str,
) -> None:
    config = _runtime("numpy", quantization)["model_config"]
    tensors = _tensors()
    if quantization == "int8-weight-only":
        tensors = quantize_qwen2_numpy_tensors(tensors)
    prompt = (1, 7, 11, 5)
    next_token = 9

    reference = _numpy_qwen2_embedding(
        tensors["model.embed_tokens.weight"],
        np.array((prompt + (next_token,),), dtype=np.int64),
    )
    for layer in range(2):
        reference = _numpy_qwen2_block(
            reference,
            tensors,
            f"model.layers.{layer}.",
            config,
        )
    reference = _numpy_rms_norm(
        reference,
        tensors["model.norm.weight"],
        float(config["rms_norm_epsilon"]),
    )
    reference = _numpy_qwen2_linear(
        reference[:, -1:, :], tensors["model.embed_tokens.weight"]
    )

    cached = _numpy_qwen2_embedding(
        tensors["model.embed_tokens.weight"],
        np.array((prompt,), dtype=np.int64),
    )
    layer_cache = {}
    for layer in range(2):
        cached, layer_cache[layer] = _numpy_qwen2_block_with_kv(
            cached,
            tensors,
            f"model.layers.{layer}.",
            config,
            0,
            None,
        )
    cached = _numpy_qwen2_embedding(
        tensors["model.embed_tokens.weight"],
        np.array(((next_token,),), dtype=np.int64),
    )
    for layer in range(2):
        cached, _ = _numpy_qwen2_block_with_kv(
            cached,
            tensors,
            f"model.layers.{layer}.",
            config,
            len(prompt),
            layer_cache[layer],
        )
    cached = _numpy_rms_norm(
        cached,
        tensors["model.norm.weight"],
        float(config["rms_norm_epsilon"]),
    )
    cached = _numpy_qwen2_linear(cached, tensors["model.embed_tokens.weight"])

    tolerance = 3e-3 if quantization == "int8-weight-only" else 2e-6
    np.testing.assert_allclose(
        cached,
        reference,
        rtol=5e-3 if quantization == "int8-weight-only" else 2e-5,
        atol=tolerance,
    )
    assert int(np.argmax(cached[0, -1])) == int(np.argmax(reference[0, -1]))
