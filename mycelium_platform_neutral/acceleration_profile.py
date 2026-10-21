# SPDX-License-Identifier: AGPL-3.0-only
"""``mycelium.runtime_acceleration_profile.v1`` — exact accelerated backend identity.

Spec §3.2, §3.4, §4.6.  A provider record is vendor-neutral but exact: it binds
implementation family, execution provider, build digest, device class,
driver/runtime identity, supported kernels and precisions, thread/queue limits,
and the qualification evidence required before each operation is eligible.

``numpy`` remains a portable correctness/reference identity and cannot satisfy
the accelerated Windows or performance-ready class without separate executed
acceleration evidence; a backend that performs a whole-weight ``int8`` ->
``float32`` conversion inside each linear operation remains correctness-only
(frozen decisions ``accelerated_windows_backend`` and
``persistent_int8_execution``).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from .primitives import (
    ACCELERATION_PROFILE_PROTOCOL,
    CORRECTNESS_ONLY_BACKENDS,
    RUNTIME_BACKEND_STAGES,
    bounded_int,
    bounded_list,
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

PROTOCOL = ACCELERATION_PROFILE_PROTOCOL

#: Operation-level evidence an accelerated backend must expose (spec §3.4).
REQUIRED_OPERATIONS = frozenset(
    {"embedding", "decoder_range", "final_norm", "vocab_head", "kv_update", "token_selection"}
)
PERSISTENT_STATE_BOUNDARIES = frozenset({"load", "warmup"})
WEIGHT_PRECISIONS = frozenset({"int8"})

FIELDS = frozenset(
    {
        "protocol",
        "profile_id",
        "backend_id",
        "registry_stage",
        "implementation_family",
        "execution_provider",
        "build_digest",
        "device_class",
        "driver_runtime",
        "supported_operations",
        "kernels",
        "precision_policy",
        "materialization_policy",
        "stage_local_kv",
        "workspace_bounded_bytes",
        "cancellation_supported",
        "cleanup_supported",
        "issued_at",
        "expires_at",
    }
)
DRIVER_FIELDS = frozenset({"identity", "version_digest"})
PRECISION_FIELDS = frozenset(
    {"weight_precision", "activation_precisions", "per_decode_full_weight_cast"}
)
MATERIALIZATION_FIELDS = frozenset(
    {
        "persistent_state_boundary",
        "decode_time_full_weight_materializations",
        "packed_state_bounded_bytes",
    }
)


@dataclass(frozen=True, slots=True)
class AccelerationProfile:
    profile_id: str
    backend_id: str
    registry_stage: str
    implementation_family: str
    execution_provider: str
    build_digest: str
    device_class: str
    driver_runtime_identity: str
    driver_runtime_digest: str
    supported_operations: tuple[str, ...]
    kernels: tuple[str, ...]
    weight_precision: str
    activation_precisions: tuple[str, ...]
    per_decode_full_weight_cast: bool
    persistent_state_boundary: str
    decode_time_full_weight_materializations: int
    packed_state_bounded_bytes: int
    stage_local_kv: bool
    workspace_bounded_bytes: int
    cancellation_supported: bool
    cleanup_supported: bool
    issued_at: float
    expires_at: float

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "AccelerationProfile":
        record = closed_fields(value, FIELDS, "acceleration_profile_invalid")
        require(record["protocol"] == PROTOCOL, "acceleration_profile_protocol_invalid")
        backend_id = record["backend_id"]
        require(
            isinstance(backend_id, str) and backend_id in RUNTIME_BACKEND_STAGES,
            "runtime_backend_not_registered",
        )
        registry_stage = enum_value(
            record["registry_stage"],
            {"implemented", "unknown"},
            "acceleration_registry_stage_invalid",
        )
        require(
            registry_stage == RUNTIME_BACKEND_STAGES[backend_id],
            "acceleration_registry_stage_mismatch",
        )
        driver = closed_fields(
            record["driver_runtime"], DRIVER_FIELDS, "acceleration_driver_invalid"
        )
        precision = closed_fields(
            record["precision_policy"], PRECISION_FIELDS, "acceleration_precision_invalid"
        )
        materialization = closed_fields(
            record["materialization_policy"],
            MATERIALIZATION_FIELDS,
            "acceleration_materialization_invalid",
        )
        supported = tuple(
            unique_names(record["supported_operations"], "acceleration_operations_invalid")
        )
        require_distinct(supported, "acceleration_operations_duplicate")
        operations = set(supported)
        missing = REQUIRED_OPERATIONS - operations
        require(
            not missing,
            "operation_evidence_missing:" + ",".join(sorted(missing)),
        )
        require(
            operations <= REQUIRED_OPERATIONS,
            "acceleration_operation_unknown",
        )
        issued_at = finite(record["issued_at"], "acceleration_issued_at_invalid", minimum=0.0)
        expires_at = finite(
            record["expires_at"], "acceleration_expires_at_invalid", minimum=0.0
        )
        require(expires_at > issued_at, "acceleration_validity_invalid")
        return cls(
            profile_id=segment(record["profile_id"], "acceleration_profile_id_invalid"),
            backend_id=backend_id,
            registry_stage=registry_stage,
            implementation_family=text(
                record["implementation_family"], "acceleration_family_invalid"
            ),
            execution_provider=text(
                record["execution_provider"], "acceleration_provider_invalid"
            ),
            build_digest=digest(record["build_digest"], "acceleration_build_invalid"),
            device_class=text(record["device_class"], "acceleration_device_class_invalid"),
            driver_runtime_identity=text(
                driver["identity"], "acceleration_driver_identity_invalid"
            ),
            driver_runtime_digest=digest(
                driver["version_digest"], "acceleration_driver_digest_invalid"
            ),
            supported_operations=supported,
            kernels=unique_names(record["kernels"], "acceleration_kernels_invalid"),
            weight_precision=enum_value(
                precision["weight_precision"], WEIGHT_PRECISIONS, "acceleration_weight_precision_invalid"
            ),
            activation_precisions=unique_names(
                precision["activation_precisions"], "acceleration_activation_precisions_invalid"
            ),
            per_decode_full_weight_cast=_require_bool(
                precision["per_decode_full_weight_cast"],
                "acceleration_per_decode_cast_invalid",
            ),
            persistent_state_boundary=enum_value(
                materialization["persistent_state_boundary"],
                PERSISTENT_STATE_BOUNDARIES,
                "acceleration_materialization_boundary_invalid",
            ),
            decode_time_full_weight_materializations=bounded_int(
                materialization["decode_time_full_weight_materializations"],
                "acceleration_materialization_count_invalid",
                minimum=0,
                maximum=1 << 32,
            ),
            packed_state_bounded_bytes=bounded_int(
                materialization["packed_state_bounded_bytes"],
                "acceleration_packed_state_invalid",
                minimum=0,
                maximum=1 << 60,
            ),
            stage_local_kv=_require_bool(
                record["stage_local_kv"], "acceleration_stage_local_kv_invalid"
            ),
            workspace_bounded_bytes=bounded_int(
                record["workspace_bounded_bytes"],
                "acceleration_workspace_invalid",
                minimum=1,
                maximum=1 << 60,
            ),
            cancellation_supported=_require_bool(
                record["cancellation_supported"], "acceleration_cancellation_invalid"
            ),
            cleanup_supported=_require_bool(
                record["cleanup_supported"], "acceleration_cleanup_invalid"
            ),
            issued_at=issued_at,
            expires_at=expires_at,
        )

    def to_mapping(self) -> dict[str, Any]:
        return {
            "protocol": PROTOCOL,
            "profile_id": self.profile_id,
            "backend_id": self.backend_id,
            "registry_stage": self.registry_stage,
            "implementation_family": self.implementation_family,
            "execution_provider": self.execution_provider,
            "build_digest": self.build_digest,
            "device_class": self.device_class,
            "driver_runtime": {
                "identity": self.driver_runtime_identity,
                "version_digest": self.driver_runtime_digest,
            },
            "supported_operations": list(self.supported_operations),
            "kernels": list(self.kernels),
            "precision_policy": {
                "weight_precision": self.weight_precision,
                "activation_precisions": list(self.activation_precisions),
                "per_decode_full_weight_cast": self.per_decode_full_weight_cast,
            },
            "materialization_policy": {
                "persistent_state_boundary": self.persistent_state_boundary,
                "decode_time_full_weight_materializations": self.decode_time_full_weight_materializations,
                "packed_state_bounded_bytes": self.packed_state_bounded_bytes,
            },
            "stage_local_kv": self.stage_local_kv,
            "workspace_bounded_bytes": self.workspace_bounded_bytes,
            "cancellation_supported": self.cancellation_supported,
            "cleanup_supported": self.cleanup_supported,
            "issued_at": self.issued_at,
            "expires_at": self.expires_at,
        }

    def canonical_bytes(self) -> bytes:
        return canonical_bytes(self.to_mapping(), "acceleration_profile_noncanonical")

    @property
    def digest(self) -> str:
        return record_digest(self.to_mapping(), "acceleration_profile_noncanonical")

    @property
    def correctness_only(self) -> bool:
        return (
            self.backend_id in CORRECTNESS_ONLY_BACKENDS
            or self.per_decode_full_weight_cast
        )

    def assert_acceleration_claim(self) -> None:
        """A provider record may never present adapter presence as acceleration."""

        require(
            self.backend_id not in CORRECTNESS_ONLY_BACKENDS,
            "correctness_only_backend_cannot_accelerate",
        )
        require(
            not self.per_decode_full_weight_cast,
            "per_decode_full_weight_cast_is_correctness_only",
        )
        require(
            self.decode_time_full_weight_materializations == 0,
            "decode_time_full_weight_materialization_present",
        )
        require(self.registry_stage == "implemented", "backend_not_implemented")
        require(self.stage_local_kv, "stage_local_kv_required")
        require(self.cancellation_supported and self.cleanup_supported, "lifecycle_support_required")


def assert_accelerated_windows_claim(profile: AccelerationProfile, *, platform_family: str) -> None:
    """Windows acceleration is an executed, exact claim — never an OS inference."""

    require(platform_family == "windows", "not_a_windows_profile")
    require(
        profile.device_class == "windows_x86_64",
        "windows_acceleration_requires_native_x86_64_device_class",
    )
    profile.assert_acceleration_claim()


def _require_bool(value: Any, code: str) -> bool:
    require(isinstance(value, bool), code)
    return value
