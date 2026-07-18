"""Inter-layer relay execution over an already locked path manifest."""

from mycelium_router.batching import PhaseAwareBatchController
from mycelium_router.contracts import (
   BatchExecutionObservation,
   BatchNetworkStats,
   ExecutionGraph,
   FailureReport,
   HopHeader,
   HopReceiveResult,
   HopWorkItem,
   ManifestDelta,
   ManifestLocked,
   PathManifest,
   PrefillChunkCompleted,
   ProgressivePrefillContext,
   ProgressivePrefillResult,
   RelayOutcome,
   RequestContext,
   RuntimeBatchKey,
   RuntimeResult,
   TokenEvent,
)
from mycelium_router.idempotency import hop_idempotency_key
from mycelium_router.routing import RoutingError
from mycelium_router.scheduler import (
   BackpressureError,
   DuplicateHopError,
   HopScheduler,
)
from mycelium_router.state import HopStateMachine
from mycelium_router.validation import validate_manifest


class RelayEngine:
   def __init__(
      self,
      *,
      node_id,
      runtime,
      transport,
      scheduler,
      clock,
      builder,
      device_states,
   ):
      self.node_id = node_id
      self.runtime = runtime
      self.transport = transport
      self.scheduler = scheduler
      self.batch_scheduler = HopScheduler(scheduler.config)
      self.batch_controller = PhaseAwareBatchController(scheduler.config)
      self.clock = clock
      self.builder = builder
      self.device_states = device_states
      self._outcomes: dict[
         tuple[str, int, str, int],
         tuple[RelayOutcome, float, str],
      ] = {}
      self._paths: dict[
         str,
         tuple[ExecutionGraph, PathManifest, RequestContext],
      ] = {}
      self._entry_node_by_path: dict[str, str] = {}
      self._hop_results: dict[
         str,
         tuple[HopReceiveResult, float, str],
      ] = {}
      self._prefill_results: dict[
         str,
         tuple[ProgressivePrefillResult, float, str],
      ] = {}
      self._pending_hops: dict[str, tuple[HopHeader, HopWorkItem]] = {}

   def register_path(
      self,
      request: RequestContext,
      manifest: PathManifest,
      graph: ExecutionGraph,
      *,
      source_node_id: str | None = None,
      entry_node_id: str | None = None,
   ) -> bool:
      validate_manifest(manifest, graph)
      if request.request_id != manifest.request_id:
         return False
      if source_node_id is not None:
         placement_map = {
            placement.placement_id: placement
            for stage in graph.stages
            for placement in stage.placements
         }
         final_placement = placement_map[manifest.ordered_hops[-1].placement_id]
         if not source_node_id or final_placement.node_id != source_node_id:
            return False
         if not entry_node_id:
            return False
      existing = self._paths.get(manifest.path_id)
      if existing is not None:
         existing_manifest = existing[1]
         if manifest.path_attempt < existing_manifest.path_attempt:
            return False
         if manifest.path_attempt == existing_manifest.path_attempt:
            if existing != (graph, manifest, request):
               return False
            existing_entry = self._entry_node_by_path.get(manifest.path_id)
            if (
               entry_node_id is not None
               and existing_entry is not None
               and entry_node_id != existing_entry
            ):
               return False
            if entry_node_id is not None:
               self._entry_node_by_path[manifest.path_id] = entry_node_id
            return True
         existing_entry = self._entry_node_by_path.get(manifest.path_id)
         if (
            entry_node_id is not None
            and existing_entry is not None
            and entry_node_id != existing_entry
         ):
            return False
      self._paths[manifest.path_id] = (graph, manifest, request)
      if entry_node_id is not None:
         self._entry_node_by_path[manifest.path_id] = entry_node_id
      return True

   def receive_progressive_prefill(
      self,
      header: HopHeader,
      context: ProgressivePrefillContext,
      *,
      source_node_id: str | None = None,
   ) -> ProgressivePrefillResult:
      now = self.clock.now()
      self._evict_prefill_results(now=now)
      expected_key = hop_idempotency_key(
         request_id=header.request_id,
         path_id=header.path_id,
         path_attempt=header.path_attempt,
         phase=header.phase,
         token_index=header.token_index,
         hop_index=header.hop_index,
      )
      if header.idempotency_key != expected_key:
         return ProgressivePrefillResult("REJECTED", "invalid_idempotency_key")
      build = context.build
      if (
         header.phase != "PREFILL"
         or header.request_id != context.request.request_id
         or header.path_id != build.path_id
         or header.path_attempt != build.path_attempt
         or header.topology_version != context.graph.topology_version
         or header.hop_index != len(build.ordered_hops) - 1
      ):
         return ProgressivePrefillResult("REJECTED", "prefill_context_mismatch")
      hop = build.ordered_hops[header.hop_index]
      if header.destination_placement_id != hop.placement_id:
         return ProgressivePrefillResult("REJECTED", "manifest_hop_mismatch")
      expected_source = (
         ""
         if header.hop_index == 0
         else build.ordered_hops[header.hop_index - 1].placement_id
      )
      if header.source_placement_id != expected_source:
         return ProgressivePrefillResult("REJECTED", "source_placement_mismatch")
      placement_map = {
         placement.placement_id: placement
         for stage in context.graph.stages
         for placement in stage.placements
      }
      if source_node_id is not None and header.hop_index > 0:
         source_placement = placement_map.get(header.source_placement_id)
         if (
            source_placement is None
            or source_placement.node_id != source_node_id
         ):
            return ProgressivePrefillResult("REJECTED", "source_node_mismatch")
      placement = placement_map[hop.placement_id]
      if placement.node_id != self.node_id:
         return ProgressivePrefillResult("REJECTED", "destination_not_local")
      cached = self._prefill_results.get(header.idempotency_key)
      if cached is not None:
         return cached[0]

      state = HopStateMachine(path_attempt=header.path_attempt)
      state.transition("QUEUED", path_attempt=header.path_attempt)
      work = HopWorkItem(
         request_id=context.request.request_id,
         path_id=build.path_id,
         path_attempt=build.path_attempt,
         phase="PREFILL",
         token_index=-1,
         hop_index=header.hop_index,
         placement_id=hop.placement_id,
         qos_class=context.request.qos_class,
         deficit_ratio=0.0,
         enqueued_at=now,
         idempotency_key=header.idempotency_key,
         payload=context.payload,
         prefill_chunk_token_count=header.prefill_chunk_token_count,
         batch_key=self._runtime_batch_key(
            graph=context.graph,
            placement=placement,
            phase="PREFILL",
            token_span=header.prefill_chunk_token_count,
         ),
      )
      try:
         self.scheduler.enqueue(work)
      except BackpressureError as error:
         return ProgressivePrefillResult(
            "REJECTED",
            reason=f"backpressure:{error.reason}",
            retry_after_seconds=error.retry_after_seconds,
         )
      selected = self.scheduler.pop_next(now=now)
      state.transition("ACCEPTED", path_attempt=header.path_attempt)
      state.transition("EXECUTING", path_attempt=header.path_attempt)
      runtime_result = self.runtime.execute(selected)
      if not runtime_result.success:
         state.transition("FAILED", path_attempt=header.path_attempt)
         report = FailureReport(
            request_id=context.request.request_id,
            path_id=build.path_id,
            path_attempt=build.path_attempt,
            token_index=-1,
            scope=runtime_result.failure_scope or "PLACEMENT",
            reason=runtime_result.failure_reason or "runtime_failure",
            placement_id=hop.placement_id,
            node_id=placement.node_id,
         )
         self.transport.send_failure_report(report)
         result = ProgressivePrefillResult(
            "FAILED",
            reason=report.reason,
            failure_report=report,
         )
         self._remember_prefill(header, result)
         return result

      if self.builder.is_complete(build):
         try:
            manifest = self.builder.lock(build, now=now)
         except RoutingError as error:
            state.transition("FAILED", path_attempt=header.path_attempt)
            report = FailureReport(
               request_id=context.request.request_id,
               path_id=build.path_id,
               path_attempt=build.path_attempt,
               token_index=-1,
               scope="PLACEMENT",
               reason=f"manifest_lock_failed:{error}",
               placement_id=hop.placement_id,
               node_id=placement.node_id,
            )
            self.transport.send_failure_report(report)
            result = ProgressivePrefillResult(
               "FAILED",
               reason=report.reason,
               failure_report=report,
            )
            self._remember_prefill(header, result)
            return result
         confirmation = ManifestLocked(
            request_id=context.request.request_id,
            path_id=build.path_id,
            path_attempt=build.path_attempt,
            manifest=manifest,
            build=build,
         )
         self.register_path(context.request, manifest, context.graph)
         self.transport.send_manifest_locked(confirmation)
         state.transition("FORWARDED", path_attempt=header.path_attempt)
         result = ProgressivePrefillResult(
            "LOCKED",
            confirmation=confirmation,
         )
         self._remember_prefill(header, result)
         return result

      try:
         updated = self.builder.advance(
            build,
            self.device_states.snapshot(),
            now=now,
         )
      except RoutingError as error:
         state.transition("FAILED", path_attempt=header.path_attempt)
         report = FailureReport(
            request_id=context.request.request_id,
            path_id=build.path_id,
            path_attempt=build.path_attempt,
            token_index=-1,
            scope="PLACEMENT",
            reason=f"route_extension_failed:{error}",
            placement_id=hop.placement_id,
            node_id=placement.node_id,
         )
         self.transport.send_failure_report(report)
         result = ProgressivePrefillResult(
            "FAILED",
            reason=report.reason,
            failure_report=report,
         )
         self._remember_prefill(header, result)
         return result
      next_index = len(updated.ordered_hops) - 1
      next_hop = updated.ordered_hops[next_index]
      self.transport.send_manifest_delta(
         ManifestDelta(
            request_id=context.request.request_id,
            path_id=updated.path_id,
            path_attempt=updated.path_attempt,
            hop_index=next_index,
            hop=next_hop,
         )
      )
      next_header = HopHeader(
         request_id=context.request.request_id,
         path_id=updated.path_id,
         path_attempt=updated.path_attempt,
         phase="PREFILL",
         token_index=-1,
         hop_index=next_index,
         source_placement_id=hop.placement_id,
         destination_placement_id=next_hop.placement_id,
         topology_version=context.graph.topology_version,
         idempotency_key=hop_idempotency_key(
            request_id=context.request.request_id,
            path_id=updated.path_id,
            path_attempt=updated.path_attempt,
            phase="PREFILL",
            token_index=-1,
            hop_index=next_index,
         ),
         prefill_chunk_token_count=header.prefill_chunk_token_count,
      )
      next_context = ProgressivePrefillContext(
         graph=context.graph,
         request=context.request,
         build=updated,
         payload=runtime_result.payload,
      )
      self.transport.send_hop(next_header, next_context)
      state.transition("FORWARDED", path_attempt=header.path_attempt)
      result = ProgressivePrefillResult(
         "FORWARDED",
         forwarded_header=next_header,
         context=next_context,
      )
      self._remember_prefill(header, result)
      return result

   def receive_hop(
      self,
      header: HopHeader,
      payload: object,
      *,
      source_node_id: str | None = None,
   ) -> HopReceiveResult:
      now = self.clock.now()
      self._evict_hop_results(now=now)
      expected_key = hop_idempotency_key(
         request_id=header.request_id,
         path_id=header.path_id,
         path_attempt=header.path_attempt,
         phase=header.phase,
         token_index=header.token_index,
         hop_index=header.hop_index,
      )
      if header.idempotency_key != expected_key:
         return HopReceiveResult("REJECTED", "invalid_idempotency_key")
      registration = self._paths.get(header.path_id)
      if registration is None:
         return HopReceiveResult("REJECTED", "unknown_path")
      graph, manifest, request = registration
      if (
         header.path_attempt != manifest.path_attempt
         or header.request_id != manifest.request_id
      ):
         return HopReceiveResult("REJECTED", "stale_path_attempt")
      if header.topology_version != manifest.topology_version:
         return HopReceiveResult("REJECTED", "topology_version_mismatch")
      if header.hop_index < 0 or header.hop_index >= len(manifest.ordered_hops):
         return HopReceiveResult("REJECTED", "invalid_hop_index")
      hop = manifest.ordered_hops[header.hop_index]
      if header.destination_placement_id != hop.placement_id:
         return HopReceiveResult("REJECTED", "manifest_hop_mismatch")
      if header.hop_index == 0:
         valid_sources = {""}
         if header.phase == "DECODE":
            valid_sources.add(manifest.ordered_hops[-1].placement_id)
      else:
         valid_sources = {
            manifest.ordered_hops[header.hop_index - 1].placement_id
         }
      if header.source_placement_id not in valid_sources:
         return HopReceiveResult("REJECTED", "source_placement_mismatch")
      placement_map = {
         placement.placement_id: placement
         for stage in graph.stages
         for placement in stage.placements
      }
      if source_node_id is not None:
         if header.hop_index == 0:
            if self._entry_node_by_path.get(header.path_id) != source_node_id:
               return HopReceiveResult("REJECTED", "entry_node_mismatch")
         else:
            source_placement = placement_map.get(header.source_placement_id)
            if (
               source_placement is None
               or source_placement.node_id != source_node_id
            ):
               return HopReceiveResult("REJECTED", "source_node_mismatch")
      placement = placement_map[hop.placement_id]
      if placement.node_id != self.node_id:
         return HopReceiveResult("REJECTED", "destination_not_local")
      cached = self._hop_results.get(header.idempotency_key)
      if cached is not None:
         return cached[0]
      if header.idempotency_key in self._pending_hops:
         return HopReceiveResult("QUEUED", "duplicate_pending")

      state = HopStateMachine(path_attempt=header.path_attempt)
      state.transition("QUEUED", path_attempt=header.path_attempt)
      work = HopWorkItem(
         request_id=request.request_id,
         path_id=manifest.path_id,
         path_attempt=manifest.path_attempt,
         phase=header.phase,
         token_index=header.token_index,
         hop_index=header.hop_index,
         placement_id=hop.placement_id,
         qos_class=request.qos_class,
         deficit_ratio=0.0,
         enqueued_at=now,
         idempotency_key=header.idempotency_key,
         payload=payload,
         prefill_chunk_token_count=header.prefill_chunk_token_count,
         batch_key=self._runtime_batch_key(
            graph=graph,
            placement=placement,
            phase=header.phase,
            token_span=(
               1
               if header.phase == "DECODE"
               else header.prefill_chunk_token_count
            ),
         ),
      )
      try:
         self.scheduler.enqueue(work)
      except BackpressureError as error:
         return HopReceiveResult(
            "REJECTED",
            reason=f"backpressure:{error.reason}",
            retry_after_seconds=error.retry_after_seconds,
         )
      selected = self.scheduler.pop_next(now=now)
      state.transition("ACCEPTED", path_attempt=header.path_attempt)
      state.transition("EXECUTING", path_attempt=header.path_attempt)
      runtime_result = self.runtime.execute(selected)
      if not runtime_result.success:
         state.transition("FAILED", path_attempt=header.path_attempt)
         report = FailureReport(
            request_id=request.request_id,
            path_id=manifest.path_id,
            path_attempt=manifest.path_attempt,
            token_index=header.token_index,
            scope=runtime_result.failure_scope or "PLACEMENT",
            reason=runtime_result.failure_reason or "runtime_failure",
            placement_id=hop.placement_id,
            node_id=placement.node_id,
         )
         self.transport.send_failure_report(report)
         result = HopReceiveResult(
            "FAILED",
            reason=report.reason,
            failure_report=report,
         )
         self._remember_hop(header, result)
         return result

      if header.hop_index + 1 < len(manifest.ordered_hops):
         next_hop = manifest.ordered_hops[header.hop_index + 1]
         next_header = HopHeader(
            request_id=request.request_id,
            path_id=manifest.path_id,
            path_attempt=manifest.path_attempt,
            phase=header.phase,
            token_index=header.token_index,
            hop_index=header.hop_index + 1,
            source_placement_id=hop.placement_id,
            destination_placement_id=next_hop.placement_id,
            topology_version=manifest.topology_version,
            idempotency_key=hop_idempotency_key(
               request_id=request.request_id,
               path_id=manifest.path_id,
               path_attempt=manifest.path_attempt,
               phase=header.phase,
               token_index=header.token_index,
               hop_index=header.hop_index + 1,
            ),
            prefill_chunk_token_count=header.prefill_chunk_token_count,
         )
         self.transport.send_hop(next_header, runtime_result.payload)
         state.transition("FORWARDED", path_attempt=header.path_attempt)
         result = HopReceiveResult(
            "FORWARDED",
            forwarded_header=next_header,
         )
         self._remember_hop(header, result)
         return result

      if header.phase == "PREFILL_CHUNK":
         event = PrefillChunkCompleted(
            request_id=request.request_id,
            path_id=manifest.path_id,
            path_attempt=manifest.path_attempt,
            chunk_index=header.token_index,
            token_count=header.prefill_chunk_token_count,
         )
         self.transport.send_prefill_chunk_completed(event)
         state.transition("FORWARDED", path_attempt=header.path_attempt)
         result = HopReceiveResult(
            "COMPLETED",
            prefill_chunk_completed=event,
         )
         self._remember_hop(header, result)
         return result
      if header.phase != "DECODE":
         state.transition("FORWARDED", path_attempt=header.path_attempt)
         result = HopReceiveResult("COMPLETED")
         self._remember_hop(header, result)
         return result
      if runtime_result.token_id is None:
         state.transition("FAILED", path_attempt=header.path_attempt)
         report = FailureReport(
            request_id=request.request_id,
            path_id=manifest.path_id,
            path_attempt=manifest.path_attempt,
            token_index=header.token_index,
            scope="PLACEMENT",
            reason="final_stage_missing_token",
            placement_id=hop.placement_id,
            node_id=placement.node_id,
         )
         self.transport.send_failure_report(report)
         result = HopReceiveResult(
            "FAILED",
            reason=report.reason,
            failure_report=report,
         )
         self._remember_hop(header, result)
         return result
      event = TokenEvent(
         request_id=request.request_id,
         path_id=manifest.path_id,
         path_attempt=manifest.path_attempt,
         token_index=header.token_index,
         token_id=runtime_result.token_id,
         sampling_counter=header.token_index + 1,
      )
      self.transport.send_token_event(event)
      state.transition("FORWARDED", path_attempt=header.path_attempt)
      result = HopReceiveResult("COMPLETED", token_event=event)
      self._remember_hop(header, result)
      return result

   def enqueue_hop(self, header: HopHeader, payload: object) -> HopReceiveResult:
      """Validate and queue one hop without blocking for runtime execution.

      Production event loops should call this method for ingress, then call
      ``drain_ready_batches`` immediately and again at ``next_batch_deadline``.
      The synchronous ``receive_hop`` API remains available for compatibility.
      """
      now = self.clock.now()
      self._evict_hop_results(now=now)
      expected_key = hop_idempotency_key(
         request_id=header.request_id,
         path_id=header.path_id,
         path_attempt=header.path_attempt,
         phase=header.phase,
         token_index=header.token_index,
         hop_index=header.hop_index,
      )
      if header.idempotency_key != expected_key:
         return HopReceiveResult("REJECTED", "invalid_idempotency_key")
      cached = self._hop_results.get(header.idempotency_key)
      if cached is not None:
         return cached[0]
      if header.idempotency_key in self._pending_hops:
         return HopReceiveResult("QUEUED", "duplicate_pending")
      registration = self._paths.get(header.path_id)
      if registration is None:
         return HopReceiveResult("REJECTED", "unknown_path")
      graph, manifest, request = registration
      if (
         header.path_attempt != manifest.path_attempt
         or header.request_id != manifest.request_id
      ):
         return HopReceiveResult("REJECTED", "stale_path_attempt")
      if header.topology_version != manifest.topology_version:
         return HopReceiveResult("REJECTED", "topology_version_mismatch")
      if header.hop_index < 0 or header.hop_index >= len(manifest.ordered_hops):
         return HopReceiveResult("REJECTED", "invalid_hop_index")
      hop = manifest.ordered_hops[header.hop_index]
      if header.destination_placement_id != hop.placement_id:
         return HopReceiveResult("REJECTED", "manifest_hop_mismatch")
      if header.hop_index == 0:
         valid_sources = {""}
         if header.phase == "DECODE":
            valid_sources.add(manifest.ordered_hops[-1].placement_id)
      else:
         valid_sources = {
            manifest.ordered_hops[header.hop_index - 1].placement_id
         }
      if header.source_placement_id not in valid_sources:
         return HopReceiveResult("REJECTED", "source_placement_mismatch")
      placement_map = {
         placement.placement_id: placement
         for stage in graph.stages
         for placement in stage.placements
      }
      placement = placement_map[hop.placement_id]
      if placement.node_id != self.node_id:
         return HopReceiveResult("REJECTED", "destination_not_local")

      work = HopWorkItem(
         request_id=request.request_id,
         path_id=manifest.path_id,
         path_attempt=manifest.path_attempt,
         phase=header.phase,
         token_index=header.token_index,
         hop_index=header.hop_index,
         placement_id=hop.placement_id,
         qos_class=request.qos_class,
         deficit_ratio=0.0,
         enqueued_at=now,
         idempotency_key=header.idempotency_key,
         payload=payload,
         prefill_chunk_token_count=header.prefill_chunk_token_count,
         batch_key=self._runtime_batch_key(
            graph=graph,
            placement=placement,
            phase=header.phase,
            token_span=(
               1
               if header.phase == "DECODE"
               else header.prefill_chunk_token_count
            ),
         ),
         deadline_at=self._hop_deadline(request, header.phase, now=now),
      )
      try:
         self.batch_scheduler.enqueue(work)
      except BackpressureError as error:
         return HopReceiveResult(
            "REJECTED",
            reason=f"backpressure:{error.reason}",
            retry_after_seconds=error.retry_after_seconds,
         )
      except DuplicateHopError:
         return HopReceiveResult("QUEUED", "duplicate_pending")
      self._pending_hops[header.idempotency_key] = (header, work)
      return HopReceiveResult("QUEUED")

   def drain_ready_batches(
      self,
      *,
      force: bool = False,
      maximum_batches: int | None = None,
   ) -> tuple[HopReceiveResult, ...]:
      """Execute ready microbatches; never sleeps or owns an event-loop thread."""
      if maximum_batches is not None and maximum_batches <= 0:
         return ()
      completed: list[HopReceiveResult] = []
      drained = 0
      while self.batch_scheduler.queue_depth():
         if maximum_batches is not None and drained >= maximum_batches:
            break
         now = self.clock.now()
         candidates = self.batch_scheduler.batch_candidates(now=now)
         decision = self.batch_controller.decide(
            candidates,
            now=now,
            force=force,
         )
         if decision.action == "WAIT":
            break
         batch = self.batch_scheduler.pop_batch(
            now=now,
            maximum_items=decision.batch_size,
            maximum_bytes=self.scheduler.config.maximum_runtime_batch_bytes,
            decision=decision,
         )
         started_at = self.clock.now()
         runtime_results = tuple(self.runtime.execute_batch(batch))
         execution_ms = max(0.0, self.clock.now() - started_at) * 1_000.0
         if len(runtime_results) != len(batch.items):
            runtime_results = tuple(
               RuntimeResult(
                  success=False,
                  failure_scope="PLACEMENT",
                  failure_reason="runtime_batch_result_count_mismatch",
               )
               for _ in batch.items
            )
         payload_bytes = sum(
            self.batch_scheduler._payload_size(item.payload) for item in batch.items
         )
         self.batch_controller.record_execution(
            BatchExecutionObservation(
               phase=batch.items[0].phase,
               batch_size=len(batch.items),
               payload_bytes=payload_bytes,
               execution_ms=execution_ms,
               successful=all(result.success for result in runtime_results),
            )
         )
         for item, runtime_result in zip(batch.items, runtime_results):
            pending = self._pending_hops.pop(item.idempotency_key, None)
            if pending is None:
               continue
            header, _ = pending
            completed.append(
               self._complete_queued_hop(header, item, runtime_result)
            )
         drained += 1
      return tuple(completed)

   def update_batch_network_stats(
      self,
      placement_id: str,
      stats: BatchNetworkStats,
   ) -> None:
      self.batch_controller.update_network_stats(placement_id, stats)

   def batch_decisions(self):
      return self.batch_controller.decisions()

   def batch_execution_profiles(self):
      return self.batch_controller.execution_profiles()

   def batch_network_stats(self):
      return self.batch_controller.network_stats()

   def pending_batch_hops(self) -> int:
      return self.batch_scheduler.queue_depth()

   def next_batch_deadline(self) -> float | None:
      now = self.clock.now()
      candidates = self.batch_scheduler.batch_candidates(now=now)
      if not candidates:
         return None
      decision = self.batch_controller.decide(candidates, now=now)
      return now if decision.action == "DISPATCH" else decision.ready_at

   def _complete_queued_hop(
      self,
      header: HopHeader,
      item: HopWorkItem,
      runtime_result: RuntimeResult,
   ) -> HopReceiveResult:
      graph, manifest, request = self._paths[item.path_id]
      hop = manifest.ordered_hops[header.hop_index]
      placement = next(
         placement
         for stage in graph.stages
         for placement in stage.placements
         if placement.placement_id == hop.placement_id
      )
      if not runtime_result.success:
         report = FailureReport(
            request_id=request.request_id,
            path_id=manifest.path_id,
            path_attempt=manifest.path_attempt,
            token_index=header.token_index,
            scope=runtime_result.failure_scope or "PLACEMENT",
            reason=runtime_result.failure_reason or "runtime_failure",
            placement_id=hop.placement_id,
            node_id=placement.node_id,
         )
         self.transport.send_failure_report(report)
         result = HopReceiveResult(
            "FAILED",
            reason=report.reason,
            failure_report=report,
         )
         self._remember_hop(header, result)
         return result

      if header.hop_index + 1 < len(manifest.ordered_hops):
         next_hop = manifest.ordered_hops[header.hop_index + 1]
         next_header = HopHeader(
            request_id=request.request_id,
            path_id=manifest.path_id,
            path_attempt=manifest.path_attempt,
            phase=header.phase,
            token_index=header.token_index,
            hop_index=header.hop_index + 1,
            source_placement_id=hop.placement_id,
            destination_placement_id=next_hop.placement_id,
            topology_version=manifest.topology_version,
            idempotency_key=hop_idempotency_key(
               request_id=request.request_id,
               path_id=manifest.path_id,
               path_attempt=manifest.path_attempt,
               phase=header.phase,
               token_index=header.token_index,
               hop_index=header.hop_index + 1,
            ),
            prefill_chunk_token_count=header.prefill_chunk_token_count,
         )
         self.transport.send_hop(next_header, runtime_result.payload)
         result = HopReceiveResult("FORWARDED", forwarded_header=next_header)
         self._remember_hop(header, result)
         return result

      if header.phase == "PREFILL_CHUNK":
         event = PrefillChunkCompleted(
            request_id=request.request_id,
            path_id=manifest.path_id,
            path_attempt=manifest.path_attempt,
            chunk_index=header.token_index,
            token_count=header.prefill_chunk_token_count,
         )
         self.transport.send_prefill_chunk_completed(event)
         result = HopReceiveResult(
            "COMPLETED",
            prefill_chunk_completed=event,
         )
         self._remember_hop(header, result)
         return result
      if header.phase != "DECODE":
         result = HopReceiveResult("COMPLETED")
         self._remember_hop(header, result)
         return result
      if runtime_result.token_id is None:
         report = FailureReport(
            request_id=request.request_id,
            path_id=manifest.path_id,
            path_attempt=manifest.path_attempt,
            token_index=header.token_index,
            scope="PLACEMENT",
            reason="final_stage_missing_token",
            placement_id=hop.placement_id,
            node_id=placement.node_id,
         )
         self.transport.send_failure_report(report)
         result = HopReceiveResult(
            "FAILED",
            reason=report.reason,
            failure_report=report,
         )
         self._remember_hop(header, result)
         return result
      event = TokenEvent(
         request_id=request.request_id,
         path_id=manifest.path_id,
         path_attempt=manifest.path_attempt,
         token_index=header.token_index,
         token_id=runtime_result.token_id,
         sampling_counter=header.token_index + 1,
      )
      self.transport.send_token_event(event)
      result = HopReceiveResult("COMPLETED", token_event=event)
      self._remember_hop(header, result)
      return result

   @staticmethod
   def _hop_deadline(
      request: RequestContext,
      phase: str,
      *,
      now: float,
   ) -> float:
      if phase in {"PREFILL", "PREFILL_CHUNK"}:
         return request.admitted_at + max(0.0, request.target_ttft_ms) / 1_000.0
      if phase == "DECODE":
         return now + max(0.0, request.target_tpot_ms) / 1_000.0
      return float("inf")

   @staticmethod
   def _runtime_batch_key(
      *,
      graph: ExecutionGraph,
      placement,
      phase: str,
      token_span: int,
   ) -> RuntimeBatchKey | None:
      if phase not in {"PREFILL", "PREFILL_CHUNK", "DECODE"}:
         return None
      if token_span <= 0:
         return None
      return RuntimeBatchKey(
         deployment_id=graph.deployment_id,
         deployment_epoch=graph.deployment_epoch,
         model_commit=graph.resolved_commit,
         manifest_digest=graph.manifest_digest,
         placement_id=placement.placement_id,
         assignment_id=placement.assignment_id,
         stage_signature=placement.stage_signature,
         load_proof_digest=placement.load_proof_digest,
         runtime_backend=placement.runtime_backend,
         phase=phase,
         hidden_size=graph.hidden_size,
         activation_bytes=graph.activation_bytes,
         token_span=token_span,
      )

   def execute_manifest(
      self,
      *,
      graph: ExecutionGraph,
      manifest: PathManifest,
      request: RequestContext,
      phase: str,
      token_index: int,
      payload: object,
   ) -> RelayOutcome:
      execution_key = (
         manifest.path_id,
         manifest.path_attempt,
         phase,
         token_index,
      )
      now = self.clock.now()
      self._evict_outcomes(now=now)
      cached = self._outcomes.get(execution_key)
      if cached is not None:
         return cached[0]
      placement_map = {
         placement.placement_id: placement
         for stage in graph.stages
         for placement in stage.placements
      }
      edge_map = {
         (edge.from_placement_id, edge.to_placement_id): edge
         for edge in graph.edges
      }
      current_payload = payload
      final_result = None
      for hop_index, hop in enumerate(manifest.ordered_hops):
         work = HopWorkItem(
            request_id=request.request_id,
            path_id=manifest.path_id,
            path_attempt=manifest.path_attempt,
            phase=phase,
            token_index=token_index,
            hop_index=hop_index,
            placement_id=hop.placement_id,
            qos_class=request.qos_class,
            deficit_ratio=0.0,
            enqueued_at=self.clock.now(),
            idempotency_key=hop_idempotency_key(
               request_id=request.request_id,
               path_id=manifest.path_id,
               path_attempt=manifest.path_attempt,
               phase=phase,
               token_index=token_index,
               hop_index=hop_index,
            ),
            payload=current_payload,
            batch_key=self._runtime_batch_key(
               graph=graph,
               placement=placement_map[hop.placement_id],
               phase=phase,
               token_span=(
                  1
                  if phase == "DECODE"
                  else len(request.prompt_token_ids)
                  if phase == "PREFILL"
                  else 0
               ),
            ),
         )
         self.scheduler.enqueue(work)
         selected = self.scheduler.pop_next(now=self.clock.now())
         result = self.runtime.execute(selected)
         if not result.success:
            placement = placement_map[hop.placement_id]
            edge_id = ""
            if hop_index:
               previous = manifest.ordered_hops[hop_index - 1].placement_id
               edge = edge_map.get((previous, hop.placement_id))
               edge_id = edge.edge_id if edge is not None else ""
            report = FailureReport(
               request_id=request.request_id,
               path_id=manifest.path_id,
               path_attempt=manifest.path_attempt,
               token_index=token_index,
               scope=result.failure_scope or "PLACEMENT",
               reason=result.failure_reason or "runtime_failure",
               placement_id=hop.placement_id,
               edge_id=edge_id,
               node_id=placement.node_id,
            )
            self.transport.send_failure_report(report)
            outcome = RelayOutcome(failure_report=report)
            self._remember(execution_key, outcome, manifest.path_id)
            return outcome
         current_payload = result.payload
         final_result = result
         if hop_index + 1 < len(manifest.ordered_hops):
            next_hop = manifest.ordered_hops[hop_index + 1]
            header = HopHeader(
               request_id=request.request_id,
               path_id=manifest.path_id,
               path_attempt=manifest.path_attempt,
               phase=phase,
               token_index=token_index,
               hop_index=hop_index + 1,
               source_placement_id=hop.placement_id,
               destination_placement_id=next_hop.placement_id,
               topology_version=manifest.topology_version,
               idempotency_key=hop_idempotency_key(
                  request_id=request.request_id,
                  path_id=manifest.path_id,
                  path_attempt=manifest.path_attempt,
                  phase=phase,
                  token_index=token_index,
                  hop_index=hop_index + 1,
               ),
            )
            self.transport.send_hop(header, current_payload)

      if phase != "DECODE":
         outcome = RelayOutcome()
         self._remember(execution_key, outcome, manifest.path_id)
         return outcome
      if final_result is None or final_result.token_id is None:
         report = FailureReport(
            request_id=request.request_id,
            path_id=manifest.path_id,
            path_attempt=manifest.path_attempt,
            token_index=token_index,
            scope="PLACEMENT",
            reason="final_stage_missing_token",
            placement_id=manifest.ordered_hops[-1].placement_id,
         )
         self.transport.send_failure_report(report)
         outcome = RelayOutcome(failure_report=report)
         self._remember(execution_key, outcome, manifest.path_id)
         return outcome

      event = TokenEvent(
         request_id=request.request_id,
         path_id=manifest.path_id,
         path_attempt=manifest.path_attempt,
         token_index=token_index,
         token_id=final_result.token_id,
         sampling_counter=token_index + 1,
      )
      self.transport.send_token_event(event)
      final_hop = manifest.ordered_hops[-1]
      first_hop = manifest.ordered_hops[0]
      loopback_header = HopHeader(
         request_id=request.request_id,
         path_id=manifest.path_id,
         path_attempt=manifest.path_attempt,
         phase="LOOPBACK",
         token_index=token_index,
         hop_index=0,
         source_placement_id=final_hop.placement_id,
         destination_placement_id=first_hop.placement_id,
         topology_version=manifest.topology_version,
         idempotency_key=hop_idempotency_key(
            request_id=request.request_id,
            path_id=manifest.path_id,
            path_attempt=manifest.path_attempt,
            phase="LOOPBACK",
            token_index=token_index,
            hop_index=0,
         ),
      )
      self.transport.send_hop(loopback_header, event.token_id)
      outcome = RelayOutcome(token_event=event)
      self._remember(execution_key, outcome, manifest.path_id)
      return outcome

   def release_path(self, path_id: str) -> None:
      self._outcomes = {
         key: value
         for key, value in self._outcomes.items()
         if value[2] != path_id
      }
      self._hop_results = {
         key: value
         for key, value in self._hop_results.items()
         if value[2] != path_id
      }
      self._prefill_results = {
         key: value
         for key, value in self._prefill_results.items()
         if value[2] != path_id
      }
      self._paths.pop(path_id, None)
      self._entry_node_by_path.pop(path_id, None)
      self._pending_hops = {
         key: value
         for key, value in self._pending_hops.items()
         if value[1].path_id != path_id
      }
      self.scheduler.release_path(path_id)
      self.batch_scheduler.release_path(path_id)

   def cached_outcome_count(self) -> int:
      return len(self._outcomes)

   def _remember_prefill(
      self,
      header: HopHeader,
      result: ProgressivePrefillResult,
   ) -> None:
      self._prefill_results[header.idempotency_key] = (
         result,
         self.clock.now(),
         header.path_id,
      )
      self._evict_prefill_results(now=self.clock.now())

   def _evict_prefill_results(self, *, now: float) -> None:
      retention = max(
         0.0,
         self.scheduler.config.idempotency_retention_seconds,
      )
      for key, (_, created_at, _) in tuple(self._prefill_results.items()):
         if now - created_at > retention:
            self._prefill_results.pop(key, None)
      maximum = max(
         1,
         self.scheduler.config.maximum_idempotency_entries,
      )
      oldest = sorted(
         (created_at, key)
         for key, (_, created_at, _) in self._prefill_results.items()
      )
      while len(self._prefill_results) > maximum:
         _, key = oldest.pop(0)
         self._prefill_results.pop(key, None)

   def _remember_hop(
      self,
      header: HopHeader,
      result: HopReceiveResult,
   ) -> None:
      self._hop_results[header.idempotency_key] = (
         result,
         self.clock.now(),
         header.path_id,
      )
      self._evict_hop_results(now=self.clock.now())

   def _evict_hop_results(self, *, now: float) -> None:
      retention = max(
         0.0,
         self.scheduler.config.idempotency_retention_seconds,
      )
      for key, (_, created_at, _) in tuple(self._hop_results.items()):
         if now - created_at > retention:
            self._hop_results.pop(key, None)
      maximum = max(
         1,
         self.scheduler.config.maximum_idempotency_entries,
      )
      oldest = sorted(
         (created_at, key)
         for key, (_, created_at, _) in self._hop_results.items()
      )
      while len(self._hop_results) > maximum:
         _, key = oldest.pop(0)
         self._hop_results.pop(key, None)

   def _remember(
      self,
      key: tuple[str, int, str, int],
      outcome: RelayOutcome,
      path_id: str,
   ) -> None:
      self._outcomes[key] = (outcome, self.clock.now(), path_id)
      self._evict_outcomes(now=self.clock.now())

   def _evict_outcomes(self, *, now: float) -> None:
      retention = max(
         0.0,
         self.scheduler.config.idempotency_retention_seconds,
      )
      expired = [
         key
         for key, (_, created_at, _) in self._outcomes.items()
         if now - created_at > retention
      ]
      for key in expired:
         self._outcomes.pop(key, None)

      maximum = max(
         1,
         self.scheduler.config.maximum_idempotency_entries,
      )
      oldest = sorted(
         (created_at, key)
         for key, (_, created_at, _) in self._outcomes.items()
      )
      while len(self._outcomes) > maximum:
         _, key = oldest.pop(0)
         self._outcomes.pop(key, None)
