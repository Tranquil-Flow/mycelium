"""Privacy-reduced M13 placement projection shared by product workspaces."""
from __future__ import annotations

import copy
import hashlib
import json
from typing import Any, Mapping, Sequence

from .contracts import RoutePlanV2
from .gossip_adapter import planner_snapshot_digest
from .serialization import route_plan_to_dict


PROTOCOL = "mycelium.m13_placement_projection.v1"
_TOP_LEVEL_FIELDS = {
    "protocol", "snapshot_digest", "evidence_bundle_digest", "snapshot_generation",
    "authority_generation", "verification_key_digest", "valid_until_unix_ms",
    "placement_provenance", "decode_mode", "quantization", "nodes", "links",
    "exclusions", "ab_deltas", "promotion", "route_ready",
}


def _digest(value: Mapping[str, Any]) -> str:
    payload = json.dumps(
        dict(value),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def build_m13_placement_projection(
    *,
    planner_snapshot: Mapping[str, Any],
    route_plan: Mapping[str, Any] | RoutePlanV2,
    assignments: Sequence[Mapping[str, Any]],
    materializations_by_node: Mapping[str, Mapping[str, Any]],
    load_proof_digests_by_node: Mapping[str, str],
    promotion_report: Mapping[str, Any] | None,
    exclusions: Sequence[Mapping[str, Any]] = (),
    ab_deltas: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    if planner_snapshot.get("placement_provenance") != "planner_v2":
        raise ValueError("M13 projection requires planner_v2 provenance")
    authority = planner_snapshot.get("evidence_authority")
    profiles = planner_snapshot.get("capacity_profile_bindings")
    runtimes = planner_snapshot.get("node_runtime")
    if not all(isinstance(value, Mapping) for value in (authority, profiles, runtimes)):
        raise ValueError("M13 projection requires signed authority and profile bindings")
    route = (
        route_plan_to_dict(route_plan)
        if isinstance(route_plan, RoutePlanV2)
        else copy.deepcopy(dict(route_plan))
    )
    if route.get("snapshot_digest") != planner_snapshot_digest(planner_snapshot):
        raise ValueError("M13 route does not bind the planner snapshot")
    placements = {
        placement["node_id"]: placement
        for placement in route.get("placements", [])
        if placement.get("primary") is True
    }
    assignment_by_node = {item.get("node_id"): item for item in assignments}
    if set(placements) != set(assignment_by_node):
        raise ValueError("M13 assignments do not cover primary placements")
    nodes = []
    for node in planner_snapshot.get("nodes", []):
        node_id = node.get("node_id")
        placement = placements.get(node_id)
        assignment = assignment_by_node.get(node_id)
        profile = profiles.get(node_id)
        runtime = runtimes.get(node_id)
        if not all(isinstance(value, Mapping) for value in (placement, assignment, profile, runtime)):
            raise ValueError("M13 node projection binding is incomplete")
        materialization = materializations_by_node.get(node_id)
        proof_digest = load_proof_digests_by_node.get(node_id)
        if materialization is not None and materialization.get("assignment_id") != assignment.get(
            "assignment_id"
        ):
            raise ValueError("M13 materialization assignment binding is invalid")
        layer_range = placement.get("layer_range")
        if not isinstance(layer_range, Mapping):
            raise ValueError("M13 placement range is invalid")
        nodes.append(
            {
                "node_id": node_id,
                "backend": runtime.get("backend"),
                "decode_mode": runtime.get("decode_mode"),
                "start_layer": layer_range.get("start"),
                "end_layer_exclusive": layer_range.get("end"),
                "fast_allocatable_bytes": node.get("fast_memory_bytes"),
                "total_allocatable_bytes": node.get("total_memory_bytes"),
                "prefill_ms_per_layer_token": node.get("prefill_ms_per_layer_token"),
                "decode_ms_per_layer_token": node.get("decode_ms_per_layer_token"),
                "profile_digest": profile.get("profile_digest"),
                "source_evidence_digest": profile.get("source_evidence_digest"),
                "assignment_id": assignment.get("assignment_id"),
                "assignment_digest": _digest(assignment),
                "assigned_object_count": (
                    0 if materialization is None else len(materialization.get("objects", []))
                ),
                "load_proof_digest": proof_digest,
                "ready": materialization is not None and isinstance(proof_digest, str),
            }
        )
    projection = {
        "protocol": PROTOCOL,
        "snapshot_digest": planner_snapshot_digest(planner_snapshot),
        "evidence_bundle_digest": planner_snapshot.get("evidence_bundle_digest"),
        "snapshot_generation": planner_snapshot.get("snapshot_generation"),
        "authority_generation": authority.get("authority_generation"),
        "verification_key_digest": authority.get("verification_key_digest"),
        "valid_until_unix_ms": authority.get("valid_until_unix_ms"),
        "placement_provenance": "planner_v2",
        "decode_mode": planner_snapshot.get("decode_mode"),
        "quantization": planner_snapshot.get("quantization"),
        "nodes": nodes,
        "links": [
            {
                "src": link.get("src"),
                "dst": link.get("dst"),
                "rtt_ms": link.get("rtt_ms"),
                "jitter_ms": link.get("jitter_ms"),
                "bandwidth_Bps": link.get("bandwidth_Bps"),
            }
            for link in planner_snapshot.get("links", [])
        ],
        "exclusions": copy.deepcopy(list(exclusions)),
        "ab_deltas": copy.deepcopy(list(ab_deltas)),
        "promotion": None if promotion_report is None else {
            "candidate_deployment_id": promotion_report.get("candidate_deployment_id"),
            "incumbent_deployment_id": promotion_report.get("incumbent_deployment_id"),
            "decision": promotion_report.get("decision"),
            "reasons": copy.deepcopy(promotion_report.get("reasons")),
            "sample_size": promotion_report.get("metrics", {}).get("sample_size"),
        },
        "route_ready": False,
    }
    return copy.deepcopy(projection)


def validate_m13_placement_projection(document: Mapping[str, Any]) -> dict[str, Any]:
    """Validate the closed public shape loaded beside a physical candidate."""

    if not isinstance(document, Mapping) or set(document) != _TOP_LEVEL_FIELDS:
        raise ValueError("M13 placement projection shape is invalid")
    if (
        document.get("protocol") != PROTOCOL
        or document.get("placement_provenance") != "planner_v2"
        or document.get("route_ready") is not False
    ):
        raise ValueError("M13 placement projection authority is invalid")
    for field in ("snapshot_digest", "evidence_bundle_digest", "verification_key_digest"):
        value = document.get(field)
        if (
            not isinstance(value, str)
            or not value.startswith("sha256:")
            or len(value) != 71
            or any(character not in "0123456789abcdef" for character in value[7:])
        ):
            raise ValueError(f"M13 placement projection {field} is invalid")
    for field in ("snapshot_generation", "authority_generation", "valid_until_unix_ms"):
        if type(document.get(field)) is not int or document[field] < 0:
            raise ValueError(f"M13 placement projection {field} is invalid")
    if not isinstance(document.get("nodes"), list) or not document["nodes"]:
        raise ValueError("M13 placement projection nodes are invalid")
    if not all(isinstance(document.get(field), list) for field in ("links", "exclusions", "ab_deltas")):
        raise ValueError("M13 placement projection arrays are invalid")
    forbidden = {"prompt", "response", "token_ids", "activation", "artifact_root", "private_key"}

    def reject_private(value: Any) -> None:
        if isinstance(value, Mapping):
            if forbidden.intersection(value):
                raise ValueError("M13 placement projection contains private fields")
            for item in value.values():
                reject_private(item)
        elif isinstance(value, list):
            for item in value:
                reject_private(item)

    reject_private(document)
    try:
        detached = json.loads(json.dumps(document, allow_nan=False))
    except (TypeError, ValueError, RecursionError) as exc:
        raise ValueError("M13 placement projection is not JSON-safe") from exc
    return detached


__all__ = ["PROTOCOL", "build_m13_placement_projection", "validate_m13_placement_projection"]
