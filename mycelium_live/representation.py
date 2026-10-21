# SPDX-License-Identifier: AGPL-3.0-only
"""Truthful public representation projection from native serving authority."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
from typing import Any, Mapping

from mycelium_physical_runner import load_operator_plan
from physical_inference_node import execution_graph_from_document
from runtime_contracts import validate_normalized_runtime
from weight_provisioning import validate_provisioning_assignment


_MAX_ASSIGNMENT_BYTES = 4 * 1024 * 1024
_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")
_TRANSFER_PROTOCOL = "mycelium.controller_transfer_manifest.v1"


class NativeRepresentationError(ValueError):
    """Required native representation authority is absent or contradictory."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class NativeDeploymentProjection:
    """Public deployment identity derived from assignment-bound runtime facts."""

    deployment_id: str
    model_id: str
    model_revision: str
    topology_size: int
    runtime_dtype: str
    quantization: str


def _reject(code: str, cause: BaseException | None = None) -> None:
    error = NativeRepresentationError(code)
    if cause is None:
        raise error
    raise error from cause


def _duplicate_key(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            _reject("native_assignment_evidence_invalid")
        result[key] = value
    return result


def _reject_constant(_value: str) -> None:
    _reject("native_assignment_evidence_invalid")


def _assignment_path(value: object) -> PurePosixPath:
    if not isinstance(value, str) or not value or "\x00" in value:
        _reject("native_assignment_authority_invalid")
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or path.as_posix() != value
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        _reject("native_assignment_authority_invalid")
    return path


def _manifest_record(
    transfer_manifest: object,
    relative: PurePosixPath,
) -> Mapping[str, Any]:
    if (
        not isinstance(transfer_manifest, Mapping)
        or set(transfer_manifest) != {"protocol", "files"}
        or transfer_manifest.get("protocol") != _TRANSFER_PROTOCOL
        or not isinstance(transfer_manifest.get("files"), list)
    ):
        _reject("native_assignment_authority_invalid")
    matches = [
        record
        for record in transfer_manifest["files"]
        if isinstance(record, Mapping) and record.get("path") == relative.as_posix()
    ]
    if len(matches) != 1:
        _reject("native_assignment_evidence_unavailable")
    record = matches[0]
    size = record.get("size_bytes")
    digest = record.get("content_digest")
    if (
        set(record) != {"path", "size_bytes", "content_digest"}
        or type(size) is not int
        or not 0 < size <= _MAX_ASSIGNMENT_BYTES
        or not isinstance(digest, str)
        or _SHA256.fullmatch(digest) is None
    ):
        _reject("native_assignment_authority_invalid")
    return record


def _read_bound_assignment(
    source_root: Path,
    relative: PurePosixPath,
    transfer_manifest: object,
) -> dict[str, Any]:
    record = _manifest_record(transfer_manifest, relative)
    try:
        root_metadata = source_root.lstat()
        resolved_root = source_root.resolve(strict=True)
        if stat.S_ISLNK(root_metadata.st_mode) or not stat.S_ISDIR(root_metadata.st_mode):
            _reject("native_assignment_evidence_unavailable")
        candidate = resolved_root.joinpath(*relative.parts)
        cursor = resolved_root
        for part in relative.parts:
            cursor = cursor / part
            metadata = cursor.lstat()
            if stat.S_ISLNK(metadata.st_mode):
                _reject("native_assignment_evidence_unavailable")
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(resolved_root)
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(resolved, flags)
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
                _reject("native_assignment_evidence_unavailable")
            raw = os.read(descriptor, _MAX_ASSIGNMENT_BYTES + 1)
        finally:
            os.close(descriptor)
    except NativeRepresentationError:
        raise
    except (OSError, RuntimeError, ValueError) as exc:
        _reject("native_assignment_evidence_unavailable", exc)
    if (
        len(raw) != record["size_bytes"]
        or len(raw) > _MAX_ASSIGNMENT_BYTES
        or "sha256:" + hashlib.sha256(raw).hexdigest() != record["content_digest"]
    ):
        _reject("native_assignment_evidence_mismatch")
    try:
        document = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_duplicate_key,
            parse_constant=_reject_constant,
        )
    except NativeRepresentationError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError, RecursionError) as exc:
        _reject("native_assignment_evidence_invalid", exc)
    if not isinstance(document, dict):
        _reject("native_assignment_evidence_invalid")
    return document


def native_deployment_projection_from_config(config: object) -> NativeDeploymentProjection:
    """Derive one representation from a strictly loaded operator-plan config.

    Assignment files must be byte-bound by the plan's transfer manifest, valid
    layer-assignment authorities, graph-bound, and unanimous about serving dtype
    and quantization. Missing or conflicting facts reject the projection.
    """

    controller = getattr(config, "controller", None)
    if not isinstance(controller, Mapping):
        _reject("native_operator_plan_authority_invalid")
    run_plan = controller.get("run_plan")
    nodes = run_plan.get("nodes") if isinstance(run_plan, Mapping) else None
    source_value = controller.get("source_root")
    if (
        not isinstance(nodes, list)
        or len(nodes) < 2
        or not isinstance(source_value, str)
        or not source_value
    ):
        _reject("native_operator_plan_authority_invalid")
    source_root = Path(source_value)

    graph_document: Mapping[str, Any] | None = None
    assignments: list[tuple[dict[str, Any], dict[str, Any]]] = []
    node_ids: list[str] = []
    for node in nodes:
        configure = node.get("configure") if isinstance(node, Mapping) else None
        node_id = node.get("node_id") if isinstance(node, Mapping) else None
        candidate_graph = configure.get("graph") if isinstance(configure, Mapping) else None
        assignment_file = (
            configure.get("assignment_file") if isinstance(configure, Mapping) else None
        )
        if (
            not isinstance(node_id, str)
            or not node_id
            or node_id in node_ids
            or not isinstance(candidate_graph, Mapping)
        ):
            _reject("native_operator_plan_authority_invalid")
        if graph_document is None:
            graph_document = candidate_graph
        elif candidate_graph != graph_document:
            _reject("native_graph_authority_conflict")
        assignment = _read_bound_assignment(
            source_root,
            _assignment_path(assignment_file),
            controller.get("transfer_manifest"),
        )
        try:
            validate_provisioning_assignment(assignment)
            runtime = validate_normalized_runtime(assignment.get("runtime"))
        except (TypeError, ValueError) as exc:
            _reject("native_assignment_evidence_invalid", exc)
        assignments.append((assignment, runtime))
        node_ids.append(node_id)

    assert graph_document is not None
    try:
        graph = execution_graph_from_document(graph_document)
    except (TypeError, ValueError, RuntimeError) as exc:
        _reject("native_graph_authority_invalid", exc)

    placements = [
        placement
        for stage in graph.stages
        for placement in stage.placements
    ]
    if {placement.node_id for placement in placements} != set(node_ids):
        _reject("native_assignment_graph_mismatch")
    for (assignment, runtime), node_id in zip(assignments, node_ids, strict=True):
        if (
            assignment.get("node_id") != node_id
            or assignment.get("deployment_id") != graph.deployment_id
            or assignment.get("deployment_epoch") != graph.deployment_epoch
            or assignment.get("model_id") != graph.model_id
            or assignment.get("resolved_commit") != graph.resolved_commit
            or assignment.get("manifest_digest") != graph.manifest_digest
            or not any(
                placement.node_id == node_id
                and placement.assignment_id == assignment.get("assignment_id")
                and placement.runtime_backend == runtime["backend"]
                for placement in placements
            )
        ):
            _reject("native_assignment_graph_mismatch")

    representations = {
        (runtime["dtype"], runtime["quantization"]) for _assignment, runtime in assignments
    }
    if len(representations) != 1:
        _reject("native_assignment_representation_conflict")
    runtime_dtype, quantization = representations.pop()
    return NativeDeploymentProjection(
        deployment_id=graph.deployment_id,
        model_id=graph.model_id,
        model_revision=graph.resolved_commit,
        topology_size=len(graph.stages),
        runtime_dtype=runtime_dtype,
        quantization=quantization,
    )


def native_deployment_projection(operator_plan: Path) -> NativeDeploymentProjection:
    """Load a strict operator plan and derive its assignment-bound projection."""

    try:
        config = load_operator_plan(operator_plan)
    except Exception as exc:
        _reject("native_operator_plan_authority_invalid", exc)
    return native_deployment_projection_from_config(config)


__all__ = [
    "NativeDeploymentProjection",
    "NativeRepresentationError",
    "native_deployment_projection",
    "native_deployment_projection_from_config",
]
