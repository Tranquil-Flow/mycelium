from __future__ import annotations

import json
from dataclasses import asdict
from typing import Any, Mapping

from .contracts import (
    LayerRange,
    LegalTrack,
    Loopback,
    ModelIdentity,
    PlanEdge,
    RoutePlanV2,
    SearchProvenance,
    StagePlacement,
)


def route_plan_to_dict(plan: RoutePlanV2) -> dict[str, Any]:
    return asdict(plan)


def dumps_route_plan(plan: RoutePlanV2, *, pretty: bool = False) -> str:
    if pretty:
        return json.dumps(route_plan_to_dict(plan), sort_keys=True, indent=2, ensure_ascii=False) + "\n"
    return json.dumps(route_plan_to_dict(plan), sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def route_plan_from_dict(data: Mapping[str, Any]) -> RoutePlanV2:
    model = ModelIdentity(**data["model"])
    placements = tuple(
        StagePlacement(
            placement_id=item["placement_id"],
            replica_group_id=item["replica_group_id"],
            node_id=item["node_id"],
            layer_range=LayerRange(**item["layer_range"]),
            primary=item["primary"],
            service_capacity_rps=item["service_capacity_rps"],
        )
        for item in data["placements"]
    )
    tracks = tuple(
        LegalTrack(
            track_id=item["track_id"],
            placement_ids=tuple(item["placement_ids"]),
            traffic_fraction=item["traffic_fraction"],
            cost_ms=item.get("cost_ms", 0.0),
        )
        for item in data["legal_tracks"]
    )
    edges = tuple(
        PlanEdge(
            src_placement_id=item["src_placement_id"],
            dst_placement_id=item["dst_placement_id"],
            capacity_rps=item["capacity_rps"],
            cost_ms=item["cost_ms"],
            kind=item.get("kind", "forward"),
        )
        for item in data["forward_edges"]
    )
    loops = tuple(
        Loopback(
            src_placement_id=item["src_placement_id"],
            dst_placement_id=item["dst_placement_id"],
            payload_bytes=item["payload_bytes"],
            cost_ms=item["cost_ms"],
        )
        for item in data["loopbacks"]
    )
    provenance_data = dict(data["provenance"])
    provenance_data.pop("elapsed_ms", None)
    provenance_data.setdefault(
        "candidate_budget",
        max(1, int(provenance_data.get("explored_candidates", 0))),
    )
    provenance_data.setdefault("budget_exhausted", False)
    provenance = SearchProvenance(**provenance_data)
    plan = RoutePlanV2(
        model=model,
        snapshot_digest=data["snapshot_digest"],
        placements=placements,
        legal_tracks=tracks,
        forward_edges=edges,
        loopbacks=loops,
        provenance=provenance,
        workload_name=data["workload_name"],
        metrics=data["metrics"],
        diagnostics=data.get("diagnostics", {}),
        handoff_state=data.get("handoff_state", "placement_intent_only"),
        protocol=data.get("protocol", "mycelium.route_plan.v2"),
    )
    from .validation import validate_route_plan

    validate_route_plan(plan)
    return plan


def loads_route_plan(text: str) -> RoutePlanV2:
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError("route plan JSON must be an object")
    return route_plan_from_dict(data)
