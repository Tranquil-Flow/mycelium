import unittest
from dataclasses import replace

from mycelium_router.contracts import (
   FailureReport,
   HopHeader,
   PrefillChunkCompleted,
   ProgressivePrefillContext,
   RouterConfig,
   TokenEvent,
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
from mycelium_router.idempotency import hop_idempotency_key
from mycelium_router.router import Router
from test_router_contracts import graph_fixture
from test_router_policy import request_fixture, state_table


def three_device_graph():
   graph = graph_fixture()
   original = graph.stages[-1].placements[0]
   final = replace(
      original,
      placement_id="node-d-stage-002",
      node_id="node-d",
   )
   final_stage = replace(graph.stages[-1], placements=(final,))
   edges = tuple(
      replace(edge, to_placement_id=final.placement_id)
      if edge.to_placement_id == original.placement_id
      else edge
      for edge in graph.edges
   )
   loopbacks = tuple(
      replace(edge, from_placement_id=final.placement_id)
      if edge.from_placement_id == original.placement_id
      else edge
      for edge in graph.loopback_edges
   )
   return replace(
      graph,
      stages=(graph.stages[0], graph.stages[1], final_stage),
      edges=edges,
      loopback_edges=loopbacks,
   )


class InProcessMeshTests(unittest.TestCase):
   def setUp(self):
      self.graph = three_device_graph()
      states = state_table(slow_b_bandwidth=True)
      states["node-d"] = replace(states["node-a"], node_id="node-d")
      self.states = states
      self.capacity = FakeCapacityPort()
      self.clock = ManualClock()
      self.mesh = InProcessMesh()
      self.runtimes = {}
      self.routers = {}
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
            config=RouterConfig(),
         )
         self.mesh.register_router(node_id, router)
         self.runtimes[node_id] = runtime
         self.routers[node_id] = router
      self.entry = self.routers["node-a"]
      self.sink = InMemoryClientSink()
      self.request = replace(
         request_fixture(),
         request_id="request-mesh",
      )

   def test_mesh_runs_three_device_prefill_and_locked_decode_exactly_once(self):
      self.entry.start_distributed_prefill(
         self.request,
         self.sink,
         excluded_placements=frozenset({"node-b-stage-000"}),
      )

      self.assertEqual(self.entry.request_status(self.request.request_id), "DECODING")
      manifest = self.entry.get_request(self.request.request_id).manifest
      placement_nodes = {
         placement.placement_id: placement.node_id
         for stage in self.graph.stages
         for placement in stage.placements
      }
      self.assertEqual(
         [placement_nodes[hop.placement_id] for hop in manifest.ordered_hops],
         ["node-a", "node-c", "node-d"],
      )

      self.assertTrue(self.entry.decode_one_distributed(self.request.request_id))

      self.assertEqual(self.sink.token_indexes, [0])
      self.assertEqual(self.sink.token_ids, [101])
      decode_deliveries = [
         delivery
         for delivery in self.mesh.hop_deliveries
         if delivery.header.phase == "DECODE"
      ]
      self.assertEqual(len(decode_deliveries), 3)
      for node_id, runtime in self.runtimes.items():
         local_decode = [item for item in runtime.executed if item.phase == "DECODE"]
         self.assertEqual(len(local_decode), 1)
         self.assertEqual(
            placement_nodes[local_decode[0].placement_id],
            node_id,
         )

   def test_hop_source_must_own_previous_locked_placement(self):
      self.entry.start_distributed_prefill(
         self.request,
         self.sink,
         excluded_placements=frozenset({"node-b-stage-000"}),
      )
      record = self.entry.get_request(self.request.request_id)
      previous_hop, destination_hop = record.manifest.ordered_hops[:2]
      forged_header = HopHeader(
         request_id=self.request.request_id,
         path_id=record.manifest.path_id,
         path_attempt=record.manifest.path_attempt,
         phase="DECODE",
         token_index=0,
         hop_index=1,
         source_placement_id=previous_hop.placement_id,
         destination_placement_id=destination_hop.placement_id,
         topology_version=record.manifest.topology_version,
         idempotency_key=hop_idempotency_key(
            request_id=self.request.request_id,
            path_id=record.manifest.path_id,
            path_attempt=record.manifest.path_attempt,
            phase="DECODE",
            token_index=0,
            hop_index=1,
         ),
      )
      executions_before = len(self.runtimes["node-c"].executed)

      self.mesh.transport_for("node-d").send_hop(forged_header, b"forged")

      self.assertEqual(self.mesh.hop_results[-1].disposition, "REJECTED")
      self.assertEqual(self.mesh.hop_results[-1].reason, "source_node_mismatch")
      self.assertEqual(len(self.runtimes["node-c"].executed), executions_before)
      self.assertEqual(record.generated_token_ids, [])
      self.assertEqual(self.sink.token_ids, [])

   def test_route_building_hop_source_must_own_previous_selected_placement(self):
      send_hop = self.mesh.send_hop

      def forge_second_route_building_hop(source_node_id, header, payload):
         if isinstance(payload, ProgressivePrefillContext) and header.hop_index == 1:
            source_node_id = "node-d"
         return send_hop(source_node_id, header, payload)

      self.mesh.send_hop = forge_second_route_building_hop

      self.entry.start_distributed_prefill(
         self.request,
         self.sink,
         excluded_placements=frozenset({"node-b-stage-000"}),
      )

      self.assertEqual(self.entry.request_status(self.request.request_id), "PREFILL")
      rejected_reasons = [
         getattr(result, "reason", "")
         for result in self.mesh.hop_results
         if getattr(result, "disposition", "") == "REJECTED"
      ]
      self.assertIn("source_node_mismatch", rejected_reasons)
      self.assertEqual(len(self.runtimes["node-c"].executed), 0)
      self.assertEqual(len(self.runtimes["node-d"].executed), 0)

   def test_route_building_hop_zero_source_must_match_admitted_entry(self):
      entry_transport = self.mesh.transport_for("node-a")
      honest_send_hop = entry_transport.send_hop

      def forge_first_route_building_hop(header, payload):
         if isinstance(payload, ProgressivePrefillContext) and header.hop_index == 0:
            return self.mesh.transport_for("node-d").send_hop(header, payload)
         return honest_send_hop(header, payload)

      entry_transport.send_hop = forge_first_route_building_hop

      self.entry.start_distributed_prefill(
         self.request,
         self.sink,
         excluded_placements=frozenset({"node-b-stage-000"}),
      )

      self.assertEqual(self.entry.request_status(self.request.request_id), "PREFILL")
      self.assertEqual(self.mesh.hop_results[-1].disposition, "REJECTED")
      self.assertEqual(self.mesh.hop_results[-1].reason, "entry_node_mismatch")
      self.assertEqual(len(self.runtimes["node-a"].executed), 0)
      self.assertEqual(len(self.runtimes["node-c"].executed), 0)
      self.assertEqual(len(self.runtimes["node-d"].executed), 0)

   def test_conflicting_request_entry_registration_fails_before_reservation(self):
      self.mesh.remember_entry(self.request.request_id, "node-d")

      with self.assertRaisesRegex(ValueError, "request_entry_conflict"):
         self.entry.start_distributed_prefill(
            self.request,
            self.sink,
            excluded_placements=frozenset({"node-b-stage-000"}),
         )

      with self.assertRaises(KeyError):
         self.entry.request_status(self.request.request_id)
      self.assertEqual(self.capacity.requests, [])
      self.assertEqual(self.mesh.manifest_deltas, [])
      self.assertEqual(self.mesh.hop_deliveries, [])

   def test_cached_route_building_hop_revalidates_transport_source(self):
      send_hop = self.mesh.send_hop
      captured = {}

      def capture_second_route_building_hop(source_node_id, header, payload):
         if isinstance(payload, ProgressivePrefillContext) and header.hop_index == 1:
            captured["header"] = header
            captured["payload"] = payload
         return send_hop(source_node_id, header, payload)

      self.mesh.send_hop = capture_second_route_building_hop
      self.entry.start_distributed_prefill(
         self.request,
         self.sink,
         excluded_placements=frozenset({"node-b-stage-000"}),
      )
      executions_before = len(self.runtimes["node-c"].executed)

      send_hop("node-d", captured["header"], captured["payload"])

      result = self.mesh.hop_results[-1]
      self.assertEqual(getattr(result, "disposition", ""), "REJECTED")
      self.assertEqual(getattr(result, "reason", ""), "source_node_mismatch")
      self.assertEqual(len(self.runtimes["node-c"].executed), executions_before)

   def test_hop_zero_source_must_match_admitted_request_entry(self):
      self.entry.start_distributed_prefill(
         self.request,
         self.sink,
         excluded_placements=frozenset({"node-b-stage-000"}),
      )
      record = self.entry.get_request(self.request.request_id)
      first_hop = record.manifest.ordered_hops[0]
      final_hop = record.manifest.ordered_hops[-1]
      forged_header = HopHeader(
         request_id=self.request.request_id,
         path_id=record.manifest.path_id,
         path_attempt=record.manifest.path_attempt,
         phase="DECODE",
         token_index=0,
         hop_index=0,
         source_placement_id=final_hop.placement_id,
         destination_placement_id=first_hop.placement_id,
         topology_version=record.manifest.topology_version,
         idempotency_key=hop_idempotency_key(
            request_id=self.request.request_id,
            path_id=record.manifest.path_id,
            path_attempt=record.manifest.path_attempt,
            phase="DECODE",
            token_index=0,
            hop_index=0,
         ),
      )
      executions_before = len(self.runtimes["node-a"].executed)

      self.mesh.transport_for("node-d").send_hop(forged_header, b"forged")

      self.assertEqual(self.mesh.hop_results[-1].disposition, "REJECTED")
      self.assertEqual(self.mesh.hop_results[-1].reason, "entry_node_mismatch")
      self.assertEqual(len(self.runtimes["node-a"].executed), executions_before)
      self.assertEqual(record.generated_token_ids, [])
      self.assertEqual(self.sink.token_ids, [])
      self.assertTrue(self.entry.decode_one_distributed(self.request.request_id))
      self.assertEqual(self.sink.token_ids, [101])

   def test_cached_hop_zero_still_rejects_non_entry_source(self):
      self.entry.start_distributed_prefill(
         self.request,
         self.sink,
         excluded_placements=frozenset({"node-b-stage-000"}),
      )
      self.assertTrue(self.entry.decode_one_distributed(self.request.request_id))
      genuine = next(
         delivery.header
         for delivery in self.mesh.hop_deliveries
         if delivery.header.phase == "DECODE" and delivery.header.hop_index == 0
      )
      executions_before = len(self.runtimes["node-a"].executed)

      self.mesh.transport_for("node-d").send_hop(genuine, b"forged-duplicate")

      self.assertEqual(self.mesh.hop_results[-1].disposition, "REJECTED")
      self.assertEqual(self.mesh.hop_results[-1].reason, "entry_node_mismatch")
      self.assertEqual(len(self.runtimes["node-a"].executed), executions_before)
      self.assertEqual(self.sink.token_ids, [101])

   def test_manifest_lock_source_cannot_register_forged_participant_path(self):
      captured_locks = []
      receive_manifest_locked = self.entry.receive_manifest_locked

      def capture_manifest_lock(locked, **_kwargs):
         captured_locks.append(locked)
         return True

      self.entry.receive_manifest_locked = capture_manifest_lock
      try:
         self.entry.start_distributed_prefill(
            self.request,
            self.sink,
            excluded_placements=frozenset({"node-b-stage-000"}),
         )
      finally:
         self.entry.receive_manifest_locked = receive_manifest_locked

      self.assertEqual(self.entry.request_status(self.request.request_id), "PREFILL")
      self.assertEqual(len(captured_locks), 1)
      locked = captured_locks[0]
      self.assertEqual(locked.manifest.ordered_hops[-1].placement_id, "node-d-stage-002")
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
      executions_before = len(self.runtimes["node-c"].executed)

      with self.assertRaisesRegex(ValueError, "manifest_lock_rejected"):
         self.mesh.transport_for("node-c").send_manifest_locked(forged)

      result = self.routers["node-c"].receive_hop(forged_header, b"forged")
      self.assertEqual(result.disposition, "REJECTED")
      self.assertEqual(result.reason, "unknown_path")
      self.assertEqual(len(self.runtimes["node-c"].executed), executions_before)
      self.assertEqual(self.entry.request_status(self.request.request_id), "PREFILL")

   def test_failure_report_source_must_own_reported_placement(self):
      self.entry.entry.config = replace(
         self.entry.entry.config,
         maximum_recovery_attempts=0,
      )
      self.entry.start_distributed_prefill(
         self.request,
         self.sink,
         excluded_placements=frozenset({"node-b-stage-000"}),
      )
      original_manifest = self.entry.get_request(self.request.request_id).manifest
      reported_placement = original_manifest.ordered_hops[1].placement_id

      self.mesh.transport_for("node-d").send_failure_report(
         FailureReport(
            request_id=self.request.request_id,
            path_id=original_manifest.path_id,
            path_attempt=original_manifest.path_attempt,
            token_index=0,
            scope="PLACEMENT",
            reason="forged_peer_origin",
            placement_id=reported_placement,
            node_id="node-d",
         )
      )

      self.assertEqual(
         self.entry.get_request(self.request.request_id).manifest,
         original_manifest,
      )
      self.assertEqual(self.entry.request_status(self.request.request_id), "DECODING")
      self.assertEqual(self.entry.entry.runtime.cancel_calls, [])

   def test_failure_report_source_must_be_endpoint_of_reported_edge(self):
      self.entry.entry.config = replace(
         self.entry.entry.config,
         maximum_recovery_attempts=0,
      )
      self.entry.start_distributed_prefill(
         self.request,
         self.sink,
         excluded_placements=frozenset({"node-b-stage-000"}),
      )
      record = self.entry.get_request(self.request.request_id)
      original_manifest = record.manifest
      first_pair = tuple(
         hop.placement_id for hop in original_manifest.ordered_hops[:2]
      )
      reported_edge = next(
         edge
         for edge in record.graph.edges
         if (edge.from_placement_id, edge.to_placement_id) == first_pair
      )

      self.mesh.transport_for("node-d").send_failure_report(
         FailureReport(
            request_id=self.request.request_id,
            path_id=original_manifest.path_id,
            path_attempt=original_manifest.path_attempt,
            token_index=0,
            scope="EDGE",
            reason="forged_unrelated_edge_origin",
            placement_id=original_manifest.ordered_hops[-1].placement_id,
            edge_id=reported_edge.edge_id,
            node_id="node-d",
         )
      )

      self.assertEqual(
         self.entry.get_request(self.request.request_id).manifest,
         original_manifest,
      )
      self.assertEqual(self.entry.request_status(self.request.request_id), "DECODING")
      self.assertEqual(self.entry.entry.runtime.cancel_calls, [])

   def test_token_event_source_must_own_final_locked_hop(self):
      self.entry.start_distributed_prefill(
         self.request,
         self.sink,
         excluded_placements=frozenset({"node-b-stage-000"}),
      )
      record = self.entry.get_request(self.request.request_id)
      self.assertEqual(record.manifest.ordered_hops[-1].placement_id, "node-d-stage-002")

      self.mesh.transport_for("node-c").send_token_event(
         TokenEvent(
            request_id=self.request.request_id,
            path_id=record.manifest.path_id,
            path_attempt=record.manifest.path_attempt,
            token_index=0,
            token_id=999,
            sampling_counter=1,
         )
      )

      self.assertEqual(self.sink.token_ids, [])
      self.assertEqual(record.generated_token_ids, [])
      self.assertEqual(self.entry.request_status(self.request.request_id), "DECODING")

   def test_prefill_completion_source_must_own_final_locked_hop(self):
      self.entry.entry.config = replace(
         self.entry.entry.config,
         prefill_chunk_size_tokens=2,
      )
      request = replace(
         self.request,
         request_id="request-prefill-completion-origin",
         prompt_token_ids=(11, 12, 13, 14),
      )
      self.mesh.defer_prefill_chunk_completions = True
      self.entry.start_distributed_prefill(
         request,
         self.sink,
         excluded_placements=frozenset({"node-b-stage-000"}),
      )
      record = self.entry.get_request(request.request_id)
      self.assertEqual(record.status, "LOCKED")
      self.assertEqual(record.completed_prefill_chunks, 1)
      _, genuine_event = self.mesh.deferred_prefill_chunk_completions[0]
      self.assertIsInstance(genuine_event, PrefillChunkCompleted)

      self.mesh.defer_prefill_chunk_completions = False
      self.mesh.transport_for("node-c").send_prefill_chunk_completed(genuine_event)

      self.assertEqual(record.status, "LOCKED")
      self.assertEqual(record.completed_prefill_chunks, 1)


if __name__ == "__main__":
   unittest.main()
