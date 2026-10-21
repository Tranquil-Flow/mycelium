# SPDX-License-Identifier: AGPL-3.0-only
"""Native thermal observation with explicit fallback to an operator declaration."""

from __future__ import annotations

import json

from mycelium_platform_neutral import thermal


def _runner(document):
    calls = []

    def run(command):
        calls.append(list(command))
        return json.dumps(document)

    run.calls = calls
    return run


def test_windows_reads_perf_and_msacpi_zones_and_power_source():
    run = _runner({"zones": [
        {"source": "perf_thermal_zone", "name": "\\_TZ.CPUZ", "kelvin": 335.0},
        {"source": "msacpi_thermal_zone", "name": "ACPI\\ThermalZone\\TZ01_0", "kelvin": 341.15},
    ], "power": "ac"})
    sample = thermal.observe(system="Windows", runner=run, clock=lambda: 1.0)
    assert run.calls[0][0] == "powershell"
    assert sample["source_kind"] == "native_observed"
    assert sample["hottest_celsius"] == 68.0
    assert sample["state"] == "nominal"
    assert sample["power_source"] == "ac"


def test_windows_single_zone_object_and_implausible_values():
    sample = thermal.observe(system="Windows", runner=_runner(
        {"zones": {"source": "perf_thermal_zone", "name": "z", "kelvin": 348.15}, "power": "battery"}))
    assert (sample["state"], sample["power_source"]) == ("fair", "battery")
    nothing = thermal.observe(system="Windows", runner=_runner(
        {"zones": [{"kelvin": 0}, {"kelvin": "hot"}, {"name": "no-reading"}], "power": "weird"}))
    assert nothing["source_kind"] == "unknown" and nothing["state"] == "unknown"
    assert nothing["power_source"] is None
    assert thermal.observe(system="Windows", runner=lambda _c: "not json")["state"] == "unknown"


def test_linux_sysfs_zones(tmp_path):
    for index, (kind, millic) in enumerate((("x86_pkg_temp", "91000"), ("acpitz", "40000"))):
        zone = tmp_path / f"thermal_zone{index}"
        zone.mkdir()
        (zone / "type").write_text(kind)
        (zone / "temp").write_text(millic)
    sample = thermal.observe(system="Linux", linux_root=tmp_path)
    assert sample["hottest_celsius"] == 91.0 and sample["state"] == "critical"


def test_unknown_platform_has_no_native_reading():
    sample = thermal.observe(system="Darwin")
    assert sample["source_kind"] == "unknown" and sample["state"] == "unknown"


def test_decide_prefers_native_and_records_fallback_explicitly():
    native = [thermal.observe(system="Windows", runner=_runner(
        {"zones": [{"source": "perf_thermal_zone", "name": "z", "kelvin": k}], "power": "ac"}))
        for k in (330.0, 350.0)]
    decided = thermal.decide(native, declared="nominal", rate_drift=1.0)
    assert (decided["state"], decided["source_kind"], decided["native_unavailable"]) == ("fair", "native_observed", False)
    unknown = [thermal.observe(system="Darwin")]
    fallback = thermal.decide(unknown, declared="nominal", rate_drift=1.0)
    assert (fallback["state"], fallback["source_kind"], fallback["native_unavailable"]) == (
        "nominal", "operator_declared", True)
    assert thermal.decide(unknown, declared=None, rate_drift=1.0)["state"] == "unknown"
    assert thermal.decide(native, declared=None, rate_drift=0.8)["state"] == "serious"
    mixed = native[:1] + unknown
    assert thermal.decide(mixed, declared="fair", rate_drift=1.0)["source_kind"] == "operator_declared"
