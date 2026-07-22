from __future__ import annotations

import copy
import math
from typing import Any

import mlx.core as mx
import numpy as np
import pytest

from numpy_runtime import NumpyGPT2Runtime, NumpyRuntimeError
from runtime_contracts import (
    MonolithicRuntimePort,
    StageRuntimeBackend,
    validate_normalized_mlx_runtime,
    validate_normalized_numpy_runtime,
    validate_normalized_runtime,
)
from runtime_loader import MLXStageBackend, _gpt2_block, _layer_norm


def _runtime(backend: str) -> dict[str, Any]:
    return {
        "backend": backend,
        "dtype": "float32",
        "quantization": "none",
        "architecture": "gpt2",
        "model_config": {
            "n_layer": 2,
            "n_embd": 8,
            "n_head": 2,
            "n_inner": 16,
            "vocab_size": 17,
            "n_positions": 12,
            "layer_norm_epsilon": 1e-5,
            "activation_function": "gelu_new",
            "scale_attn_weights": True,
            "scale_attn_by_inverse_layer_idx": False,
            "reorder_and_upcast_attn": False,
            "add_cross_attention": False,
        },
    }


def _weights(seed: int = 20260722) -> dict[str, np.ndarray]:
    rng = np.random.default_rng(seed)

    def normal(shape: tuple[int, ...], scale: float = 0.025) -> np.ndarray:
        return rng.normal(0.0, scale, size=shape).astype(np.float32)

    tensors: dict[str, np.ndarray] = {
        "transformer.wte.weight": normal((17, 8)),
        "transformer.wpe.weight": normal((12, 8)),
        "transformer.ln_f.weight": (
            np.ones((8,), dtype=np.float32) + normal((8,), 0.01)
        ),
        "transformer.ln_f.bias": normal((8,), 0.01),
    }
    for layer in range(2):
        prefix = f"transformer.h.{layer}."
        tensors.update(
            {
                prefix + "ln_1.weight": (
                    np.ones((8,), dtype=np.float32) + normal((8,), 0.01)
                ),
                prefix + "ln_1.bias": normal((8,), 0.01),
                prefix + "attn.c_attn.weight": normal((8, 24)),
                prefix + "attn.c_attn.bias": normal((24,), 0.01),
                prefix + "attn.c_proj.weight": normal((8, 8)),
                prefix + "attn.c_proj.bias": normal((8,), 0.01),
                prefix + "ln_2.weight": (
                    np.ones((8,), dtype=np.float32) + normal((8,), 0.01)
                ),
                prefix + "ln_2.bias": normal((8,), 0.01),
                prefix + "mlp.c_fc.weight": normal((8, 16)),
                prefix + "mlp.c_fc.bias": normal((16,), 0.01),
                prefix + "mlp.c_proj.weight": normal((16, 8)),
                prefix + "mlp.c_proj.bias": normal((8,), 0.01),
            }
        )
    return tensors


def _mlx_logits(weights: dict[str, np.ndarray], token_ids: np.ndarray) -> np.ndarray:
    tensors = {key: mx.array(value) for key, value in weights.items()}
    ids = mx.array(token_ids, dtype=mx.int32)
    positions = mx.arange(token_ids.shape[1], dtype=mx.int32)
    hidden = (
        tensors["transformer.wte.weight"][ids]
        + tensors["transformer.wpe.weight"][positions]
    )
    for layer in range(2):
        hidden = _gpt2_block(
            hidden,
            tensors,
            f"transformer.h.{layer}.",
            2,
            1e-5,
        )
    hidden = _layer_norm(
        hidden,
        tensors["transformer.ln_f.weight"],
        tensors["transformer.ln_f.bias"],
        1e-5,
    )
    logits = mx.matmul(hidden, tensors["transformer.wte.weight"].transpose(1, 0))
    mx.eval(logits)
    return np.asarray(logits)


def test_generic_runtime_contract_preserves_mlx_and_bounds_numpy() -> None:
    mlx_runtime = _runtime("mlx")
    numpy_runtime = _runtime("numpy")

    assert validate_normalized_runtime(mlx_runtime, expected_backend="mlx") == mlx_runtime
    assert validate_normalized_mlx_runtime(mlx_runtime) == mlx_runtime
    assert validate_normalized_runtime(numpy_runtime, expected_backend="numpy") == numpy_runtime
    assert validate_normalized_numpy_runtime(numpy_runtime) == numpy_runtime

    invalid = copy.deepcopy(numpy_runtime)
    invalid["dtype"] = "bfloat16"
    with pytest.raises(ValueError, match="numpy runtime dtype"):
        validate_normalized_numpy_runtime(invalid)


def test_existing_mlx_loader_exposes_backend_neutral_stage_adapter() -> None:
    backend = MLXStageBackend()

    assert isinstance(backend, StageRuntimeBackend)
    assert backend.backend == "mlx"
    assert callable(backend.execute_loaded_stage)


def test_numpy_monolithic_runtime_matches_mlx_same_seed_config_and_tokens() -> None:
    runtime = _runtime("numpy")
    weights = _weights()
    token_ids = np.array([[1, 4, 7, 2, 9]], dtype=np.int64)

    backend = NumpyGPT2Runtime(runtime=runtime, tensors=weights)
    numpy_logits = backend.forward_token_ids(token_ids)
    mlx_logits = _mlx_logits(weights, token_ids)

    assert isinstance(backend, MonolithicRuntimePort)
    assert backend.backend == "numpy"
    assert backend.runtime_identity["device"] == "cpu"
    assert backend.runtime_identity["route_ready"] is False
    assert numpy_logits.flags.writeable is False
    assert np.isfinite(numpy_logits).all()
    np.testing.assert_allclose(numpy_logits, mlx_logits, rtol=2e-5, atol=2e-6)
    assert np.argmax(numpy_logits, axis=-1).tolist() == np.argmax(
        mlx_logits, axis=-1
    ).tolist()


def test_numpy_runtime_rejects_nonfinite_weights_and_invalid_tokens() -> None:
    weights = _weights()
    weights["transformer.h.0.attn.c_attn.weight"][0, 0] = math.nan
    with pytest.raises(NumpyRuntimeError, match="nonfinite_tensor"):
        NumpyGPT2Runtime(runtime=_runtime("numpy"), tensors=weights)

    backend = NumpyGPT2Runtime(runtime=_runtime("numpy"), tensors=_weights())
    with pytest.raises(NumpyRuntimeError, match="token_bounds_exceeded"):
        backend.forward_token_ids(np.array([[99]], dtype=np.int64))
