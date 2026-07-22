#!/usr/bin/env python3
"""Canonical assignment-derived stage packs and local artifact verification.

A stage pack is not a second assignment contract and is not a runtime load proof.
It binds one immutable layer assignment to the exact already-provisioned local
Safetensors files that cover it. Verification proves file integrity and exact
assigned tensor coverage while deliberately keeping ``route_ready`` false.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import struct
from typing import Any, BinaryIO

import model_manifest as mm
from layer_assignment import validate_assignment_identity
from runtime_contracts import MLX_RUNTIME_BASE_FIELDS, validate_normalized_mlx_runtime
from weight_provisioning import artifact_report_errors

STAGE_PACK_PROTOCOL = "mycelium.assignment_stage_pack.v1"
STAGE_PACK_VERIFICATION_PROTOCOL = "mycelium.stage_pack_verification.v1"
_SHA256_REF_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_SAFE_PATH_RE = re.compile(r"^[A-Za-z0-9._/-]+$")
_MAX_HEADER_BYTES = 100 * 1024 * 1024
_DTYPE_BYTES = {
    "BOOL": 1,
    "U8": 1,
    "I8": 1,
    "F8_E4M3": 1,
    "F8_E5M2": 1,
    "I16": 2,
    "U16": 2,
    "F16": 2,
    "BF16": 2,
    "I32": 4,
    "U32": 4,
    "F32": 4,
    "I64": 8,
    "U64": 8,
    "F64": 8,
}
_PACK_FIELDS = frozenset(
    {
        "protocol",
        "assignment_id",
        "deployment_id",
        "deployment_epoch",
        "node_id",
        "model_id",
        "resolved_commit",
        "manifest_digest",
        "range",
        "components",
        "component_tensor_keys",
        "component_aliases",
        "expected_tensor_prefixes",
        "expected_tensor_keys",
        "upstream_files",
        "artifact_root",
        "artifacts",
        "runtime",
        "control_plane_binding",
        "route_ready",
        "claim_boundary",
        "stage_pack_digest",
    }
)
_VERIFICATION_FIELDS = frozenset(
    {
        "protocol",
        "stage_pack_digest",
        "assignment_id",
        "deployment_id",
        "deployment_epoch",
        "node_id",
        "model_id",
        "resolved_commit",
        "manifest_digest",
        "range",
        "components",
        "runtime",
        "artifact_root",
        "verified_files",
        "verified_tensor_prefixes",
        "verified_tensor_keys",
        "tensor_file_map",
        "verified_tensor_count",
        "expected_bytes",
        "overfetched_tensor_count",
        "ready_for_load",
        "route_ready",
        "claim_boundary",
        "stage_pack_verification_digest",
    }
)
_ASSIGNMENT_PACK_FIELDS = (
    "assignment_id",
    "deployment_id",
    "deployment_epoch",
    "node_id",
    "model_id",
    "resolved_commit",
    "manifest_digest",
    "range",
    "components",
    "component_tensor_keys",
    "component_aliases",
    "expected_tensor_prefixes",
    "expected_tensor_keys",
    "runtime",
    "control_plane_binding",
)


def _canonical_json(document: Any) -> str:
    try:
        return json.dumps(
            document,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("canonical JSON must contain finite JSON-compatible values") from exc


def _digest(document: Any) -> str:
    return "sha256:" + hashlib.sha256(_canonical_json(document).encode("utf-8")).hexdigest()


def stage_pack_digest_for(pack: dict[str, Any]) -> str:
    """Return the canonical digest of every stage-pack field except the digest."""
    if not isinstance(pack, dict):
        raise ValueError("stage pack must be an object")
    unsigned = copy.deepcopy(pack)
    unsigned.pop("stage_pack_digest", None)
    return _digest(unsigned)


def _verification_digest_for(verification: dict[str, Any]) -> str:
    unsigned = copy.deepcopy(verification)
    unsigned.pop("stage_pack_verification_digest", None)
    return _digest(unsigned)


def _safe_relative_path(value: Any) -> PurePosixPath:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 1024
        or not _SAFE_PATH_RE.fullmatch(value)
    ):
        raise ValueError(f"unsafe artifact path: {value!r}")
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or len(path.parts) > 32
        or any(part in {"", ".", ".."} for part in path.parts)
        or str(path) != value
    ):
        raise ValueError(f"unsafe artifact path: {value!r}")
    return path


def _normalize_runtime(runtime: Any) -> dict[str, Any]:
    if not isinstance(runtime, dict):
        raise ValueError("stage pack runtime identity must be an object")
    backend = runtime.get("backend")
    if backend == "mlx":
        return validate_normalized_mlx_runtime(runtime)
    if backend == "artifact_verifier":
        if set(runtime) != MLX_RUNTIME_BASE_FIELDS:
            raise ValueError("artifact_verifier runtime fields are invalid")
        if runtime != {
            "backend": "artifact_verifier",
            "dtype": "source",
            "quantization": "none",
        }:
            raise ValueError("unsupported artifact_verifier runtime identity")
        return copy.deepcopy(runtime)
    raise ValueError(f"unsupported stage pack runtime backend: {backend!r}")


def _manifest_file_records(manifest: dict[str, Any]) -> dict[str, dict[str, Any]]:
    raw = manifest.get("files")
    if not isinstance(raw, list) or not raw:
        raise ValueError("manifest files are missing")
    result: dict[str, dict[str, Any]] = {}
    for record in raw:
        if not isinstance(record, dict) or not isinstance(record.get("path"), str):
            raise ValueError("manifest file record is invalid")
        path = record["path"]
        digest = record.get("content_digest")
        if (
            path in result
            or not isinstance(digest, dict)
            or digest.get("algorithm") != "sha256"
            or not re.fullmatch(r"[0-9a-f]{64}", str(digest.get("value", "")))
        ):
            raise ValueError("manifest file record is invalid")
        result[path] = {
            "path": path,
            "size_bytes": record.get("size_bytes"),
            "content_digest": f"sha256:{digest['value']}",
        }
    return result


def _validate_authoritative_assignment(
    assignment: dict[str, Any], manifest: dict[str, Any]
) -> None:
    try:
        validate_assignment_identity(assignment)
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"invalid assignment identity: {exc}") from exc
    if manifest.get("protocol") != "mycelium.model_manifest.v1":
        raise ValueError("unsupported model manifest protocol")
    if manifest.get("format") != "safetensors_sharded":
        raise ValueError("unsupported model manifest format")
    if not mm.verify_manifest_digest(manifest):
        raise ValueError("manifest digest mismatch")
    for field, expected in (
        ("model_id", manifest.get("model_id")),
        ("resolved_commit", manifest.get("resolved_commit")),
        ("manifest_digest", mm.manifest_digest_ref(manifest)),
    ):
        if assignment.get(field) != expected:
            raise ValueError(f"assignment {field} does not match manifest")

    layer_range = assignment.get("range")
    if not isinstance(layer_range, dict) or set(layer_range) != {
        "start_layer",
        "end_layer_exclusive",
        "layer_count",
    }:
        raise ValueError("assignment range is invalid")
    start = layer_range.get("start_layer")
    end = layer_range.get("end_layer_exclusive")
    count = layer_range.get("layer_count")
    total = manifest.get("num_layers")
    if (
        not isinstance(start, int)
        or isinstance(start, bool)
        or not isinstance(end, int)
        or isinstance(end, bool)
        or not isinstance(count, int)
        or isinstance(count, bool)
        or not isinstance(total, int)
        or isinstance(total, bool)
        or start < 0
        or end <= start
        or end > total
        or count != end - start
    ):
        raise ValueError("assignment range is invalid")

    static_keys = manifest.get("component_tensor_keys")
    layer_keys = manifest.get("tensor_keys_by_layer")
    layer_files = manifest.get("layer_files")
    component_files = manifest.get("component_files")
    if not all(
        isinstance(value, dict)
        for value in (static_keys, layer_keys, layer_files, component_files)
    ):
        raise ValueError("manifest ownership maps are invalid")
    for layer in range(start, end):
        keys = layer_keys.get(str(layer))
        files = layer_files.get(str(layer))
        if not isinstance(keys, list) or not keys or not isinstance(files, list) or not files:
            raise ValueError(f"manifest lacks complete coverage for assigned layer {layer}")

    components = ["decoder"]
    if start == 0 and static_keys.get("input_embedding"):
        components.insert(0, "input_embedding")
    if end == total:
        components.extend(
            name
            for name, keys in static_keys.items()
            if name != "input_embedding" and keys
        )
    if assignment.get("components") != components:
        raise ValueError("assignment components do not match manifest ownership")

    decoder_keys = sorted(
        {key for layer in range(start, end) for key in layer_keys[str(layer)]}
    )
    expected_component_keys = {
        component: (
            decoder_keys if component == "decoder" else list(static_keys[component])
        )
        for component in components
    }
    if assignment.get("component_tensor_keys") != expected_component_keys:
        raise ValueError("assignment component tensor keys do not match manifest")
    expected_aliases = {
        source: target
        for source, target in manifest.get("component_aliases", {}).items()
        if source in components
    }
    if assignment.get("component_aliases") != expected_aliases:
        raise ValueError("assignment aliases do not match manifest")
    expected_prefixes = [
        manifest["block_prefix_template"].format(layer=layer)
        for layer in range(start, end)
    ]
    if assignment.get("expected_tensor_prefixes") != expected_prefixes:
        raise ValueError("assignment tensor prefixes do not match manifest")
    expected_keys = sorted(
        {key for keys in expected_component_keys.values() for key in keys}
    )
    if assignment.get("expected_tensor_keys") != expected_keys or not expected_keys:
        raise ValueError("assignment tensor keys do not match manifest")

    paths = {
        path for layer in range(start, end) for path in layer_files[str(layer)]
    }
    for component in components:
        if component != "decoder":
            files = component_files.get(component)
            if not isinstance(files, list):
                raise ValueError(f"manifest component files missing for {component}")
            paths.update(files)
    manifest_files = _manifest_file_records(manifest)
    try:
        expected_files = [manifest_files[path] for path in sorted(paths)]
    except KeyError as exc:
        raise ValueError(f"manifest covering file missing: {exc.args[0]}") from exc
    if assignment.get("files") != expected_files:
        raise ValueError("assignment does not contain the minimal covering files")
    _normalize_runtime(assignment.get("runtime"))


def compile_stage_pack(
    assignment: dict[str, Any],
    manifest: dict[str, Any],
    artifact_report: dict[str, Any],
) -> dict[str, Any]:
    """Compile one canonical local pack from authoritative assignment evidence."""
    if not isinstance(assignment, dict) or not isinstance(manifest, dict):
        raise ValueError("assignment and manifest must be objects")
    _validate_authoritative_assignment(assignment, manifest)
    errors = artifact_report_errors(assignment, artifact_report)
    if errors:
        raise ValueError("artifact report rejected: " + "; ".join(errors))
    root_raw = artifact_report.get("resolved_artifact_cache_root")
    if not isinstance(root_raw, str) or not Path(root_raw).is_absolute():
        raise ValueError("artifact report lacks an absolute resolved root")
    try:
        root = Path(root_raw).resolve(strict=True)
    except OSError as exc:
        raise ValueError("artifact root is unavailable") from exc
    if str(root) != root_raw or not root.is_dir():
        raise ValueError("artifact root must be an exact resolved directory")

    verified = artifact_report.get("verified_files")
    if not isinstance(verified, list):
        raise ValueError("artifact report lacks verified files")
    by_path: dict[str, dict[str, Any]] = {}
    for record in verified:
        if not isinstance(record, dict) or not isinstance(record.get("path"), str):
            raise ValueError("artifact report has invalid verified file")
        if record["path"] in by_path:
            raise ValueError(f"duplicate verified artifact: {record['path']}")
        by_path[record["path"]] = record

    artifacts = []
    for upstream in assignment["files"]:
        path = upstream["path"]
        record = by_path[path]
        local_raw = record.get("local_path")
        if not isinstance(local_raw, str) or not Path(local_raw).is_absolute():
            raise ValueError(f"verified local path is invalid: {path}")
        local = Path(local_raw)
        try:
            relative = local.relative_to(root)
        except ValueError as exc:
            raise ValueError(f"verified local path escapes artifact root: {path}") from exc
        relative_posix = relative.as_posix()
        _safe_relative_path(relative_posix)
        artifacts.append(
            {
                "upstream_path": path,
                "relative_path": relative_posix,
                "size_bytes": upstream["size_bytes"],
                "content_digest": upstream["content_digest"],
            }
        )

    pack = {
        "protocol": STAGE_PACK_PROTOCOL,
        "assignment_id": assignment["assignment_id"],
        "deployment_id": assignment["deployment_id"],
        "deployment_epoch": assignment["deployment_epoch"],
        "node_id": assignment["node_id"],
        "model_id": assignment["model_id"],
        "resolved_commit": assignment["resolved_commit"],
        "manifest_digest": assignment["manifest_digest"],
        "range": copy.deepcopy(assignment["range"]),
        "components": copy.deepcopy(assignment["components"]),
        "component_tensor_keys": copy.deepcopy(assignment["component_tensor_keys"]),
        "component_aliases": copy.deepcopy(assignment["component_aliases"]),
        "expected_tensor_prefixes": copy.deepcopy(
            assignment["expected_tensor_prefixes"]
        ),
        "expected_tensor_keys": copy.deepcopy(assignment["expected_tensor_keys"]),
        "upstream_files": copy.deepcopy(assignment["files"]),
        "artifact_root": str(root),
        "artifacts": artifacts,
        "runtime": copy.deepcopy(assignment["runtime"]),
        "control_plane_binding": copy.deepcopy(
            assignment.get("control_plane_binding")
        ),
        "route_ready": False,
        "claim_boundary": (
            "assignment-derived local artifact pack; files and tensor coverage "
            "not yet reverified, layers not loaded"
        ),
    }
    pack["stage_pack_digest"] = stage_pack_digest_for(pack)
    return pack


def _validate_pack_shape(pack: dict[str, Any]) -> None:
    if set(pack) != _PACK_FIELDS:
        raise ValueError("stage pack fields do not match the v1 contract")
    if pack.get("protocol") != STAGE_PACK_PROTOCOL:
        raise ValueError("unsupported stage pack protocol")
    if pack.get("route_ready") is not False:
        raise ValueError("stage pack cannot claim route readiness")
    supplied = pack.get("stage_pack_digest")
    if not _SHA256_REF_RE.fullmatch(str(supplied or "")):
        raise ValueError("stage pack digest is invalid")
    if supplied != stage_pack_digest_for(pack):
        raise ValueError("stage pack digest mismatch")
    _normalize_runtime(pack.get("runtime"))
    layer_range = pack.get("range")
    if not isinstance(layer_range, dict):
        raise ValueError("stage pack range is invalid")
    start = layer_range.get("start_layer")
    end = layer_range.get("end_layer_exclusive")
    count = layer_range.get("layer_count")
    if (
        not isinstance(start, int)
        or isinstance(start, bool)
        or not isinstance(end, int)
        or isinstance(end, bool)
        or not isinstance(count, int)
        or isinstance(count, bool)
        or start < 0
        or end <= start
        or count != end - start
    ):
        raise ValueError("stage pack range is invalid")
    components = pack.get("components")
    component_keys = pack.get("component_tensor_keys")
    expected_keys = pack.get("expected_tensor_keys")
    prefixes = pack.get("expected_tensor_prefixes")
    if (
        not isinstance(components, list)
        or not components
        or "decoder" not in components
        or len(components) != len(set(components))
        or not all(isinstance(item, str) and item for item in components)
        or not isinstance(component_keys, dict)
        or set(component_keys) != set(components)
        or not isinstance(expected_keys, list)
        or not expected_keys
        or expected_keys != sorted(expected_keys)
        or len(expected_keys) != len(set(expected_keys))
        or not isinstance(prefixes, list)
        or len(prefixes) != count
        or not all(isinstance(prefix, str) and prefix for prefix in prefixes)
    ):
        raise ValueError("stage pack assignment ownership is invalid")
    owned = []
    for component in components:
        keys = component_keys.get(component)
        if (
            not isinstance(keys, list)
            or not keys
            or keys != sorted(keys)
            or len(keys) != len(set(keys))
            or not all(isinstance(key, str) and key for key in keys)
        ):
            raise ValueError("stage pack assignment ownership is invalid")
        owned.extend(keys)
    if sorted(set(owned)) != expected_keys:
        raise ValueError("stage pack assigned tensor keys are inconsistent")


def _validate_against_assignment(pack: dict[str, Any], assignment: dict[str, Any]) -> None:
    try:
        validate_assignment_identity(assignment)
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"invalid assignment identity: {exc}") from exc
    for field in _ASSIGNMENT_PACK_FIELDS:
        if pack.get(field) != assignment.get(field):
            raise ValueError(f"stage pack assignment mismatch: {field}")
    if pack.get("upstream_files") != assignment.get("files"):
        raise ValueError("stage pack assignment mismatch: files")


def _open_beneath(root: Path, relative: PurePosixPath) -> BinaryIO:
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    cloexec = getattr(os, "O_CLOEXEC", 0)
    directory = getattr(os, "O_DIRECTORY", 0)
    root_fd = os.open(root, os.O_RDONLY | directory | nofollow | cloexec)
    current_fd = root_fd
    try:
        for part in relative.parts[:-1]:
            child_fd = os.open(
                part,
                os.O_RDONLY | directory | nofollow | cloexec,
                dir_fd=current_fd,
            )
            if current_fd != root_fd:
                os.close(current_fd)
            current_fd = child_fd
        file_fd = os.open(
            relative.parts[-1],
            os.O_RDONLY | nofollow | cloexec,
            dir_fd=current_fd,
        )
        return os.fdopen(file_fd, "rb")
    except OSError as exc:
        raise ValueError(
            f"unable to open verified artifact (symlink forbidden): {relative}"
        ) from exc
    finally:
        if current_fd != root_fd:
            os.close(current_fd)
        os.close(root_fd)


def _read_safetensors_header(handle: BinaryIO, path: str) -> set[str]:
    metadata = os.fstat(handle.fileno())
    if not stat.S_ISREG(metadata.st_mode):
        raise ValueError(f"verified artifact is not a regular file: {path}")
    if metadata.st_nlink != 1:
        raise ValueError(f"verified artifact must have one hard link: {path}")
    prefix = handle.read(8)
    if len(prefix) != 8:
        raise ValueError(f"invalid Safetensors header length: {path}")
    header_length = struct.unpack("<Q", prefix)[0]
    if header_length <= 0 or header_length > _MAX_HEADER_BYTES:
        raise ValueError(f"invalid Safetensors header length: {path}")
    if 8 + header_length > metadata.st_size:
        raise ValueError(f"truncated Safetensors header: {path}")
    raw = handle.read(header_length)

    def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate Safetensors name: {key}")
            result[key] = value
        return result

    def reject_constant(value: str) -> None:
        raise ValueError(f"non-finite Safetensors value: {value}")

    try:
        header = json.loads(
            raw,
            object_pairs_hook=reject_duplicates,
            parse_constant=reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid Safetensors JSON header: {path}") from exc
    if not isinstance(header, dict):
        raise ValueError(f"invalid Safetensors header: {path}")
    metadata_record = header.pop("__metadata__", None)
    if metadata_record is not None and (
        not isinstance(metadata_record, dict)
        or not all(
            isinstance(key, str) and isinstance(value, str)
            for key, value in metadata_record.items()
        )
    ):
        raise ValueError(f"invalid Safetensors metadata: {path}")
    if not header:
        raise ValueError(f"empty Safetensors tensor set: {path}")

    data_size = metadata.st_size - 8 - header_length
    intervals: list[tuple[int, int, str]] = []
    for name, record in header.items():
        if not isinstance(name, str) or not name or not isinstance(record, dict):
            raise ValueError(f"invalid Safetensors tensor metadata: {path}")
        if set(record) != {"dtype", "shape", "data_offsets"}:
            raise ValueError(f"invalid Safetensors tensor metadata: {name}")
        dtype = record["dtype"]
        shape = record["shape"]
        offsets = record["data_offsets"]
        if dtype not in _DTYPE_BYTES:
            raise ValueError(f"unsupported Safetensors dtype: {dtype}")
        if not isinstance(shape, list) or any(
            not isinstance(dimension, int)
            or isinstance(dimension, bool)
            or dimension < 0
            for dimension in shape
        ):
            raise ValueError(f"invalid Safetensors tensor shape: {name}")
        if (
            not isinstance(offsets, list)
            or len(offsets) != 2
            or any(
                not isinstance(offset, int) or isinstance(offset, bool)
                for offset in offsets
            )
        ):
            raise ValueError(f"invalid Safetensors data offsets: {name}")
        start, end = offsets
        element_count = 1
        for dimension in shape:
            element_count *= dimension
        if (
            start < 0
            or end < start
            or end > data_size
            or end - start != element_count * _DTYPE_BYTES[dtype]
        ):
            raise ValueError(f"invalid Safetensors tensor byte length: {name}")
        intervals.append((start, end, name))
    cursor = 0
    for start, end, name in sorted(intervals):
        if start < cursor:
            raise ValueError(f"overlapping Safetensors tensor data: {name}")
        if start > cursor:
            raise ValueError(f"unindexed Safetensors tensor data before: {name}")
        cursor = end
    if cursor != data_size:
        raise ValueError(f"unindexed trailing Safetensors data: {path}")
    handle.seek(0)
    return set(header)


def _hash_handle(handle: BinaryIO) -> str:
    handle.seek(0)
    digest = hashlib.sha256()
    while True:
        chunk = handle.read(1024 * 1024)
        if not chunk:
            break
        digest.update(chunk)
    handle.seek(0)
    return "sha256:" + digest.hexdigest()


def _file_fingerprint(metadata: os.stat_result) -> tuple[int, int, int, int, int, int]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
        metadata.st_nlink,
    )


def verify_stage_pack(
    pack: dict[str, Any], *, assignment: dict[str, Any]
) -> dict[str, Any]:
    """Reverify local files and tensor coverage against one immutable assignment."""
    if not isinstance(pack, dict):
        raise ValueError("stage pack must be an object")
    if not isinstance(assignment, dict):
        raise ValueError("assignment must be an object")
    _validate_pack_shape(pack)
    _validate_against_assignment(pack, assignment)

    root_raw = pack.get("artifact_root")
    if not isinstance(root_raw, str) or not Path(root_raw).is_absolute():
        raise ValueError("stage pack artifact root must be absolute")
    root = Path(root_raw)
    if root.is_symlink():
        raise ValueError("stage pack artifact root may not be a symlink")
    try:
        resolved_root = root.resolve(strict=True)
    except OSError as exc:
        raise ValueError("stage pack artifact root is unavailable") from exc
    if str(resolved_root) != root_raw or not resolved_root.is_dir():
        raise ValueError("stage pack artifact root must be exact and canonical")

    upstream_raw = pack.get("upstream_files")
    artifacts_raw = pack.get("artifacts")
    if not isinstance(upstream_raw, list) or not upstream_raw:
        raise ValueError("stage pack upstream files are invalid")
    if not isinstance(artifacts_raw, list) or not artifacts_raw:
        raise ValueError("stage pack artifacts are invalid")
    upstream: dict[str, dict[str, Any]] = {}
    for record in upstream_raw:
        if not isinstance(record, dict) or set(record) != {
            "path",
            "size_bytes",
            "content_digest",
        }:
            raise ValueError("stage pack upstream file record is invalid")
        path = record.get("path")
        if not isinstance(path, str) or path in upstream:
            raise ValueError(f"duplicate upstream artifact: {path}")
        upstream[path] = record

    seen_artifacts: set[str] = set()
    all_tensor_files: dict[str, str] = {}
    verified_files = []
    seen_inodes: set[tuple[int, int]] = set()
    total_header_names = 0
    for artifact in artifacts_raw:
        if not isinstance(artifact, dict) or set(artifact) != {
            "upstream_path",
            "relative_path",
            "size_bytes",
            "content_digest",
        }:
            raise ValueError("stage pack artifact record is invalid")
        upstream_path = artifact.get("upstream_path")
        if not isinstance(upstream_path, str) or upstream_path in seen_artifacts:
            raise ValueError(f"duplicate artifact: {upstream_path}")
        seen_artifacts.add(upstream_path)
        expected = upstream.get(upstream_path)
        if expected is None or artifact.get("size_bytes") != expected.get(
            "size_bytes"
        ) or artifact.get("content_digest") != expected.get("content_digest"):
            raise ValueError(f"stage pack artifact does not match upstream file: {upstream_path}")
        size = artifact.get("size_bytes")
        digest = artifact.get("content_digest")
        if (
            not isinstance(size, int)
            or isinstance(size, bool)
            or size <= 0
            or not _SHA256_REF_RE.fullmatch(str(digest or ""))
        ):
            raise ValueError(f"stage pack artifact metadata is invalid: {upstream_path}")
        relative = _safe_relative_path(artifact.get("relative_path"))
        handle = _open_beneath(resolved_root, relative)
        try:
            metadata = os.fstat(handle.fileno())
            before = _file_fingerprint(metadata)
            inode = (metadata.st_dev, metadata.st_ino)
            if inode in seen_inodes:
                raise ValueError(f"duplicate artifact inode: {upstream_path}")
            seen_inodes.add(inode)
            if metadata.st_size != size:
                raise ValueError(f"stage pack artifact size mismatch: {upstream_path}")
            actual_digest = _hash_handle(handle)
            if actual_digest != digest:
                raise ValueError(f"stage pack artifact digest mismatch: {upstream_path}")
            names = _read_safetensors_header(handle, upstream_path)
            if _file_fingerprint(os.fstat(handle.fileno())) != before:
                raise ValueError(
                    f"stage pack artifact changed during verification: {upstream_path}"
                )
        finally:
            handle.close()
        total_header_names += len(names)
        for name in names:
            if name in all_tensor_files:
                raise ValueError(f"duplicate tensor across artifacts: {name}")
            all_tensor_files[name] = upstream_path
        verified_files.append(
            {
                "path": upstream_path,
                "relative_path": str(relative),
                "size_bytes": size,
                "content_digest": digest,
                "tensor_count": len(names),
            }
        )
    if seen_artifacts != set(upstream):
        raise ValueError("stage pack artifacts do not match upstream allowlist")

    expected_keys = pack["expected_tensor_keys"]
    missing = sorted(set(expected_keys) - set(all_tensor_files))
    if missing:
        raise ValueError("missing assigned tensors: " + ", ".join(missing[:5]))
    mapping = {key: all_tensor_files[key] for key in expected_keys}
    missing_prefixes = [
        prefix
        for prefix in pack["expected_tensor_prefixes"]
        if not any(key.startswith(prefix) for key in expected_keys)
    ]
    if missing_prefixes:
        raise ValueError(
            "assigned layers lack tensor coverage: " + ", ".join(missing_prefixes)
        )

    verification = {
        "protocol": STAGE_PACK_VERIFICATION_PROTOCOL,
        "stage_pack_digest": pack["stage_pack_digest"],
        "assignment_id": pack["assignment_id"],
        "deployment_id": pack["deployment_id"],
        "deployment_epoch": pack["deployment_epoch"],
        "node_id": pack["node_id"],
        "model_id": pack["model_id"],
        "resolved_commit": pack["resolved_commit"],
        "manifest_digest": pack["manifest_digest"],
        "range": copy.deepcopy(pack["range"]),
        "components": copy.deepcopy(pack["components"]),
        "runtime": copy.deepcopy(pack["runtime"]),
        "artifact_root": root_raw,
        "verified_files": verified_files,
        "verified_tensor_prefixes": copy.deepcopy(pack["expected_tensor_prefixes"]),
        "verified_tensor_keys": copy.deepcopy(expected_keys),
        "tensor_file_map": mapping,
        "verified_tensor_count": len(expected_keys),
        "expected_bytes": sum(record["size_bytes"] for record in upstream.values()),
        "overfetched_tensor_count": total_header_names - len(expected_keys),
        "ready_for_load": True,
        "route_ready": False,
        "claim_boundary": (
            "assignment-bound stage-pack files, digests, strict Safetensors headers, "
            "and exact tensor coverage verified; layers not loaded or probed"
        ),
    }
    verification["stage_pack_verification_digest"] = _verification_digest_for(
        verification
    )
    return verification


def _validate_verification_evidence(
    pack: dict[str, Any],
    verification: dict[str, Any],
    assignment: dict[str, Any],
) -> None:
    _validate_pack_shape(pack)
    _validate_against_assignment(pack, assignment)
    if not isinstance(verification, dict) or set(verification) != _VERIFICATION_FIELDS:
        raise ValueError("stage pack verification fields do not match the v1 contract")
    supplied = verification.get("stage_pack_verification_digest")
    if (
        not _SHA256_REF_RE.fullmatch(str(supplied or ""))
        or supplied != _verification_digest_for(verification)
    ):
        raise ValueError("stage pack verification digest mismatch")
    expected_bindings = {
        "protocol": STAGE_PACK_VERIFICATION_PROTOCOL,
        "stage_pack_digest": pack["stage_pack_digest"],
        "assignment_id": pack["assignment_id"],
        "deployment_id": pack["deployment_id"],
        "deployment_epoch": pack["deployment_epoch"],
        "node_id": pack["node_id"],
        "model_id": pack["model_id"],
        "resolved_commit": pack["resolved_commit"],
        "manifest_digest": pack["manifest_digest"],
        "range": pack["range"],
        "components": pack["components"],
        "runtime": pack["runtime"],
        "artifact_root": pack["artifact_root"],
        "verified_tensor_prefixes": pack["expected_tensor_prefixes"],
        "verified_tensor_keys": pack["expected_tensor_keys"],
        "verified_tensor_count": len(pack["expected_tensor_keys"]),
        "expected_bytes": sum(item["size_bytes"] for item in pack["artifacts"]),
        "ready_for_load": True,
        "route_ready": False,
    }
    for field, expected in expected_bindings.items():
        if verification.get(field) != expected:
            raise ValueError(f"stage pack verification assignment mismatch: {field}")
    verified_files = verification.get("verified_files")
    expected_files = {
        item["upstream_path"]: {
            "relative_path": item["relative_path"],
            "size_bytes": item["size_bytes"],
            "content_digest": item["content_digest"],
        }
        for item in pack["artifacts"]
    }
    expected_paths = set(expected_files)
    if (
        not isinstance(verified_files, list)
        or len(verified_files) != len(expected_paths)
        or any(
            not isinstance(record, dict)
            or set(record)
            != {
                "path",
                "relative_path",
                "size_bytes",
                "content_digest",
                "tensor_count",
            }
            or record.get("path") not in expected_paths
            or {
                field: record.get(field)
                for field in ("relative_path", "size_bytes", "content_digest")
            }
            != expected_files.get(record.get("path"))
            or not isinstance(record.get("tensor_count"), int)
            or isinstance(record.get("tensor_count"), bool)
            or record["tensor_count"] <= 0
            for record in verified_files
        )
        or {record["path"] for record in verified_files} != expected_paths
    ):
        raise ValueError("stage pack verification file evidence is invalid")
    tensor_map = verification.get("tensor_file_map")
    overfetch = verification.get("overfetched_tensor_count")
    if (
        not isinstance(tensor_map, dict)
        or set(tensor_map) != set(pack["expected_tensor_keys"])
        or any(path not in expected_paths for path in tensor_map.values())
        or not isinstance(overfetch, int)
        or isinstance(overfetch, bool)
        or overfetch < 0
    ):
        raise ValueError("stage pack verification tensor evidence is invalid")


def artifact_report_for_loader(
    pack: dict[str, Any],
    verification: dict[str, Any],
    *,
    assignment: dict[str, Any],
) -> dict[str, Any]:
    """Adapt verified pack evidence to the existing offline runtime-loader input."""
    _validate_verification_evidence(pack, verification, assignment)
    root = Path(pack["artifact_root"])
    by_path = {record["path"]: record for record in verification["verified_files"]}
    verified_files = []
    for artifact in pack["artifacts"]:
        record = by_path[artifact["upstream_path"]]
        verified_files.append(
            {
                "path": artifact["upstream_path"],
                "local_path": str(root / artifact["relative_path"]),
                "size_bytes": artifact["size_bytes"],
                "content_digest": artifact["content_digest"],
                "cache_hit": True,
                "tensor_count": record["tensor_count"],
            }
        )
    expected_bytes = verification["expected_bytes"]
    report = {
        "protocol": "mycelium.artifact_verification_report.v1",
        "deployment_id": assignment["deployment_id"],
        "deployment_epoch": assignment["deployment_epoch"],
        "assignment_id": assignment["assignment_id"],
        "node_id": assignment["node_id"],
        "manifest_digest": assignment["manifest_digest"],
        "resolved_commit": assignment["resolved_commit"],
        "range": copy.deepcopy(assignment["range"]),
        "artifact_cache_root": assignment["artifact_cache_root"],
        "resolved_artifact_cache_root": pack["artifact_root"],
        "verified_files": verified_files,
        "verified_tensor_prefixes": copy.deepcopy(
            assignment["expected_tensor_prefixes"]
        ),
        "verified_tensor_count": len(set(assignment["expected_tensor_keys"])),
        "expected_bytes": expected_bytes,
        "network_download_bytes": 0,
        "cache_hit_bytes": expected_bytes,
        "ready_for_load": True,
        "route_ready": False,
        "stage_pack_digest": pack["stage_pack_digest"],
        "stage_pack_verification_digest": verification[
            "stage_pack_verification_digest"
        ],
        "claim_boundary": (
            "stage pack locally reverified and adapted for offline loading; "
            "layers not loaded or probed"
        ),
    }
    errors = artifact_report_errors(assignment, report)
    if errors:
        raise ValueError("loader artifact report rejected: " + "; ".join(errors))
    return report
