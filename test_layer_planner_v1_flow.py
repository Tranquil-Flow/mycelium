import unittest

from mycelium_layer_planner.contracts import LayerRange, StagePlacement
from mycelium_layer_planner.flow import FlowEdge, assign_flow


def placement(pid, group, node, capacity=1.0):
    index = int(group[1:])
    return StagePlacement(pid, group, node, LayerRange(index, index + 1), pid.startswith("a"), capacity)


class FlowTests(unittest.TestCase):
    def test_independent_loops_and_cross_loop_edges(self):
        placements = {
            p.placement_id: p for p in (
                placement("a0", "g0", "na0"), placement("a1", "g1", "na1"),
                placement("b0", "g0", "nb0"), placement("b1", "g1", "nb1"),
            )
        }
        groups = (("a0", "b0"), ("a1", "b1"))
        forward = (
            FlowEdge("a0", "a1", 1, 1), FlowEdge("b0", "b1", 1, 1),
            FlowEdge("a0", "b1", 1, 0.5), FlowEdge("b0", "a1", 1, 0.5),
        )
        loopbacks = (
            FlowEdge("a1", "a0", 1, 1), FlowEdge("b1", "b0", 1, 1),
            FlowEdge("a1", "b0", 1, 0.5), FlowEdge("b1", "a0", 1, 0.5),
        )
        result = assign_flow(groups, placements, forward, loopbacks, demand=2)
        self.assertEqual(result.admitted, 2)
        self.assertAlmostEqual(sum(track.traffic_fraction for track in result.tracks), 1)
        self.assertTrue(any(track.placement_ids in {("a0", "b1"), ("b0", "a1")} for track in result.tracks))
        self.assertEqual(len(result.complete_loop_tracks), 2)

    def test_stage_and_edge_capacities_are_respected(self):
        placements = {"p0": placement("p0", "g0", "n0", 2), "p1": placement("p1", "g1", "n1", 2)}
        result = assign_flow(
            (("p0",), ("p1",)), placements,
            (FlowEdge("p0", "p1", 0.75, 1),),
            (FlowEdge("p1", "p0", 1, 1),),
            demand=3,
        )
        self.assertEqual(result.admitted, 0.75)
        self.assertEqual(result.unmet_demand, 2.25)

    def test_disconnected_partial_path_gets_zero(self):
        placements = {"p0": placement("p0", "g0", "n0"), "p1": placement("p1", "g1", "n1")}
        result = assign_flow((("p0",), ("p1",)), placements, (), (), demand=1)
        self.assertEqual(result.admitted, 0)
        self.assertEqual(result.tracks, ())
    def test_fraction_normalization_survives_large_unmet_demand(self):
        groups = (("a0",), ("a1",))
        placements = {
            "a0": placement("a0", "g0", "na0", 0.2248886580751643),
            "a1": placement("a1", "g1", "na1", 0.2248886580751643),
        }
        result = assign_flow(
            groups,
            placements,
            (FlowEdge("a0", "a1", 1.0, 1.0),),
            (FlowEdge("a1", "a0", 1.0, 1.0),),
            demand=350.8771929824561,
        )
        self.assertEqual(result.tracks[0].traffic_fraction, 1.0)
        self.assertAlmostEqual(sum(track.traffic_fraction for track in result.tracks), 1.0)


if __name__ == "__main__":
    unittest.main()
