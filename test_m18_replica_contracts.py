"""M18 closed plan and immutable request-track runtime contracts."""

from __future__ import annotations

import copy

import pytest

from mycelium_layer_planner.planner import plan_snapshot
from mycelium_m18_replication import (
    PLAN_PROTOCOL,
    RUNTIME_PROTOCOL,
    ReplicaRuntimeLedger,
    build_replica_plan,
    validate_replica_plan,
    validate_replica_runtime,
)


class _Clock:
    value = 10.0

    def now(self) -> float:
        self.value += 1.0
        return self.value


def _route_plan():
    nodes = [
        {
            "node_id": node_id,
            "prefill_ms_per_layer_token": speed,
            "decode_ms_per_layer_token": speed,
            "fast_memory_bytes": 100_000_000,
            "total_memory_bytes": 200_000_000,
            "memory_bandwidth_Bps": 1_000_000_000,
            "spill_bandwidth_Bps": 1_000_000_000,
            "region": region,
        }
        for node_id, speed, region in (
            ("primary-a", 0.05, "site-a"),
            ("primary-b", 0.001, "site-b"),
            ("replica-c", 0.001, "site-c"),
            ("replica-d", 0.001, "site-d"),
        )
    ]
    links = [
        {
            "src": src["node_id"],
            "dst": dst["node_id"],
            "rtt_ms": 0.1,
            "jitter_ms": 0,
            "bandwidth_Bps": 1_000_000_000,
        }
        for src in nodes
        for dst in nodes
        if src != dst
    ]
    return plan_snapshot(
        {
            "model": {
                "model_id": "Qwen/Qwen2.5-0.5B-Instruct",
                "revision": "immutable-revision",
                "weight_digest": "sha256:" + "a" * 64,
                "architecture": "Qwen2ForCausalLM",
                "num_layers": 4,
                "hidden_size": 128,
                "dtype_bytes": 2,
                "kv_heads": 2,
                "head_dim": 32,
                "weight_bytes": 4_000,
            },
            "nodes": nodes,
            "links": links,
            "admitted_node_ids": ["primary-a", "primary-b"],
            "policy": {
                "memory_reserve_fraction": 0,
                "replica_budget": 2,
                "minimum_replica_gain_fraction": 0.05,
                "ttft_slo_ms": 1_000_000,
                "tpot_slo_ms": 1_000_000,
            },
            "workload": {
                "name": "m18-concurrent",
                "mode": "sensitivity_grid",
                "source": "test",
                "scenarios": [
                    {
                        "name": "concurrent",
                        "prompt_tokens": 10,
                        "output_tokens": 10,
                        "concurrency": 16,
                    }
                ],
            },
        }
    )


def _replica_plan():
    plan = _route_plan()
    return build_replica_plan(
        plan,
        deployment_binding={
            "deployment_id": "deployment-m18",
            "deployment_epoch": 18,
            "model_id": plan.model.model_id,
            "model_revision": plan.model.revision,
            "representation_digest": "sha256:" + "b" * 64,
            "manifest_digest": "sha256:" + "c" * 64,
            "qualification_id": "qualification-primary",
            "qualification_digest": "sha256:" + "d" * 64,
            "decode_mode": "stage_local_kv",
            "quantization": "bfloat16",
        },
        evidence_binding={
            "generation": 18,
            "evidence_digest": "sha256:" + "e" * 64,
            "evaluated_at_unix_ms": 1_000,
            "valid_until_unix_ms": 2_000,
        },
        generated_at_unix_ms=1_000,
    )


def test_replica_plan_is_closed_digest_bound_and_request_data_parallel() -> None:
    document = _replica_plan()

    assert document["protocol"] == PLAN_PROTOCOL
    assert document["parallelism"] == "data_parallel_request_routing"
    assert document["route_ready"] is False
    assert len(document["tracks"]) >= 2
    assert all(track["track_id"].startswith("sha256:") for track in document["tracks"])
    assert abs(sum(track["traffic_fraction"] for track in document["tracks"]) - 1) < 1e-9

    unknown = copy.deepcopy(document)
    unknown["tensor_parallel"] = True
    with pytest.raises(ValueError, match="shape"):
        validate_replica_plan(unknown)

    tampered = copy.deepcopy(document)
    tampered["tracks"][0]["placement_ids"].reverse()
    with pytest.raises(ValueError, match="digest"):
        validate_replica_plan(tampered)

    private = copy.deepcopy(document)
    private["candidate_decisions"][0]["prompt"] = "not allowed"
    with pytest.raises(ValueError, match="private"):
        validate_replica_plan(private)


def test_runtime_pins_concurrent_requests_to_distinct_tracks_and_records_work() -> None:
    plan = _replica_plan()
    qualified = {
        track["track_id"]: {
            "qualification_id": f"qualification-{index}",
            "qualification_digest": "sha256:" + f"{index + 1:064x}",
        }
        for index, track in enumerate(plan["tracks"][:2])
    }
    ledger = ReplicaRuntimeLedger(plan, qualified_tracks=qualified, clock=_Clock())

    first = ledger.admit("request-a", path_id="path-a")
    second = ledger.admit("request-b", path_id="path-b")
    assert first != second
    first_track = next(item for item in plan["tracks"] if item["track_id"] == first)
    for placement_id in first_track["placement_ids"]:
        ledger.record_placement_work(
            "request-a", placement_id, frames_sent=2, frames_received=3
        )
    ledger.complete("request-a")

    status = ledger.status()
    assert status["protocol"] == RUNTIME_PROTOCOL
    request = next(item for item in status["requests"] if item["request_id"] == "request-a")
    assert request["track_id"] == first
    assert request["placement_ids"] == first_track["placement_ids"]
    assert all(item["work_items"] == 1 for item in request["placement_work"].values())
    assert validate_replica_runtime(status) == status


def test_replica_removal_terminates_bound_request_without_migration() -> None:
    plan = _replica_plan()
    track = plan["tracks"][0]
    ledger = ReplicaRuntimeLedger(
        plan,
        qualified_tracks={
            track["track_id"]: {
                "qualification_id": "qualification-track",
                "qualification_digest": "sha256:" + "f" * 64,
            }
        },
        clock=_Clock(),
    )
    ledger.admit("request-a", path_id="path-a")
    ledger.remove_track(track["track_id"], reason="replica_unavailable")

    status = ledger.status()
    assert status["requests"][0]["terminal_state"] == "replica_lost_no_migration"
    assert status["incidents"][0]["recovery_claimed"] is False
    with pytest.raises(ValueError, match="unavailable"):
        ledger.admit("request-b", path_id="path-b")
