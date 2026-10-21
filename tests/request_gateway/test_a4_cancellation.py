# SPDX-License-Identifier: AGPL-3.0-only
from __future__ import annotations

import threading

import pytest

from mycelium_request_gateway.contracts import InferenceSubmission, qualification_binding
import mycelium_deadlines
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


def test_backend_missing_total_bound_fails_closed() -> None:
    """A cleanup landing past the owner's bound fails closed with a named code.

    The bound is the registry's since the 2026-09-20 re-derivation (ERRATA-20/21): the
    property under test is the FAIL-CLOSED outcome, not the historical 2 s number, so the
    clock jump past the bound is derived rather than re-typed.
    """

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
            # one millisecond past the owner's re-derived cleanup deadline (start 10.0)
            now[0] = 10.0 + mycelium_deadlines.CANCEL_AND_CLEANUP_BUDGET_SECONDS + 0.001
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


def test_release_time_after_a_proven_cancel_is_not_charged_to_the_cancel_budget() -> None:
    """Route replacement inside ``release`` (peer loss) is not cancellation cleanup.

    The router linearized the cancel and proved cleanup before ``run`` returned. The release
    that follows may cold-restart the route for far longer than the cancel-and-cleanup budget;
    that bookkeeping must not turn a proven cancel into ``failed``.
    """

    qualification = _synthetic_qualification()
    now = [10.0]

    class Backend:
        def __init__(self) -> None:
            self.started = threading.Event()
            self.stop = threading.Event()
            self.released = threading.Event()

        def run(self, request_id, submission, emit_token, is_cancelled):
            self.started.set()
            self.stop.wait(timeout=1.0)
            return "cancelled"

        def cancel(self, request_id):
            raise AssertionError("product cancellation must carry the owner deadline")

        def cancel_with_deadline(self, request_id, *, deadline_monotonic_s):
            self.stop.set()
            return True

        def release(self, request_id):
            now[0] += mycelium_deadlines.CANCEL_AND_CLEANUP_BUDGET_SECONDS + 1.0
            self.released.set()

    backend = Backend()
    service = RequestGatewayService(
        qualification_source=MutableQualificationSource(qualification),
        backend=backend,
        request_id_source=lambda: "a4-release-not-charged",
        clock=lambda: now[0],
    )
    try:
        request_id = service.submit(_submission(qualification))
        assert backend.started.wait(timeout=1.0)
        assert service.cancel(request_id) is True
        terminal = _terminal(service, request_id)
        assert backend.released.is_set()
        assert terminal.kind == "cancelled"
        assert service._sessions[request_id].cancellation_within_bound is True
    finally:
        backend.stop.set()
        service.close()


def test_cancel_proof_resolving_after_the_budget_still_fails_closed(caplog) -> None:
    """The proof itself landing past the bound is still a failure, and the branch is logged."""

    qualification = _synthetic_qualification()
    now = [10.0]

    class Backend:
        def __init__(self) -> None:
            self.started = threading.Event()
            self.stop = threading.Event()

        def run(self, request_id, submission, emit_token, is_cancelled):
            self.started.set()
            self.stop.wait(timeout=1.0)
            return "cancelled"

        def cancel(self, request_id):
            raise AssertionError("product cancellation must carry the owner deadline")

        def cancel_with_deadline(self, request_id, *, deadline_monotonic_s):
            now[0] += mycelium_deadlines.CANCEL_AND_CLEANUP_BUDGET_SECONDS + 1.0
            self.stop.set()
            return True

        def release(self, request_id):
            return None

    backend = Backend()
    service = RequestGatewayService(
        qualification_source=MutableQualificationSource(qualification),
        backend=backend,
        request_id_source=lambda: "a4-proof-late",
        clock=lambda: now[0],
    )
    try:
        with caplog.at_level("WARNING", logger="mycelium_request_gateway.service"):
            request_id = service.submit(_submission(qualification))
            assert backend.started.wait(timeout=1.0)
            assert service.cancel(request_id) is True
            terminal = _terminal(service, request_id)
        assert terminal.kind == "failed"
        assert terminal.code == "cancellation_cleanup_deadline_exceeded"
        assert service._sessions[request_id].cancellation_within_bound is False
        lines = [
            r.getMessage() for r in caplog.records if "gateway_cancellation_failure" in r.getMessage()
        ]
        assert lines == [
            "gateway_cancellation_failure branch=proof_after_budget elapsed_ms=9000 budget_ms=8000"
        ]
    finally:
        backend.stop.set()
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
        assert backend.deadlines == [10.0 + mycelium_deadlines.CANCEL_AND_CLEANUP_BUDGET_SECONDS]
        assert _terminal(service, request_id).kind == "cancelled"
    finally:
        backend.release.set()
        service.close()


def test_cleanup_unproven_backend_outcome_publishes_a_failure_to_prove_terminal() -> None:
    """REVERSAL, deliberate: this test used to assert that NO terminal could be published.

    It was named test_cleanup_unproven_backend_outcome_cannot_publish_terminal and asserted
    terminal_event is None. The suppression was there so an unproven cleanup could not hide behind a
    tidy failure -- which is right -- but it left the user with no terminal at all, which exit
    criterion 5 ("zero stuck requests") forbids and the pilot journey cannot survive. The terminal
    now published claims only what the route knows (cleanup was NOT proven); it asserts no claim
    about the command, so it contradicts nothing, and the session records the refusal so the soak
    counters can count it as a FAILURE rather than a settled request.
    """
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
        terminal = _terminal(service, request_id)
        assert terminal.kind == "failed"
        assert terminal.code == "request_cleanup_unproven"
        assert session.cleanup_unproven is True
        assert service.terminal_event_count(request_id) == 1
        assert session.backend_released is True
    finally:
        service.close()


def test_cancelled_session_still_reports_the_routes_cleanup_unproven_refusal() -> None:
    """The client's cancel must not mask the route's own refusal (reversal, see the test above)."""
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
        terminal = _terminal(service, request_id)
        assert terminal.kind == "failed"
        assert terminal.code == "request_cleanup_unproven"
        assert session.cleanup_unproven is True
        assert service.terminal_event_count(request_id) == 1
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
