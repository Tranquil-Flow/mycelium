import unittest

from mycelium_layer_planner.contracts import PlanningPolicy
from mycelium_layer_planner.cycle_search import exact_directed_cycle, held_karp_cycle, search_cycle


def complete_costs(nodes):
    return {(a, b): ((int(a[1:]) + 1) * 11 + (int(b[1:]) + 1) * 7) % 23 + 1 for a in nodes for b in nodes if a != b}


class ScaledCycleTests(unittest.TestCase):
    def test_held_karp_matches_exact(self):
        nodes = tuple(f"n{i}" for i in range(7))
        costs = complete_costs(nodes)
        self.assertEqual(held_karp_cycle(nodes, costs.get).cost, exact_directed_cycle(nodes, costs.get).cost)

    def test_strategy_boundaries_and_honest_optimality(self):
        expected = {
            8: "held_karp",
            13: "multi_start_insertion",
            33: "clustered_refinement",
            129: "hierarchical_refinement",
        }
        for count, mode in expected.items():
            nodes = tuple(f"n{i}" for i in range(count))
            costs = complete_costs(nodes)
            result = search_cycle(nodes, costs.get, PlanningPolicy())
            self.assertEqual(result.mode, mode)
            self.assertFalse(result.globally_exact)
            self.assertEqual(set(result.order), set(nodes))

    def test_deterministic_and_valid(self):
        nodes = tuple(f"n{i}" for i in range(20))
        costs = complete_costs(nodes)
        first = search_cycle(nodes, costs.get, PlanningPolicy())
        second = search_cycle(reversed(nodes), costs.get, PlanningPolicy())
        self.assertEqual(first, second)


if __name__ == "__main__":
    unittest.main()
