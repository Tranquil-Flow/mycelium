from __future__ import annotations

import itertools
import math
from dataclasses import dataclass
from typing import Callable, Iterable, Optional, Sequence

from .contracts import PlanningPolicy


EdgeCostFunction = Callable[[str, str], Optional[float]]


@dataclass(frozen=True)
class CycleResult:
    order: tuple[str, ...]
    cost: float
    mode: str
    globally_exact: bool
    explored_candidates: int


@dataclass(frozen=True)
class OpenCycle:
    order: tuple[str, ...]
    loopback: tuple[str, str]


def _edge(cost_fn: EdgeCostFunction, src: str, dst: str) -> float:
    value = cost_fn(src, dst)
    if not isinstance(value, (int, float)):
        # Accept Mapping.get directly as a convenience; dict.get(src, dst)
        # otherwise returns dst as its default value.
        try:
            value = cost_fn((src, dst))  # type: ignore[call-arg]
        except TypeError:
            value = None
    if value is None or not math.isfinite(float(value)):
        return math.inf
    return float(value)


def cycle_cost(order: Sequence[str], cost_fn: EdgeCostFunction) -> float:
    order = tuple(order)
    if not order:
        return math.inf
    if len(order) == 1:
        return 0.0
    total = 0.0
    for src, dst in zip(order, order[1:] + order[:1]):
        value = _edge(cost_fn, src, dst)
        if not math.isfinite(value):
            return math.inf
        total += value
    return total


def _canonical_rotation(order: Sequence[str]) -> tuple[str, ...]:
    order = tuple(order)
    if not order:
        return ()
    return min(order[i:] + order[:i] for i in range(len(order)))


def exact_directed_cycle(nodes: Iterable[str], cost_fn: EdgeCostFunction) -> CycleResult:
    canonical_nodes = tuple(sorted(set(nodes)))
    if not canonical_nodes:
        raise ValueError("cycle requires at least one node")
    if len(canonical_nodes) == 1:
        return CycleResult(canonical_nodes, 0.0, "exact_enumeration", True, 1)
    first = canonical_nodes[0]
    best: tuple[float, tuple[str, ...]] | None = None
    explored = 0
    for tail in itertools.permutations(canonical_nodes[1:]):
        order = (first,) + tail
        cost = cycle_cost(order, cost_fn)
        explored += 1
        candidate = (cost, order)
        if math.isfinite(cost) and (best is None or candidate < best):
            best = candidate
    if best is None:
        raise ValueError("no feasible directed cycle")
    return CycleResult(best[1], best[0], "exact_enumeration", True, explored)


def held_karp_cycle(nodes: Iterable[str], cost_fn: EdgeCostFunction) -> CycleResult:
    canonical = tuple(sorted(set(nodes)))
    if not canonical:
        raise ValueError("cycle requires at least one node")
    if len(canonical) == 1:
        return CycleResult(canonical, 0.0, "held_karp", False, 1)
    start = canonical[0]
    others = canonical[1:]
    dp: dict[tuple[int, int], tuple[float, tuple[str, ...]]] = {}
    explored = 0
    for index, node in enumerate(others):
        cost = _edge(cost_fn, start, node)
        if math.isfinite(cost):
            dp[(1 << index, index)] = (cost, (start, node))
    for size in range(2, len(others) + 1):
        for mask in range(1, 1 << len(others)):
            if bin(mask).count("1") != size:
                continue
            for last in range(len(others)):
                if not mask & (1 << last):
                    continue
                previous_mask = mask ^ (1 << last)
                best: tuple[float, tuple[str, ...]] | None = None
                for previous in range(len(others)):
                    state = dp.get((previous_mask, previous))
                    if state is None:
                        continue
                    edge = _edge(cost_fn, others[previous], others[last])
                    candidate = (state[0] + edge, state[1] + (others[last],))
                    explored += 1
                    if math.isfinite(candidate[0]) and (best is None or candidate < best):
                        best = candidate
                if best is not None:
                    dp[(mask, last)] = best
    full = (1 << len(others)) - 1
    best_cycle: tuple[float, tuple[str, ...]] | None = None
    for last in range(len(others)):
        state = dp.get((full, last))
        if state is None:
            continue
        candidate = (state[0] + _edge(cost_fn, others[last], start), state[1])
        if math.isfinite(candidate[0]) and (best_cycle is None or candidate < best_cycle):
            best_cycle = candidate
    if best_cycle is None:
        raise ValueError("no feasible directed cycle")
    return CycleResult(best_cycle[1], best_cycle[0], "held_karp", False, explored)


def _nearest_neighbor(nodes: tuple[str, ...], cost_fn: EdgeCostFunction, start: str) -> tuple[str, ...] | None:
    remaining = set(nodes)
    remaining.remove(start)
    order = [start]
    while remaining:
        current = order[-1]
        candidates = sorted((_edge(cost_fn, current, node), node) for node in remaining)
        if not candidates or not math.isfinite(candidates[0][0]):
            return None
        node = candidates[0][1]
        order.append(node)
        remaining.remove(node)
    if len(order) > 1 and not math.isfinite(_edge(cost_fn, order[-1], order[0])):
        return None
    return tuple(order)


def _local_swap(order: tuple[str, ...], cost_fn: EdgeCostFunction) -> tuple[str, ...]:
    """Directed swap hill-climb using O(1) edge deltas per proposal."""
    best = _canonical_rotation(order)
    best_cost = cycle_cost(best, cost_fn)
    max_passes = 4 if len(best) <= 32 else 2
    for _ in range(max_passes):
        improved = False
        for i in range(1, len(best) - 1):
            for j in range(i + 1, len(best)):
                impacted = {(i - 1) % len(best), i, (j - 1) % len(best), j}
                old_edges = sum(
                    _edge(cost_fn, best[k], best[(k + 1) % len(best)])
                    for k in impacted
                )
                candidate_list = list(best)
                candidate_list[i], candidate_list[j] = candidate_list[j], candidate_list[i]
                candidate = tuple(candidate_list)
                new_edges = sum(
                    _edge(cost_fn, candidate[k], candidate[(k + 1) % len(candidate)])
                    for k in impacted
                )
                cost = best_cost - old_edges + new_edges
                if (cost, candidate) < (best_cost, best):
                    best, best_cost, improved = candidate, cost, True
        if not improved:
            break
    return best


def heuristic_cycle(nodes: Iterable[str], cost_fn: EdgeCostFunction, mode: str) -> CycleResult:
    canonical = tuple(sorted(set(nodes)))
    if not canonical:
        raise ValueError("cycle requires nodes")
    candidates: list[tuple[float, tuple[str, ...]]] = []
    explored = 0
    starts = canonical if len(canonical) <= 32 else canonical[: min(16, len(canonical))]
    for start in starts:
        order = _nearest_neighbor(canonical, cost_fn, start)
        explored += 1
        if order is None:
            continue
        order = _local_swap(order, cost_fn)
        candidate = (cycle_cost(order, cost_fn), _canonical_rotation(order))
        if math.isfinite(candidate[0]):
            candidates.append(candidate)
    if not candidates:
        raise ValueError("no feasible directed cycle")
    cost, order = min(candidates)
    return CycleResult(order, cost, mode, False, explored)


def search_cycle(nodes: Iterable[str], cost_fn: EdgeCostFunction, policy: PlanningPolicy) -> CycleResult:
    canonical = tuple(sorted(set(nodes)))
    count = len(canonical)
    if count <= policy.exact_cycle_max_nodes:
        return exact_directed_cycle(canonical, cost_fn)
    if count <= policy.held_karp_max_nodes:
        return held_karp_cycle(canonical, cost_fn)
    if count <= policy.local_search_max_nodes:
        mode = "multi_start_insertion"
    elif count <= policy.clustered_max_nodes:
        mode = "clustered_refinement"
    else:
        mode = "hierarchical_refinement"
    return heuristic_cycle(canonical, cost_fn, mode)


def open_cycle(order: Sequence[str], start_index: int = 0) -> OpenCycle:
    order = tuple(order)
    if not order or not 0 <= start_index < len(order):
        raise ValueError("invalid cycle opening")
    rotated = order[start_index:] + order[:start_index]
    return OpenCycle(rotated, (rotated[-1], rotated[0]))
