# SPDX-License-Identifier: AGPL-3.0-only
"""A10 scaffold: runtime batch membership, continuous arrivals, and overlap honesty.

SCAFFOLD ONLY.  This module implements no runtime batching, no continuous-arrival
scheduling, no pipeline-overlap evidence and no qualification.  It declares the A10
entry points, validates their closed input shapes, and then refuses execution with an
explicit coded exception (``A10NotImplementedError``).  It never returns a value that
could be mistaken for a successful batch, overlap or qualification claim.

Why it exists: the A10 specification
(``docs/superpowers/specs/2026-08-18-mycelium-a10-runtime-batching-overlap-target-verification.md``)
is design-only and leaves several implementation seams ambiguous.  The companion
implementation contract
(``docs/superpowers/specs/2026-09-21-mycelium-a10-implementation-contract.md``)
resolves those seams and names the accessors, records and coded refusals below.  A
future implementation lane replaces the refusals with real behaviour and flips the
strong-RED suite in ``tests/a10_scaffold/``.

State: ``design_only``.  ``EARNED_CLOSURE_REQUIREMENTS`` is empty by construction:
this scaffold earns none of the thirteen live closure requirements of
``scripts/mycelium_completion_audit.py``, and the acceptance inventory
``tests/a10_acceptance/scenarios.v1.json`` remains the only frozen input.

What is genuinely implemented here (and claimed as nothing more): the closed-shape
validation of a proposed batch, the declared observation/capability record shapes,
and the explicit refusal boundary.  Shape validation is not execution evidence.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Any, NoReturn, Protocol, runtime_checkable

from mycelium_router.contracts import RuntimeBatch, RuntimeBatchKey, RuntimeResult


A10_GATE_ID = "A10"
SCAFFOLD_PROTOCOL = "mycelium.a10_scaffold.v1"
SCAFFOLD_STATE = "design_only"
ACCEPTANCE_INVENTORY = "tests/a10_acceptance/scenarios.v1.json"
ACCEPTANCE_INVENTORY_DIGEST = (
   "sha256:3fccdc3c12183c2f4d3fee9142969de20508c7e71cf553835811b60ff1e20542"
)
SPECIFICATION = (
   "docs/superpowers/specs/"
   "2026-08-18-mycelium-a10-runtime-batching-overlap-target-verification.md"
)
CLAIM_BOUNDARY = (
   "deterministic slices only: runtime batch execution and the uncertainty-aware "
   "overlap verdict are implemented and unit-tested in-process; no physical overlap, "
   "continuous-arrival, benchmark, product, browser or qualification claim; no closure "
   "requirement earned; the remaining refusal is explicit and coded"
)
DECLARED_CONTRACT_PROTOCOLS = (
   "mycelium.runtime_batch_policy.v1",
   "mycelium.runtime_batch_observation.v1",
   "mycelium.pipeline_overlap_trace.v1",
   "mycelium.target_verification_capability.v1",
   "mycelium.target_verification_runtime.v1",
   "mycelium.runtime_batch_qualification.v1",
)
EARNED_CLOSURE_REQUIREMENTS: frozenset[str] = frozenset()

# Sourced verbatim from the frozen acceptance inventory
# (tests/a10_acceptance/scenarios.v1.json, runtime_batch_requirements).
MINIMUM_PHYSICAL_BATCH_MEMBERS = 2
BACKEND_INVOCATIONS_PER_OBSERVED_BATCH = 1
MEMBERSHIP_AUTHORITY_STAGE_RUNTIME = "stage_runtime"
# v1 inventory value (six measured windows, three pairs).  Superseded for
# qualification by the pre-registered benchmark protocol v2 below; retained so
# the frozen v1 inventory stays mirrored verbatim.
MEASURED_PAIRED_WINDOWS = 6
# Sourced verbatim from the benchmark protocol v2
# (tests/a10_acceptance/benchmark_protocol.v2.json; owner ruling 2026-09-27,
# pre-registered amendment after r33 underpowered result, refined the same day
# before any v2 data existed): exactly sixteen scheduled baseline/candidate
# pairs, of which at least twelve must be valid; every valid pair is scored.
BENCHMARK_PROTOCOL = "tests/a10_acceptance/benchmark_protocol.v2.json"
SCHEDULED_BENCHMARK_PAIRS = 16
MINIMUM_VALID_BENCHMARK_PAIRS = 12

# Scaffold-level sanity ceilings only.  They are NOT qualified bounds: the qualified
# bounds are policy inputs owned by A9/A4 qualification, never by this module.
SCAFFOLD_MEMBER_CEILING = 1_024
SCAFFOLD_BYTE_CEILING = 1 << 30

_QOS_CLASSES = ("interactive", "batch")
_MEMBER_OUTCOMES = ("completed", "cancelled", "failed", "unknown")
_SHA256 = re.compile(r"sha256:[0-9a-f]{64}\Z")


# ---------------------------------------------------------------------------
# Errors: every refusal in this scaffold is explicit and coded
# ---------------------------------------------------------------------------


class A10ScaffoldError(ValueError):
   """Base class for every coded A10 scaffold refusal."""

   code = ""

   def __init__(self, code: str):
      super().__init__(code)
      self.code = code


class A10ContractError(A10ScaffoldError):
   """A closed-shape, bound, digest or identity rejection (specification section 3)."""


class A10NotImplementedError(A10ScaffoldError):
   """An A10 entry point that is declared but not implemented.

   The message is always ``not_implemented:<code>``.  This exception is never a
   success path and never stands in for real behaviour.
   """

   def __init__(self, code: str):
      super().__init__(f"not_implemented:{code}")


def _reject(code: str) -> NoReturn:
   raise A10ContractError(code)


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


def _text(value: object, *, code: str) -> str:
   if not isinstance(value, str) or not value or value != value.strip():
      _reject(code)
   return value


def _digest(value: object, *, code: str) -> str:
   if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
      _reject(code)
   return value


def _refs(value: object, *, code: str, duplicate_code: str) -> tuple[str, ...]:
   if (
      not isinstance(value, tuple)
      or not value
      or any(not isinstance(item, str) or not item or item != item.strip() for item in value)
   ):
      _reject(code)
   if len(set(value)) != len(value):
      _reject(duplicate_code)
   return value


@runtime_checkable
class RuntimeBatchPort(Protocol):
   """The declared interchange shape a target runtime port must expose.

   A future implementation lane may widen this protocol under the implementation
   contract; the scaffold rejects anything narrower so that a proposal can never be
   offered to a port that cannot execute, cancel or release work.
   """

   def execute(self, item: Any) -> RuntimeResult: ...

   def execute_batch(self, batch: RuntimeBatch) -> tuple[RuntimeResult, ...]: ...

   def cancel(self, path_id: str) -> None: ...

   def close(self, *, reason: str = ...) -> None: ...


def _require_batch_port(port: object) -> RuntimeBatchPort:
   if not isinstance(port, RuntimeBatchPort):
      _reject("invalid_runtime_batch_port")
   return port


# ---------------------------------------------------------------------------
# Declared records (specification sections 3 and 4)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RuntimeBatchProposal:
   """Proposed membership only.

   The stage runtime owns final membership; this record is never evidence that a
   batch executed.  ``member_refs`` are opaque private-path pseudonyms: no prompt,
   output or token material may appear here.
   """

   compatibility_key: RuntimeBatchKey | None
   member_refs: tuple[str, ...]
   qos_classes: tuple[str, ...]
   maximum_members: int
   maximum_bytes: int
   proposed_at: float
   collection_deadline_at: float

   def __post_init__(self) -> None:
      if not isinstance(self.compatibility_key, RuntimeBatchKey):
         _reject("missing_batch_compatibility_identity")
      _refs(
         self.member_refs,
         code="invalid_batch_member_refs",
         duplicate_code="duplicate_batch_member_ref",
      )
      _bounded_int(
         self.maximum_members,
         code="invalid_batch_member_bound",
         minimum=1,
         maximum=SCAFFOLD_MEMBER_CEILING,
      )
      _bounded_int(
         self.maximum_bytes,
         code="invalid_batch_byte_bound",
         minimum=1,
         maximum=SCAFFOLD_BYTE_CEILING,
      )
      if len(self.member_refs) > self.maximum_members:
         _reject("excess_batch_member_count")
      if not isinstance(self.qos_classes, tuple) or len(self.qos_classes) != len(
         self.member_refs
      ):
         _reject("invalid_batch_qos_count")
      if any(item not in _QOS_CLASSES for item in self.qos_classes):
         _reject("invalid_batch_qos_class")
      proposed_at = _finite(self.proposed_at, code="invalid_batch_proposed_at")
      deadline = _finite(
         self.collection_deadline_at,
         code="invalid_batch_collection_deadline",
      )
      if deadline < proposed_at:
         _reject("invalid_batch_collection_deadline")


@dataclass(frozen=True)
class RuntimeBatchObservation:
   """The runtime-owned record of what a stage actually executed.

   An observation may honestly report fewer actual members than proposed (a rejected
   member or a split), but it may never report zero or omit itself.  Missing
   observations are absent, never zero and never success.
   """

   #: The contract record this dataclass *is* (implementation contract resolution 1,
   #: on behalf of the stage runtime).  A record name is the contract, so it travels
   #: with the record instead of living only in a documentation list.
   PROTOCOL = "mycelium.runtime_batch_observation.v1"

   batch_ref: str
   phase: str
   stage: str
   backend_invocation_id: str
   backend_invocations: int
   membership_authority: str
   proposed_member_count: int
   actual_member_count: int
   member_outcomes: tuple[tuple[str, str], ...]
   collected_at: float
   executed_at: float
   cleanup: str

   def __post_init__(self) -> None:
      _text(self.batch_ref, code="invalid_batch_ref")
      _text(self.phase, code="invalid_batch_phase")
      _text(self.stage, code="invalid_batch_stage")
      _text(self.backend_invocation_id, code="invalid_backend_invocation_id")
      _bounded_int(
         self.backend_invocations,
         code="invalid_backend_invocation_count",
         minimum=1,
         maximum=SCAFFOLD_MEMBER_CEILING,
      )
      if self.membership_authority != MEMBERSHIP_AUTHORITY_STAGE_RUNTIME:
         _reject("invalid_membership_authority")
      _bounded_int(
         self.proposed_member_count,
         code="invalid_proposed_member_count",
         minimum=1,
         maximum=SCAFFOLD_MEMBER_CEILING,
      )
      _bounded_int(
         self.actual_member_count,
         code="invalid_actual_member_count",
         minimum=0,
         maximum=self.proposed_member_count,
      )
      if (
         not isinstance(self.member_outcomes, tuple)
         or len(self.member_outcomes) != self.actual_member_count
      ):
         _reject("invalid_member_outcome_count")
      refs = []
      for entry in self.member_outcomes:
         if not isinstance(entry, tuple) or len(entry) != 2:
            _reject("invalid_member_outcome")
         member_ref, outcome = entry
         _text(member_ref, code="invalid_member_outcome")
         if outcome not in _MEMBER_OUTCOMES:
            _reject("invalid_member_outcome")
         refs.append(member_ref)
      if len(set(refs)) != len(refs):
         _reject("duplicate_member_outcome")
      collected_at = _finite(self.collected_at, code="invalid_observation_intervals")
      executed_at = _finite(self.executed_at, code="invalid_observation_intervals")
      if executed_at < collected_at:
         _reject("invalid_observation_intervals")
      _text(self.cleanup, code="invalid_observation_cleanup")

   def claims_runtime_batch(self) -> bool:
      """True only for the frozen inventory's definition of an observed batch."""

      return (
         self.membership_authority == MEMBERSHIP_AUTHORITY_STAGE_RUNTIME
         and self.actual_member_count >= MINIMUM_PHYSICAL_BATCH_MEMBERS
         and self.backend_invocations == BACKEND_INVOCATIONS_PER_OBSERVED_BATCH
      )


@dataclass(frozen=True)
class StageComputeInterval:
   """One physical stage-compute interval on the monotonic stage clock."""

   interval_ref: str
   request_ref: str
   microbatch_ref: str
   stage_id: str
   placement_id: str
   started_at: float
   finished_at: float
   clock_uncertainty: float

   def __post_init__(self) -> None:
      _text(self.interval_ref, code="invalid_interval_ref")
      _text(self.request_ref, code="invalid_interval_request_ref")
      _text(self.microbatch_ref, code="invalid_interval_microbatch_ref")
      _text(self.stage_id, code="invalid_interval_stage")
      _text(self.placement_id, code="invalid_interval_placement")
      started = _finite(self.started_at, code="invalid_interval")
      finished = _finite(self.finished_at, code="invalid_interval")
      if finished < started:
         _reject("reversed_interval")
      _finite(self.clock_uncertainty, code="invalid_interval_uncertainty", minimum=0.0)


@dataclass(frozen=True)
class PipelineOverlapTrace:
   """A closed overlap trace whose causal verdict is NOT implemented here.

   This scaffold validates interval shape and causal-edge references only.  Every
   causal, clock, stage-application, activation/attempt and loopback rejection of
   specification section 6 remains unimplemented, and the overlap verdict is refused.
   """

   trace_ref: str
   intervals: tuple[StageComputeInterval, ...]
   causal_edges: tuple[tuple[str, str], ...]

   def __post_init__(self) -> None:
      _text(self.trace_ref, code="invalid_trace_ref")
      if (
         not isinstance(self.intervals, tuple)
         or not self.intervals
         or any(not isinstance(item, StageComputeInterval) for item in self.intervals)
      ):
         _reject("invalid_trace_intervals")
      refs = [item.interval_ref for item in self.intervals]
      if len(set(refs)) != len(refs):
         _reject("duplicate_interval_ref")
      known = set(refs)
      if not isinstance(self.causal_edges, tuple):
         _reject("invalid_causal_edges")
      for edge in self.causal_edges:
         if (
            not isinstance(edge, tuple)
            or len(edge) != 2
            or edge[0] not in known
            or edge[1] not in known
            or edge[0] == edge[1]
         ):
            _reject("unknown_causal_endpoint")


@dataclass(frozen=True)
class QualificationRequest:
   """Owner-private qualification inputs; the bundle itself is never public."""

   owner_private_root: str
   benchmark_manifest_digest: str
   acceptance_inventory_digest: str
   baseline_windows: int
   candidate_windows: int
   valid_pairs: int

   def __post_init__(self) -> None:
      _text(self.owner_private_root, code="invalid_qualification_root")
      _digest(self.benchmark_manifest_digest, code="invalid_benchmark_manifest_digest")
      _digest(self.acceptance_inventory_digest, code="invalid_acceptance_inventory_digest")
      # Benchmark protocol v2: exactly one baseline and one candidate window
      # per scheduled pair (the scheduled count is exact), and the valid pairs
      # among them never fall below the pre-registered floor.
      _bounded_int(
         self.baseline_windows,
         code="invalid_benchmark_window_count",
         minimum=SCHEDULED_BENCHMARK_PAIRS,
         maximum=SCHEDULED_BENCHMARK_PAIRS,
      )
      _bounded_int(
         self.candidate_windows,
         code="invalid_benchmark_window_count",
         minimum=SCHEDULED_BENCHMARK_PAIRS,
         maximum=SCHEDULED_BENCHMARK_PAIRS,
      )
      if self.baseline_windows != self.candidate_windows:
         _reject("invalid_benchmark_window_count")
      _bounded_int(
         self.valid_pairs,
         code="invalid_benchmark_valid_pair_count",
         minimum=MINIMUM_VALID_BENCHMARK_PAIRS,
         maximum=SCHEDULED_BENCHMARK_PAIRS,
      )


# ---------------------------------------------------------------------------
# Entry points: validate the closed shape, then refuse explicitly
# ---------------------------------------------------------------------------


def execute_observed_batch(
   port: object,
   proposal: RuntimeBatchProposal,
) -> object:
   """Execute one real backend batch and return the runtime's observation.

   IMPLEMENTED (deterministic slice S1).  Exactly one backend invocation is made for
   the members the runtime accepts, one isolated result is scattered per member, and the
   returned record carries a ``RuntimeBatchObservation`` whose membership the runtime
   owns.  A split, a sequential loop and a member-concurrent dispatch report honestly
   and never claim a runtime batch.  The mechanics live in
   ``mycelium_router.a10_runtime_execution``; this entry point stays the machine source
   named by the implementation contract.
   """

   from mycelium_router.a10_runtime_execution import (
      execute_observed_batch as _execute,
   )

   return _execute(port, proposal)


def observe_pipeline_overlap(trace: object) -> object:
   """Return the overlap verdict for a validated trace.

   IMPLEMENTED (deterministic slice S2).  The verdict is ``observed`` only when two
   physical stage-compute intervals for different microbatches intersect for a positive
   duration after clock uncertainty, on a trace that carries the causal, activation,
   stage and final-loopback binding of specification section 6.  The declared
   ``PipelineOverlapTrace`` record cannot carry that binding, so it is refused with
   ``incomplete_overlap_trace`` rather than being mistaken for a trace that shows no
   overlap.  The mechanics live in ``mycelium_router.a10_runtime_execution``.
   """

   from mycelium_router.a10_runtime_execution import (
      observe_pipeline_overlap as _observe,
   )

   return _observe(trace)


def qualify_runtime_batch(
   request: QualificationRequest,
   evidence: Any = None,
) -> dict[str, Any]:
   """Produce the owner-private runtime-batch qualification bundle.

   Decided from executed evidence only (``mycelium_router.a10_qualification``): the
   runtime-owned live publications (observed batch, continuous late arrival, causal
   overlap), the v2 paired benchmark windows, latency, reliability, output parity,
   residual resources and the physical negatives.  ``qualified`` additionally needs
   live provenance on the ordinary product path.  Without executed evidence there is
   nothing to qualify and the entry point refuses with
   ``runtime_batch_qualification_absent``.
   """

   if not isinstance(request, QualificationRequest):
      _reject("invalid_qualification_request_argument")
   if evidence is None:
      raise A10NotImplementedError("runtime_batch_qualification_absent")
   from mycelium_router.a10_qualification import qualify_runtime_batch_from_evidence

   return qualify_runtime_batch_from_evidence(request, evidence)


BOUNDARIES: tuple[tuple[str, str], ...] = (
   ("qualify_runtime_batch", "runtime_batch_qualification_absent"),
)


@dataclass(frozen=True)
class A10ScaffoldStatus:
   """The scaffold's own honest account of itself."""

   gate_id: str
   protocol: str
   state: str
   claim_boundary: str
   acceptance_inventory: str
   acceptance_inventory_digest: str
   declared_contract_protocols: tuple[str, ...]
   earned_closure_requirements: frozenset[str]
   boundaries: tuple[tuple[str, str], ...]


def scaffold_status() -> A10ScaffoldStatus:
   """Return the scaffold's state; it never reports a promoted state."""

   # Imported here rather than at module scope so the two scaffold modules stay
   # independent and either could be replaced without an import cycle.
   from mycelium_router.a10_target_verification import (
      BOUNDARIES as verifier_boundaries,
   )

   return A10ScaffoldStatus(
      gate_id=A10_GATE_ID,
      protocol=SCAFFOLD_PROTOCOL,
      state=SCAFFOLD_STATE,
      claim_boundary=CLAIM_BOUNDARY,
      acceptance_inventory=ACCEPTANCE_INVENTORY,
      acceptance_inventory_digest=ACCEPTANCE_INVENTORY_DIGEST,
      declared_contract_protocols=DECLARED_CONTRACT_PROTOCOLS,
      earned_closure_requirements=EARNED_CLOSURE_REQUIREMENTS,
      boundaries=BOUNDARIES + verifier_boundaries,
   )


__all__ = [
   "A10ContractError",
   "A10NotImplementedError",
   "A10ScaffoldError",
   "A10ScaffoldStatus",
   "BOUNDARIES",
   "PipelineOverlapTrace",
   "QualificationRequest",
   "RuntimeBatchObservation",
   "RuntimeBatchPort",
   "RuntimeBatchProposal",
   "StageComputeInterval",
   "execute_observed_batch",
   "observe_pipeline_overlap",
   "qualify_runtime_batch",
   "scaffold_status",
]
