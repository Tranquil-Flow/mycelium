# SPDX-License-Identifier: AGPL-3.0-only
"""Peer classes that may run as A13 managed hosts, and the stage runtime each runs.

A managed host is admitted by its exact registered peer class.  The macOS app
runs the MLX stage runtime; the native Windows host runs the NumPy stage
runtime (with the optional pinned A9 int8 provider), and so does the native
Linux host.  Any other class is not a managed host.
"""

from __future__ import annotations

MANAGED_PEER_RUNTIMES = {
    "mac_mlx_iroh": "mlx",
    "windows_native_iroh": "numpy",
    # Owner parity ruling 2026-10-07 applied to Linux: the packaged Linux host.
    "linux_native_iroh": "numpy",
}


def is_managed_peer_class(peer_class: object) -> bool:
    return isinstance(peer_class, str) and peer_class in MANAGED_PEER_RUNTIMES


def managed_runtime_backend(peer_class: object) -> str:
    if not is_managed_peer_class(peer_class):
        raise ValueError("managed_peer_class_invalid")
    return MANAGED_PEER_RUNTIMES[peer_class]  # type: ignore[index]


__all__ = ["MANAGED_PEER_RUNTIMES", "is_managed_peer_class", "managed_runtime_backend"]
