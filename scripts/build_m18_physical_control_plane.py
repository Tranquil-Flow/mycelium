#!/usr/bin/env python3
"""Replan signed physical evidence into an M18 replicated-route candidate."""

# ruff: noqa: E402 -- direct execution bootstraps the repository import root.
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from mycelium_layer_planner.gossip_adapter import (
    plan_signed_evidence,
    planner_snapshot_digest,
    planner_snapshot_from_signed_evidence,
)
from mycelium_layer_planner.serialization import route_plan_to_dict
from runtime_loader import canonical_json


WORKLOAD = {
    "preset": "interactive_chat_v1",
    "concurrency_points": [2],
    "user_scale": 2,
    "context_bucket": "interactive-4k",
}
POLICY = {
    "memory_reserve_fraction": 0,
    "replica_budget": 1,
    "minimum_replica_gain_fraction": 0.05,
    "replica_uncertainty_fraction": 0.1,
    "ttft_slo_ms": 1_000_000.0,
    "tpot_slo_ms": 1_000_000.0,
}


def _write(path: Path, document: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(canonical_json(document), encoding="utf-8")


def build(args: argparse.Namespace) -> dict[str, Any]:
    base = json.loads(args.base_control_plane.read_text(encoding="utf-8"))
    signed = base.get("signed_evidence_bundle")
    if not isinstance(signed, dict):
        raise RuntimeError("m18_signed_evidence_missing")
    trusted_digest = signed.get("verification_key", {}).get("verification_key_digest")
    captured = signed.get("statement", {}).get("captured_at_unix_ms")
    if not isinstance(trusted_digest, str) or type(captured) is not int:
        raise RuntimeError("m18_signed_evidence_invalid")
    admitted = tuple(args.admitted_node_id)
    if not admitted or len(admitted) != len(set(admitted)):
        raise RuntimeError("m18_admitted_nodes_invalid")

    common = {
        "expected_verification_key_digest": trusted_digest,
        "now_unix_ms": captured + 1,
        "model": base["model"],
        "workload": WORKLOAD,
        "policy": POLICY,
        "quantization": base["quantization"],
        "admitted_node_ids": admitted,
    }
    snapshot = planner_snapshot_from_signed_evidence(signed, **common)
    route = route_plan_to_dict(plan_signed_evidence(signed, **common))
    tracks = route.get("legal_tracks")
    metrics = route.get("metrics")
    replication = route.get("diagnostics", {}).get("replication")
    if (
        not isinstance(tracks, (list, tuple))
        or len(tracks) < 2
        or not isinstance(metrics, dict)
        or not isinstance(replication, dict)
        or replication.get("parallelism_label") != "data_parallel_request_routing"
        or replication.get("route_ready") is not False
    ):
        raise RuntimeError("m18_replica_plan_not_produced")
    primary = metrics.get("primary_request_capacity_rps")
    replicated = metrics.get("replicated_request_capacity_rps")
    gain = metrics.get("replica_capacity_gain_rps")
    if not all(
        isinstance(value, (int, float)) for value in (primary, replicated, gain)
    ):
        raise RuntimeError("m18_replica_gain_invalid")
    if primary <= 0 or replicated <= primary or gain <= 0:
        raise RuntimeError("m18_replica_gain_not_positive")

    document = {
        "protocol": "mycelium.m18_physical_candidate.v1",
        "signed_evidence_bundle": signed,
        "planner_snapshot": snapshot,
        "route_plan": route,
        "model": base["model"],
        "workload": WORKLOAD,
        "policy": POLICY,
        "quantization": base["quantization"],
        "ab_deltas": [],
        "exclusions": [],
        "admitted_node_ids": list(admitted),
        "replica_gain_evidence": {
            "primary_request_capacity_rps": primary,
            "replicated_request_capacity_rps": replicated,
            "replica_capacity_gain_rps": gain,
            "gain_fraction": gain / primary,
            "track_count": len(tracks),
            "claim_boundary": "planner model only; physical throughput gate remains separate",
            "route_ready": False,
        },
    }
    _write(args.output, document)
    return {
        "output": str(args.output),
        "planner_snapshot_digest": planner_snapshot_digest(snapshot),
        "track_count": len(tracks),
        "primary_request_capacity_rps": primary,
        "replicated_request_capacity_rps": replicated,
        "gain_fraction": gain / primary,
        "route_ready": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-control-plane", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--admitted-node-id",
        action="append",
        required=True,
        help="primary placement node; repeat in frozen primary order",
    )
    print(json.dumps(build(parser.parse_args()), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
