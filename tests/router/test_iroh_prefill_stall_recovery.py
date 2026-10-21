# SPDX-License-Identifier: AGPL-3.0-only
"""A14 class D: a prefill hop that stops crossing a degraded relay.

Under ``--a8-force-relay`` on a flapping mobile hotspot the sidecar confirmed
prefill chunk 0 and then never confirmed chunk 1.  The confirmed send waited
for the whole per-frame delivery budget (240 s), so the operator saw only the
generic ``prefill_completion_timeout`` and the request never recovered.

These tests drive the real ``IrohTransport`` against an in-memory wire that
models the sidecar contract the transport relies on:

* an outbound item is keyed by its message id; a second enqueue of a live id
  attaches to it (sidecar ``EnqueueOutcome::Duplicate``) and ``cancel`` of a
  live id withdraws it (``RecordKind::Cancel``);
* a confirmed send returns only after the receiving transport acknowledged
  local dispatch;
* a send that times out invalidates the local client session (as
  ``SidecarClient._request`` does on ``socket.timeout``).

The wire never de-duplicates deliveries itself, so exactly-once application
below is the receiving *transport's* message-id de-duplication.

Time is real but the stall policy is scaled down (sub-second windows); the
deadline formulas themselves are covered with production numbers in
``test_node_prefill_stall_wait.py``.
"""

from __future__ import annotations

from collections import deque
import threading
import time
from types import SimpleNamespace
from typing import Any

import pytest

from mycelium_iroh_sidecar import ProtocolError, SidecarError
import mycelium_router.transports.iroh as iroh_module
from mycelium_router.contracts import HopHeader, TokenEvent
from mycelium_router.transports.iroh import (
    IrohTransport,
    IrohTransportError,
    PeerBinding,
)
from mycelium_router.wire import encode_frame


# -- scaled stall policy -------------------------------------------------------


def _scaled_policy(**overrides: Any) -> Any:
    """Production policy shape with sub-second bounds for real-time tests."""

    values = dict(
        stall_floor_seconds=0.3,
        stall_ceiling_seconds=0.6,
        stall_rtt_multiplier=8.0,
        stall_transfer_multiplier=4.0,
        total_floor_seconds=1.0,
        total_command_fraction=0.8,
        total_transfer_multiplier=3.0,
        total_rtt_per_chunk_multiplier=4.0,
        # A large unmeasured goodput keeps the transfer term negligible, so the
        # stall window is the floor unless a test says otherwise.
        unmeasured_goodput_bytes_per_second=64.0 * 1024 * 1024,
        unmeasured_rtt_seconds=0.001,
        max_resends=2,
        ordering_margin_seconds=0.2,
        watchdog_rtt_multiplier=4.0,
    )
    values.update(overrides)
    policy_type = getattr(iroh_module, "PrefillStallPolicy", None)
    if policy_type is None:
        # Base tree: no policy type yet.  The stand-in is never read there, so
        # these tests fail on behaviour, not on an import.
        return SimpleNamespace(**values)
    return policy_type(**values)


@pytest.fixture
def scaled_policy(monkeypatch: pytest.MonkeyPatch) -> Any:
    policy = _scaled_policy()
    monkeypatch.setattr(
        iroh_module, "DEFAULT_PREFILL_STALL_POLICY", policy, raising=False
    )
    return policy


# -- in-memory sidecar wire ----------------------------------------------------


class _Item:
    def __init__(self, sender: str, message_id: bytes, frame: bytes) -> None:
        self.sender = sender
        self.message_id = message_id
        self.frame = frame
        self.cancelled = False
        self.delivered = threading.Event()
        self.terminal = threading.Event()


class _Wire:
    """Two sidecars and the link between them."""

    def __init__(self) -> None:
        self.lock = threading.Condition()
        self.inbox: dict[str, deque[tuple[bytes, int, bytes]]] = {
            "node-a": deque(),
            "node-b": deque(),
        }
        self.live: dict[tuple[str, bytes], _Item] = {}
        self.attempts: list[tuple[str, bytes, float]] = []
        self.cancels: list[bytes] = []
        self.clients: list[_WireClient] = []
        self.acked: dict[str, set[bytes]] = {"node-a": set(), "node-b": set()}
        # Link behaviour for frames sent by node-a.
        self.blackhole = False
        self.lose_confirmation_attempts: set[int] = set()
        self.bytes_per_second: float | None = None
        self.on_attempt: Any = None

    def factory(self, node_id: str):
        def make(*_args: Any, **_kwargs: Any) -> "_WireClient":
            client = _WireClient(self, node_id)
            with self.lock:
                self.clients.append(client)
            return client

        return make

    def lift_blackhole(self) -> None:
        with self.lock:
            self.blackhole = False
            self.lock.notify_all()

    def peer_of(self, node_id: str) -> str:
        return "node-b" if node_id == "node-a" else "node-a"

    def deliveries_to(self, node_id: str) -> int:
        return sum(1 for item in self.attempts if item[0] == self.peer_of(node_id))

    def send(
        self, sender: str, frame: bytes, message_id: bytes, timeout: float
    ) -> None:
        deadline = time.monotonic() + timeout
        receiver = self.peer_of(sender)
        with self.lock:
            attempt_index = len([a for a in self.attempts if a[0] == sender])
            self.attempts.append((sender, message_id, time.monotonic()))
            callback = self.on_attempt
            key = (sender, message_id)
            item = self.live.get(key)
            if item is None:
                item = _Item(sender, message_id, frame)
                self.live[key] = item
                fresh = True
            else:
                fresh = False
        if callback is not None:
            callback(attempt_index)
        if fresh:
            threading.Thread(
                target=self._carry,
                args=(item, receiver, attempt_index),
                daemon=True,
            ).start()
        with self.lock:
            while not item.terminal.is_set():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("confirmed delivery deadline")
                self.lock.wait(min(remaining, 0.01))
            if item.cancelled:
                raise SidecarError("cancelled")

    def _carry(self, item: _Item, receiver: str, attempt_index: int) -> None:
        if self.bytes_per_second:
            time.sleep(len(item.frame) / self.bytes_per_second)
        with self.lock:
            while self.blackhole and not item.cancelled:
                self.lock.wait(0.01)
            if item.cancelled:
                return
            self.inbox[receiver].append((item.message_id, 7, item.frame))
            item.delivered.set()
            self.lock.notify_all()
        lose = attempt_index in self.lose_confirmation_attempts
        while True:
            with self.lock:
                if item.cancelled:
                    return
                if item.message_id in self.acked[receiver] and not lose:
                    item.terminal.set()
                    self.live.pop((item.sender, item.message_id), None)
                    self.lock.notify_all()
                    return
                self.lock.wait(0.01)

    def cancel(self, sender: str, message_id: bytes) -> None:
        with self.lock:
            item = self.live.pop((sender, message_id), None)
            if item is None:
                raise SidecarError("unknown_message")
            item.cancelled = True
            item.terminal.set()
            self.cancels.append(message_id)
            self.lock.notify_all()

    def receive(self, node_id: str, timeout: float):
        deadline = time.monotonic() + timeout
        with self.lock:
            while not self.inbox[node_id]:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("empty")
                self.lock.wait(remaining)
            return self.inbox[node_id].popleft()

    def ack(self, node_id: str, message_id: bytes) -> None:
        with self.lock:
            self.acked[node_id].add(message_id)
            self.lock.notify_all()


class _WireClient:
    def __init__(self, wire: _Wire, node_id: str) -> None:
        self.wire = wire
        self.node_id = node_id
        self.connected = False
        self.endpoint_id: str | None = None
        self.closed = False

    def connect(self, *, deadline: float | None = None) -> None:
        del deadline
        self.connected = True
        self.closed = False
        self.endpoint_id = f"endpoint-{self.node_id[-1]}"

    def close(self) -> None:
        self.connected = False
        self.closed = True

    def interrupt(self) -> None:
        return

    def _require(self) -> None:
        if not self.connected:
            raise ProtocolError("not_connected")

    def configure_peer(self, *_args: Any, **_kwargs: Any) -> None:
        self._require()

    def configure_peers(self, *_args: Any, **_kwargs: Any) -> None:
        self._require()

    def send_confirmed(
        self,
        frame: bytes,
        message_id: bytes,
        *,
        timeout: float | None = None,
        expected_generation: int,
        source_generation: int,
    ) -> bytes:
        del expected_generation, source_generation
        self._require()
        try:
            self.wire.send(self.node_id, frame, message_id, float(timeout or 5.0))
        except TimeoutError:
            # SidecarClient invalidates its session on a socket timeout.
            self.connected = False
            raise
        return message_id

    def send_routed(self, _destination: str, frame: bytes, message_id: bytes, **kw):
        return self.send_confirmed(
            frame,
            message_id,
            timeout=kw.get("timeout"),
            expected_generation=kw["expected_generation"],
            source_generation=kw["source_generation"],
        )

    def recv_with_generation(self, *, timeout: float | None = None):
        self._require()
        return self.wire.receive(self.node_id, timeout or 0.05)

    def recv(self, *, timeout: float | None = None):
        message_id, _generation, frame = self.recv_with_generation(timeout=timeout)
        return message_id, frame

    def ack(self, message_id: bytes) -> None:
        self._require()
        self.wire.ack(self.node_id, message_id)

    def cancel(self, message_id: bytes, *, timeout: float | None = None) -> None:
        del timeout
        self._require()
        self.wire.cancel(self.node_id, message_id)


# -- routers ---------------------------------------------------------------------


class _ReceivingRouter:
    """Counts every Router-level application of a hop."""

    def __init__(self) -> None:
        self.hops: list[tuple[HopHeader, bytes]] = []
        self.applied = threading.Event()

    def receive_hop(self, header, payload, *, source_node_id=None):
        del source_node_id
        self.hops.append((header, payload))
        self.applied.set()
        return True


class _ForwardingRouter:
    """Entry-side router: an inbound event makes it forward the next chunk.

    This is the A14 shape: a frame arriving on the dispatcher thread makes the
    entry send the next prefill chunk, which the transport defers to its
    ordered forward worker.
    """

    def __init__(self) -> None:
        self.transport: IrohTransport | None = None
        self.frame_to_forward: bytes | None = None

    def receive_token_event(self, event, *, source_node_id=None):
        del event, source_node_id
        assert self.transport is not None and self.frame_to_forward is not None
        self.transport._send_or_dispatch("node-b", self.frame_to_forward)
        return True


def _binding(node: str) -> PeerBinding:
    endpoint = f"endpoint-{node[-1]}"
    return PeerBinding(
        node_id=node,
        endpoint_id=endpoint,
        endpoint_addr={"id": endpoint, "addrs": ["127.0.0.1:1"]},
        generation=7,
    )


def _transport(
    wire: _Wire,
    node_id: str,
    *,
    delivery_timeout_seconds: float = 6.0,
) -> IrohTransport:
    return IrohTransport(
        node_id=node_id,
        socket_path="/unused",
        bootstrap_secret=b"s" * 32,
        peer=_binding(wire.peer_of(node_id)),
        local_generation=7,
        expected_endpoint_id=f"endpoint-{node_id[-1]}",
        queue_capacity=8,
        delivery_timeout_seconds=delivery_timeout_seconds,
        poll_interval_seconds=0.01,
        client_factory=wire.factory(node_id),
    )


def _chunk_frame(
    *, request_id: str = "request-a14", chunk_index: int = 1, payload_bytes: int = 64
) -> bytes:
    header = HopHeader(
        request_id=request_id,
        path_id="path-a14",
        path_attempt=0,
        phase="PREFILL_CHUNK",
        token_index=chunk_index,
        hop_index=1,
        source_placement_id="placement-0",
        destination_placement_id="placement-1",
        topology_version=1,
        idempotency_key=f"chunk-{chunk_index}",
        prefill_chunk_token_count=8,
    )
    return encode_frame(header, b"\x01" * payload_bytes)


def _trigger_frame() -> bytes:
    return encode_frame(
        TokenEvent(
            request_id="request-trigger",
            path_id="path-trigger",
            path_attempt=0,
            token_index=0,
            token_id=1,
            sampling_counter=1,
        )
    )


def _scoped_failures(transport: IrohTransport, request_id: str) -> list[str | None]:
    return [
        event["code"]
        for event in transport.evidence().scoped_events
        if event["event"] == "failure" and event["request_id"] == request_id
    ]


def _close_quiesced(*transports: IrohTransport) -> None:
    """Close once every delivery-cancel worker has finished its attempt.

    ``close()`` grants an in-flight sidecar cancel only a short shutdown
    window and fails closed when a started attempt overruns it; under CPU
    burners the in-memory wire can take longer than that to answer, which
    would turn the test's own teardown into the failure.
    """

    for transport in transports:
        _wait_for(lambda: not transport._delivery_cancel_threads, 5.0)
    for transport in transports:
        transport.close()


def _wait_for(predicate, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


# -- (a) blackhole after chunk 0 -------------------------------------------------


def test_blackholed_forwarded_chunk_fails_as_prefill_transport_stalled_within_the_stall_bound(
    scaled_policy: Any,
) -> None:
    """Chunk 0 crosses, then the link blackholes: a specific code, quickly.

    The stall bound for one frame is ``(max_resends + 1) x stall window``
    (0.9 s here); the per-frame delivery budget is 6 s.  Before the fix the
    forward worker waited the whole delivery budget and reported only
    ``delivery_deadline_exceeded`` - which the node's prefill wait could not
    even observe before its own, equally long, wait expired.
    """

    wire = _Wire()
    sender = _transport(wire, "node-a")
    receiver = _transport(wire, "node-b")
    entry_router = _ForwardingRouter()
    entry_router.transport = sender
    receiving_router = _ReceivingRouter()
    sender.bind_router(entry_router)
    receiver.bind_router(receiving_router)
    sender.start()
    receiver.start()
    try:
        # Chunk 0 crosses normally (synchronous, like the entry's first chunk).
        sender.send_router_frame(_chunk_frame(chunk_index=0), destination_node_id="node-b")
        assert receiving_router.applied.wait(2.0)

        wire.blackhole = True
        entry_router.frame_to_forward = _chunk_frame(chunk_index=1)
        started = time.monotonic()
        wire.inbox["node-a"].append((b"t" * 16, 7, _trigger_frame()))
        with wire.lock:
            wire.lock.notify_all()

        assert _wait_for(lambda: _scoped_failures(sender, "request-a14"), 5.0)
        elapsed = time.monotonic() - started

        # The send wrapper and the forward worker each record the failed
        # forward (pre-existing); every record carries the specific code.
        assert set(_scoped_failures(sender, "request-a14")) == {
            "prefill_transport_stalled"
        }
        stall_bound = (scaled_policy.max_resends + 1) * scaled_policy.stall_floor_seconds
        assert elapsed < stall_bound + 1.0, elapsed
        assert elapsed < sender.delivery_timeout_seconds / 2
        # Every attempt carried the chunk's one idempotency key (message id).
        chunk_attempts = [a for a in wire.attempts if a[0] == "node-a"][1:]
        assert len(chunk_attempts) == scaled_policy.max_resends + 1
        assert len({message_id for _, message_id, _ in chunk_attempts}) == 1
        # Each stalled attempt was withdrawn from the sidecar before the resend,
        # and the final one is withdrawn when the transport gives up.
        assert _wait_for(
            lambda: wire.cancels.count(chunk_attempts[0][1])
            == scaled_policy.max_resends + 1,
            2.0,
        ), wire.cancels
        assert len(receiving_router.hops) == 1
    finally:
        wire.lift_blackhole()
        _close_quiesced(sender, receiver)


# -- (c) blackhole that recovers after one reconnect -----------------------------


def test_lost_confirmation_recovers_after_one_reconnect_and_applies_the_chunk_once(
    scaled_policy: Any,
) -> None:
    """The chunk crossed but its confirmation was lost on the flapping relay.

    The sender detects the stall, withdraws the stuck sidecar item, reconnects
    its data client and resends under the *same* message id.  The receiving
    transport sees the chunk twice and must apply it exactly once.
    """

    wire = _Wire()
    sender = _transport(wire, "node-a")
    receiver = _transport(wire, "node-b")
    receiving_router = _ReceivingRouter()
    sender.bind_router(_ForwardingRouter())
    receiver.bind_router(receiving_router)
    sender.start()
    receiver.start()
    try:
        wire.lose_confirmation_attempts = {0}
        clients_before = len([c for c in wire.clients if c.node_id == "node-a"])
        frame = _chunk_frame(chunk_index=1)
        started = time.monotonic()

        receipt = sender.send_router_frame(frame, destination_node_id="node-b")

        elapsed = time.monotonic() - started
        attempts = [a for a in wire.attempts if a[0] == "node-a"]
        assert len(attempts) == 2
        assert attempts[0][1] == attempts[1][1] == receipt.message_id
        assert scaled_policy.stall_floor_seconds <= elapsed < 2.0
        # One data-client replacement for the one stall.
        clients_after = len([c for c in wire.clients if c.node_id == "node-a"])
        assert clients_after == clients_before + 1
        assert wire.cancels == [receipt.message_id]
        # Delivered twice on the wire, applied once by the receiving Router.
        assert _wait_for(lambda: receiver.counter_snapshot()["duplicate_frames"] == 1, 2.0)
        assert len(receiving_router.hops) == 1
        assert receiving_router.hops[0][0].token_index == 1
        assert _scoped_failures(sender, "request-a14") == []
    finally:
        _close_quiesced(sender, receiver)


def test_blackholed_forward_recovers_when_the_link_returns_and_applies_once(
    scaled_policy: Any,
) -> None:
    """Forward-worker path: a blackhole that lifts after the first resend."""

    wire = _Wire()
    sender = _transport(wire, "node-a")
    receiver = _transport(wire, "node-b")
    entry_router = _ForwardingRouter()
    entry_router.transport = sender
    receiving_router = _ReceivingRouter()
    sender.bind_router(entry_router)
    receiver.bind_router(receiving_router)
    sender.start()
    receiver.start()
    try:
        wire.blackhole = True

        def lift_on_resend(attempt_index: int) -> None:
            if attempt_index == 1:
                wire.lift_blackhole()

        wire.on_attempt = lift_on_resend
        entry_router.frame_to_forward = _chunk_frame(chunk_index=1)
        wire.inbox["node-a"].append((b"t" * 16, 7, _trigger_frame()))
        with wire.lock:
            wire.lock.notify_all()

        assert receiving_router.applied.wait(3.0)
        assert _wait_for(
            lambda: any(
                event["event"] == "receipt" and event["request_id"] == "request-a14"
                for event in sender.evidence().scoped_events
            ),
            3.0,
        )
        assert _scoped_failures(sender, "request-a14") == []
        assert len(receiving_router.hops) == 1
        chunk_attempts = [a for a in wire.attempts if a[0] == "node-a"]
        assert len(chunk_attempts) == 2
        assert chunk_attempts[0][1] == chunk_attempts[1][1]
    finally:
        wire.lift_blackhole()
        _close_quiesced(sender, receiver)


# -- (b) slow but progressing ----------------------------------------------------


def test_slow_but_progressing_chunk_is_never_resent(monkeypatch) -> None:
    """A large frame on a slow link is not a stall.

    The window scales with ``frame_bytes / goodput x 4``: with the unmeasured
    floor goodput here (32 KiB/s) a 16 KiB chunk earns a 2 s window, so a
    0.6 s transfer completes with no resend even though it is longer than the
    0.3 s stall floor.
    """

    policy = _scaled_policy(
        stall_ceiling_seconds=4.0,
        unmeasured_goodput_bytes_per_second=32.0 * 1024,
    )
    monkeypatch.setattr(
        iroh_module, "DEFAULT_PREFILL_STALL_POLICY", policy, raising=False
    )
    wire = _Wire()
    wire.bytes_per_second = 16 * 1024 / 0.6
    sender = _transport(wire, "node-a")
    receiver = _transport(wire, "node-b")
    receiving_router = _ReceivingRouter()
    sender.bind_router(_ForwardingRouter())
    receiver.bind_router(receiving_router)
    sender.start()
    receiver.start()
    try:
        sender.send_router_frame(
            _chunk_frame(chunk_index=1, payload_bytes=16 * 1024),
            destination_node_id="node-b",
        )
        assert len([a for a in wire.attempts if a[0] == "node-a"]) == 1
        assert wire.cancels == []
        assert len(receiving_router.hops) == 1
    finally:
        _close_quiesced(sender, receiver)


# -- (2) deadline ordering -------------------------------------------------------


def test_registered_prefill_deadline_bounds_a_stuck_hop_before_the_wait_expires(
    scaled_policy: Any,
) -> None:
    """A stuck hop must report its own code before the prefill wait expires.

    The per-frame budget (6 s) is longer than the time left in the prefill
    wait (1 s).  The hop's deadline is clamped to the wait deadline minus the
    ordering margin, so the delivery failure is recorded while the wait can
    still observe and report it.
    """

    wire = _Wire()
    sender = _transport(wire, "node-a")
    sender.bind_router(_ForwardingRouter())
    sender.start()
    try:
        watch = getattr(sender, "watch_prefill", None)
        assert callable(watch), "transport exposes no prefill deadline registration"
        wait_deadline = time.monotonic() + 1.0
        watch("request-a14", deadline_monotonic=wait_deadline)
        wire.blackhole = True
        # TokenEvent is not a resendable prefill frame: only the ordering
        # clamp applies to it.
        frame = encode_frame(
            TokenEvent(
                request_id="request-a14",
                path_id="path-a14",
                path_attempt=0,
                token_index=0,
                token_id=5,
                sampling_counter=1,
            )
        )
        with pytest.raises(IrohTransportError) as caught:
            sender.send_router_frame(frame, destination_node_id="node-b")
        failed_at = time.monotonic()
        assert caught.value.code == "delivery_deadline_exceeded"
        assert failed_at <= wait_deadline - scaled_policy.ordering_margin_seconds + 0.3
        progress = sender.prefill_progress("request-a14")
        assert progress["failure_code"] == "delivery_deadline_exceeded"
    finally:
        wire.lift_blackhole()
        _close_quiesced(sender)


def test_prefill_progress_counts_confirmed_bytes_and_inbound_frames(
    scaled_policy: Any,
) -> None:
    del scaled_policy
    wire = _Wire()
    sender = _transport(wire, "node-a")
    receiver = _transport(wire, "node-b")
    sender.bind_router(_ForwardingRouter())
    receiver.bind_router(_ReceivingRouter())
    sender.start()
    receiver.start()
    try:
        sender.watch_prefill("request-a14", deadline_monotonic=time.monotonic() + 30)
        before = sender.prefill_progress("request-a14")
        assert before["progress_events"] == 0
        frame = _chunk_frame(chunk_index=0, payload_bytes=2048)
        sender.send_router_frame(frame, destination_node_id="node-b")
        after = sender.prefill_progress("request-a14")
        assert after["progress_events"] == 1
        assert after["bytes_confirmed"] == len(frame)
        assert after["max_frame_bytes"] == len(frame)
        assert after["last_progress_at"] >= before["last_progress_at"]
        assert after["failure_code"] is None
        estimate = sender.link_estimate("node-b")
        assert estimate.goodput_bytes_per_second > 0
        assert estimate.rtt_p95_seconds > 0
        sender.release_prefill_watch("request-a14")
        assert sender.prefill_progress("request-a14") is None
    finally:
        _close_quiesced(sender, receiver)


def test_non_prefill_frames_keep_the_single_attempt_delivery_contract(
    scaled_policy: Any,
) -> None:
    """Decode-time traffic is not resent: only prefill frames get stall recovery."""

    del scaled_policy
    wire = _Wire()
    sender = _transport(wire, "node-a", delivery_timeout_seconds=0.8)
    sender.bind_router(_ForwardingRouter())
    sender.start()
    try:
        wire.blackhole = True
        frame = encode_frame(
            TokenEvent(
                request_id="request-decode",
                path_id="path-decode",
                path_attempt=0,
                token_index=3,
                token_id=5,
                sampling_counter=1,
            )
        )
        with pytest.raises(IrohTransportError) as caught:
            sender.send_router_frame(frame, destination_node_id="node-b")
        assert caught.value.code == "delivery_deadline_exceeded"
        assert len([a for a in wire.attempts if a[0] == "node-a"]) == 1
    finally:
        wire.lift_blackhole()
        _close_quiesced(sender)
