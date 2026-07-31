from __future__ import annotations

import copy

import pytest

from mycelium_seed.placement import (
    MemberRecord,
    PlacementError,
    PlannerPlacementSource,
)


def _member(node_id: str, *, eligible: bool = True) -> MemberRecord:
    return MemberRecord(
        node_id=node_id,
        endpoint_id=f"{node_id}-endpoint",
        peer_class="mac_mlx_iroh",
        runtime_capability={
            "runtime_backend": "mlx",
            "transport": "iroh",
            "activation_protocol": "mycelium.router_wire.v1",
        },
        generation=1,
        lease_expires_at=2_300.0,
        activation_eligible=eligible,
    )


def _snapshot() -> dict:
    nodes = [
        {
            "node_id": node_id,
            "prefill_ms_per_layer_token": runtime,
            "decode_ms_per_layer_token": runtime,
            "fast_memory_bytes": 100_000_000,
            "total_memory_bytes": 200_000_000,
            "memory_bandwidth_Bps": 1_000_000_000,
            "spill_bandwidth_Bps": 1_000_000_000,
        }
        for node_id, runtime in (("node-a", 0.002), ("node-b", 0.001))
    ]
    return {
        "admitted_node_ids": ["node-a", "node-b"],
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
            "weight_bytes": 4_000,
        },
        "nodes": nodes,
        "links": [
            {
                "src": src,
                "dst": dst,
                "rtt_ms": 2.0,
                "jitter_ms": 0.1,
                "bandwidth_Bps": 100_000_000,
            }
            for src, dst in (("node-a", "node-b"), ("node-b", "node-a"))
        ],
        "workload": {
            "preset": "interactive_chat_v1",
            "concurrency_points": [1],
        },
        "policy": {
            "memory_reserve_fraction": 0,
            "replica_budget": 0,
            "ttft_slo_ms": 1_000_000,
            "tpot_slo_ms": 1_000_000,
        },
    }


def _snapshot_with_standby() -> dict:
    snapshot = copy.deepcopy(_snapshot())
    snapshot["nodes"].append(
        {
            **snapshot["nodes"][0],
            "node_id": "node-c",
        }
    )
    snapshot["links"].extend(
        {
            "src": src,
            "dst": dst,
            "rtt_ms": 4.0,
            "jitter_ms": 0.5,
            "bandwidth_Bps": 100_000_000,
        }
        for src, dst in (
            ("node-a", "node-c"),
            ("node-c", "node-a"),
            ("node-b", "node-c"),
            ("node-c", "node-b"),
        )
    )
    return snapshot


def test_planner_source_compiles_deterministic_contiguous_assignments() -> None:
    snapshot = _snapshot()
    source = PlannerPlacementSource(snapshot)
    snapshot["nodes"].clear()
    members = [_member("node-b"), _member("node-a")]

    first = source.compile(members)
    second = source.compile(tuple(reversed(members)))

    assert first == second
    assert first.placement_provenance == "planner_v2"
    assert first.placement_id.startswith("planner-")
    assert first.source_digest.startswith("sha256:")
    assert tuple(assignment["node_id"] for assignment in first.assignments) == (
        "node-a",
        "node-b",
    )
    assert first.assignments[0]["start_layer"] == 0
    assert first.assignments[-1]["end_layer_exclusive"] == 4
    assert all(
        left["end_layer_exclusive"] == right["start_layer"]
        for left, right in zip(first.assignments, first.assignments[1:])
    )


def test_planner_source_uses_explicit_admission_and_not_all_evidence_nodes() -> None:
    source = PlannerPlacementSource(_snapshot_with_standby())

    decision = source.compile((_member("node-a"), _member("node-b")))

    assert tuple(item["node_id"] for item in decision.assignments) == (
        "node-a",
        "node-b",
    )


def test_planner_source_requires_explicit_admission() -> None:
    snapshot = _snapshot()
    del snapshot["admitted_node_ids"]

    with pytest.raises(PlacementError) as raised:
        PlannerPlacementSource(snapshot)

    assert raised.value.code == "placement_planner_admission_required"


def test_planner_source_bounds_snapshot_nodes() -> None:
    snapshot = _snapshot()
    snapshot["nodes"] = [
        {**snapshot["nodes"][0], "node_id": f"node-{index:03}"}
        for index in range(257)
    ]

    with pytest.raises(PlacementError) as raised:
        PlannerPlacementSource(snapshot)

    assert raised.value.code == "placement_planner_snapshot_too_large"


@pytest.mark.parametrize(
    ("members", "expected_code"),
    [
        ([_member("node-a")], "placement_planner_member_set_mismatch"),
        (
            [_member("node-a"), _member("node-b", eligible=False)],
            "placement_member_activation_ineligible",
        ),
        (
            [_member("node-a"), _member("node-a")],
            "placement_member_duplicate",
        ),
    ],
)
def test_planner_source_rejects_membership_not_bound_to_snapshot(
    members: list[MemberRecord],
    expected_code: str,
) -> None:
    source = PlannerPlacementSource(copy.deepcopy(_snapshot()))

    with pytest.raises(PlacementError) as raised:
        source.compile(members)

    assert raised.value.code == expected_code
