# SPDX-License-Identifier: AGPL-3.0-or-later
"""M16 live admission ledger, bounded QoS queue, and privacy-reduced status."""

from __future__ import annotations

from dataclasses import dataclass
from functools import wraps
import hashlib
import json
import math
import threading
import time
from typing import Any, Mapping
import uuid

from mycelium_router.contracts import (
    ExecutionGraph,
    DeviceState,
    HopWorkItem,
    PathManifest,
    RequestContext,
    ReservationCommitResult,
    ReservationRequest,
    ReservationResult,
    RouterConfig,
)
from mycelium_router.routing import ProgressivePathBuilder, RoutePolicy, RoutingError
from mycelium_router.scheduler import BackpressureError, HopScheduler
from mycelium_router.scoring import RouteScorer
from mycelium_router.serialization import (
    execution_graph_to_dict,
    path_manifest_to_dict,
)
from mycelium_performance_budget import PerformanceBudgetError, validate_performance_budget_v3


PROTOCOL = "mycelium.concurrent_request_runtime.v1"
_PRIVATE_FIELDS = {
    "prompt",
    "response",
    "prompt_text",
    "response_text",
    "token_ids",
    "tokens",
    "activation",
    "activations",
    "tensor",
    "tensors",
    "kv_content",
    "credential",
    "secret",
    "runtime_endpoint",
    "endpoint_addr",
    "private_address",
    "artifact_root",
}
_TOP_LEVEL_FIELDS = {
    "protocol",
    "generated_at_monotonic_s",
    "deployment_id",
    "deployment_epoch",
    "topology_version",
    "graph_digest",
    "queue",
    "placements",
    "requests",
    "incidents",
    "batch_state",
    "claim_boundary",
    "performance_budgets",
}


class M16AdmissionError(RuntimeError):
    """Stable public admission failure without private request material."""

    def __init__(self, code: str, *, retry_after_seconds: float | None = None) -> None:
        self.code = code
        self.retry_after_seconds = retry_after_seconds
        super().__init__(code)


def _synchronized(method):
    @wraps(method)
    def locked(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)

    return locked


class _MonotonicClock:
    @staticmethod
    def now() -> float:
        return time.monotonic()


class _DistributedProtocolClock:
    """Unix-aligned lease time that remains monotonic within this process."""

    def __init__(self) -> None:
        self._unix_origin = time.time()
        self._monotonic_origin = time.monotonic()

    def now(self) -> float:
        return self._unix_origin + (time.monotonic() - self._monotonic_origin)


class _UuidIdSource:
    @staticmethod
    def new(prefix: str) -> str:
        return f"{prefix}-{uuid.uuid4().hex}"


def _digest(document: object) -> str:
    encoded = json.dumps(
        document,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


@dataclass
class _Reservation:
    reservation_id: str
    request_id: str
    path_id: str
    path_attempt: int
    placement_id: str
    deployment_epoch: int
    kv_bytes: int
    workspace_bytes: int
    memory_bytes: int
    expires_at: float
    committed: bool = False
    released: bool = False


class _ResourceLedgerCapacityPort:
    def __init__(
        self,
        *,
        graph: ExecutionGraph,
        placement_capacities: Mapping[str, Mapping[str, int]],
        clock: object,
        id_source: object,
    ) -> None:
        self._graph = graph
        self._clock = clock
        self._id_source = id_source
        self._placement_stage = {
            placement.placement_id: stage.stage_id
            for stage in graph.stages
            for placement in stage.placements
        }
        placement_ids = set(self._placement_stage)
        if set(placement_capacities) != placement_ids:
            raise ValueError("m16_capacity_placement_mismatch")
        self._capacities: dict[str, dict[str, int]] = {}
        for placement_id, raw in placement_capacities.items():
            expected = {
                "memory_capacity_bytes",
                "kv_capacity_bytes",
                "workspace_capacity_bytes",
                "maximum_reservations",
            }
            if (
                not isinstance(raw, Mapping)
                or set(raw) != expected
                or any(
                    not isinstance(value, int)
                    or isinstance(value, bool)
                    or value <= 0
                    for value in raw.values()
                )
            ):
                raise ValueError("m16_capacity_invalid")
            self._capacities[placement_id] = dict(raw)
        self._reservations: dict[str, _Reservation] = {}

    def reserve(self, request: ReservationRequest) -> ReservationResult:
        if request.placement_id not in self._capacities:
            return ReservationResult(False, reason="unknown_placement")
        existing = next(
            (
                item
                for item in self._reservations.values()
                if not item.released
                and item.request_id == request.request_id
                and item.path_id == request.path_id
                and item.path_attempt == request.path_attempt
                and item.placement_id == request.placement_id
            ),
            None,
        )
        if existing is not None:
            if (
                existing.deployment_epoch != request.deployment_epoch
                or existing.kv_bytes != request.kv_bytes
                or existing.expires_at <= self._clock.now()
            ):
                return ReservationResult(False, reason="conflicting_reservation")
            return ReservationResult(
                True,
                reservation_id=existing.reservation_id,
                deployment_epoch=existing.deployment_epoch,
                expires_at=existing.expires_at,
            )
        capacity = self._capacities[request.placement_id]
        active = self._active_for(request.placement_id)
        workspace_bytes = self._graph.activation_bytes * 2
        memory_bytes = request.kv_bytes + workspace_bytes
        if (
            len(active) >= capacity["maximum_reservations"]
            or sum(item.kv_bytes for item in active) + request.kv_bytes
            > capacity["kv_capacity_bytes"]
            or sum(item.workspace_bytes for item in active) + workspace_bytes
            > capacity["workspace_capacity_bytes"]
            or sum(item.memory_bytes for item in active) + memory_bytes
            > capacity["memory_capacity_bytes"]
        ):
            return ReservationResult(False, reason="resource_unavailable")
        reservation_id = self._id_source.new("reservation")
        record = _Reservation(
            reservation_id=reservation_id,
            request_id=request.request_id,
            path_id=request.path_id,
            path_attempt=request.path_attempt,
            placement_id=request.placement_id,
            deployment_epoch=request.deployment_epoch,
            kv_bytes=request.kv_bytes,
            workspace_bytes=workspace_bytes,
            memory_bytes=memory_bytes,
            expires_at=request.lease_expires_at,
        )
        self._reservations[reservation_id] = record
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
        if not reservation_ids or len(set(reservation_ids)) != len(reservation_ids):
            return ReservationCommitResult(False, "reservation_set_invalid")
        records = [self._reservations.get(identifier) for identifier in reservation_ids]
        if any(item is None for item in records):
            return ReservationCommitResult(False, "unknown_reservation")
        if any(
            item.released
            or item.deployment_epoch != deployment_epoch
            or item.expires_at <= self._clock.now()
            for item in records
            if item is not None
        ):
            return ReservationCommitResult(False, "reservation_not_live")
        if any(item.committed for item in records if item is not None):
            return ReservationCommitResult(False, "reservation_already_committed")
        for item in records:
            assert item is not None
            item.committed = True
        return ReservationCommitResult(True)

    def release(self, reservation_ids: tuple[str, ...]) -> None:
        for identifier in reservation_ids:
            record = self._reservations.get(identifier)
            if record is not None:
                record.released = True

    def release_request(self, request_id: str) -> None:
        self.release(
            tuple(
                item.reservation_id
                for item in self._reservations.values()
                if item.request_id == request_id and not item.released
            )
        )

    def request_reservations(self, request_id: str) -> tuple[_Reservation, ...]:
        return tuple(
            item
            for item in self._reservations.values()
            if item.request_id == request_id and not item.released
        )

    def placement_status(self) -> list[dict[str, Any]]:
        node_by_placement = {
            placement.placement_id: placement.node_id
            for stage in self._graph.stages
            for placement in stage.placements
        }
        output = []
        for placement_id in sorted(self._capacities):
            capacity = self._capacities[placement_id]
            active = self._active_for(placement_id)
            reserved_memory = sum(item.memory_bytes for item in active)
            reserved_kv = sum(item.kv_bytes for item in active)
            reserved_workspace = sum(item.workspace_bytes for item in active)
            output.append(
                {
                    "placement_id": placement_id,
                    "node_id": node_by_placement[placement_id],
                    "memory_capacity_bytes": capacity["memory_capacity_bytes"],
                    "reserved_memory_bytes": reserved_memory,
                    "free_memory_bytes": capacity["memory_capacity_bytes"] - reserved_memory,
                    "kv_capacity_bytes": capacity["kv_capacity_bytes"],
                    "reserved_kv_bytes": reserved_kv,
                    "free_kv_bytes": capacity["kv_capacity_bytes"] - reserved_kv,
                    "workspace_capacity_bytes": capacity["workspace_capacity_bytes"],
                    "reserved_workspace_bytes": reserved_workspace,
                    "free_workspace_bytes": capacity["workspace_capacity_bytes"] - reserved_workspace,
                    "active_reservations": len(active),
                    "maximum_reservations": capacity["maximum_reservations"],
                }
            )
        return output

    def _active_for(self, placement_id: str) -> list[_Reservation]:
        return [
            item
            for item in self._reservations.values()
            if item.placement_id == placement_id and not item.released
        ]


class M16RuntimeCoordinator:
    """Compose existing Router path/reservation/scheduler mechanisms for live use."""

    def __init__(
        self,
        *,
        graph: ExecutionGraph,
        device_states: Mapping[str, object],
        placement_capacities: Mapping[str, Mapping[str, int]],
        workload_profiles: Mapping[str, str],
        clock: object,
        id_source: object,
        config: RouterConfig,
        lease_clock: object | None = None,
        max_concurrent_requests: int = 1,
    ) -> None:
        if (
            not workload_profiles
            or any(qos not in {"interactive", "batch"} for qos in workload_profiles.values())
        ):
            raise ValueError("m16_workload_profiles_invalid")
        self._graph = graph
        self._states = dict(device_states)
        self._profiles = dict(workload_profiles)
        self._clock = clock
        self._lease_clock = clock if lease_clock is None else lease_clock
        self._config = config
        if (
            not isinstance(max_concurrent_requests, int)
            or isinstance(max_concurrent_requests, bool)
            or not 1 <= max_concurrent_requests <= 64
        ):
            raise ValueError("invalid_max_concurrent_requests")
        self._max_concurrent_requests = max_concurrent_requests
        self._lock = threading.RLock()
        self._ledger = _ResourceLedgerCapacityPort(
            graph=graph,
            placement_capacities=placement_capacities,
            clock=self._lease_clock,
            id_source=id_source,
        )
        self._builder = ProgressivePathBuilder(
            policy=RoutePolicy(RouteScorer(config)),
            capacity=self._ledger,
            id_source=id_source,
        )
        self._scheduler = HopScheduler(config)
        self._requests: dict[str, dict[str, Any]] = {}
        self._active_request_ids: set[str] = set()
        self._incidents: list[dict[str, Any]] = []
        self._incident_sequence = 0
        self._graph_digest = _digest(execution_graph_to_dict(graph))
        self._performance_budgets: list[dict[str, Any]] = []

    @property
    def maximum_concurrent_requests(self) -> int:
        return self._max_concurrent_requests

    @_synchronized
    def admit(
        self,
        request: RequestContext,
        *,
        workload_profile_id: str,
    ) -> PathManifest:
        expected_qos = self._profiles.get(workload_profile_id)
        if expected_qos is None or request.qos_class != expected_qos:
            raise M16AdmissionError("workload_not_qualified")
        if request.request_id in self._requests:
            raise M16AdmissionError("duplicate_request_id")
        try:
            build = self._builder.start(request, self._graph, path_attempt=0)
            while not self._builder.is_complete(build):
                build = self._builder.advance(
                    build,
                    self._states,
                    now=self._clock.now(),
                    lease_now=self._lease_clock.now(),
                )
            manifest = self._builder.lock(
                build,
                now=self._clock.now(),
                lease_now=self._lease_clock.now(),
            )
        except RoutingError as exc:
            self._record_incident(
                "admission_rejected",
                request.request_id,
                "deployment",
                "resource_unavailable",
            )
            raise M16AdmissionError("resource_unavailable") from exc
        path_digest = _digest(path_manifest_to_dict(manifest))
        queued_at = self._clock.now()
        item = HopWorkItem(
            request_id=request.request_id,
            path_id=manifest.path_id,
            path_attempt=manifest.path_attempt,
            phase="REQUEST",
            token_index=-1,
            hop_index=0,
            placement_id=manifest.ordered_hops[0].placement_id,
            qos_class=request.qos_class,
            deficit_ratio=0.0,
            enqueued_at=queued_at,
            idempotency_key=f"m16:{request.request_id}:{manifest.path_id}",
            payload=request.prompt_token_ids,
            lease_expires_at=min(hop.reservation_expires_at for hop in manifest.ordered_hops),
        )
        try:
            self._scheduler.enqueue(item)
        except BackpressureError as exc:
            self._ledger.release_request(request.request_id)
            self._record_incident(
                "backpressure",
                request.request_id,
                "queue",
                exc.reason,
                retry_after_seconds=exc.retry_after_seconds,
            )
            raise M16AdmissionError(
                "queue_full",
                retry_after_seconds=exc.retry_after_seconds,
            ) from exc
        self._requests[request.request_id] = {
            "request": request,
            "workload_profile_id": workload_profile_id,
            "manifest": manifest,
            "path_manifest_digest": path_digest,
            "phase": "queued",
            "queued_at": queued_at,
            "dispatch_at": None,
            "terminal_at": None,
            "terminal_state": None,
        }
        return manifest

    @_synchronized
    def next_dispatch(self) -> str | None:
        if (
            len(self._active_request_ids) >= self._max_concurrent_requests
            or self._scheduler.queue_depth() == 0
        ):
            return None
        item = self._scheduler.pop_next(now=self._clock.now())
        record = self._requests[item.request_id]
        if any(
            reservation.expires_at <= self._lease_clock.now()
            for reservation in self._ledger.request_reservations(item.request_id)
        ):
            self._ledger.release_request(item.request_id)
            record["phase"] = "failed"
            record["terminal_state"] = "reservation_expired"
            record["terminal_at"] = self._clock.now()
            self._record_incident(
                "reservation_expired",
                item.request_id,
                "request",
                "failed",
            )
            return self.next_dispatch()
        self._active_request_ids.add(item.request_id)
        record["phase"] = "prefill"
        record["dispatch_at"] = self._clock.now()
        return item.request_id

    @_synchronized
    def mark_phase(self, request_id: str, phase: str) -> None:
        if request_id not in self._active_request_ids or phase not in {
            "prefill",
            "first_token",
            "decode",
            "cleanup",
        }:
            raise ValueError("m16_phase_transition_invalid")
        self._requests[request_id]["phase"] = phase

    @_synchronized
    def complete(self, request_id: str, *, state: str = "completed") -> None:
        if request_id not in self._active_request_ids or state not in {
            "completed",
            "failed",
            "cancelled",
        }:
            raise ValueError("m16_completion_invalid")
        self._ledger.release_request(request_id)
        record = self._requests[request_id]
        record["phase"] = state
        record["terminal_state"] = state
        record["terminal_at"] = self._clock.now()
        self._active_request_ids.discard(request_id)

    @_synchronized
    def cancel(self, request_id: str) -> bool:
        record = self._requests.get(request_id)
        if record is None or record["terminal_state"] is not None:
            return False
        manifest = record["manifest"]
        self._scheduler.release_path(manifest.path_id)
        self._ledger.release_request(request_id)
        record["phase"] = "cancelled"
        record["terminal_state"] = "cancelled"
        record["terminal_at"] = self._clock.now()
        self._active_request_ids.discard(request_id)
        self._record_incident(
            "cancellation_cleanup",
            request_id,
            "request",
            "resources_released",
        )
        return True

    @_synchronized
    def status(self) -> dict[str, Any]:
        queue_items = [
            record
            for record in self._requests.values()
            if record["phase"] == "queued"
        ]
        document = {
            "protocol": PROTOCOL,
            "generated_at_monotonic_s": float(self._clock.now()),
            "deployment_id": self._graph.deployment_id,
            "deployment_epoch": self._graph.deployment_epoch,
            "topology_version": self._graph.topology_version,
            "graph_digest": self._graph_digest,
            "queue": {
                "depth": self._scheduler.queue_depth(),
                "maximum_items": self._config.maximum_pending_hops,
                "queued_bytes": self._scheduler.queued_payload_bytes(),
                "maximum_bytes": self._config.maximum_pending_bytes,
                "interactive_depth": sum(
                    item["request"].qos_class == "interactive" for item in queue_items
                ),
                "batch_depth": sum(
                    item["request"].qos_class == "batch" for item in queue_items
                ),
                "active_request_ids": sorted(self._active_request_ids),
                "maximum_active_requests": self._max_concurrent_requests,
            },
            "placements": self._ledger.placement_status(),
            "requests": [self._request_projection(item) for item in self._requests.values()],
            "incidents": list(self._incidents[-256:]),
            "batch_state": {
                "mode": "concurrent_request_sequential_stage_dispatch",
                "maximum_runtime_batch_size": self._config.maximum_runtime_batch_size,
                "observed_batches": [],
                "continuous_batching": False,
                "pipeline_overlap": False,
            },
            "claim_boundary": (
                "bounded admission, immutable paths, QoS queue, and cleanup; physical "
                "microbatch and pipeline overlap unclaimed until observed"
            ),
            "performance_budgets": list(self._performance_budgets),
        }
        return validate_m16_runtime_status(document)

    @_synchronized
    def phase(self, request_id: str) -> str | None:
        record = self._requests.get(request_id)
        return None if record is None else str(record["phase"])

    @_synchronized
    def route_identity(self, request_id: str) -> dict[str, Any]:
        """Return the Router-owned immutable identity for one admitted request.

        Downstream execution must consume this projection instead of rebuilding a
        path digest from the selected placements.  The projection deliberately
        excludes prompt/token material.
        """

        record = self._requests.get(request_id)
        if record is None:
            raise M16AdmissionError("request_not_admitted")
        request = record["request"]
        manifest = record["manifest"]
        return {
            "request_id": request.request_id,
            "request_attempt": 1,
            "path_id": manifest.path_id,
            "path_attempt": manifest.path_attempt,
            "path_manifest_digest": record["path_manifest_digest"],
            "deployment_id": self._graph.deployment_id,
            "deployment_epoch": manifest.deployment_epoch,
            "topology_generation": manifest.topology_version,
        }

    @_synchronized
    def path_manifest(self, request_id: str) -> dict[str, Any]:
        """Return the exact Router-owned manifest consumed by physical execution."""

        record = self._requests.get(request_id)
        if record is None:
            raise M16AdmissionError("request_not_admitted")
        return path_manifest_to_dict(record["manifest"])

    @_synchronized
    def attach_performance_budget(self, document: Mapping[str, Any]) -> None:
        validated = validate_performance_budget_v3(document)
        self._performance_budgets = [validated]

    def _request_projection(self, record: Mapping[str, Any]) -> dict[str, Any]:
        request = record["request"]
        manifest = record["manifest"]
        dispatch_at = record["dispatch_at"]
        return {
            "request_id": request.request_id,
            "workload_profile_id": record["workload_profile_id"],
            "qos_class": request.qos_class,
            "phase": record["phase"],
            "path_id": manifest.path_id,
            "path_attempt": manifest.path_attempt,
            "path_manifest_digest": record["path_manifest_digest"],
            "topology_version": manifest.topology_version,
            "path_state": "locked",
            "candidate_placement_ids": [
                hop.placement_id for hop in manifest.ordered_hops
            ],
            "placement_ids": [hop.placement_id for hop in manifest.ordered_hops],
            "reservation_count": len(self._ledger.request_reservations(request.request_id)),
            "admitted_at_monotonic_s": float(request.admitted_at),
            "queued_at_monotonic_s": float(record["queued_at"]),
            "dispatch_at_monotonic_s": None if dispatch_at is None else float(dispatch_at),
            "terminal_at_monotonic_s": None if record["terminal_at"] is None else float(record["terminal_at"]),
            "queue_wait_ms": None if dispatch_at is None else (dispatch_at - record["queued_at"]) * 1_000.0,
            "terminal_state": record["terminal_state"],
        }

    def _record_incident(
        self,
        kind: str,
        request_id: str,
        scope: str,
        state: str,
        *,
        retry_after_seconds: float | None = None,
    ) -> None:
        self._incident_sequence += 1
        self._incidents.append(
            {
                "incident_id": f"m16-incident-{self._incident_sequence}",
                "kind": kind,
                "request_id": request_id,
                "scope": scope,
                "state": state,
                "observed_at_monotonic_s": float(self._clock.now()),
                "retry_after_seconds": retry_after_seconds,
            }
        )


def _reject_private(value: object) -> None:
    if isinstance(value, Mapping):
        if _PRIVATE_FIELDS.intersection(value):
            raise ValueError("M16 runtime status contains private content")
        for item in value.values():
            _reject_private(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _reject_private(item)


def _finite(value: object) -> None:
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("M16 runtime status contains non-finite value")
    if isinstance(value, Mapping):
        for item in value.values():
            _finite(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _finite(item)


def validate_m16_runtime_status(document: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(document, Mapping) or set(document) != _TOP_LEVEL_FIELDS:
        raise ValueError("M16 runtime status shape is invalid")
    if document.get("protocol") != PROTOCOL:
        raise ValueError("M16 runtime status protocol is invalid")
    if not isinstance(document.get("queue"), Mapping) or set(document["queue"]) != {
        "depth",
        "maximum_items",
        "queued_bytes",
        "maximum_bytes",
        "interactive_depth",
        "batch_depth",
        "active_request_ids",
        "maximum_active_requests",
    }:
        raise ValueError("M16 runtime status queue shape is invalid")
    placement_fields = {
        "placement_id",
        "node_id",
        "memory_capacity_bytes",
        "reserved_memory_bytes",
        "free_memory_bytes",
        "kv_capacity_bytes",
        "reserved_kv_bytes",
        "free_kv_bytes",
        "workspace_capacity_bytes",
        "reserved_workspace_bytes",
        "free_workspace_bytes",
        "active_reservations",
        "maximum_reservations",
    }
    request_fields = {
        "request_id",
        "workload_profile_id",
        "qos_class",
        "phase",
        "path_id",
        "path_attempt",
        "path_manifest_digest",
        "topology_version",
        "path_state",
        "candidate_placement_ids",
        "placement_ids",
        "reservation_count",
        "admitted_at_monotonic_s",
        "queued_at_monotonic_s",
        "dispatch_at_monotonic_s",
        "terminal_at_monotonic_s",
        "queue_wait_ms",
        "terminal_state",
    }
    incident_fields = {
        "incident_id",
        "kind",
        "request_id",
        "scope",
        "state",
        "observed_at_monotonic_s",
        "retry_after_seconds",
    }
    if (
        not isinstance(document.get("placements"), list)
        or not document["placements"]
        or any(not isinstance(item, Mapping) or set(item) != placement_fields for item in document["placements"])
        or not isinstance(document.get("requests"), list)
        or len(document["requests"]) > 1_024
        or any(not isinstance(item, Mapping) or set(item) != request_fields for item in document["requests"])
        or not isinstance(document.get("incidents"), list)
        or len(document["incidents"]) > 256
        or any(not isinstance(item, Mapping) or set(item) != incident_fields for item in document["incidents"])
        or not isinstance(document.get("batch_state"), Mapping)
        or set(document["batch_state"]) != {
            "mode",
            "maximum_runtime_batch_size",
            "observed_batches",
            "continuous_batching",
            "pipeline_overlap",
        }
        or not isinstance(document.get("performance_budgets"), list)
        or len(document["performance_budgets"]) > 16
    ):
        raise ValueError("M16 runtime status nested shape is invalid")
    try:
        document = dict(document)
        document["performance_budgets"] = [
            validate_performance_budget_v3(item)
            for item in document["performance_budgets"]
        ]
    except (PerformanceBudgetError, TypeError) as exc:
        raise ValueError("M16 runtime status budget shape is invalid") from exc
    _reject_private(document)
    _finite(document)
    try:
        return json.loads(json.dumps(document, sort_keys=True, allow_nan=False))
    except (TypeError, ValueError, RecursionError) as exc:
        raise ValueError("M16 runtime status is not JSON-safe") from exc


def build_live_m16_runtime(
    graph: ExecutionGraph,
    *,
    placement_projection: Mapping[str, Any] | None = None,
    workload_comparison: Mapping[str, Any] | None = None,
    config: RouterConfig | None = None,
) -> M16RuntimeCoordinator:
    """Build the live coordinator from public M13/M15 evidence only."""

    projected_nodes = {
        item.get("node_id"): item
        for item in (
            placement_projection.get("nodes", [])
            if isinstance(placement_projection, Mapping)
            else []
        )
        if isinstance(item, Mapping) and isinstance(item.get("node_id"), str)
    }
    placements_by_node: dict[str, list[str]] = {}
    stages_by_placement = {}
    for stage in graph.stages:
        for placement in stage.placements:
            placements_by_node.setdefault(placement.node_id, []).append(
                placement.placement_id
            )
            stages_by_placement[placement.placement_id] = stage
    states: dict[str, DeviceState] = {}
    capacities: dict[str, dict[str, int]] = {}
    for node_id, placement_ids in placements_by_node.items():
        projection = projected_nodes.get(node_id, {})
        allocatable = projection.get("fast_allocatable_bytes", 536_870_912)
        if not isinstance(allocatable, int) or isinstance(allocatable, bool) or allocatable <= 0:
            allocatable = 536_870_912
        per_placement = max(1_048_576, allocatable // len(placement_ids))
        decode_ms = projection.get("decode_ms_per_layer_token", 1.0)
        compute_rate = 1_000.0 / decode_ms if isinstance(decode_ms, (int, float)) and decode_ms > 0 else 1_000.0
        neighbours = {
            candidate: 1.0
            for candidate in placements_by_node
            if candidate != node_id
        }
        states[node_id] = DeviceState(
            node_id=node_id,
            state_seq=1,
            last_updated=time.monotonic(),
            availability="ALIVE",
            compute_units_per_second=float(compute_rate),
            free_compute_fraction=1.0,
            available_kv_bytes=per_placement * len(placement_ids) // 2,
            pending_hop_queue_depth=0,
            neighbor_rtt_ms=dict(neighbours),
            neighbor_bandwidth_bytes_per_second={
                candidate: 1_000_000_000.0 for candidate in neighbours
            },
        )
        for placement_id in placement_ids:
            stage = stages_by_placement[placement_id]
            minimum_workspace = max(graph.activation_bytes * 2, 1)
            workspace = max(minimum_workspace, per_placement // 4)
            kv = max(
                stage.stage_cost.kv_bytes_per_context_token * 4_096,
                per_placement // 2,
            )
            capacities[placement_id] = {
                "memory_capacity_bytes": max(per_placement, workspace + kv),
                "kv_capacity_bytes": kv,
                "workspace_capacity_bytes": workspace,
                "maximum_reservations": 64,
            }
    profiles: dict[str, str] = {}
    if isinstance(workload_comparison, Mapping):
        for profile in workload_comparison.get("profiles", []):
            if not isinstance(profile, Mapping):
                continue
            scenarios = profile.get("scenarios")
            qos = (
                scenarios[0].get("qos_class")
                if isinstance(scenarios, list)
                and scenarios
                and isinstance(scenarios[0], Mapping)
                else None
            )
            if isinstance(profile.get("profile_id"), str) and qos in {"interactive", "batch"}:
                profiles[profile["profile_id"]] = qos
    if not profiles:
        profiles = {
            "interactive_chat_v1": "interactive",
            "sustained_batch_v1": "batch",
        }
    return M16RuntimeCoordinator(
        graph=graph,
        device_states=states,
        placement_capacities=capacities,
        workload_profiles=profiles,
        clock=_MonotonicClock(),
        lease_clock=_DistributedProtocolClock(),
        id_source=_UuidIdSource(),
        config=config or RouterConfig(reservation_lease_seconds=3_600.0),
        max_concurrent_requests=4,
    )


__all__ = [
    "M16AdmissionError",
    "M16RuntimeCoordinator",
    "PROTOCOL",
    "build_live_m16_runtime",
    "validate_m16_runtime_status",
]
