# SPDX-License-Identifier: AGPL-3.0-only
"""wp2-6: connecting to a dead peer must fail with a NAMED reason, in bounded time.

The defect (``rca/wp2-iroh-delivery.md``, defect 2): iroh 1.0.2 has no connect
timeout, so a peer that has vanished consumes the hop's whole latency budget and
surfaces only as the route's ``delivery_deadline_exceeded`` - a wedge-shaped
failure with no transport cause.

The fix is the explicit connect: ``SidecarClient.connect_peer`` binds the peer
and proves the dial by confirming the hop's first frame, bounded by the
registered ``mycelium_deadlines.peer_connect_deadline_seconds`` and named
``peer_connect_deadline_exceeded``.  Ordinary deliveries keep their previous
semantics exactly - the deadline applies only to a declared connect, and only
until the peer has answered once.

Everything here is offline: two ends of a ``socketpair`` with fixed session
keys, exactly like ``test_sidecar_client_framing.py``.  No relay, no socket
outside the process, no network.
"""

from __future__ import annotations

import socket
import struct
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

import pytest

import mycelium_deadlines as deadlines
from mycelium_iroh_sidecar import SidecarClient
from mycelium_iroh_sidecar import client as sidecar_client_module

ROOT = Path(__file__).resolve().parents[2]
GOLDEN_FRAME = (ROOT / "contracts" / "router-wire-golden" / "01-hop-header.bin").read_bytes()

_SEND_KEY = b"s" * 32
_RECEIVE_KEY = b"r" * 32
_DEAD_PEER_ID = bytes(range(32)).hex()
_DEAD_PEER_ADDR = {"id": _DEAD_PEER_ID, "addrs": ["127.0.0.1:1"]}

#: The caller's own delivery budget: far larger than the connect deadline the
#: registry must publish, so "the hop budget expired" and "the connect deadline
#: expired" are distinguishable.
CALLER_DELIVERY_BUDGET_SECONDS = 5.0

#: Bounded slack for scheduler noise; the assertions below are written against
#: whole seconds of difference, not against this.
_SLACK_SECONDS = 0.5

CONNECT_DEADLINE_NAME = "peer_connect_deadline_seconds"
CONNECT_DEADLINE_ERROR = "PeerConnectDeadlineExceeded"
CONNECT_DEADLINE_REASON = "peer_connect_deadline_exceeded"


# --------------------------------------------------------------------------- #
# Harness: an established session whose remote end we control byte by byte.
# --------------------------------------------------------------------------- #


def _recv_exact(stream: socket.socket, length: int) -> bytes:
    received = bytearray()
    while len(received) < length:
        chunk = stream.recv(length - len(received))
        assert chunk, "client closed while the fake peer was reading"
        received.extend(chunk)
    return bytes(received)


def _read_request(stream: socket.socket) -> bytes:
    length = struct.unpack(">I", _recv_exact(stream, 4))[0]
    assert length > 0
    return _recv_exact(stream, length)


def _answer_request(
    stream: socket.socket, record: bytes, *, request_sequence: int, reply_sequence: int
) -> None:
    """Answer one client request with the canonical acknowledgement.

    Request and reply carry independent monotonic sequences: the fake peer
    replays the client's own framing rules in the other direction.
    """

    _kind, message_id, _payload = sidecar_client_module._decode_record(
        record, _SEND_KEY, request_sequence
    )
    reply = sidecar_client_module._encode_record(
        sidecar_client_module._ACK, reply_sequence, message_id, b"", _RECEIVE_KEY
    )
    stream.sendall(struct.pack(">I", len(reply)) + reply)


def _answer_next_requests(peer_stream: socket.socket, count: int) -> threading.Thread:
    """Answer the next ``count`` client requests, off the main thread.

    The client blocks inside each request until it is answered, so answers have
    to come from another thread.
    """

    def answer() -> None:
        for sequence in range(count):
            _answer_request(
                peer_stream,
                _read_request(peer_stream),
                request_sequence=sequence,
                reply_sequence=sequence,
            )

    responder = threading.Thread(target=answer, name="wp2-6-responder", daemon=True)
    responder.start()
    return responder


@contextmanager
def _established_session() -> Iterator[tuple[SidecarClient, socket.socket]]:
    """Install fixed session keys over a socketpair, bypassing only the handshake."""

    client_stream, peer_stream = socket.socketpair()
    peer_stream.settimeout(2.0)
    client = SidecarClient("/unused", b"b" * 32, timeout=CALLER_DELIVERY_BUDGET_SECONDS)
    client._socket = client_stream  # type: ignore[assignment]
    client._send_key = bytearray(_SEND_KEY)
    client._receive_key = bytearray(_RECEIVE_KEY)
    try:
        yield client, peer_stream
    finally:
        for stream in (peer_stream, client_stream):
            try:
                stream.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            stream.close()
        client.close()


def _observe(
    call, peer_stream: socket.socket, *, answer_requests: int, wait: float
) -> tuple[BaseException | None, float]:
    """Run one client call in a worker and report how it ended, and how long it took."""

    outcome: list[BaseException | None] = []
    completed = threading.Event()

    def invoke() -> None:
        try:
            call()
        except BaseException as error:  # reported to the test thread
            outcome.append(error)
        else:
            outcome.append(None)
        finally:
            completed.set()

    responder = _answer_next_requests(peer_stream, answer_requests)
    started = time.monotonic()
    worker = threading.Thread(target=invoke, name="wp2-6-dead-peer", daemon=True)
    worker.start()
    try:
        assert completed.wait(wait + _SLACK_SECONDS), "the call never returned"
    finally:
        completed.wait(_SLACK_SECONDS)
        worker.join(_SLACK_SECONDS)
        responder.join(_SLACK_SECONDS)
    elapsed = time.monotonic() - started
    assert not worker.is_alive(), "client worker leaked"
    assert len(outcome) == 1
    return outcome[0], elapsed


def _connect_dead_peer(
    client: SidecarClient, peer_stream: socket.socket, *, timeout: float | None = None
) -> tuple[BaseException | None, float]:
    """Connect to a peer that answers the local bind, then goes silent forever."""

    return _observe(
        lambda: client.connect_peer(
            _DEAD_PEER_ID, _DEAD_PEER_ADDR, GOLDEN_FRAME, timeout=timeout
        ),
        peer_stream,
        # configure-peer is answered (the local bind is not the failure under
        # test); the confirmed send that proves the dial never is.
        answer_requests=1,
        wait=CALLER_DELIVERY_BUDGET_SECONDS if timeout is None else timeout,
    )


# --------------------------------------------------------------------------- #
# The registry: the deadline the connect path must consume.
# --------------------------------------------------------------------------- #


def _registered_connect_deadline() -> float:
    names = {record.name for record in deadlines.DEADLINES}
    assert CONNECT_DEADLINE_NAME in names, (
        "wp2-6 must register the peer connect deadline in mycelium_deadlines"
    )
    record = deadlines.deadline(CONNECT_DEADLINE_NAME)
    assert record.unit == "seconds", record.unit
    assert record.derivation.strip() and record.source.strip()
    return record.value


def test_the_connect_deadline_is_registered_inside_the_route_fanout_window() -> None:
    """A dead peer must fail strictly before the route's cleanup fanout window."""

    value = _registered_connect_deadline()
    fanout_window = deadlines.route_cleanup_fanout_window_seconds

    # RE-DERIVED 2026-09-20 (ERRATA-19/20/21): the route fanout window moved from 1.5 s
    # (budget 2.0 - reserve 0.5) to 4.5 s (budget 8.0 - reserve 3.5), because the relayed
    # peer's MEASURED cleanup was 1.83 s / 3.18 s and the old window could never honour it.
    # The connect deadline itself does NOT move: it is a dead-peer DETECTION bound, not a
    # cleanup budget, and the invariant this test protects is that it sits strictly inside
    # the window -- which it now does with more headroom, not less.
    assert fanout_window == 4.5, "the route fanout window moved; re-derive the value"
    assert 0 < value < fanout_window, (
        f"peer connect deadline {value}s must be strictly smaller than the route's "
        f"{fanout_window}s cleanup fanout window"
    )
    assert value < CALLER_DELIVERY_BUDGET_SECONDS


def test_the_connect_deadline_is_single_sourced_from_the_registry() -> None:
    """The client constant is an assignment from the registry, not a literal."""

    value = _registered_connect_deadline()
    observed = getattr(sidecar_client_module, "PEER_CONNECT_DEADLINE_SECONDS", None)
    assert observed == value, (
        "mycelium_iroh_sidecar.client.PEER_CONNECT_DEADLINE_SECONDS must equal the "
        "registry value"
    )
    binding = dict(deadlines.CONSUMER_BINDINGS).get(
        "mycelium_iroh_sidecar.client.PEER_CONNECT_DEADLINE_SECONDS"
    )
    assert binding == CONNECT_DEADLINE_NAME, binding


# --------------------------------------------------------------------------- #
# The behaviour: named, bounded failure instead of the hop's budget expiring.
# --------------------------------------------------------------------------- #


def test_dead_peer_fails_with_a_named_reason_inside_the_registered_deadline() -> None:
    error_type = getattr(sidecar_client_module, CONNECT_DEADLINE_ERROR, None)
    named = bool(
        error_type is not None
        and issubclass(error_type, sidecar_client_module.SidecarError)
    )

    with _established_session() as (client, peer):
        outcome, elapsed = _connect_dead_peer(client, peer)

    reason = getattr(outcome, "code", None)
    bounded = elapsed < deadlines.route_cleanup_fanout_window_seconds
    matched = named and isinstance(outcome, error_type)  # type: ignore[arg-type]
    assert matched and bounded and reason == CONNECT_DEADLINE_REASON, (
        "connecting to a dead peer must fail with a NAMED reason inside the "
        f"registered deadline: elapsed={elapsed:.3f}s (fanout window "
        f"{deadlines.route_cleanup_fanout_window_seconds}s, caller budget "
        f"{CALLER_DELIVERY_BUDGET_SECONDS}s), raised "
        f"{type(outcome).__name__}(code={reason!r}), named reason available={named}"
    )


def test_ordinary_delivery_keeps_the_callers_budget_byte_for_byte() -> None:
    """No declared connect: a delivery wait is exactly the caller's timeout."""

    with _established_session() as (client, peer):
        responder = _answer_next_requests(peer, 1)
        client.configure_peer(_DEAD_PEER_ID, _DEAD_PEER_ADDR, generation=1)
        responder.join(2.0)
        assert not responder.is_alive(), "the fake peer never answered configure_peer"

        # The peer is unproven and never answers, yet the caller asked for no
        # connect phase: this must stay the shipped TimeoutError, not a renamed
        # connect failure, or every slow-but-alive first delivery would break.
        outcome, elapsed = _observe(
            lambda: client.send_confirmed(
                GOLDEN_FRAME, timeout=CALLER_DELIVERY_BUDGET_SECONDS
            ),
            peer,
            answer_requests=0,
            wait=CALLER_DELIVERY_BUDGET_SECONDS,
        )

    assert isinstance(outcome, TimeoutError), (
        f"an undeclared connect must keep the caller's semantics, got "
        f"{type(outcome).__name__}(code={getattr(outcome, 'code', None)})"
    )
    assert elapsed >= CALLER_DELIVERY_BUDGET_SECONDS - _SLACK_SECONDS


def test_a_proven_peer_is_not_re_bounded_by_the_connect_deadline() -> None:
    """The deadline bounds the first proof only, not the rest of the hop."""

    registered = _registered_connect_deadline()
    caller_budget = registered + 0.5

    with _established_session() as (client, peer):
        # A connect that succeeds proves the peer reachable.
        responder = _answer_next_requests(peer, 2)
        message_id = client.connect_peer(
            _DEAD_PEER_ID, _DEAD_PEER_ADDR, GOLDEN_FRAME, timeout=caller_budget
        )
        responder.join(2.0)
        assert not responder.is_alive(), "the fake peer never answered connect_peer"
        assert isinstance(message_id, bytes) and len(message_id) == 16

        # A later delivery to the same proven peer, *with* the connect deadline
        # declared, is no longer a connect and keeps the caller's budget.
        outcome, elapsed = _observe(
            lambda: client.send_confirmed(
                GOLDEN_FRAME, timeout=caller_budget, connect_deadline=registered
            ),
            peer,
            answer_requests=0,
            wait=caller_budget,
        )

    assert isinstance(outcome, TimeoutError), (
        "a peer that already proved reachable must keep the caller's full delivery "
        f"budget, got {type(outcome).__name__}(code={getattr(outcome, 'code', None)})"
    )
    assert elapsed >= registered


def test_the_named_reason_is_exported_by_the_package() -> None:
    import mycelium_iroh_sidecar as package

    exported = getattr(package, CONNECT_DEADLINE_ERROR, None)
    assert exported is getattr(sidecar_client_module, CONNECT_DEADLINE_ERROR, None)
    assert CONNECT_DEADLINE_ERROR in package.__all__


@pytest.mark.parametrize("seconds", [0.0, -1.0])
def test_an_explicit_connect_deadline_must_be_positive(seconds: float) -> None:
    """Fail closed on a caller-supplied budget that cannot be honoured."""

    client = SidecarClient("/unused", b"b" * 32)
    with pytest.raises(ValueError):
        client.send_confirmed(
            GOLDEN_FRAME,
            timeout=1.0,
            expected_generation=1,
            connect_deadline=seconds,
        )
    with pytest.raises(ValueError):
        client.connect_peer(
            _DEAD_PEER_ID, _DEAD_PEER_ADDR, GOLDEN_FRAME, timeout=seconds
        )
