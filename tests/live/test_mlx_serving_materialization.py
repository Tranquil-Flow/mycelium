# SPDX-License-Identifier: AGPL-3.0-only
"""Serving factory and signed snapshot projection, with no model/device work."""
from types import SimpleNamespace

import pytest

import physical_inference_node as node
from mycelium_router import mlx_runtime
from mycelium_live.route import _public_interruptibility


@pytest.mark.parametrize("change", [
    {"work_unit": "transformer_stage", "materialization": "per_layer"},
    {"work_unit": "transformer_layer", "materialization": "per_stage"},
    {"work_unit": "transformer_stage", "materialization": "automatic"},
    {"work_unit": "transformer_stage", "materialization": None},
    {"maximum_observed_work_unit_ms": 2000},
    {"observed_work_unit_count": 0},
])
def test_public_projection_refuses_mislabeled_or_unbounded_stage(change):
    evidence = {
        "runtime_backend": "mlx", "decode_mode": "stage_local_kv",
        "work_unit": "transformer_stage", "materialization": "per_stage",
        "maximum_observed_work_unit_ms": 125., "observed_work_unit_count": 1,
        "cooperative_bound_candidate": True,
    }
    assert _public_interruptibility({**evidence, **change}) is None


def test_serving_factory_selects_per_stage(monkeypatch):
    monkeypatch.setenv("MYCELIUM_TOKEN_PATH", "on")
    captured = {}
    monkeypatch.setattr(node, "_route_decode_mode", lambda *args: "stage_local_kv")
    monkeypatch.setattr(mlx_runtime, "MLXRuntimePort", lambda *args, **kwargs: captured.update(kwargs) or captured)
    service = object.__new__(node.PhysicalNodeService)
    service._clock = SimpleNamespace(now=lambda: 0.)
    service.node_id = "desk"
    service.requested_decode_mode = None
    placement = SimpleNamespace(runtime_backend="mlx", placement_id="p")
    result = service._build_runtime_port(placement, SimpleNamespace(), SimpleNamespace(proof={}))
    assert result["materialization"] == "per_stage"


@pytest.mark.parametrize("mode,unit", [("per_layer", "transformer_layer"), ("per_stage", "transformer_stage")])
@pytest.mark.parametrize("observed,candidate", [(125., True), (1999., True), (2000., False), (2001., False), (float("nan"), False), (True, False)])
def test_snapshot_names_mode_and_preserves_cooperative_bound(tmp_path, monkeypatch, mode, unit, observed, candidate):
    monkeypatch.setenv("MYCELIUM_TOKEN_PATH", "on")
    service = object.__new__(node.PhysicalNodeService)
    service.state = "CONFIGURED"
    service.runtime = SimpleNamespace(kv_snapshot=lambda: {
        "mode": "stage_local_kv", "backend": "mlx", "states": {},
        "active_state_count": 0, "active_kv_bytes": 0,
        "materialization": mode, "work_unit": unit,
        "maximum_observed_work_unit_ms": observed, "observed_work_unit_count": 1,
    })
    service.capacity = service.transport = service.sidecar = None
    service.runtime_identity = {"backend": "mlx", "dtype": "float32", "quantization": "int8-weight-only"}
    service.artifact_root = tmp_path
    service._last_cancellation = None
    service._signed_result = lambda event, details=None: {"event": event, "details": details}
    evidence = service._snapshot({})["details"]["interruptibility"]
    assert evidence["work_unit"] == unit
    assert evidence["materialization"] == mode
    assert evidence["cooperative_bound_candidate"] is candidate
    assert evidence["maximum_total_cleanup_ms"] == 4200
    assert evidence["physical_proof_required"] is True
    # The public route must retain the mode, never silently rewrite stage to layer.
    assert _public_interruptibility(evidence) == evidence
