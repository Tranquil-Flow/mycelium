# SPDX-License-Identifier: AGPL-3.0-only
"""The native node keeps (or honestly loses) its seed membership across the A12
physical-gate negatives, against the real seed and pairing channel.

The node is the product ``NodeCore`` driven by ``native/mobile_core/examples/node_cli``
(the same code path the Android app runs).  The seed uses a 3 second lease, the
node the short ``--fast`` timing, and a cuttable loopback TCP proxy in front of the
seed plays the phone's network:

* network loss: the lease lapses while the network is cut; once it returns the
  same node resumes (fresh incarnation, next generation) without a new invite;
* process death: a node restarted while the network is still down keeps
  retrying the resume until the seed answers;
* revocation: a fenced member never regains membership and says so.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import queue
import socket
import subprocess
import threading
import time
from typing import Any, Callable

import pytest

from mycelium_invite import SqliteInviteRegistry
from mycelium_qualification.signing import generate_ed25519_signer
from mycelium_seed import SeedCoordinator, SqliteSeedState
from mycelium_seed.http import SeedHTTPServer
from mycelium_seed.pairing import AndroidPairingChannel

ROOT = Path(__file__).resolve().parents[2]
CORE_CRATE = ROOT / "native" / "mobile_core"
NODE_CLI = CORE_CRATE / "target" / "debug" / "examples" / "node_cli"
_BUILD_ENVIRONMENT_KEYS = ("CARGO_HOME", "HOME", "LANG", "LC_ALL", "PATH", "RUSTUP_HOME", "TMPDIR")
LEASE_SECONDS = 3.0
QUOTA = {
    "background_allowed": True,
    "battery_floor_percent": 20,
    "network_bytes_per_day": 1 << 30,
    "storage_bytes": 1 << 30,
    "thermal_ceiling_c": 45,
}


@pytest.fixture(scope="module")
def node_cli() -> Path:
    environment = {key: os.environ[key] for key in _BUILD_ENVIRONMENT_KEYS if key in os.environ}
    environment["CARGO_NET_OFFLINE"] = "true"
    subprocess.run(
        ["cargo", "build", "--locked", "--features", "net", "--example", "node_cli"],
        cwd=CORE_CRATE,
        env=environment,
        check=True,
    )
    assert NODE_CLI.is_file()
    return NODE_CLI


class CuttableProxy:
    """Loopback TCP forwarder whose link can be cut and restored."""

    def __init__(self) -> None:
        self._listener = socket.create_server(("127.0.0.1", 0))
        self.port = self._listener.getsockname()[1]
        self.target: int | None = None
        self._cut = threading.Event()
        self._open: set[socket.socket] = set()
        self._lock = threading.Lock()
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self) -> None:
        while True:
            try:
                client, _ = self._listener.accept()
            except OSError:
                return
            if self._cut.is_set() or self.target is None:
                client.close()
                continue
            try:
                upstream = socket.create_connection(("127.0.0.1", self.target))
            except OSError:
                client.close()
                continue
            with self._lock:
                self._open.update((client, upstream))
            for a, b in ((client, upstream), (upstream, client)):
                threading.Thread(target=self._pump, args=(a, b), daemon=True).start()

    def _pump(self, source: socket.socket, sink: socket.socket) -> None:
        try:
            while data := source.recv(65536):
                sink.sendall(data)
        except OSError:
            pass
        finally:
            for s in (source, sink):
                try:
                    s.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass

    def cut(self) -> None:
        self._cut.set()
        with self._lock:
            for s in self._open:
                try:
                    s.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                s.close()
            self._open.clear()

    def restore(self) -> None:
        self._cut.clear()

    def close(self) -> None:
        self.cut()
        self._listener.close()


class Seed:
    def __init__(self, tmp: Path) -> None:
        tmp.mkdir(mode=0o700, parents=True, exist_ok=True)
        database = tmp / "state.sqlite3"
        self.proxy = CuttableProxy()
        self.coordinator = SeedCoordinator(
            swarm_id="lease-swarm",
            seed_node_id="lease-seed",
            seed_url=None,
            signer=generate_ed25519_signer(endpoint_id="lease-seed-endpoint"),
            invite_registry=SqliteInviteRegistry(database),
            state=SqliteSeedState(database),
            incarnation="lease-seed-1",
            lease_seconds=LEASE_SECONDS,
            keepalive_interval_seconds=1.0,
            evidence_freshness_seconds=2.0,
        )
        self.server = SeedHTTPServer(
            self.coordinator,
            host="127.0.0.1",
            port=0,
            advertised_url=f"http://127.0.0.1:{self.proxy.port}",
        ).start()
        self.proxy.target = int(self.server.base_url.rsplit(":", 1)[1])
        self.pairing = AndroidPairingChannel(
            self.coordinator, quota=QUOTA, host="127.0.0.1", port=0, max_approvals=4
        )
        self.pairing.start()

    def member(self) -> dict[str, Any]:
        (member,) = self.coordinator.members()
        return member

    def lease_fresh(self) -> bool:
        return time.time() < float(self.member()["lease_expires_at"])

    def close(self) -> None:
        self.pairing.close()
        self.proxy.close()
        self.server.close()


class Node:
    """One node_cli process; status lines arrive on a queue."""

    def __init__(self, binary: Path, data_dir: Path, mode: str, extra: tuple[str, ...] = ()) -> None:
        self.process = subprocess.Popen(
            [str(binary), str(data_dir), mode, "local", "--fast", "--watch", "60", *extra],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        self.lines: queue.Queue[tuple[str, dict[str, Any]]] = queue.Queue()
        self.last: dict[str, Any] = {}
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self) -> None:
        assert self.process.stdout is not None
        for line in self.process.stdout:
            label, _, body = line.partition(" ")
            try:
                self.lines.put((label, json.loads(body)))
            except ValueError:
                self.lines.put((label, {"raw": body.strip()}))

    def wait(self, predicate: Callable[[dict[str, Any]], bool], timeout: float, what: str) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        if self.last and predicate(self.last):
            return self.last
        while time.monotonic() < deadline:
            try:
                label, status = self.lines.get(timeout=max(0.01, deadline - time.monotonic()))
            except queue.Empty:
                break
            if "phase" in status:
                self.last = status
            if label in {"pair_error", "join_error", "resume_error"}:
                pytest.fail(f"{label}: {status}")
            if "phase" in status and predicate(status):
                return status
        pytest.fail(f"timed out waiting for {what}; last status {self.last}")

    def stop(self) -> None:
        self.process.kill()
        self.process.wait(timeout=10)


def _held(generation: int) -> Callable[[dict[str, Any]], bool]:
    return lambda s: s["phase"] == "joined" and s["lease_fresh"] and s["member_generation"] == generation


@pytest.fixture
def seed(tmp_path: Path):
    seed = Seed(tmp_path / "seed")
    yield seed
    seed.close()


def test_network_loss_lapses_the_lease_and_the_same_member_resumes(seed: Seed, node_cli: Path, tmp_path: Path) -> None:
    node = Node(node_cli, tmp_path / "node", seed.pairing.channel_qr)
    try:
        node.wait(_held(1), 20, "join")
        before = seed.member()
        time.sleep(1.5)  # heartbeats keep a 3 s lease fresh
        assert seed.lease_fresh() and seed.member()["generation"] == 1

        seed.proxy.cut()
        lost = node.wait(lambda s: not s["lease_fresh"], 10, "the lease to lapse on the node")
        assert lost["phase"] in {"joined", "reconnecting"}
        node.wait(lambda s: s["phase"] == "reconnecting", 10, "reconnecting")
        time.sleep(0.5)
        assert not seed.lease_fresh()

        seed.proxy.restore()
        node.wait(_held(2), 15, "resume after the network returns")
        after = seed.member()
        assert after["node_id"] == before["node_id"]
        assert after["generation"] == 2
        assert after["incarnation"] != before["incarnation"]
        assert seed.lease_fresh()
        time.sleep(LEASE_SECONDS + 0.5)  # heartbeats renew the resumed lease
        assert seed.lease_fresh() and seed.member()["generation"] == 2
    finally:
        node.stop()


def test_cold_start_without_network_keeps_resuming_until_the_seed_answers(
    seed: Seed, node_cli: Path, tmp_path: Path
) -> None:
    first = Node(node_cli, tmp_path / "node", seed.pairing.channel_qr)
    first.wait(_held(1), 20, "join")
    before = seed.member()
    first.stop()

    seed.proxy.cut()
    second = Node(node_cli, tmp_path / "node", "resume")
    try:
        waiting = second.wait(
            lambda s: s["phase"] == "reconnecting" and (s["last_error"] or "").startswith("seed_unreachable"),
            15,
            "a failed resume while the network is down",
        )
        assert not waiting["lease_fresh"]
        time.sleep(1.5)  # several retries fail
        seed.proxy.restore()
        second.wait(_held(2), 15, "resume once the seed is reachable")
        after = seed.member()
        assert after["node_id"] == before["node_id"]
        assert after["incarnation"] != before["incarnation"]
        assert seed.lease_fresh()
    finally:
        second.stop()


def test_a_revoked_member_stops_and_never_regains_membership(seed: Seed, node_cli: Path, tmp_path: Path) -> None:
    node = Node(node_cli, tmp_path / "node", seed.pairing.channel_qr)
    try:
        node.wait(_held(1), 20, "join")
        member = seed.member()
        seed.coordinator.advance_member_generation(
            node_id=member["node_id"], expected_generation=1, lifecycle_state="STOPPED"
        )
        revoked = node.wait(lambda s: s["phase"] == "revoked", 15, "the node to see its revocation")
        assert not revoked["lease_fresh"]
        assert revoked["last_error"] == "seed_resume_refused:seed_resume_member_revoked"
        time.sleep(2.0)
        assert seed.member()["lifecycle_state"] == "STOPPED"
        assert seed.member()["generation"] == 2
        assert node.last["phase"] == "revoked"
    finally:
        node.stop()
