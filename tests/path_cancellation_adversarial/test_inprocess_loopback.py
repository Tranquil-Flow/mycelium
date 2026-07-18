"""In-process mesh and loopback TCP PathCancellation race corpus."""

from __future__ import annotations

import threading
import time

from mycelium_router.contracts import PathCancellation
from mycelium_router.transports.loopback_socket import LoopbackSocketMesh
from mycelium_router.wire import decode_frame

from ._harness import (
   BlockingRuntime,
   BlockingSink,
   ENTRY_NODE,
   NODE_IDS,
   build_mesh_case,
   join_bounded,
   run_in_thread,
)


def test_mesh_fanout_excludes_source_and_releases_each_participant_once() -> None:
   case = build_mesh_case(request_id="request-mesh-fanout")
   deliveries: list[tuple[str, str | None]] = []
   for node_id, router in case.routers.items():
      original = router.receive_path_cancellation

      def receive(cancellation, *, source_node_id=None, _id=node_id, _fn=original):
         deliveries.append((_id, source_node_id))
         return _fn(cancellation, source_node_id=source_node_id)

      router.receive_path_cancellation = receive

   path_id = case.record.manifest.path_id
   assert case.entry.cancel(case.request.request_id)
   assert not case.entry.cancel(case.request.request_id)

   assert deliveries == [("node-c", ENTRY_NODE), ("node-d", ENTRY_NODE)]
   assert case.mesh.path_cancellations == [(ENTRY_NODE, case.cancellation)]
   assert case.entry.request_status(case.request.request_id) == "CANCELLED"
   assert len(case.capacity.release_calls) == 1
   for node_id in NODE_IDS:
      assert path_id not in case.routers[node_id].relay._paths
      assert case.runtimes[node_id].cancel_calls == [path_id]


def _blocked_decode_case(request_id: str):
   def runtime_factory(node_id: str):
      return BlockingRuntime() if node_id == ENTRY_NODE else BlockingRuntime()

   case = build_mesh_case(request_id=request_id, runtime_factory=runtime_factory)
   blocker = case.runtimes[ENTRY_NODE]
   assert isinstance(blocker, BlockingRuntime)
   thread, results, errors = run_in_thread(
      lambda: case.entry.decode_one_distributed(case.request.request_id)
   )
   assert blocker.decode_entered.wait(timeout=1.0)
   return case, blocker, thread, results, errors


def test_cancellation_during_blocked_decode_emits_no_client_token_and_leaks_no_thread() -> None:
   case, blocker, thread, results, errors = _blocked_decode_case(
      "request-blocked-decode"
   )
   try:
      assert case.entry.cancel(case.request.request_id)
   finally:
      blocker.release_decode.set()
   join_bounded(thread)

   assert errors == []
   assert results == [True]
   assert case.sink.token_ids == []
   assert case.entry.request_status(case.request.request_id) == "CANCELLED"
   assert len(case.capacity.release_calls) == 1
   for runtime in case.runtimes.values():
      assert runtime.cancel_calls == [case.cancellation.path_id]


def test_blocked_decode_cancellation_sends_no_later_remote_tensor_hop() -> None:
   case, blocker, thread, _results, errors = _blocked_decode_case(
      "request-blocked-no-remote-payload"
   )
   late_hops: list[tuple[str, object, object]] = []
   original_send_hop = case.mesh.send_hop

   def capture_late_hop(source_node_id, header, payload):
      late_hops.append((source_node_id, header, payload))
      return original_send_hop(source_node_id, header, payload)

   case.mesh.send_hop = capture_late_hop
   try:
      assert case.entry.cancel(case.request.request_id)
   finally:
      blocker.release_decode.set()
   join_bounded(thread)

   assert errors == []
   assert late_hops == []
   for router in case.routers.values():
      assert all(
         cached[2] != case.cancellation.path_id
         for cached in router.relay._hop_results.values()
      )
      assert all(
         cached[2] != case.cancellation.path_id
         for cached in router.relay._outcomes.values()
      )


def test_local_execute_permit_is_invalidated_before_runtime_work_starts() -> None:
   case = build_mesh_case(request_id="request-local-execute-fence")
   relay = case.entry.relay
   entered = threading.Event()
   release = threading.Event()
   original_execute_manifest = relay.execute_manifest

   def paused_execute_manifest(*args, **kwargs):
      entered.set()
      assert release.wait(timeout=1.0)
      return original_execute_manifest(*args, **kwargs)

   relay.execute_manifest = paused_execute_manifest
   executed_before = len(case.runtimes[ENTRY_NODE].executed)
   thread, results, errors = run_in_thread(
      lambda: case.entry.decode_one(case.request.request_id)
   )
   assert entered.wait(timeout=1.0)
   try:
      assert case.entry.cancel(case.request.request_id)
   finally:
      release.set()
   join_bounded(thread)

   assert errors == []
   assert results == [False]
   assert len(case.runtimes[ENTRY_NODE].executed) == executed_before
   assert case.sink.token_ids == []


def test_cancel_during_scheduler_enqueue_prevents_runtime_start() -> None:
   case = build_mesh_case(request_id="request-scheduler-enqueue-fence")
   relay = case.entry.relay
   entered = threading.Event()
   release = threading.Event()
   original_enqueue = relay.scheduler.enqueue

   def paused_enqueue(item) -> None:
      original_enqueue(item)
      entered.set()
      assert release.wait(timeout=1.0)

   relay.scheduler.enqueue = paused_enqueue
   executed_before = len(case.runtimes[ENTRY_NODE].executed)
   thread, results, errors = run_in_thread(
      lambda: case.entry.decode_one(case.request.request_id)
   )
   assert entered.wait(timeout=1.0)
   try:
      assert case.entry.cancel(case.request.request_id)
   finally:
      release.set()
   join_bounded(thread)

   assert errors == []
   assert results == [False]
   assert len(case.runtimes[ENTRY_NODE].executed) == executed_before


def test_cancellation_wins_when_completion_is_blocked_inside_sink() -> None:
   sink = BlockingSink()
   case = build_mesh_case(
      request_id="request-completion-race",
      max_new_tokens=1,
      sink=sink,
   )
   thread, results, errors = run_in_thread(
      lambda: case.entry.decode_one_distributed(case.request.request_id)
   )
   assert sink.emit_entered.wait(timeout=1.0)
   try:
      assert case.entry.cancel(case.request.request_id)
   finally:
      sink.release_emit.set()
   join_bounded(thread)

   assert errors == []
   assert results == [True]
   assert case.entry.request_status(case.request.request_id) == "CANCELLED"
   assert len(case.capacity.release_calls) == 1


def test_loopback_cancellation_is_control_only_bounded_and_leak_free() -> None:
   def exercise_loopback():
      mesh = LoopbackSocketMesh()
      case = build_mesh_case(
         mesh=mesh,
         request_id="request-loopback-cancellation",
      )
      path_id = case.record.manifest.path_id
      frame_start = len(mesh.frames)
      server_threads = tuple(mesh._threads.values())
      cleanup_started = time.monotonic()
      try:
         assert case.entry.cancel(case.request.request_id)
         cancellation_frames = mesh.frames[frame_start:]
         assert len(cancellation_frames) == 2
         for frame in cancellation_frames:
            decoded = decode_frame(frame)
            assert isinstance(decoded.message, PathCancellation)
            assert decoded.message == case.cancellation
            assert decoded.payload == b""
         assert mesh.active_connection_count == 0
         assert mesh.maximum_active_connections <= len(NODE_IDS)
         assert not hasattr(mesh.transport_for(ENTRY_NODE), "route_ready")
         for node_id in NODE_IDS:
            assert case.runtimes[node_id].cancel_calls == [path_id]
      finally:
         cleanup_started = time.monotonic()
         mesh.close()
      cleanup_latency = time.monotonic() - cleanup_started
      assert cleanup_latency < 4.0
      assert all(not thread.is_alive() for thread in server_threads)
      assert mesh.active_connection_count == 0

   thread, results, errors = run_in_thread(exercise_loopback, daemon=True)
   join_bounded(thread, timeout=5.0)
   assert errors == []
   assert results == [None]
