from __future__ import annotations

import json

import pytest

from mycelium_invite import (
    INVITE_BUNDLE_PROTOCOL,
    InviteError,
    mint_invite_bundle,
    verify_invite_bundle,
)
from mycelium_qualification.signing import generate_ed25519_signer


def _bundle(*, nonce: str = "nonce-bundle-1"):
    signer = generate_ed25519_signer(endpoint_id="seed-endpoint")
    bundle = mint_invite_bundle(
        signer=signer,
        swarm_id="swarm-demo",
        seed_url="https://seed.tail.example:8788",
        ttl_seconds=300,
        nonce=nonce,
        issued_at=1_784_000_000.0,
    )
    return signer, bundle


def test_invite_bundle_pins_seed_key_and_verifies_token() -> None:
    signer, bundle = _bundle()

    verified = verify_invite_bundle(bundle, now=1_784_000_010.0)

    assert bundle == {
        "protocol": INVITE_BUNDLE_PROTOCOL,
        "seed_key_digest": signer.verification_key_digest,
        "seed_key_records": [signer.public_key_record()],
        "token": bundle["token"],
    }
    assert verified["payload"]["seed_url"] == "https://seed.tail.example:8788"
    assert verified["payload"]["nonce"] == "nonce-bundle-1"
    assert verified["seed_key_digest"] == signer.verification_key_digest
    assert verified["seed_key_records"] == (signer.public_key_record(),)


def test_invite_bundle_is_json_serializable_and_contains_no_private_key_material() -> None:
    _signer, bundle = _bundle()

    encoded = json.dumps(bundle, allow_nan=False, sort_keys=True)

    assert "private" not in encoded.lower()
    assert "seed_key_records" in encoded


@pytest.mark.parametrize(
    ("mutator", "code"),
    [
        (lambda bundle: dict(bundle, protocol="mycelium.invite_bundle.v0"), "invite_bundle_protocol_invalid"),
        (lambda bundle: dict(bundle, surprise=True), "invite_bundle_malformed"),
        (lambda bundle: dict(bundle, seed_key_digest="0" * 64), "invite_bundle_key_pin_mismatch"),
        (lambda bundle: dict(bundle, seed_key_records=[]), "invite_bundle_key_records_invalid"),
    ],
)
def test_invite_bundle_rejects_malformed_or_unpinned_shapes(mutator, code: str) -> None:
    _signer, bundle = _bundle()

    with pytest.raises(InviteError) as excinfo:
        verify_invite_bundle(mutator(bundle), now=1_784_000_010.0)

    assert excinfo.value.code == code


def test_invite_bundle_rejects_token_signed_by_different_seed() -> None:
    _signer, bundle = _bundle()
    other = generate_ed25519_signer(endpoint_id="other-seed")
    forged = mint_invite_bundle(
        signer=other,
        swarm_id="swarm-demo",
        seed_url="https://attacker.example:8788",
        ttl_seconds=300,
        nonce="forged",
        issued_at=1_784_000_000.0,
    )
    mixed = dict(bundle, token=forged["token"])

    with pytest.raises(InviteError) as excinfo:
        verify_invite_bundle(mixed, now=1_784_000_010.0)

    assert excinfo.value.code == "invite_signature_invalid"


@pytest.mark.parametrize("now", [float("nan"), float("inf"), True])
def test_invite_bundle_rejects_nonfinite_or_boolean_verification_time(now) -> None:
    _signer, bundle = _bundle()

    with pytest.raises(InviteError) as excinfo:
        verify_invite_bundle(bundle, now=now)

    assert excinfo.value.code == "invite_time_invalid"
