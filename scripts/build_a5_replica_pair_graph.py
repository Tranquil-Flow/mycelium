#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Build a legal A5 replica-pair layer-planner graph with the EXISTING planner.

Lane: mycelium-a5-lane-20260927 (base 4fea2b6d513a714614ce6cf9004e2d1ff02d0533).

This is a *shape* constructibility probe only.  It does NOT claim A5 closure,
physical qualification, or any benchmark threshold.  It exercises, in order:

  1. mycelium_layer_planner.planner.plan_snapshot        (real planner)
  2. mycelium_layer_planner.validation.validate_route_plan (via plan_snapshot)
  3. mycelium_qualification.physical_deployment.build_replicated_execution_graph
                          (the exact builder the live route uses for M18)
  4. scripts.qualify_a5_replica_track.derive_track_pair  (the A5 shape gate)

and prints the real payload of each step.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from mycelium_layer_planner.planner import plan_snapshot  # noqa: E402
from mycelium_layer_planner.serialization import route_plan_to_dict  # noqa: E402
from mycelium_qualification.physical_deployment import (  # noqa: E402
    build_replicated_execution_graph,
)
from scripts.qualify_a5_replica_track import derive_track_pair  # noqa: E402

RESOLVED_COMMIT = "4fea2b6d513a714614ce6cf9004e2d1ff02d0533"
MANIFEST_DIGEST = "sha256:" + "c" * 64
BACKEND = "numpy-stdlib"


def _node(node_id: str, ms_per_layer_token: float) -> dict[str, Any]:
    return {
        "node_id": node_id,
        "prefill_ms_per_layer_token": ms_per_layer_token,
        "decode_ms_per_layer_token": ms_per_layer_token,
        "fast_memory_bytes": 100_000_000,
        "total_memory_bytes": 200_000_000,
        "memory_bandwidth_Bps": 1_000_000_000,
        "spill_bandwidth_Bps": 1_000_000_000,
    }


def _snapshot(
    speeds: tuple[tuple[str, float], ...],
    *,
    num_layers: int,
    minimum_replica_gain_fraction: float,
    user_scale: float,
    admitted_count: int | None = None,
    weight_bytes: int = 200_000_000,
) -> dict[str, Any]:
    nodes = [_node(node_id, speed) for node_id, speed in speeds]
    links = [
        {
            "src": left["node_id"],
            "dst": right["node_id"],
            "rtt_ms": 1.0,
            "jitter_ms": 0.1,
            "bandwidth_Bps": 1_000_000_000,
        }
        for left in nodes
        for right in nodes
        if left["node_id"] != right["node_id"]
    ]
    return {
        "model": {
            "model_id": "org/a5-replica-pair-probe",
            "revision": "immutable-revision",
            "weight_digest": "sha256:" + "a" * 64,
            "architecture": "Decoder",
            "num_layers": num_layers,
            "hidden_size": 128,
            "dtype_bytes": 2,
            "kv_heads": 2,
            "head_dim": 32,
            "weight_bytes": weight_bytes,
        },
        "nodes": nodes,
        "links": links,
        "workload": {
            "preset": "interactive_chat_v1",
            "concurrency_points": [1, 4],
            "user_scale": user_scale,
        },
        "policy": {
            "memory_reserve_fraction": 0,
            "replica_budget": 1,
            "minimum_replica_gain_fraction": minimum_replica_gain_fraction,
            "ttft_slo_ms": 1_000_000.0,
            "tpot_slo_ms": 1_000_000.0,
        },
        "admitted_node_ids": [
            node_id
            for node_id, _ in (speeds if admitted_count is None else speeds[:admitted_count])
        ],
    }


def _grouped(placements: Any) -> dict[str, list[Any]]:
    groups: dict[str, list[Any]] = {}
    for placement in placements:
        groups.setdefault(placement.replica_group_id, []).append(placement)
    return groups


def search_plan(args: argparse.Namespace) -> tuple[Any, dict[str, Any]]:
    """Find the planner configuration whose output carries a replica pair.

    The primary order is frozen over EVERY admitted node
    (``select_topology_order`` cannot drop admitted nodes), so a replica can only
    land on an *eligible but non-admitted* spare node.  The sweep therefore
    admits a bounded subset and leaves one fast spare node unadmitted, behind a
    deliberately slow bottleneck node so a replica has real capacity to add.
    """

    profiles = (
        (
            ("n0-fast", 0.001),
            ("n1-fast", 0.001),
            ("n2-bottleneck", 1.0),
            ("n3-spare", 0.001),
        ),
        (
            ("n0-fast", 0.001),
            ("n1-fast", 0.001),
            ("n2-bottleneck", 0.25),
            ("n3-spare", 0.001),
        ),
        (
            ("n0-fast", 0.001),
            ("n1-fast", 0.001),
            ("n2-bottleneck", 0.05),
            ("n3-spare", 0.001),
        ),
    )
    attempts: list[dict[str, Any]] = []
    for profile_index, speeds in enumerate(profiles):
        for num_layers in (4, 6, 8):
            for gain_fraction in (0.05, 0.1, 0.0):
                for user_scale in (2.0, 8.0):
                    for weight_bytes in (100_000_000, 200_000_000):
                        admitted_count = 3
                        try:
                            snapshot = _snapshot(
                                speeds,
                                num_layers=num_layers,
                                minimum_replica_gain_fraction=gain_fraction,
                                user_scale=user_scale,
                                admitted_count=admitted_count,
                                weight_bytes=weight_bytes,
                            )
                            plan = plan_snapshot(snapshot)
                        except ValueError as exc:  # planner refused this configuration
                            attempts.append(
                                {
                                    "profile": profile_index,
                                    "num_layers": num_layers,
                                    "gain_fraction": gain_fraction,
                                    "user_scale": user_scale,
                                    "weight_bytes": weight_bytes,
                                    "rejected": str(exc),
                                }
                            )
                            continue
                        groups = _grouped(plan.placements)
                        multi = {
                            gid: members
                            for gid, members in groups.items()
                            if len(members) >= 2
                        }
                        record = {
                            "profile": profile_index,
                            "num_layers": num_layers,
                            "gain_fraction": gain_fraction,
                            "user_scale": user_scale,
                            "weight_bytes": weight_bytes,
                            "stage_count": len(groups),
                            "multi_placement_groups": {
                                gid: [m.placement_id for m in members]
                                for gid, members in multi.items()
                            },
                            "accepted_replica_nodes": list(
                                plan.diagnostics["accepted_replica_nodes"]
                            ),
                            "replica_capacity_gain_rps": plan.metrics[
                                "replica_capacity_gain_rps"
                            ],
                            "legal_track_count": len(plan.legal_tracks),
                        }
                        attempts.append(record)
                        if (
                            len(groups) >= 2
                            and len(multi) == 1
                            and len(next(iter(multi.values()))) == 2
                        ):
                            if not args.quiet_search:
                                print(
                                    "SEARCH_HIT "
                                    + json.dumps(record, sort_keys=True),
                                    flush=True,
                                )
                            return plan, {"attempts": attempts, "hit": record}
    return None, {"attempts": attempts, "hit": None}


def _wire_placements_asdict(plan: Any) -> list[dict[str, Any]]:
    """The shape scripts/build_m18_physical_control_plane.py actually writes."""

    return [dict(item) for item in route_plan_to_dict(plan)["placements"]]


def _wire_placements_documented(plan: Any) -> list[dict[str, Any]]:
    """The shape build_replicated_execution_graph/tests require: start/end."""

    return [
        {
            "placement_id": placement.placement_id,
            "replica_group_id": placement.replica_group_id,
            "node_id": placement.node_id,
            "layer_range": {
                "start": placement.layer_range.start,
                "end": placement.layer_range.end,
            },
            "primary": placement.primary,
        }
        for placement in plan.placements
    ]


def _assignments(plan: Any) -> dict[str, dict[str, Any]]:
    built: dict[str, dict[str, Any]] = {}
    for placement in plan.placements:
        layer_range = placement.layer_range
        built[placement.placement_id] = {
            "assignment_id": f"assignment-{placement.placement_id}",
            "node_id": placement.node_id,
            "deployment_id": "a5-lane-replica-pair",
            "deployment_epoch": 1,
            "model_id": plan.model.model_id,
            "resolved_commit": RESOLVED_COMMIT,
            "manifest_digest": MANIFEST_DIGEST,
            "range": {
                "start_layer": layer_range.start,
                "end_layer_exclusive": layer_range.end,
                "layer_count": layer_range.count,
            },
            "components": ["decoder"],
            "runtime": {
                "backend": BACKEND,
                "dtype": "float32",
                "model_config": {"n_embd": plan.model.hidden_size},
            },
        }
    return built


def _proofs(plan: Any) -> dict[str, dict[str, Any]]:
    return {
        placement.placement_id: {
            "protocol": "mycelium.layer_load_proof.v1",
            "assignment_id": f"assignment-{placement.placement_id}",
            "node_id": placement.node_id,
            "runtime": {"backend": BACKEND},
        }
        for placement in plan.placements
    }


def _route_document(plan: Any, placements: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "protocol": "mycelium.route_plan.v2",
        "placements": placements,
        "forward_edges": [
            {
                "src_placement_id": edge.src_placement_id,
                "dst_placement_id": edge.dst_placement_id,
            }
            for edge in plan.forward_edges
        ],
        "loopbacks": [
            {
                "src_placement_id": loop.src_placement_id,
                "dst_placement_id": loop.dst_placement_id,
            }
            for loop in plan.loopbacks
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--quiet-search", action="store_true")
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)

    plan, search = search_plan(args)
    print("STEP1_SEARCH " + json.dumps(search["hit"], sort_keys=True), flush=True)
    if plan is None:
        print("STEP1_RESULT no_planner_configuration_produced_a_replica_pair", flush=True)
        (args.output_dir / "search-attempts.json").write_text(
            json.dumps(search, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        return 1

    groups = _grouped(plan.placements)
    replica_stage_placements = [
        members for members in groups.values() if len(members) == 2
    ][0]
    print(
        "STEP2_PLAN "
        + json.dumps(
            {
                "protocol": plan.protocol,
                "handoff_state": plan.handoff_state,
                "stage_count": len(groups),
                "legal_track_count": len(plan.legal_tracks),
                "replica_group_id": replica_stage_placements[0].replica_group_id,
                "replica_pair": [
                    {
                        "placement_id": item.placement_id,
                        "node_id": item.node_id,
                        "primary": item.primary,
                        "layer_range": [
                            item.layer_range.start,
                            item.layer_range.end,
                        ],
                    }
                    for item in replica_stage_placements
                ],
                "track_placement_ids": [
                    list(track.placement_ids) for track in plan.legal_tracks
                ],
                "replica_capacity_gain_rps": plan.metrics[
                    "replica_capacity_gain_rps"
                ],
            },
            sort_keys=True,
        ),
        flush=True,
    )

    (args.output_dir / "route_plan.v2.json").write_text(
        json.dumps(route_plan_to_dict(plan), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    assignments = _assignments(plan)
    proofs = _proofs(plan)

    # --- Negative control A: the exact document build_m18_physical_control_plane
    # writes (route_plan_to_dict/asdict key names) fed to the shipped builder.
    try:
        build_replicated_execution_graph(
            assignments,
            proofs,
            _route_document(plan, _wire_placements_asdict(plan)),
        )
    except Exception as exc:  # noqa: BLE001 - probe of the shipped handoff
        print(
            "STEP3_ASDICT_HANDOFF "
            + json.dumps(
                {"raised": type(exc).__name__, "message": str(exc)}
            ),
            flush=True,
        )
    else:
        print("STEP3_ASDICT_HANDOFF built_successfully", flush=True)

    # --- The documented builder input shape.
    graph = build_replicated_execution_graph(
        assignments,
        proofs,
        _route_document(plan, _wire_placements_documented(plan)),
    )
    print(
        "STEP4_GRAPH "
        + json.dumps(
            {
                "protocol": graph.protocol,
                "stage_count": len(graph.stages),
                "placements_per_stage": [
                    [item.placement_id for item in stage.placements]
                    for stage in graph.stages
                ],
                "stage_ids": [stage.stage_id for stage in graph.stages],
                "edge_count": len(graph.edges),
                "loopback_count": len(graph.loopback_edges),
            },
            sort_keys=True,
        ),
        flush=True,
    )
    (args.output_dir / "replica_pair_execution_graph.json").write_text(
        json.dumps(asdict(graph), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    # --- The A5 shape gate.
    pair = derive_track_pair(graph)
    print(
        "STEP5_DERIVE_TRACK_PAIR "
        + json.dumps(
            {
                "incumbent_placement_ids": list(pair.incumbent_placement_ids),
                "replica_placement_ids": list(pair.replica_placement_ids),
                "replica_placement_id": pair.replica_placement_id,
                "replica_group_id": pair.replica_group_id,
                "replica_node_ids": sorted(pair.replica_node_ids),
                "replica_load_proof_digest": pair.replica_load_proof_digest,
            },
            sort_keys=True,
        ),
        flush=True,
    )

    # --- The next consumer in scripts/qualify_a5_replica_track.build().
    from mycelium_m16_runtime import build_live_m16_runtime  # noqa: E402

    coordinator = build_live_m16_runtime(graph)
    print(
        "STEP6_M16_COORDINATOR "
        + json.dumps(
            {
                "coordinator": type(coordinator).__name__,
                "status_keys": sorted(str(key) for key in coordinator.status())[:24]
                if hasattr(coordinator, "status")
                else "no_status",
            },
            sort_keys=True,
            default=str,
        ),
        flush=True,
    )

    # --- Negative control B: heterogeneous backends must fail the identity gate.
    mismatched = dict(proofs)
    replica_placement_id = pair.replica_placement_id
    mismatched[replica_placement_id] = {
        **proofs[replica_placement_id],
        "runtime": {"backend": "mlx"},
    }
    try:
        other = build_replicated_execution_graph(
            assignments,
            mismatched,
            _route_document(plan, _wire_placements_documented(plan)),
        )
        derive_track_pair(other)
    except ValueError as exc:
        print(
            "STEP7_HETEROGENEOUS_BACKEND_GATE "
            + json.dumps({"raised": "ValueError", "message": str(exc)}),
            flush=True,
        )
    else:
        print("STEP7_HETEROGENEOUS_BACKEND_GATE accepted", flush=True)

    print("A5_REPLICA_PAIR_SHAPE_CONSTRUCTIBLE", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
