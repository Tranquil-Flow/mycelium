from __future__ import annotations

import threading

import pytest

from mycelium_request_gateway.contracts import InferenceSubmission, qualification_binding
from mycelium_request_gateway.service import RequestGatewayService
from test_core import MutableQualificationSource, _synthetic_qualification


def _submission(qualification) -> InferenceSubmission:
    return InferenceSubmission(
        prompt="private prompt",
        max_new_tokens=2,
        qualification=qualification_binding(qualification),
    )


def _terminal(service: RequestGatewayService, request_id: str):
    with service.subscribe(request_id, last_event_id=None) as subscription:
        events = []
        while True:
            event = subscription.next_event(timeout=1.0)
            if event is None:
                return events[-1]
            events.append(event)
            subscription.ack(event.sequence)


def test_cancel_terminal_waits_for_backend_interruption_and_cleanup() -> None:
    qualification = _synthetic_qualification()

    class Backend:
        def __init__(self) -> None:
            self.started = threading.Event()
            self.release = threading.Event()

        def run(self, request_id, submission, emit_token, is_cancelled):
            self.started.set()
            self.release.wait(timeout=2.0)
            return "cancelled" if is_cancelled() else "completed"

        def cancel(self, request_id):
            return None

    backend = Backend()
    service = RequestGatewayService(
        qualification_source=MutableQualificationSource(qualification),
        backend=backend,
        request_id_source=lambda: "a4-cancel-cleanup",
    )
    try:
        request_id = service.submit(_submission(qualification))
        assert backend.started.wait(timeout=1.0)
        assert service.cancel(request_id) is True
        session = service._sessions[request_id]
        with session.condition:
            assert session.terminal_event is None
        backend.release.set()
        assert _terminal(service, request_id).kind == "cancelled"
        assert session.cancellation_within_bound is True
    finally:
        backend.release.set()
        service.close()


def test_cancellation_terminal_is_published_after_backend_release() -> None:
    qualification = _synthetic_qualification()

    class Backend:
        def __init__(self) -> None:
            self.started = threading.Event()
            self.allow_return = threading.Event()
            self.released = threading.Event()

        def run(self, request_id, submission, emit_token, is_cancelled):
            self.started.set()
            self.allow_return.wait(timeout=1.0)
            return "cancelled"

        def cancel(self, request_id):
            self.allow_return.set()

        def release(self, request_id):
            self.released.set()

    backend = Backend()
    service = RequestGatewayService(
        qualification_source=MutableQualificationSource(qualification),
        backend=backend,
        request_id_source=lambda: "a4-release-before-terminal",
    )
    try:
        request_id = service.submit(_submission(qualification))
        assert backend.started.wait(timeout=1.0)
        assert service.cancel(request_id) is True
        terminal = _terminal(service, request_id)

        assert terminal.kind == "cancelled"
        assert backend.released.is_set()
        assert service._sessions[request_id].backend_released is True
    finally:
        backend.allow_return.set()
        service.close()


def test_backend_missing_total_two_second_bound_fails_closed() -> None:
    qualification = _synthetic_qualification()
    now = [10.0]

    class Backend:
        def __init__(self) -> None:
            self.started = threading.Event()
            self.release = threading.Event()

        def run(self, request_id, submission, emit_token, is_cancelled):
            self.started.set()
            self.release.wait(timeout=1.0)
            return "cancelled"

        def cancel(self, request_id):
            now[0] = 12.001
            self.release.set()

    backend = Backend()
    service = RequestGatewayService(
        qualification_source=MutableQualificationSource(qualification),
        backend=backend,
        request_id_source=lambda: "a4-cancel-ineligible",
        clock=lambda: now[0],
    )
    try:
        request_id = service.submit(_submission(qualification))
        assert backend.started.wait(timeout=1.0)
        assert service.cancel(request_id) is True
        terminal = _terminal(service, request_id)
        assert terminal.kind == "failed"
        assert terminal.code == "cancellation_cleanup_deadline_exceeded"
        assert service._sessions[request_id].cancellation_within_bound is False
    finally:
        backend.release.set()
        service.close()


def test_gateway_propagates_one_original_absolute_cancellation_deadline() -> None:
    qualification = _synthetic_qualification()
    now = [10.0]

    class Backend:
        def __init__(self) -> None:
            self.started = threading.Event()
            self.release = threading.Event()
            self.deadlines: list[float] = []

        def run(self, request_id, submission, emit_token, is_cancelled):
            self.started.set()
            self.release.wait(timeout=1.0)
            return "cancelled"

        def cancel(self, request_id):
            raise AssertionError("product cancellation must carry the owner deadline")

        def cancel_with_deadline(self, request_id, *, deadline_monotonic_s):
            self.deadlines.append(deadline_monotonic_s)
            self.release.set()

        def release_request(self, request_id):
            return None

    backend = Backend()
    service = RequestGatewayService(
        qualification_source=MutableQualificationSource(qualification),
        backend=backend,
        request_id_source=lambda: "a4-owner-deadline",
        clock=lambda: now[0],
    )
    try:
        request_id = service.submit(_submission(qualification))
        assert backend.started.wait(timeout=1.0)
        assert service.cancel(request_id) is True
        assert backend.deadlines == [12.0]
        assert _terminal(service, request_id).kind == "cancelled"
    finally:
        backend.release.set()
        service.close()


def test_cleanup_unproven_backend_outcome_cannot_publish_terminal() -> None:
    qualification = _synthetic_qualification()

    class Backend:
        def run(self, request_id, submission, emit_token, is_cancelled):
            return "terminal_blocked"

        def cancel(self, request_id):
            return None

        def release(self, request_id):
            return None

    service = RequestGatewayService(
        qualification_source=MutableQualificationSource(qualification),
        backend=Backend(),
        request_id_source=lambda: "a4-cleanup-unproven",
    )
    try:
        request_id = service.submit(_submission(qualification))
        session = service._sessions[request_id]
        assert session.future is not None
        session.future.result(timeout=1.0)

        assert session.outcome == "terminal_blocked"
        assert session.terminal_event is None
        assert service.terminal_event_count(request_id) == 0
        assert session.backend_released is True
    finally:
        service.close()


def test_cancelled_session_cannot_override_cleanup_unproven_backend_outcome() -> None:
    qualification = _synthetic_qualification()

    class Backend:
        def __init__(self) -> None:
            self.started = threading.Event()
            self.cancelled = threading.Event()

        def run(self, request_id, submission, emit_token, is_cancelled):
            self.started.set()
            assert self.cancelled.wait(timeout=1.0)
            return "terminal_blocked"

        def cancel(self, request_id):
            self.cancelled.set()

        def release(self, request_id):
            return None

    backend = Backend()
    service = RequestGatewayService(
        qualification_source=MutableQualificationSource(qualification),
        backend=backend,
        request_id_source=lambda: "a4-cancel-cleanup-unproven",
    )
    try:
        request_id = service.submit(_submission(qualification))
        assert backend.started.wait(timeout=1.0)
        assert service.cancel(request_id) is True
        session = service._sessions[request_id]
        assert session.future is not None
        session.future.result(timeout=1.0)

        assert session.outcome == "terminal_blocked"
        assert session.terminal_event is None
        assert service.terminal_event_count(request_id) == 0
    finally:
        backend.cancelled.set()
        service.close()


def test_cancel_one_request_does_not_mutate_another() -> None:
    qualification = _synthetic_qualification()

    class Backend:
        def __init__(self) -> None:
            self.started = {name: threading.Event() for name in ("isolated-a", "isolated-b")}
            self.release_b = threading.Event()

        def run(self, request_id, submission, emit_token, is_cancelled):
            self.started[request_id].set()
            if request_id == "isolated-a":
                while not is_cancelled():
                    self.release_b.wait(timeout=0.005)
                return "cancelled"
            assert self.release_b.wait(timeout=1.0)
            emit_token(0, "healthy")
            return "completed"

        def cancel(self, request_id):
            return None

        def release(self, request_id):
            return None

    identifiers = iter(("isolated-a", "isolated-b"))
    backend = Backend()
    service = RequestGatewayService(
        qualification_source=MutableQualificationSource(qualification),
        backend=backend,
        request_id_source=lambda: next(identifiers),
        max_concurrent_requests=2,
        max_pending_requests=2,
    )
    try:
        first = service.submit(_submission(qualification))
        second = service.submit(_submission(qualification))
        assert backend.started[first].wait(timeout=1.0)
        assert backend.started[second].wait(timeout=1.0)

        assert service.cancel(first) is True
        backend.release_b.set()
        assert _terminal(service, first).kind == "cancelled"
        assert _terminal(service, second).kind == "completed"
        assert service.terminal_event_count(first) == 1
        assert service.terminal_event_count(second) == 1
        assert service._sessions[second].cancellation_started is False
    finally:
        backend.release_b.set()
        service.close()


def test_worker_pool_admission_is_bounded_without_blocking_caller() -> None:
    qualification = _synthetic_qualification()

    class Backend:
        def __init__(self) -> None:
            self.release = threading.Event()

        def run(self, request_id, submission, emit_token, is_cancelled):
            self.release.wait(timeout=2.0)
            return "cancelled" if is_cancelled() else "completed"

        def cancel(self, request_id):
            return None

    identifiers = iter(("bounded-a", "bounded-b"))
    backend = Backend()
    service = RequestGatewayService(
        qualification_source=MutableQualificationSource(qualification),
        backend=backend,
        request_id_source=lambda: next(identifiers),
        max_concurrent_requests=1,
        max_pending_requests=1,
    )
    try:
        service.submit(_submission(qualification))
        with pytest.raises(Exception) as caught:
            service.submit(_submission(qualification))
        assert getattr(caught.value, "code", None) == "gateway_capacity_exhausted"
    finally:
        backend.release.set()
        service.close()
