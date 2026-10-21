# SPDX-License-Identifier: AGPL-3.0-only
"""Owner-issued route assignments delivered to the enrolled member only.

The owner records one assignment (digest-verified stage pack, route peer, route
generation) for one activation-eligible Android member; the running seed answers
only that member's signed, current-generation request, over ``/seed/message``,
with a seed-signed statement bound to the request.
"""

from __future__ import annotations

from http.client import HTTPConnection
from itertools import count
import json
from pathlib import Path

import pytest

from mycelium_invite import SqliteInviteRegistry, verify_invite_bundle
from mycelium_membership import ROUTER_ACTIVATION_PROTOCOL
from mycelium_node import NodeMembershipSession, load_or_create_node_signer
from mycelium_qualification.evidence import canonical_json_bytes
from mycelium_qualification.signing import generate_ed25519_signer
from mycelium_seed import SeedCoordinator, SeedCoordinatorError, SqliteSeedState
from mycelium_seed.http import SeedHTTPServer
from mycelium_seed import route_assignment as ra

ROOT = Path(__file__).resolve().parents[2]
PACK = json.loads((ROOT / "native/mobile_core/tests/fixtures/gpt2_stage.json").read_text())["pack"]
PEER = "0d7550754e0800a5d237eef5826035766b9b3e5a15868a940ab289958788e3b0"
NOW = 5_000.0
ANDROID = {
    "runtime_backend": "mobile-rust-reference",
    "transport": "iroh",
    "activation_protocol": ROUTER_ACTIVATION_PROTOCOL,
}
# A still-ineligible class for the refusal case (windows_native_iroh is
# activation-eligible since the 2026-10-07 owner parity ruling).
WINDOWS = {"runtime_backend": "tbd", "transport": "iroh", "activation_protocol": None}


def _ids(prefix: str):
    values = count(1)
    return lambda: f"{prefix}-{next(values)}"


class Swarm:
    def __init__(self, tmp_path: Path) -> None:
        self.clock = [NOW]
        root = tmp_path / "seed"
        root.mkdir(mode=0o700)
        self.database = root / "state.sqlite3"
        self.coordinator = SeedCoordinator(
            swarm_id="swarm-a",
            seed_node_id="seed-node",
            seed_url="http://127.0.0.1:8788",
            # On-disk seed key bound to the state, as the operator commands require.
            signer=load_or_create_node_signer(root / "identity" / "seed.key"),
            invite_registry=SqliteInviteRegistry(self.database),
            state=SqliteSeedState(self.database),
            incarnation="seed-incarnation",
            clock=lambda: self.clock[0],
            id_source=_ids("seed-message"),
            lease_seconds=300.0,
        )
        self.tmp = tmp_path
        self.nodes: dict[str, NodeMembershipSession] = {}
        self.requests = 0

    def join(self, node_id: str, peer_class: str, capability: dict) -> NodeMembershipSession:
        node = NodeMembershipSession(
            node_id=node_id,
            swarm_id="swarm-a",
            seed_node_id="seed-node",
            signer=load_or_create_node_signer(self.tmp / "nodes" / f"{node_id}.key"),
            incarnation=f"incarnation-{node_id}",
            software_version="mycelium-test",
            peer_class=peer_class,
            runtime_capability=capability,
            clock=lambda: self.clock[0],
            id_source=_ids(f"{node_id}-message"),
        )
        bundle = self.coordinator.mint_invite(nonce=f"invite-{node_id}", ttl_seconds=120)
        verified = verify_invite_bundle(bundle, now=self.clock[0])
        request = node.join_request(
            invite_nonce=verified["payload"]["nonce"],
            endpoint_addrs=["https://127.0.0.1:9443/control"],
        )
        acceptance = self.coordinator.accept_join(invite_token=bundle["token"], join_envelope=request)
        node.accept_join(acceptance, seed_key_digest=verified["seed_key_digest"])
        self.nodes[node_id] = node
        return node

    def service(self) -> ra.RouteAssignmentService:
        return ra.service_for(self.coordinator)

    def request(self, node_id: str, *, applied: str | None = None, generation: int | None = None,
                incarnation: str | None = None, signer=None, issued_at: float | None = None,
                message_id: str | None = None) -> dict:
        node = self.nodes[node_id]
        member = self.coordinator.member(node_id)
        signer = signer or node.signer  # the member's own enrolled key
        issued = self.clock[0] if issued_at is None else issued_at
        if message_id is None:
            self.requests += 1
            message_id = f"route-request-{self.requests}"
        message = {
            "protocol": ra.ROUTE_ASSIGNMENT_REQUEST_PROTOCOL,
            "message_id": message_id,
            "swarm_id": "swarm-a",
            "sender_node_id": node_id,
            "sender_endpoint_id": member["endpoint_id"],
            "recipient_node_id": "seed-node",
            "incarnation": incarnation or member["incarnation"],
            "generation": generation or member["generation"],
            "issued_at": issued,
            "expires_at": issued + 60.0,
            "applied_assignment_id": applied,
        }
        return {
            "protocol": ra.SIGNED_MESSAGE_PROTOCOL,
            "message": message,
            "signature": signer.sign(message),
            "verification_key": signer.public_key_record(),
        }

    def issue(self, node_id: str, **overrides) -> dict:
        arguments = dict(
            node_id=node_id,
            stage_pack=PACK,
            peer_endpoint_id=PEER,
            route_generation=7,
            now=lambda: self.clock[0],
            id_source=lambda: "route-1",
        )
        arguments.update(overrides)
        return ra.issue_route_assignment(self.database, **arguments)


@pytest.fixture()
def swarm(tmp_path: Path) -> Swarm:
    swarm = Swarm(tmp_path)
    swarm.join("node-phone", "android_native_iroh", ANDROID)
    return swarm


def _verify(swarm: Swarm, delivery: dict, node_id: str = "node-phone") -> dict:
    # A v2 manifest also carries its seed-signed A2 grant; a v1 pack never does.
    assert set(delivery) - {"artifact_grant"} == {"protocol", "envelope", "stage_pack"}
    assert ("artifact_grant" in delivery) == (
        isinstance(delivery["stage_pack"], dict) and delivery["stage_pack"].get("protocol") == ra.SIGNED_MESSAGE_PROTOCOL
    )
    assert delivery["protocol"] == ra.ROUTE_ASSIGNMENT_DELIVERY_PROTOCOL
    envelope = delivery["envelope"]
    assert envelope["verification_key"] == swarm.coordinator.signer.public_key_record()
    from mycelium_qualification.signing import build_ed25519_verifier

    verify = build_ed25519_verifier([envelope["verification_key"]])
    assert verify(canonical_json_bytes(envelope["message"]), envelope["signature"])
    message = envelope["message"]
    assert message["recipient_node_id"] == node_id
    assert message["route_ready"] is False
    return message


def test_the_member_receives_its_signed_assignment_and_pack(swarm: Swarm) -> None:
    record = swarm.issue("node-phone")
    assert record["state"] == "issued"
    assert record["stage_pack_digest"] == PACK["pack_digest"]
    delivery = swarm.service().answer(swarm.request("node-phone"))
    message = _verify(swarm, delivery)
    member = swarm.coordinator.member("node-phone")
    assert message["recipient_endpoint_id"] == member["endpoint_id"]
    assert message["generation"] == member["generation"]
    assert message["request_message_id"] == "route-request-1"
    assert message["assignment"] == {
        "assignment_id": "route-1",
        "stage_pack_digest": PACK["pack_digest"],
        "peer_endpoint_id": PEER,
        "route_generation": 7,
        "forward_to": True,
        "expires_at": NOW + ra.DEFAULT_ASSIGNMENT_TTL_SECONDS,
        "controller_endpoint_ids": [],
    }
    assert delivery["stage_pack"] == PACK
    status = ra.route_assignment_status(swarm.database, node_id="node-phone", now=lambda: NOW)
    assert status["state"] == "delivered"

    # The member reports it applied; a restarted member that has not yet re-applied it
    # is not counted as applied.
    swarm.service().answer(swarm.request("node-phone", applied="route-1", message_id="route-request-2"))
    status = ra.route_assignment_status(swarm.database, node_id="node-phone", now=lambda: NOW)
    assert status["state"] == "applied" and status["applied_generation"] == member["generation"]
    swarm.service().answer(swarm.request("node-phone", message_id="route-request-3"))
    assert ra.route_assignment_status(swarm.database, node_id="node-phone", now=lambda: NOW)["state"] == "delivered"


def test_no_assignment_withdrawal_and_expiry_answer_a_signed_none(swarm: Swarm) -> None:
    delivery = swarm.service().answer(swarm.request("node-phone"))
    assert _verify(swarm, delivery)["assignment"] is None and delivery["stage_pack"] is None
    swarm.issue("node-phone", ttl_seconds=100.0)
    assert ra.withdraw_route_assignment(swarm.database, node_id="node-phone")["state"] == "withdrawn"
    assert _verify(swarm, swarm.service().answer(swarm.request("node-phone")))["assignment"] is None
    swarm.issue("node-phone", ttl_seconds=100.0, id_source=lambda: "route-2")
    assert _verify(swarm, swarm.service().answer(swarm.request("node-phone")))["assignment"]["assignment_id"] == "route-2"
    swarm.clock[0] = NOW + 100.0  # the 300 s lease still holds; the assignment expired
    assert _verify(swarm, swarm.service().answer(swarm.request("node-phone")))["assignment"] is None


def test_issue_refuses_ineligible_unknown_or_malformed_assignments(swarm: Swarm) -> None:
    # An ineligible class: the undeclared Linux placeholder (every native product
    # class, including linux_native_iroh, is eligible since the 2026-10-07 parity ruling).
    swarm.join("node-windows", "linux_tbd", {"runtime_backend": "tbd", "transport": "none", "activation_protocol": None})
    swarm.join("node-mac", "mac_mlx_iroh", {**ANDROID, "runtime_backend": "mlx"})

    def code(**kwargs) -> str:
        with pytest.raises(SeedCoordinatorError) as caught:
            swarm.issue(**kwargs)
        return caught.value.code

    assert code(node_id="node-windows") == "seed_member_activation_ineligible"
    assert code(node_id="node-mac") == "seed_member_activation_ineligible"
    assert code(node_id="node-ghost") == "seed_member_unknown"
    assert code(node_id="node-phone", peer_endpoint_id="not-hex") == "seed_route_assignment_peer_invalid"
    assert code(node_id="node-phone", route_generation=0) == "seed_route_assignment_generation_invalid"
    assert code(node_id="node-phone", ttl_seconds=0.0) == "seed_route_assignment_ttl_invalid"
    tampered = {**PACK, "epsilon": 0.5}
    assert code(node_id="node-phone", stage_pack=tampered).startswith("seed_route_assignment_stage_pack_invalid")
    member = swarm.coordinator.member("node-phone")
    swarm.coordinator.advance_member_generation(
        node_id="node-phone", expected_generation=member["generation"], lifecycle_state="STOPPED"
    )
    assert code(node_id="node-phone") == "seed_member_revoked"


def test_only_the_current_member_with_its_own_key_is_answered(swarm: Swarm) -> None:
    swarm.issue("node-phone")
    service = swarm.service()

    def code(envelope: dict) -> str:
        with pytest.raises(SeedCoordinatorError) as caught:
            service.answer(envelope)
        return caught.value.code

    stranger = generate_ed25519_signer(endpoint_id="stranger")
    assert code(swarm.request("node-phone", signer=stranger)) == "membership_key_pin_mismatch"
    forged = swarm.request("node-phone")
    forged["message"]["applied_assignment_id"] = "route-9"
    assert code(forged) == "membership_signature_invalid"
    assert code(swarm.request("node-phone", generation=9)) == "seed_state_member_stale"
    assert code(swarm.request("node-phone", incarnation="incarnation-old")) == "seed_state_member_stale"
    assert code(swarm.request("node-phone", issued_at=NOW - 120.0)) == "membership_message_expired"
    assert code(swarm.request("node-phone", issued_at=NOW + 120.0)) == "membership_message_from_future"
    extra = swarm.request("node-phone")
    extra["message"]["peer_endpoint_id"] = PEER
    assert code(extra) == "seed_message_malformed"
    assert code({"protocol": ra.SIGNED_MESSAGE_PROTOCOL}) == "seed_message_malformed"

    # A lapsed lease or a revoked member is not answered.
    swarm.clock[0] = NOW + 301.0
    assert code(swarm.request("node-phone")) == "seed_member_lease_expired"
    swarm.clock[0] = NOW
    member = swarm.coordinator.member("node-phone")
    swarm.coordinator.advance_member_generation(
        node_id="node-phone", expected_generation=member["generation"], lifecycle_state="STOPPED"
    )
    assert code(swarm.request("node-phone", generation=member["generation"] + 1)) == "seed_member_revoked"


def test_an_assignment_never_follows_a_re_enrolled_device(swarm: Swarm) -> None:
    swarm.issue("node-phone")
    import sqlite3

    with sqlite3.connect(swarm.database) as connection:
        connection.execute(
            "UPDATE seed_route_assignments SET verification_key_digest = ? WHERE node_id = ?",
            ("sha256:" + "0" * 64, "node-phone"),
        )
    assert _verify(swarm, swarm.service().answer(swarm.request("node-phone")))["assignment"] is None


def test_the_seed_http_member_channel_carries_the_request(swarm: Swarm) -> None:
    swarm.issue("node-phone")
    swarm.coordinator.seed_url = None  # invites are minted; let the server bind its own URL
    with SeedHTTPServer(swarm.coordinator, host="127.0.0.1", port=0) as server:
        server.start()
        host, port = server.base_url.removeprefix("http://").split(":")
        body = canonical_json_bytes(
            {"protocol": "mycelium.seed.member_http.v1", "envelope": swarm.request("node-phone")}
        )
        connection = HTTPConnection(host, int(port), timeout=5)
        connection.request("POST", "/seed/message", body=body, headers={"Content-Type": "application/json"})
        response = connection.getresponse()
        delivery = json.loads(response.read())
        assert response.status == 200
        assert _verify(swarm, delivery)["assignment"]["assignment_id"] == "route-1"

        stranger = generate_ed25519_signer(endpoint_id="stranger")
        body = canonical_json_bytes(
            {"protocol": "mycelium.seed.member_http.v1", "envelope": swarm.request("node-phone", signer=stranger)}
        )
        connection = HTTPConnection(host, int(port), timeout=5)
        connection.request("POST", "/seed/message", body=body, headers={"Content-Type": "application/json"})
        response = connection.getresponse()
        assert response.status == 401
        assert json.loads(response.read())["error"]["code"] == "membership_key_pin_mismatch"


def test_the_controller_set_is_seed_authority_and_never_a_member_request(swarm: Swarm) -> None:
    """The seed signs the controller set the phone answers on its control ALPN.

    It is authority, not a request: the set is validated at issue, carried in
    the signed statement, sorted and deduplicated, and a duplicate or malformed
    endpoint id is refused rather than normalized into an allow-list.
    """

    controllers = ["b" * 64, "a" * 64]
    record = swarm.issue("node-phone", controller_endpoint_ids=controllers)
    assert record["controller_endpoint_ids"] == sorted(controllers)

    message = _verify(swarm, swarm.service().answer(swarm.request("node-phone")))
    assert message["assignment"]["controller_endpoint_ids"] == sorted(controllers)

    # Duplicate authority is refused, not silently normalized or installed.
    with pytest.raises(SeedCoordinatorError) as duplicate:
        swarm.issue("node-phone", controller_endpoint_ids=["a" * 64, "a" * 64, "b" * 64], id_source=lambda: "route-2")
    assert duplicate.value.code == "seed_route_assignment_controller_invalid"
    again = _verify(swarm, swarm.service().answer(swarm.request("node-phone")))
    assert again["assignment"]["assignment_id"] == "route-1"
    assert again["assignment"]["controller_endpoint_ids"] == ["a" * 64, "b" * 64]

    def refused(**overrides) -> str:
        arguments = dict(controller_endpoint_ids=["not-hex"])
        arguments.update(overrides)
        with pytest.raises(SeedCoordinatorError) as caught:
            swarm.issue("node-phone", id_source=lambda: "route-x", **arguments)
        return caught.value.code

    assert refused() == "seed_route_assignment_controller_invalid"
    assert refused(controller_endpoint_ids=["a" * 63]) == "seed_route_assignment_controller_invalid"
    assert refused(controller_endpoint_ids=["a" * 64, "a" * 64]) == "seed_route_assignment_controller_invalid"
    assert refused(controller_endpoint_ids=[7]) == "seed_route_assignment_controller_invalid"
    assert refused(controller_endpoint_ids="a" * 64) == "seed_route_assignment_controller_invalid"
    assert refused(controller_endpoint_ids=[f"{index:064x}" for index in range(17)]) == "seed_route_assignment_controller_invalid"

    # A withdrawal stops authorizing anything: the next signed answer is null.
    assert ra.withdraw_route_assignment(swarm.database, node_id="node-phone")["state"] == "withdrawn"
    assert _verify(swarm, swarm.service().answer(swarm.request("node-phone")))["assignment"] is None


def test_existing_assignment_database_gains_empty_controller_authority(swarm: Swarm) -> None:
    import sqlite3

    original = swarm.issue("node-phone")
    # Exercise the exact prior table shape while retaining an actual assignment.
    with sqlite3.connect(swarm.database) as database:
        database.execute("ALTER TABLE seed_route_assignments DROP COLUMN controller_endpoint_ids_json")
    record = ra.route_assignment_status(swarm.database, node_id="node-phone", now=lambda: NOW)
    assert record["controller_endpoint_ids"] == []
    assert record["assignment_id"] == original["assignment_id"]
    assert record["stage_pack_digest"] == original["stage_pack_digest"]
    delivery = _verify(swarm, swarm.service().answer(swarm.request("node-phone")))
    assert delivery["assignment"]["controller_endpoint_ids"] == []
    # Reopening is idempotent, and explicit new authority can then be issued.
    replacement = swarm.issue("node-phone", controller_endpoint_ids=["a" * 64], id_source=lambda: "route-migrated")
    assert replacement["controller_endpoint_ids"] == ["a" * 64]


@pytest.mark.parametrize("encoded", ['null', '{}', '"abc"', '[1]', '["not-hex"]', '["' + 'a' * 64 + '","' + 'a' * 64 + '"]'])
def test_invalid_stored_controllers_are_never_signed(swarm: Swarm, encoded: str) -> None:
    import sqlite3

    swarm.issue("node-phone")
    with sqlite3.connect(swarm.database) as database:
        database.execute("UPDATE seed_route_assignments SET controller_endpoint_ids_json = ?", (encoded,))
    with pytest.raises(SeedCoordinatorError) as caught:
        swarm.service().answer(swarm.request("node-phone"))
    assert caught.value.code == "seed_route_assignment_controller_invalid"


def test_operator_command_issues_and_reports_status(swarm: Swarm, tmp_path: Path, capsys) -> None:
    fixture = ROOT / "native/mobile_core/tests/fixtures/gpt2_stage.json"
    state_root = swarm.database.parent
    # Real wall clock for the CLI: refresh the member's lease relative to now.
    import sqlite3
    import time

    with sqlite3.connect(swarm.database) as connection:
        connection.execute("UPDATE seed_members SET lease_expires_at = ?", (time.time() + 300.0,))
    argv = ["--state-root", str(state_root)]
    assert ra.main([*argv, "issue", "--stage-pack", str(fixture), "--peer-endpoint-id", PEER, "--route-generation", "7"]) == 0
    issued = json.loads(capsys.readouterr().out)
    assert issued["node_id"] == "node-phone" and issued["state"] == "issued"
    assert issued["peer_endpoint_id"] == PEER and issued["route_generation"] == 7
    assert ra.main([*argv, "status", "--wait-applied", "0.5"]) == 1
    assert json.loads(capsys.readouterr().out)["state"] == "issued"
    assert ra.main([*argv, "withdraw"]) == 0
    assert json.loads(capsys.readouterr().out)["state"] == "withdrawn"


def test_a_replayed_request_is_refused_and_records_nothing(swarm: Swarm) -> None:
    swarm.issue("node-phone")
    service = swarm.service()
    envelope = swarm.request("node-phone", applied="route-1", message_id="route-request-replay")
    service.answer(envelope)
    first = ra.route_assignment_status(swarm.database, node_id="node-phone", now=lambda: NOW)
    assert first["state"] == "applied"
    swarm.clock[0] = NOW + 5.0
    with pytest.raises(SeedCoordinatorError) as caught:
        service.answer(envelope)
    assert caught.value.code == "seed_message_replayed"
    # The replay changed nothing (same delivery time) and shares the seed_replay table.
    again = ra.route_assignment_status(swarm.database, node_id="node-phone", now=lambda: NOW)
    assert again["delivered_at"] == first["delivered_at"] == NOW
    import sqlite3

    with sqlite3.connect(swarm.database) as connection:
        rows = connection.execute(
            "SELECT generation FROM seed_replay WHERE node_id = ? AND message_id = ?",
            ("node-phone", "route-request-replay"),
        ).fetchall()
    assert rows == [(swarm.coordinator.member("node-phone")["generation"],)]


def test_a_refused_request_leaves_no_delivery_or_replay_record(swarm: Swarm) -> None:
    swarm.issue("node-phone")
    with pytest.raises(SeedCoordinatorError):
        swarm.service().answer(swarm.request("node-phone", generation=9, message_id="route-request-stale"))
    assert ra.route_assignment_status(swarm.database, node_id="node-phone", now=lambda: NOW)["state"] == "issued"
    import sqlite3

    with sqlite3.connect(swarm.database) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM seed_replay WHERE message_id = 'route-request-stale'"
        ).fetchone() == (0,)


def test_another_member_can_neither_fetch_nor_ack_the_assignment(swarm: Swarm) -> None:
    swarm.join("node-other", "android_native_iroh", ANDROID)
    swarm.issue("node-phone")
    service = swarm.service()
    other = swarm.nodes["node-other"].signer
    # The other member signs a request claiming to be the phone: wrong key.
    with pytest.raises(SeedCoordinatorError) as caught:
        service.answer(swarm.request("node-phone", signer=other, applied="route-1"))
    assert caught.value.code == "membership_key_pin_mismatch"
    # Asking as itself, it only ever learns that it has no assignment.
    own = service.answer(swarm.request("node-other", message_id="route-request-other"))
    assert _verify(swarm, own, node_id="node-other")["assignment"] is None and own["stage_pack"] is None
    assert ra.route_assignment_status(swarm.database, node_id="node-phone", now=lambda: NOW)["state"] == "issued"


def test_after_a_resume_only_the_new_generation_and_incarnation_are_answered(swarm: Swarm) -> None:
    swarm.issue("node-phone")
    old = swarm.coordinator.member("node-phone")
    stale = swarm.request("node-phone", message_id="route-request-old")
    previous = swarm.nodes["node-phone"]
    restarted = NodeMembershipSession(
        node_id="node-phone",
        swarm_id="swarm-a",
        seed_node_id="seed-node",
        signer=previous.signer,
        incarnation="incarnation-node-phone-2",
        software_version="mycelium-test",
        peer_class="android_native_iroh",
        runtime_capability=ANDROID,
        clock=lambda: swarm.clock[0],
        id_source=_ids("node-phone-restart"),
    )
    resume = restarted.resume_request(
        previous_generation=old["generation"],
        previous_incarnation=old["incarnation"],
        endpoint_addrs=["https://127.0.0.1:9443/control"],
    )
    swarm.coordinator.accept_resume(resume_envelope=resume)
    swarm.nodes["node-phone"] = restarted
    current = swarm.coordinator.member("node-phone")
    assert current["generation"] == old["generation"] + 1
    with pytest.raises(SeedCoordinatorError) as caught:
        swarm.service().answer(stale)
    assert caught.value.code == "seed_state_member_stale"
    fresh = swarm.service().answer(swarm.request("node-phone", message_id="route-request-new"))
    message = _verify(swarm, fresh)
    assert message["generation"] == current["generation"]
    assert message["assignment"]["assignment_id"] == "route-1"


def _cli(argv: list[str], capsys) -> tuple[int, dict]:
    code = ra.main(argv)
    captured = capsys.readouterr()
    return code, json.loads(captured.out or captured.err)


def test_operator_command_takes_the_seed_authority_and_refuses_unsafe_roots(swarm: Swarm, tmp_path: Path, capsys) -> None:
    import os

    fixture = ROOT / "native/mobile_core/tests/fixtures/gpt2_stage.json"
    issue = ["issue", "--node-id", "node-phone", "--stage-pack", str(fixture), "--peer-endpoint-id", PEER, "--route-generation", "7"]
    root = swarm.database.parent
    # A world-readable state root is not the owner's seed authority.
    os.chmod(root, 0o755)
    code, out = _cli(["--state-root", str(root), *issue], capsys)
    assert code == 2 and out["error"].startswith("seed_operator_")
    os.chmod(root, 0o700)
    # A symlink to the state root is refused, not followed.
    link = tmp_path / "seed-link"
    link.symlink_to(root)
    code, out = _cli(["--state-root", str(link), *issue], capsys)
    assert code == 2 and out["error"].startswith("seed_operator_")
    # A directory without the bound seed key is refused.
    other = tmp_path / "not-a-seed"
    other.mkdir(mode=0o700)
    code, out = _cli(["--state-root", str(other), *issue], capsys)
    assert code == 2 and out["error"].startswith("seed_operator_")
    # A seed key that is not the one bound to the state is refused.
    key = root / "identity" / "seed.key"
    original = key.read_bytes()
    key.unlink()
    load_or_create_node_signer(key)
    code, out = _cli(["--state-root", str(root), *issue], capsys)
    assert code == 2 and out["error"].startswith("seed_operator_identity")
    key.unlink()
    key.write_bytes(original)
    os.chmod(key, 0o600)
    with pytest.raises(SeedCoordinatorError):
        ra.route_assignment_status(swarm.database, node_id="node-phone")


def test_operator_writes_are_safe_next_to_a_live_seed_serving_the_member(swarm: Swarm, capsys) -> None:
    """The lease is not exclusive: the command runs while the seed answers the
    member, and SQLite serializes every write, so no answer or command fails and
    each answer reflects one consistent assignment."""

    import sqlite3
    import threading
    import time as wall

    fixture = ROOT / "native/mobile_core/tests/fixtures/gpt2_stage.json"
    root = swarm.database.parent
    with sqlite3.connect(swarm.database) as connection:
        connection.execute("UPDATE seed_members SET lease_expires_at = ?", (wall.time() + 300.0,))
    swarm.clock[0] = wall.time()
    live = ra.service_for(swarm.coordinator)
    live.clock = wall.time
    errors: list[str] = []
    seen: set[str | None] = set()
    stop = threading.Event()

    def member(lane: int) -> None:
        sequence = 0
        while not stop.is_set():
            sequence += 1
            swarm.clock[0] = wall.time()
            try:
                answer = live.answer(swarm.request("node-phone", message_id=f"live-{lane}-{sequence}"))
                assignment = answer["envelope"]["message"]["assignment"]
                if assignment is not None:
                    assert answer["stage_pack"]["pack_digest"] == assignment["stage_pack_digest"]
                    seen.add(assignment["assignment_id"])
                else:
                    assert answer["stage_pack"] is None
                    seen.add(None)
            except Exception as exc:  # noqa: BLE001 - collected for the assertion
                errors.append(repr(exc))

    threads = [threading.Thread(target=member, args=(lane,)) for lane in range(3)]
    for thread in threads:
        thread.start()
    try:
        base = ["--state-root", str(root)]
        issued = []
        for round_ in range(6):
            code, out = _cli([*base, "issue", "--node-id", "node-phone", "--stage-pack", str(fixture),
                              "--peer-endpoint-id", PEER, "--route-generation", str(7 + round_)], capsys)
            assert code == 0, out
            issued.append(out["assignment_id"])
            if round_ % 2:
                code, out = _cli([*base, "withdraw", "--node-id", "node-phone"], capsys)
                assert code == 0 and out["state"] == "withdrawn", out
    finally:
        stop.set()
        for thread in threads:
            thread.join(timeout=30)
    assert errors == []
    assert seen and seen <= {None, *issued}
    code, out = _cli(["--state-root", str(root), "status", "--node-id", "node-phone"], capsys)
    assert code == 0 and out["assignment_id"] == issued[-1] and out["state"] == "withdrawn"  # the last round withdrew it


# --- v2: seed-signed Qwen2 stage manifests (weights pinned by digest) -------

V2 = json.loads((ROOT / "native/mobile_core/tests/fixtures/qwen2_stage_manifest_v2.json").read_text())
SOURCE = {"endpoint_id": "c" * 64, "verification_key_digest": "sha256:" + "d" * 64}


def test_a_v2_manifest_is_issued_only_seed_signed_and_delivered_beside_the_statement(swarm: Swarm) -> None:
    from mycelium_mobile import qwen_stage_manifest as qm

    seed = swarm.coordinator.signer
    signed = qm.sign_stage_manifest(V2["manifest"], seed)
    for pack, code in [
        (V2["manifest"], "seed_route_assignment_stage_manifest_unsigned"),
        (signed, "seed_route_assignment_stage_manifest_signer_unknown"),
    ]:
        with pytest.raises(SeedCoordinatorError) as caught:
            swarm.issue("node-phone", stage_pack=pack)
        assert caught.value.code == code
    # Signed by a key that is not this seed's.
    with pytest.raises(SeedCoordinatorError) as caught:
        swarm.issue("node-phone", stage_pack=V2["signed"], seed_key_record=seed.public_key_record())
    assert caught.value.code == "seed_route_assignment_stage_pack_invalid:stage_manifest_signer_mismatch"
    tampered = json.loads(json.dumps(signed))
    tampered["message"]["golden"]["seed"] = 43
    with pytest.raises(SeedCoordinatorError) as caught:
        swarm.issue("node-phone", stage_pack=tampered, seed_key_record=seed.public_key_record())
    assert caught.value.code == "seed_route_assignment_stage_pack_invalid:stage_manifest_digest_mismatch"

    with pytest.raises(SeedCoordinatorError) as caught:
        swarm.issue("node-phone", stage_pack=signed, seed_key_record=seed.public_key_record())
    assert caught.value.code == "seed_route_assignment_artifact_sources_required"
    with pytest.raises(SeedCoordinatorError) as caught:
        swarm.issue("node-phone", artifact_sources=[SOURCE])  # a v1 pack names none
    assert caught.value.code == "seed_route_assignment_artifact_sources_invalid"
    record = swarm.issue("node-phone", stage_pack=signed, seed_key_record=seed.public_key_record(), artifact_sources=[SOURCE])
    assert record["stage_pack_digest"] == V2["manifest"]["pack_digest"]
    delivery = swarm.service().answer(swarm.request("node-phone"))
    message = _verify(swarm, delivery)
    assert message["assignment"]["stage_pack_digest"] == V2["manifest"]["pack_digest"]
    assert delivery["stage_pack"] == signed
    assert qm.verify_signed_stage_manifest(delivery["stage_pack"], seed.public_key_record()) == V2["manifest"]
    # The A2 grant: seed-signed, for exactly the manifest's objects, bound to
    # this member's node, endpoint and key, authorizing the named source.
    from mycelium_mobile import artifact_grant as ag

    grant = ag.verify_grant(delivery["artifact_grant"], seed_key_record=seed.public_key_record(), now_unix_ms=int(NOW * 1000))
    member = swarm.coordinator.member("node-phone")
    assert (grant["recipient_node_id"], grant["recipient_endpoint_id"]) == ("node-phone", member["endpoint_id"])
    assert grant["recipient_key_digest"] == swarm.nodes["node-phone"].signer.verification_key_digest
    assert grant["assignment_id"] == "route-1" and grant["manifest_pack_digest"] == V2["manifest"]["pack_digest"]
    assert grant["allowed_object_digests"] == sorted({t["sha256"] for t in V2["manifest"]["tensors"]})
    assert grant["authorized_sources"] == [{**SOURCE, "direct_addrs": []}]
    assert ra.parse_artifact_source(f"{'c' * 64}:sha256:{'d' * 64}@127.0.0.1:4433,[::1]:9") == {
        "endpoint_id": "c" * 64, "verification_key_digest": "sha256:" + "d" * 64,
        "direct_addrs": ["127.0.0.1:4433", "[::1]:9"],
    }
    with pytest.raises(SeedCoordinatorError) as caught:
        swarm.issue("node-phone", stage_pack=signed, seed_key_record=seed.public_key_record(),
                    artifact_sources=[{**SOURCE, "direct_addrs": ["localhost:1"]}], id_source=lambda: "route-x")
    assert caught.value.code == "seed_route_assignment_artifact_sources_invalid"
    assert grant["expires_at_unix_ms"] == int(NOW * 1000) + ag.DEFAULT_GRANT_TTL_MS
    # A v1 delivery carries no grant.
    swarm.issue("node-phone", id_source=lambda: "route-2")
    assert "artifact_grant" not in swarm.service().answer(swarm.request("node-phone"))


def test_operator_command_signs_an_unsigned_v2_manifest_with_the_seed_key(swarm: Swarm, tmp_path: Path, capsys) -> None:
    from mycelium_mobile import qwen_stage_manifest as qm
    import sqlite3
    import time

    manifest_file = tmp_path / "manifest.json"
    manifest_file.write_bytes(qm.canonical_ascii(V2["manifest"]))
    with sqlite3.connect(swarm.database) as connection:
        connection.execute("UPDATE seed_members SET lease_expires_at = ?", (time.time() + 300.0,))
    argv = ["--state-root", str(swarm.database.parent)]
    source = f"{SOURCE['endpoint_id']}:{SOURCE['verification_key_digest']}"
    assert ra.main([*argv, "issue", "--stage-pack", str(manifest_file), "--peer-endpoint-id", PEER,
                    "--route-generation", "7", "--artifact-source", source]) == 0
    issued = json.loads(capsys.readouterr().out)
    assert issued["stage_pack_digest"] == V2["manifest"]["pack_digest"]
    with sqlite3.connect(swarm.database) as connection:
        (stored,) = connection.execute("SELECT stage_pack_json FROM seed_route_assignments").fetchone()
    envelope = json.loads(stored)
    assert qm.verify_signed_stage_manifest(envelope, swarm.coordinator.signer.public_key_record()) == V2["manifest"]
    # A v2 manifest signed by another key is refused by the command.
    foreign = tmp_path / "foreign.json"
    foreign.write_text(json.dumps(V2["signed"]))
    assert ra.main([*argv, "issue", "--stage-pack", str(foreign), "--peer-endpoint-id", PEER,
                    "--route-generation", "8"]) == 1
    assert "stage_manifest_signer_mismatch" in capsys.readouterr().err
