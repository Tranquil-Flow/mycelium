from __future__ import annotations

import itertools
import math
from dataclasses import dataclass
from typing import Callable

from .allocation import AllocatedStage, AllocationResult, allocate_layers, stage_cost
from .contracts import ModelIdentity, PlanningPolicy, SearchProvenance, WorkloadScenario
from .cycle_search import CycleResult, cycle_cost, search_cycle
from .phase_score import PhaseScore, score_phases
from .physical_graph import PhysicalGraph
from .workload import WorkloadProfile


@dataclass(frozen=True)
class PrimaryPlan:
    order: tuple[str, ...]
    frozen_primary_order: tuple[str, ...]
    allocation: AllocationResult
    scenario_scores: tuple[PhaseScore, ...]
    provenance: SearchProvenance
    candidate_node_ids: tuple[str, ...]
    unplaced_node_ids: tuple[str, ...]
    diagnostics: tuple[str, ...] = ()

    @property
    def capacity_goodput_tps(self) -> float:
        return max(score.output_goodput_tps for score in self.scenario_scores)


def _representative(profile: WorkloadProfile) -> WorkloadScenario:
    return max(
        profile.scenarios,
        key=lambda scenario: (
            scenario.total_context_tokens * scenario.concurrency,
            scenario.effective_prompt_tokens,
            scenario.output_tokens,
            scenario.name,
        ),
    )


def _topology_cost_fn(graph: PhysicalGraph) -> Callable[[str, str], float | None]:
    """Return directed one-way latency plus observed jitter only.

    Bandwidth, loss, payload size, device runtime, and memory deliberately do not
    enter topology ordering. They remain allocation/scoring concerns after order
    freezes.
    """

    def cost(src: str, dst: str) -> float | None:
        link = graph.link(src, dst)
        if link is None:
            return None
        one_way_latency = max(link.rtt_ms / 2.0, link.geolocation_floor_ms)
        return one_way_latency + link.jitter_ms

    return cost


def _exact_topology_order(
    nodes: tuple[str, ...],
    cost_fn: Callable[[str, str], float | None],
    candidate_budget: int,
) -> CycleResult:
    canonical = tuple(sorted(set(nodes)))
    if not canonical:
        raise ValueError("topology order requires at least one admitted node")
    if len(canonical) == 1:
        return CycleResult(canonical, 0.0, "exact_topology_enumeration", True, 1)

    first = canonical[0]
    total_candidates = math.factorial(len(canonical) - 1)
    best: tuple[float, tuple[str, ...]] | None = None
    explored = 0
    for tail in itertools.islice(
        itertools.permutations(canonical[1:]),
        candidate_budget,
    ):
        order = (first,) + tail
        cost = cycle_cost(order, cost_fn)
        explored += 1
        candidate = (cost, order)
        if math.isfinite(cost) and (best is None or candidate < best):
            best = candidate
    if best is None:
        raise ValueError("no feasible directed topology cycle")
    return CycleResult(
        best[1],
        best[0],
        "exact_topology_enumeration",
        explored == total_candidates,
        explored,
    )


def select_topology_order(
    graph: PhysicalGraph,
    policy: PlanningPolicy,
    admitted_node_ids: tuple[str, ...] | None = None,
) -> CycleResult:
    """Freeze one cycle containing every admitted node from network topology.

    Ordering cannot drop admitted nodes and cannot inspect device compute or
    memory. Every later allocation receives this exact frozen order.
    """

    nodes = (
        graph.candidate_node_ids
        if admitted_node_ids is None
        else tuple(admitted_node_ids)
    )
    if not nodes:
        raise ValueError("no eligible candidate nodes")
    if len(set(nodes)) != len(nodes) or any(
        node_id not in graph.nodes for node_id in nodes
    ):
        raise ValueError("admitted nodes must be distinct and eligible")
    cost_fn = _topology_cost_fn(graph)
    if len(nodes) <= policy.exact_cycle_max_nodes:
        return _exact_topology_order(
            nodes,
            cost_fn,
            policy.search_candidate_budget,
        )

    result = search_cycle(nodes, cost_fn, policy)
    if result.explored_candidates > policy.search_candidate_budget:
        raise ValueError("topology search candidate budget exceeded")
    return result


def admit_primary_nodes(
    graph: PhysicalGraph,
    model: ModelIdentity,
    profile: WorkloadProfile,
    policy: PlanningPolicy,
) -> tuple[str, ...]:
    """Select a bounded feasible primary set before topology ordering.

    Admission may inspect per-device one-layer memory feasibility. Device runtime
    and network topology cannot enter this phase: runtime balances the later layer
    allocation, while directed latency/jitter determines order. Once admitted,
    every selected node receives at least one contiguous layer.
    """

    representative = _representative(profile)
    feasible: list[str] = []
    for node_id in graph.candidate_node_ids:
        cost = stage_cost(graph.nodes[node_id], 1, model, representative, policy)
        if cost.feasible:
            feasible.append(node_id)
    if not feasible:
        raise ValueError("no node can host one model layer")
    admitted_count = min(model.num_layers, len(feasible))
    return tuple(sorted(feasible)[:admitted_count])


def _reprice_allocation(
    base: AllocationResult,
    graph: PhysicalGraph,
    model: ModelIdentity,
    scenario: WorkloadScenario,
    policy: PlanningPolicy,
) -> AllocationResult:
    stages = []
    bottleneck = 0.0
    for stage in base.stages:
        node = graph.nodes[stage.node_id]
        cost = stage_cost(node, stage.layer_range.count, model, scenario, policy)
        if not cost.feasible:
            return AllocationResult((), math.inf, False, (cost.diagnostic,))
        stages.append(
            AllocatedStage(stage.stage_index, stage.node_id, stage.layer_range, cost)
        )
        bottleneck = max(bottleneck, cost.service_work_ms)
    return AllocationResult(tuple(stages), bottleneck, True)


def plan_primary(
    graph: PhysicalGraph,
    model: ModelIdentity,
    profile: WorkloadProfile,
    policy: PlanningPolicy,
    *,
    force_primary_nodes: tuple[str, ...] | None = None,
    admitted_node_ids: tuple[str, ...] | None = None,
) -> PrimaryPlan:
    """Allocate contiguous layers after freezing a topology-only primary order."""

    candidates = graph.candidate_node_ids
    if not candidates:
        raise ValueError("no eligible candidate nodes")
    if force_primary_nodes is not None and admitted_node_ids is not None:
        raise ValueError("forced order and admitted node set are mutually exclusive")

    if force_primary_nodes is None:
        admitted = candidates if admitted_node_ids is None else tuple(admitted_node_ids)
        if (
            not admitted
            or len(set(admitted)) != len(admitted)
            or any(node_id not in graph.nodes for node_id in admitted)
        ):
            raise ValueError("admitted nodes must be distinct and eligible")
        if len(admitted) > model.num_layers:
            raise ValueError("admitted node count exceeds layer count")
        topology = select_topology_order(graph, policy, admitted)
        order = topology.order
        budget_exhausted = (
            not topology.globally_exact
            and len(admitted) <= policy.exact_cycle_max_nodes
            and topology.explored_candidates >= policy.search_candidate_budget
        )
    else:
        order = tuple(force_primary_nodes)
        if (
            not order
            or len(set(order)) != len(order)
            or any(node_id not in graph.nodes for node_id in order)
        ):
            raise ValueError("forced primary nodes must be distinct and eligible")
        if len(order) > model.num_layers:
            raise ValueError("forced primary node count exceeds layer count")
        topology_cost = cycle_cost(order, _topology_cost_fn(graph))
        if not math.isfinite(topology_cost):
            raise ValueError("forced primary order is not a directed topology cycle")
        topology = CycleResult(
            order,
            topology_cost,
            "forced_primary_order",
            False,
            1,
        )
        admitted = order
        budget_exhausted = False

    representative = _representative(profile)
    allocation = allocate_layers(
        tuple(graph.nodes[node_id] for node_id in order),
        model,
        representative,
        policy,
    )
    if not allocation.feasible:
        detail = "; ".join(allocation.diagnostics)
        raise ValueError(f"no feasible primary allocation{'; ' + detail if detail else ''}")

    scores: list[PhaseScore] = []
    for scenario in profile.scenarios:
        priced = _reprice_allocation(allocation, graph, model, scenario, policy)
        if not priced.feasible:
            detail = "; ".join(priced.diagnostics)
            raise ValueError(
                f"no feasible scenario allocation{'; ' + detail if detail else ''}"
            )
        try:
            scores.append(score_phases(priced, graph, model, scenario, policy))
        except ValueError as exc:
            raise ValueError(f"phase scoring failed: {exc}") from exc

    provenance = SearchProvenance(
        topology.mode,
        topology.globally_exact,
        topology.explored_candidates,
        len(admitted),
        policy.search_candidate_budget,
        budget_exhausted,
    )
    unplaced = tuple(node_id for node_id in candidates if node_id not in order)
    diagnostics = (
        "primary order frozen from directed latency and jitter only",
        f"admitted_node_count={len(admitted)}",
        f"topology_cycle_cost_ms={topology.cost:.12g}",
    )
    return PrimaryPlan(
        order=order,
        frozen_primary_order=order,
        allocation=allocation,
        scenario_scores=tuple(scores),
        provenance=provenance,
        candidate_node_ids=candidates,
        unplaced_node_ids=unplaced,
        diagnostics=diagnostics,
    )
