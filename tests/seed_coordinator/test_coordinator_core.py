from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from itertools import count
from pathlib import Path
import threading

import pytest

from mycelium_invite import SqliteInviteRegistry, verify_invite_bundle
from mycelium_membership import (
    ASSIGNMENT_RESULT_PROTOCOL,
    CAPABILITY_REPORT_PROTOCOL,
    HEARTBEAT_PROTOCOL,
)
from mycelium_node import NodeMembershipSession, load_or_create_node_signer
from mycelium_qualification.signing import generate_ed25519_signer
from mycelium_router.transports.iroh import DeliveryReceipt
from mycelium_seed import SeedCoordinator, SeedCoordinatorError, SqliteSeedState


NOW = 2_000.0
MAC_RUNTIME_CAPABILITY = {
    "runtime_backend": "mlx",
    "transport": "iroh",
    "activation_protocol": "mycelium.router_wire.v1",
}
NON_ACTIVATION_RUNTIME_CAPABILITIES = {
    "browser_http": {
        "runtime_backend": "browser",
        "transport": "http",
        "activation_protocol": None,
    },
    "pixel_http": {
        "runtime_backend": "android",
        "transport": "http",
        "activation_protocol": None,
    },
    "linux_tbd": {
        "runtime_backend": "tbd",
        "transport": "none",
        "activation_protocol": None,
    },
}


def _ids(prefix: str):
    values = count(1)
    return lambda: f"{prefix}-{next(values)}"


def _coordinator(
    tmp_path: Path,
    *,
    signer=None,
    id_prefix: str = "seed-message",
) -> SeedCoordinator:
    database = tmp_path / "seed-state" / "state.sqlite3"
    return SeedCoordinator(
        swarm_id="swarm-a",
        seed_node_id="seed-node",
        seed_url="http://127.0.0.1:8788",
        signer=(
            generate_ed25519_signer(endpoint_id="seed-endpoint")
            if signer is None
            else signer
        ),
        invite_registry=SqliteInviteRegistry(database),
        state=SqliteSeedState(database),
        incarnation="seed-incarnation",
        clock=lambda: NOW,
        id_source=_ids(id_prefix),
        lease_seconds=300.0,
    )


def _node(
    tmp_path: Path,
    *,
    node_id: str = "node-a",
    key_name: str = "node-a.key",
    incarnation: str = "incarnation-a",
    peer_class: str = "mac_mlx_iroh",
    runtime_capability=None,
) -> NodeMembershipSession:
    return NodeMembershipSession(
        node_id=node_id,
        swarm_id="swarm-a",
        seed_node_id="seed-node",
        signer=load_or_create_node_signer(tmp_path / "nodes" / key_name),
        incarnation=incarnation,
        software_version="mycelium-test",
        peer_class=peer_class,
        runtime_capability=(
            MAC_RUNTIME_CAPABILITY if runtime_capability is None else runtime_capability
        ),
        clock=lambda: NOW,
        id_source=_ids(f"{node_id}-message"),
    )


def _join(
    coordinator: SeedCoordinator,
    node: NodeMembershipSession,
    *,
    nonce: str,
) -> tuple[dict, dict]:
    bundle = coordinator.mint_invite(nonce=nonce, ttl_seconds=120)
    verified_bundle = verify_invite_bundle(bundle, now=NOW)
    request = node.join_request(
        invite_nonce=verified_bundle["payload"]["nonce"],
        endpoint_addrs=["https://100.117.33.124:9443/control"],
    )
    acceptance = coordinator.accept_join(
        invite_token=bundle["token"],
        join_envelope=request,
    )
    node.accept_join(
        acceptance,
        seed_key_digest=verified_bundle["seed_key_digest"],
    )
    return bundle, request


def test_invite_join_and_signed_telemetry_roundtrip(tmp_path: Path) -> None:
    coordinator = _coordinator(tmp_path)
    node = _node(tmp_path)
    _join(coordinator, node, nonce="invite-a")

    member = coordinator.member("node-a")
    assert member["generation"] == 1
    assert member["incarnation"] == "incarnation-a"
    assert member["verification_key_digest"] == node.signer.verification_key_digest
    assert "private" not in repr(member).lower()

    capability = node.capability_report(
        platform="macOS-15",
        architecture="arm64",
        memory_bytes=8 * 1024**3,
        available_storage_bytes=100 * 1024**3,
        backends=["mlx"],
        precisions=["float16"],
    )
    accepted_capability = coordinator.receive_member_message(
        capability,
        expected_protocol=CAPABILITY_REPORT_PROTOCOL,
    )
    assert accepted_capability["sender_node_id"] == "node-a"

    heartbeat = node.heartbeat(lifecycle_state="CONFIGURED", active_requests=0)
    accepted_heartbeat = coordinator.receive_member_message(
        heartbeat,
        expected_protocol=HEARTBEAT_PROTOCOL,
    )
    assert accepted_heartbeat["route_ready"] is False
    assert coordinator.member("node-a")["last_heartbeat_sequence"] == 1


def test_join_retry_returns_exact_committed_acceptance(tmp_path: Path) -> None:
    coordinator = _coordinator(tmp_path)
    node = _node(tmp_path)
    bundle = coordinator.mint_invite(nonce="invite-idempotent", ttl_seconds=120)
    verified = verify_invite_bundle(bundle, now=NOW)
    request = node.join_request(
        invite_nonce=verified["payload"]["nonce"],
        endpoint_addrs=["https://100.117.33.124:9443/control"],
    )

    first = coordinator.accept_join(
        invite_token=bundle["token"],
        join_envelope=request,
    )
    second = coordinator.accept_join(
        invite_token=bundle["token"],
        join_envelope=request,
    )

    assert second == first
    assert coordinator.member("node-a")["generation"] == 1

    restored = _coordinator(
        tmp_path,
        signer=coordinator.signer,
        id_prefix="restored-idempotent",
    )
    after_restart = restored.accept_join(
        invite_token=bundle["token"],
        join_envelope=request,
    )
    assert after_restart == first
    assert restored.member("node-a")["generation"] == 1


def test_join_precommit_failure_does_not_consume_invite(tmp_path: Path) -> None:
    database = tmp_path / "seed-state" / "state.sqlite3"
    signer = generate_ed25519_signer(endpoint_id="seed-endpoint")

    def fail_id() -> str:
        raise RuntimeError("simulated id-source failure")

    broken = SeedCoordinator(
        swarm_id="swarm-a",
        seed_node_id="seed-node",
        seed_url="http://127.0.0.1:8788",
        signer=signer,
        invite_registry=SqliteInviteRegistry(database),
        state=SqliteSeedState(database),
        incarnation="seed-incarnation",
        clock=lambda: NOW,
        id_source=fail_id,
    )
    node = _node(tmp_path)
    bundle = broken.mint_invite(nonce="invite-retryable", ttl_seconds=120)
    verified = verify_invite_bundle(bundle, now=NOW)
    request = node.join_request(
        invite_nonce=verified["payload"]["nonce"],
        endpoint_addrs=["https://100.117.33.124:9443/control"],
    )

    with pytest.raises(RuntimeError, match="simulated id-source failure"):
        broken.accept_join(invite_token=bundle["token"], join_envelope=request)

    recovered = _coordinator(tmp_path, signer=signer, id_prefix="recovered")
    acceptance = recovered.accept_join(
        invite_token=bundle["token"],
        join_envelope=request,
    )
    assert acceptance["message"]["membership_generation"] == 1


def test_heartbeat_renews_durable_member_lease(tmp_path: Path) -> None:
    clock = [NOW]
    database = tmp_path / "seed-state" / "state.sqlite3"
    signer = generate_ed25519_signer(endpoint_id="seed-endpoint")
    coordinator = SeedCoordinator(
        swarm_id="swarm-a",
        seed_node_id="seed-node",
        seed_url="http://127.0.0.1:8788",
        signer=signer,
        invite_registry=SqliteInviteRegistry(database),
        state=SqliteSeedState(database),
        incarnation="seed-incarnation",
        clock=lambda: clock[0],
        id_source=_ids("seed-renew"),
        lease_seconds=5.0,
    )
    node = NodeMembershipSession(
        node_id="node-a",
        swarm_id="swarm-a",
        seed_node_id="seed-node",
        signer=load_or_create_node_signer(tmp_path / "nodes" / "renew.key"),
        incarnation="incarnation-a",
        software_version="mycelium-test",
        peer_class="mac_mlx_iroh",
        runtime_capability={
            "runtime_backend": "mlx",
            "transport": "iroh",
            "activation_protocol": "mycelium.router_wire.v1",
        },
        clock=lambda: clock[0],
        id_source=_ids("node-renew"),
    )
    bundle = coordinator.mint_invite(nonce="invite-renew", ttl_seconds=120)
    verified = verify_invite_bundle(bundle, now=clock[0])
    request = node.join_request(
        invite_nonce=verified["payload"]["nonce"],
        endpoint_addrs=["https://100.117.33.124:9443/control"],
    )
    acceptance = coordinator.accept_join(
        invite_token=bundle["token"],
        join_envelope=request,
    )
    node.accept_join(acceptance, seed_key_digest=verified["seed_key_digest"])

    clock[0] = NOW + 4.0
    heartbeat = node.heartbeat(lifecycle_state="RUNNING", active_requests=0)
    coordinator.receive_member_message(heartbeat, expected_protocol=HEARTBEAT_PROTOCOL)
    renewal = coordinator.lease_renewal(
        node_id="node-a",
        heartbeat_message_id=heartbeat["message"]["message_id"],
    )
    node.accept_lease_renewal(
        renewal,
        heartbeat_message_id=heartbeat["message"]["message_id"],
    )
    assert coordinator.member("node-a")["lease_expires_at"] == NOW + 9.0

    clock[0] = NOW + 6.0
    capability = node.capability_report(
        platform="macOS-15",
        architecture="arm64",
        memory_bytes=8 * 1024**3,
        available_storage_bytes=100 * 1024**3,
        backends=["mlx"],
        precisions=["float16"],
    )
    coordinator.receive_member_message(
        capability,
        expected_protocol=CAPABILITY_REPORT_PROTOCOL,
    )


def test_assignment_offer_and_result_are_bound_end_to_end(tmp_path: Path) -> None:
    coordinator = _coordinator(tmp_path)
    node = _node(tmp_path)
    _join(coordinator, node, nonce="invite-assignment")

    offer = coordinator.assignment_offer(
        node_id="node-a",
        deployment_id="deployment-1",
        deployment_epoch=3,
        assignment_id="assignment-1",
        assignment_digest="sha256:" + "1" * 64,
        stage_pack_digest="sha256:" + "2" * 64,
        graph_digest="sha256:" + "3" * 64,
        load_generation=4,
        peer_node_ids=[],
        placement_provenance="frozen_fixture",
    )
    assert node.accept_assignment_offer(offer)["assignment_id"] == "assignment-1"
    result = node.assignment_result(
        assignment_id="assignment-1",
        accepted=True,
        result_code="loaded",
        load_proof_digest="sha256:" + "4" * 64,
        runtime_endpoint="iroh://node-a-runtime",
    )
    accepted_result = coordinator.receive_member_message(
        result,
        expected_protocol=ASSIGNMENT_RESULT_PROTOCOL,
    )
    assert accepted_result["deployment_id"] == "deployment-1"
    assert coordinator.assignment_status("assignment-1")["result_code"] == "loaded"

    conflicting_result = node.assignment_result(
        assignment_id="assignment-1",
        accepted=False,
        result_code="late-conflict",
        load_proof_digest=None,
        runtime_endpoint=None,
    )
    with pytest.raises(SeedCoordinatorError) as conflict:
        coordinator.receive_member_message(
            conflicting_result,
            expected_protocol=ASSIGNMENT_RESULT_PROTOCOL,
        )
    assert conflict.value.code == "seed_assignment_result_already_recorded"
    assert coordinator.assignment_status("assignment-1")["result_code"] == "loaded"


def test_invite_join_is_idempotent_under_concurrency(tmp_path: Path) -> None:
    coordinator = _coordinator(tmp_path)
    node = _node(tmp_path)
    bundle = coordinator.mint_invite(nonce="invite-race", ttl_seconds=120)
    verified_bundle = verify_invite_bundle(bundle, now=NOW)
    request = node.join_request(
        invite_nonce=verified_bundle["payload"]["nonce"],
        endpoint_addrs=["https://node-a/control"],
    )
    workers = 8
    barrier = threading.Barrier(workers)

    def attempt() -> dict:
        barrier.wait(timeout=5)
        return coordinator.accept_join(
            invite_token=bundle["token"],
            join_envelope=request,
        )

    with ThreadPoolExecutor(max_workers=workers) as executor:
        outcomes = list(executor.map(lambda _index: attempt(), range(workers)))

    assert all(outcome == outcomes[0] for outcome in outcomes)
    assert coordinator.member("node-a")["generation"] == 1

    intruder = _node(
        tmp_path,
        node_id="node-other",
        key_name="node-other.key",
        incarnation="incarnation-other",
    )
    conflicting_request = intruder.join_request(
        invite_nonce=verified_bundle["payload"]["nonce"],
        endpoint_addrs=["https://node-other/control"],
    )
    with pytest.raises(SeedCoordinatorError, match="seed_join_retry_mismatch"):
        coordinator.accept_join(
            invite_token=bundle["token"],
            join_envelope=conflicting_request,
        )


def test_same_node_rejoin_requires_pinned_key_and_increments_generation(tmp_path: Path) -> None:
    coordinator = _coordinator(tmp_path)
    first = _node(tmp_path, incarnation="incarnation-a")
    _join(coordinator, first, nonce="invite-first")

    wrong_key = _node(
        tmp_path,
        node_id="node-a",
        key_name="wrong-node-a.key",
        incarnation="incarnation-b",
    )
    wrong_bundle = coordinator.mint_invite(nonce="invite-wrong-key", ttl_seconds=120)
    wrong_payload = verify_invite_bundle(wrong_bundle, now=NOW)["payload"]
    wrong_request = wrong_key.join_request(
        invite_nonce=wrong_payload["nonce"],
        endpoint_addrs=["https://wrong-node/control"],
    )
    with pytest.raises(SeedCoordinatorError) as conflict:
        coordinator.accept_join(
            invite_token=wrong_bundle["token"],
            join_envelope=wrong_request,
        )
    assert conflict.value.code == "seed_node_key_conflict"

    restarted = _node(tmp_path, incarnation="incarnation-c")
    _join(coordinator, restarted, nonce="invite-rejoin")
    assert restarted.generation == 2
    assert coordinator.member("node-a")["generation"] == 2


def test_member_generation_heartbeat_replay_and_assignments_survive_restart(
    tmp_path: Path,
) -> None:
    signer = generate_ed25519_signer(endpoint_id="seed-endpoint")
    first_seed = _coordinator(tmp_path, signer=signer, id_prefix="seed-first")
    node = _node(tmp_path)
    _join(first_seed, node, nonce="invite-before-restart")
    heartbeat = node.heartbeat(lifecycle_state="NEW", active_requests=0)
    first_seed.receive_member_message(
        heartbeat,
        expected_protocol=HEARTBEAT_PROTOCOL,
    )
    first_seed.assignment_offer(
        node_id="node-a",
        deployment_id="deployment-persisted",
        deployment_epoch=7,
        assignment_id="assignment-persisted",
        assignment_digest="sha256:" + "a" * 64,
        stage_pack_digest="sha256:" + "b" * 64,
        graph_digest="sha256:" + "c" * 64,
        load_generation=2,
        peer_node_ids=[],
        placement_provenance="frozen_fixture",
    )

    restarted_seed = _coordinator(
        tmp_path,
        signer=signer,
        id_prefix="seed-restarted",
    )
    restored = restarted_seed.member("node-a")
    assert restored["generation"] == 1
    assert restored["last_heartbeat_sequence"] == 1
    assert restarted_seed.assignment_status("assignment-persisted")[
        "deployment_epoch"
    ] == 7
    with pytest.raises(SeedCoordinatorError) as replay:
        restarted_seed.receive_member_message(
            heartbeat,
            expected_protocol=HEARTBEAT_PROTOCOL,
        )
    assert replay.value.code == "seed_message_replayed"

    restarted_node = _node(tmp_path, incarnation="incarnation-after-restart")
    _join(restarted_seed, restarted_node, nonce="invite-after-restart")
    assert restarted_node.generation == 2
    assert restarted_seed.member("node-a")["generation"] == 2


def test_stale_seed_process_cannot_regress_persisted_heartbeat(tmp_path: Path) -> None:
    signer = generate_ed25519_signer(endpoint_id="seed-endpoint")
    stale_seed = _coordinator(tmp_path, signer=signer, id_prefix="seed-stale")
    node = _node(tmp_path)
    _join(stale_seed, node, nonce="invite-concurrent-seeds")
    current_seed = _coordinator(
        tmp_path,
        signer=signer,
        id_prefix="seed-current",
    )

    heartbeat_one = node.heartbeat(lifecycle_state="NEW", active_requests=0)
    heartbeat_two = node.heartbeat(lifecycle_state="NEW", active_requests=0)
    current_seed.receive_member_message(
        heartbeat_two,
        expected_protocol=HEARTBEAT_PROTOCOL,
    )
    with pytest.raises(SeedCoordinatorError) as stale:
        stale_seed.receive_member_message(
            heartbeat_one,
            expected_protocol=HEARTBEAT_PROTOCOL,
        )
    assert stale.value.code == "seed_state_member_conflict"

    restored = _coordinator(
        tmp_path,
        signer=signer,
        id_prefix="seed-restored",
    )
    assert restored.member("node-a")["last_heartbeat_sequence"] == 2


def test_stale_seed_rejects_old_generation_messages_and_assignments(
    tmp_path: Path,
) -> None:
    signer = generate_ed25519_signer(endpoint_id="seed-endpoint")
    stale_seed = _coordinator(tmp_path, signer=signer, id_prefix="seed-stale")
    old_node = _node(tmp_path, incarnation="old-incarnation")
    _join(stale_seed, old_node, nonce="invite-generation-one")
    current_seed = _coordinator(
        tmp_path,
        signer=signer,
        id_prefix="seed-current",
    )
    new_node = _node(tmp_path, incarnation="new-incarnation")
    _join(current_seed, new_node, nonce="invite-generation-two")

    stale_capability = old_node.capability_report(
        platform="macOS-15",
        architecture="arm64",
        memory_bytes=8 * 1024**3,
        available_storage_bytes=80 * 1024**3,
        backends=["mlx"],
        precisions=["float16"],
    )
    with pytest.raises(SeedCoordinatorError) as stale_message:
        stale_seed.receive_member_message(
            stale_capability,
            expected_protocol=CAPABILITY_REPORT_PROTOCOL,
        )
    assert stale_message.value.code == "seed_state_member_stale"

    with pytest.raises(SeedCoordinatorError) as stale_assignment:
        stale_seed.assignment_offer(
            node_id="node-a",
            deployment_id="deployment-stale",
            deployment_epoch=1,
            assignment_id="assignment-stale",
            assignment_digest="sha256:" + "1" * 64,
            stage_pack_digest="sha256:" + "2" * 64,
            graph_digest="sha256:" + "3" * 64,
            load_generation=1,
            peer_node_ids=[],
            placement_provenance="frozen_fixture",
        )
    assert stale_assignment.value.code == "seed_state_member_stale"


def test_join_rejects_one_endpoint_identity_under_multiple_node_ids(
    tmp_path: Path,
) -> None:
    coordinator = _coordinator(tmp_path)
    node_a = _node(tmp_path)
    _join(coordinator, node_a, nonce="invite-identity-a")
    node_b = NodeMembershipSession(
        node_id="node-b",
        swarm_id="swarm-a",
        seed_node_id="seed-node",
        signer=node_a.signer,
        incarnation="incarnation-b",
        software_version="0.1.0",
        peer_class="mac_mlx_iroh",
        runtime_capability=MAC_RUNTIME_CAPABILITY,
        clock=lambda: NOW,
        id_source=_ids("node-b-message"),
    )

    with pytest.raises(SeedCoordinatorError, match="seed_member_identity_reused"):
        _join(coordinator, node_b, nonce="invite-identity-b")


@pytest.mark.parametrize(
    ("peer_class", "runtime_capability", "activation_eligible"),
    [
        ("mac_mlx_iroh", MAC_RUNTIME_CAPABILITY, True),
        *[
            (peer_class, capability, False)
            for peer_class, capability in NON_ACTIVATION_RUNTIME_CAPABILITIES.items()
        ],
    ],
)
def test_join_persists_peer_class_and_runtime_capability_across_restart(
    tmp_path: Path,
    peer_class: str,
    runtime_capability: dict,
    activation_eligible: bool,
) -> None:
    coordinator = _coordinator(tmp_path)
    node = _node(
        tmp_path,
        peer_class=peer_class,
        runtime_capability=runtime_capability,
    )
    _join(coordinator, node, nonce=f"invite-{peer_class}")

    restored = _coordinator(tmp_path, signer=coordinator.signer, id_prefix="restored-class")
    member = restored.member("node-a")
    assert member["peer_class"] == peer_class
    assert member["runtime_capability"] == runtime_capability
    assert member["activation_eligible"] is activation_eligible


def test_assignment_offer_binds_eligible_peer_endpoints_and_provenance(
    tmp_path: Path,
) -> None:
    coordinator = _coordinator(tmp_path)
    node_a = _node(tmp_path)
    node_b = _node(
        tmp_path,
        node_id="node-b",
        key_name="node-b.key",
        incarnation="incarnation-b",
    )
    browser = _node(
        tmp_path,
        node_id="node-browser",
        key_name="node-browser.key",
        incarnation="incarnation-browser",
        peer_class="browser_http",
        runtime_capability=NON_ACTIVATION_RUNTIME_CAPABILITIES["browser_http"],
    )
    _join(coordinator, node_a, nonce="invite-node-a")
    _join(coordinator, node_b, nonce="invite-node-b")
    _join(coordinator, browser, nonce="invite-browser")

    offer = coordinator.assignment_offer(
        node_id="node-a",
        deployment_id="deployment-contract",
        deployment_epoch=7,
        assignment_id="assignment-contract",
        assignment_digest="sha256:" + "1" * 64,
        stage_pack_digest="sha256:" + "2" * 64,
        graph_digest="sha256:" + "3" * 64,
        load_generation=1,
        peer_node_ids=["node-b"],
        placement_provenance="frozen_fixture",
    )
    message = offer["message"]
    assert message["placement_provenance"] == "frozen_fixture"
    assert message["peer_endpoint_records"] == [
        {
            "node_id": "node-b",
            "endpoint_id": node_b.signer.endpoint_id,
            "deployment_epoch": 7,
            "membership_generation": 1,
            "valid_from": NOW,
            "valid_until": NOW + 60.0,
        }
    ]

    with pytest.raises(SeedCoordinatorError, match="seed_member_activation_ineligible"):
        coordinator.assignment_offer(
            node_id="node-browser",
            deployment_id="deployment-browser",
            deployment_epoch=1,
            assignment_id="assignment-browser",
            assignment_digest="sha256:" + "1" * 64,
            stage_pack_digest="sha256:" + "2" * 64,
            graph_digest="sha256:" + "3" * 64,
            load_generation=1,
            peer_node_ids=[],
            placement_provenance="frozen_fixture",
        )
    with pytest.raises(SeedCoordinatorError, match="seed_peer_activation_ineligible"):
        coordinator.assignment_offer(
            node_id="node-a",
            deployment_id="deployment-browser-peer",
            deployment_epoch=1,
            assignment_id="assignment-browser-peer",
            assignment_digest="sha256:" + "1" * 64,
            stage_pack_digest="sha256:" + "2" * 64,
            graph_digest="sha256:" + "3" * 64,
            load_generation=1,
            peer_node_ids=["node-browser"],
            placement_provenance="frozen_fixture",
        )



def test_idle_staleness_and_active_decode_failure_are_distinct_and_fail_closed(
    tmp_path: Path,
) -> None:
    clock = [NOW]
    database = tmp_path / "liveness" / "state.sqlite3"
    seed_signer = generate_ed25519_signer(endpoint_id="seed-endpoint")
    coordinator = SeedCoordinator(
        swarm_id="swarm-a",
        seed_node_id="seed-node",
        seed_url="http://127.0.0.1:8788",
        signer=seed_signer,
        invite_registry=SqliteInviteRegistry(database),
        state=SqliteSeedState(database),
        incarnation="seed-incarnation",
        clock=lambda: clock[0],
        id_source=_ids("seed-liveness"),
        lease_seconds=120.0,
        keepalive_interval_seconds=10.0,
        evidence_freshness_seconds=25.0,
    )
    node = NodeMembershipSession(
        node_id="node-a",
        swarm_id="swarm-a",
        seed_node_id="seed-node",
        signer=load_or_create_node_signer(tmp_path / "liveness" / "node.key"),
        incarnation="incarnation-a",
        software_version="mycelium-test",
        peer_class="mac_mlx_iroh",
        runtime_capability=MAC_RUNTIME_CAPABILITY,
        clock=lambda: clock[0],
        id_source=_ids("node-liveness"),
    )
    bundle = coordinator.mint_invite(nonce="invite-liveness", ttl_seconds=120)
    verified = verify_invite_bundle(bundle, now=clock[0])
    request = node.join_request(
        invite_nonce=verified["payload"]["nonce"],
        endpoint_addrs=["https://node-a/control"],
    )
    acceptance = coordinator.accept_join(
        invite_token=bundle["token"],
        join_envelope=request,
    )
    node.accept_join(acceptance, seed_key_digest=verified["seed_key_digest"])

    clock[0] = NOW + 11.0
    one_miss = coordinator.member("node-a")
    assert one_miss["liveness_status"] == "liveness_stale"
    assert one_miss["liveness_dead"] is False

    clock[0] = NOW + 26.0
    corroborated = coordinator.member("node-a")
    assert corroborated["liveness_status"] == "dead"
    assert corroborated["liveness_dead"] is True
    assert corroborated["liveness_reason"] == "idle_keepalive_and_evidence_expired"
    with pytest.raises(SeedCoordinatorError, match="seed_member_liveness_dead"):
        coordinator.assignment_offer(
            node_id="node-a",
            deployment_id="deployment-dead",
            deployment_epoch=1,
            assignment_id="assignment-dead",
            assignment_digest="sha256:" + "1" * 64,
            stage_pack_digest="sha256:" + "2" * 64,
            graph_digest="sha256:" + "3" * 64,
            load_generation=1,
            peer_node_ids=[],
            placement_provenance="frozen_fixture",
        )

    active = node.heartbeat(
        lifecycle_state="RUNNING",
        active_requests=1,
        force=True,
    )
    assert active is not None
    coordinator.receive_member_message(active, expected_protocol=HEARTBEAT_PROTOCOL)
    clock[0] = NOW + 37.0
    in_flight = coordinator.member("node-a")
    assert in_flight["liveness_status"] == "active_decode_transport_failure"
    assert in_flight["liveness_dead"] is False
    assert in_flight["active_requests"] == 1
    with pytest.raises(SeedCoordinatorError, match="seed_member_liveness_stale"):
        coordinator.assignment_offer(
            node_id="node-a",
            deployment_id="deployment-in-flight",
            deployment_epoch=1,
            assignment_id="assignment-in-flight",
            assignment_digest="sha256:" + "1" * 64,
            stage_pack_digest="sha256:" + "2" * 64,
            graph_digest="sha256:" + "3" * 64,
            load_generation=1,
            peer_node_ids=[],
            placement_provenance="frozen_fixture",
        )

    restarted = SeedCoordinator(
        swarm_id="swarm-a",
        seed_node_id="seed-node",
        seed_url="http://127.0.0.1:8788",
        signer=seed_signer,
        invite_registry=SqliteInviteRegistry(database),
        state=SqliteSeedState(database),
        incarnation="seed-incarnation",
        clock=lambda: clock[0],
        id_source=_ids("seed-liveness-restart"),
        lease_seconds=120.0,
        keepalive_interval_seconds=10.0,
        evidence_freshness_seconds=25.0,
    )
    restored = restarted.member("node-a")
    assert restored["liveness_status"] == "active_decode_transport_failure"
    assert restored["active_requests"] == 1
    assert restored["last_liveness_at"] == NOW + 26.0



def test_activation_receipt_renews_liveness_and_stale_peer_is_not_routed(
    tmp_path: Path,
) -> None:
    coordinator = _coordinator(tmp_path, id_prefix="seed-receipt")
    node_a = _node(tmp_path, node_id="node-a", key_name="node-a-receipt.key")
    node_b = _node(
        tmp_path,
        node_id="node-b",
        key_name="node-b-receipt.key",
        incarnation="incarnation-b",
    )
    _join(coordinator, node_a, nonce="invite-receipt-a")
    _join(coordinator, node_b, nonce="invite-receipt-b")
    offer = coordinator.assignment_offer(
        node_id="node-a",
        deployment_id="deployment-receipt",
        deployment_epoch=1,
        assignment_id="assignment-receipt",
        assignment_digest="sha256:" + "1" * 64,
        stage_pack_digest="sha256:" + "2" * 64,
        graph_digest="sha256:" + "3" * 64,
        load_generation=1,
        peer_node_ids=["node-b"],
        placement_provenance="frozen_fixture",
    )
    node_a.accept_assignment_offer(offer)
    receipt = DeliveryReceipt(
        message_id=b"a" * 32,
        peer_endpoint_id=node_b.signer.endpoint_id,
        peer_generation=1,
    )
    activity = node_a.activation_receipt(
        assignment_id="assignment-receipt",
        peer_node_id="node-b",
        receipt=receipt,
        lifecycle_state="RUNNING",
        active_requests=1,
    )
    activity_message = activity["message"]
    accepted = coordinator.receive_member_message(
        activity,
        expected_protocol=HEARTBEAT_PROTOCOL,
    )
    renewal = coordinator.lease_renewal(
        node_id="node-a",
        heartbeat_message_id=activity_message["message_id"],
    )
    assert accepted["liveness_source"] == "activation_receipt"
    assert coordinator.member("node-a")["last_activity_receipt_at"] == NOW
    node_a.accept_lease_renewal(
        renewal,
        heartbeat_message_id=activity_message["message_id"],
    )
    assert node_a.heartbeat(lifecycle_state="RUNNING", active_requests=0) is None

    coordinator._clock = lambda: NOW + 31.0
    suppressed_grace = coordinator.member("node-a")
    assert suppressed_grace["liveness_status"] == "active_fresh"
    assert suppressed_grace["missed_keepalives"] == 0
    target_heartbeat = node_a.heartbeat(
        lifecycle_state="RUNNING",
        active_requests=0,
        force=True,
    )
    assert target_heartbeat is not None
    coordinator.receive_member_message(
        target_heartbeat,
        expected_protocol=HEARTBEAT_PROTOCOL,
    )
    with pytest.raises(SeedCoordinatorError, match="seed_peer_liveness_stale"):
        coordinator.assignment_offer(
            node_id="node-a",
            deployment_id="deployment-stale-peer",
            deployment_epoch=2,
            assignment_id="assignment-stale-peer",
            assignment_digest="sha256:" + "4" * 64,
            stage_pack_digest="sha256:" + "5" * 64,
            graph_digest="sha256:" + "6" * 64,
            load_generation=2,
            peer_node_ids=["node-b"],
            placement_provenance="frozen_fixture",
        )
