# SPDX-License-Identifier: AGPL-3.0-only
"""A12 Android/Pixel mobile onboarding and invite flow (SOURCE tranche).

The Android target speaks the exact wireless-shape protocols the committed
macOS slice froze (``mycelium.pairing_request.v1`` /
``mycelium.encrypted_invitation_handoff.v1``) and performs exactly one atomic
local join that starts ``route_ready=false``.  Nothing here executes on a
device, signs an application, or qualifies a role.
"""

from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest

import mycelium_mobile.a12_activation as a12
from mycelium_invite import mint_invite_bundle
from mycelium_mobile.android_onboarding import (
    ANDROID_PEER_CLASS,
    ENCRYPTED_HANDOFF_PROTOCOL,
    JOIN_PROTOCOL,
    ONBOARDING_STEPS,
    PAIRING_REQUEST_PROTOCOL,
    AndroidOnboardingError,
    approve_android_pairing_request,
    decrypt_android_invitation_handoff,
    join_android_member,
    new_android_pairing_request,
    validate_android_quota,
    verify_android_pairing_request,
)
from mycelium_qualification.signing import generate_ed25519_signer

NOW = 2_000_000_000.0
NONCE = "pair-android-pixel-1"
DEVICE_REF = "device-android-0001"
QUOTA = {
    "storage_bytes": 2_000_000_000,
    "network_bytes_per_day": 5_000_000_000,
    "background_allowed": False,
    "battery_floor_percent": 25,
    "thermal_ceiling_c": 40,
}


def _case(*, nonce: str = NONCE, device_ref: str = DEVICE_REF, ttl_seconds: int = 300):
    target = generate_ed25519_signer(endpoint_id="target-android-endpoint")
    seed = generate_ed25519_signer(endpoint_id="seed-endpoint")
    request, private_key = new_android_pairing_request(
        signer=target,
        device_ref=device_ref,
        now=NOW,
        ttl_seconds=ttl_seconds,
        nonce=nonce,
    )
    bundle = mint_invite_bundle(
        signer=seed,
        swarm_id="judge-swarm",
        seed_url="https://127.0.0.1:4443",
        ttl_seconds=120,
        nonce=nonce,
        issued_at=NOW,
    )
    handoff = approve_android_pairing_request(
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
    root = tmp_path / "app-private"
    root.mkdir(mode=0o700)
    return root


def _identity(root: Path) -> Path:
    path = root / "device-identity.key"
    path.write_bytes(b"\x2a" * 32)
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
    return join_android_member(**arguments)


def _code(call):
    with pytest.raises((AndroidOnboardingError, a12.A12ContractError)) as caught:
        call()
    return caught.value.code


def _native_profile_case(peer_class="android_native_qwen2_iroh"):
    # Synthetic native-shaped request; no device/runtime qualification claim.
    case = _case()
    statement = {k: v for k, v in case["request"].items() if k != "signature"}
    statement["peer_class"] = peer_class
    case["request"] = {**statement, "signature": case["target"].sign(statement)}
    return case


def test_qwen_native_profile_survives_approved_handoff_and_local_join(tmp_path):
    case = _native_profile_case()
    assert verify_android_pairing_request(case["request"], now=NOW)["peer_class"] == "android_native_qwen2_iroh"
    case["handoff"] = approve_android_pairing_request(
        request=case["request"], invite_bundle=case["bundle"], quota=QUOTA, now=NOW
    )
    payload = decrypt_android_invitation_handoff(
        request=case["request"], handoff=case["handoff"],
        recipient_private_key=case["private_key"], now=NOW + 1,
    )
    assert payload["peer_class"] == "android_native_qwen2_iroh"
    root = _state_root(tmp_path)
    joined = _join(case, root, _identity(root))
    assert joined["peer_class"] == "android_native_qwen2_iroh"
    assert joined["route_ready"] is False
    # Existing callers retain the reference profile; acceptance is not promotion.
    assert _case()["request"]["peer_class"] == ANDROID_PEER_CLASS


def test_qwen_profile_cannot_reuse_reference_profile_approval():
    case = _native_profile_case()
    assert _code(lambda: decrypt_android_invitation_handoff(
        request=case["request"], handoff=case["handoff"],
        recipient_private_key=case["private_key"], now=NOW + 1,
    )) == "android_onboarding_request_mismatch"


@pytest.mark.parametrize("peer_class", ["android_native_qwen3_iroh", "linux_numpy_iroh", [], None])
def test_native_profile_allowlist_remains_closed_and_typed(peer_class):
    case = _native_profile_case(peer_class)
    assert _code(lambda: verify_android_pairing_request(case["request"], now=NOW)) == "android_onboarding_peer_class_unsupported"


def test_unsigned_reference_profile_relabel_is_refused():
    case = _case()
    request = {**case["request"], "peer_class": "android_native_qwen2_iroh"}
    assert _code(lambda: verify_android_pairing_request(request, now=NOW)) == "android_onboarding_request_signature_invalid"


def test_android_target_speaks_the_exact_protocols_the_mac_slice_froze() -> None:
    from mycelium_a13_macos import pairing as mac_pairing

    assert PAIRING_REQUEST_PROTOCOL == mac_pairing.PAIRING_REQUEST_PROTOCOL
    assert ENCRYPTED_HANDOFF_PROTOCOL == mac_pairing.ENCRYPTED_HANDOFF_PROTOCOL
    assert PAIRING_REQUEST_PROTOCOL == "mycelium.pairing_request.v1"
    assert ENCRYPTED_HANDOFF_PROTOCOL == "mycelium.encrypted_invitation_handoff.v1"
    assert ANDROID_PEER_CLASS == "android_native_iroh"


def test_the_peer_class_is_the_generic_registered_native_android_path() -> None:
    assert ANDROID_PEER_CLASS not in a12.EXCLUDED_NORMAL_PRODUCT_PATHS
    assert a12.require_undivided_product_path("signed_native_application")
    assert "pixel" not in ANDROID_PEER_CLASS
    assert "termux" not in ANDROID_PEER_CLASS


def test_the_onboarding_step_ladder_is_closed_and_ordered() -> None:
    assert ONBOARDING_STEPS == (
        "pairing_requested",
        "invitation_approved",
        "invitation_decrypted",
        "identity_verified",
        "member_joined",
    )
    assert JOIN_PROTOCOL == "mycelium.a12_android_local_member.v1"


def test_request_and_handoff_shapes_are_closed_and_announce_the_native_path() -> None:
    case = _case()

    assert case["request"]["protocol"] == PAIRING_REQUEST_PROTOCOL
    assert case["request"]["peer_class"] == ANDROID_PEER_CLASS
    assert case["request"]["carrier"] == "signed_native_application"
    assert case["request"]["device_ref"] == DEVICE_REF
    assert case["handoff"]["protocol"] == ENCRYPTED_HANDOFF_PROTOCOL

    verified = verify_android_pairing_request(case["request"], now=NOW)
    assert verified["device_ref"] == DEVICE_REF
    assert "recipient_encryption_key" in verified


def test_no_field_of_the_request_or_handoff_carries_a_secret_bytes_copy() -> None:
    case = _case()
    encoded = json.dumps({"request": case["request"], "handoff": case["handoff"]})
    assert case["private_key"].hex() not in encoded
    assert "private" not in encoded


def test_the_excluded_development_paths_can_never_carry_an_android_request() -> None:
    case = _case()
    for carrier in ("termux", "adb", "ssh"):
        tampered = {**case["request"], "carrier": carrier}
        tampered.pop("signature")
        tampered["signature"] = case["target"].sign(tampered)
        assert (
            _code(lambda tampered=tampered: verify_android_pairing_request(tampered, now=NOW))
            == "android_onboarding_carrier_excluded"
        )
    unknown = {**case["request"], "carrier": "telepathy"}
    unknown.pop("signature")
    unknown["signature"] = case["target"].sign(unknown)
    assert (
        _code(lambda unknown=unknown: verify_android_pairing_request(unknown, now=NOW))
        == "android_onboarding_carrier_invalid"
    )


def test_a_request_signed_by_another_key_and_an_unknown_field_are_refused() -> None:
    case = _case()
    attacker = generate_ed25519_signer(endpoint_id="attacker-endpoint")
    forged = {**case["request"], "signature": attacker.sign(
        {key: value for key, value in case["request"].items() if key != "signature"}
    )}
    assert (
        _code(lambda forged=forged: verify_android_pairing_request(forged, now=NOW))
        == "android_onboarding_request_signature_invalid"
    )

    extra = {**case["request"], "surprise": 1}
    assert (
        _code(lambda extra=extra: verify_android_pairing_request(extra, now=NOW))
        == "android_onboarding_request_invalid"
    )


def test_an_expired_request_and_an_out_of_range_ttl_are_refused() -> None:
    case = _case(ttl_seconds=300)
    assert (
        _code(lambda: verify_android_pairing_request(case["request"], now=NOW + 301))
        == "android_onboarding_request_expired"
    )
    assert (
        _code(
            lambda: new_android_pairing_request(
                signer=case["target"], device_ref=DEVICE_REF, now=NOW, ttl_seconds=0
            )
        )
        == "android_onboarding_request_ttl_invalid"
    )


def test_the_quota_is_bounded_closed_and_consent_shaped() -> None:
    assert validate_android_quota(QUOTA) == QUOTA
    assert (
        _code(lambda: validate_android_quota({**QUOTA, "extra": 1}))
        == "android_onboarding_quota_invalid"
    )
    assert (
        _code(lambda: validate_android_quota({**QUOTA, "storage_bytes": -1}))
        == "android_onboarding_quota_invalid"
    )
    assert (
        _code(lambda: validate_android_quota({**QUOTA, "background_allowed": "yes"}))
        == "android_onboarding_quota_invalid"
    )
    assert (
        _code(lambda: validate_android_quota({**QUOTA, "battery_floor_percent": 101}))
        == "android_onboarding_quota_invalid"
    )


def test_the_handoff_is_recipient_only_and_binds_every_identity() -> None:
    case = _case()
    payload = decrypt_android_invitation_handoff(
        request=case["request"],
        handoff=case["handoff"],
        recipient_private_key=case["private_key"],
        now=NOW + 1,
    )
    assert payload["device_ref"] == DEVICE_REF
    assert payload["peer_class"] == ANDROID_PEER_CLASS
    assert payload["quota"] == QUOTA
    assert payload["invite_bundle"]["protocol"] == "mycelium.invite_bundle.v1"

    other = _case(nonce="pair-android-pixel-2")
    assert (
        _code(
            lambda: decrypt_android_invitation_handoff(
                request=case["request"],
                handoff=case["handoff"],
                recipient_private_key=other["private_key"],
                now=NOW + 1,
            )
        )
        == "android_onboarding_recipient_mismatch"
    )
    assert (
        _code(
            lambda: decrypt_android_invitation_handoff(
                request=case["request"],
                handoff=case["handoff"],
                recipient_private_key=case["private_key"],
                now=NOW + 200,
            )
        )
        == "android_onboarding_handoff_expired"
    )
    assert (
        _code(
            lambda: decrypt_android_invitation_handoff(
                request=case["request"],
                handoff=case["handoff"],
                recipient_private_key=case["private_key"],
                now=NOW + 301,
            )
        )
        == "android_onboarding_request_expired"
    )


def test_owner_side_approval_refuses_an_invite_that_does_not_match_the_request() -> None:
    case = _case()
    foreign = mint_invite_bundle(
        signer=case["seed"],
        swarm_id="judge-swarm",
        seed_url="https://127.0.0.1:4443",
        ttl_seconds=120,
        nonce="pair-android-pixel-other",
        issued_at=NOW,
    )
    assert (
        _code(
            lambda: approve_android_pairing_request(
                request=case["request"], invite_bundle=foreign, quota=QUOTA, now=NOW
            )
        )
        == "android_onboarding_invite_binding_invalid"
    )


def test_one_atomic_join_starts_route_ready_false_and_grants_nothing(tmp_path) -> None:
    case = _case()
    root = _state_root(tmp_path)
    identity = _identity(root)
    result = _join(case, root, identity)

    assert result["protocol"] == JOIN_PROTOCOL
    assert result["peer_class"] == ANDROID_PEER_CLASS
    assert result["route_ready"] is False
    assert result["granted"] == ()
    assert result["rung_effect"] == "none"
    assert result["product_path_kind"] == "signed_native_application"
    assert result["member_generation"] == 1
    assert result["step"] == "member_joined"
    assert tuple(result["steps_completed"]) == ONBOARDING_STEPS
    assert result["identity_key_digest"] == case["target"].verification_key_digest
    assert result["seed_key_digest"] == case["seed"].verification_key_digest

    written = json.loads((root / "android-member.json").read_text("utf-8"))
    assert written["protocol"] == JOIN_PROTOCOL
    assert written["route_ready"] is False
    assert stat.S_IMODE((root / "android-member.json").stat().st_mode) == 0o600
    assert written["consumed_pairing_nonces"] == [NONCE]


def test_the_same_pairing_nonce_is_replayed_exactly_once(tmp_path) -> None:
    case = _case()
    root = _state_root(tmp_path)
    identity = _identity(root)
    _join(case, root, identity)
    assert (
        _code(lambda: _join(case, root, identity, member_generation=2))
        == "android_onboarding_replay_rejected"
    )


def test_a_later_join_must_fence_the_generation_it_replaces(tmp_path) -> None:
    case = _case()
    root = _state_root(tmp_path)
    identity = _identity(root)
    _join(case, root, identity)
    second = _case(nonce="pair-android-pixel-3")
    assert (
        _code(lambda: _join(second, root, identity, member_generation=1))
        == "android_onboarding_generation_not_fenced"
    )
    accepted = _join(second, root, identity, member_generation=2)
    assert accepted["member_generation"] == 2
    written = json.loads((root / "android-member.json").read_text("utf-8"))
    assert written["consumed_pairing_nonces"] == [NONCE, "pair-android-pixel-3"]


def test_the_seed_pin_and_the_identity_storage_must_be_exact(tmp_path) -> None:
    case = _case()
    root = _state_root(tmp_path)
    identity = _identity(root)
    assert (
        _code(
            lambda: _join(
                case, root, identity, seed_key_digest_pin=case["target"].verification_key_digest
            )
        )
        == "android_onboarding_seed_pin_mismatch"
    )
    loose = root / "loose.key"
    loose.write_bytes(b"\x2a" * 32)
    loose.chmod(0o644)
    assert _code(lambda: _join(case, root, loose)) == "android_onboarding_identity_unsafe"
    assert (
        _code(lambda: _join(case, root, root / "missing.key"))
        == "android_onboarding_identity_missing"
    )
    world = tmp_path / "world"
    world.mkdir(mode=0o755)
    assert _code(lambda: _join(case, world, identity)) == "android_onboarding_state_root_unsafe"


def test_joining_never_rewrites_route_ready_and_never_authors_a_grant(tmp_path) -> None:
    case = _case()
    root = _state_root(tmp_path)
    identity = _identity(root)

    assert a12.require_route_ready_is_false(False) is False
    assert (
        _code(lambda: a12.require_route_ready_is_false(True)) == "route_ready_must_start_false"
    )

    result = _join(case, root, identity)
    assert result["route_ready"] is False
    assert result["granted"] == ()
    assert result["rung_effect"] == "none"
    assert result["authority"] == "a12_android_onboarding_source"
    assert (
        _code(lambda: _join(case, root, identity, route_ready=True))
        == "route_ready_must_start_false"
    )
