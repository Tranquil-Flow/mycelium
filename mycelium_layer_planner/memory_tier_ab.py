"""Executed memory-tier sensitivity evidence for one frozen planner input."""

from __future__ import annotations

import copy
from dataclasses import fields
import hashlib
import itertools
import json
import math
from typing import Any, Mapping

from .allocation import stage_cost
from .contracts import ModelIdentity, NodeCapability, PlanningPolicy, WorkloadScenario
from .planner import plan_snapshot
from .workload import empirical_interactive_chat, mlperf_qa_stress


PROTOCOL = "mycelium.a3_memory_tier_ab.v1"


def _digest(value: object) -> str:
    payload = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _policy(value: object) -> PlanningPolicy:
    if not isinstance(value, Mapping):
        raise ValueError("planning policy must be an object")
    allowed = {field.name for field in fields(PlanningPolicy)}
    unknown = set(value) - allowed
    if unknown:
        raise ValueError(f"unknown planning policy fields: {sorted(unknown)}")
    return PlanningPolicy(**value)


def _representative(value: object) -> WorkloadScenario:
    if not isinstance(value, Mapping):
        raise ValueError("workload must be an object")
    preset = value.get("preset")
    if preset == "interactive_chat_v1":
        profile = empirical_interactive_chat(
            user_scale=float(value.get("user_scale", 1.0)),
            system_prefix_tokens=int(value.get("system_prefix_tokens", 0)),
            history_tokens=int(value.get("history_tokens", 0)),
            concurrency_points=tuple(
                int(item) for item in value.get("concurrency_points", (1, 2, 4, 8, 16, 32))
            ),
        )
    elif preset == "qa_benchmark_v1":
        profile = mlperf_qa_stress(user_scale=float(value.get("user_scale", 1.0)))
    elif preset is None:
        scenarios = value.get("scenarios")
        if not isinstance(scenarios, list) or not scenarios:
            raise ValueError("custom workload requires scenarios")
        parsed = tuple(WorkloadScenario(**item) for item in scenarios)
        return max(
            parsed,
            key=lambda item: (
                item.total_context_tokens * item.concurrency,
                item.effective_prompt_tokens,
                item.output_tokens,
                item.name,
            ),
        )
    else:
        raise ValueError(f"unknown workload preset: {preset}")
    return max(
        profile.scenarios,
        key=lambda item: (
            item.total_context_tokens * item.concurrency,
            item.effective_prompt_tokens,
            item.output_tokens,
            item.name,
        ),
    )


def _node_map(snapshot: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    value = snapshot.get("nodes")
    if not isinstance(value, list) or not value:
        raise ValueError("snapshot nodes must be a non-empty list")
    result: dict[str, dict[str, Any]] = {}
    for item in value:
        if not isinstance(item, Mapping):
            raise ValueError("snapshot node must be an object")
        node_id = item.get("node_id")
        if not isinstance(node_id, str) or not node_id or node_id in result:
            raise ValueError("snapshot node_id must be unique")
        result[node_id] = copy.deepcopy(dict(item))
    return result


def _invariant_snapshot(
    snapshot: Mapping[str, Any],
    *,
    node_id: str,
) -> dict[str, Any]:
    detached = copy.deepcopy(dict(snapshot))
    nodes = _node_map(detached)
    if node_id not in nodes:
        raise ValueError("memory-tier node is absent")
    nodes[node_id].pop("fast_memory_bytes", None)
    detached["nodes"] = [nodes[key] for key in sorted(nodes)]
    return detached


def _verify_single_memory_change(
    baseline: Mapping[str, Any],
    candidate: Mapping[str, Any],
    *,
    node_id: str,
) -> tuple[int, int, str]:
    baseline_nodes = _node_map(baseline)
    candidate_nodes = _node_map(candidate)
    if set(baseline_nodes) != set(candidate_nodes) or node_id not in baseline_nodes:
        raise ValueError("memory A/B node sets differ")
    before = baseline_nodes[node_id].get("fast_memory_bytes")
    after = candidate_nodes[node_id].get("fast_memory_bytes")
    if type(before) is not int or type(after) is not int or before <= 0 or after <= 0:
        raise ValueError("fast memory values must be positive integers")
    if before == after:
        raise ValueError("memory A/B requires a changed fast-memory value")
    baseline_invariant = _invariant_snapshot(baseline, node_id=node_id)
    candidate_invariant = _invariant_snapshot(candidate, node_id=node_id)
    if baseline_invariant != candidate_invariant:
        raise ValueError("memory A/B changed an input other than one fast-memory tier")
    return before, after, _digest(baseline_invariant)


def _primary_allocation(plan: object) -> list[dict[str, Any]]:
    placements = sorted(
        (item for item in plan.placements if item.primary),
        key=lambda item: item.layer_range.start,
    )
    return [
        {
            "node_id": item.node_id,
            "start_layer": item.layer_range.start,
            "end_layer_exclusive": item.layer_range.end,
        }
        for item in placements
    ]


def _compositions(total: int, parts: int):
    for cuts in itertools.combinations(range(1, total), parts - 1):
        points = (0, *cuts, total)
        yield tuple(points[index + 1] - points[index] for index in range(parts))


def _objective(cost: object, policy: PlanningPolicy) -> float:
    if policy.objective == "prefill_ttft":
        return cost.effective_prefill_ms
    if policy.objective == "decode_tpot":
        return cost.effective_decode_ms
    return cost.service_work_ms


def _allocation_audit(
    snapshot: Mapping[str, Any],
    *,
    order: tuple[str, ...],
    memory_node_id: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], tuple[float, tuple[int, ...]]]:
    model_value = snapshot.get("model")
    if not isinstance(model_value, Mapping):
        raise ValueError("snapshot model must be an object")
    model = ModelIdentity(**model_value)
    nodes_by_id = {
        node_id: NodeCapability(**value)
        for node_id, value in _node_map(snapshot).items()
    }
    if any(node_id not in nodes_by_id for node_id in order):
        raise ValueError("planner order references an unknown node")
    workload = _representative(snapshot.get("workload", {"preset": "interactive_chat_v1"}))
    policy = _policy(snapshot.get("policy", {}))
    feasible: list[dict[str, Any]] = []
    pressures: list[dict[str, Any]] = []
    for counts in _compositions(model.num_layers, len(order)):
        cursor = 0
        bottleneck = 0.0
        rejected = False
        ranges: list[dict[str, Any]] = []
        for node_id, count in zip(order, counts, strict=True):
            node = nodes_by_id[node_id]
            cost = stage_cost(node, count, model, workload, policy)
            start = cursor
            cursor += count
            ranges.append(
                {
                    "node_id": node_id,
                    "start_layer": start,
                    "end_layer_exclusive": cursor,
                }
            )
            if not cost.feasible:
                rejected = True
                if node_id == memory_node_id:
                    pressures.append(
                        {
                            "node_id": node_id,
                            "start_layer": start,
                            "end_layer_exclusive": cursor,
                            "reason": "insufficient_fast_memory"
                            if cost.diagnostic == "spill_unavailable"
                            else cost.diagnostic,
                            "required_memory_bytes": cost.required_memory_bytes,
                        }
                    )
                break
            if node_id == memory_node_id and cost.spill_bytes > 0:
                pressures.append(
                    {
                        "node_id": node_id,
                        "start_layer": start,
                        "end_layer_exclusive": cursor,
                        "reason": "fast_memory_spill_pressure",
                        "required_memory_bytes": cost.required_memory_bytes,
                        "fast_memory_limit_bytes": node.fast_memory_bytes
                        * (1.0 - policy.memory_reserve_fraction),
                        "spill_bytes": cost.spill_bytes,
                    }
                )
            bottleneck = max(bottleneck, _objective(cost, policy))
        if not rejected:
            feasible.append(
                {
                    "counts": counts,
                    "allocation": ranges,
                    "bottleneck_objective": bottleneck,
                }
            )
    if not feasible:
        raise ValueError("memory A/B snapshot has no feasible allocation")
    selected = min(
        (float(item["bottleneck_objective"]), tuple(item["counts"]))
        for item in feasible
    )
    return feasible, pressures, selected


def compare_memory_tier_snapshots(
    baseline: Mapping[str, Any],
    candidate: Mapping[str, Any],
    *,
    memory_tier_node_id: str,
    binding_digest: str,
) -> dict[str, Any]:
    """Run the real planner twice and explain the effect of one memory-tier change."""

    if not isinstance(binding_digest, str) or not binding_digest.startswith("sha256:"):
        raise ValueError("binding digest must be sha256-prefixed")
    before, after, invariant_digest = _verify_single_memory_change(
        baseline,
        candidate,
        node_id=memory_tier_node_id,
    )
    baseline_plan = plan_snapshot(baseline)
    candidate_plan = plan_snapshot(candidate)
    if baseline_plan.model != candidate_plan.model:
        raise ValueError("memory A/B model identity differs")
    baseline_order = tuple(baseline_plan.diagnostics.get("primary_order", ()))
    candidate_order = tuple(candidate_plan.diagnostics.get("primary_order", ()))
    if baseline_order != candidate_order:
        raise ValueError("memory-only A/B changed topology order")

    baseline_feasible, _, baseline_selected = _allocation_audit(
        baseline,
        order=baseline_order,
        memory_node_id=memory_tier_node_id,
    )
    candidate_feasible, pressures, candidate_selected = _allocation_audit(
        candidate,
        order=candidate_order,
        memory_node_id=memory_tier_node_id,
    )
    baseline_allocation = _primary_allocation(baseline_plan)
    candidate_allocation = _primary_allocation(candidate_plan)
    if tuple(item["end_layer_exclusive"] - item["start_layer"] for item in baseline_allocation) != baseline_selected[1]:
        raise ValueError("baseline allocation audit disagrees with planner")
    if tuple(item["end_layer_exclusive"] - item["start_layer"] for item in candidate_allocation) != candidate_selected[1]:
        raise ValueError("candidate allocation audit disagrees with planner")

    changed = baseline_allocation != candidate_allocation
    stability_proof = None
    if not changed:
        ordered = sorted(
            (float(item["bottleneck_objective"]), tuple(item["counts"]))
            for item in candidate_feasible
        )
        selected_score = ordered[0][0]
        equal_minima = sum(math.isclose(score, selected_score) for score, _ in ordered)
        proof_without_digest = {
            "kind": "unique_minimum"
            if equal_minima == 1
            else "all_alternatives_dominated",
            "evaluated_alternative_count": max(1, len(ordered) - 1),
            "selected_objective": selected_score,
            "equal_minimum_count": equal_minima,
        }
        stability_proof = {
            **proof_without_digest,
            "proof_digest": _digest(proof_without_digest),
        }

    return {
        "protocol": PROTOCOL,
        "binding_digest": binding_digest,
        "baseline_snapshot_digest": baseline_plan.snapshot_digest,
        "candidate_snapshot_digest": candidate_plan.snapshot_digest,
        "invariant_inputs_digest": invariant_digest,
        "memory_tier_node_id": memory_tier_node_id,
        "changed_fields": [f"nodes.{memory_tier_node_id}.fast_memory_bytes"],
        "baseline_fast_memory_bytes": before,
        "candidate_fast_memory_bytes": after,
        "baseline_allocation": baseline_allocation,
        "candidate_allocation": candidate_allocation,
        "baseline_objective": baseline_selected[0],
        "candidate_objective": candidate_selected[0],
        "explored_allocation_count": len(baseline_feasible) + len(candidate_feasible),
        "rejected_intervals": pressures,
        "result": "allocation_changed" if changed else "allocation_stable",
        "stability_proof": stability_proof,
    }


def _model_report(
    operation: Mapping[str, Any],
    *,
    model_id: str,
    revision: str,
) -> tuple[Mapping[str, Any], Mapping[str, Any]]:
    catalog = operation.get("catalog")
    entries = catalog.get("entries") if isinstance(catalog, Mapping) else None
    reports = operation.get("feasibility_reports")
    matching_entries = [
        item
        for item in entries or ()
        if isinstance(item, Mapping)
        and item.get("model_id") == model_id
        and item.get("revision") == revision
    ]
    matching_reports = [
        item
        for item in reports or ()
        if isinstance(item, Mapping)
        and item.get("model_id") == model_id
        and item.get("revision") == revision
    ]
    if len(matching_entries) != 1 or len(matching_reports) != 1:
        raise ValueError("product memory A/B model identity is unavailable")
    return matching_entries[0], matching_reports[0]


def _product_memory_inputs(
    baseline: Mapping[str, Any],
    candidate: Mapping[str, Any],
    *,
    node_id: str,
) -> tuple[int, int, str, str, str]:
    baseline_copy = copy.deepcopy(dict(baseline))
    candidate_copy = copy.deepcopy(dict(candidate))
    baseline_placement = baseline_copy.get("placement")
    candidate_placement = candidate_copy.get("placement")
    if not isinstance(baseline_placement, Mapping) or not isinstance(
        candidate_placement, Mapping
    ):
        raise ValueError("product memory A/B placement is unavailable")
    baseline_nodes = _node_map(baseline_placement)
    candidate_nodes = _node_map(candidate_placement)
    if set(baseline_nodes) != set(candidate_nodes) or node_id not in baseline_nodes:
        raise ValueError("product memory A/B node sets differ")
    before = baseline_nodes[node_id].get("fast_allocatable_bytes")
    after = candidate_nodes[node_id].get("fast_allocatable_bytes")
    if type(before) is not int or type(after) is not int or before <= 0 or after <= 0:
        raise ValueError("fast allocatable memory must be positive integers")
    if before == after:
        raise ValueError("product memory A/B requires a changed fast-memory value")
    baseline_nodes[node_id].pop("fast_allocatable_bytes", None)
    candidate_nodes[node_id].pop("fast_allocatable_bytes", None)
    baseline_copy["placement"] = {
        **dict(baseline_placement),
        "nodes": [baseline_nodes[key] for key in sorted(baseline_nodes)],
    }
    candidate_copy["placement"] = {
        **dict(candidate_placement),
        "nodes": [candidate_nodes[key] for key in sorted(candidate_nodes)],
    }
    if baseline_copy != candidate_copy:
        raise ValueError(
            "product memory A/B changed an input other than one fast-memory tier"
        )
    return (
        before,
        after,
        _digest(baseline_copy),
        _digest(baseline),
        _digest(candidate),
    )


def _report_allocation(report: Mapping[str, Any]) -> list[dict[str, Any]]:
    stages = report.get("stages")
    if report.get("state") != "feasible" or not isinstance(stages, list) or not stages:
        raise ValueError("product memory A/B requires two feasible exact-DP reports")
    return [
        {
            "node_id": stage["node_id"],
            "start_layer": stage["start_layer"],
            "end_layer_exclusive": stage["end_layer_exclusive"],
            "required_memory_bytes": stage["required_memory_bytes"],
            "spill_bytes": stage["spill_bytes"],
            "headroom_bytes": stage["headroom_bytes"],
        }
        for stage in stages
        if isinstance(stage, Mapping)
    ]


def compare_product_memory_tier_operations(
    baseline_observations: Mapping[str, Any],
    candidate_observations: Mapping[str, Any],
    baseline_operation: Mapping[str, Any],
    candidate_operation: Mapping[str, Any],
    *,
    model_id: str,
    revision: str,
    memory_tier_node_id: str,
    binding_digest: str,
) -> dict[str, Any]:
    """Seal a memory-only A/B executed by the product exact-weight DP."""

    if not isinstance(binding_digest, str) or not binding_digest.startswith("sha256:"):
        raise ValueError("binding digest must be sha256-prefixed")
    before, after, invariant, baseline_input, candidate_input = _product_memory_inputs(
        baseline_observations,
        candidate_observations,
        node_id=memory_tier_node_id,
    )
    baseline_entry, baseline_report = _model_report(
        baseline_operation,
        model_id=model_id,
        revision=revision,
    )
    candidate_entry, candidate_report = _model_report(
        candidate_operation,
        model_id=model_id,
        revision=revision,
    )
    invariant_report_fields = (
        "artifact_digest",
        "source_quantization",
        "serving_quantization",
        "serving_dtype",
        "representation_digest",
        "workload",
        "planner",
    )
    if (
        baseline_entry.get("artifact_digest") != candidate_entry.get("artifact_digest")
        or any(
            baseline_report.get(field) != candidate_report.get(field)
            for field in invariant_report_fields
        )
        or baseline_report.get("planner")
        != "capability_aware_contiguous_exact_weight_dp"
    ):
        raise ValueError("product memory A/B operation identity differs")
    layers = baseline_entry.get("num_layers")
    baseline_allocation = _report_allocation(baseline_report)
    candidate_allocation = _report_allocation(candidate_report)
    if type(layers) is not int or layers < 2:
        raise ValueError("product memory A/B layer count is invalid")
    if len(baseline_allocation) != len(candidate_allocation):
        raise ValueError("product memory A/B topology size differs")
    baseline_memory_stage = next(
        (
            stage
            for stage in baseline_allocation
            if stage["node_id"] == memory_tier_node_id
        ),
        None,
    )
    candidate_memory_stage = next(
        (
            stage
            for stage in candidate_allocation
            if stage["node_id"] == memory_tier_node_id
        ),
        None,
    )
    if baseline_memory_stage is None or candidate_memory_stage is None:
        raise ValueError("product memory A/B selected stage is unavailable")
    counterfactual_spill = max(
        0,
        int(baseline_memory_stage["required_memory_bytes"]) - after,
    )
    changed = baseline_allocation != candidate_allocation
    if not changed:
        raise ValueError("product memory A/B stable result needs alternative-cost proof")
    if counterfactual_spill <= 0:
        raise ValueError("product memory A/B allocation change lacks memory pressure")
    rejected = [
        copy.deepcopy(dict(item))
        for report in (baseline_report, candidate_report)
        for item in report.get("rejected_candidates", ())
        if isinstance(item, Mapping)
    ]
    result = {
        "protocol": "mycelium.a3_product_memory_tier_ab.v1",
        "binding_digest": binding_digest,
        "model_id": model_id,
        "revision": revision,
        "artifact_digest": baseline_entry["artifact_digest"],
        "representation_digest": baseline_report["representation_digest"],
        "planner": baseline_report["planner"],
        "memory_tier_node_id": memory_tier_node_id,
        "changed_fields": [
            f"placement.nodes.{memory_tier_node_id}.fast_allocatable_bytes"
        ],
        "baseline_fast_memory_bytes": before,
        "candidate_fast_memory_bytes": after,
        "invariant_inputs_digest": invariant,
        "baseline_input_digest": baseline_input,
        "candidate_input_digest": candidate_input,
        "baseline_operation_digest": baseline_operation.get("operation_digest"),
        "candidate_operation_digest": candidate_operation.get("operation_digest"),
        "baseline_feasibility_digest": baseline_report.get("feasibility_digest"),
        "candidate_feasibility_digest": candidate_report.get("feasibility_digest"),
        "baseline_allocation": baseline_allocation,
        "candidate_allocation": candidate_allocation,
        "baseline_bottleneck_service_work_ms": baseline_report.get(
            "bottleneck_service_work_ms"
        ),
        "candidate_bottleneck_service_work_ms": candidate_report.get(
            "bottleneck_service_work_ms"
        ),
        "counterfactual_baseline_allocation_pressure": {
            "node_id": memory_tier_node_id,
            "reason": "fast_memory_spill_pressure",
            "required_memory_bytes": baseline_memory_stage["required_memory_bytes"],
            "candidate_fast_memory_bytes": after,
            "spill_bytes": counterfactual_spill,
        },
        "candidate_selected_stage_spill_bytes": candidate_memory_stage["spill_bytes"],
        "explored_allocation_count": 2
        * math.comb(layers - 1, len(baseline_allocation) - 1),
        "rejected_intervals": rejected,
        "result": "allocation_changed",
        "stability_proof": None,
    }
    result["evidence_digest"] = _digest(result)
    return result


__all__ = [
    "PROTOCOL",
    "compare_memory_tier_snapshots",
    "compare_product_memory_tier_operations",
]
