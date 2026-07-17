import json
import random
import unittest
from pathlib import Path

from mycelium_layer_planner.network_cost import transfer_time_ms
from mycelium_layer_planner.contracts import DirectedLinkObservation, PlanningPolicy
from mycelium_layer_planner.planner import plan_snapshot
from mycelium_layer_planner.serialization import dumps_route_plan
from mycelium_layer_planner.validation import validate_route_plan


ROOT = Path(__file__).parent


class PropertyTests(unittest.TestCase):
    def load(self, name):
        return json.loads((ROOT / "scenarios" / name).read_text(encoding="utf-8"))

    def test_seeded_snapshots_are_byte_deterministic_and_valid(self):
        rng = random.Random(7)
        base = self.load("product-v1-tiny-directed.json")
        for _ in range(8):
            data = json.loads(json.dumps(base))
            for link in data["links"]:
                link["rtt_ms"] += rng.random()
            first = plan_snapshot(data)
            second = plan_snapshot(data)
            validate_route_plan(first)
            self.assertEqual(dumps_route_plan(first), dumps_route_plan(second))

    def test_fixed_route_cost_is_monotonic_in_payload(self):
        link = DirectedLinkObservation("a", "b", 10, 1, 1_000_000)
        policy = PlanningPolicy()
        small = transfer_time_ms(link, 100, policy)
        large = transfer_time_ms(link, 1000, policy)
        self.assertGreaterEqual(large.total_ms, small.total_ms)

    def test_replication_never_decreases_accepted_objective(self):
        data = self.load("product-v1-replicated.json")
        without = json.loads(json.dumps(data))
        without["policy"]["replica_budget"] = 0
        baseline = plan_snapshot(without).metrics["replicated_request_capacity_rps"]
        replicated = plan_snapshot(data).metrics["replicated_request_capacity_rps"]
        self.assertGreaterEqual(replicated, baseline)

    def test_layer_ranges_remain_gap_free(self):
        plan = plan_snapshot(self.load("product-v1-replicated.json"))
        primary = sorted((p for p in plan.placements if p.primary), key=lambda p: p.layer_range.start)
        self.assertEqual(primary[0].layer_range.start, 0)
        self.assertEqual(primary[-1].layer_range.end, plan.model.num_layers)
        self.assertTrue(all(a.layer_range.end == b.layer_range.start for a, b in zip(primary, primary[1:])))


if __name__ == "__main__":
    unittest.main()
