import unittest

from mycelium_router.contracts import (
   ExecutionGraph,
   LayerRange,
   PathHop,
   PathManifest,
   Placement,
   PlacementEdge,
   Stage,
   StageCost,
)
from mycelium_router.validation import (
   ContractError,
   validate_execution_graph,
   validate_manifest,
)


def placement(stage_number, node_id, *, suffix=""):
   stage_id = f"stage-{stage_number:03d}"
   return Placement(
      placement_id=f"{node_id}-{stage_id}{suffix}",
      node_id=node_id,
      replica_group_id=f"{stage_id}-replicas",
      assignment_id=f"assignment-{node_id}-{stage_id}{suffix}",
      stage_signature=f"sha256:signature-{stage_id}",
      load_proof_digest=f"sha256:proof-{node_id}-{stage_id}{suffix}",
      runtime_backend="test",
      runtime_endpoint=f"memory://{node_id}/{stage_id}{suffix}",
   )


def stage(stage_number, start, end, placements):
   return Stage(
      stage_id=f"stage-{stage_number:03d}",
      layer_range=LayerRange(start, end, end - start),
      component_roles=("decoder",),
      stage_cost=StageCost(
         prefill_work_units_per_prompt_token=1.0,
         decode_work_units_per_token=1.0,
         kv_bytes_per_context_token=32,
      ),
      placements=tuple(placements),
   )


def graph_fixture():
   p0a = placement(0, "node-a")
   p0b = placement(0, "node-b")
   p1b = placement(1, "node-b")
   p1c = placement(1, "node-c")
   p2a = placement(2, "node-a")
   stages = (
      stage(0, 0, 4, (p0a, p0b)),
      stage(1, 4, 8, (p1b, p1c)),
      stage(2, 8, 12, (p2a,)),
   )
   edges = (
      PlacementEdge("e-0a-1b", p0a.placement_id, p1b.placement_id, "a-b"),
      PlacementEdge("e-0a-1c", p0a.placement_id, p1c.placement_id, "a-c"),
      PlacementEdge("e-0b-1b", p0b.placement_id, p1b.placement_id, "b-b"),
      PlacementEdge("e-1b-2a", p1b.placement_id, p2a.placement_id, "b-a"),
      PlacementEdge("e-1c-2a", p1c.placement_id, p2a.placement_id, "c-a"),
   )
   loopbacks = (
      PlacementEdge("loop-2a-0a", p2a.placement_id, p0a.placement_id, "a-a"),
      PlacementEdge("loop-2a-0b", p2a.placement_id, p0b.placement_id, "a-b"),
   )
   return ExecutionGraph(
      deployment_id="deployment-1",
      deployment_epoch=1,
      topology_version=3,
      model_id="test/model",
      resolved_commit="0123456789abcdef0123456789abcdef01234567",
      manifest_digest="sha256:model-manifest",
      entry_stage_id="stage-000",
      final_stage_id="stage-002",
      hidden_size=4096,
      activation_bytes=2,
      token_envelope_bytes=64,
      stages=stages,
      edges=edges,
      loopback_edges=loopbacks,
   )


class ExecutionGraphValidationTests(unittest.TestCase):
   def test_valid_graph_uses_gap_free_half_open_ranges(self):
      validated = validate_execution_graph(graph_fixture())
      self.assertEqual(validated.stages[-1].layer_range.end_layer_exclusive, 12)

   def test_range_count_mismatch_fails_closed(self):
      graph = graph_fixture()
      bad_stage = Stage(
         stage_id=graph.stages[1].stage_id,
         layer_range=LayerRange(4, 8, 5),
         component_roles=graph.stages[1].component_roles,
         stage_cost=graph.stages[1].stage_cost,
         placements=graph.stages[1].placements,
      )
      bad_graph = graph.with_stages((graph.stages[0], bad_stage, graph.stages[2]))
      with self.assertRaisesRegex(ContractError, "range_count_mismatch"):
         validate_execution_graph(bad_graph)

   def test_layer_gap_fails_closed(self):
      graph = graph_fixture()
      shifted = stage(1, 5, 8, graph.stages[1].placements)
      with self.assertRaisesRegex(ContractError, "layer_gap_or_overlap"):
         validate_execution_graph(
            graph.with_stages((graph.stages[0], shifted, graph.stages[2]))
         )

   def test_backward_or_skip_edge_fails_closed(self):
      graph = graph_fixture()
      extra = PlacementEdge(
         "bad-back-edge",
         graph.stages[2].placements[0].placement_id,
         graph.stages[1].placements[0].placement_id,
         "a-b",
      )
      with self.assertRaisesRegex(ContractError, "non_adjacent_stage_edge"):
         validate_execution_graph(graph.with_edges(graph.edges + (extra,)))

   def test_final_stage_requires_compatible_loopback(self):
      graph = graph_fixture()
      with self.assertRaisesRegex(ContractError, "missing_loopback"):
         validate_execution_graph(graph.with_loopback_edges(()))

   def test_duplicate_placement_identity_fails_closed(self):
      graph = graph_fixture()
      duplicate_stage = Stage(
         stage_id=graph.stages[1].stage_id,
         layer_range=graph.stages[1].layer_range,
         component_roles=graph.stages[1].component_roles,
         stage_cost=graph.stages[1].stage_cost,
         placements=(graph.stages[0].placements[0],),
      )
      with self.assertRaisesRegex(ContractError, "duplicate_placement_id"):
         validate_execution_graph(
            graph.with_stages((graph.stages[0], duplicate_stage, graph.stages[2]))
         )


class ManifestValidationTests(unittest.TestCase):
   def test_valid_manifest_binds_one_placement_per_stage(self):
      graph = graph_fixture()
      hops = (
         PathHop("stage-000", "node-a-stage-000", "reservation-0"),
         PathHop("stage-001", "node-b-stage-001", "reservation-1"),
         PathHop("stage-002", "node-a-stage-002", "reservation-2"),
      )
      manifest = PathManifest(
         path_id="path-1",
         path_attempt=0,
         request_id="request-1",
         deployment_id=graph.deployment_id,
         deployment_epoch=graph.deployment_epoch,
         topology_version=graph.topology_version,
         manifest_digest=graph.manifest_digest,
         ordered_hops=hops,
         loopback_edge_id="loop-2a-0a",
      )
      self.assertIs(validate_manifest(manifest, graph), manifest)

   def test_manifest_rejects_illegal_edge(self):
      graph = graph_fixture()
      manifest = PathManifest(
         path_id="path-1",
         path_attempt=0,
         request_id="request-1",
         deployment_id=graph.deployment_id,
         deployment_epoch=graph.deployment_epoch,
         topology_version=graph.topology_version,
         manifest_digest=graph.manifest_digest,
         ordered_hops=(
            PathHop("stage-000", "node-b-stage-000", "reservation-0"),
            PathHop("stage-001", "node-c-stage-001", "reservation-1"),
            PathHop("stage-002", "node-a-stage-002", "reservation-2"),
         ),
         loopback_edge_id="loop-2a-0b",
      )
      with self.assertRaisesRegex(ContractError, "illegal_manifest_edge"):
         validate_manifest(manifest, graph)

   def test_manifest_rejects_mixed_topology_version(self):
      graph = graph_fixture()
      manifest = PathManifest(
         path_id="path-1",
         path_attempt=0,
         request_id="request-1",
         deployment_id=graph.deployment_id,
         deployment_epoch=graph.deployment_epoch,
         topology_version=graph.topology_version + 1,
         manifest_digest=graph.manifest_digest,
         ordered_hops=(),
         loopback_edge_id="loop-2a-0a",
      )
      with self.assertRaisesRegex(ContractError, "topology_version_mismatch"):
         validate_manifest(manifest, graph)


if __name__ == "__main__":
   unittest.main()
