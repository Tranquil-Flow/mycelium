from __future__ import annotations

import importlib
import json
from pathlib import Path

import pytest


PROTOCOL = "mycelium.release_doctor_preflight.v1"
REQUIRED_FILES = (
    "contracts/contract-manifest.v1.json",
    "scripts/contract_audit.py",
    "native/iroh_transport/Cargo.lock",
    "ui/web/package-lock.json",
    "docs/automation/2026-07-18-manual-driver-handover.md",
)


def _doctor():
    return importlib.import_module("mycelium_demo.doctor")


def _fixture_repo(root: Path) -> Path:
    repo = root / "repo"
    for relative in REQUIRED_FILES:
        path = repo / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("fixture\n", encoding="utf-8")
    return repo


def test_preflight_reports_honest_non_release_boundary(tmp_path: Path) -> None:
    doctor = _doctor()
    repo = _fixture_repo(tmp_path)
    report = doctor.run_preflight(
        repo_root=repo,
        state_dir=tmp_path / "state",
        commands=("python3.14", "cargo"),
        required_files=REQUIRED_FILES,
        ports=(41001, 41002),
        which=lambda command: f"/tools/{command}",
        port_available=lambda port: True,
    )

    assert report["protocol"] == PROTOCOL
    assert report["local_preflight_ok"] is True
    assert report["route_ready"] is False
    assert report["release_ready"] is False
    assert report["qualification_evaluated"] is False
    assert report["claim_boundary"] == (
        "read-only local environment preflight only; no process start, provisioning, "
        "inference, transport, qualification, or physical-host evidence"
    )
    assert report["release_blockers"] == [
        "RouteQualificationV1 is not consumed by this preflight tranche",
        "physical two-host inference and transport are not evaluated",
        "request streaming, live Observatory, recovery, and evidence sealing are not evaluated",
    ]
    assert all(check["ok"] for check in report["checks"])
    assert json.loads(doctor.canonical_json(report))["protocol"] == PROTOCOL


def test_state_directory_inside_source_fails_closed(tmp_path: Path) -> None:
    doctor = _doctor()
    repo = _fixture_repo(tmp_path)
    report = doctor.run_preflight(
        repo_root=repo,
        state_dir=repo / ".state",
        commands=(),
        required_files=REQUIRED_FILES,
        ports=(),
    )

    assert report["local_preflight_ok"] is False
    check = next(item for item in report["checks"] if item["name"] == "state_directory_outside_source")
    assert check == {
        "name": "state_directory_outside_source",
        "ok": False,
        "detail": "state directory must resolve outside repository root",
    }


def test_missing_command_and_occupied_port_are_reported_without_secret_state(tmp_path: Path) -> None:
    doctor = _doctor()
    repo = _fixture_repo(tmp_path)
    report = doctor.run_preflight(
        repo_root=repo,
        state_dir=tmp_path / "state",
        commands=("python3.14", "cargo"),
        required_files=REQUIRED_FILES,
        ports=(41001, 41002),
        which=lambda command: "/tools/python3.14" if command == "python3.14" else None,
        port_available=lambda port: port == 41001,
    )

    assert report["local_preflight_ok"] is False
    assert {
        (check["name"], check["ok"], check["detail"])
        for check in report["checks"]
        if not check["ok"]
    } == {
        ("command:cargo", False, "required command not found: cargo"),
        ("port:41002", False, "local TCP port is unavailable: 41002"),
    }
    rendered = doctor.canonical_json(report).lower()
    assert "token" not in rendered
    assert "credential" not in rendered
    assert "api_key" not in rendered


def test_required_file_symlink_escape_fails_closed(tmp_path: Path) -> None:
    doctor = _doctor()
    repo = _fixture_repo(tmp_path)
    outside = tmp_path / "outside.json"
    outside.write_text("outside\n", encoding="utf-8")
    escaped = repo / REQUIRED_FILES[0]
    escaped.unlink()
    escaped.symlink_to(outside)

    report = doctor.run_preflight(
        repo_root=repo,
        state_dir=tmp_path / "state",
        commands=(),
        required_files=REQUIRED_FILES,
        ports=(),
    )

    assert report["local_preflight_ok"] is False
    check = next(item for item in report["checks"] if item["name"] == f"file:{REQUIRED_FILES[0]}")
    assert check["ok"] is False
    assert check["detail"] == "required file escapes repository root"


def test_doctor_cli_emits_canonical_json_and_uses_local_preflight_exit(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    cli = importlib.import_module("mycelium_demo.cli")
    repo = _fixture_repo(tmp_path)

    exit_code = cli.main(
        [
            "doctor",
            "--repo-root",
            str(repo),
            "--state-dir",
            str(tmp_path / "state"),
        ],
        which=lambda command: f"/tools/{command}",
    )

    output = capsys.readouterr().out
    report = json.loads(output)
    assert exit_code == 0
    assert output == json.dumps(report, sort_keys=True, separators=(",", ":")) + "\n"
    assert report["local_preflight_ok"] is True
    assert report["release_ready"] is False


def test_doctor_runbook_preserves_claim_boundary() -> None:
    runbook = Path("docs/demo/release-doctor-preflight.md").read_text(encoding="utf-8")

    for required in (
        "python3.14 -m mycelium_demo doctor",
        "route_ready=false",
        "release_ready=false",
        "does not start processes",
        "does not perform physical qualification",
        "No package installation",
    ):
        assert required in runbook
