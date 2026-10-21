# SPDX-License-Identifier: AGPL-3.0-only
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.run_a6_recovery_gate import REQUIRED_CASES, validate_plan

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "run_a6_recovery_gate.py"


def test_plan_declares_exact_physical_cases_without_execution() -> None:
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--plan"],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["protocol"] == "mycelium.a6_physical_recovery_plan.v1"
    assert report["executed"] is False
    assert report["passed"] is False
    assert report["fleet_contact_attempted"] is False
    assert report["required_cases"] == list(REQUIRED_CASES)
    assert report["expected_product_origin"] == "http://127.0.0.1:8787"
    assert report["expected_placements"] == ["node-0", "node-2", "node-3"]
    assert report["admissible_placements"] == [
        ["node-0", "node-2"], ["node-0", "node-2", "node-3"]]
    assert report["evidence_root_template"].endswith("/<source-digest>")
    physical = report["commands_after_grant"]["physical_gate"]
    browser = report["commands_after_grant"]["browser_gate"]
    assert "MYCELIUM_A6_FLEET_GRANT=FLEET GRANTED TO A6" in physical
    for required in (
        "MYCELIUM_A6_BROWSER_EVIDENCE=<owner-private-browser-evidence-file>",
        "MYCELIUM_A6_POSITIVE_FAULT_URL=http://127.0.0.1:8787/<positive-fault-control>",
        "MYCELIUM_A6_NEGATIVE_FAULT_URL=http://127.0.0.1:8787/<negative-fault-control>",
    ):
        assert required in browser


def test_live_execution_refuses_before_reading_plan_without_exact_grant(tmp_path: Path) -> None:
    missing = tmp_path / "does-not-exist.json"
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--execute", "--plan-file", str(missing)],
        cwd=ROOT,
        text=True,
        capture_output=True,
        env={**os.environ, "MYCELIUM_A6_FLEET_GRANT": "not granted"},
        check=False,
    )
    assert result.returncode == 2
    assert "explicit_fleet_grant_required" in result.stderr
    assert "does-not-exist" not in result.stderr


def test_validate_plan_requires_exact_case_set_and_absolute_private_root(tmp_path: Path) -> None:
    root = tmp_path / "evidence"
    plan = {
        "protocol": "mycelium.a6_physical_recovery_execution.v1",
        "source_digest": "sha256:" + "a" * 64,
        "spec_digest": "sha256:" + "b" * 64,
        "contract_digest": "sha256:" + "c" * 64,
        "product_origin": "http://127.0.0.1:8787",
        "operator_plan": str(tmp_path / "operator-plan.json"),
        "evidence_root": str(root),
        "expected_placements": ["node-0", "node-2"],
        "cases": [
            {
                "case_id": case,
                "argv": ["/usr/bin/true"],
                "evidence_file": str(root / f"{case}.json"),
            }
            for case in REQUIRED_CASES
        ],
        "cleanup_checks": [
            {"check_id": "local_product_port", "argv": ["/usr/bin/true"]},
            {"check_id": "physical_processes", "argv": ["/usr/bin/true"]},
            {"check_id": "reservations", "argv": ["/usr/bin/true"]},
        ],
    }
    with pytest.raises(ValueError, match="physical_case_runner_untrusted"):
        validate_plan(plan)

    runner = ROOT / "scripts" / "run_a6_recovery_case.py"
    plan["cases"] = [
        {
            "case_id": case,
            "argv": [
                sys.executable,
                str(runner),
                "--case",
                case,
                "--operator-plan",
                plan["operator_plan"],
            ],
            "evidence_file": str(root / f"{case}.json"),
        }
        for case in REQUIRED_CASES
    ]
    plan["cleanup_checks"] = [
        {
            "check_id": check_id,
            "argv": [
                sys.executable,
                str(runner),
                "--cleanup-check",
                check_id,
                "--operator-plan",
                plan["operator_plan"],
            ],
        }
        for check_id in ("local_product_port", "physical_processes", "reservations")
    ]
    validated = validate_plan(plan)
    assert tuple(case["case_id"] for case in validated["cases"]) == REQUIRED_CASES

    plan["cases"] = plan["cases"][:-1]
    with pytest.raises(ValueError, match="physical_case_set_mismatch"):
        validate_plan(plan)



def _positive_observation() -> dict[str, object]:
    return {
        "case_id": "ordinary_browser_source_failure_recovery",
        "executed": True,
        "ordinary_product_path": True,
        "browser_originated": True,
        "private_material_exposed": False,
        "source_failure_observed": True,
        "checkpoint_frozen": True,
        "successor_qualified": True,
        "source_fenced": True,
        "successor_prefill": True,
        "publication_generation_cas_count": 1,
        "duplicated_token_count": 0,
        "omitted_committed_token_count": 0,
        "late_source_publication_count": 0,
        "logical_stream_count": 1,
        "terminal_event_count": 1,
        "terminal_state": "completed",
        "process_cleanup_complete": True,
        "stale_reservation_count": 0,
    }


def test_trusted_case_runner_rejects_shallow_positive_assertion() -> None:
    from scripts.run_a6_recovery_case import validate_case_observation

    shallow = {
        "case_id": "ordinary_browser_source_failure_recovery",
        "executed": True,
        "result": "passed",
    }
    with pytest.raises(ValueError, match="physical_observation_invalid"):
        validate_case_observation(
            "ordinary_browser_source_failure_recovery",
            shallow,
        )


def test_trusted_case_runner_derives_positive_outcome_from_exact_observations() -> None:
    from scripts.run_a6_recovery_case import validate_case_observation

    validated = validate_case_observation(
        "ordinary_browser_source_failure_recovery",
        _positive_observation(),
    )
    assert validated["terminal_state"] == "completed"
    assert validated["logical_stream_count"] == 1


def test_trusted_case_runner_requires_terminal_blocked_without_fabricated_terminal() -> None:
    from scripts.run_a6_recovery_case import validate_case_observation

    observation = {
        "case_id": "cleanup_authority_failure",
        "executed": True,
        "ordinary_product_path": True,
        "private_material_exposed": False,
        "rejection_observed": True,
        "fail_closed": True,
        "terminal_state": "TERMINAL_BLOCKED",
        "terminal_event_count": 0,
        "unpublishable_suffix_discarded": True,
        "process_cleanup_complete": True,
        "stale_reservation_count": 0,
    }
    validated = validate_case_observation("cleanup_authority_failure", observation)
    assert validated["terminal_event_count"] == 0
