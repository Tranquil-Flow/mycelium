# SPDX-License-Identifier: AGPL-3.0-only
"""``mycelium.runtime_efficiency_qualification.v1`` — the frozen performance decision.

Spec §3.4, §4.9.  Binds the reference/candidate benchmark, the no-rematerialization
proof, the component-aware planner result, TTFT/TPOT/rate statistics and their
confidence, reliability and cleanup accounting, and the performance-eligibility
decision for one exact provider/device/build, model representation, component
roles, activation codec, workload envelope, and thermal state.

Thresholds are frozen constants; they are not weakened after measurement.  A
miss is published honestly: the candidate stays visible and performance-
ineligible.  NumPy correctness, package presence, or a synthetic probe cannot
mint accelerated eligibility (frozen decisions ``accelerated_windows_backend``
and ``component_aware_performance_authority``).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from .primitives import (
    EFFICIENCY_QUALIFICATION_PROTOCOL,
    CORRECTNESS_ONLY_BACKENDS,
    bounded_int,
    canonical_bytes,
    closed_fields,
    digest,
    enum_value,
    finite,
    record_digest,
    require,
    segment,
    text,
)

PROTOCOL = EFFICIENCY_QUALIFICATION_PROTOCOL

#: Frozen benchmark manifest shape and thresholds (spec §3.4).  They are not
#: parameters; a record that declares different thresholds is rejected.
MIN_PAIRED_WINDOWS = 3
MIN_REQUESTS_PER_MODE = 24
PROMPT_BUCKETS = 2
OUTPUT_BUCKETS = 2
MIN_GENERATED_TOKENS_PER_MODE = 512
MIN_MEDIAN_DECODE_RATIO = 2.0
MIN_PAIRED_LOWER_BOUND = 1.5
MIN_MEDIAN_DECODE_TOKENS_PER_SECOND = 8.0
MAX_INTERACTIVE_P95_TTFT_MS = 1_500.0
THRESHOLDS = MappingProxyType(
    {
        "min_median_decode_ratio": MIN_MEDIAN_DECODE_RATIO,
        "min_paired_confidence_lower_bound": MIN_PAIRED_LOWER_BOUND,
        "min_median_decode_tokens_per_second": MIN_MEDIAN_DECODE_TOKENS_PER_SECOND,
        "max_interactive_p95_ttft_ms": MAX_INTERACTIVE_P95_TTFT_MS,
    }
)
FROZEN_THERMAL_STATES = frozenset({"nominal", "fair"})
DECISIONS = frozenset({"performance_ready", "not_performance_ready", "unknown"})

FIELDS = frozenset(
    {
        "protocol",
        "qualification_id",
        "provider_binding",
        "model_binding",
        "benchmark",
        "results",
        "reliability",
        "cleanup",
        "materialization_proof",
        "planner_result",
        "evidence_generation",
        "decision",
        "issued_at",
        "expires_at",
    }
)
PROVIDER_FIELDS = frozenset(
    {"backend_id", "build_digest", "device_class", "driver_runtime_digest"}
)
MODEL_FIELDS = frozenset({"model_id", "revision", "representation", "profile_digest"})
BENCHMARK_FIELDS = frozenset(
    {
        "manifest_digest",
        "paired_windows",
        "requests_per_mode",
        "prompt_buckets",
        "output_buckets",
        "generated_tokens_per_mode",
        "thresholds",
        "thermal_state",
        "measured_at",
    }
)
RESULTS_FIELDS = frozenset(
    {
        "median_decode_ratio",
        "paired_confidence_lower_bound",
        "median_decode_tokens_per_second",
        "p95_ttft_ms",
    }
)
RELIABILITY_FIELDS = frozenset({"failures", "cancellations", "timeouts", "excluded"})
CLEANUP_FIELDS = frozenset({"request_scoped_cleanup", "stage_local_kv"})
MATERIALIZATION_FIELDS = frozenset(
    {"warm_decode_full_weight_materializations", "warm_decode_windows"}
)
PLANNER_FIELDS = frozenset(
    {"vocabulary_head_cost_explicit", "unknown_component_costs", "prediction_error_ms"}
)
DECISION_FIELDS = frozenset({"state", "blockers"})


@dataclass(frozen=True, slots=True)
class RuntimeEfficiencyQualification:
    qualification_id: str
    backend_id: str
    provider_build_digest: str
    device_class: str
    driver_runtime_digest: str
    model_id: str
    revision: str
    representation: str
    profile_digest: str
    manifest_digest: str
    paired_windows: int
    requests_per_mode: int
    prompt_buckets: int
    output_buckets: int
    generated_tokens_per_mode: int
    thermal_state: str
    measured_at: float
    median_decode_ratio: float
    paired_confidence_lower_bound: float
    median_decode_tokens_per_second: float
    p95_ttft_ms: float
    failures: int
    cancellations: int
    timeouts: int
    excluded: int
    request_scoped_cleanup: bool
    stage_local_kv: bool
    warm_decode_full_weight_materializations: int
    warm_decode_windows: int
    vocabulary_head_cost_explicit: bool
    unknown_component_costs: tuple[str, ...]
    prediction_error_ms: float
    evidence_generation: int
    decision: str
    blockers: tuple[str, ...]
    issued_at: float
    expires_at: float

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "RuntimeEfficiencyQualification":
        record = closed_fields(value, FIELDS, "efficiency_qualification_invalid")
        require(record["protocol"] == PROTOCOL, "efficiency_qualification_protocol_invalid")
        provider = closed_fields(
            record["provider_binding"], PROVIDER_FIELDS, "efficiency_provider_invalid"
        )
        model = closed_fields(record["model_binding"], MODEL_FIELDS, "efficiency_model_invalid")
        benchmark = closed_fields(
            record["benchmark"], BENCHMARK_FIELDS, "efficiency_benchmark_invalid"
        )
        results = closed_fields(record["results"], RESULTS_FIELDS, "efficiency_results_invalid")
        reliability = closed_fields(
            record["reliability"], RELIABILITY_FIELDS, "efficiency_reliability_invalid"
        )
        cleanup = closed_fields(record["cleanup"], CLEANUP_FIELDS, "efficiency_cleanup_invalid")
        materialization = closed_fields(
            record["materialization_proof"],
            MATERIALIZATION_FIELDS,
            "efficiency_materialization_invalid",
        )
        planner = closed_fields(
            record["planner_result"], PLANNER_FIELDS, "efficiency_planner_invalid"
        )
        decision_record = closed_fields(
            record["decision"], DECISION_FIELDS, "efficiency_decision_invalid"
        )
        state = enum_value(
            decision_record["state"], DECISIONS, "efficiency_decision_state_invalid"
        )
        declared_blockers = tuple(
            text(item, "efficiency_blocker_invalid")
            for item in _blocker_list(decision_record["blockers"])
        )

        thresholds = closed_fields(
            benchmark["thresholds"], frozenset(THRESHOLDS), "efficiency_thresholds_invalid"
        )
        for name, frozen in THRESHOLDS.items():
            require(
                thresholds[name] == frozen,
                f"efficiency_threshold_not_frozen:{name}",
            )

        manifest_digest = digest(benchmark["manifest_digest"], "efficiency_manifest_invalid")
        paired_windows = bounded_int(
            benchmark["paired_windows"], "efficiency_windows_invalid", minimum=0, maximum=1 << 20
        )
        requests_per_mode = bounded_int(
            benchmark["requests_per_mode"],
            "efficiency_requests_invalid",
            minimum=0,
            maximum=1 << 30,
        )
        prompt_buckets = bounded_int(
            benchmark["prompt_buckets"], "efficiency_prompt_buckets_invalid", minimum=0, maximum=1 << 10
        )
        output_buckets = bounded_int(
            benchmark["output_buckets"], "efficiency_output_buckets_invalid", minimum=0, maximum=1 << 10
        )
        generated_tokens = bounded_int(
            benchmark["generated_tokens_per_mode"],
            "efficiency_generated_tokens_invalid",
            minimum=0,
            maximum=1 << 40,
        )
        thermal_state = enum_value(
            benchmark["thermal_state"],
            frozenset({"nominal", "fair", "serious", "critical", "unknown"}),
            "efficiency_thermal_invalid",
        )
        measured_at = finite(
            benchmark["measured_at"], "efficiency_measured_at_invalid", minimum=0.0
        )
        readings = {
            "median_decode_ratio": finite(
                results["median_decode_ratio"], "efficiency_result_invalid", minimum=0.0
            ),
            "paired_confidence_lower_bound": finite(
                results["paired_confidence_lower_bound"],
                "efficiency_result_invalid",
                minimum=0.0,
            ),
            "median_decode_tokens_per_second": finite(
                results["median_decode_tokens_per_second"],
                "efficiency_result_invalid",
                minimum=0.0,
            ),
            "p95_ttft_ms": finite(results["p95_ttft_ms"], "efficiency_result_invalid", minimum=0.0),
        }
        failure_counts = {
            "failures": bounded_int(reliability["failures"], "efficiency_reliability_invalid", minimum=0),
            "cancellations": bounded_int(
                reliability["cancellations"], "efficiency_reliability_invalid", minimum=0
            ),
            "timeouts": bounded_int(
                reliability["timeouts"], "efficiency_reliability_invalid", minimum=0
            ),
            "excluded": bounded_int(
                reliability["excluded"], "efficiency_reliability_invalid", minimum=0
            ),
        }
        # Failed, cancelled, or timed-out requests stay in reliability accounting;
        # a record that excludes them is invalid, not merely not-ready.
        require(
            failure_counts["excluded"] == 0, "excluded_failures_not_allowed"
        )
        unknown_costs = tuple(
            text(item, "efficiency_unknown_cost_invalid")
            for item in _blocker_list(planner["unknown_component_costs"])
        )
        expected_state, expected_blockers = evaluate_performance(
            backend_id=provider["backend_id"],
            paired_windows=paired_windows,
            requests_per_mode=requests_per_mode,
            prompt_buckets=prompt_buckets,
            output_buckets=output_buckets,
            generated_tokens_per_mode=generated_tokens,
            thermal_state=thermal_state,
            readings=readings,
            failure_counts=failure_counts,
            cleanup=cleanup,
            materialization=materialization,
            planner=planner,
            unknown_costs=unknown_costs,
        )
        require(
            state == expected_state and declared_blockers == expected_blockers,
            "efficiency_decision_incoherent",
        )
        issued_at = finite(record["issued_at"], "efficiency_issued_at_invalid", minimum=0.0)
        expires_at = finite(record["expires_at"], "efficiency_expires_at_invalid", minimum=0.0)
        require(expires_at > issued_at, "efficiency_validity_invalid")
        return cls(
            qualification_id=segment(
                record["qualification_id"], "efficiency_qualification_id_invalid"
            ),
            backend_id=text(provider["backend_id"], "efficiency_backend_invalid"),
            provider_build_digest=digest(
                provider["build_digest"], "efficiency_provider_digest_invalid"
            ),
            device_class=text(provider["device_class"], "efficiency_device_class_invalid"),
            driver_runtime_digest=digest(
                provider["driver_runtime_digest"], "efficiency_driver_digest_invalid"
            ),
            model_id=text(model["model_id"], "efficiency_model_id_invalid"),
            revision=text(model["revision"], "efficiency_revision_invalid"),
            representation=text(model["representation"], "efficiency_representation_invalid"),
            profile_digest=digest(model["profile_digest"], "efficiency_profile_digest_invalid"),
            manifest_digest=manifest_digest,
            paired_windows=paired_windows,
            requests_per_mode=requests_per_mode,
            prompt_buckets=prompt_buckets,
            output_buckets=output_buckets,
            generated_tokens_per_mode=generated_tokens,
            thermal_state=thermal_state,
            measured_at=measured_at,
            median_decode_ratio=readings["median_decode_ratio"],
            paired_confidence_lower_bound=readings["paired_confidence_lower_bound"],
            median_decode_tokens_per_second=readings["median_decode_tokens_per_second"],
            p95_ttft_ms=readings["p95_ttft_ms"],
            failures=failure_counts["failures"],
            cancellations=failure_counts["cancellations"],
            timeouts=failure_counts["timeouts"],
            excluded=failure_counts["excluded"],
            request_scoped_cleanup=require_bool(
                cleanup["request_scoped_cleanup"], "efficiency_cleanup_invalid"
            ),
            stage_local_kv=require_bool(cleanup["stage_local_kv"], "efficiency_cleanup_invalid"),
            warm_decode_full_weight_materializations=bounded_int(
                materialization["warm_decode_full_weight_materializations"],
                "efficiency_materialization_invalid",
                minimum=0,
                maximum=1 << 40,
            ),
            warm_decode_windows=bounded_int(
                materialization["warm_decode_windows"],
                "efficiency_materialization_windows_invalid",
                minimum=0,
                maximum=1 << 20,
            ),
            vocabulary_head_cost_explicit=require_bool(
                planner["vocabulary_head_cost_explicit"], "efficiency_planner_invalid"
            ),
            unknown_component_costs=unknown_costs,
            prediction_error_ms=finite(
                planner["prediction_error_ms"], "efficiency_planner_invalid", minimum=0.0
            ),
            evidence_generation=bounded_int(
                record["evidence_generation"],
                "efficiency_evidence_generation_invalid",
                minimum=1,
                maximum=1 << 40,
            ),
            decision=state,
            blockers=declared_blockers,
            issued_at=issued_at,
            expires_at=expires_at,
        )

    def to_mapping(self) -> dict[str, Any]:
        return {
            "protocol": PROTOCOL,
            "qualification_id": self.qualification_id,
            "provider_binding": {
                "backend_id": self.backend_id,
                "build_digest": self.provider_build_digest,
                "device_class": self.device_class,
                "driver_runtime_digest": self.driver_runtime_digest,
            },
            "model_binding": {
                "model_id": self.model_id,
                "revision": self.revision,
                "representation": self.representation,
                "profile_digest": self.profile_digest,
            },
            "benchmark": {
                "manifest_digest": self.manifest_digest,
                "paired_windows": self.paired_windows,
                "requests_per_mode": self.requests_per_mode,
                "prompt_buckets": self.prompt_buckets,
                "output_buckets": self.output_buckets,
                "generated_tokens_per_mode": self.generated_tokens_per_mode,
                "thresholds": dict(THRESHOLDS),
                "thermal_state": self.thermal_state,
                "measured_at": self.measured_at,
            },
            "results": {
                "median_decode_ratio": self.median_decode_ratio,
                "paired_confidence_lower_bound": self.paired_confidence_lower_bound,
                "median_decode_tokens_per_second": self.median_decode_tokens_per_second,
                "p95_ttft_ms": self.p95_ttft_ms,
            },
            "reliability": {
                "failures": self.failures,
                "cancellations": self.cancellations,
                "timeouts": self.timeouts,
                "excluded": self.excluded,
            },
            "cleanup": {
                "request_scoped_cleanup": self.request_scoped_cleanup,
                "stage_local_kv": self.stage_local_kv,
            },
            "materialization_proof": {
                "warm_decode_full_weight_materializations": self.warm_decode_full_weight_materializations,
                "warm_decode_windows": self.warm_decode_windows,
            },
            "planner_result": {
                "vocabulary_head_cost_explicit": self.vocabulary_head_cost_explicit,
                "unknown_component_costs": list(self.unknown_component_costs),
                "prediction_error_ms": self.prediction_error_ms,
            },
            "evidence_generation": self.evidence_generation,
            "decision": {"state": self.decision, "blockers": list(self.blockers)},
            "issued_at": self.issued_at,
            "expires_at": self.expires_at,
        }

    def canonical_bytes(self) -> bytes:
        return canonical_bytes(self.to_mapping(), "efficiency_qualification_noncanonical")

    @property
    def digest(self) -> str:
        return record_digest(self.to_mapping(), "efficiency_qualification_noncanonical")

    def is_current(
        self,
        *,
        now: float,
        evidence_generation: int,
        provider_build_digest: str,
        driver_runtime_digest: str,
        device_class: str,
    ) -> bool:
        """Any provider/driver/kernel/device change makes this decision stale."""

        return (
            self.decision == "performance_ready"
            and now <= self.expires_at
            and self.evidence_generation == evidence_generation
            and self.provider_build_digest == provider_build_digest
            and self.driver_runtime_digest == driver_runtime_digest
            and self.device_class == device_class
        )


def evaluate_performance(
    *,
    backend_id: str,
    paired_windows: int,
    requests_per_mode: int,
    prompt_buckets: int,
    output_buckets: int,
    generated_tokens_per_mode: int,
    thermal_state: str,
    readings: Mapping[str, float],
    failure_counts: Mapping[str, int],
    cleanup: Mapping[str, Any],
    materialization: Mapping[str, Any],
    planner: Mapping[str, Any],
    unknown_costs: tuple[str, ...],
) -> tuple[str, tuple[str, ...]]:
    """The pure decision: every gate must pass; a miss is published, never hidden."""

    blockers: list[str] = []
    if backend_id in CORRECTNESS_ONLY_BACKENDS:
        blockers.append("portable_backend_cannot_be_performance_ready")
    if paired_windows < MIN_PAIRED_WINDOWS:
        blockers.append("insufficient_paired_windows")
    if requests_per_mode < MIN_REQUESTS_PER_MODE:
        blockers.append("insufficient_requests_per_mode")
    if prompt_buckets != PROMPT_BUCKETS or output_buckets != OUTPUT_BUCKETS:
        blockers.append("bucket_coverage_incomplete")
    if generated_tokens_per_mode < MIN_GENERATED_TOKENS_PER_MODE:
        blockers.append("short_output_insufficient_tokens")
    if thermal_state not in FROZEN_THERMAL_STATES:
        blockers.append("thermally_drifted_samples")
    if readings["median_decode_ratio"] < MIN_MEDIAN_DECODE_RATIO:
        blockers.append("below_decode_ratio_floor")
    if readings["paired_confidence_lower_bound"] <= MIN_PAIRED_LOWER_BOUND:
        blockers.append("paired_confidence_lower_bound_not_met")
    if readings["median_decode_tokens_per_second"] < MIN_MEDIAN_DECODE_TOKENS_PER_SECOND:
        blockers.append("below_single_request_floor")
    if readings["p95_ttft_ms"] > MAX_INTERACTIVE_P95_TTFT_MS:
        blockers.append("interactive_ttft_above_limit")
    if any(failure_counts[name] for name in ("failures", "cancellations", "timeouts")):
        blockers.append("reliability_regression")
    if failure_counts["excluded"]:
        blockers.append("excluded_failures_not_allowed")
    if not cleanup.get("request_scoped_cleanup") or not cleanup.get("stage_local_kv"):
        blockers.append("cleanup_not_proven")
    if materialization.get("warm_decode_full_weight_materializations"):
        blockers.append("materialization_detected")
    if not materialization.get("warm_decode_windows"):
        blockers.append("materialization_windows_missing")
    if unknown_costs:
        blockers.append("unknown_component_cost_present")
    if not planner.get("vocabulary_head_cost_explicit"):
        blockers.append("vocabulary_head_cost_not_explicit")
    if blockers:
        return "not_performance_ready", tuple(blockers)
    return "performance_ready", ()


def decide_efficiency(record: Mapping[str, Any]) -> RuntimeEfficiencyQualification:
    """Author a record through the frozen decision (never declared by hand)."""

    provider = record.get("provider_binding") or {}
    benchmark = record.get("benchmark") or {}
    results = record.get("results") or {}
    reliability = record.get("reliability") or {}
    cleanup = record.get("cleanup") or {}
    materialization = record.get("materialization_proof") or {}
    planner = record.get("planner_result") or {}
    unknown_costs = tuple(planner.get("unknown_component_costs") or ())
    state, blockers = evaluate_performance(
        backend_id=provider.get("backend_id"),
        paired_windows=benchmark.get("paired_windows"),
        requests_per_mode=benchmark.get("requests_per_mode"),
        prompt_buckets=benchmark.get("prompt_buckets"),
        output_buckets=benchmark.get("output_buckets"),
        generated_tokens_per_mode=benchmark.get("generated_tokens_per_mode"),
        thermal_state=benchmark.get("thermal_state"),
        readings={
            key: results.get(key)
            for key in (
                "median_decode_ratio",
                "paired_confidence_lower_bound",
                "median_decode_tokens_per_second",
                "p95_ttft_ms",
            )
        },
        failure_counts={
            key: reliability.get(key) for key in ("failures", "cancellations", "timeouts", "excluded")
        },
        cleanup=cleanup,
        materialization=materialization,
        planner=planner,
        unknown_costs=unknown_costs,
    )
    return RuntimeEfficiencyQualification.from_mapping(
        {**record, "decision": {"state": state, "blockers": list(blockers)}}
    )


def _blocker_list(value: Any) -> list[Any]:
    require(isinstance(value, (list, tuple)), "efficiency_blockers_invalid")
    return list(value)


def require_bool(value: Any, code: str) -> bool:
    require(isinstance(value, bool), code)
    return value
