# SPDX-License-Identifier: AGPL-3.0-only
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
import sqlite3

import pytest

from mycelium_platform_neutral.migration_executor import MigrationExecutor

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
from tests.host_platform.portable import copy_private_file, make_private_directory

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
        endpoint_addrs=["https://203.0.113.11:9443/control"],
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
    make_private_directory(copy.parent)
    copy_private_file(database, copy)
    return database, signer, coordinator, nodes, copy


def _member_state(seed_signer, node_id, incarnation="incarnation-a", generation=1):
    return {
        "node_id": node_id,
        "swarm_id": "swarm-a",
        "incarnation": incarnation,
        "membership_generation": generation,
        "seed_key_digest": seed_signer.verification_key_digest,
    }


def _baseline(copy, seed_signer):
    return export_seed_baseline(
        SqliteSeedState(copy),
        software_build_digest=BUILD,
        swarm_id="swarm-a",
        seed_key_digest=seed_signer.verification_key_digest,
    )


def _requests(copy, seed_signer):
    return profile_requests(
        SqliteSeedState(copy),
        signer=seed_signer,
        swarm_id="swarm-a",
        software_build_digest=BUILD,
        software_version="1.0",
    )


def _migrate(tmp_path, copy, seed_signer):
    baseline = _baseline(copy, seed_signer)
    store = FileMembershipState(tmp_path / "gate" / "private")
    store.initialize(baseline)
    envelopes = []
    for envelope in _requests(copy, seed_signer):
        node = envelope["request"]["node_id"]
        signer = load_or_create_node_signer(tmp_path / "nodes" / f"{node}.key")
        envelopes.append(
            sign_migration_profile(
                envelope,
                signer=signer,
                member_state=_member_state(seed_signer, node),
                new_incarnation="incarnation-v2",
                issued_at=NOW,
                observed_platform=HOSTS[node],
            )
        )
    request = MigrationRequest(migration_id="a9-migration", profiles=tuple(envelopes))
    assert migration.execute_identity_preserving_migration(storage=store, request=request, now=NOW) == "committed"
    return store, baseline, envelopes


def test_seed_export_is_the_executor_baseline(swarm):
    _, signer, coordinator, nodes, copy = swarm
    baseline = _baseline(copy, signer)
    assert set(baseline) == {"node-mac", "node-linux"}
    for node_id, record in baseline.items():
        assert record["generation"] == 1
        assert record["revoked"] is False
        assert record["verification_key_digest"] == coordinator.member(node_id)["verification_key_digest"]
        assert record["lease_expires_at"] == NOW + 300.0


def test_profile_requests_decompose_legacy_classes_without_raw_endpoint_ids(swarm):
    _, signer, coordinator, _, copy = swarm
    requests = {item["request"]["node_id"]: item["request"] for item in _requests(copy, signer)}
    assert requests["node-mac"]["membership_generation"] == 2
    assert [runtime["backend_id"] for runtime in requests["node-mac"]["runtimes"]] == ["mlx"]
    assert [runtime["backend_id"] for runtime in requests["node-linux"]["runtimes"]] == ["numpy"]
    endpoint = coordinator.member("node-mac")["endpoint_id"]
    assert endpoint not in repr(requests)


def test_member_platform_comes_from_the_host_not_the_label(swarm):
    _, signer, _, _, copy = swarm
    request = next(item for item in _requests(copy, signer) if item["request"]["node_id"] == "node-mac")
    member = _member_state(signer, "node-mac")
    with pytest.raises(ValueError, match="platform_observation_conflicts_legacy_class"):
        build_migration_profile(request, member_state=member, new_incarnation="i2", issued_at=NOW, observed_platform=("linux", "x86_64"))
    with pytest.raises(ValueError, match="incarnation_not_renewed"):
        build_migration_profile(request, member_state=member, new_incarnation="incarnation-a", issued_at=NOW, observed_platform=("macos", "arm64"))
    with pytest.raises(ValueError, match="legacy_generation_stale"):
        build_migration_profile(request, member_state=member, new_incarnation="i2", issued_at=NOW + 301, observed_platform=("macos", "arm64"))
    profile = build_migration_profile(request, member_state=member, new_incarnation="i2", issued_at=NOW, observed_platform=("macos", "arm64"))
    assert (profile.platform_family, profile.architecture) == ("macos", "arm64")
    assert observe_host_platform(system="Windows", machine="AMD64") == ("windows", "x86_64")
    assert observe_host_platform(system="Plan9", machine="mips") == ("unknown", "unknown")


def test_changed_key_member_cannot_migrate(swarm, tmp_path):
    _, seed_signer, _, _, copy = swarm
    store = FileMembershipState(tmp_path / "gate" / "private")
    store.initialize(_baseline(copy, seed_signer))
    before = (store.root / "members.json").read_bytes()
    envelopes = []
    for request in _requests(copy, seed_signer):
        node = request["request"]["node_id"]
        envelopes.append(sign_migration_profile(
            request, signer=generate_ed25519_signer(endpoint_id="changed"),
            member_state=_member_state(seed_signer, node),
            new_incarnation="incarnation-v2", issued_at=NOW, observed_platform=HOSTS[node],
        ))
    with pytest.raises(ValueError):
        migration.execute_identity_preserving_migration(
            storage=store, request=MigrationRequest("a9-migration", tuple(envelopes)), now=NOW
        )
    assert (store.root / "members.json").read_bytes() == before


def test_seed_refuses_every_v1_write_after_the_recorded_cutover(swarm, tmp_path):
    database, signer, _, nodes, copy = swarm
    store, _, _ = _migrate(tmp_path, copy, signer)
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
            endpoint_addrs=["https://203.0.113.11:9443/control"],
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


# -- trust boundary (security review 2026-10-07) ---------------------------------


def test_seed_copy_bound_to_another_seed_or_swarm_is_refused(swarm):
    _, signer, _, _, copy = swarm
    other = generate_ed25519_signer(endpoint_id="other-seed")
    state = SqliteSeedState(copy)
    with pytest.raises(ValueError, match="seed_identity_binding_mismatch"):
        export_seed_baseline(state, software_build_digest=BUILD, swarm_id="swarm-a",
                             seed_key_digest=other.verification_key_digest)
    with pytest.raises(ValueError, match="seed_identity_binding_mismatch"):
        export_seed_baseline(state, software_build_digest=BUILD, swarm_id="swarm-b",
                             seed_key_digest=signer.verification_key_digest)
    with pytest.raises(ValueError, match="seed_identity_binding_mismatch"):
        profile_requests(state, signer=other, swarm_id="swarm-a",
                         software_build_digest=BUILD, software_version="1.0")


def test_member_refuses_unsigned_forged_tampered_or_foreign_requests(swarm, tmp_path):
    _, signer, _, _, copy = swarm
    good = next(item for item in _requests(copy, signer) if item["request"]["node_id"] == "node-mac")
    member = _member_state(signer, "node-mac")
    node_key = load_or_create_node_signer(tmp_path / "nodes" / "node-mac.key")

    def attempt(envelope, state=member):
        return sign_migration_profile(envelope, signer=node_key, member_state=state,
                                      new_incarnation="incarnation-v2", issued_at=NOW,
                                      observed_platform=("macos", "arm64"))

    with pytest.raises(ValueError):
        attempt(good["request"])  # bare unsigned request
    forger = generate_ed25519_signer(endpoint_id="forger")
    forged = {**good, "signature": forger.sign(good["request"]),
              "verification_key": forger.public_key_record()}
    with pytest.raises(ValueError, match="migration_profile_request_not_seed_signed"):
        attempt(forged)
    tampered = {**good, "request": {**good["request"], "membership_generation": 9}}
    with pytest.raises(ValueError, match="migration_profile_request_signature_invalid"):
        attempt(tampered)
    tampered = {**good, "request": {**good["request"], "runtimes": []}}
    with pytest.raises(ValueError, match="migration_profile_request_signature_invalid"):
        attempt(tampered)
    for foreign in (
        _member_state(signer, "node-linux"),
        _member_state(signer, "node-mac", incarnation="incarnation-z"),
        _member_state(signer, "node-mac", generation=4),
        {**member, "swarm_id": "swarm-b"},
    ):
        with pytest.raises(ValueError, match="migration_profile_request_not_for_this_member"):
            attempt(good, foreign)
    with pytest.raises(ValueError, match="migration_profile_request_not_seed_signed"):
        attempt(good, {**member, "seed_key_digest": forger.verification_key_digest})
    with pytest.raises(ValueError, match="migration_member_state_invalid"):
        attempt(good, {"node_id": "node-mac"})
    assert attempt(good)["profile"]["node_id"] == "node-mac"


def test_cutover_cannot_be_recorded_between_the_gate_check_and_the_seed_write(swarm, tmp_path, monkeypatch):
    database, signer, _, nodes, copy = swarm
    store, _, _ = _migrate(tmp_path, copy, signer)
    gated = _coordinator(database, signer, cutover=SeedMembershipCutover(store.root), prefix="toctou")
    observed = []
    original = SqliteSeedState.commit_heartbeat_renewal

    def racing_commit(self, *args, **kwargs):
        # A concurrent migration operator tries to record the cutover mid-write.
        try:
            with MigrationExecutor(FileMembershipState(store.root)).storage.lock(blocking=False):
                observed.append("cutover_lock_free_during_write")
        except migration.MigrationUnavailable as busy:
            observed.append(busy.code)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(SqliteSeedState, "commit_heartbeat_renewal", racing_commit)
    gated.receive_member_message(
        nodes["node-mac"].heartbeat(lifecycle_state="CONFIGURED", active_requests=0),
        expected_protocol=HEARTBEAT_PROTOCOL,
    )
    assert observed == ["concurrent_resume_serialized"]
    monkeypatch.setattr(SqliteSeedState, "commit_heartbeat_renewal", original)
    migration.record_cutover(storage=store, contract_digests=(CONTRACT,), inventory_digest=INVENTORY, now=NOW)
    with pytest.raises(SeedCoordinatorError, match="membership_contract_upgrade_required"):
        gated.receive_member_message(
            nodes["node-mac"].heartbeat(lifecycle_state="CONFIGURED", active_requests=0),
            expected_protocol=HEARTBEAT_PROTOCOL,
        )


def test_an_unreadable_cutover_gate_fails_closed(swarm, tmp_path):
    database, signer, _, nodes, copy = swarm
    store, _, _ = _migrate(tmp_path, copy, signer)
    migration.record_cutover(storage=store, contract_digests=(CONTRACT,), inventory_digest=INVENTORY, now=NOW)
    gated = _coordinator(database, signer, cutover=SeedMembershipCutover(store.root), prefix="corrupt")
    (store.root / "cutover.json").write_bytes(b"{}")
    with pytest.raises(SeedCoordinatorError, match="membership_cutover_unreadable"):
        gated.receive_member_message(
            nodes["node-mac"].heartbeat(lifecycle_state="CONFIGURED", active_requests=0),
            expected_protocol=HEARTBEAT_PROTOCOL,
        )
