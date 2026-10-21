# SPDX-License-Identifier: AGPL-3.0-only
"""A14 serve-2 (f9a8d961 r1): a progressing slow prefill must keep its KV lease.

r1 (``--a8-force-relay``, public n0 relays, slow hotspot): chunks 0-4 crossed
at 3-6 s each, then the final node sent a ``FailureReport`` ~1.2 s after it
dispatched chunk 5 and the entry failed ``route_qualify`` ~43 s after the
serve started.  The transport was healthy (no resend, no stall).  The path's
KV reservation lease is issued once at path build as ``now +
reservation_lease_seconds`` (30 s) and every stage runtime refuses an
operation once its clock passes it (``kv_lease_expired``).  Nothing renewed it
while the chunks kept arriving, so a ten-chunk prefill at ~4 s per chunk cannot
finish inside the lease.

These tests drive the real Router (entry + relay), the real NumPy stage
runtime on both nodes and a controlled two-node link on a virtual clock: each
hop that crosses the link costs ``_HOP_SECONDS`` and each completion that
returns costs ``_RETURN_SECONDS`` (A14 r1: 3-6 s per chunk).

* a progressing slow prefill (and its decode) must complete;
* an abandoned path must still expire and have its KV reclaimed;
* only an applied operation of the owning path renews the lease: a replay, a
  refused (mismatched) operation or a different path never does, and one
  renewal is bounded (``now + window``, never cumulative).
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest

from mycelium_router.contracts import RequestContext, RouterConfig
from mycelium_router.fakes import (
   FakeCapacityPort,
   FakeDeviceStateProvider,
   FakeTopologyProvider,
   InMemoryClientSink,
   InProcessMesh,
   ManualClock,
   SequenceIdSource,
)
from mycelium_router.numpy_runtime import NumpyRuntimePort
from mycelium_router.payloads import encode_token_ids
from mycelium_router.router import Router
from test_router_numpy_qwen_kv import _case, _reference
from test_router_numpy_runtime import _work_item
from test_router_policy import state_table


#: 20 prompt tokens in chunks of 2 -> 10 prefill chunks (A14: 9-10 chunks).
_PROMPT = tuple((7 * index + 3) % 29 + 1 for index in range(20))
_CHUNK_TOKENS = 2
_MAX_NEW_TOKENS = 3
#: One activation hop node-a -> node-b over the slow relay.
_HOP_SECONDS = 3.0
#: One completion (``PrefillChunkCompleted`` / token) back to the entry.
_RETURN_SECONDS = 1.0


class _SlowLinkMesh(InProcessMesh):
   """``InProcessMesh`` whose cross-node frames take time on a virtual clock."""

   def __init__(self, clock: ManualClock, *, defer_completions: bool = False) -> None:
      super().__init__()
      self.clock = clock
      # Deferred: the test releases each chunk completion (and so the next
      # chunk) itself; otherwise the whole prefill runs on the virtual clock.
      self.defer_prefill_chunk_completions = defer_completions

   def send_hop(self, source_node_id, header, payload):
      graph = self._graph_by_path.get(header.path_id) or getattr(payload, "graph", None)
      if graph is not None:
         destination = self._placement(graph, header.destination_placement_id).node_id
         if destination != source_node_id:
            self.clock.advance(_HOP_SECONDS)
      super().send_hop(source_node_id, header, payload)

   def send_token_event(self, source_node_id, event):
      self.clock.advance(_RETURN_SECONDS)
      super().send_token_event(source_node_id, event)

   def send_prefill_chunk_completed(self, source_node_id, event):
      if not self.defer_prefill_chunk_completions:
         self.clock.advance(_RETURN_SECONDS)
      super().send_prefill_chunk_completed(source_node_id, event)

   def deliver_next_prefill_chunk_completion(self) -> bool:
      self.clock.advance(_RETURN_SECONDS)
      return super().deliver_next_prefill_chunk_completion()


def _request(request_id: str) -> RequestContext:
   return RequestContext(
      request_id=request_id,
      prompt_token_ids=_PROMPT,
      max_new_tokens=_MAX_NEW_TOKENS,
      expected_new_tokens=_MAX_NEW_TOKENS,
      qos_class="interactive",
      admitted_at=0.0,
      target_ttft_ms=1_000.0,
      target_tpot_ms=100.0,
      target_tokens_per_second=10.0,
      sampling_seed=0,
      generation_config_digest="sha256:" + "c" * 64,
   )


def _route(
   case: Any, *, defer_completions: bool = False, window: float | None = None
) -> tuple[_SlowLinkMesh, ManualClock, dict[str, Router], dict[str, NumpyRuntimePort]]:
   # ``window``: compose like a physical node with a link-scaled lease window
   # (issued leases and runtime renewal); ``None`` is the plain Router.
   scaled: dict[str, Any] = (
      {} if window is None else {"kv_lease_seconds": lambda: window}
   )
   renewal: dict[str, Any] = (
      {} if window is None else {"kv_lease_renewal_seconds": lambda: window}
   )
   clock = ManualClock(0.0)
   mesh = _SlowLinkMesh(clock, defer_completions=defer_completions)
   capacity = FakeCapacityPort(clock=clock)
   states = state_table()
   routers: dict[str, Router] = {}
   ports: dict[str, NumpyRuntimePort] = {}
   for index, assignment in enumerate(case.assignments):
      node_id = assignment["node_id"]
      port = NumpyRuntimePort(
         node_id,
         case.graph,
         {case.graph.stages[index].placements[0].placement_id: case.loaded[index]},
         clock=clock.now,
         decode_mode="stage_local_kv",
         **renewal,
      )
      router = Router(
         node_id=node_id,
         topology=FakeTopologyProvider(case.graph),
         device_states=FakeDeviceStateProvider(states),
         capacity=capacity,
         runtime=port,
         transport=mesh.transport_for(node_id),
         clock=clock,
         id_source=SequenceIdSource(),
         # The product default: reservation_lease_seconds = 30 s.
         config=RouterConfig(prefill_chunk_size_tokens=_CHUNK_TOKENS),
         **scaled,
      )
      mesh.register_router(node_id, router)
      routers[node_id] = router
      ports[node_id] = port
   return mesh, clock, routers, ports


@pytest.fixture(scope="module")
def qwen_case(tmp_path_factory):
   return _case(tmp_path_factory.mktemp("kv-lease-progress"), "qwen2")


def _failure_reasons(mesh: InProcessMesh) -> list[tuple[str, str, int]]:
   return [
      (source, report.reason, report.token_index)
      for source, report in mesh.failure_reports
   ]


def test_a_progressing_slow_prefill_keeps_its_kv_lease_and_completes(qwen_case) -> None:
   """Ten chunks at 4 s each (40 s) outlive a 30 s lease that never renews."""

   mesh, clock, routers, ports = _route(qwen_case)
   entry = routers["node-a"]
   request = _request("request-slow-progress")
   sink = InMemoryClientSink()

   entry.start_distributed_prefill(request, sink)

   assert _failure_reasons(mesh) == []
   assert entry.request_status(request.request_id) == "DECODING"
   # The prefill really outlived the 30 s lease issued at path build.
   assert clock.now() > 30.0
   assert [event.chunk_index for _, event in mesh.prefill_chunk_completions] == list(
      range(1, len(_PROMPT) // _CHUNK_TOKENS)
   )

   while entry.request_status(request.request_id) == "DECODING":
      assert entry.decode_one_distributed(request.request_id)
   assert entry.request_status(request.request_id) == "COMPLETED"
   assert _failure_reasons(mesh) == []
   context = list(_PROMPT)
   expected = []
   for _ in range(_MAX_NEW_TOKENS):
      token = _reference(qwen_case, tuple(context))
      expected.append(token)
      context.append(token)
   assert list(sink.token_ids) == expected
   for port in ports.values():
      assert port.kv_snapshot()["release_counts"].get("lease_expired", 0) == 0


def test_an_abandoned_path_still_expires_and_its_kv_is_reclaimed(qwen_case) -> None:
   """No progress for longer than the window: the KV is released, late work refused."""

   mesh, clock, routers, ports = _route(qwen_case, defer_completions=True)
   entry = routers["node-a"]
   request = _request("request-abandoned")
   entry.start_distributed_prefill(request, InMemoryClientSink())
   for _ in range(3):
      assert mesh.deliver_next_prefill_chunk_completion()
   assert entry.request_status(request.request_id) == "LOCKED"
   final = ports["node-b"]
   assert final.kv_snapshot()["active_state_count"] == 1

   # The entry stops sending (abandoned); nothing renews the lease any more.
   clock.advance(RouterConfig().reservation_lease_seconds + 1.0)
   assert set(final.expire_leases()) | set(ports["node-a"].expire_leases())
   assert final.kv_snapshot()["active_state_count"] == 0
   assert final.kv_snapshot()["release_counts"]["lease_expired"] == 1
   assert ports["node-a"].kv_snapshot()["active_state_count"] == 0

   # A late chunk of the abandoned path is refused, not resurrected.
   assert mesh.deliver_next_prefill_chunk_completion()
   assert ("node-a", "kv_lease_expired") in {
      (source, reason) for source, reason, _ in _failure_reasons(mesh)
   }


# -- the renewal rule on one runtime ------------------------------------------------


class _Clock:
   def __init__(self, now: float) -> None:
      self.now = now

   def __call__(self) -> float:
      return self.now


@pytest.fixture()
def lease_port(qwen_case):
   clock = _Clock(100.0)
   port = NumpyRuntimePort(
      "node-a",
      qwen_case.graph,
      {qwen_case.graph.stages[0].placements[0].placement_id: qwen_case.loaded[0]},
      clock=clock,
      decode_mode="stage_local_kv",
   )
   return port, clock


def _chunk(qwen_case, *, index: int, lease: float, path_id: str = "path:renew", **overrides):
   item = _work_item(
      qwen_case,
      0,
      encode_token_ids(_PROMPT[2 * index : 2 * index + 2]),
      phase="PREFILL_CHUNK",
      token_span=2,
      request_id="request:renew" if path_id == "path:renew" else f"request:{path_id}",
      path_id=path_id,
      token_index=index,
      position=2 * index,
      lease_expires_at=lease,
   )
   return replace(item, **overrides) if overrides else item


def test_each_applied_chunk_renews_the_lease_by_a_bounded_window(lease_port, qwen_case) -> None:
   port, clock = lease_port
   lease = 130.0  # issued 30 s before this test's t=100
   window = RouterConfig().reservation_lease_seconds
   for index in range(4):
      result = port.execute(_chunk(qwen_case, index=index, lease=lease))
      assert result.success, result.failure_reason
      clock.now += 1.0
   # Four chunks at t=100..103 renew to (last applied) + window, not 4 x window.
   clock.now = 103.0 + window - 0.001
   assert port.expire_leases() == ()
   clock.now = 103.0 + window
   assert port.expire_leases() == ("path:renew",)
   assert port.kv_snapshot()["release_counts"]["lease_expired"] == 1


def test_a_chunk_beyond_the_issued_lease_is_accepted_while_progress_continues(
   lease_port, qwen_case
) -> None:
   port, clock = lease_port
   lease = 130.0
   for index in range(8):
      result = port.execute(_chunk(qwen_case, index=index, lease=lease))
      assert result.success, (index, clock.now, result.failure_reason)
      clock.now += 5.0  # 8 chunks over 40 s: past the issued 130.0
   assert clock.now > lease


def test_replays_refusals_and_other_paths_do_not_renew(lease_port, qwen_case) -> None:
   port, clock = lease_port
   lease = 130.0
   first = _chunk(qwen_case, index=0, lease=lease)
   assert port.execute(first).success
   second = _chunk(qwen_case, index=1, lease=lease)
   clock.now = 110.0
   assert port.execute(second).success  # renews to 140.0
   # A replay of an applied chunk (a duplicate resend) is not progress.
   clock.now = 139.0
   assert port.execute(second).success
   # A refused continuation (wrong lease binding / wrong position) is not progress.
   mismatched = _chunk(qwen_case, index=2, lease=lease + 500.0)
   assert port.execute(mismatched).failure_reason == "kv_lease_mismatch"
   misplaced = replace(_chunk(qwen_case, index=2, lease=lease), position=7)
   assert port.execute(misplaced).failure_reason == "kv_position_mismatch"
   # Another path's progress never touches this lease.
   other = _chunk(qwen_case, index=0, lease=500.0, path_id="path:other")
   assert port.execute(other).success
   clock.now = 140.0
   assert "path:renew" in port.expire_leases()
   late = _chunk(qwen_case, index=2, lease=lease)
   assert port.execute(late).failure_reason == "kv_lease_expired"
