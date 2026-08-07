"""Dependency-light signatures for assignment-bound runtime stages."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from typing import Any

from mycelium_router.contracts import ExecutionGraph, Stage


def _stage_signature(
    *,
    deployment_id: Any,
    deployment_epoch: Any,
    model_id: Any,
    resolved_commit: Any,
    manifest_digest: Any,
    stage_id: Any,
    layer_range: Mapping[str, Any],
    component_roles: Sequence[Any],
    runtime_backend: str,
    hidden_size: Any,
    dtype_bytes: Any,
) -> str:
    if (
        not isinstance(runtime_backend, str)
        or not runtime_backend
        or runtime_backend != runtime_backend.strip()
    ):
        raise ValueError("invalid_runtime_backend")
    material = {
        "protocol": "mycelium.stage_signature.v2",
        "deployment_id": deployment_id,
        "deployment_epoch": deployment_epoch,
        "model_id": model_id,
        "resolved_commit": resolved_commit,
        "manifest_digest": manifest_digest,
        "stage_id": stage_id,
        "range": {
            "start_layer": layer_range["start_layer"],
            "end_layer_exclusive": layer_range["end_layer_exclusive"],
            "layer_count": layer_range["layer_count"],
        },
        "components": list(component_roles),
        "runtime_backend": runtime_backend,
        "hidden_size": hidden_size,
        "dtype_bytes": dtype_bytes,
    }
    encoded = json.dumps(
        material,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def stage_signature_for_assignment(
    assignment: Mapping[str, Any],
    stage_id: str,
    model: Mapping[str, Any],
    runtime_backend: str,
) -> str:
    """Build the canonical backend-bound signature before graph construction."""

    if not isinstance(assignment, Mapping) or not isinstance(model, Mapping):
        raise ValueError("invalid_stage_signature_inputs")
    layer_range = assignment.get("range")
    component_roles = assignment.get("components")
    if not isinstance(layer_range, Mapping) or not isinstance(
        component_roles, (list, tuple)
    ):
        raise ValueError("invalid_stage_signature_inputs")
    try:
        return _stage_signature(
            deployment_id=assignment["deployment_id"],
            deployment_epoch=assignment["deployment_epoch"],
            model_id=assignment["model_id"],
            resolved_commit=assignment["resolved_commit"],
            manifest_digest=assignment["manifest_digest"],
            stage_id=stage_id,
            layer_range=layer_range,
            component_roles=component_roles,
            runtime_backend=runtime_backend,
            hidden_size=model["hidden_size"],
            dtype_bytes=model["dtype_bytes"],
        )
    except (KeyError, TypeError) as exc:
        raise ValueError("invalid_stage_signature_inputs") from exc


def stage_signature_for_backend(
    graph: ExecutionGraph,
    stage: Stage,
    runtime_backend: str,
) -> str:
    """Bind graph stage identity and its serving backend into one stable digest."""

    return _stage_signature(
        deployment_id=graph.deployment_id,
        deployment_epoch=graph.deployment_epoch,
        model_id=graph.model_id,
        resolved_commit=graph.resolved_commit,
        manifest_digest=graph.manifest_digest,
        stage_id=stage.stage_id,
        layer_range={
            "start_layer": stage.layer_range.start_layer,
            "end_layer_exclusive": stage.layer_range.end_layer_exclusive,
            "layer_count": stage.layer_range.layer_count,
        },
        component_roles=stage.component_roles,
        runtime_backend=runtime_backend,
        hidden_size=graph.hidden_size,
        dtype_bytes=graph.activation_bytes,
    )


__all__ = ["stage_signature_for_assignment", "stage_signature_for_backend"]
