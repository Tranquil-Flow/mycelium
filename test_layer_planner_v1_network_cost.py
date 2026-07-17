import unittest

from mycelium_layer_planner.contracts import DirectedLinkObservation, PlanningPolicy
from mycelium_layer_planner.network_cost import phase_edge_costs, transfer_time_ms


class NetworkCostTests(unittest.TestCase):
    def setUp(self):
        self.policy = PlanningPolicy(jitter_guard_sigma=2.0, loss_penalty_ms=10.0)

    def test_direction_is_independent(self):
        ab = DirectedLinkObservation("a", "b", 10, 1, 10_000_000)
        ba = DirectedLinkObservation("b", "a", 30, 3, 5_000_000)
        self.assertNotEqual(
            transfer_time_ms(ab, 4096, self.policy).total_ms,
            transfer_time_ms(ba, 4096, self.policy).total_ms,
        )

    def test_geolocation_is_floor_not_addition(self):
        link = DirectedLinkObservation("a", "b", 20, 0, 10_000_000, geolocation_floor_ms=7)
        cost = transfer_time_ms(link, 0, self.policy)
        self.assertEqual(cost.base_one_way_ms, 10)
        self.assertEqual(cost.total_ms, 10)
        link2 = DirectedLinkObservation("a", "b", 4, 0, 10_000_000, geolocation_floor_ms=7)
        self.assertEqual(transfer_time_ms(link2, 0, self.policy).base_one_way_ms, 7)

    def test_payload_can_reverse_edge_ranking(self):
        low_latency_slow = DirectedLinkObservation("a", "b", 2, 0, 1_000_000)
        high_latency_fast = DirectedLinkObservation("a", "c", 20, 0, 100_000_000)
        small = 100
        large = 100_000_000
        self.assertLess(
            transfer_time_ms(low_latency_slow, small, self.policy).total_ms,
            transfer_time_ms(high_latency_fast, small, self.policy).total_ms,
        )
        self.assertGreater(
            transfer_time_ms(low_latency_slow, large, self.policy).total_ms,
            transfer_time_ms(high_latency_fast, large, self.policy).total_ms,
        )

    def test_phase_costs_preserve_different_payloads(self):
        link = DirectedLinkObservation("a", "b", 10, 0, 10_000_000)
        costs = phase_edge_costs(link, {"prefill": 1_000_000, "decode": 10_000}, self.policy)
        self.assertGreater(costs["prefill"].serialization_ms, costs["decode"].serialization_ms)

    def test_fallback_and_confidence_are_explicit(self):
        link = DirectedLinkObservation("a", "b", 10, 0, None, stale=True, inferred=True)
        cost = transfer_time_ms(link, 1000, self.policy)
        self.assertIn("fallback_bandwidth", cost.diagnostics)
        self.assertLess(cost.confidence, 1)
        with self.assertRaises(ValueError):
            transfer_time_ms(link, 1000, PlanningPolicy(exclude_missing_bandwidth=True))


if __name__ == "__main__":
    unittest.main()
