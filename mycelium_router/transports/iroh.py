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
from typing import Any, Callable, Mapping
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
   if (
      type(token_index) is int
      and -(2**63) <= token_index < 2**63
   ):
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
            "node_ids": [
               placement.node_id for placement in stage.placements
            ],
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
      if type(self.generation) is not int or isinstance(self.generation, bool) or self.generation <= 0:
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

   Replaces the single-peer ``_peer`` field when the sidecar is evolved to
   support an explicitly routed N-node graph.  All mutations are atomic and
   generation-fenced: a stale-generation upsert or replacement is rejected
   before any internal state changes.
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

   def atomic_replace(self, bindings: list[PeerBinding]) -> None:
      canonical = [_canonical_peer_binding(b) for b in bindings]
      node_ids = [b.node_id for b in canonical]
      if len(set(node_ids)) != len(node_ids):
         raise ValueError("duplicate_node_id")
      with self._lock:
         for binding in canonical:
            existing = self._bindings.get(binding.node_id)
            if existing is not None and binding.generation <= existing.generation:
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
   route_ready: bool = False
   delivery_semantics: str = DELIVERY_SEMANTICS
   process_lifetime_limitation: str = PROCESS_LIFETIME_LIMITATION


@dataclass
class _PendingSend:
   generation: int
   cancelled: bool = False
   reason: str = ""
   cancel_started: bool = False
   completed: bool = False


@dataclass
class _AckRequest:
   message_id: bytes
   completed: threading.Event
   error: BaseException | None = None


ClientFactory = Callable[..., Any]


class IrohTransport:
   """Point-to-point authenticated iroh adapter implementing ``TransportPort``.

   The current native v1 sidecar has one authenticated peer binding.  This
   adapter therefore fails closed on any destination other than that exact
   Mycelium node/EndpointID/generation tuple.
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
      self.local_generation = selected_local_generation
      self.expected_endpoint_id = expected_endpoint_id
      self.delivery_timeout_seconds = delivery_timeout_seconds
      self.poll_interval_seconds = poll_interval_seconds
      self._client_factory = client_factory
      self._send_slots = threading.BoundedSemaphore(queue_capacity)
      self._cancellation_slots = threading.BoundedSemaphore(queue_capacity)
      self._manifest_delta_capacity = queue_capacity
      self._state_lock = threading.RLock()
      self._receipt_trace_condition = threading.Condition(self._state_lock)
      self._lifecycle_lock = threading.Lock()
      self._rotation_lock = threading.Lock()
      self._router: Any | None = None
      self._send_client: Any | None = None
      self._receive_client: Any | None = None
      self._control_client: Any | None = None
      self._receiver_thread: threading.Thread | None = None
      self._dispatcher_thread: threading.Thread | None = None
      self._dispatch_queue: Queue[
         tuple[bytes, int, bytes, bytes, DecodedFrame | None]
      ] = Queue(maxsize=queue_capacity)
      self._ack_queue: Queue[_AckRequest] = Queue(maxsize=queue_capacity)
      self._cancellation_threads: dict[str, threading.Thread] = {}
      self._delivery_cancel_threads: dict[bytes, threading.Thread] = {}
      self._last_cancellation: PathCancellation | None = None
      self._stop = threading.Event()
      self._running = False
      self._closed = False
      self._fatal_error: IrohTransportError | None = None
      self._pending: dict[bytes, _PendingSend] = {}
      self._seen: OrderedDict[bytes, bytes] = OrderedDict()
      self._inflight_received: dict[bytes, bytes] = {}
      self._dispatcher_phase = "idle"
      self._outbound_trace: deque[str] = deque(maxlen=256)
      self._inflight_receipt_trace_commits = 0
      self._remote_frames_sent = 0
      self._remote_frames_received = 0
      self._router_frames_dispatched = 0
      self._duplicate_frames = 0
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
            *self._cancellation_threads.values(),
            *self._delivery_cancel_threads.values(),
         )
      return sum(
         int(thread is not None and thread.is_alive()) for thread in threads
      )

   @property
   def pending_delivery_count(self) -> int:
      with self._state_lock:
         return len(self._pending)

   def cancellation_cleanup_complete(self, request_id: str, path_id: str) -> bool:
      """Return a lock-coherent local cleanup observation for one cancelled path."""
      with self._state_lock:
         state_clean = (
            not self._pending
            and not self._inflight_received
            and path_id not in self._path_graphs
            and path_id not in self._participant_nodes_by_path
            and request_id not in self._entry_nodes
            and path_id not in self._cancellation_threads
         )
      with self._dispatch_queue.all_tasks_done:
         dispatch_clean = self._dispatch_queue.unfinished_tasks == 0
      with self._ack_queue.all_tasks_done:
         ack_clean = self._ack_queue.unfinished_tasks == 0
      return state_clean and dispatch_clean and ack_clean

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

   @property
   def dispatcher_phase(self) -> str:
      with self._state_lock:
         return self._dispatcher_phase

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

      clients = [self._new_client() for _ in range(3)]
      try:
         for client in clients:
            client.connect()
            actual = client.endpoint_id
            if actual != self.expected_endpoint_id:
               raise IrohTransportError(
                  "local_endpoint_mismatch",
                  f"expected={self.expected_endpoint_id},actual={actual}",
               )
         self._configure_peer(clients[2], self._peer)
      except BaseException:
         for client in clients:
            try:
               client.close()
            except BaseException:
               pass
         raise

      with self._state_lock:
         self._send_client, self._receive_client, self._control_client = clients
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
      if control is None:
         raise IrohTransportError("transport_control_unavailable")
      try:
         self._configure_peer(control, candidate)
      except BaseException as error:
         raise self._map_sidecar_error("peer_rotation_failed", error) from error

      with self._state_lock:
         self._require_running()
         if self._control_client is not control:
            raise IrohTransportError("transport_control_changed")
         try:
            refreshed_snapshot = _peer_binding_snapshot(self._peer)
         except ValueError:
            raise IrohTransportError("peer_rotated") from None
         if refreshed_snapshot != current_snapshot:
            raise IrohTransportError("peer_rotated")
         self._peer = candidate
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
               )

   def send_router_frame(
      self,
      frame: bytes,
      *,
      destination_node_id: str,
      _trace_peer_binding: PeerBinding | None = None,
   ) -> DeliveryReceipt:
      with self._state_lock:
         self._require_running()
         peer = self._peer
         if _trace_peer_binding is not None and peer != _trace_peer_binding:
            raise IrohTransportError("peer_rotated")
      if destination_node_id != peer.node_id:
         raise IrohTransportError("destination_binding_mismatch")
      try:
         decode_frame(frame)
      except WireError as error:
         raise IrohTransportError(
            "malformed_router_frame",
            error.code,
         ) from error
      if not self._send_slots.acquire(blocking=False):
         raise IrohTransportError("adapter_queue_full")

      message_id = uuid.uuid4().bytes
      deadline = time.monotonic() + self.delivery_timeout_seconds
      pending = _PendingSend(peer.generation)
      cancel_timer: threading.Timer | None = None
      try:
         with self._state_lock:
            self._require_running()
            peer = self._peer
            if _trace_peer_binding is not None and peer != _trace_peer_binding:
               raise IrohTransportError("peer_rotated")
            if destination_node_id != peer.node_id:
               raise IrohTransportError("destination_binding_mismatch")
            pending = _PendingSend(peer.generation)
            client = self._send_client
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
         self._send_confirmed(
            client,
            frame,
            message_id,
            expected_generation=pending.generation,
            timeout=self._remaining(deadline),
         )
         with self._state_lock:
            current = self._peer
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
            raise IrohTransportError(pending.reason or "delivery_cancelled") from error
         if self._reconnectable(error):
            try:
               self._reconnect_send_client(deadline=deadline)
               with self._state_lock:
                  current = self._peer
                  if pending.cancelled or current.generation != pending.generation:
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
               )
               with self._state_lock:
                  current = self._peer
                  if pending.cancelled or current.generation != pending.generation:
                     raise IrohTransportError(
                        pending.reason or "peer_rotated"
                     )
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
               raise IrohTransportError("delivery_deadline_exceeded") from retry_error
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
            self._pending.pop(message_id, None)
         self._send_slots.release()

   def _send_confirmed(
      self,
      client: Any,
      frame: bytes,
      message_id: bytes,
      *,
      expected_generation: int,
      timeout: float,
   ) -> None:
      client.send_confirmed(
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
         self._configure_peer(
            replacement,
            self.peer_binding,
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
            self._start_delivery_cancel_locked(control, message_id)

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

   def _start_delivery_cancel_locked(
      self,
      control: Any | None,
      message_id: bytes,
   ) -> None:
      if message_id in self._delivery_cancel_threads:
         raise IrohTransportError("delivery_cancel_worker_collision")
      thread = threading.Thread(
         target=self._delivery_cancel_worker,
         args=(control, message_id),
         name=f"mycelium-iroh-cancel-{message_id.hex()}",
         daemon=True,
      )
      try:
         thread.start()
      except BaseException:
         thread = None
         raise
      self._delivery_cancel_threads[message_id] = thread

   def _delivery_cancel_worker(
      self,
      control: Any | None,
      message_id: bytes,
   ) -> None:
      try:
         self._cancel_with_client(
            control,
            message_id,
            timeout=min(
               self.poll_interval_seconds,
               self.delivery_timeout_seconds,
            ),
         )
      finally:
         current = threading.current_thread()
         with self._state_lock:
            if self._delivery_cancel_threads.get(message_id) is current:
               self._delivery_cancel_threads.pop(message_id, None)

   @staticmethod
   def _cancel_with_client(
      control: Any | None,
      message_id: bytes,
      *,
      timeout: float,
   ) -> None:
      if control is None:
         return
      try:
         control.cancel(message_id, timeout=timeout)
      except BaseException:
         pass

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
            code = "sequence_gap" if (
               isinstance(error, (ProtocolError, AuthenticationError))
               and error.code in {"sequence_gap", "invalid_sequence"}
            ) else "sidecar_receive_failed"
            self._set_fatal(IrohTransportError(code, str(error)))
            return
         if delivery is None:
            continue
         message_id, delivery_generation, frame = delivery
         digest = hashlib.sha256(frame).digest()
         with self._state_lock:
            if delivery_generation != self._peer.generation:
               self._set_fatal(IrohTransportError("peer_rotated"))
               return
            previous = self._seen.get(message_id)
            inflight = self._inflight_received.get(message_id)
            known_digest = previous if previous is not None else inflight
            if known_digest is not None and known_digest != digest:
               self._set_fatal(IrohTransportError("replay_collision"))
               return
            duplicate = known_digest is not None
            self._inflight_received[message_id] = digest
         if duplicate:
            decoded = None
         else:
            try:
               decoded = decode_frame(frame)
            except WireError as error:
               with self._state_lock:
                  self._inflight_received.pop(message_id, None)
               self._set_fatal(
                  IrohTransportError("malformed_router_frame", error.code)
               )
               return
         try:
            self._dispatch_queue.put_nowait(
               (message_id, delivery_generation, frame, digest, decoded)
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
               delivery_generation,
               _frame,
               digest,
               decoded,
            ) = self._dispatch_queue.get(timeout=self.poll_interval_seconds)
         except Empty:
            continue
         try:
            with self._state_lock:
               current_generation = self._peer.generation
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
               self._dispatcher_phase = f"dispatching:{type(decoded).__name__}"
            self._dispatch(decoded, source_node_id=self.peer_binding.node_id)
            with self._state_lock:
               self._dispatcher_phase = "awaiting_local_ack"
               if delivery_generation != self._peer.generation:
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
               self._inflight_received.pop(message_id, None)
            self._set_fatal(
               error
               if isinstance(error, IrohTransportError)
               else IrohTransportError("router_dispatch_failed", str(error))
            )
            return
         finally:
            self._dispatch_queue.task_done()

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

   def _recv(self, client: Any) -> tuple[bytes, int, bytes] | None:
      receive = client.recv_with_generation
      try:
         return receive(wait_seconds=self.poll_interval_seconds)
      except TypeError as error:
         if "wait_seconds" not in str(error):
            raise
         return receive(timeout=self.poll_interval_seconds)

   def _reconnect_receive_client(self) -> None:
      replacement = self._new_client()
      try:
         replacement.connect()
         if replacement.endpoint_id != self.expected_endpoint_id:
            raise IrohTransportError("local_endpoint_mismatch_after_reconnect")
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
            previous = self._receive_client
            self._receive_client = replacement
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

   def _dispatch(self, decoded: DecodedFrame, *, source_node_id: str) -> None:
      router = self._router
      if router is None:
         raise IrohTransportError("router_not_bound")
      message = decoded.message
      if isinstance(message, ProgressivePrefillMessage):
         frame = encode_frame(message, decoded.payload)
         header, context = decode_progressive_prefill(frame)
         with self._state_lock:
            entry_node_id = self._entry_nodes.setdefault(
               header.request_id,
               source_node_id,
            )
            self._path_graphs[header.path_id] = context.graph
            self._participant_nodes_by_path[header.path_id] = frozenset(
               self._node_for_placement(context.graph, hop.placement_id)
               for hop in context.build.ordered_hops
            )
         router.receive_progressive_prefill(
            header,
            context,
            source_node_id=source_node_id,
            entry_node_id=entry_node_id,
         )
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
            accepted = router.register_path(
               message.build.request,
               message.manifest,
               message.build.graph,
               source_node_id=source_node_id,
               entry_node_id=entry_node,
            )
         if not accepted:
            raise IrohTransportError("manifest_registration_rejected")
         participants = frozenset(
            self._node_for_placement(message.build.graph, hop.placement_id)
            for hop in message.manifest.ordered_hops
         )
         with self._state_lock:
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
         entry_node = self._entry_nodes.get(cancellation.request_id)
         participants = self._participant_nodes_by_path.get(cancellation.path_id)
         peer_node = self._peer.node_id
      if entry_node != self.node_id:
         raise IrohTransportError("path_cancellation_source_not_entry")
      if participants is None:
         raise IrohTransportError("unknown_path", cancellation.path_id)
      if participants - {self.node_id, peer_node}:
         raise IrohTransportError("path_cancellation_participant_unbound")
      frame = encode_frame(cancellation)
      if peer_node not in participants:
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
               args=(cancellation, peer_node, frame),
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

   def _deliver_path_cancellation(
      self,
      cancellation: PathCancellation,
      peer_node: str,
      frame: bytes,
   ) -> None:
      delivered = False
      try:
         self._send_or_dispatch(peer_node, frame)
         delivered = True
      except BaseException as error:
         if not self._stop.is_set():
            self._set_fatal(
               self._map_sidecar_error("path_cancellation_delivery_failed", error)
            )
      finally:
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

   def _send_or_dispatch(self, destination: str, frame: bytes) -> None:
      decoded = decode_frame(frame)
      remote = destination != self.node_id
      trace_prefix = (
         f"{type(decoded.message).__name__}->"
         f"{'peer:remote' if remote else 'self:local'}:"
      )
      with self._state_lock:
         self._require_running()
      if destination == self.node_id:
         identity_budget = _TRACE_ENTRY_BYTES - len(
            trace_prefix.encode("utf-8")
         )
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

      with self._receipt_trace_condition:
         self._require_running()
         trace_peer = self._peer
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
      identity_budget = _TRACE_ENTRY_BYTES - len(
         trace_prefix.encode("utf-8")
      )
      trace = trace_prefix + _bounded_trace_identity(
         message,
         max_bytes=identity_budget,
         delivery_message_id=receipt.message_id,
      )
      receipt_prefix = "DeliveryReceipt->peer:remote:"
      receipt_trace = receipt_prefix + _bounded_delivery_receipt_identity(
         receipt,
         max_bytes=(
            _TRACE_ENTRY_BYTES - len(receipt_prefix.encode("utf-8"))
         ),
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

   def evidence(self) -> TransportEvidence:
      with self._state_lock:
         peer = self._peer
         endpoint_id = (
            self._send_client.endpoint_id
            if self._send_client is not None
            else self.expected_endpoint_id
         )
         return TransportEvidence(
            local_node_id=self.node_id,
            local_endpoint_id=endpoint_id,
            peer_node_id=peer.node_id,
            peer_endpoint_id=peer.endpoint_id,
            peer_generation=peer.generation,
            remote_frames_sent=self._remote_frames_sent,
            remote_frames_received=self._remote_frames_received,
            router_frames_dispatched=self._router_frames_dispatched,
            duplicate_frames=self._duplicate_frames,
         )

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
                  )
            clients = (
               self._receive_client,
               self._send_client,
               self._control_client,
            )
            self._receive_client = None
            self._send_client = None
            self._control_client = None
         thread = self._receiver_thread
         dispatcher_thread = self._dispatcher_thread
         cancellation_threads = tuple(self._cancellation_threads.values())
         delivery_cancel_threads = tuple(
            self._delivery_cancel_threads.values()
         )
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
      receipt_trace_deadline = (
         time.monotonic() + self.delivery_timeout_seconds
      )
      with self._receipt_trace_condition:
         while self._inflight_receipt_trace_commits:
            remaining = receipt_trace_deadline - time.monotonic()
            if remaining <= 0:
               raise IrohTransportError(
                  "receipt_trace_commit_shutdown_timeout"
               )
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
      for cancellation_thread in cancellation_threads:
         if cancellation_thread is threading.current_thread():
            continue
         cancellation_thread.join(
            timeout=max(1.0, self.delivery_timeout_seconds)
         )
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
            error = IrohTransportError(
               "delivery_cancellation_shutdown_timeout"
            )
            self._set_fatal(error)
            raise error
      with self._state_lock:
         if self._receiver_thread is thread:
            self._receiver_thread = None
         if self._dispatcher_thread is dispatcher_thread:
            self._dispatcher_thread = None

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
