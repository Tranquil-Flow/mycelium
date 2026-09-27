"""Request admission, checkpointing, decode, failure recovery, and the A7
KV-successor classification of every replacement-path attempt."""

import importlib
from dataclasses import dataclass, field
from threading import RLock
from typing import Any

from mycelium_router.contracts import (
   ExecutionGraph,
   FailureReport,
   HopHeader,
   ManifestDelta,
   ManifestLocked,
   PathBuildState,
   PathCancellation,
   PathManifest,
   PrefillChunkCompleted,
   ProgressivePrefillContext,
   RequestContext,
   TokenEvent,
)
from mycelium_router.idempotency import hop_idempotency_key, payload_fingerprint
from mycelium_router.payloads import encode_token_ids
from mycelium_router.routing import RoutingError
from mycelium_router.state import RequestStateMachine
from mycelium_router.validation import validate_manifest
from mycelium_router.leases import validate_hop_leases


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
      with record.lock:
         if record.status != "DECODING":
            return False
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
      return self.relay.dispatch_if_current(
         path_id=manifest.path_id,
         path_attempt=manifest.path_attempt,
         generation=generation,
         sender=lambda: self.transport.send_hop(
            header,
            encode_token_ids(decode_tokens),
         ),
      )

   def decode_one(self, request_id: str) -> bool:
      if request_id in self._pending_prefills:
         return False
      record = self.get_request(request_id)
      while True:
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
   ) -> bool:
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
         manifest = record.manifest
      self.capacity.release(
         tuple(hop.reservation_id for hop in manifest.ordered_hops)
      )
      self.relay.release_path(
         manifest.path_id,
         path_attempt=manifest.path_attempt,
      )

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
