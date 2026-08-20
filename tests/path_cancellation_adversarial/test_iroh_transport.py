"""Iroh adapter PathCancellation adversarial corpus using an in-memory sidecar client."""

from __future__ import annotations

from dataclasses import replace
import threading
import time

import pytest

from mycelium_router.contracts import HopHeader, PathCancellation, TokenEvent
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
      with pytest.raises(IrohTransportError, match="path_cancellation_source_not_entry"):
         transport.send_path_cancellation(cancellation)
      assert transport.route_ready is False
   finally:
      transport.close()
   _assert_all_clients_closed(hub)


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
      third_endpoint, _message_id, third_frame, _timeout, generation = hub.routed_sent[0]
      assert decode_frame(primary_frame).message == cancellation
      assert decode_frame(third_frame).message == cancellation
      assert third_endpoint == "third-endpoint"
      assert generation == 7
      assert cancellation.path_id not in transport._participant_nodes_by_path
      assert cancellation.request_id not in transport._entry_nodes
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
   transport.__dict__["_send_or_dispatch"] = (
      lambda destination, frame: dispatched.append((destination, frame))
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
