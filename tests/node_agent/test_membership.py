from __future__ import annotations

from itertools import count
from pathlib import Path

import pytest

from mycelium_membership import (
    ASSIGNMENT_OFFER_PROTOCOL,
    ASSIGNMENT_RESULT_PROTOCOL,
    CAPABILITY_REPORT_PROTOCOL,
    DRAIN_ACK_PROTOCOL,
    HEARTBEAT_PROTOCOL,
    JOIN_ACCEPTANCE_PROTOCOL,
    LINK_PROBE_REPORT_PROTOCOL,
    MembershipContractError,
    sign_membership_message,
    verify_join_request,
    verify_membership_message,
)
from mycelium_node.identity import load_or_create_node_signer
from mycelium_node import membership as node_membership_module
from mycelium_node.membership import NodeMembershipError, NodeMembershipSession
from mycelium_qualification.signing import generate_ed25519_signer


NOW = 1_000.0
MAC_RUNTIME_CAPABILITY = {
    "runtime_backend": "mlx",
    "transport": "iroh",
    "activation_protocol": "mycelium.router_wire.v1",
}


def _ids(prefix: str):
    values = count(1)
    return lambda: f"{prefix}-{next(values)}"


def _session(tmp_path: Path) -> NodeMembershipSession:
    return NodeMembershipSession(
        node_id="node-a",
        swarm_id="swarm-a",
        seed_node_id="seed-node",
        signer=load_or_create_node_signer(tmp_path / "private" / "node.key"),
        incarnation="incarnation-a",
        software_version="mycelium-test",
        peer_class="mac_mlx_iroh",
        runtime_capability=MAC_RUNTIME_CAPABILITY,
        clock=lambda: NOW,
        id_source=_ids("node-message"),
    )


def _acceptance(session: NodeMembershipSession, request: dict, seed_signer, *, generation: int = 1):
    message = {
        "protocol": JOIN_ACCEPTANCE_PROTOCOL,
        "message_id": "seed-acceptance-1",
        "swarm_id": "swarm-a",
        "sender_node_id": "seed-node",
        "sender_endpoint_id": seed_signer.endpoint_id,
        "recipient_node_id": "node-a",
        "incarnation": "seed-incarnation",
        "generation": generation,
        "issued_at": NOW,
        "expires_at": NOW + 60.0,
        "request_message_id": request["message"]["message_id"],
        "accepted_node_id": "node-a",
        "accepted_incarnation": "incarnation-a",
        "membership_generation": generation,
        "lease_expires_at": NOW + 300.0,
    }
    return sign_membership_message(signer=seed_signer, message=message)


def _joined(tmp_path: Path):
    session = _session(tmp_path)
    seed = generate_ed25519_signer(endpoint_id="seed-endpoint")
    request = session.join_request(
        invite_nonce="invite-1",
        endpoint_addrs=["https://100.117.33.124:9443/control"],
    )
    session.accept_join(
        _acceptance(session, request, seed),
        seed_key_digest=seed.verification_key_digest,
    )
    return session, seed, request


def test_join_request_self_presents_durable_key_and_accepts_pinned_seed(tmp_path: Path) -> None:
    session = _session(tmp_path)
    seed = generate_ed25519_signer(endpoint_id="seed-endpoint")
    request = session.join_request(
        invite_nonce="invite-1",
        endpoint_addrs=["https://100.117.33.124:9443/control"],
    )

    verified = verify_join_request(request, now=NOW)
    assert verified["sender_node_id"] == "node-a"
    assert verified["sender_endpoint_id"] == session.signer.endpoint_id
    assert verified["endpoint_addr"] == {
        "id": session.signer.endpoint_id,
        "addrs": ["https://100.117.33.124:9443/control"],
    }
    assert verified["generation"] == 0

    accepted = session.accept_join(
        _acceptance(session, request, seed),
        seed_key_digest=seed.verification_key_digest,
    )
    assert accepted["membership_generation"] == 1
    assert session.joined is True
    assert session.generation == 1
    assert session.seed_endpoint_id == "seed-endpoint"


def test_join_acceptance_is_bound_to_request_seed_and_incarnation(tmp_path: Path) -> None:
    session = _session(tmp_path)
    seed = generate_ed25519_signer(endpoint_id="seed-endpoint")
    wrong_seed = generate_ed25519_signer(endpoint_id="wrong-seed-endpoint")
    request = session.join_request(invite_nonce="invite-1", endpoint_addrs=["https://node"])

    with pytest.raises(MembershipContractError) as wrong_pin:
        session.accept_join(
            _acceptance(session, request, wrong_seed),
            seed_key_digest=seed.verification_key_digest,
        )
    assert wrong_pin.value.code == "membership_key_pin_mismatch"
    assert session.joined is False

    envelope = _acceptance(session, request, seed)
    envelope["message"]["request_message_id"] = "different-request"
    envelope = sign_membership_message(signer=seed, message=envelope["message"])
    with pytest.raises(NodeMembershipError) as mismatch:
        session.accept_join(envelope, seed_key_digest=seed.verification_key_digest)
    assert mismatch.value.code == "join_acceptance_mismatch"


def test_acceptance_and_assignment_offer_replays_fail_closed(tmp_path: Path) -> None:
    session = _session(tmp_path)
    seed = generate_ed25519_signer(endpoint_id="seed-endpoint")
    request = session.join_request(invite_nonce="invite-1", endpoint_addrs=["https://node"])
    acceptance = _acceptance(session, request, seed)
    session.accept_join(acceptance, seed_key_digest=seed.verification_key_digest)
    with pytest.raises(NodeMembershipError) as acceptance_replay:
        session.accept_join(acceptance, seed_key_digest=seed.verification_key_digest)
    assert acceptance_replay.value.code == "membership_message_replayed"

    offer_message = {
        "protocol": ASSIGNMENT_OFFER_PROTOCOL,
        "message_id": "offer-1",
        "swarm_id": "swarm-a",
        "sender_node_id": "seed-node",
        "sender_endpoint_id": seed.endpoint_id,
        "recipient_node_id": "node-a",
        "incarnation": "seed-incarnation",
        "generation": 1,
        "issued_at": NOW,
        "expires_at": NOW + 60.0,
        "deployment_id": "deployment-1",
        "deployment_epoch": 1,
        "assignment_id": "assignment-1",
        "assignment_digest": "sha256:" + "1" * 64,
        "stage_pack_digest": "sha256:" + "2" * 64,
        "graph_digest": "sha256:" + "3" * 64,
        "load_generation": 1,
        "placement_provenance": "offline_capacity_planner",
        "peer_endpoint_records": [],
    }
    offer = sign_membership_message(signer=seed, message=offer_message)
    assert session.accept_assignment_offer(offer)["assignment_id"] == "assignment-1"
    with pytest.raises(NodeMembershipError) as replay:
        session.accept_assignment_offer(offer)
    assert replay.value.code == "membership_message_replayed"

    stale_message = {
        **offer_message,
        "message_id": "offer-2",
        "assignment_id": "assignment-2",
    }
    stale_offer = sign_membership_message(signer=seed, message=stale_message)
    with pytest.raises(NodeMembershipError) as stale:
        session.accept_assignment_offer(stale_offer)
    assert stale.value.code == "assignment_epoch_stale"


def test_unexpired_replay_ids_fail_closed_at_capacity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session, seed, _request = _joined(tmp_path)
    monkeypatch.setattr(node_membership_module, "_MAX_REPLAY_IDS", 2)

    base = {
        "protocol": ASSIGNMENT_OFFER_PROTOCOL,
        "swarm_id": "swarm-a",
        "sender_node_id": "seed-node",
        "sender_endpoint_id": seed.endpoint_id,
        "recipient_node_id": "node-a",
        "incarnation": "seed-incarnation",
        "generation": 1,
        "issued_at": NOW,
        "expires_at": NOW + 60.0,
        "deployment_id": "deployment-1",
        "assignment_digest": "sha256:" + "1" * 64,
        "stage_pack_digest": "sha256:" + "2" * 64,
        "graph_digest": "sha256:" + "3" * 64,
        "load_generation": 1,
        "placement_provenance": "offline_capacity_planner",
        "peer_endpoint_records": [],
    }
    first = sign_membership_message(
        signer=seed,
        message={
            **base,
            "message_id": "offer-window-1",
            "deployment_epoch": 1,
            "assignment_id": "assignment-window-1",
        },
    )
    second = sign_membership_message(
        signer=seed,
        message={
            **base,
            "message_id": "offer-window-2",
            "deployment_epoch": 2,
            "assignment_id": "assignment-window-2",
        },
    )

    session.accept_assignment_offer(first)
    with pytest.raises(NodeMembershipError) as full:
        session.accept_assignment_offer(second)
    assert full.value.code == "membership_replay_window_full"
    with pytest.raises(NodeMembershipError) as replay:
        session.accept_assignment_offer(first)
    assert replay.value.code == "membership_message_replayed"


def test_assignment_result_is_signed_and_bound_to_verified_offer(tmp_path: Path) -> None:
    session, seed, _request = _joined(tmp_path)
    offer = sign_membership_message(
        signer=seed,
        message={
            "protocol": ASSIGNMENT_OFFER_PROTOCOL,
            "message_id": "offer-result-1",
            "swarm_id": "swarm-a",
            "sender_node_id": "seed-node",
            "sender_endpoint_id": seed.endpoint_id,
            "recipient_node_id": "node-a",
            "incarnation": "seed-incarnation",
            "generation": 1,
            "issued_at": NOW,
            "expires_at": NOW + 60.0,
            "deployment_id": "deployment-result",
            "deployment_epoch": 7,
            "assignment_id": "assignment-result",
            "assignment_digest": "sha256:" + "1" * 64,
            "stage_pack_digest": "sha256:" + "2" * 64,
            "graph_digest": "sha256:" + "3" * 64,
            "load_generation": 4,
            "placement_provenance": "offline_capacity_planner",
            "peer_endpoint_records": [],
        },
    )
    session.accept_assignment_offer(offer)

    result = session.assignment_result(
        assignment_id="assignment-result",
        accepted=True,
        result_code="loaded",
        load_proof_digest="sha256:" + "4" * 64,
        runtime_endpoint="iroh://runtime-endpoint",
    )
    verified = verify_membership_message(
        result,
        now=NOW,
        expected_key_digest=session.signer.verification_key_digest,
        expected_protocol=ASSIGNMENT_RESULT_PROTOCOL,
    )
    assert verified["deployment_id"] == "deployment-result"
    assert verified["deployment_epoch"] == 7
    assert verified["assignment_id"] == "assignment-result"
    assert verified["recipient_node_id"] == "seed-node"

    with pytest.raises(NodeMembershipError) as unknown:
        session.assignment_result(
            assignment_id="unknown-assignment",
            accepted=False,
            result_code="rejected",
            load_proof_digest=None,
            runtime_endpoint=None,
        )
    assert unknown.value.code == "assignment_offer_unknown"


def test_link_probe_and_drain_ack_roundtrip_with_strict_recipients(tmp_path: Path) -> None:
    session, _seed, _request = _joined(tmp_path)
    first_probe = session.link_probe_report(
        target_node_id="node-b",
        target_endpoint_id="endpoint-b",
        reachable=True,
        rtt_ms=12.5,
        goodput_bytes_per_second=4096.0,
    )
    second_probe = session.link_probe_report(
        target_node_id="node-b",
        target_endpoint_id="endpoint-b",
        reachable=False,
        rtt_ms=0.0,
        goodput_bytes_per_second=0.0,
    )
    first_message = verify_membership_message(
        first_probe,
        now=NOW,
        expected_key_digest=session.signer.verification_key_digest,
        expected_protocol=LINK_PROBE_REPORT_PROTOCOL,
    )
    second_message = verify_membership_message(
        second_probe,
        now=NOW,
        expected_key_digest=session.signer.verification_key_digest,
        expected_protocol=LINK_PROBE_REPORT_PROTOCOL,
    )
    assert first_message["recipient_node_id"] == "node-b"
    assert first_message["target_node_id"] == "node-b"
    assert (first_message["probe_sequence"], second_message["probe_sequence"]) == (1, 2)

    acknowledgement = session.drain_acknowledgement(
        drain_id="drain-1",
        last_request_id=None,
        completed_at=NOW,
    )
    ack_message = verify_membership_message(
        acknowledgement,
        now=NOW,
        expected_key_digest=session.signer.verification_key_digest,
        expected_protocol=DRAIN_ACK_PROTOCOL,
    )
    assert ack_message["recipient_node_id"] == "seed-node"
    assert ack_message["active_requests"] == 0
    assert ack_message["completed_at"] == NOW


def test_capability_and_heartbeat_are_signed_generation_bound_and_never_ready(
    tmp_path: Path,
) -> None:
    session, _seed, _request = _joined(tmp_path)
    capability = session.capability_report(
        platform="macOS-15",
        architecture="arm64",
        memory_bytes=8 * 1024**3,
        available_storage_bytes=100 * 1024**3,
        backends=["mlx"],
        precisions=["float16"],
    )
    verified_capability = verify_membership_message(
        capability,
        now=NOW,
        expected_key_digest=session.signer.verification_key_digest,
        expected_protocol=CAPABILITY_REPORT_PROTOCOL,
    )
    assert verified_capability["generation"] == 1
    assert verified_capability["recipient_node_id"] == "seed-node"

    first = session.heartbeat(lifecycle_state="CONFIGURED", active_requests=0)
    second = session.heartbeat(lifecycle_state="RUNNING", active_requests=2)
    first_message = verify_membership_message(
        first,
        now=NOW,
        expected_key_digest=session.signer.verification_key_digest,
        expected_protocol=HEARTBEAT_PROTOCOL,
    )
    second_message = verify_membership_message(
        second,
        now=NOW,
        expected_key_digest=session.signer.verification_key_digest,
        expected_protocol=HEARTBEAT_PROTOCOL,
    )
    assert (first_message["heartbeat_sequence"], second_message["heartbeat_sequence"]) == (1, 2)
    assert first_message["route_ready"] is False
    assert second_message["route_ready"] is False


def test_signed_peer_endpoint_records_are_the_only_transport_authority(
    tmp_path: Path,
) -> None:
    session, seed, _request = _joined(tmp_path)
    base = {
        "protocol": ASSIGNMENT_OFFER_PROTOCOL,
        "swarm_id": "swarm-a",
        "sender_node_id": "seed-node",
        "sender_endpoint_id": seed.endpoint_id,
        "recipient_node_id": "node-a",
        "incarnation": "seed-incarnation",
        "generation": 1,
        "issued_at": NOW,
        "expires_at": NOW + 60.0,
        "deployment_id": "deployment-peer-bindings",
        "assignment_digest": "sha256:" + "1" * 64,
        "stage_pack_digest": "sha256:" + "2" * 64,
        "graph_digest": "sha256:" + "3" * 64,
        "load_generation": 1,
        "placement_provenance": "offline_capacity_planner",
    }
    first = sign_membership_message(
        signer=seed,
        message={
            **base,
            "message_id": "offer-peer-1",
            "deployment_epoch": 1,
            "assignment_id": "assignment-peer-1",
            "peer_endpoint_records": [
                {
                    "node_id": "node-b",
                    "endpoint_id": "endpoint-node-b-v1",
                    "deployment_epoch": 1,
                    "membership_generation": 1,
                    "valid_from": NOW,
                    "valid_until": NOW + 60.0,
                }
            ],
        },
    )
    session.accept_assignment_offer(first)

    assert session.resolve_peer_endpoint(
        assignment_id="assignment-peer-1",
        peer_node_id="node-b",
    ) == "endpoint-node-b-v1"
    with pytest.raises(NodeMembershipError, match="assignment_operator_endpoint_forbidden"):
        session.resolve_peer_endpoint(
            assignment_id="assignment-peer-1",
            peer_node_id="node-b",
            operator_expected_endpoint_id="endpoint-node-b-v1",
        )
    with pytest.raises(NodeMembershipError, match="assignment_peer_unauthorized"):
        session.resolve_peer_endpoint(
            assignment_id="assignment-peer-1",
            peer_node_id="node-c",
        )
    with pytest.raises(NodeMembershipError, match="assignment_peer_record_expired"):
        session.resolve_peer_endpoint(
            assignment_id="assignment-peer-1",
            peer_node_id="node-b",
            now=NOW + 60.0,
        )

    revocation = sign_membership_message(
        signer=seed,
        message={
            **base,
            "message_id": "offer-peer-revocation",
            "deployment_epoch": 2,
            "assignment_id": "assignment-peer-revocation",
            "peer_endpoint_records": [],
        },
    )
    session.accept_assignment_offer(revocation)
    with pytest.raises(NodeMembershipError, match="assignment_offer_superseded"):
        session.resolve_peer_endpoint(
            assignment_id="assignment-peer-1",
            peer_node_id="node-b",
        )

    replacement = sign_membership_message(
        signer=seed,
        message={
            **base,
            "message_id": "offer-peer-2",
            "deployment_epoch": 3,
            "assignment_id": "assignment-peer-2",
            "peer_endpoint_records": [
                {
                    "node_id": "node-b",
                    "endpoint_id": "endpoint-node-b-v2",
                    "deployment_epoch": 3,
                    "membership_generation": 2,
                    "valid_from": NOW,
                    "valid_until": NOW + 60.0,
                }
            ],
        },
    )
    session.accept_assignment_offer(replacement)
    assert session.resolve_peer_endpoint(
        assignment_id="assignment-peer-2",
        peer_node_id="node-b",
    ) == "endpoint-node-b-v2"

    stale_generation = sign_membership_message(
        signer=seed,
        message={
            **base,
            "message_id": "offer-peer-stale-generation",
            "deployment_id": "deployment-other",
            "deployment_epoch": 1,
            "assignment_id": "assignment-peer-stale-generation",
            "peer_endpoint_records": [
                {
                    "node_id": "node-b",
                    "endpoint_id": "endpoint-node-b-v1",
                    "deployment_epoch": 1,
                    "membership_generation": 1,
                    "valid_from": NOW,
                    "valid_until": NOW + 60.0,
                }
            ],
        },
    )
    with pytest.raises(NodeMembershipError, match="assignment_peer_generation_stale"):
        session.accept_assignment_offer(stale_generation)
