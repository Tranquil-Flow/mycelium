from __future__ import annotations

import errno
import gc
import importlib
import io
from email.message import Message
from itertools import count
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
import time
from typing import Any
from urllib.error import HTTPError
from urllib.parse import urlsplit
import weakref

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


class _WeakReferenceableCloseError(OSError):
    def __init__(
        self,
        references: list[weakref.ReferenceType[_WeakReferenceableCloseError]],
        private_marker: object,
        *,
        private_errno: int = errno.EIO,
        private_path: str | None = None,
        private_secret: str = "tracked close failure",
    ) -> None:
        if private_path is None:
            super().__init__(private_errno, private_secret)
        else:
            super().__init__(private_errno, private_secret, private_path)
        self.private_marker = private_marker
        references.append(weakref.ref(self))


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


def test_join_rejection_requires_complete_canonical_status_code_matrix() -> None:
    node_main = importlib.import_module("mycelium_node.__main__")
    seed_http = importlib.import_module("mycelium_seed.http")
    canonical_codes = {
        400: {
            "invite_expired",
            "invite_malformed",
            "invite_protocol_invalid",
            "join_request_protocol_required",
            "membership_endpoint_addr_invalid",
            "membership_endpoint_id_mismatch",
            "membership_envelope_invalid",
            "membership_field_unusable",
            "membership_fields_invalid",
            "membership_generation_invalid",
            "membership_identifier_invalid",
            "membership_join_generation_invalid",
            "membership_message_expired",
            "membership_message_from_future",
            "membership_message_invalid",
            "membership_peer_class_invalid",
            "membership_protocol_invalid",
            "membership_runtime_capability_invalid",
            "membership_runtime_capability_mismatch",
            "membership_text_invalid",
            "membership_time_invalid",
            "membership_ttl_invalid",
            "membership_verifier_invalid",
            "seed_join_key_invalid",
            "seed_join_mismatch",
            "seed_join_retry_mismatch",
            "seed_member_identity_reused",
            "seed_node_endpoint_conflict",
        },
        401: {
            "invite_signature_invalid",
            "membership_key_pin_invalid",
            "membership_signature_invalid",
        },
        409: {
            "invite_replayed",
            "seed_node_key_conflict",
        },
    }
    explicitly_allowed = frozenset(seed_http.JOIN_ROUTE_ERROR_STATUSES)
    assert explicitly_allowed == frozenset().union(*canonical_codes.values())

    received_statuses = (None, 400, 401, 404, 409, 500)
    for canonical_status, codes in canonical_codes.items():
        for code in codes:
            assert int(seed_http._error_status(code)) == canonical_status
            for received_status in received_statuses:
                assert node_main._join_rejected(
                    SeedHTTPError(code, status=received_status)
                ) is (received_status == canonical_status)

    for unknown_code in (
        "invite_registry_unavailable",
        "seed_http_remote_error",
        "seed_http_route_unknown",
        "seed_http_unreachable",
    ):
        for received_status in received_statuses:
            assert (
                node_main._join_rejected(
                    SeedHTTPError(unknown_code, status=received_status)
                )
                is False
            )


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
    monkeypatch.setattr(
        process_module,
        "_await_pre_exec_launcher",
        lambda *_args: None,
    )
    monkeypatch.setattr(
        process_module,
        "_release_pre_exec_launcher",
        lambda *_args: None,
    )
    monkeypatch.setattr(
        process_module,
        "_await_target_exec",
        lambda *_args: None,
    )
    monkeypatch.setattr(process_module.threading, "Thread", FakeThread)
    monkeypatch.setattr(process_module, "_inventory_process", lambda _pid: identity)
    member = process_module._ProcessGroupMember(
        pid=identity.pid,
        process_group=identity.process_group,
        session_id=identity.session_id,
    )
    monkeypatch.setattr(
        process_module,
        "_inventory_process_group",
        lambda *_args: () if fake_process.returncode is not None else (member,),
    )
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
        parent_pid=os.getpid(),
        process_group=4242,
        session_id=4242,
        start_token="launch",
        executable=executable,
    )
    drifted = process_module._ProcessIdentity(
        pid=4242,
        parent_pid=os.getpid(),
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
    assert calls[0][0] == "protected"
    assert isinstance(calls[0][1], float)
    assert calls[1:] == ["poll", ("inventory", launch.pid)]

    calls.clear()
    current[0] = launch
    drift_on_discovery[0] = False
    assert supervised._signal_process_group(signal.SIGKILL) is True
    assert calls[0][0] == "protected"
    assert isinstance(calls[0][1], float)
    assert calls[1:] == [
        "poll",
        ("inventory", launch.pid),
        ("killpg", launch.process_group, signal.SIGKILL),
    ]


def test_process_group_signal_poll_consuming_deadline_prevents_action(
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
    clock = [10.0]
    calls: list[object] = []

    class FakePopen:
        pid = launch.pid

        def poll(self) -> None:
            calls.append("poll")
            if calls and isinstance(calls[0], tuple):
                clock[0] = 11.0
            return None

    supervised = process_module.PhysicalNodeProcess.__new__(
        process_module.PhysicalNodeProcess
    )
    supervised._process = FakePopen()
    supervised._launch_identity = launch
    supervised.shutdown_timeout_seconds = 1.0
    monkeypatch.setattr(process_module.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(
        process_module,
        "_protected_process_groups",
        lambda deadline: calls.append(("protected", deadline)) or {9999},
    )
    monkeypatch.setattr(
        process_module,
        "_inventory_process",
        lambda pid: calls.append(("inventory", pid)) or launch,
    )
    monkeypatch.setattr(
        os,
        "killpg",
        lambda pgid, signum: calls.append(("killpg", pgid, signum)),
    )

    assert supervised._signal_process_group(signal.SIGKILL, deadline=11.0) is False
    assert calls == [("protected", 11.0), "poll"]


def test_process_group_signal_poll_error_prevents_action(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process_module = importlib.import_module("mycelium_node.process")
    calls: list[object] = []
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
            calls.append("poll")
            raise RuntimeError("poll failed")

    supervised = process_module.PhysicalNodeProcess.__new__(
        process_module.PhysicalNodeProcess
    )
    supervised._process = FakePopen()
    supervised._launch_identity = launch
    supervised.shutdown_timeout_seconds = 1.0
    monkeypatch.setattr(
        process_module,
        "_protected_process_groups",
        lambda deadline: calls.append(("protected", deadline)) or {9999},
    )
    monkeypatch.setattr(
        process_module,
        "_inventory_process",
        lambda pid: calls.append(("inventory", pid)),
    )
    monkeypatch.setattr(
        os,
        "killpg",
        lambda pgid, signum: calls.append(("killpg", pgid, signum)),
    )

    assert supervised._signal_process_group(signal.SIGKILL, deadline=11.0) is False
    assert calls == [("protected", 11.0), "poll"]


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
                headers = Message()
                headers.add_header("Content-Type", "application/json")
                raise HTTPError(
                    "http://seed.test:8765/seed/join",
                    400,
                    "rejected",
                    headers,
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
        {"liveness_source": "unknown_source"},
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


def test_seed_http_client_requires_one_exact_json_response_content_type() -> None:
    seed_http = importlib.import_module("mycelium_seed.http")
    signer = generate_ed25519_signer(endpoint_id="seed-content-type")
    client = SeedHTTPClient(
        seed_url="http://seed.test:8765",
        swarm_id="swarm-content-type",
        seed_key_digest=signer.verification_key_digest,
        seed_key_records=[signer.public_key_record()],
    )
    body = canonical_json_bytes(
        {
            "protocol": seed_http.SEED_HTTP_ERROR_PROTOCOL,
            "error": {"code": "seed_join_mismatch"},
        }
    )

    def headers(values: tuple[str, ...]) -> Message:
        result = Message()
        for value in values:
            result.add_header("Content-Type", value)
        return result

    def rejected(content_types: tuple[str, ...]) -> SeedHTTPError:
        class ErrorOpener:
            def open(self, *_args: Any, **_kwargs: Any) -> Any:
                raise HTTPError(
                    "http://seed.test:8765/seed/join",
                    400,
                    "rejected",
                    headers(content_types),
                    io.BytesIO(body),
                )

        client._opener = ErrorOpener()
        with pytest.raises(SeedHTTPError) as caught:
            client._request("POST", "/seed/join", {})
        return caught.value

    exact = rejected(("application/json",))
    assert (exact.status, exact.code) == (400, "seed_join_mismatch")

    invalid_types = (
        (),
        ("text/plain",),
        ("application/json; charset=utf-8",),
        ("application/json", "application/json"),
    )
    for content_types in invalid_types:
        hidden = rejected(content_types)
        assert (hidden.status, hidden.code) == (400, "seed_http_remote_error")
        assert "seed_join_mismatch" not in str(hidden)

        class SuccessResponse:
            status = 200

            def __init__(self) -> None:
                self.headers = headers(content_types)

            def read(self, _size: int) -> bytes:
                return body

            def __enter__(self) -> "SuccessResponse":
                return self

            def __exit__(self, *_args: Any) -> None:
                return None

        class SuccessOpener:
            def open(self, *_args: Any, **_kwargs: Any) -> SuccessResponse:
                return SuccessResponse()

        client._opener = SuccessOpener()
        with pytest.raises(SeedHTTPError) as caught:
            client._request("GET", "/seed/identity")
        assert (caught.value.status, caught.value.code) == (
            200,
            "seed_http_remote_error",
        )
        assert "seed_join_mismatch" not in str(caught.value)


def test_join_route_uses_one_exact_immutable_error_status_vocabulary(
    tmp_path: Path,
) -> None:
    node_main = importlib.import_module("mycelium_node.__main__")
    membership = importlib.import_module("mycelium_node.membership")
    seed_http = importlib.import_module("mycelium_seed.http")
    coordinator_module = importlib.import_module("mycelium_seed.coordinator")
    canonical_codes = {
        400: {
            "invite_expired",
            "invite_malformed",
            "invite_protocol_invalid",
            "join_request_protocol_required",
            "membership_endpoint_addr_invalid",
            "membership_endpoint_id_mismatch",
            "membership_envelope_invalid",
            "membership_field_unusable",
            "membership_fields_invalid",
            "membership_generation_invalid",
            "membership_identifier_invalid",
            "membership_join_generation_invalid",
            "membership_message_expired",
            "membership_message_from_future",
            "membership_message_invalid",
            "membership_peer_class_invalid",
            "membership_protocol_invalid",
            "membership_runtime_capability_invalid",
            "membership_runtime_capability_mismatch",
            "membership_text_invalid",
            "membership_time_invalid",
            "membership_ttl_invalid",
            "membership_verifier_invalid",
            "seed_join_key_invalid",
            "seed_join_mismatch",
            "seed_join_retry_mismatch",
            "seed_member_identity_reused",
            "seed_node_endpoint_conflict",
        },
        401: {
            "invite_signature_invalid",
            "membership_key_pin_invalid",
            "membership_signature_invalid",
        },
        409: {
            "invite_replayed",
            "seed_node_key_conflict",
        },
    }
    expected = {
        code: status
        for status, codes in canonical_codes.items()
        for code in codes
    }
    authoritative = seed_http.JOIN_ROUTE_ERROR_STATUSES
    assert dict(authoritative) == expected
    with pytest.raises(TypeError):
        authoritative["not_authoritative"] = 400

    emitted: list[tuple[int, dict[str, Any]]] = []
    seed_url = "http://127.0.0.1:8765"
    seed_signer = generate_ed25519_signer(endpoint_id="seed-route-emission")
    coordinator = SeedCoordinator(
        swarm_id="swarm-route-emission",
        seed_node_id="seed-node",
        seed_url=seed_url,
        signer=seed_signer,
        invite_registry=SqliteInviteRegistry(
            tmp_path / "route-emission" / "state.sqlite3"
        ),
        incarnation="seed-route-emission",
        id_source=_ids(),
    )
    bundle = coordinator.mint_invite(nonce="route-emission", ttl_seconds=120)
    verified_bundle = node_main.verify_invite_bundle(bundle, now=time.time())
    node = membership.NodeMembershipSession(
        node_id="node-route-emission",
        swarm_id="swarm-route-emission",
        seed_node_id="seed-node",
        signer=generate_ed25519_signer(endpoint_id="node-route-emission-endpoint"),
        incarnation="node-route-emission",
        software_version="test",
        peer_class="mac_mlx_iroh",
        runtime_capability={
            "runtime_backend": "mlx",
            "transport": "iroh",
            "activation_protocol": "mycelium.router_wire.v1",
        },
        id_source=_ids(),
    )
    join_request = node.join_request(
        invite_nonce=verified_bundle["payload"]["nonce"],
        endpoint_addrs=["https://node-route-emission.test/control"],
    )
    join_request["verification_key"]["verification_key_digest"] = ""
    real_handler = object.__new__(seed_http._SeedRequestHandler)
    real_handler.path = "/seed/join"
    real_handler.server = type(
        "RealRouteServer",
        (),
        {"coordinator": coordinator},
    )()
    real_handler._read_body = lambda: {
        "protocol": seed_http.SEED_JOIN_HTTP_PROTOCOL,
        "invite_token": bundle["token"],
        "join_envelope": join_request,
    }
    real_handler._send = lambda status, value: emitted.append(
        (int(status), dict(value))
    )
    real_handler.do_POST()
    assert emitted.pop() == (
        401,
        {
            "protocol": seed_http.SEED_HTTP_ERROR_PROTOCOL,
            "error": {"code": "membership_key_pin_invalid"},
        },
    )

    class RouteCoordinator:
        def __init__(self, code: str) -> None:
            self.code = code

        def accept_join(self, **_kwargs: Any) -> None:
            raise coordinator_module.SeedCoordinatorError(self.code)

    for code, canonical_status in expected.items():
        handler = object.__new__(seed_http._SeedRequestHandler)
        handler.path = "/seed/join"
        handler.server = type(
            "RouteServer",
            (),
            {"coordinator": RouteCoordinator(code)},
        )()
        handler._read_body = lambda: {
            "protocol": seed_http.SEED_JOIN_HTTP_PROTOCOL,
            "invite_token": "opaque",
            "join_envelope": {},
        }
        handler._send = lambda status, value: emitted.append(
            (int(status), dict(value))
        )
        handler.do_POST()
        assert emitted.pop() == (
            canonical_status,
            {
                "protocol": seed_http.SEED_HTTP_ERROR_PROTOCOL,
                "error": {"code": code},
            },
        )
        assert int(seed_http._error_status(code)) == canonical_status
        for received_status in (None, 400, 401, 404, 409, 500):
            assert node_main._join_rejected(
                SeedHTTPError(code, status=received_status)
            ) is (received_status == canonical_status)

    assert int(authoritative["membership_key_pin_invalid"]) == 401
    for excluded in (
        "invite_field_invalid",
        "membership_integer_invalid",
        "membership_sender_endpoint_mismatch",
        "membership_signer_endpoint_mismatch",
        "membership_swarm_mismatch",
        "membership_key_pin_mismatch",
        "seed_join_invite_replayed",
    ):
        assert excluded not in authoritative
    for unknown in (
        "",
        "invite_registry_unavailable",
        "seed_http_remote_error",
        "seed_http_route_unknown",
        "seed_http_unreachable",
        "unknown_join_error",
    ):
        for received_status in (None, 400, 401, 404, 409, 500):
            assert (
                node_main._join_rejected(
                    SeedHTTPError(unknown, status=received_status)
                )
                is False
            )


def test_working_directory_restore_retries_and_preserves_primary_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process_module = importlib.import_module("mycelium_node.process")
    inventory_root = Path("/proc/self/fd")
    if not inventory_root.is_dir():
        inventory_root = Path("/dev/fd")

    def inventory() -> set[int]:
        return {
            int(name)
            for name in os.listdir(inventory_root)
            if name.isdigit()
        }

    original_path = Path.cwd()
    original_identity = os.stat(".")
    safety_fd = os.open(".", os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    real_fchdir = os.fchdir
    before = inventory()
    first_root = tmp_path / "first-root"
    second_root = tmp_path / "second-root"
    first_root.mkdir(mode=0o700)
    second_root.mkdir(mode=0o700)
    first_lease = process_module.private_directory_lease(first_root)
    second_lease = process_module.private_directory_lease(second_root)
    try:
        calls: list[int] = []

        def fail_first_restore(descriptor: int) -> None:
            calls.append(descriptor)
            if len(calls) == 2:
                raise OSError(errno.EIO, "restore-value-must-not-leak")
            real_fchdir(descriptor)

        monkeypatch.setattr(os, "fchdir", fail_first_restore)
        retry_failure: BaseException | None = None
        try:
            with first_lease.working_directory():
                assert os.fstat(first_lease._descriptor)[:2] == os.stat(".")[:2]
        except BaseException as exc:
            retry_failure = exc
        finally:
            real_fchdir(safety_fd)
        assert retry_failure is None
        assert len(calls) == 3
        assert Path.cwd() == original_path
        assert os.stat(".")[:2] == original_identity[:2]

        body_failure = RuntimeError("authoritative-body-failure")
        restore_calls = 0

        def fail_every_restore(descriptor: int) -> None:
            nonlocal restore_calls
            restore_calls += 1
            if restore_calls == 1:
                real_fchdir(descriptor)
                return
            raise OSError(errno.EIO, "restore-secret-value")

        monkeypatch.setattr(os, "fchdir", fail_every_restore)
        caught_body: BaseException | None = None
        try:
            with first_lease.working_directory():
                raise body_failure
        except BaseException as exc:
            caught_body = exc
        finally:
            real_fchdir(safety_fd)
        assert caught_body is body_failure
        assert getattr(caught_body, "__notes__", ()) == [
            "working directory restoration failed"
        ]
        assert "restore-secret-value" not in str(caught_body)

        restore_calls = 0
        restoration_failure: BaseException | None = None
        try:
            with first_lease.working_directory():
                pass
        except BaseException as exc:
            restoration_failure = exc
        finally:
            real_fchdir(safety_fd)
        assert type(restoration_failure) is ValueError
        assert str(restoration_failure) == "working directory restoration failed"
        assert "restore-secret-value" not in str(restoration_failure)

        monkeypatch.setattr(os, "fchdir", real_fchdir)
        with first_lease.working_directory():
            assert os.stat(".")[:2] == os.fstat(first_lease._descriptor)[:2]
            with second_lease.working_directory():
                assert os.stat(".")[:2] == os.fstat(second_lease._descriptor)[:2]
            assert os.stat(".")[:2] == os.fstat(first_lease._descriptor)[:2]
        assert Path.cwd() == original_path

        begin = threading.Barrier(3)
        observed: list[tuple[int, int]] = []

        def visit(lease: Any) -> None:
            begin.wait()
            with lease.working_directory():
                observed.append(os.stat(".")[:2])

        visitors = [
            threading.Thread(target=visit, args=(lease,), daemon=False)
            for lease in (first_lease, second_lease)
        ]
        for visitor in visitors:
            visitor.start()
        begin.wait()
        for visitor in visitors:
            visitor.join(timeout=2.0)
        assert all(not visitor.is_alive() for visitor in visitors)
        assert sorted(observed) == sorted(
            [
                os.fstat(first_lease._descriptor)[:2],
                os.fstat(second_lease._descriptor)[:2],
            ]
        )
        assert Path.cwd() == original_path
    finally:
        real_fchdir(safety_fd)
        monkeypatch.setattr(os, "fchdir", real_fchdir)
        first_lease.close()
        second_lease.close()
        os.close(safety_fd)
    assert len(inventory()) == len(before) - 1


def test_activation_liveness_shape_is_validated_before_every_side_effect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    membership = importlib.import_module("mycelium_node.membership")
    valid = {
        "lifecycle_state": "RUNNING",
        "active_requests": 2,
        "route_ready": False,
        "liveness_source": "activation_receipt",
        "activity_receipt_digest": "sha256:" + "a" * 64,
        "activity_peer_node_id": "peer-node",
    }
    membership.validate_heartbeat_shape(**valid)

    invalid = [
        {"lifecycle_state": "FAILED"},
        {"active_requests": -1},
        {"active_requests": True},
        {"active_requests": 2.0},
        {"route_ready": True},
        {"liveness_source": "unknown-source"},
        {"activity_receipt_digest": "secret-invalid-digest"},
        {"activity_peer_node_id": None},
        {"activity_peer_node_id": " secret-peer "},
    ]
    for change in invalid:
        with pytest.raises(ValueError) as caught:
            membership.validate_heartbeat_shape(**{**valid, **change})
        assert "secret-" not in str(caught.value)

    calls: list[str] = []
    pending = {"pending": ("scheduled_heartbeat", 200.0)}
    receipts = {"sha256:" + "b" * 64: 200.0}
    session = membership.NodeMembershipSession.__new__(
        membership.NodeMembershipSession
    )
    session._pending_liveness = dict(pending)
    session._activity_receipts = dict(receipts)
    session._heartbeat_sequence = 7
    session._now = lambda: calls.append("clock") or 100.0
    session._post_join_common = (
        lambda _protocol: calls.append("message-id")
        or {"message_id": "new-message", "expires_at": 200.0}
    )
    session.signer = object()

    def sign_message(**kwargs: Any) -> dict[str, Any]:
        calls.append("signer")
        return {"message": kwargs["message"]}

    monkeypatch.setattr(membership, "sign_membership_message", sign_message)
    with pytest.raises(ValueError) as caught:
        session._emit_liveness(
            lifecycle_state="RUNNING",
            active_requests=2,
            liveness_source="activation_receipt",
            activity_receipt_digest="secret-invalid-digest",
            activity_peer_node_id="peer-node",
        )
    assert str(caught.value) == "heartbeat activity shape is invalid"
    assert calls == []
    assert session._heartbeat_sequence == 7
    assert session._pending_liveness == pending
    assert session._activity_receipts == receipts


@pytest.mark.parametrize(
    "invalid_source",
    [
        pytest.param([], id="list"),
        pytest.param({}, id="dict"),
        pytest.param(set(), id="set"),
        pytest.param(
            type("LivenessSource", (str,), {})("scheduled_heartbeat"),
            id="str-subclass",
        ),
        pytest.param(None, id="none"),
        pytest.param(0, id="integer"),
        pytest.param(object(), id="object"),
    ],
)
def test_liveness_source_requires_exact_str_before_every_side_effect(
    invalid_source: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    membership = importlib.import_module("mycelium_node.membership")
    events: list[str] = []
    pending = {"pending": ("scheduled_heartbeat", 200.0)}
    receipts = {"sha256:" + "b" * 64: 200.0}

    class ObservedMap(dict[Any, Any]):
        def items(self) -> Any:
            events.append("maps")
            return super().items()

    session = membership.NodeMembershipSession.__new__(
        membership.NodeMembershipSession
    )
    session._pending_liveness = ObservedMap(pending)
    session._activity_receipts = ObservedMap(receipts)
    session._heartbeat_sequence = 7
    session._now = lambda: events.append("clock") or 100.0
    session._post_join_common = (
        lambda _protocol: events.append("message-id")
        or {"message_id": "new-message", "expires_at": 200.0}
    )
    session.signer = object()

    def sign_message(**kwargs: Any) -> dict[str, Any]:
        events.append("signer")
        return {"message": kwargs["message"]}

    monkeypatch.setattr(membership, "sign_membership_message", sign_message)
    with pytest.raises(ValueError) as caught:
        session._emit_liveness(
            lifecycle_state="RUNNING",
            active_requests=2,
            liveness_source=invalid_source,
            activity_receipt_digest=None,
            activity_peer_node_id=None,
        )
    assert str(caught.value) == "heartbeat liveness source is invalid"
    assert events == []
    assert session._heartbeat_sequence == 7
    assert session._pending_liveness == pending
    assert session._activity_receipts == receipts


def test_process_group_terminal_action_has_no_followup_observation(
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
        pid = launch.pid

        def __init__(self, calls: list[object]) -> None:
            self.calls = calls

        def poll(self) -> None:
            self.calls.append("poll")
            return None

    for outcome, expected in (
        ("success", True),
        ("lookup", False),
        ("other", False),
    ):
        calls: list[object] = []
        supervised = process_module.PhysicalNodeProcess.__new__(
            process_module.PhysicalNodeProcess
        )
        supervised._process = FakePopen(calls)
        supervised._launch_identity = launch
        supervised.shutdown_timeout_seconds = 1.0
        monkeypatch.setattr(process_module.time, "monotonic", lambda: 10.0)
        monkeypatch.setattr(
            process_module,
            "_protected_process_groups",
            lambda deadline: calls.append(("protected", deadline)) or {9999},
        )
        monkeypatch.setattr(
            process_module,
            "_inventory_process",
            lambda pid: calls.append(("inventory", pid)) or launch,
        )

        def terminal_action(pgid: int, signum: int) -> None:
            calls.append(("killpg", pgid, signum))
            if outcome == "lookup":
                raise ProcessLookupError("terminal")
            if outcome == "other":
                raise OSError("terminal")

        monkeypatch.setattr(os, "killpg", terminal_action)
        assert (
            supervised._signal_process_group(
                signal.SIGKILL,
                deadline=11.0,
            )
            is expected
        )
        assert calls == [
            ("protected", 11.0),
            "poll",
            ("inventory", launch.pid),
            ("killpg", launch.process_group, signal.SIGKILL),
        ]


def test_pre_exec_launcher_blocks_target_until_owned_release(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process_module = importlib.import_module("mycelium_node.process")
    marker = tmp_path / "target-executed"
    target = tmp_path / "handshake-target.py"
    target.write_text(
        """from pathlib import Path
import signal
import sys

Path(sys.argv[1]).write_text("executed")
signal.pause()
"""
    )
    release_reached = threading.Event()
    allow_release = threading.Event()
    real_release = process_module._release_pre_exec_launcher

    def gated_release(descriptor: int) -> None:
        release_reached.set()
        assert allow_release.wait(2.0)
        real_release(descriptor)

    monkeypatch.setattr(
        process_module,
        "_release_pre_exec_launcher",
        gated_release,
    )
    result: list[Any] = []

    def construct() -> None:
        try:
            result.append(
                process_module.PhysicalNodeProcess(
                    command=(sys.executable, str(target), str(marker)),
                    node_id="node-handshake",
                    run_id="run-handshake",
                    deployment_id="deployment-handshake",
                    response_timeout_seconds=0.05,
                    shutdown_timeout_seconds=1.0,
                )
            )
        except BaseException as exc:
            result.append(exc)

    constructor = threading.Thread(target=construct, daemon=False)
    constructor.start()
    assert release_reached.wait(2.0)
    assert marker.exists() is False
    allow_release.set()
    constructor.join(timeout=3.0)
    assert constructor.is_alive() is False
    assert len(result) == 1
    assert isinstance(result[0], process_module.PhysicalNodeProcess)
    supervised = result[0]
    try:
        deadline = time.monotonic() + 2.0
        while not marker.exists() and time.monotonic() < deadline:
            marker_event = threading.Event()
            marker_event.wait(0.01)
        assert marker.read_text() == "executed"
        assert supervised._pre_exec_ownership.pid == supervised.pid
        assert supervised._pre_exec_ownership.process_group == supervised.pid
        assert supervised._pre_exec_ownership.session_id == supervised.pid
    finally:
        supervised.close()


def test_owned_group_receives_term_before_kill(
    tmp_path: Path,
) -> None:
    process_module = importlib.import_module("mycelium_node.process")
    event_fifo = tmp_path / "signal-events"
    os.mkfifo(event_fifo, mode=0o600)
    reader = os.open(event_fifo, os.O_RDONLY | os.O_NONBLOCK)
    target = tmp_path / "term-target.py"
    target.write_text(
        """import os
import signal
import sys

events = os.open(sys.argv[1], os.O_WRONLY)
signal.signal(signal.SIGTERM, lambda _signum, _frame: os.write(events, b"T"))
os.write(events, b"R")
while True:
    signal.pause()
"""
    )
    supervised = process_module.PhysicalNodeProcess(
        command=(sys.executable, str(target), str(event_fifo)),
        node_id="node-term-order",
        run_id="run-term-order",
        deployment_id="deployment-term-order",
        response_timeout_seconds=0.05,
        shutdown_timeout_seconds=1.0,
    )
    selector = selectors.DefaultSelector()
    selector.register(reader, selectors.EVENT_READ)
    try:
        assert selector.select(timeout=2.0)
        assert os.read(reader, 1) == b"R"
        supervised.close()
        assert selector.select(timeout=2.0)
        assert os.read(reader, 1) == b"T"
        assert supervised._process.returncode == -signal.SIGKILL
    finally:
        selector.close()
        os.close(reader)
        if supervised._process.poll() is None:
            os.killpg(supervised.pid, signal.SIGKILL)
            supervised._process.wait(timeout=2.0)


def test_post_release_inventory_failure_cleans_preowned_group(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process_module = importlib.import_module("mycelium_node.process")
    target = tmp_path / "post-release-target.py"
    target.write_text("import signal\nsignal.pause()\n")
    real_popen = subprocess.Popen
    real_inventory = process_module._inventory_process
    launched: list[Any] = []
    child_inventory_calls = 0

    def tracked_popen(*args: Any, **kwargs: Any) -> Any:
        process = real_popen(*args, **kwargs)
        if kwargs.get("start_new_session") is True:
            launched.append(process)
        return process

    def fail_post_release(pid: int) -> Any:
        nonlocal child_inventory_calls
        if launched and pid == launched[0].pid:
            child_inventory_calls += 1
            if child_inventory_calls >= 2:
                raise ProcessLookupError("forced-post-release-inventory")
        return real_inventory(pid)

    monkeypatch.setattr(process_module.subprocess, "Popen", tracked_popen)
    monkeypatch.setattr(process_module, "_inventory_process", fail_post_release)
    with pytest.raises(process_module.NodeProcessError) as failed:
        process_module.PhysicalNodeProcess(
            command=(sys.executable, str(target)),
            node_id="node-post-release",
            run_id="run-post-release",
            deployment_id="deployment-post-release",
            response_timeout_seconds=0.05,
            shutdown_timeout_seconds=1.0,
        )

    assert failed.value.code == "node_process_start_failed"
    assert child_inventory_calls >= 3
    assert len(launched) == 1
    assert launched[0].poll() is not None
    with pytest.raises(ProcessLookupError):
        os.killpg(launched[0].pid, 0)


def test_leader_dead_inherited_sidecar_group_is_cleaned(
    tmp_path: Path,
) -> None:
    process_module = importlib.import_module("mycelium_node.process")
    pid_file = tmp_path / "leader-sidecar.pids"
    target = tmp_path / "leader-exits.py"
    target.write_text(
        """from pathlib import Path
import os
import subprocess
import sys

ready_read, ready_write = os.pipe()
sidecar = subprocess.Popen(
    [
        sys.executable,
        "-c",
        "import os,signal,sys; os.write(int(sys.argv[1]), b'R'); "
        "os.close(int(sys.argv[1])); signal.pause()",
        str(ready_write),
    ],
    pass_fds=(ready_write,),
)
os.close(ready_write)
assert os.read(ready_read, 1) == b"R"
os.close(ready_read)
Path(sys.argv[1]).write_text(f"{os.getpid()} {sidecar.pid}")
os._exit(0)
"""
    )
    supervised: Any = None
    try:
        try:
            supervised = process_module.PhysicalNodeProcess(
                command=(sys.executable, str(target), str(pid_file)),
                node_id="node-dead-leader",
                run_id="run-dead-leader",
                deployment_id="deployment-dead-leader",
                response_timeout_seconds=0.05,
                shutdown_timeout_seconds=1.0,
            )
        except process_module.NodeProcessError as exc:
            assert exc.code == "node_process_start_failed"
        deadline = time.monotonic() + 2.0
        while not pid_file.exists() and time.monotonic() < deadline:
            threading.Event().wait(0.01)
        leader_pid, sidecar_pid = [
            int(value) for value in pid_file.read_text().split()
        ]
        if supervised is not None:
            supervised._process.wait(timeout=2.0)
            supervised.close()
        deadline = time.monotonic() + 2.0
        while _pid_exists(sidecar_pid) and time.monotonic() < deadline:
            threading.Event().wait(0.01)
        assert _pid_exists(leader_pid) is False
        assert _pid_exists(sidecar_pid) is False
        with pytest.raises(ProcessLookupError):
            os.killpg(leader_pid, 0)
    finally:
        if pid_file.exists():
            leader_pid = int(pid_file.read_text().split()[0])
            try:
                os.killpg(leader_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        if supervised is not None and supervised._process.poll() is None:
            supervised._process.wait(timeout=2.0)


def test_process_cleanup_retry_and_handshake_fd_thread_inventory_stay_stable(
    tmp_path: Path,
) -> None:
    process_module = importlib.import_module("mycelium_node.process")
    inventory_root = Path("/proc/self/fd")
    if not inventory_root.is_dir():
        inventory_root = Path("/dev/fd")

    def descriptors() -> set[int]:
        return {
            int(name)
            for name in os.listdir(inventory_root)
            if name.isdigit()
        }

    target = tmp_path / "retry-target.py"
    target.write_text(
        """import signal

signal.signal(signal.SIGTERM, signal.SIG_IGN)
while True:
    signal.pause()
"""
    )
    baseline_descriptors = descriptors()
    baseline_threads = {
        thread.name for thread in threading.enumerate() if thread.is_alive()
    }
    for cycle in range(8):
        supervised = process_module.PhysicalNodeProcess(
            command=(sys.executable, str(target)),
            node_id=f"node-retry-{cycle}",
            run_id=f"run-retry-{cycle}",
            deployment_id=f"deployment-retry-{cycle}",
            response_timeout_seconds=0.01,
            shutdown_timeout_seconds=1.0,
        )
        if cycle == 0:
            supervised.shutdown_timeout_seconds = 0.000001
            with pytest.raises(process_module.NodeProcessError) as failed:
                supervised.close()
            assert failed.value.code == "node_process_cleanup_failed"
            assert supervised._process.poll() is None
            supervised.shutdown_timeout_seconds = 1.0
        supervised.close()
        supervised.close()
    assert descriptors() == baseline_descriptors
    assert {
        thread.name for thread in threading.enumerate() if thread.is_alive()
    } == baseline_threads


def test_handshake_release_failure_closes_fds_and_reaps_blocked_launcher(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process_module = importlib.import_module("mycelium_node.process")
    inventory_root = Path("/proc/self/fd")
    if not inventory_root.is_dir():
        inventory_root = Path("/dev/fd")

    def descriptors() -> set[int]:
        return {
            int(name)
            for name in os.listdir(inventory_root)
            if name.isdigit()
        }

    marker = tmp_path / "must-not-exec"
    target = tmp_path / "blocked-target.py"
    target.write_text(
        "from pathlib import Path\n"
        "import sys\n"
        "Path(sys.argv[1]).write_text('unexpected')\n"
    )
    real_popen = subprocess.Popen
    launched: list[Any] = []

    def tracked_popen(*args: Any, **kwargs: Any) -> Any:
        process = real_popen(*args, **kwargs)
        if kwargs.get("start_new_session") is True:
            launched.append(process)
        return process

    monkeypatch.setattr(process_module.subprocess, "Popen", tracked_popen)
    monkeypatch.setattr(
        process_module,
        "_release_pre_exec_launcher",
        lambda _descriptor: (_ for _ in ()).throw(
            OSError("secret-release-failure")
        ),
    )
    before = descriptors()
    with pytest.raises(process_module.NodeProcessError) as caught:
        process_module.PhysicalNodeProcess(
            command=(sys.executable, str(target), str(marker)),
            node_id="node-release-failure",
            run_id="run-release-failure",
            deployment_id="deployment-release-failure",
            response_timeout_seconds=0.05,
            shutdown_timeout_seconds=1.0,
        )

    assert caught.value.code == "node_process_start_failed"
    assert marker.exists() is False
    assert len(launched) == 1
    assert launched[0].poll() is not None
    assert descriptors() == before
    assert not any(
        thread.name.startswith("mycelium-node-") and thread.is_alive()
        for thread in threading.enumerate()
    )


def test_entrypoint_cleanup_aggregation_preserves_primary_and_all_phases() -> None:
    node_main = importlib.import_module("mycelium_node.__main__")
    phases = (
        "process",
        "temporary_root",
        "artifact_root",
        "signal_restoration",
    )
    primary = node_main._EntrypointFailure("node_join_rejected", 3)
    aggregated = node_main._aggregate_cleanup_failures(primary, phases)

    assert aggregated is primary
    assert (aggregated.code, aggregated.exit_status) == ("node_join_rejected", 3)
    assert getattr(aggregated, "__notes__", ()) == [
        "cleanup_phase=process",
        "cleanup_phase=temporary_root",
        "cleanup_phase=artifact_root",
        "cleanup_phase=signal_restoration",
        "cleanup_failure_count=4",
    ]

    cleanup_only = node_main._aggregate_cleanup_failures(None, phases)
    assert (cleanup_only.code, cleanup_only.exit_status) == (
        "node_runtime_failed",
        4,
    )
    assert getattr(cleanup_only, "__notes__", ()) == [
        "cleanup_phase=process",
        "cleanup_phase=temporary_root",
        "cleanup_phase=artifact_root",
        "cleanup_phase=signal_restoration",
        "cleanup_failure_count=4",
    ]
    assert "secret-cleanup-value" not in str(primary)


def test_node_run_bound_finally_retains_primary_through_four_cleanup_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    node_main = importlib.import_module("mycelium_node.__main__")
    primary = node_main._EntrypointFailure("node_join_rejected", 3)

    class ArtifactRoot:
        def revalidate(self) -> None:
            return None

        def close(self) -> None:
            raise OSError("secret-artifact-close")

    artifact_root = ArtifactRoot()

    class StateRoot:
        def revalidate(self) -> None:
            return None

        def private_subdirectory(self, _name: str) -> ArtifactRoot:
            return artifact_root

    class Client:
        def identity(self, *, now: float) -> dict[str, str]:
            return {"seed_node_id": "seed-node"}

    class FakeSigner:
        endpoint_id = "node-cleanup-primary"

    class FakeSession:
        def __init__(self, **_kwargs: Any) -> None:
            return None

    class FakeProcess:
        pid = 4242

        def __init__(self, **_kwargs: Any) -> None:
            return None

        def command(self, _operation: str) -> None:
            raise primary

        def close(self) -> None:
            raise OSError("secret-process-close")

    temporary_root = type(
        "TemporaryRoot",
        (),
        {"path": Path("/private/nonsecret")},
    )()
    old_handlers = {
        signal.SIGINT: object(),
        signal.SIGTERM: object(),
    }
    restore_calls = 0

    def fake_signal(signum: int, handler: object) -> object:
        nonlocal restore_calls
        if handler in old_handlers.values():
            restore_calls += 1
            if restore_calls == 1:
                raise OSError("secret-signal-restore")
            return handler
        return old_handlers[signum]

    monkeypatch.setattr(
        node_main,
        "load_or_create_node_signer",
        lambda _path: FakeSigner(),
    )
    monkeypatch.setattr(node_main, "NodeMembershipSession", FakeSession)
    monkeypatch.setattr(
        node_main,
        "_temporary_root",
        lambda: temporary_root,
    )
    monkeypatch.setattr(
        node_main,
        "build_physical_node_command",
        lambda **_kwargs: (sys.executable, "-c", "pass"),
    )
    monkeypatch.setattr(node_main, "PhysicalNodeProcess", FakeProcess)
    monkeypatch.setattr(
        node_main,
        "_remove_temporary_root",
        lambda _root: (_ for _ in ()).throw(
            OSError("secret-temporary-close")
        ),
    )
    monkeypatch.setattr(node_main.signal, "signal", fake_signal)
    args = type(
        "Args",
        (),
        {
            "node_id": "node-cleanup-primary",
            "incarnation": "incarnation-cleanup-primary",
            "run_id": "run-cleanup-primary",
            "deployment_id": "deployment-cleanup-primary",
            "advertise": ["https://node.test/control"],
            "heartbeat_interval": 1.0,
        },
    )()

    with pytest.raises(node_main._EntrypointFailure) as caught:
        node_main._run_bound(
            args,
            StateRoot(),
            {"token": "secret-token"},
            {
                "payload": {
                    "swarm_id": "swarm-cleanup-primary",
                    "nonce": "nonce-cleanup-primary",
                    "seed_url": "http://seed.test",
                },
                "seed_key_digest": "sha256:" + "a" * 64,
            },
            Client(),
            Path("/private/sidecar"),
            (),
        )

    assert caught.value is primary
    assert (caught.value.code, caught.value.exit_status) == (
        "node_join_rejected",
        3,
    )
    assert getattr(caught.value, "__notes__", ()) == [
        "cleanup_phase=process",
        "cleanup_phase=temporary_root",
        "cleanup_phase=artifact_root",
        "cleanup_phase=signal_restoration",
        "cleanup_failure_count=4",
    ]
    assert "secret-" not in str(caught.value)


def _raw_join_request(port: int, body: bytes) -> bytes:
    request = (
        b"POST /seed/join HTTP/1.1\r\n"
        b"Host: 127.0.0.1\r\n"
        b"Content-Type: application/json\r\n"
        + f"Content-Length: {len(body)}\r\n".encode("ascii")
        + b"Connection: close\r\n\r\n"
        + body
    )
    with socket.create_connection(("127.0.0.1", port), timeout=2.0) as connection:
        connection.sendall(request)
        chunks: list[bytes] = []
        while True:
            chunk = connection.recv(65536)
            if not chunk:
                return b"".join(chunks)
            chunks.append(chunk)


def test_join_route_internal_code_is_generic_and_emits_one_response() -> None:
    seed_http = importlib.import_module("mycelium_seed.http")
    secret = "secret_internal_join_code"

    class InternalJoinError(ValueError):
        code = secret

    class FailingCoordinator:
        def bind_seed_url(self, _url: str) -> None:
            return None

        def accept_join(self, **_kwargs: Any) -> None:
            raise InternalJoinError

    body = canonical_json_bytes(
        {
            "protocol": seed_http.SEED_JOIN_HTTP_PROTOCOL,
            "invite_token": "opaque",
            "join_envelope": {},
        }
    )
    with SeedHTTPServer(
        FailingCoordinator(),
        host="127.0.0.1",
        port=0,
    ) as server:
        response = _raw_join_request(urlsplit(server.base_url).port, body)

    assert response.count(b"HTTP/1.1 ") == 1
    assert response.startswith(b"HTTP/1.1 500 ")
    assert secret.encode() not in response
    assert canonical_json_bytes(
        {
            "protocol": seed_http.SEED_HTTP_ERROR_PROTOCOL,
            "error": {"code": "seed_http_internal_error"},
        }
    ) in response


def test_join_success_write_failure_never_emits_a_second_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seed_http = importlib.import_module("mycelium_seed.http")

    class AcceptingCoordinator:
        def bind_seed_url(self, _url: str) -> None:
            return None

        def accept_join(self, **_kwargs: Any) -> dict[str, bool]:
            return {"accepted": True}

    original_send = seed_http._SeedRequestHandler._send

    def fail_success_write(
        handler: Any,
        status: int,
        value: dict[str, Any],
    ) -> None:
        if int(status) != 200:
            original_send(handler, status, value)
            return
        response_body = canonical_json_bytes(dict(value))
        handler._response_started = True
        handler.send_response(status)
        handler.send_header("Content-Type", "application/json")
        handler.send_header("Content-Length", str(len(response_body)))
        handler.send_header("Cache-Control", "no-store")
        handler.end_headers()
        handler.wfile.write(response_body[:1])
        handler.wfile.flush()
        raise OSError("secret-write-failure")

    monkeypatch.setattr(
        seed_http._SeedRequestHandler,
        "_send",
        fail_success_write,
    )
    body = canonical_json_bytes(
        {
            "protocol": seed_http.SEED_JOIN_HTTP_PROTOCOL,
            "invite_token": "opaque",
            "join_envelope": {},
        }
    )
    with SeedHTTPServer(
        AcceptingCoordinator(),
        host="127.0.0.1",
        port=0,
    ) as server:
        response = _raw_join_request(urlsplit(server.base_url).port, body)

    assert response.count(b"HTTP/1.1 ") == 1
    assert response.startswith(b"HTTP/1.1 200 ")
    assert b"HTTP/1.1 500 " not in response
    assert b"seed_http_internal_error" not in response


def test_join_client_hides_status_mismatched_authoritative_error() -> None:
    seed_http = importlib.import_module("mycelium_seed.http")
    signer = generate_ed25519_signer(endpoint_id="seed-status-mismatch")
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    response_body = canonical_json_bytes(
        {
            "protocol": seed_http.SEED_HTTP_ERROR_PROTOCOL,
            "error": {"code": "membership_signature_invalid"},
        }
    )
    served = threading.Event()

    def serve_one() -> None:
        connection, _address = listener.accept()
        with connection:
            connection.recv(65536)
            connection.sendall(
                b"HTTP/1.1 500 Internal Server Error\r\n"
                b"Content-Type: application/json\r\n"
                + f"Content-Length: {len(response_body)}\r\n".encode("ascii")
                + b"Connection: close\r\n\r\n"
                + response_body
            )
        served.set()

    server_thread = threading.Thread(target=serve_one, daemon=False)
    server_thread.start()
    client = SeedHTTPClient(
        seed_url=f"http://127.0.0.1:{listener.getsockname()[1]}",
        swarm_id="swarm-status-mismatch",
        seed_key_digest=signer.verification_key_digest,
        seed_key_records=[signer.public_key_record()],
    )
    try:
        with pytest.raises(SeedHTTPError) as caught:
            client._request("POST", "/seed/join", {})
    finally:
        listener.close()
        server_thread.join(timeout=2.0)

    assert served.is_set()
    assert server_thread.is_alive() is False
    assert (caught.value.status, caught.value.code) == (
        500,
        "seed_http_remote_error",
    )
    assert "membership_signature_invalid" not in str(caught.value)


def test_working_directory_out_of_order_lease_stack_unwinds_to_origin(
    tmp_path: Path,
) -> None:
    process_module = importlib.import_module("mycelium_node.process")
    inventory_root = Path("/proc/self/fd")
    if not inventory_root.is_dir():
        inventory_root = Path("/dev/fd")

    def descriptors() -> set[int]:
        return {
            int(name)
            for name in os.listdir(inventory_root)
            if name.isdigit()
        }

    original = os.open(".", os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    original_identity = os.fstat(original)[:2]
    roots = [tmp_path / f"lease-{index}" for index in range(3)]
    for root in roots:
        root.mkdir(mode=0o700)
    leases = [process_module.private_directory_lease(root) for root in roots]
    permutations = (
        (0, 1, 2),
        (0, 2, 1),
        (1, 0, 2),
        (1, 2, 0),
        (2, 0, 1),
        (2, 1, 0),
    )
    baseline_descriptors = descriptors()
    try:
        for _cycle in range(20):
            for order in permutations:
                contexts = [lease.working_directory() for lease in leases]
                try:
                    for index, context in enumerate(contexts):
                        context.__enter__()
                        assert os.stat(".")[:2] == os.fstat(
                            leases[index]._descriptor
                        )[:2]
                    for index in order:
                        contexts[index].__exit__(None, None, None)
                    assert os.stat(".")[:2] == original_identity
                    assert process_module._CWD_LEASE_STACK == []
                finally:
                    os.fchdir(original)
        assert descriptors() == baseline_descriptors
    finally:
        os.fchdir(original)
        for lease in leases:
            lease.close()
        os.close(original)


@pytest.mark.parametrize("primary_fails", (False, True))
def test_cwd_deactivation_does_not_retain_close_failures(
    monkeypatch: pytest.MonkeyPatch,
    primary_fails: bool,
) -> None:
    process_module = importlib.import_module("mycelium_node.process")
    assert process_module._CWD_LEASE_STACK == []
    safety = os.open(".", os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    descriptor = os.dup(safety)
    primary = RuntimeError("authoritative-cwd-body") if primary_fails else None
    token = process_module._WorkingDirectoryToken(
        original_descriptor=descriptor,
        body_failure=primary,
    )
    process_module._CWD_LEASE_STACK.append(token)
    real_close = os.close
    close_calls: list[int] = []
    references: list[
        weakref.ReferenceType[_WeakReferenceableCloseError]
    ] = []
    private_marker = object()
    garbage_collection_was_enabled = gc.isenabled()
    gc.disable()
    try:

        def fail_close(received_descriptor: int) -> None:
            close_calls.append(received_descriptor)
            raise _WeakReferenceableCloseError(references, private_marker)

        monkeypatch.setattr(os, "close", fail_close)
        outcome: BaseException | None = None
        try:
            process_module._deactivate_working_directory_token(token)
        except ValueError as caught:
            outcome = caught

        assert close_calls == [descriptor]
        assert token.original_descriptor is None
        assert process_module._CWD_LEASE_STACK == []
        if primary is None:
            assert type(outcome) is ValueError
            assert str(outcome) == "working directory restoration failed"
        else:
            assert outcome is None
            assert getattr(primary, "__notes__", ()) == [
                "working directory restoration failed"
            ]

        process_module._deactivate_working_directory_token(token)
        assert close_calls == [descriptor]
        assert len(references) == 1
        assert references[0]() is None
    finally:
        monkeypatch.setattr(os, "close", real_close)
        os.fchdir(safety)
        real_close(descriptor)
        real_close(safety)
        if garbage_collection_was_enabled:
            gc.enable()


def test_cwd_cleanup_error_traceback_excludes_descriptor_and_private_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process_module = importlib.import_module("mycelium_node.process")
    assert process_module._CWD_LEASE_STACK == []
    safety = os.open(".", os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    descriptors = [os.dup(safety), os.dup(safety)]
    tokens = [
        process_module._WorkingDirectoryToken(
            original_descriptor=descriptor,
        )
        for descriptor in descriptors
    ]
    process_module._CWD_LEASE_STACK.extend(tokens)
    expected_close_calls = list(reversed(descriptors))
    real_close = os.close
    close_calls: list[int] = []
    references: list[
        weakref.ReferenceType[_WeakReferenceableCloseError]
    ] = []
    private_marker = object()
    private_errno = 987_654_321
    private_path = "/private/cwd-close-path-sentinel-2r5-fix9"
    private_secret = "private-cwd-close-secret-sentinel-2r5-fix9"
    raw_descriptor = descriptors[0]
    try:

        def fail_close(received_descriptor: int) -> None:
            close_calls.append(received_descriptor)
            raise _WeakReferenceableCloseError(
                references,
                private_marker,
                private_errno=private_errno,
                private_path=private_path,
                private_secret=private_secret,
            )

        monkeypatch.setattr(os, "close", fail_close)
        process_module._deactivate_working_directory_token(tokens[0])
        with pytest.raises(
            ValueError,
            match="^working directory restoration failed$",
        ) as caught:
            process_module._deactivate_working_directory_token(tokens[1])

        assert close_calls == expected_close_calls
        assert all(token.original_descriptor is None for token in tokens)
        assert process_module._CWD_LEASE_STACK == []
        assert caught.value.__cause__ is None
        process_module._deactivate_working_directory_token(tokens[1])
        process_module._deactivate_working_directory_token(tokens[0])
        assert close_calls == expected_close_calls

        def contains_target(value: object, target: object) -> bool:
            pending = [value]
            visited: set[int] = set()
            while pending:
                candidate = pending.pop()
                if candidate is target:
                    return True
                if (
                    type(candidate) is type(target)
                    and isinstance(candidate, (int, str, bytes))
                    and candidate == target
                ):
                    return True
                identity = id(candidate)
                if identity in visited:
                    continue
                visited.add(identity)
                if isinstance(candidate, BaseException):
                    pending.extend(candidate.args)
                    pending.extend(vars(candidate).values())
                    pending.extend(
                        (
                            candidate.__cause__,
                            candidate.__context__,
                            getattr(candidate, "__notes__", ()),
                        )
                    )
                    if isinstance(candidate, OSError):
                        pending.extend(
                            (
                                candidate.errno,
                                candidate.filename,
                                candidate.filename2,
                                candidate.strerror,
                            )
                        )
                elif isinstance(
                    candidate,
                    process_module._WorkingDirectoryToken,
                ):
                    pending.extend(vars(candidate).values())
                elif isinstance(candidate, dict):
                    pending.extend(candidate.keys())
                    pending.extend(candidate.values())
                elif isinstance(candidate, (list, tuple, set, frozenset)):
                    pending.extend(candidate)
            return False

        process_path = Path(process_module.__file__).resolve()
        production_locals: list[dict[str, object]] = []
        traceback = caught.value.__traceback__
        while traceback is not None:
            frame = traceback.tb_frame
            if Path(frame.f_code.co_filename).resolve() == process_path:
                production_locals.append(dict(frame.f_locals))
            traceback = traceback.tb_next
        assert production_locals

        exception_surfaces = (
            caught.value.__cause__,
            caught.value.__context__,
            caught.value.args,
            getattr(caught.value, "__notes__", ()),
        )
        non_traceback_roots = (
            exception_surfaces,
            tokens,
            process_module._CWD_LEASE_STACK,
        )
        injected_private_values = (
            private_marker,
            private_errno,
            private_path,
            private_secret,
        )
        assert not any(
            contains_target(root, private_value)
            for root in (*non_traceback_roots, *production_locals)
            for private_value in injected_private_values
        )
        assert not any(
            contains_target(frame_locals, raw_descriptor)
            for frame_locals in production_locals
        ), (
            "raw descriptor remains in production traceback locals"
        )
        assert len(references) == len(descriptors)
        assert all(reference() is None for reference in references)
    finally:
        monkeypatch.setattr(os, "close", real_close)
        os.fchdir(safety)
        for descriptor in descriptors:
            real_close(descriptor)
        real_close(safety)


@pytest.mark.parametrize(
    (
        "level_count",
        "failing_close_positions",
        "restoration_fails",
        "body_fails",
    ),
    (
        pytest.param(2, (0,), False, False, id="two-first-cleanup-only"),
        pytest.param(2, (1,), False, True, id="two-last-primary"),
        pytest.param(2, (0, 1), True, False, id="two-all-restore-failure"),
        pytest.param(3, (0,), False, False, id="three-first-cleanup-only"),
        pytest.param(3, (1,), False, True, id="three-middle-primary"),
        pytest.param(3, (2,), True, False, id="three-last-restore-failure"),
        pytest.param(3, (0, 2), False, False, id="three-first-last"),
        pytest.param(3, (0, 1, 2), True, True, id="three-all-primary"),
    ),
)
def test_nested_working_directory_close_failures_are_aggregated(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    level_count: int,
    failing_close_positions: tuple[int, ...],
    restoration_fails: bool,
    body_fails: bool,
) -> None:
    process_module = importlib.import_module("mycelium_node.process")
    safety = os.open(".", os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    origin_identity = os.fstat(safety)[:2]
    roots = [
        tmp_path / f"close-failure-{level_count}-{index}"
        for index in range(level_count)
    ]
    for root in roots:
        root.mkdir(mode=0o700)
    leases = [
        process_module.private_directory_lease(root)
        for root in roots
    ]
    contexts = [lease.working_directory() for lease in leases]
    real_close = os.close
    real_fchdir = os.fchdir
    real_revalidate = process_module.PrivateDirectoryLease.revalidate
    expected_descriptors: list[int] = []
    successfully_closed: set[int] = set()
    close_calls: list[int] = []
    restore_calls: list[int] = []
    primary = RuntimeError("authoritative-cwd-body")
    try:
        for context in contexts:
            context.__enter__()
        tokens = list(process_module._CWD_LEASE_STACK)
        expected_descriptors = [
            token.original_descriptor for token in reversed(tokens)
        ]
        assert all(
            descriptor is not None for descriptor in expected_descriptors
        )
        descriptor_positions = {
            descriptor: index
            for index, descriptor in enumerate(expected_descriptors)
        }

        def controlled_close(descriptor: int) -> None:
            position = descriptor_positions.get(descriptor)
            if position is None:
                real_close(descriptor)
                return
            close_calls.append(descriptor)
            if position in failing_close_positions:
                raise OSError(errno.EIO, "secret-close-value")
            real_close(descriptor)
            successfully_closed.add(descriptor)

        def controlled_fchdir(descriptor: int) -> None:
            restore_calls.append(descriptor)
            if restoration_fails:
                raise OSError(errno.EIO, "secret-restore-value")
            real_fchdir(descriptor)

        monkeypatch.setattr(os, "close", controlled_close)
        monkeypatch.setattr(os, "fchdir", controlled_fchdir)
        monkeypatch.setattr(
            process_module.PrivateDirectoryLease,
            "revalidate",
            lambda _self: None,
        )
        for context in contexts[:-1]:
            assert context.__exit__(None, None, None) is False

        outcome: BaseException | None = None
        try:
            if body_fails:
                suppress_primary = contexts[-1].__exit__(
                    type(primary),
                    primary,
                    primary.__traceback__,
                )
                if not suppress_primary:
                    raise primary
            else:
                contexts[-1].__exit__(None, None, None)
        except BaseException as exc:
            outcome = exc

        cwd_restored = os.stat(".")[:2] == origin_identity
        calls_before_repeat = list(close_calls)
        restore_calls_before_repeat = list(restore_calls)
        repeat_result = contexts[-1].__exit__(None, None, None)
        observed = {
            "close_positions": [
                descriptor_positions[descriptor]
                for descriptor in close_calls
            ],
            "detached": [
                token.original_descriptor is None for token in tokens
            ],
            "stack_empty": process_module._CWD_LEASE_STACK == [],
            "outcome_is_primary": outcome is primary,
            "outcome_type": (
                type(outcome).__name__ if outcome is not None else None
            ),
            "outcome_text": str(outcome) if outcome is not None else None,
            "primary_notes": list(getattr(primary, "__notes__", ())),
            "restore_call_count": len(restore_calls),
            "cwd_restored": cwd_restored,
            "repeat_result": repeat_result,
            "repeat_close_free": close_calls == calls_before_repeat,
            "repeat_restore_free": restore_calls == restore_calls_before_repeat,
        }

        monkeypatch.setattr(os, "close", real_close)
        monkeypatch.setattr(os, "fchdir", real_fchdir)
        monkeypatch.setattr(
            process_module.PrivateDirectoryLease,
            "revalidate",
            real_revalidate,
        )
        real_fchdir(safety)
        for descriptor in expected_descriptors:
            if descriptor not in successfully_closed:
                real_close(descriptor)
                successfully_closed.add(descriptor)

        with leases[0].working_directory():
            assert os.stat(".")[:2] == os.fstat(leases[0]._descriptor)[:2]
        assert os.stat(".")[:2] == origin_identity
        assert process_module._CWD_LEASE_STACK == []

        expected_outcome = (
            {
                "outcome_is_primary": True,
                "outcome_type": "RuntimeError",
                "outcome_text": "authoritative-cwd-body",
                "primary_notes": ["working directory restoration failed"],
            }
            if body_fails
            else {
                "outcome_is_primary": False,
                "outcome_type": "ValueError",
                "outcome_text": "working directory restoration failed",
                "primary_notes": [],
            }
        )
        assert observed == {
            "close_positions": list(range(level_count)),
            "detached": [True] * level_count,
            "stack_empty": True,
            **expected_outcome,
            "restore_call_count": 3 if restoration_fails else 1,
            "cwd_restored": not restoration_fails,
            "repeat_result": False,
            "repeat_close_free": True,
            "repeat_restore_free": True,
        }
    finally:
        monkeypatch.setattr(os, "close", real_close)
        monkeypatch.setattr(os, "fchdir", real_fchdir)
        monkeypatch.setattr(
            process_module.PrivateDirectoryLease,
            "revalidate",
            real_revalidate,
        )
        real_fchdir(safety)
        for descriptor in expected_descriptors:
            if descriptor not in successfully_closed:
                real_close(descriptor)
        for lease in leases:
            lease.close()
        real_close(safety)


def test_out_of_order_cwd_restore_failure_notes_deferred_primary_and_reuses_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process_module = importlib.import_module("mycelium_node.process")
    safety = os.open(".", os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    first_root = tmp_path / "deferred-first"
    second_root = tmp_path / "deferred-second"
    first_root.mkdir(mode=0o700)
    second_root.mkdir(mode=0o700)
    first = process_module.private_directory_lease(first_root)
    second = process_module.private_directory_lease(second_root)
    real_fchdir = os.fchdir
    outer = first.working_directory()
    inner = second.working_directory()
    primary = RuntimeError("authoritative-cwd-body")
    try:
        outer.__enter__()
        inner.__enter__()
        assert outer.__exit__(type(primary), primary, primary.__traceback__) is False

        def fail_restore(_descriptor: int) -> None:
            raise OSError(errno.EIO, "secret-deferred-restore")

        monkeypatch.setattr(os, "fchdir", fail_restore)
        assert inner.__exit__(None, None, None) is False
        assert getattr(primary, "__notes__", ()) == [
            "working directory restoration failed"
        ]
        assert "secret-deferred-restore" not in str(primary)
        assert process_module._CWD_LEASE_STACK == []

        monkeypatch.setattr(os, "fchdir", real_fchdir)
        real_fchdir(safety)
        with first.working_directory():
            assert os.stat(".")[:2] == os.fstat(first._descriptor)[:2]
    finally:
        monkeypatch.setattr(os, "fchdir", real_fchdir)
        real_fchdir(safety)
        first.close()
        second.close()
        os.close(safety)
