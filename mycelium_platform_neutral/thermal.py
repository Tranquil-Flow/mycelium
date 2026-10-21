# SPDX-License-Identifier: AGPL-3.0-only
"""Native thermal observation for the A9 runtime-efficiency benchmark.

Spec §3.3/§3.4: thermal state is an observed capability with an explicit
evidence source kind.  This module reads the host's own thermal sensors where
the platform exposes them and classifies the hottest zone:

* Windows: ``Win32_PerfFormattedData_Counters_ThermalZoneInformation``
  (Kelvin, readable without elevation) and, as a second source,
  ``root/wmi MSAcpi_ThermalZoneTemperature`` (tenths of Kelvin, often needs
  elevation), both through PowerShell CIM; plus ``Win32_Battery`` for the
  power source.
* Linux: ``/sys/class/thermal/thermal_zone*/temp`` (millidegrees Celsius).
* Anything else, or no usable zone: no native reading.

Without a native reading the caller falls back to an operator declaration and
the evidence says ``operator_declared``.  A missing reading is never treated as
nominal.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
import json
import math
from pathlib import Path
import platform
import subprocess
from typing import Any

PROTOCOL = "mycelium.a9_thermal_observation.v1"
#: Frozen classification of the hottest observed zone, degrees Celsius.  The
#: benchmark accepts only ``nominal`` and ``fair`` (runtime_efficiency).
THRESHOLDS_C = (("nominal", 70.0), ("fair", 80.0), ("serious", 90.0))
STATES = ("nominal", "fair", "serious", "critical", "unknown")
MIN_PLAUSIBLE_C = 5.0
MAX_PLAUSIBLE_C = 125.0

_WINDOWS_QUERY = r"""
$ErrorActionPreference = 'SilentlyContinue'
$zones = @()
Get-CimInstance Win32_PerfFormattedData_Counters_ThermalZoneInformation | ForEach-Object {
  if ($_.Temperature) { $zones += [pscustomobject]@{source='perf_thermal_zone'; name=$_.Name; kelvin=[double]$_.Temperature} }
}
Get-CimInstance -Namespace root/wmi -ClassName MSAcpi_ThermalZoneTemperature | ForEach-Object {
  if ($_.CurrentTemperature) { $zones += [pscustomobject]@{source='msacpi_thermal_zone'; name=$_.InstanceName; kelvin=([double]$_.CurrentTemperature / 10.0)} }
}
$battery = Get-CimInstance Win32_Battery | Select-Object -First 1
$power = if ($null -eq $battery) { 'ac_no_battery' } elseif ($battery.BatteryStatus -eq 2) { 'ac' } else { 'battery' }
[pscustomobject]@{zones=$zones; power=$power} | ConvertTo-Json -Depth 4 -Compress
"""

Runner = Callable[[Sequence[str]], str]


def _default_runner(command: Sequence[str]) -> str:
    completed = subprocess.run(list(command), capture_output=True, text=True, timeout=30, check=False)
    return completed.stdout


def classify(celsius: float | None) -> str:
    if celsius is None or not math.isfinite(celsius):
        return "unknown"
    for state, ceiling in THRESHOLDS_C:
        if celsius < ceiling:
            return state
    return "critical"


def _plausible(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number) or not MIN_PLAUSIBLE_C <= number <= MAX_PLAUSIBLE_C:
        return None
    return number


def _windows_zones(runner: Runner) -> tuple[list[dict[str, Any]], str | None]:
    raw = runner(["powershell", "-NoProfile", "-NonInteractive", "-Command", _WINDOWS_QUERY])
    try:
        document = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError:
        return [], None
    zones = document.get("zones") or []
    if isinstance(zones, dict):
        zones = [zones]
    result = []
    for zone in zones:
        if not isinstance(zone, dict):
            continue
        try:
            celsius = _plausible(float(zone["kelvin"]) - 273.15)
        except (KeyError, TypeError, ValueError):
            celsius = None
        if celsius is not None:
            result.append({"source": str(zone.get("source")), "name": str(zone.get("name"))[:64], "celsius": round(celsius, 1)})
    power = document.get("power")
    return result, power if power in {"ac", "battery", "ac_no_battery"} else None


def _linux_zones(root: Path) -> list[dict[str, Any]]:
    result = []
    for zone in sorted(root.glob("thermal_zone*")):
        try:
            celsius = _plausible(int((zone / "temp").read_text().strip()) / 1000.0)
            kind = (zone / "type").read_text().strip()[:64]
        except (OSError, ValueError):
            continue
        if celsius is not None:
            result.append({"source": "sysfs_thermal_zone", "name": kind, "celsius": round(celsius, 1)})
    return result


def observe(
    *,
    system: str | None = None,
    runner: Runner = _default_runner,
    linux_root: Path = Path("/sys/class/thermal"),
    clock: Callable[[], float] | None = None,
) -> dict[str, Any]:
    """One native thermal sample; ``state`` is ``unknown`` when nothing is readable."""

    import time

    host = platform.system() if system is None else system
    zones: list[dict[str, Any]] = []
    power = None
    if host == "Windows":
        try:
            zones, power = _windows_zones(runner)
        except (OSError, subprocess.SubprocessError):
            zones = []
    elif host == "Linux":
        zones = _linux_zones(linux_root)
    hottest = max((zone["celsius"] for zone in zones), default=None)
    return {
        "protocol": PROTOCOL,
        "observed_at": (clock or time.time)(),
        "system": host,
        "zones": zones,
        "hottest_celsius": hottest,
        "power_source": power,
        "state": classify(hottest),
        "source_kind": "native_observed" if zones else "unknown",
    }


def _worst(states: Sequence[str]) -> str:
    order = {state: index for index, state in enumerate(("nominal", "fair", "serious", "critical"))}
    known = [state for state in states if state in order]
    if not known or len(known) != len(states):
        return "unknown"
    return max(known, key=order.__getitem__)


def decide(
    samples: Sequence[dict[str, Any]], *, declared: str | None, rate_drift: float
) -> dict[str, Any]:
    """Benchmark thermal evidence: native when every sample read a zone, else declared.

    The candidate-rate drift rule applies either way: a last window below 0.85x
    the first is recorded as ``serious`` (throttling), whatever was read or
    declared.
    """

    native = bool(samples) and all(sample["source_kind"] == "native_observed" for sample in samples)
    if native:
        state, source = _worst([sample["state"] for sample in samples]), "native_observed"
    elif declared in STATES:
        state, source = declared, "operator_declared"
    else:
        state, source = "unknown", "unknown"
    if rate_drift < 0.85 and state in {"nominal", "fair", "unknown"}:
        state = "serious"
    return {
        "state": state,
        "source_kind": source,
        "declared": declared,
        "samples": list(samples),
        "rate_drift": rate_drift,
        "native_unavailable": not native,
    }


__all__ = ["PROTOCOL", "THRESHOLDS_C", "classify", "decide", "observe"]
