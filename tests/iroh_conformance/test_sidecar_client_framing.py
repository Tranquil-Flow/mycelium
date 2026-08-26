# SPDX-License-Identifier: AGPL-3.0-or-later
"""Deterministic socketpair tests for canonical SidecarClient framing."""

from __future__ import annotations

from contextlib import contextmanager
import hashlib
import hmac
import json
import socket
import struct
import threading
from types import SimpleNamespace
from typing import Callable, Iterator

import pytest

from mycelium_iroh_sidecar import AuthenticationError, ProtocolError, SidecarClient
from mycelium_iroh_sidecar import client as sidecar_client_module

_SEND_KEY = b"s" * 32
_RECEIVE_KEY = b"r" * 32
_MESSAGE_ID = b"m" * 16
_WAIT_SECONDS = 1.0


class _ObservedSocket:
    """A socket facade that reports each completed production ``recv`` call."""

    def __init__(
        self,
        stream: socket.socket,
        *,
        on_receive: Callable[[], None] | None = None,
    ) -> None:
        self._stream = stream
        self._on_receive = on_receive
        self._condition = threading.Condition()
        self._received_chunks: list[bytes] = []

    def recv(self, size: int, flags: int = 0) -> bytes:
        chunk = self._stream.recv(size, flags)
        if chunk:
            if self._on_receive is not None:
                self._on_receive()
            with self._condition:
                self._received_chunks.append(chunk)
                self._condition.notify_all()
        return chunk

    def wait_for_receive_count(self, count: int) -> bool:
        with self._condition:
            return self._condition.wait_for(
                lambda: len(self._received_chunks) >= count,
                timeout=_WAIT_SECONDS,
            )

    @property
    def received_chunks(self) -> tuple[bytes, ...]:
        with self._condition:
            return tuple(self._received_chunks)

    def __getattr__(self, name: str) -> object:
        return getattr(self._stream, name)


class _FakeClock:
    """A monotonic clock advanced only when the client consumes a byte."""

    def __init__(self) -> None:
        self._milliseconds = 100_000
        self._lock = threading.Lock()

    def monotonic(self) -> float:
        with self._lock:
            return self._milliseconds / 1_000

    def advance(self) -> None:
        with self._lock:
            self._milliseconds += 100


@contextmanager
def _established_session(
    *, on_receive: Callable[[], None] | None = None
) -> Iterator[tuple[SidecarClient, socket.socket, _ObservedSocket]]:
    """Install fixed session keys over a socketpair, bypassing only the handshake."""

    client_stream, peer_stream = socket.socketpair()
    observed_stream = _ObservedSocket(client_stream, on_receive=on_receive)
    peer_stream.settimeout(_WAIT_SECONDS)
    client = SidecarClient("/unused", b"b" * 32, timeout=_WAIT_SECONDS)
    client._socket = observed_stream  # type: ignore[assignment]
    client._send_key = bytearray(_SEND_KEY)
    client._receive_key = bytearray(_RECEIVE_KEY)
    try:
        yield client, peer_stream, observed_stream
    finally:
        # Shutdown before taking the client's lock so a failed assertion cannot
        # leave a worker blocked in recv while close waits for that same lock.
        for stream in (peer_stream, client_stream):
            try:
                stream.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            stream.close()
        client.close()


def _recv_exact(stream: socket.socket, length: int) -> bytes:
    received = bytearray()
    while len(received) < length:
        chunk = stream.recv(length - len(received))
        assert chunk, "client closed while test peer was reading its request"
        received.extend(chunk)
    return bytes(received)


def _read_request(stream: socket.socket) -> bytes:
    length = struct.unpack(">I", _recv_exact(stream, 4))[0]
    assert length > 0
    return _recv_exact(stream, length)


def _prefixed(record: bytes) -> bytes:
    return struct.pack(">I", len(record)) + record


def _ack(sequence: int, message_id: bytes = sidecar_client_module._ZERO_ID) -> bytes:
    return sidecar_client_module._encode_record(
        sidecar_client_module._ACK,
        sequence,
        message_id,
        b"",
        _RECEIVE_KEY,
    )


def _record_with_mismatched_payload_length() -> bytes:
    signed = sidecar_client_module._HEADER.pack(
        sidecar_client_module._RECORD_VERSION,
        sidecar_client_module._ACK,
        0,
        sidecar_client_module._ZERO_ID,
        1,
    )
    return signed + hmac.new(_RECEIVE_KEY, signed, hashlib.sha256).digest()


def _start_call(
    call: Callable[[], None],
) -> tuple[threading.Thread, threading.Event, list[BaseException | None]]:
    completed = threading.Event()
    outcomes: list[BaseException | None] = []

    def invoke() -> None:
        try:
            call()
        except BaseException as error:  # retained for assertions in the test thread
            outcomes.append(error)
        else:
            outcomes.append(None)
        finally:
            completed.set()

    worker = threading.Thread(target=invoke, name="sidecar-client-framing-test")
    worker.start()
    return worker, completed, outcomes


def _join_worker(
    worker: threading.Thread,
    completed: threading.Event,
    peer_stream: socket.socket,
) -> None:
    if not completed.wait(_WAIT_SECONDS):
        try:
            peer_stream.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
    worker.join(_WAIT_SECONDS)
    assert not worker.is_alive(), "SidecarClient test worker leaked"


def _assert_session_poisoned(client: SidecarClient) -> None:
    assert client.connected is False
    assert client._send_key is None
    assert client._receive_key is None


def test_ping_accepts_byte_at_a_time_length_prefix_and_record_payload() -> None:
    """Every prefix and canonical record byte may arrive in a distinct recv."""

    with _established_session() as (client, peer, observed):
        worker, completed, outcomes = _start_call(client.ping)
        try:
            assert _read_request(peer)
            response = _prefixed(_ack(0))
            for receive_count, byte in enumerate(response, start=1):
                peer.sendall(bytes((byte,)))
                assert observed.wait_for_receive_count(receive_count)
            assert completed.wait(_WAIT_SECONDS)
        finally:
            _join_worker(worker, completed, peer)

        assert outcomes == [None]
        assert observed.received_chunks == tuple(bytes((byte,)) for byte in response)
        assert client.connected is True


def test_close_interrupts_in_flight_request_before_waiting_for_client_lock() -> None:
    with _established_session() as (client, peer, _observed):
        request_worker, request_completed, request_outcomes = _start_call(client.ping)
        assert _read_request(peer)
        close_worker, close_completed, close_outcomes = _start_call(client.close)
        try:
            assert close_completed.wait(0.2), "close waited behind blocked request"
            assert request_completed.wait(0.2)
        finally:
            _join_worker(close_worker, close_completed, peer)
            _join_worker(request_worker, request_completed, peer)

        assert close_outcomes == [None]
        assert len(request_outcomes) == 1
        assert isinstance(request_outcomes[0], ProtocolError)
        assert request_outcomes[0].code == "sidecar_disconnected"
        _assert_session_poisoned(client)


def test_deadline_covers_trickled_prefix_and_record_payload(monkeypatch) -> None:
    """Progress on individual bytes cannot restart the operation deadline."""

    clock = _FakeClock()
    monkeypatch.setattr(
        sidecar_client_module,
        "time",
        SimpleNamespace(monotonic=clock.monotonic),
    )
    with _established_session(on_receive=clock.advance) as (client, peer, observed):
        worker, completed, outcomes = _start_call(
            lambda: client.cancel(_MESSAGE_ID, timeout=1.0)
        )
        try:
            assert _read_request(peer)
            response = _prefixed(_ack(0, _MESSAGE_ID))
            for receive_count, byte in enumerate(response, start=1):
                if completed.is_set():
                    break
                try:
                    peer.sendall(bytes((byte,)))
                except OSError:
                    break
                if not observed.wait_for_receive_count(receive_count):
                    assert completed.wait(_WAIT_SECONDS)
                    break
            assert completed.wait(_WAIT_SECONDS)
        finally:
            _join_worker(worker, completed, peer)

        assert len(observed.received_chunks) == 10
        assert b"".join(observed.received_chunks) == response[:10]
        assert len(outcomes) == 1
        assert isinstance(outcomes[0], socket.timeout)
        _assert_session_poisoned(client)


_BAD_TAG_RECORD = bytearray(_ack(0))
_BAD_TAG_RECORD[-1] ^= 1


@pytest.mark.parametrize(
    ("wire_response", "error_type", "error_code"),
    [
        pytest.param(
            struct.pack(">I", 0),
            ProtocolError,
            "invalid_length",
            id="zero-length-prefix",
        ),
        pytest.param(
            struct.pack(">I", sidecar_client_module._MAX_RECORD_BYTES + 1),
            ProtocolError,
            "invalid_length",
            id="oversized-length-prefix",
        ),
        pytest.param(
            _prefixed(b"x"),
            AuthenticationError,
            "invalid_record_length",
            id="record-shorter-than-header-and-tag",
        ),
        pytest.param(
            _prefixed(_record_with_mismatched_payload_length()),
            ProtocolError,
            "record_length_mismatch",
            id="authenticated-payload-length-mismatch",
        ),
        pytest.param(
            _prefixed(bytes(_BAD_TAG_RECORD)),
            AuthenticationError,
            "invalid_record_tag",
            id="tampered-authentication-tag",
        ),
    ],
)
def test_malformed_canonical_response_is_rejected_and_poisons_session(
    wire_response: bytes,
    error_type: type[ProtocolError] | type[AuthenticationError],
    error_code: str,
) -> None:
    with _established_session() as (client, peer, _observed):
        peer.sendall(wire_response)
        with pytest.raises(error_type) as raised:
            client.ping()

        assert raised.value.code == error_code
        _assert_session_poisoned(client)


_VALID_ACK_FRAME = _prefixed(_ack(0))


@pytest.mark.parametrize(
    "truncated_response",
    [
        pytest.param(_VALID_ACK_FRAME[:3], id="truncated-length-prefix"),
        pytest.param(_VALID_ACK_FRAME[:-1], id="truncated-record-body"),
    ],
)
def test_truncated_canonical_response_is_disconnection_not_success(
    truncated_response: bytes,
) -> None:
    with _established_session() as (client, peer, _observed):
        peer.sendall(truncated_response)
        peer.shutdown(socket.SHUT_WR)

        with pytest.raises(ProtocolError) as raised:
            client.ping()

        assert raised.value.code == "sidecar_disconnected"
        _assert_session_poisoned(client)


def test_authenticated_stale_response_sequence_is_rejected() -> None:
    with _established_session() as (client, peer, _observed):
        peer.sendall(_prefixed(_ack(0)) + _prefixed(_ack(0)))
        client.ping()
        assert client.connected is True

        with pytest.raises(AuthenticationError) as raised:
            client.ping()

        assert raised.value.code == "invalid_sequence"
        _assert_session_poisoned(client)


def test_authenticated_future_response_sequence_is_rejected() -> None:
    with _established_session() as (client, peer, _observed):
        peer.sendall(_prefixed(_ack(1)))

        with pytest.raises(AuthenticationError) as raised:
            client.ping()

        assert raised.value.code == "invalid_sequence"
        _assert_session_poisoned(client)


def test_inbound_admission_snapshot_requires_exact_authenticated_envelope(monkeypatch) -> None:
    client = SidecarClient("/unused", b"b" * 32, timeout=_WAIT_SECONDS)
    payload = json.dumps({
        "protocol": "mycelium.iroh_sidecar.inbound_admission.v1",
        "inbound_identity_rejections": 2,
        "inbound_frames_admitted": 3,
        "candidate_identity_rejections": 1,
        "measured_at_unix_ms": 1234,
    }).encode()
    monkeypatch.setattr(
        client,
        "_request",
        lambda *args, **kwargs: (
            sidecar_client_module._ADMISSION_COUNTERS,
            args[1],
            payload,
        ),
    )

    assert client.inbound_admission_snapshot("ab" * 32) == {
        "protocol": "mycelium.iroh_sidecar.inbound_admission.v1",
        "inbound_identity_rejections": 2,
        "inbound_frames_admitted": 3,
        "candidate_identity_rejections": 1,
        "measured_at_unix_ms": 1234,
    }


def test_inbound_admission_snapshot_rejects_extra_fields(monkeypatch) -> None:
    client = SidecarClient("/unused", b"b" * 32, timeout=_WAIT_SECONDS)
    payload = json.dumps({
        "protocol": "mycelium.iroh_sidecar.inbound_admission.v1",
        "inbound_identity_rejections": 1,
        "inbound_frames_admitted": 0,
        "candidate_identity_rejections": 1,
        "measured_at_unix_ms": 1234,
        "raw_endpoint_id": "forbidden",
    }).encode()
    monkeypatch.setattr(
        client,
        "_request",
        lambda *args, **kwargs: (
            sidecar_client_module._ADMISSION_COUNTERS,
            args[1],
            payload,
        ),
    )

    with pytest.raises(ProtocolError, match="invalid_inbound_admission_snapshot"):
        client.inbound_admission_snapshot("ab" * 32)
