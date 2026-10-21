# SPDX-License-Identifier: AGPL-3.0-only
from __future__ import annotations

import copy
import json
from pathlib import Path
import sqlite3

import pytest

from mycelium_a13_macos.authority import DemoAuthority, DemoAuthorityError
from mycelium_a13_macos.pairing import decrypt_invitation_handoff
from mycelium_node.durable_membership import load_membership_state
from mycelium_seed import SeedCoordinator
from mycelium_seed.http import SeedHTTPClient, SeedHTTPError

CONSENT = {
    "cooperative_peer_visibility_acknowledged": True,
    "resource_use_acknowledged": True,
    "contribution_enabled": False,
}
QUOTA = {
    "storage_bytes": 2_000_000_000,
    "network_bytes_per_day": 5_000_000_000,
    "background_allowed": False,
}
STATUS_FIELDS = {
    "protocol",
    "started",
    "swarm_created",
    "local_tls",
    "membership_state",
    "member_count",
    "capability_state",
    "capability",
    "membership_generation",
    "lease_state",
    "contribution_enabled",
    "route_ready",
}


def _started(tmp_path: Path) -> DemoAuthority:
    root = tmp_path / "authority"
    value = DemoAuthority(root)
    value.start()
    value.create_swarm(CONSENT)
    return value


def _enroll(authority: DemoAuthority):
    request = authority.pair_request()
    handoff = authority.approve_pairing(request, ttl_seconds=120, quota=QUOTA)
    accepted = authority.accept_pairing(handoff)
    return request, handoff, accepted


def test_fresh_swarm_creates_durable_bound_authority_and_target_before_mint(
    tmp_path: Path,
) -> None:
    authority = DemoAuthority(tmp_path / "authority")
    assert authority.start() == {
        "protocol": "mycelium.demo_authority_status.v1",
        "started": True,
        "swarm_created": False,
        "local_tls": "inactive",
        "membership_state": "absent",
        "member_count": 0,
        "capability_state": "not_reported",
        "capability": None,
        "membership_generation": None,
        "lease_state": "none",
        "contribution_enabled": False,
        "route_ready": False,
    }
    status = authority.create_swarm(CONSENT)
    assert set(status) == STATUS_FIELDS
    assert status["swarm_created"] is True
    assert status["local_tls"] == "pinned_loopback"
    assert status["membership_state"] == "unenrolled"
    assert status["route_ready"] is False
    assert isinstance(authority.coordinator, SeedCoordinator)
    assert authority.member is None
    assert authority.seed_server is not None
    assert authority.seed_server.canonical_server.base_url.startswith("http://")
    assert (authority.state_root / "seed" / "identity" / "seed.key").is_file()
    target_key = authority.state_root / "target" / "identity" / "node.key"
    assert target_key.is_file()
    assert target_key.stat().st_mode & 0o777 == 0o600
    binding = authority.coordinator._state.identity_binding()
    assert authority.coordinator.signer.verification_key_digest == binding["seed_key_digest"]
    authority.stop()


@pytest.mark.parametrize(
    "consent",
    [
        {},
        {**CONSENT, "extra": True},
        {**CONSENT, "cooperative_peer_visibility_acknowledged": False},
        {**CONSENT, "resource_use_acknowledged": False},
    ],
)
def test_swarm_requires_closed_explicit_consent_without_partial_state(
    tmp_path: Path, consent: dict
) -> None:
    authority = DemoAuthority(tmp_path / "authority")
    authority.start()
    with pytest.raises(DemoAuthorityError, match="authority_consent_invalid"):
        authority.create_swarm(consent)
    assert not (authority.state_root / "seed" / "state.sqlite3").exists()
    authority.stop()


def test_pairing_uses_real_https_join_sqlite_and_atomic_private_acceptance(
    tmp_path: Path,
) -> None:
    authority = _started(tmp_path)
    request = authority.pair_request()
    target_key = authority.state_root / "target" / "identity" / "node.key"
    target_key_bytes = target_key.read_bytes()
    assert authority.pair_request() == request
    assert target_key.read_bytes() == target_key_bytes

    handoff = authority.approve_pairing(request, ttl_seconds=120, quota=QUOTA)
    assert authority.approve_pairing(request, ttl_seconds=120, quota=QUOTA) == handoff
    with pytest.raises(DemoAuthorityError, match="pairing_approval_changed"):
        authority.approve_pairing(
            request,
            ttl_seconds=120,
            quota={**QUOTA, "background_allowed": True},
        )

    accepted = authority.accept_pairing(handoff)
    assert accepted == {
        "protocol": "mycelium.demo_membership_result.v1",
        "membership_state": "current",
        "membership_generation": 1,
        "lifecycle_state": "RUNNING",
        "route_ready": False,
    }
    assert authority.accept_pairing(handoff) == accepted
    changed = copy.deepcopy(handoff)
    changed["ciphertext"] = ("B" if changed["ciphertext"][0] != "B" else "C") + changed["ciphertext"][1:]
    with pytest.raises(DemoAuthorityError, match="pairing_handoff_changed"):
        authority.accept_pairing(changed)

    assert isinstance(authority.member, object)
    assert isinstance(authority.seed_client, SeedHTTPClient)
    node_id = request["node_id"]
    canonical_member = authority.coordinator.member(node_id)
    assert canonical_member["generation"] == 1
    assert canonical_member["lifecycle_state"] == "RUNNING"
    membership = load_membership_state(authority.state_root / "target")
    assert membership is not None and membership["membership_generation"] == 1
    with sqlite3.connect(authority.state_root / "seed" / "state.sqlite3") as database:
        row = database.execute(
            "SELECT generation, lifecycle_state FROM seed_members WHERE node_id = ?",
            (node_id,),
        ).fetchone()
    assert row == (1, "RUNNING")
    assert not (authority.state_root / "target" / "pairing.pending.json").exists()
    assert not (authority.state_root / "target" / "pairing.handoff.json").exists()
    authority.stop()


def test_tampered_handoff_leaves_only_private_encrypted_recovery_and_no_member(
    tmp_path: Path,
) -> None:
    authority = _started(tmp_path)
    request = authority.pair_request()
    handoff = authority.approve_pairing(request, ttl_seconds=120, quota=QUOTA)
    private_key = authority._load_pending_pairing()["recipient_private_key"]
    plaintext = decrypt_invitation_handoff(
        request=request,
        handoff=handoff,
        recipient_private_key=private_key,
        now=authority._clock(),
    )
    token = plaintext["invite_bundle"]["token"]
    changed = copy.deepcopy(handoff)
    changed["ciphertext"] = ("B" if changed["ciphertext"][0] != "B" else "C") + changed["ciphertext"][1:]
    with pytest.raises(DemoAuthorityError, match="pairing_handoff_invalid"):
        authority.accept_pairing(changed)

    recovery = authority.state_root / "target" / "pairing.handoff.json"
    assert recovery.is_file() and recovery.stat().st_mode & 0o777 == 0o600
    assert token.encode() not in recovery.read_bytes()
    assert authority.coordinator.members() == ()
    retained = b"\n".join(
        path.read_bytes()
        for path in authority.state_root.rglob("*")
        if path.is_file()
    )
    assert token.encode() not in retained
    authority.stop()


def test_restart_resumes_same_identities_then_renew_preserves_generation(
    tmp_path: Path,
) -> None:
    root = tmp_path / "authority"
    first = DemoAuthority(root)
    first.start()
    first.create_swarm(CONSENT)
    request, _handoff, accepted = _enroll(first)
    node_key = (root / "target" / "identity" / "node.key").read_bytes()
    seed_key = (root / "seed" / "identity" / "seed.key").read_bytes()
    tls_key = (root / "tls" / "server.key").read_bytes()
    first.stop()

    second = DemoAuthority(root)
    restarted = second.start()
    assert restarted["membership_state"] == "current"
    assert restarted["membership_generation"] == accepted["membership_generation"] + 1
    assert (root / "target" / "identity" / "node.key").read_bytes() == node_key
    assert (root / "seed" / "identity" / "seed.key").read_bytes() == seed_key
    assert (root / "tls" / "server.key").read_bytes() == tls_key
    generation = restarted["membership_generation"]
    renewed = second.renew()
    assert renewed["membership_generation"] == generation
    assert second.coordinator.member(request["node_id"])["last_heartbeat_sequence"] >= 1
    second.stop()
    assert root.is_dir()
    assert load_membership_state(root / "target")["membership_generation"] == generation


def test_revoke_fences_renew_and_restart_without_identity_regeneration(
    tmp_path: Path,
) -> None:
    root = tmp_path / "authority"
    authority = DemoAuthority(root)
    authority.start()
    authority.create_swarm(CONSENT)
    request, _handoff, accepted = _enroll(authority)
    revoked = authority.revoke(request["node_id"], accepted["membership_generation"])
    assert revoked == {
        "protocol": "mycelium.demo_revocation_result.v1",
        "previous_generation": 1,
        "generation": 2,
        "lifecycle_state": "STOPPED",
        "route_ready": False,
    }
    with pytest.raises((DemoAuthorityError, SeedHTTPError), match="stale|revoked"):
        authority.renew()
    node_key = (root / "target" / "identity" / "node.key").read_bytes()
    authority.stop()

    restarted = DemoAuthority(root)
    status = restarted.start()
    assert status["membership_state"] == "revoked"
    assert status["membership_generation"] == 2
    assert restarted.member is None
    assert (root / "target" / "identity" / "node.key").read_bytes() == node_key
    with pytest.raises(DemoAuthorityError, match="authority_member_revoked"):
        restarted.renew()
    restarted.stop()


def test_status_is_closed_and_contains_no_private_material(tmp_path: Path) -> None:
    authority = _started(tmp_path)
    request, handoff, _accepted = _enroll(authority)
    status = authority.status()
    rendered = json.dumps(status, sort_keys=True)
    assert set(status) == STATUS_FIELDS
    forbidden = [
        str(authority.state_root),
        request["node_id"],
        request["pairing_nonce"],
        request["recipient_encryption_key"],
        handoff["ciphertext"],
        authority.coordinator.signer.verification_key_digest,
        authority.seed_server.base_url,
    ]
    assert all(value not in rendered for value in forbidden)
    authority.stop()
