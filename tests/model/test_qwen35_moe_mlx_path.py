# SPDX-License-Identifier: AGPL-3.0-only
"""Qwen3.5 MoE (``qwen3_5_moe``, Qwen3.6-35B-A3B class) on the MLX stage path.

The MoE text decoder is the Qwen3.5 hybrid (Gated DeltaNet + gated attention)
with the dense MLP replaced by a sparse MoE block: a float32 router with
deterministic top-k (ties to the lowest expert index) and renormalised
weights, packed 3-D experts (``gate_up_proj [E, 2*mi, H]``, ``down_proj
[E, H, mi]``) and a sigmoid-gated shared expert.  Under int8-weight-only the
packed experts are quantized per expert and per output row (scales ``[E,
out]``); the router and the shared-expert gate stay float32.

A tiny random checkpoint with the published tensor layout
(``tests/model/qwen35_moe_tiny.py``) is driven through the production manifest
compiler, stage packs, assignment loader and chained ``MLXRuntimePort`` stages,
and compared with (a) single-process mlx-lm ``qwen3_5_moe`` (tokens pinned in a
fixture, produced in a scratch venv) and (b) the in-repo NumPy oracle.
"""

from __future__ import annotations

import json
from pathlib import Path

import mlx.core as mx
import numpy as np
import pytest

from model_adapters import ADAPTERS, adapter_for_config, adapter_for_runtime
from model_manifest import verify_manifest_digest
from mycelium_qualification.physical_deployment import (
    LocalModelSource,
    PhysicalDeploymentError,
    build_execution_graph,
    compile_local_model_manifest,
    prepare_assignment_artifacts,
)
from mycelium_router.decoding import quantized_greedy_token_id
from mycelium_router.mlx_runtime import (
    INT8_EXECUTION_PERSISTENT_PACKED,
    MLXRuntimeError,
    MLXRuntimePort,
    PackedInt8RowwiseWeight,
)
from mycelium_router.payloads import encode_token_ids
from runtime_loader import load_assignment_stage
from tests.model import qwen35_moe_tiny as tiny
from tests.model.test_qwen35_hybrid_mlx_path import _item, _phase
from weight_quantization import Int8RowwiseWeight

COMMIT = "f" * 40
MODEL_ID = "synthetic/Qwen3.5-MoE-tiny"
FIXTURE = Path(__file__).parent / "fixtures" / "qwen35_moe_tiny_mlx_lm_reference.json"
P = tiny.PREFIX

# Published Qwen/Qwen3.6-35B-A3B@995ad96eacd98c81ed38be0c5b274b04031597b0
# config.json text_config (verbatim field values; HF metadata only).
TEXT_35B_A3B = {
    "attention_bias": False,
    "attention_dropout": 0.0,
    "attn_output_gate": True,
    "bos_token_id": 248044,
    "dtype": "bfloat16",
    "eos_token_id": 248044,
    "full_attention_interval": 4,
    "head_dim": 256,
    "hidden_act": "silu",
    "hidden_size": 2048,
    "initializer_range": 0.02,
    "layer_types": (["linear_attention"] * 3 + ["full_attention"]) * 10,
    "linear_conv_kernel_dim": 4,
    "linear_key_head_dim": 128,
    "linear_num_key_heads": 16,
    "linear_num_value_heads": 32,
    "linear_value_head_dim": 128,
    "mamba_ssm_dtype": "float32",
    "max_position_embeddings": 262144,
    "model_type": "qwen3_5_moe_text",
    "moe_intermediate_size": 512,
    "mtp_num_hidden_layers": 1,
    "mtp_use_dedicated_embeddings": False,
    "num_attention_heads": 16,
    "num_experts": 256,
    "num_experts_per_tok": 8,
    "num_hidden_layers": 40,
    "num_key_value_heads": 2,
    "output_router_logits": False,
    "pad_token_id": None,
    "partial_rotary_factor": 0.25,
    "rms_norm_eps": 1e-06,
    "rope_parameters": {
        "mrope_interleaved": True,
        "mrope_section": [11, 11, 10],
        "partial_rotary_factor": 0.25,
        "rope_theta": 10000000,
        "rope_type": "default",
    },
    "router_aux_loss_coef": 0.001,
    "shared_expert_intermediate_size": 512,
    "tie_word_embeddings": False,
    "use_cache": True,
    "vocab_size": 248320,
}
CONFIG_35B_A3B = {
    "architectures": ["Qwen3_5MoeForConditionalGeneration"],
    "model_type": "qwen3_5_moe",
    "tie_word_embeddings": False,
    "text_config": TEXT_35B_A3B,
}
# Shapes read from the published safetensors headers (HTTP range reads of
# model-00001..00003-of-00026; layer 0 is linear_attention, layer 3 full).
REAL_35B_A3B_SHAPES = {
    "model.language_model.embed_tokens.weight": (248320, 2048),
    "model.language_model.layers.0.linear_attn.in_proj_qkv.weight": (8192, 2048),
    "model.language_model.layers.0.linear_attn.in_proj_z.weight": (4096, 2048),
    "model.language_model.layers.0.linear_attn.in_proj_a.weight": (32, 2048),
    "model.language_model.layers.0.linear_attn.in_proj_b.weight": (32, 2048),
    "model.language_model.layers.0.linear_attn.conv1d.weight": (8192, 1, 4),
    "model.language_model.layers.0.linear_attn.A_log": (32,),
    "model.language_model.layers.0.linear_attn.dt_bias": (32,),
    "model.language_model.layers.0.linear_attn.norm.weight": (128,),
    "model.language_model.layers.0.linear_attn.out_proj.weight": (2048, 4096),
    "model.language_model.layers.0.mlp.gate.weight": (256, 2048),
    "model.language_model.layers.0.mlp.experts.gate_up_proj": (256, 1024, 2048),
    "model.language_model.layers.0.mlp.experts.down_proj": (256, 2048, 512),
    "model.language_model.layers.0.mlp.shared_expert.gate_proj.weight": (512, 2048),
    "model.language_model.layers.0.mlp.shared_expert.up_proj.weight": (512, 2048),
    "model.language_model.layers.0.mlp.shared_expert.down_proj.weight": (2048, 512),
    "model.language_model.layers.0.mlp.shared_expert_gate.weight": (1, 2048),
    "model.language_model.layers.0.input_layernorm.weight": (2048,),
    "model.language_model.layers.0.post_attention_layernorm.weight": (2048,),
    "model.language_model.layers.3.self_attn.q_proj.weight": (8192, 2048),
    "model.language_model.layers.3.self_attn.k_proj.weight": (512, 2048),
    "model.language_model.layers.3.self_attn.v_proj.weight": (512, 2048),
    "model.language_model.layers.3.self_attn.o_proj.weight": (2048, 4096),
    "model.language_model.layers.3.self_attn.q_norm.weight": (256,),
    "model.language_model.layers.3.self_attn.k_norm.weight": (256,),
    "model.language_model.layers.3.mlp.experts.gate_up_proj": (256, 1024, 2048),
}


def _normalize(config: dict) -> dict:
    from runtime_contracts import normalize_qwen35_moe_model_config

    text = config.get("text_config", config)
    return normalize_qwen35_moe_model_config(
        config, expected_layers=text.get("num_hidden_layers", text.get("n_layer"))
    )


def _snapshot(tmp_path: Path) -> Path:
    root = tmp_path / "source"
    if not root.exists():
        tiny.write_checkpoint(root)
    return root


def _oracle(quantization: str = "none"):
    from numpy_runtime import NumpyQwen35MoeRuntime

    return NumpyQwen35MoeRuntime(
        model_config=_normalize(tiny.CONFIG),
        tensors=tiny.text_tensors(),
        quantization=quantization,
    )


def _oracle_greedy(prompt: list[int], steps: int, quantization: str = "none") -> list[int]:
    reference = _oracle(quantization)
    context = list(prompt)
    out: list[int] = []
    for _ in range(steps):
        logits = reference.forward_token_ids(np.array([context], dtype=np.int64))
        out.append(quantized_greedy_token_id(logits[0, -1].tolist()))
        context.append(out[-1])
    return out


def _route(tmp_path: Path, *, label: str, layer_ranges, quantization: str = "none",
           decode_mode: str = "stage_local_kv"):
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


def _generate(ports, graph, prompt: list[int], steps: int, *, replay: bool = False) -> list[int]:
    generated = [_phase(ports, graph, prompt, phase="PREFILL", token_index=-1, position=0)]
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


SPLIT = (range(0, 2), range(2, 4))


# --- contract ---------------------------------------------------------------------


def test_qwen35_moe_35b_a3b_config_normalizes() -> None:
    normalized = _normalize(CONFIG_35B_A3B)

    assert normalized["n_layer"] == 40 and normalized["n_embd"] == 2048
    assert normalized["num_experts"] == 256
    assert normalized["num_experts_per_tok"] == 8
    assert normalized["moe_intermediate_size"] == 512
    assert normalized["shared_expert_intermediate_size"] == 512
    assert normalized["norm_topk_prob"] is True
    # n_inner is the per-expert intermediate width (no dense MLP exists).
    assert normalized["n_inner"] == 512
    assert normalized["rotary_dim"] == 64 and normalized["head_dim"] == 256
    assert normalized["layer_types"].count("full_attention") == 10
    assert normalized["tie_word_embeddings"] is False
    assert _normalize(normalized) == normalized


@pytest.mark.parametrize(
    ("patch", "match"),
    (
        ({"norm_topk_prob": False}, "norm_topk_prob"),
        ({"num_experts_per_tok": 257}, "num_experts_per_tok"),
        ({"num_experts": 0}, "num_experts"),
        ({"decoder_sparse_step": 2}, "decoder_sparse_step"),
        ({"mlp_only_layers": [0]}, "mlp_only_layers"),
        ({"shared_expert_intermediate_size": 0}, "shared_expert_intermediate_size"),
        ({"moe_intermediate_size": None}, "moe_intermediate_size"),
    ),
)
def test_qwen35_moe_normalizer_refuses_unsupported_variants(patch: dict, match: str) -> None:
    text = {**TEXT_35B_A3B, **patch}
    with pytest.raises(ValueError, match=match):
        _normalize({**CONFIG_35B_A3B, "text_config": text})


def test_qwen35_dense_normalizer_still_refuses_moe_and_vice_versa() -> None:
    from runtime_contracts import normalize_qwen35_model_config, validate_normalized_numpy_runtime

    with pytest.raises(ValueError, match="moe"):
        normalize_qwen35_model_config(CONFIG_35B_A3B, expected_layers=40)
    with pytest.raises(ValueError, match="numpy"):
        validate_normalized_numpy_runtime(
            {
                "backend": "numpy",
                "dtype": "float32",
                "quantization": "none",
                "architecture": "qwen3_5_moe",
                "model_config": _normalize(tiny.CONFIG),
            }
        )


def test_qwen35_moe_adapter_is_mlx_runtime_supported() -> None:
    adapter = adapter_for_config(CONFIG_35B_A3B)

    assert adapter.architecture == "qwen3_5_moe"
    assert adapter.runtime_backends == ("mlx",)
    assert adapter_for_runtime("qwen3_5_moe") is adapter
    assert "mlp.experts.gate_up_proj" in adapter.decoder_tensor_suffixes
    assert ADAPTERS["qwen3_5"].runtime_backends == ("mlx",)


def test_qwen35_moe_shape_table_matches_published_35b_a3b_headers() -> None:
    from qwen35_shapes import expected_qwen35_shapes

    config = _normalize(CONFIG_35B_A3B)
    shapes = expected_qwen35_shapes(config, 0, 4, ("input_embedding",))
    for key, shape in REAL_35B_A3B_SHAPES.items():
        assert shapes[key] == shape, key
    # The dense MLP keys do not exist in the MoE layout.
    assert not any(key.endswith("mlp.gate_proj.weight") for key in shapes)


def test_qwen35_moe_memory_model_is_exact_per_layer() -> None:
    from qwen35_shapes import qwen35_stage_resident_bytes

    config = _normalize(CONFIG_35B_A3B)
    h, e, mi, si = 2048, 256, 512, 512

    def codes(n: int) -> int:
        # The single resident packed copy: int8 codes plus one float32 group
        # scale and one float32 group bias per 128 codes.
        return n + 2 * 4 * n // 128

    moe_int8 = (
        codes(e * (2 * mi * h + h * mi))  # packed expert int8 codes
        + 4 * e * (2 * mi + h)  # per-expert per-row float32 scales
        + 4 * e * h  # float32 router
        + codes(2 * si * h + h * si) + 4 * (2 * si + h)  # shared expert int8 + scales
        + 4 * h  # float32 shared-expert gate
        + 2 * 4 * h  # layer norms
    )
    linear_mixer = codes((8192 + 4096 + 32 + 32) * h) + 4 * (8192 + 4096 + 32 + 32) + (
        codes(h * 4096) + 4 * h
    ) + 4 * (8192 * 4 + 32 + 32 + 128)
    full_mixer = codes((8192 + 512 + 512) * h) + 4 * (8192 + 512 + 512) + (
        codes(h * 4096) + 4 * h
    ) + 4 * (256 + 256)
    assert qwen35_stage_resident_bytes(config, 0, 1, (), "int8-weight-only") == (
        moe_int8 + linear_mixer
    )
    assert qwen35_stage_resident_bytes(config, 3, 4, (), "int8-weight-only") == (
        moe_int8 + full_mixer
    )
    embed = codes(248320 * h) + 4 * 248320
    assert qwen35_stage_resident_bytes(
        config, 0, 0, ("input_embedding",), "int8-weight-only"
    ) == embed
    total = qwen35_stage_resident_bytes(
        config, 0, 40, ("input_embedding", "final_norm", "lm_head"), "int8-weight-only"
    )
    assert total == 30 * (moe_int8 + linear_mixer) + 10 * (moe_int8 + full_mixer) + (
        2 * embed + 4 * h
    )
    assert 36.0e9 < total < 38.0e9


# --- per-expert int8 ----------------------------------------------------------------


def test_packed_experts_quantize_per_expert_row_and_routing_stays_float32() -> None:
    from numpy_runtime import quantize_qwen2_numpy_tensor
    from weight_quantization import rowwise_int8_kind

    rng = np.random.default_rng(7)
    packed = rng.normal(0.0, 1.0, (4, 6, 8)).astype(np.float32)
    packed[2] *= 50.0  # one loud expert must not change the others' scales
    converted = quantize_qwen2_numpy_tensor(P + "layers.0.mlp.experts.gate_up_proj", packed)
    assert isinstance(converted, Int8RowwiseWeight)
    assert converted.values.shape == (4, 6, 8) and converted.values.dtype == np.int8
    assert converted.scales.shape == (4, 6) and converted.scales.dtype == np.float32
    for expert in range(4):
        alone = quantize_qwen2_numpy_tensor("x.weight", packed[expert])
        assert np.array_equal(converted.values[expert], alone.values)
        assert np.array_equal(converted.scales[expert], alone.scales)
    assert rowwise_int8_kind(P + "layers.0.mlp.experts.down_proj", 3) == "experts"
    assert rowwise_int8_kind(P + "layers.0.mlp.shared_expert.up_proj.weight", 2) == "matrix"
    assert rowwise_int8_kind(P + "layers.0.mlp.gate.weight", 2) is None
    assert rowwise_int8_kind(P + "layers.0.mlp.shared_expert_gate.weight", 2) is None
    assert rowwise_int8_kind("model.layers.0.mlp.gate_proj.weight", 2) == "matrix"
    router = rng.normal(0.0, 1.0, (8, 16)).astype(np.float32)
    assert quantize_qwen2_numpy_tensor(P + "layers.0.mlp.gate.weight", router) is router


# --- routing and the MoE block --------------------------------------------------------


def test_route_top_k_is_deterministic_with_lowest_index_tie_break() -> None:
    import qwen35_mlx
    from numpy_runtime import qwen35_moe_route_top_k

    probabilities = np.array(
        [
            [0.1, 0.3, 0.1, 0.3, 0.2, 0.0],  # tie at the top: 1 before 3
            [0.2, 0.2, 0.2, 0.2, 0.1, 0.1],  # four-way tie: 0, 1
            [0.0, 0.0, 0.1, 0.0, 0.4, 0.5],
        ],
        dtype=np.float32,
    )
    indices, weights = qwen35_mlx.route_top_k(mx.array(probabilities), 2)
    assert np.array(indices).tolist() == [[1, 3], [0, 1], [5, 4]]
    assert np.allclose(np.array(weights), [[0.5, 0.5], [0.5, 0.5], [5 / 9, 4 / 9]])
    ref_indices, ref_weights = qwen35_moe_route_top_k(probabilities, 2)
    assert ref_indices.tolist() == [[1, 3], [0, 1], [5, 4]]
    assert np.array_equal(np.array(weights), ref_weights)


def test_route_top_k_matches_the_stable_sort_reference_at_35b_width() -> None:
    """256 experts, top-8, rows with heavy ties (coarse probability grid)."""

    import qwen35_mlx
    from numpy_runtime import qwen35_moe_route_top_k

    rng = np.random.default_rng(5)
    for grid in (None, 64.0, 8.0):
        logits = rng.normal(0.0, 2.0, (64, 256)).astype(np.float32)
        if grid is not None:
            logits = np.round(logits * grid / 8.0) / (grid / 8.0)
        exponent = np.exp(logits - logits.max(axis=-1, keepdims=True))
        probabilities = (exponent / exponent.sum(axis=-1, keepdims=True)).astype(np.float32)
        indices, weights = qwen35_mlx.route_top_k(mx.array(probabilities), 8)
        ref_indices, ref_weights = qwen35_moe_route_top_k(probabilities, 8)
        assert np.array(indices).tolist() == ref_indices.tolist(), grid
        np.testing.assert_allclose(np.array(weights), ref_weights, rtol=1e-6, atol=0)


@pytest.mark.parametrize("quantization", ("none", "int8-weight-only"))
def test_mlx_sparse_moe_block_matches_numpy_reference(quantization: str) -> None:
    import qwen35_mlx
    from numpy_runtime import qwen35_sparse_moe_numpy, quantize_qwen2_numpy_tensors

    config = _normalize(tiny.CONFIG)
    prefix = P + "layers.1."
    source = {k: v for k, v in tiny.text_tensors().items() if k.startswith(prefix + "mlp.")}
    if quantization != "none":
        source = quantize_qwen2_numpy_tensors(source)
    as_mx = {
        key: Int8RowwiseWeight(mx.array(value.values), mx.array(value.scales))
        if isinstance(value, Int8RowwiseWeight)
        else mx.array(value)
        for key, value in source.items()
    }
    from runtime_loader import _qwen2_linear as loader_linear

    hidden = np.random.default_rng(3).normal(0.0, 1.0, (1, 7, 128)).astype(np.float32)
    got = qwen35_mlx.sparse_moe_block(
        mx.array(hidden), as_mx, prefix, config, lambda x, w: loader_linear(x, w, mx)
    )
    expected = qwen35_sparse_moe_numpy(hidden, source, prefix, config)
    assert got.shape == (1, 7, 128)
    np.testing.assert_allclose(np.array(got), expected, rtol=2e-4, atol=2e-5)


def test_mlx_moe_block_rows_are_independent_of_call_width(tmp_path: Path) -> None:
    """Prefill and decode compute every token's experts with the same kernels."""

    import qwen35_mlx
    from mycelium_router.mlx_runtime import _qwen2_linear, _qwen35_expert_linear

    for quantization in ("none", "int8-weight-only"):
        _prepared, _loaded, _graph, ports = _route(
            tmp_path, label=f"rows-{quantization}", layer_ranges=SPLIT,
            quantization=quantization,
        )
        port = ports[0]
        tensors = next(iter(port._execution_tensors.values()))
        if quantization != "none":
            assert isinstance(
                tensors[P + "layers.0.mlp.experts.gate_up_proj"], PackedInt8RowwiseWeight
            )
        config = _normalize(tiny.CONFIG)
        hidden = mx.random.normal((1, 9, 128), key=mx.random.key(11))
        full = qwen35_mlx.sparse_moe_block(
            hidden, tensors, P + "layers.1.", config, _qwen2_linear, _qwen35_expert_linear
        )
        for index in range(9):
            alone = qwen35_mlx.sparse_moe_block(
                hidden[:, index : index + 1], tensors, P + "layers.1.", config,
                _qwen2_linear, _qwen35_expert_linear,
            )
            assert bool(mx.array_equal(alone, full[:, index : index + 1]).item()), (
                quantization, index,
            )


# --- manifest / loader --------------------------------------------------------------


def test_qwen35_moe_local_manifest_compiles_runtime_model(tmp_path: Path) -> None:
    manifest = compile_local_model_manifest(
        LocalModelSource(root=_snapshot(tmp_path), model_id=MODEL_ID, resolved_commit=COMMIT)
    )

    assert verify_manifest_digest(manifest)
    assert manifest["architecture"] == "qwen3_5_moe"
    assert manifest["runtime_model"]["architecture"] == "qwen3_5_moe"
    assert manifest["runtime_model"]["model_config"] == _normalize(tiny.CONFIG)
    assert "mtp.layers.0.mlp.experts.gate_up_proj" in manifest["excluded_tensor_keys"]
    assert "model.visual.blocks.0.attn.qkv.weight" in manifest["excluded_tensor_keys"]
    assert P + "layers.3.mlp.experts.down_proj" in manifest["tensor_keys_by_layer"]["3"]


def test_qwen35_moe_numpy_stage_backend_is_refused(tmp_path: Path) -> None:
    with pytest.raises(
        PhysicalDeploymentError, match="qwen3_5_moe numpy stage runtime is unsupported"
    ):
        prepare_assignment_artifacts(
            tmp_path / "deployment-numpy",
            node_ids=("node-000", "node-001"),
            model_source=LocalModelSource(
                root=_snapshot(tmp_path), model_id=MODEL_ID, resolved_commit=COMMIT
            ),
            runtime_backend="numpy",
        )


def test_qwen35_moe_loader_quantizes_experts_per_expert_and_keeps_router_float32(
    tmp_path: Path,
) -> None:
    _prepared, loaded, _graph, ports = _route(
        tmp_path, label="load-int8", layer_ranges=SPLIT, quantization="int8-weight-only"
    )
    tensors = loaded[0].tensors
    experts = tensors[P + "layers.0.mlp.experts.gate_up_proj"]
    assert isinstance(experts, Int8RowwiseWeight)
    assert tuple(experts.values.shape) == (8, 256, 128) and experts.values.dtype == mx.int8
    assert tuple(experts.scales.shape) == (8, 256)
    down = tensors[P + "layers.1.mlp.experts.down_proj"]
    assert tuple(down.values.shape) == (8, 128, 128) and tuple(down.scales.shape) == (8, 128)
    router = tensors[P + "layers.0.mlp.gate.weight"]
    assert not isinstance(router, Int8RowwiseWeight) and router.dtype == mx.float32
    assert not isinstance(tensors[P + "layers.0.mlp.shared_expert_gate.weight"], Int8RowwiseWeight)
    assert isinstance(tensors[P + "layers.0.mlp.shared_expert.up_proj.weight"], Int8RowwiseWeight)
    # The loaded per-expert codes equal the NumPy oracle's quantization.
    from numpy_runtime import quantize_qwen2_numpy_tensor

    reference = quantize_qwen2_numpy_tensor(
        P + "layers.0.mlp.experts.gate_up_proj",
        tiny.text_tensors()[P + "layers.0.mlp.experts.gate_up_proj"],
    )
    assert np.array_equal(np.array(experts.values), reference.values)
    assert np.array_equal(np.array(experts.scales), reference.scales)
    for port in ports:
        assert port.int8_execution == INT8_EXECUTION_PERSISTENT_PACKED
        execution = next(iter(port._execution_tensors.values()))
        packed = [v for v in execution.values() if isinstance(v, PackedInt8RowwiseWeight)]
        assert any(v.values.ndim == 3 for v in packed)


# --- execution parity ---------------------------------------------------------------


@pytest.mark.parametrize("quantization", ("none", "int8-weight-only"))
def test_qwen35_moe_two_stage_route_matches_numpy_oracle(
    tmp_path: Path, quantization: str
) -> None:
    _prepared, _loaded, graph, ports = _route(
        tmp_path, label=f"two-{quantization}", layer_ranges=SPLIT, quantization=quantization
    )
    tokens = _generate(ports, graph, tiny.PROMPT, 16)
    assert tokens == _oracle_greedy(tiny.PROMPT, 16, quantization)


@pytest.mark.parametrize(
    "layer_ranges",
    (
        (range(0, 1), range(1, 4)),
        (range(0, 3), range(3, 4)),
        (range(0, 1), range(1, 3), range(3, 4)),
    ),
)
def test_qwen35_moe_stage_split_anywhere_is_equivalent(tmp_path: Path, layer_ranges) -> None:
    label = "-".join(f"{r.start}{r.stop}" for r in layer_ranges)
    _prepared, _loaded, graph, ports = _route(tmp_path, label=label, layer_ranges=layer_ranges)
    assert _generate(ports, graph, tiny.PROMPT, 8) == _oracle_greedy(tiny.PROMPT, 8)


def test_qwen35_moe_incremental_state_equals_complete_context_replay(tmp_path: Path) -> None:
    _p, _l, graph, ports = _route(tmp_path, label="incremental", layer_ranges=SPLIT)
    _p2, _l2, graph2, replay_ports = _route(
        tmp_path, label="replay", layer_ranges=SPLIT, decode_mode="complete_context_replay"
    )
    assert _generate(ports, graph, tiny.PROMPT, 6) == _generate(
        replay_ports, graph2, tiny.PROMPT, 6, replay=True
    )


def test_qwen35_moe_stages_refuse_state_rewind_and_batching(tmp_path: Path) -> None:
    from mycelium_router.kv_checkpoint import KVCheckpointError
    from mycelium_router.mlx_runtime import HYBRID_STATE_ARCHITECTURES

    assert "qwen3_5_moe" in HYBRID_STATE_ARCHITECTURES
    _prepared, _loaded, graph, ports = _route(tmp_path, label="refuse", layer_ranges=SPLIT)
    _phase(ports, graph, tiny.PROMPT, phase="PREFILL", token_index=-1, position=0)
    with pytest.raises(MLXRuntimeError, match="kv_checkpoint_hybrid_state_unsupported"):
        ports[0].export_kv_checkpoint("qwen35-path")
    with pytest.raises(KVCheckpointError, match="hybrid_state_rewind_unsupported"):
        ports[0].rekey_kv_path(
            "qwen35-path",
            new_path_id="successor",
            new_path_attempt=1,
            lease_expires_at=1_000_000_000_000.0,
            truncate_to_position=len(tiny.PROMPT) - 1,
        )
    result = ports[0].execute(
        _item(
            graph,
            graph.stages[0],
            encode_token_ids((4, 5)),
            phase="VERIFY",
            token_index=1,
            position=len(tiny.PROMPT),
            terminal=False,
            hop=0,
            span=2,
        )
    )
    assert not result.success
    assert result.failure_reason == "hybrid_state_rewind_unsupported"
    assert ports[0]._batch_group_refusal(()) == "hybrid_recurrent_state_batching_unsupported"


def test_qwen35_moe_refuses_the_platform_neutral_int8_kernel(tmp_path: Path) -> None:
    from runtime_loader import PLATFORM_NEUTRAL_INT8_EXECUTION, RuntimeLoadError

    prepared = prepare_assignment_artifacts(
        tmp_path / "deployment-a9",
        node_ids=("node-000", "node-001"),
        model_source=LocalModelSource(
            root=_snapshot(tmp_path), model_id=MODEL_ID, resolved_commit=COMMIT
        ),
        runtime_dtype="float32",
        runtime_quantization="int8-weight-only",
        layer_ranges=SPLIT,
    )
    with pytest.raises(RuntimeLoadError, match="platform_neutral_int8_packed_experts_unsupported"):
        load_assignment_stage(
            prepared.assignments[0],
            prepared.reports[0],
            load_generation=5,
            int8_execution=PLATFORM_NEUTRAL_INT8_EXECUTION,
        )


def test_qwen35_moe_selects_the_qwen35_thinking_off_codec(tmp_path: Path, monkeypatch) -> None:
    """Qwen3.6-35B-A3B ships the Qwen3.5-0.8B tokenizer.json (same LFS sha256
    5f9e4d49...) and a template whose enable_thinking=False envelope is identical."""

    from types import SimpleNamespace

    from mycelium_live import codec as codec_module
    from scripts import build_qwen_live_route as builder

    monkeypatch.setattr(codec_module.Qwen35PromptCodec, "from_deployment", lambda _root: "q35")
    (tmp_path / "config.json").write_text(json.dumps({"model_type": "qwen3_5_moe"}))
    assert codec_module.prompt_codec_from_deployment(tmp_path) == "q35"
    monkeypatch.setattr(
        builder, "Qwen35PromptCodec", SimpleNamespace(from_deployment=lambda root: "q35")
    )
    manifest = {"runtime_model": {"architecture": "qwen3_5_moe"}}
    assert builder._deployment_prompt_codec(manifest, Path(".")) == "q35"


# --- single-process mlx-lm reference ------------------------------------------------


def _fixture() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def test_tiny_checkpoint_is_the_one_the_mlx_lm_reference_ran_on() -> None:
    doc = _fixture()
    assert doc["checkpoint"]["text_tensor_digest"] == tiny.tensor_digest(tiny.text_tensors())
    assert doc["checkpoint"]["int8_dequantized_digest"] == tiny.tensor_digest(
        tiny.int8_dequantized_text_tensors()
    )
    assert doc["prompt_ids"] == tiny.PROMPT
    assert doc["mlx_lm_version"] and doc["model_type"] == "qwen3_5_moe"
    # The int8 run is not vacuous: per-expert int8 changes the greedy sequence.
    assert doc["runs"]["float32"]["generated"] != doc["runs"]["int8_dequantized"]["generated"]


@pytest.mark.parametrize(
    ("quantization", "reference"), (("none", "float32"), ("int8-weight-only", "int8_dequantized"))
)
def test_qwen35_moe_two_stage_route_matches_single_process_mlx_lm(
    tmp_path: Path, quantization: str, reference: str
) -> None:
    expected = _fixture()["runs"][reference]["generated"]
    assert len(expected) >= 16
    _prepared, _loaded, graph, ports = _route(
        tmp_path, label=f"mlxlm-{quantization}", layer_ranges=SPLIT, quantization=quantization
    )
    assert _generate(ports, graph, tiny.PROMPT, len(expected)) == expected
    assert _oracle_greedy(tiny.PROMPT, len(expected), quantization) == expected
