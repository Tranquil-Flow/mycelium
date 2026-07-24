import math
from collections.abc import Callable, Mapping

import pytest

from mycelium_layer_planner.contracts import (
    DirectedLinkObservation,
    ModelIdentity,
    PlanningPolicy,
)
from mycelium_layer_planner.cycle_search import cycle_cost, open_cycle, search_cycle
from mycelium_layer_planner.network_cost import EdgeCost, phase_edge_costs


NODES = ("node-a", "node-b", "node-c")
ENTRY_NODE = NODES[0]
POLICY = PlanningPolicy()
MODEL = ModelIdentity(
    model_id="synthetic/dialogpt-small-shape",
    revision="offline",
    weight_digest=f"sha256:{'0' * 64}",
    architecture="gpt2",
    num_layers=12,
    hidden_size=768,
    dtype_bytes=2,
    kv_heads=12,
    head_dim=64,
    weight_bytes=1,
)
PAYLOAD_BYTES = {
    "activation": MODEL.activation_bytes(1),
    "token_envelope": 9,
}
ASYMMETRIC_LINK_SPECS = {
    ("node-a", "node-b"): (8.0, 2_000_000.0),
    ("node-b", "node-a"): (30.0, 100_000_000.0),
    ("node-a", "node-c"): (12.0, 100_000_000.0),
    ("node-c", "node-a"): (50.0, 1_000_000.0),
    ("node-b", "node-c"): (40.0, 1_000_000.0),
    ("node-c", "node-b"): (4.0, 100_000_000.0),
}

ScoredLinks = Mapping[tuple[str, str], Mapping[str, EdgeCost]]


def _score_links(
    link_specs: Mapping[tuple[str, str], tuple[float, float]],
) -> dict[tuple[str, str], dict[str, EdgeCost]]:
    return {
        edge: phase_edge_costs(
            DirectedLinkObservation(
                src=edge[0],
                dst=edge[1],
                rtt_ms=rtt_ms,
                jitter_ms=0.0,
                bandwidth_Bps=bandwidth_Bps,
            ),
            PAYLOAD_BYTES,
            POLICY,
        )
        for edge, (rtt_ms, bandwidth_Bps) in link_specs.items()
    }


def _fixed_entry_cost(scored: ScoredLinks) -> Callable[[str, str], float | None]:
    def cost(src: str, dst: str) -> float | None:
        edge = scored.get((src, dst))
        if edge is None:
            return None
        payload = "token_envelope" if dst == ENTRY_NODE else "activation"
        return edge[payload].total_ms

    return cost


def _symmetric_link_cost(scored: ScoredLinks) -> Callable[[str, str], float | None]:
    def cost(src: str, dst: str) -> float | None:
        forward = scored.get((src, dst))
        reverse = scored.get((dst, src))
        if forward is None or reverse is None:
            return None
        payload = "token_envelope" if dst == ENTRY_NODE else "activation"
        return (forward[payload].total_ms + reverse[payload].total_ms) / 2.0

    return cost


def test_payload_aware_exact_search_beats_naive_order_deterministically():
    scored = _score_links(ASYMMETRIC_LINK_SPECS)
    directed_cost = _fixed_entry_cost(scored)

    assert PAYLOAD_BYTES == {"activation": 1_536, "token_envelope": 9}
    assert scored[("node-a", "node-c")]["activation"].payload_bytes == 1_536
    assert scored[("node-b", "node-a")]["token_envelope"].payload_bytes == 9

    result = search_cycle(reversed(NODES), directed_cost, POLICY)
    naive_cost = cycle_cost(NODES, directed_cost)
    assert result.order == ("node-a", "node-c", "node-b")
    assert result.cost == pytest.approx(23.030810)
    assert naive_cost == pytest.approx(51.313000)
    assert naive_cost - result.cost == pytest.approx(28.282190)
    assert result.mode == "exact_enumeration"
    assert result.globally_exact is True
    assert result.explored_candidates == 2
    assert search_cycle(("node-b", "node-a", "node-c"), directed_cost, POLICY) == result

    opened = open_cycle(result.order)
    assert opened.order == result.order
    assert opened.loopback == ("node-b", "node-a")
    assert scored[opened.loopback]["token_envelope"].payload_bytes == 9


def test_symmetric_link_simplification_changes_the_selected_order():
    scored = _score_links(ASYMMETRIC_LINK_SPECS)

    directed_result = search_cycle(NODES, _fixed_entry_cost(scored), POLICY)
    symmetric_cost = _symmetric_link_cost(scored)
    symmetric_result = search_cycle(NODES, symmetric_cost, POLICY)

    assert directed_result.order == ("node-a", "node-c", "node-b")
    assert symmetric_result.order == NODES
    assert symmetric_result.order != directed_result.order
    assert symmetric_result.cost == pytest.approx(37.171905)
    assert cycle_cost(("node-a", "node-c", "node-b"), symmetric_cost) == (
        pytest.approx(37.553655)
    )


def test_equal_link_control_is_an_exact_zero_delta_null_result():
    link_specs = {
        (src, dst): (10.0, 10_000_000.0)
        for src in NODES
        for dst in NODES
        if src != dst
    }
    scored = _score_links(link_specs)
    directed_cost = _fixed_entry_cost(scored)

    result = search_cycle(("node-c", "node-a", "node-b"), directed_cost, POLICY)
    naive_cost = cycle_cost(NODES, directed_cost)

    assert result.order == NODES
    assert result.cost == pytest.approx(15.308100)
    assert naive_cost == pytest.approx(result.cost)
    assert naive_cost - result.cost == pytest.approx(0.0)
    assert cycle_cost(("node-a", "node-c", "node-b"), directed_cost) == (
        pytest.approx(result.cost)
    )
    assert result.mode == "exact_enumeration"
    assert result.globally_exact is True
    assert result.explored_candidates == 2


def test_missing_directed_edges_still_reject_the_cycle():
    scored = _score_links(
        {
            ("node-a", "node-b"): (8.0, 2_000_000.0),
            ("node-b", "node-c"): (12.0, 3_500_000.0),
        }
    )
    incomplete_cost = _fixed_entry_cost(scored)

    assert math.isinf(cycle_cost(NODES, incomplete_cost))
    with pytest.raises(ValueError, match="no feasible directed cycle"):
        search_cycle(NODES, incomplete_cost, POLICY)
