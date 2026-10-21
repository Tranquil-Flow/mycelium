# SPDX-License-Identifier: AGPL-3.0-only
"""Production request-session service shared by HTTP API and CLI clients."""
from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor, TimeoutError as FutureTimeout
from dataclasses import dataclass, field
from dataclasses import replace
import hashlib
import hmac
import logging
import os
from pathlib import Path
import threading
import time
from typing import TYPE_CHECKING, Callable, Mapping, Protocol

from .contracts import (
    AdmissionError,
    InferenceSubmission,
    REQUEST_EVENT_PROTOCOL,
    REQUEST_EVENT_PROTOCOL_V2,
    TokenEmitter,
    REQUEST_GATEWAY_PROTOCOL_V2,
    StreamEvent,
    is_safe_error_code,
    is_valid_request_id,
    safe_qualification_projection,
)
from .observability import GatewayMetrics
from .qualification import CapturedQualification, QualificationGate, QualificationSource

import mycelium_deadlines

if TYPE_CHECKING:
    from mycelium_a6_recovery import ReplayCheckpoint


_MAX_BACKEND_TOKEN_TEXT_BYTES = 1 << 20
# Bound to the deadline registry (packet wp5-bind-residual): the cancellation +
# cleanup budget has one owner, so the gateway stamp cannot drift from the
# route's fanout window derivation (cancel_and_cleanup_budget_seconds).
_MAX_CANCELLATION_AND_CLEANUP_SECONDS = mycelium_deadlines.CANCEL_AND_CLEANUP_BUDGET_SECONDS
_LOGGER = logging.getLogger(__name__)


class InferenceBackend(Protocol):
    """Production session seam; implementations own Router/KV cleanup.

    ``cancel`` must be idempotent and sticky: if it wins before ``run`` enters,
    a later ``run`` for that request must return ``cancelled`` before acquiring
    runtime, capacity, or KV resources. The production Router backend enforces
    this request-ID latch.
    """

    def run(
        self,
        request_id: str,
        submission: InferenceSubmission,
        emit_token: TokenEmitter,
        is_cancelled: Callable[[], bool],
    ) -> str: ...

    def cancel(self, request_id: str) -> bool | None: ...


@dataclass(slots=True)
class _Session:
    request_id: str
    submission: InferenceSubmission | None
    captured: CapturedQualification | None
    max_new_tokens: int
    capacity: int
    stop: threading.Event = field(default_factory=threading.Event)
    condition: threading.Condition = field(
        default_factory=lambda: threading.Condition(threading.RLock())
    )
    events: list[StreamEvent] = field(default_factory=list)
    latest_sequence: int = -1
    discarded_through: int = -1
    acknowledged_through: int = -1
    expected_token_index: int = 0
    token_digests: list[bytes] = field(default_factory=list)
    # Committed token count captured when the worker releases its token
    # material; ``token_digests`` is cleared then but observers still read it.
    released_committed_count: int | None = None
    encoded_prompt_token_ids: tuple[int, ...] | None = None
    committed_token_ids: list[int | None] = field(default_factory=list)
    terminal_event: StreamEvent | None = None
    terminal_count: int = 0
    blocked_identity: tuple[str, int, str, str] | None = None
    terminal_provenance: tuple[str, int, str, str] | None = None
    # Router command identity bound by the backend before release; the
    # gateway's terminal receipt below is the authoritative attempt.
    terminal_command_identity: tuple[str, int, str, str] | None = None
    terminal_attempt: tuple[str, int, str, str] | None = None
    maximum_buffered: int = 0
    active_subscription: bool = False
    cancellation_started: bool = False
    cancellation_started_at: float | None = None
    # Gateway clock reading at which the backend's cancellation proof was already
    # resolved when the backend stopped; later release bookkeeping is not charged.
    cancellation_resolved_at: float | None = None
    cancellation_deadline_at: float | None = None
    cancellation_wall_deadline_at: float | None = None
    cancellation_within_bound: bool | None = None
    backend_started: bool = False
    backend_cancelled: bool = False
    backend_cancellation_complete: bool = False
    backend_cancellation_failed: bool = False
    backend_cancellation_accepted: bool | None = None
    backend_released: bool = False
    cleanup_unproven: bool = False
    worker_done: bool = False
    future: Future[None] | None = None
    outcome: str | None = None
    event_protocol: str = REQUEST_EVENT_PROTOCOL
    emitted_phases: set[str] = field(default_factory=set)
    owner_token_digest: bytes | None = None
    publisher_generation: int = 0
    created_at: float = field(default_factory=time.monotonic)
    last_event_at: float | None = None
    ever_subscribed: bool = False
    last_ack_at: float | None = None
    replay_checkpoint_store_identity: str | None = None


class _GatewayEmission:
    """Private backend→gateway channel for exact A6 replay material.

    It remains callable with the frozen ``TokenEmitter`` shape
    ``(token_index, token_text, token_id=None)`` -- the optional third id stays part of the
    protocol, positionally and by keyword, so a conforming backend is never narrowed by the
    widening of this emitter into an object (``contracts.py``, ``TokenEmitter``).  Only the
    production Router backend uses the two private methods that retain canonical token IDs
    atomically with the corresponding gateway delivery event.
    """

    __slots__ = ("_service", "_session")

    def __init__(self, service: "RequestGatewayService", session: _Session) -> None:
        self._service = service
        self._session = session

    def __call__(
        self,
        token_index: int,
        token_text: str,
        token_id: int | None = None,
    ) -> None:
        self._service._accept_token(
            self._session,
            token_index,
            token_text,
            token_id=token_id,
        )

    def bind_encoded_prompt(self, token_ids: tuple[int, ...]) -> None:
        self._service._bind_encoded_prompt(self._session, token_ids)

    def emit_router_token(
        self,
        token_index: int,
        token_id: int,
        token_text: str,
    ) -> None:
        self._service._accept_token(
            self._session,
            token_index,
            token_text,
            token_id=token_id,
        )

    def bind_terminal_blocked_identity(self, identity: object) -> None:
        """Private, bounded snapshot from this run before backend release."""
        if (not isinstance(identity, tuple) or len(identity) != 4
                or identity[0] != self._session.request_id
                or type(identity[1]) is not int or identity[1] < 1
                or not all(isinstance(item, str) and item for item in identity[2:])):
            return
        with self._session.condition:
            if self._session.terminal_event is None and self._session.blocked_identity is None:
                self._session.blocked_identity = identity

    def bind_terminal_command_identity(self, identity: object) -> None:
        """Private snapshot of the command that owns this run's terminal."""
        if (not isinstance(identity, tuple) or len(identity) != 4
                or identity[0] != self._session.request_id
                or type(identity[1]) is not int or identity[1] < 1
                or not all(isinstance(item, str) and item for item in identity[2:])):
            return
        with self._session.condition:
            if self._session.terminal_event is None:
                self._session.terminal_command_identity = identity

    def freeze_replay_checkpoint(
        self,
        *,
        store_identity: str,
        session_id: str,
        source_attempt: int,
        path_id: str,
        path_attempt: int,
        tokenizer_digest: str,
        created_at_unix_ms: int,
        expiry_unix_ms: int,
        path_digest: str | None = None,
        recovery_attempt: int = 0,
        reconciliation_state: str = "frozen",
    ) -> ReplayCheckpoint:
        if self._service._replay_checkpoint_root is None:
            raise AdmissionError("replay_store_unavailable")
        return self._service.freeze_replay_checkpoint(
            self._session.request_id,
            store_identity=store_identity,
            session_id=session_id,
            source_attempt=source_attempt,
            path_id=path_id,
            path_attempt=path_attempt,
            tokenizer_digest=tokenizer_digest,
            created_at_unix_ms=created_at_unix_ms,
            expiry_unix_ms=expiry_unix_ms,
            path_digest=path_digest,
            recovery_attempt=recovery_attempt,
            reconciliation_state=reconciliation_state,
        )


class EventSubscription:
    """Single-consumer acknowledged stream; close never cancels inference."""

    def __init__(
        self,
        service: "RequestGatewayService",
        session: _Session,
        cursor: int,
        publisher_generation: int,
    ) -> None:
        self._service = service
        self._session = session
        self._cursor = cursor
        self.publisher_generation = publisher_generation
        self._delivered = cursor
        self._closed = False

    @property
    def closed(self) -> bool:
        return self._closed

    def next_event(self, *, timeout: float | None = None) -> StreamEvent | None:
        if self._closed:
            raise AdmissionError("stream_closed")
        event = self._service._next_event(self._session, self._cursor, timeout)
        if event is not None:
            event = replace(event, publisher_generation=self.publisher_generation)
            self._delivered = event.sequence
        return event

    def ack(self, sequence: int) -> None:
        if self._closed:
            raise AdmissionError("stream_closed")
        self._service._ack(self._session, self, sequence)
        with self._session.condition:
            self._session.last_ack_at = self._service._clock()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._service._close_subscription(self._session)

    def __enter__(self) -> "EventSubscription":
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close()


class RequestGatewayService:
    """Own exact admission, bounded replay, and one backend lifecycle per request."""

    # Bounded grace period before a stuck non-terminal session is eligible
    # for eviction by a fresh admission. The grace covers the submit→
    # subscribe window during which a legitimate fresh request has no
    # subscription yet; before the grace, a new submission is rejected
    # with gateway_capacity_exhausted rather than cancelling an in-flight
    # request that is still expected to subscribe. After the grace, a
    # genuinely stuck session is evicted normally. See spec A4 §5 and
    # the `test_fresh_request_is_protected_from_immediate_eviction` gate.
    # Bound to the deadline registry (packet wp5-bind-residual).
    SESSION_EVICTION_GRACE_SECONDS = mycelium_deadlines.GATEWAY_SESSION_EVICTION_GRACE_SECONDS

    # Bounded budget a buffer-blocked producer grants an attached but
    # non-acknowledging consumer before the slot is reclaimed (spec A4 §5).
    # The consumer is observably stalled only when the event buffer is FULL
    # and no acknowledgement has arrived for longer than this budget; a
    # healthy consumer acks continuously while draining, so the budget is
    # never consulted on the ordinary path. It is a backstop, not a timer:
    # the decision point is the full-buffer block in _accept_token.
    # Bound to the deadline registry (packet wp5-bind-residual).
    SESSION_SILENT_SUBSCRIBER_STALL_SECONDS = mycelium_deadlines.GATEWAY_SESSION_SILENT_SUBSCRIBER_STALL_SECONDS

    def __init__(
        self,
        *,
        qualification_source: QualificationSource,
        backend: InferenceBackend,
        request_id_source: Callable[[], str],
        max_buffered_events: int = 64,
        max_sessions: int = 1_024,
        max_concurrent_requests: int = 4,
        max_pending_requests: int = 64,
        clock: Callable[[], float] = time.monotonic,
        metrics: GatewayMetrics | None = None,
        replay_checkpoint_root: str | os.PathLike[str] | None = None,
    ) -> None:
        if (
            not isinstance(max_buffered_events, int)
            or isinstance(max_buffered_events, bool)
            or not 2 <= max_buffered_events <= 65_536
        ):
            raise ValueError("invalid_max_buffered_events")
        if (
            not isinstance(max_sessions, int)
            or isinstance(max_sessions, bool)
            or not 1 <= max_sessions <= 65_536
        ):
            raise ValueError("invalid_max_sessions")
        if (
            not isinstance(max_concurrent_requests, int)
            or isinstance(max_concurrent_requests, bool)
            or not 1 <= max_concurrent_requests <= 64
        ):
            raise ValueError("invalid_max_concurrent_requests")
        if (
            not isinstance(max_pending_requests, int)
            or isinstance(max_pending_requests, bool)
            or not max_concurrent_requests <= max_pending_requests <= 65_536
        ):
            raise ValueError("invalid_max_pending_requests")
        self._gate = QualificationGate(qualification_source)
        self._backend = backend
        self._request_id_source = request_id_source
        self._max_buffered_events = max_buffered_events
        self._max_sessions = max_sessions
        self._max_concurrent_requests = max_concurrent_requests
        self._max_pending_requests = max_pending_requests
        if not callable(clock):
            raise ValueError("invalid_gateway_clock")
        self._clock = clock
        self._admission_slots = threading.BoundedSemaphore(max_pending_requests)
        self._executor = ThreadPoolExecutor(
            max_workers=max_concurrent_requests,
            thread_name_prefix="mycelium-request-worker",
        )
        self._metrics = metrics if metrics is not None else GatewayMetrics()
        self._replay_checkpoint_root = None
        if replay_checkpoint_root is not None:
            checkpoint_root = Path(replay_checkpoint_root)
            checkpoint_root.mkdir(mode=0o700, parents=True, exist_ok=True)
            if checkpoint_root.is_symlink() or not checkpoint_root.is_dir():
                raise ValueError("invalid_replay_checkpoint_root")
            os.chmod(checkpoint_root, 0o700)
            self._replay_checkpoint_root = checkpoint_root
        self._startup_recovery_reconciliations: tuple[dict[str, str], ...] = ()
        if self._replay_checkpoint_root is not None:
            from mycelium_a6_recovery import CheckpointStore

            # Crash leftovers hold the encoded prompt in the clear: sweep the temporaries, then reconcile and
            # discard the orphaned records (journaled by digest only).
            CheckpointStore.sweep_temporaries(self._replay_checkpoint_root)
            self._startup_recovery_reconciliations = (
                CheckpointStore.reconcile_orphans(self._replay_checkpoint_root)
            )
        self._sessions: dict[str, _Session] = {}
        self._lock = threading.RLock()
        self._closed = False

    def health(self) -> dict[str, str]:
        return {"service": "mycelium-request-gateway", "status": "ok"}

    def startup_recovery_reconciliations(self) -> tuple[dict[str, str], ...]:
        return tuple(dict(item) for item in self._startup_recovery_reconciliations)

    def current_qualification(self) -> dict[str, object]:
        return safe_qualification_projection(self._gate.current_projection_source())

    def recovery_public_projection(self) -> dict[str, object]:
        source = getattr(self._backend, "recovery_public_projection", None)
        if not callable(source):
            raise AdmissionError("recovery_projection_unavailable")
        with self._lock:
            sessions = tuple(self._sessions.items())
        committed_counts: dict[str, int] = {}
        for request_id, session in sessions:
            with session.condition:
                committed_counts[request_id] = session.expected_token_index
        document = source(committed_counts)
        if not isinstance(document, Mapping):
            raise AdmissionError("recovery_projection_invalid")
        try:
            from mycelium_a6_recovery import _digest, validate_recovery_runtime

            projected = validate_recovery_runtime(document)
            requests = list(projected["requests"])
            if len(requests) + len(self._startup_recovery_reconciliations) > 4_096:
                raise ValueError("recovery_projection_too_large")
            existing_ids = {str(item["request_id"]) for item in requests}
            next_index = 1
            for reconciliation in self._startup_recovery_reconciliations:
                request_id = f"restart-reconciliation-{next_index}"
                while request_id in existing_ids:
                    next_index += 1
                    request_id = f"restart-reconciliation-{next_index}"
                next_index += 1
                existing_ids.add(request_id)
                requests.append(
                    {
                        "request_id": request_id,
                        "attempt": 1,
                        "path_id": "withheld",
                        "path_attempt": 0,
                        "phase": "aborted",
                        "committed_token_count": 0,
                        "recovery_attempt": 0,
                        "terminal": None,
                        "cleanup_complete": True,
                        "reconciled": reconciliation["outcome"],
                    }
                )
            body = {
                "protocol": projected["protocol"],
                "binding": projected["binding"],
                "requests": requests,
                "breaker": projected["breaker"],
            }
            body["runtime_digest"] = _digest(body)
            return validate_recovery_runtime(body)
        except (KeyError, TypeError, ValueError) as exc:
            raise AdmissionError("recovery_projection_invalid") from exc

    def submit(
        self,
        submission: InferenceSubmission,
        *,
        owner_token: str | None = None,
    ) -> str:
        if not isinstance(submission, InferenceSubmission):
            raise AdmissionError("invalid_submission")
        if owner_token is not None and (
            not isinstance(owner_token, str)
            or not 32 <= len(owner_token.encode("utf-8")) <= 256
        ):
            raise AdmissionError("invalid_session_token")
        try:
            captured = self._gate.capture(submission.qualification)
        except AdmissionError:
            self._metrics.increment("admission_rejected_total")
            raise
        if not self._admission_slots.acquire(blocking=False):
            self._metrics.increment("admission_rejected_total")
            raise AdmissionError("gateway_capacity_exhausted")
        slot_owned = True
        with self._lock:
            try:
                if self._closed:
                    raise AdmissionError("gateway_closed")
                self._make_session_room_locked()
                request_id = self._request_id_source()
                if not is_valid_request_id(request_id):
                    raise AdmissionError("invalid_request_id")
                if request_id in self._sessions:
                    raise AdmissionError("duplicate_request_id")
                session = _Session(
                    request_id=request_id,
                    submission=submission,
                    captured=captured,
                    max_new_tokens=submission.max_new_tokens,
                    capacity=self._max_buffered_events,
                    event_protocol=(
                        REQUEST_EVENT_PROTOCOL_V2
                        if submission.protocol == REQUEST_GATEWAY_PROTOCOL_V2
                        else REQUEST_EVENT_PROTOCOL
                    ),
                    owner_token_digest=(
                        None
                        if owner_token is None
                        else hashlib.sha256(owner_token.encode("utf-8")).digest()
                    ),
                )
                with session.condition:
                    self._append_event_locked(
                        session,
                        StreamEvent(
                            request_id=request_id,
                            sequence=0,
                            kind="accepted",
                            protocol=session.event_protocol,
                        ),
                    )
                self._sessions[request_id] = session
                session.future = self._executor.submit(self._run, session)
                slot_owned = False
                self._metrics.increment("requests_admitted_total")
            finally:
                if slot_owned:
                    self._admission_slots.release()
        return request_id

    def subscribe(
        self,
        request_id: str,
        *,
        last_event_id: int | tuple[int, int] | None,
        owner_token: str | None = None,
    ) -> EventSubscription:
        session = self._get_session(request_id)
        self._require_session_owner(session, owner_token)
        if isinstance(last_event_id, tuple):
            if len(last_event_id) != 2:
                raise AdmissionError("invalid_last_event_id")
            previous_publisher_generation, cursor = last_event_id
        else:
            previous_publisher_generation = session.publisher_generation
            cursor = -1 if last_event_id is None else last_event_id
        if (
            not isinstance(cursor, int)
            or isinstance(cursor, bool)
            or cursor < -1
            or not isinstance(previous_publisher_generation, int)
            or isinstance(previous_publisher_generation, bool)
            or previous_publisher_generation < 0
        ):
            raise AdmissionError("invalid_last_event_id")
        with session.condition:
            if (
                last_event_id is not None
                and previous_publisher_generation != session.publisher_generation
            ):
                raise AdmissionError("stale_publisher_generation")
            if session.active_subscription:
                raise AdmissionError("stream_already_attached")
            if cursor < session.discarded_through:
                raise AdmissionError("resume_cursor_expired")
            if cursor > session.latest_sequence:
                raise AdmissionError("invalid_last_event_id")
            self._discard_through_locked(session, cursor)
            session.active_subscription = True
            session.ever_subscribed = True
            session.last_ack_at = self._clock()
            expected_generation = session.publisher_generation
            session.publisher_generation += 1
            subscription = EventSubscription(
                self,
                session,
                cursor,
                session.publisher_generation,
            )
        update = getattr(self._backend, "update_publisher_generation", None)
        if callable(update):
            try:
                synchronized = update(
                    request_id,
                    expected_generation=expected_generation,
                    new_generation=subscription.publisher_generation,
                )
            except Exception as exc:
                synchronized = False
                sync_error = exc
            else:
                sync_error = None
            if synchronized is not True:
                with session.condition:
                    session.active_subscription = False
                    session.condition.notify_all()
                self._cancel_session(session)
                raise AdmissionError("publisher_generation_sync_failed") from sync_error
        return subscription

    def cancel(self, request_id: str, *, owner_token: str | None = None) -> bool:
        session = self._get_session(request_id)
        self._require_session_owner(session, owner_token)
        return self._cancel_session(session)

    def _cancel_session(self, session: _Session) -> bool:
        with session.condition:
            if session.terminal_event is not None or session.cancellation_started:
                return False
            session.cancellation_started = True
            session.cancellation_started_at = self._clock()
            session.cancellation_deadline_at = (
                session.cancellation_started_at
                + _MAX_CANCELLATION_AND_CLEANUP_SECONDS
            )
            session.cancellation_wall_deadline_at = (
                time.monotonic() + _MAX_CANCELLATION_AND_CLEANUP_SECONDS
            )
            backend_started = session.backend_started
        backend_accepted: bool | None = None
        if backend_started:
            # Install the owner-issued absolute deadline in the backend before
            # exposing the stop latch to its worker.  Otherwise the worker can
            # observe cancellation first and accidentally mint a later budget.
            backend_accepted = self._cancel_backend_once(session)
        if backend_accepted is False:
            # The backend already committed another terminal state before the
            # cancellation command reached its own linearization point. Undo
            # the provisional gateway latch so that worker-owned terminal can
            # publish truthfully, and report that cancellation did not win.
            with session.condition:
                if session.terminal_event is None:
                    session.cancellation_started = False
                    session.cancellation_started_at = None
                    session.cancellation_resolved_at = None
                    session.cancellation_deadline_at = None
                    session.cancellation_wall_deadline_at = None
                session.condition.notify_all()
            return False
        with session.condition:
            session.stop.set()
            session.condition.notify_all()
        return True

    def buffered_event_count(self, request_id: str) -> int:
        session = self._get_session(request_id)
        with session.condition:
            return len(session.events)

    def maximum_observed_buffered_events(self, request_id: str) -> int:
        session = self._get_session(request_id)
        with session.condition:
            return session.maximum_buffered

    def terminal_event_count(self, request_id: str) -> int:
        session = self._get_session(request_id)
        with session.condition:
            return session.terminal_count

    def cleanup_notice_provenance(self, request_id: str) -> tuple[str, int, str, str] | None:
        """Gateway publication receipt, not part of public StreamEvent v1/v2."""
        session = self._get_session(request_id)
        with session.condition:
            return session.terminal_provenance

    def terminal_attempt_receipt(self, request_id: str) -> tuple[str, int, str, str] | None:
        """(request, attempt, path digest, command) the published terminal belongs to.

        Written once, atomically with the terminal event, from the command
        identity the backend bound before release; never a later router lookup.
        """
        session = self._get_session(request_id)
        with session.condition:
            return session.terminal_attempt

    def freeze_replay_checkpoint(
        self,
        request_id: str,
        *,
        store_identity: str,
        session_id: str,
        source_attempt: int,
        path_id: str,
        path_attempt: int,
        tokenizer_digest: str,
        created_at_unix_ms: int,
        expiry_unix_ms: int,
        path_digest: str | None = None,
        recovery_attempt: int = 0,
        reconciliation_state: str = "frozen",
    ) -> ReplayCheckpoint:
        """Freeze the gateway-owned committed delivery state into an A6 replay
        checkpoint (A6 spec section 4).  The gateway is the sole authority for
        the canonical encoded prompt and the committed generated-token prefix;
        session, attempt, path, and model authority are exact caller-supplied
        bindings, never derived or defaulted here."""
        from mycelium_a6_gateway import (
            CommittedDeliverySnapshot,
            freeze_replay_checkpoint as freeze_checkpoint,
        )

        session = self._get_session(request_id)
        with session.condition:
            submission = session.submission
            captured = session.captured
            if session.terminal_event is not None:
                raise AdmissionError("request_already_terminal")
            if submission is None or captured is None:
                raise AdmissionError("request_state_released")
            encoded_prompt_token_ids = session.encoded_prompt_token_ids
            if (
                encoded_prompt_token_ids is None
                or len(session.committed_token_ids) != len(session.token_digests)
                or any(token_id is None for token_id in session.committed_token_ids)
            ):
                raise AdmissionError("replay_material_unavailable")
            snapshot = CommittedDeliverySnapshot(
                request_id=session.request_id,
                encoded_prompt_token_ids=encoded_prompt_token_ids,
                generation_settings={
                    "protocol": submission.protocol,
                    "max_new_tokens": submission.max_new_tokens,
                    "workload_profile_id": submission.workload_profile_id,
                    "qos_class": submission.qos_class,
                },
                committed_token_ids=tuple(
                    token_id
                    for token_id in session.committed_token_ids
                    if token_id is not None
                ),
                delivery_cursor=session.acknowledged_through,
                publisher_generation=session.publisher_generation,
                session_id=session_id,
                source_attempt=source_attempt,
                path_id=path_id,
                path_digest=(
                    captured.binding.path_manifest_digest
                    if path_digest is None
                    else path_digest
                ),
                path_attempt=path_attempt,
                deployment_id=captured.binding.deployment_id,
                deployment_epoch=captured.binding.deployment_epoch,
                topology_generation=captured.binding.topology_version,
                source_qualification_digest=captured.binding.qualification_digest,
                model_id=captured.binding.model_id,
                representation_digest=captured.binding.manifest_digest,
                tokenizer_digest=tokenizer_digest,
                recovery_attempt=recovery_attempt,
            )
        private_store_identity = "a6-request-" + hashlib.sha256(
            request_id.encode("utf-8")
        ).hexdigest()
        checkpoint = freeze_checkpoint(
            snapshot,
            store_identity=private_store_identity,
            created_at_unix_ms=created_at_unix_ms,
            expiry_unix_ms=expiry_unix_ms,
            reconciliation_state=reconciliation_state,
        )
        root = self._replay_checkpoint_root
        if root is not None:
            from mycelium_a6_recovery import CheckpointStore

            store = CheckpointStore(root, store_identity=private_store_identity)
            store.write(checkpoint)
            with session.condition:
                if session.terminal_event is not None:
                    store.discard()
                    raise AdmissionError("request_already_terminal")
                existing_store = session.replay_checkpoint_store_identity
                if existing_store not in (None, private_store_identity):
                    store.discard()
                    raise AdmissionError("replay_checkpoint_store_changed")
                session.replay_checkpoint_store_identity = private_store_identity
        return checkpoint

    def metrics_snapshot(self) -> dict[str, int]:
        return self._metrics.snapshot()

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            sessions = tuple(self._sessions.values())
        for session in sessions:
            with session.condition:
                terminal = session.terminal_event is not None
            if not terminal:
                self._cancel_session(session)
        deadline = time.monotonic() + _MAX_CANCELLATION_AND_CLEANUP_SECONDS
        for session in sessions:
            future = session.future
            if future is None:
                continue
            try:
                future.result(timeout=max(0.0, deadline - time.monotonic()))
            except (FutureTimeout, Exception):
                continue
        self._executor.shutdown(wait=False, cancel_futures=True)

    def _run(self, session: _Session) -> None:
        try:
            captured = session.captured
            submission = session.submission
            if captured is None or submission is None:
                raise AdmissionError("request_state_released")
            self._gate.revalidate(captured, request_id=session.request_id)
            self._append_lifecycle(session, "admission")
            with session.condition:
                if session.cancellation_started:
                    session.outcome = "cancelled"
                    cancellation_started = True
                else:
                    # Logical backend-start linearization point. Cancellation
                    # and start race only while holding this same condition.
                    session.backend_started = True
                    cancellation_started = False
            if cancellation_started:
                self._release_backend_once(session)
                self._append_terminal(session, "cancelled")
                return
            self._append_lifecycle(session, "queue")
            self._append_lifecycle(session, "prefill")
            outcome = self._backend.run(
                session.request_id,
                submission,
                _GatewayEmission(self, session),
                session.stop.is_set,
            )
            self._stamp_cancellation_resolved(session)
            self._release_backend_once(session)
            if outcome == "completed":
                cancellation_resolution = self._await_backend_cancellation(session)
                if cancellation_resolution == "accepted":
                    self._append_cancellation_terminal(session)
                elif cancellation_resolution == "unproven":
                    self._append_cancellation_failure(session)
                else:
                    captured = session.captured
                    if captured is None:
                        raise AdmissionError("request_state_released")
                    self._gate.revalidate(captured, request_id=session.request_id)
                    self._append_terminal(session, "completed")
            elif outcome == "cancelled":
                cancellation_resolution = self._await_backend_cancellation(session)
                if cancellation_resolution == "unproven":
                    self._append_cancellation_failure(session)
                else:
                    self._append_cancellation_terminal(session)
            elif outcome == "terminal_blocked":
                self._append_cleanup_unproven_terminal(session)
            else:
                self._append_terminal(
                    session,
                    "failed",
                    code=self._backend_failure_code(session.request_id),
                )
            with session.condition:
                session.outcome = outcome
        except AdmissionError as exc:
            if exc.code == "request_cancelled" and session.stop.is_set():
                self._stamp_cancellation_resolved(session)
                self._release_backend_once(session)
                self._append_cancellation_terminal(session)
            else:
                session.stop.set()
                self._cancel_backend_once(session)
                self._release_backend_once(session)
                self._append_terminal(session, "failed", code=exc.code)
            with session.condition:
                session.outcome = exc.code
        except Exception:
            session.stop.set()
            self._cancel_backend_once(session)
            self._release_backend_once(session)
            self._append_terminal(session, "failed", code="backend_failed")
            with session.condition:
                session.outcome = "backend_failed"
        finally:
            self._release_backend_once(session)
            with session.condition:
                checkpoint_store_identity = session.replay_checkpoint_store_identity
                discard_checkpoint = session.terminal_event is not None
                session.replay_checkpoint_store_identity = None
                session.submission = None
                session.captured = None
                session.released_committed_count = len(session.token_digests)
                session.token_digests.clear()
                session.encoded_prompt_token_ids = None
                session.committed_token_ids.clear()
                session.worker_done = True
                session.condition.notify_all()
            if (
                discard_checkpoint
                and checkpoint_store_identity is not None
                and self._replay_checkpoint_root is not None
            ):
                from mycelium_a6_recovery import CheckpointStore

                CheckpointStore(
                    self._replay_checkpoint_root,
                    store_identity=checkpoint_store_identity,
                ).discard()
            self._admission_slots.release()

    def _bind_encoded_prompt(
        self,
        session: _Session,
        token_ids: tuple[int, ...],
    ) -> None:
        if (
            not isinstance(token_ids, tuple)
            or not token_ids
            or not all(type(token_id) is int and token_id >= 0 for token_id in token_ids)
        ):
            raise AdmissionError("invalid_encoded_prompt")
        with session.condition:
            if session.stop.is_set() or session.terminal_event is not None:
                raise AdmissionError("request_cancelled")
            if session.expected_token_index != 0:
                raise AdmissionError("encoded_prompt_bound_after_decode")
            if session.encoded_prompt_token_ids is None:
                session.encoded_prompt_token_ids = token_ids
            elif session.encoded_prompt_token_ids != token_ids:
                raise AdmissionError("encoded_prompt_mismatch")

    def _accept_token(
        self,
        session: _Session,
        token_index: int,
        token_text: str,
        *,
        token_id: int | None = None,
    ) -> None:
        if (
            not isinstance(token_index, int)
            or isinstance(token_index, bool)
            or token_index < 0
            or not isinstance(token_text, str)
            or len(token_text) > _MAX_BACKEND_TOKEN_TEXT_BYTES
            or (
                token_id is not None
                and (type(token_id) is not int or token_id < 0)
            )
        ):
            raise AdmissionError("invalid_backend_token")
        try:
            token_bytes = token_text.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise AdmissionError("invalid_backend_token") from exc
        if len(token_bytes) > _MAX_BACKEND_TOKEN_TEXT_BYTES:
            raise AdmissionError("invalid_backend_token")
        # Equal decoded text need not mean the same token. Bind replay to both.
        token_digest = hashlib.sha256(
            (str(token_id) + ":").encode("ascii") + token_bytes
        ).digest()
        del token_bytes

        if token_index == 0:
            self._append_lifecycle(session, "first_token")
            self._append_lifecycle(session, "decode")

        while True:
            captured = session.captured
            if captured is None:
                raise AdmissionError("request_state_released")
            self._gate.revalidate(captured, request_id=session.request_id)
            with session.condition:
                if session.stop.is_set() or session.terminal_event is not None:
                    raise AdmissionError("request_cancelled")
                if token_index < session.expected_token_index:
                    if (
                        token_index >= len(session.token_digests)
                        or session.token_digests[token_index] != token_digest
                        or (
                            token_id is not None
                            and (
                                token_index >= len(session.committed_token_ids)
                                or session.committed_token_ids[token_index] != token_id
                            )
                        )
                    ):
                        raise AdmissionError("token_order_violation")
                    return
                if (
                    token_index != session.expected_token_index
                    or token_index >= session.max_new_tokens
                ):
                    raise AdmissionError("token_order_violation")
                # Preserve one replay event when capacity permits, but always
                # reserve one slot so a terminal event cannot be starved.
                self._trim_acknowledged_replay_locked(
                    session,
                    target_size=session.capacity - 2,
                )
                if len(session.events) < session.capacity - 1:
                    sequence = session.latest_sequence + 1
                    event = StreamEvent(
                        request_id=session.request_id,
                        sequence=sequence,
                        kind="token",
                        token_index=token_index,
                        text=token_text,
                        token_id=token_id if session.event_protocol == REQUEST_EVENT_PROTOCOL_V2 else None,
                        protocol=session.event_protocol,
                    )
                    self._append_event_locked(session, event)
                    session.token_digests.append(token_digest)
                    session.committed_token_ids.append(token_id)
                    session.expected_token_index += 1
                    self._metrics.increment("token_events_total")
                    return
                if not session.active_subscription:
                    # The consumer vanished: no further acknowledgements can
                    # ever arrive. Drop the oldest unacknowledged event so a
                    # slow, vanished, or dead consumer cannot pin this worker
                    # (spec A4 §5). acknowledged_through — the committed
                    # delivery watermark — is never advanced here; a later
                    # reconnect replay of the dropped prefix fails closed via
                    # the existing resume_cursor_expired rejection.
                    self._drop_oldest_unacknowledged_locked(session)
                    continue
                if (
                    session.last_ack_at is not None
                    and self._clock() - session.last_ack_at
                    > RequestGatewayService.SESSION_SILENT_SUBSCRIBER_STALL_SECONDS
                ):
                    # The consumer is observably stalled: it attached, the
                    # buffer is full, and no acknowledgement has arrived for
                    # longer than the bounded stall budget (a parked SSE
                    # send or a dead-but-attached tab). Reclaim the slot and
                    # keep the producer moving; the request itself is never
                    # cancelled and the dropped prefix fails closed on
                    # reconnect via resume_cursor_expired (spec A4 §5).
                    self._close_subscription(session)
                    self._drop_oldest_unacknowledged_locked(session)
                    continue
                session.condition.wait(timeout=0.05)

    def _append_terminal(
        self,
        session: _Session,
        kind: str,
        *,
        code: str | None = None,
        cancellation_failure: bool = False,
    ) -> None:
        self._append_lifecycle(session, "completion")
        with session.condition:
            if session.terminal_event is not None:
                return
            self._trim_acknowledged_replay_locked(
                session,
                target_size=session.capacity - 1,
            )
            if len(session.events) >= session.capacity:
                raise RuntimeError("terminal_event_capacity_invariant")
            checkpoint_store_identity = session.replay_checkpoint_store_identity
            if (
                checkpoint_store_identity is not None
                and self._replay_checkpoint_root is not None
            ):
                from mycelium_a6_recovery import CheckpointStore

                CheckpointStore(
                    self._replay_checkpoint_root,
                    store_identity=checkpoint_store_identity,
                ).discard()
                session.replay_checkpoint_store_identity = None
            event = StreamEvent(
                request_id=session.request_id,
                sequence=session.latest_sequence + 1,
                kind=kind,
                code=code,
                protocol=session.event_protocol,
            )
            self._append_event_locked(session, event)
            session.terminal_event = event
            session.terminal_count += 1
            if kind == "failed" and code == "request_cleanup_unproven":
                session.terminal_provenance = session.blocked_identity
                session.terminal_attempt = session.blocked_identity
            else:
                session.terminal_attempt = session.terminal_command_identity
            session.blocked_identity = None
            self._metrics.increment(
                {
                    "completed": "requests_completed_total",
                    "cancelled": "requests_cancelled_total",
                    "failed": "requests_failed_total",
                }[kind]
            )
            session.condition.notify_all()

    def _append_cancellation_terminal(self, session: _Session) -> None:
        with session.condition:
            started_at = session.cancellation_started_at
        if started_at is None:
            self._append_terminal(session, "cancelled")
            return
        with session.condition:
            resolved_at = session.cancellation_resolved_at
        # The bound covers cancellation and its cleanup proof.  When the proof was already
        # resolved as the backend stopped, the release that follows (which may replace a route
        # after a peer loss) is bookkeeping and is not charged to it.
        finished_at = self._clock() if resolved_at is None else resolved_at
        within_bound = finished_at - started_at <= _MAX_CANCELLATION_AND_CLEANUP_SECONDS
        with session.condition:
            session.cancellation_within_bound = within_bound
        if within_bound:
            self._append_terminal(session, "cancelled")
        else:
            self._append_cancellation_failure(
                session,
                branch="proof_after_budget",
                elapsed_s=finished_at - started_at,
            )

    def _stamp_cancellation_resolved(self, session: _Session) -> None:
        """Record when the backend's cancellation proof was resolved, before release runs."""

        with session.condition:
            if (
                session.cancellation_started
                and session.backend_cancellation_complete
                and session.cancellation_resolved_at is None
            ):
                session.cancellation_resolved_at = self._clock()

    def _append_cancellation_failure(
        self,
        session: _Session,
        *,
        branch: str = "proof_unproven",
        elapsed_s: float | None = None,
    ) -> None:
        with session.condition:
            started_at = session.cancellation_started_at
        if elapsed_s is None and started_at is not None:
            elapsed_s = self._clock() - started_at
        # Bounded and non-disclosing: fixed branch name and two integers, no identifiers.
        _LOGGER.warning(
            "gateway_cancellation_failure branch=%s elapsed_ms=%d budget_ms=%d",
            branch,
            int(max(0.0, elapsed_s or 0.0) * 1000),
            int(_MAX_CANCELLATION_AND_CLEANUP_SECONDS * 1000),
        )
        with session.condition:
            session.cancellation_within_bound = False
        self._append_terminal(
            session,
            "failed",
            code="cancellation_cleanup_deadline_exceeded",
            cancellation_failure=True,
        )

    def _append_cleanup_unproven_terminal(self, session: _Session) -> None:
        """Publish a terminal that asserts FAILURE TO PROVE, and nothing else.

        The physical route could not prove owner-scoped cleanup inside its window and therefore did
        NOT commit its terminal CAS (``mycelium_live/route.py:4403-4448``, incident state
        ``request_cleanup_unproven``). This path used to publish nothing at all, on the reasoning
        that even a failure terminal would contradict the authoritative command ledger. That
        reasoning holds for a terminal claiming the COMMAND completed, failed or was cleaned up.
        It does not hold for this one, which claims only what the route knows: that cleanup was
        never proven.

        Publishing nothing left the user unable to tell a stuck request from a slow one -- which
        exit criterion 5 ("zero stuck requests") forbids and the pilot journey cannot survive.

        The refusal is recorded on the session so a consumer can see WHICH refusal it was and count
        it as a failure rather than a success. In particular the soak counters must treat
        ``request_cleanup_unproven`` as a failure: a soak that counted it as settled would make the
        criterion easier to pass while the defect got quieter.
        """
        with session.condition:
            session.cleanup_unproven = True
        self._append_terminal(session, "failed", code="request_cleanup_unproven")

    def _await_backend_cancellation(self, session: _Session) -> str:
        """Resolve the owner cancellation without minting a second deadline."""

        with session.condition:
            if not session.cancellation_started:
                return "not_started"
            if not session.backend_started:
                return "accepted"
            deadline = session.cancellation_wall_deadline_at
            while not session.backend_cancellation_complete:
                remaining = (
                    0.0 if deadline is None else deadline - time.monotonic()
                )
                if remaining <= 0:
                    return "unproven"
                session.condition.wait(timeout=remaining)
            if session.backend_cancellation_failed:
                return "unproven"
            if session.backend_cancellation_accepted is False:
                return "rejected"
            return "accepted"

    def _append_lifecycle(self, session: _Session, phase: str) -> None:
        if session.event_protocol != REQUEST_EVENT_PROTOCOL_V2:
            return
        with session.condition:
            if phase in session.emitted_phases or session.terminal_event is not None:
                return
            self._trim_acknowledged_replay_locked(
                session,
                target_size=session.capacity - 2,
            )
            if len(session.events) >= session.capacity - 1:
                raise AdmissionError("event_backpressure")
            event = StreamEvent(
                request_id=session.request_id,
                sequence=session.latest_sequence + 1,
                kind="lifecycle",
                phase=phase,
                protocol=session.event_protocol,
            )
            self._append_event_locked(session, event)
            session.emitted_phases.add(phase)

    def _append_event_locked(self, session: _Session, event: StreamEvent) -> None:
        if session.active_subscription and session.latest_sequence <= session.acknowledged_through:
            # Nothing was owed to the attached consumer until this event: its
            # acknowledgement clock starts now.  Otherwise a healthy consumer
            # that was idle (an A6 recovery buffers the whole successor
            # continuation and publishes it in one burst after a long silence)
            # would be judged by the age of its last ack and reclaimed as
            # stalled the moment the burst fills the buffer.
            session.last_ack_at = self._clock()
        session.events.append(event)
        session.latest_sequence = event.sequence
        session.maximum_buffered = max(session.maximum_buffered, len(session.events))
        session.condition.notify_all()

    def _next_event(
        self,
        session: _Session,
        cursor: int,
        timeout: float | None,
    ) -> StreamEvent | None:
        if timeout is not None and (
            isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout < 0
        ):
            raise ValueError("invalid_stream_timeout")
        deadline = None if timeout is None else time.monotonic() + float(timeout)
        with session.condition:
            while True:
                if cursor < session.discarded_through:
                    # The prefix after this subscription's acknowledged cursor
                    # was reclaimed (stalled-consumer drop).  Serving the
                    # surviving tail would silently skip sequences; fail
                    # closed exactly as a reconnect into the dropped prefix.
                    raise AdmissionError("resume_cursor_expired")
                for event in session.events:
                    if event.sequence > cursor:
                        return event
                terminal = session.terminal_event
                if terminal is not None and cursor >= terminal.sequence:
                    return None
                if (
                    session.worker_done
                    and session.outcome == "terminal_blocked"
                    and cursor >= session.latest_sequence
                ):
                    return None
                if deadline is not None:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError("stream_event_timeout")
                else:
                    remaining = None
                session.condition.wait(timeout=remaining)

    @staticmethod
    def _ack(session: _Session, subscription: EventSubscription, sequence: int) -> None:
        with session.condition:
            if (
                subscription.publisher_generation != session.publisher_generation
                or
                not isinstance(sequence, int)
                or isinstance(sequence, bool)
                or sequence <= subscription._cursor
                or sequence > subscription._delivered
            ):
                raise AdmissionError("invalid_stream_ack")
            subscription._cursor = sequence
            session.acknowledged_through = max(session.acknowledged_through, sequence)
            session.condition.notify_all()

    @staticmethod
    def _discard_through_locked(session: _Session, sequence: int) -> None:
        while session.events and session.events[0].sequence < sequence:
            session.events.pop(0)
        session.discarded_through = max(session.discarded_through, sequence - 1)
        session.acknowledged_through = max(session.acknowledged_through, sequence)
        session.condition.notify_all()

    @staticmethod
    def _trim_acknowledged_replay_locked(
        session: _Session,
        *,
        target_size: int,
    ) -> None:
        while (
            len(session.events) > target_size
            and session.events
            and session.events[0].sequence <= session.acknowledged_through
        ):
            removed = session.events.pop(0)
            session.discarded_through = max(session.discarded_through, removed.sequence)

    @staticmethod
    def _drop_oldest_unacknowledged_locked(session: _Session) -> None:
        """Drop the oldest buffered event whose consumer has vanished.

        Only the replay boundary (discarded_through) advances. The committed
        delivery watermark (acknowledged_through) is untouched: a dropped
        event was never delivered to any consumer, so it must never count as
        a committed delivery for replay (A6 seam 8). A reconnect attempting
        to resume inside the dropped prefix fails closed via the existing
        resume_cursor_expired rejection.
        """
        if not session.events:
            return
        removed = session.events.pop(0)
        session.discarded_through = max(session.discarded_through, removed.sequence)

    @staticmethod
    def _close_subscription(session: _Session) -> None:
        with session.condition:
            session.active_subscription = False
            session.condition.notify_all()

    def _cancel_backend_once(self, session: _Session) -> bool | None:
        with session.condition:
            if not session.backend_started or session.backend_cancelled:
                return session.backend_cancellation_accepted
            session.backend_cancelled = True
            deadline = session.cancellation_deadline_at
        accepted: bool | None = None
        failed = False
        try:
            cancel_with_deadline = getattr(
                self._backend,
                "cancel_with_deadline",
                None,
            )
            if deadline is not None and callable(cancel_with_deadline):
                result = cancel_with_deadline(
                    session.request_id,
                    deadline_monotonic_s=deadline,
                )
            else:
                result = self._backend.cancel(session.request_id)
            accepted = result is not False
        except Exception:
            failed = True
        finally:
            with session.condition:
                session.backend_cancellation_accepted = accepted
                session.backend_cancellation_failed = failed
                session.backend_cancellation_complete = True
                session.condition.notify_all()
        return accepted

    def _backend_failure_code(self, request_id: str) -> str:
        """A backend-retained bounded failure code, else the generic one."""

        code_of = getattr(self._backend, "terminal_failure_code", None)
        if callable(code_of):
            try:
                code = code_of(request_id)
            except Exception:
                code = None
            if is_safe_error_code(code):
                return code
        return "backend_failed"

    def _release_backend_once(self, session: _Session) -> None:
        with session.condition:
            if session.backend_released:
                return
        release = getattr(self._backend, "release", None)
        if callable(release):
            release(session.request_id)
        with session.condition:
            session.backend_released = True
            session.condition.notify_all()

    def _get_session(self, request_id: str) -> _Session:
        if not isinstance(request_id, str) or not request_id:
            raise AdmissionError("unknown_request")
        with self._lock:
            session = self._sessions.get(request_id)
        if session is None:
            raise AdmissionError("unknown_request")
        return session

    @staticmethod
    def _require_session_owner(session: _Session, owner_token: str | None) -> None:
        expected = session.owner_token_digest
        if expected is None:
            return
        if not isinstance(owner_token, str):
            raise AdmissionError("session_owner_mismatch")
        candidate = hashlib.sha256(owner_token.encode("utf-8")).digest()
        if not hmac.compare_digest(candidate, expected):
            raise AdmissionError("session_owner_mismatch")

    def _make_session_room_locked(self) -> None:
        while len(self._sessions) >= self._max_sessions:
            evicted = False
            for request_id, session in tuple(self._sessions.items()):
                with session.condition:
                    terminal = session.terminal_event
                    eligible = (
                        terminal is not None
                        and not session.active_subscription
                        and (session.worker_done or terminal.kind in {"completed", "failed"})
                    )
                if eligible:
                    del self._sessions[request_id]
                    evicted = True
                    break
            if not evicted:
                # No terminal session is eligible. Consider evicting the
                # oldest stuck non-terminal session without a consumer
                # (spec A4 §5). The decision is driven by an observable
                # fact, not a timer: a session whose subscriber attached
                # and then vanished is a confirmed abandoned slot; a
                # session that has never subscribed is a fresh in-flight
                # request inside its own submit→subscribe window and
                # must not be cancelled by its own kind. A small grace
                # backstop remains for the never-subscribed case so a
                # request that never subscribes (truly stuck from the
                # first emission) cannot pin capacity forever; the
                # backstop value is the same order of magnitude as the
                # ordinary browser round-trip time to the gateway
                # (per the prior session's wall-clock observations on
                # the ordinary product path: 1-50 ms) and is exposed
                # as a product constant for tests to assert.
                grace = RequestGatewayService.SESSION_EVICTION_GRACE_SECONDS
                now = time.monotonic()
                for request_id, session in tuple(self._sessions.items()):
                    with session.condition:
                        if session.terminal_event is not None:
                            continue
                        if session.active_subscription:
                            continue
                        # Fast path: an attached-then-vanished consumer
                        # is observably abandoned. Evict without grace.
                        if session.ever_subscribed:
                            abandoned = True
                        else:
                            # Backstop path: never subscribed; only
                            # evict past the bounded grace to protect
                            # the submit→subscribe window.
                            abandoned = (now - session.created_at) >= grace
                    if abandoned:
                        self._cancel_session(session)
                        del self._sessions[request_id]
                        evicted = True
                        break
            if not evicted:
                self._metrics.increment("admission_rejected_total")
                raise AdmissionError("gateway_capacity_exhausted")
