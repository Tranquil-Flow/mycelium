# SPDX-License-Identifier: AGPL-3.0-only
"""``mycelium.peer_class_qualification.v1`` — role-by-role class qualification.

Spec §2.4, §4.3, §5.  The class qualifier binds one *exact*
platform/runtime/transport/build tuple to one role and to executed positive and
negative physical evidence.  Each role is independent: a qualification for one
role can never grant another, and no qualification can issue placement,
artifact, route-readiness, registration, or selection authority (frozen
decision ``class_specific_qualification``).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from .primitives import (
    ARCHITECTURES,
    CLASS_QUALIFICATION_PROTOCOL,
    PLATFORM_FAMILIES,
    QUALIFIABLE_ROLES,
    RUNTIME_BACKEND_STAGES,
    TRANSPORT_FAMILIES,
    bounded_int,
    canonical_bytes,
    closed_fields,
    digest,
    enum_value,
    finite,
    record_digest,
    require,
    require_distinct,
    segment,
    text,
    unique_names,
)

PROTOCOL = CLASS_QUALIFICATION_PROTOCOL

DECISIONS = frozenset({"qualified", "not_qualified", "unknown"})

QUALIFICATION_FIELDS = frozenset(
    {
        "protocol",
        "qualification_id",
        "role",
        "subject",
        "exact_class",
        "decision",
        "blockers",
        "evidence",
        "constraints",
        "issued_at",
        "expires_at",
    }
)
SUBJECT_FIELDS = frozenset(
    {"node_id", "profile_digest", "incarnation", "membership_generation"}
)
EXACT_CLASS_FIELDS = frozenset(
    {
        "platform_family",
        "architecture",
        "transport_family",
        "runtime_backend_id",
        "build_digest",
    }
)
EVIDENCE_FIELDS = frozenset(
    {"profile_digest", "evidence_generation", "positive_digests", "negative_digests"}
)
#: Fields that would smuggle a later authority into a qualification.
FORBIDDEN_AUTHORITY_FIELDS = frozenset(
    {"route_ready", "placement", "registration", "selection", "artifact_grant"}
)


@dataclass(frozen=True, slots=True)
class ExactClass:
    platform_family: str
    architecture: str
    transport_family: str
    runtime_backend_id: str
    build_digest: str

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "ExactClass":
        record = closed_fields(value, EXACT_CLASS_FIELDS, "qualification_class_invalid")
        backend_id = record["runtime_backend_id"]
        require(
            isinstance(backend_id, str)
            and (backend_id in RUNTIME_BACKEND_STAGES or backend_id == "unknown"),
            "qualification_backend_not_registered",
        )
        require(
            backend_id == "unknown" or RUNTIME_BACKEND_STAGES[backend_id] == "implemented",
            "qualification_backend_not_implemented",
        )
        return cls(
            platform_family=enum_value(
                record["platform_family"], PLATFORM_FAMILIES, "qualification_platform_invalid"
            ),
            architecture=enum_value(
                record["architecture"], ARCHITECTURES, "qualification_architecture_invalid"
            ),
            transport_family=enum_value(
                record["transport_family"],
                TRANSPORT_FAMILIES,
                "qualification_transport_invalid",
            ),
            runtime_backend_id=backend_id,
            build_digest=digest(record["build_digest"], "qualification_build_invalid"),
        )

    def to_mapping(self) -> dict[str, Any]:
        return {
            "platform_family": self.platform_family,
            "architecture": self.architecture,
            "transport_family": self.transport_family,
            "runtime_backend_id": self.runtime_backend_id,
            "build_digest": self.build_digest,
        }

    @property
    def has_unknown_member(self) -> bool:
        return "unknown" in {
            self.platform_family,
            self.architecture,
            self.transport_family,
            self.runtime_backend_id,
        }


@dataclass(frozen=True, slots=True)
class ClassQualification:
    qualification_id: str
    role: str
    subject_node_id: str
    subject_profile_digest: str
    subject_incarnation: str
    subject_membership_generation: int
    exact_class: ExactClass
    decision: str
    blockers: tuple[str, ...]
    evidence_profile_digest: str
    evidence_generation: int
    positive_digests: tuple[str, ...]
    negative_digests: tuple[str, ...]
    constraints: tuple[str, ...]
    issued_at: float
    expires_at: float

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "ClassQualification":
        require(isinstance(value, Mapping), "qualification_invalid")
        _reject_forbidden_authority_fields(value)
        record = closed_fields(value, QUALIFICATION_FIELDS, "qualification_invalid")
        require(record["protocol"] == PROTOCOL, "qualification_protocol_invalid")
        subject = closed_fields(record["subject"], SUBJECT_FIELDS, "qualification_subject_invalid")
        evidence = closed_fields(
            record["evidence"], EVIDENCE_FIELDS, "qualification_evidence_invalid"
        )
        decision = enum_value(record["decision"], DECISIONS, "qualification_decision_invalid")
        blockers = unique_names(record["blockers"], "qualification_blockers_invalid")
        positive = tuple(
            digest(item, "qualification_positive_digest_invalid")
            for item in unique_names(
                evidence["positive_digests"], "qualification_positive_digests_invalid"
            )
        )
        negative = tuple(
            digest(item, "qualification_negative_digest_invalid")
            for item in unique_names(
                evidence["negative_digests"], "qualification_negative_digests_invalid"
            )
        )
        require_distinct(positive, "qualification_positive_duplicate")
        require_distinct(negative, "qualification_negative_duplicate")
        subject_digest = digest(
            subject["profile_digest"], "qualification_subject_digest_invalid"
        )
        evidence_digest = digest(
            evidence["profile_digest"], "qualification_evidence_digest_invalid"
        )
        require(
            subject_digest == evidence_digest, "qualification_profile_binding_mismatch"
        )
        issued_at = finite(record["issued_at"], "qualification_issued_at_invalid", minimum=0.0)
        expires_at = finite(
            record["expires_at"], "qualification_expires_at_invalid", minimum=0.0
        )
        require(expires_at > issued_at, "qualification_validity_invalid")
        exact_class = ExactClass.from_mapping(record["exact_class"])
        if decision == "qualified":
            require(
                len(positive) >= 1 and len(negative) >= 1,
                "qualification_requires_executed_positive_and_negative_evidence",
            )
            require(not blockers, "qualification_cannot_be_qualified_with_blockers")
            require(
                not exact_class.has_unknown_member,
                "qualification_unknown_class_cannot_be_qualified",
            )
        else:
            require(bool(blockers), "qualification_requires_blockers")
        return cls(
            qualification_id=segment(
                record["qualification_id"], "qualification_id_invalid"
            ),
            role=enum_value(record["role"], QUALIFIABLE_ROLES, "qualification_role_invalid"),
            subject_node_id=segment(subject["node_id"], "qualification_node_id_invalid"),
            subject_profile_digest=subject_digest,
            subject_incarnation=segment(
                subject["incarnation"], "qualification_incarnation_invalid"
            ),
            subject_membership_generation=bounded_int(
                subject["membership_generation"],
                "qualification_generation_invalid",
                minimum=1,
                maximum=1 << 40,
            ),
            exact_class=exact_class,
            decision=decision,
            blockers=blockers,
            evidence_profile_digest=evidence_digest,
            evidence_generation=bounded_int(
                evidence["evidence_generation"],
                "qualification_evidence_generation_invalid",
                minimum=1,
                maximum=1 << 40,
            ),
            positive_digests=positive,
            negative_digests=negative,
            constraints=unique_names(record["constraints"], "qualification_constraints_invalid"),
            issued_at=issued_at,
            expires_at=expires_at,
        )

    def to_mapping(self) -> dict[str, Any]:
        return {
            "protocol": PROTOCOL,
            "qualification_id": self.qualification_id,
            "role": self.role,
            "subject": {
                "node_id": self.subject_node_id,
                "profile_digest": self.subject_profile_digest,
                "incarnation": self.subject_incarnation,
                "membership_generation": self.subject_membership_generation,
            },
            "exact_class": self.exact_class.to_mapping(),
            "decision": self.decision,
            "blockers": list(self.blockers),
            "evidence": {
                "profile_digest": self.evidence_profile_digest,
                "evidence_generation": self.evidence_generation,
                "positive_digests": list(self.positive_digests),
                "negative_digests": list(self.negative_digests),
            },
            "constraints": list(self.constraints),
            "issued_at": self.issued_at,
            "expires_at": self.expires_at,
        }

    def canonical_bytes(self) -> bytes:
        return canonical_bytes(self.to_mapping(), "qualification_noncanonical")

    @property
    def digest(self) -> str:
        return record_digest(self.to_mapping(), "qualification_noncanonical")

    def grants(self, role: str) -> bool:
        """A qualification grants exactly one role — never a transitively related one."""

        return self.decision == "qualified" and self.role == role

    def is_current(self, *, now: float, membership_generation: int) -> bool:
        return now <= self.expires_at and self.subject_membership_generation == membership_generation

    def matches_profile(self, profile: object) -> bool:
        """Requalification trigger: any profile digest change invalidates this class."""

        return self.subject_profile_digest == getattr(profile, "digest", None)


def _reject_forbidden_authority_fields(record: Mapping[str, Any]) -> None:
    require(
        not (set(record) & FORBIDDEN_AUTHORITY_FIELDS),
        "qualification_must_not_carry_later_authority",
    )


def validate_qualification_record(value: Mapping[str, Any]) -> ClassQualification:
    """Module entry point used by the acceptance tests."""

    return ClassQualification.from_mapping(value)
