# SPDX-License-Identifier: AGPL-3.0-or-later
"""Recipient-bound encrypted pairing contracts for the macOS demo."""

from __future__ import annotations

import base64
from collections.abc import Mapping
import hashlib
import json
import math
import os
import re
from typing import Any

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric.x25519 import (
    X25519PrivateKey,
    X25519PublicKey,
)
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from mycelium_invite import INVITE_BUNDLE_PROTOCOL, InviteError, verify_invite_bundle
from mycelium_qualification.evidence import canonical_json_bytes
from mycelium_qualification.signing import (
    Ed25519EvidenceSigner,
    build_ed25519_verifier,
)

PAIRING_REQUEST_PROTOCOL = "mycelium.pairing_request.v1"
ENCRYPTED_HANDOFF_PROTOCOL = "mycelium.encrypted_invitation_handoff.v1"
_HANDOFF_PAYLOAD_PROTOCOL = "mycelium.encrypted_invitation_payload.v1"
_SUPPORTED_PEER_CLASS = "mac_mlx_iroh"
_REQUEST_TTL_MAX = 600
_MAX_HANDOFF_BYTES = 64 * 1024
_MAX_QUOTA_BYTES = 1024**4
_SEGMENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_REQUEST_FIELDS = frozenset(
    {
        "protocol",
        "node_id",
        "identity_endpoint_id",
        "identity_key_digest",
        "identity_verification_key",
        "peer_class",
        "invite_protocol",
        "recipient_encryption_key",
        "pairing_nonce",
        "issued_at",
        "expires_at",
        "signature",
    }
)
_HANDOFF_FIELDS = frozenset(
    {
        "protocol",
        "request_digest",
        "recipient_key_digest",
        "sender_ephemeral_key",
        "kdf_salt",
        "aead_nonce",
        "expires_at",
        "ciphertext",
    }
)
_PAYLOAD_FIELDS = frozenset(
    {
        "protocol",
        "request_digest",
        "node_id",
        "identity_key_digest",
        "pairing_nonce",
        "peer_class",
        "expires_at",
        "seed_key_digest",
        "quota",
        "invite_bundle",
    }
)
_QUOTA_FIELDS = frozenset(
    {"storage_bytes", "network_bytes_per_day", "background_allowed"}
)


class PairingError(RuntimeError):
    """Fail-closed pairing error carrying no private material."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _finite(value: Any) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
    ):
        raise PairingError("pairing_time_invalid")
    return float(value)


def _b64encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64decode(value: Any, *, length: int | None = None) -> bytes:
    if not isinstance(value, str) or not value or "=" in value:
        raise PairingError("pairing_handoff_invalid")
    try:
        raw = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except Exception as exc:  # noqa: BLE001 - crypto boundary
        raise PairingError("pairing_handoff_invalid") from exc
    if _b64encode(raw) != value or (length is not None and len(raw) != length):
        raise PairingError("pairing_handoff_invalid")
    return raw


def _digest(document: Mapping[str, Any]) -> str:
    return "sha256:" + hashlib.sha256(canonical_json_bytes(dict(document))).hexdigest()


def validate_quota(quota: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(quota, Mapping) or set(quota) != _QUOTA_FIELDS:
        raise PairingError("pairing_quota_invalid")
    result = dict(quota)
    for field in ("storage_bytes", "network_bytes_per_day"):
        value = result[field]
        if (
            isinstance(value, bool)
            or not isinstance(value, int)
            or value < 0
            or value > _MAX_QUOTA_BYTES
        ):
            raise PairingError("pairing_quota_invalid")
    if type(result["background_allowed"]) is not bool:
        raise PairingError("pairing_quota_invalid")
    return result


def new_pairing_request(
    *,
    signer: Ed25519EvidenceSigner,
    node_id: str,
    peer_class: str,
    now: float,
    ttl_seconds: int = 300,
    nonce: str | None = None,
) -> tuple[dict[str, Any], bytes]:
    """Create one target-signed request and its recipient-only X25519 key."""

    if not isinstance(signer, Ed25519EvidenceSigner):
        raise PairingError("pairing_identity_invalid")
    if not isinstance(node_id, str) or _SEGMENT_RE.fullmatch(node_id) is None:
        raise PairingError("pairing_request_invalid")
    if peer_class != _SUPPORTED_PEER_CLASS:
        raise PairingError("pairing_peer_class_unsupported")
    if (
        isinstance(ttl_seconds, bool)
        or not isinstance(ttl_seconds, int)
        or not 1 <= ttl_seconds <= _REQUEST_TTL_MAX
    ):
        raise PairingError("pairing_request_ttl_invalid")
    issued_at = _finite(now)
    pairing_nonce = 'pair-' + _b64encode(os.urandom(24)) if nonce is None else nonce
    if not isinstance(pairing_nonce, str) or _SEGMENT_RE.fullmatch(pairing_nonce) is None:
        raise PairingError("pairing_request_invalid")
    private = X25519PrivateKey.generate()
    public_bytes = private.public_key().public_bytes_raw()
    statement = {
        "protocol": PAIRING_REQUEST_PROTOCOL,
        "node_id": node_id,
        "identity_endpoint_id": signer.endpoint_id,
        "identity_key_digest": signer.verification_key_digest,
        "identity_verification_key": signer.public_key_record(),
        "peer_class": peer_class,
        "invite_protocol": INVITE_BUNDLE_PROTOCOL,
        "recipient_encryption_key": _b64encode(public_bytes),
        "pairing_nonce": pairing_nonce,
        "issued_at": issued_at,
        "expires_at": issued_at + ttl_seconds,
    }
    return {**statement, "signature": signer.sign(statement)}, private.private_bytes_raw()


def verify_pairing_request(
    request: Mapping[str, Any],
    *,
    now: float,
) -> dict[str, Any]:
    """Verify the closed request, permanent identity signature, and lifetime."""

    if not isinstance(request, Mapping) or set(request) != _REQUEST_FIELDS:
        raise PairingError("pairing_request_invalid")
    value = dict(request)
    if value.get("protocol") != PAIRING_REQUEST_PROTOCOL:
        raise PairingError("pairing_request_invalid")
    for field in ("node_id", "identity_endpoint_id", "pairing_nonce"):
        if not isinstance(value.get(field), str) or _SEGMENT_RE.fullmatch(value[field]) is None:
            raise PairingError("pairing_request_invalid")
    if (
        not isinstance(value.get("identity_key_digest"), str)
        or _DIGEST_RE.fullmatch(value["identity_key_digest"]) is None
        or not isinstance(value.get("identity_verification_key"), Mapping)
        or value["identity_verification_key"].get("verification_key_digest")
        != value["identity_key_digest"]
    ):
        raise PairingError("pairing_request_invalid")
    _b64decode(value.get("recipient_encryption_key"), length=32)
    issued = _finite(value.get("issued_at"))
    expires = _finite(value.get("expires_at"))
    instant = _finite(now)
    if expires <= issued or expires - issued > _REQUEST_TTL_MAX:
        raise PairingError("pairing_request_invalid")
    signature = value.get("signature")
    statement = {key: item for key, item in value.items() if key != "signature"}
    try:
        verify = build_ed25519_verifier([dict(value["identity_verification_key"])])
    except Exception as exc:  # noqa: BLE001 - external record
        raise PairingError("pairing_request_signature_invalid") from exc
    if (
        not isinstance(signature, dict)
        or signature.get("signer_endpoint_id") != value["identity_endpoint_id"]
        or not verify(canonical_json_bytes(statement), signature)
    ):
        raise PairingError("pairing_request_signature_invalid")
    if (
        value.get("peer_class") != _SUPPORTED_PEER_CLASS
        or value.get("invite_protocol") != INVITE_BUNDLE_PROTOCOL
    ):
        raise PairingError("pairing_request_invalid")
    if instant > expires:
        raise PairingError("pairing_request_expired")
    return value


def _derive_key(
    *,
    shared: bytes,
    salt: bytes,
    request_digest: str,
) -> bytes:
    return HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        info=b"mycelium.a13.pairing.handoff.v1\0" + request_digest.encode("ascii"),
    ).derive(shared)


def approve_pairing_request(
    *,
    request: Mapping[str, Any],
    invite_bundle: Mapping[str, Any],
    quota: Mapping[str, Any],
    now: float,
) -> dict[str, Any]:
    """Verify owner inputs and encrypt one complete A8 bundle to the target."""

    instant = _finite(now)
    verified_request = verify_pairing_request(request, now=instant)
    approved_quota = validate_quota(quota)
    try:
        verified_invite = verify_invite_bundle(invite_bundle, now=instant)
    except InviteError as exc:
        raise PairingError("pairing_invite_binding_invalid") from exc
    invite = verified_invite["payload"]
    if (
        invite["nonce"] != verified_request["pairing_nonce"]
        or float(invite["expires_at"]) > float(verified_request["expires_at"])
    ):
        raise PairingError("pairing_invite_binding_invalid")
    request_digest = _digest(verified_request)
    recipient_bytes = _b64decode(
        verified_request["recipient_encryption_key"], length=32
    )
    recipient = X25519PublicKey.from_public_bytes(recipient_bytes)
    sender_private = X25519PrivateKey.generate()
    sender_public = sender_private.public_key().public_bytes_raw()
    salt = os.urandom(32)
    aead_nonce = os.urandom(12)
    expires_at = float(invite["expires_at"])
    header = {
        "protocol": ENCRYPTED_HANDOFF_PROTOCOL,
        "request_digest": request_digest,
        "recipient_key_digest": "sha256:" + hashlib.sha256(recipient_bytes).hexdigest(),
        "sender_ephemeral_key": _b64encode(sender_public),
        "kdf_salt": _b64encode(salt),
        "aead_nonce": _b64encode(aead_nonce),
        "expires_at": expires_at,
    }
    payload = {
        "protocol": _HANDOFF_PAYLOAD_PROTOCOL,
        "request_digest": request_digest,
        "node_id": verified_request["node_id"],
        "identity_key_digest": verified_request["identity_key_digest"],
        "pairing_nonce": verified_request["pairing_nonce"],
        "peer_class": verified_request["peer_class"],
        "expires_at": expires_at,
        "seed_key_digest": verified_invite["seed_key_digest"],
        "quota": approved_quota,
        "invite_bundle": dict(invite_bundle),
    }
    key = _derive_key(
        shared=sender_private.exchange(recipient),
        salt=salt,
        request_digest=request_digest,
    )
    ciphertext = ChaCha20Poly1305(key).encrypt(
        aead_nonce,
        canonical_json_bytes(payload),
        canonical_json_bytes(header),
    )
    handoff = {**header, "ciphertext": _b64encode(ciphertext)}
    if len(canonical_json_bytes(handoff)) > _MAX_HANDOFF_BYTES:
        raise PairingError("pairing_handoff_too_large")
    return handoff


def _strict_json_object(raw: bytes) -> dict[str, Any]:
    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate")
            result[key] = value
        return result

    try:
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=pairs,
            parse_constant=lambda _value: (_ for _ in ()).throw(ValueError("constant")),
        )
        if not isinstance(value, dict) or canonical_json_bytes(value) != raw:
            raise ValueError("noncanonical")
    except Exception as exc:  # noqa: BLE001 - decrypted attacker-controlled bytes
        raise PairingError("pairing_handoff_invalid") from exc
    return value


def decrypt_invitation_handoff(
    *,
    request: Mapping[str, Any],
    handoff: Mapping[str, Any],
    recipient_private_key: bytes,
    now: float,
) -> dict[str, Any]:
    """Recipient-decrypt and verify every request, invite, quota, and pin binding."""

    instant = _finite(now)
    verified_request = verify_pairing_request(request, now=instant)
    if not isinstance(handoff, Mapping) or set(handoff) != _HANDOFF_FIELDS:
        raise PairingError("pairing_handoff_invalid")
    value = dict(handoff)
    request_digest = _digest(verified_request)
    if value.get("request_digest") != request_digest:
        raise PairingError("pairing_handoff_request_mismatch")
    if value.get("protocol") != ENCRYPTED_HANDOFF_PROTOCOL:
        raise PairingError("pairing_handoff_invalid")
    expires_at = _finite(value.get("expires_at"))
    if instant > expires_at:
        raise PairingError("pairing_handoff_expired")
    recipient_public = _b64decode(
        verified_request["recipient_encryption_key"], length=32
    )
    expected_recipient_digest = "sha256:" + hashlib.sha256(recipient_public).hexdigest()
    if value.get("recipient_key_digest") != expected_recipient_digest:
        raise PairingError("pairing_handoff_recipient_mismatch")
    if not isinstance(recipient_private_key, bytes) or len(recipient_private_key) != 32:
        raise PairingError("pairing_handoff_recipient_mismatch")
    try:
        private = X25519PrivateKey.from_private_bytes(recipient_private_key)
        if private.public_key().public_bytes_raw() != recipient_public:
            raise PairingError("pairing_handoff_recipient_mismatch")
        sender_public = X25519PublicKey.from_public_bytes(
            _b64decode(value.get("sender_ephemeral_key"), length=32)
        )
        salt = _b64decode(value.get("kdf_salt"), length=32)
        nonce = _b64decode(value.get("aead_nonce"), length=12)
        ciphertext = _b64decode(value.get("ciphertext"))
        header = {key: item for key, item in value.items() if key != "ciphertext"}
        key = _derive_key(
            shared=private.exchange(sender_public),
            salt=salt,
            request_digest=request_digest,
        )
        raw = ChaCha20Poly1305(key).decrypt(
            nonce,
            ciphertext,
            canonical_json_bytes(header),
        )
    except PairingError:
        raise
    except (InvalidTag, ValueError, TypeError) as exc:
        raise PairingError("pairing_handoff_invalid") from exc
    payload = _strict_json_object(raw)
    if set(payload) != _PAYLOAD_FIELDS or payload.get("protocol") != _HANDOFF_PAYLOAD_PROTOCOL:
        raise PairingError("pairing_handoff_invalid")
    try:
        verified_invite = verify_invite_bundle(payload["invite_bundle"], now=instant)
    except (InviteError, KeyError, TypeError) as exc:
        raise PairingError("pairing_handoff_invalid") from exc
    invite = verified_invite["payload"]
    validate_quota(payload.get("quota"))
    expected = (
        payload.get("request_digest") == request_digest
        and payload.get("node_id") == verified_request["node_id"]
        and payload.get("identity_key_digest") == verified_request["identity_key_digest"]
        and payload.get("pairing_nonce") == verified_request["pairing_nonce"]
        and payload.get("peer_class") == verified_request["peer_class"]
        and payload.get("expires_at") == expires_at
        and float(invite["expires_at"]) == expires_at
        and invite["nonce"] == verified_request["pairing_nonce"]
        and payload.get("seed_key_digest") == verified_invite["seed_key_digest"]
    )
    if not expected:
        raise PairingError("pairing_handoff_binding_invalid")
    return payload


__all__ = [
    "ENCRYPTED_HANDOFF_PROTOCOL",
    "PAIRING_REQUEST_PROTOCOL",
    "PairingError",
    "approve_pairing_request",
    "decrypt_invitation_handoff",
    "new_pairing_request",
    "validate_quota",
    "verify_pairing_request",
]
