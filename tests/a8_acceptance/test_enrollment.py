# SPDX-License-Identifier: AGPL-3.0-or-later
"""Deterministic gates for invitation, join, post-join control, revocation,
and bounded reconnect through the public HTTPS bootstrap adapter (spec §4-§5,
§10.1 seed_key_pinning and invitation_and_revocation)."""

from __future__ import annotations

from itertools import count
from pathlib import Path
import json

import pytest

from mycelium_invite import SqliteInviteRegistry, verify_invite_bundle, verify_invite
from mycelium_membership import LEASE_RENEWAL_PROTOCOL
from mycelium_node import NodeMembershipSession, load_or_create_node_signer
from mycelium_qualification.signing import generate_ed25519_signer
from mycelium_seed import SeedCoordinator
from mycelium_seed.http import SeedHTTPClient, SeedHTTPError, SeedHTTPServer
from mycelium_seed.operator import (
    SEED_KEY_TRANSITION_PROTOCOL,
)
from mycelium_seed.state import SqliteSeedState
from mycelium_internet.bootstrap import PublicBootstrapPolicy
from mycelium_internet.enrollment import (
    EnrollmentError,
    PublicBootstrapClient,
)


NOW = 3_000.0


class FakeClock:
    def __init__(self, start: float = NOW) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _ids(prefix: str):
    values = count(1)
    return lambda: f"{prefix}-{next(values)}"


def _node_session(root: Path, node_id: str, clock) -> NodeMembershipSession:
    return NodeMembershipSession(
        node_id=node_id,
        swarm_id="swarm-a8",
        seed_node_id="seed-node",
        signer=load_or_create_node_signer(root / "identity.key"),
        incarnation=f"{node_id}-incarnation",
        software_version="mycelium-test",
        peer_class="mac_mlx_iroh",
        runtime_capability={
            "runtime_backend": "mlx",
            "transport": "iroh",
            "activation_protocol": "mycelium.router_wire.v1",
        },
        clock=clock,
        id_source=_ids(f"{node_id}-message"),
    )


class Env:
    """A live seed + HTTPS-bound invite + adapter over a real loopback server."""

    def __init__(self, tmp_path: Path, *, clock: FakeClock | None = None) -> None:
        self.tmp_path = tmp_path
        self.clock = clock or FakeClock()
        database = tmp_path / "seed" / "seed.sqlite3"
        self.coordinator = SeedCoordinator(
            swarm_id="swarm-a8",
            seed_node_id="seed-node",
            seed_url=None,
            signer=generate_ed25519_signer(endpoint_id="seed-endpoint"),
            invite_registry=SqliteInviteRegistry(database),
            incarnation="seed-incarnation",
            state=SqliteSeedState(database),
            clock=self.clock,
            id_source=_ids("seed-message"),
        )
        self.policy = PublicBootstrapPolicy(
            canonical_origin="https://seed.example.com",
            clock=self.clock,
        )
        self.server = SeedHTTPServer(
            self.coordinator,
            host="127.0.0.1",
            port=0,
            public_seed_url="https://seed.example.com",
            policy=self.policy,
        ).start()
        self.bundle = self.coordinator.mint_invite(
            nonce="a8-invite",
            ttl_seconds=600,
        )
        verified = verify_invite_bundle(self.bundle, now=self.clock())
        assert verified["payload"]["seed_url"] == "https://seed.example.com"
        self.pin_digest = verified["seed_key_digest"]
        self.pin_records = list(verified["seed_key_records"])
        self.client = SeedHTTPClient(
            seed_url=self.server.base_url,
            swarm_id="swarm-a8",
            seed_key_digest=self.pin_digest,
            seed_key_records=self.pin_records,
            timeout=2.0,
        )
        self.adapter = PublicBootstrapClient.from_seed_client(
            self.client,
            policy=self.policy,
            tls_state="publicly_trusted",
            bundle=self.bundle,
            invite_token=self.bundle["token"],
            clock=self.clock,
        )

    def close(self) -> None:
        self.server.close()


@pytest.fixture()
def env(tmp_path: Path):
    environment = Env(tmp_path)
    yield environment
    environment.close()


def test_cleartext_marked_request_is_refused_at_the_server(env: Env) -> None:
    """End-to-end: the handler forwards the scheme markers into the boundary
    and a cleartext-marked request is refused (400), never served."""

    import urllib.error
    import urllib.request

    base = env.server.base_url
    with urllib.request.urlopen(
        urllib.request.Request(
            base + "/seed/identity", headers={"X-Forwarded-Proto": "https"}
        ),
        timeout=5,
    ) as response:
        assert response.status == 200
    with pytest.raises(urllib.error.HTTPError) as exc_info:
        urllib.request.urlopen(
            urllib.request.Request(
                base + "/seed/identity", headers={"X-Forwarded-Proto": "http"}
            ),
            timeout=5,
        )
    error = exc_info.value
    try:
        assert error.code == 400
    finally:
        error.close()


# ---------------------------------------------------------------------------
# Seed key pinning (spec §10.1)
# ---------------------------------------------------------------------------

def test_pin_success_before_any_secret_transmission(env: Env) -> None:
    identity = env.adapter.preflight(now=env.clock())
    assert identity["seed_endpoint_id"] == "seed-endpoint"
    assert env.adapter.pin_verified
    assert env.adapter._join_transmissions == 0  # noqa: SLF001


def test_wrong_pin_rejected_and_secret_never_transmitted(
    tmp_path: Path, env: Env
) -> None:
    rogue = generate_ed25519_signer(endpoint_id="rogue-seed")
    bundle = {
        **json.loads(json.dumps(env.bundle)),
        "seed_key_digest": rogue.verification_key_digest,
    }
    with pytest.raises(EnrollmentError) as exc_info:
        PublicBootstrapClient.from_seed_client(
            env.client,
            policy=env.policy,
            tls_state="publicly_trusted",
            bundle=bundle,
            invite_token=env.bundle["token"],
            clock=env.clock,
        )
    assert exc_info.value.code == "invite_bundle_key_pin_mismatch"
    # A certificate-valid but unsigned-by-pinned-key seed fails closed too:
    # serve a different signer's identity and watch the adapter refuse to join.
    wrong_pin_env = Env(tmp_path / "wrong")
    try:
        rogue_coordinator = SeedCoordinator(
            swarm_id="swarm-a8",
            seed_node_id="seed-node",
            seed_url=None,
            signer=rogue,
            invite_registry=SqliteInviteRegistry(
                tmp_path / "wrong" / "invites.sqlite3"
            ),
            incarnation="seed-incarnation",
            clock=wrong_pin_env.clock,
        )
        with SeedHTTPServer(
            rogue_coordinator,
            host="127.0.0.1",
            port=0,
            public_seed_url="https://seed.example.com",
        ) as rogue_server:
            rogue_client = SeedHTTPClient(
                seed_url=rogue_server.base_url,
                swarm_id="swarm-a8",
                seed_key_digest=wrong_pin_env.pin_digest,
                seed_key_records=wrong_pin_env.pin_records,
                timeout=2.0,
            )
            adapter = PublicBootstrapClient.from_seed_client(
                rogue_client,
                policy=wrong_pin_env.policy,
                tls_state="publicly_trusted",
                bundle=wrong_pin_env.bundle,
                invite_token=wrong_pin_env.bundle["token"],
                clock=wrong_pin_env.clock,
            )
            with pytest.raises(EnrollmentError) as exc_info:
                adapter.preflight(now=wrong_pin_env.clock())
            assert exc_info.value.code in {"pin_mismatch", "seed_signature_invalid"}
            with pytest.raises(EnrollmentError) as exc_info:
                adapter.join(
                    _node_session(
                        tmp_path / "wrong" / "node",
                        "node-wrong-pin",
                        wrong_pin_env.clock,
                    ).join_request(
                        invite_nonce="a8-invite",
                        endpoint_addrs=["https://node-wrong-pin/control"],
                    ),
                    now=wrong_pin_env.clock(),
                )
            assert exc_info.value.code == "pin_not_verified"
            assert adapter._join_transmissions == 0  # noqa: SLF001
    finally:
        wrong_pin_env.close()


def test_rotated_overlap_accepts_both_digests(env: Env) -> None:
    old_signer = env.coordinator.signer
    new_signer = generate_ed25519_signer(endpoint_id="seed-new")
    transition = {
        "swarm_id": "swarm-a8",
        "seed_node_id": "seed-node",
        "previous_generation": 1,
        "authority_generation": 2,
        "old_seed_key_digest": old_signer.verification_key_digest,
        "new_seed_key_digest": new_signer.verification_key_digest,
        "initiated_at": NOW,
        "effective_at": NOW,
        "overlap_expires_at": NOW + 60,
        "reason": "scheduled_rotation",
    }
    envelope = {
        "protocol": SEED_KEY_TRANSITION_PROTOCOL,
        "transition": transition,
        "old_signature": old_signer.sign(transition),
        "old_verification_key": old_signer.public_key_record(),
        "new_signature": new_signer.sign(transition),
        "new_verification_key": new_signer.public_key_record(),
    }
    rotation = env.adapter.rotation(now=env.clock(), envelope=envelope)
    assert rotation == envelope
    assert env.adapter.accepted_seed_key_digest(
        old_signer.verification_key_digest, now=env.clock()
    ) == old_signer.verification_key_digest
    assert env.adapter.accepted_seed_key_digest(
        new_signer.verification_key_digest, now=env.clock()
    ) == new_signer.verification_key_digest
    # After the overlap expires, the old digest is no longer accepted.
    env.clock.advance(61.0)
    with pytest.raises(EnrollmentError):
        env.adapter.accepted_seed_key_digest(
            old_signer.verification_key_digest, now=env.clock()
        )
    assert env.adapter.accepted_seed_key_digest(
        new_signer.verification_key_digest, now=env.clock()
    ) == new_signer.verification_key_digest


def test_expired_rotation_rejected(env: Env) -> None:
    old_signer = env.coordinator.signer
    new_signer = generate_ed25519_signer(endpoint_id="seed-new")
    transition = {
        "swarm_id": "swarm-a8",
        "seed_node_id": "seed-node",
        "previous_generation": 1,
        "authority_generation": 2,
        "old_seed_key_digest": old_signer.verification_key_digest,
        "new_seed_key_digest": new_signer.verification_key_digest,
        "initiated_at": NOW - 200,
        "effective_at": NOW - 200,
        "overlap_expires_at": NOW - 100,
        "reason": "scheduled_rotation",
    }
    envelope = {
        "protocol": SEED_KEY_TRANSITION_PROTOCOL,
        "transition": transition,
        "old_signature": old_signer.sign(transition),
        "old_verification_key": old_signer.public_key_record(),
        "new_signature": new_signer.sign(transition),
        "new_verification_key": new_signer.public_key_record(),
    }
    with pytest.raises(EnrollmentError):
        env.adapter.rotation(now=env.clock(), envelope=envelope)


def test_tls_success_without_invitation_authority_refuses_join(env: Env) -> None:
    adapter = PublicBootstrapClient.from_seed_client(
        env.client,
        policy=env.policy,
        tls_state="publicly_trusted",
        bundle=None,
        invite_token=None,
        clock=env.clock,
    )
    # Without an invitation there is no pin, hence no trust anchor: even the
    # identity fetch must fail closed.
    with pytest.raises(EnrollmentError) as exc_info:
        adapter.preflight(now=env.clock())
    assert exc_info.value.code == "invitation_authority_missing"
    node = _node_session(env.tmp_path, "node-no-authority", env.clock)
    with pytest.raises(EnrollmentError) as exc_info:
        adapter.join(
            node.join_request(
                invite_nonce="a8-invite",
                endpoint_addrs=["https://node-no-authority/control"],
            ),
            now=env.clock(),
        )
    assert exc_info.value.code == "pin_not_verified"
    assert adapter._join_transmissions == 0  # noqa: SLF001


def test_unverified_tls_refuses_everything(env: Env) -> None:
    adapter = PublicBootstrapClient.from_seed_client(
        env.client,
        policy=env.policy,
        tls_state="unverified",
        bundle=env.bundle,
        invite_token=env.bundle["token"],
        clock=env.clock,
    )
    with pytest.raises(EnrollmentError) as exc_info:
        adapter.preflight(now=env.clock())
    assert exc_info.value.code == "tls_not_publicly_trusted"
    node = _node_session(env.tmp_path, "node-no-tls", env.clock)
    with pytest.raises(EnrollmentError) as exc_info:
        adapter.join(
            node.join_request(
                invite_nonce="a8-invite",
                endpoint_addrs=["https://node-no-tls/control"],
            ),
            now=env.clock(),
        )
    assert exc_info.value.code == "tls_not_publicly_trusted"
    assert adapter._join_transmissions == 0  # noqa: SLF001


# ---------------------------------------------------------------------------
# Invitation and join (spec §4)
# ---------------------------------------------------------------------------

def test_full_join_through_the_public_adapter(env: Env) -> None:
    node = _node_session(env.tmp_path, "node-join", env.clock)
    identity = env.adapter.preflight(now=env.clock())
    assert identity["swarm_id"] == "swarm-a8"
    request = node.join_request(
        invite_nonce="a8-invite",
        endpoint_addrs=["https://node-join/control"],
    )
    acceptance = env.adapter.join(request, now=env.clock())
    assert acceptance["message"]["membership_generation"] == 1
    assert acceptance["message"]["accepted_node_id"] == "node-join"
    assert env.coordinator.member("node-join")["generation"] == 1
    assert env.adapter._join_transmissions == 1  # noqa: SLF001
    assert env.adapter._revoked is False  # noqa: SLF001


def test_exact_join_retry_returns_same_acceptance_without_retransmit(
    env: Env,
) -> None:
    node = _node_session(env.tmp_path, "node-retry", env.clock)
    env.adapter.preflight(now=env.clock())
    request = node.join_request(
        invite_nonce="a8-invite",
        endpoint_addrs=["https://node-retry/control"],
    )
    first = env.adapter.join(request, now=env.clock())
    second = env.adapter.join(request, now=env.clock())
    assert second == first
    assert env.adapter._join_transmissions == 1  # noqa: SLF001


def test_changed_retry_under_same_nonce_fails_closed(env: Env) -> None:
    node = _node_session(env.tmp_path, "node-changed", env.clock)
    env.adapter.preflight(now=env.clock())
    request = node.join_request(
        invite_nonce="a8-invite",
        endpoint_addrs=["https://node-changed/control"],
    )
    env.adapter.join(request, now=env.clock())
    changed = {
        **json.loads(json.dumps(request)),
        "message": {
            **json.loads(json.dumps(request["message"])),
            "endpoint_addr": {
                "id": request["message"]["sender_endpoint_id"],
                "addrs": ["https://node-changed/other"],
            },
        },
    }
    with pytest.raises(EnrollmentError) as exc_info:
        env.adapter.join(changed, now=env.clock())
    assert exc_info.value.code == "changed_retry_rejected"
    assert env.adapter._join_transmissions == 1  # noqa: SLF001
    assert env.coordinator.member("node-changed")["generation"] == 1


def test_wrong_swarm_join_rejected_without_partial_member(env: Env) -> None:
    node = NodeMembershipSession(
        node_id="node-wrong-swarm",
        swarm_id="other-swarm",
        seed_node_id="seed-node",
        signer=load_or_create_node_signer(env.tmp_path / "identity.key"),
        incarnation="node-wrong-swarm-incarnation",
        software_version="mycelium-test",
        peer_class="mac_mlx_iroh",
        runtime_capability={
            "runtime_backend": "mlx",
            "transport": "iroh",
            "activation_protocol": "mycelium.router_wire.v1",
        },
        clock=env.clock,
        id_source=_ids("node-wrong-message"),
    )
    env.adapter.preflight(now=env.clock())
    request = node.join_request(
        invite_nonce="a8-invite",
        endpoint_addrs=["https://node-wrong-swarm/control"],
    )
    with pytest.raises(EnrollmentError) as exc_info:
        env.adapter.join(request, now=env.clock())
    assert exc_info.value.code == "seed_join_mismatch"
    assert env.coordinator.members() == ()


def test_expired_invite_rejected_atomically(env: Env) -> None:
    node = _node_session(env.tmp_path, "node-expired", env.clock)
    env.adapter.preflight(now=env.clock())
    request = node.join_request(
        invite_nonce="a8-invite",
        endpoint_addrs=["https://node-expired/control"],
    )
    env.clock.advance(601.0)
    with pytest.raises(EnrollmentError) as exc_info:
        env.adapter.join(request, now=env.clock())
    assert exc_info.value.code == "invite_expired"
    assert env.coordinator.members() == ()
    # The invite remains unconsumed and still valid once time is restored.
    verify_invite(
        env.bundle["token"],
        verifier_key_records=[env.coordinator.signer.public_key_record()],
        now=NOW,
    )


def test_duplicate_key_and_endpoint_id_rejected(env: Env) -> None:
    first = _node_session(env.tmp_path, "node-dup", env.clock)
    env.adapter.preflight(now=env.clock())
    first_request = first.join_request(
        invite_nonce="a8-invite",
        endpoint_addrs=["https://node-dup/control"],
    )
    env.adapter.join(first_request, now=env.clock())
    second = _node_session(env.tmp_path, "node-dup-2", env.clock)
    # Mint a fresh invite for the second device, then replay the first
    # device's key and endpoint: the seed must refuse the duplicate identity.
    second_bundle = env.coordinator.mint_invite(nonce="a8-invite-2", ttl_seconds=600)
    second_request = second.join_request(
        invite_nonce="a8-invite-2",
        endpoint_addrs=["https://node-dup-2/control"],
    )
    second_adapter = PublicBootstrapClient.from_seed_client(
        env.client,
        policy=env.policy,
        tls_state="publicly_trusted",
        bundle=second_bundle,
        invite_token=second_bundle["token"],
        clock=env.clock,
    )
    second_adapter.preflight(now=env.clock())
    duplicate = {
        **json.loads(json.dumps(second_request)),
        "message": {
            **json.loads(json.dumps(second_request["message"])),
            "endpoint_addr": {
                "id": first_request["message"]["sender_endpoint_id"],
                "addrs": ["https://node-dup-2/control"],
            },
        },
    }
    with pytest.raises(EnrollmentError) as exc_info:
        second_adapter.join(duplicate, now=env.clock())
    assert exc_info.value.code == "seed_member_identity_reused"
    assert len(env.coordinator.members()) == 1


def test_unsupported_peer_class_rejected(env: Env) -> None:
    from mycelium_membership import SIGNED_MESSAGE_PROTOCOL

    node = _node_session(env.tmp_path, "node-bad-class", env.clock)
    request = node.join_request(
        invite_nonce="a8-invite",
        endpoint_addrs=["https://node-bad-class/control"],
    )
    message = {
        **json.loads(json.dumps(request["message"])),
        "peer_class": "not_a_real_peer_class",
    }
    envelope = {
        "protocol": SIGNED_MESSAGE_PROTOCOL,
        "message": message,
        "signature": node.signer.sign(message),
        "verification_key": node.signer.public_key_record(),
    }
    env.adapter.preflight(now=env.clock())
    with pytest.raises(EnrollmentError) as exc_info:
        env.adapter.join(envelope, now=env.clock())
    assert exc_info.value.code == "membership_peer_class_invalid"
    assert env.coordinator.members() == ()


# ---------------------------------------------------------------------------
# Post-join control and revocation (spec §5)
# ---------------------------------------------------------------------------

def _joined(env: Env, node_id: str) -> tuple[PublicBootstrapClient, NodeMembershipSession]:
    node = _node_session(env.tmp_path, node_id, env.clock)
    env.adapter.preflight(now=env.clock())
    request = node.join_request(
        invite_nonce="a8-invite",
        endpoint_addrs=[f"https://{node_id}/control"],
    )
    acceptance = env.adapter.join(request, now=env.clock())
    node.accept_join(acceptance, seed_key_digest=env.pin_digest)
    return env.adapter, node


def test_resume_advances_generation_through_the_adapter(env: Env) -> None:
    adapter, _ = _joined(env, "node-resume")
    restarted = NodeMembershipSession(
        node_id="node-resume",
        swarm_id="swarm-a8",
        seed_node_id="seed-node",
        signer=load_or_create_node_signer(env.tmp_path / "identity.key"),
        incarnation="node-resume-incarnation-r1",
        software_version="mycelium-test",
        peer_class="mac_mlx_iroh",
        runtime_capability={
            "runtime_backend": "mlx",
            "transport": "iroh",
            "activation_protocol": "mycelium.router_wire.v1",
        },
        clock=env.clock,
        id_source=_ids("node-resume-message"),
    )
    request = restarted.resume_request(
        previous_generation=1,
        previous_incarnation="node-resume-incarnation",
        endpoint_addrs=["https://node-resume/control"],
    )
    acceptance = adapter.resume(request, now=env.clock())
    assert acceptance["message"]["membership_generation"] == 2
    assert env.coordinator.member("node-resume")["generation"] == 2


def test_heartbeat_renews_lease_through_the_adapter(env: Env) -> None:
    adapter, node = _joined(env, "node-heartbeat")
    lease_before = env.coordinator.member("node-heartbeat")["lease_expires_at"]
    env.clock.advance(30.0)
    heartbeat = node.heartbeat(lifecycle_state="RUNNING", active_requests=0)
    renewal = adapter.heartbeat(heartbeat, now=env.clock())
    assert renewal["message"]["protocol"] == LEASE_RENEWAL_PROTOCOL
    lease_after = env.coordinator.member("node-heartbeat")["lease_expires_at"]
    assert lease_after >= lease_before


def test_lease_expiry_blocks_control_locally(env: Env) -> None:
    adapter, node = _joined(env, "node-lease")
    heartbeat = node.heartbeat(lifecycle_state="RUNNING", active_requests=0)
    lease_expires = float(env.coordinator.member("node-lease")["lease_expires_at"])
    env.clock.advance(lease_expires - env.clock() + 1.0)
    with pytest.raises(EnrollmentError) as exc_info:
        adapter.heartbeat(heartbeat, now=env.clock())
    assert exc_info.value.code == "lease_expired"
    assert adapter.freshness == "stale"


def test_revocation_rejects_control_and_sticks(env: Env) -> None:
    adapter, node = _joined(env, "node-revoked")
    env.coordinator.advance_member_generation(
        node_id="node-revoked",
        expected_generation=1,
        lifecycle_state="STOPPED",
    )
    restarted = NodeMembershipSession(
        node_id="node-revoked",
        swarm_id="swarm-a8",
        seed_node_id="seed-node",
        signer=load_or_create_node_signer(env.tmp_path / "identity.key"),
        incarnation="node-revoked-incarnation-r1",
        software_version="mycelium-test",
        peer_class="mac_mlx_iroh",
        runtime_capability={
            "runtime_backend": "mlx",
            "transport": "iroh",
            "activation_protocol": "mycelium.router_wire.v1",
        },
        clock=env.clock,
        id_source=_ids("node-revoked-message"),
    )
    request = restarted.resume_request(
        previous_generation=1,
        previous_incarnation="node-revoked-incarnation",
        endpoint_addrs=["https://node-revoked/control"],
    )
    with pytest.raises(EnrollmentError) as exc_info:
        adapter.resume(request, now=env.clock())
    assert exc_info.value.code == "revoked"
    assert adapter._revoked is True  # noqa: SLF001
    # Once revoked, further control refuses locally without any transmission.
    heartbeat = node.heartbeat(lifecycle_state="RUNNING", active_requests=0)
    with pytest.raises(EnrollmentError) as exc_info:
        adapter.heartbeat(heartbeat, now=env.clock())
    assert exc_info.value.code == "revoked"


def test_stale_generation_rejected(env: Env) -> None:
    adapter, _ = _joined(env, "node-stale")
    restarted = NodeMembershipSession(
        node_id="node-stale",
        swarm_id="swarm-a8",
        seed_node_id="seed-node",
        signer=load_or_create_node_signer(env.tmp_path / "identity.key"),
        incarnation="node-stale-incarnation-r1",
        software_version="mycelium-test",
        peer_class="mac_mlx_iroh",
        runtime_capability={
            "runtime_backend": "mlx",
            "transport": "iroh",
            "activation_protocol": "mycelium.router_wire.v1",
        },
        clock=env.clock,
        id_source=_ids("node-stale-message"),
    )
    request = restarted.resume_request(
        previous_generation=1,
        previous_incarnation="node-stale-incarnation",
        endpoint_addrs=["https://node-stale/control"],
    )
    adapter.resume(request, now=env.clock())
    assert env.coordinator.member("node-stale")["generation"] == 2
    # A further restart that still claims generation 1 is stale: the member
    # already advanced past it.
    stale = NodeMembershipSession(
        node_id="node-stale",
        swarm_id="swarm-a8",
        seed_node_id="seed-node",
        signer=load_or_create_node_signer(env.tmp_path / "identity.key"),
        incarnation="node-stale-incarnation-r2",
        software_version="mycelium-test",
        peer_class="mac_mlx_iroh",
        runtime_capability={
            "runtime_backend": "mlx",
            "transport": "iroh",
            "activation_protocol": "mycelium.router_wire.v1",
        },
        clock=env.clock,
        id_source=_ids("node-stale-message-2"),
    )
    stale_request = stale.resume_request(
        previous_generation=1,
        previous_incarnation="node-stale-incarnation-r1",
        endpoint_addrs=["https://node-stale/control"],
    )
    with pytest.raises(EnrollmentError) as exc_info:
        adapter.resume(stale_request, now=env.clock())
    assert exc_info.value.code in {"seed_resume_generation_stale", "generation_stale"}


def test_changed_incarnation_rejected(env: Env) -> None:
    adapter, _ = _joined(env, "node-incarnation")
    wrong_previous = NodeMembershipSession(
        node_id="node-incarnation",
        swarm_id="swarm-a8",
        seed_node_id="seed-node",
        signer=load_or_create_node_signer(env.tmp_path / "identity.key"),
        incarnation="node-incarnation-incarnation-r1",
        software_version="mycelium-test",
        peer_class="mac_mlx_iroh",
        runtime_capability={
            "runtime_backend": "mlx",
            "transport": "iroh",
            "activation_protocol": "mycelium.router_wire.v1",
        },
        clock=env.clock,
        id_source=_ids("node-incarnation-message"),
    )
    request = wrong_previous.resume_request(
        previous_generation=1,
        previous_incarnation="never_was_the_incarnation",
        endpoint_addrs=["https://node-incarnation/control"],
    )
    with pytest.raises(EnrollmentError) as exc_info:
        adapter.resume(request, now=env.clock())
    assert exc_info.value.code == "seed_resume_generation_stale"


def test_revoked_member_is_rejected_by_activation_admission_guard(
    env: Env,
) -> None:
    adapter, _ = _joined(env, "node-guard")
    env.coordinator.advance_member_generation(
        node_id="node-guard",
        expected_generation=1,
        lifecycle_state="STOPPED",
    )
    from mycelium_seed import SeedCoordinatorError

    with pytest.raises(SeedCoordinatorError) as exc_info:
        with env.coordinator.member_authority_guard(
            node_id="node-guard",
            expected_generation=1,
            expected_peer_class="mac_mlx_iroh",
            eligible_lifecycle_states=frozenset({"NEW", "RUNNING"}),
        ):
            pass
    assert exc_info.value.code in {
        "seed_member_lifecycle_ineligible",
        "seed_member_generation_stale",
    }
    assert adapter._revoked is False  # noqa: SLF001


# ---------------------------------------------------------------------------
# Bounded reconnect (spec §5)
# ---------------------------------------------------------------------------

class FlakyTransport:
    def __init__(self, real: SeedHTTPClient, failures: int, clock: FakeClock):
        self.real = real
        self.remaining_failures = failures
        self.calls: list[tuple[str, str]] = []
        self.clock = clock
        self.slept = 0.0

    def __call__(self, method: str, path: str, body):
        self.calls.append((method, path))
        if self.remaining_failures > 0:
            self.remaining_failures -= 1
            raise SeedHTTPError("seed_http_unreachable")
        return {
            "/seed/identity": lambda: self.real.request("GET", "/seed/identity", None),
            "/seed/rotation": lambda: self.real.request("GET", "/seed/rotation", None),
            "/seed/join": lambda: self.real.request("POST", "/seed/join", body),
            "/seed/resume": lambda: self.real.request("POST", "/seed/resume", body),
            "/seed/message": lambda: self.real.request("POST", "/seed/message", body),
        }[path]()


def test_bounded_reconnect_same_origin_no_fallback(env: Env) -> None:
    flaky = FlakyTransport(env.client, failures=2, clock=env.clock)
    adapter = PublicBootstrapClient(
        policy=env.policy,
        transport=flaky,
        tls_state="publicly_trusted",
        bundle=env.bundle,
        invite_token=env.bundle["token"],
        clock=env.clock,
        backoff_seconds=0.05,
    )
    identity = adapter.reconnect(max_attempts=3, backoff_seconds=0.05)
    assert identity["seed_endpoint_id"] == "seed-endpoint"
    assert flaky.calls == [
        ("GET", "/seed/identity"),
        ("GET", "/seed/identity"),
        ("GET", "/seed/identity"),
    ]
    assert adapter.total_backoff_seconds > 0.0
    # Only the canonical origin was ever used (the transport is the seam; the
    # adapter itself never dialed anything else).
    assert adapter.canonical_origin == "https://seed.example.com"


def test_reconnect_exhaustion_is_bounded_and_honest(env: Env) -> None:
    flaky = FlakyTransport(env.client, failures=99, clock=env.clock)
    adapter = PublicBootstrapClient(
        policy=env.policy,
        transport=flaky,
        tls_state="publicly_trusted",
        bundle=env.bundle,
        invite_token=env.bundle["token"],
        clock=env.clock,
        backoff_seconds=0.05,
    )
    with pytest.raises(EnrollmentError) as exc_info:
        adapter.reconnect(max_attempts=3, backoff_seconds=0.05)
    assert exc_info.value.code == "reconnect_exhausted"
    assert len(flaky.calls) == 3
    assert adapter.freshness == "unknown"


def test_bootstrap_status_contract_is_emitted_privacy_safe(env: Env) -> None:
    from mycelium_internet.contracts import validate_bootstrap_status

    adapter = env.adapter
    adapter.preflight(now=env.clock())
    status = adapter.bootstrap_status(now=env.clock())
    validate_bootstrap_status(status)
    assert status["tls_state"] == "publicly_trusted"
    assert status["canonical_origin_verified"] is True
    assert status["seed_pin_state"] == "verified"
    assert status["route_state"] == "available"
    assert status["invitation_state"] == "pending"
    encoded = json.dumps(status)
    assert "seed.example.com" not in encoded
    assert "127.0.0.1" not in encoded
    assert env.bundle["token"] not in encoded
