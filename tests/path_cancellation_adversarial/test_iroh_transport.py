"""Iroh adapter PathCancellation adversarial corpus using an in-memory sidecar client."""

from __future__ import annotations

import threading
import time

import pytest

from mycelium_router.contracts import PathCancellation
from mycelium_router.transports.iroh import IrohTransportError
from mycelium_router.wire import decode_frame, encode_frame
from test_router_iroh_transport import _Hub, _PausedAcquireSemaphore, _transport

from ._harness import join_bounded, run_in_thread


class _CancellationRouter:
   def __init__(self) -> None:
      self.calls: list[tuple[PathCancellation, str | None]] = []
      self.received = threading.Event()

   def receive_path_cancellation(self, cancellation, *, source_node_id=None):
      self.calls.append((cancellation, source_node_id))
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
      assert cancellation.path_id not in transport._participant_nodes_by_path
      assert cancellation.request_id not in transport._entry_nodes
      with pytest.raises(IrohTransportError, match="path_cancellation_source_not_entry"):
         transport.send_path_cancellation(cancellation)
      assert transport.route_ready is False
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
      with pytest.raises(IrohTransportError, match="path_cancellation_already_pending"):
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


def test_close_wins_cancellation_admission_race_without_starting_worker() -> None:
   hub = _Hub()
   transport = _transport(hub, queue_capacity=1)
   transport.bind_router(_CancellationRouter())
   cancellation = PathCancellation("request-admission-race", "path-admission-race", 0, 3)
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
