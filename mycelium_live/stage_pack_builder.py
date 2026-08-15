"""Build immutable assignment-local acquisition manifests from verified stage packs."""

from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
from typing import Any, Mapping
import uuid

from mycelium_swarm_artifacts import (
    MANIFEST_PROTOCOL,
    canonical_digest,
    merkle_proofs,
    merkle_root,
    validate_stage_pack_manifest,
)


PACK_FORMAT = "mycelium.stage_pack_stream.v1"
_COMPONENT_NAMES = {
    "input_embedding": "embedding",
    "decoder": "transformer_layers",
    "final_norm": "final_norm",
    "lm_head": "lm_head",
}


class StagePackBuildError(RuntimeError):
    """A built candidate could not be reduced to an exact acquisition pack."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _canonical(value: object) -> bytes:
    return (
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
        + "\n"
    ).encode()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while block := handle.read(4 * 1024 * 1024):
            digest.update(block)
    return "sha256:" + digest.hexdigest()


def _safe_relative(value: object) -> str:
    if not isinstance(value, str):
        raise StagePackBuildError("stage_pack_artifact_path_invalid")
    path = PurePosixPath(value)
    if (
        not value
        or path.is_absolute()
        or value != path.as_posix()
        or "\\" in value
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise StagePackBuildError("stage_pack_artifact_path_invalid")
    return value


def _placement(graph: Mapping[str, Any], assignment_id: str) -> tuple[str, str]:
    matches: list[tuple[str, str]] = []
    for stage in graph.get("stages", []):
        if not isinstance(stage, Mapping):
            continue
        for placement in stage.get("placements", []):
            if (
                isinstance(placement, Mapping)
                and placement.get("assignment_id") == assignment_id
            ):
                matches.append(
                    (str(stage.get("stage_id")), str(placement.get("placement_id")))
                )
    if len(matches) != 1 or not all(matches[0]):
        raise StagePackBuildError("stage_pack_placement_binding_invalid")
    return matches[0]


def _file_components(pack: Mapping[str, Any], tensor_keys: object) -> list[str]:
    if not isinstance(tensor_keys, list) or not tensor_keys:
        raise StagePackBuildError("stage_pack_tensor_scope_invalid")
    keys = set(tensor_keys)
    if not all(isinstance(item, str) and item for item in keys):
        raise StagePackBuildError("stage_pack_tensor_scope_invalid")
    mapping = pack.get("component_tensor_keys")
    if not isinstance(mapping, Mapping):
        raise StagePackBuildError("stage_pack_tensor_scope_invalid")
    components: set[str] = set()
    covered: set[str] = set()
    for component, raw_keys in mapping.items():
        public_component = _COMPONENT_NAMES.get(str(component))
        if public_component is None or not isinstance(raw_keys, list):
            raise StagePackBuildError("stage_pack_component_scope_invalid")
        overlap = keys.intersection(raw_keys)
        if overlap:
            components.add(public_component)
            covered.update(overlap)
    if covered != keys or not components:
        raise StagePackBuildError("stage_pack_tensor_scope_invalid")
    return sorted(components)


def _write_object(root: Path, digest: str, payload: bytes) -> None:
    destination = root / digest.removeprefix("sha256:")
    if destination.exists():
        if (
            destination.is_symlink()
            or destination.stat().st_size != len(payload)
            or _sha256(destination) != digest
        ):
            raise StagePackBuildError("stage_pack_source_object_conflict")
        return
    temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("xb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o400)
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def build_stage_pack_source(
    *,
    transfer_bundle: Path,
    pack: Mapping[str, Any],
    assignment_offer: Mapping[str, Any],
    graph: Mapping[str, Any],
    authorization: Mapping[str, Any],
    output_root: Path,
    chunk_size_bytes: int,
    issued_at_unix_ms: int,
    expires_at_unix_ms: int | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Create verified chunk objects plus one exact closed acquisition manifest."""

    if (
        type(chunk_size_bytes) is not int
        or not 65_536 <= chunk_size_bytes <= 64 * 1024 * 1024
    ):
        raise StagePackBuildError("stage_pack_chunk_size_invalid")
    bundle = Path(transfer_bundle).resolve(strict=True)
    root = Path(output_root)
    if not root.is_absolute() or root.exists():
        raise StagePackBuildError("stage_pack_source_root_invalid")
    root.mkdir(parents=True, mode=0o700)
    objects = root / "objects"
    objects.mkdir(mode=0o700)
    assignment_id = pack.get("assignment_id")
    node_id = pack.get("node_id")
    if (
        not isinstance(assignment_id, str)
        or assignment_offer.get("assignment_id") != assignment_id
        or not isinstance(node_id, str)
        or assignment_offer.get("recipient_node_id") != node_id
        or assignment_offer.get("stage_pack_digest") != pack.get("stage_pack_digest")
    ):
        raise StagePackBuildError("stage_pack_assignment_binding_invalid")
    stage_id, placement_id = _placement(graph, assignment_id)
    artifacts = pack.get("artifacts")
    if not isinstance(artifacts, list) or not artifacts:
        raise StagePackBuildError("stage_pack_artifacts_invalid")
    ordered = sorted(artifacts, key=lambda item: str(item.get("relative_path", "")))
    files: list[dict[str, Any]] = []
    pack_digest = hashlib.sha256()
    chunk_digests: list[str] = []
    chunk_sizes: list[int] = []
    chunk_payload = bytearray()
    offset = 0
    for artifact in ordered:
        if not isinstance(artifact, Mapping):
            raise StagePackBuildError("stage_pack_artifacts_invalid")
        relative = _safe_relative(artifact.get("relative_path"))
        source = bundle / "deployment" / _safe_relative(artifact.get("upstream_path"))
        try:
            source = source.resolve(strict=True)
            source.relative_to((bundle / "deployment").resolve(strict=True))
        except (OSError, ValueError) as exc:
            raise StagePackBuildError("stage_pack_artifact_path_invalid") from exc
        size = artifact.get("size_bytes")
        digest = artifact.get("content_digest")
        if (
            type(size) is not int
            or size <= 0
            or not isinstance(digest, str)
            or source.stat().st_size != size
            or _sha256(source) != digest
        ):
            raise StagePackBuildError("stage_pack_artifact_integrity_failure")
        files.append(
            {
                "relative_path": "deployment/" + relative,
                "components": _file_components(pack, artifact.get("tensor_keys")),
                "offset_bytes": offset,
                "size_bytes": size,
                "content_digest": digest,
            }
        )
        with source.open("rb") as handle:
            while block := handle.read(4 * 1024 * 1024):
                pack_digest.update(block)
                view = memoryview(block)
                while view:
                    take = min(chunk_size_bytes - len(chunk_payload), len(view))
                    chunk_payload.extend(view[:take])
                    view = view[take:]
                    if len(chunk_payload) == chunk_size_bytes:
                        payload = bytes(chunk_payload)
                        chunk_digest = "sha256:" + hashlib.sha256(payload).hexdigest()
                        _write_object(objects, chunk_digest, payload)
                        chunk_digests.append(chunk_digest)
                        chunk_sizes.append(len(payload))
                        chunk_payload.clear()
        offset += size
    if chunk_payload:
        payload = bytes(chunk_payload)
        chunk_digest = "sha256:" + hashlib.sha256(payload).hexdigest()
        _write_object(objects, chunk_digest, payload)
        chunk_digests.append(chunk_digest)
        chunk_sizes.append(len(payload))
    proofs = merkle_proofs(chunk_digests)
    graph_digest = assignment_offer.get("graph_digest")
    assignment_digest = assignment_offer.get("assignment_digest")
    layer_range = pack.get("range")
    if not isinstance(layer_range, Mapping):
        raise StagePackBuildError("stage_pack_assignment_binding_invalid")
    tensor_scope_digest = canonical_digest(
        {
            "components": pack.get("component_tensor_keys"),
            "expected_tensor_keys": pack.get("expected_tensor_keys"),
            "range": layer_range,
        }
    )
    component_scope = sorted(
        {component for record in files for component in record["components"]}
    )
    expires_at = (
        authorization.get("evidence_valid_until_unix_ms")
        if expires_at_unix_ms is None
        else expires_at_unix_ms
    )
    document: dict[str, Any] = {
        "protocol": MANIFEST_PROTOCOL,
        "manifest_id": "manifest-"
        + hashlib.sha256(
            _canonical(
                {
                    "assignment_id": assignment_id,
                    "assignment_digest": assignment_digest,
                    "evidence_generation": authorization.get("evidence_generation"),
                    "issued_at_unix_ms": issued_at_unix_ms,
                    "representation_digest": authorization.get("representation_digest"),
                    "tensor_scope_digest": tensor_scope_digest,
                }
            )
        ).hexdigest()[:24],
        "manifest_digest": "sha256:" + "0" * 64,
        "model_id": pack.get("model_id"),
        "model_revision": pack.get("resolved_commit"),
        "model_artifact_digest": pack.get("manifest_digest"),
        "source_quantization": authorization.get("source_quantization"),
        "serving_dtype": authorization.get("serving_dtype"),
        "serving_quantization": authorization.get("serving_quantization"),
        "representation_digest": authorization.get("representation_digest"),
        "owner_decision_digest": authorization.get("owner_decision_digest"),
        "feasibility_digest": authorization.get("feasibility_digest"),
        "evidence_generation": authorization.get("evidence_generation"),
        "assignment_id": assignment_id,
        "assignment_digest": assignment_digest,
        "graph_digest": graph_digest,
        "recipient_member_id": node_id,
        "recipient_membership_generation": assignment_offer.get("generation"),
        "placement_id": placement_id,
        "stage_id": stage_id,
        "layer_start": layer_range.get("start_layer"),
        "layer_end_exclusive": layer_range.get("end_layer_exclusive"),
        "component_scope": component_scope,
        "tensor_scope_digest": tensor_scope_digest,
        "pack_format": PACK_FORMAT,
        "files": files,
        "stage_pack_digest": "sha256:" + pack_digest.hexdigest(),
        "chunk_size_bytes": chunk_size_bytes,
        "total_size_bytes": offset,
        "merkle_root": merkle_root(chunk_digests),
        "chunks": [
            {
                "index": index,
                "offset_bytes": index * chunk_size_bytes,
                "size_bytes": size,
                "content_digest": digest,
                "merkle_proof": list(proofs[index]),
            }
            for index, (digest, size) in enumerate(
                zip(chunk_digests, chunk_sizes, strict=True)
            )
        ],
        "issued_at_unix_ms": issued_at_unix_ms,
        "expires_at_unix_ms": expires_at,
        "owner_provenance": "owner-approved-exact-representation",
    }
    document["manifest_digest"] = canonical_digest(
        {key: value for key, value in document.items() if key != "manifest_digest"}
    )
    binding_fields = {
        "model_id",
        "model_revision",
        "representation_digest",
        "owner_decision_digest",
        "feasibility_digest",
        "evidence_generation",
        "assignment_id",
        "assignment_digest",
        "graph_digest",
        "recipient_member_id",
        "recipient_membership_generation",
        "placement_id",
        "stage_id",
        "layer_start",
        "layer_end_exclusive",
        "component_scope",
        "tensor_scope_digest",
    }
    expected_binding = {
        field: copy.deepcopy(document[field]) for field in binding_fields
    }
    return validate_stage_pack_manifest(
        document, expected_binding=expected_binding
    ), expected_binding


__all__ = ["PACK_FORMAT", "StagePackBuildError", "build_stage_pack_source"]
