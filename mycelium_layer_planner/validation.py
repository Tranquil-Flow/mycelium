from __future__ import annotations

from collections.abc import Mapping

from .contracts import NUMERIC_EPSILON, RoutePlanV2


_FORBIDDEN_RUNTIME_KEYS = {
    "loaded",
    "ready",
    "leased",
    "load_proof",
    "runtime_ready",
    "weights_loaded",
}


def _contains_forbidden_claim(value) -> bool:
    if isinstance(value, Mapping):
        for key, child in value.items():
            if str(key).lower() in _FORBIDDEN_RUNTIME_KEYS:
                return True
            if _contains_forbidden_claim(child):
                return True
    elif isinstance(value, (tuple, list)):
        return any(_contains_forbidden_claim(child) for child in value)
    return False


def validate_route_plan(plan: RoutePlanV2) -> None:
    if plan.protocol != "mycelium.route_plan.v2" or plan.handoff_state != "placement_intent_only":
        raise ValueError("invalid Planner protocol or handoff state")
    if _contains_forbidden_claim(plan.diagnostics) or _contains_forbidden_claim(plan.metrics):
        raise ValueError("Planner output contains a forbidden runtime-readiness claim")
    placements = {placement.placement_id: placement for placement in plan.placements}
    if len(placements) != len(plan.placements) or not placements:
        raise ValueError("placements must be non-empty and uniquely identified")
    groups: dict[str, list] = {}
    for placement in plan.placements:
        groups.setdefault(placement.replica_group_id, []).append(placement)
    ordered_groups = sorted(
        groups.items(),
        key=lambda item: (item[1][0].layer_range.start, item[0]),
    )
    expected_start = 0
    group_order: list[str] = []
    for group_id, members in ordered_groups:
        ranges = {member.layer_range for member in members}
        if len(ranges) != 1:
            raise ValueError(f"replica group {group_id} has mismatched ranges")
        layer_range = next(iter(ranges))
        if layer_range.start != expected_start:
            raise ValueError("primary layer groups contain a gap or overlap")
        expected_start = layer_range.end
        if sum(member.primary for member in members) != 1:
            raise ValueError("every replica group requires exactly one primary placement")
        group_order.append(group_id)
    if expected_start != plan.model.num_layers:
        raise ValueError("layer groups do not cover the complete model")

    group_by_placement = {pid: placement.replica_group_id for pid, placement in placements.items()}
    forward = {(edge.src_placement_id, edge.dst_placement_id) for edge in plan.forward_edges}
    loops = {(loop.src_placement_id, loop.dst_placement_id) for loop in plan.loopbacks}
    for edge in plan.forward_edges:
        if edge.src_placement_id not in placements or edge.dst_placement_id not in placements:
            raise ValueError("forward edge references unknown placement")
        src_index = group_order.index(group_by_placement[edge.src_placement_id])
        dst_index = group_order.index(group_by_placement[edge.dst_placement_id])
        if dst_index - src_index != 1:
            raise ValueError("forward edges must connect adjacent ordered layer groups")
    if plan.legal_tracks:
        if abs(sum(track.traffic_fraction for track in plan.legal_tracks) - 1.0) > NUMERIC_EPSILON:
            raise ValueError("legal track traffic fractions must sum to one")
    for track in plan.legal_tracks:
        if len(track.placement_ids) != len(group_order):
            raise ValueError("legal track does not contain exactly one placement per layer group")
        if any(pid not in placements for pid in track.placement_ids):
            raise ValueError("legal track references unknown placement")
        track_groups = [group_by_placement[pid] for pid in track.placement_ids]
        if track_groups != group_order:
            raise ValueError("legal track violates immutable layer-group order")
        if any(pair not in forward for pair in zip(track.placement_ids, track.placement_ids[1:])):
            raise ValueError("legal track uses nonexistent directed forward edge")
        if len(track.placement_ids) > 1 and (track.placement_ids[-1], track.placement_ids[0]) not in loops:
            raise ValueError("legal decode track lacks final-to-first loopback")
    for loop in plan.loopbacks:
        if loop.src_placement_id not in placements or loop.dst_placement_id not in placements:
            raise ValueError("loopback references unknown placement")
        src_group = group_by_placement[loop.src_placement_id]
        dst_group = group_by_placement[loop.dst_placement_id]
        if src_group != group_order[-1] or dst_group != group_order[0]:
            raise ValueError("loopback must connect final group to first group")
    if plan.provenance.globally_exact and plan.provenance.mode not in {
        "exact_joint_enumeration",  # accepted for archived v2 plans
        "exact_topology_enumeration",
        "held_karp",
    }:
        raise ValueError("unknown global optimality claim")
