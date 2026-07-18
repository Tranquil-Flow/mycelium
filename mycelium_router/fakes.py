"""Deterministic in-memory collaborators used by Router tests and demos."""

from dataclasses import dataclass

from mycelium_router.contracts import (
   ExecutionGraph,
   FailureReport,
   HopHeader,
   HopWorkItem,
   ManifestDelta,
   ManifestLocked,
   PrefillChunkCompleted,
   ProgressivePrefillContext,
   ReservationCommitResult,
   ReservationRequest,
   ReservationResult,
   RuntimeBatch,
   RuntimeResult,
   TokenEvent,
)


class SequenceIdSource:
   def __init__(self):
      self._next = 1

   def new(self, prefix: str) -> str:
      value = f"{prefix}-{self._next}"
      self._next += 1
      return value


class ManualClock:
   def __init__(self, now: float = 0.0):
      self._now = now

   def now(self) -> float:
      return self._now

   def advance(self, seconds: float) -> None:
      self._now += seconds


class FakeCapacityPort:
   def __init__(self, *, clock=None):
      self.clock = clock
      self.reject_placements: set[str] = set()
      self.fail_commit_reason = ""
      self.requests: list[ReservationRequest] = []
      self.committed_ids: set[str] = set()
      self.released_ids: set[str] = set()
      self.release_calls: list[tuple[str, ...]] = []
      self._reservation_ids: dict[tuple[str, int, str], str] = {}
      self._reservation_requests: dict[str, ReservationRequest] = {}
      self._next = 1

   def reserve(self, request: ReservationRequest) -> ReservationResult:
      self.requests.append(request)
      key = (request.request_id, request.path_attempt, request.placement_id)
      if request.placement_id in self.reject_placements:
         return ReservationResult(False, reason="capacity_rejected")
      existing = self._reservation_ids.get(key)
      if existing:
         previous = self._reservation_requests[existing]
         if (
            existing not in self.released_ids
            and previous.deployment_epoch == request.deployment_epoch
            and previous.lease_expires_at > self._now()
         ):
            return ReservationResult(
               True,
               reservation_id=existing,
               deployment_epoch=previous.deployment_epoch,
               expires_at=previous.lease_expires_at,
            )
      reservation_id = f"reservation-{self._next}"
      self._next += 1
      self._reservation_ids[key] = reservation_id
      self._reservation_requests[reservation_id] = request
      return ReservationResult(
         True,
         reservation_id=reservation_id,
         deployment_epoch=request.deployment_epoch,
         expires_at=request.lease_expires_at,
      )

   def commit(
      self,
      reservation_ids: tuple[str, ...],
      *,
      deployment_epoch: int,
   ) -> ReservationCommitResult:
      if self.fail_commit_reason:
         return ReservationCommitResult(False, self.fail_commit_reason)
      records = [self._reservation_requests.get(item) for item in reservation_ids]
      if any(record is None for record in records):
         return ReservationCommitResult(False, "unknown_reservation")
      if any(item in self.released_ids for item in reservation_ids):
         return ReservationCommitResult(False, "reservation_released")
      if any(
         record.deployment_epoch != deployment_epoch
         for record in records
         if record is not None
      ):
         return ReservationCommitResult(False, "deployment_epoch_mismatch")
      if any(
         record.lease_expires_at <= self._now()
         for record in records
         if record is not None
      ):
         return ReservationCommitResult(False, "reservation_expired")
      self.committed_ids.update(reservation_ids)
      return ReservationCommitResult(True)

   def release(self, reservation_ids: tuple[str, ...]) -> None:
      self.release_calls.append(reservation_ids)
      self.released_ids.update(reservation_ids)

   def _now(self) -> float:
      return self.clock.now() if self.clock is not None else 0.0


class FakeTopologyProvider:
   def __init__(self, graph: ExecutionGraph):
      self._graph = graph

   def snapshot(self) -> ExecutionGraph:
      return self._graph

   def set(self, graph: ExecutionGraph) -> None:
      self._graph = graph


class FakeDeviceStateProvider:
   def __init__(self, states):
      self._states = dict(states)

   def snapshot(self):
      return dict(self._states)

   def set(self, states) -> None:
      self._states = dict(states)


class FakeTransportPort:
   def __init__(self):
      self.hops: list[tuple[HopHeader, object]] = []
      self.manifest_deltas: list[ManifestDelta] = []
      self.manifest_locks: list[ManifestLocked] = []
      self.failure_reports: list[FailureReport] = []
      self.token_events: list[TokenEvent] = []
      self.prefill_chunk_completions: list[PrefillChunkCompleted] = []

   def send_hop(self, header: HopHeader, payload: object) -> None:
      self.hops.append((header, payload))

   def send_manifest_delta(self, delta: ManifestDelta) -> None:
      self.manifest_deltas.append(delta)

   def send_manifest_locked(self, locked: ManifestLocked) -> None:
      self.manifest_locks.append(locked)

   def send_failure_report(self, report: FailureReport) -> None:
      self.failure_reports.append(report)

   def send_token_event(self, event: TokenEvent) -> None:
      self.token_events.append(event)

   def send_prefill_chunk_completed(
      self,
      event: PrefillChunkCompleted,
   ) -> None:
      self.prefill_chunk_completions.append(event)


@dataclass(frozen=True)
class InProcessHopDelivery:
   source_node_id: str
   destination_node_id: str
   header: HopHeader


class InProcessMeshTransport:
   def __init__(self, mesh, node_id: str):
      self.mesh = mesh
      self.node_id = node_id

   def send_hop(self, header: HopHeader, payload: object) -> None:
      self.mesh.send_hop(self.node_id, header, payload)

   def send_manifest_delta(self, delta: ManifestDelta) -> None:
      self.mesh.send_manifest_delta(self.node_id, delta)

   def send_manifest_locked(self, locked: ManifestLocked) -> None:
      self.mesh.send_manifest_locked(self.node_id, locked)

   def send_failure_report(self, report: FailureReport) -> None:
      self.mesh.send_failure_report(self.node_id, report)

   def send_token_event(self, event: TokenEvent) -> None:
      self.mesh.send_token_event(self.node_id, event)

   def send_prefill_chunk_completed(
      self,
      event: PrefillChunkCompleted,
   ) -> None:
      self.mesh.send_prefill_chunk_completed(self.node_id, event)


class InProcessMesh:
   """Synchronous placement-addressed transport for multi-Router proofs."""

   def __init__(self):
      self.routers: dict[str, object] = {}
      self._transports: dict[str, InProcessMeshTransport] = {}
      self._entry_by_request: dict[str, str] = {}
      self._graph_by_path: dict[str, ExecutionGraph] = {}
      self.hop_deliveries: list[InProcessHopDelivery] = []
      self.hop_results: list[object] = []
      self.manifest_deltas: list[tuple[str, ManifestDelta]] = []
      self.manifest_locks: list[tuple[str, ManifestLocked]] = []
      self.failure_reports: list[tuple[str, FailureReport]] = []
      self.token_events: list[tuple[str, TokenEvent]] = []
      self.prefill_chunk_completions: list[
         tuple[str, PrefillChunkCompleted]
      ] = []
      self.defer_prefill_chunk_completions = False
      self.deferred_prefill_chunk_completions: list[
         tuple[str, PrefillChunkCompleted]
      ] = []

   def transport_for(self, node_id: str) -> InProcessMeshTransport:
      transport = self._transports.get(node_id)
      if transport is None:
         transport = InProcessMeshTransport(self, node_id)
         self._transports[node_id] = transport
      return transport

   def register_router(self, node_id: str, router) -> None:
      if node_id in self.routers:
         raise ValueError("duplicate_mesh_node")
      self.routers[node_id] = router

   def send_hop(
      self,
      source_node_id: str,
      header: HopHeader,
      payload: object,
   ) -> None:
      if source_node_id not in self.routers:
         raise ValueError(f"unknown_mesh_source:{source_node_id}")
      if isinstance(payload, ProgressivePrefillContext):
         graph = payload.graph
         self._entry_by_request.setdefault(header.request_id, source_node_id)
         self._graph_by_path[header.path_id] = graph
      else:
         graph = self._graph_by_path.get(header.path_id)
         if graph is None:
            raise ValueError("unknown_mesh_path")
      placement = self._placement(graph, header.destination_placement_id)
      destination_node_id = placement.node_id
      router = self.routers.get(destination_node_id)
      if router is None:
         raise ValueError(f"unknown_mesh_node:{destination_node_id}")
      self.hop_deliveries.append(
         InProcessHopDelivery(
            source_node_id=source_node_id,
            destination_node_id=destination_node_id,
            header=header,
         )
      )
      if isinstance(payload, ProgressivePrefillContext):
         result = router.receive_progressive_prefill(
            header,
            payload,
            source_node_id=source_node_id,
         )
      else:
         result = router.receive_hop(
            header,
            payload,
            source_node_id=source_node_id,
         )
      self.hop_results.append(result)

   def send_manifest_delta(
      self,
      source_node_id: str,
      delta: ManifestDelta,
   ) -> None:
      self.manifest_deltas.append((source_node_id, delta))

   def send_manifest_locked(
      self,
      source_node_id: str,
      locked: ManifestLocked,
   ) -> None:
      if source_node_id not in self.routers:
         raise ValueError(f"unknown_mesh_source:{source_node_id}")
      self.manifest_locks.append((source_node_id, locked))
      graph = locked.build.graph
      placement_map = {
         placement.placement_id: placement
         for stage in graph.stages
         for placement in stage.placements
      }
      participant_nodes = {
         placement_map[hop.placement_id].node_id
         for hop in locked.manifest.ordered_hops
      }
      entry_node_id = self._entry_by_request.get(locked.request_id)
      entry = self.routers.get(entry_node_id) if entry_node_id else None
      if entry is None:
         raise ValueError("unknown_mesh_entry")
      for node_id in participant_nodes:
         router = self.routers.get(node_id)
         if router is None:
            raise ValueError(f"unknown_mesh_node:{node_id}")
         accepted = router.register_path(
            locked.build.request,
            locked.manifest,
            graph,
            source_node_id=source_node_id,
            entry_node_id=entry_node_id,
         )
         if not accepted:
            raise ValueError("manifest_lock_rejected")
      self._graph_by_path[locked.path_id] = graph
      if not entry.receive_manifest_locked(
         locked,
         source_node_id=source_node_id,
      ):
         raise ValueError("manifest_lock_rejected")

   def send_failure_report(
      self,
      source_node_id: str,
      report: FailureReport,
   ) -> None:
      if source_node_id not in self.routers:
         raise ValueError(f"unknown_mesh_source:{source_node_id}")
      self.failure_reports.append((source_node_id, report))
      entry = self._entry_router(report.request_id)
      entry.receive_failure_report(report, source_node_id=source_node_id)

   def send_token_event(
      self,
      source_node_id: str,
      event: TokenEvent,
   ) -> None:
      if source_node_id not in self.routers:
         raise ValueError(f"unknown_mesh_source:{source_node_id}")
      self.token_events.append((source_node_id, event))
      entry = self._entry_router(event.request_id)
      entry.receive_token_event(event, source_node_id=source_node_id)

   def send_prefill_chunk_completed(
      self,
      source_node_id: str,
      event: PrefillChunkCompleted,
   ) -> None:
      if source_node_id not in self.routers:
         raise ValueError(f"unknown_mesh_source:{source_node_id}")
      delivery = (source_node_id, event)
      self.prefill_chunk_completions.append(delivery)
      if self.defer_prefill_chunk_completions:
         self.deferred_prefill_chunk_completions.append(delivery)
         return
      entry = self._entry_router(event.request_id)
      entry.receive_prefill_chunk_completed(event, source_node_id=source_node_id)

   def deliver_next_prefill_chunk_completion(self) -> bool:
      if not self.deferred_prefill_chunk_completions:
         return False
      source_node_id, event = self.deferred_prefill_chunk_completions.pop(0)
      entry = self._entry_router(event.request_id)
      return entry.receive_prefill_chunk_completed(
         event,
         source_node_id=source_node_id,
      )

   def _entry_router(self, request_id: str):
      node_id = self._entry_by_request.get(request_id)
      router = self.routers.get(node_id) if node_id else None
      if router is None:
         raise ValueError("unknown_mesh_entry")
      return router

   @staticmethod
   def _placement(graph: ExecutionGraph, placement_id: str):
      for stage in graph.stages:
         for placement in stage.placements:
            if placement.placement_id == placement_id:
               return placement
      raise ValueError(f"unknown_mesh_placement:{placement_id}")


class FakeRuntimePort:
   def __init__(self, *, token_base: int = 100):
      self.token_base = token_base
      self.executed: list[HopWorkItem] = []
      self.executed_batches: list[RuntimeBatch] = []
      self.cancelled_path_ids: set[str] = set()
      self.cancel_calls: list[str] = []
      self._failures: set[tuple[str, str, int, str]] = set()

   def fail_once(
      self,
      *,
      placement_id: str,
      phase: str,
      token_index: int,
      scope: str,
   ) -> None:
      self._failures.add((placement_id, phase, token_index, scope))

   def execute(self, item: HopWorkItem) -> RuntimeResult:
      self.executed.append(item)
      matching = next(
         (
            failure
            for failure in self._failures
            if failure[0] == item.placement_id
            and failure[1] == item.phase
            and failure[2] == item.token_index
         ),
         None,
      )
      if matching is not None:
         self._failures.remove(matching)
         return RuntimeResult(
            success=False,
            failure_scope=matching[3],
            failure_reason="injected_failure",
         )
      token_id = (
         self.token_base + item.token_index + 1
         if item.phase == "DECODE" and item.token_index >= 0
         else None
      )
      return RuntimeResult(
         success=True,
         payload=item.payload,
         token_id=token_id,
      )

   def execute_batch(self, batch: RuntimeBatch) -> tuple[RuntimeResult, ...]:
      self.executed_batches.append(batch)
      return tuple(self.execute(item) for item in batch.items)

   def cancel(self, path_id: str) -> None:
      self.cancel_calls.append(path_id)
      self.cancelled_path_ids.add(path_id)


class InMemoryClientSink:
   def __init__(self):
      self.token_ids: list[int] = []
      self.token_indexes: list[int] = []

   def emit(self, token_index: int, token_id: int) -> None:
      self.token_indexes.append(token_index)
      self.token_ids.append(token_id)
