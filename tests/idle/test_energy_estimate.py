# SPDX-License-Identifier: AGPL-3.0-or-later
"""PV25: per-session energy estimate from measured worker CPU/GPU time x a per-chip power table."""
from __future__ import annotations

import pytest

from mycelium_idle import energy
from mycelium_idle import observers


class FakeTime:
    """Cumulative (cpu_s, gpu_s) per pid, as a process-time observer would report."""

    def __init__(self):
        self.values = {}
        self.available = True
        self.gpu_available = True

    def sample(self, pids):
        return {pid: self.values[pid] for pid in pids if pid in self.values}


def meter(chip="Apple M4 Pro", cores=14):
    clock = {"t": 0.0}
    source = FakeTime()
    return energy.EnergyMeter(source=source, chip=chip, cpu_cores=cores, clock=lambda: clock["t"]), source, clock


def test_every_known_apple_family_has_a_power_figure_and_unknown_falls_back():
    for name in ("Apple M1", "Apple M1 Pro", "Apple M1 Max", "Apple M1 Ultra", "Apple M2", "Apple M2 Pro",
                 "Apple M2 Max", "Apple M2 Ultra", "Apple M3", "Apple M3 Pro", "Apple M3 Max", "Apple M3 Ultra",
                 "Apple M4", "Apple M4 Pro", "Apple M4 Max"):
        figure = energy.power_figure(name)
        assert figure.cpu_watts > 0 and figure.gpu_watts > 0 and figure.known is True
    unknown = energy.power_figure("Intel Core i9")
    assert unknown.known is False and unknown.cpu_watts > 0 and unknown.gpu_watts > 0
    assert energy.power_figure(None).known is False


def test_chip_lookup_is_case_and_space_tolerant():
    assert energy.power_figure("  apple m4   PRO ").known is True
    assert energy.power_figure("Apple M4 Pro") == energy.power_figure("apple m4 pro")


def test_zero_work_is_zero_energy_and_labelled_an_estimate():
    m, source, clock = meter()
    snap = m.snapshot()
    assert snap["estimate_wh"] == 0.0 and snap["is_estimate"] is True
    assert "estimate" in snap["label"].lower() and "not measured" in snap["label"].lower()
    assert snap["method"] == "worker cpu/gpu time x per-chip active power"


def test_energy_is_cpu_time_per_core_share_plus_gpu_time():
    m, source, clock = meter()
    figure = energy.power_figure("Apple M4 Pro")
    source.values[10] = (0.0, 0.0)
    m.update([10])
    source.values[10] = (3600.0, 1800.0)  # one core-hour of CPU, half a GPU-hour
    clock["t"] = 4000.0
    m.update([10])
    expected = figure.cpu_watts / 14 * 1.0 + figure.gpu_watts * 0.5
    snap = m.snapshot()
    assert snap["estimate_wh"] == pytest.approx(expected, rel=1e-6)
    assert snap["cpu_seconds"] == 3600.0 and snap["gpu_seconds"] == 1800.0
    assert snap["gpu_time_available"] is True and snap["chip_known"] is True


def test_deltas_accumulate_across_pids_and_a_vanished_pid_keeps_its_contribution():
    m, source, clock = meter()
    source.values.update({1: (10.0, 0.0), 2: (5.0, 0.0)})
    m.update([1, 2])
    source.values.update({1: (20.0, 0.0), 2: (9.0, 0.0)})
    m.update([1, 2])
    del source.values[2]  # worker 2 exited (idle unload / restart)
    source.values[1] = (25.0, 0.0)
    m.update([1])
    assert m.snapshot()["cpu_seconds"] == pytest.approx((25.0 - 10.0) + (9.0 - 5.0))


def test_a_restarted_pid_with_lower_counters_is_not_a_negative_delta():
    m, source, clock = meter()
    source.values[1] = (100.0, 4.0)
    m.update([1])
    source.values[1] = (2.0, 0.5)  # pid reused / new worker
    m.update([1])
    snap = m.snapshot()
    # the first sample was only a baseline; the restarted process's counters are all session work
    assert snap["cpu_seconds"] == pytest.approx(2.0) and snap["gpu_seconds"] == pytest.approx(0.5)


def test_the_first_sample_is_a_baseline_not_a_charge():
    m, source, clock = meter()
    source.values[1] = (500.0, 50.0)  # process already ran before the session started
    m.update([1])
    assert m.snapshot()["estimate_wh"] == 0.0


def test_missing_gpu_observer_reports_gpu_unavailable_and_charges_nothing_for_it():
    class NoGpu(FakeTime):
        def sample(self, pids):
            return {pid: (self.values[pid][0], None) for pid in pids if pid in self.values}

    clock = {"t": 0.0}
    source = NoGpu()
    m = energy.EnergyMeter(source=source, chip="Apple M4", cpu_cores=10, clock=lambda: clock["t"])
    source.values[1] = (0.0, 0.0)
    m.update([1])
    source.values[1] = (3600.0, 0.0)
    m.update([1])
    snap = m.snapshot()
    assert snap["gpu_time_available"] is False and snap["gpu_seconds"] == 0.0
    assert snap["estimate_wh"] == pytest.approx(energy.power_figure("Apple M4").cpu_watts / 10, rel=1e-6)


def test_session_reset_starts_a_new_session():
    m, source, clock = meter()
    source.values[1] = (0.0, 0.0)
    m.update([1])
    source.values[1] = (60.0, 6.0)
    m.update([1])
    assert m.snapshot()["estimate_wh"] > 0
    m.reset()
    assert m.snapshot()["estimate_wh"] == 0.0 and m.snapshot()["session_seconds"] == 0.0


def test_display_text_is_short_and_says_estimate():
    m, source, clock = meter()
    text = energy.display_text(m.snapshot())
    assert text.startswith("Energy this session: about ") and text.endswith(" Wh (estimate)")


def test_snapshot_has_no_process_identity():
    m, source, clock = meter()
    source.values[4242] = (0.0, 0.0)
    m.update([4242])
    assert "4242" not in repr(m.snapshot())


# ---------------------------------------------------------------- observers
def test_stub_observers_have_the_same_interface_and_report_unavailable():
    for stub in (observers.LinuxProcessTimeStub(), observers.WindowsProcessTimeStub()):
        assert stub.sample([1, 2, 3]) == {}
        assert stub.cpu_available is False and stub.gpu_available is False
        assert stub.platform in ("linux", "win32")


def test_observer_for_platform_selects_by_platform_name():
    assert isinstance(observers.process_time_observer("linux"), observers.LinuxProcessTimeStub)
    assert isinstance(observers.process_time_observer("win32"), observers.WindowsProcessTimeStub)
    assert isinstance(observers.process_time_observer("darwin"), observers.DarwinProcessTime)


def test_gpu_client_parser_pairs_creator_pid_with_accumulated_gpu_time():
    text = '''
+-o AGXDeviceUserClient  <class AGXDeviceUserClient, id 0x1, registered>
  | {
  |   "IOUserClientCreator" = "pid 4242, python3.14"
  |   "AppUsage" = ({"API"="Metal","lastSubmittedTime"=1,"accumulatedGPUTime"=2000000000},{"API"="Metal","accumulatedGPUTime"=500000000})
  | }
+-o AGXDeviceUserClient  <class AGXDeviceUserClient, id 0x2, registered>
  | {
  |   "IOUserClientCreator" = "pid 401, WindowServer"
  |   "AppUsage" = ({"API"="Metal","accumulatedGPUTime"=9})
  | }
+-o AGXDeviceUserClient  <class AGXDeviceUserClient, id 0x3, registered>
  | {
  |   "IOUserClientCreator" = "pid 4242, python3.14"
  | }
'''
    assert observers.parse_gpu_clients(text) == {4242: 2.5, 401: 9e-9}


def test_darwin_cpu_time_reads_a_live_pid_and_a_missing_pid_is_absent():
    import os
    import time
    observer = observers.DarwinProcessTime()
    end = time.process_time() + 0.2
    while time.process_time() < end:
        pass
    got = observer.sample([os.getpid(), 2_000_000_000])
    assert 2_000_000_000 not in got
    if observer.cpu_available:
        assert got[os.getpid()][0] >= 0.15
