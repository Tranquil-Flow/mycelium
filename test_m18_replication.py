"""M18 robust replica selection and complete-track flow contracts."""

from __future__ import annotations

from dataclasses import replace

from mycelium_layer_planner.contracts import (
    DirectedLinkObservation,
    ModelIdentity,
    NodeCapability,
    PlanningPolicy,
    WorkloadScenario,
)
from mycelium_layer_planner.physical_graph import build_physical_graph
from mycelium_layer_planner.primary_plan import plan_primary
from mycelium_layer_planner.replication import replicate_stages
from mycelium_layer_planner.workload import WorkloadProfile


def _node(node_id: str, speed: float, *, region: str = "unknown") -> NodeCapability:
    return NodeCapability(
        node_id,
        speed,
        speed,
        100_000_000,
        200_000_000,
        1_000_000_000,
        1_000_000_000,
        region=region,
    )


def _case(policy: PlanningPolicy):
    model = ModelIdentity(
        "model",
        "immutable-revision",
        "sha256:" + "a" * 64,
        "Decoder",
        4,
        128,
        2,
        2,
        32,
        4_000,
    )
    workload = WorkloadScenario("concurrent", 10, 10, 16)
    nodes = (
        _node("primary-a", 0.05, region="site-a"),
        _node("primary-b", 0.001, region="site-b"),
        _node("replica-c", 0.001),
        _node("replica-d", 0.001, region="site-a"),
    )
    links = tuple(
        DirectedLinkObservation(src.node_id, dst.node_id, 0.1, 0, 1_000_000_000)
        for src in nodes
        for dst in nodes
        if src.node_id != dst.node_id
    )
    graph = build_physical_graph(nodes, links, policy)
    profile = WorkloadProfile("concurrent", (workload,), "sensitivity_grid", "test")
    primary = plan_primary(
        graph,
        model,
        profile,
        policy,
        force_primary_nodes=("primary-a", "primary-b"),
    )
    return replicate_stages(primary, graph, model, workload, policy)


def test_replica_selection_records_deterministic_robust_gain_and_domain_warning() -> None:
    policy = PlanningPolicy(
        memory_reserve_fraction=0,
        replica_budget=2,
        minimum_replica_gain_fraction=0.05,
        replica_uncertainty_fraction=0.1,
        ttft_slo_ms=1_000_000,
        tpot_slo_ms=1_000_000,
    )

    first = _case(policy)
    second = _case(policy)

    assert first == second
    assert first.flow.admitted > first.primary_capacity_rps
    accepted = [decision for decision in first.candidate_decisions if decision.accepted]
    assert accepted
    assert all(decision.robust_gain_rps > decision.minimum_required_gain_rps for decision in accepted)
    assert any(
        decision.failure_domain_warning in {"failure_domain_unknown", "shared_failure_domain"}
        for decision in first.candidate_decisions
    )
    assert abs(sum(track.traffic_fraction for track in first.flow.tracks) - 1.0) < 1e-9
    assert all(len(track.placement_ids) == len(first.groups) for track in first.flow.tracks)


def test_material_gain_threshold_rejects_epsilon_replication() -> None:
    baseline_policy = PlanningPolicy(
        memory_reserve_fraction=0,
        replica_budget=2,
        minimum_replica_gain_fraction=0.05,
        ttft_slo_ms=1_000_000,
        tpot_slo_ms=1_000_000,
    )
    result = _case(replace(baseline_policy, replica_uncertainty_fraction=0.99))

    assert result.accepted_replica_nodes == ()
    assert result.flow.admitted == result.primary_capacity_rps
    assert result.candidate_decisions
    assert all(not decision.accepted for decision in result.candidate_decisions)
    assert {decision.reason for decision in result.candidate_decisions} <= {
        "gain_below_threshold",
        "resource_total_memory_exceeded",
        "resource_spill_unavailable",
    }
