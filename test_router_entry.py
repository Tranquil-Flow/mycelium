"""Focused tests for explicit deployment snapshots at Router admission."""

from dataclasses import replace

import pytest

from mycelium_router.contracts import RouterConfig
from mycelium_router.fakes import (
   FakeCapacityPort,
   FakeDeviceStateProvider,
   FakeRuntimePort,
   FakeTransportPort,
   InMemoryClientSink,
   ManualClock,
   SequenceIdSource,
)
from mycelium_router.live_ports import PublishedTopologyProvider
from mycelium_router.router import Router
from test_router_contracts import graph_fixture
from test_router_policy import request_fixture, state_table


class CountingTopologyProvider:
   def __init__(self, graph):
      self.graph = graph
      self.snapshot_calls = 0

   def snapshot(self):
      self.snapshot_calls += 1
      return self.graph


class CountingPublishedTopologyProvider(PublishedTopologyProvider):
   def __init__(self, graph):
      super().__init__(graph)
      self.snapshot_calls = 0

   def snapshot(self):
      self.snapshot_calls += 1
      return super().snapshot()


def successor_with_alternative(graph, *, stage_index):
   stage = graph.stages[stage_index]
   selected = stage.placements[-1]
   alternative = replace(
      selected,
      placement_id=f"000-successor-{stage.stage_id}",
      assignment_id=f"successor-assignment-{stage.stage_id}",
      load_proof_digest=f"sha256:successor-load-proof-{stage.stage_id}",
   )
   selected_id = selected.placement_id
   alternative_id = alternative.placement_id
   edges = list(graph.edges)
   for edge in graph.edges:
      if edge.to_placement_id == selected_id:
         edges.append(
            replace(
               edge,
               edge_id=f"successor-in-{edge.edge_id}",
               to_placement_id=alternative_id,
            )
         )
      if edge.from_placement_id == selected_id:
         edges.append(
            replace(
               edge,
               edge_id=f"successor-out-{edge.edge_id}",
               from_placement_id=alternative_id,
            )
         )
   loopbacks = list(graph.loopback_edges)
   for edge in graph.loopback_edges:
      if edge.to_placement_id == selected_id:
         loopbacks.append(
            replace(
               edge,
               edge_id=f"successor-in-{edge.edge_id}",
               to_placement_id=alternative_id,
            )
         )
      if edge.from_placement_id == selected_id:
         loopbacks.append(
            replace(
               edge,
               edge_id=f"successor-out-{edge.edge_id}",
               from_placement_id=alternative_id,
            )
         )
   successor = replace(
      graph,
      topology_version=graph.topology_version + 1,
      stages=(
         *graph.stages[:stage_index],
         replace(stage, placements=(alternative, *stage.placements)),
         *graph.stages[stage_index + 1 :],
      ),
      edges=tuple(edges),
      loopback_edges=tuple(loopbacks),
   )
   return successor, alternative_id


def single_route_graph():
   graph = graph_fixture()
   selected = (
      "node-a-stage-000",
      "node-c-stage-001",
      "node-a-stage-002",
   )
   stages = tuple(
      replace(
         stage,
         placements=tuple(
            placement
            for placement in stage.placements
            if placement.placement_id == placement_id
         ),
      )
      for stage, placement_id in zip(graph.stages, selected)
   )
   return replace(
      graph,
      stages=stages,
      edges=tuple(
         edge
         for edge in graph.edges
         if (edge.from_placement_id, edge.to_placement_id)
         in set(zip(selected, selected[1:]))
      ),
      loopback_edges=tuple(
         edge
         for edge in graph.loopback_edges
         if (
            edge.from_placement_id,
            edge.to_placement_id,
         )
         == (selected[-1], selected[0])
      ),
   )


def router_stack(topology):
   clock = ManualClock()
   capacity = FakeCapacityPort(clock=clock)
   runtime = FakeRuntimePort(token_base=100)
   transport = FakeTransportPort()
   router = Router(
      node_id="entry-node",
      topology=topology,
      device_states=FakeDeviceStateProvider(state_table()),
      capacity=capacity,
      runtime=runtime,
      transport=transport,
      clock=clock,
      id_source=SequenceIdSource(),
      config=RouterConfig(),
   )
   return router, capacity, runtime, transport


def test_pinned_graph_drives_admission_while_default_caller_still_snapshots():
   original = graph_fixture()
   successor, successor_id = successor_with_alternative(
      original,
      stage_index=0,
   )
   topology = CountingTopologyProvider(successor)
   router, capacity, _runtime, _transport = router_stack(topology)

   pinned_request = request_fixture(request_id="pinned-entry-request")
   router.admit(
      pinned_request,
      InMemoryClientSink(),
      pinned_deployment=original,
   )
   pinned = router.get_request(pinned_request.request_id)

   assert topology.snapshot_calls == 0
   assert pinned.graph is original
   assert pinned.manifest.topology_version == original.topology_version
   assert successor_id not in {
      hop.placement_id for hop in pinned.manifest.ordered_hops
   }

   default_request = request_fixture(request_id="default-entry-request")
   router.admit(default_request, InMemoryClientSink())
   default = router.get_request(default_request.request_id)

   assert topology.snapshot_calls == 1
   assert default.graph is successor
   assert default.manifest.topology_version == successor.topology_version
   assert successor_id in {
      hop.placement_id for hop in default.manifest.ordered_hops
   }
   assert successor_id in {
      item.placement_id for item in capacity.requests
      if item.request_id == default_request.request_id
   }


def test_malformed_pinned_graph_rejects_before_build_or_path_side_effects(
   monkeypatch,
):
   topology = CountingTopologyProvider(graph_fixture())
   router, capacity, runtime, transport = router_stack(topology)
   builder_calls = []

   def forbidden_builder(*_args, **_kwargs):
      builder_calls.append(True)
      raise AssertionError("builder must not run")

   monkeypatch.setattr(router.entry.builder, "start", forbidden_builder)

   with pytest.raises(TypeError, match="invalid_pinned_deployment"):
      router.admit(
         request_fixture(request_id="malformed-pinned-entry-request"),
         InMemoryClientSink(),
         pinned_deployment=object(),
      )

   assert topology.snapshot_calls == 0
   assert builder_calls == []
   assert capacity.requests == []
   assert capacity.committed_ids == set()
   assert capacity.release_calls == []
   assert transport.manifest_deltas == []
   assert transport.hops == []
   assert runtime.executed == []
   assert runtime.cancel_calls == []
   assert router.entry._requests == {}
   assert router.entry._pending_prefills == {}


def test_recovery_reuses_record_graph_after_successor_publication():
   original = single_route_graph()
   successor, successor_id = successor_with_alternative(
      original,
      stage_index=1,
   )
   topology = CountingPublishedTopologyProvider(original)
   router, capacity, runtime, transport = router_stack(topology)
   request = request_fixture(
      request_id="pinned-entry-recovery",
      max_new_tokens=2,
      expected_new_tokens=2,
   )
   router.admit(
      request,
      InMemoryClientSink(),
      pinned_deployment=original,
   )
   admitted = router.get_request(request.request_id)
   initial_manifest = admitted.manifest
   failed_placement = initial_manifest.ordered_hops[1].placement_id
   topology.publish(successor)
   runtime.fail_once(
      placement_id=failed_placement,
      phase="DECODE",
      token_index=0,
      scope="PLACEMENT",
   )

   assert router.decode_one(request.request_id) is False

   recovered = router.get_request(request.request_id)
   reserved = tuple(item.placement_id for item in capacity.requests)
   executed = tuple(item.placement_id for item in runtime.executed)
   encoded_path = tuple(
      delta.hop.placement_id for delta in transport.manifest_deltas
   )
   assert topology.snapshot_calls == 0
   assert recovered.status == "FAILED"
   assert recovered.graph is original
   assert recovered.manifest == initial_manifest
   assert successor_id not in reserved
   assert successor_id not in executed
   assert successor_id not in encoded_path
