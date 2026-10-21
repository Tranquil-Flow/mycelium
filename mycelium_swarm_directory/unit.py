# SPDX-License-Identifier: AGPL-3.0-only
"""Render the prepared launchd templates of the always-on directory unit (text only).

Nothing here installs, loads or starts anything: the owner copies a rendered file into
``~/Library/LaunchAgents`` and loads it (see the lane REPORT for the exact steps).
"""
from __future__ import annotations

from pathlib import Path
from typing import Mapping
from xml.sax.saxutils import escape

DEPLOY_DIR = Path(__file__).resolve().parents[1] / 'deploy'
TEMPLATES = frozenset({'swarm-directory.plist', 'swarm-directory-watchdog.plist'})
PLACEHOLDERS = ('MYCELIUM_HOME', 'MYCELIUM_PYTHON', 'MYCELIUM_REPO_ROOT', 'SWARM_DIR_STATE_DIR', 'MYCELIUM_LOG_DIR')


class UnitError(RuntimeError):
    pass


def render_unit(name: str, mapping: Mapping[str, str]) -> bytes:
    if name not in TEMPLATES:
        raise UnitError('template_unknown')
    missing = [key for key in PLACEHOLDERS if not mapping.get(key)]
    if missing:
        raise UnitError('unsatisfied_placeholder:' + ','.join(missing))
    text = (DEPLOY_DIR / name).read_text()
    for key in PLACEHOLDERS:
        text = text.replace('__%s__' % key, escape(str(mapping[key])))
    leftovers = [key for key in PLACEHOLDERS if '__%s__' % key in text]
    if leftovers:
        raise UnitError('unsatisfied_placeholder:' + ','.join(leftovers))
    return text.encode()
