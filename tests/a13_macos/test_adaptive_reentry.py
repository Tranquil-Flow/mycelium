# SPDX-License-Identifier: AGPL-3.0-only
"""A16 re-entry feeds: real authority round trips drive mark_requalified / record_authority_observation.

Deterministic: fake clock, fake authority (the TargetApplication ``renew`` / ``report_capability``
/ ``status`` / ``member`` surface).  No device, no network.
"""
from __future__ import annotations

import pytest

from mycelium_a13_macos import adaptive_host as ah
from mycelium_a13_macos import adaptive_reentry as re_

GIB = 1 << 30


class Clock:
    def __init__(self, now=1000.0):
        self.now = now

    def __call__(self):
        return self.now


class Authority:
    def __init__(self, member=True, renew_ok=True, report_ok=True):
        self.member = object() if member else None
        self.renew_ok, self.report_ok = renew_ok, report_ok
        self.renews = self.reports = 0

    def renew(self):
        self.renews += 1
        if not self.renew_ok:
            raise RuntimeError("peer_renewal_unavailable")
        return {}

    def status(self):
        return {"membership_state": "current", "lease_state": "fresh"}

    def report_capability(self):
        self.reports += 1
        if not self.report_ok:
            raise ValueError("capability_backend_unavailable")
        return {"state": "self_report_accepted", "generation": 3, "expires_at": 9e9}


def _sample(clock, pressure="normal"):
    return {"observed_at": clock(), "pressure": pressure, "available_bytes": 8 * GIB}


def _withdrawn(clock):
    ctl = ah.AdaptiveHostController({**ah.DEFAULT_POLICY, "enabled": True}, clock=clock)
    ctl.observe(_sample(clock, "critical"))
    ctl.mark_cleanup_complete()
    clock.now += 2
    ctl.observe(_sample(clock, "critical"))
    assert ctl.mode == "withdraw"
    return ctl


def _calm(ctl, clock, seconds):
    end = clock.now + seconds
    while clock.now < end:
        clock.now += 2
        ctl.observe(_sample(clock))


def test_no_authority_traffic_until_the_host_has_been_calm_for_the_recovery_window():
    clock = Clock()
    ctl, auth, feed = _withdrawn(clock), Authority(), re_.ReentryFeed(clock=clock)
    assert feed.tick(ctl, auth)["action"] == "none"
    _calm(ctl, clock, 60)
    assert feed.tick(ctl, auth)["action"] == "none"
    assert auth.renews == 0 and auth.reports == 0


def test_real_round_trips_complete_reentry_after_calm_and_requalification():
    clock = Clock()
    ctl, auth, feed = _withdrawn(clock), Authority(), re_.ReentryFeed(clock=clock)
    _calm(ctl, clock, 130)
    for _ in range(4):
        feed.tick(ctl, auth)
        clock.now += 2
        ctl.observe(_sample(clock))
    assert auth.renews >= 2 and auth.reports == 1  # one eligibility renewal, not one per tick
    assert ctl.mode == "normal"


def test_rate_limited_to_one_authority_poll_per_sample_interval():
    clock = Clock()
    ctl, auth, feed = _withdrawn(clock), Authority(), re_.ReentryFeed(clock=clock)
    _calm(ctl, clock, 130)
    feed.tick(ctl, auth)
    assert feed.tick(ctl, auth)["action"] == "wait"
    assert auth.renews == 1


def test_failed_renewal_or_absent_member_records_no_observation_and_never_requalifies():
    clock = Clock()
    for authority in (Authority(renew_ok=False), Authority(member=False)):
        ctl, feed = _withdrawn(clock), re_.ReentryFeed(clock=clock)
        _calm(ctl, clock, 130)
        for _ in range(4):
            feed.tick(ctl, authority)
            clock.now += 2
            ctl.observe(_sample(clock))
        assert ctl.mode == "withdraw" and authority.reports == 0


def test_failed_capability_report_blocks_withdraw_exit():
    clock = Clock()
    ctl, auth, feed = _withdrawn(clock), Authority(report_ok=False), re_.ReentryFeed(clock=clock)
    _calm(ctl, clock, 130)
    for _ in range(5):
        feed.tick(ctl, auth)
        clock.now += 2
        ctl.observe(_sample(clock))
    assert auth.reports >= 1 and ctl.mode == "withdraw"


def test_owner_stop_and_game_mode_block_reentry_traffic():
    clock = Clock()
    ctl, auth, feed = _withdrawn(clock), Authority(), re_.ReentryFeed(clock=clock)
    ctl.stop_serving()
    _calm(ctl, clock, 130)
    assert feed.tick(ctl, auth)["action"] == "none" and auth.renews == 0
    ctl.resume_serving()
    ctl.apply_policy({**ah.DEFAULT_POLICY, "enabled": True, "game_mode": True})
    assert feed.tick(ctl, auth)["action"] == "none"


def test_off_controller_does_nothing():
    clock = Clock()
    ctl = ah.AdaptiveHostController(ah.DEFAULT_POLICY, clock=clock)
    auth = Authority()
    assert re_.ReentryFeed(clock=clock).tick(ctl, auth)["action"] == "none" and auth.renews == 0


def test_reduced_mode_recovers_with_observations_only_no_capability_report():
    clock = Clock()
    ctl = ah.AdaptiveHostController({**ah.DEFAULT_POLICY, "enabled": True}, clock=clock)
    ctl.observe(_sample(clock, "warn"))
    assert ctl.mode == "reduced"
    auth, feed = Authority(), re_.ReentryFeed(clock=clock)
    _calm(ctl, clock, 130)
    for _ in range(4):
        feed.tick(ctl, auth)
        clock.now += 2
        ctl.observe(_sample(clock))
    assert ctl.mode == "normal" and auth.reports == 0


def test_app_tick_runs_the_feed_with_the_target_authority(tmp_path):
    from mycelium_a13_macos.app import DemoApp

    clock = Clock()
    ctl = _withdrawn(clock)
    _calm(ctl, clock, 130)
    auth = Authority()

    class Stub:
        _adaptive_clock = clock
        authority = auth

        class _rt:
            controller = ctl
        _adaptive_runtime = _rt()

    class Runtime:
        root = tmp_path
        model_unloaded = True

    DemoApp._adaptive_drain(Stub(), Runtime())
    assert auth.renews == 1
