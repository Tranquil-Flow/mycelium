# SPDX-License-Identifier: AGPL-3.0-only
"""The native Windows managed member, its installed sidecar and its capability report.

``RemoteWindowsMember`` is the macOS target member
(``mycelium_a13_macos.peer.RemoteMacMember``: recipient-only pairing, encrypted
handoff, canonical HTTPS seed client, durable resume) declaring the
``windows_native_iroh`` class and the NumPy stage runtime.  All of its private
state goes through ``mycelium_host_platform`` owner-only directories on win32.

Under the 2026-10-07 owner parity ruling ``windows_native_iroh`` is
activation-eligible exactly like the macOS and Linux classes.
"""

from __future__ import annotations

from pathlib import Path
import platform
import shutil
import stat
import sys
from typing import Any

from mycelium_a13_macos.peer import RemoteMacMember

PEER_CLASS = "windows_native_iroh"
RUNTIME_CAPABILITY = {
    "runtime_backend": "numpy",
    "transport": "iroh",
    "activation_protocol": "mycelium.router_wire.v1",
}
SIDECAR_NAME = "mycelium-iroh-sidecar.exe"


class RemoteWindowsMember(RemoteMacMember):
    PEER_CLASS = PEER_CLASS
    RUNTIME_CAPABILITY = RUNTIME_CAPABILITY
    SOFTWARE_VERSION = "mycelium-a13-windows/1"


def installed_sidecar(*, module_root: Path | None = None, frozen: bool | None = None,
                      executable: str | None = None) -> Path:
    """The installed (packaged) or lane-built sidecar; never a peer-supplied path."""

    module = Path(__file__).absolute().parent if module_root is None else Path(module_root)
    is_frozen = getattr(sys, "frozen", False) if frozen is None else frozen
    if is_frozen:
        boundary = Path(sys.executable if executable is None else executable).resolve().parent
        candidate = boundary / "native" / SIDECAR_NAME
    else:
        boundary = module.parents[1].resolve()
        candidate = boundary / ".build-native" / "release" / SIDECAR_NAME
    try:
        if candidate.is_symlink():
            raise ValueError("native_sidecar_installation_invalid")
        path = candidate.resolve(strict=True)
        info = path.lstat()
    except OSError:
        raise ValueError("native_sidecar_installation_invalid") from None
    if not path.is_relative_to(boundary) or not stat.S_ISREG(info.st_mode):
        raise ValueError("native_sidecar_installation_invalid")
    return path


def runtime_probe_report() -> dict[str, Any]:
    """One float32 NumPy operation: runtime availability, never qualification."""

    report: dict[str, Any] = {"protocol": "mycelium.a13_runtime_probe.v1", "backend": "numpy",
                              "dtype": "float32", "available": False, "model_loaded": False}
    try:
        import numpy as np

        value = np.array([1.0], dtype=np.float32)
        result = value + value
        report.update(value=float(result[0]), device="cpu")
        report["available"] = result.dtype == np.float32 and float(result[0]) == 2.0
    except (ImportError, RuntimeError, ValueError) as exc:
        report["error_type"] = type(exc).__name__
    return report


def observe_windows(root: Path, *, system: str | None = None, machine: str | None = None,
                    total_memory: int | None = None) -> dict[str, Any]:
    system = platform.system() if system is None else system
    machine = platform.machine() if machine is None else machine
    if system != "Windows" or machine not in {"AMD64", "x86_64"}:
        raise ValueError("unsupported_windows")
    if total_memory is None:
        import mycelium_host_platform

        total_memory = _total_physical_bytes(mycelium_host_platform.win32_api())
    available = shutil.disk_usage(Path(root)).free
    if total_memory <= 0 or available < 0:
        raise ValueError("windows_resources_unavailable")
    available_backend = runtime_probe_report()["available"]
    return {
        "platform": "windows",
        "architecture": "x86_64",
        "memory_bytes": int(total_memory),
        "available_storage_bytes": int(available),
        "backends": ["numpy"] if available_backend else [],
        "precisions": ["float32"] if available_backend else [],
        "runtime_evidence": "local_arithmetic_self_probe" if available_backend else "unavailable",
        "independently_qualified": False,
    }


def _total_physical_bytes(api: Any) -> int:
    total = getattr(api, "total_physical_bytes", None)
    if callable(total):
        return int(total())
    raise ValueError("windows_resources_unavailable")


__all__ = [
    "PEER_CLASS",
    "RUNTIME_CAPABILITY",
    "RemoteWindowsMember",
    "installed_sidecar",
    "observe_windows",
    "runtime_probe_report",
]
