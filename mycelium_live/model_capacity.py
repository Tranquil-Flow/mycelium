"""Fresh, local-only model feasibility recomputation for the live product."""

from __future__ import annotations

import json
from pathlib import Path
import re
import threading
import time
from typing import Any, Callable, Mapping

from mycelium_layer_planner.contracts import (
    NodeCapability,
    PlanningPolicy,
    WorkloadScenario,
)
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


class ModelCapacityRefreshError(RuntimeError):
    """Bounded capacity-refresh lifecycle error."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _copy(value: Mapping[str, Any]) -> dict[str, Any]:
    return json.loads(json.dumps(dict(value), allow_nan=False))


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
    progress: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Rebuild the public operation from fresh signed evidence and local bytes only."""

    if progress is not None:
        progress("capturing_resources")
    evidence = swarm_feasibility_evidence_from_document(assemble(live_observations))
    placement = live_observations.get("placement")
    topology = live_observations.get("topology")
    if not isinstance(placement, Mapping) or not isinstance(topology, Mapping):
        raise ValueError("capacity_planner_projection_unavailable")
    nodes = _node_capabilities(placement, topology, workspace_bytes=workspace_bytes)
    if progress is not None:
        progress("scanning_local_models")
    entries = scan_huggingface_cache(Path(cache_root))
    catalog = catalog_document(entries, generation=evidence.generation)
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
            serving_quantization="int8-weight-only",
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
