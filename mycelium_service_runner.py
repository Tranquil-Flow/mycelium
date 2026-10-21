# SPDX-License-Identifier: AGPL-3.0-only
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
import threading
import time
from typing import Any
import uuid

import mycelium_host_platform
from mycelium_host_platform import HostPlatformError
from mycelium_qualification.evidence import canonical_json_bytes
from mycelium_service_package import validate_service_config


STATUS_PROTOCOL = "mycelium.service_runner_status.v1"


STOP_EVENT_PREFIX = "Local\\mycelium-service-"
_WIN32_STOP_POLL_MS = 250
EXIT_NOT_RUNNING = 3


def stop_event_name(service_id: str) -> str:
    """The per-user named event a Windows runner watches for ``--stop``."""

    return f"{STOP_EVENT_PREFIX}{service_id}-stop"


def _win32_atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    """Owner-only state directory + CREATE_NEW/MoveFileExW replacement."""

    try:
        with mycelium_host_platform.private_directory(path.parent, create=True) as directory:
            directory.atomic_replace(path.name, canonical_json_bytes(value))
    except HostPlatformError as exc:
        raise ValueError("service_runner_state_write_failed") from exc


def _atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    if mycelium_host_platform.is_win32():
        return _win32_atomic_json(path, value)
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


def _win32_image_matcher(executable: str) -> Any:
    expected = os.path.normcase(os.path.normpath(executable)).casefold()

    def matches(image: str) -> bool:
        return os.path.normcase(os.path.normpath(image)).casefold() == expected

    return matches


def _win32_stop_child(api: Any, job: Any, child_event: int, timeout: float) -> None:
    """Signal the child's stop event, wait ``timeout``, then TerminateJobObject."""

    from mycelium_host_platform._win32_api import Win32Error

    try:
        api.set_event(child_event)
    except Win32Error:
        pass
    if job.wait_empty(time.monotonic() + timeout):
        return
    if not job.terminate(time.monotonic() + max(timeout, 1.0)):
        raise ValueError("service_runner_child_stop_failed")


def _win32_run_service(config_path: Path) -> int:
    """The bounded-restart loop with a job-contained child and event-based stop."""

    from mycelium_host_platform._win32_api import Win32Error
    from mycelium_host_platform._win32_job import Win32JobProcess

    value = json.loads(config_path.read_text("utf-8"))
    if not isinstance(value, dict):
        raise ValueError("service_runner_config_invalid")
    config = validate_service_config(value)
    state_path = _state_path(config)
    api = mycelium_host_platform.win32_api()
    try:
        stop_handle, existed = api.create_event(stop_event_name(config["service_id"]))
    except Win32Error as exc:
        raise ValueError("service_runner_stop_event_unavailable") from exc
    if existed:
        api.close(stop_handle)
        raise ValueError("service_runner_already_running")
    # Set from the SIGINT/SIGTERM handler while this (main) thread waits on
    # it: never a threading.Event, whose set() takes the lock wait() holds.
    stopping = mycelium_host_platform.StopRequest()
    finished = threading.Event()

    def request_stop(_signum: int, _frame: Any) -> None:
        stopping.set()

    def watch_stop_event() -> None:
        while not finished.is_set():
            try:
                if api.wait_event(stop_handle, _WIN32_STOP_POLL_MS):
                    stopping.set()
                    return
            except Win32Error:
                stopping.set()
                return

    previous_handlers = {
        signum: signal.signal(signum, request_stop)
        for signum in (signal.SIGINT, signal.SIGTERM)
    }
    watcher = threading.Thread(
        target=watch_stop_event, name="mycelium-service-stop", daemon=True
    )
    watcher.start()
    log_path = Path(config["log_directory"]) / f"{config['service_id']}.log"
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
            child_event_name = (
                f"{STOP_EVENT_PREFIX}{config['service_id']}-child-{uuid.uuid4().hex}"
            )
            try:
                child_event, _existed = api.create_event(child_event_name)
            except Win32Error as exc:
                raise ValueError("service_runner_stop_event_unavailable") from exc
            try:
                environment = dict(os.environ)
                environment.update(config["environment"])
                environment[mycelium_host_platform.STOP_EVENT_ENV] = child_event_name
                job = None
                try:
                    log_path.parent.mkdir(parents=True, exist_ok=True)
                    with open(log_path, "ab") as log:
                        job = Win32JobProcess.start(
                            api,
                            config["argv"],
                            image_matches=_win32_image_matcher(config["argv"][0]),
                            cwd=config["working_directory"],
                            env=environment,
                            stdin=subprocess.DEVNULL,
                            stdout=log,
                            stderr=subprocess.STDOUT,
                            close_fds=True,
                        )
                except (HostPlatformError, OSError):
                    child_exit_code = 127
                if job is not None:
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
                    while job.process.poll() is None and not stopping.is_set():
                        stopping.wait(0.1)
                    if stopping.is_set():
                        _win32_stop_child(
                            api, job, child_event, float(config["stop_timeout_seconds"])
                        )
                        _atomic_json(
                            state_path,
                            _status(
                                config,
                                state="stopped_by_manager",
                                starts=starts,
                                child_exit_code=job.process.returncode,
                                observed_at=time.time(),
                            ),
                        )
                        return 0
                    # The child exited; nothing it started may outlive it.
                    if not job.terminate(time.monotonic() + max(
                        float(config["stop_timeout_seconds"]), 1.0
                    )):
                        raise ValueError("service_runner_child_stop_failed")
                    child_exit_code = int(job.process.returncode)
            finally:
                try:
                    api.close(child_event)
                except Win32Error:
                    pass
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
            while time.monotonic() < deadline and not stopping.is_set():
                stopping.wait(min(0.1, max(0.0, deadline - time.monotonic())))
            if stopping.is_set():
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
        finished.set()
        watcher.join(timeout=2.0)
        for signum, handler in previous_handlers.items():
            signal.signal(signum, handler)
        try:
            api.close(stop_handle)
        except Win32Error:
            pass


def stop_service(config_path: Path) -> int:
    """``--stop``: signal a running Windows runner's named stop event."""

    from mycelium_host_platform._win32_api import Win32Error

    value = json.loads(config_path.read_text("utf-8"))
    if not isinstance(value, dict):
        raise ValueError("service_runner_config_invalid")
    config = validate_service_config(value)
    if not mycelium_host_platform.is_win32():
        raise ValueError("service_runner_stop_unsupported")
    api = mycelium_host_platform.win32_api()
    try:
        handle = api.open_event(stop_event_name(config["service_id"]))
    except Win32Error:
        return EXIT_NOT_RUNNING
    try:
        api.set_event(handle)
    finally:
        try:
            api.close(handle)
        except Win32Error:
            pass
    return 0


def run_service(config_path: Path) -> int:
    if mycelium_host_platform.is_win32():
        return _win32_run_service(config_path)
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
    parser.add_argument(
        "--stop",
        action="store_true",
        help="ask a running Windows runner to stop (named stop event)",
    )
    args = parser.parse_args(argv)
    try:
        if args.stop:
            return stop_service(args.config)
        return run_service(args.config)
    except Exception as exc:
        print(type(exc).__name__, file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["STATUS_PROTOCOL", "run_service", "stop_event_name", "stop_service"]
