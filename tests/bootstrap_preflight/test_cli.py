from __future__ import annotations

import json
import subprocess
from pathlib import Path


def test_cli_emits_canonical_json_for_failure_without_private_root(tmp_path: Path) -> None:
    private_marker = "private-bootstrap-root-marker"
    missing = tmp_path / private_marker
    command = [
        "python3.14",
        "-m",
        "mycelium_bootstrap_preflight",
        "--root",
        str(missing),
        "--json",
    ]

    first = subprocess.run(command, check=False, capture_output=True, text=True)
    second = subprocess.run(command, check=False, capture_output=True, text=True)

    assert first.returncode == 1
    assert second.returncode == 1
    assert first.stderr == second.stderr == ""
    assert first.stdout == second.stdout
    assert private_marker not in first.stdout
    report = json.loads(first.stdout)
    assert report["preflight_ready"] is False
    assert report["route_ready"] is False
    assert report["release_ready"] is False
    assert report["fresh_checkout_proven"] is False
    assert report["physical_qualification_evaluated"] is False
    assert first.stdout.endswith("\n")
    assert first.stdout == json.dumps(
        report,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ) + "\n"


def test_cli_requires_json_flag(tmp_path: Path) -> None:
    completed = subprocess.run(
        [
            "python3.14",
            "-m",
            "mycelium_bootstrap_preflight",
            "--root",
            str(tmp_path),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 2
    assert completed.stdout == ""
    assert "--json" in completed.stderr
