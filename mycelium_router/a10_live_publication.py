# SPDX-License-Identifier: AGPL-3.0-only
"""A10 live publication: runtime-owned batch observations and stage-compute traces.

A stage runtime on the live route records, for every stage operation it actually
executed, one privacy-reduced *stage-compute interval* on its own monotonic clock,
and it keeps the runtime-owned ``mycelium.runtime_batch_observation.v1`` records its
batch dispatcher already publishes.  ``StageComputePublication.snapshot`` projects
both into one bounded, closed document (``PUBLICATION_PROTOCOL``) that the node
co-signs inside its ordinary signed snapshot (``physical_inference_node``), so the
route receives it with every live snapshot.

``build_overlap_trace`` turns the publications of the route's nodes into the
causal ``OverlapTrace`` that ``a10_overlap_trace.observe_stage_compute_overlap``
judges: node-local monotonic intervals are mapped onto one wall-clock timeline by
each publication's own clock anchor, and the declared cross-host clock uncertainty
is added to every interval, so overlap is only ever claimed after subtracting it.

Privacy: request, path and microbatch identities are SHA-256 pseudonyms; activation
digests are SHA-256 over activation bytes; token-ID payloads and final-stage outputs
are never digested (a digest of a short token sequence is reversible by
enumeration).  No prompt, output, token, logit, tensor or KV byte leaves the runtime.

Bounds: at most ``MAXIMUM_INTERVALS`` intervals and ``MAXIMUM_OBSERVATIONS``
observations are retained or published; older entries are dropped and counted, never
silently summarized.  A missing publication is absent, never zero.
"""

from __future__ import annotations

import hashlib
import math
import threading
import time
from collections import deque
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict
from typing import Any, NoReturn

from mycelium_router.a10_overlap_trace import OverlapTrace, TraceHop
from mycelium_router.a10_runtime_batch import A10ContractError, RuntimeBatchObservation


PUBLICATION_PROTOCOL = "mycelium.a10_live_publication.v1"
INTERVAL_PROTOCOL = "mycelium.a10_stage_compute_interval.v1"
MAXIMUM_INTERVALS = 256
MAXIMUM_OBSERVATIONS = 64
_HOP_KINDS = ("compute", "commit")
PUBLICATION_FIELDS = frozenset(
   {
      "protocol",
      "clock",
      "route_path_ref",
      "route_attempt_ref",
      "stage_count",
      "intervals",
      "dropped_interval_count",
      "runtime_batch_observations",
      "dropped_observation_count",
      "bounds",
   }
)
INTERVAL_FIELDS = frozenset(
   {
      "interval_ref",
      "hop_kind",
      "request_ref",
      "microbatch_ref",
      "stage_index",
      "phase",
      "started_at",
      "finished_at",
      "clock_resolution",
      "input_digest",
      "output_digest",
      "runtime_batch_ref",
   }
)
CLOCK_FIELDS = frozenset({"domain_ref", "monotonic_anchor", "wall_anchor", "resolution"})


class A10PublicationError(A10ContractError):
   """A coded, fail-closed publication refusal."""


def _reject(code: str) -> NoReturn:
   raise A10PublicationError(code)


def pseudonym(kind: str, value: object) -> str:
   """A stable SHA-256 pseudonym, equal on every node for the same identity."""

   material = f"mycelium.a10.{kind}:{value}".encode("utf-8")
   return "sha256:" + hashlib.sha256(material).hexdigest()


def activation_digest(payload: object) -> str:
   if not isinstance(payload, (bytes, bytearray)) or not payload:
      return ""
   return "sha256:" + hashlib.sha256(bytes(payload)).hexdigest()


def route_identity(graph: Any) -> tuple[str, str, int]:
   """The route path/attempt identity shared by every node of one graph."""

   chain = [
      [stage.stage_id, [placement.placement_id for placement in stage.placements]]
      for stage in graph.stages
   ]
   path_ref = pseudonym(
      "route_path",
      [graph.deployment_id, graph.deployment_epoch, graph.manifest_digest, chain],
   )
   attempt_ref = pseudonym("route_attempt", [graph.deployment_id, graph.topology_version])
   return path_ref, attempt_ref, len(graph.stages)


class StageComputePublication:
   """Bounded recorder of one runtime's executed stage-compute intervals."""

   def __init__(
      self,
      *,
      node_id: str,
      graph: Any,
      clock: Any = None,
      wall_clock: Any = None,
   ) -> None:
      self._clock = clock or time.monotonic
      self._wall = wall_clock or time.time
      self._lock = threading.Lock()
      self._intervals: deque[dict[str, Any]] = deque(maxlen=MAXIMUM_INTERVALS)
      self._recorded = 0
      self._sequence = 0
      self._stage_index = {stage.stage_id: index for index, stage in enumerate(graph.stages)}
      self._placement_stage = {
         placement.placement_id: stage.stage_id
         for stage in graph.stages
         for placement in stage.placements
      }
      self._final_stage_id = graph.final_stage_id
      self._path_ref, self._attempt_ref, self._stage_count = route_identity(graph)
      self._domain_ref = pseudonym("clock_domain", [node_id, id(self)])
      self._monotonic_anchor = float(self._clock())
      self._wall_anchor = float(self._wall())
      try:
         resolution = float(time.get_clock_info("monotonic").resolution)
      except (AttributeError, ValueError):
         resolution = 1e-6
      self._resolution = resolution if math.isfinite(resolution) and resolution > 0 else 1e-6

   def now(self) -> float:
      return float(self._clock())

   def record(
      self,
      item: Any,
      *,
      started_at: float,
      finished_at: float,
      result: Any,
      runtime_batch_ref: str = "",
   ) -> None:
      """Record one executed stage operation; failures and unknown stages are skipped."""

      if not getattr(result, "success", False):
         return
      stage_id = self._placement_stage.get(getattr(item, "placement_id", None))
      if stage_id is None:
         return
      stage_index = self._stage_index[stage_id]
      microbatch = pseudonym(
         "microbatch",
         [item.request_id, item.path_attempt, item.phase, item.token_index],
      )
      request_ref = pseudonym("request", item.request_id)
      entry = stage_index == 0
      final = stage_id == self._final_stage_id
      with self._lock:
         self._sequence += 1
         self._recorded += 1
         interval = {
            "interval_ref": f"interval:{self._domain_ref[7:23]}:{self._sequence}",
            "hop_kind": "compute",
            "request_ref": request_ref,
            "microbatch_ref": microbatch,
            "stage_index": stage_index,
            "phase": str(item.phase),
            "started_at": float(started_at),
            "finished_at": float(max(finished_at, started_at)),
            "clock_resolution": self._resolution,
            # Token-ID payloads (entry input) and final-stage outputs are never
            # digested: a digest of a short token sequence is reversible.
            "input_digest": "" if entry else activation_digest(item.payload),
            "output_digest": "" if final else activation_digest(getattr(result, "payload", None)),
            "runtime_batch_ref": runtime_batch_ref,
         }
         self._intervals.append(interval)
         if final and getattr(item, "emits_token", False) and result.token_id is not None:
            # The final stage's target-owned result for this token is published
            # here; the route's commit of it happens after this interval ends.
            self._sequence += 1
            self._recorded += 1
            self._intervals.append(
               {
                  **interval,
                  "interval_ref": f"interval:{self._domain_ref[7:23]}:{self._sequence}",
                  "hop_kind": "commit",
                  "stage_index": -1,
                  "started_at": interval["finished_at"],
                  "finished_at": interval["finished_at"],
                  "input_digest": "",
                  "output_digest": "",
               }
            )

   def snapshot(
      self, *, batch_observations: Sequence[RuntimeBatchObservation] = ()
   ) -> dict[str, Any]:
      with self._lock:
         intervals = [dict(interval) for interval in self._intervals]
         recorded = self._recorded
      observations = list(batch_observations)[-MAXIMUM_OBSERVATIONS:]
      document = {
         "protocol": PUBLICATION_PROTOCOL,
         "clock": {
            "domain_ref": self._domain_ref,
            "monotonic_anchor": self._monotonic_anchor,
            "wall_anchor": self._wall_anchor,
            "resolution": self._resolution,
         },
         "route_path_ref": self._path_ref,
         "route_attempt_ref": self._attempt_ref,
         "stage_count": self._stage_count,
         "intervals": intervals,
         "dropped_interval_count": recorded - len(intervals),
         "runtime_batch_observations": [
            _observation_document(observation) for observation in observations
         ],
         "dropped_observation_count": max(0, len(batch_observations) - len(observations)),
         "bounds": {
            "maximum_intervals": MAXIMUM_INTERVALS,
            "maximum_observations": MAXIMUM_OBSERVATIONS,
         },
      }
      return validate_publication(document)


def _observation_document(observation: RuntimeBatchObservation) -> dict[str, Any]:
   document = asdict(observation)
   document["protocol"] = RuntimeBatchObservation.PROTOCOL
   document["member_outcomes"] = [
      [pseudonym("batch_member", ref), outcome] for ref, outcome in observation.member_outcomes
   ]
   document["claims_runtime_batch"] = observation.claims_runtime_batch()
   return document


def _finite(value: object, code: str) -> float:
   if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
      _reject(code)
   return float(value)


def _pseudonym_text(value: object, code: str, *, allow_empty: bool = False) -> str:
   if allow_empty and value == "":
      return ""
   if not isinstance(value, str) or not value.startswith("sha256:") or len(value) != 71:
      _reject(code)
   return value


def validate_publication(document: object) -> dict[str, Any]:
   """Validate one closed, bounded, privacy-reduced publication document."""

   if not isinstance(document, Mapping) or set(document) != PUBLICATION_FIELDS:
      _reject("a10_publication_fields_invalid")
   if document["protocol"] != PUBLICATION_PROTOCOL:
      _reject("a10_publication_protocol_invalid")
   clock = document["clock"]
   if not isinstance(clock, Mapping) or set(clock) != CLOCK_FIELDS:
      _reject("a10_publication_clock_invalid")
   _pseudonym_text(clock["domain_ref"], "a10_publication_clock_invalid")
   for field in ("monotonic_anchor", "wall_anchor"):
      _finite(clock[field], "a10_publication_clock_invalid")
   if _finite(clock["resolution"], "a10_publication_clock_invalid") <= 0:
      _reject("a10_publication_clock_invalid")
   _pseudonym_text(document["route_path_ref"], "a10_publication_route_invalid")
   _pseudonym_text(document["route_attempt_ref"], "a10_publication_route_invalid")
   stage_count = document["stage_count"]
   if isinstance(stage_count, bool) or not isinstance(stage_count, int) or stage_count < 1:
      _reject("a10_publication_route_invalid")
   intervals = document["intervals"]
   if not isinstance(intervals, list) or len(intervals) > MAXIMUM_INTERVALS:
      _reject("a10_publication_intervals_invalid")
   refs: set[str] = set()
   for interval in intervals:
      if not isinstance(interval, Mapping) or set(interval) != INTERVAL_FIELDS:
         _reject("a10_publication_interval_invalid")
      if interval["hop_kind"] not in _HOP_KINDS:
         _reject("a10_publication_interval_invalid")
      ref = interval["interval_ref"]
      if not isinstance(ref, str) or not ref or ref in refs:
         _reject("a10_publication_interval_invalid")
      refs.add(ref)
      _pseudonym_text(interval["request_ref"], "a10_publication_interval_invalid")
      _pseudonym_text(interval["microbatch_ref"], "a10_publication_interval_invalid")
      _pseudonym_text(interval["input_digest"], "a10_publication_interval_invalid", allow_empty=True)
      _pseudonym_text(interval["output_digest"], "a10_publication_interval_invalid", allow_empty=True)
      index = interval["stage_index"]
      if isinstance(index, bool) or not isinstance(index, int) or index >= stage_count:
         _reject("a10_publication_interval_invalid")
      if (interval["hop_kind"] == "compute") != (index >= 0) or index < -1:
         _reject("a10_publication_interval_invalid")
      started = _finite(interval["started_at"], "a10_publication_interval_invalid")
      finished = _finite(interval["finished_at"], "a10_publication_interval_invalid")
      if finished < started:
         _reject("a10_publication_interval_reversed")
      _finite(interval["clock_resolution"], "a10_publication_interval_invalid")
      if not isinstance(interval["phase"], str) or not isinstance(interval["runtime_batch_ref"], str):
         _reject("a10_publication_interval_invalid")
   for field in ("dropped_interval_count", "dropped_observation_count"):
      value = document[field]
      if isinstance(value, bool) or not isinstance(value, int) or value < 0:
         _reject("a10_publication_counts_invalid")
   observations = document["runtime_batch_observations"]
   if not isinstance(observations, list) or len(observations) > MAXIMUM_OBSERVATIONS:
      _reject("a10_publication_observations_invalid")
   for observation in observations:
      if (
         not isinstance(observation, Mapping)
         or observation.get("protocol") != RuntimeBatchObservation.PROTOCOL
      ):
         _reject("a10_publication_observations_invalid")
   if document["bounds"] != {
      "maximum_intervals": MAXIMUM_INTERVALS,
      "maximum_observations": MAXIMUM_OBSERVATIONS,
   }:
      _reject("a10_publication_bounds_invalid")
   return dict(document)


def build_overlap_trace(
   publications: Iterable[Mapping[str, Any]],
   *,
   cross_host_clock_uncertainty: float,
   trace_ref: str = "a10-live-trace",
) -> OverlapTrace:
   """Assemble one causal overlap trace from the route nodes' live publications.

   Every interval is placed on the wall-clock timeline of its own publication's
   anchor and widened by ``cross_host_clock_uncertainty`` (the measured bound on the
   hosts' wall-clock disagreement) plus its clock resolution.  Only microbatches
   whose every stage application is present in the published windows are kept, so
   a window edge can never look like a skipped stage.  Causal edges join each
   microbatch's consecutive stage applications.
   """

   uncertainty = _finite(cross_host_clock_uncertainty, "invalid_cross_host_uncertainty")
   if uncertainty < 0:
      _reject("invalid_cross_host_uncertainty")
   documents = [validate_publication(document) for document in publications]
   if not documents:
      _reject("a10_publication_absent")
   routes = {(doc["route_path_ref"], doc["route_attempt_ref"], doc["stage_count"]) for doc in documents}
   if len(routes) != 1:
      _reject("path_or_attempt_mismatch")
   path_ref, attempt_ref, stage_count = routes.pop()
   compute: dict[str, dict[int, TraceHop]] = {}
   commits: list[TraceHop] = []
   for document in documents:
      clock = document["clock"]
      offset = clock["wall_anchor"] - clock["monotonic_anchor"]
      for interval in document["intervals"]:
         hop = TraceHop(
            interval_ref=f"{clock['domain_ref'][7:23]}:{interval['interval_ref']}",
            microbatch_ref=interval["microbatch_ref"],
            request_ref=interval["request_ref"],
            path_ref=path_ref,
            attempt_ref=attempt_ref,
            hop_kind=interval["hop_kind"],
            stage_index=interval["stage_index"],
            started_at=interval["started_at"] + offset,
            finished_at=interval["finished_at"] + offset,
            clock_uncertainty=uncertainty + interval["clock_resolution"],
            input_digest=interval["input_digest"],
            output_digest=interval["output_digest"],
         )
         if hop.is_compute:
            per_stage = compute.setdefault(hop.microbatch_ref, {})
            if hop.stage_index in per_stage:
               _reject("duplicate_stage_application")
            per_stage[hop.stage_index] = hop
         else:
            commits.append(hop)
   complete = {
      microbatch: stages
      for microbatch, stages in compute.items()
      if sorted(stages) == list(range(stage_count))
   }
   if not complete or not commits:
      _reject("a10_trace_incomplete")
   hops: list[TraceHop] = []
   edges: list[tuple[str, str]] = []
   for stages in complete.values():
      ordered = [stages[index] for index in range(stage_count)]
      hops.extend(ordered)
      edges.extend(
         (previous.interval_ref, current.interval_ref)
         for previous, current in zip(ordered, ordered[1:])
      )
   commit = max(commits, key=lambda hop: hop.finished_at)
   hops.append(commit)
   return OverlapTrace(
      trace_ref=trace_ref,
      path_ref=path_ref,
      attempt_ref=attempt_ref,
      stage_count=stage_count,
      commit_interval_ref=commit.interval_ref,
      hops=tuple(hops),
      causal_edges=tuple(edges),
   )


__all__ = [
   "A10PublicationError",
   "INTERVAL_PROTOCOL",
   "MAXIMUM_INTERVALS",
   "MAXIMUM_OBSERVATIONS",
   "PUBLICATION_PROTOCOL",
   "StageComputePublication",
   "activation_digest",
   "build_overlap_trace",
   "pseudonym",
   "route_identity",
   "validate_publication",
]
