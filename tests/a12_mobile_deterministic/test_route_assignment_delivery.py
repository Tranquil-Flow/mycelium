# SPDX-License-Identifier: AGPL-3.0-only
"""A release-path node receives the owner's route assignment from its seed.

The product ``NodeCore`` (driven by ``native/mobile_core/examples/node_cli``, the
same code path the Android app runs, with no debug-only route file) joins a real
seed; the owner issues a route assignment with the operator command; the node
loads the digest-verified stage, allow-lists the peer and reports the assignment
applied.  Withdrawal unloads it, and a restarted (resumed) node re-applies it
under its new membership generation.
"""

from __future__ import annotations

from pathlib import Path
import time

import pytest

from mycelium_seed import route_assignment as ra

from .test_node_lease_recovery import Node, Seed, _held, node_cli  # noqa: F401 - shared fixture

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "native" / "mobile_core" / "tests" / "fixtures" / "gpt2_stage.json"
PEER = "0d7550754e0800a5d237eef5826035766b9b3e5a15868a940ab289958788e3b0"


@pytest.fixture
def seed(tmp_path: Path):
    seed = Seed(tmp_path / "seed")
    yield seed
    seed.close()


def _database(seed: Seed) -> Path:
    return Path(seed.coordinator._state.database)  # noqa: SLF001 - the seed's own store


def _wait_state(seed: Seed, node_id: str, state: str, timeout: float) -> dict:
    deadline = time.monotonic() + timeout
    record: dict = {}
    while time.monotonic() < deadline:
        record = ra.route_assignment_status(_database(seed), node_id=node_id)
        if record["state"] == state:
            return record
        time.sleep(0.1)
    pytest.fail(f"route assignment never reached {state}: {record}")


def test_the_node_applies_withdraws_and_reapplies_the_seed_assignment(
    seed: Seed, node_cli: Path, tmp_path: Path  # noqa: F811
) -> None:
    node = Node(node_cli, tmp_path / "node", seed.pairing.channel_qr)
    try:
        joined = node.wait(_held(1), 20, "join")
        assert joined["route_assignment"] is None and joined["stage_loaded"] is None
        member = seed.member()
        pack = ra.load_stage_pack(FIXTURE)
        record = ra.issue_route_assignment(
            _database(seed),
            node_id=member["node_id"],
            stage_pack=pack,
            peer_endpoint_id=PEER,
            route_generation=7,
        )
        applied = node.wait(
            lambda s: s["route_assignment"] == record["assignment_id"], 15, "the assignment to be applied"
        )
        assert applied["stage_loaded"] == pack["pack_digest"]
        assert applied["route_assignment_error"] is None
        assert applied["route_ready"] is False
        done = _wait_state(seed, member["node_id"], "applied", 10)
        assert done["applied_generation"] == 1

        ra.withdraw_route_assignment(_database(seed), node_id=member["node_id"])
        cleared = node.wait(lambda s: s["route_assignment"] is None, 15, "the withdrawal to unload the route")
        assert cleared["stage_loaded"] is None

        reissued = ra.issue_route_assignment(
            _database(seed),
            node_id=member["node_id"],
            stage_pack=pack,
            peer_endpoint_id=PEER,
            route_generation=7,
        )
        node.wait(lambda s: s["route_assignment"] == reissued["assignment_id"], 15, "the reissued assignment")
    finally:
        node.stop()

    # Process death: the restarted node resumes (next generation) and the seed
    # re-delivers; the old process's applied report no longer counts.
    restarted = Node(node_cli, tmp_path / "node", "resume")
    try:
        status = restarted.wait(
            lambda s: s["route_assignment"] == reissued["assignment_id"], 20, "the resumed node to re-apply"
        )
        assert status["member_generation"] == 2
        assert status["stage_loaded"] == pack["pack_digest"]
        record = _wait_state(seed, member["node_id"], "applied", 10)
        assert record["applied_generation"] == 2
    finally:
        restarted.stop()


def test_a_revoked_node_drops_its_route(seed: Seed, node_cli: Path, tmp_path: Path) -> None:  # noqa: F811
    node = Node(node_cli, tmp_path / "node", seed.pairing.channel_qr)
    try:
        node.wait(_held(1), 20, "join")
        member = seed.member()
        record = ra.issue_route_assignment(
            _database(seed),
            node_id=member["node_id"],
            stage_pack=ra.load_stage_pack(FIXTURE),
            peer_endpoint_id=PEER,
            route_generation=7,
        )
        node.wait(lambda s: s["route_assignment"] == record["assignment_id"], 15, "the assignment")
        seed.coordinator.advance_member_generation(
            node_id=member["node_id"], expected_generation=1, lifecycle_state="STOPPED"
        )
        revoked = node.wait(lambda s: s["phase"] == "revoked", 15, "the revocation")
        if revoked["route_assignment"] is not None:
            revoked = node.wait(lambda s: s["route_assignment"] is None, 5, "the route to be dropped")
        assert revoked["stage_loaded"] is None
    finally:
        node.stop()


class TamperingSeedProxy:
    """HTTP front for the seed that can corrupt or replay route deliveries."""

    def __init__(self) -> None:
        import http.server
        import threading
        import urllib.request

        proxy = self
        self.target: str | None = None
        self.mode: str | None = None
        self.captured: bytes | None = None

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *_args) -> None:
                return

            def _forward(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else None
                request = urllib.request.Request(
                    proxy.target + self.path,
                    data=body,
                    method=self.command,
                    headers={"Content-Type": "application/json"} if body is not None else {},
                )
                try:
                    with urllib.request.urlopen(request, timeout=5) as response:
                        status, payload = response.status, response.read()
                except urllib.error.HTTPError as error:
                    status, payload = error.code, error.read()
                if status == 200 and b"route_assignment_delivery" in payload:
                    payload = proxy._rewrite(payload)
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            do_GET = do_POST = _forward

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def _rewrite(self, payload: bytes) -> bytes:
        import json

        delivery = json.loads(payload)
        if delivery["envelope"]["message"]["assignment"] is not None and self.captured is None:
            self.captured = payload
        if self.mode == "tamper_signature":
            delivery["envelope"]["signature"]["signature"] = "A" * 86 + "=="
        elif self.mode == "tamper_peer" and delivery["envelope"]["message"]["assignment"] is not None:
            delivery["envelope"]["message"]["assignment"]["peer_endpoint_id"] = "e" * 64
        elif self.mode == "replay" and self.captured is not None:
            return self.captured
        elif self.mode == "tamper_pack" and delivery["stage_pack"] is not None:
            delivery["stage_pack"]["epsilon"] = 0.5
        else:
            return payload
        return json.dumps(delivery).encode()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def tampering_seed(tmp_path: Path):
    seed = Seed(tmp_path / "seed")
    proxy = TamperingSeedProxy()
    proxy.target = f"http://127.0.0.1:{seed.proxy.port}"
    # The invite the node pairs with points at the tampering proxy.
    seed.coordinator.seed_url = f"http://127.0.0.1:{proxy.port}"
    yield seed, proxy
    proxy.close()
    seed.close()


@pytest.mark.parametrize("mode", ["tamper_signature", "tamper_peer", "replay", "tamper_pack"])
def test_an_unverifiable_delivery_drops_the_applied_route(
    tampering_seed, node_cli: Path, tmp_path: Path, mode: str  # noqa: F811
) -> None:
    seed, proxy = tampering_seed
    node = Node(node_cli, tmp_path / "node", seed.pairing.channel_qr)
    try:
        node.wait(_held(1), 20, "join")
        member = seed.member()
        pack = ra.load_stage_pack(FIXTURE)
        first = ra.issue_route_assignment(
            _database(seed), node_id=member["node_id"], stage_pack=pack, peer_endpoint_id=PEER, route_generation=7
        )
        node.wait(lambda s: s["route_assignment"] == first["assignment_id"], 15, "the honest assignment")
        if mode in {"replay", "tamper_pack"}:
            # A newer assignment the node has not applied yet: the replayed old
            # answer (or a corrupted pack) must not resurrect or install anything.
            second = ra.issue_route_assignment(
                _database(seed), node_id=member["node_id"], stage_pack=pack, peer_endpoint_id=PEER, route_generation=8
            )
            assert second["assignment_id"] != first["assignment_id"]
        proxy.mode = mode
        dropped = node.wait(
            lambda s: s["route_assignment"] is None and s["route_assignment_error"] is not None,
            15,
            f"the node to fail closed on {mode}",
        )
        assert dropped["stage_loaded"] is None
        expected = {
            "tamper_signature": "membership_signature_invalid",
            "tamper_peer": "membership_signature_invalid",
            "replay": "route_assignment_request_mismatch",
            "tamper_pack": "stage_pack_digest_mismatch",
        }[mode]
        assert dropped["route_assignment_error"] == expected
        time.sleep(1.5)  # several more polls: nothing is ever applied while tampered
        assert node.last["route_assignment"] is None and node.last["stage_loaded"] is None
        assert ra.route_assignment_status(_database(seed), node_id=member["node_id"])["state"] != "applied"
    finally:
        node.stop()
