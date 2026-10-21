# SPDX-License-Identifier: AGPL-3.0-only
"""A10 deterministic runtime-batch admission path.

The relay's drained ``RuntimeBatch`` and this module's ``BatchCollection`` are
proposals: candidate membership only.  Actual membership exists only in
``mycelium.runtime_batch_observation.v1``, produced here on behalf of the stage
runtime and carrying ``membership_authority="stage_runtime"``, the backend
invocation count, the proposed count and one outcome per actual member.

The path is fail-closed on a *dispatch report*, not on a label:

* a sequential loop of singleton executions (``sequential_dispatch``),
* a per-member concurrent dispatch (``member_concurrent_dispatch``),
* a split invocation, a stale incarnation, a misordered or unknown member, a
  result bound to a peer's KV identity, an escaped invocation arena, a partial KV
  commit or non-finite output

are all refused before any observation exists.  ``evaluate_batch_claim`` is the
frozen inventory's predicate: candidate membership, concurrent admission and a
sequential loop never qualify on their own.

Deterministic half only: no backend is executed here by the tests, no physical
evidence is produced, and the scaffold's physical boundaries keep refusing.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict, dataclass
from typing import Iterable, NoReturn, Protocol, runtime_checkable

from mycelium_router.a10_member_isolation import (
   CANCELLATION_POINTS,
   DETACHMENT_POLICIES,
   GatewayBuffer,
   MemberIdentity,
   MemberKvRegistry,
)
from mycelium_router.a10_runtime_batch import (
   A10ContractError,
   BACKEND_INVOCATIONS_PER_OBSERVED_BATCH,
   MEMBERSHIP_AUTHORITY_STAGE_RUNTIME,
   MINIMUM_PHYSICAL_BATCH_MEMBERS,
   RuntimeBatchObservation,
   RuntimeBatchProposal,
)
from mycelium_router.contracts import RuntimeBatchKey, RuntimeResult


#: The frozen inventory's minimum member count for an observed (physical) batch.
MINIMUM_OBSERVED_BATCH_MEMBERS = MINIMUM_PHYSICAL_BATCH_MEMBERS

#: What a runtime may report about how it dispatched an offered group.
DISPATCH_MODES = (
   "runtime_batch",
   "sequential_dispatch",
   "member_concurrent_dispatch",
)

#: The claim sources an A10 surface may present as runtime-batch evidence.
CLAIM_SOURCES = (
   "candidate_membership",
   "concurrent_admission",
   "sequential_loop",
   "stage_runtime_observation",
)

#: Member outcomes a runtime-owned observation may carry.
MEMBER_OUTCOMES = ("completed", "cancelled", "failed", "unknown")

_PRE_LAUNCH_CANCELLATIONS = ("pre_collection", "pre_launch", "command_cancellation")
_POST_RETURN_CANCELLATIONS = ("post_return", "pre_forward")

_BOUND_CEILING = 1 << 30
_INTERACTIVE_BASE_PRIORITY = 100.0
_BATCH_BASE_PRIORITY = 10.0


class A10AdmissionError(A10ContractError):
   """A coded, fail-closed runtime-batch admission refusal."""


def _reject(code: str) -> NoReturn:
   raise A10AdmissionError(code)


def _text(value: object, *, code: str) -> str:
   if not isinstance(value, str) or not value or value != value.strip():
      _reject(code)
   return value


def _finite(value: object, *, code: str, minimum: float | None = None) -> float:
   if (
      isinstance(value, bool)
      or not isinstance(value, (int, float))
      or not math.isfinite(float(value))
   ):
      _reject(code)
   number = float(value)
   if minimum is not None and number < minimum:
      _reject(code)
   return number


def _bounded_int(
   value: object,
   *,
   code: str,
   minimum: int,
   maximum: int,
) -> int:
   if (
      isinstance(value, bool)
      or not isinstance(value, int)
      or value < minimum
      or value > maximum
   ):
      _reject(code)
   return value


def _refs(value: object, *, code: str) -> tuple[str, ...]:
   if not isinstance(value, tuple) or not value:
      _reject(code)
   for item in value:
      _text(item, code=code)
   return value


def compatibility_digest(key: RuntimeBatchKey) -> str:
   """A stable, opaque compatibility identity for one offered group."""

   if not isinstance(key, RuntimeBatchKey):
      _reject("missing_batch_compatibility_identity")
   canonical = json.dumps(asdict(key), sort_keys=True, separators=(",", ":"))
   return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Policy: every bound is finite and explicit
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RuntimeBatchAdmissionPolicy:
   """The deterministic bounds this admission path enforces.

   These are *not* qualified production values: the qualified bounds are A9/A4
   qualification inputs.  They are the finite, testable bounds of this lane.
   """

   maximum_members: int
   maximum_batch_bytes: int
   maximum_queued_members: int
   maximum_queued_bytes: int
   maximum_active_batches: int
   collection_window_seconds: float
   deadline_guard_seconds: float
   maximum_consecutive_interactive_selections: int
   maximum_retry_after_seconds: float
   maximum_member_event_bytes: int
   gateway_buffer_bytes: int
   detachment_policy: str = "cancel"

   def __post_init__(self) -> None:
      _bounded_int(
         self.maximum_members,
         code="invalid_batch_member_limit",
         minimum=1,
         maximum=_BOUND_CEILING,
      )
      _bounded_int(
         self.maximum_batch_bytes,
         code="invalid_batch_byte_limit",
         minimum=1,
         maximum=_BOUND_CEILING,
      )
      _bounded_int(
         self.maximum_queued_members,
         code="invalid_queue_member_limit",
         minimum=1,
         maximum=_BOUND_CEILING,
      )
      _bounded_int(
         self.maximum_queued_bytes,
         code="invalid_queue_byte_limit",
         minimum=1,
         maximum=_BOUND_CEILING,
      )
      _bounded_int(
         self.maximum_active_batches,
         code="invalid_active_batch_limit",
         minimum=1,
         maximum=_BOUND_CEILING,
      )
      _finite(
         self.collection_window_seconds,
         code="invalid_collection_window",
         minimum=0.0,
      )
      _finite(self.deadline_guard_seconds, code="invalid_deadline_guard", minimum=0.0)
      _bounded_int(
         self.maximum_consecutive_interactive_selections,
         code="invalid_aging_bound",
         minimum=0,
         maximum=_BOUND_CEILING,
      )
      _finite(
         self.maximum_retry_after_seconds,
         code="invalid_retry_hint_bound",
         minimum=0.0,
      )
      if self.maximum_retry_after_seconds <= 0.0:
         _reject("invalid_retry_hint_bound")
      _bounded_int(
         self.maximum_member_event_bytes,
         code="invalid_member_event_bound",
         minimum=1,
         maximum=_BOUND_CEILING,
      )
      _bounded_int(
         self.gateway_buffer_bytes,
         code="invalid_gateway_buffer_bound",
         minimum=1,
         maximum=_BOUND_CEILING,
      )
      if self.detachment_policy not in DETACHMENT_POLICIES:
         _reject("invalid_detachment_policy")

   @property
   def bounded_retry_hint(self) -> float:
      """The bounded retry hint an overflow rejection may carry."""

      hint = self.collection_window_seconds + self.deadline_guard_seconds
      return min(self.maximum_retry_after_seconds, hint) or self.maximum_retry_after_seconds


# ---------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BatchMember:
   """One queued work item offered to the runtime as a batch candidate."""

   identity: MemberIdentity
   phase: str
   qos_class: str
   encoded_bytes: int
   enqueued_at: float
   deadline_at: float
   compatibility_key: RuntimeBatchKey
   committed_prefix: int = 0

   def __post_init__(self) -> None:
      if not isinstance(self.identity, MemberIdentity):
         _reject("missing_member_identity")
      _text(self.phase, code="invalid_member_phase")
      if self.qos_class not in ("interactive", "batch"):
         _reject("invalid_member_qos_class")
      _bounded_int(
         self.encoded_bytes,
         code="invalid_member_bytes",
         minimum=1,
         maximum=_BOUND_CEILING,
      )
      _finite(self.enqueued_at, code="invalid_member_enqueued_at")
      _finite(self.deadline_at, code="invalid_member_deadline")
      if self.deadline_at < self.enqueued_at:
         _reject("invalid_member_deadline")
      if not isinstance(self.compatibility_key, RuntimeBatchKey):
         _reject("missing_batch_compatibility_identity")
      _bounded_int(
         self.committed_prefix,
         code="invalid_kv_position",
         minimum=0,
         maximum=_BOUND_CEILING,
      )

   @property
   def member_ref(self) -> str:
      return self.identity.member_ref


@dataclass(frozen=True)
class QueueAdmission:
   """The outcome of offering work to the bounded queue."""

   accepted: tuple[str, ...]
   refusals: tuple[tuple[str, str], ...]
   retry_after_seconds: float

   @property
   def refused(self) -> bool:
      return bool(self.refusals)


@dataclass(frozen=True)
class BatchCollection:
   """Candidate membership: what the scheduler *intends* to offer.

   This is a proposal, never evidence that a batch executed.
   """

   batch_ref: str
   phase: str
   compatibility_key: RuntimeBatchKey
   selected: tuple[BatchMember, ...]
   excluded: tuple[tuple[str, str], ...]
   reason: str
   collected_at: float
   deadline_at: float
   maximum_members: int
   maximum_bytes: int

   @property
   def candidate_member_refs(self) -> tuple[str, ...]:
      return tuple(member.member_ref for member in self.selected)

   def proposal(self) -> RuntimeBatchProposal:
      if not self.selected:
         _reject("empty_batch_collection")
      return RuntimeBatchProposal(
         compatibility_key=self.compatibility_key,
         member_refs=self.candidate_member_refs,
         qos_classes=tuple(member.qos_class for member in self.selected),
         maximum_members=self.maximum_members,
         maximum_bytes=self.maximum_bytes,
         proposed_at=self.collected_at,
         collection_deadline_at=self.deadline_at,
      )


@dataclass(frozen=True)
class RuntimeBatchRequest:
   """One offered group handed to the stage runtime for a single invocation."""

   batch_ref: str
   phase: str
   compatibility_key: str
   member_refs: tuple[str, ...]
   proposed_member_count: int

   def __post_init__(self) -> None:
      _text(self.batch_ref, code="invalid_batch_ref")
      _text(self.phase, code="invalid_member_phase")
      _text(self.compatibility_key, code="missing_batch_compatibility_identity")
      _refs(self.member_refs, code="invalid_batch_member_refs")
      _bounded_int(
         self.proposed_member_count,
         code="invalid_proposed_member_count",
         minimum=len(self.member_refs),
         maximum=_BOUND_CEILING,
      )


@dataclass(frozen=True)
class MemberExecution:
   """One member's part of a runtime-owned execution report."""

   member_ref: str
   kv_ref: str
   result: RuntimeResult
   committed_prefix: int
   event_bytes: tuple[int, ...] = ()
   partial_commit: bool = False

   def __post_init__(self) -> None:
      _text(self.member_ref, code="invalid_member_outcome")
      _text(self.kv_ref, code="invalid_member_kv_identity")
      if not isinstance(self.result, RuntimeResult):
         _reject("invalid_member_result_fails_closed")
      _bounded_int(
         self.committed_prefix,
         code="invalid_kv_position",
         minimum=0,
         maximum=_BOUND_CEILING,
      )
      if not isinstance(self.event_bytes, tuple):
         _reject("invalid_member_event_fails_closed")


@dataclass(frozen=True)
class BatchExecutionReport:
   """What the stage runtime says it actually did with an offered group."""

   dispatch_mode: str
   backend_invocations: int
   membership_authority: str
   incarnation: str
   executed: tuple[MemberExecution, ...]
   arena_open: bool = False


@runtime_checkable
class BatchDispatchPort(Protocol):
   """The dispatch surface the admission path requires of a stage runtime."""

   def dispatch_mode(self) -> str: ...

   def execute_batch(self, request: RuntimeBatchRequest) -> BatchExecutionReport: ...


@dataclass(frozen=True)
class MemberOutcomeRecord:
   """Per-member terminal attribution of one dispatch."""

   member_ref: str
   outcome: str
   forwarded: bool
   forward_count: int
   committed_prefix: int
   reason: str = ""

   def __post_init__(self) -> None:
      _text(self.member_ref, code="invalid_member_outcome")
      if self.outcome not in MEMBER_OUTCOMES:
         _reject("invalid_member_outcome")
      _bounded_int(
         self.forward_count,
         code="invalid_member_forward_count",
         minimum=0,
         maximum=1,
      )


@dataclass(frozen=True)
class BatchDispatchOutcome:
   """The result of one dispatch: one invocation, one isolated result per member."""

   observation: RuntimeBatchObservation
   members: tuple[MemberOutcomeRecord, ...]
   results: tuple[tuple[str, RuntimeResult], ...]
   event_feeds: tuple[tuple[str, tuple[int, ...]], ...]
   backend_invocations: int

   @property
   def forwarded(self) -> tuple[str, ...]:
      return tuple(
         record.member_ref for record in self.members if record.forward_count > 0
      )

   @property
   def discarded(self) -> tuple[tuple[str, str], ...]:
      return tuple(
         (record.member_ref, record.reason)
         for record in self.members
         if record.reason
         in ("result_discarded_after_return", "result_discarded_before_forwarding")
      )

   @property
   def detached(self) -> tuple[tuple[str, str], ...]:
      return tuple(
         (record.member_ref, record.reason)
         for record in self.members
         if record.reason == "gateway_buffer_exhausted"
      )

   @property
   def member_outcomes(self) -> tuple[tuple[str, str], ...]:
      """Terminal attribution per offered member, including unexecuted ones."""

      return tuple((record.member_ref, record.outcome) for record in self.members)

   def claims_runtime_batch(self) -> bool:
      return self.observation.claims_runtime_batch()


@dataclass(frozen=True)
class MemberCancellation:
   """One recorded cancellation: its named point and what it did."""

   member_ref: str
   point: str
   removal: str


@dataclass(frozen=True)
class ResourceUsage:
   """Request-owned resources; a dispatch must return these to baseline."""

   queued_members: int
   queued_bytes: int
   active_batches: int
   pending_reservations: int
   buffered_event_bytes: int
   open_arenas: int


@dataclass(frozen=True)
class BatchClaimVerdict:
   """The frozen inventory's runtime-batch predicate, with its reason."""

   qualifies: bool
   reason: str


_REMOVAL_BY_POINT = {
   "pre_collection": "removed_before_collection",
   "pre_launch": "removed_before_launch",
   "post_return": "result_discarded_after_return",
   "pre_forward": "result_discarded_before_forwarding",
   "command_cancellation": "operation_blocked_at_command_point",
}


def evaluate_batch_claim(
   *,
   source: str,
   observations: tuple[RuntimeBatchObservation, ...] = (),
) -> BatchClaimVerdict:
   """Judge one claim source against the frozen ``runtime_batch_requirements``."""

   if source not in CLAIM_SOURCES:
      _reject("unknown_batch_claim_source")
   if source == "candidate_membership":
      return BatchClaimVerdict(False, "candidate_membership_alone_does_not_qualify")
   if source == "concurrent_admission":
      return BatchClaimVerdict(False, "concurrent_admission_alone_does_not_qualify")
   if source == "sequential_loop":
      return BatchClaimVerdict(False, "sequential_loop_alone_does_not_qualify")
   if len(observations) != 1:
      return BatchClaimVerdict(
         False, "observation_evidence_requires_exactly_one_observed_batch"
      )
   if not observations[0].claims_runtime_batch():
      return BatchClaimVerdict(False, "observation_does_not_claim_runtime_batch")
   return BatchClaimVerdict(True, "stage_runtime_observation_owns_membership")


def _require_dispatch_port(port: object) -> BatchDispatchPort:
   if not isinstance(port, BatchDispatchPort):
      _reject("invalid_runtime_batch_port")
   return port


# ---------------------------------------------------------------------------
# The admission path
# ---------------------------------------------------------------------------


class RuntimeBatchAdmission:
   """Bounded collection, QoS/aging, and one fail-closed dispatch path.

   The object owns the queue, the member KV registry, the per-member gateway
   buffers and the recorded observations.  Timers stay with the caller: every
   method takes ``now``.
   """

   def __init__(
      self,
      *,
      policy: RuntimeBatchAdmissionPolicy,
      port: BatchDispatchPort,
      incarnation: str,
      stage_ref: str,
      kv_registry: MemberKvRegistry | None = None,
   ) -> None:
      if not isinstance(policy, RuntimeBatchAdmissionPolicy):
         _reject("invalid_runtime_batch_policy")
      self._policy = policy
      self._port = _require_dispatch_port(port)
      self._incarnation = _text(incarnation, code="invalid_runtime_incarnation")
      self._stage_ref = _text(stage_ref, code="invalid_stage_ref")
      self._kv = kv_registry if kv_registry is not None else MemberKvRegistry()
      self._identities: dict[str, MemberIdentity] = {}
      self._queue: list[BatchMember] = []
      self._queued_bytes = 0
      self._active_batches = 0
      self._pending_reservations = 0
      self._batch_sequence = 0
      self._invocation_sequence = 0
      self._observations: list[RuntimeBatchObservation] = []
      self._dispatched: set[str] = set()
      self._cancellations: dict[str, str] = {}
      self._detached: set[str] = set()
      self._buffers: dict[str, _GatewayBuffer] = {}
      self._high_water = 0
      self._consecutive_interactive = 0

   # -- queue --------------------------------------------------------------

   def submit(
      self,
      members: Iterable[BatchMember],
      *,
      now: float,
   ) -> QueueAdmission:
      _finite(now, code="invalid_queue_admission_time")
      batch = tuple(members) if not isinstance(members, tuple) else members
      if not batch:
         _reject("empty_batch_member_refs")
      accepted: list[str] = []
      refusals: list[tuple[str, str]] = []
      for member in batch:
         if not isinstance(member, BatchMember):
            _reject("invalid_batch_member")
         member_ref = member.member_ref
         if member_ref in self._detached:
            refusals.append((member_ref, "detached_member_not_schedulable"))
            continue
         if len(self._queue) >= self._policy.maximum_queued_members:
            refusals.append((member_ref, "queue_member_limit"))
            continue
         if (
            self._queued_bytes + member.encoded_bytes
            > self._policy.maximum_queued_bytes
         ):
            refusals.append((member_ref, "queue_byte_limit"))
            continue
         self._identities[member_ref] = member.identity
         self._kv.register(member.identity, committed_prefix=member.committed_prefix)
         self._queue.append(member)
         self._queued_bytes += member.encoded_bytes
         accepted.append(member_ref)
      return QueueAdmission(
         accepted=tuple(accepted),
         refusals=tuple(refusals),
         retry_after_seconds=self._policy.bounded_retry_hint,
      )

   # -- collection (candidate membership) ---------------------------------

   def collect(self, *, now: float) -> BatchCollection:
      _finite(now, code="invalid_collection_time")
      if not self._queue:
         _reject("empty_batch_queue")
      batch_ref = f"batch:{self._batch_sequence + 1}"
      # Taken before cancellation removal: every queued member may be removed below,
      # and the empty collection still names the phase and key of what was queued.
      head = self._queue[0]
      eligible, cancelled = self._eligible(now=now)
      if not eligible:
         return self._collection(
            batch_ref=batch_ref,
            anchor=head,
            selected=(),
            excluded=cancelled,
            reason="no_eligible_members",
            now=now,
         )
      if self._active_batches >= self._policy.maximum_active_batches:
         return self._collection(
            batch_ref=batch_ref,
            anchor=self._anchor(eligible, now=now),
            selected=(),
            excluded=tuple(
               (member.member_ref, "active_batch_limit") for member in eligible
            )
            + cancelled,
            reason="active_batch_limit",
            now=now,
         )
      anchor, reason = self._anchor_and_reason(eligible, now=now)
      if reason == "collecting":
         return self._collection(
            batch_ref=batch_ref,
            anchor=anchor,
            selected=(),
            excluded=tuple(
               (member.member_ref, "collection_window_open") for member in eligible
            )
            + cancelled,
            reason="collecting",
            now=now,
         )
      selected, excluded = self._select(anchor, eligible, now=now)
      if not selected:
         return self._collection(
            batch_ref=batch_ref,
            anchor=anchor,
            selected=(),
            excluded=tuple(excluded) + cancelled,
            reason=reason,
            now=now,
         )
      return self._commit_collection(
         batch_ref=batch_ref,
         anchor=anchor,
         selected=tuple(selected),
         excluded=tuple(excluded) + cancelled,
         reason=reason,
         now=now,
      )

   # -- dispatch (actual membership) --------------------------------------

   def dispatch(self, collection: BatchCollection, *, now: float) -> BatchDispatchOutcome:
      _finite(now, code="invalid_dispatch_time")
      if not isinstance(collection, BatchCollection):
         _reject("invalid_batch_collection_argument")
      if not collection.selected:
         _reject("empty_batch_collection")
      if collection.batch_ref in self._dispatched:
         _reject("batch_already_dispatched")
      self._dispatched.add(collection.batch_ref)

      launchable = tuple(
         member
         for member in collection.selected
         if self._cancellations.get(member.member_ref)
         not in _PRE_LAUNCH_CANCELLATIONS
      )
      if not launchable:
         self._release(collection)
         _reject("no_launchable_members")

      request = RuntimeBatchRequest(
         batch_ref=collection.batch_ref,
         phase=collection.phase,
         compatibility_key=compatibility_digest(collection.compatibility_key),
         member_refs=tuple(member.member_ref for member in launchable),
         proposed_member_count=len(collection.selected),
      )
      self._invocation_sequence += 1
      invocation_ref = f"invocation:{self._invocation_sequence}"
      try:
         try:
            report = self._port.execute_batch(request)
         except A10ContractError:
            raise
         except Exception as exc:  # noqa: BLE001 - every dispatch failure is coded
            raise A10AdmissionError("runtime_dispatch_failed") from exc
         if not isinstance(report, BatchExecutionReport):
            _reject("invalid_batch_execution_report")
         outcome = self._settle(
            collection=collection,
            request=request,
            report=report,
            now=now,
            invocation_ref=invocation_ref,
         )
      finally:
         self._release(collection)
      return outcome

   # -- cancellation -------------------------------------------------------

   def cancel(self, member_ref: str, *, at: str, now: float) -> MemberCancellation:
      if at not in CANCELLATION_POINTS:
         _reject("unknown_cancellation_point")
      _finite(now, code="invalid_cancellation_time")
      if member_ref not in self._identities:
         _reject("unknown_member_ref")
      self._cancellations[member_ref] = at
      return MemberCancellation(
         member_ref=member_ref, point=at, removal=_REMOVAL_BY_POINT[at]
      )

   # -- read-only views ----------------------------------------------------

   def observations(self) -> tuple[RuntimeBatchObservation, ...]:
      return tuple(self._observations)

   def queue_depth(self) -> int:
      return len(self._queue)

   def kv_snapshot(self) -> tuple[tuple[str, tuple[int, int]], ...]:
      return tuple(
         sorted(
            (record.member_ref, (record.committed_prefix, record.tentative_prefix))
            for record in self._kv.snapshot()
         )
      )

   def buffer_high_water(self) -> int:
      return self._high_water

   def buffered_event_bytes(self) -> int:
      return sum(buffer.buffered_bytes for buffer in self._buffers.values())

   def resource_usage(self) -> ResourceUsage:
      return ResourceUsage(
         queued_members=len(self._queue),
         queued_bytes=self._queued_bytes,
         active_batches=self._active_batches,
         pending_reservations=self._pending_reservations,
         buffered_event_bytes=self.buffered_event_bytes(),
         open_arenas=0,
      )

   # -- collection internals ----------------------------------------------

   def _eligible(
      self, *, now: float
   ) -> tuple[list[BatchMember], tuple[tuple[str, str], ...]]:
      removed = [
         member
         for member in self._queue
         if self._cancellations.get(member.member_ref) == "pre_collection"
      ]
      # Checked before collection: a cancelled member never becomes a candidate.
      cancelled = tuple(
         (member.member_ref, "cancelled_before_collection") for member in removed
      )
      if removed:
         removed_refs = {member.member_ref for member in removed}
         self._queue = [
            member for member in self._queue if member.member_ref not in removed_refs
         ]
         self._queued_bytes -= sum(member.encoded_bytes for member in removed)
      return list(self._queue), cancelled

   def _anchor(self, eligible: list[BatchMember], *, now: float) -> BatchMember:
      return min(
         eligible,
         key=lambda member: (
            -self._effective_priority(member, now=now),
            member.member_ref,
         ),
      )

   def _anchor_and_reason(
      self, eligible: list[BatchMember], *, now: float
   ) -> tuple[BatchMember, str]:
      aged = self._aged_batch_member(eligible, now=now)
      limit = self._policy.maximum_consecutive_interactive_selections
      if aged is not None and self._consecutive_interactive >= limit:
         return aged, "aged_batch_selected"
      anchor = self._anchor(eligible, now=now)
      if anchor.deadline_at - now <= self._policy.deadline_guard_seconds:
         return anchor, "deadline_guard"
      if anchor.qos_class == "interactive":
         return anchor, "interactive_immediate"
      if now >= anchor.enqueued_at + self._policy.collection_window_seconds:
         return anchor, "collection_window_expired"
      return anchor, "collecting"

   def _aged_batch_member(
      self, eligible: list[BatchMember], *, now: float
   ) -> BatchMember | None:
      aged = [
         member
         for member in eligible
         if member.qos_class == "batch"
         and now >= member.enqueued_at + self._policy.collection_window_seconds
      ]
      if not aged:
         return None
      return self._anchor(aged, now=now)

   def _select(
      self,
      anchor: BatchMember,
      eligible: list[BatchMember],
      *,
      now: float,
   ) -> tuple[list[BatchMember], list[tuple[str, str]]]:
      selected: list[BatchMember] = []
      excluded: list[tuple[str, str]] = []
      total_bytes = 0
      for member in sorted(
         eligible,
         key=lambda candidate: (
            -self._effective_priority(candidate, now=now),
            candidate.member_ref,
         ),
      ):
         if (
            member.phase != anchor.phase
            or member.compatibility_key != anchor.compatibility_key
         ):
            if member is not anchor:
               excluded.append((member.member_ref, "incompatible_compatibility_key"))
            continue
         if len(selected) >= self._policy.maximum_members:
            excluded.append((member.member_ref, "batch_member_limit"))
            continue
         if (
            selected
            and total_bytes + member.encoded_bytes > self._policy.maximum_batch_bytes
         ):
            excluded.append((member.member_ref, "batch_byte_limit"))
            continue
         selected.append(member)
         total_bytes += member.encoded_bytes
      # The anchor is always part of its own selection.
      if anchor not in selected:
         selected.insert(0, anchor)
      return selected, excluded

   def _collection(
      self,
      *,
      batch_ref: str,
      anchor: BatchMember,
      selected: tuple[BatchMember, ...],
      excluded: tuple[tuple[str, str], ...],
      reason: str,
      now: float,
   ) -> BatchCollection:
      return BatchCollection(
         batch_ref=batch_ref,
         phase=anchor.phase,
         compatibility_key=anchor.compatibility_key,
         selected=selected,
         excluded=excluded,
         reason=reason,
         collected_at=now,
         deadline_at=max(
            now,
            min(
               (member.deadline_at for member in selected),
               default=anchor.deadline_at,
            )
            - self._policy.deadline_guard_seconds,
         ),
         maximum_members=self._policy.maximum_members,
         maximum_bytes=self._policy.maximum_batch_bytes,
      )

   def _commit_collection(
      self,
      *,
      batch_ref: str,
      anchor: BatchMember,
      selected: tuple[BatchMember, ...],
      excluded: tuple[tuple[str, str], ...],
      reason: str,
      now: float,
   ) -> BatchCollection:
      collection = self._collection(
         batch_ref=batch_ref,
         anchor=anchor,
         selected=selected,
         excluded=excluded,
         reason=reason,
         now=now,
      )
      selected_refs = {member.member_ref for member in selected}
      self._queue = [
         member for member in self._queue if member.member_ref not in selected_refs
      ]
      self._queued_bytes -= sum(member.encoded_bytes for member in selected)
      self._active_batches += 1
      self._pending_reservations += len(selected)
      self._batch_sequence += 1
      if anchor.qos_class == "interactive":
         self._consecutive_interactive += 1
      else:
         self._consecutive_interactive = 0
      return collection

   def _effective_priority(self, member: BatchMember, *, now: float) -> float:
      base = (
         _INTERACTIVE_BASE_PRIORITY
         if member.qos_class == "interactive"
         else _BATCH_BASE_PRIORITY
      )
      age = max(0.0, now - member.enqueued_at)
      aging = min(self._policy.maximum_retry_after_seconds, age)
      return base + aging

   def _release(self, collection: BatchCollection) -> None:
      self._active_batches = max(0, self._active_batches - 1)
      self._pending_reservations = max(
         0, self._pending_reservations - len(collection.selected)
      )

   # -- dispatch internals -------------------------------------------------

   def _settle(
      self,
      *,
      collection: BatchCollection,
      request: RuntimeBatchRequest,
      report: BatchExecutionReport,
      now: float,
      invocation_ref: str,
   ) -> BatchDispatchOutcome:
      self._validate_report(request, report)
      self._begin_tentative(report)

      verdicts: dict[str, tuple[str, str]] = {}
      feeds: dict[str, tuple[int, ...]] = {}
      cancelled = {
         member_ref: point
         for member_ref, point in self._cancellations.items()
         if point in _POST_RETURN_CANCELLATIONS
      }
      for execution in report.executed:
         member_ref = execution.member_ref
         if not execution.result.success:
            verdicts[member_ref] = ("failed", "member_failed")
            feeds[member_ref] = ()
            continue
         if member_ref in cancelled:
            reason = (
               "result_discarded_after_return"
               if cancelled[member_ref] == "post_return"
               else "result_discarded_before_forwarding"
            )
            verdicts[member_ref] = ("cancelled", reason)
            feeds[member_ref] = ()
            continue
         buffer = self._buffer_for(member_ref)
         accepted: list[int] = []
         overflowed = False
         for size in execution.event_bytes:
            if not buffer.fits(size):
               overflowed = True
               break
            buffer.offer(size)
            accepted.append(size)
         self._high_water = max(self._high_water, buffer.high_water_bytes)
         if overflowed:
            self._detached.add(member_ref)
            self._buffers.pop(member_ref, None)
            verdicts[member_ref] = ("cancelled", "gateway_buffer_exhausted")
            feeds[member_ref] = tuple(accepted)
            continue
         verdicts[member_ref] = ("completed", "")
         feeds[member_ref] = tuple(accepted)

      executed_refs = {execution.member_ref for execution in report.executed}
      members: list[MemberOutcomeRecord] = []
      results: list[tuple[str, RuntimeResult]] = []
      outcome_pairs: list[tuple[str, str]] = []
      for execution in report.executed:
         member_ref = execution.member_ref
         verdict, reason = verdicts[member_ref]
         outcome_pairs.append((member_ref, verdict))
         results.append((member_ref, execution.result))
         if verdict == "completed":
            record = self._kv.commit(
               member_ref=member_ref,
               kv_ref=execution.kv_ref,
               position=execution.committed_prefix,
            )
            members.append(
               MemberOutcomeRecord(
                  member_ref=member_ref,
                  outcome=verdict,
                  forwarded=True,
                  forward_count=1,
                  committed_prefix=record.committed_prefix,
               )
            )
            self._buffers.pop(member_ref, None)
            continue
         record = self._kv.rollback(member_ref=member_ref, kv_ref=execution.kv_ref)
         members.append(
            MemberOutcomeRecord(
               member_ref=member_ref,
               outcome=verdict,
               forwarded=False,
               forward_count=0,
               committed_prefix=record.committed_prefix,
               reason=reason,
            )
         )
      for member in collection.selected:
         if member.member_ref in executed_refs:
            continue
         if member.member_ref in {record.member_ref for record in members}:
            continue
         point = self._cancellations.get(member.member_ref)
         if point in _PRE_LAUNCH_CANCELLATIONS:
            verdict, reason = "cancelled", "cancelled_before_launch"
         else:
            verdict, reason = "unknown", "not_executed_by_runtime"
         members.append(
            MemberOutcomeRecord(
               member_ref=member.member_ref,
               outcome=verdict,
               forwarded=False,
               forward_count=0,
               committed_prefix=self._kv.address(
                  member_ref=member.member_ref, kv_ref=member.identity.kv_ref
               ).committed_prefix,
               reason=reason,
            )
         )
         feeds[member.member_ref] = ()
      observation = RuntimeBatchObservation(
         batch_ref=request.batch_ref,
         phase=collection.phase,
         stage=self._stage_ref,
         backend_invocation_id=invocation_ref,
         backend_invocations=report.backend_invocations,
         membership_authority=report.membership_authority,
         proposed_member_count=len(collection.selected),
         actual_member_count=len(report.executed),
         member_outcomes=tuple(outcome_pairs),
         collected_at=collection.collected_at,
         executed_at=now,
         cleanup="released",
      )
      self._observations.append(observation)
      return BatchDispatchOutcome(
         observation=observation,
         members=tuple(members),
         results=tuple(results),
         event_feeds=tuple(
            (member.member_ref, feeds.get(member.member_ref, ())) for member in members
         ),
         backend_invocations=report.backend_invocations,
      )

   def _validate_report(
      self, request: RuntimeBatchRequest, report: BatchExecutionReport
   ) -> None:
      if report.dispatch_mode == "sequential_dispatch":
         _reject("sequential_dispatch_does_not_qualify")
      if report.dispatch_mode == "member_concurrent_dispatch":
         _reject("concurrent_admission_alone_does_not_qualify")
      if report.dispatch_mode not in DISPATCH_MODES:
         _reject("invalid_dispatch_mode")
      if report.dispatch_mode != "runtime_batch":
         _reject("invalid_dispatch_mode")
      if self._port.dispatch_mode() != report.dispatch_mode:
         _reject("dispatch_mode_mismatch_fails_closed")
      if report.arena_open:
         _reject("invocation_arena_escaped")
      if report.membership_authority != MEMBERSHIP_AUTHORITY_STAGE_RUNTIME:
         _reject("invalid_membership_authority")
      if report.incarnation != self._incarnation:
         _reject("stale_incarnation_fails_closed")
      if report.backend_invocations != BACKEND_INVOCATIONS_PER_OBSERVED_BATCH:
         _reject("split_backend_invocations_fail_closed")
      allowed = list(request.member_refs)
      executed_refs = [execution.member_ref for execution in report.executed]
      if len(set(executed_refs)) != len(executed_refs):
         _reject("member_identity_mismatch_fails_closed")
      if any(member_ref not in allowed for member_ref in executed_refs):
         _reject("member_identity_mismatch_fails_closed")
      if executed_refs != [ref for ref in allowed if ref in set(executed_refs)]:
         _reject("member_identity_mismatch_fails_closed")
      for execution in report.executed:
         identity = self._identities.get(execution.member_ref)
         if identity is None or identity.kv_ref != execution.kv_ref:
            _reject("cross_member_kv_isolation_fails_closed")
         result = execution.result
         if isinstance(result.payload, float) and not math.isfinite(result.payload):
            _reject("nonfinite_member_output_fails_closed")
         for size in execution.event_bytes:
            if (
               isinstance(size, bool)
               or not isinstance(size, int)
               or size < 0
            ):
               _reject("invalid_member_event_fails_closed")
            if size > self._policy.maximum_member_event_bytes:
               _reject("unbounded_member_event_fails_closed")
      if any(execution.partial_commit for execution in report.executed):
         _reject("partial_kv_commit_fails_closed")

   def _begin_tentative(self, report: BatchExecutionReport) -> None:
      tentative: list[MemberExecution] = []
      try:
         for execution in report.executed:
            if not execution.result.success:
               continue
            self._kv.begin_tentative(
               member_ref=execution.member_ref,
               kv_ref=execution.kv_ref,
               position=execution.committed_prefix,
            )
            tentative.append(execution)
      except A10ContractError:
         for execution in tentative:
            self._kv.rollback(
               member_ref=execution.member_ref, kv_ref=execution.kv_ref
            )
         raise

   def _buffer_for(self, member_ref: str) -> GatewayBuffer:
      buffer = self._buffers.get(member_ref)
      if buffer is None:
         buffer = GatewayBuffer(
            member_ref=member_ref, capacity_bytes=self._policy.gateway_buffer_bytes
         )
         self._buffers[member_ref] = buffer
      return buffer
