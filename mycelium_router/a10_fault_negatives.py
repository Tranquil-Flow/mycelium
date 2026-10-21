# SPDX-License-Identifier: AGPL-3.0-only
"""CPU-deterministic execution and judgement of the nine A10 fault-hook negatives.

``a10_fault_hooks`` injects the fault; this module runs one scenario per fault
against the real A10 mechanics and decides whether the system failed closed
*within the declared scope* and left *zero residue*:

* the dispatch faults drive ``RuntimeBatchAdmission`` (bounded queue, collection,
  fail-closed dispatch, per-member KV registry, gateway buffers) through
  ``FaultInjectingDispatchPort`` around a deterministic reference runtime;
* ``verifier_rollback`` drives ``verify_candidate_positions`` (target-owned KV
  transaction) through ``FaultInjectingVerificationPort``.

Every scenario also runs an unrelated *peer placement* (its own admission and
runtime) and, on the faulted placement, a *follow-up* group, because "within
scope" means unrelated work is not poisoned.  Residue is measured from the
admission's own resource accounting and the KV registry after the scenario, never
asserted.

Claim boundary: this is the deterministic CPU surface of the hook.  The runtime is
a reference double, so a pass here proves the fault hook and the A10 fail-closed
mechanics, not the physical two-Mac route.  Every result is therefore marked
``surface = cpu_fault_hook`` and ``physical_pending = True``; the gate decision
keeps that visible (``scripts/run_a10_physical_gate.py``).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from mycelium_router.a10_batch_admission import (
   BatchExecutionReport,
   BatchMember,
   MemberExecution,
   RuntimeBatchAdmission,
   RuntimeBatchAdmissionPolicy,
   RuntimeBatchRequest,
)
from mycelium_router.a10_fault_hooks import (
   FAULT_KINDS,
   A10FaultHookRefused,
   A10FaultHooks,
   A10InjectedFault,
   FaultInjectingDispatchPort,
   FaultInjectingVerificationPort,
)
from mycelium_router.a10_member_isolation import MemberIdentity
from mycelium_router.a10_runtime_batch import A10ContractError
from mycelium_router.a10_target_verification import TargetVerificationRequest
from mycelium_router.a10_verifier_execution import (
   PositionDecision,
   PositionEvaluation,
   verify_candidate_positions,
)
from mycelium_router.contracts import RuntimeBatchKey, RuntimeResult

PROTOCOL = "mycelium.a10_fault_negatives.v1"
CPU_SURFACE = "cpu_fault_hook"
PASSED = "failed_closed_within_scope"
VIOLATED = "violated"

_INCARNATION = "incarnation:cpu-reference"
_PEER_INCARNATION = "incarnation:cpu-reference-peer"
_PREFIX = 3
_GROUP = ("g0", "g1", "g2")
_FOREIGN_REASON = "incompatible_compatibility_key"

#: (error code of the faulted dispatch, code of its cause) expected per fault.
_EXPECTED_ERRORS: dict[str, tuple[str | None, str | None]] = {
   "timeout": ("runtime_dispatch_failed", "backend_timeout"),
   "shared_backend_failure": ("runtime_dispatch_failed", "shared_backend_failure"),
   "runtime_exit": ("runtime_dispatch_failed", "runtime_exit"),
   "shutdown": ("runtime_dispatch_failed", "runtime_shutdown"),
   "wrong_result_count": ("member_identity_mismatch_fails_closed", None),
   "stale_prefix": ("invalid_kv_position", None),
   "partial_member_failure": (None, None),
   "incompatible_member": (None, None),
   "verifier_rollback": ("verifier_transaction_failure", None),
}
#: Faults whose whole invocation fails closed (no member of it is forwarded).
_WHOLE_INVOCATION = (
   "timeout",
   "shared_backend_failure",
   "runtime_exit",
   "shutdown",
   "wrong_result_count",
   "stale_prefix",
)
#: Faults after which the faulted placement's runtime must stay unusable.
_DEAD_RUNTIME = ("runtime_exit", "shutdown")


# ---------------------------------------------------------------------------
# Reference runtime (deterministic, CPU, honest report)
# ---------------------------------------------------------------------------


class ReferenceDispatchPort:
   """One real invocation per offered group, a runtime-owned report, no side effects."""

   def __init__(self, *, prefixes: dict[str, int], incarnation: str = _INCARNATION) -> None:
      self._prefixes = dict(prefixes)
      self._incarnation = incarnation
      self.calls = 0

   def dispatch_mode(self) -> str:
      return "runtime_batch"

   def execute_batch(self, request: RuntimeBatchRequest) -> BatchExecutionReport:
      self.calls += 1
      return BatchExecutionReport(
         dispatch_mode="runtime_batch",
         backend_invocations=1,
         membership_authority="stage_runtime",
         incarnation=self._incarnation,
         executed=tuple(
            MemberExecution(
               member_ref=member_ref,
               kv_ref=f"kv:{member_ref}",
               result=RuntimeResult(
                  success=True,
                  payload=f"out:{member_ref}".encode(),
                  token_id=1_000 + index,
               ),
               committed_prefix=self._prefixes.get(member_ref, 0) + 1,
               event_bytes=(4,),
            )
            for index, member_ref in enumerate(request.member_refs)
         ),
      )


class _ReferenceTransaction:
   """A target-KV transaction that reports its committed state honestly."""

   def __init__(self, *, position: int, digest: str) -> None:
      self._prefix_position = position
      self._prefix_digest = digest
      self._position = position
      self._digest = digest
      self.open = True
      self.commit_calls = 0
      self.rollback_calls = 0
      self.cleanup_calls = 0

   def committed_prefix_position(self) -> int:
      return self._position

   def committed_prefix_digest(self) -> str:
      return self._digest

   def evaluate_positions(self, candidate_position_count: int) -> PositionEvaluation:
      return PositionEvaluation(
         invocation_count=1,
         prefix_position=self._prefix_position,
         prefix_digest=self._prefix_digest,
         decisions=tuple(PositionDecision(offset=index, accepted=True) for index in range(candidate_position_count)),
      )

   def commit_verified_prefix(self, committed_count: int) -> None:
      self.commit_calls += 1
      self._position = self._prefix_position + committed_count
      self._digest = _VERIFIED_DIGEST

   def rollback_to_prefix(self) -> None:
      self.rollback_calls += 1
      self._position = self._prefix_position
      self._digest = self._prefix_digest

   def cleanup(self) -> str:
      self.cleanup_calls += 1
      self.open = False
      return "verification_released"


_PREFIX_DIGEST = "sha256:" + "a" * 64
_VERIFIED_DIGEST = "sha256:" + "c" * 64
_VERIFIER_PREFIX = 16


class ReferenceVerificationPort:
   """Opens one reference transaction per verification and remembers each."""

   def __init__(self) -> None:
      self.transactions: list[_ReferenceTransaction] = []

   def begin_verification(self) -> _ReferenceTransaction:
      transaction = _ReferenceTransaction(position=_VERIFIER_PREFIX, digest=_PREFIX_DIGEST)
      self.transactions.append(transaction)
      return transaction

   def sequential_reference(self, candidate_position_count: int) -> Any:  # pragma: no cover - unused here
      raise A10ContractError("sequential_reference_not_used_by_the_fault_scenario")


# ---------------------------------------------------------------------------
# The observation and its judge
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FaultObservation:
   """What one scenario actually did; the judge decides it, nothing here claims a pass."""

   fault: str | None
   fired: bool = False
   error_code: str | None = None
   cause_code: str | None = None
   group: tuple[str, ...] = ()
   failed: tuple[str, ...] = ()
   forwarded: tuple[str, ...] = ()
   excluded: tuple[tuple[str, str], ...] = ()
   batch_sizes: tuple[int, ...] = ()
   prefixes_before: dict[str, int] = field(default_factory=dict)
   prefixes_after: dict[str, int] = field(default_factory=dict)
   same_placement_followup: str = "not_applicable"
   peer_completed_once: bool = False
   residue_fields: dict[str, int] = field(default_factory=dict)
   verifier_open_transactions: int = 0
   verifier_commit_calls: int = 0
   verifier_rollback_calls: int = 0


def judge(observation: FaultObservation) -> tuple[bool, list[str]]:
   """Decide one observation against its declared scope; closed reason codes only."""

   kind = observation.fault
   reasons: list[str] = []
   if kind is None:
      return False, ["no_fault_to_judge"]
   if not observation.fired:
      reasons.append("fault_not_fired")
   if observation.residue_fields:
      reasons.append("residue_nonzero")
   if not observation.peer_completed_once:
      reasons.append("peer_placement_poisoned")
   expected_error, expected_cause = _EXPECTED_ERRORS[kind]
   if observation.error_code != expected_error:
      reasons.append("unexpected_error_code")
   if observation.cause_code != expected_cause:
      reasons.append("unexpected_cause_code")

   if kind in _WHOLE_INVOCATION:
      if observation.forwarded:
         reasons.append("uncommitted_result_forwarded")
      if observation.prefixes_after != observation.prefixes_before:
         reasons.append("prefix_not_restored")
   if kind in _DEAD_RUNTIME:
      if observation.same_placement_followup != "failed_closed":
         reasons.append("dead_runtime_served_work")
   elif kind != "verifier_rollback" and observation.same_placement_followup != "completed":
      reasons.append("same_placement_poisoned")

   if kind == "partial_member_failure":
      victim = "g1"
      survivors = tuple(ref for ref in _GROUP if ref != victim)
      if observation.failed != (victim,):
         reasons.append("failure_exceeds_scope")
      if observation.forwarded != survivors or any(
         observation.prefixes_after.get(ref) != observation.prefixes_before.get(ref, 0) + 1 for ref in survivors
      ):
         reasons.append("survivor_not_forwarded_once")
      if observation.prefixes_after.get(victim) != observation.prefixes_before.get(victim):
         reasons.append("prefix_not_restored")
   elif kind == "incompatible_member":
      if observation.excluded != (("g2", _FOREIGN_REASON),):
         reasons.append("incompatible_member_not_excluded")
      if observation.batch_sizes != (2, 1):
         reasons.append("incompatible_member_batched")
      if observation.forwarded != _GROUP or any(
         observation.prefixes_after.get(ref) != observation.prefixes_before.get(ref, 0) + 1 for ref in _GROUP
      ):
         reasons.append("survivor_not_forwarded_once")
   elif kind == "verifier_rollback":
      if observation.verifier_open_transactions:
         reasons.append("verifier_transaction_left_open")
      if observation.verifier_commit_calls:
         reasons.append("verifier_committed_after_failure")
      if observation.verifier_rollback_calls != 1:
         reasons.append("verifier_not_rolled_back")
      if observation.forwarded:
         reasons.append("uncommitted_result_forwarded")
      if observation.prefixes_after != observation.prefixes_before:
         reasons.append("prefix_not_restored")
   return not reasons, reasons


# ---------------------------------------------------------------------------
# Scenarios
# ---------------------------------------------------------------------------


def _policy() -> RuntimeBatchAdmissionPolicy:
   return RuntimeBatchAdmissionPolicy(
      maximum_members=8,
      maximum_batch_bytes=1 << 16,
      maximum_queued_members=16,
      maximum_queued_bytes=1 << 20,
      maximum_active_batches=2,
      collection_window_seconds=0.002,
      deadline_guard_seconds=0.001,
      maximum_consecutive_interactive_selections=2,
      maximum_retry_after_seconds=0.05,
      maximum_member_event_bytes=4_096,
      gateway_buffer_bytes=64,
      detachment_policy="cancel",
   )


def _key(**overrides: Any) -> RuntimeBatchKey:
   values: dict[str, Any] = {
      "deployment_id": "deployment-1",
      "deployment_epoch": 7,
      "model_commit": "commit-abc",
      "manifest_digest": "manifest-abc",
      "placement_id": "placement-a",
      "assignment_id": "assignment-a",
      "stage_signature": "stage-signature-a",
      "load_proof_digest": "load-proof-a",
      "runtime_backend": "numpy",
      "phase": "DECODE",
      "hidden_size": 8,
      "activation_bytes": 4,
      "token_span": 1,
   }
   values.update(overrides)
   return RuntimeBatchKey(**values)


def _member(
   member_ref: str,
   *,
   hooks: A10FaultHooks | None = None,
   qos_class: str = "batch",
   phase: str = "DECODE",
) -> BatchMember:
   key = _key(phase=phase)
   if hooks is not None:
      key = hooks.perturb_member_key(member_ref, key)
   return BatchMember(
      identity=MemberIdentity(
         member_ref=member_ref,
         request_ref=f"request:{member_ref}",
         attempt_ref=f"attempt:{member_ref}",
         path_ref=f"path:{member_ref}",
         kv_ref=f"kv:{member_ref}",
      ),
      phase=phase,
      qos_class=qos_class,
      encoded_bytes=64,
      enqueued_at=0.0,
      deadline_at=1_000.0,
      compatibility_key=key,
      committed_prefix=_PREFIX,
   )


def _prefixes(admission: RuntimeBatchAdmission, refs: tuple[str, ...]) -> dict[str, int]:
   snapshot = dict(admission.kv_snapshot())
   return {ref: snapshot[ref][0] for ref in refs if ref in snapshot}


def _residue(admission: RuntimeBatchAdmission, *, prefix: str = "") -> dict[str, int]:
   usage = admission.resource_usage()
   fields = {
      f"{prefix}queued_members": usage.queued_members,
      f"{prefix}queued_bytes": usage.queued_bytes,
      f"{prefix}active_batches": usage.active_batches,
      f"{prefix}pending_reservations": usage.pending_reservations,
      f"{prefix}buffered_event_bytes": usage.buffered_event_bytes,
      f"{prefix}open_arenas": usage.open_arenas,
      f"{prefix}tentative_kv_transactions": sum(
         1 for _, (committed, tentative) in admission.kv_snapshot() if tentative != committed
      ),
   }
   return {name: value for name, value in fields.items() if value}


def _run_group(
   admission: RuntimeBatchAdmission, *, collect_at: float, dispatch_at: float
) -> tuple[Any, Any, str | None, str | None]:
   """Collect and dispatch one group; return (collection, outcome, error, cause)."""

   collection = admission.collect(now=collect_at)
   try:
      outcome = admission.dispatch(collection, now=dispatch_at)
   except A10ContractError as error:
      cause = getattr(error.__cause__, "code", None)
      return collection, None, error.code, cause if isinstance(cause, str) else None
   return collection, outcome, None, None


def _peer_completed_once() -> tuple[bool, dict[str, int]]:
   """An unrelated placement (own admission, own runtime) serving its own group."""

   refs = ("p0", "p1")
   admission = RuntimeBatchAdmission(
      policy=_policy(),
      port=ReferenceDispatchPort(prefixes=dict.fromkeys(refs, _PREFIX), incarnation=_PEER_INCARNATION),
      incarnation=_PEER_INCARNATION,
      stage_ref="stage:peer",
   )
   admission.submit(
      (_member("p0", qos_class="interactive"), _member("p1")),
      now=0.0,
   )
   _, outcome, error, _ = _run_group(admission, collect_at=0.0, dispatch_at=0.001)
   completed = (
      error is None
      and outcome is not None
      and outcome.forwarded == refs
      and all(record.forward_count == 1 for record in outcome.members)
      and _prefixes(admission, refs) == dict.fromkeys(refs, _PREFIX + 1)
   )
   return bool(completed), _residue(admission, prefix="peer_")


def _observe_dispatch_fault(kind: str | None, hooks: A10FaultHooks) -> FaultObservation:
   port = FaultInjectingDispatchPort(
      ReferenceDispatchPort(prefixes=dict.fromkeys((*_GROUP, "f0", "f1", "q0"), _PREFIX)), hooks
   )
   admission = RuntimeBatchAdmission(
      policy=_policy(), port=port, incarnation=_INCARNATION, stage_ref="stage:faulted"
   )
   if kind in _DEAD_RUNTIME:
      # Work still queued behind the batch that is in flight when the runtime goes.
      admission.submit((_member("q0", phase="PREFILL"),), now=0.0)
   if kind == "partial_member_failure":
      hooks.arm(kind, victim="g1")
   elif kind == "incompatible_member":
      hooks.arm(kind, victim="g2")
   elif kind is not None:
      hooks.arm(kind, victim="g1")
   admission.submit(
      (
         _member("g0", hooks=hooks, qos_class="interactive"),
         _member("g1", hooks=hooks),
         _member("g2", hooks=hooks),
      ),
      now=0.0,
   )
   before = _prefixes(admission, _GROUP)

   collection, outcome, error, cause = _run_group(admission, collect_at=0.0, dispatch_at=0.001)
   batch_sizes = [len(collection.selected)]
   excluded = tuple(entry for entry in collection.excluded if entry[1] == _FOREIGN_REASON)
   failed: list[str] = []
   forwarded: list[str] = []
   if outcome is not None:
      forwarded.extend(outcome.forwarded)
      failed.extend(record.member_ref for record in outcome.members if record.outcome == "failed")
   if kind == "incompatible_member" and admission.queue_depth():
      # The foreign member is served alone, under its own compatibility key.
      second, second_outcome, second_error, _ = _run_group(admission, collect_at=1.0, dispatch_at=1.001)
      batch_sizes.append(len(second.selected))
      if second_outcome is not None:
         forwarded.extend(second_outcome.forwarded)
      elif second_error is not None:
         error = error or second_error

   # The faulted placement keeps serving (or, for a dead runtime, refuses) new work.
   admission.submit((_member("f0", qos_class="interactive"), _member("f1")), now=2.0)
   _, followup_outcome, followup_error, _ = _run_group(admission, collect_at=2.0, dispatch_at=2.001)
   followup = (
      "completed"
      if followup_error is None and followup_outcome is not None and followup_outcome.forwarded == ("f0", "f1")
      else "failed_closed"
      if followup_error is not None
      else "not_applicable"
   )

   if kind in _DEAD_RUNTIME:
      # Shutdown teardown of the dead placement: every queued member is cancelled
      # before collection and removed by the next collection pass.
      for queued in ("q0",):
         admission.cancel(queued, at="pre_collection", now=3.0)
      admission.collect(now=3.0)

   peer_ok, peer_residue = _peer_completed_once()
   residue = {**_residue(admission), **peer_residue}
   return FaultObservation(
      fault=kind,
      fired=bool(hooks.fired()) if kind is not None else False,
      error_code=error,
      cause_code=cause,
      group=tuple(member.member_ref for member in collection.selected),
      failed=tuple(failed),
      forwarded=tuple(forwarded),
      excluded=excluded,
      batch_sizes=tuple(batch_sizes),
      prefixes_before=before,
      prefixes_after=_prefixes(admission, _GROUP),
      same_placement_followup=followup,
      peer_completed_once=peer_ok,
      residue_fields=residue,
   )


def _verification_request(name: str) -> TargetVerificationRequest:
   return TargetVerificationRequest(
      request_ref=f"verification:{name}",
      target_kv_ref=f"target-kv:{name}",
      runtime_incarnation=_INCARNATION,
      committed_prefix_position=_VERIFIER_PREFIX,
      committed_prefix_digest=_PREFIX_DIGEST,
      candidate_position_count=4,
      candidate_bytes=4_096,
      generation_policy="deterministic_greedy",
      transaction_mode="commit_verified_prefix",
      deadline_at=1.0,
      cancellation_generation=0,
      idempotency_digest="sha256:" + "d" * 64,
   )


def _observe_verifier_fault(hooks: A10FaultHooks) -> FaultObservation:
   reference = ReferenceVerificationPort()
   port = FaultInjectingVerificationPort(reference, hooks)
   hooks.arm("verifier_rollback")
   error: str | None = None
   try:
      verify_candidate_positions(port, _verification_request("faulted"))
   except A10InjectedFault as fault:
      error = fault.code
   faulted = reference.transactions[0]

   # The next, unrelated verification on the same runtime is a normal one.
   follow_ok = False
   try:
      outcome = verify_candidate_positions(port, _verification_request("followup"))
      follow_ok = outcome.commit_state == "committed" and outcome.committed_position == _VERIFIER_PREFIX + 4
   except A10ContractError:
      follow_ok = False

   open_transactions = sum(1 for transaction in reference.transactions if transaction.open)
   return FaultObservation(
      fault="verifier_rollback",
      fired=bool(hooks.fired()),
      error_code=error,
      cause_code=None,
      prefixes_before={"target": _VERIFIER_PREFIX},
      prefixes_after={"target": faulted.committed_prefix_position()},
      peer_completed_once=follow_ok,
      residue_fields={"kv_transactions": open_transactions} if open_transactions else {},
      verifier_open_transactions=sum(1 for transaction in reference.transactions[:1] if transaction.open),
      verifier_commit_calls=faulted.commit_calls,
      verifier_rollback_calls=faulted.rollback_calls,
   )


def observe_fault(kind: str | None, *, grant: object) -> FaultObservation:
   """Run one scenario (``None`` is the fault-free control) and report what happened."""

   if kind is not None and kind not in FAULT_KINDS:
      raise A10FaultHookRefused("unknown_fault_kind")
   hooks = A10FaultHooks(grant=grant)
   if kind == "verifier_rollback":
      return _observe_verifier_fault(hooks)
   return _observe_dispatch_fault(kind, hooks)


def run_fault_negative(kind: str, *, grant: object) -> dict[str, Any]:
   """Execute one required negative and record its closed outcome and residue."""

   observation = observe_fault(kind, grant=grant)
   passed, reasons = judge(observation)
   return {
      "outcome": PASSED if passed else VIOLATED,
      "residue": len(observation.residue_fields),
      "fault": kind,
      "surface": CPU_SURFACE,
      "physical_pending": True,
      "reasons": reasons,
   }


__all__ = [
   "CPU_SURFACE",
   "PASSED",
   "PROTOCOL",
   "VIOLATED",
   "FaultObservation",
   "ReferenceDispatchPort",
   "ReferenceVerificationPort",
   "judge",
   "observe_fault",
   "run_fault_negative",
]
