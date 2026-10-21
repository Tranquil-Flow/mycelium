# SPDX-License-Identifier: AGPL-3.0-only
"""The host-speed factor that scales test deadlines on a slow or loaded machine.

A deadline in a test is a hang guard, not a claim: scaling it by how slow the host measures right now keeps every
assertion and only stops a loaded host (Spotlight, other suites) from turning a hang guard into a failure.
"""
from __future__ import annotations

import time

import pytest

from tests import host_speed as hs


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    monkeypatch.delenv(hs.ENV_OVERRIDE, raising=False)
    hs.reset()
    yield
    hs.reset()


def test_a_host_as_fast_as_the_baseline_scales_nothing():
    assert hs.host_speed_factor(measure=lambda: (hs.BASELINE_SPIN_SECONDS, hs.BASELINE_SPAWN_SECONDS)) == 1.0
    assert hs.scaled(2.5) == 2.5


def test_a_faster_host_is_never_scaled_below_one():
    assert hs.host_speed_factor(measure=lambda: (hs.BASELINE_SPIN_SECONDS / 4, hs.BASELINE_SPAWN_SECONDS / 4)) == 1.0


def test_the_slower_of_cpu_and_process_start_decides():
    slow_spawn = lambda: (hs.BASELINE_SPIN_SECONDS, hs.BASELINE_SPAWN_SECONDS * 4)
    assert hs.host_speed_factor(measure=slow_spawn) == pytest.approx(4.0)
    hs.reset()
    slow_cpu = lambda: (hs.BASELINE_SPIN_SECONDS * 6, hs.BASELINE_SPAWN_SECONDS)
    assert hs.host_speed_factor(measure=slow_cpu) == pytest.approx(6.0)


def test_the_factor_is_capped_so_a_wedged_host_cannot_make_a_hang_guard_infinite():
    factor = hs.host_speed_factor(measure=lambda: (hs.BASELINE_SPIN_SECONDS * 1000, hs.BASELINE_SPAWN_SECONDS * 1000))
    assert factor == hs.MAX_FACTOR


def test_scaled_deadlines_never_shrink_and_respect_a_ceiling():
    hs.host_speed_factor(measure=lambda: (hs.BASELINE_SPIN_SECONDS * 3, hs.BASELINE_SPAWN_SECONDS))
    assert hs.scaled(2.0) == pytest.approx(6.0)
    assert hs.scaled(2.0, ceiling=4.0) == 4.0          # a bound that must stay below an interval
    assert hs.scaled(2.0, ceiling=1.0) == 2.0          # a ceiling below the base never shrinks the base


def test_the_factor_is_cached_for_a_short_while_then_measured_again():
    clock = FakeClock()
    calls = []

    def measure():
        calls.append(1)
        return hs.BASELINE_SPIN_SECONDS * len(calls), hs.BASELINE_SPAWN_SECONDS

    assert hs.host_speed_factor(measure=measure, clock=clock) == 1.0
    clock.now += hs.CACHE_SECONDS - 1
    assert hs.host_speed_factor(measure=measure, clock=clock) == 1.0 and len(calls) == 1
    clock.now += 2
    assert hs.host_speed_factor(measure=measure, clock=clock) == 2.0 and len(calls) == 2


def test_an_environment_override_wins_and_is_clamped(monkeypatch):
    monkeypatch.setenv(hs.ENV_OVERRIDE, "3.5")
    assert hs.host_speed_factor(measure=lambda: pytest.fail("must not measure")) == 3.5
    monkeypatch.setenv(hs.ENV_OVERRIDE, "0.2")
    hs.reset()
    assert hs.host_speed_factor() == 1.0
    monkeypatch.setenv(hs.ENV_OVERRIDE, "9999")
    hs.reset()
    assert hs.host_speed_factor() == hs.MAX_FACTOR
    monkeypatch.setenv(hs.ENV_OVERRIDE, "not-a-number")
    hs.reset()
    assert hs.host_speed_factor(measure=lambda: (hs.BASELINE_SPIN_SECONDS, hs.BASELINE_SPAWN_SECONDS)) == 1.0


def test_the_real_measurement_is_finite_cheap_and_at_least_one():
    started = time.perf_counter()
    factor = hs.host_speed_factor()
    assert 1.0 <= factor <= hs.MAX_FACTOR
    assert time.perf_counter() - started < 5.0


def test_wait_until_returns_the_moment_the_condition_holds():
    clock = FakeClock()
    ticks = iter([False, False, True])
    assert hs.wait_until(lambda: next(ticks), 10, what="x", clock=clock, sleep=clock.sleep) is True
    assert clock.now < 1.0


def test_wait_until_scales_its_deadline_and_still_fails_a_condition_that_never_holds():
    hs.host_speed_factor(measure=lambda: (hs.BASELINE_SPIN_SECONDS * 4, hs.BASELINE_SPAWN_SECONDS))
    clock = FakeClock()
    with pytest.raises(TimeoutError, match="never became true: the thing"):
        hs.wait_until(lambda: False, 1.0, what="the thing", clock=clock, sleep=clock.sleep)
    assert 4.0 <= clock.now < 4.5  # the 1 s guard became 4 s on a 4x-slow host, and still expired
