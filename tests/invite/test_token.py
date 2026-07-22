import time

import pytest

from mycelium_invite.token import (
    InviteError,
    InviteRegistry,
    mint_invite,
    verify_invite,
)
from mycelium_qualification.signing import generate_ed25519_signer


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


def test_registry_rejects_replay() -> None:
    registry = InviteRegistry()
    registry.consume("nonce-005", now=1000.0)
    with pytest.raises(InviteError) as excinfo:
        registry.consume("nonce-005", now=1001.0)
    assert excinfo.value.code == "invite_replayed"


def test_malformed_token_is_rejected(key_records) -> None:
    for bad in ("", "no-dot", "a.b.c", "!!!.???"):
        with pytest.raises(InviteError):
            verify_invite(bad, verifier_key_records=key_records, now=time.time())
