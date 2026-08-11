# SPDX-License-Identifier: AGPL-3.0-or-later
"""Persistent bounded-restart runner shared by launchd and systemd packages."""

from __future__ import annotations

import argparse
from collections.abc import Mapping
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
from typing import Any

from mycelium_qualification.evidence import canonical_json_bytes
from mycelium_service_package import validate_service_config


STATUS_PROTOCOL = "mycelium.service_runner_status.v1"


def _atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(canonical_json_bytes(value))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _state_path(config: Mapping[str, Any]) -> Path:
    return Path(config["state_directory"]) / (
        f".mycelium-service-{config['service_id']}.json"
    )


def _load_starts(path: Path, service_id: str) -> list[float]:
    try:
        value = json.loads(path.read_text("utf-8"))
    except FileNotFoundError:
        return []
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("service_runner_state_invalid") from exc
    if (
        not isinstance(value, dict)
        or value.get("protocol") != STATUS_PROTOCOL
        or value.get("service_id") != service_id
        or not isinstance(value.get("starts_unix_seconds"), list)
        or any(
            isinstance(item, bool) or not isinstance(item, (int, float)) or item < 0
            for item in value["starts_unix_seconds"]
        )
    ):
        raise ValueError("service_runner_state_invalid")
    return [float(item) for item in value["starts_unix_seconds"]]


def _status(
    config: Mapping[str, Any],
    *,
    state: str,
    starts: list[float],
    child_exit_code: int | None,
    observed_at: float,
) -> dict[str, Any]:
    return {
        "protocol": STATUS_PROTOCOL,
        "service_id": config["service_id"],
        "role": config["role"],
        "state": state,
        "observed_at_unix_ms": int(observed_at * 1_000),
        "starts_unix_seconds": starts,
        "attempt_count_in_window": len(starts),
        "restart_limit": config["restart_limit"],
        "restart_window_seconds": config["restart_window_seconds"],
        "child_exit_code": child_exit_code,
        "membership_is_not_route_eligibility": True,
        "privacy": "no argv, environment, path, credential, endpoint, address, prompt, or output",
    }


def _terminate(process: subprocess.Popen[bytes], timeout: float) -> None:
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=timeout)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        return
    process.wait(timeout=max(timeout, 1.0))


def run_service(config_path: Path) -> int:
    value = json.loads(config_path.read_text("utf-8"))
    if not isinstance(value, dict):
        raise ValueError("service_runner_config_invalid")
    config = validate_service_config(value)
    state_path = _state_path(config)
    stopping = False

    def request_stop(_signum: int, _frame: Any) -> None:
        nonlocal stopping
        stopping = True

    previous_handlers = {
        signum: signal.signal(signum, request_stop)
        for signum in (signal.SIGINT, signal.SIGTERM)
    }
    try:
        starts = _load_starts(state_path, config["service_id"])
        while True:
            observed = time.time()
            threshold = observed - float(config["restart_window_seconds"])
            starts = [item for item in starts if item >= threshold]
            if len(starts) >= int(config["restart_limit"]):
                _atomic_json(
                    state_path,
                    _status(
                        config,
                        state="restart_budget_exhausted",
                        starts=starts,
                        child_exit_code=None,
                        observed_at=observed,
                    ),
                )
                return 0
            starts.append(observed)
            _atomic_json(
                state_path,
                _status(
                    config,
                    state="starting",
                    starts=starts,
                    child_exit_code=None,
                    observed_at=observed,
                ),
            )
            environment = dict(os.environ)
            environment.update(config["environment"])
            try:
                process = subprocess.Popen(
                    config["argv"],
                    cwd=config["working_directory"],
                    env=environment,
                    stdin=subprocess.DEVNULL,
                    start_new_session=True,
                )
            except OSError:
                child_exit_code = 127
            else:
                _atomic_json(
                    state_path,
                    _status(
                        config,
                        state="running",
                        starts=starts,
                        child_exit_code=None,
                        observed_at=time.time(),
                    ),
                )
                while process.poll() is None and not stopping:
                    time.sleep(0.1)
                if stopping:
                    _terminate(process, float(config["stop_timeout_seconds"]))
                    _atomic_json(
                        state_path,
                        _status(
                            config,
                            state="stopped_by_manager",
                            starts=starts,
                            child_exit_code=process.returncode,
                            observed_at=time.time(),
                        ),
                    )
                    return 0
                child_exit_code = int(process.returncode)
            if child_exit_code == 0:
                _atomic_json(
                    state_path,
                    _status(
                        config,
                        state="stopped_cleanly",
                        starts=starts,
                        child_exit_code=0,
                        observed_at=time.time(),
                    ),
                )
                return 0
            _atomic_json(
                state_path,
                _status(
                    config,
                    state="restart_pending",
                    starts=starts,
                    child_exit_code=child_exit_code,
                    observed_at=time.time(),
                ),
            )
            deadline = time.monotonic() + float(config["restart_delay_seconds"])
            while time.monotonic() < deadline and not stopping:
                time.sleep(min(0.1, max(0.0, deadline - time.monotonic())))
            if stopping:
                _atomic_json(
                    state_path,
                    _status(
                        config,
                        state="stopped_by_manager",
                        starts=starts,
                        child_exit_code=child_exit_code,
                        observed_at=time.time(),
                    ),
                )
                return 0
    finally:
        for signum, handler in previous_handlers.items():
            signal.signal(signum, handler)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        return run_service(args.config)
    except Exception as exc:
        print(type(exc).__name__, file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["STATUS_PROTOCOL", "run_service"]
