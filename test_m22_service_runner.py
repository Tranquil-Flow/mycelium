from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

from mycelium_service_runner import STATUS_PROTOCOL


ROOT = Path(__file__).resolve().parent


def _config(tmp_path: Path, argv: list[str], **overrides: object) -> Path:
    state = tmp_path / "state"
    logs = tmp_path / "logs"
    state.mkdir(mode=0o700)
    logs.mkdir(mode=0o700)
    value: dict[str, object] = {
        "protocol": "mycelium.service_package.v1",
        "package_version": "m22-2",
        "service_id": "runner-proof",
        "role": "supervisor",
        "argv": argv,
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


def _status(config_path: Path) -> dict[str, object]:
    config = json.loads(config_path.read_text("utf-8"))
    path = Path(config["state_directory"]) / ".mycelium-service-runner-proof.json"
    return json.loads(path.read_text("utf-8"))


def test_restart_budget_is_persistent_and_fail_closed(tmp_path: Path) -> None:
    counter = tmp_path / "counter"
    code = (
        "from pathlib import Path; import sys; "
        f"p=Path({str(counter)!r}); p.write_text(p.read_text()+'x' if p.exists() else 'x'); sys.exit(7)"
    )
    config_path = _config(tmp_path, [sys.executable, "-c", code])

    first = subprocess.run(
        [sys.executable, "-m", "mycelium_service_runner", "--config", str(config_path)],
        cwd=ROOT,
        check=False,
    )
    second = subprocess.run(
        [sys.executable, "-m", "mycelium_service_runner", "--config", str(config_path)],
        cwd=ROOT,
        check=False,
    )

    assert first.returncode == second.returncode == 0
    assert counter.read_text() == "xx"
    status = _status(config_path)
    assert status["protocol"] == STATUS_PROTOCOL
    assert status["state"] == "restart_budget_exhausted"
    assert status["attempt_count_in_window"] == 2
    assert "argv" not in status


def test_sigterm_is_forwarded_and_recorded_as_clean_manager_stop(tmp_path: Path) -> None:
    marker = tmp_path / "terminated"
    helper = tmp_path / "child.py"
    helper.write_text(
        "import signal,time\n"
        "from pathlib import Path\n"
        f"marker=Path({str(marker)!r})\n"
        "def stop(*_):\n"
        "    marker.write_text('term')\n"
        "    raise SystemExit(0)\n"
        "signal.signal(signal.SIGTERM, stop)\n"
        "time.sleep(120)\n",
        "utf-8",
    )
    config_path = _config(tmp_path, [sys.executable, str(helper)])
    process = subprocess.Popen(
        [sys.executable, "-m", "mycelium_service_runner", "--config", str(config_path)],
        cwd=ROOT,
        start_new_session=True,
    )
    try:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            try:
                if _status(config_path).get("state") == "running":
                    break
            except FileNotFoundError:
                pass
            time.sleep(0.05)
        else:
            raise AssertionError("runner did not start")
        os.kill(process.pid, signal.SIGTERM)
        assert process.wait(timeout=10) == 0
        assert marker.read_text() == "term"
        assert _status(config_path)["state"] == "stopped_by_manager"
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=5)
