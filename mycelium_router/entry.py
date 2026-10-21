# SPDX-License-Identifier: AGPL-3.0-only
"""Request admission, checkpointing, decode, failure recovery, and the A7
KV-successor classification of every replacement-path attempt."""

import importlib
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from threading import RLock
from typing import Any

from mycelium_deadlines import PHONE_THERMAL_COOLDOWN_SECONDS

from mycelium_router.contracts import (
   ExecutionGraph,
   FailureReport,
   HopHeader,
   ManifestDelta,
   ManifestLocked,
   PathBuildState,
   PathCancellation,
   PathHop,
   PathManifest,
   PrefillChunkCompleted,
   ProgressivePrefillContext,
   RequestContext,
   ReservationRequest,
   TokenEvent,
)
from mycelium_router.idempotency import hop_idempotency_key, payload_fingerprint
from mycelium_router.payloads import encode_token_ids
from mycelium_router.routing import RoutingError
from mycelium_router.state import RequestStateMachine
from mycelium_router.validation import validate_manifest
from mycelium_router.leases import validate_hop_leases

#: Requests whose last FailureReport is kept for diagnostics.
_LAST_FAILURE_REPORTS = 256


# ---------------------------------------------------------------------------
# A7 KV-successor seam: the replacement-path attempt's recovery classification
# ---------------------------------------------------------------------------
#
# A failed placement's replacement path attempt is the Router's
# checkpoint-coordinator surface (A7 specification section 2: route-wide
# watermark assembly, acknowledgement, fencing generation, replacement path
# attempt, cutover and exact source/standby cleanup).  Before this coordinator
# commits to that attempt, its recovery mode is classified by the A7
# deterministic contract (`mycelium_a7_kv_successor`), never assumed: the
# module's closed cutover-eligibility predicate runs on the exact facts this
# coordinator holds and the result is written to the request record.
#
# The classification is load-bearing:
#
# * ``full_context_replay`` -- the one outcome this router can execute, the
#   qualified A6 replay the replacement attempt performs;
# * ``fenced_kv_successor`` or ``aborted`` -- the attempt fails closed, because
#   this path cannot execute a fenced KV cutover and must never replay under a
#   KV-successor classification;
# * an unimportable A7 module -- the qualified replay proceeds and the record
#   carries ``a7_kv_successor_unavailable``; the module is never silently
#   skipped, and the reachability guard
#   (tests/a7_acceptance/test_a7_live_path_reachability.py) fails when the
#   dispatch is absent.
#
# No acknowledged KV watermark, checkpoint transport, durable fence store or
# standby exists anywhere on this path, so the predicate's honest live
# classification is ``full_context_replay`` (``route_watermark_unavailable``).

KV_SUCCESSOR_SEAM_PROTOCOL = "mycelium.router_kv_successor_seam.v1"
KV_SUCCESSOR_SEAM = "mycelium_router.entry.classify_kv_successor_recovery"
KV_SUCCESSOR_MODULE = "mycelium_a7_kv_successor"
KV_SUCCESSOR_CLASSIFIER = "evaluate_cutover_eligibility"
KV_SUCCESSOR_EXECUTABLE_OUTCOME = "full_context_replay"
KV_SUCCESSOR_SEAM_REASON_UNAVAILABLE = "a7_kv_successor_unavailable"
KV_SUCCESSOR_SEAM_CLAIM_BOUNDARY = (
   "router replacement-attempt classification through the A7 deterministic "
   "contract only: no acknowledged KV watermark, checkpoint transport, durable "
   "fence persistence or cutover execution exists in this path, and no "
   "physical, qualified or promotion claim is made."
)
# A distributed decode driver may call ``decode_one_distributed`` again before
# the token it already dispatched has come back.  Re-sending that idempotent hop
# changes nothing, so the call instead waits this long, in short sleeps, for the
# token (or any state change) and reports progress; a driver then never spins.
_DECODE_REDRIVE_WAIT_SECONDS = 0.05
_DECODE_REDRIVE_POLL_SECONDS = 0.001

_KV_SUCCESSOR_DECODE_MODES = frozenset(
   {"stage_local_kv", "complete_context_replay"}
)


def _load_a7_kv_successor():
   """Import the A7 contract module lazily; its absence is a classified state."""

   try:
      return importlib.import_module(KV_SUCCESSOR_MODULE)
   except ImportError:
      return None


def classify_kv_successor_recovery(
   *,
   decode_mode,
   path_attempt,
   prompt_token_ids,
   committed_token_ids,
):
   """Classify one replacement-path attempt through the A7 KV-successor contract.

   The coordinator calls this with whatever it holds when a non-terminal
   failure forces a replacement path attempt.  Nothing is invented: the route
   watermark is unavailable (no acknowledgement chain exists), no successor
   identity is bound (A7 contract D8: unknown identity data is incompatible,
   never assumed), nothing is qualified or fenced, and the replacement IS the
   qualified full-context replay path, so the closed predicate classifies the
   attempt as ``full_context_replay``.

   Returns a closed record; ``classification`` carries the A7 predicate's own
   closed outcome record when the module ran, and ``None`` when it could not be
   imported (``reason=a7_kv_successor_unavailable``) -- never a fabricated
   classification.  Inputs are validated fail-closed: an unknown decode mode,
   a non-positive path attempt or non-integer token ids raise ``ValueError``.
   """

   if decode_mode not in _KV_SUCCESSOR_DECODE_MODES:
      raise ValueError("invalid_runtime_decode_mode")
   if type(path_attempt) is not int or path_attempt < 1:
      raise ValueError("invalid_path_attempt")
   prompt = tuple(prompt_token_ids)
   committed = tuple(committed_token_ids)
   if any(type(token) is not int for token in prompt) or any(
      type(token) is not int for token in committed
   ):
      raise ValueError("invalid_token_ids")
   committed_token_count = len(committed)
   record = {
      "protocol": KV_SUCCESSOR_SEAM_PROTOCOL,
      "seam": KV_SUCCESSOR_SEAM,
      "dispatched": False,
      "authoritative_module": KV_SUCCESSOR_MODULE,
      "authoritative_function": KV_SUCCESSOR_CLASSIFIER,
      "decode_mode": decode_mode,
      "path_attempt": path_attempt,
      "committed_token_count": committed_token_count,
      "recovery_mode": KV_SUCCESSOR_EXECUTABLE_OUTCOME,
      "kv_successor_eligible": False,
      "reason": KV_SUCCESSOR_SEAM_REASON_UNAVAILABLE,
      "classification": None,
      "claim_boundary": KV_SUCCESSOR_SEAM_CLAIM_BOUNDARY,
   }
   module = _load_a7_kv_successor()
   if module is None:
      return record
   classification = module.evaluate_cutover_eligibility(
      # No replacement stage has acknowledged a route-wide KV watermark, so
      # the predicate's first honest rejection is `route_watermark_unavailable`;
      # the gateway count/digest are this coordinator's own committed-prefix
      # facts and are never compared in that state.
      route_watermark=None,
      gateway_committed_token_count=committed_token_count,
      gateway_committed_prefix_digest="sha256:"
      + payload_fingerprint(prompt + committed),
      # No successor identity is bound on this path; A7's rule is that unknown
      # identity data is incompatible, never assumed.
      compatibility=module.compare_compatibility({}, {}),
      qualifier_current=False,
      fence_installed=False,
      # The replacement attempt this classification covers IS the qualified
      # full-context replay path, so A6 fallback is available here.
      a6_available=True,
   )
   record.update(
      dispatched=True,
      recovery_mode=classification["outcome"],
      kv_successor_eligible=classification["kv_successor_eligible"],
      reason=classification["reason"],
      classification=classification,
   )
   return record


@dataclass
class RequestRecord:
   request: RequestContext
   graph: ExecutionGraph
   manifest: PathManifest
   client_sink: object
   state_machine: RequestStateMachine
   path_generation: int = 0
   generated_token_ids: list[int] = field(default_factory=list)
   prefill_chunks: tuple[tuple[int, ...], ...] = ()
   completed_prefill_chunks: int = 0
   excluded_placements: frozenset[str] = frozenset()
   excluded_edges: frozenset[str] = frozenset()
   excluded_devices: frozenset[str] = frozenset()
   cleaned_up: bool = False
   kv_successor_recovery: dict | None = None
   # A7: the entry controller's fenced-cutover record (privacy-reduced) for the
   # last covered failure: ``fenced_kv_successor`` or the honest fallback.
   kv_successor_cutover: dict | None = None
   # (path_id, path_attempt, path_generation, token_index) of the last decode
   # hop this entry dispatched for the request.
   decode_dispatched: tuple[str, int, int, int] | None = None
   # A11: the outstanding VERIFY round -- (dispatch key, private candidate ids).
   verify_round: tuple[tuple[str, int, int, int], tuple[int, ...]] | None = None
   lock: Any = field(default_factory=RLock, repr=False, compare=False)

   @property
   def status(self) -> str:
      return self.state_machine.state


@dataclass
class PendingPrefill:
   request: RequestContext
   graph: ExecutionGraph
   build: PathBuildState
   client_sink: object
   state_machine: RequestStateMachine
   prefill_chunks: tuple[tuple[int, ...], ...]
   lock: Any = field(default_factory=RLock, repr=False, compare=False)


class EntryCoordinator:
   #: A7 entry controller (``mycelium_a7_entry_controller.A7EntryController``);
   #: ``None`` keeps every request on the qualified A6 path unchanged.
   a7_controller = None
   #: A11 speculation controller
   #: (``mycelium_a11_route_speculation.RouteSpeculationController``); ``None``
   #: keeps every request target-only.
   a11_controller = None

   def __init__(
      self,
      *,
      node_id,
      topology,
      device_states,
      capacity,
      runtime,
      transport,
      relay,
      builder,
      clock,
      config,
   ):
      self.node_id = node_id
      self.topology = topology
      self.device_states = device_states
      self.capacity = capacity
      self.runtime = runtime
      self.transport = transport
      self.relay = relay
      self.builder = builder
      self.clock = clock
      self.config = config
      self._requests: dict[str, RequestRecord] = {}
      self._pending_prefills: dict[str, PendingPrefill] = {}
      self._phone_thermal_until: dict[str, float] = {}
      self._phone_thermal_lock = RLock()
      # Diagnostics only (A14 serve-2): the last FailureReport each request
      # received, so a failed start can name its cause.  Bounded, oldest out.
      self._last_failure_reports: OrderedDict[str, dict[str, Any]] = OrderedDict()
      self._last_failure_reports_lock = RLock()

   def last_failure_report(self, request_id: str) -> dict[str, Any] | None:
      """The last FailureReport received for ``request_id`` (diagnostics).

      ``reason``/``scope`` are the reporter's code, ``placement_id`` and
      ``stage_id`` the hop that failed, ``token_index`` its chunk (``-1`` = the
      progressive first chunk) or decode step, ``source_node_id`` the
      reporter, ``applied`` whether it matched the request's current path.
      """

      if "_last_failure_reports" not in self.__dict__:
         return None
      with self._last_failure_reports_lock:
         found = self._last_failure_reports.get(request_id)
         return None if found is None else dict(found)

   def _remember_failure_report(
      self, report: FailureReport, source_node_id: str | None
   ) -> dict[str, Any] | None:
      requests = self.__dict__.get("_requests", {})
      pending_prefills = self.__dict__.get("_pending_prefills", {})
      record = requests.get(report.request_id)
      if record is None and report.request_id not in pending_prefills:
         return None
      stage_id = None
      graph = record.graph if record is not None else None
      if graph is None:
         pending = pending_prefills.get(report.request_id)
         graph = None if pending is None else pending.graph
      for stage in getattr(graph, "stages", ()) or ():
         if any(
            placement.placement_id == report.placement_id
            for placement in stage.placements
         ):
            stage_id = stage.stage_id
            break
      entry = {
         "reason": report.reason,
         "scope": report.scope,
         "placement_id": report.placement_id,
         "stage_id": stage_id,
         "token_index": report.token_index,
         "path_attempt": report.path_attempt,
         "source_node_id": source_node_id or report.node_id or None,
         "applied": False,
      }
      if "_last_failure_reports" not in self.__dict__:
         # A coordinator assembled without ``__init__`` (test doubles).
         self._last_failure_reports = OrderedDict()
         self._last_failure_reports_lock = RLock()
      with self._last_failure_reports_lock:
         self._last_failure_reports[report.request_id] = entry
         self._last_failure_reports.move_to_end(report.request_id)
         while len(self._last_failure_reports) > _LAST_FAILURE_REPORTS:
            self._last_failure_reports.popitem(last=False)
      return entry

   def _phone_thermal_exclusions(self) -> frozenset[str]:
      with self._phone_thermal_lock:
         now = self.clock.now()
         self._phone_thermal_until = {node: until for node, until in self._phone_thermal_until.items() if now < until}
         return frozenset(self._phone_thermal_until)

   def _record_phone_thermal_failure(self, record, report, source_node_id) -> None:
      # Called ONLY after current request/path/token and authenticated origin checks.
      if report.reason != "thermal_ceiling_reached" or source_node_id is None:
         return
      selected = {h.placement_id for h in record.manifest.ordered_hops}
      if not any(p.node_id == source_node_id and p.placement_id in selected
                 and p.runtime_backend == "mobile-rust-qwen2" for s in record.graph.stages for p in s.placements):
         return
      with self._phone_thermal_lock:
         self._phone_thermal_until[source_node_id] = max(
            self._phone_thermal_until.get(source_node_id, 0),
            self.clock.now() + PHONE_THERMAL_COOLDOWN_SECONDS)


   def admit(
      self,
      request: RequestContext,
      client_sink,
      *,
      pinned_deployment: ExecutionGraph | None = None,
      excluded_placements: frozenset[str] = frozenset(),
      excluded_edges: frozenset[str] = frozenset(),
      excluded_devices: frozenset[str] = frozenset(),
   ) -> str:
      if (
         pinned_deployment is not None
         and not isinstance(pinned_deployment, ExecutionGraph)
      ):
         raise TypeError("invalid_pinned_deployment")
      if (
         request.request_id in self._requests
         or request.request_id in self._pending_prefills
      ):
         raise ValueError("duplicate_request_id")
      graph = (
         pinned_deployment
         if pinned_deployment is not None
         else self.topology.snapshot()
      )
      if not isinstance(graph, ExecutionGraph):
         raise TypeError("invalid_deployment_snapshot")
      graph, manifest, build = self._build_path(
         request,
         graph=graph,
         path_attempt=0,
         excluded_placements=excluded_placements,
         excluded_edges=excluded_edges,
         excluded_devices=excluded_devices,
      )
      state_machine = RequestStateMachine(path_attempt=manifest.path_attempt)
      state_machine.transition("PREFILL", path_attempt=manifest.path_attempt)
      record = RequestRecord(
         request=request,
         graph=graph,
         manifest=manifest,
         client_sink=client_sink,
         state_machine=state_machine,
         excluded_placements=build.excluded_placements,
         excluded_edges=build.excluded_edges,
         excluded_devices=build.excluded_devices,
      )
      self._requests[request.request_id] = record
      generation = self.relay.register_path_with_generation(
         request,
         manifest,
         graph,
         entry_node_id=self.node_id,
      )
      if generation is None:
         self._cleanup_record(record)
         raise RoutingError("path_registration_failed")
      record.path_generation = generation
      outcome = self.relay.execute_manifest(
         graph=graph,
         manifest=manifest,
         request=request,
         phase="PREFILL",
         token_index=-1,
         payload=request.prompt_token_ids,
         expected_generation=generation,
      )
      if outcome.failure_report is not None:
         if not self.receive_failure_report(outcome.failure_report):
            if record.status == "CANCELLED":
               return request.request_id
            raise RoutingError("prefill_failed", outcome.failure_report.reason)
      else:
         with record.lock:
            if record.status != "PREFILL":
               return request.request_id
            state_machine.transition("LOCKED", path_attempt=manifest.path_attempt)
            state_machine.transition("DECODING", path_attempt=manifest.path_attempt)
         if outcome.token_event is not None:
            prefill_accepted = self.receive_token_event(outcome.token_event)
         else:
            prefill_accepted = self.relay.decode_mode == "complete_context_replay"
         if not prefill_accepted:
            with record.lock:
               if record.status == "CANCELLED":
                  return request.request_id
               record.state_machine.transition(
                  "FAILED",
                  path_attempt=manifest.path_attempt,
               )
            self._cleanup_record(record)
            raise RoutingError("prefill_missing_token")
      return request.request_id

   def start_distributed_prefill(
      self,
      request: RequestContext,
      client_sink,
      *,
      excluded_placements: frozenset[str] = frozenset(),
      excluded_edges: frozenset[str] = frozenset(),
      excluded_devices: frozenset[str] = frozenset(),
   ) -> str:
      if (
         request.request_id in self._requests
         or request.request_id in self._pending_prefills
      ):
         raise ValueError("duplicate_request_id")
      self.transport.remember_entry(request.request_id, self.node_id)
      graph = self.topology.snapshot()
      excluded_devices |= self._phone_thermal_exclusions()
      build = self.builder.start(
         request,
         graph,
         path_attempt=0,
         excluded_placements=excluded_placements,
         excluded_edges=excluded_edges,
         excluded_devices=excluded_devices,
      )
      build = self.builder.advance(
         build,
         self.device_states.snapshot(),
         now=self.clock.now(),
      )
      prefill_chunks = self._prefill_chunks(request.prompt_token_ids)
      state_machine = RequestStateMachine(path_attempt=0)
      state_machine.transition("PREFILL", path_attempt=0)
      self._pending_prefills[request.request_id] = PendingPrefill(
         request=request,
         graph=graph,
         build=build,
         client_sink=client_sink,
         state_machine=state_machine,
         prefill_chunks=prefill_chunks,
      )
      first = build.ordered_hops[0]
      self.transport.send_manifest_delta(
         ManifestDelta(
            request_id=request.request_id,
            path_id=build.path_id,
            path_attempt=build.path_attempt,
            hop_index=0,
            hop=first,
         )
      )
      header = HopHeader(
         request_id=request.request_id,
         path_id=build.path_id,
         path_attempt=build.path_attempt,
         phase="PREFILL",
         token_index=-1,
         hop_index=0,
         source_placement_id="",
         destination_placement_id=first.placement_id,
         topology_version=graph.topology_version,
         idempotency_key=hop_idempotency_key(
            request_id=request.request_id,
            path_id=build.path_id,
            path_attempt=build.path_attempt,
            phase="PREFILL",
            token_index=-1,
            hop_index=0,
         ),
         prefill_chunk_token_count=len(prefill_chunks[0]),
      )
      context = ProgressivePrefillContext(
         graph=graph,
         request=request,
         build=build,
         payload=encode_token_ids(prefill_chunks[0]),
      )
      self.transport.send_hop(header, context)
      return request.request_id

   def start_locked_distributed_prefill(
      self,
      request: RequestContext,
      client_sink,
      *,
      manifest: PathManifest,
      pinned_deployment: ExecutionGraph,
   ) -> str:
      """Execute the exact Router-owned locked path without rebuilding it."""

      if (
         request.request_id in self._requests
         or request.request_id in self._pending_prefills
      ):
         raise ValueError("duplicate_request_id")
      graph = self.topology.snapshot()
      if graph != pinned_deployment:
         raise RoutingError("stale_pinned_deployment")
      validate_manifest(manifest, graph)
      if manifest.request_id != request.request_id:
         raise RoutingError("path_request_mismatch")
      validate_hop_leases(
         manifest.ordered_hops,
         deployment_epoch=graph.deployment_epoch,
         now=self.clock.now(),
      )
      placement_by_id = {
         placement.placement_id: placement
         for stage in graph.stages
         for placement in stage.placements
      }
      if placement_by_id[manifest.ordered_hops[0].placement_id].node_id != self.node_id:
         raise RoutingError("locked_path_entry_mismatch")
      self.transport.remember_entry(request.request_id, self.node_id)
      build = PathBuildState(
         request=request,
         graph=graph,
         path_id=manifest.path_id,
         path_attempt=manifest.path_attempt,
         ordered_hops=manifest.ordered_hops,
      )
      state_machine = RequestStateMachine(path_attempt=manifest.path_attempt)
      state_machine.transition("PREFILL", path_attempt=manifest.path_attempt)
      state_machine.transition("LOCKED", path_attempt=manifest.path_attempt)
      chunks = self._prefill_chunks(request.prompt_token_ids)
      record = RequestRecord(
         request=request,
         graph=graph,
         manifest=manifest,
         client_sink=client_sink,
         state_machine=state_machine,
         prefill_chunks=chunks,
      )
      self._requests[request.request_id] = record
      generation = self.relay.register_path_with_generation(
         request,
         manifest,
         graph,
         entry_node_id=self.node_id,
      )
      if generation is None:
         self._cleanup_record(record)
         raise RoutingError("path_registration_failed")
      record.path_generation = generation
      try:
         self.transport.send_manifest_locked(
            ManifestLocked(
               request_id=request.request_id,
               path_id=manifest.path_id,
               path_attempt=manifest.path_attempt,
               manifest=manifest,
               build=build,
            )
         )
         first = manifest.ordered_hops[0]
         header = HopHeader(
            request_id=request.request_id,
            path_id=manifest.path_id,
            path_attempt=manifest.path_attempt,
            phase="PREFILL_CHUNK",
            token_index=0,
            hop_index=0,
            source_placement_id="",
            destination_placement_id=first.placement_id,
            topology_version=manifest.topology_version,
            idempotency_key=hop_idempotency_key(
               request_id=request.request_id,
               path_id=manifest.path_id,
               path_attempt=manifest.path_attempt,
               phase="PREFILL_CHUNK",
               token_index=0,
               hop_index=0,
            ),
            prefill_chunk_token_count=len(chunks[0]),
         )
         if not self.relay.dispatch_if_current(
            path_id=manifest.path_id,
            path_attempt=manifest.path_attempt,
            generation=generation,
            sender=lambda: self.transport.send_hop(
               header,
               encode_token_ids(chunks[0]),
            ),
         ):
            raise RoutingError("path_cancelled")
      except BaseException:
         self._cleanup_record(record)
         raise
      return request.request_id

   def receive_manifest_locked(
      self,
      locked: ManifestLocked,
      *,
      source_node_id: str | None = None,
   ) -> bool:
      existing = self._requests.get(locked.request_id)
      if existing is not None:
         with existing.lock:
            return (
               existing.manifest == locked.manifest
               and existing.graph == locked.build.graph
               and existing.request == locked.build.request
               and existing.status in {"LOCKED", "DECODING"}
            )
      pending = self._pending_prefills.get(locked.request_id)
      if pending is None or pending.state_machine.state != "PREFILL":
         return False
      if source_node_id is not None and not self._final_hop_origin_matches(
         pending.graph,
         locked.manifest,
         source_node_id,
      ):
         return False
      if (
         locked.path_id != pending.build.path_id
         or locked.path_attempt != pending.build.path_attempt
         or locked.manifest.request_id != pending.request.request_id
         or not self.builder.is_complete(locked.build)
         or locked.build.ordered_hops[:1] != pending.build.ordered_hops[:1]
      ):
         return False
      validate_manifest(locked.manifest, pending.graph)
      if locked.manifest.ordered_hops != locked.build.ordered_hops:
         return False
      with pending.lock:
         if pending.state_machine.state != "PREFILL":
            return False
         pending.state_machine.transition(
            "LOCKED",
            path_attempt=locked.path_attempt,
         )
         record = RequestRecord(
            request=pending.request,
            graph=pending.graph,
            manifest=locked.manifest,
            client_sink=pending.client_sink,
            state_machine=pending.state_machine,
            prefill_chunks=pending.prefill_chunks,
            completed_prefill_chunks=1,
            excluded_placements=locked.build.excluded_placements,
            excluded_edges=locked.build.excluded_edges,
            excluded_devices=locked.build.excluded_devices,
         )
         self._requests[locked.request_id] = record
         self._pending_prefills.pop(locked.request_id, None)
         generation = self.relay.register_path_with_generation(
            record.request,
            record.manifest,
            record.graph,
            entry_node_id=self.node_id,
         )
         if generation is None:
            record.state_machine.transition(
               "FAILED",
               path_attempt=locked.path_attempt,
            )
            self._cleanup_record(record)
            return False
         record.path_generation = generation
         if len(record.prefill_chunks) == 1:
            record.state_machine.transition(
               "DECODING",
               path_attempt=locked.path_attempt,
            )
            send_chunk = False
         else:
            send_chunk = True
      if send_chunk:
         return self._send_prefill_chunk(record, chunk_index=1)
      return True

   def receive_prefill_chunk_completed(
      self,
      event: PrefillChunkCompleted,
      *,
      source_node_id: str | None = None,
   ) -> bool:
      record = self._requests.get(event.request_id)
      if record is None:
         return False
      with record.lock:
         if record.status != "LOCKED":
            return False
         if source_node_id is not None and not self._final_hop_origin_matches_locked_path(
            record,
            source_node_id,
         ):
            return False
         manifest = record.manifest
         expected_index = record.completed_prefill_chunks
         if (
            event.path_id != manifest.path_id
            or event.path_attempt != manifest.path_attempt
            or event.chunk_index != expected_index
            or event.chunk_index >= len(record.prefill_chunks)
            or event.token_count != len(record.prefill_chunks[event.chunk_index])
         ):
            return False
         record.completed_prefill_chunks += 1
         if record.completed_prefill_chunks == len(record.prefill_chunks):
            record.state_machine.transition(
               "DECODING",
               path_attempt=manifest.path_attempt,
            )
            next_chunk_index = None
         else:
            next_chunk_index = record.completed_prefill_chunks
      if next_chunk_index is not None:
         return self._send_prefill_chunk(
            record,
            chunk_index=next_chunk_index,
         )
      return True

   def _send_prefill_chunk(
      self,
      record: RequestRecord,
      *,
      chunk_index: int,
   ) -> bool:
      with record.lock:
         if record.status != "LOCKED":
            return False
         manifest = record.manifest
         generation = record.path_generation
      first = manifest.ordered_hops[0]
      chunk = record.prefill_chunks[chunk_index]
      header = HopHeader(
         request_id=record.request.request_id,
         path_id=manifest.path_id,
         path_attempt=manifest.path_attempt,
         phase="PREFILL_CHUNK",
         token_index=chunk_index,
         hop_index=0,
         source_placement_id="",
         destination_placement_id=first.placement_id,
         topology_version=manifest.topology_version,
         idempotency_key=hop_idempotency_key(
            request_id=record.request.request_id,
            path_id=manifest.path_id,
            path_attempt=manifest.path_attempt,
            phase="PREFILL_CHUNK",
            token_index=chunk_index,
            hop_index=0,
         ),
         prefill_chunk_token_count=len(chunk),
      )
      return self.relay.dispatch_if_current(
         path_id=manifest.path_id,
         path_attempt=manifest.path_attempt,
         generation=generation,
         sender=lambda: self.transport.send_hop(header, encode_token_ids(chunk)),
      )

   def _prefill_chunks(
      self,
      prompt_token_ids: tuple[int, ...],
   ) -> tuple[tuple[int, ...], ...]:
      size = self.config.prefill_chunk_size_tokens
      if size <= 0 or size >= len(prompt_token_ids):
         return (prompt_token_ids,)
      return tuple(
         prompt_token_ids[start : start + size]
         for start in range(0, len(prompt_token_ids), size)
      )

   def request_status(self, request_id: str) -> str:
      record = self._requests.get(request_id)
      if record is not None:
         return record.status
      pending = self._pending_prefills.get(request_id)
      if pending is not None:
         return pending.state_machine.state
      raise KeyError(request_id)

   def get_request(self, request_id: str) -> RequestRecord:
      return self._requests[request_id]

   def cancel(self, request_id: str) -> bool:
      return self._cancel(request_id, propagate_path_cancellation=True)

   def cancel_local(self, request_id: str) -> bool:
      """Cancel entry-owned state when an authenticated owner fans out control."""

      return self._cancel(request_id, propagate_path_cancellation=False)

   def _cancel(
      self,
      request_id: str,
      *,
      propagate_path_cancellation: bool,
   ) -> bool:
      pending = self._pending_prefills.get(request_id)
      if pending is not None:
         with pending.lock:
            if pending.state_machine.state == "PREFILL":
               pending.state_machine.transition(
                  "CANCELLED",
                  path_attempt=pending.build.path_attempt,
               )
               cancellation = PathCancellation(
                  request_id=request_id,
                  path_id=pending.build.path_id,
                  path_attempt=pending.build.path_attempt,
                  topology_version=pending.graph.topology_version,
               )
            else:
               cancellation = None
         if cancellation is not None:
            try:
               if propagate_path_cancellation:
                  self.transport.send_path_cancellation(cancellation)
            finally:
               self.builder.abort(pending.build)
               self.relay.release_path(
                  pending.build.path_id,
                  path_attempt=pending.build.path_attempt,
               )
            return True
      record = self._requests.get(request_id)
      if record is None:
         return False
      with record.lock:
         if record.status in {
            "COMPLETED",
            "FAILED",
            "CANCELLED",
         }:
            return False
         record.state_machine.transition(
            "CANCELLED",
            # Recovery advances the state machine before building and
            # committing the replacement manifest.  Owner cancellation can
            # linearize in that build window, so transition against the live
            # lifecycle attempt while retaining the committed manifest below
            # as the exact resource identity to retire.
            path_attempt=record.state_machine.path_attempt,
         )
         cancellation = PathCancellation(
            request_id=request_id,
            path_id=record.manifest.path_id,
            path_attempt=record.manifest.path_attempt,
            topology_version=record.manifest.topology_version,
         )
      try:
         if propagate_path_cancellation:
            self.transport.send_path_cancellation(cancellation)
      finally:
         self._cleanup_record(record)
      return True

   def generate(self, request_id: str, *, token_count: int) -> tuple[int, ...]:
      record = self.get_request(request_id)
      starting = len(record.generated_token_ids)
      target = min(starting + token_count, record.request.max_new_tokens)
      while len(record.generated_token_ids) < target:
         if not self.decode_one(request_id):
            break
      return tuple(record.generated_token_ids[starting:])

   def decode_one_distributed(self, request_id: str) -> bool:
      if request_id in self._pending_prefills:
         return False
      record = self.get_request(request_id)
      self._a7_await_acknowledged(record)
      with record.lock:
         if record.status != "DECODING":
            # A recovery (fenced A7 KV-successor cutover or A6 replay) that a
            # liveness report began between two driver decode commands owns
            # the pending step: the successor re-sends it, or the replay
            # prefill produces it.  Report it in flight so the driver waits
            # for that token or a terminal status instead of reading a
            # non-terminal recovery state as a stalled decode.
            # Only a request that already decoded can be in recovery here;
            # an initial prefill has committed no token yet.
            return record.status in {"PREFILL", "LOCKED"} and bool(
               record.generated_token_ids
            )
         token_index = len(record.generated_token_ids)
         manifest = record.manifest
         first = manifest.ordered_hops[0]
         final = manifest.ordered_hops[-1]
         if self.relay.decode_mode == "stage_local_kv":
            if not record.generated_token_ids:
               return False
            decode_tokens = (record.generated_token_ids[-1],)
         else:
            decode_tokens = (
               record.request.prompt_token_ids + tuple(record.generated_token_ids)
            )
         generation = record.path_generation
         dispatch_key = (
            manifest.path_id,
            manifest.path_attempt,
            generation,
            token_index,
         )
         redrive = record.decode_dispatched == dispatch_key
      if redrive:
         self._await_decode_progress(record, dispatch_key)
         return True
      header = HopHeader(
         request_id=record.request.request_id,
         path_id=manifest.path_id,
         path_attempt=manifest.path_attempt,
         phase="DECODE",
         token_index=token_index,
         hop_index=0,
         source_placement_id=final.placement_id,
         destination_placement_id=first.placement_id,
         topology_version=manifest.topology_version,
         idempotency_key=hop_idempotency_key(
            request_id=record.request.request_id,
            path_id=manifest.path_id,
            path_attempt=manifest.path_attempt,
            phase="DECODE",
            token_index=token_index,
            hop_index=0,
         ),
      )
      dispatched = self.relay.dispatch_if_current(
         path_id=manifest.path_id,
         path_attempt=manifest.path_attempt,
         generation=generation,
         sender=lambda: self.transport.send_hop(
            header,
            encode_token_ids(decode_tokens),
         ),
      )
      if dispatched:
         with record.lock:
            if len(record.generated_token_ids) == token_index:
               record.decode_dispatched = dispatch_key
      return dispatched

   def speculate_one_distributed(self, request_id: str) -> bool:
      """One A11 round: a target-verified span when admitted, else an ordinary DECODE.

      The draft proposes from the target-committed context; the proposal travels in
      one VERIFY hop through the locked path; the final stage's decisions return in
      a ``VerifyEvent`` and :meth:`receive_verify_event` commits the target-
      authoritative tokens.  Every refusal, deadline or draft loss is an ordinary
      DECODE from the same committed watermark.
      """

      controller = self.a11_controller
      if controller is None or request_id in self._pending_prefills:
         return self.decode_one_distributed(request_id)
      record = self.get_request(request_id)
      self._a7_await_acknowledged(record)
      with record.lock:
         if (
            record.status != "DECODING"
            or self.relay.decode_mode != "stage_local_kv"
            or not record.generated_token_ids
         ):
            ready = False
         else:
            ready = True
            token_index = len(record.generated_token_ids)
            remaining = record.request.max_new_tokens - token_index
            context = record.request.prompt_token_ids + tuple(record.generated_token_ids)
            manifest = record.manifest
            generation = record.path_generation
            dispatch_key = (manifest.path_id, manifest.path_attempt, generation, token_index)
            in_flight = record.decode_dispatched == dispatch_key
      if not ready or in_flight:
         return self.decode_one_distributed(request_id)
      admission = controller.admit(
         request_id,
         backends=self._path_backends(record),
         kv_successor_covered=self.a7_controller is not None,
         remaining_deadline_ms=self._remaining_request_budget_ms(record),
      )
      proposal = None
      if admission["mode"] == "speculative_admitted" and remaining > 1:
         # The last token of a request is always an ordinary terminal DECODE, so a
         # VERIFY never has to release KV and never overshoots ``max_new_tokens``.
         proposal = controller.propose(request_id, context, min(remaining - 1, controller.policy.width))
      if proposal is None:
         controller.note_decode_round(request_id)
         return self.decode_one_distributed(request_id)
      first = manifest.ordered_hops[0]
      final = manifest.ordered_hops[-1]
      with record.lock:
         current = (
            record.manifest.path_id,
            record.manifest.path_attempt,
            record.path_generation,
            len(record.generated_token_ids),
         )
         if record.status != "DECODING" or current != dispatch_key or record.decode_dispatched == dispatch_key:
            stale = True
         else:
            stale = False
            record.decode_dispatched = dispatch_key
            record.verify_round = (dispatch_key, proposal)
      if stale:
         controller.abandon(request_id, len(proposal))
         return self.decode_one_distributed(request_id)
      header = HopHeader(
         request_id=record.request.request_id,
         path_id=manifest.path_id,
         path_attempt=manifest.path_attempt,
         phase="VERIFY",
         token_index=token_index,
         hop_index=0,
         source_placement_id=final.placement_id,
         destination_placement_id=first.placement_id,
         topology_version=manifest.topology_version,
         idempotency_key=hop_idempotency_key(
            request_id=record.request.request_id,
            path_id=manifest.path_id,
            path_attempt=manifest.path_attempt,
            phase="VERIFY",
            token_index=token_index,
            hop_index=0,
         ),
         # A VERIFY carries its span (pending token + candidates) in this field.
         prefill_chunk_token_count=1 + len(proposal),
      )
      dispatched = self.relay.dispatch_if_current(
         path_id=manifest.path_id,
         path_attempt=manifest.path_attempt,
         generation=generation,
         sender=lambda: self.transport.send_hop(
            header,
            encode_token_ids((context[-1], *proposal)),
         ),
      )
      if not dispatched:
         with record.lock:
            if record.verify_round is not None and record.verify_round[0] == dispatch_key:
               record.verify_round = None
            if record.decode_dispatched == dispatch_key:
               record.decode_dispatched = None
         controller.abandon(request_id, len(proposal))
      return dispatched

   def receive_verify_event(
      self,
      event: Any,
      *,
      source_node_id: str | None = None,
   ) -> bool:
      """Commit the target-authoritative result of the outstanding VERIFY, exactly once.

      A late, stale, duplicate or foreign event is refused without effect: it must
      match the outstanding round's path, attempt, generation and committed count.
      """

      record = self._requests.get(event.request_id)
      controller = self.a11_controller
      if record is None or controller is None:
         return False
      with record.lock:
         round_ = record.verify_round
         manifest = record.manifest
         token_index = len(record.generated_token_ids)
         if (
            record.status != "DECODING"
            or round_ is None
            or event.path_id != manifest.path_id
            or event.path_attempt != manifest.path_attempt
            or event.token_index != token_index
            or round_[0] != (manifest.path_id, manifest.path_attempt, record.path_generation, token_index)
            or len(event.decisions) != len(round_[1]) + 1
         ):
            return False
         if source_node_id is not None and not self._final_hop_origin_matches_locked_path(
            record,
            source_node_id,
         ):
            return False
         record.verify_round = None
         context = record.request.prompt_token_ids + tuple(record.generated_token_ids)
         result = controller.complete_round(
            record.request.request_id,
            context=context,
            candidates=round_[1],
            decisions=event.decisions,
            remaining=record.request.max_new_tokens - token_index,
         )
         committed = result.committed[: max(0, record.request.max_new_tokens - token_index)]
         record.generated_token_ids.extend(committed)
      for offset, token_id in enumerate(committed):
         record.client_sink.emit(token_index + offset, token_id)
      should_cleanup = False
      with record.lock:
         if record.status != "DECODING":
            return True
         if len(record.generated_token_ids) >= record.request.max_new_tokens:
            record.state_machine.transition(
               "COMPLETED",
               path_attempt=record.manifest.path_attempt,
            )
            should_cleanup = True
      if should_cleanup:
         self._cleanup_record(record)
      else:
         self._a7_committed(record)
      return True

   def _path_backends(self, record: RequestRecord) -> tuple[str, ...]:
      placements = {
         placement.placement_id: placement
         for stage in record.graph.stages
         for placement in stage.placements
      }
      return tuple(
         placements[hop.placement_id].runtime_backend
         for hop in record.manifest.ordered_hops
         if hop.placement_id in placements
      )

   def _remaining_request_budget_ms(self, record: RequestRecord) -> float:
      """The request's own SLO budget left: TTFT plus TPOT for every output token."""

      request = record.request
      budget = max(0.0, request.target_ttft_ms) + request.max_new_tokens * max(
         0.0, request.target_tpot_ms
      )
      elapsed = max(0.0, (self.clock.now() - request.admitted_at) * 1000.0)
      return budget - elapsed

   @staticmethod
   def _await_decode_progress(
      record: RequestRecord,
      dispatch_key: tuple[str, int, int, int],
   ) -> None:
      """Wait briefly for the outstanding decode token, never re-sending its hop."""

      deadline = time.monotonic() + _DECODE_REDRIVE_WAIT_SECONDS
      while time.monotonic() < deadline:
         with record.lock:
            if (
               record.status != "DECODING"
               or len(record.generated_token_ids) != dispatch_key[3]
               or record.manifest.path_id != dispatch_key[0]
               or record.manifest.path_attempt != dispatch_key[1]
               or record.path_generation != dispatch_key[2]
            ):
               return
         time.sleep(_DECODE_REDRIVE_POLL_SECONDS)

   def decode_one(self, request_id: str) -> bool:
      if request_id in self._pending_prefills:
         return False
      record = self.get_request(request_id)
      while True:
         self._a7_await_acknowledged(record)
         record = self.get_request(request_id)
         with record.lock:
            if record.status != "DECODING":
               return False
            token_index = len(record.generated_token_ids)
            manifest = record.manifest
            graph = record.graph
            request = record.request
            if self.relay.decode_mode == "stage_local_kv":
               if not record.generated_token_ids:
                  return False
               decode_tokens = (record.generated_token_ids[-1],)
            else:
               decode_tokens = (
                  record.request.prompt_token_ids
                  + tuple(record.generated_token_ids)
               )
            generation = record.path_generation
         outcome = self.relay.execute_manifest(
            graph=graph,
            manifest=manifest,
            request=request,
            phase="DECODE",
            token_index=token_index,
            payload=decode_tokens,
            expected_generation=generation,
         )
         if outcome.failure_report is not None:
            if not self.receive_failure_report(outcome.failure_report):
               return False
            record = self.get_request(request_id)
            continue
         if outcome.token_event is None:
            with record.lock:
               if record.status != "DECODING":
                  return False
               record.state_machine.transition(
                  "FAILED",
                  path_attempt=record.manifest.path_attempt,
               )
            self._cleanup_record(record)
            return False
         return self.receive_token_event(outcome.token_event)

   def receive_token_event(
      self,
      event: TokenEvent,
      *,
      source_node_id: str | None = None,
   ) -> bool:
      record = self._requests.get(event.request_id)
      if record is None:
         return False
      with record.lock:
         if record.status != "DECODING":
            return False
         manifest = record.manifest
         if (
            event.path_id != manifest.path_id
            or event.path_attempt != manifest.path_attempt
            or event.token_index != len(record.generated_token_ids)
            or event.sampling_counter != event.token_index + 1
         ):
            return False
         if source_node_id is not None and not self._final_hop_origin_matches_locked_path(
            record,
            source_node_id,
         ):
            return False
         record.generated_token_ids.append(event.token_id)
      record.client_sink.emit(event.token_index, event.token_id)
      should_cleanup = False
      with record.lock:
         if record.status != "DECODING":
            return True
         if len(record.generated_token_ids) >= record.request.max_new_tokens:
            record.state_machine.transition(
               "COMPLETED",
               path_attempt=record.manifest.path_attempt,
            )
            should_cleanup = True
      if should_cleanup:
         self._cleanup_record(record)
      else:
         # The gateway acknowledged this token synchronously (``emit``
         # returned): it is committed output.  A7 coverage ships the source
         # KV for exactly this committed prefix.
         self._a7_committed(record)
      return True

   @staticmethod
   def _final_hop_origin_matches(
      graph: ExecutionGraph,
      manifest: PathManifest,
      source_node_id: str,
   ) -> bool:
      if not source_node_id or not manifest.ordered_hops:
         return False
      final_placement_id = manifest.ordered_hops[-1].placement_id
      return any(
         placement.placement_id == final_placement_id
         and placement.node_id == source_node_id
         for stage in graph.stages
         for placement in stage.placements
      )

   @classmethod
   def _final_hop_origin_matches_locked_path(
      cls,
      record: RequestRecord,
      source_node_id: str,
   ) -> bool:
      return cls._final_hop_origin_matches(
         record.graph,
         record.manifest,
         source_node_id,
      )

   def receive_failure_report(
      self,
      report: FailureReport,
      *,
      source_node_id: str | None = None,
      redispatch_successor: bool | None = None,
   ) -> bool:
      remembered = self._remember_failure_report(report, source_node_id)
      record = self._requests.get(report.request_id)
      if record is None:
         pending = self._pending_prefills.get(report.request_id)
         if pending is None:
            return False
         applied = self._fail_pending_prefill(
            pending,
            report,
            source_node_id,
            remembered,
         )
         if applied is not None:
            return applied
         # ManifestLocked won the race: the request is a locked record now.
         record = self._requests.get(report.request_id)
         if record is None:
            return False
      terminal_failure = False
      excluded_placements: frozenset[str] = frozenset()
      excluded_edges: frozenset[str] = frozenset()
      excluded_devices: frozenset[str] = frozenset()
      old_reservations: tuple[str, ...] = ()
      new_attempt = 0
      replacement_prompt: tuple[int, ...] = ()
      replacement_committed: tuple[int, ...] = ()
      with record.lock:
         if record.status in {
            "COMPLETED",
            "FAILED",
            "CANCELLED",
         }:
            return False
         manifest = record.manifest
         if (
            report.path_id != manifest.path_id
            or report.path_attempt != manifest.path_attempt
         ):
            return False
         if (
            record.status == "DECODING"
            and report.token_index != len(record.generated_token_ids)
         ):
            return False
         if not self._failure_identity_matches_locked_path(record, report):
            return False
         if source_node_id is not None and not self._failure_origin_matches_locked_path(
            record,
            report,
            source_node_id,
         ):
            return False
         if remembered is not None:
            remembered["applied"] = True
         self._record_phone_thermal_failure(record, report, source_node_id)
         terminal_failure = (
            report.scope == "DEVICE" and report.node_id == self.node_id
         ) or manifest.path_attempt >= self.config.maximum_recovery_attempts
         if terminal_failure:
            record.state_machine.transition(
               "FAILED",
               path_attempt=manifest.path_attempt,
            )
         else:
            excluded_placements = record.excluded_placements
            excluded_edges = record.excluded_edges
            excluded_devices = record.excluded_devices
            if report.scope == "EDGE" and report.edge_id:
               excluded_edges |= frozenset({report.edge_id})
            elif report.scope == "DEVICE" and report.node_id:
               excluded_devices |= frozenset({report.node_id})
            elif report.placement_id:
               excluded_placements |= frozenset({report.placement_id})
            old_reservations = tuple(
               hop.reservation_id for hop in manifest.ordered_hops
            )
            new_attempt = manifest.path_attempt + 1
            replacement_prompt = record.request.prompt_token_ids
            replacement_committed = tuple(record.generated_token_ids)
            record.state_machine.begin_recovery(path_attempt=new_attempt)
      if terminal_failure:
         self._cleanup_record(record)
         return False
      a7_outcome = self._a7_try_cutover(
         record,
         report,
         new_attempt=new_attempt,
         redispatch=(
            source_node_id is not None
            if redispatch_successor is None
            else redispatch_successor
         ),
      )
      if a7_outcome is not None:
         return a7_outcome
      # A7 KV-successor seam: the replacement-path attempt's recovery mode is
      # classified by the A7 contract before this coordinator commits to it.
      kv_successor_recovery = classify_kv_successor_recovery(
         decode_mode=self.relay.decode_mode,
         path_attempt=new_attempt,
         prompt_token_ids=replacement_prompt,
         committed_token_ids=replacement_committed,
      )
      with record.lock:
         record.kv_successor_recovery = kv_successor_recovery
      if kv_successor_recovery["recovery_mode"] != KV_SUCCESSOR_EXECUTABLE_OUTCOME:
         # A fenced KV cutover is not wired in this path, so an eligible (or
         # aborted) classification fails the attempt closed instead of being
         # silently downgraded to a replayed attempt.
         refused = False
         with record.lock:
            if record.status != "CANCELLED":
               record.state_machine.transition(
                  "FAILED",
                  path_attempt=new_attempt,
               )
               refused = True
         if refused:
            self._cleanup_record(record)
         return False
      self.capacity.release(old_reservations)
      self.relay.release_path(
         manifest.path_id,
         path_attempt=manifest.path_attempt,
      )
      try:
         graph, new_manifest, build = self._build_path(
            record.request,
            graph=record.graph,
            path_attempt=new_attempt,
            excluded_placements=excluded_placements,
            excluded_edges=excluded_edges,
            excluded_devices=excluded_devices,
         )
      except RoutingError:
         with record.lock:
            if record.status == "CANCELLED":
               return False
            record.state_machine.transition(
               "FAILED",
               path_attempt=new_attempt,
            )
            record.cleaned_up = True
            failed_manifest = record.manifest
         # The old path was released above; an A7-covered request still owns its
         # durable fence record and shadow, which only the terminal finish releases.
         controller = self.a7_controller
         if controller is not None:
            controller.finish(record.request.request_id, failed_manifest, outcome="failed")
         return False

      registration_failed = False
      generation = 0
      replay_tokens: tuple[int, ...] = ()
      recovery_token_index = 0
      with record.lock:
         if record.status != "PREFILL":
            cancelled_during_build = True
         else:
            cancelled_during_build = False
            record.graph = graph
            record.manifest = new_manifest
            record.excluded_placements = build.excluded_placements
            record.excluded_edges = build.excluded_edges
            record.excluded_devices = build.excluded_devices
            record.cleaned_up = False
            registered_generation = self.relay.register_path_with_generation(
               record.request,
               new_manifest,
               graph,
               entry_node_id=self.node_id,
            )
            if registered_generation is None:
               record.state_machine.transition(
                  "FAILED",
                  path_attempt=new_attempt,
               )
               registration_failed = True
            else:
               registration_failed = False
               generation = registered_generation
               record.path_generation = generation
               replay_tokens = (
                  record.request.prompt_token_ids
                  + tuple(record.generated_token_ids)
               )
               recovery_token_index = (
                  len(record.generated_token_ids)
                  if self.relay.decode_mode == "stage_local_kv"
                  else len(record.generated_token_ids) - 1
               )
      if cancelled_during_build:
         self.capacity.release(
            tuple(hop.reservation_id for hop in new_manifest.ordered_hops)
         )
         self.relay.release_path(
            new_manifest.path_id,
            path_attempt=new_manifest.path_attempt,
         )
         return False
      if registration_failed:
         self._cleanup_record(record)
         return False
      outcome = self.relay.execute_manifest(
         graph=graph,
         manifest=new_manifest,
         request=record.request,
         phase="RECOVERY_PREFILL",
         token_index=recovery_token_index,
         payload=replay_tokens,
         expected_generation=generation,
      )
      with record.lock:
         if record.status != "PREFILL":
            return False
         if outcome.failure_report is not None or (
            self.relay.decode_mode == "stage_local_kv"
            and outcome.token_event is None
         ):
            record.state_machine.transition(
               "FAILED",
               path_attempt=new_manifest.path_attempt,
            )
            failed = True
         else:
            record.state_machine.transition(
               "LOCKED",
               path_attempt=new_manifest.path_attempt,
            )
            record.state_machine.transition(
               "DECODING",
               path_attempt=new_manifest.path_attempt,
            )
            failed = False
      if failed:
         self._cleanup_record(record)
         return False
      if outcome.token_event is not None:
         return self.receive_token_event(outcome.token_event)
      return True

   def _fail_pending_prefill(
      self,
      pending: PendingPrefill,
      report: FailureReport,
      source_node_id: str | None,
      remembered: dict[str, Any] | None,
   ) -> bool | None:
      """Apply a FailureReport to a progressive prefill still awaiting its lock.

      A downstream stage can fail chunk 0 before the final stage locks the
      manifest (runtime failure, route extension, manifest lock).  The request
      fails at once with the reported cause instead of waiting for the node's
      prefill watchdog; the entry releases what it holds for the path, as for a
      terminal post-lock failure.  Returns ``None`` when ``ManifestLocked``
      already turned the request into a locked record (the caller applies the
      report there), else ``False`` (never a recovery).
      """

      with pending.lock:
         state = pending.state_machine.state
         if state != "PREFILL":
            return None if state in {"LOCKED", "DECODING"} else False
         if not self._failure_identity_matches_pending_prefill(
            pending,
            report,
            source_node_id,
         ):
            return False
         if remembered is not None:
            remembered["applied"] = True
         pending.state_machine.transition(
            "FAILED",
            path_attempt=pending.build.path_attempt,
         )
      try:
         self.builder.abort(pending.build)
      finally:
         self.relay.release_path(
            pending.build.path_id,
            path_attempt=pending.build.path_attempt,
         )
      return False

   def _failure_identity_matches_pending_prefill(
      self,
      pending: PendingPrefill,
      report: FailureReport,
      source_node_id: str | None,
   ) -> bool:
      """The same binding a locked path gets, limited to what the entry knows.

      Before the lock the entry knows the path identity and the hops it chose
      itself; later hops are chosen downstream.  A report must name this
      request's path and attempt and the first-chunk prefill (``-1``), and a
      placement that can be on the path: one of the entry's own hops, or a
      placement of a stage not yet built.  An authenticated origin must host
      that placement (``DEVICE``: be the reported node).
      """

      build = pending.build
      if (
         report.path_id != build.path_id
         or report.path_attempt != build.path_attempt
         or report.token_index != -1
      ):
         return False
      graph = pending.graph
      known = len(build.ordered_hops)
      candidates = {hop.placement_id for hop in build.ordered_hops}
      candidates.update(
         placement.placement_id
         for stage in graph.stages[known:]
         for placement in stage.placements
      )
      placement_by_id = {
         placement.placement_id: placement
         for stage in graph.stages
         for placement in stage.placements
         if placement.placement_id in candidates
      }
      if report.scope == "DEVICE":
         if not report.node_id:
            return False
         if source_node_id is not None and report.node_id != source_node_id:
            return False
         return report.node_id == self.node_id or any(
            placement.node_id == report.node_id
            for placement in placement_by_id.values()
         )
      if report.scope not in {"PLACEMENT", "EDGE"}:
         return False
      placement = placement_by_id.get(report.placement_id)
      if placement is None:
         return False
      if report.scope == "EDGE":
         edge = next(
            (
               edge
               for edge in (*graph.edges, *graph.loopback_edges)
               if edge.edge_id == report.edge_id
            ),
            None,
         )
         if (
            edge is None
            or report.placement_id not in {
               edge.from_placement_id,
               edge.to_placement_id,
            }
            or edge.from_placement_id not in candidates
            or edge.to_placement_id not in candidates
         ):
            return False
      if source_node_id is None:
         return True
      return placement.node_id == source_node_id and (
         not report.node_id or report.node_id == source_node_id
      )

   def _failure_identity_matches_locked_path(
      self,
      record: RequestRecord,
      report: FailureReport,
   ) -> bool:
      manifest = record.manifest
      selected_placement_ids = {
         hop.placement_id for hop in manifest.ordered_hops
      }
      if report.scope == "PLACEMENT":
         return report.placement_id in selected_placement_ids

      placement_by_id = {
         placement.placement_id: placement
         for stage in record.graph.stages
         for placement in stage.placements
      }
      if report.scope == "DEVICE":
         return report.node_id == self.node_id or any(
            placement_by_id[placement_id].node_id == report.node_id
            for placement_id in selected_placement_ids
         )
      if report.scope != "EDGE":
         return False

      selected_pairs = {
         (left.placement_id, right.placement_id)
         for left, right in zip(
            manifest.ordered_hops,
            manifest.ordered_hops[1:],
         )
      }
      selected_edge_ids = {
         edge.edge_id
         for edge in record.graph.edges
         if (edge.from_placement_id, edge.to_placement_id) in selected_pairs
      }
      selected_edge_ids.add(manifest.loopback_edge_id)
      return report.edge_id in selected_edge_ids

   @staticmethod
   def _failure_origin_matches_locked_path(
      record: RequestRecord,
      report: FailureReport,
      source_node_id: str,
   ) -> bool:
      if not source_node_id:
         return False
      placement_by_id = {
         placement.placement_id: placement
         for stage in record.graph.stages
         for placement in stage.placements
      }
      if report.scope == "DEVICE":
         return report.node_id == source_node_id
      placement = placement_by_id.get(report.placement_id)
      if placement is None or placement.node_id != source_node_id:
         return False
      if report.scope == "EDGE":
         edge = next(
            (
               edge
               for edge in (*record.graph.edges, *record.graph.loopback_edges)
               if edge.edge_id == report.edge_id
            ),
            None,
         )
         if edge is None or report.placement_id not in {
            edge.from_placement_id,
            edge.to_placement_id,
         }:
            return False
      return not report.node_id or report.node_id == source_node_id

   def _cleanup_record(self, record: RequestRecord) -> None:
      with record.lock:
         if record.cleaned_up:
            return
         record.cleaned_up = True
         record.verify_round = None
         manifest = record.manifest
         status = record.status
         successor = record.kv_successor_cutover is not None
      speculation = self.a11_controller
      if speculation is not None:
         speculation.release(record.request.request_id)
      self.capacity.release(
         tuple(hop.reservation_id for hop in manifest.ordered_hops)
      )
      self.relay.release_path(
         manifest.path_id,
         path_attempt=manifest.path_attempt,
      )
      controller = self.a7_controller
      if controller is not None and status in {"COMPLETED", "FAILED", "CANCELLED"}:
         controller.finish(
            record.request.request_id,
            manifest,
            outcome={"COMPLETED": "completed", "FAILED": "failed", "CANCELLED": "cancelled"}[status],
         )
      if successor:
         cancellation = PathCancellation(
            request_id=record.request.request_id,
            path_id=manifest.path_id,
            path_attempt=manifest.path_attempt,
            topology_version=manifest.topology_version,
         )
         # The route's original binding does not name the successor path.
         # Terminal release therefore reaches its relays too. Owner fanout
         # may already have retired transport ownership; do not recreate it.
         send_if_entry = getattr(self.transport, "send_path_cancellation_if_entry", None)
         if callable(send_if_entry):
            send_if_entry(cancellation)
         else:
            self.transport.send_path_cancellation(cancellation)

   # -- A7 fenced KV-successor recovery ---------------------------------------------

   def _a7_committed(self, record: RequestRecord) -> None:
      controller = self.a7_controller
      if controller is None or self.relay.decode_mode != "stage_local_kv":
         return
      with record.lock:
         if record.status != "DECODING":
            return
         manifest = record.manifest
         committed = tuple(record.generated_token_ids)
         graph = record.graph
      node_by_placement = {
         placement.placement_id: placement.node_id
         for stage in graph.stages
         for placement in stage.placements
      }
      controller.on_committed(
         record.request.request_id,
         manifest,
         committed,
         # Every node on the path holds KV the terminal release must free.
         path_node_ids=tuple(
            node_by_placement[hop.placement_id]
            for hop in manifest.ordered_hops
            if hop.placement_id in node_by_placement
         ),
      )

   def _a7_await_acknowledged(self, record: RequestRecord) -> None:
      """Synchronous-ack policy: the next decode waits (bounded) for the standby ack."""

      controller = self.a7_controller
      if controller is not None:
         controller.await_acknowledged(record.request.request_id)

   def _a7_try_cutover(
      self,
      record: RequestRecord,
      report: FailureReport,
      *,
      new_attempt: int,
      redispatch: bool = False,
   ) -> bool | None:
      """Fenced KV-successor cutover; ``None`` hands the attempt to A6 replay.

      ``redispatch`` (a transport-delivered failure report): the distributed
      decode driver is waiting for the token of the failed step, so the
      successor re-sends exactly that DECODE step itself.  A synchronous
      ``decode_one`` caller re-executes it on its own.
      """

      controller = self.a7_controller
      if controller is None or self.relay.decode_mode != "stage_local_kv":
         return None
      request_id = record.request.request_id
      with record.lock:
         if record.status != "PREFILL":
            return None
         old = record.manifest
         graph = record.graph
         committed = tuple(record.generated_token_ids)
         prompt = record.request.prompt_token_ids
      coverage = controller.failure_is_covered(request_id, old, report)
      if coverage is None:
         return None
      try:
         successor, build = self._build_successor_path(
            record,
            graph=graph,
            old=old,
            replacements={coverage.source_placement_id: coverage.standby_placement_id},
            path_attempt=new_attempt,
         )
      except RoutingError:
         result = controller.decline(request_id, old, reason="successor_path_unavailable", gateway_committed_token_ids=committed)
         return self._a7_fallback(record, result, new_attempt=new_attempt)
      node_by_placement = {
         placement.placement_id: placement.node_id
         for stage in graph.stages
         for placement in stage.placements
      }

      def router_fenced(_generation: int) -> bool:
         with record.lock:
            # Recovery already moved the request out of DECODING, so no token
            # or failure of the old attempt can be accepted any more.
            return (
               record.status == "PREFILL"
               and record.manifest is old
               and record.state_machine.path_attempt == new_attempt
            )

      def gateway_fenced(_generation: int) -> bool:
         with record.lock:
            return tuple(record.generated_token_ids) == committed

      def transport_fenced(_generation: int) -> bool:
         self.relay.release_path(old.path_id, path_attempt=old.path_attempt)
         return True

      result = controller.cutover(
         request_id=request_id,
         old_manifest=old,
         successor_manifest=successor,
         gateway_committed_token_ids=committed,
         expected_next_position=len(prompt) + len(committed) - 1,
         lease_expires_at=min(hop.reservation_expires_at for hop in successor.ordered_hops),
         node_for_placement=node_by_placement.__getitem__,
         installers={
            "request_gateway": gateway_fenced,
            "router": router_fenced,
            "transport_ingress": transport_fenced,
         },
      )
      if result["outcome"] != "fenced_kv_successor":
         self.capacity.release(tuple(hop.reservation_id for hop in successor.ordered_hops))
         return self._a7_fallback(record, result, new_attempt=new_attempt)
      self.capacity.release(tuple(hop.reservation_id for hop in old.ordered_hops))
      self.relay.release_path(old.path_id, path_attempt=old.path_attempt)
      # The successor has its own path id, so nothing else retires the old
      # one at the surviving participants' relays (the route's completion
      # cleanup names the request's original path): cancel it there now. The
      # relays accept it from this entry only; idle expiry is the backstop
      # if a participant is unreachable.
      try:
         self.transport.send_path_cancellation(
            PathCancellation(
               request_id=request_id,
               path_id=old.path_id,
               path_attempt=old.path_attempt,
               topology_version=graph.topology_version,
            )
         )
      except Exception:
         pass
      registration_failed = False
      with record.lock:
         record.kv_successor_cutover = result
         if record.status != "PREFILL":
            cancelled = True
         else:
            cancelled = False
            record.graph = graph
            record.manifest = successor
            record.excluded_placements = build.excluded_placements
            record.cleaned_up = False
            generation = self.relay.register_path_with_generation(
               record.request,
               successor,
               graph,
               entry_node_id=self.node_id,
            )
            if generation is None:
               record.state_machine.transition("FAILED", path_attempt=new_attempt)
               registration_failed = True
            else:
               record.path_generation = generation
               record.decode_dispatched = None
               record.state_machine.transition("LOCKED", path_attempt=new_attempt)
      if cancelled:
         self.capacity.release(tuple(hop.reservation_id for hop in successor.ordered_hops))
         controller.finish(request_id, successor, outcome="cancelled")
         return False
      if registration_failed:
         self._cleanup_record(record)
         return False
      try:
         self.transport.send_manifest_locked(
            ManifestLocked(
               request_id=request_id,
               path_id=successor.path_id,
               path_attempt=successor.path_attempt,
               manifest=successor,
               build=build,
            )
         )
      except Exception:
         with record.lock:
            if record.status == "LOCKED":
               record.state_machine.transition("FAILED", path_attempt=new_attempt)
         self._cleanup_record(record)
         return False
      with record.lock:
         if record.status != "LOCKED":
            return False
         # No recovery prefill: the successor resumes DECODE at the next
         # contiguous position from the imported, acknowledged KV.
         record.state_machine.transition("DECODING", path_attempt=new_attempt)
      if redispatch:
         self.decode_one_distributed(request_id)
      return True

   def report_lost_source(self, request_id: str, placement_id: str, *, reason: str = "node_process_exited") -> bool:
      """A7: an exited source placement observed by liveness, not by this transport.

      Builds the PLACEMENT failure report for the request's current locked path
      at the exact decode position and hands it to the ordinary failure path
      (fenced KV-successor cutover, or its truthful A6/abort fallback).  The
      waiting decode driver needs the successor to re-send the failed step.
      Returns whether the report was accepted; a request that is not decoding
      through ``placement_id`` keeps its existing handling.
      """

      record = self._requests.get(request_id)
      if record is None:
         return False
      with record.lock:
         if record.status != "DECODING":
            return False
         manifest = record.manifest
         if placement_id not in {hop.placement_id for hop in manifest.ordered_hops}:
            return False
         report = FailureReport(
            request_id=request_id,
            path_id=manifest.path_id,
            path_attempt=manifest.path_attempt,
            token_index=len(record.generated_token_ids),
            scope="PLACEMENT",
            reason=reason[:64],
            placement_id=placement_id,
            node_id=self.node_id,
         )
      return self.receive_failure_report(report, redispatch_successor=True)

   def _a7_fallback(self, record: RequestRecord, result: dict, *, new_attempt: int) -> bool | None:
      with record.lock:
         record.kv_successor_cutover = result
         if result["outcome"] != "aborted":
            return None
         if record.status == "CANCELLED":
            return False
         # Neither A7 nor A6 has authority: one explicit terminal result.
         record.state_machine.transition("FAILED", path_attempt=new_attempt)
      self._cleanup_record(record)
      return False

   def _build_successor_path(
      self,
      record: RequestRecord,
      *,
      graph: ExecutionGraph,
      old: PathManifest,
      replacements: dict[str, str],
      path_attempt: int,
   ) -> tuple[PathManifest, PathBuildState]:
      """Reserve the exact successor path: the old hops with the standby substituted."""

      request = record.request
      build = PathBuildState(
         request=request,
         graph=graph,
         path_id=self.builder.id_source.new("path"),
         path_attempt=path_attempt,
         excluded_placements=record.excluded_placements | frozenset(replacements),
         excluded_edges=record.excluded_edges,
         excluded_devices=record.excluded_devices,
      )
      stages = {stage.stage_id: stage for stage in graph.stages}
      now = self.clock.now()
      lease_seconds = self.builder.reservation_lease_seconds()
      for hop in old.ordered_hops:
         placement_id = replacements.get(hop.placement_id, hop.placement_id)
         stage = stages[hop.stage_id]
         reservation = self.capacity.reserve(
            ReservationRequest(
               request_id=request.request_id,
               path_id=build.path_id,
               path_attempt=path_attempt,
               placement_id=placement_id,
               kv_bytes=(len(request.prompt_token_ids) + request.max_new_tokens)
               * stage.stage_cost.kv_bytes_per_context_token,
               deployment_epoch=graph.deployment_epoch,
               lease_expires_at=now + lease_seconds,
            )
         )
         if not (
            reservation.accepted
            and reservation.deployment_epoch == graph.deployment_epoch
            and reservation.expires_at > now
         ):
            if reservation.accepted and reservation.reservation_id:
               self.capacity.release((reservation.reservation_id,))
            self.builder.abort(build)
            raise RoutingError("successor_reservation_rejected")
         build = PathBuildState(
            request=build.request,
            graph=build.graph,
            path_id=build.path_id,
            path_attempt=build.path_attempt,
            ordered_hops=build.ordered_hops
            + (
               PathHop(
                  stage_id=stage.stage_id,
                  placement_id=placement_id,
                  reservation_id=reservation.reservation_id,
                  reservation_expires_at=reservation.expires_at,
                  reservation_epoch=reservation.deployment_epoch,
               ),
            ),
            excluded_placements=build.excluded_placements,
            excluded_edges=build.excluded_edges,
            excluded_devices=build.excluded_devices,
         )
         self.transport.send_manifest_delta(
            ManifestDelta(
               request_id=request.request_id,
               path_id=build.path_id,
               path_attempt=build.path_attempt,
               hop_index=len(build.ordered_hops) - 1,
               hop=build.ordered_hops[-1],
            )
         )
      manifest = self.builder.lock(build, now=now)
      return manifest, build

   def _build_path(
      self,
      request: RequestContext,
      *,
      graph: ExecutionGraph,
      path_attempt: int,
      excluded_placements: frozenset[str],
      excluded_edges: frozenset[str],
      excluded_devices: frozenset[str],
   ) -> tuple[ExecutionGraph, PathManifest, PathBuildState]:
      excluded_devices |= self._phone_thermal_exclusions()
      build = self.builder.start(
         request,
         graph,
         path_attempt=path_attempt,
         excluded_placements=excluded_placements,
         excluded_edges=excluded_edges,
         excluded_devices=excluded_devices,
      )
      while not self.builder.is_complete(build):
         build = self.builder.advance(
            build,
            self.device_states.snapshot(),
            now=self.clock.now(),
         )
         self.transport.send_manifest_delta(
            ManifestDelta(
               request_id=request.request_id,
               path_id=build.path_id,
               path_attempt=build.path_attempt,
               hop_index=len(build.ordered_hops) - 1,
               hop=build.ordered_hops[-1],
            )
         )
      manifest = self.builder.lock(build, now=self.clock.now())
      return graph, manifest, build
