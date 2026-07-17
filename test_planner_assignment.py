#!/usr/bin/env python3
from __future__ import annotations

import copy
import json

import pytest

import model_manifest as mm
import planner_assignment as pa
from layer_assignment import validate_assignment_identity
from mycelium_gossip.evidence_bundle import evidence_bundle_to_dict
from mycelium_gossip.registry import VersionedRecordStore
from mycelium_gossip.schema import RecordKind
from mycelium_gossip.service import GossipService
from mycelium_gossip.transport import InMemoryMesh, InMemoryTransport, LivenessEvent, LivenessKind
from mycelium_layer_planner.gossip_adapter import (
    plan_evidence_bundle,
    planner_snapshot_from_evidence_bundle,
)
from mycelium_layer_planner.serialization import route_plan_to_dict
from tests.gossip.helpers import link_payload, make_record, profile_payload, status_payload

DEPLOYMENT_ID = "12345678-1234-5678-9234-abcdefabcdef"
RUNTIME = {"backend": "mlx", "dtype": "float16", "quantization": "none"}
WORKLOAD = {"preset": "interactive_chat_v1", "concurrency_points": [1], "user_scale": 1}
POLICY = {
    "memory_reserve_fraction": 0,
    "replica_budget": 0,
    "ttft_slo_ms": 1_000_000,
    "tpot_slo_ms": 1_000_000,
}
PERFORMANCE = {
    "prefill_ms_per_layer_token": 0.001,
    "decode_ms_per_layer_token": 0.001,
    "memory_bandwidth_Bps": 1_000_000_000,
    "spill_bandwidth_Bps": 1_000_000_000,
    "calibration_confidence": 1.0,
}


class Clock:
    def __call__(self) -> float:
        return 100.0


def manifest() -> dict:
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
            "weight_map": {
                "transformer.wte.weight": "shard-1.safetensors",
                "transformer.wpe.weight": "shard-1.safetensors",
                "transformer.h.0.attn.weight": "shard-2.safetensors",
                "transformer.h.1.attn.weight": "shard-2.safetensors",
                "transformer.h.2.attn.weight": "shard-3.safetensors",
                "transformer.h.3.attn.weight": "shard-3.safetensors",
                "transformer.ln_f.weight": "shard-3.safetensors",
                "lm_head.weight": "shard-1.safetensors",
            },
        },
        file_metadata={
            "shard-1.safetensors": {"size_bytes": 20_000_000, "sha256": "1" * 64},
            "shard-2.safetensors": {"size_bytes": 20_000_000, "sha256": "2" * 64},
            "shard-3.safetensors": {"size_bytes": 20_000_000, "sha256": "3" * 64},
        },
    )


def planner_model(model_manifest: dict) -> dict:
    return {
        "model_id": model_manifest["model_id"],
        "revision": model_manifest["resolved_commit"],
        "weight_digest": mm.manifest_digest_ref(model_manifest),
        "architecture": "Decoder",
        "num_layers": model_manifest["num_layers"],
        "hidden_size": 128,
        "dtype_bytes": 2,
        "kv_heads": 4,
        "head_dim": 32,
        "weight_bytes": 60_000_000,
    }


def bundle(model_manifest: dict, *, epoch: int = 3):
    clock = Clock()
    store = VersionedRecordStore("swarm-a", monotonic=clock)
    service = GossipService(
        swarm_id="swarm-a",
        node_id="local",
        incarnation=1,
        boot_id="boot-local-1",
        transport=InMemoryTransport(InMemoryMesh(monotonic=clock), "local"),
        registry=store,
        monotonic=clock,
    )
    for node_id in ("node-a", "node-b"):
        status = status_payload(node_id, free_bytes=40_000_000)
        status["performance"] = copy.deepcopy(PERFORMANCE)
        store.apply(make_record(RecordKind.PROFILE, node_id=node_id, ttl_ms=10_000, payload=profile_payload(node_id)))
        store.apply(make_record(RecordKind.STATUS, node_id=node_id, ttl_ms=10_000, payload=status))
        service.submit_liveness(
            LivenessEvent(LivenessKind.PUT, "swarm-a", node_id, 1, f"boot-{node_id}-1", clock())
        )
    for src, dst in (("node-a", "node-b"), ("node-b", "node-a")):
        store.apply(
            make_record(
                RecordKind.LINK,
                node_id=src,
                ttl_ms=10_000,
                payload=link_payload(src, dst),
            )
        )
    service.drain()
    return service.capture_evidence_bundle(
        deployment_id=DEPLOYMENT_ID,
        deployment_epoch=epoch,
        model_id=model_manifest["model_id"],
        num_layers=model_manifest["num_layers"],
        manifest_digest=mm.manifest_digest_ref(model_manifest),
        resolved_commit=model_manifest["resolved_commit"],
    )


def tranche():
    model_manifest = manifest()
    evidence = bundle(model_manifest)
    model = planner_model(model_manifest)
    snapshot = planner_snapshot_from_evidence_bundle(
        evidence, model=model, workload=WORKLOAD, policy=POLICY
    )
    route = json.loads(json.dumps(route_plan_to_dict(
        plan_evidence_bundle(evidence, model=model, workload=WORKLOAD, policy=POLICY)
    )))
    return model_manifest, evidence_bundle_to_dict(evidence), snapshot, route


def compile_tranche(*, model_manifest=None, evidence=None, snapshot=None, route=None, deployment_id=DEPLOYMENT_ID, deployment_epoch=3):
    defaults = tranche()
    model_manifest = defaults[0] if model_manifest is None else model_manifest
    evidence = defaults[1] if evidence is None else evidence
    snapshot = defaults[2] if snapshot is None else snapshot
    route = defaults[3] if route is None else route
    nodes = [placement["node_id"] for placement in route["placements"]]
    return pa.compile_bound_layer_assignments(
        route_plan=route,
        planner_snapshot=snapshot,
        evidence_bundle=evidence,
        manifest=model_manifest,
        deployment_id=deployment_id,
        deployment_epoch=deployment_epoch,
        cache_roots={node: f"/tmp/{node}" for node in nodes},
        runtime_by_node={node: RUNTIME for node in nodes},
    )


def test_happy_route_compiles_with_exact_control_plane_binding() -> None:
    model_manifest, evidence, snapshot, route = tranche()
    assignments = compile_tranche(
        model_manifest=model_manifest, evidence=evidence, snapshot=snapshot, route=route
    )
    expected = {
        "protocol": "mycelium.control_plane_binding.v1",
        "evidence_bundle_digest": evidence["evidence_bundle_digest"],
        "planner_snapshot_digest": route["snapshot_digest"],
        "snapshot_generation": evidence["snapshot_generation"],
        "swarm_id": evidence["swarm_id"],
        "deployment_id": DEPLOYMENT_ID,
        "deployment_epoch": 3,
    }
    assert assignments
    assert all(item["control_plane_binding"] == expected for item in assignments)
    assert all(item["route_ready"] is False for item in assignments)
    for assignment in assignments:
        validate_assignment_identity(assignment)


def test_assignment_ids_are_deterministic_and_bind_lineage() -> None:
    model_manifest, evidence, snapshot, route = tranche()
    first = compile_tranche(model_manifest=model_manifest, evidence=evidence, snapshot=snapshot, route=route)
    second = compile_tranche(model_manifest=model_manifest, evidence=evidence, snapshot=snapshot, route=route)
    assert [item["assignment_id"] for item in first] == [item["assignment_id"] for item in second]

    first[0]["control_plane_binding"]["snapshot_generation"] += 1
    with pytest.raises(ValueError, match="assignment_id"):
        validate_assignment_identity(first[0])


def test_rejects_mixed_bundle_snapshot_or_route() -> None:
    model_manifest, evidence, snapshot, route = tranche()
    wrong_snapshot = copy.deepcopy(snapshot)
    wrong_snapshot["snapshot_generation"] += 1
    with pytest.raises(ValueError, match="snapshot_generation|snapshot_digest"):
        compile_tranche(model_manifest=model_manifest, evidence=evidence, snapshot=wrong_snapshot, route=route)

    wrong_route = copy.deepcopy(route)
    wrong_route["snapshot_digest"] = "sha256:" + "f" * 64
    with pytest.raises(ValueError, match="snapshot_digest"):
        compile_tranche(model_manifest=model_manifest, evidence=evidence, snapshot=snapshot, route=wrong_route)


def test_rejects_deployment_and_model_mismatch() -> None:
    model_manifest, evidence, snapshot, route = tranche()
    with pytest.raises(ValueError, match="deployment_id"):
        compile_tranche(
            model_manifest=model_manifest,
            evidence=evidence,
            snapshot=snapshot,
            route=route,
            deployment_id="87654321-4321-8765-9234-abcdefabcdef",
        )
    for field, value in {
        "model_id": "other/model",
        "num_layers": 5,
        "revision": "b" * 40,
        "weight_digest": "sha256:" + "f" * 64,
    }.items():
        changed = copy.deepcopy(route)
        changed["model"][field] = value
        with pytest.raises(ValueError, match=field):
            compile_tranche(
                model_manifest=model_manifest,
                evidence=evidence,
                snapshot=snapshot,
                route=changed,
            )


def test_rejects_tampered_evidence_bundle() -> None:
    model_manifest, evidence, snapshot, route = tranche()
    evidence["deployment"]["deployment_epoch"] += 1
    with pytest.raises(ValueError, match="digest"):
        compile_tranche(model_manifest=model_manifest, evidence=evidence, snapshot=snapshot, route=route)


def test_rejects_missing_or_ambiguous_legal_tracks() -> None:
    model_manifest, evidence, snapshot, route = tranche()
    for tracks in ([], copy.deepcopy(route["legal_tracks"]) * 2):
        changed = copy.deepcopy(route)
        changed["legal_tracks"] = tracks
        with pytest.raises(ValueError, match="exactly one legal track"):
            compile_tranche(
                model_manifest=model_manifest, evidence=evidence, snapshot=snapshot, route=changed
            )


def test_rejects_replica_nonprimary_and_untracked_placement() -> None:
    model_manifest, evidence, snapshot, route = tranche()
    replica = copy.deepcopy(route["placements"][0])
    replica.update({"placement_id": "replica-extra", "node_id": "node-c", "primary": False})
    route["placements"].append(replica)
    with pytest.raises(ValueError, match="replica"):
        compile_tranche(model_manifest=model_manifest, evidence=evidence, snapshot=snapshot, route=route)


def test_rejects_gap_overlap_and_duplicate_node() -> None:
    model_manifest, evidence, snapshot, route = tranche()
    assert len(route["placements"]) >= 2
    for first_end, second_start in ((1, 2), (2, 1)):
        changed = copy.deepcopy(route)
        changed["placements"][0]["layer_range"]["end"] = first_end
        changed["placements"][1]["layer_range"]["start"] = second_start
        with pytest.raises(ValueError, match="gap or overlap"):
            compile_tranche(
                model_manifest=model_manifest, evidence=evidence, snapshot=snapshot, route=changed
            )
    duplicate = copy.deepcopy(route)
    duplicate["placements"][1]["node_id"] = duplicate["placements"][0]["node_id"]
    with pytest.raises(ValueError, match="duplicate node"):
        compile_tranche(
            model_manifest=model_manifest, evidence=evidence, snapshot=snapshot, route=duplicate
        )
