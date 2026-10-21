# SPDX-License-Identifier: AGPL-3.0-only
"""Read-only Linux resource observations for the A13 policy guards.

Same frozen surface as ``mycelium_a13_macos.resources``: ``observe_policy()``
returns ``{"power", "thermal", "network"}`` with the same closed values, and
``policy_violation`` judges it unchanged.

* power: ``/sys/class/power_supply`` -- any online ``Mains``/``USB`` supply is
  external; a battery with no online supply is battery; no supplies at all
  (a desktop) is external;
* thermal: the hottest plausible ``/sys/class/thermal/thermal_zone*``
  (``mycelium_platform_neutral.thermal``); no zone is ``unknown``, which the
  policy refuses;
* network: NetworkManager over D-Bus (``busctl``): ``Connectivity`` full (4)
  is usable, none (1) is unavailable; ``Metered`` yes/guess-yes (1, 3) is
  metered, no/guess-no (2, 4) is unmetered.  Without NetworkManager the
  network is ``unknown`` (refused), never assumed healthy.

Any failure is ``unknown`` for the affected field.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from pathlib import Path
import subprocess

from mycelium_a13_macos.resources import UNKNOWN_OBSERVATION
from mycelium_platform_neutral import thermal

Runner = Callable[[Sequence[str]], str]
_NM = ("org.freedesktop.NetworkManager", "/org/freedesktop/NetworkManager", "org.freedesktop.NetworkManager")


def _default_runner(command: Sequence[str]) -> str:
    completed = subprocess.run(list(command), capture_output=True, text=True, timeout=5, check=False)
    return completed.stdout if completed.returncode == 0 else ""


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError):
        return ""


def observe_power(power_root: Path = Path("/sys/class/power_supply")) -> str:
    try:
        supplies = sorted(power_root.iterdir())
    except OSError:
        return "unknown"
    if not supplies:
        return "external"
    batteries = online = 0
    for supply in supplies:
        kind = _read(supply / "type")
        if kind == "Battery":
            batteries += 1
        elif kind in {"Mains", "USB", "USB_C", "USB_PD"} and _read(supply / "online") == "1":
            online += 1
    if online:
        return "external"
    return "battery" if batteries else "unknown"


def observe_thermal(thermal_root: Path = Path("/sys/class/thermal")) -> str:
    zones = thermal._linux_zones(thermal_root)  # noqa: SLF001 - the shared sysfs reader
    return thermal.classify(max(zone["celsius"] for zone in zones)) if zones else "unknown"


def _nm_property(runner: Runner, name: str) -> int | None:
    raw = runner(["busctl", "--system", "get-property", *_NM, name]).split()
    if len(raw) == 2 and raw[0] == "u" and raw[1].isdigit():
        return int(raw[1])
    return None


def observe_network(runner: Runner = _default_runner) -> str:
    try:
        connectivity = _nm_property(runner, "Connectivity")
        metered = _nm_property(runner, "Metered")
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    if connectivity == 1:
        return "unavailable"
    if connectivity != 4:
        return "unknown"
    if metered in {1, 3}:
        return "metered"
    if metered in {2, 4}:
        return "unmetered"
    return "unknown"


def observe_policy(*, runner: Runner = _default_runner, power_root: Path = Path("/sys/class/power_supply"),
                   thermal_root: Path = Path("/sys/class/thermal")) -> dict[str, str]:
    observation = dict(UNKNOWN_OBSERVATION)
    observation["power"] = observe_power(power_root)
    observation["thermal"] = observe_thermal(thermal_root)
    observation["network"] = observe_network(runner)
    return observation


__all__ = ["observe_network", "observe_policy", "observe_power", "observe_thermal"]
