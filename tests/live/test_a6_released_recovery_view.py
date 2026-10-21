# SPDX-License-Identifier: AGPL-3.0-only
"""The released A6 request view still reports that recovery ran.

The gateway releases a request as soon as its backend run returns. A recovery
that settles quickly (completes, or exhausts its attempts) is therefore gone
from the router's pending state before the controlled-fault seam or the case
observation recorder polls, which read ``recovery_required=False`` for a
request that did recover. The released view retains the attempt count
(bounded), never private state.
"""
from __future__ import annotations

from tests.live.test_a6_live_router_recovery import (
    FailingThenRecoveringRoute,
    _runtime,
    _submit_and_drain,
)


def test_released_view_reports_that_a_quickly_settled_recovery_ran(tmp_path) -> None:
    """A recovery that completes before an observer polls still shows it ran."""

    route = FailingThenRecoveringRoute()
    _coordinator, router, service, _qualification = _runtime(tmp_path, route)
    try:
        request_id, events = _submit_and_drain(service, _qualification)
        assert [e.kind for e in events if e.kind in {"completed", "failed"}] == ["completed"]
        assert request_id not in router._pending
        assert router.a6_request_view(request_id) == {
            "retained": False,
            "recovery_failure_reason": None,
            "recovery_attempts": 1,
            "source_failure_observed": True,
            "terminal_status": "COMPLETED",
            # How the recovery ended, kept past release: no phase deadline
            # fired and the terminal was not cleanup-blocked. This route never
            # asks for completion-cleanup authorization, so its successor
            # output is not marked finished.
            "recovery_deadline_exceeded": False,
            "recovery_deadline_phase": None,
            "recovery_output_finished": False,
            "terminal_blocked_reason": None,
        }
        assert router.a6_request_view("never-admitted") is None
    finally:
        service.close()


def test_released_view_reports_exhausted_attempts_with_the_failure(tmp_path) -> None:
    route = FailingThenRecoveringRoute(fail_successor=True)
    _coordinator, router, service, _qualification = _runtime(tmp_path, route)
    try:
        request_id, events = _submit_and_drain(service, _qualification)
        assert [e.kind for e in events if e.kind in {"completed", "failed"}] == ["failed"]
        view = router.a6_request_view(request_id)
        assert view["retained"] is False
        assert view["recovery_attempts"] == 2
        assert view["source_failure_observed"] is True
        assert view["recovery_failure_reason"] is not None
        assert view["terminal_status"] == "FAILED"
    finally:
        service.close()


class _HealthyRoute(FailingThenRecoveringRoute):
    """Completes on the source attempt: no fault, no recovery."""

    def infer(self, prompt_token_ids, *, max_new_tokens, request_id, sink, **_kwargs):
        for index in range(max_new_tokens):
            sink.emit(index, 101 + index)


def test_released_view_keeps_the_terminal_of_a_request_that_never_recovered(
        tmp_path) -> None:
    """The cleanup_reservation_release seam waits on this released view."""

    _coordinator, router, service, _qualification = _runtime(tmp_path, _HealthyRoute())
    try:
        request_id, events = _submit_and_drain(service, _qualification)
        assert [e.kind for e in events if e.kind in {"completed", "failed"}] == ["completed"]
        assert request_id not in router._pending
        assert router.a6_request_view(request_id) == {
            "retained": False,
            "recovery_failure_reason": None,
            "terminal_status": "COMPLETED",
        }
    finally:
        service.close()


def test_cleanup_seam_wait_terminal_settles_on_the_released_view(tmp_path) -> None:
    from types import SimpleNamespace

    from mycelium_a6_product_fault_seams import LiveProductFaultTarget

    route = _HealthyRoute()
    _coordinator, router, service, _qualification = _runtime(tmp_path, route)
    try:
        request_id, _events = _submit_and_drain(service, _qualification)
        clock = iter(range(1_000)).__next__
        target = LiveProductFaultTarget(router=router, route=route, service=service,
                                        clock=lambda: float(clock()), sleep=lambda _s: None)
        view = target.wait_terminal(SimpleNamespace(request_id=request_id), seconds=120.0)
        assert view["terminal_status"] == "COMPLETED"
        assert clock() < 5, "settled on the first poll, not at the deadline"
    finally:
        service.close()
