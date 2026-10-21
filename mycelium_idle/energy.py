# SPDX-License-Identifier: AGPL-3.0-or-later
"""Per-session energy *estimate*: worker CPU/GPU time x a per-chip active-power table.

This is arithmetic on measured process time, not a power measurement: no powermetrics, no sudo, no
battery-drain reading.  The figures are rounded, typical full-load package power per chip family
(CPU cluster, GPU) and are deliberately coarse; the UI must call the result an estimate.  Idle
baseline power of the Mac is *not* attributed to Mycelium.
"""
from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass
from typing import Any, Callable, Final, Iterable, Mapping, Protocol

METHOD: Final = "worker cpu/gpu time x per-chip active power"
LABEL: Final = "Estimate, not measured: worker CPU/GPU time x typical power for this chip"


@dataclass(frozen=True)
class PowerFigure:
    cpu_watts: float  # whole-CPU active power with every core busy
    gpu_watts: float  # GPU active power
    known: bool


# chip (lower-case, single spaces) -> (cpu_w, gpu_w).  Rounded ballpark full-load figures.
_TABLE: Final[dict[str, tuple[float, float]]] = {
    "apple m1": (15, 10), "apple m1 pro": (30, 18), "apple m1 max": (35, 35), "apple m1 ultra": (70, 70),
    "apple m2": (20, 12), "apple m2 pro": (35, 20), "apple m2 max": (40, 40), "apple m2 ultra": (80, 80),
    "apple m3": (22, 12), "apple m3 pro": (30, 18), "apple m3 max": (50, 45), "apple m3 ultra": (90, 90),
    "apple m4": (22, 12), "apple m4 pro": (40, 20), "apple m4 max": (60, 50),
}
_FALLBACK: Final = (25.0, 15.0)


def _normal(chip: str | None) -> str:
    return re.sub(r"\s+", " ", (chip or "").strip().lower())


def power_figure(chip: str | None) -> PowerFigure:
    row = _TABLE.get(_normal(chip))
    return PowerFigure(*(row if row else _FALLBACK), known=row is not None)


def detect_chip() -> str | None:
    """The CPU brand string (macOS ``machdep.cpu.brand_string``); ``None`` where unavailable."""
    import subprocess
    import sys
    if sys.platform != "darwin":
        return None
    try:
        out = subprocess.run(["/usr/sbin/sysctl", "-n", "machdep.cpu.brand_string"], stdin=subprocess.DEVNULL,
                             capture_output=True, timeout=2, check=False)
        text = out.stdout.decode("utf-8", "replace").strip()
        return text[:64] or None
    except (OSError, subprocess.SubprocessError):
        return None


class ProcessTimeSource(Protocol):
    def sample(self, pids: Iterable[int]) -> Mapping[int, tuple[float, float | None]]:
        """Cumulative ``(cpu_seconds, gpu_seconds | None)`` per live pid; absent pids are omitted."""


class EnergyMeter:
    def __init__(self, *, source: ProcessTimeSource, chip: str | None, cpu_cores: int | None = None,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._source, self._chip = source, chip
        self._figure = power_figure(chip)
        self._cores = max(1, int(cpu_cores or os.cpu_count() or 1))
        self._clock = clock
        self.reset()

    def reset(self) -> None:
        self._started = self._clock()
        self._last: dict[int, tuple[float, float]] = {}
        self._baselined = False
        self._cpu = 0.0
        self._gpu = 0.0
        self._gpu_seen = False

    def update(self, pids: Iterable[int]) -> None:
        samples = self._source.sample(list(pids))
        first = not self._baselined
        for pid, (cpu, gpu) in samples.items():
            if gpu is not None:
                self._gpu_seen = True
            gpu_value = 0.0 if gpu is None else float(gpu)
            previous = self._last.get(pid)
            if previous is None:
                # The first sample ever is a baseline; a process first seen later started after it.
                delta = (0.0, 0.0) if first else (float(cpu), gpu_value)
            else:
                d_cpu, d_gpu = float(cpu) - previous[0], gpu_value - previous[1]
                delta = (float(cpu), gpu_value) if d_cpu < 0 or d_gpu < 0 else (d_cpu, d_gpu)
            self._cpu += delta[0]
            self._gpu += delta[1]
            self._last[pid] = (float(cpu), gpu_value)
        self._baselined = True

    def snapshot(self) -> dict[str, Any]:
        wh = (self._cpu * self._figure.cpu_watts / self._cores + self._gpu * self._figure.gpu_watts) / 3600.0
        return {
            "estimate_wh": round(wh, 6), "is_estimate": True, "label": LABEL, "method": METHOD,
            "cpu_seconds": round(self._cpu, 3), "gpu_seconds": round(self._gpu, 3),
            "gpu_time_available": self._gpu_seen, "chip": _normal(self._chip) or None,
            "chip_known": self._figure.known, "session_seconds": round(max(0.0, self._clock() - self._started), 1),
        }


def display_text(snapshot: Mapping[str, Any]) -> str:
    return f"Energy this session: about {float(snapshot['estimate_wh']):.2f} Wh (estimate)"
