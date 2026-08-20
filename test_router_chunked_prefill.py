import unittest
from dataclasses import replace

from mycelium_router.contracts import (
   PrefillChunkCompleted,
   RouterConfig,
)
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
from mycelium_router.payloads import decode_token_ids
from mycelium_router.router import Router
from mycelium_router.wire import decode_frame, encode_frame
from test_router_inprocess_mesh import three_device_graph
from test_router_policy import request_fixture, state_table


class ChunkedPrefillTests(unittest.TestCase):
   def setUp(self):
      self.graph = three_device_graph()
      states = state_table(slow_b_bandwidth=True)
      states["node-d"] = replace(states["node-a"], node_id="node-d")
      self.capacity = FakeCapacityPort()
      self.clock = ManualClock()
      self.mesh = InProcessMesh()
      self.runtimes = {}
      self.routers = {}
      config = RouterConfig(prefill_chunk_size_tokens=2)
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
            config=config,
         )
         self.mesh.register_router(node_id, router)
         self.runtimes[node_id] = runtime
         self.routers[node_id] = router
      self.entry = self.routers["node-a"]
      self.sink = InMemoryClientSink()
      self.request = replace(
         request_fixture(),
         request_id="request-chunked-prefill",
         prompt_token_ids=(11, 12, 13, 14, 15),
         max_new_tokens=1,
         expected_new_tokens=1,
      )

   def test_prompt_chunks_run_in_order_over_one_locked_path(self):
      self.entry.start_distributed_prefill(
         self.request,
         self.sink,
         excluded_placements=frozenset({"node-b-stage-000"}),
      )

      self.assertEqual(self.entry.request_status(self.request.request_id), "DECODING")
      manifest = self.entry.get_request(self.request.request_id).manifest
      for runtime in self.runtimes.values():
         prefill = [
            item
            for item in runtime.executed
            if item.phase in {"PREFILL", "PREFILL_CHUNK"}
         ]
         self.assertEqual(
            [item.phase for item in prefill],
            ["PREFILL", "PREFILL_CHUNK", "PREFILL_CHUNK"],
         )
         self.assertEqual([item.token_index for item in prefill], [-1, 1, 2])
         self.assertEqual(
            [item.prefill_chunk_token_count for item in prefill],
            [2, 2, 1],
         )
         self.assertEqual(
            [decode_token_ids(item.payload) for item in prefill],
            [(11, 12), (13, 14), (15,)],
         )
         self.assertTrue(all(item.batch_key is not None for item in prefill))
         self.assertEqual(
            [item.batch_key.phase for item in prefill],
            ["PREFILL", "PREFILL_CHUNK", "PREFILL_CHUNK"],
         )
         self.assertEqual(
            [item.batch_key.token_span for item in prefill],
            [2, 2, 1],
         )
         self.assertEqual({item.path_id for item in prefill}, {manifest.path_id})

      self.assertEqual(
         [event.chunk_index for _, event in self.mesh.prefill_chunk_completions],
         [1, 2],
      )
      self.assertEqual(
         [event.token_count for _, event in self.mesh.prefill_chunk_completions],
         [2, 1],
      )
      duplicate = self.mesh.prefill_chunk_completions[-1][1]
      self.assertFalse(self.entry.receive_prefill_chunk_completed(duplicate))

      self.assertTrue(self.entry.decode_one_distributed(self.request.request_id))
      self.assertEqual(self.sink.token_ids, [101])

   def test_decode_waits_for_every_chunk_completion(self):
      self.mesh.defer_prefill_chunk_completions = True

      self.entry.start_distributed_prefill(
         self.request,
         self.sink,
         excluded_placements=frozenset({"node-b-stage-000"}),
      )

      self.assertEqual(self.entry.request_status(self.request.request_id), "LOCKED")
      self.assertFalse(self.entry.decode_one_distributed(self.request.request_id))
      self.assertEqual(len(self.mesh.deferred_prefill_chunk_completions), 1)

      self.assertTrue(self.mesh.deliver_next_prefill_chunk_completion())
      self.assertEqual(self.entry.request_status(self.request.request_id), "LOCKED")
      self.assertFalse(self.entry.decode_one_distributed(self.request.request_id))
      self.assertEqual(len(self.mesh.deferred_prefill_chunk_completions), 1)

      self.assertTrue(self.mesh.deliver_next_prefill_chunk_completion())
      self.assertEqual(self.entry.request_status(self.request.request_id), "DECODING")
      self.assertTrue(self.entry.decode_one_distributed(self.request.request_id))

   def test_only_last_locked_prefill_chunk_is_terminal(self):
      relay = self.entry.relay

      self.assertFalse(
         relay._is_terminal(self.request, "PREFILL_CHUNK", 0, 2)
      )
      self.assertFalse(
         relay._is_terminal(self.request, "PREFILL_CHUNK", 1, 2)
      )
      self.assertTrue(
         relay._is_terminal(self.request, "PREFILL_CHUNK", 2, 1)
      )

   def test_chunk_completion_round_trips_through_wire(self):
      event = PrefillChunkCompleted(
         request_id="request-wire-chunk",
         path_id="path-wire-chunk",
         path_attempt=2,
         chunk_index=3,
         token_count=16,
      )

      decoded = decode_frame(encode_frame(event))

      self.assertEqual(decoded.message, event)
      self.assertEqual(decoded.payload, b"")


if __name__ == "__main__":
   unittest.main()
