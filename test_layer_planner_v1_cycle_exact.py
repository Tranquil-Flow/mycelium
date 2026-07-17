import itertools
import unittest

from mycelium_layer_planner.cycle_search import cycle_cost, exact_directed_cycle, open_cycle


class ExactCycleTests(unittest.TestCase):
    def test_closure_is_counted_and_reverse_can_differ(self):
        costs = {("a", "b"): 1, ("b", "c"): 1, ("c", "a"): 1, ("a", "c"): 9, ("c", "b"): 9, ("b", "a"): 9}
        self.assertEqual(cycle_cost(("a", "b", "c"), costs.get), 3)
        self.assertEqual(cycle_cost(("a", "c", "b"), costs.get), 27)

    def test_exact_matches_bruteforce_and_ties_are_stable(self):
        nodes = ("c", "a", "b", "d")
        costs = {(a, b): (ord(a) * 7 + ord(b) * 3) % 17 + 1 for a in nodes for b in nodes if a != b}
        result = exact_directed_cycle(nodes, costs.get)
        brute = min(
            (cycle_cost(p, costs.get), p)
            for p in itertools.permutations(sorted(nodes))
        )
        self.assertEqual(result.cost, brute[0])
        self.assertEqual(cycle_cost(result.order, costs.get), brute[0])
        self.assertTrue(result.globally_exact)

    def test_missing_edge_rejects_cycle(self):
        with self.assertRaises(ValueError):
            exact_directed_cycle(("a", "b", "c"), {("a", "b"): 1}.get)

    def test_open_cycle_rotates_and_preserves_loopback(self):
        opened = open_cycle(("a", "b", "c"), 1)
        self.assertEqual(opened.order, ("b", "c", "a"))
        self.assertEqual(opened.loopback, ("a", "b"))


if __name__ == "__main__":
    unittest.main()
