from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from conftest import ROOT, canonical_bytes


def test_cli_reads_one_plan_and_emits_byte_identical_canonical_json(
    plan: dict[str, object], tmp_path: Path
) -> None:
    path = tmp_path / "operator-plan.json"
    path.write_bytes(canonical_bytes(plan))
    command = [sys.executable, "-m", "mycelium_physical_preflight", str(path)]

    first = subprocess.run(command, cwd=ROOT, check=False, capture_output=True)
    second = subprocess.run(command, cwd=ROOT, check=False, capture_output=True)

    assert first.returncode == 0
    assert second.returncode == 0
    assert first.stdout == second.stdout
    assert first.stderr == second.stderr == b""
    payload = json.loads(first.stdout)
    assert first.stdout == json.dumps(
        payload,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode() + b"\n"
    assert payload["physical_qualification_executed"] is False
    assert payload["route_ready"] is False
    assert payload["release_ready"] is False
    assert str(ROOT).encode() not in first.stdout


def test_cli_fails_closed_without_echoing_input_path_or_values(tmp_path: Path) -> None:
    secret_location = tmp_path / "do-not-echo-this-location.json"
    secret_location.write_bytes(b'{"bad":true}')

    completed = subprocess.run(
        [sys.executable, "-m", "mycelium_physical_preflight", str(secret_location)],
        cwd=ROOT,
        check=False,
        capture_output=True,
    )

    assert completed.returncode == 2
    assert completed.stderr == b""
    assert str(secret_location).encode() not in completed.stdout
    error = json.loads(completed.stdout)
    assert error["ok"] is False
    assert set(error["error"]) == {"code", "pointer"}


def test_cli_rejects_symlink_input_without_following_it(plan: dict[str, object], tmp_path: Path) -> None:
    target = tmp_path / "plan.json"
    target.write_bytes(canonical_bytes(plan))
    link = tmp_path / "linked-plan.json"
    try:
        link.symlink_to(target)
    except OSError:
        return

    completed = subprocess.run(
        [sys.executable, "-m", "mycelium_physical_preflight", str(link)],
        cwd=ROOT,
        check=False,
        capture_output=True,
    )

    assert completed.returncode == 2
    assert json.loads(completed.stdout)["error"]["code"] == "input_symlink"
    assert str(link).encode() not in completed.stdout


def test_plan_reader_uses_only_read_only_no_follow_nonblocking_flags(
    tmp_path: Path, monkeypatch
) -> None:
    import mycelium_physical_preflight.validator as validator

    plan_file = tmp_path / "operator-plan.json"
    plan_file.write_bytes(b"{}")
    real_open = validator.os.open
    observed: list[int] = []

    def tracked_open(*args, **kwargs):
        observed.append(args[1])
        return real_open(*args, **kwargs)

    monkeypatch.setattr(validator.os, "open", tracked_open)
    assert validator.read_plan_file(plan_file) == b"{}"
    assert len(observed) == 1

    flags = observed[0]
    mutation_flags = (
        os.O_WRONLY
        | os.O_RDWR
        | os.O_APPEND
        | os.O_CREAT
        | os.O_EXCL
        | os.O_TRUNC
    )
    assert flags & mutation_flags == 0
    assert flags & getattr(os, "O_NOFOLLOW", 0) == getattr(os, "O_NOFOLLOW", 0)
    assert flags & getattr(os, "O_CLOEXEC", 0) == getattr(os, "O_CLOEXEC", 0)
    assert flags & getattr(os, "O_NONBLOCK", 0) == getattr(os, "O_NONBLOCK", 0)
