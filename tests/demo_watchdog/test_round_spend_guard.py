# SPDX-License-Identifier: AGPL-3.0-only
"""The round-spend guard: the one invariant this leaf exists to prove.

Design: DEMO-RESILIENCE-DESIGN.md §"Discipline" — "Never burn a physical round
on a broken stage: pre-gates green before serve", plus the packet requirement
that "a RECOVER with failed pre-gates must NOT spend a round" and that
"a DEGRADED must NOT tear down".
"""
from __future__ import annotations

import json
import subprocess

import pytest

from mycelium_demo_watchdog.commands import RecoveryCommands, StepSpec
from mycelium_demo_watchdog.driver import (
    HELD,
    RECOVERED,
    RecoveryDriver,
    RecoveryOutcome,
)
from mycelium_demo_watchdog.ledger import RoundLedger, RoundLedgerError

from .conftest import Completes


def _ledger(tmp_path, *, spent=0, cap=2):
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


def _commands(**overrides):
    defaults = dict(
        stage_validate=("validate-stage",),
        bind_fresh=("bind-fresh",),
        stage_prepare=("stage-prepare",),
        stage_refresh=("stage-refresh",),
        serve=("serve",),
        observer=("observer",),
        verify=("verify",),
        pre_gates=tuple(
            StepSpec(f"gate-4{n}", (f"gate-4{n}",)) for n in (5, 6, 7, 8)
        ),
    )
    defaults.update(overrides)
    return RecoveryCommands(**defaults)


def _driver(tmp_path, *, plan, **command_overrides):
    ledger = _ledger(tmp_path, **command_overrides.pop("ledger_kwargs", {}))
    runner = Completes(plan)
    driver = RecoveryDriver(_commands(**command_overrides), ledger, runner=runner)
    return driver, ledger, runner


# ------------------------------------------------------------------ guard: no spend


def test_pre_gate_failure_spends_no_round_and_runs_no_serve(tmp_path):
    """The packet's required negative: red pre-gates => zero rounds, zero serve."""
    driver, ledger, runner = _driver(
        tmp_path,
        plan={
            "validate-stage": (0, "invalidated\n", ""),
            "gate-45": (0, "", ""),
            "gate-46": (1, "", "stack preflight red"),
        },
    )
    outcome = driver.recover()

    assert isinstance(outcome, RecoveryOutcome)
    assert outcome.state == HELD
    assert outcome.reason == "pre_gates_red"
    assert outcome.round_spent == 0
    assert ledger.spent() == 0
    assert ledger.starts() == []
    assert "serve" not in runner.key_calls
    assert "bind-fresh" not in runner.key_calls
    assert "stage-prepare" not in runner.key_calls
    assert outcome.pre_gates["gate-46"] is False


def test_unknown_pre_gate_is_not_green_and_spends_nothing(tmp_path):
    """Fail-closed: a gate that could not decide is not a green gate."""
    driver, ledger, runner = _driver(
        tmp_path,
        plan={
            "validate-stage": (0, "invalidated\n", ""),
            "gate-45": (0, "", ""),
            "gate-46": subprocess.TimeoutExpired(["gate-46"], 180),
        },
    )
    outcome = driver.recover()
    assert outcome.state == HELD
    assert outcome.round_spent == 0
    assert ledger.spent() == 0
    assert outcome.pre_gates["gate-46"] is None
    assert "serve" not in runner.key_calls


def test_green_pre_gates_exhausted_budget_spends_nothing(tmp_path):
    driver, ledger, runner = _driver(
        tmp_path,
        ledger_kwargs={"spent": 2, "cap": 2},
        plan={
            "validate-stage": (0, "invalidated\n", ""),
            **{f"gate-4{n}": (0, "", "") for n in (5, 6, 7, 8)},
        },
    )
    outcome = driver.recover()
    assert outcome.state == HELD
    assert outcome.reason == "round_budget_exhausted"
    assert outcome.round_spent == 0
    assert ledger.spent() == 2
    assert "serve" not in runner.key_calls


def test_unknown_stage_state_spends_nothing_and_serves_nothing(tmp_path):
    driver, ledger, runner = _driver(
        tmp_path,
        plan={"validate-stage": subprocess.TimeoutExpired(["validate-stage"], 180)},
    )
    outcome = driver.recover()
    assert outcome.state == HELD
    assert outcome.reason == "stage_state_unknown"
    assert outcome.stage_state == "unknown"
    assert outcome.round_spent == 0
    assert ledger.spent() == 0
    assert runner.calls and len(runner.calls) == 1


def test_a_validated_stage_is_reused_without_spending_a_round(tmp_path):
    driver, ledger, runner = _driver(
        tmp_path,
        plan={
            "validate-stage": (0, "valid\n", ""),
            **{f"gate-4{n}": (0, "", "") for n in (5, 6, 7, 8)},
            "serve": (0, "ok", ""),
            "observer": (0, "", ""),
            "verify": (0, "", ""),
        },
    )
    outcome = driver.recover()
    assert outcome.state == RECOVERED
    assert outcome.round_spent == 0
    assert ledger.spent() == 0
    assert outcome.reason == "recovered_reusing_validated_stage"
    assert "bind-fresh" not in runner.key_calls
    assert "serve" in runner.key_calls


def test_invalidated_stage_with_green_gates_spends_exactly_one_round(tmp_path):
    driver, ledger, runner = _driver(
        tmp_path,
        plan={
            "validate-stage": (0, "invalidated\n", ""),
            **{f"gate-4{n}": (0, "", "") for n in (5, 6, 7, 8)},
            "bind-fresh": (0, "", ""),
            "stage-prepare": (0, "", ""),
            "stage-refresh": (0, "", ""),
            "serve": (0, "", ""),
            "observer": (0, "", ""),
            "verify": (0, "", ""),
        },
    )
    outcome = driver.recover()
    assert outcome.state == RECOVERED
    assert outcome.round_spent == 1
    assert ledger.spent() == 1
    assert len(ledger.starts()) == 1
    assert outcome.reason == "recovered_with_new_stage"


def test_the_round_is_consumed_at_serve_launch_not_before(tmp_path):
    """Order: gates -> bind -> stage -> spend -> serve.  Nothing spends before bind."""
    driver, ledger, runner = _driver(
        tmp_path,
        plan={
            "validate-stage": (0, "invalidated\n", ""),
            **{f"gate-4{n}": (0, "", "") for n in (5, 6, 7, 8)},
            "bind-fresh": (0, "", ""),
            "stage-prepare": (1, "", "stage failed"),
        },
    )
    outcome = driver.recover()
    assert outcome.state in (HELD, "FAILED")
    assert outcome.round_spent == 0
    assert ledger.spent() == 0
    assert "serve" not in runner.key_calls


def test_a_failed_serve_still_counts_the_consumed_round_honestly(tmp_path):
    driver, ledger, runner = _driver(
        tmp_path,
        plan={
            "validate-stage": (0, "invalidated\n", ""),
            **{f"gate-4{n}": (0, "", "") for n in (5, 6, 7, 8)},
            "bind-fresh": (0, "", ""),
            "stage-prepare": (0, "", ""),
            "stage-refresh": (0, "", ""),
            "serve": (1, "", "serve died"),
        },
    )
    outcome = driver.recover()
    assert outcome.state == "FAILED"
    assert outcome.reason == "serve_failed"
    assert outcome.round_spent == 1
    assert ledger.spent() == 1


def test_recovery_never_re_spends_a_consumed_round(tmp_path):
    driver, ledger, runner = _driver(
        tmp_path,
        plan={
            "validate-stage": (0, "invalidated\n", ""),
            **{f"gate-4{n}": (0, "", "") for n in (5, 6, 7, 8)},
            "bind-fresh": (0, "", ""),
            "stage-prepare": (0, "", ""),
            "stage-refresh": (0, "", ""),
            "serve": (0, "", ""),
            "observer": (0, "", ""),
            "verify": (0, "", ""),
        },
    )
    first = driver.recover()
    second = driver.recover()
    assert first.round_spent == 1
    assert second.round_spent == 1
    assert ledger.spent() == 2
    assert len(ledger.starts()) == 2

    third = driver.recover()
    assert third.round_spent == 0
    assert third.state == HELD
    assert third.reason == "round_budget_exhausted"
    assert ledger.spent() == 2


# ----------------------------------------------------------------- ledger internals


def test_ledger_spend_refuses_past_cap(tmp_path):
    ledger = _ledger(tmp_path, spent=2, cap=2)
    assert ledger.has_headroom() is False
    with pytest.raises(RoundLedgerError) as excinfo:
        ledger.spend({"source_commit": "a" * 40})
    assert "round_budget_exhausted" in str(excinfo.value)
    assert ledger.spent() == 2


def test_ledger_spend_requires_a_source_commit(tmp_path):
    ledger = _ledger(tmp_path)
    with pytest.raises(RoundLedgerError) as excinfo:
        ledger.spend({})
    assert "round_record_incomplete" in str(excinfo.value)
    assert ledger.spent() == 0


def test_ledger_spend_is_atomic_and_private(tmp_path):
    ledger = _ledger(tmp_path)
    ledger.spend({"source_commit": "b" * 40, "receipt": "receipt.json"})
    mode = ledger.path.stat().st_mode & 0o777
    assert mode == 0o600
    document = json.loads(ledger.path.read_text())
    assert document["h4"]["spent"] == 1
    assert document["h4"]["starts"][0]["state"] == "conservatively_consumed_at_serve_launch"
    assert not list(tmp_path.glob("*.tmp"))


def test_ledger_never_touches_historical_hypotheses(tmp_path):
    ledger = _ledger(tmp_path)
    before = json.loads(ledger.path.read_text())["history"]
    ledger.spend({"source_commit": "c" * 40})
    assert json.loads(ledger.path.read_text())["history"] == before


def test_driver_exposes_no_teardown_step():
    """Structural proof that DEGRADED cannot tear down: there is nothing to call."""
    commands = RecoveryCommands()
    assert not hasattr(commands, "teardown")
    assert not hasattr(commands, "stop")
    assert set(RecoveryDriver.__dict__) and all(
        "teardown" not in name for name in dir(RecoveryDriver)
    )
