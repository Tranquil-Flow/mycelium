# SPDX-License-Identifier: AGPL-3.0-only
"""A10 batch producer (lane pm-a10): the missing live caller.

``mycelium_router.a10_batch_producer.attempt_runtime_batch`` is the
translation this repository never had between the relay's already-live
microbatch decision (a plain ``RuntimeBatch``) and the A10 offer/dispatch
protocol (``offer_batch_member`` + ``execute_batch(RuntimeBatchRequest)``).

Failure modes this module catches (one per test, by name):

  test_ineligible_batch_never_touches_the_port
      A batch below the A10 minimum member count, or without a compatibility
      key, dispatched anyway -- would silently change single-request
      behaviour.
  test_port_without_a10_surface_is_left_untouched
      A port lacking the A10 methods dispatched anyway via duck-typed
      guessing -- would crash a non-A10 port (numpy/llama/openrouter today).
  test_eligible_batch_is_offered_and_dispatched_as_one_group
      The producer failing to offer every member, or offering out of order,
      or building a malformed request.
  test_successful_dispatch_returns_real_results_in_original_order
      Results returned out of order, or not derived from the runtime's own
      report.
  test_offer_failure_is_fail_closed_and_never_dispatches
      A failed offer for one member still causing ``execute_batch`` to be
      called -- would double-execute once the caller's own fallback also
      runs.
  test_execute_batch_exception_is_fail_closed
      An exception from the port's own dispatch swallowed into a false
      success instead of a clean fall-back signal.
  test_dropped_member_is_reported_cancelled_not_silently_lost_or_reexecuted
      A member the runtime legitimately dropped (a mid-flight cancellation)
      either vanishing from the result tuple or triggering a second,
      duplicate execution attempt.
  test_non_conforming_report_type_refuses_rather_than_guesses
      A port returning the wrong report shape treated as a genuine batch
      claim.
  test_mlx_runtime_port_batch_is_one_real_backend_invocation
      The one thing all of the above is *for*: wired to the real MLX stage
      runtime, two concurrent members must be served by exactly one backend
      invocation, not two -- the actual A10 physical-gate predicate
      (``backend_invocations == 1``, ``actual_member_count >= 2``).
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from mycelium_router.a10_batch_admission import BatchExecutionReport, MemberExecution
from mycelium_router.a10_batch_producer import attempt_runtime_batch, is_eligible
from mycelium_router.contracts import HopWorkItem, RuntimeBatch, RuntimeBatchKey, RuntimeResult
from mycelium_router.payloads import encode_token_ids
from test_router_mlx_runtime import _fresh_ports, _work_item, runtime_case  # noqa: F401


def _batch_key(**overrides) -> RuntimeBatchKey:
   fields = dict(
      deployment_id="deployment-1",
      deployment_epoch=1,
      model_commit="commit-1",
      manifest_digest="manifest-1",
      placement_id="placement-1",
      assignment_id="assignment-1",
      stage_signature="stage-1",
      load_proof_digest="proof-1",
      runtime_backend="fake",
      phase="PREFILL",
      hidden_size=8,
      activation_bytes=32,
      token_span=3,
   )
   fields.update(overrides)
   return RuntimeBatchKey(**fields)


def _item(*, path_id: str, idempotency_key: str, key: RuntimeBatchKey) -> HopWorkItem:
   return HopWorkItem(
      request_id=f"request:{path_id}",
      path_id=path_id,
      path_attempt=0,
      phase=key.phase,
      token_index=-1,
      hop_index=0,
      placement_id=key.placement_id,
      qos_class="interactive",
      deficit_ratio=0.0,
      enqueued_at=0.0,
      idempotency_key=idempotency_key,
      payload=b"payload",
      batch_key=key,
   )


def _two_member_batch() -> RuntimeBatch:
   key = _batch_key()
   items = (
      _item(path_id="path-a", idempotency_key="req-a:PREFILL:-1:0", key=key),
      _item(path_id="path-b", idempotency_key="req-b:PREFILL:-1:0", key=key),
   )
   return RuntimeBatch(compatibility_key=key, items=items)


def _result(token_id: int) -> RuntimeResult:
   return RuntimeResult(success=True, payload=b"out", token_id=token_id)


class _FakeA10Port:
   """A minimal stand-in declaring the full A10 dispatch surface."""

   def __init__(self, *, report_factory=None):
      self.offered: list[tuple[HopWorkItem, str]] = []
      self.offer_calls = 0
      self.dispatch_calls = 0
      self.requests: list[object] = []
      self.offer_exception_on_call: int | None = None
      self.execute_batch_exception: Exception | None = None
      self._report_factory = report_factory

   def offer_batch_member(self, item: HopWorkItem, *, member_ref: str) -> str:
      self.offer_calls += 1
      if self.offer_exception_on_call == self.offer_calls:
         raise RuntimeError("injected_offer_failure")
      self.offered.append((item, member_ref))
      return member_ref

   def dispatch_mode(self) -> str:
      return "runtime_batch"

   def execute_batch(self, request):
      self.dispatch_calls += 1
      self.requests.append(request)
      if self.execute_batch_exception is not None:
         raise self.execute_batch_exception
      return self._report_factory(request)

   def execution_interval(self):
      return (0.0, 1.0)


def _full_report(request) -> BatchExecutionReport:
   executions = tuple(
      MemberExecution(
         member_ref=ref,
         kv_ref=f"kv:{ref}",
         result=_result(100 + index),
         committed_prefix=1,
      )
      for index, ref in enumerate(request.member_refs)
   )
   return BatchExecutionReport(
      dispatch_mode="runtime_batch",
      backend_invocations=1,
      membership_authority="stage_runtime",
      incarnation="incarnation-1",
      executed=executions,
   )


class _NoA10Port:
   """A port shaped like numpy/llama/openrouter today: no A10 surface at all."""

   def __init__(self):
      self.execute_batch_calls = 0

   def execute(self, item):
      return _result(1)

   def execute_batch(self, batch):
      self.execute_batch_calls += 1
      return tuple(self.execute(item) for item in batch.items)


def test_ineligible_batch_never_touches_the_port():
   key = _batch_key()
   singleton = RuntimeBatch(
      compatibility_key=key,
      items=(_item(path_id="path-a", idempotency_key="only-one", key=key),),
   )
   port = _FakeA10Port(report_factory=_full_report)
   assert not is_eligible(singleton, port)
   assert attempt_runtime_batch(port, singleton) is None
   assert port.offer_calls == 0
   assert port.dispatch_calls == 0


def test_port_without_a10_surface_is_left_untouched():
   batch = _two_member_batch()
   port = _NoA10Port()
   assert not is_eligible(batch, port)
   assert attempt_runtime_batch(port, batch) is None
   assert port.execute_batch_calls == 0


def test_eligible_batch_is_offered_and_dispatched_as_one_group():
   batch = _two_member_batch()
   port = _FakeA10Port(report_factory=_full_report)
   assert is_eligible(batch, port)
   result = attempt_runtime_batch(port, batch)
   assert result is not None
   assert port.offer_calls == 2
   assert [item for item, _ in port.offered] == list(batch.items)
   assert port.dispatch_calls == 1
   (request,) = port.requests
   assert request.proposed_member_count == 2
   assert len(request.member_refs) == 2


def test_successful_dispatch_returns_real_results_in_original_order():
   batch = _two_member_batch()
   port = _FakeA10Port(report_factory=_full_report)
   result = attempt_runtime_batch(port, batch)
   assert result is not None
   assert len(result) == 2
   assert [entry.token_id for entry in result] == [100, 101]
   assert all(entry.success for entry in result)


def test_offer_failure_is_fail_closed_and_never_dispatches():
   batch = _two_member_batch()
   port = _FakeA10Port(report_factory=_full_report)
   port.offer_exception_on_call = 2
   result = attempt_runtime_batch(port, batch)
   assert result is None
   assert port.dispatch_calls == 0


def test_execute_batch_exception_is_fail_closed():
   batch = _two_member_batch()
   port = _FakeA10Port(report_factory=_full_report)
   port.execute_batch_exception = RuntimeError("path_cancelled")
   result = attempt_runtime_batch(port, batch)
   assert result is None
   assert port.offer_calls == 2
   assert port.dispatch_calls == 1


def test_dropped_member_is_reported_cancelled_not_silently_lost_or_reexecuted():
   def partial_report(request):
      dropped_ref, kept_ref = request.member_refs
      return BatchExecutionReport(
         dispatch_mode="sequential_dispatch",
         backend_invocations=1,
         membership_authority="stage_runtime",
         incarnation="incarnation-1",
         executed=(
            MemberExecution(
               member_ref=kept_ref,
               kv_ref=f"kv:{kept_ref}",
               result=_result(200),
               committed_prefix=1,
            ),
         ),
      )

   batch = _two_member_batch()
   port = _FakeA10Port(report_factory=partial_report)
   result = attempt_runtime_batch(port, batch)
   assert result is not None
   assert len(result) == 2
   first, second = result
   assert first.success is False
   assert first.failure_reason == "path_cancelled"
   assert first.failure_scope == "PLACEMENT"
   assert second.success is True
   assert second.token_id == 200
   # Exactly one dispatch attempt -- the dropped member must never be
   # offered or executed a second time by this module.
   assert port.dispatch_calls == 1


def test_non_conforming_report_type_refuses_rather_than_guesses():
   batch = _two_member_batch()
   port = _FakeA10Port(report_factory=lambda request: ("not", "a", "report"))
   result = attempt_runtime_batch(port, batch)
   assert result is None


def test_mlx_runtime_port_batch_is_one_real_backend_invocation(runtime_case):
   """The real thing: two concurrent PREFILL members, one real MLX port.

   This is the exact predicate the A10 physical gate reads
   (``mycelium_router/a10_runtime_batch.py:67-68``):
   ``actual_member_count >= 2`` and ``backend_invocations == 1``.
   """

   ports = _fresh_ports(runtime_case)
   port = ports[0]
   token_ids = (1, 2, 3)
   item_a = _work_item(
      runtime_case,
      0,
      encode_token_ids(token_ids),
      phase="PREFILL",
      token_span=len(token_ids),
      request_id="request-a",
      path_id="path-a",
      idempotency_key="request-a:PREFILL:-1:0",
   )
   item_b = _work_item(
      runtime_case,
      0,
      encode_token_ids(token_ids),
      phase="PREFILL",
      token_span=len(token_ids),
      request_id="request-b",
      path_id="path-b",
      idempotency_key="request-b:PREFILL:-1:0",
   )
   assert item_a.batch_key == item_b.batch_key
   batch = RuntimeBatch(compatibility_key=item_a.batch_key, items=(item_a, item_b))

   assert is_eligible(batch, port)
   results = attempt_runtime_batch(port, batch)

   assert results is not None
   assert len(results) == 2
   assert all(result.success for result in results)

   observation = port.batch_observation()
   assert observation is not None
   assert observation.actual_member_count == 2
   assert observation.backend_invocations == 1
