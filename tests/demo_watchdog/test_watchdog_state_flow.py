# SPDX-License-Identifier: AGPL-3.0-only
"""The watchdog loop: PROBE -> DEGRADED -> RECOVER, and the hold discipline.

Design: DEMO-RESILIENCE-DESIGN.md §"Watchdog state machine" — DEGRADED holds the
stage, never tears down, never raises; RECOVER runs the real recovery driver and
only ends HEALTHY when the driver actually recovered.

Only the three probes are scripted.  The recovery driver, the physical-round
ledger and the observer runner are the real objects, all sharing one scripted
command runner, so "no command was issued" is a real assertion about the
watchdog rather than an artifact of an unwired double.
"""
from __future__ import annotations

import json
import subprocess

import pytest

from mycelium_demo_watchdog.commands import RecoveryCommands
from mycelium_demo_watchdog.driver import HELD, RecoveryDriver
from mycelium_demo_watchdog.ledger import RoundLedger
from mycelium_demo_watchdog.state import WatchdogState
from mycelium_demo_watchdog.watchdog import DemoWatchdog, ObserverRunner, Transition, WatchdogConfig

from .conftest import Completes, ScriptedProbe, bad, ok, unknown


class Clock:
    """A monotonic/wall clock the test drives by hand."""

    def __init__(self, start=1_000_000.0):
        self.monotonic_now = 0.0
        self.wall_now = start
        self.slept: list[float] = []

    def monotonic(self) -> float:
        return self.monotonic_now

    def wall(self) -> float:
        return self.wall_now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.monotonic_now += seconds
        self.wall_now += seconds

    def advance(self, seconds: float) -> None:
        self.monotonic_now += seconds
        self.wall_now += seconds


GATES = tuple((f"gate-4{n}", (f"gate-4{n}",)) for n in (5, 6, 7, 8))


def make_plan(*, stage="valid", gates_ok=True, stage_prepare_fails=0, serve_fails=0, verify_ok=True, observer_ok=True):
    """A scripted command runner plan; returns (plan, counters)."""
    counters = {"serve": 0, "stage-prepare": 0}

    def plan(argv):
        key = argv[-1]
        if key == "validate-stage":
            return (0, f"{stage}\n", "")
        if key.startswith("gate-4"):
            return (0, "", "") if gates_ok else (1, "", "gate red")
        if key in counters:
            counters[key] += 1
        if key == "stage-prepare" and counters[key] <= stage_prepare_fails:
            return (1, "", "stage failed")
        if key == "serve" and counters[key] <= serve_fails:
            return (1, "", "serve died")
        if key == "verify" and not verify_ok:
            return (1, "", "verify red")
        if key == "observer" and not observer_ok:
            return (3, "", "renewal unproven")
        return (0, "", "")

    return plan, counters


def make_ledger(tmp_path, *, spent=0, cap=2):
    path = tmp_path / "ATTEMPT-LEDGER.json"
    path.write_text(
        json.dumps(
            {
                "h4": {"cap": cap, "spent": spent, "starts": []},
                "history": {"h2": {"cap": 2, "spent": 2}, "h3": {"cap": 2, "spent": 2}},
                "no_retry": True,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )
    return RoundLedger(path)


def commands():
    return RecoveryCommands(
        stage_validate=("validate-stage",),
        bind_fresh=("bind-fresh",),
        stage_prepare=("stage-prepare",),
        stage_refresh=("stage-refresh",),
        serve=("serve",),
        observer=("observer",),
        verify=("verify",),
        pre_gates=GATES,
    )


def build(tmp_path, *, device=(), process=(), origin=(), plan=None, spent=0, cap=2, config=None, default=None):
    runner = Completes(plan if plan is not None else make_plan()[0])
    ledger = make_ledger(tmp_path, spent=spent, cap=cap)
    driver = RecoveryDriver(commands(), ledger, runner=runner)
    clock = Clock()
    watchdog = DemoWatchdog(
        config or WatchdogConfig(probe_interval_seconds=30.0),
        device_probe=ScriptedProbe(results=list(device), default=default or ok()),
        process_probe=ScriptedProbe(results=list(process), default=default or ok()),
        origin_probe=ScriptedProbe(results=list(origin), default=default or ok()),
        driver=driver,
        observer=ObserverRunner(("observer",), runner=runner),
        clock=clock.monotonic,
        wall_clock=clock.wall,
        sleeper=clock.sleep,
    )
    return watchdog, driver, ledger, runner, clock


def keys(runner):
    return [argv[-1] for argv in runner.calls]


def test_initial_state_is_probe(tmp_path):
    watchdog, *_ = build(tmp_path)
    assert watchdog.state is WatchdogState.PROBE


def test_all_green_tick_records_probe_to_healthy(tmp_path):
    watchdog, driver, ledger, runner, clock = build(tmp_path)
    transition = watchdog.tick()
    assert isinstance(transition, Transition)
    assert (transition.frm, transition.to) == (WatchdogState.PROBE, WatchdogState.HEALTHY)
    assert keys(runner) == []
    assert ledger.spent() == 0


def test_healthy_tick_that_stays_green_records_no_new_transition(tmp_path):
    watchdog, *_ = build(tmp_path)
    watchdog.tick()
    assert watchdog.tick() is None
    assert watchdog.state is WatchdogState.HEALTHY
    assert len(watchdog.transitions) == 1


def test_device_drop_holds_degraded_and_issues_no_command_at_all(tmp_path):
    """The hold: offline device => DEGRADED, zero commands, zero teardown."""
    watchdog, driver, ledger, runner, clock = build(
        tmp_path,
        device=[ok(), bad("ssh_unreachable"), bad("ssh_unreachable")],
    )
    watchdog.tick()
    first = watchdog.tick()
    second = watchdog.tick()

    assert (first.frm, first.to) == (WatchdogState.HEALTHY, WatchdogState.DEGRADED)
    assert second is None
    assert watchdog.state is WatchdogState.DEGRADED
    assert watchdog.degraded_ticks == 2
    assert runner.calls == []
    assert ledger.spent() == 0
    assert watchdog.destructive_invocations == 0


def test_degraded_never_raises_when_every_probe_is_red_or_unknown(tmp_path):
    watchdog, driver, ledger, runner, clock = build(
        tmp_path,
        device=[bad(), unknown(), bad(), unknown(), bad()],
        process=[bad(), unknown(), bad(), unknown(), bad()],
        origin=[bad(), unknown(), bad(), unknown(), bad()],
    )
    seen = [watchdog.tick() for _ in range(5)]
    assert watchdog.state is WatchdogState.DEGRADED
    assert all(item is None or item.to is WatchdogState.DEGRADED for item in seen)
    assert runner.calls == []
    assert ledger.spent() == 0


def test_full_probe_degraded_recover_sequence(tmp_path):
    """The packet's required proof: PROBE -> DEGRADED -> RECOVER -> HEALTHY."""
    watchdog, driver, ledger, runner, clock = build(
        tmp_path,
        device=[ok(), bad("ssh_unreachable")],
    )
    seen = watchdog.run(max_ticks=2)
    assert [(item.frm.name, item.to.name) for item in seen] == [
        ("PROBE", "HEALTHY"),
        ("HEALTHY", "DEGRADED"),
    ]
    assert runner.calls == []

    returned = watchdog.run(max_ticks=1)  # device is back (default green)
    assert [(item.frm.name, item.to.name) for item in returned] == [
        ("DEGRADED", "RECOVER"),
        ("RECOVER", "HEALTHY"),
    ]
    assert watchdog.state is WatchdogState.HEALTHY
    assert ledger.spent() == 0, "a validated stage is reused, never re-charged"
    assert keys(runner) == [
        "validate-stage", "gate-45", "gate-46", "gate-47", "gate-48",
        "serve", "observer", "verify",
    ]


def test_recover_failure_degrades_and_retries_on_the_next_tick(tmp_path):
    plan, counters = make_plan(serve_fails=1)
    watchdog, driver, ledger, runner, clock = build(
        tmp_path,
        device=[bad("ssh_unreachable")],
        plan=plan,
    )
    watchdog.tick()  # PROBE -> DEGRADED
    watchdog.tick()  # DEGRADED -> RECOVER -> DEGRADED (serve failed)
    assert watchdog.state is WatchdogState.DEGRADED
    assert keys(runner).count("serve") == 1

    retried = watchdog.run(max_ticks=1)
    assert [(item.frm.name, item.to.name) for item in retried] == [
        ("DEGRADED", "RECOVER"),
        ("RECOVER", "HEALTHY"),
    ]
    assert keys(runner).count("serve") == 2


def test_recover_is_not_attempted_while_the_device_is_still_away(tmp_path):
    watchdog, driver, ledger, runner, clock = build(
        tmp_path,
        device=[bad("ssh_unreachable")],
        default=bad("ssh_unreachable"),
    )
    watchdog.run(max_ticks=4)
    assert watchdog.state is WatchdogState.DEGRADED
    assert runner.calls == []


def test_recover_holds_when_pre_gates_are_red_and_spends_nothing(tmp_path):
    """End-to-end: the guard survives the whole watchdog loop, not just the driver."""
    plan, _ = make_plan(stage="invalidated", gates_ok=False)
    watchdog, driver, ledger, runner, clock = build(
        tmp_path,
        origin=[ok(), bad("route_not_ready")],
        plan=plan,
    )
    seen = watchdog.run(max_ticks=2)
    assert [(item.frm.name, item.to.name) for item in seen] == [
        ("PROBE", "HEALTHY"),
        ("HEALTHY", "RECOVER"),
        ("RECOVER", "DEGRADED"),
    ]
    assert watchdog.state is WatchdogState.DEGRADED
    assert ledger.spent() == 0
    assert ledger.starts() == []
    assert "serve" not in keys(runner)
    assert "bind-fresh" not in keys(runner)


def test_origin_becomes_unhealthy_with_device_up_triggers_recover(tmp_path):
    watchdog, driver, ledger, runner, clock = build(
        tmp_path,
        origin=[ok(), bad("route_not_ready")],
    )
    seen = watchdog.run(max_ticks=2)
    assert [(item.frm.name, item.to.name) for item in seen] == [
        ("PROBE", "HEALTHY"),
        ("HEALTHY", "RECOVER"),
        ("RECOVER", "HEALTHY"),
    ]


def test_invalidated_stage_recover_spends_exactly_one_round_through_the_loop(tmp_path):
    watchdog, driver, ledger, runner, clock = build(
        tmp_path,
        device=[bad("ssh_unreachable")],
        plan=make_plan(stage="invalidated")[0],
    )
    watchdog.tick()  # device drops -> DEGRADED
    watchdog.run(max_ticks=1)  # device back -> RECOVER (stage invalidated)
    assert watchdog.state is WatchdogState.HEALTHY
    assert ledger.spent() == 1
    assert len(ledger.starts()) == 1
    assert keys(runner) == [
        "validate-stage", "gate-45", "gate-46", "gate-47", "gate-48",
        "bind-fresh", "stage-prepare", "stage-refresh", "serve", "observer", "verify",
    ]


# -------------------------------------------------------------------- the observer


def test_observer_runs_only_when_the_renewal_interval_elapses(tmp_path):
    watchdog, driver, ledger, runner, clock = build(tmp_path)
    watchdog.tick()
    assert keys(runner) == [], "healthy but not due yet"

    clock.advance(55 * 60)
    watchdog.tick()
    assert keys(runner) == ["observer"]
    assert watchdog.renewal_fresh is True
    assert watchdog.last_renewal_wall == pytest.approx(clock.wall_now)

    clock.advance(60)
    watchdog.tick()
    assert keys(runner) == ["observer"], "not due again yet"


def test_renewal_due_uses_the_configured_interval_and_surfaces_a_missed_window(tmp_path):
    watchdog, *_ = build(tmp_path)
    watchdog.tick()
    start = watchdog.serve_start_wall
    assert watchdog.renewal_due(start + 10) == "not_due"
    assert watchdog.renewal_due(start + 75 * 60 - 1) == "due"
    assert watchdog.renewal_due(start + 76 * 60) == "window_missed"


def test_a_failed_renewal_never_fakes_freshness_and_never_exits(tmp_path):
    watchdog, driver, ledger, runner, clock = build(
        tmp_path, plan=make_plan(observer_ok=False)[0]
    )
    watchdog.tick()
    clock.advance(55 * 60)
    watchdog.tick()
    assert keys(runner) == ["observer"]
    assert watchdog.renewal_fresh is False
    assert watchdog.last_renewal_wall is None
    assert watchdog.last_renewal_reason == "renewal_unproven"
    assert watchdog.state is WatchdogState.HEALTHY


def test_observer_runner_is_bounded_and_never_raises():
    class TimingOut:
        def __call__(self, argv, **kwargs):
            raise subprocess.TimeoutExpired(argv, 300)

    assert ObserverRunner(("observer",), runner=TimingOut()).run() is False


def test_observer_runner_reports_success_only_on_exit_zero():
    assert ObserverRunner(("observer",), runner=Completes()).run() is True
    assert ObserverRunner(("observer",), runner=Completes({"observer": (3, "", "")})).run() is False


def test_missed_deadline_skips_the_observer_and_records_why(tmp_path):
    watchdog, driver, ledger, runner, clock = build(tmp_path)
    watchdog.tick()
    clock.advance(80 * 60)
    watchdog.tick()
    assert keys(runner) == []
    assert watchdog.renewal_fresh is False
    assert watchdog.last_renewal_reason == "renewal_window_missed"


def test_observer_is_never_run_from_a_degraded_hold(tmp_path):
    watchdog, driver, ledger, runner, clock = build(
        tmp_path,
        device=[bad("ssh_unreachable")],
        default=bad("ssh_unreachable"),
    )
    clock.advance(60 * 60)
    watchdog.run(max_ticks=2)
    assert watchdog.state is WatchdogState.DEGRADED
    assert runner.calls == []


# ------------------------------------------------------------------------ shutdown


def test_stop_request_stops_from_degraded_without_touching_the_stage(tmp_path):
    watchdog, driver, ledger, runner, clock = build(tmp_path, device=[bad("ssh_unreachable")])
    watchdog.tick()
    assert watchdog.state is WatchdogState.DEGRADED
    watchdog.request_stop()
    seen = watchdog.tick()
    assert seen.to is WatchdogState.STOPPED
    assert watchdog.state is WatchdogState.STOPPED
    assert runner.calls == []
    assert watchdog.destructive_invocations == 0


def test_run_honours_the_tick_interval_and_the_tick_budget(tmp_path):
    watchdog, driver, ledger, runner, clock = build(tmp_path)
    watchdog.run(max_ticks=3)
    assert clock.slept == [30.0, 30.0]

    watchdog.request_stop()
    watchdog.run(max_ticks=3)
    assert clock.slept == [30.0, 30.0]


def test_a_whole_scripted_soak_never_spends_or_raises(tmp_path):
    """Long scripted flapping: one drop, one recovery, no round, no raise."""
    device = [ok()] + [bad("ssh_unreachable")] * 3 + [ok()] * 2
    watchdog, driver, ledger, runner, clock = build(tmp_path, device=device)
    transitions = watchdog.run(max_ticks=6)
    names = [(item.frm.name, item.to.name) for item in transitions]
    assert names[0] == ("PROBE", "HEALTHY")
    assert ("HEALTHY", "DEGRADED") in names
    assert ("DEGRADED", "RECOVER") in names
    assert names[-1] == ("RECOVER", "HEALTHY")
    assert ledger.spent() == 0
    assert watchdog.destructive_invocations == 0
    assert watchdog.state is WatchdogState.HEALTHY
