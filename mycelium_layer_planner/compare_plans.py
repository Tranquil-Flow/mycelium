from __future__ import annotations

from typing import Any, Mapping

from .planner import plan_snapshot

_COMPARE_PROTOCOL = "mycelium.planner_capacity_comparison.v1"
_CLAIM_BOUNDARY = (
    "placement-intent capacity comparison from two deterministic planner snapshots; "
    "no runtime readiness, route promotion, physical execution, or release claim"
)

_METRIC_KEYS = (
    "replicated_request_capacity_rps",
    "replicated_output_capacity_tps",
    "unmet_capacity_sweep_demand_rps",
)


def _signed_delta(baseline: float, candidate: float) -> float:
    return float(candidate) - float(baseline)


def compare_snapshots(
    baseline: Mapping[str, Any],
    candidate: Mapping[str, Any],
) -> dict[str, Any]:
    base_plan = plan_snapshot(baseline)
    cand_plan = plan_snapshot(candidate)

    b_model = base_plan.model
    c_model = cand_plan.model
    if (
        b_model.model_id != c_model.model_id
        or b_model.revision != c_model.revision
        or b_model.weight_digest != c_model.weight_digest
        or b_model.num_layers != c_model.num_layers
    ):
        raise ValueError("model identity mismatch: baseline and candidate must use the same model")

    delta: dict[str, dict[str, Any]] = {}
    for key in _METRIC_KEYS:
        b_val = float(base_plan.metrics.get(key, 0.0))
        c_val = float(cand_plan.metrics.get(key, 0.0))
        delta[key] = {
            "baseline": b_val,
            "candidate": c_val,
            "absolute": _signed_delta(b_val, c_val),
            "relative": _signed_delta(b_val, c_val) / b_val if b_val > 0 else None,
        }

    base_nodes = base_plan.diagnostics.get("admitted_node_ids", [])
    cand_nodes = cand_plan.diagnostics.get("admitted_node_ids", [])
    added = sorted(set(cand_nodes) - set(base_nodes))
    removed = sorted(set(base_nodes) - set(cand_nodes))

    return {
        "protocol": _COMPARE_PROTOCOL,
        "handoff_state": "placement_intent_only",
        "route_ready": False,
        "release_ready": False,
        "claim_boundary": _CLAIM_BOUNDARY,
        "baseline": {
            "snapshot_digest": base_plan.snapshot_digest,
            "primary_order": list(base_plan.diagnostics.get("primary_order", [])),
            "admitted_node_ids": list(base_nodes),
        },
        "candidate": {
            "snapshot_digest": cand_plan.snapshot_digest,
            "primary_order": list(cand_plan.diagnostics.get("primary_order", [])),
            "admitted_node_ids": list(cand_nodes),
        },
        "capacity_delta": delta,
        "node_membership": {
            "added": added,
            "removed": removed,
            "unchanged": sorted(set(base_nodes) & set(cand_nodes)),
        },
        "model": {
            "model_id": b_model.model_id,
            "revision": b_model.revision,
        },
    }
