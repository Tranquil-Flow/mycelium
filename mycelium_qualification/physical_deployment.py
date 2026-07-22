"""Offline deterministic model and assignment preparation for physical probes."""
from __future__ import annotations

import copy
from dataclasses import dataclass, replace
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import struct
from typing import Any, Mapping, Sequence

from layer_assignment import compile_layer_assignments
from model_manifest import compile_model_manifest, manifest_digest_ref
from mycelium_router.contracts import (
    DeviceState,
    ExecutionGraph,
    LayerRange,
    Placement,
    PlacementEdge,
    Stage,
    StageCost,
)
from mycelium_router.layer_builder import layer_load_proof_digest
from mycelium_router.mlx_runtime import _stage_signature
from runtime_loader import canonical_json
from two_process_runtime_qualification import (
    DEPLOYMENT_EPOCH,
    DEPLOYMENT_ID,
    _LocalOnlyFetcher,
    _build_local_model,
    _control_plane_binding,
    _route_for_manifest,
)
from weight_provisioning import artifact_report_errors, provision_assignment, sha256_file


class PhysicalDeploymentError(RuntimeError):
    """Fail-closed deterministic preparation error."""


_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
_SAFE_LOCAL_RELATIVE_RE = re.compile(r"^[A-Za-z0-9._/-]+$")
_MAX_SAFETENSORS_HEADER_BYTES = 100 * 1024 * 1024
_MODEL_INDEX_FILE = "model.safetensors.index.json"
_MONOLITHIC_WEIGHT_FILE = "model.safetensors"
_LOCAL_MODEL_SOURCE_PROTOCOL = "mycelium.local_model_source.v1"
_DEFAULT_TOKENIZER_ASSETS = (
    "tokenizer.json",
    "vocab.json",
    "merges.txt",
    "tokenizer_config.json",
    "special_tokens_map.json",
    "added_tokens.json",
)
_SAFETENSORS_DTYPE_BYTES = {
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
_RUNTIME_DTYPE_BYTES = {"float16": 2, "bfloat16": 2, "float32": 4}
_PREPARABLE_RUNTIME_DTYPES = frozenset({"float16", "float32"})


@dataclass(frozen=True)
class LocalModelSource:
    """Already-local Hugging Face GPT-2 Safetensors snapshot metadata."""

    root: str | Path
    model_id: str
    resolved_commit: str
    requested_revision: str = "local"
    tokenizer_assets: tuple[str, ...] = _DEFAULT_TOKENIZER_ASSETS


@dataclass(frozen=True)
class _ValidatedLocalModelSource:
    root: Path
    model_id: str
    requested_revision: str
    resolved_commit: str
    tokenizer_assets: tuple[str, ...]


@dataclass(frozen=True)
class _PreparedLocalModelSource:
    manifest: dict[str, Any]
    source_metadata: dict[str, Any]
    tokenizer_assets: tuple[dict[str, Any], ...]


@dataclass(frozen=True)
class _ResolvedLocalModelSource:
    source: _ValidatedLocalModelSource
    manifest: dict[str, Any]
    checkpoint_index: dict[str, Any]
    source_metadata: dict[str, Any]
    referenced_files: tuple[str, ...]
    synthesized_index: bool


@dataclass(frozen=True)
class _PreparedAssignments:
    manifest: dict[str, Any]
    route: dict[str, Any]
    assignments: list[dict[str, Any]]
    reports: list[dict[str, Any]]
    fetcher: Any
    source_metadata: dict[str, Any] | None = None
    tokenizer_assets: tuple[dict[str, Any], ...] = ()


def _activation_bytes_for_assignments(
    assignments: Sequence[dict[str, Any]],
) -> int:
    dtypes = {
        assignment.get("runtime", {}).get("dtype")
        for assignment in assignments
    }
    if len(dtypes) != 1:
        raise PhysicalDeploymentError("execution_graph_runtime_dtype_mismatch")
    dtype = next(iter(dtypes))
    activation_bytes = _RUNTIME_DTYPE_BYTES.get(str(dtype))
    if activation_bytes is None:
        raise PhysicalDeploymentError("unsupported_execution_graph_runtime_dtype")
    return activation_bytes


def build_execution_graph(
    assignments: Sequence[dict[str, Any]],
    proofs: Sequence[Mapping[str, Any]],
    *,
    link_scheme: str = "local-loopback",
    runtime_scheme: str = "pipe",
) -> ExecutionGraph:
    """Bind two exact assignment/load-proof pairs into one signed graph."""

    if len(assignments) != 2 or len(proofs) != 2:
        raise PhysicalDeploymentError("execution_graph_requires_two_assignments")
    if not link_scheme or not runtime_scheme:
        raise PhysicalDeploymentError("invalid_execution_graph_scheme")
    normalized_proofs = tuple(json.loads(canonical_json(proof)) for proof in proofs)

    stages: list[Stage] = []
    placements: list[Placement] = []
    for index, (assignment, proof) in enumerate(zip(assignments, normalized_proofs)):
        stage_id = f"stage-{index:03d}"
        placement = Placement(
            placement_id=f"placement-{index:03d}",
            node_id=assignment["node_id"],
            replica_group_id=f"{stage_id}-replicas",
            assignment_id=assignment["assignment_id"],
            stage_signature="pending-stage-signature",
            load_proof_digest=layer_load_proof_digest(proof),
            runtime_backend=assignment["runtime"]["backend"],
            runtime_endpoint=(
                f"{runtime_scheme}://{assignment['node_id']}/"
                f"{assignment['assignment_id']}"
            ),
        )
        layer_range = assignment["range"]
        stage = Stage(
            stage_id=stage_id,
            layer_range=LayerRange(
                start_layer=layer_range["start_layer"],
                end_layer_exclusive=layer_range["end_layer_exclusive"],
                layer_count=layer_range["layer_count"],
            ),
            component_roles=tuple(assignment["components"]),
            stage_cost=StageCost(
                prefill_work_units_per_prompt_token=1.0,
                decode_work_units_per_token=1.0,
                kv_bytes_per_context_token=32,
            ),
            placements=(placement,),
        )
        placements.append(placement)
        stages.append(stage)

    first_node = assignments[0]["node_id"]
    second_node = assignments[1]["node_id"]
    graph = ExecutionGraph(
        deployment_id=assignments[0]["deployment_id"],
        deployment_epoch=assignments[0]["deployment_epoch"],
        topology_version=1,
        model_id=assignments[0]["model_id"],
        resolved_commit=assignments[0]["resolved_commit"],
        manifest_digest=assignments[0]["manifest_digest"],
        entry_stage_id=stages[0].stage_id,
        final_stage_id=stages[-1].stage_id,
        hidden_size=assignments[0]["runtime"]["model_config"]["n_embd"],
        activation_bytes=_activation_bytes_for_assignments(assignments),
        token_envelope_bytes=9,
        stages=tuple(stages),
        edges=(
            PlacementEdge(
                edge_id="forward:placement-000->placement-001",
                from_placement_id=placements[0].placement_id,
                to_placement_id=placements[1].placement_id,
                link_id=f"{link_scheme}:{first_node}->{second_node}",
            ),
        ),
        loopback_edges=(
            PlacementEdge(
                edge_id="loopback:placement-001->placement-000",
                from_placement_id=placements[1].placement_id,
                to_placement_id=placements[0].placement_id,
                link_id=f"{link_scheme}:{second_node}->{first_node}",
            ),
        ),
    )
    signed_stages = tuple(
        replace(
            stage,
            placements=(
                replace(
                    stage.placements[0],
                    stage_signature=_stage_signature(graph, stage, proof),
                ),
            ),
        )
        for stage, proof in zip(graph.stages, normalized_proofs)
    )
    return replace(graph, stages=signed_stages)


def build_physical_device_states(graph: ExecutionGraph) -> dict[str, DeviceState]:
    """Create deterministic alive-state inputs for every graph participant."""

    node_ids = tuple(
        dict.fromkeys(
            placement.node_id
            for stage in graph.stages
            for placement in stage.placements
        )
    )
    if len(node_ids) != 2:
        raise PhysicalDeploymentError("physical_graph_requires_two_nodes")
    bandwidth = {node_id: 1_000_000_000.0 for node_id in node_ids}
    return {
        node_id: DeviceState(
            node_id=node_id,
            state_seq=1,
            last_updated=1.0,
            availability="ALIVE",
            compute_units_per_second=1_000.0,
            free_compute_fraction=1.0,
            available_kv_bytes=1_000_000,
            pending_hop_queue_depth=0,
            neighbor_rtt_ms={
                neighbor: 0.0 if neighbor == node_id else 1.0
                for neighbor in node_ids
            },
            neighbor_bandwidth_bytes_per_second=dict(bandwidth),
        )
        for node_id in node_ids
    }


@dataclass
class PhysicalDeployment:
    """Prepared local artifacts consumed by process and transport harnesses."""

    root: Path
    manifest: dict[str, Any]
    route_plan: dict[str, Any]
    assignments: tuple[dict[str, Any], ...]
    artifact_reports: tuple[dict[str, Any], ...]
    reference_assignment: dict[str, Any]
    reference_report: dict[str, Any]
    local_fetch_requests: tuple[str, ...]
    model_artifact_digests: tuple[tuple[str, str], ...]
    network_downloads: int = 0
    route_ready: bool = False
    model_source: dict[str, Any] | None = None
    tokenizer_assets: tuple[dict[str, Any], ...] = ()

    def evidence_document(self) -> dict[str, Any]:
        """Return JSON evidence without host-specific absolute artifact paths."""

        def assignment_evidence(assignment: dict[str, Any]) -> dict[str, Any]:
            return {
                "assignment_id": assignment["assignment_id"],
                "node_id": assignment["node_id"],
                "range": copy.deepcopy(assignment["range"]),
                "components": list(assignment["components"]),
                "expected_tensor_keys": list(assignment["expected_tensor_keys"]),
                "runtime": copy.deepcopy(assignment["runtime"]),
            }

        document = {
            "protocol": "mycelium.physical_deployment.v1",
            "route_ready": self.route_ready,
            "network_downloads": self.network_downloads,
            "model_id": self.manifest["model_id"],
            "resolved_commit": self.manifest["resolved_commit"],
            "manifest_digest": manifest_digest_ref(self.manifest),
            "model_artifact_digests": [
                {"path": path, "digest": digest}
                for path, digest in self.model_artifact_digests
            ],
            "assignments": [
                assignment_evidence(assignment) for assignment in self.assignments
            ],
            "reference_assignment": assignment_evidence(
                self.reference_assignment
            ),
            "local_fetch_requests": list(self.local_fetch_requests),
        }
        if self.model_source is not None:
            document["model_source"] = copy.deepcopy(self.model_source)
        if self.tokenizer_assets:
            document["tokenizer_assets"] = [
                copy.deepcopy(asset) for asset in self.tokenizer_assets
            ]
        return document


def _validate_nodes(node_ids: tuple[str, ...]) -> None:
    if (
        len(node_ids) < 2
        or any(not isinstance(node, str) or not node.strip() for node in node_ids)
        or len(set(node_ids)) != len(node_ids)
        or "reference-node" in node_ids
    ):
        raise PhysicalDeploymentError("invalid_physical_nodes")


def _prepare_root(root: Path) -> Path:
    if root.is_symlink():
        raise PhysicalDeploymentError("deployment_root_symlink")
    if root.exists():
        if not root.is_dir() or any(root.iterdir()):
            raise PhysicalDeploymentError("deployment_root_not_empty")
    else:
        try:
            root.mkdir()
        except OSError as exc:
            raise PhysicalDeploymentError("deployment_root_create_failed") from exc
    return root.resolve(strict=True)


def _invalid_local_model_source(reason: str) -> PhysicalDeploymentError:
    return PhysicalDeploymentError(f'invalid_local_model_source:{reason}')


def _validate_runtime_dtype(runtime_dtype: str) -> str:
    if not isinstance(runtime_dtype, str) or runtime_dtype not in _PREPARABLE_RUNTIME_DTYPES:
        raise PhysicalDeploymentError('invalid_runtime_dtype')
    return runtime_dtype


def _safe_local_relative_path(value: Any, *, field: str) -> PurePosixPath:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 1024
        or not _SAFE_LOCAL_RELATIVE_RE.fullmatch(value)
    ):
        raise _invalid_local_model_source(f'unsafe_{field}_path')
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or len(path.parts) > 16
        or any(part in ('', '.', '..') for part in path.parts)
    ):
        raise _invalid_local_model_source(f'unsafe_{field}_path')
    return path


def _path_from_posix(root: Path, relative: PurePosixPath) -> Path:
    return root.joinpath(*relative.parts)


def _resolve_source_path(
    source_root: Path,
    relative: PurePosixPath,
    *,
    required: bool,
) -> Path | None:
    candidate = _path_from_posix(source_root, relative)
    current = source_root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise _invalid_local_model_source('source_path_symlink')
        if not current.exists():
            if required:
                raise _invalid_local_model_source('source_path_missing')
            return None
    try:
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(source_root)
    except (OSError, ValueError) as exc:
        raise _invalid_local_model_source('source_path_escape') from exc
    return resolved


def _stat_source_regular(path: Path) -> os.stat_result:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise _invalid_local_model_source('source_file_unavailable') from exc
    if not stat.S_ISREG(metadata.st_mode):
        raise _invalid_local_model_source('source_file_not_regular')
    return metadata


def _open_read_only_no_follow(path: Path) -> int:
    flags = os.O_RDONLY | getattr(os, 'O_CLOEXEC', 0) | getattr(os, 'O_NOFOLLOW', 0)
    try:
        return os.open(path, flags)
    except OSError as exc:
        raise _invalid_local_model_source('source_file_open_failed') from exc


def _reject_duplicate_json_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f'duplicate JSON key: {key}')
        result[key] = value
    return result


def _reject_json_constant(value: str) -> None:
    raise ValueError(f'non-finite JSON value: {value}')


def _read_source_json(source_root: Path, relative: PurePosixPath) -> Any:
    path = _resolve_source_path(source_root, relative, required=True)
    if path is None:
        raise _invalid_local_model_source('source_path_missing')
    metadata = _stat_source_regular(path)
    if metadata.st_size <= 0:
        raise _invalid_local_model_source('json_file_empty')
    descriptor = _open_read_only_no_follow(path)
    try:
        with os.fdopen(descriptor, 'rb') as handle:
            payload = handle.read()
    except OSError as exc:
        raise _invalid_local_model_source('json_file_read_failed') from exc
    try:
        return json.loads(
            payload.decode('utf-8'),
            object_pairs_hook=_reject_duplicate_json_keys,
            parse_constant=_reject_json_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise _invalid_local_model_source('invalid_json') from exc


@dataclass(frozen=True)
class _SafetensorsHeader:
    tensor_names: tuple[str, ...]
    tensor_data_bytes: int


def _read_safetensors_header(path: Path, display_path: str) -> _SafetensorsHeader:
    del display_path
    descriptor = _open_read_only_no_follow(path)
    try:
        with os.fdopen(descriptor, 'rb') as handle:
            metadata = os.fstat(handle.fileno())
            if not stat.S_ISREG(metadata.st_mode):
                raise _invalid_local_model_source('safetensors_not_regular')
            file_size = metadata.st_size
            prefix = handle.read(8)
            if len(prefix) != 8:
                raise _invalid_local_model_source('safetensors_header_truncated')
            header_length = struct.unpack('<Q', prefix)[0]
            if (
                header_length == 0
                or header_length > _MAX_SAFETENSORS_HEADER_BYTES
                or header_length > file_size - 8
            ):
                raise _invalid_local_model_source('safetensors_header_size')
            header_bytes = handle.read(header_length)
            if len(header_bytes) != header_length:
                raise _invalid_local_model_source('safetensors_header_truncated')
    except PhysicalDeploymentError:
        raise
    except (OSError, struct.error) as exc:
        raise _invalid_local_model_source('safetensors_header_read_failed') from exc
    try:
        header = json.loads(
            header_bytes,
            object_pairs_hook=_reject_duplicate_json_keys,
            parse_constant=_reject_json_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise _invalid_local_model_source('safetensors_header_json') from exc
    if not isinstance(header, dict) or not header:
        raise _invalid_local_model_source('safetensors_header_object')
    header = dict(header)
    header_metadata = header.pop('__metadata__', None)
    if header_metadata is not None and (
        not isinstance(header_metadata, dict)
        or not all(
            isinstance(key, str) and isinstance(value, str)
            for key, value in header_metadata.items()
        )
    ):
        raise _invalid_local_model_source('safetensors_metadata_invalid')
    if not header:
        raise _invalid_local_model_source('safetensors_no_tensors')

    data_size = file_size - 8 - header_length
    intervals: list[tuple[int, int, str]] = []
    tensor_names: list[str] = []
    for name, entry in header.items():
        if not isinstance(name, str) or not name or not isinstance(entry, dict):
            raise _invalid_local_model_source('safetensors_tensor_invalid')
        if set(entry) != {'dtype', 'shape', 'data_offsets'}:
            raise _invalid_local_model_source('safetensors_tensor_metadata_invalid')
        dtype = entry['dtype']
        shape = entry['shape']
        offsets = entry['data_offsets']
        if dtype not in _SAFETENSORS_DTYPE_BYTES:
            raise _invalid_local_model_source('safetensors_dtype_unsupported')
        if (
            not isinstance(shape, list)
            or any(
                not isinstance(dimension, int)
                or isinstance(dimension, bool)
                or dimension < 0
                for dimension in shape
            )
        ):
            raise _invalid_local_model_source('safetensors_shape_invalid')
        if (
            not isinstance(offsets, list)
            or len(offsets) != 2
            or any(
                not isinstance(offset, int) or isinstance(offset, bool)
                for offset in offsets
            )
        ):
            raise _invalid_local_model_source('safetensors_offsets_invalid')
        start, end = offsets
        if start < 0 or end < start or end > data_size:
            raise _invalid_local_model_source('safetensors_offsets_out_of_bounds')
        element_count = 1
        for dimension in shape:
            element_count *= dimension
        if end - start != element_count * _SAFETENSORS_DTYPE_BYTES[dtype]:
            raise _invalid_local_model_source('safetensors_tensor_size_mismatch')
        tensor_names.append(name)
        intervals.append((start, end, name))

    ordered_intervals = sorted(intervals)
    previous_end = 0
    cursor = 0
    for start, end, _name in ordered_intervals:
        if start < previous_end:
            raise _invalid_local_model_source('safetensors_data_overlap')
        previous_end = end
        if start > cursor:
            raise _invalid_local_model_source('safetensors_data_gap')
        cursor = end
    if cursor != data_size:
        raise _invalid_local_model_source('safetensors_trailing_data')
    return _SafetensorsHeader(
        tensor_names=tuple(sorted(tensor_names)),
        tensor_data_bytes=data_size,
    )


def _validate_local_model_source(
    model_source: LocalModelSource,
) -> _ValidatedLocalModelSource:
    if not isinstance(model_source, LocalModelSource):
        raise _invalid_local_model_source('invalid_type')
    try:
        root = Path(model_source.root).expanduser()
    except TypeError as exc:
        raise _invalid_local_model_source('invalid_root') from exc
    if root.is_symlink():
        raise _invalid_local_model_source('root_symlink')
    try:
        resolved_root = root.resolve(strict=True)
    except OSError as exc:
        raise _invalid_local_model_source('root_missing') from exc
    if not resolved_root.is_dir():
        raise _invalid_local_model_source('root_not_directory')
    if (
        not isinstance(model_source.model_id, str)
        or '/' not in model_source.model_id
        or not model_source.model_id.strip()
        or model_source.model_id != model_source.model_id.strip()
    ):
        raise _invalid_local_model_source('invalid_model_id')
    if (
        not isinstance(model_source.requested_revision, str)
        or not model_source.requested_revision.strip()
        or model_source.requested_revision != model_source.requested_revision.strip()
        or '\x00' in model_source.requested_revision
    ):
        raise _invalid_local_model_source('invalid_requested_revision')
    if not _COMMIT_RE.fullmatch(str(model_source.resolved_commit)):
        raise _invalid_local_model_source('invalid_resolved_commit')
    if not isinstance(model_source.tokenizer_assets, tuple):
        raise _invalid_local_model_source('invalid_tokenizer_assets')
    tokenizer_assets: list[str] = []
    for asset in model_source.tokenizer_assets:
        relative = _safe_local_relative_path(asset, field='tokenizer_asset')
        path = str(relative)
        if path in tokenizer_assets:
            raise _invalid_local_model_source('duplicate_tokenizer_asset')
        tokenizer_assets.append(path)
    return _ValidatedLocalModelSource(
        root=resolved_root,
        model_id=model_source.model_id,
        requested_revision=model_source.requested_revision,
        resolved_commit=model_source.resolved_commit,
        tokenizer_assets=tuple(tokenizer_assets),
    )


def _normalize_checkpoint_index(index: Any) -> dict[str, Any]:
    if not isinstance(index, dict):
        raise _invalid_local_model_source('checkpoint_index_not_object')
    weight_map = index.get('weight_map')
    if not isinstance(weight_map, dict) or not weight_map:
        raise _invalid_local_model_source('checkpoint_index_missing_weight_map')
    normalized_weight_map: dict[str, str] = {}
    for tensor_name, filename in weight_map.items():
        if not isinstance(tensor_name, str) or not tensor_name:
            raise _invalid_local_model_source('checkpoint_index_invalid_tensor_name')
        relative = _safe_local_relative_path(filename, field='weight')
        normalized_weight_map[tensor_name] = str(relative)
    normalized = copy.deepcopy(index)
    normalized['weight_map'] = normalized_weight_map
    return normalized


def _load_checkpoint_index(
    source: _ValidatedLocalModelSource,
) -> tuple[dict[str, Any], str, bool]:
    index_relative = _safe_local_relative_path(_MODEL_INDEX_FILE, field='index')
    index_path = _resolve_source_path(source.root, index_relative, required=False)
    if index_path is not None:
        return (
            _normalize_checkpoint_index(_read_source_json(source.root, index_relative)),
            'safetensors_sharded',
            False,
        )

    model_relative = _safe_local_relative_path(_MONOLITHIC_WEIGHT_FILE, field='weight')
    model_path = _resolve_source_path(source.root, model_relative, required=False)
    if model_path is None:
        raise _invalid_local_model_source('missing_safetensors_checkpoint')
    _stat_source_regular(model_path)
    header = _read_safetensors_header(model_path, _MONOLITHIC_WEIGHT_FILE)
    checkpoint_index = {
        'metadata': {'total_size': model_path.stat().st_size},
        'weight_map': {
            tensor_name: _MONOLITHIC_WEIGHT_FILE
            for tensor_name in header.tensor_names
        },
    }
    return checkpoint_index, 'safetensors_monolithic', True


def _resolve_local_model_source(
    model_source: LocalModelSource,
) -> _ResolvedLocalModelSource:
    source = _validate_local_model_source(model_source)
    config_relative = _safe_local_relative_path('config.json', field='config')
    config = _read_source_json(source.root, config_relative)
    if not isinstance(config, dict):
        raise _invalid_local_model_source('config_not_object')
    checkpoint_index, source_format, synthesized_index = _load_checkpoint_index(source)
    weight_map = checkpoint_index['weight_map']
    referenced_files = tuple(sorted(set(weight_map.values())))
    if not referenced_files:
        raise _invalid_local_model_source('checkpoint_index_empty')

    file_metadata: dict[str, dict[str, Any]] = {}
    for filename in referenced_files:
        relative = _safe_local_relative_path(filename, field='weight')
        path = _resolve_source_path(source.root, relative, required=True)
        if path is None:
            raise _invalid_local_model_source('source_path_missing')
        metadata = _stat_source_regular(path)
        header = _read_safetensors_header(path, filename)
        mapped_tensors = sorted(
            tensor_name
            for tensor_name, mapped_filename in weight_map.items()
            if mapped_filename == filename
        )
        if mapped_tensors != list(header.tensor_names):
            raise _invalid_local_model_source('checkpoint_index_tensor_mismatch')
        file_metadata[filename] = {
            'size_bytes': metadata.st_size,
            'sha256': sha256_file(path),
        }

    try:
        manifest = compile_model_manifest(
            model_id=source.model_id,
            requested_revision=source.requested_revision,
            resolved_commit=source.resolved_commit,
            config=config,
            checkpoint_index=checkpoint_index,
            file_metadata=file_metadata,
            index_file=_MODEL_INDEX_FILE,
        )
    except (TypeError, ValueError) as exc:
        raise _invalid_local_model_source('manifest_compile_failed') from exc
    ordered_files = tuple(item['path'] for item in manifest['files'])
    source_metadata = {
        'protocol': _LOCAL_MODEL_SOURCE_PROTOCOL,
        'model_id': source.model_id,
        'requested_revision': source.requested_revision,
        'resolved_commit': source.resolved_commit,
        'format': source_format,
        'index_file': _MODEL_INDEX_FILE,
        'files': list(ordered_files),
    }
    return _ResolvedLocalModelSource(
        source=source,
        manifest=manifest,
        checkpoint_index=checkpoint_index,
        source_metadata=source_metadata,
        referenced_files=ordered_files,
        synthesized_index=synthesized_index,
    )


def _record_private_file(
    path: Path,
    relative: PurePosixPath,
    seen_inodes: set[tuple[int, int]],
) -> dict[str, Any]:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise PhysicalDeploymentError('private_file_stat_failed') from exc
    if (
        path.is_symlink()
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_nlink != 1
        or metadata.st_mode & 0o077 != 0
    ):
        raise PhysicalDeploymentError('private_file_is_not_isolated')
    inode = (metadata.st_dev, metadata.st_ino)
    if inode in seen_inodes:
        raise PhysicalDeploymentError('private_file_inode_reused')
    seen_inodes.add(inode)
    return {
        'path': str(relative),
        'size_bytes': metadata.st_size,
        'content_digest': {
            'algorithm': 'sha256',
            'value': sha256_file(path),
        },
    }


def _write_private_bytes(
    destination_root: Path,
    relative: PurePosixPath,
    payload: bytes,
    seen_inodes: set[tuple[int, int]],
) -> dict[str, Any]:
    destination = _path_from_posix(destination_root, relative)
    destination.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, 'O_CLOEXEC', 0)
        | getattr(os, 'O_NOFOLLOW', 0)
    )
    descriptor: int | None = None
    try:
        descriptor = os.open(destination, flags, 0o600)
        with os.fdopen(descriptor, 'wb') as handle:
            descriptor = None
            handle.write(payload)
        os.chmod(destination, 0o600)
    except BaseException:
        if descriptor is not None:
            os.close(descriptor)
        try:
            destination.unlink()
        except OSError:
            pass
        raise
    return _record_private_file(destination, relative, seen_inodes)


def _copy_private_file(
    source_root: Path,
    destination_root: Path,
    relative: PurePosixPath,
    seen_inodes: set[tuple[int, int]],
) -> dict[str, Any]:
    source_path = _resolve_source_path(source_root, relative, required=True)
    if source_path is None:
        raise _invalid_local_model_source('source_path_missing')
    destination = _path_from_posix(destination_root, relative)
    destination.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    input_descriptor = _open_read_only_no_follow(source_path)
    output_descriptor: int | None = None
    output_flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, 'O_CLOEXEC', 0)
        | getattr(os, 'O_NOFOLLOW', 0)
    )
    try:
        input_metadata = os.fstat(input_descriptor)
        if not stat.S_ISREG(input_metadata.st_mode):
            raise _invalid_local_model_source('source_file_not_regular')
        output_descriptor = os.open(destination, output_flags, 0o600)
        with os.fdopen(input_descriptor, 'rb') as source_handle, os.fdopen(
            output_descriptor, 'wb'
        ) as destination_handle:
            input_descriptor = -1
            output_descriptor = None
            while True:
                chunk = source_handle.read(1024 * 1024)
                if not chunk:
                    break
                destination_handle.write(chunk)
        os.chmod(destination, 0o600)
    except BaseException:
        if input_descriptor >= 0:
            os.close(input_descriptor)
        if output_descriptor is not None:
            os.close(output_descriptor)
        try:
            destination.unlink()
        except OSError:
            pass
        raise
    return _record_private_file(destination, relative, seen_inodes)


def _materialize_local_model_source(
    resolved: _ResolvedLocalModelSource,
    destination_root: Path,
) -> _PreparedLocalModelSource:
    seen_inodes: set[tuple[int, int]] = set()
    source = resolved.source
    config_relative = _safe_local_relative_path('config.json', field='config')
    _copy_private_file(source.root, destination_root, config_relative, seen_inodes)
    for filename in resolved.referenced_files:
        _copy_private_file(
            source.root,
            destination_root,
            _safe_local_relative_path(filename, field='weight'),
            seen_inodes,
        )
    index_relative = _safe_local_relative_path(_MODEL_INDEX_FILE, field='index')
    if resolved.synthesized_index:
        _write_private_bytes(
            destination_root,
            index_relative,
            (canonical_json(resolved.checkpoint_index) + '\n').encode('utf-8'),
            seen_inodes,
        )
    else:
        _copy_private_file(source.root, destination_root, index_relative, seen_inodes)

    reserved_paths = {
        'config.json',
        _MODEL_INDEX_FILE,
        *resolved.referenced_files,
    }
    tokenizer_assets: list[dict[str, Any]] = []
    for asset in source.tokenizer_assets:
        if asset in reserved_paths:
            raise _invalid_local_model_source('tokenizer_asset_conflicts_with_model_file')
        relative = _safe_local_relative_path(asset, field='tokenizer_asset')
        if _resolve_source_path(source.root, relative, required=False) is None:
            continue
        tokenizer_assets.append(
            _copy_private_file(source.root, destination_root, relative, seen_inodes)
        )

    return _PreparedLocalModelSource(
        manifest=resolved.manifest,
        source_metadata=resolved.source_metadata,
        tokenizer_assets=tuple(tokenizer_assets),
    )


class _IdentityAwareLocalFetcher:
    '''Resolve copied local model artifacts only for their pinned identity.'''

    def __init__(self, root: Path, *, model_id: str, resolved_commit: str) -> None:
        self.root = root.resolve(strict=True)
        self.model_id = model_id
        self.resolved_commit = resolved_commit
        self.requests: list[str] = []

    def __call__(
        self,
        model_id: str,
        revision: str,
        filename: str,
        cache_root: str | Path,
        local_files_only: bool = False,
    ) -> tuple[Path, bool]:
        if local_files_only is not True:
            raise PhysicalDeploymentError('local_model_fetch_not_offline')
        if model_id != self.model_id or revision != self.resolved_commit:
            raise PhysicalDeploymentError('local_model_fetch_identity_mismatch')
        try:
            cache_root_path = Path(cache_root).resolve(strict=True)
        except OSError as exc:
            raise PhysicalDeploymentError('local_model_fetch_cache_root_missing') from exc
        if cache_root_path != self.root:
            raise PhysicalDeploymentError('local_model_fetch_cache_root_mismatch')
        relative = _safe_local_relative_path(filename, field='artifact')
        candidate = _path_from_posix(self.root, relative)
        try:
            resolved = candidate.resolve(strict=True)
            resolved.relative_to(self.root)
        except (OSError, ValueError) as exc:
            raise PhysicalDeploymentError('local_model_fetch_path_escape') from exc
        metadata = resolved.lstat()
        if (
            candidate.is_symlink()
            or not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
        ):
            raise PhysicalDeploymentError('local_model_fetch_artifact_invalid')
        self.requests.append(str(relative))
        return resolved, True


def _balanced_layer_ranges(
    num_layers: int, node_count: int
) -> tuple[dict[str, int], ...]:
    """Split layers into node_count contiguous half-open ranges, largest first."""

    if node_count < 2 or num_layers < node_count:
        raise PhysicalDeploymentError("invalid_physical_layer_split")
    base, remainder = divmod(num_layers, node_count)
    ranges: list[dict[str, int]] = []
    start = 0
    for index in range(node_count):
        size = base + (1 if index < remainder else 0)
        ranges.append(
            {
                "start_layer": start,
                "end_layer_exclusive": start + size,
                "layer_count": size,
            }
        )
        start += size
    return tuple(ranges)


def _route_with_nodes(
    manifest: dict[str, Any], node_ids: tuple[str, ...]
) -> dict[str, Any]:
    ranges = _balanced_layer_ranges(manifest["num_layers"], len(node_ids))
    route = _route_for_manifest(manifest)
    route["route"] = [
        {"node_id": node_id, "range": copy.deepcopy(layer_range)}
        for node_id, layer_range in zip(node_ids, ranges)
    ]
    route["node_order"] = list(node_ids)
    return route


def prepare_assignment_artifacts(
    root: Path,
    *,
    node_ids: tuple[str, ...] = ("node-a", "node-b"),
    model_source: LocalModelSource | None = None,
    runtime_dtype: str = "float32",
) -> _PreparedAssignments:
    """Build, compile, provision, and verify exact offline assignments."""

    _validate_nodes(node_ids)
    runtime_dtype = _validate_runtime_dtype(runtime_dtype)
    prepared_root = _prepare_root(Path(root))
    try:
        source_metadata: dict[str, Any] | None = None
        tokenizer_assets: tuple[dict[str, Any], ...] = ()
        if model_source is None:
            manifest, _ = _build_local_model(prepared_root, n_positions=16)
            fetcher: Any = _LocalOnlyFetcher(prepared_root)
        else:
            resolved_source = _resolve_local_model_source(model_source)
            prepared_source = _materialize_local_model_source(
                resolved_source,
                prepared_root,
            )
            manifest = prepared_source.manifest
            source_metadata = prepared_source.source_metadata
            tokenizer_assets = prepared_source.tokenizer_assets
            fetcher = _IdentityAwareLocalFetcher(
                prepared_root,
                model_id=manifest["model_id"],
                resolved_commit=manifest["resolved_commit"],
            )
        route = _route_with_nodes(manifest, node_ids)
        assignments = compile_layer_assignments(
            route_plan=route,
            manifest=manifest,
            deployment_id=DEPLOYMENT_ID,
            deployment_epoch=DEPLOYMENT_EPOCH,
            cache_roots={node: str(prepared_root) for node in route["node_order"]},
            runtime_by_node={
                node: {
                    "backend": "mlx",
                    "dtype": runtime_dtype,
                    "quantization": "none",
                }
                for node in route["node_order"]
            },
            control_plane_binding=_control_plane_binding(),
        )
        if len(assignments) != len(node_ids):
            raise PhysicalDeploymentError(
                "compiled route does not contain one assignment per node"
            )
        reports = [
            provision_assignment(
                assignment,
                fetch_file=fetcher,
                local_files_only=True,
            )
            for assignment in assignments
        ]
        for assignment, report in zip(assignments, reports):
            errors = artifact_report_errors(assignment, report)
            if errors:
                raise PhysicalDeploymentError(
                    "artifact verification report failed: " + "; ".join(errors)
                )
    except PhysicalDeploymentError:
        raise
    except BaseException as exc:
        raise PhysicalDeploymentError(
            f"physical_deployment_prepare_failed:{type(exc).__name__}:{exc}"
        ) from exc
    return _PreparedAssignments(
        manifest=manifest,
        route=route,
        assignments=assignments,
        reports=reports,
        fetcher=fetcher,
        source_metadata=source_metadata,
        tokenizer_assets=tokenizer_assets,
    )


def prepare_monolithic_reference(
    root: Path,
    manifest: dict[str, Any],
    fetcher: Any,
    *,
    runtime_dtype: str = "float32",
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Compile and provision an independent full-model MLX reference stage."""

    runtime_dtype = _validate_runtime_dtype(runtime_dtype)
    reference_node = "reference-node"
    route = {
        **_route_for_manifest(manifest),
        "route": [
            {
                "node_id": reference_node,
                "range": {
                    "start_layer": 0,
                    "end_layer_exclusive": manifest["num_layers"],
                    "layer_count": manifest["num_layers"],
                },
            }
        ],
        "node_order": [reference_node],
    }
    assignments = compile_layer_assignments(
        route_plan=route,
        manifest=manifest,
        deployment_id=DEPLOYMENT_ID,
        deployment_epoch=DEPLOYMENT_EPOCH,
        cache_roots={reference_node: str(Path(root).resolve(strict=True))},
        runtime_by_node={
            reference_node: {
                "backend": "mlx",
                "dtype": runtime_dtype,
                "quantization": "none",
            }
        },
        control_plane_binding=_control_plane_binding(),
    )
    if len(assignments) != 1:
        raise PhysicalDeploymentError(
            "monolithic reference did not compile one assignment"
        )
    assignment = assignments[0]
    report = provision_assignment(
        assignment,
        fetch_file=fetcher,
        local_files_only=True,
    )
    errors = artifact_report_errors(assignment, report)
    if errors:
        raise PhysicalDeploymentError(
            "monolithic reference artifact verification failed: " + "; ".join(errors)
        )
    return assignment, report


def prepare_physical_deployment(
    root: Path,
    *,
    node_ids: tuple[str, ...] = ("node-a", "node-b"),
    model_source: LocalModelSource | None = None,
    runtime_dtype: str = "float32",
) -> PhysicalDeployment:
    """Prepare exact stage assignments plus an independent reference stage."""

    prepared = prepare_assignment_artifacts(
        root,
        node_ids=node_ids,
        model_source=model_source,
        runtime_dtype=runtime_dtype,
    )
    try:
        reference_assignment, reference_report = prepare_monolithic_reference(
            root,
            prepared.manifest,
            prepared.fetcher,
            runtime_dtype=runtime_dtype,
        )
    except PhysicalDeploymentError:
        raise
    except BaseException as exc:
        raise PhysicalDeploymentError(
            f"physical_reference_prepare_failed:{type(exc).__name__}:{exc}"
        ) from exc
    artifact_digests = tuple(
        (
            item["path"],
            f"{item['content_digest']['algorithm']}:{item['content_digest']['value']}",
        )
        for item in prepared.manifest["files"]
    )
    return PhysicalDeployment(
        root=Path(root).resolve(strict=True),
        manifest=prepared.manifest,
        route_plan=prepared.route,
        assignments=tuple(prepared.assignments),
        artifact_reports=tuple(prepared.reports),
        reference_assignment=reference_assignment,
        reference_report=reference_report,
        local_fetch_requests=tuple(prepared.fetcher.requests),
        model_artifact_digests=artifact_digests,
        model_source=prepared.source_metadata,
        tokenizer_assets=prepared.tokenizer_assets,
    )
