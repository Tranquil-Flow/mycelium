# SPDX-License-Identifier: AGPL-3.0-only
"""A10 runtime batch execution: the implemented behaviour behind ``execute_observed_batch``.

The A10 specification (section 4) and the implementation contract (resolutions 1-3)
make runtime batching a *backend-invocation* fact, not a proposal fact.  The runtime,
not the queue or the planner, is the authority for actual membership; one offered
group may only claim a runtime batch when it was served by exactly one real backend
invocation over at least two accepted members.  A split, a sequential loop, a
member-concurrent dispatch and a concurrent admission all report honestly and never
claim a batch.

This module holds the real deterministic mechanics; ``mycelium_router.a10_runtime_batch``
keeps the declared entry points and delegates here, so the machine-source surface named
by the implementation contract is unchanged while the executable body stays small and
inspectable.

Claim boundary: this is deterministic, in-process behaviour over a dispatch port that
reports what it actually did.  It is not physical overlap evidence, not a benchmark, not
a browser or product claim and not qualification.  ``qualify_runtime_batch`` decides
only from executed live evidence (``a10_qualification``), and nothing here advertises an
unqualified capability.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from mycelium_router.a10_batch_admission import (
   DISPATCH_MODES,
   BatchDispatchPort,
   BatchExecutionReport,
   BatchClaimVerdict,
   MemberExecution,
   RuntimeBatchRequest,
   compatibility_digest,
   evaluate_batch_claim,
)
from mycelium_router.a10_overlap_trace import (
   OverlapTrace,
   OverlapVerdict,
   observe_stage_compute_overlap,
)
from mycelium_router.a10_runtime_batch import (
   BACKEND_INVOCATIONS_PER_OBSERVED_BATCH,
   MEMBERSHIP_AUTHORITY_STAGE_RUNTIME,
   A10ContractError,
   PipelineOverlapTrace,
   RuntimeBatchObservation,
   RuntimeBatchProposal,
   _reject,
)
from mycelium_router.contracts import RuntimeResult


PROTOCOL = "mycelium.a10_runtime_execution.v1"

#: A dispatch mode whose members are served one call at a time.  Such a report is
#: honest about being a loop and can never claim a runtime batch.
SINGLETON_DISPATCH_MODES = ("sequential_dispatch", "member_concurrent_dispatch")

_CANCELLED_SCOPES = ("member_cancelled", "cancelled")


@dataclass(frozen=True)
class ObservedBatchExecution:
   """The runtime's own account of one offered group, with the isolated results.

   ``claim`` is the frozen inventory's verdict on this evidence; the execution is
   never a batch merely because a group was offered.
   """

   batch_ref: str
   phase: str
   dispatch_mode: str
   stage: str
   observation: RuntimeBatchObservation
   member_executions: tuple[MemberExecution, ...]
   results: tuple[tuple[str, RuntimeResult], ...]
   claim: BatchClaimVerdict

   @property
   def member_refs(self) -> tuple[str, ...]:
      return tuple(member.member_ref for member in self.member_executions)

   @property
   def claims_runtime_batch(self) -> bool:
      return self.observation.claims_runtime_batch()

   @property
   def forwarded(self) -> tuple[str, ...]:
      return tuple(
         member.member_ref
         for member in self.member_executions
         if member.result.success
      )


def _require_dispatch_port(port: object) -> BatchDispatchPort:
   if not isinstance(port, BatchDispatchPort):
      _reject("invalid_runtime_batch_port")
   return port


def _require_invocation_count(report: BatchExecutionReport, *, mode: str, actual: int) -> int:
   reported = report.backend_invocations
   if isinstance(reported, bool) or not isinstance(reported, int) or reported < 1:
      _reject("invalid_backend_invocation_count")
   if mode == "runtime_batch":
      # A runtime batch is exactly one invocation.  A larger count is a split, which
      # is reported honestly and is not a runtime batch -- never smoothed to one.
      return reported
   # A singleton loop or a member-concurrent dispatch must report one invocation per
   # member; a report of one invocation over many members is internally inconsistent.
   if reported != actual:
      _reject("inconsistent_dispatch_report")
   return reported


def _read_interval(reporter: Any) -> tuple[float, float]:
   interval = reporter()
   if (
      not isinstance(interval, tuple)
      or len(interval) != 2
      or any(
         isinstance(value, bool)
         or not isinstance(value, (int, float))
         or not math.isfinite(float(value))
         for value in interval
      )
   ):
      _reject("invalid_observation_intervals")
   collected_at = float(interval[0])
   executed_at = float(interval[1])
   if executed_at < collected_at:
      _reject("invalid_observation_intervals")
   return collected_at, executed_at


def _outcome_for(result: RuntimeResult) -> str:
   if result.success:
      return "completed"
   if (
      result.failure_scope in _CANCELLED_SCOPES
      or result.failure_reason.startswith("cancelled")
   ):
      return "cancelled"
   return "failed"


def execute_observed_batch(
   port: BatchDispatchPort,
   proposal: RuntimeBatchProposal,
   *,
   expected_incarnation: str | None = None,
) -> ObservedBatchExecution:
   """Offer ``proposal`` to the stage runtime and publish what it actually executed.

   The group is offered exactly once.  The returned observation is built only from the
   runtime's own report: proposed membership from the proposal, actual membership,
   backend-invocation count, per-member outcome and the measured collection/execution
   interval from the port.  Wrong result count or order, a member that was not
   proposed, a duplicate actual member, a zero-member execution, a partial KV commit
   and an unreleased invocation arena all fail closed.
   """

   if not isinstance(proposal, RuntimeBatchProposal):
      _reject("invalid_runtime_batch_proposal_argument")
   port = _require_dispatch_port(port)

   interval_reporter = getattr(port, "execution_interval", None)
   if not callable(interval_reporter):
      _reject("missing_execution_interval_report")

   mode = port.dispatch_mode()
   if mode not in DISPATCH_MODES:
      _reject("unknown_dispatch_mode")

   key = proposal.compatibility_key
   request = RuntimeBatchRequest(
      batch_ref=f"batch:{compatibility_digest(key)[7:23]}",
      phase=key.phase,
      compatibility_key=compatibility_digest(key),
      member_refs=proposal.member_refs,
      proposed_member_count=len(proposal.member_refs),
   )

   #: The single backend invocation this operation is allowed to make.
   report = port.execute_batch(request)
   if not isinstance(report, BatchExecutionReport):
      _reject("invalid_batch_execution_report")
   if report.dispatch_mode != mode:
      _reject("inconsistent_dispatch_report")
   if report.membership_authority != MEMBERSHIP_AUTHORITY_STAGE_RUNTIME:
      _reject("invalid_membership_authority")
   if expected_incarnation is not None and report.incarnation != expected_incarnation:
      _reject("stale_runtime_incarnation")
   if report.arena_open:
      _reject("unreleased_invocation_arena")

   actual = tuple(report.executed)
   refs = tuple(member.member_ref for member in actual)
   if not refs:
      _reject("empty_observed_batch")
   if len(set(refs)) != len(refs):
      _reject("duplicate_member_outcome")
   if any(ref not in proposal.member_refs for ref in refs):
      _reject("unknown_member_in_observation")
   offered_positions = [proposal.member_refs.index(ref) for ref in refs]
   if offered_positions != sorted(offered_positions):
      _reject("wrong_result_count_or_order")
   if any(member.partial_commit for member in actual):
      _reject("partial_kv_commit_fails_closed")
   if any(not isinstance(member.result, RuntimeResult) for member in actual):
      _reject("invalid_member_result_fails_closed")

   invocations = _require_invocation_count(
      report, mode=mode, actual=len(refs)
   )
   if mode == "runtime_batch" and invocations == BACKEND_INVOCATIONS_PER_OBSERVED_BATCH:
      pass  # the only shape that can claim a batch
   collected_at, executed_at = _read_interval(interval_reporter)

   observation = RuntimeBatchObservation(
      batch_ref=request.batch_ref,
      phase=request.phase,
      stage=report.incarnation,
      backend_invocation_id=(
         f"invocation:{request.batch_ref}:{invocations}"
      ),
      backend_invocations=invocations,
      membership_authority=MEMBERSHIP_AUTHORITY_STAGE_RUNTIME,
      proposed_member_count=len(proposal.member_refs),
      actual_member_count=len(refs),
      member_outcomes=tuple(
         (member.member_ref, _outcome_for(member.result)) for member in actual
      ),
      collected_at=collected_at,
      executed_at=executed_at,
      cleanup="released",
   )

   return ObservedBatchExecution(
      batch_ref=request.batch_ref,
      phase=request.phase,
      dispatch_mode=mode,
      stage=report.incarnation,
      observation=observation,
      member_executions=actual,
      results=tuple((member.member_ref, member.result) for member in actual),
      claim=evaluate_batch_claim(
         source="stage_runtime_observation",
         observations=(observation,),
      ),
   )


def observe_pipeline_overlap(trace: object) -> OverlapVerdict:
   """Return the uncertainty-aware overlap verdict for a fully bound trace.

   Overlap is ``observed`` only when two physical stage-compute intervals for
   different microbatches intersect for a positive duration after clock uncertainty
   (contract resolution 9), on a trace that carries the causal, activation, stage and
   final-loopback binding of specification section 6.

   The declared ``PipelineOverlapTrace`` record cannot carry that binding -- it holds
   interval shape and causal-edge references only -- so it can never produce an overlap
   verdict; it is refused with ``incomplete_overlap_trace`` rather than being treated as
   a trace that merely shows no overlap.
   """

   if isinstance(trace, OverlapTrace):
      return observe_stage_compute_overlap(trace)
   if isinstance(trace, PipelineOverlapTrace):
      _reject("incomplete_overlap_trace")
   _reject("invalid_pipeline_overlap_trace_argument")


__all__ = [
   "PROTOCOL",
   "SINGLETON_DISPATCH_MODES",
   "ObservedBatchExecution",
   "execute_observed_batch",
   "observe_pipeline_overlap",
   "A10ContractError",
]
