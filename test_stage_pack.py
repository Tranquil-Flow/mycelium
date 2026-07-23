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
    FP16_TOLERANCE_PROTOCOL,
    STAGE_PACK_PROTOCOL,
    STAGE_PACK_VERIFICATION_PROTOCOL,
    artifact_report_for_loader,
    compile_stage_pack,
    load_fp16_tolerances,
    stage_pack_digest_for,
    verify_stage_pack,
)

DEPLOYMENT_ID = "12345678-1234-5678-9234-abcdefabcdef"
_UNCHANGED = object()


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


def _case(tmp_path: Path, *, deployment_epoch: int = 7) -> tuple[
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
        deployment_epoch=deployment_epoch,
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


def _mlx_runtime(dtype: str = "float32") -> dict[str, Any]:
    return {
        "backend": "mlx",
        "dtype": dtype,
        "quantization": "none",
        "architecture": "gpt2",
        "model_config": {
            "n_layer": 3,
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
        },
    }


def _control_plane_binding(
    snapshot_generation: int = 1,
    *,
    deployment_epoch: int = 7,
) -> dict[str, Any]:
    return {
        "protocol": "mycelium.control_plane_binding.v1",
        "evidence_bundle_digest": "sha256:" + "a" * 64,
        "planner_snapshot_digest": "sha256:" + "b" * 64,
        "snapshot_generation": snapshot_generation,
        "swarm_id": "swarm-stage-pack-test",
        "deployment_id": DEPLOYMENT_ID,
        "deployment_epoch": deployment_epoch,
    }


def _rebind_assignment_pack(
    assignment: dict[str, Any],
    pack: dict[str, Any],
    *,
    runtime: dict[str, Any] | object = _UNCHANGED,
    control_plane_binding: dict[str, Any] | None | object = _UNCHANGED,
) -> None:
    if runtime is not _UNCHANGED:
        assignment["runtime"] = copy.deepcopy(runtime)
        pack["runtime"] = copy.deepcopy(runtime)
    if control_plane_binding is not _UNCHANGED:
        if control_plane_binding is None:
            assignment.pop("control_plane_binding", None)
        else:
            assignment["control_plane_binding"] = copy.deepcopy(
                control_plane_binding
            )
        pack["control_plane_binding"] = copy.deepcopy(control_plane_binding)
    assignment["assignment_id"] = la.assignment_id_for(assignment)
    pack["assignment_id"] = assignment["assignment_id"]
    _refresh_digest(pack)


def _set_present_assignment_pack_binding(
    assignment: dict[str, Any],
    pack: dict[str, Any],
    binding: Any,
) -> None:
    assignment["control_plane_binding"] = copy.deepcopy(binding)
    assignment["assignment_id"] = la.assignment_id_for(assignment)
    pack["assignment_id"] = assignment["assignment_id"]
    pack["control_plane_binding"] = copy.deepcopy(binding)
    _refresh_digest(pack)


def _assert_collection_error_is_value_free(
    error: ValueError,
    *,
    assignments: list[dict[str, Any]],
    manifest: dict[str, Any],
    extra_forbidden: tuple[str, ...] = (),
) -> None:
    message = str(error)
    tensor_keys = {
        key
        for ownership_map in (
            manifest["tensor_keys_by_layer"],
            manifest["component_tensor_keys"],
        )
        for keys in ownership_map.values()
        for key in keys
    }
    forbidden = {
        DEPLOYMENT_ID,
        *(assignment["assignment_id"] for assignment in assignments),
        *(assignment["node_id"] for assignment in assignments),
        *(record["path"] for record in manifest["files"]),
        *tensor_keys,
        json.dumps(assignments, sort_keys=True, separators=(",", ":")),
        *(
            json.dumps(binding, sort_keys=True, separators=(",", ":"))
            for assignment in assignments
            if isinstance(
                binding := assignment.get("control_plane_binding"),
                dict,
            )
        ),
        *extra_forbidden,
    }
    assert all(value not in message for value in forbidden)


def _set_legacy_deployment_epoch(
    assignments: list[dict[str, Any]],
    packs: list[dict[str, Any]],
    deployment_epoch: Any,
) -> None:
    for assignment, pack in zip(assignments, packs, strict=True):
        assignment.pop("control_plane_binding", None)
        assignment["deployment_epoch"] = deployment_epoch
        assignment["assignment_id"] = la.assignment_id_for(assignment)
        pack["deployment_epoch"] = deployment_epoch
        pack["assignment_id"] = assignment["assignment_id"]
        pack["control_plane_binding"] = None
        _refresh_digest(pack)


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

    verification = verify_stage_pack(packs[1], assignment=assignments[1], manifest=manifest)
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

    second = verify_stage_pack(packs[1], assignment=assignments[1], manifest=manifest)
    assert second == verification


def test_zero_deployment_epoch_producer_output_compiles_and_verifies_direct_pack(
    tmp_path: Path,
) -> None:
    manifest, assignments, reports, _ = _case(tmp_path, deployment_epoch=0)

    pack = compile_stage_pack(assignments[1], manifest, reports[1])
    verification = verify_stage_pack(
        pack,
        assignment=assignments[1],
        manifest=manifest,
    )

    assert pack["deployment_epoch"] == 0
    assert verification["deployment_epoch"] == 0
    assert verification["ready_for_load"] is True
    assert verification["route_ready"] is False


def test_zero_deployment_epoch_legacy_collection_is_accepted(
    tmp_path: Path,
) -> None:
    manifest, assignments, reports, _ = _case(tmp_path, deployment_epoch=0)
    packs = [
        compile_stage_pack(assignment, manifest, report)
        for assignment, report in zip(assignments, reports, strict=True)
    ]

    summary = sp.verify_stage_pack_collection(
        packs,
        assignments=assignments,
        manifest=manifest,
    )

    assert all("control_plane_binding" not in assignment for assignment in assignments)
    assert all(pack["control_plane_binding"] is None for pack in packs)
    assert summary["exact_logical_coverage"] is True
    assert summary["route_ready"] is False


def test_zero_deployment_epoch_bound_collection_is_accepted(
    tmp_path: Path,
) -> None:
    manifest, assignments, reports, _ = _case(tmp_path, deployment_epoch=0)
    packs = [
        compile_stage_pack(assignment, manifest, report)
        for assignment, report in zip(assignments, reports, strict=True)
    ]
    binding = _control_plane_binding(deployment_epoch=0)
    for assignment, pack in zip(assignments, packs, strict=True):
        _rebind_assignment_pack(
            assignment,
            pack,
            control_plane_binding=binding,
        )

    summary = sp.verify_stage_pack_collection(
        packs,
        assignments=assignments,
        manifest=manifest,
    )

    assert all(
        assignment["control_plane_binding"]["deployment_epoch"] == 0
        for assignment in assignments
    )
    assert summary["exact_logical_coverage"] is True
    assert summary["route_ready"] is False


@pytest.mark.parametrize(
    "binding_kind",
    ("present_none", "wrong_protocol"),
)
@pytest.mark.parametrize(
    "surface",
    ("compile", "verify", "evidence"),
)
def test_per_pack_surfaces_reject_present_invalid_control_plane_binding(
    tmp_path: Path,
    binding_kind: str,
    surface: str,
) -> None:
    manifest, assignments, reports, _ = _case(tmp_path)
    assignment = assignments[1]
    pack = compile_stage_pack(assignment, manifest, reports[1])
    verification = verify_stage_pack(
        pack,
        assignment=assignment,
        manifest=manifest,
    )
    binding: Any = None
    if binding_kind == "wrong_protocol":
        binding = _control_plane_binding()
        binding["protocol"] = "mycelium.control_plane_binding.v2"
    _set_present_assignment_pack_binding(assignment, pack, binding)
    reports[1]["assignment_id"] = assignment["assignment_id"]

    with pytest.raises(
        ValueError,
        match=r"^stage pack control-plane binding is invalid$",
    ):
        if surface == "compile":
            compile_stage_pack(assignment, manifest, reports[1])
        elif surface == "verify":
            verify_stage_pack(
                pack,
                assignment=assignment,
                manifest=manifest,
            )
        else:
            sp.validate_stage_pack_evidence(
                pack,
                verification,
                assignment=assignment,
                manifest=manifest,
            )


@pytest.mark.parametrize("deployment_epoch", (0, 7))
def test_per_pack_surfaces_accept_valid_control_plane_binding(
    tmp_path: Path,
    deployment_epoch: int,
) -> None:
    manifest, assignments, reports, _ = _case(
        tmp_path,
        deployment_epoch=deployment_epoch,
    )
    assignment = assignments[1]
    assignment["control_plane_binding"] = _control_plane_binding(
        deployment_epoch=deployment_epoch,
    )
    assignment["assignment_id"] = la.assignment_id_for(assignment)
    reports[1]["assignment_id"] = assignment["assignment_id"]

    pack = compile_stage_pack(assignment, manifest, reports[1])
    verification = verify_stage_pack(
        pack,
        assignment=assignment,
        manifest=manifest,
    )

    assert pack["control_plane_binding"] == assignment["control_plane_binding"]
    assert sp.validate_stage_pack_evidence(
        pack,
        verification,
        assignment=assignment,
        manifest=manifest,
    ) == (
        pack["stage_pack_digest"],
        verification["stage_pack_verification_digest"],
    )
    assert verification["route_ready"] is False


@pytest.mark.parametrize(
    ("field", "replacement"),
    (
        ("snapshot_generation", True),
        ("snapshot_generation", 1.0),
        ("deployment_epoch", True),
        ("deployment_epoch", 1.0),
    ),
    ids=(
        "bool-generation",
        "float-generation",
        "bool-epoch",
        "float-epoch",
    ),
)
@pytest.mark.parametrize("surface", ("verify", "evidence", "collection"))
def test_pack_only_numeric_binding_collisions_are_rejected(
    tmp_path: Path,
    field: str,
    replacement: Any,
    surface: str,
) -> None:
    manifest, assignments, reports, _ = _case(tmp_path, deployment_epoch=1)
    packs = [
        compile_stage_pack(assignment, manifest, report)
        for assignment, report in zip(assignments, reports, strict=True)
    ]
    binding = _control_plane_binding(
        snapshot_generation=1,
        deployment_epoch=1,
    )
    for assignment, pack in zip(assignments, packs, strict=True):
        _rebind_assignment_pack(
            assignment,
            pack,
            control_plane_binding=binding,
        )

    pack = packs[1]
    assignment = assignments[1]
    verification = verify_stage_pack(
        pack,
        assignment=assignment,
        manifest=manifest,
    )
    assignment_bytes = json.dumps(
        assignments,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    mutated_binding = copy.deepcopy(pack["control_plane_binding"])
    mutated_binding[field] = replacement
    pack["control_plane_binding"] = mutated_binding
    _refresh_digest(pack)
    verification["stage_pack_digest"] = pack["stage_pack_digest"]
    verification["stage_pack_verification_digest"] = sp._verification_digest_for(
        verification
    )

    assert (
        json.dumps(
            assignments,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        == assignment_bytes
    )
    with pytest.raises(ValueError) as raised:
        if surface == "verify":
            verify_stage_pack(
                pack,
                assignment=assignment,
                manifest=manifest,
            )
        elif surface == "evidence":
            sp.validate_stage_pack_evidence(
                pack,
                verification,
                assignment=assignment,
                manifest=manifest,
            )
        else:
            summary = sp.verify_stage_pack_collection(
                packs,
                assignments=assignments,
                manifest=manifest,
            )
            assert summary["exact_logical_coverage"] is True

    assert str(raised.value) == "stage pack control-plane binding is invalid"
    _assert_collection_error_is_value_free(
        raised.value,
        assignments=assignments,
        manifest=manifest,
        extra_forbidden=(
            json.dumps(mutated_binding, sort_keys=True, separators=(",", ":")),
        ),
    )


@pytest.mark.parametrize("binding_value", (0, 1, 7))
def test_pack_surfaces_accept_exact_integer_control_plane_bindings(
    tmp_path: Path,
    binding_value: int,
) -> None:
    manifest, assignments, reports, _ = _case(
        tmp_path,
        deployment_epoch=binding_value,
    )
    packs = [
        compile_stage_pack(assignment, manifest, report)
        for assignment, report in zip(assignments, reports, strict=True)
    ]
    binding = _control_plane_binding(
        snapshot_generation=binding_value,
        deployment_epoch=binding_value,
    )
    for assignment, pack in zip(assignments, packs, strict=True):
        _rebind_assignment_pack(
            assignment,
            pack,
            control_plane_binding=binding,
        )

    verification = verify_stage_pack(
        packs[1],
        assignment=assignments[1],
        manifest=manifest,
    )
    evidence_digests = sp.validate_stage_pack_evidence(
        packs[1],
        verification,
        assignment=assignments[1],
        manifest=manifest,
    )
    summary = sp.verify_stage_pack_collection(
        packs,
        assignments=assignments,
        manifest=manifest,
    )

    assert type(packs[1]["control_plane_binding"]["snapshot_generation"]) is int
    assert type(packs[1]["control_plane_binding"]["deployment_epoch"]) is int
    assert evidence_digests == (
        packs[1]["stage_pack_digest"],
        verification["stage_pack_verification_digest"],
    )
    assert summary["exact_logical_coverage"] is True
    assert summary["route_ready"] is False


def test_pack_surfaces_accept_legacy_none_for_missing_assignment_binding(
    tmp_path: Path,
) -> None:
    manifest, assignments, reports, _ = _case(tmp_path, deployment_epoch=1)
    packs = [
        compile_stage_pack(assignment, manifest, report)
        for assignment, report in zip(assignments, reports, strict=True)
    ]

    verification = verify_stage_pack(
        packs[1],
        assignment=assignments[1],
        manifest=manifest,
    )
    evidence_digests = sp.validate_stage_pack_evidence(
        packs[1],
        verification,
        assignment=assignments[1],
        manifest=manifest,
    )
    summary = sp.verify_stage_pack_collection(
        packs,
        assignments=assignments,
        manifest=manifest,
    )

    assert "control_plane_binding" not in assignments[1]
    assert packs[1]["control_plane_binding"] is None
    assert evidence_digests == (
        packs[1]["stage_pack_digest"],
        verification["stage_pack_verification_digest"],
    )
    assert summary["exact_logical_coverage"] is True
    assert summary["route_ready"] is False


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
    verified = verify_stage_pack(middle, assignment=assignments[1], manifest=manifest)
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

    reports = [
        wp.provision_assignment(assignment, fetch_file=fetch)
        for assignment in assignments
    ]
    packs = [
        compile_stage_pack(assignment, manifest, report)
        for assignment, report in zip(assignments, reports, strict=True)
    ]
    pack = packs[1]
    assert pack["component_aliases"] == {"lm_head": "input_embedding"}
    assert pack["component_tensor_keys"]["lm_head"] == ["transformer.wte.weight"]
    assert "embed.safetensors" in [item["upstream_path"] for item in pack["artifacts"]]
    assert verify_stage_pack(pack, assignment=final, manifest=manifest)["ready_for_load"] is True
    collection = sp.verify_stage_pack_collection(
        packs,
        assignments=assignments,
        manifest=manifest,
    )
    assert {
        record["tensor_key"] for record in collection["tied_aliases"]
    } == {"transformer.wte.weight"}
    assert {
        record["upstream_path"]
        for record in collection["shared_backing_artifacts"]
    } == {"blocks.safetensors", "embed.safetensors"}


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
        verify_stage_pack(pack, assignment=assignments[1], manifest=manifest)


def test_verifier_rejects_reidentified_assignment_that_diverges_from_manifest(
    tmp_path: Path,
) -> None:
    manifest, assignments, reports, _ = _case(tmp_path)
    pack = compile_stage_pack(assignments[1], manifest, reports[1])
    forged = copy.deepcopy(assignments[1])
    forged["components"] = ["decoder", "final_norm"]
    forged["assignment_id"] = la.assignment_id_for(forged)

    with pytest.raises(ValueError, match="components do not match manifest ownership"):
        verify_stage_pack(pack, assignment=forged, manifest=manifest)


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
        verify_stage_pack(pack, assignment=assignments[1], manifest=manifest)

    manifest, assignments, reports, _ = _case(tmp_path / "traversal")
    pack = compile_stage_pack(assignments[1], manifest, reports[1])
    pack["artifacts"][0]["relative_path"] = "../escape.safetensors"
    _refresh_digest(pack)
    with pytest.raises(ValueError, match="unsafe artifact path"):
        verify_stage_pack(pack, assignment=assignments[1], manifest=manifest)

    manifest, assignments, reports, _ = _case(tmp_path / "symlink")
    pack = compile_stage_pack(assignments[1], manifest, reports[1])
    artifact = Path(pack["artifact_root"]) / pack["artifacts"][0]["relative_path"]
    target = artifact.with_name("real.safetensors")
    artifact.rename(target)
    artifact.symlink_to(target.name)
    with pytest.raises(ValueError, match="symlink|open verified"):
        verify_stage_pack(pack, assignment=assignments[1], manifest=manifest)


def test_verifier_rejects_same_size_corruption_and_hardlinks(tmp_path: Path) -> None:
    manifest, assignments, reports, _ = _case(tmp_path / "same-size")
    pack = compile_stage_pack(assignments[1], manifest, reports[1])
    artifact = Path(pack["artifact_root"]) / pack["artifacts"][0]["relative_path"]
    with artifact.open("r+b") as handle:
        handle.seek(-1, os.SEEK_END)
        value = handle.read(1)
        handle.seek(-1, os.SEEK_END)
        handle.write(bytes([value[0] ^ 1]))
    with pytest.raises(ValueError, match="digest mismatch"):
        verify_stage_pack(pack, assignment=assignments[1], manifest=manifest)

    manifest, assignments, reports, _ = _case(tmp_path / "hardlink")
    pack = compile_stage_pack(assignments[1], manifest, reports[1])
    artifact = Path(pack["artifact_root"]) / pack["artifacts"][0]["relative_path"]
    original = artifact.with_name("original.safetensors")
    artifact.rename(original)
    os.link(original, artifact)
    with pytest.raises(ValueError, match="one hard link"):
        verify_stage_pack(pack, assignment=assignments[1], manifest=manifest)


def test_verifier_rejects_nested_and_root_symlinks(tmp_path: Path) -> None:
    manifest, assignments, reports, _ = _case(tmp_path / "nested")
    pack = compile_stage_pack(assignments[1], manifest, reports[1])
    root = Path(pack["artifact_root"])
    nested = root / "snapshot"
    real_nested = root / "real-snapshot"
    nested.rename(real_nested)
    nested.symlink_to(real_nested.name, target_is_directory=True)
    with pytest.raises(ValueError, match="symlink|open verified"):
        verify_stage_pack(pack, assignment=assignments[1], manifest=manifest)

    manifest, assignments, reports, _ = _case(tmp_path / "root-link")
    pack = compile_stage_pack(assignments[1], manifest, reports[1])
    root = Path(pack["artifact_root"])
    real_root = root.with_name(root.name + "-real")
    root.rename(real_root)
    root.symlink_to(real_root.name, target_is_directory=True)
    with pytest.raises(ValueError, match="root.*symlink"):
        verify_stage_pack(pack, assignment=assignments[1], manifest=manifest)


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
    for manifest_file in manifest["files"]:
        if manifest_file["path"] == artifact_record["upstream_path"]:
            manifest_file["size_bytes"] = artifact_record["size_bytes"]
            manifest_file["content_digest"] = {
                "algorithm": "sha256",
                "value": _sha(artifact),
            }
    unsigned_manifest = copy.deepcopy(manifest)
    unsigned_manifest.pop("manifest_digest")
    manifest["manifest_digest"] = {
        "algorithm": "sha256",
        "value": hashlib.sha256(
            mm.canonical_json(unsigned_manifest).encode("utf-8")
        ).hexdigest(),
    }
    assignment["manifest_digest"] = mm.manifest_digest_ref(manifest)
    for upstream in assignment["files"]:
        if upstream["path"] == artifact_record["upstream_path"]:
            upstream["size_bytes"] = artifact_record["size_bytes"]
            upstream["content_digest"] = artifact_record["content_digest"]
    assignment["assignment_id"] = la.assignment_id_for(assignment)
    pack["assignment_id"] = assignment["assignment_id"]
    pack["manifest_digest"] = assignment["manifest_digest"]
    pack["upstream_files"] = copy.deepcopy(assignment["files"])
    _refresh_digest(pack)
    with pytest.raises(ValueError, match="missing assigned tensors"):
        verify_stage_pack(pack, assignment=assignment, manifest=manifest)


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
        verify_stage_pack(pack, assignment=assignments[1], manifest=manifest)


def test_loader_report_is_bound_to_pack_and_assignment(tmp_path: Path) -> None:
    manifest, assignments, reports, _ = _case(tmp_path)
    pack = compile_stage_pack(assignments[1], manifest, reports[1])
    verification = verify_stage_pack(pack, assignment=assignments[1], manifest=manifest)
    report = artifact_report_for_loader(
        pack,
        verification,
        assignment=assignments[1],
        manifest=manifest,
    )

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
        "stage-pack evidence must be complete" in error
        for error in wp.artifact_report_errors(assignments[1], incomplete)
    )

    forged = copy.deepcopy(verification)
    forged["assignment_id"] = assignments[0]["assignment_id"]
    with pytest.raises(ValueError, match="verification digest|assignment"):
        artifact_report_for_loader(
            pack,
            forged,
            assignment=assignments[1],
            manifest=manifest,
        )


def test_stage_pack_digest_rejects_unknown_fields_nonfinite_and_duplicate_records(
    tmp_path: Path,
) -> None:
    manifest, assignments, reports, _ = _case(tmp_path)
    pack = compile_stage_pack(assignments[1], manifest, reports[1])
    pack["unexpected"] = True
    _refresh_digest(pack)
    with pytest.raises(ValueError, match="fields"):
        verify_stage_pack(pack, assignment=assignments[1], manifest=manifest)

    pack = compile_stage_pack(assignments[1], manifest, reports[1])
    pack["runtime"]["temperature"] = float("nan")
    with pytest.raises(ValueError, match="finite|JSON"):
        stage_pack_digest_for(pack)

    pack = compile_stage_pack(assignments[1], manifest, reports[1])
    pack["artifacts"].append(copy.deepcopy(pack["artifacts"][0]))
    _refresh_digest(pack)
    with pytest.raises(ValueError, match="duplicate artifact"):
        verify_stage_pack(pack, assignment=assignments[1], manifest=manifest)


def _twelve_layer_case(
    tmp_path: Path,
) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    source = tmp_path / "source-12"
    source.mkdir()
    tensors = {
        "shard-a.safetensors": [
            "bert.embeddings.word_embeddings.weight",
            *(f"bert.encoder.layer.{layer}.attention.self.query.weight" for layer in range(4)),
        ],
        "shard-b.safetensors": [
            "bert.encoder.layer.3.attention.self.key.weight",
            *(f"bert.encoder.layer.{layer}.attention.self.query.weight" for layer in range(4, 7)),
        ],
        "shard-c.safetensors": [
            "bert.encoder.layer.6.attention.self.key.weight",
            *(f"bert.encoder.layer.{layer}.attention.self.query.weight" for layer in range(7, 10)),
        ],
        "shard-d.safetensors": [
            "bert.encoder.layer.9.attention.self.key.weight",
            *(f"bert.encoder.layer.{layer}.attention.self.query.weight" for layer in range(10, 12)),
            "bert.pooler.dense.weight",
            "classifier.weight",
        ],
    }
    for name, names in tensors.items():
        _write_safetensors(source / name, names)
    manifest = mm.compile_model_manifest(
        model_id="org/bert-12-way",
        requested_revision="main",
        resolved_commit="d" * 40,
        config={
            "model_type": "bert",
            "num_hidden_layers": 12,
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
    boundaries = ((0, 3), (3, 6), (6, 8), (8, 10), (10, 12))
    route = {
        "ok": True,
        "protocol": "mycelium.manual_provisioning_route.v1",
        "claim_boundary": "five-way test allocation only",
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
                    "start_layer": start,
                    "end_layer_exclusive": end,
                    "layer_count": end - start,
                },
            }
            for index, (start, end) in enumerate(boundaries)
        ],
        "node_order": [f"node-{index}" for index in range(5)],
    }
    cache_roots = {
        f"node-{index}": str((tmp_path / f"cache-12-{index}").resolve())
        for index in range(5)
    }
    assignments = la.compile_layer_assignments(
        route_plan=route,
        manifest=manifest,
        deployment_id=DEPLOYMENT_ID,
        deployment_epoch=12,
        cache_roots=cache_roots,
        runtime_by_node={
            node: {
                "backend": "artifact_verifier",
                "dtype": "source",
                "quantization": "none",
            }
            for node in cache_roots
        },
    )
    reports: list[dict[str, Any]] = []
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
    return manifest, assignments, reports


def test_five_way_packs_cover_uneven_ranges_and_shared_boundary_shards(
    tmp_path: Path,
) -> None:
    manifest, assignments, reports = _twelve_layer_case(tmp_path)
    packs = [
        compile_stage_pack(assignment, manifest, report)
        for assignment, report in zip(assignments, reports, strict=True)
    ]

    expected_ranges = [(0, 3), (3, 6), (6, 8), (8, 10), (10, 12)]
    assert [
        (pack["range"]["start_layer"], pack["range"]["end_layer_exclusive"])
        for pack in packs
    ] == expected_ranges
    assert packs[0]["range"]["start_layer"] == 0
    assert packs[-1]["range"]["end_layer_exclusive"] == 12
    assert all(
        left["range"]["end_layer_exclusive"]
        == right["range"]["start_layer"]
        for left, right in zip(packs, packs[1:])
    )
    artifact_sets = [
        {record["upstream_path"] for record in pack["artifacts"]}
        for pack in packs
    ]
    for index, shared in enumerate(
        (
            "shard-a.safetensors",
            "shard-b.safetensors",
            "shard-c.safetensors",
            "shard-d.safetensors",
        )
    ):
        assert shared in artifact_sets[index] & artifact_sets[index + 1]
    for pack, assignment in zip(packs, assignments, strict=True):
        assert pack["expected_tensor_keys"] == assignment["expected_tensor_keys"]
        assert pack["route_ready"] is False
        verification = verify_stage_pack(pack, assignment=assignment, manifest=manifest)
        assert verification["ready_for_load"] is True
        assert verification["route_ready"] is False


def test_frozen_dialogpt_fp16_tolerances_are_source_bound_and_digest_pinned(
    tmp_path: Path,
) -> None:
    path = Path("tolerances/dialogpt-small-fp16.json")
    expected_artifact_digest = (
        "sha256:ea50e39b0e9368b9110cc5ab48012ddbc7bd90f2b17410aed643a7404acac567"
    )
    policy = load_fp16_tolerances(
        path,
        expected_model_id="microsoft/DialoGPT-small",
        expected_resolved_commit="49c537161a457d5256512f9d2d38a87d81ae0f0e",
        expected_model_artifact_digest=expected_artifact_digest,
    )

    assert policy["protocol"] == FP16_TOLERANCE_PROTOCOL
    assert policy["runtime"] == {
        "backend": "mlx",
        "dtype": "float16",
        "quantization": "none",
    }
    assert policy["checks"]["token_ids"] == {"exact": True}
    assert policy["route_ready"] is False
    assert policy["freeze"]["post_hoc_fitted"] is False

    mutated = copy.deepcopy(policy)
    mutated["checks"]["logits"]["absolute_tolerance"] *= 2
    tampered_path = tmp_path / "tampered.json"
    tampered_path.write_text(
        json.dumps(mutated, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="digest"):
        load_fp16_tolerances(
            tampered_path,
            expected_model_id="microsoft/DialoGPT-small",
            expected_resolved_commit="49c537161a457d5256512f9d2d38a87d81ae0f0e",
            expected_model_artifact_digest=expected_artifact_digest,
        )
    with pytest.raises(ValueError, match="source identity"):
        load_fp16_tolerances(
            path,
            expected_model_id="microsoft/DialoGPT-small",
            expected_resolved_commit="0" * 40,
            expected_model_artifact_digest=expected_artifact_digest,
        )


def _three_way_source_tensor_keys() -> set[str]:
    return {
        "bert.embeddings.word_embeddings.weight",
        "bert.encoder.layer.0.attention.self.query.weight",
        "bert.encoder.layer.1.attention.self.key.weight",
        "bert.encoder.layer.1.attention.self.query.weight",
        "bert.encoder.layer.2.attention.self.query.weight",
        "bert.pooler.dense.weight",
        "classifier.weight",
    }


def _five_way_source_tensor_keys() -> set[str]:
    return {
        "bert.embeddings.word_embeddings.weight",
        *(f"bert.encoder.layer.{layer}.attention.self.query.weight" for layer in range(12)),
        "bert.encoder.layer.3.attention.self.key.weight",
        "bert.encoder.layer.6.attention.self.key.weight",
        "bert.encoder.layer.9.attention.self.key.weight",
        "bert.pooler.dense.weight",
        "classifier.weight",
    }


def _assert_exact_logical_ownership(
    summary: dict[str, Any],
    packs: list[dict[str, Any]],
    *,
    source_tensor_keys: set[str],
) -> None:
    owned_sets = [set(pack["expected_tensor_keys"]) for pack in packs]
    tied_alias_keys = {
        record["tensor_key"] for record in summary["tied_aliases"]
    }

    for index, left in enumerate(owned_sets):
        for right in owned_sets[index + 1 :]:
            assert left & right <= tied_alias_keys
    assert set().union(*owned_sets) == source_tensor_keys
    assert set(summary["logical_source_tensor_keys"]) == source_tensor_keys
    assert summary["exact_logical_coverage"] is True
    assert summary["route_ready"] is False


def test_three_way_collection_has_exact_logical_ownership_and_round_trips_middle(
    tmp_path: Path,
) -> None:
    manifest, assignments, reports, _ = _case(tmp_path)
    packs = [
        compile_stage_pack(assignment, manifest, report)
        for assignment, report in zip(assignments, reports, strict=True)
    ]

    summary = sp.verify_stage_pack_collection(
        packs,
        assignments=assignments,
        manifest=manifest,
    )
    _assert_exact_logical_ownership(
        summary,
        packs,
        source_tensor_keys=_three_way_source_tensor_keys(),
    )
    assert summary["tied_aliases"] == []

    middle = json.loads(
        json.dumps(packs[1], sort_keys=True, separators=(",", ":"))
    )
    assert middle["components"] == ["decoder"]
    assert middle["range"] == {
        "start_layer": 1,
        "end_layer_exclusive": 2,
        "layer_count": 1,
    }
    assert verify_stage_pack(
        middle,
        assignment=assignments[1],
        manifest=manifest,
    )["verified_tensor_keys"] == assignments[1]["expected_tensor_keys"]


def test_five_way_collection_has_exact_logical_ownership_with_shared_shards(
    tmp_path: Path,
) -> None:
    manifest, assignments, reports = _twelve_layer_case(tmp_path)
    packs = [
        compile_stage_pack(assignment, manifest, report)
        for assignment, report in zip(assignments, reports, strict=True)
    ]

    summary = sp.verify_stage_pack_collection(
        packs,
        assignments=assignments,
        manifest=manifest,
    )
    _assert_exact_logical_ownership(
        summary,
        packs,
        source_tensor_keys=_five_way_source_tensor_keys(),
    )
    assert summary["tied_aliases"] == []
    assert {
        record["upstream_path"]
        for record in summary["shared_backing_artifacts"]
    } == {
        "shard-a.safetensors",
        "shard-b.safetensors",
        "shard-c.safetensors",
        "shard-d.safetensors",
    }
    assert any(
        record["overfetched_tensor_count"] > 0
        for record in summary["pack_verifications"]
    )


@pytest.mark.parametrize("drift", ("backend", "dtype", "model_config"))
def test_collection_verifier_rejects_mixed_canonical_runtime_identity(
    tmp_path: Path,
    drift: str,
) -> None:
    manifest, assignments, reports, _ = _case(tmp_path)
    packs = [
        compile_stage_pack(assignment, manifest, report)
        for assignment, report in zip(assignments, reports, strict=True)
    ]
    for assignment, pack in zip(assignments, packs, strict=True):
        _rebind_assignment_pack(assignment, pack, runtime=_mlx_runtime())

    divergent_runtime = _mlx_runtime()
    if drift == "backend":
        divergent_runtime = {
            "backend": "artifact_verifier",
            "dtype": "source",
            "quantization": "none",
        }
    elif drift == "dtype":
        divergent_runtime["dtype"] = "float16"
    else:
        divergent_runtime["model_config"]["n_positions"] = 16
    _rebind_assignment_pack(
        assignments[1],
        packs[1],
        runtime=divergent_runtime,
    )

    assert all(
        verify_stage_pack(pack, assignment=assignment, manifest=manifest)[
            "ready_for_load"
        ]
        for assignment, pack in zip(assignments, packs, strict=True)
    )
    with pytest.raises(
        ValueError,
        match=r"^stage pack collection runtime identity mismatch$",
    ):
        sp.verify_stage_pack_collection(
            packs,
            assignments=assignments,
            manifest=manifest,
        )


@pytest.mark.parametrize(
    "drift",
    (
        "missing",
        "snapshot_generation",
        "evidence_bundle_digest",
        "planner_snapshot_digest",
    ),
)
def test_collection_verifier_rejects_mixed_control_plane_binding_identity(
    tmp_path: Path,
    drift: str,
) -> None:
    manifest, assignments, reports, _ = _case(tmp_path)
    packs = [
        compile_stage_pack(assignment, manifest, report)
        for assignment, report in zip(assignments, reports, strict=True)
    ]
    binding = _control_plane_binding()
    for assignment, pack in zip(assignments, packs, strict=True):
        _rebind_assignment_pack(
            assignment,
            pack,
            control_plane_binding=binding,
        )

    divergent_binding = copy.deepcopy(binding)
    if drift == "missing":
        replacement = None
    elif drift == "snapshot_generation":
        divergent_binding[drift] = 2
        replacement = divergent_binding
    else:
        divergent_binding[drift] = "sha256:" + "f" * 64
        replacement = divergent_binding
    _rebind_assignment_pack(
        assignments[1],
        packs[1],
        control_plane_binding=replacement,
    )

    assert all(
        verify_stage_pack(pack, assignment=assignment, manifest=manifest)[
            "ready_for_load"
        ]
        for assignment, pack in zip(assignments, packs, strict=True)
    )
    with pytest.raises(
        ValueError,
        match=r"^stage pack collection control-plane binding identity mismatch$",
    ):
        sp.verify_stage_pack_collection(
            packs,
            assignments=assignments,
            manifest=manifest,
        )


def test_collection_verifier_rejects_invalid_control_plane_bindings(
    tmp_path: Path,
) -> None:
    manifest, base_assignments, reports, _ = _case(tmp_path)
    base_packs = [
        compile_stage_pack(assignment, manifest, report)
        for assignment, report in zip(base_assignments, reports, strict=True)
    ]
    attacks = (
        "not_object",
        "missing_field",
        "extra_field",
        "wrong_protocol",
        "malformed_evidence_digest",
        "malformed_snapshot_digest",
        "negative_generation",
        "bool_generation",
        "float_generation",
        "empty_swarm",
        "wrong_deployment",
        "negative_epoch",
        "wrong_epoch",
        "bool_epoch",
        "float_epoch",
        "explicit_none",
        "type_collision_generation",
        "type_collision_epoch",
    )

    for attack in attacks:
        assignments = copy.deepcopy(base_assignments)
        packs = copy.deepcopy(base_packs)
        if attack == "explicit_none":
            for assignment, pack in zip(assignments, packs, strict=True):
                _set_present_assignment_pack_binding(
                    assignment,
                    pack,
                    None,
                )
        else:
            binding: Any = _control_plane_binding()
            if attack == "not_object":
                binding = ["mycelium.control_plane_binding.v1"]
            elif attack == "missing_field":
                binding.pop("swarm_id")
            elif attack == "extra_field":
                binding["unexpected"] = "field"
            elif attack == "wrong_protocol":
                binding["protocol"] = "mycelium.control_plane_binding.v2"
            elif attack == "malformed_evidence_digest":
                binding["evidence_bundle_digest"] = "sha256:" + "A" * 64
            elif attack == "malformed_snapshot_digest":
                binding["planner_snapshot_digest"] = "sha256:short"
            elif attack == "negative_generation":
                binding["snapshot_generation"] = -1
            elif attack == "bool_generation":
                binding["snapshot_generation"] = True
            elif attack == "float_generation":
                binding["snapshot_generation"] = 1.0
            elif attack == "empty_swarm":
                binding["swarm_id"] = ""
            elif attack == "wrong_deployment":
                binding["deployment_id"] = "87654321-4321-8765-9234-fedcbafedcba"
            elif attack == "negative_epoch":
                binding["deployment_epoch"] = -1
            elif attack == "wrong_epoch":
                binding["deployment_epoch"] = 8
            elif attack == "bool_epoch":
                binding["deployment_epoch"] = True
            elif attack == "float_epoch":
                binding["deployment_epoch"] = 7.0
            for assignment, pack in zip(assignments, packs, strict=True):
                _rebind_assignment_pack(
                    assignment,
                    pack,
                    control_plane_binding=binding,
                )

        if attack in ("type_collision_generation", "type_collision_epoch"):
            type_colliding = _control_plane_binding()
            field, replacement = (
                ("snapshot_generation", True)
                if attack == "type_collision_generation"
                else ("deployment_epoch", 7.0)
            )
            type_colliding[field] = replacement
            _rebind_assignment_pack(
                assignments[1],
                packs[1],
                control_plane_binding=type_colliding,
            )

        with pytest.raises(ValueError) as raised:
            sp.verify_stage_pack_collection(
                packs,
                assignments=assignments,
                manifest=manifest,
            )
        assert str(raised.value) == "stage pack control-plane binding is invalid"
        _assert_collection_error_is_value_free(
            raised.value,
            assignments=assignments,
            manifest=manifest,
            extra_forbidden=("control_plane_binding",),
        )


@pytest.mark.parametrize(
    "deployment_epoch",
    (True, 7.0, -1, "7"),
    ids=("bool", "float", "negative", "string"),
)
def test_stage_pack_verifier_rejects_invalid_pack_deployment_epoch(
    tmp_path: Path,
    deployment_epoch: Any,
) -> None:
    manifest, assignments, reports, _ = _case(tmp_path)
    pack = compile_stage_pack(assignments[0], manifest, reports[0])
    pack["deployment_epoch"] = deployment_epoch
    _refresh_digest(pack)

    with pytest.raises(
        ValueError,
        match=r"^stage pack deployment epoch is invalid$",
    ):
        verify_stage_pack(
            pack,
            assignment=assignments[0],
            manifest=manifest,
        )


@pytest.mark.parametrize(
    "deployment_epoch",
    (True, 7.0, -1, "7"),
    ids=("bool", "float", "negative", "string"),
)
def test_stage_pack_verifier_rejects_invalid_assignment_deployment_epoch(
    tmp_path: Path,
    deployment_epoch: Any,
) -> None:
    manifest, assignments, reports, _ = _case(tmp_path)
    assignment = assignments[0]
    pack = compile_stage_pack(assignment, manifest, reports[0])
    assignment["deployment_epoch"] = deployment_epoch
    assignment["assignment_id"] = la.assignment_id_for(assignment)
    pack["assignment_id"] = assignment["assignment_id"]
    _refresh_digest(pack)

    with pytest.raises(
        ValueError,
        match=r"^stage pack deployment epoch is invalid$",
    ):
        verify_stage_pack(
            pack,
            assignment=assignment,
            manifest=manifest,
        )


@pytest.mark.parametrize(
    "deployment_epoch",
    (True, 7.0, -1, "7"),
    ids=("bool", "float", "negative", "string"),
)
def test_collection_verifier_rejects_invalid_legacy_deployment_epoch(
    tmp_path: Path,
    deployment_epoch: Any,
) -> None:
    manifest, assignments, reports, _ = _case(tmp_path)
    packs = [
        compile_stage_pack(assignment, manifest, report)
        for assignment, report in zip(assignments, reports, strict=True)
    ]
    _set_legacy_deployment_epoch(assignments, packs, deployment_epoch)

    with pytest.raises(
        ValueError,
        match=r"^stage pack deployment epoch is invalid$",
    ):
        sp.verify_stage_pack_collection(
            packs,
            assignments=assignments,
            manifest=manifest,
        )


def test_collection_verifier_accepts_reordered_control_plane_binding_keys(
    tmp_path: Path,
) -> None:
    manifest, assignments, reports, _ = _case(tmp_path)
    packs = [
        compile_stage_pack(assignment, manifest, report)
        for assignment, report in zip(assignments, reports, strict=True)
    ]
    binding = _control_plane_binding()
    for assignment, pack in zip(assignments, packs, strict=True):
        _rebind_assignment_pack(
            assignment,
            pack,
            control_plane_binding=binding,
        )
    reordered = dict(reversed(list(binding.items())))
    _rebind_assignment_pack(
        assignments[1],
        packs[1],
        control_plane_binding=reordered,
    )

    summary = sp.verify_stage_pack_collection(
        packs,
        assignments=assignments,
        manifest=manifest,
    )

    assert summary["exact_logical_coverage"] is True
    assert summary["route_ready"] is False


@pytest.mark.parametrize("with_control_plane_binding", (False, True))
def test_collection_verifier_accepts_uniform_canonical_identities(
    tmp_path: Path,
    with_control_plane_binding: bool,
) -> None:
    manifest, assignments, reports, _ = _case(tmp_path)
    packs = [
        compile_stage_pack(assignment, manifest, report)
        for assignment, report in zip(assignments, reports, strict=True)
    ]
    binding = _control_plane_binding() if with_control_plane_binding else None
    for assignment, pack in zip(assignments, packs, strict=True):
        _rebind_assignment_pack(
            assignment,
            pack,
            runtime=_mlx_runtime("float16"),
            control_plane_binding=binding,
        )

    summary = sp.verify_stage_pack_collection(
        json.loads(json.dumps(packs)),
        assignments=json.loads(json.dumps(assignments)),
        manifest=manifest,
    )

    assert summary["exact_logical_coverage"] is True
    assert summary["route_ready"] is False
    assert all(
        pack["control_plane_binding"] == binding
        for pack in packs
    )


def test_collection_verifier_aggregates_only_authenticated_entry_snapshots(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest, assignments, reports, _ = _case(tmp_path)
    packs = [
        compile_stage_pack(assignment, manifest, report)
        for assignment, report in zip(assignments, reports, strict=True)
    ]
    authenticated_keys = copy.deepcopy(packs[1]["expected_tensor_keys"])
    actual_verify = sp.verify_stage_pack
    verification_count = 0

    def verify_then_mutate_original(
        pack: dict[str, Any],
        *,
        assignment: dict[str, Any],
        manifest: dict[str, Any],
    ) -> dict[str, Any]:
        nonlocal verification_count
        verification = actual_verify(
            pack,
            assignment=assignment,
            manifest=manifest,
        )
        if assignment["node_id"] == "node-1":
            pack["expected_tensor_keys"].reverse()
        verification_count += 1
        if verification_count == len(packs):
            packs[1]["expected_tensor_keys"].reverse()
        return verification

    monkeypatch.setattr(sp, "verify_stage_pack", verify_then_mutate_original)

    summary = sp.verify_stage_pack_collection(
        packs,
        assignments=assignments,
        manifest=manifest,
    )

    assert verification_count == len(packs)
    assert summary["exact_logical_coverage"] is True
    assert summary["logical_owned_tensor_keys"][1]["tensor_keys"] == authenticated_keys
    with pytest.raises(ValueError, match=r"^stage pack digest mismatch$"):
        actual_verify(
            packs[1],
            assignment=assignments[1],
            manifest=manifest,
        )

    detached_summary = copy.deepcopy(summary)
    packs[0]["expected_tensor_keys"].clear()
    assignments[0]["range"]["start_layer"] = 99
    manifest["tensor_keys_by_layer"].clear()
    assert summary == detached_summary


@pytest.mark.parametrize(
    ("field", "replacement"),
    (
        ("deployment_id", "87654321-4321-8765-9234-fedcbafedcba"),
        ("deployment_epoch", 8),
    ),
)
def test_collection_identity_errors_are_fixed_and_value_free(
    tmp_path: Path,
    field: str,
    replacement: Any,
) -> None:
    manifest, assignments, reports, _ = _case(tmp_path)
    packs = [
        compile_stage_pack(assignment, manifest, report)
        for assignment, report in zip(assignments, reports, strict=True)
    ]
    assignments[1][field] = replacement
    assignments[1]["assignment_id"] = la.assignment_id_for(assignments[1])
    packs[1][field] = replacement
    packs[1]["assignment_id"] = assignments[1]["assignment_id"]
    _refresh_digest(packs[1])
    assert verify_stage_pack(
        packs[1],
        assignment=assignments[1],
        manifest=manifest,
    )["ready_for_load"] is True

    with pytest.raises(ValueError) as raised:
        sp.verify_stage_pack_collection(
            packs,
            assignments=assignments,
            manifest=manifest,
        )

    assert str(raised.value) == "stage pack collection identity mismatch"
    _assert_collection_error_is_value_free(
        raised.value,
        assignments=assignments,
        manifest=manifest,
        extra_forbidden=(field, str(replacement)),
    )


def test_collection_exact_union_error_is_fixed_and_uses_verification_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest, assignments, reports, _ = _case(tmp_path)
    packs = [
        compile_stage_pack(assignment, manifest, report)
        for assignment, report in zip(assignments, reports, strict=True)
    ]
    actual_verify = sp.verify_stage_pack

    def verify_with_omission(
        pack: dict[str, Any],
        *,
        assignment: dict[str, Any],
        manifest: dict[str, Any],
    ) -> dict[str, Any]:
        verification = actual_verify(
            pack,
            assignment=assignment,
            manifest=manifest,
        )
        if assignment["node_id"] == "node-1":
            omitted = verification["verified_tensor_keys"].pop()
            verification["tensor_file_map"].pop(omitted)
            verification["verified_tensor_count"] -= 1
        return verification

    monkeypatch.setattr(sp, "verify_stage_pack", verify_with_omission)

    with pytest.raises(ValueError) as raised:
        sp.verify_stage_pack_collection(
            packs,
            assignments=assignments,
            manifest=manifest,
        )

    assert (
        str(raised.value)
        == "stage pack collection logical tensor ownership mismatch"
    )
    _assert_collection_error_is_value_free(
        raised.value,
        assignments=assignments,
        manifest=manifest,
        extra_forbidden=("verified_tensor_keys",),
    )


def test_collection_duplicate_ownership_error_is_fixed_and_value_free(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest, assignments, reports, _ = _case(tmp_path)
    packs = [
        compile_stage_pack(assignment, manifest, report)
        for assignment, report in zip(assignments, reports, strict=True)
    ]
    duplicate_key = assignments[0]["expected_tensor_keys"][0]
    actual_verify = sp.verify_stage_pack

    def verify_with_duplicate(
        pack: dict[str, Any],
        *,
        assignment: dict[str, Any],
        manifest: dict[str, Any],
    ) -> dict[str, Any]:
        verification = actual_verify(
            pack,
            assignment=assignment,
            manifest=manifest,
        )
        if assignment["node_id"] == "node-1":
            verification["verified_tensor_keys"].append(duplicate_key)
            verification["verified_tensor_keys"].sort()
            verification["verified_tensor_count"] += 1
        return verification

    monkeypatch.setattr(sp, "verify_stage_pack", verify_with_duplicate)

    with pytest.raises(ValueError) as raised:
        sp.verify_stage_pack_collection(
            packs,
            assignments=assignments,
            manifest=manifest,
        )

    assert (
        str(raised.value)
        == "stage pack collection has duplicate logical tensor ownership"
    )
    _assert_collection_error_is_value_free(
        raised.value,
        assignments=assignments,
        manifest=manifest,
        extra_forbidden=(duplicate_key,),
    )


@pytest.mark.parametrize("mutation", ("swap", "gap"))
def test_collection_verifier_preserves_order_and_range_rejections(
    tmp_path: Path,
    mutation: str,
) -> None:
    manifest, assignments, reports, _ = _case(tmp_path)
    packs = [
        compile_stage_pack(assignment, manifest, report)
        for assignment, report in zip(assignments, reports, strict=True)
    ]
    if mutation == "swap":
        assignments[0], assignments[1] = assignments[1], assignments[0]
        packs[0], packs[1] = packs[1], packs[0]
    else:
        assignments.pop(1)
        packs.pop(1)

    with pytest.raises(ValueError, match="ranges overlap or contain a gap"):
        sp.verify_stage_pack_collection(
            packs,
            assignments=assignments,
            manifest=manifest,
        )


@pytest.mark.parametrize("mutation", ("omit", "duplicate", "misassign"))
def test_collection_verifier_rejects_mutated_logical_tensor_ownership(
    tmp_path: Path,
    mutation: str,
) -> None:
    manifest, assignments, reports, _ = _case(tmp_path)
    packs = [
        compile_stage_pack(assignment, manifest, report)
        for assignment, report in zip(assignments, reports, strict=True)
    ]
    mutated = copy.deepcopy(packs)
    middle = mutated[1]
    middle_keys = middle["component_tensor_keys"]["decoder"]

    if mutation == "omit":
        omitted = middle_keys.pop()
        middle["expected_tensor_keys"].remove(omitted)
    elif mutation == "duplicate":
        middle["expected_tensor_keys"].append(middle["expected_tensor_keys"][0])
    else:
        middle_keys[0] = assignments[0]["expected_tensor_keys"][0]
        middle_keys.sort()
        middle["expected_tensor_keys"] = sorted(middle_keys)
    _refresh_digest(middle)

    with pytest.raises(ValueError, match="ownership|assignment"):
        sp.verify_stage_pack_collection(
            mutated,
            assignments=assignments,
            manifest=manifest,
        )
