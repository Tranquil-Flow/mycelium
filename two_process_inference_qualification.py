#!/usr/bin/env python3
"""Phase 6 local Router execution qualification with two spawned MLX workers.

Exactly two persistent ``spawn`` children load the assignment-bound stages of a
locally generated tiny GPT-2 model.  The parent binds each child to the resulting
immutable ExecutionGraph, exposes the child MLXRuntimePort through a bounded pipe
RPC proxy, and composes two production Routers over LoopbackSocketMesh.  This is
an execution qualification, not a production transport or benchmark.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import multiprocessing
import os
import sys
import tempfile
import threading
import time
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from pathlib import Path
from types import MappingProxyType
from typing import Any, NoReturn

import mlx.core as mx

from layer_assignment import compile_layer_assignments
from model_manifest import manifest_digest_ref
from mycelium_router.contracts import (
    DeviceState,
    ExecutionGraph,
    HopHeader,
    HopWorkItem,
    LayerRange,
    Placement,
    PlacementEdge,
    ProgressivePrefillMessage,
    RequestContext,
    RouterConfig,
    RuntimeBatch,
    RuntimeResult,
    Stage,
    StageCost,
)
from mycelium_router.layer_builder import layer_load_proof_digest
from mycelium_router.live_ports import (
    InProcessLeaseCapacityPort,
    PublishedDeviceStateProvider,
    PublishedTopologyProvider,
)
from mycelium_router.mlx_runtime import MLXRuntimePort, _stage_signature
from mycelium_router.payloads import decode_activation, encode_activation
from mycelium_router.relay import RelayEngine
from mycelium_router.router import Router
from mycelium_router.transports.loopback_socket import LoopbackSocketMesh
from mycelium_router.wire import ROUTER_WIRE_PROTOCOL, decode_frame
from runtime_loader import canonical_json, execute_loaded_stage, load_assignment_stage
from two_process_runtime_qualification import (
    DEPLOYMENT_EPOCH,
    DEPLOYMENT_ID,
    LOAD_GENERATION,
    _LocalOnlyFetcher,
    _build_local_model,
    _control_plane_binding,
    _install_network_audit_guard,
    _route_for_manifest,
)
from weight_provisioning import artifact_report_errors, provision_assignment


QUALIFICATION_PROTOCOL = "mycelium.two_process_inference_qualification.v1"
RUNTIME_RPC_PROTOCOL = "mycelium.local_runtime_pipe_rpc.v1"
KV_NUMERIC_TOLERANCE = 1e-5
CLAIM = (
    "Two persistent spawned local MLX runtime workers executed their exact "
    "assignment-bound stages through two production Routers, production relay "
    "logic, and versioned Router frames over loopback TCP with reference parity."
)
CLAIM_BOUNDARY = (
    "Qualified only for local loopback TCP and bounded parent/child pipes: exactly "
    "two assignment-bound MLX runtime workers exercised stage-local KV-backed "
    "prefill/decode through the Router path; no authenticated multi-host transport "
    "or performance claim."
)
NEGATIVE_CLAIMS = (
    "No authenticated multi-host transport or remote-peer security was demonstrated.",
    "PREFILL_CHUNK continuity remains disabled and is not qualified.",
    "No performance, throughput, latency, scalability, or device-residency claim is made.",
    (
        "Process-local lease capacity and local loopback/pipe coordination are "
        "not distributed consensus."
    ),
)

_LOAD_ENVELOPE_FIELDS = frozenset(
    {
        "protocol",
        "kind",
        "pid",
        "start_method",
        "assignment_id",
        "node_id",
        "assigned_range",
        "proof_json",
        "proof_sha256",
        "load_proof_immutable",
        "network_event_count",
    }
)
_RPC_REQUEST_FIELDS = frozenset(
    {"protocol", "kind", "request_id", "operation", "payload"}
)
_RPC_RESPONSE_FIELDS = frozenset(
    {"protocol", "kind", "request_id", "ok", "result", "error"}
)
_SUPPORTED_OPERATIONS = frozenset(
    {"bind_graph", "execute", "execute_batch", "cancel", "snapshot", "shutdown"}
)


class QualificationError(RuntimeError):
    """Fail-closed qualification error, optionally carrying spawned child PIDs."""

    def __init__(self, message: str, *, child_pids: Sequence[int] = ()) -> None:
        super().__init__(message)
        self.child_pids = tuple(child_pids)


def _fail(message: str) -> NoReturn:
    raise QualificationError(message)


def _canonical_json(document: Any) -> str:
    return json.dumps(
        document,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def _sha256(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _require_parity(actual: Any, expected: Any, label: str) -> None:
    """Reject any qualification reference mismatch without coercion."""

    if actual != expected:
        raise QualificationError(
            f"{label} parity mismatch: actual={actual!r}, expected={expected!r}"
        )


def _deeply_immutable(value: Any) -> bool:
    if isinstance(value, MappingProxyType):
        return all(
            isinstance(key, str) and _deeply_immutable(item)
            for key, item in value.items()
        )
    if isinstance(value, tuple):
        return all(_deeply_immutable(item) for item in value)
    return value is None or isinstance(value, (str, int, float, bool))


def _response(
    request_id: int,
    *,
    ok: bool,
    result: Any = None,
    error: Any = None,
) -> dict[str, Any]:
    return {
        "protocol": RUNTIME_RPC_PROTOCOL,
        "kind": "response",
        "request_id": request_id,
        "ok": ok,
        "result": result,
        "error": error,
    }


def _runtime_call_evidence(operation: str, item: Any, result: RuntimeResult) -> dict[str, Any]:
    output_payload = result.payload if isinstance(result.payload, bytes) else None
    input_payload = item.payload if hasattr(item, "payload") else None
    return {
        "operation": operation,
        "phase": getattr(item, "phase", ""),
        "placement_id": getattr(item, "placement_id", ""),
        "request_id": getattr(item, "request_id", ""),
        "path_id": getattr(item, "path_id", ""),
        "path_attempt": getattr(item, "path_attempt", -1),
        "token_index": getattr(item, "token_index", -1),
        "position": getattr(item, "position", -1),
        "terminal": getattr(item, "terminal", False),
        "input_sequence_tokens": getattr(
            getattr(item, "batch_key", None), "token_span", 0
        ),
        "hop_index": getattr(item, "hop_index", -1),
        "idempotency_key": getattr(item, "idempotency_key", ""),
        "input_payload_sha256": (
            _sha256(input_payload) if isinstance(input_payload, bytes) else ""
        ),
        "success": result.success,
        "token_id": result.token_id,
        "output_payload_bytes": len(output_payload) if output_payload is not None else 0,
        "output_payload_sha256": (
            _sha256(output_payload) if output_payload is not None else ""
        ),
        "failure_scope": result.failure_scope,
        "failure_reason": result.failure_reason,
    }


def _runtime_worker(
    connection: Any,
    assignment: dict[str, Any],
    artifact_report: dict[str, Any],
    load_generation: int,
) -> None:
    """Load one exact stage, bind MLXRuntimePort, and serve bounded parent RPCs."""

    network_events: list[str] = []
    _install_network_audit_guard(network_events)
    try:
        loaded = load_assignment_stage(
            assignment,
            artifact_report,
            load_generation=load_generation,
        )
        proof_json = canonical_json(loaded.proof)
        load_envelope = {
            "protocol": RUNTIME_RPC_PROTOCOL,
            "kind": "load_proof",
            "pid": os.getpid(),
            "start_method": multiprocessing.get_start_method(),
            "assignment_id": assignment["assignment_id"],
            "node_id": assignment["node_id"],
            "assigned_range": json.loads(_canonical_json(assignment["range"])),
            "proof_json": proof_json,
            "proof_sha256": _sha256(proof_json.encode("utf-8")),
            "load_proof_immutable": _deeply_immutable(loaded.proof),
            "network_event_count": len(network_events),
        }
        connection.send(load_envelope)
    except BaseException as exc:
        try:
            connection.send(
                {
                    "protocol": RUNTIME_RPC_PROTOCOL,
                    "kind": "load_error",
                    "pid": os.getpid(),
                    "error": {"type": type(exc).__name__, "message": str(exc)},
                }
            )
        except (BrokenPipeError, EOFError, OSError):
            pass
        connection.close()
        return

    runtime: MLXRuntimePort | None = None
    graph: ExecutionGraph | None = None
    calls: list[dict[str, Any]] = []
    cancellations: list[str] = []

    def snapshot() -> dict[str, Any]:
        phases = [item["phase"] for item in calls]
        return {
            "pid": os.getpid(),
            "assignment_id": assignment["assignment_id"],
            "node_id": assignment["node_id"],
            "assigned_range": json.loads(_canonical_json(assignment["range"])),
            "runtime_bound": runtime is not None,
            "graph_deployment_id": graph.deployment_id if graph is not None else "",
            "phases": phases,
            "phase_counts": dict(Counter(phases)),
            "rpc_call_count": len(calls),
            "calls": list(calls),
            "cancellations": list(cancellations),
            "network_event_count": len(network_events),
            "kv": runtime.kv_snapshot() if runtime is not None else None,
        }

    try:
        while True:
            try:
                request = connection.recv()
            except EOFError:
                break
            request_id = request.get("request_id", -1) if isinstance(request, dict) else -1
            try:
                if not isinstance(request, dict) or set(request) != _RPC_REQUEST_FIELDS:
                    raise QualificationError("malformed runtime RPC request")
                if (
                    request["protocol"] != RUNTIME_RPC_PROTOCOL
                    or request["kind"] != "request"
                    or not isinstance(request["request_id"], int)
                    or isinstance(request["request_id"], bool)
                    or request["request_id"] <= 0
                ):
                    raise QualificationError("malformed runtime RPC request")
                operation = request["operation"]
                if operation not in _SUPPORTED_OPERATIONS:
                    raise QualificationError("unsupported runtime RPC operation")
                payload = request["payload"]

                if operation == "bind_graph":
                    if runtime is not None:
                        raise QualificationError("runtime graph already bound")
                    if (
                        not isinstance(payload, tuple)
                        or len(payload) != 2
                        or not isinstance(payload[0], ExecutionGraph)
                        or (
                            payload[1] is not None
                            and (
                                not isinstance(payload[1], (int, float))
                                or isinstance(payload[1], bool)
                                or not math.isfinite(float(payload[1]))
                            )
                        )
                    ):
                        raise QualificationError("invalid runtime graph binding")
                    bound_graph, clock_now = payload
                    local = next(
                        (
                            placement
                            for stage in bound_graph.stages
                            for placement in stage.placements
                            if placement.node_id == assignment["node_id"]
                            and placement.assignment_id == assignment["assignment_id"]
                        ),
                        None,
                    )
                    if local is None:
                        raise QualificationError("assignment placement missing from graph")
                    runtime = MLXRuntimePort(
                        assignment["node_id"],
                        bound_graph,
                        {local.placement_id: loaded},
                        clock=(None if clock_now is None else lambda: float(clock_now)),
                    )
                    graph = bound_graph
                    result: Any = {
                        "pid": os.getpid(),
                        "node_id": assignment["node_id"],
                        "assignment_id": assignment["assignment_id"],
                        "placement_id": local.placement_id,
                        "runtime": "mycelium_router.mlx_runtime.MLXRuntimePort",
                    }
                elif operation == "shutdown":
                    if payload is not None:
                        raise QualificationError("invalid runtime shutdown RPC payload")
                    if runtime is not None:
                        runtime.close(reason="worker_shutdown")
                    result = snapshot()
                    connection.send(_response(request_id, ok=True, result=result))
                    break
                else:
                    if runtime is None:
                        raise QualificationError("runtime graph is not bound")
                    if operation == "execute":
                        if not isinstance(payload, HopWorkItem):
                            raise QualificationError("invalid runtime execute RPC payload")
                        result = runtime.execute(payload)
                        if not isinstance(result, RuntimeResult):
                            raise QualificationError("runtime returned invalid result")
                        calls.append(_runtime_call_evidence(operation, payload, result))
                    elif operation == "execute_batch":
                        if not isinstance(payload, RuntimeBatch):
                            raise QualificationError("invalid runtime batch RPC payload")
                        result = runtime.execute_batch(payload)
                        if (
                            not isinstance(result, tuple)
                            or len(result) != len(payload.items)
                            or any(not isinstance(item, RuntimeResult) for item in result)
                        ):
                            raise QualificationError("invalid runtime batch RPC result")
                        for item, item_result in zip(payload.items, result):
                            calls.append(
                                _runtime_call_evidence(operation, item, item_result)
                            )
                    elif operation == "cancel":
                        if not isinstance(payload, str) or not payload:
                            raise QualificationError("invalid runtime cancellation payload")
                        runtime.cancel(payload)
                        cancellations.append(payload)
                        result = None
                    elif operation == "snapshot":
                        if payload is not None:
                            raise QualificationError("invalid runtime snapshot RPC payload")
                        result = snapshot()
                    else:  # pragma: no cover - guarded by the supported set.
                        raise QualificationError("unsupported runtime RPC operation")
                connection.send(_response(request_id, ok=True, result=result))
            except BaseException as exc:
                try:
                    connection.send(
                        _response(
                            request_id if isinstance(request_id, int) else -1,
                            ok=False,
                            error={"type": type(exc).__name__, "message": str(exc)},
                        )
                    )
                except (BrokenPipeError, EOFError, OSError):
                    break
    finally:
        connection.close()


WorkerTarget = Callable[[Any, dict[str, Any], dict[str, Any], int], None]


def _cleanup_processes(processes: Sequence[Any]) -> None:
    def alive(process: Any) -> bool:
        try:
            return bool(process.is_alive())
        except (AssertionError, OSError, ValueError):
            return False

    for process in processes:
        if alive(process):
            try:
                process.terminate()
            except (AssertionError, OSError, ValueError):
                pass
    for process in processes:
        if process.pid is not None:
            try:
                process.join(timeout=1.0)
            except (AssertionError, OSError, ValueError):
                pass
    for process in processes:
        if alive(process):
            try:
                process.kill()
            except (AssertionError, OSError, ValueError):
                pass
    for process in processes:
        if process.pid is not None:
            try:
                process.join(timeout=1.0)
            except (AssertionError, OSError, ValueError):
                pass


class _WorkerChannel:
    """Synchronous, request-correlated, timeout-bounded parent pipe endpoint."""

    def __init__(
        self,
        *,
        process: Any,
        connection: Any,
        timeout_seconds: float,
        proof_evidence: dict[str, Any],
    ) -> None:
        self.process = process
        self.connection = connection
        self.timeout_seconds = timeout_seconds
        self.proof_evidence = proof_evidence
        self._next_request_id = 1
        self._lock = threading.Lock()
        self.closed = False

    def _poison(self) -> None:
        self.close()

    @staticmethod
    def _bounded_io(operation: Callable[[], Any], timeout: float) -> Any:
        completed = threading.Event()
        values: list[Any] = []
        failures: list[BaseException] = []

        def invoke() -> None:
            try:
                values.append(operation())
            except BaseException as exc:
                failures.append(exc)
            finally:
                completed.set()

        thread = threading.Thread(target=invoke, daemon=True)
        thread.start()
        if not completed.wait(timeout=max(0.0, timeout)):
            raise TimeoutError
        if failures:
            raise failures[0]
        return values[0] if values else None

    def request(self, operation: str, payload: Any) -> Any:
        deadline = time.monotonic() + self.timeout_seconds
        if not self._lock.acquire(timeout=self.timeout_seconds):
            self._poison()
            raise QualificationError(
                f"runtime worker {self.process.pid} RPC timed out acquiring channel"
            )
        try:
            if self.closed:
                raise QualificationError("runtime worker connection is closed")
            request_id = self._next_request_id
            self._next_request_id += 1
            request = {
                "protocol": RUNTIME_RPC_PROTOCOL,
                "kind": "request",
                "request_id": request_id,
                "operation": operation,
                "payload": payload,
            }
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError
            try:
                self._bounded_io(lambda: self.connection.send(request), remaining)
            except TimeoutError as exc:
                raise QualificationError(
                    f"runtime worker {self.process.pid} RPC timed out during send"
                ) from exc
            except (BrokenPipeError, EOFError, OSError) as exc:
                raise QualificationError(
                    f"runtime worker {self.process.pid} IPC send failed"
                ) from exc

            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise QualificationError(
                        f"runtime worker {self.process.pid} RPC timed out"
                    )
                if self.connection.poll(min(0.05, remaining)):
                    try:
                        response = self._bounded_io(self.connection.recv, remaining)
                    except TimeoutError as exc:
                        raise QualificationError(
                            f"runtime worker {self.process.pid} RPC timed out during receive"
                        ) from exc
                    except (EOFError, OSError) as exc:
                        raise QualificationError(
                            f"runtime worker {self.process.pid} RPC channel closed"
                        ) from exc
                    break
                if not self.process.is_alive():
                    raise QualificationError(
                        f"runtime worker {self.process.pid} crashed during RPC; "
                        f"exit_code={self.process.exitcode}"
                    )

            if (
                not isinstance(response, dict)
                or set(response) != _RPC_RESPONSE_FIELDS
                or response.get("protocol") != RUNTIME_RPC_PROTOCOL
                or response.get("kind") != "response"
                or type(response.get("request_id")) is not int
                or response.get("request_id") != request_id
                or type(response.get("ok")) is not bool
            ):
                raise QualificationError(
                    f"malformed runtime RPC response from child {self.process.pid}"
                )
            if response["ok"] is not True:
                raise QualificationError(
                    f"runtime worker {self.process.pid} rejected {operation}: "
                    f"{response['error']!r}"
                )
            if response["error"] is not None:
                raise QualificationError(
                    f"malformed runtime RPC response from child {self.process.pid}"
                )
            return response["result"]
        except BaseException:
            self._poison()
            raise
        finally:
            self._lock.release()

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        try:
            self.connection.close()
        except OSError:
            pass


class _MLXWorkerProxy:
    """Router RuntimePort proxy backed by one assignment-bound child."""

    decode_mode = "stage_local_kv"

    def __init__(self, channel: _WorkerChannel) -> None:
        self.channel = channel

    def execute(self, item: Any) -> RuntimeResult:
        result = self.channel.request("execute", item)
        if not isinstance(result, RuntimeResult):
            raise QualificationError("runtime worker returned invalid RuntimeResult")
        return result

    def execute_batch(self, batch: RuntimeBatch) -> tuple[RuntimeResult, ...]:
        result = self.channel.request("execute_batch", batch)
        if (
            not isinstance(result, tuple)
            or any(not isinstance(item, RuntimeResult) for item in result)
        ):
            raise QualificationError("runtime worker returned invalid batch result")
        if len(result) != len(batch.items):
            raise QualificationError("runtime worker batch result cardinality mismatch")
        return result

    def cancel(self, path_id: str) -> None:
        result = self.channel.request("cancel", path_id)
        if result is not None:
            raise QualificationError("runtime worker returned invalid cancellation result")


class _RuntimeWorkerSet:
    def __init__(
        self,
        channels: Sequence[_WorkerChannel],
        processes: Sequence[Any],
    ) -> None:
        self.channels = tuple(channels)
        self.processes = tuple(processes)
        self.child_pids = tuple(process.pid for process in self.processes)
        self.exit_codes: list[int] = []
        self.clean_shutdown = False

    @property
    def proxies(self) -> tuple[_MLXWorkerProxy, ...]:
        return tuple(_MLXWorkerProxy(channel) for channel in self.channels)

    def bind_graph(
        self,
        graph: ExecutionGraph,
        *,
        clock_now: float | None = None,
    ) -> list[dict[str, Any]]:
        bindings: list[dict[str, Any]] = []
        for channel in self.channels:
            result = channel.request("bind_graph", (graph, clock_now))
            if not isinstance(result, dict) or set(result) != {
                "pid",
                "node_id",
                "assignment_id",
                "placement_id",
                "runtime",
            }:
                raise QualificationError("malformed runtime graph binding evidence")
            if (
                result["pid"] != channel.process.pid
                or result["node_id"] != channel.proof_evidence["node_id"]
                or result["assignment_id"]
                != channel.proof_evidence["assignment_id"]
                or result["runtime"]
                != "mycelium_router.mlx_runtime.MLXRuntimePort"
            ):
                raise QualificationError("runtime graph binding identity mismatch")
            bindings.append(result)
        return bindings

    def snapshots(self) -> list[dict[str, Any]]:
        results = [channel.request("snapshot", None) for channel in self.channels]
        if any(not isinstance(item, dict) for item in results):
            raise QualificationError("malformed runtime snapshot evidence")
        return results

    def shutdown(self) -> list[int]:
        if self.clean_shutdown:
            return list(self.exit_codes)
        try:
            for channel in self.channels:
                result = channel.request("shutdown", None)
                if not isinstance(result, dict) or result.get("pid") != channel.process.pid:
                    raise QualificationError("malformed runtime shutdown evidence")
            deadline = time.monotonic() + max(
                channel.timeout_seconds for channel in self.channels
            )
            for process in self.processes:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise QualificationError("runtime worker shutdown timed out")
                process.join(timeout=remaining)
                if process.is_alive():
                    raise QualificationError("runtime worker shutdown timed out")
            raw_codes = [process.exitcode for process in self.processes]
            if any(code != 0 for code in raw_codes):
                raise QualificationError(
                    f"runtime worker exit codes are not clean: {raw_codes}"
                )
            if any(not isinstance(code, int) for code in raw_codes):
                raise QualificationError("runtime worker exit code is unavailable")
            self.exit_codes = [int(code) for code in raw_codes]
            self.clean_shutdown = True
            return list(self.exit_codes)
        except BaseException:
            _cleanup_processes(self.processes)
            raise
        finally:
            for channel in self.channels:
                channel.close()

    def abort(self) -> None:
        for channel in self.channels:
            channel.close()
        _cleanup_processes(self.processes)


def _validate_load_envelope(
    envelope: Any,
    *,
    process: Any,
    assignment: dict[str, Any],
) -> dict[str, Any]:
    if not isinstance(envelope, dict):
        raise QualificationError(
            f"child {process.pid} returned non-object load proof evidence"
        )
    if envelope.get("kind") == "load_error":
        raise QualificationError(
            f"child {process.pid} rejected assignment load: {envelope.get('error')!r}"
        )
    if set(envelope) != _LOAD_ENVELOPE_FIELDS:
        raise QualificationError(f"child {process.pid} returned malformed load proof")
    if (
        envelope["protocol"] != RUNTIME_RPC_PROTOCOL
        or envelope["kind"] != "load_proof"
        or envelope["pid"] != process.pid
        or envelope["start_method"] != "spawn"
        or envelope["assignment_id"] != assignment["assignment_id"]
        or envelope["node_id"] != assignment["node_id"]
        or envelope["assigned_range"] != assignment["range"]
        or envelope["load_proof_immutable"] is not True
        or envelope["network_event_count"] != 0
    ):
        raise QualificationError(f"child {process.pid} load proof identity mismatch")
    proof_json = envelope["proof_json"]
    if not isinstance(proof_json, str) or envelope["proof_sha256"] != _sha256(
        proof_json.encode("utf-8")
    ):
        raise QualificationError(f"child {process.pid} load proof digest mismatch")
    try:
        proof = json.loads(proof_json)
    except json.JSONDecodeError as exc:
        raise QualificationError(f"child {process.pid} load proof is not JSON") from exc
    if not isinstance(proof, dict) or canonical_json(proof) != proof_json:
        raise QualificationError(f"child {process.pid} load proof is not canonical")
    for field in (
        "deployment_id",
        "deployment_epoch",
        "assignment_id",
        "node_id",
        "model_id",
        "manifest_digest",
        "resolved_commit",
    ):
        if proof.get(field) != assignment[field]:
            raise QualificationError(
                f"child {process.pid} load proof {field} mismatch"
            )
    if proof.get("loaded_range") != assignment["range"]:
        raise QualificationError(f"child {process.pid} load proof range mismatch")
    if proof.get("loaded_components") != assignment["components"]:
        raise QualificationError(f"child {process.pid} load proof roles mismatch")
    if proof.get("loaded_tensor_keys") != sorted(assignment["expected_tensor_keys"]):
        raise QualificationError(f"child {process.pid} load proof tensors mismatch")
    if proof.get("route_ready") is not False:
        raise QualificationError(f"child {process.pid} load proof overclaimed readiness")
    evidence = dict(envelope)
    evidence.pop("proof_json")
    evidence["proof"] = proof
    evidence["load_proof_digest"] = layer_load_proof_digest(proof)
    return evidence


def _spawn_runtime_workers(
    jobs: Sequence[tuple[dict[str, Any], dict[str, Any]]],
    *,
    load_generation: int,
    timeout_seconds: float,
    worker_target: WorkerTarget = _runtime_worker,
) -> _RuntimeWorkerSet:
    """Spawn exactly two persistent assignment workers and await load proofs."""

    if len(jobs) != 2:
        raise QualificationError("qualification requires exactly two runtime worker jobs")
    if (
        not isinstance(timeout_seconds, (int, float))
        or isinstance(timeout_seconds, bool)
        or not math.isfinite(float(timeout_seconds))
        or timeout_seconds <= 0
    ):
        raise QualificationError("timeout_seconds must be positive and finite")

    context = multiprocessing.get_context("spawn")
    processes: list[Any] = []
    parent_connections: list[Any] = []
    child_connections: list[Any] = []
    child_pids: list[int] = []
    deadline = time.monotonic() + float(timeout_seconds)
    try:
        for index, (assignment, report) in enumerate(jobs):
            parent_connection, child_connection = context.Pipe(duplex=True)
            process = context.Process(
                target=worker_target,
                args=(child_connection, assignment, report, load_generation),
                name=f"mlx-runtime-worker-{index}",
                daemon=False,
            )
            parent_connections.append(parent_connection)
            child_connections.append(child_connection)
            processes.append(process)
            process.start()
            if process.pid is None:
                raise QualificationError("spawned runtime worker has no PID")
            child_pids.append(process.pid)
            child_connection.close()

        proof_evidence: list[dict[str, Any]] = []
        for process, connection, (assignment, _) in zip(
            processes, parent_connections, jobs
        ):
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise QualificationError("runtime worker load timed out")
                if connection.poll(min(0.05, remaining)):
                    try:
                        envelope = _WorkerChannel._bounded_io(
                            connection.recv, remaining
                        )
                    except TimeoutError as exc:
                        raise QualificationError(
                            f"child {process.pid} load proof receive timed out"
                        ) from exc
                    except (EOFError, OSError) as exc:
                        raise QualificationError(
                            f"child {process.pid} exited without load proof"
                        ) from exc
                    break
                if not process.is_alive():
                    raise QualificationError(
                        f"child {process.pid} exited without load proof; "
                        f"exit_code={process.exitcode}"
                    )
            proof_evidence.append(
                _validate_load_envelope(
                    envelope,
                    process=process,
                    assignment=assignment,
                )
            )

        if len(set(child_pids)) != 2 or os.getpid() in child_pids:
            raise QualificationError("workers do not have two distinct child PIDs")
        channels = [
            _WorkerChannel(
                process=process,
                connection=connection,
                timeout_seconds=float(timeout_seconds),
                proof_evidence=proof,
            )
            for process, connection, proof in zip(
                processes, parent_connections, proof_evidence
            )
        ]
        return _RuntimeWorkerSet(channels, processes)
    except QualificationError as exc:
        _cleanup_processes(processes)
        for connection in parent_connections + child_connections:
            try:
                connection.close()
            except OSError:
                pass
        raise QualificationError(str(exc), child_pids=child_pids) from exc
    except BaseException:
        _cleanup_processes(processes)
        for connection in parent_connections + child_connections:
            try:
                connection.close()
            except OSError:
                pass
        raise


def _prepare_assignments(
    root: Path,
) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]], _LocalOnlyFetcher]:
    manifest, _ = _build_local_model(root, n_positions=16)
    route = _route_for_manifest(manifest)
    assignments = compile_layer_assignments(
        route_plan=route,
        manifest=manifest,
        deployment_id=DEPLOYMENT_ID,
        deployment_epoch=DEPLOYMENT_EPOCH,
        cache_roots={node: str(root) for node in route["node_order"]},
        runtime_by_node={
            node: {"backend": "mlx", "dtype": "float32", "quantization": "none"}
            for node in route["node_order"]
        },
        control_plane_binding=_control_plane_binding(),
    )
    if len(assignments) != 2:
        raise QualificationError("compiled route does not contain exactly two assignments")
    fetcher = _LocalOnlyFetcher(root)
    reports = [
        provision_assignment(
            assignment,
            fetch_file=fetcher,
            local_files_only=True,
        )
        for assignment in assignments
    ]
    for assignment, report in zip(assignments, reports):
        errors = artifact_report_errors(assignment, report)
        if errors:
            raise QualificationError(
                "artifact verification report failed: " + "; ".join(errors)
            )
    return manifest, assignments, reports, fetcher


def _prepare_monolithic_reference(
    root: Path,
    manifest: dict[str, Any],
    fetcher: _LocalOnlyFetcher,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Compile and provision one independent full-model MLX reference stage."""

    reference_node = "reference-node"
    route = {
        **_route_for_manifest(manifest),
        "route": [
            {
                "node_id": reference_node,
                "range": {
                    "start_layer": 0,
                    "end_layer_exclusive": manifest["num_layers"],
                    "layer_count": manifest["num_layers"],
                },
            }
        ],
        "node_order": [reference_node],
    }
    reference_assignments = compile_layer_assignments(
        route_plan=route,
        manifest=manifest,
        deployment_id=DEPLOYMENT_ID,
        deployment_epoch=DEPLOYMENT_EPOCH,
        cache_roots={reference_node: str(root)},
        runtime_by_node={
            reference_node: {
                "backend": "mlx",
                "dtype": "float32",
                "quantization": "none",
            }
        },
        control_plane_binding=_control_plane_binding(),
    )
    if len(reference_assignments) != 1:
        raise QualificationError("monolithic reference did not compile one assignment")
    assignment = reference_assignments[0]
    report = provision_assignment(
        assignment,
        fetch_file=fetcher,
        local_files_only=True,
    )
    errors = artifact_report_errors(assignment, report)
    if errors:
        raise QualificationError(
            "monolithic reference artifact verification failed: " + "; ".join(errors)
        )
    return assignment, report


def _build_execution_graph(
    assignments: Sequence[dict[str, Any]],
    proofs: Sequence[Mapping[str, Any]],
) -> ExecutionGraph:
    if len(assignments) != 2 or len(proofs) != 2:
        raise QualificationError("execution graph requires exactly two assignments")
    stages: list[Stage] = []
    placements: list[Placement] = []
    for index, (assignment, proof) in enumerate(zip(assignments, proofs)):
        stage_id = f"stage-{index:03d}"
        placement = Placement(
            placement_id=f"placement-{index:03d}",
            node_id=assignment["node_id"],
            replica_group_id=f"{stage_id}-replicas",
            assignment_id=assignment["assignment_id"],
            stage_signature="pending-stage-signature",
            load_proof_digest=layer_load_proof_digest(proof),
            runtime_backend=assignment["runtime"]["backend"],
            runtime_endpoint=(
                f"pipe://{assignment['node_id']}/{assignment['assignment_id']}"
            ),
        )
        layer_range = assignment["range"]
        stage = Stage(
            stage_id=stage_id,
            layer_range=LayerRange(
                start_layer=layer_range["start_layer"],
                end_layer_exclusive=layer_range["end_layer_exclusive"],
                layer_count=layer_range["layer_count"],
            ),
            component_roles=tuple(assignment["components"]),
            stage_cost=StageCost(
                prefill_work_units_per_prompt_token=1.0,
                decode_work_units_per_token=1.0,
                kv_bytes_per_context_token=32,
            ),
            placements=(placement,),
        )
        placements.append(placement)
        stages.append(stage)

    graph = ExecutionGraph(
        deployment_id=assignments[0]["deployment_id"],
        deployment_epoch=assignments[0]["deployment_epoch"],
        topology_version=1,
        model_id=assignments[0]["model_id"],
        resolved_commit=assignments[0]["resolved_commit"],
        manifest_digest=assignments[0]["manifest_digest"],
        entry_stage_id=stages[0].stage_id,
        final_stage_id=stages[-1].stage_id,
        hidden_size=assignments[0]["runtime"]["model_config"]["n_embd"],
        activation_bytes=4,
        token_envelope_bytes=9,
        stages=tuple(stages),
        edges=(
            PlacementEdge(
                edge_id="forward:placement-000->placement-001",
                from_placement_id=placements[0].placement_id,
                to_placement_id=placements[1].placement_id,
                link_id="local-loopback:node-a->node-b",
            ),
        ),
        loopback_edges=(
            PlacementEdge(
                edge_id="loopback:placement-001->placement-000",
                from_placement_id=placements[1].placement_id,
                to_placement_id=placements[0].placement_id,
                link_id="local-loopback:node-b->node-a",
            ),
        ),
    )
    signed_stages = tuple(
        replace(
            stage,
            placements=(
                replace(
                    stage.placements[0],
                    stage_signature=_stage_signature(graph, stage, proof),
                ),
            ),
        )
        for stage, proof in zip(graph.stages, proofs)
    )
    return replace(graph, stages=signed_stages)


class _FixedClock:
    def __init__(self, now: float = 1.0) -> None:
        self._now = now

    def now(self) -> float:
        return self._now


class _SequenceIdSource:
    def __init__(self) -> None:
        self._next = 1
        self._lock = threading.Lock()

    def new(self, prefix: str) -> str:
        with self._lock:
            value = self._next
            self._next += 1
        return f"{prefix}-{value:06d}"


class _CaptureSink:
    def __init__(self) -> None:
        self.token_ids: list[int] = []
        self.token_indexes: list[int] = []
        self._lock = threading.Lock()

    def emit(self, token_index: int, token_id: int) -> None:
        with self._lock:
            self.token_indexes.append(token_index)
            self.token_ids.append(token_id)


def _device_states() -> dict[str, DeviceState]:
    return {
        "node-a": DeviceState(
            node_id="node-a",
            state_seq=1,
            last_updated=1.0,
            availability="ALIVE",
            compute_units_per_second=1_000.0,
            free_compute_fraction=1.0,
            available_kv_bytes=1_000_000,
            pending_hop_queue_depth=0,
            neighbor_rtt_ms={"node-a": 0.0, "node-b": 1.0},
            neighbor_bandwidth_bytes_per_second={
                "node-a": 1_000_000_000.0,
                "node-b": 1_000_000_000.0,
            },
        ),
        "node-b": DeviceState(
            node_id="node-b",
            state_seq=1,
            last_updated=1.0,
            availability="ALIVE",
            compute_units_per_second=1_000.0,
            free_compute_fraction=1.0,
            available_kv_bytes=1_000_000,
            pending_hop_queue_depth=0,
            neighbor_rtt_ms={"node-a": 1.0, "node-b": 0.0},
            neighbor_bandwidth_bytes_per_second={
                "node-a": 1_000_000_000.0,
                "node-b": 1_000_000_000.0,
            },
        ),
    }


def _reference_execution(
    loaded_stages: Sequence[Any],
    monolithic_stage: Any,
    token_ids: tuple[int, ...],
    *,
    last_position_only: bool = False,
) -> tuple[bytes, bytes, int]:
    tokens = mx.array((token_ids,), dtype=mx.uint32)
    hidden = execute_loaded_stage(loaded_stages[0], token_ids=tokens)
    split_logits = execute_loaded_stage(loaded_stages[1], hidden_states=hidden)
    monolithic_logits = execute_loaded_stage(monolithic_stage, token_ids=tokens)
    activation = hidden[:, -1:, :] if last_position_only else hidden
    contiguous = mx.contiguous(activation)
    mx.eval(contiguous, split_logits, monolithic_logits)
    split_token = int(mx.argmax(split_logits[0, -1, :]).item())
    monolithic_token = int(mx.argmax(monolithic_logits[0, -1, :]).item())
    _require_parity(split_token, monolithic_token, "concatenated and monolithic reference")
    hidden_bytes = bytes(contiguous)
    activation_payload = encode_activation(
        dtype="float32",
        shape=tuple(int(value) for value in contiguous.shape),
        data=hidden_bytes,
    )
    return activation_payload, hidden_bytes, monolithic_token


def _activation_wire_evidence(
    frames: Sequence[bytes],
    *,
    first_placement_id: str,
    second_placement_id: str,
    request_id: str,
    path_id: str,
    path_attempt: int,
    topology_version: int,
    references: Mapping[tuple[str, int], tuple[bytes, bytes, int]],
    first_runtime_calls: Sequence[Mapping[str, Any]],
    second_runtime_calls: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    expected_keys = tuple(references)
    expected_key_set = set(expected_keys)

    def runtime_calls_by_key(
        calls: Sequence[Mapping[str, Any]],
        *,
        placement_id: str,
        hop_index: int,
    ) -> dict[tuple[str, int], Mapping[str, Any]]:
        keyed: dict[tuple[str, int], Mapping[str, Any]] = {}
        for call in calls:
            phase = call.get("phase")
            token_index = call.get("token_index")
            if not isinstance(phase, str) or type(token_index) is not int:
                raise QualificationError("malformed runtime call identity")
            key = (phase, token_index)
            if key in keyed:
                raise QualificationError(f"duplicate runtime call identity: {key!r}")
            if (
                key not in expected_key_set
                or call.get("operation") != "execute"
                or call.get("request_id") != request_id
                or call.get("path_id") != path_id
                or call.get("path_attempt") != path_attempt
                or call.get("placement_id") != placement_id
                or call.get("hop_index") != hop_index
                or call.get("success") is not True
            ):
                raise QualificationError(f"runtime call identity mismatch: {key!r}")
            keyed[key] = call
        if set(keyed) != expected_key_set:
            raise QualificationError("runtime activation call set mismatch")
        return keyed

    first_calls = runtime_calls_by_key(
        first_runtime_calls,
        placement_id=first_placement_id,
        hop_index=0,
    )
    second_calls = runtime_calls_by_key(
        second_runtime_calls,
        placement_id=second_placement_id,
        hop_index=1,
    )

    captured: dict[tuple[str, int], tuple[bytes, HopHeader, bytes]] = {}
    for frame in frames:
        decoded = decode_frame(frame)
        message = decoded.message
        header = (
            message.header if isinstance(message, ProgressivePrefillMessage) else message
        )
        if not isinstance(header, HopHeader):
            continue
        if (
            header.source_placement_id != first_placement_id
            or header.destination_placement_id != second_placement_id
        ):
            continue
        key = (header.phase, header.token_index)
        if key in captured:
            raise QualificationError(f"duplicate activation frame identity: {key!r}")
        if (
            key not in expected_key_set
            or header.request_id != request_id
            or header.path_id != path_id
            or header.path_attempt != path_attempt
            or header.hop_index != 1
            or header.topology_version != topology_version
            or header.idempotency_key != second_calls[key].get("idempotency_key")
        ):
            raise QualificationError(f"activation frame identity mismatch: {key!r}")
        captured[key] = (frame, header, decoded.payload)
    if set(captured) != expected_key_set:
        raise QualificationError(
            "stage0->stage1 activation frame set mismatch: "
            f"actual={sorted(captured)!r}, expected={sorted(expected_key_set)!r}"
        )

    evidence: list[dict[str, Any]] = []
    maximum_absolute_error = 0.0
    for key in expected_keys:
        frame, header, payload = captured[key]
        reference_payload, reference_hidden, _ = references[key]
        envelope = decode_activation(payload)
        reference_envelope = decode_activation(reference_payload)
        payload_sha256 = _sha256(payload)
        reference_sha256 = _sha256(reference_payload)
        stage0_sha256 = first_calls[key].get("output_payload_sha256")
        stage1_sha256 = second_calls[key].get("input_payload_sha256")
        if (
            envelope.dtype != reference_envelope.dtype
            or envelope.shape != reference_envelope.shape
            or reference_envelope.data != reference_hidden
        ):
            raise QualificationError(
                f"activation envelope parity mismatch for {header.phase}:{header.token_index}"
            )
        actual_hidden = mx.array(memoryview(envelope.data)).view(mx.float32)
        expected_hidden = mx.array(memoryview(reference_hidden)).view(mx.float32)
        absolute_error = float(mx.max(mx.abs(actual_hidden - expected_hidden)).item())
        maximum_absolute_error = max(maximum_absolute_error, absolute_error)
        within_tolerance = absolute_error <= KV_NUMERIC_TOLERANCE
        if not within_tolerance:
            raise QualificationError(
                "activation numeric parity mismatch for "
                f"{header.phase}:{header.token_index}: "
                f"max_abs={absolute_error}, tolerance={KV_NUMERIC_TOLERANCE}"
            )
        if payload_sha256 != stage0_sha256:
            raise QualificationError("captured wire activation differs from worker output")
        if payload_sha256 != stage1_sha256:
            raise QualificationError(
                "destination runtime input differs from captured wire activation"
            )
        evidence.append(
            {
                "request_id": header.request_id,
                "path_id": header.path_id,
                "path_attempt": header.path_attempt,
                "phase": header.phase,
                "token_index": header.token_index,
                "hop_index": header.hop_index,
                "topology_version": header.topology_version,
                "source_placement_id": header.source_placement_id,
                "destination_placement_id": header.destination_placement_id,
                "payload_bytes": len(payload),
                "tcp_payload_sha256": payload_sha256,
                "reference_payload_sha256": reference_sha256,
                "stage0_output_sha256": stage0_sha256,
                "stage1_input_sha256": stage1_sha256,
                "tcp_frame_sha256": _sha256(frame),
                "hidden_state_bytes": len(envelope.data),
                "hidden_state_sha256": _sha256(envelope.data),
                "exact_reference_bytes": payload == reference_payload,
                "maximum_absolute_error": absolute_error,
                "numeric_tolerance": KV_NUMERIC_TOLERANCE,
                "within_numeric_tolerance": within_tolerance,
            }
        )
    return {
        "activation_frame_count": len(evidence),
        "activation_frames": evidence,
        "maximum_absolute_error": maximum_absolute_error,
        "numeric_tolerance": KV_NUMERIC_TOLERANCE,
        "stage0_to_stage1_within_numeric_tolerance": True,
    }


def run_qualification(
    work_dir: str | Path,
    *,
    timeout_seconds: float = 30.0,
) -> dict[str, Any]:
    """Run one complete local two-worker production-Router route challenge."""

    root = Path(work_dir).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    manifest, assignments, reports, fetcher = _prepare_assignments(root)
    reference_assignment, reference_report = _prepare_monolithic_reference(
        root, manifest, fetcher
    )

    workers: _RuntimeWorkerSet | None = None
    mesh: LoopbackSocketMesh | None = None
    normal_shutdown = False
    try:
        workers = _spawn_runtime_workers(
            list(zip(assignments, reports)),
            load_generation=LOAD_GENERATION,
            timeout_seconds=timeout_seconds,
        )
        child_proofs = [channel.proof_evidence["proof"] for channel in workers.channels]

        # Parent-only split stages provide exact intermediate-byte evidence; a
        # separately compiled full-model stage supplies the independent output oracle.
        # None of these parent stages are given to Router.
        parent_loaded = tuple(
            load_assignment_stage(
                assignment,
                report,
                load_generation=LOAD_GENERATION,
            )
            for assignment, report in zip(assignments, reports)
        )
        reference_loaded = load_assignment_stage(
            reference_assignment,
            reference_report,
            load_generation=LOAD_GENERATION,
        )
        reference_proof = json.loads(canonical_json(reference_loaded.proof))
        for channel, loaded in zip(workers.channels, parent_loaded):
            parent_proof = json.loads(canonical_json(loaded.proof))
            _require_parity(
                channel.proof_evidence["proof"],
                parent_proof,
                "child and parent load proof",
            )

        graph = _build_execution_graph(assignments, child_proofs)
        clock = _FixedClock()
        bindings = workers.bind_graph(graph, clock_now=clock.now())
        proxies = workers.proxies

        prompt = (1, 2, 3)
        prefill_reference = _reference_execution(
            parent_loaded, reference_loaded, prompt
        )
        references: dict[tuple[str, int], tuple[bytes, bytes, int]] = {
            ("PREFILL", -1): prefill_reference,
        }
        expected_decode_tokens: list[int] = []
        reference_context = prompt
        reference_input_token = prefill_reference[2]
        for token_index in range(1, 9):
            reference_context = reference_context + (reference_input_token,)
            decode_reference = _reference_execution(
                parent_loaded,
                reference_loaded,
                reference_context,
                last_position_only=True,
            )
            references[("DECODE", token_index)] = decode_reference
            expected_decode_tokens.append(decode_reference[2])
            reference_input_token = decode_reference[2]

        ids = _SequenceIdSource()
        topology = PublishedTopologyProvider(graph)
        states = PublishedDeviceStateProvider(topology, _device_states())
        capacity = InProcessLeaseCapacityPort(
            topology,
            {"node-a": 1_000_000, "node-b": 1_000_000},
            clock=clock,
            id_source=ids,
        )
        config = RouterConfig(prefill_chunk_size_tokens=0)
        request = RequestContext(
            request_id="phase6-local-route-challenge",
            prompt_token_ids=prompt,
            max_new_tokens=9,
            expected_new_tokens=9,
            qos_class="interactive",
            admitted_at=clock.now(),
            target_ttft_ms=1_000.0,
            target_tpot_ms=100.0,
            target_tokens_per_second=10.0,
            sampling_seed=7,
            generation_config_digest="sha256:" + "c" * 64,
        )
        sink = _CaptureSink()
        mesh = LoopbackSocketMesh(connect_timeout_seconds=timeout_seconds)
        routers: dict[str, Router] = {}
        for assignment, proxy in zip(assignments, proxies):
            node_id = assignment["node_id"]
            router = Router(
                node_id=node_id,
                topology=topology,
                device_states=states,
                capacity=capacity,
                runtime=proxy,
                transport=mesh.transport_for(node_id),
                clock=clock,
                id_source=ids,
                config=config,
            )
            mesh.register_router(node_id, router)
            routers[node_id] = router

        mesh.start()
        entry = routers[assignments[0]["node_id"]]
        request_id = entry.start_distributed_prefill(request, sink)
        status_after_prefill = entry.request_status(request_id)
        if status_after_prefill != "DECODING":
            raise QualificationError(
                f"Router path did not lock after prefill: {status_after_prefill}"
            )
        record = entry.get_request(request_id)
        locked_manifest = record.manifest
        locked_nodes = [
            next(
                placement.node_id
                for stage in graph.stages
                for placement in stage.placements
                if placement.placement_id == hop.placement_id
            )
            for hop in locked_manifest.ordered_hops
        ]
        _require_parity(locked_nodes, ["node-a", "node-b"], "locked Router path")
        committed_snapshot = capacity.snapshot()
        committed_count = sum(
            item.status == "COMMITTED"
            for item in committed_snapshot.reservations.values()
        )
        _require_parity(committed_count, 2, "committed route reservation")
        prefill_snapshots = workers.snapshots()
        prefill_active_states = {
            item["node_id"]: item["kv"]["active_state_count"]
            for item in prefill_snapshots
        }
        prefill_cached_context_tokens = {
            item["node_id"]: next(iter(item["kv"]["states"].values()))[
                "cached_context_tokens"
            ]
            for item in prefill_snapshots
        }
        _require_parity(
            prefill_active_states,
            {"node-a": 1, "node-b": 1},
            "prefill KV active state",
        )
        _require_parity(
            prefill_cached_context_tokens,
            {"node-a": len(prompt), "node-b": len(prompt)},
            "prefill KV context length",
        )

        _require_parity(
            list(sink.token_ids),
            [prefill_reference[2]],
            "prefill token",
        )
        for decode_step in range(8):
            if not entry.decode_one_distributed(request_id):
                raise QualificationError(
                    f"distributed decode step {decode_step + 1} was not dispatched"
                )
        actual_decode_tokens = list(sink.token_ids[1:])
        _require_parity(sink.token_indexes, list(range(9)), "generated token index")
        _require_parity(entry.request_status(request_id), "COMPLETED", "request completion")

        completed_snapshots = workers.snapshots()
        if len(completed_snapshots) != 2:
            raise QualificationError("runtime snapshot count mismatch")
        final_calls = completed_snapshots[1].get("calls")
        if not isinstance(final_calls, list) or len(final_calls) != 9:
            raise QualificationError("final runtime trace call count mismatch")
        actual_prefill_token = final_calls[0].get("token_id")
        _require_parity(
            actual_prefill_token,
            prefill_reference[2],
            "final-stage PREFILL token",
        )
        expected_phases = ["PREFILL"] + ["DECODE"] * 8
        for snapshot in completed_snapshots:
            _require_parity(snapshot.get("phases"), expected_phases, "runtime phase")
            _require_parity(snapshot.get("rpc_call_count"), 9, "runtime call count")
            if snapshot.get("network_event_count") != 0:
                raise QualificationError("runtime worker observed network activity")

        wire_activation = _activation_wire_evidence(
            tuple(mesh.frames),
            first_placement_id=graph.stages[0].placements[0].placement_id,
            second_placement_id=graph.stages[1].placements[0].placement_id,
            request_id=request_id,
            path_id=locked_manifest.path_id,
            path_attempt=locked_manifest.path_attempt,
            topology_version=graph.topology_version,
            references=references,
            first_runtime_calls=completed_snapshots[0]["calls"],
            second_runtime_calls=completed_snapshots[1]["calls"],
        )
        _require_parity(actual_decode_tokens, expected_decode_tokens, "decode token")
        endpoints = mesh.endpoints()
        bound_hosts = sorted(mesh.bound_hosts())
        connection_count = mesh.connection_count
        frame_count = len(mesh.frames)
        if bound_hosts != ["127.0.0.1"] or any(port <= 0 for _, port in endpoints.values()):
            raise QualificationError("LoopbackSocketMesh endpoint boundary mismatch")

        challenge_material = {
            "request_id": request_id,
            "path_id": locked_manifest.path_id,
            "assignment_ids": [item["assignment_id"] for item in assignments],
            "activation_payload_sha256": [
                item["tcp_payload_sha256"]
                for item in wire_activation["activation_frames"]
            ],
            "prefill_token": actual_prefill_token,
            "decode_tokens": actual_decode_tokens,
        }
        challenge_digest = _sha256(_canonical_json(challenge_material).encode("utf-8"))

        request_completed = entry.request_status(request_id) == "COMPLETED"
        runtime_snapshots = completed_snapshots
        for snapshot in runtime_snapshots:
            _require_parity(
                snapshot.get("rpc_call_count"), 9, "completed runtime call count"
            )
            kv_snapshot = snapshot.get("kv")
            if (
                not isinstance(kv_snapshot, dict)
                or kv_snapshot.get("active_state_count") != 0
                or kv_snapshot.get("release_counts", {}).get("normal_completion") != 1
            ):
                raise QualificationError("worker KV state was not released on completion")
            if snapshot.get("network_event_count") != 0:
                raise QualificationError(
                    "runtime worker observed network activity during request lifecycle"
                )
        released_snapshot = capacity.snapshot()
        completion_active_states = {
            item["node_id"]: item["kv"]["active_state_count"]
            for item in runtime_snapshots
        }
        capacity_after_completion = sum(
            released_snapshot.node_reserved_kv_bytes.values()
        )
        cross_request_leakage = any(completion_active_states.values())
        capacity_released = capacity_after_completion == 0 and all(
            item.status == "RELEASED"
            for item in released_snapshot.reservations.values()
        )
        if not capacity_released:
            raise QualificationError("route capacity was not released during cleanup")

        mesh.close()
        mesh_closed = not mesh.endpoints() and mesh.active_connection_count == 0
        if not mesh_closed:
            raise QualificationError("LoopbackSocketMesh did not close cleanly")

        exit_codes = workers.shutdown()
        normal_shutdown = True
        worker_connections_closed = all(
            channel.closed for channel in workers.channels
        )
        workers_reaped = all(not process.is_alive() for process in workers.processes)
        if not worker_connections_closed or not workers_reaped:
            raise QualificationError("runtime worker cleanup evidence is incomplete")

        assignment_evidence = [
            {
                "pid": channel.process.pid,
                "assignment_id": assignment["assignment_id"],
                "node_id": assignment["node_id"],
                "assigned_range": json.loads(_canonical_json(assignment["range"])),
                "load_proof_digest": channel.proof_evidence["load_proof_digest"],
                "load_proof_immutable": channel.proof_evidence[
                    "load_proof_immutable"
                ],
                "runtime_bound": binding["runtime"]
                == "mycelium_router.mlx_runtime.MLXRuntimePort",
                "placement_id": binding["placement_id"],
                "loaded_tensor_digest": channel.proof_evidence["proof"][
                    "loaded_tensor_digest"
                ],
            }
            for channel, assignment, binding in zip(
                workers.channels, assignments, bindings
            )
        ]
        runtime_evidence = {
            snapshot["node_id"]: snapshot for snapshot in runtime_snapshots
        }
        locked_stage_ranges = [
            {
                "start_layer": stage.layer_range.start_layer,
                "end_layer_exclusive": stage.layer_range.end_layer_exclusive,
                "layer_count": stage.layer_range.layer_count,
            }
            for stage in graph.stages
        ]
        network_download_bytes = sum(
            report["network_download_bytes"] for report in reports
        )
        document = {
            "protocol": QUALIFICATION_PROTOCOL,
            "qualified": True,
            "claim": CLAIM,
            "claim_boundary": CLAIM_BOUNDARY,
            "negative_claims": list(NEGATIVE_CLAIMS),
            "route_ready": False,
            "model": {
                "model_id": manifest["model_id"],
                "resolved_commit": manifest["resolved_commit"],
                "manifest_digest": manifest_digest_ref(manifest),
                "num_layers": manifest["num_layers"],
                "format": manifest["format"],
                "generated_locally": True,
            },
            "offline_evidence": {
                "local_files_only": True,
                "artifact_reports_validated": True,
                "artifact_request_count": len(fetcher.requests),
                "requested_files": list(fetcher.requests),
                "network_download_bytes": network_download_bytes,
                "worker_load_network_event_counts": [
                    channel.proof_evidence["network_event_count"]
                    for channel in workers.channels
                ],
                "worker_network_event_counts": [
                    snapshot["network_event_count"] for snapshot in runtime_snapshots
                ],
            },
            "process_evidence": {
                "start_method": "spawn",
                "persistent_worker_count": 2,
                "parent_pid": os.getpid(),
                "child_pids": list(workers.child_pids),
                "exit_codes": exit_codes,
                "clean_shutdown": workers.clean_shutdown,
            },
            "assignments": assignment_evidence,
            "router_path": {
                "route_challenge_passed": True,
                "challenge_digest": challenge_digest,
                "path_locked": True,
                "status_after_prefill": status_after_prefill,
                "path_id": locked_manifest.path_id,
                "locked_placements": [
                    hop.placement_id for hop in locked_manifest.ordered_hops
                ],
                "locked_nodes": locked_nodes,
                "locked_stage_ranges": locked_stage_ranges,
                "capacity_reservations_committed": committed_count,
                "prefill_chunk_size_tokens": config.prefill_chunk_size_tokens,
                "router_count": len(routers),
                "relay": f"{RelayEngine.__module__}.{RelayEngine.__name__}",
                "topology_provider": type(topology).__name__,
                "device_state_provider": type(states).__name__,
                "capacity_port": type(capacity).__name__,
            },
            "wire_evidence": {
                "protocol": ROUTER_WIRE_PROTOCOL,
                "transport": type(mesh).__name__,
                "bound_hosts": bound_hosts,
                "connection_count": connection_count,
                "frame_count": frame_count,
                **wire_activation,
            },
            "runtime_evidence": runtime_evidence,
            "parity": {
                "prefill": {
                    "actual_token": actual_prefill_token,
                    "reference_token": prefill_reference[2],
                    "passed": True,
                },
                "decode": {
                    "actual_tokens": actual_decode_tokens,
                    "reference_tokens": expected_decode_tokens,
                    "numeric_tolerance": KV_NUMERIC_TOLERANCE,
                    "max_hidden_abs_error": wire_activation[
                        "maximum_absolute_error"
                    ],
                    "passed": True,
                },
                "reference": {
                    "parent_pid": os.getpid(),
                    "api": "runtime_loader.execute_loaded_stage",
                    "kind": "independent_full_model_stage",
                    "assigned_range": reference_proof["loaded_range"],
                    "loaded_components": reference_proof["loaded_components"],
                    "load_proof_digest": layer_load_proof_digest(reference_proof),
                    "stages_loaded_independently": True,
                },
                "all_passed": True,
            },
            "kv_lifecycle": {
                "prefill_active_states": prefill_active_states,
                "prefill_cached_context_tokens": prefill_cached_context_tokens,
                "completion_active_states": completion_active_states,
                "capacity_after_completion": capacity_after_completion,
                "cross_request_leakage": cross_request_leakage,
            },
            "cleanup": {
                "request_completed": request_completed,
                "capacity_released": capacity_released,
                "mesh_closed": mesh_closed,
                "worker_connections_closed": worker_connections_closed,
                "workers_reaped": workers_reaped,
            },
        }
        _canonical_json(document)
        return document
    except QualificationError as exc:
        pids = workers.child_pids if workers is not None else exc.child_pids
        raise QualificationError(str(exc), child_pids=pids) from exc
    finally:
        if mesh is not None and mesh.endpoints():
            mesh.close()
        if workers is not None and not normal_shutdown:
            workers.abort()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Qualify a local two-Router execution path backed by exactly two "
            "persistent spawned assignment-bound MLX workers."
        )
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="emit the complete JSON-safe qualification evidence document",
    )
    parser.add_argument(
        "--work-dir",
        type=Path,
        help="retain generated deterministic tiny-model artifacts here",
    )
    parser.add_argument(
        "--timeout-seconds",
        type=float,
        default=30.0,
        help="bound each worker load/RPC/shutdown wait (default: 30)",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.work_dir is None:
            with tempfile.TemporaryDirectory(
                prefix="mycelium-two-runtime-router-qualification-"
            ) as temporary:
                result = run_qualification(
                    temporary,
                    timeout_seconds=args.timeout_seconds,
                )
        else:
            result = run_qualification(
                args.work_dir,
                timeout_seconds=args.timeout_seconds,
            )
    except (QualificationError, OSError, ValueError) as exc:
        print(f"qualification failed: {exc}", file=sys.stderr)
        return 1

    if args.json:
        print(_canonical_json(result))
    else:
        print("QUALIFIED: " + result["claim"])
        print("BOUNDARY: " + result["claim_boundary"])
        for negative_claim in result["negative_claims"]:
            print("NOT CLAIMED: " + negative_claim)
        print("local_route_challenge_passed=true")
        print("route_ready=false")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
