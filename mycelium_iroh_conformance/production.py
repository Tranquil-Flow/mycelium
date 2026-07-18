# SPDX-License-Identifier: AGPL-3.0-or-later
"""Deterministic production-adapter driver for the independent reference model.

This module is deliberately separate from :mod:`mycelium_iroh_conformance.model`.
The reference model imports no production code.  Here, explicit Events expose
scheduler boundaries in the production adapter without changing production.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
import threading
import time
from typing import Callable

from mycelium_iroh_sidecar import ProtocolError
from mycelium_router.contracts import TokenEvent
from mycelium_router.transports.iroh import IrohTransport, IrohTransportError, PeerBinding
from mycelium_router.wire import encode_frame

from .model import AdapterAction, CLIENT_ROLES, EvidenceCounters, ModelState

_WAIT_SECONDS = 2.0


@dataclass
class _ConnectCall:
    client: "_ScriptedClient"
    release: threading.Event
    outcome: BaseException | None = None
    blocked: bool = True
    role: str = ""


@dataclass
class _SendCall:
    client: "_ScriptedClient"
    frame: bytes
    message_id: bytes
    expected_generation: int
    begin: threading.Event
    begun: threading.Event
    release: threading.Event
    outcome: BaseException | None = None


@dataclass
class _ReceiveCall:
    value: tuple[bytes, int, bytes]
    release: threading.Event


@dataclass
class _AckCall:
    client: "_ScriptedClient"
    message_id: bytes
    release: threading.Event
    outcome: BaseException | None = None
    succeeded: bool = False


@dataclass
class _DispatchCall:
    event: TokenEvent
    source_node_id: str | None
    release: threading.Event
    completed: bool = False


class _Hub:
    """Thread-safe, event-scripted stand-in for three local sidecar sessions."""

    def __init__(self) -> None:
        self.endpoint_id = "local-endpoint"
        self.condition = threading.Condition()
        self.clients: list[_ScriptedClient] = []
        self.inbound: deque[tuple[bytes, int, bytes] | BaseException] = deque()
        self.connect_calls: list[_ConnectCall] = []
        self.send_calls: list[_SendCall] = []
        self.receive_calls: list[_ReceiveCall] = []
        self.ack_calls: list[_AckCall] = []
        self.cancels: list[bytes] = []
        self.configurations: list[tuple[str, int]] = []
        self.block_next_connect = False
        self.cleanup_started = False
        self.message_symbols: dict[bytes, str] = {}
        self._next_send_symbol = 0

    def client(self, *_args, **_kwargs) -> "_ScriptedClient":
        with self.condition:
            client = _ScriptedClient(self, len(self.clients))
            self.clients.append(client)
            self.condition.notify_all()
            return client

    def symbol_for(self, message_id: bytes) -> str:
        with self.condition:
            symbol = self.message_symbols.get(message_id)
            if symbol is None:
                symbol = f"send-{self._next_send_symbol}"
                self._next_send_symbol += 1
                self.message_symbols[message_id] = symbol
            return symbol

    def deliver(self, message_id: bytes, generation: int, frame: bytes) -> None:
        with self.condition:
            self.inbound.append((message_id, generation, frame))
            self.condition.notify_all()

    def fail_receive(self, error: BaseException) -> None:
        with self.condition:
            self.inbound.append(error)
            self.condition.notify_all()

    def wait_for(self, predicate: Callable[[], bool], detail: str) -> None:
        with self.condition:
            if not self.condition.wait_for(predicate, timeout=_WAIT_SECONDS):
                raise AssertionError(f"deterministic fixture stalled: {detail}")

    def begin_cleanup(self) -> None:
        with self.condition:
            self.cleanup_started = True
            for call in self.connect_calls:
                call.release.set()
            for call in self.send_calls:
                call.begin.set()
                if not call.release.is_set():
                    call.outcome = call.outcome or IrohTransportError("transport_closed")
                    call.release.set()
            for call in self.receive_calls:
                call.release.set()
            for call in self.ack_calls:
                if not call.release.is_set():
                    call.outcome = call.outcome or IrohTransportError("transport_closed")
                    call.release.set()
            self.condition.notify_all()


class _ScriptedClient:
    def __init__(self, hub: _Hub, creation_index: int) -> None:
        self.hub = hub
        self.creation_index = creation_index
        self.endpoint_id: str | None = None
        self.connected = False
        self.closed = False
        self.was_installed = False

    def connect(self, *, deadline: float | None = None) -> None:
        del deadline
        with self.hub.condition:
            blocked = self.creation_index >= 3 and self.hub.block_next_connect
            if blocked:
                self.hub.block_next_connect = False
            call = _ConnectCall(self, threading.Event(), blocked=blocked)
            self.hub.connect_calls.append(call)
            self.hub.condition.notify_all()
        if blocked and not self.hub.cleanup_started:
            if not call.release.wait(_WAIT_SECONDS):
                raise TimeoutError("scripted replacement connect stalled")
        if call.outcome is not None:
            raise call.outcome
        self.connected = True
        self.endpoint_id = self.hub.endpoint_id

    def close(self) -> None:
        with self.hub.condition:
            self.connected = False
            self.closed = True
            self.hub.condition.notify_all()

    def interrupt(self) -> None:
        self.close()

    def configure_peer(
        self,
        endpoint_id: str,
        endpoint_addr: dict,
        *,
        generation: int,
        timeout: float | None = None,
    ) -> None:
        del endpoint_addr, timeout
        if not self.connected:
            raise ProtocolError("not_connected")
        with self.hub.condition:
            self.hub.configurations.append((endpoint_id, generation))
            self.hub.condition.notify_all()

    def send_confirmed(
        self,
        frame: bytes,
        message_id: bytes,
        *,
        timeout: float | None = None,
        expected_generation: int,
    ) -> bytes:
        del timeout
        if not self.connected:
            raise ProtocolError("not_connected")
        self.hub.symbol_for(message_id)
        call = _SendCall(
            self,
            frame,
            message_id,
            expected_generation,
            threading.Event(),
            threading.Event(),
            threading.Event(),
        )
        with self.hub.condition:
            self.hub.send_calls.append(call)
            self.hub.condition.notify_all()
        if not self.hub.cleanup_started:
            if not call.begin.wait(_WAIT_SECONDS):
                raise TimeoutError("scripted send-begin boundary stalled")
            call.begun.set()
            with self.hub.condition:
                self.hub.condition.notify_all()
            if not call.release.wait(_WAIT_SECONDS):
                raise TimeoutError("scripted confirmed send stalled")
        if call.outcome is not None:
            raise call.outcome
        return message_id

    def recv_with_generation(self, *, wait_seconds: float | None = None, timeout: float | None = None):
        maximum_wait = wait_seconds if wait_seconds is not None else timeout
        deadline = time.monotonic() + (maximum_wait or 0.01)
        with self.hub.condition:
            while not self.hub.inbound:
                if not self.connected:
                    raise ProtocolError("sidecar_disconnected")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("empty")
                self.hub.condition.wait(remaining)
            value = self.hub.inbound.popleft()
        if isinstance(value, BaseException):
            raise value
        call = _ReceiveCall(value, threading.Event())
        with self.hub.condition:
            self.hub.receive_calls.append(call)
            self.hub.condition.notify_all()
        if not self.hub.cleanup_started:
            if not call.release.wait(_WAIT_SECONDS):
                raise TimeoutError("scripted receive boundary stalled")
        return value

    def ack(self, message_id: bytes) -> None:
        call = _AckCall(self, message_id, threading.Event())
        with self.hub.condition:
            self.hub.ack_calls.append(call)
            self.hub.condition.notify_all()
        if not self.hub.cleanup_started:
            if not call.release.wait(_WAIT_SECONDS):
                raise TimeoutError("scripted ACK stalled")
        if call.outcome is not None:
            raise call.outcome
        call.succeeded = True
        with self.hub.condition:
            self.hub.condition.notify_all()

    def cancel(self, message_id: bytes, *, timeout: float | None = None) -> None:
        del timeout
        with self.hub.condition:
            self.hub.cancels.append(message_id)
            self.hub.condition.notify_all()


class _RecordingRouter:
    def __init__(self, hub: _Hub) -> None:
        self.hub = hub
        self.dispatch_calls: list[_DispatchCall] = []

    def receive_token_event(self, event: TokenEvent, *, source_node_id: str | None = None) -> bool:
        call = _DispatchCall(event, source_node_id, threading.Event())
        with self.hub.condition:
            self.dispatch_calls.append(call)
            self.hub.condition.notify_all()
        if not self.hub.cleanup_started:
            if not call.release.wait(_WAIT_SECONDS):
                raise TimeoutError("scripted Router dispatch stalled")
        call.completed = True
        with self.hub.condition:
            self.hub.condition.notify_all()
        return True


@dataclass(frozen=True)
class ProductionSnapshot:
    lifecycle: str
    router_bound: bool
    installed_client_roles: tuple[str, ...]
    closed_client_count: int
    closed_replacement_count: int
    pending_receipt_ids: tuple[str, ...]
    queue_permits: int
    generation: int
    dispatch_count: int
    ack_count: int
    cancellation_count: int
    fatal_error: str | None
    evidence: EvidenceCounters


@dataclass
class _SendWorker:
    symbol: str
    thread: threading.Thread
    outcome: list[object]


class ProductionAdapterDriver:
    """Execute symbolic actions against a fresh production ``IrohTransport``."""

    def __init__(self, *, queue_capacity: int = 2, initial_generation: int = 7) -> None:
        self.hub = _Hub()
        self.router = _RecordingRouter(self.hub)
        self.transport = IrohTransport(
            node_id="local-node",
            socket_path="/unused",
            bootstrap_secret=b"s" * 32,
            peer=self._binding(initial_generation),
            expected_endpoint_id=self.hub.endpoint_id,
            queue_capacity=queue_capacity,
            delivery_timeout_seconds=0.08,
            poll_interval_seconds=0.005,
            client_factory=self.hub.client,
        )
        self.queue_capacity = queue_capacity
        self._workers: dict[str, _SendWorker] = {}
        self._close_thread: threading.Thread | None = None
        self._close_outcome: list[BaseException] = []
        self._payload_numbers: dict[str, int] = {}
        self._next_payload_number = 101
        self._last_receive_symbol: str | None = None
        self._last_receive_payload = "frame-a"

    @staticmethod
    def _binding(generation: int) -> PeerBinding:
        endpoint_id = f"peer-{generation}"
        return PeerBinding(
            node_id="peer-node",
            endpoint_id=endpoint_id,
            endpoint_addr={"id": endpoint_id, "addrs": ["127.0.0.1:1"]},
            generation=generation,
        )

    def _frame(self, payload_id: str) -> bytes:
        number = self._payload_numbers.get(payload_id)
        if number is None:
            number = self._next_payload_number
            self._next_payload_number += 1
            self._payload_numbers[payload_id] = number
        return encode_frame(
            TokenEvent(
                request_id="request-1",
                path_id="path-1",
                path_attempt=0,
                token_index=0,
                token_id=number,
                sampling_counter=1,
            )
        )

    @staticmethod
    def _message_bytes(symbol: str) -> bytes:
        return symbol.encode("ascii").ljust(16, b"_")[:16]

    def _eventually(self, predicate: Callable[[], bool], detail: str) -> None:
        deadline = time.monotonic() + _WAIT_SECONDS
        while not predicate():
            if time.monotonic() >= deadline:
                raise AssertionError(f"production adapter did not settle: {detail}")
            time.sleep(0.001)

    def _pending_symbol(self, requested: str | None = None) -> str | None:
        snapshot = self.snapshot()
        if requested is not None:
            return requested if requested in snapshot.pending_receipt_ids else None
        return snapshot.pending_receipt_ids[0] if snapshot.pending_receipt_ids else None

    def _unreleased_send_call(self, symbol: str) -> _SendCall | None:
        return next(
            (
                call
                for call in reversed(self.hub.send_calls)
                if self.hub.message_symbols.get(call.message_id) == symbol
                and not call.release.is_set()
            ),
            None,
        )

    def _unreleased_receive(self) -> _ReceiveCall | None:
        return next(
            (call for call in reversed(self.hub.receive_calls) if not call.release.is_set()),
            None,
        )

    def _unreleased_ack(self) -> _AckCall | None:
        return next((call for call in reversed(self.hub.ack_calls) if not call.release.is_set()), None)

    def _unreleased_dispatch(self) -> _DispatchCall | None:
        return next(
            (call for call in reversed(self.router.dispatch_calls) if not call.release.is_set()),
            None,
        )

    def _blocked_connect(self, role: str) -> _ConnectCall | None:
        candidates = [
            call
            for call in self.hub.connect_calls[3:]
            if call.blocked and not call.release.is_set() and call.role == role
        ]
        return candidates[-1] if candidates else None

    def _start_send(self, action: AdapterAction) -> None:
        symbol = action.message_id or f"send-{len(self._workers)}"
        outcome: list[object] = []

        def invoke() -> None:
            try:
                outcome.append(
                    self.transport.send_router_frame(
                        self._frame(action.payload_id),
                        destination_node_id="peer-node",
                    )
                )
            except BaseException as error:
                outcome.append(error)

        worker = _SendWorker(
            symbol,
            threading.Thread(target=invoke, name=f"iroh-conformance-{symbol}"),
            outcome,
        )
        self._workers[symbol] = worker
        before = len(self.hub.send_calls)
        worker.thread.start()
        self._eventually(
            lambda: len(self.hub.send_calls) > before or not worker.thread.is_alive(),
            f"send admission {symbol}",
        )

    def _join_send(self, symbol: str) -> None:
        worker = self._workers.get(symbol)
        if worker is None:
            return
        worker.thread.join(_WAIT_SECONDS)
        if worker.thread.is_alive():
            raise AssertionError(f"send worker leaked: {symbol}")

    def apply(self, action: AdapterAction) -> None:
        name = action.name.strip().lower().replace("-", "_")
        aliases = {
            "bind": "bind_router",
            "register_router": "bind_router",
            "restart": "start",
            "send_frame": "send",
            "complete_send": "send_confirmed",
            "send_disconnect": "send_reconnect_begin",
            "receive_disconnect": "receive_reconnect_begin",
            "receive_frame": "receive_delivery",
            "dispatch_success": "dispatch_complete",
            "ack": "ack_success",
            "ack_lost": "ack_fail",
            "lost_ack": "ack_fail",
            "delayed_ack": "ack_delayed",
            "delay_ack": "ack_delayed",
            "lose_confirmation": "deadline",
            "cancel_send": "deadline",
        }
        name = aliases.get(name, name)
        try:
            if name == "bind_router":
                self.transport.bind_router(self.router)
            elif name == "start":
                self.transport.start()
            elif name == "close":
                self._apply_close()
            elif name == "fatal_receive":
                self.hub.fail_receive(ProtocolError(action.error or "sequence_gap"))
                self._eventually(lambda: self.transport.fatal_error is not None, "fatal receive")
            elif name in {"queue_send", "send"}:
                self._start_send(action)
            elif name == "send_begin":
                symbol = self._pending_symbol(action.message_id)
                if symbol is not None:
                    call = self._unreleased_send_call(symbol)
                    if call is not None and not call.begin.is_set():
                        call.begin.set()
                        self.hub.wait_for(call.begun.is_set, "send begin")
            elif name == "send_confirmed":
                self._finish_send(action, None)
            elif name == "send_failed":
                self._finish_send(action, ProtocolError(action.error or "rejected"))
            elif name in {"send_reconnect_begin", "reconnect_begin"} and (action.role or "send") == "send":
                self._begin_send_reconnect(action)
            elif name in {"receive_reconnect_begin", "reconnect_begin"}:
                self._begin_receive_reconnect()
            elif name in {"send_reconnect_complete", "reconnect_complete"} and (action.role or "send") == "send":
                self._complete_reconnect("send", fail=False)
            elif name in {"receive_reconnect_complete", "reconnect_complete"}:
                self._complete_reconnect("receive", fail=False)
            elif name in {"send_reconnect_fail", "reconnect_fail"} and (action.role or "send") == "send":
                self._complete_reconnect("send", fail=True)
            elif name in {"receive_reconnect_fail", "reconnect_fail"}:
                self._complete_reconnect("receive", fail=True)
            elif name == "rotate_peer":
                self._rotate(action)
            elif name == "receive_begin":
                self._receive(action, "receive_delivery", hold=True)
            elif name == "receive_complete":
                self._complete_receive()
            elif name in {
                "receive_delivery",
                "receive_exact_replay",
                "receive_collision",
                "receive_stale_sequence",
                "receive_future_sequence",
                "receive_stale_generation",
                "receive_future_generation",
                "receive_malformed_frame",
                "receive_truncated_frame",
                "malformed_frame",
                "truncated_frame",
            }:
                self._receive(action, name)
            elif name == "dispatch_begin":
                pass
            elif name in {"dispatch", "dispatch_complete"}:
                call = self._unreleased_dispatch()
                if call is not None:
                    before = len(self.hub.ack_calls)
                    call.release.set()
                    self.hub.wait_for(
                        lambda: len(self.hub.ack_calls) > before
                        or self.transport.fatal_error is not None,
                        "dispatch completion",
                    )
            elif name == "dispatch_fail":
                call = self._unreleased_dispatch()
                if call is not None:
                    call.release.set()
                    self.hub.begin_cleanup()
                    self._eventually(lambda: self.transport.fatal_error is not None, "dispatch failure")
            elif name == "ack_begin":
                pass
            elif name == "ack_success":
                self._finish_ack(None)
            elif name == "ack_delayed":
                pass
            elif name == "ack_fail":
                self._finish_ack(ProtocolError(action.error or "sidecar_disconnected"))
            elif name in {"delay_confirmation"}:
                pass
            elif name == "deadline":
                if self.snapshot().pending_receipt_ids:
                    before = len(self.hub.cancels)
                    self.hub.wait_for(
                        lambda: len(self.hub.cancels) > before,
                        "delivery deadline cancel",
                    )
            elif name == "finish_cancelled":
                symbol = self._pending_symbol(action.message_id)
                reason = ""
                if symbol is not None:
                    with self.transport._state_lock:
                        reason = next(
                            (
                                pending.reason
                                for message_id, pending in self.transport._pending.items()
                                if self.hub.symbol_for(message_id) == symbol
                            ),
                            "",
                        )
                outcome: BaseException = (
                    ProtocolError(reason)
                    if reason and reason != "delivery_deadline_exceeded"
                    else TimeoutError("confirmed delivery deadline")
                )
                self._finish_send(action, outcome)
            else:
                # Unknown and invalid scheduler actions are reference-model rejections.
                pass
        except IrohTransportError:
            pass

    def _apply_close(self) -> None:
        if self._blocked_connect("receive") is not None and self._close_thread is None:
            def invoke() -> None:
                try:
                    self.transport.close()
                except BaseException as error:
                    self._close_outcome.append(error)

            self._close_thread = threading.Thread(target=invoke, name="iroh-conformance-close")
            self._close_thread.start()
            self._eventually(lambda: self.transport._closed, "close fence installation")
            return
        self.transport.close()

    def _finish_send(self, action: AdapterAction, outcome: BaseException | None) -> None:
        symbol = self._pending_symbol(action.message_id)
        if symbol is None:
            return
        call = self._unreleased_send_call(symbol)
        if call is None:
            return
        call.outcome = outcome
        call.begin.set()
        call.release.set()
        self._join_send(symbol)

    def _begin_send_reconnect(self, action: AdapterAction) -> None:
        symbol = self._pending_symbol(action.message_id)
        if symbol is None:
            return
        call = self._unreleased_send_call(symbol)
        if call is None:
            return
        self.hub.block_next_connect = True
        before = len(self.hub.connect_calls)
        call.outcome = ProtocolError("sidecar_disconnected")
        call.begin.set()
        call.release.set()
        self.hub.wait_for(lambda: len(self.hub.connect_calls) > before, "send reconnect begin")
        self.hub.connect_calls[-1].role = "send"

    def _begin_receive_reconnect(self) -> None:
        if not self.transport.running:
            return
        self.hub.block_next_connect = True
        before = len(self.hub.connect_calls)
        self.hub.fail_receive(ProtocolError("sidecar_disconnected"))
        self.hub.wait_for(lambda: len(self.hub.connect_calls) > before, "receive reconnect begin")
        self.hub.connect_calls[-1].role = "receive"

    def _complete_reconnect(self, role: str, *, fail: bool) -> None:
        call = self._blocked_connect(role)
        if call is None:
            return
        if fail:
            call.outcome = ConnectionResetError("scripted reconnect failure")
        before_sends = len(self.hub.send_calls)
        call.release.set()
        if role == "send":
            symbol = self.hub.message_symbols.get(
                next(iter(self.transport._pending), b""),
                "",
            )
            if fail or self.transport._closed or not self.transport._running:
                if symbol:
                    self._join_send(symbol)
            else:
                self.hub.wait_for(
                    lambda: len(self.hub.send_calls) > before_sends,
                    "send retry after reconnect",
                )
        else:
            if self._close_thread is not None:
                self._close_thread.join(_WAIT_SECONDS)
                if self._close_thread.is_alive():
                    raise AssertionError("close worker leaked after receive reconnect fence")
            elif fail:
                self._eventually(lambda: self.transport.fatal_error is not None, "receive reconnect failure")
            else:
                self._eventually(
                    lambda: self.transport._receive_client is call.client,
                    "receive replacement install",
                )

    def _rotate(self, action: AdapterAction) -> None:
        if not self.transport.running:
            try:
                self.transport.rotate_peer(
                    self._binding(action.generation or self.transport.peer_binding.generation + 1)
                )
            except IrohTransportError:
                pass
            return
        generation = action.generation or self.transport.peer_binding.generation + 1
        self.transport.rotate_peer(self._binding(generation))
        dispatch = self._unreleased_dispatch()
        ack = self._unreleased_ack()
        if dispatch is not None:
            dispatch.release.set()
            self._eventually(lambda: self.transport.fatal_error is not None, "rotation during dispatch")
        elif ack is not None:
            ack.outcome = ProtocolError("peer_rotated")
            ack.release.set()
            self._eventually(lambda: self.transport.fatal_error is not None, "rotation during ACK")

    def _receive(
        self,
        action: AdapterAction,
        name: str,
        *,
        hold: bool = False,
    ) -> None:
        if (
            not self.transport.running
            or self._unreleased_receive() is not None
            or self._unreleased_dispatch() is not None
            or self._unreleased_ack() is not None
        ):
            return
        if name in {"receive_stale_sequence", "receive_future_sequence"}:
            self.hub.fail_receive(ProtocolError("sequence_gap"))
            self._eventually(lambda: self.transport.fatal_error is not None, name)
            return
        generation = action.generation or self.transport.peer_binding.generation
        if name == "receive_stale_generation":
            generation = self.transport.peer_binding.generation - 1
        elif name == "receive_future_generation":
            generation = self.transport.peer_binding.generation + 1
        if name in {"receive_exact_replay", "receive_collision"}:
            if self._last_receive_symbol is None:
                return
            symbol = self._last_receive_symbol
            payload_id = (
                self._last_receive_payload
                if name == "receive_exact_replay"
                else f"{self._last_receive_payload}#collision"
            )
        else:
            symbol = action.message_id or f"recv-{len(self.router.dispatch_calls)}"
            payload_id = action.payload_id
        frame = self._frame(payload_id)
        if name in {"receive_malformed_frame", "malformed_frame"} or action.frame_kind == "malformed":
            frame = b"not-router-wire"
        elif name in {"receive_truncated_frame", "truncated_frame"} or action.frame_kind == "truncated":
            frame = frame[:-1]
        before_receive = len(self.hub.receive_calls)
        before_dispatch = len(self.router.dispatch_calls)
        before_ack = len(self.hub.ack_calls)
        self.hub.deliver(self._message_bytes(symbol), generation, frame)
        self.hub.wait_for(
            lambda: len(self.hub.receive_calls) > before_receive
            or self.transport.fatal_error is not None,
            f"receive boundary {name}",
        )
        if self.transport.fatal_error is not None:
            return
        call = self.hub.receive_calls[-1]
        if hold:
            self._last_receive_symbol = symbol
            self._last_receive_payload = payload_id
            return
        call.release.set()
        self.hub.wait_for(
            lambda: len(self.router.dispatch_calls) > before_dispatch
            or len(self.hub.ack_calls) > before_ack
            or self.transport.fatal_error is not None,
            f"receive completion {name}",
        )
        if self.transport.fatal_error is None and name not in {"receive_exact_replay"}:
            self._last_receive_symbol = symbol
            self._last_receive_payload = payload_id

    def _complete_receive(self) -> None:
        call = self._unreleased_receive()
        if call is None:
            return
        before_dispatch = len(self.router.dispatch_calls)
        before_ack = len(self.hub.ack_calls)
        call.release.set()
        self.hub.wait_for(
            lambda: len(self.router.dispatch_calls) > before_dispatch
            or len(self.hub.ack_calls) > before_ack
            or self.transport.fatal_error is not None,
            "receive completion",
        )

    def _finish_ack(self, outcome: BaseException | None) -> None:
        call = self._unreleased_ack()
        if call is None:
            return
        before = self.transport.evidence().remote_frames_received
        call.outcome = outcome
        call.release.set()
        if outcome is None:
            self._eventually(
                lambda: call.succeeded
                and (
                    self.transport.evidence().remote_frames_received > before
                    or self.transport.evidence().duplicate_frames > 0
                ),
                "ACK success",
            )
        else:
            self._eventually(lambda: self.transport.fatal_error is not None, "ACK failure")

    def snapshot(self) -> ProductionSnapshot:
        with self.transport._state_lock:
            role_clients = (
                ("send", self.transport._send_client),
                ("receive", self.transport._receive_client),
                ("control", self.transport._control_client),
            )
            roles = tuple(role for role, client in role_clients if client is not None)
            for _role, client in role_clients:
                if client is not None:
                    client.was_installed = True
            if self._close_thread is not None and self._close_thread.is_alive():
                lifecycle = "CLOSING"
            elif self.transport._closed:
                lifecycle = "CLOSED"
            elif self.transport._fatal_error is not None:
                lifecycle = "FATAL"
            elif self.transport._running:
                lifecycle = "RUNNING"
            elif self.transport._router is not None:
                lifecycle = "BOUND"
            else:
                lifecycle = "NEW"
            pending = tuple(
                self.hub.symbol_for(message_id)
                for message_id in self.transport._pending
            )
            fatal = (
                None
                if self.transport._fatal_error is None
                else self.transport._fatal_error.code
            )
            evidence = self.transport.evidence()
        permits = getattr(self.transport._send_slots, "_value")
        return ProductionSnapshot(
            lifecycle=lifecycle,
            router_bound=self.transport._router is not None,
            installed_client_roles=roles,
            closed_client_count=sum(client.closed for client in self.hub.clients),
            closed_replacement_count=sum(
                client.closed and client.creation_index >= 3 and not client.was_installed
                for client in self.hub.clients
            ),
            pending_receipt_ids=pending,
            queue_permits=permits,
            generation=evidence.peer_generation,
            dispatch_count=sum(call.completed for call in self.router.dispatch_calls),
            ack_count=sum(call.succeeded for call in self.hub.ack_calls),
            cancellation_count=len(self.hub.cancels),
            fatal_error=fatal,
            evidence=EvidenceCounters(
                sent=evidence.remote_frames_sent,
                received=evidence.remote_frames_received,
                dispatched=evidence.router_frames_dispatched,
                duplicates=evidence.duplicate_frames,
            ),
        )

    def close(self) -> None:
        self.hub.begin_cleanup()
        for call in self.router.dispatch_calls:
            call.release.set()
        try:
            self.transport.close()
        except IrohTransportError:
            pass
        for worker in self._workers.values():
            worker.thread.join(_WAIT_SECONDS)
            if worker.thread.is_alive():
                raise AssertionError(f"send worker leaked during cleanup: {worker.symbol}")
        if self._close_thread is not None:
            self._close_thread.join(_WAIT_SECONDS)
            if self._close_thread.is_alive():
                raise AssertionError("close worker leaked during cleanup")


def project_model(state: ModelState) -> ProductionSnapshot:
    """Project only user-requested observables from the independent model."""

    return ProductionSnapshot(
        lifecycle=state.lifecycle,
        router_bound=state.router_bound,
        installed_client_roles=state.installed_client_roles,
        closed_client_count=state.closed_client_count,
        closed_replacement_count=state.closed_replacement_count,
        pending_receipt_ids=state.pending_receipt_ids,
        queue_permits=state.queue_permits,
        generation=state.generation,
        dispatch_count=state.dispatch_count,
        ack_count=state.ack_count,
        cancellation_count=state.cancellation_count,
        fatal_error=state.fatal_error,
        evidence=state.evidence,
    )


__all__ = ["ProductionAdapterDriver", "ProductionSnapshot", "project_model"]
