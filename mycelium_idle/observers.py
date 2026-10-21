# SPDX-License-Identifier: AGPL-3.0-or-later
"""Worker process-time observers: one interface, macOS real, Linux/Windows stubs (PV29 replaces them).

``sample(pids)`` returns cumulative ``(cpu_seconds, gpu_seconds | None)`` per live pid.  No sudo, no
powermetrics: CPU time comes from ``proc_pid_rusage`` and GPU time from the I/O Registry's
per-client ``accumulatedGPUTime`` (what Activity Monitor shows), both readable by the same user.
"""
from __future__ import annotations

import ctypes
import re
import subprocess
import sys
from typing import Final, Iterable, Mapping


class _StubProcessTime:
    platform = ""
    cpu_available = False
    gpu_available = False

    def sample(self, pids: Iterable[int]) -> Mapping[int, tuple[float, float | None]]:
        return {}


class LinuxProcessTimeStub(_StubProcessTime):
    platform = "linux"


class WindowsProcessTimeStub(_StubProcessTime):
    platform = "win32"


_GPU_BLOCK: Final = re.compile(r"^[\s|]*\+-o ", re.M)
_CREATOR: Final = re.compile(r'"IOUserClientCreator"\s*=\s*"pid (\d+),')
_GPU_TIME: Final = re.compile(r'"accumulatedGPUTime"=(\d+)')


def parse_gpu_clients(text: str) -> dict[int, float]:
    """Seconds of accumulated GPU time per creator pid from ``ioreg -r -c AGXDeviceUserClient``."""
    totals: dict[int, float] = {}
    for block in _GPU_BLOCK.split(text):
        creator = _CREATOR.search(block)
        if creator is None:
            continue
        pid = int(creator.group(1))
        nanoseconds = sum(int(value) for value in _GPU_TIME.findall(block))
        totals[pid] = totals.get(pid, 0.0) + nanoseconds / 1e9
    return totals


class _MachTimebase(ctypes.Structure):
    _fields_ = [("numer", ctypes.c_uint32), ("denom", ctypes.c_uint32)]


class DarwinProcessTime:
    platform = "darwin"

    def __init__(self) -> None:
        self.cpu_available = False
        self.gpu_available = False
        self._proc_pid_rusage = None
        self._scale = 1.0
        if sys.platform == "darwin":
            try:
                libproc = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
                libsystem = ctypes.CDLL("/usr/lib/libSystem.B.dylib")
                base = _MachTimebase()
                libsystem.mach_timebase_info(ctypes.byref(base))
                self._scale = base.numer / base.denom / 1e9
                libproc.proc_pid_rusage.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_void_p]
                self._proc_pid_rusage = libproc.proc_pid_rusage
                self.cpu_available = True
            except (OSError, AttributeError, ZeroDivisionError):
                self._proc_pid_rusage = None

    def _cpu_seconds(self, pid: int) -> float | None:
        if self._proc_pid_rusage is None:
            return None
        buffer = ctypes.create_string_buffer(512)
        if self._proc_pid_rusage(pid, 0, buffer) != 0:  # RUSAGE_INFO_V0: uuid[16], user, system
            return None
        user = int.from_bytes(buffer.raw[16:24], sys.byteorder)
        system = int.from_bytes(buffer.raw[24:32], sys.byteorder)
        return (user + system) * self._scale

    def _gpu_by_pid(self) -> dict[int, float] | None:
        try:
            out = subprocess.run(["/usr/sbin/ioreg", "-r", "-c", "AGXDeviceUserClient", "-w0"],
                                 stdin=subprocess.DEVNULL, capture_output=True, timeout=2.0, check=False)
        except (OSError, subprocess.SubprocessError):
            return None
        if out.returncode != 0 or not out.stdout:
            return None
        return parse_gpu_clients(out.stdout.decode("utf-8", "replace"))

    def sample(self, pids: Iterable[int]) -> Mapping[int, tuple[float, float | None]]:
        wanted = list(pids)
        gpu = self._gpu_by_pid() if wanted else None
        self.gpu_available = gpu is not None or self.gpu_available
        result: dict[int, tuple[float, float | None]] = {}
        for pid in wanted:
            cpu = self._cpu_seconds(pid)
            if cpu is None:
                continue
            result[pid] = (cpu, None if gpu is None else gpu.get(pid, 0.0))
        return result


def process_tree(root_pid: int) -> list[int]:
    """``root_pid`` and its live descendants (best effort; ``ps`` is read-only)."""
    try:
        out = subprocess.run(["/bin/ps", "-axo", "pid=,ppid="], stdin=subprocess.DEVNULL, capture_output=True,
                             timeout=2.0, check=False)
    except (OSError, subprocess.SubprocessError):
        return [root_pid]
    children: dict[int, list[int]] = {}
    for line in out.stdout.decode("utf-8", "replace").splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
            children.setdefault(int(parts[1]), []).append(int(parts[0]))
    seen, stack = [], [root_pid]
    while stack and len(seen) < 64:
        pid = stack.pop()
        if pid not in seen:
            seen.append(pid)
            stack.extend(children.get(pid, ()))
    return seen


def process_time_observer(platform: str | None = None):
    name = sys.platform if platform is None else platform
    if name.startswith("linux"):
        return LinuxProcessTimeStub()
    if name == "win32":
        return WindowsProcessTimeStub()
    return DarwinProcessTime()
