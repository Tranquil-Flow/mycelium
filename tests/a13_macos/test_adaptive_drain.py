# SPDX-License-Identifier: AGPL-3.0-only
"""A16 drain-to-withdraw wiring against the real capacity ledger and runtime/worker seams.

Deterministic only: a real SQLite reservation ledger (same schema as the node's port), fake
clock, fake worker.  No device, model or host signal.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from mycelium_a13_macos import adaptive_drain as ad
from mycelium_a13_macos import adaptive_host as ah

GIB = 1 << 30


class Clock:
    def __init__(self, now=1000.0):
        self.now = now

    def __call__(self):
        return self.now


def _ledger(root: Path, rows=()):
    db = sqlite3.connect(root / "dep.capacity.sqlite3")
    db.execute(
        "CREATE TABLE reservations (reservation_id TEXT PRIMARY KEY, request_id TEXT NOT NULL,"
        " path_id TEXT NOT NULL, path_attempt INTEGER NOT NULL, placement_id TEXT NOT NULL,"
        " node_id TEXT NOT NULL, placement_identity TEXT NOT NULL, kv_bytes INTEGER NOT NULL,"
        " deployment_epoch INTEGER NOT NULL, lease_expires_at REAL NOT NULL,"
        " status TEXT NOT NULL)")
    for i, (status, lease) in enumerate(rows):
        db.execute("INSERT INTO reservations VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                   (f"res{i}", f"req{i}", "p", 0, "pl", "n", "x", 1000, 1, lease, status))
    db.commit()
    db.close()


def _set_status(root: Path, status):
    db = sqlite3.connect(root / "dep.capacity.sqlite3")
    db.execute("UPDATE reservations SET status = ?", (status,))
    db.commit()
    db.close()


class FakeWorker:
    def __init__(self, rss=4 * GIB):
        self.rss, self.running, self.closed = rss, True, 0

    def resident_bytes(self):
        return self.rss if self.running else 0


class FakeRuntime:
    def __init__(self, root, worker=None, unload_ok=True):
        self.root, self.worker, self.unloads, self._ok = root, worker or FakeWorker(), 0, unload_ok
        self.model_unloaded = False

    def unload_model(self):
        self.unloads += 1
        if self._ok:
            self.worker.running = False
            self.model_unloaded = True
        return self._ok


def _controller(clock, **over):
    policy = {**ah.DEFAULT_POLICY, "enabled": True, **over}
    ctl = ah.AdaptiveHostController(policy, clock=clock)
    return ctl


def _draining(clock, runtime):
    ctl = ah.AdaptiveHostController(
        {**ah.DEFAULT_POLICY, "enabled": True}, clock=clock,
        footprint_bytes=lambda: ad.footprint_bytes(runtime),
        inflight=lambda: ad.inflight_count(runtime))
    ctl.observe({"observed_at": clock(), "pressure": "normal", "available_bytes": 8 * GIB})
    ctl.apply_policy({**ah.DEFAULT_POLICY, "enabled": True, "game_mode": True})
    assert ctl.mode == "drain"
    return ctl


def test_inflight_counts_only_live_reservations(tmp_path):
    _ledger(tmp_path, [("RESERVED", 2000.0), ("COMMITTED", 2000.0), ("RELEASED", 2000.0), ("EXPIRED", 5.0)])
    assert ad.inflight_count(FakeRuntime(tmp_path)) == 2


def test_unobservable_ledger_never_counts_as_drained(tmp_path):
    assert ad.inflight_count(FakeRuntime(tmp_path)) == 1  # no ledger file: cannot prove zero
    (tmp_path / "dep.capacity.sqlite3").write_bytes(b"not sqlite")
    assert ad.inflight_count(FakeRuntime(tmp_path)) == 1
    assert ad.inflight_count(None) == 0  # no runtime at all: nothing to drain


def test_footprint_is_worker_resident_bytes_and_survives_a_failed_read(tmp_path):
    worker = FakeWorker(3 * GIB)
    runtime = FakeRuntime(tmp_path, worker)
    assert ad.footprint_bytes(runtime) == 3 * GIB
    worker.resident_bytes = lambda: (_ for _ in ()).throw(OSError("ps stalled"))
    assert ad.footprint_bytes(runtime) == 3 * GIB  # last good value, never an invented zero
    assert ad.footprint_bytes(None) == 0


def test_drain_never_unloads_while_a_request_is_in_flight(tmp_path):
    _ledger(tmp_path, [("COMMITTED", 1100.0)])
    clock, runtime = Clock(), FakeRuntime(tmp_path)
    ctl = _draining(clock, runtime)
    report = ad.drain_step(ctl, runtime, wall_clock=clock)
    assert runtime.unloads == 0 and report["inflight"] == 1
    assert report["dispositions"] == {"finish": 1}
    assert ctl.mode == "drain"


def test_drain_unloads_after_the_last_request_and_reaches_withdraw(tmp_path):
    _ledger(tmp_path, [("COMMITTED", 1100.0)])
    clock, runtime = Clock(), FakeRuntime(tmp_path)
    ctl = _draining(clock, runtime)
    ad.drain_step(ctl, runtime, wall_clock=clock)
    _set_status(tmp_path, "RELEASED")
    report = ad.drain_step(ctl, runtime, wall_clock=clock)
    assert runtime.unloads == 1 and report["unloaded"] is True
    clock.now += 2
    ctl.observe({"observed_at": clock(), "pressure": "normal", "available_bytes": 8 * GIB})
    assert ctl.mode == "withdraw"
    assert ad.footprint_bytes(runtime) == 0  # measured release after the unload
    ad.drain_step(ctl, runtime, wall_clock=clock)
    assert runtime.unloads == 1  # idempotent


def test_failed_unload_is_visible_and_never_marks_cleanup(tmp_path):
    _ledger(tmp_path)
    clock, runtime = Clock(), FakeRuntime(tmp_path, unload_ok=False)
    ctl = _draining(clock, runtime)
    report = ad.drain_step(ctl, runtime, wall_clock=clock)
    assert report["unloaded"] is False and runtime.unloads == 1
    clock.now += 2
    ctl.observe({"observed_at": clock(), "pressure": "normal", "available_bytes": 8 * GIB})
    assert ctl.mode == "drain"  # no withdraw without proven cleanup


def test_expired_lease_is_aborted_not_finished_and_critical_pressure_bypasses_finish(tmp_path):
    _ledger(tmp_path, [("COMMITTED", 999.0), ("COMMITTED", 1100.0)])
    clock, runtime = Clock(), FakeRuntime(tmp_path)
    ctl = _draining(clock, runtime)
    aborted = []
    hooks = ad.DrainHooks(abort=lambda row: aborted.append(row["request_id"]) or True)
    report = ad.drain_step(ctl, runtime, wall_clock=clock, hooks=hooks)
    assert report["dispositions"] == {"abort_terminal": 1, "finish": 1}
    assert aborted == ["req0"]
    ctl.observe({"observed_at": clock(), "pressure": "critical", "available_bytes": 8 * GIB})
    aborted.clear()
    report = ad.drain_step(ctl, runtime, wall_clock=clock, hooks=hooks)
    assert report["dispositions"] == {"abort_terminal": 2} and sorted(aborted) == ["req0", "req1"]


def test_abort_without_a_hook_is_reported_and_blocks_unload(tmp_path):
    _ledger(tmp_path, [("COMMITTED", 999.0)])
    clock, runtime = Clock(), FakeRuntime(tmp_path)
    ctl = _draining(clock, runtime)
    report = ad.drain_step(ctl, runtime, wall_clock=clock)
    assert report["abort_unavailable"] == 1 and runtime.unloads == 0


def test_qualified_successor_selects_a6_handoff_and_failed_handoff_is_not_silent(tmp_path):
    _ledger(tmp_path, [("COMMITTED", 1100.0)])
    clock, runtime = Clock(), FakeRuntime(tmp_path)
    ctl = _draining(clock, runtime)
    ctl.observe({"observed_at": clock(), "pressure": "critical", "available_bytes": 8 * GIB})
    ready = {"qualified": True, "compatible": True, "fence_ready": True, "checkpoint_committed": True}
    calls = []
    hooks = ad.DrainHooks(successor=lambda row: ready, handoff=lambda row: calls.append(row["request_id"]) or True)
    report = ad.drain_step(ctl, runtime, wall_clock=clock, hooks=hooks)
    assert report["dispositions"] == {"a6_handoff": 1} and calls == ["req0"]
    refused = ad.DrainHooks(successor=lambda row: ready, handoff=lambda row: False)
    report = ad.drain_step(ctl, runtime, wall_clock=clock, hooks=refused)
    assert report["handoff_refused"] == 1 and runtime.unloads == 0


def test_cancelled_request_keeps_cancel_precedence(tmp_path):
    _ledger(tmp_path, [("COMMITTED", 1100.0)])
    clock, runtime = Clock(), FakeRuntime(tmp_path)
    ctl = _draining(clock, runtime)
    report = ad.drain_step(ctl, runtime, wall_clock=clock, hooks=ad.DrainHooks(cancelled=lambda row: True))
    assert report["dispositions"] == {"cancel": 1} and runtime.unloads == 0


def test_normal_reduced_and_off_do_nothing(tmp_path):
    _ledger(tmp_path)
    clock, runtime = Clock(), FakeRuntime(tmp_path)
    off = _controller(clock, enabled=False)
    assert ad.drain_step(off, runtime, wall_clock=clock) == {"action": "none"}
    normal = _controller(clock)
    assert ad.drain_step(normal, runtime, wall_clock=clock) == {"action": "none"}
    assert runtime.unloads == 0


def test_app_resources_are_bound_only_for_a_target_runtime_and_stay_zero_otherwise(tmp_path):
    class Auth:  # no runtime attached
        _runtime = None

    class App:
        root, authority = tmp_path, Auth()

    assert ad.target_runtime(App()) is None
    App.authority._runtime = rt = FakeRuntime(tmp_path)
    assert ad.target_runtime(App()) is rt


# ------------------------------------------------------------ runtime / worker / app seams
import inspect
import threading


def test_worker_resident_bytes_reads_the_group_only_while_running():
    from mycelium_a13_macos.worker import ManagedWorker

    worker = object.__new__(ManagedWorker)
    worker._account_lock = threading.RLock()
    worker._session = type("S", (), {"returncode": None})()
    worker._group_rss = lambda deadline_monotonic_s=None: 123
    assert worker.resident_bytes() == 123
    worker._session = type("S", (), {"returncode": 0})()
    assert worker.resident_bytes() == 0
    worker._session = None
    assert worker.resident_bytes() == 0


class _Worker:
    def __init__(self, ok=True):
        self.ok, self.closed = ok, 0
        self.running = True

    def close(self):
        self.closed += 1
        self.running = False

    def cleanup_proven(self):
        return self.ok

    def process_group_present(self):
        return not self.ok

    def status(self):
        return {"running": self.running}


def _managed(worker):
    from mycelium_a13_macos.managed_runtime import ManagedTargetRuntime

    runtime = object.__new__(ManagedTargetRuntime)
    runtime._lock = threading.RLock()
    runtime._stop = threading.Event()
    runtime._state = "running"
    runtime.worker = worker
    runtime._withdrawn = False
    return runtime


def test_unload_model_closes_the_worker_with_cleanup_proof_and_keeps_the_runtime_alive():
    worker = _Worker()
    runtime = _managed(worker)
    assert runtime.model_unloaded is False
    assert runtime.unload_model() is True
    assert worker.closed == 1 and runtime.model_unloaded is True
    assert runtime._state == "running" and not runtime._stop.is_set()  # membership/control loop stays up
    assert runtime.unload_model() is True and worker.closed == 1       # idempotent
    runtime._replace_worker_if_dead()                                    # a withdrawn model is not replaced
    assert runtime._state == "running" and not runtime._stop.is_set()


def test_unload_without_cleanup_proof_is_not_reported_as_unloaded():
    worker = _Worker(ok=False)
    runtime = _managed(worker)
    assert runtime.unload_model() is False
    assert runtime.model_unloaded is False


def test_unload_refuses_a_stopped_or_not_running_runtime():
    runtime = _managed(_Worker())
    runtime._state = "blocked"
    assert runtime.unload_model() is False and runtime.worker.closed == 0


def test_serve_loop_and_constructors_carry_the_optional_drain_seam():
    from mycelium_a13_macos.managed_runtime import ManagedTargetRuntime
    from mycelium_a13_macos.target_app import TargetApplication

    assert "adaptive_drain" in inspect.signature(ManagedTargetRuntime.__init__).parameters
    assert "self._adaptive_drain" in inspect.getsource(ManagedTargetRuntime._serve)
    assert TargetApplication.adaptive_drain is None
    assert "adaptive_drain=self.adaptive_drain" in inspect.getsource(TargetApplication.prepare_runtime)


def test_app_runtime_callbacks_follow_the_attached_target_runtime(tmp_path):
    _ledger(tmp_path, [("COMMITTED", 1100.0)])

    class Auth:
        _runtime = None

    class App:
        root = tmp_path
        authority = Auth()
        _adaptive_clock = Clock()

    app = App()
    ah.adaptive_resource_check(app)  # policy file absent -> off, runtime built lazily
    controller = app._adaptive_runtime.controller
    assert controller._inflight() == 0 and controller._footprint() == 0  # no target runtime
    app.authority._runtime = FakeRuntime(tmp_path, FakeWorker(5 * GIB))
    assert controller._inflight() == 1 and controller._footprint() == 5 * GIB


def test_app_drain_tick_is_a_noop_while_adaptive_is_off_or_never_created(tmp_path):
    from mycelium_a13_macos.app import DemoApp

    class Stub:
        _adaptive_runtime = None

    runtime = FakeRuntime(tmp_path)
    DemoApp._adaptive_drain(Stub(), runtime)
    assert runtime.unloads == 0
