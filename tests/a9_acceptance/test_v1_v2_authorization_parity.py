# SPDX-License-Identifier: AGPL-3.0-only
"""Authorization parity: every negative case gets the same refusal on v1 and v2.

The same world is built twice: a v1 seed (before the cutover) and a v2 seed
(after the live cutover).  In each, node-a has resumed once so it holds a live
session.  Each case is then run against both worlds, in the declaration form
that world admits, and must end in the identical outcome (same error type and
code, or the same accepted result).  Only the migration-specific refusals of
pre-migration identities (``membership_contract_upgrade_required``) and v2
profile signature checks have no v1 counterpart; they are tested elsewhere.
"""

from __future__ import annotations

from itertools import count
import sqlite3

import pytest

from mycelium_invite import InviteError, SqliteInviteRegistry, verify_invite_bundle
from mycelium_membership import HEARTBEAT_PROTOCOL
from mycelium_node import NodeMembershipSession, load_or_create_node_signer
from mycelium_platform_neutral import migration
from mycelium_platform_neutral.migration_executor import FileMembershipState, MigrationRequest
from mycelium_platform_neutral.seed_migration import (
    executor_cutover_inputs,
    export_seed_baseline,
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
MAC = {"runtime_backend": "mlx", "transport": "iroh", "activation_protocol": "mycelium.router_wire.v1"}
LINUX = {"runtime_backend": "numpy", "transport": "iroh", "activation_protocol": "mycelium.router_wire.v1"}
ADDR = ["https://203.0.113.11:9443/control"]


def _ids(prefix):
    values = count(1)
    return lambda: f"{prefix}-{next(values)}"


class Clock:
    def __init__(self):
        self.now = NOW

    def __call__(self):
        return self.now


class World:
    def __init__(self, tmp_path, form):
        self.tmp, self.form, self.clock = tmp_path, form, Clock()
        self.database = tmp_path / "seed" / "state.sqlite3"
        self.seed = generate_ed25519_signer(endpoint_id="seed-endpoint")
        self.coordinator = self._coordinator("seed")
        self.templates = {}
        for index, node in enumerate(("node-a", "node-b")):
            self.join(self.session(node, "incarnation-a"), f"invite-{index}")
        generation, incarnation = 1, "incarnation-a"
        if form == "v2":
            self._cut_over()
            self.coordinator = self._coordinator("after")
            generation, incarnation = 2, "incarnation-v2"
        self.current = self.resume("node-a", "incarnation-cur", generation, incarnation)[0]
        self.generation = self.current.generation

    def _coordinator(self, prefix):
        return SeedCoordinator(
            swarm_id="swarm-a", seed_node_id="seed-node", seed_url="http://127.0.0.1:8788",
            signer=self.seed, invite_registry=SqliteInviteRegistry(self.database),
            state=SqliteSeedState(self.database), incarnation="seed-incarnation",
            clock=self.clock, id_source=_ids(prefix), lease_seconds=300.0,
        )

    def restart_seed(self):
        self.coordinator = self._coordinator(f"restart-{self.clock.now}")

    def key(self, node):
        return load_or_create_node_signer(self.tmp / "nodes" / f"{node}.key")

    def session(self, node, incarnation, *, signer=None, peer_class="mac_mlx_iroh", capability=MAC):
        return NodeMembershipSession(
            node_id=node, swarm_id="swarm-a", seed_node_id="seed-node",
            signer=signer or self.key(node), incarnation=incarnation, software_version="mycelium-test",
            peer_class=peer_class, runtime_capability=capability, clock=self.clock,
            id_source=_ids(f"{node}-{incarnation}-{self.clock.now}"),
        )

    def profile(self, node, session, generation, **changes):
        template = {"profile": dict(self.templates.get(node, self.templates.get("node-a"))["profile"], **changes)}
        if node not in self.templates:
            template["profile"].update(node_id=node)
        return v2_profile_for_session(template=template, signer=session.signer, incarnation=session.incarnation,
                                      membership_generation=generation, issued_at=self.clock(),
                                      validity_seconds=60.0)

    def _cut_over(self):
        gate = self.tmp / "gate"
        make_private_directory(gate)
        copy_private_file(self.database, gate / "copy.sqlite3")
        state = SqliteSeedState(gate / "copy.sqlite3")
        store = FileMembershipState(gate / "private")
        store.initialize(export_seed_baseline(state, software_build_digest=BUILD, swarm_id="swarm-a",
                                              seed_key_digest=self.seed.verification_key_digest))
        signed = []
        for request in profile_requests(state, signer=self.seed, swarm_id="swarm-a",
                                        software_build_digest=BUILD, software_version="1.0"):
            node = request["request"]["node_id"]
            signed.append(sign_migration_profile(
                request, signer=self.key(node),
                member_state={"node_id": node, "swarm_id": "swarm-a", "incarnation": "incarnation-a",
                              "membership_generation": 1,
                              "seed_key_digest": self.seed.verification_key_digest},
                new_incarnation="incarnation-v2", issued_at=NOW, observed_platform=("macos", "arm64")))
        migration.execute_identity_preserving_migration(
            storage=store, request=MigrationRequest("parity", tuple(signed)), now=NOW)
        migration.record_cutover(storage=store, contract_digests=("sha256:" + "c" * 64,),
                                 inventory_digest="sha256:" + "d" * 64, now=NOW)
        inputs = executor_cutover_inputs(store)
        SqliteSeedState(self.database).commit_membership_cutover(
            **prepare_seed_cutover(**inputs, seed_state=SqliteSeedState(self.database)))
        self.templates = {p["profile"]["node_id"]: p for p in inputs["profiles"]}

    # -- requests in the form this world admits -----------------------------------

    def resume_request(self, node, incarnation, previous_generation, previous_incarnation, *,
                       signer=None, linux=False, mutate=None):
        session = self.session(node, incarnation, signer=signer,
                               peer_class="linux_numpy_iroh" if linux else "mac_mlx_iroh",
                               capability=LINUX if linux else MAC)
        profile = None
        if self.form == "v2":
            changes = {"platform_family": "linux"} if linux else {}
            profile = self.profile(node, session, previous_generation + 1, **changes)
        envelope = session.resume_request(previous_generation=previous_generation,
                                          previous_incarnation=previous_incarnation,
                                          endpoint_addrs=ADDR, peer_profile=profile)
        if mutate:
            envelope = mutate(envelope)
        return session, envelope

    def resume(self, node, incarnation, previous_generation, previous_incarnation, **kwargs):
        session, envelope = self.resume_request(node, incarnation, previous_generation, previous_incarnation, **kwargs)
        acceptance = self.coordinator.accept_resume(resume_envelope=envelope)
        session.accept_resume(acceptance, seed_key_digest=self.seed.verification_key_digest)
        return session, envelope, acceptance

    def join(self, session, nonce, *, bundle=None):
        bundle = bundle or self.coordinator.mint_invite(nonce=nonce, ttl_seconds=120)
        verified = verify_invite_bundle(bundle, now=NOW)
        profile = None
        if self.form == "v2" and self.coordinator.membership_contract == "v2":
            profile = self.profile(session.node_id, session, 1)
        request = session.join_request(invite_nonce=verified["payload"]["nonce"], endpoint_addrs=ADDR,
                                       peer_profile=profile)
        acceptance = self.coordinator.accept_join(invite_token=bundle["token"], join_envelope=request)
        session.accept_join(acceptance, seed_key_digest=verified["seed_key_digest"])
        return bundle, acceptance


def _outcome(fn):
    try:
        result = fn()
    except (SeedCoordinatorError, InviteError, ValueError) as error:
        return ("refused", type(error).__name__, getattr(error, "code", str(error)))
    if isinstance(result, (bool, int, str)):
        return ("accepted", result)
    return ("accepted", type(result).__name__)


def _forge(envelope):
    forger = generate_ed25519_signer(endpoint_id="forger")
    return {**envelope, "signature": forger.sign(envelope["message"]),
            "verification_key": forger.public_key_record()}


def _resign_with_swarm(world, node, incarnation, previous_generation, previous_incarnation):
    session, envelope = world.resume_request(node, incarnation, previous_generation, previous_incarnation)
    from mycelium_membership.contracts import sign_membership_message

    message = dict(envelope["message"], swarm_id="swarm-other")
    if "peer_profile" in message:  # the sender controls its own declaration too
        message["peer_profile"] = world.profile(node, session, previous_generation + 1, swarm_id="swarm-other")
    return sign_membership_message(signer=session.signer, message=message)


CASES = {
    "resume_accepts_the_current_identity": lambda w: w.resume(
        "node-b", "b-next", w.b_generation, w.b_incarnation)[2]["message"]["membership_generation"] - w.b_generation,
    "resume_stale_previous_generation": lambda w: w.resume("node-b", "b-next", w.b_generation + 5, w.b_incarnation),
    "resume_wrong_previous_incarnation": lambda w: w.resume("node-b", "b-next", w.b_generation, "not-it"),
    "resume_unknown_member": lambda w: w.resume("node-zz", "z1", w.b_generation, w.b_incarnation),
    "resume_forged_signature": lambda w: w.coordinator.accept_resume(resume_envelope=w.resume_request(
        "node-b", "b-next", w.b_generation, w.b_incarnation, mutate=_forge)[1]),
    "resume_other_member_key": lambda w: w.resume(
        "node-b", "b-next", w.b_generation, w.b_incarnation, signer=w.key("node-a")),
    "resume_wrong_swarm": lambda w: w.coordinator.accept_resume(
        resume_envelope=_resign_with_swarm(w, "node-b", "b-next", w.b_generation, w.b_incarnation)),
    "resume_changes_declared_capability": lambda w: w.resume(
        "node-b", "b-next", w.b_generation, w.b_incarnation, linux=True),
    "resume_expired_message": lambda w: (setattr(w.clock, "now", NOW + 3_000), w.restart_seed(),
                                         w.coordinator.accept_resume(resume_envelope=w.expired))[-1],
    "resume_exact_replay_is_idempotent": lambda w: w.coordinator.accept_resume(
        resume_envelope=w.current_request) == w.current_acceptance,
    "resume_revoked_member": lambda w: (w.revoke("node-b"), w.resume(
        "node-b", "b-next", w.b_generation, w.b_incarnation))[-1],
    "join_reuses_a_member_key": lambda w: w.join(w.session("node-new", "n1", signer=w.key("node-a")), "x1"),
    "join_existing_node_with_new_key": lambda w: w.join(
        w.session("node-b", "n1", signer=generate_ed25519_signer(endpoint_id="fresh")), "x2"),
    "join_replays_a_consumed_invite": lambda w: (w.join(w.session("node-c", "c1"), "x3"),
                                                 w.join(w.session("node-d", "d1"), "x3",
                                                        bundle=w.last_bundle))[-1],
    "join_fresh_member": lambda w: w.join(w.session("node-e", "e1"), "x4")[1]["message"]["membership_generation"],
    "heartbeat_current_session": lambda w: w.coordinator.receive_member_message(
        w.current.heartbeat(lifecycle_state="CONFIGURED", active_requests=0),
        expected_protocol=HEARTBEAT_PROTOCOL)["sender_node_id"],
    "heartbeat_from_a_superseded_session": lambda w: (w.resume(
        "node-a", "a-later", w.generation, "incarnation-cur"), w.coordinator.receive_member_message(
        w.current.heartbeat(lifecycle_state="CONFIGURED", active_requests=0),
        expected_protocol=HEARTBEAT_PROTOCOL))[-1],
    "heartbeat_forged_signature": lambda w: w.coordinator.receive_member_message(
        _forge(w.current.heartbeat(lifecycle_state="CONFIGURED", active_requests=0)),
        expected_protocol=HEARTBEAT_PROTOCOL),
    "assignment_offer_unknown_member": lambda w: w.coordinator.assignment_offer(
        node_id="node-zz", deployment_id="d", deployment_epoch=1, assignment_id="a1",
        assignment_digest="sha256:" + "1" * 64, stage_pack_digest="sha256:" + "2" * 64,
        graph_digest="sha256:" + "3" * 64, load_generation=1, peer_node_ids=["node-a"],
        placement_provenance="planner_v2"),
}


def _prepare(world):
    world.b_generation = 1 if world.form == "v1" else 2
    world.b_incarnation = "incarnation-a" if world.form == "v1" else "incarnation-v2"
    # node-b's request that expires before the clock jump.
    world.expired = world.resume_request("node-b", "b-exp", world.b_generation, world.b_incarnation)[1]
    session, request = world.resume_request("node-b", "b-cur", world.b_generation, world.b_incarnation)
    world.current_request = request
    world.current_acceptance = world.coordinator.accept_resume(resume_envelope=request)
    world.b_generation += 1
    world.b_incarnation = "b-cur"
    original_join = world.join

    def join(session, nonce, *, bundle=None):
        result = original_join(session, nonce, bundle=bundle)
        world.last_bundle = result[0]
        return result

    def tracking_join(session, nonce, *, bundle=None):
        if bundle is None:
            bundle = world.coordinator.mint_invite(nonce=nonce, ttl_seconds=120)
            world.last_bundle = bundle
        return original_join(session, nonce, bundle=bundle)

    world.join = tracking_join

    def revoke(node):
        with sqlite3.connect(world.database) as connection:
            connection.execute("UPDATE seed_members SET lifecycle_state = 'STOPPED' WHERE node_id = ?", (node,))
        world.restart_seed()

    world.revoke = revoke
    del join
    return world


@pytest.mark.parametrize("case", sorted(CASES))
def test_v1_and_v2_give_identical_outcomes(tmp_path, case):
    outcomes = {}
    for form in ("v1", "v2"):
        world = _prepare(World(tmp_path / form, form))
        assert world.coordinator.membership_contract == form
        outcomes[form] = _outcome(lambda: CASES[case](world))
    assert outcomes["v1"] == outcomes["v2"], outcomes
