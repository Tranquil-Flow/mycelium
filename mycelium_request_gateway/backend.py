"""Adapter from request sessions to the existing production Router lifecycle."""
from __future__ import annotations

import hashlib
import json
import threading
from typing import Callable, Protocol

from mycelium_qualification.contracts import RouteQualificationV1
from mycelium_router.contracts import ExecutionGraph, RequestContext

from .contracts import AdmissionError, InferenceSubmission, qualification_binding
from .qualification import QualificationSource


class PromptCodec(Protocol):
    def encode(self, prompt: str) -> tuple[int, ...]: ...

    def decode_token(self, token_id: int) -> str: ...


class RouterPort(Protocol):
    def admit(self, request: RequestContext, client_sink: object, **kwargs: object) -> str: ...

    def decode_one(self, request_id: str) -> bool: ...

    def request_status(self, request_id: str) -> str: ...

    def cancel(self, request_id: str) -> bool: ...


class _GatewayTokenSink:
    def __init__(self, codec: PromptCodec, emit_token: Callable[[int, str], None]) -> None:
        self._codec = codec
        self._emit_token = emit_token

    def emit(self, token_index: int, token_id: int) -> None:
        if (
            not isinstance(token_index, int)
            or isinstance(token_index, bool)
            or token_index < 0
            or not isinstance(token_id, int)
            or isinstance(token_id, bool)
            or token_id < 0
        ):
            raise AdmissionError("invalid_router_token")
        text = self._codec.decode_token(token_id)
        if not isinstance(text, str):
            raise AdmissionError("invalid_decoded_token")
        self._emit_token(token_index, text)


class RouterSessionBackend:
    """Drive one request through Router admission, decode, and cleanup paths."""

    def __init__(
        self,
        *,
        router: RouterPort,
        codec: PromptCodec,
        clock: Callable[[], float],
        qualification_source: QualificationSource | None = None,
        excluded_placements: frozenset[str] = frozenset(),
        sampling_seed: int = 0,
    ) -> None:
        if not isinstance(excluded_placements, frozenset) or not all(
            isinstance(item, str) and item for item in excluded_placements
        ):
            raise ValueError("invalid_excluded_placements")
        if not isinstance(sampling_seed, int) or isinstance(sampling_seed, bool):
            raise ValueError("invalid_sampling_seed")
        self._router = router
        self._codec = codec
        self._clock = clock
        self._qualification_source = qualification_source
        self._excluded_placements = excluded_placements
        self._sampling_seed = sampling_seed
        self._lock = threading.RLock()
        self._active: set[str] = set()
        self._cancelled: set[str] = set()
        self._pending_cancelled: set[str] = set()
        self._internally_cancelled: set[str] = set()
        self._external_cancellation_observed: set[str] = set()
        self._awaiting_cancel_ack: set[str] = set()

    def run(
        self,
        request_id: str,
        submission: InferenceSubmission,
        emit_token: Callable[[int, str], None],
        is_cancelled: Callable[[], bool],
    ) -> str:
        with self._lock:
            if request_id in self._active:
                raise AdmissionError("duplicate_request_id")
            self._awaiting_cancel_ack.discard(request_id)
            self._external_cancellation_observed.discard(request_id)
            self._active.add(request_id)
            if request_id in self._pending_cancelled:
                self._pending_cancelled.discard(request_id)
                self._cancelled.add(request_id)
        failed = True
        try:
            outcome = self._run(request_id, submission, emit_token, is_cancelled)
            failed = False
            return outcome
        finally:
            with self._lock:
                needs_ack = failed or request_id in self._internally_cancelled
                if (
                    needs_ack
                    and request_id not in self._external_cancellation_observed
                ):
                    self._awaiting_cancel_ack.add(request_id)
                self._active.discard(request_id)
                self._cancelled.discard(request_id)
                self._internally_cancelled.discard(request_id)
                self._external_cancellation_observed.discard(request_id)

    def _run(
        self,
        request_id: str,
        submission: InferenceSubmission,
        emit_token: Callable[[int, str], None],
        is_cancelled: Callable[[], bool],
    ) -> str:
        if is_cancelled() or self._is_cancelled(request_id):
            return "cancelled"
        self._require_current_deployment(submission)
        prompt_token_ids = self._codec.encode(submission.prompt)
        if not isinstance(prompt_token_ids, tuple) or not prompt_token_ids or not all(
            isinstance(item, int) and not isinstance(item, bool) and item >= 0
            for item in prompt_token_ids
        ):
            raise AdmissionError("invalid_encoded_prompt")
        now = self._clock()
        if not isinstance(now, (int, float)) or isinstance(now, bool):
            raise AdmissionError("invalid_router_clock")
        config_document = {
            "max_new_tokens": submission.max_new_tokens,
            "sampling_seed": self._sampling_seed,
        }
        config_digest = "sha256:" + hashlib.sha256(
            json.dumps(
                config_document,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        request = RequestContext(
            request_id=request_id,
            prompt_token_ids=prompt_token_ids,
            max_new_tokens=submission.max_new_tokens,
            expected_new_tokens=submission.max_new_tokens,
            qos_class="interactive",
            admitted_at=float(now),
            target_ttft_ms=1_000.0,
            target_tpot_ms=100.0,
            target_tokens_per_second=10.0,
            sampling_seed=self._sampling_seed,
            generation_config_digest=config_digest,
        )
        sink = _GatewayTokenSink(self._codec, emit_token)
        admitted = False
        try:
            if is_cancelled() or self._is_cancelled(request_id):
                return "cancelled"
            admitted_id = self._router.admit(
                request,
                sink,
                excluded_placements=self._excluded_placements,
            )
            if admitted_id != request_id:
                raise AdmissionError("router_request_id_mismatch")
            admitted = True
            while True:
                if is_cancelled() or self._is_cancelled(request_id):
                    self._cancel_once(request_id)
                    return "cancelled"
                status = self._router.request_status(request_id)
                if status == "COMPLETED":
                    return "completed"
                if status == "CANCELLED":
                    return "cancelled"
                if status == "FAILED":
                    return "failed"
                if status != "DECODING":
                    raise AdmissionError("invalid_router_state")
                progressed = self._router.decode_one(request_id)
                if not progressed and self._router.request_status(request_id) == "DECODING":
                    raise AdmissionError("router_decode_stalled")
        except Exception:
            if admitted:
                self._cancel_once(request_id)
            raise

    def _require_current_deployment(self, submission: InferenceSubmission) -> None:
        source = self._qualification_source
        if source is None:
            return
        try:
            current = source.current()
        except Exception as exc:
            raise AdmissionError("qualification_unavailable") from exc
        if current is None:
            raise AdmissionError("route_dropped")
        if not isinstance(current, RouteQualificationV1):
            raise AdmissionError("qualification_unavailable")
        if current.route_ready is not True:
            raise AdmissionError("readiness_revoked")
        try:
            current_binding = qualification_binding(current)
        except Exception as exc:
            raise AdmissionError("qualification_unavailable") from exc
        if current_binding != submission.qualification:
            raise AdmissionError("qualification_mismatch")
        deployment_source = getattr(self._router, "current_deployment", None)
        if deployment_source is None:
            return
        if not callable(deployment_source):
            raise AdmissionError("qualification_unavailable")
        try:
            deployment = deployment_source()
        except Exception as exc:
            raise AdmissionError("qualification_unavailable") from exc
        if not isinstance(deployment, ExecutionGraph):
            raise AdmissionError("qualification_unavailable")
        if (
            deployment.deployment_id != current.deployment_id
            or deployment.deployment_epoch != current.deployment_epoch
            or deployment.topology_version != current.topology_version
            or deployment.model_id != current.model_id
            or deployment.resolved_commit != current.resolved_commit
        ):
            raise AdmissionError("qualification_mismatch")

    def cancel(self, request_id: str) -> None:
        self._cancel_once(request_id, external=True)

    def _cancel_once(self, request_id: str, *, external: bool = False) -> None:
        with self._lock:
            if external and request_id in self._awaiting_cancel_ack:
                self._awaiting_cancel_ack.discard(request_id)
                return
            if request_id not in self._active:
                if not external or request_id in self._pending_cancelled:
                    return
                self._pending_cancelled.add(request_id)
            elif request_id in self._cancelled:
                if external:
                    self._external_cancellation_observed.add(request_id)
                    self._internally_cancelled.discard(request_id)
                return
            else:
                self._cancelled.add(request_id)
                if external:
                    self._external_cancellation_observed.add(request_id)
                else:
                    self._internally_cancelled.add(request_id)
        self._router.cancel(request_id)

    def _is_cancelled(self, request_id: str) -> bool:
        with self._lock:
            return request_id in self._cancelled
