# SPDX-License-Identifier: AGPL-3.0-or-later
"""Production Router ``TransportPort`` over the authenticated iroh sidecar.

Router payloads remain byte-for-byte canonical ``mycelium.router_wire.v1``
frames.  No adapter envelope or simulator fallback exists on the remote path.
A confirmed receipt means the authenticated remote adapter dispatched and
ACKed the Router frame.  Dedupe and confirmation history are process-local;
simultaneous loss of both sidecars is not durable exactly-once delivery.
"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
import hashlib
from pathlib import Path
import socket
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


class IrohTransportError(RuntimeError):
   """Fail-closed adapter error with a stable code."""

   def __init__(self, code: str, detail: str = ""):
      self.code = code
      self.detail = detail
      super().__init__(code if not detail else f"{code}:{detail}")


@dataclass(frozen=True)
class PeerBinding:
   node_id: str
   endpoint_id: str
   endpoint_addr: Mapping[str, Any]
   generation: int

   def __post_init__(self) -> None:
      if not self.node_id or not self.endpoint_id:
         raise ValueError("peer node and endpoint ids must not be empty")
      if self.endpoint_addr.get("id") != self.endpoint_id:
         raise ValueError("endpoint_addr id must match endpoint_id")
      if self.generation <= 0:
         raise ValueError("peer generation must be positive")


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

      self.node_id = node_id
      self.socket_path = Path(socket_path)
      self._bootstrap_secret = bytes(bootstrap_secret)
      self._peer = peer
      self.expected_endpoint_id = expected_endpoint_id
      self.delivery_timeout_seconds = delivery_timeout_seconds
      self.poll_interval_seconds = poll_interval_seconds
      self._client_factory = client_factory
      self._send_slots = threading.BoundedSemaphore(queue_capacity)
      self._manifest_delta_capacity = queue_capacity
      self._state_lock = threading.RLock()
      self._lifecycle_lock = threading.Lock()
      self._rotation_lock = threading.Lock()
      self._router: Any | None = None
      self._send_client: Any | None = None
      self._receive_client: Any | None = None
      self._control_client: Any | None = None
      self._receiver_thread: threading.Thread | None = None
      self._stop = threading.Event()
      self._running = False
      self._closed = False
      self._fatal_error: IrohTransportError | None = None
      self._pending: dict[bytes, _PendingSend] = {}
      self._seen: OrderedDict[bytes, bytes] = OrderedDict()
      self._remote_frames_sent = 0
      self._remote_frames_received = 0
      self._router_frames_dispatched = 0
      self._duplicate_frames = 0
      self._path_graphs: dict[str, Any] = {}
      self._entry_nodes: dict[str, str] = {}
      self.manifest_deltas: list[ManifestDelta] = []

   @property
   def peer_binding(self) -> PeerBinding:
      with self._state_lock:
         return self._peer

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
      thread = self._receiver_thread
      return int(thread is not None and thread.is_alive())

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
         self._receiver_thread = threading.Thread(
            target=self._receive_loop,
            name=f"mycelium-iroh-{self.node_id}",
            daemon=True,
         )
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
         dict(binding.endpoint_addr),
         generation=binding.generation,
         timeout=timeout,
      )

   def rotate_peer(self, replacement: PeerBinding) -> None:
      with self._rotation_lock:
         self._rotate_peer(replacement)

   def _rotate_peer(self, replacement: PeerBinding) -> None:
      with self._state_lock:
         self._require_running()
         current = self._peer
         if replacement.node_id != current.node_id:
            raise IrohTransportError("peer_node_mismatch")
         if replacement.generation <= current.generation:
            raise IrohTransportError("stale_peer_generation")
         control = self._control_client
      assert control is not None
      try:
         self._configure_peer(control, replacement)
      except BaseException as error:
         raise self._map_sidecar_error("peer_rotation_failed", error) from error

      with self._state_lock:
         self._peer = replacement
         stale = [
            (message_id, pending)
            for message_id, pending in self._pending.items()
            if pending.generation < replacement.generation
         ]
         for _, pending in stale:
            pending.cancelled = True
            pending.reason = "peer_rotated"
      for message_id, _ in stale:
         self._cancel(message_id)

   def send_router_frame(
      self,
      frame: bytes,
      *,
      destination_node_id: str,
   ) -> DeliveryReceipt:
      with self._state_lock:
         self._require_running()
         peer = self._peer
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
      with self._state_lock:
         self._pending[message_id] = pending
         client = self._send_client
      cancel_timer = threading.Timer(
         self.delivery_timeout_seconds,
         self._expire_pending,
         args=(message_id, pending, "delivery_deadline_exceeded"),
      )
      cancel_timer.daemon = True
      cancel_timer.start()
      assert client is not None
      try:
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
               assert client is not None
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
         if (
            self._pending.get(message_id) is not pending
            or pending.completed
            or pending.cancel_started
         ):
            return
         pending.cancelled = True
         pending.reason = reason
         pending.cancel_started = True
         control = self._control_client
      thread = threading.Thread(
         target=self._cancel_with_client,
         args=(control, message_id),
         kwargs={"timeout": self.poll_interval_seconds},
         name=f"mycelium-iroh-cancel-{message_id.hex()}",
         daemon=True,
      )
      thread.start()

   def _cancel(self, message_id: bytes) -> None:
      with self._state_lock:
         control = self._control_client
      self._cancel_with_client(control, message_id)

   @staticmethod
   def _cancel_with_client(
      control: Any | None,
      message_id: bytes,
      *,
      timeout: float | None = None,
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
         try:
            delivery = self._recv(client)
         except TimeoutError:
            continue
         except BaseException as error:
            if self._stop.is_set():
               return
            if self._reconnectable(error):
               try:
                  self._reconnect_receive_client()
                  continue
               except BaseException as reconnect_error:
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
            if previous is not None:
               if previous != digest:
                  self._set_fatal(IrohTransportError("replay_collision"))
                  return
               self._duplicate_frames += 1
               duplicate = True
            else:
               duplicate = False
         if duplicate:
            try:
               client.ack(message_id)
            except BaseException as error:
               self._set_fatal(self._map_sidecar_error("ack_failed", error))
               return
            continue
         try:
            decoded = decode_frame(frame)
         except WireError as error:
            self._set_fatal(
               IrohTransportError("malformed_router_frame", error.code)
            )
            return
         try:
            self._dispatch(decoded, source_node_id=self.peer_binding.node_id)
         except BaseException as error:
            self._set_fatal(
               error
               if isinstance(error, IrohTransportError)
               else IrohTransportError("router_dispatch_failed", str(error))
            )
            return
         with self._state_lock:
            if delivery_generation != self._peer.generation:
               self._set_fatal(IrohTransportError("peer_rotated_during_dispatch"))
               return
         try:
            client.ack(message_id)
         except BaseException as error:
            self._set_fatal(self._map_sidecar_error("ack_failed", error))
            return
         with self._state_lock:
            self._seen[message_id] = digest
            self._seen.move_to_end(message_id)
            while len(self._seen) > _SEEN_LIMIT:
               self._seen.popitem(last=False)
            self._remote_frames_received += 1
            self._router_frames_dispatched += 1

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
         accepted = router.register_path(
            message.build.request,
            message.manifest,
            message.build.graph,
            source_node_id=source_node_id,
            entry_node_id=entry_node,
         )
         if not accepted:
            raise IrohTransportError("manifest_registration_rejected")
         self._path_graphs[message.path_id] = message.build.graph
         self._entry_nodes.setdefault(message.request_id, entry_node)
         if entry_node == self.node_id:
            router.receive_manifest_locked(
               message,
               source_node_id=source_node_id,
            )
         return
      if isinstance(message, ManifestDelta):
         with self._state_lock:
            if len(self.manifest_deltas) >= self._manifest_delta_capacity:
               raise IrohTransportError("manifest_delta_queue_full")
            self.manifest_deltas.append(message)
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
         self._entry_nodes.setdefault(header.request_id, self.node_id)
         self._path_graphs[header.path_id] = graph
         frame = encode_progressive_prefill(header, payload)
      else:
         if not isinstance(payload, bytes):
            raise IrohTransportError("hop_payload_must_be_bytes")
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
      self._path_graphs[locked.path_id] = graph
      destinations = {
         self._node_for_placement(graph, hop.placement_id)
         for hop in locked.manifest.ordered_hops
      }
      entry = self._entry_nodes.get(locked.request_id)
      if entry is not None:
         destinations.add(entry)
      for destination in sorted(destinations):
         self._send_or_dispatch(destination, encode_frame(locked))

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
      with self._state_lock:
         self._require_running()
      if destination == self.node_id:
         self._dispatch(decode_frame(frame), source_node_id=self.node_id)
         return
      self.send_router_frame(frame, destination_node_id=destination)

   def _entry_node(self, request_id: str) -> str:
      node_id = self._entry_nodes.get(request_id)
      if node_id is None:
         raise IrohTransportError("unknown_entry_node", request_id)
      return node_id

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
            pending_ids: tuple[bytes, ...] = ()
            clients: tuple[Any | None, ...] = ()
            control = None
         else:
            self._closed = True
            self._running = False
            self._stop.set()
            for pending in self._pending.values():
               pending.cancelled = True
               pending.reason = "transport_closed"
               pending.cancel_started = True
            pending_ids = tuple(self._pending)
            clients = (
               self._receive_client,
               self._send_client,
               self._control_client,
            )
            control = self._control_client
            self._receive_client = None
            self._send_client = None
            self._control_client = None
         thread = self._receiver_thread
      for message_id in pending_ids:
         self._cancel_with_client(control, message_id)
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
      if thread is threading.current_thread():
         return
      if thread is not None:
         thread.join(timeout=max(1.0, self.poll_interval_seconds * 4))
         if thread.is_alive():
            error = IrohTransportError("receiver_shutdown_timeout")
            self._set_fatal(error)
            raise error
      with self._state_lock:
         if self._receiver_thread is thread:
            self._receiver_thread = None

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
