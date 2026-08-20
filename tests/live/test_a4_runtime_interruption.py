from __future__ import annotations

from collections import Counter, OrderedDict
from types import SimpleNamespace
import threading
import time

import numpy as np

import mycelium_router.numpy_runtime as numpy_runtime
from mycelium_router.contracts import LayerRange, Stage, StageCost
from mycelium_router.numpy_runtime import NumpyRouterRuntimeError, NumpyRuntimePort


def test_numpy_stage_cancellation_marks_without_waiting_for_runtime_lock(
    monkeypatch,
) -> None:
    runtime = object.__new__(NumpyRuntimePort)
    runtime._cancellation_lock = threading.RLock()
    runtime._state_lock = threading.RLock()
    runtime._cancelled_paths = OrderedDict()
    runtime._kv_states = {}
    runtime._released_paths = OrderedDict()
    runtime._replays = OrderedDict()
    runtime._release_counts = Counter()
    runtime._last_release_reason = None
    runtime._observed_work_unit_count = 0
    runtime._maximum_observed_work_unit_ms = 0.0

    first_layer_entered = threading.Event()
    release_first_layer = threading.Event()
    calls = []

    def bounded_layer(hidden, *_args):
        calls.append(len(calls))
        first_layer_entered.set()
        assert release_first_layer.wait(timeout=1.0)
        return hidden, (hidden.copy(), hidden.copy())

    monkeypatch.setattr(numpy_runtime, "_qwen2_block_with_kv", bounded_layer)
    stage = Stage(
        stage_id="stage-000",
        layer_range=LayerRange(0, 2, 2),
        component_roles=("decoder",),
        stage_cost=StageCost(1.0, 1.0, 1),
        placements=(),
    )
    errors = []

    def execute() -> None:
        try:
            with runtime._state_lock:
                runtime._execute_stage_with_kv(
                    stage=stage,
                    loaded=SimpleNamespace(tensors={}),
                    runtime={
                        "architecture": "qwen2",
                        "dtype": "float32",
                        "model_config": {},
                    },
                    token_ids=None,
                    hidden_states=np.zeros((1, 1, 2), dtype=np.float32),
                    position=0,
                    past_layers={},
                    path_id="path-cancelled",
                )
        except BaseException as exc:
            errors.append(exc)

    execution = threading.Thread(target=execute)
    execution.start()
    assert first_layer_entered.wait(timeout=1.0)

    cancellation = threading.Thread(target=runtime.cancel, args=("path-cancelled",))
    started = time.monotonic()
    cancellation.start()
    deadline = time.monotonic() + 0.2
    while "path-cancelled" not in runtime._cancelled_paths:
        assert time.monotonic() < deadline
        time.sleep(0.001)
    marked_elapsed = time.monotonic() - started
    release_first_layer.set()
    execution.join(timeout=1.0)
    cancellation.join(timeout=1.0)

    assert marked_elapsed < 0.2
    assert not execution.is_alive()
    assert not cancellation.is_alive()
    assert len(calls) == 1
    assert len(errors) == 1
    assert isinstance(errors[0], NumpyRouterRuntimeError)
    assert errors[0].code == "path_cancelled"
    assert runtime._observed_work_unit_count == 1
    assert 0 < runtime._maximum_observed_work_unit_ms < 1_000
