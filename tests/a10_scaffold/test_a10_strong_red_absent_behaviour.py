# SPDX-License-Identifier: AGPL-3.0-only
"""A10 strong-RED acceptance suite: behaviour that is ABSENT at the frozen base.

Every test in this module imports only surfaces that already exist at the frozen
acceptance base ``a7b412a`` -- ``mycelium_router.contracts``, ``mycelium_router.fakes``,
``mycelium_router.numpy_runtime``, ``mycelium_router.router`` and the existing Router
test helpers.  None of them imports the A10 scaffold.  Each failure is therefore
evidence that the baseline cannot do the thing, not evidence that a scaffold refused
to do it.

Reproduce against the untouched base (no scaffold, no A10 production module):

    git -C <repository root> \
        archive a7b412a | tar -x -C /tmp/a10-base
    mkdir -p /tmp/a10-base/tests/a10_scaffold
    cp tests/a10_scaffold/__init__.py \
       tests/a10_scaffold/test_a10_strong_red_absent_behaviour.py \
       /tmp/a10-base/tests/a10_scaffold/
    cd /tmp/a10-base && python3.14 -m pytest \
        tests/a10_scaffold/test_a10_strong_red_absent_behaviour.py -q

(Use any Python 3.14 interpreter with numpy and pytest; clear PYTHONPATH if it is set.)

Expected: 9 failed, 1 passed.  The nine failures name absent baseline behaviour; the
passing test is a guard that must never regress (an unqualified runtime must not
advertise the target verifier).

Frozen inputs this module binds:
  docs/superpowers/specs/2026-08-18-mycelium-a10-runtime-batching-overlap-target-verification.md
  tests/a10_acceptance/scenarios.v1.json
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from mycelium_router.contracts import (
   RouterConfig,
   RuntimeBatch,
   RuntimeResult,
)
from mycelium_router.fakes import (
   FakeCapacityPort,
   FakeDeviceStateProvider,
   FakeRuntimePort,
   FakeTopologyProvider,
   FakeTransportPort,
   ManualClock,
   SequenceIdSource,
)
from mycelium_router.numpy_runtime import NumpyRuntimePort
from mycelium_router.payloads import encode_token_ids
from mycelium_router.router import Router
from test_router_batching import batch_key, work
from test_router_numpy_runtime import _fresh_ports, _work_item, runtime_case
from test_router_policy import state_table
from test_router_receive_hop import first_header, locked_path

STRONG_RED = "STRONG RED (absent at a7b412a)"


# ---------------------------------------------------------------------------
# Real target-runtime level (mycelium_router/numpy_runtime.py)
# ---------------------------------------------------------------------------


def _two_member_batch(case) -> tuple[tuple, RuntimeBatch]:
   """Two compatible PREFILL members, as the existing port tests build them."""

   items = (
      _work_item(
         case,
         0,
         encode_token_ids((1, 2)),
         phase="PREFILL",
         token_span=2,
         path_id="path:batch-1",
         request_id="request:batch-1",
         idempotency_key="request:batch-1:PREFILL:-1:0",
      ),
      _work_item(
         case,
         0,
         encode_token_ids((3, 4)),
         phase="PREFILL",
         token_span=2,
         path_id="path:batch-2",
         request_id="request:batch-2",
         idempotency_key="request:batch-2:PREFILL:-1:0",
      ),
   )
   return items, RuntimeBatch(compatibility_key=items[0].batch_key, items=items)


def test_strong_red_one_batch_is_served_by_one_backend_invocation(
   runtime_case, monkeypatch
):
   """One RuntimeBatch of two members must be one real backend invocation."""

   per_item_calls: list[str] = []
   original = NumpyRuntimePort.execute

   def counting_execute(self, item):
      per_item_calls.append(item.request_id)
      return original(self, item)

   monkeypatch.setattr(NumpyRuntimePort, "execute", counting_execute)
   port = _fresh_ports(runtime_case)[0]
   _, batch = _two_member_batch(runtime_case)

   results = port.execute_batch(batch)

   assert len(results) == 2
   assert len(per_item_calls) == 1, (
      f"{STRONG_RED}: one RuntimeBatch of {len(batch.items)} compatible members is "
      f"served by {len(per_item_calls)} single-item backend invocations "
      f"({per_item_calls}) instead of one real backend batch. "
      "mycelium_router/numpy_runtime.py execute_batch() is a sequential loop "
      "(`tuple(self.execute(item) for item in batch.items)`); the A10 specification "
      "section 4 and the frozen inventory both reject a loop over singleton runtime "
      "calls as runtime batching."
   )


def test_strong_red_runtime_owns_no_batch_observation(runtime_case):
   """The runtime must report the membership it actually executed."""

   port = _fresh_ports(runtime_case)[0]
   _, batch = _two_member_batch(runtime_case)
   port.execute_batch(batch)

   observation = getattr(port, "batch_observation", None)
   if observation is None:
      snapshot = port.kv_snapshot()
      pytest.fail(
         f"{STRONG_RED}: the target runtime exposes no runtime-owned batch "
         "observation. After executing a two-member batch the NumPy target port "
         f"exposes kv_snapshot() with mode={snapshot['mode']!r} and "
         f"active_state_count={snapshot['active_state_count']!r}, and says nothing "
         "about actual batch membership, backend invocation identity, collection and "
         "execution intervals, or per-member outcome (specification section 3: "
         "mycelium.runtime_batch_observation.v1)."
      )

   latest = observation() if callable(observation) else observation
   assert latest.membership_authority == "stage_runtime"
   assert latest.actual_member_count == 2
   assert latest.backend_invocations == 1


def test_strong_red_target_runtime_retains_no_verification_transaction(runtime_case):
   """No target-KV transaction exists, so no commit or rollback is possible."""

   port = _fresh_ports(runtime_case)[0]
   snapshot = port.kv_snapshot()

   transaction = getattr(port, "verification_transaction", None)
   if transaction is None:
      pytest.fail(
         f"{STRONG_RED}: the qualified target runtime owns no temporary target-KV "
         "transaction, so the verifier's commit/rollback semantics do not exist. "
         f"The port reports mode={snapshot['mode']!r} with "
         f"active_state_count={snapshot['active_state_count']!r} and "
         f"applied_operation_count={snapshot['applied_operation_count']!r}; there is "
         "no committed prefix to roll back to and no operation that could roll it "
         "back (specification section 7)."
      )

   assert transaction.commit_states(include=("rollback_to_prefix",))


def test_strong_red_no_multi_position_verification_operation_exists(runtime_case):
   """The bounded multi-position verification operation does not exist."""

   port = _fresh_ports(runtime_case)[0]

   operation = getattr(port, "verify_candidate_positions", None)
   if operation is None:
      pytest.fail(
         f"{STRONG_RED}: the qualified target runtime exposes no bounded "
         "multi-position verification operation. A10 section 7 requires one real "
         "backend call that evaluates all bounded candidate positions with causal "
         "target semantics and returns target-authoritative per-position decisions; "
         "the baseline surface is execute()/execute_batch()/cancel()/close()/"
         "kv_snapshot() only."
      )

   assert callable(operation)


def test_strong_red_sampling_verification_is_not_rejected_by_any_admission(runtime_case):
   """An unsupported generation policy must fail admission with a coded reason."""

   port = _fresh_ports(runtime_case)[0]

   admit = getattr(port, "admit_verification", None)
   if admit is None:
      pytest.fail(
         f"{STRONG_RED}: the target runtime has no verification admission surface at "
         "all, so an unsupported sampling policy cannot be rejected. The frozen "
         "scenario target_verifier_sampling_rejection requires "
         "unsupported_sampling_fails_admission and "
         "generation_policy_never_silently_switches_to_greedy; at this base there is "
         "no generation-policy input anywhere on the target runtime."
      )

   try:
      admit(generation_policy="sampling")
   except Exception as exc:  # noqa: BLE001 - the coded reason is the assertion
      assert getattr(exc, "code", "") == "unsupported_generation_policy", (
         f"{STRONG_RED}: sampling was refused for the wrong reason: {exc!r}"
      )
   else:
      pytest.fail(
         f"{STRONG_RED}: a sampling verification request was silently admitted; "
         "sampling must never switch to greedy and must never proceed."
      )


def test_strong_red_no_stage_compute_interval_surface_exists(runtime_case):
   """Overlap is only provable from physical per-stage compute intervals."""

   port = _fresh_ports(runtime_case)[0]
   result = port.execute(
      _work_item(
         runtime_case,
         0,
         encode_token_ids((1, 2)),
         phase="PREFILL",
         token_span=2,
         path_id="path:intervals",
         request_id="request:intervals",
         idempotency_key="request:intervals:PREFILL:-1:0",
      )
   )
   assert result.success is True

   intervals = getattr(port, "stage_compute_intervals", None)
   if intervals is None:
      pytest.fail(
         f"{STRONG_RED}: no per-stage compute-interval surface exists on the target "
         "runtime, so the uncertainty-aware overlap of specification section 6 can "
         f"never be evidenced from real output. A real execute() result carries only "
         f"{sorted(RuntimeResult.__dataclass_fields__)} -- no stage compute interval, "
         "no clock-correlation uncertainty, no causal edge."
      )

   assert intervals() == ()


# ---------------------------------------------------------------------------
# Product path level (Router drain -> relay -> RuntimePort.execute_batch)
# ---------------------------------------------------------------------------


class CountingRuntimePort(FakeRuntimePort):
   """Sequential test double that counts per-item runtime invocations."""

   def __init__(self, **kwargs):
      super().__init__(**kwargs)
      self.single_item_invocations = 0

   def execute(self, item):
      self.single_item_invocations += 1
      return super().execute(item)


def _router(request_count: int = 2):
   """The existing phase-batching test recipe: two compatible queued requests."""

   graph, base_request, base_manifest = locked_path()
   clock = ManualClock()
   runtime = CountingRuntimePort()
   transport = FakeTransportPort()
   first_placement = base_manifest.ordered_hops[0].placement_id
   placement = next(
      candidate
      for stage in graph.stages
      for candidate in stage.placements
      if candidate.placement_id == first_placement
   )
   router = Router(
      node_id=placement.node_id,
      topology=FakeTopologyProvider(graph),
      device_states=FakeDeviceStateProvider(state_table()),
      capacity=FakeCapacityPort(),
      runtime=runtime,
      transport=transport,
      clock=clock,
      id_source=SequenceIdSource(),
      config=RouterConfig(),
   )
   registrations = []
   for index in range(request_count):
      suffix = chr(ord("a") + index)
      request = replace(
         base_request,
         request_id=f"request-{suffix}",
         qos_class="batch",
         admitted_at=0.0,
         target_ttft_ms=1_000.0,
      )
      manifest = replace(
         base_manifest,
         request_id=request.request_id,
         path_id=f"path-{suffix}",
      )
      assert router.register_path(request, manifest, graph)
      registrations.append((request, manifest))
   return router, clock, runtime, transport, registrations


def test_strong_red_product_drain_batch_is_a_loop_of_singletons():
   """The product drain path proposes one batch and executes two singletons."""

   router, _, runtime, _, registrations = _router(request_count=2)
   for request, manifest in registrations:
      queued = router.enqueue_hop(first_header(request, manifest), b"abcd")
      assert queued.disposition == "QUEUED"

   results = router.drain_ready_batches()

   assert len(results) == 2
   assert len(runtime.executed_batches) == 1
   assert len(runtime.executed_batches[0].items) == 2
   assert runtime.single_item_invocations == 1, (
      f"{STRONG_RED}: the product drain path proposed one batch of "
      f"{len(runtime.executed_batches[0].items)} compatible members "
      f"(decision reason {router.batch_decisions()[-1].reason!r}) but the runtime "
      f"executed {runtime.single_item_invocations} single-item invocations for it. "
      "Runtime batching is a backend-invocation fact, not a proposal fact."
   )


def test_strong_red_product_drain_path_reports_no_runtime_owned_observation():
   """The product path must expose what the runtime actually executed."""

   router, _, runtime, _, registrations = _router(request_count=2)
   for request, manifest in registrations:
      router.enqueue_hop(first_header(request, manifest), b"abcd")
   results = router.drain_ready_batches()
   assert len(results) == 2

   observations = getattr(router, "runtime_batch_observations", None)
   if observations is None:
      pytest.fail(
         f"{STRONG_RED}: the product drain path exposes no runtime-owned batch "
         f"observation. It exposes only relay-side EWMA profiles "
         f"{router.batch_execution_profiles()!r} keyed by (phase, batch_size) and the "
         f"proposal-side decision reason {router.batch_decisions()[-1].reason!r}. "
         "Neither is runtime-owned membership, backend invocation identity, or a "
         "per-member outcome (specification sections 2 and 3)."
      )

   latest = observations()[-1] if callable(observations) else observations[-1]
   assert latest.membership_authority == "stage_runtime"
   assert latest.backend_invocations == 1
   assert latest.actual_member_count == 2


def test_strong_red_sequential_dispatch_is_not_reported_at_all():
   """The test-only sequential fake must always report sequential_dispatch."""

   key = batch_key()
   batch = RuntimeBatch(
      compatibility_key=key,
      items=(
         work("request-a", key=key),
         work("request-b", key=key, token_index=3),
      ),
   )
   runtime = FakeRuntimePort(token_base=100)

   results = runtime.execute_batch(batch)

   assert len(results) == 2
   mode = getattr(runtime, "dispatch_mode", None)
   if mode is None:
      pytest.fail(
         f"{STRONG_RED}: the sequential test double reports no dispatch mode at all, "
         "so the product cannot distinguish a real runtime batch from a loop of "
         "singleton calls. Specification section 2 requires this fake to remain "
         "test-only and to always report `sequential_dispatch`."
      )

   assert mode == "sequential_dispatch", (
      f"{STRONG_RED}: the sequential fake reported {mode!r}; a loop of singleton "
      "calls may never report a runtime batch."
   )


# ---------------------------------------------------------------------------
# Guard: honesty while the verifier is unqualified (passes at a7b412a)
# ---------------------------------------------------------------------------


def test_guard_unqualified_runtime_advertises_no_verifier(runtime_case):
   """An unqualified runtime must not advertise the verifier capability."""

   port = _fresh_ports(runtime_case)[0]
   capability = getattr(port, "target_verification_capability", None)
   if capability is not None:
      binding = getattr(capability, "qualification_binding", None)
      assert binding, (
         "an advertised target-verification capability carries no qualification "
         "binding; specification section 7 forbids advertising it before the exact "
         "selected target runtime physically passes this gate"
      )
