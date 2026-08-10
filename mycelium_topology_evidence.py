"""M14 activation-plane observations and deterministic directed topology."""
from __future__ import annotations

import hashlib
import itertools
import json
import math
from collections.abc import Mapping, Sequence
from typing import Any

from mycelium_layer_planner.cycle_search import cycle_cost, open_cycle, search_cycle
from mycelium_layer_planner.contracts import PlanningPolicy


TRANSPORT_OBSERVATION_PROTOCOL = "mycelium.transport_path_observation.v1"
TOPOLOGY_PROJECTION_PROTOCOL = "mycelium.m14_topology_projection.v1"
MEASUREMENT_SOURCE = "iroh_activation_plane"
LINK_FORMULA = "one_way_rtt_plus_jitter_v1"
_DIGEST_PREFIX = "sha256:"
_OBSERVATION_FIELDS = {
    "protocol",
    "local_node_id",
    "local_endpoint_id",
    "remote_node_id",
    "remote_endpoint_id",
    "connection_generation",
    "path_class",
    "relay_identity",
    "relay_region",
    "cold_rtt_ms",
    "warm_rtt_ms",
    "observed_goodput_Bps",
    "jitter_ms",
    "loss_ratio",
    "sample_count",
    "connections_opened",
    "frames_sent",
    "reconnect_count",
    "selected_path_changes",
    "measurement_source",
    "measured_at_unix_ms",
    "fresh_until_unix_ms",
    "exclusions",
}


def _json_copy(value: Any) -> Any:
    return json.loads(json.dumps(value, allow_nan=False))


def _digest(value: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        dict(value),
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return _DIGEST_PREFIX + hashlib.sha256(encoded).hexdigest()


def _nonempty_string(document: Mapping[str, Any], field: str) -> str:
    value = document.get(field)
    if not isinstance(value, str) or not value:
        raise ValueError(f"transport observation {field} is invalid")
    return value


def _integer(document: Mapping[str, Any], field: str, *, minimum: int = 0) -> int:
    value = document.get(field)
    if type(value) is not int or value < minimum:
        raise ValueError(f"transport observation {field} is invalid")
    return value


def _number(
    document: Mapping[str, Any],
    field: str,
    *,
    positive: bool = False,
) -> float:
    value = document.get(field)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"transport observation {field} is invalid")
    selected = float(value)
    if not math.isfinite(selected) or selected < 0 or (positive and selected <= 0):
        raise ValueError(f"transport observation {field} is invalid")
    return selected


def validate_transport_path_observation(
    document: Mapping[str, Any],
    *,
    now_unix_ms: int | None = None,
    require_resolved: bool = False,
) -> dict[str, Any]:
    """Validate the closed, privacy-reduced sidecar observation shape."""

    if not isinstance(document, Mapping) or set(document) != _OBSERVATION_FIELDS:
        raise ValueError("transport observation shape is invalid")
    if document.get("protocol") != TRANSPORT_OBSERVATION_PROTOCOL:
        raise ValueError("transport observation protocol is invalid")
    local_node = _nonempty_string(document, "local_node_id")
    remote_node = _nonempty_string(document, "remote_node_id")
    local_endpoint = _nonempty_string(document, "local_endpoint_id")
    remote_endpoint = _nonempty_string(document, "remote_endpoint_id")
    if local_node == remote_node or local_endpoint == remote_endpoint:
        raise ValueError("transport observation endpoints must be distinct")
    _integer(document, "connection_generation", minimum=1)
    path_class = document.get("path_class")
    if path_class not in {"unknown", "direct", "relay"}:
        raise ValueError("transport observation path_class is invalid")
    if require_resolved and path_class == "unknown":
        raise ValueError("transport observation path is unresolved")
    relay_identity = document.get("relay_identity")
    relay_region = document.get("relay_region")
    if relay_identity is not None and not isinstance(relay_identity, str):
        raise ValueError("transport observation relay_identity is invalid")
    if relay_region is not None and not isinstance(relay_region, str):
        raise ValueError("transport observation relay_region is invalid")
    if path_class != "relay" and (relay_identity is not None or relay_region is not None):
        raise ValueError("non-relay observation cannot claim relay identity")
    for field in (
        "cold_rtt_ms",
        "warm_rtt_ms",
        "observed_goodput_Bps",
        "jitter_ms",
        "loss_ratio",
    ):
        _number(document, field, positive=field == "observed_goodput_Bps" and require_resolved)
    if float(document["loss_ratio"]) > 1:
        raise ValueError("transport observation loss_ratio is invalid")
    sample_count = _integer(document, "sample_count")
    connections_opened = _integer(document, "connections_opened")
    frames_sent = _integer(document, "frames_sent")
    for field in ("reconnect_count", "selected_path_changes"):
        _integer(document, field)
    measured = _integer(document, "measured_at_unix_ms")
    fresh_until = _integer(document, "fresh_until_unix_ms")
    if fresh_until <= measured:
        raise ValueError("transport observation freshness window is invalid")
    if now_unix_ms is not None and now_unix_ms > fresh_until:
        raise ValueError("transport observation is stale")
    if document.get("measurement_source") != MEASUREMENT_SOURCE:
        raise ValueError("transport observation is not activation-plane evidence")
    exclusions = document.get("exclusions")
    if not isinstance(exclusions, list) or not all(
        isinstance(item, str) and item for item in exclusions
    ):
        raise ValueError("transport observation exclusions are invalid")
    if require_resolved:
        # The selected path is sampled when the persistent connection opens
        # and again after acknowledged frames, so RTT sample_count may be one
        # greater than frames_sent. Eligibility itself requires three real
        # successful Router frames.
        if sample_count < 3 or frames_sent < 3:
            raise ValueError("transport observation sample window is insufficient")
        if connections_opened < 1 or connections_opened >= frames_sent:
            raise ValueError("transport observation does not prove connection reuse")
    return _json_copy(document)


def complete_directed_observation_matrix(
    observations: Sequence[Mapping[str, Any]],
    *,
    node_ids: Sequence[str],
    endpoint_ids_by_node: Mapping[str, str],
    now_unix_ms: int,
) -> dict[tuple[str, str], dict[str, Any]]:
    """Require one independently measured activation edge for every ordered pair."""

    nodes = tuple(node_ids)
    if len(nodes) < 3 or len(nodes) != len(set(nodes)):
        raise ValueError("M14 requires at least three distinct physical nodes")
    if set(endpoint_ids_by_node) != set(nodes):
        raise ValueError("M14 endpoint inventory does not cover nodes")
    matrix: dict[tuple[str, str], dict[str, Any]] = {}
    for value in observations:
        item = validate_transport_path_observation(
            value,
            now_unix_ms=now_unix_ms,
            require_resolved=True,
        )
        edge = (item["local_node_id"], item["remote_node_id"])
        if edge[0] not in nodes or edge[1] not in nodes:
            raise ValueError("M14 observation contains an unknown node")
        if item["local_endpoint_id"] != endpoint_ids_by_node[edge[0]]:
            raise ValueError("M14 source endpoint binding is invalid")
        if item["remote_endpoint_id"] != endpoint_ids_by_node[edge[1]]:
            raise ValueError("M14 destination endpoint binding is invalid")
        if edge in matrix:
            raise ValueError(f"duplicate M14 observation for {edge[0]}->{edge[1]}")
        matrix[edge] = item
    required = {(src, dst) for src in nodes for dst in nodes if src != dst}
    if set(matrix) != required:
        missing = sorted(required - set(matrix))
        detail = "none" if not missing else f"{missing[0][0]}->{missing[0][1]}"
        raise ValueError(f"M14 directed observation matrix is incomplete:{detail}")
    return matrix


def link_state_from_transport_observation(document: Mapping[str, Any]) -> dict[str, Any]:
    item = validate_transport_path_observation(document, require_resolved=True)
    return {
        "protocol": "mycelium.link_state.v1",
        "src_node_id": item["local_node_id"],
        "dst_node_id": item["remote_node_id"],
        "src_endpoint_id": item["local_endpoint_id"],
        "dst_endpoint_id": item["remote_endpoint_id"],
        "reachable": True,
        "connect_rtt_ema_ms": item["warm_rtt_ms"],
        # Planning prices the reusable activation path. Cold connection setup
        # remains visible in the observation but is outside the steady-state
        # directed-cycle objective.
        "rtt_p95_ms": item["warm_rtt_ms"],
        "jitter_ms": item["jitter_ms"],
        "loss_ratio": item["loss_ratio"],
        "goodput_mbps": item["observed_goodput_Bps"] * 8.0 / 1_000_000.0,
        "sample_count": item["sample_count"],
        "measurement_method": "iroh-activation-plane-v1",
        "measurement_payload_bytes": 0,
        "connection_state": item["path_class"],
        "extensions": {
            "m14": {
                "transport_observation_digest": _digest(item),
                "connection_generation": item["connection_generation"],
                "path_class": item["path_class"],
                "connections_opened": item["connections_opened"],
                "frames_sent": item["frames_sent"],
                "fresh_until_unix_ms": item["fresh_until_unix_ms"],
                "formula": LINK_FORMULA,
            }
        },
    }


def select_measured_topology(
    matrix: Mapping[tuple[str, str], Mapping[str, Any]],
    *,
    node_ids: Sequence[str],
    entry_node_id: str,
    policy: PlanningPolicy | None = None,
) -> dict[str, Any]:
    """Select and open a directed cycle from the complete measured matrix."""

    nodes = tuple(sorted(node_ids))
    if entry_node_id not in nodes:
        raise ValueError("M14 entry node is not eligible")
    required = {(src, dst) for src in nodes for dst in nodes if src != dst}
    if set(matrix) != required:
        raise ValueError("M14 topology selection requires a complete matrix")

    def cost(src: str, dst: str) -> float | None:
        item = matrix.get((src, dst))
        if item is None:
            return None
        return float(item["warm_rtt_ms"]) / 2.0 + float(item["jitter_ms"])

    selected = search_cycle(nodes, cost, policy or PlanningPolicy())
    start_index = selected.order.index(entry_node_id)
    opened = open_cycle(selected.order, start_index)
    first = nodes[0]
    candidates = []
    for tail in itertools.permutations(nodes[1:]):
        order = (first, *tail)
        value = cycle_cost(order, cost)
        candidates.append(
            {
                "order": list(order),
                "cost_ms": value,
                "selected": order == selected.order,
                "rejection_reason": None if math.isfinite(value) else "missing_edge",
            }
        )
    candidates.sort(key=lambda item: (item["cost_ms"], item["order"]))
    return {
        "mode": selected.mode,
        "globally_exact": selected.globally_exact,
        "explored_candidates": selected.explored_candidates,
        "selected_cycle": list(selected.order),
        "selected_cost_ms": selected.cost,
        "opened_order": list(opened.order),
        "loopback": {"src": opened.loopback[0], "dst": opened.loopback[1]},
        "canonical_node_id_order": list(nodes),
        "differs_from_canonical_order": selected.order != nodes,
        "candidates": candidates,
        "winning_rationale": (
            "minimum measured directed RTT/2 plus jitter; stable lexicographic tie-break"
        ),
    }


def build_m14_topology_projection(
    *,
    observations: Sequence[Mapping[str, Any]],
    decision: Mapping[str, Any],
    allocation: Sequence[Mapping[str, Any]],
    promotion: Mapping[str, Any] | None,
    exclusions: Sequence[str] = (),
) -> dict[str, Any]:
    edges = []
    opened = list(decision.get("opened_order", []))
    loopback = decision.get("loopback", {})
    for item in observations:
        validated = validate_transport_path_observation(item, require_resolved=True)
        src = validated["local_node_id"]
        dst = validated["remote_node_id"]
        role = "physical_only"
        if any(pair == (src, dst) for pair in zip(opened, opened[1:])):
            role = "forward"
        if (src, dst) == (loopback.get("src"), loopback.get("dst")):
            role = "decode_loopback"
        edges.append(
            {
                "src": src,
                "dst": dst,
                "src_endpoint_digest": _DIGEST_PREFIX
                + hashlib.sha256(validated["local_endpoint_id"].encode()).hexdigest(),
                "dst_endpoint_digest": _DIGEST_PREFIX
                + hashlib.sha256(validated["remote_endpoint_id"].encode()).hexdigest(),
                "path_class": validated["path_class"],
                "relay_identity": validated["relay_identity"],
                "relay_region": validated["relay_region"],
                "rtt_ms": validated["warm_rtt_ms"],
                "jitter_ms": validated["jitter_ms"],
                "loss_ratio": validated["loss_ratio"],
                "goodput_Bps": validated["observed_goodput_Bps"],
                "sample_count": validated["sample_count"],
                "connections_opened": validated["connections_opened"],
                "frames_sent": validated["frames_sent"],
                "connection_generation": validated["connection_generation"],
                "fresh_until_unix_ms": validated["fresh_until_unix_ms"],
                "observation_digest": _digest(validated),
                "formula": LINK_FORMULA,
                "logical_role": role,
            }
        )
    projection = {
        "protocol": TOPOLOGY_PROJECTION_PROTOCOL,
        "measurement_source": MEASUREMENT_SOURCE,
        "decision": _json_copy(decision),
        "allocation": _json_copy(list(allocation)),
        "edges": sorted(edges, key=lambda item: (item["src"], item["dst"])),
        "exclusions": list(exclusions),
        "promotion": None if promotion is None else _json_copy(promotion),
        "route_ready": False,
    }
    return validate_m14_topology_projection(projection)


def validate_m14_topology_projection(document: Mapping[str, Any]) -> dict[str, Any]:
    fields = {
        "protocol",
        "measurement_source",
        "decision",
        "allocation",
        "edges",
        "exclusions",
        "promotion",
        "route_ready",
    }
    if not isinstance(document, Mapping) or set(document) != fields:
        raise ValueError("M14 topology projection shape is invalid")
    if (
        document.get("protocol") != TOPOLOGY_PROJECTION_PROTOCOL
        or document.get("measurement_source") != MEASUREMENT_SOURCE
        or document.get("route_ready") is not False
    ):
        raise ValueError("M14 topology projection authority is invalid")
    decision = document.get("decision")
    edges = document.get("edges")
    allocation = document.get("allocation")
    exclusions = document.get("exclusions")
    if not isinstance(decision, Mapping):
        raise ValueError("M14 topology decision is invalid")
    if not all(isinstance(value, list) for value in (edges, allocation, exclusions)):
        raise ValueError("M14 topology projection arrays are invalid")
    opened = decision.get("opened_order")
    if not isinstance(opened, list) or len(opened) < 3 or len(opened) != len(set(opened)):
        raise ValueError("M14 opened order is invalid")
    required = {(src, dst) for src in opened for dst in opened if src != dst}
    actual = {
        (item.get("src"), item.get("dst"))
        for item in edges
        if isinstance(item, Mapping)
    }
    if actual != required:
        raise ValueError("M14 projected directed matrix is incomplete")
    forbidden = {
        "prompt",
        "response",
        "token_ids",
        "activation",
        "artifact_root",
        "private_key",
        "endpoint_addr",
    }

    def reject_private(value: Any) -> None:
        if isinstance(value, Mapping):
            if forbidden.intersection(value):
                raise ValueError("M14 topology projection contains private fields")
            for item in value.values():
                reject_private(item)
        elif isinstance(value, list):
            for item in value:
                reject_private(item)

    reject_private(document)
    return _json_copy(document)


__all__ = [
    "LINK_FORMULA",
    "MEASUREMENT_SOURCE",
    "TOPOLOGY_PROJECTION_PROTOCOL",
    "TRANSPORT_OBSERVATION_PROTOCOL",
    "build_m14_topology_projection",
    "complete_directed_observation_matrix",
    "link_state_from_transport_observation",
    "select_measured_topology",
    "validate_m14_topology_projection",
    "validate_transport_path_observation",
]
