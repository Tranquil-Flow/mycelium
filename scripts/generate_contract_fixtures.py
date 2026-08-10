#!/usr/bin/env python3
"""Generate deterministic executable compatibility fixtures for Mycelium contracts."""
# ruff: noqa: E402 -- imports follow repository-root bootstrap for direct CLI execution.
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import model_manifest as mm
from layer_assignment import compile_layer_assignments
from mycelium_capacity_profiles import (
    CapacityObservation,
    CapacityProfileKey,
    CapacityProfilePolicy,
    compile_capacity_profile,
)
from mycelium_assignment_cache import (
    validate_cache_status,
    validate_materialization_report,
)
from mycelium_candidate_promotion import evaluate_candidate_promotion
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
from mycelium_gossip.signed_bundle import seal_evidence_bundle
from mycelium_layer_planner.gossip_adapter import (
    plan_evidence_bundle,
    planner_snapshot_from_evidence_bundle,
)
from mycelium_layer_planner.planner import plan_snapshot
from mycelium_layer_planner.public_projection import validate_m13_placement_projection
from mycelium_layer_planner.replan_simulator import simulate_bundle
from mycelium_layer_planner.serialization import route_plan_to_dict
from mycelium_membership import (
    ASSIGNMENT_OFFER_PROTOCOL,
    ASSIGNMENT_RESULT_PROTOCOL,
    CAPABILITY_REPORT_PROTOCOL,
    DRAIN_ACK_PROTOCOL,
    HEARTBEAT_PROTOCOL,
    JOIN_ACCEPTANCE_PROTOCOL,
    JOIN_REQUEST_PROTOCOL,
    LEASE_RENEWAL_PROTOCOL,
    LINK_PROBE_REPORT_PROTOCOL,
    RESUME_ACCEPTANCE_PROTOCOL,
    RESUME_REQUEST_PROTOCOL,
    SEED_ROTATION_ACK_PROTOCOL,
    sign_membership_message,
    validate_membership_message,
)
from mycelium_performance_budget import validate_performance_budget
from mycelium_product_spine import (
    ENTITY_KINDS,
    validate_product_event,
    validate_product_snapshot,
)
from mycelium_qualification.signing import Ed25519EvidenceSigner
from mycelium_qualification.contracts import (
    route_qualification_to_dict,
    synthetic_route_qualification_fixture,
)
from mycelium_request_gateway.contracts import (
    InferenceSubmission,
    QualificationBinding,
    StreamEvent,
)
from mycelium_topology_evidence import (
    build_m14_topology_projection,
    complete_directed_observation_matrix,
    select_measured_topology,
    validate_transport_path_observation,
)
from mycelium_seed.operator import (
    SEED_BACKUP_PROTOCOL,
    SEED_KEY_TRANSITION_PROTOCOL,
    SEED_OPERATOR_INVENTORY_PROTOCOL,
    SEED_OPERATOR_REVOCATION_PROTOCOL,
    SEED_OPERATOR_ROTATION_PROTOCOL,
)
from mycelium_router.contracts import PathHop, PathManifest
from mycelium_router.layer_builder import build_execution_graph
from mycelium_router.serialization import execution_graph_to_dict, path_manifest_to_dict
from mycelium_router.validation import validate_manifest
from mycelium_ui_gateway.validation import validate_swarm_status
from planner_assignment import compile_bound_layer_assignments, validate_control_plane_tranche
from route_contract import validate_manual_provisioning_route_v1
from runtime_contracts import GPT2_DECODER_TENSOR_SUFFIXES
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


def _gpt2_weight_map() -> dict[str, str]:
    weight_map = {
        "transformer.wte.weight": "shard-1.safetensors",
        "transformer.wpe.weight": "shard-1.safetensors",
    }
    for layer in range(4):
        shard = "shard-2.safetensors" if layer < 2 else "shard-3.safetensors"
        for suffix in GPT2_DECODER_TENSOR_SUFFIXES:
            weight_map[f"transformer.h.{layer}.{suffix}"] = shard
    weight_map.update(
        {
            "transformer.ln_f.weight": "shard-3.safetensors",
            "transformer.ln_f.bias": "shard-3.safetensors",
            "lm_head.weight": "shard-3.safetensors",
        }
    )
    return weight_map


def model_manifest() -> dict[str, Any]:
    return mm.compile_model_manifest(
        model_id="org/model",
        requested_revision="main",
        resolved_commit="a" * 40,
        config={
            "model_type": "gpt2",
            "architectures": ["GPT2LMHeadModel"],
            "n_layer": 4,
            "n_embd": 128,
            "n_head": 4,
            "n_inner": 512,
            "vocab_size": 1024,
            "n_positions": 128,
            "layer_norm_epsilon": 1e-5,
            "activation_function": "gelu_new",
            "scale_attn_weights": True,
            "scale_attn_by_inverse_layer_idx": False,
            "reorder_and_upcast_attn": False,
            "add_cross_attention": False,
            "tie_word_embeddings": False,
        },
        checkpoint_index={
            "metadata": {"total_size": 60},
            "weight_map": _gpt2_weight_map(),
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
                    "backend": "mlx",
                    "decode_mode": "stage_local_kv",
                    "prefill_ms_per_layer_token": 0.001,
                    "decode_ms_per_layer_token": 0.001,
                    "memory_bandwidth_Bps": 1_000_000_000,
                    "spill_bandwidth_Bps": 1_000_000_000,
                    "calibration_confidence": 1.0,
                },
            },
        )
        add(
            RecordKind.MEMBERSHIP,
            node_id,
            {
                "protocol": "mycelium.membership.v1",
                "subject_node_id": node_id,
                "subject_incarnation": 1,
                "state": "alive",
                "reporter_node_id": node_id,
                "reason": "self_heartbeat",
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


def layer_load_proofs(tranche: dict[str, Any]) -> list[dict[str, Any]]:
    """Build synthetic compatibility proofs with runtime-loadable tensor ownership."""
    proofs = []
    for assignment in tranche["assignments"]:
        probe_width = (
            assignment["runtime"]["model_config"]["vocab_size"]
            if "lm_head" in assignment["components"]
            else assignment["runtime"]["model_config"]["n_embd"]
        )
        loaded_identity = {
            "assignment_id": assignment["assignment_id"],
            "runtime": assignment["runtime"],
            "tensor_keys": sorted(assignment["expected_tensor_keys"]),
        }
        loaded_digest = hashlib.sha256(
            json.dumps(
                loaded_identity,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        ).hexdigest()
        probe_digest = hashlib.sha256(
            f"contract-probe:{assignment['assignment_id']}".encode("utf-8")
        ).hexdigest()
        resolved_aliases = {
            component: {
                "target_component": target,
                "tensor_keys": assignment["component_tensor_keys"][component],
            }
            for component, target in assignment.get("component_aliases", {}).items()
        }
        proofs.append(
            {
                "protocol": "mycelium.layer_load_proof.v1",
                "deployment_id": assignment["deployment_id"],
                "deployment_epoch": assignment["deployment_epoch"],
                "assignment_id": assignment["assignment_id"],
                "node_id": assignment["node_id"],
                "model_id": assignment["model_id"],
                "manifest_digest": assignment["manifest_digest"],
                "resolved_commit": assignment["resolved_commit"],
                "loaded_range": assignment["range"],
                "loaded_components": assignment["components"],
                "loaded_tensor_keys": sorted(assignment["expected_tensor_keys"]),
                "loaded_tensor_digest": f"sha256:{loaded_digest}",
                "resolved_component_aliases": resolved_aliases,
                "runtime": assignment["runtime"],
                "runtime_identity": {
                    "backend": "mlx",
                    "backend_version": "compatibility-fixture",
                    "device": "gpu",
                    "dtype": assignment["runtime"]["dtype"],
                    "quantization": assignment["runtime"]["quantization"],
                    "architecture": assignment["runtime"]["architecture"],
                },
                "probe_shape": [1, 3, probe_width],
                "probe_digest": f"sha256:{probe_digest}",
                "load_generation": 1,
                "control_plane_binding": assignment["control_plane_binding"],
                "route_ready": False,
                "claim_boundary": (
                    "assignment-bound local MLX stage loaded and deterministically probed; "
                    "no route challenge or distributed inference claim"
                ),
            }
        )
    build_execution_graph(
        tranche,
        proofs,
        manifest=model_manifest(),
        runtime_endpoints={
            proof["assignment_id"]: f"tcp://127.0.0.1:{9100 + index}"
            for index, proof in enumerate(proofs)
        },
        topology_version=1,
        token_envelope_bytes=1024,
    )
    return proofs


def execution_graph_document(
    tranche: dict[str, Any], proofs: list[dict[str, Any]]
) -> dict[str, Any]:
    graph = build_execution_graph(
        tranche,
        proofs,
        manifest=model_manifest(),
        runtime_endpoints={
            proof["assignment_id"]: f"tcp://127.0.0.1:{9100 + index}"
            for index, proof in enumerate(proofs)
        },
        topology_version=1,
        token_envelope_bytes=1024,
    )
    return execution_graph_to_dict(graph)


def path_manifest_document(graph_document: dict[str, Any]) -> dict[str, Any]:
    from mycelium_router.serialization import execution_graph_from_dict

    graph = execution_graph_from_dict(graph_document)
    placements = [stage.placements[0] for stage in graph.stages]
    first = placements[0].placement_id
    last = placements[-1].placement_id
    loopback = next(
        edge
        for edge in graph.loopback_edges
        if edge.from_placement_id == last and edge.to_placement_id == first
    )
    manifest = PathManifest(
        path_id="path-fixture",
        path_attempt=1,
        request_id="request-fixture",
        deployment_id=graph.deployment_id,
        deployment_epoch=graph.deployment_epoch,
        topology_version=graph.topology_version,
        manifest_digest=graph.manifest_digest,
        ordered_hops=tuple(
            PathHop(
                stage_id=stage.stage_id,
                placement_id=placement.placement_id,
                reservation_id=f"reservation-{index}",
                reservation_expires_at=1_300.0,
                reservation_epoch=graph.deployment_epoch,
            )
            for index, (stage, placement) in enumerate(
                zip(graph.stages, placements, strict=True)
            )
        ),
        loopback_edge_id=loopback.edge_id,
    )
    return path_manifest_to_dict(validate_manifest(manifest, graph))


def membership_envelope() -> dict[str, Any]:
    private_key = Ed25519PrivateKey.from_private_bytes(b"\x01" * 32)
    signer = Ed25519EvidenceSigner(
        endpoint_id="endpoint-node-a",
        _private_key=private_key,
        _public_key_bytes=private_key.public_key().public_bytes_raw(),
    )
    message = {
        "protocol": JOIN_REQUEST_PROTOCOL,
        "message_id": "join-request-fixture",
        "swarm_id": "swarm-fixture",
        "sender_node_id": "node-a",
        "sender_endpoint_id": "endpoint-node-a",
        "recipient_node_id": "seed-node",
        "incarnation": "incarnation-fixture",
        "generation": 0,
        "issued_at": 1_000.0,
        "expires_at": 1_300.0,
        "invite_nonce": "invite-fixture",
        "endpoint_addr": {
            "id": "endpoint-node-a",
            "addrs": ["iroh-relay://relay.example/node-a"],
        },
        "software_version": "0.1.0",
        "peer_class": "mac_mlx_iroh",
        "runtime_capability": {
            "runtime_backend": "mlx",
            "transport": "iroh",
            "activation_protocol": "mycelium.router_wire.v1",
        },
    }
    return sign_membership_message(signer=signer, message=message)


def membership_message(protocol: str) -> dict[str, Any]:
    seed_to_node = protocol in {
        JOIN_ACCEPTANCE_PROTOCOL,
        RESUME_ACCEPTANCE_PROTOCOL,
        LEASE_RENEWAL_PROTOCOL,
        ASSIGNMENT_OFFER_PROTOCOL,
    }
    sender_node_id = "seed-node" if seed_to_node else "node-a"
    sender_endpoint_id = "endpoint-seed" if seed_to_node else "endpoint-node-a"
    common = {
        "protocol": protocol,
        "message_id": f"{protocol.rsplit('.', 2)[-2]}-fixture",
        "swarm_id": "swarm-fixture",
        "sender_node_id": sender_node_id,
        "sender_endpoint_id": sender_endpoint_id,
        "recipient_node_id": "node-a" if seed_to_node else "seed-node",
        "incarnation": "incarnation-fixture",
        "generation": 1,
        "issued_at": 1_000.0,
        "expires_at": 1_300.0,
    }
    specific = {
        JOIN_REQUEST_PROTOCOL: {
            "generation": 0,
            "invite_nonce": "invite-fixture",
            "endpoint_addr": {
                "id": "endpoint-node-a",
                "addrs": ["iroh-relay://relay.example/node-a"],
            },
            "software_version": "0.1.0",
            "peer_class": "mac_mlx_iroh",
            "runtime_capability": {
                "runtime_backend": "mlx",
                "transport": "iroh",
                "activation_protocol": "mycelium.router_wire.v1",
            },
        },
        JOIN_ACCEPTANCE_PROTOCOL: {
            "request_message_id": "join-request-fixture",
            "accepted_node_id": "node-a",
            "accepted_incarnation": "incarnation-fixture",
            "membership_generation": 1,
            "lease_expires_at": 1_300.0,
        },
        RESUME_REQUEST_PROTOCOL: {
            "previous_incarnation": "incarnation-previous",
            "endpoint_addr": {
                "id": "endpoint-node-a",
                "addrs": ["iroh-relay://relay.example/node-a"],
            },
            "software_version": "0.1.0",
            "peer_class": "mac_mlx_iroh",
            "runtime_capability": {
                "runtime_backend": "mlx",
                "transport": "iroh",
                "activation_protocol": "mycelium.router_wire.v1",
            },
        },
        RESUME_ACCEPTANCE_PROTOCOL: {
            "generation": 2,
            "request_message_id": "resume-request-fixture",
            "accepted_node_id": "node-a",
            "accepted_incarnation": "incarnation-fixture",
            "previous_membership_generation": 1,
            "membership_generation": 2,
            "lease_expires_at": 1_300.0,
        },
        CAPABILITY_REPORT_PROTOCOL: {
            "platform": "android",
            "architecture": "aarch64",
            "memory_bytes": 8_000_000_000,
            "available_storage_bytes": 64_000_000_000,
            "backends": ["pixel-stdlib"],
            "precisions": ["float32"],
        },
        LINK_PROBE_REPORT_PROTOCOL: {
            "target_node_id": "seed-node",
            "target_endpoint_id": "endpoint-seed",
            "reachable": True,
            "rtt_ms": 42.5,
            "goodput_bytes_per_second": 12_000_000.0,
            "probe_sequence": 3,
        },
        HEARTBEAT_PROTOCOL: {
            "heartbeat_sequence": 7,
            "lifecycle_state": "RUNNING",
            "route_ready": False,
            "active_requests": 0,
            "liveness_source": "scheduled_heartbeat",
            "activity_receipt_digest": None,
            "activity_peer_node_id": None,
        },
        LEASE_RENEWAL_PROTOCOL: {
            "heartbeat_message_id": "heartbeat-fixture",
            "member_incarnation": "incarnation-fixture",
            "membership_generation": 1,
            "lease_expires_at": 1_300.0,
        },
        ASSIGNMENT_OFFER_PROTOCOL: {
            "deployment_id": "deployment-fixture",
            "deployment_epoch": 1,
            "assignment_id": "assignment-fixture",
            "assignment_digest": "sha256:" + "a" * 64,
            "stage_pack_digest": "sha256:" + "b" * 64,
            "graph_digest": "sha256:" + "c" * 64,
            "load_generation": 1,
            "placement_provenance": "frozen_fixture",
            "peer_endpoint_records": [
                {
                    "node_id": "node-b",
                    "endpoint_id": "endpoint-node-b",
                    "deployment_epoch": 1,
                    "membership_generation": 1,
                    "valid_from": 1_000.0,
                    "valid_until": 1_300.0,
                }
            ],
        },
        ASSIGNMENT_RESULT_PROTOCOL: {
            "deployment_id": "deployment-fixture",
            "deployment_epoch": 1,
            "assignment_id": "assignment-fixture",
            "accepted": True,
            "result_code": "loaded",
            "load_proof_digest": "sha256:" + "d" * 64,
            "runtime_endpoint": "iroh://endpoint-node-a/assignment-fixture",
        },
        DRAIN_ACK_PROTOCOL: {
            "drain_id": "drain-fixture",
            "active_requests": 0,
            "last_request_id": None,
            "completed_at": 1_050.0,
        },
        SEED_ROTATION_ACK_PROTOCOL: {
            "authority_generation": 2,
            "transition_digest": "sha256:" + "e" * 64,
        },
    }[protocol]
    return validate_membership_message({**common, **specific})


def capacity_profile() -> dict[str, Any]:
    profile = compile_capacity_profile(
        CapacityProfileKey(
            model_digest="sha256:" + "a" * 64,
            source_evidence_digest="sha256:" + "b" * 64,
            quantization="int8",
            backend="mlx",
            runtime_build="fixture-runtime",
            hardware_class="fixture-arm64",
            power_mode="ac",
            context_bucket="0-4096",
            kv_mode="stage-local",
        ),
        (
            CapacityObservation(
                concurrency=1,
                sample_count=5,
                p95_ttft_ms=500.0,
                p95_tpot_ms=50.0,
                aggregate_output_tps=10.0,
                peak_memory_bytes=1_000_000_000,
                memory_budget_bytes=2_000_000_000,
            ),
        ),
        CapacityProfilePolicy(
            ttft_p95_slo_ms=1_000.0,
            tpot_p95_slo_ms=100.0,
            min_samples=3,
        ),
    )
    return profile.to_document()


def signed_evidence_bundle(evidence_bundle: dict[str, Any]) -> dict[str, Any]:
    private_key = Ed25519PrivateKey.from_private_bytes(b"\x02" * 32)
    signer = Ed25519EvidenceSigner(
        endpoint_id="seed-fixture",
        _private_key=private_key,
        _public_key_bytes=private_key.public_key().public_bytes_raw(),
    )
    profile = capacity_profile()
    return seal_evidence_bundle(
        evidence_bundle,
        signer=signer,
        captured_at_unix_ms=1_000,
        valid_for_ms=5_000,
        authority_generation=2,
        capacity_profiles={"node-a": profile, "node-b": profile},
    )


def link_state() -> dict[str, Any]:
    document = {
        "protocol": "mycelium.link_state.v1",
        "src_node_id": "node-a",
        "dst_node_id": "node-b",
        "src_endpoint_id": "endpoint-node-a",
        "dst_endpoint_id": "endpoint-node-b",
        "reachable": True,
        "connect_rtt_ema_ms": 1.2,
        "rtt_p95_ms": 1.8,
        "jitter_ms": 0.1,
        "loss_ratio": 0.0,
        "goodput_mbps": 900.0,
        "sample_count": 16,
        "measurement_method": "active_probe",
    }
    build_record(
        swarm_id="swarm-fixture",
        kind=RecordKind.LINK,
        origin_node_id="node-a",
        incarnation=1,
        sequence=1,
        boot_id="boot-node-a",
        generated_at_unix_ms=100,
        ttl_ms=10_000,
        payload=document,
    )
    return document


def product_swarm_status() -> dict[str, Any]:
    document = {
        "protocol": "mycelium.product_ui.swarm.v1",
        "native_nodes": [
            {
                "member_id": "mobile-conformance-001",
                "capability": "native_inference_node",
                "membership_state": "reachable",
                "connectivity": "unknown",
                "endpoint_id": None,
            }
        ],
        "browser_workers": [],
    }
    validate_swarm_status(document)
    return document


def live_route_incident() -> dict[str, Any]:
    return {
        "protocol": "mycelium.live_route_incident.v1",
        "incident_id": "route-incident-fixture",
        "deployment_id": "deployment-fixture",
        "request_id": None,
        "state": "route_unavailable",
        "reason": "fixture_route_unavailable",
        "observed_at_unix_ms": 1_900_000_000_000,
    }


def performance_budget() -> dict[str, Any]:
    return validate_performance_budget(
        {
            "protocol": "mycelium.performance_budget.v1",
            "budget_id": "m12-interactive-v1",
            "workload_label": "interactive_chat_v1",
            "minimum_sample_size": 5,
            "ttft_ms": {"maximum_p50": 5_000.0, "maximum_p95": 10_000.0},
            "tpot_ms": {"maximum_p50": 2_000.0, "maximum_p95": 4_000.0},
            "minimum_output_tokens_per_second": 0.1,
            "maximum_peak_rss_bytes_by_member": {
                "member-a": 20_000_000_000,
                "member-b": 20_000_000_000,
            },
            "maximum_frames_per_request_by_stage": {
                "stage-a": 512,
                "stage-b": 512,
            },
            "execution_scope": "sequential_observed",
            "queueing_budget_state": "deferred_to_m16",
        }
    )


def assignment_cache_status() -> dict[str, Any]:
    return validate_cache_status(
        {
            "protocol": "mycelium.assignment_artifact_cache.v1",
            "max_bytes": 10_000,
            "entry_count": 3,
            "used_bytes": 600,
            "pinned_object_count": 2,
        }
    )


def assignment_materialization() -> dict[str, Any]:
    return validate_materialization_report(
        {
            "protocol": "mycelium.assignment_materialization.v1",
            "assignment_id": "assignment-node-a",
            "objects": [
                {
                    "relative_path": "model-stage-000.safetensors",
                    "object_id": "sha256:" + "1" * 64,
                    "tensor_digest": "sha256:" + "2" * 64,
                    "size_bytes": 200,
                },
                {
                    "relative_path": "model-static.safetensors",
                    "object_id": "sha256:" + "3" * 64,
                    "tensor_digest": "sha256:" + "4" * 64,
                    "size_bytes": 100,
                },
            ],
            "opened_object_ids": ["sha256:" + "1" * 64, "sha256:" + "3" * 64],
            "cache_entry_count": 3,
            "unassigned_object_count": 1,
            "route_ready": False,
        }
    )


def candidate_promotion_report() -> dict[str, Any]:
    observations = [
        {
            "case_id": f"canary-{index}",
            "completed": True,
            "quality_passed": True,
            "negative_check_passed": True,
            "ttft_ms": 1_000.0 + index,
            "tpot_ms": 100.0 + index,
            "output_tokens_per_second": 2.0,
            "peak_rss_bytes_by_member": {
                "member-a": 1_000_000_000,
                "member-b": 1_000_000_000,
            },
            "frames_per_request_by_stage": {"stage-a": 8, "stage-b": 8},
        }
        for index in range(5)
    ]
    return evaluate_candidate_promotion(
        candidate_deployment_id="candidate-fixture",
        incumbent_deployment_id="incumbent-fixture",
        planner_snapshot_digest="sha256:" + "5" * 64,
        observations=observations,
        performance_budget=performance_budget(),
    )


def m13_placement_projection() -> dict[str, Any]:
    return validate_m13_placement_projection(
        {
            "protocol": "mycelium.m13_placement_projection.v1",
            "snapshot_digest": "sha256:" + "5" * 64,
            "evidence_bundle_digest": "sha256:" + "6" * 64,
            "snapshot_generation": 9,
            "authority_generation": 2,
            "verification_key_digest": "sha256:" + "7" * 64,
            "valid_until_unix_ms": 6_000,
            "placement_provenance": "planner_v2",
            "decode_mode": "stage_local_kv",
            "quantization": "int8-weight-only",
            "nodes": [
                {
                    "node_id": "node-a",
                    "backend": "mlx",
                    "decode_mode": "stage_local_kv",
                    "start_layer": 0,
                    "end_layer_exclusive": 2,
                    "fast_allocatable_bytes": 2_000_000_000,
                    "total_allocatable_bytes": 4_000_000_000,
                    "prefill_ms_per_layer_token": 0.1,
                    "decode_ms_per_layer_token": 0.2,
                    "profile_digest": "sha256:" + "8" * 64,
                    "source_evidence_digest": "sha256:" + "9" * 64,
                    "assignment_id": "assignment-node-a",
                    "assignment_digest": "sha256:" + "a" * 64,
                    "assigned_object_count": 2,
                    "load_proof_digest": "sha256:" + "b" * 64,
                    "ready": True,
                }
            ],
            "links": [],
            "exclusions": [],
            "ab_deltas": [],
            "promotion": {
                "candidate_deployment_id": "candidate-fixture",
                "incumbent_deployment_id": "incumbent-fixture",
                "decision": "promote",
                "reasons": [],
                "sample_size": 5,
            },
            "route_ready": False,
        }
    )


def transport_path_observation(
    src: str = "node-a", dst: str = "node-b", *, warm_rtt_ms: float = 4.0
) -> dict[str, Any]:
    endpoints = {node: f"endpoint-{node}" for node in ("node-a", "node-b", "node-c")}
    return validate_transport_path_observation(
        {
            "protocol": "mycelium.transport_path_observation.v1",
            "local_node_id": src,
            "local_endpoint_id": endpoints[src],
            "remote_node_id": dst,
            "remote_endpoint_id": endpoints[dst],
            "connection_generation": 1,
            "path_class": "direct",
            "relay_identity": None,
            "relay_region": None,
            "cold_rtt_ms": warm_rtt_ms + 1.0,
            "warm_rtt_ms": warm_rtt_ms,
            "observed_goodput_Bps": 10_000_000.0,
            "jitter_ms": 0.25,
            "loss_ratio": 0.0,
            "sample_count": 8,
            "connections_opened": 1,
            "frames_sent": 8,
            "reconnect_count": 0,
            "selected_path_changes": 0,
            "measurement_source": "iroh_activation_plane",
            "measured_at_unix_ms": 1_000,
            "fresh_until_unix_ms": 7_201_000,
            "exclusions": ["path_transition_not_observed_within_budget"],
        },
        require_resolved=True,
    )


def m14_topology_projection() -> dict[str, Any]:
    nodes = ("node-a", "node-b", "node-c")
    endpoints = {node: f"endpoint-{node}" for node in nodes}
    rtts = {
        ("node-a", "node-b"): 4.0,
        ("node-b", "node-c"): 4.0,
        ("node-c", "node-a"): 4.0,
        ("node-a", "node-c"): 8.0,
        ("node-c", "node-b"): 8.0,
        ("node-b", "node-a"): 8.0,
    }
    observations = [
        transport_path_observation(src, dst, warm_rtt_ms=rtt)
        for (src, dst), rtt in sorted(rtts.items())
    ]
    matrix = complete_directed_observation_matrix(
        observations,
        node_ids=nodes,
        endpoint_ids_by_node=endpoints,
        now_unix_ms=2_000,
    )
    decision = select_measured_topology(
        matrix,
        node_ids=nodes,
        entry_node_id="node-a",
    )
    return build_m14_topology_projection(
        observations=observations,
        decision=decision,
        allocation=[
            {"node_id": node, "start": index * 8, "end": (index + 1) * 8}
            for index, node in enumerate(decision["opened_order"])
        ],
        promotion=None,
        exclusions=("path_transition_not_observed_within_budget",),
    )


def product_snapshot() -> dict[str, Any]:
    document = {
        "protocol": "mycelium.product_snapshot.v1",
        "publication": {
            "snapshot_id": "snapshot-fixture",
            "generation": 1,
            "cursor": 1,
            "published_at_unix_ms": 1_900_000_000_000,
            "source_mode": "fixture",
        },
        "supported_entity_kinds": list(ENTITY_KINDS),
        "source_states": [
            {
                "source_id": "membership-source",
                "authority": "seed_coordinator",
                "status": "current",
                "observed_at_unix_ms": 1_900_000_000_000,
                "valid_until_unix_ms": 1_900_000_300_000,
                "generation": 1,
                "reason_code": None,
            }
        ],
        "entities": [
            {
                "entity_id": "mobile-conformance-fixture",
                "kind": "device",
                "label": "Mobile conformance device",
                "source_id": "membership-source",
                "binding": {
                    "deployment_id": None,
                    "deployment_epoch": None,
                    "route_id": None,
                    "route_generation": None,
                    "topology_version": None,
                },
                "freshness": {
                    "status": "current",
                    "observed_at_unix_ms": 1_900_000_000_000,
                    "valid_until_unix_ms": 1_900_000_300_000,
                },
                "attributes": {
                    "peer_class": "android_termux_iroh",
                    "membership_generation": 1,
                    "authority_generation": 1,
                    "incarnation": "mobile-incarnation-fixture",
                    "lifecycle": "running",
                    "lease_freshness": "fresh",
                    "runtime_backend": "pixel-stdlib",
                    "transport": "iroh",
                    "activation_protocol": "mycelium.router_wire.v1",
                    "activation_eligible": False,
                    "placement_id": None,
                },
            }
        ],
        "relations": [],
        "readiness": [
            {
                "scope_id": "mobile-conformance-fixture",
                "dimension": "membership",
                "state": "ready",
                "reason_code": None,
                "source_id": "membership-source",
            },
            {
                "scope_id": "mobile-conformance-fixture",
                "dimension": "qualification",
                "state": "not_ready",
                "reason_code": "mobile_qualification_required",
                "source_id": "membership-source",
            },
        ],
        "notices": [],
        "provenance": {
            "projector": "mycelium_product_spine",
            "projector_version": "m12-v1",
            "source_mode": "fixture",
        },
    }
    return validate_product_snapshot(document)


def product_event() -> dict[str, Any]:
    return validate_product_event(
        {
            "protocol": "mycelium.product_event.v1",
            "cursor": 1,
            "previous_cursor": 0,
            "event_kind": "snapshot_published",
            "snapshot": product_snapshot(),
        }
    )


def live_route_status(graph_document: dict[str, Any]) -> dict[str, Any]:
    stages = [
        {
            "stage_id": stage["stage_id"],
            "placement_id": placement["placement_id"],
            "node_id": placement["node_id"],
            "runtime_backend": placement["runtime_backend"],
            "start_layer": stage["range"]["start_layer"],
            "end_layer_exclusive": stage["range"]["end_layer_exclusive"],
            "component_roles": stage["component_roles"],
        }
        for stage in graph_document["stages"]
        for placement in stage["placements"]
    ]
    peers = []
    for node_id in sorted({stage["node_id"] for stage in stages}):
        placements = [stage for stage in stages if stage["node_id"] == node_id]
        peers.append(
            {
                "node_id": node_id,
                "placements": placements,
                "frames_sent": 0,
                "frames_received": 0,
                "applied_operation_count": 0,
                "decode_mode": "stage_local_kv",
                "active_kv_state_count": 0,
                "retained_result_count": 0,
                "release_counts": {},
            }
        )
    identity_digest = hashlib.sha256(canonical_bytes(graph_document)).hexdigest()
    return {
        "protocol": "mycelium.live_route_status.v1",
        "route_alive": False,
        "simulated": False,
        "route_identity_digest": f"sha256:{identity_digest}",
        "deployment_id": graph_document["deployment_id"],
        "model_id": graph_document["model_id"],
        "topology_version": graph_document["topology_version"],
        "decode_mode": "stage_local_kv",
        "counters": {
            "frames_sent": 0,
            "frames_received": 0,
            "applied_operation_count": 0,
            "fatal": None,
        },
        "stages": stages,
        "peers": peers,
        "recent_inferences": [],
        "incidents": [],
    }


def product_bootstrap() -> dict[str, Any]:
    return {
        "protocol": "mycelium.product_ui.bootstrap.v1",
        "source_mode": "fixture",
        "session": {
            "csrf_header": "X-Mycelium-CSRF",
            "csrf_token": "synthetic-fixture-csrf-token",
            "expires_at_unix_ms": 1_900_000_000_000,
        },
        "api": {
            "observatory_snapshot": "/api/v1/observatory/snapshot",
            "observatory_events": "/api/v1/observatory/events",
            "qualification_current": "/api/v1/qualification/current",
            "inference_submit": "/api/v1/inference",
            "swarm_status": "/api/v1/swarm/status",
            "swarm_invites": "/api/v1/swarm/invites",
            "swarm_join": "/api/v1/swarm/join",
            "swarm_leave": "/api/v1/swarm/leave",
            "product_snapshot": "/api/v1/product/snapshot",
            "product_events": "/api/v1/product/events",
            "product_export": "/api/v1/product/export",
        },
        "limits": {"max_prompt_utf8_bytes": 131_072, "max_new_tokens": 4_096},
        "qualification_authority": (
            "mycelium_qualification.qualifier:RouteQualificationV1"
        ),
    }


def observatory_snapshot() -> dict[str, Any]:
    freshness = {
        "observed_at": "2026-07-17T00:00:00Z",
        "valid_until": "2026-07-17T00:05:00Z",
    }
    binding = {
        "deployment": {"id": "deployment-fixture", "epoch": 1},
        "model": {
            "id": "model-fixture",
            "revision": "immutable-revision",
            "manifest_digest": "sha256:" + "a" * 64,
            "num_layers": 4,
        },
        "route": {
            "id": "route-fixture",
            "generation": 1,
            "digest": "sha256:" + "b" * 64,
            "assignments": [
                {
                    "id": "assignment-a",
                    "peer_id": "peer-a",
                    "start_layer": 0,
                    "end_layer_exclusive": 2,
                },
                {
                    "id": "assignment-b",
                    "peer_id": "peer-b",
                    "start_layer": 2,
                    "end_layer_exclusive": 4,
                },
            ],
        },
    }
    claim_specs = (
        ("deployment", "deployment-fixture", "deployment_bound", "gateway_projection", "mycelium_gateway"),
        ("model", "model-fixture", "model_bound", "provisioning_audit", "mycelium_provisioning"),
        ("route", "route-fixture", "route_challenge_succeeded", "route_challenge", "mycelium_router"),
        ("assignment", "assignment-a", "assignment_ready", "provisioning_audit", "mycelium_provisioning"),
        ("assignment", "assignment-b", "assignment_ready", "provisioning_audit", "mycelium_provisioning"),
        ("request", "request-fixture", "request_lifecycle_observed", "router_runtime", "mycelium_router"),
    )
    return {
        "protocol": "mycelium.observatory.snapshot.v1",
        "snapshot_id": "snapshot-fixture",
        "freshness": freshness,
        "binding": binding,
        "claims": [
            {
                "id": f"claim-{index}",
                "scope": {"kind": kind, "id": scope_id},
                "statement": statement,
                "value": "confirmed",
                "freshness": freshness,
                "provenance": {"kind": provenance_kind, "producer": producer},
            }
            for index, (kind, scope_id, statement, provenance_kind, producer) in enumerate(
                claim_specs, start=1
            )
        ],
        "conflicts": [],
        "route_challenge": {
            "id": "challenge-fixture",
            "status": "succeeded",
            "freshness": freshness,
            "binding": binding,
            "provenance": {"kind": "route_challenge", "producer": "mycelium_router"},
        },
        "request_lifecycle": {
            "request_id": "request-fixture",
            "state": "completed",
            "path_attempt": 1,
            "freshness": freshness,
            "binding": binding,
            "provenance": {"kind": "router_runtime", "producer": "mycelium_router"},
        },
        "provenance": {"kind": "gateway_projection", "producer": "mycelium_gateway"},
    }


def request_gateway_binding() -> QualificationBinding:
    return QualificationBinding(
        qualification_id="qualification-fixture",
        qualification_digest="sha256:" + "1" * 64,
        deployment_id="deployment-fixture",
        deployment_epoch=1,
        topology_version=1,
        model_id="org/model",
        resolved_commit="immutable-revision",
        manifest_digest="sha256:" + "2" * 64,
        path_manifest_digest="sha256:" + "3" * 64,
        stage_load_proof_digests=(
            "sha256:" + "4" * 64,
            "sha256:" + "5" * 64,
        ),
    )


def request_gateway_submission() -> dict[str, Any]:
    return InferenceSubmission(
        prompt="synthetic compatibility prompt",
        max_new_tokens=16,
        qualification=request_gateway_binding(),
    ).to_dict()


def request_event() -> dict[str, Any]:
    return StreamEvent(
        request_id="request-fixture",
        sequence=1,
        kind="token",
        token_index=0,
        text="fixture-token",
    ).to_dict()


def seed_operator_inventory() -> dict[str, Any]:
    return {
        "protocol": SEED_OPERATOR_INVENTORY_PROTOCOL,
        "swarm_id": "swarm-fixture",
        "seed_node_id": "seed-fixture",
        "seed_key_digest": "sha256:" + "1" * 64,
        "observed_at_unix_ms": 1_700_000_000_000,
        "members": [
            {
                "node_id": "node-fixture",
                "peer_class": "mac_mlx_iroh",
                "generation": 2,
                "incarnation": "incarnation-fixture",
                "lifecycle_state": "RUNNING",
                "lease_freshness": "fresh",
                "activation_eligible": True,
                "revocation_state": "active",
            }
        ],
        "route_ready": False,
    }


def seed_operator_revocation() -> dict[str, Any]:
    return {
        "protocol": SEED_OPERATOR_REVOCATION_PROTOCOL,
        "swarm_id": "swarm-fixture",
        "seed_key_digest": "sha256:" + "1" * 64,
        "node_id": "node-fixture",
        "previous_generation": 2,
        "generation": 3,
        "lifecycle_state": "STOPPED",
        "reason": "operator_revoked",
        "revoked_at_unix_ms": 1_700_000_000_000,
        "route_ready": False,
    }


def seed_backup() -> dict[str, Any]:
    return {
        "protocol": SEED_BACKUP_PROTOCOL,
        "backup_id": "backup-fixture",
        "seed_key_digest": "sha256:" + "1" * 64,
        "authority_generation": 1,
        "member_count": 1,
        "backup_directory": "/operator-private/backup-fixture",
        "route_ready": False,
    }


def seed_key_transition() -> dict[str, Any]:
    old_key = Ed25519PrivateKey.from_private_bytes(b"\x02" * 32)
    new_key = Ed25519PrivateKey.from_private_bytes(b"\x03" * 32)
    old_signer = Ed25519EvidenceSigner(
        endpoint_id="seed-old-fixture",
        _private_key=old_key,
        _public_key_bytes=old_key.public_key().public_bytes_raw(),
    )
    new_signer = Ed25519EvidenceSigner(
        endpoint_id="seed-new-fixture",
        _private_key=new_key,
        _public_key_bytes=new_key.public_key().public_bytes_raw(),
    )
    transition = {
        "swarm_id": "swarm-fixture",
        "seed_node_id": "seed-fixture",
        "previous_generation": 1,
        "authority_generation": 2,
        "old_seed_key_digest": old_signer.verification_key_digest,
        "new_seed_key_digest": new_signer.verification_key_digest,
        "initiated_at": 1_700_000_000.0,
        "effective_at": 1_700_000_000.0,
        "overlap_expires_at": 1_700_003_600.0,
        "reason": "scheduled_rotation",
    }
    return {
        "protocol": SEED_KEY_TRANSITION_PROTOCOL,
        "transition": transition,
        "old_signature": old_signer.sign(transition),
        "old_verification_key": old_signer.public_key_record(),
        "new_signature": new_signer.sign(transition),
        "new_verification_key": new_signer.public_key_record(),
    }


def seed_operator_rotation() -> dict[str, Any]:
    return {
        "protocol": SEED_OPERATOR_ROTATION_PROTOCOL,
        "event": "rotation_pending",
        "swarm_id": "swarm-fixture",
        "previous_generation": 1,
        "authority_generation": 2,
        "old_seed_key_digest": "sha256:" + "1" * 64,
        "new_seed_key_digest": "sha256:" + "2" * 64,
        "effective_at_unix_ms": 1_700_000_000_000,
        "overlap_expires_at_unix_ms": 1_700_003_600_000,
        "route_ready": False,
    }


def documents() -> dict[str, dict[str, Any]]:
    assignments, reports = assignments_and_reports()
    router, allocator, evidence_bundle = gossip_documents()
    planner_evidence_snapshot, _, control_plane_tranche = control_plane_documents(evidence_bundle)
    load_proofs = layer_load_proofs(control_plane_tranche)
    graph = execution_graph_document(control_plane_tranche, load_proofs)
    semantic_snapshot = observatory_snapshot()
    generated = {
        "route-plan-v2.json": route_plan_to_dict(plan_snapshot(planner_snapshot())),
        "manual-provisioning-route-v1.json": manual_route(),
        "layer-assignment-v2.json": assignments[0],
        "artifact-verification-report-v1.json": reports[0],
        "provisioning-audit-v1.json": provisioning_audit(),
        "gossip-router-view-v1.json": router,
        "gossip-allocator-view-v1.json": allocator,
        "gossip-evidence-bundle-v1.json": evidence_bundle,
        "signed-gossip-evidence-bundle-v1.json": signed_evidence_bundle(evidence_bundle),
        "layer-planner-snapshot-v1.json": planner_evidence_snapshot,
        "control-plane-tranche-v1.json": control_plane_tranche,
        "layer-load-proof-v1.json": load_proofs[0],
        "route-qualification-v1.json": route_qualification_to_dict(
            synthetic_route_qualification_fixture()
        ),
        "request-gateway-v1.json": request_gateway_submission(),
        "request-event-v1.json": request_event(),
        "membership-signed-message-v1.json": membership_envelope(),
        "membership-join-request-v1.json": membership_message(
            JOIN_REQUEST_PROTOCOL
        ),
        "membership-join-acceptance-v1.json": membership_message(
            JOIN_ACCEPTANCE_PROTOCOL
        ),
        "membership-resume-request-v1.json": membership_message(
            RESUME_REQUEST_PROTOCOL
        ),
        "membership-resume-acceptance-v1.json": membership_message(
            RESUME_ACCEPTANCE_PROTOCOL
        ),
        "membership-capability-report-v1.json": membership_message(
            CAPABILITY_REPORT_PROTOCOL
        ),
        "membership-link-probe-report-v1.json": membership_message(
            LINK_PROBE_REPORT_PROTOCOL
        ),
        "membership-heartbeat-v1.json": membership_message(HEARTBEAT_PROTOCOL),
        "membership-lease-renewal-v1.json": membership_message(
            LEASE_RENEWAL_PROTOCOL
        ),
        "membership-assignment-offer-v1.json": membership_message(
            ASSIGNMENT_OFFER_PROTOCOL
        ),
        "membership-assignment-result-v1.json": membership_message(
            ASSIGNMENT_RESULT_PROTOCOL
        ),
        "membership-drain-ack-v1.json": membership_message(DRAIN_ACK_PROTOCOL),
        "membership-seed-rotation-ack-v1.json": membership_message(
            SEED_ROTATION_ACK_PROTOCOL
        ),
        "seed-operator-inventory-v1.json": seed_operator_inventory(),
        "seed-operator-revocation-v1.json": seed_operator_revocation(),
        "seed-key-transition-v1.json": seed_key_transition(),
        "seed-operator-rotation-v1.json": seed_operator_rotation(),
        "seed-backup-v1.json": seed_backup(),
        "capacity-profile-v1.json": capacity_profile(),
        "link-state-v1.json": link_state(),
        "product-ui-swarm-v1.json": product_swarm_status(),
        "live-route-incident-v1.json": live_route_incident(),
        "performance-budget-v1.json": performance_budget(),
        "assignment-artifact-cache-v1.json": assignment_cache_status(),
        "assignment-materialization-v1.json": assignment_materialization(),
        "candidate-promotion-report-v1.json": candidate_promotion_report(),
        "m13-placement-projection-v1.json": m13_placement_projection(),
        "transport-path-observation-v1.json": transport_path_observation(),
        "m14-topology-projection-v1.json": m14_topology_projection(),
        "product-snapshot-v1.json": product_snapshot(),
        "product-event-v1.json": product_event(),
        "execution-graph-v1.json": graph,
        "path-manifest-v1.json": path_manifest_document(graph),
        "live-route-status-v1.json": live_route_status(graph),
        "router-wire-v1.json": json.loads(
            read_under_root(ROOT, ROOT / "contracts" / "router-wire-golden" / "index.json")
        ),
        "layer-replan-simulation-report-v1.json": simulate_bundle(
            ROOT / "scenarios" / "product-v1-replanning.json"
        ),
        "product-ui-bootstrap-v1.json": product_bootstrap(),
        "observatory-snapshot-v1.json": semantic_snapshot,
        "observatory-event-v1.json": {
            "protocol": "mycelium.observatory.event.v1",
            "generation": 1,
            "snapshot": semantic_snapshot,
        },
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
