"""Thread-safe local Router state and lease-capacity ports.

These implementations support deterministic local and multi-process qualification.
The capacity coordinator is deliberately process-local; it is not a remote
reservation transport and makes no distributed-consensus claim.
"""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from math import isfinite
from threading import RLock
from types import MappingProxyType
from typing import NoReturn, cast


from mycelium_router.contracts import (
   DeviceState,
   ExecutionGraph,
   LayerRange,
   Placement,
   PlacementEdge,
   ReservationCommitResult,
   ReservationRequest,
   ReservationResult,
   Stage,
   StageCost,
)
from mycelium_router.ports import CapacityPort, DeviceStateProvider, TopologyProvider
from mycelium_router.validation import validate_execution_graph


CAPACITY_CLAIM_BOUNDARY = (
   "centralized_in_process_coordinator_for_local_qualification_"
   "not_remote_production_transport"
)


GraphIdentity = tuple[object, ...]
PlacementIdentity = tuple[str, ...]
ReservationIdentity = tuple[str, int, str]


def _invalid_graph_field(field: str) -> NoReturn:
   raise ValueError(f"invalid_graph_field:{field}")


def _is_int(value: object) -> bool:
   return isinstance(value, int) and not isinstance(value, bool)


def _is_finite_number(value: object) -> bool:
   if not isinstance(value, (int, float)) or isinstance(value, bool):
      return False
   try:
      return isfinite(float(value))
   except (OverflowError, TypeError, ValueError):
      return False


def _as_tuple(value: object, field: str) -> tuple[object, ...]:
   if isinstance(value, (str, bytes, Mapping)) or not isinstance(value, Iterable):
      _invalid_graph_field(field)
   try:
      return tuple(cast(Iterable[object], value))
   except (TypeError, ValueError):
      _invalid_graph_field(field)


def _validate_graph_runtime_types(graph: ExecutionGraph) -> None:
   if not isinstance(graph, ExecutionGraph):
      _invalid_graph_field("graph")
   for field in (
      "deployment_id",
      "model_id",
      "resolved_commit",
      "manifest_digest",
      "entry_stage_id",
      "final_stage_id",
      "protocol",
   ):
      value = getattr(graph, field)
      if not isinstance(value, str) or not value:
         _invalid_graph_field(field)
   for field in (
      "deployment_epoch",
      "topology_version",
      "hidden_size",
      "activation_bytes",
      "token_envelope_bytes",
   ):
      if not _is_int(getattr(graph, field)):
         _invalid_graph_field(field)

   for stage in graph.stages:
      if not isinstance(stage, Stage):
         _invalid_graph_field("stage")
      if not isinstance(stage.stage_id, str) or not stage.stage_id:
         _invalid_graph_field("stage_id")
      if not isinstance(stage.layer_range, LayerRange):
         _invalid_graph_field("layer_range")
      for field in ("start_layer", "end_layer_exclusive", "layer_count"):
         if not _is_int(getattr(stage.layer_range, field)):
            _invalid_graph_field(field)
      if any(
         not isinstance(role, str) or not role for role in stage.component_roles
      ):
         _invalid_graph_field("component_roles")
      if not isinstance(stage.stage_cost, StageCost):
         _invalid_graph_field("stage_cost")
      for field in (
         "prefill_work_units_per_prompt_token",
         "decode_work_units_per_token",
      ):
         if not _is_finite_number(getattr(stage.stage_cost, field)):
            _invalid_graph_field(field)
      if not _is_int(stage.stage_cost.kv_bytes_per_context_token):
         _invalid_graph_field("kv_bytes_per_context_token")
      for placement in stage.placements:
         if not isinstance(placement, Placement):
            _invalid_graph_field("placement")
         for field in (
            "placement_id",
            "node_id",
            "replica_group_id",
            "assignment_id",
            "stage_signature",
            "load_proof_digest",
            "runtime_backend",
            "runtime_endpoint",
            "lifecycle_state",
         ):
            value = getattr(placement, field)
            if not isinstance(value, str) or not value:
               _invalid_graph_field(field)

   for edge in graph.edges + graph.loopback_edges:
      if not isinstance(edge, PlacementEdge):
         _invalid_graph_field("placement_edge")
      for field in (
         "edge_id",
         "from_placement_id",
         "to_placement_id",
         "link_id",
      ):
         value = getattr(edge, field)
         if not isinstance(value, str) or not value:
            _invalid_graph_field(field)


def _freeze_graph(graph: ExecutionGraph) -> ExecutionGraph:
   """Copy graph-owned containers before validating and publishing them."""

   if not isinstance(graph, ExecutionGraph):
      _invalid_graph_field("graph")
   stage_items = _as_tuple(graph.stages, "stages")
   frozen_stages: list[Stage] = []
   for stage in stage_items:
      if not isinstance(stage, Stage):
         _invalid_graph_field("stage")
      frozen_stages.append(
         replace(
            stage,
            component_roles=_as_tuple(
               stage.component_roles,
               "component_roles",
            ),
            placements=_as_tuple(stage.placements, "placements"),
         )
      )
   frozen = replace(
      graph,
      stages=tuple(frozen_stages),
      edges=_as_tuple(graph.edges, "edges"),
      loopback_edges=_as_tuple(graph.loopback_edges, "loopback_edges"),
   )
   _validate_graph_runtime_types(frozen)
   return validate_execution_graph(frozen)


def _graph_identity(graph: ExecutionGraph) -> GraphIdentity:
   stage_identity = tuple(
      (
         stage.stage_id,
         stage.layer_range.start_layer,
         stage.layer_range.end_layer_exclusive,
         stage.layer_range.layer_count,
         stage.component_roles,
         stage.stage_cost.prefill_work_units_per_prompt_token,
         stage.stage_cost.decode_work_units_per_token,
         stage.stage_cost.kv_bytes_per_context_token,
      )
      for stage in graph.stages
   )
   return (
      graph.deployment_id,
      graph.model_id,
      graph.resolved_commit,
      graph.manifest_digest,
      graph.hidden_size,
      graph.activation_bytes,
      graph.token_envelope_bytes,
      graph.protocol,
      stage_identity,
   )


def _placement_identity(placement: Placement) -> PlacementIdentity:
   return (
      placement.placement_id,
      placement.node_id,
      placement.replica_group_id,
      placement.assignment_id,
      placement.stage_signature,
      placement.load_proof_digest,
      placement.runtime_backend,
      placement.runtime_endpoint,
      placement.lifecycle_state,
   )


def _placement_identities(graph: ExecutionGraph) -> dict[str, PlacementIdentity]:
   return {
      placement.placement_id: (stage.stage_id,) + _placement_identity(placement)
      for stage in graph.stages
      for placement in stage.placements
   }


class PublishedTopologyProvider(TopologyProvider):
   """Lock-protected publication point for validated immutable graphs."""

   def __init__(self, graph: ExecutionGraph):
      self._lock = RLock()
      self._graph = _freeze_graph(graph)
      self._placement_identity_by_epoch = {
         self._graph.deployment_epoch: _placement_identities(self._graph)
      }

   def snapshot(self) -> ExecutionGraph:
      with self._lock:
         return self._graph

   def publish(self, graph: ExecutionGraph) -> ExecutionGraph:
      candidate = _freeze_graph(graph)
      with self._lock:
         current = self._graph
         if candidate.topology_version <= current.topology_version:
            raise ValueError("topology_version_not_increasing")
         if candidate.deployment_epoch < current.deployment_epoch:
            raise ValueError("deployment_epoch_regression")
         if (
            _graph_identity(candidate) != _graph_identity(current)
            and candidate.deployment_epoch <= current.deployment_epoch
         ):
            raise ValueError(
               "deployment_epoch_not_increasing_for_identity_change"
            )
         candidate_identities = _placement_identities(candidate)
         epoch_identities = (
            self._placement_identity_by_epoch.get(candidate.deployment_epoch, {})
            if candidate.deployment_epoch == current.deployment_epoch
            else {}
         )
         for placement_id, identity in candidate_identities.items():
            original = epoch_identities.get(placement_id)
            if original is not None and original != identity:
               raise ValueError("placement_identity_changed_within_epoch")
         updated_identities = dict(epoch_identities)
         updated_identities.update(candidate_identities)
         self._graph = candidate
         self._placement_identity_by_epoch = {
            candidate.deployment_epoch: updated_identities
         }
         return candidate


def _freeze_device_state(state: DeviceState) -> DeviceState:
   if not isinstance(state, DeviceState):
      raise ValueError("invalid_device_state:state")
   if not isinstance(state.neighbor_rtt_ms, Mapping):
      raise ValueError("invalid_device_state:neighbor_rtt_ms")
   if not isinstance(state.neighbor_bandwidth_bytes_per_second, Mapping):
      raise ValueError(
         "invalid_device_state:neighbor_bandwidth_bytes_per_second"
      )
   try:
      rtt = dict(state.neighbor_rtt_ms)
   except (TypeError, ValueError):
      raise ValueError("invalid_device_state:neighbor_rtt_ms") from None
   try:
      bandwidth = dict(state.neighbor_bandwidth_bytes_per_second)
   except (TypeError, ValueError):
      raise ValueError(
         "invalid_device_state:neighbor_bandwidth_bytes_per_second"
      ) from None

   if not isinstance(state.node_id, str) or not state.node_id:
      raise ValueError("invalid_device_state:node_id")
   if (
      not isinstance(state.availability, str)
      or state.availability not in {"ALIVE", "DEAD", "DRAIN", "STALE"}
   ):
      raise ValueError("invalid_device_state:availability")
   for field in ("state_seq", "available_kv_bytes", "pending_hop_queue_depth"):
      value = getattr(state, field)
      if not _is_int(value) or value < 0:
         raise ValueError(f"invalid_device_state:{field}")
   for field in ("last_updated", "compute_units_per_second"):
      value = getattr(state, field)
      if not _is_finite_number(value) or float(value) < 0.0:
         raise ValueError(f"invalid_device_state:{field}")
   if (
      not _is_finite_number(state.free_compute_fraction)
      or not 0.0 <= float(state.free_compute_fraction) <= 1.0
   ):
      raise ValueError("invalid_device_state:free_compute_fraction")
   for peer, value in rtt.items():
      if (
         not isinstance(peer, str)
         or not peer
         or not _is_finite_number(value)
         or float(value) < 0.0
      ):
         raise ValueError("invalid_device_state:neighbor_rtt_ms")
   for peer, value in bandwidth.items():
      if (
         not isinstance(peer, str)
         or not peer
         or not _is_finite_number(value)
         or float(value) < 0.0
      ):
         raise ValueError(
            "invalid_device_state:neighbor_bandwidth_bytes_per_second"
         )
   return replace(
      state,
      neighbor_rtt_ms=MappingProxyType(rtt),
      neighbor_bandwidth_bytes_per_second=MappingProxyType(bandwidth),
   )


def _copy_device_state(state: DeviceState) -> DeviceState:
   """Return a contract-compatible copy without exposing stored nested maps."""

   return replace(
      state,
      neighbor_rtt_ms=dict(state.neighbor_rtt_ms),
      neighbor_bandwidth_bytes_per_second=dict(
         state.neighbor_bandwidth_bytes_per_second
      ),
   )


class PublishedDeviceStateProvider(DeviceStateProvider):
   """Atomically publishes defensive whole-map DeviceState snapshots."""

   def __init__(
      self,
      topology: TopologyProvider,
      states: Mapping[str, DeviceState] | None = None,
      *,
      allow_unknown_nodes: bool = False,
      allowed_unknown_node_ids: Iterable[str] = (),
   ):
      self._topology = topology
      if type(allow_unknown_nodes) is not bool:
         raise ValueError("invalid_allow_unknown_nodes")
      if isinstance(allowed_unknown_node_ids, (str, bytes)):
         raise ValueError("invalid_allowed_unknown_node_ids")
      try:
         allowed_ids = frozenset(allowed_unknown_node_ids)
      except TypeError:
         raise ValueError("invalid_allowed_unknown_node_ids") from None
      if any(not isinstance(node_id, str) or not node_id for node_id in allowed_ids):
         raise ValueError("invalid_allowed_unknown_node_ids")
      self._allow_unknown_nodes = allow_unknown_nodes
      self._allowed_unknown_node_ids = allowed_ids
      self._lock = RLock()
      self._states: dict[str, DeviceState] = {}
      self.publish({} if states is None else states)

   def snapshot(self) -> dict[str, DeviceState]:
      with self._lock:
         return {
            node_id: _copy_device_state(state)
            for node_id, state in self._states.items()
         }

   def publish(
      self,
      states: Mapping[str, DeviceState],
   ) -> dict[str, DeviceState]:
      if not isinstance(states, Mapping):
         raise ValueError("invalid_device_state_map")
      graph = _freeze_graph(self._topology.snapshot())
      graph_nodes = {
         placement.node_id
         for stage in graph.stages
         for placement in stage.placements
      }
      candidate: dict[str, DeviceState] = {}
      for node_id, item in dict(states).items():
         frozen = _freeze_device_state(item)
         if node_id != frozen.node_id:
            raise ValueError(f"device_state_key_mismatch:{node_id}")
         if (
            node_id not in graph_nodes
            and node_id not in self._allowed_unknown_node_ids
            and not self._allow_unknown_nodes
         ):
            raise ValueError(f"unknown_device_state_node:{node_id}")
         candidate[node_id] = frozen

      with self._lock:
         self._states = candidate
         return {
            node_id: _copy_device_state(state)
            for node_id, state in candidate.items()
         }


@dataclass(frozen=True)
class CapacityReservationSnapshot:
   reservation_id: str
   request_id: str
   path_id: str
   path_attempt: int
   placement_id: str
   node_id: str
   kv_bytes: int
   deployment_epoch: int
   lease_expires_at: float
   status: str


@dataclass(frozen=True)
class CapacitySnapshot:
   """Read-only evidence view of one process-local capacity coordinator."""

   claim_boundary: str
   deployment_id: str
   deployment_epoch: int
   topology_version: int
   observed_at: float
   node_capacity_kv_bytes: Mapping[str, int]
   node_reserved_kv_bytes: Mapping[str, int]
   node_available_kv_bytes: Mapping[str, int]
   reservations: Mapping[str, CapacityReservationSnapshot]


@dataclass(frozen=True)
class _ReservationRecord:
   reservation_id: str
   request: ReservationRequest
   node_id: str
   placement_identity: PlacementIdentity
   status: str = "RESERVED"


class InProcessLeaseCapacityPort(CapacityPort):
   """Centralized lease coordinator for local qualification only.

   One lock covers validation, capacity accounting, idempotency, commit, release,
   and expiry reaping. It therefore prevents process-local overcommit, but it is
   intentionally not a remote production reservation transport.
   """

   claim_boundary = CAPACITY_CLAIM_BOUNDARY

   def __init__(
      self,
      topology: TopologyProvider | ExecutionGraph,
      node_available_kv_bytes: Mapping[str, int],
      *,
      clock,
      id_source,
   ):
      self._topology: TopologyProvider
      if isinstance(topology, ExecutionGraph):
         self._topology = PublishedTopologyProvider(topology)
      else:
         self._topology = topology
      self._clock = clock
      self._id_source = id_source
      self._lock = RLock()
      self._id_lock = RLock()

      graph = self._current_graph()
      graph_nodes = self._graph_nodes(graph)
      capacities = dict(node_available_kv_bytes)
      for node_id, value in capacities.items():
         if not isinstance(node_id, str) or not node_id:
            raise ValueError("invalid_capacity_node_id")
         if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"invalid_node_capacity:{node_id}")
         if value < 0:
            raise ValueError(f"negative_node_capacity:{node_id}")
      missing_nodes = graph_nodes - set(capacities)
      if missing_nodes:
         raise ValueError(
            f"missing_node_capacity:{sorted(missing_nodes)[0]}"
         )

      self._node_capacity_kv_bytes: Mapping[str, int] = MappingProxyType(
         capacities
      )
      self._node_reserved_kv_bytes = {
         node_id: 0 for node_id in capacities
      }
      self._records: dict[str, _ReservationRecord] = {}
      self._reservation_by_identity: dict[ReservationIdentity, str] = {}
      self._placement_identity_by_epoch: dict[
         int, dict[str, PlacementIdentity]
      ] = {
         graph.deployment_epoch: {
            placement.placement_id: self._placement_identity(placement)
            for placement in self._placements_by_id(graph).values()
         }
      }

   def reserve(self, request: ReservationRequest) -> ReservationResult:
      # External clock, topology, and ID callbacks run outside the accounting
      # lock. The request is fully revalidated under the lock after ID creation.
      now = self._now()
      graph = self._current_graph()
      with self._lock:
         self._reap_expired(now)
         result, _ = self._evaluate_reserve(request, now, graph)
         if result is not None:
            return result

      with self._id_lock:
         reservation_id = self._id_source.new("reservation")
      if not isinstance(reservation_id, str) or not reservation_id:
         raise RuntimeError("invalid_reservation_id")

      now = self._now()
      graph = self._current_graph()
      with self._lock:
         self._reap_expired(now)
         result, placement = self._evaluate_reserve(request, now, graph)
         if result is not None:
            return result
         if placement is None:
            raise RuntimeError("missing_validated_placement")
         if reservation_id in self._records:
            raise RuntimeError("duplicate_reservation_id")

         identity = self._identity(request)
         placement_identity = self._placement_identity(placement)
         record = _ReservationRecord(
            reservation_id=reservation_id,
            request=request,
            node_id=placement.node_id,
            placement_identity=placement_identity,
         )
         self._records[reservation_id] = record
         self._reservation_by_identity[identity] = reservation_id
         self._node_reserved_kv_bytes[placement.node_id] += request.kv_bytes
         return self._accepted(record)

   def commit(
      self,
      reservation_ids: tuple[str, ...],
      *,
      deployment_epoch: int,
   ) -> ReservationCommitResult:
      now = self._now()
      graph = self._current_graph()
      with self._lock:
         self._reap_expired(now)
         if (
            isinstance(deployment_epoch, bool)
            or not isinstance(deployment_epoch, int)
            or deployment_epoch < 0
         ):
            return ReservationCommitResult(False, "invalid_deployment_epoch")
         if deployment_epoch != graph.deployment_epoch:
            return ReservationCommitResult(
               False,
               "deployment_epoch_mismatch",
            )
         if not reservation_ids:
            return ReservationCommitResult(False, "empty_reservation_set")

         records: list[_ReservationRecord] = []
         for reservation_id in reservation_ids:
            record = self._records.get(reservation_id)
            if record is None:
               return ReservationCommitResult(False, "unknown_reservation")
            if record.status == "RELEASED":
               return ReservationCommitResult(False, "reservation_released")
            if record.status == "EXPIRED":
               return ReservationCommitResult(False, "reservation_expired")
            if record.request.deployment_epoch != deployment_epoch:
               return ReservationCommitResult(
                  False,
                  "deployment_epoch_mismatch",
               )
            if (
               record.status == "RESERVED"
               and record.request.lease_expires_at <= now
            ):
               return ReservationCommitResult(False, "reservation_expired")
            placement, placement_reason = self._bound_placement(
               graph,
               record.request.placement_id,
            )
            if placement_reason:
               return ReservationCommitResult(False, placement_reason)
            if placement is None:
               raise RuntimeError("missing_bound_placement")
            if self._placement_identity(placement) != record.placement_identity:
               return ReservationCommitResult(False, "placement_changed")
            records.append(record)

         for record in records:
            if record.status == "RESERVED":
               self._records[record.reservation_id] = replace(
                  record,
                  status="COMMITTED",
               )
         return ReservationCommitResult(True)

   def release(self, reservation_ids: tuple[str, ...]) -> None:
      with self._lock:
         for reservation_id in reservation_ids:
            record = self._records.get(reservation_id)
            if record is None or record.status in {"RELEASED", "EXPIRED"}:
               continue
            self._uncharge(record)
            self._records[reservation_id] = replace(
               record,
               status="RELEASED",
            )

   def snapshot(self) -> CapacitySnapshot:
      while True:
         now = self._now()
         graph = self._current_graph()
         with self._lock:
            self._reap_expired(now)
            capacity = dict(self._node_capacity_kv_bytes)
            reserved = dict(self._node_reserved_kv_bytes)
            records = dict(self._records)
         confirmed_graph = self._current_graph()
         if (
            confirmed_graph.deployment_id,
            confirmed_graph.deployment_epoch,
            confirmed_graph.topology_version,
         ) != (
            graph.deployment_id,
            graph.deployment_epoch,
            graph.topology_version,
         ):
            continue
         available = {
            node_id: capacity[node_id] - reserved[node_id]
            for node_id in capacity
         }
         reservations = {
            reservation_id: CapacityReservationSnapshot(
               reservation_id=record.reservation_id,
               request_id=record.request.request_id,
               path_id=record.request.path_id,
               path_attempt=record.request.path_attempt,
               placement_id=record.request.placement_id,
               node_id=record.node_id,
               kv_bytes=record.request.kv_bytes,
               deployment_epoch=record.request.deployment_epoch,
               lease_expires_at=record.request.lease_expires_at,
               status=record.status,
            )
            for reservation_id, record in records.items()
         }
         return CapacitySnapshot(
            claim_boundary=CAPACITY_CLAIM_BOUNDARY,
            deployment_id=graph.deployment_id,
            deployment_epoch=graph.deployment_epoch,
            topology_version=graph.topology_version,
            observed_at=now,
            node_capacity_kv_bytes=MappingProxyType(capacity),
            node_reserved_kv_bytes=MappingProxyType(reserved),
            node_available_kv_bytes=MappingProxyType(available),
            reservations=MappingProxyType(reservations),
         )

   def _current_graph(self) -> ExecutionGraph:
      return _freeze_graph(self._topology.snapshot())

   @staticmethod
   def _graph_nodes(graph: ExecutionGraph) -> set[str]:
      return {
         placement.node_id
         for stage in graph.stages
         for placement in stage.placements
      }

   @staticmethod
   def _placements_by_id(graph: ExecutionGraph) -> dict[str, Placement]:
      return {
         placement.placement_id: placement
         for stage in graph.stages
         for placement in stage.placements
      }

   @staticmethod
   def _placement_identity(placement: Placement) -> PlacementIdentity:
      return _placement_identity(placement)

   def _bound_placement(
      self,
      graph: ExecutionGraph,
      placement_id: str,
   ) -> tuple[Placement | None, str]:
      placement = self._placements_by_id(graph).get(placement_id)
      if placement is None:
         return None, "unknown_placement"
      identity = self._placement_identity(placement)
      epoch_identities = self._placement_identity_by_epoch.setdefault(
         graph.deployment_epoch,
         {},
      )
      original = epoch_identities.setdefault(placement_id, identity)
      if original != identity:
         return None, "placement_changed"
      return placement, ""

   def _evaluate_reserve(
      self,
      request: ReservationRequest,
      now: float,
      graph: ExecutionGraph,
   ) -> tuple[ReservationResult | None, Placement | None]:
      invalid_reason = self._validate_request_shape(request)
      if invalid_reason:
         return ReservationResult(False, reason=invalid_reason), None

      if request.deployment_epoch != graph.deployment_epoch:
         return (
            ReservationResult(False, reason="deployment_epoch_mismatch"),
            None,
         )
      placement, placement_reason = self._bound_placement(
         graph,
         request.placement_id,
      )
      if placement_reason:
         return ReservationResult(False, reason=placement_reason), None
      if placement is None:
         raise RuntimeError("missing_bound_placement")

      identity = self._identity(request)
      existing_id = self._reservation_by_identity.get(identity)
      if existing_id is not None:
         existing = self._records[existing_id]
         if existing.request != request:
            return ReservationResult(False, reason="idempotency_conflict"), None
         if existing.placement_identity != self._placement_identity(placement):
            return ReservationResult(False, reason="placement_changed"), None
         if existing.status == "RESERVED":
            return self._accepted(existing), placement
         if existing.status == "COMMITTED":
            if existing.request.lease_expires_at <= now:
               return (
                  ReservationResult(
                     False,
                     reason="reservation_already_committed",
                  ),
                  None,
               )
            return self._accepted(existing), placement
         if existing.status == "RELEASED":
            return ReservationResult(False, reason="reservation_released"), None
         return ReservationResult(False, reason="reservation_expired"), None

      if request.lease_expires_at <= now:
         return ReservationResult(False, reason="reservation_expired"), None
      capacity = self._node_capacity_kv_bytes.get(placement.node_id)
      if capacity is None:
         return ReservationResult(False, reason="capacity_unconfigured"), None
      charged = self._node_reserved_kv_bytes[placement.node_id]
      if charged + request.kv_bytes > capacity:
         return ReservationResult(False, reason="capacity_exceeded"), None
      return None, placement

   @staticmethod
   def _identity(request: ReservationRequest) -> ReservationIdentity:
      return (
         request.request_id,
         request.path_attempt,
         request.placement_id,
      )

   @staticmethod
   def _validate_request_shape(request: ReservationRequest) -> str:
      if not isinstance(request, ReservationRequest):
         return "invalid_reservation_request"
      if (
         not isinstance(request.request_id, str)
         or not request.request_id
         or not isinstance(request.path_id, str)
         or not request.path_id
         or not isinstance(request.placement_id, str)
         or not request.placement_id
      ):
         return "invalid_reservation_identity"
      if (
         isinstance(request.path_attempt, bool)
         or not isinstance(request.path_attempt, int)
         or request.path_attempt < 0
      ):
         return "invalid_path_attempt"
      if (
         isinstance(request.kv_bytes, bool)
         or not isinstance(request.kv_bytes, int)
         or request.kv_bytes < 0
      ):
         return "invalid_kv_bytes"
      if (
         isinstance(request.deployment_epoch, bool)
         or not isinstance(request.deployment_epoch, int)
         or request.deployment_epoch < 0
      ):
         return "invalid_deployment_epoch"
      if (
         not _is_finite_number(request.lease_expires_at)
      ):
         return "invalid_lease_expiry"
      return ""

   @staticmethod
   def _accepted(record: _ReservationRecord) -> ReservationResult:
      return ReservationResult(
         True,
         reservation_id=record.reservation_id,
         deployment_epoch=record.request.deployment_epoch,
         expires_at=record.request.lease_expires_at,
      )

   def _now(self) -> float:
      value = self._clock.now()
      if not _is_finite_number(value):
         raise ValueError("invalid_clock_value")
      return float(value)

   def _reap_expired(self, now: float) -> None:
      for reservation_id, record in tuple(self._records.items()):
         if (
            record.status == "RESERVED"
            and record.request.lease_expires_at <= now
         ):
            self._uncharge(record)
            self._records[reservation_id] = replace(
               record,
               status="EXPIRED",
            )

   def _uncharge(self, record: _ReservationRecord) -> None:
      current = self._node_reserved_kv_bytes[record.node_id]
      remaining = current - record.request.kv_bytes
      if remaining < 0:
         raise RuntimeError("capacity_accounting_underflow")
      self._node_reserved_kv_bytes[record.node_id] = remaining
