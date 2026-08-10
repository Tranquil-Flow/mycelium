from __future__ import annotations

import pytest

from mycelium_live.health import RouteHealthSource
from mycelium_live.route import FakeLiveRoute
from mycelium_request_gateway.contracts import AdmissionError, qualification_binding
from mycelium_request_gateway.qualification import QualificationGate


def test_closed_route_revokes_readiness_and_rejects_stale_binding(qualified_route):
    qualification, _graph = qualified_route
    route = FakeLiveRoute(scripted_tokens=(1,))
    route.open()
    health = RouteHealthSource(route=route)
    health.publish(qualification)
    gate = QualificationGate(health)
    stale_binding = qualification_binding(qualification)

    assert health.current() is qualification
    route.close()

    assert health.current() is None
    with pytest.raises(AdmissionError, match="route_dropped") as dropped:
        gate.capture(stale_binding)
    assert dropped.value.code == "route_dropped"


def test_route_cleanup_is_safe_after_close():
    route = FakeLiveRoute(scripted_tokens=(1,))
    route.open()
    route.close()
    route.cleanup()
    route.cleanup()
    assert route.is_alive() is False
