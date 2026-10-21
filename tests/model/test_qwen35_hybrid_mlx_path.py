# SPDX-License-Identifier: AGPL-3.0-only
"""Qwen3.5 hybrid (Gated DeltaNet + gated attention) text models on the MLX stage path.

Qwen3.5 checkpoints (``model_type: qwen3_5``) interleave three Gated DeltaNet
("linear_attention") layers with one gated full-attention layer, keep the text
decoder under ``model.language_model.``, and ship a vision encoder and an MTP
head in the same safetensors file.  These tests use tiny synthetic checkpoints
with exactly that layout (including the excluded vision/MTP tensors and
``Hv > Hk`` value-head grouping) and drive them through the production manifest
compiler, assignment compiler, stage packs, the assignment loader and chained
``MLXRuntimePort`` stages with stage-local state.  The oracle is an independent
full-context NumPy implementation (sequential recurrence, no MLX).
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import mlx.core as mx
import numpy as np
import pytest

from model_adapters import ADAPTERS, adapter_for_config
from model_manifest import verify_manifest_digest
from mycelium_qualification.physical_deployment import (
    LocalModelSource,
    PhysicalDeploymentError,
    build_execution_graph,
    compile_local_model_manifest,
    prepare_assignment_artifacts,
)
from mycelium_router.contracts import ExecutionGraph, HopWorkItem, RuntimeBatchKey
from mycelium_router.decoding import quantized_greedy_token_id
from mycelium_router.mlx_runtime import MLXRuntimeError, MLXRuntimePort
from mycelium_router.payloads import encode_token_ids
from runtime_loader import load_assignment_stage

COMMIT = "d" * 40
MODEL_ID = "synthetic/Qwen3.5-hybrid"

# 8 layers: [L, L, L, F, L, L, L, F].  hidden 16; gated attention 2 heads x 16
# (wider than hidden), 1 KV head, partial rotary 4 of 16 channels; DeltaNet 2 key
# heads x 8, 4 value heads x 8 (Hv / Hk = 2, as 27B's 48 / 16 = 3).
_TEXT = {
    "model_type": "qwen3_5_text",
    "attention_bias": False,
    "attention_dropout": 0.0,
    "attn_output_gate": True,
    "dtype": "bfloat16",
    "eos_token_id": 60,
    "full_attention_interval": 4,
    "head_dim": 16,
    "hidden_act": "silu",
    "hidden_size": 16,
    "intermediate_size": 24,
    "layer_types": ["linear_attention"] * 3 + ["full_attention"]
    + ["linear_attention"] * 3 + ["full_attention"],
    "linear_conv_kernel_dim": 4,
    "linear_key_head_dim": 8,
    "linear_num_key_heads": 2,
    "linear_num_value_heads": 4,
    "linear_value_head_dim": 8,
    "max_position_embeddings": 64,
    "mlp_only_layers": [],
    "mtp_num_hidden_layers": 1,
    "mtp_use_dedicated_embeddings": False,
    "num_attention_heads": 2,
    "num_hidden_layers": 8,
    "num_key_value_heads": 1,
    "rms_norm_eps": 1e-6,
    "tie_word_embeddings": False,
    "use_cache": True,
    "vocab_size": 64,
    "mamba_ssm_dtype": "float32",
    "rope_parameters": {
        "mrope_interleaved": True,
        "mrope_section": [1, 1, 0],
        "rope_type": "default",
        "rope_theta": 10_000_000,
        "partial_rotary_factor": 0.25,
    },
}
_CONFIG = {
    "architectures": ["Qwen3_5ForConditionalGeneration"],
    "model_type": "qwen3_5",
    "tie_word_embeddings": False,
    "text_config": _TEXT,
    "vision_config": {"depth": 1, "hidden_size": 8, "model_type": "qwen3_5"},
}

# Published Qwen3.5-27B text config (= Qwen3.8-27B per the lane brief).
_TEXT_27B = {
    **{k: v for k, v in _TEXT.items() if k not in {"layer_types", "rope_parameters"}},
    "eos_token_id": 248044,
    "head_dim": 256,
    "hidden_size": 5120,
    "intermediate_size": 17408,
    "layer_types": (["linear_attention"] * 3 + ["full_attention"]) * 16,
    "linear_key_head_dim": 128,
    "linear_num_key_heads": 16,
    "linear_num_value_heads": 48,
    "linear_value_head_dim": 128,
    "max_position_embeddings": 262144,
    "num_attention_heads": 24,
    "num_hidden_layers": 64,
    "num_key_value_heads": 4,
    "vocab_size": 248320,
    "rope_parameters": {
        "mrope_interleaved": True,
        "mrope_section": [11, 11, 10],
        "rope_type": "default",
        "rope_theta": 10000000,
        "partial_rotary_factor": 0.25,
    },
}

_PREFIX = "model.language_model."


def _normalize(config: dict) -> dict:
    from runtime_contracts import normalize_qwen35_model_config

    text = config.get("text_config", config)
    return normalize_qwen35_model_config(
        config, expected_layers=text.get("num_hidden_layers", text.get("n_layer"))
    )


def _tensors(seed: int = 20261013) -> dict[str, np.ndarray]:
    rng = np.random.default_rng(seed)
    t = _TEXT
    hidden = t["hidden_size"]
    inner = t["intermediate_size"]
    head_dim = t["head_dim"]
    q_dim = t["num_attention_heads"] * head_dim
    kv_dim = t["num_key_value_heads"] * head_dim
    key_dim = t["linear_num_key_heads"] * t["linear_key_head_dim"]
    value_dim = t["linear_num_value_heads"] * t["linear_value_head_dim"]
    conv_dim = 2 * key_dim + value_dim
    hv = t["linear_num_value_heads"]
    tensors: dict[str, np.ndarray] = {
        _PREFIX + "embed_tokens.weight": rng.normal(0.0, 0.5, (t["vocab_size"], hidden)),
        # Zero-centred RMSNorm weights are stored as offsets from one.
        _PREFIX + "norm.weight": rng.normal(0.0, 0.05, (hidden,)),
        "lm_head.weight": rng.normal(0.0, 0.35, (t["vocab_size"], hidden)),
        # Excluded tensors: vision encoder and MTP head live in the same file.
        "model.visual.blocks.0.attn.qkv.weight": rng.normal(0.0, 0.1, (24, 8)),
        "model.visual.merger.linear_fc1.weight": rng.normal(0.0, 0.1, (8, 8)),
        "mtp.fc.weight": rng.normal(0.0, 0.1, (hidden, 2 * hidden)),
        "mtp.norm.weight": rng.normal(0.0, 0.05, (hidden,)),
    }
    shared = {
        "input_layernorm.weight": (hidden,),
        "post_attention_layernorm.weight": (hidden,),
        "mlp.gate_proj.weight": (inner, hidden),
        "mlp.up_proj.weight": (inner, hidden),
        "mlp.down_proj.weight": (hidden, inner),
    }
    linear = {
        "linear_attn.in_proj_qkv.weight": (conv_dim, hidden),
        "linear_attn.in_proj_z.weight": (value_dim, hidden),
        "linear_attn.in_proj_b.weight": (hv, hidden),
        "linear_attn.in_proj_a.weight": (hv, hidden),
        "linear_attn.conv1d.weight": (conv_dim, 1, t["linear_conv_kernel_dim"]),
        "linear_attn.out_proj.weight": (hidden, value_dim),
    }
    full = {
        "self_attn.q_proj.weight": (2 * q_dim, hidden),
        "self_attn.k_proj.weight": (kv_dim, hidden),
        "self_attn.v_proj.weight": (kv_dim, hidden),
        "self_attn.o_proj.weight": (hidden, q_dim),
    }
    for layer, kind in enumerate(t["layer_types"]):
        prefix = f"{_PREFIX}layers.{layer}."
        for suffix, shape in shared.items():
            scale = 0.05 if suffix.endswith("norm.weight") else 0.35
            tensors[prefix + suffix] = rng.normal(0.0, scale, shape)
        if kind == "linear_attention":
            for suffix, shape in linear.items():
                scale = 0.4 if "conv1d" in suffix else 0.35
                tensors[prefix + suffix] = rng.normal(0.0, scale, shape)
            tensors[prefix + "linear_attn.A_log"] = np.log(rng.uniform(1.0, 16.0, (hv,)))
            tensors[prefix + "linear_attn.dt_bias"] = rng.normal(0.0, 0.5, (hv,))
            tensors[prefix + "linear_attn.norm.weight"] = rng.normal(
                1.0, 0.05, (t["linear_value_head_dim"],)
            )
        else:
            for suffix, shape in full.items():
                tensors[prefix + suffix] = rng.normal(0.0, 0.35, shape)
            tensors[prefix + "self_attn.q_norm.weight"] = rng.normal(0.0, 0.05, (head_dim,))
            tensors[prefix + "self_attn.k_norm.weight"] = rng.normal(0.0, 0.05, (head_dim,))
    return {name: value.astype(np.float32) for name, value in tensors.items()}


def _text_tensors() -> dict[str, np.ndarray]:
    return {
        name: value
        for name, value in _tensors().items()
        if not name.startswith(("model.visual.", "mtp."))
    }


def _snapshot(tmp_path: Path) -> Path:
    root = tmp_path / "source"
    if root.exists():
        return root
    root.mkdir()
    (root / "config.json").write_text(json.dumps(_CONFIG), encoding="utf-8")
    mx.save_safetensors(
        str(root / "model.safetensors"),
        {name: mx.array(value) for name, value in _tensors().items()},
        metadata={"format": "pt"},
    )
    return root


def _oracle(quantization: str = "none"):
    from numpy_runtime import NumpyQwen35Runtime

    return NumpyQwen35Runtime(
        model_config=_normalize(_CONFIG),
        tensors=_text_tensors(),
        quantization=quantization,
    )


def _oracle_greedy(prompt: list[int], steps: int, quantization: str = "none") -> list[int]:
    reference = _oracle(quantization)
    context = list(prompt)
    expected: list[int] = []
    for _ in range(steps):
        logits = reference.forward_token_ids(np.array([context], dtype=np.int64))
        expected.append(quantized_greedy_token_id(logits[0, -1].tolist()))
        context.append(expected[-1])
    return expected


def _route(
    tmp_path: Path,
    *,
    label: str,
    layer_ranges: tuple[range, ...],
    quantization: str = "none",
    decode_mode: str = "stage_local_kv",
):
    node_ids = tuple(f"node-{index:03d}" for index in range(len(layer_ranges)))
    prepared = prepare_assignment_artifacts(
        tmp_path / f"deployment-{label}",
        node_ids=node_ids,
        model_source=LocalModelSource(
            root=_snapshot(tmp_path), model_id=MODEL_ID, resolved_commit=COMMIT
        ),
        runtime_dtype="float32",
        runtime_quantization=quantization,
        layer_ranges=layer_ranges,
    )
    loaded = [
        load_assignment_stage(assignment, report, load_generation=5)
        for assignment, report in zip(prepared.assignments, prepared.reports, strict=True)
    ]
    graph = build_execution_graph(
        prepared.assignments,
        [stage.proof for stage in loaded],
        link_scheme="iroh",
        runtime_scheme="iroh",
    )
    ports = tuple(
        MLXRuntimePort(
            assignment["node_id"],
            graph,
            {graph.stages[index].placements[0].placement_id: loaded[index]},
            decode_mode=decode_mode,
        )
        for index, assignment in enumerate(prepared.assignments)
    )
    return prepared, loaded, graph, ports


def _item(graph, stage, payload, *, phase, token_index, position, terminal, hop, span):
    placement = stage.placements[0]
    return HopWorkItem(
        request_id="qwen35-request",
        path_id="qwen35-path",
        path_attempt=0,
        phase=phase,
        token_index=token_index,
        hop_index=hop,
        placement_id=placement.placement_id,
        qos_class="interactive",
        deficit_ratio=0.0,
        enqueued_at=0.0,
        idempotency_key=f"qwen35:{phase}:{token_index}:{hop}",
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


def _phase(ports, graph: ExecutionGraph, token_ids, *, phase, token_index, position, terminal=False):
    payload = encode_token_ids(tuple(token_ids))
    span = 1 if phase == "DECODE" else len(token_ids)
    for hop, (port, stage) in enumerate(zip(ports, graph.stages, strict=True)):
        result = port.execute(
            _item(
                graph,
                stage,
                payload,
                phase=phase,
                token_index=token_index,
                position=position,
                terminal=terminal,
                hop=hop,
                span=span,
            )
        )
        assert result.success, result.failure_reason
        if hop + 1 < len(graph.stages):
            payload = result.payload
        else:
            return int(result.token_id)
    raise AssertionError("final stage missing")


def _generate(ports, graph, prompt: list[int], steps: int, *, replay: bool = False) -> list[int]:
    generated = [
        _phase(ports, graph, prompt, phase="PREFILL", token_index=-1, position=0)
    ]
    for token_index in range(1, steps):
        tokens = (prompt + generated) if replay else [generated[-1]]
        generated.append(
            _phase(
                ports,
                graph,
                tokens,
                phase="DECODE",
                token_index=token_index,
                position=len(prompt) + token_index - 1,
                terminal=token_index == steps - 1,
            )
        )
    return generated


PROMPT = [3, 17, 5, 9, 22, 41]


# --- contract -------------------------------------------------------------------


def test_qwen35_27b_text_config_normalizes() -> None:
    normalized = _normalize({"model_type": "qwen3_5", "text_config": _TEXT_27B})

    assert normalized["n_layer"] == 64
    assert normalized["n_embd"] == 5120
    assert normalized["n_head"] == 24 and normalized["n_kv_head"] == 4
    assert normalized["head_dim"] == 256
    assert normalized["rotary_dim"] == 64
    assert normalized["linear_num_value_heads"] == 48
    assert normalized["linear_num_key_heads"] == 16
    assert normalized["layer_types"].count("full_attention") == 16
    assert normalized["tie_word_embeddings"] is False
    # Already-normalized documents round-trip (assignment / loader re-validation).
    assert _normalize(normalized) == normalized


@pytest.mark.parametrize(
    ("patch", "match"),
    (
        ({"num_experts": 256}, "moe"),
        ({"attn_output_gate": False}, "attn_output_gate"),
        ({"linear_num_value_heads": 40}, "linear"),
        ({"layer_types": ["linear_attention"] * 63 + ["mamba"]}, "layer_types"),
        (
            {"rope_parameters": {**_TEXT_27B["rope_parameters"], "mrope_section": [11, 11, 11]}},
            "mrope",
        ),
        (
            {"rope_parameters": {**_TEXT_27B["rope_parameters"], "rope_type": "yarn"}},
            "rope",
        ),
    ),
)
def test_qwen35_normalizer_refuses_unsupported_variants(patch: dict, match: str) -> None:
    with pytest.raises(ValueError, match=match):
        _normalize({"model_type": "qwen3_5", "text_config": {**_TEXT_27B, **patch}})


def test_qwen35_adapter_declares_heterogeneous_layers_and_exclusions() -> None:
    adapter = adapter_for_config(_CONFIG)

    assert adapter.architecture == "qwen3_5"
    assert adapter.runtime_backends == ("mlx",)
    assert adapter.block_prefix_template == "model.language_model.layers.{layer}."
    assert set(adapter.layer_type_tensor_suffixes) == {"linear_attention", "full_attention"}
    assert "linear_attn.A_log" in adapter.layer_type_tensor_suffixes["linear_attention"]
    assert "self_attn.q_norm.weight" in adapter.layer_type_tensor_suffixes["full_attention"]
    assert adapter.excluded_tensor_prefixes == ("model.visual.", "mtp.")
    # Existing adapters are untouched.
    assert ADAPTERS["qwen3"].layer_type_tensor_suffixes == {}
    assert ADAPTERS["qwen3"].excluded_tensor_prefixes == ()


def test_qwen35_local_manifest_excludes_vision_and_mtp(tmp_path: Path) -> None:
    manifest = compile_local_model_manifest(
        LocalModelSource(root=_snapshot(tmp_path), model_id=MODEL_ID, resolved_commit=COMMIT)
    )

    assert verify_manifest_digest(manifest)
    assert manifest["architecture"] == "qwen3_5"
    assert manifest["num_layers"] == 8
    assert manifest["runtime_model"]["architecture"] == "qwen3_5"
    assert manifest["excluded_tensor_keys"] == sorted(
        key for key in _tensors() if key.startswith(("model.visual.", "mtp."))
    )
    owned = {key for keys in manifest["tensor_keys_by_layer"].values() for key in keys}
    owned |= {key for keys in manifest["component_tensor_keys"].values() for key in keys}
    assert not any(key.startswith(("model.visual.", "mtp.")) for key in owned)
    assert "model.language_model.layers.0.linear_attn.A_log" in manifest["tensor_keys_by_layer"]["0"]
    assert "model.language_model.layers.3.self_attn.q_norm.weight" in manifest["tensor_keys_by_layer"]["3"]


def test_qwen35_numpy_stage_backend_is_refused(tmp_path: Path) -> None:
    with pytest.raises(PhysicalDeploymentError, match="qwen3_5 numpy stage runtime is unsupported"):
        prepare_assignment_artifacts(
            tmp_path / "numpy",
            node_ids=("node-000", "node-001"),
            model_source=LocalModelSource(
                root=_snapshot(tmp_path), model_id=MODEL_ID, resolved_commit=COMMIT
            ),
            runtime_backend="numpy",
        )


# --- oracle -----------------------------------------------------------------------


def test_qwen35_numpy_oracle_shapes_and_prefix_consistency() -> None:
    oracle = _oracle()
    ids = np.array([PROMPT], dtype=np.int64)
    logits = oracle.forward_token_ids(ids)

    assert logits.shape == (1, len(PROMPT), 64)
    assert np.isfinite(logits).all()
    # Causality: a prefix's logits do not depend on later tokens (recurrence + mask).
    prefix = oracle.forward_token_ids(ids[:, :3])
    np.testing.assert_allclose(prefix[0], logits[0, :3], rtol=0, atol=1e-5)


# --- MLX route --------------------------------------------------------------------


@pytest.mark.parametrize("quantization", ("none", "int8-weight-only"))
def test_qwen35_two_stage_mlx_stage_local_state_matches_monolithic_oracle(
    tmp_path: Path, quantization: str
) -> None:
    prepared, loaded, graph, ports = _route(
        tmp_path,
        label=f"two-{quantization}",
        layer_ranges=(range(0, 4), range(4, 8)),
        quantization=quantization,
    )
    assert [a["runtime"]["architecture"] for a in prepared.assignments] == ["qwen3_5"] * 2
    assert prepared.assignments[0]["components"] == ["input_embedding", "decoder"]
    assert prepared.assignments[1]["components"] == ["decoder", "final_norm", "lm_head"]
    assert not any(
        key.startswith(("model.visual.", "mtp."))
        for stage in loaded
        for key in stage.tensors
    )

    generated = _generate(ports, graph, PROMPT, 10)

    assert generated == _oracle_greedy(PROMPT, 10, quantization)


def test_qwen35_stage_state_shapes_carry_across_decode(tmp_path: Path) -> None:
    _prepared, _loaded, graph, ports = _route(
        tmp_path, label="state", layer_ranges=(range(0, 3), range(3, 8))
    )
    _phase(ports, graph, PROMPT, phase="PREFILL", token_index=-1, position=0)
    first = ports[0]._kv_states["qwen35-path"]
    second = ports[1]._kv_states["qwen35-path"]
    conv_dim = 2 * 2 * 8 + 4 * 8
    assert sorted(first.layers) == [0, 1, 2]
    assert sorted(second.layers) == [3, 4, 5, 6, 7]
    for layer in (0, 1, 2, 4, 5, 6):
        conv, recurrent = (first.layers | second.layers)[layer]
        assert tuple(conv.shape) == (1, 3, conv_dim)
        assert tuple(recurrent.shape) == (1, 4, 8, 8)
        assert conv.dtype == mx.float32 and recurrent.dtype == mx.float32
    for layer in (3, 7):
        key, value = second.layers[layer]
        assert tuple(key.shape) == (1, 1, len(PROMPT), 16)
    before = np.array(first.layers[0][1])

    _phase(ports, graph, [7], phase="DECODE", token_index=1, position=len(PROMPT))

    assert tuple(second.layers[3][0].shape) == (1, 1, len(PROMPT) + 1, 16)
    assert tuple(first.layers[0][1].shape) == (1, 4, 8, 8)
    assert not np.array_equal(before, np.array(first.layers[0][1]))
    assert first.next_position == second.next_position == len(PROMPT) + 1


@pytest.mark.parametrize(
    "layer_ranges",
    (
        (range(0, 1), range(1, 8)),
        (range(0, 5), range(5, 8)),
        (range(0, 2), range(2, 5), range(5, 8)),
    ),
)
def test_qwen35_stage_split_anywhere_is_equivalent(tmp_path: Path, layer_ranges) -> None:
    label = "-".join(f"{r.start}_{r.stop}" for r in layer_ranges)
    _prepared, _loaded, graph, ports = _route(tmp_path, label=label, layer_ranges=layer_ranges)

    assert _generate(ports, graph, PROMPT, 8) == _oracle_greedy(PROMPT, 8)


def test_qwen35_incremental_state_equals_complete_context_replay(tmp_path: Path) -> None:
    _p, _l, graph, ports = _route(tmp_path, label="kv", layer_ranges=(range(0, 4), range(4, 8)))
    _p2, _l2, graph2, replay_ports = _route(
        tmp_path,
        label="replay",
        layer_ranges=(range(0, 4), range(4, 8)),
        decode_mode="complete_context_replay",
    )

    assert _generate(ports, graph, PROMPT, 8) == _generate(
        replay_ports, graph2, PROMPT, 8, replay=True
    )


def test_qwen35_refuses_state_rewind_and_kv_checkpoint_export(tmp_path: Path) -> None:
    _p, _l, graph, ports = _route(tmp_path, label="refuse", layer_ranges=(range(0, 4), range(4, 8)))
    _phase(ports, graph, PROMPT, phase="PREFILL", token_index=-1, position=0)

    with pytest.raises(MLXRuntimeError, match="kv_checkpoint_hybrid_state_unsupported"):
        ports[0].export_kv_checkpoint("qwen35-path")
    from mycelium_router.kv_checkpoint import KVCheckpointError

    with pytest.raises(KVCheckpointError, match="hybrid_state_rewind_unsupported"):
        ports[0].rekey_kv_path(
            "qwen35-path",
            new_path_id="successor",
            new_path_attempt=1,
            lease_expires_at=1_000_000_000_000.0,
            truncate_to_position=len(PROMPT) - 1,
        )
    verify = _item(
        graph,
        graph.stages[0],
        encode_token_ids((7, 8)),
        phase="VERIFY",
        token_index=1,
        position=len(PROMPT),
        terminal=False,
        hop=0,
        span=2,
    )
    result = ports[0].execute(verify)
    assert not result.success
    assert result.failure_reason == "hybrid_state_rewind_unsupported"


def test_qwen35_batched_dispatch_is_refused_as_a_group(tmp_path: Path) -> None:
    _p, _l, _graph, ports = _route(tmp_path, label="batch", layer_ranges=(range(0, 4), range(4, 8)))

    from mycelium_router.mlx_runtime import _OfferedMember  # noqa: F401  (presence)

    assert ports[0]._batch_group_refusal(()) in {
        "hybrid_recurrent_state_batching_unsupported"
    }


def test_qwen2_and_qwen3_adapters_are_unchanged() -> None:
    import model_adapters

    assert model_adapters.ADAPTERS["qwen3"].decoder_tensor_suffixes == (
        model_adapters.QWEN3_DECODER_TENSOR_SUFFIXES
    )
    assert model_adapters.ADAPTERS["qwen2"].runtime_backends == (
        "mlx",
        "numpy",
        "mobile-rust-qwen2",
    )
    assert copy.deepcopy(model_adapters.ADAPTERS["qwen3"].components) == {
        "input_embedding": ("model.embed_tokens.",),
        "decoder": ("model.layers.{layer}.",),
        "final_norm": ("model.norm.",),
        "lm_head": ("lm_head.",),
    }
