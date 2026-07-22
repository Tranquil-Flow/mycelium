"""N-process greedy-decode qualification for locally provisioned GPT-2 stages.

This focused harness keeps the model stages in distinct spawned processes and moves
only versioned token/activation byte payloads across process boundaries.  It is
intentionally smaller than the Router qualification harness: this module proves
that arbitrary contiguous sharding preserves greedy token identity, while the
Router and physical-network gates prove transport behavior separately.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
import multiprocessing
import os
from pathlib import Path
import time
from typing import Any, Sequence

import mlx.core as mx

from mycelium_qualification.physical_deployment import (
    LocalModelSource,
    PhysicalDeploymentError,
    prepare_physical_deployment,
)
from mycelium_router.payloads import (
    decode_activation,
    decode_token_ids,
    encode_activation,
    encode_token_ids,
)
from runtime_loader import execute_loaded_stage, load_assignment_stage

_PROTOCOL = "mycelium.distributed_decode_rpc.v1"
_RUNTIME_DTYPES = {
    "bfloat16": mx.bfloat16,
    "float16": mx.float16,
    "float32": mx.float32,
}


class DistributedInferenceQualificationError(RuntimeError):
    """Raised when an N-process decode cannot produce trustworthy evidence."""


@dataclass(frozen=True)
class DistributedDecodeQualification:
    """Token-parity evidence from one N-process greedy decode."""

    node_count: int
    node_ids: tuple[str, ...]
    worker_pids: tuple[int, ...]
    layer_ranges: tuple[tuple[int, int], ...]
    prompt_token_ids: tuple[int, ...]
    distributed_token_ids: tuple[int, ...]
    reference_token_ids: tuple[int, ...]
    seed: int


def _encode_array(array: mx.array) -> bytes:
    contiguous = mx.contiguous(array)
    mx.eval(contiguous)
    return encode_activation(
        dtype=str(contiguous.dtype).removeprefix("mlx.core."),
        shape=tuple(int(value) for value in contiguous.shape),
        data=bytes(contiguous),
    )


def _decode_array(payload: bytes) -> mx.array:
    envelope = decode_activation(payload)
    dtype = _RUNTIME_DTYPES.get(envelope.dtype)
    if dtype is None:
        raise DistributedInferenceQualificationError(
            f"unsupported activation dtype: {envelope.dtype}"
        )
    array = (
        mx.array(memoryview(envelope.data), dtype=mx.uint8)
        .view(dtype)
        .reshape(envelope.shape)
    )
    mx.eval(array)
    return array


def _response(
    request_id: int,
    *,
    ok: bool,
    result: Any = None,
    error: dict[str, str] | None = None,
) -> dict[str, Any]:
    return {
        "protocol": _PROTOCOL,
        "kind": "response",
        "request_id": request_id,
        "ok": ok,
        "result": result,
        "error": error,
    }


def _stage_worker(
    connection: Any,
    assignment: dict[str, Any],
    artifact_report: dict[str, Any],
    load_generation: int,
    seed: int,
) -> None:
    try:
        mx.random.seed(seed)
        loaded = load_assignment_stage(
            assignment,
            artifact_report,
            load_generation=load_generation,
        )
        components = tuple(loaded.proof["loaded_components"])
        connection.send(
            {
                "protocol": _PROTOCOL,
                "kind": "ready",
                "pid": os.getpid(),
                "assignment_id": assignment["assignment_id"],
                "node_id": assignment["node_id"],
                "layer_range": dict(assignment["range"]),
            }
        )
    except BaseException as exc:
        try:
            connection.send(
                {
                    "protocol": _PROTOCOL,
                    "kind": "load_error",
                    "pid": os.getpid(),
                    "error": {"type": type(exc).__name__, "message": str(exc)},
                }
            )
        except (BrokenPipeError, EOFError, OSError):
            pass
        connection.close()
        return

    try:
        while True:
            try:
                request = connection.recv()
            except EOFError:
                break
            request_id = (
                request.get("request_id", -1) if isinstance(request, dict) else -1
            )
            try:
                if (
                    not isinstance(request, dict)
                    or set(request)
                    != {"protocol", "kind", "request_id", "operation", "payload"}
                    or request["protocol"] != _PROTOCOL
                    or request["kind"] != "request"
                    or type(request["request_id"]) is not int
                    or request["request_id"] <= 0
                ):
                    raise DistributedInferenceQualificationError(
                        "malformed stage-worker request"
                    )
                operation = request["operation"]
                payload = request["payload"]
                if operation == "shutdown":
                    if payload is not None:
                        raise DistributedInferenceQualificationError(
                            "invalid shutdown payload"
                        )
                    connection.send(
                        _response(request_id, ok=True, result={"pid": os.getpid()})
                    )
                    break
                if operation != "execute" or not isinstance(payload, bytes):
                    raise DistributedInferenceQualificationError(
                        "unsupported stage-worker request"
                    )

                if "input_embedding" in components:
                    token_ids = decode_token_ids(payload)
                    output = execute_loaded_stage(
                        loaded,
                        token_ids=mx.array((token_ids,), dtype=mx.uint32),
                    )
                else:
                    output = execute_loaded_stage(
                        loaded,
                        hidden_states=_decode_array(payload),
                    )

                if "lm_head" in components:
                    result: Any = {
                        "kind": "token",
                        "token_id": int(mx.argmax(output[0, -1, :]).item()),
                    }
                else:
                    result = {"kind": "activation", "payload": _encode_array(output)}
                connection.send(_response(request_id, ok=True, result=result))
            except BaseException as exc:
                try:
                    connection.send(
                        _response(
                            request_id if type(request_id) is int else -1,
                            ok=False,
                            error={"type": type(exc).__name__, "message": str(exc)},
                        )
                    )
                except (BrokenPipeError, EOFError, OSError):
                    break
    finally:
        connection.close()


class _StageWorker:
    def __init__(
        self,
        *,
        process: Any,
        connection: Any,
        timeout_seconds: float,
    ) -> None:
        self.process = process
        self.connection = connection
        self.timeout_seconds = timeout_seconds
        self._request_id = 0
        self.closed = False

    def request(self, operation: str, payload: bytes | None) -> Any:
        if self.closed:
            raise DistributedInferenceQualificationError("stage worker is closed")
        self._request_id += 1
        request_id = self._request_id
        try:
            self.connection.send(
                {
                    "protocol": _PROTOCOL,
                    "kind": "request",
                    "request_id": request_id,
                    "operation": operation,
                    "payload": payload,
                }
            )
        except (BrokenPipeError, EOFError, OSError) as exc:
            raise DistributedInferenceQualificationError(
                f"stage worker {self.process.pid} IPC send failed"
            ) from exc

        deadline = time.monotonic() + self.timeout_seconds
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise DistributedInferenceQualificationError(
                    f"stage worker {self.process.pid} RPC timed out"
                )
            if self.connection.poll(min(0.05, remaining)):
                try:
                    response = self.connection.recv()
                except (EOFError, OSError) as exc:
                    raise DistributedInferenceQualificationError(
                        f"stage worker {self.process.pid} RPC channel closed"
                    ) from exc
                break
            if not self.process.is_alive():
                raise DistributedInferenceQualificationError(
                    f"stage worker {self.process.pid} exited; "
                    f"exit_code={self.process.exitcode}"
                )

        if (
            not isinstance(response, dict)
            or set(response)
            != {"protocol", "kind", "request_id", "ok", "result", "error"}
            or response["protocol"] != _PROTOCOL
            or response["kind"] != "response"
            or response["request_id"] != request_id
            or type(response["ok"]) is not bool
        ):
            raise DistributedInferenceQualificationError(
                f"malformed response from stage worker {self.process.pid}"
            )
        if response["ok"] is not True or response["error"] is not None:
            raise DistributedInferenceQualificationError(
                f"stage worker {self.process.pid} rejected {operation}: "
                f"{response['error']!r}"
            )
        return response["result"]

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        try:
            self.connection.close()
        except OSError:
            pass


class _StageWorkerSet:
    def __init__(self, workers: Sequence[_StageWorker]) -> None:
        self.workers = tuple(workers)
        self.worker_pids = tuple(int(worker.process.pid) for worker in self.workers)
        self.closed = False

    def execute(self, token_ids: tuple[int, ...]) -> int:
        payload = encode_token_ids(token_ids)
        for index, worker in enumerate(self.workers):
            result = worker.request("execute", payload)
            if not isinstance(result, dict):
                raise DistributedInferenceQualificationError(
                    "stage worker returned a malformed result"
                )
            if index < len(self.workers) - 1:
                if set(result) != {"kind", "payload"} or result["kind"] != "activation":
                    raise DistributedInferenceQualificationError(
                        "non-final stage did not return an activation"
                    )
                payload = result["payload"]
                if not isinstance(payload, bytes):
                    raise DistributedInferenceQualificationError(
                        "stage activation payload is not bytes"
                    )
            else:
                if (
                    set(result) != {"kind", "token_id"}
                    or result["kind"] != "token"
                    or type(result["token_id"]) is not int
                ):
                    raise DistributedInferenceQualificationError(
                        "final stage did not return a token"
                    )
                return result["token_id"]
        raise DistributedInferenceQualificationError("no stage workers were available")

    def shutdown(self) -> None:
        if self.closed:
            return
        try:
            for worker in self.workers:
                result = worker.request("shutdown", None)
                if result != {"pid": worker.process.pid}:
                    raise DistributedInferenceQualificationError(
                        "stage worker returned malformed shutdown evidence"
                    )
            deadline = time.monotonic() + max(
                worker.timeout_seconds for worker in self.workers
            )
            for worker in self.workers:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise DistributedInferenceQualificationError(
                        "stage-worker shutdown timed out"
                    )
                worker.process.join(timeout=remaining)
                if worker.process.is_alive() or worker.process.exitcode != 0:
                    raise DistributedInferenceQualificationError(
                        "stage worker did not exit cleanly"
                    )
        finally:
            self.closed = True
            for worker in self.workers:
                worker.close()

    def abort(self) -> None:
        self.closed = True
        for worker in self.workers:
            worker.close()
        for worker in self.workers:
            if worker.process.is_alive():
                worker.process.terminate()
        for worker in self.workers:
            worker.process.join(timeout=1.0)
        for worker in self.workers:
            if worker.process.is_alive():
                worker.process.kill()
                worker.process.join(timeout=1.0)


def _receive_ready(
    process: Any,
    connection: Any,
    assignment: dict[str, Any],
    *,
    deadline: float,
) -> None:
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise DistributedInferenceQualificationError(
                "stage-worker load timed out"
            )
        if connection.poll(min(0.05, remaining)):
            try:
                envelope = connection.recv()
            except (EOFError, OSError) as exc:
                raise DistributedInferenceQualificationError(
                    f"stage worker {process.pid} exited before loading"
                ) from exc
            break
        if not process.is_alive():
            raise DistributedInferenceQualificationError(
                f"stage worker {process.pid} exited before loading; "
                f"exit_code={process.exitcode}"
            )

    expected = {
        "protocol",
        "kind",
        "pid",
        "assignment_id",
        "node_id",
        "layer_range",
    }
    if (
        not isinstance(envelope, dict)
        or set(envelope) != expected
        or envelope["protocol"] != _PROTOCOL
        or envelope["kind"] != "ready"
        or envelope["pid"] != process.pid
        or envelope["assignment_id"] != assignment["assignment_id"]
        or envelope["node_id"] != assignment["node_id"]
        or envelope["layer_range"] != assignment["range"]
    ):
        detail = envelope.get("error") if isinstance(envelope, dict) else envelope
        raise DistributedInferenceQualificationError(
            f"stage worker {process.pid} returned invalid load evidence: {detail!r}"
        )


def _spawn_stage_workers(
    assignments: Sequence[dict[str, Any]],
    reports: Sequence[dict[str, Any]],
    *,
    seed: int,
    timeout_seconds: float,
) -> _StageWorkerSet:
    context = multiprocessing.get_context("spawn")
    workers: list[_StageWorker] = []
    child_connections: list[Any] = []
    deadline = time.monotonic() + timeout_seconds
    try:
        for index, (assignment, report) in enumerate(
            zip(assignments, reports, strict=True)
        ):
            parent_connection, child_connection = context.Pipe(duplex=True)
            process = context.Process(
                target=_stage_worker,
                args=(child_connection, assignment, report, index + 1, seed),
                name=f"mycelium-stage-worker-{index}",
                daemon=False,
            )
            process.start()
            child_connection.close()
            child_connections.append(child_connection)
            if process.pid is None:
                raise DistributedInferenceQualificationError(
                    "spawned stage worker has no PID"
                )
            workers.append(
                _StageWorker(
                    process=process,
                    connection=parent_connection,
                    timeout_seconds=timeout_seconds,
                )
            )

        for worker, assignment in zip(workers, assignments, strict=True):
            _receive_ready(
                worker.process,
                worker.connection,
                assignment,
                deadline=deadline,
            )
        worker_pids = tuple(worker.process.pid for worker in workers)
        if len(set(worker_pids)) != len(assignments) or os.getpid() in worker_pids:
            raise DistributedInferenceQualificationError(
                "stage workers do not have distinct child PIDs"
            )
        return _StageWorkerSet(workers)
    except BaseException:
        worker_set = _StageWorkerSet(workers)
        worker_set.abort()
        raise
    finally:
        for connection in child_connections:
            try:
                connection.close()
            except OSError:
                pass


def _reference_next_token(loaded_reference: Any, token_ids: tuple[int, ...]) -> int:
    logits = execute_loaded_stage(
        loaded_reference,
        token_ids=mx.array((token_ids,), dtype=mx.uint32),
    )
    return int(mx.argmax(logits[0, -1, :]).item())


def qualify_distributed_decode(
    root: Path,
    *,
    node_count: int,
    model_source: LocalModelSource,
    prompt_token_ids: Sequence[int],
    max_new_tokens: int,
    seed: int,
    runtime_dtype: str = "float16",
    timeout_seconds: float = 60.0,
) -> DistributedDecodeQualification:
    """Run deterministic greedy decode through N spawned assignment workers."""

    if not isinstance(node_count, int) or isinstance(node_count, bool) or node_count < 2:
        raise DistributedInferenceQualificationError("node_count must be at least two")
    if not isinstance(seed, int) or isinstance(seed, bool) or seed < 0:
        raise DistributedInferenceQualificationError("seed must be a non-negative integer")
    if (
        not isinstance(max_new_tokens, int)
        or isinstance(max_new_tokens, bool)
        or max_new_tokens <= 0
    ):
        raise DistributedInferenceQualificationError(
            "max_new_tokens must be a positive integer"
        )
    if (
        not isinstance(timeout_seconds, (int, float))
        or isinstance(timeout_seconds, bool)
        or not math.isfinite(float(timeout_seconds))
        or timeout_seconds <= 0
    ):
        raise DistributedInferenceQualificationError(
            "timeout_seconds must be positive and finite"
        )
    prompt = tuple(prompt_token_ids)
    if not prompt or any(type(token_id) is not int or token_id < 0 for token_id in prompt):
        raise DistributedInferenceQualificationError(
            "prompt_token_ids must contain non-negative integers"
        )

    node_ids = tuple(f"node-{index:03d}" for index in range(node_count))
    try:
        deployment = prepare_physical_deployment(
            Path(root),
            node_ids=node_ids,
            model_source=model_source,
            runtime_dtype=runtime_dtype,
        )
    except PhysicalDeploymentError as exc:
        raise DistributedInferenceQualificationError(str(exc)) from exc

    loaded_reference = load_assignment_stage(
        deployment.reference_assignment,
        deployment.reference_report,
        load_generation=node_count + 1,
    )
    worker_set = _spawn_stage_workers(
        deployment.assignments,
        deployment.artifact_reports,
        seed=seed,
        timeout_seconds=float(timeout_seconds),
    )
    distributed_context = prompt
    reference_context = prompt
    distributed_tokens: list[int] = []
    reference_tokens: list[int] = []
    try:
        for _ in range(max_new_tokens):
            distributed_token = worker_set.execute(distributed_context)
            reference_token = _reference_next_token(loaded_reference, reference_context)
            distributed_tokens.append(distributed_token)
            reference_tokens.append(reference_token)
            distributed_context = (*distributed_context, distributed_token)
            reference_context = (*reference_context, reference_token)
        worker_set.shutdown()
    except BaseException:
        worker_set.abort()
        raise

    layer_ranges = tuple(
        (
            int(assignment["range"]["start_layer"]),
            int(assignment["range"]["end_layer_exclusive"]),
        )
        for assignment in deployment.assignments
    )
    return DistributedDecodeQualification(
        node_count=node_count,
        node_ids=node_ids,
        worker_pids=worker_set.worker_pids,
        layer_ranges=layer_ranges,
        prompt_token_ids=prompt,
        distributed_token_ids=tuple(distributed_tokens),
        reference_token_ids=tuple(reference_tokens),
        seed=seed,
    )
