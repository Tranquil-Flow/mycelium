# SPDX-License-Identifier: AGPL-3.0-only
"""Tiny actual-MLX composition checks; not a physical qualification campaign."""
import threading

import numpy as np
import pytest

from mycelium_router import mlx_runtime as module
from mycelium_router.a10_live_publication import validate_publication
from mycelium_router.payloads import encode_token_ids
from test_router_mlx_runtime import _run_two_stage, _work_item, runtime_case  # noqa: F401


def ports_for(case, mode):
    return tuple(module.MLXRuntimePort(
        assignment["node_id"], case.graph,
        {stage.placements[0].placement_id: loaded}, materialization=mode,
    ) for assignment, stage, loaded in zip(case.assignments, case.graph.stages, case.loaded))


def test_modes_preserve_actual_mlx_logits_tokens_and_stage_a10_intervals(runtime_case, monkeypatch):
    case = runtime_case
    runs = []
    for mode in ("per_layer", "per_stage"):
        ports = ports_for(case, mode)
        logits, results = [], []
        original = ports[-1]._execute_stage_with_kv

        def capture(**kwargs):
            output, layers = original(**kwargs)
            logits.append(np.asarray(output).tobytes())
            return output, layers

        monkeypatch.setattr(ports[-1], "_execute_stage_with_kv", capture)
        ids = (1, 2, 3)
        for step in range(5):
            result = _run_two_stage(case, ids, phase="PREFILL" if step == 0 else "DECODE",
                                   path_id=f"parity-{mode}", token_index=-1 if step == 0 else step,
                                   position=0 if step == 0 else step + 2, ports=ports)
            results.append(result)
            ids = (result[1],)
        runs.append((logits, results))
        for port in ports:
            publication = port.a10_publication_snapshot()
            assert validate_publication(publication) == publication
            compute = [i for i in publication["intervals"] if i["hop_kind"] == "compute"]
            assert len(compute) == 5  # still one per stage call, not per layer
            assert all(i["finished_at"] >= i["started_at"] for i in compute)
            snapshot = port.kv_snapshot()
            assert snapshot["materialization"] == mode
            if mode == "per_stage":
                assert snapshot["observed_work_unit_count"] == 5
            assert snapshot["maximum_observed_work_unit_ms"] > 0
            port.cancel(f"parity-{mode}")
            assert port.kv_snapshot()["active_state_count"] == 0
    assert runs[0] == runs[1]


def test_cancel_api_during_stage_eval_never_commits_kv_or_output(runtime_case, monkeypatch):
    case = runtime_case
    port = ports_for(case, "per_stage")[0]
    entered, release = threading.Event(), threading.Event()
    real_eval = module.mx.eval
    eval_count = []

    def held_eval(*arrays):
        real_eval(*arrays)
        eval_count.append(1)
        entered.set()
        assert release.wait(5)

    monkeypatch.setattr(module.mx, "eval", held_eval)
    results = []
    item = _work_item(case, 0, encode_token_ids((1, 2, 3)), path_id="cancel-stage")
    worker = threading.Thread(target=lambda: results.append(port.execute(item)))
    worker.start()
    try:
        assert entered.wait(5)
        port.cancel("cancel-stage")  # the real nonblocking cancellation API
        assert "cancel-stage" in port._cancelled_paths
    finally:
        release.set()
        worker.join(5)
    assert not worker.is_alive()
    assert len(results) == 1
    assert results[0].failure_reason == "path_cancelled"
    assert results[0].payload is None and results[0].token_id is None
    assert len(eval_count) == 1
    snapshot = port.kv_snapshot()
    assert snapshot["observed_work_unit_count"] == 1
    assert snapshot["maximum_observed_work_unit_ms"] > 0
    assert snapshot["active_state_count"] == 0
    assert port.a10_publication_snapshot()["intervals"] == []


def test_mode_is_fixed_for_evidence_lifetime(runtime_case):
    port = ports_for(runtime_case, "per_stage")[0]
    with pytest.raises(AttributeError):
        port.materialization = "per_layer"
