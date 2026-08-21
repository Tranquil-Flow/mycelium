# SPDX-License-Identifier: AGPL-3.0-or-later
"""Seed-side physical-case execution against a live loopback boundary.

These pins prove the runner executes the three seed-side negative cases
for real when an adapter exists: cleartext/redirect bootstrap refusal,
certificate-without-seed-authority, and invalid/replayed invitation. All
network observation is deterministic (a stub probe or a loopback server);
no physical result is fabricated - the envelopes carry executed=True only
because the procedures genuinely ran here.
"""

from __future__ import annotations

from itertools import count
from pathlib import Path
import time

import pytest

from mycelium_invite import SqliteInviteRegistry, verify_invite_bundle
from mycelium_node.identity import load_or_create_node_signer
from mycelium_node.membership import NodeMembershipSession
from mycelium_qualification.signing import generate_ed25519_signer
from mycelium_seed import SeedCoordinator
from mycelium_seed.http import SeedHTTPClient, SeedHTTPServer
from mycelium_seed.state import SqliteSeedState
from mycelium_internet.bootstrap import PublicBootstrapPolicy
from mycelium_internet.contracts import validate_internet_native_qualification
from mycelium_internet.enrollment import PublicBootstrapClient
from mycelium_internet.physical import (
    PhysicalGateError,
    execute_case,
    probe_bootstrap_over_cleartext,
)

ORIGIN = "https://seed.example.com"


def _ids(prefix: str):
    values = count(1)
    return lambda: f"{prefix}-{next(values)}"


class SeedEnv:
    def __init__(self, tmp_path: Path) -> None:
        self.tmp_path = tmp_path
        database = tmp_path / "seed" / "seed.sqlite3"
        self.coordinator = SeedCoordinator(
            swarm_id="swarm-a8",
            seed_node_id="seed-node",
            seed_url=None,
            signer=generate_ed25519_signer(endpoint_id="seed-endpoint"),
            invite_registry=SqliteInviteRegistry(database),
            incarnation="seed-incarnation",
            state=SqliteSeedState(database),
            clock=time.time,
            id_source=_ids("seed-message"),
        )
        self.policy = PublicBootstrapPolicy(canonical_origin=ORIGIN, clock=time.time)
        self.server = SeedHTTPServer(
            self.coordinator,
            host="127.0.0.1",
            port=0,
            public_seed_url=ORIGIN,
            policy=self.policy,
        ).start()
        self.bundle = self.coordinator.mint_invite(nonce="a8-invite", ttl_seconds=600)
        verified = verify_invite_bundle(self.bundle, now=time.time())
        self.pin_digest = verified["seed_key_digest"]
        self.pin_records = list(verified["seed_key_records"])
        rogue_database = tmp_path / "rogue" / "rogue.sqlite3"
        self.rogue_coordinator = SeedCoordinator(
            swarm_id="swarm-a8",
            seed_node_id="seed-node",
            seed_url=None,
            signer=generate_ed25519_signer(endpoint_id="rogue-seed-endpoint"),
            invite_registry=SqliteInviteRegistry(rogue_database),
            incarnation="rogue-incarnation",
            state=SqliteSeedState(rogue_database),
            clock=time.time,
            id_source=_ids("rogue-message"),
        )
        self.rogue_server = SeedHTTPServer(
            self.rogue_coordinator,
            host="127.0.0.1",
            port=0,
            public_seed_url=ORIGIN,
        ).start()

    def adapter(
        self,
        *,
        rogue: bool = False,
        unreachable: bool = False,
    ) -> PublicBootstrapClient:
        if unreachable:
            seed_url = "https://seed.example.invalid"
        elif rogue:
            seed_url = self.rogue_server.base_url
        else:
            seed_url = self.server.base_url
        client = SeedHTTPClient(
            seed_url=seed_url,
            swarm_id="swarm-a8",
            seed_key_digest=self.pin_digest,
            seed_key_records=self.pin_records,
            timeout=2.0,
        )
        return PublicBootstrapClient.from_seed_client(
            client,
            policy=PublicBootstrapPolicy(canonical_origin=ORIGIN, clock=time.time),
            tls_state="publicly_trusted",
            bundle=dict(self.bundle),
            invite_token=str(self.bundle["token"]),
            clock=time.time,
            backoff_seconds=0.01,
        )

    def node(self, node_id: str) -> NodeMembershipSession:
        return NodeMembershipSession(
            node_id=node_id,
            swarm_id="swarm-a8",
            seed_node_id="seed-node",
            signer=load_or_create_node_signer(
                self.tmp_path / f"{node_id}.key"
            ),
            incarnation=f"{node_id}-incarnation",
            software_version="mycelium-test",
            peer_class="mac_mlx_iroh",
            runtime_capability={
                "runtime_backend": "mlx",
                "transport": "iroh",
                "activation_protocol": "mycelium.router_wire.v1",
            },
            clock=time.time,
            id_source=_ids(node_id),
        )

    def close(self) -> None:
        self.server.close()
        self.rogue_server.close()


@pytest.fixture()
def env(tmp_path: Path):
    environment = SeedEnv(tmp_path)
    yield environment
    environment.close()


def _probe(result: str):
    return lambda origin: result


def test_cleartext_case_passes_when_boundary_refuses_cleartext(env: SeedEnv) -> None:
    adapter = env.adapter()
    document = execute_case(
        "cleartext_or_redirect_bootstrap",
        origin=ORIGIN,
        evidence_root=None,
        adapter=adapter,
        case_inputs={"probe": _probe("cleartext_refused")},
    )
    validate_internet_native_qualification(document)
    assert document["executed"] is True
    assert document["result"] == "passed"
    outcomes = document["public_projection"]["outcomes"]
    assert "cleartext_refused" in outcomes
    assert "bounded_public_error" in outcomes
    assert "invite_secret_never_transmitted" in outcomes


def test_cleartext_case_fails_when_identity_exposed_over_plaintext(env: SeedEnv) -> None:
    adapter = env.adapter()
    document = execute_case(
        "cleartext_or_redirect_bootstrap",
        origin=ORIGIN,
        evidence_root=None,
        adapter=adapter,
        case_inputs={"probe": _probe("cleartext_identity_exposed")},
    )
    validate_internet_native_qualification(document)
    assert document["executed"] is True
    assert document["result"] == "failed"
    assert "cleartext_identity_exposed" in document["public_projection"]["outcomes"]


def test_cleartext_case_requires_a_live_boundary(env: SeedEnv) -> None:
    unreachable = env.adapter(unreachable=True)
    with pytest.raises(PhysicalGateError) as exc_info:
        execute_case(
            "cleartext_or_redirect_bootstrap",
            origin=ORIGIN,
            evidence_root=None,
            adapter=unreachable,
            case_inputs={"probe": _probe("cleartext_refused")},
        )
    assert exc_info.value.code == "physical_infrastructure_unavailable"


def test_certificate_case_passes_on_pin_mismatch(env: SeedEnv) -> None:
    rogue_adapter = env.adapter(rogue=True)
    document = execute_case(
        "certificate_without_seed_authority",
        origin=ORIGIN,
        evidence_root=None,
        adapter=rogue_adapter,
    )
    validate_internet_native_qualification(document)
    assert document["executed"] is True
    assert document["result"] == "passed"
    outcomes = document["public_projection"]["outcomes"]
    assert "seed_pin_mismatch_before_invite_transmission" in outcomes
    assert "join_not_attempted" in outcomes
    assert "invite_secret_never_transmitted" in outcomes


def test_certificate_case_fails_when_pin_matches(env: SeedEnv) -> None:
    adapter = env.adapter()
    document = execute_case(
        "certificate_without_seed_authority",
        origin=ORIGIN,
        evidence_root=None,
        adapter=adapter,
    )
    validate_internet_native_qualification(document)
    assert document["executed"] is True
    assert document["result"] == "failed"


def test_replay_case_rejects_changed_retry_and_stays_idempotent(env: SeedEnv) -> None:
    first_adapter = env.adapter()
    second_adapter = env.adapter()
    node_a = env.node("replay-node")
    first_envelope = node_a.join_request(
        invite_nonce="a8-invite",
        endpoint_addrs=["https://replay-node-a/control"],
    )
    first_adapter.preflight(now=time.time())
    first_adapter.join(first_envelope, now=time.time())
    node_b = env.node("replay-node")
    second_envelope = node_b.join_request(
        invite_nonce="a8-invite",
        endpoint_addrs=["https://replay-node-b/control"],
    )
    document = execute_case(
        "invalid_or_replayed_invitation",
        origin=ORIGIN,
        evidence_root=None,
        adapter=first_adapter,
        case_inputs={
            "first_join_envelope": first_envelope,
            "second_join_envelope": second_envelope,
            "second_adapter": second_adapter,
        },
    )
    validate_internet_native_qualification(document)
    assert document["executed"] is True
    assert document["result"] == "passed"
    outcomes = document["public_projection"]["outcomes"]
    assert "exact_retry_idempotent" in outcomes
    assert "changed_retry_rejected" in outcomes
    members = env.coordinator.members()
    assert len(members) == 1


def test_probe_returns_refused_for_unreachable_origin() -> None:
    assert probe_bootstrap_over_cleartext(
        "https://seed.example.invalid", timeout=2.0
    ) == "cleartext_refused"
