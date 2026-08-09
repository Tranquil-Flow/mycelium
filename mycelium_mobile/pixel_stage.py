"""Pure-stdlib exact GPT-2 decoder stage for an isolated Android worker.

This module is intentionally dependency-free so the same reviewed bytes run in
Termux without installing packages. It executes one assignment-derived decoder
substage and never claims route readiness. The HTTP entry point is a bounded
physical-qualification data plane, not the production Router transport.
"""

from __future__ import annotations

import argparse
from collections import OrderedDict
import hashlib
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import math
import os
from pathlib import Path
import platform
import tempfile
import threading
import time
from types import MappingProxyType
from typing import Any, Mapping, NoReturn, Sequence
import uuid

STAGE_PACK_PROTOCOL = "mycelium.pixel_stage_pack.v1"
STAGE_REQUEST_PROTOCOL = "mycelium.pixel_stage_request.v1"
STAGE_RESPONSE_PROTOCOL = "mycelium.pixel_stage_response.v1"
STAGE_EVIDENCE_PROTOCOL = "mycelium.pixel_stage_evidence.v1"
MAX_BODY_BYTES = 1024 * 1024
MAX_SEQUENCE_LENGTH = 256
MAX_HIDDEN_SIZE = 4096
MAX_INNER_SIZE = 16384
MAX_REPLAY_RESULTS = 128
MAX_ACTIVE_CONNECTIONS = 16
SOCKET_TIMEOUT_SECONDS = 5.0
TOKEN_HEADER = "x-mycelium-stage-token"


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    return value

_PACK_FIELDS = frozenset(
    {
        "protocol",
        "run_id",
        "deployment_id",
        "assignment_id",
        "stage_id",
        "model_id",
        "resolved_commit",
        "manifest_digest",
        "parent_assignment_digest",
        "parent_load_proof_digest",
        "component_roles",
        "derived_substage",
        "start_layer",
        "end_layer_exclusive",
        "n_head",
        "hidden_size",
        "epsilon",
        "activation_function",
        "scale_attn_weights",
        "scale_attn_by_inverse_layer_idx",
        "reorder_and_upcast_attn",
        "add_cross_attention",
        "tensors",
        "pack_digest",
        "route_ready",
    }
)
_REQUEST_FIELDS = frozenset(
    {
        "protocol",
        "request_id",
        "assignment_id",
        "stage_id",
        "hidden",
        "input_digest",
    }
)


class PixelStageError(ValueError):
    """Fail-closed stage or wire validation error with a stable public code."""


def _reject(code: str) -> NoReturn:
    raise PixelStageError(code)


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            _reject("duplicate_json_key")
        result[key] = value
    return result


def _reject_nonfinite_json(_value: str) -> Any:
    _reject("nonfinite_json_number")


def _canonical(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=True,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("ascii")
    except (TypeError, ValueError, UnicodeError):
        _reject("noncanonical_document")


def _digest(value: Any) -> str:
    return "sha256:" + hashlib.sha256(_canonical(value)).hexdigest()


def _strict_json_bytes(raw: bytes) -> Any:
    try:
        return json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_nonfinite_json,
        )
    except PixelStageError:
        raise
    except (UnicodeError, ValueError, RecursionError):
        _reject("invalid_json")


def _finite_number(value: Any, code: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        _reject(code)
    numeric = float(value)
    if not math.isfinite(numeric):
        _reject(code)
    return numeric


def _positive_int(value: Any, code: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        _reject(code)
    return value


def _nonempty_string(value: Any, code: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 512:
        _reject(code)
    return value


def _vector(value: Any, width: int, code: str) -> list[float]:
    if not isinstance(value, list) or len(value) != width:
        _reject(code)
    return [_finite_number(item, code) for item in value]


def _matrix(value: Any, rows: int, columns: int, code: str) -> list[list[float]]:
    if not isinstance(value, list) or len(value) != rows:
        _reject(code)
    return [_vector(row, columns, code) for row in value]


def _add(left: Sequence[float], right: Sequence[float]) -> list[float]:
    return [a + b for a, b in zip(left, right)]


def _linear(
    rows: Sequence[Sequence[float]],
    weight: Sequence[Sequence[float]],
    bias: Sequence[float],
) -> list[list[float]]:
    output_width = len(bias)
    return [
        [
            sum(
                float(row[index]) * float(weight[index][column])
                for index in range(len(row))
            )
            + float(bias[column])
            for column in range(output_width)
        ]
        for row in rows
    ]


def _layer_norm(
    rows: Sequence[Sequence[float]],
    weight: Sequence[float],
    bias: Sequence[float],
    epsilon: float,
) -> list[list[float]]:
    normalized: list[list[float]] = []
    for row in rows:
        mean = sum(row) / len(row)
        variance = sum((value - mean) ** 2 for value in row) / len(row)
        inverse = 1.0 / math.sqrt(variance + epsilon)
        normalized.append(
            [
                (value - mean) * inverse * float(weight[index]) + float(bias[index])
                for index, value in enumerate(row)
            ]
        )
    return normalized


def _softmax(values: Sequence[float]) -> list[float]:
    maximum = max(values)
    exponentials = [math.exp(value - maximum) for value in values]
    denominator = sum(exponentials)
    return [value / denominator for value in exponentials]


def _gelu_new(value: float) -> float:
    return (
        0.5
        * value
        * (1.0 + math.tanh(math.sqrt(2.0 / math.pi) * (value + 0.044715 * value**3)))
    )


def build_stage_pack(
    *,
    run_id: str,
    deployment_id: str,
    assignment_id: str,
    stage_id: str,
    model_id: str,
    resolved_commit: str,
    manifest_digest: str,
    parent_assignment_digest: str,
    parent_load_proof_digest: str,
    start_layer: int,
    end_layer_exclusive: int,
    n_head: int,
    hidden_size: int,
    epsilon: float,
    activation_function: str,
    scale_attn_weights: bool,
    scale_attn_by_inverse_layer_idx: bool,
    reorder_and_upcast_attn: bool,
    add_cross_attention: bool,
    tensors: Mapping[str, Any],
) -> dict[str, Any]:
    """Build a canonical, digest-bound one-layer mobile stage document."""

    unsigned = {
        "protocol": STAGE_PACK_PROTOCOL,
        "run_id": run_id,
        "deployment_id": deployment_id,
        "assignment_id": assignment_id,
        "stage_id": stage_id,
        "model_id": model_id,
        "resolved_commit": resolved_commit,
        "manifest_digest": manifest_digest,
        "parent_assignment_digest": parent_assignment_digest,
        "parent_load_proof_digest": parent_load_proof_digest,
        "component_roles": ["decoder"],
        "derived_substage": True,
        "start_layer": start_layer,
        "end_layer_exclusive": end_layer_exclusive,
        "n_head": n_head,
        "hidden_size": hidden_size,
        "epsilon": epsilon,
        "activation_function": activation_function,
        "scale_attn_weights": scale_attn_weights,
        "scale_attn_by_inverse_layer_idx": scale_attn_by_inverse_layer_idx,
        "reorder_and_upcast_attn": reorder_and_upcast_attn,
        "add_cross_attention": add_cross_attention,
        "tensors": dict(tensors),
        "route_ready": False,
    }
    document = {**unsigned, "pack_digest": _digest(unsigned)}
    PixelStage.from_document(document)
    return document


class PixelStage:
    """One exact assignment-derived GPT-2 decoder block with replay defense."""

    __slots__ = (
        "_document",
        "_tensors",
        "_inner_size",
        "request_count",
        "_replay",
        "_lock",
    )

    def __setattr__(self, name: str, value: Any) -> None:
        if name in {"_document", "_tensors", "_inner_size"} and hasattr(self, name):
            raise AttributeError(f"{name[1:]} is immutable")
        object.__setattr__(self, name, value)

    def __init__(
        self,
        *,
        document: Mapping[str, Any],
        tensors: Mapping[str, Any],
        inner_size: int,
    ) -> None:
        self._document = _freeze(document)
        self._tensors = _freeze(tensors)
        self._inner_size = inner_size
        self.request_count = 0
        self._replay: OrderedDict[str, tuple[str, tuple[tuple[float, ...], ...]]] = (
            OrderedDict()
        )
        self._lock = threading.Lock()

    @property
    def document(self) -> Mapping[str, Any]:
        return self._document

    @property
    def tensors(self) -> Mapping[str, Any]:
        return self._tensors

    @property
    def inner_size(self) -> int:
        return self._inner_size

    @classmethod
    def from_document(cls, value: Any) -> "PixelStage":
        if not isinstance(value, dict) or set(value) != set(_PACK_FIELDS):
            _reject("stage_pack_fields_invalid")
        if value.get("protocol") != STAGE_PACK_PROTOCOL:
            _reject("stage_pack_protocol_invalid")
        if value.get("route_ready") is not False:
            _reject("stage_pack_route_ready_invalid")
        unsigned = {key: item for key, item in value.items() if key != "pack_digest"}
        if value.get("pack_digest") != _digest(unsigned):
            _reject("stage_pack_digest_mismatch")

        for field in (
            "run_id",
            "deployment_id",
            "assignment_id",
            "stage_id",
            "model_id",
            "resolved_commit",
            "manifest_digest",
            "parent_assignment_digest",
            "parent_load_proof_digest",
        ):
            _nonempty_string(value.get(field), f"stage_pack_{field}_invalid")
        for field in (
            "manifest_digest",
            "parent_assignment_digest",
            "parent_load_proof_digest",
        ):
            digest = value[field]
            if (
                not isinstance(digest, str)
                or not digest.startswith("sha256:")
                or len(digest) != 71
                or any(character not in "0123456789abcdef" for character in digest[7:])
            ):
                _reject(f"stage_pack_{field}_invalid")
        if value.get("component_roles") != ["decoder"]:
            _reject("stage_pack_component_roles_invalid")
        if value.get("derived_substage") is not True:
            _reject("stage_pack_derived_substage_invalid")
        start = _positive_int(value.get("start_layer"), "stage_pack_range_invalid")
        end = _positive_int(
            value.get("end_layer_exclusive"), "stage_pack_range_invalid"
        )
        if end != start + 1:
            _reject("stage_pack_range_invalid")
        hidden_size = _positive_int(
            value.get("hidden_size"), "stage_pack_hidden_size_invalid"
        )
        if hidden_size > MAX_HIDDEN_SIZE:
            _reject("stage_pack_hidden_size_invalid")
        n_head = _positive_int(value.get("n_head"), "stage_pack_n_head_invalid")
        if hidden_size % n_head:
            _reject("stage_pack_n_head_invalid")
        epsilon = _finite_number(value.get("epsilon"), "stage_pack_epsilon_invalid")
        if epsilon <= 0.0:
            _reject("stage_pack_epsilon_invalid")
        if value.get("activation_function") != "gelu_new":
            _reject("stage_pack_activation_function_unsupported")
        if value.get("scale_attn_weights") is not True:
            _reject("stage_pack_scale_attn_weights_unsupported")
        if value.get("scale_attn_by_inverse_layer_idx") is not False:
            _reject("stage_pack_inverse_layer_scaling_unsupported")
        if value.get("reorder_and_upcast_attn") is not False:
            _reject("stage_pack_reordered_attention_unsupported")
        if value.get("add_cross_attention") is not False:
            _reject("stage_pack_cross_attention_unsupported")

        prefix = f"transformer.h.{start}."
        expected = {
            prefix + suffix
            for suffix in (
                "ln_1.weight",
                "ln_1.bias",
                "attn.c_attn.weight",
                "attn.c_attn.bias",
                "attn.c_proj.weight",
                "attn.c_proj.bias",
                "ln_2.weight",
                "ln_2.bias",
                "mlp.c_fc.weight",
                "mlp.c_fc.bias",
                "mlp.c_proj.weight",
                "mlp.c_proj.bias",
            )
        }
        raw_tensors = value.get("tensors")
        if not isinstance(raw_tensors, dict) or set(raw_tensors) != expected:
            _reject("stage_pack_tensor_set_invalid")
        inner_raw = raw_tensors[prefix + "mlp.c_fc.bias"]
        if not isinstance(inner_raw, list):
            _reject("stage_pack_tensor_shape_invalid")
        inner_size = _positive_int(len(inner_raw), "stage_pack_tensor_shape_invalid")
        if inner_size > MAX_INNER_SIZE:
            _reject("stage_pack_tensor_shape_invalid")
        tensors = {
            prefix + "ln_1.weight": _vector(
                raw_tensors[prefix + "ln_1.weight"],
                hidden_size,
                "stage_pack_tensor_shape_invalid",
            ),
            prefix + "ln_1.bias": _vector(
                raw_tensors[prefix + "ln_1.bias"],
                hidden_size,
                "stage_pack_tensor_shape_invalid",
            ),
            prefix + "attn.c_attn.weight": _matrix(
                raw_tensors[prefix + "attn.c_attn.weight"],
                hidden_size,
                3 * hidden_size,
                "stage_pack_tensor_shape_invalid",
            ),
            prefix + "attn.c_attn.bias": _vector(
                raw_tensors[prefix + "attn.c_attn.bias"],
                3 * hidden_size,
                "stage_pack_tensor_shape_invalid",
            ),
            prefix + "attn.c_proj.weight": _matrix(
                raw_tensors[prefix + "attn.c_proj.weight"],
                hidden_size,
                hidden_size,
                "stage_pack_tensor_shape_invalid",
            ),
            prefix + "attn.c_proj.bias": _vector(
                raw_tensors[prefix + "attn.c_proj.bias"],
                hidden_size,
                "stage_pack_tensor_shape_invalid",
            ),
            prefix + "ln_2.weight": _vector(
                raw_tensors[prefix + "ln_2.weight"],
                hidden_size,
                "stage_pack_tensor_shape_invalid",
            ),
            prefix + "ln_2.bias": _vector(
                raw_tensors[prefix + "ln_2.bias"],
                hidden_size,
                "stage_pack_tensor_shape_invalid",
            ),
            prefix + "mlp.c_fc.weight": _matrix(
                raw_tensors[prefix + "mlp.c_fc.weight"],
                hidden_size,
                inner_size,
                "stage_pack_tensor_shape_invalid",
            ),
            prefix + "mlp.c_fc.bias": _vector(
                raw_tensors[prefix + "mlp.c_fc.bias"],
                inner_size,
                "stage_pack_tensor_shape_invalid",
            ),
            prefix + "mlp.c_proj.weight": _matrix(
                raw_tensors[prefix + "mlp.c_proj.weight"],
                inner_size,
                hidden_size,
                "stage_pack_tensor_shape_invalid",
            ),
            prefix + "mlp.c_proj.bias": _vector(
                raw_tensors[prefix + "mlp.c_proj.bias"],
                hidden_size,
                "stage_pack_tensor_shape_invalid",
            ),
        }
        return cls(document=value, tensors=tensors, inner_size=inner_size)

    @property
    def prefix(self) -> str:
        return f"transformer.h.{self.document['start_layer']}."

    def _execute_rows(self, hidden: list[list[float]]) -> list[list[float]]:
        prefix = self.prefix
        tensors = self.tensors
        hidden_size = int(self.document["hidden_size"])
        n_head = int(self.document["n_head"])
        head_size = hidden_size // n_head
        epsilon = float(self.document["epsilon"])

        normalized = _layer_norm(
            hidden,
            tensors[prefix + "ln_1.weight"],
            tensors[prefix + "ln_1.bias"],
            epsilon,
        )
        qkv = _linear(
            normalized,
            tensors[prefix + "attn.c_attn.weight"],
            tensors[prefix + "attn.c_attn.bias"],
        )
        queries = [row[:hidden_size] for row in qkv]
        keys = [row[hidden_size : 2 * hidden_size] for row in qkv]
        values = [row[2 * hidden_size :] for row in qkv]
        attended: list[list[float]] = []
        scale = head_size**-0.5
        for token_index, query in enumerate(queries):
            combined: list[float] = []
            for head in range(n_head):
                start = head * head_size
                scores = [
                    sum(
                        query[start + offset] * keys[key_index][start + offset]
                        for offset in range(head_size)
                    )
                    * scale
                    for key_index in range(token_index + 1)
                ]
                weights = _softmax(scores)
                combined.extend(
                    sum(
                        weights[key_index] * values[key_index][start + offset]
                        for key_index in range(token_index + 1)
                    )
                    for offset in range(head_size)
                )
            attended.append(combined)
        attention = _linear(
            attended,
            tensors[prefix + "attn.c_proj.weight"],
            tensors[prefix + "attn.c_proj.bias"],
        )
        after_attention = [_add(row, delta) for row, delta in zip(hidden, attention)]
        normalized = _layer_norm(
            after_attention,
            tensors[prefix + "ln_2.weight"],
            tensors[prefix + "ln_2.bias"],
            epsilon,
        )
        feed_forward = _linear(
            normalized,
            tensors[prefix + "mlp.c_fc.weight"],
            tensors[prefix + "mlp.c_fc.bias"],
        )
        activated = [[_gelu_new(value) for value in row] for row in feed_forward]
        projected = _linear(
            activated,
            tensors[prefix + "mlp.c_proj.weight"],
            tensors[prefix + "mlp.c_proj.bias"],
        )
        output = [_add(row, delta) for row, delta in zip(after_attention, projected)]
        if any(not math.isfinite(value) for row in output for value in row):
            _reject("stage_output_nonfinite")
        return output

    def execute(
        self,
        *,
        request_id: str,
        assignment_id: str,
        stage_id: str,
        hidden: Any,
    ) -> list[list[float]]:
        request_id = _nonempty_string(request_id, "request_id_invalid")
        if assignment_id != self.document["assignment_id"]:
            _reject("request_assignment_mismatch")
        if stage_id != self.document["stage_id"]:
            _reject("request_stage_mismatch")
        if not isinstance(hidden, list) or not 1 <= len(hidden) <= MAX_SEQUENCE_LENGTH:
            _reject("request_hidden_shape_invalid")
        rows = [
            _vector(
                row,
                int(self.document["hidden_size"]),
                "request_hidden_nonfinite"
                if isinstance(row, list)
                and len(row) == int(self.document["hidden_size"])
                else "request_hidden_shape_invalid",
            )
            for row in hidden
        ]
        fingerprint = _digest(
            {
                "assignment_id": assignment_id,
                "stage_id": stage_id,
                "hidden": rows,
            }
        )
        with self._lock:
            replay = self._replay.get(request_id)
            if replay is not None:
                if replay[0] != fingerprint:
                    _reject("request_replay_conflict")
                self._replay.move_to_end(request_id)
                return [list(row) for row in replay[1]]
            if len(self._replay) >= MAX_REPLAY_RESULTS:
                _reject("request_replay_ledger_full")
            output = self._execute_rows(rows)
            frozen = tuple(tuple(value for value in row) for row in output)
            self._replay[request_id] = (fingerprint, frozen)
            self.request_count += 1
            return [list(row) for row in frozen]

    def release_requests(self, request_ids: Sequence[str] | None = None) -> int:
        """Release selected replay entries, or every entry during shutdown."""

        if request_ids is not None and (
            not isinstance(request_ids, Sequence)
            or isinstance(request_ids, (str, bytes, bytearray))
            or not all(isinstance(value, str) and value for value in request_ids)
        ):
            _reject("release_request_ids_invalid")
        with self._lock:
            if request_ids is None:
                released = len(self._replay)
                self._replay.clear()
                return released
            released = 0
            for request_id in set(request_ids):
                if self._replay.pop(request_id, None) is not None:
                    released += 1
            return released


class _StageServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(
        self,
        address: tuple[str, int],
        *,
        stage: PixelStage,
        token: bytes,
        evidence_file: Path,
        boot_id: str,
    ) -> None:
        super().__init__(address, _StageHandler)
        self.stage = stage
        self.token = token
        self.evidence_file = evidence_file
        self.runtime_instance_id = str(uuid.uuid4())
        self.started_monotonic = time.monotonic()
        self.worker_source_digest = (
            "sha256:"
            + hashlib.sha256(
                Path(__file__).resolve(strict=True).read_bytes()
            ).hexdigest()
        )
        self.boot_id = boot_id.strip()
        if not self.boot_id:
            _reject("worker_boot_id_invalid")
        self.last_evidence: dict[str, Any] | None = None
        self.execution_lock = threading.Lock()
        self._connection_slots = threading.BoundedSemaphore(MAX_ACTIVE_CONNECTIONS)

    def get_request(self) -> tuple[Any, Any]:
        request, address = super().get_request()
        request.settimeout(SOCKET_TIMEOUT_SECONDS)
        return request, address

    def process_request(self, request: Any, client_address: Any) -> None:
        if not self._connection_slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._connection_slots.release()
            raise

    def process_request_thread(self, request: Any, client_address: Any) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._connection_slots.release()

    def write_evidence(
        self,
        *,
        request_id: str,
        input_digest: str,
        output_digest: str,
        duration_ms: float,
    ) -> dict[str, Any]:
        evidence = {
            "protocol": STAGE_EVIDENCE_PROTOCOL,
            "route_ready": False,
            "run_id": self.stage.document["run_id"],
            "deployment_id": self.stage.document["deployment_id"],
            "assignment_id": self.stage.document["assignment_id"],
            "stage_id": self.stage.document["stage_id"],
            "pack_digest": self.stage.document["pack_digest"],
            "parent_assignment_digest": self.stage.document["parent_assignment_digest"],
            "parent_load_proof_digest": self.stage.document["parent_load_proof_digest"],
            "worker_source_digest": self.worker_source_digest,
            "boot_id": self.boot_id,
            "runtime_instance_id": self.runtime_instance_id,
            "process_id": os.getpid(),
            "process_host_id": platform.node(),
            "machine": platform.machine(),
            "python_version": platform.python_version(),
            "request_id": request_id,
            "request_count": self.stage.request_count,
            "input_digest": input_digest,
            "output_digest": output_digest,
            "duration_ms": duration_ms,
        }
        raw = _canonical(evidence)
        self.evidence_file.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(
            prefix=f".{self.evidence_file.name}.", dir=self.evidence_file.parent
        )
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.evidence_file)
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
        self.last_evidence = evidence
        return evidence


class _StageHandler(BaseHTTPRequestHandler):
    server: _StageServer
    server_version = "MyceliumPixelStage/1"
    sys_version = ""

    def log_message(self, _format: str, *_args: Any) -> None:
        return

    def _send(self, status: int, value: Mapping[str, Any]) -> None:
        raw = _canonical(dict(value))
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(raw)))
        self.send_header("cache-control", "no-store")
        self.send_header("connection", "close")
        self.end_headers()
        self.wfile.write(raw)
        self.close_connection = True

    def _authorized(self) -> bool:
        supplied_values = self.headers.get_all(TOKEN_HEADER, [])
        if len(supplied_values) != 1:
            return False
        supplied = supplied_values[0].encode("utf-8")
        return bool(supplied) and hmac.compare_digest(supplied, self.server.token)

    def _document(self) -> Any:
        if self.headers.get_all("transfer-encoding", []) or self.headers.get_all(
            "content-encoding", []
        ):
            _reject("request_encoding_invalid")
        content_type_values = self.headers.get_all("content-type", [])
        if len(content_type_values) != 1:
            _reject("request_content_type_invalid")
        content_type = content_type_values[0].split(";", 1)[0].strip().lower()
        if content_type != "application/json":
            _reject("request_content_type_invalid")
        raw_lengths = self.headers.get_all("content-length", [])
        if len(raw_lengths) != 1:
            _reject("request_content_length_invalid")
        raw_length = raw_lengths[0]
        try:
            length = int(raw_length) if raw_length is not None else -1
        except ValueError:
            _reject("request_content_length_invalid")
        if not 0 <= length <= MAX_BODY_BYTES:
            _reject("request_content_length_invalid")
        raw = self.rfile.read(length)
        if len(raw) != length:
            _reject("request_body_truncated")
        return _strict_json_bytes(raw)

    def do_GET(self) -> None:
        if not self._authorized():
            self._send(401, {"error": "unauthorized"})
            return
        if self.path != "/health":
            self._send(404, {"error": "not_found"})
            return
        self._send(
            200,
            {
                "status": "ok",
                "route_ready": False,
                "protocol": STAGE_RESPONSE_PROTOCOL,
                "run_id": self.server.stage.document["run_id"],
                "deployment_id": self.server.stage.document["deployment_id"],
                "assignment_id": self.server.stage.document["assignment_id"],
                "stage_id": self.server.stage.document["stage_id"],
                "pack_digest": self.server.stage.document["pack_digest"],
                "parent_assignment_digest": self.server.stage.document[
                    "parent_assignment_digest"
                ],
                "parent_load_proof_digest": self.server.stage.document[
                    "parent_load_proof_digest"
                ],
                "worker_source_digest": self.server.worker_source_digest,
                "boot_id": self.server.boot_id,
                "runtime_instance_id": self.server.runtime_instance_id,
                "request_count": self.server.stage.request_count,
            },
        )

    def do_POST(self) -> None:
        if not self._authorized():
            self._send(401, {"error": "unauthorized"})
            return
        try:
            if self.path == "/shutdown":
                self._send(200, {"status": "stopping", "route_ready": False})
                threading.Thread(target=self.server.shutdown, daemon=True).start()
                return
            if self.path != "/execute":
                self._send(404, {"error": "not_found"})
                return
            document = self._document()
            if not isinstance(document, dict) or set(document) != set(_REQUEST_FIELDS):
                _reject("request_fields_invalid")
            if document.get("protocol") != STAGE_REQUEST_PROTOCOL:
                _reject("request_protocol_invalid")
            if document.get("input_digest") != _digest(document.get("hidden")):
                _reject("request_input_digest_mismatch")
            with self.server.execution_lock:
                started = time.monotonic()
                output = self.server.stage.execute(
                    request_id=document.get("request_id"),
                    assignment_id=document.get("assignment_id"),
                    stage_id=document.get("stage_id"),
                    hidden=document.get("hidden"),
                )
                duration_ms = (time.monotonic() - started) * 1000.0
                output_digest = _digest(output)
                evidence = self.server.write_evidence(
                    request_id=document["request_id"],
                    input_digest=document["input_digest"],
                    output_digest=output_digest,
                    duration_ms=duration_ms,
                )
                response = {
                    "protocol": STAGE_RESPONSE_PROTOCOL,
                    "route_ready": False,
                    "run_id": self.server.stage.document["run_id"],
                    "deployment_id": self.server.stage.document["deployment_id"],
                    "request_id": document["request_id"],
                    "assignment_id": document["assignment_id"],
                    "stage_id": document["stage_id"],
                    "pack_digest": self.server.stage.document["pack_digest"],
                    "parent_assignment_digest": self.server.stage.document[
                        "parent_assignment_digest"
                    ],
                    "parent_load_proof_digest": self.server.stage.document[
                        "parent_load_proof_digest"
                    ],
                    "worker_source_digest": self.server.worker_source_digest,
                    "boot_id": self.server.boot_id,
                    "runtime_instance_id": self.server.runtime_instance_id,
                    "request_count": self.server.stage.request_count,
                    "output": output,
                    "output_digest": output_digest,
                    "evidence_digest": _digest(evidence),
                    "duration_ms": duration_ms,
                }
            self._send(200, response)
        except PixelStageError as exc:
            self._send(400, {"error": str(exc), "route_ready": False})


def serve(
    *,
    pack_file: Path,
    token_file: Path,
    evidence_file: Path,
    bind: str,
    port: int,
) -> None:
    if pack_file.is_symlink() or token_file.is_symlink() or evidence_file.is_symlink():
        _reject("worker_path_symlink_forbidden")
    if pack_file.stat().st_size > MAX_BODY_BYTES:
        _reject("stage_pack_too_large")
    pack_raw = pack_file.read_bytes()
    pack = _strict_json_bytes(pack_raw)
    stage = PixelStage.from_document(pack)
    token_raw = token_file.read_bytes()
    if len(token_raw) > 1024:
        _reject("worker_token_invalid")
    token = token_raw.strip()
    if len(token) < 32 or b"\n" in token or b"\r" in token:
        _reject("worker_token_invalid")
    boot_id = (
        Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip()
    )
    server = _StageServer(
        (bind, port),
        stage=stage,
        token=token,
        evidence_file=evidence_file,
        boot_id=boot_id,
    )
    try:
        server.serve_forever(poll_interval=0.1)
    finally:
        server.server_close()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Mycelium isolated Pixel stage worker")
    parser.add_argument("--pack", type=Path, required=True)
    parser.add_argument("--token-file", type=Path, required=True)
    parser.add_argument("--evidence-file", type=Path, required=True)
    parser.add_argument("--bind", required=True)
    parser.add_argument("--port", type=int, default=9018)
    args = parser.parse_args(argv)
    if not 1 <= args.port <= 65535:
        _reject("worker_port_invalid")
    serve(
        pack_file=args.pack,
        token_file=args.token_file,
        evidence_file=args.evidence_file,
        bind=args.bind,
        port=args.port,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
