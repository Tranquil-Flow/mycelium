from __future__ import annotations

import copy
import hashlib
import json
import os
import shutil
import struct
from pathlib import Path
from typing import Any

import pytest

import layer_assignment as la
import model_manifest as mm
import stage_pack as sp
import weight_provisioning as wp
from stage_pack import (
    STAGE_PACK_PROTOCOL,
    STAGE_PACK_VERIFICATION_PROTOCOL,
    artifact_report_for_loader,
    compile_stage_pack,
    stage_pack_digest_for,
    verify_stage_pack,
)

DEPLOYMENT_ID = "12345678-1234-5678-9234-abcdefabcdef"


def _write_safetensors(path: Path, tensor_names: list[str]) -> None:
    header: dict[str, Any] = {}
    payload = bytearray()
    offset = 0
    for index, name in enumerate(tensor_names):
        data = struct.pack("<f", float(index + 1))
        header[name] = {
            "dtype": "F32",
            "shape": [1],
            "data_offsets": [offset, offset + len(data)],
        }
        payload.extend(data)
        offset += len(data)
    encoded = json.dumps(header, sort_keys=True, separators=(",", ":")).encode()
    encoded += b" " * ((8 - len(encoded) % 8) % 8)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(struct.pack("<Q", len(encoded)) + encoded + payload)


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _manifest_and_files(source: Path) -> tuple[dict[str, Any], dict[str, list[str]]]:
    tensors = {
        "a.safetensors": [
            "bert.embeddings.word_embeddings.weight",
            "bert.encoder.layer.0.attention.self.query.weight",
            "bert.encoder.layer.1.attention.self.query.weight",
        ],
        "b.safetensors": [
            "bert.encoder.layer.1.attention.self.key.weight",
            "bert.encoder.layer.2.attention.self.query.weight",
            "bert.pooler.dense.weight",
        ],
        "c.safetensors": ["classifier.weight"],
    }
    for name, names in tensors.items():
        _write_safetensors(source / name, names)
    manifest = mm.compile_model_manifest(
        model_id="org/bert-classifier",
        requested_revision="main",
        resolved_commit="a" * 40,
        config={
            "model_type": "bert",
            "num_hidden_layers": 3,
            "architectures": ["BertForSequenceClassification"],
        },
        checkpoint_index={
            "weight_map": {
                tensor: filename
                for filename, names in tensors.items()
                for tensor in names
            }
        },
        file_metadata={
            name: {
                "size_bytes": (source / name).stat().st_size,
                "sha256": _sha(source / name),
            }
            for name in tensors
        },
    )
    return manifest, tensors


def _route(manifest: dict[str, Any]) -> dict[str, Any]:
    return {
        "ok": True,
        "protocol": "mycelium.manual_provisioning_route.v1",
        "claim_boundary": "manual provisioning only",
        "model": {
            "model_id": manifest["model_id"],
            "num_layers": manifest["num_layers"],
            "manifest_digest": mm.manifest_digest_ref(manifest),
            "resolved_commit": manifest["resolved_commit"],
        },
        "route": [
            {
                "node_id": f"node-{index}",
                "range": {
                    "start_layer": index,
                    "end_layer_exclusive": index + 1,
                    "layer_count": 1,
                },
            }
            for index in range(3)
        ],
        "node_order": ["node-0", "node-1", "node-2"],
    }


def _case(tmp_path: Path) -> tuple[
    dict[str, Any], list[dict[str, Any]], list[dict[str, Any]], Path
]:
    source = tmp_path / "source"
    source.mkdir(parents=True)
    manifest, _ = _manifest_and_files(source)
    cache_roots = {
        f"node-{index}": str((tmp_path / f"cache-{index}").resolve())
        for index in range(3)
    }
    assignments = la.compile_layer_assignments(
        route_plan=_route(manifest),
        manifest=manifest,
        deployment_id=DEPLOYMENT_ID,
        deployment_epoch=7,
        cache_roots=cache_roots,
        runtime_by_node={
            f"node-{index}": {
                "backend": "artifact_verifier",
                "dtype": "source",
                "quantization": "none",
            }
            for index in range(3)
        },
    )

    reports = []
    for assignment in assignments:
        def fetch(
            model_id: str,
            revision: str,
            filename: str,
            cache_root: str | Path,
            local_files_only: bool = False,
        ) -> tuple[Path, bool]:
            assert model_id == manifest["model_id"]
            assert revision == manifest["resolved_commit"]
            target = Path(cache_root) / "snapshot" / filename
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source / filename, target)
            return target, False

        reports.append(wp.provision_assignment(assignment, fetch_file=fetch))
    return manifest, assignments, reports, source


def _refresh_digest(pack: dict[str, Any]) -> None:
    pack["stage_pack_digest"] = stage_pack_digest_for(pack)


def test_compiles_deterministic_assignment_local_packs_and_verifies_warm_artifacts(
    tmp_path: Path,
) -> None:
    manifest, assignments, reports, _ = _case(tmp_path)
    packs = [
        compile_stage_pack(assignment, manifest, report)
        for assignment, report in zip(assignments, reports, strict=True)
    ]
    repeated = compile_stage_pack(assignments[1], manifest, reports[1])

    assert packs[1] == repeated
    assert packs[1]["protocol"] == STAGE_PACK_PROTOCOL
    assert packs[1]["stage_pack_digest"] == stage_pack_digest_for(packs[1])
    assert packs[0]["components"] == ["input_embedding", "decoder"]
    assert packs[1]["components"] == ["decoder"]
    assert packs[2]["components"] == ["decoder", "pooler", "classifier"]
    assert [item["upstream_path"] for item in packs[1]["artifacts"]] == [
        "a.safetensors",
        "b.safetensors",
    ]
    assert packs[1]["range"] == {
        "start_layer": 1,
        "end_layer_exclusive": 2,
        "layer_count": 1,
    }
    assert packs[1]["runtime"] == assignments[1]["runtime"]
    assert packs[1]["route_ready"] is False

    verification = verify_stage_pack(packs[1], assignment=assignments[1])
    assert verification["protocol"] == STAGE_PACK_VERIFICATION_PROTOCOL
    assert verification["stage_pack_digest"] == packs[1]["stage_pack_digest"]
    assert verification["assignment_id"] == assignments[1]["assignment_id"]
    assert verification["verified_tensor_keys"] == assignments[1]["expected_tensor_keys"]
    assert verification["verified_tensor_count"] == len(
        assignments[1]["expected_tensor_keys"]
    )
    assert verification["overfetched_tensor_count"] > 0
    assert verification["ready_for_load"] is True
    assert verification["route_ready"] is False
    assert verification["stage_pack_verification_digest"].startswith("sha256:")

    second = verify_stage_pack(packs[1], assignment=assignments[1])
    assert second == verification


def test_layer_spanning_files_and_file_spanning_layers_are_preserved(tmp_path: Path) -> None:
    manifest, assignments, reports, _ = _case(tmp_path)
    first = compile_stage_pack(assignments[0], manifest, reports[0])
    middle = compile_stage_pack(assignments[1], manifest, reports[1])

    assert [item["upstream_path"] for item in first["artifacts"]] == [
        "a.safetensors"
    ]
    assert [item["upstream_path"] for item in middle["artifacts"]] == [
        "a.safetensors",
        "b.safetensors",
    ]
    verified = verify_stage_pack(middle, assignment=assignments[1])
    mapping = verified["tensor_file_map"]
    assert mapping["bert.encoder.layer.1.attention.self.query.weight"] == "a.safetensors"
    assert mapping["bert.encoder.layer.1.attention.self.key.weight"] == "b.safetensors"


def test_tied_alias_pack_includes_embedding_source_for_final_stage(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    tensors = {
        "embed.safetensors": ["transformer.wte.weight", "transformer.wpe.weight"],
        "blocks.safetensors": [
            "transformer.h.0.attn.weight",
            "transformer.h.1.attn.weight",
            "transformer.ln_f.weight",
        ],
    }
    for name, names in tensors.items():
        _write_safetensors(source / name, names)
    manifest = mm.compile_model_manifest(
        model_id="org/tied-gpt2",
        requested_revision="main",
        resolved_commit="b" * 40,
        config={
            "model_type": "gpt2",
            "architectures": ["GPT2LMHeadModel"],
            "n_layer": 2,
            "n_embd": 4,
            "n_head": 2,
            "n_inner": 8,
            "vocab_size": 7,
            "n_positions": 8,
            "layer_norm_epsilon": 1e-5,
            "activation_function": "gelu_new",
            "scale_attn_weights": True,
            "scale_attn_by_inverse_layer_idx": False,
            "reorder_and_upcast_attn": False,
            "add_cross_attention": False,
            "tie_word_embeddings": True,
        },
        checkpoint_index={
            "weight_map": {
                tensor: filename
                for filename, names in tensors.items()
                for tensor in names
            }
        },
        file_metadata={
            name: {
                "size_bytes": (source / name).stat().st_size,
                "sha256": _sha(source / name),
            }
            for name in tensors
        },
    )
    route = {
        "ok": True,
        "protocol": "mycelium.manual_provisioning_route.v1",
        "claim_boundary": "manual provisioning only",
        "model": {
            "model_id": manifest["model_id"],
            "num_layers": 2,
            "manifest_digest": mm.manifest_digest_ref(manifest),
            "resolved_commit": manifest["resolved_commit"],
        },
        "route": [
            {
                "node_id": "node-a",
                "range": {"start_layer": 0, "end_layer_exclusive": 1, "layer_count": 1},
            },
            {
                "node_id": "node-b",
                "range": {"start_layer": 1, "end_layer_exclusive": 2, "layer_count": 1},
            },
        ],
        "node_order": ["node-a", "node-b"],
    }
    caches = {node: str((tmp_path / node).resolve()) for node in ("node-a", "node-b")}
    assignments = la.compile_layer_assignments(
        route_plan=route,
        manifest=manifest,
        deployment_id=DEPLOYMENT_ID,
        deployment_epoch=1,
        cache_roots=caches,
        runtime_by_node={
            node: {
                "backend": "artifact_verifier",
                "dtype": "source",
                "quantization": "none",
            }
            for node in caches
        },
    )
    final = assignments[1]

    def fetch(
        model_id: str,
        revision: str,
        filename: str,
        cache_root: str | Path,
        local_files_only: bool = False,
    ) -> tuple[Path, bool]:
        target = Path(cache_root) / "snapshot" / filename
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source / filename, target)
        return target, False

    report = wp.provision_assignment(final, fetch_file=fetch)
    pack = compile_stage_pack(final, manifest, report)
    assert pack["component_aliases"] == {"lm_head": "input_embedding"}
    assert pack["component_tensor_keys"]["lm_head"] == ["transformer.wte.weight"]
    assert "embed.safetensors" in [item["upstream_path"] for item in pack["artifacts"]]
    assert verify_stage_pack(pack, assignment=final)["ready_for_load"] is True


@pytest.mark.parametrize(
    ("mutation", "error"),
    [
        (lambda pack: pack.__setitem__("deployment_epoch", 8), "assignment"),
        (lambda pack: pack.__setitem__("manifest_digest", "sha256:" + "f" * 64), "assignment"),
        (lambda pack: pack["range"].__setitem__("layer_count", 2), "assignment|range"),
        (lambda pack: pack.__setitem__("components", ["decoder", "unknown"]), "assignment"),
        (lambda pack: pack["runtime"].__setitem__("backend", "cuda"), "assignment|runtime"),
    ],
)
def test_verifier_rejects_identity_range_role_and_runtime_drift(
    tmp_path: Path, mutation: Any, error: str
) -> None:
    manifest, assignments, reports, _ = _case(tmp_path)
    pack = compile_stage_pack(assignments[1], manifest, reports[1])
    mutation(pack)
    _refresh_digest(pack)
    with pytest.raises(ValueError, match=error):
        verify_stage_pack(pack, assignment=assignments[1])


def test_compile_rejects_manifest_digest_and_minimal_file_drift(tmp_path: Path) -> None:
    manifest, assignments, reports, _ = _case(tmp_path)
    bad_manifest = copy.deepcopy(manifest)
    bad_manifest["manifest_digest"]["value"] = "f" * 64
    with pytest.raises(ValueError, match="manifest digest"):
        compile_stage_pack(assignments[1], bad_manifest, reports[1])

    bad_assignment = copy.deepcopy(assignments[1])
    bad_assignment["files"] = bad_assignment["files"][:1]
    bad_assignment["assignment_id"] = la.assignment_id_for(bad_assignment)
    bad_report = copy.deepcopy(reports[1])
    bad_report["assignment_id"] = bad_assignment["assignment_id"]
    bad_report["verified_files"] = bad_report["verified_files"][:1]
    bad_report["expected_bytes"] = bad_assignment["files"][0]["size_bytes"]
    bad_report["network_download_bytes"] = bad_report["expected_bytes"]
    with pytest.raises(ValueError, match="minimal covering files"):
        compile_stage_pack(bad_assignment, manifest, bad_report)


def test_verifier_rejects_corruption_traversal_and_symlink(tmp_path: Path) -> None:
    manifest, assignments, reports, _ = _case(tmp_path)
    pack = compile_stage_pack(assignments[1], manifest, reports[1])
    artifact = Path(pack["artifact_root"]) / pack["artifacts"][0]["relative_path"]
    artifact.write_bytes(artifact.read_bytes() + b"corrupt")
    with pytest.raises(ValueError, match="size mismatch"):
        verify_stage_pack(pack, assignment=assignments[1])

    manifest, assignments, reports, _ = _case(tmp_path / "traversal")
    pack = compile_stage_pack(assignments[1], manifest, reports[1])
    pack["artifacts"][0]["relative_path"] = "../escape.safetensors"
    _refresh_digest(pack)
    with pytest.raises(ValueError, match="unsafe artifact path"):
        verify_stage_pack(pack, assignment=assignments[1])

    manifest, assignments, reports, _ = _case(tmp_path / "symlink")
    pack = compile_stage_pack(assignments[1], manifest, reports[1])
    artifact = Path(pack["artifact_root"]) / pack["artifacts"][0]["relative_path"]
    target = artifact.with_name("real.safetensors")
    artifact.rename(target)
    artifact.symlink_to(target.name)
    with pytest.raises(ValueError, match="symlink|open verified"):
        verify_stage_pack(pack, assignment=assignments[1])


def test_verifier_rejects_missing_assigned_tensor_even_with_matching_file_digest(
    tmp_path: Path,
) -> None:
    manifest, assignments, reports, _ = _case(tmp_path)
    pack = compile_stage_pack(assignments[1], manifest, reports[1])
    assignment = copy.deepcopy(assignments[1])
    artifact_record = pack["artifacts"][1]
    artifact = Path(pack["artifact_root"]) / artifact_record["relative_path"]
    _write_safetensors(artifact, ["bert.encoder.layer.2.attention.self.query.weight"])
    artifact_record["size_bytes"] = artifact.stat().st_size
    artifact_record["content_digest"] = "sha256:" + _sha(artifact)
    for upstream in assignment["files"]:
        if upstream["path"] == artifact_record["upstream_path"]:
            upstream["size_bytes"] = artifact_record["size_bytes"]
            upstream["content_digest"] = artifact_record["content_digest"]
    assignment["assignment_id"] = la.assignment_id_for(assignment)
    pack["assignment_id"] = assignment["assignment_id"]
    pack["upstream_files"] = copy.deepcopy(assignment["files"])
    _refresh_digest(pack)
    with pytest.raises(ValueError, match="missing assigned tensors"):
        verify_stage_pack(pack, assignment=assignment)


def test_verifier_rejects_concurrent_artifact_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest, assignments, reports, _ = _case(tmp_path)
    pack = compile_stage_pack(assignments[1], manifest, reports[1])
    artifact = Path(pack["artifact_root"]) / pack["artifacts"][0]["relative_path"]
    original_hash = sp._hash_handle

    def hash_then_mutate(handle: Any) -> str:
        result = original_hash(handle)
        with artifact.open("r+b") as writer:
            writer.seek(-1, os.SEEK_END)
            current = writer.read(1)
            writer.seek(-1, os.SEEK_END)
            writer.write(bytes([current[0] ^ 1]))
            writer.flush()
            os.fsync(writer.fileno())
        return result

    monkeypatch.setattr(sp, "_hash_handle", hash_then_mutate)
    with pytest.raises(ValueError, match="changed during verification"):
        verify_stage_pack(pack, assignment=assignments[1])


def test_loader_report_is_bound_to_pack_and_assignment(tmp_path: Path) -> None:
    manifest, assignments, reports, _ = _case(tmp_path)
    pack = compile_stage_pack(assignments[1], manifest, reports[1])
    verification = verify_stage_pack(pack, assignment=assignments[1])
    report = artifact_report_for_loader(pack, verification, assignment=assignments[1])

    assert report["protocol"] == "mycelium.artifact_verification_report.v1"
    assert report["stage_pack_digest"] == pack["stage_pack_digest"]
    assert report["assignment_id"] == assignments[1]["assignment_id"]
    assert report["ready_for_load"] is True
    assert report["route_ready"] is False
    assert not wp.artifact_report_errors(assignments[1], report)
    assert all(Path(record["local_path"]).is_absolute() for record in report["verified_files"])

    malformed = copy.deepcopy(report)
    malformed["stage_pack_digest"] = "sha256:invalid"
    assert any(
        "stage_pack_digest" in error
        for error in wp.artifact_report_errors(assignments[1], malformed)
    )
    incomplete = copy.deepcopy(report)
    incomplete.pop("stage_pack_verification_digest")
    assert any(
        "digest evidence must be complete" in error
        for error in wp.artifact_report_errors(assignments[1], incomplete)
    )

    forged = copy.deepcopy(verification)
    forged["assignment_id"] = assignments[0]["assignment_id"]
    with pytest.raises(ValueError, match="verification digest|assignment"):
        artifact_report_for_loader(pack, forged, assignment=assignments[1])


def test_stage_pack_digest_rejects_unknown_fields_nonfinite_and_duplicate_records(
    tmp_path: Path,
) -> None:
    manifest, assignments, reports, _ = _case(tmp_path)
    pack = compile_stage_pack(assignments[1], manifest, reports[1])
    pack["unexpected"] = True
    _refresh_digest(pack)
    with pytest.raises(ValueError, match="fields"):
        verify_stage_pack(pack, assignment=assignments[1])

    pack = compile_stage_pack(assignments[1], manifest, reports[1])
    pack["runtime"]["temperature"] = float("nan")
    with pytest.raises(ValueError, match="finite|JSON"):
        stage_pack_digest_for(pack)

    pack = compile_stage_pack(assignments[1], manifest, reports[1])
    pack["artifacts"].append(copy.deepcopy(pack["artifacts"][0]))
    _refresh_digest(pack)
    with pytest.raises(ValueError, match="duplicate artifact"):
        verify_stage_pack(pack, assignment=assignments[1])
