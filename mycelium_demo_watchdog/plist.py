# SPDX-License-Identifier: AGPL-3.0-only
"""LaunchAgent plist TEMPLATE rendering.

The template ships with ``__PLACEHOLDER__`` tokens; the operator fills them in at
install time.  Nothing in this lane loads the plist -- the packet gates the
deployment on the laptop returning, so the plist is text here and text only.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Mapping

__all__ = [
    "PLACEHOLDERS",
    "TEMPLATE_PATH",
    "UnsatisfiedPlaceholder",
    "render_plist",
]

TEMPLATE_PATH = Path(__file__).resolve().parents[1] / "deploy" / "demo-watchdog.plist"

PLACEHOLDERS: tuple[str, ...] = (
    "MYCELIUM_HOME",
    "MYCELIUM_PYTHON",
    "MYCELIUM_REPO_ROOT",
    "MYCELIUM_STATE_DIR",
    "MYCELIUM_LOG_DIR",
)


class UnsatisfiedPlaceholder(RuntimeError):
    """Raised rather than emitting a plist that still carries a token."""


def render_plist(
    mapping: Mapping[str, str],
    *,
    template_path: str | os.PathLike[str] = TEMPLATE_PATH,
) -> bytes:
    """Substitute every placeholder, refusing to return a partially rendered file."""

    text = Path(template_path).read_text()
    missing = [name for name in PLACEHOLDERS if not mapping.get(name)]
    if missing:
        raise UnsatisfiedPlaceholder(f"unsatisfied_placeholder:{','.join(missing)}")
    for name in PLACEHOLDERS:
        text = text.replace(f"__{name}__", str(mapping[name]))
    leftovers = [name for name in PLACEHOLDERS if f"__{name}__" in text]
    if leftovers:
        raise UnsatisfiedPlaceholder(f"unsatisfied_placeholder:{','.join(leftovers)}")
    return text.encode("utf-8")
