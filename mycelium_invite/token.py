"""Signed single-use swarm invite tokens.

Invites reuse the repository's ``Ed25519EvidenceSigner`` rather than handling raw
key bytes: ``mint_invite`` takes a signer, ``verify_invite`` takes the seed's
published public-key record(s). The signer signs the invite payload as a canonical
statement; verification rebuilds the same statement bytes and checks the detached
evidence signature. This respects the signing library's deliberate private-key
boundary and adds no new cryptographic code.
"""
from __future__ import annotations

import base64
from collections.abc import Mapping, Sequence
import json
import math
from typing import Any

from mycelium_qualification.evidence import canonical_json_bytes
from mycelium_qualification.signing import (
    Ed25519EvidenceSigner,
    build_ed25519_verifier,
)

_INVITE_PROTOCOL = "mycelium.invite.v1"
_PAYLOAD_FIELDS = ("protocol", "swarm_id", "seed_url", "nonce", "issued_at", "expires_at")


class InviteError(ValueError):
    """Fail-closed invite error carrying a stable code."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _b64encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    try:
        return base64.urlsafe_b64decode(value + padding)
    except Exception as exc:  # noqa: BLE001 - fail closed on any decode error
        raise InviteError("invite_malformed") from exc


def _strict_json_object(raw: bytes) -> dict[str, Any]:
    def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        document: dict[str, Any] = {}
        for key, value in pairs:
            if key in document:
                raise ValueError("duplicate JSON key")
            document[key] = value
        return document

    def reject_constant(_value: str) -> None:
        raise ValueError("non-finite JSON value")

    try:
        document = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=reject_duplicates,
            parse_constant=reject_constant,
        )
        if not isinstance(document, dict) or canonical_json_bytes(document) != raw:
            raise ValueError("non-canonical JSON object")
    except Exception as exc:
        raise InviteError("invite_malformed") from exc
    return document


def mint_invite(
    *,
    signer: Ed25519EvidenceSigner,
    swarm_id: str,
    seed_url: str,
    ttl_seconds: int,
    nonce: str,
    issued_at: float | None = None,
) -> str:
    """Mint a signed single-use invite token bound to the seed's signer."""

    if (
        isinstance(ttl_seconds, bool)
        or not isinstance(ttl_seconds, int)
        or ttl_seconds <= 0
    ):
        raise InviteError("invite_ttl_invalid")
    if not all(
        isinstance(value, str) and bool(value) and value == value.strip()
        for value in (swarm_id, seed_url, nonce)
    ):
        raise InviteError("invite_field_invalid")
    import time

    issued = time.time() if issued_at is None else issued_at
    if (
        isinstance(issued, bool)
        or not isinstance(issued, (int, float))
        or not math.isfinite(float(issued))
    ):
        raise InviteError("invite_time_invalid")
    issued = float(issued)
    payload = {
        "protocol": _INVITE_PROTOCOL,
        "swarm_id": swarm_id,
        "seed_url": seed_url,
        "nonce": nonce,
        "issued_at": issued,
        "expires_at": issued + ttl_seconds,
    }
    signature = signer.sign(payload)
    body = canonical_json_bytes(payload)
    envelope = canonical_json_bytes(signature)
    return f"{_b64encode(body)}.{_b64encode(envelope)}"


def verify_invite(
    token: str,
    *,
    verifier_key_records: Sequence[Mapping[str, Any]],
    now: float,
) -> dict[str, Any]:
    """Verify an invite against the seed's trusted public-key record(s)."""

    if (
        isinstance(now, bool)
        or not isinstance(now, (int, float))
        or not math.isfinite(float(now))
    ):
        raise InviteError("invite_time_invalid")
    if not isinstance(token, str) or token.count(".") != 1:
        raise InviteError("invite_malformed")
    encoded_body, _, encoded_signature = token.partition(".")
    if not encoded_body or not encoded_signature:
        raise InviteError("invite_malformed")
    body = _b64decode(encoded_body)
    payload = _strict_json_object(body)
    signature = _strict_json_object(_b64decode(encoded_signature))
    if payload.get("protocol") != _INVITE_PROTOCOL:
        raise InviteError("invite_protocol_invalid")
    if set(payload) != set(_PAYLOAD_FIELDS):
        raise InviteError("invite_malformed")
    if not all(
        isinstance(payload[field], str)
        and bool(payload[field])
        and payload[field] == payload[field].strip()
        for field in ("swarm_id", "seed_url", "nonce")
    ):
        raise InviteError("invite_malformed")
    issued_at = payload["issued_at"]
    expires_at = payload["expires_at"]
    if (
        isinstance(issued_at, bool)
        or not isinstance(issued_at, (int, float))
        or not math.isfinite(float(issued_at))
        or isinstance(expires_at, bool)
        or not isinstance(expires_at, (int, float))
        or not math.isfinite(float(expires_at))
        or float(expires_at) <= float(issued_at)
    ):
        raise InviteError("invite_malformed")

    try:
        verify = build_ed25519_verifier(verifier_key_records)
    except Exception as exc:  # noqa: BLE001 - bad key records fail closed
        raise InviteError("invite_verifier_invalid") from exc
    # Re-canonicalize the payload exactly as the signer did so the digest matches.
    if not verify(canonical_json_bytes(payload), signature):
        raise InviteError("invite_signature_invalid")

    if now > float(expires_at):
        raise InviteError("invite_expired")
    return payload


class InviteRegistry:
    """Tracks consumed invite nonces so a token is usable exactly once."""

    def __init__(self) -> None:
        self._consumed: dict[str, float] = {}

    def consume(self, nonce: str, now: float) -> None:
        if nonce in self._consumed:
            raise InviteError("invite_replayed")
        self._consumed[nonce] = now
