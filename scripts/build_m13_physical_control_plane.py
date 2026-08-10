#!/usr/bin/env python3
"""Compile signed physical measurements into an M13 planner-v2 candidate."""
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

from mycelium_capacity_profiles import (
    CapacityObservation,
    CapacityProfileKey,
    CapacityProfilePolicy,
    compile_capacity_profile,
    placement_calibration_digest,
    status_with_capacity_profile,
)
from mycelium_gossip.registry import VersionedRecordStore
from mycelium_gossip.schema import RecordKind, build_record
from mycelium_gossip.service import GossipService
from mycelium_gossip.signed_bundle import seal_evidence_bundle
from mycelium_gossip.transport import (
    InMemoryMesh,
    InMemoryTransport,
    LivenessEvent,
    LivenessKind,
)
from mycelium_layer_planner.gossip_adapter import (
    plan_signed_evidence,
    planner_snapshot_digest,
    planner_snapshot_from_signed_evidence,
)
from mycelium_layer_planner.serialization import route_plan_to_dict
from mycelium_qualification.signing import generate_ed25519_signer
from runtime_loader import canonical_json


def _write(path: Path, document: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(canonical_json(document), encoding="utf-8")


def _model(manifest: dict[str, Any]) -> dict[str, Any]:
    config = manifest["runtime_model"]["model_config"]
    weight_bytes = sum(
        item["size_bytes"]
        for item in manifest["files"]
        if item["path"].endswith(".safetensors")
    )
    return {
        "model_id": manifest["model_id"],
        "revision": manifest["resolved_commit"],
        "weight_digest": "sha256:" + manifest["manifest_digest"]["value"],
        "architecture": manifest["architecture"],
        "num_layers": manifest["num_layers"],
        "hidden_size": config["n_embd"],
        "dtype_bytes": 4,
        "kv_heads": config["n_kv_head"],
        "head_dim": config["head_dim"],
        "weight_bytes": weight_bytes,
    }


def _memory_domains(node: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    total = node["total_memory_bytes"]
    fast = node["fast_allocatable_bytes"]
    total_allocatable = node["total_allocatable_bytes"]
    if not 0 < fast <= total_allocatable <= total:
        raise ValueError("physical measurement memory tiers are invalid")
    if fast == total_allocatable:
        profile = [{"memory_domain_id": "unified-0", "kind": "unified", "total_bytes": total}]
        status = [
            {
                "memory_domain_id": "unified-0",
                "kind": "unified",
                "total_bytes": total,
                "allocatable_after_reservations_bytes": total_allocatable,
                "committed_bytes": total - total_allocatable,
                "reclaimable_bytes": 0,
                "reservation_generation": 1,
            }
        ]
        return profile, status
    slow_total = total - fast
    slow_allocatable = total_allocatable - fast
    profile = [
        {"memory_domain_id": "accelerator-0", "kind": "accelerator", "total_bytes": fast},
        {"memory_domain_id": "system-0", "kind": "system", "total_bytes": slow_total},
    ]
    status = [
        {
            "memory_domain_id": "accelerator-0",
            "kind": "accelerator",
            "total_bytes": fast,
            "allocatable_after_reservations_bytes": fast,
            "committed_bytes": 0,
            "reclaimable_bytes": 0,
            "reservation_generation": 1,
        },
        {
            "memory_domain_id": "system-0",
            "kind": "system",
            "total_bytes": slow_total,
            "allocatable_after_reservations_bytes": slow_allocatable,
            "committed_bytes": slow_total - slow_allocatable,
            "reclaimable_bytes": 0,
            "reservation_generation": 1,
        },
    ]
    return profile, status


def _compile(
    *,
    manifest: dict[str, Any],
    measurements: dict[str, Any],
    deployment_id: str,
    captured_at_unix_ms: int,
    signer: Any,
    authority_generation: int,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    swarm_id = measurements["swarm_id"]
    def clock() -> float:
        return 100.0
    store = VersionedRecordStore(swarm_id, monotonic=clock)
    service = GossipService(
        swarm_id=swarm_id,
        node_id="placement-authority",
        incarnation=1,
        boot_id="placement-authority-boot-1",
        transport=InMemoryTransport(InMemoryMesh(monotonic=clock), "placement-authority"),
        registry=store,
        monotonic=clock,
    )
    model = _model(manifest)
    workload = {
        "preset": "interactive_chat_v1",
        "concurrency_points": [1],
        "user_scale": 1,
        "context_bucket": "interactive-4k",
    }
    policy = {
        "memory_reserve_fraction": 0,
        "replica_budget": 0,
        "ttft_slo_ms": 1_000_000.0,
        "tpot_slo_ms": 1_000_000.0,
    }
    profiles = {}
    nodes = measurements["nodes"]
    for sequence, node in enumerate(nodes, start=1):
        node_id = node["node_id"]
        profile_domains, status_domains = _memory_domains(node)
        endpoint = node["endpoint"]
        performance = copy.deepcopy(node["performance"])
        profile = compile_capacity_profile(
            CapacityProfileKey(
                model_digest=model["weight_digest"],
                source_evidence_digest=placement_calibration_digest(node_id, performance),
                quantization="int8-weight-only",
                backend=performance["backend"],
                runtime_build=performance["runtime_build"],
                hardware_class=performance["hardware_class"],
                power_mode=performance["power_mode"],
                context_bucket=workload["context_bucket"],
                kv_mode=performance["decode_mode"],
            ),
            (
                CapacityObservation(
                    concurrency=1,
                    sample_count=node["sample_count"],
                    p95_ttft_ms=node["p95_ttft_ms"],
                    p95_tpot_ms=node["p95_tpot_ms"],
                    aggregate_output_tps=node["aggregate_output_tps"],
                    peak_memory_bytes=node["peak_memory_bytes"],
                    memory_budget_bytes=node["total_allocatable_bytes"],
                ),
            ),
            CapacityProfilePolicy(
                ttft_p95_slo_ms=1_000_000.0,
                tpot_p95_slo_ms=1_000_000.0,
                min_samples=3,
            ),
        )
        profiles[node_id] = profile
        status = status_with_capacity_profile(
            {
                "protocol": "mycelium.device_status.v1",
                "node_id": node_id,
                "lifecycle": "ready",
                "memory_domains": status_domains,
                "queue_depth": 0,
                "in_flight": 0,
                "concurrency_limit": 1,
                "performance": performance,
            },
            profile,
            allow_concurrency_limit_update=True,
        )
        payloads = (
            (
                RecordKind.PROFILE,
                {
                    "protocol": "mycelium.device_profile.v2",
                    "node_id": node_id,
                    "software_version": measurements["software_version"],
                    "protocol_versions": ["mycelium.gossip.record.v1"],
                    "platform": node.get("platform", "darwin"),
                    "architecture": node.get("architecture", "arm64"),
                    "memory_domains": profile_domains,
                    "endpoints": [endpoint],
                    "policy": {"available_for_swarm": True},
                },
            ),
            (RecordKind.STATUS, status),
            (
                RecordKind.MEMBERSHIP,
                {
                    "protocol": "mycelium.membership.v1",
                    "subject_node_id": node_id,
                    "subject_incarnation": 1,
                    "state": "alive",
                    "reporter_node_id": node_id,
                    "reason": "signed_physical_measurement",
                },
            ),
        )
        for kind, payload in payloads:
            store.apply(
                build_record(
                    swarm_id=swarm_id,
                    kind=kind,
                    origin_node_id=node_id,
                    incarnation=1,
                    sequence=sequence,
                    boot_id=f"physical-{node_id}-boot-1",
                    generated_at_unix_ms=captured_at_unix_ms,
                    ttl_ms=7_200_000,
                    payload=payload,
                )
            )
        service.submit_liveness(
            LivenessEvent(
                LivenessKind.PUT,
                swarm_id,
                node_id,
                1,
                f"physical-{node_id}-boot-1",
                clock(),
            )
        )
    for sequence, link in enumerate(measurements["links"], start=100):
        store.apply(
            build_record(
                swarm_id=swarm_id,
                kind=RecordKind.LINK,
                origin_node_id=link["src_node_id"],
                incarnation=1,
                sequence=sequence,
                boot_id=f"physical-{link['src_node_id']}-boot-1",
                generated_at_unix_ms=captured_at_unix_ms,
                ttl_ms=7_200_000,
                payload={"protocol": "mycelium.link_state.v1", **link},
            )
        )
    service.drain()
    bundle = service.capture_evidence_bundle(
        deployment_id=deployment_id,
        deployment_epoch=1,
        model_id=manifest["model_id"],
        num_layers=manifest["num_layers"],
        manifest_digest=model["weight_digest"],
        resolved_commit=manifest["resolved_commit"],
    )
    signed = seal_evidence_bundle(
        bundle,
        signer=signer,
        captured_at_unix_ms=captured_at_unix_ms,
        valid_for_ms=7_200_000,
        authority_generation=authority_generation,
        capacity_profiles=profiles,
    )
    snapshot = planner_snapshot_from_signed_evidence(
        signed,
        expected_verification_key_digest=signer.verification_key_digest,
        now_unix_ms=captured_at_unix_ms + 1,
        model=model,
        workload=workload,
        policy=policy,
        quantization="int8-weight-only",
    )
    route = route_plan_to_dict(
        plan_signed_evidence(
            signed,
            expected_verification_key_digest=signer.verification_key_digest,
            now_unix_ms=captured_at_unix_ms + 1,
            model=model,
            workload=workload,
            policy=policy,
            quantization="int8-weight-only",
        )
    )
    return signed, snapshot, route


def _allocation(route: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {"node_id": item["node_id"], **item["layer_range"]}
        for item in route["placements"]
        if item["primary"] is True
    ]


def build(args: argparse.Namespace) -> dict[str, Any]:
    manifest = json.loads(args.manifest.read_text("utf-8"))
    measurements = json.loads(args.measurements.read_text("utf-8"))
    captured = int(time.time() * 1_000)
    signer = generate_ed25519_signer(endpoint_id="m13-physical-placement-authority")
    signed, snapshot, route = _compile(
        manifest=manifest,
        measurements=measurements,
        deployment_id=args.deployment_id,
        captured_at_unix_ms=captured,
        signer=signer,
        authority_generation=1,
    )

    compute_measurements = copy.deepcopy(measurements)
    compute_target = compute_measurements["nodes"][0]
    for field in ("prefill_ms_per_layer_token", "decode_ms_per_layer_token"):
        compute_target["performance"][field] *= 4.0
    _, compute_snapshot, compute_route = _compile(
        manifest=manifest,
        measurements=compute_measurements,
        deployment_id=args.deployment_id,
        captured_at_unix_ms=captured + 10,
        signer=signer,
        authority_generation=2,
    )

    memory_measurements = copy.deepcopy(measurements)
    memory_target = memory_measurements["nodes"][0]
    memory_target["fast_allocatable_bytes"] = args.memory_ab_fast_bytes
    _, memory_snapshot, memory_route = _compile(
        manifest=manifest,
        measurements=memory_measurements,
        deployment_id=args.deployment_id,
        captured_at_unix_ms=captured + 20,
        signer=signer,
        authority_generation=3,
    )
    baseline_allocation = _allocation(route)
    ab_deltas = [
        {
            "kind": "compute_only",
            "changed_input": "node-0 prefill/decode coefficient x4",
            "baseline_snapshot_digest": planner_snapshot_digest(snapshot),
            "candidate_snapshot_digest": planner_snapshot_digest(compute_snapshot),
            "allocation_before": baseline_allocation,
            "allocation_after": _allocation(compute_route),
        },
        {
            "kind": "memory_only",
            "changed_input": f"node-0 fast tier={args.memory_ab_fast_bytes}",
            "baseline_snapshot_digest": planner_snapshot_digest(snapshot),
            "candidate_snapshot_digest": planner_snapshot_digest(memory_snapshot),
            "allocation_before": baseline_allocation,
            "allocation_after": _allocation(memory_route),
        },
    ]
    document = {
        "protocol": "mycelium.m13_physical_candidate.v1",
        "signed_evidence_bundle": signed,
        "planner_snapshot": snapshot,
        "route_plan": route,
        "model": _model(manifest),
        "workload": snapshot["workload"],
        "policy": snapshot["policy"],
        "quantization": "int8-weight-only",
        "ab_deltas": ab_deltas,
        "exclusions": [],
    }
    _write(args.output, document)
    _write(args.output.with_name("compute-only-ab.json"), {
        "protocol": "mycelium.m13_physical_ab.v1",
        "kind": "compute_only",
        "planner_snapshot": compute_snapshot,
        "route_plan": compute_route,
    })
    _write(args.output.with_name("memory-only-ab.json"), {
        "protocol": "mycelium.m13_physical_ab.v1",
        "kind": "memory_only",
        "planner_snapshot": memory_snapshot,
        "route_plan": memory_route,
    })
    return {
        "output": str(args.output),
        "snapshot_digest": planner_snapshot_digest(snapshot),
        "allocation": baseline_allocation,
        "compute_ab_allocation": _allocation(compute_route),
        "memory_ab_allocation": _allocation(memory_route),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--measurements", type=Path, required=True)
    parser.add_argument("--deployment-id", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--memory-ab-fast-bytes", type=int, default=450_000_000)
    print(json.dumps(build(parser.parse_args()), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
