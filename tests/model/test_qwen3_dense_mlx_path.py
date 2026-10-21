# SPDX-License-Identifier: AGPL-3.0-only
"""Qwen3 dense models run end to end on the MLX stage path.

Real Qwen3 dense checkpoints do not all keep ``num_attention_heads * head_dim``
equal to ``hidden_size``: Qwen3-0.6B (16 x 128 = 2048 vs 1024), Qwen3-4B
(32 x 128 = 4096 vs 2560) and Qwen3-32B (64 x 128 = 8192 vs 5120) project
attention wider than the residual stream.  These tests use a tiny synthetic
checkpoint with exactly that property and drive it through the production
manifest compiler, the stage sharder, the assignment loader, and a two-stage
``MLXRuntimePort`` chain with stage-local KV.
"""

from __future__ import annotations

import json
from pathlib import Path

import mlx.core as mx
import numpy as np
import pytest

from model_manifest import compile_model_manifest, verify_manifest_digest
from mycelium_qualification.physical_deployment import (
    LocalModelSource,
    build_execution_graph,
    compile_local_model_manifest,
    prepare_physical_deployment,
)
from mycelium_router.contracts import ExecutionGraph, HopWorkItem, RuntimeBatchKey
from mycelium_router.decoding import quantized_greedy_token_id
from mycelium_router.mlx_runtime import MLXRuntimePort
from mycelium_router.payloads import encode_token_ids
from numpy_runtime import NumpyQwen3Runtime
from runtime_contracts import (
    normalize_qwen2_model_config,
    normalize_qwen3_model_config,
)
from runtime_loader import load_assignment_stage

COMMIT = "c" * 40
MODEL_ID = "synthetic/Qwen3-wide-attention"

# hidden 8, 4 query heads x head_dim 4 = 16 (wider than hidden), 2 KV heads.
_CONFIG = {
    "architectures": ["Qwen3ForCausalLM"],
    "model_type": "qwen3",
    "torch_dtype": "bfloat16",
    "num_hidden_layers": 4,
    "hidden_size": 8,
    "num_attention_heads": 4,
    "num_key_value_heads": 2,
    "head_dim": 4,
    "intermediate_size": 16,
    "vocab_size": 48,
    "max_position_embeddings": 64,
    "rms_norm_eps": 1e-6,
    "rope_theta": 1_000_000,
    "hidden_act": "silu",
    "tie_word_embeddings": True,
    "attention_bias": False,
    "attention_dropout": 0.0,
    "rope_scaling": None,
    "use_sliding_window": False,
    "sliding_window": None,
    "max_window_layers": 4,
}

_QWEN3_0_6B_CONFIG = {
    "architectures": ["Qwen3ForCausalLM"],
    "model_type": "qwen3",
    "attention_bias": False,
    "attention_dropout": 0.0,
    "head_dim": 128,
    "hidden_act": "silu",
    "hidden_size": 1024,
    "intermediate_size": 3072,
    "max_position_embeddings": 40960,
    "max_window_layers": 28,
    "num_attention_heads": 16,
    "num_hidden_layers": 28,
    "num_key_value_heads": 8,
    "rms_norm_eps": 1e-06,
    "rope_scaling": None,
    "rope_theta": 1000000,
    "sliding_window": None,
    "tie_word_embeddings": True,
    "torch_dtype": "bfloat16",
    "use_sliding_window": False,
    "vocab_size": 151936,
}


def _tensors(config: dict = _CONFIG, seed: int = 20261013) -> dict[str, np.ndarray]:
    rng = np.random.default_rng(seed)
    hidden = config["hidden_size"]
    q_dim = config["num_attention_heads"] * config["head_dim"]
    kv_dim = config["num_key_value_heads"] * config["head_dim"]
    inner = config["intermediate_size"]
    head_dim = config["head_dim"]
    tensors = {
        "model.embed_tokens.weight": rng.normal(
            0.0, 0.5, (config["vocab_size"], hidden)
        ),
        "model.norm.weight": rng.normal(1.0, 0.05, (hidden,)),
    }
    shapes = {
        "input_layernorm.weight": (hidden,),
        "self_attn.q_proj.weight": (q_dim, hidden),
        "self_attn.q_norm.weight": (head_dim,),
        "self_attn.k_proj.weight": (kv_dim, hidden),
        "self_attn.k_norm.weight": (head_dim,),
        "self_attn.v_proj.weight": (kv_dim, hidden),
        "self_attn.o_proj.weight": (hidden, q_dim),
        "post_attention_layernorm.weight": (hidden,),
        "mlp.gate_proj.weight": (inner, hidden),
        "mlp.up_proj.weight": (inner, hidden),
        "mlp.down_proj.weight": (hidden, inner),
    }
    for layer in range(config["num_hidden_layers"]):
        for suffix, shape in shapes.items():
            if suffix.endswith("norm.weight"):
                value = rng.normal(1.0, 0.05, shape)
            else:
                value = rng.normal(0.0, 0.35, shape)
            tensors[f"model.layers.{layer}.{suffix}"] = value
    return {name: value.astype(np.float32) for name, value in tensors.items()}


def _snapshot(tmp_path: Path) -> Path:
    root = tmp_path / "source"
    root.mkdir()
    (root / "config.json").write_text(json.dumps(_CONFIG), encoding="utf-8")
    mx.save_safetensors(
        str(root / "model.safetensors"),
        {name: mx.array(value) for name, value in _tensors().items()},
        metadata={"format": "pt"},
    )
    return root


def _numpy_runtime(quantization: str = "none") -> dict[str, object]:
    return {
        "backend": "numpy",
        "dtype": "float32",
        "quantization": quantization,
        "architecture": "qwen3",
        "model_config": normalize_qwen3_model_config(
            _CONFIG, expected_layers=_CONFIG["num_hidden_layers"]
        ),
    }


def _phase(
    ports: tuple[MLXRuntimePort, ...],
    graph: ExecutionGraph,
    token_ids: tuple[int, ...],
    *,
    phase: str,
    token_index: int,
    position: int,
    terminal: bool = False,
) -> int:
    payload = encode_token_ids(token_ids)
    span = len(token_ids) if phase == "PREFILL" else 1
    for hop, (port, stage) in enumerate(zip(ports, graph.stages, strict=True)):
        placement = stage.placements[0]
        result = port.execute(
            HopWorkItem(
                request_id="qwen3-request",
                path_id="qwen3-path",
                path_attempt=0,
                phase=phase,
                token_index=token_index,
                hop_index=hop,
                placement_id=placement.placement_id,
                qos_class="interactive",
                deficit_ratio=0.0,
                enqueued_at=0.0,
                idempotency_key=f"qwen3:{phase}:{token_index}:{hop}",
                payload=payload,
                batch_key=RuntimeBatchKey(
                    deployment_id=graph.deployment_id,
                    deployment_epoch=graph.deployment_epoch,
                    model_commit=graph.resolved_commit,
                    manifest_digest=graph.manifest_digest,
                    placement_id=placement.placement_id,
                    assignment_id=placement.assignment_id,
                    stage_signature=placement.stage_signature,
                    load_proof_digest=placement.load_proof_digest,
                    runtime_backend=placement.runtime_backend,
                    phase=phase,
                    hidden_size=graph.hidden_size,
                    activation_bytes=graph.activation_bytes,
                    token_span=span,
                ),
                position=position,
                terminal=terminal,
                lease_expires_at=1_000_000_000_000.0,
            )
        )
        assert result.success, result.failure_reason
        if hop + 1 < len(graph.stages):
            payload = result.payload
        else:
            return int(result.token_id)
    raise AssertionError("final stage missing")


def test_qwen3_normalizes_attention_wider_than_hidden() -> None:
    normalized = normalize_qwen3_model_config(
        _QWEN3_0_6B_CONFIG, expected_layers=28
    )

    assert normalized["n_embd"] == 1024
    assert normalized["n_head"] == 16
    assert normalized["n_kv_head"] == 8
    assert normalized["head_dim"] == 128
    assert normalized["tie_word_embeddings"] is True


def test_qwen3_32b_shape_normalizes() -> None:
    config = {
        **_QWEN3_0_6B_CONFIG,
        "hidden_size": 5120,
        "num_attention_heads": 64,
        "num_key_value_heads": 8,
        "intermediate_size": 25600,
        "num_hidden_layers": 64,
        "max_window_layers": 64,
        "tie_word_embeddings": False,
    }

    normalized = normalize_qwen3_model_config(config, expected_layers=64)

    assert normalized["n_head"] * normalized["head_dim"] == 8192
    assert normalized["n_embd"] == 5120


def test_qwen3_still_refuses_kv_heads_that_do_not_divide_query_heads() -> None:
    with pytest.raises(ValueError, match="incompatible attention heads"):
        normalize_qwen3_model_config(
            {**_QWEN3_0_6B_CONFIG, "num_key_value_heads": 6}, expected_layers=28
        )


def test_qwen2_keeps_the_hidden_equals_heads_contract() -> None:
    qwen2 = {
        key: value
        for key, value in _QWEN3_0_6B_CONFIG.items()
        if key not in {"attention_bias", "attention_dropout", "rope_scaling"}
    }
    qwen2["model_type"] = "qwen2"

    with pytest.raises(ValueError, match="incompatible attention heads"):
        normalize_qwen2_model_config(qwen2, expected_layers=28)


def test_qwen3_0_6b_shaped_tied_manifest_compiles() -> None:
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
        **{
            f"model.layers.{layer}.{suffix}": "model.safetensors"
            for layer in range(28)
            for suffix in suffixes
        },
    }

    manifest = compile_model_manifest(
        model_id="Qwen/Qwen3-0.6B",
        requested_revision="main",
        resolved_commit=COMMIT,
        config=_QWEN3_0_6B_CONFIG,
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
    assert manifest["runtime_model"]["model_config"]["head_dim"] == 128


def test_qwen3_local_source_manifest_compiles(tmp_path: Path) -> None:
    manifest = compile_local_model_manifest(
        LocalModelSource(
            root=_snapshot(tmp_path), model_id=MODEL_ID, resolved_commit=COMMIT
        )
    )

    assert manifest["runtime_model"]["architecture"] == "qwen3"
    assert manifest["num_layers"] == 4


def test_qwen3_numpy_oracle_accepts_wide_attention_projection() -> None:
    logits = NumpyQwen3Runtime(
        runtime=_numpy_runtime(), tensors=_tensors()
    ).forward_token_ids(np.array([[3, 17, 5, 9]], dtype=np.int64))

    assert logits.shape == (1, 4, 48)
    assert np.isfinite(logits).all()


@pytest.mark.parametrize(
    ("dtype", "quantization"),
    # float16 is not exercised: Qwen-family MLX stages (Qwen2 included, on the
    # base tree too) emit float32 activations because RoPE multiplies by float32
    # cos/sin, so a float16 two-stage route refuses activation_data_length_mismatch.
    (("float32", "none"), ("float32", "int8-weight-only")),
)
def test_qwen3_two_stage_mlx_stage_local_kv_matches_monolithic_reference(
    tmp_path: Path, dtype: str, quantization: str
) -> None:
    deployment = prepare_physical_deployment(
        tmp_path / f"deployment-{dtype}-{quantization}",
        node_ids=("node-000", "node-001"),
        model_source=LocalModelSource(
            root=_snapshot(tmp_path), model_id=MODEL_ID, resolved_commit=COMMIT
        ),
        runtime_dtype=dtype,
        runtime_quantization=quantization,
    )
    assert [a["runtime"]["architecture"] for a in deployment.assignments] == [
        "qwen3",
        "qwen3",
    ]
    assert [a["runtime"]["quantization"] for a in deployment.assignments] == [
        quantization,
        quantization,
    ]
    loaded = [
        load_assignment_stage(assignment, report, load_generation=17)
        for assignment, report in zip(
            deployment.assignments, deployment.artifact_reports, strict=True
        )
    ]
    graph = build_execution_graph(
        deployment.assignments,
        [stage.proof for stage in loaded],
        link_scheme="iroh",
        runtime_scheme="iroh",
    )
    ports = tuple(
        MLXRuntimePort(
            assignment["node_id"],
            graph,
            {graph.stages[index].placements[0].placement_id: loaded[index]},
        )
        for index, assignment in enumerate(deployment.assignments)
    )
    prompt = [3, 17, 5, 9, 22]
    steps = 8
    generated = [
        _phase(ports, graph, tuple(prompt), phase="PREFILL", token_index=-1, position=0)
    ]
    for token_index in range(1, steps):
        generated.append(
            _phase(
                ports,
                graph,
                (generated[-1],),
                phase="DECODE",
                token_index=token_index,
                position=len(prompt) + token_index - 1,
                terminal=token_index == steps - 1,
            )
        )

    reference = NumpyQwen3Runtime(
        runtime=_numpy_runtime(quantization), tensors=_tensors()
    )
    context = list(prompt)
    expected: list[int] = []
    for _ in range(steps):
        logits = reference.forward_token_ids(np.array([context], dtype=np.int64))
        expected.append(quantized_greedy_token_id(logits[0, -1].tolist()))
        context.append(expected[-1])

    assert generated == expected
