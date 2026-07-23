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
import math
import os
from pathlib import Path, PurePosixPath
import re
import stat
import struct
from typing import Any, BinaryIO, Sequence

import model_manifest as mm
from layer_assignment import validate_assignment_identity
from planner_assignment import CONTROL_PLANE_BINDING_PROTOCOL
from runtime_contracts import MLX_RUNTIME_BASE_FIELDS, validate_normalized_mlx_runtime
from weight_provisioning import artifact_report_errors

STAGE_PACK_PROTOCOL = "mycelium.assignment_stage_pack.v1"
STAGE_PACK_VERIFICATION_PROTOCOL = "mycelium.stage_pack_verification.v1"
FP16_TOLERANCE_PROTOCOL = "mycelium.fp16_tolerance.v1"
_SHA256_REF_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
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
_CONTROL_PLANE_BINDING_FIELDS = frozenset(
    {
        "protocol",
        "evidence_bundle_digest",
        "planner_snapshot_digest",
        "snapshot_generation",
        "swarm_id",
        "deployment_id",
        "deployment_epoch",
    }
)
_TOLERANCE_FIELDS = frozenset(
    {
        "protocol",
        "model_id",
        "resolved_commit",
        "model_artifact",
        "runtime",
        "checks",
        "freeze",
        "route_ready",
        "claim_boundary",
        "tolerance_digest",
    }
)
_TOLERANCE_CHECKS = frozenset({"activations", "logits", "token_ids"})
_MAX_TOLERANCE_BYTES = 64 * 1024


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


def _deployment_epoch_is_valid(value: Any) -> bool:
    return type(value) is int and value >= 0


def _validate_deployment_epoch(value: Any) -> None:
    if not _deployment_epoch_is_valid(value):
        raise ValueError("stage pack deployment epoch is invalid")


def _canonical_optional_control_plane_binding(
    assignment: dict[str, Any],
) -> bytes | None:
    if "control_plane_binding" not in assignment:
        return None
    binding = assignment["control_plane_binding"]
    if (
        not isinstance(binding, dict)
        or set(binding) != _CONTROL_PLANE_BINDING_FIELDS
        or type(binding.get("protocol")) is not str
        or binding["protocol"] != CONTROL_PLANE_BINDING_PROTOCOL
    ):
        raise ValueError("stage pack control-plane binding is invalid")
    for field in ("evidence_bundle_digest", "planner_snapshot_digest"):
        value = binding.get(field)
        if type(value) is not str or _SHA256_REF_RE.fullmatch(value) is None:
            raise ValueError("stage pack control-plane binding is invalid")
    snapshot_generation = binding.get("snapshot_generation")
    if type(snapshot_generation) is not int or snapshot_generation < 0:
        raise ValueError("stage pack control-plane binding is invalid")
    if not _deployment_epoch_is_valid(binding.get("deployment_epoch")):
        raise ValueError("stage pack control-plane binding is invalid")
    for field in ("swarm_id", "deployment_id"):
        value = binding.get(field)
        if type(value) is not str or not value:
            raise ValueError("stage pack control-plane binding is invalid")
    if (
        binding["deployment_id"] != assignment.get("deployment_id")
        or binding["deployment_epoch"] != assignment.get("deployment_epoch")
    ):
        raise ValueError("stage pack control-plane binding is invalid")
    return _canonical_json(binding).encode("utf-8")


def stage_pack_digest_for(pack: dict[str, Any]) -> str:
    """Return the canonical digest of every stage-pack field except the digest."""
    if not isinstance(pack, dict):
        raise ValueError("stage pack must be an object")
    unsigned = copy.deepcopy(pack)
    unsigned.pop("stage_pack_digest", None)
    return _digest(unsigned)


def tolerance_digest_for(policy: dict[str, Any]) -> str:
    """Return the canonical digest of a frozen tolerance policy."""
    if not isinstance(policy, dict):
        raise ValueError("FP16 tolerance policy must be an object")
    unsigned = copy.deepcopy(policy)
    unsigned.pop("tolerance_digest", None)
    return _digest(unsigned)


def _json_without_duplicate_keys(raw: bytes) -> Any:
    def object_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("FP16 tolerance policy has duplicate fields")
            result[key] = value
        return result

    def reject_constant(value: str) -> None:
        raise ValueError(f"FP16 tolerance policy contains non-finite value: {value}")

    try:
        return json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=object_pairs,
            parse_constant=reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("FP16 tolerance policy is invalid JSON") from exc


def _read_tolerance_file(path: Path) -> bytes:
    flags = os.O_RDONLY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise ValueError("FP16 tolerance policy cannot be opened safely") from exc
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or before.st_size <= 0
            or before.st_size > _MAX_TOLERANCE_BYTES
        ):
            raise ValueError("FP16 tolerance policy file is invalid")
        chunks: list[bytes] = []
        remaining = before.st_size
        while remaining:
            chunk = os.read(descriptor, min(remaining, 16 * 1024))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    if (
        len(raw) != before.st_size
        or (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
        != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
    ):
        raise ValueError("FP16 tolerance policy changed during read")
    return raw


def load_fp16_tolerances(
    path: str | Path,
    *,
    expected_model_id: str,
    expected_resolved_commit: str,
    expected_model_artifact_digest: str,
) -> dict[str, Any]:
    """Load one immutable source-bound FP16 parity policy, fail closed."""
    raw = _read_tolerance_file(Path(path))
    policy = _json_without_duplicate_keys(raw)
    if not isinstance(policy, dict) or set(policy) != _TOLERANCE_FIELDS:
        raise ValueError("FP16 tolerance policy fields are invalid")
    if _canonical_json(policy).encode("utf-8") != raw:
        raise ValueError("FP16 tolerance policy must use canonical JSON")
    if policy.get("protocol") != FP16_TOLERANCE_PROTOCOL:
        raise ValueError("FP16 tolerance policy protocol is invalid")
    if policy.get("route_ready") is not False:
        raise ValueError("FP16 tolerance policy cannot claim route readiness")
    if not isinstance(policy.get("claim_boundary"), str) or not policy["claim_boundary"]:
        raise ValueError("FP16 tolerance policy claim boundary is invalid")
    supplied_digest = policy.get("tolerance_digest")
    if (
        not isinstance(supplied_digest, str)
        or not _SHA256_REF_RE.fullmatch(supplied_digest)
        or supplied_digest != tolerance_digest_for(policy)
    ):
        raise ValueError("FP16 tolerance policy digest mismatch")

    artifact = policy.get("model_artifact")
    if (
        not isinstance(artifact, dict)
        or set(artifact) != {"path", "size_bytes", "content_digest"}
        or artifact.get("path") != "model.safetensors"
        or not isinstance(artifact.get("size_bytes"), int)
        or isinstance(artifact.get("size_bytes"), bool)
        or artifact["size_bytes"] <= 0
        or not isinstance(artifact.get("content_digest"), str)
        or not _SHA256_REF_RE.fullmatch(artifact["content_digest"])
    ):
        raise ValueError("FP16 tolerance policy model artifact is invalid")
    if (
        policy.get("model_id") != expected_model_id
        or policy.get("resolved_commit") != expected_resolved_commit
        or artifact["content_digest"] != expected_model_artifact_digest
        or not isinstance(expected_model_id, str)
        or not expected_model_id
        or not isinstance(expected_resolved_commit, str)
        or not _COMMIT_RE.fullmatch(expected_resolved_commit)
        or not isinstance(expected_model_artifact_digest, str)
        or not _SHA256_REF_RE.fullmatch(expected_model_artifact_digest)
    ):
        raise ValueError("FP16 tolerance policy source identity mismatch")
    if policy.get("runtime") != {
        "backend": "mlx",
        "dtype": "float16",
        "quantization": "none",
    }:
        raise ValueError("FP16 tolerance policy runtime is invalid")
    freeze = policy.get("freeze")
    if freeze != {
        "basis": "precommitted_policy_before_matrix",
        "measurement_status": "unmeasured",
        "post_hoc_fitted": False,
    }:
        raise ValueError("FP16 tolerance policy freeze metadata is invalid")
    checks = policy.get("checks")
    if not isinstance(checks, dict) or set(checks) != _TOLERANCE_CHECKS:
        raise ValueError("FP16 tolerance checks are invalid")
    if checks.get("token_ids") != {"exact": True}:
        raise ValueError("FP16 token parity must be exact")
    for name in ("activations", "logits"):
        check = checks.get(name)
        if not isinstance(check, dict) or set(check) != {
            "absolute_tolerance",
            "relative_tolerance",
        }:
            raise ValueError(f"FP16 {name} tolerance is invalid")
        for value in check.values():
            if (
                not isinstance(value, (int, float))
                or isinstance(value, bool)
                or not math.isfinite(float(value))
                or value <= 0
                or value > 1
            ):
                raise ValueError(f"FP16 {name} tolerance is invalid")
    return copy.deepcopy(policy)


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
    _validate_deployment_epoch(assignment.get("deployment_epoch"))
    _canonical_optional_control_plane_binding(assignment)
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
    aliases = manifest.get("component_aliases")
    if not all(
        isinstance(value, dict)
        for value in (
            static_keys,
            layer_keys,
            layer_files,
            component_files,
            aliases,
        )
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
        for source, target in aliases.items()
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
    _validate_deployment_epoch(pack.get("deployment_epoch"))
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
    try:
        root_before = os.stat(root, follow_symlinks=False)
        root_fd = os.open(root, os.O_RDONLY | directory | nofollow | cloexec)
    except OSError as exc:
        raise ValueError("unable to pin stage pack artifact root") from exc
    current_fd = root_fd
    file_fd: int | None = None
    try:
        opened_root = os.fstat(root_fd)
        if (
            not stat.S_ISDIR(root_before.st_mode)
            or not stat.S_ISDIR(opened_root.st_mode)
            or _file_fingerprint(root_before) != _file_fingerprint(opened_root)
        ):
            raise ValueError("stage pack artifact root changed before open")
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
        root_after = os.stat(root, follow_symlinks=False)
        if _file_fingerprint(root_after) != _file_fingerprint(opened_root):
            raise ValueError("stage pack artifact root changed during open")
        handle = os.fdopen(file_fd, "rb")
        file_fd = None
        return handle
    except OSError as exc:
        raise ValueError(
            f"unable to open verified artifact (symlink forbidden): {relative}"
        ) from exc
    finally:
        if file_fd is not None:
            os.close(file_fd)
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
    pack: dict[str, Any],
    *,
    assignment: dict[str, Any],
    manifest: dict[str, Any],
) -> dict[str, Any]:
    """Verify pack identity, authoritative ownership, files, and tensor coverage."""
    if not isinstance(pack, dict):
        raise ValueError("stage pack must be an object")
    if not isinstance(assignment, dict):
        raise ValueError("assignment must be an object")
    if not isinstance(manifest, dict):
        raise ValueError("manifest must be an object")
    _validate_authoritative_assignment(assignment, manifest)
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
            stable_digest = _hash_handle(handle)
            if stable_digest != actual_digest:
                raise ValueError(
                    f"stage pack artifact changed during verification: {upstream_path}"
                )
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


def verify_stage_pack_collection(
    packs: Sequence[dict[str, Any]],
    *,
    assignments: Sequence[dict[str, Any]],
    manifest: dict[str, Any],
) -> dict[str, Any]:
    """Verify exact logical ownership across one ordered N-stage pack collection."""

    if (
        not isinstance(packs, (list, tuple))
        or not isinstance(assignments, (list, tuple))
        or not packs
        or len(packs) != len(assignments)
    ):
        raise ValueError("stage pack collection must match a non-empty assignment set")
    if not isinstance(manifest, dict):
        raise ValueError("manifest must be an object")

    entry_snapshot = copy.deepcopy(
        {
            "packs": list(packs),
            "assignments": list(assignments),
            "manifest": manifest,
        }
    )
    packs = entry_snapshot["packs"]
    assignments = entry_snapshot["assignments"]
    manifest = entry_snapshot["manifest"]

    pack_verifications = [
        verify_stage_pack(
            pack,
            assignment=assignment,
            manifest=manifest,
        )
        for pack, assignment in zip(packs, assignments, strict=True)
    ]
    assignment_ids = [assignment["assignment_id"] for assignment in assignments]
    node_ids = [assignment["node_id"] for assignment in assignments]
    if len(assignment_ids) != len(set(assignment_ids)):
        raise ValueError("stage pack collection has duplicate assignments")
    if len(node_ids) != len(set(node_ids)):
        raise ValueError("stage pack collection has duplicate nodes")

    first = assignments[0]
    canonical_runtime = _normalize_runtime(first.get("runtime"))
    canonical_control_plane_bindings = [
        _canonical_optional_control_plane_binding(assignment)
        for assignment in assignments
    ]
    canonical_control_plane_binding = canonical_control_plane_bindings[0]
    if any(
        binding != canonical_control_plane_binding
        for binding in canonical_control_plane_bindings[1:]
    ):
        raise ValueError(
            "stage pack collection control-plane binding identity mismatch"
        )
    for assignment in assignments[1:]:
        if (
            assignment["deployment_id"],
            assignment["deployment_epoch"],
        ) != (
            first["deployment_id"],
            first["deployment_epoch"],
        ):
            raise ValueError("stage pack collection identity mismatch")
        if _normalize_runtime(assignment.get("runtime")) != canonical_runtime:
            raise ValueError("stage pack collection runtime identity mismatch")

    expected_start = 0
    for assignment in assignments:
        layer_range = assignment["range"]
        if layer_range["start_layer"] != expected_start:
            raise ValueError("stage pack collection ranges overlap or contain a gap")
        expected_start = layer_range["end_layer_exclusive"]
    if expected_start != manifest.get("num_layers"):
        raise ValueError("stage pack collection does not cover every model layer")

    layer_keys = manifest["tensor_keys_by_layer"]
    component_keys = manifest["component_tensor_keys"]
    aliases = manifest["component_aliases"]
    source_tensor_keys = {
        key
        for keys in (*layer_keys.values(), *component_keys.values())
        for key in keys
    }
    verified_tensor_keys = [
        verification["verified_tensor_keys"]
        for verification in pack_verifications
    ]
    owned_sets = [set(keys) for keys in verified_tensor_keys]
    owned_union = set().union(*owned_sets)
    if owned_union != source_tensor_keys:
        raise ValueError(
            "stage pack collection logical tensor ownership mismatch"
        )

    component_owners = [
        {
            key: {
                component
                for component, keys in assignment[
                    "component_tensor_keys"
                ].items()
                if key in keys
            }
            for key in keys
        }
        for assignment, keys in zip(
            assignments,
            verified_tensor_keys,
            strict=True,
        )
    ]
    tied_aliases: list[dict[str, Any]] = []
    for left_index, left_keys in enumerate(owned_sets):
        for right_index in range(left_index + 1, len(owned_sets)):
            for key in sorted(left_keys & owned_sets[right_index]):
                alias_match: tuple[int, str, int, str] | None = None
                for alias_component, target_component in aliases.items():
                    if (
                        alias_component in component_owners[left_index][key]
                        and target_component in component_owners[right_index][key]
                    ):
                        alias_match = (
                            left_index,
                            alias_component,
                            right_index,
                            target_component,
                        )
                    elif (
                        target_component in component_owners[left_index][key]
                        and alias_component in component_owners[right_index][key]
                    ):
                        alias_match = (
                            right_index,
                            alias_component,
                            left_index,
                            target_component,
                        )
                if alias_match is None:
                    raise ValueError(
                        "stage pack collection has duplicate logical tensor ownership"
                    )
                alias_index, alias_component, target_index, target_component = (
                    alias_match
                )
                tied_aliases.append(
                    {
                        "tensor_key": key,
                        "alias_component": alias_component,
                        "alias_assignment_id": assignment_ids[alias_index],
                        "alias_node_id": node_ids[alias_index],
                        "target_component": target_component,
                        "target_assignment_id": assignment_ids[target_index],
                        "target_node_id": node_ids[target_index],
                    }
                )

    artifact_owners: dict[str, list[int]] = {}
    for index, pack in enumerate(packs):
        for artifact in pack["artifacts"]:
            artifact_owners.setdefault(artifact["upstream_path"], []).append(index)
    shared_backing_artifacts = [
        {
            "upstream_path": path,
            "assignment_ids": [assignment_ids[index] for index in owner_indexes],
            "node_ids": [node_ids[index] for index in owner_indexes],
        }
        for path, owner_indexes in sorted(artifact_owners.items())
        if len(owner_indexes) > 1
    ]

    return {
        "protocol": "mycelium.stage_pack_collection_verification.v1",
        "manifest_digest": first["manifest_digest"],
        "pack_count": len(packs),
        "logical_source_tensor_keys": sorted(source_tensor_keys),
        "logical_owned_tensor_keys": [
            {
                "assignment_id": assignment_id,
                "node_id": node_id,
                "tensor_keys": list(keys),
            }
            for assignment_id, node_id, keys in zip(
                assignment_ids,
                node_ids,
                verified_tensor_keys,
                strict=True,
            )
        ],
        "tied_aliases": tied_aliases,
        "shared_backing_artifacts": shared_backing_artifacts,
        "pack_verifications": pack_verifications,
        "exact_logical_coverage": True,
        "route_ready": False,
        "claim_boundary": (
            "ordered assignment-bound stage packs have exact logical tensor "
            "coverage; shared authenticated backing artifacts are not duplicate "
            "logical ownership; layers are not loaded or physically qualified"
        ),
    }


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


def validate_stage_pack_evidence(
    pack: dict[str, Any],
    verification: dict[str, Any],
    *,
    assignment: dict[str, Any],
    manifest: dict[str, Any],
) -> tuple[str, str]:
    """Validate canonical pack evidence without rereading already verified files."""

    _validate_authoritative_assignment(assignment, manifest)
    _validate_verification_evidence(pack, verification, assignment)
    return (
        pack["stage_pack_digest"],
        verification["stage_pack_verification_digest"],
    )


def artifact_report_for_loader(
    pack: dict[str, Any],
    verification: dict[str, Any],
    *,
    assignment: dict[str, Any],
    manifest: dict[str, Any],
) -> dict[str, Any]:
    """Adapt verified pack evidence to the existing offline runtime-loader input."""
    validate_stage_pack_evidence(
        pack,
        verification,
        assignment=assignment,
        manifest=manifest,
    )
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
        "stage_pack": copy.deepcopy(pack),
        "stage_pack_manifest": copy.deepcopy(manifest),
        "stage_pack_verification": copy.deepcopy(verification),
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
