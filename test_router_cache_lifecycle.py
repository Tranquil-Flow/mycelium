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
from mycelium_router.scheduler import DuplicateHopError, HopScheduler
from test_router_contracts import graph_fixture
from test_router_policy import request_fixture, state_table
from test_router_scheduler import work


class SchedulerIdempotencyLifecycleTests(unittest.TestCase):
   def test_duplicate_key_expires_after_retention(self):
      scheduler = HopScheduler(
         RouterConfig(idempotency_retention_seconds=10.0)
      )
      original = work("request", enqueued_at=0.0)
      scheduler.enqueue(original)
      scheduler.pop_next(now=0.0)
      with self.assertRaises(DuplicateHopError):
         scheduler.enqueue(work("request", enqueued_at=9.0))

      scheduler.enqueue(work("request", enqueued_at=11.0))

      self.assertEqual(scheduler.queue_depth(), 1)

   def test_seen_key_storage_is_bounded_after_work_drains(self):
      scheduler = HopScheduler(
         RouterConfig(maximum_idempotency_entries=2)
      )
      for index in range(3):
         scheduler.enqueue(
            work(f"request-{index}", enqueued_at=float(index))
         )
         scheduler.pop_next(now=float(index))

      self.assertLessEqual(scheduler.seen_key_count(), 2)


class RelayCacheLifecycleTests(unittest.TestCase):
   def make_router(self, config):
      self.runtime = FakeRuntimePort()
      self.clock = ManualClock()
      router = Router(
         node_id="entry-node",
         topology=FakeTopologyProvider(graph_fixture()),
         device_states=FakeDeviceStateProvider(state_table()),
         capacity=FakeCapacityPort(),
         runtime=self.runtime,
         transport=FakeTransportPort(),
         clock=self.clock,
         id_source=SequenceIdSource(),
         config=config,
      )
      request = request_fixture(max_new_tokens=4, expected_new_tokens=4)
      router.admit(request, InMemoryClientSink())
      return router, router.get_request(request.request_id)

   def execute_decode(self, router, record, token_index):
      return router.relay.execute_manifest(
         graph=record.graph,
         manifest=record.manifest,
         request=record.request,
         phase="DECODE",
         token_index=token_index,
         payload=record.request.prompt_token_ids,
      )

   def test_cached_outcome_expires_and_can_execute_again(self):
      router, record = self.make_router(
         RouterConfig(idempotency_retention_seconds=10.0)
      )
      self.execute_decode(router, record, 0)
      after_first = len(self.runtime.executed)
      self.execute_decode(router, record, 0)
      self.assertEqual(len(self.runtime.executed), after_first)

      self.clock.advance(11.0)
      self.execute_decode(router, record, 0)

      self.assertEqual(len(self.runtime.executed), after_first + 3)

   def test_outcome_storage_is_bounded(self):
      router, record = self.make_router(
         RouterConfig(maximum_idempotency_entries=2)
      )

      for token_index in range(3):
         self.execute_decode(router, record, token_index)

      self.assertLessEqual(router.relay.cached_outcome_count(), 2)

   def test_request_completion_releases_path_idempotency_state(self):
      router, record = self.make_router(RouterConfig())
      record.request = request_fixture(
         max_new_tokens=1,
         expected_new_tokens=1,
      )

      router.generate(record.request.request_id, token_count=1)

      self.assertEqual(router.relay.cached_outcome_count(), 0)
      self.assertEqual(router.relay.scheduler.seen_key_count(), 0)


if __name__ == "__main__":
   unittest.main()
