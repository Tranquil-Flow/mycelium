"""Public-interface lifecycle, replay-window, and capacity conformance tests."""

from __future__ import annotations

from dataclasses import replace
import threading
import time

import pytest

from mycelium_request_gateway.contracts import AdmissionError
from mycelium_request_gateway.service import RequestGatewayService

from .support import (
    CountingBackend,
    MutableQualificationSource,
    clone_qualification,
    drain,
    submission,
    synthetic_qualification,
)


class PausingRevalidationSource:
    """Allow capture, then pause the worker's first continuous revalidation."""

    def __init__(self, qualification) -> None:
        self.value = qualification
        self.blocked = threading.Event()
        self.release = threading.Event()
        self._lock = threading.Lock()
        self._reads = 0

    def current(self):
        with self._lock:
            self._reads += 1
            reads = self._reads
        if reads == 2:
            self.blocked.set()
            self.release.wait()
        return self.value


def test_cancel_before_backend_start_has_no_runtime_capacity_or_kv_effects():
    qualification = synthetic_qualification()
    source = PausingRevalidationSource(qualification)

    def script(_backend, _request_id, _submission, _emit_token, _is_cancelled):
        raise AssertionError("cancelled request reached backend runtime")

    backend = CountingBackend(script)
    service = RequestGatewayService(
        qualification_source=source,
        backend=backend,
        request_id_source=lambda: "request-cancel-before-start",
        max_buffered_events=4,
    )
    try:
        request_id = service.submit(submission(qualification))
        assert source.blocked.wait(timeout=2)

        assert service.cancel(request_id) is True
        source.release.set()
        events = drain(service, request_id)
        service.close()

        assert [event.kind for event in events] == ["accepted", "cancelled"]
        assert service.terminal_event_count(request_id) == 1
        assert backend.finished.is_set() is False
        assert backend.counters() == (0, 0, 0, 0, 0, 0, 0, 0)
    finally:
        source.release.set()
        service.close()


def test_cancel_during_stream_releases_runtime_capacity_and_kv_once():
    qualification = synthetic_qualification()

    def script(backend, _request_id, _submission, _emit_token, is_cancelled):
        backend.release.wait(timeout=2)
        return "cancelled" if is_cancelled() else "completed"

    backend = CountingBackend(script)
    service = RequestGatewayService(
        qualification_source=MutableQualificationSource(qualification),
        backend=backend,
        request_id_source=lambda: "request-cancel-during-stream",
        max_buffered_events=4,
    )
    try:
        request_id = service.submit(submission(qualification))
        assert backend.started.wait(timeout=2)

        assert service.cancel(request_id) is True
        assert service.cancel(request_id) is False
        events = drain(service, request_id)
        assert backend.finished.wait(timeout=2)

        assert [event.kind for event in events] == ["accepted", "cancelled"]
        assert service.terminal_event_count(request_id) == 1
        assert backend.counters() == (1, 1, 1, 1, 1, 1, 0, 0)
    finally:
        service.close()


def test_disconnect_reconnect_replays_unacknowledged_token_then_resumes_in_order():
    qualification = synthetic_qualification()
    first_emitted = threading.Event()

    def script(backend, _request_id, _submission, emit_token, is_cancelled):
        emit_token(0, "alpha")
        first_emitted.set()
        backend.release.wait(timeout=2)
        if is_cancelled():
            return "cancelled"
        emit_token(1, "beta")
        return "completed"

    backend = CountingBackend(script)
    service = RequestGatewayService(
        qualification_source=MutableQualificationSource(qualification),
        backend=backend,
        request_id_source=lambda: "request-resume",
        max_buffered_events=5,
    )
    try:
        request_id = service.submit(submission(qualification))
        assert first_emitted.wait(timeout=2)

        initial = service.subscribe(request_id, last_event_id=None)
        accepted = initial.next_event(timeout=2)
        assert accepted is not None and accepted.kind == "accepted"
        initial.ack(accepted.sequence)
        delivered_not_applied = initial.next_event(timeout=2)
        assert delivered_not_applied is not None
        assert delivered_not_applied.text == "alpha"
        initial.close()

        resumed = service.subscribe(request_id, last_event_id=accepted.sequence)
        replayed = resumed.next_event(timeout=2)
        assert replayed == replace(
            delivered_not_applied,
            publisher_generation=resumed.publisher_generation,
        )
        resumed.ack(replayed.sequence)
        backend.release.set()

        remaining = []
        while True:
            event = resumed.next_event(timeout=2)
            if event is None:
                break
            remaining.append(event)
            resumed.ack(event.sequence)
            if event.kind in {"completed", "cancelled", "failed"}:
                break
        resumed.close()

        applied = (replayed, *remaining)
        assert [event.text for event in applied if event.kind == "token"] == [
            "alpha",
            "beta",
        ]
        assert applied[-1].kind == "completed"
        assert service.terminal_event_count(request_id) == 1
        assert backend.counters() == (1, 0, 1, 1, 1, 1, 0, 0)
    finally:
        backend.release.set()
        service.close()


def test_backpressure_drops_oldest_unacknowledged_when_no_consumer_consumes():
    qualification = synthetic_qualification()
    first_emitted = threading.Event()
    emit_total = {"count": 0}

    def script(_backend, _request_id, _submission, emit_token, _is_cancelled):
        emit_token(0, "alpha")
        first_emitted.set()
        emit_token(1, "beta")
        emit_total["count"] += 2
        return "completed"

    backend = CountingBackend(script)
    service = RequestGatewayService(
        qualification_source=MutableQualificationSource(qualification),
        backend=backend,
        request_id_source=lambda: "request-backpressure",
        max_buffered_events=3,
    )
    try:
        request_id = service.submit(submission(qualification))
        assert first_emitted.wait(timeout=2)
        assert backend.finished.wait(timeout=2)
        assert service.buffered_event_count(request_id) <= 3
        assert service.maximum_observed_buffered_events(request_id) <= 3
        assert emit_total["count"] >= 2

        with pytest.raises(AdmissionError) as expired:
            service.subscribe(request_id, last_event_id=None)
        assert expired.value.code == "resume_cursor_expired"
    finally:
        service.close()


def test_session_capacity_evicts_oldest_unattached_under_pressure():
    qualification = synthetic_qualification()
    request_ids = iter(("capacity-a", "capacity-b"))

    def script_stuck(backend, _request_id, _submission, _emit_token, is_cancelled):
        backend.release.wait(timeout=2)
        return "cancelled" if is_cancelled() else "completed"

    def script_normal(backend, _request_id, _submission, _emit_token, _is_cancelled):
        backend.release.set()
        return "completed"

    scripts = [script_stuck, script_normal]

    def dispatcher(backend, request_id, submission, emit_token, is_cancelled):
        return scripts.pop(0)(backend, request_id, submission, emit_token, is_cancelled)

    backend = CountingBackend(dispatcher)
    service = RequestGatewayService(
        qualification_source=MutableQualificationSource(qualification),
        backend=backend,
        request_id_source=lambda: next(request_ids),
        max_buffered_events=4,
        max_sessions=1,
    )
    try:
        service.submit(submission(qualification))
        assert backend.started.wait(timeout=2)

        # A second submission arriving inside the minimum-age grace is
        # rejected with ``gateway_capacity_exhausted`` so a fresh request
        # inside its own submit→subscribe window cannot be cancelled by
        # its own kind. After the grace elapses, the genuinely stuck
        # resident is evicted normally and the second submit is admitted.
        with pytest.raises(AdmissionError) as within_grace:
            service.submit(submission(qualification))
        assert within_grace.value.code == "gateway_capacity_exhausted"
        time.sleep(
            RequestGatewayService.SESSION_EVICTION_GRACE_SECONDS + 0.1
        )

        second = service.submit(submission(qualification))
        assert backend.finished.wait(timeout=2)
        assert len(service._sessions) == 1

        events = drain(service, second)
        assert events[-1].kind == "completed"
        assert backend.runtime_starts == 2
    finally:
        service.close()


def test_revocation_before_racing_token_wins_without_token_or_private_error():
    qualification = synthetic_qualification()
    source = MutableQualificationSource(qualification)

    def script(backend, _request_id, _submission, emit_token, _is_cancelled):
        backend.release.wait(timeout=2)
        emit_token(0, "private-token-canary")
        return "completed"

    backend = CountingBackend(script)
    service = RequestGatewayService(
        qualification_source=source,
        backend=backend,
        request_id_source=lambda: "request-revocation-race",
        max_buffered_events=4,
    )
    try:
        request_id = service.submit(
            submission(qualification, prompt="private-prompt-canary")
        )
        assert backend.started.wait(timeout=2)
        source.value = None
        backend.release.set()
        events = drain(service, request_id)

        assert [event.kind for event in events] == ["accepted", "failed"]
        assert events[-1].code == "route_dropped"
        assert "private" not in events[-1].code
        assert service.terminal_event_count(request_id) == 1
        assert backend.counters() == (1, 1, 1, 1, 1, 1, 0, 0)
        assert all("private" not in key for key in service.metrics_snapshot())
    finally:
        backend.release.set()
        service.close()


def test_non_utf8_backend_token_fails_closed_with_stable_code_and_cleanup():
    qualification = synthetic_qualification()

    def script(_backend, _request_id, _submission, emit_token, _is_cancelled):
        emit_token(0, "\ud800")
        return "completed"

    backend = CountingBackend(script)
    service = RequestGatewayService(
        qualification_source=MutableQualificationSource(qualification),
        backend=backend,
        request_id_source=lambda: "request-invalid-unicode-token",
        max_buffered_events=4,
    )
    try:
        request_id = service.submit(submission(qualification))
        events = drain(service, request_id)

        assert [event.kind for event in events] == ["accepted", "failed"]
        assert events[-1].code == "invalid_backend_token"
        assert service.terminal_event_count(request_id) == 1
        assert backend.counters() == (1, 1, 1, 1, 1, 1, 0, 0)
    finally:
        service.close()


def test_revocation_between_admission_and_start_never_enters_or_cancels_backend():
    qualification = synthetic_qualification()
    source = PausingRevalidationSource(qualification)

    def script(_backend, _request_id, _submission, _emit_token, _is_cancelled):
        raise AssertionError("revoked request reached backend runtime")

    backend = CountingBackend(script)
    service = RequestGatewayService(
        qualification_source=source,
        backend=backend,
        request_id_source=lambda: "request-revoked-before-start",
        max_buffered_events=4,
    )
    try:
        request_id = service.submit(submission(qualification))
        assert source.blocked.wait(timeout=2)
        source.value = clone_qualification(
            qualification,
            route_ready=False,
            reason_codes=("synthetic_revocation",),
        )
        source.release.set()
        events = drain(service, request_id)

        assert [event.kind for event in events] == ["accepted", "failed"]
        assert events[-1].code == "readiness_revoked"
        assert backend.finished.is_set() is False
        assert backend.counters() == (0, 0, 0, 0, 0, 0, 0, 0)
    finally:
        source.release.set()
        service.close()


def test_generated_request_id_collision_fails_before_gateway_side_effects():
    qualification = synthetic_qualification()

    def script(backend, _request_id, _submission, _emit_token, is_cancelled):
        backend.release.wait()
        return "cancelled" if is_cancelled() else "completed"

    backend = CountingBackend(script)
    service = RequestGatewayService(
        qualification_source=MutableQualificationSource(qualification),
        backend=backend,
        request_id_source=lambda: "request-collision",
        max_buffered_events=4,
        max_sessions=2,
    )
    try:
        request_id = service.submit(submission(qualification, prompt="same"))
        assert backend.started.wait(timeout=2)
        before = (
            backend.counters(),
            service.metrics_snapshot(),
            service.buffered_event_count(request_id),
            service.terminal_event_count(request_id),
        )

        for prompt in ("same", "different"):
            with pytest.raises(AdmissionError) as collision:
                service.submit(submission(qualification, prompt=prompt))
            assert collision.value.code == "duplicate_request_id"
            after = (
                backend.counters(),
                service.metrics_snapshot(),
                service.buffered_event_count(request_id),
                service.terminal_event_count(request_id),
            )
            assert after == before
    finally:
        service.close()


def test_oversized_backend_token_fails_before_buffer_growth():
    qualification = synthetic_qualification()

    def script(_backend, _request_id, _submission, emit_token, _is_cancelled):
        emit_token(0, "x" * 1_048_577)
        return "completed"

    backend = CountingBackend(script)
    service = RequestGatewayService(
        qualification_source=MutableQualificationSource(qualification),
        backend=backend,
        request_id_source=lambda: "request-oversized-token",
        max_buffered_events=4,
    )
    try:
        request_id = service.submit(submission(qualification))
        events = drain(service, request_id)

        assert [event.kind for event in events] == ["accepted", "failed"]
        assert events[-1].code == "invalid_backend_token"
        assert service.maximum_observed_buffered_events(request_id) == 2
        assert service.metrics_snapshot()["token_events_total"] == 0
        assert backend.counters() == (1, 1, 1, 1, 1, 1, 0, 0)
    finally:
        service.close()
