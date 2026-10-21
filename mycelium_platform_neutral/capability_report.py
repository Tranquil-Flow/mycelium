# SPDX-License-Identifier: AGPL-3.0-only
"""``mycelium.membership.capability_report.v2`` — fresh signed observations.

Spec §3.3, §4.2, §4 (final paragraph), §5.  A report carries bounded, nullable
observation families; every family names its own evidence source kind, and one
source kind can never satisfy another's gate (frozen decision
``native_synthetic_evidence_separation``).

Missing or unavailable observations are ``null``/``unknown`` — never zero,
healthy, supported, direct, background-capable, or eligible.  A report is bound
to the exact profile digest and evidence generation it was observed under.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from mycelium_qualification.signing import Ed25519EvidenceSigner, build_ed25519_verifier

from .peer_profile import PeerProfile
from .primitives import (
    CAPABILITY_REPORT_PROTOCOL,
    EVIDENCE_SOURCE_KINDS,
    PlatformNeutralContractError,
    bounded_int,
    bounded_list,
    canonical_bytes,
    closed_fields,
    digest,
    enum_value,
    finite,
    nullable_bool,
    nullable_bounded_int,
    nullable_enum,
    nullable_finite,
    nullable_text,
    record_digest,
    require,
    require_distinct,
    segment,
    text,
    unique_names,
)

PROTOCOL = CAPABILITY_REPORT_PROTOCOL

#: The seven observation families of the capability profile (spec §3.3).
CAPABILITY_FAMILIES = (
    "memory",
    "storage",
    "limits",
    "thermal",
    "power",
    "lifecycle",
    "network",
)

REPORT_FIELDS = frozenset(
    {
        "protocol",
        "report_id",
        "swarm_id",
        "node_id",
        "incarnation",
        "membership_generation",
        "profile_digest",
        "evidence_generation",
        "observations",
        "issued_at",
        "expires_at",
    }
)
FAMILY_FIELDS = frozenset({"source_kind", "observed_at", "values"})
ENVELOPE_FIELDS = frozenset({"protocol", "report", "signature", "verification_key"})

_THERMAL_STATES = frozenset({"nominal", "fair", "serious", "critical", "unknown"})
_THERMAL_CLASSES = frozenset({"sustained", "burst", "unknown"})
_THROTTLE_BEHAVIORS = frozenset(
    {"none", "frequency_limited", "power_limited", "unknown"}
)
_POWER_SOURCES = frozenset({"ac", "battery", "unknown"})
_LIFECYCLE_STATES = frozenset(
    {"active", "foreground", "background", "suspended", "unknown"}
)
_SUSPEND_BEHAVIORS = frozenset({"resume_safe", "restart_required", "unknown"})
_RESTART_SEMANTICS = frozenset({"same_incarnation", "new_incarnation", "unknown"})
_NETWORK_LOSS_BEHAVIORS = frozenset({"reconnect", "drain", "unknown"})

#: family -> field -> value kind.  ``bytes``/``count``/``seconds``/``ratio``
#: are nullable numerics; ``bool``/``text``/``protocol``/``text_list`` are
#: nullable primitives; ``enum:<name>`` is a nullable registered value.
FAMILY_SCHEMA = MappingProxyType(
    {
        "memory": {
            "sustainable_bytes": "bytes",
            "available_bytes": "bytes",
            "workspace_bytes": "bytes",
            "kv_headroom_bytes": "bytes",
        },
        "storage": {
            "assignment_local_bytes": "bytes",
            "free_bytes": "bytes",
        },
        "limits": {
            "supported_context_tokens": "count",
            "active_requests": "count",
            "batch_size": "count",
            "prefill_tokens": "count",
            "decode_tokens": "count",
            "multi_position_verify": "count",
            "speculative_draft": "count",
        },
        "thermal": {
            "state": "enum:thermal_state",
            "sustained_class": "enum:thermal_class",
            "observation_duration_s": "seconds",
            "throttle_behavior": "enum:throttle_behavior",
        },
        "power": {
            "source": "enum:power_source",
            "battery_class": "text",
            "battery_level": "ratio",
            "low_power_state": "bool",
            "minimum_contribution": "text",
            "drain_deadline_s": "seconds",
        },
        "lifecycle": {
            "state": "enum:lifecycle_state",
            "background_execution": "bool",
            "suspend_behavior": "enum:suspend_behavior",
            "restart_semantics": "enum:restart_semantics",
            "max_background_grace_s": "seconds",
        },
        "network": {
            "activation_protocol": "protocol",
            "transport_observations": "text_list",
            "network_loss_behavior": "enum:network_loss_behavior",
            "link_measurement_available": "bool",
            "freshness_s": "seconds",
        },
    }
)
_ENUMS = MappingProxyType(
    {
        "thermal_state": _THERMAL_STATES,
        "thermal_class": _THERMAL_CLASSES,
        "throttle_behavior": _THROTTLE_BEHAVIORS,
        "power_source": _POWER_SOURCES,
        "lifecycle_state": _LIFECYCLE_STATES,
        "suspend_behavior": _SUSPEND_BEHAVIORS,
        "restart_semantics": _RESTART_SEMANTICS,
        "network_loss_behavior": _NETWORK_LOSS_BEHAVIORS,
    }
)


@dataclass(frozen=True, slots=True)
class FamilyObservation:
    family: str
    source_kind: str
    observed_at: float | None
    values: Mapping[str, Any]

    @classmethod
    def from_mapping(cls, family: str, value: Mapping[str, Any]) -> "FamilyObservation":
        require(family in CAPABILITY_FAMILIES, "capability_family_unknown")
        record = closed_fields(value, FAMILY_FIELDS, "capability_family_invalid")
        source_kind = enum_value(
            record["source_kind"], EVIDENCE_SOURCE_KINDS, "capability_source_kind_invalid"
        )
        observed_at = nullable_finite(
            record["observed_at"], "capability_observed_at_invalid", minimum=0.0
        )
        schema = FAMILY_SCHEMA[family]
        values = closed_fields(record["values"], frozenset(schema), "capability_values_invalid")
        validated = {
            field: _validate_value(field, values[field], kind, family)
            for field, kind in schema.items()
        }
        if source_kind == "unknown":
            require(
                all(item is None for item in validated.values()),
                "capability_unknown_family_must_be_null",
            )
        return cls(
            family=family,
            source_kind=source_kind,
            observed_at=observed_at,
            values=validated,
        )

    def to_mapping(self) -> dict[str, Any]:
        return {
            "source_kind": self.source_kind,
            "observed_at": self.observed_at,
            "values": dict(self.values),
        }

    def satisfies(self, required_source_kind: str) -> bool:
        """Only the exact required source kind can close a gate."""

        return (
            self.source_kind == required_source_kind and required_source_kind != "unknown"
        )

    def is_unknown(self) -> bool:
        return self.source_kind == "unknown" or all(
            item is None for item in self.values.values()
        )


@dataclass(frozen=True, slots=True)
class CapabilityReport:
    report_id: str
    swarm_id: str
    node_id: str
    incarnation: str
    membership_generation: int
    profile_digest: str
    evidence_generation: int
    observations: Mapping[str, FamilyObservation]
    issued_at: float
    expires_at: float

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "CapabilityReport":
        record = closed_fields(value, REPORT_FIELDS, "capability_report_invalid")
        require(record["protocol"] == PROTOCOL, "capability_report_protocol_invalid")
        observations = closed_fields(
            record["observations"], frozenset(CAPABILITY_FAMILIES), "capability_families_invalid"
        )
        issued_at = finite(
            record["issued_at"], "capability_report_issued_at_invalid", minimum=0.0
        )
        expires_at = finite(
            record["expires_at"], "capability_report_expires_at_invalid", minimum=0.0
        )
        require(expires_at > issued_at, "capability_report_validity_invalid")
        return cls(
            report_id=segment(record["report_id"], "capability_report_id_invalid"),
            swarm_id=segment(record["swarm_id"], "capability_report_swarm_id_invalid"),
            node_id=segment(record["node_id"], "capability_report_node_id_invalid"),
            incarnation=segment(
                record["incarnation"], "capability_report_incarnation_invalid"
            ),
            membership_generation=bounded_int(
                record["membership_generation"],
                "capability_report_generation_invalid",
                minimum=1,
                maximum=1 << 40,
            ),
            profile_digest=digest(
                record["profile_digest"], "capability_report_profile_digest_invalid"
            ),
            evidence_generation=bounded_int(
                record["evidence_generation"],
                "capability_report_evidence_generation_invalid",
                minimum=1,
                maximum=1 << 40,
            ),
            observations={
                family: FamilyObservation.from_mapping(family, observations[family])
                for family in CAPABILITY_FAMILIES
            },
            issued_at=issued_at,
            expires_at=expires_at,
        )

    def to_mapping(self) -> dict[str, Any]:
        return {
            "protocol": PROTOCOL,
            "report_id": self.report_id,
            "swarm_id": self.swarm_id,
            "node_id": self.node_id,
            "incarnation": self.incarnation,
            "membership_generation": self.membership_generation,
            "profile_digest": self.profile_digest,
            "evidence_generation": self.evidence_generation,
            "observations": {
                family: self.observations[family].to_mapping()
                for family in CAPABILITY_FAMILIES
            },
            "issued_at": self.issued_at,
            "expires_at": self.expires_at,
        }

    def canonical_bytes(self) -> bytes:
        return canonical_bytes(self.to_mapping(), "capability_report_noncanonical")

    @property
    def digest(self) -> str:
        return record_digest(self.to_mapping(), "capability_report_noncanonical")

    def bound_to(self, profile: PeerProfile) -> None:
        """Reject a report that is not bound to this exact profile generation."""

        require(
            self.profile_digest == profile.digest
            and self.membership_generation == profile.membership_generation
            and self.incarnation == profile.incarnation
            and self.node_id == profile.node_id,
            "capability_report_profile_mismatch",
        )

    def is_fresh(self, now: float) -> bool:
        return now <= self.expires_at

    def family(self, name: str) -> FamilyObservation:
        return self.observations[name]


def sign_capability_report(
    *, signer: Ed25519EvidenceSigner, report: CapabilityReport
) -> dict[str, Any]:
    require(isinstance(signer, Ed25519EvidenceSigner), "capability_report_signer_invalid")
    statement = report.to_mapping()
    return {
        "protocol": PROTOCOL,
        "report": statement,
        "signature": signer.sign(statement),
        "verification_key": signer.public_key_record(),
    }


def verify_capability_report(
    envelope: Mapping[str, Any],
    *,
    now: float,
    expected_key_digest: str | None = None,
) -> CapabilityReport:
    record = closed_fields(envelope, ENVELOPE_FIELDS, "capability_report_envelope_invalid")
    require(record["protocol"] == PROTOCOL, "capability_report_protocol_invalid")
    report = CapabilityReport.from_mapping(record["report"])
    signature = closed_fields(
        record["signature"],
        frozenset(
            {
                "algorithm",
                "signer_endpoint_id",
                "verification_key_digest",
                "signed_statement_digest",
                "signature",
            }
        ),
        "capability_report_signature_invalid",
    )
    if expected_key_digest is not None:
        require(
            signature["verification_key_digest"] == expected_key_digest,
            "capability_report_key_mismatch",
        )
    verify = build_ed25519_verifier([record["verification_key"]])
    statement = report.to_mapping()
    require(
        verify(canonical_bytes(statement, "capability_report_noncanonical"), dict(signature)),
        "capability_report_signature_invalid",
    )
    require(now <= report.expires_at, "capability_report_expired")
    return report


def _validate_value(field: str, value: Any, kind: str, family: str) -> Any:
    code = f"capability_value_invalid:{family}:{field}"
    if kind == "bytes":
        return nullable_bounded_int(value, code, minimum=0, maximum=1 << 60)
    if kind == "count":
        return nullable_bounded_int(value, code, minimum=0, maximum=1 << 40)
    if kind == "seconds":
        return nullable_finite(value, code, minimum=0.0, maximum=1.0e9)
    if kind == "ratio":
        return nullable_finite(value, code, minimum=0.0, maximum=1.0)
    if kind == "bool":
        return nullable_bool(value, code)
    if kind == "text":
        return nullable_text(value, code)
    if kind == "protocol":
        return nullable_text(value, code, maximum=128)
    if kind == "text_list":
        if value is None:
            return None
        return list(unique_names(value, code))
    if kind.startswith("enum:"):
        return nullable_enum(value, _ENUMS[kind.split(":", 1)[1]], code)
    raise PlatformNeutralContractError("capability_value_kind_invalid")
