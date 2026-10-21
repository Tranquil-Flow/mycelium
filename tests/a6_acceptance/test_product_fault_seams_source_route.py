# SPDX-License-Identifier: AGPL-3.0-only
"""The source-failure measurement survives a route replacement.

On a physical serve the route owner cold-restarts a route that lost a peer as
soon as the request is released, which happens synchronously when recovery is
refused. The seam must read the source incident from the route that executed
the source attempt, not from the owner's replacement.
"""
from __future__ import annotations

import signal
from types import SimpleNamespace

import pytest

from mycelium_a6_fault_control import FaultRejected
from mycelium_a6_product_fault_seams import LiveProductFaultTarget, ProductCaseSeam
from tests.a6_acceptance.a6_measured_stack import CASE
from tests.a6_acceptance.test_product_fault_seams import _ScriptedRouter, _binding

_IDENTITY = {key: value for key, value in _binding().items() if key not in {
    "source_node_id", "source_attempt", "absolute_deadline_ms", "path_manifest_digest"}}


class _Owner:
    """A route owner whose current route is replaced when the source dies."""

    def __init__(self, source, replacement, *, raise_while_restarting=False) -> None:
        self.current = source
        self._replacement = replacement
        self._raise = raise_while_restarting

    def _current_route_or_raise(self):
        if self._raise and self.current is self._replacement:
            raise RuntimeError("cold_route_restart_in_progress")
        return self.current

    def replace(self):
        self.current = self._replacement


def _route(incidents, on_kill):
    def signal_group(signum):
        if signum == signal.SIGKILL:
            on_kill()
        return True

    process = SimpleNamespace(_signal_process_group=signal_group)
    return SimpleNamespace(
        _sessions={"node-0": SimpleNamespace(_process=process),
                   "node-2": SimpleNamespace(_process=process)},
        a4_scoped_runtime_incidents=lambda: list(incidents))


def _incident(**overrides):
    return {"incident_id": "i-1", "request_id": "r-1", "subject_id": "node-0",
            "scope": "request", "fatal_accepted": False, "reason": "node_process_exited",
            **_IDENTITY, **overrides}


@pytest.mark.parametrize("raise_while_restarting", [False, True])
def test_incident_is_read_from_the_source_route_after_the_owner_replaced_it(
        raise_while_restarting):
    router = _ScriptedRouter(_binding(), [
        {"recovery_phase": "active"},
        {"source_failure_observed": True, "retained": False,
         "recovery_failure_reason": "recovery_successor_unavailable"},
    ])
    source_incidents: list[dict] = []
    owner = None

    def kill():
        # The refused recovery settles, the gateway releases the request and
        # the owner replaces the degraded route before the seam polls.
        source_incidents.append(_incident())
        router.advance()
        owner.replace()

    source = _route(source_incidents, kill)
    replacement = _route([], lambda: None)
    owner = _Owner(source, replacement, raise_while_restarting=raise_while_restarting)
    target = LiveProductFaultTarget(
        router=router, route=owner,
        service=SimpleNamespace(terminal_event_count=lambda _rid: 0),
        wait_seconds=0.2, poll_seconds=0.001)
    seam = ProductCaseSeam(CASE, target)
    seam.arm(case_id=CASE)
    state = seam.apply(case_id=CASE)
    assert state["source_failure_observed"] is True
    assert state["actual_incident"]["incident_id"] == "i-1"
    (executed,) = target.executed
    assert executed.source_route is source


def test_arm_refuses_while_the_owner_has_no_current_route():
    router = _ScriptedRouter(_binding(), [{"recovery_phase": "active"}])

    class _Restarting:
        def _current_route_or_raise(self):
            raise RuntimeError("cold_route_restart_in_progress")

    target = LiveProductFaultTarget(
        router=router, route=_Restarting(),
        service=SimpleNamespace(terminal_event_count=lambda _rid: 0))
    with pytest.raises(FaultRejected, match="a6_fault_route_unavailable"):
        target.arm(CASE)
