import unittest

from mycelium_router.contracts import RouterConfig
from mycelium_router.fakes import (
   FakeCapacityPort,
   FakeDeviceStateProvider,
   FakeRuntimePort,
   FakeTopologyProvider,
   FakeTransportPort,
   InMemoryClientSink,
   ManualClock,
   SequenceIdSource,
)
from mycelium_router.router import Router
from test_router_contracts import graph_fixture
from test_router_policy import request_fixture, state_table


class DistributedProgressivePrefillTests(unittest.TestCase):
   def setUp(self):
      self.graph = graph_fixture()
      self.request = request_fixture()
      self.capacity = FakeCapacityPort()
      self.clock = ManualClock()
      self.runtimes = {}
      self.transports = {}
      self.routers = {}

      node_states = {
         "node-a": state_table(slow_b_bandwidth=True),
         "node-b": state_table(slow_b_bandwidth=False),
         "node-c": state_table(slow_b_bandwidth=False),
      }
      for node_id in ("node-a", "node-b", "node-c"):
         runtime = FakeRuntimePort()
         transport = FakeTransportPort()
         self.runtimes[node_id] = runtime
         self.transports[node_id] = transport
         self.routers[node_id] = self.make_router(
            node_id=node_id,
            states=node_states[node_id],
            runtime=runtime,
            transport=transport,
         )

      self.entry_runtime = FakeRuntimePort()
      self.entry_transport = FakeTransportPort()
      self.entry = self.make_router(
         node_id="entry-node",
         states=state_table(slow_b_bandwidth=False),
         runtime=self.entry_runtime,
         transport=self.entry_transport,
      )
      self.placement_nodes = {
         placement.placement_id: placement.node_id
         for stage in self.graph.stages
         for placement in stage.placements
      }

   def make_router(self, *, node_id, states, runtime, transport):
      return Router(
         node_id=node_id,
         topology=FakeTopologyProvider(self.graph),
         device_states=FakeDeviceStateProvider(states),
         capacity=self.capacity,
         runtime=runtime,
         transport=transport,
         clock=self.clock,
         id_source=SequenceIdSource(),
         config=RouterConfig(),
      )

   def test_prefill_branches_on_current_router_and_locks_at_final_stage(self):
      sink = InMemoryClientSink()
      request_id = self.entry.start_distributed_prefill(
         self.request,
         sink,
         excluded_placements=frozenset({"node-b-stage-000"}),
      )

      self.assertEqual(request_id, self.request.request_id)
      self.assertEqual(self.entry.request_status(request_id), "PREFILL")
      self.assertFalse(self.entry.decode_one(request_id))
      self.assertFalse(self.entry_runtime.executed)
      self.assertEqual(len(self.entry_transport.manifest_deltas), 1)
      header, context = self.entry_transport.hops[0]
      self.assertEqual(len(context.build.ordered_hops), 1)
      self.assertIsInstance(context.payload, bytes)
      self.assertTrue(context.payload)
      self.assertFalse(self.capacity.committed_ids)

      first_node = self.placement_nodes[header.destination_placement_id]
      first_result = self.routers[first_node].receive_progressive_prefill(
         header,
         context,
      )
      self.assertEqual(first_result.disposition, "FORWARDED")
      self.assertEqual(len(first_result.context.build.ordered_hops), 2)
      self.assertEqual(
         first_result.context.build.ordered_hops[1].placement_id,
         "node-c-stage-001",
      )
      self.assertEqual(len(self.transports[first_node].manifest_deltas), 1)
      self.assertFalse(self.capacity.committed_ids)

      header, context = self.transports[first_node].hops[-1]
      second_node = self.placement_nodes[header.destination_placement_id]
      second_result = self.routers[second_node].receive_progressive_prefill(
         header,
         context,
      )
      self.assertEqual(second_result.disposition, "FORWARDED")
      self.assertEqual(len(second_result.context.build.ordered_hops), 3)
      self.assertEqual(len(self.transports[second_node].manifest_deltas), 1)
      self.assertFalse(self.capacity.committed_ids)

      header, context = self.transports[second_node].hops[-1]
      final_node = self.placement_nodes[header.destination_placement_id]
      final_result = self.routers[final_node].receive_progressive_prefill(
         header,
         context,
      )

      self.assertEqual(final_result.disposition, "LOCKED")
      self.assertIsNotNone(final_result.confirmation)
      self.assertEqual(len(self.transports[final_node].manifest_locks), 1)
      self.assertEqual(
         self.capacity.committed_ids,
         {
            hop.reservation_id
            for hop in final_result.confirmation.manifest.ordered_hops
         },
      )

      self.assertTrue(
         self.entry.receive_manifest_locked(final_result.confirmation)
      )
      self.assertEqual(self.entry.request_status(request_id), "DECODING")
      self.assertEqual(
         self.entry.get_request(request_id).manifest,
         final_result.confirmation.manifest,
      )

      executed = [
         item
         for runtime in self.runtimes.values()
         for item in runtime.executed
      ]
      self.assertEqual(len(executed), 3)
      for node_id, runtime in self.runtimes.items():
         self.assertTrue(
            all(
               self.placement_nodes[item.placement_id] == node_id
               for item in runtime.executed
            )
         )

   def test_duplicate_progressive_hop_does_not_reexecute_or_extend_twice(self):
      self.entry.start_distributed_prefill(
         self.request,
         InMemoryClientSink(),
         excluded_placements=frozenset({"node-b-stage-000"}),
      )
      header, context = self.entry_transport.hops[0]
      node_id = self.placement_nodes[header.destination_placement_id]
      router = self.routers[node_id]

      first = router.receive_progressive_prefill(header, context)
      executed = len(self.runtimes[node_id].executed)
      forwarded = len(self.transports[node_id].hops)
      deltas = len(self.transports[node_id].manifest_deltas)
      second = router.receive_progressive_prefill(header, context)

      self.assertEqual(second, first)
      self.assertEqual(len(self.runtimes[node_id].executed), executed)
      self.assertEqual(len(self.transports[node_id].hops), forwarded)
      self.assertEqual(len(self.transports[node_id].manifest_deltas), deltas)

   def test_cancel_during_distributed_prefill_releases_partial_path(self):
      self.entry.start_distributed_prefill(
         self.request,
         InMemoryClientSink(),
         excluded_placements=frozenset({"node-b-stage-000"}),
      )
      _, context = self.entry_transport.hops[0]
      reservation_ids = {
         hop.reservation_id for hop in context.build.ordered_hops
      }

      self.assertTrue(self.entry.cancel(self.request.request_id))

      self.assertTrue(reservation_ids <= self.capacity.released_ids)
      self.assertEqual(
         self.entry.request_status(self.request.request_id),
         "CANCELLED",
      )
      self.assertFalse(self.entry.decode_one(self.request.request_id))


if __name__ == "__main__":
   unittest.main()
