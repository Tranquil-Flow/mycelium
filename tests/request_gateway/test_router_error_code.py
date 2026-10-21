# SPDX-License-Identifier: AGPL-3.0-only
"""V1.1 item g: the chat terminal carries the router's own bounded error code, not a generic ``backend_failed``.

Live demo (2026-10-21): every chat ended ``backend_failed`` in 0.02 s and nothing named the cause.  The router had
the code (``terminal_error_code``, read through ``request_error_code``); the gateway never asked for it, and the router
drops its pending record when the gateway releases the request, so the code has to be read before release.
"""
from __future__ import annotations

from mycelium_request_gateway.backend import RouterSessionBackend
from mycelium_request_gateway.contracts import InferenceSubmission, qualification_binding
from mycelium_request_gateway.service import RequestGatewayService
from test_backend_cli import RecordingCodec, _runtime_stack, _synthetic_execution_graph
from test_core import MutableQualificationSource, _synthetic_qualification


class FailingRouter:
    """Admits, then FAILS at once with a router-side code; the record is gone after ``release_request``."""

    def __init__(self, code, *, recovery_reason=None):
        self.code = code
        self.recovery_reason = recovery_reason
        self.released = False
        self.status = "UNKNOWN"

    def admit(self, request, client_sink, **kwargs):
        del client_sink, kwargs
        self.status = "FAILED"
        return request.request_id

    def request_status(self, request_id):
        del request_id
        return self.status

    def request_error_code(self, request_id):
        del request_id
        return None if self.released else self.code

    def recovery_failure_reason(self, request_id):
        del request_id
        return self.recovery_reason

    def release_request(self, request_id):
        del request_id
        self.released = True

    def cancel(self, request_id):
        del request_id
        return True


class NoErrorCodeRouter(FailingRouter):
    request_error_code = None  # an older router: no reader for its own code


def terminal_code(router):
    qualification = _synthetic_qualification()
    _router, clock, _capacity, _runtime = _runtime_stack(graph=_synthetic_execution_graph())
    backend = RouterSessionBackend(router=router, codec=RecordingCodec(), clock=clock.now)
    service = RequestGatewayService(
        qualification_source=MutableQualificationSource(qualification), backend=backend,
        request_id_source=lambda: "chat-router-code-001", max_buffered_events=8)
    try:
        request_id = service.submit(InferenceSubmission(
            prompt="private prompt", max_new_tokens=2, qualification=qualification_binding(qualification)))
        subscription = service.subscribe(request_id, last_event_id=None)
        while True:
            event = subscription.next_event(timeout=5)
            assert event is not None, "no terminal event"
            subscription.ack(event.sequence)
            if event.kind in {"failed", "completed", "cancelled", "terminal_blocked"}:
                return event.kind, event.code
    finally:
        service.close()


def test_a_router_refusal_reaches_the_chat_terminal_with_its_own_code():
    assert terminal_code(FailingRouter("affected_peer_quarantined")) == ("failed", "affected_peer_quarantined")


def test_the_code_is_read_before_the_router_drops_the_request():
    router = FailingRouter("route_capacity_exhausted")
    assert terminal_code(router) == ("failed", "route_capacity_exhausted")
    assert router.released is True


def test_a_recovery_reason_still_wins_over_the_generic_router_code():
    assert terminal_code(FailingRouter("runtime_error", recovery_reason="recovery_attempts_exhausted")) == (
        "failed", "recovery_attempts_exhausted")


def test_an_unsafe_or_missing_router_code_keeps_the_generic_gateway_code():
    assert terminal_code(FailingRouter("Bad Code! with spaces")) == ("failed", "backend_failed")
    assert terminal_code(FailingRouter(None)) == ("failed", "backend_failed")


def test_a_router_without_an_error_code_reader_keeps_the_generic_gateway_code():
    assert terminal_code(NoErrorCodeRouter("affected_peer_quarantined")) == ("failed", "backend_failed")
