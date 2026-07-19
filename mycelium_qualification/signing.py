"""Ed25519 helpers for canonical physical qualification evidence."""
from __future__ import annotations

import base64
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Callable

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from .evidence import EvidenceValidationError, canonical_json_bytes, sha256_bytes

SIGNATURE_FIELDS = frozenset(
    {
        "algorithm",
        "signer_endpoint_id",
        "verification_key_digest",
        "signed_statement_digest",
        "signature",
    }
)
_PUBLIC_KEY_FIELDS = frozenset(
    {
        "algorithm",
        "encoding",
        "verification_key",
        "verification_key_digest",
    }
)
_SIGNATURE_DOMAIN = "mycelium.ed25519_evidence_signature.v1"


class EvidenceSigningError(ValueError):
    """Fail-closed signing or public-key validation error with a stable code."""

    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        self.detail = detail
        super().__init__(code if not detail else f"{code}: {detail}")


def _require(condition: bool, code: str, detail: str = "") -> None:
    if not condition:
        raise EvidenceSigningError(code, detail)


def _signature_payload(
    *,
    signer_endpoint_id: str,
    verification_key_digest: str,
    signed_statement_digest: str,
) -> bytes:
    return canonical_json_bytes(
        {
            "algorithm": "ed25519",
            "protocol": _SIGNATURE_DOMAIN,
            "signed_statement_digest": signed_statement_digest,
            "signer_endpoint_id": signer_endpoint_id,
            "verification_key_digest": verification_key_digest,
        }
    )


def _decode_base64(value: Any, *, code: str) -> bytes:
    _require(isinstance(value, str) and bool(value), code)
    try:
        return base64.b64decode(value, validate=True)
    except (ValueError, TypeError) as exc:
        raise EvidenceSigningError(code) from exc


@dataclass(frozen=True, slots=True)
class Ed25519EvidenceSigner:
    """Process-local signer; private key has no serialization-facing API."""

    endpoint_id: str
    _private_key: Ed25519PrivateKey
    _public_key_bytes: bytes

    @property
    def verification_key_digest(self) -> str:
        return sha256_bytes(self._public_key_bytes)

    def public_key_record(self) -> dict[str, str]:
        return {
            "algorithm": "ed25519",
            "encoding": "base64",
            "verification_key": base64.b64encode(self._public_key_bytes).decode("ascii"),
            "verification_key_digest": self.verification_key_digest,
        }

    def sign(self, statement: Mapping[str, Any]) -> dict[str, str]:
        _require(isinstance(statement, Mapping), "invalid_signed_statement")
        try:
            statement_bytes = canonical_json_bytes(dict(statement))
        except EvidenceValidationError as exc:
            raise EvidenceSigningError("invalid_signed_statement", exc.code) from exc
        statement_digest = sha256_bytes(statement_bytes)
        payload = _signature_payload(
            signer_endpoint_id=self.endpoint_id,
            verification_key_digest=self.verification_key_digest,
            signed_statement_digest=statement_digest,
        )
        signature = self._private_key.sign(payload)
        return {
            "algorithm": "ed25519",
            "signer_endpoint_id": self.endpoint_id,
            "verification_key_digest": self.verification_key_digest,
            "signed_statement_digest": statement_digest,
            "signature": base64.b64encode(signature).decode("ascii"),
        }


def generate_ed25519_signer(*, endpoint_id: str) -> Ed25519EvidenceSigner:
    """Generate one ephemeral process-local signer for an Iroh endpoint."""
    _require(
        isinstance(endpoint_id, str) and bool(endpoint_id.strip()),
        "invalid_signer_endpoint_id",
    )
    private_key = Ed25519PrivateKey.generate()
    return Ed25519EvidenceSigner(
        endpoint_id=endpoint_id,
        _private_key=private_key,
        _public_key_bytes=private_key.public_key().public_bytes_raw(),
    )


def _load_public_key(record: Mapping[str, Any]) -> tuple[str, Ed25519PublicKey]:
    _require(
        isinstance(record, Mapping) and set(record) == _PUBLIC_KEY_FIELDS,
        "invalid_verification_key_record",
    )
    _require(
        record["algorithm"] == "ed25519" and record["encoding"] == "base64",
        "invalid_verification_key_record",
    )
    public_key_bytes = _decode_base64(
        record["verification_key"], code="invalid_verification_key_record"
    )
    _require(len(public_key_bytes) == 32, "invalid_verification_key_record")
    expected_digest = sha256_bytes(public_key_bytes)
    _require(
        record["verification_key_digest"] == expected_digest,
        "verification_key_digest_mismatch",
    )
    try:
        public_key = Ed25519PublicKey.from_public_bytes(public_key_bytes)
    except ValueError as exc:
        raise EvidenceSigningError("invalid_verification_key_record") from exc
    return expected_digest, public_key


def build_ed25519_verifier(
    records: Iterable[Mapping[str, Any]],
) -> Callable[[bytes, dict[str, Any]], bool]:
    """Build the callback shape consumed by RouteQualificationV1 authority."""
    keyring: dict[str, Ed25519PublicKey] = {}
    for record in records:
        digest, public_key = _load_public_key(record)
        _require(digest not in keyring, "duplicate_verification_key")
        keyring[digest] = public_key
    _require(bool(keyring), "empty_verification_keyring")

    def verify(statement_bytes: bytes, signature: dict[str, Any]) -> bool:
        if not isinstance(statement_bytes, bytes):
            return False
        if not isinstance(signature, dict) or set(signature) != SIGNATURE_FIELDS:
            return False
        if signature.get("algorithm") != "ed25519":
            return False
        endpoint_id = signature.get("signer_endpoint_id")
        key_digest = signature.get("verification_key_digest")
        claimed_statement_digest = signature.get("signed_statement_digest")
        if not isinstance(endpoint_id, str) or not endpoint_id:
            return False
        if not isinstance(key_digest, str) or key_digest not in keyring:
            return False
        if not isinstance(claimed_statement_digest, str):
            return False
        if claimed_statement_digest != sha256_bytes(statement_bytes):
            return False
        try:
            signature_bytes = _decode_base64(
                signature.get("signature"), code="invalid_signature_encoding"
            )
            if len(signature_bytes) != 64:
                return False
            payload = _signature_payload(
                signer_endpoint_id=endpoint_id,
                verification_key_digest=key_digest,
                signed_statement_digest=claimed_statement_digest,
            )
            keyring[key_digest].verify(signature_bytes, payload)
        except (EvidenceSigningError, EvidenceValidationError, InvalidSignature, ValueError):
            return False
        return True

    return verify
