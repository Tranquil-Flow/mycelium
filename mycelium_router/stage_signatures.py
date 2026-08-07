"""Dependency-light signatures for assignment-bound runtime stages."""

from __future__ import annotations

import hashlib
import json

from mycelium_router.contracts import ExecutionGraph, Stage


def stage_signature_for_backend(
    graph: ExecutionGraph,
    stage: Stage,
    runtime_backend: str,
) -> str:
    """Bind stage identity and its serving backend into one stable digest."""

    if (
        not isinstance(runtime_backend, str)
        or not runtime_backend
        or runtime_backend != runtime_backend.strip()
    ):
        raise ValueError("invalid_runtime_backend")
    material = {
        "protocol": "mycelium.stage_signature.v2",
        "deployment_id": graph.deployment_id,
        "deployment_epoch": graph.deployment_epoch,
        "model_id": graph.model_id,
        "resolved_commit": graph.resolved_commit,
        "manifest_digest": graph.manifest_digest,
        "stage_id": stage.stage_id,
        "range": {
            "start_layer": stage.layer_range.start_layer,
            "end_layer_exclusive": stage.layer_range.end_layer_exclusive,
            "layer_count": stage.layer_range.layer_count,
        },
        "components": list(stage.component_roles),
        "runtime_backend": runtime_backend,
        "hidden_size": graph.hidden_size,
        "dtype_bytes": graph.activation_bytes,
    }
    encoded = json.dumps(
        material,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


__all__ = ["stage_signature_for_backend"]
