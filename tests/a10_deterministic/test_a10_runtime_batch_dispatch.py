# SPDX-License-Identifier: AGPL-3.0-only
"""A10 runtime-batch dispatch (lane A10-runtime): reachability and parity.

This is the A10-runtime lane's own suite for ``mycelium_router/mlx_runtime.py``.
It proves the live MLX stage runtime can reach the surface this lane built, and
that the surface does what the A10 contract requires:

  * an offered group of N >= 2 members is computed in ONE padded batched backend
    invocation, counted at the real block boundary (never at ``execute()``);
  * the stage runtime publishes ``mycelium.runtime_batch_observation.v1`` with
    ``membership_authority="stage_runtime"``, the invocation count, the proposed
    and actual member counts, and one outcome per actual member;
  * the sequential parity oracle matches batched output against the incumbent
    one-member-at-a-time path numerically -- and is not vacuous: it rejects a
    batch whose numbers differ;
  * a group the runtime cannot serve as one invocation is reported as a split and
    never claims a batch.

Claim boundary: deterministic, in-process evidence over a real (tiny) GPT-2 stage
pair and the real MLX runtime.  No physical qualification, no benchmark, no
product/browser claim, and no A10 closure requirement is made or implied.

Failure modes this module catches (one per test, by name):

  test_reachability_offered_group_is_dispatched_in_one_backend_invocation
      A runtime batch served by a loop of two single-item invocations, or a
      single invocation that silently drops the padding (members padded to the
      widest member must still be computed together).
  test_batched_token_decisions_match_the_sequential_reference
      "Batching" as a label on top of different results: the batched token
      decisions must equal the incumbent per-item decisions, and a decision that
      differs must be caught.
  test_batched_decode_group_is_one_invocation_and_matches_sequential
      Two members advanced by two singleton decode invocations (or by a batched
      pass whose stacked KV is not each member's own committed KV).
  test_padding_and_member_kv_cannot_leak_into_a_result
      Padding or a peer member leaking into a member's output or KV: the same
      member must be identical in differently padded groups, each member's
      committed KV prefix must be its own length, and a result bound to a peer
      member must be caught by the oracle.
  test_batched_prefill_kv_supports_the_same_decode_as_the_sequential_path
      A batched prefill that commits KV which is not the sequential KV.
  test_split_group_is_reported_as_a_split_and_never_claims_a_batch
      An ineligible group wearing a batch label.
  test_incomplete_offered_set_is_never_reported_as_a_runtime_batch
      A runtime batch claimed for a set the runtime never fully computed.
  test_incumbent_runtime_batch_path_is_still_a_loop_and_publishes_nothing
      A change to the incumbent ordered per-item path, or an observation
      published for a loop of singletons.
  test_sequential_parity_oracle_rejects_a_batch_with_different_numbers
      A vacuous oracle that would pass a batch that computed different numbers.
"""

from __future__ import annotations

import hashlib
from dataclasses import replace

import mlx.core as mx
import pytest

import mycelium_router.mlx_runtime as mlx_runtime
from mycelium_router.a10_batch_admission import (
   BatchExecutionReport,
   RuntimeBatchRequest,
   compatibility_digest,
   evaluate_batch_claim,
)
from mycelium_router.a10_runtime_batch import RuntimeBatchObservation
from mycelium_router.contracts import RuntimeBatch
from mycelium_router.mlx_runtime import (
   A10_OBSERVATION_PROTOCOL,
   MLXRuntimePort,
   RUNTIME_BATCH_DISPATCH_MODE,
   SEQUENTIAL_DISPATCH_MODE,
)
from mycelium_router.payloads import encode_activation, encode_token_ids
from test_router_mlx_runtime import (  # noqa: F401 - fixtures for this suite
   _fresh_ports,
   _work_item,
   runtime_case,
)

LEASE = 1_000_000_000_000.0


def _payload_digest(payload: bytes | None) -> str:
   assert isinstance(payload, bytes)
   return hashlib.sha256(payload).hexdigest()


def _activation_payload(token_ids: tuple[int, ...], case) -> bytes:
   """A real stage-0 activation for a real token run (the non-entry input shape)."""

   tokens = mx.array((token_ids,), dtype=mx.uint32)
   hidden = mlx_runtime._execute_loaded_stage(case.loaded[0], token_ids=tokens)
   contiguous = mx.contiguous(hidden)
   mx.eval(contiguous)
   return encode_activation(
      dtype="float32",
      shape=tuple(int(value) for value in contiguous.shape),
      data=bytes(contiguous),
   )


def _prefill(case, stage, token_ids, *, request, path, payload=None, phase="PREFILL"):
   token_index = -1 if phase == "PREFILL" else 0
   return _work_item(
      case,
      stage,
      payload if payload is not None else encode_token_ids(token_ids),
      phase=phase,
      token_span=len(token_ids),
      request_id=request,
      path_id=path,
      token_index=token_index,
      position=0,
      idempotency_key=f"{request}:{phase}:{token_index}:{stage}",
      lease_expires_at=LEASE,
   )


def _request(case, items, *, batch_ref, phase="PREFILL", proposed=None):
   refs = tuple(f"member-{index}" for index in range(len(items)))
   return (
      RuntimeBatchRequest(
         batch_ref=batch_ref,
         phase=phase,
         compatibility_key=compatibility_digest(items[0].batch_key),
         member_refs=refs,
         proposed_member_count=proposed if proposed is not None else len(refs),
      ),
      refs,
   )


def _offer(port, items, refs):
   for item, ref in zip(items, refs):
      port.offer_batch_member(item, member_ref=ref)


def _dispatch(port, request) -> BatchExecutionReport:
   """Dispatch one offered group; the A10 shape returns the runtime's own report."""

   report = port.execute_batch(request)
   assert isinstance(report, BatchExecutionReport), (
      "a RuntimeBatchRequest must be answered with the runtime's own "
      f"BatchExecutionReport, not {type(report)!r}"
   )
   return report


class _BlockCalls:
   """Counts real block invocations and the batch width each one saw."""

   def __init__(self, monkeypatch):
      self.widths: list[int] = []
      original = mlx_runtime._gpt2_block_with_kv

      def counting(hidden, *args, **kwargs):
         self.widths.append(int(hidden.shape[0]))
         return original(hidden, *args, **kwargs)

      monkeypatch.setattr(mlx_runtime, "_gpt2_block_with_kv", counting)


class _ItemCalls:
   """Counts public per-item ``execute()`` invocations."""

   def __init__(self, monkeypatch):
      self.paths: list[str] = []
      original = MLXRuntimePort.execute

      def counting(self, item):
         calls.append(item.path_id)
         return original(self, item)

      calls = self.paths
      monkeypatch.setattr(MLXRuntimePort, "execute", counting)


# ---------------------------------------------------------------------------
# Reachability: the live dispatch path reaches one real backend invocation
# ---------------------------------------------------------------------------


def test_reachability_offered_group_is_dispatched_in_one_backend_invocation(
   runtime_case, monkeypatch
):
   blocks = _BlockCalls(monkeypatch)
   items = _ItemCalls(monkeypatch)
   port = _fresh_ports(runtime_case)[0]
   member_a = _prefill(
      runtime_case, 0, (1, 2), request="request-a", path="path-a"
   )
   member_b = _prefill(
      runtime_case, 0, (3, 4, 5), request="request-b", path="path-b"
   )
   request, refs = _request(
      runtime_case, (member_a, member_b), batch_ref="batch:a10-runtime-1"
   )
   _offer(port, (member_a, member_b), refs)

   report = _dispatch(port, request)

   # ONE real backend invocation, and it saw BOTH members at once.
   assert blocks.widths == [2], (
      f"the padded batched pass ran {len(blocks.widths)} block invocations with "
      f"batch widths {blocks.widths}; two compatible members must be computed "
      "TOGETHER in one invocation of width 2"
   )
   assert items.paths == [], (
      f"the dispatch fell back on per-item execute() calls {items.paths}; a "
      "runtime batch must not be a loop of singletons"
   )
   assert report.dispatch_mode == "runtime_batch"
   assert report.backend_invocations == 1
   assert report.membership_authority == "stage_runtime"
   assert report.arena_open is False
   assert [execution.member_ref for execution in report.executed] == list(refs)
   assert [execution.kv_ref for execution in report.executed] == [
      "kv:path-a:0:placement-000",
      "kv:path-b:0:placement-000",
   ], (
      "each member's result must be bound to its own KV identity, never to a "
      "peer's"
   )
   assert [execution.committed_prefix for execution in report.executed] == [2, 3], (
      "each member's committed KV prefix must be its own token count, so the pad "
      "positions cannot be committed as if they were the member's context"
   )
   assert port.dispatch_mode() == RUNTIME_BATCH_DISPATCH_MODE

   observation = port.batch_observation()
   assert isinstance(observation, RuntimeBatchObservation)
   assert A10_OBSERVATION_PROTOCOL == "mycelium.runtime_batch_observation.v1"
   assert observation.membership_authority == "stage_runtime"
   assert observation.backend_invocations == 1
   assert observation.proposed_member_count == 2
   assert observation.actual_member_count == 2
   assert observation.member_outcomes == (
      ("member-0", "completed"),
      ("member-1", "completed"),
   )
   assert observation.claims_runtime_batch() is True
   assert port.execution_interval() == (
      observation.collected_at,
      observation.executed_at,
   )
   assert port.kv_snapshot()["active_state_count"] == 2

   # The frozen judge accepts this evidence, and still refuses the shortcuts.
   verdict = evaluate_batch_claim(
      source="stage_runtime_observation", observations=(observation,)
   )
   assert (verdict.qualifies, verdict.reason) == (
      True,
      "stage_runtime_observation_owns_membership",
   )
   for source in ("candidate_membership", "concurrent_admission", "sequential_loop"):
      assert evaluate_batch_claim(source=source).qualifies is False


# ---------------------------------------------------------------------------
# Parity: decisions, not tensors
# ---------------------------------------------------------------------------


def test_batched_token_decisions_match_the_sequential_reference(runtime_case):
   port = _fresh_ports(runtime_case)[1]
   item_a = _prefill(
      runtime_case,
      1,
      (1, 2),
      request="request-a",
      path="path-a",
      payload=_activation_payload((1, 2), runtime_case),
   )
   item_b = _prefill(
      runtime_case,
      1,
      (3, 4, 5),
      request="request-b",
      path="path-b",
      payload=_activation_payload((3, 4, 5), runtime_case),
   )
   request, refs = _request(
      runtime_case, (item_a, item_b), batch_ref="batch:a10-runtime-2"
   )
   _offer(port, (item_a, item_b), refs)

   report = _dispatch(port, request)
   assert report.dispatch_mode == "runtime_batch"
   assert report.backend_invocations == 1
   assert all(execution.result.success for execution in report.executed)

   verdict = port.verify_sequential_parity(
      report,
      batch_ref="batch:a10-runtime-2",
      members=dict(zip(refs, (item_a, item_b))),
   )

   assert verdict.all_equal is True
   assert verdict.compared_member_count == 2
   assert verdict.batched_backend_invocations == 1
   for member in verdict.members:
      assert member.decision_equal is True
      assert member.maximum_absolute_deviation == 0.0, (
         f"{member.member_ref} deviated from its sequential reference by "
         f"{member.maximum_absolute_deviation}; batching must not change numbers"
      )

   # Non-vacuity of the decision half: a batched decision that differs from the
   # sequential decision must be caught, not smoothed over.
   tampered = replace(
      report,
      executed=(
         replace(report.executed[0], result=replace(report.executed[0].result, token_id=3)),
         *report.executed[1:],
      ),
   )
   tampered_verdict = port.verify_sequential_parity(
      tampered, batch_ref="batch:a10-runtime-2", members=dict(zip(refs, (item_a, item_b)))
   )
   assert tampered_verdict.all_equal is False
   assert tampered_verdict.members[0].decision_equal is False


def test_padding_and_member_kv_cannot_leak_into_a_result(runtime_case):
   item_a_short = _prefill(runtime_case, 0, (1, 2), request="request-a", path="path-a")
   item_a_wide = _prefill(runtime_case, 0, (1, 2), request="request-a", path="path-a")
   partner_short = _prefill(
      runtime_case, 0, (3, 4, 5), request="request-b", path="path-b"
   )
   partner_wide = _prefill(
      runtime_case,
      0,
      (4, 5, 6, 4, 5),
      request="request-b",
      path="path-b",
   )

   narrow = _fresh_ports(runtime_case)[0]
   request, refs = _request(
      runtime_case, (item_a_short, partner_short), batch_ref="batch:narrow"
   )
   _offer(narrow, (item_a_short, partner_short), refs)
   narrow_report = _dispatch(narrow, request)

   wide = _fresh_ports(runtime_case)[0]
   request, refs = _request(
      runtime_case, (item_a_wide, partner_wide), batch_ref="batch:wide"
   )
   _offer(wide, (item_a_wide, partner_wide), refs)
   wide_report = _dispatch(wide, request)

   assert narrow_report.dispatch_mode == RUNTIME_BATCH_DISPATCH_MODE
   assert wide_report.dispatch_mode == RUNTIME_BATCH_DISPATCH_MODE
   narrow_digest = _payload_digest(narrow_report.executed[0].result.payload)
   wide_digest = _payload_digest(wide_report.executed[0].result.payload)
   assert narrow_digest == wide_digest, (
      "member-0's output changed when its group was padded to a wider member; "
      "padding must never enter a member's result"
   )
   assert narrow_report.executed[0].committed_prefix == 2
   assert wide_report.executed[0].committed_prefix == 2, (
      "member-0's committed KV prefix followed the padding width instead of its "
      "own token count"
   )

   reference = _fresh_ports(runtime_case)[0]
   sequential = reference.execute(item_a_short)
   assert _payload_digest(sequential.payload) == narrow_digest, (
      "the batched member output is not the sequential member output"
   )

   # A result bound to a peer member must be caught: the members' payload digests
   # differ, so relabelling one member's result as its peer's cannot pass.
   members = dict(zip(("member-0", "member-1"), (item_a_short, partner_short)))
   mislabeled = replace(
      narrow_report,
      executed=(
         replace(narrow_report.executed[0], member_ref="member-1"),
         narrow_report.executed[1],
      ),
   )
   mislabeled_verdict = narrow.verify_sequential_parity(
      mislabeled, batch_ref="batch:narrow", members=members
   )
   assert mislabeled_verdict.all_equal is False, (
      "the oracle accepted a batched result bound to a peer member"
   )


def test_batched_prefill_kv_supports_the_same_decode_as_the_sequential_path(
   runtime_case,
):
   member_a = _prefill(runtime_case, 0, (1, 2), request="request-a", path="path-a")
   member_b = _prefill(runtime_case, 0, (3, 4, 5), request="request-b", path="path-b")
   decode = _work_item(
      runtime_case,
      0,
      encode_token_ids((6,)),
      phase="DECODE",
      token_span=1,
      request_id="request-a",
      path_id="path-a",
      token_index=1,
      position=2,
      idempotency_key="request-a:DECODE:1:0",
      lease_expires_at=LEASE,
   )

   batched = _fresh_ports(runtime_case)[0]
   request, refs = _request(
      runtime_case, (member_a, member_b), batch_ref="batch:a10-runtime-4"
   )
   _offer(batched, (member_a, member_b), refs)
   assert _dispatch(batched, request).backend_invocations == 1
   batched_decode = batched.execute(decode)

   sequential = _fresh_ports(runtime_case)[0]
   assert sequential.execute(member_a).success is True
   sequential_decode = sequential.execute(decode)

   assert sequential_decode.success is True
   assert batched_decode.success is True
   assert _payload_digest(batched_decode.payload) == _payload_digest(
      sequential_decode.payload
   ), (
      "the KV committed by the batched prefill does not support the same decode "
      "as the sequentially committed KV"
   )


# ---------------------------------------------------------------------------
# Honesty: splits are splits
# ---------------------------------------------------------------------------


def test_split_group_is_reported_as_a_split_and_never_claims_a_batch(
   runtime_case, monkeypatch
):
   items = _ItemCalls(monkeypatch)
   port = _fresh_ports(runtime_case)[0]
   member_a = _prefill(runtime_case, 0, (1, 2), request="request-a", path="path-a")
   member_b = _prefill(
      runtime_case,
      0,
      (3, 4),
      request="request-b",
      path="path-b",
      phase="RECOVERY_PREFILL",
   )
   request, refs = _request(
      runtime_case, (member_a, member_b), batch_ref="batch:a10-runtime-5"
   )
   _offer(port, (member_a, member_b), refs)

   report = _dispatch(port, request)

   assert report.dispatch_mode == SEQUENTIAL_DISPATCH_MODE, (
      "a group the runtime cannot compute together must not report runtime_batch"
   )
   assert report.backend_invocations == 2
   assert items.paths == ["path-a", "path-b"], (
      "the split must be served one member at a time, on the incumbent path"
   )
   observation = port.batch_observation()
   assert observation is not None
   assert observation.actual_member_count == 2
   assert observation.claims_runtime_batch() is False
   verdict = evaluate_batch_claim(
      source="stage_runtime_observation", observations=(observation,)
   )
   assert (verdict.qualifies, verdict.reason) == (
      False,
      "observation_does_not_claim_runtime_batch",
   )


def test_incomplete_offered_set_is_never_reported_as_a_runtime_batch(runtime_case):
   port = _fresh_ports(runtime_case)[0]
   member_a = _prefill(runtime_case, 0, (1, 2), request="request-a", path="path-a")
   member_b = _prefill(runtime_case, 0, (3, 4), request="request-b", path="path-b")
   _offer(port, (member_a, member_b), ("member-0", "member-1"))
   request = RuntimeBatchRequest(
      batch_ref="batch:a10-runtime-6",
      phase="PREFILL",
      compatibility_key=compatibility_digest(member_a.batch_key),
      member_refs=("member-0", "member-1", "member-2"),
      proposed_member_count=3,
   )

   report = _dispatch(port, request)

   assert report.dispatch_mode == SEQUENTIAL_DISPATCH_MODE
   assert len(report.executed) == 2
   observation = port.batch_observation()
   assert observation is not None
   assert observation.proposed_member_count == 3
   assert observation.actual_member_count == 2
   assert observation.claims_runtime_batch() is False


def test_batched_decode_group_is_one_invocation_and_matches_sequential(
   runtime_case, monkeypatch
):
   """N >= 2 members at the same decode position, computed together once.

   Covers the continuation branch: each member's own committed KV is stacked and
   the whole group is advanced by one real batched invocation.
   """

   blocks = _BlockCalls(monkeypatch)
   items = _ItemCalls(monkeypatch)
   prefill_a = _prefill(runtime_case, 0, (1, 2), request="request-a", path="path-a")
   prefill_b = _prefill(runtime_case, 0, (3, 4), request="request-b", path="path-b")
   decode_a = _work_item(
      runtime_case,
      0,
      encode_token_ids((5,)),
      phase="DECODE",
      token_span=1,
      request_id="request-a",
      path_id="path-a",
      token_index=1,
      position=2,
      idempotency_key="request-a:DECODE:1:0",
      lease_expires_at=LEASE,
   )
   decode_b = _work_item(
      runtime_case,
      0,
      encode_token_ids((6,)),
      phase="DECODE",
      token_span=1,
      request_id="request-b",
      path_id="path-b",
      token_index=1,
      position=2,
      idempotency_key="request-b:DECODE:1:0",
      lease_expires_at=LEASE,
   )
   refs = ("member-0", "member-1")

   port = _fresh_ports(runtime_case)[0]
   assert port.execute(prefill_a).success is True
   assert port.execute(prefill_b).success is True
   request, refs = _request(
      runtime_case, (decode_a, decode_b), batch_ref="batch:a10-runtime-8", phase="DECODE"
   )
   _offer(port, (decode_a, decode_b), refs)

   blocks.widths.clear()
   items.paths.clear()
   report = _dispatch(port, request)

   assert report.dispatch_mode == RUNTIME_BATCH_DISPATCH_MODE
   assert report.backend_invocations == 1
   assert blocks.widths == [2], (
      f"a batched decode of two members ran block widths {blocks.widths}; both "
      "members must advance in ONE invocation"
   )
   assert items.paths == []
   assert [execution.result.success for execution in report.executed] == [True, True]
   assert [execution.committed_prefix for execution in report.executed] == [3, 3]

   sequential = _fresh_ports(runtime_case)[0]
   assert sequential.execute(prefill_a).success is True
   assert sequential.execute(prefill_b).success is True
   reference_a = sequential.execute(decode_a)
   reference_b = sequential.execute(decode_b)
   assert _payload_digest(report.executed[0].result.payload) == _payload_digest(
      reference_a.payload
   )
   assert _payload_digest(report.executed[1].result.payload) == _payload_digest(
      reference_b.payload
   )
   observation = port.batch_observation()
   assert observation is not None
   assert observation.phase == "DECODE"
   assert observation.actual_member_count == 2
   assert observation.claims_runtime_batch() is True


def test_incumbent_runtime_batch_path_is_still_a_loop_and_publishes_nothing(
   runtime_case, monkeypatch
):
   """The legacy ``RuntimeBatch`` path is unchanged and claims no batch.

   Guard, not a new claim: the ordered per-item path must stay a loop, and it
   must publish no observation at all -- missing evidence is absent, never zero
   and never success.
   """

   blocks = _BlockCalls(monkeypatch)
   port = _fresh_ports(runtime_case)[0]
   member_a = _prefill(runtime_case, 0, (1, 2), request="request-a", path="path-a")
   member_b = _prefill(runtime_case, 0, (3, 4), request="request-b", path="path-b")
   batch = RuntimeBatch(
      compatibility_key=member_a.batch_key,
      items=(member_a, member_b),
   )

   results = port.execute_batch(batch)

   assert isinstance(results, tuple) and len(results) == 2
   assert blocks.widths == [1, 1], (
      "the incumbent ordered per-item path changed: it must stay a loop of "
      "single-item invocations"
   )
   assert port.batch_observation() is None, (
      "the incumbent loop published an observation; a loop may never produce "
      "batch evidence"
   )
   assert port.batch_observations() == ()


def test_sequential_parity_oracle_rejects_a_batch_with_different_numbers(
   runtime_case,
):
   port = _fresh_ports(runtime_case)[0]
   member_a = _prefill(runtime_case, 0, (1, 2), request="request-a", path="path-a")
   member_b = _prefill(runtime_case, 0, (3, 4, 5), request="request-b", path="path-b")
   request, refs = _request(
      runtime_case, (member_a, member_b), batch_ref="batch:a10-runtime-7"
   )
   _offer(port, (member_a, member_b), refs)
   report = _dispatch(port, request)
   members = dict(zip(refs, (member_a, member_b)))

   assert (
      port.verify_sequential_parity(
         report, batch_ref=request.batch_ref, members=members
      ).all_equal
      is True
   )

   # Perturb one member's payload by one byte -- the shape and envelope stay
   # valid, so only the oracle's numeric comparison can catch it.
   original = report.executed[0].result
   assert isinstance(original.payload, bytes)
   tampered_payload = original.payload[:-1] + bytes(
      (original.payload[-1] ^ 0x01,)
   )
   tampered = replace(
      report,
      executed=(
         replace(report.executed[0], result=replace(original, payload=tampered_payload)),
         *report.executed[1:],
      ),
   )

   verdict = port.verify_sequential_parity(
      tampered, batch_ref=request.batch_ref, members=members
   )
   assert verdict.all_equal is False, (
      "the sequential parity oracle passed a batch whose payload differs from "
      "the sequential reference: it is vacuous and cannot catch a batch that "
      "computed different numbers"
   )
   assert verdict.members[0].payload_digest_equal is False
   assert all(member.decision_equal for member in verdict.members)
