"""Production request-session service shared by HTTP API and CLI clients."""
from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor, TimeoutError as FutureTimeout
from dataclasses import dataclass, field
from dataclasses import replace
import hashlib
import hmac
import threading
import time
from typing import Callable, Protocol

from .contracts import (
    AdmissionError,
    InferenceSubmission,
    REQUEST_EVENT_PROTOCOL,
    REQUEST_EVENT_PROTOCOL_V2,
    REQUEST_GATEWAY_PROTOCOL_V2,
    StreamEvent,
    is_valid_request_id,
    safe_qualification_projection,
)
from .observability import GatewayMetrics
from .qualification import CapturedQualification, QualificationGate, QualificationSource


_MAX_BACKEND_TOKEN_TEXT_BYTES = 1 << 20
_MAX_CANCELLATION_AND_CLEANUP_SECONDS = 2.0


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
        emit_token: Callable[[int, str], None],
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
    terminal_event: StreamEvent | None = None
    terminal_count: int = 0
    maximum_buffered: int = 0
    active_subscription: bool = False
    cancellation_started: bool = False
    cancellation_started_at: float | None = None
    cancellation_deadline_at: float | None = None
    cancellation_wall_deadline_at: float | None = None
    cancellation_within_bound: bool | None = None
    backend_started: bool = False
    backend_cancelled: bool = False
    backend_cancellation_complete: bool = False
    backend_cancellation_failed: bool = False
    backend_cancellation_accepted: bool | None = None
    backend_released: bool = False
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
    SESSION_EVICTION_GRACE_SECONDS = 0.05

    # Bounded budget a buffer-blocked producer grants an attached but
    # non-acknowledging consumer before the slot is reclaimed (spec A4 §5).
    # The consumer is observably stalled only when the event buffer is FULL
    # and no acknowledgement has arrived for longer than this budget; a
    # healthy consumer acks continuously while draining, so the budget is
    # never consulted on the ordinary path. It is a backstop, not a timer:
    # the decision point is the full-buffer block in _accept_token.
    SESSION_SILENT_SUBSCRIBER_STALL_SECONDS = 0.25

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
        self._sessions: dict[str, _Session] = {}
        self._lock = threading.RLock()
        self._closed = False

    def health(self) -> dict[str, str]:
        return {"service": "mycelium-request-gateway", "status": "ok"}

    def current_qualification(self) -> dict[str, object]:
        return safe_qualification_projection(self._gate.current_projection_source())

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
        deadline = time.monotonic() + 2.0
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
            self._gate.revalidate(captured)
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
                lambda token_index, token_text: self._accept_token(
                    session,
                    token_index,
                    token_text,
                ),
                session.stop.is_set,
            )
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
                    self._gate.revalidate(captured)
                    self._append_terminal(session, "completed")
            elif outcome == "cancelled":
                cancellation_resolution = self._await_backend_cancellation(session)
                if cancellation_resolution == "unproven":
                    self._append_cancellation_failure(session)
                else:
                    self._append_cancellation_terminal(session)
            elif outcome == "terminal_blocked":
                # The physical route could not prove owner-scoped cleanup and
                # therefore did not commit its terminal CAS. Retain a
                # deliberately nonterminal session: publishing even a failure
                # here would contradict the authoritative command ledger.
                pass
            else:
                self._append_terminal(session, "failed", code="backend_failed")
            with session.condition:
                session.outcome = outcome
        except AdmissionError as exc:
            if exc.code == "request_cancelled" and session.stop.is_set():
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
                session.submission = None
                session.captured = None
                session.token_digests.clear()
                session.worker_done = True
                session.condition.notify_all()
            self._admission_slots.release()

    def _accept_token(self, session: _Session, token_index: int, token_text: str) -> None:
        if (
            not isinstance(token_index, int)
            or isinstance(token_index, bool)
            or token_index < 0
            or not isinstance(token_text, str)
            or len(token_text) > _MAX_BACKEND_TOKEN_TEXT_BYTES
        ):
            raise AdmissionError("invalid_backend_token")
        try:
            token_bytes = token_text.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise AdmissionError("invalid_backend_token") from exc
        if len(token_bytes) > _MAX_BACKEND_TOKEN_TEXT_BYTES:
            raise AdmissionError("invalid_backend_token")
        token_digest = hashlib.sha256(token_bytes).digest()
        del token_bytes

        if token_index == 0:
            self._append_lifecycle(session, "first_token")
            self._append_lifecycle(session, "decode")

        while True:
            captured = session.captured
            if captured is None:
                raise AdmissionError("request_state_released")
            self._gate.revalidate(captured)
            with session.condition:
                if session.stop.is_set() or session.terminal_event is not None:
                    raise AdmissionError("request_cancelled")
                if token_index < session.expected_token_index:
                    if (
                        token_index >= len(session.token_digests)
                        or session.token_digests[token_index] != token_digest
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
                        protocol=session.event_protocol,
                    )
                    self._append_event_locked(session, event)
                    session.token_digests.append(token_digest)
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
        within_bound = (
            self._clock() - started_at
            <= _MAX_CANCELLATION_AND_CLEANUP_SECONDS
        )
        with session.condition:
            session.cancellation_within_bound = within_bound
        if within_bound:
            self._append_terminal(session, "cancelled")
        else:
            self._append_cancellation_failure(session)

    def _append_cancellation_failure(self, session: _Session) -> None:
        with session.condition:
            session.cancellation_within_bound = False
        self._append_terminal(
            session,
            "failed",
            code="cancellation_cleanup_deadline_exceeded",
            cancellation_failure=True,
        )

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

    @staticmethod
    def _append_event_locked(session: _Session, event: StreamEvent) -> None:
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
                for event in session.events:
                    if event.sequence > cursor:
                        return event
                terminal = session.terminal_event
                if terminal is not None and cursor >= terminal.sequence:
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
