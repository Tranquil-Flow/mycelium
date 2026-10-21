# SPDX-License-Identifier: AGPL-3.0-only
"""The ``--dry-run`` acceptance: full PROBE->DEGRADED->RECOVER against mocks.

Design deliverable 3: "A ``--dry-run`` acceptance that exercises
PROBE->DEGRADED->RECOVER against mocks and proves the round-spend guard (no round
spent on a broken stage)."  No network, no hardware, no live service: the dry run
writes into a caller-supplied temporary directory and touches nothing else.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

import mycelium_demo_watchdog
from mycelium_demo_watchdog.dryrun import PROTOCOL, run_dry_run

REPO_ROOT = Path(mycelium_demo_watchdog.__file__).resolve().parents[1]
FORBIDDEN_VERBS = ("teardown", "kill", "SIGTERM", "SIGKILL", "rmtree", "stop_service")


@pytest.fixture
def receipt(tmp_path):
    code = run_dry_run(out_dir=tmp_path)
    assert code == 0, "the dry run must report its own invariants as satisfied"
    return json.loads((tmp_path / "DRY-RUN.json").read_text())


def scenario(receipt, name):
    for entry in receipt["scenarios"]:
        if entry["name"] == name:
            return entry
    raise AssertionError(f"scenario {name!r} missing from {[e['name'] for e in receipt['scenarios']]}")


def transitions(entry):
    return [(item["frm"], item["to"]) for item in entry["transitions"]]


def test_dry_run_writes_a_protocol_receipt(tmp_path):
    assert run_dry_run(out_dir=tmp_path) == 0
    document = json.loads((tmp_path / "DRY-RUN.json").read_text())
    assert document["protocol"] == PROTOCOL
    assert document["invariants"]["all_scenarios_ok"] is True


def test_dry_run_exercises_the_full_probe_degraded_recover_path(receipt):
    entry = scenario(receipt, "device_drop_recover")
    assert transitions(entry) == [
        ("PROBE", "HEALTHY"),
        ("HEALTHY", "DEGRADED"),
        ("DEGRADED", "RECOVER"),
        ("RECOVER", "HEALTHY"),
    ]
    assert entry["final_state"] == "HEALTHY"


def test_dry_run_proves_degraded_issues_no_command(receipt):
    entry = scenario(receipt, "device_drop_recover")
    assert entry["checks"]["degraded_issued_no_command"] is True
    hold = [item for item in entry["commands"] if item["phase"] == "DEGRADED"]
    assert hold == []
    assert entry["checks"]["no_destructive_verb"] is True


def test_dry_run_proves_red_pre_gates_spend_no_round(receipt):
    entry = scenario(receipt, "red_pre_gates")
    assert entry["checks"]["round_spent"] == 0
    assert entry["ledger_spent_after"] == entry["ledger_spent_before"]
    assert entry["checks"]["serve_not_reached"] is True
    assert "serve" not in [item["name"] for item in entry["commands"]]
    assert transitions(entry)[-1] == ("RECOVER", "DEGRADED")


def test_dry_run_proves_an_exhausted_budget_spends_no_round(receipt):
    entry = scenario(receipt, "budget_exhausted")
    assert entry["checks"]["round_spent"] == 0
    assert entry["checks"]["serve_not_reached"] is True
    assert entry["ledger_spent_before"] == entry["ledger_spent_after"] == 2


def test_dry_run_proves_an_unknown_stage_spends_no_round(receipt):
    entry = scenario(receipt, "stage_state_unknown")
    assert entry["checks"]["round_spent"] == 0
    assert entry["checks"]["serve_not_reached"] is True


def test_dry_run_proves_an_invalidated_stage_spends_exactly_one_round(receipt):
    entry = scenario(receipt, "invalidated_stage_restaged")
    assert entry["checks"]["round_spent"] == 1
    assert entry["ledger_spent_after"] == entry["ledger_spent_before"] + 1
    assert transitions(entry)[-1] == ("RECOVER", "HEALTHY")


def test_dry_run_proves_a_reused_stage_spends_nothing(receipt):
    entry = scenario(receipt, "validated_stage_reused")
    assert entry["checks"]["round_spent"] == 0
    assert entry["ledger_spent_after"] == 0


def test_dry_run_receipt_holds_the_guard_invariants(receipt):
    invariants = receipt["invariants"]
    assert invariants["round_spend_guard_holds"] is True
    assert invariants["degraded_holds_without_command"] is True
    assert invariants["unknown_never_healthy"] is True
    assert invariants["no_destructive_verb"] is True


def test_dry_run_receipt_contains_no_destructive_verb(receipt):
    text = json.dumps(receipt).lower()
    for verb in FORBIDDEN_VERBS:
        assert verb not in text, verb


def test_dry_run_is_idempotent(tmp_path):
    first = run_dry_run(out_dir=tmp_path)
    second = run_dry_run(out_dir=tmp_path)
    assert (first, second) == (0, 0)
    document = json.loads((tmp_path / "DRY-RUN.json").read_text())
    for entry in document["scenarios"]:
        assert entry["ok"] is True


def test_dry_run_touches_only_its_own_output_directory(tmp_path):
    out = tmp_path / "out"
    assert run_dry_run(out_dir=out) == 0
    assert sorted(path.name for path in tmp_path.iterdir()) == ["out"]
    assert sorted(path.name for path in out.iterdir()) == ["DRY-RUN.json"]


def test_cli_dry_run_exits_zero_and_writes_the_receipt(tmp_path):
    completed = subprocess.run(
        [sys.executable, "-B", "-m", "mycelium_demo_watchdog", "--dry-run", "--out", str(tmp_path)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert completed.returncode == 0, completed.stderr[-2000:]
    assert (tmp_path / "DRY-RUN.json").is_file()


def test_cli_refuses_to_run_live_without_arm(tmp_path):
    completed = subprocess.run(
        [sys.executable, "-B", "-m", "mycelium_demo_watchdog"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert completed.returncode == 3, "a live run is deploy-gated; it must refuse without --arm"
    assert "arm" in (completed.stdout + completed.stderr).lower()


def test_cli_reports_usage_for_an_unknown_flag():
    completed = subprocess.run(
        [sys.executable, "-B", "-m", "mycelium_demo_watchdog", "--nonsense"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert completed.returncode == 2
