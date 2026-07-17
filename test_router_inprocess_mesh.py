import unittest
from dataclasses import replace

from mycelium_router.contracts import RouterConfig
from mycelium_router.fakes import (
   FakeCapacityPort,
   FakeDeviceStateProvider,
   FakeRuntimePort,
   FakeTopologyProvider,
   InMemoryClientSink,
   InProcessMesh,
   ManualClock,
   SequenceIdSource,
)
from mycelium_router.router import Router
from test_router_contracts import graph_fixture
from test_router_policy import request_fixture, state_table


def three_device_graph():
   graph = graph_fixture()
   original = graph.stages[-1].placements[0]
   final = replace(
      original,
      placement_id="node-d-stage-002",
      node_id="node-d",
   )
   final_stage = replace(graph.stages[-1], placements=(final,))
   edges = tuple(
      replace(edge, to_placement_id=final.placement_id)
      if edge.to_placement_id == original.placement_id
      else edge
      for edge in graph.edges
   )
   loopbacks = tuple(
      replace(edge, from_placement_id=final.placement_id)
      if edge.from_placement_id == original.placement_id
      else edge
      for edge in graph.loopback_edges
   )
   return replace(
      graph,
      stages=(graph.stages[0], graph.stages[1], final_stage),
      edges=edges,
      loopback_edges=loopbacks,
   )


class InProcessMeshTests(unittest.TestCase):
   def setUp(self):
      self.graph = three_device_graph()
      states = state_table(slow_b_bandwidth=True)
      states["node-d"] = replace(states["node-a"], node_id="node-d")
      self.states = states
      self.capacity = FakeCapacityPort()
      self.clock = ManualClock()
      self.mesh = InProcessMesh()
      self.runtimes = {}
      self.routers = {}
      for node_id in ("node-a", "node-c", "node-d"):
         runtime = FakeRuntimePort()
         router = Router(
            node_id=node_id,
            topology=FakeTopologyProvider(self.graph),
            device_states=FakeDeviceStateProvider(states),
            capacity=self.capacity,
            runtime=runtime,
            transport=self.mesh.transport_for(node_id),
            clock=self.clock,
            id_source=SequenceIdSource(),
            config=RouterConfig(),
         )
         self.mesh.register_router(node_id, router)
         self.runtimes[node_id] = runtime
         self.routers[node_id] = router
      self.entry = self.routers["node-a"]
      self.sink = InMemoryClientSink()
      self.request = replace(
         request_fixture(),
         request_id="request-mesh",
      )

   def test_mesh_runs_three_device_prefill_and_locked_decode_exactly_once(self):
      self.entry.start_distributed_prefill(
         self.request,
         self.sink,
         excluded_placements=frozenset({"node-b-stage-000"}),
      )

      self.assertEqual(self.entry.request_status(self.request.request_id), "DECODING")
      manifest = self.entry.get_request(self.request.request_id).manifest
      placement_nodes = {
         placement.placement_id: placement.node_id
         for stage in self.graph.stages
         for placement in stage.placements
      }
      self.assertEqual(
         [placement_nodes[hop.placement_id] for hop in manifest.ordered_hops],
         ["node-a", "node-c", "node-d"],
      )

      self.assertTrue(self.entry.decode_one_distributed(self.request.request_id))

      self.assertEqual(self.sink.token_indexes, [0])
      self.assertEqual(self.sink.token_ids, [101])
      decode_deliveries = [
         delivery
         for delivery in self.mesh.hop_deliveries
         if delivery.header.phase == "DECODE"
      ]
      self.assertEqual(len(decode_deliveries), 3)
      for node_id, runtime in self.runtimes.items():
         local_decode = [item for item in runtime.executed if item.phase == "DECODE"]
         self.assertEqual(len(local_decode), 1)
         self.assertEqual(
            placement_nodes[local_decode[0].placement_id],
            node_id,
         )


if __name__ == "__main__":
   unittest.main()
