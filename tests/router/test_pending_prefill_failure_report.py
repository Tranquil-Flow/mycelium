# SPDX-License-Identifier: AGPL-3.0-only
"""A FailureReport for a progressive prefill that is still pending at the entry.

Before ``ManifestLocked`` reaches the entry, a progressive prefill has no
``RequestRecord`` yet: the entry knows only its ``PendingPrefill`` (the hops it
chose itself, normally hop 0).  A downstream stage can still fail chunk 0 before
the final stage locks the manifest (runtime failure, ``route_extension_failed``,
``manifest_lock_failed:reservation_expired``) and send the entry a
FailureReport.  The entry used to drop that report, so the request sat in
``PREFILL`` until the node's prefill watchdog instead of failing promptly with
the reported reason (kv-lease lane, "found, not fixed").

Here the real relays of the downstream nodes execute chunk 0 and produce the
report; the in-memory transports only carry the frames.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest

from mycelium_router.contracts import FailureReport, RouterConfig
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
from test_router_contracts import graph_fixture
from test_router_policy import request_fixture, state_table

_FAILING_NODE = "node-c"
_FAILING_PLACEMENT = "node-c-stage-001"


class _World:
   """An entry plus the three stage nodes, each with its own transport."""

   def __init__(self) -> None:
      self.graph = graph_fixture()
      self.clock = ManualClock()
      self.capacity = FakeCapacityPort(clock=self.clock)
      self.transports: dict[str, FakeTransportPort] = {}
      self.runtimes: dict[str, FakeRuntimePort] = {}
      self.routers: dict[str, Router] = {}
      states = {
         "node-a": state_table(slow_b_bandwidth=True),
         "node-b": state_table(slow_b_bandwidth=False),
         "node-c": state_table(slow_b_bandwidth=False),
      }
      for node_id, table in states.items():
         self.runtimes[node_id] = FakeRuntimePort()
         self.transports[node_id] = FakeTransportPort()
         self.routers[node_id] = self._router(
            node_id, table, self.runtimes[node_id], self.transports[node_id]
         )
      self.entry_transport = FakeTransportPort()
      self.entry = self._router(
         "entry-node",
         state_table(slow_b_bandwidth=False),
         FakeRuntimePort(),
         self.entry_transport,
      )
      self.node_of = {
         placement.placement_id: placement.node_id
         for stage in self.graph.stages
         for placement in stage.placements
      }

   def _router(self, node_id, table, runtime, transport) -> Router:
      return Router(
         node_id=node_id,
         topology=FakeTopologyProvider(self.graph),
         device_states=FakeDeviceStateProvider(table),
         capacity=self.capacity,
         runtime=runtime,
         transport=transport,
         clock=self.clock,
         id_source=SequenceIdSource(),
         config=RouterConfig(),
      )

   def start(self, request_id: str = "pending-fr") -> str:
      """Entry sends chunk 0; stage 0 forwards it; stage 1 fails it pre-lock."""

      self.runtimes[_FAILING_NODE].fail_once(
         placement_id=_FAILING_PLACEMENT,
         phase="PREFILL",
         token_index=-1,
         scope="PLACEMENT",
      )
      self.request = request_fixture(request_id=request_id)
      self.sink = InMemoryClientSink()
      self.entry.start_distributed_prefill(
         self.request,
         self.sink,
         excluded_placements=frozenset({"node-b-stage-000"}),
      )
      header, context = self.entry_transport.hops[0]
      self.entry_hop = context.build.ordered_hops[0]
      first = self.node_of[header.destination_placement_id]
      assert self.routers[first].receive_progressive_prefill(
         header, context
      ).disposition == "FORWARDED"
      header, context = self.transports[first].hops[-1]
      assert header.destination_placement_id == _FAILING_PLACEMENT
      result = self.routers[_FAILING_NODE].receive_progressive_prefill(
         header, context
      )
      assert result.disposition == "FAILED"
      assert self.transports[_FAILING_NODE].manifest_locks == []
      (self.report,) = self.transports[_FAILING_NODE].failure_reports
      return self.request.request_id


@pytest.fixture
def world() -> _World:
   return _World()


def test_a_pre_lock_failure_report_fails_the_pending_prefill_promptly(world) -> None:
   request_id = world.start()
   report = world.report
   assert (report.token_index, report.scope, report.reason) == (
      -1,
      "PLACEMENT",
      "injected_failure",
   )
   # No ManifestLocked ever reached the entry: the request is still pending.
   assert world.entry.request_status(request_id) == "PREFILL"
   released_before = set(world.capacity.released_ids)

   world.entry.receive_failure_report(report, source_node_id=_FAILING_NODE)

   # Prompt, not at the node's watchdog: the very report ends the request.
   assert world.entry.request_status(request_id) == "FAILED"
   cause = world.entry.entry.last_failure_report(request_id)
   assert cause is not None
   assert cause["applied"] is True
   assert cause["reason"] == "injected_failure"
   assert cause["scope"] == "PLACEMENT"
   assert cause["placement_id"] == _FAILING_PLACEMENT
   assert cause["stage_id"] == "stage-001"
   assert cause["source_node_id"] == _FAILING_NODE
   assert cause["token_index"] == -1
   # The entry's own reservation (hop 0) is released, as for a post-lock
   # terminal failure, and nothing more is dispatched for the request.
   assert world.entry_hop.reservation_id not in released_before
   assert world.entry_hop.reservation_id in world.capacity.released_ids
   assert world.entry.decode_one_distributed(request_id) is False
   hops_sent = len(world.entry_transport.hops)
   # A late duplicate is not applied twice and does not resurrect anything.
   assert world.entry.receive_failure_report(report, source_node_id=_FAILING_NODE) is False
   assert world.entry.request_status(request_id) == "FAILED"
   assert len(world.entry_transport.hops) == hops_sent
   # The request id stays taken (no silent re-admission under the same id).
   with pytest.raises(ValueError, match="duplicate_request_id"):
      world.entry.start_distributed_prefill(world.request, InMemoryClientSink())


def _tampered(world: _World, kind: str) -> tuple[FailureReport, str | None]:
   report = world.report
   source = _FAILING_NODE
   if kind == "other_path":
      report = replace(report, path_id="path-not-this-request")
   elif kind == "other_attempt":
      report = replace(report, path_attempt=report.path_attempt + 1)
   elif kind == "decode_step":
      report = replace(report, token_index=0)
   elif kind == "wrong_origin":
      # node-b hosts placements of this graph, but not the reported one.
      source = "node-b"
   elif kind == "unknown_placement":
      report = replace(report, placement_id="ghost-stage-001")
   elif kind == "not_the_entrys_hop":
      # The entry chose hop 0 itself: a stage-0 report must name that hop.
      report = replace(
         report, placement_id="node-b-stage-000", node_id="node-b"
      )
      source = "node-b"
   elif kind == "device_of_another_node":
      report = replace(report, scope="DEVICE", node_id="node-b")
   elif kind == "edge_off_the_placement":
      report = replace(report, scope="EDGE", edge_id="e-0b-1b")
   else:  # pragma: no cover - test table
      raise AssertionError(kind)
   return report, source


@pytest.mark.parametrize(
   "kind",
   [
      "other_path",
      "other_attempt",
      "decode_step",
      "wrong_origin",
      "unknown_placement",
      "not_the_entrys_hop",
      "device_of_another_node",
      "edge_off_the_placement",
   ],
)
def test_a_mismatched_pre_lock_failure_report_is_ignored_and_logged(
   world, kind
) -> None:
   request_id = world.start()
   report, source = _tampered(world, kind)

   assert world.entry.receive_failure_report(report, source_node_id=source) is False

   assert world.entry.request_status(request_id) == "PREFILL"
   logged = world.entry.entry.last_failure_report(request_id)
   assert logged is not None and logged["applied"] is False
   assert world.entry_hop.reservation_id not in world.capacity.released_ids
   # The rejection did not poison the pending request: the genuine report
   # still applies.
   world.entry.receive_failure_report(world.report, source_node_id=_FAILING_NODE)
   assert world.entry.request_status(request_id) == "FAILED"
   assert world.entry.entry.last_failure_report(request_id)["applied"] is True


def test_a_failure_report_for_an_unknown_request_is_ignored(world) -> None:
   request_id = world.start()
   stranger = replace(world.report, request_id="someone-else")
   assert world.entry.receive_failure_report(
      stranger, source_node_id=_FAILING_NODE
   ) is False
   assert world.entry.entry.last_failure_report("someone-else") is None
   assert world.entry.request_status(request_id) == "PREFILL"


def test_a_late_manifest_locked_after_the_pre_lock_failure_is_refused(world) -> None:
   """A lock that raced the report cannot revive the failed request."""

   request_id = world.start()
   world.entry.receive_failure_report(world.report, source_node_id=_FAILING_NODE)
   assert world.entry.request_status(request_id) == "FAILED"
   # Replay chunk 0 on a fresh path through a healthy stage 1 to obtain a
   # genuine ManifestLocked for this request id, then deliver it late.
   other = _World()
   other.request = world.request
   other.entry.start_distributed_prefill(
      world.request,
      InMemoryClientSink(),
      excluded_placements=frozenset({"node-b-stage-000"}),
   )
   header, context = other.entry_transport.hops[0]
   while True:
      node = other.node_of[header.destination_placement_id]
      result: Any = other.routers[node].receive_progressive_prefill(header, context)
      if result.disposition == "LOCKED":
         locked = result.confirmation
         break
      header, context = other.transports[node].hops[-1]
   assert world.entry.receive_manifest_locked(locked) is False
   assert world.entry.request_status(request_id) == "FAILED"
