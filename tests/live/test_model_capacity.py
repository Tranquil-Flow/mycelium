from __future__ import annotations

from pathlib import Path
import threading

import pytest

from mycelium_live.model_capacity import (
    ModelCapacityRefresh,
    ModelCapacityRefreshError,
    _capacity_topology,
    live_observations_document,
    recompute_model_operation,
)


def _operation(generation: int = 9) -> dict[str, object]:
    return {
        "protocol": "mycelium.model_operation.v1",
        "operation_digest": "sha256:" + "a" * 64,
        "catalog_generation": generation,
        "feasibility_reports": [{"model_id": "Qwen/example"}],
    }


def test_capacity_refresh_is_single_flight_and_publishes_atomically() -> None:
    entered = threading.Event()
    release = threading.Event()
    published = []

    def evaluate(progress):
        progress("capturing_resources")
        entered.set()
        assert release.wait(timeout=5)
        progress("evaluating_models")
        return _operation()

    refresh = ModelCapacityRefresh(
        evaluator=evaluate,
        operation_sink=published.append,
        clock_unix_ms=lambda: 1_000,
    )

    started = refresh.start()
    assert started["state"] == "refreshing"
    assert entered.wait(timeout=5)
    with pytest.raises(ModelCapacityRefreshError, match="capacity_refresh_busy"):
        refresh.start()
    release.set()
    refresh.close()

    status = refresh.status()
    assert status["state"] == "succeeded"
    assert status["catalog_generation"] == 9
    assert status["evaluated_model_count"] == 1
    assert status["download_authorized"] is False
    assert status["provisioning_started"] is False
    assert published == [_operation()]


def test_capacity_refresh_failure_is_bounded_and_preserves_no_partial_operation() -> None:
    def fail(_progress):
        raise RuntimeError("capacity_evidence_stale")

    published = []
    refresh = ModelCapacityRefresh(evaluator=fail, operation_sink=published.append)
    refresh.start()
    refresh.close()

    assert refresh.status()["state"] == "failed"
    assert refresh.status()["reason_code"] == "capacity_evidence_stale"
    assert published == []


def test_live_capacity_observations_file_is_private_and_closed(tmp_path: Path) -> None:
    source = tmp_path / "live-observations.json"
    source.write_text(
        '{"placement":{},"protocol":"mycelium.live_swarm_resource_observations.v1",'
        '"signed_snapshots":[{}],"topology":{}}',
        encoding="utf-8",
    )
    source.chmod(0o600)

    assert live_observations_document(source)["signed_snapshots"] == [{}]
    source.chmod(0o644)
    with pytest.raises(ValueError, match="member_model_inventory_file_invalid"):
        live_observations_document(source)


def test_capacity_topology_derives_contiguous_m13_stage_order() -> None:
    topology = _capacity_topology(
        {
            "nodes": [
                {"node_id": "node-2", "start_layer": 23, "end_layer_exclusive": 24},
                {"node_id": "node-0", "start_layer": 0, "end_layer_exclusive": 23},
            ]
        },
        None,
    )

    assert topology == {
        "protocol": "mycelium.capacity_route_order.v1",
        "source": "validated_m13_placement_stage_order",
        "decision": {"opened_order": ["node-0", "node-2"]},
        "route_ready": False,
    }


@pytest.mark.parametrize(
    "nodes",
    [
        [
            {"node_id": "node-0", "start_layer": 0, "end_layer_exclusive": 12},
            {"node_id": "node-2", "start_layer": 13, "end_layer_exclusive": 24},
        ],
        [
            {"node_id": "node-0", "start_layer": 0, "end_layer_exclusive": 12},
            {"node_id": "node-0", "start_layer": 12, "end_layer_exclusive": 24},
        ],
    ],
)
def test_capacity_topology_rejects_ambiguous_stage_order(nodes) -> None:
    with pytest.raises(ValueError, match="capacity_topology_order_invalid"):
        _capacity_topology({"nodes": nodes}, None)


def test_recompute_joins_fresh_evidence_local_catalog_and_existing_planner(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    compatible = type("Entry", (), {"compatible": True})()
    unsupported = type("Entry", (), {"compatible": False})()
    evidence = type("Evidence", (), {"generation": 77})()
    nodes = (object(),)
    reports = []
    phases = []
    live = {
        "placement": {
            "nodes": [
                {"node_id": "node-0", "start_layer": 0, "end_layer_exclusive": 1}
            ]
        },
        "topology": {"decision": {"opened_order": ["node-0"]}},
    }

    monkeypatch.setattr("mycelium_live.model_capacity.assemble", lambda value: {"assembled": value})
    monkeypatch.setattr("mycelium_live.model_capacity.swarm_feasibility_evidence_from_document", lambda _value: evidence)
    monkeypatch.setattr("mycelium_live.model_capacity.scan_huggingface_cache", lambda root: (compatible, unsupported) if root == tmp_path else ())
    monkeypatch.setattr("mycelium_live.model_capacity._node_capabilities", lambda *_args, **_kwargs: nodes)
    monkeypatch.setattr("mycelium_live.model_capacity.catalog_document", lambda entries, generation: {"protocol": "mycelium.model_catalog.v1", "entries": list(entries), "generation": generation})

    def evaluate(entry, **kwargs):
        reports.append((entry, kwargs))
        return {"protocol": "mycelium.model_feasibility.v1", "model_id": "Qwen/example"}

    monkeypatch.setattr("mycelium_live.model_capacity.evaluate_model_feasibility", evaluate)
    monkeypatch.setattr("mycelium_live.model_capacity.model_operation_document", lambda catalog, values: {**_operation(77), "catalog": catalog, "feasibility_reports": list(values)})

    operation = recompute_model_operation(
        cache_root=tmp_path,
        live_observations=live,
        evaluated_at_unix_ms=1_000,
        progress=phases.append,
    )

    assert operation["catalog_generation"] == 77
    assert len(reports) == 1
    assert reports[0][0] is compatible
    assert reports[0][1]["ordered_nodes"] == nodes
    assert reports[0][1]["evidence"] is evidence
    assert phases == [
        "capturing_resources",
        "scanning_local_models",
        "evaluating_models",
        "publishing",
    ]
