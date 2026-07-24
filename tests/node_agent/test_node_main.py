from __future__ import annotations

import importlib
from itertools import count
import json
import os
from pathlib import Path
import selectors
import signal
import stat
import subprocess
import sys
import threading
import time
from typing import Any

import pytest

from mycelium_invite import SqliteInviteRegistry
from mycelium_qualification.evidence import canonical_json_bytes
from mycelium_qualification.signing import generate_ed25519_signer
from mycelium_seed import SeedCoordinator
from mycelium_seed.http import SeedHTTPServer
from tests.e2e_request_iroh.conftest import (
    native_iroh_sidecar_binary as node_main_sidecar_binary,  # noqa: F401
)


def _ids():
    values = count(1)
    return lambda: f"node-main-seed-message-{next(values)}"


def _read_status(process: subprocess.Popen[str]) -> dict[str, Any]:
    assert process.stdout is not None
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ)
    try:
        ready = selector.select(timeout=15)
        assert ready, f"process did not emit startup status; returncode={process.poll()}"
        line = process.stdout.readline()
    finally:
        selector.close()
    if not line:
        process.wait(timeout=5)
        diagnostic = process.stderr.read() if process.stderr is not None else ""
        raise AssertionError(
            f"process exited before startup status: {process.returncode}: {diagnostic}"
        )
    return json.loads(line)


def _pid_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def _node_command(
    *,
    data_dir: Path,
    node_id: str,
    sidecar: Path,
    bundle_file: Path | None = None,
    bundle_stdin: bool = False,
    dry_run: bool = False,
) -> list[str]:
    command = [
        sys.executable,
        "-m",
        "mycelium_node",
        "--data-dir",
        str(data_dir),
    ]
    if bundle_file is not None:
        command.extend(["--join-bundle-file", str(bundle_file)])
    if bundle_stdin:
        command.append("--join-bundle-stdin")
    command.extend(
        [
            "--node-id",
            node_id,
            "--advertise",
            f"https://{node_id}.test/control",
            "--sidecar-path",
            str(sidecar),
            "--run-id",
            "node-main-run",
            "--deployment-id",
            "node-main-unassigned",
        ]
    )
    if dry_run:
        command.append("--dry-run")
    return command


def _write_bundle(path: Path, bundle: dict[str, Any]) -> None:
    path.write_bytes(canonical_json_bytes(bundle))
    path.chmod(0o600)


def test_node_module_joins_seed_supervises_child_and_cleans_up(
    tmp_path: Path,
    node_main_sidecar_binary: Path,  # noqa: F811
) -> None:
    coordinator = SeedCoordinator(
        swarm_id="swarm-node-main",
        seed_node_id="seed-node",
        seed_url=None,
        signer=generate_ed25519_signer(endpoint_id="seed-endpoint"),
        invite_registry=SqliteInviteRegistry(tmp_path / "seed" / "state.sqlite3"),
        incarnation="seed-main-test",
        id_source=_ids(),
    )
    data_dir = tmp_path / "node"
    invite_file = tmp_path / "node-invite.json"

    with SeedHTTPServer(
        coordinator,
        host="127.0.0.1",
        port=0,
    ) as seed_server:
        bundle = coordinator.mint_invite(nonce="node-main-invite", ttl_seconds=120)
        _write_bundle(invite_file, bundle)
        command = _node_command(
            data_dir=data_dir,
            node_id="node-main-a",
            sidecar=node_main_sidecar_binary,
            bundle_file=invite_file,
        )
        assert bundle["token"] not in command
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        try:
            status = _read_status(process)
            process_listing = subprocess.check_output(
                ["ps", "-ww", "-o", "command=", "-p", str(process.pid)],
                text=True,
            )
            assert bundle["token"] not in process_listing
            assert bundle["token"] not in json.dumps(status, sort_keys=True)
            assert status == {
                "event": "node_started",
                "membership_generation": 1,
                "node_endpoint_id": status["node_endpoint_id"],
                "node_id": "node-main-a",
                "node_process_pid": status["node_process_pid"],
                "protocol": "mycelium.node_main_status.v1",
                "route_ready": False,
                "seed_url": seed_server.base_url,
            }
            assert isinstance(status["node_process_pid"], int)
            assert status["node_process_pid"] > 0
            assert status["node_process_pid"] != process.pid
            member = coordinator.member("node-main-a")
            assert member["endpoint_id"] == status["node_endpoint_id"]
            assert member["generation"] == 1
            assert member["last_heartbeat_sequence"] == 1
        finally:
            process.terminate()
            stdout, stderr = process.communicate(timeout=10)

    assert process.returncode == 0
    combined_output = stdout + stderr
    assert bundle["token"] not in combined_output
    assert "private_key" not in combined_output
    key_file = data_dir / "identity" / "node.key"
    assert key_file.is_file()
    assert stat.S_IMODE(key_file.stat().st_mode) == 0o600

    child_pid = status["node_process_pid"]
    deadline = time.monotonic() + 5
    while _pid_exists(child_pid) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert _pid_exists(child_pid) is False


def test_node_accepts_join_bundle_from_stdin_without_secret_leakage(
    tmp_path: Path,
    node_main_sidecar_binary: Path,  # noqa: F811
) -> None:
    coordinator = SeedCoordinator(
        swarm_id="swarm-node-main-stdin",
        seed_node_id="seed-node",
        seed_url=None,
        signer=generate_ed25519_signer(endpoint_id="seed-endpoint-stdin"),
        invite_registry=SqliteInviteRegistry(tmp_path / "seed-stdin" / "state.sqlite3"),
        incarnation="seed-main-test",
        id_source=_ids(),
    )
    with SeedHTTPServer(coordinator, host="127.0.0.1", port=0):
        bundle = coordinator.mint_invite(nonce="node-main-stdin", ttl_seconds=120)
        command = _node_command(
            data_dir=tmp_path / "node-stdin",
            node_id="node-main-stdin",
            sidecar=node_main_sidecar_binary,
            bundle_stdin=True,
        )
        assert bundle["token"] not in command
        process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        try:
            assert process.stdin is not None
            process.stdin.write(canonical_json_bytes(bundle).decode("utf-8"))
            process.stdin.close()
            process.stdin = None
            status = _read_status(process)
            process_listing = subprocess.check_output(
                ["ps", "-ww", "-o", "command=", "-p", str(process.pid)],
                text=True,
            )
            assert bundle["token"] not in process_listing
            assert bundle["token"] not in json.dumps(status, sort_keys=True)
        finally:
            if process.poll() is None:
                process.terminate()
            stdout, stderr = process.communicate(timeout=10)

    assert process.returncode == 0
    assert bundle["token"] not in stdout + stderr
    assert status["route_ready"] is False


def test_node_dry_run_performs_no_network_or_process_io(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    node_main = importlib.import_module("mycelium_node.__main__")
    data_dir = tmp_path / "node-dry-run"
    data_dir.mkdir(mode=0o700)
    data_dir.chmod(0o700)
    sidecar = tmp_path / "sidecar"
    sidecar.write_text("#!/bin/sh\nexit 0\n")
    sidecar.chmod(0o700)
    coordinator = SeedCoordinator(
        swarm_id="swarm-node-main-dry",
        seed_node_id="seed-node",
        seed_url="http://127.0.0.1:9",
        signer=generate_ed25519_signer(endpoint_id="seed-endpoint-dry"),
        invite_registry=SqliteInviteRegistry(tmp_path / "seed-dry" / "state.sqlite3"),
        incarnation="seed-main-test",
        id_source=_ids(),
    )
    bundle = coordinator.mint_invite(nonce="node-main-dry", ttl_seconds=120)
    bundle_file = tmp_path / "dry-bundle.json"
    _write_bundle(bundle_file, bundle)

    def forbidden(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("dry-run crossed a network or process boundary")

    monkeypatch.setattr(node_main.SeedHTTPClient, "_request", forbidden)
    monkeypatch.setattr(node_main, "PhysicalNodeProcess", forbidden)
    monkeypatch.setattr(node_main, "load_or_create_node_signer", forbidden)

    assert node_main.run(
        _node_command(
            data_dir=data_dir,
            node_id="node-main-dry",
            sidecar=sidecar,
            bundle_file=bundle_file,
            dry_run=True,
        )[3:]
    ) == 0

    captured = capsys.readouterr()
    expected = {
        "event": "node_dry_run",
        "protocol": "mycelium.node_main_status.v1",
        "route_ready": False,
    }
    assert captured.out.encode() == canonical_json_bytes(expected) + b"\n"
    assert captured.err == ""


def test_node_rejects_non_owner_only_bundle_file(tmp_path: Path) -> None:
    data_dir = tmp_path / "node-private"
    data_dir.mkdir(mode=0o700)
    data_dir.chmod(0o700)
    sidecar = tmp_path / "sidecar"
    sidecar.write_text("sidecar")
    sidecar.chmod(0o700)
    bundle_file = tmp_path / "public-bundle.json"
    bundle_file.write_bytes(b"{}")
    bundle_file.chmod(0o644)

    command = _node_command(
        data_dir=data_dir,
        node_id="node-public-bundle",
        sidecar=sidecar,
        bundle_file=bundle_file,
    )
    command[command.index("--join-bundle-file")] = "--seed-invite"
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
    assert completed.stderr == "node_preflight_failed\n"
    assert stat.S_IMODE(bundle_file.stat().st_mode) == 0o644


def test_node_rejects_non_owner_only_state_root(tmp_path: Path) -> None:
    data_dir = tmp_path / "node-public-state"
    data_dir.mkdir(mode=0o755)
    data_dir.chmod(0o755)
    sidecar = tmp_path / "sidecar"
    sidecar.write_text("sidecar")
    sidecar.chmod(0o700)
    coordinator = SeedCoordinator(
        swarm_id="swarm-node-main-state",
        seed_node_id="seed-node",
        seed_url="http://127.0.0.1:9",
        signer=generate_ed25519_signer(endpoint_id="seed-endpoint-state"),
        invite_registry=SqliteInviteRegistry(tmp_path / "seed-state" / "state.sqlite3"),
        incarnation="seed-main-test",
        id_source=_ids(),
    )
    bundle_file = tmp_path / "state-bundle.json"
    _write_bundle(
        bundle_file,
        coordinator.mint_invite(nonce="node-main-state", ttl_seconds=120),
    )

    command = _node_command(
        data_dir=data_dir,
        node_id="node-public-state",
        sidecar=sidecar,
        bundle_file=bundle_file,
    )
    command[command.index("--join-bundle-file")] = "--seed-invite"
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
    assert completed.stderr == "node_preflight_failed\n"
    assert stat.S_IMODE(data_dir.stat().st_mode) == 0o755


def test_node_join_rejection_has_distinct_exit_and_redacts_token(
    tmp_path: Path,
    node_main_sidecar_binary: Path,  # noqa: F811
) -> None:
    coordinator = SeedCoordinator(
        swarm_id="swarm-node-main-replay",
        seed_node_id="seed-node",
        seed_url=None,
        signer=generate_ed25519_signer(endpoint_id="seed-endpoint-replay"),
        invite_registry=SqliteInviteRegistry(tmp_path / "seed-replay" / "state.sqlite3"),
        incarnation="seed-main-test",
        id_source=_ids(),
    )
    bundle_file = tmp_path / "replay-bundle.json"
    with SeedHTTPServer(coordinator, host="127.0.0.1", port=0):
        bundle = coordinator.mint_invite(nonce="node-main-replay", ttl_seconds=120)
        _write_bundle(bundle_file, bundle)
        first_command = _node_command(
            data_dir=tmp_path / "node-replay-first",
            node_id="node-main-replay-first",
            sidecar=node_main_sidecar_binary,
            bundle_file=bundle_file,
        )
        first_command[first_command.index("--join-bundle-file")] = "--seed-invite"
        first = subprocess.Popen(
            first_command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        try:
            _read_status(first)
        finally:
            if first.poll() is None:
                first.terminate()
            first.communicate(timeout=10)
        assert first.returncode == 0

        rejected_command = _node_command(
            data_dir=tmp_path / "node-replay-second",
            node_id="node-main-replay-second",
            sidecar=node_main_sidecar_binary,
            bundle_file=bundle_file,
        )
        rejected_command[rejected_command.index("--join-bundle-file")] = "--seed-invite"
        rejected = subprocess.run(
            rejected_command,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )

    assert rejected.returncode == 3
    assert rejected.stdout == ""
    assert rejected.stderr == "node_join_rejected\n"
    assert bundle["token"] not in " ".join(rejected_command)
    assert bundle["token"] not in rejected.stdout + rejected.stderr


def test_node_runtime_failure_has_distinct_exit_status(
    tmp_path: Path,
    node_main_sidecar_binary: Path,  # noqa: F811
) -> None:
    coordinator = SeedCoordinator(
        swarm_id="swarm-node-main-runtime",
        seed_node_id="seed-node",
        seed_url=None,
        signer=generate_ed25519_signer(endpoint_id="seed-endpoint-runtime"),
        invite_registry=SqliteInviteRegistry(tmp_path / "seed-runtime" / "state.sqlite3"),
        incarnation="seed-main-test",
        id_source=_ids(),
    )
    bundle_file = tmp_path / "runtime-bundle.json"
    with SeedHTTPServer(coordinator, host="127.0.0.1", port=0):
        bundle = coordinator.mint_invite(nonce="node-main-runtime", ttl_seconds=120)
        _write_bundle(bundle_file, bundle)
    command = _node_command(
        data_dir=tmp_path / "node-runtime",
        node_id="node-main-runtime",
        sidecar=node_main_sidecar_binary,
        bundle_file=bundle_file,
    )
    command[command.index("--join-bundle-file")] = "--seed-invite"

    completed = subprocess.run(
        command,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )

    assert completed.returncode == 4
    assert completed.stdout == ""
    assert completed.stderr == "node_runtime_failed\n"
    assert bundle["token"] not in completed.stdout + completed.stderr


def test_sigterm_reaps_node_service_and_sidecar_within_deadline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    node_main = importlib.import_module("mycelium_node.__main__")
    pid_file = tmp_path / "process-tree.pids"
    service_script = tmp_path / "blocking-node-service.py"
    service_script.write_text(
        """from pathlib import Path
import json
import os
import signal
import subprocess
import sys
import time

sidecar = subprocess.Popen([
    sys.executable,
    "-c",
    "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(120)",
])
Path(sys.argv[1]).write_text(f"{os.getpid()} {sidecar.pid}")
for line in sys.stdin:
    request = json.loads(line)
    if request["command"] == "stop":
        time.sleep(120)
    response = {
        "command_id": request["command_id"],
        "node_id": "node-cleanup",
        "ok": True,
        "protocol": "mycelium.physical_node_control.v1",
        "result": {"route_ready": False},
        "route_ready": False,
    }
    print(json.dumps(response, sort_keys=True, separators=(",", ":")), flush=True)
"""
    )
    sidecar = tmp_path / "sidecar"
    sidecar.write_text("sidecar")
    sidecar.chmod(0o700)
    data_dir = tmp_path / "node-cleanup-state"
    data_dir.mkdir(mode=0o700)
    data_dir.chmod(0o700)
    coordinator = SeedCoordinator(
        swarm_id="swarm-node-cleanup",
        seed_node_id="seed-node",
        seed_url="http://127.0.0.1:9",
        signer=generate_ed25519_signer(endpoint_id="seed-endpoint-cleanup"),
        invite_registry=SqliteInviteRegistry(tmp_path / "seed-cleanup" / "state.sqlite3"),
        incarnation="seed-main-test",
        id_source=_ids(),
    )
    bundle_file = tmp_path / "cleanup-bundle.json"
    _write_bundle(
        bundle_file,
        coordinator.mint_invite(nonce="node-cleanup", ttl_seconds=120),
    )

    class FakeClient:
        def identity(self, *, now: float) -> dict[str, Any]:
            return {"seed_node_id": "seed-node"}

        def join(
            self,
            *,
            invite_token: str,
            join_envelope: dict[str, Any],
        ) -> dict[str, Any]:
            return {}

        def send_member_message(
            self,
            envelope: dict[str, Any],
            *,
            now: float,
        ) -> dict[str, Any]:
            return {}

    class FakeSeedHTTPClient:
        @classmethod
        def from_invite_bundle(
            cls,
            bundle: dict[str, Any],
            *,
            now: float,
        ) -> FakeClient:
            return FakeClient()

    class FakeSession:
        generation = 1

        def __init__(self, **_kwargs: Any) -> None:
            pass

        def join_request(self, **_kwargs: Any) -> dict[str, Any]:
            return {}

        def accept_join(self, *_args: Any, **_kwargs: Any) -> None:
            return None

        def heartbeat(self, **_kwargs: Any) -> dict[str, Any]:
            return {"message": {"message_id": "heartbeat-cleanup"}}

        def accept_lease_renewal(self, *_args: Any, **_kwargs: Any) -> None:
            return None

    class FakeSigner:
        endpoint_id = "node-cleanup-endpoint"

    monkeypatch.setattr(node_main, "SeedHTTPClient", FakeSeedHTTPClient)
    monkeypatch.setattr(node_main, "NodeMembershipSession", FakeSession)
    monkeypatch.setattr(
        node_main,
        "load_or_create_node_signer",
        lambda _path: FakeSigner(),
    )
    monkeypatch.setattr(
        node_main,
        "build_physical_node_command",
        lambda **_kwargs: (sys.executable, str(service_script), str(pid_file)),
    )

    timer_errors: list[str] = []

    def terminate_entrypoint() -> None:
        deadline = time.monotonic() + 10
        while not pid_file.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        if not pid_file.exists():
            timer_errors.append("node service did not start")
            return
        os.kill(os.getpid(), signal.SIGTERM)

    timer = threading.Thread(target=terminate_entrypoint, daemon=True)
    timer.start()
    cleanup_args = _node_command(
        data_dir=data_dir,
        node_id="node-cleanup",
        sidecar=sidecar,
        bundle_file=bundle_file,
    )[3:]
    cleanup_args[cleanup_args.index("--join-bundle-file")] = "--seed-invite"
    started = time.monotonic()
    result = node_main.run(cleanup_args)
    elapsed = time.monotonic() - started
    timer.join(timeout=2)
    assert timer_errors == []
    pids = [int(value) for value in pid_file.read_text().split()]
    alive_after_cleanup = [pid for pid in pids if _pid_exists(pid)]
    for pid in alive_after_cleanup:
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass

    assert result == 0
    assert elapsed < 7.0
    assert alive_after_cleanup == []
