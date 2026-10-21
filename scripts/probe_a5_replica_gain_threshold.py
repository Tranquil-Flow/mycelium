#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Probe the shipped-5% replica gain threshold for the A5 replica-pair shape.

Answers: with the policy the shipped M18 command uses
(``minimum_replica_gain_fraction: 0.05``), can the EXISTING planner accept a
replica and therefore emit a replicated stage range?  Prints the planner's own
candidate decisions so the rejection/accepted reason is real payload, not a
guess.  Shape probe only; no A5 closure or physical claim.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from mycelium_layer_planner.planner import plan_snapshot  # noqa: E402


def snapshot(
    speeds: tuple[tuple[str, float], ...],
    *,
    num_layers: int,
    gain_fraction: float,
    weight_bytes: int,
    user_scale: float,
) -> dict:
    nodes = [
        {
            "node_id": node_id,
            "prefill_ms_per_layer_token": speed,
            "decode_ms_per_layer_token": speed,
            "fast_memory_bytes": 100_000_000,
            "total_memory_bytes": 200_000_000,
            "memory_bandwidth_Bps": 1_000_000_000,
            "spill_bandwidth_Bps": 1_000_000_000,
        }
        for node_id, speed in speeds
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
        "links": [
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
        ],
        "workload": {
            "preset": "interactive_chat_v1",
            "concurrency_points": [1, 4],
            "user_scale": user_scale,
        },
        "policy": {
            "memory_reserve_fraction": 0,
            "replica_budget": 1,
            "minimum_replica_gain_fraction": gain_fraction,
            "ttft_slo_ms": 1_000_000.0,
            "tpot_slo_ms": 1_000_000.0,
        },
        "admitted_node_ids": [node_id for node_id, _ in speeds[:3]],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--attempts", type=Path)
    args = parser.parse_args()

    if args.attempts is not None:
        document = json.loads(args.attempts.read_text(encoding="utf-8"))
        shipped = [
            record
            for record in document["attempts"]
            if record.get("minimum_replica_gain_fraction") == 0.05
            and record.get("num_layers") == 4
        ]
        print(
            "FIRST_SWEEP_5PCT_NUMLAYERS4 "
            + json.dumps(shipped, sort_keys=True),
            flush=True,
        )

    for bottleneck in (0.05, 0.25, 1.0):
        for num_layers in (4, 6):
            for weight_bytes in (50_000_000, 100_000_000):
                speeds = (
                    ("n0-fast", 0.001),
                    ("n1-fast", 0.001),
                    ("n2-bottleneck", bottleneck),
                    ("n3-spare", 0.001),
                )
                try:
                    plan = plan_snapshot(
                        snapshot(
                            speeds,
                            num_layers=num_layers,
                            gain_fraction=0.05,
                            weight_bytes=weight_bytes,
                            user_scale=2.0,
                        )
                    )
                except ValueError as exc:
                    print(
                        "PROBE "
                        + json.dumps(
                            {
                                "bottleneck": bottleneck,
                                "num_layers": num_layers,
                                "weight_bytes": weight_bytes,
                                "rejected": str(exc),
                            },
                            sort_keys=True,
                        ),
                        flush=True,
                    )
                    continue
                decisions = plan.diagnostics["replication"]["candidate_decisions"]
                accepted = [
                    decision for decision in decisions if decision["accepted"]
                ]
                print(
                    "PROBE "
                    + json.dumps(
                        {
                            "bottleneck": bottleneck,
                            "num_layers": num_layers,
                            "weight_bytes": weight_bytes,
                            "accepted_replica_nodes": list(
                                plan.diagnostics["accepted_replica_nodes"]
                            ),
                            "replica_capacity_gain_rps": plan.metrics[
                                "replica_capacity_gain_rps"
                            ],
                            "legal_track_count": len(plan.legal_tracks),
                            "stage_count": len(
                                {
                                    placement.replica_group_id
                                    for placement in plan.placements
                                }
                            ),
                            "multi_placement_group_sizes": sorted(
                                [
                                    sum(
                                        1
                                        for other in plan.placements
                                        if other.replica_group_id
                                        == placement.replica_group_id
                                    )
                                    for placement in plan.placements
                                    if placement.primary
                                ]
                            ),
                            "accepted_decision": accepted[0] if accepted else None,
                            "top_rejections": [
                                {
                                    "placement_id": decision["placement_id"],
                                    "reason": decision["reason"],
                                    "robust_gain_rps": decision["robust_gain_rps"],
                                    "minimum_required_gain_rps": decision[
                                        "minimum_required_gain_rps"
                                    ],
                                }
                                for decision in decisions[:3]
                            ],
                        },
                        sort_keys=True,
                    ),
                    flush=True,
                )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
