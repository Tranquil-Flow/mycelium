"""External seams for topology, state, capacity, transport, and execution."""

from typing import Protocol

from mycelium_router.contracts import (
   DeviceState,
   ExecutionGraph,
   FailureReport,
   HopHeader,
   HopWorkItem,
   ManifestDelta,
   ManifestLocked,
   PathCancellation,
   PrefillChunkCompleted,
   ReservationCommitResult,
   ReservationRequest,
   ReservationResult,
   RuntimeBatch,
   RuntimeResult,
   TokenEvent,
)


class TopologyProvider(Protocol):
   def snapshot(self) -> ExecutionGraph: ...


class DeviceStateProvider(Protocol):
   def snapshot(self) -> dict[str, DeviceState]: ...


class CapacityPort(Protocol):
   def reserve(self, request: ReservationRequest) -> ReservationResult: ...
   def commit(
      self,
      reservation_ids: tuple[str, ...],
      *,
      deployment_epoch: int,
   ) -> ReservationCommitResult: ...
   def release(self, reservation_ids: tuple[str, ...]) -> None: ...


class TransportPort(Protocol):
   def remember_entry(self, request_id: str, node_id: str) -> None: ...
   def send_hop(self, header: HopHeader, payload: object) -> None: ...
   def send_manifest_delta(self, delta: ManifestDelta) -> None: ...
   def send_manifest_locked(self, locked: ManifestLocked) -> None: ...
   def send_path_cancellation(self, cancellation: PathCancellation) -> None: ...
   def send_failure_report(self, report: FailureReport) -> None: ...
   def send_token_event(self, event: TokenEvent) -> None: ...
   def send_prefill_chunk_completed(
      self,
      event: PrefillChunkCompleted,
   ) -> None: ...


class RuntimePort(Protocol):
   decode_mode: str

   def execute(self, item: HopWorkItem) -> RuntimeResult: ...
   def execute_batch(self, batch: RuntimeBatch) -> tuple[RuntimeResult, ...]: ...
   def cancel(self, path_id: str) -> None: ...


class Clock(Protocol):
   def now(self) -> float: ...


class IdSource(Protocol):
   def new(self, prefix: str) -> str: ...


class ClientSink(Protocol):
   def emit(self, token_index: int, token_id: int) -> None: ...
