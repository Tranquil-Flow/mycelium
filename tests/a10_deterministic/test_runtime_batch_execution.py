# SPDX-License-Identifier: AGPL-3.0-only
"""A10 runtime batch execution: the implemented runtime-owned batch path.

This suite covers the behaviour behind the contract's ``execute_observed_batch``
entry point (implementation contract sections 2 and 3, slice S1) and the overlap
entry point ``observe_pipeline_overlap`` (slice S2).

Everything here is deterministic: an in-process dispatch double reports honestly
what it did -- one real invocation, a split, a sequential loop, a member-concurrent
dispatch -- and the entry point is judged on that report.  A mode label never
travels without its evidence.  No socket, no child process, no model artifact and
no physical route is used, and nothing here is physical or qualification evidence.
"""

from __future__ import annotations

from tests.a10_deterministic import ensure_worktree_imports

# The lane worktree must win the import race even when another test module already
# bound ``mycelium_router`` to the standing checkout (see tests/a10_deterministic).
ensure_worktree_imports()

import pytest

from mycelium_router import a10_runtime_batch as scaffold
from mycelium_router.a10_batch_admission import (
   BatchExecutionReport,
   MemberExecution,
   evaluate_batch_claim,
)
from mycelium_router.a10_overlap_trace import OverlapTrace, OverlapVerdict
from mycelium_router.a10_runtime_execution import (
   ObservedBatchExecution,
   execute_observed_batch,
   observe_pipeline_overlap,
)
from mycelium_router.contracts import RuntimeResult
from tests.a10_deterministic.doubles import compatibility_key
from tests.a10_deterministic.test_overlap_trace_rejection import _chain, _trace

INTERACTIVE = "interactive"
BATCH = "batch"


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def proposal(member_count: int = 2, **overrides):
   refs = tuple(f"member:{index}" for index in range(member_count))
   values = {
      "compatibility_key": compatibility_key(),
      "member_refs": refs,
      "qos_classes": tuple(
         INTERACTIVE if index == 0 else BATCH for index in range(member_count)
      ),
      "maximum_members": 8,
      "maximum_bytes": 1 << 20,
      "proposed_at": 0.0,
      "collection_deadline_at": 0.002,
   }
   values.update(overrides)
   return scaffold.RuntimeBatchProposal(**values)


def executed(
   member_ref: str,
   *,
   success: bool = True,
   committed_prefix: int = 3,
   partial_commit: bool = False,
   failure_scope: str = "",
   failure_reason: str = "",
) -> MemberExecution:
   return MemberExecution(
      member_ref=member_ref,
      kv_ref=f"kv:{member_ref}",
      result=RuntimeResult(
         success=success,
         failure_scope=failure_scope,
         failure_reason=failure_reason,
      ),
      committed_prefix=committed_prefix,
      partial_commit=partial_commit,
   )


def both_members(**overrides) -> tuple[MemberExecution, ...]:
   values = {"member_ref": "member:0"}, {"member_ref": "member:1"}
   return tuple(executed(**value) for value in values)  # type: ignore[arg-type]


class Port:
   """A dispatch double that reports exactly what it was told to report."""

   def __init__(
      self,
      *,
      mode: str = "runtime_batch",
      invocations: int = 1,
      members: tuple[MemberExecution, ...] | None = None,
      authority: str = "stage_runtime",
      incarnation: str = "incarnation:one",
      interval: tuple[float, float] = (0.0, 0.5),
      arena_open: bool = False,
   ) -> None:
      self.mode = mode
      self.invocations = invocations
      self.members = both_members() if members is None else members
      self.authority = authority
      self.incarnation = incarnation
      self.interval = interval
      self.arena_open = arena_open
      self.requests: list[object] = []

   def dispatch_mode(self) -> str:
      return self.mode

   def execution_interval(self) -> tuple[float, float]:
      return self.interval

   def execute_batch(self, request) -> BatchExecutionReport:
      self.requests.append(request)
      return BatchExecutionReport(
         dispatch_mode=self.mode,
         backend_invocations=self.invocations,
         membership_authority=self.authority,
         incarnation=self.incarnation,
         executed=self.members,
         arena_open=self.arena_open,
      )


class BarePort(Port):
   """A port that reports no execution interval at all."""

   execution_interval = None


def code_of(exc: pytest.ExceptionInfo) -> str:
   return exc.value.code


# ---------------------------------------------------------------------------
# One real backend invocation over accepted members
# ---------------------------------------------------------------------------


def test_a_runtime_batch_of_two_members_is_one_invocation_that_claims_a_batch():
   port = Port()
   execution = execute_observed_batch(port, proposal())

   assert isinstance(execution, ObservedBatchExecution)
   assert len(port.requests) == 1, "the proposal is offered exactly once"
   assert execution.claims_runtime_batch is True
   observation = execution.observation
   assert observation.membership_authority == "stage_runtime"
   assert observation.proposed_member_count == 2
   assert observation.actual_member_count == 2
   assert observation.backend_invocations == 1
   assert observation.member_outcomes == (
      ("member:0", "completed"),
      ("member:1", "completed"),
   )


def test_the_observation_is_runtime_owned_not_proposal_owned():
   port = Port()
   execution = execute_observed_batch(port, proposal())
   observation = execution.observation
   assert observation.batch_ref == execution.batch_ref
   assert observation.phase == "DECODE"
   assert observation.collected_at == 0.0
   assert observation.executed_at == 0.5
   assert observation.cleanup == "released"
   assert observation.backend_invocation_id


def test_the_offer_carries_the_declared_width_and_the_opaque_member_refs():
   port = Port()
   execute_observed_batch(port, proposal())
   request = port.requests[0]
   assert request.member_refs == ("member:0", "member:1")
   assert request.proposed_member_count == 2
   assert request.batch_ref


def test_each_actual_member_gets_exactly_one_isolated_result():
   port = Port()
   execution = execute_observed_batch(port, proposal())
   refs = tuple(ref for ref, _ in execution.results)
   assert refs == ("member:0", "member:1"), "scatter order follows the offer"
   assert tuple(result.kv_ref for result in execution.member_executions) == (
      "kv:member:0",
      "kv:member:1",
   ), "cross-member KV identity is never shared"


# ---------------------------------------------------------------------------
# Dispatch-mode honesty: a mode label may never travel without its evidence
# ---------------------------------------------------------------------------


def test_a_sequential_loop_is_reported_honestly_and_never_claims_a_batch():
   port = Port(mode="sequential_dispatch", invocations=2)
   execution = execute_observed_batch(port, proposal())

   assert execution.observation.backend_invocations == 2
   assert execution.dispatch_mode == "sequential_dispatch"
   assert execution.claims_runtime_batch is False


def test_member_concurrent_dispatch_never_claims_a_batch():
   port = Port(mode="member_concurrent_dispatch", invocations=2)
   execution = execute_observed_batch(port, proposal())

   assert execution.observation.backend_invocations == 2
   assert execution.claims_runtime_batch is False


def test_a_split_runtime_batch_is_not_a_runtime_batch():
   port = Port(mode="runtime_batch", invocations=2)
   execution = execute_observed_batch(port, proposal())

   assert execution.observation.backend_invocations == 2
   assert execution.claims_runtime_batch is False, (
      "a group that was split into two invocations is not a runtime batch"
   )


def test_a_sequential_report_that_claims_one_invocation_fails_closed():
   port = Port(mode="sequential_dispatch", invocations=1)
   with pytest.raises(scaffold.A10ContractError) as raised:
      execute_observed_batch(port, proposal())
   assert code_of(raised) == "inconsistent_dispatch_report"


def test_an_unknown_dispatch_mode_fails_closed():
   port = Port(mode="candidate_membership", invocations=1)
   with pytest.raises(scaffold.A10ContractError) as raised:
      execute_observed_batch(port, proposal())
   assert code_of(raised) == "unknown_dispatch_mode"


def test_the_frozen_claim_source_rules_apply_to_the_observation():
   claiming = execute_observed_batch(Port(), proposal())
   assert evaluate_batch_claim(
      source="stage_runtime_observation",
      observations=(claiming.observation,),
   ).qualifies is True

   sequential = execute_observed_batch(
      Port(mode="sequential_dispatch", invocations=2), proposal()
   )
   verdict = evaluate_batch_claim(
      source="stage_runtime_observation",
      observations=(sequential.observation,),
   )
   assert verdict.qualifies is False


# ---------------------------------------------------------------------------
# Membership truth: what the runtime actually executed
# ---------------------------------------------------------------------------


def test_a_member_executed_but_not_proposed_fails_closed():
   port = Port(members=(executed("member:0"), executed("member:elsewhere")))
   with pytest.raises(scaffold.A10ContractError) as raised:
      execute_observed_batch(port, proposal())
   assert code_of(raised) == "unknown_member_in_observation"


def test_a_duplicate_actual_member_fails_closed():
   port = Port(members=(executed("member:0"), executed("member:0")))
   with pytest.raises(scaffold.A10ContractError) as raised:
      execute_observed_batch(port, proposal())
   assert code_of(raised) == "duplicate_member_outcome"


def test_zero_actual_members_fails_closed_and_is_never_a_success():
   port = Port(members=())
   with pytest.raises(scaffold.A10ContractError) as raised:
      execute_observed_batch(port, proposal())
   assert code_of(raised) == "empty_observed_batch"


def test_a_reordered_scatter_fails_closed():
   port = Port(members=(executed("member:1"), executed("member:0")))
   with pytest.raises(scaffold.A10ContractError) as raised:
      execute_observed_batch(port, proposal())
   assert code_of(raised) == "wrong_result_count_or_order"


def test_a_partial_kv_commit_fails_closed():
   port = Port(
      members=(executed("member:0"), executed("member:1", partial_commit=True))
   )
   with pytest.raises(scaffold.A10ContractError) as raised:
      execute_observed_batch(port, proposal())
   assert code_of(raised) == "partial_kv_commit_fails_closed"


def test_an_unreleased_invocation_arena_fails_closed():
   port = Port(arena_open=True)
   with pytest.raises(scaffold.A10ContractError) as raised:
      execute_observed_batch(port, proposal())
   assert code_of(raised) == "unreleased_invocation_arena"


def test_a_stale_runtime_incarnation_fails_closed():
   port = Port(incarnation="incarnation:two")
   with pytest.raises(scaffold.A10ContractError) as raised:
      execute_observed_batch(
         port, proposal(), expected_incarnation="incarnation:one"
      )
   assert code_of(raised) == "stale_runtime_incarnation"


def test_a_missing_execution_interval_report_fails_closed():
   port = BarePort()
   with pytest.raises(scaffold.A10ContractError) as raised:
      execute_observed_batch(port, proposal())
   assert code_of(raised) == "missing_execution_interval_report"


def test_per_member_failure_and_cancellation_stay_per_member():
   port = Port(
      members=(
         executed("member:0"),
         executed(
            "member:1",
            success=False,
            failure_scope="member_cancelled",
            failure_reason="cancelled_before_forwarding",
         ),
      )
   )
   execution = execute_observed_batch(port, proposal())
   assert execution.observation.member_outcomes == (
      ("member:0", "completed"),
      ("member:1", "cancelled"),
   )
   assert execution.claims_runtime_batch is True


def test_a_member_failure_is_not_reported_as_completed():
   port = Port(members=(executed("member:0"), executed("member:1", success=False)))
   execution = execute_observed_batch(port, proposal())
   assert execution.observation.member_outcomes[1] == ("member:1", "failed")


# ---------------------------------------------------------------------------
# Coded arguments and the honest boundary that remains
# ---------------------------------------------------------------------------


def test_a_non_proposal_argument_is_rejected_with_its_code():
   with pytest.raises(scaffold.A10ContractError) as raised:
      execute_observed_batch(Port(), object())
   assert code_of(raised) == "invalid_runtime_batch_proposal_argument"


def test_a_port_that_cannot_dispatch_is_rejected_with_its_code():
   with pytest.raises(scaffold.A10ContractError) as raised:
      execute_observed_batch(object(), proposal())
   assert code_of(raised) == "invalid_runtime_batch_port"


def test_runtime_batch_qualification_still_refuses_and_earns_nothing():
   with pytest.raises(scaffold.A10NotImplementedError) as raised:
      scaffold.qualify_runtime_batch(
         scaffold.QualificationRequest(
            owner_private_root="private:root",
            benchmark_manifest_digest="sha256:" + "a" * 64,
            acceptance_inventory_digest="sha256:" + "b" * 64,
            baseline_windows=16,
            candidate_windows=16,
            valid_pairs=12,
         )
      )
   assert raised.value.code == "not_implemented:runtime_batch_qualification_absent"


# ---------------------------------------------------------------------------
# Overlap verdict
# ---------------------------------------------------------------------------


def test_a_full_trace_produces_the_uncertainty_aware_verdict():
   trace = _trace(chains=(_chain(), _chain("microbatch:two", "request:two")))
   verdict = observe_pipeline_overlap(trace)
   assert isinstance(verdict, OverlapVerdict)
   assert verdict.observed is True
   assert verdict.reason == "observed_stage_compute_intersection"


def test_a_trace_with_no_stage_compute_intersection_is_not_observed():
   trace = _trace(chains=(_chain(),))
   verdict = observe_pipeline_overlap(trace)
   assert verdict.observed is False


def test_an_invalid_trace_is_refused_with_its_coded_reason():
   trace = _trace(
      chains=(
         _chain(),
         _chain("microbatch:two", "request:two", stages=2),
      ),
      stage_count=1,
   )
   from mycelium_router.a10_overlap_trace import A10TraceError

   with pytest.raises(A10TraceError):
      observe_pipeline_overlap(trace)


def test_an_overlap_record_without_causal_binding_is_refused():
   record = scaffold.PipelineOverlapTrace(
      trace_ref="trace:one",
      intervals=(
         scaffold.StageComputeInterval(
            interval_ref="interval:one",
            request_ref="request:one",
            microbatch_ref="microbatch:one",
            stage_id="stage-000",
            placement_id="placement-000",
            started_at=0.0,
            finished_at=1.0,
            clock_uncertainty=0.0,
         ),
      ),
      causal_edges=(),
   )
   with pytest.raises(scaffold.A10ContractError) as raised:
      observe_pipeline_overlap(record)
   assert code_of(raised) == "incomplete_overlap_trace"


def test_a_non_trace_argument_is_rejected_with_its_code():
   with pytest.raises(scaffold.A10ContractError) as raised:
      observe_pipeline_overlap(object())
   assert code_of(raised) == "invalid_pipeline_overlap_trace_argument"
