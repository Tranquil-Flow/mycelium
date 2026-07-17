import unittest

from mycelium_layer_planner.contracts import DirectedLinkObservation, ModelIdentity, NodeCapability, PlanningPolicy, WorkloadScenario
from mycelium_layer_planner.physical_graph import build_physical_graph
from mycelium_layer_planner.primary_plan import plan_primary
from mycelium_layer_planner.replication import replicate_stages
from mycelium_layer_planner.workload import WorkloadProfile


def node(name, speed):
    return NodeCapability(name, speed, speed, 100_000_000, 200_000_000, 1_000_000_000, 1_000_000_000)


class ReplicationTests(unittest.TestCase):
    def setUp(self):
        self.policy = PlanningPolicy(memory_reserve_fraction=0, replica_budget=4, ttft_slo_ms=1_000_000, tpot_slo_ms=1_000_000)
        self.model = ModelIdentity("m", "immutable-revision", "sha256:" + "a" * 64, "Decoder", 4, 128, 2, 2, 32, 4000)
        self.workload = WorkloadScenario("w", 10, 10, 16)
        self.profile = WorkloadProfile("w", (self.workload,), "sensitivity_grid", "test")
        self.nodes = [node("a", 0.05), node("b", 0.001), node("c", 0.001), node("d", 0.001)]
        links = [
            DirectedLinkObservation(x.node_id, y.node_id, 0.1, 0, 1_000_000_000)
            for x in self.nodes for y in self.nodes if x.node_id != y.node_id
        ]
        self.graph = build_physical_graph(self.nodes, links, self.policy)

    def test_replica_preserves_group_range_and_primary_order(self):
        primary = plan_primary(self.graph, self.model, self.profile, self.policy, force_primary_nodes=("a", "b"))
        result = replicate_stages(primary, self.graph, self.model, self.workload, self.policy)
        self.assertGreaterEqual(len(result.accepted_replica_nodes), 1)
        self.assertEqual(result.frozen_primary_order, primary.order)
        by_group = {}
        for placement in result.placements:
            by_group.setdefault(placement.replica_group_id, []).append(placement)
        for group in by_group.values():
            self.assertEqual(len({p.layer_range for p in group}), 1)

    def test_multiple_replica_groups_can_form_independent_loop_with_cross_edges(self):
        primary = plan_primary(self.graph, self.model, self.profile, self.policy, force_primary_nodes=("a", "b"))
        result = replicate_stages(primary, self.graph, self.model, self.workload, self.policy)
        self.assertTrue(result.flow.complete_loop_tracks)
        self.assertTrue(any(not p.primary for p in result.placements))
        placement_by_id = {placement.placement_id: placement for placement in result.placements}
        self.assertTrue(
            any(all(not placement_by_id[pid].primary for pid in track.placement_ids) for track in result.flow.tracks),
            "expected one complete independent replica loop",
        )
        self.assertTrue(
            any(
                any(placement_by_id[pid].primary for pid in track.placement_ids)
                and any(not placement_by_id[pid].primary for pid in track.placement_ids)
                for track in result.flow.tracks
            ),
            "expected one cross-loop hybrid track",
        )
        # Every edge remains between adjacent ordered groups or final->first loopback.
        placement_group = {p.placement_id: int(p.replica_group_id.split("-")[-1]) for p in result.placements}
        for edge in result.forward_edges:
            self.assertEqual(placement_group[edge.dst] - placement_group[edge.src], 1)
        for edge in result.loopback_edges:
            self.assertGreater(placement_group[edge.src], placement_group[edge.dst])

    def test_no_negative_gain_replica_is_accepted(self):
        primary = plan_primary(self.graph, self.model, self.profile, self.policy, force_primary_nodes=("b", "c"))
        result = replicate_stages(primary, self.graph, self.model, self.workload, PlanningPolicy(memory_reserve_fraction=0, replica_budget=0))
        self.assertEqual(result.accepted_replica_nodes, ())


if __name__ == "__main__":
    unittest.main()
