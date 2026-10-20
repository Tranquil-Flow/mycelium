# SPDX-License-Identifier: AGPL-3.0-only
"""Short, deterministic Unix-socket roots for live-route nodes.

macOS ``sun_path`` holds 104 bytes including the NUL and the node refuses a sidecar socket path of 100 bytes or
more (``sidecar_socket_path_too_long``), so a deep run root cannot also host ``<root>/socket/i.sock``.  A root
that fits keeps its legacy ``<staging>/socket`` location; one that does not moves to a hashed directory under a
short base, derived only from the staging root so the plan and the node agree and a rebuild is reproducible.
"""
from __future__ import annotations

import hashlib
import os

SOCKET_FILE_NAME = "i.sock"
SOCKET_PATH_LIMIT = 100  # the node's own bound: len(os.fsencode(socket_path)) < 100
SHORT_SOCKET_BASE = "/tmp/mycelium-s"
_DIGEST_HEX = 16


def live_socket_root(staging_root: str, *, base: str = SHORT_SOCKET_BASE) -> str:
    """The sidecar socket root for ``staging_root``; its ``i.sock`` is always under ``SOCKET_PATH_LIMIT`` bytes."""
    legacy = f"{staging_root}/socket"
    if len(os.fsencode(f"{legacy}/{SOCKET_FILE_NAME}")) < SOCKET_PATH_LIMIT:
        return legacy
    digest = hashlib.sha256(os.fsencode(staging_root)).hexdigest()[:_DIGEST_HEX]
    short = f"{base}/{digest}"
    if len(os.fsencode(f"{short}/{SOCKET_FILE_NAME}")) >= SOCKET_PATH_LIMIT:
        raise ValueError("socket_base_too_long")
    return short
