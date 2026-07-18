import unittest
from dataclasses import replace

from mycelium_router.contracts import (
   FailureReport,
   HopHeader,
   ManifestLocked,
   PrefillChunkCompleted,
   ProgressivePrefillMessage,
   RouterConfig,
   TokenEvent,
)
from mycelium_router.fakes import (
   FakeCapacityPort,
   FakeDeviceStateProvider,
   FakeRuntimePort,
   FakeTopologyProvider,
   InMemoryClientSink,
   ManualClock,
   SequenceIdSource,
)
from mycelium_router.router import Router
from mycelium_router.transports.loopback_socket import LoopbackSocketMesh
from mycelium_router.wire import decode_frame
from test_router_inprocess_mesh import three_device_graph
from test_router_policy import request_fixture, state_table


class LoopbackSocketMeshTests(unittest.TestCase):
   def test_three_routers_prefill_and_decode_through_real_tcp_frames(self):
      graph = three_device_graph()
      states = state_table(slow_b_bandwidth=True)
      states["node-d"] = replace(states["node-a"], node_id="node-d")
      capacity = FakeCapacityPort()
      clock = ManualClock()
      mesh = LoopbackSocketMesh()
      runtimes = {}
      routers = {}
      sink = InMemoryClientSink()
      request = replace(request_fixture(), request_id="request-socket-mesh")

      for node_id in ("node-a", "node-c", "node-d"):
         runtime = FakeRuntimePort()
         router = Router(
            node_id=node_id,
            topology=FakeTopologyProvider(graph),
            device_states=FakeDeviceStateProvider(states),
            capacity=capacity,
            runtime=runtime,
            transport=mesh.transport_for(node_id),
            clock=clock,
            id_source=SequenceIdSource(),
            config=RouterConfig(prefill_chunk_size_tokens=2),
         )
         mesh.register_router(node_id, router)
         runtimes[node_id] = runtime
         routers[node_id] = router

      mesh.start()
      try:
         entry = routers["node-a"]
         entry.start_distributed_prefill(
            request,
            sink,
            excluded_placements=frozenset({"node-b-stage-000"}),
         )
         self.assertEqual(entry.request_status(request.request_id), "DECODING")
         self.assertTrue(entry.decode_one_distributed(request.request_id))

         self.assertEqual(sink.token_ids, [101])
         self.assertEqual(sink.token_indexes, [0])
         self.assertEqual(mesh.bound_hosts(), {"127.0.0.1"})
         self.assertTrue(all(port > 0 for _, port in mesh.endpoints().values()))
         self.assertGreater(mesh.connection_count, 0)
         self.assertLessEqual(mesh.maximum_active_connections, 8)
         self.assertEqual(mesh.active_connection_count, 0)
         self.assertTrue(mesh.frames)
         self.assertTrue(all(isinstance(frame, bytes) for frame in mesh.frames))

         decoded_types = {type(decode_frame(frame).message) for frame in mesh.frames}
         self.assertTrue(
            {
               ProgressivePrefillMessage,
               ManifestLocked,
               HopHeader,
               PrefillChunkCompleted,
               TokenEvent,
            }
            <= decoded_types
         )
         for runtime in runtimes.values():
            decode_work = [item for item in runtime.executed if item.phase == "DECODE"]
            self.assertEqual(len(decode_work), 1)
            self.assertIsInstance(decode_work[0].payload, bytes)
      finally:
         mesh.close()

   def test_tcp_failure_report_dispatch_preserves_registered_source_identity(self):
      graph = three_device_graph()
      states = state_table(slow_b_bandwidth=True)
      states["node-d"] = replace(states["node-a"], node_id="node-d")
      capacity = FakeCapacityPort()
      clock = ManualClock()
      mesh = LoopbackSocketMesh()
      routers = {}
      request = replace(request_fixture(), request_id="request-socket-origin")

      for node_id in ("node-a", "node-c", "node-d"):
         config = RouterConfig(
            maximum_recovery_attempts=0 if node_id == "node-a" else 3,
            prefill_chunk_size_tokens=0,
         )
         router = Router(
            node_id=node_id,
            topology=FakeTopologyProvider(graph),
            device_states=FakeDeviceStateProvider(states),
            capacity=capacity,
            runtime=FakeRuntimePort(),
            transport=mesh.transport_for(node_id),
            clock=clock,
            id_source=SequenceIdSource(),
            config=config,
         )
         mesh.register_router(node_id, router)
         routers[node_id] = router

      mesh.start()
      try:
         entry = routers["node-a"]
         entry.start_distributed_prefill(
            request,
            InMemoryClientSink(),
            excluded_placements=frozenset({"node-b-stage-000"}),
         )
         original_manifest = entry.get_request(request.request_id).manifest

         mesh.transport_for("node-d").send_failure_report(
            FailureReport(
               request_id=request.request_id,
               path_id=original_manifest.path_id,
               path_attempt=original_manifest.path_attempt,
               token_index=0,
               scope="PLACEMENT",
               reason="forged_tcp_peer_origin",
               placement_id=original_manifest.ordered_hops[1].placement_id,
               node_id="node-d",
            )
         )

         self.assertEqual(entry.request_status(request.request_id), "DECODING")
         self.assertEqual(entry.get_request(request.request_id).manifest, original_manifest)
         self.assertEqual(entry.entry.runtime.cancel_calls, [])
      finally:
         mesh.close()

   def test_tcp_token_event_dispatch_rejects_non_final_hop_source(self):
      graph = three_device_graph()
      states = state_table(slow_b_bandwidth=True)
      states["node-d"] = replace(states["node-a"], node_id="node-d")
      capacity = FakeCapacityPort()
      clock = ManualClock()
      mesh = LoopbackSocketMesh()
      routers = {}
      sink = InMemoryClientSink()
      request = replace(request_fixture(), request_id="request-socket-token-origin")

      for node_id in ("node-a", "node-c", "node-d"):
         router = Router(
            node_id=node_id,
            topology=FakeTopologyProvider(graph),
            device_states=FakeDeviceStateProvider(states),
            capacity=capacity,
            runtime=FakeRuntimePort(),
            transport=mesh.transport_for(node_id),
            clock=clock,
            id_source=SequenceIdSource(),
            config=RouterConfig(prefill_chunk_size_tokens=0),
         )
         mesh.register_router(node_id, router)
         routers[node_id] = router

      mesh.start()
      try:
         entry = routers["node-a"]
         entry.start_distributed_prefill(
            request,
            sink,
            excluded_placements=frozenset({"node-b-stage-000"}),
         )
         record = entry.get_request(request.request_id)

         mesh.transport_for("node-c").send_token_event(
            TokenEvent(
               request_id=request.request_id,
               path_id=record.manifest.path_id,
               path_attempt=record.manifest.path_attempt,
               token_index=0,
               token_id=999,
               sampling_counter=1,
            )
         )

         self.assertEqual(sink.token_ids, [])
         self.assertEqual(record.generated_token_ids, [])
         self.assertEqual(entry.request_status(request.request_id), "DECODING")
      finally:
         mesh.close()


if __name__ == "__main__":
   unittest.main()
