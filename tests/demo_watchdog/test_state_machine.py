# SPDX-License-Identifier: AGPL-3.0-only
"""The four documented states, their legal transitions, and the decide() rule.

Design: DEMO-RESILIENCE-DESIGN.md §"Watchdog state machine".
"""
from __future__ import annotations

import pytest

from mycelium_demo_watchdog.probes import HealthReport
from mycelium_demo_watchdog.state import (
    LEGAL_TRANSITIONS,
    IllegalTransition,
    WatchdogState,
    check_transition,
    decide,
)


def report(device=True, process=True, origin=True):
    return HealthReport(
        device_reachable=device,
        local_process_alive=process,
        origin_healthy=origin,
        detail={},
    )


def test_the_four_documented_states_exist():
    assert {state.name for state in WatchdogState} >= {"PROBE", "HEALTHY", "DEGRADED", "RECOVER"}


def test_probe_is_read_only_and_never_mutates():
    """PROBE may only reach HEALTHY, DEGRADED, RECOVER or STOPPED."""
    assert LEGAL_TRANSITIONS[WatchdogState.PROBE] == frozenset(
        {WatchdogState.HEALTHY, WatchdogState.DEGRADED, WatchdogState.RECOVER, WatchdogState.STOPPED}
    )


def test_degraded_can_never_reach_a_healthy_claim_without_recovering():
    """A device drop must be paid for with a RECOVER; DEGRADED->HEALTHY is illegal."""
    assert WatchdogState.HEALTHY not in LEGAL_TRANSITIONS[WatchdogState.DEGRADED]
    assert LEGAL_TRANSITIONS[WatchdogState.DEGRADED] >= {WatchdogState.RECOVER, WatchdogState.DEGRADED}


def test_stopped_is_terminal():
    assert LEGAL_TRANSITIONS[WatchdogState.STOPPED] == frozenset()


def test_check_transition_accepts_the_documented_edges():
    for frm, to in (
        (WatchdogState.PROBE, WatchdogState.HEALTHY),
        (WatchdogState.PROBE, WatchdogState.DEGRADED),
        (WatchdogState.PROBE, WatchdogState.RECOVER),
        (WatchdogState.HEALTHY, WatchdogState.DEGRADED),
        (WatchdogState.DEGRADED, WatchdogState.RECOVER),
        (WatchdogState.RECOVER, WatchdogState.HEALTHY),
        (WatchdogState.RECOVER, WatchdogState.DEGRADED),
    ):
        check_transition(frm, to)


def test_check_transition_rejects_degraded_to_healthy():
    with pytest.raises(IllegalTransition):
        check_transition(WatchdogState.DEGRADED, WatchdogState.HEALTHY)


def test_check_transition_rejects_leaving_stopped():
    with pytest.raises(IllegalTransition):
        check_transition(WatchdogState.STOPPED, WatchdogState.PROBE)


def test_all_green_from_probe_reaches_healthy():
    assert decide(WatchdogState.PROBE, report()) is WatchdogState.HEALTHY


def test_device_offline_from_probe_reaches_degraded():
    assert decide(WatchdogState.PROBE, report(device=False)) is WatchdogState.DEGRADED


def test_device_offline_while_degraded_holds_degraded():
    """The hold: still offline after a drop must stay DEGRADED, never escalate."""
    assert decide(WatchdogState.DEGRADED, report(device=False)) is WatchdogState.DEGRADED


def test_device_return_while_degraded_requires_a_recover_not_a_healthy_claim():
    """Boot ids drift on sleep, so 'everything looks green' still means RECOVER."""
    assert decide(WatchdogState.DEGRADED, report()) is WatchdogState.RECOVER


def test_local_process_dead_with_device_up_reaches_recover():
    assert decide(WatchdogState.PROBE, report(process=False)) is WatchdogState.RECOVER


def test_origin_unhealthy_with_device_up_reaches_recover():
    assert decide(WatchdogState.HEALTHY, report(origin=False)) is WatchdogState.RECOVER


def test_local_dead_and_device_offline_still_holds_degraded():
    """A device that is offline is never a reason to touch the stage."""
    assert decide(WatchdogState.HEALTHY, report(device=False, process=False, origin=False)) is WatchdogState.DEGRADED


def test_unknown_device_is_never_healthy_and_holds_degraded():
    """Unknown device reachability must not be read as 'device present'."""
    unknown = HealthReport(device_reachable=None, local_process_alive=True, origin_healthy=True, detail={})
    assert unknown.all_green is False
    assert decide(WatchdogState.PROBE, unknown) is WatchdogState.DEGRADED


def test_unknown_origin_is_not_healthy():
    unknown = HealthReport(device_reachable=True, local_process_alive=True, origin_healthy=None, detail={})
    assert unknown.all_green is False
    assert decide(WatchdogState.HEALTHY, unknown) is WatchdogState.RECOVER


def test_health_report_names_its_unknowns():
    partial = HealthReport(device_reachable=None, local_process_alive=False, origin_healthy=None, detail={})
    assert partial.unknowns == ("device_reachable", "origin_healthy")
    assert partial.all_green is False


def test_stop_request_wins_from_every_non_terminal_state():
    for state in (WatchdogState.PROBE, WatchdogState.HEALTHY, WatchdogState.DEGRADED, WatchdogState.RECOVER):
        assert decide(state, report(), stop_requested=True) is WatchdogState.STOPPED


def test_decide_never_turns_a_failure_into_a_stop():
    """No probe outcome may terminate the watchdog: failure is DEGRADED or RECOVER only."""
    for device in (True, False, None):
        for process in (True, False, None):
            for origin in (True, False, None):
                target = decide(
                    WatchdogState.DEGRADED,
                    HealthReport(device, process, origin, {}),
                )
                assert target in (WatchdogState.DEGRADED, WatchdogState.RECOVER)
