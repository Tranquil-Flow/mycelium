#!/usr/bin/env python3
"""Focused contract and tamper tests for the assignment-bound MLX loader."""

from __future__ import annotations

import copy
import json
import struct
from pathlib import Path
from typing import Any

import mlx.core as mx
import pytest

from layer_assignment import assignment_id_for
from runtime_loader import (
    RuntimeLoadError,
    _gpt2_block,
    canonical_json,
    load_assignment_stage,
)
from weight_provisioning import sha256_file


DEPLOYMENT_ID = "12345678-1234-5678-9234-abcdefabcdef"
COMMIT = "c" * 40
MANIFEST_DIGEST = "sha256:" + "d" * 64
LAYER_SUFFIXES = (
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


def _values(
    shape: tuple[int, ...], *, offset: int = 0, scale: float = 0.01
) -> mx.array:
    size = 1
    for dimension in shape:
        size *= dimension
    return (mx.arange(size, dtype=mx.float32).reshape(shape) + offset) * scale


def _layer_tensors(layer: int, *, offset: int = 0) -> dict[str, mx.array]:
    prefix = f"transformer.h.{layer}."
    return {
        prefix + "ln_1.weight": mx.ones((4,), dtype=mx.float32),
        prefix + "ln_1.bias": mx.zeros((4,), dtype=mx.float32),
        prefix + "attn.c_attn.weight": _values((4, 12), offset=offset + 1, scale=0.002),
        prefix + "attn.c_attn.bias": _values((12,), offset=offset + 2, scale=0.001),
        prefix + "attn.c_proj.weight": _values((4, 4), offset=offset + 3, scale=0.002),
        prefix + "attn.c_proj.bias": _values((4,), offset=offset + 4, scale=0.001),
        prefix + "ln_2.weight": mx.ones((4,), dtype=mx.float32),
        prefix + "ln_2.bias": mx.zeros((4,), dtype=mx.float32),
        prefix + "mlp.c_fc.weight": _values((4, 8), offset=offset + 5, scale=0.002),
        prefix + "mlp.c_fc.bias": _values((8,), offset=offset + 6, scale=0.001),
        prefix + "mlp.c_proj.weight": _values((8, 4), offset=offset + 7, scale=0.002),
        prefix + "mlp.c_proj.bias": _values((4,), offset=offset + 8, scale=0.001),
    }


def _source_tensors(*, weight_offset: int = 0) -> dict[str, mx.array]:
    tensors = {
        "transformer.wte.weight": _values((7, 4), offset=weight_offset + 1, scale=0.01),
        "transformer.wpe.weight": _values(
            (8, 4), offset=weight_offset + 2, scale=0.005
        ),
        "transformer.ln_f.weight": mx.ones((4,), dtype=mx.float32),
        "transformer.ln_f.bias": mx.zeros((4,), dtype=mx.float32),
        # Stock shards may overfetch other stages. This must never enter the loaded stage.
        "transformer.h.9.ln_1.weight": mx.ones((4,), dtype=mx.float32) * 9,
    }
    tensors.update(_layer_tensors(0, offset=weight_offset + 10))
    tensors.update(_layer_tensors(1, offset=weight_offset + 30))
    return tensors


def _control_plane_binding() -> dict[str, Any]:
    return {
        "protocol": "mycelium.control_plane_binding.v1",
        "evidence_bundle_digest": "sha256:" + "a" * 64,
        "planner_snapshot_digest": "sha256:" + "b" * 64,
        "snapshot_generation": 4,
        "swarm_id": "swarm-test",
        "deployment_id": DEPLOYMENT_ID,
        "deployment_epoch": 7,
    }


def _runtime() -> dict[str, Any]:
    return {
        "backend": "mlx",
        "dtype": "float32",
        "quantization": "none",
        "architecture": "gpt2",
        "model_config": {
            "n_layer": 2,
            "n_embd": 4,
            "n_head": 2,
            "n_inner": 8,
            "vocab_size": 7,
            "n_positions": 8,
            "layer_norm_epsilon": 1e-5,
            "activation_function": "gelu_new",
            "scale_attn_weights": True,
            "scale_attn_by_inverse_layer_idx": False,
            "reorder_and_upcast_attn": False,
            "add_cross_attention": False,
        },
    }


def _rebind(assignment: dict[str, Any], report: dict[str, Any] | None = None) -> None:
    assignment["assignment_id"] = assignment_id_for(assignment)
    if report is not None:
        report["assignment_id"] = assignment["assignment_id"]
        report["verified_tensor_count"] = len(set(assignment["expected_tensor_keys"]))


def _case(
    tmp_path: Path, *, weight_offset: int = 0
) -> tuple[dict[str, Any], dict[str, Any], Path]:
    artifact = tmp_path / "model.safetensors"
    source_tensors = _source_tensors(weight_offset=weight_offset)
    mx.save_safetensors(artifact, source_tensors)

    decoder_keys = sorted(
        f"transformer.h.{layer}.{suffix}"
        for layer in range(2)
        for suffix in LAYER_SUFFIXES
    )
    component_tensor_keys = {
        "input_embedding": ["transformer.wpe.weight", "transformer.wte.weight"],
        "decoder": decoder_keys,
        "final_norm": ["transformer.ln_f.bias", "transformer.ln_f.weight"],
        "lm_head": ["transformer.wte.weight"],
    }
    expected_keys = sorted(
        {
            key
            for component_keys in component_tensor_keys.values()
            for key in component_keys
        }
    )
    size = artifact.stat().st_size
    digest = "sha256:" + sha256_file(artifact)
    assignment = {
        "protocol": "mycelium.layer_assignment.v2",
        "deployment_id": DEPLOYMENT_ID,
        "deployment_epoch": 7,
        "node_id": "node-a",
        "manifest_digest": MANIFEST_DIGEST,
        "model_id": "test/tiny-gpt2",
        "resolved_commit": COMMIT,
        "range": {"start_layer": 0, "end_layer_exclusive": 2, "layer_count": 2},
        "components": ["input_embedding", "decoder", "final_norm", "lm_head"],
        "component_tensor_keys": component_tensor_keys,
        "component_aliases": {"lm_head": "input_embedding"},
        "expected_tensor_prefixes": ["transformer.h.0.", "transformer.h.1."],
        "expected_tensor_keys": expected_keys,
        "files": [
            {"path": artifact.name, "size_bytes": size, "content_digest": digest}
        ],
        "artifact_cache_root": str(tmp_path),
        "runtime": _runtime(),
        "control_plane_binding": _control_plane_binding(),
        "route_ready": False,
        "claim_boundary": "test assignment; runtime not loaded",
    }
    _rebind(assignment)
    report = {
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
                "tensor_count": len(source_tensors),
            }
        ],
        "verified_tensor_prefixes": list(assignment["expected_tensor_prefixes"]),
        "verified_tensor_count": len(expected_keys),
        "expected_bytes": size,
        "network_download_bytes": 0,
        "cache_hit_bytes": size,
        "ready_for_load": True,
        "route_ready": False,
        "claim_boundary": "test artifacts verified; layers not loaded",
    }
    return assignment, report, artifact


def _refresh_file_evidence(
    assignment: dict[str, Any], report: dict[str, Any], artifact: Path
) -> None:
    size = artifact.stat().st_size
    digest = "sha256:" + sha256_file(artifact)
    assignment["files"][0]["size_bytes"] = size
    assignment["files"][0]["content_digest"] = digest
    report["verified_files"][0]["size_bytes"] = size
    report["verified_files"][0]["content_digest"] = digest
    report["expected_bytes"] = size
    report["network_download_bytes"] = 0
    report["cache_hit_bytes"] = size
    _rebind(assignment, report)


def _restrict_assignment(
    assignment: dict[str, Any],
    report: dict[str, Any],
    *,
    start: int,
    end: int,
    components: list[str],
) -> None:
    prefixes = [f"transformer.h.{layer}." for layer in range(start, end)]
    original = assignment["component_tensor_keys"]
    component_keys: dict[str, list[str]] = {}
    for component in components:
        if component == "decoder":
            component_keys[component] = sorted(
                key
                for key in original[component]
                if any(key.startswith(prefix) for prefix in prefixes)
            )
        else:
            component_keys[component] = list(original[component])
    assignment["range"] = {
        "start_layer": start,
        "end_layer_exclusive": end,
        "layer_count": end - start,
    }
    assignment["components"] = components
    assignment["component_tensor_keys"] = component_keys
    assignment["component_aliases"] = {
        source: target
        for source, target in assignment["component_aliases"].items()
        if source in components
    }
    assignment["expected_tensor_prefixes"] = prefixes
    assignment["expected_tensor_keys"] = sorted(
        {key for keys in component_keys.values() for key in keys}
    )
    report["range"] = copy.deepcopy(assignment["range"])
    report["verified_tensor_prefixes"] = prefixes
    _rebind(assignment, report)


def test_loads_exact_assignment_owned_gpt2_stage_and_emits_canonical_proof(
    tmp_path: Path,
) -> None:
    assignment, report, _ = _case(tmp_path)

    loaded = load_assignment_stage(assignment, report, load_generation=11)

    assert set(loaded.tensors) == set(assignment["expected_tensor_keys"])
    assert "transformer.h.9.ln_1.weight" not in loaded.tensors
    assert canonical_json(loaded.resolved_aliases) == canonical_json(
        {
            "lm_head": {
                "target_component": "input_embedding",
                "tensor_keys": ["transformer.wte.weight"],
            }
        }
    )
    proof = loaded.proof
    assert proof["protocol"] == "mycelium.layer_load_proof.v1"
    assert proof["deployment_id"] == assignment["deployment_id"]
    assert proof["deployment_epoch"] == assignment["deployment_epoch"]
    assert proof["assignment_id"] == assignment["assignment_id"]
    assert proof["node_id"] == assignment["node_id"]
    assert proof["model_id"] == assignment["model_id"]
    assert proof["manifest_digest"] == assignment["manifest_digest"]
    assert proof["resolved_commit"] == assignment["resolved_commit"]
    assert proof["loaded_range"] == assignment["range"]
    assert list(proof["loaded_components"]) == assignment["components"]
    assert list(proof["loaded_tensor_keys"]) == sorted(
        assignment["expected_tensor_keys"]
    )
    assert proof["loaded_tensor_digest"].startswith("sha256:")
    assert proof["runtime"] == assignment["runtime"]
    assert tuple(proof["probe_shape"]) == (1, 3, 7)
    assert proof["probe_digest"].startswith("sha256:")
    assert proof["load_generation"] == 11
    assert proof["control_plane_binding"] == assignment["control_plane_binding"]
    assert proof["route_ready"] is False
    assert proof["claim_boundary"] == (
        "assignment-bound local MLX stage loaded and deterministically probed; "
        "no route challenge or distributed inference claim"
    )
    assert set(proof) == {
        "protocol",
        "deployment_id",
        "deployment_epoch",
        "assignment_id",
        "node_id",
        "model_id",
        "manifest_digest",
        "resolved_commit",
        "loaded_range",
        "loaded_components",
        "loaded_tensor_keys",
        "loaded_tensor_digest",
        "resolved_component_aliases",
        "runtime",
        "runtime_identity",
        "probe_shape",
        "probe_digest",
        "load_generation",
        "control_plane_binding",
        "route_ready",
        "claim_boundary",
    }
    assert canonical_json({"z": 1, "a": 2}) == '{"a":2,"z":1}'
    serialized = canonical_json(proof)
    assert canonical_json(json.loads(serialized)) == serialized


def test_emitted_proof_and_alias_evidence_are_deeply_immutable(tmp_path: Path) -> None:
    assignment, report, _ = _case(tmp_path)
    loaded = load_assignment_stage(assignment, report, load_generation=11)
    before = canonical_json(loaded.proof)

    def mutate(mapping: Any, key: str, value: Any) -> None:
        mapping[key] = value

    with pytest.raises(TypeError):
        mutate(loaded.proof, "route_ready", True)
    with pytest.raises(TypeError):
        mutate(loaded.proof["runtime"]["model_config"], "n_layer", 999)
    with pytest.raises(TypeError):
        mutate(loaded.proof["control_plane_binding"], "deployment_epoch", 999)
    with pytest.raises(TypeError):
        mutate(loaded.resolved_aliases["lm_head"], "target_component", "decoder")
    with pytest.raises(AttributeError):
        loaded.proof["loaded_components"].append("forged")

    assert canonical_json(loaded.proof) == before


def test_loads_untied_lm_head_explicitly(tmp_path: Path) -> None:
    assignment, report, artifact = _case(tmp_path)
    tensors = _source_tensors()
    tensors["lm_head.weight"] = mx.ones((7, 4), dtype=mx.float32)
    mx.save_safetensors(artifact, tensors)
    assignment["component_aliases"] = {}
    assignment["component_tensor_keys"]["lm_head"] = ["lm_head.weight"]
    assignment["expected_tensor_keys"] = sorted(
        set(assignment["expected_tensor_keys"]) | {"lm_head.weight"}
    )
    _refresh_file_evidence(assignment, report, artifact)

    loaded = load_assignment_stage(assignment, report, load_generation=2)

    assert "lm_head.weight" in loaded.tensors
    assert loaded.resolved_aliases == {}
    assert loaded.proof["resolved_component_aliases"] == {}


def test_loads_explicit_unnamespaced_gpt2_tensor_layout(tmp_path: Path) -> None:
    assignment, report, artifact = _case(tmp_path)
    tensors = {
        key.removeprefix("transformer."): value
        for key, value in _source_tensors().items()
    }
    mx.save_safetensors(artifact, tensors)
    assignment["expected_tensor_prefixes"] = ["h.0.", "h.1."]
    assignment["component_tensor_keys"] = {
        component: sorted(key.removeprefix("transformer.") for key in keys)
        for component, keys in assignment["component_tensor_keys"].items()
    }
    assignment["expected_tensor_keys"] = sorted(
        key.removeprefix("transformer.") for key in assignment["expected_tensor_keys"]
    )
    report["verified_tensor_prefixes"] = list(assignment["expected_tensor_prefixes"])
    _refresh_file_evidence(assignment, report, artifact)

    loaded = load_assignment_stage(assignment, report, load_generation=3)

    assert "h.0.attn.c_attn.weight" in loaded.tensors
    assert "wte.weight" in loaded.tensors
    assert all(not key.startswith("transformer.") for key in loaded.tensors)


def test_canonical_json_rejects_nonfinite_numbers() -> None:
    with pytest.raises(ValueError, match="JSON compliant"):
        canonical_json({"invalid": float("nan")})


@pytest.mark.parametrize(
    ("start", "end", "components", "probe_shape", "forbidden_keys"),
    [
        (
            0,
            1,
            ["input_embedding", "decoder"],
            [1, 3, 4],
            ["transformer.h.1.ln_1.weight", "transformer.ln_f.weight"],
        ),
        (
            0,
            1,
            ["decoder"],
            [1, 3, 4],
            ["transformer.wte.weight", "transformer.h.1.ln_1.weight"],
        ),
        (
            1,
            2,
            ["decoder", "final_norm", "lm_head"],
            [1, 3, 7],
            ["transformer.wpe.weight", "transformer.h.0.ln_1.weight"],
        ),
    ],
)
def test_materializes_only_range_and_static_components_owned_by_assignment(
    tmp_path: Path,
    start: int,
    end: int,
    components: list[str],
    probe_shape: list[int],
    forbidden_keys: list[str],
) -> None:
    assignment, report, _ = _case(tmp_path)
    _restrict_assignment(
        assignment, report, start=start, end=end, components=components
    )

    loaded = load_assignment_stage(assignment, report, load_generation=2)

    assert set(loaded.tensors) == set(assignment["expected_tensor_keys"])
    assert tuple(loaded.proof["probe_shape"]) == tuple(probe_shape)
    for key in forbidden_keys:
        assert key not in loaded.tensors


@pytest.mark.parametrize("runtime_dtype", ["float16", "bfloat16", "float32"])
def test_materializes_declared_runtime_dtype(
    tmp_path: Path, runtime_dtype: str
) -> None:
    assignment, report, _ = _case(tmp_path)
    assignment["runtime"]["dtype"] = runtime_dtype
    _rebind(assignment, report)

    loaded = load_assignment_stage(assignment, report, load_generation=5)

    assert {str(tensor.dtype) for tensor in loaded.tensors.values()} == {
        f"mlx.core.{runtime_dtype}"
    }
    assert loaded.proof["runtime_identity"]["dtype"] == runtime_dtype


def test_probe_and_proof_are_deterministic_for_same_assignment(tmp_path: Path) -> None:
    assignment, report, _ = _case(tmp_path)

    first = load_assignment_stage(assignment, report, load_generation=3)
    second = load_assignment_stage(assignment, report, load_generation=3)

    assert canonical_json(first.proof) == canonical_json(second.proof)
    assert bytes(first.probe_output) == bytes(second.probe_output)


def test_probe_digest_changes_when_verified_weight_content_changes(
    tmp_path: Path,
) -> None:
    first_root = tmp_path / "first"
    second_root = tmp_path / "second"
    first_root.mkdir()
    second_root.mkdir()
    assignment_a, report_a, _ = _case(first_root, weight_offset=0)
    assignment_b, report_b, _ = _case(second_root, weight_offset=17)

    first = load_assignment_stage(assignment_a, report_a, load_generation=1)
    second = load_assignment_stage(assignment_b, report_b, load_generation=1)

    assert first.proof["probe_digest"] != second.proof["probe_digest"]
    assert first.proof["loaded_tensor_digest"] != second.proof["loaded_tensor_digest"]


def test_decoder_only_probe_changes_when_decoder_weight_changes(tmp_path: Path) -> None:
    first_root = tmp_path / "first"
    second_root = tmp_path / "second"
    first_root.mkdir()
    second_root.mkdir()
    assignment_a, report_a, _ = _case(first_root)
    assignment_b, report_b, artifact_b = _case(second_root)
    _restrict_assignment(assignment_a, report_a, start=0, end=1, components=["decoder"])
    _restrict_assignment(assignment_b, report_b, start=0, end=1, components=["decoder"])
    tensors = _source_tensors()
    tensors["transformer.h.0.attn.c_proj.bias"] = mx.ones((4,), dtype=mx.float32)
    mx.save_safetensors(artifact_b, tensors)
    _refresh_file_evidence(assignment_b, report_b, artifact_b)

    first = load_assignment_stage(assignment_a, report_a, load_generation=1)
    second = load_assignment_stage(assignment_b, report_b, load_generation=1)

    assert first.proof["probe_digest"] != second.proof["probe_digest"]


def test_causal_attention_never_reads_future_token() -> None:
    hidden_a = mx.array([[[2.0, -1.0], [-1.0, 2.0]]], dtype=mx.float32)
    hidden_b = mx.array([[[2.0, -1.0], [2.0, -1.0]]], dtype=mx.float32)
    tensors = {
        "ln_1.weight": mx.ones((2,), dtype=mx.float32),
        "ln_1.bias": mx.zeros((2,), dtype=mx.float32),
        "attn.c_attn.weight": mx.array(
            [[0.0, 0.0, 0.0, 0.0, 1.0, 0.0], [0.0, 0.0, 0.0, 0.0, 0.0, 1.0]],
            dtype=mx.float32,
        ),
        "attn.c_attn.bias": mx.array(
            [2000.0, 2000.0, -2000.0, -2000.0, 0.0, 0.0],
            dtype=mx.float32,
        ),
        "attn.c_proj.weight": mx.eye(2, dtype=mx.float32),
        "attn.c_proj.bias": mx.zeros((2,), dtype=mx.float32),
        "ln_2.weight": mx.ones((2,), dtype=mx.float32),
        "ln_2.bias": mx.zeros((2,), dtype=mx.float32),
        "mlp.c_fc.weight": mx.zeros((2, 4), dtype=mx.float32),
        "mlp.c_fc.bias": mx.zeros((4,), dtype=mx.float32),
        "mlp.c_proj.weight": mx.zeros((4, 2), dtype=mx.float32),
        "mlp.c_proj.bias": mx.zeros((2,), dtype=mx.float32),
    }

    output_a = _gpt2_block(hidden_a, tensors, "", n_head=1, epsilon=1e-5)
    output_b = _gpt2_block(hidden_b, tensors, "", n_head=1, epsilon=1e-5)
    mx.eval(output_a, output_b)

    assert bool(mx.allclose(output_a[:, 0, :], output_b[:, 0, :]).item())


def test_decoder_probe_changes_when_query_and_key_weights_change(tmp_path: Path) -> None:
    first_root = tmp_path / "first"
    second_root = tmp_path / "second"
    first_root.mkdir()
    second_root.mkdir()
    assignment_a, report_a, _ = _case(first_root)
    assignment_b, report_b, artifact_b = _case(second_root)
    _restrict_assignment(assignment_a, report_a, start=0, end=1, components=["decoder"])
    _restrict_assignment(assignment_b, report_b, start=0, end=1, components=["decoder"])
    tensors = _source_tensors()
    key = "transformer.h.0.attn.c_attn.weight"
    tensors[key] = mx.concatenate(
        [tensors[key][:, :8] * -17.0 + 3.0, tensors[key][:, 8:]], axis=1
    )
    mx.save_safetensors(artifact_b, tensors)
    _refresh_file_evidence(assignment_b, report_b, artifact_b)

    first = load_assignment_stage(assignment_a, report_a, load_generation=1)
    second = load_assignment_stage(assignment_b, report_b, load_generation=1)

    assert first.proof["probe_digest"] != second.proof["probe_digest"]


def test_entry_probe_changes_when_third_position_embedding_changes(tmp_path: Path) -> None:
    first_root = tmp_path / "first"
    second_root = tmp_path / "second"
    first_root.mkdir()
    second_root.mkdir()
    assignment_a, report_a, _ = _case(first_root)
    assignment_b, report_b, artifact_b = _case(second_root)
    tensors = _source_tensors()
    position_embeddings = tensors["transformer.wpe.weight"]
    tensors["transformer.wpe.weight"] = mx.concatenate(
        [position_embeddings[:2], position_embeddings[2:3] + 100.0, position_embeddings[3:]],
        axis=0,
    )
    mx.save_safetensors(artifact_b, tensors)
    _refresh_file_evidence(assignment_b, report_b, artifact_b)

    first = load_assignment_stage(assignment_a, report_a, load_generation=1)
    second = load_assignment_stage(assignment_b, report_b, load_generation=1)

    assert first.proof["probe_digest"] != second.proof["probe_digest"]


def test_rejects_gpt2_config_too_small_for_three_token_probe(tmp_path: Path) -> None:
    assignment, report, _ = _case(tmp_path)
    assignment["runtime"]["model_config"]["vocab_size"] = 2
    assignment["runtime"]["model_config"]["n_positions"] = 2
    _rebind(assignment, report)

    with pytest.raises(RuntimeLoadError, match="at least 3"):
        load_assignment_stage(assignment, report, load_generation=1)


def test_rejects_static_component_at_wrong_stage_boundary(tmp_path: Path) -> None:
    assignment, report, _ = _case(tmp_path)
    _restrict_assignment(
        assignment,
        report,
        start=1,
        end=2,
        components=["input_embedding", "decoder", "final_norm", "lm_head"],
    )

    with pytest.raises(RuntimeLoadError, match="input_embedding.*first layer"):
        load_assignment_stage(assignment, report, load_generation=1)


def test_rejects_mlp_width_inconsistent_with_bound_model_config(
    tmp_path: Path,
) -> None:
    assignment, report, artifact = _case(tmp_path)
    tensors = _source_tensors()
    tensors["transformer.h.0.mlp.c_fc.weight"] = mx.ones((4, 7), dtype=mx.float32)
    tensors["transformer.h.0.mlp.c_fc.bias"] = mx.ones((7,), dtype=mx.float32)
    tensors["transformer.h.0.mlp.c_proj.weight"] = mx.ones((7, 4), dtype=mx.float32)
    mx.save_safetensors(artifact, tensors)
    _refresh_file_evidence(assignment, report, artifact)

    with pytest.raises(RuntimeLoadError, match="mlp.c_fc.weight"):
        load_assignment_stage(assignment, report, load_generation=1)


def test_rejects_tampered_assignment_identity_before_loading(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assignment, report, _ = _case(tmp_path)
    assignment["model_id"] = "attacker/substitute"
    called = False

    def forbidden_load(*args: Any, **kwargs: Any) -> Any:
        nonlocal called
        called = True
        raise AssertionError("MLX load must not run")

    monkeypatch.setattr(mx, "load", forbidden_load)
    with pytest.raises(RuntimeLoadError, match="assignment_id"):
        load_assignment_stage(assignment, report, load_generation=1)
    assert called is False


def test_requires_assignment_control_plane_binding(tmp_path: Path) -> None:
    assignment, report, _ = _case(tmp_path)
    assignment["control_plane_binding"] = None
    _rebind(assignment, report)

    with pytest.raises(RuntimeLoadError, match="control_plane_binding"):
        load_assignment_stage(assignment, report, load_generation=1)


def test_rejects_control_plane_binding_identity_mismatch(tmp_path: Path) -> None:
    assignment, report, _ = _case(tmp_path)
    assignment["control_plane_binding"]["deployment_epoch"] = 8
    _rebind(assignment, report)

    with pytest.raises(RuntimeLoadError, match="control-plane deployment_epoch"):
        load_assignment_stage(assignment, report, load_generation=1)


def test_rejects_mismatched_or_unverified_artifact_report(tmp_path: Path) -> None:
    assignment, report, _ = _case(tmp_path)
    report["ready_for_load"] = False
    report["assignment_id"] = "00000000-0000-0000-0000-000000000000"

    with pytest.raises(RuntimeLoadError, match="artifact verification report"):
        load_assignment_stage(assignment, report, load_generation=1)


def test_rejects_wrong_artifact_report_protocol(tmp_path: Path) -> None:
    assignment, report, _ = _case(tmp_path)
    report["protocol"] = "mycelium.artifact_verification_report.v0"

    with pytest.raises(RuntimeLoadError, match="wrong report protocol"):
        load_assignment_stage(assignment, report, load_generation=1)


def test_rejects_assigned_artifact_path_traversal(tmp_path: Path) -> None:
    assignment, report, _ = _case(tmp_path)
    assignment["files"][0]["path"] = "../model.safetensors"
    report["verified_files"][0]["path"] = "../model.safetensors"
    _rebind(assignment, report)

    with pytest.raises(RuntimeLoadError, match=r"unsafe .*artifact path"):
        load_assignment_stage(assignment, report, load_generation=1)


def test_rejects_verified_local_path_outside_cache_root(tmp_path: Path) -> None:
    cache_root = tmp_path / "cache"
    cache_root.mkdir()
    assignment, report, artifact = _case(cache_root)
    escaped = tmp_path / artifact.name
    escaped.write_bytes(artifact.read_bytes())
    report["verified_files"][0]["local_path"] = str(escaped.resolve())

    with pytest.raises(RuntimeLoadError, match="escapes artifact cache root"):
        load_assignment_stage(assignment, report, load_generation=1)


def test_rejects_multiply_linked_artifact(tmp_path: Path) -> None:
    assignment, report, artifact = _case(tmp_path)
    (tmp_path / "artifact-alias.safetensors").hardlink_to(artifact)

    with pytest.raises(RuntimeLoadError, match="exactly one hard link"):
        load_assignment_stage(assignment, report, load_generation=1)


def test_rejects_non_safetensors_artifact_even_when_report_matches(
    tmp_path: Path,
) -> None:
    assignment, report, artifact = _case(tmp_path)
    artifact.write_bytes(b"not a Safetensors container")
    _refresh_file_evidence(assignment, report, artifact)

    with pytest.raises(RuntimeLoadError, match="Safetensors header"):
        load_assignment_stage(assignment, report, load_generation=1)


def test_rejects_file_tampering_after_artifact_report(tmp_path: Path) -> None:
    assignment, report, artifact = _case(tmp_path)
    content = bytearray(artifact.read_bytes())
    content[-1] ^= 1
    artifact.write_bytes(content)

    with pytest.raises(RuntimeLoadError, match="digest mismatch"):
        load_assignment_stage(assignment, report, load_generation=1)


def test_rejects_missing_expected_tensor(tmp_path: Path) -> None:
    assignment, report, artifact = _case(tmp_path)
    tensors = _source_tensors()
    tensors.pop("transformer.h.0.attn.c_proj.bias")
    mx.save_safetensors(artifact, tensors)
    _refresh_file_evidence(assignment, report, artifact)

    with pytest.raises(RuntimeLoadError, match="missing assigned tensors"):
        load_assignment_stage(assignment, report, load_generation=1)


def test_rejects_extra_or_unowned_expected_tensor(tmp_path: Path) -> None:
    assignment, report, _ = _case(tmp_path)
    assignment["expected_tensor_keys"].append("transformer.h.9.ln_1.weight")
    assignment["expected_tensor_keys"].sort()
    _rebind(assignment, report)

    with pytest.raises(RuntimeLoadError, match="component ownership"):
        load_assignment_stage(assignment, report, load_generation=1)


def test_rejects_duplicate_expected_tensor_key(tmp_path: Path) -> None:
    assignment, report, _ = _case(tmp_path)
    assignment["expected_tensor_keys"].append(assignment["expected_tensor_keys"][0])
    _rebind(assignment, report)

    with pytest.raises(RuntimeLoadError, match="duplicate expected tensor"):
        load_assignment_stage(assignment, report, load_generation=1)


def test_rejects_tensor_serialized_in_two_verified_files(tmp_path: Path) -> None:
    assignment, report, _ = _case(tmp_path)
    duplicate_path = tmp_path / "duplicate.safetensors"
    duplicate_key = assignment["expected_tensor_keys"][0]
    mx.save_safetensors(
        duplicate_path, {duplicate_key: mx.ones((4,), dtype=mx.float32)}
    )
    duplicate_record = {
        "path": duplicate_path.name,
        "size_bytes": duplicate_path.stat().st_size,
        "content_digest": "sha256:" + sha256_file(duplicate_path),
    }
    assignment["files"].append(duplicate_record)
    assignment["files"].sort(key=lambda item: item["path"])
    report["verified_files"].append(
        {
            **duplicate_record,
            "local_path": str(duplicate_path.resolve()),
            "cache_hit": True,
            "tensor_count": 1,
        }
    )
    report["verified_files"].sort(key=lambda item: item["path"])
    report["expected_bytes"] += duplicate_record["size_bytes"]
    report["cache_hit_bytes"] += duplicate_record["size_bytes"]
    _rebind(assignment, report)

    with pytest.raises(RuntimeLoadError, match="duplicate assigned tensor"):
        load_assignment_stage(assignment, report, load_generation=1)


def test_rejects_duplicate_tensor_name_inside_safetensors_header(
    tmp_path: Path,
) -> None:
    assignment, report, artifact = _case(tmp_path)
    content = artifact.read_bytes()
    header_length = struct.unpack("<Q", content[:8])[0]
    header = json.loads(content[8 : 8 + header_length])
    name, metadata = next(
        (key, value) for key, value in header.items() if key != "__metadata__"
    )
    original_json = content[8 : 8 + header_length].decode("utf-8").rstrip()
    duplicate_entry = (
        json.dumps(name) + ":" + json.dumps(metadata, separators=(",", ":"))
    )
    duplicate_json = ("{" + duplicate_entry + "," + original_json[1:]).encode("utf-8")
    duplicate_json += b" " * ((8 - len(duplicate_json) % 8) % 8)
    data = content[8 + header_length :]
    artifact.write_bytes(struct.pack("<Q", len(duplicate_json)) + duplicate_json + data)
    _refresh_file_evidence(assignment, report, artifact)

    with pytest.raises(RuntimeLoadError, match="duplicate Safetensors"):
        load_assignment_stage(assignment, report, load_generation=1)


def test_rejects_overlapping_safetensors_storage_offsets(tmp_path: Path) -> None:
    assignment, report, artifact = _case(tmp_path)
    content = artifact.read_bytes()
    header_length = struct.unpack("<Q", content[:8])[0]
    header = json.loads(content[8 : 8 + header_length])
    source = "transformer.h.0.ln_1.weight"
    target = "transformer.h.0.ln_1.bias"
    header[target]["data_offsets"] = list(header[source]["data_offsets"])
    rewritten = json.dumps(header, separators=(",", ":")).encode("utf-8")
    rewritten += b" " * ((8 - len(rewritten) % 8) % 8)
    artifact.write_bytes(
        struct.pack("<Q", len(rewritten)) + rewritten + content[8 + header_length :]
    )
    _refresh_file_evidence(assignment, report, artifact)

    with pytest.raises(RuntimeLoadError, match="overlapping Safetensors data"):
        load_assignment_stage(assignment, report, load_generation=1)


def test_rejects_unindexed_trailing_safetensors_data(tmp_path: Path) -> None:
    assignment, report, artifact = _case(tmp_path)
    artifact.write_bytes(artifact.read_bytes() + b"trailing")
    _refresh_file_evidence(assignment, report, artifact)

    with pytest.raises(RuntimeLoadError, match="unindexed trailing Safetensors data"):
        load_assignment_stage(assignment, report, load_generation=1)


def test_rejects_nonfinite_loaded_tensor(tmp_path: Path) -> None:
    assignment, report, artifact = _case(tmp_path)
    tensors = _source_tensors()
    tensors["transformer.h.0.attn.c_proj.bias"] = mx.array(
        [0.0, float("inf"), 0.0, 0.0], dtype=mx.float32
    )
    mx.save_safetensors(artifact, tensors)
    _refresh_file_evidence(assignment, report, artifact)

    with pytest.raises(RuntimeLoadError, match="non-finite"):
        load_assignment_stage(assignment, report, load_generation=1)


def test_wraps_mlx_execution_failure_as_fail_closed_runtime_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assignment, report, _ = _case(tmp_path)

    def fail_eval(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("device execution failed")

    monkeypatch.setattr(mx, "eval", fail_eval)
    with pytest.raises(RuntimeLoadError, match="runtime load rejected"):
        load_assignment_stage(assignment, report, load_generation=1)


@pytest.mark.parametrize(
    ("runtime_patch", "message"),
    [
        ({"backend": "torch"}, "runtime backend"),
        ({"quantization": "int4"}, "quantization"),
        ({"architecture": "qwen2"}, "architecture"),
        ({"dtype": "source"}, "runtime dtype"),
    ],
)
def test_rejects_unsupported_runtime_cases(
    tmp_path: Path, runtime_patch: dict[str, Any], message: str
) -> None:
    assignment, report, _ = _case(tmp_path)
    assignment["runtime"].update(runtime_patch)
    _rebind(assignment, report)

    with pytest.raises(RuntimeLoadError, match=message):
        load_assignment_stage(assignment, report, load_generation=1)


def test_rejects_mismatched_gpt2_tensor_shape(tmp_path: Path) -> None:
    assignment, report, artifact = _case(tmp_path)
    tensors = _source_tensors()
    tensors["transformer.h.0.attn.c_attn.weight"] = mx.ones((4, 11), dtype=mx.float32)
    mx.save_safetensors(artifact, tensors)
    _refresh_file_evidence(assignment, report, artifact)

    with pytest.raises(RuntimeLoadError, match="shape mismatch"):
        load_assignment_stage(assignment, report, load_generation=1)


def test_rejects_unsupported_alias_instead_of_silently_guessing(tmp_path: Path) -> None:
    assignment, report, _ = _case(tmp_path)
    assignment["component_aliases"]["lm_head"] = "final_norm"
    _rebind(assignment, report)

    with pytest.raises(RuntimeLoadError, match="unsupported component alias"):
        load_assignment_stage(assignment, report, load_generation=1)


@pytest.mark.parametrize("generation", [-1, True, 1.5, "1"])
def test_rejects_invalid_load_generation(tmp_path: Path, generation: Any) -> None:
    assignment, report, _ = _case(tmp_path)

    with pytest.raises(RuntimeLoadError, match="load_generation"):
        load_assignment_stage(assignment, report, load_generation=generation)
