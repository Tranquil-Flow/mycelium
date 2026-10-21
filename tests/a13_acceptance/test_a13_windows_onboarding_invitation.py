# SPDX-License-Identifier: AGPL-3.0-only
"""A13 Windows target-side invitation onboarding (SOURCE tranche).

The Windows target speaks the exact protocols the committed Mac slice froze and
the landed Linux slice reuses (``mycelium.pairing_request.v1`` /
``mycelium.encrypted_invitation_handoff.v1``).  These tests pin that structural
overlap and pin the (now closed) cross-lane gap: the Mac owner module's
peer-class allow-set now names ``windows_native_iroh`` explicitly alongside its
own ``mac_mlx_iroh``, so owner-side approval for a Windows target is served by
the Mac module, while an unlisted peer class remains refused (negative
control).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from mycelium_a13_macos import pairing as mac_pairing
from mycelium_a13_windows.onboarding import (
    ENCRYPTED_HANDOFF_PROTOCOL,
    PAIRING_REQUEST_PROTOCOL,
    WINDOWS_PEER_CLASS,
    OnboardingError,
    approve_windows_pairing_request,
    complete_local_join,
    decrypt_windows_invitation_handoff,
    new_windows_pairing_request,
    validate_windows_quota,
    verify_windows_pairing_request,
)
from mycelium_invite import mint_invite_bundle
from mycelium_qualification.evidence import canonical_json_bytes
from mycelium_qualification.signing import generate_ed25519_signer

NOW = 2_000_000_000.0
NONCE = "pair-win1-1"
QUOTA = {
    "storage_bytes": 2_000_000_000,
    "network_bytes_per_day": 5_000_000_000,
    "background_allowed": False,
}


def _case(*, nonce: str = NONCE, node_id: str = "mycelium-win-1", ttl_seconds: int = 300):
    target = generate_ed25519_signer(endpoint_id="target-windows-endpoint")
    seed = generate_ed25519_signer(endpoint_id="seed-endpoint")
    request, private_key = new_windows_pairing_request(
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
    handoff = approve_windows_pairing_request(
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
        "acl_observation": "owner_only",
        "now": NOW + 5,
    }
    arguments.update(overrides)
    return complete_local_join(**arguments)


def _code(call):
    with pytest.raises(OnboardingError) as caught:
        call()
    return caught.value.code


def test_windows_target_speaks_the_exact_frozen_protocols() -> None:
    assert PAIRING_REQUEST_PROTOCOL == mac_pairing.PAIRING_REQUEST_PROTOCOL
    assert ENCRYPTED_HANDOFF_PROTOCOL == mac_pairing.ENCRYPTED_HANDOFF_PROTOCOL
    assert PAIRING_REQUEST_PROTOCOL == "mycelium.pairing_request.v1"
    assert ENCRYPTED_HANDOFF_PROTOCOL == "mycelium.encrypted_invitation_handoff.v1"
    assert WINDOWS_PEER_CLASS == "windows_native_iroh"


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
        request=mac_request, invite_bundle=case["bundle"], quota=QUOTA, now=NOW
    )
    assert set(case["request"]) == set(mac_request)
    assert set(case["handoff"]) == set(mac_handoff)
    assert case["request"]["peer_class"] == WINDOWS_PEER_CLASS
    # The request carries no swarm credential, seed location, or plaintext secret.
    rendered = repr(case["request"]).lower()
    for forbidden in ("judge-swarm", "seed_url", "swarm_id", "seed_key", "secret"):
        assert forbidden not in rendered


def test_the_mac_owner_module_now_admits_the_windows_peer_class() -> None:
    """Cross-lane gap A1 closed: the owner module's allow-set now names both

    supported peer classes explicitly.  ``new_pairing_request`` (the target-side
    minting entry point the owner module also exposes) and
    ``verify_pairing_request`` (the owner-side check on an incoming request)
    both admit ``windows_native_iroh`` through the same closed allow-set.
    """

    signer = generate_ed25519_signer(endpoint_id="judge-mac")
    request, _ = mac_pairing.new_pairing_request(
        signer=signer,
        node_id="judge-mac",
        peer_class=WINDOWS_PEER_CLASS,
        now=NOW,
        ttl_seconds=300,
        nonce=NONCE,
    )
    assert request["peer_class"] == WINDOWS_PEER_CLASS
    verified = mac_pairing.verify_pairing_request(request, now=NOW + 1)
    assert verified["peer_class"] == WINDOWS_PEER_CLASS


def test_the_mac_owner_module_still_refuses_an_unknown_peer_class() -> None:
    """Negative control: the allow-set stays closed for classes never listed.

    This proves the A1 fix is an explicit allow-set addition, not a relaxed or
    removed check — an OS/brand string that nobody declared is still refused.
    """

    signer = generate_ed25519_signer(endpoint_id="judge-mac")
    with pytest.raises(mac_pairing.PairingError) as caught:
        mac_pairing.new_pairing_request(
            signer=signer,
            node_id="judge-mac",
            peer_class="some_future_unlisted_iroh",
            now=NOW,
            ttl_seconds=300,
            nonce=NONCE,
        )
    assert caught.value.code == "pairing_peer_class_unsupported"


def test_a_windows_request_verifies_and_a_handoff_round_trips() -> None:
    case = _case()
    verified = verify_windows_pairing_request(case["request"], now=NOW + 1)
    assert verified["peer_class"] == WINDOWS_PEER_CLASS
    payload = decrypt_windows_invitation_handoff(
        request=case["request"],
        handoff=case["handoff"],
        recipient_private_key=case["private_key"],
        now=NOW + 1,
    )
    assert payload["seed_key_digest"] == case["seed"].verification_key_digest
    assert payload["quota"] == QUOTA
    assert validate_windows_quota(QUOTA) == QUOTA


def test_pairing_request_refusals_fail_closed() -> None:
    case = _case()
    assert _code(lambda: verify_windows_pairing_request(case["request"], now=NOW + 1000)) == (
        "onboarding_request_expired"
    )
    tampered = dict(case["request"])
    tampered["node_id"] = "mycelium-win-2"
    assert _code(lambda: verify_windows_pairing_request(tampered, now=NOW + 1)) == (
        "onboarding_request_signature_invalid"
    )
    foreign = dict(case["request"])
    foreign["peer_class"] = "mac_mlx_iroh"
    assert _code(lambda: verify_windows_pairing_request(foreign, now=NOW + 1)) in {
        "onboarding_request_signature_invalid",
        "onboarding_peer_class_unsupported",
    }
    other = _case(nonce="pair-win1-2")
    assert _code(
        lambda: decrypt_windows_invitation_handoff(
            request=case["request"],
            handoff=other["handoff"],
            recipient_private_key=case["private_key"],
            now=NOW + 1,
        )
    ) in {"onboarding_request_mismatch", "onboarding_handoff_invalid"}


def test_handoff_refuses_foreign_recipients_expiry_and_corruption() -> None:
    case = _case()
    other_private = b"\x22" * 32
    assert _code(
        lambda: decrypt_windows_invitation_handoff(
            request=case["request"],
            handoff=case["handoff"],
            recipient_private_key=other_private,
            now=NOW + 1,
        )
    ) == "onboarding_recipient_mismatch"
    assert _code(
        lambda: decrypt_windows_invitation_handoff(
            request=case["request"],
            handoff=case["handoff"],
            recipient_private_key=case["private_key"],
            # The request is still live (ttl 300s); the invite-bound handoff expired.
            now=NOW + 200,
        )
    ) == "onboarding_handoff_expired"
    corrupted = dict(case["handoff"])
    corrupted["ciphertext"] = corrupted["ciphertext"][:-4] + "AAAA"
    assert _code(
        lambda: decrypt_windows_invitation_handoff(
            request=case["request"],
            handoff=corrupted,
            recipient_private_key=case["private_key"],
            now=NOW + 1,
        )
    ) == "onboarding_handoff_invalid"
    unknown = {**case["handoff"], "extra": 1}
    assert _code(
        lambda: decrypt_windows_invitation_handoff(
            request=case["request"],
            handoff=unknown,
            recipient_private_key=case["private_key"],
            now=NOW + 1,
        )
    ) == "onboarding_handoff_invalid"
    assert _code(lambda: validate_windows_quota({"storage_bytes": 1})) == (
        "onboarding_quota_invalid"
    )
    assert _code(
        lambda: validate_windows_quota({**QUOTA, "storage_bytes": -1})
    ) == "onboarding_quota_invalid"


def test_local_join_persists_one_member_and_never_grants_eligibility(tmp_path) -> None:
    case = _case()
    root = _state_root(tmp_path)
    identity = _identity(root)
    result = _join(case, root, identity)
    assert result["joined"] is True
    assert result["member_generation"] == 1
    assert result["eligibility"] == "member_only"
    assert result["route_ready"] is False
    assert result["progress"]["eligibility_granted"] is False
    assert result["progress"]["route_ready"] is False
    member = root / "onboarding" / "member.json"
    assert member.is_file()
    ledger = root / "onboarding" / "consumed-handoffs.json"
    assert NONCE in ledger.read_text("utf-8")
    assert "identity.key" not in member.read_text("utf-8")
    assert str(root) not in member.read_text("utf-8")


def test_replay_and_stale_generation_are_refused(tmp_path) -> None:
    case = _case()
    root = _state_root(tmp_path)
    identity = _identity(root)
    _join(case, root, identity)
    assert _code(lambda: _join(case, root, identity, now=NOW + 6)) == (
        "onboarding_invitation_replayed"
    )
    newer = _case(nonce="pair-win1-3")
    assert _code(lambda: _join(newer, root, identity, now=NOW + 7)) == (
        "onboarding_member_generation_stale"
    )


def test_seed_pin_acl_and_state_root_boundaries_are_enforced(tmp_path) -> None:
    case = _case()
    root = _state_root(tmp_path)
    identity = _identity(root)
    other_seed = generate_ed25519_signer(endpoint_id="other-seed")
    assert _code(
        lambda: _join(case, root, identity, seed_key_digest_pin=other_seed.verification_key_digest)
    ) == "onboarding_seed_pin_mismatch"
    assert _code(lambda: _join(case, root, identity, acl_observation="unknown")) in {
        "onboarding_state_root_unsafe",
        "onboarding_identity_unsafe",
    }
    assert _code(
        lambda: _join(case, root, identity, acl_observation="not_owner_only")
    ) in {"onboarding_state_root_unsafe", "onboarding_identity_unsafe"}
    assert _code(lambda: _join(case, root, identity, acl_observation=None)) in {
        "onboarding_state_root_unsafe",
        "onboarding_identity_unsafe",
    }

    open_root = tmp_path / "open-state"
    open_root.mkdir(mode=0o755)
    open_root.chmod(0o755)
    open_identity = _identity(open_root)
    assert _code(lambda: _join(case, open_root, open_identity)) == (
        "onboarding_state_root_unsafe"
    )

    missing_identity = root / "absent.key"
    fresh = _case(nonce="pair-win1-4")
    assert _code(lambda: _join(fresh, root, missing_identity, now=NOW + 8)) == (
        "onboarding_identity_missing"
    )


def test_plaintext_handoff_state_is_erased_after_durable_acceptance(tmp_path) -> None:
    case = _case()
    root = _state_root(tmp_path)
    identity = _identity(root)
    plaintext = tmp_path / "handoff.json"
    plaintext.write_bytes(canonical_json_bytes(case["handoff"]))
    result = _join(case, root, identity, plaintext_handoff_file=plaintext)
    assert result["plaintext_erased"] is True
    assert not plaintext.exists()


def test_the_mac_owner_module_approves_a_windows_target_request_end_to_end(tmp_path) -> None:
    """Gap A1, owner side end to end: the SHIPPED owner module serves Windows.

    Every other test here approves through the Windows tranche's own
    ``approve_windows_pairing_request``.  This one proves the real owner path:
    a request minted by the Windows target verifies through the Mac owner
    module, the Mac owner approves it, only the Windows target's recipient key
    decrypts the handoff back to the bound invitation, and the Windows target
    completes its one local join from the owner-minted handoff.
    """

    case = _case(nonce="pair-win1-owner")
    verified = mac_pairing.verify_pairing_request(case["request"], now=NOW)
    assert verified["peer_class"] == WINDOWS_PEER_CLASS

    owner_handoff = mac_pairing.approve_pairing_request(
        request=case["request"],
        invite_bundle=case["bundle"],
        quota=QUOTA,
        now=NOW,
    )
    payload = decrypt_windows_invitation_handoff(
        request=case["request"],
        handoff=owner_handoff,
        recipient_private_key=case["private_key"],
        now=NOW + 1,
    )
    assert payload["peer_class"] == WINDOWS_PEER_CLASS
    assert payload["invite_bundle"] == case["bundle"]
    assert payload["seed_key_digest"] == case["seed"].verification_key_digest

    # Only the requesting Windows target's recipient key opens the owner handoff.
    stranger = _case(nonce="pair-win1-owner")
    assert _code(
        lambda: decrypt_windows_invitation_handoff(
            request=case["request"],
            handoff=owner_handoff,
            recipient_private_key=stranger["private_key"],
            now=NOW + 1,
        )
    ) == "onboarding_recipient_mismatch"

    root = _state_root(tmp_path)
    identity = _identity(root)
    result = _join({**case, "handoff": owner_handoff}, root, identity)
    assert result["joined"] is True
    assert result["eligibility"] == "member_only"
    assert result["route_ready"] is False


def test_the_mac_owner_module_refuses_an_unlisted_class_on_the_verify_path() -> None:
    """Negative control on the owner's VERIFY path (not only on minting).

    A correctly signed request whose peer class nobody declared is refused by
    the owner module's verifier, and therefore can never be approved.
    """

    signer = generate_ed25519_signer(endpoint_id="judge-mac")
    request, _ = mac_pairing.new_pairing_request(
        signer=signer,
        node_id="judge-mac",
        peer_class=WINDOWS_PEER_CLASS,
        now=NOW,
        ttl_seconds=300,
        nonce=NONCE,
    )
    unsigned = {key: value for key, value in request.items() if key != "signature"}
    unsigned["peer_class"] = "windows_future_unlisted_iroh"
    forged = dict(unsigned, signature=signer.sign(unsigned))
    with pytest.raises(mac_pairing.PairingError) as verified:
        mac_pairing.verify_pairing_request(forged, now=NOW)
    assert verified.value.code == "pairing_request_invalid"
    bundle = mint_invite_bundle(
        signer=generate_ed25519_signer(endpoint_id="seed-endpoint"),
        swarm_id="judge-swarm",
        seed_url="https://127.0.0.1:4443",
        ttl_seconds=120,
        nonce=NONCE,
        issued_at=NOW,
    )
    with pytest.raises(mac_pairing.PairingError) as approved:
        mac_pairing.approve_pairing_request(
            request=forged, invite_bundle=bundle, quota=QUOTA, now=NOW
        )
    assert approved.value.code == "pairing_request_invalid"
