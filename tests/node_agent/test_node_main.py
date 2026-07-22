from __future__ import annotations

from itertools import count
import json
import os
from pathlib import Path
import selectors
import stat
import subprocess
import sys
import time
from typing import Any

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
        invite_file.write_bytes(canonical_json_bytes(bundle))
        invite_file.chmod(0o600)
        command = [
            sys.executable,
            "-m",
            "mycelium_node",
            "--data-dir",
            str(data_dir),
            "--seed-invite",
            str(invite_file),
            "--node-id",
            "node-main-a",
            "--advertise",
            "https://node-main-a.test/control",
            "--sidecar-path",
            str(node_main_sidecar_binary),
            "--run-id",
            "node-main-run",
            "--deployment-id",
            "node-main-unassigned",
        ]
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
