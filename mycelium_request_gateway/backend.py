"""Adapter from request sessions to the existing production Router lifecycle."""
from __future__ import annotations

import hashlib
import json
import threading
from dataclasses import dataclass
from typing import Callable, Protocol

from mycelium_qualification.contracts import RouteQualificationV1
from mycelium_qualification.evidence import sha256_document
from mycelium_router.contracts import ExecutionGraph, RequestContext
from mycelium_router.serialization import execution_graph_to_dict
from mycelium_m16_runtime import M16AdmissionError

from .contracts import AdmissionError, InferenceSubmission, qualification_binding
from .qualification import QualificationSource


class PromptCodec(Protocol):
    def encode(self, prompt: str) -> tuple[int, ...]: ...

    def decode_token(self, token_id: int) -> str: ...


class RouterPort(Protocol):
    def current_deployment(self) -> ExecutionGraph: ...

    def admit(
        self,
        request: RequestContext,
        client_sink: object,
        *,
        pinned_deployment: ExecutionGraph | None = None,
        **kwargs: object,
    ) -> str: ...

    def decode_one(self, request_id: str) -> bool: ...

    def request_status(self, request_id: str) -> str: ...

    def cancel(self, request_id: str) -> bool: ...

    def cancel_with_deadline(
        self,
        request_id: str,
        *,
        deadline_monotonic_s: float,
    ) -> bool: ...

    def update_publisher_generation(
        self,
        request_id: str,
        *,
        expected_generation: int,
        new_generation: int,
    ) -> bool: ...


@dataclass(frozen=True)
class _AdmissionDecision:
    graph: ExecutionGraph | None
    excluded_placements: frozenset[str]


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
        selected_placements: frozenset[str] | None = None,
    ) -> None:
        if not isinstance(excluded_placements, frozenset) or not all(
            isinstance(item, str) and item for item in excluded_placements
        ):
            raise ValueError("invalid_excluded_placements")
        if selected_placements is not None and (
            not isinstance(selected_placements, frozenset)
            or not selected_placements
            or not all(isinstance(item, str) and item for item in selected_placements)
        ):
            raise ValueError("invalid_selected_placements")
        if (
            selected_placements is not None
            and selected_placements & excluded_placements
        ):
            raise ValueError("selected_placements_conflict")
        if not isinstance(sampling_seed, int) or isinstance(sampling_seed, bool):
            raise ValueError("invalid_sampling_seed")
        self._router = router
        self._codec = codec
        self._clock = clock
        self._qualification_source = qualification_source
        self._excluded_placements = excluded_placements
        self._selected_placements = selected_placements
        self._sampling_seed = sampling_seed
        self._lock = threading.RLock()
        self._active: set[str] = set()
        self._cancelled: set[str] = set()
        self._pending_cancelled: set[str] = set()
        self._internally_cancelled: set[str] = set()
        self._external_cancellation_observed: set[str] = set()
        self._awaiting_cancel_ack: set[str] = set()
        self._cancellation_deadlines: dict[str, float] = {}
        self._publisher_generations: dict[str, int] = {}
        self._router_admitted: set[str] = set()

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
                self._cancellation_deadlines.pop(request_id, None)
                self._publisher_generations.pop(request_id, None)
                self._router_admitted.discard(request_id)

    def _run(
        self,
        request_id: str,
        submission: InferenceSubmission,
        emit_token: Callable[[int, str], None],
        is_cancelled: Callable[[], bool],
    ) -> str:
        if is_cancelled() or self._is_cancelled(request_id):
            return "cancelled"
        admission = self._require_current_deployment(submission)
        policy = getattr(self._codec, "policy_response", None)
        if callable(policy):
            response = policy(submission.prompt)
            if response is not None:
                if not isinstance(response, str) or not response or len(response) > 4_096:
                    raise AdmissionError("invalid_policy_response")
                emit_token(0, response)
                return "completed"
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
            qos_class=submission.qos_class or "interactive",
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
            admission_kwargs: dict[str, object] = {
                "excluded_placements": admission.excluded_placements,
            }
            with self._lock:
                admitted_publisher_generation = self._publisher_generations.get(
                    request_id,
                    1,
                )
            if getattr(
                self._router,
                "supports_publisher_generation",
                False,
            ):
                admission_kwargs["publisher_generation"] = (
                    admitted_publisher_generation
                )
            if getattr(self._router, "requires_qualification_binding", False):
                admission_kwargs["qualification_binding"] = submission.qualification
            if (
                submission.workload_profile_id is not None
                and getattr(self._router, "supports_workload_profiles", False)
            ):
                admission_kwargs["workload_profile_id"] = submission.workload_profile_id
            if admission.graph is None:
                admitted_id = self._router.admit(
                    request,
                    sink,
                    **admission_kwargs,
                )
            else:
                admitted_id = self._router.admit(
                    request,
                    sink,
                    pinned_deployment=admission.graph,
                    **admission_kwargs,
                )
            if admitted_id != request_id:
                raise AdmissionError("router_request_id_mismatch")
            admitted = True
            with self._lock:
                self._router_admitted.add(request_id)
                current_publisher_generation = self._publisher_generations.get(
                    request_id,
                    admitted_publisher_generation,
                )
            if current_publisher_generation != admitted_publisher_generation:
                update = getattr(
                    self._router,
                    "update_publisher_generation",
                    None,
                )
                if not callable(update):
                    raise AdmissionError("publisher_generation_sync_failed")
                for publisher_generation in range(
                    admitted_publisher_generation,
                    current_publisher_generation,
                ):
                    if update(
                        request_id,
                        expected_generation=publisher_generation,
                        new_generation=publisher_generation + 1,
                    ) is not True:
                        raise AdmissionError("publisher_generation_sync_failed")
            while True:
                if is_cancelled() or self._is_cancelled(request_id):
                    self._cancel_once(request_id)
                status = self._router.request_status(request_id)
                if status == "COMPLETED":
                    return "completed"
                if status == "CANCELLED":
                    return "cancelled"
                if status == "FAILED":
                    return "failed"
                if status == "TERMINAL_BLOCKED":
                    return "terminal_blocked"
                if status != "DECODING":
                    raise AdmissionError("invalid_router_state")
                progressed = self._router.decode_one(request_id)
                if not progressed and self._router.request_status(request_id) == "DECODING":
                    raise AdmissionError("router_decode_stalled")
        except M16AdmissionError as exc:
            if admitted:
                self._cancel_once(request_id)
            raise AdmissionError(exc.code) from exc
        except Exception:
            if admitted:
                self._cancel_once(request_id)
            raise

    def _require_current_deployment(
        self,
        submission: InferenceSubmission,
    ) -> _AdmissionDecision:
        source = self._qualification_source
        if source is None:
            return _AdmissionDecision(
                graph=None,
                excluded_placements=self._excluded_placements,
            )
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
        try:
            deployment_source = getattr(
                self._router,
                "current_deployment",
                None,
            )
        except Exception as exc:
            raise AdmissionError("qualification_unavailable") from exc
        if not callable(deployment_source):
            raise AdmissionError("qualification_unavailable")
        try:
            deployment = deployment_source()
        except Exception as exc:
            raise AdmissionError("qualification_unavailable") from exc
        if not isinstance(deployment, ExecutionGraph):
            raise AdmissionError("qualification_unavailable")
        try:
            deployment_digest = sha256_document(
                execution_graph_to_dict(deployment)
            )
        except Exception as exc:
            raise AdmissionError("qualification_unavailable") from exc
        live_identity = (
            deployment.deployment_id,
            deployment.deployment_epoch,
            deployment.topology_version,
            deployment.model_id,
            deployment.resolved_commit,
            deployment.manifest_digest,
        )
        qualified_identity = (
            current.deployment_id,
            current.deployment_epoch,
            current.topology_version,
            current.model_id,
            current.resolved_commit,
            current.manifest_digest,
        )
        if (
            live_identity != qualified_identity
            or deployment_digest != current.execution_graph_digest
        ):
            raise AdmissionError("qualification_mismatch")
        selected_placements = self._qualified_placement_projection(
            current,
            deployment,
        )
        live_placements = frozenset(
            placement.placement_id
            for stage in deployment.stages
            for placement in stage.placements
        )
        override = self._selected_placements
        if override is not None:
            if not override <= live_placements:
                raise AdmissionError("qualification_mismatch")
            if override & self._excluded_placements:
                raise AdmissionError("qualification_mismatch")
            selected_placements = override
        elif selected_placements & self._excluded_placements:
            raise AdmissionError("qualification_mismatch")
        return _AdmissionDecision(
            graph=deployment,
            excluded_placements=live_placements - selected_placements,
        )

    @staticmethod
    def _qualified_placement_projection(
        current: RouteQualificationV1,
        deployment: ExecutionGraph,
    ) -> frozenset[str]:
        if len(current.stage_bindings) != len(deployment.stages):
            raise AdmissionError("qualification_mismatch")
        stages = {stage.stage_id: stage for stage in deployment.stages}
        selected_by_stage: dict[str, str] = {}
        selected_placements: set[str] = set()
        for binding in current.stage_bindings:
            if (
                binding.stage_id in selected_by_stage
                or binding.placement_id in selected_placements
            ):
                raise AdmissionError("qualification_mismatch")
            stage = stages.get(binding.stage_id)
            if stage is None:
                raise AdmissionError("qualification_mismatch")
            placement = next(
                (
                    candidate
                    for candidate in stage.placements
                    if candidate.placement_id == binding.placement_id
                ),
                None,
            )
            if (
                placement is None
                or placement.lifecycle_state != "ACTIVE"
                or placement.node_id != binding.node_id
                or placement.assignment_id != binding.assignment_id
                or placement.stage_signature != binding.stage_signature
                or placement.load_proof_digest != binding.load_proof_digest
            ):
                raise AdmissionError("qualification_mismatch")
            selected_by_stage[binding.stage_id] = binding.placement_id
            selected_placements.add(binding.placement_id)
        if set(selected_by_stage) != set(stages):
            raise AdmissionError("qualification_mismatch")
        ordered = tuple(
            selected_by_stage[stage.stage_id]
            for stage in deployment.stages
        )
        legal_edges = {
            (edge.from_placement_id, edge.to_placement_id)
            for edge in deployment.edges
        }
        if any(
            pair not in legal_edges
            for pair in zip(ordered, ordered[1:])
        ):
            raise AdmissionError("qualification_mismatch")
        legal_loopbacks = {
            (edge.from_placement_id, edge.to_placement_id)
            for edge in deployment.loopback_edges
        }
        if (ordered[-1], ordered[0]) not in legal_loopbacks:
            raise AdmissionError("qualification_mismatch")
        return frozenset(selected_placements)

    def cancel(self, request_id: str) -> bool:
        return self._cancel_once(request_id, external=True)

    def cancel_with_deadline(
        self,
        request_id: str,
        *,
        deadline_monotonic_s: float,
    ) -> bool:
        """Propagate the gateway's one original cancellation deadline."""

        if (
            not isinstance(deadline_monotonic_s, (int, float))
            or isinstance(deadline_monotonic_s, bool)
        ):
            raise ValueError("invalid_cancellation_deadline")
        return self._cancel_once(
            request_id,
            external=True,
            deadline_monotonic_s=float(deadline_monotonic_s),
        )

    def release(self, request_id: str) -> None:
        release = getattr(self._router, "release_request", None)
        if callable(release):
            release(request_id)

    def update_publisher_generation(
        self,
        request_id: str,
        *,
        expected_generation: int,
        new_generation: int,
    ) -> bool:
        if (
            type(expected_generation) is not int
            or expected_generation < 0
            or type(new_generation) is not int
            or new_generation != expected_generation + 1
        ):
            return False
        with self._lock:
            current = self._publisher_generations.get(request_id, 1)
            if expected_generation == 0 and new_generation == 1 and current == 1:
                self._publisher_generations[request_id] = 1
                return True
            if current != expected_generation:
                return False
            self._publisher_generations[request_id] = new_generation
            admitted = request_id in self._router_admitted
        if not admitted:
            return True
        if not getattr(
            self._router,
            "supports_publisher_generation",
            False,
        ):
            return True
        update = getattr(self._router, "update_publisher_generation", None)
        if callable(update) and update(
            request_id,
            expected_generation=expected_generation,
            new_generation=new_generation,
        ) is True:
            return True
        try:
            status = self._router.request_status(request_id)
        except Exception:
            return False
        # A terminal/retired physical request has no remaining mutation
        # boundary to update. Replay still advances the gateway-owned
        # publisher generation and cannot revive that command.
        return status in {
            "COMPLETED",
            "CANCELLED",
            "FAILED",
            "TERMINAL_BLOCKED",
            "UNKNOWN",
        }

    def _cancel_once(
        self,
        request_id: str,
        *,
        external: bool = False,
        deadline_monotonic_s: float | None = None,
    ) -> bool:
        with self._lock:
            if deadline_monotonic_s is not None:
                existing_deadline = self._cancellation_deadlines.get(request_id)
                if (
                    existing_deadline is not None
                    and existing_deadline != deadline_monotonic_s
                ):
                    raise ValueError("cancellation_deadline_conflict")
                self._cancellation_deadlines[request_id] = deadline_monotonic_s
            if external and request_id in self._awaiting_cancel_ack:
                self._awaiting_cancel_ack.discard(request_id)
                return True
            active = request_id in self._active
            router_admitted = request_id in self._router_admitted
            if not active:
                if not external or request_id in self._pending_cancelled:
                    return True
                self._pending_cancelled.add(request_id)
            elif request_id in self._cancelled:
                if external:
                    self._external_cancellation_observed.add(request_id)
                    self._internally_cancelled.discard(request_id)
                if not router_admitted:
                    # The backend run has started, but Router admission has
                    # not published its request record yet. Keep cancellation
                    # sticky; the first post-admit polling round will route it
                    # with this same owner deadline.
                    self._pending_cancelled.add(request_id)
                    return True
                if request_id not in self._pending_cancelled:
                    return True
                self._pending_cancelled.discard(request_id)
            else:
                self._cancelled.add(request_id)
                if external:
                    self._external_cancellation_observed.add(request_id)
                else:
                    self._internally_cancelled.add(request_id)
                if not router_admitted:
                    self._pending_cancelled.add(request_id)
                    return True
            effective_deadline = self._cancellation_deadlines.get(request_id)
        cancel_with_deadline = getattr(self._router, "cancel_with_deadline", None)
        if effective_deadline is not None and callable(cancel_with_deadline):
            cancelled = cancel_with_deadline(
                request_id,
                deadline_monotonic_s=effective_deadline,
            )
        else:
            cancelled = self._router.cancel(request_id)
        return cancelled is not False

    def _is_cancelled(self, request_id: str) -> bool:
        with self._lock:
            return request_id in self._cancelled
