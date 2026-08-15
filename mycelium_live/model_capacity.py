"""Fresh, local-only model feasibility recomputation for the live product."""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import stat
import threading
import time
from typing import Any, Callable, Mapping, Sequence

from mycelium_layer_planner.contracts import (
    NodeCapability,
    PlanningPolicy,
    WorkloadScenario,
)
from mycelium_member_models import reconcile_member_model_catalog
from mycelium_model_catalog import (
    catalog_document,
    evaluate_model_feasibility,
    model_operation_document,
    scan_huggingface_cache,
    swarm_feasibility_evidence_from_document,
)
from scripts.assemble_m17_swarm_evidence import assemble


CAPACITY_REFRESH_PROTOCOL = "mycelium.model_capacity_refresh.v1"
_PHASES = frozenset(
    {"capturing_resources", "scanning_local_models", "evaluating_models", "publishing"}
)
_SAFE_REASON = re.compile(r"^[a-z][a-z0-9_]{0,127}$")
_MEMBER_AUTHORITIES_PROTOCOL = "mycelium.member_model_inventory_authorities.v1"


class ModelCapacityRefreshError(RuntimeError):
    """Bounded capacity-refresh lifecycle error."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _copy(value: Mapping[str, Any]) -> dict[str, Any]:
    return json.loads(json.dumps(dict(value), allow_nan=False))


def _inventory_document(path: Path, *, private: bool) -> dict[str, Any]:
    candidate = Path(path)
    try:
        metadata = candidate.lstat()
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise ValueError("member_model_inventory_file_invalid") from exc
    if (
        not candidate.is_absolute()
        or resolved != candidate
        or not stat.S_ISREG(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or metadata.st_nlink != 1
        or metadata.st_size <= 0
        or metadata.st_size > 32 * 1024 * 1024
        or (private and stat.S_IMODE(metadata.st_mode) != 0o600)
    ):
        raise ValueError("member_model_inventory_file_invalid")
    try:
        value = json.loads(candidate.read_text("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("member_model_inventory_file_invalid") from exc
    if not isinstance(value, dict):
        raise ValueError("member_model_inventory_file_invalid")
    return value


def _member_catalog(
    *,
    entries: tuple[Any, ...],
    generation: int,
    inventory_files: tuple[Path, ...],
    authorities_file: Path | None,
    now_unix_ms: int,
) -> dict[str, object]:
    if not inventory_files and authorities_file is None:
        return catalog_document(entries, generation=generation)
    if not inventory_files or authorities_file is None:
        raise ValueError("member_model_inventory_configuration_incomplete")
    authority_document = _inventory_document(authorities_file, private=True)
    if (
        set(authority_document) != {"protocol", "members"}
        or authority_document.get("protocol") != _MEMBER_AUTHORITIES_PROTOCOL
        or not isinstance(authority_document.get("members"), list)
    ):
        raise ValueError("member_model_inventory_authorities_invalid")
    inventories = tuple(
        _inventory_document(path, private=False) for path in inventory_files
    )
    return reconcile_member_model_catalog(
        local_entries=entries,
        inventories=inventories,
        authorities=tuple(authority_document["members"]),
        now_unix_ms=now_unix_ms,
        generation=generation,
    )


def _node_capabilities(
    placement: Mapping[str, Any],
    topology: Mapping[str, Any],
    *,
    workspace_bytes: int,
) -> tuple[NodeCapability, ...]:
    records = placement.get("nodes")
    decision = topology.get("decision")
    order = decision.get("opened_order") if isinstance(decision, Mapping) else None
    if not isinstance(records, list) or not isinstance(order, list):
        raise ValueError("capacity_planner_projection_unavailable")
    by_id = {
        record.get("node_id"): record
        for record in records
        if isinstance(record, Mapping) and isinstance(record.get("node_id"), str)
    }
    if (
        not order
        or not all(isinstance(node_id, str) and node_id for node_id in order)
        or len(order) != len(set(order))
    ):
        raise ValueError("capacity_topology_order_invalid")
    result: list[NodeCapability] = []
    for node_id in order:
        record = by_id.get(node_id)
        if record is None:
            raise ValueError("capacity_placement_node_missing")
        result.append(
            NodeCapability(
                node_id=node_id,
                prefill_ms_per_layer_token=float(record["prefill_ms_per_layer_token"]),
                decode_ms_per_layer_token=float(record["decode_ms_per_layer_token"]),
                fast_memory_bytes=int(record["fast_allocatable_bytes"]),
                total_memory_bytes=int(record["total_allocatable_bytes"]),
                memory_bandwidth_Bps=1_000_000_000.0,
                spill_bandwidth_Bps=1_000_000_000.0,
                workspace_bytes=workspace_bytes,
            )
        )
    return tuple(result)


def _capacity_topology(
    placement: Mapping[str, Any], topology: object
) -> Mapping[str, Any]:
    if isinstance(topology, Mapping):
        decision = topology.get("decision")
        order = decision.get("opened_order") if isinstance(decision, Mapping) else None
        if isinstance(order, list) and order:
            return topology
    records = placement.get("nodes")
    if not isinstance(records, list) or not records:
        raise ValueError("capacity_planner_projection_unavailable")
    intervals: list[tuple[int, int, str]] = []
    for record in records:
        if not isinstance(record, Mapping):
            raise ValueError("capacity_planner_projection_unavailable")
        node_id = record.get("node_id")
        start = record.get("start_layer")
        end = record.get("end_layer_exclusive")
        if (
            not isinstance(node_id, str)
            or not node_id
            or type(start) is not int
            or type(end) is not int
            or start < 0
            or end <= start
        ):
            raise ValueError("capacity_planner_projection_unavailable")
        intervals.append((start, end, node_id))
    intervals.sort()
    if (
        len({node_id for _, _, node_id in intervals}) != len(intervals)
        or intervals[0][0] != 0
        or any(left[1] != right[0] for left, right in zip(intervals, intervals[1:]))
    ):
        raise ValueError("capacity_topology_order_invalid")
    return {
        "protocol": "mycelium.capacity_route_order.v1",
        "source": "validated_m13_placement_stage_order",
        "decision": {"opened_order": [node_id for _, _, node_id in intervals]},
        "route_ready": False,
    }


def recompute_model_operation(
    *,
    cache_root: Path,
    live_observations: Mapping[str, Any],
    evaluated_at_unix_ms: int,
    prompt_tokens: int = 256,
    output_tokens: int = 128,
    concurrency: int = 1,
    workspace_bytes: int = 536_870_912,
    required_decode_mode: str = "complete_context_replay",
    member_inventory_files: Sequence[Path] = (),
    member_inventory_authorities_file: Path | None = None,
    representation_authorization: Mapping[str, Any] | None = None,
    progress: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Rebuild the public operation from fresh signed evidence and local bytes only."""

    if progress is not None:
        progress("capturing_resources")
    placement = live_observations.get("placement")
    if not isinstance(placement, Mapping):
        raise ValueError("capacity_planner_projection_unavailable")
    topology = _capacity_topology(placement, live_observations.get("topology"))
    observations = _copy(live_observations)
    observations["topology"] = topology
    evidence = swarm_feasibility_evidence_from_document(assemble(observations))
    nodes = _node_capabilities(placement, topology, workspace_bytes=workspace_bytes)
    if progress is not None:
        progress("scanning_local_models")
    entries = scan_huggingface_cache(Path(cache_root))
    authorized_identity: tuple[str, str] | None = None
    if representation_authorization is not None:
        model_id = representation_authorization.get("model_id")
        revision = representation_authorization.get("revision")
        if not isinstance(model_id, str) or not isinstance(revision, str):
            raise ValueError("approved_serving_representation_invalid")
        authorized_identity = (model_id, revision)
        matches = [
            entry
            for entry in entries
            if entry.compatible
            and (entry.model_id, entry.revision) == authorized_identity
        ]
        if len(matches) != 1:
            raise ValueError("approved_serving_representation_model_unavailable")
    catalog = _member_catalog(
        entries=entries,
        generation=evidence.generation,
        inventory_files=tuple(Path(path) for path in member_inventory_files),
        authorities_file=member_inventory_authorities_file,
        now_unix_ms=evaluated_at_unix_ms,
    )
    workload = WorkloadScenario(
        name="live_interactive_capacity_v1",
        prompt_tokens=prompt_tokens,
        output_tokens=output_tokens,
        concurrency=concurrency,
    )
    policy = PlanningPolicy(memory_reserve_fraction=0.1, objective="balanced")
    if progress is not None:
        progress("evaluating_models")
    reports = [
        evaluate_model_feasibility(
            entry,
            ordered_nodes=nodes,
            workload=workload,
            policy=policy,
            evidence=evidence,
            evaluated_at_unix_ms=evaluated_at_unix_ms,
            required_decode_mode=required_decode_mode,
            serving_quantization=(
                str(representation_authorization["serving_quantization"])
                if representation_authorization is not None
                and (entry.model_id, entry.revision) == authorized_identity
                else "int8-weight-only"
            ),
            representation_authorization=(
                representation_authorization
                if representation_authorization is not None
                and (entry.model_id, entry.revision) == authorized_identity
                else None
            ),
        )
        for entry in entries
        if entry.compatible
    ]
    if progress is not None:
        progress("publishing")
    return model_operation_document(catalog, reports)


class ModelCapacityRefresh:
    """Single-flight background recomputation that never provisions or downloads."""

    def __init__(
        self,
        *,
        evaluator: Callable[[Callable[[str], None]], Mapping[str, Any]],
        operation_sink: Callable[[Mapping[str, Any]], None],
        clock_unix_ms: Callable[[], int] | None = None,
    ) -> None:
        if not callable(evaluator) or not callable(operation_sink):
            raise ValueError("capacity_refresh_callbacks_required")
        self._evaluator = evaluator
        self._operation_sink = operation_sink
        self._clock = clock_unix_ms or (lambda: int(time.time() * 1_000))
        self._lock = threading.RLock()
        self._generation = 1
        self._state = "idle"
        self._phase: str | None = None
        self._started_at: int | None = None
        self._completed_at: int | None = None
        self._operation_digest: str | None = None
        self._catalog_generation: int | None = None
        self._evaluated_model_count = 0
        self._reason_code: str | None = None
        self._worker: threading.Thread | None = None

    def _status_locked(self) -> dict[str, Any]:
        return {
            "protocol": CAPACITY_REFRESH_PROTOCOL,
            "generation": self._generation,
            "state": self._state,
            "phase": self._phase,
            "started_at_unix_ms": self._started_at,
            "completed_at_unix_ms": self._completed_at,
            "operation_digest": self._operation_digest,
            "catalog_generation": self._catalog_generation,
            "evaluated_model_count": self._evaluated_model_count,
            "reason_code": self._reason_code,
            "download_authorized": False,
            "provisioning_started": False,
        }

    def status(self) -> Mapping[str, Any]:
        with self._lock:
            return _copy(self._status_locked())

    def _progress(self, phase: str) -> None:
        if phase not in _PHASES:
            raise ValueError("capacity_refresh_phase_invalid")
        with self._lock:
            self._phase = phase
            self._generation += 1

    def _run(self) -> None:
        try:
            operation = self._evaluator(self._progress)
            if operation.get("protocol") != "mycelium.model_operation.v1":
                raise ValueError("capacity_refresh_operation_invalid")
            self._operation_sink(operation)
            reports = operation.get("feasibility_reports")
            with self._lock:
                self._state = "succeeded"
                self._phase = None
                self._completed_at = self._clock()
                self._operation_digest = str(operation["operation_digest"])
                self._catalog_generation = int(operation["catalog_generation"])
                self._evaluated_model_count = (
                    len(reports) if isinstance(reports, list) else 0
                )
                self._reason_code = None
                self._generation += 1
        except BaseException as exc:
            code = str(exc)
            if _SAFE_REASON.fullmatch(code) is None:
                code = "capacity_refresh_failed"
            with self._lock:
                self._state = "failed"
                self._phase = None
                self._completed_at = self._clock()
                self._reason_code = code
                self._generation += 1

    def start(self) -> Mapping[str, Any]:
        with self._lock:
            if self._state == "refreshing":
                raise ModelCapacityRefreshError("capacity_refresh_busy")
            self._state = "refreshing"
            self._phase = "capturing_resources"
            self._started_at = self._clock()
            self._completed_at = None
            self._reason_code = None
            self._generation += 1
            self._worker = threading.Thread(target=self._run, daemon=True)
            self._worker.start()
            return _copy(self._status_locked())

    def close(self) -> None:
        worker = self._worker
        if worker is not None:
            worker.join(timeout=5)
