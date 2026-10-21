# SPDX-License-Identifier: AGPL-3.0-only
"""Local-only TCP mesh exercising production Router wire frames.

This adapter binds exclusively to loopback. It is an MVP integration harness, not an
authenticated multi-host transport.
"""

from dataclasses import dataclass
import socket
import socketserver
import struct
import threading
import time

from mycelium_router import instrument
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
   VerifyEvent,
)
from mycelium_router.rtt_measurement import (
   SOURCE_SOCKET_ROUND_TRIP,
   MeasuredRttTable,
)
from mycelium_router.wire import (
   decode_frame,
   decode_progressive_prefill,
   encode_frame,
   encode_progressive_prefill,
)


_ACTION_HOP = 1
_ACTION_REGISTER_LOCK = 2
_ACTION_ENTRY_LOCK = 3
_ACTION_MANIFEST_DELTA = 4
_ACTION_TOKEN_EVENT = 5
_ACTION_FAILURE_REPORT = 6
_ACTION_PREFILL_CHUNK_COMPLETED = 7
_ACTION_PATH_CANCELLATION = 8
_ACTION_VERIFY_EVENT = 9
_MAX_PACKET_BYTES = 285_212_672
_PACKET_LENGTH = struct.Struct(">I")
_RESPONSE = struct.Struct(">BI")
_DELIVERY_ID = struct.Struct(">Q")

#: Hop ``stage_id`` per delivery action, so an emitted hop names the operation
#: that actually crossed the socket.
_ACTION_STAGE_NAMES = {
   _ACTION_HOP: "hop",
   _ACTION_REGISTER_LOCK: "register_lock",
   _ACTION_ENTRY_LOCK: "entry_lock",
   _ACTION_MANIFEST_DELTA: "manifest_delta",
   _ACTION_TOKEN_EVENT: "token_event",
   _ACTION_FAILURE_REPORT: "failure_report",
   _ACTION_PREFILL_CHUNK_COMPLETED: "prefill_chunk_completed",
   _ACTION_PATH_CANCELLATION: "path_cancellation",
}


@dataclass(frozen=True)
class _OutboundSpan:
   """The serialize span and hop identity measured by the caller of ``_send``.

   ``encode_frame`` runs in the transport method, not in ``_send``, so the
   caller brackets it and hands the span down.  ``None`` timestamps mean the
   caller did not measure that span and the hop record says so.
   """

   request_id: str | None
   serialize_start_at: float | None
   serialize_end_at: float | None


def _encode_with_span(encode, *message, request_id: str | None) -> tuple[bytes, _OutboundSpan]:
   """Encode one wire frame and bracket the encode with monotonic timestamps."""

   start = time.monotonic()
   frame = encode(*message)
   return frame, _OutboundSpan(
      request_id=request_id,
      serialize_start_at=start,
      serialize_end_at=time.monotonic(),
   )



class SocketTransportError(RuntimeError):
   pass


class _ThreadingServer(socketserver.ThreadingTCPServer):
   allow_reuse_address = True
   daemon_threads = True


@dataclass(frozen=True)
class _Endpoint:
   host: str
   port: int


@dataclass
class _PendingDelivery:
   completed: threading.Event
   source_node_id: str
   error: str = ""


class LoopbackSocketTransport:
   def __init__(self, mesh: "LoopbackSocketMesh", source_node_id: str):
      self.mesh = mesh
      self.source_node_id = source_node_id

   def remember_entry(self, request_id: str, node_id: str) -> None:
      if node_id != self.source_node_id:
         raise SocketTransportError("request_entry_transport_mismatch")
      self.mesh.remember_entry(request_id, node_id)

   def send_hop(self, header: HopHeader, payload: object) -> None:
      self.mesh._send_hop(self.source_node_id, header, payload)

   def send_manifest_delta(self, delta: ManifestDelta) -> None:
      frame, span = _encode_with_span(
         encode_frame, delta, request_id=delta.request_id
      )
      self.mesh._send_to_entry(
         self.source_node_id,
         delta.request_id,
         _ACTION_MANIFEST_DELTA,
         frame,
         span=span,
      )

   def send_manifest_locked(self, locked: ManifestLocked) -> None:
      self.mesh._send_manifest_locked(self.source_node_id, locked)

   def send_path_cancellation(self, cancellation: PathCancellation) -> None:
      self.mesh._send_path_cancellation(self.source_node_id, cancellation)

   def send_failure_report(self, report: FailureReport) -> None:
      frame, span = _encode_with_span(
         encode_frame, report, request_id=report.request_id
      )
      self.mesh._send_to_entry(
         self.source_node_id,
         report.request_id,
         _ACTION_FAILURE_REPORT,
         frame,
         span=span,
      )

   def send_token_event(self, event: TokenEvent) -> None:
      frame, span = _encode_with_span(
         encode_frame, event, request_id=event.request_id
      )
      self.mesh._send_to_entry(
         self.source_node_id,
         event.request_id,
         _ACTION_TOKEN_EVENT,
         frame,
         span=span,
      )

   def send_verify_event(self, event: VerifyEvent) -> None:
      frame, span = _encode_with_span(
         encode_frame, event, request_id=event.request_id
      )
      self.mesh._send_to_entry(
         self.source_node_id,
         event.request_id,
         _ACTION_VERIFY_EVENT,
         frame,
         span=span,
      )

   def send_prefill_chunk_completed(
      self,
      event: PrefillChunkCompleted,
   ) -> None:
      frame, span = _encode_with_span(
         encode_frame, event, request_id=event.request_id
      )
      self.mesh._send_to_entry(
         self.source_node_id,
         event.request_id,
         _ACTION_PREFILL_CHUNK_COMPLETED,
         frame,
         span=span,
      )


class LoopbackSocketMesh:
   def __init__(
      self,
      *,
      connect_timeout_seconds: float = 5.0,
      rtt_table: MeasuredRttTable | None = None,
      hop_recorder: "instrument.HopRecorder | None" = None,
   ):
      """A loopback TCP mesh, optionally filing live link telemetry.

      ``rtt_table`` receives one measured round trip per delivery, keyed by the
      directed node pair — this is the continuous, traffic-driven RTT probe.
      ``hop_recorder`` receives one hop per delivery with true socket spans.
      Both default to ``None`` (no telemetry, no behaviour change).
      """

      if rtt_table is not None and not isinstance(rtt_table, MeasuredRttTable):
         raise ValueError("rtt_table must be a MeasuredRttTable")
      if hop_recorder is not None and not isinstance(
         hop_recorder, instrument.HopRecorder
      ):
         raise ValueError("hop_recorder must be a HopRecorder")
      self.rtt_table = rtt_table
      self.hop_recorder = hop_recorder

      self.connect_timeout_seconds = connect_timeout_seconds
      self.routers: dict[str, object] = {}
      self._transports: dict[str, LoopbackSocketTransport] = {}
      self._servers: dict[str, _ThreadingServer] = {}
      self._threads: dict[str, threading.Thread] = {}
      self._endpoints: dict[str, _Endpoint] = {}
      self._entry_nodes: dict[str, str] = {}
      self._path_graphs: dict[str, object] = {}
      self._participant_nodes_by_path: dict[str, frozenset[str]] = {}
      self.frames: list[bytes] = []
      self.manifest_deltas: list[ManifestDelta] = []
      self.connection_count = 0
      self.active_connection_count = 0
      self.maximum_active_connections = 0
      self._next_delivery_id = 1
      self._pending_deliveries: dict[int, _PendingDelivery] = {}
      self._dispatch_context = threading.local()
      self._lock = threading.Lock()
      self._started = False

   def transport_for(self, node_id: str) -> LoopbackSocketTransport:
      transport = self._transports.get(node_id)
      if transport is None:
         transport = LoopbackSocketTransport(self, node_id)
         self._transports[node_id] = transport
      return transport

   def register_router(self, node_id: str, router: object) -> None:
      if self._started:
         raise SocketTransportError("cannot_register_after_start")
      if node_id in self.routers:
         raise SocketTransportError(f"duplicate_router:{node_id}")
      self.routers[node_id] = router

   def remember_entry(self, request_id: str, node_id: str) -> None:
      if node_id not in self.routers:
         raise SocketTransportError(f"unknown_entry_node:{node_id}")
      with self._lock:
         existing = self._entry_nodes.setdefault(request_id, node_id)
         if existing != node_id:
            raise SocketTransportError("request_entry_conflict")

   def start(self) -> None:
      if self._started:
         return
      if not self.routers:
         raise SocketTransportError("no_routers_registered")
      for node_id in sorted(self.routers):
         server = _ThreadingServer(
            ("127.0.0.1", 0),
            self._handler_for(node_id),
         )
         host, port = server.server_address
         self._servers[node_id] = server
         self._endpoints[node_id] = _Endpoint(host, port)
         thread = threading.Thread(
            target=server.serve_forever,
            name=f"mycelium-router-{node_id}",
            daemon=True,
         )
         self._threads[node_id] = thread
         thread.start()
      self._started = True

   def close(self) -> None:
      with self._lock:
         pending = tuple(self._pending_deliveries.values())
         self._pending_deliveries.clear()
      for delivery in pending:
         delivery.error = "socket_mesh_closed"
         delivery.completed.set()
      for server in self._servers.values():
         server.shutdown()
      for server in self._servers.values():
         server.server_close()
      for thread in self._threads.values():
         thread.join(timeout=2.0)
      self._servers.clear()
      self._threads.clear()
      self._endpoints.clear()
      self._started = False

   def endpoints(self) -> dict[str, tuple[str, int]]:
      return {
         node_id: (endpoint.host, endpoint.port)
         for node_id, endpoint in self._endpoints.items()
      }

   def bound_hosts(self) -> set[str]:
      return {endpoint.host for endpoint in self._endpoints.values()}

   def _handler_for(self, node_id: str):
      mesh = self

      class Handler(socketserver.BaseRequestHandler):
         def handle(self) -> None:
            mesh._handle_connection(node_id, self.request)

      return Handler

   def _handle_connection(self, node_id: str, connection: socket.socket) -> None:
      delivery_id: int | None = None
      try:
         packet_length = _PACKET_LENGTH.unpack(self._recv_exact(connection, 4))[0]
         minimum_length = 1 + _DELIVERY_ID.size + 1
         if packet_length < minimum_length or packet_length > _MAX_PACKET_BYTES:
            raise SocketTransportError("invalid_packet_length")
         packet = self._recv_exact(connection, packet_length)
         action = packet[0]
         delivery_id = _DELIVERY_ID.unpack(
            packet[1 : 1 + _DELIVERY_ID.size]
         )[0]
         frame = packet[1 + _DELIVERY_ID.size :]
         if delivery_id is None:
            raise SocketTransportError("missing_delivery_id")
         with self._lock:
            pending = self._pending_deliveries.get(delivery_id)
            if pending is None:
               raise SocketTransportError("unknown_delivery_id")
            source_node_id = pending.source_node_id
            if source_node_id not in self.routers:
               raise SocketTransportError("unknown_source_node")
            self.frames.append(frame)
            self.connection_count += 1
      except (
         OSError,
         ValueError,
         RuntimeError,
         TypeError,
         KeyError,
         AttributeError,
         struct.error,
      ) as error:
         detail = f"{type(error).__name__}:{error}".encode("utf-8")[:4096]
         try:
            connection.sendall(_RESPONSE.pack(1, len(detail)) + detail)
         except OSError:
            pass
         if delivery_id is not None:
            self._complete_delivery(delivery_id, detail.decode("utf-8"))
         return

      # This is a same-process TCP proof harness. Acknowledge byte receipt and close
      # the network descriptor before dispatch. The sender then waits on the
      # in-memory delivery event below, preserving synchronous execution/error
      # semantics without recursively retaining one socket per prefill chunk.
      try:
         connection.sendall(_RESPONSE.pack(0, 0))
         connection.shutdown(socket.SHUT_RDWR)
      except OSError:
         pass
      finally:
         connection.close()

      error_detail = ""
      self._dispatch_context.source_node_id = source_node_id
      try:
         self._dispatch(node_id, action, frame)
      except (
         OSError,
         ValueError,
         RuntimeError,
         TypeError,
         KeyError,
         AttributeError,
         struct.error,
      ) as error:
         error_detail = f"{type(error).__name__}:{error}"[:4096]
      finally:
         del self._dispatch_context.source_node_id
      self._complete_delivery(delivery_id, error_detail)

   def _dispatch(self, node_id: str, action: int, frame: bytes) -> None:
      router = self.routers[node_id]
      decoded = decode_frame(frame)
      message = decoded.message
      if action == _ACTION_HOP:
         if isinstance(message, ProgressivePrefillMessage):
            header, context = decode_progressive_prefill(frame)
            router.receive_progressive_prefill(
               header,
               context,
               source_node_id=getattr(
                  self._dispatch_context,
                  "source_node_id",
                  None,
               ),
               entry_node_id=self._entry_node(header.request_id),
            )
            return
         if not isinstance(message, HopHeader):
            raise SocketTransportError("invalid_hop_message")
         router.receive_hop(
            message,
            decoded.payload,
            source_node_id=getattr(
               self._dispatch_context,
               "source_node_id",
               None,
            ),
         )
         return
      if action == _ACTION_REGISTER_LOCK:
         if not isinstance(message, ManifestLocked):
            raise SocketTransportError("invalid_manifest_lock_message")
         accepted = router.register_path(
            message.build.request,
            message.manifest,
            message.build.graph,
            source_node_id=getattr(
               self._dispatch_context,
               "source_node_id",
               None,
            ),
            entry_node_id=self._entry_node(message.request_id),
         )
         if not accepted:
            raise SocketTransportError("manifest_registration_rejected")
         return
      if action == _ACTION_ENTRY_LOCK:
         if not isinstance(message, ManifestLocked):
            raise SocketTransportError("invalid_manifest_lock_message")
         router.receive_manifest_locked(
            message,
            source_node_id=getattr(
               self._dispatch_context,
               "source_node_id",
               None,
            ),
         )
         return
      if action == _ACTION_MANIFEST_DELTA:
         if not isinstance(message, ManifestDelta):
            raise SocketTransportError("invalid_manifest_delta_message")
         with self._lock:
            self.manifest_deltas.append(message)
         return
      if action == _ACTION_PATH_CANCELLATION:
         if not isinstance(message, PathCancellation):
            raise SocketTransportError("invalid_path_cancellation_message")
         accepted = router.receive_path_cancellation(
            message,
            source_node_id=getattr(
               self._dispatch_context,
               "source_node_id",
               None,
            ),
         )
         if not accepted:
            raise SocketTransportError("path_cancellation_rejected")
         return
      if action == _ACTION_TOKEN_EVENT:
         if not isinstance(message, TokenEvent):
            raise SocketTransportError("invalid_token_event_message")
         router.receive_token_event(
            message,
            source_node_id=getattr(
               self._dispatch_context,
               "source_node_id",
               None,
            ),
         )
         return
      if action == _ACTION_VERIFY_EVENT:
         if not isinstance(message, VerifyEvent):
            raise SocketTransportError("invalid_verify_event_message")
         router.receive_verify_event(
            message,
            source_node_id=getattr(
               self._dispatch_context,
               "source_node_id",
               None,
            ),
         )
         return
      if action == _ACTION_PREFILL_CHUNK_COMPLETED:
         if not isinstance(message, PrefillChunkCompleted):
            raise SocketTransportError("invalid_prefill_chunk_completed_message")
         router.receive_prefill_chunk_completed(
            message,
            source_node_id=getattr(
               self._dispatch_context,
               "source_node_id",
               None,
            ),
         )
         return
      if action == _ACTION_FAILURE_REPORT:
         if not isinstance(message, FailureReport):
            raise SocketTransportError("invalid_failure_report_message")
         router.receive_failure_report(
            message,
            source_node_id=getattr(
               self._dispatch_context,
               "source_node_id",
               None,
            ),
         )
         return
      raise SocketTransportError(f"unknown_delivery_action:{action}")

   def _send_hop(self, source_node_id: str, header: HopHeader, payload: object) -> None:
      if isinstance(payload, ProgressivePrefillContext):
         graph = payload.graph
         self._entry_nodes.setdefault(header.request_id, source_node_id)
         self._path_graphs[header.path_id] = graph
         self._participant_nodes_by_path[header.path_id] = frozenset(
            self._node_for_placement(graph, hop.placement_id)
            for hop in payload.build.ordered_hops
         )
         frame, span = _encode_with_span(
            encode_progressive_prefill, header, payload,
            request_id=header.request_id,
         )
      else:
         if not isinstance(payload, bytes):
            raise SocketTransportError("hop_payload_must_be_bytes")
         graph = self._path_graphs.get(header.path_id)
         if graph is None:
            raise SocketTransportError(f"unknown_path:{header.path_id}")
         frame, span = _encode_with_span(
            encode_frame, header, payload, request_id=header.request_id
         )
      destination_node = self._node_for_placement(
         graph,
         header.destination_placement_id,
      )
      self._send(source_node_id, destination_node, _ACTION_HOP, frame, span=span)

   def _send_manifest_locked(
      self,
      source_node_id: str,
      locked: ManifestLocked,
   ) -> None:
      graph = locked.build.graph
      frame, span = _encode_with_span(
         encode_frame, locked, request_id=locked.request_id
      )
      participants = {
         self._node_for_placement(graph, hop.placement_id)
         for hop in locked.manifest.ordered_hops
      }
      self._participant_nodes_by_path[locked.path_id] = frozenset(participants)
      for node_id in sorted(participants):
         self._send(
            source_node_id, node_id, _ACTION_REGISTER_LOCK, frame, span=span
         )
      self._path_graphs[locked.path_id] = graph
      self._send(
         source_node_id,
         self._entry_node(locked.request_id),
         _ACTION_ENTRY_LOCK,
         frame,
         span=span,
      )

   def _send_path_cancellation(
      self,
      source_node_id: str,
      cancellation: PathCancellation,
   ) -> None:
      if self._entry_node(cancellation.request_id) != source_node_id:
         raise SocketTransportError("path_cancellation_source_not_entry")
      participants = self._participant_nodes_by_path.get(cancellation.path_id)
      if participants is None:
         raise SocketTransportError(f"unknown_path:{cancellation.path_id}")
      frame, span = _encode_with_span(
         encode_frame, cancellation, request_id=cancellation.request_id
      )
      for node_id in sorted(participants - {source_node_id}):
         self._send(
            source_node_id,
            node_id,
            _ACTION_PATH_CANCELLATION,
            frame,
            span=span,
         )
      self._path_graphs.pop(cancellation.path_id, None)
      self._participant_nodes_by_path.pop(cancellation.path_id, None)
      self._entry_nodes.pop(cancellation.request_id, None)

   def _send_to_entry(
      self,
      source_node_id: str,
      request_id: str,
      action: int,
      frame: bytes,
      *,
      span: _OutboundSpan | None = None,
   ) -> None:
      self._entry_nodes.setdefault(request_id, source_node_id)
      self._send(
         source_node_id,
         self._entry_node(request_id),
         action,
         frame,
         span=span if span is not None else _OutboundSpan(request_id, None, None),
      )

   def _send(
      self,
      source_node_id: str,
      node_id: str,
      action: int,
      frame: bytes,
      *,
      span: _OutboundSpan | None = None,
   ) -> None:
      """Write one framed delivery and read its acknowledgement back.

      Timing: ``write_at`` is taken immediately *before* the socket write and
      ``read_end_at`` immediately *after* the last read of the response, so
      ``read_end_at - write_at`` is a measured round trip.  The frame encode
      (``span``) ran before the write; the response decode runs after the read.
      The three spans are reported separately and never summed into one number.
      """

      if not self._started:
         raise SocketTransportError("socket_mesh_not_started")
      if source_node_id not in self.routers:
         raise SocketTransportError(f"unknown_source_node:{source_node_id}")
      endpoint = self._endpoints.get(node_id)
      if endpoint is None:
         raise SocketTransportError(f"unknown_node:{node_id}")
      with self._lock:
         delivery_id = self._next_delivery_id
         self._next_delivery_id += 1
         pending = _PendingDelivery(
            completed=threading.Event(),
            source_node_id=source_node_id,
         )
         self._pending_deliveries[delivery_id] = pending
      packet = (
         bytes((action,))
         + _DELIVERY_ID.pack(delivery_id)
         + frame
      )
      if len(packet) > _MAX_PACKET_BYTES:
         with self._lock:
            self._pending_deliveries.pop(delivery_id, None)
         raise SocketTransportError("packet_too_large")
      with self._lock:
         self.active_connection_count += 1
         self.maximum_active_connections = max(
            self.maximum_active_connections,
            self.active_connection_count,
         )
      wire_bytes = _PACKET_LENGTH.pack(len(packet)) + packet
      write_at: float | None = None
      write_after_at: float | None = None
      read_end_at: float | None = None
      deserialize_start_at: float | None = None
      deserialize_end_at: float | None = None
      response_bytes = 0
      detail_text = ""
      status = 0
      try:
         with socket.create_connection(
            (endpoint.host, endpoint.port),
            timeout=self.connect_timeout_seconds,
         ) as connection:
            # TRUE socket spans: stamp immediately before the write and
            # immediately after the read completes.
            write_at = time.monotonic()
            connection.sendall(wire_bytes)
            write_after_at = time.monotonic()
            head = self._recv_exact(connection, _RESPONSE.size)
            status, detail_length = _RESPONSE.unpack(head)
            detail = (
               self._recv_exact(connection, detail_length)
               if detail_length
               else b""
            )
            read_end_at = time.monotonic()
            response_bytes = _RESPONSE.size + detail_length
            deserialize_start_at = time.monotonic()
            detail_text = detail.decode("utf-8", errors="replace")
            deserialize_end_at = time.monotonic()
      except (
         OSError,
         RuntimeError,
         struct.error,
      ):
         with self._lock:
            self._pending_deliveries.pop(delivery_id, None)
         raise
      finally:
         with self._lock:
            self.active_connection_count -= 1
         self._file_link_telemetry(
            source_node_id=source_node_id,
            destination_node_id=node_id,
            action=action,
            span=span,
            write_at=write_at,
            write_after_at=write_after_at,
            read_end_at=read_end_at,
            deserialize_start_at=deserialize_start_at,
            deserialize_end_at=deserialize_end_at,
            bytes_sent=len(wire_bytes),
            bytes_recv=response_bytes,
            delivery_id=delivery_id,
         )
      if status:
         with self._lock:
            self._pending_deliveries.pop(delivery_id, None)
         raise SocketTransportError(detail_text)
      if not pending.completed.wait(timeout=self.connect_timeout_seconds):
         with self._lock:
            self._pending_deliveries.pop(delivery_id, None)
         raise SocketTransportError(f"dispatch_timeout:{delivery_id}")
      with self._lock:
         self._pending_deliveries.pop(delivery_id, None)
      if pending.error:
         raise SocketTransportError(pending.error)

   def _file_link_telemetry(
      self,
      *,
      source_node_id: str,
      destination_node_id: str,
      action: int,
      span: _OutboundSpan | None,
      write_at: float | None,
      write_after_at: float | None,
      read_end_at: float | None,
      deserialize_start_at: float | None,
      deserialize_end_at: float | None,
      bytes_sent: int,
      bytes_recv: int,
      delivery_id: int,
   ) -> None:
      """File one measured round trip and one hop for a completed delivery.

      A round trip is filed only when both ends of the socket span exist, so a
      failed delivery never contributes an invented latency.  Telemetry failures
      never break a delivery: a recording error is swallowed here by design,
      because the transport's job is to move frames.
      """

      if self.rtt_table is None and self.hop_recorder is None:
         return
      try:
         if self.rtt_table is not None and write_at is not None and read_end_at is not None:
            self.rtt_table.record_round_trip_ms(
               source_node_id,
               destination_node_id,
               (read_end_at - write_at) * 1000.0,
               measurement_source=SOURCE_SOCKET_ROUND_TRIP,
            )
         if self.hop_recorder is not None:
            self.hop_recorder.record_hop(
               instrument.HopSample(
                  request_id=(
                     span.request_id
                     if span is not None and span.request_id is not None
                     else f"delivery-{delivery_id}"
                  ),
                  stage_id=_ACTION_STAGE_NAMES.get(action, f"action_{action}"),
                  node_id=f"{source_node_id}->{destination_node_id}",
                  serialize_start_at=None if span is None else span.serialize_start_at,
                  serialize_end_at=None if span is None else span.serialize_end_at,
                  serialize_source=(
                     None if span is None else instrument.SERIALIZE_SOURCE_ENCODE_FRAME
                  ),
                  socket_write_at=write_at,
                  socket_write_after_at=write_after_at,
                  socket_read_end_at=read_end_at,
                  deserialize_start_at=deserialize_start_at,
                  deserialize_end_at=deserialize_end_at,
                  deserialize_source=(
                     None
                     if deserialize_start_at is None
                     else instrument.DESERIALIZE_SOURCE_RESPONSE_FRAME
                  ),
                  bytes_sent=bytes_sent,
                  bytes_recv=bytes_recv,
               )
            )
      except Exception:  # noqa: BLE001 - telemetry must never fail a delivery
         return

   def _complete_delivery(self, delivery_id: int, error: str) -> None:
      with self._lock:
         pending = self._pending_deliveries.get(delivery_id)
         if pending is None:
            return
         pending.error = error
         pending.completed.set()

   def _entry_node(self, request_id: str) -> str:
      try:
         return self._entry_nodes[request_id]
      except KeyError as error:
         raise SocketTransportError(f"unknown_request_entry:{request_id}") from error

   @staticmethod
   def _node_for_placement(graph, placement_id: str) -> str:
      for stage in graph.stages:
         for placement in stage.placements:
            if placement.placement_id == placement_id:
               return placement.node_id
      raise SocketTransportError(f"unknown_placement:{placement_id}")

   @staticmethod
   def _recv_exact(connection: socket.socket, size: int) -> bytes:
      chunks = bytearray()
      while len(chunks) < size:
         chunk = connection.recv(size - len(chunks))
         if not chunk:
            raise SocketTransportError("truncated_socket_message")
         chunks.extend(chunk)
      return bytes(chunks)
