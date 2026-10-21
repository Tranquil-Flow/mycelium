# SPDX-License-Identifier: AGPL-3.0-only
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import mycelium_live.supervisor as supervisor_module
from layer_assignment import assignment_id_for
from mycelium_live.representation import (
    NativeDeploymentProjection,
    NativeRepresentationError,
    native_deployment_projection,
    native_deployment_projection_from_config,
)
from mycelium_live.route import PhysicalLiveRoute, RouteIdentity


ROOT = Path(__file__).resolve().parents[2]
DEPLOYMENT_ID = "12345678-1234-5678-1234-567812345678"
MODEL_ID = "org/model"
REVISION = "a" * 40
MANIFEST_DIGEST = "sha256:" + "9" * 64


def _runtime(quantization: str, *, backend: str = "mlx", dtype: str = "float32") -> dict:
    return {
        "backend": backend,
        "dtype": dtype,
        "quantization": quantization,
        "architecture": "qwen2",
        "model_config": {
            "n_layer": 4,
            "n_embd": 8,
            "n_head": 2,
            "n_kv_head": 1,
            "n_inner": 16,
            "vocab_size": 32,
            "n_positions": 64,
            "rms_norm_epsilon": 1e-6,
            "rope_theta": 1_000_000.0,
            "head_dim": 4,
            "activation_function": "silu",
            "tie_word_embeddings": True,
        },
    }


def _assignment(
    node_id: str,
    start: int,
    end: int,
    quantization: str,
    *,
    dtype: str = "float32",
) -> dict:
    components = ["decoder"]
    if start == 0:
        components.insert(0, "input_embedding")
    if end == 4:
        components.extend(("final_norm", "lm_head"))
    assignment = {
        "protocol": "mycelium.layer_assignment.v2",
        "deployment_id": DEPLOYMENT_ID,
        "deployment_epoch": 1,
        "node_id": node_id,
        "manifest_digest": MANIFEST_DIGEST,
        "model_id": MODEL_ID,
        "resolved_commit": REVISION,
        "range": {
            "start_layer": start,
            "end_layer_exclusive": end,
            "layer_count": end - start,
        },
        "components": components,
        "component_tensor_keys": {component: [f"{component}.weight"] for component in components},
        "component_aliases": {},
        "expected_tensor_prefixes": [f"model.layers.{start}."],
        "expected_tensor_keys": [f"model.layers.{start}.weight"],
        "files": [
            {
                "path": "model.safetensors",
                "size_bytes": 1,
                "content_digest": "sha256:" + "1" * 64,
            }
        ],
        "artifact_cache_root": f"/opt/mycelium/{node_id}/artifacts",
        "runtime": _runtime(quantization, dtype=dtype),
        "route_ready": False,
        "claim_boundary": "fixture assignment authority only",
    }
    assignment["assignment_id"] = assignment_id_for(assignment)
    return assignment


def _authority(
    tmp_path: Path,
    quantizations: tuple[str, str] = ("none", "none"),
    *,
    dtypes: tuple[str, str] = ("float32", "float32"),
) -> tuple[SimpleNamespace, Path]:
    source = tmp_path / "source"
    control = source / "control"
    control.mkdir(parents=True)
    assignments = (
        _assignment("node-a", 0, 2, quantizations[0], dtype=dtypes[0]),
        _assignment("node-b", 2, 4, quantizations[1], dtype=dtypes[1]),
    )
    records = []
    assignment_paths = []
    for assignment in assignments:
        relative = f"control/{assignment['node_id']}-assignment.json"
        payload = json.dumps(assignment, sort_keys=True, separators=(",", ":")).encode()
        (source / relative).write_bytes(payload)
        records.append(
            {
                "path": relative,
                "size_bytes": len(payload),
                "content_digest": "sha256:" + hashlib.sha256(payload).hexdigest(),
            }
        )
        assignment_paths.append(relative)

    graph = json.loads(
        (ROOT / "contracts/compatibility-fixtures/execution-graph-v1.json").read_text()
    )
    graph["manifest_digest"] = MANIFEST_DIGEST
    for stage, assignment in zip(graph["stages"], assignments, strict=True):
        stage["layer_range"] = stage.pop("range")
        stage["placements"][0]["assignment_id"] = assignment["assignment_id"]
    config = SimpleNamespace(
        controller={
            "source_root": str(source),
            "transfer_manifest": {
                "protocol": "mycelium.controller_transfer_manifest.v1",
                "files": records,
            },
            "run_plan": {
                "nodes": [
                    {
                        "node_id": assignment["node_id"],
                        "configure": {
                            "assignment_file": relative,
                            "graph": graph,
                        },
                    }
                    for assignment, relative in zip(assignments, assignment_paths, strict=True)
                ]
            },
        }
    )
    return config, source


@pytest.mark.parametrize("quantization", ("none", "int8-weight-only"))
def test_projection_uses_matching_assignment_runtime_authority(
    tmp_path: Path, quantization: str
) -> None:
    config, _source = _authority(tmp_path, (quantization, quantization))

    projection = native_deployment_projection_from_config(config)

    assert projection.quantization == quantization
    assert projection.runtime_dtype == "float32"
    assert projection.topology_size == 2


@pytest.mark.parametrize(
    ("quantizations", "dtypes"),
    [
        (("none", "int8-weight-only"), ("float32", "float32")),
        (("none", "none"), ("float16", "float32")),
    ],
)
def test_projection_fails_closed_on_conflicting_assignment_representations(
    tmp_path: Path,
    quantizations: tuple[str, str],
    dtypes: tuple[str, str],
) -> None:
    config, _source = _authority(tmp_path, quantizations, dtypes=dtypes)

    with pytest.raises(
        NativeRepresentationError,
        match="native_assignment_representation_conflict",
    ):
        native_deployment_projection_from_config(config)


def test_projection_fails_closed_when_assignment_evidence_is_unavailable(
    tmp_path: Path,
) -> None:
    config, source = _authority(tmp_path)
    (source / "control/node-b-assignment.json").unlink()

    with pytest.raises(
        NativeRepresentationError,
        match="native_assignment_evidence_unavailable",
    ):
        native_deployment_projection_from_config(config)


def test_projection_fails_closed_when_assignment_bytes_are_not_manifest_bound(
    tmp_path: Path,
) -> None:
    config, source = _authority(tmp_path)
    path = source / "control/node-b-assignment.json"
    path.write_bytes(path.read_bytes() + b" ")

    with pytest.raises(
        NativeRepresentationError,
        match="native_assignment_evidence_mismatch",
    ):
        native_deployment_projection_from_config(config)


def test_operator_plan_projection_uses_strict_plan_loader(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, _source = _authority(tmp_path)
    observed = []

    def load(path: Path) -> SimpleNamespace:
        observed.append(path)
        return config

    monkeypatch.setattr("mycelium_live.representation.load_operator_plan", load)

    projection = native_deployment_projection(tmp_path / "operator-plan.json")

    assert observed == [tmp_path / "operator-plan.json"]
    assert projection.quantization == "none"


def test_qualified_runtime_projects_authorized_non_int8_representation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    graph = SimpleNamespace(
        deployment_id=DEPLOYMENT_ID,
        model_id=MODEL_ID,
        resolved_commit=REVISION,
        stages=(
            SimpleNamespace(placements=(SimpleNamespace(node_id="node-a"),)),
            SimpleNamespace(placements=(SimpleNamespace(node_id="node-b"),)),
        ),
    )
    projection = NativeDeploymentProjection(
        deployment_id=DEPLOYMENT_ID,
        model_id=MODEL_ID,
        model_revision=REVISION,
        topology_size=2,
        runtime_dtype="float32",
        quantization="none",
    )
    qualification = SimpleNamespace(
        deployment_id=DEPLOYMENT_ID,
        model_id=MODEL_ID,
        issued_at_unix_ms=1234,
        route_ready=True,
    )

    class Route:
        execution_graph = graph
        startup_challenge = ((1,), (2,))

        def __init__(self) -> None:
            self.quantization = None

        def open(self) -> RouteIdentity:
            return RouteIdentity(DEPLOYMENT_ID, MODEL_ID, REVISION, ("endpoint-a", "endpoint-b"))

        def set_deployment_qualification(self, value, *, quantization: str) -> None:
            assert value is qualification
            self.quantization = quantization

        def set_stop_token_ids(self, _values) -> None:
            return None

        def close(self) -> None:
            return None

    route = Route()
    monkeypatch.setattr(
        supervisor_module,
        "native_deployment_projection",
        lambda _plan: projection,
    )
    monkeypatch.setattr(
        supervisor_module.PhysicalLiveRoute,
        "from_operator_plan",
        lambda *_args, **_kwargs: route,
    )
    monkeypatch.setattr(supervisor_module, "_qualify_open_route", lambda _route: qualification)
    monkeypatch.setattr(supervisor_module, "prompt_codec_from_deployment", lambda _path: SimpleNamespace())
    monkeypatch.setattr(supervisor_module, "_deployment_from_plan", lambda _path: tmp_path)
    for name in (
        "_placement_projection",
        "_topology_projection",
        "_workload_comparison",
        "_m16_performance_budget",
        "_m17_model_operation",
        "_m18_replica_plan",
    ):
        monkeypatch.setattr(supervisor_module, name, lambda *_args, **_kwargs: None)
    monkeypatch.setattr(supervisor_module, "_historical_evidence", lambda *_args, **_kwargs: ())

    runtime = supervisor_module._qualified_runtime(
        tmp_path / "operator-plan.json",
        seed_state_root=tmp_path / "seed",
    )

    assert runtime.quantization == "none"
    assert route.quantization == "none"


def test_registry_unavailable_entry_retains_plan_authorized_representation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    projection = NativeDeploymentProjection(
        deployment_id=DEPLOYMENT_ID,
        model_id=MODEL_ID,
        model_revision=REVISION,
        topology_size=2,
        runtime_dtype="float32",
        quantization="none",
    )
    captured = {}

    class StopAfterRegistry(RuntimeError):
        pass

    def registry(_runtimes, *, unavailable_deployments, **_kwargs):
        captured["unavailable"] = unavailable_deployments
        raise StopAfterRegistry

    monkeypatch.setattr(
        supervisor_module,
        "native_deployment_projection",
        lambda _plan: projection,
    )
    monkeypatch.setattr(
        supervisor_module,
        "_qualified_runtime",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("node_command_rejected")),
    )
    monkeypatch.setattr(supervisor_module, "LiveDeploymentRegistry", registry)

    with pytest.raises(StopAfterRegistry):
        supervisor_module.run_registry_server(
            operator_plans=(tmp_path / "operator-plan.json",),
            host="127.0.0.1",
            port=0,
            seed_state_root=tmp_path / "seed",
        )

    unavailable = captured["unavailable"]
    assert len(unavailable) == 1
    assert unavailable[0].quantization == "none"


def test_route_qualification_requires_explicit_representation() -> None:
    route = object.__new__(PhysicalLiveRoute)
    with pytest.raises(TypeError):
        route.set_deployment_qualification(SimpleNamespace())
