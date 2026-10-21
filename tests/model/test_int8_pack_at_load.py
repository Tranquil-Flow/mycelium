# SPDX-License-Identifier: AGPL-3.0-only
"""int8-weight-only stages keep exactly one resident representation per weight.

Failure modes this module catches, on the production path (loader -> stage
proof -> ``MLXRuntimePort``) for dense Qwen2, dense Qwen3 and ``qwen3_5_moe``:

* the loader keeping signed int8 codes that the port then packs into a second,
  persistent copy (measured x2.04 resident before pack-at-load);
* a port that re-packs, or otherwise allocates weight-sized state, at
  construction instead of executing the loaded packed weights in place;
* resident MLX bytes that drift from the per-tensor memory model.
"""

from __future__ import annotations

import gc
import json
from pathlib import Path

import mlx.core as mx
import numpy as np
import pytest

from mycelium_qualification.physical_deployment import (
    LocalModelSource,
    build_execution_graph,
    prepare_assignment_artifacts,
)
from mycelium_router.mlx_runtime import (
    INT8_EXECUTION_PERSISTENT_PACKED,
    MLXRuntimePort,
    PackedInt8RowwiseWeight,
)
from runtime_loader import load_assignment_stage
from weight_quantization import Int8RowwiseWeight, rowwise_int8_kind

from tests.model import qwen35_moe_tiny as moe_tiny
from tests.model.test_qwen3_dense_mlx_path import _tensors as _qwen3_tensors

# Every int8 matrix of these configs has a multiple of 128 input columns, so the
# whole stage takes the persistent packed kernel.
_QWEN3 = {
    "architectures": ["Qwen3ForCausalLM"],
    "model_type": "qwen3",
    "torch_dtype": "bfloat16",
    "num_hidden_layers": 4,
    "hidden_size": 128,
    "num_attention_heads": 2,
    "num_key_value_heads": 1,
    "head_dim": 64,
    "intermediate_size": 256,
    "vocab_size": 96,
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
_QWEN2 = {
    "architectures": ["Qwen2ForCausalLM"],
    "model_type": "qwen2",
    "torch_dtype": "bfloat16",
    "num_hidden_layers": 4,
    "hidden_size": 128,
    "num_attention_heads": 2,
    "num_key_value_heads": 1,
    "intermediate_size": 256,
    "vocab_size": 96,
    "max_position_embeddings": 64,
    "rms_norm_eps": 1e-6,
    "rope_theta": 1_000_000.0,
    "hidden_act": "silu",
    "tie_word_embeddings": True,
    "use_sliding_window": False,
    "sliding_window": None,
    "max_window_layers": 4,
}


def _qwen2_tensors() -> dict[str, np.ndarray]:
    config = dict(_QWEN3, head_dim=64)
    tensors = {
        key: value
        for key, value in _qwen3_tensors(config, seed=7).items()
        if not key.endswith(("q_norm.weight", "k_norm.weight"))
    }
    rng = np.random.default_rng(8)
    for layer in range(_QWEN2["num_hidden_layers"]):
        for name, width in (("q_proj", 128), ("k_proj", 64), ("v_proj", 64)):
            tensors[f"model.layers.{layer}.self_attn.{name}.bias"] = rng.normal(
                0.0, 0.1, (width,)
            ).astype(np.float32)
    return tensors


def _write(root: Path, config: dict, tensors: dict[str, np.ndarray]) -> Path:
    root.mkdir(parents=True)
    (root / "config.json").write_text(json.dumps(config), encoding="utf-8")
    mx.save_safetensors(
        str(root / "model.safetensors"),
        {name: mx.array(value) for name, value in tensors.items()},
        metadata={"format": "pt"},
    )
    return root


def _qwen3_checkpoint() -> dict[str, np.ndarray]:
    tensors = _qwen3_tensors(_QWEN3, seed=11)
    # Like Qwen/Qwen3-0.6B: a redundant head copy although embeddings are tied
    # (the manifest ties the head to the embedding and never loads this one).
    tensors["lm_head.weight"] = tensors["model.embed_tokens.weight"].copy()
    return tensors


def _source(tmp_path: Path, architecture: str) -> Path:
    root = tmp_path / f"source-{architecture}"
    if architecture == "qwen2":
        return _write(root, _QWEN2, _qwen2_tensors())
    if architecture == "qwen3":
        return _write(root, _QWEN3, _qwen3_checkpoint())
    return moe_tiny.write_checkpoint(root)


def _array_bytes(value: object) -> int:
    if isinstance(value, PackedInt8RowwiseWeight):
        parts = (value.packed, value.scales, value.group_scales, value.group_biases)
        return sum(int(part.nbytes) for part in parts)
    if isinstance(value, Int8RowwiseWeight):
        return int(value.values.nbytes) + int(value.scales.nbytes)
    return int(value.nbytes)


def _model_bytes(key: str, shape: tuple[int, ...]) -> int:
    """Per-tensor memory model: packed int8 codes, row scales, group params."""

    elements = int(np.prod(shape))
    if rowwise_int8_kind(key, len(shape)) is None:
        return 4 * elements
    rows = elements // shape[-1]
    return elements + 4 * rows + 2 * 4 * elements // 128


def _measure(tmp_path: Path, architecture: str):
    prepared = prepare_assignment_artifacts(
        tmp_path / f"deployment-{architecture}",
        node_ids=("node-000", "node-001"),
        model_source=LocalModelSource(
            root=_source(tmp_path, architecture),
            model_id=f"synthetic/{architecture}-int8-pack",
            resolved_commit="e" * 40,
        ),
        runtime_dtype="float32",
        runtime_quantization="int8-weight-only",
        layer_ranges=(range(0, 2), range(2, 4)),
    )
    gc.collect()
    mx.clear_cache()
    before = mx.get_active_memory()
    loaded = [
        load_assignment_stage(assignment, report, load_generation=3)
        for assignment, report in zip(prepared.assignments, prepared.reports, strict=True)
    ]
    gc.collect()
    mx.clear_cache()
    after_load = mx.get_active_memory() - before
    graph = build_execution_graph(
        prepared.assignments,
        [stage.proof for stage in loaded],
        link_scheme="iroh",
        runtime_scheme="iroh",
    )
    ports = [
        MLXRuntimePort(
            assignment["node_id"],
            graph,
            {graph.stages[index].placements[0].placement_id: loaded[index]},
        )
        for index, assignment in enumerate(prepared.assignments)
    ]
    gc.collect()
    mx.clear_cache()
    after_ports = mx.get_active_memory() - before
    return loaded, ports, after_load, after_ports


@pytest.mark.parametrize("architecture", ("qwen2", "qwen3", "qwen3_5_moe"))
def test_int8_stage_is_packed_once_at_load_and_never_copied(
    tmp_path: Path, architecture: str
) -> None:
    loaded, ports, after_load, after_ports = _measure(tmp_path, architecture)

    quantized = {
        (index, key): value
        for index, stage in enumerate(loaded)
        for key, value in stage.tensors.items()
        if isinstance(value, Int8RowwiseWeight)
    }
    assert quantized
    assert any(len(value.shape) == 3 for value in quantized.values()) == (
        architecture == "qwen3_5_moe"
    )
    # Packed at load: the loaded stage itself holds the execution layout ...
    assert all(isinstance(value, PackedInt8RowwiseWeight) for value in quantized.values())
    # ... and the port executes those very objects instead of a second copy.
    for index, port in enumerate(ports):
        assert port.int8_execution == INT8_EXECUTION_PERSISTENT_PACKED
        (execution,) = port._execution_tensors.values()
        for key, value in loaded[index].tensors.items():
            assert execution[key] is value, key


@pytest.mark.parametrize("architecture", ("qwen2", "qwen3", "qwen3_5_moe"))
def test_int8_stage_resident_bytes_equal_the_single_copy_memory_model(
    tmp_path: Path, architecture: str
) -> None:
    loaded, ports, after_load, after_ports = _measure(tmp_path, architecture)

    model = sum(
        _model_bytes(key, tuple(int(extent) for extent in value.shape))
        for stage in loaded
        for key, value in stage.tensors.items()
    )
    # Measured MLX residency once the ports exist: one representation.  The
    # slack covers allocator bookkeeping only; a duplicate copy is ~+100%.
    slack = 64 * 1024 + model // 100
    assert len(ports) == 2
    assert model - slack <= after_load <= model + slack, (model, after_load)
    assert model - slack <= after_ports <= model + slack, (model, after_ports)
    held = sum(_array_bytes(value) for stage in loaded for value in stage.tensors.values())
    assert held == model


def test_codes_packed_at_load_are_bit_identical_to_packing_the_int8_values(
    tmp_path: Path,
) -> None:
    """Same bytes the port used to pack from the loader's int8 copy, so the
    persistent kernel computes exactly what it computed before."""

    from numpy_runtime import quantize_qwen2_numpy_tensor
    from mycelium_router.mlx_runtime import _pack_int8_rowwise

    loaded, _ports, _after_load, _after_ports = _measure(tmp_path, "qwen3_5_moe")
    source = moe_tiny.text_tensors()
    checked = 0
    for stage in loaded:
        for key, value in stage.tensors.items():
            if not isinstance(value, Int8RowwiseWeight):
                continue
            reference = quantize_qwen2_numpy_tensor(
                key if value.packed.ndim == 2 else key + ".rows.weight",
                source[key].reshape(-1, source[key].shape[-1]),
            )
            values = mx.array(reference.values.reshape(source[key].shape))
            scales = mx.array(reference.scales.reshape(source[key].shape[:-1]))
            repacked = _pack_int8_rowwise(Int8RowwiseWeight(values, scales))
            for part in ("packed", "scales", "group_scales", "group_biases"):
                assert bool(
                    mx.array_equal(getattr(value, part), getattr(repacked, part)).item()
                ), (key, part)
                assert getattr(value, part).dtype == getattr(repacked, part).dtype
            assert bool(mx.array_equal(value.values, values).item())
            checked += 1
    assert checked >= 10


def _hub_entry(tmp_path: Path, architecture: str):
    from mycelium_model_catalog import scan_huggingface_cache

    snapshot = tmp_path / "hub" / f"models--synthetic--{architecture}" / "snapshots" / ("e" * 40)
    if architecture == "qwen2":
        _write(snapshot, _QWEN2, _qwen2_tensors())
    else:
        _write(snapshot, _QWEN3, _qwen3_checkpoint())
    ref = snapshot.parent.parent / "refs" / "main"
    ref.parent.mkdir(parents=True)
    ref.write_text("e" * 40, encoding="utf-8")
    (entry,) = scan_huggingface_cache(tmp_path / "hub")
    return entry


@pytest.mark.parametrize("architecture", ("qwen2", "qwen3"))
def test_catalog_int8_residency_equals_the_loaded_stage_bytes(
    tmp_path: Path, architecture: str
) -> None:
    loaded, _ports, _after_load, after_ports = _measure(tmp_path, architecture)
    held = [sum(_array_bytes(v) for v in stage.tensors.values()) for stage in loaded]
    entry = _hub_entry(tmp_path, architecture)

    layers = entry.int8_layer_weight_bytes
    assert len(layers) == 4
    # Stage 0 owns the embedding, stage 1 the final norm and the tied head
    # (the embedding again); a redundant checkpoint head is never counted.
    assert sum(layers[:2]) + entry.int8_entry_static_bytes == held[0]
    assert sum(layers[2:]) + entry.int8_final_static_bytes == held[1]
    representation = entry.projection()["serving_representations"][0]
    assert representation["resident_weight_bytes"] == sum(held)
    assert abs(after_ports - sum(held)) <= 64 * 1024 + sum(held) // 100


def test_qwen35_memory_model_counts_a_tied_head_as_the_embedding() -> None:
    from qwen35_shapes import qwen35_stage_resident_bytes
    from runtime_contracts import normalize_qwen35_moe_model_config

    untied = normalize_qwen35_moe_model_config(moe_tiny.CONFIG, expected_layers=4)
    tied = dict(untied, tie_word_embeddings=True)
    head = ("final_norm", "lm_head")
    everything = ("input_embedding", "final_norm", "lm_head")
    vocab, hidden = int(untied["vocab_size"]), int(untied["n_embd"])
    matrix = vocab * hidden + 4 * vocab + 8 * vocab * hidden // 128
    for quantization in ("int8-weight-only", "none"):
        # The final stage loads the tied embedding as its head: same bytes as
        # an untied head of the same shape.
        assert qwen35_stage_resident_bytes(tied, 2, 4, head, quantization) == (
            qwen35_stage_resident_bytes(untied, 2, 4, head, quantization)
        )
        # A stage owning both ends loads the embedding once.
        once = matrix if quantization == "int8-weight-only" else 4 * vocab * hidden
        assert qwen35_stage_resident_bytes(tied, 0, 4, everything, quantization) == (
            qwen35_stage_resident_bytes(untied, 0, 4, everything, quantization) - once
        )


def test_qwen35_moe_memory_model_equals_the_loaded_stage_bytes(tmp_path: Path) -> None:
    from qwen35_shapes import qwen35_stage_resident_bytes
    from runtime_contracts import normalize_qwen35_moe_model_config

    loaded, _ports, _after_load, _after_ports = _measure(tmp_path, "qwen3_5_moe")
    config = normalize_qwen35_moe_model_config(moe_tiny.CONFIG, expected_layers=4)
    for stage, (start, end), components in zip(
        loaded, ((0, 2), (2, 4)), (("input_embedding",), ("final_norm", "lm_head"))
    ):
        held = sum(_array_bytes(v) for v in stage.tensors.values())
        assert qwen35_stage_resident_bytes(
            config, start, end, components, "int8-weight-only"
        ) == held

