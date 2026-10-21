# SPDX-License-Identifier: AGPL-3.0-only
"""A7: fence the failed attempt's transport forwards at cutover.

A source killed while a decode hop to it is in flight leaves the entry's
deferred forward blocked until the iroh delivery deadline (up to 240 s in the
product path).  At cutover the entry fences that exact attempt: queued and
in-flight forwards are dropped or interrupted, pending deliveries are cancelled,
and the interruption is never reported as a new failure, so the entry's request
cleanup receipt can complete within its budget.
"""

from __future__ import annotations

import threading
from pathlib import Path

from mycelium_router.contracts import FailureReport, PathCancellation
from mycelium_router.wire import encode_frame
from test_router_iroh_transport import _Hub, _transport

from tests.a7_acceptance.test_entry_cutover import _Harness, _reference_tokens


def test_cutover_fences_the_failed_attempt_once(tmp_path: Path) -> None:
   harness = _Harness(tmp_path)
   fenced: list[tuple[str, str, int]] = []
   harness.controller.fence_old_path = lambda *scope: fenced.append(scope)
   request_id = "request:a7-fence"
   old_path = harness.manifest(request_id)

   sink = harness.run(request_id, kill_after=2)

   record = harness.entry.entry.get_request(request_id)
   assert record.kv_successor_cutover["outcome"] == "fenced_kv_successor"
   assert sink.token_ids == _reference_tokens(harness.case)
   assert fenced == [(request_id, old_path.path_id, old_path.path_attempt)]
   assert harness.controller.snapshot(request_id)["old_path_fenced"] is True


def test_fallback_fences_the_failed_attempt_too(tmp_path: Path) -> None:
   harness = _Harness(tmp_path, standby_dtype="float16")
   fenced: list[tuple[str, str, int]] = []
   harness.controller.fence_old_path = lambda *scope: fenced.append(scope)
   request_id = "request:a7-fence-fallback"
   old_path = harness.manifest(request_id)

   harness.run(request_id, kill_after=2)

   assert harness.entry.entry.get_request(request_id).kv_successor_cutover["outcome"] == "full_context_replay"
   assert fenced == [(request_id, old_path.path_id, old_path.path_attempt)]


def test_a_failing_fence_never_blocks_the_cutover(tmp_path: Path) -> None:
   harness = _Harness(tmp_path)

   def broken(*_scope):
      raise RuntimeError("sidecar_unavailable")

   harness.controller.fence_old_path = broken
   request_id = "request:a7-fence-broken"
   sink = harness.run(request_id, kill_after=2)
   assert harness.entry.entry.get_request(request_id).kv_successor_cutover["outcome"] == "fenced_kv_successor"
   assert sink.token_ids == _reference_tokens(harness.case)


def test_fence_interrupts_a_forward_blocked_on_the_dead_source_without_a_new_failure() -> None:
   hub = _Hub()
   hub.block_confirmed_send = True
   transport = _transport(hub, delivery_timeout_seconds=30.0)

   class _Router:
      def receive_path_cancellation(self, *_args, **_kwargs):
         return True

   transport.bind_router(_Router())
   observed: list[str] = []
   transport.forward_failure_observer = lambda _header, _destination, code: observed.append(code)
   cancellation = PathCancellation("request-a", "path-old", 0, 3)
   transport.remember_entry(cancellation.request_id, "local-node")
   with transport._state_lock:
      transport._participant_nodes_by_path[cancellation.path_id] = frozenset({"local-node", "peer-node"})
   transport.start()
   report = FailureReport(
      request_id="request-a",
      path_id="path-old",
      path_attempt=0,
      token_index=2,
      scope="PATH",
      reason="probe",
   )
   scope = ("request-a", "path-old", 0)
   other = ("request-a", "path-successor", 1)
   transport._forward_queue.put_nowait(("peer-node", encode_frame(report), *scope))
   transport._forward_queue.put_nowait(("peer-node", encode_frame(report), *scope))
   with transport._state_lock:
      transport._forward_scopes[scope] = 2
      worker = threading.Thread(target=transport._forward_loop, daemon=True)
      transport._forward_thread = worker
      worker.start()
   try:
      assert hub.confirmed_send_entered.wait(timeout=1.0)
      blocked_client = transport._forward_client

      assert transport.fence_path_forwards(*scope) is True
      worker.join(timeout=2.0)

      assert not worker.is_alive(), "blocked forward was not interrupted"
      assert blocked_client is not None and blocked_client.connected is False
      assert scope not in transport._forward_scopes
      assert transport._forward_queue.empty()
      assert observed == []  # a fenced scope is not a new failure
      assert transport.fatal_error is None
      state = transport.cancellation_cleanup_state(*scope)
      assert state["pending_delivery_count"] == 0
      assert state["forward_count"] == 0
      # Other attempts stay open: the successor path can still forward.
      assert other not in transport._cancelled_forward_scopes
   finally:
      hub.block_confirmed_send = False
      transport.close()


def test_fence_rejects_a_malformed_scope() -> None:
   transport = _transport(_Hub())
   try:
      for scope in (("", "p", 0), ("r", "", 0), ("r", "p", -1), ("r", "p", True)):
         try:
            transport.fence_path_forwards(*scope)
         except ValueError as error:
            assert str(error) == "invalid_forward_fence_scope"
         else:
            raise AssertionError(scope)
      # No router bound yet: nothing to fence, and nothing is recorded.
      assert transport.fence_path_forwards("r", "p", 0) is False
   finally:
      transport.close()


def test_cutover_cancels_the_old_path_at_every_participant_relay(tmp_path: Path) -> None:
   """The successor has a new path id; the old one must not stay registered
   at the participants' relays until idle expiry."""
   harness = _Harness(tmp_path)
   request_id = "request:a7-old-path-cancelled"
   old_path = harness.manifest(request_id)
   sent: list[PathCancellation] = []
   entry_transport = harness.routers["node-a"].entry.transport
   original = entry_transport.send_path_cancellation

   def recording(cancellation):
      sent.append(cancellation)
      return original(cancellation)

   entry_transport.send_path_cancellation = recording
   sink = harness.run(request_id, kill_after=2)

   record = harness.entry.entry.get_request(request_id)
   assert record.kv_successor_cutover["outcome"] == "fenced_kv_successor"
   assert sink.token_ids == _reference_tokens(harness.case)
   assert any(c.path_id == old_path.path_id and c.path_attempt == old_path.path_attempt for c in sent)
   for node_id, router in harness.routers.items():
      assert old_path.path_id not in router.relay._paths, node_id
