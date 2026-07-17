from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Sequence

from .allocation import stage_cost
from .contracts import NUMERIC_EPSILON, LayerRange, ModelIdentity, PlanningPolicy, StagePlacement, WorkloadScenario
from .flow import FlowEdge, FlowResult, assign_flow
from .network_cost import transfer_time_ms
from .physical_graph import PhysicalGraph
from .primary_plan import PrimaryPlan


@dataclass(frozen=True)
class ReplicationResult:
    frozen_primary_order: tuple[str, ...]
    groups: tuple[tuple[str, ...], ...]
    placements: tuple[StagePlacement, ...]
    forward_edges: tuple[FlowEdge, ...]
    loopback_edges: tuple[FlowEdge, ...]
    flow: FlowResult
    accepted_replica_nodes: tuple[str, ...]
    iterations: int


def _request_edge(
    graph: PhysicalGraph,
    src_node: str,
    dst_node: str,
    model: ModelIdentity,
    workload: WorkloadScenario,
    policy: PlanningPolicy,
    *,
    loopback: bool = False,
) -> tuple[float, float] | None:
    if src_node == dst_node:
        return 1e12, 0.0
    link = graph.link(src_node, dst_node)
    if link is None:
        return None
    if loopback:
        total_ms = workload.output_tokens * transfer_time_ms(link, 16, policy).total_ms
    else:
        prefill = transfer_time_ms(link, model.activation_bytes(workload.effective_prompt_tokens), policy).total_ms
        decode = transfer_time_ms(link, model.activation_bytes(1), policy).total_ms
        total_ms = prefill + workload.output_tokens * decode
    capacity = 1000.0 / total_ms if total_ms > 0 else 1e12
    return capacity, total_ms


def _build_edges(
    groups: Sequence[Sequence[str]],
    placements: Mapping[str, StagePlacement],
    graph: PhysicalGraph,
    model: ModelIdentity,
    workload: WorkloadScenario,
    policy: PlanningPolicy,
) -> tuple[tuple[FlowEdge, ...], tuple[FlowEdge, ...]]:
    forward: list[FlowEdge] = []
    for left_group, right_group in zip(groups, groups[1:]):
        for src in left_group:
            for dst in right_group:
                estimate = _request_edge(
                    graph,
                    placements[src].node_id,
                    placements[dst].node_id,
                    model,
                    workload,
                    policy,
                )
                if estimate is not None:
                    forward.append(FlowEdge(src, dst, estimate[0], estimate[1]))
    loops: list[FlowEdge] = []
    if len(groups) > 1:
        for src in groups[-1]:
            for dst in groups[0]:
                estimate = _request_edge(
                    graph,
                    placements[src].node_id,
                    placements[dst].node_id,
                    model,
                    workload,
                    policy,
                    loopback=True,
                )
                if estimate is not None:
                    loops.append(FlowEdge(src, dst, estimate[0], estimate[1]))
    return tuple(sorted(forward, key=lambda edge: (edge.src, edge.dst))), tuple(sorted(loops, key=lambda edge: (edge.src, edge.dst)))


def _solve(
    groups: tuple[tuple[str, ...], ...],
    placements: Mapping[str, StagePlacement],
    graph: PhysicalGraph,
    model: ModelIdentity,
    workload: WorkloadScenario,
    policy: PlanningPolicy,
) -> tuple[tuple[FlowEdge, ...], tuple[FlowEdge, ...], FlowResult]:
    forward, loops = _build_edges(groups, placements, graph, model, workload, policy)
    demand = sum(placements[pid].service_capacity_rps for pid in groups[0])
    flow = assign_flow(groups, placements, forward, loops, demand=demand)
    return forward, loops, flow


def _primary_state(primary: PrimaryPlan) -> tuple[dict[str, StagePlacement], tuple[tuple[str, ...], ...]]:
    placements: dict[str, StagePlacement] = {}
    groups: list[tuple[str, ...]] = []
    for stage in primary.allocation.stages:
        group_id = f"stage-{stage.stage_index:03}"
        placement_id = f"{group_id}-primary"
        capacity = 1000.0 / stage.cost.service_work_ms if stage.cost.service_work_ms > 0 else 1e12
        placements[placement_id] = StagePlacement(
            placement_id,
            group_id,
            stage.node_id,
            stage.layer_range,
            True,
            capacity,
        )
        groups.append((placement_id,))
    return placements, tuple(groups)


def replicate_stages(
    primary: PrimaryPlan,
    graph: PhysicalGraph,
    model: ModelIdentity,
    workload: WorkloadScenario,
    policy: PlanningPolicy,
) -> ReplicationResult:
    placements, groups = _primary_state(primary)
    unused = set(primary.unplaced_node_ids)
    accepted: list[str] = []
    forward, loops, current = _solve(groups, placements, graph, model, workload, policy)
    iterations = 0
    while unused and iterations < policy.replica_budget:
        best = None
        for node_id in sorted(unused):
            node = graph.nodes[node_id]
            for group_index, group in enumerate(groups):
                template = placements[group[0]]
                cost = stage_cost(node, template.layer_range.count, model, workload, policy)
                if not cost.feasible or cost.service_work_ms <= 0:
                    continue
                placement_id = f"stage-{group_index:03}-replica-{node_id}"
                proposal = StagePlacement(
                    placement_id,
                    template.replica_group_id,
                    node_id,
                    template.layer_range,
                    False,
                    1000.0 / cost.service_work_ms,
                )
                proposal_placements = dict(placements)
                proposal_placements[placement_id] = proposal
                proposal_groups = list(groups)
                proposal_groups[group_index] = tuple(sorted(group + (placement_id,)))
                proposal_groups_tuple = tuple(proposal_groups)
                proposal_forward, proposal_loops, proposal_flow = _solve(
                    proposal_groups_tuple,
                    proposal_placements,
                    graph,
                    model,
                    workload,
                    policy,
                )
                gain = proposal_flow.admitted - current.admitted
                candidate = (
                    gain,
                    -proposal_flow.unmet_demand,
                    placement_id,
                    proposal,
                    proposal_groups_tuple,
                    proposal_forward,
                    proposal_loops,
                    proposal_flow,
                )
                if best is None or candidate[:3] > best[:3]:
                    best = candidate
        if best is None or best[0] <= NUMERIC_EPSILON:
            break
        _, _, _, proposal, groups, forward, loops, current = best
        placements[proposal.placement_id] = proposal
        unused.remove(proposal.node_id)
        accepted.append(proposal.node_id)
        iterations += 1

    # Remove any zero-flow replica; primary placements remain as immutable intent.
    used_ids = {pid for track in current.tracks for pid in track.placement_ids}
    removable = {pid for pid, p in placements.items() if not p.primary and pid not in used_ids}
    if removable:
        placements = {pid: p for pid, p in placements.items() if pid not in removable}
        groups = tuple(tuple(pid for pid in group if pid not in removable) for group in groups)
        accepted = [node for node in accepted if any(p.node_id == node for p in placements.values())]
        forward, loops, current = _solve(groups, placements, graph, model, workload, policy)

    return ReplicationResult(
        frozen_primary_order=primary.frozen_primary_order,
        groups=groups,
        placements=tuple(placements[pid] for pid in sorted(placements)),
        forward_edges=forward,
        loopback_edges=loops,
        flow=current,
        accepted_replica_nodes=tuple(accepted),
        iterations=iterations,
    )
