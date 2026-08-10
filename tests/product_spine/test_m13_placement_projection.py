from __future__ import annotations

import json

from mycelium_layer_planner.gossip_adapter import planner_snapshot_digest
from mycelium_layer_planner.public_projection import build_m13_placement_projection


def _snapshot() -> dict:
    return {
        "protocol": "mycelium.layer_planner_snapshot.v1",
        "swarm_id": "swarm-a",
        "deployment": {"deployment_id": "candidate-a", "deployment_epoch": 4},
        "snapshot_generation": 9,
        "evidence_bundle_digest": "sha256:" + "a" * 64,
        "model": {"model_id": "org/model"},
        "nodes": [
            {
                "node_id": node_id,
                "prefill_ms_per_layer_token": 0.1,
                "decode_ms_per_layer_token": 0.2,
                "fast_memory_bytes": 100_000_000,
                "total_memory_bytes": 200_000_000,
            }
            for node_id in ("node-a", "node-b")
        ],
        "links": [
            {"src": "node-a", "dst": "node-b", "rtt_ms": 4.0, "jitter_ms": 0.5, "bandwidth_Bps": 20_000_000.0},
            {"src": "node-b", "dst": "node-a", "rtt_ms": 5.0, "jitter_ms": 0.7, "bandwidth_Bps": 18_000_000.0},
        ],
        "workload": {},
        "policy": {},
        "placement_provenance": "planner_v2",
        "decode_mode": "stage_local_kv",
        "quantization": "int8-weight-only",
        "node_runtime": {
            node_id: {"backend": "mlx", "decode_mode": "stage_local_kv"}
            for node_id in ("node-a", "node-b")
        },
        "capacity_profile_bindings": {
            node_id: {
                "profile_digest": "sha256:" + digit * 64,
                "source_evidence_digest": "sha256:" + "c" * 64,
            }
            for node_id, digit in (("node-a", "1"), ("node-b", "2"))
        },
        "evidence_authority": {
            "authority_generation": 2,
            "verification_key_digest": "sha256:" + "d" * 64,
            "valid_until_unix_ms": 10_000,
        },
    }


def test_projection_explains_same_snapshot_across_product_views_without_private_data() -> None:
    snapshot = _snapshot()
    digest = planner_snapshot_digest(snapshot)
    route = {
        "snapshot_digest": digest,
        "placements": [
            {"node_id": "node-a", "primary": True, "layer_range": {"start": 0, "end": 2}},
            {"node_id": "node-b", "primary": True, "layer_range": {"start": 2, "end": 4}},
        ],
    }
    assignments = [
        {"node_id": node_id, "assignment_id": f"assignment-{node_id}", "range": layer_range}
        for node_id, layer_range in (
            ("node-a", {"start_layer": 0, "end_layer_exclusive": 2}),
            ("node-b", {"start_layer": 2, "end_layer_exclusive": 4}),
        )
    ]
    materializations = {
        node_id: {"assignment_id": f"assignment-{node_id}", "objects": [{"object_id": index}]}
        for index, node_id in enumerate(("node-a", "node-b"))
    }
    projection = build_m13_placement_projection(
        planner_snapshot=snapshot,
        route_plan=route,
        assignments=assignments,
        materializations_by_node=materializations,
        load_proof_digests_by_node={
            "node-a": "sha256:" + "e" * 64,
            "node-b": "sha256:" + "f" * 64,
        },
        promotion_report=None,
        ab_deltas=[
            {
                "kind": "compute_only",
                "node_id": "node-a",
                "before": "0.1",
                "after": "0.3",
                "allocation_before": "2",
                "allocation_after": "1",
            }
        ],
    )

    assert projection["snapshot_digest"] == digest
    assert [node["start_layer"] for node in projection["nodes"]] == [0, 2]
    assert all(node["ready"] for node in projection["nodes"])
    wire = json.dumps(projection)
    assert "prompt" not in wire
    assert "token_ids" not in wire
    assert "/Users/" not in wire
