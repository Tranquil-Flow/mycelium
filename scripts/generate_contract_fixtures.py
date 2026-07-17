#!/usr/bin/env python3
"""Generate deterministic executable compatibility fixtures for Mycelium contracts."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import model_manifest as mm
from layer_assignment import compile_layer_assignments
from mycelium_gossip.evidence_bundle import build_evidence_bundle, evidence_bundle_to_dict
from mycelium_gossip.registry import VersionedRecordStore
from mycelium_gossip.schema import RecordKind, build_record
from mycelium_gossip.service import PeerHealthState, PeerState
from mycelium_gossip.views import (
    allocator_view_to_dict,
    build_allocator_view,
    build_router_view,
    router_view_to_dict,
)
from mycelium_layer_planner.gossip_adapter import (
    plan_evidence_bundle,
    planner_snapshot_from_evidence_bundle,
)
from mycelium_layer_planner.planner import plan_snapshot
from mycelium_layer_planner.serialization import route_plan_to_dict
from planner_assignment import compile_bound_layer_assignments, validate_control_plane_tranche
from route_contract import validate_manual_provisioning_route_v1
from weight_provisioning import artifact_report_errors, audit_provisioning

if __package__:
    from .contract_io import atomic_write_under_root, read_under_root
    from .contract_registry import EXPECTED_FIXTURE_NAMES, SPECS_BY_FIXTURE
else:
    from contract_io import atomic_write_under_root, read_under_root
    from contract_registry import EXPECTED_FIXTURE_NAMES, SPECS_BY_FIXTURE

FIXTURE_DIR = ROOT / "contracts" / "compatibility-fixtures"


def canonical_bytes(document: dict[str, Any]) -> bytes:
    return (json.dumps(document, sort_keys=True, indent=2, ensure_ascii=False) + "\n").encode("utf-8")


def planner_snapshot() -> dict[str, Any]:
    nodes = [
        {
            "node_id": f"node-{suffix}",
            "prefill_ms_per_layer_token": 0.001,
            "decode_ms_per_layer_token": 0.001,
            "fast_memory_bytes": 100_000_000,
            "total_memory_bytes": 200_000_000,
            "memory_bandwidth_Bps": 1_000_000_000,
            "spill_bandwidth_Bps": 1_000_000_000,
        }
        for suffix in ("a", "b")
    ]
    links = [
        {"src": src["node_id"], "dst": dst["node_id"], "rtt_ms": 1.0, "jitter_ms": 0.1, "bandwidth_Bps": 100_000_000}
        for src in nodes
        for dst in nodes
        if src is not dst
    ]
    return {
        "model": {
            "model_id": "org/model",
            "revision": "immutable-revision",
            "weight_digest": "sha256:" + "a" * 64,
            "architecture": "Decoder",
            "num_layers": 4,
            "hidden_size": 128,
            "dtype_bytes": 2,
            "kv_heads": 2,
            "head_dim": 32,
            "weight_bytes": 240_000_000,
        },
        "nodes": nodes,
        "links": links,
        "workload": {"preset": "interactive_chat_v1", "concurrency_points": [1, 4], "user_scale": 2},
        "policy": {"memory_reserve_fraction": 0, "replica_budget": 1, "ttft_slo_ms": 1_000_000, "tpot_slo_ms": 1_000_000},
    }


def manual_route() -> dict[str, Any]:
    manifest = model_manifest()
    route = {
        "ok": True,
        "protocol": "mycelium.manual_provisioning_route.v1",
        "model": {
            "model_id": manifest["model_id"],
            "num_layers": manifest["num_layers"],
            "manifest_digest": mm.manifest_digest_ref(manifest),
            "resolved_commit": manifest["resolved_commit"],
        },
        "route": [
            {"node_id": "node-a", "range": {"start_layer": 0, "end_layer_exclusive": 2, "layer_count": 2}},
            {"node_id": "node-b", "range": {"start_layer": 2, "end_layer_exclusive": 4, "layer_count": 2}},
        ],
        "node_order": ["node-a", "node-b"],
        "claim_boundary": "manual provisioning order only; not product Planner output",
    }
    validate_manual_provisioning_route_v1(route)
    return route


def model_manifest() -> dict[str, Any]:
    return mm.compile_model_manifest(
        model_id="org/model",
        requested_revision="main",
        resolved_commit="a" * 40,
        config={
            "model_type": "gpt2",
            "architectures": ["GPT2LMHeadModel"],
            "n_layer": 4,
            "tie_word_embeddings": False,
        },
        checkpoint_index={
            "metadata": {"total_size": 60},
            "weight_map": {
                "transformer.wte.weight": "shard-1.safetensors",
                "transformer.wpe.weight": "shard-1.safetensors",
                "transformer.h.0.attn.weight": "shard-2.safetensors",
                "transformer.h.1.attn.weight": "shard-2.safetensors",
                "transformer.h.2.attn.weight": "shard-3.safetensors",
                "transformer.h.3.attn.weight": "shard-3.safetensors",
                "transformer.ln_f.weight": "shard-3.safetensors",
                "lm_head.weight": "shard-3.safetensors",
            },
        },
        file_metadata={
            "shard-1.safetensors": {"size_bytes": 10, "sha256": "1" * 64},
            "shard-2.safetensors": {"size_bytes": 20, "sha256": "2" * 64},
            "shard-3.safetensors": {"size_bytes": 30, "sha256": "3" * 64},
        },
    )


def assignments_and_reports() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    assignments = compile_layer_assignments(
        route_plan=manual_route(),
        manifest=model_manifest(),
        deployment_id="12345678-1234-5678-1234-567812345678",
        deployment_epoch=1,
        cache_roots={"node-a": "/var/lib/mycelium/node-a", "node-b": "/var/lib/mycelium/node-b"},
        runtime_by_node={
            "node-a": {"backend": "mlx", "dtype": "float16", "quantization": "none"},
            "node-b": {"backend": "mlx", "dtype": "float16", "quantization": "none"},
        },
    )
    reports = []
    for assignment in assignments:
        expected_bytes = sum(item["size_bytes"] for item in assignment["files"])
        report = {
            "protocol": "mycelium.artifact_verification_report.v1",
            "deployment_id": assignment["deployment_id"],
            "deployment_epoch": assignment["deployment_epoch"],
            "assignment_id": assignment["assignment_id"],
            "node_id": assignment["node_id"],
            "manifest_digest": assignment["manifest_digest"],
            "resolved_commit": assignment["resolved_commit"],
            "range": assignment["range"],
            "artifact_cache_root": assignment["artifact_cache_root"],
            "verified_files": assignment["files"],
            "verified_tensor_prefixes": assignment["expected_tensor_prefixes"],
            "verified_tensor_count": len(set(assignment["expected_tensor_keys"])),
            "expected_bytes": expected_bytes,
            "network_download_bytes": 0,
            "cache_hit_bytes": expected_bytes,
            "ready_for_load": True,
            "route_ready": False,
            "claim_boundary": "fixture proves artifact identity only; runtime layers are not loaded",
            "timestamp": "2026-07-17T00:00:00+00:00",
        }
        errors = artifact_report_errors(assignment, report)
        if errors:
            raise ValueError("invalid generated artifact report: " + "; ".join(errors))
        reports.append(report)
    return assignments, reports


def provisioning_audit() -> dict[str, Any]:
    assignments, reports = assignments_and_reports()
    audit = audit_provisioning(manual_route(), assignments, reports)
    audit["timestamp"] = "2026-07-17T00:00:00+00:00"
    if not audit["all_assignments_verified"]:
        raise ValueError("invalid generated provisioning audit: " + "; ".join(audit["errors"]))
    return audit


def gossip_documents() -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    store = VersionedRecordStore("swarm-a", monotonic=lambda: 100.0)

    def add(kind: RecordKind, node_id: str, payload: dict[str, Any]) -> None:
        result = store.apply(
            build_record(
                swarm_id="swarm-a",
                kind=kind,
                origin_node_id=node_id,
                incarnation=1,
                sequence=1,
                boot_id=f"boot-{node_id}-1",
                generated_at_unix_ms=100,
                ttl_ms=10_000,
                payload=payload,
            )
        )
        if result.status.value != "accepted":
            raise ValueError(f"fixture record rejected: {kind.value}: {result.reason}")

    for index, node_id in enumerate(("node-a", "node-b"), start=1):
        endpoint = {
            "endpoint_id": "http-overlay",
            "transport": "http",
            "host": f"100.64.0.{index}",
            "port": 9000,
            "scope": "overlay",
            "inbound": True,
        }
        add(
            RecordKind.PROFILE,
            node_id,
            {
                "protocol": "mycelium.device_profile.v2",
                "node_id": node_id,
                "software_version": "0.1.0",
                "protocol_versions": ["mycelium.gossip.record.v1"],
                "platform": "darwin",
                "architecture": "arm64",
                "memory_domains": [
                    {"memory_domain_id": "unified-0", "kind": "unified", "total_bytes": 48 * 1024**3}
                ],
                "endpoints": [endpoint],
                "policy": {"available_for_swarm": True},
            },
        )
        add(
            RecordKind.STATUS,
            node_id,
            {
                "protocol": "mycelium.device_status.v1",
                "node_id": node_id,
                "lifecycle": "ready",
                "memory_domains": [
                    {
                        "memory_domain_id": "unified-0",
                        "kind": "unified",
                        "total_bytes": 48 * 1024**3,
                        "allocatable_after_reservations_bytes": 32 * 1024**3,
                        "committed_bytes": 8 * 1024**3,
                        "reclaimable_bytes": 2 * 1024**3,
                        "reservation_generation": 1,
                    }
                ],
                "queue_depth": 0,
                "in_flight": 0,
                "concurrency_limit": 2,
                "performance": {
                    "prefill_ms_per_layer_token": 0.001,
                    "decode_ms_per_layer_token": 0.001,
                    "memory_bandwidth_Bps": 1_000_000_000,
                    "spill_bandwidth_Bps": 1_000_000_000,
                    "calibration_confidence": 1.0,
                },
            },
        )
        add(
            RecordKind.OFFERING,
            node_id,
            {
                "protocol": "mycelium.runtime_offering.v1",
                "deployment_id": "deploy-1",
                "deployment_epoch": 1,
                "assignment_id": f"assignment-{node_id}",
                "manifest_digest": "sha256:" + "c" * 64,
                "resolved_commit": "a" * 40,
                "model_id": "model-a",
                "start_layer": 0 if node_id == "node-a" else 2,
                "end_layer_exclusive": 2 if node_id == "node-a" else 4,
                "runtime_instance_id": f"runtime-{node_id}-1",
                "load_generation": 1,
                "readiness_state": "loaded_and_probed",
                "proof_digest": "sha256:" + "d" * 64,
                "inference_endpoint_id": "http-overlay",
            },
        )

    for src_node_id, dst_node_id in (
        ("node-a", "node-b"),
        ("node-b", "node-a"),
    ):
        add(
            RecordKind.LINK,
            src_node_id,
            {
                "protocol": "mycelium.link_state.v1",
                "src_node_id": src_node_id,
                "dst_node_id": dst_node_id,
                "src_endpoint_id": "http-overlay",
                "dst_endpoint_id": "http-overlay",
                "reachable": True,
                "connect_rtt_ema_ms": 1.2,
                "rtt_p95_ms": 1.8,
                "jitter_ms": 0.1,
                "loss_ratio": 0.0,
                "goodput_mbps": 900.0,
                "sample_count": 16,
                "measurement_method": "active_probe",
            },
        )

    peer_states = tuple(
        PeerState(
            node_id=node_id,
            incarnation=1,
            boot_id=f"boot-{node_id}-1",
            state=PeerHealthState.ALIVE,
            liveness_present=True,
            changed_at_monotonic=100.0,
        )
        for node_id in ("node-a", "node-b")
    )
    snapshot = store.snapshot()
    router = build_router_view(snapshot, peer_states, ())
    allocator = build_allocator_view(snapshot, peer_states, ())
    manifest = model_manifest()
    bundle = build_evidence_bundle(
        snapshot=snapshot,
        peer_states=peer_states,
        quarantines=(),
        deployment_id="12345678-1234-5678-1234-567812345678",
        deployment_epoch=1,
        model_id=manifest["model_id"],
        num_layers=manifest["num_layers"],
        manifest_digest=mm.manifest_digest_ref(manifest),
        resolved_commit=manifest["resolved_commit"],
    )
    return (
        router_view_to_dict(router),
        allocator_view_to_dict(allocator),
        evidence_bundle_to_dict(bundle),
    )


def control_plane_documents(
    evidence_bundle: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    manifest = model_manifest()
    model = {
        "model_id": manifest["model_id"],
        "revision": manifest["resolved_commit"],
        "weight_digest": mm.manifest_digest_ref(manifest),
        "architecture": "Decoder",
        "num_layers": manifest["num_layers"],
        "hidden_size": 128,
        "dtype_bytes": 2,
        "kv_heads": 2,
        "head_dim": 32,
        "weight_bytes": 50 * 1024**3,
    }
    workload = {
        "preset": "interactive_chat_v1",
        "concurrency_points": [1, 4],
        "user_scale": 2,
    }
    policy = {
        "memory_reserve_fraction": 0,
        "replica_budget": 0,
        "ttft_slo_ms": 1_000_000,
        "tpot_slo_ms": 1_000_000,
    }
    snapshot = planner_snapshot_from_evidence_bundle(
        evidence_bundle,
        model=model,
        workload=workload,
        policy=policy,
    )
    product_route = route_plan_to_dict(
        plan_evidence_bundle(
            evidence_bundle,
            model=model,
            workload=workload,
            policy=policy,
        )
    )
    route_wire = json.loads(json.dumps(product_route))
    nodes = [placement["node_id"] for placement in route_wire["placements"]]
    assignments = compile_bound_layer_assignments(
        route_plan=route_wire,
        planner_snapshot=snapshot,
        evidence_bundle=evidence_bundle,
        manifest=manifest,
        deployment_id=evidence_bundle["deployment"]["deployment_id"],
        deployment_epoch=evidence_bundle["deployment"]["deployment_epoch"],
        cache_roots={node: f"/var/lib/mycelium/{node}" for node in nodes},
        runtime_by_node={
            node: {"backend": "mlx", "dtype": "float16", "quantization": "none"}
            for node in nodes
        },
    )
    tranche = {
        "protocol": "mycelium.control_plane_tranche.v1",
        "evidence_bundle": evidence_bundle,
        "planner_snapshot": snapshot,
        "route_plan": route_wire,
        "assignments": assignments,
        "claim_boundary": (
            "atomic control-plane compatibility evidence only; assigned runtime layers remain unloaded"
        ),
    }
    validate_control_plane_tranche(tranche, manifest=manifest)
    return snapshot, route_wire, tranche


def documents() -> dict[str, dict[str, Any]]:
    assignments, reports = assignments_and_reports()
    router, allocator, evidence_bundle = gossip_documents()
    planner_evidence_snapshot, _, control_plane_tranche = control_plane_documents(evidence_bundle)
    generated = {
        "route-plan-v2.json": route_plan_to_dict(plan_snapshot(planner_snapshot())),
        "manual-provisioning-route-v1.json": manual_route(),
        "layer-assignment-v2.json": assignments[0],
        "artifact-verification-report-v1.json": reports[0],
        "provisioning-audit-v1.json": provisioning_audit(),
        "gossip-router-view-v1.json": router,
        "gossip-allocator-view-v1.json": allocator,
        "gossip-evidence-bundle-v1.json": evidence_bundle,
        "layer-planner-snapshot-v1.json": planner_evidence_snapshot,
        "control-plane-tranche-v1.json": control_plane_tranche,
    }
    if set(generated) != EXPECTED_FIXTURE_NAMES:
        raise ValueError("generated fixture set differs from authoritative contract registry")
    for name, document in generated.items():
        expected_protocol = SPECS_BY_FIXTURE[name].protocol
        if document.get("protocol") != expected_protocol:
            raise ValueError(
                f"generated protocol mismatch for {name}: expected {expected_protocol!r}, "
                f"got {document.get('protocol')!r}"
            )
    return generated


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true", help="fail if checked-in fixtures differ")
    args = parser.parse_args()
    expected = documents()
    FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
    present = {path.name for path in FIXTURE_DIR.iterdir() if path.name.endswith(".json")}
    unexpected = sorted(present - EXPECTED_FIXTURE_NAMES)
    if unexpected:
        print("unexpected fixture: " + ", ".join(unexpected), file=sys.stderr)
        return 1

    drift: list[str] = []
    for name, document in sorted(expected.items()):
        path = FIXTURE_DIR / name
        content = canonical_bytes(document)
        if args.check:
            try:
                actual = read_under_root(ROOT, path)
            except ValueError:
                actual = None
            if actual != content:
                drift.append(name)
        else:
            atomic_write_under_root(ROOT, path, content)
    if drift:
        print("fixture drift: " + ", ".join(drift), file=sys.stderr)
        return 1
    print(f"contract fixtures {'verified' if args.check else 'generated'}: {len(expected)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
