from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from typing import Any, Mapping

from .planner import plan_snapshot
from .replanning import TopologyEvent, replan_for_event
from .serialization import dumps_route_plan, route_plan_to_dict
from .contracts import RoutePlanV2


BUNDLE_PROTOCOL = "mycelium.layer_replan_simulation.v1"
REPORT_PROTOCOL = "mycelium.layer_replan_simulation_report.v1"


def _load_object(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{path}: JSON root must be an object")
    return data


def _plan_digest(plan: RoutePlanV2) -> str:
    serialized = dumps_route_plan(plan)
    return "sha256:" + hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _event_from_dict(data: Mapping[str, Any]) -> TopologyEvent:
    edges = tuple(tuple(edge) for edge in data.get("edges", ()))
    if any(len(edge) != 2 for edge in edges):
        raise ValueError("each topology-event edge must contain src and dst")
    return TopologyEvent(
        event_id=str(data["event_id"]),
        snapshot_generation=int(data["snapshot_generation"]),
        kind=str(data["kind"]),
        node_ids=tuple(str(node_id) for node_id in data.get("node_ids", ())),
        edges=tuple((str(edge[0]), str(edge[1])) for edge in edges),
    )


def _extend_snapshot(snapshot: dict[str, Any], case: Mapping[str, Any]) -> None:
    add_nodes = case.get("add_nodes", [])
    add_links = case.get("add_links", [])
    if not isinstance(add_nodes, list) or not isinstance(add_links, list):
        raise ValueError("add_nodes and add_links must be arrays")
    snapshot["nodes"].extend(copy.deepcopy(add_nodes))
    snapshot["links"].extend(copy.deepcopy(add_links))


def simulate_bundle(path: Path) -> dict[str, Any]:
    bundle_path = path.resolve()
    bundle = _load_object(bundle_path)
    if bundle.get("protocol") != BUNDLE_PROTOCOL:
        raise ValueError(f"bundle protocol must be {BUNDLE_PROTOCOL}")

    base_name = bundle.get("base_snapshot")
    if not isinstance(base_name, str) or not base_name:
        raise ValueError("base_snapshot must be a non-empty path")
    base_dir = bundle_path.parent
    base_snapshot_path = (base_dir / base_name).resolve()
    try:
        base_snapshot_path.relative_to(base_dir)
    except ValueError as exc:
        raise ValueError("base_snapshot must stay inside the scenario directory") from exc
    base_snapshot = _load_object(base_snapshot_path)
    base_plan = plan_snapshot(base_snapshot)

    raw_cases = bundle.get("cases")
    if not isinstance(raw_cases, list) or not raw_cases:
        raise ValueError("cases must be a non-empty array")

    reports: list[dict[str, Any]] = []
    seen_names: set[str] = set()
    for raw_case in raw_cases:
        if not isinstance(raw_case, dict):
            raise ValueError("each simulation case must be an object")
        name = raw_case.get("name")
        if not isinstance(name, str) or not name or name in seen_names:
            raise ValueError("case names must be non-empty and unique")
        seen_names.add(name)

        raw_event = raw_case.get("event")
        if not isinstance(raw_event, dict):
            raise ValueError(f"{name}: event must be an object")
        event = _event_from_dict(raw_event)
        event_snapshot = copy.deepcopy(base_snapshot)
        _extend_snapshot(event_snapshot, raw_case)
        threshold = float(raw_case.get("min_capacity_gain_fraction", 0.05))
        outcome = replan_for_event(
            base_plan,
            event_snapshot,
            event,
            min_capacity_gain_fraction=threshold,
        )
        assessment = outcome.assessment
        candidate = outcome.candidate_plan
        reports.append(
            {
                "action": assessment.action,
                "affected_group_ids": list(assessment.affected_group_ids),
                "candidate_plan": route_plan_to_dict(candidate) if candidate is not None else None,
                "candidate_plan_digest": outcome.candidate_plan_digest,
                "capacity_gain_fraction": outcome.capacity_gain_fraction,
                "escalation_order": list(assessment.escalation_order),
                "event_id": event.event_id,
                "external_readiness_required": assessment.external_readiness_required,
                "handoff_state": outcome.handoff_state,
                "name": name,
                "reason": outcome.reason,
                "recommendation": outcome.recommendation,
                "snapshot_generation": event.snapshot_generation,
                "surviving_track_ids": list(assessment.surviving_track_ids),
                "urgency": assessment.urgency,
            }
        )

    return {
        "base_plan_digest": _plan_digest(base_plan),
        "cases": reports,
        "handoff_state": "placement_intent_only",
        "protocol": REPORT_PROTOCOL,
    }
