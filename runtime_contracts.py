#!/usr/bin/env python3
"""Pure normalized runtime contracts shared by manifest, assignment, and loader."""
from __future__ import annotations

import copy
import json
import math
from typing import Any, Mapping, Protocol, runtime_checkable

MLX_RUNTIME_BASE_FIELDS = frozenset({"backend", "dtype", "quantization"})
GPT2_DECODER_TENSOR_SUFFIXES = (
    "ln_1.weight",
    "ln_1.bias",
    "attn.c_attn.weight",
    "attn.c_attn.bias",
    "attn.c_proj.weight",
    "attn.c_proj.bias",
    "ln_2.weight",
    "ln_2.bias",
    "mlp.c_fc.weight",
    "mlp.c_fc.bias",
    "mlp.c_proj.weight",
    "mlp.c_proj.bias",
)
NORMALIZED_MLX_RUNTIME_FIELDS = frozenset(
    {*MLX_RUNTIME_BASE_FIELDS, "architecture", "model_config"}
)
GPT2_MODEL_CONFIG_FIELDS = frozenset(
    {
        "n_layer",
        "n_embd",
        "n_head",
        "n_inner",
        "vocab_size",
        "n_positions",
        "layer_norm_epsilon",
        "activation_function",
        "scale_attn_weights",
        "scale_attn_by_inverse_layer_idx",
        "reorder_and_upcast_attn",
        "add_cross_attention",
    }
)
_SUPPORTED_MLX_DTYPES = frozenset({"float16", "bfloat16", "float32"})
_SUPPORTED_GPT2_FLAGS = {
    "scale_attn_weights": True,
    "scale_attn_by_inverse_layer_idx": False,
    "reorder_and_upcast_attn": False,
    "add_cross_attention": False,
}


@runtime_checkable
class StageRuntimeBackend(Protocol):
    """Minimal adapter for executing one already authenticated loaded stage."""

    backend: str

    def execute_loaded_stage(
        self,
        loaded_stage: Any,
        *,
        token_ids: Any | None = None,
        hidden_states: Any | None = None,
    ) -> Any: ...


@runtime_checkable
class MonolithicRuntimePort(Protocol):
    """Backend-neutral monolithic parity surface used before stage integration."""

    backend: str

    @property
    def runtime_identity(self) -> Mapping[str, Any]: ...

    def forward_token_ids(self, token_ids: Any) -> Any: ...


def _positive_int(value: Any, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ValueError(f"runtime model_config {field} must be a positive integer")
    return value


def normalize_gpt2_model_config(
    config: Mapping[str, Any], *, expected_layers: int
) -> dict[str, Any]:
    """Extract the exact GPT-2 subset executable by the MVP MLX runtime."""
    if not isinstance(config, Mapping):
        raise ValueError("runtime model_config source must be an object")
    n_layer = _positive_int(config.get("n_layer"), "n_layer")
    if n_layer != expected_layers:
        raise ValueError("runtime model_config n_layer does not match manifest layer count")
    n_embd = _positive_int(config.get("n_embd"), "n_embd")
    n_head = _positive_int(config.get("n_head"), "n_head")
    if n_embd % n_head != 0:
        raise ValueError("runtime model_config requires n_embd divisible by n_head")
    raw_inner = config.get("n_inner")
    n_inner = 4 * n_embd if raw_inner is None else _positive_int(raw_inner, "n_inner")
    vocab_size = _positive_int(config.get("vocab_size"), "vocab_size")
    n_positions = _positive_int(config.get("n_positions"), "n_positions")
    if vocab_size < 3 or n_positions < 3:
        raise ValueError(
            "runtime model_config vocab_size and n_positions must be at least 3"
        )
    epsilon = config.get("layer_norm_epsilon")
    if (
        not isinstance(epsilon, (int, float))
        or isinstance(epsilon, bool)
        or not math.isfinite(float(epsilon))
        or float(epsilon) <= 0
    ):
        raise ValueError(
            "runtime model_config layer_norm_epsilon must be positive and finite"
        )
    if config.get("activation_function") != "gelu_new":
        raise ValueError("runtime model_config activation_function must be gelu_new")
    for field, expected in _SUPPORTED_GPT2_FLAGS.items():
        actual = config.get(field, expected)
        if actual is not expected:
            raise ValueError(
                f"unsupported runtime model_config {field}={actual!r}"
            )
    normalized = {
        "n_layer": n_layer,
        "n_embd": n_embd,
        "n_head": n_head,
        "n_inner": n_inner,
        "vocab_size": vocab_size,
        "n_positions": n_positions,
        "layer_norm_epsilon": float(epsilon),
        "activation_function": "gelu_new",
        **_SUPPORTED_GPT2_FLAGS,
    }
    json.dumps(normalized, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return normalized


def validate_normalized_mlx_runtime(runtime: Any) -> dict[str, Any]:
    """Validate an assignment runtime as the exact executable MLX contract."""
    if not isinstance(runtime, Mapping):
        raise ValueError("runtime identity must be an object")
    if set(runtime) != NORMALIZED_MLX_RUNTIME_FIELDS:
        raise ValueError(
            "runtime identity fields do not match the normalized MLX contract"
        )
    if runtime.get("backend") != "mlx":
        raise ValueError("unsupported runtime backend; expected mlx")
    if runtime.get("quantization") != "none":
        raise ValueError("unsupported runtime quantization; only none is supported")
    if runtime.get("dtype") not in _SUPPORTED_MLX_DTYPES:
        raise ValueError(
            "unsupported runtime dtype; expected float16, bfloat16, or float32"
        )
    if runtime.get("architecture") != "gpt2":
        raise ValueError("unsupported runtime architecture; only gpt2 is supported")
    model_config = runtime.get("model_config")
    if not isinstance(model_config, Mapping):
        raise ValueError("gpt2 runtime requires model_config")
    if set(model_config) != GPT2_MODEL_CONFIG_FIELDS:
        raise ValueError(
            "gpt2 model_config fields do not match the normalized runtime contract"
        )
    normalized_config = normalize_gpt2_model_config(
        model_config,
        expected_layers=_positive_int(model_config.get("n_layer"), "n_layer"),
    )
    normalized = {
        "backend": "mlx",
        "dtype": runtime["dtype"],
        "quantization": "none",
        "architecture": "gpt2",
        "model_config": normalized_config,
    }
    if json.loads(json.dumps(runtime, allow_nan=False)) != normalized:
        raise ValueError("runtime identity is not in normalized canonical form")
    return copy.deepcopy(normalized)


def validate_normalized_numpy_runtime(runtime: Any) -> dict[str, Any]:
    """Validate the concrete CPU NumPy GPT-2 monolithic runtime contract."""

    if not isinstance(runtime, Mapping):
        raise ValueError("runtime identity must be an object")
    if set(runtime) != NORMALIZED_MLX_RUNTIME_FIELDS:
        raise ValueError(
            "runtime identity fields do not match the normalized NumPy contract"
        )
    if runtime.get("backend") != "numpy":
        raise ValueError("unsupported runtime backend; expected numpy")
    if runtime.get("quantization") != "none":
        raise ValueError("unsupported runtime quantization; only none is supported")
    if runtime.get("dtype") not in {"float16", "float32"}:
        raise ValueError("unsupported numpy runtime dtype; expected float16 or float32")
    if runtime.get("architecture") != "gpt2":
        raise ValueError("unsupported runtime architecture; only gpt2 is supported")
    model_config = runtime.get("model_config")
    if not isinstance(model_config, Mapping):
        raise ValueError("gpt2 runtime requires model_config")
    if set(model_config) != GPT2_MODEL_CONFIG_FIELDS:
        raise ValueError(
            "gpt2 model_config fields do not match the normalized runtime contract"
        )
    normalized_config = normalize_gpt2_model_config(
        model_config,
        expected_layers=_positive_int(model_config.get("n_layer"), "n_layer"),
    )
    normalized = {
        "backend": "numpy",
        "dtype": runtime["dtype"],
        "quantization": "none",
        "architecture": "gpt2",
        "model_config": normalized_config,
    }
    if json.loads(json.dumps(runtime, allow_nan=False)) != normalized:
        raise ValueError("runtime identity is not in normalized canonical form")
    return copy.deepcopy(normalized)


def validate_normalized_runtime(
    runtime: Any,
    *,
    expected_backend: str | None = None,
) -> dict[str, Any]:
    """Dispatch strict validation without weakening backend-specific contracts."""

    if not isinstance(runtime, Mapping):
        raise ValueError("runtime identity must be an object")
    backend = runtime.get("backend")
    if expected_backend is not None and backend != expected_backend:
        raise ValueError(f"runtime backend mismatch; expected {expected_backend}")
    if backend == "mlx":
        return validate_normalized_mlx_runtime(runtime)
    if backend == "numpy":
        return validate_normalized_numpy_runtime(runtime)
    raise ValueError(f"unsupported runtime backend: {backend!r}")
