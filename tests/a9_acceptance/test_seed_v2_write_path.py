# SPDX-License-Identifier: AGPL-3.0-only
"""Live A9 cutover: the seed's v2 write path.

After the executor commits the migration privately, the seed itself commits
generation n+1 for every member in one compare-and-swap transaction and records
the v2 membership contract.  From then on v1 declarations and stale v1
processes are refused with ``membership_contract_upgrade_required`` while a
member that resumes with its signed v2 profile (and a new member that joins
with one) keeps working: heartbeats, renewals and assignment offers continue.
"""

from __future__ import annotations

from itertools import count
from pathlib import Path
import sqlite3

import pytest

from mycelium_invite import SqliteInviteRegistry, verify_invite_bundle
from mycelium_membership import HEARTBEAT_PROTOCOL
from mycelium_membership.contracts import MembershipContractError, validate_membership_message
from mycelium_node import NodeMembershipSession, load_or_create_node_signer
from mycelium_platform_neutral import migration
from mycelium_platform_neutral.migration_executor import FileMembershipState, MigrationRequest
from mycelium_platform_neutral.peer_profile import PeerProfile, sign_peer_profile
from mycelium_platform_neutral.seed_migration import (
    export_seed_baseline,
    executor_cutover_inputs,
    prepare_seed_cutover,
    profile_requests,
    sign_migration_profile,
    v2_profile_for_session,
)
from mycelium_qualification.signing import generate_ed25519_signer
from mycelium_seed import SeedCoordinator, SeedCoordinatorError, SqliteSeedState
from tests.host_platform.portable import copy_private_file, make_private_directory

NOW = 2_000.0
BUILD = "sha256:" + "b" * 64
CONTRACT = "sha256:" + "c" * 64
INVENTORY = "sha256:" + "d" * 64
MAC = {"runtime_backend": "mlx", "transport": "iroh", "activation_protocol": "mycelium.router_wire.v1"}
ADDR = ["https://203.0.113.11:9443/control"]


def _ids(prefix):
    values = count(1)
    return lambda: f"{prefix}-{next(values)}"


class Clock:
    def __init__(self):
        self.now = NOW

    def __call__(self):
        return self.now


def _coordinator(database, signer, clock, prefix):
    return SeedCoordinator(
        swarm_id="swarm-a", seed_node_id="seed-node", seed_url="http://127.0.0.1:8788",
        signer=signer, invite_registry=SqliteInviteRegistry(database), state=SqliteSeedState(database),
        incarnation="seed-incarnation", clock=clock, id_source=_ids(prefix), lease_seconds=300.0,
    )


def _session(tmp_path, node_id, clock, incarnation):
    return NodeMembershipSession(
        node_id=node_id, swarm_id="swarm-a", seed_node_id="seed-node",
        signer=load_or_create_node_signer(tmp_path / "nodes" / f"{node_id}.key"),
        incarnation=incarnation, software_version="mycelium-test", peer_class="mac_mlx_iroh",
        runtime_capability=MAC, clock=clock, id_source=_ids(f"{node_id}-{incarnation}"),
    )


def _join(coordinator, session, nonce, **kwargs):
    bundle = coordinator.mint_invite(nonce=nonce, ttl_seconds=120)
    verified = verify_invite_bundle(bundle, now=NOW)
    request = session.join_request(invite_nonce=verified["payload"]["nonce"], endpoint_addrs=ADDR, **kwargs)
    acceptance = coordinator.accept_join(invite_token=bundle["token"], join_envelope=request)
    session.accept_join(acceptance, seed_key_digest=verified["seed_key_digest"])
    return acceptance


@pytest.fixture()
def migrated(tmp_path):
    """Two v1 members, a private executor migration, then the live seed cutover inputs."""

    clock = Clock()
    database = tmp_path / "seed" / "state.sqlite3"
    seed_signer = generate_ed25519_signer(endpoint_id="seed-endpoint")
    coordinator = _coordinator(database, seed_signer, clock, "seed")
    for index, node_id in enumerate(("node-a", "node-b")):
        _join(coordinator, _session(tmp_path, node_id, clock, "incarnation-a"), f"invite-{index}")
    gate = tmp_path / "gate"
    make_private_directory(gate)
    copy = gate / "seed.sqlite3"
    copy_private_file(database, copy)
    state = SqliteSeedState(copy)
    store = FileMembershipState(gate / "private")
    store.initialize(export_seed_baseline(state, software_build_digest=BUILD, swarm_id="swarm-a",
                                          seed_key_digest=seed_signer.verification_key_digest))
    envelopes = []
    for request in profile_requests(state, signer=seed_signer, swarm_id="swarm-a",
                                    software_build_digest=BUILD, software_version="1.0"):
        node_id = request["request"]["node_id"]
        envelopes.append(sign_migration_profile(
            request, signer=load_or_create_node_signer(tmp_path / "nodes" / f"{node_id}.key"),
            member_state={"node_id": node_id, "swarm_id": "swarm-a", "incarnation": "incarnation-a",
                          "membership_generation": 1, "seed_key_digest": seed_signer.verification_key_digest},
            new_incarnation="incarnation-v2", issued_at=NOW, observed_platform=("macos", "arm64"),
        ))
    request = MigrationRequest("a9-live", tuple(envelopes))
    assert migration.execute_identity_preserving_migration(storage=store, request=request, now=NOW) == "committed"
    migration.record_cutover(storage=store, contract_digests=(CONTRACT,), inventory_digest=INVENTORY, now=NOW)
    inputs = executor_cutover_inputs(store)
    return {"tmp": tmp_path, "clock": clock, "database": database, "seed_signer": seed_signer,
            "coordinator": coordinator, "inputs": inputs, "record": inputs["record"],
            "profiles": inputs["profiles"]}


def _cut_over(ctx):
    prepared = prepare_seed_cutover(**ctx["inputs"], seed_state=SqliteSeedState(ctx["database"]))
    SqliteSeedState(ctx["database"]).commit_membership_cutover(**prepared)
    return _coordinator(ctx["database"], ctx["seed_signer"], ctx["clock"], "after")


def _v2_resume(ctx, coordinator, node_id, *, previous_generation=2, previous_incarnation="incarnation-v2",
               new_incarnation="incarnation-v3", signer=None):
    session = _session(ctx["tmp"], node_id, ctx["clock"], new_incarnation)
    profile = v2_profile_for_session(
        template=next(p for p in ctx["profiles"] if p["profile"]["node_id"] == node_id),
        signer=signer or session.signer, incarnation=new_incarnation,
        membership_generation=previous_generation + 1, issued_at=ctx["clock"](), validity_seconds=60.0,
    )
    request = session.resume_request(previous_generation=previous_generation,
                                     previous_incarnation=previous_incarnation,
                                     endpoint_addrs=ADDR, peer_profile=profile)
    acceptance = coordinator.accept_resume(resume_envelope=request)
    session.accept_resume(acceptance, seed_key_digest=ctx["seed_signer"].verification_key_digest)
    return session, acceptance


def test_v2_form_is_refused_before_the_cutover(migrated):
    with pytest.raises(SeedCoordinatorError, match="membership_v2_before_cutover"):
        _v2_resume(migrated, migrated["coordinator"], "node-a", previous_generation=1,
                   previous_incarnation="incarnation-a")
    assert SqliteSeedState(migrated["database"]).membership_contract()["version"] == "v1"


def test_live_cutover_commits_n_plus_one_atomically_and_records_the_contract(migrated):
    _cut_over(migrated)
    state = SqliteSeedState(migrated["database"])
    contract = state.membership_contract()
    assert contract["version"] == "v2" and contract["cutover_generation"] == 2
    rows = {row["node_id"]: row for row in state.load_members()}
    assert {node: (row["generation"], row["incarnation"]) for node, row in rows.items()} == {
        "node-a": (2, "incarnation-v2"), "node-b": (2, "incarnation-v2")}
    assert set(state.member_profiles_v2()) == {"node-a", "node-b"}
    with pytest.raises(Exception, match="membership_cutover_already_recorded"):
        _cut_over(migrated)


def test_cutover_compare_and_swap_aborts_on_any_concurrent_member_change(migrated):
    with sqlite3.connect(migrated["database"]) as connection:
        connection.execute("UPDATE seed_members SET incarnation = 'moved' WHERE node_id = 'node-b'")
    prepared_inputs = migrated["inputs"]
    with pytest.raises(ValueError):
        prepare_seed_cutover(**prepared_inputs, seed_state=SqliteSeedState(migrated["database"]))
    with sqlite3.connect(migrated["database"]) as connection:
        connection.execute("UPDATE seed_members SET incarnation = 'incarnation-a' WHERE node_id = 'node-b'")
    prepared = prepare_seed_cutover(**prepared_inputs, seed_state=SqliteSeedState(migrated["database"]))
    with sqlite3.connect(migrated["database"]) as connection:
        connection.execute("UPDATE seed_members SET generation = 7 WHERE node_id = 'node-b'")
    with pytest.raises(Exception, match="membership_cutover_member_conflict"):
        SqliteSeedState(migrated["database"]).commit_membership_cutover(**prepared)
    state = SqliteSeedState(migrated["database"])
    assert state.membership_contract()["version"] == "v1"
    assert {row["node_id"]: row["generation"] for row in state.load_members()}["node-a"] == 1


def test_prepare_refuses_forged_or_mismatched_profiles(migrated):
    state = SqliteSeedState(migrated["database"])
    forger = generate_ed25519_signer(endpoint_id="forger")
    forged = [sign_peer_profile(signer=forger, profile=PeerProfile.from_mapping(p["profile"]))
              for p in migrated["profiles"]]
    source = migrated["inputs"]["source_members"]
    with pytest.raises(ValueError, match="key_continuity"):
        prepare_seed_cutover(record=migrated["record"], profiles=forged, source_members=source, seed_state=state)
    with pytest.raises(ValueError, match="membership_cutover_inventory_mismatch"):
        prepare_seed_cutover(record=migrated["record"], profiles=migrated["profiles"][:1],
                             source_members=source, seed_state=state)
    tampered = dict(migrated["record"], post_generations={"node-a": 3, "node-b": 2})
    with pytest.raises(ValueError):
        prepare_seed_cutover(record=tampered, profiles=migrated["profiles"], source_members=source, seed_state=state)


def test_after_cutover_v1_writes_and_stale_processes_are_refused(migrated):
    stale = _session(migrated["tmp"], "node-a", migrated["clock"], "incarnation-a")
    _join(_coordinator(migrated["tmp"] / "scratch" / "state.sqlite3", generate_ed25519_signer(endpoint_id="x"),
                       migrated["clock"], "scratch"), stale, "scratch")  # a joined v1 session object
    coordinator = _cut_over(migrated)
    before = sqlite3.connect(migrated["database"]).execute(
        "SELECT node_id, generation, incarnation, last_heartbeat_sequence FROM seed_members ORDER BY node_id").fetchall()
    with pytest.raises(SeedCoordinatorError, match="membership_contract_upgrade_required"):
        coordinator.receive_member_message(stale.heartbeat(lifecycle_state="CONFIGURED", active_requests=0),
                                           expected_protocol=HEARTBEAT_PROTOCOL)
    v1 = _session(migrated["tmp"], "node-a", migrated["clock"], "incarnation-z")
    with pytest.raises(SeedCoordinatorError, match="membership_contract_upgrade_required"):
        coordinator.accept_resume(resume_envelope=v1.resume_request(
            previous_generation=2, previous_incarnation="incarnation-v2", endpoint_addrs=ADDR))
    with pytest.raises(SeedCoordinatorError, match="membership_contract_upgrade_required"):
        _join(coordinator, _session(migrated["tmp"], "node-new", migrated["clock"], "i1"), "late")
    after = sqlite3.connect(migrated["database"]).execute(
        "SELECT node_id, generation, incarnation, last_heartbeat_sequence FROM seed_members ORDER BY node_id").fetchall()
    assert after == before


def test_v2_resume_heartbeat_and_assignment_keep_working_after_cutover(migrated):
    coordinator = _cut_over(migrated)
    session, acceptance = _v2_resume(migrated, coordinator, "node-a")
    assert session.generation == 3
    assert coordinator.member("node-a")["incarnation"] == "incarnation-v3"
    assert SqliteSeedState(migrated["database"]).member_profiles_v2()["node-a"]["generation"] == 3
    renewal = coordinator.receive_member_message(
        session.heartbeat(lifecycle_state="CONFIGURED", active_requests=0), expected_protocol=HEARTBEAT_PROTOCOL)
    assert renewal["sender_node_id"] == "node-a"
    restarted = _coordinator(migrated["database"], migrated["seed_signer"], migrated["clock"], "restarted")
    assert restarted.member("node-a")["generation"] == 3


def test_v2_resume_refuses_forged_profile_and_wrong_generation(migrated):
    coordinator = _cut_over(migrated)
    with pytest.raises(SeedCoordinatorError, match="peer_profile"):
        _v2_resume(migrated, coordinator, "node-a", signer=generate_ed25519_signer(endpoint_id="forger"))
    session = _session(migrated["tmp"], "node-b", migrated["clock"], "incarnation-v3")
    wrong = v2_profile_for_session(
        template=migrated["profiles"][1], signer=session.signer, incarnation="incarnation-v3",
        membership_generation=9, issued_at=NOW, validity_seconds=60.0)
    with pytest.raises(SeedCoordinatorError, match="membership_v2_profile_generation_mismatch"):
        coordinator.accept_resume(resume_envelope=session.resume_request(
            previous_generation=2, previous_incarnation="incarnation-v2", endpoint_addrs=ADDR, peer_profile=wrong))
    assert coordinator.member("node-b")["generation"] == 2


def test_v2_join_after_cutover_enrols_a_windows_member(migrated):
    coordinator = _cut_over(migrated)
    session = _session(migrated["tmp"], "node-win", migrated["clock"], "win-1")
    template = migrated["profiles"][0]
    profile = dict(template["profile"], node_id="node-win", profile_id="profile-node-win",
                   platform_family="windows", architecture="x86_64", runtimes=[])
    envelope = v2_profile_for_session(
        template={"profile": profile}, signer=session.signer, incarnation="win-1",
        membership_generation=1, issued_at=NOW, validity_seconds=60.0)
    _join(coordinator, session, "invite-win", peer_profile=envelope)
    member = coordinator.member("node-win")
    assert member["generation"] == 1
    assert member["peer_class"] == "windows_native_iroh"  # legacy consumer projection only
    assert SqliteSeedState(migrated["database"]).member_profiles_v2()["node-win"]["generation"] == 1


def test_contract_accepts_exactly_one_declaration_form(migrated):
    session = _session(migrated["tmp"], "node-a", migrated["clock"], "incarnation-q")
    envelope = session.resume_request(previous_generation=1, previous_incarnation="incarnation-a",
                                      endpoint_addrs=ADDR)
    both = dict(envelope["message"], peer_profile=migrated["profiles"][0])
    with pytest.raises(MembershipContractError):
        validate_membership_message(both)
    mismatched = {k: v for k, v in envelope["message"].items() if k not in ("peer_class", "runtime_capability")}
    mismatched["peer_profile"] = migrated["profiles"][1]  # node-b's profile on node-a's message
    with pytest.raises(MembershipContractError, match="binding_mismatch"):
        validate_membership_message(mismatched)


def test_online_cutover_updates_the_running_coordinator(migrated):
    coordinator = migrated["coordinator"]
    prepared = prepare_seed_cutover(**migrated["inputs"], seed_state=SqliteSeedState(migrated["database"]))
    coordinator.commit_membership_cutover(prepared)
    assert coordinator.membership_contract == "v2"
    assert coordinator.member("node-b")["generation"] == 2
    session, _ = _v2_resume(migrated, coordinator, "node-b")
    assert session.generation == 3
    with pytest.raises(SeedCoordinatorError, match="membership_cutover_already_recorded|upgrade_required"):
        coordinator.commit_membership_cutover(prepared)


def test_replaying_a_pre_cutover_v1_resume_is_refused_not_answered(migrated):
    coordinator = migrated["coordinator"]
    session = _session(migrated["tmp"], "node-b", migrated["clock"], "incarnation-r")
    request = session.resume_request(previous_generation=1, previous_incarnation="incarnation-a", endpoint_addrs=ADDR)
    acceptance = coordinator.accept_resume(resume_envelope=request)
    assert coordinator.accept_resume(resume_envelope=request) == acceptance  # v1 idempotent replay
    # The migration inventory now disagrees with node-b, so cut over from node-a's view only:
    with sqlite3.connect(migrated["database"]) as connection:
        connection.execute("INSERT INTO seed_metadata (key, value) VALUES ('membership_contract_version', 'v2')")
        connection.execute("INSERT INTO seed_metadata (key, value) VALUES ('membership_cutover_generation', '2')")
        connection.execute("INSERT INTO seed_metadata (key, value) VALUES ('membership_migration_record_digest', 'x')")
    after = _coordinator(migrated["database"], migrated["seed_signer"], migrated["clock"], "replay")
    with pytest.raises(SeedCoordinatorError, match="membership_contract_upgrade_required"):
        after.accept_resume(resume_envelope=request)


def test_http_join_route_exposes_the_cutover_codes(migrated):
    from mycelium_seed.http import SeedHTTPClient, SeedHTTPError, SeedHTTPServer

    _cut_over(migrated)
    coordinator = SeedCoordinator(
        swarm_id="swarm-a", seed_node_id="seed-node", seed_url=None, signer=migrated["seed_signer"],
        invite_registry=SqliteInviteRegistry(migrated["database"]), state=SqliteSeedState(migrated["database"]),
        incarnation="seed-http", clock=migrated["clock"], id_source=_ids("http"), lease_seconds=300.0)
    with SeedHTTPServer(coordinator, host="127.0.0.1", port=0):
        for nonce, use_profile in (("http-v1", False), ("http-v2", True)):
            bundle = coordinator.mint_invite(nonce=nonce, ttl_seconds=120)
            verified = verify_invite_bundle(bundle, now=NOW)
            client = SeedHTTPClient.from_invite_bundle(bundle, now=NOW, timeout=5.0)
            session = _session(migrated["tmp"], f"node-{nonce}", migrated["clock"], "h1")
            profile = None
            if use_profile:
                profile = v2_profile_for_session(
                    template={"profile": dict(migrated["profiles"][0]["profile"], node_id=session.node_id)},
                    signer=session.signer, incarnation="h1", membership_generation=1,
                    issued_at=NOW, validity_seconds=60.0)
            request = session.join_request(invite_nonce=verified["payload"]["nonce"], endpoint_addrs=ADDR,
                                           peer_profile=profile)
            if not use_profile:
                with pytest.raises(SeedHTTPError) as refused:
                    client.join(invite_token=bundle["token"], join_envelope=request)
                assert refused.value.code == "membership_contract_upgrade_required"
                assert int(refused.value.status) == 426
            else:
                acceptance = client.join(invite_token=bundle["token"], join_envelope=request)
                session.accept_join(acceptance, seed_key_digest=verified["seed_key_digest"])
                assert session.generation == 1
