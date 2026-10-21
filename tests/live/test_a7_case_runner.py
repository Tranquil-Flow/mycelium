# SPDX-License-Identifier: AGPL-3.0-only
"""A7 physical case-runner shell contract tests."""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
GATE_PATH = ROOT / "scripts" / "run_a7_kv_successor_gate.py"
RUNNER_PATH = ROOT / "scripts" / "run_a7_kv_successor_case_runner.py"

_SPEC = importlib.util.spec_from_file_location("run_a7_kv_successor_gate", GATE_PATH)
assert _SPEC is not None
assert _SPEC.loader is not None
gate = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(gate)

CANDIDATE = "a" * 40


def _write_plan(path: Path) -> dict[str, Any]:
    plan = gate.build_plan(CANDIDATE)
    path.write_text(json.dumps(plan, sort_keys=True), encoding="utf-8")
    return plan


def _note_path(record_out: Path) -> Path:
    return record_out.with_name(f"{record_out.stem}.note.json")


def _runner_env(*, fleet_grant: bool) -> dict[str, str]:
    env = os.environ.copy()
    env.pop("MYCELIUM_A7_PHYSICAL_CONFIG", None)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    if fleet_grant:
        env["MYCELIUM_A7_FLEET_GRANT"] = gate.EXACT_FLEET_GRANT
    else:
        env.pop("MYCELIUM_A7_FLEET_GRANT", None)
    return env


def _run_runner(case: str, plan_path: Path, record_out: Path, *, fleet_grant: bool = False) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            "-B",
            str(RUNNER_PATH),
            "--case",
            case,
            "--plan",
            str(plan_path),
            "--record-out",
            str(record_out),
        ],
        cwd=ROOT,
        env=_runner_env(fleet_grant=fleet_grant),
        text=True,
        capture_output=True,
        check=False,
    )


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def test_positive_without_fleet_grant_writes_closed_refusal_record_and_note(tmp_path: Path) -> None:
    plan_path = tmp_path / "plan.json"
    plan = _write_plan(plan_path)
    record_out = tmp_path / f"{gate.POSITIVE}.json"

    completed = _run_runner(gate.POSITIVE, plan_path, record_out, fleet_grant=False)

    assert completed.returncode == 0, completed.stderr
    record = _load_json(record_out)
    assert set(record) == set(gate._CASE_FIELDS)
    assert record["protocol"] == gate.CASE_PROTOCOL
    assert record["case"] == gate.POSITIVE
    assert record["candidate_commit"] == plan["candidate_commit"]
    assert record["plan_digest"] == plan["plan_digest"]
    assert record["ordinary_browser_request"] is True
    assert record["fault"] == "source_process_kill"
    assert record["outcome"] == "unknown"
    assert record["reason"] == "fleet_grant_missing"
    assert record["kv_continuity_claimed"] is False
    body = {key: value for key, value in record.items() if key != "evidence_digest"}
    assert record["evidence_digest"] == gate._sha(body)

    note = _load_json(_note_path(record_out))
    assert note["case"] == gate.POSITIVE
    assert note["plan_digest"] == plan["plan_digest"]
    assert note["record_evidence_digest"] == record["evidence_digest"]
    assert "completion-step residual race" in note["known_limit"]


def test_fleet_grant_reaches_the_physical_plug_in_and_gate_rejects_its_refusal(tmp_path: Path) -> None:
    plan_path = tmp_path / "plan.json"
    plan = _write_plan(plan_path)
    record_out = tmp_path / f"{gate.POSITIVE}.json"

    completed = _run_runner(gate.POSITIVE, plan_path, record_out, fleet_grant=True)

    assert completed.returncode != 0
    record = _load_json(record_out)
    # No measurement configuration: the plug-in refuses before any physical action.
    assert record["reason"] == "a7_measurement_config_missing"
    verdict = gate.evaluate(plan, {gate.POSITIVE: record})
    assert verdict["passed"] is False
    assert "outcome" in verdict["cases"][gate.POSITIVE]


def test_unknown_case_exits_2_and_writes_nothing(tmp_path: Path) -> None:
    plan_path = tmp_path / "plan.json"
    _write_plan(plan_path)
    record_out = tmp_path / "unknown.json"

    completed = _run_runner("not_a_real_a7_case", plan_path, record_out)

    assert completed.returncode == 2
    assert not record_out.exists()
    assert not _note_path(record_out).exists()


def test_pre_existing_record_exits_2_and_is_not_overwritten(tmp_path: Path) -> None:
    plan_path = tmp_path / "plan.json"
    _write_plan(plan_path)
    record_out = tmp_path / f"{gate.INCOMPATIBLE}.json"
    record_out.write_text("existing-record\n", encoding="utf-8")

    completed = _run_runner(gate.INCOMPATIBLE, plan_path, record_out)

    assert completed.returncode == 2
    assert record_out.read_text(encoding="utf-8") == "existing-record\n"
    assert not _note_path(record_out).exists()


def test_tampered_plan_digest_exits_2_and_writes_nothing(tmp_path: Path) -> None:
    plan_path = tmp_path / "plan.json"
    plan = _write_plan(plan_path)
    plan["plan_digest"] = "sha256:" + "0" * 64
    plan_path.write_text(json.dumps(plan, sort_keys=True), encoding="utf-8")
    record_out = tmp_path / f"{gate.LAGGING}.json"

    completed = _run_runner(gate.LAGGING, plan_path, record_out)

    assert completed.returncode == 2
    assert not record_out.exists()
    assert not _note_path(record_out).exists()


def test_known_limit_note_is_written_for_each_refusal_case(tmp_path: Path) -> None:
    plan_path = tmp_path / "plan.json"
    _write_plan(plan_path)

    for case in gate.CASES:
        record_out = tmp_path / f"{case}.json"
        completed = _run_runner(case, plan_path, record_out, fleet_grant=False)
        assert completed.returncode == 0, completed.stderr
        note = _load_json(_note_path(record_out))
        assert "completion-step residual race" in note["known_limit"]
