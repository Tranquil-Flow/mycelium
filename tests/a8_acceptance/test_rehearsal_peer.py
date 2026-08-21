# SPDX-License-Identifier: AGPL-3.0-or-later
"""Rehearsal-peer harness pins: fail-closed CLI, rehearsal labelling,
canonical-origin refusal, and no accidental claim vocabulary. The live
flow itself is exercised by scripts/a8_run_rehearsal_peer.py against the
public origin (rehearsal, never sealed)."""

from __future__ import annotations

from pathlib import Path
import subprocess

from scripts.a8_run_rehearsal_peer import (
    PEER_INCARNATION,
    REHEARSAL_LABEL,
)

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "a8_run_rehearsal_peer.py"
PYTHON = "/opt/homebrew/bin/python3.14"


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [PYTHON, str(SCRIPT), *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=60,
    )


def test_module_labels_rehearsal_and_never_qualification() -> None:
    assert "rehearsal" in REHEARSAL_LABEL
    assert "qualification" not in REHEARSAL_LABEL
    assert PEER_INCARNATION.startswith("a8-rehearsal")


def test_cli_fails_closed_without_arguments() -> None:
    completed = _run()
    assert completed.returncode == 2
    assert "usage:" in completed.stderr


def test_cli_rejects_non_canonical_origin() -> None:
    completed = _run(
        "--origin",
        "http://seed.example.test/path",
        "--bundle-file",
        str(ROOT / "nonexistent.json"),
    )
    assert completed.returncode == 2
    assert "invalid input" in completed.stderr


def test_cli_requires_bundle_file() -> None:
    completed = _run("--origin", "https://seed.example.test")
    assert completed.returncode == 2
    assert "bundle-file" in completed.stderr


def test_cli_missing_bundle_file_fails_bounded() -> None:
    completed = _run(
        "--origin",
        "https://seed.example.test",
        "--bundle-file",
        str(ROOT / "does-not-exist-bundle.json"),
    )
    assert completed.returncode != 0
