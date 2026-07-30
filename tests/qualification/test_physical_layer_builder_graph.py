"""Tests for the production layer-builder graph delegation path (TDD RED phase).

The existing ``physical_deployment.build_execution_graph`` is a deliberately
two-assignment scaffolding function bound by existing qualification tests.
This test file validates the *separate* delegation path that invokes the
production ``mycelium_router.layer_builder.build_execution_graph`` via a
control-plane tranche.

Pipeline under test:
    control-plane tranche + load proofs + runtime endpoints
      -> physical_deployment.build_execution_graph_from_tranche
      -> mycelium_router.layer_builder.build_execution_graph
"""

from __future__ import annotations

import copy
import json
from typing import Any

import pytest

import model_manifest as mm
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
from mycelium_qualification.physical_deployment import (
    PhysicalDeploymentError,
    build_execution_graph_from_tranche,
)
from mycelium_router.contracts import ExecutionGraph
from mycelium_router.layer_builder import LAYER_LOAD_PROOF_PROTOCOL
from planner_assignment import compile_bound_layer_assignments
from tests.gossip.helpers import link_payload, make_record, profile_payload, status_payload

DEPLOYMENT_ID = "12345678-1234-5678-9234-abcdefabcdef"
PERFORMANCE = {
    "prefill_ms_per_layer_token": 0.001,
    "decode_ms_per_layer_token": 0.001,
    "memory_bandwidth_Bps": 1_000_000_000,
    "spill_bandwidth_Bps": 1_000_000_000,
    "calibration_confidence": 1.0,
}
WORKLOAD = {"preset": "interactive_chat_v1", "concurrency_points": [1], "user_scale": 1}
POLICY = {
    "memory_reserve_fraction": 0,
    "replica_budget": 0,
    "ttft_slo_ms": 1_000_000,
    "tpot_slo_ms": 1_000_000,
}
RUNTIME = {"backend": "mlx", "dtype": "float16", "quantization": "none"}


class Clock:
    def __call__(self) -> float:
        return 100.0


def _model_manifest() -> dict[str, Any]:
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


def _planner_model(manifest: dict[str, Any]) -> dict[str, Any]:
    return {
        "model_id": manifest["model_id"],
        "revision": manifest["resolved_commit"],
        "weight_digest": mm.manifest_digest_ref(manifest),
        "architecture": "Decoder",
        "num_layers": manifest["num_layers"],
        "hidden_size": 128,
        "dtype_bytes": 2,
        "kv_heads": 4,
        "head_dim": 32,
        "weight_bytes": 60_000_000,
    }


def _build_evidence_bundle(manifest: dict[str, Any], *, epoch: int = 3):
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
        store.apply(
            make_record(RecordKind.PROFILE, node_id=node_id, ttl_ms=10_000, payload=profile_payload(node_id))
        )
        store.apply(make_record(RecordKind.STATUS, node_id=node_id, ttl_ms=10_000, payload=status))
        service.submit_liveness(
            LivenessEvent(LivenessKind.PUT, "swarm-a", node_id, 1, f"boot-{node_id}-1", clock())
        )
    for src, dst in (("node-a", "node-b"), ("node-b", "node-a")):
        store.apply(
            make_record(RecordKind.LINK, node_id=src, ttl_ms=10_000, payload=link_payload(src, dst))
        )
    service.drain()
    bundle = service.capture_evidence_bundle(
        deployment_id=DEPLOYMENT_ID,
        deployment_epoch=epoch,
        model_id=manifest["model_id"],
        num_layers=manifest["num_layers"],
        manifest_digest=mm.manifest_digest_ref(manifest),
        resolved_commit=manifest["resolved_commit"],
    )
    return evidence_bundle_to_dict(bundle)


def _build_tranche(manifest: dict[str, Any] | None = None):
    manifest = manifest or _model_manifest()
    evidence = _build_evidence_bundle(manifest)
    model = _planner_model(manifest)
    snapshot = planner_snapshot_from_evidence_bundle(
        evidence, model=model, workload=WORKLOAD, policy=POLICY
    )
    route = json.loads(json.dumps(route_plan_to_dict(
        plan_evidence_bundle(evidence, model=model, workload=WORKLOAD, policy=POLICY)
    )))
    nodes = [placement["node_id"] for placement in route["placements"]]
    assignments = compile_bound_layer_assignments(
        route_plan=route,
        planner_snapshot=snapshot,
        evidence_bundle=evidence,
        manifest=manifest,
        deployment_id=evidence["deployment"]["deployment_id"],
        deployment_epoch=evidence["deployment"]["deployment_epoch"],
        cache_roots={node: f"/var/lib/mycelium/{node}" for node in nodes},
        runtime_by_node={node: RUNTIME for node in nodes},
    )
    tranche = {
        "protocol": "mycelium.control_plane_tranche.v1",
        "evidence_bundle": evidence,
        "planner_snapshot": snapshot,
        "route_plan": route,
        "assignments": assignments,
    }
    return manifest, tranche


def _build_load_proofs(assignments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    import hashlib

    proofs = []
    for assignment in assignments:
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
            json.dumps(loaded_identity, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
        ).hexdigest()
        probe_digest = hashlib.sha256(
            f"test-probe:{assignment['assignment_id']}".encode("utf-8")
        ).hexdigest()
        proofs.append(
            {
                "protocol": LAYER_LOAD_PROOF_PROTOCOL,
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
                "resolved_component_aliases": {},
                "runtime": assignment["runtime"],
                "runtime_identity": {
                    "backend": "mlx",
                    "backend_version": "test",
                    "device": "gpu",
                    "dtype": assignment["runtime"]["dtype"],
                    "quantization": assignment["runtime"]["quantization"],
                    "architecture": assignment["runtime"]["architecture"],
                },
                "control_plane_binding": assignment["control_plane_binding"],
                "probe_shape": [1, 3, probe_width],
                "probe_digest": f"sha256:{probe_digest}",
                "load_generation": 1,
                "route_ready": False,
                "claim_boundary": (
                    "test-only local stage loaded and probed; no route challenge"
                ),
            }
        )
    return proofs


def _runtime_endpoints(assignments: list[dict[str, Any]]) -> dict[str, str]:
    return {
        a["assignment_id"]: f"iroh://{a['node_id']}/endpoint-{i}"
        for i, a in enumerate(assignments)
    }


# ---------------------------------------------------------------------------
# Happy path: production layer-builder graph builds from a valid tranche.
# ---------------------------------------------------------------------------


def test_builds_execution_graph_from_valid_tranche() -> None:
    manifest, tranche = _build_tranche()
    proofs = _build_load_proofs(tranche["assignments"])
    endpoints = _runtime_endpoints(tranche["assignments"])
    graph = build_execution_graph_from_tranche(
        tranche,
        proofs,
        manifest=manifest,
        runtime_endpoints=endpoints,
        topology_version=7,
        token_envelope_bytes=9,
    )
    assert isinstance(graph, ExecutionGraph)
    assert graph.topology_version == 7
    assert graph.token_envelope_bytes == 9
    assert len(graph.stages) == len(tranche["assignments"])
    assert len(graph.edges) >= 1
    assert len(graph.loopback_edges) == 1


# ---------------------------------------------------------------------------
# Missing load proof rejects.
# ---------------------------------------------------------------------------


def test_missing_load_proof_rejects() -> None:
    manifest, tranche = _build_tranche()
    proofs = _build_load_proofs(tranche["assignments"])
    # Drop one proof.
    incomplete = proofs[:-1]
    endpoints = _runtime_endpoints(tranche["assignments"])
    with pytest.raises((PhysicalDeploymentError, ValueError)):
        build_execution_graph_from_tranche(
            tranche,
            incomplete,
            manifest=manifest,
            runtime_endpoints=endpoints,
            topology_version=1,
            token_envelope_bytes=9,
        )


# ---------------------------------------------------------------------------
# Runtime endpoint / assignment mismatch rejects.
# ---------------------------------------------------------------------------


def test_runtime_endpoint_assignment_mismatch_rejects() -> None:
    manifest, tranche = _build_tranche()
    proofs = _build_load_proofs(tranche["assignments"])
    # Add an endpoint for an unknown assignment.
    endpoints = _runtime_endpoints(tranche["assignments"])
    endpoints["unknown-assignment"] = "iroh://unknown/endpoint"
    with pytest.raises((PhysicalDeploymentError, ValueError)):
        build_execution_graph_from_tranche(
            tranche,
            proofs,
            manifest=manifest,
            runtime_endpoints=endpoints,
            topology_version=1,
            token_envelope_bytes=9,
        )


def test_missing_runtime_endpoint_rejects() -> None:
    manifest, tranche = _build_tranche()
    proofs = _build_load_proofs(tranche["assignments"])
    # Remove one endpoint.
    endpoints = _runtime_endpoints(tranche["assignments"])
    del endpoints[next(iter(endpoints))]
    with pytest.raises((PhysicalDeploymentError, ValueError)):
        build_execution_graph_from_tranche(
            tranche,
            proofs,
            manifest=manifest,
            runtime_endpoints=endpoints,
            topology_version=1,
            token_envelope_bytes=9,
        )


# ---------------------------------------------------------------------------
# Stale epoch in tranche rejects.
# ---------------------------------------------------------------------------


def test_stale_epoch_in_tranche_rejects() -> None:
    manifest, tranche = _build_tranche()
    tampered = copy.deepcopy(tranche)
    tampered["evidence_bundle"]["deployment"]["deployment_epoch"] += 1
    proofs = _build_load_proofs(tranche["assignments"])
    endpoints = _runtime_endpoints(tranche["assignments"])
    with pytest.raises((PhysicalDeploymentError, ValueError)):
        build_execution_graph_from_tranche(
            tampered,
            proofs,
            manifest=manifest,
            runtime_endpoints=endpoints,
            topology_version=1,
            token_envelope_bytes=9,
        )


# ---------------------------------------------------------------------------
# Invalid topology_version rejects.
# ---------------------------------------------------------------------------


def test_invalid_topology_version_rejects() -> None:
    manifest, tranche = _build_tranche()
    proofs = _build_load_proofs(tranche["assignments"])
    endpoints = _runtime_endpoints(tranche["assignments"])
    with pytest.raises((PhysicalDeploymentError, ValueError)):
        build_execution_graph_from_tranche(
            tranche,
            proofs,
            manifest=manifest,
            runtime_endpoints=endpoints,
            topology_version=-1,
            token_envelope_bytes=9,
        )


# ---------------------------------------------------------------------------
# Existing build_execution_graph is untouched.
# ---------------------------------------------------------------------------


def test_existing_build_execution_graph_unchanged() -> None:
    """The existing two-assignment build_execution_graph must remain available
    and unmodified. We import it and verify it still exists with the same
    parameter signature."""
    from mycelium_qualification.physical_deployment import build_execution_graph
    import inspect

    sig = inspect.signature(build_execution_graph)
    params = list(sig.parameters)
    assert "assignments" in params
    assert "proofs" in params
    assert "link_scheme" in params
    assert "runtime_scheme" in params
