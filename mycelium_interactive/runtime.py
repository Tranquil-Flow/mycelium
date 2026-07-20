"""Interactive local distributed-inference runtime.

This module wires the reviewed physical-qualification artifacts into an
operator-driven browser worker loop. It is local evidence only; ``route_ready``
never becomes true.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
import hashlib
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Mapping
import uuid

import mlx.core as mx

from mycelium_mobile.pixel_stage import PixelStage, build_stage_pack
from mycelium_qualification.evidence import canonical_json_bytes
from mycelium_qualification.physical_deployment import prepare_physical_deployment
from mycelium_router.mlx_runtime import _gpt2_block_with_kv
from physical_pixel_host_stage import _final_logits
from runtime_loader import canonical_json, execute_loaded_stage, load_assignment_stage

from .swarm import SwarmCoordinator, SwarmError

VOCABULARY = ("<pad>", "moon", "moss", "spark", "river", "owl", "seed")
LOAD_GENERATION = 29
INTERMEDIATE_TOLERANCE = 1e-6
LOGIT_TOLERANCE = 2e-6


class InteractiveRuntimeError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _reject(code: str) -> None:
    raise InteractiveRuntimeError(code)


@contextmanager
def _cancellable_lock(lock: Any, cancel_event: threading.Event) -> Any:
    while True:
        if cancel_event.is_set():
            _reject("request_cancelled")
        if lock.acquire(timeout=0.05):
            break
    try:
        if cancel_event.is_set():
            _reject("request_cancelled")
        yield
    finally:
        lock.release()


def _digest(value: Any) -> str:
    return "sha256:" + hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _quantized_argmax(values: list[float], *, quantum: float = 1e-5) -> int:
    buckets = [round(float(value) / quantum) for value in values]
    return max(range(len(buckets)), key=buckets.__getitem__)


def _max_error(left: list[list[float]], right: list[list[float]]) -> float:
    if len(left) != len(right):
        return float("inf")
    maximum = 0.0
    for left_row, right_row in zip(left, right):
        if len(left_row) != len(right_row):
            return float("inf")
        for left_value, right_value in zip(left_row, right_row):
            maximum = max(maximum, abs(float(left_value) - float(right_value)))
    return maximum


def _prompt_tokens(prompt: str, *, max_context_tokens: int) -> list[int]:
    if not isinstance(prompt, str):
        _reject("prompt_invalid")
    text = prompt.strip()
    if len(text) > 512:
        _reject("prompt_too_large")
    if not text:
        return [1, 2, 3]
    encoded = text.encode("utf-8")
    tokens = [(byte % (len(VOCABULARY) - 1)) + 1 for byte in encoded]
    return tokens[:max_context_tokens] or [1]


@dataclass(frozen=True, slots=True)
class InferenceRecord:
    protocol: str
    request_id: str
    prompt_digest: str
    prompt_bytes: int
    initial_tokens: tuple[int, ...]
    generated_tokens: tuple[int, ...]
    generated_labels: tuple[str, ...]
    max_intermediate_error: float
    max_logit_error: float
    peer_ids: tuple[str, ...]
    required_distinct_peers: int
    observed_distinct_peers: int
    stage_pack_digest: str
    token_records: tuple[dict[str, Any], ...]
    created_at: float
    completed_at: float
    route_ready: bool = False
    local_evidence_only: bool = True

    def public_document(self) -> dict[str, Any]:
        return {
            "protocol": self.protocol,
            "request_id": self.request_id,
            "prompt_digest": self.prompt_digest,
            "prompt_bytes": self.prompt_bytes,
            "initial_tokens": list(self.initial_tokens),
            "generated_tokens": list(self.generated_tokens),
            "generated_labels": list(self.generated_labels),
            "max_intermediate_error": self.max_intermediate_error,
            "max_logit_error": self.max_logit_error,
            "peer_ids": list(self.peer_ids),
            "required_distinct_peers": self.required_distinct_peers,
            "observed_distinct_peers": self.observed_distinct_peers,
            "stage_pack_digest": self.stage_pack_digest,
            "token_records": list(self.token_records),
            "created_at": self.created_at,
            "completed_at": self.completed_at,
            "route_ready": self.route_ready,
            "local_evidence_only": self.local_evidence_only,
        }


@dataclass
class InteractiveRuntime:
    root: Path | None = None
    max_records: int = 32
    run_id: str = field(default_factory=lambda: str(uuid.uuid4()))

    def __post_init__(self) -> None:
        self._lock = threading.RLock()
        self._infer_lock = threading.Lock()
        self._active_requests: dict[str, str | None] = {}
        self._cancel_events: dict[str, threading.Event] = {}
        self._cancelled_requests: set[str] = set()
        self._tempdir: tempfile.TemporaryDirectory[str] | None = None
        if (
            not isinstance(self.run_id, str)
            or not self.run_id
            or len(self.run_id) > 128
            or self.run_id in {".", ".."}
            or "/" in self.run_id
            or "\\" in self.run_id
            or any(ord(character) < 0x20 for character in self.run_id)
        ):
            _reject("run_id_invalid")
        if self.root is None:
            self._tempdir = tempfile.TemporaryDirectory(prefix="mycelium-interactive-")
            self.root = Path(self._tempdir.name)
        else:
            self.root = Path(self.root)
            self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        run_root = self.root / "runs" / self.run_id
        try:
            run_root.mkdir(mode=0o700, parents=True, exist_ok=False)
        except OSError as exc:
            raise InteractiveRuntimeError("run_root_create_failed") from exc
        deployment = prepare_physical_deployment(run_root / "deployment")
        loaded = [
            load_assignment_stage(assignment, report, load_generation=LOAD_GENERATION)
            for assignment, report in zip(deployment.assignments, deployment.artifact_reports)
        ]
        reference = load_assignment_stage(
            deployment.reference_assignment,
            deployment.reference_report,
            load_generation=LOAD_GENERATION,
        )
        stage_pack = self._pixel_pack(deployment, loaded)
        self.deployment = deployment
        self.loaded = loaded
        self.reference = reference
        self.stage_pack = stage_pack
        self.pixel_stage = PixelStage.from_document(stage_pack)
        self.swarm = SwarmCoordinator(stage_pack=stage_pack)
        self.records: dict[str, InferenceRecord] = {}
        self._record_order: list[str] = []
        self.config = loaded[1].proof["runtime"]["model_config"]

    def _pixel_pack(self, deployment: Any, loaded: list[Any]) -> dict[str, Any]:
        assignment = deployment.assignments[1]
        config = assignment["runtime"]["model_config"]
        tensors = {
            key: value.tolist()
            for key, value in loaded[1].tensors.items()
            if key.startswith("transformer.h.1.")
        }
        return build_stage_pack(
            run_id=self.run_id,
            deployment_id=assignment["deployment_id"],
            assignment_id=assignment["assignment_id"],
            stage_id="interactive-browser-stage-001",
            model_id=assignment["model_id"],
            resolved_commit=assignment["resolved_commit"],
            manifest_digest=assignment["manifest_digest"],
            parent_assignment_digest=_digest(assignment),
            parent_load_proof_digest=_digest(loaded[1].proof),
            start_layer=1,
            end_layer_exclusive=2,
            n_head=assignment["runtime"]["model_config"]["n_head"],
            hidden_size=assignment["runtime"]["model_config"]["n_embd"],
            epsilon=assignment["runtime"]["model_config"]["layer_norm_epsilon"],
            activation_function=config["activation_function"],
            scale_attn_weights=config["scale_attn_weights"],
            scale_attn_by_inverse_layer_idx=config["scale_attn_by_inverse_layer_idx"],
            reorder_and_upcast_attn=config["reorder_and_upcast_attn"],
            add_cross_attention=config["add_cross_attention"],
            tensors=tensors,
        )

    def close(self) -> None:
        if self._tempdir is not None:
            self._tempdir.cleanup()
            self._tempdir = None

    def status(self) -> dict[str, Any]:
        with self._lock:
            status = self.swarm.status()
            status.update(
                {
                    "interactive_protocol": "mycelium.interactive_runtime.v1",
                    "run_id": self.run_id,
                    "stage_pack_digest": self.stage_pack["pack_digest"],
                    "vocabulary": list(VOCABULARY),
                    "active_request_count": len(self._active_requests),
                    "completed_request_count": len(self.records),
                    "recent_requests": [
                        self.records[request_id].public_document()
                        for request_id in self._record_order[-5:]
                    ],
                }
            )
            return status

    def get_record(self, request_id: str) -> dict[str, Any] | None:
        with self._lock:
            record = self.records.get(request_id)
            return None if record is None else record.public_document()

    def cancel_request(self, request_id: str) -> bool:
        with self._lock:
            if request_id not in self._active_requests:
                return False
            self._cancelled_requests.add(request_id)
            cancel_event = self._cancel_events[request_id]
            cancel_event.set()
            active_stage_request = self._active_requests[request_id]
        if active_stage_request is not None:
            self.swarm.cancel_request(active_stage_request)
        return True

    def infer(
        self,
        *,
        prompt: str,
        max_new_tokens: int = 1,
        required_distinct_peers: int = 1,
        timeout_seconds: float = 25.0,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        if type(max_new_tokens) is not int or not 1 <= max_new_tokens <= 8:
            _reject("max_new_tokens_invalid")
        if (
            type(required_distinct_peers) is not int
            or not 1 <= required_distinct_peers <= max_new_tokens
        ):
            _reject("required_distinct_peers_invalid")
        if not isinstance(timeout_seconds, (int, float)) or not 0 < float(timeout_seconds) <= 120:
            _reject("timeout_invalid")
        created_at = time.time()
        request_id = request_id or f"request-{uuid.uuid4()}"
        if not isinstance(request_id, str) or not request_id or len(request_id) > 128:
            _reject("request_id_invalid")
        prompt_digest = "sha256:" + hashlib.sha256(prompt.encode("utf-8")).hexdigest()
        max_context = int(self.config["n_positions"]) - max_new_tokens
        if max_context < 1:
            _reject("max_new_tokens_invalid")
        context = _prompt_tokens(prompt, max_context_tokens=max_context)
        initial_tokens = tuple(context)
        generated: list[int] = []
        token_records: list[dict[str, Any]] = []
        max_intermediate_error = 0.0
        max_logit_error = 0.0
        peer_ids: list[str] = []
        cancel_event = threading.Event()
        with self._lock:
            if request_id in self.records or request_id in self._active_requests:
                _reject("request_id_duplicate")
            self._active_requests[request_id] = None
            self._cancel_events[request_id] = cancel_event
            self._cancelled_requests.discard(request_id)
        try:
            with _cancellable_lock(self._infer_lock, cancel_event):
                for token_index in range(max_new_tokens):
                    with self._lock:
                        if request_id in self._cancelled_requests:
                            _reject("request_cancelled")
                    entry = execute_loaded_stage(
                        self.loaded[0],
                        token_ids=mx.array((tuple(context),), dtype=mx.uint32),
                    )
                    mx.eval(entry)
                    entry_hidden = entry.tolist()[0]
                    stage_request_id = f"{request_id}:token-{token_index}"
                    with self._lock:
                        if request_id in self._cancelled_requests:
                            _reject("request_cancelled")
                        self._active_requests[request_id] = stage_request_id
                    try:
                        browser_result = self.swarm.dispatch(
                            request_id=stage_request_id,
                            hidden=entry_hidden,
                            timeout_seconds=float(timeout_seconds),
                            excluded_peer_ids=(
                                set(peer_ids)
                                if len(set(peer_ids)) < required_distinct_peers
                                else None
                            ),
                            allowed_peer_ids=(
                                set(peer_ids)
                                if len(set(peer_ids)) >= required_distinct_peers
                                else None
                            ),
                            cancel_event=cancel_event,
                        )
                    except SwarmError as exc:
                        if cancel_event.is_set():
                            _reject("request_cancelled")
                        raise InteractiveRuntimeError(exc.code) from exc
                    finally:
                        with self._lock:
                            if self._active_requests.get(request_id) == stage_request_id:
                                self._active_requests[request_id] = None
                    with self._lock:
                        if request_id in self._cancelled_requests:
                            _reject("request_cancelled")
                    browser_output = [list(row) for row in browser_result.output]
                    local_output = self.pixel_stage.execute(
                        request_id=f"{stage_request_id}:local-reference",
                        assignment_id=self.stage_pack["assignment_id"],
                        stage_id=self.stage_pack["stage_id"],
                        hidden=entry_hidden,
                    )
                    intermediate_error = _max_error(browser_output, local_output)
                    max_intermediate_error = max(max_intermediate_error, intermediate_error)
                    if intermediate_error > INTERMEDIATE_TOLERANCE:
                        _reject("browser_stage_parity_failed")

                    final_logits = _final_logits(self.loaded[1], browser_output).tolist()[0]
                    expected = execute_loaded_stage(
                        self.reference,
                        token_ids=mx.array((tuple(context),), dtype=mx.uint32),
                    )
                    mx.eval(expected)
                    expected_logits = expected.tolist()[0]
                    logit_error = _max_error(final_logits, expected_logits)
                    max_logit_error = max(max_logit_error, logit_error)
                    if logit_error > LOGIT_TOLERANCE:
                        _reject("monolithic_reference_mismatch")
                    selected = _quantized_argmax(final_logits[-1])
                    generated.append(selected)
                    context.append(selected)
                    peer_ids.append(browser_result.peer_id)
                    token_records.append(
                        {
                            "token_index": token_index,
                            "stage_request_id": stage_request_id,
                            "browser_peer_id": browser_result.peer_id,
                            "browser_job_id": browser_result.job_id,
                            "browser_output_digest": browser_result.output_digest,
                            "selected_token": selected,
                            "selected_label": VOCABULARY[selected],
                            "context_length": len(context),
                            "intermediate_error": intermediate_error,
                            "logit_error": logit_error,
                            "route_ready": False,
                        }
                    )
                record = InferenceRecord(
                    protocol="mycelium.interactive_inference_record.v1",
                    request_id=request_id,
                    prompt_digest=prompt_digest,
                    prompt_bytes=len(prompt.encode("utf-8")),
                    initial_tokens=initial_tokens,
                    generated_tokens=tuple(generated),
                    generated_labels=tuple(VOCABULARY[token] for token in generated),
                    max_intermediate_error=max_intermediate_error,
                    max_logit_error=max_logit_error,
                    peer_ids=tuple(dict.fromkeys(peer_ids)),
                    required_distinct_peers=required_distinct_peers,
                    observed_distinct_peers=len(set(peer_ids)),
                    stage_pack_digest=self.stage_pack["pack_digest"],
                    token_records=tuple(token_records),
                    created_at=created_at,
                    completed_at=time.time(),
                )
                with self._lock:
                    if request_id in self._cancelled_requests:
                        _reject("request_cancelled")
                    self.records[request_id] = record
                    self._record_order.append(request_id)
                    while len(self._record_order) > self.max_records:
                        removed = self._record_order.pop(0)
                        self.records.pop(removed, None)
                    self._active_requests.pop(request_id, None)
                    self._cancel_events.pop(request_id, None)
                    self._cancelled_requests.discard(request_id)
                return record.public_document()
        finally:
            with self._lock:
                self._active_requests.pop(request_id, None)
                self._cancel_events.pop(request_id, None)
                self._cancelled_requests.discard(request_id)
