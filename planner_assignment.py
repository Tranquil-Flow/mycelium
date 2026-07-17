#!/usr/bin/env python3
"""Bind product Planner placement intent to executable provisioning assignments."""
from __future__ import annotations

import copy
from typing import Any, Mapping

import model_manifest as mm
from layer_assignment import compile_layer_assignments, validate_assignment_identity
from mycelium_gossip.evidence_bundle import evidence_bundle_from_dict, evidence_bundle_to_dict
from mycelium_layer_planner.contracts import RoutePlanV2
from mycelium_layer_planner.gossip_adapter import planner_snapshot_digest
from mycelium_layer_planner.serialization import route_plan_to_dict
from route_contract import validate_manual_provisioning_route_v1

CONTROL_PLANE_BINDING_PROTOCOL = "mycelium.control_plane_binding.v1"


def _route_wire(value: Mapping[str, Any] | RoutePlanV2) -> dict[str, Any]:
    if isinstance(value, RoutePlanV2):
        return route_plan_to_dict(value)
    if isinstance(value, Mapping):
        return copy.deepcopy(dict(value))
    raise ValueError("route_plan must be a RoutePlanV2 or mapping")


def _validate_lineage(
    *,
    route_plan: Mapping[str, Any],
    planner_snapshot: Mapping[str, Any],
    evidence_bundle: Mapping[str, Any],
    manifest: Mapping[str, Any],
    deployment_id: str,
    deployment_epoch: int,
) -> tuple[dict[str, Any], str]:
    bundle = evidence_bundle_to_dict(evidence_bundle_from_dict(evidence_bundle))
    if planner_snapshot.get("protocol") != "mycelium.layer_planner_snapshot.v1":
        raise ValueError("planner snapshot protocol mismatch")
    snapshot_digest = planner_snapshot_digest(planner_snapshot)
    if route_plan.get("protocol") != "mycelium.route_plan.v2":
        raise ValueError("expected mycelium.route_plan.v2")
    if route_plan.get("snapshot_digest") != snapshot_digest:
        raise ValueError("route snapshot_digest does not bind the supplied planner snapshot")
    if planner_snapshot.get("evidence_bundle_digest") != bundle["evidence_bundle_digest"]:
        raise ValueError("planner snapshot evidence_bundle_digest mismatch")
    if planner_snapshot.get("snapshot_generation") != bundle["snapshot_generation"]:
        raise ValueError("planner snapshot snapshot_generation mismatch")
    if planner_snapshot.get("swarm_id") != bundle["swarm_id"]:
        raise ValueError("planner snapshot swarm_id mismatch")
    snapshot_deployment = planner_snapshot.get("deployment")
    if snapshot_deployment != bundle["deployment"]:
        raise ValueError("planner snapshot deployment identity mismatch")
    if deployment_id != bundle["deployment"]["deployment_id"]:
        raise ValueError("deployment_id does not match evidence bundle")
    if deployment_epoch != bundle["deployment"]["deployment_epoch"]:
        raise ValueError("deployment_epoch does not match evidence bundle")

    if not mm.verify_manifest_digest(dict(manifest)):
        raise ValueError("manifest digest mismatch")
    manifest_identity = {
        "model_id": manifest.get("model_id"),
        "num_layers": manifest.get("num_layers"),
        "revision": manifest.get("resolved_commit"),
        "weight_digest": mm.manifest_digest_ref(dict(manifest)),
    }
    route_model = route_plan.get("model")
    snapshot_model = planner_snapshot.get("model")
    if not isinstance(route_model, Mapping) or not isinstance(snapshot_model, Mapping):
        raise ValueError("route and planner snapshot model identities are required")
    for field, expected in manifest_identity.items():
        if route_model.get(field) != expected:
            raise ValueError(f"route model {field} does not match manifest")
        if snapshot_model.get(field) != expected:
            raise ValueError(f"planner model {field} does not match manifest")
    bundle_identity = {
        "model_id": manifest_identity["model_id"],
        "num_layers": manifest_identity["num_layers"],
        "resolved_commit": manifest_identity["revision"],
        "manifest_digest": manifest_identity["weight_digest"],
    }
    for field, expected in bundle_identity.items():
        if bundle["model"].get(field) != expected:
            raise ValueError(f"evidence bundle model {field} does not match manifest")
    if dict(route_model) != dict(snapshot_model):
        raise ValueError("route model does not equal bound planner snapshot model")
    return bundle, snapshot_digest


def product_route_to_manual_provisioning_route(
    *,
    route_plan: Mapping[str, Any] | RoutePlanV2,
    manifest: Mapping[str, Any],
) -> dict[str, Any]:
    route = _route_wire(route_plan)
    if route.get("protocol") != "mycelium.route_plan.v2":
        raise ValueError("expected mycelium.route_plan.v2")
    if route.get("handoff_state") != "placement_intent_only":
        raise ValueError("product route handoff_state must remain placement_intent_only")
    tracks = route.get("legal_tracks")
    if not isinstance(tracks, list) or len(tracks) != 1 or tracks[0].get("traffic_fraction") != 1.0:
        raise ValueError("MVP assignment requires exactly one legal track with traffic_fraction 1.0")
    placement_ids = tracks[0].get("placement_ids")
    if not isinstance(placement_ids, list) or not placement_ids or len(set(placement_ids)) != len(placement_ids):
        raise ValueError("legal track placement_ids must be unique and non-empty")

    placements_raw = route.get("placements")
    if not isinstance(placements_raw, list) or not placements_raw:
        raise ValueError("route placements must be non-empty")
    if any(placement.get("primary") is not True for placement in placements_raw):
        raise ValueError("replica placements are not supported by the MVP assignment adapter")
    placements: dict[str, Mapping[str, Any]] = {}
    for placement in placements_raw:
        placement_id = placement.get("placement_id")
        if not isinstance(placement_id, str) or not placement_id or placement_id in placements:
            raise ValueError("route placement IDs must be unique and non-empty")
        placements[placement_id] = placement
    if set(placement_ids) != set(placements):
        raise ValueError("MVP legal track must reference every placement exactly once; replicas or branches are unsupported")

    expected_start = 0
    seen_nodes: set[str] = set()
    manual_stages: list[dict[str, Any]] = []
    for placement_id in placement_ids:
        placement = placements.get(placement_id)
        if placement is None:
            raise ValueError(f"legal track references unknown placement {placement_id}")
        if placement.get("primary") is not True:
            raise ValueError("every tracked placement must be primary")
        node_id = placement.get("node_id")
        if not isinstance(node_id, str) or not node_id:
            raise ValueError("tracked placement node_id is invalid")
        if node_id in seen_nodes:
            raise ValueError(f"duplicate node in provisioning track: {node_id}")
        seen_nodes.add(node_id)
        layer_range = placement.get("layer_range")
        if not isinstance(layer_range, Mapping):
            raise ValueError(f"placement {placement_id} layer_range is invalid")
        start = layer_range.get("start")
        end = layer_range.get("end")
        if (
            isinstance(start, bool)
            or isinstance(end, bool)
            or not isinstance(start, int)
            or not isinstance(end, int)
            or start != expected_start
            or end <= start
        ):
            raise ValueError("product route contains a layer gap or overlap")
        manual_stages.append({
            "node_id": node_id,
            "range": {
                "start_layer": start,
                "end_layer_exclusive": end,
                "layer_count": end - start,
            },
        })
        expected_start = end
    num_layers = manifest.get("num_layers")
    if expected_start != num_layers:
        raise ValueError("product route contains a layer gap or overlap")

    manual = {
        "ok": True,
        "protocol": "mycelium.manual_provisioning_route.v1",
        "source_protocol": "mycelium.route_plan.v2",
        "model": {
            "model_id": manifest["model_id"],
            "num_layers": manifest["num_layers"],
            "manifest_digest": mm.manifest_digest_ref(dict(manifest)),
            "resolved_commit": manifest["resolved_commit"],
        },
        "route": manual_stages,
        "node_order": [stage["node_id"] for stage in manual_stages],
        "claim_boundary": (
            "deterministic single-track provisioning projection; runtime layers remain unloaded"
        ),
    }
    validate_manual_provisioning_route_v1(manual)
    return manual


def compile_bound_layer_assignments(
    *,
    route_plan: Mapping[str, Any] | RoutePlanV2,
    planner_snapshot: Mapping[str, Any],
    evidence_bundle: Mapping[str, Any],
    manifest: dict[str, Any],
    deployment_id: str,
    deployment_epoch: int,
    cache_roots: dict[str, str],
    runtime_by_node: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    route_wire = _route_wire(route_plan)
    bundle, snapshot_digest = _validate_lineage(
        route_plan=route_wire,
        planner_snapshot=planner_snapshot,
        evidence_bundle=evidence_bundle,
        manifest=manifest,
        deployment_id=deployment_id,
        deployment_epoch=deployment_epoch,
    )
    manual_route = product_route_to_manual_provisioning_route(
        route_plan=route_wire,
        manifest=manifest,
    )
    binding = {
        "protocol": CONTROL_PLANE_BINDING_PROTOCOL,
        "evidence_bundle_digest": bundle["evidence_bundle_digest"],
        "planner_snapshot_digest": snapshot_digest,
        "snapshot_generation": bundle["snapshot_generation"],
        "swarm_id": bundle["swarm_id"],
        "deployment_id": deployment_id,
        "deployment_epoch": deployment_epoch,
    }
    assignments = compile_layer_assignments(
        route_plan=manual_route,
        manifest=manifest,
        deployment_id=deployment_id,
        deployment_epoch=deployment_epoch,
        cache_roots=cache_roots,
        runtime_by_node=runtime_by_node,
        control_plane_binding=binding,
    )
    for assignment in assignments:
        if assignment.get("control_plane_binding") != binding:
            raise ValueError("assignment lost control-plane lineage")
        validate_assignment_identity(assignment)
    return assignments
