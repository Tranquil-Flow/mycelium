"""W6 27B lane: Qwen3.5 (Qwen3.8-27B) hybrid decoder naming is first-class.

The exact tensor namespace, hybrid layer split, and quantized companions below
were captured from the verified route artifact
``mlx-community/Qwen3.8-27B-4bit`` revision
``3e6447f082e89cc7f0bc6e5441afd38dfce760ff``.  The tests rebuild that namespace
at a tiny synthetic size so they stay bounded and do not depend on the local
16 GB snapshot.  The affine-4-bit cases below additionally rebuild the
artifact's U32-packed weight sources (``quantization_config``:
``{bits: 4, group_size: 64, mode: affine}``) with hand-computed and
MLX-verified dequantization references.
"""

from __future__ import annotations

import copy
import io
import json
import struct
import uuid
from pathlib import Path
from typing import Any

import mlx.core as mx
import numpy as np
import pytest

import model_manifest as mm
import numpy_runtime
import runtime_loader
from layer_assignment import assignment_id_for
from model_adapters import (
    QWEN3_5_EMBEDDING_TENSOR_KEYS,
    QWEN3_5_FINAL_NORM_TENSOR_KEYS,
    QWEN3_5_FULL_ATTENTION_TENSOR_SUFFIXES,
    QWEN3_5_LINEAR_ATTENTION_TENSOR_SUFFIXES,
    QWEN3_5_LM_HEAD_TENSOR_KEYS,
    adapter_for_config,
    adapter_for_runtime,
)
from runtime_contracts import (
    QWEN3_5_MODEL_CONFIG_FIELDS,
    validate_normalized_mlx_runtime,
    validate_normalized_runtime,
)
from runtime_loader import (
    RuntimeLoadError,
    _validate_assignment,
    _validate_component_ownership,
    load_assignment_stage,
)
from weight_provisioning import sha256_file

LINEAR_LAYER_TYPE = "linear_attention"
FULL_LAYER_TYPE = "full_attention"
SHARD = "model-00001-of-00001.safetensors"

TEXT_CONFIG = {
    "attention_bias": False,
    "head_dim": 4,
    "hidden_act": "silu",
    "hidden_size": 16,
    "intermediate_size": 32,
    "layer_types": [LINEAR_LAYER_TYPE, FULL_LAYER_TYPE],
    "linear_conv_kernel_dim": 4,
    "linear_key_head_dim": 2,
    "linear_num_key_heads": 2,
    "linear_value_head_dim": 2,
    "linear_num_value_heads": 4,
    "max_position_embeddings": 64,
    "model_type": "qwen3_5_text",
    "num_attention_heads": 4,
    "num_key_value_heads": 1,
    "num_hidden_layers": 2,
    "partial_rotary_factor": 0.5,
    "rms_norm_eps": 1e-6,
    "rope_parameters": {"rope_theta": 10000000.0, "rope_type": "default"},
    "tie_word_embeddings": False,
    "vocab_size": 32,
}

CONFIG = {
    "architectures": ["Qwen3_5ForConditionalGeneration"],
    "image_token_id": 30,
    "model_type": "qwen3_5",
    "text_config": TEXT_CONFIG,
    "tie_word_embeddings": False,
}


def _layer_keys(layer: int, suffixes: tuple[str, ...]) -> list[str]:
    prefix = f"language_model.model.layers.{layer}."
    return [prefix + suffix for suffix in suffixes]


WEIGHT_MAP = {
    **{key: SHARD for key in _layer_keys(0, QWEN3_5_LINEAR_ATTENTION_TENSOR_SUFFIXES)},
    **{key: SHARD for key in _layer_keys(1, QWEN3_5_FULL_ATTENTION_TENSOR_SUFFIXES)},
    **{key: SHARD for key in QWEN3_5_EMBEDDING_TENSOR_KEYS},
    **{key: SHARD for key in QWEN3_5_FINAL_NORM_TENSOR_KEYS},
    **{key: SHARD for key in QWEN3_5_LM_HEAD_TENSOR_KEYS},
    # Vision tower tensors are present in the multimodal checkpoint but are not
    # route-owned by the language-model stages.
    "vision_tower.blocks.0.attn.qkv.weight": SHARD,
    "vision_tower.blocks.0.attn.qkv.bias": SHARD,
}


def _manifest(weight_map: dict[str, str] | None = None) -> dict:
    return mm.compile_model_manifest(
        model_id="mlx-community/Qwen3.8-27B-4bit",
        requested_revision="3e6447f082e89cc7f0bc6e5441afd38dfce760ff",
        resolved_commit="3e6447f082e89cc7f0bc6e5441afd38dfce760ff",
        config=CONFIG,
        checkpoint_index={"weight_map": dict(weight_map or WEIGHT_MAP)},
        file_metadata={SHARD: {"size_bytes": 1024, "sha256": "a" * 64}},
    )


def _runtime(backend: str = "mlx", *, layer_types: list[str] | None = None) -> dict:
    runtime_model = _manifest()["runtime_model"]
    model_config = copy.deepcopy(runtime_model["model_config"])
    if layer_types is not None:
        model_config["layer_types"] = layer_types
    return {
        "backend": backend,
        "dtype": "float32",
        "quantization": "none",
        "architecture": runtime_model["architecture"],
        "model_config": model_config,
    }


def _assignment(
    manifest: dict,
    *,
    start: int,
    end: int,
    components: list[str],
    runtime: dict,
    node_id: str = "node-2",
) -> dict:
    static_keys = manifest["component_tensor_keys"]
    layers = range(start, end)
    decoder_keys = sorted(
        key for layer in layers for key in manifest["tensor_keys_by_layer"][str(layer)]
    )
    component_tensor_keys = {
        component: (decoder_keys if component == "decoder" else list(static_keys[component]))
        for component in components
    }
    covering = {
        path
        for layer in layers
        for path in manifest["layer_files"][str(layer)]
    }
    for component in components:
        if component != "decoder":
            covering.update(manifest["component_files"][component])
    files = [
        {
            "path": path,
            "size_bytes": 1024,
            "content_digest": "sha256:" + "b" * 64,
        }
        for path in sorted(covering)
    ]
    identity = {
        "protocol": "mycelium.layer_assignment.v2",
        "deployment_id": str(uuid.uuid5(uuid.NAMESPACE_URL, "w6-27b-tests")),
        "deployment_epoch": 3,
        "node_id": node_id,
        "manifest_digest": mm.manifest_digest_ref(manifest),
        "model_id": manifest["model_id"],
        "resolved_commit": manifest["resolved_commit"],
        "range": {
            "start_layer": start,
            "end_layer_exclusive": end,
            "layer_count": end - start,
        },
        "components": list(components),
        "component_tensor_keys": component_tensor_keys,
        "component_aliases": {},
        "expected_tensor_prefixes": [
            manifest["block_prefix_template"].format(layer=layer) for layer in layers
        ],
        "expected_tensor_keys": sorted(
            {key for keys in component_tensor_keys.values() for key in keys}
        ),
        "files": files,
        "artifact_cache_root": f"/tmp/w6-27b-tests/{node_id}",
        "runtime": copy.deepcopy(runtime),
        "control_plane_binding": {
            "protocol": "mycelium.control_plane_binding.v1",
            "evidence_bundle_digest": "sha256:" + "c" * 64,
            "planner_snapshot_digest": "sha256:" + "d" * 64,
            "snapshot_generation": 1,
            "swarm_id": "w6-27b-tests",
            "deployment_id": str(uuid.uuid5(uuid.NAMESPACE_URL, "w6-27b-tests")),
            "deployment_epoch": 3,
        },
    }
    assignment = {
        **identity,
        "assignment_id": assignment_id_for(identity),
        "route_ready": False,
    }
    return assignment


def test_qwen3_5_family_is_a_first_class_runtime_adapter() -> None:
    adapter = adapter_for_config(CONFIG)
    assert adapter is adapter_for_runtime("qwen3_5")
    assert adapter.layer_count(CONFIG) == 2
    assert adapter.block_prefix_template == "language_model.model.layers.{layer}."
    assert adapter.supported_architectures == (
        "Qwen3_5ForConditionalGeneration",
        "Qwen3_5ForCausalLM",
    )
    assert adapter.runtime_backends == ("mlx", "numpy")
    assert dict(adapter.decoder_tensor_suffixes_by_layer_type) == {
        LINEAR_LAYER_TYPE: QWEN3_5_LINEAR_ATTENTION_TENSOR_SUFFIXES,
        FULL_LAYER_TYPE: QWEN3_5_FULL_ATTENTION_TENSOR_SUFFIXES,
    }
    assert "linear_attn.in_proj_qkv.weight" in QWEN3_5_LINEAR_ATTENTION_TENSOR_SUFFIXES
    assert "self_attn.q_norm.weight" in QWEN3_5_FULL_ATTENTION_TENSOR_SUFFIXES
    assert adapter.excluded_tensor_prefixes == ("vision_tower.",)
    assert dict(adapter.exact_static_component_keys) == {
        "input_embedding": QWEN3_5_EMBEDDING_TENSOR_KEYS,
        "final_norm": QWEN3_5_FINAL_NORM_TENSOR_KEYS,
        "lm_head": QWEN3_5_LM_HEAD_TENSOR_KEYS,
    }


def test_qwen3_5_manifest_owns_exact_language_model_namespace() -> None:
    manifest = _manifest()
    assert mm.verify_manifest_digest(manifest)
    assert manifest["architecture"] == "qwen3_5"
    assert manifest["num_layers"] == 2
    assert manifest["block_prefix_template"] == "language_model.model.layers.{layer}."
    assert manifest["component_tensor_keys"]["input_embedding"] == sorted(
        QWEN3_5_EMBEDDING_TENSOR_KEYS
    )
    assert manifest["component_tensor_keys"]["final_norm"] == sorted(
        QWEN3_5_FINAL_NORM_TENSOR_KEYS
    )
    assert manifest["component_tensor_keys"]["lm_head"] == sorted(
        QWEN3_5_LM_HEAD_TENSOR_KEYS
    )
    assert manifest["tensor_keys_by_layer"]["0"] == sorted(
        _layer_keys(0, QWEN3_5_LINEAR_ATTENTION_TENSOR_SUFFIXES)
    )
    assert manifest["tensor_keys_by_layer"]["1"] == sorted(
        _layer_keys(1, QWEN3_5_FULL_ATTENTION_TENSOR_SUFFIXES)
    )
    runtime_model = manifest["runtime_model"]
    assert runtime_model["architecture"] == "qwen3_5"
    assert set(runtime_model["model_config"]) == QWEN3_5_MODEL_CONFIG_FIELDS
    assert runtime_model["model_config"]["layer_types"] == [
        LINEAR_LAYER_TYPE,
        FULL_LAYER_TYPE,
    ]


def test_qwen3_5_manifest_fails_closed_on_unowned_language_tensors() -> None:
    weight_map = dict(WEIGHT_MAP)
    weight_map["audio_tower.blocks.0.attn.qkv.weight"] = SHARD
    with pytest.raises(ValueError, match="unowned tensor keys"):
        _manifest(weight_map)


def test_qwen3_5_normalized_runtime_round_trips_and_rejects_bad_layer_types() -> None:
    runtime = _runtime()
    assert validate_normalized_mlx_runtime(runtime) == runtime
    assert (
        validate_normalized_runtime(runtime, expected_backend="mlx") == runtime
    )

    bad = _runtime(layer_types=[LINEAR_LAYER_TYPE, "moe_attention"])
    with pytest.raises(ValueError, match="layer_types"):
        validate_normalized_mlx_runtime(bad)

    short = _runtime(layer_types=[LINEAR_LAYER_TYPE])
    with pytest.raises(ValueError, match="layer_types"):
        validate_normalized_mlx_runtime(short)


def test_qwen3_5_loader_accepts_mixed_range_decoder_ownership() -> None:
    manifest = _manifest()
    runtime = _runtime()
    prefixes = [f"language_model.model.layers.{layer}." for layer in range(2)]
    assignment = _assignment(
        manifest,
        start=0,
        end=2,
        components=["input_embedding", "decoder"],
        runtime=runtime,
    )
    expected_keys, aliases = _validate_component_ownership(
        assignment,
        prefixes,
        "language_model.model.",
        "qwen3_5",
        runtime["model_config"],
    )
    assert aliases == {}
    assert expected_keys == assignment["expected_tensor_keys"]
    assert "language_model.model.layers.0.linear_attn.in_proj_qkv.scales" in expected_keys
    assert "language_model.model.layers.1.self_attn.o_proj.biases" in expected_keys


def test_qwen3_5_loader_rejects_layer_types_that_do_not_match_tensor_ownership() -> None:
    manifest = _manifest()
    runtime = _runtime(layer_types=[FULL_LAYER_TYPE, FULL_LAYER_TYPE])
    prefixes = [f"language_model.model.layers.{layer}." for layer in range(2)]
    assignment = _assignment(
        manifest,
        start=0,
        end=2,
        components=["input_embedding", "decoder"],
        runtime=runtime,
    )
    with pytest.raises(Exception, match="decoder tensor ownership"):
        _validate_component_ownership(
            assignment,
            prefixes,
            "language_model.model.",
            "qwen3_5",
            runtime["model_config"],
        )


def test_qwen3_5_runtime_shapes_are_visible_to_stage_pack_schemas() -> None:
    import stage_pack

    runtime = _runtime()
    assert stage_pack._matches_exact_schema(runtime, stage_pack._PACK_RUNTIME_SCHEMA)
    verification_runtime_schema = dict(stage_pack._VERIFICATION_SCHEMA[1])["runtime"]
    assert stage_pack._matches_exact_schema(runtime, verification_runtime_schema)

    extra = copy.deepcopy(runtime)
    extra["model_config"]["mtp_num_hidden_layers"] = 1
    assert not stage_pack._matches_exact_schema(extra, stage_pack._PACK_RUNTIME_SCHEMA)


def test_qwen3_5_loader_validates_full_final_stage_assignment() -> None:
    manifest = _manifest()
    runtime = _runtime("numpy")
    assignment = _assignment(
        manifest,
        start=1,
        end=2,
        components=["decoder", "final_norm", "lm_head"],
        runtime=runtime,
    )
    validated_runtime, _, expected_keys, aliases, start, end, namespace, binding = (
        _validate_assignment(assignment, 17)
    )
    assert validated_runtime["architecture"] == "qwen3_5"
    assert (start, end) == (1, 2)
    assert namespace == "language_model.model."
    assert binding == assignment["control_plane_binding"]
    assert aliases == {}
    assert expected_keys == assignment["expected_tensor_keys"]


def test_qwen3_5_loader_still_rejects_wrong_namespace_ownership() -> None:
    manifest = _manifest()
    runtime = _runtime("numpy")
    assignment = _assignment(
        manifest,
        start=1,
        end=2,
        components=["decoder", "final_norm", "lm_head"],
        runtime=runtime,
    )
    tampered = copy.deepcopy(assignment)
    # Swap one real qwen3_5 tensor for a qwen2-namespaced one and re-bind the id.
    keys = tampered["component_tensor_keys"]["decoder"]
    keys[0] = "model.layers.1.input_layernorm.weight"
    tampered["component_tensor_keys"]["decoder"] = sorted(keys)
    tampered["expected_tensor_keys"] = sorted(
        {
            key
            for component in tampered["component_tensor_keys"].values()
            for key in component
        }
    )
    tampered["assignment_id"] = assignment_id_for(
        {field: tampered[field] for field in (
            "protocol", "deployment_id", "deployment_epoch", "node_id",
            "manifest_digest", "model_id", "resolved_commit", "range",
            "components", "component_tensor_keys", "component_aliases",
            "expected_tensor_prefixes", "expected_tensor_keys", "files",
            "artifact_cache_root", "runtime",
        )}
        | {"control_plane_binding": tampered["control_plane_binding"]}
    )
    with pytest.raises(Exception, match="decoder tensor ownership"):
        _validate_assignment(tampered, 17)


# --- affine 4-bit (U32-packed) source loading ---------------------------------


AFFINE_TEXT_CONFIG = {
    **TEXT_CONFIG,
    "hidden_size": 64,
    "intermediate_size": 128,
    "head_dim": 16,
    "num_key_value_heads": 2,
}
# Node-2-shaped slice of the verified artifact (one full-attention layer +
# final norm + lm_head), rebuilt at sizes where group_size=64 divides exactly
# (head_dim 16 with 4 query heads against a 64-wide q projection).
AFFINE_QUANTIZED_WEIGHT_SHAPES = {
    "mlp.down_proj.weight": (64, 128),
    "mlp.gate_proj.weight": (128, 64),
    "mlp.up_proj.weight": (128, 64),
    "self_attn.k_proj.weight": (32, 64),
    "self_attn.o_proj.weight": (64, 64),
    "self_attn.q_proj.weight": (128, 64),
    "self_attn.v_proj.weight": (32, 64),
}


def _bf16_bytes(values: np.ndarray) -> bytes:
    """Encode bf16-representable float32 values as BF16 little-endian bytes."""

    words = np.asarray(values, dtype=np.float32).view(np.uint32)
    return (words >> np.uint32(16)).astype("<u2").tobytes()


def _pack_affine4bit_words(quants: np.ndarray) -> np.ndarray:
    """Pack uint8 quants (rows, in) into MLX-style uint32 words, LSB nibble first."""

    rows, in_features = quants.shape
    words = quants.reshape(rows, in_features // 8, 8).astype(np.uint32)
    shifts = np.arange(8, dtype=np.uint32) * np.uint32(4)
    return (words << shifts).sum(axis=2, dtype=np.uint32)


def _quantized_weight_entries(
    key: str, rows: int, in_features: int, *, seed: int
) -> tuple[dict[str, tuple[str, tuple[int, ...], bytes]], np.ndarray]:
    """Build affine 4-bit source bytes plus their exact dequantized values."""

    rng = np.random.default_rng(seed)
    quants = rng.integers(0, 16, size=(rows, in_features)).astype(np.uint8)
    groups = in_features // 64
    scales = rng.integers(1, 9, size=(rows, groups)).astype(np.float32) / 64.0
    biases = rng.integers(-8, 9, size=(rows, groups)).astype(np.float32) / 4.0
    expected = (
        quants.astype(np.float32).reshape(rows, groups, 64) * scales[:, :, None]
        + biases[:, :, None]
    ).reshape(rows, in_features)
    words = _pack_affine4bit_words(quants)
    entries = {
        f"{key}.weight": (
            "U32",
            (rows, in_features // 8),
            words.astype("<u4").tobytes(),
        ),
        f"{key}.scales": ("BF16", (rows, groups), _bf16_bytes(scales)),
        f"{key}.biases": ("BF16", (rows, groups), _bf16_bytes(biases)),
    }
    return entries, expected


def _safetensors_container(
    tensors: dict[str, tuple[str, tuple[int, ...], bytes]],
) -> bytes:
    """Serialize exact tensor payloads into a canonical Safetensors file."""

    header: dict[str, dict[str, Any]] = {}
    payload = bytearray()
    for name in sorted(tensors):
        dtype, shape, data = tensors[name]
        header[name] = {
            "dtype": dtype,
            "shape": list(shape),
            "data_offsets": [len(payload), len(payload) + len(data)],
        }
        payload += data
    header_bytes = json.dumps(header, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )
    return struct.pack("<Q", len(header_bytes)) + header_bytes + bytes(payload)


def _affine_manifest() -> dict:
    return mm.compile_model_manifest(
        model_id="mlx-community/Qwen3.8-27B-4bit",
        requested_revision="3e6447f082e89cc7f0bc6e5441afd38dfce760ff",
        resolved_commit="3e6447f082e89cc7f0bc6e5441afd38dfce760ff",
        config={**CONFIG, "text_config": AFFINE_TEXT_CONFIG},
        checkpoint_index={"weight_map": dict(WEIGHT_MAP)},
        file_metadata={SHARD: {"size_bytes": 1024, "sha256": "a" * 64}},
    )


def _affine_runtime(backend: str) -> dict:
    runtime_model = _affine_manifest()["runtime_model"]
    return {
        "backend": backend,
        "dtype": "float32",
        "quantization": "none",
        "architecture": runtime_model["architecture"],
        "model_config": copy.deepcopy(runtime_model["model_config"]),
    }


def _affine_artifact(tmp_path: Path) -> tuple[Path, dict[str, np.ndarray]]:
    """Write the node-2-shaped U32-packed fixture; return expected float tensors."""

    tensors: dict[str, tuple[str, tuple[int, ...], bytes]] = {}
    expected: dict[str, np.ndarray] = {}
    layer_prefix = "language_model.model.layers.1."
    for seed, (suffix, (rows, in_features)) in enumerate(
        AFFINE_QUANTIZED_WEIGHT_SHAPES.items()
    ):
        entries, values = _quantized_weight_entries(
            layer_prefix + suffix[: -len(".weight")],
            rows,
            in_features,
            seed=200 + seed,
        )
        tensors.update(entries)
        expected[layer_prefix + suffix] = values
    for suffix, width in (
        ("input_layernorm.weight", 64),
        ("post_attention_layernorm.weight", 64),
        ("self_attn.q_norm.weight", 16),
        ("self_attn.k_norm.weight", 16),
    ):
        values = ((np.arange(width) % 7) + 1).astype(np.float32) / 8.0
        tensors[layer_prefix + suffix] = ("BF16", (width,), _bf16_bytes(values))
        expected[layer_prefix + suffix] = values
    norm_values = ((np.arange(64) % 5) + 1).astype(np.float32) / 4.0
    tensors["language_model.model.norm.weight"] = (
        "BF16",
        (64,),
        _bf16_bytes(norm_values),
    )
    expected["language_model.model.norm.weight"] = norm_values
    entries, values = _quantized_weight_entries(
        "language_model.lm_head", 32, 64, seed=300
    )
    tensors.update(entries)
    expected["language_model.lm_head.weight"] = values
    artifact = tmp_path / SHARD
    artifact.write_bytes(_safetensors_container(tensors))
    return artifact, expected


def _affine_assignment(tmp_path: Path, artifact: Path, runtime: dict) -> dict:
    assignment = _assignment(
        _affine_manifest(),
        start=1,
        end=2,
        components=["decoder", "final_norm", "lm_head"],
        runtime=runtime,
    )
    size = artifact.stat().st_size
    assignment["files"] = [
        {
            "path": artifact.name,
            "size_bytes": size,
            "content_digest": "sha256:" + sha256_file(artifact),
        }
    ]
    assignment["artifact_cache_root"] = str(tmp_path)
    assignment["assignment_id"] = assignment_id_for(assignment)
    return assignment


def _affine_report(tmp_path: Path, artifact: Path, assignment: dict) -> dict:
    size = artifact.stat().st_size
    digest = "sha256:" + sha256_file(artifact)
    return {
        "protocol": "mycelium.artifact_verification_report.v1",
        "deployment_id": assignment["deployment_id"],
        "deployment_epoch": assignment["deployment_epoch"],
        "assignment_id": assignment["assignment_id"],
        "node_id": assignment["node_id"],
        "manifest_digest": assignment["manifest_digest"],
        "resolved_commit": assignment["resolved_commit"],
        "range": copy.deepcopy(assignment["range"]),
        "artifact_cache_root": str(tmp_path),
        "resolved_artifact_cache_root": str(tmp_path.resolve()),
        "verified_files": [
            {
                "path": artifact.name,
                "local_path": str(artifact.resolve()),
                "size_bytes": size,
                "content_digest": digest,
                "cache_hit": True,
                "tensor_count": 29,
            }
        ],
        "verified_tensor_prefixes": list(assignment["expected_tensor_prefixes"]),
        "verified_tensor_count": len(set(assignment["expected_tensor_keys"])),
        "expected_bytes": size,
        "network_download_bytes": 0,
        "cache_hit_bytes": size,
        "ready_for_load": True,
        "route_ready": False,
        "claim_boundary": (
            "test fixture; affine 4-bit sources verified, layers not loaded"
        ),
    }


def test_qwen3_5_affine4bit_dequantize_matches_hand_computed_reference() -> None:
    rows, in_features = 2, 64
    column = np.arange(in_features, dtype=np.uint8)
    quants = np.stack([column % 16, 15 - (column % 16)]).astype(np.uint8)
    scales = np.array([[0.5], [0.25]], dtype=np.float32)
    biases = np.array([[-3.0], [1.5]], dtype=np.float32)
    weight_key = "language_model.lm_head.weight"
    scales_key = "language_model.lm_head.scales"
    biases_key = "language_model.lm_head.biases"
    words_bytes = _pack_affine4bit_words(quants).astype("<u4").tobytes()
    scales_bytes = _bf16_bytes(scales)
    biases_bytes = _bf16_bytes(biases)
    blob = words_bytes + scales_bytes + biases_bytes
    header = {
        weight_key: {
            "dtype": "U32",
            "shape": [rows, in_features // 8],
            "data_offsets": [0, len(words_bytes)],
        },
        scales_key: {
            "dtype": "BF16",
            "shape": [rows, 1],
            "data_offsets": [len(words_bytes), len(words_bytes) + len(scales_bytes)],
        },
        biases_key: {
            "dtype": "BF16",
            "shape": [rows, 1],
            "data_offsets": [len(words_bytes) + len(scales_bytes), len(blob)],
        },
    }

    loaded = runtime_loader._load_numpy_safetensors(
        io.BytesIO(blob),
        header,
        0,
        {weight_key, scales_key, biases_key},
        np.dtype("float32"),
    )

    value = loaded[weight_key]
    assert value.dtype == np.float32
    assert value.shape == (rows, in_features)
    assert value.flags.writeable is False
    # Hand-computed reference: value[row, col] = quant * scale + bias.
    assert value[0, 0] == np.float32(0 * 0.5 - 3.0)
    assert value[0, 1] == np.float32(1 * 0.5 - 3.0)
    assert value[0, 15] == np.float32(15 * 0.5 - 3.0)
    assert value[0, 63] == np.float32(15 * 0.5 - 3.0)
    assert value[1, 0] == np.float32(15 * 0.25 + 1.5)
    assert value[1, 15] == np.float32(0 * 0.25 + 1.5)
    assert value[1, 63] == np.float32(0 * 0.25 + 1.5)
    np.testing.assert_array_equal(value, quants.astype(np.float32) * scales + biases)
    np.testing.assert_array_equal(loaded[scales_key], scales)
    np.testing.assert_array_equal(loaded[biases_key], biases)

    # A U32 weight without its assigned companions must fail closed.
    with pytest.raises(RuntimeLoadError, match="companion"):
        runtime_loader._load_numpy_safetensors(
            io.BytesIO(blob),
            header,
            0,
            {weight_key, biases_key},
            np.dtype("float32"),
        )


def test_qwen3_5_affine4bit_unpack_matches_mlx_quantize_layout() -> None:
    rows, in_features = 4, 128
    rng = np.random.default_rng(20260913)
    source = mx.array(rng.standard_normal((rows, in_features)).astype(np.float32))
    packed, scales, biases = mx.quantize(source, group_size=64, bits=4)
    mx.eval(packed, scales, biases)
    reference = np.array(
        mx.dequantize(packed, scales, biases, group_size=64, bits=4)
    )
    words = np.array(packed).astype("<u4")
    scales_np = np.array(scales, dtype=np.float32)
    biases_np = np.array(biases, dtype=np.float32)
    weight_key = "language_model.model.layers.1.mlp.gate_proj.weight"
    scales_key = "language_model.model.layers.1.mlp.gate_proj.scales"
    biases_key = "language_model.model.layers.1.mlp.gate_proj.biases"
    words_bytes = words.tobytes()
    scales_bytes = scales_np.astype("<f4").tobytes()
    biases_bytes = biases_np.astype("<f4").tobytes()
    blob = words_bytes + scales_bytes + biases_bytes
    header = {
        weight_key: {
            "dtype": "U32",
            "shape": [rows, in_features // 8],
            "data_offsets": [0, len(words_bytes)],
        },
        scales_key: {
            "dtype": "F32",
            "shape": list(scales_np.shape),
            "data_offsets": [len(words_bytes), len(words_bytes) + len(scales_bytes)],
        },
        biases_key: {
            "dtype": "F32",
            "shape": list(biases_np.shape),
            "data_offsets": [len(words_bytes) + len(scales_bytes), len(blob)],
        },
    }

    loaded = runtime_loader._load_numpy_safetensors(
        io.BytesIO(blob),
        header,
        0,
        {weight_key, scales_key, biases_key},
        np.dtype("float32"),
    )

    value = loaded[weight_key]
    assert value.shape == (rows, in_features)
    np.testing.assert_allclose(value, reference, rtol=1e-4, atol=1e-5)


def test_qwen3_5_affine4bit_loader_reads_bounded_row_chunks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Reader(io.BytesIO):
        maximum_read = 0

        def read(self, size: int = -1) -> bytes:
            self.maximum_read = max(self.maximum_read, size)
            return super().read(size)

    monkeypatch.setattr(
        runtime_loader,
        "_AFFINE4BIT_DEQUANT_CHUNK_FLOAT_BYTES",
        64,
    )
    rows, in_features = 8, 64
    entries, expected = _quantized_weight_entries(
        "model.layers.0.mlp.gate_proj", rows, in_features, seed=7
    )
    blob = b""
    header: dict[str, dict[str, Any]] = {}
    for name, (dtype, shape, data) in entries.items():
        header[name] = {
            "dtype": dtype,
            "shape": list(shape),
            "data_offsets": [len(blob), len(blob) + len(data)],
        }
        blob += data
    reader = Reader(blob)

    loaded = runtime_loader._load_numpy_safetensors(
        reader, header, 0, set(header), np.dtype("float32")
    )

    value = loaded["model.layers.0.mlp.gate_proj.weight"]
    np.testing.assert_array_equal(value, expected)
    assert value.flags.writeable is False
    # One packed 32-byte row per read: a whole-tensor read would be 8 * 32.
    assert reader.maximum_read == 32


@pytest.mark.parametrize("backend", ["numpy", "mlx"])
def test_qwen3_5_affine4bit_load_path_materializes_quantized_sources_and_passes_execution_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, backend: str
) -> None:
    artifact, expected = _affine_artifact(tmp_path)
    runtime = _affine_runtime(backend)
    assignment = _affine_assignment(tmp_path, artifact, runtime)
    report = _affine_report(tmp_path, artifact, assignment)

    captured: dict[str, dict[str, Any]] = {}
    original = runtime_loader._load_exact_tensors

    def spy(*args: Any, **kwargs: Any) -> dict[str, Any]:
        tensors = original(*args, **kwargs)
        captured["tensors"] = tensors
        return tensors

    monkeypatch.setattr(runtime_loader, "_load_exact_tensors", spy)

    stage = load_assignment_stage(assignment, report, load_generation=17)

    tensors = captured["tensors"]
    assert set(tensors) == set(assignment["expected_tensor_keys"])
    for key in (
        "language_model.lm_head.weight",
        "language_model.model.layers.1.mlp.down_proj.weight",
        "language_model.model.layers.1.self_attn.q_proj.weight",
    ):
        value = tensors[key]
        if backend == "mlx":
            assert str(value.dtype) == "mlx.core.float32"
            np.testing.assert_array_equal(np.array(value), expected[key])
        else:
            assert value.dtype == np.float32
            assert value.flags.writeable is False
            np.testing.assert_array_equal(value, expected[key])
    assert tensors["language_model.model.norm.weight"].dtype is not None
    assert tensors["language_model.lm_head.scales"].dtype is not None
    # The hybrid execution kernel now consumes the materialized slice: the
    # load passes the gate for the full-attention layer and returns a stage.
    assert list(stage.proof["probe_shape"]) == [1, 3, 32]
    assert stage.proof["probe_digest"].startswith("sha256:")
    assert stage.proof["route_ready"] is False


# --- hybrid linear/full attention execution kernel ----------------------------


KERNEL_TEXT_CONFIG = {
    **TEXT_CONFIG,
    "hidden_size": 64,
    "intermediate_size": 128,
    "head_dim": 16,
    "num_key_value_heads": 2,
    "linear_key_head_dim": 16,
    "linear_num_key_heads": 2,
    "linear_value_head_dim": 16,
    "linear_num_value_heads": 4,
}
# Full tiny hybrid model (both layer types + embedding + final norm + lm_head),
# rebuilt at sizes where group_size=64 divides exactly (hidden 64, value 64,
# intermediate 128): 62 tensors (30 linear-layer + 25 full-layer + 7 static).
KERNEL_LINEAR_WEIGHT_SHAPES = {
    "linear_attn.in_proj_qkv.weight": (128, 64),
    "linear_attn.in_proj_z.weight": (64, 64),
    "linear_attn.in_proj_a.weight": (4, 64),
    "linear_attn.in_proj_b.weight": (4, 64),
    "linear_attn.out_proj.weight": (64, 64),
    "mlp.down_proj.weight": (64, 128),
    "mlp.gate_proj.weight": (128, 64),
    "mlp.up_proj.weight": (128, 64),
}
KERNEL_FULL_WEIGHT_SHAPES = {
    "self_attn.k_proj.weight": (32, 64),
    "self_attn.o_proj.weight": (64, 64),
    "self_attn.q_proj.weight": (128, 64),
    "self_attn.v_proj.weight": (32, 64),
    "mlp.down_proj.weight": (64, 128),
    "mlp.gate_proj.weight": (128, 64),
    "mlp.up_proj.weight": (128, 64),
}
KERNEL_STATIC_KEYS = (
    "language_model.lm_head.biases",
    "language_model.lm_head.scales",
    "language_model.lm_head.weight",
    "language_model.model.embed_tokens.biases",
    "language_model.model.embed_tokens.scales",
    "language_model.model.embed_tokens.weight",
    "language_model.model.norm.weight",
)
KERNEL_LINEAR_FLOAT_SUFFIXES = (
    "input_layernorm.weight",
    "linear_attn.A_log",
    "linear_attn.conv1d.weight",
    "linear_attn.dt_bias",
    "linear_attn.norm.weight",
    "post_attention_layernorm.weight",
)
KERNEL_FULL_FLOAT_SUFFIXES = (
    "input_layernorm.weight",
    "post_attention_layernorm.weight",
    "self_attn.k_norm.weight",
    "self_attn.q_norm.weight",
)


def _kernel_weight_map(
    *,
    linear_shapes: dict[str, tuple[int, int]] | None = None,
    full_shapes: dict[str, tuple[int, int]] | None = None,
) -> dict[str, str]:
    weight_map: dict[str, str] = {}
    for key_root, shapes in (
        ("language_model.model.layers.0.", linear_shapes or KERNEL_LINEAR_WEIGHT_SHAPES),
        ("language_model.model.layers.1.", full_shapes or KERNEL_FULL_WEIGHT_SHAPES),
    ):
        for suffix in shapes:
            base = key_root + suffix[: -len(".weight")]
            weight_map[base + ".weight"] = SHARD
            weight_map[base + ".scales"] = SHARD
            weight_map[base + ".biases"] = SHARD
    for key in KERNEL_STATIC_KEYS:
        weight_map[key] = SHARD
    for prefix, suffixes in (
        ("language_model.model.layers.0.", KERNEL_LINEAR_FLOAT_SUFFIXES),
        ("language_model.model.layers.1.", KERNEL_FULL_FLOAT_SUFFIXES),
    ):
        for suffix in suffixes:
            weight_map[prefix + suffix] = SHARD
    return weight_map


def _kernel_manifest(
    *,
    linear_shapes: dict[str, tuple[int, int]] | None = None,
    full_shapes: dict[str, tuple[int, int]] | None = None,
) -> dict:
    return mm.compile_model_manifest(
        model_id="mlx-community/Qwen3.8-27B-4bit",
        requested_revision="3e6447f082e89cc7f0bc6e5441afd38dfce760ff",
        resolved_commit="3e6447f082e89cc7f0bc6e5441afd38dfce760ff",
        config={**CONFIG, "text_config": KERNEL_TEXT_CONFIG},
        checkpoint_index={
            "weight_map": _kernel_weight_map(
                linear_shapes=linear_shapes, full_shapes=full_shapes
            )
        },
        file_metadata={SHARD: {"size_bytes": 1024, "sha256": "a" * 64}},
    )


def _kernel_artifact(
    tmp_path: Path,
    *,
    linear_shapes: dict[str, tuple[int, int]] | None = None,
    full_shapes: dict[str, tuple[int, int]] | None = None,
) -> tuple[Path, dict[str, np.ndarray]]:
    """Write the tiny full hybrid artifact; return (path, expected float tensors)."""

    tensors: dict[str, tuple[str, tuple[int, ...], bytes]] = {}
    expected: dict[str, np.ndarray] = {}
    seed = 5000

    def add_quantized(key_root: str, rows: int, columns: int) -> None:
        nonlocal seed
        entries, values = _quantized_weight_entries(
            key_root, rows, columns, seed=seed
        )
        seed += 1
        tensors.update(entries)
        expected[key_root + ".weight"] = values

    def add_float(key: str, values: np.ndarray) -> None:
        tensors[key] = ("BF16", values.shape, _bf16_bytes(values))
        expected[key] = values

    add_quantized("language_model.model.embed_tokens", 32, 64)
    add_quantized("language_model.lm_head", 32, 64)
    add_float(
        "language_model.model.norm.weight",
        ((np.arange(64) % 5) + 1).astype(np.float32) / 4.0,
    )

    linear = "language_model.model.layers.0."
    for suffix, (rows, columns) in (
        linear_shapes or KERNEL_LINEAR_WEIGHT_SHAPES
    ).items():
        add_quantized(linear + suffix[: -len(".weight")], rows, columns)
    for suffix, width in (
        ("input_layernorm.weight", 64),
        ("post_attention_layernorm.weight", 64),
        ("linear_attn.norm.weight", 16),
    ):
        add_float(
            linear + suffix, ((np.arange(width) % 7) + 1).astype(np.float32) / 8.0
        )
    add_float(
        linear + "linear_attn.A_log",
        -np.arange(4, dtype=np.float32) / 2.0 - 0.5,
    )
    add_float(
        linear + "linear_attn.dt_bias",
        (np.arange(4, dtype=np.float32) % 3) - 1.0,
    )
    conv_rng = np.random.default_rng(4242)
    add_float(
        linear + "linear_attn.conv1d.weight",
        conv_rng.integers(-8, 9, size=(128, 4, 1)).astype(np.float32) / 16.0,
    )

    full = "language_model.model.layers.1."
    for suffix, (rows, columns) in (
        full_shapes or KERNEL_FULL_WEIGHT_SHAPES
    ).items():
        add_quantized(full + suffix[: -len(".weight")], rows, columns)
    for suffix, width in (
        ("input_layernorm.weight", 64),
        ("post_attention_layernorm.weight", 64),
        ("self_attn.q_norm.weight", 16),
        ("self_attn.k_norm.weight", 16),
    ):
        add_float(
            full + suffix, ((np.arange(width) % 7) + 1).astype(np.float32) / 8.0
        )

    artifact = tmp_path / SHARD
    artifact.write_bytes(_safetensors_container(tensors))
    return artifact, expected


def _kernel_runtime(
    backend: str,
    *,
    linear_shapes: dict[str, tuple[int, int]] | None = None,
    full_shapes: dict[str, tuple[int, int]] | None = None,
) -> dict:
    runtime_model = _kernel_manifest(
        linear_shapes=linear_shapes, full_shapes=full_shapes
    )["runtime_model"]
    return {
        "backend": backend,
        "dtype": "float32",
        "quantization": "none",
        "architecture": runtime_model["architecture"],
        "model_config": copy.deepcopy(runtime_model["model_config"]),
    }


def _kernel_assignment(
    tmp_path: Path,
    artifact: Path,
    runtime: dict,
    *,
    start: int,
    end: int,
    components: list[str],
    linear_shapes: dict[str, tuple[int, int]] | None = None,
    full_shapes: dict[str, tuple[int, int]] | None = None,
) -> dict:
    assignment = _assignment(
        _kernel_manifest(linear_shapes=linear_shapes, full_shapes=full_shapes),
        start=start,
        end=end,
        components=components,
        runtime=runtime,
    )
    size = artifact.stat().st_size
    assignment["files"] = [
        {
            "path": artifact.name,
            "size_bytes": size,
            "content_digest": "sha256:" + sha256_file(artifact),
        }
    ]
    assignment["artifact_cache_root"] = str(tmp_path)
    assignment["assignment_id"] = assignment_id_for(assignment)
    return assignment


def _kernel_report(tmp_path: Path, artifact: Path, assignment: dict) -> dict:
    size = artifact.stat().st_size
    digest = "sha256:" + sha256_file(artifact)
    return {
        "protocol": "mycelium.artifact_verification_report.v1",
        "deployment_id": assignment["deployment_id"],
        "deployment_epoch": assignment["deployment_epoch"],
        "assignment_id": assignment["assignment_id"],
        "node_id": assignment["node_id"],
        "manifest_digest": assignment["manifest_digest"],
        "resolved_commit": assignment["resolved_commit"],
        "range": copy.deepcopy(assignment["range"]),
        "artifact_cache_root": str(tmp_path),
        "resolved_artifact_cache_root": str(tmp_path.resolve()),
        "verified_files": [
            {
                "path": artifact.name,
                "local_path": str(artifact.resolve()),
                "size_bytes": size,
                "content_digest": digest,
                "cache_hit": True,
                "tensor_count": len(assignment["expected_tensor_keys"]),
            }
        ],
        "verified_tensor_prefixes": list(assignment["expected_tensor_prefixes"]),
        "verified_tensor_count": len(set(assignment["expected_tensor_keys"])),
        "expected_bytes": size,
        "network_download_bytes": 0,
        "cache_hit_bytes": size,
        "ready_for_load": True,
        "route_ready": False,
        "claim_boundary": "test fixture; hybrid execution gate evidence only",
    }


@pytest.mark.parametrize("backend", ["numpy", "mlx"])
def test_qwen3_5_execution_gate_passes_for_hybrid_layers(
    tmp_path: Path, backend: str
) -> None:
    artifact, _ = _kernel_artifact(tmp_path)
    runtime = _kernel_runtime(backend)
    assignment = _kernel_assignment(
        tmp_path,
        artifact,
        runtime,
        start=0,
        end=2,
        components=["decoder", "final_norm", "lm_head"],
    )
    report = _kernel_report(tmp_path, artifact, assignment)

    stage = load_assignment_stage(assignment, report, load_generation=31)

    proof = stage.proof
    assert proof["runtime"]["architecture"] == "qwen3_5"
    assert proof["loaded_range"] == {
        "start_layer": 0,
        "end_layer_exclusive": 2,
        "layer_count": 2,
    }
    assert list(proof["loaded_tensor_keys"]) == sorted(
        assignment["expected_tensor_keys"]
    )
    assert len(proof["loaded_tensor_keys"]) == 59
    assert list(proof["probe_shape"]) == [1, 3, 32]
    assert proof["probe_digest"].startswith("sha256:")
    assert proof["route_ready"] is False

    # The probe is deterministic for the same authenticated evidence.
    again = load_assignment_stage(assignment, report, load_generation=31)
    assert again.proof["probe_digest"] == proof["probe_digest"]

    # Both hybrid layer kinds were materialized at the runtime dtype.
    for key in (
        "language_model.model.layers.0.linear_attn.in_proj_qkv.weight",
        "language_model.model.layers.0.linear_attn.conv1d.weight",
        "language_model.model.layers.1.self_attn.q_proj.weight",
        "language_model.model.layers.1.self_attn.q_norm.weight",
    ):
        value = stage.tensors[key]
        if backend == "mlx":
            assert str(value.dtype) == "mlx.core.float32"
        else:
            assert value.dtype == np.float32


@pytest.mark.parametrize("backend", ["numpy", "mlx"])
def test_qwen3_5_execution_gate_passes_for_entry_stage(
    tmp_path: Path, backend: str
) -> None:
    artifact, _ = _kernel_artifact(tmp_path)
    runtime = _kernel_runtime(backend)
    assignment = _kernel_assignment(
        tmp_path,
        artifact,
        runtime,
        start=0,
        end=1,
        components=["input_embedding", "decoder"],
    )
    report = _kernel_report(tmp_path, artifact, assignment)

    stage = load_assignment_stage(assignment, report, load_generation=32)

    assert len(stage.proof["loaded_tensor_keys"]) == 33
    # The entry stage owns no lm_head: the probe returns hidden states.
    assert list(stage.proof["probe_shape"]) == [1, 3, 64]
    assert stage.proof["route_ready"] is False


def test_qwen3_5_execution_gate_fails_closed_on_unsupported_rotary_factor(
    tmp_path: Path,
) -> None:
    artifact, _ = _kernel_artifact(tmp_path)
    runtime = _kernel_runtime("numpy")
    runtime["model_config"]["partial_rotary_factor"] = 0.1  # int(16*0.1)=1, odd
    assignment = _kernel_assignment(
        tmp_path,
        artifact,
        runtime,
        start=0,
        end=2,
        components=["decoder", "final_norm", "lm_head"],
    )
    report = _kernel_report(tmp_path, artifact, assignment)

    with pytest.raises(RuntimeLoadError, match="partial rotary factor"):
        load_assignment_stage(assignment, report, load_generation=33)


def test_qwen3_5_execution_gate_fails_closed_on_incompatible_linear_heads(
    tmp_path: Path,
) -> None:
    artifact, _ = _kernel_artifact(tmp_path)
    runtime = _kernel_runtime("numpy")
    runtime["model_config"]["linear_num_value_heads"] = 3  # 3 % 2 != 0
    assignment = _kernel_assignment(
        tmp_path,
        artifact,
        runtime,
        start=0,
        end=2,
        components=["decoder", "final_norm", "lm_head"],
    )
    report = _kernel_report(tmp_path, artifact, assignment)

    with pytest.raises(RuntimeLoadError, match="linear attention heads"):
        load_assignment_stage(assignment, report, load_generation=34)


def test_qwen3_5_execution_gate_fails_closed_on_tensor_shape_mismatch(
    tmp_path: Path,
) -> None:
    mismatched_full = {**KERNEL_FULL_WEIGHT_SHAPES, "self_attn.q_proj.weight": (129, 64)}
    artifact, _ = _kernel_artifact(tmp_path, full_shapes=mismatched_full)
    runtime = _kernel_runtime("numpy", full_shapes=mismatched_full)
    assignment = _kernel_assignment(
        tmp_path,
        artifact,
        runtime,
        start=1,
        end=2,
        components=["decoder", "final_norm", "lm_head"],
        full_shapes=mismatched_full,
    )
    report = _kernel_report(tmp_path, artifact, assignment)

    with pytest.raises(RuntimeLoadError, match="shape mismatch"):
        load_assignment_stage(assignment, report, load_generation=35)


def test_qwen3_5_hybrid_kernels_agree_across_backends(tmp_path: Path) -> None:
    artifact, expected = _kernel_artifact(tmp_path)
    config = _kernel_runtime("numpy")["model_config"]
    ids = np.array([[0, 1, 2]], dtype=np.int64)

    np_tensors: dict[str, np.ndarray] = {
        key: np.array(value) for key, value in expected.items()
    }
    hidden = np_tensors["language_model.model.embed_tokens.weight"][ids]
    for layer in range(2):
        hidden = numpy_runtime._qwen3_5_block(
            hidden,
            np_tensors,
            f"language_model.model.layers.{layer}.",
            config,
            config["layer_types"][layer],
        )
    hidden = numpy_runtime._rms_norm(
        hidden,
        np_tensors["language_model.model.norm.weight"],
        float(config["rms_norm_epsilon"]),
    )
    numpy_out = numpy_runtime._qwen2_linear(
        hidden, np_tensors["language_model.lm_head.weight"]
    )

    mlx_tensors = {key: mx.array(value) for key, value in expected.items()}
    hidden = mlx_tensors["language_model.model.embed_tokens.weight"][
        mx.array(ids, dtype=mx.int32)
    ]
    for layer in range(2):
        hidden = runtime_loader._qwen3_5_block(
            hidden,
            mlx_tensors,
            f"language_model.model.layers.{layer}.",
            config,
            mx,
            config["layer_types"][layer],
        )
    hidden = runtime_loader._rms_norm(
        hidden,
        mlx_tensors["language_model.model.norm.weight"],
        float(config["rms_norm_epsilon"]),
        mx,
    )
    mlx_out = np.array(
        runtime_loader._qwen2_linear(
            hidden, mlx_tensors["language_model.lm_head.weight"], mx
        )
    )

    assert mlx_out.shape == numpy_out.shape == (1, 3, 32)
    assert np.isfinite(mlx_out).all() and np.isfinite(numpy_out).all()
    np.testing.assert_allclose(mlx_out, numpy_out, rtol=1e-3, atol=1e-4)
