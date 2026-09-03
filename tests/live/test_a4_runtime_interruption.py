from __future__ import annotations

from collections import Counter, OrderedDict
from types import SimpleNamespace
import threading
import time

import numpy as np
import pytest

import mycelium_router.mlx_runtime as mlx_runtime
from mycelium_router.mlx_runtime import MLXRuntimePort
import mycelium_router.numpy_runtime as numpy_runtime
from mycelium_router.contracts import (
    HopWorkItem,
    LayerRange,
    RuntimeResult,
    Stage,
    StageCost,
)
from mycelium_router.numpy_runtime import NumpyRouterRuntimeError, NumpyRuntimePort


class _TrackedRLock:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self.queued_waiting = threading.Event()

    def acquire(self, blocking: bool = True) -> bool:
        if threading.current_thread().name == "queued-runtime-execution":
            self.queued_waiting.set()
        return self._lock.acquire(blocking=blocking)

    def release(self) -> None:
        self._lock.release()

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *_args) -> None:
        self.release()


@pytest.mark.parametrize("runtime_type", (NumpyRuntimePort, MLXRuntimePort))
def test_queued_runtime_execution_does_not_block_active_checkpoint(
    runtime_type,
) -> None:
    runtime = object.__new__(runtime_type)
    runtime._cancellation_lock = threading.RLock()
    runtime._cancellation_condition = threading.Condition(
        runtime._cancellation_lock
    )
    runtime._cancellation_pending = 0
    runtime._execution_admission_lock = threading.RLock()
    runtime._state_lock = _TrackedRLock()
    runtime._resource_index_lock = threading.RLock()
    runtime._executing_subjects = {}
    runtime._cancelled_paths = OrderedDict()
    runtime._deferred_cancellation_cleanup = set()

    active_holds_state = threading.Event()
    checkpoint_now = threading.Event()
    checkpoint_complete = threading.Event()

    def active_execution() -> None:
        with runtime._state_lock:
            active_holds_state.set()
            assert checkpoint_now.wait(timeout=1.0)
            runtime._checkpoint_path("path-active")
            checkpoint_complete.set()

    item = SimpleNamespace(
        request_id="request-queued",
        path_id="path-queued",
        path_attempt=0,
    )

    def queued_execution() -> None:
        runtime._acquire_execution_state(item)
        runtime._state_lock.release()

    active = threading.Thread(target=active_execution)
    queued = threading.Thread(
        target=queued_execution,
        name="queued-runtime-execution",
    )
    active.start()
    assert active_holds_state.wait(timeout=1.0)
    queued.start()
    assert runtime._state_lock.queued_waiting.wait(timeout=1.0)
    checkpoint_now.set()

    assert checkpoint_complete.wait(timeout=0.2)
    active.join(timeout=1.0)
    queued.join(timeout=1.0)
    assert not active.is_alive()
    assert not queued.is_alive()


@pytest.mark.parametrize("runtime_type", (NumpyRuntimePort, MLXRuntimePort))
def test_runtime_cancel_never_waits_for_queued_execution_admission(
    runtime_type,
) -> None:
    runtime = object.__new__(runtime_type)
    runtime._cancellation_lock = threading.RLock()
    runtime._cancellation_condition = threading.Condition(
        runtime._cancellation_lock
    )
    runtime._cancellation_pending = 0
    runtime._execution_admission_lock = threading.RLock()
    runtime._state_lock = threading.RLock()
    runtime._resource_index_lock = threading.RLock()
    runtime._executing_subjects = {}
    runtime._kv_subjects = {}
    runtime._cancelled_paths = OrderedDict()
    runtime._deferred_cancellation_cleanup = set()
    runtime._kv_states = {}
    runtime._released_paths = OrderedDict()
    runtime._replays = OrderedDict()
    runtime._release_counts = Counter()
    runtime._last_release_reason = None

    admission_held = threading.Event()

    def queued_execution() -> None:
        with runtime._execution_admission_lock:
            admission_held.set()
            with runtime._state_lock:
                pass

    runtime._state_lock.acquire()
    queued = threading.Thread(target=queued_execution)
    queued.start()
    assert admission_held.wait(timeout=1.0)

    cancelled = threading.Event()

    def cancel() -> None:
        runtime.cancel("path-queued")
        cancelled.set()

    cancellation = threading.Thread(target=cancel)
    started = time.monotonic()
    cancellation.start()
    try:
        assert cancelled.wait(timeout=0.2)
        assert time.monotonic() - started < 0.2
        assert "path-queued" in runtime._cancelled_paths
    finally:
        runtime._state_lock.release()
    cancellation.join(timeout=1.0)
    queued.join(timeout=1.0)
    assert not cancellation.is_alive()
    assert not queued.is_alive()


@pytest.mark.parametrize(
    ("runtime_type", "release_reason"),
    (
        (NumpyRuntimePort, "cancelled"),
        (MLXRuntimePort, "cancellation"),
    ),
)
def test_queued_execution_yields_admission_to_deferred_cleanup(
    runtime_type,
    release_reason: str,
) -> None:
    runtime = object.__new__(runtime_type)
    runtime._cancellation_lock = threading.RLock()
    runtime._cancellation_condition = threading.Condition(
        runtime._cancellation_lock
    )
    runtime._cancellation_pending = 0
    runtime._execution_admission_lock = threading.RLock()
    runtime._state_lock = _TrackedRLock()
    runtime._resource_index_lock = threading.RLock()
    runtime._executing_subjects = {}
    runtime._kv_subjects = {"path-cancel": ("request-cancel", 0)}
    runtime._cancelled_paths = OrderedDict()
    runtime._deferred_cancellation_cleanup = set()
    runtime._kv_states = {
        "path-cancel": SimpleNamespace(layers={"layer": object()}),
    }
    runtime._released_paths = OrderedDict()
    runtime._replays = OrderedDict()
    runtime._release_counts = Counter()
    runtime._last_release_reason = None

    runtime._state_lock.acquire()
    queued_observed_clean: list[bool] = []
    item = SimpleNamespace(
        request_id="request-queued",
        path_id="path-queued",
        path_attempt=0,
    )

    def queued_execution() -> None:
        runtime._acquire_execution_state(item)
        queued_observed_clean.append("path-cancel" not in runtime._kv_states)
        runtime._state_lock.release()

    queued = threading.Thread(
        target=queued_execution,
        name="queued-runtime-execution",
    )
    queued.start()
    assert runtime._state_lock.queued_waiting.wait(timeout=1.0)

    runtime.cancel("path-cancel")
    assert "path-cancel" in runtime._deferred_cancellation_cleanup
    runtime._state_lock.release()
    queued.join(timeout=1.0)

    assert not queued.is_alive()
    assert queued_observed_clean == [True]
    assert runtime._deferred_cancellation_cleanup == set()
    assert runtime._release_counts[release_reason] == 1


@pytest.mark.parametrize(
    ("runtime_type", "release_reason"),
    (
        (NumpyRuntimePort, "cancelled"),
        (MLXRuntimePort, "cancellation"),
    ),
)
def test_idle_snapshot_lock_handoff_cannot_strand_cancelled_kv(
    runtime_type,
    release_reason: str,
) -> None:
    runtime = object.__new__(runtime_type)
    runtime._cancellation_lock = threading.RLock()
    runtime._cancellation_condition = threading.Condition(
        runtime._cancellation_lock
    )
    runtime._cancellation_pending = 0
    runtime._execution_admission_lock = threading.RLock()
    runtime._state_lock = threading.RLock()
    runtime._resource_index_lock = threading.RLock()
    runtime._executing_subjects = {}
    runtime._kv_subjects = {"path-idle": ("request-idle", 0)}
    runtime._cancelled_paths = OrderedDict()
    runtime._deferred_cancellation_cleanup = set()
    runtime._kv_states = {
        "path-idle": SimpleNamespace(layers={"layer": object()}),
    }
    runtime._released_paths = OrderedDict()
    runtime._replays = OrderedDict()
    runtime._release_counts = Counter()
    runtime._last_release_reason = None
    snapshot_holds_state = threading.Event()
    release_snapshot = threading.Event()

    def snapshot_owner() -> None:
        with runtime._state_lock:
            snapshot_holds_state.set()
            assert release_snapshot.wait(timeout=1.0)

    snapshot = threading.Thread(target=snapshot_owner)
    snapshot.start()
    assert snapshot_holds_state.wait(timeout=1.0)

    started = time.monotonic()
    runtime.cancel("path-idle")
    assert time.monotonic() - started < 0.2
    assert "path-idle" in runtime._deferred_cancellation_cleanup
    assert "path-idle" in runtime._kv_states

    release_snapshot.set()
    snapshot.join(timeout=1.0)
    deadline = time.monotonic() + 1.0
    while "path-idle" in runtime._kv_states and time.monotonic() < deadline:
        time.sleep(0.001)

    assert not snapshot.is_alive()
    assert "path-idle" not in runtime._kv_states
    assert "path-idle" not in runtime._kv_subjects
    assert runtime._deferred_cancellation_cleanup == set()
    assert runtime._release_counts[release_reason] == 1


def test_numpy_cleanup_snapshot_never_waits_for_active_stage_lock() -> None:
    runtime = object.__new__(NumpyRuntimePort)
    runtime._state_lock = threading.RLock()
    lock_held = threading.Event()
    release_lock = threading.Event()

    def hold_stage_lock() -> None:
        with runtime._state_lock:
            lock_held.set()
            assert release_lock.wait(timeout=1.0)

    worker = threading.Thread(target=hold_stage_lock)
    worker.start()
    assert lock_held.wait(timeout=1.0)
    started = time.monotonic()
    assert runtime.kv_snapshot_nonblocking() is None
    assert time.monotonic() - started < 0.1
    release_lock.set()
    worker.join(timeout=1.0)
    assert not worker.is_alive()


@pytest.mark.parametrize("runtime_type", (NumpyRuntimePort, MLXRuntimePort))
def test_runtime_cleanup_proof_is_scoped_while_unrelated_stage_is_active(
    runtime_type,
) -> None:
    runtime = object.__new__(runtime_type)
    runtime._resource_index_lock = threading.RLock()
    runtime._executing_subjects = {
        "path-active": ("request-active", 0),
    }
    runtime._kv_subjects = {
        "path-retained": ("request-retained", 0),
    }

    assert runtime.kv_subject_clean("request-other", "path-other", 0) is True
    assert runtime.kv_subject_clean("request-active", "path-active", 0) is False
    assert (
        runtime.kv_subject_clean("request-retained", "path-retained", 0)
        is False
    )


def test_numpy_stage_cancellation_marks_without_waiting_for_runtime_lock(
    monkeypatch,
) -> None:
    runtime = object.__new__(NumpyRuntimePort)
    runtime._cancellation_lock = threading.RLock()
    runtime._cancellation_condition = threading.Condition(
        runtime._cancellation_lock
    )
    runtime._cancellation_pending = 0
    runtime._execution_admission_lock = threading.RLock()
    runtime._state_lock = threading.RLock()
    runtime._cancelled_paths = OrderedDict()
    runtime._deferred_cancellation_cleanup = set()
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

    def bounded_layer(hidden, *_args, **_kwargs):
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


@pytest.mark.parametrize(
    ("runtime_type", "runtime_module", "monolithic_name", "output_factory"),
    (
        (
            NumpyRuntimePort,
            numpy_runtime,
            "_numpy_execute_loaded_stage",
            lambda: np.zeros((1, 1, 2), dtype=np.float32),
        ),
        (
            MLXRuntimePort,
            mlx_runtime,
            "_execute_loaded_stage",
            lambda: mlx_runtime.mx.zeros((1, 1, 2)),
        ),
    ),
)
def test_complete_context_qwen_uses_checkpointed_stage_execution(
    monkeypatch,
    runtime_type,
    runtime_module,
    monolithic_name: str,
    output_factory,
) -> None:
    runtime = object.__new__(runtime_type)
    output = output_factory()
    cooperative_calls = []
    remembered = []

    def cooperative(**kwargs):
        cooperative_calls.append(kwargs)
        return output, {0: (output, output)}

    def monolithic(*_args, **_kwargs):
        raise AssertionError("complete-context execution was not checkpointed")

    monkeypatch.setattr(runtime_module, monolithic_name, monolithic)
    runtime._execute_stage_with_kv = cooperative
    runtime._runtime_result = lambda *_args: RuntimeResult(success=True)
    runtime._remember_result = lambda *args: remembered.append(args)
    runtime._applied_operation_count = 0
    runtime._activation_output_bytes = 0
    runtime._prefill_operation_count = 0
    runtime._prefill_input_token_count = 0
    runtime._decode_operation_count = 0
    runtime._decode_input_token_count = 0
    item = SimpleNamespace(
        phase="PREFILL",
        token_index=-1,
        position=0,
        path_id="path-checkpointed",
    )

    result = runtime._execute_complete_context(
        item,
        SimpleNamespace(component_roles=("decoder",)),
        SimpleNamespace(),
        {"architecture": "qwen2"},
        None,
        output,
        1,
        "fingerprint",
    )

    assert result == RuntimeResult(success=True)
    assert len(cooperative_calls) == 1
    assert cooperative_calls[0]["position"] == 0
    assert cooperative_calls[0]["past_layers"] == {}
    assert cooperative_calls[0]["path_id"] == "path-checkpointed"
    assert remembered


def test_busy_runtime_cancel_returns_immediately_and_execute_finalizer_cleans() -> None:
    runtime = object.__new__(NumpyRuntimePort)
    runtime._cancellation_lock = threading.RLock()
    runtime._cancellation_condition = threading.Condition(
        runtime._cancellation_lock
    )
    runtime._cancellation_pending = 0
    runtime._execution_admission_lock = threading.RLock()
    runtime._state_lock = threading.RLock()
    runtime._resource_index_lock = threading.RLock()
    runtime._executing_subjects = {}
    runtime._kv_subjects = {"path-cancel": ("request-cancel", 0)}
    runtime._cancelled_paths = OrderedDict()
    runtime._deferred_cancellation_cleanup = set()
    runtime._kv_states = {}
    runtime._released_paths = OrderedDict()
    runtime._replays = OrderedDict()
    runtime._release_counts = Counter()
    runtime._last_release_reason = None

    runtime._closed = False
    runtime._kv_states = {
        "path-cancel": SimpleNamespace(layers={"layer": object()})
    }
    runtime.expire_leases = lambda: ()  # type: ignore[method-assign]
    execution_entered = threading.Event()
    release_execution = threading.Event()

    def execute_bound(_item: HopWorkItem) -> RuntimeResult:
        execution_entered.set()
        assert release_execution.wait(timeout=1.0)
        return RuntimeResult(success=True)

    runtime._execute_bound = execute_bound  # type: ignore[method-assign]
    item = HopWorkItem(
        request_id="request-cancel",
        path_id="path-cancel",
        path_attempt=0,
        phase="PREFILL",
        token_index=-1,
        hop_index=0,
        placement_id="placement-cancel",
        qos_class="interactive",
        deficit_ratio=0.0,
        enqueued_at=0.0,
        idempotency_key="operation-cancel",
        payload=b"payload",
    )
    execution = threading.Thread(target=runtime.execute, args=(item,))
    execution.start()
    assert execution_entered.wait(timeout=1.0)

    started = time.monotonic()
    runtime.cancel("path-cancel")
    cancel_elapsed = time.monotonic() - started

    assert cancel_elapsed < 0.2
    assert "path-cancel" in runtime._cancelled_paths
    assert "path-cancel" in runtime._kv_states
    release_execution.set()
    execution.join(timeout=1.0)

    assert not execution.is_alive()
    assert "path-cancel" not in runtime._kv_states
    assert runtime._release_counts["cancelled"] == 1


@pytest.mark.parametrize(
    ("runtime_type", "release_reason"),
    (
        (NumpyRuntimePort, "cancelled"),
        (MLXRuntimePort, "cancellation"),
    ),
)
def test_active_operation_drains_cancelled_inactive_path_kv(
    runtime_type,
    release_reason: str,
) -> None:
    runtime = object.__new__(runtime_type)
    runtime._cancellation_lock = threading.RLock()
    runtime._cancellation_condition = threading.Condition(
        runtime._cancellation_lock
    )
    runtime._cancellation_pending = 0
    runtime._execution_admission_lock = threading.RLock()
    runtime._state_lock = threading.RLock()
    runtime._resource_index_lock = threading.RLock()
    runtime._executing_subjects = {}
    runtime._kv_subjects = {
        "path-inactive": ("request-inactive", 0),
    }
    runtime._cancelled_paths = OrderedDict()
    runtime._deferred_cancellation_cleanup = set()
    runtime._kv_states = {
        "path-inactive": SimpleNamespace(layers={"layer": object()}),
    }
    runtime._released_paths = OrderedDict()
    runtime._replays = OrderedDict()
    runtime._release_counts = Counter()
    runtime._last_release_reason = None
    runtime._closed = False
    runtime.expire_leases = lambda: ()  # type: ignore[method-assign]

    active_entered = threading.Event()
    release_active = threading.Event()

    def execute_bound(_item: HopWorkItem) -> RuntimeResult:
        active_entered.set()
        assert release_active.wait(timeout=1.0)
        return RuntimeResult(success=True)

    runtime._execute_bound = execute_bound  # type: ignore[method-assign]
    active_item = HopWorkItem(
        request_id="request-active",
        path_id="path-active",
        path_attempt=0,
        phase="PREFILL",
        token_index=-1,
        hop_index=0,
        placement_id="placement-active",
        qos_class="interactive",
        deficit_ratio=0.0,
        enqueued_at=0.0,
        idempotency_key="operation-active",
        payload=b"payload",
    )
    execution = threading.Thread(target=runtime.execute, args=(active_item,))
    execution.start()
    assert active_entered.wait(timeout=1.0)

    runtime.cancel("path-inactive")

    assert "path-inactive" in runtime._kv_states
    assert "path-inactive" in runtime._deferred_cancellation_cleanup
    release_active.set()
    execution.join(timeout=1.0)

    assert not execution.is_alive()
    assert "path-inactive" not in runtime._kv_states
    assert "path-inactive" not in runtime._kv_subjects
    assert runtime._deferred_cancellation_cleanup == set()
    assert runtime._release_counts[release_reason] == 1
