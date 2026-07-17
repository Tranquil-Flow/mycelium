import unittest

from mycelium_router.contracts import HopWorkItem, RouterConfig
from mycelium_router.fakes import (
   FakeCapacityPort,
   FakeDeviceStateProvider,
   FakeRuntimePort,
   FakeTopologyProvider,
   FakeTransportPort,
   ManualClock,
   SequenceIdSource,
)
from mycelium_router.router import Router
from mycelium_router.scheduler import BackpressureError, HopScheduler
from test_router_policy import state_table
from test_router_receive_hop import first_header, locked_path


def work(request_id: str, payload: bytes) -> HopWorkItem:
   return HopWorkItem(
      request_id=request_id,
      path_id=f"path-{request_id}",
      path_attempt=0,
      phase="DECODE",
      token_index=0,
      hop_index=0,
      placement_id="placement-local",
      qos_class="batch",
      deficit_ratio=0.0,
      enqueued_at=0.0,
      idempotency_key=f"{request_id}:path-{request_id}:0:DECODE:0:0",
      payload=payload,
   )


class SchedulerBackpressureTests(unittest.TestCase):
   def test_work_limit_rejects_without_poisoning_idempotency(self):
      scheduler = HopScheduler(
         RouterConfig(maximum_pending_hops=1, maximum_pending_bytes=100)
      )
      scheduler.enqueue(work("first", b"a"))
      rejected = work("second", b"b")

      with self.assertRaises(BackpressureError) as caught:
         scheduler.enqueue(rejected)

      self.assertEqual(caught.exception.reason, "work_limit")
      self.assertGreater(caught.exception.retry_after_seconds, 0.0)
      self.assertEqual(scheduler.queue_depth(), 1)
      self.assertEqual(scheduler.seen_key_count(), 1)
      scheduler.pop_next(now=0.0)
      scheduler.enqueue(rejected)
      self.assertEqual(scheduler.queue_depth(), 1)

   def test_byte_limit_tracks_enqueued_and_popped_payloads(self):
      scheduler = HopScheduler(
         RouterConfig(maximum_pending_hops=10, maximum_pending_bytes=4)
      )
      scheduler.enqueue(work("first", b"1234"))

      with self.assertRaises(BackpressureError) as caught:
         scheduler.enqueue(work("second", b"5"))

      self.assertEqual(caught.exception.reason, "byte_limit")
      self.assertEqual(scheduler.queued_payload_bytes(), 4)
      scheduler.pop_next(now=0.0)
      self.assertEqual(scheduler.queued_payload_bytes(), 0)


class RouterBackpressureTests(unittest.TestCase):
   def test_receive_hop_returns_explicit_backpressure_without_execution(self):
      graph, request, manifest = locked_path()
      runtime = FakeRuntimePort()
      transport = FakeTransportPort()
      first = manifest.ordered_hops[0]
      placement = next(
         placement
         for stage in graph.stages
         for placement in stage.placements
         if placement.placement_id == first.placement_id
      )
      router = Router(
         node_id=placement.node_id,
         topology=FakeTopologyProvider(graph),
         device_states=FakeDeviceStateProvider(state_table()),
         capacity=FakeCapacityPort(),
         runtime=runtime,
         transport=transport,
         clock=ManualClock(),
         id_source=SequenceIdSource(),
         config=RouterConfig(maximum_pending_hops=1, maximum_pending_bytes=100),
      )
      router.register_path(request, manifest, graph)
      router.relay.scheduler.enqueue(work("already-queued", b"x"))

      result = router.receive_hop(first_header(request, manifest), b"activation")

      self.assertEqual(result.disposition, "REJECTED")
      self.assertEqual(result.reason, "backpressure:work_limit")
      self.assertGreater(result.retry_after_seconds, 0.0)
      self.assertFalse(runtime.executed)
      self.assertFalse(transport.hops)


if __name__ == "__main__":
   unittest.main()
