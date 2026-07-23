from __future__ import annotations

import importlib
import json
import os
from pathlib import Path
import selectors
import signal
import socket
import stat
import subprocess
import sys
import threading
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
        assert ready, (
            f"process did not emit startup status; returncode={process.poll()}"
        )
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
        with urlopen(
            first_status["seed_url"] + "/seed/identity", timeout=2
        ) as response:
            identity = json.loads(response.read())
        assert (
            identity["statement"]["seed_endpoint_id"]
            == first_status["seed_endpoint_id"]
        )
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


def test_seed_state_root_rejects_nonfinal_symlink_without_creation(
    tmp_path: Path,
) -> None:
    seed_main = importlib.import_module("mycelium_seed.__main__")
    real_parent = tmp_path / "real"
    real_parent.mkdir(mode=0o700)
    linked_parent = tmp_path / "linked"
    linked_parent.symlink_to(real_parent, target_is_directory=True)

    with pytest.raises(ValueError):
        seed_main._private_directory(linked_parent / "state")

    assert not (real_parent / "state").exists()


@pytest.mark.parametrize(
    ("flag", "value"),
    [
        ("--swarm-id", "invalid swarm"),
        ("--seed-node-id", "invalid seed"),
        ("--incarnation", "invalid incarnation"),
        ("--bind", "http://127.0.0.1"),
        ("--port", "65536"),
        ("--advertised-url", "http://user:password@127.0.0.1:8765"),
    ],
)
def test_seed_dry_run_uses_real_constructor_validators_without_state(
    tmp_path: Path,
    flag: str,
    value: str,
) -> None:
    data_dir = tmp_path / "missing" / "seed"
    command = _command(data_dir)
    command.append("--dry-run")
    if flag in command:
        command[command.index(flag) + 1] = value
    else:
        command.extend([flag, value])

    completed = subprocess.run(
        command,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    assert completed.returncode == 2
    assert completed.stdout == ""
    assert completed.stderr == "seed_preflight_failed\n"
    assert not data_dir.exists()


def test_seed_dry_run_accepts_absent_root_without_creating_it(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    seed_main = importlib.import_module("mycelium_seed.__main__")
    data_dir = tmp_path / "missing" / "seed"
    assert seed_main.run(["--data-dir", str(data_dir), "--dry-run"]) == 0
    assert not data_dir.exists()
    capsys.readouterr()


def test_second_signal_handler_failure_restores_first_and_closes_server(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seed_main = importlib.import_module("mycelium_seed.__main__")
    data_dir = tmp_path / "seed-handlers"
    data_dir.mkdir(mode=0o700)
    old_handler = object()
    calls: list[tuple[int, object]] = []

    class FakeSigner:
        endpoint_id = "seed-handler-endpoint"

    class FakeCoordinator:
        seed_url = "http://127.0.0.1:8765"

        def __init__(self, **_kwargs: Any) -> None:
            return None

    class FakeRegistry:
        def __init__(self, _path: Path) -> None:
            return None

    class FakeServer:
        instances: list["FakeServer"] = []

        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            self.closed = False
            self.started = False
            self.instances.append(self)

        def start(self) -> "FakeServer":
            self.started = True
            return self

        def close(self) -> None:
            self.closed = True

    installs = 0

    def fake_signal(signum: int, handler: object) -> object:
        nonlocal installs
        calls.append((signum, handler))
        if handler is not old_handler:
            installs += 1
            if installs == 2:
                raise RuntimeError("second-handler-secret")
        return old_handler

    monkeypatch.setattr(
        seed_main, "load_or_create_node_signer", lambda _path: FakeSigner()
    )
    monkeypatch.setattr(seed_main, "SeedCoordinator", FakeCoordinator)
    monkeypatch.setattr(seed_main, "SqliteInviteRegistry", FakeRegistry)
    monkeypatch.setattr(seed_main, "SeedHTTPServer", FakeServer)
    monkeypatch.setattr(seed_main.signal, "signal", fake_signal)

    with pytest.raises(seed_main._EntrypointFailure) as failed:
        seed_main.run(["--data-dir", str(data_dir)])

    assert failed.value.code == "seed_runtime_failed"
    assert FakeServer.instances[0].closed is True
    assert (signal.SIGINT, old_handler) in calls


def test_seed_status_failure_closes_started_server(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seed_main = importlib.import_module("mycelium_seed.__main__")
    data_dir = tmp_path / "seed-status"
    data_dir.mkdir(mode=0o700)

    class FakeSigner:
        endpoint_id = "seed-status-endpoint"

    class FakeCoordinator:
        seed_url = "http://127.0.0.1:8765"

        def __init__(self, **_kwargs: Any) -> None:
            return None

    class FakeRegistry:
        def __init__(self, _path: Path) -> None:
            return None

    class FakeServer:
        instance: "FakeServer"

        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            self.started = False
            self.closed = False
            FakeServer.instance = self

        def start(self) -> "FakeServer":
            self.started = True
            return self

        def close(self) -> None:
            self.closed = True

    monkeypatch.setattr(
        seed_main, "load_or_create_node_signer", lambda _path: FakeSigner()
    )
    monkeypatch.setattr(seed_main, "SeedCoordinator", FakeCoordinator)
    monkeypatch.setattr(seed_main, "SqliteInviteRegistry", FakeRegistry)
    monkeypatch.setattr(seed_main, "SeedHTTPServer", FakeServer)
    monkeypatch.setattr(
        seed_main,
        "_emit_status",
        lambda _status: (_ for _ in ()).throw(KeyError("status-secret")),
    )

    with pytest.raises(seed_main._EntrypointFailure) as failed:
        seed_main.run(["--data-dir", str(data_dir)])

    assert failed.value.code == "seed_runtime_failed"
    assert FakeServer.instance.started is True
    assert FakeServer.instance.closed is True


def test_server_start_failure_closes_without_shutdown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seed_http = importlib.import_module("mycelium_seed.http")

    class FakeCoordinator:
        def bind_seed_url(self, _url: str) -> None:
            return None

    class FakeHTTPServer:
        server_address = ("127.0.0.1", 8765)

        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            self.shutdown_calls = 0
            self.close_calls = 0

        def shutdown(self) -> None:
            self.shutdown_calls += 1

        def server_close(self) -> None:
            self.close_calls += 1

        def serve_forever(self) -> None:
            return None

    class FailingThread:
        def __init__(self, **_kwargs: Any) -> None:
            return None

        def start(self) -> None:
            raise RuntimeError("thread-start-secret")

    monkeypatch.setattr(seed_http, "ThreadingHTTPServer", FakeHTTPServer)
    monkeypatch.setattr(seed_http.threading, "Thread", FailingThread)
    server = seed_http.SeedHTTPServer(
        FakeCoordinator(),
        host="127.0.0.1",
        port=8765,
    )

    with pytest.raises(RuntimeError):
        server.start()
    server.close()

    assert server._server.shutdown_calls == 0
    assert server._server.close_calls == 1
    assert server._thread is None


def test_seed_main_catches_keyerror_without_value_or_traceback(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    seed_main = importlib.import_module("mycelium_seed.__main__")
    secret = "seed-secret-value"

    def fail() -> int:
        raise KeyError(secret)

    monkeypatch.setattr(seed_main, "run", fail)
    with pytest.raises(SystemExit) as stopped:
        seed_main.main()

    captured = capsys.readouterr()
    assert stopped.value.code == 4
    assert captured.out == ""
    assert captured.err == "seed_runtime_failed\n"
    assert secret not in captured.err
    assert "Traceback" not in captured.err


def test_seed_server_start_close_state_machine_leaves_no_thread() -> None:
    seed_http = importlib.import_module("mycelium_seed.http")

    class FakeCoordinator:
        def bind_seed_url(self, _url: str) -> None:
            return None

    server = seed_http.SeedHTTPServer(
        FakeCoordinator(),
        host="127.0.0.1",
        port=0,
    )
    assert server.start() is server
    assert server.start() is server
    server.close()
    server.close()

    assert server._thread is None
    assert not any(
        thread.name == "mycelium-seed-http" and thread.is_alive()
        for thread in threading.enumerate()
    )
    with pytest.raises(RuntimeError):
        server.start()


def test_seed_server_close_before_start_never_calls_shutdown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seed_http = importlib.import_module("mycelium_seed.http")

    class FakeCoordinator:
        def bind_seed_url(self, _url: str) -> None:
            return None

    class FakeHTTPServer:
        server_address = ("127.0.0.1", 8765)

        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            self.shutdown_calls = 0
            self.close_calls = 0

        def shutdown(self) -> None:
            self.shutdown_calls += 1

        def server_close(self) -> None:
            self.close_calls += 1

    monkeypatch.setattr(seed_http, "ThreadingHTTPServer", FakeHTTPServer)
    server = seed_http.SeedHTTPServer(
        FakeCoordinator(),
        host="127.0.0.1",
        port=8765,
    )
    server.close()
    server.close()

    assert server._server.shutdown_calls == 0
    assert server._server.close_calls == 1
