# SPDX-License-Identifier: AGPL-3.0-only
"""Seed assignment admission for the native peer classes.

An ``android_native_iroh`` member and (owner parity ruling 2026-10-07) a
``windows_native_iroh`` member may receive assignment offers and be named as
peers, and so (the same ruling applied to Linux) may the A13 Linux product
class ``linux_native_iroh``.  Ineligible classes keep the unchanged code.
"""

from __future__ import annotations

from itertools import count
from pathlib import Path

import pytest

from mycelium_invite import SqliteInviteRegistry, verify_invite_bundle
from mycelium_membership import ROUTER_ACTIVATION_PROTOCOL
from mycelium_node import NodeMembershipSession, load_or_create_node_signer
from mycelium_qualification.signing import generate_ed25519_signer
from mycelium_seed import SeedCoordinator, SeedCoordinatorError, SqliteSeedState

NOW = 2_000.0
CAPABILITIES = {
    "mac_mlx_iroh": {
        "runtime_backend": "mlx",
        "transport": "iroh",
        "activation_protocol": ROUTER_ACTIVATION_PROTOCOL,
    },
    "android_native_iroh": {
        "runtime_backend": "mobile-rust-reference",
        "transport": "iroh",
        "activation_protocol": ROUTER_ACTIVATION_PROTOCOL,
    },
    "windows_native_iroh": {
        "runtime_backend": "numpy",
        "transport": "iroh",
        "activation_protocol": ROUTER_ACTIVATION_PROTOCOL,
    },
    "linux_native_iroh": {
        "runtime_backend": "numpy",
        "transport": "iroh",
        "activation_protocol": ROUTER_ACTIVATION_PROTOCOL,
    },
    "linux_numpy_iroh": {
        "runtime_backend": "numpy",
        "transport": "iroh",
        "activation_protocol": ROUTER_ACTIVATION_PROTOCOL,
    },
    "browser_http": {
        "runtime_backend": "browser",
        "transport": "http",
        "activation_protocol": None,
    },
}


def _ids(prefix: str):
    values = count(1)
    return lambda: f"{prefix}-{next(values)}"


def _coordinator(tmp_path: Path) -> SeedCoordinator:
    database = tmp_path / "seed-state" / "state.sqlite3"
    return SeedCoordinator(
        swarm_id="swarm-a",
        seed_node_id="seed-node",
        seed_url="http://127.0.0.1:8788",
        signer=generate_ed25519_signer(endpoint_id="seed-endpoint"),
        invite_registry=SqliteInviteRegistry(database),
        state=SqliteSeedState(database),
        incarnation="seed-incarnation",
        clock=lambda: NOW,
        id_source=_ids("seed-message"),
        lease_seconds=300.0,
    )


def _join(tmp_path: Path, coordinator: SeedCoordinator, peer_class: str) -> str:
    node_id = "node-" + peer_class.replace("_", "-")
    node = NodeMembershipSession(
        node_id=node_id,
        swarm_id="swarm-a",
        seed_node_id="seed-node",
        signer=load_or_create_node_signer(tmp_path / "nodes" / f"{node_id}.key"),
        incarnation=f"incarnation-{node_id}",
        software_version="mycelium-test",
        peer_class=peer_class,
        runtime_capability=CAPABILITIES[peer_class],
        clock=lambda: NOW,
        id_source=_ids(f"{node_id}-message"),
    )
    bundle = coordinator.mint_invite(nonce=f"invite-{node_id}", ttl_seconds=120)
    verified = verify_invite_bundle(bundle, now=NOW)
    request = node.join_request(
        invite_nonce=verified["payload"]["nonce"],
        endpoint_addrs=["https://203.0.113.11:9443/control"],
    )
    acceptance = coordinator.accept_join(invite_token=bundle["token"], join_envelope=request)
    node.accept_join(acceptance, seed_key_digest=verified["seed_key_digest"])
    return node_id


def _offer(coordinator: SeedCoordinator, node_id: str, peers: list[str], tag: str):
    return coordinator.assignment_offer(
        node_id=node_id,
        deployment_id=f"deployment-{tag}",
        deployment_epoch=1,
        assignment_id=f"assignment-{tag}",
        assignment_digest="sha256:" + "1" * 64,
        stage_pack_digest="sha256:" + "2" * 64,
        graph_digest="sha256:" + "3" * 64,
        load_generation=1,
        peer_node_ids=peers,
        placement_provenance="frozen_fixture",
    )


@pytest.fixture()
def swarm(tmp_path: Path):
    coordinator = _coordinator(tmp_path)
    ids = {peer_class: _join(tmp_path, coordinator, peer_class) for peer_class in CAPABILITIES}
    return coordinator, ids


def test_membership_projection_marks_native_eligibility(swarm) -> None:
    coordinator, ids = swarm
    assert coordinator.member(ids["android_native_iroh"])["activation_eligible"] is True
    assert coordinator.member(ids["windows_native_iroh"])["activation_eligible"] is True


def test_android_native_member_and_peer_receive_offers(swarm) -> None:
    coordinator, ids = swarm
    offer = _offer(coordinator, ids["android_native_iroh"], [ids["mac_mlx_iroh"]], "android")
    assert offer["message"]["recipient_node_id"] == ids["android_native_iroh"]
    as_peer = _offer(coordinator, ids["mac_mlx_iroh"], [ids["android_native_iroh"]], "mac")
    assert [record["node_id"] for record in as_peer["message"]["peer_endpoint_records"]] == [
        ids["android_native_iroh"]
    ]


def test_windows_member_and_peer_receive_offers(swarm) -> None:
    # Owner parity ruling 2026-10-07: windows_native_iroh receives activation.
    coordinator, ids = swarm
    offer = _offer(coordinator, ids["windows_native_iroh"], [ids["mac_mlx_iroh"]], "windows")
    assert offer["message"]["recipient_node_id"] == ids["windows_native_iroh"]
    as_peer = _offer(coordinator, ids["mac_mlx_iroh"], [ids["windows_native_iroh"]], "mac-win")
    assert [record["node_id"] for record in as_peer["message"]["peer_endpoint_records"]] == [
        ids["windows_native_iroh"]
    ]


def test_linux_native_member_and_peer_receive_offers(swarm) -> None:
    """Owner parity ruling 2026-10-07 applied to Linux: the product class serves."""

    coordinator, ids = swarm
    assert coordinator.member(ids["linux_native_iroh"])["activation_eligible"] is True
    offer = _offer(coordinator, ids["linux_native_iroh"], [ids["mac_mlx_iroh"]], "linux-native")
    assert offer["message"]["recipient_node_id"] == ids["linux_native_iroh"]
    as_peer = _offer(coordinator, ids["mac_mlx_iroh"], [ids["linux_native_iroh"]], "mac-linux")
    assert [record["node_id"] for record in as_peer["message"]["peer_endpoint_records"]] == [
        ids["linux_native_iroh"]
    ]


def test_linux_numpy_member_still_receives_offers(swarm) -> None:
    """Negative control: the Linux stage-runtime class keeps its activation path."""

    coordinator, ids = swarm
    assert coordinator.member(ids["linux_numpy_iroh"])["activation_eligible"] is True
    offer = _offer(coordinator, ids["linux_numpy_iroh"], [ids["mac_mlx_iroh"]], "numpy")
    assert offer["message"]["recipient_node_id"] == ids["linux_numpy_iroh"]


def test_unnamed_ineligible_class_keeps_the_unchanged_code(swarm) -> None:
    """Negative control: only a class with a named refusal changes its code."""

    coordinator, ids = swarm
    with pytest.raises(SeedCoordinatorError) as refused:
        _offer(coordinator, ids["browser_http"], [], "browser")
    assert refused.value.code == "seed_member_activation_ineligible"
    with pytest.raises(SeedCoordinatorError) as refused_peer:
        _offer(coordinator, ids["mac_mlx_iroh"], [ids["browser_http"]], "mac-browser")
    assert refused_peer.value.code == "seed_peer_activation_ineligible"
