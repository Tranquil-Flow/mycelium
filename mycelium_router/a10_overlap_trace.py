# SPDX-License-Identifier: AGPL-3.0-only
"""A10 deterministic pipeline-overlap trace validation.

The specification (section 6) records synchronized monotonic intervals for queue,
runtime compute, send, receive and bounded wait at each placement, plus
predecessor/successor causal edges and clock-correlation uncertainty.  Overlap is
``observed`` only when, after accounting for timestamp uncertainty, at least two
*physical stage-compute* intervals for different microbatches intersect for a
positive duration — ``min(finish_a, finish_b) - max(start_a, start_b) -
(uncertainty_a + uncertainty_b) > 0`` (implementation contract resolution 9).

A trace that only overlaps queueing, serialization or UI does not pass.  Reversed
intervals, impossible clocks, missing causal predecessors, same-request dependency
overlap, duplicated stage application, skipped stages, activation/attempt/path
mismatch and final loopback before the target commit are all rejected, so an
invalid trace can never become observed overlap.

This is the deterministic half: the arithmetic is decided for a *given* trace
record.  Physical overlap evidence still needs a live route, and the scaffold's
``observe_pipeline_overlap`` boundary keeps refusing.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import NoReturn

from mycelium_router.a10_runtime_batch import A10ContractError


#: The hop kinds a correlated stage-clock trace may carry.
HOP_KINDS = (
   "queue",
   "compute",
   "send",
   "receive",
   "wait",
   "commit",
   "loopback",
   "ui",
)

#: Hop kinds whose overlap can never qualify: only stage compute counts.
NON_QUALIFYING_OVERLAP_KINDS = ("queue", "send", "receive", "wait", "ui")


class A10TraceError(A10ContractError):
   """A coded, fail-closed overlap-trace refusal."""


def _reject(code: str) -> NoReturn:
   raise A10TraceError(code)


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


@dataclass(frozen=True)
class TraceHop:
   """One synchronized hop interval on the single correlated stage clock."""

   interval_ref: str
   microbatch_ref: str
   request_ref: str
   path_ref: str
   attempt_ref: str
   hop_kind: str
   stage_index: int
   started_at: float
   finished_at: float
   clock_uncertainty: float
   input_digest: str = ""
   output_digest: str = ""

   def __post_init__(self) -> None:
      _text(self.interval_ref, code="invalid_trace_hop")
      _text(self.microbatch_ref, code="invalid_trace_hop")
      _text(self.request_ref, code="invalid_trace_hop")
      _text(self.path_ref, code="invalid_trace_hop")
      _text(self.attempt_ref, code="invalid_trace_hop")
      if self.hop_kind not in HOP_KINDS:
         _reject("invalid_trace_hop")
      if isinstance(self.stage_index, bool) or not isinstance(self.stage_index, int):
         _reject("invalid_trace_hop")
      if self.hop_kind == "compute":
         if self.stage_index < 0:
            _reject("invalid_trace_hop")
      elif self.stage_index != -1:
         _reject("invalid_trace_hop")
      started = _finite(self.started_at, code="reversed_or_impossible_interval")
      finished = _finite(self.finished_at, code="reversed_or_impossible_interval")
      if finished < started:
         _reject("reversed_or_impossible_interval")
      _finite(self.clock_uncertainty, code="invalid_trace_uncertainty", minimum=0.0)

   @property
   def is_compute(self) -> bool:
      return self.hop_kind == "compute"


@dataclass(frozen=True)
class OverlapTrace:
   """A closed, stage-clock-correlated trace with causal edges."""

   trace_ref: str
   path_ref: str
   attempt_ref: str
   stage_count: int
   commit_interval_ref: str
   hops: tuple[TraceHop, ...]
   causal_edges: tuple[tuple[str, str], ...]

   def __post_init__(self) -> None:
      _text(self.trace_ref, code="invalid_trace_ref")
      _text(self.path_ref, code="invalid_trace_ref")
      _text(self.attempt_ref, code="invalid_trace_ref")
      _text(self.commit_interval_ref, code="invalid_trace_ref")
      if (
         isinstance(self.stage_count, bool)
         or not isinstance(self.stage_count, int)
         or self.stage_count < 1
      ):
         _reject("invalid_trace_stage_count")
      if (
         not isinstance(self.hops, tuple)
         or not self.hops
         or any(not isinstance(hop, TraceHop) for hop in self.hops)
      ):
         _reject("invalid_trace_intervals")
      refs = {hop.interval_ref for hop in self.hops}
      if len(refs) != len(self.hops):
         _reject("duplicate_interval_ref")
      if not isinstance(self.causal_edges, tuple):
         _reject("invalid_causal_edges")
      for edge in self.causal_edges:
         if (
            not isinstance(edge, tuple)
            or len(edge) != 2
            or edge[0] not in refs
            or edge[1] not in refs
            or edge[0] == edge[1]
         ):
            _reject("missing_causal_predecessor")


@dataclass(frozen=True)
class OverlapVerdict:
   """The deterministic overlap verdict for one validated trace."""

   observed: bool
   reason: str
   pair: tuple[str, str] | None
   intersection_seconds: float
   #: Causal edges whose successor started before its predecessor finished by no
   #: more than the two hops' clock uncertainty: neither a dependency violation
   #: nor ordering evidence.  Hops of these edges never count as overlap evidence.
   unresolved_edges: tuple[tuple[str, str], ...] = ()


def _intersection_after_uncertainty(
   first: TraceHop, second: TraceHop
) -> float:
   return (
      min(first.finished_at, second.finished_at)
      - max(first.started_at, second.started_at)
      - (first.clock_uncertainty + second.clock_uncertainty)
   )


def observe_stage_compute_overlap(trace: OverlapTrace) -> OverlapVerdict:
   """Validate ``trace`` and decide overlap from stage compute alone.

   Invalid traces raise a coded refusal; a valid trace returns ``observed=False``
   when only queueing, serialization, transport, wait or UI hops overlap.
   """

   if not isinstance(trace, OverlapTrace):
      _reject("invalid_pipeline_overlap_trace_argument")

   _check_binding(trace)
   compute_by_microbatch = _check_stage_applications(trace)
   _check_activation_chain(compute_by_microbatch)
   _check_causal_edges(trace, compute_by_microbatch)
   _check_stage_clocks(trace)
   _check_final_loopback(trace)
   unresolved_edges = _check_dependency_timing(trace)
   unresolved_refs = {ref for edge in unresolved_edges for ref in edge}

   compute = [
      hop
      for hop in trace.hops
      if hop.is_compute and hop.interval_ref not in unresolved_refs
   ]
   for index, first in enumerate(compute):
      for second in compute[index + 1 :]:
         if first.request_ref == second.request_ref:
            continue
         intersection = _intersection_after_uncertainty(first, second)
         if intersection > 0.0:
            return OverlapVerdict(
               observed=True,
               reason="observed_stage_compute_intersection",
               pair=(first.interval_ref, second.interval_ref),
               intersection_seconds=intersection,
               unresolved_edges=unresolved_edges,
            )

   for index, first in enumerate(trace.hops):
      for second in trace.hops[index + 1 :]:
         if first.request_ref == second.request_ref:
            continue
         if first.hop_kind not in NON_QUALIFYING_OVERLAP_KINDS:
            continue
         if second.hop_kind not in NON_QUALIFYING_OVERLAP_KINDS:
            continue
         if _intersection_after_uncertainty(first, second) > 0.0:
            return OverlapVerdict(
               observed=False,
               reason="queue_serialization_or_ui_overlap_only",
               pair=None,
               intersection_seconds=0.0,
               unresolved_edges=unresolved_edges,
            )

   return OverlapVerdict(
      observed=False,
      reason=(
         "unresolved_dependency_edge_within_uncertainty"
         if unresolved_edges
         else "no_positive_stage_compute_intersection"
      ),
      pair=None,
      intersection_seconds=0.0,
      unresolved_edges=unresolved_edges,
   )


def _check_binding(trace: OverlapTrace) -> None:
   for hop in trace.hops:
      if hop.path_ref != trace.path_ref:
         _reject("path_or_attempt_mismatch")
      if hop.attempt_ref != trace.attempt_ref:
         _reject("path_or_attempt_mismatch")


def _check_stage_applications(
   trace: OverlapTrace,
) -> dict[str, list[TraceHop]]:
   compute_by_microbatch: dict[str, list[TraceHop]] = {}
   seen: set[tuple[str, int]] = set()
   for hop in trace.hops:
      if not hop.is_compute:
         continue
      key = (hop.microbatch_ref, hop.stage_index)
      if key in seen:
         _reject("duplicate_stage_application")
      seen.add(key)
      compute_by_microbatch.setdefault(hop.microbatch_ref, []).append(hop)
   for hops in compute_by_microbatch.values():
      indices = sorted(hop.stage_index for hop in hops)
      if indices != list(range(trace.stage_count)):
         _reject("skipped_stage_application")
   return compute_by_microbatch


def _check_activation_chain(
   compute_by_microbatch: dict[str, list[TraceHop]],
) -> None:
   for hops in compute_by_microbatch.values():
      ordered = sorted(hops, key=lambda hop: hop.stage_index)
      for previous, current in zip(ordered, ordered[1:]):
         if (
            not previous.output_digest
            or not current.input_digest
            or previous.output_digest != current.input_digest
         ):
            _reject("activation_chain_mismatch")


def _check_causal_edges(
   trace: OverlapTrace,
   compute_by_microbatch: dict[str, list[TraceHop]],
) -> None:
   by_ref = {hop.interval_ref: hop for hop in trace.hops}
   incoming: dict[str, list[str]] = {}
   for predecessor_ref, successor_ref in trace.causal_edges:
      incoming.setdefault(successor_ref, []).append(predecessor_ref)
   for hops in compute_by_microbatch.values():
      ordered = sorted(hops, key=lambda hop: hop.stage_index)
      for previous, current in zip(ordered, ordered[1:]):
         if previous.interval_ref not in incoming.get(current.interval_ref, []):
            _reject("missing_causal_predecessor")
   for hop in trace.hops:
      if hop.hop_kind == "loopback" and hop.interval_ref not in incoming:
         _reject("missing_causal_predecessor")
   for hop in trace.hops:
      for predecessor_ref in incoming.get(hop.interval_ref, []):
         if predecessor_ref not in by_ref:
            _reject("missing_causal_predecessor")


def _check_stage_clocks(trace: OverlapTrace) -> None:
   by_ref = {hop.interval_ref: hop for hop in trace.hops}
   for predecessor_ref, successor_ref in trace.causal_edges:
      predecessor = by_ref[predecessor_ref]
      successor = by_ref[successor_ref]
      if (
         successor.started_at + successor.clock_uncertainty
         < predecessor.started_at
      ):
         _reject("impossible_stage_clock")


def _check_final_loopback(trace: OverlapTrace) -> None:
   by_ref = {hop.interval_ref: hop for hop in trace.hops}
   commit = by_ref.get(trace.commit_interval_ref)
   if commit is None or commit.hop_kind != "commit":
      _reject("missing_causal_predecessor")
   for hop in trace.hops:
      if hop.hop_kind != "loopback":
         continue
      if hop.started_at < commit.finished_at:
         _reject("final_loopback_before_commit")


def _check_dependency_timing(
   trace: OverlapTrace,
) -> tuple[tuple[str, str], ...]:
   """Reject dependency overlap; return the edges that cannot be resolved.

   A successor that starts before its predecessor finished by more than the two
   hops' summed clock uncertainty is a dependency violation.  By no more than
   that bound, the clocks cannot say which came first: the edge is *unresolved*,
   neither a violation nor ordering evidence (the same uncertainty the overlap
   arithmetic subtracts).
   """

   by_ref = {hop.interval_ref: hop for hop in trace.hops}
   unresolved: list[tuple[str, str]] = []
   for predecessor_ref, successor_ref in trace.causal_edges:
      predecessor = by_ref[predecessor_ref]
      successor = by_ref[successor_ref]
      backwards = predecessor.finished_at - successor.started_at
      if backwards > 0.0:
         if backwards > predecessor.clock_uncertainty + successor.clock_uncertainty:
            _reject("same_request_dependency_overlap")
         unresolved.append((predecessor_ref, successor_ref))
   compute = [hop for hop in trace.hops if hop.is_compute]
   for index, first in enumerate(compute):
      for second in compute[index + 1 :]:
         if first.request_ref != second.request_ref:
            continue
         if _intersection_after_uncertainty(first, second) > 0.0:
            _reject("same_request_dependency_overlap")
   return tuple(unresolved)
