# SPDX-License-Identifier: AGPL-3.0-or-later
"""PV25: idle -> unloaded -> warming -> loaded state machine for one stage worker's weights."""
from __future__ import annotations

import threading

import pytest

from mycelium_idle.residency import MemberWarming, ResidencyError, WeightResidency


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class Host:
    """Counts release/restore; restore blocks on an event so tests can hold a reload open."""

    def __init__(self, clock):
        self.clock = clock
        self.releases = 0
        self.restores = 0
        self.gate = threading.Event()
        self.gate.set()
        self.fail_with: BaseException | None = None
        self.reload_takes_s = 2.5
        self.rss = 7_000_000_000

    def release(self):
        self.releases += 1
        self.rss = 150_000_000

    def restore(self):
        self.restores += 1
        self.gate.wait(5)
        if self.fail_with is not None:
            raise self.fail_with
        self.clock.now += self.reload_takes_s
        self.rss = 7_000_000_000


def make(minutes=10, **kwargs):
    clock = Clock()
    host = Host(clock)
    box = {"minutes": minutes}
    failures = []
    residency = WeightResidency(
        release=host.release, restore=host.restore,
        unload_after_s=lambda: None if box["minutes"] is None else box["minutes"] * 60.0,
        clock=clock, rss_bytes=lambda: host.rss, on_reload_failed=failures.append, **kwargs)
    return residency, host, clock, box, failures


def test_loaded_member_is_not_unloaded_before_the_idle_deadline():
    residency, host, clock, _, _ = make()
    clock.now += 599
    assert residency.tick() == "loaded" and host.releases == 0


def test_unloads_after_n_idle_minutes_and_reports_idle_weights_unloaded():
    residency, host, clock, _, _ = make()
    clock.now += 600
    assert residency.tick() == "idle" and host.releases == 1
    snap = residency.snapshot()
    assert snap["state"] == "idle" and snap["line"] == "idle (weights unloaded)"
    assert snap["rss_before_unload_bytes"] == 7_000_000_000
    assert snap["rss_after_unload_bytes"] == 150_000_000


def test_activity_resets_the_idle_clock():
    residency, host, clock, _, _ = make()
    clock.now += 500
    residency.note_activity()
    clock.now += 500
    assert residency.tick() == "loaded"
    clock.now += 100
    assert residency.tick() == "idle"


def test_never_does_not_unload():
    residency, host, clock, _, _ = make(minutes=None)
    clock.now += 10**7
    assert residency.tick() == "loaded" and host.releases == 0


def test_the_setting_applies_live():
    residency, host, clock, box, _ = make(minutes=60)
    clock.now += 700
    assert residency.tick() == "loaded"
    box["minutes"] = 10
    assert residency.tick() == "idle"


def test_never_unloads_with_a_request_in_flight():
    inflight = {"n": 1}
    residency, host, clock, _, _ = make(inflight=lambda: inflight["n"])
    clock.now += 10_000
    assert residency.tick() == "loaded" and host.releases == 0
    inflight["n"] = 0
    assert residency.tick() == "idle"


def test_ensure_resident_reloads_lazily_and_records_reload_time():
    residency, host, clock, _, _ = make()
    clock.now += 600
    residency.tick()
    assert residency.ensure_resident(timeout_s=5.0) == "loaded"
    assert host.restores == 1
    snap = residency.snapshot()
    assert snap["state"] == "loaded" and snap["reload_count"] == 1
    assert snap["last_reload_ms"] == 2500 and snap["reload_ms_history"] == [2500]
    assert snap["rss_after_reload_bytes"] == 7_000_000_000


def test_a_reload_does_not_count_as_idle_time_afterwards():
    residency, host, clock, _, _ = make()
    clock.now += 600
    residency.tick()
    residency.ensure_resident(timeout_s=5.0)
    clock.now += 5
    assert residency.tick() == "loaded"


def test_loaded_member_ensure_resident_is_free_and_counts_as_activity():
    residency, host, clock, _, _ = make()
    clock.now += 599
    assert residency.ensure_resident(timeout_s=1.0) == "loaded"
    clock.now += 599
    assert residency.tick() == "loaded" and host.restores == 0


def test_slow_reload_is_warming_and_the_wait_raises_member_warming_without_failing():
    residency, host, clock, _, failures = make()
    clock.now += 600
    residency.tick()
    host.gate.clear()
    with pytest.raises(MemberWarming) as error:
        residency.ensure_resident(timeout_s=0.05)
    assert error.value.code == "member_warming"
    assert residency.snapshot()["state"] == "warming"
    assert residency.snapshot()["line"] == "warming (reloading weights)"
    assert failures == []
    host.gate.set()
    assert residency.wait_loaded(timeout_s=5.0) == "loaded"
    assert host.restores == 1


def test_concurrent_requests_share_one_reload():
    residency, host, clock, _, _ = make()
    clock.now += 600
    residency.tick()
    host.gate.clear()
    results = []
    threads = [threading.Thread(target=lambda: results.append(residency.ensure_resident(timeout_s=5.0)))
               for _ in range(4)]
    for thread in threads:
        thread.start()
    host.gate.set()
    for thread in threads:
        thread.join(5)
    assert results == ["loaded"] * 4 and host.restores == 1


def test_a_failed_reload_is_reported_and_fails_closed_through_the_callback():
    residency, host, clock, _, failures = make()
    clock.now += 600
    residency.tick()
    host.fail_with = OSError("disk")
    with pytest.raises(ResidencyError) as error:
        residency.ensure_resident(timeout_s=5.0)
    assert error.value.code == "member_reload_failed"
    assert residency.snapshot()["state"] == "failed"
    assert failures == ["member_reload_failed"]


def test_a_reload_that_never_finishes_is_failed_after_the_hard_deadline():
    residency, host, clock, _, failures = make(reload_deadline_s=120.0)
    clock.now += 600
    residency.tick()
    host.gate.clear()
    residency.begin_reload()
    clock.now += 119
    assert residency.tick() == "warming" and failures == []
    clock.now += 2
    assert residency.tick() == "failed"
    assert failures == ["member_reload_timeout"]
    host.gate.set()


def test_begin_reload_is_a_noop_while_loaded_and_idempotent_while_warming():
    residency, host, clock, _, _ = make()
    assert residency.begin_reload() == "loaded" and host.restores == 0
    clock.now += 600
    residency.tick()
    host.gate.clear()
    assert residency.begin_reload() == "warming"
    assert residency.begin_reload() == "warming"
    host.gate.set()
    residency.wait_loaded(timeout_s=5.0)
    assert host.restores == 1


def test_manual_unload_refuses_while_busy_and_force_is_not_available_with_inflight():
    residency, host, clock, _, _ = make(inflight=lambda: 2)
    assert residency.unload() is False and host.releases == 0


def test_history_is_bounded():
    residency, host, clock, _, _ = make()
    for _ in range(40):
        clock.now += 600
        residency.tick()
        residency.ensure_resident(timeout_s=5.0)
    snap = residency.snapshot()
    assert snap["reload_count"] == 40 and len(snap["reload_ms_history"]) == 16
