from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import mlx.core as mx

import model_manifest
from mycelium_qualification.physical_deployment import (
    PhysicalDeploymentError,
    build_execution_graph,
    build_physical_device_states,
    prepare_physical_deployment,
)
from runtime_loader import execute_loaded_stage, load_assignment_stage
import weight_provisioning


EXPECTED_RANGES = (
    {"start_layer": 0, "end_layer_exclusive": 1, "layer_count": 1},
    {"start_layer": 1, "end_layer_exclusive": 2, "layer_count": 1},
)


def test_prepare_physical_deployment_is_offline_deterministic_and_exact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def network_forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError(f"network called: {args!r} {kwargs!r}")

    monkeypatch.setattr(
        model_manifest, "resolve_huggingface_manifest", network_forbidden
    )
    monkeypatch.setattr(
        weight_provisioning, "fetch_huggingface_file", network_forbidden
    )

    first = prepare_physical_deployment(tmp_path / "first")
    second = prepare_physical_deployment(tmp_path / "second")

    assert first.route_ready is False
    assert first.network_downloads == 0
    assert first.manifest == second.manifest
    assert first.model_artifact_digests == second.model_artifact_digests
    assert tuple(item["range"] for item in first.assignments) == EXPECTED_RANGES
    assert tuple(item["node_id"] for item in first.assignments) == (
        "node-a",
        "node-b",
    )
    assert len({item["assignment_id"] for item in first.assignments}) == 2
    assert first.reference_assignment["range"] == {
        "start_layer": 0,
        "end_layer_exclusive": 2,
        "layer_count": 2,
    }
    assert first.reference_assignment["node_id"] == "reference-node"
    assert first.reference_assignment["assignment_id"] not in {
        item["assignment_id"] for item in first.assignments
    }
    assert first.local_fetch_requests
    assert all(not Path(item).is_absolute() for item in first.local_fetch_requests)
    json.dumps(first.evidence_document(), sort_keys=True, allow_nan=False)


def test_prepared_assignments_and_independent_reference_load_and_execute(
    tmp_path: Path,
) -> None:
    deployment = prepare_physical_deployment(tmp_path / "deployment")

    loaded_stages = [
        load_assignment_stage(assignment, report, load_generation=17)
        for assignment, report in zip(
            deployment.assignments,
            deployment.artifact_reports,
        )
    ]
    loaded_reference = load_assignment_stage(
        deployment.reference_assignment,
        deployment.reference_report,
        load_generation=17,
    )

    assert [stage.proof["loaded_range"] for stage in loaded_stages] == list(
        EXPECTED_RANGES
    )
    assert loaded_reference.proof["loaded_range"] == {
        "start_layer": 0,
        "end_layer_exclusive": 2,
        "layer_count": 2,
    }
    reference_result = execute_loaded_stage(
        loaded_reference,
        token_ids=mx.array(((1, 2, 3),), dtype=mx.uint32),
    )
    mx.eval(reference_result)
    assert tuple(reference_result.shape) == (1, 3, 7)
    assert int(mx.argmax(reference_result[0, -1, :]).item()) in range(7)

    graph = build_execution_graph(
        deployment.assignments,
        [stage.proof for stage in loaded_stages],
        link_scheme="iroh",
    )
    assert [stage.placements[0].node_id for stage in graph.stages] == [
        "node-a",
        "node-b",
    ]
    assert graph.edges[0].link_id == "iroh:node-a->node-b"
    states = build_physical_device_states(graph)
    assert set(states) == {"node-a", "node-b"}
    assert all(state.availability == "ALIVE" for state in states.values())


def test_prepare_physical_deployment_rejects_reuse_and_bad_nodes(
    tmp_path: Path,
) -> None:
    occupied = tmp_path / "occupied"
    occupied.mkdir()
    (occupied / "existing").write_bytes(b"do not replace")

    with pytest.raises(PhysicalDeploymentError, match="deployment_root_not_empty"):
        prepare_physical_deployment(occupied)
    assert (occupied / "existing").read_bytes() == b"do not replace"

    with pytest.raises(PhysicalDeploymentError, match="invalid_physical_nodes"):
        prepare_physical_deployment(tmp_path / "bad-nodes", node_ids=("same", "same"))
    assert not (tmp_path / "bad-nodes").exists()
