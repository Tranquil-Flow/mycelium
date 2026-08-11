#!/usr/bin/env python3
"""Bind an M18 physical throughput gate into plan and runtime projections."""

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

from mycelium_layer_planner.serialization import route_plan_from_dict
from mycelium_m18_replication import ReplicaRuntimeLedger, build_replica_plan
from mycelium_qualification.evidence import canonical_json_bytes, sha256_document


def _read(path: Path) -> dict[str, Any]:
    document = json.loads(path.read_text("utf-8"))
    if not isinstance(document, dict):
        raise ValueError("m18_evidence_document_invalid")
    return document


def _write(path: Path, document: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(canonical_json_bytes(document))


def build(args: argparse.Namespace) -> dict[str, Any]:
    control = _read(args.control_plane)
    gate = _read(args.physical_gate)
    if (
        control.get("protocol") != "mycelium.m18_physical_candidate.v1"
        or gate.get("protocol") != "mycelium.m18_physical_throughput_gate.v1"
        or gate.get("route_ready") is not True
        or gate.get("gain", {}).get("passed") is not True
    ):
        raise ValueError("m18_physical_gate_not_promotable")
    route_plan = route_plan_from_dict(control["route_plan"])
    primary = gate["primary_qualification"]
    captured_at = gate["captured_at_unix_ms"]
    gate_digest = sha256_document(gate)
    plan = build_replica_plan(
        route_plan,
        deployment_binding={
            "deployment_id": gate["deployment_id"],
            "deployment_epoch": primary["deployment_epoch"],
            "model_id": gate["model_id"],
            "model_revision": gate["resolved_commit"],
            "representation_digest": route_plan.model.weight_digest,
            "manifest_digest": primary["manifest_digest"],
            "qualification_id": primary["qualification_id"],
            "qualification_digest": sha256_document(primary),
            "decode_mode": "stage_local_kv",
            "quantization": "int8-weight-only",
        },
        evidence_binding={
            "generation": 18,
            "evidence_digest": gate_digest,
            "evaluated_at_unix_ms": captured_at,
            "valid_until_unix_ms": captured_at + 86_400_000,
        },
        generated_at_unix_ms=captured_at,
    )
    track_by_placements = {
        tuple(track["placement_ids"]): track for track in plan["tracks"]
    }
    qualified_tracks: dict[str, dict[str, str]] = {}
    for item in gate["track_qualifications"]:
        track = track_by_placements[tuple(item["placement_ids"])]
        qualified_tracks[track["track_id"]] = {
            "qualification_id": item["qualification"]["qualification_id"],
            "qualification_digest": item["qualification_digest"],
        }
    if set(qualified_tracks) != {track["track_id"] for track in plan["tracks"]}:
        raise ValueError("m18_track_qualification_incomplete")

    throughput = {
        "evidence_digest": gate_digest,
        "mode": gate["replicated"]["mode"],
        "baseline_request_count": gate["baseline"]["request_count"],
        "baseline_throughput_rps": gate["baseline"]["throughput_rps"],
        "replicated_request_count": gate["replicated"]["request_count"],
        "replicated_throughput_rps": gate["replicated"]["throughput_rps"],
        "gain_fraction": gate["gain"]["measured_fraction"],
        "minimum_required_fraction": gate["gain"]["minimum_required_fraction"],
        "passed": gate["gain"]["passed"],
    }
    physical_requests = [
        *gate["concurrency"]["requests"],
        *gate["saturation"]["requests"],
    ]

    def populate(ledger: ReplicaRuntimeLedger) -> None:
        for request in physical_requests:
            track = track_by_placements[tuple(request["placement_ids"])]
            selected = ledger.admit(
                request["request_id"],
                path_id=request["path_id"],
                requested_track_id=track["track_id"],
            )
            if selected != track["track_id"]:
                raise ValueError("m18_runtime_track_binding_changed")
            ledger.mark_phase(request["request_id"], "decode")
            for placement_id in request["placement_ids"]:
                ledger.record_placement_work(
                    request["request_id"],
                    placement_id,
                    frames_sent=0,
                    frames_received=0,
                    work_items=request["output_token_count"],
                )
            ledger.complete(request["request_id"])

    ledger = ReplicaRuntimeLedger(
        plan,
        qualified_tracks=qualified_tracks,
        throughput_evidence=throughput,
    )
    populate(ledger)
    runtime = ledger.status()

    replica_track = next(
        track
        for track in plan["tracks"]
        if any(
            not placement["primary"]
            for placement in plan["placements"]
            if placement["placement_id"] in track["placement_ids"]
        )
    )
    degradation = ReplicaRuntimeLedger(
        plan,
        qualified_tracks=qualified_tracks,
        throughput_evidence=throughput,
    )
    populate(degradation)
    degradation.admit(
        "m18-replica-removal-probe",
        path_id="m18-replica-removal-path",
        requested_track_id=replica_track["track_id"],
    )
    degradation.remove_track(
        replica_track["track_id"], reason="replica_removed_after_physical_gate"
    )
    degradation_runtime = degradation.status()

    _write(args.plan_output, plan)
    _write(args.runtime_output, runtime)
    _write(args.degradation_output, degradation_runtime)
    return {
        "plan_output": str(args.plan_output),
        "runtime_output": str(args.runtime_output),
        "degradation_output": str(args.degradation_output),
        "plan_digest": plan["plan_digest"],
        "qualified_track_count": len(qualified_tracks),
        "physical_request_count": len(physical_requests),
        "measured_gain_fraction": throughput["gain_fraction"],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--control-plane", type=Path, required=True)
    parser.add_argument("--physical-gate", type=Path, required=True)
    parser.add_argument("--plan-output", type=Path, required=True)
    parser.add_argument("--runtime-output", type=Path, required=True)
    parser.add_argument("--degradation-output", type=Path, required=True)
    print(json.dumps(build(parser.parse_args()), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
