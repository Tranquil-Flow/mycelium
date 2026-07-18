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
from mycelium_router.idempotency import hop_idempotency_key
from mycelium_router.router import Router
from mycelium_router.transports.loopback_socket import (
   LoopbackSocketMesh,
   SocketTransportError,
)
from mycelium_router.wire import decode_frame
from test_router_inprocess_mesh import three_device_graph
from test_router_policy import request_fixture, state_table


class _DeferringPrefillCompletionMesh(LoopbackSocketMesh):
   def __init__(self):
      super().__init__()
      self.defer_prefill_completion = True
      self.deferred_prefill_completions = []

   def _dispatch(self, node_id, action, frame):
      message = decode_frame(frame).message
      if self.defer_prefill_completion and isinstance(
         message,
         PrefillChunkCompleted,
      ):
         self.deferred_prefill_completions.append(message)
         return
      return super()._dispatch(node_id, action, frame)


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

   def test_tcp_hop_source_must_own_previous_locked_placement(self):
      graph = three_device_graph()
      states = state_table(slow_b_bandwidth=True)
      states["node-d"] = replace(states["node-a"], node_id="node-d")
      capacity = FakeCapacityPort()
      clock = ManualClock()
      mesh = LoopbackSocketMesh()
      runtimes = {}
      routers = {}
      sink = InMemoryClientSink()
      request = replace(request_fixture(), request_id="request-socket-hop-origin")

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
            config=RouterConfig(prefill_chunk_size_tokens=0),
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
         record = entry.get_request(request.request_id)
         previous_hop, destination_hop = record.manifest.ordered_hops[:2]
         forged_header = HopHeader(
            request_id=request.request_id,
            path_id=record.manifest.path_id,
            path_attempt=record.manifest.path_attempt,
            phase="DECODE",
            token_index=0,
            hop_index=1,
            source_placement_id=previous_hop.placement_id,
            destination_placement_id=destination_hop.placement_id,
            topology_version=record.manifest.topology_version,
            idempotency_key=hop_idempotency_key(
               request_id=request.request_id,
               path_id=record.manifest.path_id,
               path_attempt=record.manifest.path_attempt,
               phase="DECODE",
               token_index=0,
               hop_index=1,
            ),
         )
         executions_before = len(runtimes["node-c"].executed)

         mesh.transport_for("node-d").send_hop(forged_header, b"forged")

         self.assertEqual(len(runtimes["node-c"].executed), executions_before)
         self.assertEqual(record.generated_token_ids, [])
         self.assertEqual(sink.token_ids, [])
      finally:
         mesh.close()

   def test_tcp_route_building_hop_source_must_own_previous_selected_placement(self):
      graph = three_device_graph()
      states = state_table(slow_b_bandwidth=True)
      states["node-d"] = replace(states["node-a"], node_id="node-d")
      capacity = FakeCapacityPort()
      clock = ManualClock()
      mesh = LoopbackSocketMesh()
      runtimes = {}
      routers = {}
      sink = InMemoryClientSink()
      request = replace(
         request_fixture(),
         request_id="request-socket-route-building-origin",
      )

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
            config=RouterConfig(),
         )
         mesh.register_router(node_id, router)
         runtimes[node_id] = runtime
         routers[node_id] = router

      honest_send_hop = mesh.transport_for("node-a").send_hop

      def forge_second_route_building_hop(header, payload):
         if header.phase == "PREFILL" and header.hop_index == 1:
            return mesh.transport_for("node-d").send_hop(header, payload)
         return honest_send_hop(header, payload)

      mesh.transport_for("node-a").send_hop = forge_second_route_building_hop
      mesh.start()
      try:
         entry = routers["node-a"]
         entry.start_distributed_prefill(
            request,
            sink,
            excluded_placements=frozenset({"node-b-stage-000"}),
         )

         self.assertEqual(entry.request_status(request.request_id), "PREFILL")
         self.assertEqual(len(runtimes["node-a"].executed), 1)
         self.assertEqual(runtimes["node-c"].executed, [])
         self.assertEqual(runtimes["node-d"].executed, [])
         self.assertEqual(sink.token_ids, [])
      finally:
         mesh.close()

   def test_tcp_hop_zero_source_must_match_admitted_request_entry(self):
      graph = three_device_graph()
      states = state_table(slow_b_bandwidth=True)
      states["node-d"] = replace(states["node-a"], node_id="node-d")
      capacity = FakeCapacityPort()
      clock = ManualClock()
      mesh = LoopbackSocketMesh()
      runtimes = {}
      routers = {}
      sink = InMemoryClientSink()
      request = replace(request_fixture(), request_id="request-socket-hop-zero-origin")

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
            config=RouterConfig(prefill_chunk_size_tokens=0),
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
         record = entry.get_request(request.request_id)
         first_hop = record.manifest.ordered_hops[0]
         final_hop = record.manifest.ordered_hops[-1]
         forged_header = HopHeader(
            request_id=request.request_id,
            path_id=record.manifest.path_id,
            path_attempt=record.manifest.path_attempt,
            phase="DECODE",
            token_index=0,
            hop_index=0,
            source_placement_id=final_hop.placement_id,
            destination_placement_id=first_hop.placement_id,
            topology_version=record.manifest.topology_version,
            idempotency_key=hop_idempotency_key(
               request_id=request.request_id,
               path_id=record.manifest.path_id,
               path_attempt=record.manifest.path_attempt,
               phase="DECODE",
               token_index=0,
               hop_index=0,
            ),
         )
         executions_before = len(runtimes["node-a"].executed)

         mesh.transport_for("node-d").send_hop(forged_header, b"forged")

         self.assertEqual(len(runtimes["node-a"].executed), executions_before)
         self.assertEqual(record.generated_token_ids, [])
         self.assertEqual(sink.token_ids, [])
         self.assertTrue(entry.decode_one_distributed(request.request_id))
         self.assertEqual(sink.token_ids, [101])
      finally:
         mesh.close()

   def test_tcp_manifest_lock_source_cannot_register_forged_participant_path(self):
      graph = three_device_graph()
      states = state_table(slow_b_bandwidth=True)
      states["node-d"] = replace(states["node-a"], node_id="node-d")
      capacity = FakeCapacityPort()
      clock = ManualClock()
      mesh = LoopbackSocketMesh()
      routers = {}
      request = replace(
         request_fixture(),
         request_id="request-socket-manifest-lock-origin",
      )

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
         captured_locks = []
         receive_manifest_locked = entry.receive_manifest_locked

         def capture_manifest_lock(locked, **_kwargs):
            captured_locks.append(locked)
            return True

         entry.receive_manifest_locked = capture_manifest_lock
         try:
            entry.start_distributed_prefill(
               request,
               InMemoryClientSink(),
               excluded_placements=frozenset({"node-b-stage-000"}),
            )
         finally:
            entry.receive_manifest_locked = receive_manifest_locked

         self.assertEqual(entry.request_status(request.request_id), "PREFILL")
         self.assertEqual(len(captured_locks), 1)
         locked = captured_locks[0]
         self.assertEqual(
            locked.manifest.ordered_hops[-1].placement_id,
            "node-d-stage-002",
         )
         forged_path_id = f"{locked.path_id}-forged"
         forged = replace(
            locked,
            path_id=forged_path_id,
            manifest=replace(locked.manifest, path_id=forged_path_id),
            build=replace(locked.build, path_id=forged_path_id),
         )
         middle_hop = forged.manifest.ordered_hops[1]
         previous_hop = forged.manifest.ordered_hops[0]
         forged_header = HopHeader(
            request_id=forged.request_id,
            path_id=forged.path_id,
            path_attempt=forged.path_attempt,
            phase="DECODE",
            token_index=0,
            hop_index=1,
            source_placement_id=previous_hop.placement_id,
            destination_placement_id=middle_hop.placement_id,
            topology_version=forged.manifest.topology_version,
            idempotency_key=hop_idempotency_key(
               request_id=forged.request_id,
               path_id=forged.path_id,
               path_attempt=forged.path_attempt,
               phase="DECODE",
               token_index=0,
               hop_index=1,
            ),
         )
         executions_before = len(routers["node-c"].relay.runtime.executed)

         with self.assertRaisesRegex(
            SocketTransportError,
            "manifest_registration_rejected",
         ):
            mesh.transport_for("node-c").send_manifest_locked(forged)

         result = routers["node-c"].receive_hop(forged_header, b"forged")
         self.assertEqual(result.disposition, "REJECTED")
         self.assertEqual(result.reason, "unknown_path")
         self.assertEqual(
            len(routers["node-c"].relay.runtime.executed),
            executions_before,
         )
         self.assertEqual(entry.request_status(request.request_id), "PREFILL")
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

   def test_tcp_prefill_completion_rejects_non_final_hop_source(self):
      graph = three_device_graph()
      states = state_table(slow_b_bandwidth=True)
      states["node-d"] = replace(states["node-a"], node_id="node-d")
      capacity = FakeCapacityPort()
      clock = ManualClock()
      mesh = _DeferringPrefillCompletionMesh()
      routers = {}
      request = replace(
         request_fixture(),
         request_id="request-socket-prefill-completion-origin",
         prompt_token_ids=(11, 12, 13, 14),
      )

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
            config=RouterConfig(prefill_chunk_size_tokens=2),
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
         record = entry.get_request(request.request_id)
         self.assertEqual(record.status, "LOCKED")
         self.assertEqual(record.completed_prefill_chunks, 1)
         genuine_event = mesh.deferred_prefill_completions[0]

         mesh.defer_prefill_completion = False
         mesh.transport_for("node-c").send_prefill_chunk_completed(genuine_event)

         self.assertEqual(record.status, "LOCKED")
         self.assertEqual(record.completed_prefill_chunks, 1)
      finally:
         mesh.close()


if __name__ == "__main__":
   unittest.main()
