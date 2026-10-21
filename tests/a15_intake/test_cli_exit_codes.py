# SPDX-License-Identifier: AGPL-3.0-only
"""CLI exit-code contract for `scripts/a15_run_intake.py`.

The operator note documents exactly three exit codes:

* ``0`` -- intake executed and a report was produced (findings are the deliverable);
* ``2`` -- the intake refused to run (fail closed);
* ``3`` -- the report could not be written.

The A15 intake parent review found two refusal paths escaping the contract with a raw
traceback and exit 1: an unreadable ``--declaration`` path and an unparseable ``--now``.
Both are fail-closed *refusals*, so both must be exit 2 with a structured stderr token,
no traceback, and no report file. These tests pin that behaviour; they do not assert
anything about the report contents (covered in ``test_run_intake.py``).
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "a15_run_intake.py"
NOW = "2026-09-21T12:00:00Z"


def run_cli(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-B", str(SCRIPT), *args],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )


def refusal_payload(completed: subprocess.CompletedProcess) -> dict:
    assert "Traceback" not in completed.stderr, completed.stderr
    return json.loads(completed.stderr)


def test_missing_declaration_path_refuses_with_exit_2(tmp_path: Path):
    report = tmp_path / "report.json"
    completed = run_cli(
        "--declaration",
        str(tmp_path / "does-not-exist.json"),
        "--now",
        NOW,
        "--report",
        str(report),
    )
    assert completed.returncode == 2
    payload = refusal_payload(completed)
    assert payload["intake_refused"] is True
    assert payload["error"] == "a15_run_intake_input_unreadable"
    assert not report.exists()


def test_declaration_directory_refuses_with_exit_2(tmp_path: Path):
    report = tmp_path / "report.json"
    completed = run_cli("--declaration", str(tmp_path), "--now", NOW, "--report", str(report))
    assert completed.returncode == 2
    assert refusal_payload(completed)["intake_refused"] is True
    assert not report.exists()


def test_malformed_declaration_refuses_with_exit_2(tmp_path: Path):
    declaration = tmp_path / "declaration.json"
    declaration.write_text('{"protocol": "mycelium.a15_run_intake.v1",', encoding="utf-8")
    report = tmp_path / "report.json"
    completed = run_cli(
        "--declaration", str(declaration), "--now", NOW, "--report", str(report)
    )
    assert completed.returncode == 2
    assert refusal_payload(completed)["intake_refused"] is True
    assert not report.exists()


def test_unparseable_now_refuses_with_exit_2(tmp_path: Path):
    report = tmp_path / "report.json"
    completed = run_cli("--now", "banana", "--report", str(report))
    assert completed.returncode == 2
    payload = refusal_payload(completed)
    assert payload["error"] == "a15_run_intake_now_must_be_utc"
    assert payload["intake_refused"] is True
    assert not report.exists()


def test_naive_now_refuses_with_exit_2(tmp_path: Path):
    report = tmp_path / "report.json"
    completed = run_cli("--now", "2026-09-21T12:00:00", "--report", str(report))
    assert completed.returncode == 2
    assert refusal_payload(completed)["error"] == "a15_run_intake_now_must_be_utc"
    assert not report.exists()


def test_non_utc_offset_now_refuses_with_exit_2(tmp_path: Path):
    report = tmp_path / "report.json"
    completed = run_cli("--now", "2026-09-21T12:00:00+02:00", "--report", str(report))
    assert completed.returncode == 2
    assert refusal_payload(completed)["error"] == "a15_run_intake_now_must_be_utc"
    assert not report.exists()


def test_utc_offset_zero_now_is_accepted(tmp_path: Path):
    """Control: `+00:00` is UTC and must NOT be refused as a shape error."""
    declaration = tmp_path / "declaration.json"
    declaration.write_text(json.dumps(_minimal_declaration(tmp_path)), encoding="utf-8")
    report = tmp_path / "report.json"
    completed = run_cli(
        "--declaration",
        str(declaration),
        "--now",
        "2026-09-21T12:00:00+00:00",
        "--report",
        str(report),
    )
    assert completed.returncode == 0, completed.stderr
    assert report.is_file()


def test_unwritable_report_refuses_with_exit_3(tmp_path: Path):
    declaration = tmp_path / "declaration.json"
    declaration.write_text(json.dumps(_minimal_declaration(tmp_path)), encoding="utf-8")
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory", encoding="utf-8")
    completed = run_cli(
        "--declaration",
        str(declaration),
        "--now",
        NOW,
        "--report",
        str(blocker / "report.json"),
    )
    assert completed.returncode == 3
    assert "Traceback" not in completed.stderr


def _minimal_declaration(tmp_path: Path) -> dict:
    """A one-artifact declaration over bytes the test writes itself (no retained input)."""
    from mycelium_a15_run_intake import CLAIM_BOUNDARY, PROTOCOL, sha256_file

    run_dir = tmp_path / "run"
    run_dir.mkdir(exist_ok=True)
    artifact = run_dir / "gate-execution.json"
    artifact.write_text('{"protocol": "mycelium.pilot_readiness_gate.v1"}\n', encoding="utf-8")
    return {
        "protocol": PROTOCOL,
        "claim_boundary": CLAIM_BOUNDARY,
        "run_dir": str(run_dir),
        "evidence_roots": [],
        "repo_root": str(REPO_ROOT),
        "source": {"commit": "fb0314cf1cc5a57d14a2096226878267506b27f6", "branch": "test"},
        "artifacts": [
            {
                "artifact_id": "gate_execution",
                "path": "gate-execution.json",
                "declared_digest": sha256_file(artifact),
                "protocol": "mycelium.pilot_readiness_gate.v1",
                "provenance": "live",
                "required": True,
                "fresh_until": None,
            }
        ],
        "completion_records": [],
        "checklist": {
            "path": "docs/handover/mycelium-completion-checklist.v1.json",
            "declared_digest": None,
        },
    }
