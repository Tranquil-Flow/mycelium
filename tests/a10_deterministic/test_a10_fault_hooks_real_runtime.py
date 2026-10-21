# SPDX-License-Identifier: AGPL-3.0-only
"""The A10 fault hooks composed with the real (tiny) MLX stage runtime.

``test_a10_fault_hooks`` exercises the hooks over a deterministic reference
runtime.  This suite proves the same wrapper sits around the live stage runtime
port (``MLXRuntimePort``, the port a node serves a batch with) without changing
it: unarmed it is a pass-through, and each dispatch fault reaches the caller as the
declared fail-closed shape while the real runtime keeps its own state.  It is still
a CPU/in-process test over a tiny model, not the physical two-Mac execution.

  test_unarmed_wrapper_leaves_the_real_runtime_dispatch_unchanged
      The wrapper altering a real batch report or hiding the runtime's surface.
  test_a_shared_backend_failure_leaves_the_real_runtime_without_active_state
      An injected failure that still ran the backend, or lost the offered members.
  test_a_partial_member_failure_fails_only_the_victim_on_the_real_report
      A member failure that spreads to the peer or is not visible in the report.
  test_a_wrong_result_count_is_rejected_by_the_runtime_batch_observer
      A surplus result accepted into an observed runtime batch.
  test_the_relay_producer_treats_an_injected_dispatch_failure_as_no_execution
      The relay's fail-closed batch producer double-executing, or raising, after an
      injected backend failure.
"""

from __future__ import annotations

from tests.a10_deterministic import ensure_worktree_imports

ensure_worktree_imports()

import pytest

from mycelium_a10_verify_control import EXACT_FLEET_GRANT
from mycelium_router import a10_batch_producer
from mycelium_router.a10_batch_admission import BatchDispatchPort, BatchExecutionReport
from mycelium_router.a10_fault_hooks import A10FaultHooks, A10InjectedFault, FaultInjectingDispatchPort
from mycelium_router.a10_runtime_batch import A10ContractError, RuntimeBatchProposal
from mycelium_router.a10_runtime_execution import execute_observed_batch
from mycelium_router.contracts import RuntimeBatch
from tests.a10_deterministic.test_a10_runtime_batch_dispatch import (
   _dispatch,
   _offer,
   _prefill,
   _request,
)
from test_router_mlx_runtime import (  # noqa: F401 - fixtures for this suite
   _fresh_ports,
   runtime_case,
)


def _group(case, port):
   member_a = _prefill(case, 0, (1, 2), request="request-a", path="path-a")
   member_b = _prefill(case, 0, (3, 4, 5), request="request-b", path="path-b")
   request, refs = _request(case, (member_a, member_b), batch_ref="batch:a10-fault-1")
   _offer(port, (member_a, member_b), refs)
   return (member_a, member_b), request, refs


def _wrapped(port):
   hooks = A10FaultHooks(grant=EXACT_FLEET_GRANT)
   return hooks, FaultInjectingDispatchPort(port, hooks)


def test_unarmed_wrapper_leaves_the_real_runtime_dispatch_unchanged(runtime_case):
   bare_port = _fresh_ports(runtime_case)[0]
   wrapped_port = _fresh_ports(runtime_case)[0]
   _, request, refs = _group(runtime_case, bare_port)
   _, wrapped_request, _ = _group(runtime_case, wrapped_port)
   hooks, wrapper = _wrapped(wrapped_port)

   assert isinstance(wrapper, BatchDispatchPort)
   assert wrapper.dispatch_mode() == bare_port.dispatch_mode()
   bare = _dispatch(bare_port, request)
   wrapped = _dispatch(wrapper, wrapped_request)

   assert hooks.fired() == ()
   assert [e.member_ref for e in wrapped.executed] == list(refs)
   assert [e.kv_ref for e in wrapped.executed] == [e.kv_ref for e in bare.executed]
   assert [e.committed_prefix for e in wrapped.executed] == [e.committed_prefix for e in bare.executed]
   assert (wrapped.backend_invocations, wrapped.dispatch_mode) == (bare.backend_invocations, bare.dispatch_mode)
   assert wrapper.batch_observation().claims_runtime_batch() is True, "the runtime surface stays reachable"


def test_a_shared_backend_failure_leaves_the_real_runtime_without_active_state(runtime_case):
   port = _fresh_ports(runtime_case)[0]
   _, request, _ = _group(runtime_case, port)
   hooks, wrapper = _wrapped(port)
   hooks.arm("shared_backend_failure")

   with pytest.raises(A10InjectedFault) as raised:
      wrapper.execute_batch(request)

   assert raised.value.code == "shared_backend_failure"
   assert port.kv_snapshot()["active_state_count"] == 0, "the backend never ran"


def test_a_partial_member_failure_fails_only_the_victim_on_the_real_report(runtime_case):
   port = _fresh_ports(runtime_case)[0]
   _, request, refs = _group(runtime_case, port)
   hooks, wrapper = _wrapped(port)
   hooks.arm("partial_member_failure", victim=refs[0])

   report = wrapper.execute_batch(request)

   assert isinstance(report, BatchExecutionReport)
   assert [(e.member_ref, e.result.success) for e in report.executed] == [(refs[0], False), (refs[1], True)]
   assert report.backend_invocations == 1


def test_a_wrong_result_count_is_rejected_by_the_runtime_batch_observer(runtime_case):
   port = _fresh_ports(runtime_case)[0]
   items, _, refs = _group(runtime_case, port)
   hooks, wrapper = _wrapped(port)
   hooks.arm("wrong_result_count")
   proposal = RuntimeBatchProposal(
      compatibility_key=items[0].batch_key,
      member_refs=refs,
      qos_classes=("interactive", "batch"),
      maximum_members=8,
      maximum_bytes=1 << 20,
      proposed_at=0.0,
      collection_deadline_at=0.002,
   )

   with pytest.raises(A10ContractError) as raised:
      execute_observed_batch(wrapper, proposal)

   assert raised.value.code in {"duplicate_member_outcome", "wrong_result_count_or_order"}


def test_the_relay_producer_treats_an_injected_dispatch_failure_as_no_execution(runtime_case):
   port = _fresh_ports(runtime_case)[0]
   items = (
      _prefill(runtime_case, 0, (1, 2), request="request-a", path="path-a"),
      _prefill(runtime_case, 0, (3, 4), request="request-b", path="path-b"),
   )
   hooks, wrapper = _wrapped(port)
   hooks.arm("shared_backend_failure")
   batch = RuntimeBatch(items=items, compatibility_key=items[0].batch_key)

   assert a10_batch_producer.is_eligible(batch, wrapper) is True
   # Control: the same group on a fresh port with nothing armed is served.
   control_hooks, control = _wrapped(_fresh_ports(runtime_case)[0])
   served = a10_batch_producer.attempt_runtime_batch(control, batch)
   assert served is not None and len(served) == 2 and all(result.success for result in served)
   assert control_hooks.fired() == ()
   assert a10_batch_producer.attempt_runtime_batch(wrapper, batch) is None, "no result is invented"
   assert hooks.fired() == ("shared_backend_failure",)
   assert port.kv_snapshot()["active_state_count"] == 0
