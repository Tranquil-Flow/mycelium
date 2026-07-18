"""Local-only TCP mesh exercising production Router wire frames.

This adapter binds exclusively to loopback. It is an MVP integration harness, not an
authenticated multi-host transport.
"""

from dataclasses import dataclass
import socket
import socketserver
import struct
import threading

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
_MAX_PACKET_BYTES = 285_212_672
_PACKET_LENGTH = struct.Struct(">I")
_RESPONSE = struct.Struct(">BI")
_DELIVERY_ID = struct.Struct(">Q")


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

   def send_hop(self, header: HopHeader, payload: object) -> None:
      self.mesh._send_hop(self.source_node_id, header, payload)

   def send_manifest_delta(self, delta: ManifestDelta) -> None:
      self.mesh._send_to_entry(
         self.source_node_id,
         delta.request_id,
         _ACTION_MANIFEST_DELTA,
         encode_frame(delta),
      )

   def send_manifest_locked(self, locked: ManifestLocked) -> None:
      self.mesh._send_manifest_locked(self.source_node_id, locked)

   def send_failure_report(self, report: FailureReport) -> None:
      self.mesh._send_to_entry(
         self.source_node_id,
         report.request_id,
         _ACTION_FAILURE_REPORT,
         encode_frame(report),
      )

   def send_token_event(self, event: TokenEvent) -> None:
      self.mesh._send_to_entry(
         self.source_node_id,
         event.request_id,
         _ACTION_TOKEN_EVENT,
         encode_frame(event),
      )

   def send_prefill_chunk_completed(
      self,
      event: PrefillChunkCompleted,
   ) -> None:
      self.mesh._send_to_entry(
         self.source_node_id,
         event.request_id,
         _ACTION_PREFILL_CHUNK_COMPLETED,
         encode_frame(event),
      )


class LoopbackSocketMesh:
   def __init__(self, *, connect_timeout_seconds: float = 5.0):
      self.connect_timeout_seconds = connect_timeout_seconds
      self.routers: dict[str, object] = {}
      self._transports: dict[str, LoopbackSocketTransport] = {}
      self._servers: dict[str, _ThreadingServer] = {}
      self._threads: dict[str, threading.Thread] = {}
      self._endpoints: dict[str, _Endpoint] = {}
      self._entry_nodes: dict[str, str] = {}
      self._path_graphs: dict[str, object] = {}
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
            router.receive_progressive_prefill(header, context)
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
         frame = encode_progressive_prefill(header, payload)
      else:
         if not isinstance(payload, bytes):
            raise SocketTransportError("hop_payload_must_be_bytes")
         graph = self._path_graphs.get(header.path_id)
         if graph is None:
            raise SocketTransportError(f"unknown_path:{header.path_id}")
         frame = encode_frame(header, payload)
      destination_node = self._node_for_placement(
         graph,
         header.destination_placement_id,
      )
      self._send(source_node_id, destination_node, _ACTION_HOP, frame)

   def _send_manifest_locked(
      self,
      source_node_id: str,
      locked: ManifestLocked,
   ) -> None:
      graph = locked.build.graph
      frame = encode_frame(locked)
      participants = {
         self._node_for_placement(graph, hop.placement_id)
         for hop in locked.manifest.ordered_hops
      }
      for node_id in sorted(participants):
         self._send(source_node_id, node_id, _ACTION_REGISTER_LOCK, frame)
      self._path_graphs[locked.path_id] = graph
      self._send(
         source_node_id,
         self._entry_node(locked.request_id),
         _ACTION_ENTRY_LOCK,
         frame,
      )

   def _send_to_entry(
      self,
      source_node_id: str,
      request_id: str,
      action: int,
      frame: bytes,
   ) -> None:
      self._entry_nodes.setdefault(request_id, source_node_id)
      self._send(
         source_node_id,
         self._entry_node(request_id),
         action,
         frame,
      )

   def _send(
      self,
      source_node_id: str,
      node_id: str,
      action: int,
      frame: bytes,
   ) -> None:
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
      try:
         with socket.create_connection(
            (endpoint.host, endpoint.port),
            timeout=self.connect_timeout_seconds,
         ) as connection:
            connection.sendall(_PACKET_LENGTH.pack(len(packet)) + packet)
            status, detail_length = _RESPONSE.unpack(
               self._recv_exact(connection, _RESPONSE.size)
            )
            detail = (
               self._recv_exact(connection, detail_length)
               if detail_length
               else b""
            )
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
      if status:
         with self._lock:
            self._pending_deliveries.pop(delivery_id, None)
         raise SocketTransportError(detail.decode("utf-8", errors="replace"))
      if not pending.completed.wait(timeout=self.connect_timeout_seconds):
         with self._lock:
            self._pending_deliveries.pop(delivery_id, None)
         raise SocketTransportError(f"dispatch_timeout:{delivery_id}")
      with self._lock:
         self._pending_deliveries.pop(delivery_id, None)
      if pending.error:
         raise SocketTransportError(pending.error)

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
