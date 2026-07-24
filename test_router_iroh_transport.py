# SPDX-License-Identifier: AGPL-3.0-or-later
"""Focused contract tests for the production Router-to-iroh adapter."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from pathlib import Path
import socket
import struct
import threading
import time

import pytest

from mycelium_iroh_sidecar import ProtocolError, SidecarClient
from mycelium_iroh_sidecar import client as sidecar_client_module
from mycelium_router.contracts import TokenEvent
from mycelium_router.transports.iroh import (
    PROCESS_LIFETIME_LIMITATION,
    IrohTransport,
    IrohTransportError,
    PeerBinding,
    _bounded_trace_identity,
)
from mycelium_router.wire import ROUTER_WIRE_PROTOCOL, encode_frame


@dataclass
class _Hub:
    endpoint_id: str = "local-endpoint"

    def __post_init__(self) -> None:
        self.clients: list[_FakeClient] = []
        self.inbound: deque[tuple[bytes, int, bytes] | BaseException] = deque()
        self.inbound_ready = threading.Condition()
        self.sent: list[tuple[bytes, bytes, float | None, int]] = []
        self.acks: list[bytes] = []
        self.cancels: list[bytes] = []
        self.configurations: list[tuple[str, dict, int]] = []
        self.confirmed_send_entered = threading.Event()
        self.retry_confirmed_send_entered = threading.Event()
        self.release_confirmed_send = threading.Event()
        self.block_confirmed_send = False
        self.confirmed_send_timeouts: list[float | None] = []
        self.send_failure: BaseException | None = None
        self.send_failures: deque[BaseException] = deque()
        self.connect_delay = 0.0
        self.block_connect = False
        self.connect_entered = threading.Event()
        self.release_connect = threading.Event()
        self.cancel_entered = threading.Event()
        self.release_cancel = threading.Event()
        self.cancel_completed = threading.Event()
        self.block_cancel = False

    def client(self, *_args, **_kwargs) -> "_FakeClient":
        client = _FakeClient(self)
        self.clients.append(client)
        return client

    def deliver(self, message_id: bytes, frame: bytes, *, generation: int = 7) -> None:
        with self.inbound_ready:
            self.inbound.append((message_id, generation, frame))
            self.inbound_ready.notify_all()

    def fail_receive(self, error: BaseException) -> None:
        with self.inbound_ready:
            self.inbound.append(error)
            self.inbound_ready.notify_all()


class _FakeClient:
    def __init__(self, hub: _Hub):
        self.hub = hub
        self.connected = False
        self.endpoint_id = None

    def connect(self, *, deadline: float | None = None) -> None:
        del deadline
        self.hub.connect_entered.set()
        if self.hub.block_connect:
            self.hub.release_connect.wait(timeout=1)
        if self.hub.connect_delay:
            time.sleep(self.hub.connect_delay)
        self.connected = True
        self.endpoint_id = self.hub.endpoint_id

    def close(self) -> None:
        self.connected = False
        with self.hub.inbound_ready:
            self.hub.inbound_ready.notify_all()

    def configure_peer(
        self,
        endpoint_id: str,
        endpoint_addr: dict,
        *,
        generation: int,
        timeout: float | None = None,
    ) -> None:
        del timeout
        if not self.connected:
            raise ProtocolError("not_connected")
        self.hub.configurations.append((endpoint_id, endpoint_addr, generation))

    def send_confirmed(
        self,
        frame: bytes,
        message_id: bytes,
        *,
        timeout: float | None = None,
        expected_generation: int,
    ) -> bytes:
        if not self.connected:
            raise ProtocolError("not_connected")
        self.hub.confirmed_send_timeouts.append(timeout)
        self.hub.confirmed_send_entered.set()
        if len(self.hub.confirmed_send_timeouts) == 2:
            self.hub.retry_confirmed_send_entered.set()
        if self.hub.send_failures:
            raise self.hub.send_failures.popleft()
        if self.hub.block_confirmed_send:
            if not self.hub.release_confirmed_send.wait(timeout=timeout):
                raise TimeoutError("confirmed delivery deadline")
        if self.hub.send_failure is not None:
            raise self.hub.send_failure
        self.hub.sent.append((message_id, frame, timeout, expected_generation))
        return message_id

    def recv_with_generation(self, *, timeout: float | None = None):
        deadline = time.monotonic() + (timeout or 0.1)
        with self.hub.inbound_ready:
            while not self.hub.inbound:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("empty")
                self.hub.inbound_ready.wait(remaining)
            value = self.hub.inbound.popleft()
        if isinstance(value, BaseException):
            raise value
        return value

    def recv(self, *, timeout: float | None = None):
        message_id, _generation, frame = self.recv_with_generation(timeout=timeout)
        return message_id, frame

    def ack(self, message_id: bytes) -> None:
        self.hub.acks.append(message_id)

    def cancel(self, message_id: bytes, *, timeout: float | None = None) -> None:
        del timeout
        self.hub.cancel_entered.set()
        if self.hub.block_cancel:
            self.hub.release_cancel.wait()
        self.hub.cancels.append(message_id)
        self.hub.cancel_completed.set()


class _PausedAcquireSemaphore:
    """Pause first successful non-blocking acquire at a deterministic race point."""

    def __init__(self, delegate) -> None:
        self._delegate = delegate
        self.entered = threading.Event()
        self.resume = threading.Event()
        self._pause_next = True
        self._lock = threading.Lock()

    def acquire(self, *args, **kwargs) -> bool:
        acquired = self._delegate.acquire(*args, **kwargs)
        should_pause = False
        if acquired:
            with self._lock:
                if self._pause_next:
                    self._pause_next = False
                    should_pause = True
        if should_pause:
            self.entered.set()
            assert self.resume.wait(timeout=1.0)
        return acquired

    def release(self) -> None:
        self._delegate.release()

    def permit_available(self) -> bool:
        acquired = self._delegate.acquire(blocking=False)
        if acquired:
            self._delegate.release()
        return acquired


class _RecordingRouter:
    def __init__(self) -> None:
        self.token_events: list[tuple[TokenEvent, str | None]] = []
        self.received = threading.Event()

    def receive_token_event(self, event, *, source_node_id=None):
        self.token_events.append((event, source_node_id))
        self.received.set()
        return True


class _RejectingManifestRouter:
    def __init__(self) -> None:
        self.registration_attempted = threading.Event()

    def register_path(self, *args, **kwargs):
        self.registration_attempted.set()
        return False


def _binding(*, generation: int = 7) -> PeerBinding:
    return PeerBinding(
        node_id="peer-node",
        endpoint_id="peer-endpoint",
        endpoint_addr={"id": "peer-endpoint", "addrs": ["127.0.0.1:1"]},
        generation=generation,
    )


def _event_frame(token_id: int = 101) -> bytes:
    return encode_frame(
        TokenEvent(
            request_id="request-1",
            path_id="path-1",
            path_attempt=0,
            token_index=0,
            token_id=token_id,
            sampling_counter=1,
        )
    )


def _transport(
    hub: _Hub,
    *,
    queue_capacity: int = 2,
    delivery_timeout_seconds: float = 0.2,
    expected_endpoint_id: str = "local-endpoint",
) -> IrohTransport:
    return IrohTransport(
        node_id="local-node",
        socket_path="/unused",
        bootstrap_secret=b"s" * 32,
        peer=_binding(),
        expected_endpoint_id=expected_endpoint_id,
        queue_capacity=queue_capacity,
        delivery_timeout_seconds=delivery_timeout_seconds,
        poll_interval_seconds=0.01,
        client_factory=hub.client,
    )


def test_remote_router_frame_uses_confirmed_sidecar_path_and_canonical_wire() -> None:
    hub = _Hub()
    transport = _transport(hub)
    transport.bind_router(_RecordingRouter())
    transport.start()
    try:
        receipt = transport.send_router_frame(
            _event_frame(), destination_node_id="peer-node"
        )
    finally:
        transport.close()

    assert len(hub.sent) == 1
    assert hub.sent[0][1] == _event_frame()
    assert receipt.message_id == hub.sent[0][0]
    assert receipt.peer_endpoint_id == "peer-endpoint"
    assert receipt.peer_generation == 7
    assert hub.sent[0][3] == 7
    assert receipt.semantics == "remote_router_dispatch_ack"
    assert receipt.router_protocol == ROUTER_WIRE_PROTOCOL
    assert transport.route_ready is False


def test_outbound_trace_binds_public_request_and_bounded_frame_identity() -> None:
    hub = _Hub()
    transport = _transport(hub)
    transport.bind_router(_RecordingRouter())
    transport.start()
    try:
        transport.remember_entry("request-1", "local-node")
        transport._entry_nodes["request-1"] = "peer-node"
        transport.send_token_event(
            TokenEvent(
                request_id="request-1",
                path_id="path-1",
                path_attempt=0,
                token_index=7,
                token_id=987_654_321,
                sampling_counter=8,
            )
        )
        long_request = "public-" + "x" * 2_048
        transport._entry_nodes[long_request] = "peer-node"
        transport.send_token_event(
            TokenEvent(
                request_id=long_request,
                path_id="path-long",
                path_attempt=0,
                token_index=8,
                token_id=123_456_789,
                sampling_counter=9,
            )
        )
        trace = transport.outbound_trace
    finally:
        transport.close()

    assert len(trace) == 2
    assert "request-1" in trace[0]
    assert "TokenEvent" in trace[0] and '"token_index":7' in trace[0]
    assert "987654321" not in trace[0]
    assert "request_id_sha256" in trace[1] and long_request not in trace[1]
    assert "123456789" not in trace[1]
    assert all(len(entry.encode()) <= 512 for entry in trace)


def test_trace_identity_omits_overlong_public_fields_without_leaking_them() -> None:
    sensitive_request = "request-sensitive-" + "r" * 2_048
    sensitive_phase = "phase-sensitive-" + "p" * 2_048
    sensitive_token = int("7" * 800)
    identity = _bounded_trace_identity(
        type(
            "TraceMessage",
            (),
            {
                "request_id": sensitive_request,
                "phase": sensitive_phase,
                "token_index": sensitive_token,
            },
        )()
    )
    entry = f"TokenEvent->peer:remote:{identity}"

    assert len(identity.encode()) <= 512
    assert len(entry.encode()) <= 512
    assert "request_id_sha256" in identity
    assert all(
        value not in identity
        for value in (sensitive_request, sensitive_phase, str(sensitive_token))
    )


def test_start_binds_expected_authenticated_local_endpoint_and_exact_peer_generation() -> None:
    hub = _Hub(endpoint_id="unexpected")
    transport = _transport(hub)
    transport.bind_router(_RecordingRouter())
    with pytest.raises(IrohTransportError, match="local_endpoint_mismatch"):
        transport.start()
    transport.close()

    good = _Hub()
    transport = _transport(good)
    transport.bind_router(_RecordingRouter())
    transport.start()
    try:
        assert good.configurations == [
            ("peer-endpoint", _binding().endpoint_addr, 7)
        ]
        assert transport.peer_binding == _binding()
    finally:
        transport.close()


def test_authenticated_inbound_is_acked_only_after_bounded_adapter_admission() -> None:
    hub = _Hub()
    router = _RecordingRouter()
    transport = _transport(hub)
    transport.bind_router(router)
    transport.start()
    try:
        message_id = b"a" * 16
        hub.deliver(message_id, _event_frame())
        assert router.received.wait(1)
        deadline = time.monotonic() + 1
        while not hub.acks and time.monotonic() < deadline:
            time.sleep(0.01)
        assert hub.acks == [message_id]
        assert router.token_events == [
            (
                TokenEvent(
                    request_id="request-1",
                    path_id="path-1",
                    path_attempt=0,
                    token_index=0,
                    token_id=101,
                    sampling_counter=1,
                ),
                "peer-node",
            )
        ]
        evidence = transport.evidence()
        deadline = time.monotonic() + 1
        while evidence.remote_frames_received != 1 and time.monotonic() < deadline:
            time.sleep(0.01)
            evidence = transport.evidence()
        assert evidence.remote_frames_received == 1
        assert evidence.router_frames_dispatched == 1
        assert evidence.peer_endpoint_id == "peer-endpoint"
        assert evidence.peer_generation == 7
    finally:
        transport.close()


def test_stale_inbound_generation_is_never_dispatched_or_acked() -> None:
    hub = _Hub()
    router = _RecordingRouter()
    transport = _transport(hub)
    transport.bind_router(router)
    transport.start()
    try:
        hub.deliver(b"s" * 16, _event_frame(), generation=6)
        deadline = time.monotonic() + 1
        while transport.fatal_error is None and time.monotonic() < deadline:
            time.sleep(0.01)
        assert transport.fatal_error is not None
        assert transport.fatal_error.code == "peer_rotated"
        assert hub.acks == []
        assert router.token_events == []
    finally:
        transport.close()


def test_duplicate_delivery_is_acked_but_dispatched_once() -> None:
    hub = _Hub()
    router = _RecordingRouter()
    transport = _transport(hub)
    transport.bind_router(router)
    transport.start()
    try:
        message_id = b"d" * 16
        frame = _event_frame()
        hub.deliver(message_id, frame)
        hub.deliver(message_id, frame)
        deadline = time.monotonic() + 1
        while len(hub.acks) < 2 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert hub.acks == [message_id, message_id]
        assert len(router.token_events) == 1
        evidence = transport.evidence()
        deadline = time.monotonic() + 1
        while evidence.duplicate_frames != 1 and time.monotonic() < deadline:
            time.sleep(0.01)
            evidence = transport.evidence()
        assert evidence.duplicate_frames == 1
    finally:
        transport.close()


def test_malformed_inbound_frame_is_never_acked_or_dispatched() -> None:
    hub = _Hub()
    router = _RecordingRouter()
    transport = _transport(hub)
    transport.bind_router(router)
    transport.start()
    try:
        hub.deliver(b"m" * 16, b"not-router-wire")
        deadline = time.monotonic() + 1
        while transport.fatal_error is None and time.monotonic() < deadline:
            time.sleep(0.01)
        assert transport.fatal_error is not None
        assert transport.fatal_error.code == "malformed_router_frame"
        assert hub.acks == []
        assert router.token_events == []
    finally:
        transport.close()


def test_rejected_manifest_registration_is_never_acked_as_delivered() -> None:
    hub = _Hub()
    router = _RejectingManifestRouter()
    transport = _transport(hub)
    transport.bind_router(router)
    transport.start()
    try:
        message_id = b"j" * 16
        frame = (
            Path(__file__).parent
            / "contracts"
            / "router-wire-golden"
            / "03-manifest-locked.bin"
        ).read_bytes()
        hub.deliver(message_id, frame)
        assert router.registration_attempted.wait(1)
        deadline = time.monotonic() + 1
        while transport.fatal_error is None and time.monotonic() < deadline:
            time.sleep(0.01)
        assert transport.fatal_error is not None
        assert transport.fatal_error.code == "manifest_registration_rejected"
        assert hub.acks == []
    finally:
        transport.close()


def test_bounded_send_queue_fails_closed_and_deadline_cancels_message() -> None:
    hub = _Hub()
    hub.block_confirmed_send = True
    transport = _transport(hub, queue_capacity=1, delivery_timeout_seconds=0.1)
    transport.bind_router(_RecordingRouter())
    transport.start()
    first_error: list[BaseException] = []

    def first_send() -> None:
        try:
            transport.send_router_frame(
                _event_frame(), destination_node_id="peer-node"
            )
        except BaseException as error:  # captured for assertion in test thread
            first_error.append(error)

    first = threading.Thread(target=first_send)
    first.start()
    assert hub.confirmed_send_entered.wait(1)
    with pytest.raises(IrohTransportError, match="adapter_queue_full"):
        transport.send_router_frame(_event_frame(102), destination_node_id="peer-node")
    first.join(timeout=1)
    try:
        assert not first.is_alive()
        assert len(first_error) == 1
        assert isinstance(first_error[0], IrohTransportError)
        assert first_error[0].code == "delivery_deadline_exceeded"
        assert len(hub.cancels) == 1
    finally:
        hub.release_confirmed_send.set()
        transport.close()


def test_rotation_is_monotonic_and_cancels_old_generation_inflight() -> None:
    hub = _Hub()
    hub.block_confirmed_send = True
    transport = _transport(hub, delivery_timeout_seconds=1.0)
    transport.bind_router(_RecordingRouter())
    transport.start()
    errors: list[BaseException] = []

    def send() -> None:
        try:
            transport.send_router_frame(
                _event_frame(), destination_node_id="peer-node"
            )
        except BaseException as error:
            errors.append(error)

    thread = threading.Thread(target=send)
    thread.start()
    assert hub.confirmed_send_entered.wait(1)
    with pytest.raises(IrohTransportError, match="stale_peer_generation"):
        transport.rotate_peer(_binding(generation=7))
    transport.rotate_peer(_binding(generation=8))
    hub.release_confirmed_send.set()
    thread.join(timeout=1)
    try:
        assert not thread.is_alive()
        assert errors and isinstance(errors[0], IrohTransportError)
        assert errors[0].code == "peer_rotated"
        assert len(hub.cancels) == 1
        assert hub.configurations[-1][2] == 8
        assert transport.peer_binding.generation == 8
    finally:
        transport.close()


def test_clean_shutdown_rejects_new_work_and_leaves_no_threads() -> None:
    hub = _Hub()
    transport = _transport(hub)
    transport.bind_router(_RecordingRouter())
    transport.start()
    transport.close()
    transport.close()

    assert transport.running is False
    assert transport.worker_threads_alive == 0
    with pytest.raises(IrohTransportError, match="transport_closed"):
        transport.send_router_frame(_event_frame(), destination_node_id="peer-node")


def test_sequence_gap_is_fatal_and_never_acked() -> None:
    hub = _Hub()
    transport = _transport(hub)
    transport.bind_router(_RecordingRouter())
    transport.start()
    try:
        hub.fail_receive(ProtocolError("sequence_gap"))
        deadline = time.monotonic() + 1
        while transport.fatal_error is None and time.monotonic() < deadline:
            time.sleep(0.01)
        assert transport.fatal_error is not None
        assert transport.fatal_error.code == "sequence_gap"
        assert hub.acks == []
    finally:
        transport.close()


def test_same_message_id_with_different_frame_is_fatal_replay_collision() -> None:
    hub = _Hub()
    router = _RecordingRouter()
    transport = _transport(hub)
    transport.bind_router(router)
    transport.start()
    try:
        message_id = b"r" * 16
        hub.deliver(message_id, _event_frame(101))
        assert router.received.wait(1)
        deadline = time.monotonic() + 1
        while not hub.acks and time.monotonic() < deadline:
            time.sleep(0.01)
        assert hub.acks == [message_id]
        hub.deliver(message_id, _event_frame(102))
        deadline = time.monotonic() + 1
        while transport.fatal_error is None and time.monotonic() < deadline:
            time.sleep(0.01)
        assert transport.fatal_error is not None
        assert transport.fatal_error.code == "replay_collision"
        assert hub.acks == [message_id]
        assert len(router.token_events) == 1
    finally:
        transport.close()


def test_sidecar_crash_never_returns_false_delivery_success() -> None:
    hub = _Hub()
    hub.send_failure = ConnectionResetError("sidecar crashed")
    transport = _transport(hub)
    transport.bind_router(_RecordingRouter())
    transport.start()
    try:
        with pytest.raises(IrohTransportError, match="delivery_not_confirmed"):
            transport.send_router_frame(
                _event_frame(), destination_node_id="peer-node"
            )
        assert transport.evidence().remote_frames_sent == 0
    finally:
        transport.close()


def test_delivery_process_lifetime_limit_is_explicit_and_route_stays_unready() -> None:
    hub = _Hub()
    transport = _transport(hub)
    transport.bind_router(_RecordingRouter())
    transport.start()
    try:
        evidence = transport.evidence()
        assert evidence.process_lifetime_limitation == PROCESS_LIFETIME_LIMITATION
        assert "not durable" in evidence.process_lifetime_limitation
        assert evidence.delivery_semantics == "remote_router_dispatch_ack"
        assert evidence.route_ready is False
    finally:
        transport.close()


def test_receive_reconnects_after_sidecar_disconnected_protocol_error() -> None:
    hub = _Hub()
    router = _RecordingRouter()
    transport = _transport(hub)
    transport.bind_router(router)
    transport.start()
    try:
        hub.fail_receive(ProtocolError("sidecar_disconnected"))
        deadline = time.monotonic() + 1
        while len(hub.clients) < 4 and time.monotonic() < deadline:
            time.sleep(0.01)
        hub.deliver(b"p" * 16, _event_frame())
        while not hub.acks and time.monotonic() < deadline:
            time.sleep(0.01)
        assert hub.acks == [b"p" * 16]
        assert len(router.token_events) == 1
        assert transport.fatal_error is None
    finally:
        transport.close()


def test_local_dispatch_after_close_is_rejected_without_router_mutation() -> None:
    hub = _Hub()
    router = _RecordingRouter()
    transport = _transport(hub)
    transport.bind_router(router)
    transport.start()
    transport.close()
    event = TokenEvent(
        request_id="req-1",
        path_id="path-1",
        path_attempt=0,
        token_index=0,
        token_id=101,
        sampling_counter=1,
    )
    transport._entry_nodes[event.request_id] = "local-node"
    with pytest.raises(IrohTransportError, match="transport_closed"):
        transport.send_token_event(event)
    assert router.token_events == []


def test_close_wins_send_admission_race_without_leaking_pending_or_permit() -> None:
    hub = _Hub()
    transport = _transport(hub, queue_capacity=1)
    transport.bind_router(_RecordingRouter())
    transport.start()
    paused = _PausedAcquireSemaphore(transport._send_slots)
    transport.__dict__["_send_slots"] = paused
    results: list[object] = []
    errors: list[BaseException] = []

    def send() -> None:
        try:
            results.append(
                transport.send_router_frame(
                    _event_frame(), destination_node_id="peer-node"
                )
            )
        except BaseException as error:
            errors.append(error)

    sender = threading.Thread(target=send, name="send-admission-close-race")
    sender.start()
    assert paused.entered.wait(timeout=1.0)
    transport.close()
    paused.resume.set()
    sender.join(timeout=1.0)

    assert not sender.is_alive()
    assert results == []
    assert len(errors) == 1
    assert isinstance(errors[0], IrohTransportError)
    assert errors[0].code == "transport_closed"
    assert transport._pending == {}
    assert paused.permit_available()
    assert hub.sent == []


def test_concurrent_starts_create_one_client_set() -> None:
    hub = _Hub()
    transport = _transport(hub)
    transport.bind_router(_RecordingRouter())
    entered = threading.Event()
    release = threading.Event()
    errors: list[BaseException] = []
    original = transport._configure_peer

    def blocking_configure(client, binding) -> None:
        entered.set()
        assert release.wait(timeout=1)
        original(client, binding)

    def start() -> None:
        try:
            transport.start()
        except BaseException as error:
            errors.append(error)

    transport._configure_peer = blocking_configure
    first = threading.Thread(target=start)
    second = threading.Thread(target=start)
    first.start()
    assert entered.wait(timeout=1)
    second.start()
    time.sleep(0.05)
    try:
        assert len(hub.clients) == 3
    finally:
        release.set()
        first.join(timeout=1)
        second.join(timeout=1)
        transport.close()
    assert errors == []
    assert len(hub.clients) == 3


def test_concurrent_rotations_commit_in_generation_order() -> None:
    hub = _Hub()
    transport = _transport(hub)
    transport.bind_router(_RecordingRouter())
    transport.start()
    first_entered = threading.Event()
    release_first = threading.Event()
    errors: list[BaseException] = []
    original = transport._configure_peer

    def ordered_configure(client, binding) -> None:
        if binding.generation == 8:
            first_entered.set()
            assert release_first.wait(timeout=1)
        original(client, binding)

    def rotate(binding: PeerBinding) -> None:
        try:
            transport.rotate_peer(binding)
        except BaseException as error:
            errors.append(error)

    transport._configure_peer = ordered_configure
    first = threading.Thread(
        target=rotate,
        args=(PeerBinding("peer-node", "peer-8", {"id": "peer-8"}, 8),),
    )
    second = threading.Thread(
        target=rotate,
        args=(PeerBinding("peer-node", "peer-9", {"id": "peer-9"}, 9),),
    )
    first.start()
    assert first_entered.wait(timeout=1)
    second.start()
    time.sleep(0.05)
    release_first.set()
    first.join(timeout=1)
    second.join(timeout=1)
    try:
        assert errors == []
        assert transport.peer_binding.generation == 9
        assert transport.peer_binding.endpoint_id == "peer-9"
    finally:
        transport.close()


def test_manifest_delta_sink_is_bounded_and_never_false_acks() -> None:
    hub = _Hub()
    transport = _transport(hub, queue_capacity=1)
    transport.bind_router(_RecordingRouter())
    transport.start()
    frame = (
        Path(__file__).parent
        / "contracts"
        / "router-wire-golden"
        / "02-manifest-delta.bin"
    ).read_bytes()
    try:
        hub.deliver(b"1" * 16, frame)
        deadline = time.monotonic() + 1
        while len(hub.acks) < 1 and time.monotonic() < deadline:
            time.sleep(0.01)
        hub.deliver(b"2" * 16, frame)
        while transport.fatal_error is None and time.monotonic() < deadline:
            time.sleep(0.01)
        assert transport.fatal_error is not None
        assert transport.fatal_error.code == "manifest_delta_queue_full"
        assert hub.acks == [b"1" * 16]
    finally:
        transport.close()


def test_close_reports_blocked_router_callback_instead_of_false_clean_shutdown() -> None:
    hub = _Hub()
    entered = threading.Event()
    release = threading.Event()

    class BlockingRouter(_RecordingRouter):
        def receive_token_event(
            self, event: TokenEvent, *, source_node_id: str | None = None
        ):
            entered.set()
            assert release.wait(timeout=5)
            return super().receive_token_event(event, source_node_id=source_node_id)

    transport = _transport(hub)
    transport.bind_router(BlockingRouter())
    transport.start()
    hub.deliver(b"b" * 16, _event_frame())
    assert entered.wait(timeout=1)
    try:
        with pytest.raises(IrohTransportError, match="dispatcher_shutdown_timeout"):
            transport.close()
        assert transport.worker_threads_alive == 1
    finally:
        release.set()
        transport.close()
    assert transport.worker_threads_alive == 0


def test_confirmed_send_deadline_includes_wait_for_client_lock() -> None:
    client = SidecarClient("/unused", b"s" * 32, timeout=0.05)
    client._peer_generation = 7
    errors: list[BaseException] = []
    completed = threading.Event()
    client._lock.acquire()

    def send() -> None:
        try:
            client.send_confirmed(
                _event_frame(),
                b"d" * 16,
                timeout=0.05,
                expected_generation=7,
            )
        except BaseException as error:
            errors.append(error)
        finally:
            completed.set()

    thread = threading.Thread(target=send)
    thread.start()
    try:
        assert completed.wait(timeout=0.2)
    finally:
        client._lock.release()
        thread.join(timeout=1)
    assert len(errors) == 1
    assert isinstance(errors[0], TimeoutError)


def test_reconnect_retry_uses_original_end_to_end_deadline() -> None:
    hub = _Hub()
    delivery_timeout = 0.3
    transport = _transport(hub, delivery_timeout_seconds=delivery_timeout)
    transport.bind_router(_RecordingRouter())
    transport.start()
    hub.send_failures.append(ProtocolError("sidecar_disconnected"))
    hub.connect_delay = 0.05
    hub.block_confirmed_send = True
    hub.block_cancel = True
    errors: list[BaseException] = []
    elapsed: list[float] = []
    started = time.monotonic()

    def send() -> None:
        try:
            transport.send_router_frame(
                _event_frame(), destination_node_id="peer-node"
            )
        except BaseException as error:
            errors.append(error)
        finally:
            elapsed.append(time.monotonic() - started)

    thread = threading.Thread(target=send)
    thread.start()
    try:
        assert hub.retry_confirmed_send_entered.wait(timeout=1)
        assert len(hub.confirmed_send_timeouts) == 2
        retry_timeout = hub.confirmed_send_timeouts[1]
        assert retry_timeout is not None
        assert 0 < retry_timeout < delivery_timeout - 0.02
        thread.join(timeout=1)
        assert not thread.is_alive()
        assert len(errors) == 1
        assert isinstance(errors[0], IrohTransportError)
        assert errors[0].code == "delivery_deadline_exceeded"
        assert len(elapsed) == 1 and elapsed[0] < delivery_timeout + 0.08
        assert hub.cancel_entered.wait(timeout=1)
        assert hub.cancels == []
        hub.release_cancel.set()
        assert hub.cancel_completed.wait(timeout=1)
        assert len(hub.cancels) == 1
    finally:
        hub.release_confirmed_send.set()
        hub.release_cancel.set()
        thread.join(timeout=1)
        transport.close()


def test_reconnect_can_exhaust_deadline_before_retry_confirmed_send() -> None:
    hub = _Hub()
    transport = _transport(hub, delivery_timeout_seconds=0.05)
    transport.bind_router(_RecordingRouter())
    transport.start()
    hub.send_failures.append(ProtocolError("sidecar_disconnected"))
    hub.connect_delay = 0.08
    try:
        with pytest.raises(IrohTransportError) as raised:
            transport.send_router_frame(
                _event_frame(), destination_node_id="peer-node"
            )
        assert raised.value.code == "delivery_deadline_exceeded"
        assert len(hub.confirmed_send_timeouts) == 1
        assert not hub.retry_confirmed_send_entered.is_set()
    finally:
        transport.close()


def test_client_confirmed_send_deadline_covers_trickled_response() -> None:
    client_stream, server_stream = socket.socketpair()
    client = SidecarClient("/unused", b"s" * 32, timeout=1.0)
    client._socket = client_stream
    client._send_key = bytearray(b"a" * 32)
    client._receive_key = bytearray(b"b" * 32)
    client._peer_generation = 1
    message_id = b"t" * 16

    def trickle_ack() -> None:
        try:
            request_length = struct.unpack(">I", server_stream.recv(4))[0]
            request = bytearray()
            while len(request) < request_length:
                request.extend(server_stream.recv(request_length - len(request)))
            encoded = sidecar_client_module._encode_record(
                sidecar_client_module._ACK,
                0,
                message_id,
                b"",
                b"b" * 32,
            )
            response = struct.pack(">I", len(encoded)) + encoded
            for byte in response:
                server_stream.sendall(bytes([byte]))
                time.sleep(0.02)
        except (BrokenPipeError, OSError):
            pass

    thread = threading.Thread(target=trickle_ack, daemon=True)
    thread.start()
    started = time.monotonic()
    try:
        with pytest.raises(TimeoutError):
            client.send_confirmed(
                _event_frame(),
                message_id,
                timeout=0.05,
                expected_generation=1,
            )
    finally:
        elapsed = time.monotonic() - started
        client.close()
        server_stream.close()
        thread.join(timeout=1)
    assert elapsed < 0.15


def test_client_receive_deadline_covers_trickled_delivery() -> None:
    client_stream, server_stream = socket.socketpair()
    client = SidecarClient("/unused", b"s" * 32, timeout=1.0)
    client._socket = client_stream
    client._send_key = bytearray(b"a" * 32)
    client._receive_key = bytearray(b"b" * 32)
    message_id = b"r" * 16

    def trickle_delivery() -> None:
        try:
            request_length = struct.unpack(">I", server_stream.recv(4))[0]
            request = bytearray()
            while len(request) < request_length:
                request.extend(server_stream.recv(request_length - len(request)))
            encoded = sidecar_client_module._encode_record(
                sidecar_client_module._DELIVERY,
                0,
                message_id,
                struct.pack(">Q", 1) + _event_frame(),
                b"b" * 32,
            )
            response = struct.pack(">I", len(encoded)) + encoded
            for byte in response:
                server_stream.sendall(bytes([byte]))
                time.sleep(0.02)
        except (BrokenPipeError, OSError):
            pass

    thread = threading.Thread(target=trickle_delivery, daemon=True)
    thread.start()
    started = time.monotonic()
    try:
        with pytest.raises(TimeoutError):
            client.recv_with_generation(timeout=0.05)
    finally:
        elapsed = time.monotonic() - started
        client.close()
        server_stream.close()
        thread.join(timeout=1)
    assert elapsed < 0.15


def test_close_fences_receive_reconnect_install() -> None:
    hub = _Hub()
    transport = _transport(hub)
    transport.bind_router(_RecordingRouter())
    transport.start()
    hub.connect_entered.clear()
    hub.block_connect = True
    hub.fail_receive(ProtocolError("sidecar_disconnected"))
    assert hub.connect_entered.wait(timeout=1)

    closed: list[BaseException] = []

    def close() -> None:
        try:
            transport.close()
        except BaseException as error:
            closed.append(error)

    close_thread = threading.Thread(target=close)
    close_thread.start()
    hub.release_connect.set()
    close_thread.join(timeout=2)

    assert not close_thread.is_alive()
    assert not closed
    assert all(not client.connected for client in hub.clients)


def test_close_fences_reconnect_install_and_pending_receipt() -> None:
    hub = _Hub()
    transport = _transport(hub, delivery_timeout_seconds=1.0)
    transport.bind_router(_RecordingRouter())
    transport.start()
    hub.connect_entered.clear()
    hub.send_failures.append(ProtocolError("sidecar_disconnected"))
    hub.block_connect = True
    receipts: list[object] = []

    def send() -> None:
        try:
            receipts.append(
                transport.send_router_frame(
                    _event_frame(), destination_node_id="peer-node"
                )
            )
        except BaseException as error:
            receipts.append(error)

    thread = threading.Thread(target=send)
    thread.start()
    assert hub.connect_entered.wait(timeout=1)
    transport.close()
    hub.release_connect.set()
    thread.join(timeout=2)

    assert not thread.is_alive()
    assert len(receipts) == 1
    assert isinstance(receipts[0], IrohTransportError)
    assert receipts[0].code == "transport_closed"
    assert all(not client.connected for client in hub.clients)
    assert transport.evidence().remote_frames_sent == 0


def test_start_rejects_prior_fatal_receiver_state() -> None:
    hub = _Hub()
    transport = _transport(hub)
    transport.bind_router(_RecordingRouter())
    transport.start()
    transport._set_fatal(IrohTransportError("fatal_receive"))
    with pytest.raises(IrohTransportError, match="fatal_receive"):
        transport.start()
    transport.close()
