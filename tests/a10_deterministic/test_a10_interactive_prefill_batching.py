# SPDX-License-Identifier: AGPL-3.0-only
"""Two interactive prefill hops that meet at a node batch (A10 live finding f7b2).

Live run f7b2 (7B, two stages, collection window 46 ms): two browser requests
accepted 1 ms apart never shared a ``runtime_batch_ref``.  The batch controller
dispatches an interactive prefill at once, so the only place two prefill hops can
pair is node ingress, inside the live collection window.

Failure modes this suite catches (one per test, by name):

  test_interactive_prefill_hops_within_the_window_form_one_batch
      Two registered paths whose prefill hops arrive ``g`` apart (g inside the
      window) executed as two singleton batches.
  test_prefill_hop_waits_for_a_request_being_admitted_inside_its_window
      A request whose infer command is in flight when another's prefill hop is
      queued being left out of the batch.
  test_an_admission_that_never_registers_costs_at_most_the_window /
  test_an_abandoned_admission_releases_the_waiting_hop_at_once
      An announced admission that never registers holding a hop past the window,
      or past the admission's own end.
  test_a_path_unknown_to_the_node_when_a_hop_is_queued_is_a_recorded_split
      A hop waiting for a partner nobody announced.
  test_lone_interactive_prefill_hop_is_not_delayed
      A request with no other registered path paying the collection window.
  test_a_late_prefill_hop_is_never_held_past_the_window
      A hop waiting beyond the window for a partner that never sends.
"""

from __future__ import annotations

import threading
import time

import pytest

from mycelium_router.contracts import HopHeader
from mycelium_router.idempotency import hop_idempotency_key
from mycelium_router.wire import encode_frame
from mycelium_router.payloads import encode_token_ids
from mycelium_router.transports import iroh as iroh_transport
from tests.a10_deterministic.test_a10_live_hop_batching import (
   PROMPT,
   _GateRuntime,
   _locked,
   _nodes,
   _policy,
   _router,
   _start,
   _tokens,
   _transport,
   _wait,
)
from test_router_iroh_transport import _Hub
from test_router_mlx_runtime import runtime_case  # noqa: F401

WINDOW_MS = 46.0
WINDOW_S = WINDOW_MS / 1_000.0


def _prefill_header(case, name) -> HopHeader:
   graph = case.graph
   return HopHeader(
      request_id=f"request-{name}",
      path_id=f"path-{name}",
      path_attempt=0,
      phase="PREFILL_CHUNK",
      token_index=0,
      hop_index=1,
      source_placement_id=graph.stages[0].placements[0].placement_id,
      destination_placement_id=graph.stages[1].placements[0].placement_id,
      topology_version=graph.topology_version,
      idempotency_key=hop_idempotency_key(
         request_id=f"request-{name}",
         path_id=f"path-{name}",
         path_attempt=0,
         phase="PREFILL_CHUNK",
         token_index=0,
         hop_index=1,
      ),
      prefill_chunk_token_count=len(PROMPT),
   )


def _deliver_prefill(case, hub, name, byte):
   hub.deliver(byte * 16, encode_frame(_prefill_header(case, name), encode_token_ids(PROMPT)))


def _batch_sizes(runtime):
   return sorted(len(batch.items) for batch in runtime.executed_batches)


def _shared_batch_ref(runtime):
   """The member refs the relay gave each batch (one ref per executed batch)."""

   return [tuple(sorted(item.request_id for item in batch.items)) for batch in runtime.executed_batches]


class _WindowClock:
   """The collection window's clock, advanced only by the test.

   The dispatcher measures the live collection window against ``time.monotonic`` as seen
   by ``mycelium_router.transports.iroh``.  A gap of ``g`` ms between two hops is a
   statement about that clock: ``g`` of the window has elapsed when the second hop
   arrives.  Driving the gap with ``time.sleep(g)`` on the wall clock instead makes the
   statement a race against the host -- a descheduled test thread overshoots a 40 ms
   sleep past the 46 ms window (it failed three L2 rehearsal attempts under load) and the
   second hop then meets a window that has already closed.  Everything else the module
   reads from ``time`` (``sleep``, ``time``, ...) is the real thing; the window is
   closed only by ``advance``.
   """

   def __init__(self) -> None:
      self._now = time.monotonic()

   def monotonic(self) -> float:
      return self._now

   def advance(self, seconds: float) -> None:
      self._now += seconds

   def __getattr__(self, name):
      return getattr(time, name)


@pytest.mark.parametrize("gap_ms", [0.0, 1.0, 10.0, 40.0])
def test_interactive_prefill_hops_within_the_window_form_one_batch(
   runtime_case,  # noqa: F811 - pytest fixture
   gap_ms,
   monkeypatch,
):
   window_clock = _WindowClock()
   monkeypatch.setattr(iroh_transport, "time", window_clock)
   runtime = _GateRuntime()
   hub, transport, router = _start(runtime_case, runtime, ("a", "b"))
   _policy(router, f"candidate@{int(WINDOW_MS)}")
   assert router.hop_collection_window_seconds() == pytest.approx(WINDOW_S)
   try:
      _deliver_prefill(runtime_case, hub, "a", b"\x01")
      # The window is open once the dispatcher has queued a's hop and armed it; the
      # gap then elapses on the window's own clock, not on the host's.
      _wait(lambda: transport._hop_collection_deadline is not None)
      assert gap_ms / 1_000.0 < WINDOW_S
      window_clock.advance(gap_ms / 1_000.0)
      _deliver_prefill(runtime_case, hub, "b", b"\x02")
      # A candidate hop is acknowledged once the dispatcher has queued it: both hops are
      # queued inside the one window; now it closes.
      _wait(lambda: b"\x02" * 16 in hub.acks)
      window_clock.advance(WINDOW_S)
      _wait(lambda: len(runtime.executed_batches) >= 1)
      time.sleep(WINDOW_S * 2)
      assert _shared_batch_ref(runtime) == [("request-a", "request-b")], _shared_batch_ref(runtime)
      assert transport.fatal_error is None
   finally:
      transport.close()


def test_prefill_hop_waits_for_a_request_being_admitted_inside_its_window(
   runtime_case,  # noqa: F811 - pytest fixture
):
   """Path b is still being admitted when a's prefill hop is queued.

   Documented outcome: the node knows b's infer command is in flight (accepted,
   path not registered yet), so a's hop waits for b for at most the window; b
   registers, its hop arrives, and both execute as one batch.
   """

   runtime = _GateRuntime()
   hub, transport, router = _start(runtime_case, runtime, ("a",))
   _policy(router, f"candidate@{int(WINDOW_MS)}")
   router.relay.begin_admission("request-b")
   try:
      _deliver_prefill(runtime_case, hub, "a", b"\x01")
      time.sleep(0.005)
      assert runtime.executed_batches == []
      hub.deliver(b"\x42" * 16, encode_frame(_locked(runtime_case, "b")))
      _wait(lambda: router.relay._paths.get("path-b") is not None)
      time.sleep(0.005)
      _deliver_prefill(runtime_case, hub, "b", b"\x02")
      _wait(lambda: len(runtime.executed_batches) >= 1)
      time.sleep(WINDOW_S * 2)
      assert _shared_batch_ref(runtime) == [("request-a", "request-b")], _shared_batch_ref(runtime)
   finally:
      transport.close()


def test_an_admission_that_never_registers_costs_at_most_the_window(
   runtime_case,  # noqa: F811 - pytest fixture
):
   runtime = _GateRuntime()
   hub, transport, router = _start(runtime_case, runtime, ("a",))
   _policy(router, f"candidate@{int(WINDOW_MS)}")
   router.relay.begin_admission("request-ghost")
   try:
      queued_at = time.monotonic()
      _deliver_prefill(runtime_case, hub, "a", b"\x01")
      _wait(lambda: len(runtime.executed_batches) >= 1)
      assert time.monotonic() - queued_at < WINDOW_S + 0.1
      assert _batch_sizes(runtime) == [1]
   finally:
      transport.close()


def test_an_abandoned_admission_releases_the_waiting_hop_at_once(
   runtime_case,  # noqa: F811 - pytest fixture
):
   runtime = _GateRuntime()
   hub, transport, router = _start(runtime_case, runtime, ("a",))
   _policy(router, "candidate@200")
   router.relay.begin_admission("request-ghost")
   try:
      queued_at = time.monotonic()
      _deliver_prefill(runtime_case, hub, "a", b"\x01")
      time.sleep(0.05)
      assert runtime.executed_batches == []
      router.relay.end_admission("request-ghost")
      _wait(lambda: len(runtime.executed_batches) >= 1)
      assert time.monotonic() - queued_at < 0.15
      assert _batch_sizes(runtime) == [1]
   finally:
      transport.close()


def test_a_path_unknown_to_the_node_when_a_hop_is_queued_is_a_recorded_split(
   runtime_case,  # noqa: F811 - pytest fixture
):
   """No admission, no registration: nothing says b exists, so a never waits.

   A lone request is never delayed for a partner nobody announced; b's hop,
   arriving after a already executed, is its own batch (the controller records
   ``interactive_prefill_immediate`` for both).
   """

   runtime = _GateRuntime()
   hub, transport, router = _start(runtime_case, runtime, ("a",))
   _policy(router, f"candidate@{int(WINDOW_MS)}")
   try:
      _deliver_prefill(runtime_case, hub, "a", b"\x01")
      _wait(lambda: len(runtime.executed_batches) >= 1)
      hub.deliver(b"\x42" * 16, encode_frame(_locked(runtime_case, "b")))
      _wait(lambda: router.relay._paths.get("path-b") is not None)
      _deliver_prefill(runtime_case, hub, "b", b"\x02")
      _wait(lambda: len(runtime.executed_batches) >= 2)
      assert _batch_sizes(runtime) == [1, 1]
      assert {d.reason for d in router.relay.batch_decisions()} >= {"interactive_prefill_immediate"}
   finally:
      transport.close()


def test_lone_interactive_prefill_hop_is_not_delayed(
   runtime_case,  # noqa: F811 - pytest fixture
):
   runtime = _GateRuntime()
   hub, transport, router = _start(runtime_case, runtime, ("a",))
   _policy(router, f"candidate@{int(WINDOW_MS)}")
   try:
      queued_at = time.monotonic()
      _deliver_prefill(runtime_case, hub, "a", b"\x01")
      _wait(lambda: len(runtime.executed_batches) >= 1)
      assert time.monotonic() - queued_at < WINDOW_S * 0.5
      assert _batch_sizes(runtime) == [1]
   finally:
      transport.close()


def test_a_late_prefill_hop_is_never_held_past_the_window(
   runtime_case,  # noqa: F811 - pytest fixture
):
   """b is registered but its prefill hop only comes after a's window closed."""

   runtime = _GateRuntime()
   hub, transport, router = _start(runtime_case, runtime, ("a", "b"))
   _policy(router, f"candidate@{int(WINDOW_MS)}")
   try:
      queued_at = time.monotonic()
      _deliver_prefill(runtime_case, hub, "a", b"\x01")
      _wait(lambda: len(runtime.executed_batches) >= 1)
      waited = time.monotonic() - queued_at
      assert waited < WINDOW_S + 0.1
      assert _batch_sizes(runtime) == [1]
      _deliver_prefill(runtime_case, hub, "b", b"\x02")
      _wait(lambda: len(runtime.executed_batches) >= 2)
      assert _batch_sizes(runtime) == [1, 1]
   finally:
      transport.close()


def test_a_request_whose_own_hop_is_queued_is_never_its_own_partner(
   runtime_case,  # noqa: F811 - pytest fixture
):
   """The entry's progressive start keeps announcing until it returns, but the
   hop it queued ends the announcement: the request does not wait for itself."""

   runtime = _GateRuntime()
   hub, transport, router = _start(runtime_case, runtime, ("a",))
   _policy(router, "candidate@200")
   router.relay.begin_admission("request-a")
   try:
      queued_at = time.monotonic()
      _deliver_prefill(runtime_case, hub, "a", b"\x01")
      _wait(lambda: len(runtime.executed_batches) >= 1)
      assert time.monotonic() - queued_at < 0.1
      assert _batch_sizes(runtime) == [1]
   finally:
      transport.close()
