import unittest

from mycelium_layer_planner.contracts import DirectedLinkObservation, NodeCapability, PlanningPolicy
from mycelium_layer_planner.physical_graph import build_physical_graph


def node(node_id, eligible=True):
    return NodeCapability(node_id, 0.01, 0.02, 1_000_000, 2_000_000, 1_000_000, 500_000, eligible=eligible, exclusion_reason=None if eligible else "offline")


class PhysicalGraphTests(unittest.TestCase):
    def test_all_130_candidates_remain(self):
        graph = build_physical_graph([node(f"n{i:03}") for i in range(130)], [], PlanningPolicy())
        self.assertEqual(len(graph.nodes), 130)
        self.assertEqual(graph.candidate_node_ids[0], "n000")
        self.assertEqual(graph.candidate_node_ids[-1], "n129")

    def test_excluded_nodes_have_reasons(self):
        graph = build_physical_graph([node("a"), node("b", False)], [], PlanningPolicy())
        self.assertEqual(graph.candidate_node_ids, ("a",))
        self.assertEqual(graph.exclusions["b"], "offline")

    def test_asymmetric_and_missing_reverse_edges_are_not_invented(self):
        links = [DirectedLinkObservation("a", "b", 10, 1, 10_000)]
        graph = build_physical_graph([node("a"), node("b")], links, PlanningPolicy())
        self.assertIsNotNone(graph.link("a", "b"))
        self.assertIsNone(graph.link("b", "a"))

    def test_deterministic_edge_order_and_stale_label(self):
        links = [
            DirectedLinkObservation("b", "a", 10, 1, 10_000),
            DirectedLinkObservation("a", "b", 10, 1, 10_000, stale=True),
        ]
        graph = build_physical_graph([node("b"), node("a")], links, PlanningPolicy())
        self.assertEqual(tuple(graph.edges), (("a", "b"), ("b", "a")))
        self.assertTrue(graph.edges[("a", "b")].stale)


if __name__ == "__main__":
    unittest.main()
