"""Iroh adapter PathCancellation adversarial corpus using an in-memory sidecar client."""

from __future__ import annotations

from dataclasses import replace
import threading
import time

import pytest

from physical_inference_node import PhysicalNodeService
from mycelium_router.contracts import (
    FailureReport,
    HopHeader,
    ManifestDelta,
    PathCancellation,
    ProgressivePrefillContext,
    TokenEvent,
)
from mycelium_router.transports.iroh import (
    IrohTransport,
    IrohTransportError,
    _InboundFrame,
    _PendingSend,
)
from mycelium_router.wire import decode_frame, encode_frame
from test_router_iroh_integration import _locked_route
from test_router_iroh_transport import (
    _Hub,
    _PausedAcquireSemaphore,
    _binding,
    _transport,
)

from ._harness import join_bounded, run_in_thread


class _CancellationRouter:
    def __init__(self) -> None:
        self.calls: list[tuple[PathCancellation, str | None]] = []
        self.received = threading.Event()

    def receive_path_cancellation(self, cancellation, *, source_node_id=None):
        self.calls.append((cancellation, source_node_id))
        self.received.set()
        return True


class _OwnerCancellationRouter(_CancellationRouter):
    def apply_controlled_path_cancellation(self, cancellation):
        self.calls.append((cancellation, None))
        self.received.set()
        return True


def _register_sender_path(transport, cancellation: PathCancellation) -> None:
    transport.remember_entry(cancellation.request_id, "local-node")
    with transport._state_lock:
        transport._participant_nodes_by_path[cancellation.path_id] = frozenset(
            {"local-node", "peer-node"}
        )


def _wait_until(predicate, *, timeout: float = 1.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            raise AssertionError("bounded condition deadline expired")
        time.sleep(0.002)


def _assert_all_clients_closed(hub: _Hub) -> None:
    assert hub.clients
    assert all(not client.connected for client in hub.clients)


class _CloseObservedTransport:
    def __init__(self, transport):
        self.transport = transport
        self.entered = threading.Event()

    def close(self) -> None:
        self.entered.set()
        self.transport.close()


class _PauseOnFirstSet(dict):
    def __init__(self) -> None:
        super().__init__()
        self.entered = threading.Event()
        self.release_set = threading.Event()
        self._armed = True

    def __setitem__(self, key, value) -> None:
        super().__setitem__(key, value)
        if self._armed:
            self._armed = False
            self.entered.set()
            if not self.release_set.wait(timeout=1):
                raise AssertionError("path publication barrier timed out")


def test_iroh_cancellation_frame_is_control_only_then_path_is_forgotten() -> None:
    hub = _Hub()
    transport = _transport(hub)
    transport.bind_router(_CancellationRouter())
    cancellation = PathCancellation("request-iroh-control", "path-iroh-control", 0, 3)
    transport.start()
    try:
        _register_sender_path(transport, cancellation)
        transport.send_path_cancellation(cancellation)
        _wait_until(lambda: len(hub.sent) == 1)
        _wait_until(lambda: not transport._cancellation_threads)

        _message_id, frame, _timeout, generation = hub.sent[0]
        decoded = decode_frame(frame)
        assert decoded.message == cancellation
        assert decoded.payload == b""
        assert generation == transport.peer_binding.generation
        assert transport.cancellation_observed(
            cancellation.request_id,
            cancellation.path_id,
            cancellation.path_attempt,
        )
        assert not transport.cancellation_observed(
            cancellation.request_id,
            cancellation.path_id,
            cancellation.path_attempt + 1,
        )
        assert not transport.cancellation_observed(
            "unrelated-request",
            cancellation.path_id,
            cancellation.path_attempt,
        )
        assert cancellation.path_id not in transport._participant_nodes_by_path
        assert cancellation.request_id not in transport._entry_nodes
        with pytest.raises(
            IrohTransportError, match="path_cancellation_source_not_entry"
        ):
            transport.send_path_cancellation(cancellation)
        assert transport.route_ready is False
    finally:
        transport.close()
    _assert_all_clients_closed(hub)


@pytest.mark.parametrize("entry_local", (False, True))
def test_iroh_controlled_cancellation_retires_local_path_without_send(
    entry_local: bool,
) -> None:
    hub = _Hub()
    transport = _transport(hub)
    router = _CancellationRouter()
    cancellation = PathCancellation(
        f"request-controlled-{entry_local}",
        f"path-controlled-{entry_local}",
        0,
        3,
    )
    transport.bind_router(router)
    with transport._state_lock:
        transport._entry_nodes[cancellation.request_id] = (
            "local-node" if entry_local else "peer-node"
        )
        transport._participant_nodes_by_path[cancellation.path_id] = frozenset(
            {"local-node", "peer-node"}
        )
    transport.start()
    try:
        assert transport.apply_controlled_path_cancellation(
            cancellation,
            entry_cancelled=entry_local,
        )
        assert hub.sent == []
        assert transport.cancellation_observed(
            cancellation.request_id,
            cancellation.path_id,
            cancellation.path_attempt,
        )
        assert cancellation.path_id not in transport._participant_nodes_by_path
        assert cancellation.request_id not in transport._entry_nodes
        assert router.calls == [
            (
                cancellation,
                "local-node" if entry_local else "peer-node",
            )
        ]
    finally:
        transport.close()
    _assert_all_clients_closed(hub)


def test_controlled_cancellation_tombstones_before_transport_registration() -> None:
    hub = _Hub()
    transport = _transport(hub)
    router = _OwnerCancellationRouter()
    cancellation = PathCancellation(
        "request-controlled-before-registration",
        "path-controlled-before-registration",
        0,
        3,
    )
    transport.bind_router(router)
    transport.start()
    try:
        assert transport.apply_controlled_path_cancellation(
            cancellation,
            entry_cancelled=False,
        )
        assert transport.cancellation_observed(
            cancellation.request_id,
            cancellation.path_id,
            cancellation.path_attempt,
        )
        with pytest.raises(IrohTransportError, match="path_cancelled"):
            transport.remember_entry(cancellation.request_id, "local-node")
        assert router.calls == [(cancellation, None)]
    finally:
        transport.close()
    _assert_all_clients_closed(hub)


def test_controlled_cancellation_retires_transport_registry_when_router_release_raises(
) -> None:
    """A partial lower-layer release cannot orphan transport ownership."""

    class RaisingRouter(_OwnerCancellationRouter):
        def apply_controlled_path_cancellation(self, cancellation):
            super().apply_controlled_path_cancellation(cancellation)
            raise RuntimeError("transient_resource_release")

    hub = _Hub()
    transport = _transport(hub)
    router = RaisingRouter()
    cancellation = PathCancellation(
        "request-controlled-release-error",
        "path-controlled-release-error",
        0,
        3,
    )
    transport.bind_router(router)
    with transport._state_lock:
        transport._entry_nodes[cancellation.request_id] = "local-node"
        transport._path_graphs[cancellation.path_id] = object()
        transport._participant_nodes_by_path[cancellation.path_id] = frozenset(
            {"local-node", "peer-node"}
        )
    transport.start()
    try:
        with pytest.raises(RuntimeError, match="transient_resource_release"):
            transport.apply_controlled_path_cancellation(
                cancellation,
                entry_cancelled=False,
            )

        assert transport.cancellation_observed(
            cancellation.request_id,
            cancellation.path_id,
            cancellation.path_attempt,
        )
        assert transport.cancellation_cleanup_state(
            cancellation.request_id,
            cancellation.path_id,
            cancellation.path_attempt,
        )["entry_registered"] is False
        assert cancellation.path_id not in transport._path_graphs
        assert cancellation.path_id not in transport._participant_nodes_by_path
    finally:
        transport.close()
    _assert_all_clients_closed(hub)


def test_controlled_cancellation_passes_owner_deadline_to_delivery_worker() -> None:
    hub = _Hub()
    transport = _transport(hub)
    transport.bind_router(_OwnerCancellationRouter())
    cancellation = PathCancellation(
        "request-owner-deadline",
        "path-owner-deadline",
        0,
        3,
    )
    message_id = b"o" * 16
    owner_deadline = time.monotonic() + 1.5
    captured: list[float | None] = []

    def capture_start(
        _control,
        candidate_message_id,
        candidate_pending,
        *,
        cleanup_deadline_monotonic_s=None,
    ) -> None:
        assert candidate_message_id == message_id
        assert candidate_pending is transport._pending[message_id]
        captured.append(cleanup_deadline_monotonic_s)

    transport._start_delivery_cancel_locked = capture_start
    transport.start()
    try:
        with transport._state_lock:
            transport._pending[message_id] = _PendingSend(
                generation=transport.peer_binding.generation,
                request_id=cancellation.request_id,
                path_id=cancellation.path_id,
                path_attempt=cancellation.path_attempt,
                admission_started=True,
            )
        assert transport.apply_controlled_path_cancellation(
            cancellation,
            entry_cancelled=False,
            cleanup_deadline_monotonic_s=owner_deadline,
        )
        assert captured == [owner_deadline]
    finally:
        with transport._state_lock:
            transport._pending.pop(message_id, None)
        transport.close()
    _assert_all_clients_closed(hub)


def test_controlled_cancellation_blocks_later_recovery_attempt_publication() -> None:
    hub = _Hub()
    transport = _transport(hub)
    router = _OwnerCancellationRouter()
    cancellation = PathCancellation(
        "request-controlled-recovery",
        "path-controlled-recovery",
        0,
        3,
    )
    transport.bind_router(router)
    dispatched: list[tuple[str, bytes]] = []
    transport.__dict__["_send_or_dispatch"] = lambda destination, frame, **_kwargs: (
        dispatched.append((destination, frame))
    )
    transport.start()
    try:
        assert transport.apply_controlled_path_cancellation(
            cancellation,
            entry_cancelled=False,
        )
        locked = _locked_route()
        first_hop = locked.manifest.ordered_hops[0]
        transport.send_manifest_delta(
            ManifestDelta(
                request_id=cancellation.request_id,
                path_id=cancellation.path_id,
                path_attempt=1,
                hop_index=0,
                hop=first_hop,
            )
        )

        assert cancellation.request_id not in transport._entry_nodes
        assert dispatched == []
    finally:
        transport.close()
    _assert_all_clients_closed(hub)


def test_controlled_cancellation_cleans_terminal_entry_without_relay_send() -> None:
    """Owner fanout remains authoritative after the entry Router is terminal."""

    hub = _Hub()
    transport = _transport(hub)
    router = _CancellationRouter()
    cancellation = PathCancellation(
        "request-terminal-entry",
        "path-terminal-entry",
        0,
        3,
    )
    transport.bind_router(router)
    with transport._state_lock:
        transport._entry_nodes[cancellation.request_id] = "local-node"
        transport._participant_nodes_by_path[cancellation.path_id] = frozenset(
            {"local-node", "peer-node"}
        )
    transport.start()
    try:
        assert transport.apply_controlled_path_cancellation(
            cancellation,
            entry_cancelled=False,
        )
        assert hub.sent == []
        assert hub.routed_sent == []
        assert router.calls == [(cancellation, "local-node")]
        assert transport.cancellation_observed(
            cancellation.request_id,
            cancellation.path_id,
            cancellation.path_attempt,
        )
        assert transport.cancellation_cleanup_complete(
            cancellation.request_id,
            cancellation.path_id,
            cancellation.path_attempt,
        )
    finally:
        transport.close()
    _assert_all_clients_closed(hub)


def test_controlled_cancel_between_manifest_accept_and_publication_wins() -> None:
    hub = _Hub()
    transport = _transport(hub)
    locked = _locked_route()
    cancellation = PathCancellation(
        locked.request_id,
        locked.path_id,
        locked.path_attempt,
        locked.build.graph.topology_version,
    )
    entry_node = locked.build.graph.stages[0].placements[0].node_id

    class CancellationWinsRegistration(_CancellationRouter):
        def register_path(self, *_args, **_kwargs):
            assert transport.apply_controlled_path_cancellation(
                cancellation,
                entry_cancelled=False,
            )
            return True

    router = CancellationWinsRegistration()
    transport.bind_router(router)
    transport.start()
    try:
        with transport._state_lock:
            transport._entry_nodes[cancellation.request_id] = entry_node
            transport._participant_nodes_by_path[cancellation.path_id] = frozenset(
                {transport.node_id}
            )
        transport._dispatch(
            decode_frame(encode_frame(locked)),
            source_node_id="node-b",
        )

        assert router.calls == [(cancellation, entry_node)]
        assert transport.cancellation_observed(
            cancellation.request_id,
            cancellation.path_id,
            cancellation.path_attempt,
        )
        assert cancellation.request_id not in transport._entry_nodes
        assert cancellation.path_id not in transport._path_graphs
        assert cancellation.path_id not in transport._participant_nodes_by_path
    finally:
        transport.close()
    _assert_all_clients_closed(hub)


@pytest.mark.parametrize("publisher", ("entry", "progressive", "delta", "locked"))
def test_cancelled_subject_cannot_republish_outbound_transport_metadata(
    publisher: str,
) -> None:
    hub = _Hub()
    transport = _transport(hub)
    locked = _locked_route()
    cancellation = PathCancellation(
        locked.request_id,
        locked.path_id,
        locked.path_attempt,
        locked.build.graph.topology_version,
    )
    first_hop = locked.build.ordered_hops[0]
    header = HopHeader(
        request_id=locked.request_id,
        path_id=locked.path_id,
        path_attempt=locked.path_attempt,
        phase="PREFILL",
        token_index=-1,
        hop_index=0,
        source_placement_id="",
        destination_placement_id=first_hop.placement_id,
        topology_version=locked.build.graph.topology_version,
        idempotency_key="cancelled-outbound-publication",
    )
    transport.bind_router(_CancellationRouter())
    transport.start()
    try:
        with transport._state_lock:
            transport._remember_cancellation_locked(cancellation)
        with pytest.raises(IrohTransportError, match="path_cancelled"):
            if publisher == "entry":
                transport.remember_entry(
                    locked.request_id,
                    transport.node_id,
                )
            elif publisher == "progressive":
                transport.send_hop(
                    header,
                    ProgressivePrefillContext(
                        graph=locked.build.graph,
                        request=locked.build.request,
                        build=replace(
                            locked.build,
                            ordered_hops=(first_hop,),
                        ),
                        payload=b"activation",
                    ),
                )
            elif publisher == "delta":
                transport.send_manifest_delta(
                    ManifestDelta(
                        request_id=locked.request_id,
                        path_id=locked.path_id,
                        path_attempt=locked.path_attempt,
                        hop_index=0,
                        hop=first_hop,
                    )
                )
            else:
                transport.send_manifest_locked(locked)

        assert cancellation.request_id not in transport._entry_nodes
        assert cancellation.path_id not in transport._path_graphs
        assert cancellation.path_id not in transport._participant_nodes_by_path
    finally:
        transport.close()
    _assert_all_clients_closed(hub)


def test_controlled_cancellation_fences_late_exact_subject_send() -> None:
    hub = _Hub()
    transport = _transport(hub)
    transport.bind_router(_CancellationRouter())
    cancellation = PathCancellation("request-a", "path-a", 1, 3)
    with transport._state_lock:
        transport._entry_nodes[cancellation.request_id] = "local-node"
        transport._participant_nodes_by_path[cancellation.path_id] = frozenset(
            {"local-node", "peer-node"}
        )
    transport.start()
    try:
        assert transport.apply_controlled_path_cancellation(
            cancellation,
            entry_cancelled=False,
        )
        late = TokenEvent(
            request_id=cancellation.request_id,
            path_id=cancellation.path_id,
            path_attempt=cancellation.path_attempt,
            token_index=0,
            token_id=7,
            sampling_counter=1,
        )
        with pytest.raises(IrohTransportError, match="path_cancelled"):
            transport.send_router_frame(
                encode_frame(late),
                destination_node_id="peer-node",
            )
        assert transport._pending == {}
        assert hub.sent == []
        assert hub.routed_sent == []
    finally:
        transport.close()
    _assert_all_clients_closed(hub)


def test_controlled_cancellation_fences_send_waiting_for_data_client() -> None:
    class PausedSendOperation:
        def __init__(self) -> None:
            self.entered = threading.Event()
            self.resume = threading.Event()
            self.released = False

        def acquire(self, *, timeout: float) -> bool:
            assert timeout > 0
            self.entered.set()
            return self.resume.wait(timeout=timeout)

        def release(self) -> None:
            self.released = True

    hub = _Hub()
    transport = _transport(hub)
    transport.bind_router(_CancellationRouter())
    cancellation = PathCancellation("request-queued", "path-queued", 1, 3)
    event = TokenEvent(
        request_id=cancellation.request_id,
        path_id=cancellation.path_id,
        path_attempt=cancellation.path_attempt,
        token_index=0,
        token_id=7,
        sampling_counter=1,
    )
    with transport._state_lock:
        transport._entry_nodes[cancellation.request_id] = "local-node"
        transport._participant_nodes_by_path[cancellation.path_id] = frozenset(
            {"local-node", "peer-node"}
        )
    transport.start()
    paused = PausedSendOperation()
    transport.__dict__["_send_operation_lock"] = paused
    sender = None
    try:
        sender, results, errors = run_in_thread(
            lambda: transport.send_router_frame(
                encode_frame(event),
                destination_node_id="peer-node",
            )
        )
        assert paused.entered.wait(timeout=1.0)
        assert len(transport._pending) == 1
        [pending] = transport._pending.values()
        assert pending.admission_started is False
        assert transport.apply_controlled_path_cancellation(
            cancellation,
            entry_cancelled=False,
        )
        paused.resume.set()
        join_bounded(sender)

        assert results == []
        assert len(errors) == 1
        assert isinstance(errors[0], IrohTransportError)
        assert errors[0].code == "path_cancelled"
        assert paused.released is True
        assert transport._pending == {}
        assert hub.sent == []
        assert hub.routed_sent == []
    finally:
        paused.resume.set()
        if sender is not None and sender.is_alive():
            join_bounded(sender)
        transport.close()
    _assert_all_clients_closed(hub)


def test_controlled_cancellation_only_interrupts_exact_deferred_forward() -> None:
    cancelled_message_ids: list[bytes] = []
    cancellation_observed = threading.Event()

    class Control:
        @staticmethod
        def cancel(message_id: bytes, *, timeout: float) -> None:
            assert timeout > 0
            cancelled_message_ids.append(message_id)
            cancellation_observed.set()

    transport = _transport(_Hub())
    cancellation = PathCancellation("request-a", "path-a", 1, 3)
    exact_forward_id = b"f" * 16
    exact_regular_id = b"r" * 16
    unrelated_id = b"u" * 16
    forward_client = object()
    with transport._state_lock:
        transport._control_client = Control()
        transport._forward_client = forward_client
        transport._active_forward_scope = (
            cancellation.request_id,
            cancellation.path_id,
            cancellation.path_attempt,
        )
        transport._pending[exact_forward_id] = _PendingSend(
            7,
            cancellation.request_id,
            cancellation.path_id,
            cancellation.path_attempt,
            cancellable_forward=True,
            admission_started=True,
        )
        transport._pending[exact_regular_id] = _PendingSend(
            7,
            cancellation.request_id,
            cancellation.path_id,
            cancellation.path_attempt,
            admission_started=True,
        )
        transport._pending[unrelated_id] = _PendingSend(
            7,
            "request-b",
            "path-b",
            cancellation.path_attempt,
            admission_started=True,
            cancellable_forward=True,
        )
        interrupted = transport._cancel_forward_scope_locked(cancellation)

    assert interrupted is forward_client
    assert transport._forward_client is None
    assert transport._pending[exact_forward_id].cancelled is True
    assert transport._pending[exact_forward_id].reason == "path_cancelled"
    assert transport._pending[exact_regular_id].cancelled is False
    assert transport._pending[unrelated_id].cancelled is False
    assert cancellation_observed.wait(timeout=1.0)
    _wait_until(lambda: transport._pending[exact_forward_id].cancel_confirmed)
    assert cancelled_message_ids == [exact_forward_id]
    assert (
        transport.cancellation_cleanup_state("request-a", "path-a", 1)[
            "pending_delivery_count"
        ]
        == 1
    )


def test_controlled_cancellation_cancels_exact_regular_pending_delivery() -> None:
    cancelled_message_ids: list[bytes] = []
    cancellation_observed = threading.Event()

    class Control:
        @staticmethod
        def cancel(message_id: bytes, *, timeout: float) -> None:
            assert timeout > 0
            cancelled_message_ids.append(message_id)
            cancellation_observed.set()

    transport = _transport(_Hub())
    cancellation = PathCancellation("request-a", "path-a", 1, 3)
    exact_id = b"e" * 16
    unrelated_id = b"u" * 16
    with transport._state_lock:
        transport._control_client = Control()
        transport._pending[exact_id] = _PendingSend(
            7,
            cancellation.request_id,
            cancellation.path_id,
            cancellation.path_attempt,
            admission_started=True,
        )
        transport._pending[unrelated_id] = _PendingSend(
            7,
            "request-b",
            "path-b",
            cancellation.path_attempt,
            admission_started=True,
        )
        transport._cancel_pending_scope_locked(cancellation)

    assert cancellation_observed.wait(timeout=1.0)
    assert cancelled_message_ids == [exact_id]
    assert transport._pending[exact_id].cancelled is True
    assert transport._pending[exact_id].reason == "path_cancelled"
    assert transport._pending[unrelated_id].cancelled is False
    deadline = time.monotonic() + 1.0
    while (
        not transport._pending[exact_id].cancel_confirmed
        and time.monotonic() < deadline
    ):
        time.sleep(0.001)
    assert transport._pending[exact_id].cancel_confirmed is True
    assert transport.cancellation_cleanup_complete("request-a", "path-a", 1)
    assert not transport.cancellation_cleanup_complete("request-b", "path-b", 1)


def test_cleanup_waits_for_sidecar_cancellation_acknowledgement() -> None:
    cancel_entered = threading.Event()
    release_cancel = threading.Event()

    class Control:
        @staticmethod
        def cancel(message_id: bytes, *, timeout: float) -> None:
            del message_id, timeout
            cancel_entered.set()
            assert release_cancel.wait(timeout=1.0)

    transport = _transport(_Hub())
    cancellation = PathCancellation("request-a", "path-a", 1, 3)
    exact_id = b"e" * 16
    with transport._state_lock:
        transport._control_client = Control()
        transport._pending[exact_id] = _PendingSend(
            7,
            cancellation.request_id,
            cancellation.path_id,
            cancellation.path_attempt,
            admission_started=True,
        )
        transport._cancel_pending_scope_locked(cancellation)

    assert cancel_entered.wait(timeout=1.0)
    assert not transport.cancellation_cleanup_complete("request-a", "path-a", 1)
    release_cancel.set()
    deadline = time.monotonic() + 1.0
    while (
        not transport.cancellation_cleanup_complete("request-a", "path-a", 1)
        and time.monotonic() < deadline
    ):
        time.sleep(0.001)
    assert transport.cancellation_cleanup_complete("request-a", "path-a", 1)


def test_sidecar_cancellation_ack_uses_cleanup_budget_not_poll_cadence() -> None:
    observed_timeout: list[float] = []
    cancellation_observed = threading.Event()

    class Control:
        @staticmethod
        def cancel(message_id: bytes, *, timeout: float) -> None:
            del message_id
            observed_timeout.append(timeout)
            if timeout <= 0.05:
                raise TimeoutError("poll cadence is not an acknowledgement budget")
            cancellation_observed.set()

    transport = _transport(_Hub())
    transport.poll_interval_seconds = 0.05
    cancellation = PathCancellation("request-a", "path-a", 1, 3)
    exact_id = b"e" * 16
    with transport._state_lock:
        transport._control_client = Control()
        transport._pending[exact_id] = _PendingSend(
            7,
            cancellation.request_id,
            cancellation.path_id,
            cancellation.path_attempt,
            admission_started=True,
        )
        transport._cancel_pending_scope_locked(cancellation)

    assert cancellation_observed.wait(timeout=1.0)
    _wait_until(lambda: transport._pending[exact_id].cancel_confirmed)
    assert len(observed_timeout) == 1
    assert transport.poll_interval_seconds < observed_timeout[0] <= 1.0
    assert transport.cancellation_cleanup_complete("request-a", "path-a", 1)


def test_concurrent_delivery_cancels_use_independent_sidecar_clients() -> None:
    shared_calls: list[bytes] = []

    class SharedControl:
        @staticmethod
        def cancel(message_id: bytes, *, timeout: float) -> None:
            del timeout
            shared_calls.append(message_id)
            raise AssertionError("production cancellation used shared control client")

    class DedicatedControl:
        def __init__(self) -> None:
            self.calls: list[bytes] = []

        def cancel(self, message_id: bytes, *, timeout: float) -> None:
            assert timeout > 0
            self.calls.append(message_id)

    transport = _transport(_Hub())
    first = DedicatedControl()
    second = DedicatedControl()
    cancellation = PathCancellation("request-a", "path-a", 1, 3)
    message_ids = (b"a" * 16, b"b" * 16)
    with transport._state_lock:
        transport._control_client = SharedControl()
        transport._cancellation_clients = (first, second)
        transport._available_cancellation_clients.put_nowait(first)
        transport._available_cancellation_clients.put_nowait(second)
        for message_id in message_ids:
            transport._pending[message_id] = _PendingSend(
                7,
                cancellation.request_id,
                cancellation.path_id,
                cancellation.path_attempt,
                admission_started=True,
            )
        transport._cancel_pending_scope_locked(cancellation)

    _wait_until(
        lambda: all(
            transport._pending[message_id].cancel_confirmed
            for message_id in message_ids
        )
    )
    assert shared_calls == []
    assert sorted(first.calls + second.calls) == sorted(message_ids)
    assert transport._available_cancellation_clients.qsize() == 2
    assert transport.cancellation_cleanup_complete("request-a", "path-a", 1)


def test_obsolete_admission_race_releases_client_for_live_cleanup_blocker() -> None:
    """A retired send must not monopolize the bounded cancellation pool."""

    from mycelium_iroh_sidecar import ProtocolError

    stale_id = b"s" * 16
    live_id = b"l" * 16
    stale_attempted = threading.Event()
    release_first_unknown = threading.Event()
    live_cancelled = threading.Event()
    stale_admission_states: list[bool] = []

    class DedicatedControl:
        connected = True

        @staticmethod
        def cancel(message_id: bytes, *, timeout: float) -> None:
            assert 0 < timeout <= 0.2
            if message_id == stale_id:
                stale_admission_states.append(stale_pending.admission_finished)
                stale_attempted.set()
                if len(stale_admission_states) == 1:
                    assert release_first_unknown.wait(timeout=0.2)
                raise ProtocolError("unknown_message")
            assert message_id == live_id
            live_cancelled.set()

    stale_pending = _PendingSend(
        7,
        "request-a",
        "path-a",
        1,
        admission_started=True,
    )
    transport = _transport(_Hub())
    dedicated = DedicatedControl()
    cancellation = PathCancellation("request-a", "path-a", 1, 3)
    cleanup_deadline = time.monotonic() + 0.4
    with transport._state_lock:
        transport._cancellation_clients = (dedicated,)
        transport._available_cancellation_clients.put_nowait(dedicated)
        transport._pending[stale_id] = stale_pending
        transport._cancel_pending_scope_locked(
            cancellation,
            cleanup_deadline_monotonic_s=cleanup_deadline,
        )

    assert stale_attempted.wait(timeout=0.2)
    with transport._state_lock:
        transport._pending[live_id] = _PendingSend(
            7,
            cancellation.request_id,
            cancellation.path_id,
            cancellation.path_attempt,
            admission_started=True,
        )
        transport._cancel_pending_scope_locked(
            cancellation,
            cleanup_deadline_monotonic_s=cleanup_deadline,
        )
        # The original data sender has now unwound after its admission race.
        # Its sidecar admission call cannot create the message after the valid
        # unknown-message response, so the cancellation lane can move on.
        transport._pending[stale_id].admission_finished = True
        transport._pending.pop(stale_id)
    release_first_unknown.set()

    assert live_cancelled.wait(timeout=0.2)
    _wait_until(lambda: transport._pending[live_id].cancel_confirmed)
    assert stale_admission_states == [False, True]
    assert transport.cancellation_cleanup_complete("request-a", "path-a", 1)


def test_delivery_cancel_retries_connected_sidecar_admission_race() -> None:
    class DedicatedControl:
        connected = True

        def __init__(self) -> None:
            self.calls = 0

        def cancel(self, message_id: bytes, *, timeout: float) -> None:
            del message_id
            assert 0 < timeout <= 0.2
            self.calls += 1
            if self.calls == 1:
                from mycelium_iroh_sidecar import ProtocolError

                raise ProtocolError("unknown_message")

    transport = _transport(_Hub())
    dedicated = DedicatedControl()
    cancellation = PathCancellation("request-a", "path-a", 1, 3)
    exact_id = b"e" * 16
    with transport._state_lock:
        transport._cancellation_clients = (dedicated,)
        transport._available_cancellation_clients.put_nowait(dedicated)
        transport._pending[exact_id] = _PendingSend(
            7,
            cancellation.request_id,
            cancellation.path_id,
            cancellation.path_attempt,
            admission_started=True,
        )
        transport._cancel_pending_scope_locked(cancellation)

    _wait_until(lambda: transport._pending[exact_id].cancel_confirmed)
    assert dedicated.calls == 2
    assert transport._available_cancellation_clients.qsize() == 1
    assert transport.cancellation_cleanup_complete("request-a", "path-a", 1)


def test_delivery_cancel_reauthenticates_timed_out_sidecar_lane() -> None:
    class DedicatedControl:
        endpoint_id = "local-endpoint"

        def __init__(self) -> None:
            self.connected = True
            self.cancel_calls = 0
            self.connect_calls = 0
            self.configure_calls = 0

        def cancel(self, message_id: bytes, *, timeout: float) -> None:
            del message_id
            assert 0 < timeout <= 0.2
            self.cancel_calls += 1
            if self.cancel_calls == 1:
                self.connected = False
                raise TimeoutError("sidecar request deadline")

        def close(self) -> None:
            self.connected = False

        def connect(self, *, deadline: float) -> None:
            assert deadline > time.monotonic()
            self.connect_calls += 1
            self.connected = True

        def configure_peers(self, peers, *, timeout: float) -> None:
            assert peers
            assert timeout > 0
            self.configure_calls += 1

        def configure_peer(
            self,
            endpoint_id,
            endpoint_addr,
            *,
            generation,
            timeout: float,
        ) -> None:
            assert endpoint_id
            assert endpoint_addr
            assert generation > 0
            assert timeout > 0
            self.configure_calls += 1

    transport = _transport(_Hub())
    transport.bind_router(_CancellationRouter())
    dedicated = DedicatedControl()
    cancellation = PathCancellation("request-a", "path-a", 1, 3)
    exact_id = b"e" * 16
    with transport._state_lock:
        transport._running = True
        transport._cancellation_clients = (dedicated,)
        transport._available_cancellation_clients.put_nowait(dedicated)
        transport._pending[exact_id] = _PendingSend(
            7,
            cancellation.request_id,
            cancellation.path_id,
            cancellation.path_attempt,
            admission_started=True,
        )
        transport._cancel_pending_scope_locked(cancellation)

    _wait_until(lambda: transport._pending[exact_id].cancel_confirmed)
    assert dedicated.cancel_calls == 2
    assert dedicated.connect_calls == 1
    assert dedicated.configure_calls == 1
    assert dedicated.connected is True
    assert transport._available_cancellation_clients.qsize() == 1


def test_failed_sidecar_cancellation_remains_cleanup_blocker() -> None:
    cancel_attempted = threading.Event()

    class Control:
        @staticmethod
        def cancel(message_id: bytes, *, timeout: float) -> None:
            del message_id, timeout
            cancel_attempted.set()
            raise RuntimeError("sidecar cancellation unavailable")

    transport = _transport(_Hub())
    cancellation = PathCancellation("request-a", "path-a", 1, 3)
    exact_id = b"e" * 16
    with transport._state_lock:
        transport._control_client = Control()
        transport._pending[exact_id] = _PendingSend(
            7,
            cancellation.request_id,
            cancellation.path_id,
            cancellation.path_attempt,
            admission_started=True,
        )
        transport._cancel_pending_scope_locked(cancellation)

    assert cancel_attempted.wait(timeout=1.0)
    deadline = time.monotonic() + 1.0
    while (
        exact_id in transport._delivery_cancel_threads and time.monotonic() < deadline
    ):
        time.sleep(0.001)
    assert transport._pending[exact_id].cancel_confirmed is False
    assert transport._pending[exact_id].cancel_started is False
    assert not transport.cancellation_cleanup_complete("request-a", "path-a", 1)


def test_node_owner_retries_retryable_async_sidecar_cancellation() -> None:
    class RetryOnceControl:
        def __init__(self) -> None:
            self.cancel_calls = 0

        def cancel(self, message_id: bytes, *, timeout: float) -> None:
            assert message_id == b"e" * 16
            assert timeout > 0
            self.cancel_calls += 1
            if self.cancel_calls == 1:
                raise RuntimeError("transient sidecar cancellation failure")

        def close(self) -> None:
            pass

    class CleanRuntime:
        @staticmethod
        def cancel(path_id: str) -> None:
            assert path_id == "path-a"

        @staticmethod
        def kv_subject_clean(
            request_id: str,
            path_id: str,
            path_attempt: int,
        ) -> bool:
            assert (request_id, path_id, path_attempt) == (
                "request-a",
                "path-a",
                1,
            )
            return True

    class IdempotentRouter:
        @staticmethod
        def cancel_local(request_id: str) -> bool:
            assert request_id == "request-a"
            return True

    transport = _transport(_Hub())
    transport.bind_router(_OwnerCancellationRouter())
    control = RetryOnceControl()
    exact_id = b"e" * 16
    with transport._state_lock:
        transport._running = True
        transport._control_client = control
        transport._pending[exact_id] = _PendingSend(
            7,
            "request-a",
            "path-a",
            1,
            admission_started=True,
        )

    service = object.__new__(PhysicalNodeService)
    service.__dict__.update(
        runtime=CleanRuntime(),
        transport=transport,
        router=IdempotentRouter(),
        _control_lock=threading.RLock(),
        _cancellation_worker_errors={},
    )
    service._apply_cancellation_until_deadline(
        {
            "request_id": "request-a",
            "path_id": "path-a",
            "path_attempt": 1,
            "topology_generation": 3,
            "deadline_budget_ms": 1_500,
        }
    )

    assert control.cancel_calls == 2
    assert transport._pending[exact_id].cancel_confirmed is True
    assert transport.cancellation_cleanup_complete("request-a", "path-a", 1)
    assert service._cancellation_worker_errors == {}
    transport.close()


def test_controlled_cancellation_removes_only_exact_queued_forwards() -> None:
    transport = _transport(_Hub(), queue_capacity=4)
    cancellation = PathCancellation("request-a", "path-a", 1, 3)
    exact_scope = ("request-a", "path-a", 1)
    unrelated_scope = ("request-b", "path-b", 1)
    exact_items = (
        ("peer-node", b"one", *exact_scope),
        ("peer-node", b"two", *exact_scope),
    )
    unrelated_item = ("peer-node", b"other", *unrelated_scope)
    for item in (*exact_items, unrelated_item):
        transport._forward_queue.put_nowait(item)
    with transport._state_lock:
        transport._forward_scopes[exact_scope] = 2
        transport._forward_scopes[unrelated_scope] = 1
        transport._cancel_forward_scope_locked(cancellation)

    assert tuple(transport._forward_queue.queue) == (unrelated_item,)
    assert transport._forward_queue.unfinished_tasks == 1
    assert exact_scope not in transport._forward_scopes
    assert transport._forward_scopes[unrelated_scope] == 1
    transport._forward_queue.get_nowait()
    transport._forward_queue.task_done()


def test_cancelled_scope_cannot_enqueue_a_new_deferred_forward() -> None:
    transport = _transport(_Hub())
    cancellation = PathCancellation("request-a", "path-a", 1, 3)
    report = FailureReport(
        request_id=cancellation.request_id,
        path_id=cancellation.path_id,
        path_attempt=cancellation.path_attempt,
        token_index=0,
        scope="PATH",
        reason="cancelled",
    )
    with transport._state_lock:
        transport._running = True
        transport._dispatcher_thread = threading.current_thread()
        transport._cancel_forward_scope_locked(cancellation)

    transport._send_or_dispatch("peer-node", encode_frame(report))

    assert transport._forward_queue.empty()
    assert transport._forward_queue.unfinished_tasks == 0
    assert transport._forward_scopes == {}
    assert transport._forward_thread is None


def test_active_forward_interrupt_reconnects_without_blocking_next_scope() -> None:
    hub = _Hub()
    hub.block_confirmed_send = True
    transport = _transport(hub, delivery_timeout_seconds=1.0)
    transport.bind_router(_CancellationRouter())
    cancellation = PathCancellation("request-a", "path-a", 1, 3)
    _register_sender_path(transport, cancellation)
    transport.start()
    old_forward_client = transport._forward_client
    report = FailureReport(
        request_id=cancellation.request_id,
        path_id=cancellation.path_id,
        path_attempt=cancellation.path_attempt,
        token_index=0,
        scope="PATH",
        reason="cancelled",
    )
    exact_scope = (
        cancellation.request_id,
        cancellation.path_id,
        cancellation.path_attempt,
    )
    transport._forward_queue.put_nowait(
        ("peer-node", encode_frame(report), *exact_scope)
    )
    with transport._state_lock:
        transport._forward_scopes[exact_scope] = 1
        worker = threading.Thread(target=transport._forward_loop, daemon=True)
        transport._forward_thread = worker
        worker.start()
    try:
        assert hub.confirmed_send_entered.wait(timeout=1.0)
        _wait_until(lambda: transport._active_forward_scope == exact_scope)

        assert transport.apply_controlled_path_cancellation(
            cancellation,
            entry_cancelled=True,
        )
        worker.join(timeout=1.0)

        assert not worker.is_alive()
        assert old_forward_client is not None
        assert old_forward_client.connected is False
        assert transport.fatal_error is None
        assert transport.cancellation_cleanup_complete(*exact_scope)

        hub.block_confirmed_send = False
        next_scope = ("request-b", "path-b", 0)
        next_report = replace(
            report,
            request_id=next_scope[0],
            path_id=next_scope[1],
            path_attempt=next_scope[2],
        )
        transport._forward_queue.put_nowait(
            ("peer-node", encode_frame(next_report), *next_scope)
        )
        with transport._state_lock:
            transport._forward_scopes[next_scope] = 1
            replacement_worker = threading.Thread(
                target=transport._forward_loop,
                daemon=True,
            )
            transport._forward_thread = replacement_worker
            replacement_worker.start()
        replacement_worker.join(timeout=1.0)

        assert not replacement_worker.is_alive()
        assert transport._forward_client is not None
        assert transport._forward_client is not old_forward_client
        assert hub.confirmed_sender_indices[-1] == transport._forward_client.index
        assert next_scope not in transport._forward_scopes
    finally:
        transport.close()
    _assert_all_clients_closed(hub)


def test_forward_client_construction_failure_is_request_scoped(monkeypatch) -> None:
    transport = _transport(_Hub())
    scope = ("request-fd-exhausted", "path-fd-exhausted", 0)
    report = FailureReport(
        request_id=scope[0],
        path_id=scope[1],
        path_attempt=scope[2],
        token_index=0,
        scope="PATH",
        reason="relay_failed",
    )

    def fail_client_construction():
        raise OSError(24, "Too many open files")

    monkeypatch.setattr(
        transport,
        "_forward_client_for_send",
        fail_client_construction,
    )
    transport._forward_queue.put_nowait(
        ("peer-node", encode_frame(report), *scope)
    )
    with transport._state_lock:
        transport._forward_scopes[scope] = 1
        worker = threading.Thread(target=transport._forward_loop, daemon=True)
        transport._forward_thread = worker
        worker.start()

    worker.join(timeout=1.0)

    assert not worker.is_alive()
    assert transport.fatal_error is None
    assert transport._active_forward_scope is None
    assert scope not in transport._forward_scopes
    event = transport.evidence().scoped_events[-1]
    assert event["event"] == "failure"
    assert event["request_id"] == scope[0]
    assert event["code"] == "forward_delivery_failed"


def test_cleanup_observation_ignores_unrelated_request_traffic() -> None:
    hub = _Hub()
    transport = _transport(hub)
    with transport._state_lock:
        transport._pending[b"p" * 16] = _PendingSend(
            7,
            "request-b",
            "path-b",
            0,
        )
        transport._inflight_received[b"i" * 16] = _InboundFrame(
            b"digest",
            "request-b",
            "path-b",
            0,
        )

    assert transport.cancellation_cleanup_complete("request-a", "path-a", 0)
    assert not transport.cancellation_cleanup_complete("request-b", "path-b", 0)


def test_counter_snapshot_is_local_and_bounded() -> None:
    transport = _transport(_Hub())
    with transport._state_lock:
        transport._remote_frames_sent = 11
        transport._remote_frames_received = 12
        transport._router_frames_dispatched = 13
        transport._duplicate_frames = 2

    assert transport.counter_snapshot() == {
        "remote_frames_sent": 11,
        "remote_frames_received": 12,
        "router_frames_dispatched": 13,
        "duplicate_frames": 2,
    }


def test_cleanup_observation_requires_exact_path_attempt() -> None:
    hub = _Hub()
    transport = _transport(hub)
    with transport._state_lock:
        transport._pending[b"a" * 16] = _PendingSend(
            7,
            "request-a",
            "path-a",
            None,
        )

    assert not transport.cancellation_cleanup_complete("request-a", "path-a", 1)
    assert not transport.cancellation_cleanup_complete("request-a", "path-a")


def test_cleanup_observation_isolated_from_newer_attempt() -> None:
    hub = _Hub()
    transport = _transport(hub)
    with transport._state_lock:
        transport._pending[b"n" * 16] = _PendingSend(
            7,
            "request-a",
            "path-a",
            2,
        )

    assert transport.cancellation_cleanup_complete("request-a", "path-a", 1)
    assert not transport.cancellation_cleanup_complete("request-a", "path-a", 2)


def test_scoped_send_failure_does_not_latch_shared_transport_fatal() -> None:
    hub = _Hub()
    hub.send_failure = RuntimeError("request-local delivery failure")
    transport = _transport(hub)
    transport.bind_router(_CancellationRouter())
    transport.start()
    try:
        frame = encode_frame(PathCancellation("request-a", "path-a", 0, 1))
        with pytest.raises(IrohTransportError, match="delivery_not_confirmed"):
            transport.send_router_frame(frame, destination_node_id="peer-node")

        assert transport.running is True
        assert transport.fatal_error is None
        events = transport.evidence().scoped_events
        assert events[-1] == {
            "protocol": "mycelium.iroh_scoped_transport_event.v1",
            "sequence": 1,
            "event": "failure",
            "request_id": "request-a",
            "path_id": "path-a",
            "path_attempt": 0,
            "peer_node_id": "peer-node",
            "peer_generation": 7,
            "code": "delivery_not_confirmed",
        }
    finally:
        transport.close()


def test_scoped_dispatch_failure_keeps_reader_alive_for_unrelated_request() -> None:
    class _OneRequestFailsRouter(_CancellationRouter):
        def receive_path_cancellation(self, cancellation, *, source_node_id=None):
            if cancellation.request_id == "request-fails":
                raise RuntimeError("request-local dispatch failure")
            return super().receive_path_cancellation(
                cancellation,
                source_node_id=source_node_id,
            )

    hub = _Hub()
    router = _OneRequestFailsRouter()
    transport = _transport(hub)
    transport.bind_router(router)
    transport.start()
    try:
        hub.deliver(
            b"f" * 16,
            encode_frame(PathCancellation("request-fails", "path-fails", 0, 1)),
        )
        _wait_until(lambda: b"f" * 16 in hub.acks)
        assert transport.running is True
        assert transport.fatal_error is None

        survivor = PathCancellation("request-survives", "path-survives", 0, 1)
        hub.deliver(b"s" * 16, encode_frame(survivor))
        assert router.received.wait(timeout=1)
        _wait_until(lambda: b"s" * 16 in hub.acks)

        assert router.calls == [(survivor, "peer-node")]
        assert transport.running is True
        assert transport.evidence().scoped_events[-1]["request_id"] == "request-fails"
    finally:
        transport.close()


def test_dispatcher_schedules_downstream_frame_before_ack_without_cycle() -> None:
    """A peer response must not hold the one inbound dispatcher awaiting itself."""

    hub = _Hub()
    hub.block_confirmed_send = True
    transport = _transport(hub, delivery_timeout_seconds=0.5)

    class _RespondingRouter(_CancellationRouter):
        def receive_hop(self, header, payload, *, source_node_id=None):
            response = TokenEvent(
                request_id="request-response",
                path_id="path-response",
                path_attempt=0,
                token_index=0,
                token_id=7,
                sampling_counter=1,
            )
            transport._send_or_dispatch("peer-node", encode_frame(response))
            self.received.set()
            return True

    router = _RespondingRouter()
    transport.bind_router(router)
    transport.start()
    inbound_id = b"d" * 16
    try:
        inbound = HopHeader(
            request_id="request-inbound",
            path_id="path-inbound",
            path_attempt=0,
            phase="DECODE",
            token_index=0,
            hop_index=1,
            source_placement_id="placement-a",
            destination_placement_id="placement-b",
            topology_version=1,
            idempotency_key="hop-response-cycle",
        )
        hub.deliver(inbound_id, encode_frame(inbound, b"activation"))

        assert hub.confirmed_send_entered.wait(timeout=1.0)
        _wait_until(lambda: inbound_id in hub.acks)
        assert router.received.is_set()
        assert transport.running is True
        assert transport.fatal_error is None
    finally:
        hub.release_confirmed_send.set()
        transport.close()


def test_iroh_cancellation_fans_out_to_every_configured_path_participant() -> None:
    hub = _Hub()
    third = _binding(
        node_id="third-node",
        endpoint_id="third-endpoint",
        generation=7,
    )
    transport = _transport(hub, peers=[third])
    transport.bind_router(_CancellationRouter())
    cancellation = PathCancellation(
        "request-iroh-fanout",
        "path-iroh-fanout",
        0,
        3,
    )
    transport.start()
    try:
        transport.remember_entry(cancellation.request_id, "local-node")
        with transport._state_lock:
            transport._participant_nodes_by_path[cancellation.path_id] = frozenset(
                {"local-node", "peer-node", "third-node"}
            )

        transport.send_path_cancellation(cancellation)
        _wait_until(lambda: len(hub.sent) + len(hub.routed_sent) == 2)
        _wait_until(lambda: not transport._cancellation_threads)

        primary_frame = hub.sent[0][1]
        third_endpoint, _message_id, third_frame, _timeout, generation = (
            hub.routed_sent[0]
        )
        assert decode_frame(primary_frame).message == cancellation
        assert decode_frame(third_frame).message == cancellation
        assert third_endpoint == "third-endpoint"
        assert generation == 7
        assert cancellation.path_id not in transport._participant_nodes_by_path
        assert cancellation.request_id not in transport._entry_nodes
    finally:
        transport.close()
    _assert_all_clients_closed(hub)


def test_concurrent_cancellations_use_independent_preconnected_clients() -> None:
    hub = _Hub()
    transport = _transport(hub, queue_capacity=4)
    transport.bind_router(_CancellationRouter())
    cancellations = tuple(
        PathCancellation(
            f"request-concurrent-{index}",
            f"path-concurrent-{index}",
            0,
            3,
        )
        for index in range(3)
    )
    transport.start()
    try:
        for cancellation in cancellations:
            _register_sender_path(transport, cancellation)
            transport.send_path_cancellation(cancellation)
        _wait_until(lambda: len(hub.sent) == len(cancellations))
        _wait_until(lambda: not transport._cancellation_threads)

        sender_indices = hub.confirmed_sender_indices[-len(cancellations) :]
        assert len(set(sender_indices)) == len(cancellations)
        assert all(index >= 4 for index in sender_indices)
    finally:
        transport.close()
    _assert_all_clients_closed(hub)


@pytest.mark.parametrize(
    ("setup", "error_code"),
    [
        ("not-entry", "path_cancellation_source_not_entry"),
        ("unknown-path", "unknown_path"),
        ("unbound-participant", "path_cancellation_participant_unbound"),
    ],
)
def test_iroh_sender_fails_closed_before_worker_creation(
    setup: str,
    error_code: str,
) -> None:
    hub = _Hub()
    transport = _transport(hub)
    transport.bind_router(_CancellationRouter())
    cancellation = PathCancellation(f"request-{setup}", f"path-{setup}", 0, 3)
    transport.start()
    try:
        if setup != "not-entry":
            transport.remember_entry(cancellation.request_id, "local-node")
        if setup == "unbound-participant":
            with transport._state_lock:
                transport._participant_nodes_by_path[cancellation.path_id] = frozenset(
                    {"local-node", "peer-node", "third-node"}
                )
        with pytest.raises(IrohTransportError, match=error_code):
            transport.send_path_cancellation(cancellation)
        assert transport._cancellation_threads == {}
        assert hub.sent == []
    finally:
        transport.close()
    _assert_all_clients_closed(hub)


def test_iroh_replayed_inbound_cancellation_dispatches_once() -> None:
    hub = _Hub()
    transport = _transport(hub)
    router = _CancellationRouter()
    cancellation = PathCancellation("request-iroh-replay", "path-iroh-replay", 0, 3)
    transport.bind_router(router)
    with transport._state_lock:
        transport._entry_nodes[cancellation.request_id] = "peer-node"
        transport._participant_nodes_by_path[cancellation.path_id] = frozenset(
            {"local-node", "peer-node"}
        )
    transport.start()
    try:
        message_id = b"r" * 16
        frame = encode_frame(cancellation)
        hub.deliver(message_id, frame)
        assert router.received.wait(timeout=1.0)
        hub.deliver(message_id, frame)
        _wait_until(lambda: transport.evidence().duplicate_frames == 1)

        assert router.calls == [(cancellation, "peer-node")]
        assert transport.evidence().router_frames_dispatched == 1
        assert cancellation.path_id not in transport._participant_nodes_by_path
    finally:
        transport.close()
    assert transport.worker_threads_alive == 0
    _assert_all_clients_closed(hub)


def test_iroh_workers_are_queue_bounded_and_close_race_cleans_up() -> None:
    hub = _Hub()
    hub.block_confirmed_send = True
    transport = _transport(
        hub,
        queue_capacity=2,
        delivery_timeout_seconds=0.2,
    )
    transport.bind_router(_CancellationRouter())
    cancellations = [
        PathCancellation(f"request-worker-{index}", f"path-worker-{index}", 0, 3)
        for index in range(3)
    ]
    transport.start()
    cancellation_threads: tuple[threading.Thread, ...] = ()
    close_thread = None
    try:
        for cancellation in cancellations:
            _register_sender_path(transport, cancellation)
        transport.send_path_cancellation(cancellations[0])
        assert hub.confirmed_send_entered.wait(timeout=1.0)
        with pytest.raises(
            IrohTransportError, match="path_cancellation_already_pending"
        ):
            transport.send_path_cancellation(cancellations[0])
        transport.send_path_cancellation(cancellations[1])
        _wait_until(lambda: len(transport._cancellation_threads) == 2)
        cancellation_threads = tuple(transport._cancellation_threads.values())
        assert len(cancellation_threads) == 2
        with pytest.raises(IrohTransportError, match="path_cancellation_queue_full"):
            transport.send_path_cancellation(cancellations[2])

        started = time.monotonic()
        observed_close = _CloseObservedTransport(transport)
        close_thread, close_results, close_errors = run_in_thread(observed_close.close)
        assert observed_close.entered.wait(timeout=1.0)
        _wait_until(lambda: not transport.running)
        hub.release_confirmed_send.set()
        join_bounded(close_thread)
        cleanup_latency = time.monotonic() - started

        assert close_results == [None]
        assert close_errors == []
        assert cleanup_latency < 1.0
        assert transport._cancellation_threads == {}
        assert all(not thread.is_alive() for thread in cancellation_threads)
        assert transport.worker_threads_alive == 0
        assert not transport.running
        assert transport.route_ready is False
        _assert_all_clients_closed(hub)
    finally:
        hub.release_confirmed_send.set()
        if close_thread is not None and close_thread.is_alive():
            join_bounded(close_thread)
        transport.close()


def test_manifest_publication_is_atomic_with_concurrent_cancellation() -> None:
    hub = _Hub()
    transport = IrohTransport(
        node_id="node-a",
        socket_path="/unused",
        bootstrap_secret=b"s" * 32,
        peer=replace(_binding(), node_id="node-b"),
        expected_endpoint_id="local-endpoint",
        queue_capacity=2,
        delivery_timeout_seconds=0.2,
        poll_interval_seconds=0.01,
        client_factory=hub.client,
    )
    transport.bind_router(_CancellationRouter())
    locked = _locked_route()
    cancellation = PathCancellation(locked.request_id, locked.path_id, 0, 3)
    published_graphs = _PauseOnFirstSet()
    transport.__dict__["_path_graphs"] = published_graphs
    dispatched: list[tuple[str, bytes]] = []
    transport.__dict__["_send_or_dispatch"] = lambda destination, frame, **_kwargs: (
        dispatched.append((destination, frame))
    )
    transport.start()
    transport.remember_entry(locked.request_id, "node-a")
    manifest_thread = None
    cancellation_thread = None
    try:
        manifest_thread, manifest_results, manifest_errors = run_in_thread(
            lambda: transport.send_manifest_locked(locked)
        )
        assert published_graphs.entered.wait(timeout=1.0)
        cancellation_thread, cancellation_results, cancellation_errors = run_in_thread(
            lambda: transport.send_path_cancellation(cancellation)
        )
        cancellation_thread.join(timeout=0.05)
        assert cancellation_thread.is_alive(), (
            "cancellation observed a partially published path instead of waiting"
        )

        published_graphs.release_set.set()
        join_bounded(manifest_thread)
        join_bounded(cancellation_thread)
        _wait_until(lambda: not transport._cancellation_threads)

        assert manifest_results == [None]
        assert manifest_errors == []
        assert cancellation_results == [None]
        assert cancellation_errors == []
        assert [destination for destination, _frame in dispatched] == [
            "node-a",
            "node-b",
            "node-b",
        ]
        assert locked.path_id not in transport._participant_nodes_by_path
        assert locked.request_id not in transport._entry_nodes
    finally:
        published_graphs.release_set.set()
        if manifest_thread is not None and manifest_thread.is_alive():
            join_bounded(manifest_thread)
        if cancellation_thread is not None and cancellation_thread.is_alive():
            join_bounded(cancellation_thread)
        transport.close()


def test_close_wins_cancellation_admission_race_without_starting_worker() -> None:
    hub = _Hub()
    transport = _transport(hub, queue_capacity=1)
    transport.bind_router(_CancellationRouter())
    cancellation = PathCancellation(
        "request-admission-race", "path-admission-race", 0, 3
    )
    transport.start()
    _register_sender_path(transport, cancellation)
    paused = _PausedAcquireSemaphore(transport._cancellation_slots)
    transport.__dict__["_cancellation_slots"] = paused

    sender, results, errors = run_in_thread(
        lambda: transport.send_path_cancellation(cancellation)
    )
    assert paused.entered.wait(timeout=1.0)
    transport.close()
    paused.resume.set()
    join_bounded(sender)

    assert results == []
    assert len(errors) == 1
    assert isinstance(errors[0], IrohTransportError)
    assert errors[0].code == "transport_closed"
    assert transport._cancellation_threads == {}
    assert paused.permit_available()
    assert hub.sent == []
