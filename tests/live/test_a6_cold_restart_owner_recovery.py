# SPDX-License-Identifier: AGPL-3.0-only
"""A6 source-failure recovery through the production cold-restart owner.

The physical serve wraps its route in ``ColdRouteRestartOwner``.  A killed
source peer makes the whole route report ``is_alive() is False`` while the
surviving placements can still serve a disjoint successor.  The owner must let
M16 recovery own that request instead of cold-restarting the route, must not
delete the staging a reopen needs, and must still cold-restart a route that
cannot serve the request it was given.
"""
from __future__ import annotations

import time

import pytest

from mycelium_live.route import RouteCounters, RouteIdentity
from mycelium_live.supervisor import (
    COLD_ROUTE_RESTART_ERROR_CODE,
    ColdRouteRestartError,
    ColdRouteRestartOwner,
)
from tests.live.test_a6_live_router_recovery import (
    FailingThenRecoveringRoute,
    _runtime,
    _submit_and_drain,
)


class _KilledSourceRoute(FailingThenRecoveringRoute):
    """The source peer process exits during the first (source) attempt."""

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.source_killed = False
        self.open_count = 0
        self.close_count = 0
        self.cleanup_count = 0

    def open(self) -> RouteIdentity:
        self.open_count += 1
        return RouteIdentity(
            deployment_id="deployment",
            model_id="model",
            resolved_commit="commit",
            endpoint_ids=("endpoint-source", "endpoint-successor"),
        )

    def infer(self, *args, **kwargs):
        if not self.calls:
            try:
                return super().infer(*args, **kwargs)
            finally:
                self.source_killed = True
                self._scoped_runtime_incidents[-1]["fatal_accepted"] = False
        return super().infer(*args, **kwargs)

    def is_alive(self) -> bool:
        return self.open_count > self.close_count and not self.source_killed

    def counters(self) -> RouteCounters:
        return RouteCounters(frames_sent=0, frames_received=0, applied_operation_count=0)

    def a4_liveness_status(self):
        return {
            "protocol": "mycelium.traffic_liveness.v1",
            "deployment_fatal_reason": None,
            "subjects": [
                {"subject_id": "source", "state": "failed" if self.source_killed else "fresh"},
                {"subject_id": "successor", "state": "fresh"},
            ],
            "incidents": [],
        }

    def request_cleanup_receipt_scoped(self, request_id, *, path_attempt, cleanup_owner_id, **_identity):
        # The physical route proves exact cleanup of each attempt it ran.
        for call in reversed(self.calls):
            command = call["command_identity"]
            if call["request_id"] == request_id and command["path_attempt"] == path_attempt:
                return {**command, "cleanup_owner_id": cleanup_owner_id, "node_ids": []}
        return None

    def a4_scoped_runtime_incidents(self):
        return tuple(dict(item) for item in self._scoped_runtime_incidents)

    def close(self) -> None:
        self.close_count += 1

    def cleanup(self) -> None:
        self.cleanup_count += 1


def _owner(routes, qualification):
    created = []

    def factory():
        route = routes[len(created)]
        created.append(route)
        return route

    def qualify(route, _generation):
        return route.open(), qualification

    return ColdRouteRestartOwner(route_factory=factory, open_and_qualify=qualify, restart_budget=1)


def test_killed_source_recovers_on_a_disjoint_successor_without_a_cold_restart(tmp_path) -> None:
    route = _KilledSourceRoute()
    spare = _KilledSourceRoute()
    _coordinator, _router, _service, qualification = _runtime(tmp_path, FailingThenRecoveringRoute())
    owner = _owner([route, spare], qualification)
    owner.open()
    coordinator, router, service, qualification = _runtime(
        tmp_path, owner, qualification=qualification, port_qualification_source=owner,
    )
    try:
        request_id, events = _submit_and_drain(service, qualification)
        terminals = [event.kind for event in events if event.kind in {"completed", "failed", "cancelled"}]
        assert terminals == ["completed"], router.recovery_failure_reason(request_id)
        assert [event.text for event in events if event.kind == "token"] == [
            "<101>", "<102>", "<103>", "<104>",
        ]
        # The successor ran on the surviving route: no cold restart retired
        # the source's incident, and the spare generation served nothing.
        assert len(route.calls) == 2
        assert spare.calls == []
        assert route.cleanup_count == 0
        # Once the recovered request is released the degraded route is
        # replaced, so the serve does not stay unqualified; the replacement
        # reopens the staging the retired route kept.
        deadline = time.monotonic() + 3.0
        while owner.public_status()["cold_route_restart"]["generation"] != 2:
            assert time.monotonic() < deadline, owner.public_status()["cold_route_restart"]
            time.sleep(0.01)
        assert route.close_count == 1
        assert route.cleanup_count == 0
        assert spare.open_count == 1
        assert owner.current() is qualification
    finally:
        service.close()


def test_cold_restart_keeps_the_staging_the_reopen_needs() -> None:
    route = _KilledSourceRoute()
    spare = _KilledSourceRoute()
    owner = _owner([route, spare], "qualification")
    owner.open()
    route.source_killed = True  # dead with no request-scoped incident
    with pytest.raises(ColdRouteRestartError) as caught:
        owner.infer((1,), max_new_tokens=1, request_id="ordinary", sink=None,
                    route_identity={}, locked_path_manifest={"ordered_hops": []},
                    command_identity={})
    assert caught.value.code == COLD_ROUTE_RESTART_ERROR_CODE
    assert route.close_count == 1
    assert route.cleanup_count == 0
    assert spare.open_count == 1


def test_incident_lookup_never_raises_while_the_owner_restarts(tmp_path) -> None:
    route = _KilledSourceRoute()
    owner = _owner([route], "qualification")
    owner.open()
    owner._state = "restarting"  # the lookup must answer, not raise
    from mycelium_live.router_port import LiveRouterPort

    assert LiveRouterPort._route_has_scoped_incident_for(
        type("_Port", (), {"_route": owner})(), type("_Request", (), {"request_id": "x"})()
    ) is False


def test_a_real_route_with_a_killed_source_is_recoverable_not_restartable(monkeypatch, tmp_path) -> None:
    """The owner's predicates read a real PhysicalLiveRoute, not a fake."""

    from mycelium_a6_measured_fault_endpoint import MeasuredFaultEndpoint
    from tests.a6_acceptance.a6_measured_stack import CASE, build_measured_stack, settle
    from tests.a6_acceptance.test_measured_fault_endpoint import identity_of

    with build_measured_stack(monkeypatch, tmp_path) as stack:
        route = stack.route
        with MeasuredFaultEndpoint(
            seams={stack.seam.seam_id: stack.seam}, case_ids=(CASE,),
            expected_identity=identity_of(stack.binding), clock_ms=lambda: 1000,
            incident_source=route.a4_scoped_runtime_incidents, test_only=True,
        ) as endpoint:
            assert endpoint.post(case_id=CASE)[0] == 200
        # PhysicalLiveRoute.open registers one liveness subject per peer; the
        # measured fixture registers only the source, so register the rest.
        from mycelium_live.liveness import LivenessSubject, SubjectKind

        for node_id in route._sessions:
            if node_id not in route._liveness_subjects:
                subject = LivenessSubject(node_id, SubjectKind.PEER, 1)
                route._liveness.register_subject(subject, observed_at_ms=int(time.monotonic() * 1000) - 1_000)
                route._liveness_subjects[node_id] = subject
        stack.source.returncode = -9  # the source peer process is gone
        route._handle_lost_peer_processes()
        assert route.is_alive() is False
        assert ColdRouteRestartOwner._route_survives_peer_loss(route) is True
        owner = ColdRouteRestartOwner(route_factory=lambda: route, open_and_qualify=lambda *_: None)
        assert owner._recovery_owns_failure(route, stack.request_id) is True
        assert owner._recovery_owns_failure(route, "another-request") is False
        for session in route._sessions.values():
            session.returncode = -9  # every peer gone: nothing left to serve
        route._handle_lost_peer_processes()  # the route's own liveness monitor tick
        assert ColdRouteRestartOwner._route_survives_peer_loss(route) is False
        assert owner._recovery_owns_failure(route, stack.request_id) is False
        for session in route._sessions.values():
            session.returncode = None
        stack.source.returncode = -9
        assert settle(stack) is True


class _RefusingRoute(_KilledSourceRoute):
    """A degraded route that refuses any path through its dead peer."""

    def infer(self, *_args, **_kwargs):
        raise RuntimeError("selected_route_not_open")


def test_a_refused_probe_never_restarts_a_route_another_request_still_recovers_on() -> None:
    route = _RefusingRoute()
    spare = _RefusingRoute()
    owner = _owner([route, spare], "qualification")
    owner.open()
    route.source_killed = True
    owner._request_routes["source-request"] = route  # still recovering on this route
    with pytest.raises(RuntimeError, match="selected_route_not_open"):
        owner.infer((1,), max_new_tokens=1, request_id="successor-probe", sink=None,
                    selected_placement_ids=("placement-b",))
    assert route.close_count == 0
    assert spare.open_count == 0
    owner.release_request("successor-probe")
    assert owner.public_status()["cold_route_restart"]["generation"] == 1
    # The recovering request ends; only now is the degraded route replaced.
    owner.release_request("source-request")
    assert owner.public_status()["cold_route_restart"]["generation"] == 2
    assert route.close_count == 1
    assert route.cleanup_count == 0
    assert spare.open_count == 1


def test_an_ordinary_request_through_the_dead_peer_cold_restarts_an_idle_route() -> None:
    route = _RefusingRoute()
    spare = _RefusingRoute()
    owner = _owner([route, spare], "qualification")
    owner.open()
    route.source_killed = True
    with pytest.raises(ColdRouteRestartError) as caught:
        owner.infer((1,), max_new_tokens=1, request_id="ordinary", sink=None)
    assert caught.value.code == COLD_ROUTE_RESTART_ERROR_CODE
    assert route.close_count == 1 and route.cleanup_count == 0
    assert owner.public_status()["cold_route_restart"]["generation"] == 2


def test_cleanup_evidence_is_read_from_the_route_that_ran_the_request() -> None:
    """When the last peer of an A6 successor dies the owner cold-restarts; the
    replacement never ran the request, so the router must read that request's
    cleanup evidence (exited-peer incidents, exact receipts) from the retired
    route the owner still maps it to, never from the replacement."""

    from mycelium_live.router_port import _route_that_ran

    route = _RefusingRoute()
    spare = _RefusingRoute()
    owner = _owner([route, spare], "qualification")
    owner.open()
    route.source_killed = True
    with pytest.raises(ColdRouteRestartError):
        owner.infer((1,), max_new_tokens=1, request_id="successor", sink=None)
    assert owner.public_status()["cold_route_restart"]["generation"] == 2
    assert owner.route_for_request("successor") is route
    assert _route_that_ran(owner, "successor") is route
    assert _route_that_ran(owner, "unknown") is owner
    owner.release_request("successor")
    assert owner.route_for_request("successor") is None
    assert _route_that_ran(route, "successor") is route
