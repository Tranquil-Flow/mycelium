# SPDX-License-Identifier: AGPL-3.0-only
"""Desk-only synchronization/cancellation tests: all MLX execution is replaced.

Arithmetic here is a deterministic NumPy stand-in. The cached-model benchmark
separately establishes real MLX logit/token bit parity.
"""
from collections import OrderedDict
from types import SimpleNamespace
import threading

import numpy as np
import pytest

from mycelium_router import mlx_runtime as module
from mycelium_router.contracts import LayerRange, Stage, StageCost


@pytest.fixture
def desk(monkeypatch):
    evaluations = []
    fake = SimpleNamespace(eval=lambda *arrays: evaluations.append(arrays),
                           all=np.all, isfinite=np.isfinite)
    monkeypatch.setattr(module, "mx", fake)
    calls = []

    def block(hidden, tensors, prefix, config, position, past, *args, **kwargs):
        calls.append(prefix)
        value = hidden * np.float32(1.25) + np.float32(position + 1)
        return value, (value.copy(), value.copy())

    monkeypatch.setattr(module, "_qwen2_block_with_kv", block)
    monkeypatch.setattr(module, "_qwen2_embedding", lambda weight, ids: weight[ids])
    monkeypatch.setattr(module, "_rms_norm", lambda x, *args: x * np.float32(.5))
    monkeypatch.setattr(module, "_qwen2_linear", lambda x, w: x @ w.T)
    stage = Stage(stage_id="stage-000", layer_range=LayerRange(0, 3, 3),
                  component_roles=("input_embedding", "decoder", "final_norm", "lm_head"),
                  stage_cost=StageCost(1., 1., 1), placements=())
    tensors = {"model.embed_tokens.weight": np.arange(32, dtype=np.float32).reshape(8, 4),
               "model.norm.weight": np.ones(4), "lm_head.weight": np.eye(4, dtype=np.float32)}
    loaded = SimpleNamespace(tensors=tensors, resolved_aliases={})

    def port(mode):
        p = object.__new__(module.MLXRuntimePort)
        p._materialization = mode
        p._weights_resident = True
        p._execution_tensors = {}
        p._observed_work_unit_count = 0
        p._maximum_observed_work_unit_ms = 0.
        p._cancellation_lock = threading.RLock()
        p._cancelled_paths = OrderedDict()
        return p

    def execute(p, position=0, past=None):
        return p._execute_stage_with_kv(
            stage=stage, loaded=loaded,
            runtime={"architecture": "qwen2", "model_config": {"rms_norm_epsilon": 1e-6}},
            token_ids=np.array([[1]], dtype=np.uint32), hidden_states=None,
            position=position, past_layers=past or {}, path_id="desk",
        )
    return SimpleNamespace(port=port, execute=execute, calls=calls, evaluations=evaluations, fake=fake)


@pytest.mark.parametrize("mode,expected_evals,expected_units", [("per_stage", 1, 1), ("per_layer", 6, 5)])
def test_one_materialization_per_stage_per_token(desk, mode, expected_evals, expected_units):
    p = desk.port(mode)
    past = {}
    for position in range(3):
        before = len(desk.evaluations)
        _, past = desk.execute(p, position, past)
        assert len(desk.evaluations) - before == expected_evals
    assert p._observed_work_unit_count == expected_units * 3
    assert p._maximum_observed_work_unit_ms > 0


def test_stage_and_layer_have_identical_logits_tokens_and_kv(desk):
    outputs = []
    for mode in ("per_layer", "per_stage"):
        p, past, rows = desk.port(mode), {}, []
        for position in range(4):
            logits, past = desk.execute(p, position, past)
            rows.append((logits.tobytes(), int(np.argmax(logits)),
                         [x.tobytes() for pair in past.values() for x in pair]))
        outputs.append(rows)
    assert outputs[0] == outputs[1]


@pytest.mark.parametrize("mode,expected_layers", [("per_stage", 3), ("per_layer", 1)])
def test_cancellation_during_materialization_stops_before_result_release(desk, mode, expected_layers):
    p = desk.port(mode)
    entered, release = threading.Event(), threading.Event()
    original = desk.fake.eval

    def held(*arrays):
        original(*arrays)
        entered.set()
        assert release.wait(5)

    desk.fake.eval = held
    errors, results = [], []

    def worker():
        try:
            results.append(desk.execute(p))
        except BaseException as error:
            errors.append(error)

    thread = threading.Thread(target=worker)
    thread.start()
    try:
        assert entered.wait(5)
        # Same cancellation fence checked by production _checkpoint_path; no GPU.
        with p._cancellation_lock:
            p._cancelled_paths["desk"] = None
    finally:
        release.set()
        thread.join(5)
    assert not thread.is_alive()
    assert not results
    assert len(errors) == 1 and errors[0].code == "path_cancelled"
    assert len(desk.calls) == expected_layers
    assert p._observed_work_unit_count == 1
    assert p._maximum_observed_work_unit_ms > 0


def test_precancelled_stage_does_not_build_or_materialize(desk):
    p = desk.port("per_stage")
    p._cancelled_paths["desk"] = None
    with pytest.raises(module.MLXRuntimeError, match="path_cancelled"):
        desk.execute(p)
    assert desk.calls == []
    assert desk.evaluations == []


def test_invalid_materialization_refused_before_model_binding():
    with pytest.raises(module.MLXRuntimeError, match="invalid_runtime_materialization"):
        module.MLXRuntimePort("desk", None, {}, materialization="automatic")
