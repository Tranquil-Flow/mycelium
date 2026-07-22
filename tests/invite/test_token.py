import base64
import json
import time

import pytest

from mycelium_invite.token import (
    InviteError,
    InviteRegistry,
    mint_invite,
    verify_invite,
)
from mycelium_qualification.signing import generate_ed25519_signer


def _encoded(value: object) -> str:
    return base64.urlsafe_b64encode(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).decode().rstrip("=")


def _signed_token(signer, payload: dict, *, pretty_body: bool = False) -> str:
    body = (
        json.dumps(payload, indent=2).encode()
        if pretty_body
        else json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    )
    encoded_body = base64.urlsafe_b64encode(body).decode().rstrip("=")
    return f"{encoded_body}.{_encoded(signer.sign(payload))}"


@pytest.fixture
def signer():
    return generate_ed25519_signer(endpoint_id="seed-endpoint")


@pytest.fixture
def key_records(signer):
    return [signer.public_key_record()]


def test_valid_invite_roundtrips(signer, key_records) -> None:
    now = 1_784_000_000.0
    token = mint_invite(
        signer=signer,
        swarm_id="swarm-demo",
        seed_url="http://100.84.252.4:8788",
        ttl_seconds=3600,
        nonce="nonce-001",
        issued_at=now,
    )
    payload = verify_invite(token, verifier_key_records=key_records, now=now + 10)
    assert payload["swarm_id"] == "swarm-demo"
    assert payload["seed_url"] == "http://100.84.252.4:8788"
    assert payload["nonce"] == "nonce-001"


def test_expired_invite_is_rejected(signer, key_records) -> None:
    now = 1_784_000_000.0
    token = mint_invite(
        signer=signer,
        swarm_id="swarm-demo",
        seed_url="http://seed:8788",
        ttl_seconds=60,
        nonce="nonce-002",
        issued_at=now,
    )
    with pytest.raises(InviteError) as excinfo:
        verify_invite(token, verifier_key_records=key_records, now=now + 3600)
    assert excinfo.value.code == "invite_expired"


def test_tampered_payload_is_rejected(signer, key_records) -> None:
    token = mint_invite(
        signer=signer,
        swarm_id="swarm-demo",
        seed_url="http://seed:8788",
        ttl_seconds=3600,
        nonce="nonce-003",
    )
    body, _, signature = token.partition(".")
    forged = body[:-1] + ("A" if body[-1] != "A" else "B") + "." + signature
    with pytest.raises(InviteError):
        verify_invite(forged, verifier_key_records=key_records, now=time.time())


def test_wrong_key_is_rejected(signer) -> None:
    other_records = [
        generate_ed25519_signer(endpoint_id="other-seed").public_key_record()
    ]
    token = mint_invite(
        signer=signer,
        swarm_id="swarm-demo",
        seed_url="http://seed:8788",
        ttl_seconds=3600,
        nonce="nonce-004",
    )
    with pytest.raises(InviteError) as excinfo:
        verify_invite(token, verifier_key_records=other_records, now=time.time())
    assert excinfo.value.code == "invite_signature_invalid"


def test_signed_non_numeric_expiry_is_rejected(signer, key_records) -> None:
    payload = {
        "protocol": "mycelium.invite.v1",
        "swarm_id": "swarm-demo",
        "seed_url": "http://seed:8788",
        "nonce": "nonce-nonnumeric-expiry",
        "issued_at": 1_000.0,
        "expires_at": "NaN",
    }
    token = _signed_token(signer, payload)

    with pytest.raises(InviteError) as excinfo:
        verify_invite(token, verifier_key_records=key_records, now=1_001.0)

    assert excinfo.value.code == "invite_malformed"


def test_signed_noncanonical_or_extra_field_body_is_rejected(
    signer, key_records
) -> None:
    payload = {
        "protocol": "mycelium.invite.v1",
        "swarm_id": "swarm-demo",
        "seed_url": "http://seed:8788",
        "nonce": "nonce-noncanonical",
        "issued_at": 1_000.0,
        "expires_at": 1_100.0,
    }
    with pytest.raises(InviteError, match="invite_malformed"):
        verify_invite(
            _signed_token(signer, payload, pretty_body=True),
            verifier_key_records=key_records,
            now=1_001.0,
        )

    payload["unexpected"] = "field"
    with pytest.raises(InviteError, match="invite_malformed"):
        verify_invite(
            _signed_token(signer, payload),
            verifier_key_records=key_records,
            now=1_001.0,
        )


def test_registry_rejects_replay() -> None:
    registry = InviteRegistry()
    registry.consume("nonce-005", now=1000.0)
    with pytest.raises(InviteError) as excinfo:
        registry.consume("nonce-005", now=1001.0)
    assert excinfo.value.code == "invite_replayed"


@pytest.mark.parametrize(
    ("overrides", "code"),
    [
        ({"swarm_id": 7}, "invite_field_invalid"),
        ({"seed_url": " seed.example"}, "invite_field_invalid"),
        ({"nonce": ""}, "invite_field_invalid"),
        ({"ttl_seconds": True}, "invite_ttl_invalid"),
        ({"ttl_seconds": 0}, "invite_ttl_invalid"),
        ({"ttl_seconds": 1.5}, "invite_ttl_invalid"),
        ({"issued_at": float("nan")}, "invite_time_invalid"),
        ({"issued_at": float("inf")}, "invite_time_invalid"),
        ({"issued_at": True}, "invite_time_invalid"),
    ],
)
def test_mint_rejects_malformed_fields_with_stable_codes(overrides, code: str) -> None:
    signer = generate_ed25519_signer(endpoint_id="seed-endpoint")
    arguments = {
        "signer": signer,
        "swarm_id": "swarm-demo",
        "seed_url": "https://seed.example:8788",
        "ttl_seconds": 300,
        "nonce": "nonce-valid",
        "issued_at": 1_784_000_000.0,
    }
    arguments.update(overrides)

    with pytest.raises(InviteError) as excinfo:
        mint_invite(**arguments)

    assert excinfo.value.code == code


def test_malformed_token_is_rejected(key_records) -> None:
    for bad in ("", "no-dot", "a.b.c", "!!!.???"):
        with pytest.raises(InviteError):
            verify_invite(bad, verifier_key_records=key_records, now=time.time())
