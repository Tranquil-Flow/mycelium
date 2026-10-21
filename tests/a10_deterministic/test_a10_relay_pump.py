# SPDX-License-Identifier: AGPL-3.0-only
"""Relay and scheduler guarantees the live hop batching relies on.

Failure modes this suite catches (one per test, by name):

  test_enqueue_checks_the_hop_sender_exactly_as_receive_hop_does
      Queued ingress accepting a hop from a node the manifest does not name as
      its sender (the synchronous path refuses it).
  test_work_queued_during_a_drain_is_drained_by_the_active_drainer
      A hop queued by one thread while another drains being stranded until some
      later ingress, because the second thread found the drain busy.
  test_a_batch_never_holds_two_items_of_one_request
      One request's dependent items (a later chunk extends the KV an earlier one
      writes) sharing one runtime batch.
  test_a_member_completion_failure_without_a_handler_is_raised_after_its_batch
      A completion failure losing the batch's other members, or vanishing.
  test_the_receiver_takes_the_next_frame_as_soon_as_a_dispatch_is_acknowledged
      The receiver waiting for its next poll before sending a queued dispatch
      acknowledgement, so frames arrive one poll interval apart.
  test_pump_on_a_router_with_nothing_queued_is_a_no_op
      A pump or non-blocking drain doing work, or failing, with nothing queued.
"""

from __future__ import annotations

import threading
import time

import pytest

from mycelium_router.contracts import HopWorkItem, RouterConfig, RuntimeBatchKey
from mycelium_router.scheduler import HopScheduler
from tests.a10_deterministic.test_a10_live_hop_batching import (
   _GateRuntime,
   _Hub,
   _decode_header,
   _event_frame_for,
   _locked,
   _nodes,
   _router,
   _transport,
   _wait,
)
from test_router_mlx_runtime import runtime_case  # noqa: F401


def _registered(case, runtime, names=("a", "b")):
   router = _router(case, runtime, None)
   entry_node, _ = _nodes(case)
   for name in names:
      locked = _locked(case, name)
      assert router.register_path(
         locked.build.request,
         locked.manifest,
         locked.build.graph,
         entry_node_id=entry_node,
      )
   return router


class _Sink:
   def __init__(self):
      self.tokens = []
      self.reports = []

   def send_token_event(self, event):
      self.tokens.append(event)

   def send_failure_report(self, report):
      self.reports.append(report)

   def send_hop(self, header, payload):
      raise AssertionError("the final stage forwards no hop")


def test_enqueue_checks_the_hop_sender_exactly_as_receive_hop_does(
   runtime_case,  # noqa: F811 - pytest fixture
):
   router = _registered(runtime_case, _GateRuntime())
   header = _decode_header(runtime_case, "a")
   entry_node, final_node = _nodes(runtime_case)

   refused = router.enqueue_hop(header, b"hidden", source_node_id=final_node)
   assert (refused.disposition, refused.reason) == ("REJECTED", "source_node_mismatch")
   assert router.pending_batch_hops() == 0
   accepted = router.enqueue_hop(header, b"hidden", source_node_id=entry_node)
   assert accepted.disposition == "QUEUED"
   assert router.pending_batch_hops() == 1


def test_work_queued_during_a_drain_is_drained_by_the_active_drainer(
   runtime_case,  # noqa: F811 - pytest fixture
):
   runtime = _GateRuntime()
   router = _registered(runtime_case, runtime)
   sink = _Sink()
   router.relay.transport = sink
   entry_node, _ = _nodes(runtime_case)
   runtime.block = True

   router.enqueue_hop(_decode_header(runtime_case, "a"), b"a", source_node_id=entry_node)
   drainer = threading.Thread(target=router.pump_ready_batches)
   drainer.start()
   assert runtime.entered.wait(5)
   # Another thread queues work while the drain is busy executing; its own pump
   # finds the drain taken and returns at once.
   router.enqueue_hop(_decode_header(runtime_case, "b"), b"b", source_node_id=entry_node)
   assert router.pump_ready_batches() == ()
   runtime.block = False
   runtime.release.set()
   drainer.join(5)
   assert not drainer.is_alive()
   # The active drainer took the late work before it returned.
   assert router.pending_batch_hops() == 0
   assert sorted(event.request_id for event in sink.tokens) == ["request-a", "request-b"]


def _item(request_id, key, index):
   return HopWorkItem(
      request_id=request_id,
      path_id=f"path:{request_id}",
      path_attempt=0,
      phase="PREFILL_CHUNK",
      token_index=index,
      hop_index=0,
      placement_id="placement-000",
      qos_class="interactive",
      deficit_ratio=0.0,
      enqueued_at=0.0,
      idempotency_key=f"{request_id}:{index}",
      payload=b"x",
      batch_key=key,
   )


def test_a_batch_never_holds_two_items_of_one_request():
   key = RuntimeBatchKey(
      deployment_id="d",
      deployment_epoch=1,
      model_commit="c",
      manifest_digest="m",
      placement_id="placement-000",
      assignment_id="a",
      stage_signature="s",
      load_proof_digest="l",
      runtime_backend="mlx",
      phase="PREFILL_CHUNK",
      hidden_size=8,
      activation_bytes=4,
      token_span=4,
   )
   scheduler = HopScheduler(RouterConfig())
   for item in (_item("r1", key, 0), _item("r1", key, 1), _item("r2", key, 0)):
      scheduler.enqueue(item)

   candidates = scheduler.batch_candidates(now=0.0)
   assert [(item.request_id, item.token_index) for item in candidates] == [
      ("r1", 0),
      ("r2", 0),
   ]
   first = scheduler.pop_batch(now=0.0)
   assert [(item.request_id, item.token_index) for item in first.items] == [
      ("r1", 0),
      ("r2", 0),
   ]
   second = scheduler.pop_batch(now=0.0)
   assert [(item.request_id, item.token_index) for item in second.items] == [("r1", 1)]


def test_a_member_completion_failure_without_a_handler_is_raised_after_its_batch(
   runtime_case,  # noqa: F811 - pytest fixture
):
   router = _registered(runtime_case, _GateRuntime())
   sink = _Sink()
   router.relay.transport = sink
   entry_node, _ = _nodes(runtime_case)
   original = router.relay._complete_queued_hop

   def complete(header, item, result, digest, generation):
      if header.request_id == "request-a":
         raise RuntimeError("injected_member_completion_failure")
      return original(header, item, result, digest, generation)

   router.relay._complete_queued_hop = complete
   for name in ("a", "b"):
      router.enqueue_hop(_decode_header(runtime_case, name), b"h", source_node_id=entry_node)
   with pytest.raises(RuntimeError, match="injected_member_completion_failure"):
      router.drain_ready_batches()
   # b, batched with a, was still completed before the failure was raised.
   assert [event.request_id for event in sink.tokens] == ["request-b"]
   assert router.pending_batch_hops() == 0


def test_the_receiver_takes_the_next_frame_as_soon_as_a_dispatch_is_acknowledged(
   runtime_case,  # noqa: F811 - pytest fixture
):
   received = []

   class _Recorder:
      def receive_token_event(self, event, *, source_node_id=None):
         received.append(time.monotonic())
         return True

   hub = _Hub()
   transport = _transport(hub, runtime_case)
   transport.poll_interval_seconds = 0.5
   transport.bind_router(_Recorder())
   transport.start()
   try:
      for index in range(3):
         hub.deliver(bytes([index + 1]) * 16, _event_frame_for(index))
      _wait(lambda: len(received) == 3)
      # Three frames in far less than one poll interval each.
      assert received[-1] - received[0] < 0.5
   finally:
      transport.close()


def test_pump_on_a_router_with_nothing_queued_is_a_no_op(
   runtime_case,  # noqa: F811 - pytest fixture
):
   router = _registered(runtime_case, _GateRuntime())
   assert router.pump_ready_batches() == ()
   assert router.drain_ready_batches(blocking=False) == ()
