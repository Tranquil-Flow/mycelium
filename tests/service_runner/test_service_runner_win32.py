# SPDX-License-Identifier: AGPL-3.0-only
"""The service runner on a simulated win32 host.

The child is a real process contained in a fake job object; the runner's
``--stop`` channel and the child's stop channel are fake named events.  The
child "observes" its event through a sentinel file the fake writes when the
runner signals that event (a separate process cannot share the fake's
in-memory event table).
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import sys
import threading
import time

import pytest

import mycelium_host_platform
import mycelium_service_runner as runner
from mycelium_host_platform import _win32_api as w

from tests.host_platform.fake_win32 import simulate_win32
from tests.host_platform.signal_probe import guard_signal_handlers


REAL_INTERPRETER = sys.executable
ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def host(tmp_path, monkeypatch):
    fake = simulate_win32(monkeypatch)
    fake.interpreter = REAL_INTERPRETER
    python = tmp_path / "python" / "python.exe"
    python.parent.mkdir()
    python.write_bytes(b"MZ")
    stop_sentinel = tmp_path / "child-stop-requested"
    original_set_event = fake.set_event

    def set_event(event: int) -> None:
        original_set_event(event)
        if fake.event_handles.get(event, "").find("-child-") >= 0:
            stop_sentinel.write_text("stop")

    fake.set_event = set_event
    yield fake, python, stop_sentinel
    for popen in fake.popen_by_pid.values():
        if popen.poll() is None:
            popen.kill()
            popen.wait(timeout=5)


def _config(tmp_path: Path, python: Path, code: str, **overrides) -> Path:
    state = tmp_path / "state"
    mycelium_host_platform.private_directory(state, create=True).close()
    logs = tmp_path / "logs"
    logs.mkdir()
    value = {
        "protocol": "mycelium.service_package.v1",
        "package_version": "m22-2",
        "service_id": "win-runner",
        "role": "supervisor",
        "argv": [str(python), "-c", code],
        "working_directory": str(ROOT),
        "state_directory": str(state),
        "log_directory": str(logs),
        "environment": {"PYTHONDONTWRITEBYTECODE": "1"},
        "health_url": None,
        "restart_limit": 2,
        "restart_window_seconds": 60,
        "restart_delay_seconds": 0.01,
        "stop_timeout_seconds": 2,
    }
    value.update(overrides)
    path = tmp_path / "service-config.json"
    path.write_text(json.dumps(value), "utf-8")
    return path


def _status(config_path: Path) -> dict:
    config = json.loads(config_path.read_text("utf-8"))
    return json.loads(
        (Path(config["state_directory"]) / ".mycelium-service-win-runner.json").read_text("utf-8")
    )


def _stop_when_running(config_path: Path, *, after: float = 0.2) -> threading.Thread:
    def stop() -> None:
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            try:
                if _status(config_path)["state"] == "running":
                    break
            except (OSError, ValueError, KeyError):
                pass
            time.sleep(0.02)
        time.sleep(after)
        assert runner.main(["--config", str(config_path), "--stop"]) == 0

    thread = threading.Thread(target=stop, daemon=True)
    thread.start()
    return thread


def _child(stop_sentinel: Path, *, obey: bool, environment_log: Path | None = None) -> str:
    lines = ["import os, sys, time, pathlib"]
    if environment_log is not None:
        lines.append(
            f"pathlib.Path({str(environment_log)!r}).write_text(os.environ.get('MYCELIUM_STOP_EVENT', ''))"
        )
    if obey:
        lines.append(
            f"p = pathlib.Path({str(stop_sentinel)!r})\n"
            "for _ in range(6000):\n"
            "    if p.exists():\n"
            "        sys.exit(0)\n"
            "    time.sleep(0.01)\n"
            "sys.exit(9)"
        )
    else:
        lines.append("time.sleep(120)")
    return "\n".join(lines)


def test_stop_signals_the_child_event_and_the_child_exits_gracefully(tmp_path, host) -> None:
    fake, python, sentinel = host
    seen = tmp_path / "child-env"
    config = _config(tmp_path, python, _child(sentinel, obey=True, environment_log=seen))
    stopper = _stop_when_running(config)
    assert runner.run_service(config) == 0
    stopper.join(10)
    status = _status(config)
    assert status["state"] == "stopped_by_manager"
    assert status["child_exit_code"] == 0
    names = fake.call_names()
    assert "terminate_job" not in names
    assert names.index("set_event") < len(names)
    child_event = seen.read_text()
    assert child_event.startswith("Local\\mycelium-service-win-runner-child-")
    spawn = next(call for call in fake.calls if call[0] == "spawn_suspended")
    assert spawn[1][0] == str(python)


def test_a_child_that_ignores_its_event_is_job_terminated_after_the_timeout(tmp_path, host) -> None:
    fake, python, sentinel = host
    config = _config(tmp_path, python, _child(sentinel, obey=False), stop_timeout_seconds=1)
    stopper = _stop_when_running(config)
    started = time.monotonic()
    assert runner.run_service(config) == 0
    stopper.join(10)
    assert time.monotonic() - started < 30
    names = fake.call_names()
    assert names.index("set_event") < names.index("terminate_job")
    status = _status(config)
    assert status["state"] == "stopped_by_manager"
    assert status["child_exit_code"] not in (None, 0)
    assert all(popen.poll() is not None for popen in fake.popen_by_pid.values())


def test_the_child_runs_in_a_kill_on_close_job(tmp_path, host) -> None:
    fake, python, _sentinel = host
    config = _config(tmp_path, python, "import sys; sys.exit(0)")
    assert runner.run_service(config) == 0
    assert _status(config)["state"] == "stopped_cleanly"
    limits = next(call for call in fake.calls if call[0] == "set_job_limits")
    assert limits[2] & w.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    order = [
        fake.call_names().index(name)
        for name in ("spawn_suspended", "assign_process", "process_image_name", "resume_process")
    ]
    assert order == sorted(order)
    assert fake.jobs == {}


def test_crashes_consume_the_persistent_restart_budget(tmp_path, host) -> None:
    fake, python, _sentinel = host
    config = _config(tmp_path, python, "import sys; sys.exit(7)")
    assert runner.run_service(config) == 0
    status = _status(config)
    assert status["state"] == "restart_budget_exhausted"
    assert status["attempt_count_in_window"] == 2
    assert fake.call_names().count("spawn_suspended") == 2
    assert "argv" not in status


def test_a_job_that_cannot_be_proven_counts_as_a_failed_start(tmp_path, host) -> None:
    fake, python, _sentinel = host
    fake.fail_next("assign_process", w.ERROR_ACCESS_DENIED, times=2)
    config = _config(tmp_path, python, "import sys; sys.exit(0)")
    assert runner.run_service(config) == 0
    status = _status(config)
    assert status["state"] == "restart_budget_exhausted"
    assert all(popen.poll() is not None for popen in fake.popen_by_pid.values())
    assert "resume_process" not in fake.call_names()


def test_state_is_written_through_the_owner_only_seam(tmp_path, host) -> None:
    fake, python, _sentinel = host
    config = _config(tmp_path, python, "import sys; sys.exit(0)")
    assert runner.run_service(config) == 0
    moves = [call for call in fake.calls if call[0] == "move_replace"]
    assert moves and all(call[2].endswith(".mycelium-service-win-runner.json") for call in moves)
    widened = tmp_path / "widened"
    widened.mkdir()
    with pytest.raises(ValueError, match="service_runner_state_write_failed"):
        runner._atomic_json(widened / "state.json", {"a": 1})


def test_a_second_runner_for_the_same_service_is_refused(tmp_path, host) -> None:
    fake, python, _sentinel = host
    fake.create_event(runner.stop_event_name("win-runner"))
    config = _config(tmp_path, python, "import sys; sys.exit(0)")
    with pytest.raises(ValueError, match="service_runner_already_running"):
        runner.run_service(config)
    assert "spawn_suspended" not in fake.call_names()


def test_stop_without_a_running_runner_reports_not_running(tmp_path, host) -> None:
    _fake, python, _sentinel = host
    config = _config(tmp_path, python, "import sys; sys.exit(0)")
    assert runner.main(["--config", str(config), "--stop"]) == runner.EXIT_NOT_RUNNING


def test_stop_is_windows_only(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(mycelium_host_platform.sys, "platform", "darwin")
    config = tmp_path / "service-config.json"
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    (tmp_path / "logs").mkdir()
    config.write_text(
        json.dumps(
            {
                "protocol": "mycelium.service_package.v1",
                "package_version": "m22-2",
                "service_id": "win-runner",
                "role": "supervisor",
                "argv": [REAL_INTERPRETER, "-c", "pass"],
                "working_directory": str(ROOT),
                "state_directory": str(state),
                "log_directory": str(tmp_path / "logs"),
                "environment": {},
                "health_url": None,
                "restart_limit": 1,
                "restart_window_seconds": 60,
                "restart_delay_seconds": 0.01,
                "stop_timeout_seconds": 2,
            }
        ),
        "utf-8",
    )
    assert runner.main(["--config", str(config), "--stop"]) == 2
    assert os.listdir(state) == []


@pytest.mark.skipif(not hasattr(signal, "pthread_kill"), reason="needs a POSIX test host")
def test_sigterm_stops_the_runner_without_its_handler_taking_a_lock(
    tmp_path, host, monkeypatch
) -> None:
    # The handler runs on the main thread between bytecodes, while that thread
    # sits in the runner's stop wait; a handler that takes the wait's lock
    # (Event.set under Event.wait) deadlocks the runner on its own thread.
    fake, python, sentinel = host
    config = _config(tmp_path, python, _child(sentinel, obey=True))
    installed = guard_signal_handlers(monkeypatch)
    before = signal.getsignal(signal.SIGTERM)
    main_thread = threading.main_thread().ident

    def deliver() -> None:
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            try:
                if _status(config)["state"] == "running":
                    break
            except (OSError, ValueError, KeyError):
                pass
            time.sleep(0.02)
        time.sleep(0.2)
        if signal.SIGTERM in installed and signal.getsignal(signal.SIGTERM) is not before:
            signal.pthread_kill(main_thread, signal.SIGTERM)

    sender = threading.Thread(target=deliver, daemon=True)
    sender.start()
    assert runner.run_service(config) == 0
    sender.join(10)
    status = _status(config)
    assert status["state"] == "stopped_by_manager"
    assert status["child_exit_code"] == 0
    assert set(installed) == {signal.SIGINT, signal.SIGTERM}
    assert signal.getsignal(signal.SIGTERM) is before
