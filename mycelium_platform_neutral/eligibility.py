# SPDX-License-Identifier: AGPL-3.0-only
"""``mycelium.peer_eligibility.v1`` — computed, fail-closed eligibility ladder.

Spec §5, §4.4.  Each of the six roles is decided independently from current
signed evidence and an exact class-specific qualification.  Eligibility is
*derived*, never granted: membership, platform recognition, installed code, a
synthetic fixture, or an accepted Router frame alone cannot advance a role.

Unknown platform, architecture, runtime, transport, lifecycle semantics, source
kind, qualification class, or required measurement is ``ineligible`` for
activation — never guessed; a current member with no evidence is ineligible.
The result carries no route-ready boolean and no placement, artifact,
registration, or selection authority (frozen decisions
``separated_capability_authority`` and ``unknown_remains_ineligible``).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from .capability_report import CapabilityReport
from .class_qualification import ClassQualification
from .peer_profile import PeerProfile
from .primitives import (
    ELIGIBILITY_PROTOCOL,
    ELIGIBILITY_ROLES,
    ELIGIBILITY_STATES,
    IDENTITY_KINDS,
    PlatformNeutralContractError,
    bounded_int,
    canonical_bytes,
    closed_fields,
    digest,
    enum_value,
    finite,
    record_digest,
    require,
    segment,
    unique_names,
)

PROTOCOL = ELIGIBILITY_PROTOCOL

FIELDS = frozenset(
    {
        "protocol",
        "eligibility_id",
        "node_id",
        "role",
        "state",
        "blockers",
        "authority_generation",
        "evidence_generation",
        "qualification_digest",
        "constraints",
        "issued_at",
        "expires_at",
    }
)
#: Fields that would smuggle a later authority into an eligibility result.
FORBIDDEN_AUTHORITY_FIELDS = frozenset(
    {"route_ready", "placement", "registration", "selection", "artifact_grant"}
)
APPROVED_PROBE_KINDS = frozenset({"native_peer", "browser_peer"})
ARTIFACT_GRANT_SCOPE = "artifact_chunks"
_STAGE_ROLES = frozenset(
    {
        "artifact_source",
        "speculative_draft_worker",
        "qualified_model_stage",
        "performance_ready_model_stage",
    }
)


@dataclass(frozen=True, slots=True)
class ProbeGate:
    integrity_verified: bool
    fresh: bool


@dataclass(frozen=True, slots=True)
class ArtifactAssignmentGrant:
    """A separate assignment-scoped A2 grant; A9 never issues it."""

    grant_id: str
    scope: str = ARTIFACT_GRANT_SCOPE

    def __post_init__(self) -> None:
        segment(self.grant_id, "artifact_grant_id_invalid")
        require(self.scope == ARTIFACT_GRANT_SCOPE, "artifact_grant_scope_invalid")


@dataclass(frozen=True, slots=True)
class LadderInputs:
    """The typed facts one role decision consumes; nothing is inferred."""

    node_id: str
    membership_generation: int | None
    lease_expires_at: float | None
    revoked: bool | None
    identity_kind: str | None
    consent: bool | None
    profile: PeerProfile | None = None
    report: CapabilityReport | None = None
    qualification: ClassQualification | None = None
    probe_gate: ProbeGate | None = None
    assignment_grant: ArtifactAssignmentGrant | None = None
    a11_closed: bool = False
    efficiency: Any = None
    now: float = 0.0

    def efficiency_is_current(self, now: float) -> bool:
        """Current means: decision ready, unexpired, and bound to this exact provider."""

        record = self.efficiency
        if record is None:
            return False
        return bool(
            record.is_current(
                now=now,
                evidence_generation=record.evidence_generation,
                provider_build_digest=record.provider_build_digest,
                driver_runtime_digest=record.driver_runtime_digest,
                device_class=record.device_class,
            )
        )


@dataclass(frozen=True, slots=True)
class PeerEligibility:
    eligibility_id: str
    node_id: str
    role: str
    state: str
    blockers: tuple[str, ...]
    authority_generation: int
    evidence_generation: int
    qualification_digest: str | None
    constraints: tuple[str, ...]
    issued_at: float
    expires_at: float

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "PeerEligibility":
        require(isinstance(value, Mapping), "eligibility_invalid")
        require(
            not (set(value) & FORBIDDEN_AUTHORITY_FIELDS),
            "eligibility_must_not_carry_later_authority",
        )
        record = closed_fields(value, FIELDS, "eligibility_invalid")
        require(record["protocol"] == PROTOCOL, "eligibility_protocol_invalid")
        state = enum_value(record["state"], ELIGIBILITY_STATES, "eligibility_state_invalid")
        blockers = unique_names(record["blockers"], "eligibility_blockers_invalid")
        if state == "eligible":
            require(not blockers, "eligibility_eligible_with_blockers")
        else:
            require(bool(blockers), "eligibility_requires_blockers")
        issued_at = finite(record["issued_at"], "eligibility_issued_at_invalid", minimum=0.0)
        expires_at = finite(
            record["expires_at"], "eligibility_expires_at_invalid", minimum=0.0
        )
        require(expires_at > issued_at, "eligibility_validity_invalid")
        return cls(
            eligibility_id=segment(record["eligibility_id"], "eligibility_id_invalid"),
            node_id=segment(record["node_id"], "eligibility_node_id_invalid"),
            role=enum_value(record["role"], ELIGIBILITY_ROLES, "eligibility_role_invalid"),
            state=state,
            blockers=blockers,
            authority_generation=bounded_int(
                record["authority_generation"],
                "eligibility_authority_generation_invalid",
                minimum=1,
            ),
            evidence_generation=bounded_int(
                record["evidence_generation"],
                "eligibility_evidence_generation_invalid",
                minimum=1,
            ),
            qualification_digest=(
                None
                if record["qualification_digest"] is None
                else digest(
                    record["qualification_digest"], "eligibility_qualification_digest_invalid"
                )
            ),
            constraints=unique_names(record["constraints"], "eligibility_constraints_invalid"),
            issued_at=issued_at,
            expires_at=expires_at,
        )

    def to_mapping(self) -> dict[str, Any]:
        return {
            "protocol": PROTOCOL,
            "eligibility_id": self.eligibility_id,
            "node_id": self.node_id,
            "role": self.role,
            "state": self.state,
            "blockers": list(self.blockers),
            "authority_generation": self.authority_generation,
            "evidence_generation": self.evidence_generation,
            "qualification_digest": self.qualification_digest,
            "constraints": list(self.constraints),
            "issued_at": self.issued_at,
            "expires_at": self.expires_at,
        }

    def canonical_bytes(self) -> bytes:
        return canonical_bytes(self.to_mapping(), "eligibility_noncanonical")

    @property
    def digest(self) -> str:
        return record_digest(self.to_mapping(), "eligibility_noncanonical")

    def grants_placement(self) -> bool:
        """Eligibility never selects anything; the planner and deployment qualifier do."""

        return False

    def grants_artifact_chunks(self) -> bool:
        """Only a separate assignment-scoped A2 grant admits immutable chunks."""

        return False


def derive_eligibility(role: str, inputs: LadderInputs, *, now: float) -> PeerEligibility:
    """Derive one role's state without consulting any prior peer label."""

    enum_value(role, ELIGIBILITY_ROLES, "eligibility_role_invalid")
    require(isinstance(inputs, LadderInputs), "eligibility_inputs_invalid")
    finite(now, "eligibility_now_invalid", minimum=0.0)
    failures: list[str] = []
    unknowns: list[str] = []
    evidence: list[str] = []

    _membership_facts(inputs, now=now, failures=failures, unknowns=unknowns)
    if role == "signed_member":
        return _result(
            role, inputs, now=now, failures=failures, unknowns=unknowns, evidence=evidence
        )

    require(
        inputs.identity_kind is None or inputs.identity_kind in IDENTITY_KINDS,
        "eligibility_identity_kind_invalid",
    )
    profile = inputs.profile
    if profile is None:
        evidence.append("capability_profile_absent")
    else:
        require(profile.node_id == inputs.node_id, "eligibility_profile_node_mismatch")
        if now > profile.expires_at:
            failures.append("capability_profile_expired")
        if profile.is_unknown_platform:
            failures.append("unknown_platform_ineligible")
        _known_class_facts(profile, failures=failures, unknowns=unknowns, evidence=evidence)

    if role == "probe_contributor":
        if inputs.identity_kind is None:
            evidence.append("identity_kind_unknown")
        elif inputs.identity_kind not in APPROVED_PROBE_KINDS:
            failures.append("identity_kind_not_approved")
        if inputs.consent is None:
            evidence.append("consent_unknown")
        elif not inputs.consent:
            failures.append("consent_missing")
        if inputs.probe_gate is None:
            evidence.append("probe_gate_absent")
        else:
            if not inputs.probe_gate.integrity_verified:
                failures.append("probe_integrity_failed")
            if not inputs.probe_gate.fresh:
                failures.append("probe_evidence_stale")
        return _result(
            role, inputs, now=now, failures=failures, unknowns=unknowns, evidence=evidence
        )

    if role in _STAGE_ROLES:
        _capability_facts(inputs, failures=failures, evidence=evidence)

    if role == "artifact_source":
        if not _has_registered_transport(profile):
            failures.append("transport_not_registered")
        _qualification_facts(
            inputs, role, now=now, failures=failures, evidence=evidence
        )
        if inputs.report is None:
            evidence.append("capability_report_absent")
        else:
            if profile is not None:
                try:
                    inputs.report.bound_to(profile)
                except PlatformNeutralContractError:
                    failures.append("capability_report_profile_mismatch")
            for family in ("storage", "thermal", "power"):
                if inputs.report.family(family).is_unknown():
                    evidence.append(f"{family}_policy_unknown")
        if inputs.assignment_grant is None:
            failures.append("artifact_assignment_grant_missing")
        return _result(
            role, inputs, now=now, failures=failures, unknowns=unknowns, evidence=evidence
        )

    if role == "speculative_draft_worker":
        if profile is None or not profile.runtimes:
            evidence.append("registered_runtime_absent")
        _qualification_facts(
            inputs, role, now=now, failures=failures, evidence=evidence
        )
        if not inputs.a11_closed:
            failures.append("a11_not_closed")
        return _result(
            role, inputs, now=now, failures=failures, unknowns=unknowns, evidence=evidence
        )

    if role == "qualified_model_stage":
        _stage_facts(inputs, failures=failures, evidence=evidence)
        _qualification_facts(
            inputs, role, now=now, failures=failures, evidence=evidence
        )
        return _result(
            role, inputs, now=now, failures=failures, unknowns=unknowns, evidence=evidence
        )

    # performance_ready_model_stage
    _stage_facts(inputs, failures=failures, evidence=evidence)
    _qualification_facts(
        inputs, "qualified_model_stage", now=now, failures=failures, evidence=evidence
    )
    if inputs.efficiency is None:
        evidence.append("performance_qualification_absent")
    else:
        decision = getattr(inputs.efficiency, "decision", None)
        if decision != "performance_ready":
            failures.append("performance_qualification_not_ready")
        efficiency_backend = getattr(inputs.efficiency, "backend_id", None)
        efficiency_device = getattr(inputs.efficiency, "device_class", None)
        if efficiency_backend in {"numpy", "unknown"}:
            failures.append("portable_backend_cannot_be_performance_ready")
        if efficiency_device == "windows_x86_64" and efficiency_backend in {
            "numpy",
            "unknown",
        }:
            failures.append("windows_acceleration_not_physically_observed")
        if not inputs.efficiency_is_current(now):
            failures.append("performance_qualification_stale")
    return _result(
        role, inputs, now=now, failures=failures, unknowns=unknowns, evidence=evidence
    )


def _membership_facts(
    inputs: LadderInputs, *, now: float, failures: list[str], unknowns: list[str]
) -> None:
    if inputs.membership_generation is None:
        unknowns.append("membership_generation_unknown")
    elif inputs.membership_generation < 1:
        failures.append("membership_generation_invalid")
    if inputs.lease_expires_at is None:
        unknowns.append("lease_unknown")
    elif now > inputs.lease_expires_at:
        failures.append("lease_expired")
    if inputs.revoked is None:
        unknowns.append("revocation_state_unknown")
    elif inputs.revoked:
        failures.append("member_revoked")


def _known_class_facts(
    profile: PeerProfile,
    *,
    failures: list[str],
    unknowns: list[str],
    evidence: list[str],
) -> None:
    if profile.architecture == "unknown":
        failures.append("unknown_architecture_ineligible")
    if not profile.transports or all(item.family == "unknown" for item in profile.transports):
        evidence.append("transport_unknown")
    elif any(item.lifecycle_availability == "unknown" for item in profile.transports):
        failures.append("unknown_lifecycle_semantics_ineligible")


def _capability_facts(
    inputs: LadderInputs, *, failures: list[str], evidence: list[str]
) -> None:
    if inputs.report is None:
        evidence.append("capability_report_absent")
        return
    report = inputs.report
    if report.node_id != inputs.node_id:
        failures.append("capability_report_node_mismatch")
    for family in ("memory", "limits", "thermal", "power", "lifecycle", "network"):
        observation = report.family(family)
        if observation.is_unknown():
            evidence.append(f"{family}_evidence_unknown")
        elif observation.source_kind == "synthetic_conformance":
            failures.append(f"native_gate_requires_native_evidence:{family}")


def _stage_facts(
    inputs: LadderInputs, *, failures: list[str], evidence: list[str]
) -> None:
    profile = inputs.profile
    if profile is None:
        return
    for runtime in profile.runtimes:
        if not runtime.cancellation_supported:
            failures.append("cancellation_unsupported")
        if not runtime.cleanup_supported:
            failures.append("cleanup_unsupported")
        if not runtime.artifact_formats:
            evidence.append("artifact_format_absent")
    if profile.identity_kind == "probe_only":
        failures.append("probe_only_cannot_stage")
    if profile.identity_kind == "artifact_source":
        failures.append("artifact_source_cannot_stage")


def _qualification_facts(
    inputs: LadderInputs,
    role: str,
    *,
    now: float,
    failures: list[str],
    evidence: list[str],
) -> None:
    qualification = inputs.qualification
    if qualification is None:
        evidence.append("class_qualification_absent")
        return
    if not qualification.grants(role):
        failures.append("class_qualification_role_mismatch")
    if qualification.subject_node_id != inputs.node_id:
        failures.append("class_qualification_node_mismatch")
    if now > qualification.expires_at:
        failures.append("class_qualification_expired")
    if (
        inputs.membership_generation is not None
        and qualification.subject_membership_generation != inputs.membership_generation
    ):
        failures.append("class_qualification_generation_stale")
    if inputs.profile is not None and not qualification.matches_profile(inputs.profile):
        failures.append("class_qualification_profile_stale")
    if inputs.report is not None:
        if qualification.evidence_generation != inputs.report.evidence_generation:
            failures.append("class_qualification_evidence_generation_stale")
        if qualification.evidence_profile_digest != inputs.report.profile_digest:
            failures.append("class_qualification_report_profile_stale")
    if qualification.exact_class.has_unknown_member:
        failures.append("unknown_qualification_class_ineligible")


def _has_registered_transport(profile: PeerProfile | None) -> bool:
    if profile is None:
        return False
    return any(item.family != "unknown" for item in profile.transports)


def _result(
    role: str,
    inputs: LadderInputs,
    *,
    now: float,
    failures: list[str],
    unknowns: list[str],
    evidence: list[str],
) -> PeerEligibility:
    ordered = tuple(dict.fromkeys([*failures, *evidence, *unknowns]))
    if failures or evidence:
        state = "ineligible"
    elif unknowns:
        state = "unknown"
    else:
        state = "eligible"
    qualification = inputs.qualification
    return PeerEligibility(
        eligibility_id=f"eligibility:{inputs.node_id}:{role}",
        node_id=inputs.node_id,
        role=role,
        state=state,
        blockers=ordered,
        authority_generation=max(inputs.membership_generation or 1, 1),
        evidence_generation=(
            inputs.report.evidence_generation if inputs.report is not None else 1
        ),
        qualification_digest=qualification.digest if qualification is not None else None,
        constraints=(),
        issued_at=now,
        expires_at=now + 300.0,
    )
