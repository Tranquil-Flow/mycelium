"""M23: assignment-bound NumPy Qwen stages retain and release local KV."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import mlx.core as mx
import numpy as np
import pytest

from layer_assignment import compile_layer_assignments
from model_manifest import compile_model_manifest, manifest_digest_ref
from mycelium_router.numpy_runtime import (
    NumpyRuntimePort,
)
from mycelium_router.payloads import encode_token_ids
from numpy_runtime import execute_loaded_stage
from physical_inference_node import _route_decode_mode
from runtime_loader import load_assignment_stage
from test_router_numpy_runtime import _build_graph, _work_item
from two_process_runtime_qualification import _control_plane_binding
from weight_provisioning import provision_assignment, sha256_file


_COMMIT = "1234567890abcdef1234567890abcdef12345678"
_DEPLOYMENT_ID = "23456789-2345-6789-a345-bcdefabcdefa"
_SHARDS = ("model-00001-of-00002.safetensors", "model-00002-of-00002.safetensors")


def _config(architecture: str) -> dict[str, object]:
    common: dict[str, object] = {
        "model_type": architecture,
        "architectures": [
            "Qwen2ForCausalLM" if architecture == "qwen2" else "Qwen3ForCausalLM"
        ],
        "num_hidden_layers": 2,
        "hidden_size": 8,
        "num_attention_heads": 2,
        "num_key_value_heads": 1,
        "intermediate_size": 16,
        "vocab_size": 32,
        "max_position_embeddings": 64,
        "rms_norm_eps": 1e-6,
        "rope_theta": 1_000_000.0,
        "head_dim": 4,
        "hidden_act": "silu",
        "tie_word_embeddings": False,
        "attention_dropout": 0.0,
        "rope_scaling": None,
    }
    common["attention_bias"] = architecture == "qwen2"
    return common


def _tensors(architecture: str) -> dict[str, np.ndarray]:
    rng = np.random.default_rng(20260811 if architecture == "qwen2" else 20260812)
    tensors: dict[str, np.ndarray] = {
        "model.embed_tokens.weight": rng.normal(0.0, 0.05, (32, 8)).astype(np.float32),
        "model.norm.weight": rng.normal(1.0, 0.02, (8,)).astype(np.float32),
        "lm_head.weight": rng.normal(0.0, 0.05, (32, 8)).astype(np.float32),
    }
    shapes: dict[str, tuple[int, ...]] = {
        "input_layernorm.weight": (8,),
        "self_attn.q_proj.weight": (8, 8),
        "self_attn.k_proj.weight": (4, 8),
        "self_attn.v_proj.weight": (4, 8),
        "self_attn.o_proj.weight": (8, 8),
        "post_attention_layernorm.weight": (8,),
        "mlp.gate_proj.weight": (16, 8),
        "mlp.up_proj.weight": (16, 8),
        "mlp.down_proj.weight": (8, 16),
    }
    if architecture == "qwen2":
        shapes.update(
            {
                "self_attn.q_proj.bias": (8,),
                "self_attn.k_proj.bias": (4,),
                "self_attn.v_proj.bias": (4,),
            }
        )
    else:
        shapes.update(
            {
                "self_attn.q_norm.weight": (4,),
                "self_attn.k_norm.weight": (4,),
            }
        )
    for layer in range(2):
        for suffix, shape in shapes.items():
            if suffix.endswith("norm.weight") or suffix.endswith("layernorm.weight"):
                value = rng.normal(1.0, 0.02, shape)
            elif suffix.endswith(".bias"):
                value = rng.normal(0.0, 0.005, shape)
            else:
                value = rng.normal(0.0, 0.04, shape)
            tensors[f"model.layers.{layer}.{suffix}"] = value.astype(np.float32)
    return tensors


class _LocalFetcher:
    def __init__(self, root: Path, model_id: str) -> None:
        self.root = root
        self.model_id = model_id

    def __call__(
        self,
        model_id: str,
        revision: str,
        filename: str,
        cache_root: str | Path,
        local_files_only: bool = False,
    ) -> tuple[Path, bool]:
        assert model_id == self.model_id
        assert revision == _COMMIT
        assert Path(cache_root).resolve() == self.root.resolve()
        assert local_files_only is True
        candidate = (self.root / filename).resolve()
        candidate.relative_to(self.root.resolve())
        assert candidate.is_file()
        return candidate, True


def _case(root: Path, architecture: str, *, clock=None) -> SimpleNamespace:
    model_id = f"local/tiny-{architecture}-kv"
    tensors = _tensors(architecture)
    shards = (
        {
            "model.embed_tokens.weight": tensors["model.embed_tokens.weight"],
            **{
                key: value
                for key, value in tensors.items()
                if key.startswith("model.layers.0.")
            },
        },
        {
            **{
                key: value
                for key, value in tensors.items()
                if key.startswith("model.layers.1.")
            },
            "model.norm.weight": tensors["model.norm.weight"],
            "lm_head.weight": tensors["lm_head.weight"],
        },
    )
    for name, shard in zip(_SHARDS, shards):
        mx.save_safetensors(
            str(root / name), {key: mx.array(value) for key, value in shard.items()}
        )
    weight_map = {
        key: name
        for name, shard in zip(_SHARDS, shards)
        for key in sorted(shard)
    }
    checkpoint_index = {"weight_map": weight_map}
    manifest = compile_model_manifest(
        model_id=model_id,
        requested_revision="offline-generated",
        resolved_commit=_COMMIT,
        config=_config(architecture),
        checkpoint_index=checkpoint_index,
        file_metadata={
            name: {
                "size_bytes": (root / name).stat().st_size,
                "sha256": sha256_file(root / name),
            }
            for name in _SHARDS
        },
    )
    route = {
        "ok": True,
        "protocol": "mycelium.manual_provisioning_route.v1",
        "claim_boundary": "offline M23 NumPy KV contract fixture",
        "model": {
            "model_id": model_id,
            "num_layers": 2,
            "manifest_digest": manifest_digest_ref(manifest),
            "resolved_commit": _COMMIT,
        },
        "route": [
            {"node_id": "node-a", "range": {"start_layer": 0, "end_layer_exclusive": 1, "layer_count": 1}},
            {"node_id": "node-b", "range": {"start_layer": 1, "end_layer_exclusive": 2, "layer_count": 1}},
        ],
        "node_order": ["node-a", "node-b"],
    }
    binding = {**_control_plane_binding(), "deployment_id": _DEPLOYMENT_ID}
    assignments = compile_layer_assignments(
        route_plan=route,
        manifest=manifest,
        deployment_id=_DEPLOYMENT_ID,
        deployment_epoch=1,
        cache_roots={"node-a": str(root), "node-b": str(root)},
        runtime_by_node={
            node: {"backend": "numpy", "dtype": "float32", "quantization": "none"}
            for node in ("node-a", "node-b")
        },
        control_plane_binding=binding,
    )
    fetcher = _LocalFetcher(root, model_id)
    reports = [
        provision_assignment(assignment, fetch_file=fetcher, local_files_only=True)
        for assignment in assignments
    ]
    loaded = tuple(
        load_assignment_stage(assignment, report, load_generation=23)
        for assignment, report in zip(assignments, reports)
    )
    graph = _build_graph(assignments, loaded)
    ports = tuple(
        NumpyRuntimePort(
            assignment["node_id"],
            graph,
            {graph.stages[index].placements[0].placement_id: loaded[index]},
            clock=clock,
            decode_mode="stage_local_kv",
        )
        for index, assignment in enumerate(assignments)
    )
    return SimpleNamespace(
        assignments=tuple(assignments), loaded=loaded, graph=graph, ports=ports
    )


def _run(case: SimpleNamespace, token_ids: tuple[int, ...], *, phase: str, path_id: str, request_id: str, token_index: int, position: int, terminal: bool = False):
    payload = encode_token_ids(token_ids)
    for stage_index, port in enumerate(case.ports):
        result = port.execute(
            _work_item(
                case,
                stage_index,
                payload,
                phase=phase,
                token_span=len(token_ids) if phase != "DECODE" else 1,
                request_id=request_id,
                path_id=path_id,
                token_index=token_index,
                position=position,
                terminal=terminal,
                lease_expires_at=1_000_000_000_000.0,
            )
        )
        assert result.success, result.failure_reason
        if result.payload is not None:
            payload = result.payload
    return result.token_id


def _reference(case: SimpleNamespace, tokens: tuple[int, ...]) -> int:
    hidden = execute_loaded_stage(
        case.loaded[0], token_ids=np.array((tokens,), dtype=np.int64)
    )
    logits = execute_loaded_stage(case.loaded[1], hidden_states=hidden)
    return int(np.argmax(logits[0, -1]))


@pytest.mark.parametrize("architecture", ["qwen2", "qwen3"])
def test_assignment_bound_numpy_qwen_kv_parity_and_cleanup(
    tmp_path: Path, architecture: str
) -> None:
    case = _case(tmp_path, architecture)
    prompt = (1, 7, 11, 5)
    path_id = f"path:{architecture}:kv"
    request_id = f"request:{architecture}:kv"
    first = _run(
        case,
        prompt,
        phase="PREFILL",
        path_id=path_id,
        request_id=request_id,
        token_index=-1,
        position=0,
    )
    assert first == _reference(case, prompt)
    for stage_index, port in enumerate(case.ports):
        snapshot = port.kv_snapshot()
        assert snapshot["mode"] == "stage_local_kv"
        assert snapshot["backend"] == "numpy"
        state = snapshot["states"][path_id]
        assert state["placement_id"] == case.graph.stages[stage_index].placements[0].placement_id
        assert state["next_position"] == len(prompt)
        assert state["cached_context_tokens"] == len(prompt)
        assert state["layer_count"] == 1
        assert state["kv_bytes"] > 0

    context = list(prompt)
    token = first
    for token_index in range(1, 4):
        context.append(token)
        token = _run(
            case,
            (token,),
            phase="DECODE",
            path_id=path_id,
            request_id=request_id,
            token_index=token_index,
            position=len(context) - 1,
            terminal=token_index == 3,
        )
        assert token == _reference(case, tuple(context))
    for port in case.ports:
        snapshot = port.kv_snapshot()
        assert snapshot["active_state_count"] == 0
        assert snapshot["release_counts"]["normal_completion"] == 1
        assert snapshot["prefill_operation_count"] == 1
        assert snapshot["prefill_input_token_count"] == len(prompt)
        assert snapshot["decode_operation_count"] == 3
        assert snapshot["decode_input_token_count"] == 3
        assert snapshot["peak_kv_bytes"] > 0
        if port is case.ports[0]:
            assert snapshot["activation_output_bytes"] > 0
        else:
            assert snapshot["activation_output_bytes"] == 0


def test_numpy_kv_identity_replay_and_lifecycle_fail_closed(tmp_path: Path) -> None:
    case = _case(tmp_path, "qwen2")
    port = case.ports[0]
    item = _work_item(
        case,
        0,
        encode_token_ids((1, 2, 3)),
        request_id="request:lifecycle",
        path_id="path:lifecycle",
        lease_expires_at=1_000_000_000_000.0,
    )
    first = port.execute(item)
    assert first.success
    assert port.execute(item) == first
    assert port.kv_snapshot()["applied_operation_count"] == 1
    conflict = port.execute(replace(item, payload=encode_token_ids((3, 2, 1))))
    assert conflict.failure_reason == "kv_sequence_replay_conflict"

    decode = _work_item(
        case,
        0,
        encode_token_ids((4,)),
        phase="DECODE",
        token_span=1,
        request_id=item.request_id,
        path_id=item.path_id,
        token_index=1,
        position=3,
        lease_expires_at=item.lease_expires_at,
    )
    for changed, reason in (
        ({"request_id": "wrong"}, "kv_request_id_mismatch"),
        ({"path_attempt": 1}, "kv_path_attempt_mismatch"),
        ({"position": 4}, "kv_position_mismatch"),
        ({"token_index": 2}, "kv_sequence_mismatch"),
        ({"lease_expires_at": item.lease_expires_at - 1}, "kv_lease_mismatch"),
    ):
        assert port.execute(replace(decode, **changed)).failure_reason == reason
    port.cancel(item.path_id)
    snapshot = port.kv_snapshot()
    assert snapshot["active_state_count"] == 0
    assert snapshot["release_counts"]["cancelled"] == 1
    assert port.execute(decode).failure_reason == "path_cancelled"


def test_numpy_kv_lease_expiry_and_close_release_arrays(tmp_path: Path) -> None:
    class Clock:
        now = 10.0

        def __call__(self) -> float:
            return self.now

    clock = Clock()
    case = _case(tmp_path, "qwen3", clock=clock)
    port = case.ports[0]
    expiring = _work_item(
        case,
        0,
        encode_token_ids((1, 2, 3)),
        request_id="request:expiry",
        path_id="path:expiry",
        lease_expires_at=12.0,
    )
    assert port.execute(expiring).success
    assert port.kv_snapshot()["active_state_count"] == 1
    clock.now = 12.0
    assert port.expire_leases() == (expiring.path_id,)
    snapshot = port.kv_snapshot()
    assert snapshot["active_state_count"] == 0
    assert snapshot["release_counts"]["lease_expired"] == 1

    clock.now = 20.0
    active = replace(
        expiring,
        request_id="request:close",
        path_id="path:close",
        idempotency_key="request:close:PREFILL:-1:0",
        lease_expires_at=100.0,
    )
    assert port.execute(active).success
    port.close(reason="worker_shutdown")
    snapshot = port.kv_snapshot()
    assert snapshot["closed"] is True
    assert snapshot["active_state_count"] == 0
    assert snapshot["retained_result_count"] == 0
    assert snapshot["release_counts"]["worker_shutdown"] == 1
    assert port.execute(active).failure_reason == "runtime_closed"


def test_route_decode_negotiation_is_architecture_scoped(tmp_path: Path) -> None:
    case = _case(tmp_path, "qwen2")
    mixed = replace(
        case.graph,
        stages=(
            replace(
                case.graph.stages[0],
                placements=(
                    replace(
                        case.graph.stages[0].placements[0], runtime_backend="mlx"
                    ),
                ),
            ),
            case.graph.stages[1],
        ),
    )
    assert _route_decode_mode(mixed, "qwen2") == "stage_local_kv"
    assert _route_decode_mode(mixed, "qwen3") == "stage_local_kv"
    assert _route_decode_mode(mixed, "gpt2") == "complete_context_replay"
    pixel = replace(
        mixed,
        stages=(
            mixed.stages[0],
            replace(
                mixed.stages[1],
                placements=(
                    replace(
                        mixed.stages[1].placements[0],
                        runtime_backend="pixel-stdlib",
                    ),
                ),
            ),
        ),
    )
    assert _route_decode_mode(pixel, "qwen2") == "complete_context_replay"
