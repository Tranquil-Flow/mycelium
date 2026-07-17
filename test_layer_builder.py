import copy
import hashlib
import json
from pathlib import Path

import pytest

from mycelium_router.layer_builder import (
    LayerBuildError,
    build_execution_graph,
    device_states_from_evidence_bundle,
    layer_load_proof_digest,
)
from mycelium_router.serialization import (
    execution_graph_from_dict,
    execution_graph_to_dict,
)
from scripts.generate_contract_fixtures import model_manifest as generated_model_manifest


ROOT = Path(__file__).parent
TRANCHE_PATH = ROOT / "contracts" / "compatibility-fixtures" / "control-plane-tranche-v1.json"


def _load_tranche():
    return json.loads(TRANCHE_PATH.read_text(encoding="utf-8"))


def _load_manifest():
    return generated_model_manifest()


def _proof(assignment):
    backend = assignment["runtime"]["backend"]
    return {
        "protocol": "mycelium.layer_load_proof.v1",
        "deployment_id": assignment["deployment_id"],
        "deployment_epoch": assignment["deployment_epoch"],
        "assignment_id": assignment["assignment_id"],
        "node_id": assignment["node_id"],
        "model_id": assignment["model_id"],
        "manifest_digest": assignment["manifest_digest"],
        "resolved_commit": assignment["resolved_commit"],
        "loaded_range": copy.deepcopy(assignment["range"]),
        "loaded_components": copy.deepcopy(assignment["components"]),
        "loaded_tensor_keys": copy.deepcopy(assignment["expected_tensor_keys"]),
        "loaded_tensor_digest": "sha256:" + "1" * 64,
        "resolved_component_aliases": copy.deepcopy(
            assignment.get("component_aliases", {})
        ),
        "runtime": copy.deepcopy(assignment["runtime"]),
        "runtime_identity": {
            "backend": backend,
            "backend_version": "test",
            "device": "test-device",
            "dtype": assignment["runtime"]["dtype"],
            "quantization": assignment["runtime"]["quantization"],
        },
        "probe_shape": [1, 2, 128],
        "probe_digest": "sha256:" + "2" * 64,
        "load_generation": 1,
        "control_plane_binding": copy.deepcopy(
            assignment["control_plane_binding"]
        ),
        "route_ready": False,
        "claim_boundary": (
            "assignment-bound local stage loaded and deterministically probed; "
            "no route challenge or distributed inference claim"
        ),
    }


def _proofs(tranche):
    return [_proof(assignment) for assignment in tranche["assignments"]]


def _endpoints(tranche):
    return {
        assignment["assignment_id"]: f"memory://{assignment['node_id']}/stage"
        for assignment in tranche["assignments"]
    }


def _build(tranche=None, proofs=None, endpoints=None):
    tranche = tranche or _load_tranche()
    return build_execution_graph(
        tranche,
        proofs or _proofs(tranche),
        manifest=_load_manifest(),
        runtime_endpoints=endpoints or _endpoints(tranche),
        topology_version=9,
        token_envelope_bytes=8,
    )


def test_builds_deterministic_router_graph_from_bound_control_plane():
    tranche = _load_tranche()
    proofs = _proofs(tranche)

    first = _build(tranche, proofs)
    second = _build(tranche, copy.deepcopy(proofs))

    assert execution_graph_to_dict(first) == execution_graph_to_dict(second)
    assert first.deployment_id == tranche["assignments"][0]["deployment_id"]
    assert first.deployment_epoch == tranche["assignments"][0]["deployment_epoch"]
    assert first.topology_version == 9
    assert first.hidden_size == 128
    assert first.activation_bytes == 2
    assert first.token_envelope_bytes == 8
    assert [stage.stage_id for stage in first.stages] == ["stage-000", "stage-001"]
    assert [
        (stage.layer_range.start_layer, stage.layer_range.end_layer_exclusive)
        for stage in first.stages
    ] == [(0, 2), (2, 4)]
    assert first.stages[0].component_roles == ("input_embedding", "decoder")
    assert first.stages[1].component_roles == ("decoder", "final_norm", "lm_head")
    assert [placement.placement_id for stage in first.stages for placement in stage.placements] == [
        "stage-000-primary",
        "stage-001-primary",
    ]
    assert [placement.load_proof_digest for stage in first.stages for placement in stage.placements] == [
        layer_load_proof_digest(proof) for proof in proofs
    ]
    assert len(first.edges) == 1
    assert len(first.loopback_edges) == 1
    assert first.edges[0].link_id.startswith("link:node-a/http-overlay->node-b/http-overlay@sha256:")
    assert first.loopback_edges[0].link_id.startswith("link:node-b/http-overlay->node-a/http-overlay@sha256:")


def test_graph_round_trips_through_production_router_codec():
    graph = _build()
    document = json.loads(json.dumps(execution_graph_to_dict(graph)))
    assert execution_graph_from_dict(document) == graph


def test_stage_costs_preserve_exact_layer_and_kv_footprints():
    tranche = _load_tranche()
    graph = _build(tranche)

    first = graph.stages[0].stage_cost
    assert first.prefill_work_units_per_prompt_token == 2.0
    assert first.decode_work_units_per_token == 2.0
    # 2 (K/V) * 2 layers * 2 KV heads * 32 head dim * 2 bytes.
    assert first.kv_bytes_per_context_token == 512


def test_device_state_adapter_uses_same_atomic_evidence_generation():
    tranche = _load_tranche()
    states = device_states_from_evidence_bundle(tranche["evidence_bundle"])

    assert set(states) == {"node-a", "node-b"}
    assert states["node-a"].availability == "ALIVE"
    assert states["node-a"].compute_units_per_second == 1_000_000.0
    assert states["node-a"].free_compute_fraction == 1.0
    assert states["node-a"].available_kv_bytes == 34_359_738_368
    assert states["node-a"].pending_hop_queue_depth == 0
    assert states["node-a"].neighbor_rtt_ms == {"node-b": 1.8}
    assert states["node-a"].neighbor_bandwidth_bytes_per_second == {
        "node-b": 112_500_000.0
    }


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("protocol", "mycelium.layer_load_proof.v2", "unsupported_load_proof_protocol"),
        ("node_id", "wrong", "load_proof_node_id_mismatch"),
        ("manifest_digest", "sha256:" + "f" * 64, "load_proof_manifest_digest_mismatch"),
        ("route_ready", True, "load_proof_claim_boundary_violation"),
        ("loaded_tensor_digest", "bad", "invalid_loaded_tensor_digest"),
        ("probe_digest", "bad", "invalid_probe_digest"),
    ],
)
def test_load_proof_identity_and_claim_boundary_fail_closed(field, value, message):
    tranche = _load_tranche()
    proofs = _proofs(tranche)
    proofs[0][field] = value

    with pytest.raises(LayerBuildError, match=message):
        _build(tranche, proofs)


@pytest.mark.parametrize(
    ("field", "message"),
    [
        ("loaded_range", "load_proof_loaded_range_mismatch"),
        ("loaded_components", "load_proof_loaded_components_mismatch"),
        ("loaded_tensor_keys", "load_proof_loaded_tensor_keys_mismatch"),
        ("runtime", "load_proof_runtime_mismatch"),
        ("control_plane_binding", "load_proof_control_plane_binding_mismatch"),
    ],
)
def test_nested_load_proof_binding_fails_closed(field, message):
    tranche = _load_tranche()
    proofs = _proofs(tranche)
    value = proofs[0][field]
    if isinstance(value, list):
        value = list(reversed(value))
    else:
        value = copy.deepcopy(value)
        value[next(iter(value))] = "tampered"
    proofs[0][field] = value

    with pytest.raises(LayerBuildError, match=message):
        _build(tranche, proofs)


def test_missing_duplicate_and_unknown_load_proofs_fail_closed():
    tranche = _load_tranche()
    proofs = _proofs(tranche)

    with pytest.raises(LayerBuildError, match="missing_load_proof"):
        _build(tranche, proofs[:1])

    with pytest.raises(LayerBuildError, match="duplicate_load_proof"):
        _build(tranche, [proofs[0], proofs[0]])

    unknown = copy.deepcopy(proofs[0])
    unknown["assignment_id"] = "unknown-assignment"
    with pytest.raises(LayerBuildError, match="unknown_load_proof_assignment"):
        _build(tranche, proofs + [unknown])


def test_missing_or_secret_bearing_runtime_endpoint_fails_closed():
    tranche = _load_tranche()
    endpoints = _endpoints(tranche)
    endpoints.pop(tranche["assignments"][0]["assignment_id"])
    with pytest.raises(LayerBuildError, match="missing_runtime_endpoint"):
        _build(tranche, endpoints=endpoints)

    endpoints = _endpoints(tranche)
    endpoints[tranche["assignments"][0]["assignment_id"]] = (
        "https://user:secret@example.test/stage?token=secret"
    )
    with pytest.raises(LayerBuildError, match="unsafe_runtime_endpoint"):
        _build(tranche, endpoints=endpoints)


def test_ambiguous_or_ineligible_physical_link_fails_closed():
    tranche = _load_tranche()
    duplicate = copy.deepcopy(tranche["evidence_bundle"]["router_view"]["edges"][0])
    duplicate["src_endpoint_id"] = "second"
    tranche["evidence_bundle"]["router_view"]["edges"].append(duplicate)
    with pytest.raises((LayerBuildError, ValueError)):
        _build(tranche)

    tranche = _load_tranche()
    tranche["evidence_bundle"]["router_view"]["edges"][0]["eligible"] = False
    with pytest.raises((LayerBuildError, ValueError)):
        _build(tranche)


def test_topology_and_token_envelope_parameters_fail_closed():
    tranche = _load_tranche()
    proofs = _proofs(tranche)
    endpoints = _endpoints(tranche)
    with pytest.raises(LayerBuildError, match="invalid_topology_version"):
        build_execution_graph(
            tranche,
            proofs,
            manifest=_load_manifest(),
            runtime_endpoints=endpoints,
            topology_version=-1,
            token_envelope_bytes=8,
        )
    with pytest.raises(LayerBuildError, match="invalid_token_envelope_bytes"):
        build_execution_graph(
            tranche,
            proofs,
            manifest=_load_manifest(),
            runtime_endpoints=endpoints,
            topology_version=1,
            token_envelope_bytes=-1,
        )


def test_load_proof_digest_is_canonical_json_and_mutation_sensitive():
    tranche = _load_tranche()
    proof = _proofs(tranche)[0]
    reordered = {key: proof[key] for key in reversed(proof)}
    assert layer_load_proof_digest(reordered) == layer_load_proof_digest(proof)

    mutated = copy.deepcopy(proof)
    mutated["load_generation"] += 1
    assert layer_load_proof_digest(mutated) != layer_load_proof_digest(proof)
    assert layer_load_proof_digest(proof) == "sha256:" + hashlib.sha256(
        json.dumps(proof, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
