# SPDX-License-Identifier: AGPL-3.0-only
"""A16 live wiring: overlay file protocol, controller overlay, app provider, runtime publisher.

Deterministic only (fake clocks and samples); no device or real host signal.
"""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path

import pytest

from mycelium_a13_macos import adaptive_host as ah
from mycelium_router import adaptive_overlay as ov

GIB = 1 << 30


class Clock:
    def __init__(self, now=1000.0):
        self.now = now

    def __call__(self):
        return self.now


def _sample(clock, pressure="normal", available=8 * GIB):
    return {"observed_at": clock(), "pressure": pressure, "available_bytes": available}


def _enabled(**over):
    return {**ah.DEFAULT_POLICY, "enabled": True, **over}


def _controller(clock, policy=None):
    return ah.AdaptiveHostController(policy or _enabled(), clock=clock)


# ------------------------------------------------------------------ protocol
def _doc(**over):
    base = {"schema": 1, "mode": "normal", "reason": "none", "generation": 0, "admit": True,
            "refusal": None, "capacity_scale": "full", "new_kv_budget_bytes": None,
            "epoch": 5.0, "issued_at": 100.0, "expires_at": 106.0}
    base.update(over)
    return base


def test_absent_overlay_means_adaptive_inactive(tmp_path):
    assert ov.read_gate(tmp_path, wall_clock=lambda: 100.0) is None


def test_overlay_roundtrip_is_private_and_exact(tmp_path):
    ov.write_overlay(tmp_path, _doc())
    path = tmp_path / ov.OVERLAY_FILENAME
    assert oct(path.stat().st_mode & 0o777) == "0o600"
    assert ov.read_gate(tmp_path, wall_clock=lambda: 101.0)["admit"] is True
    ov.clear_overlay(tmp_path)
    assert not path.exists()
    ov.clear_overlay(tmp_path)  # idempotent


@pytest.mark.parametrize("bad", [
    {"app": "game.app"}, {"admit": 1}, {"mode": "turbo"}, {"new_kv_budget_bytes": -1},
    {"new_kv_budget_bytes": True}, {"capacity_scale": "huge"}, {"expires_at": float("nan")},
])
def test_invalid_overlay_is_refused_by_write_and_fails_closed_on_read(tmp_path, bad):
    with pytest.raises(ov.AdaptiveOverlayError):
        ov.write_overlay(tmp_path, _doc(**bad))
    raw = json.dumps(_doc(**bad), allow_nan=True).encode()
    path = tmp_path / ov.OVERLAY_FILENAME
    fd = os.open(path, os.O_WRONLY | os.O_CREAT, 0o600)
    os.write(fd, raw); os.close(fd)
    gate = ov.read_gate(tmp_path, wall_clock=lambda: 101.0)
    assert gate["admit"] is False and gate["refusal"] == "adaptive_controller_stale"


def test_extra_key_never_carried_through(tmp_path):
    doc = _doc(); doc["foreground_app"] = "x"
    with pytest.raises(ov.AdaptiveOverlayError):
        ov.write_overlay(tmp_path, doc)


def test_expired_overlay_fails_closed(tmp_path):
    ov.write_overlay(tmp_path, _doc())
    gate = ov.read_gate(tmp_path, wall_clock=lambda: 107.0)
    assert gate["admit"] is False and gate["refusal"] == "adaptive_controller_stale"
    assert gate["new_kv_budget_bytes"] == 0


def test_overlay_symlink_or_loose_mode_fails_closed(tmp_path):
    target = tmp_path / "elsewhere"
    target.write_text(json.dumps(_doc()))
    (tmp_path / ov.OVERLAY_FILENAME).symlink_to(target)
    assert ov.read_gate(tmp_path, wall_clock=lambda: 101.0)["admit"] is False
    (tmp_path / ov.OVERLAY_FILENAME).unlink()
    p = tmp_path / ov.OVERLAY_FILENAME
    p.write_text(json.dumps(_doc())); p.chmod(0o644)
    assert ov.read_gate(tmp_path, wall_clock=lambda: 101.0)["admit"] is False


# ----------------------------------------------------------- controller overlay
def test_off_controller_has_no_overlay():
    clock = Clock()
    c = ah.AdaptiveHostController(ah.DEFAULT_POLICY, clock=clock)
    assert c.overlay(wall_now=500.0) is None


def test_normal_overlay_carries_measured_headroom_budget():
    clock = Clock()
    c = _controller(clock, _enabled(reserve_bytes=GIB))
    c.observe(_sample(clock, available=5 * GIB))
    doc = c.overlay(wall_now=500.0)
    assert doc["admit"] is True and doc["mode"] == "normal"
    assert doc["new_kv_budget_bytes"] == 4 * GIB
    assert doc["expires_at"] == pytest.approx(500.0 + 6)
    ov.validate_overlay(doc)


def test_overlay_expiry_is_anchored_to_the_last_sample_not_the_write_time():
    clock = Clock()
    c = _controller(clock)
    c.observe(_sample(clock))
    clock.now += 4
    doc = c.overlay(wall_now=500.0)
    assert doc["expires_at"] == pytest.approx(502.0)  # 6 s staleness bound minus 4 s age
    clock.now += 10
    stale = c.overlay(wall_now=510.0)
    assert stale["admit"] is False and stale["refusal"] == "adaptive_controller_stale"
    assert stale["expires_at"] <= 510.0 + 1e-9


def test_reduced_overlay_with_no_headroom_measurement_admits_nothing_new():
    clock = Clock()
    c = _controller(clock)
    c.observe(_sample(clock, pressure="warn", available=None))
    doc = c.overlay(wall_now=1.0)
    assert doc["mode"] == "reduced" and doc["new_kv_budget_bytes"] == 0


@pytest.mark.parametrize("setup,refusal", [
    ("game", "adaptive_host_draining"), ("stop", "adaptive_host_draining"),
    ("critical", "adaptive_host_draining"), ("unknown", "adaptive_signal_unknown"),
])
def test_fencing_states_publish_admit_false(setup, refusal):
    clock = Clock()
    policy = _enabled(game_mode=setup == "game")
    c = _controller(clock, policy)
    if setup == "game":
        c.apply_policy(policy)
    raw = _sample(clock, pressure={"critical": "critical", "unknown": "unknown"}.get(setup, "normal"))
    if setup == "stop":
        c.stop_serving()
    c.observe(raw)
    doc = c.overlay(wall_now=1.0)
    assert doc["admit"] is False and doc["refusal"] == refusal and doc["new_kv_budget_bytes"] == 0
    assert set(doc) == ov.OVERLAY_KEYS  # no app identity or raw memory map


# ------------------------------------------------------------- runtime / app
def test_runtime_overlay_is_none_while_off_and_never_samples():
    clock = Clock()
    calls = []
    rt = ah.AdaptiveHostRuntime(policy_loader=lambda: dict(ah.DEFAULT_POLICY),
                                sampler=lambda: calls.append(1) or _sample(clock), clock=clock)
    assert rt.overlay(wall_clock=lambda: 1.0) is None
    assert calls == []


def test_runtime_overlay_tracks_policy_toggle():
    clock = Clock()
    policy = {"v": _enabled()}
    rt = ah.AdaptiveHostRuntime(policy_loader=lambda: dict(policy["v"]),
                                sampler=lambda: _sample(clock), clock=clock)
    assert rt.overlay(wall_clock=lambda: 1.0)["admit"] is True
    policy["v"] = dict(ah.DEFAULT_POLICY)
    assert rt.overlay(wall_clock=lambda: 2.0) is None


def test_runtime_invalid_policy_publishes_a_fence_not_silence():
    clock = Clock()
    def bad():
        raise ah.AdaptiveHostError("adaptive_policy_invalid")
    rt = ah.AdaptiveHostRuntime(policy_loader=bad, sampler=lambda: _sample(clock), clock=clock)
    doc = rt.overlay(wall_clock=lambda: 1.0)
    assert doc["admit"] is False and doc["refusal"] == "adaptive_policy_invalid"


def test_runtime_overlay_is_thread_safe_single_sample_per_interval():
    clock = Clock()
    calls = []
    rt = ah.AdaptiveHostRuntime(policy_loader=lambda: _enabled(),
                                sampler=lambda: calls.append(1) or _sample(clock), clock=clock)
    threads = [threading.Thread(target=lambda: rt.overlay(wall_clock=lambda: 1.0)) for _ in range(8)]
    [t.start() for t in threads]; [t.join() for t in threads]
    assert len(calls) == 1


def test_publisher_writes_overlay_throttled_and_clears_when_off(tmp_path):
    clock = Clock()
    wall = Clock(500.0)
    policy = {"v": _enabled()}
    rt = ah.AdaptiveHostRuntime(policy_loader=lambda: dict(policy["v"]),
                                sampler=lambda: _sample(clock), clock=clock)
    pub = ah.OverlayPublisher(tmp_path, rt.overlay, wall_clock=wall, interval_s=1.0)
    pub.tick()
    assert (tmp_path / ov.OVERLAY_FILENAME).exists()
    first = (tmp_path / ov.OVERLAY_FILENAME).read_bytes()
    wall.now += 0.2; clock.now += 0.2
    pub.tick()
    assert (tmp_path / ov.OVERLAY_FILENAME).read_bytes() == first  # throttled
    policy["v"] = dict(ah.DEFAULT_POLICY)
    wall.now += 2; clock.now += 2
    pub.tick()
    assert not (tmp_path / ov.OVERLAY_FILENAME).exists()


def test_publisher_provider_failure_publishes_a_fence(tmp_path):
    wall = Clock(500.0)
    def boom(*, wall_clock):
        raise RuntimeError("x")
    pub = ah.OverlayPublisher(tmp_path, boom, wall_clock=wall, interval_s=1.0)
    pub.tick()
    gate = ov.read_gate(tmp_path, wall_clock=wall)
    assert gate["admit"] is False and gate["refusal"] == "adaptive_controller_stale"


def test_app_overlay_provider_is_none_when_policy_absent(tmp_path):
    class FakeApp:
        root = tmp_path
    assert ah.adaptive_overlay_provider(FakeApp())(wall_clock=lambda: 1.0) is None
    assert not (tmp_path / ov.OVERLAY_FILENAME).exists()


def test_app_overlay_provider_reads_owner_policy(tmp_path):
    ah.write_adaptive_policy(tmp_path, _enabled(), owner_authorized=True)
    class FakeApp:
        root = tmp_path
        _adaptive_clock = Clock()
        _adaptive_sampler = staticmethod(lambda: _sample(FakeApp._adaptive_clock))
    doc = ah.adaptive_overlay_provider(FakeApp())(wall_clock=lambda: 1.0)
    assert doc["admit"] is True


# ------------------------------------------------- managed runtime publisher
def test_managed_runtime_publishes_overlay_into_its_node_root_only_when_given_a_provider(tmp_path):
    from test_control_pump import _world
    from test_managed_runtime import runtime as make_runtime

    with _world(tmp_path / "world") as world:
        plain = make_runtime(tmp_path / "plain", world)
        assert plain._adaptive is None  # default: nothing is ever published
        assert not (plain.root / ov.OVERLAY_FILENAME).exists()
        calls = []
        provider = lambda *, wall_clock: calls.append(1) or _doc(
            issued_at=wall_clock(), expires_at=wall_clock() + 6)
        wired = make_runtime(tmp_path / "wired", world, adaptive_overlay=provider)
        wired._adaptive.tick()
        assert ov.read_gate(wired.root)["admit"] is True
        assert calls == [1]


def test_demo_app_hands_target_application_the_overlay_provider(tmp_path):
    from mycelium_a13_macos.app import DemoApp
    from mycelium_a13_macos.target_app import TargetApplication

    assert TargetApplication.adaptive_overlay is None  # class default: no publication
    assert callable(DemoApp._adaptive_overlay)


def test_node_construction_reads_overlay_from_the_capacity_root():
    import inspect
    import physical_inference_node as node

    source = inspect.getsource(node)
    assert "adaptive_gate=lambda: _read_adaptive_gate(self.capacity_root)" in source
    assert "adaptive_node_id=placement.node_id" in source
