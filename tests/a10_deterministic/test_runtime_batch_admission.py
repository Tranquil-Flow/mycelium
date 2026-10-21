# SPDX-License-Identifier: AGPL-3.0-only
"""A10 runtime batch admission: candidate versus actual membership, one invocation.

Frozen source (read-only): ``tests/a10_acceptance/scenarios.v1.json``
``runtime_batch_requirements`` — ``final_membership_authority = stage_runtime``,
``minimum_physical_batch_members = 2``, ``backend_invocations_per_observed_batch = 1``,
and the three "alone does not qualify" flags.

These tests are deterministic.  They judge a *dispatch report*, so a sequential loop
of singleton calls and a per-member concurrent dispatch are refused on what they
report rather than on a label.  Physical-positive evidence still needs a live route
and stays RED in ``tests/a10_scaffold``.
"""

from __future__ import annotations

from tests.a10_deterministic import ensure_worktree_imports

# Resolve imports inside this lane's worktree even when another test module
# bound ``mycelium_router`` to the standing checkout first (see __init__).
ensure_worktree_imports()

import dataclasses

import pytest

from mycelium_router import a10_runtime_batch as scaffold
from mycelium_router.a10_batch_admission import (
   CLAIM_SOURCES,
   MINIMUM_OBSERVED_BATCH_MEMBERS,
   BatchClaimVerdict,
   RuntimeBatchAdmission,
   evaluate_batch_claim,
)
from mycelium_router.contracts import RuntimeResult
from tests.a10_deterministic.doubles import (
   ArenaEscapingPort,
   BatchPort,
   ConcurrentMemberPort,
   MisorderedPort,
   PartialCommitPort,
   PeerKvPort,
   SequentialLoopPort,
   SplitPort,
   StaleIncarnationPort,
   UnknownModePort,
   compatibility_key,
   member,
   policy,
)

COLLECT_AT = 0.01
DISPATCH_AT = 0.011


def _admission(port, **policy_overrides) -> RuntimeBatchAdmission:
   return RuntimeBatchAdmission(
      policy=policy(**policy_overrides),
      port=port,
      incarnation="incarnation:1",
      stage_ref="stage-000",
   )


def _two_member_admission(port) -> RuntimeBatchAdmission:
   admission = _admission(port)
   receipt = admission.submit((member("member:a"), member("member:b")), now=0.0)
   assert receipt.accepted == ("member:a", "member:b")
   return admission


def _code(exc: pytest.ExceptionInfo) -> str:
   return exc.value.code


# ---------------------------------------------------------------------------
# Candidate membership is not runtime batching
# ---------------------------------------------------------------------------


def test_candidate_membership_alone_does_not_qualify():
   port = BatchPort()
   admission = _two_member_admission(port)

   collection = admission.collect(now=COLLECT_AT)

   assert collection.candidate_member_refs == ("member:a", "member:b")
   proposal = collection.proposal()
   assert proposal.member_refs == ("member:a", "member:b")
   assert proposal.maximum_members == 8
   # A collect never asks the port to execute anything.
   assert port.calls == []
   assert admission.observations() == ()

   verdict = evaluate_batch_claim(source="candidate_membership")
   assert isinstance(verdict, BatchClaimVerdict)
   assert verdict.qualifies is False
   assert verdict.reason == "candidate_membership_alone_does_not_qualify"


def test_one_observed_batch_is_one_backend_invocation():
   port = BatchPort()
   admission = _two_member_admission(port)
   collection = admission.collect(now=COLLECT_AT)

   outcome = admission.dispatch(collection, now=DISPATCH_AT)

   assert len(port.calls) == 1
   assert port.calls[0].member_refs == ("member:a", "member:b")
   assert port.calls[0].proposed_member_count == 2

   observation = outcome.observation
   assert observation.backend_invocations == 1
   assert observation.membership_authority == "stage_runtime"
   assert observation.proposed_member_count == 2
   assert observation.actual_member_count == 2
   assert observation.member_outcomes == (
      ("member:a", "completed"),
      ("member:b", "completed"),
   )
   assert observation.claims_runtime_batch() is True
   assert MINIMUM_OBSERVED_BATCH_MEMBERS == 2

   verdict = evaluate_batch_claim(
      source="stage_runtime_observation",
      observations=(observation,),
   )
   assert verdict.qualifies is True
   assert verdict.reason == "stage_runtime_observation_owns_membership"


def test_observation_records_candidate_and_actual_membership_distinctly():
   port = BatchPort()
   admission = _two_member_admission(port)
   admission.cancel("member:b", at="pre_launch", now=0.005)
   collection = admission.collect(now=COLLECT_AT)

   outcome = admission.dispatch(collection, now=DISPATCH_AT)

   observation = outcome.observation
   assert observation.proposed_member_count == 2
   assert observation.actual_member_count == 1
   assert observation.actual_member_count < observation.proposed_member_count
   assert observation.claims_runtime_batch() is False
   assert port.calls[0].member_refs == ("member:a",)


# ---------------------------------------------------------------------------
# The three "alone does not qualify" dispatch paths fail closed
# ---------------------------------------------------------------------------


def test_sequential_loop_alone_does_not_qualify():
   port = SequentialLoopPort()
   admission = _two_member_admission(port)
   collection = admission.collect(now=COLLECT_AT)

   with pytest.raises(scaffold.A10ContractError) as raised:
      admission.dispatch(collection, now=DISPATCH_AT)
   assert _code(raised) == "sequential_dispatch_does_not_qualify"
   assert admission.observations() == ()

   verdict = evaluate_batch_claim(source="sequential_loop")
   assert verdict.qualifies is False
   assert verdict.reason == "sequential_loop_alone_does_not_qualify"


def test_concurrent_member_dispatch_alone_does_not_qualify():
   port = ConcurrentMemberPort()
   admission = _two_member_admission(port)
   collection = admission.collect(now=COLLECT_AT)

   with pytest.raises(scaffold.A10ContractError) as raised:
      admission.dispatch(collection, now=DISPATCH_AT)
   assert _code(raised) == "concurrent_admission_alone_does_not_qualify"
   assert admission.observations() == ()

   verdict = evaluate_batch_claim(source="concurrent_admission")
   assert verdict.qualifies is False
   assert verdict.reason == "concurrent_admission_alone_does_not_qualify"


def test_two_singleton_dispatches_are_not_one_runtime_batch():
   port = BatchPort()
   admission = _admission(port)
   observations = []
   for member_ref in ("member:a", "member:b"):
      assert admission.submit((member(member_ref),), now=0.0).accepted == (member_ref,)
      collection = admission.collect(now=COLLECT_AT)
      observations.append(admission.dispatch(collection, now=DISPATCH_AT).observation)

   assert len(port.calls) == 2
   assert [item.actual_member_count for item in observations] == [1, 1]
   assert all(item.claims_runtime_batch() is False for item in observations)

   verdict = evaluate_batch_claim(
      source="stage_runtime_observation", observations=tuple(observations)
   )
   assert verdict.qualifies is False
   assert verdict.reason == "observation_evidence_requires_exactly_one_observed_batch"


def test_unknown_claim_source_is_refused():
   with pytest.raises(scaffold.A10ContractError) as raised:
      evaluate_batch_claim(source="modeled_schedule")
   assert _code(raised) == "unknown_batch_claim_source"
   assert CLAIM_SOURCES == (
      "candidate_membership",
      "concurrent_admission",
      "sequential_loop",
      "stage_runtime_observation",
   )


# ---------------------------------------------------------------------------
# Runtime-reported defects fail closed before any observation exists
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
   "port_factory,code",
   [
      (SplitPort, "split_backend_invocations_fail_closed"),
      (MisorderedPort, "member_identity_mismatch_fails_closed"),
      (StaleIncarnationPort, "stale_incarnation_fails_closed"),
      (ArenaEscapingPort, "invocation_arena_escaped"),
      (PeerKvPort, "cross_member_kv_isolation_fails_closed"),
      (UnknownModePort, "invalid_dispatch_mode"),
   ],
   ids=[
      "split",
      "misordered",
      "stale_incarnation",
      "arena_escaped",
      "peer_kv",
      "unknown_mode",
   ],
)
def test_runtime_reported_defects_fail_closed(port_factory, code):
   port = port_factory()
   admission = _two_member_admission(port)
   collection = admission.collect(now=COLLECT_AT)

   with pytest.raises(scaffold.A10ContractError) as raised:
      admission.dispatch(collection, now=DISPATCH_AT)
   assert _code(raised) == code
   assert admission.observations() == ()


def test_partial_kv_commit_fails_closed_and_rolls_every_uncommitted_member_back():
   port = PartialCommitPort(
      committed={"member:a": 4, "member:b": 4}, partial=("member:b",)
   )
   admission = _two_member_admission(port)
   baseline = admission.kv_snapshot()
   collection = admission.collect(now=COLLECT_AT)

   with pytest.raises(scaffold.A10ContractError) as raised:
      admission.dispatch(collection, now=DISPATCH_AT)
   assert _code(raised) == "partial_kv_commit_fails_closed"
   assert admission.observations() == ()
   assert admission.kv_snapshot() == baseline


def test_nonfinite_member_output_fails_closed():
   port = BatchPort()
   admission = _two_member_admission(port)

   def _nonfinite(request):
      report = BatchPort.execute_batch(port, request)
      broken = report.executed[1]
      return dataclasses.replace(
         report,
         executed=(
            report.executed[0],
            dataclasses.replace(
               broken, result=RuntimeResult(success=True, payload=float("nan"))
            ),
         ),
      )

   port.execute_batch = _nonfinite  # type: ignore[method-assign]
   collection = admission.collect(now=COLLECT_AT)
   with pytest.raises(scaffold.A10ContractError) as raised:
      admission.dispatch(collection, now=DISPATCH_AT)
   assert _code(raised) == "nonfinite_member_output_fails_closed"


def test_per_member_failure_fails_only_that_member():
   port = BatchPort(failures=("member:b",))
   admission = _two_member_admission(port)
   collection = admission.collect(now=COLLECT_AT)

   outcome = admission.dispatch(collection, now=DISPATCH_AT)

   assert outcome.observation.member_outcomes == (
      ("member:a", "completed"),
      ("member:b", "failed"),
   )
   assert outcome.forwarded == ("member:a",)
   # The shared invocation still executed both members in one call.
   assert len(port.calls) == 1
   assert outcome.observation.backend_invocations == 1


def test_dispatch_is_single_shot_per_collection():
   port = BatchPort()
   admission = _two_member_admission(port)
   collection = admission.collect(now=COLLECT_AT)
   admission.dispatch(collection, now=DISPATCH_AT)

   with pytest.raises(scaffold.A10ContractError) as raised:
      admission.dispatch(collection, now=DISPATCH_AT + 1.0)
   assert _code(raised) == "batch_already_dispatched"
   assert len(port.calls) == 1


def test_incompatible_members_are_never_offered_to_one_batch():
   port = BatchPort()
   admission = _admission(port)
   admission.submit(
      (
         member("member:a", compatibility_key=compatibility_key()),
         member(
            "member:b",
            compatibility_key=compatibility_key(placement_id="placement-b"),
         ),
      ),
      now=0.0,
   )

   collection = admission.collect(now=COLLECT_AT)

   assert collection.candidate_member_refs == ("member:a",)
   assert collection.excluded == (("member:b", "incompatible_compatibility_key"),)
   admission.dispatch(collection, now=DISPATCH_AT)
   assert port.calls[0].member_refs == ("member:a",)

   second = admission.collect(now=COLLECT_AT + 1.0)
   assert second.candidate_member_refs == ("member:b",)


def test_execution_boundaries_are_implemented_and_qualification_still_refuses():
   """The deterministic execution entry points are implemented; qualification refuses.

   The physical positives still stay RED: this lane supplies the runtime-owned
   execution, overlap and verification semantics in-process, and claims no physical
   route, no benchmark and no qualification.
   """

   codes = dict(scaffold.scaffold_status().boundaries)
   for implemented in (
      "execute_observed_batch",
      "observe_pipeline_overlap",
      "verify_candidate_positions",
      "sequential_parity_reference",
   ):
      assert implemented not in codes, f"{implemented} is implemented, not a refusal"
   assert codes["qualify_runtime_batch"] == "runtime_batch_qualification_absent"
   assert codes["qualify_target_verifier"] == "target_verification_qualification_absent"
