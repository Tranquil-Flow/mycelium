# SPDX-License-Identifier: AGPL-3.0-only
"""A13 Linux target-side invitation onboarding (SOURCE tranche).

The Linux target speaks the exact wireless-shape protocols the committed Mac
slice froze (``mycelium.pairing_request.v1`` /
``mycelium.encrypted_invitation_handoff.v1``).  These tests assert that overlap
structurally, and they pin the (now closed) cross-lane gap: the Mac owner
module's closed peer-class allow-set names ``linux_native_iroh`` explicitly, so
owner-side approval for a Linux target is served by the Mac module end to end,
while an unlisted peer class remains refused (negative control).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from mycelium_a13_linux.onboarding import (
    ENCRYPTED_HANDOFF_PROTOCOL,
    LINUX_PEER_CLASS,
    PAIRING_REQUEST_PROTOCOL,
    OnboardingError,
    approve_linux_pairing_request,
    complete_local_join,
    decrypt_linux_invitation_handoff,
    new_linux_pairing_request,
    validate_linux_quota,
    verify_linux_pairing_request,
)
from mycelium_a13_macos import pairing as mac_pairing
from mycelium_invite import mint_invite_bundle
from mycelium_qualification.evidence import canonical_json_bytes
from mycelium_qualification.signing import generate_ed25519_signer

NOW = 2_000_000_000.0
NONCE = "pair-linux-node2-1"
QUOTA = {
    "storage_bytes": 2_000_000_000,
    "network_bytes_per_day": 5_000_000_000,
    "background_allowed": False,
}


def _case(*, nonce: str = NONCE, node_id: str = "mycelium-node2", ttl_seconds: int = 300):
    target = generate_ed25519_signer(endpoint_id="target-linux-endpoint")
    seed = generate_ed25519_signer(endpoint_id="seed-endpoint")
    request, private_key = new_linux_pairing_request(
        signer=target, node_id=node_id, now=NOW, ttl_seconds=ttl_seconds, nonce=nonce
    )
    bundle = mint_invite_bundle(
        signer=seed,
        swarm_id="judge-swarm",
        seed_url="https://127.0.0.1:4443",
        ttl_seconds=120,
        nonce=nonce,
        issued_at=NOW,
    )
    handoff = approve_linux_pairing_request(
        request=request, invite_bundle=bundle, quota=QUOTA, now=NOW
    )
    return {
        "target": target,
        "seed": seed,
        "request": request,
        "private_key": private_key,
        "bundle": bundle,
        "handoff": handoff,
    }


def _state_root(tmp_path: Path) -> Path:
    root = tmp_path / "state"
    root.mkdir(mode=0o700)
    return root


def _identity(root: Path) -> Path:
    path = root / "identity.key"
    path.write_bytes(b"\x11" * 32)
    path.chmod(0o600)
    return path


def _join(case, root: Path, identity: Path, **overrides):
    arguments = {
        "state_root": root,
        "identity_path": identity,
        "request": case["request"],
        "handoff": case["handoff"],
        "recipient_private_key": case["private_key"],
        "seed_key_digest_pin": case["seed"].verification_key_digest,
        "member_generation": 1,
        "now": NOW + 5,
    }
    arguments.update(overrides)
    return complete_local_join(**arguments)


def _code(call):
    with pytest.raises(OnboardingError) as caught:
        call()
    return caught.value.code


def test_linux_target_speaks_the_exact_protocols_the_mac_slice_froze() -> None:
    assert PAIRING_REQUEST_PROTOCOL == mac_pairing.PAIRING_REQUEST_PROTOCOL
    assert ENCRYPTED_HANDOFF_PROTOCOL == mac_pairing.ENCRYPTED_HANDOFF_PROTOCOL
    assert PAIRING_REQUEST_PROTOCOL == "mycelium.pairing_request.v1"
    assert ENCRYPTED_HANDOFF_PROTOCOL == "mycelium.encrypted_invitation_handoff.v1"
    assert LINUX_PEER_CLASS == "linux_native_iroh"


def test_request_and_handoff_wire_shapes_match_the_mac_slice_field_for_field() -> None:
    case = _case()
    mac_signer = generate_ed25519_signer(endpoint_id="judge-mac")
    mac_request, _ = mac_pairing.new_pairing_request(
        signer=mac_signer,
        node_id="judge-mac",
        peer_class="mac_mlx_iroh",
        now=NOW,
        ttl_seconds=300,
        nonce=NONCE,
    )
    mac_handoff = mac_pairing.approve_pairing_request(
        request=mac_request,
        invite_bundle=case["bundle"],
        quota=QUOTA,
        now=NOW,
    )
    assert set(case["request"]) == set(mac_request)
    assert set(case["handoff"]) == set(mac_handoff)


def test_mac_owner_module_now_admits_a_linux_peer_class_request() -> None:
    """Cross-lane gap closed: the owner module admits the Linux peer class.

    A request minted by the Linux target verifies through the Mac owner
    module, the Mac owner approves it, and only the Linux target's recipient
    key decrypts the resulting handoff back to the bound invitation.  The Mac
    module's own ``new_pairing_request`` entry point admits the class through
    the same closed allow-set.
    """

    case = _case()
    verified = mac_pairing.verify_pairing_request(case["request"], now=NOW)
    assert verified["peer_class"] == LINUX_PEER_CLASS

    handoff = mac_pairing.approve_pairing_request(
        request=case["request"],
        invite_bundle=case["bundle"],
        quota=QUOTA,
        now=NOW,
    )
    payload = decrypt_linux_invitation_handoff(
        request=case["request"],
        handoff=handoff,
        recipient_private_key=case["private_key"],
        now=NOW + 1,
    )
    assert payload["peer_class"] == LINUX_PEER_CLASS
    assert payload["invite_bundle"] == case["bundle"]

    minted, _ = mac_pairing.new_pairing_request(
        signer=generate_ed25519_signer(endpoint_id="judge-mac"),
        node_id="judge-mac",
        peer_class=LINUX_PEER_CLASS,
        now=NOW,
        ttl_seconds=300,
        nonce=NONCE,
    )
    assert minted["peer_class"] == LINUX_PEER_CLASS


def test_mac_owner_module_still_refuses_an_unlisted_peer_class() -> None:
    """Negative control: the allow-set stays closed for classes never listed.

    The Linux admission is an explicit allow-set addition, not a relaxed or
    removed check: a Linux-looking class nobody declared is still refused on
    both the minting and the verifying path.
    """

    signer = generate_ed25519_signer(endpoint_id="judge-mac")
    with pytest.raises(mac_pairing.PairingError) as minted:
        mac_pairing.new_pairing_request(
            signer=signer,
            node_id="judge-mac",
            peer_class="linux_future_unlisted_iroh",
            now=NOW,
            ttl_seconds=300,
            nonce=NONCE,
        )
    assert minted.value.code == "pairing_peer_class_unsupported"

    request, _ = mac_pairing.new_pairing_request(
        signer=signer,
        node_id="judge-mac",
        peer_class="mac_mlx_iroh",
        now=NOW,
        ttl_seconds=300,
        nonce=NONCE,
    )
    unsigned = {key: value for key, value in request.items() if key != "signature"}
    unsigned["peer_class"] = "linux_future_unlisted_iroh"
    forged = dict(unsigned, signature=signer.sign(unsigned))
    with pytest.raises(mac_pairing.PairingError) as verified:
        mac_pairing.verify_pairing_request(forged, now=NOW)
    assert verified.value.code == "pairing_request_invalid"


def test_linux_verifier_rejects_a_mac_peer_class_request() -> None:
    mac_request, _ = mac_pairing.new_pairing_request(
        signer=generate_ed25519_signer(endpoint_id="judge-mac"),
        node_id="judge-mac",
        peer_class="mac_mlx_iroh",
        now=NOW,
        ttl_seconds=300,
        nonce=NONCE,
    )
    assert _code(lambda: verify_linux_pairing_request(mac_request, now=NOW)) == (
        "onboarding_peer_class_unsupported"
    )


def test_pairing_request_contains_no_swarm_secret() -> None:
    case = _case()
    rendered = canonical_json_bytes(case["request"]).decode("utf-8").lower()
    # The request names the invitation PROTOCOL it accepts (a public constant)
    # but carries no swarm identifier, seed origin, or invitation material.
    for forbidden in ("swarm_id", "seed_url", "swarm_secret", "judge-swarm"):
        assert forbidden not in rendered, forbidden
    assert case["bundle"]["token"].split(".")[0].lower() not in rendered
    assert case["request"]["peer_class"] == LINUX_PEER_CLASS


def test_pairing_request_expiry_and_signature_are_verified() -> None:
    case = _case()
    assert _code(lambda: verify_linux_pairing_request(case["request"], now=NOW + 400)) == (
        "onboarding_request_expired"
    )
    tampered = {**case["request"], "node_id": "other-node"}
    assert _code(lambda: verify_linux_pairing_request(tampered, now=NOW)) == (
        "onboarding_request_signature_invalid"
    )


def test_handoff_carries_no_plaintext_invitation() -> None:
    case = _case()
    rendered = canonical_json_bytes(case["handoff"]).decode("utf-8").lower()
    for forbidden in ("invite", "token", "swarm_id", "seed_url", "judge-swarm"):
        assert forbidden not in rendered, forbidden
    assert case["bundle"]["token"].split(".")[0].lower() not in rendered


def test_recipient_only_decryption_recovers_the_bound_invitation() -> None:
    case = _case()
    payload = decrypt_linux_invitation_handoff(
        request=case["request"],
        handoff=case["handoff"],
        recipient_private_key=case["private_key"],
        now=NOW + 5,
    )
    assert payload["node_id"] == "mycelium-node2"
    assert payload["pairing_nonce"] == NONCE
    assert payload["seed_key_digest"] == case["seed"].verification_key_digest
    assert payload["peer_class"] == LINUX_PEER_CLASS
    assert payload["quota"] == QUOTA


def test_handoff_for_another_request_is_rejected() -> None:
    case = _case()
    other = _case(nonce="pair-linux-node2-2")
    assert _code(
        lambda: decrypt_linux_invitation_handoff(
            request=other["request"],
            handoff=case["handoff"],
            recipient_private_key=other["private_key"],
            now=NOW + 5,
        )
    ) == "onboarding_request_mismatch"


def test_another_private_key_cannot_decrypt_the_handoff() -> None:
    case = _case()
    stranger = _case(nonce="pair-linux-node2-3")
    assert _code(
        lambda: decrypt_linux_invitation_handoff(
            request=case["request"],
            handoff=case["handoff"],
            recipient_private_key=stranger["private_key"],
            now=NOW + 5,
        )
    ) == "onboarding_recipient_mismatch"


def test_tampered_ciphertext_is_rejected() -> None:
    case = _case()
    raw = case["handoff"]["ciphertext"]
    tampered = {**case["handoff"], "ciphertext": ("B" if raw[0] == "A" else "A") + raw[1:]}
    assert _code(
        lambda: decrypt_linux_invitation_handoff(
            request=case["request"],
            handoff=tampered,
            recipient_private_key=case["private_key"],
            now=NOW + 5,
        )
    ) == "onboarding_handoff_invalid"


def test_expired_handoff_is_rejected() -> None:
    case = _case()
    # The invitation lifetime is inside the request lifetime, so a clock that is
    # past the handoff expiry but still inside the request reports the handoff;
    # a clock past both reports the request.  Both refuse.
    assert _code(
        lambda: decrypt_linux_invitation_handoff(
            request=case["request"],
            handoff=case["handoff"],
            recipient_private_key=case["private_key"],
            now=NOW + 200,
        )
    ) == "onboarding_handoff_expired"
    assert _code(
        lambda: decrypt_linux_invitation_handoff(
            request=case["request"],
            handoff=case["handoff"],
            recipient_private_key=case["private_key"],
            now=NOW + 3600,
        )
    ) == "onboarding_request_expired"


def test_quota_shape_is_closed() -> None:
    assert validate_linux_quota(QUOTA) == QUOTA
    assert _code(lambda: validate_linux_quota({"storage_bytes": 1})) == (
        "onboarding_quota_invalid"
    )
    assert _code(
        lambda: approve_linux_pairing_request(
            request=_case()["request"],
            invite_bundle=_case()["bundle"],
            quota={"storage_bytes": -1, "network_bytes_per_day": 0, "background_allowed": False},
            now=NOW,
        )
    ) == "onboarding_quota_invalid"


def test_local_join_persists_a_private_member_and_erases_plaintext(tmp_path: Path) -> None:
    case = _case()
    root = _state_root(tmp_path)
    identity = _identity(root)
    plaintext = root / "recovery-invitation.txt"
    plaintext.write_text("plaintext handoff state", encoding="utf-8")
    before = identity.read_bytes()
    result = _join(case, root, identity, plaintext_handoff_file=plaintext)
    assert result["joined"] is True
    assert result["member_generation"] == 1
    assert result["eligibility"] == "member_only"
    assert result["route_ready"] is False
    assert result["plaintext_erased"] is True
    assert plaintext.exists() is False
    member = root / "onboarding" / "member.json"
    assert member.exists() and (member.stat().st_mode & 0o777) == 0o600
    text = member.read_text("utf-8")
    for forbidden in ("seed_url", "invite_bundle", "ciphertext", "127.0.0.1:4443", "pairing.key"):
        assert forbidden not in text, forbidden
    # The durable identity is never regenerated by a retry.
    assert identity.read_bytes() == before
    assert (identity.stat().st_mode & 0o777) == 0o600
    assert result["progress"]["current_rung"] == "signed_membership"
    assert result["progress"]["eligibility_granted"] is False


def test_replaying_the_same_handoff_is_refused_and_leaves_state_unchanged(
    tmp_path: Path,
) -> None:
    case = _case()
    root = _state_root(tmp_path)
    identity = _identity(root)
    first = _join(case, root, identity)
    member = root / "onboarding" / "member.json"
    before = member.read_bytes()
    assert _code(lambda: _join(case, root, identity)) == "onboarding_invitation_replayed"
    assert member.read_bytes() == before
    assert first["member_generation"] == 1
    assert json.loads(member.read_text("utf-8"))["member_generation"] == 1


def test_seed_pin_mismatch_fails_closed_without_a_partial_member(tmp_path: Path) -> None:
    case = _case()
    root = _state_root(tmp_path)
    identity = _identity(root)
    wrong_pin = generate_ed25519_signer(endpoint_id="impostor-seed").verification_key_digest
    assert _code(
        lambda: _join(case, root, identity, seed_key_digest_pin=wrong_pin)
    ) == "onboarding_seed_pin_mismatch"
    assert (root / "onboarding" / "member.json").exists() is False


def test_missing_identity_is_never_created_silently(tmp_path: Path) -> None:
    case = _case()
    root = _state_root(tmp_path)
    absent = root / "identity.key"
    assert _code(lambda: _join(case, root, absent)) == "onboarding_identity_missing"
    assert absent.exists() is False


def test_unsafe_identity_permissions_are_refused(tmp_path: Path) -> None:
    case = _case()
    root = _state_root(tmp_path)
    identity = _identity(root)
    identity.chmod(0o644)
    assert _code(lambda: _join(case, root, identity)) == "onboarding_identity_unsafe"
    assert (root / "onboarding" / "member.json").exists() is False


def test_unsafe_state_root_is_refused(tmp_path: Path) -> None:
    case = _case()
    root = _state_root(tmp_path)
    identity = _identity(root)
    open_root = tmp_path / "open"
    open_root.mkdir(mode=0o755)
    assert _code(lambda: _join(case, open_root, identity)) == (
        "onboarding_state_root_unsafe"
    )


def test_stale_member_generation_cannot_overwrite_a_newer_accepted_member(
    tmp_path: Path,
) -> None:
    case = _case()
    root = _state_root(tmp_path)
    identity = _identity(root)
    _join(case, root, identity, member_generation=4)
    second = _case(nonce="pair-linux-node2-9")
    assert _code(lambda: _join(second, root, identity, member_generation=4)) == (
        "onboarding_member_generation_stale"
    )
    third = _join(second, root, identity, member_generation=5)
    assert third["member_generation"] == 5


def test_plaintext_erasure_failure_is_reported_after_durable_acceptance(
    tmp_path: Path,
) -> None:
    case = _case()
    root = _state_root(tmp_path)
    identity = _identity(root)
    directory = root / "not-a-file"
    directory.mkdir()
    assert _code(
        lambda: _join(case, root, identity, plaintext_handoff_file=directory)
    ) == "onboarding_plaintext_erasure_failed"
    member = root / "onboarding" / "member.json"
    assert member.exists() is True
    assert json.loads(member.read_text("utf-8"))["member_generation"] == 1


def test_replay_ledger_fails_closed_when_full_instead_of_evicting(tmp_path: Path) -> None:
    case = _case()
    root = _state_root(tmp_path)
    identity = _identity(root)
    onboarding = root / "onboarding"
    onboarding.mkdir(mode=0o700)
    ledger = onboarding / "consumed-handoffs.json"
    ledger.write_text(
        json.dumps(
            {
                "protocol": "mycelium.a13_linux_replay_ledger.v1",
                "nonces": [f"pair-old-{index:03d}" for index in range(64)],
            }
        ),
        encoding="utf-8",
    )
    ledger.chmod(0o600)
    assert _code(lambda: _join(case, root, identity)) == "onboarding_replay_ledger_full"
    assert (onboarding / "member.json").exists() is False
