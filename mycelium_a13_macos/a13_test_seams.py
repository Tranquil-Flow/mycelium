# SPDX-License-Identifier: AGPL-3.0-only
"""Gated product TEST SEAM: a forced member clock offset for the A13 app-flow gate.

``MYCELIUM_A13_TEST_CLOCK_OFFSET_SECONDS`` shifts the clock the joined (member / target) role signs and validates
managed-control and run-exchange timestamps with, and ONLY when ``MYCELIUM_A13_TEST_SEAMS=1`` is also set.  Without the
gate variable the offset variable is ignored.  The owner role never reads it.  An unparseable, non-finite or implausible
(|offset| > 1 hour) value never activates the seam.  When active the app prints a loud warning and ``/api/status`` shows
``test_seams_active: true``.
"""
from __future__ import annotations

import json
import logging
import math
import os
import time
from collections.abc import Callable, Mapping

GATE_ENVIRONMENT = 'MYCELIUM_A13_TEST_SEAMS'
OFFSET_ENVIRONMENT = 'MYCELIUM_A13_TEST_CLOCK_OFFSET_SECONDS'
MAX_OFFSET_SECONDS = 3600.0

_LOG = logging.getLogger('mycelium.a13.test_seams')


def clock_offset_seconds(environ: Mapping[str, str] | None = None) -> float | None:
    """The forced offset, or None when the seam is inert (gate not exactly ``1``, no/invalid offset)."""
    environ = os.environ if environ is None else environ
    if environ.get(GATE_ENVIRONMENT) != '1':
        return None
    raw = environ.get(OFFSET_ENVIRONMENT)
    if raw is None:
        return None
    try:
        value = float(raw.strip())
    except (TypeError, ValueError):
        return None
    if not math.isfinite(value) or abs(value) > MAX_OFFSET_SECONDS:
        return None
    return value


def member_clock(environ: Mapping[str, str] | None = None) -> Callable[[], float] | None:
    """A wall clock shifted by the forced offset, or None when the seam is inert."""
    offset = clock_offset_seconds(environ)
    if offset is None:
        return None
    return lambda: time.time() + offset


def announce(offset: float) -> None:
    """The loud warning: a product started with a test seam must never pass unnoticed in its log."""
    warning = (f'TEST SEAM ACTIVE: the member clock is forced {offset:+.3f} s from real time '
               f'({OFFSET_ENVIRONMENT} with {GATE_ENVIRONMENT}=1). Never run a real swarm like this.')
    _LOG.warning(warning)
    print(json.dumps({'event': 'a13_test_seams_active', 'clock_offset_seconds': offset, 'warning': warning},
                     sort_keys=True), flush=True)


def status(offset: float | None) -> dict:
    return {'active': offset is not None, 'clock_offset_seconds': offset}
