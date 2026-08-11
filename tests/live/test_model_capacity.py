from __future__ import annotations

from pathlib import Path
import threading

import pytest

from mycelium_live.model_capacity import (
    ModelCapacityRefresh,
    ModelCapacityRefreshError,
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
        "placement": {"nodes": []},
        "topology": {"decision": {"opened_order": []}},
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
