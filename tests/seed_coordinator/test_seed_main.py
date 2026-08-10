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
from urllib.parse import urlsplit
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


def _initialize(data_dir: Path) -> dict[str, Any]:
    completed = subprocess.run(
        [*_command(data_dir), "--init-only"],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert completed.returncode == 0
    assert completed.stderr == ""
    return json.loads(completed.stdout)


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
    initialized = _initialize(data_dir)
    assert initialized["event"] == "seed_initialized"
    assert initialized["route_ready"] is False
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


def test_seed_service_refuses_implicit_initialization(tmp_path: Path) -> None:
    completed = subprocess.run(
        _command(tmp_path / "missing-seed"),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    assert completed.returncode == 2
    assert completed.stdout == ""
    assert completed.stderr == "seed_preflight_failed\n"
    assert not (tmp_path / "missing-seed").exists()


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
    _initialize(data_dir)
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
    (data_dir / "state.sqlite3").touch(mode=0o600)
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

    class FakeState:
        def __init__(self, _path: Path) -> None:
            return None

        def identity_binding(self) -> dict[str, str]:
            return {"seed_key_digest": "sha256:" + "1" * 64}

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
        seed_main,
        "load_bound_seed_signer",
        lambda _path, **_kwargs: FakeSigner(),
    )
    monkeypatch.setattr(seed_main, "SeedCoordinator", FakeCoordinator)
    monkeypatch.setattr(seed_main, "SqliteInviteRegistry", FakeRegistry)
    monkeypatch.setattr(seed_main, "SqliteSeedState", FakeState)
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
    (data_dir / "state.sqlite3").touch(mode=0o600)

    class FakeSigner:
        endpoint_id = "seed-status-endpoint"

    class FakeCoordinator:
        seed_url = "http://127.0.0.1:8765"

        def __init__(self, **_kwargs: Any) -> None:
            return None

    class FakeRegistry:
        def __init__(self, _path: Path) -> None:
            return None

    class FakeState:
        def __init__(self, _path: Path) -> None:
            return None

        def identity_binding(self) -> dict[str, str]:
            return {"seed_key_digest": "sha256:" + "1" * 64}

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
        seed_main,
        "load_bound_seed_signer",
        lambda _path, **_kwargs: FakeSigner(),
    )
    monkeypatch.setattr(seed_main, "SeedCoordinator", FakeCoordinator)
    monkeypatch.setattr(seed_main, "SqliteInviteRegistry", FakeRegistry)
    monkeypatch.setattr(seed_main, "SqliteSeedState", FakeState)
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


def test_state_root_lease_pins_original_during_signer_path_api(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seed_main = importlib.import_module("mycelium_seed.__main__")
    data_dir = tmp_path / "seed-bound-root"
    moved = tmp_path / "seed-bound-root-original"
    data_dir.mkdir(mode=0o700)

    class FakeSigner:
        endpoint_id = "seed-bound-root-endpoint"

    def replace_during_signer(key_path: Path) -> FakeSigner:
        data_dir.rename(moved)
        data_dir.mkdir(mode=0o700)
        key_path.parent.mkdir(mode=0o700, parents=True)
        key_path.write_bytes(b"original-descriptor-only")
        return FakeSigner()

    def forbidden_registry(_path: Path) -> Any:
        raise AssertionError("database setup followed a replaced state root")

    monkeypatch.setattr(
        seed_main,
        "load_or_create_node_signer",
        replace_during_signer,
    )
    monkeypatch.setattr(seed_main, "SqliteInviteRegistry", forbidden_registry)

    with pytest.raises(seed_main._EntrypointFailure) as failed:
        seed_main.run(["--data-dir", str(data_dir), "--init-only"])

    assert failed.value.code == "seed_preflight_failed"
    assert not (data_dir / "identity" / "seed.key").exists()
    assert (
        moved / "identity" / "seed.key"
    ).read_bytes() == b"original-descriptor-only"


def test_private_directory_lease_rejects_opened_final_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process_module = importlib.import_module("mycelium_node.process")
    state_root = tmp_path / "state"
    moved = tmp_path / "state-original"
    state_root.mkdir(mode=0o700)
    real_open = os.open
    replaced = False

    def replace_after_open(path: Any, flags: int, *args: Any, **kwargs: Any) -> int:
        nonlocal replaced
        descriptor = real_open(path, flags, *args, **kwargs)
        if (
            not replaced
            and path == state_root.name
            and kwargs.get("dir_fd") is not None
        ):
            replaced = True
            state_root.rename(moved)
            state_root.mkdir(mode=0o700)
        return descriptor

    monkeypatch.setattr(os, "open", replace_after_open)
    with pytest.raises(ValueError):
        process_module.private_directory_lease(state_root)

    assert state_root.is_dir()
    assert moved.is_dir()


@pytest.mark.parametrize(
    ("created", "final", "failure"),
    [
        (False, True, "fstat"),
        (True, True, "fchmod"),
        (True, False, "fchmod"),
    ],
)
def test_private_directory_lease_closes_each_opened_fd_on_post_open_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    created: bool,
    final: bool,
    failure: str,
) -> None:
    process_module = importlib.import_module("mycelium_node.process")
    target_name = f"tracked-{created}-{final}-{failure}"
    target = tmp_path / target_name
    path = target if final else target / "final"
    if not created:
        target.mkdir(mode=0o700)
    opened: list[int] = []
    closed: list[int] = []
    components: dict[int, str] = {}
    failed = False
    real_open = os.open
    real_close = os.close
    real_fstat = os.fstat
    real_fchmod = os.fchmod

    def tracked_open(
        name: Any,
        flags: int,
        *args: Any,
        **kwargs: Any,
    ) -> int:
        descriptor = real_open(name, flags, *args, **kwargs)
        opened.append(descriptor)
        components[descriptor] = os.fspath(name)
        return descriptor

    def tracked_close(descriptor: int) -> None:
        closed.append(descriptor)
        components.pop(descriptor, None)
        real_close(descriptor)

    def injected_fstat(descriptor: int) -> os.stat_result:
        nonlocal failed
        if (
            failure == "fstat"
            and components.get(descriptor) == target_name
            and not failed
        ):
            failed = True
            raise OSError("injected fstat failure")
        return real_fstat(descriptor)

    def injected_fchmod(descriptor: int, mode: int) -> None:
        nonlocal failed
        if (
            failure == "fchmod"
            and components.get(descriptor) == target_name
            and not failed
        ):
            failed = True
            raise OSError("injected fchmod failure")
        real_fchmod(descriptor, mode)

    monkeypatch.setattr(os, "open", tracked_open)
    monkeypatch.setattr(os, "close", tracked_close)
    monkeypatch.setattr(os, "fstat", injected_fstat)
    monkeypatch.setattr(os, "fchmod", injected_fchmod)

    with pytest.raises(ValueError):
        process_module.private_directory_lease(path)

    assert failed is True
    assert sorted(closed) == sorted(opened)


@pytest.mark.parametrize("failure", ["fstat", "validation"])
def test_private_directory_parent_fd_closes_each_opened_fd_on_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    process_module = importlib.import_module("mycelium_node.process")
    target_name = f"tracked-parent-{failure}"
    target = tmp_path / target_name
    target.mkdir(mode=0o700)
    path = target / "leaf"
    opened: list[int] = []
    closed: list[int] = []
    components: dict[int, str] = {}
    failed = False
    real_open = os.open
    real_close = os.close
    real_fstat = os.fstat
    real_validate = process_module._validate_walk_component

    def tracked_open(
        name: Any,
        flags: int,
        *args: Any,
        **kwargs: Any,
    ) -> int:
        descriptor = real_open(name, flags, *args, **kwargs)
        opened.append(descriptor)
        components[descriptor] = os.fspath(name)
        return descriptor

    def tracked_close(descriptor: int) -> None:
        closed.append(descriptor)
        components.pop(descriptor, None)
        real_close(descriptor)

    def injected_fstat(descriptor: int) -> os.stat_result:
        nonlocal failed
        if (
            failure == "fstat"
            and components.get(descriptor) == target_name
            and not failed
        ):
            failed = True
            raise OSError("injected fstat failure")
        return real_fstat(descriptor)

    def injected_validation(metadata: os.stat_result) -> None:
        nonlocal failed
        if (
            failure == "validation"
            and target_name in components.values()
            and not failed
        ):
            failed = True
            raise ValueError("injected validation failure")
        real_validate(metadata)

    monkeypatch.setattr(os, "open", tracked_open)
    monkeypatch.setattr(os, "close", tracked_close)
    monkeypatch.setattr(os, "fstat", injected_fstat)
    monkeypatch.setattr(
        process_module,
        "_validate_walk_component",
        injected_validation,
    )

    with pytest.raises((OSError, ValueError)):
        process_module.private_directory_parent_fd(path)

    assert failed is True
    assert sorted(closed) == sorted(opened)


def test_private_directory_walkers_transfer_fd_ownership_exactly_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process_module = importlib.import_module("mycelium_node.process")
    state_root = tmp_path / "tracked-success"
    state_root.mkdir(mode=0o700)
    opened: list[int] = []
    closed: list[int] = []
    real_open = os.open
    real_close = os.close

    def tracked_open(
        name: Any,
        flags: int,
        *args: Any,
        **kwargs: Any,
    ) -> int:
        descriptor = real_open(name, flags, *args, **kwargs)
        opened.append(descriptor)
        return descriptor

    def tracked_close(descriptor: int) -> None:
        closed.append(descriptor)
        real_close(descriptor)

    monkeypatch.setattr(os, "open", tracked_open)
    monkeypatch.setattr(os, "close", tracked_close)

    lease = process_module.private_directory_lease(state_root)
    assert sorted(opened).count(lease._parent_descriptor) == (
        sorted(closed).count(lease._parent_descriptor) + 1
    )
    assert sorted(opened).count(lease._descriptor) == (
        sorted(closed).count(lease._descriptor) + 1
    )
    lease.close()
    assert sorted(closed) == sorted(opened)

    parent = process_module.private_directory_parent_fd(state_root / "leaf")
    assert sorted(opened).count(parent) == sorted(closed).count(parent) + 1
    os.close(parent)
    assert sorted(closed) == sorted(opened)


def test_private_directory_walkers_keep_fd_inventory_stable_for_200_cycles(
    tmp_path: Path,
) -> None:
    process_module = importlib.import_module("mycelium_node.process")
    state_root = tmp_path / "inventory-stability"
    state_root.mkdir(mode=0o700)
    inventory_root = Path("/proc/self/fd")
    if not inventory_root.is_dir():
        inventory_root = Path("/dev/fd")

    before = {int(name) for name in os.listdir(inventory_root) if name.isdigit()}
    for _cycle in range(200):
        lease = process_module.private_directory_lease(state_root)
        lease.close()
        parent = process_module.private_directory_parent_fd(state_root / "leaf")
        os.close(parent)
    after = {int(name) for name in os.listdir(inventory_root) if name.isdigit()}

    assert after == before


@pytest.mark.parametrize("value", ["0", "1", "65535"])
def test_seed_port_parser_accepts_only_canonical_decimal_range(value: str) -> None:
    seed_main = importlib.import_module("mycelium_seed.__main__")
    args = seed_main._parser().parse_args(["--port", value, "--data-dir", "/unused"])
    assert args.port == int(value)


@pytest.mark.parametrize(
    "value",
    [
        "+1",
        "-0",
        "00",
        "01",
        "1_0",
        " 1",
        "1 ",
        "１２",
        "1e2",
        "12x",
        "-1",
        "65536",
    ],
)
def test_seed_port_parser_rejects_noncanonical_values_with_stable_error(
    value: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    seed_main = importlib.import_module("mycelium_seed.__main__")
    with pytest.raises(SystemExit) as stopped:
        seed_main._parser().parse_args(
            ["--port", value, "--data-dir", "/unused"]
        )

    captured = capsys.readouterr()
    assert stopped.value.code == 2
    assert captured.out == ""
    assert captured.err == "seed_preflight_failed\n"


def test_seed_server_close_retries_until_live_request_thread_is_dead(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seed_http = importlib.import_module("mycelium_seed.http")

    class FakeCoordinator:
        def bind_seed_url(self, _url: str) -> None:
            return None

    class FakeRequestThread:
        def __init__(self) -> None:
            self.alive = True
            self.join_calls = 0

        def join(self, timeout: float | None = None) -> None:
            assert timeout is not None
            self.join_calls += 1

        def is_alive(self) -> bool:
            return self.alive

    request_thread = FakeRequestThread()

    class FakeHTTPServer:
        server_address = ("127.0.0.1", 8765)

        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            self._threads = [request_thread]
            self.close_calls = 0

        def server_close(self) -> None:
            self.close_calls += 1

    monkeypatch.setattr(seed_http, "ThreadingHTTPServer", FakeHTTPServer)
    server = seed_http.SeedHTTPServer(
        FakeCoordinator(),
        host="127.0.0.1",
        port=8765,
    )

    with pytest.raises(RuntimeError, match="seed HTTP server cleanup failed"):
        server.close()
    assert server._closed is False
    assert server._server.close_calls == 1
    assert server._request_threads == (request_thread,)

    with pytest.raises(RuntimeError, match="seed HTTP server cleanup failed"):
        server.close()
    assert server._server.close_calls == 1
    assert request_thread.join_calls == 2

    request_thread.alive = False
    server.close()
    server.close()
    assert server._closed is True
    assert server._server.close_calls == 1
    assert server._request_threads == ()


def test_concurrent_seed_server_close_calls_socket_close_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seed_http = importlib.import_module("mycelium_seed.http")

    class FakeCoordinator:
        def bind_seed_url(self, _url: str) -> None:
            return None

    calls_changed = threading.Condition()
    release_close = threading.Event()

    class FakeHTTPServer:
        server_address = ("127.0.0.1", 8765)

        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            self._threads: list[Any] = []
            self.close_calls = 0

        def server_close(self) -> None:
            with calls_changed:
                self.close_calls += 1
                calls_changed.notify_all()
            assert release_close.wait(1.0)

    monkeypatch.setattr(seed_http, "ThreadingHTTPServer", FakeHTTPServer)
    server = seed_http.SeedHTTPServer(
        FakeCoordinator(),
        host="127.0.0.1",
        port=8765,
    )
    begin = threading.Barrier(3)
    failures: list[BaseException] = []

    def close_server() -> None:
        begin.wait()
        try:
            server.close()
        except BaseException as exc:
            failures.append(exc)

    closers = [
        threading.Thread(target=close_server, daemon=False)
        for _index in range(2)
    ]
    for closer in closers:
        closer.start()
    begin.wait()
    with calls_changed:
        assert calls_changed.wait_for(
            lambda: server._server.close_calls >= 1,
            timeout=1.0,
        )
        calls_changed.wait_for(
            lambda: server._server.close_calls >= 2,
            timeout=0.2,
        )
    release_close.set()
    for closer in closers:
        closer.join(timeout=1.0)

    assert failures == []
    assert all(not closer.is_alive() for closer in closers)
    assert server._server.close_calls == 1


def test_seed_server_base_url_maps_wildcards_to_loopback_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seed_http = importlib.import_module("mycelium_seed.http")
    bound: list[tuple[str, int]] = []

    class FakeHTTPServer:
        def __init__(self, address: tuple[str, int], *_args: Any) -> None:
            bound.append(address)
            self.server_address = address
            self.daemon_threads = False
            self.block_on_close = True
            self.timeout = None

        def server_close(self) -> None:
            return None

    class FakeCoordinator:
        def __init__(self) -> None:
            self.seed_url: str | None = None

        def bind_seed_url(self, url: str) -> None:
            self.seed_url = url

    monkeypatch.setattr(seed_http, "ThreadingHTTPServer", FakeHTTPServer)
    monkeypatch.setattr(seed_http, "_IPv6ThreadingHTTPServer", FakeHTTPServer)
    cases = (
        (
            "0.0.0.0",
            "http://seed-v4.test:8765",
            "http://127.0.0.1:8765",
            "http://seed-v4.test:8765",
        ),
        (
            "::",
            "http://[::1]:8765",
            "http://[::1]:8765",
            "http://[::1]:8765",
        ),
        (
            "127.0.0.1",
            None,
            "http://127.0.0.1:8765",
            "http://127.0.0.1:8765",
        ),
        (
            "::1",
            None,
            "http://[::1]:8765",
            "http://[::1]:8765",
        ),
    )
    for host, advertised_url, expected_base_url, expected_bound_url in cases:
        coordinator = FakeCoordinator()
        server = seed_http.SeedHTTPServer(
            coordinator,
            host=host,
            port=8765,
            advertised_url=advertised_url,
        )
        assert bound.pop() == (host, 8765)
        assert server._server.server_address == (host, 8765)
        assert server.base_url == expected_base_url
        assert coordinator.seed_url == expected_bound_url
        server.close()


@pytest.mark.parametrize(
    ("host", "family"),
    [
        pytest.param("127.0.0.1", socket.AF_INET, id="ipv4-control"),
        pytest.param("::1", socket.AF_INET6, id="ipv6-loopback"),
        pytest.param("::", socket.AF_INET6, id="ipv6-wildcard"),
    ],
)
def test_seed_server_real_bind_selects_literal_address_family(
    host: str,
    family: socket.AddressFamily,
) -> None:
    seed_http = importlib.import_module("mycelium_seed.http")

    try:
        with socket.socket(family, socket.SOCK_STREAM) as probe:
            probe.bind((host, 0))
            port = int(probe.getsockname()[1])
    except OSError as exc:
        if family == socket.AF_INET6:
            pytest.skip(f"local IPv6 bind is unavailable: errno={exc.errno}")
        raise

    class FakeCoordinator:
        def __init__(self) -> None:
            self.seed_url: str | None = None

        def bind_seed_url(self, url: str) -> None:
            self.seed_url = url

    coordinator = FakeCoordinator()
    advertised_url = f"http://[::1]:{port}" if host == "::" else None
    server = seed_http.SeedHTTPServer(
        coordinator,
        host=host,
        port=port,
        advertised_url=advertised_url,
    )
    try:
        assert server._server.address_family == family
        assert server._server.server_address[0] == host
        assert server._server.server_address[1] == port
        expected_base_host = "::1" if host == "::" else host
        assert urlsplit(server.base_url).hostname == expected_base_host
        assert urlsplit(server.base_url).port == port
        assert coordinator.seed_url == (advertised_url or server.base_url)
    finally:
        server.close()


@pytest.mark.parametrize(
    ("host", "family", "advertised_url", "expected_base_host"),
    [
        pytest.param(
            "127.0.0.1",
            socket.AF_INET,
            "http://127.0.0.1:45678",
            "127.0.0.1",
            id="ipv4-concrete",
        ),
        pytest.param(
            "0.0.0.0",
            socket.AF_INET,
            "http://seed-v4.test:45678",
            "127.0.0.1",
            id="ipv4-wildcard",
        ),
        pytest.param(
            "::1",
            socket.AF_INET6,
            "http://[::1]:45678",
            "::1",
            id="ipv6-concrete",
        ),
        pytest.param(
            "::",
            socket.AF_INET6,
            "http://seed-v6.test:45678",
            "::1",
            id="ipv6-wildcard",
        ),
    ],
)
def test_seed_server_port_zero_keeps_local_and_advertised_ports_distinct(
    host: str,
    family: socket.AddressFamily,
    advertised_url: str,
    expected_base_host: str,
) -> None:
    seed_http = importlib.import_module("mycelium_seed.http")

    if family == socket.AF_INET6:
        try:
            with socket.socket(family, socket.SOCK_STREAM) as probe:
                probe.bind((host, 0))
        except OSError as exc:
            pytest.skip(f"local IPv6 bind is unavailable: errno={exc.errno}")

    class FakeCoordinator:
        def __init__(self) -> None:
            self.seed_url: str | None = None

        def bind_seed_url(self, url: str) -> None:
            self.seed_url = url

    coordinator = FakeCoordinator()
    server = seed_http.SeedHTTPServer(
        coordinator,
        host=host,
        port=0,
        advertised_url=advertised_url,
    )
    try:
        bound_port = int(server._server.server_address[1])
        parsed_base = urlsplit(server.base_url)
        assert bound_port > 0
        assert parsed_base.hostname == expected_base_host
        assert parsed_base.port == bound_port
        assert coordinator.seed_url == advertised_url
        assert urlsplit(coordinator.seed_url).port == 45678
    finally:
        server.close()


def test_seed_cleanup_aggregation_preserves_primary_and_is_value_free() -> None:
    seed_main = importlib.import_module("mycelium_seed.__main__")
    primary = seed_main._EntrypointFailure("seed_preflight_failed", 2)
    phases = ("server", "signal_restoration", "state_root")

    aggregated = seed_main._aggregate_cleanup_failures(primary, phases)
    assert aggregated is primary
    assert (aggregated.code, aggregated.exit_status) == (
        "seed_preflight_failed",
        2,
    )
    assert getattr(aggregated, "__notes__", ()) == [
        "cleanup_phase=server",
        "cleanup_phase=signal_restoration",
        "cleanup_phase=state_root",
        "cleanup_failure_count=3",
    ]

    cleanup_only = seed_main._aggregate_cleanup_failures(None, phases)
    assert (cleanup_only.code, cleanup_only.exit_status) == (
        "seed_runtime_failed",
        4,
    )
    assert "secret-close-value" not in str(cleanup_only)


@pytest.mark.parametrize(
    ("host", "advertised_url"),
    [
        ("127.0.0.1", "http://127.0.0.1:45678"),
        ("localhost", "http://localhost:45678"),
        ("0.0.0.0", "http://seed-v4.test:45678"),
        ("::1", "http://[::1]:45678"),
        ("::", "http://seed-v6.test:45678"),
    ],
)
def test_seed_cli_port_zero_accepts_independent_nonzero_advertisement(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    host: str,
    advertised_url: str,
) -> None:
    seed_main = importlib.import_module("mycelium_seed.__main__")
    data_dir = tmp_path / host.replace(":", "v6")
    data_dir.mkdir(mode=0o700)

    assert (
        seed_main.run(
            [
                "--bind",
                host,
                "--port",
                "0",
                "--advertised-url",
                advertised_url,
                "--data-dir",
                str(data_dir),
                "--dry-run",
            ]
        )
        == 0
    )
    capsys.readouterr()


def test_seed_server_port_zero_preserves_localhost_advertised_identity() -> None:
    seed_http = importlib.import_module("mycelium_seed.http")

    class FakeCoordinator:
        def __init__(self) -> None:
            self.seed_url: str | None = None

        def bind_seed_url(self, url: str) -> None:
            self.seed_url = url

    coordinator = FakeCoordinator()
    server = seed_http.SeedHTTPServer(
        coordinator,
        host="localhost",
        port=0,
        advertised_url="http://localhost:45678",
    )
    try:
        assert urlsplit(server.base_url).hostname in {"127.0.0.1", "::1"}
        assert urlsplit(server.base_url).port == server._server.server_address[1]
        assert coordinator.seed_url == "http://localhost:45678"
    finally:
        server.close()


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:0",
        "http://[::1]:0",
        "https://seed.test:0",
    ],
)
def test_endpoint_url_rejects_explicit_port_zero(url: str) -> None:
    seed_http = importlib.import_module("mycelium_seed.http")
    with pytest.raises(ValueError, match="URL is invalid"):
        seed_http._validate_endpoint_url(url)


@pytest.mark.parametrize(
    ("bind_port", "advertised_url", "accepted"),
    [
        (0, "http://localhost", True),
        (80, "http://localhost", True),
        (8765, "http://localhost", False),
        (0, "http://localhost:0", False),
        (0, "http://localhost/", False),
        (0, "http://localhost:45678/path", False),
    ],
)
def test_seed_cli_advertised_url_default_missing_and_malformed_controls(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    bind_port: int,
    advertised_url: str,
    accepted: bool,
) -> None:
    seed_main = importlib.import_module("mycelium_seed.__main__")
    data_dir = tmp_path / f"control-{bind_port}-{accepted}"
    data_dir.mkdir(mode=0o700)
    arguments = [
        "--bind",
        "localhost",
        "--port",
        str(bind_port),
        "--advertised-url",
        advertised_url,
        "--data-dir",
        str(data_dir),
        "--dry-run",
    ]

    if accepted:
        assert seed_main.run(arguments) == 0
    else:
        with pytest.raises(seed_main._EntrypointFailure) as caught:
            seed_main.run(arguments)
        assert (caught.value.code, caught.value.exit_status) == (
            "seed_preflight_failed",
            2,
        )
    capsys.readouterr()


def test_seed_server_port_zero_accepts_default_advertised_port() -> None:
    seed_http = importlib.import_module("mycelium_seed.http")

    class FakeCoordinator:
        def __init__(self) -> None:
            self.seed_url: str | None = None

        def bind_seed_url(self, url: str) -> None:
            self.seed_url = url

    coordinator = FakeCoordinator()
    server = seed_http.SeedHTTPServer(
        coordinator,
        host="localhost",
        port=0,
        advertised_url="http://localhost",
    )
    try:
        assert server._server.server_address[1] > 0
        assert urlsplit(server.base_url).port == server._server.server_address[1]
        assert coordinator.seed_url == "http://localhost"
        assert urlsplit(coordinator.seed_url).port is None
    finally:
        server.close()
