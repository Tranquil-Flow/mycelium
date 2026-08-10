#!/usr/bin/env python3
"""Compile activation-plane Iroh observations into an M14 physical candidate."""
# ruff: noqa: E402 -- direct execution bootstraps the repository import root.
from __future__ import annotations

import argparse
import copy
import json
from pathlib import Path
import sys
import time
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from mycelium_layer_planner.gossip_adapter import planner_snapshot_digest
from mycelium_qualification.signing import generate_ed25519_signer
from mycelium_qualification.physical_deployment import (
    LocalModelSource,
    compile_local_model_manifest,
)
from mycelium_topology_evidence import (
    build_m14_topology_projection,
    complete_directed_observation_matrix,
    link_state_from_transport_observation,
    select_measured_topology,
)
from runtime_loader import canonical_json
from scripts.build_m13_physical_control_plane import _compile, _model


def _write(path: Path, document: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(canonical_json(document), encoding="utf-8")


def _load_object(path: Path, code: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(code) from exc
    if not isinstance(value, dict):
        raise RuntimeError(code)
    return value


def _load_observations(path: Path) -> list[dict[str, Any]]:
    value = _load_object(path, "m14_transport_observations_invalid")
    observations = value.get("observations")
    if value.get("protocol") != "mycelium.m14_transport_matrix_input.v1" or not isinstance(
        observations, list
    ):
        raise RuntimeError("m14_transport_observations_invalid")
    if not all(isinstance(item, dict) for item in observations):
        raise RuntimeError("m14_transport_observations_invalid")
    return observations


def build(args: argparse.Namespace) -> dict[str, Any]:
    if args.model_root is None:
        manifest = _load_object(args.manifest, "m14_manifest_invalid")
    else:
        manifest = compile_local_model_manifest(
            LocalModelSource(
                root=args.model_root,
                model_id=args.model_id,
                requested_revision="main",
                resolved_commit=args.resolved_commit,
            )
        )
    measurements = _load_object(args.measurements, "m14_measurements_invalid")
    observations = _load_observations(args.transport_observations)
    nodes = measurements.get("nodes")
    if not isinstance(nodes, list) or len(nodes) < 3:
        raise RuntimeError("m14_requires_three_nodes")
    node_ids = tuple(node.get("node_id") for node in nodes if isinstance(node, dict))
    if len(node_ids) != len(nodes) or not all(isinstance(node_id, str) for node_id in node_ids):
        raise RuntimeError("m14_node_inventory_invalid")
    endpoint_ids = {
        node["node_id"]: node.get("endpoint", {}).get("endpoint_id")
        for node in nodes
    }
    if not all(isinstance(value, str) and value for value in endpoint_ids.values()):
        raise RuntimeError("m14_endpoint_inventory_invalid")

    captured = int(time.time() * 1_000)
    matrix = complete_directed_observation_matrix(
        observations,
        node_ids=node_ids,
        endpoint_ids_by_node=endpoint_ids,
        now_unix_ms=captured,
    )
    decision = select_measured_topology(
        matrix,
        node_ids=node_ids,
        entry_node_id=args.entry_node_id,
    )
    compiled_measurements = copy.deepcopy(measurements)
    compiled_measurements["links"] = [
        {
            key: value
            for key, value in link_state_from_transport_observation(item).items()
            if key != "protocol"
        }
        for item in observations
    ]
    signer = generate_ed25519_signer(endpoint_id="m14-physical-topology-authority")
    signed, snapshot, route = _compile(
        manifest=manifest,
        measurements=compiled_measurements,
        deployment_id=args.deployment_id,
        captured_at_unix_ms=captured,
        signer=signer,
        authority_generation=1,
    )
    placements = {
        item["placement_id"]: item
        for item in route["placements"]
        if item.get("primary") is True
    }
    track = route["legal_tracks"][0]
    ordered = [placements[placement_id] for placement_id in track["placement_ids"]]
    route_order = [item["node_id"] for item in ordered]
    if route_order != decision["opened_order"]:
        raise RuntimeError("m14_planner_topology_derivation_mismatch")
    allocation = [
        {"node_id": item["node_id"], **item["layer_range"]}
        for item in ordered
    ]
    projection = build_m14_topology_projection(
        observations=observations,
        decision=decision,
        allocation=allocation,
        promotion=None,
        exclusions=args.exclusion,
    )
    document = {
        "protocol": "mycelium.m14_physical_candidate.v1",
        "signed_evidence_bundle": signed,
        "planner_snapshot": snapshot,
        "route_plan": route,
        "model": _model(manifest),
        "workload": snapshot["workload"],
        "policy": snapshot["policy"],
        "quantization": "int8-weight-only",
        "ab_deltas": [],
        "exclusions": list(args.exclusion),
        "transport_observations": observations,
        "topology_decision": decision,
        "topology_projection": projection,
    }
    _write(args.output, document)
    return {
        "output": str(args.output),
        "snapshot_digest": planner_snapshot_digest(snapshot),
        "selected_cycle": decision["selected_cycle"],
        "opened_order": route_order,
        "selected_cost_ms": decision["selected_cost_ms"],
        "allocation": allocation,
        "globally_exact": decision["globally_exact"],
        "explored_candidates": decision["explored_candidates"],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--manifest", type=Path)
    source.add_argument("--model-root", type=Path)
    parser.add_argument("--model-id", default="Qwen/Qwen2.5-0.5B-Instruct")
    parser.add_argument(
        "--resolved-commit",
        default="7ae557604adf67be50417f59c2c2f167def9a775",
    )
    parser.add_argument("--measurements", type=Path, required=True)
    parser.add_argument("--transport-observations", type=Path, required=True)
    parser.add_argument("--deployment-id", required=True)
    parser.add_argument("--entry-node-id", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--exclusion", action="append", default=[])
    print(json.dumps(build(parser.parse_args()), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
