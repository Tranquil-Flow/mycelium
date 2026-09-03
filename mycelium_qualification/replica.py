"""A5 replica-track qualification leaf (pure, isolated).

Only the qualifier may mark a replica track qualified. This module provides
the deterministic decision function that turns structured qualification
evidence into a closed ``mycelium.replica_qualification.v1`` document.

Plan note: the A5 finish plan named this file ``mycelium_qualifier/replica.py``;
the repository's qualifier package is ``mycelium_qualification``, so the leaf
lives here. No existing qualifier modules are modified.

Design rules (spec §2, §7, §10):
- The document this module returns MUST pass
  ``mycelium_replica_contracts.validate_replica_qualification``; the module
  validates its own output and raises if a bug would emit an invalid document.
- ``route_ready`` is true iff there are zero rejection reasons.
- Rejection reasons are sorted and unique.
- Generation fencing: a non-positive qualifier generation or an expiry at or
  before issuance is stale authority and fails closed.
- Unknown failure-domain information stays unknown; this module neither infers
  nor fabricates diversity facts.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

from mycelium_replica_contracts import (
    REPLICA_QUALIFICATION_PROTOCOL,
    replica_qualification_digest,
    validate_replica_qualification,
)


def _is_digest(value: str) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 71
        and value.startswith("sha256:")
        and all(character in "0123456789abcdef" for character in value[7:])
    )


@dataclass(frozen=True)
class ReplicaQualificationInput:
    """Structured evidence the qualifier consumes.

    All fields are exactly what the closed qualification document carries,
    minus the identifiers the qualifier derives (qualification_id /
    qualification_digest / route_ready / rejected_reasons).
    """

    deployment_id: str
    deployment_epoch: int
    replica_group_id: str
    placement_id: str
    placement_ids: tuple[str, ...]
    track_id: str
    traffic_fraction: float
    qualifier_generation: int
    issued_at_unix_ms: int
    expires_at_unix_ms: int
    evidence_bundle_digest: str
    load_proof_digest: str
    assignment_digest: str
    artifact_verification_digest: str
    parity_verified: bool
    startup_challenge_passed: bool
    memory_within_bounds: bool
    cleanup_within_bounds: bool
    directed_link_qualified: bool
    workload_envelope_digest: str


def _rejection_reasons(data: ReplicaQualificationInput) -> list[str]:
    reasons: list[str] = []
    if data.qualifier_generation <= 0:
        reasons.append("stale_authority")
    if data.expires_at_unix_ms <= data.issued_at_unix_ms:
        reasons.append("stale_authority")
    if not data.parity_verified:
        reasons.append("parity_mismatch")
    if not data.startup_challenge_passed:
        reasons.append("startup_challenge_failed")
    if not data.memory_within_bounds:
        reasons.append("memory_budget_exceeded")
    if not data.cleanup_within_bounds:
        reasons.append("cleanup_budget_exceeded")
    if not data.directed_link_qualified:
        reasons.append("directed_link_unqualified")
    return sorted(set(reasons))


def qualify_replica_track(
    data: ReplicaQualificationInput,
    *,
    extra_rejections: Iterable[str] = (),
) -> dict[str, Any]:
    """Decide replica-track qualification and emit a closed document.

    ``extra_rejections`` lets callers add bounded reason strings (for example
    ``replica_loss`` or ``owner_authority_missing`` from a later gate); they
    are merged, sorted, and deduplicated with the evidence-derived reasons.

    Returns a valid ``mycelium.replica_qualification.v1`` document. The
    document's ``qualification_id`` and ``qualification_digest`` are the
    canonical digest of the document itself with both fields blanked — the
    same self-binding A3 uses, so the digest never depends on itself.
    """
    reasons = _rejection_reasons(data)
    for extra in extra_rejections:
        if not isinstance(extra, str) or not extra:
            raise ValueError("extra rejections must be non-empty strings")
        reasons.append(extra)
    reasons = sorted(set(reasons))

    # Malformed evidence digests cannot legally appear in the closed document;
    # reject at the input boundary (fail closed, preserve any incumbent).
    for field in (
        "evidence_bundle_digest",
        "load_proof_digest",
        "assignment_digest",
        "artifact_verification_digest",
        "workload_envelope_digest",
    ):
        if not _is_digest(getattr(data, field)):
            raise ValueError(f"malformed evidence digest in {field}")

    document: dict[str, Any] = {
        "protocol": REPLICA_QUALIFICATION_PROTOCOL,
        "qualification_id": "",
        "qualification_digest": "",
        "deployment_id": data.deployment_id,
        "deployment_epoch": data.deployment_epoch,
        "replica_group_id": data.replica_group_id,
        "placement_id": data.placement_id,
        "placement_ids": list(data.placement_ids),
        "track_id": data.track_id,
        "traffic_fraction": data.traffic_fraction,
        "qualifier_generation": data.qualifier_generation,
        "issued_at_unix_ms": data.issued_at_unix_ms,
        "expires_at_unix_ms": data.expires_at_unix_ms,
        "evidence_bundle_digest": data.evidence_bundle_digest,
        "load_proof_digest": data.load_proof_digest,
        "assignment_digest": data.assignment_digest,
        "artifact_verification_digest": data.artifact_verification_digest,
        "parity_verified": data.parity_verified,
        "startup_challenge_passed": data.startup_challenge_passed,
        "memory_within_bounds": data.memory_within_bounds,
        "cleanup_within_bounds": data.cleanup_within_bounds,
        "directed_link_qualified": data.directed_link_qualified,
        "workload_envelope_digest": data.workload_envelope_digest,
        "rejected_reasons": reasons,
        "route_ready": not reasons,
    }
    digest = replica_qualification_digest(document)
    document["qualification_id"] = digest
    document["qualification_digest"] = digest

    # Fail closed: this module must never emit an invalid document.
    validated = validate_replica_qualification(document)
    if validated != document:
        raise RuntimeError("replica qualifier produced non-canonical document")
    return document


__all__ = [
    "ReplicaQualificationInput",
    "qualify_replica_track",
]
