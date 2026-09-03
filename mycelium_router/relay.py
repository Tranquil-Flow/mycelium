"""Inter-layer relay execution over an already locked path manifest."""

from collections import OrderedDict
from contextlib import contextmanager
import hashlib
from threading import Lock, RLock
from typing import Iterator

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
   PathCancellation,
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
from mycelium_router.idempotency import (
   hop_idempotency_key,
   payload_fingerprint,
   snapshot_payload,
)
from mycelium_router.routing import RoutingError
from mycelium_router.scheduler import (
   BackpressureError,
   DuplicateHopError,
   HopScheduler,
)
from mycelium_router.state import HopStateMachine
from mycelium_router.validation import validate_manifest


_MAX_TERMINAL_PATH_METADATA = 4096
_MAX_PENDING_PATH_CANCELLATIONS = 4096
_CANCELLED_PATH_FILTER_BYTES = 1 << 20
_CANCELLED_PATH_FILTER_HASHES = 5
_CANCELLED_PATH_FILTER_ROTATION_CAPACITY = 100_000


class _RotatingReplayFilter:
   """Bounded two-window replay filter with a stable false-positive rate."""

   def __init__(
      self,
      *,
      byte_count: int,
      hashes: int,
      rotation_capacity: int,
   ) -> None:
      if byte_count <= 0 or hashes <= 0 or rotation_capacity <= 0:
         raise ValueError("invalid_replay_filter_configuration")
      self._byte_count = byte_count
      self._hashes = hashes
      self._rotation_capacity = rotation_capacity
      self._current = bytearray(byte_count)
      self._previous = bytearray(byte_count)
      self._current_insertions = 0

   def _positions(self, path_id: str, path_attempt: int) -> tuple[int, ...]:
      digest = hashlib.blake2b(
         f"{path_id}\x00{path_attempt}".encode("utf-8"),
         digest_size=16,
      ).digest()
      first = int.from_bytes(digest[:8], "big")
      second = int.from_bytes(digest[8:], "big") | 1
      bit_count = self._byte_count * 8
      return tuple(
         (first + index * second) % bit_count
         for index in range(self._hashes)
      )

   @staticmethod
   def _contains(window: bytearray, positions: tuple[int, ...]) -> bool:
      return all(
         window[position // 8] & (1 << (position % 8))
         for position in positions
      )

   def add(self, path_id: str, path_attempt: int) -> None:
      if self._current_insertions >= self._rotation_capacity:
         self._previous = self._current
         self._current = bytearray(self._byte_count)
         self._current_insertions = 0
      for position in self._positions(path_id, path_attempt):
         self._current[position // 8] |= 1 << (position % 8)
      self._current_insertions += 1

   def contains(self, path_id: str, path_attempt: int) -> bool:
      positions = self._positions(path_id, path_attempt)
      return self._contains(self._current, positions) or self._contains(
         self._previous,
         positions,
      )


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
      self.decode_mode = getattr(runtime, "decode_mode", "complete_context_replay")
      if self.decode_mode not in {"complete_context_replay", "stage_local_kv"}:
         raise ValueError("invalid_runtime_decode_mode")
      self.transport = transport
      self.scheduler = scheduler
      self.batch_scheduler = HopScheduler(scheduler.config)
      self.batch_controller = PhaseAwareBatchController(scheduler.config)
      self.clock = clock
      self.builder = builder
      self.device_states = device_states
      self._outcomes: dict[
         tuple[str, int, str, int],
         tuple[RelayOutcome, float, str, str],
      ] = {}
      self._paths: dict[
         str,
         tuple[ExecutionGraph, PathManifest, RequestContext],
      ] = {}
      self._path_lock = RLock()
      # Operations for one path must serialize, but unrelated paths must never
      # share a lock: cancellation owns a fixed two-second proof budget and
      # cannot wait behind a slow request that merely hashes to the same stripe.
      # References include holders and waiters, so an idle entry can be removed
      # without replacing the lock while another thread is about to acquire it.
      self._path_operation_registry_lock = Lock()
      self._path_operation_locks: dict[str, tuple[Lock, int]] = {}
      self._path_generations: OrderedDict[str, int] = OrderedDict()
      self._cancelled_path_attempts: OrderedDict[str, int] = OrderedDict()
      self._cancelled_path_filter = _RotatingReplayFilter(
         byte_count=_CANCELLED_PATH_FILTER_BYTES,
         hashes=_CANCELLED_PATH_FILTER_HASHES,
         rotation_capacity=_CANCELLED_PATH_FILTER_ROTATION_CAPACITY,
      )
      self._pending_path_cancellations: OrderedDict[
         tuple[str, str, int, int, str],
         None,
      ] = OrderedDict()
      self._provisional_paths: dict[
         str,
         tuple[str, int, int, str | None],
      ] = {}
      self._entry_node_by_path: dict[str, str] = {}
      self._hop_results: dict[
         str,
         tuple[HopReceiveResult, float, str, str],
      ] = {}
      self._prefill_results: dict[
         str,
         tuple[ProgressivePrefillResult, float, str, str],
      ] = {}
      self._pending_hops: dict[
         str,
         tuple[HopHeader, HopWorkItem, str, int],
      ] = {}

   def path_generation(self, path_id: str) -> int:
      """Return the generation used to fence entry-side dispatches."""
      with self._path_lock:
         return self._path_generations.get(path_id, 0)

   def _path_generation(self, path_id: str) -> int:
      return self.path_generation(path_id)

   def _prune_terminal_path_metadata_locked(self) -> None:
      while len(self._cancelled_path_attempts) > _MAX_TERMINAL_PATH_METADATA:
         path_id, _ = self._cancelled_path_attempts.popitem(last=False)
         if path_id not in self._paths and path_id not in self._provisional_paths:
            self._path_generations.pop(path_id, None)

   @contextmanager
   def _path_operation_lock(self, path_id: str) -> Iterator[None]:
      with self._path_operation_registry_lock:
         registered = self._path_operation_locks.get(path_id)
         if registered is None:
            operation_lock = Lock()
            references = 0
         else:
            operation_lock, references = registered
         self._path_operation_locks[path_id] = (operation_lock, references + 1)
      try:
         with operation_lock:
            yield
      finally:
         with self._path_operation_registry_lock:
            current_lock, current_references = self._path_operation_locks[path_id]
            assert current_lock is operation_lock and current_references >= 1
            if current_references == 1:
               self._path_operation_locks.pop(path_id)
            else:
               self._path_operation_locks[path_id] = (
                  operation_lock,
                  current_references - 1,
               )

   def _mark_cancelled_attempt_locked(self, path_id: str, path_attempt: int) -> None:
      self._cancelled_path_filter.add(path_id, path_attempt)

   def _was_cancelled_attempt_locked(self, path_id: str, path_attempt: int) -> bool:
      return self._cancelled_path_filter.contains(path_id, path_attempt)

   @staticmethod
   def _pending_cancellation_key(
      *,
      request_id: str,
      path_id: str,
      path_attempt: int,
      topology_version: int,
      entry_node_id: str,
   ) -> tuple[str, str, int, int, str]:
      return (
         path_id,
         request_id,
         path_attempt,
         topology_version,
         entry_node_id,
      )

   def _remember_pending_cancellation_locked(
      self,
      cancellation: PathCancellation,
      source_node_id: str,
   ) -> None:
      key = self._pending_cancellation_key(
         request_id=cancellation.request_id,
         path_id=cancellation.path_id,
         path_attempt=cancellation.path_attempt,
         topology_version=cancellation.topology_version,
         entry_node_id=source_node_id,
      )
      self._pending_path_cancellations[key] = None
      self._pending_path_cancellations.move_to_end(key)
      while len(self._pending_path_cancellations) > _MAX_PENDING_PATH_CANCELLATIONS:
         self._pending_path_cancellations.popitem(last=False)

   def _consume_pending_cancellation_locked(
      self,
      *,
      request_id: str,
      path_id: str,
      path_attempt: int,
      topology_version: int,
      entry_node_id: str | None,
   ) -> bool:
      if entry_node_id is None:
         return False
      expected = self._pending_cancellation_key(
         request_id=request_id,
         path_id=path_id,
         path_attempt=path_attempt,
         topology_version=topology_version,
         entry_node_id=entry_node_id,
      )
      path_keys = tuple(
         key for key in self._pending_path_cancellations if key[0] == path_id
      )
      matched = expected in self._pending_path_cancellations
      for key in path_keys:
         self._pending_path_cancellations.pop(key, None)
      if not matched:
         return False
      self._mark_cancelled_attempt_locked(path_id, path_attempt)
      self._cancelled_path_attempts[path_id] = max(
         path_attempt,
         self._cancelled_path_attempts.get(path_id, path_attempt),
      )
      self._cancelled_path_attempts.move_to_end(path_id)
      self._path_generations[path_id] = self._path_generations.get(path_id, 0) + 1
      self._path_generations.move_to_end(path_id)
      self._prune_terminal_path_metadata_locked()
      return True

   def _path_is_current_locked(
      self,
      path_id: str,
      path_attempt: int,
      generation: int,
   ) -> bool:
      if self._path_generations.get(path_id, 0) != generation:
         return False
      if self._was_cancelled_attempt_locked(path_id, path_attempt):
         return False
      cancelled_attempt = self._cancelled_path_attempts.get(path_id)
      return cancelled_attempt is None or path_attempt > cancelled_attempt

   def _send_if_path_current(
      self,
      path_id: str,
      path_attempt: int,
      generation: int,
      sender,
   ) -> bool:
      with self._path_lock:
         if not self._path_is_current_locked(
            path_id,
            path_attempt,
            generation,
         ):
            return False
      # Acquiring this generation-checked permit is the dispatch linearization
      # point. Cancellation invalidates the generation, preventing every
      # dispatch that has not acquired a permit yet. Already-permitted work is
      # in flight and cannot be retracted from an arbitrary transport.
      sender()
      return True

   def dispatch_if_current(
      self,
      *,
      path_id: str,
      path_attempt: int,
      generation: int,
      sender,
   ) -> bool:
      """Dispatch only if cancellation has not invalidated the path permit."""
      return self._send_if_path_current(
         path_id,
         path_attempt,
         generation,
         sender,
      )

   @staticmethod
   def _path_cancelled_outcome(
      request: RequestContext,
      manifest: PathManifest,
      token_index: int,
   ) -> RelayOutcome:
      return RelayOutcome(
         failure_report=FailureReport(
            request_id=request.request_id,
            path_id=manifest.path_id,
            path_attempt=manifest.path_attempt,
            token_index=token_index,
            scope="REQUEST",
            reason="path_cancelled",
         )
      )

   @staticmethod
   def _runtime_unavailable() -> RuntimeResult:
      return RuntimeResult(
         success=False,
         failure_scope="PLACEMENT",
         failure_reason="worker_runtime_unavailable",
      )

   def _execute_runtime(self, item: HopWorkItem) -> RuntimeResult:
      try:
         return self.runtime.execute(item)
      except Exception:
         return self._runtime_unavailable()

   def _execute_runtime_batch(self, batch) -> tuple[RuntimeResult, ...]:
      try:
         return tuple(self.runtime.execute_batch(batch))
      except Exception:
         return tuple(self._runtime_unavailable() for _ in batch.items)

   def _kv_position(self, request: RequestContext, phase: str, token_index: int) -> int:
      if phase in {"PREFILL", "RECOVERY_PREFILL"}:
         return 0
      if phase == "PREFILL_CHUNK":
         size = self.scheduler.config.prefill_chunk_size_tokens
         return 0 if size <= 0 else token_index * size
      if phase == "DECODE":
         return len(request.prompt_token_ids) + token_index - 1
      return 0

   def _is_terminal(
      self,
      request: RequestContext,
      phase: str,
      token_index: int,
      prefill_chunk_token_count: int = 0,
   ) -> bool:
      if phase == "PREFILL":
         return request.max_new_tokens == 1
      if phase == "PREFILL_CHUNK":
         return (
            self._kv_position(request, phase, token_index)
            + prefill_chunk_token_count
            >= len(request.prompt_token_ids)
            and request.max_new_tokens == 1
         )
      if phase == "RECOVERY_PREFILL":
         return token_index + 1 >= request.max_new_tokens
      if phase == "DECODE":
         return token_index + 1 >= request.max_new_tokens
      return False

   def register_path(
      self,
      request: RequestContext,
      manifest: PathManifest,
      graph: ExecutionGraph,
      *,
      source_node_id: str | None = None,
      entry_node_id: str | None = None,
   ) -> bool:
      return self.register_path_with_generation(
         request,
         manifest,
         graph,
         source_node_id=source_node_id,
         entry_node_id=entry_node_id,
      ) is not None

   def register_path_with_generation(
      self,
      request: RequestContext,
      manifest: PathManifest,
      graph: ExecutionGraph,
      *,
      source_node_id: str | None = None,
      entry_node_id: str | None = None,
   ) -> int | None:
      with self._path_operation_lock(manifest.path_id):
         validate_manifest(manifest, graph)
         if request.request_id != manifest.request_id:
            return None
         if source_node_id is not None:
            placement_map = {
               placement.placement_id: placement
               for stage in graph.stages
               for placement in stage.placements
            }
            final_placement = placement_map[manifest.ordered_hops[-1].placement_id]
            if not source_node_id or final_placement.node_id != source_node_id:
               return None
            if not entry_node_id:
               return None
         with self._path_lock:
            if self._consume_pending_cancellation_locked(
               request_id=request.request_id,
               path_id=manifest.path_id,
               path_attempt=manifest.path_attempt,
               topology_version=graph.topology_version,
               entry_node_id=entry_node_id,
            ):
               return None
            if self._was_cancelled_attempt_locked(
               manifest.path_id,
               manifest.path_attempt,
            ):
               return None
            cancelled_attempt = self._cancelled_path_attempts.get(manifest.path_id)
            if (
               cancelled_attempt is not None
               and manifest.path_attempt <= cancelled_attempt
            ):
               return None
            existing = self._paths.get(manifest.path_id)
            provisional = self._provisional_paths.get(manifest.path_id)
            if provisional is not None:
               if provisional[:3] != (
                  request.request_id,
                  manifest.path_attempt,
                  graph.topology_version,
               ):
                  return None
               provisional_entry = provisional[3]
               if (
                  entry_node_id is not None
                  and provisional_entry is not None
                  and entry_node_id != provisional_entry
               ):
                  return None
               if entry_node_id is None:
                  entry_node_id = provisional_entry
            if existing is not None:
               existing_manifest = existing[1]
               if manifest.path_attempt < existing_manifest.path_attempt:
                  return None
               if manifest.path_attempt == existing_manifest.path_attempt:
                  if existing != (graph, manifest, request):
                     return None
                  existing_entry = self._entry_node_by_path.get(manifest.path_id)
                  if (
                     entry_node_id is not None
                     and existing_entry is not None
                     and entry_node_id != existing_entry
                  ):
                     return None
                  if entry_node_id is not None:
                     self._entry_node_by_path[manifest.path_id] = entry_node_id
                  return self._path_generations[manifest.path_id]
               existing_entry = self._entry_node_by_path.get(manifest.path_id)
               if (
                  entry_node_id is not None
                  and existing_entry is not None
                  and entry_node_id != existing_entry
               ):
                  return None
               self._mark_cancelled_attempt_locked(
                  manifest.path_id,
                  existing_manifest.path_attempt,
               )
               self._cancelled_path_attempts[manifest.path_id] = max(
                  existing_manifest.path_attempt,
                  self._cancelled_path_attempts.get(
                     manifest.path_id,
                     existing_manifest.path_attempt,
                  ),
               )
            self._paths[manifest.path_id] = (graph, manifest, request)
            self._provisional_paths.pop(manifest.path_id, None)
            self._path_generations[manifest.path_id] = (
               self._path_generations.get(manifest.path_id, 0) + 1
            )
            self._path_generations.move_to_end(manifest.path_id)
            if manifest.path_id in self._cancelled_path_attempts:
               self._cancelled_path_attempts.move_to_end(manifest.path_id)
            self._prune_terminal_path_metadata_locked()
            if entry_node_id is not None:
               self._entry_node_by_path[manifest.path_id] = entry_node_id
            return self._path_generations[manifest.path_id]

   def _register_provisional_path(
      self,
      *,
      request_id: str,
      path_id: str,
      path_attempt: int,
      topology_version: int,
      entry_node_id: str | None,
   ) -> int | None:
      identity = (
         request_id,
         path_attempt,
         topology_version,
         entry_node_id,
      )
      with self._path_operation_lock(path_id):
         with self._path_lock:
            if self._consume_pending_cancellation_locked(
               request_id=request_id,
               path_id=path_id,
               path_attempt=path_attempt,
               topology_version=topology_version,
               entry_node_id=entry_node_id,
            ):
               return None
            if self._was_cancelled_attempt_locked(path_id, path_attempt):
               return None
            cancelled_attempt = self._cancelled_path_attempts.get(path_id)
            if cancelled_attempt is not None and path_attempt <= cancelled_attempt:
               return None
            existing = self._provisional_paths.get(path_id)
            if existing is not None:
               if existing[:3] != identity[:3]:
                  return None
               if (
                  existing[3] is not None
                  and entry_node_id is not None
                  and existing[3] != entry_node_id
               ):
                  return None
               return self._path_generations[path_id]
            self._provisional_paths[path_id] = identity
            self._path_generations[path_id] = (
               self._path_generations.get(path_id, 0) + 1
            )
            self._path_generations.move_to_end(path_id)
            return self._path_generations[path_id]

   def receive_progressive_prefill(
      self,
      header: HopHeader,
      context: ProgressivePrefillContext,
      *,
      source_node_id: str | None = None,
      entry_node_id: str | None = None,
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
      if (
         source_node_id is not None
         and header.hop_index == 0
         and entry_node_id != source_node_id
      ):
         return ProgressivePrefillResult("REJECTED", "entry_node_mismatch")
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
      provisional_entry = entry_node_id
      if provisional_entry is None and header.hop_index == 0:
         provisional_entry = source_node_id
      generation = self._register_provisional_path(
         request_id=context.request.request_id,
         path_id=build.path_id,
         path_attempt=build.path_attempt,
         topology_version=context.graph.topology_version,
         entry_node_id=provisional_entry,
      )
      if generation is None:
         return ProgressivePrefillResult("REJECTED", "cancelled_path")
      try:
         accepted_payload = snapshot_payload(context.payload)
         payload_digest = payload_fingerprint(accepted_payload)
      except TypeError:
         return ProgressivePrefillResult("REJECTED", "unsupported_payload_type")
      cached = self._prefill_results.get(header.idempotency_key)
      if cached is not None:
         if cached[3] != payload_digest:
            return ProgressivePrefillResult(
               "REJECTED",
               "idempotency_payload_mismatch",
            )
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
         payload=accepted_payload,
         prefill_chunk_token_count=header.prefill_chunk_token_count,
         position=0,
         terminal=self._is_terminal(context.request, "PREFILL", -1),
         lease_expires_at=hop.reservation_expires_at,
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
      with self._path_lock:
         if not self._path_is_current_locked(
            build.path_id,
            build.path_attempt,
            generation,
         ):
            return ProgressivePrefillResult("REJECTED", "cancelled_path")
      selected = self.scheduler.pop_next(now=now)
      state.transition("ACCEPTED", path_attempt=header.path_attempt)
      state.transition("EXECUTING", path_attempt=header.path_attempt)
      with self._path_lock:
         if not self._path_is_current_locked(
            build.path_id,
            build.path_attempt,
            generation,
         ):
            return ProgressivePrefillResult("REJECTED", "cancelled_path")
      runtime_result = self._execute_runtime(selected)
      with self._path_lock:
         if not self._path_is_current_locked(
            build.path_id,
            build.path_attempt,
            generation,
         ):
            return ProgressivePrefillResult("REJECTED", "cancelled_path")
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
         if not self._send_if_path_current(
            build.path_id,
            build.path_attempt,
            generation,
            lambda: self.transport.send_failure_report(report),
         ):
            return ProgressivePrefillResult("REJECTED", "cancelled_path")
         result = ProgressivePrefillResult(
            "FAILED",
            reason=report.reason,
            failure_report=report,
         )
         self.release_path(build.path_id, path_attempt=build.path_attempt)
         self._remember_prefill(header, result, payload_digest, generation)
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
            if not self._send_if_path_current(
               build.path_id,
               build.path_attempt,
               generation,
               lambda: self.transport.send_failure_report(report),
            ):
               return ProgressivePrefillResult("REJECTED", "cancelled_path")
            result = ProgressivePrefillResult(
               "FAILED",
               reason=report.reason,
               failure_report=report,
            )
            self.release_path(build.path_id, path_attempt=build.path_attempt)
            self._remember_prefill(header, result, payload_digest, generation)
            return result
         confirmation = ManifestLocked(
            request_id=context.request.request_id,
            path_id=build.path_id,
            path_attempt=build.path_attempt,
            manifest=manifest,
            build=build,
         )
         generation = self.register_path_with_generation(
            context.request,
            manifest,
            context.graph,
            entry_node_id=provisional_entry,
         )
         if generation is None:
            return ProgressivePrefillResult("REJECTED", "cancelled_path")
         if (
            self.decode_mode == "stage_local_kv"
            and runtime_result.token_id is None
         ):
            state.transition("FAILED", path_attempt=header.path_attempt)
            report = FailureReport(
               request_id=context.request.request_id,
               path_id=build.path_id,
               path_attempt=build.path_attempt,
               token_index=-1,
               scope="PLACEMENT",
               reason="final_stage_missing_token",
               placement_id=hop.placement_id,
               node_id=placement.node_id,
            )
            if not self._send_if_path_current(
               build.path_id,
               build.path_attempt,
               generation,
               lambda: self.transport.send_failure_report(report),
            ):
               return ProgressivePrefillResult("REJECTED", "cancelled_path")
            result = ProgressivePrefillResult(
               "FAILED",
               reason=report.reason,
               failure_report=report,
            )
            self.release_path(build.path_id, path_attempt=build.path_attempt)
            self._remember_prefill(header, result, payload_digest, generation)
            return result
         token_event = (
            TokenEvent(
               request_id=context.request.request_id,
               path_id=build.path_id,
               path_attempt=build.path_attempt,
               token_index=0,
               token_id=runtime_result.token_id,
               sampling_counter=1,
            )
            if runtime_result.token_id is not None
            else None
         )

         def send_locked_result() -> None:
            self.transport.send_manifest_locked(confirmation)
            if token_event is not None:
               self.transport.send_token_event(token_event)

         if not self._send_if_path_current(
            build.path_id,
            build.path_attempt,
            generation,
            send_locked_result,
         ):
            return ProgressivePrefillResult("REJECTED", "cancelled_path")
         state.transition("FORWARDED", path_attempt=header.path_attempt)
         result = ProgressivePrefillResult(
            "LOCKED",
            confirmation=confirmation,
         )
         self._remember_prefill(header, result, payload_digest, generation)
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
         if not self._send_if_path_current(
            build.path_id,
            build.path_attempt,
            generation,
            lambda: self.transport.send_failure_report(report),
         ):
            return ProgressivePrefillResult("REJECTED", "cancelled_path")
         result = ProgressivePrefillResult(
            "FAILED",
            reason=report.reason,
            failure_report=report,
         )
         self.release_path(build.path_id, path_attempt=build.path_attempt)
         self._remember_prefill(header, result, payload_digest, generation)
         return result
      next_index = len(updated.ordered_hops) - 1
      next_hop = updated.ordered_hops[next_index]
      delta = ManifestDelta(
         request_id=context.request.request_id,
         path_id=updated.path_id,
         path_attempt=updated.path_attempt,
         hop_index=next_index,
         hop=next_hop,
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
      def send_progressive_hop() -> None:
         self.transport.send_manifest_delta(delta)
         self.transport.send_hop(next_header, next_context)

      if not self._send_if_path_current(
         build.path_id,
         build.path_attempt,
         generation,
         send_progressive_hop,
      ):
         return ProgressivePrefillResult("REJECTED", "cancelled_path")
      state.transition("FORWARDED", path_attempt=header.path_attempt)
      result = ProgressivePrefillResult(
         "FORWARDED",
         forwarded_header=next_header,
         context=next_context,
      )
      self._remember_prefill(header, result, payload_digest, generation)
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
      with self._path_lock:
         registration = self._paths.get(header.path_id)
         generation = self._path_generations.get(header.path_id, 0)
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
      try:
         accepted_payload = snapshot_payload(payload)
         payload_digest = payload_fingerprint(accepted_payload)
      except TypeError:
         return HopReceiveResult("REJECTED", "unsupported_payload_type")
      cached = self._hop_results.get(header.idempotency_key)
      if cached is not None:
         if cached[3] != payload_digest:
            return HopReceiveResult("REJECTED", "idempotency_payload_mismatch")
         return cached[0]
      pending = self._pending_hops.get(header.idempotency_key)
      if pending is not None:
         if pending[2] != payload_digest:
            return HopReceiveResult("REJECTED", "idempotency_payload_mismatch")
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
         payload=accepted_payload,
         prefill_chunk_token_count=header.prefill_chunk_token_count,
         position=self._kv_position(request, header.phase, header.token_index),
         emits_token=(
            header.phase == "PREFILL_CHUNK"
            and self._kv_position(request, header.phase, header.token_index)
            + header.prefill_chunk_token_count
            >= len(request.prompt_token_ids)
         ),
         terminal=self._is_terminal(
            request,
            header.phase,
            header.token_index,
            header.prefill_chunk_token_count,
         ),
         lease_expires_at=hop.reservation_expires_at,
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
      with self._path_lock:
         if not self._path_is_current_locked(
            header.path_id,
            header.path_attempt,
            generation,
         ):
            return HopReceiveResult("REJECTED", "path_cancelled")
      selected = self.scheduler.pop_next(now=now)
      state.transition("ACCEPTED", path_attempt=header.path_attempt)
      state.transition("EXECUTING", path_attempt=header.path_attempt)
      with self._path_lock:
         if not self._path_is_current_locked(
            header.path_id,
            header.path_attempt,
            generation,
         ):
            return HopReceiveResult("REJECTED", "path_cancelled")
      runtime_result = self._execute_runtime(selected)
      with self._path_lock:
         if not self._path_is_current_locked(
            header.path_id,
            header.path_attempt,
            generation,
         ):
            return HopReceiveResult("REJECTED", "path_cancelled")
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
         if not self._send_if_path_current(
            header.path_id,
            header.path_attempt,
            generation,
            lambda: self.transport.send_failure_report(report),
         ):
            return HopReceiveResult("REJECTED", "path_cancelled")
         result = HopReceiveResult(
            "FAILED",
            reason=report.reason,
            failure_report=report,
         )
         self._remember_hop(header, result, payload_digest, generation)
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
         if not self._send_if_path_current(
            header.path_id,
            header.path_attempt,
            generation,
            lambda: self.transport.send_hop(next_header, runtime_result.payload),
         ):
            return HopReceiveResult("REJECTED", "path_cancelled")
         state.transition("FORWARDED", path_attempt=header.path_attempt)
         result = HopReceiveResult(
            "FORWARDED",
            forwarded_header=next_header,
         )
         self._remember_hop(header, result, payload_digest, generation)
         return result

      final_prefill_chunk = (
         self.decode_mode == "stage_local_kv"
         and header.phase == "PREFILL_CHUNK"
         and selected.position + header.prefill_chunk_token_count
         >= len(request.prompt_token_ids)
      )
      if (
         header.phase == "DECODE" or final_prefill_chunk
      ) and runtime_result.token_id is None:
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
         if not self._send_if_path_current(
            header.path_id,
            header.path_attempt,
            generation,
            lambda: self.transport.send_failure_report(report),
         ):
            return HopReceiveResult("REJECTED", "path_cancelled")
         result = HopReceiveResult(
            "FAILED",
            reason=report.reason,
            failure_report=report,
         )
         self._remember_hop(header, result, payload_digest, generation)
         return result

      if header.phase == "PREFILL_CHUNK":
         event = PrefillChunkCompleted(
            request_id=request.request_id,
            path_id=manifest.path_id,
            path_attempt=manifest.path_attempt,
            chunk_index=header.token_index,
            token_count=header.prefill_chunk_token_count,
         )
         token_event = (
            TokenEvent(
               request_id=request.request_id,
               path_id=manifest.path_id,
               path_attempt=manifest.path_attempt,
               token_index=0,
               token_id=runtime_result.token_id,
               sampling_counter=1,
            )
            if final_prefill_chunk
            else None
         )

         def send_prefill_result() -> None:
            self.transport.send_prefill_chunk_completed(event)
            if token_event is not None:
               self.transport.send_token_event(token_event)

         if not self._send_if_path_current(
            header.path_id,
            header.path_attempt,
            generation,
            send_prefill_result,
         ):
            return HopReceiveResult("REJECTED", "path_cancelled")
         state.transition("FORWARDED", path_attempt=header.path_attempt)
         result = HopReceiveResult(
            "COMPLETED",
            prefill_chunk_completed=event,
            token_event=token_event,
         )
         self._remember_hop(header, result, payload_digest, generation)
         return result
      if header.phase != "DECODE":
         state.transition("FORWARDED", path_attempt=header.path_attempt)
         result = HopReceiveResult("COMPLETED")
         self._remember_hop(header, result, payload_digest, generation)
         return result
      event = TokenEvent(
         request_id=request.request_id,
         path_id=manifest.path_id,
         path_attempt=manifest.path_attempt,
         token_index=header.token_index,
         token_id=runtime_result.token_id,
         sampling_counter=header.token_index + 1,
      )
      if not self._send_if_path_current(
         header.path_id,
         header.path_attempt,
         generation,
         lambda: self.transport.send_token_event(event),
      ):
         return HopReceiveResult("REJECTED", "path_cancelled")
      state.transition("FORWARDED", path_attempt=header.path_attempt)
      result = HopReceiveResult("COMPLETED", token_event=event)
      self._remember_hop(header, result, payload_digest, generation)
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
      with self._path_lock:
         registration = self._paths.get(header.path_id)
         generation = self._path_generations.get(header.path_id, 0)
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
      try:
         accepted_payload = snapshot_payload(payload)
         payload_digest = payload_fingerprint(accepted_payload)
      except TypeError:
         return HopReceiveResult("REJECTED", "unsupported_payload_type")
      cached = self._hop_results.get(header.idempotency_key)
      if cached is not None:
         if cached[3] != payload_digest:
            return HopReceiveResult("REJECTED", "idempotency_payload_mismatch")
         return cached[0]
      pending = self._pending_hops.get(header.idempotency_key)
      if pending is not None:
         if pending[2] != payload_digest:
            return HopReceiveResult("REJECTED", "idempotency_payload_mismatch")
         return HopReceiveResult("QUEUED", "duplicate_pending")

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
         payload=accepted_payload,
         prefill_chunk_token_count=header.prefill_chunk_token_count,
         position=self._kv_position(request, header.phase, header.token_index),
         emits_token=(
            header.phase == "PREFILL_CHUNK"
            and self._kv_position(request, header.phase, header.token_index)
            + header.prefill_chunk_token_count
            >= len(request.prompt_token_ids)
         ),
         terminal=self._is_terminal(
            request,
            header.phase,
            header.token_index,
            header.prefill_chunk_token_count,
         ),
         lease_expires_at=hop.reservation_expires_at,
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
      cancelled = False
      with self._path_lock:
         if not self._path_is_current_locked(
            header.path_id,
            header.path_attempt,
            generation,
         ):
            cancelled = True
         else:
            self._pending_hops[header.idempotency_key] = (
               header,
               work,
               payload_digest,
               generation,
            )
      if cancelled:
         self.batch_scheduler.release_path(header.path_id)
         return HopReceiveResult("REJECTED", "path_cancelled")
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
         with self._path_lock:
            active = tuple(
               item.idempotency_key in self._pending_hops
               and self._path_is_current_locked(
                  item.path_id,
                  item.path_attempt,
                  self._pending_hops[item.idempotency_key][3],
               )
               for item in batch.items
            )
         if all(active):
            runtime_results = self._execute_runtime_batch(batch)
         else:
            runtime_results = tuple(
               self._execute_runtime(item)
               if is_active
               else self._runtime_unavailable()
               for item, is_active in zip(batch.items, active)
            )
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
            with self._path_lock:
               pending = self._pending_hops.pop(item.idempotency_key, None)
            if pending is None:
               continue
            header, _, payload_digest, generation = pending
            completed.append(
               self._complete_queued_hop(
                  header,
                  item,
                  runtime_result,
                  payload_digest,
                  generation,
               )
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
      payload_digest: str,
      generation: int,
   ) -> HopReceiveResult:
      with self._path_lock:
         registration = self._paths.get(item.path_id)
         if registration is None or not self._path_is_current_locked(
            item.path_id,
            item.path_attempt,
            generation,
         ):
            return HopReceiveResult("REJECTED", "path_cancelled")
         graph, manifest, request = registration
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
         if not self._send_if_path_current(
            header.path_id,
            header.path_attempt,
            generation,
            lambda: self.transport.send_failure_report(report),
         ):
            return HopReceiveResult("REJECTED", "path_cancelled")
         result = HopReceiveResult(
            "FAILED",
            reason=report.reason,
            failure_report=report,
         )
         self._remember_hop(header, result, payload_digest, generation)
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
         if not self._send_if_path_current(
            header.path_id,
            header.path_attempt,
            generation,
            lambda: self.transport.send_hop(next_header, runtime_result.payload),
         ):
            return HopReceiveResult("REJECTED", "path_cancelled")
         result = HopReceiveResult("FORWARDED", forwarded_header=next_header)
         self._remember_hop(header, result, payload_digest, generation)
         return result

      final_prefill_chunk = (
         self.decode_mode == "stage_local_kv"
         and header.phase == "PREFILL_CHUNK"
         and item.position + header.prefill_chunk_token_count
         >= len(request.prompt_token_ids)
      )
      if (
         header.phase == "DECODE" or final_prefill_chunk
      ) and runtime_result.token_id is None:
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
         if not self._send_if_path_current(
            header.path_id,
            header.path_attempt,
            generation,
            lambda: self.transport.send_failure_report(report),
         ):
            return HopReceiveResult("REJECTED", "path_cancelled")
         result = HopReceiveResult(
            "FAILED",
            reason=report.reason,
            failure_report=report,
         )
         self._remember_hop(header, result, payload_digest, generation)
         return result

      if header.phase == "PREFILL_CHUNK":
         event = PrefillChunkCompleted(
            request_id=request.request_id,
            path_id=manifest.path_id,
            path_attempt=manifest.path_attempt,
            chunk_index=header.token_index,
            token_count=header.prefill_chunk_token_count,
         )
         token_event = (
            TokenEvent(
               request_id=request.request_id,
               path_id=manifest.path_id,
               path_attempt=manifest.path_attempt,
               token_index=0,
               token_id=runtime_result.token_id,
               sampling_counter=1,
            )
            if final_prefill_chunk
            else None
         )

         def send_prefill_result() -> None:
            self.transport.send_prefill_chunk_completed(event)
            if token_event is not None:
               self.transport.send_token_event(token_event)

         if not self._send_if_path_current(
            header.path_id,
            header.path_attempt,
            generation,
            send_prefill_result,
         ):
            return HopReceiveResult("REJECTED", "path_cancelled")
         result = HopReceiveResult(
            "COMPLETED",
            prefill_chunk_completed=event,
            token_event=token_event,
         )
         self._remember_hop(header, result, payload_digest, generation)
         return result
      if header.phase != "DECODE":
         result = HopReceiveResult("COMPLETED")
         self._remember_hop(header, result, payload_digest, generation)
         return result
      event = TokenEvent(
         request_id=request.request_id,
         path_id=manifest.path_id,
         path_attempt=manifest.path_attempt,
         token_index=header.token_index,
         token_id=runtime_result.token_id,
         sampling_counter=header.token_index + 1,
      )
      if not self._send_if_path_current(
         header.path_id,
         header.path_attempt,
         generation,
         lambda: self.transport.send_token_event(event),
      ):
         return HopReceiveResult("REJECTED", "path_cancelled")
      result = HopReceiveResult("COMPLETED", token_event=event)
      self._remember_hop(header, result, payload_digest, generation)
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
      if phase not in {"PREFILL", "PREFILL_CHUNK", "RECOVERY_PREFILL", "DECODE"}:
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
      expected_generation: int | None = None,
   ) -> RelayOutcome:
      generation = (
         self._path_generation(manifest.path_id)
         if expected_generation is None
         else expected_generation
      )
      with self._path_lock:
         if not self._path_is_current_locked(
            manifest.path_id,
            manifest.path_attempt,
            generation,
         ):
            return self._path_cancelled_outcome(request, manifest, token_index)
      execution_key = (
         manifest.path_id,
         manifest.path_attempt,
         phase,
         token_index,
      )
      now = self.clock.now()
      self._evict_outcomes(now=now)
      try:
         accepted_payload = snapshot_payload(payload)
         payload_digest = payload_fingerprint(accepted_payload)
      except TypeError:
         return RelayOutcome(
            failure_report=FailureReport(
               request_id=request.request_id,
               path_id=manifest.path_id,
               path_attempt=manifest.path_attempt,
               token_index=token_index,
               scope="REQUEST",
               reason="unsupported_payload_type",
            )
         )
      cached = self._outcomes.get(execution_key)
      if cached is not None:
         if cached[3] != payload_digest:
            return RelayOutcome(
               failure_report=FailureReport(
                  request_id=request.request_id,
                  path_id=manifest.path_id,
                  path_attempt=manifest.path_attempt,
                  token_index=token_index,
                  scope="REQUEST",
                  reason="idempotency_payload_mismatch",
               )
            )
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
      current_payload = accepted_payload
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
            position=self._kv_position(request, phase, token_index),
            terminal=self._is_terminal(request, phase, token_index),
            lease_expires_at=hop.reservation_expires_at,
            batch_key=self._runtime_batch_key(
               graph=graph,
               placement=placement_map[hop.placement_id],
               phase=phase,
               token_span=(
                  1
                  if phase == "DECODE"
                  else len(payload)
                  if phase in {"PREFILL", "RECOVERY_PREFILL"}
                  and isinstance(payload, (tuple, list))
                  else 0
               ),
            ),
         )
         self.scheduler.enqueue(work)
         with self._path_lock:
            if not self._path_is_current_locked(
               manifest.path_id,
               manifest.path_attempt,
               generation,
            ):
               return self._path_cancelled_outcome(
                  request,
                  manifest,
                  token_index,
               )
         selected = self.scheduler.pop_next(now=self.clock.now())
         result = self._execute_runtime(selected)
         with self._path_lock:
            if not self._path_is_current_locked(
               manifest.path_id,
               manifest.path_attempt,
               generation,
            ):
               return self._path_cancelled_outcome(request, manifest, token_index)
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
            if not self._send_if_path_current(
               manifest.path_id,
               manifest.path_attempt,
               generation,
               lambda: self.transport.send_failure_report(report),
            ):
               return self._path_cancelled_outcome(request, manifest, token_index)
            outcome = RelayOutcome(failure_report=report)
            self._remember(
               execution_key,
               outcome,
               manifest.path_id,
               payload_digest,
               generation,
            )
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
            if not self._send_if_path_current(
               manifest.path_id,
               manifest.path_attempt,
               generation,
               lambda: self.transport.send_hop(header, current_payload),
            ):
               return self._path_cancelled_outcome(request, manifest, token_index)

      if phase == "PREFILL" or (
         phase == "RECOVERY_PREFILL" and self.decode_mode == "stage_local_kv"
      ):
         if final_result is None or final_result.token_id is None:
            if self.decode_mode == "complete_context_replay":
               outcome = RelayOutcome()
               self._remember(
                  execution_key,
                  outcome,
                  manifest.path_id,
                  payload_digest,
                  generation,
               )
               return outcome
            report = FailureReport(
               request_id=request.request_id,
               path_id=manifest.path_id,
               path_attempt=manifest.path_attempt,
               token_index=token_index,
               scope="PLACEMENT",
               reason="final_stage_missing_token",
               placement_id=manifest.ordered_hops[-1].placement_id,
            )
            if not self._send_if_path_current(
               manifest.path_id,
               manifest.path_attempt,
               generation,
               lambda: self.transport.send_failure_report(report),
            ):
               return self._path_cancelled_outcome(request, manifest, token_index)
            outcome = RelayOutcome(failure_report=report)
            self._remember(
               execution_key,
               outcome,
               manifest.path_id,
               payload_digest,
               generation,
            )
            return outcome
         event = TokenEvent(
            request_id=request.request_id,
            path_id=manifest.path_id,
            path_attempt=manifest.path_attempt,
            token_index=0 if phase == "PREFILL" else token_index,
            token_id=final_result.token_id,
            sampling_counter=1 if phase == "PREFILL" else token_index + 1,
         )
         if not self._send_if_path_current(
            manifest.path_id,
            manifest.path_attempt,
            generation,
            lambda: self.transport.send_token_event(event),
         ):
            return self._path_cancelled_outcome(request, manifest, token_index)
         outcome = RelayOutcome(token_event=event)
         self._remember(
            execution_key,
            outcome,
            manifest.path_id,
            payload_digest,
            generation,
         )
         return outcome
      if phase != "DECODE":
         outcome = RelayOutcome()
         self._remember(
            execution_key,
            outcome,
            manifest.path_id,
            payload_digest,
            generation,
         )
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
         if not self._send_if_path_current(
            manifest.path_id,
            manifest.path_attempt,
            generation,
            lambda: self.transport.send_failure_report(report),
         ):
            return self._path_cancelled_outcome(request, manifest, token_index)
         outcome = RelayOutcome(failure_report=report)
         self._remember(
            execution_key,
            outcome,
            manifest.path_id,
            payload_digest,
            generation,
         )
         return outcome

      event = TokenEvent(
         request_id=request.request_id,
         path_id=manifest.path_id,
         path_attempt=manifest.path_attempt,
         token_index=token_index,
         token_id=final_result.token_id,
         sampling_counter=token_index + 1,
      )
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

      if not self._send_if_path_current(
         manifest.path_id,
         manifest.path_attempt,
         generation,
         lambda: self.transport.send_token_event(event),
      ):
         return self._path_cancelled_outcome(request, manifest, token_index)
      if not self._send_if_path_current(
         manifest.path_id,
         manifest.path_attempt,
         generation,
         lambda: self.transport.send_hop(loopback_header, event.token_id),
      ):
         return self._path_cancelled_outcome(request, manifest, token_index)
      outcome = RelayOutcome(token_event=event)
      self._remember(
         execution_key,
         outcome,
         manifest.path_id,
         payload_digest,
         generation,
      )
      return outcome

   def receive_path_cancellation(
      self,
      cancellation: PathCancellation,
      *,
      source_node_id: str | None,
   ) -> bool:
      if (
         type(cancellation.request_id) is not str
         or not cancellation.request_id
         or type(cancellation.path_id) is not str
         or not cancellation.path_id
         or type(cancellation.path_attempt) is not int
         or type(cancellation.topology_version) is not int
         or type(source_node_id) is not str
         or not source_node_id
      ):
         return False
      with self._path_operation_lock(cancellation.path_id):
         with self._path_lock:
            registered = self._paths.get(cancellation.path_id)
            provisional = self._provisional_paths.get(cancellation.path_id)
            if registered is None and provisional is None:
               cancelled_attempt = self._cancelled_path_attempts.get(
                  cancellation.path_id
               )
               if (
                  self._was_cancelled_attempt_locked(
                     cancellation.path_id,
                     cancellation.path_attempt,
                  )
                  or (
                     cancelled_attempt is not None
                     and cancellation.path_attempt <= cancelled_attempt
                  )
               ):
                  return False
               self._remember_pending_cancellation_locked(
                  cancellation,
                  source_node_id,
               )
               return True
            if registered is not None:
               graph, manifest, request = registered
               expected = (
                  request.request_id,
                  manifest.path_attempt,
                  graph.topology_version,
               )
               entry_node_id = self._entry_node_by_path.get(cancellation.path_id)
            else:
               assert provisional is not None
               expected = provisional[:3]
               entry_node_id = provisional[3]
            if (
               not source_node_id
               or not entry_node_id
               or type(cancellation.path_attempt) is not int
               or type(cancellation.topology_version) is not int
               or source_node_id != entry_node_id
               or (
                  cancellation.request_id,
                  cancellation.path_attempt,
                  cancellation.topology_version,
               )
               != expected
            ):
               return False
            registered_for_release = registered
            if not self._release_path_state_locked(
               cancellation.path_id,
               path_attempt=cancellation.path_attempt,
            ):
               return False
         self._release_synchronized_capacity(registered_for_release)
         self._release_path_resources(cancellation.path_id)
      return True

   def apply_controlled_path_cancellation(
      self,
      cancellation: PathCancellation,
   ) -> bool:
      """Install an owner-authoritative tombstone before path publication.

      Physical control has already generation-fenced the exact request/path
      identity.  Unlike a data-plane cancellation, it does not depend on an
      already-published entry node.  This closes the lifecycle gap where
      cancellation can win after request control is bound but before
      ``register_path`` publishes its provisional or registered path.
      """

      if (
         type(cancellation.request_id) is not str
         or not cancellation.request_id
         or type(cancellation.path_id) is not str
         or not cancellation.path_id
         or type(cancellation.path_attempt) is not int
         or type(cancellation.topology_version) is not int
      ):
         return False
      # Owner control must publish the generation tombstone immediately.  A
      # live execution owns the per-path operation lock across runtime work and
      # transport delivery; waiting for that lock here serialized cancellation
      # behind the operation it was meant to interrupt.  Every publication
      # boundary already validates the generation under ``_path_lock``, so
      # advancing it atomically is the safe preemption point.  The active
      # operation observes the new generation and cannot republish state.
      with self._path_lock:
         registered = self._paths.get(cancellation.path_id)
         provisional = self._provisional_paths.get(cancellation.path_id)
         if registered is not None:
            graph, manifest, request = registered
            if (
               request.request_id,
               manifest.path_attempt,
               graph.topology_version,
            ) != (
               cancellation.request_id,
               cancellation.path_attempt,
               cancellation.topology_version,
            ):
               return False
         elif provisional is not None and provisional[:3] != (
            cancellation.request_id,
            cancellation.path_attempt,
            cancellation.topology_version,
         ):
            return False
         self._release_path_state_locked(
            cancellation.path_id,
            path_attempt=cancellation.path_attempt,
         )
      self._release_synchronized_capacity(registered)
      self._release_path_resources(cancellation.path_id)
      return True

   def _cancel_runtime(self, path_id: str) -> None:
      try:
         self.runtime.cancel(path_id)
      except Exception:
         pass

   def _release_path_resources(self, path_id: str) -> None:
      self.scheduler.release_path(path_id)
      self.batch_scheduler.release_path(path_id)
      self._cancel_runtime(path_id)

   def _release_synchronized_capacity(
      self,
      registered: tuple[ExecutionGraph, PathManifest, RequestContext] | None,
   ) -> None:
      """Retire host-local charges imported for one registered remote path."""

      if registered is None:
         return
      release = getattr(
         self.builder.capacity,
         "release_synchronized_build",
         None,
      )
      if not callable(release):
         return
      manifest = registered[1]
      release(tuple(hop.reservation_id for hop in manifest.ordered_hops))

   def _release_path_state_locked(
      self,
      path_id: str,
      *,
      path_attempt: int | None = None,
   ) -> bool:
      registered = self._paths.get(path_id)
      if registered is not None:
         registered_attempt = registered[1].path_attempt
         if path_attempt is not None and path_attempt != registered_attempt:
            return False
         path_attempt = registered_attempt
      provisional = self._provisional_paths.get(path_id)
      if provisional is not None:
         provisional_attempt = provisional[1]
         if path_attempt is not None and path_attempt != provisional_attempt:
            return False
         path_attempt = provisional_attempt
      known_path = (
         registered is not None
         or provisional is not None
         or path_attempt is not None
         or path_id in self._path_generations
      )
      if path_attempt is not None:
         self._mark_cancelled_attempt_locked(path_id, path_attempt)
         self._cancelled_path_attempts[path_id] = max(
            path_attempt,
            self._cancelled_path_attempts.get(path_id, path_attempt),
         )
         self._cancelled_path_attempts.move_to_end(path_id)
      if known_path:
         self._path_generations[path_id] = (
            self._path_generations.get(path_id, 0) + 1
         )
         self._path_generations.move_to_end(path_id)
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
      self._provisional_paths.pop(path_id, None)
      self._entry_node_by_path.pop(path_id, None)
      self._pending_hops = {
         key: value
         for key, value in self._pending_hops.items()
         if value[1].path_id != path_id
      }
      self._prune_terminal_path_metadata_locked()
      return known_path

   def release_path(
      self,
      path_id: str,
      *,
      path_attempt: int | None = None,
   ) -> None:
      # Owner-controlled cancellation publishes the exact-attempt tombstone
      # without waiting for a live send's per-path operation lock. Entry
      # cleanup then calls release_path for the same attempt. Waiting for that
      # operation lock a second time can strand infer_cancel_wait behind the
      # data-plane operation it already fenced, even though all relay state and
      # runtime resources are gone. The tombstone is authoritative and blocks
      # late same-attempt registration, so this exact retired attempt is a
      # complete idempotent release. A newer registered attempt is deliberately
      # excluded from the fast path and remains protected by the operation lock.
      if path_attempt is not None:
         with self._path_lock:
            if (
               path_id not in self._paths
               and path_id not in self._provisional_paths
               and self._was_cancelled_attempt_locked(path_id, path_attempt)
            ):
               return
      with self._path_operation_lock(path_id):
         with self._path_lock:
            registered = self._paths.get(path_id)
            known_path = self._release_path_state_locked(
               path_id,
               path_attempt=path_attempt,
            )
         if known_path:
            self._release_synchronized_capacity(registered)
            self._release_path_resources(path_id)

   def cached_outcome_count(self) -> int:
      return len(self._outcomes)

   def _remember_prefill(
      self,
      header: HopHeader,
      result: ProgressivePrefillResult,
      payload_digest: str,
      generation: int,
   ) -> None:
      with self._path_lock:
         if not self._path_is_current_locked(
            header.path_id,
            header.path_attempt,
            generation,
         ):
            return
         self._prefill_results[header.idempotency_key] = (
            result,
            self.clock.now(),
            header.path_id,
            payload_digest,
         )
         self._evict_prefill_results(now=self.clock.now())

   def _evict_prefill_results(self, *, now: float) -> None:
      retention = max(
         0.0,
         self.scheduler.config.idempotency_retention_seconds,
      )
      for key, (_, created_at, _, _) in tuple(self._prefill_results.items()):
         if now - created_at > retention:
            self._prefill_results.pop(key, None)
      maximum = max(
         1,
         self.scheduler.config.maximum_idempotency_entries,
      )
      oldest = sorted(
         (created_at, key)
         for key, (_, created_at, _, _) in self._prefill_results.items()
      )
      while len(self._prefill_results) > maximum:
         _, key = oldest.pop(0)
         self._prefill_results.pop(key, None)

   def _remember_hop(
      self,
      header: HopHeader,
      result: HopReceiveResult,
      payload_digest: str,
      expected_generation: int,
   ) -> None:
      with self._path_lock:
         if not self._path_is_current_locked(
            header.path_id,
            header.path_attempt,
            expected_generation,
         ):
            return
         self._hop_results[header.idempotency_key] = (
            result,
            self.clock.now(),
            header.path_id,
            payload_digest,
         )
         self._evict_hop_results(now=self.clock.now())

   def _evict_hop_results(self, *, now: float) -> None:
      retention = max(
         0.0,
         self.scheduler.config.idempotency_retention_seconds,
      )
      for key, (_, created_at, _, _) in tuple(self._hop_results.items()):
         if now - created_at > retention:
            self._hop_results.pop(key, None)
      maximum = max(
         1,
         self.scheduler.config.maximum_idempotency_entries,
      )
      oldest = sorted(
         (created_at, key)
         for key, (_, created_at, _, _) in self._hop_results.items()
      )
      while len(self._hop_results) > maximum:
         _, key = oldest.pop(0)
         self._hop_results.pop(key, None)

   def _remember(
      self,
      key: tuple[str, int, str, int],
      outcome: RelayOutcome,
      path_id: str,
      payload_digest: str,
      expected_generation: int,
   ) -> None:
      with self._path_lock:
         if not self._path_is_current_locked(
            path_id,
            key[1],
            expected_generation,
         ):
            return
         self._outcomes[key] = (
            outcome,
            self.clock.now(),
            path_id,
            payload_digest,
         )
         self._evict_outcomes(now=self.clock.now())

   def _evict_outcomes(self, *, now: float) -> None:
      retention = max(
         0.0,
         self.scheduler.config.idempotency_retention_seconds,
      )
      expired = [
         key
         for key, (_, created_at, _, _) in self._outcomes.items()
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
         for key, (_, created_at, _, _) in self._outcomes.items()
      )
      while len(self._outcomes) > maximum:
         _, key = oldest.pop(0)
         self._outcomes.pop(key, None)
