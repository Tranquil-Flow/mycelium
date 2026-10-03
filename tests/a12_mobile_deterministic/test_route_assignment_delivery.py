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
