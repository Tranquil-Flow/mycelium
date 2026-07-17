import unittest
from dataclasses import replace

from mycelium_router.batching import PhaseAwareBatchController
from mycelium_router.contracts import (
   BatchExecutionObservation,
   BatchNetworkStats,
   RouterConfig,
)
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
from test_router_batching import batch_key, work
from test_router_policy import state_table
from test_router_receive_hop import first_header, locked_path


class WrongCountRuntime(FakeRuntimePort):
   def execute_batch(self, batch):
      self.executed_batches.append(batch)
      return ()


class PhaseAwareBatchControllerTests(unittest.TestCase):
   def test_loss_reduces_adaptive_prefill_target(self):
      config = RouterConfig(
         maximum_runtime_batch_size=20,
         prefill_runtime_batch_size=20,
         maximum_runtime_batch_bytes=2_400_000,
      )
      items = tuple(
         work(
            f"request-{index:02d}",
            key=batch_key(phase="PREFILL_CHUNK", token_span=16),
            payload=b"x" * 12_000,
         )
         for index in range(20)
      )
      clean = PhaseAwareBatchController(config)
      clean.update_network_stats(
         "placement-a",
         BatchNetworkStats(
            one_way_p95_ms=10.0,
            goodput_bytes_per_second=12_000_000.0,
            loss_rate=0.0,
            receiver_queue_ms=0.0,
            observed_at=0.0,
         ),
      )
      lossy = PhaseAwareBatchController(config)
      lossy.update_network_stats(
         "placement-a",
         BatchNetworkStats(
            one_way_p95_ms=10.0,
            goodput_bytes_per_second=12_000_000.0,
            loss_rate=0.08,
            receiver_queue_ms=0.0,
            observed_at=0.0,
         ),
      )

      clean_decision = clean.decide(items, now=0.0)
      lossy_decision = lossy.decide(items, now=0.0)

      self.assertGreater(clean_decision.target_items, lossy_decision.target_items)
      self.assertGreater(clean_decision.predicted_payload_bytes, 0)

   def test_singleton_profile_does_not_collapse_prefill_target(self):
      controller = PhaseAwareBatchController(
         RouterConfig(
            maximum_runtime_batch_size=8,
            prefill_runtime_batch_size=8,
         )
      )
      controller.record_execution(
         BatchExecutionObservation(
            phase="PREFILL_CHUNK",
            batch_size=1,
            payload_bytes=12_000,
            execution_ms=1.0,
            successful=True,
         )
      )
      items = tuple(
         work(
            f"request-{index}",
            key=batch_key(phase="PREFILL_CHUNK", token_span=16),
            payload=b"x" * 12_000,
         )
         for index in range(8)
      )

      decision = controller.decide(items, now=1.0)

      self.assertEqual(decision.target_items, 8)
      self.assertEqual(decision.reason, "prefill_target_ready")

   def test_runtime_observations_prefer_lower_latency_batch_shape(self):
      controller = PhaseAwareBatchController(
         RouterConfig(
            maximum_runtime_batch_size=8,
            prefill_runtime_batch_size=8,
         )
      )
      controller.record_execution(
         BatchExecutionObservation(
            phase="PREFILL_CHUNK",
            batch_size=2,
            payload_bytes=24_000,
            execution_ms=4.0,
            successful=True,
         )
      )
      controller.record_execution(
         BatchExecutionObservation(
            phase="PREFILL_CHUNK",
            batch_size=4,
            payload_bytes=48_000,
            execution_ms=100.0,
            successful=True,
         )
      )
      items = tuple(
         work(
            f"request-{index}",
            key=batch_key(phase="PREFILL_CHUNK", token_span=16),
            payload=b"x" * 12_000,
         )
         for index in range(8)
      )

      decision = controller.decide(items, now=1.0)

      self.assertEqual(decision.target_items, 2)
      self.assertEqual(decision.reason, "profiled_prefill_target")


class RouterPhaseBatchingTests(unittest.TestCase):
   def make_router(
      self,
      *,
      config=None,
      qos="batch",
      target_ttft_ms=1_000.0,
      request_count=2,
   ):
      graph, base_request, base_manifest = locked_path()
      clock = ManualClock()
      runtime = FakeRuntimePort()
      transport = FakeTransportPort()
      first_placement = base_manifest.ordered_hops[0].placement_id
      placement = next(
         placement
         for stage in graph.stages
         for placement in stage.placements
         if placement.placement_id == first_placement
      )
      router = Router(
         node_id=placement.node_id,
         topology=FakeTopologyProvider(graph),
         device_states=FakeDeviceStateProvider(state_table()),
         capacity=FakeCapacityPort(),
         runtime=runtime,
         transport=transport,
         clock=clock,
         id_source=SequenceIdSource(),
         config=config or RouterConfig(),
      )
      registrations = []
      for index in range(request_count):
         suffix = chr(ord("a") + index)
         request = replace(
            base_request,
            request_id=f"request-{suffix}",
            qos_class=qos,
            admitted_at=0.0,
            target_ttft_ms=target_ttft_ms,
         )
         manifest = replace(
            base_manifest,
            request_id=request.request_id,
            path_id=f"path-{suffix}",
         )
         self.assertTrue(router.register_path(request, manifest, graph))
         registrations.append((request, manifest))
      return router, clock, runtime, transport, registrations

   def test_single_decode_executes_immediately_without_collection_wait(self):
      router, _, runtime, _, registrations = self.make_router(request_count=1)
      request, manifest = registrations[0]

      queued = router.enqueue_hop(first_header(request, manifest), b"abcd")
      results = router.drain_ready_batches()

      self.assertEqual(queued.disposition, "QUEUED")
      self.assertEqual(len(results), 1)
      self.assertEqual(len(runtime.executed_batches), 1)
      self.assertEqual(len(runtime.executed_batches[0].items), 1)
      self.assertEqual(router.batch_decisions()[-1].reason, "decode_singleton_immediate")

   def test_runtime_result_count_mismatch_fails_every_member_and_drains_queue(self):
      router, _, _, transport, registrations = self.make_router(request_count=1)
      runtime = WrongCountRuntime()
      router.relay.runtime = runtime
      request, manifest = registrations[0]
      router.enqueue_hop(first_header(request, manifest), b"abcd")

      results = router.drain_ready_batches()

      self.assertEqual(len(results), 1)
      self.assertEqual(results[0].disposition, "FAILED")
      self.assertEqual(results[0].reason, "runtime_batch_result_count_mismatch")
      self.assertEqual(len(transport.failure_reports), 1)
      self.assertEqual(router.pending_batch_hops(), 0)

   def test_ready_concurrent_decode_requests_execute_as_one_batch(self):
      router, _, runtime, transport, registrations = self.make_router()
      for request, manifest in registrations:
         self.assertEqual(
            router.enqueue_hop(first_header(request, manifest), b"abcd").disposition,
            "QUEUED",
         )

      results = router.drain_ready_batches()

      self.assertEqual(len(results), 2)
      self.assertEqual(len(runtime.executed_batches), 1)
      self.assertEqual(
         [item.request_id for item in runtime.executed_batches[0].items],
         ["request-a", "request-b"],
      )
      self.assertEqual(len(transport.hops), 2)
      self.assertEqual(router.batch_decisions()[-1].reason, "decode_ready_batch")
      self.assertIn(("DECODE", 2), router.batch_execution_profiles())

   def test_batch_prefill_waits_only_until_collection_window(self):
      config = RouterConfig(
         prefill_collection_window_seconds=0.005,
         prefill_runtime_batch_size=4,
      )
      router, clock, runtime, _, registrations = self.make_router(
         config=config,
         request_count=1,
      )
      request, manifest = registrations[0]
      header = replace(
         first_header(request, manifest, phase="PREFILL_CHUNK", token_index=1),
         prefill_chunk_token_count=16,
      )

      router.enqueue_hop(header, b"prefill")
      self.assertEqual(router.drain_ready_batches(), ())
      self.assertEqual(runtime.executed_batches, [])
      self.assertAlmostEqual(router.next_batch_deadline(), 0.005)

      clock.advance(0.005)
      results = router.drain_ready_batches()

      self.assertEqual(len(results), 1)
      self.assertEqual(len(runtime.executed_batches), 1)
      self.assertEqual(router.batch_decisions()[-1].reason, "prefill_collection_expired")

   def test_ready_concurrent_prefill_reaches_target_without_waiting(self):
      config = RouterConfig(
         prefill_collection_window_seconds=0.100,
         prefill_runtime_batch_size=2,
      )
      router, _, runtime, _, registrations = self.make_router(config=config)
      for request, manifest in registrations:
         header = replace(
            first_header(
               request,
               manifest,
               phase="PREFILL_CHUNK",
               token_index=1,
            ),
            prefill_chunk_token_count=16,
         )
         router.enqueue_hop(header, b"prefill")

      results = router.drain_ready_batches()

      self.assertEqual(len(results), 2)
      self.assertEqual(len(runtime.executed_batches), 1)
      self.assertEqual(len(runtime.executed_batches[0].items), 2)
      self.assertEqual(router.batch_decisions()[-1].reason, "prefill_target_ready")

   def test_pending_duplicate_is_idempotent_and_release_clears_queue(self):
      config = RouterConfig(prefill_collection_window_seconds=1.0)
      router, _, runtime, _, registrations = self.make_router(
         config=config,
         request_count=1,
      )
      request, manifest = registrations[0]
      header = replace(
         first_header(request, manifest, phase="PREFILL_CHUNK", token_index=1),
         prefill_chunk_token_count=16,
      )

      first = router.enqueue_hop(header, b"prefill")
      duplicate = router.enqueue_hop(header, b"prefill")
      router.relay.release_path(manifest.path_id)

      self.assertEqual(first.disposition, "QUEUED")
      self.assertEqual(duplicate.reason, "duplicate_pending")
      self.assertEqual(router.pending_batch_hops(), 0)
      self.assertEqual(runtime.executed_batches, [])

   def test_interactive_prefill_singleton_never_waits_for_batch(self):
      config = RouterConfig(prefill_collection_window_seconds=0.100)
      router, _, runtime, _, registrations = self.make_router(
         config=config,
         qos="interactive",
         request_count=1,
      )
      request, manifest = registrations[0]
      header = replace(
         first_header(request, manifest, phase="PREFILL_CHUNK", token_index=1),
         prefill_chunk_token_count=16,
      )

      router.enqueue_hop(header, b"prefill")
      results = router.drain_ready_batches()

      self.assertEqual(len(results), 1)
      self.assertEqual(len(runtime.executed_batches), 1)
      self.assertEqual(router.batch_decisions()[-1].reason, "interactive_prefill_immediate")

   def test_prefill_timer_wakes_before_collection_window_to_protect_sla(self):
      config = RouterConfig(prefill_collection_window_seconds=0.200)
      router, _, _, _, registrations = self.make_router(
         config=config,
         qos="batch",
         target_ttft_ms=100.0,
         request_count=1,
      )
      request, manifest = registrations[0]
      header = replace(
         first_header(request, manifest, phase="PREFILL_CHUNK", token_index=1),
         prefill_chunk_token_count=16,
      )

      router.enqueue_hop(header, b"prefill")
      self.assertEqual(router.drain_ready_batches(), ())
      wake_at = router.next_batch_deadline()

      self.assertIsNotNone(wake_at)
      self.assertGreater(wake_at, 0.0)
      self.assertLess(wake_at, 0.100)
      self.assertLess(wake_at, config.prefill_collection_window_seconds)

   def test_prefill_earliest_deadline_flushes_before_collection_window(self):
      config = RouterConfig(prefill_collection_window_seconds=0.100)
      router, _, _, _, registrations = self.make_router(
         config=config,
         qos="batch",
         target_ttft_ms=1.0,
         request_count=1,
      )
      request, manifest = registrations[0]
      header = replace(
         first_header(request, manifest, phase="PREFILL_CHUNK", token_index=1),
         prefill_chunk_token_count=16,
      )

      router.enqueue_hop(header, b"prefill")
      results = router.drain_ready_batches()

      self.assertEqual(len(results), 1)
      self.assertEqual(router.batch_decisions()[-1].reason, "earliest_deadline")

   def test_batch_byte_cap_leaves_excess_compatible_work_queued(self):
      config = RouterConfig(
         maximum_runtime_batch_size=8,
         decode_runtime_batch_size=8,
         maximum_runtime_batch_bytes=8,
      )
      router, _, runtime, _, registrations = self.make_router(
         config=config,
         request_count=3,
      )
      for request, manifest in registrations:
         router.enqueue_hop(first_header(request, manifest), b"abcd")

      first_results = router.drain_ready_batches(maximum_batches=1)

      self.assertEqual(len(first_results), 2)
      self.assertEqual(len(runtime.executed_batches[0].items), 2)
      self.assertEqual(router.pending_batch_hops(), 1)

      second_results = router.drain_ready_batches()
      self.assertEqual(len(second_results), 1)
      self.assertEqual(router.pending_batch_hops(), 0)


if __name__ == "__main__":
   unittest.main()
