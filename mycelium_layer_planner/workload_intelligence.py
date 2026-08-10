"""M15 phase-aware workload comparison over one immutable planner snapshot."""

from __future__ import annotations

import copy
import hashlib
import json
import math
from typing import Any, Mapping, Sequence

from mycelium_performance_budget import validate_performance_budget_v2

from .contracts import WorkloadScenario
from .planner import plan_snapshot
from .workload import WorkloadProfile


PROTOCOL = "mycelium.m15_plan_comparison.v1"
POLICIES = ("balanced", "decode_tpot", "prefill_ttft")
_TOP_LEVEL_FIELDS = {
    "protocol",
    "planner_snapshot_digest",
    "evidence_bundle_digest",
    "profiles",
    "comparisons",
    "performance_budgets",
    "observations",
    "calibration_state",
    "deferred_to_m16",
    "route_ready",
    "claim_boundary",
}
_PRIVATE_FIELDS = {
    "prompt",
    "response",
    "prompt_text",
    "response_text",
    "token_ids",
    "tokens",
    "activation",
    "activations",
    "tensor",
    "tensors",
    "kv",
    "private_key",
    "credential",
    "artifact_root",
    "trace_path",
}


def _digest(value: object) -> str:
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _profile_document(profile: WorkloadProfile) -> dict[str, Any]:
    scenarios = [
        {
            "scenario_id": scenario.name,
            "prompt_p50_tokens": scenario.prompt_tokens,
            "prompt_p95_tokens": scenario.prompt_p95_tokens or scenario.prompt_tokens,
            "output_p50_tokens": scenario.output_tokens,
            "output_p95_tokens": scenario.output_p95_tokens or scenario.output_tokens,
            "modeled_concurrency": scenario.concurrency,
            "batch_size": scenario.batch_size,
            "qos_class": scenario.qos_class,
            "arrival_rate_rps": scenario.arrival_rate_rps,
            "probability": scenario.probability,
        }
        for scenario in profile.scenarios
    ]
    body = {
        "profile_id": profile.name,
        "source": profile.source,
        "trace_digest": profile.trace_digest,
        "trace_sample_count": profile.trace_sample_count,
        "content_removed": profile.content_removed,
        "mode": profile.mode,
        "arrival_process": profile.arrival_process,
        "scenarios": scenarios,
    }
    return {**body, "profile_digest": _digest(body)}


def _workload_input(profile: WorkloadProfile) -> dict[str, Any]:
    return {
        "name": profile.name,
        "mode": profile.mode,
        "source": profile.source,
        "arrival_process": profile.arrival_process,
        "average_turns": profile.average_turns,
        "scenarios": [
            {
                "name": scenario.name,
                "prompt_tokens": scenario.prompt_tokens,
                "output_tokens": scenario.output_tokens,
                "concurrency": scenario.concurrency,
                "probability": scenario.probability,
                "user_scale": scenario.user_scale,
                "arrival_rate_rps": scenario.arrival_rate_rps,
                "system_prefix_tokens": scenario.system_prefix_tokens,
                "history_tokens": scenario.history_tokens,
                "prompt_p95_tokens": scenario.prompt_p95_tokens,
                "output_p95_tokens": scenario.output_p95_tokens,
                "batch_size": scenario.batch_size,
                "qos_class": scenario.qos_class,
            }
            for scenario in profile.scenarios
        ],
    }


def _candidate(
    snapshot: Mapping[str, Any],
    profile: WorkloadProfile,
    policy_id: str,
) -> dict[str, Any]:
    configured = copy.deepcopy(dict(snapshot))
    configured["workload"] = _workload_input(profile)
    configured_policy = dict(configured.get("policy", {}))
    configured_policy["objective"] = policy_id
    configured["policy"] = configured_policy
    route = plan_snapshot(configured)
    scenarios = []
    for metric in route.metrics["scenarios"]:
        scenarios.append(
            {
                "scenario_id": metric["name"],
                "ttft_ms": metric["ttft_ms"],
                "prefill_compute_ms": metric["prefill_compute_ms"],
                "prefill_transfer_ms": metric["prefill_transfer_ms"],
                "tpot_ms": metric["tpot_ms"],
                "decode_compute_ms": metric["decode_compute_ms"],
                "decode_transfer_ms": metric["decode_transfer_ms"],
                "output_goodput_tps": metric["output_goodput_tps"],
                "expected_response_ms": metric["expected_response_ms"],
                "required_memory_bytes": metric["required_memory_bytes"],
                "confidence": metric["confidence"],
            }
        )
    return {
        "candidate_id": f"{profile.name}:{policy_id}",
        "policy_id": policy_id,
        "objective": policy_id,
        "selected": False,
        "pareto": False,
        "allocation": [
            {
                "node_id": placement.node_id,
                "start": placement.layer_range.start,
                "end": placement.layer_range.end,
            }
            for placement in route.placements
            if placement.primary
        ],
        "scenarios": scenarios,
        "worst_normalized_regret": 0.0,
        "worst_regret_scenario_id": scenarios[0]["scenario_id"],
        "worst_regret_metric": "ttft_ms",
        "deltas_from_selected": [],
    }


def _by_scenario(candidate: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    return {str(item["scenario_id"]): item for item in candidate["scenarios"]}


def _dominates(left: Mapping[str, Any], right: Mapping[str, Any]) -> bool:
    left_metrics = _by_scenario(left)
    right_metrics = _by_scenario(right)
    no_worse = True
    strict = False
    for scenario_id in sorted(left_metrics):
        l_metric = left_metrics[scenario_id]
        r_metric = right_metrics[scenario_id]
        for key in ("ttft_ms", "tpot_ms"):
            no_worse = no_worse and float(l_metric[key]) <= float(r_metric[key])
            strict = strict or float(l_metric[key]) < float(r_metric[key])
        key = "output_goodput_tps"
        no_worse = no_worse and float(l_metric[key]) >= float(r_metric[key])
        strict = strict or float(l_metric[key]) > float(r_metric[key])
    return no_worse and strict


def _score_regret(candidates: list[dict[str, Any]]) -> None:
    scenario_ids = sorted(_by_scenario(candidates[0]))
    for candidate in candidates:
        metrics = _by_scenario(candidate)
        regrets: list[tuple[float, str, str]] = []
        for scenario_id in scenario_ids:
            values = [_by_scenario(item)[scenario_id] for item in candidates]
            for key in ("ttft_ms", "tpot_ms"):
                best = min(float(value[key]) for value in values)
                actual = float(metrics[scenario_id][key])
                regrets.append(((actual - best) / max(best, 1e-12), scenario_id, key))
            key = "output_goodput_tps"
            best = max(float(value[key]) for value in values)
            actual = float(metrics[scenario_id][key])
            regrets.append(((best - actual) / max(best, 1e-12), scenario_id, key))
        worst = max(regrets, key=lambda item: (item[0], item[1], item[2]))
        candidate["worst_normalized_regret"] = worst[0]
        candidate["worst_regret_scenario_id"] = worst[1]
        candidate["worst_regret_metric"] = worst[2]


def _comparison(snapshot: Mapping[str, Any], profile: WorkloadProfile) -> dict[str, Any]:
    candidates = [_candidate(snapshot, profile, policy_id) for policy_id in POLICIES]
    _score_regret(candidates)
    frontier = [
        candidate["candidate_id"]
        for candidate in candidates
        if not any(
            _dominates(other, candidate)
            for other in candidates
            if other["candidate_id"] != candidate["candidate_id"]
        )
    ]
    selected = min(
        (candidate for candidate in candidates if candidate["candidate_id"] in frontier),
        key=lambda item: (item["worst_normalized_regret"], item["candidate_id"]),
    )
    selected_metrics = _by_scenario(selected)
    for candidate in candidates:
        candidate["selected"] = candidate is selected
        candidate["pareto"] = candidate["candidate_id"] in frontier
        candidate["deltas_from_selected"] = [
            {
                "scenario_id": scenario_id,
                "ttft_ms": float(metric["ttft_ms"])
                - float(selected_metrics[scenario_id]["ttft_ms"]),
                "tpot_ms": float(metric["tpot_ms"])
                - float(selected_metrics[scenario_id]["tpot_ms"]),
                "output_goodput_tps": float(metric["output_goodput_tps"])
                - float(selected_metrics[scenario_id]["output_goodput_tps"]),
            }
            for scenario_id, metric in sorted(_by_scenario(candidate).items())
        ]
    return {
        "profile_id": profile.name,
        "selection_mode": "minimax_normalized_regret",
        "selected_candidate_id": selected["candidate_id"],
        "winning_scenario_id": selected["worst_regret_scenario_id"],
        "winning_metric": selected["worst_regret_metric"],
        "pareto_candidate_ids": sorted(frontier),
        "candidates": candidates,
    }


def build_m15_plan_comparison(
    planner_snapshot: Mapping[str, Any],
    profiles: Sequence[WorkloadProfile],
) -> dict[str, Any]:
    if not profiles or len({profile.name for profile in profiles}) != len(profiles):
        raise ValueError("M15 requires distinct workload profiles")
    profile_documents = [_profile_document(profile) for profile in profiles]
    if any(
        document["trace_digest"] is None or document["trace_sample_count"] is None
        for document in profile_documents
    ):
        raise ValueError("M15 workload profiles require content-free trace provenance")
    document = {
        "protocol": PROTOCOL,
        "planner_snapshot_digest": _digest(planner_snapshot),
        "evidence_bundle_digest": planner_snapshot.get("evidence_bundle_digest"),
        "profiles": profile_documents,
        "comparisons": [_comparison(planner_snapshot, profile) for profile in profiles],
        "performance_budgets": [],
        "observations": [],
        "calibration_state": "predicted_unobserved",
        "deferred_to_m16": [
            "admission_latency",
            "batch_shape",
            "concurrency",
            "queueing",
        ],
        "route_ready": False,
        "claim_boundary": (
            "planner intent and phase-separated predictions; no runtime readiness, "
            "concurrent execution, queueing, batching, or release claim"
        ),
    }
    return validate_m15_plan_comparison(document)


def _exact_shape_prediction(
    planner_snapshot: Mapping[str, Any],
    profile_document: Mapping[str, Any],
    policy_id: str,
    context_tokens: int,
    output_tokens: int,
) -> dict[str, Any]:
    qos_class = str(profile_document["scenarios"][0]["qos_class"])
    profile = WorkloadProfile(
        name=str(profile_document["profile_id"]),
        scenarios=(
            WorkloadScenario(
                name="exact_observed_shape",
                prompt_tokens=context_tokens,
                output_tokens=output_tokens,
                concurrency=1,
                batch_size=1,
                qos_class=qos_class,
            ),
        ),
        mode="sensitivity_grid",
        source="privacy_reduced_physical_observation_shape",
        trace_digest=str(profile_document["trace_digest"]),
        trace_sample_count=1,
    )
    candidate = _candidate(planner_snapshot, profile, policy_id)
    metric = candidate["scenarios"][0]
    return {
        "scenario_id": "exact_observed_shape",
        "ttft_ms": metric["ttft_ms"],
        "tpot_ms": metric["tpot_ms"],
        "output_goodput_tps": metric["output_goodput_tps"],
    }


def _relative_error(observed: float, predicted: float) -> float:
    return (observed - predicted) / max(abs(predicted), 1e-12)


def attach_m15_observations(
    comparison_document: Mapping[str, Any],
    planner_snapshot: Mapping[str, Any],
    performance_budgets: Sequence[Mapping[str, Any]],
    observations: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Bind privacy-reduced sequential physical observations to exact-shape predictions."""

    document = validate_m15_plan_comparison(comparison_document)
    profile_ids = [str(item["profile_id"]) for item in document["profiles"]]
    budgets = [validate_performance_budget_v2(item) for item in performance_budgets]
    if sorted(str(item["profile_id"]) for item in budgets) != sorted(profile_ids):
        raise ValueError("M15 requires exactly one budget per profile")
    if sorted(str(item.get("profile_id")) for item in observations) != sorted(profile_ids):
        raise ValueError("M15 requires exactly one observation per profile")
    if _digest(planner_snapshot) != document["planner_snapshot_digest"]:
        raise ValueError("M15 observation planner snapshot mismatch")

    profiles = {str(item["profile_id"]): item for item in document["profiles"]}
    matrices = {str(item["profile_id"]): item for item in document["comparisons"]}
    budgets_by_profile = {str(item["profile_id"]): item for item in budgets}
    calibrated: list[dict[str, Any]] = []
    request_ids: set[str] = set()
    for raw in observations:
        profile_id = str(raw.get("profile_id"))
        request_id = raw.get("request_id")
        if not isinstance(request_id, str) or not request_id or request_id in request_ids:
            raise ValueError("M15 observation request binding is invalid")
        request_ids.add(request_id)
        context_tokens = raw.get("context_tokens")
        output_tokens = raw.get("output_tokens")
        topology_version = raw.get("topology_version")
        if not all(
            isinstance(value, int) and not isinstance(value, bool) and value > 0
            for value in (context_tokens, output_tokens, topology_version)
        ):
            raise ValueError("M15 observation shape binding is invalid")
        before = raw.get("counters_before")
        after = raw.get("counters_after")
        counter_fields = {"frames_sent", "frames_received", "applied_operation_count"}
        if (
            not isinstance(before, Mapping)
            or not isinstance(after, Mapping)
            or set(before) != counter_fields
            or set(after) != counter_fields
            or any(
                not isinstance(before[field], int)
                or isinstance(before[field], bool)
                or not isinstance(after[field], int)
                or isinstance(after[field], bool)
                or before[field] < 0
                or after[field] < before[field]
                for field in counter_fields
            )
        ):
            raise ValueError("M15 observation counter binding is invalid")
        observed = raw.get("observed")
        if not isinstance(observed, Mapping) or set(observed) != {
            "ttft_ms",
            "tpot_ms",
            "output_goodput_tps",
        }:
            raise ValueError("M15 observation metrics are invalid")
        if any(
            not isinstance(value, (int, float))
            or isinstance(value, bool)
            or not math.isfinite(float(value))
            or float(value) <= 0
            for value in observed.values()
        ):
            raise ValueError("M15 observation metrics are invalid")
        matrix = matrices[profile_id]
        selected_id = str(matrix["selected_candidate_id"])
        selected = next(
            candidate
            for candidate in matrix["candidates"]
            if candidate["candidate_id"] == selected_id
        )
        prediction = _exact_shape_prediction(
            planner_snapshot,
            profiles[profile_id],
            str(selected["policy_id"]),
            context_tokens,
            output_tokens,
        )
        signed_error = {
            key: float(observed[key]) - float(prediction[key])
            for key in ("ttft_ms", "tpot_ms", "output_goodput_tps")
        }
        relative_error = {
            "ttft": abs(_relative_error(float(observed["ttft_ms"]), float(prediction["ttft_ms"]))),
            "tpot": abs(_relative_error(float(observed["tpot_ms"]), float(prediction["tpot_ms"]))),
            "throughput": abs(
                _relative_error(
                    float(observed["output_goodput_tps"]),
                    float(prediction["output_goodput_tps"]),
                )
            ),
        }
        budget = budgets_by_profile[profile_id]
        frame_delta = max(
            after["frames_sent"] - before["frames_sent"],
            after["frames_received"] - before["frames_received"],
        )
        budget_results = {
            "ttft": "met" if float(observed["ttft_ms"]) <= float(budget["ttft_ms_maximum"]) else "failed",
            "tpot": "met" if float(observed["tpot_ms"]) <= float(budget["tpot_ms_maximum"]) else "failed",
            "throughput": "met" if float(observed["output_goodput_tps"]) >= float(budget["minimum_output_tokens_per_second"]) else "failed",
            "frames": "met" if frame_delta <= int(budget["maximum_frames_per_request"]) else "failed",
            "model_ttft": "met" if relative_error["ttft"] <= float(budget["maximum_relative_model_error"]["ttft"]) else "failed",
            "model_tpot": "met" if relative_error["tpot"] <= float(budget["maximum_relative_model_error"]["tpot"]) else "failed",
            "model_throughput": "met" if relative_error["throughput"] <= float(budget["maximum_relative_model_error"]["throughput"]) else "failed",
            "peak_memory": budget["peak_memory_budget_state"],
            "energy_thermal": budget["energy_thermal_budget_state"],
            "reconnect": budget["reconnect_budget_state"],
            "queueing": budget["queueing_budget_state"],
            "admission_latency": budget["admission_latency_budget_state"],
            "concurrency": budget["concurrency_budget_state"],
            "batch_shape": budget["batch_shape_budget_state"],
        }
        required = (
            "ttft",
            "tpot",
            "throughput",
            "frames",
            "model_ttft",
            "model_tpot",
            "model_throughput",
        )
        calibrated.append(
            {
                "profile_id": profile_id,
                "request_id": request_id,
                "selected_candidate_id": selected_id,
                "context_tokens": context_tokens,
                "output_tokens": output_tokens,
                "runtime_backends": sorted(set(raw.get("runtime_backends", []))),
                "topology_version": topology_version,
                "placement": copy.deepcopy(raw.get("placement")),
                "counters_before": dict(before),
                "counters_after": dict(after),
                "prediction": prediction,
                "observed": {key: float(value) for key, value in observed.items()},
                "signed_error": signed_error,
                "absolute_relative_error": relative_error,
                "budget_results": budget_results,
                "overall_state": "met" if all(budget_results[key] == "met" for key in required) else "failed",
            }
        )
    document["performance_budgets"] = sorted(budgets, key=lambda item: item["profile_id"])
    document["observations"] = sorted(calibrated, key=lambda item: item["profile_id"])
    document["calibration_state"] = "observed"
    return validate_m15_plan_comparison(document)


def _reject_private(value: object) -> None:
    if isinstance(value, Mapping):
        if _PRIVATE_FIELDS.intersection(value):
            raise ValueError("M15 plan comparison contains private content")
        for item in value.values():
            _reject_private(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _reject_private(item)


def _finite_tree(value: object) -> None:
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("M15 plan comparison contains a non-finite number")
    if isinstance(value, Mapping):
        for item in value.values():
            _finite_tree(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _finite_tree(item)


def _validate_observation_shape(item: object) -> None:
    expected = {
        "profile_id",
        "request_id",
        "selected_candidate_id",
        "context_tokens",
        "output_tokens",
        "runtime_backends",
        "topology_version",
        "placement",
        "counters_before",
        "counters_after",
        "prediction",
        "observed",
        "signed_error",
        "absolute_relative_error",
        "budget_results",
        "overall_state",
    }
    metric_fields = {"ttft_ms", "tpot_ms", "output_goodput_tps"}
    counter_fields = {"frames_sent", "frames_received", "applied_operation_count"}
    budget_fields = {
        "ttft",
        "tpot",
        "throughput",
        "frames",
        "model_ttft",
        "model_tpot",
        "model_throughput",
        "peak_memory",
        "energy_thermal",
        "reconnect",
        "queueing",
        "admission_latency",
        "concurrency",
        "batch_shape",
    }
    if not isinstance(item, Mapping) or set(item) != expected:
        raise ValueError("M15 observation shape is invalid")
    if (
        not isinstance(item["runtime_backends"], list)
        or not item["runtime_backends"]
        or any(not isinstance(value, str) or not value for value in item["runtime_backends"])
        or not isinstance(item["placement"], list)
        or not item["placement"]
        or any(
            not isinstance(value, Mapping)
            or set(value) != {"node_id", "start", "end"}
            or not isinstance(value["node_id"], str)
            or not isinstance(value["start"], int)
            or not isinstance(value["end"], int)
            or value["start"] < 0
            or value["end"] <= value["start"]
            for value in item["placement"]
        )
        or any(
            not isinstance(item[field], Mapping) or set(item[field]) != counter_fields
            for field in ("counters_before", "counters_after")
        )
        or not isinstance(item["prediction"], Mapping)
        or set(item["prediction"]) != metric_fields | {"scenario_id"}
        or any(
            not isinstance(item[field], Mapping) or set(item[field]) != metric_fields
            for field in ("observed", "signed_error")
        )
        or not isinstance(item["absolute_relative_error"], Mapping)
        or set(item["absolute_relative_error"]) != {"ttft", "tpot", "throughput"}
        or not isinstance(item["budget_results"], Mapping)
        or set(item["budget_results"]) != budget_fields
        or item["overall_state"] not in {"met", "failed"}
    ):
        raise ValueError("M15 observation shape is invalid")


def validate_m15_plan_comparison(document: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(document, Mapping) or set(document) != _TOP_LEVEL_FIELDS:
        raise ValueError("M15 plan comparison shape is invalid")
    if (
        document.get("protocol") != PROTOCOL
        or document.get("route_ready") is not False
        or document.get("calibration_state")
        not in {"predicted_unobserved", "partially_observed", "observed"}
        or document.get("deferred_to_m16")
        != ["admission_latency", "batch_shape", "concurrency", "queueing"]
    ):
        raise ValueError("M15 plan comparison authority is invalid")
    for field in ("planner_snapshot_digest", "evidence_bundle_digest"):
        value = document.get(field)
        if not isinstance(value, str) or not value.startswith("sha256:") or len(value) != 71:
            raise ValueError(f"M15 plan comparison {field} is invalid")
    profiles = document.get("profiles")
    comparisons = document.get("comparisons")
    if not isinstance(profiles, list) or not profiles or not isinstance(comparisons, list):
        raise ValueError("M15 plan comparison profiles are invalid")
    if [item.get("profile_id") for item in profiles] != [
        item.get("profile_id") for item in comparisons
    ]:
        raise ValueError("M15 plan comparison profile binding is invalid")
    for comparison in comparisons:
        candidates = comparison.get("candidates")
        if not isinstance(candidates, list) or {item.get("policy_id") for item in candidates} != set(POLICIES):
            raise ValueError("M15 plan comparison candidate matrix is invalid")
        selected = [item for item in candidates if item.get("selected") is True]
        if len(selected) != 1 or selected[0].get("candidate_id") != comparison.get(
            "selected_candidate_id"
        ):
            raise ValueError("M15 plan comparison selection is invalid")
        if comparison.get("selected_candidate_id") not in comparison.get(
            "pareto_candidate_ids", []
        ):
            raise ValueError("M15 selected candidate must be Pareto-optimal")
    budgets = document.get("performance_budgets")
    observations = document.get("observations")
    if not isinstance(budgets, list) or not isinstance(observations, list):
        raise ValueError("M15 calibration records are invalid")
    validated_budgets = [validate_performance_budget_v2(item) for item in budgets]
    if document["calibration_state"] == "predicted_unobserved":
        if validated_budgets or observations:
            raise ValueError("M15 unobserved comparison cannot contain calibration")
    elif document["calibration_state"] == "observed":
        profile_ids = sorted(str(item["profile_id"]) for item in profiles)
        for observation in observations:
            _validate_observation_shape(observation)
        if (
            sorted(str(item["profile_id"]) for item in validated_budgets) != profile_ids
            or sorted(str(item.get("profile_id")) for item in observations) != profile_ids
            or any(item.get("overall_state") not in {"met", "failed"} for item in observations)
        ):
            raise ValueError("M15 observed calibration binding is invalid")
    _reject_private(document)
    _finite_tree(document)
    try:
        return json.loads(json.dumps(document, allow_nan=False))
    except (TypeError, ValueError, RecursionError) as exc:
        raise ValueError("M15 plan comparison is not JSON-safe") from exc


__all__ = [
    "POLICIES",
    "PROTOCOL",
    "attach_m15_observations",
    "build_m15_plan_comparison",
    "validate_m15_plan_comparison",
]
