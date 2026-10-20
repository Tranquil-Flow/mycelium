# SPDX-License-Identifier: AGPL-3.0-only
"""A16 host-memory sampler on Linux: /proc/meminfo + /proc/pressure/memory (PSI), fake file contents only."""
from __future__ import annotations

import pytest

from mycelium_a13_macos import adaptive_host as ah

MEMINFO = "MemTotal:       16000000 kB\nMemFree:          100000 kB\nMemAvailable:    {avail} kB\nBuffers: 1 kB\n"
PSI = ("some avg10={s10} avg60=0.00 avg300=0.00 total=123\n"
       "full avg10={f10} avg60=0.00 avg300=0.00 total=45\n")
TIMINGS = {"stale_after_s": 6.0}


def _reader(avail=8000000, s10="0.00", f10="0.00", meminfo=None, psi=None):
    files = {
        "/proc/meminfo": MEMINFO.format(avail=avail) if meminfo is None else meminfo,
        "/proc/pressure/memory": PSI.format(s10=s10, f10=f10) if psi is None else psi,
    }

    def read(path):
        value = files[path]
        if value is None:
            raise FileNotFoundError(path)
        return value
    return read


def _sample(**kw):
    return ah.sample_linux_memory(_reader(**kw), clock=lambda: 7.0)


def test_idle_host_is_normal_with_headroom_from_memavailable():
    sample = _sample()
    assert sample["pressure"] == "normal" and sample["observed_at"] == 7.0
    assert sample["available_bytes"] == 8000000 * 1024
    ah.parse_sample(sample)  # same allow-listed sample shape as the macOS sampler


@pytest.mark.parametrize("some,full,level", [
    ("0.00", "0.00", "normal"), ("2.50", "0.00", "normal"), ("12.00", "0.00", "warn"),
    ("3.00", "1.50", "warn"), ("45.00", "0.00", "critical"), ("20.00", "6.00", "critical"),
])
def test_psi_avg10_maps_to_the_three_levels(some, full, level):
    assert _sample(s10=some, f10=full)["pressure"] == level


_BAD_PSI = [
    "", "garbage",
    "some avg10=abc avg60=0 avg300=0 total=1\nfull avg10=0.00 avg60=0 avg300=0 total=1\n",
    "some avg10=0.00 avg60=0.00 avg300=0.00 total=1\n",
    "some avg10=101.00 avg60=0 avg300=0 total=1\nfull avg10=0.00 avg60=0 avg300=0 total=1\n",
    "some avg10=nan avg60=0 avg300=0 total=1\nfull avg10=0.00 avg60=0 avg300=0 total=1\n",
    "some avg10=-1.00 avg60=0 avg300=0 total=1\nfull avg10=0.00 avg60=0 avg300=0 total=1\n",
]


@pytest.mark.parametrize("psi", _BAD_PSI)
def test_malformed_psi_fails_closed_to_unknown(psi):
    sample = ah.sample_linux_memory(_reader(psi=psi), clock=lambda: 1.0)
    assert sample["pressure"] == "unknown" and "available_bytes" not in sample


def test_missing_psi_file_fails_closed_to_unknown():
    reader = _reader()

    def missing(path):  # kernels without CONFIG_PSI or psi=0
        if path == "/proc/pressure/memory":
            raise FileNotFoundError(path)
        return reader(path)
    sample = ah.sample_linux_memory(missing, clock=lambda: 1.0)
    assert sample["pressure"] == "unknown" and "available_bytes" not in sample


def test_unreadable_meminfo_keeps_pressure_but_omits_headroom():
    def reader(path):
        if path == "/proc/meminfo":
            raise PermissionError(path)
        return PSI.format(s10="0.00", f10="0.00")
    sample = ah.sample_linux_memory(reader, clock=lambda: 1.0)
    assert sample["pressure"] == "normal" and "available_bytes" not in sample
    assert "pressure_crosscheck" not in sample
    # headroom unmeasured while a footprint is admitted -> never healthy
    assert ah.classify_sample(sample, now=1.0, timings=TIMINGS, footprint_bytes=1 << 30, reserve_bytes=0) == "unknown"


@pytest.mark.parametrize("meminfo", ["", "MemAvailable: lots kB\n", "MemTotal: 10 kB\n", "MemAvailable: -5 kB\nMemTotal: 10 kB\n"])
def test_malformed_meminfo_omits_headroom(meminfo):
    sample = _sample(meminfo=meminfo)
    assert sample["pressure"] == "normal" and "available_bytes" not in sample


def test_memavailable_percentage_is_the_same_independent_crosscheck_as_macos():
    plenty = _sample(avail=12000000)  # 75 % available
    assert plenty["pressure_crosscheck"] == "normal"
    scarce = _sample(avail=400000)  # 2.5 % available
    assert scarce["pressure_crosscheck"] == "critical"
    # kernel says calm but MemAvailable is exhausted: contradictory probes -> unknown, never healthy
    assert ah.classify_sample(scarce, now=7.0, timings=TIMINGS, footprint_bytes=0, reserve_bytes=0) == "unknown"
    assert ah.classify_sample(plenty, now=7.0, timings=TIMINGS, footprint_bytes=1 << 30, reserve_bytes=1 << 28) == "healthy"


def test_classification_levels_match_the_macos_sampler_contract():
    crit = _sample(s10="50.00", avail=6000000)
    assert ah.classify_sample(crit, now=7.0, timings=TIMINGS, footprint_bytes=0, reserve_bytes=0) == "critical"
    warn = _sample(s10="15.00", avail=6000000)
    assert ah.classify_sample(warn, now=7.0, timings=TIMINGS, footprint_bytes=0, reserve_bytes=0) == "moderate"


def test_host_sampler_dispatches_by_platform_and_windows_stays_unknown():
    linux = ah.HostSampler(platform="linux", file_reader=_reader(), clock=lambda: 3.0, meter=ah.CpuMeter(lambda: (1, 1, 1, 1)))
    assert linux()["pressure"] == "normal"

    def no_tool(argv):
        raise FileNotFoundError(argv[0])
    windows = ah.HostSampler(platform="win32", runner=no_tool, file_reader=_reader(), clock=lambda: 3.0,
                             meter=ah.CpuMeter(lambda: (1, 1, 1, 1)))
    assert windows()["pressure"] == "unknown"
    darwin = ah.HostSampler(platform="darwin", runner=lambda argv: "1\n" if argv[0] == "sysctl" else (_ for _ in ()).throw(OSError()),
                            file_reader=_reader(), clock=lambda: 3.0, meter=ah.CpuMeter(lambda: (1, 1, 1, 1)))
    assert darwin()["pressure"] == "normal" and "available_bytes" not in darwin()
