from __future__ import annotations

import importlib
import io
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
from urllib.error import HTTPError

import pytest

from mycelium_invite import SqliteInviteRegistry
from mycelium_qualification.evidence import canonical_json_bytes
from mycelium_qualification.signing import generate_ed25519_signer
from mycelium_seed import SeedCoordinator
from mycelium_seed.http import SeedHTTPClient, SeedHTTPError, SeedHTTPServer
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
        assert ready, (
            f"process did not emit startup status; returncode={process.poll()}"
        )
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

    assert (
        node_main.run(
            _node_command(
                data_dir=data_dir,
                node_id="node-main-dry",
                sidecar=sidecar,
                bundle_file=bundle_file,
                dry_run=True,
            )[3:]
        )
        == 0
    )

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
        invite_registry=SqliteInviteRegistry(
            tmp_path / "seed-replay" / "state.sqlite3"
        ),
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
        invite_registry=SqliteInviteRegistry(
            tmp_path / "seed-runtime" / "state.sqlite3"
        ),
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
        invite_registry=SqliteInviteRegistry(
            tmp_path / "seed-cleanup" / "state.sqlite3"
        ),
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

    timer = threading.Thread(target=terminate_entrypoint, daemon=False)
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

    assert result == 0
    assert elapsed < 7.0
    assert alive_after_cleanup == []


def test_node_state_root_rejects_nonfinal_symlink_without_creation(
    tmp_path: Path,
) -> None:
    node_main = importlib.import_module("mycelium_node.__main__")
    real_parent = tmp_path / "real"
    real_parent.mkdir(mode=0o700)
    linked_parent = tmp_path / "linked"
    linked_parent.symlink_to(real_parent, target_is_directory=True)

    with pytest.raises(ValueError):
        node_main._private_directory(linked_parent / "state")

    assert not (real_parent / "state").exists()


def test_node_dry_run_accepts_absent_root_without_creating_it(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    node_main = importlib.import_module("mycelium_node.__main__")
    state_root = tmp_path / "missing" / "node"
    sidecar = tmp_path / "sidecar"
    sidecar.write_text("#!/bin/sh\nexit 0\n")
    sidecar.chmod(0o700)
    coordinator = SeedCoordinator(
        swarm_id="swarm-node-absent",
        seed_node_id="seed-node",
        seed_url="http://127.0.0.1:9",
        signer=generate_ed25519_signer(endpoint_id="seed-endpoint-absent"),
        invite_registry=SqliteInviteRegistry(
            tmp_path / "seed-absent" / "state.sqlite3"
        ),
        incarnation="seed-main-test",
        id_source=_ids(),
    )
    bundle_file = tmp_path / "absent-bundle.json"
    _write_bundle(
        bundle_file,
        coordinator.mint_invite(nonce="node-absent", ttl_seconds=120),
    )

    assert (
        node_main.run(
            _node_command(
                data_dir=state_root,
                node_id="node-absent",
                sidecar=sidecar,
                bundle_file=bundle_file,
                dry_run=True,
            )[3:]
        )
        == 0
    )
    assert not state_root.exists()
    capsys.readouterr()


def test_join_bundle_open_is_descriptor_bounded_and_never_uses_read_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    node_main = importlib.import_module("mycelium_node.__main__")
    bundle_file = tmp_path / "bundle.json"
    bundle_file.write_bytes(b"{}")
    bundle_file.chmod(0o600)

    def forbidden_read_bytes(_path: Path) -> bytes:
        raise AssertionError("pathname read crossed the descriptor boundary")

    monkeypatch.setattr(Path, "read_bytes", forbidden_read_bytes)
    assert node_main._canonical_document(bundle_file) == {}


def test_join_bundle_rejects_hardlink_symlink_oversize_and_noncanonical(
    tmp_path: Path,
) -> None:
    node_main = importlib.import_module("mycelium_node.__main__")
    original = tmp_path / "bundle.json"
    original.write_bytes(b"{}")
    original.chmod(0o600)
    hardlink = tmp_path / "hardlink.json"
    os.link(original, hardlink)
    symlink = tmp_path / "symlink.json"
    symlink.symlink_to(original)
    oversized = tmp_path / "oversized.json"
    oversized.write_bytes(b"x" * (1024 * 1024 + 1))
    oversized.chmod(0o600)
    noncanonical = tmp_path / "noncanonical.json"
    noncanonical.write_bytes(b'{ "a": 1 }')
    noncanonical.chmod(0o600)

    for candidate in (original, hardlink, symlink, oversized, noncanonical):
        with pytest.raises(ValueError):
            node_main._canonical_document(candidate)


def test_node_refuses_path_sidecar_discovery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    node_main = importlib.import_module("mycelium_node.__main__")
    sidecar = tmp_path / "mycelium-iroh-sidecar"
    sidecar.write_text("#!/bin/sh\nexit 0\n")
    sidecar.chmod(0o700)
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.setattr(node_main, "__file__", str(tmp_path / "pkg" / "__main__.py"))

    with pytest.raises(ValueError):
        node_main._sidecar_path(None)


@pytest.mark.parametrize(
    ("status", "code", "expected"),
    [
        (400, "seed_join_mismatch", True),
        (401, "invite_signature_invalid", True),
        (409, "invite_replayed", True),
        (408, "seed_join_mismatch", False),
        (400, "seed_http_remote_error", False),
        (400, "invite_registry_unavailable", False),
        (404, "seed_http_route_unknown", False),
        (500, "seed_join_mismatch", False),
        (None, "seed_http_unreachable", False),
    ],
)
def test_join_rejection_requires_authoritative_status_and_code(
    status: int | None,
    code: str,
    expected: bool,
) -> None:
    node_main = importlib.import_module("mycelium_node.__main__")
    assert node_main._join_rejected(SeedHTTPError(code, status=status)) is expected


@pytest.mark.parametrize(
    "invalid_url",
    [
        "http://user:password@seed.test:8765",
        "http://seed.test:8765?token=secret",
        "http://seed.test:8765/#secret",
        "http://seed.test:not-a-port",
        "http://seed.test:8765\\@attacker.test",
        "http:///seed.test:8765",
    ],
)
def test_seed_client_rejects_credentialed_or_ambiguous_urls(
    invalid_url: str,
) -> None:
    signer = generate_ed25519_signer(endpoint_id="seed-url-validator")
    with pytest.raises(ValueError):
        SeedHTTPClient(
            seed_url=invalid_url,
            swarm_id="swarm-url-validator",
            seed_key_digest=signer.verification_key_digest,
            seed_key_records=[signer.public_key_record()],
        )


@pytest.mark.parametrize(
    ("flag", "value"),
    [
        ("--node-id", "invalid node"),
        ("--run-id", "invalid run"),
        ("--deployment-id", "invalid deployment"),
        ("--incarnation", "invalid incarnation"),
        ("--advertise", ""),
        (
            "--advertise",
            "https://user:password@node.test/control?secret=value",
        ),
    ],
)
def test_node_dry_run_uses_real_session_and_command_validators(
    tmp_path: Path,
    flag: str,
    value: str,
) -> None:
    data_dir = tmp_path / "node-dry-invalid"
    sidecar = tmp_path / "sidecar"
    sidecar.write_text("#!/bin/sh\nexit 0\n")
    sidecar.chmod(0o700)
    coordinator = SeedCoordinator(
        swarm_id="swarm-node-dry-invalid",
        seed_node_id="seed-node",
        seed_url="http://127.0.0.1:9",
        signer=generate_ed25519_signer(endpoint_id="seed-dry-invalid"),
        invite_registry=SqliteInviteRegistry(
            tmp_path / "seed-dry-invalid" / "state.sqlite3"
        ),
        incarnation="seed-main-test",
        id_source=_ids(),
    )
    bundle_file = tmp_path / "dry-invalid-bundle.json"
    _write_bundle(
        bundle_file,
        coordinator.mint_invite(nonce="node-dry-invalid", ttl_seconds=120),
    )
    command = _node_command(
        data_dir=data_dir,
        node_id="node-dry-invalid",
        sidecar=sidecar,
        bundle_file=bundle_file,
        dry_run=True,
    )
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
    assert completed.stderr == "node_preflight_failed\n"
    assert not data_dir.exists()


def test_node_main_catches_keyerror_without_value_or_traceback(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    node_main = importlib.import_module("mycelium_node.__main__")
    secret = "secret-token-value"

    def fail() -> int:
        raise KeyError(secret)

    monkeypatch.setattr(node_main, "run", fail)
    with pytest.raises(SystemExit) as stopped:
        node_main.main()

    captured = capsys.readouterr()
    assert stopped.value.code == 4
    assert captured.out == ""
    assert captured.err == "node_runtime_failed\n"
    assert secret not in captured.err
    assert "Traceback" not in captured.err


def test_process_verified_signal_refuses_changed_or_protected_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process_module = importlib.import_module("mycelium_node.process")
    executable = process_module._ExecutableIdentity(
        path="/trusted/python",
        device=1,
        inode=2,
        mode=stat.S_IFREG | 0o755,
        uid=os.getuid(),
        size=10,
        mtime_ns=11,
        ctime_ns=12,
    )
    launch = process_module._ProcessIdentity(
        pid=4242,
        parent_pid=os.getpid(),
        process_group=4242,
        session_id=4242,
        start_token="launch",
        executable=executable,
    )

    class FakePopen:
        pid = 4242

        def poll(self) -> None:
            return None

    supervised = process_module.PhysicalNodeProcess.__new__(
        process_module.PhysicalNodeProcess
    )
    supervised._process = FakePopen()
    supervised._launch_identity = launch
    supervised.shutdown_timeout_seconds = 1.0
    sent: list[tuple[int, int]] = []
    monkeypatch.setattr(os, "killpg", lambda pgid, sig: sent.append((pgid, sig)))
    monkeypatch.setattr(
        process_module,
        "_inventory_process",
        lambda _pid: process_module._ProcessIdentity(
            pid=4242,
            parent_pid=os.getpid(),
            process_group=4242,
            session_id=4242,
            start_token="reused",
            executable=executable,
        ),
    )
    monkeypatch.setattr(
        process_module,
        "_protected_process_groups",
        lambda _deadline: {9999},
    )

    assert supervised._signal_process_group(signal.SIGKILL) is False
    assert sent == []

    monkeypatch.setattr(process_module, "_inventory_process", lambda _pid: launch)
    monkeypatch.setattr(
        process_module,
        "_protected_process_groups",
        lambda _deadline: {launch.process_group},
    )
    assert supervised._signal_process_group(signal.SIGKILL) is False
    assert sent == []


def test_reader_thread_start_failure_reaps_launched_child(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process_module = importlib.import_module("mycelium_node.process")
    executable_path = Path(sys.executable).resolve()
    metadata = executable_path.stat()
    executable = process_module._ExecutableIdentity(
        path=str(executable_path),
        device=metadata.st_dev,
        inode=metadata.st_ino,
        mode=metadata.st_mode,
        uid=metadata.st_uid,
        size=metadata.st_size,
        mtime_ns=metadata.st_mtime_ns,
        ctime_ns=metadata.st_ctime_ns,
    )
    identity = process_module._ProcessIdentity(
        pid=4343,
        parent_pid=os.getpid(),
        process_group=4343,
        session_id=4343,
        start_token="launch",
        executable=executable,
    )

    class FakePopen:
        pid = 4343
        stdin = io.BytesIO()
        stdout = io.BytesIO()
        stderr = io.BytesIO()

        def __init__(self, *_args: Any, **kwargs: Any) -> None:
            if kwargs:
                assert kwargs["start_new_session"] is True
            self.returncode: int | None = None

        def poll(self) -> int | None:
            return self.returncode

        def wait(self, timeout: float | None = None) -> int:
            if self.returncode is None:
                raise subprocess.TimeoutExpired("fake", timeout)
            return self.returncode

    starts = 0

    class FakeThread:
        def __init__(self, **kwargs: Any) -> None:
            assert kwargs["daemon"] is False
            self.started = False

        def start(self) -> None:
            nonlocal starts
            starts += 1
            if starts == 2:
                raise RuntimeError("thread-start-secret")
            self.started = True

        def join(self, timeout: float | None = None) -> None:
            return None

        def is_alive(self) -> bool:
            return False

    sent: list[tuple[int, int]] = []

    def kill_group(pgid: int, sig: int) -> None:
        sent.append((pgid, sig))
        fake_process.returncode = -sig

    fake_process = FakePopen()
    monkeypatch.setattr(
        process_module.subprocess, "Popen", lambda *_a, **_k: fake_process
    )
    monkeypatch.setattr(process_module.threading, "Thread", FakeThread)
    monkeypatch.setattr(process_module, "_inventory_process", lambda _pid: identity)
    monkeypatch.setattr(
        process_module,
        "_protected_process_groups",
        lambda _deadline: {os.getpgrp()},
    )
    monkeypatch.setattr(os, "killpg", kill_group)

    with pytest.raises(process_module.NodeProcessError) as failed:
        process_module.PhysicalNodeProcess(
            command=(str(executable_path), "-c", "pass"),
            node_id="node-thread-start",
            run_id="run-thread-start",
            deployment_id="deployment-thread-start",
            response_timeout_seconds=0.01,
            shutdown_timeout_seconds=0.1,
        )

    assert failed.value.code == "node_process_start_failed"
    assert sent
    assert all(pgid == identity.process_group for pgid, _sig in sent)
    assert fake_process.returncode is not None


def test_join_bundle_rejects_path_replacement_during_descriptor_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    node_main = importlib.import_module("mycelium_node.__main__")
    bundle_file = tmp_path / "bundle.json"
    replacement = tmp_path / "replacement.json"
    bundle_file.write_bytes(b"{}")
    replacement.write_bytes(b"{}")
    bundle_file.chmod(0o600)
    replacement.chmod(0o600)
    real_read = os.read
    replaced = False

    def replace_then_read(descriptor: int, size: int) -> bytes:
        nonlocal replaced
        if not replaced:
            os.replace(replacement, bundle_file)
            replaced = True
        return real_read(descriptor, size)

    monkeypatch.setattr(os, "read", replace_then_read)
    with pytest.raises(ValueError):
        node_main._canonical_document(bundle_file)


def test_node_parser_rejects_file_and_stdin_bundle_sources(
    tmp_path: Path,
) -> None:
    sidecar = tmp_path / "sidecar"
    sidecar.write_text("#!/bin/sh\nexit 0\n")
    sidecar.chmod(0o700)
    bundle = tmp_path / "bundle.json"
    bundle.write_bytes(b"{}")
    bundle.chmod(0o600)
    command = _node_command(
        data_dir=tmp_path / "node",
        node_id="node-parser",
        sidecar=sidecar,
        bundle_file=bundle,
        bundle_stdin=True,
    )

    completed = subprocess.run(
        command,
        input="{}",
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    assert completed.returncode == 2
    assert completed.stdout == ""
    assert completed.stderr == "node_preflight_failed\n"


def test_sidecar_replacement_is_rejected_before_popen(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process_module = importlib.import_module("mycelium_node.process")
    sidecar = tmp_path / "sidecar"
    replacement = tmp_path / "replacement"
    sidecar.write_text("#!/bin/sh\nexit 0\n")
    replacement.write_text("#!/bin/sh\nexit 1\n")
    sidecar.chmod(0o700)
    replacement.chmod(0o700)
    identity = process_module.capture_executable_identity(
        sidecar,
        require_canonical=True,
        require_private_owner=True,
    )
    os.replace(replacement, sidecar)
    popen_calls = 0

    def forbidden_popen(*_args: Any, **_kwargs: Any) -> Any:
        nonlocal popen_calls
        popen_calls += 1
        raise AssertionError("replaced executable reached Popen")

    monkeypatch.setattr(process_module.subprocess, "Popen", forbidden_popen)
    with pytest.raises(process_module.NodeProcessError) as failed:
        process_module.PhysicalNodeProcess(
            command=(sys.executable, "-c", "pass"),
            node_id="node-sidecar-replaced",
            run_id="run-sidecar-replaced",
            deployment_id="deployment-sidecar-replaced",
            expected_executables=(identity,),
        )

    assert failed.value.code == "node_process_executable_changed"
    assert popen_calls == 0


def test_temporary_root_cleanup_refuses_path_replacement(tmp_path: Path) -> None:
    node_main = importlib.import_module("mycelium_node.__main__")
    original = node_main._temporary_root()
    moved = tmp_path / "moved-original"
    original.path.rename(moved)
    original.path.mkdir(mode=0o700)

    with pytest.raises(RuntimeError):
        node_main._remove_temporary_root(original)

    assert original.path.is_dir()
    assert moved.is_dir()
    original.path.rmdir()
    moved.rmdir()


def test_process_group_signal_reinventories_after_protected_group_discovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process_module = importlib.import_module("mycelium_node.process")
    executable = process_module._ExecutableIdentity(
        path="/trusted/python",
        device=1,
        inode=2,
        mode=stat.S_IFREG | 0o755,
        uid=os.getuid(),
        size=10,
        mtime_ns=11,
        ctime_ns=12,
    )
    launch = process_module._ProcessIdentity(
        pid=4242,
        parent_pid=3131,
        process_group=4242,
        session_id=4242,
        start_token="launch",
        executable=executable,
    )
    drifted = process_module._ProcessIdentity(
        pid=4242,
        parent_pid=3131,
        process_group=4242,
        session_id=4242,
        start_token="reused-during-protected-discovery",
        executable=executable,
    )
    calls: list[object] = []
    current = [launch]
    drift_on_discovery = [True]

    class FakePopen:
        pid = launch.pid

        def poll(self) -> None:
            calls.append("poll")
            return None

    def protected_groups(deadline: float | None = None) -> set[int]:
        calls.append(("protected", deadline))
        if drift_on_discovery[0]:
            current[0] = drifted
        return {9999}

    def inventory(pid: int) -> Any:
        calls.append(("inventory", pid))
        return current[0]

    def kill_group(pgid: int, signum: int) -> None:
        calls.append(("killpg", pgid, signum))

    supervised = process_module.PhysicalNodeProcess.__new__(
        process_module.PhysicalNodeProcess
    )
    supervised._process = FakePopen()
    supervised._launch_identity = launch
    supervised.shutdown_timeout_seconds = 1.0
    monkeypatch.setattr(
        process_module,
        "_protected_process_groups",
        protected_groups,
    )
    monkeypatch.setattr(process_module, "_inventory_process", inventory)
    monkeypatch.setattr(os, "killpg", kill_group)

    assert supervised._signal_process_group(signal.SIGKILL) is False
    assert calls[0] == "poll"
    assert calls[1][0] == "protected"
    assert isinstance(calls[1][1], float)
    assert calls[2:] == [("inventory", launch.pid)]

    calls.clear()
    current[0] = launch
    drift_on_discovery[0] = False
    assert supervised._signal_process_group(signal.SIGKILL) is True
    assert calls[0] == "poll"
    assert calls[1][0] == "protected"
    assert isinstance(calls[1][1], float)
    assert calls[2:] == [
        ("inventory", launch.pid),
        "poll",
        ("killpg", launch.process_group, signal.SIGKILL),
    ]


def test_seed_http_client_only_exposes_exact_authoritative_error_envelope() -> None:
    seed_http = importlib.import_module("mycelium_seed.http")
    signer = generate_ed25519_signer(endpoint_id="seed-error-envelope")
    client = SeedHTTPClient(
        seed_url="http://seed.test:8765",
        swarm_id="swarm-error-envelope",
        seed_key_digest=signer.verification_key_digest,
        seed_key_records=[signer.public_key_record()],
    )

    def rejected(raw: bytes) -> SeedHTTPError:
        class ErrorOpener:
            def open(self, *_args: Any, **_kwargs: Any) -> Any:
                raise HTTPError(
                    "http://seed.test:8765/seed/join",
                    400,
                    "rejected",
                    {},
                    io.BytesIO(raw),
                )

        client._opener = ErrorOpener()
        with pytest.raises(SeedHTTPError) as caught:
            client._request("POST", "/seed/join", {})
        return caught.value

    authoritative = canonical_json_bytes(
        {
            "protocol": seed_http.SEED_HTTP_ERROR_PROTOCOL,
            "error": {"code": "seed_join_mismatch"},
        }
    )
    exposed = rejected(authoritative)
    assert (exposed.status, exposed.code) == (400, "seed_join_mismatch")
    node_main = importlib.import_module("mycelium_node.__main__")
    assert node_main._join_rejected(exposed) is True

    malformed = [
        canonical_json_bytes({"error": {"code": "seed_join_mismatch"}}),
        canonical_json_bytes(
            {
                "protocol": seed_http.SEED_HTTP_ERROR_PROTOCOL,
                "error": {"code": "seed_join_mismatch"},
                "extra": False,
            }
        ),
        canonical_json_bytes(
            {
                "protocol": "mycelium.seed.http_error.v2",
                "error": {"code": "seed_join_mismatch"},
            }
        ),
        canonical_json_bytes(
            {
                "protocol": seed_http.SEED_HTTP_ERROR_PROTOCOL,
                "error": "seed_join_mismatch",
            }
        ),
        canonical_json_bytes(
            {
                "protocol": seed_http.SEED_HTTP_ERROR_PROTOCOL,
                "error": {"code": "seed_join_mismatch", "extra": None},
            }
        ),
        canonical_json_bytes(
            {
                "protocol": seed_http.SEED_HTTP_ERROR_PROTOCOL,
                "error": {},
            }
        ),
        canonical_json_bytes(
            {
                "protocol": seed_http.SEED_HTTP_ERROR_PROTOCOL,
                "error": {"code": 3},
            }
        ),
        canonical_json_bytes(
            {
                "protocol": seed_http.SEED_HTTP_ERROR_PROTOCOL,
                "error": {"code": "Seed_Join_Mismatch"},
            }
        ),
        (
            b'{"protocol": "mycelium.seed.http_error.v1",'
            b'"error":{"code":"seed_join_mismatch"}}'
        ),
        canonical_json_bytes(
            {
                "protocol": seed_http.SEED_HTTP_ERROR_PROTOCOL,
                "error": {"code": "a" * seed_http.MAX_HTTP_FRAME_BYTES},
            }
        ),
    ]
    for raw in malformed:
        hidden = rejected(raw)
        assert (hidden.status, hidden.code) == (400, "seed_http_remote_error")
        assert node_main._join_rejected(hidden) is False


def test_heartbeat_shape_validator_rejects_noncanonical_scheduled_shapes() -> None:
    membership = importlib.import_module("mycelium_node.membership")
    valid = {
        "lifecycle_state": "NEW",
        "active_requests": 0,
        "route_ready": False,
        "liveness_source": "scheduled_heartbeat",
        "activity_receipt_digest": None,
        "activity_peer_node_id": None,
    }
    membership.validate_heartbeat_shape(**valid)

    invalid = [
        {"lifecycle_state": "FAILED"},
        {"active_requests": -1},
        {"active_requests": True},
        {"active_requests": 0.0},
        {"route_ready": True},
        {"route_ready": 0},
        {"liveness_source": "activation_receipt"},
        {"activity_receipt_digest": "sha256:" + "0" * 64},
        {"activity_peer_node_id": "peer-node"},
    ]
    for change in invalid:
        with pytest.raises(ValueError):
            membership.validate_heartbeat_shape(**{**valid, **change})


def test_heartbeat_validator_delegates_before_mutation_and_during_dry_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    membership = importlib.import_module("mycelium_node.membership")
    node_main = importlib.import_module("mycelium_node.__main__")
    calls: list[dict[str, Any]] = []
    side_effects: list[str] = []

    class ShapeObserved(RuntimeError):
        pass

    def stop_after_shape(**shape: Any) -> None:
        calls.append(shape)
        raise ShapeObserved

    monkeypatch.setattr(membership, "validate_heartbeat_shape", stop_after_shape)
    session = membership.NodeMembershipSession.__new__(
        membership.NodeMembershipSession
    )
    session._now = lambda: side_effects.append("clock")
    with pytest.raises(ShapeObserved):
        session._emit_liveness(
            lifecycle_state="NEW",
            active_requests=0,
            liveness_source="scheduled_heartbeat",
            activity_receipt_digest=None,
            activity_peer_node_id=None,
        )
    assert side_effects == []
    assert calls == [
        {
            "lifecycle_state": "NEW",
            "active_requests": 0,
            "route_ready": False,
            "liveness_source": "scheduled_heartbeat",
            "activity_receipt_digest": None,
            "activity_peer_node_id": None,
        }
    ]

    original_validator = membership.validate_heartbeat_shape

    def record_shape(**shape: Any) -> None:
        calls.append(shape)
        valid_shape = {
            "lifecycle_state": "NEW",
            "active_requests": 0,
            "route_ready": False,
            "liveness_source": "scheduled_heartbeat",
            "activity_receipt_digest": None,
            "activity_peer_node_id": None,
        }
        if shape != valid_shape:
            raise AssertionError("unexpected preflight heartbeat shape")

    monkeypatch.setattr(membership, "validate_heartbeat_shape", record_shape)
    data_dir = tmp_path / "node-heartbeat-dry"
    sidecar = tmp_path / "sidecar"
    sidecar.write_text("#!/bin/sh\nexit 0\n")
    sidecar.chmod(0o700)
    coordinator = SeedCoordinator(
        swarm_id="swarm-heartbeat-dry",
        seed_node_id="seed-node",
        seed_url="http://127.0.0.1:9",
        signer=generate_ed25519_signer(endpoint_id="seed-heartbeat-dry"),
        invite_registry=SqliteInviteRegistry(
            tmp_path / "seed-heartbeat-dry" / "state.sqlite3"
        ),
        incarnation="seed-main-test",
        id_source=_ids(),
    )
    bundle_file = tmp_path / "heartbeat-dry-bundle.json"
    _write_bundle(
        bundle_file,
        coordinator.mint_invite(nonce="heartbeat-dry", ttl_seconds=120),
    )

    assert (
        node_main.run(
            _node_command(
                data_dir=data_dir,
                node_id="node-heartbeat-dry",
                sidecar=sidecar,
                bundle_file=bundle_file,
                dry_run=True,
            )[3:]
        )
        == 0
    )
    assert len(calls) == 2
    assert not data_dir.exists()
    capsys.readouterr()
    assert original_validator is stop_after_shape
