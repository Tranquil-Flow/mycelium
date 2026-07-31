import unittest
from dataclasses import replace

from mycelium_layer_planner.contracts import LayerRange
from mycelium_layer_planner.planner import plan_snapshot
from mycelium_layer_planner.validation import validate_route_plan
from test_layer_planner_v1_planner import snapshot


class ValidationTests(unittest.TestCase):
    def test_valid_plan_passes(self):
        validate_route_plan(plan_snapshot(snapshot()))

    def test_layer_gap_is_rejected(self):
        plan = plan_snapshot(snapshot())
        first = plan.placements[0]
        broken = replace(first, layer_range=LayerRange(first.layer_range.start + 1, first.layer_range.end + 1))
        with self.assertRaises(ValueError):
            validate_route_plan(replace(plan, placements=(broken,) + plan.placements[1:]))

    def test_incomplete_track_is_rejected(self):
        plan = plan_snapshot(snapshot())
        track = plan.legal_tracks[0]
        with self.assertRaises(ValueError):
            broken = replace(track, placement_ids=track.placement_ids[:-1])
            validate_route_plan(replace(plan, legal_tracks=(broken,) + plan.legal_tracks[1:]))

    def test_traffic_fractions_must_sum_one(self):
        plan = plan_snapshot(snapshot())
        tracks = tuple(replace(track, traffic_fraction=0.1) for track in plan.legal_tracks)
        with self.assertRaises(ValueError):
            validate_route_plan(replace(plan, legal_tracks=tracks))

    def test_runtime_ready_claim_is_rejected(self):
        plan = plan_snapshot(snapshot())
        with self.assertRaises(ValueError):
            validate_route_plan(replace(plan, diagnostics={**plan.diagnostics, "loaded": True}))

    def test_exact_held_karp_topology_provenance_is_accepted(self):
        plan = plan_snapshot(snapshot())
        provenance = replace(
            plan.provenance,
            mode="held_karp",
            globally_exact=True,
        )

        validate_route_plan(replace(plan, provenance=provenance))

    def test_unknown_global_topology_provenance_is_rejected(self):
        plan = plan_snapshot(snapshot())
        provenance = replace(
            plan.provenance,
            mode="unknown_exact_search",
            globally_exact=True,
        )

        with self.assertRaisesRegex(ValueError, "unknown global optimality claim"):
            validate_route_plan(replace(plan, provenance=provenance))


if __name__ == "__main__":
    unittest.main()
