# SPDX-License-Identifier: AGPL-3.0-or-later
"""PV5: hop acknowledgement on receipt (RECEIVED) versus after execute (EXECUTED).

Today a hop's sidecar acknowledgement is sent only after the hop has been
dispatched, and under a batch cap of one dispatched means *executed*.  The
receiver takes no further frame until that acknowledgement, and the sender's
ordered forward worker sends nothing else on the stream until it hears it, so a
second request's frame waits for the first request's whole stage execute.

``hop_ack_on_receipt`` (default OFF, ``MYCELIUM_HOP_ACK_ON_RECEIPT=1``) splits the
signal: a hop frame is acknowledged once it is held in the dispatch queue
(RECEIVED, ownership transferred), while everything that relies on the old
acknowledgement keeps waiting for EXECUTED.  ``hop_ack_window`` bounds how many
RECEIVED-but-not-EXECUTED hops a node holds (default 2).

These cases drive the production ``IrohTransport`` dispatcher with an in-memory
sidecar client and a router double that exposes the batch surface under a cap of
one, so every hop blocks inside its own execute until the test releases it.

Failure modes this suite catches (one per test, by name):

  test_default_off_hop_frame_waits_for_the_previous_execute_ack
      Characterises today's serialisation: the next frame is not taken, and
      nothing is acknowledged, until the executing hop is acknowledged.
  test_on_receipt_accepts_the_next_request_while_the_first_executes
      Flag ON still serialising the second request's frame behind the first
      request's execute.
  test_on_receipt_does_not_execute_two_hops_at_once
      Early acknowledgement turning into concurrent execution on a node that has
      one dispatcher (state corruption in a stage runtime).
  test_per_request_hop_order_is_preserved
      A request's hops executing out of delivery order once frames overlap.
  test_in_flight_window_applies_backpressure_when_full
      An unbounded buffer of RECEIVED hops, or a window that never refills.
  test_window_and_flag_validation
      A window outside 1..queue_capacity, or the flag defaulting to ON.
  test_cleanup_state_counts_received_hops_until_executed
      Cleanup / withdraw / drain seeing a RECEIVED-only hop as already finished.
  test_executed_counters_do_not_move_at_receipt
      Dispatch evidence claiming a hop ran when it was only stored.
  test_execute_failure_after_received_reports_the_same_failure_once
      A hop that fails after RECEIVED losing its FailureReport, being
      acknowledged twice, or killing the transport.
  test_replayed_hop_after_received_is_executed_once
      A re-sent hop (A6/A7 recovery) emitting a second token for a key.
  test_non_hop_frames_keep_acknowledge_after_dispatch
      Token events, failure reports and manifests acknowledging on receipt.
  test_only_plain_hop_frames_are_early_acknowledged
      The synchronous prefill lock handshake or a receipt-bearing frame becoming
      asynchronous.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from types import SimpleNamespace

import pytest

from mycelium_router.contracts import FailureReport, HopHeader, TokenEvent
from mycelium_router.idempotency import hop_idempotency_key
from mycelium_router.transports.iroh import IrohTransport, PeerBinding
from mycelium_router.wire import decode_frame, encode_frame
from test_router_iroh_transport import _Hub

SETTLE_SECONDS = 0.25


def _wait(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            raise AssertionError("condition not reached")
        time.sleep(0.005)


def _stays(predicate, seconds=SETTLE_SECONDS) -> bool:
    """True when ``predicate`` holds for the whole interval (a bounded negative)."""

    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if not predicate():
            return False
        time.sleep(0.005)
    return True


def _hop(request: str, token: int = 1, hop_index: int = 1) -> HopHeader:
    return HopHeader(
        request_id=f"req-{request}",
        path_id=f"path-{request}",
        path_attempt=0,
        phase="DECODE",
        token_index=token,
        hop_index=hop_index,
        source_placement_id="slot-0",
        destination_placement_id="slot-1",
        topology_version=1,
        idempotency_key=hop_idempotency_key(
            request_id=f"req-{request}",
            path_id=f"path-{request}",
            path_attempt=0,
            phase="DECODE",
            token_index=token,
            hop_index=hop_index,
        ),
    )


def _mid(name: str) -> bytes:
    return name.encode().ljust(16, b"_")[:16]


class _GatedHopRouter:
    """Batch-surface router under a cap of one: a pumped hop runs until released.

    ``executed`` is the order hops finished, ``running`` the hops inside their
    execute right now, ``emitted`` counts emissions per idempotency key so a hop
    that ran twice is visible.  A key already executed is not run again, as the
    real Router's hop-result cache guarantees.
    """

    def __init__(self) -> None:
        self.queue: deque[tuple[HopHeader, bytes]] = deque()
        self.lock = threading.Lock()
        self.started: dict[str, threading.Event] = {}
        self.release: dict[str, threading.Event] = {}
        self.executed: list[str] = []
        self.running: set[str] = set()
        self.max_concurrent = 0
        self.emitted: dict[str, int] = {}
        self.failing: set[str] = set()
        self.pending_keys: set[str] = set()
        self.token_events: list[TokenEvent] = []
        self.runtime_cap = 1

    def _gate(self, table: dict[str, threading.Event], name: str) -> threading.Event:
        with self.lock:
            return table.setdefault(name, threading.Event())

    def started_event(self, request: str) -> threading.Event:
        return self._gate(self.started, f"req-{request}")

    def release_event(self, request: str) -> threading.Event:
        return self._gate(self.release, f"req-{request}")

    # --- batch surface the transport probes for -------------------------------
    def enqueue_hop(self, header, payload, *, source_node_id=None):
        with self.lock:
            if header.idempotency_key in self.emitted:
                return SimpleNamespace(disposition="COMPLETED", reason=None)
            if header.idempotency_key in self.pending_keys:
                # The real Router answers a replay of a queued hop this way.
                return SimpleNamespace(disposition="QUEUED", reason="duplicate_pending")
            self.pending_keys.add(header.idempotency_key)
            self.queue.append((header, payload))
        return SimpleNamespace(disposition="QUEUED", reason=None)

    def pump_ready_batches(self, *, on_item_error=None):
        while True:
            with self.lock:
                if not self.queue:
                    return ()
                header, _payload = self.queue.popleft()
            name = header.request_id
            with self.lock:
                self.running.add(name)
                self.max_concurrent = max(self.max_concurrent, len(self.running))
            self._gate(self.started, name).set()
            assert self._gate(self.release, name).wait(10), "test never released the hop"
            with self.lock:
                self.running.discard(name)
                self.pending_keys.discard(header.idempotency_key)
            if header.request_id in self.failing:
                if on_item_error is not None:
                    on_item_error(header, "peer-node", RuntimeError("stage failed"))
                continue
            with self.lock:
                self.executed.append(f"{header.request_id}#{header.token_index}")
                self.emitted[header.idempotency_key] = (
                    self.emitted.get(header.idempotency_key, 0) + 1
                )

    def pending_batch_hops(self):
        with self.lock:
            return len(self.queue)

    def runtime_batch_cap(self):
        return self.runtime_cap

    def hop_collection_window_seconds(self):
        return 0.0

    # --- other sinks -----------------------------------------------------------
    def receive_token_event(self, event, *, source_node_id=None):
        self.token_events.append(event)
        return True


def _transport(hub: _Hub, **overrides) -> IrohTransport:
    options = dict(
        node_id="local-node",
        socket_path="/unused",
        bootstrap_secret=b"s" * 32,
        peer=PeerBinding(
            node_id="peer-node",
            endpoint_id="peer-endpoint",
            endpoint_addr={"id": "peer-endpoint", "addrs": ["127.0.0.1:1"]},
            generation=7,
        ),
        expected_endpoint_id="local-endpoint",
        queue_capacity=8,
        delivery_timeout_seconds=2.0,
        poll_interval_seconds=0.01,
        client_factory=hub.client,
    )
    options.update(overrides)
    return IrohTransport(**options)


class _Node:
    """One started transport with a gated router, closed (and released) on exit."""

    def __init__(self, **overrides) -> None:
        self.hub = _Hub()
        self.router = _GatedHopRouter()
        self.transport = _transport(self.hub, **overrides)
        self.transport.bind_router(self.router)

    def __enter__(self) -> "_Node":
        self.transport.start()
        return self

    def __exit__(self, *_exc) -> None:
        for event in list(self.router.release.values()):
            event.set()
        for request in ("a", "b", "c", "d"):
            self.router.release_event(request).set()
        self.transport.close()

    def deliver(self, name: str, header: HopHeader, payload: bytes = b"x") -> bytes:
        message_id = _mid(name)
        self.hub.deliver(message_id, encode_frame(header, payload))
        return message_id

    def acked(self, message_id: bytes) -> bool:
        return message_id in self.hub.acks


def _on(**extra):
    return dict(hop_ack_on_receipt=True, **extra)


# --- (a) serialisation today, overlap after the change ------------------------


def test_default_off_hop_frame_waits_for_the_previous_execute_ack() -> None:
    with _Node() as node:
        first = node.deliver("m-a", _hop("a"))
        assert node.router.started_event("a").wait(5)
        second = node.deliver("m-b", _hop("b"))
        # B is not taken, let alone acknowledged, while A executes.
        assert _stays(lambda: len(node.hub.inbound) == 1 and not node.acked(second))
        assert not node.acked(first)
        node.router.release_event("a").set()
        _wait(lambda: node.acked(first))
        assert node.router.started_event("b").wait(5)
        node.router.release_event("b").set()
        _wait(lambda: node.acked(second))
        assert node.hub.acks == [first, second]


def test_on_receipt_accepts_the_next_request_while_the_first_executes() -> None:
    with _Node(**_on()) as node:
        first = node.deliver("m-a", _hop("a"))
        assert node.router.started_event("a").wait(5)
        second = node.deliver("m-b", _hop("b"))
        _wait(lambda: node.acked(first) and node.acked(second))
        # Both are RECEIVED while A is still inside its execute.
        assert node.router.running == {"req-a"}
        assert node.router.executed == []
        assert not node.router.started_event("b").is_set()
        node.router.release_event("a").set()
        assert node.router.started_event("b").wait(5)
        node.router.release_event("b").set()
        _wait(lambda: node.router.executed == ["req-a#1", "req-b#1"])
        assert node.hub.acks == [first, second]
        assert node.transport.fatal_error is None


def test_on_receipt_does_not_execute_two_hops_at_once() -> None:
    with _Node(**_on()) as node:
        node.deliver("m-a", _hop("a"))
        assert node.router.started_event("a").wait(5)
        node.deliver("m-b", _hop("b"))
        _wait(lambda: len(node.hub.acks) == 2)
        assert _stays(lambda: node.router.max_concurrent == 1)
        node.router.release_event("a").set()
        node.router.release_event("b").set()
        _wait(lambda: len(node.router.executed) == 2)
        assert node.router.max_concurrent == 1


# --- (c) order -----------------------------------------------------------------


def test_per_request_hop_order_is_preserved() -> None:
    with _Node(**_on(hop_ack_window=4)) as node:
        order = [("a", 1), ("b", 1), ("a", 2), ("b", 2)]
        for name in ("a", "b"):
            node.router.release_event(name).set()  # run freely; order is the subject
        for index, (request, token) in enumerate(order):
            node.deliver(f"m-{index}", _hop(request, token))
        _wait(lambda: len(node.router.executed) == 4)
        assert node.router.executed == [f"req-{r}#{t}" for r, t in order]
        for request in ("a", "b"):
            tokens = [
                int(item.split("#")[1])
                for item in node.router.executed
                if item.startswith(f"req-{request}#")
            ]
            assert tokens == sorted(tokens)


# --- (d) bounded in-flight window ----------------------------------------------


def test_in_flight_window_applies_backpressure_when_full() -> None:
    with _Node(**_on(hop_ack_window=2)) as node:
        ids = [node.deliver(f"m-{i}", _hop(name)) for i, name in enumerate("abcd")]
        assert node.router.started_event("a").wait(5)
        # Two RECEIVED (the executing one and one waiting); the rest stay queued
        # in the sidecar, so the sender's own bounded stream is what backs up.
        _wait(lambda: len(node.hub.acks) == 2)
        assert _stays(lambda: len(node.hub.acks) == 2 and len(node.hub.inbound) == 2)
        node.router.release_event("a").set()
        _wait(lambda: len(node.hub.acks) == 3)
        assert _stays(lambda: len(node.hub.acks) == 3 and len(node.hub.inbound) == 1)
        for name in "bcd":
            node.router.release_event(name).set()
        _wait(lambda: len(node.router.executed) == 4)
        assert node.hub.acks == ids


def test_window_and_flag_validation(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MYCELIUM_HOP_ACK_ON_RECEIPT", raising=False)
    hub = _Hub()
    default = _transport(hub)
    assert default.hop_ack_on_receipt is False
    assert default.hop_ack_window == 2
    assert _transport(hub, hop_ack_on_receipt=True).hop_ack_on_receipt is True
    for bad in (0, -1, 9):
        with pytest.raises(ValueError, match="hop_ack_window"):
            _transport(hub, hop_ack_window=bad)
    monkeypatch.setenv("MYCELIUM_HOP_ACK_ON_RECEIPT", "1")
    assert _transport(hub).hop_ack_on_receipt is True
    assert _transport(hub, hop_ack_on_receipt=False).hop_ack_on_receipt is False
    monkeypatch.setenv("MYCELIUM_HOP_ACK_ON_RECEIPT", "0")
    assert _transport(hub).hop_ack_on_receipt is False


# --- (e) cleanup, withdraw and drain wait for EXECUTED --------------------------


def test_cleanup_state_counts_received_hops_until_executed() -> None:
    with _Node(**_on()) as node:
        node.deliver("m-a", _hop("a"))
        assert node.router.started_event("a").wait(5)
        second = node.deliver("m-b", _hop("b"))
        _wait(lambda: node.acked(second))
        state = node.transport.cancellation_cleanup_state("req-b", "path-b", 0)
        assert state["inflight_received_count"] == 1
        assert node.transport.cancellation_cleanup_complete("req-b", "path-b", 0) is False
        node.router.release_event("a").set()
        node.router.release_event("b").set()
        _wait(lambda: len(node.router.executed) == 2)
        _wait(
            lambda: node.transport.cancellation_cleanup_state("req-b", "path-b", 0)[
                "inflight_received_count"
            ]
            == 0
        )


def test_executed_counters_do_not_move_at_receipt() -> None:
    with _Node(**_on()) as node:
        node.deliver("m-a", _hop("a"))
        assert node.router.started_event("a").wait(5)
        second = node.deliver("m-b", _hop("b"))
        _wait(lambda: node.acked(second))
        assert node.transport.evidence().router_frames_dispatched == 0
        node.router.release_event("a").set()
        _wait(lambda: node.transport.evidence().router_frames_dispatched == 1)
        node.router.release_event("b").set()
        _wait(lambda: node.transport.evidence().router_frames_dispatched == 2)


# --- (b) loss semantics ---------------------------------------------------------


def _failure_reports(hub: _Hub) -> list[FailureReport]:
    reports = []
    for _message_id, frame, _timeout, _generation in list(hub.sent):
        message = decode_frame(frame).message
        if isinstance(message, FailureReport):
            reports.append(message)
    return reports


@pytest.mark.parametrize("flag", [False, True], ids=["default_off", "on_receipt"])
def test_execute_failure_after_received_reports_the_same_failure_once(flag) -> None:
    overrides = _on() if flag else {}
    with _Node(**overrides) as node:
        node.router.failing.add("req-a")
        message_id = node.deliver("m-a", _hop("a"))
        node.router.release_event("a").set()
        _wait(lambda: _failure_reports(node.hub))
        _wait(lambda: node.acked(message_id))
        reports = _failure_reports(node.hub)
        assert len(reports) == 1
        assert (reports[0].request_id, reports[0].path_id) == ("req-a", "path-a")
        assert reports[0].scope == "PLACEMENT"
        assert reports[0].reason == "router_dispatch_failed"
        assert reports[0].token_index == 1
        assert node.hub.acks.count(message_id) == 1
        assert node.transport.fatal_error is None
        # Unrelated traffic stays live afterwards.
        other = node.deliver("m-b", _hop("b"))
        node.router.release_event("b").set()
        _wait(lambda: node.acked(other))
        assert node.router.executed == ["req-b#1"]


def test_replayed_hop_after_received_is_executed_once() -> None:
    """A re-sent hop (A6 replay / A7 successor re-send) never emits twice."""

    with _Node(**_on()) as node:
        header = _hop("a")
        first = node.deliver("m-1", header)
        assert node.router.started_event("a").wait(5)
        # The recovery path re-sends the same hop under a new message id while the
        # original is RECEIVED but not yet EXECUTED.
        replay = node.deliver("m-2", header)
        _wait(lambda: node.acked(first) and node.acked(replay))
        node.router.release_event("a").set()
        _wait(lambda: node.router.emitted.get(header.idempotency_key) == 1)
        _wait(lambda: len(node.hub.inbound) == 0 and not node.router.queue)
        assert _stays(lambda: node.router.emitted[header.idempotency_key] == 1)
        assert node.router.executed == ["req-a#1"]


# --- scope: only hop frames acknowledge on receipt -------------------------------


def test_non_hop_frames_keep_acknowledge_after_dispatch() -> None:
    with _Node(**_on()) as node:
        held = threading.Event()
        released = threading.Event()
        original = node.router.receive_token_event

        def gated_token_event(event, *, source_node_id=None):
            held.set()
            assert released.wait(5)
            return original(event, source_node_id=source_node_id)

        node.router.receive_token_event = gated_token_event
        token = TokenEvent(
            request_id="req-t",
            path_id="path-t",
            path_attempt=0,
            token_index=0,
            token_id=1,
            sampling_counter=1,
        )
        message_id = _mid("m-token")
        node.hub.deliver(message_id, encode_frame(token))
        assert held.wait(5)
        assert _stays(lambda: not node.acked(message_id))
        released.set()
        _wait(lambda: node.acked(message_id))


def test_only_plain_hop_frames_are_early_acknowledged() -> None:
    # Progressive prefill keeps its synchronous lock handshake, manifests and
    # assignment receipts keep ``remote_router_dispatch_ack``; only the hop
    # header qualifies for RECEIVED.
    assert IrohTransport._early_ack_kinds == ("HopHeader",)
