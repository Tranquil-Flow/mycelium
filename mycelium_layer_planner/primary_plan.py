from __future__ import annotations

import itertools
import math
from collections import Counter
from dataclasses import dataclass
from typing import Iterable, Sequence

from .allocation import AllocatedStage, AllocationResult, allocate_layers, stage_cost
from .contracts import ModelIdentity, PlanningPolicy, SearchProvenance, WorkloadScenario
from .cycle_search import cycle_cost, search_cycle
from .network_cost import transfer_time_ms
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


def _fleet_mode(count: int, policy: PlanningPolicy) -> str:
    if count <= policy.exact_cycle_max_nodes:
        return "exact_joint_enumeration"
    if count <= policy.held_karp_max_nodes:
        return "held_karp"
    if count <= policy.local_search_max_nodes:
        return "multi_start_insertion"
    if count <= policy.clustered_max_nodes:
        return "clustered_refinement"
    return "hierarchical_refinement"


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


def _network_cost_fn(
    graph: PhysicalGraph,
    profile: WorkloadProfile,
    model: ModelIdentity,
    policy: PlanningPolicy,
):
    def cost(src: str, dst: str) -> float | None:
        link = graph.link(src, dst)
        if link is None:
            return None
        scenario_costs = []
        for scenario in profile.scenarios:
            prefill = transfer_time_ms(link, model.activation_bytes(scenario.effective_prompt_tokens), policy).total_ms
            decode = transfer_time_ms(link, model.activation_bytes(1), policy).total_ms
            scenario_costs.append(prefill + scenario.output_tokens * decode)
        return max(scenario_costs)

    return cost


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
        stages.append(AllocatedStage(stage.stage_index, stage.node_id, stage.layer_range, cost))
        bottleneck = max(bottleneck, cost.service_work_ms)
    return AllocationResult(tuple(stages), bottleneck, True)


def _rank(scores: Sequence[PhaseScore]) -> tuple[float, float, float, float]:
    capacity = max(score.output_goodput_tps for score in scores)
    floor = min(score.output_goodput_tps for score in scores)
    single = max(score.single_request_tps for score in scores if score.output_goodput_tps >= 0)
    response = max(score.expected_response_ms for score in scores)
    return capacity, floor, single, -response


def _all_exact_orders(nodes: tuple[str, ...], max_stages: int) -> Iterable[tuple[str, ...]]:
    for size in range(1, min(len(nodes), max_stages) + 1):
        yield from itertools.permutations(nodes, size)


def _heuristic_active_orders(
    graph: PhysicalGraph,
    model: ModelIdentity,
    profile: WorkloadProfile,
    policy: PlanningPolicy,
) -> tuple[tuple[str, ...], ...]:
    nodes = graph.candidate_node_ids
    network_cost = _network_cost_fn(graph, profile, model, policy)
    seed = search_cycle(nodes, network_cost, policy)
    max_stages = min(model.num_layers, len(nodes))
    if len(nodes) <= max_stages:
        seeds = [seed.order]
    else:
        seeds = [
            tuple(seed.order[(start + offset) % len(seed.order)] for offset in range(max_stages))
            for start in range(len(seed.order))
        ]
        representative = _representative(profile)
        compute_rank = sorted(
            nodes,
            key=lambda node_id: (
                stage_cost(graph.nodes[node_id], 1, model, representative, policy).service_work_ms,
                node_id,
            ),
        )[:max_stages]
        selected = set(compute_rank)
        seeds.append(tuple(node_id for node_id in seed.order if node_id in selected))
    unique = sorted(set(seeds))
    # Every cycle opening is legal because final->first carries a different payload.
    opened: set[tuple[str, ...]] = set()
    for order in unique:
        for start in range(len(order)):
            opened.add(order[start:] + order[:start])
    return tuple(sorted(opened))


def plan_primary(
    graph: PhysicalGraph,
    model: ModelIdentity,
    profile: WorkloadProfile,
    policy: PlanningPolicy,
    *,
    force_primary_nodes: tuple[str, ...] | None = None,
) -> PrimaryPlan:
    candidates = graph.candidate_node_ids
    if not candidates:
        raise ValueError("no eligible candidate nodes")
    if force_primary_nodes is not None:
        if not force_primary_nodes or any(node_id not in graph.nodes for node_id in force_primary_nodes):
            raise ValueError("forced primary nodes must be eligible")
        orders: Iterable[tuple[str, ...]] = (tuple(force_primary_nodes),)
        globally_exact = False
        mode = "forced_primary_order"
    elif len(candidates) <= policy.exact_cycle_max_nodes:
        orders = _all_exact_orders(candidates, model.num_layers)
        globally_exact = True
        mode = "exact_joint_enumeration"
    else:
        orders = _heuristic_active_orders(graph, model, profile, policy)
        globally_exact = False
        mode = _fleet_mode(len(candidates), policy)

    representative = _representative(profile)
    best_order: tuple[str, ...] | None = None
    best_allocation: AllocationResult | None = None
    best_scores: tuple[PhaseScore, ...] | None = None
    best_rank: tuple[float, float, float, float] | None = None
    explored = 0
    rejected: Counter[str] = Counter()
    order_iter = iter(orders)
    while explored < policy.search_candidate_budget:
        try:
            order = next(order_iter)
        except StopIteration:
            break
        explored += 1
        if len(order) > 1 and not math.isfinite(cycle_cost(order, _network_cost_fn(graph, profile, model, policy))):
            rejected["disconnected directed cycle"] += 1
            continue
        allocation = allocate_layers(tuple(graph.nodes[node_id] for node_id in order), model, representative, policy)
        if not allocation.feasible:
            reason = allocation.diagnostics[0] if allocation.diagnostics else "infeasible allocation"
            rejected[f"allocation: {reason}"] += 1
            continue
        scores: list[PhaseScore] = []
        feasible = True
        for scenario in profile.scenarios:
            priced = _reprice_allocation(allocation, graph, model, scenario, policy)
            if not priced.feasible:
                reason = priced.diagnostics[0] if priced.diagnostics else "infeasible scenario allocation"
                rejected[f"scenario allocation: {reason}"] += 1
                feasible = False
                break
            try:
                scores.append(score_phases(priced, graph, model, scenario, policy))
            except ValueError as exc:
                rejected[f"phase scoring: {exc}"] += 1
                feasible = False
                break
        if not feasible:
            continue
        rank = _rank(scores)
        if best_rank is None or rank > best_rank or (rank == best_rank and order < best_order):
            best_rank = rank
            best_order = tuple(order)
            best_allocation = allocation
            best_scores = tuple(scores)
    try:
        next(order_iter)
        budget_exhausted = True
    except StopIteration:
        budget_exhausted = False
    diagnostics = tuple(
        f"{reason}: {count} candidate order(s) rejected"
        for reason, count in sorted(rejected.items())
    )
    if best_order is None or best_allocation is None or best_scores is None:
        detail = "; ".join(diagnostics)
        raise ValueError(f"no feasible primary plan{'; ' + detail if detail else ''}")
    provenance = SearchProvenance(
        mode,
        globally_exact and not budget_exhausted,
        explored,
        len(candidates),
        policy.search_candidate_budget,
        budget_exhausted,
    )
    unplaced = tuple(node_id for node_id in candidates if node_id not in best_order)
    return PrimaryPlan(
        order=best_order,
        frozen_primary_order=best_order,
        allocation=best_allocation,
        scenario_scores=best_scores,
        provenance=provenance,
        candidate_node_ids=candidates,
        unplaced_node_ids=unplaced,
        diagnostics=diagnostics,
    )
