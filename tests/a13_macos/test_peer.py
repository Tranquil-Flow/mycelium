# SPDX-License-Identifier: AGPL-3.0-only
from __future__ import annotations

import base64
import copy
import json
from pathlib import Path
import sqlite3
import stat
import time

import pytest

from mycelium_a13_macos.authority import DemoAuthority, _atomic_private_json
from mycelium_a13_macos.pairing import decrypt_invitation_handoff
from mycelium_a13_macos.peer import RemoteMacMember, RemoteMacMemberError
from mycelium_a13_macos.tls import pinned_seed_client, pinned_seed_client_from_invite
from mycelium_membership import CAPABILITY_REPORT_PROTOCOL
from mycelium_node import NodeMembershipSession
from mycelium_node.durable_membership import load_membership_state
from mycelium_node.identity import load_node_signer
from mycelium_seed.http import SeedHTTPError


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
    "identity_state",
    "pairing_state",
    "membership_state",
    "membership_generation",
    "lease_state",
    "cooperative_peer_visibility_acknowledged",
    "resource_use_acknowledged",
    "contribution_enabled",
    "capability_state",
    "serving_state",
    "route_ready",
}


class _DroppingClient:
    def __init__(self, client, method: str, dropped: list[bool]) -> None:
        self._client = client
        self._method = method
        self._dropped = dropped

    def __getattr__(self, name):
        attribute = getattr(self._client, name)
        if name != self._method:
            return attribute

        def drop_after_commit(*args, **kwargs):
            result = attribute(*args, **kwargs)
            if not self._dropped:
                self._dropped.append(True)
                raise SeedHTTPError("seed_http_unreachable")
            return result

        return drop_after_commit


@pytest.fixture
def seed(tmp_path: Path):
    authority = DemoAuthority(tmp_path / "seed-owner")
    authority.start()
    authority.create_swarm(CONSENT)
    assert authority.seed_server is not None
    ca = authority.seed_server.material.ca_certificate

    def factory(*, bundle=None, membership=None, now, timeout):
        assert (bundle is None) != (membership is None)
        if bundle is not None:
            return pinned_seed_client_from_invite(
                bundle,
                now=now,
                ca_certificate=ca,
                timeout=timeout,
            )
        return pinned_seed_client(
            seed_url=membership["seed_url"],
            swarm_id=membership["swarm_id"],
            seed_key_digest=membership["seed_key_digest"],
            seed_key_records=list(membership["seed_key_records"]),
            ca_certificate=ca,
            timeout=timeout,
        )

    try:
        yield authority, factory
    finally:
        authority.stop()


def _member(root: Path, factory, *, clock=time.time) -> RemoteMacMember:
    return RemoteMacMember(root, clock=clock, _test_seed_client_factory=factory)


def _approve(authority: DemoAuthority, member: RemoteMacMember):
    request = member.pair_request(CONSENT, ttl_seconds=300)
    handoff = authority.approve_pairing(request, ttl_seconds=120, quota=QUOTA)
    return request, handoff


def _enroll(authority: DemoAuthority, member: RemoteMacMember):
    request, handoff = _approve(authority, member)
    return request, handoff, member.accept_handoff(handoff)


def _assert_private_tree(root: Path) -> None:
    for path in root.rglob("*"):
        mode = stat.S_IMODE(path.lstat().st_mode)
        assert mode == (0o700 if path.is_dir() else 0o600), path


def test_identity_precedes_request_and_request_retry_survives_restart(
    tmp_path: Path, seed
) -> None:
    _authority, factory = seed
    root = tmp_path / "remote-target"
    first = _member(root, factory)
    initial = first.start()
    assert set(initial) == STATUS_FIELDS
    assert initial["identity_state"] == "present"
    assert initial["membership_state"] == "absent"
    assert initial["serving_state"] == "not_yet_serving"
    key_path = root / "identity" / "node.key"
    key_bytes = key_path.read_bytes()

    with pytest.raises(RemoteMacMemberError, match="peer_consent_invalid"):
        first.pair_request(
            {**CONSENT, "cooperative_peer_visibility_acknowledged": False}
        )
    assert not (root / "pairing.pending.json").exists()

    request = first.pair_request(CONSENT, ttl_seconds=300)
    assert first.pair_request(CONSENT, ttl_seconds=300) == request
    first.stop()

    second = _member(root, factory)
    second.start()
    assert second.pair_request(CONSENT, ttl_seconds=300) == request
    assert key_path.read_bytes() == key_bytes
    assert not any(path.suffix in {".sqlite", ".sqlite3", ".db"} for path in root.rglob("*"))
    assert not (root / "seed").exists()
    _assert_private_tree(root)
    second.stop()


def test_two_independent_targets_join_real_https_sqlite_and_remain_not_serving(
    tmp_path: Path, seed
) -> None:
    authority, factory = seed
    left = _member(tmp_path / "left-target", factory)
    right = _member(tmp_path / "right-target", factory)
    left.start()
    right.start()
    left_request, left_handoff, left_result = _enroll(authority, left)
    right_request, _right_handoff, right_result = _enroll(authority, right)

    assert left_request["identity_key_digest"] != right_request["identity_key_digest"]
    assert left_request["node_id"] != right_request["node_id"]
    assert left_result["membership_generation"] == 1
    assert right_result["membership_generation"] == 1
    assert {member["node_id"] for member in authority.coordinator.members()} == {
        left_request["node_id"],
        right_request["node_id"],
    }
    with sqlite3.connect(authority.state_root / "seed" / "state.sqlite3") as database:
        rows = database.execute(
            "SELECT node_id, generation FROM seed_members ORDER BY node_id"
        ).fetchall()
    assert rows == sorted(
        [(left_request["node_id"], 1), (right_request["node_id"], 1)]
    )
    for target in (left, right):
        status = target.status()
        assert status["membership_state"] == "current"
        assert status["serving_state"] == "not_yet_serving"
        assert status["route_ready"] is False
        assert not (target.state_root / "seed").exists()
        assert not any(
            path.suffix in {".sqlite", ".sqlite3", ".db"}
            for path in target.state_root.rglob("*")
        )
        _assert_private_tree(target.state_root)

    rendered = json.dumps(left.status(), sort_keys=True)
    assert all(
        secret not in rendered
        for secret in (
            left_request["node_id"],
            left_request["identity_key_digest"],
            left_request["recipient_encryption_key"],
            left_request["pairing_nonce"],
            left_handoff["ciphertext"],
            authority.seed_server.base_url,
        )
    )
    left.stop()
    right.stop()


def test_tamper_and_wrong_target_fail_without_partial_membership(
    tmp_path: Path, seed
) -> None:
    authority, factory = seed
    intended = _member(tmp_path / "intended", factory)
    wrong = _member(tmp_path / "wrong", factory)
    intended.start()
    wrong.start()
    intended_request, handoff = _approve(authority, intended)
    wrong.pair_request(CONSENT)
    pending = json.loads((intended.state_root / "pairing.pending.json").read_text())
    recipient_private = base64.urlsafe_b64decode(
        pending["recipient_private_key"]
        + "=" * (-len(pending["recipient_private_key"]) % 4)
    )
    invite_token = decrypt_invitation_handoff(
        request=intended_request,
        handoff=handoff,
        recipient_private_key=recipient_private,
        now=time.time(),
    )["invite_bundle"]["token"]

    changed = copy.deepcopy(handoff)
    changed["ciphertext"] = ("A" if changed["ciphertext"][0] != "A" else "B") + changed[
        "ciphertext"
    ][1:]
    with pytest.raises(RemoteMacMemberError, match="pairing_handoff_invalid"):
        intended.accept_handoff(changed)
    with pytest.raises(
        RemoteMacMemberError, match="pairing_handoff_request_mismatch"
    ):
        wrong.accept_handoff(handoff)

    assert authority.coordinator.members() == ()
    assert intended.status()["membership_state"] == "absent"
    assert wrong.status()["membership_state"] == "absent"
    assert (intended.state_root / "pairing.handoff.json").is_file()
    retained = b"\n".join(
        path.read_bytes() for path in intended.state_root.rglob("*") if path.is_file()
    )
    assert intended_request["pairing_nonce"].encode() in retained
    assert invite_token.encode() not in retained
    intended.stop()
    wrong.stop()


def test_default_transport_does_not_trust_the_local_test_ca(
    tmp_path: Path, seed
) -> None:
    authority, factory = seed
    root = tmp_path / "target"
    production_default = RemoteMacMember(root, timeout=2)
    production_default.start()
    request, handoff = _approve(authority, production_default)
    with pytest.raises(RemoteMacMemberError, match="peer_transport_unreachable"):
        production_default.accept_handoff(handoff)
    assert authority.coordinator.members() == ()
    production_default.stop()

    explicitly_test_only = _member(root, factory)
    explicitly_test_only.start()
    assert explicitly_test_only.accept_handoff(handoff)["membership_generation"] == 1
    assert authority.coordinator.member(request["node_id"])["generation"] == 1
    explicitly_test_only.stop()


def test_join_response_loss_replays_exact_checkpoint_after_restart(
    tmp_path: Path, seed
) -> None:
    authority, factory = seed
    dropped: list[bool] = []

    def drop_join_factory(**kwargs):
        return _DroppingClient(factory(**kwargs), "join", dropped)

    root = tmp_path / "target"
    first = _member(root, drop_join_factory)
    first.start()
    request, handoff = _approve(authority, first)
    with pytest.raises(RemoteMacMemberError, match="peer_transport_unreachable"):
        first.accept_handoff(handoff)
    checkpoint_document = json.loads((root / "join.pending.json").read_text())
    pending_document = json.loads((root / "pairing.pending.json").read_text())
    handoff_document = json.loads((root / "pairing.handoff.json").read_text())
    assert authority.coordinator.member(request["node_id"])["generation"] == 1
    assert load_membership_state(root) is None
    first.stop()

    retry = _member(root, factory)
    retry.start()
    accepted = retry.accept_handoff(handoff)
    assert accepted["membership_generation"] == 1
    assert authority.coordinator.member(request["node_id"])["generation"] == 1
    assert not (root / "join.pending.json").exists()
    retry.stop()

    # Simulate a process death after accepted.json was durably replaced but
    # before obsolete recipient recovery files were unlinked.
    _atomic_private_json(root / "pairing.pending.json", pending_document)
    _atomic_private_json(root / "pairing.handoff.json", handoff_document)
    _atomic_private_json(root / "join.pending.json", checkpoint_document)
    recovered = _member(root, factory)
    assert recovered.start()["membership_generation"] == 2
    assert not (root / "pairing.pending.json").exists()
    assert not (root / "pairing.handoff.json").exists()
    assert not (root / "join.pending.json").exists()
    recovered.stop()


def test_resume_response_loss_replays_exact_generation_then_next_restart_advances_once(
    tmp_path: Path, seed
) -> None:
    authority, factory = seed
    root = tmp_path / "target"
    enrolled = _member(root, factory)
    enrolled.start()
    request, _handoff, _accepted = _enroll(authority, enrolled)
    enrolled.stop()

    dropped: list[bool] = []

    def drop_resume_factory(**kwargs):
        return _DroppingClient(factory(**kwargs), "resume", dropped)

    failed = _member(root, drop_resume_factory)
    with pytest.raises(RemoteMacMemberError, match="peer_transport_unreachable"):
        failed.start()
    pending = (root / "resume.pending.json").read_bytes()
    assert load_membership_state(root)["membership_generation"] == 1
    assert authority.coordinator.member(request["node_id"])["generation"] == 2

    retried = _member(root, factory)
    status = retried.start()
    assert status["membership_generation"] == 2
    assert load_membership_state(root)["restart_count"] == 1
    assert not (root / "resume.pending.json").exists()
    assert pending
    retried.stop()

    restarted = _member(root, factory)
    assert restarted.start()["membership_generation"] == 3
    assert load_membership_state(root)["restart_count"] == 2
    restarted.stop()


def test_renew_capability_expiry_and_stop_retain_identity_and_evidence(
    tmp_path: Path, seed
) -> None:
    authority, factory = seed
    offset = [0.0]
    clock = lambda: time.time() + offset[0]
    root = tmp_path / "target"
    target = _member(root, factory, clock=clock)
    target.start()
    _request, _handoff, accepted = _enroll(authority, target)
    generation = accepted["membership_generation"]

    assert target.renew()["membership_generation"] == generation
    capability = target.capability_report(
        platform="macos",
        architecture="arm64",
        memory_bytes=16 * 1024**3,
        available_storage_bytes=100 * 1024**3,
        backends=["mlx"],
        precisions=["float32"],
    )
    assert capability["accepted"] is True
    assert capability["report"]["message"]["protocol"] == CAPABILITY_REPORT_PROTOCOL
    assert target.status()["capability_state"] == "reported"

    identity = (root / "identity" / "node.key").read_bytes()
    accepted_evidence = (root / "accepted.json").read_bytes()
    stopped = target.stop()
    assert stopped["started"] is False
    assert stopped["membership_state"] == "retained"
    assert stopped["serving_state"] == "not_yet_serving"
    assert (root / "identity" / "node.key").read_bytes() == identity
    assert (root / "accepted.json").read_bytes() == accepted_evidence

    resumed = _member(root, factory, clock=clock)
    resumed.start()
    offset[0] = 10_000.0
    with pytest.raises(RemoteMacMemberError, match="peer_member_expired"):
        resumed.renew()
    assert resumed.status()["membership_state"] == "expired"
    resumed.stop()


def test_revoked_stale_and_unknown_members_fail_closed_over_https(
    tmp_path: Path, seed
) -> None:
    authority, factory = seed

    revoked_root = tmp_path / "revoked"
    revoked = _member(revoked_root, factory)
    revoked.start()
    revoked_request, _handoff, revoked_result = _enroll(authority, revoked)
    authority.coordinator.advance_member_generation(
        node_id=revoked_request["node_id"],
        expected_generation=revoked_result["membership_generation"],
        lifecycle_state="STOPPED",
    )
    revoked.stop()
    rejected = _member(revoked_root, factory)
    with pytest.raises(RemoteMacMemberError, match="peer_member_revoked"):
        rejected.start()
    assert rejected.status()["membership_state"] == "revoked"

    stale_root = tmp_path / "stale"
    stale = _member(stale_root, factory)
    stale.start()
    stale_request, _handoff, _result = _enroll(authority, stale)
    state = load_membership_state(stale_root)
    signer = load_node_signer(
        stale_root / "identity" / "node.key", endpoint_id=state["endpoint_id"]
    )
    other = NodeMembershipSession(
        node_id=state["node_id"],
        swarm_id=state["swarm_id"],
        seed_node_id=state["seed_node_id"],
        signer=signer,
        incarnation="alternate-restart",
        software_version="mycelium-a13-macos-demo/1",
        peer_class="mac_mlx_iroh",
        runtime_capability={
            "runtime_backend": "mlx",
            "transport": "iroh",
            "activation_protocol": "mycelium.router_wire.v1",
        },
    )
    resume = other.resume_request(
        previous_generation=state["membership_generation"],
        previous_incarnation=state["incarnation"],
        endpoint_addrs=[f"https://{state['endpoint_id']}.invalid/control"],
    )
    client = factory(bundle=None, membership=state, now=time.time(), timeout=2)
    client.identity(now=time.time() + 1)
    acceptance = client.resume(resume_envelope=resume)
    other.accept_resume(acceptance, seed_key_digest=state["seed_key_digest"])
    assert authority.coordinator.member(stale_request["node_id"])["generation"] == 2
    stale.stop()
    stale_retry = _member(stale_root, factory)
    with pytest.raises(RemoteMacMemberError, match="peer_generation_stale"):
        stale_retry.start()
    assert stale_retry.status()["membership_state"] == "stale"

    unknown_root = tmp_path / "unknown"
    unknown = _member(unknown_root, factory)
    unknown.start()
    unknown_request, _unknown_handoff, _unknown_result = _enroll(authority, unknown)
    unknown.stop()
    authority.coordinator._members.pop(unknown_request["node_id"])  # noqa: SLF001 - fault injection
    missing = _member(unknown_root, factory)
    with pytest.raises(RemoteMacMemberError, match="peer_member_unknown"):
        missing.start()
    assert missing.status()["membership_state"] == "unknown"


def test_status_never_projects_request_handoff_invite_or_private_paths(
    tmp_path: Path, seed
) -> None:
    authority, factory = seed
    target = _member(tmp_path / "target", factory)
    target.start()
    request, handoff = _approve(authority, target)
    pending = json.loads((target.state_root / "pairing.pending.json").read_text())
    recipient_private = base64.urlsafe_b64decode(
        pending["recipient_private_key"] + "=" * (-len(pending["recipient_private_key"]) % 4)
    )
    plaintext = decrypt_invitation_handoff(
        request=request,
        handoff=handoff,
        recipient_private_key=recipient_private,
        now=time.time(),
    )
    invite_token = plaintext["invite_bundle"]["token"]
    target.accept_handoff(handoff)
    rendered = json.dumps(target.status(), sort_keys=True)
    assert set(target.status()) == STATUS_FIELDS
    assert all(
        value not in rendered
        for value in (
            str(target.state_root),
            request["node_id"],
            request["identity_key_digest"],
            request["recipient_encryption_key"],
            request["pairing_nonce"],
            handoff["ciphertext"],
            invite_token,
            authority.seed_server.base_url,
        )
    )
    target.stop()
