from __future__ import annotations

import pytest

from mycelium_layer_planner.compare_plans import compare_snapshots
from mycelium_layer_planner.planner import plan_snapshot


def _snapshot(node_count: int = 3) -> dict:
    nodes = [
        {
            "node_id": f"n{i}",
            "prefill_ms_per_layer_token": 0.001 if i else 0.01,
            "decode_ms_per_layer_token": 0.001 if i else 0.01,
            "fast_memory_bytes": 100_000_000,
            "total_memory_bytes": 200_000_000,
            "memory_bandwidth_Bps": 1_000_000_000,
            "spill_bandwidth_Bps": 1_000_000_000,
        }
        for i in range(node_count)
    ]
    links = [
        {
            "src": f"n{i}",
            "dst": f"n{j}",
            "rtt_ms": 1 + i,
            "jitter_ms": 0.1,
            "bandwidth_Bps": 100_000_000,
        }
        for i in range(node_count)
        for j in range(node_count)
        if i != j
    ]
    return {
        "model": {
            "model_id": "org/model",
            "revision": "immutable-revision",
            "weight_digest": "sha256:" + "a" * 64,
            "architecture": "Decoder",
            "num_layers": 4,
            "hidden_size": 128,
            "dtype_bytes": 2,
            "kv_heads": 2,
            "head_dim": 32,
            "weight_bytes": 240_000_000,
        },
        "nodes": nodes,
        "links": links,
        "workload": {
            "preset": "interactive_chat_v1",
            "concurrency_points": [1, 4],
            "user_scale": 2,
        },
        "policy": {
            "memory_reserve_fraction": 0,
            "replica_budget": 2,
            "ttft_slo_ms": 1_000_000,
            "tpot_slo_ms": 1_000_000,
        },
    }


def test_compare_snapshots_emits_structured_capacity_delta() -> None:
    baseline = _snapshot(node_count=2)
    candidate = _snapshot(node_count=3)
    result = compare_snapshots(baseline, candidate)

    assert result["protocol"] == "mycelium.planner_capacity_comparison.v1"
    assert result["handoff_state"] == "placement_intent_only"
    assert result["route_ready"] is False
    assert result["release_ready"] is False

    assert result["baseline"]["snapshot_digest"] == plan_snapshot(baseline).snapshot_digest
    assert result["candidate"]["snapshot_digest"] == plan_snapshot(candidate).snapshot_digest

    delta = result["capacity_delta"]
    assert "replicated_request_capacity_rps" in delta
    assert delta["replicated_request_capacity_rps"]["candidate"] >= 0
    assert delta["replicated_request_capacity_rps"]["baseline"] >= 0
    assert isinstance(delta["replicated_request_capacity_rps"]["absolute"], (int, float))

    assert "claim_boundary" in result
    assert "no runtime readiness" in result["claim_boundary"].lower()


def test_compare_snapshots_rejects_mismatched_models() -> None:
    baseline = _snapshot(node_count=2)
    candidate = _snapshot(node_count=3)
    candidate["model"]["model_id"] = "different/model"
    with pytest.raises(ValueError, match="model identity mismatch"):
        compare_snapshots(baseline, candidate)


def test_compare_snapshots_delta_is_signed_and_reproducible() -> None:
    baseline = _snapshot(node_count=2)
    candidate = _snapshot(node_count=3)
    first = compare_snapshots(baseline, candidate)
    second = compare_snapshots(baseline, candidate)
    assert first == second
