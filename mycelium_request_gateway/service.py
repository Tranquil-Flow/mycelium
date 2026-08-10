"""Production request-session service shared by HTTP API and CLI clients."""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import threading
import time
from typing import Callable, Protocol

from .contracts import (
    AdmissionError,
    InferenceSubmission,
    StreamEvent,
    is_valid_request_id,
    safe_qualification_projection,
)
from .observability import GatewayMetrics
from .qualification import CapturedQualification, QualificationGate, QualificationSource


_MAX_BACKEND_TOKEN_TEXT_BYTES = 1 << 20


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

    def cancel(self, request_id: str) -> None: ...


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
    backend_started: bool = False
    backend_cancelled: bool = False
    worker_done: bool = False
    thread: threading.Thread | None = None
    outcome: str | None = None


class EventSubscription:
    """Single-consumer acknowledged stream; close never cancels inference."""

    def __init__(
        self,
        service: "RequestGatewayService",
        session: _Session,
        cursor: int,
    ) -> None:
        self._service = service
        self._session = session
        self._cursor = cursor
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
            self._delivered = event.sequence
        return event

    def ack(self, sequence: int) -> None:
        if self._closed:
            raise AdmissionError("stream_closed")
        self._service._ack(self._session, self, sequence)

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

    def __init__(
        self,
        *,
        qualification_source: QualificationSource,
        backend: InferenceBackend,
        request_id_source: Callable[[], str],
        max_buffered_events: int = 64,
        max_sessions: int = 1_024,
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
        self._gate = QualificationGate(qualification_source)
        self._backend = backend
        self._request_id_source = request_id_source
        self._max_buffered_events = max_buffered_events
        self._max_sessions = max_sessions
        self._metrics = metrics if metrics is not None else GatewayMetrics()
        self._sessions: dict[str, _Session] = {}
        self._lock = threading.RLock()
        self._closed = False

    def health(self) -> dict[str, str]:
        return {"service": "mycelium-request-gateway", "status": "ok"}

    def current_qualification(self) -> dict[str, object]:
        return safe_qualification_projection(self._gate.current_projection_source())

    def submit(self, submission: InferenceSubmission) -> str:
        if not isinstance(submission, InferenceSubmission):
            raise AdmissionError("invalid_submission")
        try:
            captured = self._gate.capture(submission.qualification)
        except AdmissionError:
            self._metrics.increment("admission_rejected_total")
            raise
        with self._lock:
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
            )
            with session.condition:
                self._append_event_locked(
                    session,
                    StreamEvent(request_id=request_id, sequence=0, kind="accepted"),
                )
            thread = threading.Thread(
                target=self._run,
                args=(session,),
                name=f"mycelium-request-{request_id[:16]}",
                daemon=True,
            )
            session.thread = thread
            self._sessions[request_id] = session
            thread.start()
            self._metrics.increment("requests_admitted_total")
        return request_id

    def subscribe(
        self,
        request_id: str,
        *,
        last_event_id: int | None,
    ) -> EventSubscription:
        session = self._get_session(request_id)
        cursor = -1 if last_event_id is None else last_event_id
        if (
            not isinstance(cursor, int)
            or isinstance(cursor, bool)
            or cursor < -1
        ):
            raise AdmissionError("invalid_last_event_id")
        with session.condition:
            if session.active_subscription:
                raise AdmissionError("stream_already_attached")
            if cursor < session.discarded_through:
                raise AdmissionError("resume_cursor_expired")
            if cursor > session.latest_sequence:
                raise AdmissionError("invalid_last_event_id")
            self._discard_through_locked(session, cursor)
            session.active_subscription = True
            return EventSubscription(self, session, cursor)

    def cancel(self, request_id: str) -> bool:
        session = self._get_session(request_id)
        with session.condition:
            if session.terminal_event is not None or session.cancellation_started:
                return False
            session.cancellation_started = True
            session.stop.set()
            session.condition.notify_all()
            backend_started = session.backend_started
        if backend_started:
            self._cancel_backend_once(session)
        self._append_terminal(session, "cancelled")
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
                self.cancel(session.request_id)
        for session in sessions:
            thread = session.thread
            if thread is not None and thread is not threading.current_thread():
                thread.join(timeout=2)

    def _run(self, session: _Session) -> None:
        try:
            captured = session.captured
            submission = session.submission
            if captured is None or submission is None:
                raise AdmissionError("request_state_released")
            self._gate.revalidate(captured)
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
                self._append_terminal(session, "cancelled")
                return
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
            with session.condition:
                cancellation_started = session.cancellation_started
            if cancellation_started:
                self._append_terminal(session, "cancelled")
            elif outcome == "completed":
                captured = session.captured
                if captured is None:
                    raise AdmissionError("request_state_released")
                self._gate.revalidate(captured)
                self._append_terminal(session, "completed")
            elif outcome == "cancelled":
                self._append_terminal(session, "cancelled")
            else:
                self._append_terminal(session, "failed", code="backend_failed")
            with session.condition:
                session.outcome = "cancelled" if cancellation_started else outcome
        except AdmissionError as exc:
            if exc.code == "request_cancelled" and session.stop.is_set():
                self._append_terminal(session, "cancelled")
            else:
                session.stop.set()
                self._cancel_backend_once(session)
                self._append_terminal(session, "failed", code=exc.code)
            with session.condition:
                session.outcome = exc.code
        except Exception:
            session.stop.set()
            self._cancel_backend_once(session)
            self._append_terminal(session, "failed", code="backend_failed")
            with session.condition:
                session.outcome = "backend_failed"
        finally:
            release = getattr(self._backend, "release", None)
            if callable(release):
                release(session.request_id)
            with session.condition:
                session.submission = None
                session.captured = None
                session.token_digests.clear()
                session.worker_done = True
                session.condition.notify_all()

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
                    )
                    self._append_event_locked(session, event)
                    session.token_digests.append(token_digest)
                    session.expected_token_index += 1
                    self._metrics.increment("token_events_total")
                    return
                session.condition.wait(timeout=0.05)

    def _append_terminal(
        self,
        session: _Session,
        kind: str,
        *,
        code: str | None = None,
    ) -> None:
        with session.condition:
            if session.terminal_event is not None:
                return
            if session.cancellation_started and kind != "cancelled":
                kind = "cancelled"
                code = None
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
    def _close_subscription(session: _Session) -> None:
        with session.condition:
            session.active_subscription = False
            session.condition.notify_all()

    def _cancel_backend_once(self, session: _Session) -> None:
        with session.condition:
            if not session.backend_started or session.backend_cancelled:
                return
            session.backend_cancelled = True
        try:
            self._backend.cancel(session.request_id)
        except Exception:
            pass

    def _get_session(self, request_id: str) -> _Session:
        if not isinstance(request_id, str) or not request_id:
            raise AdmissionError("unknown_request")
        with self._lock:
            session = self._sessions.get(request_id)
        if session is None:
            raise AdmissionError("unknown_request")
        return session

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
                self._metrics.increment("admission_rejected_total")
                raise AdmissionError("gateway_capacity_exhausted")
