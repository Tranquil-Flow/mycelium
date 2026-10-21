# SPDX-License-Identifier: AGPL-3.0-only
"""A14 class D, round 2: prefill stalls that happen *upstream* of the bounded send.

A14 r1 (1112a179, forced relay): node-0 confirmed one 32 KiB frame, dispatched
one reply, and then nothing moved for 364 s.  Neither node printed a
``MYCELIUM-TRANSPORT-STALL`` line, so no prefill frame was ever awaited by
``_send_confirmed_recovering`` for a whole stall window.  These tests pin the
paths through which a prefill can stall without that bounded send ever being
reached, or without a reply ever being produced:

* c1 - a prefill frame waits on ``_send_operation_lock`` behind an unrelated
  long send.  That wait was bounded only by the frame deadline (up to ~716 s),
  not by the stall window, and it was invisible.
* c2a - the first generated token (``TokenEvent`` index 0) is sent inside the
  final stage's prefill dispatch, synchronously on its dispatcher, as part of
  the prefill completion the entry waits for.  It had no stall window: one
  attempt for the whole delivery deadline, no resend, no stall line.
* c2b - a ``ProgressivePrefillMessage`` whose dispatch raised was recorded only
  on the receiving node; the sending node was never told.
* c2c - the dispatcher's own failure report send was unguarded: if it raised,
  the dispatcher thread died silently and the node stopped receiving.

The wire is the in-memory sidecar model of ``test_iroh_prefill_stall_recovery``
(confirmed send returns only after the receiver acknowledged dispatch; it
never de-duplicates) plus a selective hold for chosen frames.
"""

from __future__ import annotations

from dataclasses import replace
import json
import queue
import threading
import time
from typing import Any

import pytest

from mycelium_router.contracts import (
    FailureReport,
    HopHeader,
    ProgressivePrefillContext,
    TokenEvent,
)
from mycelium_router.transports.iroh import IrohTransportError
from mycelium_router.wire import decode_frame, encode_frame, encode_progressive_prefill
from test_iroh_prefill_stall_recovery import (  # noqa: F401 - fixture re-export
    _Wire,
    _chunk_frame,
    _close_quiesced,
    _scoped_failures,
    _transport,
    _wait_for,
    scaled_policy,
)
from test_router_iroh_integration import _locked_route


class _HoldingWire(_Wire):
    """``_Wire`` that keeps chosen frames from crossing until released."""

    def __init__(self) -> None:
        super().__init__()
        self.hold: Any = None

    def _carry(self, item: Any, receiver: str, attempt_index: int) -> None:
        with self.lock:
            while (
                self.hold is not None
                and self.hold(item.frame)
                and not item.cancelled
            ):
                self.lock.wait(0.01)
        super()._carry(item, receiver, attempt_index)

    def release(self) -> None:
        with self.lock:
            self.hold = None
            self.lock.notify_all()


class _Router:
    """Records every Router-level application; optional failures by kind."""

    def __init__(self) -> None:
        self.hops: list[HopHeader] = []
        self.tokens: list[TokenEvent] = []
        self.failures: list[FailureReport] = []
        self.fail_hop_index: int | None = None
        self.fail_progressive = False

    def receive_hop(self, header, payload, *, source_node_id=None):
        del payload, source_node_id
        if self.fail_hop_index is not None and header.token_index == self.fail_hop_index:
            raise RuntimeError("stage runtime failed")
        self.hops.append(header)
        return True

    def receive_token_event(self, event, *, source_node_id=None):
        del source_node_id
        self.tokens.append(event)
        return True

    def receive_failure_report(self, report, *, source_node_id=None):
        del source_node_id
        self.failures.append(report)
        return True

    def receive_progressive_prefill(self, header, context, **_kwargs):
        del header, context
        if self.fail_progressive:
            raise RuntimeError("final stage dispatch failed after the manifest lock")
        return None


def _token_frame(*, token_index: int, request_id: str = "request-a14") -> bytes:
    return encode_frame(
        TokenEvent(
            request_id=request_id,
            path_id="path-a14" if request_id == "request-a14" else f"path-{request_id}",
            path_attempt=0,
            token_index=token_index,
            token_id=11,
            sampling_counter=token_index + 1,
        )
    )


def _is_token_event(frame: bytes) -> bool:
    return isinstance(decode_frame(frame).message, TokenEvent)


def _pair(wire: _Wire, *, delivery_timeout_seconds: float = 6.0):
    sender = _transport(wire, "node-a", delivery_timeout_seconds=delivery_timeout_seconds)
    receiver = _transport(wire, "node-b", delivery_timeout_seconds=delivery_timeout_seconds)
    sender_router, receiver_router = _Router(), _Router()
    sender.bind_router(sender_router)
    receiver.bind_router(receiver_router)
    events: list[tuple[str, dict[str, Any]]] = []
    sender.stall_event_observer = lambda event, fields: events.append(
        (event, dict(fields))
    )
    sender.start()
    receiver.start()
    return sender, receiver, sender_router, receiver_router, events


def _hold_send_lock_with_a_decode_token(sender: Any, wire: _HoldingWire):
    """Start a non-prefill send that occupies the shared data client."""

    wire.hold = _is_token_event
    outcome: dict[str, Any] = {}

    def run() -> None:
        try:
            sender.send_router_frame(
                _token_frame(token_index=3, request_id="request-decode"),
                destination_node_id="node-b",
            )
            outcome["code"] = "delivered"
        except IrohTransportError as error:
            outcome["code"] = error.code

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    assert _wait_for(lambda: sender._send_operation_lock.locked(), 2.0)
    return thread, outcome


# -- c1: a prefill frame queued behind an unrelated long send ------------------


def test_prefill_chunk_behind_a_long_non_prefill_send_is_bounded_by_the_stall_window(
    scaled_policy: Any,
) -> None:
    """Waiting for the shared data client is a stall like any other.

    The holder is a decode-time token frame that stops crossing the link; it
    keeps its single attempt for the whole 6 s delivery deadline.  The prefill
    chunk queued behind it must not wait for that deadline: every stall window
    it spends waiting for the client is a stalled window (with a stall line
    naming the holder), and after ``max_resends + 1`` windows it fails as
    ``prefill_transport_stalled`` - the same bound as a stalled attempt.
    """

    wire = _HoldingWire()
    sender, receiver, _, receiver_router, events = _pair(wire)
    try:
        sender.watch_prefill("request-a14", deadline_monotonic=time.monotonic() + 30.0)
        holder, holder_outcome = _hold_send_lock_with_a_decode_token(sender, wire)
        started = time.monotonic()

        with pytest.raises(IrohTransportError) as caught:
            sender.send_router_frame(
                _chunk_frame(chunk_index=1), destination_node_id="node-b"
            )
        elapsed = time.monotonic() - started

        windows = scaled_policy.max_resends + 1
        bound = windows * scaled_policy.stall_floor_seconds
        assert caught.value.code == "prefill_transport_stalled"
        assert elapsed < bound + 1.0, elapsed
        assert elapsed < sender.delivery_timeout_seconds / 2, elapsed
        lock_stalls = [fields for event, fields in events if event == "prefill_stall_send_lock"]
        assert len(lock_stalls) == scaled_policy.max_resends, events
        assert [fields["attempts"] for fields in lock_stalls] == list(
            range(1, scaled_policy.max_resends + 1)
        )
        assert {fields["holder_kind"] for fields in lock_stalls} == {"TokenEvent"}
        assert all(
            fields["holder_age_ms"] >= fields["stall_window_ms"] for fields in lock_stalls
        )
        assert "prefill_stall_exhausted" in [event for event, _ in events]
        progress = sender.prefill_progress("request-a14")
        assert progress["failure_code"] == "prefill_transport_stalled"
        assert progress["send_lock_stalls"] == scaled_policy.max_resends
        assert set(_scoped_failures(sender, "request-a14")) == {"prefill_transport_stalled"}
        # The chunk never reached the shared client, so nothing was sent for it.
        assert len([a for a in wire.attempts if a[0] == "node-a"]) == 1
        assert receiver_router.hops == []
        # The holder keeps the single-attempt contract of a non-prefill frame.
        assert holder.is_alive()
    finally:
        wire.release()
        _close_quiesced(sender, receiver)


def test_prefill_chunk_behind_a_short_send_waits_and_is_delivered_without_a_stall(
    scaled_policy: Any,
) -> None:
    """Serialising behind a send shorter than one window is not a stall."""

    wire = _HoldingWire()
    sender, receiver, _, receiver_router, events = _pair(wire)
    try:
        holder, holder_outcome = _hold_send_lock_with_a_decode_token(sender, wire)
        threading.Timer(scaled_policy.stall_floor_seconds / 3, wire.release).start()

        sender.send_router_frame(_chunk_frame(chunk_index=1), destination_node_id="node-b")

        holder.join(3.0)
        assert holder_outcome == {"code": "delivered"}
        assert [header.token_index for header in receiver_router.hops] == [1]
        assert [event for event, _ in events] == []
        assert len([a for a in wire.attempts if a[0] == "node-a"]) == 2
    finally:
        wire.release()
        _close_quiesced(sender, receiver)


class _HandOffLock:
    """A send lock that changes hands between windows ``hand_offs`` times."""

    def __init__(self, transport: Any, hand_offs: int) -> None:
        self._lock = threading.Lock()
        self.transport = transport
        self.hand_offs = hand_offs
        self.waits: list[float | None] = []

    def acquire(self, blocking: bool = True, timeout: float = -1) -> bool:
        if self.hand_offs > 0:
            self.waits.append(timeout)
            time.sleep(timeout)
            self.hand_offs -= 1
            # Another send took the client during this window.
            with self.transport._state_lock:
                self.transport._send_operation_holder = (
                    "HopHeader:PREFILL_CHUNK",
                    b"\x00" * 16,
                    time.monotonic(),
                )
            return False
        return self._lock.acquire(blocking, timeout)

    def release(self) -> None:
        self._lock.release()

    def locked(self) -> bool:
        return self._lock.locked()


def test_waiting_behind_sends_that_keep_completing_is_not_a_stall(
    scaled_policy: Any,
) -> None:
    """Only a window in which one send held the client throughout counts.

    Here the client changes hands in every window (other sends progress), for
    more windows than the stall budget: the chunk keeps waiting and is then
    delivered, with no stall event.
    """

    wire = _Wire()
    sender, receiver, _, receiver_router, events = _pair(wire)
    try:
        lock = _HandOffLock(sender, hand_offs=scaled_policy.max_resends + 2)
        sender._send_operation_lock = lock  # type: ignore[assignment]

        sender.send_router_frame(_chunk_frame(chunk_index=1), destination_node_id="node-b")

        assert len(lock.waits) == scaled_policy.max_resends + 2
        assert all(
            wait is not None and wait <= scaled_policy.stall_floor_seconds + 1e-6
            for wait in lock.waits
        )
        assert [event for event, _ in events] == []
        assert [header.token_index for header in receiver_router.hops] == [1]
    finally:
        _close_quiesced(sender, receiver)


# -- c2a: the prefill's first token rides the prefill completion ----------------


def test_first_token_event_of_a_prefill_is_stall_detected_and_resent_once_applied(
    scaled_policy: Any,
) -> None:
    """Token 0 is produced inside the final stage's prefill dispatch.

    Its confirmation gates the acknowledgement of the frame the entry sent
    (the entry's prefill progress), so a lost confirmation on a flapping
    relay must be detected within the stall window and resent under the
    same message id - and applied once by the receiver.
    """

    wire = _Wire()
    sender, receiver, _, receiver_router, events = _pair(wire)
    try:
        wire.lose_confirmation_attempts = {0}
        started = time.monotonic()

        receipt = sender.send_router_frame(
            _token_frame(token_index=0), destination_node_id="node-b"
        )

        elapsed = time.monotonic() - started
        attempts = [a for a in wire.attempts if a[0] == "node-a"]
        assert len(attempts) == 2
        assert attempts[0][1] == attempts[1][1] == receipt.message_id
        assert elapsed < sender.delivery_timeout_seconds / 2, elapsed
        assert [event for event, _ in events] == ["prefill_stall_resend"]
        assert _wait_for(lambda: receiver.counter_snapshot()["duplicate_frames"] == 1, 2.0)
        assert [event.token_index for event in receiver_router.tokens] == [0]
    finally:
        _close_quiesced(sender, receiver)


# -- c2b: a failed progressive-prefill dispatch is reported upstream ------------


def _progressive_frame() -> tuple[bytes, Any]:
    locked = _locked_route()
    hops = locked.build.ordered_hops
    header = HopHeader(
        request_id=locked.request_id,
        path_id=locked.path_id,
        path_attempt=locked.path_attempt,
        phase="PREFILL",
        token_index=-1,
        hop_index=1,
        source_placement_id=hops[0].placement_id,
        destination_placement_id=hops[1].placement_id,
        topology_version=locked.build.graph.topology_version,
        idempotency_key="progressive-hop-1",
        prefill_chunk_token_count=8,
    )
    context = ProgressivePrefillContext(
        graph=locked.build.graph,
        request=locked.build.request,
        build=replace(locked.build, ordered_hops=hops[:2]),
        payload=b"\x02" * 64,
    )
    return encode_progressive_prefill(header, context), header


def test_failed_progressive_prefill_dispatch_is_reported_to_the_sending_node(
    scaled_policy: Any,
) -> None:
    """The receiving node must not keep a prefill failure to itself.

    A14's chunk 0 is a ``ProgressivePrefillMessage``.  When the final stage's
    dispatch raised after the manifest lock (for example while sending the
    first token), the frame was acknowledged and the failure recorded only on
    the final node: the entry saw silence until its watchdog.  A hop failure
    already produces a ``FailureReport`` to the sender; a progressive prefill
    failure must too.
    """

    del scaled_policy
    wire = _Wire()
    sender, receiver, sender_router, receiver_router, _ = _pair(wire)
    try:
        receiver_router.fail_progressive = True
        frame, header = _progressive_frame()

        sender.send_router_frame(frame, destination_node_id="node-b")

        assert _wait_for(lambda: bool(sender_router.failures), 3.0)
        (report,) = sender_router.failures
        assert (report.request_id, report.path_id, report.path_attempt) == (
            header.request_id,
            header.path_id,
            header.path_attempt,
        )
        assert report.token_index == -1
        assert report.placement_id == header.destination_placement_id
        assert report.node_id == "node-b"
        assert report.reason == "router_dispatch_failed"
    finally:
        _close_quiesced(sender, receiver)


# -- c2c: the dispatcher survives a failure report it cannot send ----------------


def test_dispatcher_survives_a_failure_report_that_cannot_be_sent(
    scaled_policy: Any,
) -> None:
    """A failed hop whose failure report also fails must not kill the dispatcher.

    The report is handed to the ordered forward worker from inside the
    dispatcher's exception handler, with no guard.  On a degraded link the
    forward worker sits in stalled sends for up to ``3 x S`` and its bounded
    queue fills; the hand-off then raises ``forward_queue_full``, that error
    escaped ``_dispatch_loop``, the thread ended without a fatal error, the
    frame stayed in flight, and the receive loop waited on it for ever - the
    node silently stopped receiving.
    """

    del scaled_policy
    wire = _Wire()
    sender, receiver, _, receiver_router, _ = _pair(wire)
    original_put = receiver._forward_queue.put_nowait

    def full_for_failure_reports(item: Any) -> None:
        if isinstance(decode_frame(item[1]).message, FailureReport):
            raise queue.Full
        original_put(item)

    receiver._forward_queue.put_nowait = full_for_failure_reports  # type: ignore[method-assign]
    try:
        receiver_router.fail_hop_index = 1
        try:
            sender.send_router_frame(_chunk_frame(chunk_index=1), destination_node_id="node-b")
        except IrohTransportError:
            pass

        sender.send_router_frame(_chunk_frame(chunk_index=2), destination_node_id="node-b")

        assert [header.token_index for header in receiver_router.hops] == [2]
        assert receiver._dispatcher_thread is not None
        assert receiver._dispatcher_thread.is_alive()
        assert receiver.fatal_error is None
        assert "router_dispatch_failed" in _scoped_failures(receiver, "request-a14")
    finally:
        _close_quiesced(sender, receiver)


# -- diagnosability: the transport state an evidence bundle carries -------------


def test_diagnostic_state_names_the_send_lock_holder_pending_sends_and_inbound_kinds(
    scaled_policy: Any,
) -> None:
    del scaled_policy
    wire = _HoldingWire()
    sender, receiver, _, _, _ = _pair(wire)
    try:
        # node-b -> node-a: one inbound prefill hop on node-a.
        receiver.send_router_frame(_chunk_frame(chunk_index=0), destination_node_id="node-a")
        _hold_send_lock_with_a_decode_token(sender, wire)
        time.sleep(0.05)

        state = sender.diagnostic_state()

        lock = state["send_operation"]
        assert lock["held"] is True
        assert lock["holder_kind"] == "TokenEvent"
        assert lock["holder_age_ms"] >= 40
        assert isinstance(lock["holder_message"], str) and len(lock["holder_message"]) == 8
        pending = [entry for entry in state["pending_sends"] if entry["kind"] == "TokenEvent"]
        assert len(pending) == 1
        assert pending[0]["message"] == lock["holder_message"]
        assert pending[0]["peer"] == "node-b"
        assert state["dispatcher"]["phase"] == "idle"
        inbound = state["peers"]["node-b"]["inbound"]
        assert [entry["kind"] for entry in inbound] == ["HopHeader:PREFILL_CHUNK"]
        assert inbound[0]["outcome"] == "dispatched"
        outbound = state["peers"]["node-b"]["outbound"]
        assert outbound == [] or all(entry["kind"] for entry in outbound)
        rendered = json.dumps(state, sort_keys=True)
        assert "endpoint-" not in rendered
        assert "127.0.0.1" not in rendered
    finally:
        wire.release()
        _close_quiesced(sender, receiver)
