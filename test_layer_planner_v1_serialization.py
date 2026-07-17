import json
import unittest

from mycelium_layer_planner.planner import plan_snapshot
from mycelium_layer_planner.serialization import dumps_route_plan, loads_route_plan
from test_layer_planner_v1_planner import snapshot


class SerializationTests(unittest.TestCase):
    def test_deterministic_json_and_roundtrip(self):
        plan = plan_snapshot(snapshot())
        first = dumps_route_plan(plan)
        second = dumps_route_plan(plan)
        self.assertEqual(first, second)
        self.assertEqual(loads_route_plan(first), plan)
        self.assertEqual(json.loads(first)["protocol"], "mycelium.route_plan.v2")

    def test_json_contains_explicit_edges_and_loopbacks(self):
        data = json.loads(dumps_route_plan(plan_snapshot(snapshot())))
        self.assertIn("forward_edges", data)
        self.assertIn("loopbacks", data)
        self.assertEqual(data["handoff_state"], "placement_intent_only")

    def test_json_records_search_budget_and_primary_diagnostics(self):
        data = json.loads(dumps_route_plan(plan_snapshot(snapshot())))
        self.assertIn("candidate_budget", data["provenance"])
        self.assertIn("budget_exhausted", data["provenance"])
        self.assertNotIn("elapsed_ms", data["provenance"])
        self.assertIn("primary_search", data["diagnostics"])


if __name__ == "__main__":
    unittest.main()
