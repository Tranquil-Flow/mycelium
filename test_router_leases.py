import unittest
from dataclasses import replace

from mycelium_router.contracts import (
   ReservationRequest,
   RouterConfig,
)
from mycelium_router.fakes import (
   FakeCapacityPort,
   ManualClock,
   SequenceIdSource,
)
from mycelium_router.routing import ProgressivePathBuilder, RoutePolicy, RoutingError
from mycelium_router.scoring import RouteScorer
from test_router_contracts import graph_fixture
from test_router_policy import request_fixture, state_table


class ReservationLeaseTests(unittest.TestCase):
   def setUp(self):
      self.clock = ManualClock()
      self.capacity = FakeCapacityPort(clock=self.clock)
      self.builder = ProgressivePathBuilder(
         policy=RoutePolicy(
            RouteScorer(RouterConfig(reservation_lease_seconds=10.0))
         ),
         capacity=self.capacity,
         id_source=SequenceIdSource(),
      )

   def complete_build(self):
      build = self.builder.start(
         request_fixture(),
         graph_fixture(),
         path_attempt=0,
      )
      while not self.builder.is_complete(build):
         build = self.builder.advance(
            build,
            state_table(),
            now=self.clock.now(),
         )
      return build

   def test_reservation_carries_expiry_and_deployment_epoch(self):
      build = self.builder.start(
         request_fixture(),
         graph_fixture(),
         path_attempt=0,
      )

      build = self.builder.advance(build, state_table(), now=0.0)

      request = self.capacity.requests[0]
      self.assertEqual(request.deployment_epoch, build.graph.deployment_epoch)
      self.assertEqual(request.lease_expires_at, 10.0)
      hop = build.ordered_hops[0]
      self.assertEqual(hop.reservation_epoch, build.graph.deployment_epoch)
      self.assertEqual(hop.reservation_expires_at, 10.0)

   def test_expired_reservation_cannot_lock_path(self):
      build = self.complete_build()
      reservation_ids = {
         hop.reservation_id for hop in build.ordered_hops
      }
      self.clock.advance(11.0)

      with self.assertRaises(RoutingError) as caught:
         self.builder.lock(build, now=self.clock.now())

      self.assertEqual(caught.exception.code, "reservation_expired")
      self.assertFalse(self.capacity.committed_ids)
      self.assertEqual(self.capacity.released_ids, reservation_ids)

   def test_commit_is_atomic_when_one_reservation_is_invalid(self):
      first = self.capacity.reserve(
         ReservationRequest(
            request_id="request",
            path_id="path",
            path_attempt=0,
            placement_id="placement-a",
            kv_bytes=100,
            deployment_epoch=7,
            lease_expires_at=10.0,
         )
      )
      second = self.capacity.reserve(
         ReservationRequest(
            request_id="request",
            path_id="path",
            path_attempt=0,
            placement_id="placement-b",
            kv_bytes=100,
            deployment_epoch=7,
            lease_expires_at=2.0,
         )
      )
      self.clock.advance(3.0)

      result = self.capacity.commit(
         (first.reservation_id, second.reservation_id),
         deployment_epoch=7,
      )

      self.assertFalse(result.accepted)
      self.assertEqual(result.reason, "reservation_expired")
      self.assertFalse(self.capacity.committed_ids)

   def test_epoch_change_rejects_whole_commit(self):
      reservation = self.capacity.reserve(
         ReservationRequest(
            request_id="request",
            path_id="path",
            path_attempt=0,
            placement_id="placement-a",
            kv_bytes=100,
            deployment_epoch=7,
            lease_expires_at=10.0,
         )
      )

      result = self.capacity.commit(
         (reservation.reservation_id,),
         deployment_epoch=8,
      )

      self.assertFalse(result.accepted)
      self.assertEqual(result.reason, "deployment_epoch_mismatch")
      self.assertFalse(self.capacity.committed_ids)

   def test_failed_atomic_commit_rolls_back_path(self):
      build = self.complete_build()
      self.capacity.fail_commit_reason = "capacity_changed"

      with self.assertRaises(RoutingError) as caught:
         self.builder.lock(build, now=self.clock.now())

      self.assertEqual(caught.exception.code, "path_commit_rejected")
      self.assertFalse(self.capacity.committed_ids)
      self.assertEqual(
         self.capacity.released_ids,
         {hop.reservation_id for hop in build.ordered_hops},
      )

   def test_stale_accepted_reservation_is_released_before_rescoring(self):
      class OneStaleCapacity(FakeCapacityPort):
         def __init__(self):
            super().__init__()
            self.stale_id = ""

         def reserve(self, request):
            result = super().reserve(request)
            if result.accepted and not self.stale_id:
               self.stale_id = result.reservation_id
               return replace(
                  result,
                  deployment_epoch=result.deployment_epoch + 1,
               )
            return result

      capacity = OneStaleCapacity()
      builder = ProgressivePathBuilder(
         policy=RoutePolicy(RouteScorer(RouterConfig())),
         capacity=capacity,
         id_source=SequenceIdSource(),
      )
      build = builder.start(request_fixture(), graph_fixture(), path_attempt=0)

      build = builder.advance(build, state_table(), now=0.0)

      self.assertTrue(build.ordered_hops)
      self.assertIn(capacity.stale_id, capacity.released_ids)


if __name__ == "__main__":
   unittest.main()
