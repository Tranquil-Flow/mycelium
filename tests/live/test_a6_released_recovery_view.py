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
    finally:
        service.close()
