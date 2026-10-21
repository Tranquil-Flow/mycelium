# SPDX-License-Identifier: AGPL-3.0-only
"""Member-side leave: ``Leave smoothly`` (bounded drain, then withdraw) and ``Instant leave``.

The state machine is exercised with fakes and a fake clock; the route tests join a real
target application through the owner's pairing window (loopback fixtures, no network).
"""
from __future__ import annotations

import json

import pytest

from mycelium_a13_macos import adaptive_host
from mycelium_a13_macos.member_leave import (LEAVE_SMOOTH_CAP_S, LEAVE_SMOOTH_STALL_S, LeaveError,
                                             LeaveSteps, MemberLeave)
from tests.a13_macos.test_join_window_flow import CONSENT, STANDING, _owner, _target


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


class Recorder:
    """Fake seams that log the order of every call."""

    def __init__(self, inflight=(0,), *, handoff=False, fail=()) -> None:
        self.log: list[str] = []
        self._inflight = list(inflight)
        self._fail = set(fail)
        self.handoff_available = handoff

    def _do(self, name):
        self.log.append(name)
        if name in self._fail:
            raise LeaveError("step_failed")

    def steps(self) -> LeaveSteps:
        def inflight() -> int:
            self.log.append("inflight")
            return self._inflight.pop(0) if len(self._inflight) > 1 else self._inflight[0]

        def handoff() -> bool:
            self._do("handoff")
            return True

        return LeaveSteps(
            fence=lambda: self._do("fence"), inflight=inflight,
            handoff=handoff if self.handoff_available else None,
            unload=lambda: self._do("unload"),
            publish_withdrawal=lambda: self._do("publish_withdrawal"),
            withdraw_consent=lambda: self._do("withdraw_consent"),
            disconnect=lambda: self._do("disconnect"),
            cancel_work=lambda: self._do("cancel_work"))


def _leave(tmp_path, recorder, clock=None):
    clock = clock or Clock()
    tmp_path.mkdir(parents=True, exist_ok=True)
    return MemberLeave(tmp_path, recorder.steps(), clock=clock, sleep=clock.sleep), clock


def _names(log):
    return [entry for entry in log if entry != "inflight"]


# -- Instant leave -------------------------------------------------------------

def test_instant_leave_withdraws_consent_disconnects_and_cancels_even_if_a_step_fails(tmp_path):
    recorder = Recorder(fail={"withdraw_consent", "disconnect"})
    leave, _ = _leave(tmp_path, recorder)
    result = leave.run("instant")
    assert _names(recorder.log) == ["fence", "withdraw_consent", "disconnect", "cancel_work"]
    assert result["state"] == "left" and result["mode"] == "instant"
    failed = {row["name"]: row["code"] for row in result["steps"] if not row["ok"]}
    assert failed == {"withdraw_consent": "step_failed", "disconnect": "step_failed"}
    assert leave.status()["state"] == "left"


def test_instant_leave_does_not_drain_or_wait(tmp_path):
    recorder = Recorder(inflight=(3,))
    leave, clock = _leave(tmp_path, recorder)
    leave.run("instant")
    assert "inflight" not in recorder.log and "unload" not in recorder.log
    assert clock.now == 1000.0


# -- Leave smoothly --------------------------------------------------------------

def test_smooth_leave_drains_then_publishes_withdrawal_then_leaves_in_order(tmp_path):
    recorder = Recorder(inflight=(2, 1, 0))
    leave, _ = _leave(tmp_path, recorder)
    result = leave.run("smooth")
    assert _names(recorder.log) == ["fence", "unload", "publish_withdrawal",
                                    "withdraw_consent", "disconnect", "cancel_work"]
    assert recorder.log.count("inflight") == 3          # polled until it reached zero
    assert result["state"] == "left" and result["fallback"] is None
    assert result["handoff"] == "unavailable"


def test_smooth_leave_calls_handover_hook_when_available_and_skips_it_when_not(tmp_path):
    with_hook = Recorder(inflight=(1, 0), handoff=True)
    result = _leave(tmp_path / "a", with_hook)[0].run("smooth")
    assert _names(with_hook.log)[:3] == ["fence", "handoff", "unload"]
    assert result["handoff"] == "done"
    without = Recorder(inflight=(1, 0))
    result = _leave(tmp_path / "b", without)[0].run("smooth")
    assert "handoff" not in without.log and result["handoff"] == "unavailable"


def test_smooth_leave_stall_falls_back_to_instant_within_bound(tmp_path):
    recorder = Recorder(inflight=(2,))                  # never shrinks
    leave, clock = _leave(tmp_path, recorder)
    result = leave.run("smooth")
    assert result["fallback"] == "stall" and result["state"] == "left"
    assert LEAVE_SMOOTH_STALL_S == 30
    assert 30 <= clock.now - 1000.0 <= 30 + 5           # within one poll of the stall bound
    assert "unload" not in recorder.log                 # never unloads under a live request
    assert _names(recorder.log)[-3:] == ["withdraw_consent", "disconnect", "cancel_work"]


def test_smooth_leave_progress_resets_the_stall_timer_but_total_cap_applies(tmp_path):
    slow = [n for n in range(10, 3, -1) for _ in range(50)]   # one step per ~25 s at 0.5 s polls
    recorder = Recorder(inflight=slow)
    leave, clock = _leave(tmp_path, recorder)
    result = leave.run("smooth")
    assert result["fallback"] == "cap" and LEAVE_SMOOTH_CAP_S == 120
    assert clock.now - 1000.0 <= 120 + 5


def test_smooth_leave_works_with_adaptive_sharing_off(tmp_path):
    # The steps never need an adaptive controller; the fence is the app's own.
    recorder = Recorder(inflight=(0,))
    result = _leave(tmp_path, recorder)[0].run("smooth")
    assert result["state"] == "left"


def test_leave_state_survives_restart_and_resumes(tmp_path):
    recorder = Recorder(inflight=(2,))
    clock = Clock()
    # A crash while draining: the state file says draining and nothing else happened.
    (tmp_path / "leave-state.json").write_text(json.dumps({
        "protocol": "mycelium.a13_member_leave.v1", "mode": "smooth", "state": "draining",
        "started_at": 1.0, "fallback": None, "steps": [], "handoff": "unavailable"}))
    result = MemberLeave(tmp_path, recorder.steps(), clock=clock, sleep=clock.sleep).resume()
    assert result["state"] == "left" and result["fallback"] == "restart"
    assert _names(recorder.log) == ["fence", "withdraw_consent", "disconnect", "cancel_work"]
    # Once left, a later start does nothing.
    again = Recorder()
    assert MemberLeave(tmp_path, again.steps(), clock=clock, sleep=clock.sleep).resume() is None
    assert again.log == []


def test_modes_are_closed_and_runs_are_idempotent(tmp_path):
    leave, _ = _leave(tmp_path, Recorder())
    with pytest.raises(LeaveError) as caught:
        leave.run("gentle")
    assert caught.value.code == "leave_mode_invalid"
    leave.run("instant")
    second = MemberLeave(tmp_path, Recorder().steps()).run("instant")
    assert second["state"] == "left"                    # re-running is safe


def test_leave_does_not_widen_closed_reason_enum():
    assert adaptive_host.REASON_CATEGORIES == frozenset({
        "none", "owner_game_mode", "owner_stop", "host_memory_pressure", "host_cpu_saturation"})


# -- the application route -------------------------------------------------------

def _joined_pair(tmp_path, monkeypatch):
    owner = _owner(tmp_path, monkeypatch, "https://relay-fixture.invalid")
    target = _target(tmp_path, owner)
    opened = owner.action("/api/pair/window/open", {"ttl_seconds": 600})
    target.action("/api/join/window", {"consent": CONSENT, "standing_consent": STANDING,
                                       "join": opened["join_link"]})
    [window] = owner.action("/api/pair/window/list", {})["windows"]
    owner.action("/api/pair/window/approve", {"window_id": window["window_id"],
                                              "request_digest": window["pending"]["request_digest"]})
    assert target.action("/api/join/window/poll", {})["state"] == "joined"
    return owner, target


def test_leave_requires_confirm_and_strict_keys(tmp_path, monkeypatch):
    owner, target = _joined_pair(tmp_path, monkeypatch)
    try:
        for body, code in [({"mode": "instant"}, "leave_confirmation_required"),
                           ({"mode": "instant", "confirm": False}, "leave_confirmation_required"),
                           ({"mode": "instant", "confirm": True, "extra": 1}, "invalid_request"),
                           ({"mode": "gentle", "confirm": True}, "leave_mode_invalid"),
                           ({}, "invalid_request")]:
            with pytest.raises(Exception) as caught:
                target.action("/api/leave", body)
            assert str(caught.value) == code, body
        with pytest.raises(Exception) as caught:
            owner.action("/api/leave", {"mode": "instant", "confirm": True})
        assert str(caught.value) == "target_role_required"
        assert target.status()["authority"]["membership_state"] == "current"   # nothing happened
    finally:
        target.close(); owner.close()


def test_instant_leave_route_withdraws_consent_stops_renewal_and_owner_survives(tmp_path, monkeypatch):
    owner, target = _joined_pair(tmp_path, monkeypatch)
    try:
        assert target.status()["join_flow"]["standing_consent"]["state"] == "granted"
        result = target.action("/api/leave", {"mode": "instant", "confirm": True})
        assert result["state"] == "left" and result["mode"] == "instant"
        status = target.status()
        assert status["join_flow"]["standing_consent"]["state"] == "withdrawn"
        assert status["leave"]["state"] == "left"
        assert target._resource_check() == "owner_stop"      # nothing new is admitted
        assert target._left is True                           # lease renewal has stopped
        assert owner.status()["authority"]["swarm_created"] is True   # owner unaffected
        # The owner still sees the Mac as a member until its lease ends; revoking it works.
        [member] = [m for m in owner.status()["authority"]["members"]
                    if m["node_id"] == target.authority.member.node_id]
        revoked = owner.action("/api/revoke", {"node_id": member["node_id"],
                                               "generation": member["generation"]})
        assert revoked["route_ready"] is False
        assert owner.status()["authority"]["swarm_created"] is True
    finally:
        target.close(); owner.close()


def test_smooth_leave_route_runs_in_background_and_reaches_left(tmp_path, monkeypatch):
    owner, target = _joined_pair(tmp_path, monkeypatch)
    try:
        started = target.action("/api/leave", {"mode": "smooth", "confirm": True})
        assert started["mode"] == "smooth" and started["state"] in {"draining", "left"}
        thread = target._leave_thread
        thread.join(timeout=60)
        assert not thread.is_alive()
        assert target.status()["leave"]["state"] == "left"
        assert target.status()["join_flow"]["standing_consent"]["state"] == "withdrawn"
        assert target.status()["leave"]["fallback"] is None    # nothing in flight: no stall
    finally:
        target.close(); owner.close()


def test_leave_state_file_is_private_and_restart_resumes(tmp_path, monkeypatch):
    owner, target = _joined_pair(tmp_path, monkeypatch)
    try:
        path = target.root / "leave-state.json"
        path.write_text(json.dumps({
            "protocol": "mycelium.a13_member_leave.v1", "mode": "smooth", "state": "draining",
            "started_at": 1.0, "fallback": None, "steps": [], "handoff": "unavailable"}))
        path.chmod(0o600)
        result = target._resume_leave()
        assert result["state"] == "left" and result["fallback"] == "restart"
        assert target.status()["join_flow"]["standing_consent"]["state"] == "withdrawn"
        assert oct(path.stat().st_mode & 0o777) == "0o600"
    finally:
        target.close(); owner.close()
