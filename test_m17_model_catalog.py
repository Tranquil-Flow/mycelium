from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
import struct
import hashlib

import pytest

from mycelium_layer_planner.contracts import (
    NodeCapability,
    PlanningPolicy,
    WorkloadScenario,
)
from mycelium_model_catalog import (
    DirectedEdgeFeasibilityEvidence,
    NodeFeasibilityEvidence,
    MODEL_CATALOG_PROTOCOL,
    MODEL_FEASIBILITY_PROTOCOL,
    MODEL_OPERATION_PROTOCOL,
    SwarmFeasibilityEvidence,
    catalog_document,
    evaluate_model_feasibility,
    validate_feasibility_currency,
    model_operation_document,
    enrich_model_operation_lifecycle,
    scan_huggingface_cache,
    swarm_feasibility_evidence_from_document,
)
from mycelium_qualification.evidence import canonical_json_bytes
from mycelium_qualification.signing import generate_ed25519_signer
from scripts.assemble_m17_swarm_evidence import assemble


REVISION = "a" * 40
EVIDENCE_DIGEST = "sha256:" + "b" * 64


def _write(path: Path, value: str | bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(value, bytes):
        path.write_bytes(value)
    else:
        path.write_text(value, encoding="utf-8")


def _model_snapshot(
    root: Path,
    *,
    model_name: str = "models--Qwen--Qwen2.5-3B-Instruct",
    model_type: str = "qwen2",
    include_weight: bool = True,
    weight_bytes: int = 1_024,
) -> Path:
    model_root = root / model_name
    snapshot = model_root / "snapshots" / REVISION
    _write(model_root / "refs" / "main", REVISION)
    _write(
        snapshot / "config.json",
        json.dumps(
            {
                "architectures": ["Qwen2ForCausalLM"],
                "model_type": model_type,
                "num_hidden_layers": 4,
                "hidden_size": 64,
                "num_attention_heads": 4,
                "num_key_value_heads": 2,
                "torch_dtype": "bfloat16",
            }
        ),
    )
    _write(snapshot / "tokenizer.json", "{}")
    _write(
        snapshot / "model.safetensors.index.json",
        json.dumps(
            {
                "weight_map": {
                    f"model.layers.{layer}.self_attn.q_proj.weight": "model-00001-of-00001.safetensors"
                    for layer in range(4)
                }
            }
        ),
    )
    if include_weight:
        tensor_names = [
            f"model.layers.{layer}.self_attn.q_proj.weight"
            for layer in range(4)
        ]
        cursor = 0
        header: dict[str, object] = {}
        for index, name in enumerate(tensor_names):
            end = weight_bytes if index == len(tensor_names) - 1 else (index + 1) * weight_bytes // 4
            header[name] = {
                "dtype": "U8",
                "shape": [end - cursor],
                "data_offsets": [cursor, end],
            }
            cursor = end
        header_bytes = json.dumps(header, separators=(",", ":")).encode("utf-8")
        header_bytes += b" " * (-len(header_bytes) % 8)
        _write(
            snapshot / "model-00001-of-00001.safetensors",
            struct.pack("<Q", len(header_bytes)) + header_bytes + b"x" * weight_bytes,
        )
    return snapshot


def _node(node_id: str, memory: int) -> NodeCapability:
    return NodeCapability(
        node_id=node_id,
        prefill_ms_per_layer_token=0.1,
        decode_ms_per_layer_token=0.05,
        fast_memory_bytes=memory,
        total_memory_bytes=memory,
        memory_bandwidth_Bps=1_000_000_000,
        spill_bandwidth_Bps=1_000_000_000,
        workspace_bytes=64,
    )


def _workload() -> WorkloadScenario:
    return WorkloadScenario(
        name="interactive",
        prompt_tokens=8,
        output_tokens=4,
        concurrency=1,
    )


def _swarm_evidence(
    nodes: tuple[NodeCapability, ...],
    *,
    valid_until_unix_ms: int = 2_000,
    backend: str = "numpy",
    disk_free_bytes: int = 1_000_000,
    omit_edge: tuple[str, str] | None = None,
) -> SwarmFeasibilityEvidence:
    node_evidence = tuple(
        NodeFeasibilityEvidence(
            node_id=node.node_id,
            observation_digest="sha256:" + f"{index + 1:064x}",
            signature_digest="sha256:" + f"{index + 101:064x}",
            observed_at_unix_ms=900,
            valid_until_unix_ms=valid_until_unix_ms,
            backend=backend,
            supported_architectures=("qwen2", "qwen3"),
            supported_dtypes=("bfloat16",),
            supported_quantizations=("bfloat16",),
            supported_decode_modes=("complete_context_replay", "stage_local_kv"),
            decode_modes_by_architecture={
                "qwen2": ("complete_context_replay", "stage_local_kv"),
                "qwen3": ("complete_context_replay", "stage_local_kv"),
            },
            runtime_build_digest="sha256:" + f"{index + 201:064x}",
            available_memory_bytes=node.total_memory_bytes,
            rss_bytes=128,
            swap_used_bytes=0,
            disk_free_bytes=disk_free_bytes,
            cached_content_digests=(),
            thermal_state=None,
            power_state="external",
        )
        for index, node in enumerate(nodes)
    )
    required_pairs = [
        (nodes[index].node_id, nodes[index + 1].node_id)
        for index in range(len(nodes) - 1)
    ]
    if len(nodes) > 1:
        required_pairs.append((nodes[-1].node_id, nodes[0].node_id))
    edges = tuple(
        DirectedEdgeFeasibilityEvidence(
            src=src,
            dst=dst,
            observation_digest="sha256:" + f"{index + 301:064x}",
            observed_at_unix_ms=900,
            valid_until_unix_ms=valid_until_unix_ms,
            goodput_Bps=1_000_000,
            rtt_ms=10,
            jitter_ms=1,
            loss_ratio=0,
        )
        for index, (src, dst) in enumerate(required_pairs)
        if (src, dst) != omit_edge
    )
    return SwarmFeasibilityEvidence(
        generation=3,
        evidence_digest=EVIDENCE_DIGEST,
        signature_set_digest="sha256:" + "c" * 64,
        verification_key_set_digest="sha256:" + "d" * 64,
        placement_snapshot_generation=3,
        placement_digest="sha256:" + "e" * 64,
        topology_digest="sha256:" + "f" * 64,
        observed_at_unix_ms=900,
        valid_until_unix_ms=valid_until_unix_ms,
        nodes=node_evidence,
        directed_edges=edges,
    )


def _evaluate(
    entry: object,
    nodes: tuple[NodeCapability, ...],
    *,
    evidence: SwarmFeasibilityEvidence | None = None,
) -> dict[str, object]:
    return evaluate_model_feasibility(
        entry,  # type: ignore[arg-type]
        ordered_nodes=nodes,
        workload=_workload(),
        policy=PlanningPolicy(memory_reserve_fraction=0.1),
        evidence=evidence or _swarm_evidence(nodes),
        evaluated_at_unix_ms=1_000,
        required_decode_mode="stage_local_kv",
    )


def test_read_only_catalog_discovers_complete_runtime_compatible_snapshot(tmp_path: Path) -> None:
    snapshot = _model_snapshot(tmp_path)
    before = sorted(path.relative_to(tmp_path) for path in tmp_path.rglob("*"))

    entries = scan_huggingface_cache(tmp_path)
    document = catalog_document(entries, generation=1)

    after = sorted(path.relative_to(tmp_path) for path in tmp_path.rglob("*"))
    assert before == after
    assert len(entries) == 1
    assert entries[0].state == "compatible"
    assert entries[0].weight_bytes == 1_024
    assert document["protocol"] == MODEL_CATALOG_PROTOCOL
    assert document["download_policy"] == "operator_approval_required"
    serialized = json.dumps(document)
    assert str(snapshot) not in serialized
    assert str(tmp_path) not in serialized


def test_incomplete_snapshot_names_missing_shard_and_never_repairs_it(tmp_path: Path) -> None:
    snapshot = _model_snapshot(tmp_path, include_weight=False)

    (entry,) = scan_huggingface_cache(tmp_path)

    assert entry.state == "incomplete"
    assert "missing_required_file:model-00001-of-00001.safetensors" in entry.reasons
    assert not (snapshot / "model-00001-of-00001.safetensors").exists()


def test_unsupported_runtime_family_is_discovered_but_not_compatible(tmp_path: Path) -> None:
    _model_snapshot(tmp_path, model_type="llama")

    (entry,) = scan_huggingface_cache(tmp_path)

    assert entry.state == "discovered"
    assert "runtime_adapter_unavailable:llama" in entry.reasons


def test_catalog_is_deterministic_and_separates_snapshot_revisions(tmp_path: Path) -> None:
    _model_snapshot(tmp_path)
    model_root = tmp_path / "models--Qwen--Qwen2.5-3B-Instruct"
    second = model_root / "snapshots" / ("c" * 40)
    source = model_root / "snapshots" / REVISION
    for path in source.iterdir():
        _write(second / path.name, path.read_bytes())

    first = catalog_document(scan_huggingface_cache(tmp_path), generation=7)
    second_document = catalog_document(scan_huggingface_cache(tmp_path), generation=7)

    assert first == second_document
    assert len(first["entries"]) == 2
    assert first["entries"][0]["artifact_digest"] != first["entries"][1]["artifact_digest"]


def test_feasibility_uses_existing_contiguous_dp_and_does_not_authorize_provisioning(
    tmp_path: Path,
) -> None:
    _model_snapshot(tmp_path, weight_bytes=4_096)
    (entry,) = scan_huggingface_cache(tmp_path)

    nodes = (_node("node-a", 8_192), _node("node-b", 8_192))
    report = _evaluate(entry, nodes)

    assert report["protocol"] == MODEL_FEASIBILITY_PROTOCOL
    assert report["planner"] == "capability_aware_contiguous_exact_weight_dp"
    assert report["state"] == "feasible"
    assert [(stage["start_layer"], stage["end_layer_exclusive"]) for stage in report["stages"]] == [
        (0, 2),
        (2, 4),
    ]
    assert report["provisioning_authorized"] is True
    assert report["route_ready"] is False
    assert report["maximum_qualified_context_tokens"] >= 12
    assert report["maximum_qualified_concurrency"] >= 1
    assert report["cached_artifact_bytes"] == 0
    assert report["missing_artifact_bytes"] > 0
    assert report["modeled_transfer_ms"] > 0
    assert report["resource_bottleneck"]["kind"] in {"memory", "disk", "directed_edge"}


def test_model_one_byte_over_total_swarm_capacity_fails_before_provisioning(
    tmp_path: Path,
) -> None:
    _model_snapshot(tmp_path, weight_bytes=10_001)
    (entry,) = scan_huggingface_cache(tmp_path)

    nodes = (_node("node-a", 5_000), _node("node-b", 5_000))
    report = evaluate_model_feasibility(
        entry,
        ordered_nodes=nodes,
        workload=_workload(),
        policy=PlanningPolicy(memory_reserve_fraction=0),
        evidence=_swarm_evidence(nodes),
        evaluated_at_unix_ms=1_000,
        required_decode_mode="stage_local_kv",
    )

    assert report["state"] == "infeasible"
    assert report["stages"] == []
    assert report["provisioning_authorized"] is False
    assert "no_feasible_contiguous_exact_weight_allocation" in report["reasons"]


def test_incompatible_model_is_rejected_without_running_planner(tmp_path: Path) -> None:
    _model_snapshot(tmp_path, model_type="llama")
    (entry,) = scan_huggingface_cache(tmp_path)

    nodes = (_node("node-a", 100_000),)
    report = _evaluate(entry, nodes)

    assert report["state"] == "infeasible"
    assert "runtime_adapter_unavailable:llama" in report["reasons"]


def test_model_operation_binds_catalog_and_feasibility_without_promoting_it(
    tmp_path: Path,
) -> None:
    _model_snapshot(tmp_path)
    (entry,) = scan_huggingface_cache(tmp_path)
    catalog = catalog_document((entry,), generation=4)
    nodes = (_node("node-a", 20_000),)
    feasibility = _evaluate(entry, nodes)

    operation = model_operation_document(catalog, (feasibility,))

    assert operation["protocol"] == MODEL_OPERATION_PROTOCOL
    assert operation["catalog_digest"] == catalog["catalog_digest"]
    assert operation["selection_authority"] == "qualified_deployment_registry"
    assert operation["route_ready"] is False
    assert operation["lifecycle"]["models"][0]["state"] == "feasible"
    assert operation["lifecycle"]["models"][0]["selectable"] is False


def test_registry_alone_promotes_qualified_and_active_lifecycle_states(
    tmp_path: Path,
) -> None:
    _model_snapshot(tmp_path)
    (entry,) = scan_huggingface_cache(tmp_path)
    catalog = catalog_document((entry,), generation=4)
    nodes = (_node("node-a", 20_000),)
    feasibility = _evaluate(entry, nodes)
    operation = model_operation_document(catalog, (feasibility,))
    deployment_id = "12345678-1234-5678-9234-abcdefabcdef"

    enriched = enrich_model_operation_lifecycle(
        operation,
        {
            "protocol": "mycelium.live_deployment_registry.v1",
            "selected_deployment_id": deployment_id,
            "switching_allowed": True,
            "deployments": [
                {
                    "deployment_id": deployment_id,
                    "model_id": entry.model_id,
                    "model_revision": entry.revision,
                    "health": "qualified",
                    "qualification_id": "sha256:" + "d" * 64,
                }
            ],
        },
    )

    lifecycle = enriched["lifecycle"]["models"][0]
    assert lifecycle["state"] == "active"
    assert lifecycle["authority"] == "qualified_deployment_registry"
    assert lifecycle["active_deployment_id"] == deployment_id
    assert lifecycle["selectable"] is True
    assert enriched["operation_digest"] != operation["operation_digest"]


def test_model_operation_rejects_duplicate_feasibility_identity(tmp_path: Path) -> None:
    _model_snapshot(tmp_path)
    (entry,) = scan_huggingface_cache(tmp_path)
    catalog = catalog_document((entry,), generation=4)
    nodes = (_node("node-a", 20_000),)
    feasibility = _evaluate(entry, nodes)

    with pytest.raises(ValueError, match="duplicate"):
        model_operation_document(catalog, (feasibility, feasibility))


def test_stale_signed_swarm_evidence_rejects_before_allocation(tmp_path: Path) -> None:
    _model_snapshot(tmp_path)
    (entry,) = scan_huggingface_cache(tmp_path)
    nodes = (_node("node-a", 20_000),)

    report = _evaluate(
        entry,
        nodes,
        evidence=_swarm_evidence(nodes, valid_until_unix_ms=999),
    )

    assert report["state"] == "infeasible"
    assert report["stages"] == []
    assert report["provisioning_authorized"] is False
    assert "stale_swarm_evidence" in report["reasons"]


def test_backend_quantization_and_disk_pressure_fail_closed(tmp_path: Path) -> None:
    _model_snapshot(tmp_path)
    (entry,) = scan_huggingface_cache(tmp_path)
    nodes = (_node("node-a", 100_000),)

    backend = _evaluate(entry, nodes, evidence=_swarm_evidence(nodes, backend="cuda"))
    disk = _evaluate(
        entry,
        nodes,
        evidence=_swarm_evidence(nodes, disk_free_bytes=1),
    )

    assert "backend_unsupported:node-a:cuda" in backend["reasons"]
    assert "insufficient_disk:node-a" in disk["reasons"]
    assert backend["provisioning_authorized"] is False
    assert disk["provisioning_authorized"] is False


def test_missing_required_directed_loopback_rejects_complete_track(tmp_path: Path) -> None:
    _model_snapshot(tmp_path)
    (entry,) = scan_huggingface_cache(tmp_path)
    nodes = (_node("node-a", 8_192), _node("node-b", 8_192))

    report = _evaluate(
        entry,
        nodes,
        evidence=_swarm_evidence(nodes, omit_edge=("node-b", "node-a")),
    )

    assert report["state"] == "infeasible"
    assert "missing_directed_edge:node-b->node-a" in report["reasons"]


def test_decode_mode_is_checked_for_the_candidate_architecture(tmp_path: Path) -> None:
    _model_snapshot(tmp_path)
    (entry,) = scan_huggingface_cache(tmp_path)
    nodes = (_node("node-a", 20_000),)
    evidence = _swarm_evidence(nodes)
    constrained = replace(
        evidence,
        nodes=(
            replace(
                evidence.nodes[0],
                decode_modes_by_architecture={
                    "qwen2": ("complete_context_replay",),
                    "qwen3": ("complete_context_replay", "stage_local_kv"),
                },
            ),
        ),
    )

    report = _evaluate(entry, nodes, evidence=constrained)

    assert report["state"] == "infeasible"
    assert "decode_mode_unsupported:node-a:stage_local_kv" in report["reasons"]


def test_capability_drift_invalidates_a_previously_feasible_report(tmp_path: Path) -> None:
    _model_snapshot(tmp_path)
    (entry,) = scan_huggingface_cache(tmp_path)
    nodes = (_node("node-a", 20_000),)
    evidence = _swarm_evidence(nodes)
    report = _evaluate(entry, nodes, evidence=evidence)
    drifted = SwarmFeasibilityEvidence(
        generation=evidence.generation + 1,
        evidence_digest="sha256:" + "e" * 64,
        signature_set_digest=evidence.signature_set_digest,
        verification_key_set_digest=evidence.verification_key_set_digest,
        placement_snapshot_generation=evidence.placement_snapshot_generation,
        placement_digest=evidence.placement_digest,
        topology_digest=evidence.topology_digest,
        observed_at_unix_ms=evidence.observed_at_unix_ms,
        valid_until_unix_ms=evidence.valid_until_unix_ms,
        nodes=evidence.nodes,
        directed_edges=evidence.directed_edges,
    )

    validate_feasibility_currency(report, evidence, evaluated_at_unix_ms=1_100)
    with pytest.raises(ValueError, match="capability_evidence_drift"):
        validate_feasibility_currency(report, drifted, evaluated_at_unix_ms=1_100)


def test_signed_live_resource_observation_round_trips_to_closed_swarm_evidence() -> None:
    signer = generate_ed25519_signer(endpoint_id="endpoint-a")
    resources = {
        "protocol": "mycelium.host_resource_snapshot.v1",
        "observed_at_unix_ms": 900,
        "valid_until_unix_ms": 2_000,
        "backend": "numpy",
        "supported_architectures": ["qwen2", "qwen3"],
        "supported_dtypes": ["bfloat16"],
        "supported_quantizations": ["bfloat16"],
        "supported_decode_modes": ["complete_context_replay"],
        "runtime_build_digest": "sha256:" + "a" * 64,
        "available_memory_bytes": 10_000,
        "rss_bytes": 1_000,
        "swap_used_bytes": 0,
        "disk_free_bytes": 20_000,
        "disk_total_bytes": 40_000,
        "cached_content_digests": [],
        "thermal_state": None,
        "power_state": "external",
        "route_ready": False,
    }
    resources["resource_digest"] = "sha256:" + hashlib.sha256(
        canonical_json_bytes(resources)
    ).hexdigest()
    observation = {
        "protocol": "mycelium.physical_node_observation.v1",
        "event": "snapshot",
        "monotonic_ns": 1,
        "run_id": "run",
        "deployment_id": "deployment",
        "node_id": "node-a",
        "host_id": "host",
        "process_id": 1,
        "endpoint_id": "endpoint-a",
        "peer_generation": 1,
        "state": "RUNNING",
        "route_ready": False,
        "details": {
            "host_resources": resources,
            "transport": {"transport_path_observations": []},
        },
    }
    signed = {
        "observation": observation,
        "signature": signer.sign(observation),
        "verification_key": signer.public_key_record(),
    }
    source = {
        "protocol": "mycelium.live_swarm_resource_observations.v1",
        "placement": {"snapshot_generation": 3, "valid_until_unix_ms": 2_000},
        "topology": {"decision": {"opened_order": ["node-a"]}},
        "signed_snapshots": [signed],
    }

    document = assemble(source)
    decoded = swarm_feasibility_evidence_from_document(document)

    assert decoded.generation == 900
    assert decoded.placement_snapshot_generation == 3
    assert decoded.nodes[0].node_id == "node-a"
    assert decoded.directed_edges == ()
    tampered = json.loads(json.dumps(source))
    tampered["signed_snapshots"][0]["observation"]["details"]["host_resources"][
        "available_memory_bytes"
    ] += 1
    with pytest.raises(ValueError, match="signature verification failed"):
        assemble(tampered)


@pytest.mark.parametrize("generation", [0, -1, True])
def test_catalog_rejects_invalid_generation(tmp_path: Path, generation: object) -> None:
    with pytest.raises(ValueError, match="positive exact integer"):
        catalog_document(scan_huggingface_cache(tmp_path), generation=generation)  # type: ignore[arg-type]
