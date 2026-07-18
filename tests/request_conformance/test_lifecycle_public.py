"""Public-interface lifecycle, replay-window, and capacity conformance tests."""

from __future__ import annotations

import threading
import time

import pytest

from mycelium_request_gateway.contracts import AdmissionError
from mycelium_request_gateway.service import RequestGatewayService

from .support import (
    CountingBackend,
    MutableQualificationSource,
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
        events = drain(service, request_id)
        source.release.set()
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
        assert replayed == delivered_not_applied
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


def test_backpressure_reserves_terminal_slot_and_never_exceeds_buffer_bound():
    qualification = synthetic_qualification()
    first_emitted = threading.Event()

    def script(_backend, _request_id, _submission, emit_token, _is_cancelled):
        emit_token(0, "alpha")
        first_emitted.set()
        emit_token(1, "beta")
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
        stream = service.subscribe(request_id, last_event_id=None)

        accepted = stream.next_event(timeout=2)
        assert accepted is not None and accepted.kind == "accepted"
        stream.ack(accepted.sequence)
        first = stream.next_event(timeout=2)
        assert first is not None and first.text == "alpha"
        stream.ack(first.sequence)

        remaining = []
        while True:
            event = stream.next_event(timeout=2)
            if event is None:
                break
            remaining.append(event)
            stream.ack(event.sequence)
            if event.kind in {"completed", "cancelled", "failed"}:
                break
        stream.close()

        assert [event.text for event in remaining if event.kind == "token"] == [
            "beta"
        ]
        assert remaining[-1].kind == "completed"
        assert service.maximum_observed_buffered_events(request_id) <= 3
        assert service.buffered_event_count(request_id) <= 3
        assert backend.counters() == (1, 0, 1, 1, 1, 1, 0, 0)

        with pytest.raises(AdmissionError) as expired:
            service.subscribe(request_id, last_event_id=-1)
        assert expired.value.code == "resume_cursor_expired"
    finally:
        service.close()


def test_session_capacity_reclaims_only_after_cancelled_worker_cleanup():
    qualification = synthetic_qualification()
    request_ids = iter(("capacity-a", "capacity-b"))

    def script(backend, _request_id, _submission, _emit_token, is_cancelled):
        backend.release.wait(timeout=2)
        return "cancelled" if is_cancelled() else "completed"

    backend = CountingBackend(script)
    service = RequestGatewayService(
        qualification_source=MutableQualificationSource(qualification),
        backend=backend,
        request_id_source=lambda: next(request_ids),
        max_buffered_events=4,
        max_sessions=1,
    )
    try:
        first = service.submit(submission(qualification))
        assert backend.started.wait(timeout=2)
        with pytest.raises(AdmissionError) as full:
            service.submit(submission(qualification))
        assert full.value.code == "gateway_capacity_exhausted"

        assert service.cancel(first) is True
        drain(service, first)
        assert backend.finished.wait(timeout=2)

        deadline = time.monotonic() + 2
        while True:
            try:
                second = service.submit(submission(qualification))
                break
            except AdmissionError as exc:
                assert exc.code == "gateway_capacity_exhausted"
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.001)
        events = drain(service, second)

        assert events[-1].kind == "completed"
        assert backend.runtime_starts == 2
        assert backend.capacity_acquires == backend.capacity_releases == 2
        assert backend.kv_acquires == backend.kv_cleanups == 2
        assert backend.active_capacity == backend.active_kv == 0
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
