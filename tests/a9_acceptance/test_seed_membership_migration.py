"""Seed -> executor -> seed: the A9 migration over a real seed database.

Two members join a real ``SeedCoordinator`` (macOS mlx and Linux numpy legacy
classes).  The seed rows are exported into the private executor baseline, each
member signs its v2 profile with its own durable node key, the migration
commits generation n+1 and records the cutover, and the same seed then refuses
every v1 write with ``membership_contract_upgrade_required``.
"""

from __future__ import annotations

from itertools import count
from pathlib import Path
import shutil
import sqlite3

import pytest

from mycelium_invite import SqliteInviteRegistry, verify_invite_bundle
from mycelium_membership import HEARTBEAT_PROTOCOL
from mycelium_node import NodeMembershipSession, load_or_create_node_signer
from mycelium_platform_neutral import migration
from mycelium_platform_neutral.migration_executor import (
    FileMembershipState,
    MigrationError,
    MigrationRequest,
)
from mycelium_platform_neutral.seed_migration import (
    SeedMembershipCutover,
    build_migration_profile,
    export_seed_baseline,
    observe_host_platform,
    profile_requests,
    sign_migration_profile,
)
from mycelium_qualification.signing import generate_ed25519_signer
from mycelium_seed import SeedCoordinator, SeedCoordinatorError, SqliteSeedState

NOW = 2_000.0
BUILD = "sha256:" + "b" * 64
CONTRACT = "sha256:" + "c" * 64
INVENTORY = "sha256:" + "d" * 64
CAPABILITIES = {
    "mac_mlx_iroh": {"runtime_backend": "mlx", "transport": "iroh", "activation_protocol": "mycelium.router_wire.v1"},
    "linux_numpy_iroh": {"runtime_backend": "numpy", "transport": "iroh", "activation_protocol": "mycelium.router_wire.v1"},
}
HOSTS = {"node-mac": ("macos", "arm64"), "node-linux": ("linux", "x86_64")}


def _ids(prefix):
    values = count(1)
    return lambda: f"{prefix}-{next(values)}"


def _coordinator(database: Path, signer, *, cutover=None, prefix="seed") -> SeedCoordinator:
    return SeedCoordinator(
        swarm_id="swarm-a",
        seed_node_id="seed-node",
        seed_url="http://127.0.0.1:8788",
        signer=signer,
        invite_registry=SqliteInviteRegistry(database),
        state=SqliteSeedState(database),
        incarnation="seed-incarnation",
        clock=lambda: NOW,
        id_source=_ids(prefix),
        lease_seconds=300.0,
        membership_cutover=cutover,
    )


def _node(tmp_path, node_id, peer_class, incarnation="incarnation-a"):
    return NodeMembershipSession(
        node_id=node_id,
        swarm_id="swarm-a",
        seed_node_id="seed-node",
        signer=load_or_create_node_signer(tmp_path / "nodes" / f"{node_id}.key"),
        incarnation=incarnation,
        software_version="mycelium-test",
        peer_class=peer_class,
        runtime_capability=CAPABILITIES[peer_class],
        clock=lambda: NOW,
        id_source=_ids(f"{node_id}-message"),
    )


def _join(coordinator, node, nonce):
    bundle = coordinator.mint_invite(nonce=nonce, ttl_seconds=120)
    verified = verify_invite_bundle(bundle, now=NOW)
    request = node.join_request(
        invite_nonce=verified["payload"]["nonce"],
        endpoint_addrs=["https://100.117.33.124:9443/control"],
    )
    node.accept_join(
        coordinator.accept_join(invite_token=bundle["token"], join_envelope=request),
        seed_key_digest=verified["seed_key_digest"],
    )


@pytest.fixture()
def swarm(tmp_path):
    database = tmp_path / "seed" / "state.sqlite3"
    signer = generate_ed25519_signer(endpoint_id="seed-endpoint")
    coordinator = _coordinator(database, signer)
    nodes = {
        "node-mac": _node(tmp_path, "node-mac", "mac_mlx_iroh"),
        "node-linux": _node(tmp_path, "node-linux", "linux_numpy_iroh"),
    }
    for index, node in enumerate(nodes.values()):
        _join(coordinator, node, f"invite-{index}")
    copy = tmp_path / "gate" / "seed-copy.sqlite3"
    copy.parent.mkdir()
    copy.parent.chmod(0o700)
    shutil.copy2(database, copy)
    return database, signer, coordinator, nodes, copy


def _migrate(tmp_path, copy):
    state = SqliteSeedState(copy)
    baseline = export_seed_baseline(state, software_build_digest=BUILD)
    store = FileMembershipState(tmp_path / "gate" / "private")
    store.initialize(baseline)
    envelopes = []
    for request in profile_requests(
        state, swarm_id="swarm-a", software_build_digest=BUILD, software_version="1.0"
    ):
        node = request["node_id"]
        signer = load_or_create_node_signer(tmp_path / "nodes" / f"{node}.key")
        envelopes.append(
            sign_migration_profile(
                request,
                signer=signer,
                new_incarnation="incarnation-v2",
                issued_at=NOW,
                observed_platform=HOSTS[node],
            )
        )
    request = MigrationRequest(migration_id="a9-migration", profiles=tuple(envelopes))
    assert migration.execute_identity_preserving_migration(storage=store, request=request, now=NOW) == "committed"
    return store, baseline, envelopes


def test_seed_export_is_the_executor_baseline(swarm):
    _, _, coordinator, nodes, copy = swarm
    baseline = export_seed_baseline(SqliteSeedState(copy), software_build_digest=BUILD)
    assert set(baseline) == {"node-mac", "node-linux"}
    for node_id, record in baseline.items():
        assert record["generation"] == 1
        assert record["revoked"] is False
        assert record["verification_key_digest"] == coordinator.member(node_id)["verification_key_digest"]
        assert record["lease_expires_at"] == NOW + 300.0


def test_profile_requests_decompose_legacy_classes_without_raw_endpoint_ids(swarm):
    _, _, coordinator, _, copy = swarm
    requests = {item["node_id"]: item for item in profile_requests(
        SqliteSeedState(copy), swarm_id="swarm-a", software_build_digest=BUILD, software_version="1.0"
    )}
    assert requests["node-mac"]["membership_generation"] == 2
    assert [runtime["backend_id"] for runtime in requests["node-mac"]["runtimes"]] == ["mlx"]
    assert [runtime["backend_id"] for runtime in requests["node-linux"]["runtimes"]] == ["numpy"]
    endpoint = coordinator.member("node-mac")["endpoint_id"]
    assert endpoint not in repr(requests)


def test_member_platform_comes_from_the_host_not_the_label(swarm):
    _, _, _, _, copy = swarm
    request = next(item for item in profile_requests(
        SqliteSeedState(copy), swarm_id="swarm-a", software_build_digest=BUILD, software_version="1.0"
    ) if item["node_id"] == "node-mac")
    with pytest.raises(ValueError, match="platform_observation_conflicts_legacy_class"):
        build_migration_profile(request, new_incarnation="i2", issued_at=NOW, observed_platform=("linux", "x86_64"))
    with pytest.raises(ValueError, match="incarnation_not_renewed"):
        build_migration_profile(request, new_incarnation="incarnation-a", issued_at=NOW, observed_platform=("macos", "arm64"))
    with pytest.raises(ValueError, match="legacy_generation_stale"):
        build_migration_profile(request, new_incarnation="i2", issued_at=NOW + 301, observed_platform=("macos", "arm64"))
    profile = build_migration_profile(request, new_incarnation="i2", issued_at=NOW, observed_platform=("macos", "arm64"))
    assert (profile.platform_family, profile.architecture) == ("macos", "arm64")
    assert observe_host_platform(system="Windows", machine="AMD64") == ("windows", "x86_64")
    assert observe_host_platform(system="Plan9", machine="mips") == ("unknown", "unknown")


def test_changed_key_member_cannot_migrate(swarm, tmp_path):
    _, _, _, _, copy = swarm
    state = SqliteSeedState(copy)
    store = FileMembershipState(tmp_path / "gate" / "private")
    store.initialize(export_seed_baseline(state, software_build_digest=BUILD))
    before = (store.root / "members.json").read_bytes()
    envelopes = []
    for request in profile_requests(state, swarm_id="swarm-a", software_build_digest=BUILD, software_version="1.0"):
        envelopes.append(sign_migration_profile(
            request, signer=generate_ed25519_signer(endpoint_id="changed"),
            new_incarnation="incarnation-v2", issued_at=NOW, observed_platform=HOSTS[request["node_id"]],
        ))
    with pytest.raises(ValueError):
        migration.execute_identity_preserving_migration(
            storage=store, request=MigrationRequest("a9-migration", tuple(envelopes)), now=NOW
        )
    assert (store.root / "members.json").read_bytes() == before


def test_seed_refuses_every_v1_write_after_the_recorded_cutover(swarm, tmp_path):
    database, signer, _, nodes, copy = swarm
    store, _, _ = _migrate(tmp_path, copy)
    gate = SeedMembershipCutover(store.root)
    gated = _coordinator(database, signer, cutover=gate, prefix="gated")
    accepted = gated.receive_member_message(
        nodes["node-mac"].heartbeat(lifecycle_state="CONFIGURED", active_requests=0),
        expected_protocol=HEARTBEAT_PROTOCOL,
    )
    assert accepted["sender_node_id"] == "node-mac"
    record = migration.record_cutover(
        storage=store, contract_digests=(CONTRACT,), inventory_digest=INVENTORY, now=NOW
    )
    assert record["post_generations"] == {"node-mac": 2, "node-linux": 2}
    assert gate.recorded() is True
    with sqlite3.connect(database) as connection:
        rows_before = connection.execute(
            "SELECT node_id, generation, incarnation, last_heartbeat_sequence FROM seed_members ORDER BY node_id"
        ).fetchall()
    with pytest.raises(SeedCoordinatorError) as heartbeat:
        gated.receive_member_message(
            nodes["node-mac"].heartbeat(lifecycle_state="CONFIGURED", active_requests=0),
            expected_protocol=HEARTBEAT_PROTOCOL,
        )
    assert heartbeat.value.code == "membership_contract_upgrade_required"
    resumed = _node(tmp_path, "node-linux", "linux_numpy_iroh", incarnation="incarnation-b")
    with pytest.raises(SeedCoordinatorError) as resume:
        gated.accept_resume(resume_envelope=resumed.resume_request(
            previous_generation=1, previous_incarnation="incarnation-a",
            endpoint_addrs=["https://100.117.33.124:9443/control"],
        ))
    assert resume.value.code == "membership_contract_upgrade_required"
    late = _node(tmp_path, "node-late", "mac_mlx_iroh")
    with pytest.raises(SeedCoordinatorError) as join:
        _join(gated, late, "invite-late")
    assert join.value.code == "membership_contract_upgrade_required"
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT node_id, generation, incarnation, last_heartbeat_sequence FROM seed_members ORDER BY node_id"
        ).fetchall() == rows_before
    removal = migration.remove_migration_only_reader(storage=store, now=NOW + 1)
    assert removal["v1_reader_removed"] is True
    with pytest.raises(MigrationError):
        migration.rollback_before_commit(storage=store, now=NOW + 1)


def test_coordinator_rejects_an_invalid_cutover_gate(swarm):
    database, signer, _, _, _ = swarm
    with pytest.raises(ValueError, match="membership_cutover"):
        _coordinator(database, signer, cutover=object())
