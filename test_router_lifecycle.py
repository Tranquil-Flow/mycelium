import unittest
from dataclasses import replace

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
from mycelium_router.routing import ProgressivePathBuilder, RoutePolicy, RoutingError
from mycelium_router.scoring import RouteScorer
from test_router_contracts import graph_fixture
from test_router_policy import request_fixture, state_table


class RouterLifecycleTests(unittest.TestCase):
   def setUp(self):
      self.capacity = FakeCapacityPort()
      self.runtime = FakeRuntimePort()
      self.clock = ManualClock()
      self.router = Router(
         node_id="entry-node",
         topology=FakeTopologyProvider(graph_fixture()),
         device_states=FakeDeviceStateProvider(state_table()),
         capacity=self.capacity,
         runtime=self.runtime,
         transport=FakeTransportPort(),
         clock=self.clock,
         id_source=SequenceIdSource(),
         config=RouterConfig(),
      )
      self.sink = InMemoryClientSink()

   def admit(self, **request_overrides):
      request = request_fixture(**request_overrides)
      return self.router.admit(request, self.sink)

   def test_completion_releases_reservations_and_runtime_path(self):
      request_id = self.admit(max_new_tokens=1, expected_new_tokens=1)
      manifest = self.router.get_request(request_id).manifest

      self.router.generate(request_id, token_count=1)

      self.assertEqual(self.router.get_request(request_id).status, "COMPLETED")
      reservation_ids = {hop.reservation_id for hop in manifest.ordered_hops}
      self.assertEqual(self.capacity.released_ids, reservation_ids)
      self.assertIn(manifest.path_id, self.runtime.cancelled_path_ids)

   def test_cancel_is_idempotent_and_cleans_up_exactly_once(self):
      request_id = self.admit()
      manifest = self.router.get_request(request_id).manifest

      self.assertTrue(self.router.cancel(request_id))
      self.assertFalse(self.router.cancel(request_id))

      self.assertEqual(self.router.get_request(request_id).status, "CANCELLED")
      self.assertEqual(
         self.capacity.release_calls,
         [tuple(hop.reservation_id for hop in manifest.ordered_hops)],
      )
      self.assertEqual(self.runtime.cancel_calls, [manifest.path_id])

   def test_partial_path_failure_rolls_back_prior_reservations(self):
      capacity = FakeCapacityPort()
      builder = ProgressivePathBuilder(
         policy=RoutePolicy(RouteScorer(RouterConfig())),
         capacity=capacity,
         id_source=SequenceIdSource(),
      )
      build = builder.start(
         request_fixture(),
         graph_fixture(),
         path_attempt=0,
      )
      build = builder.advance(build, state_table(), now=0.0)
      first_reservation = build.ordered_hops[0].reservation_id
      capacity.reject_placements.update(
         {"node-b-stage-001", "node-c-stage-001"}
      )

      with self.assertRaises(RoutingError) as caught:
         builder.advance(build, state_table(), now=0.0)

      self.assertEqual(caught.exception.code, "no_feasible_route")
      self.assertIn(first_reservation, capacity.released_ids)

   def test_failed_recovery_prefill_releases_new_attempt(self):
      request_id = self.admit(max_new_tokens=2, expected_new_tokens=2)
      initial = self.router.get_request(request_id).manifest
      initial_middle = initial.ordered_hops[1].placement_id
      alternate_middle = (
         "node-b-stage-001"
         if initial_middle == "node-c-stage-001"
         else "node-c-stage-001"
      )
      self.runtime.fail_once(
         placement_id=initial_middle,
         phase="DECODE",
         token_index=0,
         scope="PLACEMENT",
      )
      self.runtime.fail_once(
         placement_id=alternate_middle,
         phase="RECOVERY_PREFILL",
         token_index=-1,
         scope="PLACEMENT",
      )

      self.assertFalse(self.router.decode_one(request_id))

      record = self.router.get_request(request_id)
      self.assertEqual(record.status, "FAILED")
      self.assertEqual(self.capacity.released_ids, self.capacity.committed_ids)
      self.assertIn(initial.path_id, self.runtime.cancelled_path_ids)
      self.assertIn(record.manifest.path_id, self.runtime.cancelled_path_ids)


if __name__ == "__main__":
   unittest.main()
