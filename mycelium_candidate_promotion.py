"""Deterministic canary and performance decision for planner-v2 candidates."""
from __future__ import annotations

import copy
import math
import statistics
from typing import Any, Mapping, Sequence

from mycelium_performance_budget import validate_performance_budget


PROTOCOL = "mycelium.candidate_promotion_report.v1"
_OBSERVATION_FIELDS = frozenset(
    {
        "case_id",
        "completed",
        "quality_passed",
        "negative_check_passed",
        "ttft_ms",
        "tpot_ms",
        "output_tokens_per_second",
        "peak_rss_bytes_by_member",
        "frames_per_request_by_stage",
    }
)


def _finite_positive(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be finite and positive")
    result = float(value)
    if not math.isfinite(result) or result <= 0:
        raise ValueError(f"{name} must be finite and positive")
    return result


def _bounded_counts(value: object, name: str) -> dict[str, int]:
    if (
        not isinstance(value, Mapping)
        or not value
        or len(value) > 256
        or any(
            not isinstance(key, str)
            or not key
            or type(item) is not int
            or item < 0
            for key, item in value.items()
        )
    ):
        raise ValueError(f"{name} must be a bounded non-negative integer map")
    return {str(key): int(item) for key, item in value.items()}


def _percentile(values: Sequence[float], percentile: float) -> float:
    ordered = sorted(values)
    index = max(0, math.ceil(percentile * len(ordered)) - 1)
    return ordered[index]


def evaluate_candidate_promotion(
    *,
    candidate_deployment_id: str,
    incumbent_deployment_id: str,
    planner_snapshot_digest: str,
    observations: Sequence[Mapping[str, Any]],
    performance_budget: Mapping[str, Any],
) -> dict[str, Any]:
    budget = validate_performance_budget(performance_budget)
    for name, value in (
        ("candidate_deployment_id", candidate_deployment_id),
        ("incumbent_deployment_id", incumbent_deployment_id),
        ("planner_snapshot_digest", planner_snapshot_digest),
    ):
        if not isinstance(value, str) or not value:
            raise ValueError(f"{name} must be non-empty")
    if candidate_deployment_id == incumbent_deployment_id:
        raise ValueError("candidate and incumbent deployments must differ")
    if not planner_snapshot_digest.startswith("sha256:") or len(planner_snapshot_digest) != 71:
        raise ValueError("planner_snapshot_digest must be a SHA-256 reference")
    if not isinstance(observations, Sequence) or isinstance(observations, (str, bytes)):
        raise ValueError("candidate observations must be a sequence")
    if not 2 <= len(observations) <= 256:
        raise ValueError("candidate promotion requires 2..256 bounded canary observations")

    normalized = []
    seen: set[str] = set()
    for observation in observations:
        if not isinstance(observation, Mapping) or set(observation) != _OBSERVATION_FIELDS:
            raise ValueError("candidate observation shape is invalid")
        case_id = observation.get("case_id")
        if not isinstance(case_id, str) or not case_id or case_id in seen:
            raise ValueError("candidate observation case_id must be unique")
        seen.add(case_id)
        booleans = {
            name: observation[name]
            for name in ("completed", "quality_passed", "negative_check_passed")
        }
        if any(type(value) is not bool for value in booleans.values()):
            raise ValueError("candidate observation outcomes must be boolean")
        normalized.append(
            {
                "case_id": case_id,
                **booleans,
                "ttft_ms": _finite_positive(observation["ttft_ms"], "ttft_ms"),
                "tpot_ms": _finite_positive(observation["tpot_ms"], "tpot_ms"),
                "output_tokens_per_second": _finite_positive(
                    observation["output_tokens_per_second"],
                    "output_tokens_per_second",
                ),
                "peak_rss_bytes_by_member": _bounded_counts(
                    observation["peak_rss_bytes_by_member"],
                    "peak_rss_bytes_by_member",
                ),
                "frames_per_request_by_stage": _bounded_counts(
                    observation["frames_per_request_by_stage"],
                    "frames_per_request_by_stage",
                ),
            }
        )

    ttft = [item["ttft_ms"] for item in normalized]
    tpot = [item["tpot_ms"] for item in normalized]
    throughput = [item["output_tokens_per_second"] for item in normalized]
    metrics = {
        "sample_size": len(normalized),
        "ttft_ms": {"p50": statistics.median(ttft), "p95": _percentile(ttft, 0.95)},
        "tpot_ms": {"p50": statistics.median(tpot), "p95": _percentile(tpot, 0.95)},
        "minimum_output_tokens_per_second": min(throughput),
        "maximum_peak_rss_bytes_by_member": {
            member: max(item["peak_rss_bytes_by_member"].get(member, 0) for item in normalized)
            for member in budget["maximum_peak_rss_bytes_by_member"]
        },
        "maximum_frames_per_request_by_stage": {
            stage: max(item["frames_per_request_by_stage"].get(stage, 0) for item in normalized)
            for stage in budget["maximum_frames_per_request_by_stage"]
        },
    }
    reasons: list[str] = []
    if len(normalized) < budget["minimum_sample_size"]:
        reasons.append("minimum_sample_size_not_met")
    if any(
        not item["completed"]
        or not item["quality_passed"]
        or not item["negative_check_passed"]
        for item in normalized
    ):
        reasons.append("canary_check_failed")
    for phase in ("ttft_ms", "tpot_ms"):
        if metrics[phase]["p50"] > budget[phase]["maximum_p50"]:
            reasons.append(f"{phase}_p50_exceeded")
        if metrics[phase]["p95"] > budget[phase]["maximum_p95"]:
            reasons.append(f"{phase}_p95_exceeded")
    if (
        metrics["minimum_output_tokens_per_second"]
        < budget["minimum_output_tokens_per_second"]
    ):
        reasons.append("minimum_output_tokens_per_second_not_met")
    for metric_name in (
        "maximum_peak_rss_bytes_by_member",
        "maximum_frames_per_request_by_stage",
    ):
        if any(
            metrics[metric_name][key] > maximum
            for key, maximum in budget[metric_name].items()
        ):
            reasons.append(f"{metric_name}_exceeded")
    return {
        "protocol": PROTOCOL,
        "candidate_deployment_id": candidate_deployment_id,
        "incumbent_deployment_id": incumbent_deployment_id,
        "planner_snapshot_digest": planner_snapshot_digest,
        "placement_provenance": "planner_v2",
        "performance_budget": budget,
        "observations": copy.deepcopy(normalized),
        "metrics": metrics,
        "decision": "promote" if not reasons else "reject",
        "reasons": reasons,
        "route_ready": False,
    }


def validate_candidate_promotion_report(document: Mapping[str, Any]) -> dict[str, Any]:
    expected = {
        "protocol",
        "candidate_deployment_id",
        "incumbent_deployment_id",
        "planner_snapshot_digest",
        "placement_provenance",
        "performance_budget",
        "observations",
        "metrics",
        "decision",
        "reasons",
        "route_ready",
    }
    if not isinstance(document, Mapping) or set(document) != expected:
        raise ValueError("candidate promotion report shape is invalid")
    rebuilt = evaluate_candidate_promotion(
        candidate_deployment_id=document["candidate_deployment_id"],
        incumbent_deployment_id=document["incumbent_deployment_id"],
        planner_snapshot_digest=document["planner_snapshot_digest"],
        observations=document["observations"],
        performance_budget=document["performance_budget"],
    )
    if dict(document) != rebuilt:
        raise ValueError("candidate promotion report is not derived from its observations")
    return rebuilt


__all__ = [
    "PROTOCOL",
    "evaluate_candidate_promotion",
    "validate_candidate_promotion_report",
]
