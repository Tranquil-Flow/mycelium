import unittest

from mycelium_layer_planner.allocation import allocate_layers
from mycelium_layer_planner.contracts import DirectedLinkObservation, ModelIdentity, NodeCapability, PlanningPolicy, WorkloadScenario
from mycelium_layer_planner.phase_score import score_phases
from mycelium_layer_planner.physical_graph import build_physical_graph


def node(name):
    return NodeCapability(name, 0.001, 0.01, 10_000_000, 20_000_000, 1_000_000_000, 1_000_000_000)


class PhaseScoreTests(unittest.TestCase):
    def setUp(self):
        self.model = ModelIdentity("m", "immutable-revision", "sha256:" + "a" * 64, "Decoder", 4, 128, 2, 2, 32, 4000)
        self.workload = WorkloadScenario("w", 100, 20, 4)
        self.policy = PlanningPolicy(memory_reserve_fraction=0)
        nodes = [node("a"), node("b")]
        links = [
            DirectedLinkObservation("a", "b", 10, 0, 10_000_000),
            DirectedLinkObservation("b", "a", 30, 0, 10_000_000),
        ]
        self.graph = build_physical_graph(nodes, links, self.policy)
        self.allocation = allocate_layers(tuple(nodes), self.model, self.workload, self.policy)

    def test_prefill_excludes_loopback_decode_includes_it(self):
        score = score_phases(self.allocation, self.graph, self.model, self.workload, self.policy)
        self.assertEqual(score.prefill_loopback_ms, 0)
        self.assertGreater(score.decode_loopback_ms, 0)
        self.assertGreater(score.prefill_payload_bytes, score.decode_payload_bytes)

    def test_serial_latency_and_bottleneck_are_separate(self):
        score = score_phases(self.allocation, self.graph, self.model, self.workload, self.policy)
        self.assertGreater(score.tpot_ms, score.decode_bottleneck_ms)
        self.assertGreater(score.single_request_tps, 0)
        self.assertGreaterEqual(score.output_goodput_tps, score.single_request_tps)

    def test_missing_loopback_is_infeasible(self):
        graph = build_physical_graph(
            [node("a"), node("b")],
            [DirectedLinkObservation("a", "b", 10, 0, 10_000_000)],
            self.policy,
        )
        with self.assertRaises(ValueError):
            score_phases(self.allocation, graph, self.model, self.workload, self.policy)


if __name__ == "__main__":
    unittest.main()
