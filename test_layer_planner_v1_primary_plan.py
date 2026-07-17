import unittest

from mycelium_layer_planner.contracts import DirectedLinkObservation, ModelIdentity, NodeCapability, PlanningPolicy
from mycelium_layer_planner.physical_graph import build_physical_graph
from mycelium_layer_planner.primary_plan import plan_primary
from mycelium_layer_planner.workload import empirical_interactive_chat


def model(layers=8):
    return ModelIdentity("m", "immutable-revision", "sha256:" + "a" * 64, "Decoder", layers, 128, 2, 2, 32, layers * 1000)


def node(name, speed=0.0001):
    return NodeCapability(name, speed, speed, 100_000_000, 200_000_000, 1_000_000_000, 1_000_000_000)


def graph(nodes, policy):
    links = [
        DirectedLinkObservation(a.node_id, b.node_id, 2, 0, 100_000_000)
        for a in nodes for b in nodes if a.node_id != b.node_id
    ]
    return build_physical_graph(nodes, links, policy)


class PrimaryPlanTests(unittest.TestCase):
    def test_tiny_joint_search_can_drop_slow_node(self):
        policy = PlanningPolicy(memory_reserve_fraction=0)
        nodes = [node("fast-a"), node("slow", 10), node("fast-b")]
        result = plan_primary(graph(nodes, policy), model(6), empirical_interactive_chat(concurrency_points=(1, 4)), policy)
        self.assertNotIn("slow", result.order)
        self.assertIn("slow", result.unplaced_node_ids)
        self.assertTrue(result.provenance.globally_exact)

    def test_fleet_strategy_uses_candidate_count_without_candidate_truncation(self):
        expected = {8: "held_karp", 13: "multi_start_insertion", 33: "clustered_refinement", 129: "hierarchical_refinement"}
        for count, mode in expected.items():
            policy = PlanningPolicy(memory_reserve_fraction=0)
            nodes = [node(f"n{i:03}") for i in range(count)]
            result = plan_primary(graph(nodes, policy), model(4), empirical_interactive_chat(concurrency_points=(1,)), policy)
            self.assertEqual(result.provenance.mode, mode)
            self.assertEqual(result.provenance.candidate_node_count, count)
            self.assertLessEqual(len(result.order), 4)
            self.assertFalse(result.provenance.globally_exact)

    def test_primary_order_is_frozen_contract(self):
        policy = PlanningPolicy(memory_reserve_fraction=0)
        nodes = [node("a"), node("b"), node("c")]
        result = plan_primary(graph(nodes, policy), model(6), empirical_interactive_chat(concurrency_points=(1,)), policy)
        self.assertEqual(result.frozen_primary_order, result.order)

    def test_candidate_budget_exhaustion_is_explicit_and_not_globally_exact(self):
        policy = PlanningPolicy(memory_reserve_fraction=0, search_candidate_budget=1)
        nodes = [node("a"), node("b"), node("c")]
        result = plan_primary(graph(nodes, policy), model(6), empirical_interactive_chat(concurrency_points=(1,)), policy)
        self.assertEqual(result.provenance.explored_candidates, 1)
        self.assertEqual(result.provenance.candidate_budget, 1)
        self.assertTrue(result.provenance.budget_exhausted)
        self.assertFalse(result.provenance.globally_exact)

    def test_rejected_candidate_orders_have_deterministic_diagnostics(self):
        policy = PlanningPolicy(memory_reserve_fraction=0)
        nodes = [node("a"), node("b")]
        one_way = [DirectedLinkObservation("a", "b", 2, 0, 100_000_000)]
        partial_graph = build_physical_graph(nodes, one_way, policy)
        result = plan_primary(
            partial_graph,
            model(6),
            empirical_interactive_chat(concurrency_points=(1,)),
            policy,
        )
        self.assertTrue(any("disconnected directed cycle" in item for item in result.diagnostics))


if __name__ == "__main__":
    unittest.main()
