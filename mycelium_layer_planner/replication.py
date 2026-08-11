from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Mapping, Sequence

from .allocation import stage_cost
from .contracts import NUMERIC_EPSILON, ModelIdentity, PlanningPolicy, StagePlacement, WorkloadScenario
from .flow import FlowEdge, FlowResult, assign_flow
from .network_cost import transfer_time_ms
from .physical_graph import PhysicalGraph
from .primary_plan import PrimaryPlan


@dataclass(frozen=True)
class ReplicaCandidateDecision:
    iteration: int
    placement_id: str
    node_id: str
    replica_group_id: str
    accepted: bool
    reason: str
    baseline_admitted_rps: float
    proposed_admitted_rps: float
    raw_gain_rps: float
    robust_gain_rps: float
    minimum_required_gain_rps: float
    failure_domain: str
    failure_domain_warning: str | None


@dataclass(frozen=True)
class ReplicationResult:
    frozen_primary_order: tuple[str, ...]
    groups: tuple[tuple[str, ...], ...]
    placements: tuple[StagePlacement, ...]
    forward_edges: tuple[FlowEdge, ...]
    loopback_edges: tuple[FlowEdge, ...]
    flow: FlowResult
    accepted_replica_nodes: tuple[str, ...]
    candidate_decisions: tuple[ReplicaCandidateDecision, ...]
    zero_flow_removed_placement_ids: tuple[str, ...]
    primary_capacity_rps: float
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
    primary_capacity_rps = current.admitted
    decisions: list[ReplicaCandidateDecision] = []
    iterations = 0
    while unused and iterations < policy.replica_budget:
        best = None
        evaluated: list[tuple[tuple[object, ...], ReplicaCandidateDecision]] = []
        for node_id in sorted(unused):
            node = graph.nodes[node_id]
            for group_index, group in enumerate(groups):
                template = placements[group[0]]
                cost = stage_cost(node, template.layer_range.count, model, workload, policy)
                if not cost.feasible or cost.service_work_ms <= 0:
                    decisions.append(
                        ReplicaCandidateDecision(
                            iteration=iterations,
                            placement_id=f"stage-{group_index:03}-replica-{node_id}",
                            node_id=node_id,
                            replica_group_id=template.replica_group_id,
                            accepted=False,
                            reason=f"resource_{cost.diagnostic or 'infeasible'}",
                            baseline_admitted_rps=current.admitted,
                            proposed_admitted_rps=current.admitted,
                            raw_gain_rps=0.0,
                            robust_gain_rps=0.0,
                            minimum_required_gain_rps=(
                                current.admitted * policy.minimum_replica_gain_fraction
                            ),
                            failure_domain=node.region,
                            failure_domain_warning=(
                                "failure_domain_unknown"
                                if node.region == "unknown"
                                else None
                            ),
                        )
                    )
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
                uncertainty = max(
                    policy.replica_uncertainty_fraction,
                    1.0 - node.calibration_confidence,
                )
                robust_gain = max(0.0, gain * (1.0 - uncertainty))
                minimum_gain = current.admitted * policy.minimum_replica_gain_fraction
                primary_node = graph.nodes[template.node_id]
                failure_domain_warning = None
                if node.region == "unknown" or primary_node.region == "unknown":
                    failure_domain_warning = "failure_domain_unknown"
                elif node.region == primary_node.region:
                    failure_domain_warning = "shared_failure_domain"
                candidate = (
                    robust_gain,
                    gain,
                    -proposal_flow.unmet_demand,
                    placement_id,
                    proposal,
                    proposal_groups_tuple,
                    proposal_forward,
                    proposal_loops,
                    proposal_flow,
                )
                decision = ReplicaCandidateDecision(
                    iteration=iterations,
                    placement_id=placement_id,
                    node_id=node_id,
                    replica_group_id=template.replica_group_id,
                    accepted=False,
                    reason=(
                        "gain_below_threshold"
                        if robust_gain <= max(NUMERIC_EPSILON, minimum_gain)
                        else "lower_ranked_candidate"
                    ),
                    baseline_admitted_rps=current.admitted,
                    proposed_admitted_rps=proposal_flow.admitted,
                    raw_gain_rps=gain,
                    robust_gain_rps=robust_gain,
                    minimum_required_gain_rps=minimum_gain,
                    failure_domain=node.region,
                    failure_domain_warning=failure_domain_warning,
                )
                evaluated.append((candidate, decision))
                if best is None or candidate[:4] > best[:4]:
                    best = candidate
        if best is None or best[0] <= max(
            NUMERIC_EPSILON,
            current.admitted * policy.minimum_replica_gain_fraction,
        ):
            decisions.extend(decision for _candidate, decision in evaluated)
            break
        _, _, _, selected_id, proposal, groups, forward, loops, current = best
        decisions.extend(
            replace(
                decision,
                accepted=decision.placement_id == selected_id,
                reason=(
                    "accepted_positive_robust_gain"
                    if decision.placement_id == selected_id
                    else decision.reason
                ),
            )
            for _candidate, decision in evaluated
        )
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
        candidate_decisions=tuple(decisions),
        zero_flow_removed_placement_ids=tuple(sorted(removable)),
        primary_capacity_rps=primary_capacity_rps,
        iterations=iterations,
    )
