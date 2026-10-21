# SPDX-License-Identifier: AGPL-3.0-only
"""A16 macOS collectors: memory_pressure cross-check and aggregate CPU utilization.

Read-only and unprivileged.  Parsing is tolerant by design: the utility's prose is not a stable
ABI, so an unparseable cross-check is *absent* (the kernel pressure level still stands), while a
parseable contradiction yields ``unknown`` through ``classify_sample``.
"""
from __future__ import annotations

import sys

import pytest

from mycelium_a13_macos import adaptive_host as ah

GIB = 1 << 30
VM_STAT = (
    "Mach Virtual Memory Statistics: (page size of 16384 bytes)\n"
    "Pages free:                               1000.\n"
    "Pages inactive:                           1000.\n"
    "Pages speculative:                        1000.\n"
)


def _mp(percent):
    return f"The system has 51539607552 (3145728 pages with a page size of 16384).\n\nSystem-wide memory free percentage: {percent}%\n"


def _runner(level="1", mp=None, vm=VM_STAT):
    def run(argv):
        if argv[0] == "sysctl":
            if level is None:
                raise OSError("no sysctl")
            return level + "\n"
        if argv[0] == "vm_stat":
            return vm
        if argv[0] == "memory_pressure":
            if mp is None:
                raise OSError("no memory_pressure")
            return mp
        raise AssertionError(argv)
    return run


def test_crosscheck_maps_only_the_extremes_and_never_guesses_from_prose():
    assert ah.parse_memory_pressure(_mp(89)) == "normal"
    assert ah.parse_memory_pressure(_mp(3)) == "critical"
    assert ah.parse_memory_pressure(_mp(30)) == "warn"
    for garbage in ("", "System-wide memory free percentage: lots", "free: 5%", "System-wide memory free percentage: 140%"):
        assert ah.parse_memory_pressure(garbage) is None


def test_sample_carries_the_crosscheck_when_parseable_and_omits_it_otherwise():
    clock = lambda: 5.0
    ok = ah.sample_host_memory(_runner("1", _mp(89)), clock)
    assert ok["pressure"] == "normal" and ok["pressure_crosscheck"] == "normal"
    assert "pressure_crosscheck" not in ah.sample_host_memory(_runner("1", None), clock)
    assert "pressure_crosscheck" not in ah.sample_host_memory(_runner("1", "garbage"), clock)
    ah.parse_sample(ok)  # still inside the allow-listed schema


@pytest.mark.parametrize("level,percent", [("1", 2), ("4", 90)])
def test_contradictory_probes_are_unknown_never_healthy(level, percent):
    sample = ah.sample_host_memory(_runner(level, _mp(percent)), lambda: 10.0)
    cls = ah.classify_sample(sample, now=10.0, timings=ah.DEFAULT_TIMINGS, footprint_bytes=0, reserve_bytes=0)
    assert cls == "unknown"


def test_agreeing_probes_stay_healthy():
    sample = ah.sample_host_memory(_runner("1", _mp(89)), lambda: 10.0)
    cls = ah.classify_sample(sample, now=10.0, timings=ah.DEFAULT_TIMINGS, footprint_bytes=0, reserve_bytes=0)
    assert cls == "healthy"


def test_failed_primary_probe_still_wins_over_a_crosscheck():
    assert ah.sample_host_memory(_runner(None, _mp(89)), lambda: 1.0)["pressure"] == "unknown"


# ----------------------------------------------------------------------------- CPU
def _meter(*readings):
    items = list(readings)

    def read():
        value = items.pop(0)
        if isinstance(value, Exception):
            raise value
        return value
    return ah.CpuMeter(read)


def test_cpu_meter_uses_tick_deltas_and_is_unknown_until_it_has_two_reads():
    meter = _meter((100, 100, 800, 0), (200, 100, 900, 0), (200, 100, 900, 0), (300, 100, 900, 0))
    assert meter.utilization() is None            # first read: nothing to compare with
    assert meter.utilization() == pytest.approx(0.5)   # +100 busy, +100 idle
    assert meter.utilization() is None            # no ticks elapsed: unknown, not zero
    assert meter.utilization() == pytest.approx(1.0)


def test_cpu_meter_refuses_counter_regression_and_read_failure():
    meter = _meter((500, 0, 500, 0), (100, 0, 100, 0), OSError("host_statistics"), (200, 0, 200, 0))
    meter.utilization()
    assert meter.utilization() is None            # counters went backwards (wrap/restart)
    assert meter.utilization() is None            # failed read
    assert meter.utilization() is None            # first read after a failure has no baseline


def test_host_sampler_composes_memory_and_cpu_inside_the_allow_list():
    sampler = ah.HostSampler(runner=_runner("1", _mp(89)), clock=lambda: 7.0,
                             meter=_meter((0, 0, 100, 0), (50, 0, 150, 0)))
    first, second = sampler(), sampler()
    assert "cpu_utilization" not in first            # unknown, not zero
    assert second["cpu_utilization"] == pytest.approx(0.5)
    assert set(second) <= ah._SAMPLE_KEYS
    ah.parse_sample(first), ah.parse_sample(second)


def test_default_app_sampler_is_the_host_sampler_and_an_override_still_wins():
    class App:
        root = "unused"

    app = App()
    assert isinstance(ah.sampler_for(app), ah.HostSampler)
    assert ah.sampler_for(app) is ah.sampler_for(app)    # one meter per app, so deltas are real
    app._adaptive_sampler = lambda: {"observed_at": 1.0, "pressure": "normal"}
    assert ah.sampler_for(app)() == {"observed_at": 1.0, "pressure": "normal"}


@pytest.mark.skipif(sys.platform != "darwin", reason="host_statistics is macOS")
def test_real_tick_reader_returns_monotonic_counters_without_privileges():
    first, second = ah.read_cpu_ticks(), ah.read_cpu_ticks()
    assert len(first) == 4 and all(type(x) is int and x >= 0 for x in first)
    assert all(b >= a for a, b in zip(first, second))
