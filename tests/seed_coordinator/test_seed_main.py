from __future__ import annotations

import json
import os
from pathlib import Path
import selectors
import stat
import subprocess
import sys
from typing import Any
from urllib.request import urlopen


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
