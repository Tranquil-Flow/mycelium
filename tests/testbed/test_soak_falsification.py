# SPDX-License-Identifier: AGPL-3.0-only
"""Mandatory falsification of the seeded control-plane soak (packet wp2-3, item 3).

A soak that has never been observed to fail is not evidence (``PLAN.md`` 5.1).
Every test here *injects* a deliberate defect through the soak's own seams and
asserts that the matching stable-named invariant code fires - and, just as
importantly, that the same observations with the defect removed do **not** fire,
so the check discriminates.

These run without the native sidecar: the inner runtime is a stand-in and the
ledger clock is driven directly, so the *judgement* is exercised in
milliseconds.  The same faults are re-run against the real
``harness.run_complete_request`` / real gateway stack by
``tests/testbed/test_soak_real_path.py`` (opt-in).
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from mycelium_testbed import soak


def _bounds() -> soak.SoakBounds:
    return soak.soak_bounds()


def _ledger_with_clock(start: float = 1_000.0) -> tuple[soak.Ledger, dict[str, float]]:
    """A ledger whose ``now()`` is driven by the test, not by the wall clock."""

    ledger = soak.Ledger()
    clock = {"now": start}
    ledger.now = lambda: clock["now"]  # type: ignore[method-assign]
    ledger.started_at = clock["now"]
    return ledger, clock


class _InnerRuntime:
    """Stand-in for the harness's deterministic stage runtime."""

    def __init__(self) -> None:
        self.calls: list[Any] = []

    def execute(self, item: Any) -> Any:
        self.calls.append(item)
        return SimpleNamespace(success=True, failure_reason=None, token_id=None)

    def execute_batch(self, batch: Any) -> tuple[Any, ...]:
        return tuple(self.execute(item) for item in batch.items)


class _Recorder:
    def __init__(self) -> None:
        self.events: list[tuple[Any, dict[str, Any]]] = []

    def event(self, kind: Any, **kwargs: Any) -> None:
        self.events.append((kind, kwargs))

    def failure(self, kind: Any, **kwargs: Any) -> None:
        self.event(kind, **kwargs)


def _item(*, request_id: str = "request-soak-0001-001") -> Any:
    return SimpleNamespace(
        idempotency_key="idempotency-1",
        request_id=request_id,
        placement_id="node-a-stage-000",
        phase="PREFILL",
        token_index=-1,
        hop_index=0,
        path_id="path-1",
        path_attempt=0,
    )


def _codes(ledger: soak.Ledger) -> set[str]:
    return {violation.code for violation in ledger.check_invariants(_bounds())}


def _session(**kwargs: Any) -> soak.SoakSession:
    return soak.SoakSession(seed=kwargs.pop("seed", 1), bounds=_bounds(), **kwargs)


def test_the_four_invariant_codes_are_the_packet_codes() -> None:
    assert soak.INVARIANT_CODES == (
        "at_most_one_native_execution_per_command",
        "nonterminal_row_not_retired_within_bound",
        "control_loop_death_while_membership_valid",
        "request_without_terminal_frame_within_budget",
    )


def test_double_execution_fires_the_at_most_one_native_execution_code() -> None:
    session = _session()
    runtime = soak.SoakStageRuntime(
        inner=_InnerRuntime(), recorder_for_placement=lambda placement: _Recorder()
    )

    with soak.activate_soak(session):
        runtime.execute(_item())

    # Control: one delivery, one execution, no violation.
    assert len(session.ledger.executions) == 1
    assert soak.INVARIANT_AT_MOST_ONE_NATIVE_EXECUTION_PER_COMMAND not in _codes(
        session.ledger
    )

    # Falsification: the same command is deliberately executed a second time.
    session.double_execution = True
    with soak.activate_soak(session):
        runtime.execute(_item())

    assert len(session.ledger.executions) == 3
    violations = session.ledger.check_invariants(_bounds())
    codes = {violation.code for violation in violations}
    assert soak.INVARIANT_AT_MOST_ONE_NATIVE_EXECUTION_PER_COMMAND in codes
    fired = [
        violation
        for violation in violations
        if violation.code == soak.INVARIANT_AT_MOST_ONE_NATIVE_EXECUTION_PER_COMMAND
    ]
    assert fired[0].bound_name == "maximum_native_executions_per_command"
    assert fired[0].bound_value == soak.MAXIMUM_NATIVE_EXECUTIONS_PER_COMMAND
    assert "native_executions=3" in fired[0].detail
    assert any(record.injected for record in session.ledger.executions)


def test_stalled_control_loop_fires_the_control_loop_code() -> None:
    bounds = _bounds()
    ledger, clock = _ledger_with_clock()
    subject = "gateway-worker:request-soak-0001-001"
    ledger.observe_loop(subject, progress=True, membership_valid=True, terminal_seen=False)

    # Control: a short silence inside the registry bound is not a death.
    clock["now"] += bounds.control_loop_stall_seconds / 2
    ledger.observe_loop(subject, progress=False, membership_valid=True, terminal_seen=False)
    assert soak.INVARIANT_CONTROL_LOOP_DEATH_WHILE_MEMBERSHIP_VALID not in _codes(ledger)

    # Falsification: the loop produces nothing for longer than the bound while the
    # membership still calls the subject valid.
    clock["now"] += bounds.control_loop_stall_seconds * 2
    ledger.observe_loop(
        subject, progress=False, membership_valid=True, terminal_seen=False, note="stall"
    )
    violations = ledger.check_invariants(bounds)
    codes = {violation.code for violation in violations}
    assert soak.INVARIANT_CONTROL_LOOP_DEATH_WHILE_MEMBERSHIP_VALID in codes
    fired = [
        violation
        for violation in violations
        if violation.code == soak.INVARIANT_CONTROL_LOOP_DEATH_WHILE_MEMBERSHIP_VALID
    ]
    assert fired[0].bound_name == "liveness_active_failure_detection_ms"
    assert fired[0].bound_value == bounds.control_loop_stall_seconds


def test_a_stalled_loop_with_a_terminal_frame_or_invalid_membership_is_not_a_death() -> None:
    bounds = _bounds()
    ledger, clock = _ledger_with_clock()
    subject = "gateway-worker:request-soak-0001-001"
    ledger.observe_loop(subject, progress=True, membership_valid=True, terminal_seen=False)
    clock["now"] += bounds.control_loop_stall_seconds * 3

    # The request reached its terminal: a silent loop is expected, not a death.
    ledger.observe_loop(subject, progress=False, membership_valid=True, terminal_seen=True)
    # And a subject whose membership is no longer valid is not "valid membership".
    ledger.observe_loop("peer-process:node-c", progress=True, membership_valid=False, terminal_seen=False)
    ledger.observe_loop("peer-process:node-c", progress=False, membership_valid=False, terminal_seen=False)

    codes = _codes(ledger)
    assert soak.INVARIANT_CONTROL_LOOP_DEATH_WHILE_MEMBERSHIP_VALID not in codes


def test_the_stall_injection_seam_stalls_inside_the_execution_path() -> None:
    """The stall is injected where a real stall lives: a runtime execution."""

    session = _session()
    session.stall_seconds = 0.0 if _bounds().control_loop_stall_seconds else 0.0
    inner = _InnerRuntime()
    runtime = soak.SoakStageRuntime(
        inner=inner, recorder_for_placement=lambda placement: _Recorder()
    )

    with soak.activate_soak(session):
        runtime.execute(_item())

    assert session.stall_latch.is_set()
    assert session.stall_complete.is_set()
    # The stall is one-shot: the next execution is not stalled again.
    assert session.stall_seconds is None
    assert len(inner.calls) == 1


def test_nonterminal_row_retired_within_bound_fires_that_code() -> None:
    bounds = _bounds()
    ledger, clock = _ledger_with_clock()
    ledger.admit_request(
        "request-soak-0001-001",
        bound_name="cancel_and_cleanup_budget_seconds",
        bound_seconds=bounds.request_terminal_cancelled_seconds,
    )

    # Control: retiring the row *after* the bound is the named retiring transition.
    clock["now"] += bounds.nonterminal_row_retirement_seconds * 2
    ledger.retire_row("request-soak-0001-001", note="late_retirement")
    assert soak.INVARIANT_NONTERMINAL_ROW_NOT_RETIRED_WITHIN_BOUND not in _codes(ledger)

    # Falsification: a fresh non-terminal row is retired inside its bound.
    ledger.admit_request(
        "request-soak-0001-002",
        bound_name="cancel_and_cleanup_budget_seconds",
        bound_seconds=bounds.request_terminal_cancelled_seconds,
    )
    clock["now"] += bounds.nonterminal_row_retirement_seconds / 2
    ledger.retire_row("request-soak-0001-002", note="absent_from_session_table")
    violations = ledger.check_invariants(bounds)
    codes = {violation.code for violation in violations}
    assert soak.INVARIANT_NONTERMINAL_ROW_NOT_RETIRED_WITHIN_BOUND in codes
    fired = [
        violation
        for violation in violations
        if violation.code == soak.INVARIANT_NONTERMINAL_ROW_NOT_RETIRED_WITHIN_BOUND
    ]
    assert fired[0].subject == "request-soak-0001-002"
    assert fired[0].bound_name == "cancel_and_cleanup_budget_seconds"


def test_a_soak_side_teardown_is_recorded_but_never_judged_as_a_retirement() -> None:
    bounds = _bounds()
    ledger, clock = _ledger_with_clock()
    ledger.admit_request(
        "request-soak-0001-001",
        bound_name="cancel_and_cleanup_budget_seconds",
        bound_seconds=bounds.request_terminal_cancelled_seconds,
    )
    clock["now"] += bounds.nonterminal_row_retirement_seconds / 2
    ledger.retire_row(
        "request-soak-0001-001", note="service_rebuilt_by_soak", kind="soak_teardown"
    )

    assert soak.INVARIANT_NONTERMINAL_ROW_NOT_RETIRED_WITHIN_BOUND not in _codes(ledger)


def test_request_without_terminal_frame_within_budget_fires_that_code() -> None:
    bounds = _bounds()
    ledger, clock = _ledger_with_clock()
    ledger.admit_request(
        "request-soak-0001-001",
        bound_name="cancel_and_cleanup_budget_seconds",
        bound_seconds=bounds.request_terminal_cancelled_seconds,
        bound_origin="last_progress",
    )
    ledger.mark_row_stall("request-soak-0001-001", note="terminal_frame_dropped")

    # Control: inside the budget nothing fires.
    clock["now"] += bounds.request_terminal_cancelled_seconds / 2
    assert soak.INVARIANT_REQUEST_WITHOUT_TERMINAL_FRAME_WITHIN_BUDGET not in _codes(ledger)

    # Falsification: the terminal frame never lands, past the budget.
    clock["now"] += bounds.request_terminal_cancelled_seconds * 2
    violations = ledger.check_invariants(bounds)
    codes = {violation.code for violation in violations}
    assert soak.INVARIANT_REQUEST_WITHOUT_TERMINAL_FRAME_WITHIN_BUDGET in codes
    fired = [
        violation
        for violation in violations
        if violation.code == soak.INVARIANT_REQUEST_WITHOUT_TERMINAL_FRAME_WITHIN_BUDGET
    ]
    assert fired[0].bound_name == "cancel_and_cleanup_budget_seconds"

    # Control: recording the terminal frame clears it.
    ledger.observe_row_terminal("request-soak-0001-001", kind="completed", code=None)
    clock["now"] += bounds.request_terminal_cancelled_seconds
    assert soak.INVARIANT_REQUEST_WITHOUT_TERMINAL_FRAME_WITHIN_BUDGET not in _codes(ledger)


def test_a_soak_closed_row_is_never_judged_as_a_missing_terminal() -> None:
    bounds = _bounds()
    ledger, clock = _ledger_with_clock()
    ledger.admit_request(
        "request-soak-0001-001",
        bound_name="cancel_and_cleanup_budget_seconds",
        bound_seconds=bounds.request_terminal_cancelled_seconds,
    )
    ledger.close_row_by_soak("request-soak-0001-001", note="episode_end")
    clock["now"] += bounds.request_terminal_cancelled_seconds * 10
    assert soak.INVARIANT_REQUEST_WITHOUT_TERMINAL_FRAME_WITHIN_BUDGET not in _codes(ledger)


def test_falsification_faults_are_never_drawn_by_the_random_schedule() -> None:
    drawn: set[soak.FaultKind] = set()
    for seed in range(1, 64):
        schedule = soak.FaultSchedule(seed=seed, events=64)
        drawn |= {event.kind for event in schedule.events_for("mailbox")}
        drawn |= {event.kind for event in schedule.events_for("transport")}

    assert not drawn & set(soak.FALSIFICATION_FAULTS)
    assert soak.FaultKind.DROP in drawn
    assert soak.FaultKind.DUPLICATE in drawn
    assert soak.FaultKind.DELAY in drawn
    assert soak.FaultKind.REORDER in drawn
    assert soak.FaultKind.RESTART_OWNER in drawn
    assert soak.FaultKind.RESTART_PEER in drawn


def test_a_forced_fault_must_be_a_falsification_fault() -> None:
    with pytest.raises(soak.SoakError):
        soak.FaultSchedule(
            seed=1, events=4, forced=(soak.ForcedFault(soak.FaultKind.DROP),)
        )


def test_a_forced_fault_is_pinned_to_its_step() -> None:
    schedule = soak.FaultSchedule(
        seed=1,
        events=6,
        forced=(
            soak.ForcedFault(soak.FaultKind.DOUBLE_EXECUTION, 2),
            soak.ForcedFault(soak.FaultKind.STALL_CONTROL_LOOP, 4),
        ),
    )
    events = schedule.events_for("mailbox")
    assert len(events) == 6
    assert events[2].kind is soak.FaultKind.DOUBLE_EXECUTION
    assert events[2].target is soak.FaultTarget.RUNTIME
    assert events[4].kind is soak.FaultKind.STALL_CONTROL_LOOP
    assert events[4].bound_name == "liveness_active_failure_detection_ms"
    assert events[4].bound_seconds == (
        _bounds().control_loop_stall_seconds + _bounds().control_loop_idle_seconds
    )
    for index, event in enumerate(events):
        if index in {2, 4}:
            continue
        assert (event.kind, event.target) in soak.RANDOM_SCHEDULE_FAULTS
