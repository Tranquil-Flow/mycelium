# SPDX-License-Identifier: AGPL-3.0-only
"""``mycelium.stage_performance_observation.v1`` — signed component cost evidence.

Spec §3.4, §4.7.  Every stage observation separates component compute time
(embedding, decoder range, final normalization, vocabulary head, token
selection), conversion/serialization/queue/transport/publication time and bytes,
and the thermal/sample/concurrency envelope it was measured under.

The vocabulary head is an explicit component cost; it is never approximated as
one ordinary decoder layer.  Materialization counters are monotonic and
component-scoped: after warmup a decode window must show zero full-weight
materializations.  A component with no samples reports ``null`` — never zero:
unknown or stale component cost makes the affected placement
performance-ineligible rather than zero-cost (frozen decision
``component_aware_performance_authority``).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from .primitives import (
    STAGE_OBSERVATION_PROTOCOL,
    bounded_int,
    canonical_bytes,
    closed_fields,
    digest,
    enum_value,
    finite,
    nullable_bounded_int,
    nullable_finite,
    record_digest,
    require,
    segment,
    text,
)

PROTOCOL = STAGE_OBSERVATION_PROTOCOL

#: Compute components are explicit; the last five are the conversion/edge costs
#: the planner reconciles separately (spec §3.4).
COMPUTE_COMPONENTS = (
    "embedding",
    "decoder_range",
    "final_norm",
    "vocab_head",
    "token_selection",
)
EDGE_COMPONENTS = (
    "conversion",
    "serialization",
    "queue",
    "transport",
    "publication",
)
COMPONENT_KEYS = COMPUTE_COMPONENTS + EDGE_COMPONENTS
PHASES = frozenset({"prefill", "decode"})
THERMAL_STATES = frozenset({"nominal", "fair", "serious", "critical", "unknown"})

FIELDS = frozenset(
    {
        "protocol",
        "observation_id",
        "node_id",
        "profile_digest",
        "provider",
        "phase",
        "components",
        "end_to_end_ms",
        "materialization",
        "thermal",
        "concurrency",
        "bucket",
        "sample_count",
        "uncertainty_ms",
        "warm",
        "issued_at",
        "expires_at",
    }
)
PROVIDER_FIELDS = frozenset(
    {"backend_id", "build_digest", "device_class", "driver_runtime_digest"}
)
COMPONENT_FIELDS = frozenset({"time_ms", "bytes", "samples"})
MATERIALIZATION_FIELDS = frozenset({"full_weight_materializations", "packed_state_bytes"})
BUCKET_FIELDS = frozenset({"context_tokens", "output_tokens"})


@dataclass(frozen=True, slots=True)
class ComponentCost:
    """One component's cost: ``null`` when it was not observed, never zero."""

    time_ms: float | None
    bytes: int | None
    samples: int

    @classmethod
    def from_mapping(cls, key: str, value: Mapping[str, Any]) -> "ComponentCost":
        record = closed_fields(value, COMPONENT_FIELDS, f"component_cost_invalid:{key}")
        samples = bounded_int(
            record["samples"], f"component_samples_invalid:{key}", minimum=0, maximum=1 << 32
        )
        time_ms = nullable_finite(
            record["time_ms"], f"component_time_invalid:{key}", minimum=0.0, maximum=1.0e9
        )
        byte_count = nullable_bounded_int(
            record["bytes"], f"component_bytes_invalid:{key}", minimum=0, maximum=1 << 60
        )
        if samples == 0:
            require(
                time_ms is None and byte_count is None,
                f"component_cost_unknown_must_be_null:{key}",
            )
        else:
            require(
                time_ms is not None and byte_count is not None,
                f"component_cost_observed_must_be_present:{key}",
            )
        return cls(time_ms=time_ms, bytes=byte_count, samples=samples)

    def to_mapping(self) -> dict[str, Any]:
        return {"time_ms": self.time_ms, "bytes": self.bytes, "samples": self.samples}

    @property
    def is_unknown(self) -> bool:
        return self.samples == 0


@dataclass(frozen=True, slots=True)
class StagePerformanceObservation:
    observation_id: str
    node_id: str
    profile_digest: str
    backend_id: str
    build_digest: str
    device_class: str
    driver_runtime_digest: str
    phase: str
    components: Mapping[str, ComponentCost]
    end_to_end_ms: float
    full_weight_materializations: int
    packed_state_bytes: int
    thermal_state: str
    concurrency: int
    context_tokens: int
    output_tokens: int
    sample_count: int
    uncertainty_ms: float
    warm: bool
    issued_at: float
    expires_at: float

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "StagePerformanceObservation":
        record = closed_fields(value, FIELDS, "stage_observation_invalid")
        require(record["protocol"] == PROTOCOL, "stage_observation_protocol_invalid")
        provider = closed_fields(
            record["provider"], PROVIDER_FIELDS, "stage_observation_provider_invalid"
        )
        materialization = closed_fields(
            record["materialization"],
            MATERIALIZATION_FIELDS,
            "stage_observation_materialization_invalid",
        )
        bucket = closed_fields(
            record["bucket"], BUCKET_FIELDS, "stage_observation_bucket_invalid"
        )
        components = closed_fields(
            record["components"],
            frozenset(COMPONENT_KEYS),
            "stage_observation_components_invalid",
        )
        validated = {
            key: ComponentCost.from_mapping(key, components[key]) for key in COMPONENT_KEYS
        }
        issued_at = finite(
            record["issued_at"], "stage_observation_issued_at_invalid", minimum=0.0
        )
        expires_at = finite(
            record["expires_at"], "stage_observation_expires_at_invalid", minimum=0.0
        )
        require(expires_at > issued_at, "stage_observation_validity_invalid")
        return cls(
            observation_id=segment(
                record["observation_id"], "stage_observation_id_invalid"
            ),
            node_id=segment(record["node_id"], "stage_observation_node_id_invalid"),
            profile_digest=digest(
                record["profile_digest"], "stage_observation_profile_digest_invalid"
            ),
            backend_id=text(provider["backend_id"], "stage_observation_backend_invalid"),
            build_digest=digest(provider["build_digest"], "stage_observation_build_invalid"),
            device_class=text(
                provider["device_class"], "stage_observation_device_class_invalid"
            ),
            driver_runtime_digest=digest(
                provider["driver_runtime_digest"], "stage_observation_driver_digest_invalid"
            ),
            phase=enum_value(record["phase"], PHASES, "stage_observation_phase_invalid"),
            components=validated,
            end_to_end_ms=finite(
                record["end_to_end_ms"], "stage_observation_end_to_end_invalid", minimum=0.0
            ),
            full_weight_materializations=bounded_int(
                materialization["full_weight_materializations"],
                "stage_observation_materialization_count_invalid",
                minimum=0,
                maximum=1 << 40,
            ),
            packed_state_bytes=bounded_int(
                materialization["packed_state_bytes"],
                "stage_observation_packed_state_invalid",
                minimum=0,
                maximum=1 << 60,
            ),
            thermal_state=enum_value(
                record["thermal"], THERMAL_STATES, "stage_observation_thermal_invalid"
            ),
            concurrency=bounded_int(
                record["concurrency"],
                "stage_observation_concurrency_invalid",
                minimum=1,
                maximum=1 << 20,
            ),
            context_tokens=bounded_int(
                bucket["context_tokens"],
                "stage_observation_context_invalid",
                minimum=1,
                maximum=1 << 30,
            ),
            output_tokens=bounded_int(
                bucket["output_tokens"],
                "stage_observation_output_invalid",
                minimum=0,
                maximum=1 << 30,
            ),
            sample_count=bounded_int(
                record["sample_count"],
                "stage_observation_samples_invalid",
                minimum=1,
                maximum=1 << 32,
            ),
            uncertainty_ms=finite(
                record["uncertainty_ms"],
                "stage_observation_uncertainty_invalid",
                minimum=0.0,
            ),
            warm=require_bool(record["warm"], "stage_observation_warm_invalid"),
            issued_at=issued_at,
            expires_at=expires_at,
        )

    def to_mapping(self) -> dict[str, Any]:
        return {
            "protocol": PROTOCOL,
            "observation_id": self.observation_id,
            "node_id": self.node_id,
            "profile_digest": self.profile_digest,
            "provider": {
                "backend_id": self.backend_id,
                "build_digest": self.build_digest,
                "device_class": self.device_class,
                "driver_runtime_digest": self.driver_runtime_digest,
            },
            "phase": self.phase,
            "components": {key: self.components[key].to_mapping() for key in COMPONENT_KEYS},
            "end_to_end_ms": self.end_to_end_ms,
            "materialization": {
                "full_weight_materializations": self.full_weight_materializations,
                "packed_state_bytes": self.packed_state_bytes,
            },
            "thermal": self.thermal_state,
            "concurrency": self.concurrency,
            "bucket": {
                "context_tokens": self.context_tokens,
                "output_tokens": self.output_tokens,
            },
            "sample_count": self.sample_count,
            "uncertainty_ms": self.uncertainty_ms,
            "warm": self.warm,
            "issued_at": self.issued_at,
            "expires_at": self.expires_at,
        }

    def canonical_bytes(self) -> bytes:
        return canonical_bytes(self.to_mapping(), "stage_observation_noncanonical")

    @property
    def digest(self) -> str:
        return record_digest(self.to_mapping(), "stage_observation_noncanonical")

    def unknown_components(self) -> tuple[str, ...]:
        return tuple(key for key in COMPONENT_KEYS if self.components[key].is_unknown)

    def component_total_ms(self) -> float:
        """Sum of known components; unknown components make the placement ineligible."""

        require(
            not self.unknown_components(),
            "component_cost_unknown_not_zero:"
            + ",".join(self.unknown_components()),
        )
        return sum(
            self.components[key].time_ms or 0.0 for key in COMPONENT_KEYS
        )

    def reconciles(self) -> bool:
        """Known-component totals reconcile with end-to-end within uncertainty."""

        if self.unknown_components():
            return False
        return abs(self.component_total_ms() - self.end_to_end_ms) <= self.uncertainty_ms

    def assert_reconciles(self) -> None:
        require(self.reconciles(), "component_totals_do_not_reconcile")

    def vocabulary_head_cost_ms(self) -> float:
        """The vocabulary head is an explicit component, never folded into decoder."""

        return self.components["vocab_head"].time_ms or 0.0


def assert_zero_decode_materialization(observation: StagePerformanceObservation) -> None:
    """A warm decode window must show zero full-weight materializations."""

    if observation.phase == "decode" and observation.warm:
        require(
            observation.full_weight_materializations == 0,
            "decode_time_full_weight_materialization_detected",
        )


def assert_monotonic_materialization(
    previous: StagePerformanceObservation, current: StagePerformanceObservation
) -> None:
    """Counters are monotonic; bounded packed state may not grow with output tokens."""

    require(
        current.full_weight_materializations >= previous.full_weight_materializations,
        "materialization_counter_regressed",
    )
    require(
        current.output_tokens <= previous.output_tokens
        or current.packed_state_bytes <= previous.packed_state_bytes,
        "packed_state_grows_with_output_tokens",
    )


def component_rate_breakdown(observation: StagePerformanceObservation) -> dict[str, float]:
    """Confidence-bounded component service rates the planner consumes."""

    return {
        key: (observation.components[key].time_ms or 0.0)
        / max(observation.components[key].samples, 1)
        for key in COMPONENT_KEYS
    }


def require_bool(value: Any, code: str) -> bool:
    require(isinstance(value, bool), code)
    return value
