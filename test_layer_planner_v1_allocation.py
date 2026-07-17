import unittest

from mycelium_layer_planner.allocation import allocate_layers, stage_cost
from mycelium_layer_planner.contracts import ModelIdentity, NodeCapability, PlanningPolicy, WorkloadScenario


def model(layers=8, weight_bytes=8000):
    return ModelIdentity("m", "revision-immutable", "sha256:" + "a" * 64, "Decoder", layers, 64, 2, 2, 16, weight_bytes)


def node(name, speed=1.0, fast=1_000_000, total=2_000_000, spill=1_000_000):
    return NodeCapability(name, speed, speed, fast, total, 1_000_000, spill)


class AllocationTests(unittest.TestCase):
    def setUp(self):
        self.workload = WorkloadScenario("w", 10, 4, 2)
        self.policy = PlanningPolicy(memory_reserve_fraction=0)

    def test_ranges_are_contiguous_positive_and_complete(self):
        result = allocate_layers((node("a"), node("b"), node("c")), model(8), self.workload, self.policy)
        self.assertTrue(result.feasible)
        self.assertEqual(result.stages[0].layer_range.start, 0)
        self.assertEqual(result.stages[-1].layer_range.end, 8)
        self.assertTrue(all(s.layer_range.count > 0 for s in result.stages))
        self.assertTrue(all(a.layer_range.end == b.layer_range.start for a, b in zip(result.stages, result.stages[1:])))

    def test_faster_node_receives_more_layers(self):
        result = allocate_layers((node("slow", 2), node("fast", 0.5)), model(8), self.workload, self.policy)
        counts = {s.node_id: s.layer_range.count for s in result.stages}
        self.assertGreater(counts["fast"], counts["slow"])

    def test_memory_and_spill_affect_cost(self):
        m = model(4, weight_bytes=4000)
        roomy = stage_cost(node("roomy", fast=100_000), 4, m, self.workload, self.policy)
        spilling = stage_cost(node("spill", fast=1000, total=100_000, spill=1000), 4, m, self.workload, self.policy)
        self.assertGreater(spilling.spill_bytes, 0)
        self.assertGreater(spilling.service_work_ms, roomy.service_work_ms)

    def test_impossible_allocation_has_diagnostic(self):
        tiny = node("tiny", fast=100, total=100)
        result = allocate_layers((tiny,), model(2, 10_000), self.workload, self.policy)
        self.assertFalse(result.feasible)
        self.assertIn("no feasible", result.diagnostics[0])

    def test_stage_count_cannot_exceed_layer_count(self):
        result = allocate_layers(tuple(node(str(i)) for i in range(3)), model(2), self.workload, self.policy)
        self.assertFalse(result.feasible)


if __name__ == "__main__":
    unittest.main()
