from __future__ import annotations

import pytest

from mycelium_layer_planner.contracts import (
    DirectedLinkObservation,
    ModelIdentity,
    NodeCapability,
    PlanningPolicy,
)
from mycelium_layer_planner.physical_graph import build_physical_graph
from mycelium_layer_planner import primary_plan as primary_plan_module
from mycelium_layer_planner.primary_plan import plan_primary
from mycelium_layer_planner.workload import empirical_interactive_chat


def _model(*, layers: int = 6) -> ModelIdentity:
    return ModelIdentity(
        "model",
        "immutable-revision",
        "sha256:" + "a" * 64,
        "Decoder",
        layers,
        128,
        2,
        2,
        32,
        layers * 1_000,
    )


def _node(node_id: str, *, runtime: float = 0.001) -> NodeCapability:
    return NodeCapability(
        node_id,
        runtime,
        runtime,
        100_000_000,
        200_000_000,
        1_000_000_000,
        1_000_000_000,
    )


def _links() -> tuple[DirectedLinkObservation, ...]:
    # Clockwise path has lower latency but deliberately hostile bandwidth/loss.
    # Ordering must still choose it because allocation and payload economics are
    # separate decisions.
    clockwise = {("a", "b"), ("b", "c"), ("c", "a")}
    links = []
    for src in ("a", "b", "c"):
        for dst in ("a", "b", "c"):
            if src == dst:
                continue
            low_latency = (src, dst) in clockwise
            links.append(
                DirectedLinkObservation(
                    src,
                    dst,
                    rtt_ms=2.0 if low_latency else 20.0,
                    jitter_ms=0.25,
                    bandwidth_Bps=1.0 if low_latency else 1_000_000_000.0,
                    loss_ratio=0.9 if low_latency else 0.0,
                )
            )
    return tuple(links)


def _graph(*, runtimes: dict[str, float] | None = None):
    runtimes = runtimes or {}
    policy = PlanningPolicy(memory_reserve_fraction=0)
    nodes = tuple(
        _node(node_id, runtime=runtimes.get(node_id, 0.001))
        for node_id in ("a", "b", "c")
    )
    return build_physical_graph(nodes, _links(), policy), policy


def test_topology_order_uses_only_directed_latency_and_jitter() -> None:
    graph, policy = _graph()

    result = primary_plan_module.select_topology_order(graph, policy)

    assert result.order == ("a", "b", "c")
    assert result.cost == pytest.approx(3.75)


def test_primary_order_cannot_change_when_device_compute_changes() -> None:
    fast_graph, policy = _graph()
    slow_graph, _ = _graph(runtimes={"a": 100.0, "b": 0.001, "c": 0.001})
    profile = empirical_interactive_chat(concurrency_points=(1,))

    fast = plan_primary(fast_graph, _model(), profile, policy)
    slow = plan_primary(slow_graph, _model(), profile, policy)

    assert fast.order == slow.order == ("a", "b", "c")
    assert fast.unplaced_node_ids == slow.unplaced_node_ids == ()
    assert tuple(stage.node_id for stage in fast.allocation.stages) == fast.order
    assert tuple(stage.node_id for stage in slow.allocation.stages) == slow.order
    assert all(stage.layer_range.count >= 1 for stage in slow.allocation.stages)


def test_planner_rejects_more_admitted_nodes_than_model_layers() -> None:
    graph, policy = _graph()

    with pytest.raises(ValueError, match="admitted node count exceeds layer count"):
        plan_primary(
            graph,
            _model(layers=2),
            empirical_interactive_chat(concurrency_points=(1,)),
            policy,
        )
