# SPDX-License-Identifier: AGPL-3.0-only
"""A10 `overlap_trace_rejection` deterministic negatives.

Frozen invariants covered: reversed or impossible intervals are rejected; missing
predecessor and same-request dependency overlap are rejected; duplicate or skipped
stage application is rejected; activation/attempt/path mismatch is rejected; early
final loopback is rejected; an invalid trace never becomes observed overlap; and a
trace that only overlaps queue, serialization or UI does not pass.

The overlap arithmetic (uncertainty subtraction, implementation contract
resolution 9) is deterministic *given a trace*.  Physical overlap evidence still
requires a live route: the scaffold's ``observe_pipeline_overlap`` boundary keeps
refusing, and the last test in this module guards that.
"""

from __future__ import annotations

from tests.a10_deterministic import ensure_worktree_imports

# Resolve imports inside this lane's worktree even when another test module
# bound ``mycelium_router`` to the standing checkout first (see __init__).
ensure_worktree_imports()

import dataclasses

import pytest

from mycelium_router import a10_runtime_batch as scaffold
from mycelium_router.a10_overlap_trace import (
   HOP_KINDS,
   OverlapTrace,
   OverlapVerdict,
   TraceHop,
   observe_stage_compute_overlap,
)

PATH = "path:one"
ATTEMPT = "attempt:one"


def _hop(interval_ref: str, **overrides) -> TraceHop:
   values = {
      "interval_ref": interval_ref,
      "microbatch_ref": "microbatch:one",
      "request_ref": "request:one",
      "path_ref": PATH,
      "attempt_ref": ATTEMPT,
      "hop_kind": "compute",
      "stage_index": 0,
      "started_at": 0.0,
      "finished_at": 1.0,
      "clock_uncertainty": 0.0,
      "input_digest": "digest:in",
      "output_digest": "digest:out",
   }
   values.update(overrides)
   return TraceHop(**values)


def _chain(
   microbatch: str = "microbatch:one",
   request: str = "request:one",
   *,
   stages: int = 1,
   start: float = 0.0,
   span: float = 1.0,
) -> tuple[list[TraceHop], list[tuple[str, str]]]:
   """A complete microbatch application: stage computes, target commit, loopback."""

   hops = [
      _hop(
         f"compute:{microbatch}:s{index}",
         microbatch_ref=microbatch,
         request_ref=request,
         stage_index=index,
         started_at=start + index * span,
         finished_at=start + (index + 1) * span,
         input_digest=f"digest:{microbatch}:{index}",
         output_digest=f"digest:{microbatch}:{index + 1}",
      )
      for index in range(stages)
   ]
   edges = [
      (f"compute:{microbatch}:s{index - 1}", f"compute:{microbatch}:s{index}")
      for index in range(1, stages)
   ]
   last_finish = start + stages * span
   hops.append(
      _hop(
         f"commit:{microbatch}",
         microbatch_ref=microbatch,
         request_ref=request,
         hop_kind="commit",
         stage_index=-1,
         started_at=last_finish,
         finished_at=last_finish + 0.5,
      )
   )
   hops.append(
      _hop(
         f"loopback:{microbatch}",
         microbatch_ref=microbatch,
         request_ref=request,
         hop_kind="loopback",
         stage_index=-1,
         started_at=last_finish + 0.5,
         finished_at=last_finish + 1.0,
      )
   )
   edges.append((f"compute:{microbatch}:s{stages - 1}", f"commit:{microbatch}"))
   edges.append((f"commit:{microbatch}", f"loopback:{microbatch}"))
   return hops, edges


def _trace(chains=None, *, stage_count: int = 1, hops=None, causal_edges=None):
   if chains is None:
      chains = (_chain(),)
   built_hops = tuple(hop for chain, _ in chains for hop in chain)
   built_edges = tuple(edge for _, chain_edges in chains for edge in chain_edges)
   return OverlapTrace(
      trace_ref="trace:one",
      path_ref=PATH,
      attempt_ref=ATTEMPT,
      stage_count=stage_count,
      commit_interval_ref="commit:microbatch:one",
      hops=hops if hops is not None else built_hops,
      causal_edges=causal_edges if causal_edges is not None else built_edges,
   )


def _replaced(chain, interval_ref: str, **overrides):
   """One chain with a single hop replaced."""

   hops, edges = chain
   return (
      [
         dataclasses.replace(hop, **overrides) if hop.interval_ref == interval_ref else hop
         for hop in hops
      ],
      edges,
   )


def _with_uncertainty(chain, value: float):
   hops, _edges = chain
   return [dataclasses.replace(hop, clock_uncertainty=value) for hop in hops]


def _code(exc: pytest.ExceptionInfo) -> str:
   return exc.value.code


# ---------------------------------------------------------------------------
# Queue, serialization and UI overlap alone never pass
# ---------------------------------------------------------------------------


def test_queue_transport_and_ui_overlap_alone_do_not_pass():
   first = _chain("microbatch:one", "request:one", start=0.0)
   second = _chain("microbatch:two", "request:two", start=5.0)
   hops = list(first[0]) + list(second[0])
   hops.append(
      _hop(
         "queue:one",
         microbatch_ref="microbatch:one",
         request_ref="request:one",
         hop_kind="queue",
         stage_index=-1,
         started_at=0.0,
         finished_at=6.0,
      )
   )
   hops.append(
      _hop(
         "queue:two",
         microbatch_ref="microbatch:two",
         request_ref="request:two",
         hop_kind="queue",
         stage_index=-1,
         started_at=1.0,
         finished_at=6.0,
      )
   )
   hops.append(
      _hop(
         "ui:one",
         microbatch_ref="microbatch:one",
         request_ref="request:one",
         hop_kind="ui",
         stage_index=-1,
         started_at=0.0,
         finished_at=7.0,
      )
   )

   verdict = observe_stage_compute_overlap(_trace((first, second), hops=tuple(hops)))

   assert isinstance(verdict, OverlapVerdict)
   assert verdict.observed is False
   assert verdict.reason == "queue_serialization_or_ui_overlap_only"
   assert verdict.pair is None
   assert verdict.intersection_seconds == 0.0
   assert HOP_KINDS == (
      "queue",
      "compute",
      "send",
      "receive",
      "wait",
      "commit",
      "loopback",
      "ui",
   )


# ---------------------------------------------------------------------------
# Rejections: shape, order, application, causality, loopback
# ---------------------------------------------------------------------------


def test_reversed_intervals_are_rejected():
   with pytest.raises(scaffold.A10ContractError) as raised:
      _hop("compute:bad", started_at=1.0, finished_at=0.5)
   assert _code(raised) == "reversed_or_impossible_interval"


def test_impossible_stage_clock_is_rejected():
   chain = _replaced(
      _chain("microbatch:one", "request:one", stages=2),
      "compute:microbatch:one:s1",
      started_at=-2.0,
      finished_at=0.5,
   )
   with pytest.raises(scaffold.A10ContractError) as raised:
      observe_stage_compute_overlap(_trace((chain,), stage_count=2))
   assert _code(raised) == "impossible_stage_clock"


def test_duplicate_stage_application_is_rejected():
   hops, edges = _chain("microbatch:one", "request:one", stages=2)
   duplicated = tuple(hops) + (
      _hop(
         "compute:microbatch:one:s0-again",
         microbatch_ref="microbatch:one",
         request_ref="request:one",
         stage_index=0,
         started_at=0.0,
         finished_at=0.5,
         input_digest="digest:microbatch:one:0",
         output_digest="digest:microbatch:one:1",
      ),
   )
   with pytest.raises(scaffold.A10ContractError) as raised:
      observe_stage_compute_overlap(_trace(hops=duplicated, causal_edges=tuple(edges)))
   assert _code(raised) == "duplicate_stage_application"


def test_skipped_stage_is_rejected():
   hops, edges = _chain("microbatch:one", "request:one", stages=3)
   skipped = tuple(
      hop for hop in hops if hop.interval_ref != "compute:microbatch:one:s1"
   )
   surviving_edges = tuple(
      edge for edge in edges if "compute:microbatch:one:s1" not in edge
   )
   with pytest.raises(scaffold.A10ContractError) as raised:
      observe_stage_compute_overlap(
         _trace(hops=skipped, causal_edges=surviving_edges, stage_count=3)
      )
   assert _code(raised) == "skipped_stage_application"


def test_missing_causal_predecessor_is_rejected():
   hops, edges = _chain("microbatch:one", "request:one", stages=2)
   without_stage_edge = tuple(edge for edge in edges if edge[0].endswith(":s0"))
   with pytest.raises(scaffold.A10ContractError) as raised:
      observe_stage_compute_overlap(
         _trace(hops=tuple(hops), causal_edges=without_stage_edge, stage_count=2)
      )
   assert _code(raised) == "missing_causal_predecessor"


def test_same_request_dependency_overlap_is_rejected():
   chain = _replaced(
      _chain("microbatch:one", "request:one", stages=2),
      "compute:microbatch:one:s1",
      started_at=0.5,
      finished_at=1.5,
   )
   with pytest.raises(scaffold.A10ContractError) as raised:
      observe_stage_compute_overlap(_trace((chain,), stage_count=2))
   assert _code(raised) == "same_request_dependency_overlap"


def test_activation_attempt_and_path_mismatch_are_rejected():
   tampered = _replaced(
      _chain("microbatch:one", "request:one", stages=2),
      "compute:microbatch:one:s1",
      input_digest="digest:tampered",
   )
   with pytest.raises(scaffold.A10ContractError) as raised:
      observe_stage_compute_overlap(_trace((tampered,), stage_count=2))
   assert _code(raised) == "activation_chain_mismatch"

   elsewhere = _replaced(
      _chain("microbatch:one", "request:one", stages=2),
      "compute:microbatch:one:s0",
      path_ref="path:elsewhere",
   )
   with pytest.raises(scaffold.A10ContractError) as mismatch:
      observe_stage_compute_overlap(_trace((elsewhere,), stage_count=2))
   assert _code(mismatch) == "path_or_attempt_mismatch"


def test_early_final_loopback_is_rejected():
   early = _replaced(
      _chain("microbatch:one", "request:one", stages=1),
      "loopback:microbatch:one",
      started_at=1.0,
      finished_at=2.0,
   )
   with pytest.raises(scaffold.A10ContractError) as raised:
      observe_stage_compute_overlap(_trace((early,)))
   assert _code(raised) == "final_loopback_before_commit"


def test_invalid_trace_never_becomes_observed_overlap():
   hops, edges = _chain("microbatch:one", "request:one", stages=2)
   duplicated = tuple(hops) + (
      _hop(
         "compute:microbatch:one:s0-again",
         microbatch_ref="microbatch:one",
         request_ref="request:one",
         stage_index=0,
         started_at=0.0,
         finished_at=0.5,
      ),
   )
   lacking_edge = tuple(edge for edge in edges if edge[0].endswith(":s0"))
   candidates = {
      "duplicate_stage_application": _trace(
         hops=duplicated, causal_edges=tuple(edges), stage_count=2
      ),
      "missing_causal_predecessor": _trace(
         hops=tuple(hops), causal_edges=lacking_edge, stage_count=2
      ),
   }
   for code, trace in candidates.items():
      with pytest.raises(scaffold.A10ContractError) as raised:
         verdict = observe_stage_compute_overlap(trace)
         pytest.fail(f"invalid trace produced {verdict!r}")
      assert _code(raised) == code


# ---------------------------------------------------------------------------
# The arithmetic boundary, and the boundary this lane does not cross
# ---------------------------------------------------------------------------


def test_uncertainty_subtraction_boundary_is_exact():
   first = _chain("microbatch:one", "request:one", start=0.0)
   second = _chain("microbatch:two", "request:two", start=0.5)

   # min(finish) - max(start) - (uncertainty_a + uncertainty_b)
   #   = 1.0 - 0.5 - (0.25 + 0.25) = 0 -> not observed.
   touching = _trace(
      (
         (_with_uncertainty(first, 0.25), first[1]),
         (_with_uncertainty(second, 0.25), second[1]),
      )
   )
   verdict = observe_stage_compute_overlap(touching)
   assert verdict.observed is False
   assert verdict.reason == "no_positive_stage_compute_intersection"

   positive = observe_stage_compute_overlap(_trace((first, second)))
   assert positive.observed is True
   assert positive.reason == "observed_stage_compute_intersection"
   assert positive.pair == (
      "compute:microbatch:one:s0",
      "compute:microbatch:two:s0",
   )
   assert positive.intersection_seconds == pytest.approx(0.5)


def test_the_overlap_entry_point_produces_the_verdict_for_a_full_trace():
   """Deterministic arithmetic over a bound trace is the overlap verdict."""

   verdict = scaffold.observe_pipeline_overlap(
      _trace(chains=(_chain(), _chain("microbatch:two", "request:two")))
   )
   assert verdict.observed is True
   assert verdict.reason == "observed_stage_compute_intersection"


def test_an_overlap_record_without_causal_binding_is_refused():
   """The interval-only record cannot carry the section 6 causal evidence.

   It is refused with a coded contract error -- never mistaken for a trace that merely
   shows no overlap, and never promoted to a physical overlap claim.
   """

   with pytest.raises(scaffold.A10ContractError) as raised:
      scaffold.observe_pipeline_overlap(
         scaffold.PipelineOverlapTrace(
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
      )
   assert raised.value.code == "incomplete_overlap_trace"


# ---------------------------------------------------------------------------
# A dependency edge that runs backwards by no more than the clock uncertainty
# ---------------------------------------------------------------------------


def _skewed_chain(microbatch, request, *, start, skew, uncertainty):
   """Two stages whose second compute starts ``skew`` before the first finishes."""

   chain = _chain(microbatch, request, stages=2, start=start)
   hops, edges = chain
   first_finish = start + 1.0
   hops = [
      dataclasses.replace(
         hop,
         clock_uncertainty=uncertainty,
         **(
            {"started_at": first_finish - skew, "finished_at": first_finish - skew + 1.0}
            if hop.interval_ref == f"compute:{microbatch}:s1"
            else {}
         ),
      )
      for hop in hops
   ]
   return hops, edges


def test_an_edge_violation_within_the_clock_uncertainty_is_unresolved_not_rejected():
   chain = _skewed_chain("microbatch:one", "request:one", start=0.0, skew=0.25, uncertainty=0.25)

   verdict = observe_stage_compute_overlap(_trace((chain,), stage_count=2))

   assert verdict.observed is False
   assert verdict.reason == "unresolved_dependency_edge_within_uncertainty"
   assert verdict.unresolved_edges == (
      ("compute:microbatch:one:s0", "compute:microbatch:one:s1"),
   )


def test_an_edge_violation_exactly_at_the_uncertainty_bound_is_unresolved():
   chain = _skewed_chain("microbatch:one", "request:one", start=0.0, skew=0.5, uncertainty=0.25)

   verdict = observe_stage_compute_overlap(_trace((chain,), stage_count=2))

   assert verdict.unresolved_edges == (
      ("compute:microbatch:one:s0", "compute:microbatch:one:s1"),
   )


def test_an_edge_violation_beyond_the_clock_uncertainty_is_still_rejected():
   chain = _skewed_chain("microbatch:one", "request:one", start=0.0, skew=0.5625, uncertainty=0.25)

   with pytest.raises(scaffold.A10ContractError) as raised:
      observe_stage_compute_overlap(_trace((chain,), stage_count=2))
   assert _code(raised) == "same_request_dependency_overlap"


def test_a_clean_trace_reports_no_unresolved_edges():
   verdict = observe_stage_compute_overlap(_trace())

   assert verdict.unresolved_edges == ()


def test_an_unresolved_edge_never_counts_as_overlap_evidence():
   """The second request overlaps only compute hops of the unresolved edge."""

   skewed = _skewed_chain("microbatch:one", "request:one", start=0.0, skew=0.125, uncertainty=0.0625)
   other = _chain("microbatch:two", "request:two", stages=2, start=0.5)
   other = (_with_uncertainty(other, 0.0625), other[1])

   verdict = observe_stage_compute_overlap(_trace((skewed, other), stage_count=2))

   assert verdict.observed is False
   assert verdict.pair is None
   assert verdict.intersection_seconds == 0.0
   assert verdict.reason == "unresolved_dependency_edge_within_uncertainty"
   assert verdict.unresolved_edges


def test_overlap_between_hops_outside_an_unresolved_edge_is_still_observed():
   skewed = _skewed_chain("microbatch:one", "request:one", start=0.0, skew=0.125, uncertainty=0.0625)
   second = _chain("microbatch:two", "request:two", stages=2, start=10.0)
   third = _chain("microbatch:three", "request:three", stages=2, start=10.5)

   verdict = observe_stage_compute_overlap(_trace((skewed, second, third), stage_count=2))

   assert verdict.observed is True
   assert verdict.reason == "observed_stage_compute_intersection"
   assert set(verdict.pair) <= {
      "compute:microbatch:two:s0",
      "compute:microbatch:two:s1",
      "compute:microbatch:three:s0",
      "compute:microbatch:three:s1",
   }
   assert verdict.unresolved_edges == (
      ("compute:microbatch:one:s0", "compute:microbatch:one:s1"),
   )
