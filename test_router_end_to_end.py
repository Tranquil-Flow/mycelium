import unittest
from dataclasses import replace

from mycelium_router.contracts import FailureReport, RouterConfig, TokenEvent
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


class RouterEndToEndTests(unittest.TestCase):
   def setUp(self):
      self.graph = graph_fixture()
      self.topology = FakeTopologyProvider(self.graph)
      self.states = FakeDeviceStateProvider(state_table())
      self.capacity = FakeCapacityPort()
      self.runtime = FakeRuntimePort(token_base=100)
      self.transport = FakeTransportPort()
      self.clock = ManualClock()
      self.router = Router(
         node_id="entry-node",
         topology=self.topology,
         device_states=self.states,
         capacity=self.capacity,
         runtime=self.runtime,
         transport=self.transport,
         clock=self.clock,
         id_source=SequenceIdSource(),
         config=RouterConfig(),
      )
      self.sink = InMemoryClientSink()
      self.request = request_fixture()
      self.entry_exclusions = frozenset({"node-b-stage-000"})

   def admit(self, request=None):
      return self.router.admit(
         request or self.request,
         self.sink,
         excluded_placements=self.entry_exclusions,
      )

   def test_request_prefills_progressively_then_decode_replays_locked_path(self):
      request_id = self.admit()
      record = self.router.get_request(request_id)
      original_path = tuple(
         hop.placement_id for hop in record.manifest.ordered_hops
      )
      self.assertEqual(original_path[1], "node-c-stage-001")
      self.assertEqual(len(self.transport.manifest_deltas), 3)

      # Make the other branch more attractive after lock. Decode must not rescore.
      self.states.set(state_table(slow_b_bandwidth=False))
      self.router.generate(request_id, token_count=3)

      record = self.router.get_request(request_id)
      self.assertEqual(
         tuple(hop.placement_id for hop in record.manifest.ordered_hops),
         original_path,
      )
      self.assertEqual(self.sink.token_ids, [101, 102, 103])
      decode_calls = [item for item in self.runtime.executed if item.phase == "DECODE"]
      self.assertEqual(len(decode_calls), 9)
      self.assertTrue(self.transport.hops)

   def test_topology_update_affects_new_request_not_locked_request(self):
      first_id = self.admit()
      first_version = self.router.get_request(first_id).manifest.topology_version
      self.topology.set(replace(self.graph, topology_version=4))
      second = request_fixture(request_id="request-2")
      second_sink = InMemoryClientSink()
      self.router.admit(
         second,
         second_sink,
         excluded_placements=self.entry_exclusions,
      )
      self.assertEqual(first_version, 3)
      self.assertEqual(self.router.get_request("request-2").manifest.topology_version, 4)
      self.router.generate(first_id, token_count=1)
      self.assertEqual(self.router.get_request(first_id).manifest.topology_version, 3)

   def test_non_entry_failure_rebuilds_kv_and_does_not_duplicate_output(self):
      request_id = self.admit()
      initial = self.router.get_request(request_id).manifest
      self.runtime.fail_once(
         placement_id="node-c-stage-001",
         phase="DECODE",
         token_index=1,
         scope="PLACEMENT",
      )

      self.router.generate(request_id, token_count=2)

      recovered = self.router.get_request(request_id)
      self.assertEqual(recovered.manifest.path_attempt, 1)
      self.assertNotEqual(recovered.manifest.path_id, initial.path_id)
      self.assertEqual(recovered.manifest.ordered_hops[1].placement_id, "node-b-stage-001")
      self.assertEqual(self.sink.token_ids, [101, 102])
      self.assertIn(initial.path_id, self.runtime.cancelled_path_ids)
      recovery_prefills = [
         item
         for item in self.runtime.executed
         if item.phase == "RECOVERY_PREFILL"
      ]
      self.assertTrue(recovery_prefills)
      self.assertEqual(recovery_prefills[0].payload, self.request.prompt_token_ids + (101,))
      self.assertTrue(self.transport.failure_reports)

   def test_stale_token_event_from_old_attempt_is_ignored(self):
      request_id = self.admit()
      record = self.router.get_request(request_id)
      accepted = self.router.receive_token_event(
         TokenEvent(
            request_id=request_id,
            path_id=record.manifest.path_id,
            path_attempt=record.manifest.path_attempt - 1,
            token_index=0,
            token_id=999,
            sampling_counter=1,
         )
      )
      self.assertFalse(accepted)
      self.assertEqual(self.sink.token_ids, [])

   def test_entry_device_failure_is_explicitly_unrecoverable(self):
      request_id = self.admit()
      record = self.router.get_request(request_id)
      recovered = self.router.receive_failure_report(
         FailureReport(
            request_id=request_id,
            path_id=record.manifest.path_id,
            path_attempt=record.manifest.path_attempt,
            token_index=0,
            scope="DEVICE",
            reason="entry_lost",
            node_id="entry-node",
         )
      )
      self.assertFalse(recovered)
      self.assertEqual(self.router.get_request(request_id).status, "FAILED")


if __name__ == "__main__":
   unittest.main()
