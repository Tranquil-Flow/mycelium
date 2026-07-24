from __future__ import annotations

import importlib
import json
import os
from pathlib import Path
import selectors
import socket
import stat
import subprocess
import sys
from typing import Any
from urllib.request import urlopen

import pytest

from mycelium_qualification.evidence import canonical_json_bytes


def _read_status(process: subprocess.Popen[str]) -> dict[str, Any]:
    assert process.stdout is not None
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ)
    try:
        ready = selector.select(timeout=10)
        assert ready, f"process did not emit startup status; returncode={process.poll()}"
        line = process.stdout.readline()
    finally:
        selector.close()
    assert line
    return json.loads(line)


def _command(data_dir: Path) -> list[str]:
    return [
        sys.executable,
        "-m",
        "mycelium_seed",
        "--bind",
        "127.0.0.1",
        "--port",
        "0",
        "--data-dir",
        str(data_dir),
    ]


def _start(data_dir: Path) -> tuple[subprocess.Popen[str], dict[str, Any]]:
    process = subprocess.Popen(
        _command(data_dir),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        return process, _read_status(process)
    except BaseException:
        process.terminate()
        process.wait(timeout=5)
        raise


def test_seed_module_starts_real_listener_and_reuses_private_identity(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "seed"
    first, first_status = _start(data_dir)
    try:
        assert first_status == {
            "event": "seed_started",
            "protocol": "mycelium.seed_main_status.v1",
            "route_ready": False,
            "seed_endpoint_id": first_status["seed_endpoint_id"],
            "seed_url": first_status["seed_url"],
        }
        with urlopen(first_status["seed_url"] + "/seed/identity", timeout=2) as response:
            identity = json.loads(response.read())
        assert identity["statement"]["seed_endpoint_id"] == first_status[
            "seed_endpoint_id"
        ]
    finally:
        first.terminate()
        first_stdout, first_stderr = first.communicate(timeout=5)
    assert first.returncode == 0
    assert "private" not in first_stdout.lower() + first_stderr.lower()

    key_file = data_dir / "identity" / "seed.key"
    assert key_file.is_file()
    assert stat.S_IMODE(key_file.stat().st_mode) == 0o600
    assert key_file.stat().st_uid == os.getuid()

    second, second_status = _start(data_dir)
    try:
        assert second_status["seed_endpoint_id"] == first_status["seed_endpoint_id"]
        assert second_status["route_ready"] is False
    finally:
        second.terminate()
        second_stdout, second_stderr = second.communicate(timeout=5)
    assert second.returncode == 0
    assert "private" not in second_stdout.lower() + second_stderr.lower()


def test_seed_dry_run_performs_no_network_io_and_emits_canonical_not_ready(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    seed_main = importlib.import_module("mycelium_seed.__main__")
    data_dir = tmp_path / "seed-dry-run"
    data_dir.mkdir(mode=0o700)
    data_dir.chmod(0o700)

    def forbidden(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("dry-run crossed a network or persistent-runtime boundary")

    monkeypatch.setattr(seed_main, "load_or_create_node_signer", forbidden)
    monkeypatch.setattr(seed_main, "SeedHTTPServer", forbidden)

    assert seed_main.run(["--data-dir", str(data_dir), "--dry-run"]) == 0

    captured = capsys.readouterr()
    expected = {
        "event": "seed_dry_run",
        "protocol": "mycelium.seed_main_status.v1",
        "route_ready": False,
    }
    assert captured.out.encode() == canonical_json_bytes(expected) + b"\n"
    assert captured.err == ""


def test_seed_rejects_non_owner_only_state_root_without_chmod(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "public-seed-state"
    data_dir.mkdir(mode=0o755)
    data_dir.chmod(0o755)

    occupied = socket.socket()
    occupied.bind(("127.0.0.1", 0))
    port = occupied.getsockname()[1]
    command = _command(data_dir)
    command[command.index("0")] = str(port)
    try:
        completed = subprocess.run(
            command,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    finally:
        occupied.close()

    assert completed.returncode == 2
    assert completed.stdout == ""
    assert completed.stderr == "seed_preflight_failed\n"
    assert stat.S_IMODE(data_dir.stat().st_mode) == 0o755


def test_seed_runtime_failure_has_distinct_exit_status(tmp_path: Path) -> None:
    data_dir = tmp_path / "seed-runtime-failure"
    data_dir.mkdir(mode=0o700)
    data_dir.chmod(0o700)
    occupied = socket.socket()
    occupied.bind(("127.0.0.1", 0))
    port = occupied.getsockname()[1]
    command = _command(data_dir)
    command[command.index("0")] = str(port)
    try:
        completed = subprocess.run(
            command,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    finally:
        occupied.close()

    assert completed.returncode == 4
    assert completed.stdout == ""
    assert completed.stderr == "seed_runtime_failed\n"
