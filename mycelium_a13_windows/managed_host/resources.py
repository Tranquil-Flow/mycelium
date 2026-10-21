# SPDX-License-Identifier: AGPL-3.0-only
"""Read-only Windows resource observations for the A13 policy guards.

Same frozen surface as ``mycelium_a13_macos.resources``: ``observe_policy()``
returns ``{"power", "thermal", "network"}`` with the same closed values, and
``mycelium_a13_macos.resources.policy_violation`` judges it unchanged.  One
bounded PowerShell query reads:

* power: ``Win32_Battery`` (no battery or ``BatteryStatus == 2`` -> external);
* thermal: the hottest thermal zone (``mycelium_platform_neutral.thermal``);
  no readable zone is ``unknown``, which the policy refuses;
* network: the Windows connection profile's cost
  (``Unrestricted`` -> unmetered, ``Fixed``/``Variable`` -> metered, no
  internet profile -> unavailable).

Any failure is ``unknown`` for the affected field, never a healthy value.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
import json
import subprocess
from typing import Any

from mycelium_a13_macos.resources import UNKNOWN_OBSERVATION
from mycelium_platform_neutral import thermal

Runner = Callable[[Sequence[str]], str]

_QUERY = r"""
$ErrorActionPreference = 'SilentlyContinue'
$zones = @()
Get-CimInstance Win32_PerfFormattedData_Counters_ThermalZoneInformation | ForEach-Object {
  if ($_.Temperature) { $zones += [double]$_.Temperature }
}
Get-CimInstance -Namespace root/wmi -ClassName MSAcpi_ThermalZoneTemperature | ForEach-Object {
  if ($_.CurrentTemperature) { $zones += ([double]$_.CurrentTemperature / 10.0) }
}
$battery = Get-CimInstance Win32_Battery | Select-Object -First 1
$power = if ($null -eq $battery) { 'external' } elseif ($battery.BatteryStatus -eq 2) { 'external' } else { 'battery' }
$network = 'unknown'
try {
  [void][Windows.Networking.Connectivity.NetworkInformation,Windows.Networking.Connectivity,ContentType=WindowsRuntime]
  $profile = [Windows.Networking.Connectivity.NetworkInformation]::GetInternetConnectionProfile()
  if ($null -eq $profile) { $network = 'unavailable' }
  else {
    $cost = $profile.GetConnectionCost().NetworkCostType.ToString()
    $network = switch ($cost) { 'Unrestricted' { 'unmetered' } 'Fixed' { 'metered' } 'Variable' { 'metered' } default { 'unknown' } }
  }
} catch { $network = 'unknown' }
[pscustomobject]@{kelvin=$zones; power=$power; network=$network} | ConvertTo-Json -Compress
"""


def _default_runner(command: Sequence[str]) -> str:
    completed = subprocess.run(list(command), capture_output=True, text=True, timeout=10, check=False)
    return completed.stdout


def observe_policy(*, runner: Runner = _default_runner) -> dict[str, str]:
    observation = dict(UNKNOWN_OBSERVATION)
    try:
        raw = runner(["powershell", "-NoProfile", "-NonInteractive", "-Command", _QUERY])
        document: Any = json.loads(raw) if raw and raw.strip() else {}
    except (OSError, subprocess.SubprocessError, ValueError):
        return observation
    if not isinstance(document, dict):
        return observation
    if document.get("power") in {"external", "battery"}:
        observation["power"] = document["power"]
    if document.get("network") in {"unmetered", "metered", "unavailable"}:
        observation["network"] = document["network"]
    kelvin = document.get("kelvin")
    values = kelvin if isinstance(kelvin, list) else [kelvin]
    celsius = []
    for value in values:
        try:
            plausible = thermal._plausible(float(value) - 273.15)  # noqa: SLF001
        except (TypeError, ValueError):
            plausible = None
        if plausible is not None:
            celsius.append(plausible)
    observation["thermal"] = thermal.classify(max(celsius)) if celsius else "unknown"
    return observation


__all__ = ["observe_policy"]
