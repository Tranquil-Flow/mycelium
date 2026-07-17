import unittest

from mycelium_layer_planner.planner import plan_snapshot


def snapshot(node_count=3):
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
        {"src": f"n{i}", "dst": f"n{j}", "rtt_ms": 1 + i, "jitter_ms": 0.1, "bandwidth_Bps": 100_000_000}
        for i in range(node_count) for j in range(node_count) if i != j
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
        "workload": {"preset": "interactive_chat_v1", "concurrency_points": [1, 4], "user_scale": 2},
        "policy": {"memory_reserve_fraction": 0, "replica_budget": 2, "ttft_slo_ms": 1_000_000, "tpot_slo_ms": 1_000_000},
    }


class PlannerTests(unittest.TestCase):
    def test_end_to_end_emits_multi_loop_placement_intent(self):
        plan = plan_snapshot(snapshot())
        self.assertEqual(plan.protocol, "mycelium.route_plan.v2")
        self.assertEqual(plan.handoff_state, "placement_intent_only")
        self.assertTrue(plan.placements)
        self.assertTrue(plan.legal_tracks)
        self.assertEqual(plan.diagnostics["topology"], "ordered_stage_groups_with_independent_loops_and_cross_edges")
        self.assertEqual(plan.diagnostics["frozen_primary_order"], plan.diagnostics["primary_order"])
        self.assertEqual(plan.diagnostics["workload"]["user_scale"], 2)

    def test_unknown_policy_and_max_nodes_are_rejected(self):
        data = snapshot()
        data["policy"]["max_nodes"] = 2
        with self.assertRaises(ValueError):
            plan_snapshot(data)

    def test_same_snapshot_is_deterministic(self):
        self.assertEqual(plan_snapshot(snapshot()), plan_snapshot(snapshot()))


if __name__ == "__main__":
    unittest.main()
