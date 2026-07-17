import inspect
import unittest

import mycelium_layer_planner
from mycelium_layer_planner.planner import plan_snapshot
from test_layer_planner_v1_planner import snapshot


class HandoffTests(unittest.TestCase):
    def test_planner_has_no_router_dependency(self):
        source = inspect.getsource(mycelium_layer_planner)
        self.assertNotIn("mycelium_router", source)

    def test_handoff_has_model_ranges_tracks_and_no_readiness_claim(self):
        plan = plan_snapshot(snapshot())
        self.assertEqual(plan.handoff_state, "placement_intent_only")
        self.assertTrue(plan.model.revision)
        self.assertTrue(all(p.layer_range.end > p.layer_range.start for p in plan.placements))
        self.assertTrue(plan.legal_tracks)
        serialized = repr(plan).lower()
        for forbidden in ("runtime_ready=true", "weights_loaded=true", "leased=true"):
            self.assertNotIn(forbidden, serialized)


if __name__ == "__main__":
    unittest.main()
