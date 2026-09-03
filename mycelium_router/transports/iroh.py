# SPDX-License-Identifier: AGPL-3.0-or-later
"""Production Router ``TransportPort`` over the authenticated iroh sidecar.

Router payloads remain byte-for-byte canonical ``mycelium.router_wire.v1``
frames.  No adapter envelope or simulator fallback exists on the remote path.
A confirmed receipt means the authenticated remote adapter dispatched and
ACKed the Router frame.  Dedupe and confirmation history are process-local;
simultaneous loss of both sidecars is not durable exactly-once delivery.
"""

from __future__ import annotations

from collections import OrderedDict, deque
from dataclasses import asdict, dataclass, is_dataclass
import hashlib
import json
from pathlib import Path
from queue import Empty, Full, Queue
import threading
import time
from typing import Any, Callable, Mapping, Sequence
import uuid

from mycelium_iroh_sidecar import (
    AuthenticationError,
    ProtocolError,
    QueueFull,
    SidecarClient,
    SidecarError,
)
from mycelium_router.contracts import (
    FailureReport,
    HopHeader,
    ManifestDelta,
    ManifestLocked,
    PathCancellation,
    PrefillChunkCompleted,
    ProgressivePrefillContext,
    ProgressivePrefillMessage,
    TokenEvent,
)
from mycelium_router.wire import (
    ROUTER_WIRE_PROTOCOL,
    DecodedFrame,
    WireError,
    decode_frame,
    decode_progressive_prefill,
    encode_frame,
    encode_progressive_prefill,
)


DELIVERY_SEMANTICS = "remote_router_dispatch_ack"
PROCESS_LIFETIME_LIMITATION = (
    "delivery confirmation and replay history are not durable across "
    "simultaneous sidecar process loss"
)
_SEEN_LIMIT = 4096
_TRACE_ID_BYTES = 128
_TRACE_ENTRY_BYTES = 512
_MAX_CONCURRENT_CANCELLATION_CLIENTS = 4
# The node's generation-fenced cancellation command reserves 500 ms of its
# non-restartable 2,000 ms authority for controller receipt collection.  A
# sidecar cancellation acknowledgement is part of the remaining local cleanup
# work, not a transport polling operation; limiting it to the 50 ms poll cadence
# turns ordinary physical latency into a permanent pending-delivery blocker.
_DELIVERY_CANCEL_ACK_TIMEOUT_SECONDS = 1.0
_NON_DELIVERED_SCOPED_ERRORS = frozenset(
    {
        "manifest_delta_queue_full",
        "manifest_registration_rejected",
    }
)


class IrohTransportError(RuntimeError):
    """Fail-closed adapter error with a stable code."""

    def __init__(self, code: str, detail: str = ""):
        self.code = code
        self.detail = detail
        super().__init__(code if not detail else f"{code}:{detail}")


def _bounded_trace_identity(
    message: object,
    *,
    max_bytes: int = _TRACE_ENTRY_BYTES,
    delivery_message_id: bytes | None = None,
) -> str:
    if type(max_bytes) is not int or max_bytes < 2:
        raise IrohTransportError("trace_identity_budget_invalid")

    def render(candidate: Mapping[str, Any]) -> str:
        return json.dumps(candidate, sort_keys=True, separators=(",", ":"))

    def fits(candidate: Mapping[str, Any]) -> bool:
        return len(render(candidate).encode("utf-8")) <= max_bytes

    header = getattr(message, "header", None)
    request_id = getattr(message, "request_id", None)
    if request_id is None and header is not None:
        request_id = getattr(header, "request_id", None)
    identity: dict[str, Any] = {}
    encoded_request = request_id.encode("utf-8") if isinstance(request_id, str) else b""
    if encoded_request:
        public_request = {"request_id": request_id}
        if len(encoded_request) <= _TRACE_ID_BYTES and fits(public_request):
            identity.update(public_request)
        else:
            request_digest = {
                "request_id_sha256": hashlib.sha256(encoded_request).hexdigest()
            }
            if not fits(request_digest):
                raise IrohTransportError("trace_identity_budget_exhausted")
            identity.update(request_digest)
    source = header if header is not None else message
    phase = getattr(source, "phase", None)
    if isinstance(phase, str) and len(phase.encode("utf-8")) <= _TRACE_ID_BYTES:
        candidate = {**identity, "phase": phase}
        if fits(candidate):
            identity = candidate
    token_index = getattr(source, "token_index", None)
    if type(token_index) is int and -(2**63) <= token_index < 2**63:
        candidate = {**identity, "token_index": token_index}
        if fits(candidate):
            identity = candidate
    if isinstance(message, FailureReport):
        for field_name in (
            "scope",
            "reason",
            "path_id",
            "placement_id",
            "edge_id",
            "node_id",
        ):
            value = getattr(message, field_name)
            if not value:
                continue
            encoded_value = value.encode("utf-8")
            if len(encoded_value) <= _TRACE_ID_BYTES:
                candidate = {**identity, field_name: value}
            else:
                candidate = {
                    **identity,
                    f"{field_name}_sha256": hashlib.sha256(encoded_value).hexdigest(),
                }
            if fits(candidate):
                identity = candidate
        if 0 <= message.path_attempt < 2**63:
            candidate = {**identity, "path_attempt": message.path_attempt}
            if fits(candidate):
                identity = candidate
    request = getattr(message, "request", None)
    graph = getattr(message, "graph", None)
    build = getattr(message, "build", None)
    if request is None and build is not None:
        request = getattr(build, "request", None)
    if graph is None and build is not None:
        graph = getattr(build, "graph", None)
    if request is not None and is_dataclass(request):
        request_input = asdict(request)
        request_input.pop("admitted_at", None)
        encoded_request_input = json.dumps(
            request_input,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        candidate = {
            **identity,
            "request_input_sha256": (
                "sha256:" + hashlib.sha256(encoded_request_input).hexdigest()
            ),
        }
        if fits(candidate):
            identity = candidate
    deployment_id = getattr(graph, "deployment_id", None)
    if (
        isinstance(deployment_id, str)
        and len(deployment_id.encode("utf-8")) <= _TRACE_ID_BYTES
    ):
        candidate = {**identity, "deployment_id": deployment_id}
        if fits(candidate):
            identity = candidate
    deployment_epoch = getattr(graph, "deployment_epoch", None)
    if type(deployment_epoch) is int and 0 < deployment_epoch < 2**63:
        candidate = {**identity, "deployment_epoch": deployment_epoch}
        if fits(candidate):
            identity = candidate
    stages = getattr(graph, "stages", None)
    if stages:
        cuts = [
            {
                "end_layer_exclusive": stage.layer_range.end_layer_exclusive,
                "node_ids": [placement.node_id for placement in stage.placements],
                "placement_ids": [
                    placement.placement_id for placement in stage.placements
                ],
                "stage_id": stage.stage_id,
                "start_layer": stage.layer_range.start_layer,
            }
            for stage in stages
        ]
        encoded_cuts = json.dumps(
            cuts,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        candidate = {
            **identity,
            "planner_stage_cuts_sha256": (
                "sha256:" + hashlib.sha256(encoded_cuts).hexdigest()
            ),
        }
        if fits(candidate):
            identity = candidate
    if isinstance(delivery_message_id, bytes) and len(delivery_message_id) == 16:
        candidate = {
            **identity,
            "delivery_message_id": delivery_message_id.hex(),
        }
        if fits(candidate):
            identity = candidate
    rendered = render(identity)
    if len(rendered.encode("utf-8")) > max_bytes:
        raise IrohTransportError("trace_identity_too_large")
    return rendered


def _delivery_receipt_document(receipt: "DeliveryReceipt") -> dict[str, Any]:
    return {
        "message_id": receipt.message_id.hex(),
        "peer_endpoint_id": receipt.peer_endpoint_id,
        "peer_generation": receipt.peer_generation,
        "router_protocol": receipt.router_protocol,
        "semantics": receipt.semantics,
    }


def _delivery_receipt_digest(receipt: "DeliveryReceipt") -> str:
    encoded = json.dumps(
        _delivery_receipt_document(receipt),
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _bounded_delivery_receipt_identity(
    receipt: "DeliveryReceipt",
    *,
    max_bytes: int,
) -> str:
    identity = {
        **_delivery_receipt_document(receipt),
        "delivery_receipt_sha256": _delivery_receipt_digest(receipt),
    }
    rendered = json.dumps(identity, sort_keys=True, separators=(",", ":"))
    if len(rendered.encode()) > max_bytes:
        raise IrohTransportError("delivery_receipt_trace_too_large")
    return rendered


@dataclass(frozen=True)
class PeerBinding:
    node_id: str
    endpoint_id: str
    endpoint_addr: Mapping[str, Any]
    generation: int

    def __post_init__(self) -> None:
        if type(self.node_id) is not str or not self.node_id:
            raise ValueError("peer node and endpoint ids must not be empty")
        if type(self.endpoint_id) is not str or not self.endpoint_id:
            raise ValueError("peer node and endpoint ids must not be empty")
        if self.endpoint_addr.get("id") != self.endpoint_id:
            raise ValueError("endpoint_addr id must match endpoint_id")
        if (
            type(self.generation) is not int
            or isinstance(self.generation, bool)
            or self.generation <= 0
        ):
            raise ValueError("peer generation must be positive")


def _canonical_endpoint_document(
    endpoint_addr: Mapping[str, Any],
) -> dict[str, Any]:
    try:
        pairs = list(endpoint_addr.items())
    except Exception:
        raise ValueError("endpoint_addr must be valid JSON data") from None
    for key in (pair[0] for pair in pairs):
        if type(key) is not str:
            raise ValueError("endpoint_addr must be valid JSON data") from None
    try:
        encoded = json.dumps(
            dict(pairs),
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except Exception:
        raise ValueError("endpoint_addr must be valid JSON data") from None
    try:
        document = json.loads(encoded)
    except Exception:
        raise ValueError("endpoint_addr must be valid JSON data") from None
    if not isinstance(document, dict):
        raise ValueError("endpoint_addr must be valid JSON data") from None
    if len(document) != len(pairs):
        raise ValueError("endpoint_addr must be valid JSON data") from None
    return document


def _canonical_peer_binding(binding: PeerBinding) -> PeerBinding:
    return PeerBinding(
        node_id=str(binding.node_id),
        endpoint_id=str(binding.endpoint_id),
        endpoint_addr=_canonical_endpoint_document(binding.endpoint_addr),
        generation=int(binding.generation),
    )


def _peer_binding_snapshot(
    binding: PeerBinding,
) -> tuple[str, str, str, int]:
    endpoint_document = _canonical_endpoint_document(binding.endpoint_addr)
    return (
        str(binding.node_id),
        str(binding.endpoint_id),
        json.dumps(
            endpoint_document,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ),
        int(binding.generation),
    )


class PeerSet:
    """Thread-safe multi-peer binding manager for routed topologies.

    All mutations are atomic and generation-fenced: a stale-generation upsert
    or replacement is rejected before any internal state changes.
    """

    def __init__(self) -> None:
        self._bindings: dict[str, PeerBinding] = {}
        self._lock = threading.RLock()

    @property
    def count(self) -> int:
        with self._lock:
            return len(self._bindings)

    def upsert(self, binding: PeerBinding) -> None:
        canonical = _canonical_peer_binding(binding)
        with self._lock:
            existing = self._bindings.get(canonical.node_id)
            if existing is not None and canonical.generation <= existing.generation:
                raise ValueError("stale_peer_generation")
            self._bindings[canonical.node_id] = canonical

    def lookup(self, node_id: str) -> PeerBinding:
        with self._lock:
            binding = self._bindings.get(node_id)
        if binding is None:
            raise KeyError(node_id)
        return binding

    def lookup_endpoint(self, endpoint_id: str) -> PeerBinding:
        with self._lock:
            matches = [
                binding
                for binding in self._bindings.values()
                if binding.endpoint_id == endpoint_id
            ]
        if len(matches) != 1:
            raise KeyError(endpoint_id)
        return matches[0]

    def atomic_replace(self, bindings: list[PeerBinding]) -> None:
        canonical = [_canonical_peer_binding(b) for b in bindings]
        node_ids = [b.node_id for b in canonical]
        if len(set(node_ids)) != len(node_ids):
            raise ValueError("duplicate_node_id")
        with self._lock:
            for binding in canonical:
                existing = self._bindings.get(binding.node_id)
                if existing is None:
                    continue
                if binding.generation < existing.generation:
                    raise ValueError("stale_peer_generation")
                if binding.generation == existing.generation and _peer_binding_snapshot(
                    binding
                ) != _peer_binding_snapshot(existing):
                    raise ValueError("stale_peer_generation")
            self._bindings = {b.node_id: b for b in canonical}

    def snapshot(self) -> dict[str, PeerBinding]:
        with self._lock:
            return dict(self._bindings)


@dataclass(frozen=True)
class DeliveryReceipt:
    message_id: bytes
    peer_endpoint_id: str
    peer_generation: int
    semantics: str = DELIVERY_SEMANTICS
    router_protocol: str = ROUTER_WIRE_PROTOCOL


@dataclass(frozen=True)
class TransportEvidence:
    local_node_id: str
    local_endpoint_id: str
    peer_node_id: str
    peer_endpoint_id: str
    peer_generation: int
    remote_frames_sent: int
    remote_frames_received: int
    router_frames_dispatched: int
    duplicate_frames: int
    scoped_events: tuple[Mapping[str, Any], ...] = ()
    transport_path_observations: tuple[Mapping[str, Any], ...] = ()
    route_ready: bool = False
    delivery_semantics: str = DELIVERY_SEMANTICS
    process_lifetime_limitation: str = PROCESS_LIFETIME_LIMITATION


@dataclass
class _PendingSend:
    generation: int
    request_id: str | None = None
    path_id: str | None = None
    path_attempt: int | None = None
    cancellable_forward: bool = False
    admission_started: bool = False
    admission_finished: bool = False
    cancelled: bool = False
    reason: str = ""
    cancel_started: bool = False
    cancel_confirmed: bool = False
    completed: bool = False


@dataclass
class _AckRequest:
    message_id: bytes
    completed: threading.Event
    error: BaseException | None = None


@dataclass(frozen=True)
class _InboundFrame:
    digest: bytes
    request_id: str | None
    path_id: str | None
    path_attempt: int | None


@dataclass(frozen=True)
class _ScopedTransportEvent:
    sequence: int
    event: str
    request_id: str
    path_id: str
    path_attempt: int
    peer_node_id: str
    peer_generation: int
    code: str | None = None


ClientFactory = Callable[..., Any]


class IrohTransport:
    """Authenticated iroh adapter implementing ``TransportPort``.

    Every destination and inbound source must match the configured Mycelium
    node/EndpointID/generation peer set. ``peer`` remains the primary successor
    binding for compatibility and rotation; ``peers`` supplies the additional
    participants required by an N-node routed topology.
    """

    route_ready = False
    delivery_semantics = DELIVERY_SEMANTICS
    process_lifetime_limitation = PROCESS_LIFETIME_LIMITATION

    def __init__(
        self,
        *,
        node_id: str,
        socket_path: str | Path,
        bootstrap_secret: bytes,
        peer: PeerBinding,
        peers: Sequence[PeerBinding] | None = None,
        local_generation: int | None = None,
        expected_endpoint_id: str,
        queue_capacity: int = 128,
        delivery_timeout_seconds: float = 5.0,
        poll_interval_seconds: float = 0.05,
        client_factory: ClientFactory = SidecarClient,
    ):
        if not node_id or not expected_endpoint_id:
            raise ValueError("node and expected endpoint ids must not be empty")
        if not isinstance(bootstrap_secret, bytes) or len(bootstrap_secret) != 32:
            raise ValueError("bootstrap_secret must be exactly 32 bytes")
        if queue_capacity <= 0:
            raise ValueError("queue_capacity must be positive")
        if delivery_timeout_seconds <= 0 or poll_interval_seconds <= 0:
            raise ValueError("timeouts must be positive")
        selected_local_generation = (
            peer.generation if local_generation is None else local_generation
        )
        if (
            type(selected_local_generation) is not int
            or not 0 < selected_local_generation <= (1 << 64) - 1
        ):
            raise ValueError("local generation must be a positive u64")

        self.node_id = node_id
        self.socket_path = Path(socket_path)
        self._bootstrap_secret = bytes(bootstrap_secret)
        self._peer = _canonical_peer_binding(peer)
        self._peers = PeerSet()
        try:
            self._peers.atomic_replace([self._peer, *(peers or ())])
        except ValueError as error:
            raise ValueError(str(error)) from error
        self.local_generation = selected_local_generation
        self.expected_endpoint_id = expected_endpoint_id
        self.delivery_timeout_seconds = delivery_timeout_seconds
        self.poll_interval_seconds = poll_interval_seconds
        self._client_factory = client_factory
        self._send_slots = threading.BoundedSemaphore(queue_capacity)
        cancellation_client_count = min(
            queue_capacity,
            _MAX_CONCURRENT_CANCELLATION_CLIENTS,
        )
        self._cancellation_slots = threading.BoundedSemaphore(cancellation_client_count)
        self._manifest_delta_capacity = queue_capacity
        self._state_lock = threading.RLock()
        self._receipt_trace_condition = threading.Condition(self._state_lock)
        self._lifecycle_lock = threading.Lock()
        self._rotation_lock = threading.Lock()
        # SidecarClient serializes one authenticated session internally. Mirror
        # that ownership here so an ordinary send waiting for the data session
        # is not published as remotely pending before it can reach the sidecar.
        # The explicit boundary also gives cancellation one final exact-subject
        # fence immediately before pending registration.
        self._send_operation_lock = threading.Lock()
        self._router: Any | None = None
        self._send_client: Any | None = None
        self._receive_client: Any | None = None
        self._control_client: Any | None = None
        self._forward_client: Any | None = None
        self._cancellation_clients: tuple[Any, ...] = ()
        self._available_cancellation_clients: Queue[Any] = Queue(
            maxsize=cancellation_client_count
        )
        self._receiver_thread: threading.Thread | None = None
        self._dispatcher_thread: threading.Thread | None = None
        self._forward_thread: threading.Thread | None = None
        self._dispatch_queue: Queue[
            tuple[bytes, str, int, bytes, bytes, DecodedFrame | None]
        ] = Queue(maxsize=queue_capacity)
        self._forward_queue: Queue[
            tuple[str, bytes, str | None, str | None, int | None]
        ] = Queue(maxsize=queue_capacity)
        self._ack_queue: Queue[_AckRequest] = Queue(maxsize=queue_capacity)
        self._cancellation_threads: dict[str, threading.Thread] = {}
        self._delivery_cancel_threads: dict[bytes, threading.Thread] = {}
        self._last_cancellation: PathCancellation | None = None
        self._cancellation_history_capacity = queue_capacity
        self._cancellations_by_subject: OrderedDict[
            tuple[str, str, int], PathCancellation
        ] = OrderedDict()
        # Physical owner fanout is terminal for the whole request, not merely
        # for the path attempt present when the control command was issued.
        # Keep this distinct from ordinary attempt-scoped PathCancellation so
        # a stale data-plane cancellation cannot poison a legitimate retry.
        self._controlled_cancelled_requests: OrderedDict[str, None] = OrderedDict()
        self._stop = threading.Event()
        self._running = False
        self._closed = False
        self._fatal_error: IrohTransportError | None = None
        self._pending: dict[bytes, _PendingSend] = {}
        self._seen: OrderedDict[bytes, bytes] = OrderedDict()
        self._inflight_received: dict[bytes, _InboundFrame] = {}
        self._forward_scopes: dict[tuple[str, str, int], int] = {}
        self._active_forward_scope: tuple[str, str, int] | None = None
        self._cancelled_forward_scopes: OrderedDict[tuple[str, str, int], None] = (
            OrderedDict()
        )
        self._dispatcher_phase = "idle"
        self._last_dispatch_error: dict[str, str] | None = None
        self._outbound_trace: deque[str] = deque(maxlen=256)
        self._inflight_receipt_trace_commits = 0
        self._remote_frames_sent = 0
        self._remote_frames_received = 0
        self._router_frames_dispatched = 0
        self._duplicate_frames = 0
        self._scoped_event_sequence = 0
        self._scoped_events: deque[_ScopedTransportEvent] = deque(maxlen=256)
        self._path_graphs: dict[str, Any] = {}
        self._participant_nodes_by_path: dict[str, frozenset[str]] = {}
        self._entry_nodes: dict[str, str] = {}
        self.manifest_deltas: list[ManifestDelta] = []

    @property
    def peer_binding(self) -> PeerBinding:
        with self._state_lock:
            return _canonical_peer_binding(self._peer)

    @property
    def fatal_error(self) -> IrohTransportError | None:
        with self._state_lock:
            return self._fatal_error

    @property
    def running(self) -> bool:
        with self._state_lock:
            return self._running

    @property
    def worker_threads_alive(self) -> int:
        with self._state_lock:
            threads = (
                self._receiver_thread,
                self._dispatcher_thread,
                self._forward_thread,
                *self._cancellation_threads.values(),
                *self._delivery_cancel_threads.values(),
            )
        return sum(int(thread is not None and thread.is_alive()) for thread in threads)

    @property
    def pending_delivery_count(self) -> int:
        with self._state_lock:
            return len(self._pending)

    def cancellation_cleanup_complete(
        self,
        request_id: str,
        path_id: str,
        path_attempt: int | None = None,
    ) -> bool:
        """Observe cleanup for one request/path without waiting on unrelated traffic."""
        state = self.cancellation_cleanup_state(request_id, path_id, path_attempt)
        return all(
            value in (0, False)
            for key, value in state.items()
            if key != "cancellation_observed"
        )

    def cancellation_cleanup_state(
        self,
        request_id: str,
        path_id: str,
        path_attempt: int | None = None,
    ) -> dict[str, int | bool]:
        """Return privacy-reduced exact-subject blockers for cleanup proof."""

        with self._state_lock:
            return self._cancellation_cleanup_state_locked(
                request_id,
                path_id,
                path_attempt,
            )

    def _cancellation_cleanup_state_locked(
        self,
        request_id: str,
        path_id: str,
        path_attempt: int | None,
    ) -> dict[str, int | bool]:
        """Project exact-subject blockers while ``_state_lock`` is owned."""

        def matches(
            candidate_request: str | None,
            candidate_path: str | None,
            candidate_attempt: int | None,
        ) -> bool:
            return (
                candidate_request == request_id
                and candidate_path == path_id
                and (
                    path_attempt is None
                    or candidate_attempt is None
                    or candidate_attempt == path_attempt
                )
            )

        pending_count = sum(
            1
            for item in self._pending.values()
            if matches(item.request_id, item.path_id, item.path_attempt)
            # A sidecar-confirmed message cancellation is no longer an
            # outstanding delivery even if the original blocking Python
            # send frame has not reached its finally block yet.  Merely
            # scheduling cancellation is not enough: failures retain the
            # item as a cleanup blocker.
            and not item.cancel_confirmed
        )
        inflight_count = sum(
            1
            for item in self._inflight_received.values()
            if matches(item.request_id, item.path_id, item.path_attempt)
        )
        forward_count = sum(
            count
            for scope, count in self._forward_scopes.items()
            if count > 0
            and scope[0] == request_id
            and scope[1] == path_id
            and (path_attempt is None or scope[2] == path_attempt)
        )
        return {
            "pending_delivery_count": pending_count,
            "inflight_received_count": inflight_count,
            "forward_count": forward_count,
            "path_graph_registered": path_id in self._path_graphs,
            "participants_registered": path_id in self._participant_nodes_by_path,
            "entry_registered": request_id in self._entry_nodes,
            "cancellation_worker_active": path_id in self._cancellation_threads,
            "cancellation_observed": (
                False
                if path_attempt is None
                else (request_id, path_id, path_attempt)
                in self._cancellations_by_subject
            ),
        }

    def cancellation_cleanup_observation_nonblocking(
        self,
        request_id: str,
        path_id: str,
        path_attempt: int,
    ) -> tuple[dict[str, int], dict[str, int | bool]] | None:
        """Return one atomic receipt observation, or pending on lock contention.

        A cleanup snapshot is an observer, never teardown authority.  Waiting
        for the ordinary transport state lock here orders the observer behind
        active data-plane and cancellation work and can consume the route's
        immutable cleanup deadline without returning any signed evidence.
        Instead, fail the observation attempt open-to-retry but closed-to-proof:
        ``None`` means the caller must sign ``complete=False``.  A later attempt
        can prove cleanup only after it acquires this lock and sees exact
        request/path absence.
        """

        if not self._state_lock.acquire(blocking=False):
            return None
        try:
            return (
                self._counter_snapshot_locked(),
                self._cancellation_cleanup_state_locked(
                    request_id,
                    path_id,
                    path_attempt,
                ),
            )
        finally:
            self._state_lock.release()

    @staticmethod
    def _frame_scope(
        decoded: DecodedFrame,
    ) -> tuple[str | None, str | None, int | None]:
        message = decoded.message
        header = getattr(message, "header", None)
        request = getattr(message, "request", None)
        request_id = getattr(message, "request_id", None)
        path_id = getattr(message, "path_id", None)
        path_attempt = getattr(message, "path_attempt", None)
        if request_id is None and header is not None:
            request_id = getattr(header, "request_id", None)
        if request_id is None and request is not None:
            request_id = getattr(request, "request_id", None)
        if path_id is None and header is not None:
            path_id = getattr(header, "path_id", None)
        if path_attempt is None and header is not None:
            path_attempt = getattr(header, "path_attempt", None)
        return (
            request_id if isinstance(request_id, str) else None,
            path_id if isinstance(path_id, str) else None,
            path_attempt if type(path_attempt) is int else None,
        )

    @property
    def last_cancellation(self) -> dict[str, object] | None:
        with self._state_lock:
            if self._last_cancellation is None:
                return None
            cancellation = self._last_cancellation
            return {
                "request_id": cancellation.request_id,
                "path_id": cancellation.path_id,
                "path_attempt": cancellation.path_attempt,
            }

    def cancellation_observed(
        self,
        request_id: str,
        path_id: str,
        path_attempt: int,
    ) -> bool:
        """Return exact bounded cancellation evidence, never node-global state."""

        with self._state_lock:
            return (request_id, path_id, path_attempt) in self._cancellations_by_subject

    def _remember_cancellation_locked(self, cancellation: PathCancellation) -> None:
        key = (
            cancellation.request_id,
            cancellation.path_id,
            cancellation.path_attempt,
        )
        self._cancellations_by_subject.pop(key, None)
        self._cancellations_by_subject[key] = cancellation
        while len(self._cancellations_by_subject) > self._cancellation_history_capacity:
            self._cancellations_by_subject.popitem(last=False)

    def _remember_controlled_cancellation_locked(self, request_id: str) -> None:
        self._controlled_cancelled_requests.pop(request_id, None)
        self._controlled_cancelled_requests[request_id] = None
        while (
            len(self._controlled_cancelled_requests)
            > self._cancellation_history_capacity
        ):
            self._controlled_cancelled_requests.popitem(last=False)

    def _request_controlled_cancelled_locked(self, request_id: str | None) -> bool:
        return (
            request_id is not None
            and request_id in self._controlled_cancelled_requests
        )

    @property
    def dispatcher_phase(self) -> str:
        with self._state_lock:
            return self._dispatcher_phase

    @property
    def last_dispatch_error(self) -> Mapping[str, str] | None:
        """Return one bounded internal diagnostic without frame payload material."""

        with self._state_lock:
            return (
                None
                if self._last_dispatch_error is None
                else dict(self._last_dispatch_error)
            )

    @property
    def outbound_trace(self) -> tuple[str, ...]:
        with self._state_lock:
            return tuple(self._outbound_trace)

    def bind_router(self, router: Any) -> None:
        with self._state_lock:
            if self._running:
                raise IrohTransportError("cannot_bind_router_after_start")
            if self._router is not None:
                raise IrohTransportError("router_already_bound")
            self._router = router

    register_router = bind_router

    def _new_client(self, *, timeout: float | None = None) -> Any:
        return self._client_factory(
            self.socket_path,
            self._bootstrap_secret,
            timeout=self.delivery_timeout_seconds if timeout is None else timeout,
        )

    def start(self) -> None:
        with self._lifecycle_lock:
            self._start()

    def _start(self) -> None:
        with self._state_lock:
            if self._closed:
                raise IrohTransportError("transport_closed")
            if self._fatal_error is not None:
                raise self._fatal_error
            if self._running:
                return
            if self._router is None:
                raise IrohTransportError("router_not_bound")

        cancellation_client_count = self._available_cancellation_clients.maxsize
        clients = [self._new_client() for _ in range(4 + cancellation_client_count)]
        try:
            for client in clients:
                client.connect()
                actual = client.endpoint_id
                if actual != self.expected_endpoint_id:
                    raise IrohTransportError(
                        "local_endpoint_mismatch",
                        f"expected={self.expected_endpoint_id},actual={actual}",
                    )
            self._configure_current_peers(clients[2])
            self._configure_current_peers(clients[3])
            for client in clients[4:]:
                self._configure_current_peers(client)
        except BaseException:
            for client in clients:
                try:
                    client.close()
                except BaseException:
                    pass
            raise

        with self._state_lock:
            self._send_client, self._receive_client, self._control_client = clients[:3]
            self._forward_client = clients[3]
            self._cancellation_clients = tuple(clients[4:])
            for client in self._cancellation_clients:
                self._available_cancellation_clients.put_nowait(client)
            self._stop.clear()
            self._running = True
            self._dispatcher_thread = threading.Thread(
                target=self._dispatch_loop,
                name=f"mycelium-iroh-dispatch-{self.node_id}",
                daemon=True,
            )
            self._receiver_thread = threading.Thread(
                target=self._receive_loop,
                name=f"mycelium-iroh-{self.node_id}",
                daemon=True,
            )
            self._dispatcher_thread.start()
            self._receiver_thread.start()

    @staticmethod
    def _configure_peer(
        client: Any,
        binding: PeerBinding,
        *,
        timeout: float | None = None,
    ) -> None:
        client.configure_peer(
            binding.endpoint_id,
            _canonical_endpoint_document(binding.endpoint_addr),
            generation=binding.generation,
            timeout=timeout,
        )

    @staticmethod
    def _configure_peers(
        client: Any,
        bindings: Sequence[PeerBinding],
        *,
        timeout: float | None = None,
    ) -> None:
        client.configure_peers(
            [
                {
                    "endpoint_id": binding.endpoint_id,
                    "endpoint_addr": _canonical_endpoint_document(
                        binding.endpoint_addr
                    ),
                    "generation": binding.generation,
                }
                for binding in bindings
            ],
            timeout=timeout,
        )

    def _ordered_peer_bindings(self) -> list[PeerBinding]:
        snapshot = self._peers.snapshot()
        ordered = [self._peer]
        ordered.extend(
            binding
            for node_id, binding in snapshot.items()
            if node_id != self._peer.node_id
        )
        return ordered

    def _configure_current_peers(
        self,
        client: Any,
        *,
        timeout: float | None = None,
    ) -> None:
        bindings = self._ordered_peer_bindings()
        if len(bindings) == 1:
            if timeout is None:
                self._configure_peer(client, bindings[0])
            else:
                self._configure_peer(client, bindings[0], timeout=timeout)
        else:
            if timeout is None:
                self._configure_peers(client, bindings)
            else:
                self._configure_peers(client, bindings, timeout=timeout)

    def _lookup_destination_peer(self, destination_node_id: str) -> PeerBinding:
        try:
            return self._peers.lookup(destination_node_id)
        except KeyError:
            raise IrohTransportError("destination_binding_mismatch") from None

    def _lookup_source_peer(self, source_endpoint_id: str) -> PeerBinding:
        try:
            return self._peers.lookup_endpoint(source_endpoint_id)
        except KeyError:
            raise IrohTransportError("source_binding_mismatch") from None

    def rotate_peer(self, replacement: PeerBinding) -> None:
        with self._rotation_lock:
            self._rotate_peer(replacement)

    def _rotate_peer(self, replacement: PeerBinding) -> None:
        with self._state_lock:
            self._require_running()
            current_snapshot = _peer_binding_snapshot(self._peer)
            candidate = _canonical_peer_binding(replacement)
            if candidate.node_id != self._peer.node_id:
                raise IrohTransportError("peer_node_mismatch")
            if candidate.generation <= self._peer.generation:
                raise IrohTransportError("stale_peer_generation")
            control = self._control_client
            cancellation_clients = self._cancellation_clients
        if control is None:
            raise IrohTransportError("transport_control_unavailable")
        try:
            current_peers = self._peers.snapshot()
            configured_bindings = [
                candidate if binding.node_id == candidate.node_id else binding
                for binding in current_peers.values()
            ]
            if len(configured_bindings) == 1:
                self._configure_peer(control, candidate)
                with self._state_lock:
                    self._require_running()
                for client in cancellation_clients:
                    self._configure_peer(client, candidate)
                    with self._state_lock:
                        self._require_running()
            else:
                self._configure_peers(control, configured_bindings)
                with self._state_lock:
                    self._require_running()
                for client in cancellation_clients:
                    self._configure_peers(client, configured_bindings)
                    with self._state_lock:
                        self._require_running()
        except BaseException as error:
            raise self._map_sidecar_error("peer_rotation_failed", error) from error

        with self._state_lock:
            self._require_running()
            if self._control_client is not control:
                raise IrohTransportError("transport_control_changed")
            if self._cancellation_clients != cancellation_clients:
                raise IrohTransportError("transport_control_changed")
            try:
                refreshed_snapshot = _peer_binding_snapshot(self._peer)
            except ValueError:
                raise IrohTransportError("peer_rotated") from None
            if refreshed_snapshot != current_snapshot:
                raise IrohTransportError("peer_rotated")
            self._peer = candidate
            current_peers = self._peers.snapshot()
            self._peers.atomic_replace(
                [
                    candidate if binding.node_id == candidate.node_id else binding
                    for binding in current_peers.values()
                ]
            )
            for message_id, pending in self._pending.items():
                if pending.generation >= candidate.generation:
                    continue
                reserved, cancellation_client = (
                    self._reserve_pending_cancellation_locked(
                        message_id,
                        pending,
                        "peer_rotated",
                    )
                )
                if reserved:
                    self._start_delivery_cancel_locked(
                        cancellation_client,
                        message_id,
                        pending,
                    )

    def send_router_frame(
        self,
        frame: bytes,
        *,
        destination_node_id: str,
        _trace_peer_binding: PeerBinding | None = None,
        _send_client_override: Any | None = None,
        _cancellable_forward: bool = False,
    ) -> DeliveryReceipt:
        """Deliver one frame and retain bounded exact-subject receipt/failure evidence."""

        try:
            decoded = decode_frame(frame)
        except WireError:
            # An unauthenticated/malformed frame has no trustworthy request scope.
            return self._send_router_frame(
                frame,
                destination_node_id=destination_node_id,
                _trace_peer_binding=_trace_peer_binding,
                _send_client_override=_send_client_override,
                _cancellable_forward=_cancellable_forward,
            )
        request_id, path_id, path_attempt = self._frame_scope(decoded)
        try:
            receipt = self._send_router_frame(
                frame,
                destination_node_id=destination_node_id,
                _trace_peer_binding=_trace_peer_binding,
                _send_client_override=_send_client_override,
                _cancellable_forward=_cancellable_forward,
            )
        except IrohTransportError as error:
            self._record_scoped_event(
                event="failure",
                request_id=request_id,
                path_id=path_id,
                path_attempt=path_attempt,
                peer_node_id=destination_node_id,
                code=error.code,
            )
            raise
        self._record_scoped_event(
            event="receipt",
            request_id=request_id,
            path_id=path_id,
            path_attempt=path_attempt,
            peer_node_id=destination_node_id,
        )
        return receipt

    def _send_router_frame(
        self,
        frame: bytes,
        *,
        destination_node_id: str,
        _trace_peer_binding: PeerBinding | None = None,
        _send_client_override: Any | None = None,
        _cancellable_forward: bool = False,
    ) -> DeliveryReceipt:
        with self._state_lock:
            self._require_running()
            peer = self._lookup_destination_peer(destination_node_id)
            if _trace_peer_binding is not None and peer != _trace_peer_binding:
                raise IrohTransportError("peer_rotated")
        try:
            decoded = decode_frame(frame)
        except WireError as error:
            raise IrohTransportError(
                "malformed_router_frame",
                error.code,
            ) from error
        if not self._send_slots.acquire(blocking=False):
            raise IrohTransportError("adapter_queue_full")

        message_id = uuid.uuid4().bytes
        deadline = time.monotonic() + self.delivery_timeout_seconds
        request_id, path_id, path_attempt = self._frame_scope(decoded)
        pending = _PendingSend(
            peer.generation,
            request_id,
            path_id,
            path_attempt,
            cancellable_forward=_cancellable_forward,
        )
        cancel_timer: threading.Timer | None = None
        send_operation_acquired = False
        try:
            with self._state_lock:
                self._require_running()
                peer = self._lookup_destination_peer(destination_node_id)
                if _trace_peer_binding is not None and peer != _trace_peer_binding:
                    raise IrohTransportError("peer_rotated")
                scope = (request_id, path_id, path_attempt)
                if (
                    not isinstance(decoded.message, PathCancellation)
                    and (
                        self._request_controlled_cancelled_locked(request_id)
                        or (
                            None not in scope
                            and scope in self._cancellations_by_subject
                        )
                    )
                ):
                    # Cancellation and ordinary sends can cross between frame
                    # construction and pending-delivery registration.  Fence the
                    # second boundary under the same state lock used by controlled
                    # cancellation so a late token/response cannot create a fresh
                    # exact-subject delivery after the cancellation sweep.
                    raise IrohTransportError("path_cancelled")
                pending = _PendingSend(
                    peer.generation,
                    request_id,
                    path_id,
                    path_attempt,
                    cancellable_forward=_cancellable_forward,
                )
                client = (
                    self._send_client
                    if _send_client_override is None
                    else _send_client_override
                )
                if client is None:
                    raise IrohTransportError("transport_not_running")
                self._pending[message_id] = pending
            cancel_timer = threading.Timer(
                self.delivery_timeout_seconds,
                self._expire_pending,
                args=(message_id, pending, "delivery_deadline_exceeded"),
            )
            cancel_timer.daemon = True
            cancel_timer.start()
            if _send_client_override is None:
                if not self._send_operation_lock.acquire(
                    timeout=self._remaining(deadline)
                ):
                    raise TimeoutError("delivery deadline exceeded")
                send_operation_acquired = True
            with self._state_lock:
                self._require_running()
                current = self._lookup_destination_peer(destination_node_id)
                scope = (request_id, path_id, path_attempt)
                if (
                    pending.cancelled
                    or current.generation != pending.generation
                    or (
                        not isinstance(decoded.message, PathCancellation)
                        and (
                            self._request_controlled_cancelled_locked(request_id)
                            or (
                                None not in scope
                                and scope in self._cancellations_by_subject
                            )
                        )
                    )
                ):
                    raise IrohTransportError(pending.reason or "peer_rotated")
                client = (
                    self._send_client
                    if _send_client_override is None
                    else _send_client_override
                )
                if client is None:
                    raise IrohTransportError("transport_not_running")
                # Cancellation takes the same state lock. If it wins before this
                # flag, the waiter is locally fenced and never reaches the
                # sidecar. If it wins after, exact-message sidecar cancellation is
                # required and retried across the admission crossing.
                pending.admission_started = True
            self._send_confirmed(
                client,
                frame,
                message_id,
                expected_generation=pending.generation,
                timeout=self._remaining(deadline),
                destination_endpoint_id=(
                    peer.endpoint_id if peer.node_id != self._peer.node_id else None
                ),
            )
            with self._state_lock:
                current = self._lookup_destination_peer(destination_node_id)
                if pending.cancelled or current.generation != pending.generation:
                    raise IrohTransportError(pending.reason or "peer_rotated")
                pending.completed = True
                self._remote_frames_sent += 1
            return DeliveryReceipt(
                message_id=message_id,
                peer_endpoint_id=peer.endpoint_id,
                peer_generation=peer.generation,
            )
        except TimeoutError as error:
            self._expire_pending(
                message_id,
                pending,
                "delivery_deadline_exceeded",
            )
            raise IrohTransportError("delivery_deadline_exceeded") from error
        except IrohTransportError:
            raise
        except QueueFull as error:
            raise IrohTransportError("sidecar_queue_full") from error
        except BaseException as error:
            if pending.cancelled:
                raise IrohTransportError(
                    pending.reason or "delivery_cancelled"
                ) from error
            if _send_client_override is None and self._reconnectable(error):
                try:
                    self._reconnect_send_client(deadline=deadline)
                    with self._state_lock:
                        current = self._lookup_destination_peer(destination_node_id)
                        if (
                            pending.cancelled
                            or current.generation != pending.generation
                        ):
                            raise IrohTransportError(pending.reason or "peer_rotated")
                        client = self._send_client
                    if client is None:
                        raise IrohTransportError("transport_not_running")
                    self._send_confirmed(
                        client,
                        frame,
                        message_id,
                        expected_generation=pending.generation,
                        timeout=self._remaining(deadline),
                        destination_endpoint_id=(
                            peer.endpoint_id
                            if peer.node_id != self._peer.node_id
                            else None
                        ),
                    )
                    with self._state_lock:
                        current = self._lookup_destination_peer(destination_node_id)
                        if (
                            pending.cancelled
                            or current.generation != pending.generation
                        ):
                            raise IrohTransportError(pending.reason or "peer_rotated")
                        pending.completed = True
                        self._remote_frames_sent += 1
                    return DeliveryReceipt(
                        message_id=message_id,
                        peer_endpoint_id=peer.endpoint_id,
                        peer_generation=peer.generation,
                    )
                except IrohTransportError:
                    raise
                except TimeoutError as retry_error:
                    self._expire_pending(
                        message_id,
                        pending,
                        "delivery_deadline_exceeded",
                    )
                    raise IrohTransportError(
                        "delivery_deadline_exceeded"
                    ) from retry_error
                except BaseException as retry_error:
                    raise self._map_sidecar_error(
                        "delivery_not_confirmed",
                        retry_error,
                    ) from retry_error
            raise self._map_sidecar_error("delivery_not_confirmed", error) from error
        finally:
            if cancel_timer is not None:
                cancel_timer.cancel()
            with self._state_lock:
                pending.admission_finished = True
                self._pending.pop(message_id, None)
            if send_operation_acquired:
                self._send_operation_lock.release()
            self._send_slots.release()

    def _record_scoped_event(
        self,
        *,
        event: str,
        request_id: str | None,
        path_id: str | None,
        path_attempt: int | None,
        peer_node_id: str,
        code: str | None = None,
    ) -> bool:
        if (
            event not in {"receipt", "failure"}
            or not request_id
            or not path_id
            or type(path_attempt) is not int
        ):
            return False
        with self._state_lock:
            try:
                peer = self._lookup_destination_peer(peer_node_id)
            except IrohTransportError:
                return False
            self._scoped_event_sequence += 1
            self._scoped_events.append(
                _ScopedTransportEvent(
                    sequence=self._scoped_event_sequence,
                    event=event,
                    request_id=request_id,
                    path_id=path_id,
                    path_attempt=path_attempt,
                    peer_node_id=peer_node_id,
                    peer_generation=peer.generation,
                    code=code,
                )
            )
        return True

    def _send_confirmed(
        self,
        client: Any,
        frame: bytes,
        message_id: bytes,
        *,
        expected_generation: int,
        timeout: float,
        destination_endpoint_id: str | None = None,
    ) -> None:
        if destination_endpoint_id is None:
            client.send_confirmed(
                frame,
                message_id,
                timeout=timeout,
                expected_generation=expected_generation,
                source_generation=self.local_generation,
            )
        else:
            client.send_routed(
                destination_endpoint_id,
                frame,
                message_id,
                timeout=timeout,
                expected_generation=expected_generation,
                source_generation=self.local_generation,
            )

    @staticmethod
    def _reconnectable(error: BaseException) -> bool:
        if isinstance(error, (ConnectionError, EOFError, OSError)):
            return True
        return isinstance(error, ProtocolError) and error.code in {
            "not_connected",
            "sidecar_disconnected",
            "truncated_record",
            "truncated_record_length",
        }

    def _reconnect_send_client(self, *, deadline: float) -> None:
        replacement = self._new_client(timeout=self._remaining(deadline))
        try:
            replacement.connect(deadline=deadline)
            if replacement.endpoint_id != self.expected_endpoint_id:
                raise IrohTransportError("local_endpoint_mismatch_after_reconnect")
            self._configure_current_peers(
                replacement,
                timeout=self._remaining(deadline),
            )
        except BaseException:
            try:
                replacement.close()
            except BaseException:
                pass
            raise
        rejected: IrohTransportError | None = None
        with self._state_lock:
            if self._closed:
                rejected = IrohTransportError("transport_closed")
                previous = None
            elif not self._running:
                rejected = self._fatal_error or IrohTransportError(
                    "transport_not_running"
                )
                previous = None
            else:
                previous = self._send_client
                self._send_client = replacement
        if rejected is not None:
            try:
                replacement.close()
            except BaseException:
                pass
            raise rejected
        if previous is not None:
            try:
                previous.close()
            except BaseException:
                pass

    @staticmethod
    def _remaining(deadline: float) -> float:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("delivery deadline exceeded")
        return remaining

    def _expire_pending(
        self,
        message_id: bytes,
        pending: _PendingSend,
        reason: str,
    ) -> None:
        with self._state_lock:
            reserved, control = self._reserve_pending_cancellation_locked(
                message_id,
                pending,
                reason,
            )
            if reserved:
                self._start_delivery_cancel_locked(control, message_id, pending)

    def _reserve_pending_cancellation_locked(
        self,
        message_id: bytes,
        pending: _PendingSend,
        reason: str,
    ) -> tuple[bool, Any | None]:
        if (
            self._pending.get(message_id) is not pending
            or pending.completed
            or pending.cancel_started
        ):
            return False, None
        pending.cancelled = True
        pending.reason = reason
        pending.cancel_started = True
        return True, self._control_client

    def _cancel_forward_scope_locked(
        self,
        cancellation: PathCancellation,
    ) -> Any | None:
        """Fence, remove queued, and interrupt active deferred path forwards."""

        scope = (
            cancellation.request_id,
            cancellation.path_id,
            cancellation.path_attempt,
        )
        self._cancelled_forward_scopes[scope] = None
        self._cancelled_forward_scopes.move_to_end(scope)
        while len(self._cancelled_forward_scopes) > _SEEN_LIMIT:
            self._cancelled_forward_scopes.popitem(last=False)

        removed = 0
        with self._forward_queue.mutex:
            retained = [
                item
                for item in self._forward_queue.queue
                if not (
                    item[2] == scope[0] and item[3] == scope[1] and item[4] == scope[2]
                )
            ]
            removed = len(self._forward_queue.queue) - len(retained)
            if removed:
                self._forward_queue.queue.clear()
                self._forward_queue.queue.extend(retained)
                self._forward_queue.unfinished_tasks -= removed
                if self._forward_queue.unfinished_tasks == 0:
                    self._forward_queue.all_tasks_done.notify_all()
                self._forward_queue.not_full.notify_all()
        if removed:
            remaining = self._forward_scopes.get(scope, 0) - removed
            if remaining > 0:
                self._forward_scopes[scope] = remaining
            else:
                self._forward_scopes.pop(scope, None)

        for message_id, pending in tuple(self._pending.items()):
            if (
                not pending.cancellable_forward
                or pending.request_id != scope[0]
                or pending.path_id != scope[1]
                or pending.path_attempt != scope[2]
            ):
                continue
            reserved, control = self._reserve_pending_cancellation_locked(
                message_id,
                pending,
                "path_cancelled",
            )
            if reserved:
                # Interrupting the isolated forward socket unblocks the Python
                # sender, but it is not sidecar delivery authority.  Cancel the
                # exact message ID as well so request cleanup can rely on a real
                # acknowledgement instead of waiting for the original send's
                # longer delivery timeout.
                self._start_delivery_cancel_locked(control, message_id, pending)

        interrupted_client = None
        if self._active_forward_scope == scope:
            interrupted_client = self._forward_client
            self._forward_client = None
        return interrupted_client

    def _cancel_pending_scope_locked(
        self,
        cancellation: PathCancellation,
        *,
        cleanup_deadline_monotonic_s: float | None = None,
    ) -> None:
        """Cancel every exact-subject delivery still awaiting sidecar receipt."""

        for message_id, pending in tuple(self._pending.items()):
            if (
                pending.request_id != cancellation.request_id
                or pending.path_id != cancellation.path_id
                or pending.path_attempt != cancellation.path_attempt
            ):
                continue
            reserved, control = self._reserve_pending_cancellation_locked(
                message_id,
                pending,
                "path_cancelled",
            )
            if reserved:
                self._start_delivery_cancel_locked(
                    control,
                    message_id,
                    pending,
                    cleanup_deadline_monotonic_s=cleanup_deadline_monotonic_s,
                )

    @staticmethod
    def _interrupt_client(client: Any | None) -> None:
        if client is None:
            return
        interrupt = getattr(client, "interrupt", None)
        if callable(interrupt):
            interrupt()

    def _forward_client_for_send(self) -> Any:
        with self._state_lock:
            self._require_running()
            current = self._forward_client
        if current is not None:
            return current

        with self._rotation_lock:
            with self._state_lock:
                self._require_running()
                current = self._forward_client
            if current is not None:
                return current
            replacement = self._new_client()
            try:
                replacement.connect()
                if replacement.endpoint_id != self.expected_endpoint_id:
                    raise IrohTransportError("local_endpoint_mismatch")
                self._configure_current_peers(replacement)
            except BaseException:
                try:
                    replacement.close()
                except BaseException:
                    pass
                raise
            with self._state_lock:
                try:
                    self._require_running()
                except BaseException:
                    rejected = True
                else:
                    rejected = False
                    self._forward_client = replacement
            if rejected:
                replacement.close()
                raise IrohTransportError("transport_not_running")
            return replacement

    def _start_delivery_cancel_locked(
        self,
        control: Any | None,
        message_id: bytes,
        pending: _PendingSend,
        *,
        cleanup_deadline_monotonic_s: float | None = None,
    ) -> None:
        if message_id in self._delivery_cancel_threads:
            raise IrohTransportError("delivery_cancel_worker_collision")
        thread = threading.Thread(
            target=self._delivery_cancel_worker,
            args=(control, message_id, pending, cleanup_deadline_monotonic_s),
            name=f"mycelium-iroh-cancel-{message_id.hex()}",
            daemon=True,
        )
        # Publish ownership before start: a fast acknowledgement worker can
        # otherwise finish before the mapping is installed and leave a dead
        # worker permanently registered.
        self._delivery_cancel_threads[message_id] = thread
        try:
            thread.start()
        except BaseException:
            self._delivery_cancel_threads.pop(message_id, None)
            raise

    def _delivery_cancel_worker(
        self,
        control: Any | None,
        message_id: bytes,
        pending: _PendingSend,
        cleanup_deadline_monotonic_s: float | None = None,
    ) -> None:
        if cleanup_deadline_monotonic_s is None:
            acknowledgement_timeout = min(
                _DELIVERY_CANCEL_ACK_TIMEOUT_SECONDS,
                self.delivery_timeout_seconds,
            )
            deadline = time.monotonic() + acknowledgement_timeout
        else:
            # Owner-fanned cancellation already carries the one immutable
            # interruption-and-cleanup deadline.  Do not replace it with the
            # legacy one-second sidecar helper window: when a cancel socket
            # has to reconnect, that nested window can expire, reset the
            # pending send to retryable, and leave no owner operation to retry
            # it because all later cleanup snapshots are observers only.
            # Inheriting the aged absolute owner deadline lets this exact
            # message-ID cancellation reconnect/retry while remaining inside
            # the frozen outer bound.
            deadline = cleanup_deadline_monotonic_s
            acknowledgement_timeout = max(0.0, deadline - time.monotonic())
        dedicated = None
        confirmed = False
        with self._state_lock:
            dedicated_pool_available = bool(self._cancellation_clients)
            if not pending.admission_started:
                # The explicit data-client operation lane proves this waiter has
                # not written a sidecar record. Marking it locally cancelled is
                # authoritative because the sender must recheck ``cancelled``
                # under this same lock before setting ``admission_started``.
                pending.cancel_confirmed = True
                current = threading.current_thread()
                if self._delivery_cancel_threads.get(message_id) is current:
                    self._delivery_cancel_threads.pop(message_id, None)
                return
        try:
            if dedicated_pool_available:
                while not confirmed:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    if dedicated is None:
                        try:
                            dedicated = self._available_cancellation_clients.get(
                                timeout=remaining
                            )
                        except Empty:
                            break
                    with self._state_lock:
                        admission_finished_before_cancel = (
                            pending.admission_finished
                        )
                    outcome = self._cancel_with_client(
                        dedicated,
                        message_id,
                        # A valid ``unknown_message`` response can race just ahead
                        # of the data client's admission. Keep time for a bounded
                        # retry instead of consuming the whole cleanup authority in
                        # one attempt. Socket timeouts invalidate SidecarClient and
                        # are handled by moving to another pre-established lane.
                        timeout=min(0.2, remaining),
                    )
                    confirmed = outcome is True
                    if confirmed:
                        break
                    if outcome == "replace":
                        try:
                            dedicated.close()
                        except BaseException:
                            pass
                        if not self._reconnect_cancellation_client(
                            dedicated,
                            deadline=deadline,
                        ):
                            dedicated = None
                        continue
                    # Only a valid sidecar ``unknown_message`` response is the
                    # expected admission crossing. Other failures remain a single
                    # fail-closed attempt rather than being amplified in a loop.
                    if outcome != "retry":
                        break
                    # Once admission has finished, a cancellation attempt that
                    # began afterward and returned ``unknown_message`` is
                    # definitive: this exact message can no longer appear.
                    if admission_finished_before_cancel:
                        break
                    remaining = deadline - time.monotonic()
                    if remaining > 0:
                        self._stop.wait(min(0.01, remaining))
            else:
                # Unit adapters and pre-start teardown do not own the production
                # cancellation pool; retain the explicit control-client fallback.
                confirmed = self._cancel_with_client(
                    control,
                    message_id,
                    timeout=acknowledgement_timeout,
                )
        finally:
            if dedicated is not None:
                self._available_cancellation_clients.put_nowait(dedicated)
            current = threading.current_thread()
            with self._state_lock:
                pending = self._pending.get(message_id)
                if pending is not None and confirmed:
                    pending.cancel_confirmed = True
                if self._delivery_cancel_threads.get(message_id) is current:
                    self._delivery_cancel_threads.pop(message_id, None)
                    if pending is not None and not confirmed:
                        # A failed acknowledgement is still a cleanup blocker, but
                        # it must remain eligible for the delivery timer or a later
                        # exact-subject cancellation sweep to retry.
                        pending.cancel_started = False

    def _reconnect_cancellation_client(
        self,
        client: Any,
        *,
        deadline: float,
    ) -> bool:
        """Restore one invalidated cancellation lane within its owner deadline."""

        try:
            client.connect(deadline=deadline)
            if client.endpoint_id != self.expected_endpoint_id:
                raise IrohTransportError("local_endpoint_mismatch_after_reconnect")
            self._configure_current_peers(
                client,
                timeout=self._remaining(deadline),
            )
            with self._state_lock:
                self._require_running()
                if client not in self._cancellation_clients:
                    raise IrohTransportError("transport_control_changed")
        except BaseException:
            try:
                client.close()
            except BaseException:
                pass
            return False
        return True

    @staticmethod
    def _cancel_with_client(
        control: Any | None,
        message_id: bytes,
        *,
        timeout: float,
    ) -> bool | str:
        if control is None:
            return False
        try:
            control.cancel(message_id, timeout=timeout)
        except ProtocolError as error:
            if error.code == "unknown_message":
                return "retry"
            if getattr(control, "connected", None) is False:
                return "replace"
            return False
        except BaseException:
            if getattr(control, "connected", None) is False:
                return "replace"
            return False
        return True

    def _receive_loop(self) -> None:
        while not self._stop.is_set():
            with self._state_lock:
                client = self._receive_client
            if client is None:
                if self._stop.is_set():
                    return
                self._set_fatal(IrohTransportError("sidecar_receive_client_missing"))
                return
            if not self._drain_ack_requests(client):
                return
            with self._state_lock:
                awaiting_dispatch_ack = bool(self._inflight_received)
            if awaiting_dispatch_ack:
                self._stop.wait(self.poll_interval_seconds)
                continue
            try:
                delivery = self._recv(client)
            except TimeoutError:
                if self._stop.is_set():
                    return
                if getattr(client, "connected", True):
                    continue
                try:
                    self._reconnect_receive_client()
                except BaseException as reconnect_error:
                    if self._stop.is_set():
                        return
                    self._set_fatal(
                        self._map_sidecar_error(
                            "sidecar_receive_reconnect_failed",
                            reconnect_error,
                        )
                    )
                    return
                continue
            except BaseException as error:
                if self._stop.is_set():
                    return
                if self._reconnectable(error):
                    try:
                        self._reconnect_receive_client()
                        continue
                    except BaseException as reconnect_error:
                        if self._stop.is_set():
                            return
                        self._set_fatal(
                            self._map_sidecar_error(
                                "sidecar_receive_reconnect_failed",
                                reconnect_error,
                            )
                        )
                        return
                code = (
                    "sequence_gap"
                    if (
                        isinstance(error, (ProtocolError, AuthenticationError))
                        and error.code in {"sequence_gap", "invalid_sequence"}
                    )
                    else "sidecar_receive_failed"
                )
                self._set_fatal(IrohTransportError(code, str(error)))
                return
            if delivery is None:
                continue
            message_id, source_endpoint_id, delivery_generation, frame = delivery
            digest = hashlib.sha256(frame).digest()
            with self._state_lock:
                source_peer = (
                    self._peer
                    if source_endpoint_id is None
                    else self._lookup_source_peer(source_endpoint_id)
                )
                if delivery_generation != source_peer.generation:
                    self._set_fatal(IrohTransportError("peer_rotated"))
                    return
                previous = self._seen.get(message_id)
                inflight = self._inflight_received.get(message_id)
                known_digest = (
                    previous
                    if previous is not None
                    else (None if inflight is None else inflight.digest)
                )
                if known_digest is not None and known_digest != digest:
                    self._set_fatal(IrohTransportError("replay_collision"))
                    return
                duplicate = known_digest is not None
                inbound_scope = (None, None, None)
            if duplicate:
                decoded = None
            else:
                try:
                    decoded = decode_frame(frame)
                    inbound_scope = self._frame_scope(decoded)
                except WireError as error:
                    with self._state_lock:
                        self._inflight_received.pop(message_id, None)
                    self._set_fatal(
                        IrohTransportError("malformed_router_frame", error.code)
                    )
                    return
            with self._state_lock:
                self._inflight_received[message_id] = _InboundFrame(
                    digest,
                    inbound_scope[0],
                    inbound_scope[1],
                    inbound_scope[2],
                )
            try:
                self._dispatch_queue.put_nowait(
                    (
                        message_id,
                        source_peer.node_id,
                        delivery_generation,
                        frame,
                        digest,
                        decoded,
                    )
                )
            except Full:
                with self._state_lock:
                    self._inflight_received.pop(message_id, None)
                self._set_fatal(IrohTransportError("dispatch_queue_full"))
                return

    def _dispatch_loop(self) -> None:
        while not self._stop.is_set():
            try:
                (
                    message_id,
                    source_node_id,
                    delivery_generation,
                    _frame,
                    digest,
                    decoded,
                ) = self._dispatch_queue.get(timeout=self.poll_interval_seconds)
            except Empty:
                continue
            try:
                current_generation = self._peers.lookup(source_node_id).generation
                if delivery_generation != current_generation:
                    raise IrohTransportError("peer_rotated_during_dispatch")
                if decoded is None:
                    with self._state_lock:
                        self._dispatcher_phase = "acknowledging_duplicate"
                        self._duplicate_frames += 1
                    self._ack_after_dispatch(message_id)
                    with self._state_lock:
                        self._dispatcher_phase = "idle"
                        self._inflight_received.pop(message_id, None)
                    continue
                with self._state_lock:
                    self._dispatcher_phase = (
                        f"dispatching:{type(decoded.message).__name__}"
                    )
                self._dispatch(decoded, source_node_id=source_node_id)
                with self._state_lock:
                    self._dispatcher_phase = "awaiting_local_ack"
                if delivery_generation != self._peers.lookup(source_node_id).generation:
                    raise IrohTransportError("peer_rotated_during_dispatch")
                self._ack_after_dispatch(message_id)
                with self._state_lock:
                    self._dispatcher_phase = "idle"
                    self._inflight_received.pop(message_id, None)
                    self._seen[message_id] = digest
                    self._seen.move_to_end(message_id)
                    while len(self._seen) > _SEEN_LIMIT:
                        self._seen.popitem(last=False)
                    self._remote_frames_received += 1
                    self._router_frames_dispatched += 1
            except BaseException as error:
                with self._state_lock:
                    inbound = self._inflight_received.get(message_id)
                if self._stop.is_set():
                    with self._state_lock:
                        self._dispatcher_phase = "idle"
                        self._inflight_received.pop(message_id, None)
                    return
                error_code = (
                    error.code
                    if isinstance(error, IrohTransportError)
                    else "router_dispatch_failed"
                )
                with self._state_lock:
                    self._last_dispatch_error = {
                        "code": error_code,
                        "exception_type": type(error).__name__[:64],
                        "detail": str(error)[:128],
                    }
                scoped = (
                    inbound is not None
                    and inbound.request_id is not None
                    and inbound.path_id is not None
                    and inbound.path_attempt is not None
                    and error_code
                    not in {
                        "peer_rotated_during_dispatch",
                        "replay_collision",
                        "malformed_router_frame",
                    }
                )
                if scoped:
                    self._record_scoped_event(
                        event="failure",
                        request_id=inbound.request_id,
                        path_id=inbound.path_id,
                        path_attempt=inbound.path_attempt,
                        peer_node_id=source_node_id,
                        code=error_code,
                    )
                    if isinstance(decoded, DecodedFrame) and isinstance(
                        decoded.message,
                        HopHeader,
                    ):
                        failed_hop = decoded.message
                        self._send_or_dispatch(
                            source_node_id,
                            encode_frame(
                                FailureReport(
                                    request_id=failed_hop.request_id,
                                    path_id=failed_hop.path_id,
                                    path_attempt=failed_hop.path_attempt,
                                    token_index=failed_hop.token_index,
                                    scope="PLACEMENT",
                                    reason=error_code,
                                    placement_id=failed_hop.destination_placement_id,
                                    node_id=self.node_id,
                                )
                            ),
                        )
                    if error_code in _NON_DELIVERED_SCOPED_ERRORS:
                        # These local sinks explicitly rejected the frame. Preserve
                        # its request-scoped incident without acknowledging delivery
                        # or poisoning the replay table as though dispatch succeeded.
                        with self._state_lock:
                            self._dispatcher_phase = "idle"
                            self._inflight_received.pop(message_id, None)
                        continue
                    # The authenticated frame was dispatched to its exact local
                    # subject even though that request-local operation failed. ACK it
                    # so the sidecar can retire this delivery; the scoped incident is
                    # the durable failure signal and unrelated traffic remains live.
                    self._ack_after_dispatch(message_id)
                    with self._state_lock:
                        self._dispatcher_phase = "idle"
                        self._inflight_received.pop(message_id, None)
                        self._seen[message_id] = inbound.digest
                        self._seen.move_to_end(message_id)
                        while len(self._seen) > _SEEN_LIMIT:
                            self._seen.popitem(last=False)
                        self._remote_frames_received += 1
                    continue
                with self._state_lock:
                    self._inflight_received.pop(message_id, None)
                self._set_fatal(
                    error
                    if isinstance(error, IrohTransportError)
                    else IrohTransportError("router_dispatch_failed", str(error))
                )
                return
            finally:
                self._dispatch_queue.task_done()

    def _forward_loop(self) -> None:
        current = threading.current_thread()
        while not self._stop.is_set():
            try:
                destination, frame, request_id, path_id, path_attempt = (
                    self._forward_queue.get(timeout=self.poll_interval_seconds)
                )
            except Empty:
                with self._state_lock:
                    if self._forward_queue.empty():
                        if self._forward_thread is current:
                            self._forward_thread = None
                        return
                continue
            scope = None
            forward_client = None
            try:
                with self._state_lock:
                    scope = (
                        None
                        if request_id is None or path_id is None or path_attempt is None
                        else (request_id, path_id, path_attempt)
                    )
                    cancelled = (
                        scope is not None and scope in self._cancelled_forward_scopes
                    )
                if not cancelled:
                    forward_client = self._forward_client_for_send()
                    with self._state_lock:
                        cancelled = (
                            scope is not None
                            and scope in self._cancelled_forward_scopes
                        )
                        if not cancelled:
                            self._active_forward_scope = scope
                    if cancelled:
                        continue
                    self._send_or_dispatch(
                        destination,
                        frame,
                        _send_client_override=forward_client,
                        _cancellable_forward=True,
                    )
            except BaseException as error:
                with self._state_lock:
                    cancelled = (
                        scope is not None and scope in self._cancelled_forward_scopes
                    )
                    if (
                        forward_client is not None
                        and self._forward_client is forward_client
                    ):
                        self._forward_client = None
                    retired_client = forward_client
                if retired_client is not None:
                    try:
                        retired_client.close()
                    except BaseException:
                        pass
                if not cancelled and not self._stop.is_set():
                    mapped = (
                        error
                        if isinstance(error, IrohTransportError)
                        else self._map_sidecar_error("forward_delivery_failed", error)
                    )
                    if not self._record_scoped_event(
                        event="failure",
                        request_id=request_id,
                        path_id=path_id,
                        path_attempt=path_attempt,
                        peer_node_id=destination,
                        code=mapped.code,
                    ):
                        self._set_fatal(mapped)
                        return
            finally:
                with self._state_lock:
                    if self._active_forward_scope == scope:
                        self._active_forward_scope = None
                if (
                    request_id is not None
                    and path_id is not None
                    and path_attempt is not None
                ):
                    with self._state_lock:
                        scope = (request_id, path_id, path_attempt)
                        remaining = self._forward_scopes.get(scope, 0) - 1
                        if remaining > 0:
                            self._forward_scopes[scope] = remaining
                        else:
                            self._forward_scopes.pop(scope, None)
                self._forward_queue.task_done()
        with self._state_lock:
            if self._forward_thread is current:
                self._forward_thread = None

    def _ack_after_dispatch(self, message_id: bytes) -> None:
        request = _AckRequest(message_id=message_id, completed=threading.Event())
        try:
            self._ack_queue.put_nowait(request)
        except Full as error:
            raise IrohTransportError("ack_queue_full") from error
        deadline = time.monotonic() + self.delivery_timeout_seconds
        while not request.completed.wait(timeout=self.poll_interval_seconds):
            if self._stop.is_set():
                raise IrohTransportError("transport_closed_during_ack")
            if time.monotonic() >= deadline:
                raise IrohTransportError("ack_dispatch_timeout")
        if request.error is not None:
            raise self._map_sidecar_error("ack_failed", request.error)

    def _drain_ack_requests(self, client: Any) -> bool:
        while True:
            try:
                request = self._ack_queue.get_nowait()
            except Empty:
                return True
            try:
                client.ack(request.message_id)
            except BaseException as error:
                request.error = error
                request.completed.set()
                self._ack_queue.task_done()
                self._set_fatal(self._map_sidecar_error("ack_failed", error))
                return False
            request.completed.set()
            self._ack_queue.task_done()

    def _recv(self, client: Any) -> tuple[bytes, str | None, int, bytes] | None:
        receive = getattr(client, "recv_with_source", None)
        source_aware = receive is not None
        if receive is None:
            receive = client.recv_with_generation
        try:
            delivery = receive(wait_seconds=self.poll_interval_seconds)
        except TypeError as error:
            if "wait_seconds" not in str(error):
                raise
            delivery = receive(timeout=self.poll_interval_seconds)
        if delivery is None or source_aware:
            return delivery
        message_id, generation, frame = delivery
        return message_id, None, generation, frame

    def _reconnect_receive_client(self) -> None:
        # A disconnected receive session can still occupy one sidecar handler
        # until its local socket close is observed.  Retire it before asking the
        # sidecar to admit a replacement; creating the replacement first makes
        # a transient handler-capacity refusal permanently fatal under load.
        with self._state_lock:
            previous = self._receive_client
        if previous is not None:
            try:
                previous.close()
            except BaseException:
                pass

        # Reconnection owns no additional time budget.  Transient accept or
        # handshake failures may be retried, but only inside the same delivery
        # deadline that previously bounded the single connect attempt.
        deadline = time.monotonic() + self.delivery_timeout_seconds
        replacement: Any | None = None
        while replacement is None:
            if self._stop.is_set():
                raise IrohTransportError("transport_closed")
            candidate = self._new_client(timeout=self._remaining(deadline))
            try:
                candidate.connect(deadline=deadline)
                if candidate.endpoint_id != self.expected_endpoint_id:
                    raise IrohTransportError(
                        "local_endpoint_mismatch_after_reconnect"
                    )
            except IrohTransportError:
                try:
                    candidate.close()
                except BaseException:
                    pass
                raise
            except BaseException:
                try:
                    candidate.close()
                except BaseException:
                    pass
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise
                self._stop.wait(min(self.poll_interval_seconds, remaining))
                continue
            replacement = candidate
        rejected: IrohTransportError | None = None
        with self._state_lock:
            if self._closed:
                rejected = IrohTransportError("transport_closed")
                previous = None
            elif not self._running:
                rejected = self._fatal_error or IrohTransportError(
                    "transport_not_running"
                )
                previous = None
            else:
                # Only this receive worker may replace its client.  A lifecycle
                # close that cleared the slot must win over this late install.
                if self._receive_client is not previous:
                    rejected = IrohTransportError("transport_control_changed")
                else:
                    self._receive_client = replacement
        if rejected is not None:
            try:
                replacement.close()
            except BaseException:
                pass
            raise rejected

    def _dispatch(self, decoded: DecodedFrame, *, source_node_id: str) -> None:
        router = self._router
        if router is None:
            raise IrohTransportError("router_not_bound")
        message = decoded.message
        request_id, _path_id, _path_attempt = self._frame_scope(decoded)
        with self._state_lock:
            if (
                not isinstance(message, PathCancellation)
                and self._request_controlled_cancelled_locked(request_id)
            ):
                # Owner control can overtake a frame already accepted into the
                # receive queue.  ACK and absorb every later request frame so a
                # recovery attempt cannot recreate Router or transport state.
                return
        if isinstance(message, ProgressivePrefillMessage):
            frame = encode_frame(message, decoded.payload)
            header, context = decode_progressive_prefill(frame)
            entry_node = self._node_for_placement(
                context.graph,
                context.build.ordered_hops[0].placement_id,
            )
            with self._state_lock:
                scope = (
                    header.request_id,
                    header.path_id,
                    header.path_attempt,
                )
                if scope in self._cancellations_by_subject:
                    # The authenticated frame was already in the receive queue
                    # when owner cancellation won. Absorb it without
                    # republishing Router/transport ownership; the dispatcher
                    # will ACK it so the remote sender can retire delivery.
                    return
                entry_node_id = self._entry_nodes.setdefault(
                    header.request_id,
                    entry_node,
                )
                if entry_node_id != entry_node:
                    raise IrohTransportError("request_entry_conflict")
                self._path_graphs[header.path_id] = context.graph
                self._participant_nodes_by_path[header.path_id] = frozenset(
                    self._node_for_placement(context.graph, hop.placement_id)
                    for hop in context.build.ordered_hops
                )
            result = router.receive_progressive_prefill(
                header,
                context,
                source_node_id=source_node_id,
                entry_node_id=entry_node_id,
            )
            if getattr(result, "status", None) in {"REJECTED", "FAILED"}:
                self._forget_path(header.request_id, header.path_id)
            return
        if isinstance(message, HopHeader):
            router.receive_hop(
                message,
                decoded.payload,
                source_node_id=source_node_id,
            )
            return
        if isinstance(message, ManifestLocked):
            entry_node = self._node_for_placement(
                message.build.graph,
                message.build.ordered_hops[0].placement_id,
            )
            if entry_node == self.node_id:
                accepted = router.receive_manifest_locked(
                    message,
                    source_node_id=source_node_id,
                )
            else:
                owner_prelocked = source_node_id == entry_node
                accepted = router.register_path(
                    message.build.request,
                    message.manifest,
                    message.build.graph,
                    source_node_id=None if owner_prelocked else source_node_id,
                    entry_node_id=entry_node,
                )
            if not accepted:
                raise IrohTransportError("manifest_registration_rejected")
            participants = frozenset(
                self._node_for_placement(message.build.graph, hop.placement_id)
                for hop in message.manifest.ordered_hops
            )
            with self._state_lock:
                scope = (
                    message.request_id,
                    message.path_id,
                    message.path_attempt,
                )
                if scope in self._cancellations_by_subject:
                    # Router acceptance and transport publication are distinct
                    # boundaries. Owner cancellation can win between them; its
                    # relay tombstone and transport sweep are then final, and
                    # this already-queued manifest must not recreate ownership.
                    return
                self._path_graphs[message.path_id] = message.build.graph
                self._participant_nodes_by_path[message.path_id] = participants
                self._entry_nodes.setdefault(message.request_id, entry_node)
            return
        if isinstance(message, ManifestDelta):
            with self._state_lock:
                if len(self.manifest_deltas) >= self._manifest_delta_capacity:
                    raise IrohTransportError("manifest_delta_queue_full")
                self.manifest_deltas.append(message)
            return
        if isinstance(message, PathCancellation):
            with self._state_lock:
                self._last_cancellation = message
                self._remember_cancellation_locked(message)
            accepted = router.receive_path_cancellation(
                message,
                source_node_id=source_node_id,
            )
            if not accepted:
                raise IrohTransportError("path_cancellation_rejected")
            self._forget_path(message.request_id, message.path_id)
            return
        if isinstance(message, TokenEvent):
            router.receive_token_event(message, source_node_id=source_node_id)
            return
        if isinstance(message, PrefillChunkCompleted):
            router.receive_prefill_chunk_completed(
                message,
                source_node_id=source_node_id,
            )
            return
        if isinstance(message, FailureReport):
            router.receive_failure_report(message, source_node_id=source_node_id)
            return
        raise IrohTransportError(
            "unsupported_router_message",
            type(message).__name__,
        )

    def remember_entry(self, request_id: str, node_id: str) -> None:
        if node_id != self.node_id:
            raise IrohTransportError("request_entry_transport_mismatch")
        with self._state_lock:
            if self._request_controlled_cancelled_locked(request_id) or any(
                cancelled_request_id == request_id
                for cancelled_request_id, _path_id, _path_attempt in self._cancellations_by_subject
            ):
                # infer_start and owner cancellation are independent control
                # commands. Cancellation can install the exact path tombstone
                # while entry startup is between its generation check and this
                # publication boundary; never resurrect request-level entry
                # ownership after that point.
                raise IrohTransportError("path_cancelled")
            existing = self._entry_nodes.setdefault(request_id, node_id)
            if existing != node_id:
                raise IrohTransportError("request_entry_conflict")

    def send_hop(self, header: HopHeader, payload: object) -> None:
        with self._state_lock:
            self._require_running()
        if isinstance(payload, ProgressivePrefillContext):
            graph = payload.graph
            participants = frozenset(
                self._node_for_placement(graph, hop.placement_id)
                for hop in payload.build.ordered_hops
            )
            frame = encode_progressive_prefill(header, payload)
            with self._state_lock:
                self._require_running()
                if (
                    self._request_controlled_cancelled_locked(header.request_id)
                    or (
                        header.request_id,
                        header.path_id,
                        header.path_attempt,
                    ) in self._cancellations_by_subject
                ):
                    raise IrohTransportError("path_cancelled")
                self._entry_nodes.setdefault(header.request_id, self.node_id)
                self._path_graphs[header.path_id] = graph
                self._participant_nodes_by_path[header.path_id] = participants
        else:
            if not isinstance(payload, bytes):
                raise IrohTransportError("hop_payload_must_be_bytes")
            with self._state_lock:
                self._require_running()
                graph = self._path_graphs.get(header.path_id)
            if graph is None:
                raise IrohTransportError("unknown_path", header.path_id)
            frame = encode_frame(header, payload)
        destination = self._node_for_placement(
            graph,
            header.destination_placement_id,
        )
        self._send_or_dispatch(destination, frame)

    def send_manifest_delta(self, delta: ManifestDelta) -> None:
        with self._state_lock:
            self._require_running()
            if self._request_controlled_cancelled_locked(delta.request_id):
                # Recovery may already own reservations when physical owner
                # cancellation wins.  Suppress the late publication and let
                # EntryCoordinator finish the build, observe CANCELLED under
                # its record lock, and release those reservations exactly once.
                return
            if (
                delta.request_id,
                delta.path_id,
                delta.path_attempt,
            ) in self._cancellations_by_subject:
                raise IrohTransportError("path_cancelled")
            entry_node = self._entry_nodes.setdefault(delta.request_id, self.node_id)
        self._send_or_dispatch(
            entry_node,
            encode_frame(delta),
        )

    def send_manifest_locked(self, locked: ManifestLocked) -> None:
        with self._state_lock:
            self._require_running()
        graph = locked.build.graph
        entry_node = self._node_for_placement(
            graph,
            locked.build.ordered_hops[0].placement_id,
        )
        destinations = {
            self._node_for_placement(graph, hop.placement_id)
            for hop in locked.manifest.ordered_hops
        }
        with self._state_lock:
            self._require_running()
            if self._request_controlled_cancelled_locked(locked.request_id) or (
                locked.request_id,
                locked.path_id,
                locked.path_attempt,
            ) in self._cancellations_by_subject:
                raise IrohTransportError("path_cancelled")
            entry = self._entry_nodes.get(locked.request_id)
            if entry is not None:
                destinations.add(entry)
            self._path_graphs[locked.path_id] = graph
            self._participant_nodes_by_path[locked.path_id] = frozenset(destinations)
        frame = encode_frame(locked)
        for destination in sorted(destinations):
            if destination == self.node_id and entry_node != self.node_id:
                continue
            self._send_or_dispatch(destination, frame)

    def send_path_cancellation(self, cancellation: PathCancellation) -> None:
        with self._state_lock:
            self._require_running()
            self._last_cancellation = cancellation
            self._remember_cancellation_locked(cancellation)
            entry_node = self._entry_nodes.get(cancellation.request_id)
            participants = self._participant_nodes_by_path.get(cancellation.path_id)
            configured_nodes = frozenset(self._peers.snapshot())
        if entry_node != self.node_id:
            raise IrohTransportError("path_cancellation_source_not_entry")
        if participants is None:
            raise IrohTransportError("unknown_path", cancellation.path_id)
        remote_participants = tuple(sorted(participants - {self.node_id}))
        if set(remote_participants) - configured_nodes:
            raise IrohTransportError("path_cancellation_participant_unbound")
        frame = encode_frame(cancellation)
        if not remote_participants:
            self._forget_path(cancellation.request_id, cancellation.path_id)
            return
        if not self._cancellation_slots.acquire(blocking=False):
            raise IrohTransportError("path_cancellation_queue_full")
        worker_started = False
        try:
            with self._state_lock:
                self._require_running()
                if cancellation.path_id in self._cancellation_threads:
                    raise IrohTransportError("path_cancellation_already_pending")
                thread = threading.Thread(
                    target=self._deliver_path_cancellation,
                    args=(cancellation, remote_participants, frame),
                    name=f"mycelium-iroh-path-cancel-{cancellation.path_id}",
                    daemon=True,
                )
                self._cancellation_threads[cancellation.path_id] = thread
                try:
                    thread.start()
                except BaseException:
                    self._cancellation_threads.pop(cancellation.path_id, None)
                    raise
                worker_started = True
        except BaseException:
            if not worker_started:
                self._cancellation_slots.release()
            raise

    def send_path_cancellation_if_entry(
        self,
        cancellation: PathCancellation,
    ) -> bool:
        """Issue exact teardown when the entry runtime already retired its record."""

        with self._state_lock:
            is_owned_entry = (
                self._entry_nodes.get(cancellation.request_id) == self.node_id
                and cancellation.path_id in self._participant_nodes_by_path
            )
        if not is_owned_entry:
            return False
        self.send_path_cancellation(cancellation)
        return True

    def apply_controlled_path_cancellation(
        self,
        cancellation: PathCancellation,
        *,
        entry_cancelled: bool,
        cleanup_deadline_monotonic_s: float | None = None,
    ) -> bool:
        """Apply an owner-fanned cancellation without serial data-plane relay.

        The physical route sends the same generation-fenced ``infer_cancel``
        command to every participant.  The node applies this transport fence
        before entry Router teardown so an exact-subject send cannot retain the
        relay's per-path operation lock ahead of cancellation.  ``entry_cancelled``
        remains accepted for legacy callers, and can legitimately be false when
        inference completed just before cleanup; the exact registered entry/path
        transport state still needs local teardown without another data-plane
        cancellation send. Non-entry nodes apply the exact registered entry
        identity directly to their relay. This keeps simultaneous cancellations
        independent of the ordered receive stream.
        """

        if not isinstance(entry_cancelled, bool):
            raise ValueError("entry_cancelled must be boolean")
        interrupted_forward_client = None
        with self._state_lock:
            self._require_running()
            entry_node = self._entry_nodes.get(cancellation.request_id)
            participants = self._participant_nodes_by_path.get(cancellation.path_id)
            router = self._router
            if router is None:
                return False
            if (
                entry_node is not None
                and participants is not None
                and self.node_id not in participants
            ):
                return False
            # A non-entry node cannot truthfully report that it cancelled the
            # entry Router record. On the entry node, false is valid when the
            # record is already terminal or has not yet reached Router.
            if entry_node is not None and entry_node != self.node_id and entry_cancelled:
                return False
            # Publish the exact cancellation fence before sweeping current sends.
            # Any ordinary same-subject send reaching pending registration after
            # this point is rejected by _send_router_frame.  PathCancellation
            # frames remain exempt for the legacy entry-propagation path.
            self._last_cancellation = cancellation
            self._remember_cancellation_locked(cancellation)
            self._remember_controlled_cancellation_locked(cancellation.request_id)
            interrupted_forward_client = self._cancel_forward_scope_locked(cancellation)
            # Deferred forwards use an isolated socket that can be interrupted
            # directly. Ordinary exact-subject sends share the data client, so
            # cancel their individual message IDs over the sidecar control lane
            # instead of interrupting unrelated traffic.
            self._cancel_pending_scope_locked(
                cancellation,
                cleanup_deadline_monotonic_s=cleanup_deadline_monotonic_s,
            )
        self._interrupt_client(interrupted_forward_client)
        # Fanout is authoritative on the entry node too.  Entry.cancel_local()
        # can legitimately find an already-terminal/retired request, while the
        # relay still owns a provisional or registered path.  Installing the
        # relay's exact-attempt tombstone here prevents an inbound manifest or
        # progressive-prefill frame that was already queued at cancellation
        # time from re-registering transport ownership after this sweep.
        try:
            apply_owner_control = getattr(
                router,
                "apply_controlled_path_cancellation",
                None,
            )
            if callable(apply_owner_control):
                accepted = bool(apply_owner_control(cancellation))
            elif entry_node is not None:
                accepted = bool(
                    router.receive_path_cancellation(
                        cancellation,
                        source_node_id=entry_node,
                    )
                )
            else:
                accepted = False
            if not accepted and self.cancellation_observed(
                cancellation.request_id,
                cancellation.path_id,
                cancellation.path_attempt,
            ):
                accepted = True
            return accepted
        finally:
            # The request/path transport fence was published above before any
            # lower-layer release.  Retire its registries even if Router or a
            # host resource release raises after partially completing
            # teardown.  The physical node retains that worker error and
            # retries the same idempotent cancellation inside the original
            # owner deadline, so forgetting transport metadata cannot be
            # mistaken for completed cleanup.  Previously the exception
            # skipped this boundary, the daemon worker erased itself in its
            # own ``finally``, and cleanup snapshots were left with a lone
            # ``entry_registered`` blocker that no lifecycle owner remained
            # to remove.
            self._forget_path(cancellation.request_id, cancellation.path_id)

    def _deliver_path_cancellation(
        self,
        cancellation: PathCancellation,
        peer_nodes: tuple[str, ...],
        frame: bytes,
    ) -> None:
        delivered = False
        cancellation_client: Any | None = None
        try:
            try:
                cancellation_client = self._available_cancellation_clients.get_nowait()
            except Empty as error:
                raise IrohTransportError(
                    "path_cancellation_client_unavailable"
                ) from error
            for peer_node in peer_nodes:
                self._send_or_dispatch(
                    peer_node,
                    frame,
                    _send_client_override=cancellation_client,
                )
            delivered = True
        except BaseException as error:
            if not self._stop.is_set():
                mapped = self._map_sidecar_error(
                    "path_cancellation_delivery_failed", error
                )
                for peer_node in peer_nodes:
                    self._record_scoped_event(
                        event="failure",
                        request_id=cancellation.request_id,
                        path_id=cancellation.path_id,
                        path_attempt=cancellation.path_attempt,
                        peer_node_id=peer_node,
                        code=mapped.code,
                    )
        finally:
            if cancellation_client is not None:
                self._available_cancellation_clients.put_nowait(cancellation_client)
            if delivered:
                self._forget_path(cancellation.request_id, cancellation.path_id)
            with self._state_lock:
                self._cancellation_threads.pop(cancellation.path_id, None)
            self._cancellation_slots.release()

    def send_failure_report(self, report: FailureReport) -> None:
        self._send_or_dispatch(
            self._entry_node(report.request_id),
            encode_frame(report),
        )

    def send_token_event(self, event: TokenEvent) -> None:
        self._send_or_dispatch(
            self._entry_node(event.request_id),
            encode_frame(event),
        )

    def send_prefill_chunk_completed(self, event: PrefillChunkCompleted) -> None:
        self._send_or_dispatch(
            self._entry_node(event.request_id),
            encode_frame(event),
        )

    def _send_or_dispatch(
        self,
        destination: str,
        frame: bytes,
        *,
        _send_client_override: Any | None = None,
        _cancellable_forward: bool = False,
    ) -> None:
        decoded = decode_frame(frame)
        remote = destination != self.node_id
        trace_prefix = (
            f"{type(decoded.message).__name__}->"
            f"{'peer:remote' if remote else 'self:local'}:"
        )
        with self._state_lock:
            self._require_running()
        if destination == self.node_id:
            identity_budget = _TRACE_ENTRY_BYTES - len(trace_prefix.encode("utf-8"))
            trace = trace_prefix + _bounded_trace_identity(
                decoded.message,
                max_bytes=identity_budget,
            )
            if len(trace.encode("utf-8")) > _TRACE_ENTRY_BYTES:
                raise IrohTransportError("trace_entry_too_large")
            with self._state_lock:
                self._require_running()
                self._outbound_trace.append(trace)
            self._dispatch(decoded, source_node_id=self.node_id)
            return

        # An inbound Router dispatch may synchronously produce a downstream hop or
        # token. Waiting for that frame's remote dispatch ACK on this same dispatcher
        # thread creates an acknowledgement cycle when the peer's response needs this
        # dispatcher to finish the original delivery. Progressive path construction
        # retains its existing synchronous lock handshake, while completed manifest
        # publication, hops, and tokens move to one ordered forward worker. The worker
        # preserves confirmed remote dispatch and exact-subject receipt evidence.
        message = decoded.message
        defer_remote_forward = isinstance(
            message,
            (FailureReport, HopHeader, ProgressivePrefillMessage),
        )
        if isinstance(message, ManifestLocked):
            entry_node = self._node_for_placement(
                message.build.graph,
                message.build.ordered_hops[0].placement_id,
            )
            defer_remote_forward = destination != entry_node
        if isinstance(message, TokenEvent):
            with self._forward_queue.all_tasks_done:
                ordered_forward_pending = self._forward_queue.unfinished_tasks > 0
            with self._state_lock:
                response_would_cycle = self._dispatcher_phase in {
                    "dispatching:HopHeader",
                    "dispatching:TokenEvent",
                }
            defer_remote_forward = ordered_forward_pending or response_would_cycle
        if (
            threading.current_thread() is self._dispatcher_thread
            and defer_remote_forward
        ):
            with self._state_lock:
                self._require_running()
                try:
                    request_id, path_id, path_attempt = self._frame_scope(decoded)
                    if (
                        request_id is not None
                        and path_id is not None
                        and path_attempt is not None
                        and (request_id, path_id, path_attempt)
                        in self._cancelled_forward_scopes
                    ):
                        return
                    self._forward_queue.put_nowait(
                        (destination, frame, request_id, path_id, path_attempt)
                    )
                    if (
                        request_id is not None
                        and path_id is not None
                        and path_attempt is not None
                    ):
                        scope = (request_id, path_id, path_attempt)
                        self._forward_scopes[scope] = (
                            self._forward_scopes.get(scope, 0) + 1
                        )
                except Full as error:
                    raise IrohTransportError("forward_queue_full") from error
                thread = self._forward_thread
                if thread is None or not thread.is_alive():
                    thread = threading.Thread(
                        target=self._forward_loop,
                        name=f"mycelium-iroh-forward-{self.node_id}",
                        daemon=True,
                    )
                    self._forward_thread = thread
                    thread.start()
            return

        with self._receipt_trace_condition:
            self._require_running()
            trace_peer = self._lookup_destination_peer(destination)
            self._inflight_receipt_trace_commits += 1
        try:
            placeholder_receipt = DeliveryReceipt(
                message_id=b"\0" * 16,
                peer_endpoint_id=trace_peer.endpoint_id,
                peer_generation=trace_peer.generation,
            )
            self._remote_trace_entries(
                decoded.message,
                trace_prefix,
                placeholder_receipt,
            )
            receipt = self.send_router_frame(
                frame,
                destination_node_id=destination,
                _trace_peer_binding=trace_peer,
                _send_client_override=_send_client_override,
                _cancellable_forward=_cancellable_forward,
            )
            trace, receipt_trace = self._remote_trace_entries(
                decoded.message,
                trace_prefix,
                receipt,
            )
            with self._state_lock:
                self._outbound_trace.extend((trace, receipt_trace))
        finally:
            with self._receipt_trace_condition:
                self._inflight_receipt_trace_commits -= 1
                self._receipt_trace_condition.notify_all()

    @staticmethod
    def _remote_trace_entries(
        message: object,
        trace_prefix: str,
        receipt: DeliveryReceipt,
    ) -> tuple[str, str]:
        identity_budget = _TRACE_ENTRY_BYTES - len(trace_prefix.encode("utf-8"))
        trace = trace_prefix + _bounded_trace_identity(
            message,
            max_bytes=identity_budget,
            delivery_message_id=receipt.message_id,
        )
        receipt_prefix = "DeliveryReceipt->peer:remote:"
        receipt_trace = receipt_prefix + _bounded_delivery_receipt_identity(
            receipt,
            max_bytes=(_TRACE_ENTRY_BYTES - len(receipt_prefix.encode("utf-8"))),
        )
        if (
            len(trace.encode("utf-8")) > _TRACE_ENTRY_BYTES
            or len(receipt_trace.encode("utf-8")) > _TRACE_ENTRY_BYTES
        ):
            raise IrohTransportError("trace_entry_too_large")
        return trace, receipt_trace

    def _entry_node(self, request_id: str) -> str:
        node_id = self._entry_nodes.get(request_id)
        if node_id is None:
            raise IrohTransportError("unknown_entry_node", request_id)
        return node_id

    def _forget_path(self, request_id: str, path_id: str) -> None:
        with self._state_lock:
            self._path_graphs.pop(path_id, None)
            self._participant_nodes_by_path.pop(path_id, None)
            self._entry_nodes.pop(request_id, None)

    @staticmethod
    def _node_for_placement(graph: Any, placement_id: str) -> str:
        for stage in graph.stages:
            for placement in stage.placements:
                if placement.placement_id == placement_id:
                    return placement.node_id
        raise IrohTransportError("unknown_placement", placement_id)

    def inbound_admission_snapshot(
        self, candidate_endpoint_id: str
    ) -> Mapping[str, Any]:
        """Read native admission counters for one candidate remote identity."""

        with self._state_lock:
            control_client = self._control_client
        if control_client is None:
            raise IrohTransportError("transport_not_started")
        query = getattr(control_client, "inbound_admission_snapshot", None)
        if not callable(query):
            raise IrohTransportError("admission_snapshot_unavailable")
        try:
            return query(candidate_endpoint_id)
        except BaseException as error:
            raise self._map_sidecar_error("admission_snapshot_failed", error) from error

    def evidence(self) -> TransportEvidence:
        with self._state_lock:
            peer = self._peer
            peers = self._peers.snapshot()
            control_client = self._control_client
            endpoint_id = (
                self._send_client.endpoint_id
                if self._send_client is not None
                else self.expected_endpoint_id
            )
            counters = (
                self._remote_frames_sent,
                self._remote_frames_received,
                self._router_frames_dispatched,
                self._duplicate_frames,
            )
            scoped_events = tuple(
                {
                    "protocol": "mycelium.iroh_scoped_transport_event.v1",
                    "sequence": item.sequence,
                    "event": item.event,
                    "request_id": item.request_id,
                    "path_id": item.path_id,
                    "path_attempt": item.path_attempt,
                    "peer_node_id": item.peer_node_id,
                    "peer_generation": item.peer_generation,
                    "code": item.code,
                }
                for item in self._scoped_events
            )
        observations: tuple[Mapping[str, Any], ...] = ()
        if control_client is not None:
            query = getattr(control_client, "transport_observations", None)
            if callable(query):
                try:
                    raw = query()
                    by_endpoint = {
                        binding.endpoint_id: binding for binding in peers.values()
                    }
                    projected = []
                    for item in raw:
                        remote_endpoint = item.get("remote_endpoint_id")
                        binding = by_endpoint.get(remote_endpoint)
                        if binding is None:
                            raise IrohTransportError(
                                "transport_observation_peer_mismatch"
                            )
                        measured_at = item.get("measured_at_unix_ms", 0)
                        if type(measured_at) is not int or measured_at < 0:
                            raise IrohTransportError(
                                "transport_observation_time_invalid"
                            )
                        projected.append(
                            {
                                "protocol": "mycelium.transport_path_observation.v1",
                                "local_node_id": self.node_id,
                                "local_endpoint_id": endpoint_id,
                                "remote_node_id": binding.node_id,
                                "remote_endpoint_id": remote_endpoint,
                                "connection_generation": item.get(
                                    "connection_generation"
                                ),
                                "path_class": item.get("path_class"),
                                "relay_identity": item.get("relay_identity"),
                                "relay_region": item.get("relay_region"),
                                "cold_rtt_ms": item.get("cold_rtt_ms"),
                                "warm_rtt_ms": item.get("warm_rtt_ms"),
                                "observed_goodput_Bps": item.get(
                                    "observed_goodput_bps"
                                ),
                                "jitter_ms": item.get("jitter_ms"),
                                "loss_ratio": item.get("loss_ratio"),
                                "sample_count": item.get("sample_count"),
                                "connections_opened": item.get("connections_opened"),
                                "frames_sent": item.get("frames_sent"),
                                "reconnect_count": item.get("reconnect_count"),
                                "selected_path_changes": item.get(
                                    "selected_path_changes"
                                ),
                                "measurement_source": "iroh_activation_plane",
                                "measured_at_unix_ms": measured_at,
                                "fresh_until_unix_ms": measured_at + 7_200_000,
                                "exclusions": [],
                            }
                        )
                    observations = tuple(projected)
                except IrohTransportError:
                    raise
                except BaseException as error:
                    raise self._map_sidecar_error(
                        "transport_observation_failed", error
                    ) from error
        return TransportEvidence(
            local_node_id=self.node_id,
            local_endpoint_id=endpoint_id,
            peer_node_id=peer.node_id,
            peer_endpoint_id=peer.endpoint_id,
            peer_generation=peer.generation,
            remote_frames_sent=counters[0],
            remote_frames_received=counters[1],
            router_frames_dispatched=counters[2],
            duplicate_frames=counters[3],
            scoped_events=scoped_events,
            transport_path_observations=observations,
        )

    def counter_snapshot(self) -> dict[str, int]:
        """Return bounded local counters without querying the sidecar.

        Request-scoped cleanup receipts run under a strict two-second owner
        deadline. Full transport evidence may query activation-plane
        observations from the sidecar, so it must not sit in that critical
        path. These counters are read under the transport state lock and are
        signed by the physical node as part of its lean cleanup observation.
        """

        with self._state_lock:
            return self._counter_snapshot_locked()

    def _counter_snapshot_locked(self) -> dict[str, int]:
        return {
            "remote_frames_sent": self._remote_frames_sent,
            "remote_frames_received": self._remote_frames_received,
            "router_frames_dispatched": self._router_frames_dispatched,
            "duplicate_frames": self._duplicate_frames,
        }

    def close(self) -> None:
        with self._lifecycle_lock:
            self._close()

    def _close(self) -> None:
        with self._state_lock:
            if self._closed:
                clients: tuple[Any | None, ...] = ()
            else:
                self._closed = True
                self._running = False
                self._stop.set()
                for message_id, pending in self._pending.items():
                    reserved, cancellation_client = (
                        self._reserve_pending_cancellation_locked(
                            message_id,
                            pending,
                            "transport_closed",
                        )
                    )
                    if reserved:
                        self._start_delivery_cancel_locked(
                            cancellation_client,
                            message_id,
                            pending,
                        )
                clients = (
                    self._receive_client,
                    self._send_client,
                    self._control_client,
                    self._forward_client,
                    *self._cancellation_clients,
                )
                self._receive_client = None
                self._send_client = None
                self._control_client = None
                self._forward_client = None
                self._cancellation_clients = ()
            thread = self._receiver_thread
            dispatcher_thread = self._dispatcher_thread
            forward_thread = self._forward_thread
            cancellation_threads = tuple(self._cancellation_threads.values())
            delivery_cancel_threads = tuple(self._delivery_cancel_threads.values())
        for client in clients:
            if client is None:
                continue
            interrupt = getattr(client, "interrupt", None)
            if callable(interrupt):
                try:
                    interrupt()
                except BaseException:
                    pass
            try:
                client.close()
            except BaseException:
                pass
        receipt_trace_deadline = time.monotonic() + self.delivery_timeout_seconds
        with self._receipt_trace_condition:
            while self._inflight_receipt_trace_commits:
                remaining = receipt_trace_deadline - time.monotonic()
                if remaining <= 0:
                    raise IrohTransportError("receipt_trace_commit_shutdown_timeout")
                self._receipt_trace_condition.wait(remaining)
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=max(1.0, self.poll_interval_seconds * 4))
            if thread.is_alive():
                error = IrohTransportError("receiver_shutdown_timeout")
                self._set_fatal(error)
                raise error
        if (
            dispatcher_thread is not None
            and dispatcher_thread is not threading.current_thread()
        ):
            dispatcher_thread.join(timeout=max(1.0, self.delivery_timeout_seconds))
            if dispatcher_thread.is_alive():
                error = IrohTransportError("dispatcher_shutdown_timeout")
                self._set_fatal(error)
                raise error
        if (
            forward_thread is not None
            and forward_thread is not threading.current_thread()
        ):
            forward_thread.join(timeout=max(1.0, self.delivery_timeout_seconds))
            if forward_thread.is_alive():
                error = IrohTransportError("forwarder_shutdown_timeout")
                self._set_fatal(error)
                raise error
        for cancellation_thread in cancellation_threads:
            if cancellation_thread is threading.current_thread():
                continue
            cancellation_thread.join(timeout=max(1.0, self.delivery_timeout_seconds))
            if cancellation_thread.is_alive():
                error = IrohTransportError("path_cancellation_shutdown_timeout")
                self._set_fatal(error)
                raise error
        for delivery_cancel_thread in delivery_cancel_threads:
            if delivery_cancel_thread is None:
                continue
            if delivery_cancel_thread is threading.current_thread():
                continue
            if not delivery_cancel_thread.is_alive():
                continue
            delivery_cancel_thread.join(
                timeout=max(0.05, self.poll_interval_seconds * 4)
            )
            if delivery_cancel_thread.is_alive():
                error = IrohTransportError("delivery_cancellation_shutdown_timeout")
                self._set_fatal(error)
                raise error
        with self._state_lock:
            if self._receiver_thread is thread:
                self._receiver_thread = None
            if self._dispatcher_thread is dispatcher_thread:
                self._dispatcher_thread = None
            if self._forward_thread is forward_thread:
                self._forward_thread = None

    def _set_fatal(self, error: IrohTransportError) -> None:
        with self._state_lock:
            if self._fatal_error is None:
                self._fatal_error = error
            self._running = False
            self._stop.set()

    def _require_running(self) -> None:
        if self._closed:
            raise IrohTransportError("transport_closed")
        if not self._running:
            if self._fatal_error is not None:
                raise self._fatal_error
            raise IrohTransportError("transport_not_running")

    @staticmethod
    def _map_sidecar_error(prefix: str, error: BaseException) -> IrohTransportError:
        if isinstance(error, IrohTransportError):
            return error
        detail = error.code if isinstance(error, SidecarError) else type(error).__name__
        return IrohTransportError(prefix, detail)


IrohTransportPort = IrohTransport


__all__ = [
    "DELIVERY_SEMANTICS",
    "DeliveryReceipt",
    "IrohTransport",
    "IrohTransportError",
    "IrohTransportPort",
    "PROCESS_LIFETIME_LIMITATION",
    "PeerBinding",
    "TransportEvidence",
]
