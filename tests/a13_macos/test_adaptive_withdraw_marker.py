# SPDX-License-Identifier: AGPL-3.0-only
"""A16 persisted withdraw marker: a restart after a withdraw starts fenced.

Deterministic only (fake clocks and samples).  The marker is a small, 0600, allow-listed,
expiring file next to the overlay in the node's capacity_root.  Any marker that is present
-- valid, expired, malformed, symlinked or loosely moded -- fences; only the controller's
own re-entry (calm window + authority observations + requalification) or an owner opt-out
removes it.
"""
from __future__ import annotations

import json
import os
import time

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


def _marker(tmp_path):
    return tmp_path / ov.MARKER_FILENAME


# ----------------------------------------------------------------- file protocol
def test_marker_roundtrip_is_private_small_and_allow_listed(tmp_path):
    ov.write_marker(tmp_path, reason="owner_game_mode", wall_now=100.0)
    path = _marker(tmp_path)
    assert oct(path.stat().st_mode & 0o777) == "0o600" and path.stat().st_size <= ov.MAX_MARKER_BYTES
    assert set(json.loads(path.read_text())) == ov.MARKER_KEYS
    state = ov.read_marker(tmp_path, wall_clock=lambda: 101.0)
    assert state == {"valid": True, "reason": "owner_game_mode"}
    ov.clear_marker(tmp_path)
    assert ov.read_marker(tmp_path, wall_clock=lambda: 101.0) is None


def test_marker_writer_refuses_non_allow_listed_content(tmp_path):
    for reason in ("firefox", "", None, "host_gpu"):
        with pytest.raises(ov.AdaptiveOverlayError):
            ov.write_marker(tmp_path, reason=reason, wall_now=100.0)
    assert not _marker(tmp_path).exists()


@pytest.mark.parametrize("breakage", ["symlink", "loose_mode", "oversize", "malformed", "extra_key",
                                      "expired", "bad_reason", "future", "hardlink"])
def test_any_unsafe_malformed_or_expired_marker_is_fenced_not_ignored(tmp_path, breakage):
    path = _marker(tmp_path)
    good = {"schema": 1, "reason": "host_memory_pressure", "withdrawn_at": 100.0, "expires_at": 130.0}
    if breakage == "symlink":
        target = tmp_path / "elsewhere"
        target.write_text(json.dumps(good))
        os.chmod(target, 0o600)
        path.symlink_to(target)
    else:
        doc = dict(good)
        raw = None
        if breakage == "extra_key":
            doc["app"] = "game"
        elif breakage == "bad_reason":
            doc["reason"] = "app_is_running"
        elif breakage == "expired":
            doc.update(withdrawn_at=10.0, expires_at=20.0)
        elif breakage == "future":
            doc.update(withdrawn_at=900.0, expires_at=930.0)
        elif breakage == "oversize":
            raw = json.dumps({**doc, "pad": "x" * 4096})
        elif breakage == "malformed":
            raw = "{not json"
        path.write_text(raw if raw is not None else json.dumps(doc))
        os.chmod(path, 0o644 if breakage == "loose_mode" else 0o600)
        if breakage == "hardlink":
            os.link(path, tmp_path / "second-name")
    state = ov.read_marker(tmp_path, wall_clock=lambda: 110.0)
    assert state == {"valid": False, "reason": "none"}


def test_read_gate_fences_on_a_marker_even_when_the_overlay_says_admit(tmp_path):
    wall = lambda: 101.0  # noqa: E731
    assert ov.read_gate(tmp_path, wall_clock=wall) is None  # opt-out: no files, no gate
    ov.write_marker(tmp_path, reason="owner_stop", wall_now=100.0)
    gate = ov.read_gate(tmp_path, wall_clock=wall)  # marker without overlay
    assert gate["admit"] is False and gate["refusal"] == "adaptive_host_withdrawn"
    ov.write_overlay(tmp_path, {"schema": 1, "mode": "normal", "reason": "none", "generation": 0,
                                "admit": True, "refusal": None, "capacity_scale": "full",
                                "new_kv_budget_bytes": None, "epoch": 5.0, "issued_at": 100.0,
                                "expires_at": 106.0})
    gate = ov.read_gate(tmp_path, wall_clock=wall)  # marker beats a permissive overlay
    assert gate["admit"] is False and gate["refusal"] == "adaptive_host_withdrawn"
    ov.write_overlay(tmp_path, ov.fence_document("adaptive_host_draining", wall_now=100.0))
    assert ov.read_gate(tmp_path, wall_clock=wall)["refusal"] == "adaptive_host_draining"
    ov.clear_marker(tmp_path)
    assert ov.read_gate(tmp_path, wall_clock=wall)["refusal"] == "adaptive_host_draining"


# ------------------------------------------------------------------- controller
def _restored(clock, reason="host_memory_pressure"):
    ctl = ah.AdaptiveHostController({**ah.DEFAULT_POLICY, "enabled": True}, clock=clock)
    ctl.restore_withdrawn(reason)
    return ctl


def test_restored_controller_is_withdrawn_and_needs_requalification():
    clock = Clock()
    ctl = _restored(clock)
    assert ctl.mode == "withdraw" and ctl.reason == "host_memory_pressure"
    assert ctl.admission() == "adaptive_host_withdrawn" and ctl.needs_requalification()
    assert ctl.restore_withdrawn("owner_stop") is None and ctl.reason == "host_memory_pressure"  # idempotent
    assert _restored(clock, "not-a-reason").reason == "none"


def _calm_with_evidence(ctl, clock, seconds):
    """Healthy samples every 2 s; feed authority observations and requalification throughout."""
    end = clock() + seconds
    while clock() < end:
        ctl.observe(_sample(clock))
        ctl.record_authority_observation(ctl.generation, observed_at=clock())
        ctl.mark_requalified(ctl.generation)
        clock.now += 2


def test_post_restart_reentry_needs_the_full_120s_calm_window_even_with_all_evidence():
    clock = Clock()
    ctl = _restored(clock)
    _calm_with_evidence(ctl, clock, 118)
    ctl.observe(_sample(clock))
    assert ctl.mode == "withdraw"  # healthy since t0; 118+ s < 120 s
    _calm_with_evidence(ctl, clock, 6)
    assert ctl.mode == "normal"


def test_pressure_after_restart_resets_the_calm_window():
    clock = Clock()
    ctl = _restored(clock)
    _calm_with_evidence(ctl, clock, 100)
    ctl.observe(_sample(clock, "warn", 2 * GIB))
    clock.now += 2
    _calm_with_evidence(ctl, clock, 100)
    assert ctl.mode == "withdraw"


# ------------------------------------------------------------ runtime + publisher
def _rt(clock, policy):
    return ah.AdaptiveHostRuntime(policy_loader=lambda: dict(policy["v"]),
                                  sampler=lambda: _sample(clock), clock=clock)


def _withdraw(rt, clock, wall, directory):
    pub = ah.OverlayPublisher(directory, rt.overlay, wall_clock=wall, interval_s=1.0)
    pub.tick()
    assert rt.controller.mode == "normal" and not _marker(directory).exists()
    rt.controller.apply_policy({**ah.DEFAULT_POLICY, "enabled": True, "game_mode": True})
    rt.controller.mark_cleanup_complete()
    clock.now += 2; wall.now += 2
    pub.tick()
    assert rt.controller.mode == "withdraw"
    return pub


def test_withdraw_writes_the_marker_and_a_restarted_process_starts_fenced(tmp_path):
    clock, wall = Clock(), Clock(5000.0)
    policy = {"v": {**ah.DEFAULT_POLICY, "enabled": True, "game_mode": False}}
    rt = _rt(clock, policy)
    _withdraw(rt, clock, wall, tmp_path)
    state = ov.read_marker(tmp_path, wall_clock=wall)
    assert state == {"valid": True, "reason": "owner_game_mode"}
    # "restart": a brand new runtime/controller over the same capacity_root; Game mode is off now
    clock2 = Clock(7000.0)
    rt2 = _rt(clock2, policy)
    wall.now += 30
    pub2 = ah.OverlayPublisher(tmp_path, rt2.overlay, wall_clock=wall, interval_s=1.0)
    pub2.tick()
    gate = ov.read_gate(tmp_path, wall_clock=wall)
    assert rt2.controller.mode == "withdraw"
    assert gate["admit"] is False and gate["refusal"] == "adaptive_host_withdrawn"
    assert ov.read_marker(tmp_path, wall_clock=wall)["valid"] is True  # refreshed, not dropped


def test_restart_reentry_clears_the_marker_only_after_the_calm_window_and_evidence(tmp_path):
    clock, wall = Clock(), Clock(5000.0)
    policy = {"v": {**ah.DEFAULT_POLICY, "enabled": True}}
    rt = _rt(clock, policy)
    _withdraw(rt, clock, wall, tmp_path)
    clock2 = Clock(9000.0)
    rt2 = _rt(clock2, policy)
    pub = ah.OverlayPublisher(tmp_path, rt2.overlay, wall_clock=wall, interval_s=1.0)
    for _ in range(59):  # 118 s healthy: not yet
        pub.tick()
        rt2.controller.record_authority_observation(rt2.controller.generation, observed_at=clock2())
        rt2.controller.mark_requalified(rt2.controller.generation)
        clock2.now += 2; wall.now += 2
    assert rt2.controller.mode == "withdraw" and _marker(tmp_path).exists()
    for _ in range(5):
        pub.tick()
        rt2.controller.record_authority_observation(rt2.controller.generation, observed_at=clock2())
        rt2.controller.mark_requalified(rt2.controller.generation)
        clock2.now += 2; wall.now += 2
    pub.tick()
    assert rt2.controller.mode == "normal"
    assert not _marker(tmp_path).exists()
    assert ov.read_gate(tmp_path, wall_clock=wall)["admit"] is True


def test_corrupt_or_expired_marker_still_starts_fenced(tmp_path):
    clock, wall = Clock(), Clock(5000.0)
    policy = {"v": {**ah.DEFAULT_POLICY, "enabled": True}}
    _marker(tmp_path).write_text("{garbage")
    os.chmod(_marker(tmp_path), 0o600)
    rt = _rt(clock, policy)
    ah.OverlayPublisher(tmp_path, rt.overlay, wall_clock=wall).tick()
    assert rt.controller.mode == "withdraw" and rt.controller.reason == "none"
    assert ov.read_gate(tmp_path, wall_clock=wall)["admit"] is False


def test_opting_out_removes_the_marker_and_the_node_is_ungated_again(tmp_path):
    clock, wall = Clock(), Clock(5000.0)
    policy = {"v": {**ah.DEFAULT_POLICY, "enabled": True}}
    rt = _rt(clock, policy)
    pub = _withdraw(rt, clock, wall, tmp_path)
    policy["v"] = dict(ah.DEFAULT_POLICY)  # owner turns A16 off
    clock.now += 2; wall.now += 2
    pub.tick()
    assert not _marker(tmp_path).exists() and not (tmp_path / ov.OVERLAY_FILENAME).exists()
    assert ov.read_gate(tmp_path, wall_clock=wall) is None


def test_a16_off_never_creates_a_marker(tmp_path):
    clock, wall = Clock(), Clock(5000.0)
    rt = _rt(clock, {"v": dict(ah.DEFAULT_POLICY)})
    ah.OverlayPublisher(tmp_path, rt.overlay, wall_clock=wall).tick()
    assert list(tmp_path.iterdir()) == []


def test_publisher_only_passes_the_capacity_root_to_providers_that_ask_for_it(tmp_path):
    seen = []
    ah.OverlayPublisher(tmp_path, lambda *, wall_clock: seen.append("old") or None, wall_clock=Clock()).tick()
    ah.OverlayPublisher(tmp_path, lambda *, wall_clock, capacity_root: seen.append(capacity_root) or None,
                        wall_clock=Clock()).tick()
    assert seen == ["old", tmp_path]


def test_resource_check_restores_from_the_attached_target_runtime_root(tmp_path):
    (tmp_path / "run").mkdir()
    ov.write_marker(tmp_path / "run", reason="owner_stop", wall_now=time.time())
    app_root = tmp_path / "app"
    app_root.mkdir()
    ah.write_adaptive_policy(app_root, {**ah.DEFAULT_POLICY, "enabled": True}, owner_authorized=True)

    class Runtime:
        root = tmp_path / "run"
        model_unloaded = False

    class Auth:
        _runtime = Runtime()

    class App:
        root = app_root
        authority = Auth()
        _adaptive_clock = Clock()
        _adaptive_sampler = staticmethod(lambda: _sample(Clock()))

    app = App()
    assert ah.adaptive_resource_check(app) == "adaptive_host_withdrawn"
    assert app._adaptive_runtime.controller.mode == "withdraw"
