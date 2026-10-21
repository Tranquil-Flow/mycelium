# SPDX-License-Identifier: AGPL-3.0-only
"""``mycelium.membership.peer_profile.v2`` — the closed platform-neutral declaration.

Spec §3.1-3.2, §4.1.  A profile is the member's *signed declaration* of identity
kind, platform family, architecture, software build, installed transports, and
installed runtime backends.  A declaration is never proof that a capability
works: evidence, class qualification, and eligibility remain separate
authorities (frozen decision ``separated_capability_authority``).

The record is closed: unknown enum values and unknown fields are rejected rather
than coerced to ``unknown``.  ``unknown`` is a registered sentinel that stays
visible and ineligible.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from mycelium_qualification.signing import Ed25519EvidenceSigner, build_ed25519_verifier

from .primitives import (
    ARCHITECTURES,
    IDENTITY_KINDS,
    LIFECYCLE_AVAILABILITY,
    PEER_PROFILE_PROTOCOL,
    PLATFORM_FAMILIES,
    RUNTIME_BACKEND_STAGES,
    TRANSPORT_FAMILIES,
    PlatformNeutralContractError,
    bounded_int,
    bounded_list,
    canonical_bytes,
    closed_fields,
    detached,
    digest,
    enum_value,
    finite,
    nullable_bool,
    nullable_text,
    record_digest,
    require,
    require_distinct,
    segment,
    text,
    unique_names,
)

PROTOCOL = PEER_PROFILE_PROTOCOL

PROFILE_FIELDS = frozenset(
    {
        "protocol",
        "profile_id",
        "swarm_id",
        "node_id",
        "incarnation",
        "membership_generation",
        "identity_kind",
        "platform_family",
        "platform_variant",
        "architecture",
        "software_build",
        "transports",
        "runtimes",
        "lease_expires_at",
        "issued_at",
        "expires_at",
    }
)
BUILD_FIELDS = frozenset({"version", "digest"})
TRANSPORT_FIELDS = frozenset(
    {
        "family",
        "build_digest",
        "wire_protocol_versions",
        "endpoint_id_digest",
        "connection_roles",
        "direct_observed",
        "relay_observed",
        "persistent_reuse",
        "lifecycle_availability",
        "max_frame_bytes",
        "max_concurrency",
    }
)
RUNTIME_FIELDS = frozenset(
    {
        "backend_id",
        "build_digest",
        "model_families",
        "representations",
        "precisions",
        "decode_operations",
        "max_context_tokens",
        "max_concurrent_requests",
        "kv_schema",
        "artifact_formats",
        "cancellation_supported",
        "cleanup_supported",
    }
)
CONNECTION_ROLES = frozenset({"initiator", "responder"})
ENVELOPE_FIELDS = frozenset({"protocol", "profile", "signature", "verification_key"})
SIGNATURE_FIELDS = frozenset(
    {
        "algorithm",
        "signer_endpoint_id",
        "verification_key_digest",
        "signed_statement_digest",
        "signature",
    }
)
MAX_TRANSPORTS = 4
MAX_RUNTIMES = 8


@dataclass(frozen=True, slots=True)
class TransportDeclaration:
    family: str
    build_digest: str
    wire_protocol_versions: tuple[str, ...]
    endpoint_id_digest: str | None
    connection_roles: tuple[str, ...]
    direct_observed: bool | None
    relay_observed: bool | None
    persistent_reuse: bool | None
    lifecycle_availability: str
    max_frame_bytes: int
    max_concurrency: int

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "TransportDeclaration":
        record = closed_fields(value, TRANSPORT_FIELDS, "transport_declaration_invalid")
        return cls(
            family=enum_value(
                record["family"], TRANSPORT_FAMILIES, "transport_family_invalid"
            ),
            build_digest=digest(record["build_digest"], "transport_build_invalid"),
            wire_protocol_versions=unique_names(
                record["wire_protocol_versions"], "transport_wire_versions_invalid"
            ),
            endpoint_id_digest=(
                None
                if record["endpoint_id_digest"] is None
                else digest(
                    record["endpoint_id_digest"], "transport_endpoint_digest_invalid"
                )
            ),
            connection_roles=tuple(
                sorted(
                    enum_value(role, CONNECTION_ROLES, "transport_role_invalid")
                    for role in unique_names(
                        record["connection_roles"], "transport_roles_invalid"
                    )
                )
            ),
            direct_observed=nullable_bool(
                record["direct_observed"], "transport_direct_observed_invalid"
            ),
            relay_observed=nullable_bool(
                record["relay_observed"], "transport_relay_observed_invalid"
            ),
            persistent_reuse=nullable_bool(
                record["persistent_reuse"], "transport_persistent_reuse_invalid"
            ),
            lifecycle_availability=enum_value(
                record["lifecycle_availability"],
                LIFECYCLE_AVAILABILITY,
                "transport_lifecycle_invalid",
            ),
            max_frame_bytes=bounded_int(
                record["max_frame_bytes"],
                "transport_frame_bound_invalid",
                minimum=1,
                maximum=1 << 30,
            ),
            max_concurrency=bounded_int(
                record["max_concurrency"],
                "transport_concurrency_bound_invalid",
                minimum=1,
                maximum=1 << 20,
            ),
        )

    def to_mapping(self) -> dict[str, Any]:
        return {
            "family": self.family,
            "build_digest": self.build_digest,
            "wire_protocol_versions": list(self.wire_protocol_versions),
            "endpoint_id_digest": self.endpoint_id_digest,
            "connection_roles": list(self.connection_roles),
            "direct_observed": self.direct_observed,
            "relay_observed": self.relay_observed,
            "persistent_reuse": self.persistent_reuse,
            "lifecycle_availability": self.lifecycle_availability,
            "max_frame_bytes": self.max_frame_bytes,
            "max_concurrency": self.max_concurrency,
        }


@dataclass(frozen=True, slots=True)
class RuntimeDeclaration:
    backend_id: str
    build_digest: str
    model_families: tuple[str, ...]
    representations: tuple[str, ...]
    precisions: tuple[str, ...]
    decode_operations: tuple[str, ...]
    max_context_tokens: int
    max_concurrent_requests: int
    kv_schema: str
    artifact_formats: tuple[str, ...]
    cancellation_supported: bool
    cleanup_supported: bool

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "RuntimeDeclaration":
        record = closed_fields(value, RUNTIME_FIELDS, "runtime_declaration_invalid")
        backend_id = record["backend_id"]
        require(
            isinstance(backend_id, str) and backend_id in RUNTIME_BACKEND_STAGES,
            "runtime_backend_not_registered",
        )
        return cls(
            backend_id=backend_id,
            build_digest=digest(record["build_digest"], "runtime_build_invalid"),
            model_families=unique_names(
                record["model_families"], "runtime_model_families_invalid"
            ),
            representations=unique_names(
                record["representations"], "runtime_representations_invalid"
            ),
            precisions=unique_names(record["precisions"], "runtime_precisions_invalid"),
            decode_operations=unique_names(
                record["decode_operations"], "runtime_decode_operations_invalid"
            ),
            max_context_tokens=bounded_int(
                record["max_context_tokens"],
                "runtime_context_bound_invalid",
                minimum=1,
                maximum=1 << 30,
            ),
            max_concurrent_requests=bounded_int(
                record["max_concurrent_requests"],
                "runtime_concurrency_bound_invalid",
                minimum=1,
                maximum=1 << 20,
            ),
            kv_schema=text(record["kv_schema"], "runtime_kv_schema_invalid"),
            artifact_formats=unique_names(
                record["artifact_formats"], "runtime_artifact_formats_invalid"
            ),
            cancellation_supported=_require_bool(
                record["cancellation_supported"], "runtime_cancellation_invalid"
            ),
            cleanup_supported=_require_bool(
                record["cleanup_supported"], "runtime_cleanup_invalid"
            ),
        )

    def to_mapping(self) -> dict[str, Any]:
        return {
            "backend_id": self.backend_id,
            "build_digest": self.build_digest,
            "model_families": list(self.model_families),
            "representations": list(self.representations),
            "precisions": list(self.precisions),
            "decode_operations": list(self.decode_operations),
            "max_context_tokens": self.max_context_tokens,
            "max_concurrent_requests": self.max_concurrent_requests,
            "kv_schema": self.kv_schema,
            "artifact_formats": list(self.artifact_formats),
            "cancellation_supported": self.cancellation_supported,
            "cleanup_supported": self.cleanup_supported,
        }


@dataclass(frozen=True, slots=True)
class PeerProfile:
    profile_id: str
    swarm_id: str
    node_id: str
    incarnation: str
    membership_generation: int
    identity_kind: str
    platform_family: str
    platform_variant: str | None
    architecture: str
    software_build_version: str
    software_build_digest: str
    transports: tuple[TransportDeclaration, ...]
    runtimes: tuple[RuntimeDeclaration, ...]
    lease_expires_at: float
    issued_at: float
    expires_at: float

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "PeerProfile":
        record = closed_fields(value, PROFILE_FIELDS, "peer_profile_invalid")
        require(record["protocol"] == PROTOCOL, "peer_profile_protocol_invalid")
        build = closed_fields(
            record["software_build"], BUILD_FIELDS, "peer_profile_build_invalid"
        )
        platform_family = enum_value(
            record["platform_family"], PLATFORM_FAMILIES, "peer_profile_platform_invalid"
        )
        platform_variant = nullable_text(
            record["platform_variant"],
            "peer_profile_platform_variant_invalid",
            maximum=64,
        )
        require(
            platform_variant is None or platform_family == "unknown",
            "peer_profile_platform_variant_requires_unknown_family",
        )
        transports = tuple(
            TransportDeclaration.from_mapping(item)
            for item in bounded_list(
                record["transports"],
                "peer_profile_transports_invalid",
                maximum=MAX_TRANSPORTS,
            )
        )
        require_distinct([item.family for item in transports], "peer_profile_transports_duplicate")
        runtimes = tuple(
            RuntimeDeclaration.from_mapping(item)
            for item in bounded_list(
                record["runtimes"], "peer_profile_runtimes_invalid", maximum=MAX_RUNTIMES
            )
        )
        require_distinct(
            [item.backend_id for item in runtimes], "peer_profile_runtimes_duplicate"
        )
        issued_at = finite(record["issued_at"], "peer_profile_issued_at_invalid", minimum=0.0)
        expires_at = finite(
            record["expires_at"], "peer_profile_expires_at_invalid", minimum=0.0
        )
        require(expires_at > issued_at, "peer_profile_validity_invalid")
        return cls(
            profile_id=segment(record["profile_id"], "peer_profile_id_invalid"),
            swarm_id=segment(record["swarm_id"], "peer_profile_swarm_id_invalid"),
            node_id=segment(record["node_id"], "peer_profile_node_id_invalid"),
            incarnation=segment(record["incarnation"], "peer_profile_incarnation_invalid"),
            membership_generation=bounded_int(
                record["membership_generation"],
                "peer_profile_generation_invalid",
                minimum=1,
                maximum=1 << 40,
            ),
            identity_kind=enum_value(
                record["identity_kind"], IDENTITY_KINDS, "peer_profile_identity_kind_invalid"
            ),
            platform_family=platform_family,
            platform_variant=platform_variant,
            architecture=enum_value(
                record["architecture"], ARCHITECTURES, "peer_profile_architecture_invalid"
            ),
            software_build_version=text(
                build["version"], "peer_profile_build_version_invalid"
            ),
            software_build_digest=digest(
                build["digest"], "peer_profile_build_digest_invalid"
            ),
            transports=transports,
            runtimes=runtimes,
            lease_expires_at=finite(
                record["lease_expires_at"], "peer_profile_lease_invalid", minimum=0.0
            ),
            issued_at=issued_at,
            expires_at=expires_at,
        )

    def to_mapping(self) -> dict[str, Any]:
        return {
            "protocol": PROTOCOL,
            "profile_id": self.profile_id,
            "swarm_id": self.swarm_id,
            "node_id": self.node_id,
            "incarnation": self.incarnation,
            "membership_generation": self.membership_generation,
            "identity_kind": self.identity_kind,
            "platform_family": self.platform_family,
            "platform_variant": self.platform_variant,
            "architecture": self.architecture,
            "software_build": {
                "version": self.software_build_version,
                "digest": self.software_build_digest,
            },
            "transports": [item.to_mapping() for item in self.transports],
            "runtimes": [item.to_mapping() for item in self.runtimes],
            "lease_expires_at": self.lease_expires_at,
            "issued_at": self.issued_at,
            "expires_at": self.expires_at,
        }

    def canonical_bytes(self) -> bytes:
        return canonical_bytes(self.to_mapping(), "peer_profile_noncanonical")

    @property
    def digest(self) -> str:
        """The profile digest bound by evidence, qualification, and eligibility."""

        return record_digest(self.to_mapping(), "peer_profile_noncanonical")

    @property
    def is_unknown_platform(self) -> bool:
        return self.platform_family == "unknown" or self.architecture == "unknown"

    def runtime_for(self, backend_id: str) -> RuntimeDeclaration | None:
        for runtime in self.runtimes:
            if runtime.backend_id == backend_id:
                return runtime
        return None


def sign_peer_profile(
    *, signer: Ed25519EvidenceSigner, profile: PeerProfile
) -> dict[str, Any]:
    """Sign one profile with the member's durable key (frozen decision §3.2)."""

    require(isinstance(signer, Ed25519EvidenceSigner), "peer_profile_signer_invalid")
    statement = profile.to_mapping()
    return {
        "protocol": PROTOCOL,
        "profile": statement,
        "signature": signer.sign(statement),
        "verification_key": signer.public_key_record(),
    }


def verify_peer_profile(
    envelope: Mapping[str, Any],
    *,
    now: float,
    expected_key_digest: str | None = None,
    expected_node_id: str | None = None,
) -> PeerProfile:
    """Verify a signed v2 profile; a declaration proves only that it was declared."""

    record = closed_fields(envelope, ENVELOPE_FIELDS, "peer_profile_envelope_invalid")
    require(record["protocol"] == PROTOCOL, "peer_profile_protocol_invalid")
    profile = PeerProfile.from_mapping(record["profile"])
    signature = closed_fields(
        record["signature"], SIGNATURE_FIELDS, "peer_profile_signature_invalid"
    )
    require(
        signature["verification_key_digest"]
        == detached(record["verification_key"], "peer_profile_key_invalid").get(
            "verification_key_digest"
        ),
        "peer_profile_key_digest_mismatch",
    )
    if expected_key_digest is not None:
        require(
            signature["verification_key_digest"] == expected_key_digest,
            "peer_profile_key_continuity_mismatch",
        )
    if expected_node_id is not None:
        require(profile.node_id == expected_node_id, "peer_profile_node_mismatch")
    verify = build_ed25519_verifier([record["verification_key"]])
    statement = profile.to_mapping()
    require(
        verify(canonical_bytes(statement, "peer_profile_noncanonical"), dict(signature)),
        "peer_profile_signature_invalid",
    )
    finite(now, "peer_profile_now_invalid", minimum=0.0)
    require(now <= profile.expires_at, "peer_profile_expired")
    return profile


def _require_bool(value: Any, code: str) -> bool:
    require(isinstance(value, bool), code)
    return value
