"""Offline deterministic model and assignment preparation for physical probes."""
from __future__ import annotations

import copy
from dataclasses import dataclass, replace
import json
from pathlib import Path
from typing import Any, Mapping, Sequence

from layer_assignment import compile_layer_assignments
from model_manifest import manifest_digest_ref
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
from weight_provisioning import artifact_report_errors, provision_assignment


class PhysicalDeploymentError(RuntimeError):
    """Fail-closed deterministic preparation error."""


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
        activation_bytes=4,
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

        return {
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


def _validate_nodes(node_ids: tuple[str, str]) -> None:
    if (
        len(node_ids) != 2
        or any(not isinstance(node, str) or not node.strip() for node in node_ids)
        or len(set(node_ids)) != 2
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


def _route_with_nodes(
    manifest: dict[str, Any], node_ids: tuple[str, str]
) -> dict[str, Any]:
    route = _route_for_manifest(manifest)
    for item, node_id in zip(route["route"], node_ids):
        item["node_id"] = node_id
    route["node_order"] = list(node_ids)
    return route


def prepare_assignment_artifacts(
    root: Path,
    *,
    node_ids: tuple[str, str] = ("node-a", "node-b"),
) -> tuple[
    dict[str, Any],
    dict[str, Any],
    list[dict[str, Any]],
    list[dict[str, Any]],
    _LocalOnlyFetcher,
]:
    """Build, compile, provision, and verify two exact offline assignments."""

    _validate_nodes(node_ids)
    prepared_root = _prepare_root(Path(root))
    try:
        manifest, _ = _build_local_model(prepared_root, n_positions=16)
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
                    "dtype": "float32",
                    "quantization": "none",
                }
                for node in route["node_order"]
            },
            control_plane_binding=_control_plane_binding(),
        )
        if len(assignments) != 2:
            raise PhysicalDeploymentError(
                "compiled route does not contain exactly two assignments"
            )
        fetcher = _LocalOnlyFetcher(prepared_root)
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
    return manifest, route, assignments, reports, fetcher


def prepare_monolithic_reference(
    root: Path,
    manifest: dict[str, Any],
    fetcher: _LocalOnlyFetcher,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Compile and provision an independent full-model MLX reference stage."""

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
                "dtype": "float32",
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
    node_ids: tuple[str, str] = ("node-a", "node-b"),
) -> PhysicalDeployment:
    """Prepare exact stage assignments plus an independent reference stage."""

    manifest, route, assignments, reports, fetcher = prepare_assignment_artifacts(
        root,
        node_ids=node_ids,
    )
    try:
        reference_assignment, reference_report = prepare_monolithic_reference(
            root,
            manifest,
            fetcher,
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
        for item in manifest["files"]
    )
    return PhysicalDeployment(
        root=Path(root).resolve(strict=True),
        manifest=manifest,
        route_plan=route,
        assignments=tuple(assignments),
        artifact_reports=tuple(reports),
        reference_assignment=reference_assignment,
        reference_report=reference_report,
        local_fetch_requests=tuple(fetcher.requests),
        model_artifact_digests=artifact_digests,
    )
