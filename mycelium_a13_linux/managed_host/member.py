# SPDX-License-Identifier: AGPL-3.0-only
"""The native Linux managed member, its installed sidecar and its capability report.

``RemoteLinuxMember`` is the macOS target member
(``mycelium_a13_macos.peer.RemoteMacMember``: recipient-only pairing, encrypted
handoff, canonical HTTPS seed client, durable resume) declaring the
``linux_native_iroh`` class and the NumPy stage runtime.
"""

from __future__ import annotations

import os
from pathlib import Path
import platform
import shutil
import stat
import sys
from typing import Any

from mycelium_a13_macos.peer import RemoteMacMember
from mycelium_a13_windows.managed_host.member import runtime_probe_report

PEER_CLASS = "linux_native_iroh"
RUNTIME_CAPABILITY = {
    "runtime_backend": "numpy",
    "transport": "iroh",
    "activation_protocol": "mycelium.router_wire.v1",
}
SIDECAR_NAME = "mycelium-iroh-sidecar"
ARCHITECTURES = frozenset({"x86_64", "aarch64"})


class RemoteLinuxMember(RemoteMacMember):
    PEER_CLASS = PEER_CLASS
    RUNTIME_CAPABILITY = RUNTIME_CAPABILITY
    SOFTWARE_VERSION = "mycelium-a13-linux/1"


def installed_sidecar(*, module_root: Path | None = None, frozen: bool | None = None,
                      executable: str | None = None) -> Path:
    """The packaged (frozen one-folder) or lane-built sidecar; never a peer-supplied path.

    Frozen: ``<app>/_internal/mycelium_a13_macos/native/mycelium-iroh-sidecar``
    inside the installed version folder.  Source: ``.build-native/release``.
    It must be a regular, owner-owned, executable file that group and others
    cannot write, inside that boundary.
    """

    is_frozen = getattr(sys, "frozen", False) if frozen is None else frozen
    if is_frozen:
        boundary = Path(sys.executable if executable is None else executable).resolve().parent
        candidate = boundary / "_internal" / "mycelium_a13_macos" / "native" / SIDECAR_NAME
    else:
        module = Path(__file__).absolute().parent if module_root is None else Path(module_root)
        boundary = module.parents[1].resolve()
        candidate = boundary / ".build-native" / "release" / SIDECAR_NAME
    try:
        if candidate.is_symlink():
            raise ValueError("native_sidecar_installation_invalid")
        path = candidate.resolve(strict=True)
        info = path.lstat()
    except OSError:
        raise ValueError("native_sidecar_installation_invalid") from None
    if (not path.is_relative_to(boundary) or not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.geteuid() or not os.access(path, os.X_OK) or info.st_mode & 0o022):
        raise ValueError("native_sidecar_installation_invalid")
    return path


def _total_memory_bytes() -> int:
    pages = os.sysconf("SC_PHYS_PAGES")
    size = os.sysconf("SC_PAGE_SIZE")
    return int(pages) * int(size)


def observe_linux(root: Path, *, system: str | None = None, machine: str | None = None,
                  total_memory: int | None = None) -> dict[str, Any]:
    system = platform.system() if system is None else system
    machine = platform.machine() if machine is None else machine
    if system != "Linux" or machine not in ARCHITECTURES:
        raise ValueError("unsupported_linux")
    total_memory = _total_memory_bytes() if total_memory is None else total_memory
    available = shutil.disk_usage(Path(root)).free
    if total_memory <= 0 or available < 0:
        raise ValueError("linux_resources_unavailable")
    available_backend = runtime_probe_report()["available"]
    return {
        "platform": "linux",
        "architecture": machine,
        "memory_bytes": int(total_memory),
        "available_storage_bytes": int(available),
        "backends": ["numpy"] if available_backend else [],
        "precisions": ["float32"] if available_backend else [],
        "runtime_evidence": "local_arithmetic_self_probe" if available_backend else "unavailable",
        "independently_qualified": False,
    }


__all__ = ["PEER_CLASS", "RUNTIME_CAPABILITY", "RemoteLinuxMember", "installed_sidecar", "observe_linux"]
