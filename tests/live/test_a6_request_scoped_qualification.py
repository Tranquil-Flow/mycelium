# SPDX-License-Identifier: AGPL-3.0-only
"""An admitted request keeps its qualification across a scoped peer loss.

In the production composition the request gateway revalidates every token and
the terminal against the route owner. A source peer's death makes the owner's
route not alive, so ``current()`` is None (correctly: nothing new may be
admitted), but the gateway then failed the very request A6 recovery was
continuing (``route_dropped``). Releasing that request cold-restarts the
degraded route before the gateway seals its terminal, so the completion was
also refused against the replacement's qualification (``path_changed``).
The gateway now revalidates an admitted request through the owner's
request-scoped authority; admission still reads ``current()``.
"""
from __future__ import annotations

import time

import pytest

from mycelium_request_gateway.contracts import AdmissionError, qualification_binding
from mycelium_request_gateway.qualification import CapturedQualification, QualificationGate
from tests.live.test_a6_cold_restart_owner_recovery import _KilledSourceRoute, _owner
from tests.request_gateway.test_core import _synthetic_qualification


class _ScopedSource:
    def __init__(self, current, scoped):
        self._current = current
        self._scoped = scoped
        self.asked: list[str] = []

    def current(self):
        return self._current

    def current_for_request(self, request_id):
        self.asked.append(request_id)
        return self._scoped


def test_gate_revalidates_an_admitted_request_through_its_scoped_authority() -> None:
    record = _synthetic_qualification()
    captured = CapturedQualification(qualification=record, binding=qualification_binding(record))
    gate = QualificationGate(_ScopedSource(None, record))
    gate.revalidate(captured, request_id="request-1")
    with pytest.raises(AdmissionError, match="route_dropped"):
        gate.revalidate(captured)  # no request scope: admission semantics
    with pytest.raises(AdmissionError, match="route_dropped"):
        gate.capture(captured.binding)
    blind = QualificationGate(_ScopedSource(None, None))
    with pytest.raises(AdmissionError, match="route_dropped"):
        blind.revalidate(captured, request_id="request-1")


def test_gate_without_a_scoped_source_reads_current() -> None:
    record = _synthetic_qualification()
    captured = CapturedQualification(qualification=record, binding=qualification_binding(record))

    class _Plain:
        def current(self):
            return record

    QualificationGate(_Plain()).revalidate(captured, request_id="request-1")


def _degraded_owner():
    route = _KilledSourceRoute()
    spare = _KilledSourceRoute()
    owner = _owner([route, spare], "qualification-1")
    owner.open()
    return owner, route, spare


def test_owner_scopes_a_degraded_route_to_the_requests_already_on_it() -> None:
    owner, route, _spare = _degraded_owner()
    assert owner.current_for_request("request-1") == "qualification-1"  # healthy
    with owner._lock:
        owner._request_routes["request-1"] = route
    route.source_killed = True
    assert owner.current() is None  # nothing new is admitted
    assert owner.current_for_request("request-1") == "qualification-1"
    assert owner.current_for_request("request-2") is None
    owner.drop()
    assert owner.current_for_request("request-1") is None


def test_owner_seals_a_released_request_against_the_route_it_ran_on() -> None:
    owner, route, spare = _degraded_owner()
    with owner._lock:
        owner._request_routes["request-1"] = route
    route.source_killed = True
    # Releasing the last request on the degraded route cold-restarts it.
    owner.release_request("request-1")
    deadline = time.monotonic() + 3.0
    while owner.public_status()["cold_route_restart"]["generation"] != 2:
        assert time.monotonic() < deadline
        time.sleep(0.01)
    assert spare.open_count == 1
    # The replacement publishes its own record (same object in this double);
    # the released request still reads the one it ran under, nothing else does.
    owner._qualification = "qualification-2"
    assert owner.current() == "qualification-2"
    assert owner.current_for_request("request-1") == "qualification-1"
    assert owner.current_for_request("request-3") == "qualification-2"


def test_settled_authority_is_bounded() -> None:
    owner, route, _spare = _degraded_owner()
    for index in range(300):
        with owner._lock:
            owner._request_routes[f"request-{index}"] = route
        owner.release_request(f"request-{index}")
    assert len(owner._settled_request_authority) == 256
    assert "request-0" not in owner._settled_request_authority
