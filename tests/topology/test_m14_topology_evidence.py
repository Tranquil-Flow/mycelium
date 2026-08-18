from __future__ import annotations

import copy

import pytest

from mycelium_topology_evidence import (
    build_m14_topology_projection,
    complete_directed_observation_matrix,
    select_measured_topology,
    validate_m14_topology_projection,
    validate_transport_path_observation,
)


NODES = ("node-a", "node-b", "node-c")
ENDPOINTS = {node: f"endpoint-{node}" for node in NODES}


def observation(src: str, dst: str, rtt: float) -> dict:
    return {
        "protocol": "mycelium.transport_path_observation.v1",
        "local_node_id": src,
        "local_endpoint_id": ENDPOINTS[src],
        "remote_node_id": dst,
        "remote_endpoint_id": ENDPOINTS[dst],
        "connection_generation": 7,
        "path_class": "direct",
        "relay_identity": None,
        "relay_region": None,
        "cold_rtt_ms": rtt + 1,
        "warm_rtt_ms": rtt,
        "observed_goodput_Bps": 10_000_000.0,
        "jitter_ms": 0.25,
        "loss_ratio": 0.0,
        "sample_count": 4,
        "connections_opened": 1,
        "frames_sent": 4,
        "reconnect_count": 0,
        "selected_path_changes": 1,
        "measurement_source": "iroh_activation_plane",
        "measured_at_unix_ms": 1_000,
        "fresh_until_unix_ms": 11_000,
        "exclusions": ["path_transition_not_observed_within_budget"],
    }


def matrix_values() -> list[dict]:
    rtts = {
        ("node-a", "node-b"): 4,
        ("node-b", "node-a"): 20,
        ("node-a", "node-c"): 20,
        ("node-c", "node-a"): 4,
        ("node-b", "node-c"): 4,
        ("node-c", "node-b"): 20,
    }
    return [observation(src, dst, rtt) for (src, dst), rtt in rtts.items()]


def test_unknown_path_is_honest_before_resolution_but_not_eligible() -> None:
    value = observation("node-a", "node-b", 4)
    value.update(
        path_class="unknown",
        sample_count=0,
        connections_opened=0,
        frames_sent=0,
        observed_goodput_Bps=0,
    )

    assert validate_transport_path_observation(value)["path_class"] == "unknown"
    with pytest.raises(ValueError, match="unresolved"):
        validate_transport_path_observation(value, require_resolved=True)


@pytest.mark.parametrize(
    ("mutation", "match"),
    [
        ({"measurement_source": "tailscale_ping"}, "activation-plane"),
        ({"fresh_until_unix_ms": 999}, "freshness"),
        ({"connections_opened": 4}, "connection reuse"),
        ({"local_endpoint_id": "wrong"}, "source endpoint"),
    ],
)
def test_matrix_rejects_unqualified_or_unbound_evidence(
    mutation: dict, match: str
) -> None:
    values = matrix_values()
    values[0].update(mutation)
    with pytest.raises(ValueError, match=match):
        complete_directed_observation_matrix(
            values,
            node_ids=NODES,
            endpoint_ids_by_node=ENDPOINTS,
            now_unix_ms=2_000,
        )


def test_reverse_edge_is_not_inferred() -> None:
    values = matrix_values()
    values.pop()
    with pytest.raises(ValueError, match="incomplete"):
        complete_directed_observation_matrix(
            values,
            node_ids=NODES,
            endpoint_ids_by_node=ENDPOINTS,
            now_unix_ms=2_000,
        )


def test_measured_exact_cycle_is_selected_and_opened_at_entry() -> None:
    matrix = complete_directed_observation_matrix(
        matrix_values(),
        node_ids=NODES,
        endpoint_ids_by_node=ENDPOINTS,
        now_unix_ms=2_000,
    )

    decision = select_measured_topology(
        matrix,
        node_ids=NODES,
        entry_node_id="node-b",
    )

    assert decision["selected_cycle"] == ["node-a", "node-b", "node-c"]
    assert decision["opened_order"] == ["node-b", "node-c", "node-a"]
    assert decision["loopback"] == {"src": "node-a", "dst": "node-b"}
    assert decision["globally_exact"] is True
    assert decision["explored_candidates"] == 2
    assert len(decision["candidates"]) == 2


def test_generic_matrix_can_admit_two_current_physical_nodes() -> None:
    nodes = ("node-a", "node-b")
    endpoints = {node: ENDPOINTS[node] for node in nodes}
    values = [
        observation("node-a", "node-b", 4),
        observation("node-b", "node-a", 7),
    ]

    matrix = complete_directed_observation_matrix(
        values,
        node_ids=nodes,
        endpoint_ids_by_node=endpoints,
        now_unix_ms=2_000,
        minimum_node_count=2,
    )
    decision = select_measured_topology(
        matrix,
        node_ids=nodes,
        entry_node_id="node-b",
    )

    assert set(matrix) == {("node-a", "node-b"), ("node-b", "node-a")}
    assert decision["opened_order"] == ["node-b", "node-a"]
    assert decision["loopback"] == {"src": "node-a", "dst": "node-b"}


def test_m14_default_still_requires_three_nodes() -> None:
    nodes = ("node-a", "node-b")
    endpoints = {node: ENDPOINTS[node] for node in nodes}
    with pytest.raises(ValueError, match="M14 requires at least three"):
        complete_directed_observation_matrix(
            [
                observation("node-a", "node-b", 4),
                observation("node-b", "node-a", 7),
            ],
            node_ids=nodes,
            endpoint_ids_by_node=endpoints,
            now_unix_ms=2_000,
        )


def test_projection_carries_complete_matrix_without_endpoint_addresses() -> None:
    values = matrix_values()
    matrix = complete_directed_observation_matrix(
        values,
        node_ids=NODES,
        endpoint_ids_by_node=ENDPOINTS,
        now_unix_ms=2_000,
    )
    decision = select_measured_topology(
        matrix,
        node_ids=NODES,
        entry_node_id="node-a",
    )
    projection = build_m14_topology_projection(
        observations=values,
        decision=decision,
        allocation=[{"node_id": node, "start": index, "end": index + 1} for index, node in enumerate(decision["opened_order"])],
        promotion=None,
    )

    assert len(projection["edges"]) == 6
    assert {edge["logical_role"] for edge in projection["edges"]} == {
        "physical_only",
        "forward",
        "decode_loopback",
    }
    assert "endpoint_addr" not in repr(projection)
    assert validate_m14_topology_projection(copy.deepcopy(projection)) == projection
