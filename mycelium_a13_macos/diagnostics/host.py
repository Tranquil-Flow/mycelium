# SPDX-License-Identifier: AGPL-3.0-only
"""Host facts that explain slow or late control commands: load, memory pressure, Spotlight CPU, free disk.

Read-only.  Every reading that cannot be taken is ``None`` (or ``unknown``), never a guess.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable

_SPOTLIGHT = frozenset({"mds", "mds_stores", "mdworker", "mdworker_shared"})


def _run(argv: list[str]) -> str:
    return subprocess.run(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                          timeout=2.5, check=True, text=True).stdout


def spotlight_cpu_percent(ps_output: str) -> float:
    """Sum of ``%CPU`` over the Spotlight processes in ``ps -axo pcpu=,comm=`` output."""
    total = 0.0
    for line in ps_output.splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) != 2:
            continue
        try:
            value = float(parts[0])
        except ValueError:
            continue
        if os.path.basename(parts[1].strip()) in _SPOTLIGHT:
            total += value
    return round(total, 1)


def _existing_parent(path: Path) -> Path:
    path = path.absolute()
    while not path.exists() and path != path.parent:
        path = path.parent
    return path


def host_metrics(*, runner: Callable[[list[str]], str] | None = None, disk_path: str | Path = "/") -> dict[str, Any]:
    run = runner or _run
    out: dict[str, Any] = {"load_average": None, "cpu_count": os.cpu_count(), "memory_pressure": "unknown",
                           "memory_available_bytes": None, "spotlight_mds_cpu_percent": None,
                           "disk_free_bytes": None}
    try:
        out["load_average"] = [round(v, 2) for v in os.getloadavg()]
    except (OSError, AttributeError):
        pass
    try:
        from mycelium_a13_macos.adaptive_host import sample_host_memory
        sample = sample_host_memory(run) if sys.platform == "darwin" or runner is not None else {}
        out["memory_pressure"] = sample.get("pressure", "unknown")
        out["memory_available_bytes"] = sample.get("available_bytes")
    except Exception:
        pass
    if sys.platform == "darwin" or runner is not None:
        try:
            out["spotlight_mds_cpu_percent"] = spotlight_cpu_percent(run(["ps", "-axo", "pcpu=,comm="]))
        except Exception:
            pass
    try:
        out["disk_free_bytes"] = int(shutil.disk_usage(_existing_parent(Path(disk_path))).free)
    except OSError:
        pass
    return out
