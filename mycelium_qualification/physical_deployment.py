"""Offline deterministic model and assignment preparation for physical probes."""
from __future__ import annotations

import copy
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from layer_assignment import compile_layer_assignments
from model_manifest import manifest_digest_ref
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
    """Fail-closed deterministic deployment preparation error."""


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
