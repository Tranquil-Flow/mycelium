from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Mapping, Sequence

from .contracts import NUMERIC_EPSILON, StagePlacement


@dataclass(frozen=True)
class FlowEdge:
    src: str
    dst: str
    capacity: float
    cost_ms: float

    def __post_init__(self) -> None:
        if not self.src or not self.dst or self.src == self.dst:
            raise ValueError("flow edge requires distinct endpoints")
        if self.capacity < 0 or self.cost_ms < 0:
            raise ValueError("flow edge capacity and cost must be non-negative")


@dataclass(frozen=True)
class FlowTrack:
    placement_ids: tuple[str, ...]
    amount: float
    traffic_fraction: float
    cost_ms: float
    loopback: tuple[str, str]


@dataclass(frozen=True)
class FlowResult:
    admitted: float
    unmet_demand: float
    tracks: tuple[FlowTrack, ...]
    stage_utilization: Mapping[str, float]
    edge_utilization: Mapping[tuple[str, str], float]

    @property
    def complete_loop_tracks(self) -> tuple[FlowTrack, ...]:
        return self.tracks


def _edge_map(edges: Sequence[FlowEdge]) -> dict[tuple[str, str], FlowEdge]:
    result: dict[tuple[str, str], FlowEdge] = {}
    for edge in edges:
        key = (edge.src, edge.dst)
        if key in result:
            raise ValueError(f"duplicate flow edge {key}")
        result[key] = edge
    return result


def _shortest_complete_loop(
    groups: tuple[tuple[str, ...], ...],
    placement_residual: Mapping[str, float],
    forward: Mapping[tuple[str, str], FlowEdge],
    forward_residual: Mapping[tuple[str, str], float],
    loopbacks: Mapping[tuple[str, str], FlowEdge],
    loopback_residual: Mapping[tuple[str, str], float],
) -> tuple[float, tuple[str, ...]] | None:
    candidates: list[tuple[float, tuple[str, ...]]] = []
    for first in groups[0]:
        if placement_residual.get(first, 0) <= NUMERIC_EPSILON:
            continue
        states: dict[str, tuple[float, tuple[str, ...]]] = {first: (0.0, (first,))}
        for group in groups[1:]:
            next_states: dict[str, tuple[float, tuple[str, ...]]] = {}
            for dst in group:
                if placement_residual.get(dst, 0) <= NUMERIC_EPSILON:
                    continue
                best: tuple[float, tuple[str, ...]] | None = None
                for src, state in states.items():
                    edge = forward.get((src, dst))
                    if edge is None or forward_residual.get((src, dst), 0) <= NUMERIC_EPSILON:
                        continue
                    candidate = (state[0] + edge.cost_ms, state[1] + (dst,))
                    if best is None or candidate < best:
                        best = candidate
                if best is not None:
                    next_states[dst] = best
            states = next_states
            if not states:
                break
        for last, state in states.items():
            if len(groups) == 1 and last == first:
                candidates.append(state)
                continue
            loop = loopbacks.get((last, first))
            if loop is not None and loopback_residual.get((last, first), 0) > NUMERIC_EPSILON:
                candidates.append((state[0] + loop.cost_ms, state[1]))
    return min(candidates) if candidates else None


def assign_flow(
    groups: Sequence[Sequence[str]],
    placements: Mapping[str, StagePlacement],
    forward_edges: Sequence[FlowEdge],
    loopback_edges: Sequence[FlowEdge],
    *,
    demand: float,
) -> FlowResult:
    if demand < 0:
        raise ValueError("demand must be non-negative")
    ordered_groups = tuple(tuple(sorted(group)) for group in groups)
    if not ordered_groups or any(not group for group in ordered_groups):
        raise ValueError("stage groups must be non-empty")
    flattened = [placement_id for group in ordered_groups for placement_id in group]
    if len(flattened) != len(set(flattened)) or any(pid not in placements for pid in flattened):
        raise ValueError("groups must reference each placement exactly once")
    forward = _edge_map(tuple(forward_edges))
    loops = _edge_map(tuple(loopback_edges))
    placement_residual = {pid: placements[pid].service_capacity_rps for pid in flattened}
    forward_residual = {key: edge.capacity for key, edge in forward.items()}
    loop_residual = {key: edge.capacity for key, edge in loops.items()}
    placement_used = {pid: 0.0 for pid in flattened}
    edge_used = {key: 0.0 for key in tuple(forward) + tuple(loops)}
    remaining = demand
    raw_tracks: list[tuple[tuple[str, ...], float, float]] = []
    while remaining > NUMERIC_EPSILON:
        selected = _shortest_complete_loop(
            ordered_groups,
            placement_residual,
            forward,
            forward_residual,
            loops,
            loop_residual,
        )
        if selected is None:
            break
        cost, path = selected
        capacities = [placement_residual[pid] for pid in path]
        capacities.extend(forward_residual[(src, dst)] for src, dst in zip(path, path[1:]))
        if len(path) > 1:
            capacities.append(loop_residual[(path[-1], path[0])])
        amount = min([remaining] + capacities)
        if amount <= NUMERIC_EPSILON or not math.isfinite(amount):
            break
        for pid in path:
            placement_residual[pid] -= amount
            placement_used[pid] += amount
        for src, dst in zip(path, path[1:]):
            forward_residual[(src, dst)] -= amount
            edge_used[(src, dst)] += amount
        if len(path) > 1:
            loop_residual[(path[-1], path[0])] -= amount
            edge_used[(path[-1], path[0])] += amount
        raw_tracks.append((path, amount, cost))
        remaining -= amount
    admitted = math.fsum(amount for _path, amount, _cost in raw_tracks)
    remaining = max(0.0, demand - admitted)
    tracks_list: list[FlowTrack] = []
    assigned_fraction = 0.0
    for index, (path, amount, cost) in enumerate(raw_tracks):
        if not admitted:
            fraction = 0.0
        elif index == len(raw_tracks) - 1:
            fraction = max(0.0, min(1.0, 1.0 - assigned_fraction))
        else:
            fraction = max(0.0, min(1.0, amount / admitted))
            assigned_fraction = math.fsum((assigned_fraction, fraction))
        tracks_list.append(FlowTrack(path, amount, fraction, cost, (path[-1], path[0])))
    tracks = tuple(tracks_list)
    stage_utilization = {
        pid: placement_used[pid] / placements[pid].service_capacity_rps
        if placements[pid].service_capacity_rps > 0 else 0.0
        for pid in flattened
    }
    all_edges = {**forward, **loops}
    edge_utilization = {
        key: edge_used[key] / all_edges[key].capacity if all_edges[key].capacity > 0 else 0.0
        for key in edge_used
    }
    return FlowResult(admitted, remaining, tracks, stage_utilization, edge_utilization)
