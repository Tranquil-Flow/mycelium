# SPDX-License-Identifier: AGPL-3.0-only
"""The failed attempt's route state is retired before its A6 successor runs.

A physical route keeps one active record and one exact cleanup receipt per
request id. The source attempt's receipt (sealed when its command terminal was
recorded) stayed in the route, so the successor attempt of the same request
could never store its own cleanup receipt (``route_cleanup_receipt_identity_
mismatch``) and every recovered request ended cancelled/terminal-blocked.
The router releases exactly the predecessor attempt's scoped state, after its
proof, and never releases the request (that would let the route owner cold
restart the route underneath the successor).
"""
from __future__ import annotations

import threading

from mycelium_live.supervisor import ColdRouteRestartOwner
from tests.live.test_a6_cold_restart_owner_recovery import _KilledSourceRoute, _owner
from tests.live.test_a6_live_router_recovery import _runtime, _submit_and_drain


class _PerRequestReceiptRoute(_KilledSourceRoute):
    """Physical-route receipt semantics: one receipt per request id."""

    is_simulated = False

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.order: list[tuple] = []

    def infer(self, *args, route_identity, **kwargs):
        self.order.append(("infer", route_identity["request_attempt"]))
        return super().infer(*args, route_identity=route_identity, **kwargs)

    def release_request_scoped(self, request_id, *, request_attempt, path_id,
                               path_attempt, path_digest, cleanup_owner_id):
        receipt = self.request_cleanup_receipt_scoped(
            request_id, path_attempt=path_attempt, cleanup_owner_id=cleanup_owner_id)
        assert receipt is not None, "released before cleanup proof"
        assert receipt["request_attempt"] == request_attempt
        assert receipt["path_id"] == path_id and receipt["path_digest"] == path_digest
        self.order.append(("release_scoped", request_attempt))


def test_router_retires_the_source_attempt_before_the_successor_runs(tmp_path) -> None:
    route = _PerRequestReceiptRoute()
    route.open()
    _coordinator, router, service, qualification = _runtime(tmp_path, route)
    try:
        _request_id, events = _submit_and_drain(service, qualification)
        assert [e.kind for e in events if e.kind in {"completed", "failed"}] == ["completed"]
        # Source attempt 1 runs, its exact scoped state is retired, then the
        # successor (attempt 2) runs; the final release retires attempt 2.
        assert route.order[:3] == [
            ("infer", 1), ("release_scoped", 1), ("infer", 2)]
        assert route.order[3:] == [("release_scoped", 2)]
    finally:
        service.close()


def test_owner_attempt_release_keeps_the_request_bound_and_never_restarts() -> None:
    route = _PerRequestReceiptRoute()
    spare = _PerRequestReceiptRoute()
    owner = _owner([route, spare], "qualification")
    owner.open()
    released = threading.Event()

    def release_request_scoped(request_id, **identity):
        released.set()

    route.release_request_scoped = release_request_scoped
    route.source_killed = True  # degraded: an idle release would cold-restart
    with owner._lock:
        owner._request_routes["request-1"] = route
    owner.release_attempt_scoped(
        "request-1", request_attempt=1, path_id="p", path_attempt=0,
        path_digest="d", cleanup_owner_id="o")
    assert released.is_set()
    assert owner._request_routes["request-1"] is route
    status = owner.public_status()["cold_route_restart"]
    assert status["generation"] == 1 and status["attempts"] == 0
    assert spare.open_count == 0


def test_owner_attempt_release_is_a_no_op_for_a_route_without_attempt_state() -> None:
    class _Bare:
        def open(self):
            return _KilledSourceRoute().open()

        def is_alive(self):
            return True

    owner = ColdRouteRestartOwner(
        route_factory=_Bare, open_and_qualify=lambda r, _g: (r.open(), "q"))
    owner.open()
    owner.release_attempt_scoped(
        "request-1", request_attempt=1, path_id="p", path_attempt=0,
        path_digest="d", cleanup_owner_id="o")
