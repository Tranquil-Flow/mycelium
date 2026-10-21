# SPDX-License-Identifier: AGPL-3.0-only
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
import re
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
    KVCheckpointAck,
    KVCheckpointCommit,
    KVCheckpointShipment,
    KVShadowRelease,
    KVSuccessorCutover,
    KVSuccessorCutoverReceipt,
    ManifestDelta,
    ManifestLocked,
    PathCancellation,
    PrefillChunkCompleted,
    ProgressivePrefillContext,
    ProgressivePrefillMessage,
    TokenEvent,
    VerifyEvent,
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
# One bounded sidecar cancellation attempt.  Inside a live owner operation this
# is the per-attempt window, sized to leave retry headroom rather than consume
# the whole cleanup authority at once; after the transport is closed it is the
# whole shutdown grant close() hands a worker it spawns for a still-pending
# send.
_DELIVERY_CANCEL_ATTEMPT_TIMEOUT_SECONDS = 0.2
# A delivery timeout is a *delivery* budget, not a *shutdown* budget: it is the
# sender's own deadline and may be tens of seconds.  Joining the delivery-cancel
# workers ``_close`` spawns for ``max(1.0, delivery_timeout_seconds)`` therefore
# made one ``close()`` call take 10 s at a 10 s delivery timeout, which is what
# `test_close_or_rotation_first_blocked_cancel_has_bounded_lifecycle[close]`
# measures (and `test_deadline_cancel_worker_is_accounted_and_close_is_bounded`
# measures at a 50 ms delivery timeout).  ``_close`` now hands every worker it
# spawns ONE absolute shutdown deadline and waits that same deadline plus this
# handoff margin, so the worker's sanction and close()'s join are one clock by
# construction -- a join merely equal to an independently computed window still
# races it.
_DELIVERY_CANCEL_SHUTDOWN_GRANT_SECONDS = _DELIVERY_CANCEL_ATTEMPT_TIMEOUT_SECONDS
_DELIVERY_CANCEL_SHUTDOWN_HANDOFF_SECONDS = 0.05
_NON_DELIVERED_SCOPED_ERRORS = frozenset(
    {
        "manifest_delta_queue_full",
        "manifest_registration_rejected",
    }
)


#: Queued on the dispatch queue (never sent on the wire) to make the dispatcher
#: take over hops that another thread queued for a runtime batch.
_HOP_BATCH_WAKE = object()


# -- Prefill stall detection and recovery (A14 class D) ----------------------
#
# Under --a8-force-relay on a degraded mobile hotspot the relay connection
# flapped: prefill chunk 0 was confirmed and chunk 1 never crossed.  The chunk's
# confirmed send would have waited its whole delivery budget (240 s), but the
# node's equally long prefill wait had started earlier, so it expired first
# with the generic ``prefill_completion_timeout``.  Owner policy: slow links
# are never refused - deadlines scale with the measured link - but a link that
# makes *no* progress is detected, recovered where possible and otherwise
# reported with its own code.
#
# Only prefill-phase frames get the resend recovery; everything else keeps the
# single-attempt delivery contract byte for byte.
_PREFILL_STALL_HOP_PHASES = frozenset({"PREFILL", "PREFILL_CHUNK"})
#: Confirmed deliveries kept per peer endpoint for the in-product link estimate.
_LINK_SAMPLE_LIMIT = 64
#: A confirmed frame at least this large measures goodput (bytes / seconds).
_GOODPUT_SAMPLE_MIN_BYTES = 16 * 1024
#: A confirmed frame at most this large measures round-trip time.
_RTT_SAMPLE_MAX_BYTES = 4 * 1024
#: Requests whose prefill progress is tracked at once (bounded LRU).
_PREFILL_PROGRESS_LIMIT = 256
#: Upper bound on the one synchronous exact-message cancel a stall resend issues.
_STALL_CANCEL_TIMEOUT_SECONDS = 1.0


@dataclass(frozen=True)
class LinkEstimate:
    """Measured (or floor) link figures that scale the prefill deadlines.

    ``goodput_source`` / ``rtt_source`` name where each figure came from:
    ``transport_observation:relay`` (the sidecar's per-peer activation-plane
    rows - the rows the product transport history records - restricted to the
    relay path when the peer has relay rows), ``transport_observation:direct``,
    ``delivery_samples`` (this adapter's own confirmed deliveries) or
    ``unmeasured`` (policy floor).  Several sources combine conservatively:
    the lowest goodput and the highest RTT win.
    """

    goodput_bytes_per_second: float
    rtt_p95_seconds: float
    goodput_source: str
    rtt_source: str


@dataclass(frozen=True)
class PrefillStallPolicy:
    """Every number the prefill stall/deadline logic uses, in one place.

    * stall window  ``S = clamp(8 x rtt_p95 + chunk_bytes / goodput x 4,
      30 s, 120 s)`` - one confirmed-send attempt for one prefill frame;
    * resends       at most ``max_resends`` (2) after the first attempt, each
      also bounded by the frame's remaining delivery deadline;
    * watchdog      ``W = (max_resends + 1) x S + 4 x rtt_p95`` - the node's
      no-progress bound for a request, long enough for either end's transport
      to finish its own recovery first;
    * total         ``clamp(remaining_bytes / goodput x 3 + chunks x 4 x
      rtt_p95, 60 s, command_timeout x 0.8)``; the ceiling wins when
      ``command_timeout x 0.8`` is below the floor;
    * ordering      a frame of a watched request must expire
      ``ordering_margin_seconds`` before the prefill wait does.

    Unmeasured links use 4 KiB/s and 1 s.
    """

    stall_floor_seconds: float = 30.0
    stall_ceiling_seconds: float = 120.0
    stall_rtt_multiplier: float = 8.0
    stall_transfer_multiplier: float = 4.0
    total_floor_seconds: float = 60.0
    total_command_fraction: float = 0.8
    total_transfer_multiplier: float = 3.0
    total_rtt_per_chunk_multiplier: float = 4.0
    unmeasured_goodput_bytes_per_second: float = 4096.0
    unmeasured_rtt_seconds: float = 1.0
    max_resends: int = 2
    ordering_margin_seconds: float = 2.0
    watchdog_rtt_multiplier: float = 4.0

    def __post_init__(self) -> None:
        for name in (
            "stall_floor_seconds",
            "stall_ceiling_seconds",
            "total_floor_seconds",
            "total_command_fraction",
            "unmeasured_goodput_bytes_per_second",
            "unmeasured_rtt_seconds",
        ):
            value = getattr(self, name)
            if not isinstance(value, (int, float)) or not value > 0:
                raise ValueError(f"{name} must be positive")
        for name in (
            "stall_rtt_multiplier",
            "stall_transfer_multiplier",
            "total_transfer_multiplier",
            "total_rtt_per_chunk_multiplier",
            "ordering_margin_seconds",
            "watchdog_rtt_multiplier",
        ):
            value = getattr(self, name)
            if not isinstance(value, (int, float)) or value < 0:
                raise ValueError(f"{name} must not be negative")
        if self.stall_floor_seconds > self.stall_ceiling_seconds:
            raise ValueError("stall floor must not exceed the stall ceiling")
        if type(self.max_resends) is not int or self.max_resends < 0:
            raise ValueError("max_resends must be a non-negative integer")

    def stall_seconds(self, chunk_bytes: int, link: Any) -> float:
        raw = (
            self.stall_rtt_multiplier * float(link.rtt_p95_seconds)
            + max(0, int(chunk_bytes))
            / float(link.goodput_bytes_per_second)
            * self.stall_transfer_multiplier
        )
        return min(self.stall_ceiling_seconds, max(self.stall_floor_seconds, raw))

    def watchdog_seconds(self, chunk_bytes: int, link: Any) -> float:
        return (self.max_resends + 1) * self.stall_seconds(
            chunk_bytes, link
        ) + self.watchdog_rtt_multiplier * float(link.rtt_p95_seconds)

    def total_seconds(
        self,
        *,
        remaining_bytes: int,
        chunks: int,
        link: Any,
        command_timeout: float,
    ) -> float:
        raw = (
            max(0, int(remaining_bytes))
            / float(link.goodput_bytes_per_second)
            * self.total_transfer_multiplier
            + max(0, int(chunks))
            * self.total_rtt_per_chunk_multiplier
            * float(link.rtt_p95_seconds)
        )
        ceiling = float(command_timeout) * self.total_command_fraction
        return min(ceiling, max(self.total_floor_seconds, raw))

    def kv_lease_ceiling_seconds(self, base_seconds: float) -> float:
        """Upper bound of a link-scaled KV lease window.

        ``(max_resends + 1) x stall_ceiling + watchdog_rtt_multiplier x
        rtt_cap`` with ``rtt_cap = stall_ceiling / stall_rtt_multiplier`` (the
        RTT above which ``S`` is clamped anyway): 420 s with the defaults.  A
        host also bounds an imported (peer-issued) reservation lease by it.
        """

        rtt_cap = (
            self.stall_ceiling_seconds / self.stall_rtt_multiplier
            if self.stall_rtt_multiplier > 0
            else self.stall_ceiling_seconds
        )
        return max(
            float(base_seconds),
            (self.max_resends + 1) * self.stall_ceiling_seconds
            + self.watchdog_rtt_multiplier * rtt_cap,
        )

    def kv_lease_seconds(
        self, *, base_seconds: float, chunk_bytes: int, link: Any
    ) -> float:
        """KV reservation lease / progress-renewal window for the measured link.

        ``min(ceiling, max(base, W))``: never shorter than the Router's base
        lease, and never shorter than the watchdog ``W`` - the no-progress
        bound after which the request is abandoned anyway - so a lease cannot
        expire on a link that is still delivering within the transport's own
        stall recovery.  Work with no progress for this long expires.
        """

        return min(
            self.kv_lease_ceiling_seconds(base_seconds),
            max(float(base_seconds), self.watchdog_seconds(chunk_bytes, link)),
        )


DEFAULT_PREFILL_STALL_POLICY = PrefillStallPolicy()


def _prefill_stall_frame(message: Any) -> bool:
    """Frames whose stalled delivery is detected and resent (same message id).

    The first generated token (``TokenEvent`` index 0) belongs to the prefill:
    the final stage sends it synchronously inside the dispatch of the frame
    that completed the prefill, so the entry's own prefill frame is confirmed
    only after this token is.  Decode tokens (index >= 1) keep the
    single-attempt contract.
    """

    if isinstance(message, HopHeader):
        return message.phase in _PREFILL_STALL_HOP_PHASES
    if isinstance(message, TokenEvent):
        return message.token_index == 0
    return isinstance(
        message, (ProgressivePrefillMessage, PrefillChunkCompleted, ManifestLocked)
    )


#: Per-peer ring of recent frame kinds kept for an evidence bundle.
_PEER_FRAME_HISTORY = 16
#: Pending sends listed in one ``diagnostic_state`` snapshot.
_DIAGNOSTIC_PENDING_LIMIT = 32
#: FailureReports (sent and received) kept for an evidence bundle.
_FAILURE_REPORT_HISTORY = 16
_FAILURE_REPORT_TEXT = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:\-]{0,95}\Z")


def _failure_report_fields(report: FailureReport) -> dict[str, Any]:
    """Bounded, id-free fields of one FailureReport for diagnostics.

    ``reason`` is the stage runtime's (or relay's) failure code, ``scope``
    its class, ``placement_id`` names the hop/stage placement that failed and
    ``token_index`` the chunk (``PREFILL_CHUNK``; ``-1`` = the progressive
    first chunk) or decode step.  Request and path ids are never included; a
    value outside the plain code shape is replaced by its sha256 prefix.
    """

    fields: dict[str, Any] = {}
    for name in ("scope", "reason", "placement_id", "edge_id", "node_id"):
        value = getattr(report, name, "")
        if not isinstance(value, str) or not value:
            continue
        if _FAILURE_REPORT_TEXT.fullmatch(value):
            fields[name] = value
        else:
            fields[name] = "sha256:" + hashlib.sha256(
                value.encode("utf-8", errors="replace")
            ).hexdigest()[:16]
    token_index = getattr(report, "token_index", None)
    if type(token_index) is int and -(2**31) <= token_index < 2**31:
        fields["token_index"] = token_index
    return fields


def _frame_kind(message: Any) -> str:
    """The message type name, plus the hop phase for a hop (never ids)."""

    name = type(message).__name__
    if isinstance(message, HopHeader):
        return f"{name}:{message.phase}"
    header = getattr(message, "header", None)
    if isinstance(header, HopHeader):
        return f"{name}:{header.phase}"
    return name


def _age_ms(since: float | None, now: float) -> int | None:
    if since is None:
        return None
    return max(0, min(int(round((now - since) * 1000)), 10**12))


def _positive_number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if not number > 0 or number != number or number == float("inf"):
        return None
    return number


def _p95(values: Sequence[float]) -> float:
    ordered = sorted(values)
    index = max(0, -(-95 * len(ordered) // 100) - 1)
    return ordered[min(index, len(ordered) - 1)]


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
    if isinstance(source, HopHeader):
        # Retain the existing bounded request/receipt evidence first. Missing
        # optional attribution means unknown, never permission to infer a hop.
        for field_name in ('destination_placement_id', 'path_id', 'source_placement_id'):
            value = getattr(source, field_name)
            if isinstance(value, str) and len(value.encode('utf-8')) <= _TRACE_ID_BYTES:
                candidate = {**identity, field_name: value}
                if fits(candidate):
                    identity = candidate
        if type(source.path_attempt) is int and 0 <= source.path_attempt < 2**63:
            candidate = {**identity, 'path_attempt': source.path_attempt}
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
    # Diagnostics only (evidence bundle): frame kind, peer node, start time.
    kind: str = ""
    peer_node_id: str = ""
    started_at: float = 0.0
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
    # Diagnostics only (evidence bundle).
    kind: str = ""
    received_at: float = 0.0


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


@dataclass(frozen=True)
class _ForwardFenceScope:
    """The exact (request, path, attempt) subject an A7 forward fence sweeps."""

    request_id: str
    path_id: str
    path_attempt: int


@dataclass
class _PrefillProgress:
    """Per-request link progress for one watched prefill (cheap counters only).

    Progress is any confirmed outbound frame or dispatched inbound frame of
    the request.  ``deadline`` is the node's prefill-wait deadline: frames of
    the request expire ``ordering_margin_seconds`` before it.
    """

    started_at: float
    last_progress_at: float
    deadline: float | None = None
    progress_events: int = 0
    bytes_confirmed: int = 0
    max_frame_bytes: int = 0
    stall_resends: int = 0
    send_lock_stalls: int = 0
    failure_code: str | None = None


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
        stall_policy: PrefillStallPolicy | None = None,
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
        from mycelium_iroh_sidecar.client import local_sidecar_address

        self.socket_path = local_sidecar_address(socket_path)
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
        # Received frames, plus at most one ``_HOP_BATCH_WAKE`` marker (never on
        # the wire), which has its own slot so frames keep their full capacity.
        self._dispatch_queue: Queue[
            tuple[bytes, str, int, bytes, bytes, DecodedFrame | None] | object
        ] = Queue(maxsize=queue_capacity + 1)
        self._hop_wake_pending = False
        self._forward_queue: Queue[
            tuple[str, bytes, str | None, str | None, int | None]
        ] = Queue(maxsize=queue_capacity)
        self._ack_queue: Queue[_AckRequest] = Queue(maxsize=queue_capacity)
        # Set whenever a dispatch acknowledgement is queued and whenever the
        # dispatcher finishes a frame, so the receiver sends the acknowledgement
        # and takes the next frame at once instead of at its next poll.
        self._ack_ready = threading.Event()
        self._cancellation_threads: dict[str, threading.Thread] = {}
        self._delivery_cancel_threads: dict[bytes, threading.Thread] = {}
        # Message ids whose registered delivery-cancel worker has actually issued a
        # sidecar cancellation attempt.  close() reads it to attribute a shutdown
        # overrun (see _mark_delivery_cancel_attempt_started).  It is keyed by the
        # message id because the send record itself is removed by the sender's own
        # wind-down while the worker that owns the cancellation is still running,
        # and it is discarded exactly where the worker unregisters, so it stays
        # bounded by the number of live delivery-cancel workers.
        self._delivery_cancel_attempts: set[bytes] = set()
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
        # True only on the dispatcher thread while it drains runtime batches:
        # every forward a drain produces then goes through the forward worker.
        self._draining_hop_batches = False
        self._last_dispatch_error: dict[str, str] | None = None
        # Monotonic end of the open hop collection window, or None (dispatcher
        # thread only; see ``_queue_hop_for_batch``).
        self._hop_collection_deadline: float | None = None
        self._outbound_trace: deque[str] = deque(maxlen=256)
        self._inflight_receipt_trace_commits = 0
        self._remote_frames_sent = 0
        self._remote_frames_received = 0
        self._router_frames_dispatched = 0
        self._duplicate_frames = 0
        self._scoped_event_sequence = 0
        self._scoped_events: deque[_ScopedTransportEvent] = deque(maxlen=256)
        self._path_graphs: dict[str, Any] = {}
        # Live, locally registered subjects. Request owner control fences all
        # admitted successors; a peer PathCancellation remains exact-path only.
        self._path_subjects: dict[str, PathCancellation] = {}
        self._participant_nodes_by_path: dict[str, frozenset[str]] = {}
        self._entry_nodes: dict[str, str] = {}
        self.manifest_deltas: list[ManifestDelta] = []
        # A14 class D: prefill stall detection, recovery and link estimate.
        # The module default is read at construction so a test can scale it.
        self._stall_policy: PrefillStallPolicy = (
            DEFAULT_PREFILL_STALL_POLICY if stall_policy is None else stall_policy
        )
        self._prefill_progress: OrderedDict[str, _PrefillProgress] = OrderedDict()
        # peer endpoint id -> recent (frame bytes, seconds) confirmed deliveries
        self._delivery_samples: dict[str, deque[tuple[int, float]]] = {}
        # Last activation-plane rows ``evidence()`` projected (no extra query).
        self._path_observation_cache: tuple[Mapping[str, Any], ...] = ()
        self._prefill_stall_resends = 0
        self._prefill_stall_failures = 0
        self._prefill_send_lock_stalls = 0
        # Evidence-bundle diagnostics (bounded, ids never included): who holds
        # the shared data client, since when the dispatcher is in its phase,
        # the active forward, and the last frame kinds per peer.
        self._send_operation_holder: tuple[str, bytes, float] | None = None
        self._send_operation_waiters = 0
        self._active_forward: tuple[str, float] | None = None
        self._peer_inbound: dict[str, deque[tuple[str, float, str]]] = {}
        self._peer_outbound: dict[str, deque[tuple[str, float, str]]] = {}
        # A14 serve-2 (f9a8d961 r1): the FailureReport's own reason was in
        # neither node's bundle; keep the last reports (both directions).
        self._failure_reports: deque[dict[str, Any]] = deque(
            maxlen=_FAILURE_REPORT_HISTORY
        )

    @property
    def stall_policy(self) -> PrefillStallPolicy:
        return self._stall_policy

    @property
    def _dispatcher_phase(self) -> str:
        return self.__dict__.get("_dispatcher_phase_value", "idle")

    @_dispatcher_phase.setter
    def _dispatcher_phase(self, phase: str) -> None:
        # Every assignment happens under ``_state_lock``; the time it entered
        # the phase is what an evidence bundle reports for a stuck dispatcher.
        self.__dict__["_dispatcher_phase_value"] = phase
        self.__dict__["_dispatcher_phase_since"] = time.monotonic()

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

        request_wide = self._request_controlled_cancelled_locked(request_id)
        paths = {path_id}
        if request_wide:
            paths.update(subject.path_id for subject in self._path_subjects.values()
                         if subject.request_id == request_id)

        def matches(
            candidate_request: str | None,
            candidate_path: str | None,
            candidate_attempt: int | None,
        ) -> bool:
            return (
                candidate_request == request_id
                and (request_wide or candidate_path == path_id)
                and (
                    request_wide or path_attempt is None
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
            and (request_wide or scope[1] == path_id)
            and (request_wide or path_attempt is None or scope[2] == path_attempt)
        )
        return {
            "pending_delivery_count": pending_count,
            "inflight_received_count": inflight_count,
            "forward_count": forward_count,
            "lineage_subject_registered": any(path in self._path_subjects for path in paths),
            "path_graph_registered": any(path in self._path_graphs for path in paths),
            "participants_registered": any(path in self._participant_nodes_by_path for path in paths),
            # Peer/legacy teardown proves one exact path. A different live
            # successor (or retired predecessor awaiting delivery) retains the
            # shared entry identity, but is not that exact path's blocker.
            # Authenticated owner cleanup still covers the whole request.
            "entry_registered": (
                request_id in self._entry_nodes
                and (
                    request_wide
                    or path_id in self._path_subjects
                    or not any(subject.request_id == request_id
                               for subject in self._path_subjects.values())
                )
            ),
            "cancellation_worker_active": any(path in self._cancellation_threads for path in paths),
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

    @classmethod
    def _fence_scope(
        cls,
        decoded: DecodedFrame,
    ) -> tuple[str | None, str | None, int | None]:
        """The request scope cancellation fences and sweeps apply to.

        A ``KVShadowRelease`` only frees A7 state a node holds for a request
        that has already ended; it can never create work.  Owner cancellation
        of the same request routinely lands just before the entry's terminal
        release, so fencing the release would silently strand the standby's
        shadow chain.  It is therefore delivered unscoped: no cancellation
        fence, pending-send sweep or forward-scope drop applies to it.
        Receipt/failure evidence still uses ``_frame_scope``.
        """

        if isinstance(decoded.message, KVShadowRelease):
            return (None, None, None)
        return cls._frame_scope(decoded)

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
            frame_bytes=len(frame),
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
        request_id, path_id, path_attempt = self._fence_scope(decoded)
        # Deadline ordering (A14 class D): a frame of a request whose prefill
        # wait is registered must expire before that wait does, so a stuck hop
        # reports its own code while the wait can still observe it.
        deadline = time.monotonic() + self.delivery_timeout_seconds
        if request_id is not None:
            with self._state_lock:
                watched = self._prefill_progress.get(request_id)
                watched_deadline = None if watched is None else watched.deadline
            if watched_deadline is not None:
                deadline = min(
                    deadline,
                    watched_deadline - self._stall_policy.ordering_margin_seconds,
                )
                if deadline <= time.monotonic():
                    raise IrohTransportError("delivery_deadline_exceeded")
        stall_seconds = (
            self._stall_policy.stall_seconds(
                len(frame), self.link_estimate(destination_node_id)
            )
            if _prefill_stall_frame(decoded.message)
            else None
        )
        if not self._send_slots.acquire(blocking=False):
            raise IrohTransportError("adapter_queue_full")

        message_id = uuid.uuid4().bytes
        frame_kind = _frame_kind(decoded.message)
        failure_record = (
            self._note_failure_report(
                "sent", destination_node_id, decoded.message, "sending"
            )
            if isinstance(decoded.message, FailureReport)
            else None
        )
        pending = _PendingSend(
            peer.generation,
            request_id,
            path_id,
            path_attempt,
            cancellable_forward=_cancellable_forward,
        )
        cancel_timer: threading.Timer | None = None
        send_operation_acquired = False
        stalled_windows = 0
        outcome = "failed"
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
                    kind=frame_kind,
                    peer_node_id=destination_node_id,
                    started_at=time.monotonic(),
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
                max(0.0, deadline - time.monotonic()),
                self._expire_pending,
                args=(message_id, pending, "delivery_deadline_exceeded"),
            )
            cancel_timer.daemon = True
            cancel_timer.start()
            if _send_client_override is None:
                stalled_windows = self._acquire_send_operation(
                    message_id,
                    pending,
                    deadline=deadline,
                    stall_seconds=stall_seconds,
                    request_id=request_id,
                )
                send_operation_acquired = True
                with self._state_lock:
                    self._send_operation_holder = (
                        frame_kind,
                        message_id,
                        time.monotonic(),
                    )
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
            self._send_confirmed_recovering(
                client,
                frame,
                message_id,
                pending=pending,
                peer=peer,
                destination_node_id=destination_node_id,
                deadline=deadline,
                stall_seconds=stall_seconds,
                send_client_override=_send_client_override,
                request_id=request_id,
                stalled_windows=stalled_windows,
            )
            with self._state_lock:
                current = self._lookup_destination_peer(destination_node_id)
                if pending.cancelled or current.generation != pending.generation:
                    raise IrohTransportError(pending.reason or "peer_rotated")
                pending.completed = True
                self._remote_frames_sent += 1
            outcome = "confirmed"
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
                    outcome = "confirmed"
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
                    self._send_operation_holder = None
                self._note_peer_frame_locked(
                    self._peer_outbound,
                    destination_node_id,
                    frame_kind,
                    outcome if outcome == "confirmed" else pending.reason or outcome,
                )
                if failure_record is not None:
                    failure_record["outcome"] = str(
                        outcome if outcome == "confirmed" else pending.reason or outcome
                    )[:96]
            if send_operation_acquired:
                self._send_operation_lock.release()
            self._send_slots.release()

    def _acquire_send_operation(
        self,
        message_id: bytes,
        pending: _PendingSend,
        *,
        deadline: float,
        stall_seconds: float | None,
        request_id: str | None,
    ) -> int:
        """Take the shared data client; return the stall windows spent waiting.

        A frame without a stall window waits up to its deadline, as before.  A
        prefill frame's wait is a stall like a stalled attempt (A14 r1, c1):
        it waits at most one window at a time, every window during which
        one other send held the client throughout is counted, reported with
        the holder's frame kind and age, and charged to the frame's stall budget,
        and after ``max_resends + 1`` windows the frame fails as
        ``prefill_transport_stalled`` instead of waiting out a deadline of up
        to ``command_timeout x 0.8``.
        """

        lock = self._send_operation_lock
        with self._state_lock:
            self._send_operation_waiters += 1
        try:
            if stall_seconds is None:
                if not lock.acquire(timeout=self._remaining(deadline)):
                    raise TimeoutError("delivery deadline exceeded")
                return 0
            windows = 0
            while True:
                remaining = self._remaining(deadline)
                window_bound = stall_seconds < remaining
                window_started = time.monotonic()
                if lock.acquire(timeout=min(stall_seconds, remaining)):
                    return windows
                if not window_bound:
                    raise TimeoutError("delivery deadline exceeded")
                now = time.monotonic()
                with self._state_lock:
                    if pending.cancelled:
                        raise IrohTransportError(pending.reason or "delivery_cancelled")
                    holder = self._send_operation_holder
                    if holder is None or holder[2] > window_started:
                        # The client changed hands during the window: other
                        # sends are progressing; waiting behind them is not
                        # a stall (still bounded by the frame deadline).
                        continue
                    exhausted = windows >= self._stall_policy.max_resends
                    if exhausted:
                        self._prefill_stall_failures += 1
                    else:
                        windows += 1
                        self._prefill_send_lock_stalls += 1
                        progress = (
                            None
                            if request_id is None
                            else self._prefill_progress.get(request_id)
                        )
                        if progress is not None:
                            progress.send_lock_stalls += 1
                holder_fields = (
                    {}
                    if holder is None
                    else {
                        "holder_kind": holder[0],
                        "holder_age_ms": _age_ms(holder[2], now) or 0,
                    }
                )
                if exhausted:
                    self._emit_stall_event(
                        "prefill_stall_exhausted",
                        attempts=windows + 1,
                        stall_seconds=stall_seconds,
                        **holder_fields,
                    )
                    self._expire_pending(message_id, pending, "prefill_transport_stalled")
                    raise IrohTransportError("prefill_transport_stalled")
                self._emit_stall_event(
                    "prefill_stall_send_lock",
                    attempts=windows,
                    stall_seconds=stall_seconds,
                    **holder_fields,
                )
        finally:
            with self._state_lock:
                self._send_operation_waiters -= 1

    def _record_scoped_event(
        self,
        *,
        event: str,
        request_id: str | None,
        path_id: str | None,
        path_attempt: int | None,
        peer_node_id: str,
        code: str | None = None,
        frame_bytes: int = 0,
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
            if event == "receipt":
                self._note_prefill_progress_locked(
                    request_id, frame_bytes, confirmed=True
                )
            else:
                progress = self._prefill_progress.get(request_id)
                if progress is not None and progress.failure_code is None:
                    progress.failure_code = code
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
        started = time.monotonic()
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
        elapsed = time.monotonic() - started
        if elapsed > 0:
            endpoint = (
                destination_endpoint_id
                if destination_endpoint_id is not None
                else self._peer.endpoint_id
            )
            with self._state_lock:
                samples = self._delivery_samples.get(endpoint)
                if samples is None:
                    if len(self._delivery_samples) >= _PREFILL_PROGRESS_LIMIT:
                        self._delivery_samples.pop(next(iter(self._delivery_samples)))
                    samples = deque(maxlen=_LINK_SAMPLE_LIMIT)
                    self._delivery_samples[endpoint] = samples
                samples.append((len(frame), elapsed))

    def _send_confirmed_recovering(
        self,
        client: Any,
        frame: bytes,
        message_id: bytes,
        *,
        pending: _PendingSend,
        peer: PeerBinding,
        destination_node_id: str,
        deadline: float,
        stall_seconds: float | None,
        send_client_override: Any | None,
        request_id: str | None,
        stalled_windows: int = 0,
    ) -> None:
        """One confirmed delivery, with stall recovery for prefill frames.

        Without a stall window this is exactly one ``_send_confirmed`` bounded
        by the frame deadline.  With one, each attempt is bounded by the
        window; an attempt that times out *because of the window* (not the
        frame deadline) is a stall: the stuck sidecar item is withdrawn by its
        exact message id, the data client whose session the timeout
        invalidated is replaced, and the same frame is resent under the same
        message id - the receiving sidecar and transport de-duplicate by it.
        After ``max_resends`` resends the frame fails as
        ``prefill_transport_stalled``.  The frame deadline still bounds every
        attempt, so a deadline expiry stays ``delivery_deadline_exceeded``.
        ``stalled_windows`` already spent waiting for the shared data client
        count against the same budget.
        """

        destination_endpoint_id = (
            peer.endpoint_id if peer.node_id != self._peer.node_id else None
        )
        if stall_seconds is None:
            self._send_confirmed(
                client,
                frame,
                message_id,
                expected_generation=pending.generation,
                timeout=self._remaining(deadline),
                destination_endpoint_id=destination_endpoint_id,
            )
            return
        resends = stalled_windows
        while True:
            remaining = self._remaining(deadline)
            window_bound = stall_seconds < remaining
            try:
                self._send_confirmed(
                    client,
                    frame,
                    message_id,
                    expected_generation=pending.generation,
                    timeout=min(stall_seconds, remaining),
                    destination_endpoint_id=destination_endpoint_id,
                )
                return
            except TimeoutError:
                if not window_bound:
                    raise
            with self._state_lock:
                if pending.cancelled:
                    raise IrohTransportError(pending.reason or "delivery_cancelled")
            if resends >= self._stall_policy.max_resends:
                with self._state_lock:
                    self._prefill_stall_failures += 1
                self._emit_stall_event(
                    "prefill_stall_exhausted",
                    attempts=resends + 1,
                    stall_seconds=stall_seconds,
                )
                self._expire_pending(message_id, pending, "prefill_transport_stalled")
                raise IrohTransportError("prefill_transport_stalled")
            resends += 1
            with self._state_lock:
                self._prefill_stall_resends += 1
                progress = (
                    None
                    if request_id is None
                    else self._prefill_progress.get(request_id)
                )
                if progress is not None:
                    progress.stall_resends += 1
            self._emit_stall_event(
                "prefill_stall_resend",
                attempts=resends,
                stall_seconds=stall_seconds,
            )
            try:
                client = self._recover_stalled_send(
                    client,
                    message_id,
                    pending=pending,
                    destination_node_id=destination_node_id,
                    deadline=deadline,
                    send_client_override=send_client_override,
                )
            except (IrohTransportError, TimeoutError):
                raise
            except BaseException as error:
                self._expire_pending(message_id, pending, "prefill_transport_stalled")
                raise self._map_sidecar_error(
                    "prefill_transport_stalled", error
                ) from error

    def _recover_stalled_send(
        self,
        client: Any,
        message_id: bytes,
        *,
        pending: _PendingSend,
        destination_node_id: str,
        deadline: float,
        send_client_override: Any | None,
    ) -> Any:
        # 1. Withdraw the stuck sidecar outbound item so the resend is a fresh
        #    attempt instead of attaching to it as a duplicate.  A failed or
        #    ``unknown_message`` cancel is not fatal: the item was then already
        #    terminal (possibly delivered - the receiver de-duplicates).
        self._cancel_stalled_outbound(message_id, deadline=deadline)
        # 2. The timed-out attempt invalidated the client's local session.
        if send_client_override is None:
            self._reconnect_send_client(deadline=deadline)
            with self._state_lock:
                replacement = self._send_client
            if replacement is None:
                raise IrohTransportError("transport_not_running")
        else:
            replacement = self._replace_forward_client(client, deadline=deadline)
        # 3. The same fences as the first admission.
        with self._state_lock:
            self._require_running()
            current = self._lookup_destination_peer(destination_node_id)
            if pending.cancelled or current.generation != pending.generation:
                raise IrohTransportError(pending.reason or "peer_rotated")
        return replacement

    def _cancel_stalled_outbound(self, message_id: bytes, *, deadline: float) -> bool | str:
        timeout = min(_STALL_CANCEL_TIMEOUT_SECONDS, self._remaining(deadline))
        with self._state_lock:
            pooled = bool(self._cancellation_clients)
            control = self._control_client
        if not pooled:
            return self._cancel_with_client(control, message_id, timeout=timeout)
        try:
            lane = self._available_cancellation_clients.get(timeout=timeout)
        except Empty:
            return False
        try:
            outcome = self._cancel_with_client(lane, message_id, timeout=timeout)
            if outcome == "replace":
                try:
                    lane.close()
                except BaseException:
                    pass
                if self._reconnect_cancellation_client(
                    lane,
                    deadline=min(deadline, time.monotonic() + timeout),
                ):
                    outcome = self._cancel_with_client(
                        lane, message_id, timeout=timeout
                    )
            return outcome
        finally:
            with self._state_lock:
                reusable = not self._closed and lane in self._cancellation_clients
            if reusable:
                self._available_cancellation_clients.put_nowait(lane)

    def _replace_forward_client(self, previous: Any, *, deadline: float) -> Any:
        with self._rotation_lock:
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
            with self._state_lock:
                if self._closed or not self._running:
                    rejected = True
                    displaced = None
                else:
                    rejected = False
                    displaced = self._forward_client
                    self._forward_client = replacement
        if rejected:
            try:
                replacement.close()
            except BaseException:
                pass
            raise IrohTransportError("transport_not_running")
        for retired in {id(previous): previous, id(displaced): displaced}.values():
            if retired is not None and retired is not replacement:
                try:
                    retired.close()
                except BaseException:
                    pass
        return replacement

    #: Optional ``observer(event, fields)`` told about prefill stall recovery
    #: (``prefill_stall_resend`` / ``prefill_stall_exhausted``).  Fields are
    #: bounded numbers only.  A physical node installs one that prints an
    #: allowlisted stderr line, so a peer's own recovery reaches the route's
    #: per-node stderr tails.  ``None`` changes nothing.
    stall_event_observer = None

    #: Optional ``observer(direction, fields, request_id=...)`` told about
    #: every FailureReport this transport sends (``"sent"``) or receives
    #: (``"received"``).  ``fields`` are the bounded id-free fields of
    #: ``_failure_report_fields`` plus ``peer``.  A physical node prints a
    #: ``MYCELIUM-FAILURE-REPORT`` line, so the reason reaches both nodes'
    #: stderr tails and evidence bundles.  ``None`` changes nothing.
    failure_report_observer = None

    def _note_failure_report(
        self,
        direction: str,
        peer_node_id: str,
        report: FailureReport,
        outcome: str,
    ) -> dict[str, Any]:
        fields = _failure_report_fields(report)
        record: dict[str, Any] = {
            "direction": direction,
            "peer": str(peer_node_id)[:96],
            **fields,
            "outcome": outcome,
            "at": time.monotonic(),
        }
        with self._state_lock:
            history = self.__dict__.get("_failure_reports")
            if history is None:
                history = deque(maxlen=_FAILURE_REPORT_HISTORY)
                self._failure_reports = history
            history.append(record)
        observer = self.failure_report_observer
        if observer is not None:
            try:
                observer(
                    direction,
                    {"peer": record["peer"], **fields},
                    request_id=getattr(report, "request_id", None),
                )
            except BaseException:
                pass
        return record

    def _emit_stall_event(
        self,
        event: str,
        *,
        attempts: int,
        stall_seconds: float,
        holder_kind: str | None = None,
        holder_age_ms: int | None = None,
    ) -> None:
        observer = self.stall_event_observer
        if observer is None:
            return
        fields: dict[str, Any] = {
            "attempts": int(attempts),
            "stall_window_ms": int(round(stall_seconds * 1000)),
        }
        if holder_kind is not None:
            # The kind of the send that holds the shared data client (a
            # message type name, never an id) and how long it has held it.
            fields["holder_kind"] = holder_kind.split(":", 1)[0]
            fields["holder_age_ms"] = int(holder_age_ms or 0)
        try:
            observer(event, fields)
        except BaseException:
            pass

    # -- prefill progress / link estimate (A14 class D) -----------------------

    def watch_prefill(self, request_id: str, *, deadline_monotonic: float) -> None:
        """Track ``request_id``'s link progress and order its frames' deadlines.

        Repeated calls update the deadline (the node re-derives it as bytes
        complete) and keep the counters.
        """

        if not isinstance(request_id, str) or not request_id:
            raise ValueError("request_id must be a non-empty string")
        now = time.monotonic()
        with self._state_lock:
            progress = self._prefill_progress.get(request_id)
            if progress is None:
                progress = _PrefillProgress(started_at=now, last_progress_at=now)
                self._prefill_progress[request_id] = progress
                while len(self._prefill_progress) > _PREFILL_PROGRESS_LIMIT:
                    self._prefill_progress.popitem(last=False)
            progress.deadline = float(deadline_monotonic)
            self._prefill_progress.move_to_end(request_id)

    def release_prefill_watch(self, request_id: str) -> None:
        with self._state_lock:
            self._prefill_progress.pop(request_id, None)

    def prefill_progress(self, request_id: str) -> dict[str, Any] | None:
        """Snapshot of a watched request's progress (no sidecar query)."""

        with self._state_lock:
            progress = self._prefill_progress.get(request_id)
            if progress is None:
                return None
            return {
                "started_at": progress.started_at,
                "last_progress_at": progress.last_progress_at,
                "deadline": progress.deadline,
                "progress_events": progress.progress_events,
                "bytes_confirmed": progress.bytes_confirmed,
                "max_frame_bytes": progress.max_frame_bytes,
                "stall_resends": progress.stall_resends,
                "send_lock_stalls": progress.send_lock_stalls,
                "failure_code": progress.failure_code,
            }

    def stall_recovery_snapshot(self) -> dict[str, int]:
        with self._state_lock:
            return {
                "stall_resends": self._prefill_stall_resends,
                "stall_failures": self._prefill_stall_failures,
                "send_lock_stalls": self._prefill_send_lock_stalls,
            }

    def _note_peer_frame_locked(
        self,
        history: dict[str, deque[tuple[str, float, str]]],
        peer_node_id: str,
        kind: str,
        outcome: str,
    ) -> None:
        """Remember one frame kind for a peer (bounded; node ids only)."""

        if not isinstance(peer_node_id, str) or not peer_node_id:
            return
        ring = history.get(peer_node_id)
        if ring is None:
            if len(history) >= _PREFILL_PROGRESS_LIMIT:
                history.pop(next(iter(history)))
            ring = deque(maxlen=_PEER_FRAME_HISTORY)
            history[peer_node_id] = ring
        ring.append((kind[:96], time.monotonic(), outcome[:96]))

    def diagnostic_state(self) -> dict[str, Any]:
        """Bounded transport state for a node's evidence bundle.

        It names who holds the shared data client and since when, every
        outstanding send (frame kind, peer node, age, first 8 hex of its
        message id - the receiving node's bundle lists the same prefix, which
        correlates the two sides), the dispatcher's phase and its age, the
        active forward, frames still waiting for local dispatch, and the
        last frame kinds sent to and received from each peer.  Peers are
        named by node id; endpoint ids, addresses, payloads and request ids
        are never included.  No sidecar query is made.
        """

        now = time.monotonic()
        with self._state_lock:
            holder = self._send_operation_holder
            pending = sorted(
                self._pending.items(), key=lambda item: item[1].started_at
            )[:_DIAGNOSTIC_PENDING_LIMIT]
            inflight = list(self._inflight_received.items())[:_DIAGNOSTIC_PENDING_LIMIT]
            active_forward = self._active_forward
            failure_reports = [
                {
                    **{key: value for key, value in record.items() if key != "at"},
                    "age_ms": _age_ms(record.get("at"), now),
                }
                for record in self.__dict__.get("_failure_reports", ())
            ]
            peers: dict[str, dict[str, list[dict[str, Any]]]] = {}
            for name, history in (
                ("inbound", self._peer_inbound),
                ("outbound", self._peer_outbound),
            ):
                for peer_node_id, ring in history.items():
                    peers.setdefault(peer_node_id, {"inbound": [], "outbound": []})[
                        name
                    ] = [
                        {"kind": kind, "age_ms": _age_ms(at, now), "outcome": outcome}
                        for kind, at, outcome in ring
                    ]
            state = {
                "send_operation": {
                    "held": self._send_operation_lock.locked(),
                    "holder_kind": None if holder is None else holder[0],
                    "holder_message": None if holder is None else holder[1][:4].hex(),
                    "holder_age_ms": None if holder is None else _age_ms(holder[2], now),
                    "waiters": self._send_operation_waiters,
                },
                "pending_sends": [
                    {
                        "kind": record.kind,
                        "peer": record.peer_node_id,
                        "message": message_id[:4].hex(),
                        "age_ms": _age_ms(record.started_at or None, now),
                        "forward": record.cancellable_forward,
                        "admission_started": record.admission_started,
                        "cancelled": record.cancelled,
                    }
                    for message_id, record in pending
                ],
                "pending_send_count": len(self._pending),
                "dispatcher": {
                    "phase": self._dispatcher_phase,
                    "phase_age_ms": _age_ms(
                        self.__dict__.get("_dispatcher_phase_since"), now
                    ),
                    "queued": self._dispatch_queue.qsize(),
                },
                "inflight_received": [
                    {
                        "kind": record.kind,
                        "message": message_id[:4].hex(),
                        "age_ms": _age_ms(record.received_at or None, now),
                    }
                    for message_id, record in inflight
                ],
                "forward": {
                    "queued": self._forward_queue.qsize(),
                    "active_kind": None if active_forward is None else active_forward[0],
                    "active_age_ms": None
                    if active_forward is None
                    else _age_ms(active_forward[1], now),
                    "thread_alive": bool(
                        self._forward_thread is not None
                        and self._forward_thread.is_alive()
                    ),
                },
                "threads": {
                    "dispatcher_alive": bool(
                        self._dispatcher_thread is not None
                        and self._dispatcher_thread.is_alive()
                    ),
                    "receiver_alive": bool(
                        self._receiver_thread is not None
                        and self._receiver_thread.is_alive()
                    ),
                },
                "peers": peers,
                "failure_reports": failure_reports,
                "counters": self._counter_snapshot_locked(),
                "stall_recovery": {
                    "stall_resends": self._prefill_stall_resends,
                    "stall_failures": self._prefill_stall_failures,
                    "send_lock_stalls": self._prefill_send_lock_stalls,
                },
                "fatal_error": None
                if self._fatal_error is None
                else self._fatal_error.code,
                "last_dispatch_error": None
                if self._last_dispatch_error is None
                else {
                    "code": self._last_dispatch_error.get("code"),
                    "exception_type": self._last_dispatch_error.get("exception_type"),
                },
            }
        return state

    def _note_prefill_progress_locked(
        self, request_id: str | None, frame_bytes: int, *, confirmed: bool
    ) -> None:
        if request_id is None:
            return
        progress = self._prefill_progress.get(request_id)
        if progress is None:
            return
        progress.progress_events += 1
        progress.last_progress_at = time.monotonic()
        if confirmed:
            progress.bytes_confirmed += frame_bytes
            progress.max_frame_bytes = max(progress.max_frame_bytes, frame_bytes)

    def link_estimate(self, destination_node_id: str | None = None) -> LinkEstimate:
        """Conservative goodput / p95 RTT for one peer, or for all peers.

        Sources, never queried here (this is on the send path):

        * the activation-plane rows ``evidence()`` last projected for the peer
          - relay rows when the peer has any, else its other resolved rows;
          RTT p95 is approximated as ``warm_rtt_ms + 2 x jitter_ms``;
        * this adapter's own confirmed deliveries to the peer: frames of at
          least 16 KiB give goodput (median of bytes / confirm time; the
          confirm includes the remote dispatch, so this under-states goodput),
          frames of at most 4 KiB give RTT samples (their p95).

        The lowest goodput and the highest RTT win; a figure with no source
        takes the policy floor (4 KiB/s, 1 s).
        """

        policy = self._stall_policy
        with self._state_lock:
            peers = self._peers.snapshot()
            observations = self._path_observation_cache
            samples = {
                endpoint: tuple(values)
                for endpoint, values in self._delivery_samples.items()
            }
        if destination_node_id is None:
            bindings = list(peers.values())
        else:
            binding = peers.get(destination_node_id)
            bindings = [] if binding is None else [binding]
        goodputs: list[tuple[float, str]] = []
        rtts: list[tuple[float, str]] = []
        for binding in bindings:
            rows = [
                row
                for row in observations
                if row.get("remote_endpoint_id") == binding.endpoint_id
                and row.get("path_class") in {"relay", "direct"}
            ]
            relay_rows = [row for row in rows if row.get("path_class") == "relay"]
            for row in relay_rows or rows:
                source = f"transport_observation:{row.get('path_class')}"
                goodput = _positive_number(row.get("observed_goodput_Bps"))
                if goodput is not None:
                    goodputs.append((goodput, source))
                warm = _positive_number(row.get("warm_rtt_ms"))
                if warm is not None:
                    jitter = _positive_number(row.get("jitter_ms")) or 0.0
                    rtts.append(((warm + 2.0 * jitter) / 1000.0, source))
            confirmed = samples.get(binding.endpoint_id, ())
            large = sorted(
                size / seconds
                for size, seconds in confirmed
                if size >= _GOODPUT_SAMPLE_MIN_BYTES and seconds > 0
            )
            if large:
                # The median: one confirm that waited on a slow remote stage
                # must not pin the estimate for the next 64 frames.
                goodputs.append((large[len(large) // 2], "delivery_samples"))
            small = [
                seconds
                for size, seconds in confirmed
                if size <= _RTT_SAMPLE_MAX_BYTES and seconds > 0
            ]
            if small:
                rtts.append((_p95(small), "delivery_samples"))
        if goodputs:
            goodput, goodput_source = min(goodputs)
        else:
            goodput = float(policy.unmeasured_goodput_bytes_per_second)
            goodput_source = "unmeasured"
        if rtts:
            rtt, rtt_source = max(rtts)
        else:
            rtt = float(policy.unmeasured_rtt_seconds)
            rtt_source = "unmeasured"
        return LinkEstimate(
            goodput_bytes_per_second=goodput,
            rtt_p95_seconds=rtt,
            goodput_source=goodput_source,
            rtt_source=rtt_source,
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

    def fence_path_forwards(
        self,
        request_id: str,
        path_id: str,
        path_attempt: int,
    ) -> bool:
        """A7: fence one superseded path attempt's transport forwards.

        Called by the entry once a fenced KV-successor cutover (or its fallback)
        has durably excluded the failed attempt.  Queued deferred forwards of
        that exact attempt are dropped, a forward still blocked sending to the
        dead source is interrupted, and its pending deliveries are cancelled
        through the sidecar control lane, so the entry's request cleanup
        receipt does not wait for the original delivery deadline.  An
        interrupted forward is a fenced scope, never reported as a new failure.
        Other attempts, requests and the A7 message lane are untouched.
        """

        if (
            not isinstance(request_id, str)
            or not request_id
            or not isinstance(path_id, str)
            or not path_id
            or type(path_attempt) is not int
            or path_attempt < 0
        ):
            raise ValueError("invalid_forward_fence_scope")
        scope = _ForwardFenceScope(request_id, path_id, path_attempt)
        with self._state_lock:
            if self._router is None:
                return False
            interrupted = self._cancel_forward_scope_locked(scope)  # type: ignore[arg-type]
            self._cancel_pending_scope_locked(scope)  # type: ignore[arg-type]
        self._interrupt_client(interrupted)
        return True

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

    def _mark_delivery_cancel_attempt_started(
        self,
        message_id: bytes,
    ) -> None:
        """Publish the one piece of evidence ``_close`` needs to attribute an overrun.

        The defect ``_close`` reports as ``delivery_cancellation_shutdown_timeout`` is,
        in its own words, "a sidecar call that ignored its deadline".  Thread liveness
        alone does not establish that: a granted worker can still be alive past the
        shutdown grant having asked the sidecar nothing at all -- it may have spent the
        whole grant queueing for one of the ``_MAX_CONCURRENT_CANCELLATION_CLIENTS``
        lanes, or simply not have been scheduled.  Recording the attempt here, under the
        same lock ``_close`` reads it with, is what separates "the sidecar overran its
        deadline" (report, stay fail-closed) from "the worker had not started yet"
        (return, retain and account).  Without it a bound can only be sized to fail one
        of those two cases.

        The evidence is filed against the message id and only while this worker is the
        registered owner of it, so it cannot outlive the worker that produced it.
        """

        with self._state_lock:
            if (
                self._delivery_cancel_threads.get(message_id)
                is threading.current_thread()
            ):
                self._delivery_cancel_attempts.add(message_id)

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
                    self._mark_delivery_cancel_attempt_started(message_id)
                    outcome = self._cancel_with_client(
                        dedicated,
                        message_id,
                        # A valid ``unknown_message`` response can race just ahead
                        # of the data client's admission. Keep time for a bounded
                        # retry instead of consuming the whole cleanup authority in
                        # one attempt. Socket timeouts invalidate SidecarClient and
                        # are handled by moving to another pre-established lane.
                        timeout=min(
                            _DELIVERY_CANCEL_ATTEMPT_TIMEOUT_SECONDS, remaining
                        ),
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
                            # Reconnection is bounded by this request's owner
                            # deadline, but the pre-established client object is
                            # reusable after ``close``.  Do not permanently
                            # subtract a lane from the fixed pool merely because
                            # this owner ran out of time: a later cancellation
                            # has independent authority to reconnect the same
                            # authenticated lane inside its own deadline.
                            with self._state_lock:
                                reusable = (
                                    not self._closed
                                    and dedicated in self._cancellation_clients
                                )
                            if reusable:
                                self._available_cancellation_clients.put_nowait(
                                    dedicated
                                )
                            dedicated = None
                            remaining = deadline - time.monotonic()
                            if remaining > 0:
                                self._stop.wait(min(0.01, remaining))
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
                    # The message may still cross admission, so this worker
                    # must retry.  It must not retain one of the small bounded
                    # cancellation-client pool while waiting, though: another
                    # already-admitted message is an immediately actionable
                    # cleanup blocker.  Return the authenticated lane before
                    # the retry cadence so sibling workers can make progress
                    # inside the same immutable owner deadline.
                    self._available_cancellation_clients.put_nowait(dedicated)
                    dedicated = None
                    remaining = deadline - time.monotonic()
                    if remaining > 0:
                        self._stop.wait(min(0.01, remaining))
            else:
                # Unit adapters and pre-start teardown do not own the production
                # cancellation pool; retain the explicit control-client fallback.
                if control is not None:
                    self._mark_delivery_cancel_attempt_started(message_id)
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
                    self._delivery_cancel_attempts.discard(message_id)
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
        except SidecarError as error:
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
            self._ack_ready.clear()
            if not self._drain_ack_requests(client):
                return
            with self._state_lock:
                awaiting_dispatch_ack = bool(self._inflight_received)
            if awaiting_dispatch_ack:
                # Bounded as before; an acknowledgement queued after the drain
                # above wakes the receiver at once.
                self._ack_ready.wait(self.poll_interval_seconds)
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
                    kind="duplicate" if decoded is None else _frame_kind(decoded.message),
                    received_at=time.monotonic(),
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
                item = self._dispatch_queue.get(timeout=self._dispatch_wait_seconds())
            except Empty:
                self._service_queued_hops()
                continue
            if item is _HOP_BATCH_WAKE:
                with self._state_lock:
                    self._hop_wake_pending = False
                try:
                    self._take_over_queued_hops()
                finally:
                    self._dispatch_queue.task_done()
                continue
            (
                message_id,
                source_node_id,
                delivery_generation,
                _frame,
                digest,
                decoded,
            ) = item
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
                        self._note_peer_frame_locked(
                            self._peer_inbound, source_node_id, "duplicate", "acknowledged"
                        )
                    continue
                with self._state_lock:
                    self._dispatcher_phase = (
                        f"dispatching:{type(decoded.message).__name__}"
                    )
                if isinstance(decoded.message, FailureReport):
                    # Recorded before the dispatch acts on it (cancellation,
                    # recovery), so the reason survives whatever follows.
                    self._note_failure_report(
                        "received", source_node_id, decoded.message, "received"
                    )
                self._dispatch(decoded, source_node_id=source_node_id)
                with self._state_lock:
                    self._dispatcher_phase = "awaiting_local_ack"
                if delivery_generation != self._peers.lookup(source_node_id).generation:
                    raise IrohTransportError("peer_rotated_during_dispatch")
                self._ack_after_dispatch(message_id)
                with self._state_lock:
                    self._dispatcher_phase = "idle"
                    dispatched = self._inflight_received.pop(message_id, None)
                    self._seen[message_id] = digest
                    self._seen.move_to_end(message_id)
                    while len(self._seen) > _SEEN_LIMIT:
                        self._seen.popitem(last=False)
                    self._remote_frames_received += 1
                    self._router_frames_dispatched += 1
                    if dispatched is not None:
                        self._note_prefill_progress_locked(
                            dispatched.request_id, 0, confirmed=False
                        )
                    self._note_peer_frame_locked(
                        self._peer_inbound,
                        source_node_id,
                        _frame_kind(decoded.message),
                        "dispatched",
                    )
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
                    self._note_peer_frame_locked(
                        self._peer_inbound,
                        source_node_id,
                        "unknown"
                        if not isinstance(decoded, DecodedFrame)
                        else _frame_kind(decoded.message),
                        f"failed:{error_code}",
                    )
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
                    failed_hop = (
                        None
                        if not isinstance(decoded, DecodedFrame)
                        else decoded.message
                        if isinstance(decoded.message, HopHeader)
                        else decoded.message.header
                        if isinstance(decoded.message, ProgressivePrefillMessage)
                        else None
                    )
                    if failed_hop is not None:
                        # A progressive prefill hop is a hop: its sender (the
                        # entry for A14's chunk 0) must hear of the failure
                        # instead of waiting for its watchdog.  The hand-off
                        # to the forward worker can itself fail (a full
                        # forward queue behind stalled sends); the scoped
                        # incident above is the durable signal, and the
                        # dispatcher must survive to acknowledge this frame.
                        try:
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
                        except BaseException:
                            pass
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
                # The frame is no longer in flight: let the receiver take the next.
                self._ack_ready.set()
            if not self._stop.is_set():
                self._service_queued_hops()

    #: A7: optional ``observer(header, destination_node_id, code)`` told when a
    #: forwarded hop could not be delivered.  ``None`` (the default) changes
    #: nothing; only an A7-configured node installs one.
    forward_failure_observer = None

    def _notify_forward_failure(self, frame: bytes, destination: str, code: str) -> None:
        observer = self.forward_failure_observer
        if observer is None:
            return
        try:
            decoded = decode_frame(frame)
            if isinstance(decoded.message, HopHeader):
                observer(decoded.message, destination, code)
        except BaseException:
            # The scoped incident below remains the durable failure signal.
            pass

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
                    with self._state_lock:
                        try:
                            kind = _frame_kind(decode_frame(frame).message)
                        except WireError:
                            kind = "unknown"
                        self._active_forward = (kind, time.monotonic())
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
                    self._notify_forward_failure(frame, destination, mapped.code)
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
                    self._active_forward = None
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
        self._ack_ready.set()
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

    @staticmethod
    def _batching_router(router: Any) -> Any | None:
        """The bound router when it offers the queued runtime-batch surface."""

        names = (
            "enqueue_hop",
            "pump_ready_batches",
            "pending_batch_hops",
            "runtime_batch_cap",
            "hop_collection_window_seconds",
        )
        if all(callable(getattr(router, name, None)) for name in names):
            return router
        return None

    def _queue_hop_for_batch(self, router: Any) -> None:
        """Decide when a hop the dispatcher just queued is executed.

        The receiver takes the next frame only after this one is acknowledged,
        so concurrent hops can meet in one runtime batch only if the dispatcher
        acknowledges a queued hop before executing it.  Under a batch cap of one
        there is nothing to collect: the hop is executed here, before its frame
        is acknowledged, exactly as the synchronous path does.  Otherwise the
        dispatcher keeps collecting until the configured window closes, and
        drains at once when the batch is full or when no other active request
        is predicted to send a hop within the window (nobody left to wait for).
        """

        cap = max(1, int(router.runtime_batch_cap()))
        if (
            cap <= 1
            or router.pending_batch_hops() >= cap
            or not self._join_partners_due(router)
        ):
            self._hop_collection_deadline = None
            self._pump_hop_batches(router)
            return
        if self._hop_collection_deadline is None:
            window = max(0.0, float(router.hop_collection_window_seconds()))
            self._hop_collection_deadline = time.monotonic() + window

    @staticmethod
    def _join_partners_due(router: Any) -> bool:
        """Whether another request may still join the open collection window.

        A router without the join predictor keeps the plain bounded window.
        """

        partners = getattr(router, "hop_join_partners_due", None)
        if not callable(partners):
            return True
        return int(partners()) > 0

    def _dispatch_wait_seconds(self) -> float:
        """How long the dispatcher may block for the next frame."""

        deadline = self._hop_collection_deadline
        if deadline is None:
            return self.poll_interval_seconds
        return max(0.0, min(self.poll_interval_seconds, deadline - time.monotonic()))

    @staticmethod
    def _newly_queued(result: Any) -> bool:
        return getattr(result, "disposition", None) == "QUEUED" and not getattr(
            result, "reason", None
        )

    def _wake_dispatcher_for_hops(self) -> None:
        with self._state_lock:
            if self._hop_wake_pending:
                return
            self._hop_wake_pending = True
        try:
            self._dispatch_queue.put_nowait(_HOP_BATCH_WAKE)
        except Full:
            # The dispatcher has frames to take and services queued hops after
            # each of them, so no marker is needed.
            with self._state_lock:
                self._hop_wake_pending = False

    def _pump_hop_batches(self, router: Any) -> None:
        on_dispatcher = threading.current_thread() is self._dispatcher_thread
        if on_dispatcher:
            self._draining_hop_batches = True
        try:
            router.pump_ready_batches(on_item_error=self._hop_batch_member_failed)
        except BaseException as error:
            if self._stop.is_set():
                return
            # Member failures are scoped by the handler; anything else broke the
            # batch machinery itself, which no request-local outcome can contain.
            self._set_fatal(
                error
                if isinstance(error, IrohTransportError)
                else IrohTransportError("router_batch_drain_failed", str(error))
            )
        finally:
            if on_dispatcher:
                self._draining_hop_batches = False

    def _hop_batch_member_failed(
        self,
        header: HopHeader,
        source_node_id: str | None,
        error: BaseException,
    ) -> None:
        """Scope one batch member's completion failure to its own request.

        Mirrors the dispatcher's handling of a hop that fails synchronously: a
        request-scoped failure incident, and a failure report to the hop's sender.
        """

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
        peer_node_id = source_node_id or self.node_id
        self._record_scoped_event(
            event="failure",
            request_id=header.request_id,
            path_id=header.path_id,
            path_attempt=header.path_attempt,
            peer_node_id=peer_node_id,
            code=error_code,
        )
        if source_node_id is None:
            return
        try:
            self._send_or_dispatch(
                source_node_id,
                encode_frame(
                    FailureReport(
                        request_id=header.request_id,
                        path_id=header.path_id,
                        path_attempt=header.path_attempt,
                        token_index=header.token_index,
                        scope="PLACEMENT",
                        reason=error_code,
                        placement_id=header.destination_placement_id,
                        node_id=self.node_id,
                    )
                ),
            )
        except BaseException:
            # The scoped incident above is the durable failure signal.
            pass

    def _take_over_queued_hops(self) -> None:
        """Dispatcher side of ``_wake_dispatcher_for_hops``.

        Hops another thread queued get the same collection decision as a hop
        the dispatcher queued itself (cap, full batch, partners, window).
        """

        router = self._batching_router(self._router)
        if router is None or not router.pending_batch_hops():
            return
        self._queue_hop_for_batch(router)

    def _service_queued_hops(self) -> None:
        """Dispatcher-thread drain between frames and on idle polls.

        Collected hops are drained when their collection window closes.  Outside
        a window, queued work (a prefill the batch controller holds for its own
        collection window) is re-offered after every frame and idle poll.
        """

        router = self._batching_router(self._router)
        if router is None:
            return
        if not router.pending_batch_hops():
            self._hop_collection_deadline = None
            return
        deadline = self._hop_collection_deadline
        if (
            deadline is not None
            and time.monotonic() < deadline
            and self._join_partners_due(router)
        ):
            return
        self._hop_collection_deadline = None
        self._pump_hop_batches(router)

    def _dispatch(self, decoded: DecodedFrame, *, source_node_id: str) -> None:
        router = self._router
        if router is None:
            raise IrohTransportError("router_not_bound")
        message = decoded.message
        request_id, _path_id, _path_attempt = self._fence_scope(decoded)
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
                if (scope in self._cancellations_by_subject
                        or self._request_controlled_cancelled_locked(header.request_id)):
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
                self._path_subjects[header.path_id] = PathCancellation(
                    header.request_id, header.path_id, header.path_attempt,
                    header.topology_version,
                )
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
            batching = self._batching_router(router)
            if batching is None:
                router.receive_hop(
                    message,
                    decoded.payload,
                    source_node_id=source_node_id,
                )
                return
            queued = batching.enqueue_hop(
                message,
                decoded.payload,
                source_node_id=source_node_id,
            )
            if not self._newly_queued(queued):
                # A replay of a hop that is already queued or already done (an
                # entry re-driving its next hop, say) adds no work to drain.
                return
            if threading.current_thread() is self._dispatcher_thread:
                self._queue_hop_for_batch(batching)
            else:
                # Drains run only on the dispatcher.  Another thread (an entry's
                # request worker dispatching its own local hop, say) must never
                # end up executing and forwarding other requests' queued hops.
                self._wake_dispatcher_for_hops()
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
                cancelled = self._request_controlled_cancelled_locked(message.request_id)
                subject = PathCancellation(
                    message.request_id, message.path_id, message.path_attempt,
                    message.manifest.topology_version,
                )
                self._path_subjects[message.path_id] = subject
                if not cancelled:
                    self._path_graphs[message.path_id] = message.build.graph
                    self._participant_nodes_by_path[message.path_id] = participants
                    self._entry_nodes.setdefault(message.request_id, entry_node)
            if cancelled:
                # Router admitted this successor while owner control overtook
                # it. Retire the admitted resources, not merely the metadata.
                self.apply_controlled_path_cancellation(subject, entry_cancelled=False)
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
        if isinstance(message, VerifyEvent):
            router.receive_verify_event(message, source_node_id=source_node_id)
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
        if isinstance(message, KVCheckpointShipment):
            if len(decoded.payload) != message.byte_size:
                raise IrohTransportError("a7_shipment_size_mismatch")
            if not router.receive_kv_checkpoint_shipment(
                message, decoded.payload, source_node_id=source_node_id
            ):
                raise IrohTransportError("a7_shipment_rejected")
            return
        if isinstance(message, KVCheckpointAck):
            if not router.receive_kv_checkpoint_ack(message, source_node_id=source_node_id):
                raise IrohTransportError("a7_ack_rejected")
            return
        if isinstance(message, KVSuccessorCutover):
            if not router.receive_kv_successor_cutover(message, source_node_id=source_node_id):
                raise IrohTransportError("a7_cutover_rejected")
            return
        if isinstance(message, KVSuccessorCutoverReceipt):
            if not router.receive_kv_successor_cutover_receipt(
                message, source_node_id=source_node_id
            ):
                raise IrohTransportError("a7_cutover_receipt_rejected")
            return
        if isinstance(message, KVCheckpointCommit):
            if not router.receive_kv_checkpoint_commit(message, source_node_id=source_node_id):
                raise IrohTransportError("a7_commit_rejected")
            return
        if isinstance(message, KVShadowRelease):
            if not router.receive_kv_shadow_release(message, source_node_id=source_node_id):
                raise IrohTransportError("a7_release_rejected")
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
                self._path_subjects[header.path_id] = PathCancellation(
                    header.request_id, header.path_id, header.path_attempt,
                    header.topology_version,
                )
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
            self._path_subjects[locked.path_id] = PathCancellation(
                locked.request_id, locked.path_id, locked.path_attempt,
                locked.manifest.topology_version,
            )
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
        """Fence an authenticated owner request and retire its admitted lineage.

        The control binding names the original path, whereas an A6/A7 cutover
        can leave a successor alive under the same immutable request. Never
        derive successors from caller input, and never broaden peer data-plane
        cancellations. The owner fence also blocks later successor publication.
        """
        if not isinstance(entry_cancelled, bool):
            raise ValueError("entry_cancelled must be boolean")
        with self._state_lock:
            self._require_running()
            if self._router is None:
                return False
            entry = self._entry_nodes.get(cancellation.request_id)
            participants = self._participant_nodes_by_path.get(cancellation.path_id)
            if entry is not None and participants is not None and self.node_id not in participants:
                return False
            if entry is not None and entry != self.node_id and entry_cancelled:
                return False
            self._remember_controlled_cancellation_locked(cancellation.request_id)
            subjects = {cancellation.path_id: cancellation}
            subjects.update({path: subject for path, subject in self._path_subjects.items()
                             if subject.request_id == cancellation.request_id})
        accepted = False
        for subject in subjects.values():
            try:
                result = self._apply_controlled_path_cancellation(
                    subject, entry_cancelled=entry_cancelled,
                    cleanup_deadline_monotonic_s=cleanup_deadline_monotonic_s,
                )
            except BaseException:
                # Exact transport metadata is retired even on lower-layer
                # errors. Retain the admitted subject for owner retry, so a
                # successor failure cannot disappear from cleanup authority.
                with self._state_lock:
                    self._path_subjects[subject.path_id] = subject
                raise
            accepted = result or accepted
        return accepted

    def _apply_controlled_path_cancellation(
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

    def send_a7_message(
        self,
        destination_node_id: str,
        message: object,
        payload: bytes = b"",
    ) -> None:
        """Send one A7 control message (checkpoint, ack, cutover, receipt) to a node."""

        if not isinstance(
            message,
            (
                KVCheckpointShipment,
                KVCheckpointAck,
                KVSuccessorCutover,
                KVSuccessorCutoverReceipt,
                KVCheckpointCommit,
                KVShadowRelease,
            ),
        ):
            raise IrohTransportError("a7_message_type_invalid")
        self._send_or_dispatch(destination_node_id, encode_frame(message, payload))

    def send_token_event(self, event: TokenEvent) -> None:
        self._send_or_dispatch(
            self._entry_node(event.request_id),
            encode_frame(event),
        )

    def send_verify_event(self, event: VerifyEvent) -> None:
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
            (
                FailureReport,
                HopHeader,
                ProgressivePrefillMessage,
                # A7 control replies are produced inside inbound dispatch; never
                # wait for their remote ACK on the dispatcher thread.
                KVCheckpointShipment,
                KVCheckpointAck,
                KVSuccessorCutover,
                KVSuccessorCutoverReceipt,
                KVCheckpointCommit,
                KVShadowRelease,
            ),
        )
        if isinstance(message, ManifestLocked):
            entry_node = self._node_for_placement(
                message.build.graph,
                message.build.ordered_hops[0].placement_id,
            )
            defer_remote_forward = destination != entry_node
        if isinstance(message, (TokenEvent, VerifyEvent)):
            with self._forward_queue.all_tasks_done:
                ordered_forward_pending = self._forward_queue.unfinished_tasks > 0
            with self._state_lock:
                response_would_cycle = self._dispatcher_phase in {
                    "dispatching:HopHeader",
                    "dispatching:TokenEvent",
                    "dispatching:VerifyEvent",
                }
            # A dispatcher draining runtime batches between frames must not wait
            # for a peer's dispatch ACK either: the peer may be waiting on it.
            response_would_cycle = response_would_cycle or (
                self._draining_hop_batches
                and threading.current_thread() is self._dispatcher_thread
            )
            defer_remote_forward = ordered_forward_pending or response_would_cycle
        if (
            threading.current_thread() is self._dispatcher_thread
            and defer_remote_forward
        ):
            with self._state_lock:
                self._require_running()
                try:
                    request_id, path_id, path_attempt = self._fence_scope(decoded)
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
            self._path_subjects.pop(path_id, None)
            if not any(subject.request_id == request_id
                       for subject in self._path_subjects.values()):
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
                    with self._state_lock:
                        # The link estimate reads these rows instead of
                        # querying the sidecar on the send path.
                        self._path_observation_cache = observations
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

    def prefill_hop_diagnostic(self) -> dict[str, Any]:
        """Bounded local verdict for a stalled distributed-prefill hop.

        The prefill wait observes only ``fatal_error``, while a hop that fails
        inside the deferred forward path is recorded as a scoped, per-path,
        *non-fatal* event.  That is why a hop failure reaches the operator as
        the generic ``prefill_completion_timeout`` instead of its real cause.
        Return the transport's own local verdict so the cause stays inspectable.

        Deliberately never queries the sidecar, for the same reason
        ``counter_snapshot`` exists: this runs on the failure path under a
        bounded command budget.  Nothing address-shaped, key-shaped or payload
        bytes are returned -- only counters and a status token.
        """

        with self._state_lock:
            counts = self._counter_snapshot_locked()
            scoped_failure_code: str | None = None
            for item in reversed(self._scoped_events):
                if item.event == "failure":
                    scoped_failure_code = item.code
                    break
        return {"scoped_failure_code": scoped_failure_code, **counts}

    def close(self) -> None:
        with self._lifecycle_lock:
            self._close()

    def _close(self) -> None:
        shutdown_cleanup_deadline = (
            time.monotonic() + _DELIVERY_CANCEL_SHUTDOWN_GRANT_SECONDS
        )
        with self._state_lock:
            if self._closed:
                clients: tuple[Any | None, ...] = ()
            else:
                self._closed = True
                self._running = False
                self._stop.set()
                self._ack_ready.set()
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
                            cleanup_deadline_monotonic_s=shutdown_cleanup_deadline,
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
            delivery_cancel_threads = tuple(self._delivery_cancel_threads.items())
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
        for message_id, delivery_cancel_thread in delivery_cancel_threads:
            if delivery_cancel_thread is None:
                continue
            if delivery_cancel_thread is threading.current_thread():
                continue
            if not delivery_cancel_thread.is_alive():
                continue
            # close() is bounded by ONE shutdown grant, not by the delivery
            # budget: see _DELIVERY_CANCEL_SHUTDOWN_GRANT_SECONDS.  The worker
            # this loop waits for was either handed that same absolute deadline
            # by _close above, or owns its own (the delivery-deadline path hands
            # the aged owner deadline down).  Either way the join ends at
            # shutdown_cleanup_deadline + one handoff margin, so the worker's
            # sanction and this join share one clock and a bound merely equal to
            # an independently computed window cannot race it.  Widening this
            # join to the delivery timeout is what made close() take 10 s at a
            # 10 s delivery timeout and is measured red by
            # `test_close_or_rotation_first_blocked_cancel_has_bounded_lifecycle
            # [close]` and `test_deadline_cancel_worker_is_accounted_and_
            # close_is_bounded`.
            delivery_cancel_thread.join(
                timeout=max(
                    0.0,
                    shutdown_cleanup_deadline
                    + _DELIVERY_CANCEL_SHUTDOWN_HANDOFF_SECONDS
                    - time.monotonic(),
                )
            )
            if not delivery_cancel_thread.is_alive():
                continue
            # A worker still alive past the grant is reported ONLY when it is
            # past the sanction it was granted AND inside it -- i.e. a sidecar
            # call that ignored its deadline -- which is the defect the report
            # names.  Liveness by itself does not establish that defect, and this
            # is a bounded-close test, not a thread-count test: the worker may
            # have consumed the whole grant queueing for one of the
            # _MAX_CONCURRENT_CANCELLATION_CLIENTS lanes, or simply not have been
            # scheduled yet.  That case is a late start, not an overrun:
            # `test_close_grants_delivery_cancel_workers_their_acknowledgement_
            # window` (adversarial corpus) and
            # `test_close_retains_a_delivery_cancel_worker_that_has_not_begun_its_
            # attempt` (root file) measure it, and no join bound can serve both it
            # and the two bound tests above -- only the distinction can.
            # Returning here is the bounded, retained outcome: the worker keeps
            # ownership of the message (it is still registered and will unregister
            # itself), the pending send stays reserved but never falsely confirmed
            # cancelled, and `cancel_started` is left False by the non-confirmed
            # path so the record remains eligible for a later sweep instead of
            # being silently reported as done.  No window is widened and no
            # receipt is dropped: the accounted state is exactly what the failure
            # path already leaves behind, minus the false FATAL.
            with self._state_lock:
                attempt_started = message_id in self._delivery_cancel_attempts
            if not attempt_started:
                continue
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
            self._ack_ready.set()

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
    "DEFAULT_PREFILL_STALL_POLICY",
    "DELIVERY_SEMANTICS",
    "DeliveryReceipt",
    "IrohTransport",
    "IrohTransportError",
    "IrohTransportPort",
    "LinkEstimate",
    "PROCESS_LIFETIME_LIMITATION",
    "PeerBinding",
    "PrefillStallPolicy",
    "TransportEvidence",
]
