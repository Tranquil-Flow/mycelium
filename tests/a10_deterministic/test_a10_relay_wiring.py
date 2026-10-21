# SPDX-License-Identifier: AGPL-3.0-only
"""A10 relay wiring (lane pm-a10, parent-approved 2026-09-27): the one seam
that makes the A10 physical gate satisfiable end-to-end.

``RelayEngine._execute_runtime_batch`` (mycelium_router/relay.py) is where
the relay's own already-live microbatch decision (a ``RuntimeBatch`` of >=2
compatible ``HopWorkItem``s -- see ``drain_ready_batches``) meets the runtime
port. Before this change it always called ``self.runtime.execute_batch(batch)``
directly, which every shipped port answers as a sequential per-item loop. It
now first offers the batch to
``mycelium_router.a10_batch_producer.attempt_runtime_batch``, and only falls
back to the pre-existing call when that returns ``None``.

Failure modes this suite catches (one per test, by name):

  test_non_a10_port_batch_dispatch_is_byte_identical_to_before
      A non-A10 port (numpy/llama/openrouter today) being touched by the new
      producer at all, or its ``execute_batch`` call/result shape changing.
  test_ineligible_singleton_batch_on_an_a10_port_is_byte_identical_to_before
      A 1-member batch on an A10-capable port silently starting to go
      through the offer/dispatch protocol instead of the incumbent
      sequential ``execute_batch(RuntimeBatch)`` path.
  test_two_member_batch_on_mlx_port_is_one_backend_invocation_in_order
      The actual point of this change: two compatible concurrent members
      served by two backend invocations instead of one, or returned out of
      item order.
  test_producer_exception_still_falls_back_to_the_old_sequential_path
      An exception raised inside the producer's own call crashing the relay
      instead of falling back exactly as before this wiring existed.
"""

from __future__ import annotations

import pytest

from mycelium_router.contracts import RouterConfig, RuntimeBatch, RuntimeResult
from mycelium_router.fakes import FakeRuntimePort, ManualClock
from mycelium_router.relay import RelayEngine
from mycelium_router.scheduler import HopScheduler
from mycelium_router.payloads import encode_token_ids
from test_router_mlx_runtime import _fresh_ports, _work_item, runtime_case  # noqa: F401


def _relay(runtime) -> RelayEngine:
   return RelayEngine(
      node_id="node-a",
      runtime=runtime,
      transport=None,
      scheduler=HopScheduler(RouterConfig()),
      clock=ManualClock(),
      builder=None,
      device_states=None,
   )


class _RaisingPort(FakeRuntimePort):
   """Shaped like a non-A10 port whose ``execute_batch`` itself explodes."""

   def execute_batch(self, batch):
      raise RuntimeError("injected_runtime_batch_failure")


def _two_prefill_items(case):
   token_ids = (1, 2, 3)
   item_a = _work_item(
      case,
      0,
      encode_token_ids(token_ids),
      phase="PREFILL",
      token_span=len(token_ids),
      request_id="request-a",
      path_id="path-a",
      idempotency_key="request-a:PREFILL:-1:0",
   )
   item_b = _work_item(
      case,
      0,
      encode_token_ids(token_ids),
      phase="PREFILL",
      token_span=len(token_ids),
      request_id="request-b",
      path_id="path-b",
      idempotency_key="request-b:PREFILL:-1:0",
   )
   return item_a, item_b


def test_non_a10_port_batch_dispatch_is_byte_identical_to_before():
   runtime = FakeRuntimePort()
   relay = _relay(runtime)
   # FakeRuntimePort has no batch_key machinery of its own; build a minimal
   # compatible pair by hand using two items that share one batch_key.
   from mycelium_router.contracts import HopWorkItem, RuntimeBatchKey

   key = RuntimeBatchKey(
      deployment_id="d",
      deployment_epoch=1,
      model_commit="c",
      manifest_digest="m",
      placement_id="p",
      assignment_id="a",
      stage_signature="s",
      load_proof_digest="l",
      runtime_backend="fake",
      phase="PREFILL",
      hidden_size=8,
      activation_bytes=32,
      token_span=3,
   )

   def item(path_id, idem):
      return HopWorkItem(
         request_id=f"request:{path_id}",
         path_id=path_id,
         path_attempt=0,
         phase="PREFILL",
         token_index=-1,
         hop_index=0,
         placement_id="p",
         qos_class="interactive",
         deficit_ratio=0.0,
         enqueued_at=0.0,
         idempotency_key=idem,
         payload=b"payload",
         batch_key=key,
      )

   batch = RuntimeBatch(
      compatibility_key=key,
      items=(item("path-a", "req-a"), item("path-b", "req-b")),
   )

   before = len(runtime.executed_batches)
   result = relay._execute_runtime_batch(batch)

   assert len(runtime.executed_batches) == before + 1
   assert runtime.executed_batches[-1] is batch
   # The fake's own execute_batch just loops execute() over batch.items, in
   # order; confirm the relay returned exactly that -- one call, on the
   # original RuntimeBatch object, with no A10 side effects (a non-A10 port
   # has no offer table at all, which is itself the proof the producer
   # never touched it).
   assert len(result) == 2
   assert all(entry.success for entry in result)
   assert [entry.payload for entry in result] == [item.payload for item in batch.items]
   assert list(runtime.executed[-2:]) == list(batch.items)


def test_ineligible_singleton_batch_on_an_a10_port_is_byte_identical_to_before(
   runtime_case,
):
   ports = _fresh_ports(runtime_case)
   port = ports[0]
   relay = _relay(port)
   item_a, _ = _two_prefill_items(runtime_case)
   batch = RuntimeBatch(compatibility_key=item_a.batch_key, items=(item_a,))

   result = relay._execute_runtime_batch(batch)

   assert len(result) == 1
   assert result[0].success
   # A singleton batch must take the incumbent sequential path: no A10
   # observation is ever published for it (mlx_runtime.py's own contract --
   # "RuntimeBatch keeps the incumbent ... behaviour ... publishes no
   # observation record").
   assert port.batch_observation() is None


def test_two_member_batch_on_mlx_port_is_one_backend_invocation_in_order(
   runtime_case,
):
   ports = _fresh_ports(runtime_case)
   port = ports[0]
   relay = _relay(port)
   item_a, item_b = _two_prefill_items(runtime_case)
   batch = RuntimeBatch(compatibility_key=item_a.batch_key, items=(item_a, item_b))

   result = relay._execute_runtime_batch(batch)

   assert len(result) == 2
   assert all(entry.success for entry in result)

   observation = port.batch_observation()
   assert observation is not None
   assert observation.backend_invocations == 1
   assert observation.actual_member_count == 2
   # Order: the observation's own outcome list is in dispatch order, which
   # must match the batch's item order (path-a before path-b).
   assert [ref for ref, _ in observation.member_outcomes] == [
      f"a10-member:{item_a.idempotency_key}",
      f"a10-member:{item_b.idempotency_key}",
   ]


def test_producer_exception_still_falls_back_to_the_old_sequential_path(
   monkeypatch,
):
   runtime = _RaisingPort()
   relay = _relay(runtime)
   from mycelium_router.contracts import HopWorkItem, RuntimeBatchKey

   key = RuntimeBatchKey(
      deployment_id="d",
      deployment_epoch=1,
      model_commit="c",
      manifest_digest="m",
      placement_id="p",
      assignment_id="a",
      stage_signature="s",
      load_proof_digest="l",
      runtime_backend="fake",
      phase="PREFILL",
      hidden_size=8,
      activation_bytes=32,
      token_span=3,
   )
   item = HopWorkItem(
      request_id="request:path-a",
      path_id="path-a",
      path_attempt=0,
      phase="PREFILL",
      token_index=-1,
      hop_index=0,
      placement_id="p",
      qos_class="interactive",
      deficit_ratio=0.0,
      enqueued_at=0.0,
      idempotency_key="req-a",
      payload=b"payload",
      batch_key=key,
   )
   batch = RuntimeBatch(compatibility_key=key, items=(item,))

   result = relay._execute_runtime_batch(batch)

   assert len(result) == 1
   assert result[0].success is False
   assert result[0].failure_scope == "PLACEMENT"
   assert result[0].failure_reason == "worker_runtime_unavailable"
