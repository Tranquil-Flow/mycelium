#!/usr/bin/env python3
"""Pure normalized runtime contracts shared by manifest, assignment, and loader."""
from __future__ import annotations

import copy
import json
import math
import uuid
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
SUPPORTED_NUMPY_DTYPES = frozenset({"float32"})
_RUNTIME_IDENTITY_FIELDS = frozenset(
    {
        "backend",
        "backend_version",
        "device",
        "dtype",
        "quantization",
        "architecture",
    }
)
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
    if runtime.get("dtype") not in SUPPORTED_NUMPY_DTYPES:
        raise ValueError("unsupported numpy runtime dtype; expected float32")
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


def _plain_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        if not all(isinstance(key, str) for key in value):
            raise ValueError("canonical JSON mappings require string keys")
        return {key: _plain_json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain_json(item) for item in value]
    return value


def validate_loaded_stage_authentication(
    proof: Any,
    *,
    authenticated_assignment_id: Any,
    authenticated_load_generation: Any,
    authenticated_runtime: Any,
    authenticated_runtime_identity: Any,
    normalized_runtime: Mapping[str, Any],
) -> None:
    """Authenticate execution-critical proof fields against loader-held values."""

    if not isinstance(proof, Mapping):
        raise ValueError("invalid_loaded_stage_proof")
    if proof.get("protocol") != "mycelium.layer_load_proof.v1":
        raise ValueError("invalid_loaded_stage_proof")
    if proof.get("route_ready") is not False:
        raise ValueError("invalid_loaded_stage_route_claim")

    assignment_id = proof.get("assignment_id")
    try:
        canonical_assignment_id = str(uuid.UUID(str(assignment_id)))
    except (TypeError, ValueError) as exc:
        raise ValueError("assignment_id_mismatch") from exc
    if (
        assignment_id != canonical_assignment_id
        or authenticated_assignment_id != canonical_assignment_id
    ):
        raise ValueError("assignment_id_mismatch")

    proof_generation = proof.get("load_generation")
    if (
        not isinstance(proof_generation, int)
        or isinstance(proof_generation, bool)
        or proof_generation < 0
        or not isinstance(authenticated_load_generation, int)
        or isinstance(authenticated_load_generation, bool)
        or authenticated_load_generation < 0
        or proof_generation != authenticated_load_generation
    ):
        raise ValueError("load_generation_mismatch")

    try:
        runtime = _plain_json(normalized_runtime)
        bound_runtime = _plain_json(authenticated_runtime)
        runtime_identity = _plain_json(proof.get("runtime_identity"))
        bound_runtime_identity = _plain_json(authenticated_runtime_identity)
        json.dumps(runtime, sort_keys=True, separators=(",", ":"), allow_nan=False)
        json.dumps(
            runtime_identity,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("runtime_identity_mismatch") from exc
    if runtime != bound_runtime:
        raise ValueError("runtime_identity_mismatch")
    if (
        not isinstance(runtime_identity, dict)
        or set(runtime_identity) != _RUNTIME_IDENTITY_FIELDS
        or runtime_identity != bound_runtime_identity
    ):
        raise ValueError("runtime_identity_mismatch")
    for field in ("backend", "dtype", "quantization", "architecture"):
        if runtime_identity.get(field) != runtime.get(field):
            raise ValueError("runtime_identity_mismatch")
    for field in ("backend_version", "device"):
        if (
            not isinstance(runtime_identity.get(field), str)
            or not runtime_identity[field]
        ):
            raise ValueError("runtime_identity_mismatch")


_ASSIGNMENT_STAGE_COMPONENTS = frozenset(
    {"input_embedding", "decoder", "final_norm", "lm_head"}
)


def assignment_stage_role(components: Any) -> str:
    """Classify the assignment-bound role of a stage from its component set.

    The role is determined by the inclusive endpoints of the stage range:

    - "entry" when the stage owns input_embedding (regardless of other
      components). It is the only role that may accept token_ids.
    - "final" when the stage owns final_norm or lm_head (with or
      without the decoder).
    - "intermediate" for purely decoder-only stages that pass hidden
      states between caller hops.

    Raises ValueError when any component is unknown or the component set is
    empty.
    """
    if not isinstance(components, (set, frozenset, list, tuple)):
        raise ValueError(
            "assignment components must be a set-like of component names"
        )
    tokens = set(components)
    if not tokens:
        raise ValueError("assignment components must not be empty")
    unknown = tokens - _ASSIGNMENT_STAGE_COMPONENTS
    if unknown:
        raise ValueError(
            f"unknown assignment component(s): {", ".join(sorted(unknown))}"
        )
    if "input_embedding" in tokens:
        return "entry"
    if "final_norm" in tokens or "lm_head" in tokens:
        return "final"
    return "intermediate"


def validate_assignment_stage_boundaries(
    components: Any,
    *,
    start_layer: int,
    end_layer_exclusive: int,
    total_layers: int,
) -> None:
    """Validate component ownership against executable GPT-2 stage boundaries."""

    assignment_stage_role(components)
    tokens = set(components)
    if "input_embedding" in tokens and start_layer != 0:
        raise ValueError("input_embedding may only be assigned with the first layer")
    if {"final_norm", "lm_head"}.intersection(tokens) and (
        end_layer_exclusive != total_layers
    ):
        raise ValueError(
            "final_norm and lm_head may only be assigned with the final layer"
        )
