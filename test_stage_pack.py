from __future__ import annotations

import copy
import hashlib
import json
import math
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


class _StrictDictCollision(dict[Any, Any]):
    pass


class _StrictListCollision(list[Any]):
    pass


class _StrictStrCollision(str):
    pass


class _StrictIntCollision(int):
    pass


class _DeepcopyBomb:
    def __init__(self, calls: list[str]) -> None:
        self.calls = calls

    def __deepcopy__(self, memo: dict[int, Any]) -> Any:
        self.calls.append("deepcopy")
        raise RuntimeError("PRIVATE-assignment-deepcopy")


class _ArmedStr(str):
    def __new__(cls, value: str, calls: list[str]) -> _ArmedStr:
        instance = super().__new__(cls, value)
        instance.calls = calls
        return instance

    def _trip(self, operation: str) -> Any:
        self.calls.append(operation)
        raise RuntimeError("PRIVATE-armed-scalar")

    def __copy__(self) -> Any:
        return self._trip("copy")

    def __deepcopy__(self, memo: dict[int, Any]) -> Any:
        return self._trip("deepcopy")

    def __eq__(self, other: object) -> bool:
        return self._trip("eq")

    def __ne__(self, other: object) -> bool:
        return self._trip("ne")

    def __str__(self) -> str:
        return self._trip("str")

    def __bool__(self) -> bool:
        return self._trip("bool")

    __hash__ = str.__hash__


class _ArmedInt(int):
    def __new__(cls, value: int, calls: list[str]) -> _ArmedInt:
        instance = super().__new__(cls, value)
        instance.calls = calls
        return instance

    def _trip(self, operation: str) -> Any:
        self.calls.append(operation)
        raise RuntimeError("PRIVATE-armed-scalar")

    def __copy__(self) -> Any:
        return self._trip("copy")

    def __deepcopy__(self, memo: dict[int, Any]) -> Any:
        return self._trip("deepcopy")

    def __eq__(self, other: object) -> bool:
        return self._trip("eq")

    def __ne__(self, other: object) -> bool:
        return self._trip("ne")

    def __int__(self) -> int:
        return self._trip("int")

    def __float__(self) -> float:
        return self._trip("float")

    def __bool__(self) -> bool:
        return self._trip("bool")

    __hash__ = int.__hash__


class _ArmedFloat(float):
    def __new__(cls, value: float, calls: list[str]) -> _ArmedFloat:
        instance = super().__new__(cls, value)
        instance.calls = calls
        return instance

    def _trip(self, operation: str) -> Any:
        self.calls.append(operation)
        raise RuntimeError("PRIVATE-armed-scalar")

    def __copy__(self) -> Any:
        return self._trip("copy")

    def __deepcopy__(self, memo: dict[int, Any]) -> Any:
        return self._trip("deepcopy")

    def __eq__(self, other: object) -> bool:
        return self._trip("eq")

    def __ne__(self, other: object) -> bool:
        return self._trip("ne")

    def __int__(self) -> int:
        return self._trip("int")

    def __float__(self) -> float:
        return self._trip("float")

    def __bool__(self) -> bool:
        return self._trip("bool")

    __hash__ = float.__hash__


class _ArmedDict(dict[Any, Any]):
    def __init__(self, value: dict[Any, Any], calls: list[str]) -> None:
        super().__init__(value)
        self.calls = calls

    def __deepcopy__(self, memo: dict[int, Any]) -> Any:
        self.calls.append("deepcopy")
        raise RuntimeError("PRIVATE-armed-container")

    def __iter__(self) -> Any:
        self.calls.append("iter")
        raise RuntimeError("PRIVATE-armed-container")

    def items(self) -> Any:
        self.calls.append("items")
        raise RuntimeError("PRIVATE-armed-container")


class _ArmedList(list[Any]):
    def __init__(self, value: list[Any], calls: list[str]) -> None:
        super().__init__(value)
        self.calls = calls

    def __deepcopy__(self, memo: dict[int, Any]) -> Any:
        self.calls.append("deepcopy")
        raise RuntimeError("PRIVATE-armed-container")

    def __iter__(self) -> Any:
        self.calls.append("iter")
        raise RuntimeError("PRIVATE-armed-container")


class _BoolLike:
    def __init__(self, calls: list[str]) -> None:
        self.calls = calls

    def __deepcopy__(self, memo: dict[int, Any]) -> Any:
        self.calls.append("deepcopy")
        raise RuntimeError("PRIVATE-bool-like")

    def __bool__(self) -> bool:
        self.calls.append("bool")
        raise RuntimeError("PRIVATE-bool-like")


class _AssignmentFileKey(str):
    def __new__(cls, value: str) -> _AssignmentFileKey:
        instance = super().__new__(cls, value)
        instance.armed = False
        instance.equality_calls = 0
        return instance

    def __eq__(self, other: object) -> bool:
        self.equality_calls += 1
        if self.armed:
            raise RuntimeError("assignment file key equality was invoked")
        return super().__eq__(other)

    __hash__ = str.__hash__


class _ExactSchemaKey(str):
    def __new__(cls, value: str) -> _ExactSchemaKey:
        instance = super().__new__(cls, value)
        instance.armed = False
        instance.equality_calls = 0
        return instance

    def __eq__(self, other: object) -> bool:
        self.equality_calls += 1
        if self.armed:
            raise RuntimeError("exact schema key equality was invoked")
        return super().__eq__(other)

    __hash__ = str.__hash__


def _replace_with_exact_schema_key(
    mapping: dict[str, Any],
    field: str,
) -> _ExactSchemaKey:
    value = mapping.pop(field)
    key = _ExactSchemaKey(field)
    mapping[key] = value
    return key


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


def _refresh_manifest_digest(manifest: dict[str, Any]) -> None:
    unsigned = copy.deepcopy(manifest)
    unsigned.pop("manifest_digest", None)
    manifest["manifest_digest"] = mm._digest_document(unsigned)


def _alias_collection_case(
    tmp_path: Path,
    *,
    aliases: dict[Any, Any],
    tied_sources: tuple[str, ...] = ("classifier",),
) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    source = tmp_path / "source"
    source.mkdir(parents=True)
    manifest, _ = _manifest_and_files(source)
    tied_key = manifest["component_tensor_keys"]["input_embedding"][0]
    for component in tied_sources:
        manifest["component_tensor_keys"][component] = [tied_key]
        manifest["component_files"][component] = ["a.safetensors"]
    manifest["component_aliases"] = copy.deepcopy(aliases)
    _refresh_manifest_digest(manifest)
    assignments = la.compile_layer_assignments(
        route_plan=_route(manifest),
        manifest=manifest,
        deployment_id=DEPLOYMENT_ID,
        deployment_epoch=7,
        cache_roots={
            f"node-{index}": str((tmp_path / f"cache-{index}").resolve())
            for index in range(3)
        },
        runtime_by_node={
            f"node-{index}": {
                "backend": "artifact_verifier",
                "dtype": "source",
                "quantization": "none",
            }
            for index in range(3)
        },
    )

    def fetch(
        model_id: str,
        revision: str,
        filename: str,
        cache_root: str | Path,
        local_files_only: bool = False,
    ) -> tuple[Path, bool]:
        target = Path(cache_root) / filename
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source / filename, target)
        return target, True

    reports = [
        wp.provision_assignment(
            assignment,
            fetch_file=fetch,
            local_files_only=True,
        )
        for assignment in assignments
    ]
    packs = [
        compile_stage_pack(assignment, manifest, report)
        for assignment, report in zip(assignments, reports, strict=True)
    ]
    return manifest, assignments, packs


def _replace_collection_aliases(
    manifest: dict[str, Any],
    assignments: list[dict[str, Any]],
    packs: list[dict[str, Any]],
    aliases: dict[Any, Any],
) -> None:
    manifest["component_aliases"] = copy.deepcopy(aliases)
    _refresh_manifest_digest(manifest)
    manifest_digest = mm.manifest_digest_ref(manifest)
    for assignment, pack in zip(assignments, packs, strict=True):
        component_aliases = {
            source: copy.deepcopy(target)
            for source, target in aliases.items()
            if source in assignment["components"]
        }
        assignment["manifest_digest"] = manifest_digest
        assignment["component_aliases"] = component_aliases
        assignment["assignment_id"] = la.assignment_id_for(assignment)
        pack["manifest_digest"] = manifest_digest
        pack["component_aliases"] = copy.deepcopy(component_aliases)
        pack["assignment_id"] = assignment["assignment_id"]
        _refresh_digest(pack)


def _refresh_digest(pack: dict[str, Any]) -> None:
    pack["stage_pack_digest"] = stage_pack_digest_for(pack)


def _refresh_joint_evidence_digests(
    pack: dict[str, Any],
    verification: dict[str, Any],
) -> None:
    _refresh_digest(pack)
    verification["stage_pack_digest"] = pack["stage_pack_digest"]
    verification["stage_pack_verification_digest"] = sp._verification_digest_for(
        verification
    )


def _swap_embedded_tensor_ownership(
    pack: dict[str, Any],
    verification: dict[str, Any],
    left_key: str,
    right_key: str,
) -> None:
    artifacts_by_path = {
        record["upstream_path"]: record for record in pack["artifacts"]
    }
    verified_by_path = {
        record["path"]: record for record in verification["verified_files"]
    }
    left_path = verification["tensor_file_map"][left_key]
    right_path = verification["tensor_file_map"][right_key]
    assert left_path != right_path
    for records_by_path in (artifacts_by_path, verified_by_path):
        left_keys = records_by_path[left_path]["tensor_keys"]
        right_keys = records_by_path[right_path]["tensor_keys"]
        left_keys.remove(left_key)
        right_keys.remove(right_key)
        left_keys.append(right_key)
        right_keys.append(left_key)
        left_keys.sort()
        right_keys.sort()
    verification["tensor_file_map"][left_key] = right_path
    verification["tensor_file_map"][right_key] = left_path
    _refresh_joint_evidence_digests(pack, verification)


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


def _rebind_assignment_file_evidence(
    assignment: dict[str, Any],
    pack: dict[str, Any],
    verification: dict[str, Any],
    report: dict[str, Any],
) -> None:
    assignment["assignment_id"] = la.assignment_id_for(assignment)
    pack["assignment_id"] = assignment["assignment_id"]
    pack["upstream_files"] = copy.deepcopy(assignment["files"])
    report["assignment_id"] = assignment["assignment_id"]
    verification["assignment_id"] = assignment["assignment_id"]
    _refresh_joint_evidence_digests(pack, verification)


def _rewrite_assignment_file_path(
    *,
    assignment: dict[str, Any],
    manifest: dict[str, Any],
    pack: dict[str, Any],
    verification: dict[str, Any],
    report: dict[str, Any],
    new_path: str,
    loader_report: dict[str, Any] | None = None,
) -> None:
    assignment_record = assignment["files"][0]
    old_path = assignment_record["path"]
    assignment_record["path"] = new_path
    for manifest_record in manifest["files"]:
        if manifest_record["path"] == old_path:
            manifest_record["path"] = new_path
    for paths in (
        *manifest["layer_files"].values(),
        *manifest["component_files"].values(),
    ):
        paths[:] = [
            new_path if path == old_path else path
            for path in paths
        ]
    _refresh_manifest_digest(manifest)
    manifest_digest = mm.manifest_digest_ref(manifest)

    assignment["manifest_digest"] = manifest_digest
    assignment["assignment_id"] = la.assignment_id_for(assignment)
    report["assignment_id"] = assignment["assignment_id"]
    report["manifest_digest"] = manifest_digest
    for verified_file in report["verified_files"]:
        if verified_file["path"] == old_path:
            verified_file["path"] = new_path
            verified_file["local_path"] = report[
                "resolved_artifact_cache_root"
            ]

    pack["assignment_id"] = assignment["assignment_id"]
    pack["manifest_digest"] = manifest_digest
    for upstream in pack["upstream_files"]:
        if upstream["path"] == old_path:
            upstream["path"] = new_path
    for artifact in pack["artifacts"]:
        if artifact["upstream_path"] == old_path:
            artifact["upstream_path"] = new_path
            artifact["relative_path"] = new_path

    verification["assignment_id"] = assignment["assignment_id"]
    verification["manifest_digest"] = manifest_digest
    for verified_file in verification["verified_files"]:
        if verified_file["path"] == old_path:
            verified_file["path"] = new_path
            verified_file["relative_path"] = new_path
    verification["tensor_file_map"] = {
        tensor_key: new_path if path == old_path else path
        for tensor_key, path in verification["tensor_file_map"].items()
    }
    _refresh_joint_evidence_digests(pack, verification)

    if loader_report is not None:
        loader_report["assignment_id"] = assignment["assignment_id"]
        loader_report["manifest_digest"] = manifest_digest
        for verified_file in loader_report["verified_files"]:
            if verified_file["path"] == old_path:
                verified_file["path"] = new_path
        loader_report["stage_pack"] = copy.deepcopy(pack)
        loader_report["stage_pack_manifest"] = copy.deepcopy(manifest)
        loader_report["stage_pack_verification"] = copy.deepcopy(verification)
        loader_report["stage_pack_digest"] = pack["stage_pack_digest"]
        loader_report["stage_pack_verification_digest"] = verification[
            "stage_pack_verification_digest"
        ]


def _mutate_assignment_files(
    assignment: dict[str, Any],
    collision: str,
) -> None:
    files = assignment["files"]
    record = files[0]
    if collision == "list-subclass":
        assignment["files"] = _StrictListCollision(files)
    elif collision == "dict-subclass":
        files[0] = _StrictDictCollision(record)
    elif collision == "path-str-subclass":
        record["path"] = _StrictStrCollision(record["path"])
    elif collision == "size-int-subclass":
        record["size_bytes"] = _StrictIntCollision(record["size_bytes"])
    elif collision == "digest-str-subclass":
        record["content_digest"] = _StrictStrCollision(record["content_digest"])
    elif collision == "bool-size":
        record["size_bytes"] = True
    elif collision == "missing-field":
        record.pop("content_digest")
    elif collision == "extra-field":
        record["source_etag"] = "not-in-assignment-schema"
    elif collision == "duplicate-path":
        files.append(copy.deepcopy(record))
    elif collision == "empty-path":
        record["path"] = ""
    elif collision == "noncanonical-path":
        record["path"] = "nested/../escape.safetensors"
    elif collision == "malformed-digest":
        record["content_digest"] = "sha256:not-a-digest"
    elif collision == "record-scalar":
        files[0] = "not-a-record"
    elif collision == "path-int":
        record["path"] = 7
    elif collision == "size-str":
        record["size_bytes"] = str(record["size_bytes"])
    elif collision == "digest-list":
        record["content_digest"] = [record["content_digest"]]
    elif collision == "size-float":
        record["size_bytes"] = float(record["size_bytes"])
    elif collision == "size-zero":
        record["size_bytes"] = 0
    elif collision == "size-negative":
        record["size_bytes"] = -1
    else:
        assignment["files"] = []


def _exercise_assignment_file_boundary(
    validation_path: str,
    *,
    assignment: dict[str, Any],
    manifest: dict[str, Any],
    report: dict[str, Any],
    pack: dict[str, Any],
    verification: dict[str, Any],
) -> Any:
    if validation_path == "compile":
        return compile_stage_pack(assignment, manifest, report)
    if validation_path == "verify":
        return verify_stage_pack(pack, assignment=assignment, manifest=manifest)
    if validation_path == "evidence":
        return sp.validate_stage_pack_evidence(
            pack,
            verification,
            assignment=assignment,
            manifest=manifest,
        )
    if validation_path == "adapter":
        return artifact_report_for_loader(
            pack,
            verification,
            assignment=assignment,
            manifest=manifest,
        )
    raise AssertionError(f"unknown assignment file validation path: {validation_path}")


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
    verification = verify_stage_pack(pack, assignment=final, manifest=manifest)
    assert verification["ready_for_load"] is True
    assert sp.validate_stage_pack_evidence(
        pack,
        verification,
        assignment=final,
        manifest=manifest,
    ) == (
        pack["stage_pack_digest"],
        verification["stage_pack_verification_digest"],
    )
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


def test_tied_alias_ownership_is_stable_when_manifest_aliases_are_reordered(
    tmp_path: Path,
) -> None:
    aliases = {
        "classifier": "input_embedding",
        "pooler": "input_embedding",
    }
    first = _alias_collection_case(
        tmp_path / "first",
        aliases=aliases,
    )
    second = _alias_collection_case(
        tmp_path / "second",
        aliases=dict(reversed(list(aliases.items()))),
    )

    summaries = [
        sp.verify_stage_pack_collection(
            packs,
            assignments=assignments,
            manifest=manifest,
        )
        for manifest, assignments, packs in (first, second)
    ]

    assert [
        {
            "tensor_key": record["tensor_key"],
            "alias_component": record["alias_component"],
            "target_component": record["target_component"],
        }
        for record in summaries[0]["tied_aliases"]
    ] == [
        {
            "tensor_key": record["tensor_key"],
            "alias_component": record["alias_component"],
            "target_component": record["target_component"],
        }
        for record in summaries[1]["tied_aliases"]
    ]


@pytest.mark.parametrize(
    "reverse_alias_order",
    (False, True),
    ids=("duplicate-target-forward", "duplicate-target-reversed"),
)
def test_tied_alias_ownership_rejects_ambiguous_duplicate_targets(
    tmp_path: Path,
    reverse_alias_order: bool,
) -> None:
    aliases = {
        "classifier": "input_embedding",
        "pooler": "input_embedding",
    }
    if reverse_alias_order:
        aliases = dict(reversed(list(aliases.items())))
    manifest, assignments, packs = _alias_collection_case(
        tmp_path,
        aliases=aliases,
        tied_sources=("classifier", "pooler"),
    )

    with pytest.raises(
        ValueError,
        match=r"^stage pack collection tied alias ownership is ambiguous$",
    ):
        sp.verify_stage_pack_collection(
            packs,
            assignments=assignments,
            manifest=manifest,
        )


@pytest.mark.parametrize(
    "aliases",
    (
        {1: "input_embedding"},
        {"classifier": ["input_embedding"]},
        {"": "input_embedding"},
        {"classifier": ""},
    ),
    ids=(
        "non-string-source",
        "non-string-target",
        "empty-source",
        "empty-target",
    ),
)
def test_collection_rejects_invalid_alias_schema_without_raw_type_errors(
    tmp_path: Path,
    aliases: dict[Any, Any],
) -> None:
    manifest, assignments, packs = _alias_collection_case(
        tmp_path,
        aliases={"classifier": "input_embedding"},
    )
    _replace_collection_aliases(
        manifest,
        assignments,
        packs,
        aliases,
    )

    with pytest.raises(
        ValueError,
        match=r"^manifest component aliases are invalid$",
    ):
        sp.verify_stage_pack_collection(
            packs,
            assignments=assignments,
            manifest=manifest,
        )


@pytest.mark.parametrize(
    "aliases",
    (
        {1: "input_embedding", "classifier": "input_embedding"},
        {"classifier": 1, "pooler": "input_embedding"},
        {_StrictStrCollision("classifier"): "input_embedding"},
        {"classifier": _StrictStrCollision("input_embedding")},
        {"": "input_embedding"},
        {"classifier": ""},
        {"classifier": "pooler", "pooler": "input_embedding"},
        {"classifier": "pooler", "pooler": "classifier"},
        {"classifier": "classifier"},
    ),
    ids=(
        "mixed-non-string-source",
        "mixed-non-string-target",
        "source-str-subclass",
        "target-str-subclass",
        "empty-source",
        "empty-target",
        "alias-chain",
        "alias-cycle",
        "self-alias",
    ),
)
@pytest.mark.parametrize(
    "validation_path",
    ("compile", "verify", "collection"),
)
def test_alias_schema_is_validated_before_digest_or_ordering_operations(
    tmp_path: Path,
    aliases: dict[Any, Any],
    validation_path: str,
) -> None:
    manifest, assignments, reports, _ = _case(tmp_path)
    packs = [
        compile_stage_pack(assignment, manifest, report)
        for assignment, report in zip(assignments, reports, strict=True)
    ]
    manifest["component_aliases"] = copy.deepcopy(aliases)

    with pytest.raises(ValueError) as raised:
        if validation_path == "compile":
            compile_stage_pack(assignments[0], manifest, reports[0])
        elif validation_path == "verify":
            verify_stage_pack(
                packs[0],
                assignment=assignments[0],
                manifest=manifest,
            )
        else:
            sp.verify_stage_pack_collection(
                packs,
                assignments=assignments,
                manifest=manifest,
            )

    assert str(raised.value) == "manifest component aliases are invalid"


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


@pytest.mark.parametrize(
    "collision",
    (
        "list-subclass",
        "dict-subclass",
        "path-str-subclass",
        "size-int-subclass",
        "digest-str-subclass",
        "bool-size",
        "missing-field",
        "extra-field",
        "duplicate-path",
        "empty-path",
        "noncanonical-path",
        "malformed-digest",
        "record-scalar",
        "path-int",
        "size-str",
        "digest-list",
        "size-float",
        "size-zero",
        "size-negative",
        "empty-list",
    ),
    ids=lambda collision: collision,
)
@pytest.mark.parametrize("validation_path", ("compile", "verify", "evidence"))
def test_authoritative_assignment_files_require_exact_canonical_schema(
    tmp_path: Path,
    validation_path: str,
    collision: str,
) -> None:
    manifest, assignments, reports, _ = _case(tmp_path)
    assignment = assignments[1]
    report = reports[1]
    pack = compile_stage_pack(assignment, manifest, report)
    verification = verify_stage_pack(
        pack,
        assignment=assignment,
        manifest=manifest,
    )

    _mutate_assignment_files(assignment, collision)
    _rebind_assignment_file_evidence(
        assignment,
        pack,
        verification,
        report,
    )

    with pytest.raises(
        ValueError,
        match=r"^stage pack assignment files are invalid$",
    ):
        _exercise_assignment_file_boundary(
            validation_path,
            assignment=assignment,
            manifest=manifest,
            report=report,
            pack=pack,
            verification=verification,
        )


@pytest.mark.parametrize(
    "field",
    ("path", "size_bytes", "content_digest"),
)
@pytest.mark.parametrize("armed", (False, True), ids=("benign", "armed"))
@pytest.mark.parametrize(
    "validation_path",
    ("compile", "verify", "evidence", "adapter"),
)
def test_authoritative_assignment_file_keys_require_exact_builtin_strings(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    validation_path: str,
    armed: bool,
    field: str,
) -> None:
    manifest, assignments, reports, _ = _case(tmp_path)
    assignment = assignments[1]
    report = reports[1]
    pack = compile_stage_pack(assignment, manifest, report)
    verification = verify_stage_pack(
        pack,
        assignment=assignment,
        manifest=manifest,
    )

    record = assignment["files"][0]
    value = record.pop(field)
    key = _AssignmentFileKey(field)
    record[key] = value
    _rebind_assignment_file_evidence(
        assignment,
        pack,
        verification,
        report,
    )
    key.equality_calls = 0
    key.armed = armed
    file_accesses: list[tuple[Any, ...]] = []

    def reject_file_access(*args: Any, **kwargs: Any) -> Any:
        file_accesses.append((*args, kwargs))
        raise AssertionError("assignment validation reached file access")

    monkeypatch.setattr(sp, "_open_beneath", reject_file_access)

    with pytest.raises(ValueError) as raised:
        _exercise_assignment_file_boundary(
            validation_path,
            assignment=assignment,
            manifest=manifest,
            report=report,
            pack=pack,
            verification=verification,
        )

    assert str(raised.value) == "stage pack assignment files are invalid"
    assert key.equality_calls == 0
    assert file_accesses == []


@pytest.mark.parametrize(
    "validation_path",
    ("compile", "verify", "evidence", "adapter"),
)
def test_assignment_dot_path_is_rejected_before_file_access(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    validation_path: str,
) -> None:
    manifest, assignments, reports, _ = _case(tmp_path)
    assignment = assignments[1]
    report = reports[1]
    pack = compile_stage_pack(assignment, manifest, report)
    verification = verify_stage_pack(
        pack,
        assignment=assignment,
        manifest=manifest,
    )
    _rewrite_assignment_file_path(
        assignment=assignment,
        manifest=manifest,
        pack=pack,
        verification=verification,
        report=report,
        new_path=".",
    )
    file_accesses: list[tuple[Any, ...]] = []

    def reject_file_access(*args: Any, **kwargs: Any) -> Any:
        file_accesses.append((*args, kwargs))
        raise AssertionError("assignment validation reached file access")

    monkeypatch.setattr(sp, "_open_beneath", reject_file_access)

    with pytest.raises(ValueError) as raised:
        _exercise_assignment_file_boundary(
            validation_path,
            assignment=assignment,
            manifest=manifest,
            report=report,
            pack=pack,
            verification=verification,
        )

    assert str(raised.value) == "stage pack assignment files are invalid"
    assert file_accesses == []


def test_direct_loader_rejects_assignment_dot_path_before_file_access(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mycelium_qualification.physical_deployment import (
        prepare_assignment_artifacts,
    )
    from runtime_loader import RuntimeLoadError, load_assignment_stage

    prepared = prepare_assignment_artifacts(tmp_path)
    assignment = copy.deepcopy(prepared.assignments[0])
    manifest = copy.deepcopy(prepared.manifest)
    pack = copy.deepcopy(prepared.stage_packs[0])
    verification = copy.deepcopy(prepared.stage_pack_verifications[0])
    loader_report = copy.deepcopy(prepared.reports[0])
    _rewrite_assignment_file_path(
        assignment=assignment,
        manifest=manifest,
        pack=pack,
        verification=verification,
        report=loader_report,
        new_path=".",
        loader_report=loader_report,
    )
    file_accesses: list[tuple[Any, ...]] = []

    def reject_file_access(*args: Any, **kwargs: Any) -> Any:
        file_accesses.append((*args, kwargs))
        raise AssertionError("assignment validation reached file access")

    monkeypatch.setattr(sp, "_open_beneath", reject_file_access)

    with pytest.raises(RuntimeLoadError) as raised:
        load_assignment_stage(
            assignment,
            loader_report,
            load_generation=17,
        )

    assert str(raised.value) == (
        "stage-pack evidence rejected: "
        "stage pack assignment files are invalid"
    )
    assert file_accesses == []


@pytest.mark.parametrize(
    "key_location",
    (
        "outer-protocol",
        "file-path",
        "file-size-bytes",
        "file-content-digest",
    ),
    ids=lambda key_location: key_location,
)
@pytest.mark.parametrize("armed", (False, True), ids=("benign", "armed"))
def test_direct_loader_canonicalizes_assignment_before_other_entry_work(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    armed: bool,
    key_location: str,
) -> None:
    from mycelium_qualification.physical_deployment import (
        prepare_assignment_artifacts,
    )
    import runtime_loader as rl

    prepared = prepare_assignment_artifacts(tmp_path)
    assignment = copy.deepcopy(prepared.assignments[0])
    report = copy.deepcopy(prepared.reports[0])
    pack = report["stage_pack"]
    verification = report["stage_pack_verification"]

    if key_location == "outer-protocol":
        key = _replace_with_exact_schema_key(assignment, "protocol")
    else:
        field = key_location.removeprefix("file-").replace("-", "_")
        key = _replace_with_exact_schema_key(assignment["files"][0], field)
    _rebind_assignment_file_evidence(
        assignment,
        pack,
        verification,
        report,
    )
    report["stage_pack_digest"] = pack["stage_pack_digest"]
    report["stage_pack_verification_digest"] = verification[
        "stage_pack_verification_digest"
    ]
    key.equality_calls = 0
    key.armed = armed

    entry_calls = {
        "identity": 0,
        "report": 0,
        "file": 0,
    }
    original_identity = rl.validate_assignment_identity
    original_report_validation = rl.artifact_report_errors
    original_file_open = rl._open_verified_artifact

    def record_identity(*args: Any, **kwargs: Any) -> Any:
        entry_calls["identity"] += 1
        return original_identity(*args, **kwargs)

    def record_report(*args: Any, **kwargs: Any) -> Any:
        entry_calls["report"] += 1
        return original_report_validation(*args, **kwargs)

    def record_file(*args: Any, **kwargs: Any) -> Any:
        entry_calls["file"] += 1
        return original_file_open(*args, **kwargs)

    monkeypatch.setattr(rl, "validate_assignment_identity", record_identity)
    monkeypatch.setattr(rl, "artifact_report_errors", record_report)
    monkeypatch.setattr(rl, "_open_verified_artifact", record_file)

    with pytest.raises(rl.RuntimeLoadError) as raised:
        rl.load_assignment_stage(
            assignment,
            report,
            load_generation=17,
        )

    assert str(raised.value) == (
        "stage-pack evidence rejected: "
        "stage pack assignment files are invalid"
    )
    assert key.equality_calls == 0
    assert entry_calls == {
        "identity": 0,
        "report": 0,
        "file": 0,
    }


def test_assignment_canonicalizer_returns_plain_epoch_zero_snapshot(
    tmp_path: Path,
) -> None:
    _, assignments, _, _ = _case(tmp_path, deployment_epoch=0)
    assignment = assignments[1]
    assignment["extra_json"] = {
        "list": [1, 1.5, True, None, {"text": "plain"}],
    }

    snapshot = sp.canonicalize_stage_pack_assignment(assignment)

    assert snapshot == assignment
    assert snapshot is not assignment
    assert type(snapshot) is dict
    assert snapshot["deployment_epoch"] == 0
    assert snapshot["files"] is not assignment["files"]
    assert all(
        type(record) is dict
        and all(type(key) is str for key in record)
        for record in snapshot["files"]
    )
    assert type(snapshot["extra_json"]["list"][0]) is int
    assert type(snapshot["extra_json"]["list"][1]) is float
    assert type(snapshot["extra_json"]["list"][2]) is bool
    assignment["range"]["start_layer"] = 99
    assignment["components"].append("mutated")
    assignment["extra_json"]["list"][4]["text"] = "mutated"
    assert snapshot["range"]["start_layer"] != 99
    assert "mutated" not in snapshot["components"]
    assert snapshot["extra_json"]["list"][4]["text"] == "plain"


@pytest.mark.parametrize(
    "field",
    (
        "components",
        "runtime",
        "component_aliases",
        "control_plane_binding",
        "claim_boundary",
        "extra",
    ),
)
def test_assignment_canonicalizer_rejects_deepcopy_callbacks_value_free(
    tmp_path: Path,
    field: str,
) -> None:
    _, assignments, _, _ = _case(tmp_path)
    assignment = assignments[1]
    calls: list[str] = []
    assignment[field] = _DeepcopyBomb(calls)

    with pytest.raises(ValueError) as raised:
        sp.canonicalize_stage_pack_assignment(assignment)

    assert str(raised.value) == "stage pack assignment files are invalid"
    assert "PRIVATE-" not in str(raised.value)
    assert calls == []


@pytest.mark.parametrize(
    "collision",
    (
        "dict-subclass",
        "list-subclass",
        "str-subclass",
        "int-subclass",
        "float-subclass",
        "bool-like",
        "tuple",
        "set",
        "bytes",
        "custom",
    ),
)
def test_assignment_canonicalizer_rejects_non_exact_json_before_callbacks(
    tmp_path: Path,
    collision: str,
) -> None:
    _, assignments, _, _ = _case(tmp_path)
    assignment = assignments[1]
    calls: list[str] = []
    if collision == "dict-subclass":
        assignment["range"] = _ArmedDict(assignment["range"], calls)
    elif collision == "list-subclass":
        assignment["components"] = _ArmedList(assignment["components"], calls)
    elif collision == "str-subclass":
        assignment["components"][0] = _ArmedStr(
            assignment["components"][0],
            calls,
        )
    elif collision == "int-subclass":
        assignment["range"]["start_layer"] = _ArmedInt(1, calls)
    elif collision == "float-subclass":
        assignment["extra"] = {"value": _ArmedFloat(1.5, calls)}
    elif collision == "bool-like":
        assignment["route_ready"] = _BoolLike(calls)
    elif collision == "tuple":
        assignment["extra"] = ("not", "json")
    elif collision == "set":
        assignment["extra"] = {"not-json"}
    elif collision == "bytes":
        assignment["extra"] = b"not-json"
    else:
        assignment["extra"] = _DeepcopyBomb(calls)

    with pytest.raises(ValueError) as raised:
        sp.canonicalize_stage_pack_assignment(assignment)

    assert str(raised.value) == "stage pack assignment files are invalid"
    assert calls == []


def _nested_assignment_value(depth: int, leaf: Any) -> Any:
    value = leaf
    for _ in range(depth):
        value = [value]
    return value


def test_assignment_canonicalizer_accepts_maximum_bounded_depth() -> None:
    maximum_depth = 64
    assignment = {
        "files": [
            {
                "path": "model.safetensors",
                "size_bytes": 1,
                "content_digest": "sha256:" + "a" * 64,
            }
        ],
        "extra": _nested_assignment_value(maximum_depth - 1, "leaf"),
    }

    snapshot = sp.canonicalize_stage_pack_assignment(assignment)

    assert snapshot == assignment
    assert snapshot is not assignment


@pytest.mark.parametrize("depth", (64, 2000), ids=("over-limit", "python-recursion"))
def test_assignment_canonicalizer_rejects_excess_nesting_without_callbacks(
    depth: int,
) -> None:
    calls: list[str] = []
    assignment = {
        "files": [
            {
                "path": "model.safetensors",
                "size_bytes": 1,
                "content_digest": "sha256:" + "a" * 64,
            }
        ],
        "extra": _nested_assignment_value(depth, _DeepcopyBomb(calls)),
    }

    with pytest.raises(ValueError) as raised:
        sp.canonicalize_stage_pack_assignment(assignment)

    assert str(raised.value) == "stage pack assignment files are invalid"
    assert calls == []


def test_assignment_canonicalizer_rejects_cycles_value_free() -> None:
    cycle: list[Any] = []
    cycle.append(cycle)
    assignment = {
        "files": [
            {
                "path": "model.safetensors",
                "size_bytes": 1,
                "content_digest": "sha256:" + "a" * 64,
            }
        ],
        "extra": cycle,
    }

    with pytest.raises(ValueError) as raised:
        sp.canonicalize_stage_pack_assignment(assignment)

    assert str(raised.value) == "stage pack assignment files are invalid"


@pytest.mark.parametrize("value", (float("nan"), float("inf"), float("-inf")))
def test_assignment_canonicalizer_rejects_nonfinite_exact_floats(
    value: float,
) -> None:
    assignment = {
        "files": [
            {
                "path": "model.safetensors",
                "size_bytes": 1,
                "content_digest": "sha256:" + "a" * 64,
            }
        ],
        "extra": value,
    }

    with pytest.raises(ValueError) as raised:
        sp.canonicalize_stage_pack_assignment(assignment)

    assert str(raised.value) == "stage pack assignment files are invalid"


@pytest.mark.parametrize("validation_path", ("compile", "verify", "evidence"))
def test_authoritative_assignment_file_schema_valid_epoch_zero_control(
    tmp_path: Path,
    validation_path: str,
) -> None:
    manifest, assignments, reports, _ = _case(tmp_path, deployment_epoch=0)
    assignment = assignments[1]
    report = reports[1]
    pack = compile_stage_pack(assignment, manifest, report)
    verification = verify_stage_pack(
        pack,
        assignment=assignment,
        manifest=manifest,
    )

    result = _exercise_assignment_file_boundary(
        validation_path,
        assignment=assignment,
        manifest=manifest,
        report=report,
        pack=pack,
        verification=verification,
    )

    assert assignment["files"]
    assert all(record["size_bytes"] > 0 for record in assignment["files"])
    assert result


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
    replacement_tensor_keys = [
        "bert.encoder.layer.2.attention.self.query.weight"
    ]
    _write_safetensors(artifact, replacement_tensor_keys)
    artifact_record["size_bytes"] = artifact.stat().st_size
    artifact_record["content_digest"] = "sha256:" + _sha(artifact)
    artifact_record["tensor_keys"] = replacement_tensor_keys
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
    from mycelium_qualification.physical_deployment import (
        prepare_assignment_artifacts,
    )

    prepared = prepare_assignment_artifacts(tmp_path)
    manifest = prepared.manifest
    assignments = prepared.assignments
    pack = prepared.stage_packs[1]
    verification = prepared.stage_pack_verifications[1]
    report = prepared.reports[1]

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


@pytest.mark.parametrize(
    "collision",
    (
        "protocol-str-subclass",
        "verification-digest-str-subclass",
        "deployment-epoch-float",
        "range-start-float",
        "range-map-subclass",
        "components-list-subclass",
        "runtime-map-subclass",
        "runtime-nested-int-float",
        "runtime-nested-bool-int",
        "verified-files-list-subclass",
        "verified-file-map-subclass",
        "verified-file-relative-path-str-subclass",
        "verified-file-digest-str-subclass",
        "verified-file-size-float",
        "verified-file-tensor-keys-list-subclass",
        "verified-file-tensor-key-str-subclass",
        "verified-file-tensor-count-float",
        "verified-tensor-keys-list-subclass",
        "verified-tensor-key-str-subclass",
        "tensor-file-map-subclass",
        "tensor-file-map-value-str-subclass",
        "verified-tensor-count-float",
        "expected-bytes-float",
        "ready-for-load-int",
        "route-ready-int",
        "claim-boundary-int",
    ),
    ids=lambda collision: collision,
)
def test_redigested_verification_rejects_exact_type_collisions(
    tmp_path: Path,
    collision: str,
) -> None:
    manifest, assignments, reports, _ = _case(tmp_path)
    assignment = assignments[1]
    pack = compile_stage_pack(assignment, manifest, reports[1])
    if collision.startswith("runtime-nested-"):
        _rebind_assignment_pack(
            assignment,
            pack,
            runtime=_mlx_runtime(),
        )
    verification = verify_stage_pack(
        pack,
        assignment=assignment,
        manifest=manifest,
    )

    if collision == "protocol-str-subclass":
        verification["protocol"] = _StrictStrCollision(verification["protocol"])
    elif collision == "verification-digest-str-subclass":
        verification["stage_pack_verification_digest"] = _StrictStrCollision(
            verification["stage_pack_verification_digest"]
        )
    elif collision == "deployment-epoch-float":
        verification["deployment_epoch"] = float(verification["deployment_epoch"])
    elif collision == "range-start-float":
        verification["range"]["start_layer"] = float(
            verification["range"]["start_layer"]
        )
    elif collision == "range-map-subclass":
        verification["range"] = _StrictDictCollision(verification["range"])
    elif collision == "components-list-subclass":
        verification["components"] = _StrictListCollision(
            verification["components"]
        )
    elif collision == "runtime-map-subclass":
        verification["runtime"] = _StrictDictCollision(verification["runtime"])
    elif collision == "runtime-nested-int-float":
        model_config = verification["runtime"]["model_config"]
        model_config["n_layer"] = float(model_config["n_layer"])
    elif collision == "runtime-nested-bool-int":
        verification["runtime"]["model_config"]["scale_attn_weights"] = 1
    elif collision == "verified-files-list-subclass":
        verification["verified_files"] = _StrictListCollision(
            verification["verified_files"]
        )
    elif collision == "verified-file-map-subclass":
        verification["verified_files"][0] = _StrictDictCollision(
            verification["verified_files"][0]
        )
    elif collision == "verified-file-relative-path-str-subclass":
        record = verification["verified_files"][0]
        record["relative_path"] = _StrictStrCollision(record["relative_path"])
    elif collision == "verified-file-digest-str-subclass":
        record = verification["verified_files"][0]
        record["content_digest"] = _StrictStrCollision(record["content_digest"])
    elif collision == "verified-file-size-float":
        record = verification["verified_files"][0]
        record["size_bytes"] = float(record["size_bytes"])
    elif collision == "verified-file-tensor-keys-list-subclass":
        record = verification["verified_files"][0]
        record["tensor_keys"] = _StrictListCollision(record["tensor_keys"])
    elif collision == "verified-file-tensor-key-str-subclass":
        record = verification["verified_files"][0]
        record["tensor_keys"][0] = _StrictStrCollision(record["tensor_keys"][0])
    elif collision == "verified-file-tensor-count-float":
        record = verification["verified_files"][0]
        record["tensor_count"] = float(record["tensor_count"])
    elif collision == "verified-tensor-keys-list-subclass":
        verification["verified_tensor_keys"] = _StrictListCollision(
            verification["verified_tensor_keys"]
        )
    elif collision == "verified-tensor-key-str-subclass":
        verification["verified_tensor_keys"][0] = _StrictStrCollision(
            verification["verified_tensor_keys"][0]
        )
    elif collision == "tensor-file-map-subclass":
        verification["tensor_file_map"] = _StrictDictCollision(
            verification["tensor_file_map"]
        )
    elif collision == "tensor-file-map-value-str-subclass":
        tensor_key = next(iter(verification["tensor_file_map"]))
        verification["tensor_file_map"][tensor_key] = _StrictStrCollision(
            verification["tensor_file_map"][tensor_key]
        )
    elif collision == "verified-tensor-count-float":
        verification["verified_tensor_count"] = float(
            verification["verified_tensor_count"]
        )
    elif collision == "expected-bytes-float":
        verification["expected_bytes"] = float(verification["expected_bytes"])
    elif collision == "ready-for-load-int":
        verification["ready_for_load"] = 1
    elif collision == "route-ready-int":
        verification["route_ready"] = 0
    else:
        verification["claim_boundary"] = 1
    if collision != "verification-digest-str-subclass":
        verification["stage_pack_verification_digest"] = (
            sp._verification_digest_for(verification)
        )

    with pytest.raises(
        ValueError,
        match=r"^stage pack verification schema is invalid$",
    ):
        sp.validate_stage_pack_evidence(
            pack,
            verification,
            assignment=assignment,
            manifest=manifest,
        )


@pytest.mark.parametrize(
    ("document", "key_location", "validation_path"),
    (
        ("pack", "top", "verify"),
        ("pack", "range", "evidence"),
        ("pack", "runtime", "adapter"),
        ("pack", "component-tensor-keys", "verify"),
        ("pack", "artifact-record", "evidence"),
        ("pack", "runtime-model-config", "adapter"),
        ("verification", "top", "evidence"),
        ("verification", "top", "adapter"),
        ("verification", "range", "evidence"),
        ("verification", "runtime", "adapter"),
        ("verification", "verified-file-record", "evidence"),
        ("verification", "tensor-file-map", "adapter"),
        ("verification", "runtime-model-config", "evidence"),
    ),
    ids=lambda value: value,
)
@pytest.mark.parametrize("armed", (False, True), ids=("benign", "armed"))
def test_recursive_schema_keys_are_exact_before_comparison_or_file_access(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    armed: bool,
    document: str,
    key_location: str,
    validation_path: str,
) -> None:
    manifest, assignments, reports, _ = _case(tmp_path)
    assignment = assignments[1]
    pack = compile_stage_pack(assignment, manifest, reports[1])
    if key_location == "runtime-model-config":
        _rebind_assignment_pack(
            assignment,
            pack,
            runtime=_mlx_runtime(),
        )
    verification = verify_stage_pack(
        pack,
        assignment=assignment,
        manifest=manifest,
    )

    target = pack if document == "pack" else verification
    if key_location == "top":
        key = _replace_with_exact_schema_key(target, "range")
    elif key_location == "range":
        key = _replace_with_exact_schema_key(target["range"], "start_layer")
    elif key_location == "runtime":
        key = _replace_with_exact_schema_key(target["runtime"], "backend")
    elif key_location == "runtime-model-config":
        key = _replace_with_exact_schema_key(
            target["runtime"]["model_config"],
            "n_layer",
        )
    elif key_location == "component-tensor-keys":
        component = next(iter(target["component_tensor_keys"]))
        key = _replace_with_exact_schema_key(
            target["component_tensor_keys"],
            component,
        )
    elif key_location == "artifact-record":
        key = _replace_with_exact_schema_key(
            target["artifacts"][0],
            "upstream_path",
        )
    elif key_location == "verified-file-record":
        key = _replace_with_exact_schema_key(
            target["verified_files"][0],
            "path",
        )
    else:
        tensor_key = next(iter(target["tensor_file_map"]))
        key = _replace_with_exact_schema_key(
            target["tensor_file_map"],
            tensor_key,
        )

    _refresh_joint_evidence_digests(pack, verification)
    key.equality_calls = 0
    key.armed = armed
    file_accesses: list[tuple[Any, ...]] = []

    def reject_file_access(*args: Any, **kwargs: Any) -> Any:
        file_accesses.append((*args, kwargs))
        raise AssertionError("recursive schema validation reached file access")

    monkeypatch.setattr(sp, "_open_beneath", reject_file_access)

    with pytest.raises(ValueError) as raised:
        _exercise_assignment_file_boundary(
            validation_path,
            assignment=assignment,
            manifest=manifest,
            report=reports[1],
            pack=pack,
            verification=verification,
        )

    expected = (
        "stage pack schema is invalid"
        if document == "pack"
        else "stage pack verification schema is invalid"
    )
    assert str(raised.value) == expected
    assert key.equality_calls == 0
    assert file_accesses == []


_ARMED_PACK_SCALAR_LEAVES = (
    "protocol",
    "digest",
    "epoch",
    "route-ready",
    "range",
    "list-item",
    "map-of-value",
    "upstream-file",
    "artifact",
    "runtime",
    "model-config",
    "control-binding",
    "claim-boundary",
)


def _arm_pack_scalar_leaf(
    pack: dict[str, Any],
    leaf: str,
    calls: list[str],
) -> None:
    if leaf == "protocol":
        pack["protocol"] = _ArmedStr(pack["protocol"], calls)
    elif leaf == "digest":
        pack["stage_pack_digest"] = _ArmedStr(
            pack["stage_pack_digest"],
            calls,
        )
    elif leaf == "epoch":
        pack["deployment_epoch"] = _ArmedInt(pack["deployment_epoch"], calls)
    elif leaf == "route-ready":
        pack["route_ready"] = _ArmedInt(0, calls)
    elif leaf == "range":
        pack["range"]["start_layer"] = _ArmedInt(
            pack["range"]["start_layer"],
            calls,
        )
    elif leaf == "list-item":
        pack["components"][0] = _ArmedStr(pack["components"][0], calls)
    elif leaf == "map-of-value":
        pack["component_aliases"]["armed-source"] = _ArmedStr(
            "armed-target",
            calls,
        )
    elif leaf == "upstream-file":
        pack["upstream_files"][0]["path"] = _ArmedStr(
            pack["upstream_files"][0]["path"],
            calls,
        )
    elif leaf == "artifact":
        pack["artifacts"][0]["size_bytes"] = _ArmedInt(
            pack["artifacts"][0]["size_bytes"],
            calls,
        )
    elif leaf == "runtime":
        pack["runtime"]["backend"] = _ArmedStr(
            pack["runtime"]["backend"],
            calls,
        )
    elif leaf == "model-config":
        pack["runtime"]["model_config"]["n_layer"] = _ArmedInt(
            pack["runtime"]["model_config"]["n_layer"],
            calls,
        )
    elif leaf == "control-binding":
        pack["control_plane_binding"]["protocol"] = _ArmedStr(
            pack["control_plane_binding"]["protocol"],
            calls,
        )
    else:
        pack["claim_boundary"] = _ArmedStr(pack["claim_boundary"], calls)


@pytest.mark.parametrize("leaf", _ARMED_PACK_SCALAR_LEAVES)
@pytest.mark.parametrize("validation_path", ("verify", "evidence", "adapter"))
def test_pack_any_schema_rejects_armed_scalars_before_entry_effects(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    validation_path: str,
    leaf: str,
) -> None:
    manifest, assignments, reports, _ = _case(tmp_path)
    assignment = assignments[1]
    pack = compile_stage_pack(assignment, manifest, reports[1])
    if leaf in {"model-config", "control-binding"}:
        _rebind_assignment_pack(
            assignment,
            pack,
            runtime=_mlx_runtime() if leaf == "model-config" else _UNCHANGED,
            control_plane_binding=(
                _control_plane_binding()
                if leaf == "control-binding"
                else _UNCHANGED
            ),
        )
    verification = verify_stage_pack(
        pack,
        assignment=assignment,
        manifest=manifest,
    )
    calls: list[str] = []
    _arm_pack_scalar_leaf(pack, leaf, calls)

    entry_calls = {
        "assignment": 0,
        "file": 0,
        "physical_verifier": 0,
    }

    def reject_assignment(*args: Any, **kwargs: Any) -> Any:
        entry_calls["assignment"] += 1
        raise AssertionError("pack schema validation reached assignment semantics")

    def reject_file(*args: Any, **kwargs: Any) -> Any:
        entry_calls["file"] += 1
        raise AssertionError("pack schema validation reached file access")

    def reject_physical_verifier(*args: Any, **kwargs: Any) -> Any:
        entry_calls["physical_verifier"] += 1
        raise AssertionError("pack schema validation reached physical verifier")

    monkeypatch.setattr(sp, "_validate_authoritative_assignment", reject_assignment)
    monkeypatch.setattr(sp, "_open_beneath", reject_file)
    if validation_path != "verify":
        monkeypatch.setattr(sp, "verify_stage_pack", reject_physical_verifier)

    with pytest.raises(ValueError) as raised:
        _exercise_assignment_file_boundary(
            validation_path,
            assignment=assignment,
            manifest=manifest,
            report=reports[1],
            pack=pack,
            verification=verification,
        )

    assert str(raised.value) == "stage pack schema is invalid"
    assert "PRIVATE-" not in str(raised.value)
    assert calls == []
    assert entry_calls == {
        "assignment": 0,
        "file": 0,
        "physical_verifier": 0,
    }


@pytest.mark.parametrize("value", (float("nan"), float("inf"), float("-inf")))
def test_pack_any_schema_rejects_nonfinite_exact_floats(
    tmp_path: Path,
    value: float,
) -> None:
    manifest, assignments, reports, _ = _case(tmp_path)
    assignment = assignments[1]
    pack = compile_stage_pack(assignment, manifest, reports[1])
    _rebind_assignment_pack(assignment, pack, runtime=_mlx_runtime())
    pack["runtime"]["model_config"]["layer_norm_epsilon"] = value

    with pytest.raises(ValueError) as raised:
        verify_stage_pack(pack, assignment=assignment, manifest=manifest)

    assert str(raised.value) == "stage pack schema is invalid"


def test_pack_any_schema_accepts_finite_float_and_bool_is_not_int(
    tmp_path: Path,
) -> None:
    manifest, assignments, reports, _ = _case(tmp_path)
    assignment = assignments[1]
    pack = compile_stage_pack(assignment, manifest, reports[1])
    _rebind_assignment_pack(assignment, pack, runtime=_mlx_runtime())

    verification = verify_stage_pack(
        pack,
        assignment=assignment,
        manifest=manifest,
    )

    assert math.isfinite(pack["runtime"]["model_config"]["layer_norm_epsilon"])
    assert verification["deployment_epoch"] == 7
    assert sp._matches_exact_schema(True, sp._ANY_SCHEMA)
    assert not sp._matches_exact_schema(True, int)


@pytest.mark.parametrize(
    "cross_link",
    (
        "claim-boundary",
        "file-tensor-count",
        "tensor-file-ownership",
    ),
    ids=lambda cross_link: cross_link,
)
def test_redigested_verification_rejects_semantic_cross_link_drift(
    tmp_path: Path,
    cross_link: str,
) -> None:
    manifest, assignments, reports, _ = _case(tmp_path)
    index = 2 if cross_link == "tensor-file-ownership" else 1
    assignment = assignments[index]
    pack = compile_stage_pack(assignment, manifest, reports[index])
    verification = verify_stage_pack(
        pack,
        assignment=assignment,
        manifest=manifest,
    )

    if cross_link == "claim-boundary":
        verification["claim_boundary"] = "authenticated but overclaimed"
    elif cross_link == "file-tensor-count":
        verification["verified_files"][0]["tensor_count"] += 1
    else:
        classifier_key = manifest["component_tensor_keys"]["classifier"][0]
        verification["tensor_file_map"][classifier_key] = "b.safetensors"
    verification["stage_pack_verification_digest"] = sp._verification_digest_for(
        verification
    )

    with pytest.raises(ValueError, match=r"^stage pack verification evidence is invalid$"):
        sp.validate_stage_pack_evidence(
            pack,
            verification,
            assignment=assignment,
            manifest=manifest,
        )


def test_redigested_verification_rejects_exact_tensor_file_ownership_swap(
    tmp_path: Path,
) -> None:
    manifest, assignments, reports, _ = _case(tmp_path)
    assignment = assignments[1]
    pack = compile_stage_pack(assignment, manifest, reports[1])
    verification = verify_stage_pack(
        pack,
        assignment=assignment,
        manifest=manifest,
    )
    owners = verification["tensor_file_map"]
    left = next(key for key, path in owners.items() if path == "a.safetensors")
    right = next(key for key, path in owners.items() if path == "b.safetensors")
    owners[left], owners[right] = owners[right], owners[left]
    verification["stage_pack_verification_digest"] = sp._verification_digest_for(
        verification
    )

    with pytest.raises(
        ValueError,
        match=r"^stage pack verification evidence is invalid$",
    ):
        sp.validate_stage_pack_evidence(
            pack,
            verification,
            assignment=assignment,
            manifest=manifest,
        )


def test_redigested_verification_rejects_balanced_file_count_redistribution(
    tmp_path: Path,
) -> None:
    manifest, assignments, reports, _ = _case(tmp_path)
    assignment = assignments[1]
    pack = compile_stage_pack(assignment, manifest, reports[1])
    verification = verify_stage_pack(
        pack,
        assignment=assignment,
        manifest=manifest,
    )
    left, right = verification["verified_files"]
    left["tensor_count"] -= 1
    right["tensor_count"] += 1
    verification["stage_pack_verification_digest"] = sp._verification_digest_for(
        verification
    )

    with pytest.raises(
        ValueError,
        match=r"^stage pack verification evidence is invalid$",
    ):
        sp.validate_stage_pack_evidence(
            pack,
            verification,
            assignment=assignment,
            manifest=manifest,
        )


def test_redigested_verification_rejects_file_count_and_overfetch_inflation(
    tmp_path: Path,
) -> None:
    manifest, assignments, reports, _ = _case(tmp_path)
    assignment = assignments[1]
    pack = compile_stage_pack(assignment, manifest, reports[1])
    verification = verify_stage_pack(
        pack,
        assignment=assignment,
        manifest=manifest,
    )
    verification["verified_files"][0]["tensor_count"] += 1
    verification["overfetched_tensor_count"] += 1
    verification["stage_pack_verification_digest"] = sp._verification_digest_for(
        verification
    )

    with pytest.raises(
        ValueError,
        match=r"^stage pack verification evidence is invalid$",
    ):
        sp.validate_stage_pack_evidence(
            pack,
            verification,
            assignment=assignment,
            manifest=manifest,
        )


def test_redigested_verification_rejects_verified_file_tensor_key_drift(
    tmp_path: Path,
) -> None:
    manifest, assignments, reports, _ = _case(tmp_path)
    assignment = assignments[1]
    pack = compile_stage_pack(assignment, manifest, reports[1])
    verification = verify_stage_pack(
        pack,
        assignment=assignment,
        manifest=manifest,
    )
    left, right = verification["verified_files"]
    left["tensor_keys"][0], right["tensor_keys"][0] = (
        right["tensor_keys"][0],
        left["tensor_keys"][0],
    )
    left["tensor_keys"].sort()
    right["tensor_keys"].sort()
    verification["stage_pack_verification_digest"] = sp._verification_digest_for(
        verification
    )

    with pytest.raises(
        ValueError,
        match=r"^stage pack verification file evidence is invalid$",
    ):
        sp.validate_stage_pack_evidence(
            pack,
            verification,
            assignment=assignment,
            manifest=manifest,
        )


def test_joint_redigest_rejects_cross_component_tensor_ownership_swap(
    tmp_path: Path,
) -> None:
    manifest, assignments, reports, _ = _case(tmp_path)
    assignment = assignments[2]
    pack = compile_stage_pack(assignment, manifest, reports[2])
    verification = verify_stage_pack(
        pack,
        assignment=assignment,
        manifest=manifest,
    )
    layer_key = manifest["tensor_keys_by_layer"]["2"][0]
    classifier_key = manifest["component_tensor_keys"]["classifier"][0]
    _swap_embedded_tensor_ownership(
        pack,
        verification,
        layer_key,
        classifier_key,
    )

    with pytest.raises(
        ValueError,
        match=r"^stage pack artifact tensor ownership mismatch$",
    ):
        verify_stage_pack(pack, assignment=assignment, manifest=manifest)
    with pytest.raises(
        ValueError,
        match=r"^stage pack verification evidence is invalid$",
    ):
        sp.validate_stage_pack_evidence(
            pack,
            verification,
            assignment=assignment,
            manifest=manifest,
        )


def test_joint_redigest_physically_rejects_same_layer_multifile_ownership_swap(
    tmp_path: Path,
) -> None:
    manifest, assignments, reports, _ = _case(tmp_path)
    assignment = assignments[1]
    pack = compile_stage_pack(assignment, manifest, reports[1])
    verification = verify_stage_pack(
        pack,
        assignment=assignment,
        manifest=manifest,
    )
    left_key, right_key = manifest["tensor_keys_by_layer"]["1"]
    original_paths = {
        verification["tensor_file_map"][left_key],
        verification["tensor_file_map"][right_key],
    }
    assert original_paths == set(manifest["layer_files"]["1"])
    _swap_embedded_tensor_ownership(
        pack,
        verification,
        left_key,
        right_key,
    )
    assert {
        verification["tensor_file_map"][left_key],
        verification["tensor_file_map"][right_key],
    } <= set(manifest["layer_files"]["1"])

    with pytest.raises(
        ValueError,
        match=r"^stage pack artifact tensor ownership mismatch$",
    ):
        verify_stage_pack(pack, assignment=assignment, manifest=manifest)
    with pytest.raises(
        ValueError,
        match=r"^stage pack physical verification evidence is invalid$",
    ):
        sp.validate_stage_pack_evidence(
            pack,
            verification,
            assignment=assignment,
            manifest=manifest,
        )


def test_joint_redigest_cannot_replace_authoritative_file_digest(
    tmp_path: Path,
) -> None:
    manifest, assignments, reports, _ = _case(tmp_path)
    assignment = assignments[1]
    assignment_snapshot = copy.deepcopy(assignment)
    pack = compile_stage_pack(assignment, manifest, reports[1])
    verification = verify_stage_pack(
        pack,
        assignment=assignment,
        manifest=manifest,
    )
    artifact = pack["artifacts"][0]
    verified_file = next(
        record
        for record in verification["verified_files"]
        if record["path"] == artifact["upstream_path"]
    )
    artifact["content_digest"] = "sha256:" + "f" * 64
    verified_file["content_digest"] = artifact["content_digest"]
    _refresh_joint_evidence_digests(pack, verification)

    assert assignment == assignment_snapshot
    with pytest.raises(
        ValueError,
        match=r"^stage pack verification file evidence is invalid$",
    ):
        sp.validate_stage_pack_evidence(
            pack,
            verification,
            assignment=assignment,
            manifest=manifest,
        )


@pytest.mark.parametrize(
    "layout_attack",
    ("nested-relative-path", "post-verification-symlink"),
    ids=lambda layout_attack: layout_attack,
)
def test_adapter_never_emits_ready_report_rejected_by_loader_path_contract(
    tmp_path: Path,
    layout_attack: str,
) -> None:
    from mycelium_qualification.physical_deployment import (
        prepare_assignment_artifacts,
    )

    prepared = prepare_assignment_artifacts(tmp_path)
    assignment = prepared.assignments[0]
    pack = prepared.stage_packs[0]
    root = Path(pack["artifact_root"])
    artifact_record = pack["artifacts"][0]
    local_path = root / artifact_record["relative_path"]
    if layout_attack == "nested-relative-path":
        nested_path = root / "snapshot" / artifact_record["upstream_path"]
        nested_path.parent.mkdir(parents=True)
        local_path.replace(nested_path)
        artifact_record["relative_path"] = (
            f"snapshot/{artifact_record['upstream_path']}"
        )
        _refresh_digest(pack)
        verification = verify_stage_pack(
            pack,
            assignment=assignment,
            manifest=prepared.manifest,
        )
    else:
        verification = prepared.stage_pack_verifications[0]
        backing = root / "authenticated-backing.safetensors"
        shutil.copyfile(local_path, backing)
        local_path.unlink()
        local_path.symlink_to(backing.name)

    expected_error = (
        "stage pack artifacts are not loader-compatible"
        if layout_attack == "nested-relative-path"
        else "stage pack physical verification evidence is invalid"
    )
    with pytest.raises(ValueError) as raised:
        artifact_report_for_loader(
            pack,
            verification,
            assignment=assignment,
            manifest=prepared.manifest,
        )
    assert str(raised.value) == expected_error


def test_flat_local_adapter_report_loads_through_runtime_contract(
    tmp_path: Path,
) -> None:
    from mycelium_qualification.physical_deployment import (
        prepare_assignment_artifacts,
    )
    from runtime_loader import load_assignment_stage

    prepared = prepare_assignment_artifacts(tmp_path)
    loaded = [
        load_assignment_stage(assignment, report, load_generation=17)
        for assignment, report in zip(
            prepared.assignments,
            prepared.reports,
            strict=True,
        )
    ]

    assert all(stage.proof["route_ready"] is False for stage in loaded)
    assert [
        stage.proof["stage_pack_digest"] for stage in loaded
    ] == [
        pack["stage_pack_digest"] for pack in prepared.stage_packs
    ]


def test_direct_loader_rejects_joint_embedded_file_metadata_rewrite(
    tmp_path: Path,
) -> None:
    from mycelium_qualification.physical_deployment import (
        prepare_assignment_artifacts,
    )
    from runtime_loader import RuntimeLoadError, load_assignment_stage

    prepared = prepare_assignment_artifacts(tmp_path)
    assignment = prepared.assignments[0]
    report = copy.deepcopy(prepared.reports[0])
    outer_report_snapshot = {
        "verified_files": copy.deepcopy(report["verified_files"]),
        "expected_bytes": report["expected_bytes"],
        "cache_hit_bytes": report["cache_hit_bytes"],
    }
    pack = report["stage_pack"]
    verification = report["stage_pack_verification"]
    artifact = pack["artifacts"][0]
    verified_file = verification["verified_files"][0]
    artifact["size_bytes"] += 1
    verified_file["size_bytes"] += 1
    verification["expected_bytes"] += 1
    _refresh_joint_evidence_digests(pack, verification)
    report["stage_pack_digest"] = pack["stage_pack_digest"]
    report["stage_pack_verification_digest"] = verification[
        "stage_pack_verification_digest"
    ]
    forged_digest = pack["stage_pack_digest"]

    assert {
        "verified_files": report["verified_files"],
        "expected_bytes": report["expected_bytes"],
        "cache_hit_bytes": report["cache_hit_bytes"],
    } == outer_report_snapshot
    with pytest.raises(
        RuntimeLoadError,
        match=(
            r"^stage-pack evidence rejected: "
            r"stage pack verification file evidence is invalid$"
        ),
    ) as raised:
        load_assignment_stage(
            assignment,
            report,
            load_generation=17,
        )
    assert forged_digest not in str(raised.value)


def test_coherent_nested_upstream_path_loads_through_runtime_contract(
    tmp_path: Path,
) -> None:
    from mycelium_qualification.physical_deployment import (
        prepare_assignment_artifacts,
    )
    from runtime_loader import load_assignment_stage

    prepared = prepare_assignment_artifacts(tmp_path)
    manifest = copy.deepcopy(prepared.manifest)
    assignment = copy.deepcopy(prepared.assignments[0])
    upstream_report = copy.deepcopy(prepared.reports[0])
    report_record = upstream_report["verified_files"][0]
    actual_local_path = Path(report_record["local_path"])
    old_upstream_path = report_record["path"]
    nested_upstream_path = f"nested/snapshot/{old_upstream_path}"
    nested_local_path = actual_local_path.parent / nested_upstream_path
    nested_local_path.parent.mkdir(parents=True)
    actual_local_path.replace(nested_local_path)

    for record in manifest["files"]:
        if record["path"] == old_upstream_path:
            record["path"] = nested_upstream_path
    for files in (
        *manifest["layer_files"].values(),
        *manifest["component_files"].values(),
    ):
        files[:] = [
            nested_upstream_path if path == old_upstream_path else path
            for path in files
        ]
    _refresh_manifest_digest(manifest)
    manifest_digest = mm.manifest_digest_ref(manifest)

    assignment["manifest_digest"] = manifest_digest
    assignment["files"][0]["path"] = nested_upstream_path
    assignment["assignment_id"] = la.assignment_id_for(assignment)

    upstream_report["assignment_id"] = assignment["assignment_id"]
    upstream_report["manifest_digest"] = manifest_digest
    report_record["path"] = nested_upstream_path
    report_record["local_path"] = str(nested_local_path)
    for field in (
        "stage_pack",
        "stage_pack_manifest",
        "stage_pack_verification",
        "stage_pack_digest",
        "stage_pack_verification_digest",
    ):
        upstream_report.pop(field)

    pack = compile_stage_pack(assignment, manifest, upstream_report)
    verification = verify_stage_pack(
        pack,
        assignment=assignment,
        manifest=manifest,
    )
    loader_report = artifact_report_for_loader(
        pack,
        verification,
        assignment=assignment,
        manifest=manifest,
    )
    loaded = load_assignment_stage(
        assignment,
        loader_report,
        load_generation=23,
    )

    assert assignment["files"][0]["path"] == nested_upstream_path
    assert pack["artifacts"][0]["upstream_path"] == nested_upstream_path
    assert pack["artifacts"][0]["relative_path"] == nested_upstream_path
    assert verification["verified_files"][0]["path"] == nested_upstream_path
    assert loader_report["verified_files"][0] == {
        "path": nested_upstream_path,
        "local_path": str(nested_local_path),
        "size_bytes": report_record["size_bytes"],
        "content_digest": report_record["content_digest"],
        "cache_hit": True,
        "tensor_count": report_record["tensor_count"],
    }
    assert loaded.proof["stage_pack_digest"] == pack["stage_pack_digest"]
    assert loaded.proof["route_ready"] is False


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


@pytest.mark.parametrize(
    "attack",
    (
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
    ),
    ids=(
        "not-object",
        "missing-field",
        "extra-field",
        "wrong-protocol",
        "malformed-evidence-digest",
        "malformed-snapshot-digest",
        "negative-generation",
        "bool-generation",
        "float-generation",
        "empty-swarm",
        "wrong-deployment",
        "negative-epoch",
        "wrong-epoch",
        "bool-epoch",
        "float-epoch",
        "explicit-none",
        "type-collision-generation",
        "type-collision-epoch",
    ),
)
def test_collection_verifier_rejects_invalid_control_plane_bindings(
    tmp_path: Path,
    attack: str,
) -> None:
    manifest, assignments, reports, _ = _case(tmp_path)
    packs = [
        compile_stage_pack(assignment, manifest, report)
        for assignment, report in zip(assignments, reports, strict=True)
    ]
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
