from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from mycelium_live.stage_pack_builder import (
    StagePackBuildError,
    build_stage_pack_source,
)


def _digest(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _fixture(tmp_path: Path) -> tuple[Path, dict, dict, dict, dict]:
    bundle = tmp_path / "bundle"
    deployment = bundle / "deployment"
    deployment.mkdir(parents=True)
    layer = b"layer" * 20_000
    shared = b"shared" * 2_000
    (deployment / "layer.safetensors").write_bytes(layer)
    (deployment / "shared.safetensors").write_bytes(shared)
    pack = {
        "assignment_id": "assignment-1",
        "node_id": "member-1",
        "model_id": "Qwen/Qwen3-8B",
        "resolved_commit": "a" * 40,
        "manifest_digest": "sha256:" + "b" * 64,
        "stage_pack_digest": "sha256:" + "c" * 64,
        "range": {
            "start_layer": 0,
            "end_layer_exclusive": 2,
            "layer_count": 2,
        },
        "component_tensor_keys": {
            "input_embedding": ["model.embed_tokens.weight"],
            "decoder": ["model.layers.0.weight", "model.layers.1.weight"],
        },
        "expected_tensor_keys": [
            "model.embed_tokens.weight",
            "model.layers.0.weight",
            "model.layers.1.weight",
        ],
        "artifacts": [
            {
                "relative_path": "shared.safetensors",
                "upstream_path": "shared.safetensors",
                "size_bytes": len(shared),
                "content_digest": _digest(shared),
                "tensor_keys": ["model.embed_tokens.weight"],
            },
            {
                "relative_path": "layer.safetensors",
                "upstream_path": "layer.safetensors",
                "size_bytes": len(layer),
                "content_digest": _digest(layer),
                "tensor_keys": ["model.layers.0.weight", "model.layers.1.weight"],
            },
        ],
    }
    offer = {
        "assignment_id": "assignment-1",
        "assignment_digest": "sha256:" + "d" * 64,
        "graph_digest": "sha256:" + "e" * 64,
        "recipient_node_id": "member-1",
        "generation": 4,
        "stage_pack_digest": pack["stage_pack_digest"],
    }
    graph = {
        "stages": [
            {
                "stage_id": "stage-1",
                "placements": [
                    {
                        "assignment_id": "assignment-1",
                        "placement_id": "placement-1",
                    }
                ],
            }
        ]
    }
    authorization = {
        "source_quantization": "bfloat16",
        "serving_dtype": "float32",
        "serving_quantization": "bfloat16",
        "representation_digest": "sha256:" + "1" * 64,
        "owner_decision_digest": "sha256:" + "2" * 64,
        "feasibility_digest": "sha256:" + "3" * 64,
        "evidence_generation": 7,
        "evidence_valid_until_unix_ms": 2_000,
    }
    return bundle, pack, offer, graph, authorization


def test_builds_exact_sorted_pack_chunks_and_component_bindings(
    tmp_path: Path,
) -> None:
    bundle, pack, offer, graph, authorization = _fixture(tmp_path)
    manifest, binding = build_stage_pack_source(
        transfer_bundle=bundle,
        pack=pack,
        assignment_offer=offer,
        graph=graph,
        authorization=authorization,
        output_root=tmp_path / "source",
        chunk_size_bytes=65_536,
        issued_at_unix_ms=1_000,
        expires_at_unix_ms=901_000,
    )
    assert manifest["component_scope"] == ["embedding", "transformer_layers"]
    assert [item["relative_path"] for item in manifest["files"]] == [
        "deployment/layer.safetensors",
        "deployment/shared.safetensors",
    ]
    expected = (bundle / "deployment" / "layer.safetensors").read_bytes() + (
        bundle / "deployment" / "shared.safetensors"
    ).read_bytes()
    rebuilt = b"".join(
        (
            tmp_path
            / "source"
            / "objects"
            / item["content_digest"].removeprefix("sha256:")
        ).read_bytes()
        for item in manifest["chunks"]
    )
    assert rebuilt == expected
    assert sum(item["size_bytes"] for item in manifest["chunks"]) == len(expected)
    assert manifest["issued_at_unix_ms"] == 1_000
    assert manifest["expires_at_unix_ms"] == 901_000
    assert binding["assignment_id"] == "assignment-1"
    assert binding["placement_id"] == "placement-1"


def test_rejects_path_escape_assignment_drift_and_unowned_tensor(
    tmp_path: Path,
) -> None:
    bundle, pack, offer, graph, authorization = _fixture(tmp_path)
    pack["artifacts"][0]["relative_path"] = "../shared.safetensors"
    with pytest.raises(StagePackBuildError, match="stage_pack_artifact_path_invalid"):
        build_stage_pack_source(
            transfer_bundle=bundle,
            pack=pack,
            assignment_offer=offer,
            graph=graph,
            authorization=authorization,
            output_root=tmp_path / "source-a",
            chunk_size_bytes=65_536,
            issued_at_unix_ms=1_000,
        )

    bundle, pack, offer, graph, authorization = _fixture(tmp_path / "second")
    offer["recipient_node_id"] = "member-2"
    with pytest.raises(
        StagePackBuildError, match="stage_pack_assignment_binding_invalid"
    ):
        build_stage_pack_source(
            transfer_bundle=bundle,
            pack=pack,
            assignment_offer=offer,
            graph=graph,
            authorization=authorization,
            output_root=tmp_path / "source-b",
            chunk_size_bytes=65_536,
            issued_at_unix_ms=1_000,
        )

    bundle, pack, offer, graph, authorization = _fixture(tmp_path / "third")
    pack["artifacts"][0]["tensor_keys"].append("unowned.weight")
    with pytest.raises(StagePackBuildError, match="stage_pack_tensor_scope_invalid"):
        build_stage_pack_source(
            transfer_bundle=bundle,
            pack=pack,
            assignment_offer=offer,
            graph=graph,
            authorization=authorization,
            output_root=tmp_path / "source-c",
            chunk_size_bytes=65_536,
            issued_at_unix_ms=1_000,
        )
