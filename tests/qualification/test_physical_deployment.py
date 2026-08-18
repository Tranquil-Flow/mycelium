from __future__ import annotations

import copy
import json
import stat
from pathlib import Path
from typing import Any

import pytest
import mlx.core as mx

import model_manifest
from mycelium_qualification.physical_deployment import (
    LocalModelSource,
    PhysicalDeploymentError,
    build_execution_graph,
    build_replicated_execution_graph,
    build_physical_device_states,
    compile_local_model_manifest,
    prepare_physical_deployment,
)
from layer_assignment import assignment_id_for
from runtime_loader import RuntimeLoadError, execute_loaded_stage, load_assignment_stage
from two_process_runtime_qualification import (
    SHARD_NAMES,
    _build_local_model,
    _canonical_json,
    _layer_tensors,
    _model_config,
    _values,
)
from weight_provisioning import sha256_file
import weight_provisioning


EXPECTED_RANGES = (
    {"start_layer": 0, "end_layer_exclusive": 1, "layer_count": 1},
    {"start_layer": 1, "end_layer_exclusive": 2, "layer_count": 1},
)


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(_canonical_json(payload) + "\n", encoding="utf-8")


def _write_dialogpt_style_monolithic_source(root: Path, *, n_layer: int = 5) -> None:
    root.mkdir()
    config = _model_config(n_positions=16)
    config["n_layer"] = n_layer
    config["tie_word_embeddings"] = True
    tensors = {
        "transformer.wte.weight": _values((7, 4), offset=1, scale=0.01),
        "transformer.wpe.weight": _values((16, 4), offset=2, scale=0.005),
        **{
            key: value
            for layer in range(n_layer)
            for key, value in _layer_tensors(layer).items()
        },
        "transformer.ln_f.weight": mx.ones((4,), dtype=mx.float32),
        "transformer.ln_f.bias": mx.zeros((4,), dtype=mx.float32),
    }
    _write_json(root / "config.json", config)
    mx.save_safetensors(str(root / "model.safetensors"), tensors)
    (root / "vocab.json").write_text(
        _canonical_json({"<|endoftext|>": 0, "hello": 1}) + "\n",
        encoding="utf-8",
    )
    (root / "merges.txt").write_text("#version: 0.2\nh e\n", encoding="utf-8")


def _assert_private_regular_file(path: Path) -> None:
    metadata = path.lstat()
    assert stat.S_ISREG(metadata.st_mode)
    assert not path.is_symlink()
    assert metadata.st_nlink == 1
    assert metadata.st_mode & 0o077 == 0


def test_prepare_physical_deployment_is_offline_deterministic_and_exact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def network_forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError(f"network called: {args!r} {kwargs!r}")

    monkeypatch.setattr(
        model_manifest, "resolve_huggingface_manifest", network_forbidden
    )
    monkeypatch.setattr(
        weight_provisioning, "fetch_huggingface_file", network_forbidden
    )

    first = prepare_physical_deployment(tmp_path / "first")
    second = prepare_physical_deployment(tmp_path / "second")

    assert first.route_ready is False
    assert first.network_downloads == 0
    assert first.manifest == second.manifest
    assert first.model_artifact_digests == second.model_artifact_digests
    assert tuple(item["range"] for item in first.assignments) == EXPECTED_RANGES
    assert tuple(item["node_id"] for item in first.assignments) == (
        "node-a",
        "node-b",
    )
    assert len({item["assignment_id"] for item in first.assignments}) == 2
    assert len(first.stage_packs) == len(first.assignments) == 2
    assert len(first.stage_pack_verifications) == 2
    for assignment, report, pack, verification in zip(
        first.assignments,
        first.artifact_reports,
        first.stage_packs,
        first.stage_pack_verifications,
        strict=True,
    ):
        assert pack["assignment_id"] == assignment["assignment_id"]
        assert verification["stage_pack_digest"] == pack["stage_pack_digest"]
        assert report["stage_pack_digest"] == pack["stage_pack_digest"]
        assert (
            report["stage_pack_verification_digest"]
            == verification["stage_pack_verification_digest"]
        )
        assert verification["ready_for_load"] is True
        assert verification["route_ready"] is False
    assert first.reference_assignment["range"] == {
        "start_layer": 0,
        "end_layer_exclusive": 2,
        "layer_count": 2,
    }
    assert first.reference_assignment["node_id"] == "reference-node"
    assert first.reference_assignment["assignment_id"] not in {
        item["assignment_id"] for item in first.assignments
    }
    assert first.local_fetch_requests
    assert all(not Path(item).is_absolute() for item in first.local_fetch_requests)
    json.dumps(first.evidence_document(), sort_keys=True, allow_nan=False)


def test_prepare_physical_deployment_binds_distinct_backends_by_node(
    tmp_path: Path,
) -> None:
    deployment = prepare_physical_deployment(
        tmp_path / "mixed",
        runtime_backends_by_node={"node-a": "mlx", "node-b": "numpy"},
    )

    assert [item["runtime"]["backend"] for item in deployment.assignments] == [
        "mlx",
        "numpy",
    ]


@pytest.mark.parametrize(
    ("runtime_backends_by_node", "reason"),
    (
        ({"node-a": "mlx"}, "runtime_backend_node_set_mismatch"),
        (
            {"node-a": "mlx", "node-b": "numpy", "node-c": "numpy"},
            "runtime_backend_node_set_mismatch",
        ),
        ({"node-a": "mlx", "node-b": "cuda"}, "invalid_runtime_backend"),
        (["mlx", "numpy"], "invalid_runtime_backends_by_node"),
    ),
)
def test_prepare_physical_deployment_rejects_invalid_per_node_backends(
    tmp_path: Path,
    runtime_backends_by_node: Any,
    reason: str,
) -> None:
    with pytest.raises(PhysicalDeploymentError, match=f"^{reason}$"):
        prepare_physical_deployment(
            tmp_path / "invalid",
            runtime_backends_by_node=runtime_backends_by_node,
        )


def test_loader_rejects_tampered_embedded_stage_pack_evidence(tmp_path: Path) -> None:
    deployment = prepare_physical_deployment(tmp_path / "deployment")
    report = copy.deepcopy(deployment.artifact_reports[0])
    report["stage_pack"]["node_id"] = "forged-node"

    with pytest.raises(RuntimeLoadError, match="stage-pack evidence rejected"):
        load_assignment_stage(
            deployment.assignments[0],
            report,
            load_generation=17,
        )


def test_prepared_assignments_and_independent_reference_load_and_execute(
    tmp_path: Path,
) -> None:
    deployment = prepare_physical_deployment(tmp_path / "deployment")

    loaded_stages = [
        load_assignment_stage(assignment, report, load_generation=17)
        for assignment, report in zip(
            deployment.assignments,
            deployment.artifact_reports,
        )
    ]
    loaded_reference = load_assignment_stage(
        deployment.reference_assignment,
        deployment.reference_report,
        load_generation=17,
    )

    assert [stage.proof["loaded_range"] for stage in loaded_stages] == list(
        EXPECTED_RANGES
    )
    for stage, pack, verification in zip(
        loaded_stages,
        deployment.stage_packs,
        deployment.stage_pack_verifications,
        strict=True,
    ):
        assert stage.proof["stage_pack_digest"] == pack["stage_pack_digest"]
        assert (
            stage.proof["stage_pack_verification_digest"]
            == verification["stage_pack_verification_digest"]
        )
    assert loaded_reference.proof["loaded_range"] == {
        "start_layer": 0,
        "end_layer_exclusive": 2,
        "layer_count": 2,
    }
    reference_result = execute_loaded_stage(
        loaded_reference,
        token_ids=mx.array(((1, 2, 3),), dtype=mx.uint32),
    )
    mx.eval(reference_result)
    assert tuple(reference_result.shape) == (1, 3, 7)
    assert int(mx.argmax(reference_result[0, -1, :]).item()) in range(7)

    graph = build_execution_graph(
        deployment.assignments,
        [stage.proof for stage in loaded_stages],
        link_scheme="iroh",
    )
    assert [stage.placements[0].node_id for stage in graph.stages] == [
        "node-a",
        "node-b",
    ]
    assert graph.edges[0].link_id == "iroh:node-a->node-b"
    states = build_physical_device_states(graph)
    assert set(states) == {"node-a", "node-b"}
    assert all(state.availability == "ALIVE" for state in states.values())


def test_prepare_physical_deployment_rejects_reuse_and_bad_nodes(
    tmp_path: Path,
) -> None:
    occupied = tmp_path / "occupied"
    occupied.mkdir()
    (occupied / "existing").write_bytes(b"do not replace")

    with pytest.raises(PhysicalDeploymentError, match="deployment_root_not_empty"):
        prepare_physical_deployment(occupied)
    assert (occupied / "existing").read_bytes() == b"do not replace"

    with pytest.raises(PhysicalDeploymentError, match="invalid_physical_nodes"):
        prepare_physical_deployment(tmp_path / "bad-nodes", node_ids=("same", "same"))
    assert not (tmp_path / "bad-nodes").exists()


def test_replicated_execution_graph_retains_alternative_complete_tracks(
    tmp_path: Path,
) -> None:
    deployment = prepare_physical_deployment(tmp_path / "replicated")
    loaded = [
        load_assignment_stage(assignment, report, load_generation=17)
        for assignment, report in zip(
            deployment.assignments,
            deployment.artifact_reports,
            strict=True,
        )
    ]
    entry = copy.deepcopy(deployment.assignments[0])
    primary = copy.deepcopy(deployment.assignments[1])
    replica = copy.deepcopy(primary)
    replica["node_id"] = "node-c"
    replica["artifact_cache_root"] = "/tmp/node-c"
    replica["assignment_id"] = assignment_id_for(replica)
    assignments = {"p0": entry, "p1": primary, "r1": replica}
    proofs = {
        "p0": loaded[0].proof,
        "p1": loaded[1].proof,
        "r1": loaded[1].proof,
    }
    route = {
        "protocol": "mycelium.route_plan.v2",
        "placements": [
            {
                "placement_id": "p0",
                "replica_group_id": "group-0",
                "node_id": "node-a",
                "layer_range": {"start": 0, "end": 1},
                "primary": True,
            },
            {
                "placement_id": "p1",
                "replica_group_id": "group-1",
                "node_id": "node-b",
                "layer_range": {"start": 1, "end": 2},
                "primary": True,
            },
            {
                "placement_id": "r1",
                "replica_group_id": "group-1",
                "node_id": "node-c",
                "layer_range": {"start": 1, "end": 2},
                "primary": False,
            },
        ],
        "forward_edges": [
            {"src_placement_id": "p0", "dst_placement_id": "p1"},
            {"src_placement_id": "p0", "dst_placement_id": "r1"},
        ],
        "loopbacks": [
            {"src_placement_id": "p1", "dst_placement_id": "p0"},
            {"src_placement_id": "r1", "dst_placement_id": "p0"},
        ],
    }

    graph = build_replicated_execution_graph(assignments, proofs, route)

    assert len(graph.stages) == 2
    assert [item.placement_id for item in graph.stages[1].placements] == ["p1", "r1"]
    assert {(edge.from_placement_id, edge.to_placement_id) for edge in graph.edges} == {
        ("p0", "p1"),
        ("p0", "r1"),
    }
    assert set(build_physical_device_states(graph)) == {"node-a", "node-b", "node-c"}


def test_replicated_execution_graph_supports_complete_model_replica_tracks(
    tmp_path: Path,
) -> None:
    deployment = prepare_physical_deployment(tmp_path / "full-replicas")
    loaded = load_assignment_stage(
        deployment.assignments[0], deployment.artifact_reports[0], load_generation=17
    )
    assignments = {}
    proofs = {}
    for placement_id, node_id in (
        ("full-primary", "node-a"),
        ("full-replica", "node-b"),
    ):
        assignment = copy.deepcopy(deployment.assignments[0])
        assignment["node_id"] = node_id
        assignment["range"] = {
            "start_layer": 0,
            "end_layer_exclusive": 2,
            "layer_count": 2,
        }
        assignment["components"] = [
            "input_embedding",
            "decoder",
            "final_norm",
            "lm_head",
        ]
        assignment["assignment_id"] = assignment_id_for(assignment)
        assignments[placement_id] = assignment
        proofs[placement_id] = loaded.proof
    route = {
        "protocol": "mycelium.route_plan.v2",
        "placements": [
            {
                "placement_id": "full-primary",
                "replica_group_id": "full-model",
                "node_id": "node-a",
                "layer_range": {"start": 0, "end": 2},
                "primary": True,
            },
            {
                "placement_id": "full-replica",
                "replica_group_id": "full-model",
                "node_id": "node-b",
                "layer_range": {"start": 0, "end": 2},
                "primary": False,
            },
        ],
        "forward_edges": [],
        "loopbacks": [],
    }

    graph = build_replicated_execution_graph(assignments, proofs, route)

    assert len(graph.stages) == 1
    assert graph.edges == ()
    assert {
        (edge.from_placement_id, edge.to_placement_id, edge.link_id)
        for edge in graph.loopback_edges
    } == {
        ("full-primary", "full-primary", "local:node-a"),
        ("full-replica", "full-replica", "local:node-b"),
    }


def test_prepare_physical_deployment_accepts_local_monolithic_gpt2_source(
    tmp_path: Path,
) -> None:
    source_root = tmp_path / "dialogpt-source"
    _write_dialogpt_style_monolithic_source(source_root, n_layer=5)
    source_digest = sha256_file(source_root / "model.safetensors")

    planning_manifest = compile_local_model_manifest(
        LocalModelSource(
            root=source_root,
            model_id="microsoft/DialoGPT-small",
            requested_revision="local-main",
            resolved_commit="f" * 40,
        )
    )
    assert planning_manifest["model_id"] == "microsoft/DialoGPT-small"
    assert planning_manifest["num_layers"] == 5
    assert planning_manifest["files"][0]["content_digest"]["value"] == source_digest

    deployment = prepare_physical_deployment(
        tmp_path / "deployment",
        node_ids=("mac-a", "mac-b"),
        model_source=LocalModelSource(
            root=source_root,
            model_id="microsoft/DialoGPT-small",
            requested_revision="local-main",
            resolved_commit="f" * 40,
        ),
        runtime_dtype="float16",
    )

    assert deployment.manifest["model_id"] == "microsoft/DialoGPT-small"
    assert deployment.manifest["requested_revision"] == "local-main"
    assert deployment.manifest["resolved_commit"] == "f" * 40
    assert deployment.manifest["num_layers"] == 5
    assert deployment.manifest["component_aliases"] == {"lm_head": "input_embedding"}
    assert deployment.model_source is not None
    assert deployment.model_source["format"] == "safetensors_monolithic"
    assert [item["path"] for item in deployment.manifest["files"]] == [
        "model.safetensors"
    ]
    assert deployment.manifest["files"][0]["content_digest"]["value"] == source_digest
    assert sorted(item["path"] for item in deployment.tokenizer_assets) == [
        "merges.txt",
        "vocab.json",
    ]
    assert "tokenizer.json" not in {
        item["path"] for item in deployment.tokenizer_assets
    }
    assert all(
        item["path"] not in {"merges.txt", "vocab.json"}
        for item in deployment.manifest["files"]
    )
    for name in (
        "config.json",
        "model.safetensors",
        "model.safetensors.index.json",
        "vocab.json",
        "merges.txt",
    ):
        _assert_private_regular_file(deployment.root / name)

    assert tuple(item["range"] for item in deployment.assignments) == (
        {"start_layer": 0, "end_layer_exclusive": 3, "layer_count": 3},
        {"start_layer": 3, "end_layer_exclusive": 5, "layer_count": 2},
    )
    assert all(
        assignment["runtime"]["dtype"] == "float16"
        for assignment in (*deployment.assignments, deployment.reference_assignment)
    )

    loaded_stages = [
        load_assignment_stage(assignment, report, load_generation=17)
        for assignment, report in zip(
            deployment.assignments,
            deployment.artifact_reports,
        )
    ]
    loaded_reference = load_assignment_stage(
        deployment.reference_assignment,
        deployment.reference_report,
        load_generation=17,
    )
    token_ids = mx.array(((1, 2, 3),), dtype=mx.uint32)
    split_hidden = execute_loaded_stage(loaded_stages[0], token_ids=token_ids)
    split_logits = execute_loaded_stage(loaded_stages[1], hidden_states=split_hidden)
    reference_logits = execute_loaded_stage(loaded_reference, token_ids=token_ids)
    mx.eval(split_logits, reference_logits)
    assert tuple(split_logits.shape) == (1, 3, 7)
    assert str(split_logits.dtype) == "mlx.core.float16"
    assert bool(
        mx.allclose(split_logits, reference_logits, rtol=1e-3, atol=1e-3).item()
    )

    graph = build_execution_graph(
        deployment.assignments,
        [stage.proof for stage in loaded_stages],
    )
    assert graph.activation_bytes == 2
    assert graph.hidden_size == 4
    json.dumps(deployment.evidence_document(), sort_keys=True, allow_nan=False)


def test_execution_graph_accepts_three_ordered_physical_stages(
    tmp_path: Path,
) -> None:
    source_root = tmp_path / "three-stage-source"
    _write_dialogpt_style_monolithic_source(source_root, n_layer=5)
    deployment = prepare_physical_deployment(
        tmp_path / "three-stage-deployment",
        node_ids=("node-0", "node-1", "node-2"),
        model_source=LocalModelSource(
            root=source_root,
            model_id="local/three-stage-model",
            requested_revision="snapshot",
            resolved_commit="3" * 40,
        ),
    )
    loaded = [
        load_assignment_stage(assignment, report, load_generation=17)
        for assignment, report in zip(
            deployment.assignments,
            deployment.artifact_reports,
            strict=True,
        )
    ]

    graph = build_execution_graph(
        deployment.assignments,
        [stage.proof for stage in loaded],
        link_scheme="iroh",
    )

    assert [stage.placements[0].node_id for stage in graph.stages] == [
        "node-0",
        "node-1",
        "node-2",
    ]
    assert [edge.link_id for edge in graph.edges] == [
        "iroh:node-0->node-1",
        "iroh:node-1->node-2",
    ]
    assert graph.loopback_edges[0].link_id == "iroh:node-2->node-0"
    assert set(build_physical_device_states(graph)) == {
        "node-0",
        "node-1",
        "node-2",
    }


def test_prepare_physical_deployment_accepts_local_sharded_gpt2_source(
    tmp_path: Path,
) -> None:
    source_root = tmp_path / "sharded-source"
    source_root.mkdir()
    _build_local_model(source_root, n_positions=16)

    deployment = prepare_physical_deployment(
        tmp_path / "deployment",
        model_source=LocalModelSource(
            root=source_root,
            model_id="local/copied-gpt2-qualification",
            requested_revision="snapshot",
            resolved_commit="1" * 40,
        ),
    )

    assert deployment.manifest["model_id"] == "local/copied-gpt2-qualification"
    assert deployment.manifest["resolved_commit"] == "1" * 40
    assert deployment.model_source is not None
    assert deployment.model_source["format"] == "safetensors_sharded"
    assert tuple(item["range"] for item in deployment.assignments) == EXPECTED_RANGES
    assert [item["path"] for item in deployment.manifest["files"]] == list(SHARD_NAMES)
    for shard_name in SHARD_NAMES:
        copied = deployment.root / shard_name
        _assert_private_regular_file(copied)
        assert sha256_file(copied) == sha256_file(source_root / shard_name)


def test_local_model_materialization_is_independent_of_source_mutation(
    tmp_path: Path,
) -> None:
    source_root = tmp_path / "source"
    _write_dialogpt_style_monolithic_source(source_root, n_layer=2)
    deployment = prepare_physical_deployment(
        tmp_path / "deployment",
        model_source=LocalModelSource(
            root=source_root,
            model_id="local/copy-on-write-model",
            requested_revision="snapshot",
            resolved_commit="2" * 40,
        ),
    )
    materialized = deployment.root / "model.safetensors"
    expected = sha256_file(materialized)

    with (source_root / "model.safetensors").open("r+b") as source:
        source.seek(-1, 2)
        final_byte = source.read(1)
        source.seek(-1, 2)
        source.write(bytes((final_byte[0] ^ 0xFF,)))

    assert sha256_file(source_root / "model.safetensors") != expected
    assert sha256_file(materialized) == expected
    assert materialized.stat().st_ino != (source_root / "model.safetensors").stat().st_ino


def test_local_model_source_validation_rejects_unpinned_identity(
    tmp_path: Path,
) -> None:
    source_root = tmp_path / "source"
    _write_dialogpt_style_monolithic_source(source_root, n_layer=2)

    with pytest.raises(PhysicalDeploymentError, match="invalid_local_model_source"):
        prepare_physical_deployment(
            tmp_path / "deployment",
            model_source=LocalModelSource(
                root=source_root,
                model_id="local/dialogpt",
                resolved_commit="not-a-commit",
            ),
        )


def test_prepare_physical_deployment_splits_across_five_nodes(tmp_path: Path) -> None:
    source_root = tmp_path / "dialogpt-source"
    _write_dialogpt_style_monolithic_source(source_root, n_layer=12)

    deployment = prepare_physical_deployment(
        tmp_path / "deployment",
        node_ids=("mac-a", "mac-b", "linux-a", "linux-b", "pixel"),
        model_source=LocalModelSource(
            root=source_root,
            model_id="microsoft/DialoGPT-small",
            requested_revision="local-main",
            resolved_commit="f" * 40,
        ),
        runtime_dtype="float16",
    )

    ranges = [entry["range"] for entry in deployment.route_plan["route"]]
    assert [item["layer_count"] for item in ranges] == [3, 3, 2, 2, 2]
    assert [item["start_layer"] for item in ranges] == [0, 3, 6, 8, 10]
    assert [item["end_layer_exclusive"] for item in ranges] == [3, 6, 8, 10, 12]
    assert sum(item["layer_count"] for item in ranges) == 12
    assert len(deployment.assignments) == 5


def test_prepare_physical_deployment_rejects_more_nodes_than_layers(
    tmp_path: Path,
) -> None:
    source_root = tmp_path / "dialogpt-source"
    _write_dialogpt_style_monolithic_source(source_root, n_layer=2)

    with pytest.raises(PhysicalDeploymentError, match="invalid_physical_layer_split"):
        prepare_physical_deployment(
            tmp_path / "deployment",
            node_ids=("a", "b", "c"),
            model_source=LocalModelSource(
                root=source_root,
                model_id="microsoft/DialoGPT-small",
                requested_revision="local-main",
                resolved_commit="f" * 40,
            ),
            runtime_dtype="float16",
        )
