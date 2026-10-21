# SPDX-License-Identifier: AGPL-3.0-only
"""A16 restart re-entry: the withdraw fence survives a restart before any runtime attaches.

Hardware finding (A16 session 4): after ``--resume`` the app reported ``normal`` with admissions
allowed although a withdraw marker existed in the target's managed capacity root, because the
marker was only synced from an *attached* managed runtime.  And with no runtime there was no
driver for re-entry.  Deterministic only (fake clocks, samples and authority).
"""
from __future__ import annotations

import os
import threading
import time
from pathlib import Path

import pytest

from mycelium_a13_macos import adaptive_host as ah
from mycelium_a13_macos.adaptive_owner import adaptive_publication, adaptive_status
from mycelium_a13_macos.app import DemoApp
from mycelium_router import adaptive_overlay as ov

GIB = 1 << 30


class Clock:
    def __init__(self, now=1000.0):
        self.now = now

    def __call__(self):
        return self.now


class Authority:
    """Target authority after ``--resume``: member current, no managed runtime attached."""

    def __init__(self, state_root):
        self.peer = type("Peer", (), {"state_root": state_root})()
        self.member = object()
        self._runtime = None
        self.renews = self.reports = 0

    def renew(self):
        self.renews += 1
        return {}

    def status(self):
        return {"membership_state": "current", "lease_state": "fresh"}

    def report_capability(self):
        self.reports += 1
        return {"state": "self_report_accepted", "generation": 3, "expires_at": 9e9}


class AppStub:
    installation_role = "target"

    def __init__(self, root, clock):
        self.root = Path(root)
        self.authority = Authority(self.root / "target")
        self._adaptive_clock = clock
        self._adaptive_sampler = lambda: {
            "observed_at": clock.now, "pressure": "normal", "available_bytes": 8 * GIB}
        self.closed = threading.Event()
        self.guard = threading.RLock()

    @property
    def run_dir(self):
        return self.authority.peer.state_root / "managed" / "run-1"


def _enable(root, **timings):
    root.mkdir(parents=True, exist_ok=True)
    policy = {**ah.DEFAULT_POLICY, "enabled": True, "timings": timings}
    ah.write_adaptive_policy(root, policy, owner_authorized=True)


def _restarted_app(tmp_path, reason="host_memory_pressure", **timings):
    """A freshly started app whose previous process left a withdraw marker behind."""
    clock = Clock()
    app = AppStub(tmp_path, clock)
    app.run_dir.mkdir(parents=True)
    ov.write_marker(app.run_dir, reason=reason, wall_now=time.time())
    _enable(app.root, **timings)
    return app, clock


def _marker(app):
    return app.run_dir / ov.MARKER_FILENAME


# ------------------------------------------------------------------------- P1
def test_first_status_after_restart_is_withdrawn_before_any_runtime_attaches(tmp_path):
    app, _clock = _restarted_app(tmp_path)
    assert app.authority._runtime is None
    status = adaptive_status(app)
    assert status["mode"] == "withdraw" and status["state"] == "withdraw"
    assert status["reason"] == "host_memory_pressure"
    assert status["new_admissions_allowed"] is False and status["generation"] >= 1
    assert status["capacity_scale"] == "zero"
    publication = adaptive_publication(app)
    assert publication["new_admissions_allowed"] is False and publication["mode"] == "withdraw"
    assert ah.adaptive_resource_check(app) is not None  # admission refused, not allowed


def test_an_expired_marker_still_fences_the_restart(tmp_path):
    app, _clock = _restarted_app(tmp_path)
    ov.write_marker(app.run_dir, reason="owner_stop", wall_now=time.time() - 3600)
    status = adaptive_status(app)
    assert status["mode"] == "withdraw" and status["new_admissions_allowed"] is False


def test_unsafe_marker_in_a_managed_root_fences_the_restart(tmp_path):
    app, _clock = _restarted_app(tmp_path)
    os.chmod(_marker(app), 0o644)
    assert adaptive_status(app)["mode"] == "withdraw"


def test_recovery_window_is_measured_from_the_new_process_healthy_samples(tmp_path):
    app, clock = _restarted_app(tmp_path)
    adaptive_status(app)
    controller = app._adaptive_runtime.controller
    for _ in range(50):  # 100 s of calm: shorter than recovery_s (120)
        clock.now += 2
        adaptive_status(app)
    assert controller.mode == "withdraw" and controller.recovery_ready() is False
    for _ in range(15):  # 130 s total
        clock.now += 2
        adaptive_status(app)
    assert controller.mode == "withdraw" and controller.recovery_ready() is True


def test_owner_policy_timing_overrides_apply_to_the_restored_fence(tmp_path):
    app, clock = _restarted_app(tmp_path, recovery_s=10, drain_dwell_s=1)
    adaptive_status(app)
    controller = app._adaptive_runtime.controller
    for _ in range(4):
        clock.now += 2
        adaptive_status(app)
    assert controller.recovery_ready() is False
    for _ in range(3):
        clock.now += 2
        adaptive_status(app)
    assert controller.recovery_ready() is True


def test_no_marker_means_a_normal_start_and_nothing_is_written(tmp_path):
    clock = Clock()
    app = AppStub(tmp_path, clock)
    app.run_dir.mkdir(parents=True)
    _enable(app.root)
    status = adaptive_status(app)
    assert status["mode"] == "normal" and status["new_admissions_allowed"] is True
    assert ah.adaptive_resource_check(app) is None
    assert not _marker(app).exists()


def test_marker_is_ignored_while_a16_is_off_and_removed_by_the_next_tick(tmp_path):
    app, _clock = _restarted_app(tmp_path)
    ah.write_adaptive_policy(app.root, {**ah.DEFAULT_POLICY, "enabled": False}, owner_authorized=True)
    assert adaptive_status(app)["state"] == "off"
    assert ah.adaptive_resource_check(app) is None
    DemoApp._adaptive_tick(app)
    assert not _marker(app).exists()


def test_unreadable_policy_never_clears_the_marker(tmp_path):
    app, _clock = _restarted_app(tmp_path)
    policy_file = app.root / ah.POLICY_FILENAME
    policy_file.write_text("{not json")
    policy_file.chmod(0o600)
    DemoApp._adaptive_tick(app)
    assert _marker(app).exists()


# ------------------------------------------------------------------------- P2
def _tick(app, clock, seconds, step=2):
    end = clock.now + seconds
    while clock.now < end:
        clock.now += step
        DemoApp._adaptive_tick(app)


def test_reentry_is_reachable_without_a_runtime_and_removes_the_marker(tmp_path):
    app, clock = _restarted_app(tmp_path)
    DemoApp._adaptive_tick(app)
    controller = app._adaptive_runtime.controller
    assert controller.mode == "withdraw"
    _tick(app, clock, 100)  # still inside the recovery window: no authority traffic
    assert controller.mode == "withdraw" and app.authority.renews == 0
    assert _marker(app).exists()  # refreshed while withdrawn, so a second restart is fenced too
    _tick(app, clock, 60)
    assert app.authority.renews >= ah.DEFAULT_TIMINGS["authority_observations"]
    assert app.authority.reports == 1  # requalification after a withdraw
    assert controller.mode == "normal" and controller.generation >= 2
    assert not _marker(app).exists()
    status = adaptive_status(app)
    assert status["mode"] == "normal" and status["new_admissions_allowed"] is True
    assert ah.adaptive_resource_check(app) is None


def test_marker_is_refreshed_while_withdrawn_without_a_runtime(tmp_path):
    app, clock = _restarted_app(tmp_path)
    ov.write_marker(app.run_dir, reason="host_memory_pressure", wall_now=time.time() - 3600)
    DemoApp._adaptive_tick(app)
    assert _marker(app).exists()
    # an expired marker restores with reason "none" (existing semantics) and is made valid again
    assert ov.read_marker(app.run_dir, wall_clock=time.time) == {"valid": True, "reason": "none"}


def test_owner_stop_blocks_reentry_without_a_runtime(tmp_path):
    app, clock = _restarted_app(tmp_path)
    DemoApp._adaptive_tick(app)
    app._adaptive_runtime.controller.stop_serving()
    _tick(app, clock, 400)
    assert app._adaptive_runtime.controller.mode != "normal" and app.authority.reports == 0
    assert _marker(app).exists()


def test_tick_does_not_drive_reentry_while_a_runtime_is_attached(tmp_path):
    app, clock = _restarted_app(tmp_path)
    DemoApp._adaptive_tick(app)
    app.authority._runtime = object()  # the managed control loop owns re-entry now
    _tick(app, clock, 400)
    assert app.authority.renews == 0 and app._adaptive_runtime.controller.mode == "withdraw"


def test_tick_is_a_noop_for_the_owner_role_and_while_off(tmp_path):
    clock = Clock()
    app = AppStub(tmp_path, clock)
    app.installation_role = "owner"
    app._adaptive_sampler = lambda: pytest.fail("sampled")
    DemoApp._adaptive_tick(app)
    assert not hasattr(app, "_adaptive_runtime")
    app.installation_role = "target"
    DemoApp._adaptive_tick(app)  # no policy file: A16 off, never samples
    assert app.authority.renews == 0


def test_policy_loop_drives_the_adaptive_tick_even_without_acquisition(tmp_path):
    class Closed:
        def __init__(self):
            self.calls = 0

        def wait(self, _timeout):
            self.calls += 1
            return self.calls > 2

        def is_set(self):
            return False

    app, _clock = _restarted_app(tmp_path)
    app.closed = Closed()
    app._acquisition_active = False
    ticks = []
    app._adaptive_tick = lambda: ticks.append(1)
    DemoApp._policy_loop(app)
    assert ticks == [1, 1]
