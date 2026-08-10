"""Signed, time-bounded authority envelope for one atomic Gossip snapshot."""
from __future__ import annotations

import copy
from dataclasses import dataclass
import hashlib
import json
from typing import TYPE_CHECKING, Any, Mapping

from mycelium_capacity_profiles import CapacityProfile, parse_capacity_profile_bytes

from .evidence_bundle import (
    EvidenceBundle,
    EvidenceBundleError,
    evidence_bundle_from_dict,
    evidence_bundle_to_dict,
)
from .schema import canonical_json_bytes

if TYPE_CHECKING:
    from mycelium_qualification.signing import Ed25519EvidenceSigner


SIGNED_EVIDENCE_BUNDLE_PROTOCOL = "mycelium.gossip.signed_evidence_bundle.v1"
SIGNED_EVIDENCE_STATEMENT_KIND = "mycelium.gossip.evidence_bundle_statement.v1"
_ENVELOPE_FIELDS = frozenset(
    {
        "protocol",
        "statement",
        "evidence_bundle",
        "capacity_profiles",
        "signature",
        "verification_key",
    }
)
_STATEMENT_FIELDS = frozenset(
    {
        "kind",
        "swarm_id",
        "deployment_id",
        "deployment_epoch",
        "snapshot_generation",
        "evidence_bundle_digest",
        "capacity_profiles_digest",
        "captured_at_unix_ms",
        "valid_until_unix_ms",
        "authority_generation",
        "signer_endpoint_id",
    }
)


class SignedEvidenceBundleError(ValueError):
    """Stable fail-closed error for evidence authority validation."""


@dataclass(frozen=True, slots=True)
class ValidatedSignedEvidence:
    bundle: EvidenceBundle
    capacity_profiles: Mapping[str, CapacityProfile]
    statement: Mapping[str, Any]
    verification_key: Mapping[str, Any]


def _exact_nonnegative_integer(value: object, name: str, *, positive: bool = False) -> int:
    if type(value) is not int or value < (1 if positive else 0):
        qualifier = "positive" if positive else "non-negative"
        raise SignedEvidenceBundleError(f"{name} must be an exact {qualifier} integer")
    return value


def _statement_for(
    bundle: EvidenceBundle,
    *,
    captured_at_unix_ms: int,
    valid_until_unix_ms: int,
    authority_generation: int,
    signer_endpoint_id: str,
    capacity_profiles_digest: str,
) -> dict[str, Any]:
    return {
        "kind": SIGNED_EVIDENCE_STATEMENT_KIND,
        "swarm_id": bundle.swarm_id,
        "deployment_id": bundle.deployment["deployment_id"],
        "deployment_epoch": bundle.deployment["deployment_epoch"],
        "snapshot_generation": bundle.snapshot_generation,
        "evidence_bundle_digest": bundle.evidence_bundle_digest,
        "capacity_profiles_digest": capacity_profiles_digest,
        "captured_at_unix_ms": captured_at_unix_ms,
        "valid_until_unix_ms": valid_until_unix_ms,
        "authority_generation": authority_generation,
        "signer_endpoint_id": signer_endpoint_id,
    }


def _capacity_profile_documents(
    values: Mapping[str, CapacityProfile | bytes | Mapping[str, Any]],
) -> tuple[dict[str, dict[str, Any]], dict[str, CapacityProfile], str]:
    if not isinstance(values, Mapping) or not values:
        raise SignedEvidenceBundleError("capacity profiles must be a non-empty mapping")
    documents: dict[str, dict[str, Any]] = {}
    profiles: dict[str, CapacityProfile] = {}
    for node_id in sorted(values):
        if not isinstance(node_id, str) or not node_id or len(node_id) > 128:
            raise SignedEvidenceBundleError("capacity profile node id is invalid")
        value = values[node_id]
        try:
            if isinstance(value, CapacityProfile):
                profile = value
            elif isinstance(value, bytes):
                profile = parse_capacity_profile_bytes(value)
            elif isinstance(value, Mapping):
                payload = json.dumps(
                    dict(value),
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=False,
                    allow_nan=False,
                ).encode("utf-8")
                profile = parse_capacity_profile_bytes(payload)
            else:
                raise ValueError("unsupported capacity profile value")
        except (TypeError, ValueError, RecursionError) as exc:
            raise SignedEvidenceBundleError("capacity profile is invalid") from exc
        documents[node_id] = profile.to_document()
        profiles[node_id] = profile
    digest = "sha256:" + hashlib.sha256(canonical_json_bytes(documents)).hexdigest()
    return documents, profiles, digest


def seal_evidence_bundle(
    bundle_value: EvidenceBundle | Mapping[str, Any],
    *,
    signer: "Ed25519EvidenceSigner",
    captured_at_unix_ms: int,
    valid_for_ms: int,
    authority_generation: int,
    capacity_profiles: Mapping[str, CapacityProfile | bytes | Mapping[str, Any]],
) -> dict[str, Any]:
    """Sign one canonical bundle and bind its source, generation, and validity window."""

    bundle = (
        bundle_value
        if isinstance(bundle_value, EvidenceBundle)
        else evidence_bundle_from_dict(copy.deepcopy(dict(bundle_value)))
    )
    captured = _exact_nonnegative_integer(captured_at_unix_ms, "captured_at_unix_ms")
    lifetime = _exact_nonnegative_integer(valid_for_ms, "valid_for_ms", positive=True)
    authority = _exact_nonnegative_integer(
        authority_generation, "authority_generation", positive=True
    )
    valid_until = captured + lifetime
    if valid_until > 9_007_199_254_740_991:
        raise SignedEvidenceBundleError("valid_until_unix_ms exceeds safe integer range")
    profile_documents, _, profiles_digest = _capacity_profile_documents(capacity_profiles)
    statement = _statement_for(
        bundle,
        captured_at_unix_ms=captured,
        valid_until_unix_ms=valid_until,
        authority_generation=authority,
        signer_endpoint_id=signer.endpoint_id,
        capacity_profiles_digest=profiles_digest,
    )
    return {
        "protocol": SIGNED_EVIDENCE_BUNDLE_PROTOCOL,
        "statement": statement,
        "evidence_bundle": evidence_bundle_to_dict(bundle),
        "capacity_profiles": profile_documents,
        "signature": signer.sign(statement),
        "verification_key": signer.public_key_record(),
    }


def validate_signed_evidence_bundle(
    document: Mapping[str, Any],
    *,
    expected_verification_key_digest: str,
    now_unix_ms: int,
) -> ValidatedSignedEvidence:
    """Verify authority, freshness, and exact source binding, returning the inner bundle."""

    if not isinstance(document, Mapping) or set(document) != _ENVELOPE_FIELDS:
        raise SignedEvidenceBundleError("signed evidence bundle shape is invalid")
    if document.get("protocol") != SIGNED_EVIDENCE_BUNDLE_PROTOCOL:
        raise SignedEvidenceBundleError("signed evidence bundle protocol is invalid")
    statement = document.get("statement")
    if not isinstance(statement, Mapping) or set(statement) != _STATEMENT_FIELDS:
        raise SignedEvidenceBundleError("signed evidence statement shape is invalid")
    if statement.get("kind") != SIGNED_EVIDENCE_STATEMENT_KIND:
        raise SignedEvidenceBundleError("signed evidence statement kind is invalid")
    captured = _exact_nonnegative_integer(
        statement.get("captured_at_unix_ms"), "captured_at_unix_ms"
    )
    valid_until = _exact_nonnegative_integer(
        statement.get("valid_until_unix_ms"), "valid_until_unix_ms", positive=True
    )
    authority_generation = _exact_nonnegative_integer(
        statement.get("authority_generation"), "authority_generation", positive=True
    )
    now = _exact_nonnegative_integer(now_unix_ms, "now_unix_ms")
    if valid_until <= captured or now < captured or now >= valid_until:
        raise SignedEvidenceBundleError("signed evidence bundle is not current")

    verification_key = document.get("verification_key")
    signature = document.get("signature")
    if not isinstance(verification_key, Mapping) or not isinstance(signature, Mapping):
        raise SignedEvidenceBundleError("signed evidence authority is invalid")
    if verification_key.get("verification_key_digest") != expected_verification_key_digest:
        raise SignedEvidenceBundleError("signed evidence authority is not trusted")
    if (
        signature.get("verification_key_digest") != expected_verification_key_digest
        or signature.get("signer_endpoint_id") != statement.get("signer_endpoint_id")
    ):
        raise SignedEvidenceBundleError("signed evidence source binding is invalid")
    try:
        from mycelium_qualification.signing import (
            EvidenceSigningError,
            build_ed25519_verifier,
        )

        verifier = build_ed25519_verifier([verification_key])
        verified = verifier(canonical_json_bytes(statement), dict(signature))
    except (EvidenceSigningError, TypeError, ValueError) as exc:
        raise SignedEvidenceBundleError("signed evidence signature is invalid") from exc
    if verified is not True:
        raise SignedEvidenceBundleError("signed evidence signature is invalid")

    try:
        bundle = evidence_bundle_from_dict(document.get("evidence_bundle"))
    except (EvidenceBundleError, TypeError, ValueError) as exc:
        raise SignedEvidenceBundleError("signed evidence bundle payload is invalid") from exc
    try:
        profile_documents, profiles, profiles_digest = _capacity_profile_documents(
            document.get("capacity_profiles")
        )
    except (SignedEvidenceBundleError, TypeError) as exc:
        raise SignedEvidenceBundleError("signed capacity profiles are invalid") from exc
    if document.get("capacity_profiles") != profile_documents:
        raise SignedEvidenceBundleError("signed capacity profiles are not canonical")
    expected_statement = _statement_for(
        bundle,
        captured_at_unix_ms=captured,
        valid_until_unix_ms=valid_until,
        authority_generation=authority_generation,
        signer_endpoint_id=statement["signer_endpoint_id"],
        capacity_profiles_digest=profiles_digest,
    )
    if dict(statement) != expected_statement:
        raise SignedEvidenceBundleError("signed evidence statement does not bind its bundle")

    for record in bundle.records:
        generated = record.get("generated_at_unix_ms")
        ttl_ms = record.get("ttl_ms")
        if (
            type(generated) is not int
            or type(ttl_ms) is not int
            or generated > captured
            or captured >= generated + ttl_ms
        ):
            raise SignedEvidenceBundleError("signed evidence bundle contains stale records")
    return ValidatedSignedEvidence(
        bundle=bundle,
        capacity_profiles=profiles,
        statement=copy.deepcopy(dict(statement)),
        verification_key=copy.deepcopy(dict(verification_key)),
    )


__all__ = [
    "SIGNED_EVIDENCE_BUNDLE_PROTOCOL",
    "SIGNED_EVIDENCE_STATEMENT_KIND",
    "SignedEvidenceBundleError",
    "ValidatedSignedEvidence",
    "seal_evidence_bundle",
    "validate_signed_evidence_bundle",
]
