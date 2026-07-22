"""Seed-pinned invitation bundles for secure first contact."""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from mycelium_qualification.signing import Ed25519EvidenceSigner

from .token import InviteError, mint_invite, verify_invite

INVITE_BUNDLE_PROTOCOL = "mycelium.invite_bundle.v1"
_BUNDLE_FIELDS = frozenset(
    {"protocol", "seed_key_digest", "seed_key_records", "token"}
)


def mint_invite_bundle(
    *,
    signer: Ed25519EvidenceSigner,
    swarm_id: str,
    seed_url: str,
    ttl_seconds: int,
    nonce: str,
    issued_at: float | None = None,
) -> dict[str, Any]:
    """Mint one invite plus the exact public seed key trusted by its holder."""

    key_record = signer.public_key_record()
    token = mint_invite(
        signer=signer,
        swarm_id=swarm_id,
        seed_url=seed_url,
        ttl_seconds=ttl_seconds,
        nonce=nonce,
        issued_at=issued_at,
    )
    return {
        "protocol": INVITE_BUNDLE_PROTOCOL,
        "seed_key_digest": signer.verification_key_digest,
        "seed_key_records": [key_record],
        "token": token,
    }


def verify_invite_bundle(
    bundle: Mapping[str, Any],
    *,
    now: float,
) -> dict[str, Any]:
    """Verify strict bundle shape, key pin, token signature, and expiry."""

    if not isinstance(bundle, Mapping) or set(bundle) != _BUNDLE_FIELDS:
        raise InviteError("invite_bundle_malformed")
    if bundle.get("protocol") != INVITE_BUNDLE_PROTOCOL:
        raise InviteError("invite_bundle_protocol_invalid")

    records = bundle.get("seed_key_records")
    if (
        not isinstance(records, list)
        or len(records) != 1
        or not isinstance(records[0], Mapping)
    ):
        raise InviteError("invite_bundle_key_records_invalid")
    record = dict(records[0])
    pinned_digest = bundle.get("seed_key_digest")
    if (
        not isinstance(pinned_digest, str)
        or not pinned_digest
        or record.get("verification_key_digest") != pinned_digest
    ):
        raise InviteError("invite_bundle_key_pin_mismatch")

    token = bundle.get("token")
    if not isinstance(token, str) or not token:
        raise InviteError("invite_bundle_malformed")
    payload = verify_invite(
        token,
        verifier_key_records=[record],
        now=now,
    )
    return {
        "payload": payload,
        "seed_key_digest": pinned_digest,
        "seed_key_records": (record,),
    }
