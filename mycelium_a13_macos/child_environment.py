# SPDX-License-Identifier: AGPL-3.0-only
"""The environment of the app's own source-mode child processes.

Source mode launches package entry points as ``python -m mycelium_a13_macos.<x>``
(the native node in ``worker``, the staging operation in ``managed_staging``).
Those must not depend on the parent's working directory: a parent that found
the package through ``sys.path`` alone (pytest from a sub-directory, an
installed entry point) would otherwise start a child that cannot import it.
"""

from __future__ import annotations

import os
from pathlib import Path
import sys


def source_mode_environment(environ=None):
    """The child's environment with the package root first on PYTHONPATH.

    Existing PYTHONPATH entries are kept after the root.  A frozen app returns
    ``None`` (inherit unchanged): its children run through fixed trampolines
    (``--managed-node``, ``--managed-stage``), never ``-m``.
    """
    if getattr(sys, "frozen", False):
        return None
    source = dict(os.environ if environ is None else environ)
    root = str(Path(__file__).resolve().parents[1])
    existing = [entry for entry in source.get("PYTHONPATH", "").split(os.pathsep) if entry and entry != root]
    source["PYTHONPATH"] = os.pathsep.join([root, *existing])
    return source


__all__ = ["source_mode_environment"]
