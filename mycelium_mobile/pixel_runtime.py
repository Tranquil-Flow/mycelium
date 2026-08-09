"""Pure-stdlib Pixel stage implementation of the Router RuntimePort.

This adapter terminates only the Router runtime boundary.  Production Iroh
transport remains owned by ``IrohTransport`` and the native sidecar.  Local
success therefore never promotes route or release readiness.
"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
import hashlib
import json
import struct
from threading import RLock
from types import MappingProxyType
from typing import Any, NoReturn

from mycelium_mobile.pixel_stage import (
    MAX_SEQUENCE_LENGTH,
    PixelStage,
    PixelStageError,
)
from mycelium_router.contracts import (
    ExecutionGraph,
    HopWorkItem,
    RuntimeBatch,
    RuntimeBatchKey,
    RuntimeResult,
)
from mycelium_router.payloads import PayloadError, decode_activation, encode_activation
from mycelium_router.stage_signatures import stage_signature_for_backend
from mycelium_router.validation import ContractError, validate_execution_graph

_MAX_RETAINED_PATHS = 4096
_MAX_RETAINED_RESULTS = 128


@dataclass(frozen=True)
class _ReplayResult:
    fingerprint: str
    result: RuntimeResult


class PixelRuntimeError(ValueError):
    """Fail-closed Pixel Router-runtime error with a stable code."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _reject(code: str) -> NoReturn:
    raise PixelRuntimeError(code)


class PixelStageRuntimePort:
    """Serial complete-context-replay runtime for one mobile decoder stage."""

    decode_mode = "complete_context_replay"
    backend = "pixel-stdlib"
    _IMMUTABLE_BINDING_FIELDS = frozenset(
        {"stage", "_binding", "placement_id", "_IMMUTABLE_BINDING_FIELDS"}
    )

    def __setattr__(self, name: str, value: Any) -> None:
        if name in self._IMMUTABLE_BINDING_FIELDS:
            try:
                object.__getattribute__(self, name)
            except AttributeError:
                pass
            else:
                raise AttributeError(f"{name} is immutable")
        object.__setattr__(self, name, value)

    @staticmethod
    def _validate_graph_integer_metadata(graph: ExecutionGraph) -> None:
        root_fields = (
            "deployment_epoch",
            "topology_version",
            "hidden_size",
            "activation_bytes",
            "token_envelope_bytes",
        )
        for field in root_fields:
            if type(getattr(graph, field)) is not int:
                _reject(f"invalid_graph_{field}")
        for stage in graph.stages:
            for field in ("start_layer", "end_layer_exclusive", "layer_count"):
                if type(getattr(stage.layer_range, field)) is not int:
                    _reject(f"invalid_graph_{field}")

    def __init__(
        self,
        stage: PixelStage,
        *,
        graph: ExecutionGraph,
        placement_id: str,
        parent_assignment_digest: str,
    ) -> None:
        if not isinstance(stage, PixelStage):
            _reject("invalid_pixel_stage")
        if not isinstance(graph, ExecutionGraph):
            _reject("invalid_execution_graph")
        self._validate_graph_integer_metadata(graph)
        try:
            validate_execution_graph(graph)
        except (ContractError, AttributeError, TypeError, ValueError) as exc:
            raise PixelRuntimeError("invalid_execution_graph") from exc
        if not isinstance(placement_id, str) or not placement_id:
            _reject("invalid_runtime_placement_id")
        if type(parent_assignment_digest) is not str or not parent_assignment_digest:
            _reject("invalid_parent_assignment_digest")

        matches = [
            (candidate_stage, placement)
            for candidate_stage in graph.stages
            for placement in candidate_stage.placements
            if placement.placement_id == placement_id
        ]
        if len(matches) != 1:
            _reject("unknown_runtime_placement")
        graph_stage, placement = matches[0]
        if placement.lifecycle_state != "ACTIVE":
            _reject("inactive_runtime_placement")
        if placement.runtime_backend != self.backend:
            _reject("runtime_backend_mismatch")
        if not placement.runtime_endpoint.startswith("iroh://"):
            _reject("runtime_endpoint_mismatch")
        if graph_stage.stage_id in {graph.entry_stage_id, graph.final_stage_id}:
            _reject("pixel_stage_must_be_intermediate")
        if graph_stage.component_roles != ("decoder",):
            _reject("pixel_stage_component_roles_mismatch")
        if graph.activation_bytes != 4:
            _reject("pixel_runtime_requires_float32")
        expected_signature = stage_signature_for_backend(
            graph, graph_stage, self.backend
        )
        if placement.stage_signature != expected_signature:
            _reject("stage_signature_mismatch")

        document = stage.document
        expected_document = {
            "deployment_id": graph.deployment_id,
            "assignment_id": placement.assignment_id,
            "stage_id": graph_stage.stage_id,
            "model_id": graph.model_id,
            "resolved_commit": graph.resolved_commit,
            "manifest_digest": graph.manifest_digest,
            "parent_assignment_digest": parent_assignment_digest,
            "parent_load_proof_digest": placement.load_proof_digest,
            "start_layer": graph_stage.layer_range.start_layer,
            "end_layer_exclusive": graph_stage.layer_range.end_layer_exclusive,
            "hidden_size": graph.hidden_size,
            "component_roles": graph_stage.component_roles,
        }
        for field, expected in expected_document.items():
            actual = document[field]
            if field == "component_roles":
                actual = tuple(actual)
            if actual != expected:
                _reject(f"stage_pack_{field}_mismatch")
        if document["parent_load_proof_digest"] != placement.load_proof_digest:
            _reject("load_proof_digest_mismatch")

        self.stage = stage
        self._binding = MappingProxyType(
            {
                "deployment_id": graph.deployment_id,
                "deployment_epoch": graph.deployment_epoch,
                "model_commit": graph.resolved_commit,
                "manifest_digest": graph.manifest_digest,
                "placement_id": placement.placement_id,
                "assignment_id": placement.assignment_id,
                "stage_id": graph_stage.stage_id,
                "stage_signature": placement.stage_signature,
                "load_proof_digest": placement.load_proof_digest,
                "parent_assignment_digest": parent_assignment_digest,
                "hidden_size": graph.hidden_size,
                "activation_bytes": graph.activation_bytes,
            }
        )
        self.placement_id = placement.placement_id
        self._lock = RLock()
        self._cancelled_paths: OrderedDict[str, None] = OrderedDict()
        self._cancellation_fence_saturated = False
        self._requests_by_path: OrderedDict[str, set[str]] = OrderedDict()
        self._replays: OrderedDict[tuple[str, str], _ReplayResult] = OrderedDict()
        self._applied_operation_count = 0
        self._release_counts: dict[str, int] = {}
        self._closed = False

    @staticmethod
    def _failure(reason: str) -> RuntimeResult:
        return RuntimeResult(
            success=False,
            failure_scope="PLACEMENT",
            failure_reason=reason,
        )

    def _remember_cancelled_path(self, path_id: str) -> None:
        if path_id in self._cancelled_paths:
            self._cancelled_paths.move_to_end(path_id)
            return
        if len(self._cancelled_paths) >= _MAX_RETAINED_PATHS:
            self._cancellation_fence_saturated = True
            return
        self._cancelled_paths[path_id] = None

    @staticmethod
    def _operation_fingerprint(item: HopWorkItem) -> str:
        if not isinstance(item.payload, bytes):
            _reject("runtime_payload_must_be_bytes")
        key = item.batch_key
        key_material: Any = None
        if isinstance(key, RuntimeBatchKey):
            key_material = {
                field: getattr(key, field)
                for field in RuntimeBatchKey.__dataclass_fields__
            }
        material = {
            "request_id": item.request_id,
            "path_id": item.path_id,
            "path_attempt": item.path_attempt,
            "phase": item.phase,
            "token_index": item.token_index,
            "position": item.position,
            "hop_index": item.hop_index,
            "placement_id": item.placement_id,
            "idempotency_key": item.idempotency_key,
            "terminal": item.terminal,
            "batch_key": key_material,
            "payload_digest": hashlib.sha256(item.payload).hexdigest(),
        }
        encoded = json.dumps(
            material,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    @staticmethod
    def _stage_request_id(item: HopWorkItem) -> str:
        encoded = json.dumps(
            (item.path_id, item.idempotency_key),
            ensure_ascii=True,
            separators=(",", ":"),
        ).encode("ascii")
        return "router-" + hashlib.sha256(encoded).hexdigest()

    def _validate_batch_key(
        self,
        item: HopWorkItem,
        *,
        sequence_length: int,
    ) -> None:
        key = item.batch_key
        if not isinstance(key, RuntimeBatchKey):
            _reject("missing_runtime_batch_key")
        for field in (
            "deployment_epoch",
            "hidden_size",
            "activation_bytes",
            "token_span",
            "speculative_width",
        ):
            value = getattr(key, field)
            if type(value) is not int:
                _reject(f"invalid_batch_key_{field}")
        if key.token_span <= 0:
            _reject("invalid_batch_key_token_span")
        if key.speculative_width < 0:
            _reject("invalid_batch_key_speculative_width")
        binding = self._binding
        expected = {
            "deployment_id": binding["deployment_id"],
            "deployment_epoch": binding["deployment_epoch"],
            "model_commit": binding["model_commit"],
            "manifest_digest": binding["manifest_digest"],
            "placement_id": binding["placement_id"],
            "assignment_id": binding["assignment_id"],
            "stage_signature": binding["stage_signature"],
            "load_proof_digest": binding["load_proof_digest"],
            "runtime_backend": self.backend,
            "phase": item.phase,
            "hidden_size": binding["hidden_size"],
            "activation_bytes": binding["activation_bytes"],
            "speculative_role": "NONE",
            "speculative_width": 0,
        }
        for field, value in expected.items():
            if getattr(key, field) != value:
                _reject(f"batch_key_{field}_mismatch")
        expected_span = sequence_length if item.phase != "DECODE" else 1
        if key.token_span != expected_span:
            _reject("batch_key_token_span_mismatch")

    def _decode_input(self, item: HopWorkItem) -> list[list[float]]:
        if not isinstance(item.payload, bytes):
            _reject("runtime_payload_must_be_bytes")
        envelope = decode_activation(item.payload)
        hidden_size = int(self._binding["hidden_size"])
        if envelope.dtype != "float32":
            _reject("activation_dtype_mismatch")
        if (
            len(envelope.shape) != 3
            or envelope.shape[0] != 1
            or envelope.shape[2] != hidden_size
        ):
            _reject("activation_shape_mismatch")
        if envelope.shape[1] > MAX_SEQUENCE_LENGTH:
            _reject("activation_sequence_too_long")
        self._validate_batch_key(item, sequence_length=envelope.shape[1])
        values = (value[0] for value in struct.iter_unpack("<f", envelope.data))
        return [
            [next(values) for _ in range(hidden_size)]
            for _ in range(envelope.shape[1])
        ]

    def _execute_bound(self, item: HopWorkItem) -> RuntimeResult:
        if item.phase not in {"PREFILL", "RECOVERY_PREFILL", "DECODE"}:
            _reject("unsupported_runtime_phase")
        if type(item.placement_id) is not str or item.placement_id != self.placement_id:
            _reject("unbound_runtime_placement")
        if type(item.request_id) is not str or not item.request_id:
            _reject("invalid_runtime_request_id")
        if type(item.path_id) is not str or not item.path_id:
            _reject("invalid_runtime_path_id")
        if type(item.idempotency_key) is not str or not item.idempotency_key:
            _reject("invalid_idempotency_key")
        if type(item.path_attempt) is not int or item.path_attempt < 0:
            _reject("invalid_runtime_path_attempt")
        if type(item.hop_index) is not int or item.hop_index < 0:
            _reject("invalid_runtime_hop_index")
        if type(item.prefill_chunk_token_count) is not int:
            _reject("invalid_runtime_prefill_chunk_token_count")
        if type(item.token_index) is not int:
            _reject("invalid_runtime_token_index")
        if type(item.terminal) is not bool or item.terminal:
            _reject("pixel_stage_terminal_forbidden")
        if type(item.position) is not int or item.position < 0:
            _reject("invalid_runtime_position")
        hidden = self._decode_input(item)
        if item.phase == "PREFILL":
            if item.position != 0 or item.token_index != -1:
                _reject("prefill_sequence_mismatch")
        elif item.phase == "RECOVERY_PREFILL":
            if item.position != 0 or item.token_index < 0:
                _reject("recovery_prefill_sequence_mismatch")
        elif item.position <= 0 or len(hidden) != item.position + 1 or item.token_index < 0:
            _reject("decode_sequence_mismatch")

        fingerprint = self._operation_fingerprint(item)
        replay_key = (item.path_id, item.idempotency_key)
        replay = self._replays.get(replay_key)
        if replay is not None:
            if replay.fingerprint != fingerprint:
                _reject("replay_fingerprint_mismatch")
            self._replays.move_to_end(replay_key)
            return replay.result
        if len(self._replays) >= _MAX_RETAINED_RESULTS:
            _reject("runtime_replay_capacity_exhausted")

        stage_request_id = self._stage_request_id(item)
        output = self.stage.execute(
            request_id=stage_request_id,
            assignment_id=str(self._binding["assignment_id"]),
            stage_id=str(self._binding["stage_id"]),
            hidden=hidden,
        )
        try:
            flattened = [float(value) for row in output for value in row]
            encoded = encode_activation(
                dtype="float32",
                shape=(1, len(output), int(self._binding["hidden_size"])),
                data=struct.pack(f"<{len(flattened)}f", *flattened),
            )
        except BaseException:
            self.stage.release_requests((stage_request_id,))
            raise
        requests = self._requests_by_path.setdefault(item.path_id, set())
        requests.add(stage_request_id)
        self._requests_by_path.move_to_end(item.path_id)
        result = RuntimeResult(success=True, payload=encoded)
        self._replays[replay_key] = _ReplayResult(
            fingerprint=fingerprint,
            result=result,
        )
        self._replays.move_to_end(replay_key)
        self._applied_operation_count += 1
        return result

    def execute(self, item: HopWorkItem) -> RuntimeResult:
        if not isinstance(item, HopWorkItem):
            return self._failure("invalid_runtime_work_item")
        if not isinstance(item.path_id, str) or not item.path_id:
            return self._failure("invalid_runtime_path_id")
        with self._lock:
            if self._closed:
                return self._failure("runtime_closed")
            if self._cancellation_fence_saturated:
                return self._failure("cancellation_fence_saturated")
            if item.path_id in self._cancelled_paths:
                return self._failure("path_cancelled")
            try:
                return self._execute_bound(item)
            except (PayloadError, PixelRuntimeError) as exc:
                return self._failure(exc.code)
            except PixelStageError as exc:
                return self._failure(str(exc) or "pixel_stage_execution_rejected")
            except Exception:
                return self._failure("pixel_stage_execution_rejected")

    def execute_batch(self, batch: RuntimeBatch) -> tuple[RuntimeResult, ...]:
        if not isinstance(batch, RuntimeBatch):
            raise PixelRuntimeError("invalid_runtime_batch")
        if type(batch.items) is not tuple:
            return (self._failure("invalid_runtime_batch_items"),)
        return tuple(self.execute(item) for item in batch.items)

    def cancel(self, path_id: str) -> None:
        if not isinstance(path_id, str):
            return
        with self._lock:
            self._remember_cancelled_path(path_id)
            request_ids = tuple(self._requests_by_path.pop(path_id, set()))
            for key in tuple(self._replays):
                if key[0] == path_id:
                    self._replays.pop(key, None)
            self.stage.release_requests(request_ids)

    def close(self, *, reason: str = "worker_shutdown") -> None:
        if not isinstance(reason, str) or not reason:
            raise PixelRuntimeError("invalid_runtime_close_reason")
        with self._lock:
            if self._closed:
                return
            self.stage.release_requests()
            self._requests_by_path.clear()
            self._replays.clear()
            self._cancelled_paths.clear()
            self._cancellation_fence_saturated = False
            self._closed = True
            self._release_counts[reason] = self._release_counts.get(reason, 0) + 1

    def kv_snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "mode": self.decode_mode,
                "backend": self.backend,
                "closed": self._closed,
                "active_state_count": 0,
                "retained_result_count": len(self._replays),
                "applied_operation_count": self._applied_operation_count,
                "cancelled_path_count": len(self._cancelled_paths),
                "cancellation_fence_saturated": self._cancellation_fence_saturated,
                "release_counts": dict(sorted(self._release_counts.items())),
                "route_ready": False,
            }


__all__ = ["PixelRuntimeError", "PixelStageRuntimePort"]
