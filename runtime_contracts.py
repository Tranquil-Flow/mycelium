#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Pure normalized runtime contracts shared by manifest, assignment, and loader."""
from __future__ import annotations

import copy
import json
import math
import uuid
from typing import Any, Mapping, Protocol, runtime_checkable

from model_adapters import (
    GPT2_DECODER_TENSOR_SUFFIXES as GPT2_DECODER_TENSOR_SUFFIXES,
    QWEN2_DECODER_TENSOR_SUFFIXES as QWEN2_DECODER_TENSOR_SUFFIXES,
    QWEN3_DECODER_TENSOR_SUFFIXES as QWEN3_DECODER_TENSOR_SUFFIXES,
)

MLX_RUNTIME_BASE_FIELDS = frozenset({"backend", "dtype", "quantization"})
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
QWEN2_MODEL_CONFIG_FIELDS = frozenset(
    {
        "n_layer",
        "n_embd",
        "n_head",
        "n_kv_head",
        "n_inner",
        "vocab_size",
        "n_positions",
        "rms_norm_epsilon",
        "rope_theta",
        "head_dim",
        "activation_function",
        "tie_word_embeddings",
    }
)
QWEN3_MODEL_CONFIG_FIELDS = QWEN2_MODEL_CONFIG_FIELDS
QWEN35_MODEL_CONFIG_FIELDS = frozenset(
    {
        *QWEN2_MODEL_CONFIG_FIELDS,
        "layer_types",
        "rotary_dim",
        "attn_output_gate",
        "linear_conv_kernel_dim",
        "linear_num_key_heads",
        "linear_key_head_dim",
        "linear_num_value_heads",
        "linear_value_head_dim",
    }
)
QWEN35_LAYER_TYPES = frozenset({"linear_attention", "full_attention"})
# qwen3_5_moe: the hybrid fields plus the sparse MoE MLP.  ``n_inner`` is the
# per-expert intermediate width (== moe_intermediate_size): no dense MLP exists.
QWEN35_MOE_FIELDS = frozenset(
    {
        "num_experts",
        "num_experts_per_tok",
        "moe_intermediate_size",
        "shared_expert_intermediate_size",
        "norm_topk_prob",
    }
)
QWEN35_MOE_MODEL_CONFIG_FIELDS = frozenset({*QWEN35_MODEL_CONFIG_FIELDS, *QWEN35_MOE_FIELDS})
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
_SUPPORTED_QUANTIZATIONS = frozenset({"none", "int8-weight-only"})


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


def normalize_qwen2_model_config(
    config: Mapping[str, Any], *, expected_layers: int
) -> dict[str, Any]:
    """Extract the exact Qwen2 decoder subset executable by Mycelium."""

    return _normalize_qwen_decoder_config(
        config, expected_layers=expected_layers, wide_attention=False
    )


def _normalize_qwen_decoder_config(
    config: Mapping[str, Any], *, expected_layers: int, wide_attention: bool
) -> dict[str, Any]:
    # ``wide_attention`` admits Qwen3 checkpoints whose attention width
    # ``n_head * head_dim`` differs from ``hidden_size`` (Qwen3-0.6B, -4B, -32B).
    # Qwen2 keeps the exact equality it always required.
    if not isinstance(config, Mapping):
        raise ValueError("runtime model_config source must be an object")
    n_layer = _positive_int(config.get("num_hidden_layers", config.get("n_layer")), "n_layer")
    if n_layer != expected_layers:
        raise ValueError("runtime model_config n_layer does not match manifest layer count")
    n_embd = _positive_int(config.get("hidden_size", config.get("n_embd")), "n_embd")
    n_head = _positive_int(config.get("num_attention_heads", config.get("n_head")), "n_head")
    n_kv_head = _positive_int(config.get("num_key_value_heads", config.get("n_kv_head")), "n_kv_head")
    head_dim = _positive_int(config.get("head_dim", n_embd // n_head), "head_dim")
    if (
        (not wide_attention and n_embd != n_head * head_dim)
        or n_head % n_kv_head != 0
    ):
        raise ValueError("runtime model_config has incompatible attention heads")
    n_inner = _positive_int(config.get("intermediate_size", config.get("n_inner")), "n_inner")
    vocab_size = _positive_int(config.get("vocab_size"), "vocab_size")
    n_positions = _positive_int(
        config.get("max_position_embeddings", config.get("n_positions")),
        "n_positions",
    )
    epsilon = config.get("rms_norm_eps", config.get("rms_norm_epsilon"))
    rope_theta = config.get("rope_theta")
    for value, field in ((epsilon, "rms_norm_epsilon"), (rope_theta, "rope_theta")):
        if (
            not isinstance(value, (int, float))
            or isinstance(value, bool)
            or not math.isfinite(float(value))
            or float(value) <= 0
        ):
            raise ValueError(f"runtime model_config {field} must be positive and finite")
    activation = config.get("hidden_act", config.get("activation_function"))
    if activation != "silu":
        raise ValueError("runtime model_config activation_function must be silu")
    tied = config.get("tie_word_embeddings")
    if not isinstance(tied, bool):
        raise ValueError("runtime model_config tie_word_embeddings must be boolean")
    normalized = {
        "n_layer": n_layer,
        "n_embd": n_embd,
        "n_head": n_head,
        "n_kv_head": n_kv_head,
        "n_inner": n_inner,
        "vocab_size": vocab_size,
        "n_positions": n_positions,
        "rms_norm_epsilon": float(epsilon),
        "rope_theta": float(rope_theta),
        "head_dim": head_dim,
        "activation_function": "silu",
        "tie_word_embeddings": tied,
    }
    json.dumps(normalized, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return normalized


def normalize_qwen3_model_config(
    config: Mapping[str, Any], *, expected_layers: int
) -> dict[str, Any]:
    """Extract the exact Qwen3 dense decoder subset executable by Mycelium."""

    normalized = _normalize_qwen_decoder_config(
        config, expected_layers=expected_layers, wide_attention=True
    )
    if config.get("attention_bias", False) is not False:
        raise ValueError("runtime qwen3 attention_bias must be false")
    attention_dropout = config.get("attention_dropout", 0.0)
    if attention_dropout != 0 and attention_dropout != 0.0:
        raise ValueError("runtime qwen3 attention_dropout must be zero")
    if config.get("rope_scaling") is not None:
        raise ValueError("runtime qwen3 rope_scaling is unsupported")
    if config.get("use_sliding_window", False) is not False:
        raise ValueError("runtime qwen3 sliding-window attention is unsupported")
    if config.get("sliding_window") is not None:
        raise ValueError("runtime qwen3 sliding_window must be null")
    return normalized


def normalize_qwen35_model_config(
    config: Mapping[str, Any], *, expected_layers: int
) -> dict[str, Any]:
    """Extract the exact Qwen3.5 hybrid text decoder executable by Mycelium.

    Accepts the Hugging Face ``Qwen3_5ForConditionalGeneration`` config (text
    fields under ``text_config``), a bare text config, or an already
    normalized document.  Only the text decoder is admitted: vision inputs
    (multimodal positions) and the MTP head are out of scope.
    """

    if not isinstance(config, Mapping):
        raise ValueError("runtime model_config source must be an object")
    text = config.get("text_config", config)
    if not isinstance(text, Mapping):
        raise ValueError("runtime qwen3_5 text_config must be an object")
    for field in ("num_experts", "num_experts_per_tok"):
        experts = text.get(field, 0)
        if experts not in (0, None):
            raise ValueError("runtime qwen3_5 dense contract refuses moe fields")
    decoder_source = dict(text)
    if "tie_word_embeddings" not in decoder_source and "tie_word_embeddings" in config:
        # Published 27B/35B text_configs carry the flag only at the top level.
        decoder_source["tie_word_embeddings"] = config["tie_word_embeddings"]
    if "rotary_dim" not in text:
        rope = text.get("rope_parameters")
        if isinstance(rope, Mapping) and "rope_theta" not in decoder_source:
            decoder_source["rope_theta"] = rope.get("rope_theta")
    normalized = _normalize_qwen_decoder_config(
        decoder_source, expected_layers=expected_layers, wide_attention=True
    )
    if text.get("attention_bias", False) is not False:
        raise ValueError("runtime qwen3_5 attention_bias must be false")
    attention_dropout = text.get("attention_dropout", 0.0)
    if attention_dropout != 0 and attention_dropout != 0.0:
        raise ValueError("runtime qwen3_5 attention_dropout must be zero")
    if text.get("attn_output_gate", True) is not True:
        raise ValueError("runtime qwen3_5 attn_output_gate must be true")
    if text.get("mlp_only_layers", []) not in ([], ()):
        raise ValueError("runtime qwen3_5 mlp_only_layers must be empty")
    if text.get("mamba_ssm_dtype", "float32") != "float32":
        raise ValueError("runtime qwen3_5 linear state must be float32")
    head_dim = normalized["head_dim"]
    if "rotary_dim" in text:
        rotary_dim = _positive_int(text.get("rotary_dim"), "rotary_dim")
        rope_theta = normalized["rope_theta"]
    else:
        rope = text.get("rope_parameters")
        if not isinstance(rope, Mapping):
            raise ValueError("runtime qwen3_5 rope_parameters must be an object")
        if rope.get("rope_type", rope.get("type")) != "default":
            raise ValueError("runtime qwen3_5 rope_type must be default (no rope scaling)")
        factor = rope.get("partial_rotary_factor")
        if (
            not isinstance(factor, (int, float))
            or isinstance(factor, bool)
            or not 0 < float(factor) <= 1
        ):
            raise ValueError("runtime qwen3_5 partial_rotary_factor is invalid")
        rotary_dim = int(head_dim * float(factor))
        if rotary_dim * 1.0 != head_dim * float(factor):
            raise ValueError("runtime qwen3_5 rotary dimension is not integral")
        section = rope.get("mrope_section")
        # Text-only positions: all three mRoPE streams carry the token position,
        # so interleaved mRoPE is exactly 1-D RoPE on the rotary slice when the
        # sections cover every rotary frequency.
        if (
            not isinstance(section, (list, tuple))
            or len(section) != 3
            or not all(isinstance(v, int) and not isinstance(v, bool) and v >= 0 for v in section)
            or sum(section) != rotary_dim // 2
        ):
            raise ValueError("runtime qwen3_5 mrope_section does not cover the rotary dimension")
        theta = rope.get("rope_theta")
        if (
            not isinstance(theta, (int, float))
            or isinstance(theta, bool)
            or not math.isfinite(float(theta))
            or float(theta) <= 0
        ):
            raise ValueError("runtime qwen3_5 rope_theta must be positive and finite")
        rope_theta = float(theta)
    if rotary_dim % 2 or rotary_dim > head_dim:
        raise ValueError("runtime qwen3_5 rotary dimension is invalid")
    linear = {
        field: _positive_int(text.get(field), field)
        for field in (
            "linear_conv_kernel_dim",
            "linear_num_key_heads",
            "linear_key_head_dim",
            "linear_num_value_heads",
            "linear_value_head_dim",
        )
    }
    if linear["linear_num_value_heads"] % linear["linear_num_key_heads"]:
        raise ValueError("runtime qwen3_5 linear value heads must be a multiple of key heads")
    layer_types = text.get("layer_types")
    if (
        not isinstance(layer_types, (list, tuple))
        or len(layer_types) != normalized["n_layer"]
        or any(kind not in QWEN35_LAYER_TYPES for kind in layer_types)
    ):
        raise ValueError("runtime qwen3_5 layer_types are invalid")
    interval = text.get("full_attention_interval")
    if interval is not None:
        interval = _positive_int(interval, "full_attention_interval")
        expected = [
            "full_attention" if (index + 1) % interval == 0 else "linear_attention"
            for index in range(normalized["n_layer"])
        ]
        if list(layer_types) != expected:
            raise ValueError("runtime qwen3_5 layer_types disagree with full_attention_interval")
    normalized.update(
        {
            "rope_theta": rope_theta,
            "layer_types": list(layer_types),
            "rotary_dim": rotary_dim,
            "attn_output_gate": True,
            **linear,
        }
    )
    json.dumps(normalized, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return normalized


def normalize_qwen35_moe_model_config(
    config: Mapping[str, Any], *, expected_layers: int
) -> dict[str, Any]:
    """Extract the exact Qwen3.5 MoE (``qwen3_5_moe``) text decoder.

    The hybrid decoder of :func:`normalize_qwen35_model_config` with every
    layer's MLP replaced by a sparse MoE block: a router over ``num_experts``
    experts, top ``num_experts_per_tok`` with renormalised weights
    (``norm_topk_prob``; the Hugging Face router always renormalises), packed
    experts of width ``moe_intermediate_size`` and one sigmoid-gated shared
    expert of width ``shared_expert_intermediate_size``.  Accepts the HF
    ``Qwen3_5MoeForConditionalGeneration`` config, a bare text config, or an
    already normalized document.
    """

    if not isinstance(config, Mapping):
        raise ValueError("runtime model_config source must be an object")
    text = config.get("text_config", config)
    if not isinstance(text, Mapping):
        raise ValueError("runtime qwen3_5_moe text_config must be an object")
    moe: dict[str, Any] = {}
    for field in (
        "num_experts",
        "num_experts_per_tok",
        "moe_intermediate_size",
        "shared_expert_intermediate_size",
    ):
        try:
            moe[field] = _positive_int(text.get(field), field)
        except ValueError as exc:
            raise ValueError(f"runtime qwen3_5_moe {field} must be a positive integer") from exc
    if moe["num_experts_per_tok"] > moe["num_experts"]:
        raise ValueError("runtime qwen3_5_moe num_experts_per_tok exceeds num_experts")
    if text.get("norm_topk_prob", True) is not True:
        raise ValueError("runtime qwen3_5_moe norm_topk_prob must be true")
    if text.get("decoder_sparse_step", 1) != 1:
        raise ValueError("runtime qwen3_5_moe decoder_sparse_step must be 1 (every layer sparse)")
    if text.get("mlp_only_layers", []) not in ([], ()):
        raise ValueError("runtime qwen3_5_moe mlp_only_layers must be empty")
    if "n_inner" in text and text.get("n_inner") != moe["moe_intermediate_size"]:
        raise ValueError("runtime qwen3_5_moe n_inner must equal moe_intermediate_size")
    dense_source = {
        key: value
        for key, value in text.items()
        if key not in QWEN35_MOE_FIELDS
        and key not in {"decoder_sparse_step", "intermediate_size", "n_inner"}
    }
    dense_source["intermediate_size"] = moe["moe_intermediate_size"]
    if "tie_word_embeddings" not in dense_source and "tie_word_embeddings" in config:
        dense_source["tie_word_embeddings"] = config["tie_word_embeddings"]
    normalized = normalize_qwen35_model_config(dense_source, expected_layers=expected_layers)
    normalized.update({**moe, "norm_topk_prob": True})
    json.dumps(normalized, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return normalized


def _normalize_architecture_runtime(runtime: Mapping[str, Any]) -> tuple[str, dict[str, Any]]:
    architecture = runtime.get("architecture")
    model_config = runtime.get("model_config")
    if not isinstance(model_config, Mapping):
        raise ValueError(f"{architecture} runtime requires model_config")
    if architecture == "gpt2":
        if set(model_config) != GPT2_MODEL_CONFIG_FIELDS:
            raise ValueError("gpt2 model_config fields do not match the normalized runtime contract")
        return architecture, normalize_gpt2_model_config(
            model_config,
            expected_layers=_positive_int(model_config.get("n_layer"), "n_layer"),
        )
    if architecture == "qwen2":
        if set(model_config) != QWEN2_MODEL_CONFIG_FIELDS:
            raise ValueError(
                "qwen2 architecture model_config fields do not match the normalized runtime contract"
            )
        return architecture, normalize_qwen2_model_config(
            model_config,
            expected_layers=_positive_int(model_config.get("n_layer"), "n_layer"),
        )
    if architecture == "qwen3":
        if set(model_config) != QWEN3_MODEL_CONFIG_FIELDS:
            raise ValueError(
                "qwen3 architecture model_config fields do not match the normalized runtime contract"
            )
        return architecture, normalize_qwen3_model_config(
            model_config,
            expected_layers=_positive_int(model_config.get("n_layer"), "n_layer"),
        )
    if architecture == "qwen3_5":
        if set(model_config) != QWEN35_MODEL_CONFIG_FIELDS:
            raise ValueError(
                "qwen3_5 architecture model_config fields do not match the normalized runtime contract"
            )
        return architecture, normalize_qwen35_model_config(
            model_config,
            expected_layers=_positive_int(model_config.get("n_layer"), "n_layer"),
        )
    if architecture == "qwen3_5_moe":
        if set(model_config) != QWEN35_MOE_MODEL_CONFIG_FIELDS:
            raise ValueError(
                "qwen3_5_moe architecture model_config fields do not match the normalized "
                "runtime contract"
            )
        return architecture, normalize_qwen35_moe_model_config(
            model_config,
            expected_layers=_positive_int(model_config.get("n_layer"), "n_layer"),
        )
    raise ValueError(
        "unsupported runtime architecture; expected gpt2, qwen2, qwen3, qwen3_5, or qwen3_5_moe"
    )


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
    if runtime.get("quantization") not in _SUPPORTED_QUANTIZATIONS:
        raise ValueError("unsupported runtime quantization")
    if runtime.get("dtype") not in _SUPPORTED_MLX_DTYPES:
        raise ValueError(
            "unsupported runtime dtype; expected float16, bfloat16, or float32"
        )
    architecture, normalized_config = _normalize_architecture_runtime(runtime)
    if architecture == "gpt2" and runtime["quantization"] != "none":
        raise ValueError("gpt2 runtime quantization is unsupported")
    normalized = {
        "backend": "mlx",
        "dtype": runtime["dtype"],
        "quantization": runtime["quantization"],
        "architecture": architecture,
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
    if runtime.get("quantization") not in _SUPPORTED_QUANTIZATIONS:
        raise ValueError("unsupported runtime quantization")
    if runtime.get("dtype") not in SUPPORTED_NUMPY_DTYPES:
        raise ValueError("unsupported numpy runtime dtype; expected float32")
    architecture, normalized_config = _normalize_architecture_runtime(runtime)
    if architecture == "gpt2" and runtime["quantization"] != "none":
        raise ValueError("gpt2 runtime quantization is unsupported")
    if architecture in {"qwen3_5", "qwen3_5_moe"}:
        # The hybrid decoder has an MLX stage kernel only; the NumPy Qwen3.5
        # implementations are test oracles, not stage backends.
        raise ValueError(f"{architecture} numpy stage runtime is unsupported")
    normalized = {
        "backend": "numpy",
        "dtype": runtime["dtype"],
        "quantization": runtime["quantization"],
        "architecture": architecture,
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
    if backend == "mobile-rust-qwen2":
        # Metadata admission only. Execution/load proof belongs to the native phone.
        if runtime.get("architecture") != "qwen2" or runtime.get("quantization") != "int8-weight-only":
            raise ValueError("phone runtime requires qwen2 int8-weight-only")
        normalized = validate_normalized_numpy_runtime({**runtime, "backend": "numpy"})
        return {**normalized, "backend": backend}
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
    authenticated_loaded_components: Any,
    authenticated_loaded_range: Any,
    resolved_aliases: Any,
    authenticated_resolved_aliases: Any,
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
        loaded_components = _plain_json(proof.get("loaded_components"))
        bound_loaded_components = _plain_json(authenticated_loaded_components)
        json.dumps(
            loaded_components,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("loaded_components_mismatch") from exc
    if (
        not isinstance(loaded_components, list)
        or not all(isinstance(component, str) for component in loaded_components)
        or loaded_components != bound_loaded_components
    ):
        raise ValueError("loaded_components_mismatch")

    try:
        loaded_range = _plain_json(proof.get("loaded_range"))
        bound_loaded_range = _plain_json(authenticated_loaded_range)
        json.dumps(
            loaded_range,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("loaded_range_mismatch") from exc
    if not isinstance(loaded_range, dict) or loaded_range != bound_loaded_range:
        raise ValueError("loaded_range_mismatch")

    try:
        alias_documents = (
            _plain_json(proof.get("resolved_component_aliases")),
            _plain_json(resolved_aliases),
            _plain_json(authenticated_resolved_aliases),
        )
        canonical_aliases = []
        for aliases in alias_documents:
            if not isinstance(aliases, dict):
                raise ValueError
            for source, alias in aliases.items():
                if (
                    not source
                    or not isinstance(alias, dict)
                    or set(alias) != {"target_component", "tensor_keys"}
                    or not isinstance(alias["target_component"], str)
                    or not alias["target_component"]
                    or not isinstance(alias["tensor_keys"], list)
                    or not alias["tensor_keys"]
                    or not all(
                        isinstance(key, str) and key
                        for key in alias["tensor_keys"]
                    )
                    or len(alias["tensor_keys"]) != len(set(alias["tensor_keys"]))
                ):
                    raise ValueError
            canonical_aliases.append(
                json.dumps(
                    aliases,
                    sort_keys=True,
                    separators=(",", ":"),
                    allow_nan=False,
                )
            )
    except (TypeError, ValueError):
        raise ValueError("resolved_aliases_mismatch") from None
    if len(set(canonical_aliases)) != 1:
        raise ValueError("resolved_aliases_mismatch")

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
            f"unknown assignment component(s): {', '.join(sorted(unknown))}"
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
