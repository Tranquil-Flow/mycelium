# SPDX-License-Identifier: AGPL-3.0-or-later
"""Focused contract tests for the production Router-to-iroh adapter."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
import json
from pathlib import Path
import socket
import struct
import threading
import time

import pytest

from mycelium_iroh_sidecar import ProtocolError, SidecarClient
from mycelium_iroh_sidecar import client as sidecar_client_module
import mycelium_router.transports.iroh as iroh_module
from mycelium_router.contracts import FailureReport, TokenEvent
from mycelium_router.transports.iroh import (
    PROCESS_LIFETIME_LIMITATION,
    DeliveryReceipt,
    IrohTransport,
    IrohTransportError,
    PeerBinding,
    _bounded_trace_identity,
    _delivery_receipt_digest,
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
        self.source_generations: list[int] = []
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
        self.cancel_timeouts: list[float | None] = []
        self.cancel_failure: BaseException | None = None

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
        source_generation: int,
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
        self.hub.source_generations.append(source_generation)
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
        self.hub.cancel_timeouts.append(timeout)
        self.hub.cancel_entered.set()
        if self.hub.block_cancel:
            self.hub.release_cancel.wait()
        if self.hub.cancel_failure is not None:
            raise self.hub.cancel_failure
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
    local_generation: int | None = None,
) -> IrohTransport:
    return IrohTransport(
        node_id="local-node",
        socket_path="/unused",
        bootstrap_secret=b"s" * 32,
        peer=_binding(),
        local_generation=local_generation,
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
    assert hub.source_generations == [7]
    assert receipt.message_id == hub.sent[0][0]
    assert receipt.peer_endpoint_id == "peer-endpoint"
    assert receipt.peer_generation == 7
    assert hub.sent[0][3] == 7
    assert receipt.semantics == "remote_router_dispatch_ack"
    assert receipt.router_protocol == ROUTER_WIRE_PROTOCOL
    assert transport.route_ready is False


def test_confirmed_send_binds_distinct_local_membership_generation() -> None:
    hub = _Hub()
    transport = _transport(hub, local_generation=3)
    transport.bind_router(_RecordingRouter())
    transport.start()
    try:
        transport.send_router_frame(
            _event_frame(), destination_node_id="peer-node"
        )
    finally:
        transport.close()

    assert hub.sent[0][3] == 7
    assert hub.source_generations == [3]


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

    assert len(trace) == 4
    assert "request-1" in trace[0]
    assert "TokenEvent" in trace[0] and '"token_index":7' in trace[0]
    assert "987654321" not in trace[0]
    assert trace[1].startswith("DeliveryReceipt->peer:remote:")
    assert "request_id_sha256" in trace[2] and long_request not in trace[2]
    assert "123456789" not in trace[2]
    assert trace[3].startswith("DeliveryReceipt->peer:remote:")
    for sent, message_trace, receipt_trace in zip(
        hub.sent,
        trace[::2],
        trace[1::2],
        strict=True,
    ):
        receipt_identity = json.loads(receipt_trace.partition(":remote:")[2])
        receipt = DeliveryReceipt(
            message_id=sent[0],
            peer_endpoint_id=receipt_identity["peer_endpoint_id"],
            peer_generation=receipt_identity["peer_generation"],
        )
        assert sent[0].hex() in message_trace
        assert receipt_identity["message_id"] == sent[0].hex()
        assert (
            receipt_identity["delivery_receipt_sha256"]
            == _delivery_receipt_digest(receipt)
        )
    assert all(len(entry.encode()) <= 512 for entry in trace)


def test_close_waits_for_confirmed_remote_receipt_trace_commit() -> None:
    hub = _Hub()
    transport = _transport(hub, delivery_timeout_seconds=0.5)
    transport.bind_router(_RecordingRouter())
    transport.start()
    transport._entry_nodes["request-1"] = "peer-node"
    confirmed = threading.Event()
    release = threading.Event()
    original_send = transport.send_router_frame
    send_errors: list[BaseException] = []
    close_errors: list[BaseException] = []

    def paused_send(*args, **kwargs):
        receipt = original_send(*args, **kwargs)
        confirmed.set()
        assert release.wait(timeout=2)
        return receipt

    def send() -> None:
        try:
            transport.send_token_event(
                TokenEvent(
                    request_id="request-1",
                    path_id="path-1",
                    path_attempt=0,
                    token_index=0,
                    token_id=101,
                    sampling_counter=1,
                )
            )
        except BaseException as error:
            send_errors.append(error)

    def close() -> None:
        try:
            transport.close()
        except BaseException as error:
            close_errors.append(error)

    transport.send_router_frame = paused_send
    send_thread = threading.Thread(target=send)
    close_thread = threading.Thread(target=close)
    try:
        send_thread.start()
        assert confirmed.wait(timeout=1)
        assert transport.outbound_trace == ()
        with transport._state_lock:
            assert transport._inflight_receipt_trace_commits == 1
        close_thread.start()
        time.sleep(0.05)
        close_returned_before_commit = not close_thread.is_alive()
        trace_before_release = transport.outbound_trace
    finally:
        release.set()
        send_thread.join(timeout=2)
        close_thread.join(timeout=2)
        if close_thread.is_alive():
            transport.close()

    assert not send_thread.is_alive()
    assert not close_thread.is_alive()
    assert close_returned_before_commit is False
    assert trace_before_release == ()
    assert not send_errors
    assert not close_errors
    with transport._state_lock:
        assert transport._inflight_receipt_trace_commits == 0
    assert len(transport.outbound_trace) == 2
    closed_trace = transport.outbound_trace
    time.sleep(0.02)
    assert transport.outbound_trace == closed_trace


def test_close_timeout_is_stable_and_repeated_close_waits_for_trace_commit() -> None:
    hub = _Hub()
    transport = _transport(hub, delivery_timeout_seconds=0.05)
    transport.bind_router(_RecordingRouter())
    transport.start()
    transport._entry_nodes["request-1"] = "peer-node"
    confirmed = threading.Event()
    release = threading.Event()
    original_send = transport.send_router_frame
    send_errors: list[BaseException] = []

    def paused_send(*args, **kwargs):
        receipt = original_send(*args, **kwargs)
        confirmed.set()
        assert release.wait(timeout=2)
        return receipt

    def send() -> None:
        try:
            transport.send_token_event(
                TokenEvent(
                    request_id="request-1",
                    path_id="path-1",
                    path_attempt=0,
                    token_index=0,
                    token_id=101,
                    sampling_counter=1,
                )
            )
        except BaseException as error:
            send_errors.append(error)

    transport.send_router_frame = paused_send
    thread = threading.Thread(target=send)
    thread.start()
    try:
        assert confirmed.wait(timeout=1)
        with transport._state_lock:
            assert transport._inflight_receipt_trace_commits == 1
        with pytest.raises(
            IrohTransportError,
            match=r"^receipt_trace_commit_shutdown_timeout$",
        ) as raised:
            transport.close()
        assert raised.value.code == "receipt_trace_commit_shutdown_timeout"
        assert raised.value.detail == ""
        assert transport.outbound_trace == ()
    finally:
        release.set()
        thread.join(timeout=2)

    assert not thread.is_alive()
    assert not send_errors
    with transport._state_lock:
        assert transport._inflight_receipt_trace_commits == 0
    assert len(transport.outbound_trace) == 2
    transport.close()
    closed_trace = transport.outbound_trace
    time.sleep(0.02)
    assert transport.outbound_trace == closed_trace


def test_remote_trace_builder_preflight_failure_sends_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hub = _Hub()
    transport = _transport(hub)
    transport.bind_router(_RecordingRouter())
    transport.start()
    transport._entry_nodes["request-1"] = "peer-node"
    original_builder = iroh_module._bounded_trace_identity

    def fail_delivery_identity(
        message,
        *,
        max_bytes=iroh_module._TRACE_ENTRY_BYTES,
        delivery_message_id=None,
    ):
        if delivery_message_id is not None:
            raise IrohTransportError("trace_identity_budget_exhausted")
        return original_builder(
            message,
            max_bytes=max_bytes,
            delivery_message_id=delivery_message_id,
        )

    monkeypatch.setattr(
        iroh_module,
        "_bounded_trace_identity",
        fail_delivery_identity,
    )
    try:
        with pytest.raises(
            IrohTransportError,
            match=r"^trace_identity_budget_exhausted$",
        ):
            transport.send_token_event(
                TokenEvent(
                    request_id="request-1",
                    path_id="path-1",
                    path_attempt=0,
                    token_index=0,
                    token_id=101,
                    sampling_counter=1,
                )
            )
        assert hub.sent == []
        assert transport.outbound_trace == ()
        with transport._state_lock:
            assert transport._inflight_receipt_trace_commits == 0
    finally:
        transport.close()


def test_remote_receipt_trace_oversize_is_rejected_before_send() -> None:
    oversized_endpoint_id = "peer-endpoint-" + "x" * 600
    hub = _Hub()
    transport = _transport(hub)
    transport._peer = PeerBinding(
        node_id="peer-node",
        endpoint_id=oversized_endpoint_id,
        endpoint_addr={
            "id": oversized_endpoint_id,
            "addrs": ["127.0.0.1:1"],
        },
        generation=7,
    )
    transport.bind_router(_RecordingRouter())
    transport.start()
    transport._entry_nodes["request-1"] = "peer-node"
    try:
        with pytest.raises(
            IrohTransportError,
            match=r"^delivery_receipt_trace_too_large$",
        ):
            transport.send_token_event(
                TokenEvent(
                    request_id="request-1",
                    path_id="path-1",
                    path_attempt=0,
                    token_index=0,
                    token_id=101,
                    sampling_counter=1,
                )
            )
        assert hub.sent == []
        assert transport.outbound_trace == ()
        with transport._state_lock:
            assert transport._inflight_receipt_trace_commits == 0
    finally:
        transport.close()


def test_local_trace_append_rechecks_running_after_builder_race(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hub = _Hub()
    router = _RecordingRouter()
    transport = _transport(hub)
    transport.bind_router(router)
    transport.start()
    transport._entry_nodes["request-1"] = "local-node"
    builder_entered = threading.Event()
    release_builder = threading.Event()
    original_builder = iroh_module._bounded_trace_identity
    send_errors: list[BaseException] = []

    def paused_builder(
        message,
        *,
        max_bytes=iroh_module._TRACE_ENTRY_BYTES,
        delivery_message_id=None,
    ):
        if delivery_message_id is None:
            builder_entered.set()
            assert release_builder.wait(timeout=2)
        return original_builder(
            message,
            max_bytes=max_bytes,
            delivery_message_id=delivery_message_id,
        )

    def send() -> None:
        try:
            transport.send_token_event(
                TokenEvent(
                    request_id="request-1",
                    path_id="path-1",
                    path_attempt=0,
                    token_index=0,
                    token_id=101,
                    sampling_counter=1,
                )
            )
        except BaseException as error:
            send_errors.append(error)

    monkeypatch.setattr(iroh_module, "_bounded_trace_identity", paused_builder)
    thread = threading.Thread(target=send)
    thread.start()
    try:
        assert builder_entered.wait(timeout=1)
        transport.close()
        assert transport.outbound_trace == ()
    finally:
        release_builder.set()
        thread.join(timeout=2)

    assert not thread.is_alive()
    assert len(send_errors) == 1
    assert isinstance(send_errors[0], IrohTransportError)
    assert send_errors[0].code == "transport_closed"
    assert transport.outbound_trace == ()
    assert router.token_events == []


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


def test_failure_trace_identity_preserves_bounded_failure_diagnostics() -> None:
    identity = json.loads(
        _bounded_trace_identity(
            FailureReport(
                request_id="request-1",
                path_id="path-1",
                path_attempt=0,
                token_index=-1,
                scope="PLACEMENT",
                reason="runtime_payload_shape_mismatch",
                placement_id="placement-001",
                node_id="node-1",
            )
        )
    )

    assert identity == {
        "node_id": "node-1",
        "path_attempt": 0,
        "path_id": "path-1",
        "placement_id": "placement-001",
        "reason": "runtime_payload_shape_mismatch",
        "request_id": "request-1",
        "scope": "PLACEMENT",
        "token_index": -1,
    }


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


def test_constructor_peer_endpoint_document_is_deeply_detached() -> None:
    hub = _Hub()
    endpoint_addr = {
        "id": "peer-endpoint",
        "relay": {"urls": ["https://relay.invalid/original"]},
    }
    peer = PeerBinding(
        "peer-node",
        "peer-endpoint",
        endpoint_addr,
        7,
    )
    transport = IrohTransport(
        node_id="local-node",
        socket_path="/unused",
        bootstrap_secret=b"s" * 32,
        peer=peer,
        expected_endpoint_id="local-endpoint",
        client_factory=hub.client,
    )

    endpoint_addr["relay"]["urls"].append("https://relay.invalid/alias")
    endpoint_addr["concurrent_marker"] = "caller-owned"

    assert transport.peer_binding.endpoint_addr == {
        "id": "peer-endpoint",
        "relay": {"urls": ["https://relay.invalid/original"]},
    }


def test_public_peer_binding_document_is_a_deep_defensive_copy() -> None:
    hub = _Hub()
    transport = _transport(hub)
    exposed = transport.peer_binding

    exposed.endpoint_addr["addrs"].append("127.0.0.1:2")
    exposed.endpoint_addr["concurrent_marker"] = "public-alias"

    assert transport.peer_binding.endpoint_addr == {
        "id": "peer-endpoint",
        "addrs": ["127.0.0.1:1"],
    }


def test_replacement_endpoint_document_is_owned_before_remote_configure() -> None:
    hub = _Hub()
    transport = _transport(hub)
    transport.bind_router(_RecordingRouter())
    transport.start()
    replacement_addr = {
        "id": "peer-endpoint",
        "relay": {"urls": ["https://relay.invalid/original"]},
    }
    replacement = PeerBinding(
        "peer-node",
        "peer-endpoint",
        replacement_addr,
        8,
    )
    configure_entered = threading.Event()
    release_configure = threading.Event()
    original_configure = transport._configure_peer
    errors: list[BaseException] = []

    def configure_then_pause(client, binding) -> None:
        configure_entered.set()
        assert release_configure.wait(timeout=1)
        original_configure(client, binding)

    def rotate() -> None:
        try:
            transport.rotate_peer(replacement)
        except BaseException as error:
            errors.append(error)

    transport._configure_peer = configure_then_pause
    rotation = threading.Thread(target=rotate)
    rotation.start()
    try:
        assert configure_entered.wait(timeout=1)
        replacement_addr["relay"]["urls"].append(
            "https://relay.invalid/during-configure"
        )
        replacement_addr["concurrent_marker"] = "during-configure"
        release_configure.set()
        rotation.join(timeout=1)
        assert not rotation.is_alive()
        assert errors == []

        replacement_addr["relay"]["urls"].append(
            "https://relay.invalid/after-commit"
        )
        replacement_addr["post_commit_marker"] = "caller-owned"
        expected = {
            "id": "peer-endpoint",
            "relay": {"urls": ["https://relay.invalid/original"]},
        }
        assert hub.configurations[-1][1] == expected
        assert transport.peer_binding.endpoint_addr == expected
    finally:
        release_configure.set()
        rotation.join(timeout=1)
        transport.close()


def test_sidecar_configure_document_cannot_mutate_candidate_binding() -> None:
    hub = _Hub()
    transport = _transport(hub)
    transport.bind_router(_RecordingRouter())
    transport.start()
    control = transport._control_client
    assert control is not None
    configured_documents: list[dict] = []

    def mutate_configure_document(
        endpoint_id: str,
        endpoint_addr: dict,
        *,
        generation: int,
        timeout: float | None = None,
    ) -> None:
        del endpoint_id, generation, timeout
        configured_documents.append(endpoint_addr)
        endpoint_addr["relay"]["urls"].append(
            "https://relay.invalid/sidecar-mutated"
        )
        endpoint_addr["sidecar_marker"] = True

    control.configure_peer = mutate_configure_document
    replacement_addr = {
        "id": "peer-endpoint",
        "relay": {"urls": ["https://relay.invalid/original"]},
    }
    replacement = PeerBinding(
        "peer-node",
        "peer-endpoint",
        replacement_addr,
        8,
    )
    try:
        transport.rotate_peer(replacement)

        assert configured_documents == [
            {
                "id": "peer-endpoint",
                "relay": {
                    "urls": [
                        "https://relay.invalid/original",
                        "https://relay.invalid/sidecar-mutated",
                    ]
                },
                "sidecar_marker": True,
            }
        ]
        expected = {
            "id": "peer-endpoint",
            "relay": {"urls": ["https://relay.invalid/original"]},
        }
        assert replacement.endpoint_addr == expected
        assert transport.peer_binding.endpoint_addr == expected
    finally:
        transport.close()


def test_rotation_detects_in_place_current_peer_document_mutation() -> None:
    hub = _Hub()
    hub.block_confirmed_send = True
    transport = _transport(hub, delivery_timeout_seconds=1.0)
    transport.bind_router(_RecordingRouter())
    transport.start()
    send_results: list[DeliveryReceipt] = []
    send_errors: list[BaseException] = []
    rotation_errors: list[BaseException] = []
    configured = threading.Event()
    release_commit = threading.Event()
    original_configure = transport._configure_peer

    def send() -> None:
        try:
            send_results.append(
                transport.send_router_frame(
                    _event_frame(),
                    destination_node_id="peer-node",
                )
            )
        except BaseException as error:
            send_errors.append(error)

    def configure_then_pause(client, binding) -> None:
        original_configure(client, binding)
        configured.set()
        assert release_commit.wait(timeout=1)

    def rotate() -> None:
        try:
            transport.rotate_peer(_binding(generation=8))
        except BaseException as error:
            rotation_errors.append(error)

    sender = threading.Thread(target=send)
    transport._configure_peer = configure_then_pause
    rotation = threading.Thread(target=rotate)
    sender.start()
    assert hub.confirmed_send_entered.wait(timeout=1)
    rotation.start()
    try:
        assert configured.wait(timeout=1)
        with transport._state_lock:
            current = transport._peer
            current.endpoint_addr["addrs"].append("127.0.0.1:2")
            assert transport._peer is current
            state_after_mutation = transport._peer
            pending_after_mutation = tuple(transport._pending)
        release_commit.set()
        rotation.join(timeout=1)
        assert not rotation.is_alive()
        assert len(rotation_errors) == 1
        assert isinstance(rotation_errors[0], IrohTransportError)
        assert rotation_errors[0].code == "peer_rotated"
        with transport._state_lock:
            assert transport._peer is state_after_mutation
            assert transport._peer.generation == 7
            assert tuple(transport._pending) == pending_after_mutation
        assert hub.cancels == []

        hub.release_confirmed_send.set()
        sender.join(timeout=1)
        assert not sender.is_alive()
        assert send_errors == []
        assert len(send_results) == 1
        assert send_results[0].peer_generation == 7
        assert hub.cancels == []
    finally:
        release_commit.set()
        hub.release_confirmed_send.set()
        rotation.join(timeout=1)
        sender.join(timeout=1)
        transport.close()


@pytest.mark.parametrize("invalid_value", [float("nan"), object()])
def test_peer_endpoint_document_rejects_non_json_without_value_leak(
    invalid_value: object,
) -> None:
    endpoint_addr = {
        "id": "peer-endpoint",
        "private_material": {"value": invalid_value},
    }

    with pytest.raises(ValueError) as raised:
        IrohTransport(
            node_id="local-node",
            socket_path="/unused",
            bootstrap_secret=b"s" * 32,
            peer=PeerBinding(
                "peer-node",
                "peer-endpoint",
                endpoint_addr,
                7,
            ),
            expected_endpoint_id="local-endpoint",
        )

    assert str(raised.value) == "endpoint_addr must be valid JSON data"
    assert "private_material" not in str(raised.value)


def test_rotation_configured_before_close_cannot_commit_after_close() -> None:
    hub = _Hub()
    transport = _transport(hub)
    transport.bind_router(_RecordingRouter())
    transport.start()
    configured = threading.Event()
    release_commit = threading.Event()
    original_configure = transport._configure_peer
    errors: list[BaseException] = []

    def configure_then_pause(client, binding) -> None:
        original_configure(client, binding)
        configured.set()
        assert release_commit.wait(timeout=1)

    def rotate() -> None:
        try:
            transport.rotate_peer(_binding(generation=8))
        except BaseException as error:
            errors.append(error)

    transport._configure_peer = configure_then_pause
    rotation = threading.Thread(target=rotate)
    rotation.start()
    assert configured.wait(timeout=1)
    transport.close()
    state_after_close = (
        transport.peer_binding,
        transport.pending_delivery_count,
        transport.outbound_trace,
        transport.evidence(),
        tuple(hub.cancels),
    )
    release_commit.set()
    rotation.join(timeout=1)

    assert not rotation.is_alive()
    assert len(errors) == 1
    assert isinstance(errors[0], IrohTransportError)
    assert errors[0].code == "transport_closed"
    assert (
        transport.peer_binding,
        transport.pending_delivery_count,
        transport.outbound_trace,
        transport.evidence(),
        tuple(hub.cancels),
    ) == state_after_close
    assert transport.peer_binding.generation == 7
    assert hub.configurations[-1][2] == 8


def test_rotation_configure_finishing_during_close_cannot_commit() -> None:
    hub = _Hub()
    transport = _transport(hub)
    transport.bind_router(_RecordingRouter())
    transport.start()
    control = transport._control_client
    assert control is not None
    configure_entered = threading.Event()
    release_configure = threading.Event()
    close_in_progress = threading.Event()
    release_close = threading.Event()
    original_configure = transport._configure_peer
    original_control_close = control.close
    rotation_errors: list[BaseException] = []
    close_errors: list[BaseException] = []

    def configure_during_close(client, binding) -> None:
        configure_entered.set()
        assert release_configure.wait(timeout=1)
        original_configure(client, binding)

    def block_control_close() -> None:
        close_in_progress.set()
        assert release_close.wait(timeout=1)
        original_control_close()

    def rotate() -> None:
        try:
            transport.rotate_peer(_binding(generation=8))
        except BaseException as error:
            rotation_errors.append(error)

    def close() -> None:
        try:
            transport.close()
        except BaseException as error:
            close_errors.append(error)

    transport._configure_peer = configure_during_close
    control.close = block_control_close
    rotation = threading.Thread(target=rotate)
    closing = threading.Thread(target=close)
    rotation.start()
    try:
        assert configure_entered.wait(timeout=1)
        closing.start()
        assert close_in_progress.wait(timeout=1)
        with transport._state_lock:
            assert transport._closed is True
            state_during_close = (
                transport._peer,
                tuple(transport._pending.items()),
                tuple(transport._outbound_trace),
            )
        release_configure.set()
        rotation.join(timeout=1)
        assert not rotation.is_alive()
        assert len(rotation_errors) == 1
        assert isinstance(rotation_errors[0], IrohTransportError)
        assert rotation_errors[0].code == "transport_closed"
        with transport._state_lock:
            assert (
                transport._peer,
                tuple(transport._pending.items()),
                tuple(transport._outbound_trace),
            ) == state_during_close
        assert transport.peer_binding.generation == 7
        assert hub.configurations[-1][2] == 8
    finally:
        release_configure.set()
        release_close.set()
        rotation.join(timeout=1)
        if closing.ident is not None:
            closing.join(timeout=1)

    assert not closing.is_alive()
    assert close_errors == []


def test_rotation_accepts_unchanged_same_object_control_and_peer_snapshot() -> None:
    hub = _Hub()
    transport = _transport(hub)
    transport.bind_router(_RecordingRouter())
    transport.start()
    captured_control = transport._control_client
    captured_peer = transport._peer
    configured = threading.Event()
    release_commit = threading.Event()
    original_configure = transport._configure_peer
    errors: list[BaseException] = []

    def configure_then_pause(client, binding) -> None:
        original_configure(client, binding)
        configured.set()
        assert release_commit.wait(timeout=1)

    def rotate() -> None:
        try:
            transport.rotate_peer(_binding(generation=8))
        except BaseException as error:
            errors.append(error)

    transport._configure_peer = configure_then_pause
    rotation = threading.Thread(target=rotate)
    rotation.start()
    try:
        assert configured.wait(timeout=1)
        with transport._state_lock:
            transport._control_client = captured_control
            transport._peer = captured_peer
            assert transport._control_client is captured_control
            assert transport._peer is captured_peer
        release_commit.set()
        rotation.join(timeout=1)
        assert not rotation.is_alive()
        assert errors == []
        assert transport.peer_binding.generation == 8
        assert hub.configurations[-1][2] == 8
    finally:
        release_commit.set()
        rotation.join(timeout=1)
        transport.close()


@pytest.mark.parametrize(
    ("state_change", "expected_code"),
    [
        ("control", "transport_control_changed"),
        ("peer", "peer_rotated"),
    ],
)
def test_rotation_revalidates_control_and_current_before_commit(
    state_change: str,
    expected_code: str,
) -> None:
    hub = _Hub()
    transport = _transport(hub)
    transport.bind_router(_RecordingRouter())
    transport.start()
    original_control = transport._control_client
    configured = threading.Event()
    release_commit = threading.Event()
    original_configure = transport._configure_peer
    errors: list[BaseException] = []

    def configure_then_pause(client, binding) -> None:
        original_configure(client, binding)
        configured.set()
        assert release_commit.wait(timeout=1)

    def rotate() -> None:
        try:
            transport.rotate_peer(_binding(generation=8))
        except BaseException as error:
            errors.append(error)

    transport._configure_peer = configure_then_pause
    rotation = threading.Thread(target=rotate)
    rotation.start()
    assert configured.wait(timeout=1)
    with transport._state_lock:
        if state_change == "control":
            swapped_control = object()
            transport._control_client = swapped_control
        else:
            swapped_control = None
            transport._peer = PeerBinding(
                "peer-node",
                "peer-9",
                {"id": "peer-9"},
                9,
            )
        state_before_release = (
            transport._peer,
            tuple(transport._pending.items()),
            tuple(transport._outbound_trace),
        )
    release_commit.set()
    rotation.join(timeout=1)
    try:
        assert not rotation.is_alive()
        assert len(errors) == 1
        assert isinstance(errors[0], IrohTransportError)
        assert errors[0].code == expected_code
        with transport._state_lock:
            assert (
                transport._peer,
                tuple(transport._pending.items()),
                tuple(transport._outbound_trace),
            ) == state_before_release
            if state_change == "control":
                assert transport._control_client is swapped_control
                transport._control_client = original_control
    finally:
        transport.close()


@pytest.mark.parametrize("_interleaving", range(12))
def test_rotation_and_deadline_reserve_one_cancel_per_message(
    _interleaving: int,
) -> None:
    hub = _Hub()
    hub.block_confirmed_send = True
    transport = _transport(hub, delivery_timeout_seconds=10.0)
    transport.bind_router(_RecordingRouter())
    transport.start()
    cancel_entered = threading.Event()
    release_cancel = threading.Event()
    cancel_call_lock = threading.Lock()
    cancel_calls = 0
    original_cancel = transport._cancel_with_client
    errors: list[BaseException] = []

    def paused_cancel(control, message_id, *, timeout=None) -> None:
        nonlocal cancel_calls
        with cancel_call_lock:
            cancel_calls += 1
            call_number = cancel_calls
        if call_number == 1:
            cancel_entered.set()
            assert release_cancel.wait(timeout=1.0)
        original_cancel(control, message_id, timeout=timeout)

    transport._cancel_with_client = paused_cancel

    def send() -> None:
        try:
            transport.send_router_frame(
                _event_frame(), destination_node_id="peer-node"
            )
        except BaseException as error:
            errors.append(error)

    sender = threading.Thread(target=send)
    rotation = threading.Thread(
        target=transport.rotate_peer,
        args=(_binding(generation=8),),
    )
    sender.start()
    assert hub.confirmed_send_entered.wait(timeout=1.0)
    with transport._state_lock:
        [(message_id, pending)] = transport._pending.items()

    deadline = threading.Thread(
        target=transport._expire_pending,
        args=(message_id, pending, "delivery_deadline_exceeded"),
    )
    if _interleaving % 2:
        deadline.start()
        expected_error = "delivery_deadline_exceeded"
    else:
        rotation.start()
        expected_error = "peer_rotated"
    assert cancel_entered.wait(timeout=1.0)
    if _interleaving % 2:
        rotation.start()
    else:
        deadline.start()
    deadline.join(timeout=1.0)
    assert not deadline.is_alive()
    release_cancel.set()
    assert hub.cancel_completed.wait(timeout=1.0)
    rotation.join(timeout=1.0)
    hub.release_confirmed_send.set()
    sender.join(timeout=1.0)
    try:
        assert not rotation.is_alive()
        assert not sender.is_alive()
        assert len(hub.cancels) == 1
        assert hub.cancels == [message_id]
        assert len(errors) == 1
        assert isinstance(errors[0], IrohTransportError)
        assert errors[0].code == expected_error
    finally:
        release_cancel.set()
        hub.release_confirmed_send.set()
        transport.close()


def test_deadline_cancel_worker_is_accounted_and_close_is_bounded() -> None:
    hub = _Hub()
    hub.block_confirmed_send = True
    hub.block_cancel = True
    transport = _transport(
        hub,
        delivery_timeout_seconds=0.05,
    )
    transport.bind_router(_RecordingRouter())
    transport.start()
    errors: list[BaseException] = []

    def send() -> None:
        try:
            transport.send_router_frame(
                _event_frame(),
                destination_node_id="peer-node",
            )
        except BaseException as error:
            errors.append(error)

    sender = threading.Thread(target=send)
    sender.start()
    try:
        assert hub.confirmed_send_entered.wait(timeout=1)
        assert hub.cancel_entered.wait(timeout=1)
        sender.join(timeout=1)
        assert not sender.is_alive()
        assert len(errors) == 1
        assert isinstance(errors[0], IrohTransportError)
        assert errors[0].code == "delivery_deadline_exceeded"
        assert hub.cancel_timeouts
        assert all(
            timeout is not None and 0 < timeout <= 0.05
            for timeout in hub.cancel_timeouts
        )
        started = time.monotonic()
        with pytest.raises(
            IrohTransportError,
            match="delivery_cancellation_shutdown_timeout",
        ):
            transport.close()
        assert time.monotonic() - started < 0.5
        assert transport.worker_threads_alive == 1
        assert hub.cancels == []
        hub.release_cancel.set()
        transport.close()
        assert transport.worker_threads_alive == 0
        assert hub.cancels == [hub.sent[0][0]] if hub.sent else len(hub.cancels) == 1
    finally:
        hub.release_confirmed_send.set()
        hub.release_cancel.set()
        sender.join(timeout=1)
        transport.close()


@pytest.mark.parametrize("first_winner", ["close", "rotation"])
def test_close_or_rotation_first_blocked_cancel_has_bounded_lifecycle(
    first_winner: str,
) -> None:
    hub = _Hub()
    hub.block_confirmed_send = True
    hub.block_cancel = True
    transport = _transport(
        hub,
        delivery_timeout_seconds=10.0,
    )
    transport.bind_router(_RecordingRouter())
    transport.start()
    send_errors: list[BaseException] = []

    def send() -> None:
        try:
            transport.send_router_frame(
                _event_frame(),
                destination_node_id="peer-node",
            )
        except BaseException as error:
            send_errors.append(error)

    sender = threading.Thread(target=send)
    sender.start()
    lifecycle_errors: list[BaseException] = []

    def lifecycle() -> None:
        try:
            if first_winner == "close":
                transport.close()
            else:
                transport.rotate_peer(_binding(generation=8))
        except BaseException as error:
            lifecycle_errors.append(error)

    lifecycle_thread = threading.Thread(target=lifecycle)
    try:
        assert hub.confirmed_send_entered.wait(timeout=1)
        lifecycle_thread.start()
        assert hub.cancel_entered.wait(timeout=1)
        lifecycle_thread.join(timeout=0.5)
        assert not lifecycle_thread.is_alive()
        if first_winner == "close":
            assert len(lifecycle_errors) == 1
            assert isinstance(lifecycle_errors[0], IrohTransportError)
            assert (
                lifecycle_errors[0].code
                == "delivery_cancellation_shutdown_timeout"
            )
        else:
            assert lifecycle_errors == []
            with pytest.raises(
                IrohTransportError,
                match="delivery_cancellation_shutdown_timeout",
            ):
                transport.close()
        assert transport.worker_threads_alive == 1
        assert len(hub.cancel_timeouts) == 1
        assert hub.cancel_timeouts[0] is not None
        hub.release_cancel.set()
        hub.release_confirmed_send.set()
        sender.join(timeout=1)
        transport.close()
        assert transport.worker_threads_alive == 0
        assert len(hub.cancels) == 1
        assert len(send_errors) == 1
        assert isinstance(send_errors[0], IrohTransportError)
        assert send_errors[0].code == (
            "transport_closed" if first_winner == "close" else "peer_rotated"
        )
    finally:
        hub.release_cancel.set()
        hub.release_confirmed_send.set()
        lifecycle_thread.join(timeout=1)
        sender.join(timeout=1)
        transport.close()


def test_cancel_failure_unregisters_late_registered_message_worker_once() -> None:
    hub = _Hub()
    hub.block_confirmed_send = True
    hub.cancel_failure = RuntimeError("injected cancel RPC failure")
    transport = _transport(
        hub,
        delivery_timeout_seconds=10.0,
    )
    transport.bind_router(_RecordingRouter())
    transport.start()
    worker_entered = threading.Event()
    release_worker = threading.Event()
    original_cancel = transport._cancel_with_client

    def delayed_cancel(control, message_id, *, timeout) -> None:
        worker_entered.set()
        assert release_worker.wait(timeout=1)
        original_cancel(control, message_id, timeout=timeout)

    transport._cancel_with_client = delayed_cancel
    errors: list[BaseException] = []

    def send() -> None:
        try:
            transport.send_router_frame(
                _event_frame(),
                destination_node_id="peer-node",
            )
        except BaseException as error:
            errors.append(error)

    sender = threading.Thread(target=send)
    sender.start()
    try:
        assert hub.confirmed_send_entered.wait(timeout=1)
        with transport._state_lock:
            [(message_id, pending)] = transport._pending.items()
        transport._expire_pending(
            message_id,
            pending,
            "delivery_deadline_exceeded",
        )
        assert worker_entered.wait(timeout=1)
        with transport._state_lock:
            assert message_id in transport._delivery_cancel_threads
        assert transport.worker_threads_alive == 3
        release_worker.set()
        assert hub.cancel_entered.wait(timeout=1)
        deadline = time.monotonic() + 1
        while transport.worker_threads_alive == 3 and time.monotonic() < deadline:
            time.sleep(0.01)
        with transport._state_lock:
            assert message_id not in transport._delivery_cancel_threads
        assert len(hub.cancel_timeouts) == 1
        assert hub.cancel_timeouts[0] is not None
        hub.release_confirmed_send.set()
        sender.join(timeout=1)
        assert len(errors) == 1
        assert isinstance(errors[0], IrohTransportError)
        assert errors[0].code == "delivery_deadline_exceeded"
        transport.close()
        assert transport.worker_threads_alive == 0
    finally:
        release_worker.set()
        hub.release_confirmed_send.set()
        sender.join(timeout=1)
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
    # macOS background-QoS timer coalescing can add roughly 150 ms to a timed
    # wait. Use a longer deadline with a tighter relative tolerance so the
    # test still detects deadline reset without depending on sub-100-ms wakeup.
    delivery_timeout = 1.0
    scheduler_slack = delivery_timeout * 0.2
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
        thread.join(timeout=delivery_timeout + scheduler_slack)
        assert not thread.is_alive()
        assert len(errors) == 1
        assert isinstance(errors[0], IrohTransportError)
        assert errors[0].code == "delivery_deadline_exceeded"
        assert len(elapsed) == 1 and elapsed[0] < delivery_timeout + scheduler_slack
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
